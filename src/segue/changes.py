"""The change log: plan, commit, undo and redo batches of operations.

Every applied operation stores its playlist's state before and after. Undo writes the
"before" state back, but only if the playlist still looks exactly like "after"; redo does
the reverse. That check is what keeps an undo from silently overwriting later changes.

In **commit** mode (the default) planned batches wait in a queue for review. Each batch
remembers the revision of every playlist it touches when it was planned; committing a
batch whose playlists have changed since is refused as stale.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone

from .model import Playlist, PlaylistState
from .ops import Operation
from .store import Store

APPLY_MODES = ("commit", "auto")
DEFAULT_APPLY_MODE = "commit"


class ChangeError(Exception):
    """A change can't be applied, undone or redone as asked."""


@dataclass
class Batch:
    id: int
    description: str
    status: str
    created_at: str
    applied_at: str | None
    undone_at: str | None


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _dump(state: PlaylistState | None) -> str | None:
    return None if state is None else json.dumps(state.to_dict())


def _load(text: str | None) -> PlaylistState | None:
    return None if text is None else PlaylistState.from_dict(json.loads(text))


def apply_mode(store: Store) -> str:
    return store.get_setting("apply_mode", DEFAULT_APPLY_MODE)


def set_apply_mode(store: Store, mode: str) -> None:
    if mode not in APPLY_MODES:
        raise ValueError(f"apply mode must be one of: {', '.join(APPLY_MODES)}")
    store.set_setting("apply_mode", mode)


# Planning and previews

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
        if target is not None:
            try:
                states[target] = op.apply(before)
            except ValueError as e:
                lines[-1] += f"  [will fail: {e}]"
    return lines


def plan(store: Store, description: str, ops: list[Operation]) -> int:
    """Queue ops as a pending batch and return its ID."""
    if not ops:
        raise ValueError("nothing to do")
    with store.transaction():
        expected = {
            str(op.target): store.revision(op.target) for op in ops if op.target is not None
        }
        cur = store.db.execute(
            "INSERT INTO batches (description, status, expected, created_at) VALUES (?, 'pending', ?, ?)",
            (description, json.dumps(expected), _now()),
        )
        batch_id = cur.lastrowid
        store.db.executemany(
            "INSERT INTO operations (batch_id, seq, op) VALUES (?, ?, ?)",
            [(batch_id, seq, json.dumps(op.to_dict())) for seq, op in enumerate(ops)],
        )
    return batch_id


def submit(store: Store, description: str, ops: list[Operation], mode: str | None = None) -> tuple[int, bool]:
    """Plan a batch and, in auto mode, apply it right away. Returns ``(batch_id, applied)``."""
    mode = mode or apply_mode(store)
    with store.transaction():
        batch_id = plan(store, description, ops)
        if mode == "auto":
            commit(store, batch_id)
    return batch_id, mode == "auto"


# Applying, undoing, redoing

def commit(store: Store, batch_id: int) -> None:
    batch = get_batch(store, batch_id)
    if batch.status != "pending":
        raise ChangeError(f"batch #{batch_id} is {batch.status}, not pending")
    expected = json.loads(store.db.execute(
        "SELECT expected FROM batches WHERE id = ?", (batch_id,)).fetchone()[0])
    with store.transaction():
        for pid, revision in expected.items():
            now = store.revision(int(pid))
            if now != revision:
                what = "was deleted" if now is None else "has changed"
                raise ChangeError(
                    f"batch #{batch_id} is stale: playlist {pid} {what} since it was planned. "
                    "Discard it and plan it again."
                )
        for seq, op_json in _ops(store, batch_id):
            op = Operation.from_dict(json.loads(op_json))
            pid = op.target
            before = None if pid is None else store.playlist_state(pid)
            try:
                after = op.apply(before)
            except ValueError as e:
                raise ChangeError(f"batch #{batch_id}: {e}") from e
            if pid is None:
                pid = store.create_state(after)
            else:
                store.write_state(pid, after)
            store.db.execute(
                "UPDATE operations SET playlist_id = ?, before = ?, after = ? WHERE batch_id = ? AND seq = ?",
                (pid, _dump(before), _dump(after), batch_id, seq),
            )
        _set_status(store, batch_id, "applied", "applied_at")


def discard(store: Store, batch_id: int) -> None:
    batch = get_batch(store, batch_id)
    if batch.status != "pending":
        raise ChangeError(f"batch #{batch_id} is {batch.status}; only pending batches can be discarded")
    _set_status(store, batch_id, "discarded")


def undo(store: Store, batch_id: int | None = None) -> int:
    """Undo a batch (default: the most recently applied or redone one)."""
    batch_id = batch_id or _latest(store, "applied", "nothing to undo")
    batch = get_batch(store, batch_id)
    if batch.status != "applied":
        raise ChangeError(f"batch #{batch_id} is {batch.status}; only applied batches can be undone")
    with store.transaction():
        for pid, before, after in reversed(_snapshots(store, batch_id)):
            _swap(store, batch_id, pid, expect=after, write=before, verb="undo")
        _set_status(store, batch_id, "undone", "undone_at")
    return batch_id


