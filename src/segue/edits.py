"""Planners: turn a user action into a description and a list of operations.

Planners only read the library. Nothing changes until the change log applies the result.
"""

from __future__ import annotations

import random
import re
from dataclasses import dataclass, field

from . import dupes, select
from .model import EntryState, PlaylistState, normalize
from .ops import (
    AddEntries, CreatePlaylist, DeletePlaylist, EditPlaylist, Operation, RemovePositions, Reorder,
)
from .store import Store

Plan = tuple[str, list[Operation]]


def _state(store: Store, playlist_id: int) -> PlaylistState:
    state = store.playlist_state(playlist_id)
    if state is None:
        raise KeyError(f"No playlist with id {playlist_id}")
    return state


def _editable(store: Store, playlist_id: int) -> PlaylistState:
    """A playlist whose tracks may be changed."""
    state = _state(store, playlist_id)
    if state.kind == "followed":
        raise ValueError(f"{state.name!r} is a followed playlist: copy it to change its tracks")
    return state


def plan_delete(store: Store, playlist_ids: list[int]) -> Plan:
    states = [_state(store, pid) for pid in playlist_ids]
    if liked := [s.name for s in states if s.kind == "liked"]:
        raise ValueError(f"Liked songs can't be deleted ({_list(liked)})")
    return f"delete {_list([s.name for s in states])}", [DeletePlaylist(pid) for pid in playlist_ids]


def plan_rename(store: Store, playlist_id: int, name: str) -> Plan:
    state = _state(store, playlist_id)
    if state.kind in ("followed", "liked"):
        raise ValueError(f"{state.name!r} can't be renamed: it's a {state.kind} playlist")
    ops = [] if state.name == name else [EditPlaylist(playlist_id, name=name)]
    return f"rename {state.name!r} to {name!r}", ops


def plan_remove(store: Store, playlist_id: int, positions: list[int]) -> Plan:
    """Remove entries by 1-based position, as `segue show` numbers them."""
    state = _editable(store, playlist_id)
    bad = [p for p in positions if not 1 <= p <= len(state.entries)]
    if bad:
        raise ValueError(f"{state.name!r} has {len(state.entries)} tracks; no position {bad[0]}")
    zero_based = tuple(sorted({p - 1 for p in positions}))
    return f"remove {len(zero_based)} from {state.name!r}", [RemovePositions(playlist_id, zero_based)]


def plan_dedupe(store: Store, playlist_ids: list[int], level: str | None = None,
                keep: str | None = None) -> Plan:
    """Remove duplicates from each playlist, as defined by the duplicate settings."""
    level = level or dupes.level(store)
    keep = keep or dupes.keep(store)
    key = dupes.key_function(store, level)
    ops: list[Operation] = []
    names = []
    for pid in playlist_ids:
        state = _editable(store, pid)
        names.append(state.name)
        repeats = dupes.duplicate_positions(state.entries, key, keep)
        if repeats:
            ops.append(RemovePositions(pid, tuple(repeats)))
    return f"remove duplicates ({level}) from {_list(names)}", ops


def plan_merge(
    store: Store,
    source_ids: list[int],
    into_id: int | None = None,
    into_name: str | None = None,
    delete_sources: bool = False,
    level: str | None = None,
) -> Plan:
    """Merge playlists without duplicate tracks.

    Into an existing playlist (``into_id``): its tracks stay as they are and every source
    track it doesn't already have is appended, in source order. Into a new playlist
    (``into_name``): it is created with the sources' tracks, each track once. "Already has"
    follows the duplicate setting (or ``level``).
    """
    key = dupes.key_function(store, level or dupes.level(store))
    if (into_id is None) == (into_name is None):
        raise ValueError("merge needs exactly one target: an existing playlist or a new name")
    sources = [pid for pid in dict.fromkeys(source_ids) if pid != into_id]
    if not sources:
        raise ValueError("merge needs at least one playlist besides the target")
    states = {pid: _state(store, pid) for pid in sources}

    if into_id is not None:
        target = _editable(store, into_id)
        seen = {key(e) for e in target.entries}
        target_label = repr(target.name)
    else:
        seen = set()
        target_label = f"new playlist {into_name!r}"

    added: list[EntryState] = []
    for pid in sources:
        for entry in states[pid].entries:
            if key(entry) not in seen:
                seen.add(key(entry))
                added.append(entry)

    ops: list[Operation] = []
    if into_id is not None:
        if added:
            ops.append(AddEntries(into_id, tuple(added)))
    else:
        ops.append(CreatePlaylist(into_name, entries=tuple(added)))
    if delete_sources:
        if liked := [states[pid].name for pid in sources if states[pid].kind == "liked"]:
            raise ValueError(f"Liked songs can't be deleted ({_list(liked)}); merge without deleting")
        ops.extend(DeletePlaylist(pid) for pid in sources)
    names = _list([states[pid].name for pid in sources])
    return f"merge {names} into {target_label}", ops


