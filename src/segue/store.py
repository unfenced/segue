"""Local SQLite library: tracks, their IDs on each service, and playlists."""

from __future__ import annotations

import json
import os
import sqlite3
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path

from .model import (
    EntryState, Playlist, PlaylistEntry, PlaylistState, ServiceRef, Track, normalize, normalize_isrc,
)

SCHEMA_VERSION = 5

# Two durations further apart than this mean two different recordings.
DURATION_TOLERANCE_MS = 3000

_SCHEMA = """
CREATE TABLE tracks (
    id           INTEGER PRIMARY KEY,
    title        TEXT NOT NULL,
    artists      TEXT NOT NULL,          -- JSON list
    album        TEXT,
    duration_ms  INTEGER,
    isrc         TEXT,
    norm_title   TEXT NOT NULL,
    norm_artists TEXT NOT NULL,
    release_date TEXT
);
CREATE INDEX tracks_isrc ON tracks(isrc);
CREATE INDEX tracks_key ON tracks(norm_title, norm_artists);

CREATE TABLE track_refs (
    track_id   INTEGER NOT NULL REFERENCES tracks(id) ON DELETE CASCADE,
    service    TEXT NOT NULL,
    service_id TEXT NOT NULL,
    url        TEXT,
    PRIMARY KEY (service, service_id)
);
CREATE INDEX track_refs_track ON track_refs(track_id);

CREATE TABLE playlists (
    -- AUTOINCREMENT: IDs are never reused, so the change log can always tell playlists apart.
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    name           TEXT NOT NULL,
    description    TEXT,
    source_service TEXT,
    source_id      TEXT,
    source_url     TEXT,
    created_at     TEXT NOT NULL,
    updated_at     TEXT NOT NULL,
    revision       INTEGER NOT NULL DEFAULT 0,   -- bumped on every change
    kind           TEXT NOT NULL DEFAULT 'created', -- created | collaborative | followed | liked
    UNIQUE (source_service, source_id)
);

CREATE TABLE playlist_entries (
    playlist_id INTEGER NOT NULL REFERENCES playlists(id) ON DELETE CASCADE,
    position    INTEGER NOT NULL,
    track_id    INTEGER NOT NULL REFERENCES tracks(id),
    added_at    TEXT,
    -- The service ID this entry had where it came from. A track can have several IDs on
    -- one service (re-releases, regional copies); this keeps restores exact.
    ref_service TEXT,
    ref_id      TEXT,
    handle      TEXT,   -- the service's own ID for this entry, e.g. Qobuz playlist_track_id
    PRIMARY KEY (playlist_id, position)
);
CREATE INDEX playlist_entries_track ON playlist_entries(track_id);
"""

_V2_LOG_SCHEMA = """
CREATE TABLE settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- The change log. A batch is one user action (e.g. a merge); its operations are applied,
-- undone and redone together.
CREATE TABLE batches (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    description TEXT NOT NULL,
    status      TEXT NOT NULL,              -- pending | applied | undone | discarded
    expected    TEXT NOT NULL DEFAULT '{}', -- JSON {playlist_id: revision} when planned
    created_at  TEXT NOT NULL,
    applied_at  TEXT,
    undone_at   TEXT,
    -- Increases on every apply, undo and redo, so "the last change" is unambiguous.
    changed_seq INTEGER
);

CREATE TABLE operations (
    batch_id    INTEGER NOT NULL REFERENCES batches(id) ON DELETE CASCADE,
    seq         INTEGER NOT NULL,
    op          TEXT NOT NULL,   -- JSON of the planned operation
    playlist_id INTEGER,         -- known once applied (a create gets its ID then)
    before      TEXT,            -- JSON PlaylistState snapshots, NULL = no playlist
    after       TEXT,
    PRIMARY KEY (batch_id, seq)
);
"""

