"""Command-line interface."""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

from . import __version__, backup, changes, dupes, edits
from .formats import FORMATS, csv_format, detect_format, json_format, m3u_format, read_playlists
from .model import Playlist, normalize
from .ops import DeletePlaylist
from .store import Store, default_db_path

MODE_HELP = {
    "safe": "stage changes; undo freely; nothing is final until you commit",
    "auto": "apply each change right away; the last few can still be undone",
}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="segue", description=__doc__)
    parser.add_argument("--version", action="version", version=f"segue {__version__}")
    parser.add_argument("--db", type=Path, default=None,
                        help=f"library file (default: $SEGUE_DB or {default_db_path()})")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("setup", help="choose how duplicates are found and how changes are applied")
    p.add_argument("--duplicates", choices=dupes.LEVELS)
    p.add_argument("--keep", choices=dupes.KEEPS)
    p.add_argument("--apply-mode", choices=changes.APPLY_MODES)
    p.set_defaults(func=cmd_setup)

    p = sub.add_parser("import", help="import playlists from JSON, CSV or M3U files")
    p.add_argument("files", nargs="+", type=Path)
    p.add_argument("--format", choices=FORMATS, help="override detection by file extension")
    p.set_defaults(func=cmd_import)

    p = sub.add_parser("export", help="export playlists to a file (or a folder, for M3U)")
    p.add_argument("output", type=Path)
    p.add_argument("--format", choices=FORMATS, help="default: from the output's extension")
    p.add_argument("-p", "--playlist", action="append", default=[],
                   help="only this playlist (ID or name); repeatable")
    p.add_argument("--prefer", action="append", default=[],
                   help="M3U: service whose links to use first, e.g. spotify; repeatable")
    p.set_defaults(func=cmd_export)

    p = sub.add_parser("backup", help="write a full JSON + CSV backup of the library")
    p.add_argument("folder", type=Path, nargs="?", help="default: a 'backups' folder next to the library")
    p.set_defaults(func=cmd_backup)

    p = sub.add_parser("list", help="list playlists")
    p.set_defaults(func=cmd_list)

    p = sub.add_parser("show", help="show a playlist's tracks")
    p.add_argument("playlist", help="ID or name")
    p.set_defaults(func=cmd_show)

    # Changes: each is staged (safe mode) or applied right away (auto mode).
    def change_parser(name, help):
        p = sub.add_parser(name, help=help)
        group = p.add_mutually_exclusive_group()
        group.add_argument("--now", dest="mode", action="store_const", const="auto",
                           help="apply right away, as in auto mode")
        group.add_argument("--stage", dest="mode", action="store_const", const="safe",
                           help="stage for commit, as in safe mode")
        group.add_argument("--dry-run", dest="mode", action="store_const", const="dry",
                           help="only show what would change")
        return p

    def backup_flags(p):
        group = p.add_mutually_exclusive_group()
        group.add_argument("--backup", action="store_true",
                           help="if a deleted playlist isn't backed up yet, back up first without asking")
        group.add_argument("--no-backup", action="store_true",
                           help="delete even if there is no backup of the current version")

    def followed_flag(p):
        p.add_argument("--include-followed", action="store_true",
                       help="also match followed playlists when selecting by name")

    p = change_parser("rename", "rename a playlist")
    p.add_argument("playlist", help="ID or name")
    p.add_argument("name", help="new name")
    p.set_defaults(func=cmd_rename)

    p = change_parser("delete", "delete playlists (unfollow, for followed ones)")
    p.add_argument("playlists", nargs="+", help="IDs or names")
    backup_flags(p)
    followed_flag(p)
    p.set_defaults(func=cmd_delete)

    p = change_parser("remove", "remove tracks from a playlist by position")
    p.add_argument("playlist", help="ID or name")
    p.add_argument("positions", nargs="+", type=int, help="positions as shown by 'segue show'")
    p.set_defaults(func=cmd_remove)

    p = change_parser("dedupe", "remove duplicate tracks from playlists")
    p.add_argument("playlists", nargs="+", help="IDs or names")
    p.add_argument("--duplicates", choices=dupes.LEVELS, help="override the duplicate setting")
    p.add_argument("--keep", choices=dupes.KEEPS, help="override which copy is kept")
    followed_flag(p)
    p.set_defaults(func=cmd_dedupe)

    p = change_parser("merge", "merge playlists without duplicate tracks")
    p.add_argument("playlists", nargs="+", help="IDs or names of the playlists to merge in")
    target = p.add_mutually_exclusive_group(required=True)
    target.add_argument("--into", metavar="PLAYLIST", help="existing playlist to merge into")
    target.add_argument("--new", metavar="NAME", help="create a new playlist with this name")
    p.add_argument("--delete-sources", action="store_true", help="delete the merged-in playlists")
    p.add_argument("--duplicates", choices=dupes.LEVELS, help="override the duplicate setting")
    backup_flags(p)
    followed_flag(p)
    p.set_defaults(func=cmd_merge)

    p = change_parser("restore", "put playlists back as they are in a backup file")
    p.add_argument("file", type=Path)
    p.add_argument("--format", choices=FORMATS, help="override detection by file extension")
    p.add_argument("-p", "--playlist", action="append", default=[],
                   help="only restore this playlist (name); repeatable")
    p.set_defaults(func=cmd_restore)

    # The change log
    p = sub.add_parser("pending", help="show staged changes waiting for commit")
    p.set_defaults(func=cmd_pending)

    p = sub.add_parser("commit", help="make all staged changes final")
    p.set_defaults(func=cmd_commit)

    p = sub.add_parser("discard", help="throw away all staged changes")
    p.set_defaults(func=cmd_discard)

    p = sub.add_parser("history", help="show recent changes")
    p.add_argument("-n", type=int, default=20, help="how many (default 20)")
    p.add_argument("--all", action="store_true", help="include discarded changes")
    p.set_defaults(func=cmd_history)

    p = sub.add_parser("undo", help="undo a change (default: the last one)")
    p.add_argument("batch", nargs="?", type=int)
    p.set_defaults(func=cmd_undo)

    p = sub.add_parser("redo", help="redo an undone change (default: the last one)")
    p.add_argument("batch", nargs="?", type=int)
    p.set_defaults(func=cmd_redo)

    p = sub.add_parser("config", help="show or change settings")
    p.add_argument("key", nargs="?", choices=["apply-mode", "duplicates", "keep", "undo-limit"])
    p.add_argument("value", nargs="?")
    p.set_defaults(func=cmd_config)

    args = parser.parse_args(argv)
    db_path = args.db or default_db_path()
    try:
        with Store(db_path) as store:
            args.db_path = db_path
            return args.func(store, args) or 0
    except (ValueError, KeyError, OSError, changes.ChangeError) as e:
        print(f"segue: {e.args[0] if isinstance(e, KeyError) else e}", file=sys.stderr)
        return 1


