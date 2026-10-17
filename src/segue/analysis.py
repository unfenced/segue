"""Finding things in the library, and the "Needs a look" report.

Nothing here changes the library; each finding comes with the command that would fix it.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass

from . import dupes, select
from .model import Track, normalize
from .store import Store

SIMILAR_DEFAULT = 0.8


@dataclass
class Hit:
    track: Track
    places: list[tuple[int, str, list[int]]]  # (playlist id, name, 1-based positions)


def where(store: Store, query: str) -> list[Hit]:
    """Tracks whose title or artists contain ``query``, and every playlist they're in."""
    words = normalize(query).split()
    if not words:
        raise ValueError("search for at least one word")
    places: dict[int, dict[int, list[int]]] = {}
    for row in store.db.execute("SELECT id, norm_title, norm_artists FROM tracks"):
        text = f"{row['norm_title']} {row['norm_artists']}"
        if all(word in text for word in words):
            places[row["id"]] = {}
    names = {}
    for summary in store.playlist_summaries():
        names[summary.id] = summary.name
        state = store.playlist_state(summary.id)
        for position, entry in enumerate(state.entries, start=1):
            if entry.track_id in places:
                places[entry.track_id].setdefault(summary.id, []).append(position)
    hits = [
        Hit(store.get_track(tid), [(pid, names[pid], pos) for pid, pos in found.items()])
        for tid, found in places.items()
    ]
    hits.sort(key=lambda h: (-len(h.places), normalize(h.track.title)))
    return hits


def unplaylisted(store: Store) -> list[Track]:
    """Library tracks that are in no playlist (left over from deleted playlists)."""
    rows = store.db.execute(
        "SELECT id FROM tracks WHERE id NOT IN (SELECT track_id FROM playlist_entries) ORDER BY norm_title"
    )
    return [store.get_track(row[0]) for row in rows]


def liked_unplaylisted(store: Store) -> list[Track]:
    """Liked songs that aren't in any of your other playlists."""
    rows = store.db.execute(
        "SELECT DISTINCT e.track_id FROM playlist_entries e JOIN playlists p ON p.id = e.playlist_id"
        " WHERE p.kind = 'liked' AND e.track_id NOT IN ("
        "   SELECT e2.track_id FROM playlist_entries e2 JOIN playlists p2 ON p2.id = e2.playlist_id"
        "   WHERE p2.kind != 'liked')"
    )
    return [store.get_track(row[0]) for row in rows]


@dataclass
class Overlap:
    a: int
    b: int
    kind: str  # identical | subset | similar
    shared: int
    ratio: float  # shared / size of the union


def overlaps(store: Store, ids: list[int] | None = None, min_ratio: float = SIMILAR_DEFAULT) -> list[Overlap]:
    """Pairs of playlists on the same service that share most of their tracks.

    Compared as sets of tracks, per the duplicate setting. ``subset`` means every track of
    ``a`` is also in ``b``. Liked songs are left out: most tracks are in it on purpose.
    """
    if ids is None:
        ids = [s.id for s in store.playlist_summaries() if s.kind != "liked"]
    key = dupes.key_function(store, dupes.level(store))
    states = {pid: store.playlist_state(pid) for pid in ids}
    sets = {pid: {key(e) for e in states[pid].entries} for pid in ids}
    by_track: dict = {}
    for pid in ids:
        for k in sets[pid]:
            by_track.setdefault(k, []).append(pid)
    shared: dict[tuple[int, int], int] = {}
    for pids in by_track.values():
        for i, a in enumerate(pids):
            for b in pids[i + 1:]:
                shared[(a, b)] = shared.get((a, b), 0) + 1

    found = []
    for (a, b), n in shared.items():
        if select.service_of(states[a]) != select.service_of(states[b]):
            continue
        size_a, size_b = len(sets[a]), len(sets[b])
        union = size_a + size_b - n
        ratio = n / union
        if size_a == size_b == n:
            # "identical" matches what `delete --identical` selects: exactly the same tracks.
            same = Counter(states[a].track_ids) == Counter(states[b].track_ids)
            found.append(Overlap(a, b, "identical" if same else "similar", n, 1.0))
        elif n == size_a or n == size_b:
            small, big = (a, b) if n == size_a else (b, a)
            found.append(Overlap(small, big, "subset", n, ratio))
        elif ratio >= min_ratio:
            found.append(Overlap(a, b, "similar", n, ratio))
    order = {"identical": 0, "subset": 1, "similar": 2}
    found.sort(key=lambda o: (order[o.kind], -o.ratio, o.a, o.b))
    return found


