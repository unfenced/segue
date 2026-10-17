"""Planned operations: small, serializable changes to one playlist each.

An operation is a pure function from a playlist's state to its new state. Planners
(merge, dedupe, ...) produce lists of them; the change log (``changes``) applies them,
snapshots the states before and after, and uses those snapshots to undo and redo.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, replace
from typing import ClassVar

from .model import EntryState, PlaylistState

# Formats a track ID for previews, e.g. "The Beatles - Hey Jude".
Describer = Callable[[int], str]

_PREVIEW_TITLES = 5


class Operation:
    kind: ClassVar[str]
    _registry: ClassVar[dict[str, type[Operation]]] = {}

    def __init_subclass__(cls, **kw):
        super().__init_subclass__(**kw)
        Operation._registry[cls.kind] = cls

    @property
    def target(self) -> int | None:
        """The playlist this acts on; None for a playlist that doesn't exist yet."""
        return getattr(self, "playlist_id", None)

    def apply(self, state: PlaylistState | None) -> PlaylistState | None:
        raise NotImplementedError

    def describe(self, state: PlaylistState | None, track: Describer) -> str:
        raise NotImplementedError

    def to_dict(self) -> dict:
        data = {"kind": self.kind}
        for name, value in vars(self).items():
            if name == "entries":
                value = [e.to_dict() for e in value]
            elif isinstance(value, tuple):
                value = list(value)
            data[name] = value
        return data

    @staticmethod
    def from_dict(data: dict) -> Operation:
        data = dict(data)
        cls = Operation._registry[data.pop("kind")]
        if "entries" in data:
            data["entries"] = tuple(EntryState.from_dict(e) for e in data["entries"])
        for name in ("positions", "order"):
            if name in data:
                data[name] = tuple(data[name])
        return cls(**data)


def _require(state: PlaylistState | None, op: Operation) -> PlaylistState:
    if state is None:
        raise ValueError(f"{op.kind}: playlist {op.target} doesn't exist")
    return state


def _titles(entries, track: Describer) -> str:
    names = [track(e.track_id) for e in entries[:_PREVIEW_TITLES]]
    more = len(entries) - len(names)
    return "; ".join(names) + (f"; and {more} more" if more > 0 else "")


def _tracks(n: int) -> str:
    return f"{n} track" if n == 1 else f"{n} tracks"


@dataclass(frozen=True)
class CreatePlaylist(Operation):
    kind: ClassVar[str] = "create"
    name: str
    description: str | None = None
    entries: tuple[EntryState, ...] = ()

    @property
    def target(self) -> None:
        return None

    def apply(self, state):
        if state is not None:
            raise ValueError("create: playlist already exists")
        return PlaylistState(self.name, self.description, None, self.entries)

    def describe(self, state, track):
        return f"create {self.name!r} with {_tracks(len(self.entries))}"


@dataclass(frozen=True)
class DeletePlaylist(Operation):
    kind: ClassVar[str] = "delete"
    playlist_id: int

    def apply(self, state):
        _require(state, self)
        return None

    def describe(self, state, track):
        if state is None:
            return f"delete playlist {self.playlist_id} (already gone)"
        return f"delete {state.name!r} ({_tracks(len(state.entries))})"


@dataclass(frozen=True)
class EditPlaylist(Operation):
    kind: ClassVar[str] = "edit"
    playlist_id: int
    name: str | None = None
    description: str | None = None

    def apply(self, state):
        state = _require(state, self)
        return replace(
            state,
            name=state.name if self.name is None else self.name,
            description=state.description if self.description is None else self.description,
        )

    def describe(self, state, track):
        old = state.name if state else f"playlist {self.playlist_id}"
        parts = []
        if self.name is not None:
            parts.append(f"rename {old!r} to {self.name!r}")
        if self.description is not None:
            parts.append(f"set the description of {old!r}")
        return " and ".join(parts) or f"leave {old!r} unchanged"


@dataclass(frozen=True)
class AddEntries(Operation):
    kind: ClassVar[str] = "add"
    playlist_id: int
    entries: tuple[EntryState, ...]
    position: int | None = None  # None appends

    def apply(self, state):
        state = _require(state, self)
        current = list(state.entries)
        at = len(current) if self.position is None else max(0, min(self.position, len(current)))
        return replace(state, entries=tuple(current[:at] + list(self.entries) + current[at:]))

    def describe(self, state, track):
        name = state.name if state else f"playlist {self.playlist_id}"
        return f"add {_tracks(len(self.entries))} to {name!r}: {_titles(self.entries, track)}"


@dataclass(frozen=True)
class RemovePositions(Operation):
    kind: ClassVar[str] = "remove"
    playlist_id: int
    positions: tuple[int, ...]  # 0-based, in the state the batch was planned on

    def apply(self, state):
        state = _require(state, self)
        drop = set(self.positions)
        if any(p < 0 or p >= len(state.entries) for p in drop):
            raise ValueError(f"remove: position out of range for {state.name!r}")
        return replace(state, entries=tuple(e for i, e in enumerate(state.entries) if i not in drop))

    def describe(self, state, track):
        if state is None:
            return f"remove {_tracks(len(self.positions))} from playlist {self.playlist_id}"
        removed = [state.entries[p] for p in sorted(self.positions) if p < len(state.entries)]
        return f"remove {_tracks(len(removed))} from {state.name!r}: {_titles(removed, track)}"


@dataclass(frozen=True)
class Reorder(Operation):
    kind: ClassVar[str] = "reorder"
    playlist_id: int
    order: tuple[int, ...]  # current positions, listed in their new order

    def apply(self, state):
        state = _require(state, self)
        if sorted(self.order) != list(range(len(state.entries))):
            raise ValueError(f"reorder: not a full reordering of {state.name!r}")
        return replace(state, entries=tuple(state.entries[i] for i in self.order))

    def describe(self, state, track):
        moved = sum(1 for new, old in enumerate(self.order) if new != old)
        name = state.name if state else f"playlist {self.playlist_id}"
        return f"reorder {name!r} ({_tracks(moved)} move)"
