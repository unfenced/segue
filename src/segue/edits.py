"""Planners: turn a user action into a description and a list of operations.

Planners only read the library. Nothing changes until the change log applies the result.
"""

from __future__ import annotations

from .model import EntryState, PlaylistState
from .ops import AddEntries, CreatePlaylist, DeletePlaylist, EditPlaylist, Operation, RemovePositions
from .store import Store

Plan = tuple[str, list[Operation]]


def _state(store: Store, playlist_id: int) -> PlaylistState:
    state = store.playlist_state(playlist_id)
    if state is None:
        raise KeyError(f"No playlist with id {playlist_id}")
    return state


def plan_delete(store: Store, playlist_ids: list[int]) -> Plan:
    names = [_state(store, pid).name for pid in playlist_ids]
    return f"delete {_list(names)}", [DeletePlaylist(pid) for pid in playlist_ids]


def plan_rename(store: Store, playlist_id: int, name: str) -> Plan:
    old = _state(store, playlist_id).name
    ops = [] if old == name else [EditPlaylist(playlist_id, name=name)]
    return f"rename {old!r} to {name!r}", ops


def plan_remove(store: Store, playlist_id: int, positions: list[int]) -> Plan:
    """Remove entries by 1-based position, as `segue show` numbers them."""
    state = _state(store, playlist_id)
    bad = [p for p in positions if not 1 <= p <= len(state.entries)]
    if bad:
        raise ValueError(f"{state.name!r} has {len(state.entries)} tracks; no position {bad[0]}")
    zero_based = tuple(sorted({p - 1 for p in positions}))
    return f"remove {len(zero_based)} from {state.name!r}", [RemovePositions(playlist_id, zero_based)]


def plan_dedupe(store: Store, playlist_ids: list[int]) -> Plan:
    """Remove repeated tracks from each playlist, keeping the first occurrence."""
    ops: list[Operation] = []
    names = []
    for pid in playlist_ids:
        state = _state(store, pid)
        names.append(state.name)
        seen: set[int] = set()
        repeats = []
        for position, entry in enumerate(state.entries):
            if entry.track_id in seen:
                repeats.append(position)
            seen.add(entry.track_id)
        if repeats:
            ops.append(RemovePositions(pid, tuple(repeats)))
    return f"remove duplicates from {_list(names)}", ops


def plan_merge(
    store: Store,
    source_ids: list[int],
    into_id: int | None = None,
    into_name: str | None = None,
    delete_sources: bool = False,
) -> Plan:
    """Merge playlists without duplicate tracks.

    Into an existing playlist (``into_id``): its tracks stay as they are and every source
    track it doesn't already have is appended, in source order. Into a new playlist
    (``into_name``): it is created with the sources' tracks, each track once.
    """
    if (into_id is None) == (into_name is None):
        raise ValueError("merge needs exactly one target: an existing playlist or a new name")
    sources = [pid for pid in dict.fromkeys(source_ids) if pid != into_id]
    if not sources:
        raise ValueError("merge needs at least one playlist besides the target")
    states = {pid: _state(store, pid) for pid in sources}

    if into_id is not None:
        target = _state(store, into_id)
        seen = set(target.track_ids)
        target_label = repr(target.name)
    else:
        seen = set()
        target_label = f"new playlist {into_name!r}"

    added: list[EntryState] = []
    for pid in sources:
        for entry in states[pid].entries:
            if entry.track_id not in seen:
                seen.add(entry.track_id)
                added.append(entry)

    ops: list[Operation] = []
    if into_id is not None:
        if added:
            ops.append(AddEntries(into_id, tuple(added)))
    else:
        ops.append(CreatePlaylist(into_name, entries=tuple(added)))
    if delete_sources:
        ops.extend(DeletePlaylist(pid) for pid in sources)
    names = _list([states[pid].name for pid in sources])
    return f"merge {names} into {target_label}", ops


def _list(names: list[str]) -> str:
    quoted = [repr(n) for n in names]
    if len(quoted) <= 3:
        return ", ".join(quoted)
    return ", ".join(quoted[:3]) + f" and {len(quoted) - 3} more"