# Needs a look

@dataclass
class Finding:
    title: str
    lines: list[str]
    fix: str | None = None


def check(store: Store) -> list[Finding]:
    """Everything in the library worth a look, each with a suggested command."""
    summaries = store.playlist_summaries()
    editable = [s.id for s in summaries if s.kind in ("created", "collaborative")]
    name = {s.id: s.name for s in summaries}
    findings: list[Finding] = []

    empty = [s for s in summaries if s.id in set(editable) and s.track_count == 0]
    if empty:
        findings.append(Finding(
            f"{len(empty)} empty playlist{'s' if len(empty) != 1 else ''}",
            [repr(s.name) for s in empty],
            "segue delete --empty",
        ))

    groups = select.name_groups(store, editable)
    if groups:
        findings.append(Finding(
            f"{len(groups)} name{'s' if len(groups) != 1 else ''} used by more than one playlist",
            [", ".join(f"{name[pid]!r} (#{pid})" for pid in g) for g in groups],
            "segue merge-names --dry-run",
        ))

    # Pairs in the same name group are already handled by merge-names; don't report them twice.
    same_name = {pid: n for n, g in enumerate(groups) for pid in g}
    found = [
        o for o in overlaps(store, editable)
        if same_name.get(o.a, -1) != same_name.get(o.b, -2)
    ]
    identical = [o for o in found if o.kind == "identical"]
    if identical:
        findings.append(Finding(
            f"{len(identical)} pair{'s' if len(identical) != 1 else ''} of playlists with the same tracks",
            [f"{name[o.a]!r} and {name[o.b]!r} ({o.shared} tracks)" for o in identical],
            "segue delete --identical --dry-run",
        ))
    for o in [o for o in found if o.kind == "subset"]:
        findings.append(Finding(
            f"{name[o.a]!r} is entirely inside {name[o.b]!r}",
            [f"all {o.shared} of its tracks are in {name[o.b]!r}"],
            f"segue merge {o.a} --into {o.b} --delete-sources --dry-run",
        ))
    similar = [o for o in found if o.kind == "similar"]
    if similar:
        findings.append(Finding(
            f"{len(similar)} pair{'s' if len(similar) != 1 else ''} of nearly identical playlists",
            [f"{name[o.a]!r} and {name[o.b]!r} share {o.ratio:.0%} ({o.shared} tracks)" for o in similar],
            f"segue merge {similar[0].b} --into {similar[0].a} --dry-run",
        ))

    level, keep = dupes.level(store), dupes.keep(store)
    key = dupes.key_function(store, level)
    with_dupes = []
    for pid in editable:
        count = len(dupes.duplicate_positions(store.playlist_state(pid).entries, key, keep))
        if count:
            with_dupes.append((pid, count))
    if with_dupes:
        findings.append(Finding(
            f"{len(with_dupes)} playlist{'s' if len(with_dupes) != 1 else ''} with duplicates ({level})",
            [f"{name[pid]!r}: {n} duplicate{'s' if n != 1 else ''}" for pid, n in with_dupes],
            "segue dedupe " + " ".join(str(pid) for pid, _ in with_dupes) + " --dry-run",
        ))
    return findings