# Merge by name

SURVIVORS = ("largest", "oldest")


def plan_merge_by_name(
    store: Store,
    playlist_ids: list[int],
    survivor: str = "largest",
    delete_sources: bool = True,
    level: str | None = None,
) -> tuple[str, list[Operation], list[tuple[str, list[str]]]]:
    """Merge each group of same-named playlists (per service) into one.

    The survivor is the playlist with the most tracks (fewest changes, so the most tracks keep
    their date added), or the one that has been in Segue's library longest. Returns ``(description, ops, groups)`` where groups
    lists each survivor's name and the names merged into it.
    """
    if survivor not in SURVIVORS:
        raise ValueError(f"survivor must be one of: {', '.join(SURVIVORS)}")
    ops: list[Operation] = []
    shown: list[tuple[str, list[str]]] = []
    for group in select.name_groups(store, playlist_ids):
        states = {pid: _state(store, pid) for pid in group}
        if survivor == "largest":
            keep = max(group, key=lambda pid: (len(states[pid].entries), -pid))
        else:
            keep = group[0]  # groups are oldest first
        others = [pid for pid in group if pid != keep]
        ops.extend(plan_merge(store, others, into_id=keep, delete_sources=delete_sources, level=level)[1])
        shown.append((states[keep].name, [states[pid].name for pid in others]))
    count = len(shown)
    return f"merge playlists with the same name ({count} group{'s' if count != 1 else ''})", ops, shown


# Bulk rename

CASES = ("lower", "upper", "title", "sentence")


@dataclass
class RenameRules:
    replace: list[tuple[str, str, bool]] = field(default_factory=list)  # (old, new, is_regex)
    trim: bool = False
    case: str | None = None
    strip_copies: bool = False
    prefix: str = ""
    suffix: str = ""

    def apply(self, name: str) -> str:
        for old, new, is_regex in self.replace:
            name = re.sub(old, new, name, flags=re.IGNORECASE) if is_regex else name.replace(old, new)
        if self.strip_copies:
            name = select.strip_copy_marker(name)
        if self.trim:
            name = " ".join(name.split())
        if self.case == "lower":
            name = name.lower()
        elif self.case == "upper":
            name = name.upper()
        elif self.case == "title":
            name = " ".join(w[:1].upper() + w[1:].lower() for w in name.split(" "))
        elif self.case == "sentence":
            name = name[:1].upper() + name[1:].lower()
        return f"{self.prefix}{name}{self.suffix}"


def plan_bulk_rename(store: Store, playlist_ids: list[int], rules: RenameRules) -> Plan:
    if rules.case and rules.case not in CASES:
        raise ValueError(f"case must be one of: {', '.join(CASES)}")
    ops: list[Operation] = []
    for pid in playlist_ids:
        state = _state(store, pid)
        if state.kind in ("followed", "liked"):
            continue
        try:
            new = rules.apply(state.name)
        except re.error as e:
            raise ValueError(f"bad regular expression: {e}") from None
        if new.strip() and new != state.name:
            ops.append(EditPlaylist(pid, name=new))
    return f"rename {len(ops)} playlist{'s' if len(ops) != 1 else ''}", ops


# Sort, shuffle

SORT_KEYS = ("title", "artist", "album", "duration", "added", "release")


def _sort_value(store: Store, entry: EntryState, key: str):
    if key == "added":
        return entry.added_at or None
    track = store.get_track(entry.track_id)
    if key == "title":
        return normalize(track.title)
    if key == "artist":
        return normalize(track.artists[0]) if track.artists else None
    if key == "album":
        return normalize(track.album) or None
    if key == "duration":
        return track.duration_ms
    if key == "release":
        return track.release_date or None
    raise ValueError(f"can't sort by {key!r}; choose from {', '.join(SORT_KEYS)}")


def sorted_order(store: Store, entries, keys: list[str], reverse: bool = False) -> list[int]:
    """Positions in sorted order. Stable; entries missing a value go last either way."""
    order = list(range(len(entries)))
    for key in reversed(keys):  # least significant key first; each pass is stable
        values = {i: _sort_value(store, entries[i], key) for i in order}
        present = [i for i in order if values[i] is not None]
        missing = [i for i in order if values[i] is None]
        present.sort(key=lambda i: values[i], reverse=reverse)
        order = present + missing
    return order


