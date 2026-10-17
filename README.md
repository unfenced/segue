# Segue

One place to manage playlists across music streaming services: connect accounts,
see every playlist side by side, clean them up, merge them, and keep them in sync —
so switching away from Spotify (or any other service) takes minutes, not weekends.

## Status

Step 1 is done: the neutral track library, SQLite store, and JSON/CSV/M3U import and export
(see the build order below). There are no service connectors yet. You can already import
playlist exports from other tools, such as [Exportify](https://exportify.net) CSVs for Spotify.

```bash
uv run segue import "Liked Songs.csv" road-trip.m3u8 backup.json
uv run segue list
uv run segue show "road trip"
uv run segue export everything.csv              # or .json; a folder name → one .m3u8 per playlist
uv run segue export chill.m3u8 -p Chill --prefer spotify
uv run segue backup                             # timestamped JSON + CSV next to the library
uv run pytest
```

The library lives at `~/.segue/segue.db`; override it with `--db` or `SEGUE_DB`.

| Format | Use it for | Keeps |
|---|---|---|
| JSON | Backups and restores | Everything: all service IDs per track, ISRC, added dates, playlist source |
| CSV | Spreadsheets, and imports from other tools | One ID per service per row; reads Exportify-style and semicolon (Dutch Excel) files |
| M3U8 | Media players | Title, artists, length, album and one link or file path; tracks without either are left out |

Importing is safe to repeat. A playlist with a service ID replaces its earlier copy, and an
identical playlist without one is skipped instead of duplicated.

## Goals

1. **Connect** several services at once.
2. **Edit** playlists in one interface: reorder, rename, remove, bulk-select.
3. **Merge** playlists, within one service or across services, with duplicate removal.
4. **Clean up** the library: duplicates, dead or unavailable tracks, empty or near-identical
   playlists, inconsistent names, bulk deletes.
5. **Sync** playlists across services, one-way (mirror) or two-way — individually or by rule.
6. **Never lose anything**: mandatory backup before the first write, a review step, and undo.

## Safety model

Playlists are personal and often irreplaceable, so every write goes through the same pipeline.

### 1. Backup first

Before Segue makes its first change to a connected service, it requires a full export of
that service's playlists (CSV, plus JSON with every service ID). This step can't be skipped.
After that, Segue takes a fresh snapshot before each batch of changes.

### 2. Apply mode: auto or commit

Every action (merge, delete, sync, rename) is turned into a list of **planned operations**
first. The user chooses how those are applied:

- **Commit mode (default):** operations wait in a pending queue. The user reviews the diff
  ("+14 tracks to *Road trip* on Spotify, delete 3 playlists") and commits all of it, part
  of it, or none.
- **Auto mode:** operations are applied right away, but still logged and undoable.

### 3. Undo

Every applied operation is written to an **operation log** together with its inverse:

| Operation | Inverse |
|---|---|
| Add tracks | Remove those tracks |
| Remove tracks | Re-add them at their original positions |
| Reorder | Restore the previous order |
| Rename / edit description | Restore the previous values |
| Delete playlist | Recreate it from the snapshot |

Undo works per operation or per batch. One limit is unavoidable: a deleted playlist that is
recreated gets a **new ID**, so its followers, share link and collaborators are lost. Segue
warns about this before a delete.

## Batch tools

Doing things one playlist at a time is what makes every service painful. Segue works on
whole selections and on **rules**:

- **Merge by name:** "merge all playlists with the same name" on one service (or across
  all of them). Tracks are deduplicated through the neutral track model.
- **Sync by name:** one rule — "keep every playlist whose name matches on Spotify and Tidal
  in sync" — instead of setting up each sync by hand. New playlists that match get picked up
  automatically.
- **Bulk delete / rename:** select by filter (empty, under N tracks, name pattern,
  not touched since a date, near-duplicate of another playlist).

Name matching is normalized (case, whitespace, emoji, trailing "(1)" / "copy"), and the
matched groups are shown for review before anything happens.

## Core idea: a neutral track model

Every service uses its own track IDs. Segue keeps its own library of tracks and
matches each one to its ID on every connected service:

1. **ISRC** (the standard recording code) when the service exposes it — the most reliable.
2. Otherwise **fuzzy matching** on title, artists, album and duration, with a confidence score.
3. Low-confidence matches go to a **review queue** instead of being guessed silently.

Playlists are stored as ordered lists of neutral tracks, so merge, dedupe and sync work
the same regardless of service.

## Services: what is realistic (check before building each connector)

| Service | Access | Notes |
|---|---|---|
| Spotify | Official Web API (OAuth) | New apps start in development mode with a small user allowlist; fine for a bring-your-own-key tool |
| Apple Music | Official MusicKit / Apple Music API | Needs a paid Apple Developer account for the developer token |
| YouTube Music | No official API; community `ytmusicapi` | Works, but can break when YouTube changes things |
| Tidal | Official developer API | Check current playlist write access |
| Deezer | Official API | Check whether new app registrations are open |
| Local files | M3U / XSPF / CSV import & export | Always available; a good first "service" for testing |

## Proposed architecture

- **Connectors**: one module per service implementing the same interface
  (`list_playlists`, `get_tracks`, `search`, `create_playlist`, `delete_playlist`,
  `add_tracks`, `remove_tracks`, `reorder`, `rename`).
- **Library store**: local SQLite with tracks, service IDs, playlists, snapshots,
  sync rules and the operation log.
- **Engine**: matching, dedupe, merge, diff and sync planning. Produces planned operations;
  never writes to a service directly.
- **Executor**: applies planned operations (commit or auto mode), records each one with its
  inverse, and handles rate limits and partial failures (a failed batch can be resumed or
  rolled back).
- **Interface**: start with a CLI, then a local web UI.

## Build order (working plan)

1. ✅ Neutral model + SQLite store + CSV/JSON/M3U import/export (this is also the backup).
2. Planned operations, operation log and undo — tested against the local-files "service".
3. Spotify connector (read, then write) behind the mandatory backup gate.
4. Cleanup and batch tools: duplicates, merge by name, bulk delete, with previews.
5. Second connector (YouTube Music or Apple Music) + cross-service matching.
6. Sync, including sync-by-name rules, with dry-run.
7. Local web UI with the pending-changes queue and undo button.

## Prior art to learn from

Soundiiz, TuneMyMusic, SongShift and Free Your Music do transfers as hosted (mostly paid)
services with few organizing tools. Segue aims to be open source, local-first and focused on
ongoing library cleanup and sync rather than one-off transfers.
