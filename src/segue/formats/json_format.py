"""Segue's own JSON format: everything the library knows, in a form it can read back."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from .. import __version__
from ..model import Playlist, PlaylistEntry, ServiceRef, Track

FORMAT_NAME = "segue-playlists"
FORMAT_VERSION = 1


def _ref_to_dict(ref: ServiceRef) -> dict:
    out = {"service": ref.service, "id": ref.id}
    if ref.url:
        out["url"] = ref.url
    return out


def _ref_from_dict(data: dict) -> ServiceRef:
    return ServiceRef(str(data["service"]), str(data["id"]), data.get("url"))


def to_dict(playlists: list[Playlist]) -> dict:
    return {
        "format": FORMAT_NAME,
        "version": FORMAT_VERSION,
        "exported_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "exported_by": f"segue {__version__}",
        "playlists": [
            {
                "name": p.name,
                "description": p.description,
                "source": _ref_to_dict(p.source) if p.source else None,
                "tracks": [
                    {
                        "title": e.track.title,
                        "artists": e.track.artists,
                        "album": e.track.album,
                        "duration_ms": e.track.duration_ms,
                        "isrc": e.track.isrc,
                        "refs": [_ref_to_dict(r) for r in e.track.refs],
                        "added_at": e.added_at,
                    }
                    for e in p.entries
                ],
            }
            for p in playlists
        ],
    }


def from_dict(data: dict) -> list[Playlist]:
    if data.get("format") != FORMAT_NAME:
        raise ValueError("Not a Segue playlists file (missing \"format\": \"segue-playlists\")")
    if data.get("version", 0) > FORMAT_VERSION:
        raise ValueError(f"File is format version {data['version']}; this Segue reads up to {FORMAT_VERSION}")
    playlists = []
    for p in data.get("playlists", []):
        entries = [
            PlaylistEntry(
                Track(
                    title=t["title"],
                    artists=list(t.get("artists") or []),
                    album=t.get("album"),
                    duration_ms=t.get("duration_ms"),
                    isrc=t.get("isrc"),
                    refs=[_ref_from_dict(r) for r in t.get("refs") or []],
                ),
                t.get("added_at"),
            )
            for t in p.get("tracks", [])
        ]
        source = _ref_from_dict(p["source"]) if p.get("source") else None
        playlists.append(Playlist(p["name"], entries, p.get("description"), source))
    return playlists


def write(playlists: list[Playlist], path: Path) -> None:
    path.write_text(json.dumps(to_dict(playlists), indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def read(path: Path) -> list[Playlist]:
    return from_dict(json.loads(path.read_text(encoding="utf-8")))
