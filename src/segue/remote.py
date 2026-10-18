"""Pulling libraries from services into Segue, and writing changes back.

For every playlist linked to a service, Segue keeps what that service last had
(``remote_state``). A push writes the difference between that and the playlist's current
state, then reads the playlist back and stores the result. Before writing anything, a push
re-reads every playlist it will touch: if one changed on the service since (for example
from your phone), nothing is written and the conflict is reported instead.

New playlists made by organizing (merge into a new one, split, clone, restore) are created
on every *destination* service. Tracks that have no ID on a service yet are kept in Segue's
copy and skipped there; matching across services comes later.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from typing import Protocol

from . import changes
from .model import EntryState, Playlist, PlaylistState
from .store import Store


class Service(Protocol):
    name: str
    display: str

    def read_library(self) -> list[Playlist]: ...
    def read_playlist(self, remote_id: str) -> Playlist: ...
    def write(self, remote_id, remote, target, track_id) -> tuple[str | None, list[str]]: ...


class RemoteError(Exception):
    pass


@dataclass
class PullReport:
    added: int = 0
    updated: int = 0
    removed: int = 0
    unchanged: int = 0
    batch_id: int | None = None


@dataclass
class PushReport:
    written: list[tuple[str, str, list[str]]] = field(default_factory=list)  # (playlist, service, notes)
    conflicts: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)  # new playlists waiting for a service to be connected
    unmatched: list[str] = field(default_factory=list)  # playlists with tracks the service has no ID for

    @property
    def ok(self) -> bool:
        return not self.conflicts and not self.errors


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def destinations(store: Store) -> list[str]:
    """Where new playlists made by organizing are created. Default: every service you've pulled from."""
    value = store.get_setting("destinations")
    if value is not None:
        return [s for s in value.split(",") if s]
    return sorted({service for _, service, _, _ in store.remote_links()})


def set_destinations(store: Store, value: str, known: tuple[str, ...]) -> None:
    services = [s.strip().lower() for s in value.split(",") if s.strip()]
    unknown = [s for s in services if s not in known]
    if unknown:
        raise ValueError(f"unknown service {unknown[0]!r}; choose from {', '.join(known)}")
    store.set_setting("destinations", ",".join(dict.fromkeys(services)))


def last_pull(store: Store, service: str) -> str | None:
    return store.get_setting(f"pulled_at:{service}")


# Pull

def pull(store: Store, service: Service) -> PullReport:
    """Read the whole library from a service into Segue, as one change that can be undone."""
    linked = {pid: rid for pid, svc, rid, _ in store.remote_links() if svc == service.name}
    staged = {pid for pid, _, _ in changes.staged_changes(store)}
    if set(linked) & staged:
        raise RemoteError(f"some playlists from {service.display} have staged changes; "
                          "commit or discard them before pulling")
    report = PullReport()
    playlists = service.read_library()
    writes: list[tuple[int | None, PlaylistState | None]] = []
    remote_states: list[tuple[str, PlaylistState]] = []
    seen: set[str] = set()
    for playlist in playlists:
        seen.add(playlist.source.id)
        remote = store.to_state(playlist)
        pid = store.find_by_source(playlist.source)
        if pid is None:
            report.added += 1
            writes.append((None, remote))
        else:
            merged = keep_unmatched(store, store.playlist_state(pid), remote, service.name)
            writes.append((pid, merged))
        remote_states.append((playlist.source.id, remote))
    gone = [pid for pid, rid in linked.items() if rid not in seen]
    writes += [(pid, None) for pid in gone]
    report.removed = len(gone)

    with store.transaction():
        before = {pid: store.playlist_state(pid) for pid, _ in writes if pid is not None}
        ids, report.batch_id = changes.record_states(store, f"pull from {service.display}", writes)
        for pid, (remote_id, remote) in zip(ids, remote_states):
            store.set_remote(pid, service.name, remote_id, remote)
        for pid in gone:
            store.remove_remote(pid, service.name)
        store.set_setting(f"pulled_at:{service.name}", _now())
    for pid, state in writes:
        if pid is not None and state is not None:
            if before.get(pid) == state:
                report.unchanged += 1
            else:
                report.updated += 1
    return report


