"""Backups: writing them, knowing what they cover, and restoring from them.

Every backup records the revision of each playlist it contains. A playlist counts as
backed up only if a backup holds its current revision, so any change after the last
backup (including a staged one) means a fresh backup is needed before deleting it.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from .formats import csv_format, json_format
from .model import Playlist
from .ops import Operation, RestorePlaylist
from .store import Store


def default_folder(db_path: Path) -> Path:
    return Path(db_path).parent / "backups"


def write_backup(store: Store, folder: Path) -> list[Path]:
    """Write a full JSON + CSV backup of the library and record what it covers."""
    folder.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y-%m-%d_%H%M%S")
    playlists = store.all_playlists()
    paths = []
    for fmt, module in (("json", json_format), ("csv", csv_format)):
        path = folder / f"segue-backup-{stamp}.{fmt}"
        module.write(playlists, path, library_id=store.library_id)
        paths.append(path)
    with store.transaction():
        cur = store.db.execute(
            "INSERT INTO backups (created_at, paths) VALUES (?, ?)",
            (datetime.now(timezone.utc).isoformat(timespec="seconds"), json.dumps([str(p) for p in paths])),
        )
        store.db.executemany(
            "INSERT INTO backup_playlists (backup_id, playlist_id, revision) VALUES (?, ?, ?)",
            [(cur.lastrowid, p.id, store.revision(p.id)) for p in playlists],
        )
    return paths


def last_backup_time(store: Store) -> str | None:
    row = store.db.execute("SELECT MAX(created_at) FROM backups").fetchone()
    return row[0]


def not_backed_up(store: Store, playlist_ids: list[int]) -> list[int]:
    """The playlists whose current version isn't in any backup."""
    missing = []
    for pid in playlist_ids:
        revision = store.revision(pid)
        if revision is None:
            continue
        row = store.db.execute(
            "SELECT 1 FROM backup_playlists WHERE playlist_id = ? AND revision = ? LIMIT 1",
            (pid, revision),
        ).fetchone()
        if row is None:
            missing.append(pid)
    return missing


def plan_restore(store: Store, playlists: list[Playlist]) -> tuple[str, list[Operation], list[str]]:
    """Plan putting playlists from a backup back as they were.

    Each backed-up playlist is matched to the library by, in order: its Segue ID (only if
    the backup came from this library), its service ID, or a unique exact name. A match is
    replaced (or recreated, if it was deleted); anything unmatched is added as new.
    Returns ``(description, ops, notes)``; notes list playlists that are already identical.
    """
    ops: list[Operation] = []
    notes: list[str] = []
    claimed: set[int] = set()
    for playlist in playlists:
        target = _match(store, playlist, claimed)
        state = store.to_state(playlist)
        if target is not None:
            claimed.add(target)
            if store.playlist_state(target) == state:
                notes.append(f"{playlist.name!r} is already as in the backup")
                continue
        ops.append(RestorePlaylist(target, state))
    names = [p.name for p in playlists]
    shown = ", ".join(repr(n) for n in names[:3]) + (f" and {len(names) - 3} more" if len(names) > 3 else "")
    return f"restore {shown} from backup", ops, notes


def _match(store: Store, playlist: Playlist, claimed: set[int]) -> int | None:
    own_id = None
    if playlist.segue_id and "/" in playlist.segue_id:
        library, _, pid = playlist.segue_id.partition("/")
        if library == store.library_id and pid.isdigit():
            own_id = int(pid)
    if own_id is not None and store.revision(own_id) is not None:
        return own_id
    if playlist.source:
        found = store.find_by_source(playlist.source)
        if found is not None:
            return found
    if own_id is not None:
        return own_id  # deleted since the backup: recreate it under its old ID
    same_name = [s.id for s in store.playlist_summaries() if s.name == playlist.name and s.id not in claimed]
    return same_name[0] if len(same_name) == 1 else None