# Library

def cmd_setup(store: Store, args) -> int:
    flags = args.duplicates or args.keep or args.apply_mode
    if not flags and not _interactive():
        raise ValueError("setup asks questions in a terminal; or pass --duplicates, --keep and --apply-mode")
    if args.duplicates or not flags:
        dupes.set_level(store, args.duplicates or _choose(
            "What counts as a duplicate?", dupes.LEVEL_HELP, dupes.level(store)))
    if args.keep or not flags:
        dupes.set_keep(store, args.keep or _choose(
            "When removing duplicates, which copy is kept?", dupes.KEEP_HELP, dupes.keep(store)))
    if args.apply_mode or not flags:
        changes.set_apply_mode(store, args.apply_mode or _choose(
            "How should changes be applied?", MODE_HELP, changes.apply_mode(store)))
    return cmd_config(store, argparse.Namespace(key=None, value=None))


def cmd_import(store: Store, args) -> int:
    before = store.track_count()
    for path in args.files:
        for playlist in read_playlists(path, args.format):
            _, status, batch_id = changes.record_import(store, playlist, path.name)
            note = f"  [#{batch_id}]" if batch_id else ""
            print(f"{status} {playlist.name!r} ({len(playlist.entries)} tracks) from {path.name}{note}")
    print(f"library: {store.track_count()} tracks ({store.track_count() - before} new)")
    return 0