def keep_unmatched(store: Store, old: PlaylistState | None, fresh: PlaylistState, service: str) -> PlaylistState:
    """``fresh`` (as read from a service), plus the entries of ``old`` the service can't hold.

    Tracks without an ID on the service were never written there, so a pull mustn't drop
    them: each goes back after the entry it followed, or first if it was first.
    """
    if old is None:
        return fresh
    lookup = _track_ids(store, service)
    entries = list(fresh.entries)
    index = {e.handle: i for i, e in enumerate(entries) if e.handle}
    previous_handle = None
    inserts: list[tuple[int, EntryState]] = []
    for entry in old.entries:
        if lookup(entry.track_id, entry.ref_service, entry.ref_id) is None:
            at = index[previous_handle] + 1 if previous_handle in index else 0
            inserts.append((at, entry))
        elif entry.handle:
            previous_handle = entry.handle
    for offset, (at, entry) in enumerate(sorted(inserts, key=lambda x: x[0])):
        entries.insert(at + offset, entry)
    return replace(fresh, entries=tuple(entries))


def _track_ids(store: Store, service: str):
    """``(track_id, ref_service, ref_id) -> the service's track ID or None``."""
    cache: dict[int, str | None] = {}

    def lookup(track_id: int, ref_service: str | None, ref_id: str | None) -> str | None:
        if ref_service == service and ref_id:
            return ref_id
        if track_id not in cache:
            ref = store.get_track(track_id).ref(service)
            cache[track_id] = ref.id if ref else None
        return cache[track_id]

    return lookup


def _holdable(state: PlaylistState | None, lookup) -> tuple | None:
    """The part of a playlist's content a service can show: tracks it has an ID for.

    Liked songs are compared as a set: services keep them in their own order (date liked).
    """
    if state is None:
        return None
    entries = tuple(e for e in state.entries if lookup(e.track_id, e.ref_service, e.ref_id) is not None)
    if state.kind == "liked":
        entries = tuple(sorted(entries, key=lambda e: (e.track_id, e.ref_id or "")))
        return ("liked", tuple(dict.fromkeys(e.track_id for e in entries)))
    return replace(state, entries=entries).content()


# Push

@dataclass
class _Target:
    pid: int
    service: str
    remote_id: str | None
    remote: PlaylistState | None
    current: PlaylistState | None


def pending_pushes(store: Store, services: dict[str, Service]) -> list[_Target]:
    """Everything that differs between Segue and the services it's linked to."""
    targets: list[_Target] = []
    linked: dict[int, set[str]] = {}
    for pid, svc, rid, remote in store.remote_links():
        linked.setdefault(pid, set()).add(svc)
        current = store.playlist_state(pid)
        lookup = _track_ids(store, svc)
        if _holdable(current, lookup) != _holdable(remote, lookup):
            targets.append(_Target(pid, svc, rid, remote, current))
        elif current is not None and current.entries != remote.entries:
            _adopt_service_fields(store, pid, current, remote, svc)
    created = _created_by_organizing(store)
    for summary in store.playlist_summaries():
        if summary.id in linked:
            continue
        if summary.id in created:  # made by organizing: goes to every destination
            for svc in destinations(store):
                targets.append(_Target(summary.id, svc, None, None, store.playlist_state(summary.id)))
        elif summary.source and summary.source.service in services and summary.kind != "liked":
            # came from a service but isn't linked (e.g. restored after a delete): back to that service
            targets.append(_Target(summary.id, summary.source.service, None, None,
                                   store.playlist_state(summary.id)))
    return targets


def push(store: Store, services: dict[str, Service]) -> PushReport:
    """Write every pending difference to its service. Writes nothing if any playlist conflicts."""
    report = PushReport()
    targets = []
    for t in pending_pushes(store, services):
        if t.service in services:
            targets.append(t)
        elif t.remote_id is None:
            report.skipped.append(f"{_name(t)!r} waits for {t.service} to be connected")
        else:
            report.errors.append(f"{_name(t)!r} is linked to {t.service}, which isn't connected; "
                                 "connect it, or commit with --local")
    if report.errors:
        return report
    report.unmatched = unmatched_counts(store, services)

    # Preflight: has anything changed on the services since Segue last read it?
    for t in targets:
        service = services[t.service]
        if t.remote_id is None:
            source = t.current.source if t.current else None
            if source and source.service == t.service:
                try:  # an unlinked playlist that came from this service: adopt it if it still exists
                    fresh = store.to_state(service.read_playlist(source.id))
                    t.remote_id, t.remote = source.id, fresh
                except Exception:
                    pass
            continue
        try:
            fresh = store.to_state(service.read_playlist(t.remote_id))
        except Exception as e:
            report.conflicts.append(f"{_name(t)!r} can't be read from {service.display} ({e}); "
                                    f"it may have been deleted there. Run: segue pull {t.service}")
            continue
        lookup = _track_ids(store, t.service)
        if _holdable(fresh, lookup) != _holdable(t.remote, lookup):
            report.conflicts.append(f"{_name(t)!r} was changed on {service.display} since the last pull. "
                                    f"Run: segue pull {t.service} (after committing or discarding)")
    if report.conflicts:
        return report

    for t in targets:
        service = services[t.service]
        try:
            remote_id, notes = service.write(t.remote_id, t.remote, t.current, _track_ids(store, t.service))
        except Exception as e:
            report.errors.append(f"writing {_name(t)!r} to {service.display} failed: {e}")
            return report  # stop; what was written is refreshed below on the next push
        if t.current is None:
            store.remove_remote(t.pid, t.service)
        else:
            fresh = _read_settled(store, service, remote_id, t.current)
            _record_written(store, t, remote_id, fresh)
        report.written.append((_name(t), service.display, notes))
    return report


