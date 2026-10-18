"""User actions as plain data, for the web interface (and anything else that isn't the CLI).

An action is a name and parameters, for example
``{"action": "merge", "params": {"ids": [3, 4], "into_id": 1, "delete_sources": true}}``.
``preview`` says what it would do without changing anything; ``apply`` does it. Both return
dictionaries that can be sent as JSON.

When a backup is needed first (before deleting playlists, or before writing to a service
after a pull), ``apply`` returns ``{"needs_backup": [...reasons]}`` instead of changing
anything. Call it again with ``backup="make"`` (back up, then go ahead) or ``backup="skip"``.
"""

from __future__ import annotations

from pathlib import Path

from . import backup, changes, edits, remote, select
from .formats import read_playlists
from .ops import DeletePlaylist, Operation
from .store import Store

# Playlist selection: {"ids": [...]} and/or {"filter": {...select.Filter fields}}


def _ids(store: Store, params: dict, default_all: bool = False) -> list[int]:
    ids = [int(i) for i in params.get("ids") or []]
    f = select.Filter(**{k: (set(v) if k == "kinds" else v) for k, v in (params.get("filter") or {}).items()})
    if ids:
        return select.apply_filter(store, ids, f) if f.is_set() else ids
    if f.is_set() or default_all:
        return select.apply_filter(store, [s.id for s in store.playlist_summaries()], f)
    raise ValueError("choose one or more playlists")


def _one(params: dict, key: str = "id") -> int:
    if params.get(key) is None:
        raise ValueError("choose a playlist")
    return int(params[key])


def _plan(store: Store, action: str, p: dict) -> tuple[str, list[Operation], dict]:
    extra: dict = {}
    if action == "delete":
        description, ops = edits.plan_delete(store, _ids(store, p))
    elif action == "dedupe":
        description, ops = edits.plan_dedupe(store, _ids(store, p), p.get("level"), p.get("keep"))
    elif action == "merge":
        ids = _ids(store, p)
        description, ops = edits.plan_merge(store, ids, into_id=p.get("into_id"), into_name=p.get("new_name"),
                                            delete_sources=bool(p.get("delete_sources")), level=p.get("level"))
    elif action == "merge_names":
        description, ops, groups = edits.plan_merge_by_name(
            store, _ids(store, p, default_all=True), p.get("survivor", "largest"),
            delete_sources=p.get("delete_sources", True), level=p.get("level"))
        extra["groups"] = [{"kept": kept, "merged": others} for kept, others in groups]
    elif action == "rename":
        description, ops = edits.plan_rename(store, _one(p), str(p.get("name") or "").strip()
                                             or _fail("give the playlist a name"))
    elif action == "rename_all":
        rules = edits.RenameRules(
            replace=[(r[0], r[1], bool(r[2]) if len(r) > 2 else False) for r in p.get("replace") or []],
            trim=bool(p.get("trim")), case=p.get("case") or None, strip_copies=bool(p.get("strip_copies")),
            prefix=p.get("prefix") or "", suffix=p.get("suffix") or "",
        )
        description, ops = edits.plan_bulk_rename(store, _ids(store, p), rules)
    elif action == "remove":
        description, ops = edits.plan_remove(store, _one(p), [int(x) for x in p.get("positions") or []])
    elif action == "sort":
        description, ops = edits.plan_sort(store, _one(p), list(p.get("keys") or ["title"]),
                                           bool(p.get("reverse")), p.get("as_new") or None)
    elif action == "shuffle":
        description, ops = edits.plan_shuffle(store, _one(p), p.get("seed"), p.get("as_new") or None)
    elif action == "split":
        description, ops = edits.plan_split(store, _one(p), every=p.get("every"), by=p.get("by"),
                                            min_size=int(p.get("min") or 1),
                                            delete_original=bool(p.get("delete_original")))
    elif action == "clone":
        description, ops = edits.plan_clone(store, _one(p), p.get("name") or None)
    elif action == "restore":
        playlists = read_playlists(Path(p["path"]))
        if p.get("names"):
            wanted = set(p["names"])
            playlists = [pl for pl in playlists if pl.name in wanted]
        description, ops, notes = backup.plan_restore(store, playlists)
        extra["notes"] = notes
    else:
        raise ValueError(f"unknown action {action!r}")
    return description, ops, extra


