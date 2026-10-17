"""Command-line interface."""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

from . import __version__, analysis, backup, changes, dupes, edits, select
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

    def filter_flags(p):
        g = p.add_argument_group("filters (combine freely)")
        g.add_argument("--name", metavar="PATTERN", help="names matching a pattern, e.g. 'gym*' (case-insensitive)")
        g.add_argument("--regex", action="store_true", help="treat --name as a regular expression")
        g.add_argument("--empty", action="store_true", help="playlists without tracks")
        g.add_argument("--min-tracks", type=int, metavar="N")
        g.add_argument("--max-tracks", type=int, metavar="N")
        g.add_argument("--kind", action="append", default=[], choices=["created", "collaborative", "followed", "liked"],
                       help="only this kind (default: created and collaborative); repeatable")
        g.add_argument("--service", help="only playlists from this service ('local' for imported files)")
        g.add_argument("--identical", action="store_true",
                       help="newer copies of a playlist with exactly the same tracks (the oldest is left out)")
        g.add_argument("--subset", action="store_true", help="playlists whose tracks are all in a bigger one")
        g.add_argument("--include-followed", action="store_true",
                       help="also include followed playlists (when matching by name or filter)")

    p = sub.add_parser("list", help="list playlists (all, or those matching filters)")
    filter_flags(p)
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

    p = change_parser("rename", "rename a playlist")
    p.add_argument("playlist", help="ID or name")
    p.add_argument("name", help="new name")
    p.set_defaults(func=cmd_rename)

    p = change_parser("delete", "delete playlists (unfollow, for followed ones)")
    p.add_argument("playlists", nargs="*", help="IDs or names (or choose with filters)")
    backup_flags(p)
    filter_flags(p)
    p.set_defaults(func=cmd_delete)

    p = change_parser("remove", "remove tracks from a playlist by position")
    p.add_argument("playlist", help="ID or name")
    p.add_argument("positions", nargs="+", type=int, help="positions as shown by 'segue show'")
    p.set_defaults(func=cmd_remove)

    p = change_parser("dedupe", "remove duplicate tracks from playlists")
    p.add_argument("playlists", nargs="*", help="IDs or names (or choose with filters)")
    p.add_argument("--duplicates", choices=dupes.LEVELS, help="override the duplicate setting")
    p.add_argument("--keep", choices=dupes.KEEPS, help="override which copy is kept")
    filter_flags(p)
    p.set_defaults(func=cmd_dedupe)

    p = change_parser("merge", "merge playlists without duplicate tracks")
    p.add_argument("playlists", nargs="+", help="IDs or names of the playlists to merge in")
    target = p.add_mutually_exclusive_group(required=True)
    target.add_argument("--into", metavar="PLAYLIST", help="existing playlist to merge into")
    target.add_argument("--new", metavar="NAME", help="create a new playlist with this name")
    p.add_argument("--delete-sources", action="store_true", help="delete the merged-in playlists")
    p.add_argument("--duplicates", choices=dupes.LEVELS, help="override the duplicate setting")
    p.add_argument("--include-followed", action="store_true",
                   help="also match followed playlists when selecting by name")
    backup_flags(p)
    p.set_defaults(func=cmd_merge)

    p = change_parser("merge-names", "merge every group of playlists that share a name (per service)")
    p.add_argument("playlists", nargs="*", help="limit to these IDs or names (default: all)")
    p.add_argument("--survivor", choices=edits.SURVIVORS, default="oldest",
                   help="which playlist of each group is kept (default: oldest)")
    p.add_argument("--keep-sources", action="store_true", help="don't delete the merged-in playlists")
    p.add_argument("--duplicates", choices=dupes.LEVELS, help="override the duplicate setting")
    backup_flags(p)
    filter_flags(p)
    p.set_defaults(func=cmd_merge_names)

    p = change_parser("rename-all", "rename many playlists by rules")
    p.add_argument("playlists", nargs="*", help="IDs or names (or choose with filters)")
    rules = p.add_argument_group("rules (applied in this order)")
    rules.add_argument("--replace", nargs=2, action="append", default=[], metavar=("OLD", "NEW"),
                       help="replace text; repeatable")
    rules.add_argument("--replace-regex", nargs=2, action="append", default=[], metavar=("PATTERN", "NEW"),
                       help="replace a regular expression; repeatable")
    rules.add_argument("--strip-copies", action="store_true", help="remove trailing '(1)', '(copy)', '- copy'")
    rules.add_argument("--trim", action="store_true", help="remove extra spaces")
    rules.add_argument("--case", choices=edits.CASES)
    rules.add_argument("--prefix", default="")
    rules.add_argument("--suffix", default="")
    filter_flags(p)
    p.set_defaults(func=cmd_rename_all)

    p = change_parser("sort", "sort a playlist's tracks")
    p.add_argument("playlist", help="ID or name")
    p.add_argument("--by", required=True, metavar="KEY[,KEY]",
                   help=f"one or more of: {', '.join(edits.SORT_KEYS)} (e.g. artist,album)")
    p.add_argument("--reverse", action="store_true")
    p.add_argument("--as-new", metavar="NAME", help="save the result as a new playlist instead")
    p.set_defaults(func=cmd_sort)

    p = change_parser("shuffle", "shuffle a playlist's tracks")
    p.add_argument("playlist", help="ID or name")
    p.add_argument("--seed", type=int, help="repeatable shuffle")
    p.add_argument("--as-new", metavar="NAME", help="save the result as a new playlist instead")
    p.set_defaults(func=cmd_shuffle)

    p = change_parser("split", "split a playlist into several")
    p.add_argument("playlist", help="ID or name")
    how = p.add_mutually_exclusive_group(required=True)
    how.add_argument("--every", type=int, metavar="N", help="a new playlist every N tracks")
    how.add_argument("--by", choices=edits.SPLIT_BY)
    p.add_argument("--min", type=int, default=1, metavar="N",
                   help="with --by: groups smaller than N go together into '<name> – other'")
    p.add_argument("--delete-original", action="store_true")
    backup_flags(p)
    p.set_defaults(func=cmd_split)

    p = change_parser("clone", "copy a playlist")
    p.add_argument("playlist", help="ID or name")
    p.add_argument("--name", help="name of the copy (default: '<name> (copy)')")
    p.set_defaults(func=cmd_clone)

    p = sub.add_parser("find", help="find where tracks are, loose tracks, or overlapping playlists")
    p.add_argument("query", nargs="?", help="words from a title or artist")
    p.add_argument("--no-playlist", action="store_true", help="library tracks that are in no playlist")
    p.add_argument("--liked-only", action="store_true", help="liked songs that are in no other playlist")
    p.add_argument("--overlaps", action="store_true", help="playlists that are copies or subsets of others")
    p.add_argument("--min-ratio", type=float, default=analysis.SIMILAR_DEFAULT,
                   help="with --overlaps: how much two playlists must share (default 0.8)")
    p.set_defaults(func=cmd_find)

    p = sub.add_parser("check", help="list what needs a look: empty, duplicate and overlapping playlists")
    p.set_defaults(func=cmd_check)

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
    f = _filter(args)
    if f.is_set():
        chosen = set(select.apply_filter(store, [s.id for s in summaries], f))
        summaries = [s for s in summaries if s.id in chosen]
        if not summaries:
            print("no playlists match")
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
    return _submit(store, args, edits.plan_delete(store, _pick(store, args)))