def _reorder_plan(store: Store, playlist_id: int, order: list[int], what: str, as_new: str | None) -> Plan:
    state = _state(store, playlist_id)
    if as_new:
        entries = tuple(state.entries[i] for i in order)
        return f"{what} {state.name!r} into new playlist {as_new!r}", [
            CreatePlaylist(as_new, state.description, entries)
        ]
    if state.kind == "liked":
        raise ValueError("Liked songs are ordered by date liked; save the result with --as-new NAME")
    _editable(store, playlist_id)
    ops = [] if order == list(range(len(order))) else [Reorder(playlist_id, tuple(order))]
    return f"{what} {state.name!r}", ops


def plan_sort(store: Store, playlist_id: int, keys: list[str], reverse: bool = False,
              as_new: str | None = None) -> Plan:
    for key in keys:
        if key not in SORT_KEYS:
            raise ValueError(f"can't sort by {key!r}; choose from {', '.join(SORT_KEYS)}")
    state = _state(store, playlist_id)
    order = sorted_order(store, state.entries, keys, reverse)
    return _reorder_plan(store, playlist_id, order, f"sort by {', '.join(keys)}{' (reversed)' if reverse else ''}",
                         as_new)


def plan_shuffle(store: Store, playlist_id: int, seed: int | None = None, as_new: str | None = None) -> Plan:
    state = _state(store, playlist_id)
    order = list(range(len(state.entries)))
    rng = random.Random(seed)
    for _ in range(10):  # avoid "shuffling" into the same order
        rng.shuffle(order)
        if order != sorted(order) or len(order) < 2:
            break
    return _reorder_plan(store, playlist_id, order, "shuffle", as_new)


# Split, clone

SPLIT_BY = ("artist", "album", "decade")


def plan_split(store: Store, playlist_id: int, every: int | None = None, by: str | None = None,
               min_size: int = 1, delete_original: bool = False) -> Plan:
    """Split a playlist into new ones: every N tracks, or by artist, album or decade.

    Groups smaller than ``min_size`` go together into one "other" playlist. Parts are named
    "<name> – part 1" or "<name> – <artist>", so merge-by-name never folds them back.
    """
    if (every is None) == (by is None):
        raise ValueError("split needs exactly one of: every N tracks, or by artist/album/decade")
    state = _state(store, playlist_id)
    if every is not None:
        if every < 1:
            raise ValueError("split every needs a positive number")
        chunks = [state.entries[i:i + every] for i in range(0, len(state.entries), every)]
        parts = [(f"{state.name} – part {n}", chunk) for n, chunk in enumerate(chunks, start=1)]
    else:
        if by not in SPLIT_BY:
            raise ValueError(f"split by must be one of: {', '.join(SPLIT_BY)}")
        groups: dict[str, list[EntryState]] = {}
        for entry in state.entries:
            groups.setdefault(_split_label(store, entry, by), []).append(entry)
        parts, other = [], []
        for label, entries in groups.items():
            if len(entries) >= min_size:
                parts.append((f"{state.name} – {label}", entries))
            else:
                other.extend(entries)
        if other:
            parts.append((f"{state.name} – other", other))
    if len(parts) < 2:
        raise ValueError(f"nothing to split: {state.name!r} would stay in one piece")
    ops: list[Operation] = [CreatePlaylist(name, state.description, tuple(entries)) for name, entries in parts]
    if delete_original:
        ops.append(DeletePlaylist(playlist_id))
    how = f"every {every} tracks" if every else f"by {by}"
    return f"split {state.name!r} {how} into {len(parts)} playlists", ops


def _split_label(store: Store, entry: EntryState, by: str) -> str:
    track = store.get_track(entry.track_id)
    if by == "artist":
        return track.artists[0] if track.artists else "unknown artist"
    if by == "album":
        return track.album or "unknown album"
    year = (track.release_date or "")[:4]
    return f"{int(year) // 10 * 10}s" if year.isdigit() else "unknown year"


def plan_clone(store: Store, playlist_id: int, name: str | None = None) -> Plan:
    state = _state(store, playlist_id)
    new_name = name or f"{state.name} (copy)"
    return f"copy {state.name!r} to {new_name!r}", [CreatePlaylist(new_name, state.description, state.entries)]


def _list(names: list[str]) -> str:
    quoted = [repr(n) for n in names]
    if len(quoted) <= 3:
        return ", ".join(quoted)
    return ", ".join(quoted[:3]) + f" and {len(quoted) - 3} more"
