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

How changes work today: in commit mode a change waits in a queue until `segue commit`, and
undo also works after a commit. Step 3 changes this to the staged model described in the spec:
changes show up right away, undo works only until you commit, and commit is final.

## What Segue will do

The full feature spec, with open questions, is in **[docs/spec.md](docs/spec.md)**. In short:

1. **Organize first.** Bulk delete, rename, merge (also "merge everything with the same
   name"), remove duplicates, split, sort, clone, and find things like "which playlists
   contain this track". Every tool works on a selection or rule and shows a preview.
2. **You decide what a duplicate is**: the exact same track, the same recording, or the same
   song in any version. Asked during setup, changeable in settings.
3. **Never lose anything.** A CSV/JSON backup is required before the first change to a
   service, and Segue asks for a fresh one before anything that deletes playlists. Backups
   can be uploaded again to restore.
4. **Safe mode by default.** Changes are staged, so you can undo and redo until you commit.
   Auto mode writes each change right away.
5. **Your playlists vs. followed ones**, and **Liked songs** as one collection everywhere,
   translated per service (a collection on Spotify, a favorite tag on Qobuz).
6. **Later: transfers and sync rules.** Copy and sync across services, with deletions
   propagating, followed playlists excludable, and unmatched tracks kept greyed out instead
   of dropped.

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
| Spotify | OAuth (authorization code + PKCE) with your own developer app | `sp_dc` web-player cookie | Since Feb 2026, development-mode apps allow 5 users, which is fine for personal use. SongMirror reports that they get `403` when reading or changing playlist *tracks*, though listing and renaming work. Re-check this first: it decides whether Spotify writes need the cookie path. ISRCs only come from the official `/tracks` endpoint |
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

Organizing comes first; transfers and sync come after.

1. ✅ Neutral model + SQLite store + CSV/JSON/M3U import/export (this is also the backup).
2. ✅ Planned operations, operation log and undo, applied to Segue's own library.
3. **Safety rework:** staged changes with undo until commit, commit as the final step, and
   backup tracking with a prompt before deletes. Also restore from a backup, the
   duplicate-definition setting (asked on first run), and followed/owned playlists plus Liked
   songs in the model.
4. **Organizing tools** on the local library: merge by name, bulk delete and rename by filter,
   sort, split, clone, find, and the "Needs a look" list.
5. **Spotify connector:** read playlists (created and followed) and Liked Songs; commit staged
   changes by editing in place, so followers and links survive; detect conflicts before
   writing.
6. **Local web UI**: organizing hundreds of playlists needs a visual interface, not just a CLI.
7. Second connector, cross-service matching, and transfers, including Liked songs.
8. Sync rules.

## Prior art and credits

Soundiiz, TuneMyMusic, SongShift and Free Your Music do transfers as hosted (mostly paid)
services with few organizing tools. Segue aims to be open source, local-first and focused on
ongoing library cleanup and sync rather than one-off transfers. The spec has a
[comparison table](docs/spec.md#what-other-tools-do-oct-2026), including
[spotify-dedup](https://github.com/JMPerez/spotify-dedup) and Skiley.

**[SongMirror](https://github.com/ahnafnafee/songmirror)** by Ahnaf An Nafee (MIT) is a
self-hosted, open-source sync tool for many services, and the closest project to Segue.
Several ideas here come from its design and research. Credit for them goes to SongMirror:

- the connector protocol of auth kinds plus per-account capability sets;
- checking connection status with a live call instead of trusting a stored token;
- its per-service research on how competitors connect accounts, and what is and isn't
  possible in a browser;
- the secret-handling rules above;
- modelling Liked songs as one collection with a single internal key, translated per service;
- sync guards: capped removals, skipping a pass after a suspiciously short read, and never
  deleting a track just because it has no match.

Segue's focus is different: bulk organizing (merge, dedupe, delete and rename by rule),
mandatory backups, a review-before-apply queue and undo. Where the projects overlap, the
plan is to contribute back rather than compete.

## License

[MIT](LICENSE)
