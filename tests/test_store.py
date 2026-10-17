import pytest

from segue.model import Playlist, PlaylistEntry, ServiceRef
from segue.store import Store

from .conftest import make_track


def test_same_service_id_is_one_track(store):
    a = store.resolve_track(make_track("A", refs=[ServiceRef("spotify", "x")]))
    b = store.resolve_track(make_track("A (different spelling)", refs=[ServiceRef("spotify", "x")]))
    assert a == b
    assert store.track_count() == 1


def test_isrc_links_tracks_across_services(store):
    a = store.resolve_track(make_track("A", isrc="us-abc-12-34567", refs=[ServiceRef("spotify", "s1")]))
    b = store.resolve_track(make_track("A - Remastered", isrc="USABC1234567", refs=[ServiceRef("tidal", "t1")]))
    assert a == b
    track = store.get_track(a)
    assert track.isrc == "USABC1234567"
    assert {r.service for r in track.refs} == {"spotify", "tidal"}


def test_exact_name_match_is_case_and_space_insensitive(store):
    a = store.resolve_track(make_track("Hey  Jude", ["The Beatles"], duration_ms=431000))
    b = store.resolve_track(make_track("hey jude", ["THE BEATLES"], duration_ms=432500))
    assert a == b


def test_name_match_respects_duration_and_versions(store):
    studio = store.resolve_track(make_track("Song", duration_ms=200000))
    assert store.resolve_track(make_track("Song", duration_ms=260000)) != studio  # different length
    assert store.resolve_track(make_track("Song (Live)", duration_ms=200000)) != studio


def test_name_match_never_overrides_conflicting_isrc(store):
    a = store.resolve_track(make_track("Song", isrc="AAA111111111"))
    assert store.resolve_track(make_track("Song", isrc="BBB222222222")) != a


def test_merge_fills_missing_fields(store):
    a = store.resolve_track(make_track("Song", refs=[ServiceRef("spotify", "s")]))
    store.resolve_track(make_track("Song", album="LP", duration_ms=1000, refs=[ServiceRef("spotify", "s")]))
    track = store.get_track(a)
    assert (track.album, track.duration_ms) == ("LP", 1000)


def test_playlist_round_trip_keeps_order_and_duplicates(store):
    x, y = make_track("X"), make_track("Y")
    playlist = Playlist("Mix", [PlaylistEntry(x), PlaylistEntry(y), PlaylistEntry(make_track("X"))])
    pid, status = store.save_playlist(playlist)
    assert status == "added"
    loaded = store.get_playlist(pid)
    assert [t.title for t in loaded.tracks] == ["X", "Y", "X"]
    assert loaded.tracks[0].id == loaded.tracks[2].id


def test_playlist_with_same_source_is_replaced(store):
    src = ServiceRef("spotify", "pl1")
    first, _ = store.save_playlist(Playlist("Old", [PlaylistEntry(make_track("A"))], source=src))
    second, status = store.save_playlist(Playlist("New", [PlaylistEntry(make_track("B"))], source=src))
    assert status == "replaced" and first == second
    loaded = store.get_playlist(first)
    assert loaded.name == "New"
    assert [t.title for t in loaded.tracks] == ["B"]


def test_identical_playlist_without_source_is_not_duplicated(store):
    first, _ = store.save_playlist(Playlist("Same", [PlaylistEntry(make_track("A"))]))
    second, status = store.save_playlist(Playlist("Same", [PlaylistEntry(make_track("A"))]))
    assert (second, status) == (first, "unchanged")


def test_different_playlists_with_same_name_are_both_kept(store):
    store.save_playlist(Playlist("Same", [PlaylistEntry(make_track("A"))]))
    _, status = store.save_playlist(Playlist("Same", [PlaylistEntry(make_track("B"))]))
    assert status == "added"
    assert len(store.find_playlists("same")) == 2


def test_entries_keep_their_own_service_id(store):
    # Two Spotify IDs for one recording become one track, but each entry restores exactly.
    a = make_track("Song", duration_ms=200000, refs=[ServiceRef("spotify", "id1")])
    b = make_track("Song", duration_ms=200000, refs=[ServiceRef("spotify", "id2")])
    pid, _ = store.save_playlist(Playlist("P", [PlaylistEntry(a), PlaylistEntry(b)]))
    first, second = store.get_playlist(pid).tracks
    assert first.id == second.id
    assert (first.ref("spotify").id, second.ref("spotify").id) == ("id1", "id2")


def test_delete_playlist_keeps_tracks(store):
    pid, _ = store.save_playlist(Playlist("P", [PlaylistEntry(make_track("A"))]))
    store.delete_playlist(pid)
    assert store.playlist_summaries() == []
    assert store.track_count() == 1


def test_library_persists_on_disk(tmp_path):
    path = tmp_path / "lib" / "segue.db"
    with Store(path) as s:
        s.save_playlist(Playlist("P", [PlaylistEntry(make_track("A"))]))
    with Store(path) as s:
        assert [p.name for p in s.playlist_summaries()] == ["P"]


def test_refuses_newer_schema(tmp_path):
    path = tmp_path / "segue.db"
    Store(path).close()
    import sqlite3
    con = sqlite3.connect(path)
    con.execute("PRAGMA user_version = 99")
    con.close()
    with pytest.raises(RuntimeError, match="newer Segue"):
        Store(path)