def cmd_remove(store: Store, args) -> int:
    return _submit(store, args, edits.plan_remove(store, _one(store, args.playlist), args.positions))


def cmd_dedupe(store: Store, args) -> int:
    if not args.duplicates:
        _ensure_duplicate_setting(store)
    return _submit(store, args, edits.plan_dedupe(store, _pick(store, args), args.duplicates, args.keep))


def cmd_merge(store: Store, args) -> int:
    if not args.duplicates:
        _ensure_duplicate_setting(store)
    into = _one(store, args.into) if args.into else None
    plan = edits.plan_merge(store, _ids(store, args.playlists, args.include_followed), into_id=into,
                            into_name=args.new, delete_sources=args.delete_sources,
                            level=args.duplicates)
    return _submit(store, args, plan)


def cmd_merge_names(store: Store, args) -> int:
    if not args.duplicates:
        _ensure_duplicate_setting(store)
    ids = _pick(store, args, default_all=True)
    description, ops, groups = edits.plan_merge_by_name(
        store, ids, args.survivor, delete_sources=not args.keep_sources, level=args.duplicates)
    if not groups:
        print("no playlists share a name")
        return 0
    print("groups (kept playlist ← merged in):")
    for kept, others in groups:
        print(f"  {kept!r} ← {', '.join(map(repr, others))}")
    return _submit(store, args, (description, ops))


def cmd_rename_all(store: Store, args) -> int:
    rules = edits.RenameRules(
        replace=[(old, new, False) for old, new in args.replace]
        + [(old, new, True) for old, new in args.replace_regex],
        trim=args.trim, case=args.case, strip_copies=args.strip_copies,
        prefix=args.prefix, suffix=args.suffix,
    )
    if not (rules.replace or rules.trim or rules.case or rules.strip_copies or rules.prefix or rules.suffix):
        raise ValueError("give at least one rule, e.g. --trim or --replace OLD NEW")
    return _submit(store, args, edits.plan_bulk_rename(store, _pick(store, args), rules))


