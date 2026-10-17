"""The change log: apply, undo, redo and commit batches of operations.

A batch is one user action (a merge, a dedupe, ...). It changes Segue's library as soon as
it's made, and every operation stores its playlist's state before and after.

- **Safe mode** (default): the batch is *staged*. Undo and redo step through staged batches;
  ``commit`` makes them final, and after that they can't be undone (restore a backup instead).
  Once services are connected, commit is when changes are written to them.
- **Auto mode**: the batch is committed right away, but the most recent ones (``undo-limit``,
  default 20) can still be undone.

Undo writes the "before" state back only if the playlist still looks exactly like "after",
so an undo never silently overwrites a later change. Making a new change clears the redo list.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone

from .model import Playlist, PlaylistState
from .ops import Operation, describe_change
from .store import Store

APPLY_MODES = ("safe", "auto")
DEFAULT_APPLY_MODE = "safe"
DEFAULT_UNDO_LIMIT = 20


class ChangeError(Exception):
    """A change can't be applied, undone or redone as asked."""


@dataclass
class Batch:
    id: int
    description: str
    status: str  # staged | committed | undone | discarded
    mode: str  # safe | auto | import
    created_at: str
    applied_at: str | None
    undone_at: str | None


_BATCH_COLUMNS = "id, description, status, mode, created_at, applied_at, undone_at"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _dump(state: PlaylistState | None) -> str | None:
    return None if state is None else json.dumps(state.to_dict())


def _load(text: str | None) -> PlaylistState | None:
    return None if text is None else PlaylistState.from_dict(json.loads(text))


# Settings

def apply_mode(store: Store) -> str:
    return store.get_setting("apply_mode", DEFAULT_APPLY_MODE)


def set_apply_mode(store: Store, mode: str) -> None:
    if mode not in APPLY_MODES:
        raise ValueError(f"apply mode must be one of: {', '.join(APPLY_MODES)}")
    store.set_setting("apply_mode", mode)


def undo_limit(store: Store) -> int:
    return int(store.get_setting("undo_limit", str(DEFAULT_UNDO_LIMIT)))


def set_undo_limit(store: Store, value: str | int) -> None:
    try:
        limit = int(value)
    except ValueError:
        raise ValueError("undo-limit must be a whole number") from None
    if limit < 0:
        raise ValueError("undo-limit can't be negative")
    store.set_setting("undo_limit", str(limit))


# Previews

def track_label(store: Store):
    cache: dict[int, str] = {}

    def label(track_id: int) -> str:
        if track_id not in cache:
            t = store.get_track(track_id)
            cache[track_id] = f"{', '.join(t.artists)} - {t.title}" if t.artists else t.title
        return cache[track_id]

    return label


def preview(store: Store, ops: list[Operation]) -> list[str]:
    """Describe what ``ops`` would do to the library as it is now, without changing it."""
    states: dict[int, PlaylistState | None] = {}
    label = track_label(store)
    lines = []
    for op in ops:
        target = op.target
        if target is not None and target not in states:
            states[target] = store.playlist_state(target)
        before = states.get(target) if target is not None else None
        lines.append(op.describe(before, label))
        try:
            after = op.apply(before)
            if target is not None:
                states[target] = after
        except ValueError as e:
            lines[-1] += f"  [will fail: {e}]"
    return lines


def describe_batch(store: Store, batch_id: int) -> list[str]:
    """What a batch did, from the snapshots it recorded."""
    label = track_label(store)
    lines = []
    for row in _op_rows(store, batch_id):
        data = json.loads(row["op"])
        if data["kind"] == "import":
            lines.append(describe_change(_load(row["before"]), _load(row["after"]), label))
        else:
            lines.append(Operation.from_dict(data).describe(_load(row["before"]), label))
    return lines


def staged_changes(store: Store) -> list[tuple[int, PlaylistState | None, PlaylistState | None]]:
    """The net effect of everything staged: ``(playlist_id, committed, current)`` per playlist.

    This is what a commit will write once services are connected.
    """
    base: dict[int, PlaylistState | None] = {}
    for row in store.db.execute(
        "SELECT o.playlist_id, o.before FROM operations o JOIN batches b ON b.id = o.batch_id"
        " WHERE b.status = 'staged' ORDER BY b.changed_seq, o.seq"
    ):
        base.setdefault(row["playlist_id"], _load(row["before"]))
    result = []
    for pid, committed in base.items():
        current = store.playlist_state(pid)
        if current != committed:
            result.append((pid, committed, current))
    return result


def describe_staged(store: Store) -> list[str]:
    label = track_label(store)
    return [describe_change(old, new, label) for _, old, new in staged_changes(store)]


# Applying

