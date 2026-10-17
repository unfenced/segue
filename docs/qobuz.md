# Qobuz

How Segue connects to Qobuz, what it can do there, and how to set it up.

## How Segue signs in

Qobuz only gives API keys to business partners, so there's no "Connect" button an
open-source tool can offer. Like [SongMirror](https://github.com/ahnafnafee/songmirror), Segue
uses your **web player session**: the two values the Qobuz web player sends with every request.

- **`X-App-Id`**: which app is calling (the web player's own ID).
- **`X-User-Auth-Token`**: your signed-in session.

You copy one request from the web player and paste it into Segue. Segue keeps only those two
values (and your user ID); cookies, other headers and the rest of the request are thrown away
before anything is saved. Segue never sees your password.

**What to know before you use it:**

- **It's unofficial.** Qobuz's API terms only allow apps that Qobuz approved, so using the web
  player's session is against those terms. The risk is to your Qobuz account; for personal use
  it's the same thing SongMirror does.
- **It can break** whenever Qobuz changes its web player. `segue qobuz probe` tells you quickly.
- **The session is a login.** Anyone with it can change your library until it expires or you
  sign out. Segue stores it in `~/.segue/qobuz.json`, readable only by you.

## Setting it up

1. Open **[play.qobuz.com](https://play.qobuz.com)** in your browser and sign in.
2. Open the developer tools:
   - **Chrome, Edge, Firefox:** `Cmd+Option+I` on a Mac, `Ctrl+Shift+I` on Windows or Linux.
   - **Safari:** first turn on **Settings → Advanced → Show features for web developers**, then
     `Cmd+Option+I`.
3. Go to the **Network** tab and type `api.json` in its filter box. Click around in the web
   player (open a playlist) so requests show up.
4. Right-click any request to `www.qobuz.com/api.json/0.2/…` and choose **Copy → Copy as cURL**.
5. Run:

   ```bash
   uv run segue qobuz connect
   ```

   Paste, then press Enter on an empty line. Or save the copied text in a file and run
   `uv run segue qobuz connect --file request.txt`. Segue checks the session by reading your
   profile and playlists, and says who it's connected as.
6. Check what works on your account:

   ```bash
   uv run segue qobuz probe
   ```

   It reads first, then asks before testing changes. Those use a temporary private playlist,
   "Segue test (safe to delete)", which it always deletes again. `--read-only` skips them.

When the session expires, Segue says so; copy a fresh request and run `segue qobuz connect`
again. `segue qobuz disconnect` deletes the stored session.

## Organizing your Qobuz library

```bash
uv run segue pull qobuz          # read your playlists and favorites into Segue
uv run segue backup              # needed before the first change to Qobuz after a pull
uv run segue check               # what needs a look
uv run segue merge-names         # … organize: every change is staged
uv run segue pending             # what commit will write to Qobuz
uv run segue commit              # write it; final after this
```

- **Your playlists, followed playlists and favorites** all come in. Favorites become Segue's
  **Liked songs**; adding tracks there favorites them on Qobuz.
- **Edits happen in place.** Every Qobuz playlist entry has its own ID, so Segue removes exactly
  the copy you removed, moves entries instead of re-adding them (they keep their date added),
  and keeps the playlist's link.
- **Deleting a followed playlist unfollows it.**
- **New playlists** (from `merge --new`, `split`, `clone`) are created on your *destinations*:
  `segue config destinations` (default: the services you've pulled from).
- **Conflicts are caught.** Before writing, Segue re-reads every playlist it will change. If one
  changed on Qobuz since your last pull (say, from your phone), nothing is written: commit or
  discard your staged changes, `segue pull qobuz`, and redo what's needed.
- **In auto mode** each change is written right away, and undoing one of the last 20 writes the
  previous version back to Qobuz.

## Known quirks

- **Emoji in names.** Qobuz is known to save an empty name when a playlist is *created* with an
  emoji in it. Segue creates playlists with a plain name, then renames them. If Qobuz drops the
  emoji on rename too, Segue keeps the plain name and says so. The probe checks which applies.
- **Playlist reads can be cached.** Qobuz has been seen to serve cached contents for a playlist
  right after a change. Segue reads every playlist back after writing it; if what it reads
  doesn't line up with what it wrote, it takes Qobuz's version as the truth.
- **Tracks without a Qobuz match** (say, merged in from a file import) stay in Segue's copy of
  the playlist at their position. They aren't written to Qobuz, and the next pull keeps them.

## Credits

The endpoints and quirks come from open-source projects that use the Qobuz web API:
[SongMirror](https://github.com/ahnafnafee/songmirror) (MIT; the session approach and most
endpoints), [Music Assistant](https://github.com/music-assistant/server) (Apache-2.0; telling
owned from followed playlists, unsubscribing),
[qobuzify](https://github.com/matthewprince/qobuzify) (MIT; the emoji and caching quirks) and
[vvaidy/qobuz](https://github.com/vvaidy/qobuz) (the playlist reorder endpoint). Segue's code is
its own.
