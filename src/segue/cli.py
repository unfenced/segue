"""Command-line interface."""

from __future__ import annotations

import argparse
import re
import sys
from datetime import datetime
from pathlib import Path

from . import __version__, changes, edits
from .formats import FORMATS, csv_format, detect_format, json_format, m3u_format, read_playlists
from .model import Playlist
from .store import Store, default_db_path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="segue", description=__doc__)
    parser.add_argument("--version", action="version", version=f"segue {__version__}")
    parser.add_argument("--db", type=Path, default=None,
                        help=f"library file (default: $SEGUE_DB or {default_db_path()})")
    sub = parser.add_subparsers(dest="command", required=True)

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

    # Changes: each goes through the change log (queued or applied, per apply mode).
    def change_parser(name, help):
        p = sub.add_parser(name, help=help)
        group = p.add_mutually_exclusive_group()
        group.add_argument("--now", dest="mode", action="store_const", const="auto",
                           help="apply right away, even in commit mode")
        group.add_argument("--queue", dest="mode", action="store_const", const="commit",
                           help="queue for review, even in auto mode")
        group.add_argument("--dry-run", dest="mode", action="store_const", const="dry",
                           help="only show what would change")
        return p

    p = change_parser("rename", "rename a playlist")
    p.add_argument("playlist", help="ID or name")
    p.add_argument("name", help="new name")
    p.set_defaults(func=cmd_rename)

    p = change_parser("delete", "delete playlists")
    p.add_argument("playlists", nargs="+", help="IDs or names")
    p.set_defaults(func=cmd_delete)

    p = change_parser("remove", "remove tracks from a playlist by position")
    p.add_argument("playlist", help="ID or name")
    p.add_argument("positions", nargs="+", type=int, help="positions as shown by 'segue show'")
    p.set_defaults(func=cmd_remove)

    p = change_parser("dedupe", "remove repeated tracks from playlists, keeping the first")
    p.add_argument("playlists", nargs="+", help="IDs or names")
    p.set_defaults(func=cmd_dedupe)

    p = change_parser("merge", "merge playlists without duplicate tracks")
    p.add_argument("playlists", nargs="+", help="IDs or names of the playlists to merge in")
    target = p.add_mutually_exclusive_group(required=True)
    target.add_argument("--into", metavar="PLAYLIST", help="existing playlist to merge into")
    target.add_argument("--new", metavar="NAME", help="create a new playlist with this name")
    p.add_argument("--delete-sources", action="store_true", help="delete the merged-in playlists")
    p.set_defaults(func=cmd_merge)

    # The change log
    p = sub.add_parser("pending", help="show changes waiting to be committed")
    p.set_defaults(func=cmd_pending)

    p = sub.add_parser("commit", help="apply pending changes (default: all, oldest first)")
    p.add_argument("batches", nargs="*", type=int)
    p.set_defaults(func=cmd_commit)

    p = sub.add_parser("discard", help="drop pending changes (default: all)")
    p.add_argument("batches", nargs="*", type=int)
    p.set_defaults(func=cmd_discard)

    p = sub.add_parser("history", help="show applied and undone changes")
    p.add_argument("-n", type=int, default=20, help="how many (default 20)")
    p.set_defaults(func=cmd_history)

    p = sub.add_parser("undo", help="undo a change (default: the last one)")
    p.add_argument("batch", nargs="?", type=int)
    p.set_defaults(func=cmd_undo)

    p = sub.add_parser("redo", help="redo an undone change (default: the last one)")
    p.add_argument("batch", nargs="?", type=int)
    p.set_defaults(func=cmd_redo)

    p = sub.add_parser("config", help="show or change settings")
    p.add_argument("key", nargs="?", choices=["apply-mode"])
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


def cmd_import(store: Store, args) -> int:
    before = store.track_count()
    for path in args.files:
        for playlist in read_playlists(path, args.format):
            _, status, batch_id = changes.record_import(store, playlist, path.name)
            note = f"  [#{batch_id}]" if batch_id else ""
            print(f"{status} {playlist.name!r} ({len(playlist.entries)} tracks) from {path.name}{note}")
    print(f"library: {store.track_count()} tracks ({store.track_count() - before} new)")
    print("undo an import with: segue undo <#>")
    return 0


def cmd_export(store: Store, args) -> int:
    playlists = _select(store, args.playlist)
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
        (json_format if fmt == "json" else csv_format).write(playlists, args.output)
        print(f"wrote {len(playlists)} playlists to {args.output}")
    return 0


def cmd_backup(store: Store, args) -> int:
    folder = args.folder or Path(args.db_path).parent / "backups"
    folder.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y-%m-%d_%H%M%S")
    playlists = store.all_playlists()
    for fmt, module in (("json", json_format), ("csv", csv_format)):
        path = folder / f"segue-backup-{stamp}.{fmt}"
        module.write(playlists, path)
        print(f"wrote {path}")
    print(f"{len(playlists)} playlists, {sum(len(p.entries) for p in playlists)} entries")
    return 0