def cmd_export(store: Store, args) -> int:
    playlists = _select(store, args.playlist, include_followed=True)
    # A path without an extension is a folder of M3U files.
    fmt = args.format or ("m3u" if not args.output.suffix else detect_format(args.output))

    if fmt == "m3u" and args.output.suffix.lower() in (".m3u", ".m3u8"):
        if len(playlists) != 1:
            raise ValueError("an .m3u file holds one playlist; pick one with -p, or give a folder")
        args.output.parent.mkdir(parents=True, exist_ok=True)
        skipped = m3u_format.write(playlists[0], args.output, args.prefer)
        note = f" ({skipped} tracks without a link left out)" if skipped else ""
        print(f"wrote {args.output}{note}")
    elif fmt == "m3u":
        args.output.mkdir(parents=True, exist_ok=True)
        used: set[str] = set()
        for playlist in playlists:
            path = args.output / f"{_filename(playlist.name, used)}.m3u8"
            skipped = m3u_format.write(playlist, path, args.prefer)
            note = f" ({skipped} tracks without a link left out)" if skipped else ""
            print(f"wrote {path}{note}")
    else:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        (json_format if fmt == "json" else csv_format).write(playlists, args.output, store.library_id)
        print(f"wrote {len(playlists)} playlists to {args.output}")
    return 0


def cmd_backup(store: Store, args) -> int:
    paths = backup.write_backup(store, args.folder or backup.default_folder(args.db_path))
    for path in paths:
        print(f"wrote {path}")
    summaries = store.playlist_summaries()
    print(f"{len(summaries)} playlists, {sum(s.track_count for s in summaries)} entries. "
          "Restore with: segue restore <file>")
    return 0


def cmd_list(store: Store, args) -> int:
    summaries = store.playlist_summaries()
    if not summaries:
        print("No playlists yet. Add some with: segue import <file>")
        return 0
    width = max(len(s.name) for s in summaries)
    kinds = {s.id: "" if s.kind == "created" else s.kind for s in summaries}
    kind_width = max(len(k) for k in kinds.values())
    for s in summaries:
        source = f"{s.source.service}:{s.source.id}" if s.source else "-"
        kind = f"{kinds[s.id]:<{kind_width}}  " if kind_width else ""
        print(f"{s.id:>5}  {s.name:<{width}}  {s.track_count:>5} tracks  {kind}{source}")
    return 0


def cmd_show(store: Store, args) -> int:
    for playlist in _select(store, [args.playlist], include_followed=True):
        kind = "" if playlist.kind == "created" else f", {playlist.kind}"
        print(f"{playlist.name} ({len(playlist.entries)} tracks{kind})")
        for position, track in enumerate(playlist.tracks, start=1):
            artists = ", ".join(track.artists) or "?"
            length = ""
            if track.duration_ms is not None:
                minutes, seconds = divmod(round(track.duration_ms / 1000), 60)
                length = f"  {minutes}:{seconds:02d}"
            services = ",".join(dict.fromkeys(r.service for r in track.refs))
            print(f"{position:>4}. {artists} - {track.title}{length}" + (f"  [{services}]" if services else ""))
    return 0


# Changes

def cmd_rename(store: Store, args) -> int:
    return _submit(store, args, edits.plan_rename(store, _one(store, args.playlist), args.name))


def cmd_delete(store: Store, args) -> int:
    ids = _ids(store, args.playlists, args.include_followed)
    return _submit(store, args, edits.plan_delete(store, ids))


def cmd_remove(store: Store, args) -> int:
    return _submit(store, args, edits.plan_remove(store, _one(store, args.playlist), args.positions))


def cmd_dedupe(store: Store, args) -> int:
    if not args.duplicates:
        _ensure_duplicate_setting(store)
    ids = _ids(store, args.playlists, args.include_followed)
    return _submit(store, args, edits.plan_dedupe(store, ids, args.duplicates, args.keep))


def cmd_merge(store: Store, args) -> int:
    if not args.duplicates:
        _ensure_duplicate_setting(store)
    into = _one(store, args.into) if args.into else None
    plan = edits.plan_merge(store, _ids(store, args.playlists, args.include_followed), into_id=into,
                            into_name=args.new, delete_sources=args.delete_sources,
                            level=args.duplicates)
    return _submit(store, args, plan)