def cmd_sort(store: Store, args) -> int:
    keys = [k.strip() for k in args.by.split(",") if k.strip()]
    return _submit(store, args, edits.plan_sort(store, _one(store, args.playlist), keys, args.reverse, args.as_new))


def cmd_shuffle(store: Store, args) -> int:
    return _submit(store, args, edits.plan_shuffle(store, _one(store, args.playlist), args.seed, args.as_new))


def cmd_split(store: Store, args) -> int:
    plan = edits.plan_split(store, _one(store, args.playlist), every=args.every, by=args.by,
                            min_size=args.min, delete_original=args.delete_original)
    return _submit(store, args, plan)


def cmd_clone(store: Store, args) -> int:
    return _submit(store, args, edits.plan_clone(store, _one(store, args.playlist), args.name))


def cmd_find(store: Store, args) -> int:
    modes = [bool(args.query), args.no_playlist, args.liked_only, args.overlaps]
    if sum(modes) != 1:
        raise ValueError("give words to search for, or one of --no-playlist, --liked-only, --overlaps")
    if args.query:
        hits = analysis.where(store, args.query)
        if not hits:
            print(f"no tracks match {args.query!r}")
        for hit in hits:
            print(f"{_label(hit.track)}")
            if not hit.places:
                print("    in no playlist")
            for pid, name, positions in hit.places:
                print(f"    {name!r} (#{pid}) at {', '.join(map(str, positions))}")
    elif args.no_playlist or args.liked_only:
        tracks = analysis.liked_unplaylisted(store) if args.liked_only else analysis.unplaylisted(store)
        what = "liked songs in no other playlist" if args.liked_only else "tracks in no playlist"
        print(f"{len(tracks)} {what}")
        for track in tracks:
            print(f"    {_label(track)}")
    else:
        names = {s.id: s.name for s in store.playlist_summaries()}
        found = analysis.overlaps(store, min_ratio=args.min_ratio)
        if not found:
            print("no overlapping playlists")
        for o in found:
            if o.kind == "subset":
                print(f"{names[o.a]!r} (#{o.a}) is entirely inside {names[o.b]!r} (#{o.b}): {o.shared} tracks")
            else:
                print(f"{names[o.a]!r} (#{o.a}) and {names[o.b]!r} (#{o.b}) are {o.kind}: "
                      f"{o.shared} shared tracks, {o.ratio:.0%}")
    return 0


def cmd_check(store: Store, args) -> int:
    findings = analysis.check(store)
    if not findings:
        print("Needs a look: nothing. The library looks tidy.")
        return 0
    print("Needs a look:")
    for finding in findings:
        print(f"\n* {finding.title}")
        for line in finding.lines[:10]:
            print(f"    {line}")
        if len(finding.lines) > 10:
            print(f"    … and {len(finding.lines) - 10} more")
        if finding.fix:
            print(f"  fix: {finding.fix}")
    print("\nUnavailable tracks will show up here once a service is connected.")
    return 0


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


def _filter(args) -> select.Filter:
    kinds = set(getattr(args, "kind", []) or [])
    if getattr(args, "include_followed", False):
        kinds |= {"created", "collaborative", "followed"}
    return select.Filter(
        name=getattr(args, "name", None), regex=getattr(args, "regex", False),
        empty=getattr(args, "empty", False), min_tracks=getattr(args, "min_tracks", None),
        max_tracks=getattr(args, "max_tracks", None), kinds=kinds,
        service=getattr(args, "service", None), identical=getattr(args, "identical", False),
        subset=getattr(args, "subset", False),
    )


def _pick(store: Store, args, default_all: bool = False) -> list[int]:
    """Playlists named on the command line, narrowed by any filters; or all that pass the filters."""
    f = _filter(args)
    if args.playlists:
        ids = _ids(store, args.playlists, include_followed=bool(f.kinds - {"created", "collaborative"}))
        return select.apply_filter(store, ids, f) if f.is_set() else ids
    if not f.is_set() and not default_all:
        raise ValueError("name some playlists, or choose them with a filter such as --empty or --name")
    ids = select.apply_filter(store, [s.id for s in store.playlist_summaries()], f)
    if not ids:
        raise ValueError("no playlists match the filters")
    return ids


def _label(track) -> str:
    artists = ", ".join(track.artists)
    return f"{artists} - {track.title}" if artists else track.title


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
