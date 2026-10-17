"""Extended M3U (.m3u / .m3u8), one playlist per file.

Each entry is written as ``#EXTINF:<seconds>,<artists> - <title>``, an optional
``#EXTALB:<album>``, and a location: the track's service URL, or its file path for
local files. M3U has no room for ISRCs or several service IDs, so it is a convenience
format for players — use JSON or CSV for backups.
"""

from __future__ import annotations

import re
from pathlib import Path

from ..model import Playlist, PlaylistEntry, ServiceRef, Track
from ..services import parse_track_link, track_url

_EXTINF = re.compile(r"^#EXTINF:\s*(-?\d+(?:\.\d+)?)?[^,]*,(.*)$")


def location(track: Track, prefer: list[str] | None = None) -> str | None:
    """The best location line for a track: a preferred service, else any URL, else a file."""
    order = list(prefer or []) + [r.service for r in track.refs]
    for service in order:
        ref = track.ref(service)
        if ref is None:
            continue
        if ref.service == "file":
            return ref.id
        if url := track_url(ref):
            return url
    return None


def write(playlist: Playlist, path: Path, prefer: list[str] | None = None) -> int:
    """Write one playlist. Returns how many entries had no location and were left out."""
    lines = ["#EXTM3U", f"#PLAYLIST:{_one_line(playlist.name)}"]
    skipped = 0
    for entry in playlist.entries:
        t = entry.track
        where = location(t, prefer)
        if where is None:
            skipped += 1
            continue
        seconds = -1 if t.duration_ms is None else round(t.duration_ms / 1000)
        label = f"{', '.join(t.artists)} - {t.title}" if t.artists else t.title
        lines.append(f"#EXTINF:{seconds},{_one_line(label)}")
        if t.album:
            lines.append(f"#EXTALB:{_one_line(t.album)}")
        lines.append(where)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return skipped


def read(path: Path) -> Playlist:
    raw = path.read_bytes()
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = raw.decode("latin-1")  # classic .m3u files are often not UTF-8

    playlist = Playlist(path.stem)
    pending: dict = {}
    for line in (l.strip() for l in text.splitlines()):
        if not line:
            continue
        if line.startswith("#"):
            if line.startswith("#PLAYLIST:"):
                playlist.name = line[len("#PLAYLIST:"):].strip() or playlist.name
            elif m := _EXTINF.match(line):
                pending["seconds"], pending["label"] = m.group(1), m.group(2).strip()
            elif line.startswith("#EXTALB:"):
                pending["album"] = line[len("#EXTALB:"):].strip() or None
            continue
        playlist.entries.append(PlaylistEntry(_track(line, pending, path.parent)))
        pending = {}
    return playlist


def _track(where: str, info: dict, base: Path) -> Track:
    ref = parse_track_link(where)
    if ref is None and not re.match(r"^[a-z][a-z0-9+.-]*://", where, re.I):
        ref = ServiceRef("file", str((base / where).resolve()) if not Path(where).is_absolute() else where)
    elif ref is None:
        ref = ServiceRef("url", where, where)

    label = info.get("label") or Path(where).stem
    artists, title = [], label
    if " - " in label:
        artist_text, title = label.split(" - ", 1)
        artists = [a.strip() for a in artist_text.split(",") if a.strip()]

    duration = None
    if info.get("seconds") and float(info["seconds"]) >= 0:
        duration = int(float(info["seconds"]) * 1000)
    return Track(title=title.strip(), artists=artists, album=info.get("album"),
                 duration_ms=duration, refs=[ref])


def _one_line(text: str) -> str:
    return " ".join(text.split())
