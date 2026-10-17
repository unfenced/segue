"""Recognising and building track links for known services."""

from __future__ import annotations

import re
from urllib.parse import parse_qs, urlparse

from .model import ServiceRef

_SPOTIFY_URI = re.compile(r"^spotify:track:([A-Za-z0-9]+)$")
_PATH_PATTERNS = {
    "spotify": re.compile(r"(?:^|\.)spotify\.com$"),
    "tidal": re.compile(r"(?:^|\.)tidal\.com$"),
    "deezer": re.compile(r"(?:^|\.)deezer\.com$"),
}
_TRACK_PATH = re.compile(r"/track/([A-Za-z0-9]+)")

_URL_TEMPLATES = {
    "spotify": "https://open.spotify.com/track/{id}",
    "tidal": "https://tidal.com/browse/track/{id}",
    "deezer": "https://www.deezer.com/track/{id}",
    "youtube": "https://music.youtube.com/watch?v={id}",
}


def parse_track_link(text: str | None) -> ServiceRef | None:
    """Turn a track URL or URI into a ServiceRef, or None if it isn't one we know."""
    if not text:
        return None
    text = text.strip()
    if m := _SPOTIFY_URI.match(text):
        return ServiceRef("spotify", m.group(1), _URL_TEMPLATES["spotify"].format(id=m.group(1)))
    parsed = urlparse(text)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        return None
    host = parsed.hostname.lower()
    for service, pattern in _PATH_PATTERNS.items():
        if pattern.search(host) and (m := _TRACK_PATH.search(parsed.path)):
            return ServiceRef(service, m.group(1), text)
    query = parse_qs(parsed.query)
    if host.endswith("music.apple.com") and "i" in query:
        return ServiceRef("apple", query["i"][0], text)
    if host.endswith("youtube.com") and "v" in query:
        return ServiceRef("youtube", query["v"][0], text)
    if host == "youtu.be" and parsed.path.strip("/"):
        return ServiceRef("youtube", parsed.path.strip("/"), text)
    return None


def track_url(ref: ServiceRef) -> str | None:
    if ref.url:
        return ref.url
    template = _URL_TEMPLATES.get(ref.service)
    return template.format(id=ref.id) if template else None
