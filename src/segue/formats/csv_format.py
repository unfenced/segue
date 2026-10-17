"""CSV: one row per playlist entry, readable in any spreadsheet.

Segue writes these columns:

    playlist, playlist_description, playlist_service, playlist_id, playlist_url,
    position, title, artists, album, duration_ms, isrc, added_at,
    <service>_id, <service>_url   (one pair per service that appears, e.g. spotify_id)

Artists are separated by "; ". A playlist with no tracks is written as a single row with
an empty title, so it survives a round trip.

On import, column names are matched loosely, so CSVs from other tools work too
(for example Exportify's "Track URI", "Track Name", "Artist Name(s)", "Duration (ms)").
A file without a playlist column is one playlist named after the file.
"""

from __future__ import annotations

import csv
import re
from pathlib import Path

from ..model import Playlist, PlaylistEntry, ServiceRef, Track
from ..services import parse_track_link, track_url

ARTIST_SEPARATOR = "; "

_FIXED_COLUMNS = [
    "playlist", "playlist_description", "playlist_service", "playlist_id", "playlist_url",
    "position", "title", "artists", "album", "duration_ms", "isrc", "added_at",
]

# Accepted header spellings (after lower-casing and trimming) for each field.
_ALIASES = {
    "playlist": {"playlist", "playlist name", "playlist_name"},
    "playlist_description": {"playlist_description", "playlist description", "description"},
    "playlist_service": {"playlist_service"},
    "playlist_id": {"playlist_id", "playlist id"},
    "playlist_url": {"playlist_url", "playlist url"},
    "position": {"position", "#", "pos"},
    "title": {"title", "track name", "track", "name", "song", "track title"},
    "artists": {"artists", "artist", "artist name(s)", "artist name", "artist names"},
    "album": {"album", "album name", "album title"},
    "duration_ms": {"duration_ms", "duration (ms)", "duration ms"},
    "duration": {"duration", "length", "time"},
    "isrc": {"isrc"},
    "added_at": {"added_at", "added at", "date added", "added"},
    "link": {"track uri", "spotify uri", "uri", "url", "link", "track url", "spotify url"},
}

_SERVICE_COLUMN = re.compile(r"^([a-z0-9]+)_(id|url)$")


