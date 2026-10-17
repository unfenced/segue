# Segue feature spec

What Segue should do, and why. The README describes what exists today; this file describes
the target. Items marked **Assumption** are interpretations that still need to be confirmed;
the open questions are listed at the end.

**Priority:** organizing playlists comes first. Transfers and cross-service sync come later
and build on the same pieces.

## 1. The library

Segue keeps a local copy of each connected service's library and organizes that copy.
Changes only reach the service when they are committed (see [section 4](#4-safety-backups-apply-modes-undo)).

### Playlist kinds

Every playlist records how it relates to you:

| Kind | Can Segue change it? | Notes |
|---|---|---|
| **Created** by you | Yes | The normal case |
| **Collaborative** | Yes, if the service allows | Others may change it too, so conflicts are likelier |
| **Followed** (someone else's) | No: only unfollow, or copy into your own playlist | Excluded from bulk tools by default; can be excluded from sync rules |

Every selection, filter and rule can include or exclude followed playlists.

### Liked songs

"Liked songs" is not a normal playlist, and each service models it differently:

- **Spotify:** *Liked Songs* is a special collection that looks like a playlist.
- **Qobuz, TIDAL, Deezer:** *favorites* are a flag (a tag) on each track.
- **Apple Music:** *Favorite Songs*; **YouTube Music:** *Liked Music*.

Segue shows it as one **Liked songs** collection on every service, under one internal key,
so tools and transfers treat it the same everywhere. It's translated per service when written:
added to the collection on Spotify, tagged as a favorite on Qobuz. Its rules:

- It is a set: no duplicates, and it can't be renamed or deleted.
- It is ordered by date liked. Writing to another service can keep the **order** (by liking
  oldest first), but not the original **dates**.
- Some services can add likes but not remove them through their public API (Apple), so
  removals are offered only where they work.

(This design follows SongMirror's [liked-tracks study](https://github.com/ahnafnafee/songmirror/blob/main/docs/design/2026-09-01-liked-tracks-sync-feasibility.md).)

### Unmatched and unavailable tracks

A track that has no match on a service **stays in the playlist** at its position. It is never
dropped. Segue shows it greyed out, with the reason ("not on TIDAL", "unavailable in your
region", "removed by the service").

What the service shows depends on why the track doesn't match:

- **The track exists on the service but can't be played** (region, licensing): Segue adds it,
  and the service itself shows it greyed out.
- **The track doesn't exist on the service at all:** there is nothing to add, so the service
  can't show it. Segue keeps it in its copy of the playlist and in backups, shows it greyed
  out, and tries to match it again later.

## 2. Organizing tools (first priority)

Every tool works on a selection or a rule, shows a preview first, and follows the apply mode.

**Built** (step 4): bulk delete and rename by filter, merge and merge by name, split, clone,
remove duplicates, sort and shuffle, finding tracks and overlaps, and the "Needs a look" list.
**Not yet:** empty, replace, moving or copying selected tracks, the Liked-songs tools, the
"not changed since" filter (needs dates from a service), and unavailable tracks in "Needs a
look" (needs a connected service).

**Playlists**
- **Delete** in bulk, selected by filter: empty, fewer than N tracks, name pattern, not
  changed since a date, the same as another playlist. For followed playlists this means
  *unfollow*.
- **Rename** in bulk: pattern replace, trim, fix case, strip "(1)" and "copy".
- **Merge into an existing playlist** (Soundiiz "merge") or **into a new one** (Soundiiz
  "join"), with duplicates removed according to the duplicate setting. Merging into an existing
  playlist keeps its link, followers and creation date. So when merging duplicates by name,
  the default survivor is the oldest playlist, or the biggest (`--survivor largest`). Once
  services are connected, the one with the most followers should win.
- **Merge by name**: group the playlists that have the same normalized name (on one service, or
  per service), review the groups, and merge each group into one. Normalizing ignores case,
  spacing, emoji, and a trailing "(1)" or "copy".
- **Split** by number of tracks, by artist, or by decade.
- **Clone** a playlist, for example before a risky edit.
- **Empty** a playlist.
- **Replace** one playlist's tracks with another's (Soundiiz "replace").

**Tracks**
- **Remove duplicates** inside playlists, and in Liked songs.
- **Sort** by title, artist, album, length, date added or release date, in either direction;
  **shuffle**. Either way, save as a new playlist or apply to the original. Sorting by BPM,
  key or mood isn't possible: Spotify stopped giving new apps audio features in late 2024.
- **Move or copy** selected tracks to another playlist; **remove** selected tracks.
- **Like** every track in a playlist, or make a playlist from Liked songs.

**Finding things**
- Which playlists contain this track?
- Tracks that are in no playlist, and liked songs that are in no playlist.
- Playlists that are a subset, or nearly a copy, of another.
- A **"Needs a look"** list: empty playlists, duplicate names, near-identical playlists,
  duplicates inside playlists, unavailable tracks.

## 3. What counts as a duplicate

Segue asks this **during setup**. You can change it later in **settings**, and override it for
a single action.

| Setting | Two entries are duplicates when… | Example caught |
|---|---|---|
| **Exact** | They are the same track on the service (same ID) | The same song added twice |
| **Same recording** | Same ISRC, or the same title and artists with lengths within 3 seconds | The single and the album copy of one recording |
| **Same song, any version** | As above, but ignoring version labels such as "Remastered 2011", "Radio Edit", "Live" and "feat." credits | The original and the remaster |

A second setting decides which copy is kept: the **first in the playlist** (default), the
**earliest added**, or the **most recently added**.

For comparison, spotify-dedup uses "same ID, or same name + main artist + length within 2
seconds", which is close to *Same recording*. Skiley also catches remasters.

## 4. Safety: backups, apply modes, undo

### Backups

- **When:** before the first change to a service, and before any action that deletes
  playlists (bulk delete, or a merge with "delete the merged playlists") if no backup has
  been made since those playlists last changed. Segue asks: *"You haven't downloaded a
  backup since 3 May. Download one now?"* You can download it right there, then continue.
- **What:** a CSV file (opens in any spreadsheet), plus the JSON file that holds every service
  ID. Unmatched and unavailable entries are included.
- **Scheduled backups** (later): daily or weekly, keeping the last N.

### Restoring

Upload a backup (CSV or JSON) to recover. Pick which playlists to restore. Deleted ones are
recreated and changed ones are put back as they were. A restore is a change like any other:
it shows a preview and follows the apply mode. Restoring a deleted playlist on a service
creates a **new** playlist, so its old link and followers don't come back.

### Apply modes

- **Safe mode (default):** changes are **staged**. Segue's view shows the result immediately,
  so you can keep organizing on top of it, for example merge and then dedupe the result.
  **Undo** and **redo** step through the staged changes. **Commit** writes them all to the
  services. **After a commit there is no undo**: the backup is the way back.
- **Auto mode:** every change is written to the service immediately. The most recent changes
  (20 by default; `segue config undo-limit`) can still be undone. Undo writes the previous
  state back to the service, so a playlist deleted there comes back with a new link, and
  tracks put back get a new "date added".

Before a commit, Segue re-reads the affected playlists. If one changed on the service in the
meantime (for example, edited on your phone), it shows the conflict instead of overwriting it.

## 5. Sync rules (later)

- **You define the rules:** which services; which playlists (all, by matching name, or chosen
  pairs); one-way or two-way; whether followed playlists and Liked songs are included.
- **Sync by name:** one rule covers every playlist with the same name across the chosen
  services, including new ones that appear later.
- **Deletion propagates:** removing a track, or deleting a playlist, on one service removes it
  on the others. Guards, based on SongMirror's: a cap on how much one pass may remove, and
  skipping a pass when a service suddenly returns far fewer tracks (a failed read must not
  look like a deletion). A track that simply has no match on another service is never treated
  as a deletion.
- **Not while organizing:** **Assumption:** syncs pause while there are staged changes. What
  you commit after organizing becomes the new starting point for sync (see the open questions).
- Schedules, and a dry-run preview of every sync pass.

## 6. Transfers (later)

- Copy playlists and Liked songs between services, and between two accounts on the same
  service. Later possibly saved albums and followed artists.
- A match review for uncertain matches. Unmatched tracks are kept greyed out as in
  [section 1](#unmatched-and-unavailable-tracks).
- Transfer from a public playlist link without following it first.
- Keep the date-added order where the service allows it.

## 7. Import and export

- **Import:** Segue JSON, CSV (including Exportify), M3U/M3U8; later plain text lists and
  public playlist links.
- **Export:** CSV, JSON, M3U8; later Soundiiz's JSON import format, so a Segue backup can also
  be taken to Soundiiz.

## Not planned

- Translations of the interface (English only).
- AI playlist generation, SmartLinks and promotion features (Soundiiz has these; they aren't
  about organizing).

## What other tools do (Oct 2026)

| Tool | Organizing | Sync and transfer | Worth taking |
|---|---|---|---|
| [Soundiiz](https://soundiiz.com/features) (paid, hosted) | Merge, join, replace, split, sort, shuffle, clone, empty, remove duplicates, export. The free plan only selects up to 200 tracks per playlist | Scheduled one-way sync; no sync of favorites | The full set of playlist operations; the difference between "merge" and "join" |
| [SongMirror](https://github.com/ahnafnafee/songmirror) (MIT, self-hosted) | Little | One-way, multi-source merge, two-way across N services; liked songs; transfers; transfer history | Its safety guards (removal caps, read-collapse guard, never deleting unmatched tracks), the liked-songs collection model, scheduled backups, an editable list of track matches |
| [spotify-dedup](https://github.com/JMPerez/spotify-dedup) (MIT) | Duplicates in playlists and Liked Songs | — | Edits in place, so playlists keep their followers and creation date; its duplicate definition |
| [Skiley](https://skiley.net/features/playlist-manager) (hosted, one-time payment) | Sort by many properties, filter, merge with duplicates flagged, copy before editing, shuffle | — | "Save as new playlist, or apply to the original"; offering a copy before a risky edit |
| [Exportify](https://github.com/watsonbox/exportify), [spotify-backup](https://github.com/caseychu/spotify-backup) (MIT) | — | Export only | The CSV format people already have (Segue imports it) |
| [spotify_to_ytmusic](https://github.com/sigma67/spotify_to_ytmusic) (MIT), [spotify_to_tidal](https://github.com/spotify2tidal/spotify_to_tidal) (AGPL) | — | One-direction transfer tools | Matching approaches. spotify_to_tidal is AGPL, so read it but don't copy its code into MIT-licensed Segue |

## Open questions

1. **Sync while organizing.** Say Spotify *Gym* and *Gym 2* are both synced to TIDAL, and you
   merge *Gym 2* into *Gym* on Spotify and delete *Gym 2*. Should TIDAL get the same result
   (its *Gym 2* deleted and the tracks merged into its *Gym*), or should TIDAL's *Gym 2* be
   left alone because the deletion was organizing, not a real deletion?
2. **Default for followed playlists in sync rules:** excluded unless you include them, or the
   other way around?
