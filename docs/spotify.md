# Spotify

How Segue connects to Spotify, what Spotify allows, and how to set it up.

## What a development-mode app can do (October 2026)

Segue uses your own Spotify developer app in **development mode**: free, for up to 5 users,
and enough for personal use. Spotify changed what these apps may do on
[11 February 2026](https://developer.spotify.com/documentation/web-api/tutorials/february-2026-migration-guide)
(existing apps followed on 9 March 2026). The old playlist endpoints were removed for them and
replaced by new ones. That is most likely why other tools, SongMirror among them, saw `403`
errors when reading or changing playlist tracks: they still called the old endpoints.

| What | Possible? | Endpoint |
|---|---|---|
| List your playlists (created and followed) | Yes | `GET /me/playlists` |
| Read the tracks of playlists you **own or collaborate on** | Yes, including ISRC and date added | `GET /playlists/{id}/items` |
| Read the tracks of playlists you only **follow** | **No**: Spotify returns `403` | — |
| Read Liked Songs | Yes | `GET /me/tracks` |
| Like and unlike tracks | Yes | `PUT` / `DELETE /me/library` |
| Create a playlist | Yes | `POST /me/playlists` |
| Add, remove and reorder tracks | Yes | `POST` / `DELETE` / `PUT /playlists/{id}/items` |
| Rename, change the description | Yes | `PUT /playlists/{id}` |
| Delete (unfollow) a playlist | Yes | `DELETE /me/library` with the playlist's URI |
| Look up many tracks at once | No (removed); not needed, because playlist reads include ISRCs | — |
| Search | Yes, at most 10 results per request | `GET /search` |

**Not documented, so `segue spotify probe` checks them on your account:**

- Whether removing a track removes **every copy** of it. Spotify historically did this, and the
  current documentation doesn't say. If so, removing one copy of a duplicate means removing
  them all and adding one back, and that copy then gets a new "date added".
- Whether **reordering** keeps each track's "date added". Spotify historically did; replacing
  a playlist's tracks wholesale doesn't.

### What this means for Segue

- **Followed playlists** show up in Segue with their names, but without their tracks. Segue can
  unfollow them, but it can't read or copy their contents.
- **Commits edit playlists in place** (remove, add at a position, move) instead of replacing
  them, so each playlist keeps its link, followers and the "date added" of every track that
  stays. Tracks Segue has to add back are the exception.
- **ISRCs come with every playlist read.** That's the most reliable way to match tracks across
  services later.
- A playlist deleted from Spotify can usually be recovered for a while from your account page
  (**Recover playlists**), in addition to Segue's own backups.

## Setting up your Spotify developer app

You need to do this once. Segue never sees your Spotify password: you log in on Spotify's own
page, and Segue gets a token that only allows the permissions listed below.

1. **Spotify Premium.** Since February 2026, the account that owns a development-mode app
   needs an active Premium subscription, or the app stops working.
2. Go to **[developer.spotify.com/dashboard](https://developer.spotify.com/dashboard)** and log
   in with your Spotify account. The first time, Spotify asks you to accept its Developer
   Terms; read and accept them yourself.
3. Click **Create app** and fill in:
   - **App name:** for example `Segue (personal)`
   - **App description:** for example `Organizing my own playlists`
   - **Redirect URI:** `http://127.0.0.1:8765/callback`. Type it exactly like this, then click
     **Add**. Spotify doesn't accept `localhost`, only `127.0.0.1`.
   - **Which API/SDKs are you planning to use?** tick **Web API**.
   - Agree to the terms and click **Save**.
4. Open the app's **Settings** and copy the **Client ID**. You don't need the client secret:
   Segue uses the PKCE login flow, which works without one.
5. Using a different Spotify account from the one that owns the app? Add that account under
   **User Management** (name and email). The app's owner doesn't need this.
6. Connect Segue:

   ```bash
   uv run segue spotify connect --client-id YOUR_CLIENT_ID
   ```

   Your browser opens Spotify's consent page; after you accept, Segue says who it's connected
   as. Then check what your app can do:

   ```bash
   uv run segue spotify probe
   ```

   The probe reads your playlists and Liked Songs, and then asks before testing writes. Those
   use a temporary private playlist named "Segue test (safe to delete)", which it deletes
   again at the end. `--read-only` skips the write tests.

**Permissions Segue asks for:** read your private and collaborative playlists
(`playlist-read-private`, `playlist-read-collaborative`), change your playlists
(`playlist-modify-private`, `playlist-modify-public`), read and change Liked Songs
(`user-library-read`, `user-library-modify`), and unfollow playlists (`user-follow-modify`).

**Where the token is stored:** `~/.segue/spotify.json`, readable only by you (`0600`). It holds
your Client ID and the login token, never your password. `segue spotify disconnect` deletes
it. To revoke access completely, also remove Segue under **Manage apps** on your Spotify
account page.

If the port 8765 is taken, register `http://127.0.0.1:PORT/callback` with another port and pass
`--port PORT` to `connect`.