def cmd_restore(store: Store, args) -> int:
    playlists = read_playlists(args.file, args.format)
    if args.playlist:
        wanted = {normalize(name) for name in args.playlist}
        playlists = [p for p in playlists if normalize(p.name) in wanted]
        if not playlists:
            raise ValueError(f"none of {', '.join(map(repr, args.playlist))} are in {args.file.name}")
    description, ops, notes = backup.plan_restore(store, playlists)
    for note in notes:
        print(note)
    return _submit(store, args, (description, ops))


def _submit(store: Store, args, plan: edits.Plan) -> int:
    description, ops = plan
    if not ops:
        print(f"nothing to do ({description})")
        return 0
    print(f"{description}:")
    for line in changes.preview(store, ops):
        print(f"  - {line}")
    if args.mode == "dry":
        print("dry run: nothing changed")
        return 0
    _backup_gate(store, args, ops)
    batch_id, status = changes.submit(store, description, ops, args.mode)
    if status == "staged":
        print(f"staged as #{batch_id}. Undo with: segue undo. Make it final with: segue commit")
    else:
        print(f"applied as #{batch_id}. Undo with: segue undo")
    return 0


def _backup_gate(store: Store, args, ops) -> None:
    """Before deleting playlists, make sure their current version is in a backup."""
    deleting = [op.playlist_id for op in ops if isinstance(op, DeletePlaylist)]
    missing = backup.not_backed_up(store, deleting)
    if not missing:
        return
    names = ", ".join(repr(store.playlist_state(pid).name) for pid in missing)
    last = backup.last_backup_time(store)
    since = f"The last backup is from {last[:10]}." if last else "You haven't made a backup yet."
    message = f"{since} It doesn't include the current version of {names}."
    if getattr(args, "no_backup", False):
        print(f"warning: {message} Continuing without a backup (--no-backup).")
        return
    if getattr(args, "backup", False) or (_interactive() and _confirm(f"{message}\nMake a backup now? [Y/n] ")):
        for path in backup.write_backup(store, backup.default_folder(args.db_path)):
            print(f"backed up to {path}")
        return
    if _interactive():
        raise ValueError("cancelled; nothing changed")
    raise ValueError(f"{message} Run again with --backup to make one first, or --no-backup to go ahead without.")


def _ensure_duplicate_setting(store: Store) -> None:
    if dupes.is_configured(store):
        return
    if _interactive():
        print("First, choose what counts as a duplicate. You can change this later with: segue setup")
        dupes.set_level(store, _choose("What counts as a duplicate?", dupes.LEVEL_HELP, dupes.DEFAULT_LEVEL))
        dupes.set_keep(store, _choose("When removing duplicates, which copy is kept?", dupes.KEEP_HELP,
                                      dupes.DEFAULT_KEEP))
    else:
        print(f"using the default duplicate setting {dupes.DEFAULT_LEVEL!r}; choose one with: segue setup")


# The change log

def cmd_pending(store: Store, args) -> int:
    staged = changes.batches(store, ["staged"])
    if not staged:
        print("nothing staged")
        return 0
    print("staged changes (newest last):")
    for batch in staged:
        print(f"  #{batch.id}  {batch.description}")
        for line in changes.describe_batch(store, batch.id):
            print(f"        - {line}")
    print("commit will write:")
    for line in changes.describe_staged(store) or ["nothing (the staged changes cancel out)"]:
        print(f"  - {line}")
    print("undo with: segue undo   make final with: segue commit   throw away with: segue discard")
    return 0


def cmd_commit(store: Store, args) -> int:
    ids = changes.commit(store)
    if not ids:
        print("nothing staged")
        return 0
    print(f"committed {len(ids)} change{'s' if len(ids) != 1 else ''} "
          f"({', '.join(f'#{i}' for i in ids)}). They are final now and can't be undone.")
    print("(no services are connected yet, so nothing was written to one)")
    return 0


def cmd_discard(store: Store, args) -> int:
    ids = changes.discard(store)
    print(f"discarded {', '.join(f'#{i}' for i in ids)}" if ids else "nothing staged")
    return 0