def write(playlists: list[Playlist], path: Path) -> None:
    services = sorted({r.service for p in playlists for t in p.tracks for r in t.refs})
    columns = _FIXED_COLUMNS + [f"{s}_{kind}" for s in services for kind in ("id", "url")]
    # utf-8-sig so Excel shows accents and non-Latin titles correctly.
    with path.open("w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, columns)
        writer.writeheader()
        for p in playlists:
            base = {
                "playlist": p.name,
                "playlist_description": p.description or "",
                "playlist_service": p.source.service if p.source else "",
                "playlist_id": p.source.id if p.source else "",
                "playlist_url": (p.source.url or "") if p.source else "",
            }
            if not p.entries:
                writer.writerow(base)
                continue
            for position, entry in enumerate(p.entries, start=1):
                t = entry.track
                row = base | {
                    "position": position,
                    "title": t.title,
                    "artists": ARTIST_SEPARATOR.join(t.artists),
                    "album": t.album or "",
                    "duration_ms": "" if t.duration_ms is None else t.duration_ms,
                    "isrc": t.isrc or "",
                    "added_at": entry.added_at or "",
                }
                # One ID per service fits in a row; the first is the entry's own.
                for ref in t.refs:
                    if f"{ref.service}_id" not in row:
                        row[f"{ref.service}_id"] = ref.id
                        row[f"{ref.service}_url"] = track_url(ref) or ""
                writer.writerow(row)


def read(path: Path) -> list[Playlist]:
    with path.open(newline="", encoding="utf-8-sig") as f:
        sample = f.read(64 * 1024)
        f.seek(0)
        reader = csv.reader(f, _dialect(path, sample))
        try:
            header = next(reader)
        except StopIteration:
            return []
        fields = _map_header(header)
        if "title" not in fields.values():
            raise ValueError(f"{path.name}: no title column found (looked for e.g. 'title', 'Track Name')")

        playlists: dict[tuple, Playlist] = {}
        positions: dict[tuple, list[int | None]] = {}
        for raw in reader:
            if not any(cell.strip() for cell in raw):
                continue
            row = _row_values(raw, fields)
            key, playlist = _playlist_for(row, path, playlists)
            title = row.get("title", "").strip()
            if not title:
                continue  # empty-playlist marker row
            playlist.entries.append(PlaylistEntry(_track(row), row.get("added_at") or None))
            positions.setdefault(key, []).append(_int(row.get("position")))

    # Honour an explicit position column; otherwise keep file order.
    for key, playlist in playlists.items():
        pos = positions.get(key)
        if pos and all(p is not None for p in pos):
            order = sorted(range(len(pos)), key=lambda i: pos[i])
            playlist.entries = [playlist.entries[i] for i in order]
    return list(playlists.values())


def _dialect(path: Path, sample: str) -> type[csv.Dialect] | csv.Dialect:
    if path.suffix.lower() == ".tsv":
        return csv.excel_tab
    try:
        # Dutch and other European Excel versions save CSV with semicolons.
        return csv.Sniffer().sniff(sample.split("\n", 1)[0], delimiters=",;\t")
    except csv.Error:
        return csv.excel


def _map_header(header: list[str]) -> dict[int, str]:
    """Column index -> field name. Service columns map to e.g. 'ref:spotify:id'."""
    fields: dict[int, str] = {}
    for index, name in enumerate(header):
        clean = name.strip().lower()
        for field, aliases in _ALIASES.items():
            if clean in aliases and field not in fields.values():
                fields[index] = field
                break
        else:
            if m := _SERVICE_COLUMN.match(clean):
                if m.group(1) != "playlist":
                    fields[index] = f"ref:{m.group(1)}:{m.group(2)}"
    return fields


def _row_values(raw: list[str], fields: dict[int, str]) -> dict[str, str]:
    row = {}
    for index, field in fields.items():
        if index < len(raw):
            row[field] = raw[index].strip()
    return row


def _playlist_for(row: dict[str, str], path: Path, playlists: dict[tuple, Playlist]):
    name = row.get("playlist") or path.stem
    service, pid = row.get("playlist_service") or None, row.get("playlist_id") or None
    key = (name, service, pid) if not (service and pid) else (service, pid)
    if key not in playlists:
        source = ServiceRef(service, pid, row.get("playlist_url") or None) if service and pid else None
        playlists[key] = Playlist(name, description=row.get("playlist_description") or None, source=source)
    return key, playlists[key]


def _track(row: dict[str, str]) -> Track:
    artists_text = row.get("artists", "")
    # Segue separates artists with ";"; other tools (Exportify) use ",".
    separator = ";" if ";" in artists_text else ","
    artists = [a.strip() for a in artists_text.split(separator) if a.strip()]

    duration = _int(row.get("duration_ms"))
    if duration is None and row.get("duration"):
        duration = _parse_duration(row["duration"])

    refs: dict[str, ServiceRef] = {}
    ids: dict[str, dict[str, str]] = {}
    for field, value in row.items():
        if field.startswith("ref:") and value:
            _, service, kind = field.split(":")
            ids.setdefault(service, {})[kind] = value
    for service, parts in ids.items():
        if "id" in parts:
            refs[service] = ServiceRef(service, parts["id"], parts.get("url"))
        elif link := parse_track_link(parts.get("url")):
            refs[link.service] = link
    if (link := parse_track_link(row.get("link"))) and link.service not in refs:
        refs[link.service] = link

    return Track(
        title=row["title"],
        artists=artists,
        album=row.get("album") or None,
        duration_ms=duration,
        isrc=row.get("isrc") or None,
        refs=list(refs.values()),
    )


def _int(value: str | None) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(float(value))
    except ValueError:
        return None


def _parse_duration(value: str) -> int | None:
    """'3:45', '1:02:03' or seconds -> milliseconds."""
    try:
        parts = [float(p) for p in value.split(":")]
    except ValueError:
        return None
    seconds = 0.0
    for part in parts:
        seconds = seconds * 60 + part
    return int(seconds * 1000)