_BACKUP_SCHEMA = """
-- Backups written by Segue, and the revision of each playlist they contain, so Segue can
-- tell whether a playlist has changed since its last backup.
CREATE TABLE backups (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,
    paths      TEXT NOT NULL      -- JSON list of the files written
);

CREATE TABLE backup_playlists (
    backup_id   INTEGER NOT NULL REFERENCES backups(id) ON DELETE CASCADE,
    playlist_id INTEGER NOT NULL,
    revision    INTEGER NOT NULL,
    PRIMARY KEY (backup_id, playlist_id)
);
CREATE INDEX backup_playlists_playlist ON backup_playlists(playlist_id, revision);
"""

_LOG_SCHEMA = """
CREATE TABLE settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- The change log. A batch is one user action (e.g. a merge). It is applied to the library
-- as soon as it is made: "staged" in safe mode until committed, "committed" in auto mode.
CREATE TABLE batches (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    description TEXT NOT NULL,
    status      TEXT NOT NULL,   -- staged | committed | undone | discarded
    mode        TEXT NOT NULL,   -- safe | auto | import | pull
    created_at  TEXT NOT NULL,
    applied_at  TEXT,
    undone_at   TEXT,
    -- Increases on every apply, commit, undo and redo, so "the last change" is unambiguous.
    changed_seq INTEGER
);

CREATE TABLE operations (
    batch_id    INTEGER NOT NULL REFERENCES batches(id) ON DELETE CASCADE,
    seq         INTEGER NOT NULL,
    op          TEXT NOT NULL,   -- JSON of the operation
    playlist_id INTEGER,         -- known once applied (a create gets its ID then)
    before      TEXT,            -- JSON PlaylistState snapshots, NULL = no playlist
    after       TEXT,
    PRIMARY KEY (batch_id, seq)
);
""" + _BACKUP_SCHEMA

_REMOTE_SCHEMA = """
-- What each service last had for a playlist (read on pull, refreshed after each write).
-- A commit writes the difference between this and the playlist's current state.
CREATE TABLE remote_state (
    playlist_id INTEGER NOT NULL,
    service     TEXT NOT NULL,
    remote_id   TEXT NOT NULL,
    state       TEXT NOT NULL,   -- JSON PlaylistState
    read_at     TEXT NOT NULL,
    PRIMARY KEY (playlist_id, service)
);
"""

_SCHEMA += _LOG_SCHEMA + _REMOTE_SCHEMA

# Upgrades from each older schema version to the next.
_MIGRATIONS = {
    2: """
CREATE TABLE playlists_v2 (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    name           TEXT NOT NULL,
    description    TEXT,
    source_service TEXT,
    source_id      TEXT,
    source_url     TEXT,
    created_at     TEXT NOT NULL,
    updated_at     TEXT NOT NULL,
    revision       INTEGER NOT NULL DEFAULT 0,
    UNIQUE (source_service, source_id)
);
INSERT INTO playlists_v2 (id, name, description, source_service, source_id, source_url,
                          created_at, updated_at)
    SELECT id, name, description, source_service, source_id, source_url, created_at, updated_at
    FROM playlists;
DROP TABLE playlists;
ALTER TABLE playlists_v2 RENAME TO playlists;
""" + _V2_LOG_SCHEMA,
    3: """
ALTER TABLE playlists ADD COLUMN kind TEXT NOT NULL DEFAULT 'created';
ALTER TABLE batches ADD COLUMN mode TEXT NOT NULL DEFAULT 'auto';
ALTER TABLE batches DROP COLUMN expected;
UPDATE batches SET mode = 'import'
    WHERE id IN (SELECT batch_id FROM operations WHERE op LIKE '{"kind": "import"%');
UPDATE batches SET status = 'discarded' WHERE status = 'pending';
UPDATE batches SET status = 'committed' WHERE status = 'applied';
UPDATE settings SET value = 'safe' WHERE key = 'apply_mode' AND value = 'commit';
UPDATE settings SET value = 'auto' WHERE key = 'apply_mode' AND value NOT IN ('safe', 'auto');
""" + _BACKUP_SCHEMA,
    4: "ALTER TABLE tracks ADD COLUMN release_date TEXT;",
    5: "ALTER TABLE playlist_entries ADD COLUMN handle TEXT;" + _REMOTE_SCHEMA,
}


def default_db_path() -> Path:
    if env := os.environ.get("SEGUE_DB"):
        return Path(env)
    return Path.home() / ".segue" / "segue.db"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass
class PlaylistSummary:
    id: int
    name: str
    source: ServiceRef | None
    track_count: int
    kind: str = "created"


class Store:
    def __init__(self, path: str | Path = ":memory:"):
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self._depth = 0
        self._migrate()
        self.db.execute("PRAGMA foreign_keys = ON")
        if self.get_setting("library_id") is None:
            # Ties backups to this library, so a restore never matches another library's IDs.
            self.set_setting("library_id", uuid.uuid4().hex[:12])

    @property
    def library_id(self) -> str:
        return self.get_setting("library_id")

    def __enter__(self) -> Store:
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def close(self) -> None:
        self.db.close()

    def _migrate(self) -> None:
        # Runs with foreign keys off, so rebuilding a table doesn't cascade-delete its rows.
        version = self.db.execute("PRAGMA user_version").fetchone()[0]
        if version > SCHEMA_VERSION:
            raise RuntimeError(
                f"Library was created by a newer Segue (schema {version}); please upgrade."
            )
        if version == SCHEMA_VERSION:
            return
        steps = [_SCHEMA] if version == 0 else [_MIGRATIONS[v] for v in range(version + 1, SCHEMA_VERSION + 1)]
        script = "".join(steps)
        self.db.executescript(f"BEGIN;\n{script}\nPRAGMA user_version = {SCHEMA_VERSION};\nCOMMIT;")

    @contextmanager
    def transaction(self) -> Iterator[None]:
        """Group writes; nested calls join the outermost transaction."""
        if self._depth:
            self._depth += 1
            try:
                yield
            finally:
                self._depth -= 1
            return
        self._depth = 1
        try:
            with self.db:
                yield
        finally:
            self._depth = 0

    # Tracks

    def resolve_track(self, track: Track) -> int:
        """Find the library track this one refers to, or add it. Sets and returns ``track.id``.

        Matching, most reliable first: a shared service ID, then ISRC, then exact
        normalized title + artists with compatible durations. Whatever the incoming track
        knows that the stored one doesn't (ISRC, album, new service IDs) is merged in.
        """
        track.isrc = normalize_isrc(track.isrc)
        existing = self._find_track(track)
        if existing is None:
            title, artists = track.match_key()
            cur = self.db.execute(
                "INSERT INTO tracks (title, artists, album, duration_ms, isrc, norm_title, norm_artists,"
                " release_date) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (track.title, json.dumps(track.artists), track.album, track.duration_ms,
                 track.isrc, title, artists, track.release_date),
            )
            track_id = cur.lastrowid
        else:
            track_id = existing
            self.db.execute(
                "UPDATE tracks SET isrc = COALESCE(isrc, ?), album = COALESCE(album, ?),"
                " duration_ms = COALESCE(duration_ms, ?), release_date = COALESCE(release_date, ?)"
                " WHERE id = ?",
                (track.isrc, track.album, track.duration_ms, track.release_date, track_id),
            )
        for ref in track.refs:
            self.db.execute(
                "INSERT INTO track_refs (track_id, service, service_id, url) VALUES (?, ?, ?, ?)"
                " ON CONFLICT (service, service_id) DO UPDATE SET url = COALESCE(url, excluded.url)",
                (track_id, ref.service, ref.id, ref.url),
            )
        track.id = track_id
        return track_id

    def _find_track(self, track: Track) -> int | None:
        for ref in track.refs:
            row = self.db.execute(
                "SELECT track_id FROM track_refs WHERE service = ? AND service_id = ?",
                (ref.service, ref.id),
            ).fetchone()
            if row:
                return row[0]
        if track.isrc:
            row = self.db.execute("SELECT id FROM tracks WHERE isrc = ?", (track.isrc,)).fetchone()
            if row:
                return row[0]
        title, artists = track.match_key()
        if not title:
            return None
        for row in self.db.execute(
            "SELECT id, duration_ms, isrc FROM tracks WHERE norm_title = ? AND norm_artists = ?",
            (title, artists),
        ):
            if track.isrc and row["isrc"] and row["isrc"] != track.isrc:
                continue
            if (
                track.duration_ms is not None
                and row["duration_ms"] is not None
                and abs(track.duration_ms - row["duration_ms"]) > DURATION_TOLERANCE_MS
            ):
                continue
            return row["id"]
        return None

    def get_track(self, track_id: int) -> Track:
        return self._load_tracks([track_id])[track_id]

    def _load_tracks(self, ids: list[int]) -> dict[int, Track]:
        tracks: dict[int, Track] = {}
        unique = list(dict.fromkeys(ids))
        # Chunk to stay under SQLite's bound-parameter limit.
        for start in range(0, len(unique), 500):
            chunk = unique[start:start + 500]
            marks = ",".join("?" * len(chunk))
            for row in self.db.execute(f"SELECT * FROM tracks WHERE id IN ({marks})", chunk):
                tracks[row["id"]] = Track(
                    id=row["id"], title=row["title"], artists=json.loads(row["artists"]),
                    album=row["album"], duration_ms=row["duration_ms"], isrc=row["isrc"],
                    release_date=row["release_date"],
                )
            for row in self.db.execute(
                f"SELECT * FROM track_refs WHERE track_id IN ({marks}) ORDER BY service", chunk
            ):
                tracks[row["track_id"]].refs.append(
                    ServiceRef(row["service"], row["service_id"], row["url"])
                )
        return tracks

    def track_count(self) -> int:
        return self.db.execute("SELECT COUNT(*) FROM tracks").fetchone()[0]

    # Playlists

    def save_playlist(self, playlist: Playlist) -> tuple[int, str]:
        """Store a playlist and its tracks in one transaction.

        Returns ``(id, status)`` where status is one of:

        - ``"replaced"``: a stored playlist has the same ``source``; it keeps its library ID
          and gets the new name and contents.
        - ``"unchanged"``: no source, but a stored playlist with the same name and the same
          tracks in the same order exists (e.g. the same backup imported twice).
        - ``"added"``: anything else.
        """
        with self.transaction():
            track_ids = [self.resolve_track(entry.track) for entry in playlist.entries]
            existing = None
            if playlist.source:
                existing = self.db.execute(
                    "SELECT id FROM playlists WHERE source_service = ? AND source_id = ?",
                    (playlist.source.service, playlist.source.id),
                ).fetchone()
            elif (twin := self._identical_playlist(playlist.name, track_ids)) is not None:
                playlist.id = twin
                return twin, "unchanged"
            now = _now()
            if existing:
                playlist_id = existing[0]
                self.db.execute(
                    "UPDATE playlists SET name = ?, description = ?, source_url = ?, kind = ?,"
                    " updated_at = ?, revision = revision + 1 WHERE id = ?",
                    (playlist.name, playlist.description, playlist.source.url, playlist.kind,
                     now, playlist_id),
                )
                self.db.execute("DELETE FROM playlist_entries WHERE playlist_id = ?", (playlist_id,))
            else:
                src = playlist.source
                cur = self.db.execute(
                    "INSERT INTO playlists (name, description, source_service, source_id, source_url,"
                    " kind, created_at, updated_at, revision) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1)",
                    (playlist.name, playlist.description, src and src.service, src and src.id,
                     src and src.url, playlist.kind, now, now),
                )
                playlist_id = cur.lastrowid
            self._insert_entries(playlist_id, [
                EntryState(track_id, entry.added_at, *_entry_ref(entry, playlist.source), entry.handle)
                for track_id, entry in zip(track_ids, playlist.entries)
            ])
        playlist.id = playlist_id
        return playlist_id, "replaced" if existing else "added"

    def _identical_playlist(self, name: str, track_ids: list[int]) -> int | None:
        for row in self.db.execute(
            "SELECT id, name FROM playlists WHERE source_service IS NULL"
        ).fetchall():
            if row["name"] != name:
                continue
            stored = [
                r[0] for r in self.db.execute(
                    "SELECT track_id FROM playlist_entries WHERE playlist_id = ? ORDER BY position",
                    (row["id"],),
                )
            ]
            if stored == track_ids:
                return row["id"]
        return None

    def playlist_summaries(self) -> list[PlaylistSummary]:
        rows = self.db.execute(
            "SELECT p.*, COUNT(e.position) AS n FROM playlists p"
            " LEFT JOIN playlist_entries e ON e.playlist_id = p.id"
            " GROUP BY p.id ORDER BY p.name COLLATE NOCASE, p.id"
        )
        return [
            PlaylistSummary(row["id"], row["name"], self._source(row), row["n"], row["kind"])
            for row in rows
        ]

    def get_playlist(self, playlist_id: int) -> Playlist:
        row = self.db.execute("SELECT * FROM playlists WHERE id = ?", (playlist_id,)).fetchone()
        if row is None:
            raise KeyError(f"No playlist with id {playlist_id}")
        entries = self.db.execute(
            "SELECT track_id, added_at, ref_service, ref_id, handle FROM playlist_entries"
            " WHERE playlist_id = ? ORDER BY position",
            (playlist_id,),
        ).fetchall()
        tracks = self._load_tracks([e["track_id"] for e in entries])
        return Playlist(
            id=row["id"], name=row["name"], description=row["description"],
            source=self._source(row), kind=row["kind"],
            entries=[PlaylistEntry(_with_ref_first(tracks[e["track_id"]], e), e["added_at"], e["handle"])
                     for e in entries],
        )

    def find_playlists(self, name: str) -> list[Playlist]:
        """Playlists whose normalized name equals ``name``'s."""
        wanted = normalize(name)
        return [
            self.get_playlist(s.id) for s in self.playlist_summaries() if normalize(s.name) == wanted
        ]

    def all_playlists(self) -> list[Playlist]:
        return [self.get_playlist(s.id) for s in self.playlist_summaries()]

    def delete_playlist(self, playlist_id: int) -> None:
        with self.transaction():
            self.db.execute("DELETE FROM playlists WHERE id = ?", (playlist_id,))

    def find_by_source(self, source: ServiceRef) -> int | None:
        row = self.db.execute(
            "SELECT id FROM playlists WHERE source_service = ? AND source_id = ?",
            (source.service, source.id),
        ).fetchone()
        return row[0] if row else None

    # Playlist states, used by the change log

    def playlist_state(self, playlist_id: int) -> PlaylistState | None:
        row = self.db.execute("SELECT * FROM playlists WHERE id = ?", (playlist_id,)).fetchone()
        if row is None:
            return None
        entries = self.db.execute(
            "SELECT track_id, added_at, ref_service, ref_id, handle FROM playlist_entries"
            " WHERE playlist_id = ? ORDER BY position",
            (playlist_id,),
        )
        return PlaylistState(
            name=row["name"], description=row["description"], source=self._source(row),
            entries=tuple(EntryState(*e) for e in entries), kind=row["kind"],
        )

    def to_state(self, playlist: Playlist) -> PlaylistState:
        """The stored form of a playlist read from a file, adding any new tracks to the library."""
        with self.transaction():
            entries = tuple(
                EntryState(self.resolve_track(e.track), e.added_at, *_entry_ref(e, playlist.source), e.handle)
                for e in playlist.entries
            )
        return PlaylistState(playlist.name, playlist.description, playlist.source, entries, playlist.kind)

    def revision(self, playlist_id: int) -> int | None:
        row = self.db.execute("SELECT revision FROM playlists WHERE id = ?", (playlist_id,)).fetchone()
        return row[0] if row else None

    def create_state(self, state: PlaylistState) -> int:
        """Add a new playlist with this state and return its ID."""
        with self.transaction():
            now = _now()
            src = state.source
            cur = self.db.execute(
                "INSERT INTO playlists (name, description, source_service, source_id, source_url,"
                " kind, created_at, updated_at, revision) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1)",
                (state.name, state.description, src and src.service, src and src.id,
                 src and src.url, state.kind, now, now),
            )
            self._insert_entries(cur.lastrowid, state.entries)
        return cur.lastrowid

    def write_state(self, playlist_id: int, state: PlaylistState | None) -> None:
        """Make playlist ``playlist_id`` look exactly like ``state``.

        ``None`` deletes it; a state for a missing ID recreates it under that same ID
        (this is how undoing a delete brings a playlist back).
        """
        with self.transaction():
            if state is None:
                self.db.execute("DELETE FROM playlists WHERE id = ?", (playlist_id,))
                return
            now = _now()
            src = state.source
            values = (state.name, state.description, src and src.service, src and src.id,
                      src and src.url, state.kind)
            if self.revision(playlist_id) is None:
                self.db.execute(
                    "INSERT INTO playlists (id, name, description, source_service, source_id,"
                    " source_url, kind, created_at, updated_at, revision)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 1)",
                    (playlist_id, *values, now, now),
                )
            else:
                self.db.execute(
                    "UPDATE playlists SET name = ?, description = ?, source_service = ?, source_id = ?,"
                    " source_url = ?, kind = ?, updated_at = ?, revision = revision + 1 WHERE id = ?",
                    (*values, now, playlist_id),
                )
                self.db.execute("DELETE FROM playlist_entries WHERE playlist_id = ?", (playlist_id,))
            self._insert_entries(playlist_id, state.entries)

    def _insert_entries(self, playlist_id: int, entries) -> None:
        self.db.executemany(
            "INSERT INTO playlist_entries"
            " (playlist_id, position, track_id, added_at, ref_service, ref_id, handle)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            [
                (playlist_id, position, e.track_id, e.added_at, e.ref_service, e.ref_id, e.handle)
                for position, e in enumerate(entries)
            ],
        )

    # What services last had

    def remote_links(self, playlist_id: int | None = None) -> list[tuple[int, str, str, PlaylistState]]:
        """``(playlist_id, service, remote_id, state)`` for one playlist, or for all."""
        query = "SELECT playlist_id, service, remote_id, state FROM remote_state"
        params: tuple = ()
        if playlist_id is not None:
            query += " WHERE playlist_id = ?"
            params = (playlist_id,)
        return [
            (row[0], row[1], row[2], PlaylistState.from_dict(json.loads(row[3])))
            for row in self.db.execute(query + " ORDER BY playlist_id, service", params)
        ]

    def set_remote(self, playlist_id: int, service: str, remote_id: str, state: PlaylistState) -> None:
        with self.transaction():
            self.db.execute(
                "INSERT INTO remote_state (playlist_id, service, remote_id, state, read_at) VALUES (?, ?, ?, ?, ?)"
                " ON CONFLICT (playlist_id, service) DO UPDATE SET remote_id = excluded.remote_id,"
                " state = excluded.state, read_at = excluded.read_at",
                (playlist_id, service, remote_id, json.dumps(state.to_dict()), _now()),
            )

    def remove_remote(self, playlist_id: int, service: str) -> None:
        with self.transaction():
            self.db.execute("DELETE FROM remote_state WHERE playlist_id = ? AND service = ?", (playlist_id, service))

    # Settings

    def get_setting(self, key: str, default: str | None = None) -> str | None:
        row = self.db.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
        return row[0] if row else default

    def set_setting(self, key: str, value: str) -> None:
        with self.transaction():
            self.db.execute(
                "INSERT INTO settings (key, value) VALUES (?, ?)"
                " ON CONFLICT (key) DO UPDATE SET value = excluded.value",
                (key, value),
            )

    @staticmethod
    def _source(row: sqlite3.Row) -> ServiceRef | None:
        if row["source_service"] is None:
            return None
        return ServiceRef(row["source_service"], row["source_id"], row["source_url"])


def _entry_ref(entry: PlaylistEntry, source: ServiceRef | None) -> tuple[str | None, str | None]:
    """The ID an entry had on its source: the playlist's service if known, else its first ref."""
    refs = entry.track.refs
    ref = (source and entry.track.ref(source.service)) or (refs[0] if refs else None)
    return (ref.service, ref.id) if ref else (None, None)


def _with_ref_first(track: Track, entry: sqlite3.Row) -> Track:
    """A copy of ``track`` whose refs start with the entry's own service ID."""
    if entry["ref_service"] is None:
        return track
    own = [r for r in track.refs if (r.service, r.id) == (entry["ref_service"], entry["ref_id"])]
    if not own:
        return track
    return replace(track, refs=own + [r for r in track.refs if r is not own[0]])
