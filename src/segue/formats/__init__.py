"""Import and export of playlists in open formats.

- JSON: Segue's own full-fidelity format; the canonical backup.
- CSV: one row per playlist entry, opens in any spreadsheet. Also reads common exports
  from other tools (e.g. Exportify) by recognising their column names.
- M3U / M3U8: extended M3U, one file per playlist, for media players.
"""

from __future__ import annotations

from pathlib import Path

from ..model import Playlist
from . import csv_format, json_format, m3u_format

FORMATS = ("json", "csv", "m3u")


def detect_format(path: Path) -> str:
    suffix = path.suffix.lower()
    if suffix == ".json":
        return "json"
    if suffix in (".csv", ".tsv"):
        return "csv"
    if suffix in (".m3u", ".m3u8"):
        return "m3u"
    raise ValueError(f"Can't tell the format of {path.name}; pass --format ({', '.join(FORMATS)})")


def read_playlists(path: Path, fmt: str | None = None) -> list[Playlist]:
    fmt = fmt or detect_format(path)
    if fmt == "json":
        return json_format.read(path)
    if fmt == "csv":
        return csv_format.read(path)
    if fmt == "m3u":
        return [m3u_format.read(path)]
    raise ValueError(f"Unknown format {fmt!r}")