def _read_settled(store: Store, service: Service, remote_id: str, target: PlaylistState,
                  tries: int = 3) -> PlaylistState:
    """Read a playlist after writing it, waiting briefly while the service still serves a cached copy."""
    lookup = _track_ids(store, service.name)
    fresh = store.to_state(service.read_playlist(remote_id))
    for _ in range(tries - 1):
        if _holdable(fresh, lookup) == _holdable(target, lookup):
            break
        wait = getattr(service, "wait", None)
        if wait:
            wait()
        fresh = store.to_state(service.read_playlist(remote_id))
    return fresh


def unmatched_counts(store: Store, services: dict[str, Service]) -> list[str]:
    """For each linked playlist: how many of its tracks have no ID on that service yet."""
    lines = []
    for pid, svc, _, _ in store.remote_links():
        current = store.playlist_state(pid)
        if current is None or svc not in services:
            continue
        lookup = _track_ids(store, svc)
        n = sum(1 for e in current.entries if lookup(e.track_id, e.ref_service, e.ref_id) is None)
        if n:
            lines.append(f"{current.name!r}: {n} track{'s' if n != 1 else ''} with no match on "
                         f"{services[svc].display} yet (kept in Segue, matched later)")
    return lines


def _record_written(store: Store, t: _Target, remote_id: str, fresh: PlaylistState) -> None:
    """After a write: remember what the service has, and take its entry IDs and dates."""
    store.set_remote(t.pid, t.service, remote_id, fresh)
    current = store.playlist_state(t.pid)
    if current is None:
        return
    merged = _with_service_fields(store, current, fresh, t.service)
    if current.source is None or (current.source.service == t.service and current.source.id != remote_id):
        merged = replace(merged, source=fresh.source)
    if merged != current:
        store.write_state(t.pid, merged)  # only service-side fields change; not a user change


def _adopt_service_fields(store: Store, pid: int, current: PlaylistState, remote: PlaylistState, service: str) -> None:
    merged = _with_service_fields(store, current, remote, service)
    if merged != current:
        store.write_state(pid, merged)


def _with_service_fields(store: Store, current: PlaylistState, fresh: PlaylistState, service: str) -> PlaylistState:
    """``current`` with entry IDs and dates from ``fresh``, pairing the tracks the service holds in order."""
    lookup = _track_ids(store, service)
    held = iter(fresh.entries)
    entries = []
    for entry in current.entries:
        if lookup(entry.track_id, entry.ref_service, entry.ref_id) is None:
            entries.append(entry)
            continue
        match = next(held, None)
        if match is None or match.track_id != entry.track_id:
            return keep_unmatched(store, current, fresh, service)  # out of step: trust the service
        entries.append(replace(entry, handle=match.handle, added_at=match.added_at or entry.added_at))
    return replace(current, entries=tuple(entries))


def _created_by_organizing(store: Store) -> set[int]:
    rows = store.db.execute(
        "SELECT DISTINCT o.playlist_id FROM operations o JOIN batches b ON b.id = o.batch_id"
        " WHERE o.before IS NULL AND o.after IS NOT NULL AND b.mode IN ('safe', 'auto')"
        " AND b.status IN ('staged', 'committed')"
    )
    return {row[0] for row in rows}


def _name(t: _Target) -> str:
    state = t.current or t.remote
    return state.name if state else f"playlist {t.pid}"


def describe_pending(store: Store, services: dict[str, Service]) -> list[str]:
    lines = []
    for t in pending_pushes(store, services):
        where = services[t.service].display if t.service in services else t.service
        if t.current is None:
            lines.append(f"{where}: delete {_name(t)!r}")
        elif t.remote_id is None:
            lines.append(f"{where}: create {_name(t)!r} ({len(t.current.entries)} tracks)")
        else:
            lines.append(f"{where}: update {_name(t)!r}")
    return lines
