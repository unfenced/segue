"""Command-line interface."""

from __future__ import annotations

import argparse
import re
import sys
from datetime import datetime
from pathlib import Path

from . import __version__
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

    args = parser.parse_args(argv)
    db_path = args.db or default_db_path()
    try:
        with Store(db_path) as store:
            args.db_path = db_path
            return args.func(store, args) or 0
    except (ValueError, KeyError, OSError) as e:
        print(f"segue: {e.args[0] if isinstance(e, KeyError) else e}", file=sys.stderr)
        return 1


def cmd_import(store: Store, args) -> int:
    before = store.track_count()
    for path in args.files:
        for playlist in read_playlists(path, args.format):
            _, status = store.save_playlist(playlist)
            print(f"{status} {playlist.name!r} ({len(playlist.entries)} tracks) from {path.name}")
    print(f"library: {store.track_count()} tracks ({store.track_count() - before} new)")
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
            print(f"{position:>4}. {artists} - {track.title}{length}  [{services}]")
    return 0


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
