"""`segue spotify probe`: find out what this Spotify app may actually do.

Reads first: your profile, your playlists, the tracks of one of your own playlists and of a
followed one, and Liked Songs. Then, if allowed, it tests changes on a temporary private
playlist, which it deletes again at the end, even when a step fails:

1. create it and add three tracks, one of them twice;
2. move a track and check that its "date added" stays the same;
3. remove the duplicated track once and see whether Spotify removes one copy or every copy;
4. rename it, then delete (unfollow) it.
"""

from __future__ import annotations

from collections.abc import Callable

from .common import Check
from .spotify import SpotifyClient, SpotifyError

TEST_NAME = "Segue test (safe to delete)"
TEST_DESCRIPTION = "Made by 'segue spotify probe' to test what Segue can do. Delete it if it's still here."


def _track(item: dict) -> dict:
    return item.get("item") or item.get("track") or {}


def _total(playlist: dict) -> int:
    return (playlist.get("items") or playlist.get("tracks") or {}).get("total") or 0


def probe(client: SpotifyClient, write: bool, report: Callable[[Check], None] = lambda c: None) -> list[Check]:
    checks: list[Check] = []

    def note(name: str, result: str, detail: str) -> Check:
        check = Check(name, result, detail)
        checks.append(check)
        report(check)
        return check

    # Reading
    try:
        me = client.me()
    except SpotifyError as e:
        note("Connect", "error", str(e))
        return checks
    note("Connect", "ok", f"logged in as {me.get('display_name') or me['id']} ({me['id']})")

    try:
        playlists = client.playlists()
    except SpotifyError as e:
        note("List playlists", "error", str(e))
        return checks
    owned = [p for p in playlists if (p.get("owner") or {}).get("id") == me["id"]]
    collaborative = [p for p in playlists if p not in owned and p.get("collaborative")]
    followed = [p for p in playlists if p not in owned and p not in collaborative]
    note("List playlists", "ok", f"{len(playlists)} playlists: {len(owned)} yours, "
                                 f"{len(collaborative)} collaborative, {len(followed)} followed")

    sample_uris: list[str] = []
    own = next((p for p in owned if _total(p) > 0), None)
    if own is None:
        note("Read your playlist", "skipped", "none of your own playlists has tracks")
    else:
        try:
            items = client.playlist_items(own["id"])
            tracks = [_track(i) for i in items]
            with_isrc = sum(1 for t in tracks if (t.get("external_ids") or {}).get("isrc"))
            with_date = sum(1 for i in items if i.get("added_at"))
            note("Read your playlist", "ok", f"{len(items)} tracks from {own['name']!r}: "
                                             f"{with_isrc} with an ISRC, {with_date} with a date added")
            sample_uris += [t["uri"] for t in tracks if t.get("uri", "").startswith("spotify:track:")]
        except SpotifyError as e:
            note("Read your playlist", "error", str(e))

    other = followed[0] if followed else None
    if other is None:
        note("Read a followed playlist", "skipped", "you don't follow any playlists")
    else:
        try:
            items = client.playlist_items(other["id"])
            note("Read a followed playlist", "ok", f"{len(items)} tracks from {other['name']!r} (Spotify allowed it)")
        except SpotifyError as e:
            if e.status == 403:
                note("Read a followed playlist", "no", "Spotify doesn't let development-mode apps read "
                                                     "playlists you only follow (403), as documented")
            else:
                note("Read a followed playlist", "error", str(e))

    try:
        liked = client.saved_tracks(limit=20)
        note("Read Liked Songs", "ok", f"read the {len(liked)} most recent liked songs")
        sample_uris = [_track(i).get("uri", "") for i in liked if _track(i).get("uri", "").startswith(
            "spotify:track:")] + sample_uris
    except SpotifyError as e:
        note("Read Liked Songs", "error", str(e))

    if not write:
        note("Change playlists", "skipped", "read-only probe")
        return checks
    unique = list(dict.fromkeys(sample_uris))
    if len(unique) < 2:
        note("Change playlists", "skipped", "need at least two tracks from your library to test with")
        return checks

    # Changing, on a temporary playlist
    a, b = unique[0], unique[1]
    created = None
    deleted = False
    try:
        created = client.create_playlist(TEST_NAME, TEST_DESCRIPTION, public=False)
        note("Create a playlist", "ok", f"made private playlist {TEST_NAME!r}")

        client.add_items(created["id"], [a, b, a])
        items = _read_back(client, created["id"], lambda order: len(order) == 3)
        order = [_track(i).get("uri") for i in items]
        if order == [a, b, a]:
            note("Add tracks", "ok", "added 3 tracks, one of them twice, in the right order")
        else:
            note("Add tracks", "error", f"expected 3 tracks in order, read back {len(order)}")
            return checks
        dates = [i.get("added_at") for i in items]

        client.move_items(created["id"], range_start=2, insert_before=0)
        items = _read_back(client, created["id"], lambda order: order != [a, b, a])
        order = [_track(i).get("uri") for i in items]
        if order != [a, a, b]:
            note("Move a track", "error", "the order after moving wasn't what was asked for")
        elif items[0].get("added_at") == dates[2]:
            note("Move a track", "ok", "moving keeps the track's date added")
        else:
            note("Move a track", "no", "moving worked, but the moved track got a new date added")

        client.remove_items(created["id"], [a])
        items = _read_back(client, created["id"], lambda order: len(order) < 3)
        order = [_track(i).get("uri") for i in items]
        if order == [b]:
            note("Remove a duplicated track", "ok",
                 "removing a track removes every copy of it (Segue re-adds copies it keeps)")
        elif sorted(order) == sorted([a, b]):
            note("Remove a duplicated track", "ok", "removing a track removes one copy")
        else:
            note("Remove a duplicated track", "error", f"unexpected result: {len(order)} tracks left")

        client.change_details(created["id"], name=f"{TEST_NAME} (renamed)")
        name = client.playlist(created["id"]).get("name")
        note("Rename", "ok" if name == f"{TEST_NAME} (renamed)" else "error",
             "renamed the playlist" if name == f"{TEST_NAME} (renamed)" else f"name is still {name!r}")

        client.unfollow_playlist(created["id"])
        deleted = True
        still_there = any(p["id"] == created["id"] for p in client.playlists())
        note("Delete", "ok" if not still_there else "error",
             "deleted the test playlist" if not still_there else "the test playlist is still listed")
    except SpotifyError as e:
        note("Change playlists", "error", str(e))
    finally:
        if created and not deleted:
            try:
                client.unfollow_playlist(created["id"])
                note("Clean up", "ok", "deleted the test playlist")
            except SpotifyError as e:
                note("Clean up", "error", f"couldn't delete {TEST_NAME!r}; please delete it yourself ({e})")
    return checks


def _read_back(client: SpotifyClient, playlist_id: str, done: Callable[[list[str]], bool],
               tries: int = 3) -> list[dict]:
    """Read a playlist, waiting briefly if Spotify hasn't caught up with the last change yet."""
    items = client.playlist_items(playlist_id)
    for _ in range(tries - 1):
        if done([_track(i).get("uri") for i in items]):
            break
        client.sleep(1)
        items = client.playlist_items(playlist_id)
    return items


def summary(checks: list[Check]) -> str:
    errors = [c for c in checks if c.result == "error"]
    if errors:
        return "Some checks failed: " + "; ".join(c.name for c in errors) + ". See above."
    wrote = any(c.name == "Delete" and c.result == "ok" for c in checks)
    if wrote:
        return "Everything Segue needs works with this app."
    return "Reading works. Run without --read-only to test changes too."
