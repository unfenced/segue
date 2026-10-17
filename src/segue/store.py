"""Local SQLite library: tracks, their IDs on each service, and playlists."""

from __future__ import annotations

import json
import os
import sqlite3
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path

from .model import Playlist, PlaylistEntry, ServiceRef, Track, normalize, normalize_isrc

SCHEMA_VERSION = 1

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
    norm_artists TEXT NOT NULL
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
    id             INTEGER PRIMARY KEY,
    name           TEXT NOT NULL,
    description    TEXT,
    source_service TEXT,
    source_id      TEXT,
    source_url     TEXT,
    created_at     TEXT NOT NULL,
    updated_at     TEXT NOT NULL,
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
    PRIMARY KEY (playlist_id, position)
);
CREATE INDEX playlist_entries_track ON playlist_entries(track_id);
"""


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


class Store:
    def __init__(self, path: str | Path = ":memory:"):
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA foreign_keys = ON")
        self._migrate()

    def __enter__(self) -> Store:
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def close(self) -> None:
        self.db.close()

    def _migrate(self) -> None:
        version = self.db.execute("PRAGMA user_version").fetchone()[0]
        if version == 0:
            with self.db:
                self.db.executescript(_SCHEMA)
                self.db.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        elif version > SCHEMA_VERSION:
            raise RuntimeError(
                f"Library was created by a newer Segue (schema {version}); please upgrade."
            )

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
                "INSERT INTO tracks (title, artists, album, duration_ms, isrc, norm_title, norm_artists)"
                " VALUES (?, ?, ?, ?, ?, ?, ?)",
                (track.title, json.dumps(track.artists), track.album, track.duration_ms,
                 track.isrc, title, artists),
            )
            track_id = cur.lastrowid
        else:
            track_id = existing
            self.db.execute(
                "UPDATE tracks SET isrc = COALESCE(isrc, ?), album = COALESCE(album, ?),"
                " duration_ms = COALESCE(duration_ms, ?) WHERE id = ?",
                (track.isrc, track.album, track.duration_ms, track_id),
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
        with self.db:
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
                    "UPDATE playlists SET name = ?, description = ?, source_url = ?, updated_at = ?"
                    " WHERE id = ?",
                    (playlist.name, playlist.description, playlist.source.url, now, playlist_id),
                )
                self.db.execute("DELETE FROM playlist_entries WHERE playlist_id = ?", (playlist_id,))
            else:
                src = playlist.source
                cur = self.db.execute(
                    "INSERT INTO playlists (name, description, source_service, source_id, source_url,"
                    " created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (playlist.name, playlist.description, src and src.service, src and src.id,
                     src and src.url, now, now),
                )
                playlist_id = cur.lastrowid
            self.db.executemany(
                "INSERT INTO playlist_entries"
                " (playlist_id, position, track_id, added_at, ref_service, ref_id)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                [
                    (playlist_id, position, track_id, entry.added_at,
                     *_entry_ref(entry, playlist.source))
                    for position, (track_id, entry) in enumerate(zip(track_ids, playlist.entries))
                ],
            )
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
            PlaylistSummary(row["id"], row["name"], self._source(row), row["n"]) for row in rows
        ]

    def get_playlist(self, playlist_id: int) -> Playlist:
        row = self.db.execute("SELECT * FROM playlists WHERE id = ?", (playlist_id,)).fetchone()
        if row is None:
            raise KeyError(f"No playlist with id {playlist_id}")
        entries = self.db.execute(
            "SELECT track_id, added_at, ref_service, ref_id FROM playlist_entries"
            " WHERE playlist_id = ? ORDER BY position",
            (playlist_id,),
        ).fetchall()
        tracks = self._load_tracks([e["track_id"] for e in entries])
        return Playlist(
            id=row["id"], name=row["name"], description=row["description"],
            source=self._source(row),
            entries=[PlaylistEntry(_with_ref_first(tracks[e["track_id"]], e), e["added_at"]) for e in entries],
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
        with self.db:
            self.db.execute("DELETE FROM playlists WHERE id = ?", (playlist_id,))

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
