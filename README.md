# Segue

One place to manage playlists across music streaming services: connect accounts,
see every playlist side by side, clean them up, merge them, and keep them in sync —
so switching away from Spotify (or any other service) takes minutes, not weekends.

## Status

Steps 1–4 are done (see the build order below):

- the neutral track library, the SQLite store, and JSON/CSV/M3U import and export;
- the change log, with safe mode (staged, undo until commit) and auto mode (applied at once,
  recent changes undoable);
- backups that Segue keeps track of, a backup check before anything deletes playlists, and
  restoring from a CSV or JSON backup;
- the duplicate setting (exact, same recording, same song in any version);
- playlist kinds: created, collaborative, followed and Liked songs;
- the organizing tools: merge by name, bulk delete and rename by filter, sort, shuffle, split,
  clone, find, and `check` (the "Needs a look" list).

**Qobuz works end to end** (step 5): connect with a session copied from the web player,
`segue pull qobuz` reads your playlists and favorites, and `segue commit` writes your
organizing back by editing playlists in place. Setup: **[docs/qobuz.md](docs/qobuz.md)**. It's
tested against a simulated Qobuz; run `segue qobuz probe` to check it on your account.

**Spotify:** next up is the web player's session cookie, the way SongMirror does it, so no
Premium or developer app is needed. Until then: `segue spotify connect` works with your own
developer app (which needs Premium; see [docs/spotify.md](docs/spotify.md)), and you can import
[Exportify](https://exportify.net) CSVs.

```bash
uv run segue setup                              # what counts as a duplicate, safe or auto mode
uv run segue import "Liked Songs.csv" road-trip.m3u8 backup.json
uv run segue list
uv run segue show "road trip"
uv run segue export everything.csv              # or .json; a folder name → one .m3u8 per playlist
uv run segue backup                             # timestamped JSON + CSV next to the library

uv run segue dedupe "road trip"                 # uses the duplicate setting; or --duplicates song
uv run segue merge "Gym 2" --into Gym --delete-sources   # checks for a backup first
uv run segue merge A B C --new "Everything" --dry-run
uv run segue rename 3 "Gym"
uv run segue delete Old
uv run segue remove Gym 4 7                     # positions as shown by: segue show Gym
uv run segue pending                            # staged changes, and what commit will write
uv run segue undo                               # last change; or: segue undo 12
uv run segue redo
uv run segue commit                             # final: no undo after this (or: segue discard)
uv run segue restore ~/.segue/backups/segue-backup-2026-10-17_120000.csv -p Gym
uv run segue history
uv run segue config                             # apply-mode, duplicates, keep, undo-limit

uv run segue check                              # "Needs a look", with a command to fix each item
uv run segue merge-names                        # merge every group of same-named playlists
uv run segue delete --empty                     # or --identical, --max-tracks 3, --name "test*"
uv run segue list --subset                      # filters work on list too, to see what they pick
uv run segue rename-all --name "*road*" --trim --case title --strip-copies
uv run segue sort Gym --by artist,title         # or --by release --reverse; --as-new "Gym sorted"
uv run segue shuffle Gym --as-new "Gym shuffled"
uv run segue split "Liked Songs" --by decade --min 10
uv run segue clone Gym                          # "Gym (copy)"
uv run segue find "hey jude"                    # which playlists contain it, at which positions
uv run segue find --overlaps                    # playlists that are copies or subsets of others
uv run segue find --liked-only                  # liked songs in no other playlist

uv run segue qobuz connect                      # paste a request copied from play.qobuz.com
uv run segue qobuz probe                        # what works on your account
uv run segue pull qobuz                         # your Qobuz library into Segue
uv run segue commit                             # writes staged changes to Qobuz, then final
uv run segue commit --local                     # final in Segue only; writes nothing
uv run segue config destinations qobuz,spotify  # where new playlists show up (one or more)
uv run pytest
```

**Where organized playlists show up.** Changes to a playlist that came from a service are
written back to that service. New playlists made by organizing (`merge --new`, `split`,
`clone`, restores) are created on every service in `destinations`: one, or several. The default
is the services you've pulled from. A service that isn't connected yet is skipped, and the
playlist waits for it. Tracks that have no ID on a service (for example, a Qobuz track and
Spotify) are kept in Segue and skipped there until cross-service matching (step 7) exists.

Commands that change playlists take `--now` (apply right away), `--stage` (stage for commit)
or `--dry-run` (only show the preview). Commands that pick several playlists accept names or
IDs, filters (`--name`, `--empty`, `--min-tracks`, `--max-tracks`, `--kind`, `--service`,
`--identical`, `--subset`), or both. Followed playlists and Liked songs are left out unless you
add `--include-followed` or `--kind`.

Playlists count as having the same name when they match ignoring case, spacing, emoji and a
trailing "(1)", "(copy)" or "- copy"; "Gym 2" stays separate. Merging happens per service, and
the playlist kept is the one with the most tracks (`--survivor oldest` keeps the one that has been
in Segue longest).
Clones are named "<name> (copy)", so `merge-names` would fold them back in; split parts
("<name> – part 1") are left alone.

The library lives at `~/.segue/segue.db`; override it with `--db` or `SEGUE_DB`.

| Format | Use it for | Keeps |
|---|---|---|
| JSON | Backups and restores | Everything: all service IDs per track, ISRC, added dates, playlist source and kind |
| CSV | Backups you can open in a spreadsheet; imports from other tools | One ID per service per row; reads Exportify-style and semicolon (Dutch Excel) files |
| M3U8 | Media players | Title, artists, length, album and one link or file path; tracks without either are left out |

Both backup files carry each playlist's Segue ID, so `segue restore` puts playlists back in
place, including ones deleted since the backup. A backup from another Segue library is
restored as new playlists and never overwrites existing ones.

Importing is safe to repeat. A playlist with a service ID replaces its earlier copy, and an
identical playlist is skipped instead of duplicated. Imports bring data into Segue rather
than change a service, so they apply right away in either mode and can be undone like
recent auto-mode changes.

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
4. **Safe mode by default.** Changes are staged, so you can undo and redo until you commit,
   and a commit is final. Auto mode writes each change right away, and the last 20 can still
   be undone.
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
| Spotify | OAuth (authorization code + PKCE) with your own developer app; see **[docs/spotify.md](docs/spotify.md)** | `sp_dc` web-player cookie, only if the official API falls short | Since Feb 2026: up to 5 users, the app owner needs Premium, and the playlist endpoints moved to `/playlists/{id}/items` and `/me/playlists`. The old ones are gone, which likely explains the `403`s SongMirror saw. Tracks of playlists you only *follow* can't be read. Playlist reads include ISRCs |
| TIDAL | Official OAuth 2.1 + PKCE; anyone can create an app | — | Keep scopes minimal; test every playlist operation |
| YouTube Music | Google OAuth **device flow** ("TVs and Limited Input" client) via `ytmusicapi` | Browser cookie headers | Unofficial API; can break |
| Apple Music | MusicKit JS authorization (needs a paid Apple Developer account for the developer token) | Paste the web player's bearer + `media-user-token` | No subscription means catalog-only access |
| Deezer | OAuth with an approved app | `refresh-token` cookie | New app registrations were closed as of Aug 2026 |
| Qobuz | ✅ Web player session (`X-App-Id` + `X-User-Auth-Token`); see **[docs/qobuz.md](docs/qobuz.md)** | — | Qobuz gives API keys to partners only, so this is unofficial and against its terms; it can break when the web player changes |
| Amazon Music | Partner access only | Web-session tokens | Terms forbid unofficial clients; treat as later or never |
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
- **Change log** (`changes.py`): applies operations right away and records before/after
  snapshots for undo and redo. In safe mode, the snapshots before the first staged change are
  also the "last committed" state, so the net difference is exactly what a commit will write
  to a service. Locally a batch is all-or-nothing. With services, commit will also need to
  handle rate limits and partial failures (resume, or roll back the part that was written).
- **Backups** (`backup.py`): writes backups, records which version of each playlist they
  hold, and plans restores.
- **Interface**: start with a CLI, then a local web UI.

## Build order (working plan)

Organizing comes first; transfers and sync come after.

1. ✅ Neutral model + SQLite store + CSV/JSON/M3U import/export (this is also the backup).
2. ✅ Planned operations, operation log and undo, applied to Segue's own library.
3. ✅ **Safety rework:** staged changes with undo until commit, commit as the final step, and
   backup tracking with a prompt before deletes. Also restore from a backup, the
   duplicate-definition setting (asked on first run), and followed/owned playlists plus Liked
   songs in the model.
4. ✅ **Organizing tools** on the local library: merge by name, bulk delete and rename by filter,
   sort, split, clone, find, and the "Needs a look" list.
5. **Connectors** (in progress): ✅ **Qobuz**: connect, probe, pull, commit in place, conflict
   checks, a backup before the first write after a pull, and destinations for new playlists.
   ✅ Spotify login with a developer app and its probe. Next: **Spotify through the web player's
   session cookie** (no Premium needed), with the same pull and commit.
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
  deleting a track just because it has no match;
- signing in to Qobuz with the web player's session, and most of the Qobuz endpoints.

The Qobuz connector also draws on [Music Assistant](https://github.com/music-assistant/server)
(Apache-2.0), [qobuzify](https://github.com/matthewprince/qobuzify) (MIT) and
[vvaidy/qobuz](https://github.com/vvaidy/qobuz); details in [docs/qobuz.md](docs/qobuz.md#credits).

Segue's focus is different: bulk organizing (merge, dedupe, delete and rename by rule),
mandatory backups, a review-before-apply queue and undo. Where the projects overlap, the
plan is to contribute back rather than compete.

## License

[MIT](LICENSE)