def submit(store: Store, description: str, ops: list[Operation], mode: str | None = None) -> tuple[int, str]:
    """Apply a batch now. Returns ``(batch_id, status)``: "staged" (safe) or "committed" (auto)."""
    if not ops:
        raise ValueError("nothing to do")
    mode = mode or apply_mode(store)
    if mode not in APPLY_MODES:
        raise ValueError(f"apply mode must be one of: {', '.join(APPLY_MODES)}")
    status = "staged" if mode == "safe" else "committed"
    with store.transaction():
        _clear_redo(store)
        batch_id = _new_batch(store, description, mode)
        for seq, op in enumerate(ops):
            pid = op.target
            before = None if pid is None else store.playlist_state(pid)
            try:
                after = op.apply(before)
            except ValueError as e:
                raise ChangeError(str(e)) from e
            if pid is None:
                pid = store.create_state(after)
            else:
                store.write_state(pid, after)
            _record_op(store, batch_id, seq, op.to_dict(), pid, before, after)
        _set_status(store, batch_id, status, "applied_at")
    return batch_id, status


def record_import(store: Store, playlist: Playlist, source_name: str) -> tuple[int, str, int | None]:
    """Save an imported playlist through the log.

    Imports bring data into Segue rather than change a service, so they are committed right
    away in either mode, and undoable like recent auto-mode changes. Returns
    ``(playlist_id, status, batch_id)``; nothing is logged when nothing changed.
    """
    with store.transaction():
        existing = store.find_by_source(playlist.source) if playlist.source else None
        before = store.playlist_state(existing) if existing else None
        pid, status = store.save_playlist(playlist)
        if status == "unchanged":
            return pid, status, None
        after = store.playlist_state(pid)
        if after == before:
            return pid, "unchanged", None  # re-import of an identical playlist
        _clear_redo(store)
        batch_id = _new_batch(store, f"import {playlist.name!r} from {source_name}", "import")
        _record_op(store, batch_id, 0, {"kind": "import", "name": playlist.name, "file": source_name},
                   pid, before, after)
        _set_status(store, batch_id, "committed", "applied_at")
    return pid, status, batch_id


def commit(store: Store) -> list[int]:
    """Make every staged batch final. Returns their IDs, oldest first."""
    ids = [b.id for b in batches(store, ["staged"])]
    with store.transaction():
        for batch_id in ids:
            _set_status(store, batch_id, "committed")
    return ids


def discard(store: Store) -> list[int]:
    """Undo every staged batch, newest first, without keeping them for redo."""
    discarded = []
    with store.transaction():
        while (batch_id := _latest(store, ["staged"])) is not None:
            _revert(store, batch_id)
            _set_status(store, batch_id, "discarded", "undone_at")
            discarded.append(batch_id)
    return discarded


def undo(store: Store, batch_id: int | None = None) -> int:
    """Undo a batch; by default the most recent change."""
    if batch_id is None:
        batch_id = _latest(store, ["staged", "committed"])
        if batch_id is None:
            raise ChangeError("nothing to undo")
    batch = get_batch(store, batch_id)
    if problem := _why_not_undoable(store, batch):
        raise ChangeError(f"can't undo #{batch_id}: {problem}")
    with store.transaction():
        _revert(store, batch_id)
        _set_status(store, batch_id, "undone", "undone_at")
    return batch_id


def redo(store: Store, batch_id: int | None = None) -> int:
    """Re-apply an undone batch; by default the most recently undone one."""
    if batch_id is None:
        batch_id = _latest(store, ["undone"])
        if batch_id is None:
            raise ChangeError("nothing to redo")
    batch = get_batch(store, batch_id)
    if batch.status != "undone":
        raise ChangeError(f"can't redo #{batch_id}: it is {batch.status}, not undone")
    with store.transaction():
        for pid, before, after in _snapshots(store, batch_id):
            _swap(store, batch_id, pid, expect=before, write=after, verb="redo")
        _set_status(store, batch_id, "staged" if batch.mode == "safe" else "committed", "applied_at")
    return batch_id


def can_undo(store: Store, batch: Batch) -> bool:
    """Whether undoing ``batch`` would work right now (allowed, and nothing later in the way)."""
    if _why_not_undoable(store, batch) is not None:
        return False
    return not any(
        _later_batch_touching(store, batch.id, pid) for pid, _, _ in _snapshots(store, batch.id)
    )


