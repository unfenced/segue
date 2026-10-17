"""The neutral track model.

A Track is a recording, independent of any service. It carries the IDs it has on each
service it is known to (``refs``), so playlists can be expressed once and written anywhere.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field


@dataclass(frozen=True)
class ServiceRef:
    """Where a track or playlist lives on one service."""

    service: str  # "spotify", "tidal", "file", ...
    id: str
    url: str | None = None


@dataclass
class Track:
    title: str
    artists: list[str] = field(default_factory=list)
    album: str | None = None
    duration_ms: int | None = None
    isrc: str | None = None
    refs: list[ServiceRef] = field(default_factory=list)
    id: int | None = None  # library ID, set once stored

    def ref(self, service: str) -> ServiceRef | None:
        return next((r for r in self.refs if r.service == service), None)

    def match_key(self) -> tuple[str, str]:
        """Exact-match key for deduplicating tracks that have no shared ID."""
        return normalize(self.title), normalize(", ".join(self.artists))


@dataclass
class PlaylistEntry:
    track: Track
    added_at: str | None = None  # ISO 8601, as given by the source


@dataclass
class Playlist:
    name: str
    entries: list[PlaylistEntry] = field(default_factory=list)
    description: str | None = None
    # Identity on the source service. Playlists without one are always imported as new.
    source: ServiceRef | None = None
    id: int | None = None

    @property
    def tracks(self) -> list[Track]:
        return [e.track for e in self.entries]


_SPACE = re.compile(r"\s+")


def normalize(text: str | None) -> str:
    """Case-, width- and whitespace-insensitive form of a string.

    Deliberately conservative: keeps brackets and words like "live" or "remix", so
    different recordings never collapse into one. Fuzzy matching comes later.
    """
    if not text:
        return ""
    text = unicodedata.normalize("NFKC", text).casefold()
    return _SPACE.sub(" ", text).strip()


def normalize_isrc(isrc: str | None) -> str | None:
    if not isrc:
        return None
    cleaned = re.sub(r"[^A-Za-z0-9]", "", isrc).upper()
    return cleaned or None
