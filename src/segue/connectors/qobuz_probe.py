"""`segue qobuz probe`: find out what Segue can do with your Qobuz session.

Reads first: who you are, your playlists, one of your own playlists and a followed one,
and your favorite tracks. Then, if allowed, it tests changes on a temporary private
playlist, which it deletes again at the end, even when a step fails:

1. add three tracks, one of them twice, and remove just one copy of the duplicate;
2. move a track to the front and check that its "date added" stays the same;
3. rename it with an emoji in the name (Qobuz is known to drop such names);
4. delete it.
"""

from __future__ import annotations

from collections.abc import Callable

from .common import Check
from .qobuz import QobuzClient, QobuzError, kind_of

TEST_NAME = "Segue test (safe to delete)"
EMOJI_NAME = "Segue test 🧪 (renamed)"


def probe(client: QobuzClient, write: bool, report: Callable[[Check], None] = lambda c: None) -> list[Check]:
    checks: list[Check] = []

    def note(name: str, result: str, detail: str) -> None:
        check = Check(name, result, detail)
        checks.append(check)
        report(check)

    try:
        me = client.me()
        my_id = str(me.get("id") or "") or None
        who = me.get("display_name") or me.get("login") or my_id
        note("Connect", "ok", f"signed in as {who}" + (f" ({my_id})" if my_id else ""))
    except QobuzError as e:
        note("Connect", "error", str(e))
        return checks

    try:
        playlists = client.playlists()
    except QobuzError as e:
        note("List playlists", "error", str(e))
        return checks
    kinds = [kind_of(p, my_id) for p in playlists]
    note("List playlists", "ok", f"{len(playlists)} playlists: {kinds.count('created')} yours, "
                                 f"{kinds.count('collaborative')} collaborative, {kinds.count('followed')} followed")

    sample: list[str] = []
    own = next((p for p, k in zip(playlists, kinds) if k == "created" and (p.get("tracks_count") or 0) > 0), None)
    if own is None:
        note("Read your playlist", "skipped", "none of your own playlists has tracks")
    else:
        try:
            _, items = client.playlist(str(own["id"]))
            with_isrc = sum(1 for i in items if i.get("isrc"))
            with_handle = sum(1 for i in items if i.get("playlist_track_id") is not None)
            note("Read your playlist", "ok", f"{len(items)} tracks from {own['name']!r}: {with_isrc} with an ISRC, "
                                             f"{with_handle} with their own entry ID")
            sample += [str(i["id"]) for i in items if i.get("id") is not None]
        except QobuzError as e:
            note("Read your playlist", "error", str(e))

    other = next((p for p, k in zip(playlists, kinds) if k == "followed"), None)
    if other is None:
        note("Read a followed playlist", "skipped", "you don't follow any playlists")
    else:
        try:
            _, items = client.playlist(str(other["id"]))
            note("Read a followed playlist", "ok", f"{len(items)} tracks from {other['name']!r}")
        except QobuzError as e:
            note("Read a followed playlist", "no" if e.status in (401, 403) else "error", str(e))

    try:
        favorites = client.favorites()
        note("Read favorite tracks", "ok", f"{len(favorites)} favorite tracks")
        sample += [str(i["id"]) for i in favorites if i.get("id") is not None]
    except QobuzError as e:
        note("Read favorite tracks", "error", str(e))

    if not write:
        note("Change playlists", "skipped", "read-only probe")
        return checks
    unique = list(dict.fromkeys(sample))
    if len(unique) < 3:
        note("Change playlists", "skipped", "need at least three tracks from your library to test with")
        return checks

    a, b, c = unique[:3]
    created = None
    deleted = False
    try:
        created = str(client.create_playlist(TEST_NAME, "Made by 'segue qobuz probe'. Delete it if it's still here.")["id"])
        note("Create a playlist", "ok", f"made private playlist {TEST_NAME!r}")

        client.add_tracks(created, [a, b])
        items = _read(client, created, 2)
        if [str(i["id"]) for i in items] != [a, b]:
            note("Add tracks", "error", f"added 2 tracks, read back {len(items)}")
            return checks
        note("Add tracks", "ok", "added 2 tracks")

        client.add_tracks(created, [a])  # the same track again, in its own request
        items = _read(client, created, 3)
        order = [str(i["id"]) for i in items]
        handles = [str(i.get("playlist_track_id")) for i in items]
        if order == [a, b, a] and len(set(handles)) == 3:
            note("Add a duplicate", "ok", "added a track that was already there; each copy has its own entry ID")
            client.delete_tracks(created, [handles[2]])
            items = _read(client, created, 2)
            if [str(i["id"]) for i in items] == [a, b]:
                note("Remove one copy", "ok", "removed just one copy of the duplicated track")
            else:
                note("Remove one copy", "error", f"expected 2 tracks left, read back {len(items)}")
        elif order == [a, b]:
            note("Add a duplicate", "no", "Qobuz didn't add a track that was already in the playlist; "
                                          "Segue can't create duplicates there (removing them works)")
        else:
            note("Add a duplicate", "error", f"unexpected result: {len(order)} tracks")

        client.add_tracks(created, [c])
        items = _read(client, created, 3)
        dates = {str(i.get("playlist_track_id")): i.get("created_at") for i in items}
        last = str(items[-1].get("playlist_track_id"))
        client.move_tracks(created, [last], insert_before=0)
        items = _read(client, created, 3, lambda its: str(its[0]["id"]) == c if its else False)
        order = [str(i["id"]) for i in items]
        if order != [c, a, b]:
            note("Move a track", "error", "the order after moving wasn't what was asked for")
        elif items[0].get("created_at") == dates[last]:
            note("Move a track", "ok", "moved a track to the front; its date added stayed the same")
        else:
            note("Move a track", "no", "moving worked, but the track got a new date added")

        client.update_playlist(created, name=EMOJI_NAME)
        details, _ = client.playlist(created)
        name = details.get("name") or ""
        if name == EMOJI_NAME:
            note("Rename (with emoji)", "ok", "renamed; Qobuz kept the emoji")
        elif not name.strip():
            note("Rename (with emoji)", "no", "Qobuz dropped a name with emoji; Segue saves such names without emoji")
        else:
            note("Rename (with emoji)", "ok", f"renamed; Qobuz saved it as {name!r}")

        client.delete_playlist(created)
        deleted = True
        still_there = any(str(p["id"]) == created for p in client.playlists())
        note("Delete", "ok" if not still_there else "error",
             "deleted the test playlist" if not still_there else "the test playlist is still listed")
    except QobuzError as e:
        note("Change playlists", "error", str(e))
    finally:
        if created and not deleted:
            try:
                client.delete_playlist(created)
                note("Clean up", "ok", "deleted the test playlist")
            except QobuzError as e:
                note("Clean up", "error", f"couldn't delete {TEST_NAME!r}; please delete it yourself ({e})")
    return checks


def _read(client: QobuzClient, playlist_id: str, count: int, done=None, tries: int = 4) -> list[dict]:
    """Read the test playlist, waiting briefly if Qobuz still serves an older (cached) version."""
    _, items = client.playlist(playlist_id)
    for _ in range(tries - 1):
        if len(items) == count and (done is None or done(items)):
            break
        client.sleep(1.5)
        _, items = client.playlist(playlist_id)
    return items


def summary(checks: list[Check]) -> str:
    errors = [c for c in checks if c.result == "error"]
    if errors:
        return "Some checks failed: " + "; ".join(c.name for c in errors) + ". See above."
    if any(c.name == "Delete" and c.result == "ok" for c in checks):
        return "Everything Segue needs works with this Qobuz session."
    return "Reading works. Run without --read-only to test changes too."
