"""Choosing playlists by filter, and grouping playlists that share a name.

Filters narrow a set of playlists. Followed playlists and Liked songs are left out unless
asked for, because most bulk tools can't (or shouldn't) change them.
"""

from __future__ import annotations

import fnmatch
import re
import unicodedata
from collections import Counter
from dataclasses import dataclass, field

from . import dupes
from .model import KINDS, PlaylistState, normalize
from .store import Store


@dataclass
class Filter:
    name: str | None = None  # glob ("gym*"), or a regular expression with ``regex``
    regex: bool = False
    empty: bool = False
    min_tracks: int | None = None
    max_tracks: int | None = None
    kinds: set[str] = field(default_factory=set)  # empty: created and collaborative
    service: str | None = None  # "local" for playlists that aren't from a service
    identical: bool = False  # newer copies of a playlist with exactly the same tracks
    subset: bool = False  # playlists whose tracks are all in a bigger one

    def is_set(self) -> bool:
        return any([self.name, self.empty, self.min_tracks is not None, self.max_tracks is not None,
                    self.kinds, self.service, self.identical, self.subset])


def service_of(state: PlaylistState) -> str:
    return state.source.service if state.source else "local"


def apply_filter(store: Store, ids: list[int], f: Filter) -> list[int]:
    """The IDs from ``ids`` that pass every condition of ``f``, in the same order."""
    for kind in f.kinds:
        if kind not in KINDS:
            raise ValueError(f"unknown kind {kind!r}; choose from {', '.join(KINDS)}")
    kinds = f.kinds or {"created", "collaborative"}
    states = {pid: store.playlist_state(pid) for pid in ids}
    keep = [pid for pid in ids if states[pid] is not None and states[pid].kind in kinds]

    if f.name:
        if f.regex:
            try:
                pattern = re.compile(f.name, re.IGNORECASE)
            except re.error as e:
                raise ValueError(f"bad regular expression {f.name!r}: {e}") from None
            keep = [pid for pid in keep if pattern.search(states[pid].name)]
        else:
            glob = normalize(f.name)
            keep = [pid for pid in keep if fnmatch.fnmatchcase(normalize(states[pid].name), glob)]
    if f.service:
        keep = [pid for pid in keep if service_of(states[pid]) == f.service]
    if f.empty:
        keep = [pid for pid in keep if not states[pid].entries]
    if f.min_tracks is not None:
        keep = [pid for pid in keep if len(states[pid].entries) >= f.min_tracks]
    if f.max_tracks is not None:
        keep = [pid for pid in keep if len(states[pid].entries) <= f.max_tracks]
    if f.identical:
        newer = set()
        for group in identical_groups(store, keep):
            newer.update(group[1:])  # keep the oldest of each group out of the selection
        keep = [pid for pid in keep if pid in newer]
    if f.subset:
        inside = {small for small, _ in subsets(store, keep)}
        keep = [pid for pid in keep if pid in inside]
    return keep


def identical_groups(store: Store, ids: list[int]) -> list[list[int]]:
    """Playlists on the same service with exactly the same tracks (in any order), oldest first."""
    groups: dict[tuple, list[int]] = {}
    for pid in sorted(ids):
        state = store.playlist_state(pid)
        if state is None or not state.entries:
            continue
        key = (service_of(state), tuple(sorted(Counter(state.track_ids).items())))
        groups.setdefault(key, []).append(pid)
    return [g for g in groups.values() if len(g) > 1]


def subsets(store: Store, ids: list[int]) -> list[tuple[int, int]]:
    """``(small, big)`` pairs on the same service where every track of ``small`` is also in
    the bigger ``big``, comparing tracks per the duplicate setting."""
    key = dupes.key_function(store, dupes.level(store))
    states = {pid: store.playlist_state(pid) for pid in ids}
    sets = {pid: {key(e) for e in states[pid].entries} for pid in ids}
    pairs = []
    for small in ids:
        if not sets[small]:
            continue
        for big in ids:
            if (big != small and service_of(states[big]) == service_of(states[small])
                    and len(sets[big]) > len(sets[small]) and sets[small] <= sets[big]):
                pairs.append((small, big))
                break  # one container is enough to report
    return pairs


# Names

# "(1)", "(copy)", "(Copy 2)", "- copy". A bare trailing word "copy" is left alone ("Carbon Copy").
_COPY_SUFFIX = re.compile(r"\s*(?:\((?:\d+|copy(?:\s*\d+)?)\)|[-–]\s*copy(?:\s*\d+)?)\s*$", re.IGNORECASE)


def _is_decoration(ch: str) -> bool:
    """Emoji, symbols, joiners and variation selectors: ignored when comparing names."""
    return unicodedata.category(ch) in ("So", "Sk", "Cf") or "\ufe00" <= ch <= "\ufe0f"


def name_key(name: str) -> str:
    """A playlist name with case, spacing, emoji and copy markers ("(1)", "copy") ignored."""
    text = unicodedata.normalize("NFKC", name)
    text = "".join(ch for ch in text if not _is_decoration(ch))
    previous = None
    while previous != text:
        previous = text
        text = _COPY_SUFFIX.sub("", text)
    return normalize(text).strip(" -–_.")


def strip_copy_marker(name: str) -> str:
    previous = None
    while previous != name:
        previous = name
        name = _COPY_SUFFIX.sub("", name)
    return name.strip() or name


def name_groups(store: Store, ids: list[int]) -> list[list[int]]:
    """Playlists whose names match after ``name_key``, per service; each group oldest first."""
    groups: dict[tuple[str, str], list[int]] = {}
    for pid in sorted(ids):
        state = store.playlist_state(pid)
        key = name_key(state.name) or normalize(state.name)  # an emoji-only name is its own key
        groups.setdefault((service_of(state), key), []).append(pid)
    return [g for g in groups.values() if len(g) > 1]