def redo(store: Store, batch_id: int | None = None) -> int:
    """Re-apply an undone batch (default: the most recently undone one)."""
    batch_id = batch_id or _latest(store, "undone", "nothing to redo")
    batch = get_batch(store, batch_id)
    if batch.status != "undone":
        raise ChangeError(f"batch #{batch_id} is {batch.status}; only undone batches can be redone")
    with store.transaction():
        for pid, before, after in _snapshots(store, batch_id):
            _swap(store, batch_id, pid, expect=before, write=after, verb="redo")
        _set_status(store, batch_id, "applied", "applied_at")
    return batch_id


def _swap(store: Store, batch_id: int, pid: int, expect, write, verb: str) -> None:
    current = store.playlist_state(pid)
    if current != expect:
        name = (current or expect or write).name
        later = _later_batch_touching(store, batch_id, pid)
        if later and verb == "undo":
            reason = f"{name!r} was changed later by batch #{later}; undo that first"
        else:
            reason = f"{name!r} has changed since"
        raise ChangeError(f"can't {verb} batch #{batch_id}: {reason}")
    store.write_state(pid, write)


def record_import(store: Store, playlist: Playlist, source_name: str) -> tuple[int, str, int | None]:
    """Save an imported playlist through the log, so a replace can be undone.

    Imports only touch the local library, so they apply right away in either mode.
    Returns ``(playlist_id, status, batch_id)``; nothing is logged for "unchanged".
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
        cur = store.db.execute(
            "INSERT INTO batches (description, status, created_at) VALUES (?, 'pending', ?)",
            (f"import {playlist.name!r} from {source_name}", _now()),
        )
        batch_id = cur.lastrowid
        store.db.execute(
            "INSERT INTO operations (batch_id, seq, op, playlist_id, before, after) VALUES (?, 0, ?, ?, ?, ?)",
            (batch_id, json.dumps({"kind": "import", "name": playlist.name, "file": source_name}),
             pid, _dump(before), _dump(after)),
        )
        _set_status(store, batch_id, "applied", "applied_at")
    return pid, status, batch_id


# Reading the log

def get_batch(store: Store, batch_id: int) -> Batch:
    row = store.db.execute(
        "SELECT id, description, status, created_at, applied_at, undone_at FROM batches WHERE id = ?",
        (batch_id,),
    ).fetchone()
    if row is None:
        raise ChangeError(f"no batch #{batch_id}")
    return Batch(*row)


def batches(store: Store, status: str | None = None, limit: int | None = None) -> list[Batch]:
    query = "SELECT id, description, status, created_at, applied_at, undone_at FROM batches"
    params: list = []
    if status:
        query += " WHERE status = ?"
        params.append(status)
    query += " ORDER BY id DESC" if status != "pending" else " ORDER BY id"
    if limit:
        query += " LIMIT ?"
        params.append(limit)
    return [Batch(*row) for row in store.db.execute(query, params)]


def batch_ops(store: Store, batch_id: int) -> list[Operation]:
    return [
        Operation.from_dict(data) for _, text in _ops(store, batch_id)
        if (data := json.loads(text))["kind"] != "import"
    ]


def _ops(store: Store, batch_id: int) -> list[tuple[int, str]]:
    return store.db.execute(
        "SELECT seq, op FROM operations WHERE batch_id = ? ORDER BY seq", (batch_id,)
    ).fetchall()


def _snapshots(store: Store, batch_id: int):
    return [
        (row["playlist_id"], _load(row["before"]), _load(row["after"]))
        for row in store.db.execute(
            "SELECT playlist_id, before, after FROM operations WHERE batch_id = ? ORDER BY seq",
            (batch_id,),
        )
    ]


def _set_status(store: Store, batch_id: int, status: str, stamp: str | None = None) -> None:
    seq = store.db.execute("SELECT COALESCE(MAX(changed_seq), 0) + 1 FROM batches").fetchone()[0]
    stamp_sql = f", {stamp} = ?" if stamp else ""
    params = [status, seq] + ([_now()] if stamp else []) + [batch_id]
    store.db.execute(f"UPDATE batches SET status = ?, changed_seq = ?{stamp_sql} WHERE id = ?", params)


def _latest(store: Store, status: str, empty: str) -> int:
    row = store.db.execute(
        "SELECT id FROM batches WHERE status = ? ORDER BY changed_seq DESC LIMIT 1", (status,)
    ).fetchone()
    if row is None:
        raise ChangeError(empty)
    return row[0]


def _later_batch_touching(store: Store, batch_id: int, pid: int) -> int | None:
    row = store.db.execute(
        "SELECT b.id FROM batches b JOIN operations o ON o.batch_id = b.id"
        " WHERE b.status = 'applied' AND o.playlist_id = ? AND b.changed_seq >"
        " (SELECT changed_seq FROM batches WHERE id = ?) ORDER BY b.changed_seq DESC LIMIT 1",
        (pid, batch_id),
    ).fetchone()
    return row[0] if row else None