def cmd_list(store: Store, args) -> int:
    summaries = store.playlist_summaries()
    if not summaries:
        print("No playlists yet. Add some with: segue import <file>")
        return 0
    width = max(len(s.name) for s in summaries)
    for s in summaries:
        source = f"{s.source.service}:{s.source.id}" if s.source else "-"
        print(f"{s.id:>5}  {s.name:<{width}}  {s.track_count:>5} tracks  {source}")
    return 0


def cmd_show(store: Store, args) -> int:
    for playlist in _select(store, [args.playlist]):
        print(f"{playlist.name} ({len(playlist.entries)} tracks)")
        for position, track in enumerate(playlist.tracks, start=1):
            artists = ", ".join(track.artists) or "?"
            length = ""
            if track.duration_ms is not None:
                minutes, seconds = divmod(round(track.duration_ms / 1000), 60)
                length = f"  {minutes}:{seconds:02d}"
            services = ",".join(dict.fromkeys(r.service for r in track.refs))
            print(f"{position:>4}. {artists} - {track.title}{length}" + (f"  [{services}]" if services else ""))
    return 0


def cmd_rename(store: Store, args) -> int:
    return _submit(store, args, edits.plan_rename(store, _one(store, args.playlist), args.name))


def cmd_delete(store: Store, args) -> int:
    return _submit(store, args, edits.plan_delete(store, _ids(store, args.playlists)))


def cmd_remove(store: Store, args) -> int:
    return _submit(store, args, edits.plan_remove(store, _one(store, args.playlist), args.positions))


def cmd_dedupe(store: Store, args) -> int:
    return _submit(store, args, edits.plan_dedupe(store, _ids(store, args.playlists)))


def cmd_merge(store: Store, args) -> int:
    into = _one(store, args.into) if args.into else None
    plan = edits.plan_merge(store, _ids(store, args.playlists), into_id=into,
                            into_name=args.new, delete_sources=args.delete_sources)
    return _submit(store, args, plan)


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
    batch_id, applied = changes.submit(store, description, ops, args.mode)
    if applied:
        print(f"applied as #{batch_id}. Undo with: segue undo")
    else:
        print(f"queued as #{batch_id}. Apply with: segue commit (or discard with: segue discard {batch_id})")
    return 0


def cmd_pending(store: Store, args) -> int:
    pending = changes.batches(store, "pending")
    if not pending:
        print("nothing pending")
        return 0
    for batch in pending:
        print(f"#{batch.id}  {batch.description}")
        for line in changes.preview(store, changes.batch_ops(store, batch.id)):
            print(f"      - {line}")
    print("apply with: segue commit [#...]   drop with: segue discard [#...]")
    return 0


def cmd_commit(store: Store, args) -> int:
    ids = args.batches or [b.id for b in changes.batches(store, "pending")]
    if not ids:
        print("nothing pending")
    for batch_id in ids:
        changes.commit(store, batch_id)
        print(f"applied #{batch_id}  {changes.get_batch(store, batch_id).description}")
    return 0


def cmd_discard(store: Store, args) -> int:
    ids = args.batches or [b.id for b in changes.batches(store, "pending")]
    if not ids:
        print("nothing pending")
    for batch_id in ids:
        changes.discard(store, batch_id)
        print(f"discarded #{batch_id}")
    return 0


def cmd_history(store: Store, args) -> int:
    shown = [b for b in changes.batches(store, limit=args.n * 2) if b.status in ("applied", "undone")]
    if not shown:
        print("no changes yet")
    for batch in shown[:args.n]:
        when = (batch.undone_at if batch.status == "undone" else batch.applied_at) or ""
        print(f"#{batch.id:<4} {batch.status:<8} {when[:19].replace('T', ' ')}  {batch.description}")
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
    if args.key is None:
        print(f"apply-mode = {changes.apply_mode(store)}")
        return 0
    if args.value is None:
        print(changes.apply_mode(store))
        return 0
    changes.set_apply_mode(store, args.value)
    print(f"apply-mode = {args.value}")
    return 0


def _ids(store: Store, selectors: list[str]) -> list[int]:
    return [p.id for p in _select(store, selectors)]


def _one(store: Store, selector: str) -> int:
    ids = _ids(store, [selector])
    if len(ids) > 1:
        raise ValueError(f"{selector!r} matches several playlists ({', '.join(map(str, ids))}); use an ID")
    return ids[0]


def _select(store: Store, selectors: list[str]) -> list[Playlist]:
    if not selectors:
        return store.all_playlists()
    playlists: dict[int, Playlist] = {}
    for selector in selectors:
        found = [store.get_playlist(int(selector))] if selector.isdigit() else store.find_playlists(selector)
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