def _why_not_undoable(store: Store, batch: Batch) -> str | None:
    if batch.status == "staged":
        return None
    if batch.status != "committed":
        return f"it is {batch.status}"
    if batch.mode == "safe":
        return "it was committed. Committed changes are final; restore a backup to go back"
    limit = undo_limit(store)
    recent = [
        row[0] for row in store.db.execute(
            "SELECT id FROM batches WHERE status = 'committed' AND mode IN ('auto', 'import')"
            " ORDER BY changed_seq DESC LIMIT ?",
            (limit,),
        )
    ]
    if batch.id not in recent:
        return f"only the last {limit} auto-mode changes can be undone (see: segue config undo-limit)"
    return None


def _revert(store: Store, batch_id: int) -> None:
    for pid, before, after in reversed(_snapshots(store, batch_id)):
        _swap(store, batch_id, pid, expect=after, write=before, verb="undo")


def _swap(store: Store, batch_id: int, pid: int, expect, write, verb: str) -> None:
    current = store.playlist_state(pid)
    if current != expect:
        name = (current or expect or write).name
        later = _later_batch_touching(store, batch_id, pid)
        if later and verb == "undo":
            reason = f"{name!r} was changed later by #{later}; undo that first"
        else:
            reason = f"{name!r} has changed since"
        raise ChangeError(f"can't {verb} #{batch_id}: {reason}")
    store.write_state(pid, write)


# Reading the log

def get_batch(store: Store, batch_id: int) -> Batch:
    row = store.db.execute(f"SELECT {_BATCH_COLUMNS} FROM batches WHERE id = ?", (batch_id,)).fetchone()
    if row is None:
        raise ChangeError(f"no change #{batch_id}")
    return Batch(*row)


def batches(store: Store, statuses: list[str] | None = None, limit: int | None = None) -> list[Batch]:
    """Batches in the order they were applied; with ``limit``, the most recent ones, newest first."""
    query = f"SELECT {_BATCH_COLUMNS} FROM batches"
    params: list = []
    if statuses:
        query += f" WHERE status IN ({','.join('?' * len(statuses))})"
        params.extend(statuses)
    if limit:
        query += " ORDER BY changed_seq DESC LIMIT ?"
        params.append(limit)
    else:
        query += " ORDER BY changed_seq, id"
    return [Batch(*row) for row in store.db.execute(query, params)]


def _op_rows(store: Store, batch_id: int):
    return store.db.execute(
        "SELECT op, playlist_id, before, after FROM operations WHERE batch_id = ? ORDER BY seq",
        (batch_id,),
    ).fetchall()


def _snapshots(store: Store, batch_id: int):
    return [(row["playlist_id"], _load(row["before"]), _load(row["after"])) for row in _op_rows(store, batch_id)]


def _new_batch(store: Store, description: str, mode: str) -> int:
    cur = store.db.execute(
        "INSERT INTO batches (description, status, mode, created_at) VALUES (?, 'staged', ?, ?)",
        (description, mode, _now()),
    )
    return cur.lastrowid


def _record_op(store: Store, batch_id: int, seq: int, op: dict, pid: int, before, after) -> None:
    store.db.execute(
        "INSERT INTO operations (batch_id, seq, op, playlist_id, before, after) VALUES (?, ?, ?, ?, ?, ?)",
        (batch_id, seq, json.dumps(op), pid, _dump(before), _dump(after)),
    )


def _clear_redo(store: Store) -> None:
    for row in store.db.execute("SELECT id FROM batches WHERE status = 'undone'").fetchall():
        _set_status(store, row[0], "discarded")


def _set_status(store: Store, batch_id: int, status: str, stamp: str | None = None) -> None:
    seq = store.db.execute("SELECT COALESCE(MAX(changed_seq), 0) + 1 FROM batches").fetchone()[0]
    stamp_sql = f", {stamp} = ?" if stamp else ""
    params = [status, seq] + ([_now()] if stamp else []) + [batch_id]
    store.db.execute(f"UPDATE batches SET status = ?, changed_seq = ?{stamp_sql} WHERE id = ?", params)


def _latest(store: Store, statuses: list[str]) -> int | None:
    row = store.db.execute(
        f"SELECT id FROM batches WHERE status IN ({','.join('?' * len(statuses))})"
        " ORDER BY changed_seq DESC LIMIT 1",
        statuses,
    ).fetchone()
    return row[0] if row else None


def _later_batch_touching(store: Store, batch_id: int, pid: int) -> int | None:
    row = store.db.execute(
        "SELECT b.id FROM batches b JOIN operations o ON o.batch_id = b.id"
        " WHERE b.status IN ('staged', 'committed') AND o.playlist_id = ? AND b.id != ? AND b.changed_seq >"
        " (SELECT changed_seq FROM batches WHERE id = ?) ORDER BY b.changed_seq DESC LIMIT 1",
        (pid, batch_id, batch_id),
    ).fetchone()
    return row[0] if row else None