def cmd_history(store: Store, args) -> int:
    statuses = None if args.all else ["staged", "committed", "undone"]
    shown = changes.batches(store, statuses, limit=args.n)
    if not shown:
        print("no changes yet")
    for batch in shown:
        when = (batch.undone_at if batch.status in ("undone", "discarded") else batch.applied_at) or ""
        mark = "  (can undo)" if changes.can_undo(store, batch) else ""
        print(f"#{batch.id:<4} {batch.status:<9} {when[:16].replace('T', ' ')}  {batch.description}{mark}")
    return 0


def cmd_undo(store: Store, args) -> int:
    batch_id = changes.undo(store, args.batch)
    print(f"undid #{batch_id}  {changes.get_batch(store, batch_id).description}  (redo with: segue redo)")
    return 0


def cmd_redo(store: Store, args) -> int:
    batch_id = changes.redo(store, args.batch)
    print(f"redid #{batch_id}  {changes.get_batch(store, batch_id).description}")
    return 0


def cmd_config(store: Store, args) -> int:
    getters = {
        "apply-mode": (changes.apply_mode, changes.set_apply_mode),
        "duplicates": (dupes.level, dupes.set_level),
        "keep": (dupes.keep, dupes.set_keep),
        "undo-limit": (changes.undo_limit, changes.set_undo_limit),
    }
    if args.key is None:
        for key, (get, _) in getters.items():
            print(f"{key} = {get(store)}")
        return 0
    get, put = getters[args.key]
    if args.value is not None:
        put(store, args.value)
    print(f"{args.key} = {get(store)}")
    return 0


# Helpers

def _interactive() -> bool:
    return sys.stdin.isatty() and sys.stdout.isatty()


def _confirm(question: str) -> bool:
    return input(question).strip().lower() in ("", "y", "yes")


def _choose(question: str, options: dict[str, str], default: str) -> str:
    keys = list(options)
    print(question)
    for number, key in enumerate(keys, start=1):
        marker = " (current)" if key == default else ""
        print(f"  {number}. {key}: {options[key]}{marker}")
    while True:
        answer = input(f"Choose 1-{len(keys)} [{keys.index(default) + 1}]: ").strip()
        if not answer:
            return default
        if answer.isdigit() and 1 <= int(answer) <= len(keys):
            return keys[int(answer) - 1]
        if answer in options:
            return answer


def _ids(store: Store, selectors: list[str], include_followed: bool = False) -> list[int]:
    return [p.id for p in _select(store, selectors, include_followed)]


def _one(store: Store, selector: str) -> int:
    ids = _ids(store, [selector], include_followed=True)
    if len(ids) > 1:
        raise ValueError(f"{selector!r} matches several playlists ({', '.join(map(str, ids))}); use an ID")
    return ids[0]


def _select(store: Store, selectors: list[str], include_followed: bool = False) -> list[Playlist]:
    """Playlists by ID or name. Names skip followed playlists unless ``include_followed``."""
    if not selectors:
        return store.all_playlists()
    playlists: dict[int, Playlist] = {}
    for selector in selectors:
        if selector.isdigit():
            found = [store.get_playlist(int(selector))]
        else:
            found = store.find_playlists(selector)
            skipped = [p for p in found if p.kind == "followed" and not include_followed]
            for p in skipped:
                print(f"skipping followed playlist {p.name!r} (#{p.id}); use --include-followed to include it")
            skipped_ids = {p.id for p in skipped}
            found = [p for p in found if p.id not in skipped_ids]
            if not found and skipped:
                raise ValueError(f"{selector!r} only matches followed playlists")
        if not found:
            raise ValueError(f"no playlist matches {selector!r}")
        for p in found:
            playlists[p.id] = p
    return list(playlists.values())


def _filename(name: str, used: set[str]) -> str:
    base = re.sub(r'[\\/:*?"<>|\x00-\x1f]', "_", name).strip(" .") or "playlist"
    candidate, n = base, 2
    while candidate.lower() in used:
        candidate, n = f"{base} ({n})", n + 1
    used.add(candidate.lower())
    return candidate


if __name__ == "__main__":
    sys.exit(main())
