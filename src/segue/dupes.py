"""What counts as a duplicate, and which copy is kept.

Three levels, from strictest to loosest:

- ``exact``: the same track on the service (the same service ID).
- ``recording``: the same recording: same ISRC, or the same title and artists with
  lengths within a few seconds (how the library already identifies tracks).
- ``song``: the same song in any version: also ignores version labels such as
  "Remastered 2011", "Radio Edit", "Live" and "feat." credits, and compares the main
  artist only.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Hashable, Sequence

from .model import EntryState, normalize
from .store import Store

LEVELS = ("exact", "recording", "song")
KEEPS = ("first", "earliest", "latest")
DEFAULT_LEVEL = "recording"
DEFAULT_KEEP = "first"

LEVEL_HELP = {
    "exact": "the same track on the service (same ID), e.g. a song added twice",
    "recording": "the same recording, e.g. the single and the album copy",
    "song": "the same song in any version, e.g. the original and the remaster",
}
KEEP_HELP = {
    "first": "the first one in the playlist",
    "earliest": "the one added earliest",
    "latest": "the one added most recently",
}

_VERSION_WORDS = (
    r"remaster(?:ed)?|re-?recorded|live|radio edit|single edit|edit|version|mono|stereo|"
    r"deluxe|bonus(?: track)?|explicit|clean|acoustic|demo|remix|mix|instrumental|"
    r"anniversary|edition|\d{4}"
)
_CREDIT = r"feat\.?|ft\.?|featuring|with"
# "(Remastered 2011)", "[Live]", "(feat. X)", "(with X)"
_BRACKETED = re.compile(
    rf"\s*[(\[][^)\]]*\b(?:{_VERSION_WORDS}|{_CREDIT})\b[^)\]]*[)\]]", re.IGNORECASE
)
# "Song - Remastered 2011", "Song - Live at Wembley", "Song - Radio Edit"
_DASH_SUFFIX = re.compile(rf"\s+-\s+[^-]*\b(?:{_VERSION_WORDS})\b.*$", re.IGNORECASE)
# "Song feat. X" outside brackets
_LOOSE_CREDIT = re.compile(r"\s+(?:feat\.?|ft\.?|featuring)\s.*$", re.IGNORECASE)


def core_title(title: str) -> str:
    """A title without version labels and featured-artist credits, normalized."""
    previous = None
    while previous != title:  # labels can be stacked: "Song (Live) - 2011 Remaster"
        previous = title
        title = _BRACKETED.sub("", title)
        title = _DASH_SUFFIX.sub("", title)
        title = _LOOSE_CREDIT.sub("", title)
    return normalize(title)


def level(store: Store) -> str:
    return store.get_setting("duplicates", DEFAULT_LEVEL)


def keep(store: Store) -> str:
    return store.get_setting("keep", DEFAULT_KEEP)


def is_configured(store: Store) -> bool:
    return store.get_setting("duplicates") is not None


def set_level(store: Store, value: str) -> None:
    if value not in LEVELS:
        raise ValueError(f"duplicates must be one of: {', '.join(LEVELS)}")
    store.set_setting("duplicates", value)


def set_keep(store: Store, value: str) -> None:
    if value not in KEEPS:
        raise ValueError(f"keep must be one of: {', '.join(KEEPS)}")
    store.set_setting("keep", value)


def key_function(store: Store, level: str) -> Callable[[EntryState], Hashable]:
    """How to tell two playlist entries apart at this duplicate level."""
    if level == "exact":
        return lambda e: ("ref", e.ref_service, e.ref_id) if e.ref_id else ("track", e.track_id)
    if level == "recording":
        return lambda e: e.track_id
    if level == "song":
        cache: dict[int, tuple[str, str]] = {}

        def song_key(e: EntryState) -> tuple[str, str]:
            if e.track_id not in cache:
                t = store.get_track(e.track_id)
                cache[e.track_id] = (core_title(t.title), normalize(t.artists[0] if t.artists else ""))
            return cache[e.track_id]

        return song_key
    raise ValueError(f"unknown duplicate level {level!r}")


def duplicate_positions(
    entries: Sequence[EntryState], key: Callable[[EntryState], Hashable], keep: str = DEFAULT_KEEP
) -> list[int]:
    """Positions to remove so each key appears once, keeping the copy ``keep`` picks."""
    groups: dict[Hashable, list[int]] = {}
    for position, entry in enumerate(entries):
        groups.setdefault(key(entry), []).append(position)

    def keeper(positions: list[int]) -> int:
        if keep == "first":
            return positions[0]
        dated = [p for p in positions if entries[p].added_at]
        if not dated:
            return positions[0]  # no dates to compare: fall back to the first copy
        pick = min if keep == "earliest" else max
        return pick(dated, key=lambda p: (entries[p].added_at, -p if keep == "latest" else p))

    remove = []
    for positions in groups.values():
        if len(positions) > 1:
            kept = keeper(positions)
            remove.extend(p for p in positions if p != kept)
    return sorted(remove)
