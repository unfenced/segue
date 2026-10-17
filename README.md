# Segue

One place to manage playlists across music streaming services: connect accounts,
see every playlist side by side, clean them up, merge them, and keep them in sync —
so switching away from Spotify (or any other service) takes minutes, not weekends.

## Status

Steps 1 and 2 are done (see the build order below):

- the neutral track library, the SQLite store, and JSON/CSV/M3U import and export;
- the change log, with commit or auto mode, undo and redo.

There are no service connectors yet, so changes apply to Segue's own library. You can
already import playlist exports from other tools, such as [Exportify](https://exportify.net)
CSVs for Spotify, and clean them up.

```bash
uv run segue import "Liked Songs.csv" road-trip.m3u8 backup.json
uv run segue list
uv run segue show "road trip"
uv run segue export everything.csv              # or .json; a folder name → one .m3u8 per playlist
uv run segue export chill.m3u8 -p Chill --prefer spotify
uv run segue backup                             # timestamped JSON + CSV next to the library

uv run segue dedupe "road trip"                 # plans a change: queued in commit mode
uv run segue merge Chill "Chill 2" --into Chill --delete-sources
uv run segue merge A B C --new "Everything" --dry-run
uv run segue rename 3 "Gym"
uv run segue delete Old
uv run segue remove Gym 4 7                     # positions as shown by: segue show Gym
uv run segue pending                            # review what's queued
uv run segue commit                             # apply it (or: segue discard)
uv run segue history
uv run segue undo                               # last change; or: segue undo 12
uv run segue redo
uv run segue config apply-mode auto             # or commit (default); --now / --queue per command
uv run pytest
```

The library lives at `~/.segue/segue.db`; override it with `--db` or `SEGUE_DB`.

| Format | Use it for | Keeps |
|---|---|---|
| JSON | Backups and restores | Everything: all service IDs per track, ISRC, added dates, playlist source |
| CSV | Spreadsheets, and imports from other tools | One ID per service per row; reads Exportify-style and semicolon (Dutch Excel) files |
| M3U8 | Media players | Title, artists, length, album and one link or file path; tracks without either are left out |

Importing is safe to repeat. A playlist with a service ID replaces its earlier copy, and an
identical playlist is skipped instead of duplicated. Imports only touch Segue's own library,
so they apply right away in either mode, but they are logged: an import that replaced a
playlist can be undone.

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

Each user action becomes one **batch** of operations (create, delete, edit, add, remove,
reorder). In commit mode, a batch records the revision of every playlist it touches. If one
of those playlists changes before you commit, the batch is refused as **stale** rather than
applied to positions that no longer mean the same thing.

### 3. Undo

Every applied operation stores a **snapshot** of its playlist before and after. Undo writes
the "before" back, but only if the playlist still matches "after". So an undo never silently
overwrites a later change: Segue names the later batch to undo first. Redo works the same
way in reverse. Undo and redo work per batch, by default the most recent one.

Snapshots also make undo exact: positions, "added" dates and each entry's service ID come
back as they were, and a deleted playlist returns under its old library ID.

Limits once real services are connected:
- A playlist deleted on a service comes back with a **new ID**, so its followers, share link
  and collaborators are lost. Segue warns about this before a delete.
- Tracks removed and re-added on a service get a new "date added" there.

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

## Account linking

There's no browser trick for logging in. A clean "Connect" button only works when the
service has registered the app (OAuth) — that is how Soundiiz and TuneMyMusic do it, with
partner app IDs. Without that, the only options are pasting a token or cookie from the
service's web player, or a helper (a browser extension or local browser) that captures it.

Every connector declares **how** it connects and **what** the connection allows:

- **Auth kind:** `oauth_redirect` (browser consent + callback), `oauth_device` (show a code,
  approve on another device), `token_paste` (copy a value from the web player's dev tools),
  or `file` (no account: imports and public playlist links).
- **Capabilities:** `library_read`, `library_write`, `public_playlist_read`,
  `favorites_write`. A connection can be read-only; the UI and engine only offer what was
  granted.
- **Status** comes from a real API call (`connected`, `expired`, `unconfigured`, `error`),
  not from whether a token file exists — an expired session often just returns an empty
  library instead of an error.

| Service | Best path | Fallback | Watch out for |
|---|---|---|---|
| Spotify | OAuth (authorization code + PKCE) with your own developer app | `sp_dc` web-player cookie | SongMirror reports that development-mode apps get `403` when reading or changing playlist *tracks*, though listing and renaming work. Re-check this first: it decides whether Spotify writes need the cookie path. ISRCs only come from the official `/tracks` endpoint |
| TIDAL | Official OAuth 2.1 + PKCE; anyone can create an app | — | Keep scopes minimal; test every playlist operation |
| YouTube Music | Google OAuth **device flow** ("TVs and Limited Input" client) via `ytmusicapi` | Browser cookie headers | Unofficial API; can break |
| Apple Music | MusicKit JS authorization (needs a paid Apple Developer account for the developer token) | Paste the web player's bearer + `media-user-token` | No subscription means catalog-only access |
| Deezer | OAuth with an approved app | `refresh-token` cookie | New app registrations were closed as of Aug 2026 |
| Qobuz, Amazon Music | Partner access only | Web-session tokens | Terms forbid unofficial clients; treat as later or never |
| Files and public links | No account needed | — | Read and import only |

### Secret handling

- Tokens and cookies are account credentials. Store them owner-only (`0600`), or in the OS
  keychain when available. Never log them, put them in URLs or show them in the UI after
  they're entered.
- Keep only the minimal values a connector needs (one cookie, one header), never a full
  cookie jar or copied request.
- **Disconnect** deletes everything stored for that account and revokes the OAuth grant
  where the service supports it.
- Don't handle passwords: login, MFA and CAPTCHAs always happen on the service's own page.

## Proposed architecture

- **Connectors**: one module per service (auth kind + capabilities, see Account linking) implementing the same interface
  (`list_playlists`, `get_tracks`, `search`, `create_playlist`, `delete_playlist`,
  `add_tracks`, `remove_tracks`, `reorder`, `rename`).
- **Library store**: local SQLite with tracks, service IDs, playlists, snapshots,
  sync rules and the operation log.
- **Engine**: matching, dedupe, merge, diff and sync planning. Produces planned operations;
  never writes to a service directly.
- **Change log** (`changes.py`): applies planned operations (commit or auto mode) and
  records before/after snapshots for undo and redo. Locally a batch is all-or-nothing. With
  services it will also need to handle rate limits and partial failures (resume, or roll back
  the part that was written).
- **Interface**: start with a CLI, then a local web UI.

## Build order (working plan)

1. ✅ Neutral model + SQLite store + CSV/JSON/M3U import/export (this is also the backup).
2. ✅ Planned operations, operation log and undo, applied to Segue's own library.
3. Spotify connector (read, then write) behind the mandatory backup gate.
4. Cleanup and batch tools: duplicates, merge by name, bulk delete, with previews.
5. Second connector (YouTube Music or Apple Music) + cross-service matching.
6. Sync, including sync-by-name rules, with dry-run.
7. Local web UI with the pending-changes queue and undo button.

## Prior art and credits

Soundiiz, TuneMyMusic, SongShift and Free Your Music do transfers as hosted (mostly paid)
services with few organizing tools. Segue aims to be open source, local-first and focused on
ongoing library cleanup and sync rather than one-off transfers.

**[SongMirror](https://github.com/ahnafnafee/songmirror)** by Ahnaf An Nafee (MIT) is a
self-hosted, open-source sync tool for many services, and the closest project to Segue.
Several ideas here come from its design and research. Credit for them goes to SongMirror:

- the connector protocol of auth kinds plus per-account capability sets;
- checking connection status with a live call instead of trusting a stored token;
- its per-service research on how competitors connect accounts, and what is and isn't
  possible in a browser;
- the secret-handling rules above.

Segue's focus is different: bulk organizing (merge, dedupe, delete and rename by rule),
mandatory backups, a review-before-apply queue and undo. Where the projects overlap, the
plan is to contribute back rather than compete.

## License

[MIT](LICENSE)