def _fail(message: str):
    raise ValueError(message)


def preview(store: Store, action: str, params: dict) -> dict:
    description, ops, extra = _plan(store, action, params)
    return {"description": description, "lines": changes.preview(store, ops), "nothing": not ops,
            "deletes": sum(isinstance(op, DeletePlaylist) for op in ops), **extra}


# Backups

def delete_reasons(store: Store, ops: list[Operation]) -> list[str]:
    missing = backup.not_backed_up(store, [op.playlist_id for op in ops if isinstance(op, DeletePlaylist)])
    if not missing:
        return []
    names = ", ".join(repr(store.playlist_state(pid).name) for pid in missing[:5])
    if len(missing) > 5:
        names += f" and {len(missing) - 5} more"
    last = backup.last_backup_time(store)
    since = f"The last backup is from {last[:10]}." if last else "You haven't made a backup yet."
    return [f"{since} It doesn't include the current version of {names}."]


def service_reasons(store: Store, services: set[str]) -> list[str]:
    last = backup.last_backup_time(store)
    stale = [s for s in sorted(services) if (pulled := remote.last_pull(store, s)) and (not last or last < pulled)]
    if not stale:
        return []
    return [f"There's no backup since you last read your library from {', '.join(s.capitalize() for s in stale)}, "
            "and this is about to change playlists there."]


def _linked_services(store: Store, services: dict) -> set[str]:
    return {svc for _, svc, _, _ in store.remote_links() if svc in services}


def _resolve_backup(store: Store, reasons: list[str], choice: str | None, folder: Path) -> dict | None:
    """None to go ahead; or the answer to send back when the user still has to choose."""
    if not reasons or choice == "skip":
        return None
    if choice == "make":
        backup.write_backup(store, folder)
        return None
    return {"needs_backup": reasons}


# Doing things

def apply(store: Store, action: str, params: dict, services: dict, backup_folder: Path,
          backup_choice: str | None = None, mode: str | None = None) -> dict:
    description, ops, _ = _plan(store, action, params)
    if not ops:
        return {"done": False, "message": f"Nothing to do ({description})."}
    mode = mode or changes.apply_mode(store)
    reasons = delete_reasons(store, ops)
    if mode == "auto":
        reasons += service_reasons(store, _linked_services(store, services))
    if (stop := _resolve_backup(store, reasons, backup_choice, backup_folder)) is not None:
        return stop
    batch_id, status = changes.submit(store, description, ops, mode)
    result = {"done": True, "batch": batch_id, "status": status, "description": description}
    if status == "committed":
        result["push"] = _push(store, services)
    return result


def commit(store: Store, services: dict, backup_folder: Path, backup_choice: str | None = None,
           local: bool = False) -> dict:
    pending = [] if local else remote.pending_pushes(store, services)
    writing = {t.service for t in pending if t.service in services}
    if (stop := _resolve_backup(store, service_reasons(store, writing), backup_choice, backup_folder)) is not None:
        return stop
    result: dict = {"done": True}
    if pending:
        result["push"] = _push(store, services)
        if not result["push"]["ok"]:
            result["done"] = False
            return result
    result["committed"] = changes.commit(store)
    return result


def undo(store: Store, services: dict, backup_folder: Path, backup_choice: str | None = None,
         redo: bool = False) -> dict:
    target = changes.next_redo(store) if redo else changes.next_undo(store)
    if target is None:
        return {"done": False, "message": "Nothing to redo." if redo else "Nothing to undo."}
    batch = changes.get_batch(store, target)
    writes = batch.mode != "safe"  # an applied auto-mode change is already on the service
    if writes:
        stop = _resolve_backup(store, service_reasons(store, _linked_services(store, services)),
                               backup_choice, backup_folder)
        if stop is not None:
            return stop
    batch_id = changes.redo(store) if redo else changes.undo(store)
    result = {"done": True, "batch": batch_id, "description": changes.get_batch(store, batch_id).description}
    if writes:
        result["push"] = _push(store, services)
    return result


def _push(store: Store, services: dict) -> dict:
    report = remote.push(store, services)
    return {
        "ok": report.ok,
        "written": [{"playlist": name, "service": where, "notes": notes} for name, where, notes in report.written],
        "conflicts": report.conflicts, "errors": report.errors,
        "skipped": report.skipped, "unmatched": report.unmatched,
    }
