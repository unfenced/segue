import pytest

from segue import changes, edits, remote
from segue.connectors import qobuz
from segue.model import Playlist, PlaylistEntry

from .conftest import make_track
from .fake_qobuz import FakeQobuz


@pytest.fixture
def fake():
    return FakeQobuz()


@pytest.fixture
def service(fake):
    return qobuz.QobuzService(qobuz.QobuzClient({"app_id": fake.app_id, "user_auth_token": fake.token}, fake,
                                                sleep=lambda s: None))


def pulled(store, service):
    remote.pull(store, service)
    return {s.name: s.id for s in store.playlist_summaries()}


def push(store, service):
    report = remote.push(store, {"qobuz": service})
    assert report.ok, report
    return report


def do(store, plan):
    changes.submit(store, *plan)


# Pull

def test_pull_brings_in_the_library(store, fake, service):
    fake.add_playlist("Gym", [1, 2])
    fake.add_playlist("Theirs", [3], owner=999)
    fake.like([4])
    ids = pulled(store, service)
    assert store.get_playlist(ids["Gym"]).source.service == "qobuz"
    assert {s.name: s.kind for s in store.playlist_summaries()} == {
        "Gym": "created", "Theirs": "followed", "Qobuz favorites": "liked"}
    assert remote.last_pull(store, "qobuz")
    assert remote.pending_pushes(store, {"qobuz": service}) == []


def test_pull_again_updates_and_removes(store, fake, service):
    gym = fake.add_playlist("Gym", [1, 2])
    old = fake.add_playlist("Old", [3])
    ids = pulled(store, service)
    fake._append(gym, ["5"])
    fake._playlist_delete({"playlist_id": old})
    report = remote.pull(store, service)
    assert (report.updated, report.removed) == (1, 1)
    assert [t.title for t in store.get_playlist(ids["Gym"]).tracks] == ["Song 1", "Song 2", "Song 5"]
    assert "Old" not in {s.name for s in store.playlist_summaries()}


def test_pull_waits_for_staged_changes(store, fake, service):
    fake.add_playlist("Gym", [1])
    ids = pulled(store, service)
    do(store, edits.plan_rename(store, ids["Gym"], "Gym 2"))
    with pytest.raises(remote.RemoteError, match="staged changes"):
        remote.pull(store, service)


def test_a_pull_cant_be_undone(store, fake, service):
    fake.add_playlist("Gym", [1])
    remote.pull(store, service)
    with pytest.raises(changes.ChangeError, match="pull"):
        changes.undo(store)


# Push

def test_dedupe_removes_only_the_extra_copy(store, fake, service):
    gym = fake.add_playlist("Gym", [1, 2, 1, 3])
    kept_entry = fake.entries_of(gym)[0]
    ids = pulled(store, service)
    do(store, edits.plan_dedupe(store, [ids["Gym"]], level="recording"))
    push(store, service)
    assert fake.tracks_of(gym) == ["1", "2", "3"]
    assert fake.entries_of(gym)[0] is kept_entry  # same entry, same date added
    assert remote.pending_pushes(store, {"qobuz": service}) == []


def test_adding_a_duplicate_and_reading_through_the_cache(store, fake, service):
    mix = fake.add_playlist("Mix", [1, 2])
    other = fake.add_playlist("Other", [1, 3])
    ids = pulled(store, service)
    fake.stale_reads = 1
    do(store, edits.plan_merge(store, [ids["Other"]], into_id=ids["Mix"], level="exact"))  # adds 3 only
    entry = store.playlist_state(ids["Other"]).entries[0]
    from segue.ops import AddEntries
    changes.submit(store, "add a copy", [AddEntries(ids["Mix"], (entry,), position=0)])
    fake.stale_reads = 2  # the read-back after the moves is cached too
    push(store, service)
    assert fake.tracks_of(mix) == ["1", "1", "2", "3"]
    assert remote.pending_pushes(store, {"qobuz": service}) == []  # no false difference left behind


def test_merge_and_delete_sources(store, fake, service):
    gym = fake.add_playlist("Gym", [1, 2])
    dup = fake.add_playlist("gym (1)", [2, 3, 4])
    ids = pulled(store, service)
    do(store, edits.plan_merge(store, [ids["gym (1)"]], into_id=ids["Gym"], delete_sources=True))
    report = push(store, service)
    assert fake.tracks_of(gym) == ["1", "2", "3", "4"]
    assert dup not in fake.playlists
    assert len(report.written) == 2


def test_sort_moves_entries_without_re_adding(store, fake, service):
    mix = fake.add_playlist("Mix", [5, 3, 9, 1])
    before = {e["playlist_track_id"]: e["created_at"] for e in fake.entries_of(mix)}
    ids = pulled(store, service)
    do(store, edits.plan_sort(store, ids["Mix"], ["title"]))
    push(store, service)
    assert fake.tracks_of(mix) == ["1", "3", "5", "9"]
    assert {e["playlist_track_id"]: e["created_at"] for e in fake.entries_of(mix)} == before
    assert not any(endpoint == "playlist/addTracks" for _, endpoint, _ in fake.calls)


def test_rename_and_emoji_fallback(store, fake, service):
    mix = fake.add_playlist("Mix", [1])
    ids = pulled(store, service)
    fake.update_drops_emoji = True
    do(store, edits.plan_rename(store, ids["Mix"], "Mix 🔥"))
    report = push(store, service)
    assert fake.playlists[mix]["name"] == "Mix"
    assert "doesn't keep emoji" in report.written[0][2][0]


def test_new_playlists_go_to_destinations(store, fake, service):
    fake.add_playlist("A", [1, 2])
    fake.add_playlist("B", [2, 3])
    ids = pulled(store, service)
    do(store, edits.plan_merge(store, [ids["A"], ids["B"]], into_name="A and B 🎧"))
    push(store, service)
    [new_id] = [pid for pid, pl in fake.playlists.items() if pl["name"].startswith("A and B")]
    assert fake.tracks_of(new_id) == ["1", "2", "3"]
    segue_id = store.find_playlists("A and B 🎧")[0].id
    assert store.get_playlist(segue_id).source.id == new_id
    assert remote.pending_pushes(store, {"qobuz": service}) == []
    changes.commit(store)


def test_conflicts_stop_the_whole_push(store, fake, service):
    a = fake.add_playlist("A", [1])
    fake.add_playlist("B", [2])
    ids = pulled(store, service)
    do(store, edits.plan_rename(store, ids["A"], "A2"))
    do(store, edits.plan_rename(store, ids["B"], "B2"))
    fake._append(a, ["9"])  # someone edits A on their phone
    report = remote.push(store, {"qobuz": service})
    assert report.conflicts and not report.written
    assert sorted(p["name"] for p in fake.playlists.values()) == ["A", "B"]


def test_unmatched_tracks_stay_in_segue(store, fake, service):
    mix = fake.add_playlist("Mix", [1, 2])
    ids = pulled(store, service)
    local, _ = store.save_playlist(Playlist("From a file", [PlaylistEntry(make_track("Not on Qobuz"))]))
    do(store, edits.plan_merge(store, [local], into_id=ids["Mix"]))
    report = push(store, service)
    assert report.written == []  # nothing Qobuz can show changed
    assert report.unmatched == ["'Mix': 1 track with no match on Qobuz yet (kept in Segue, matched later)"]
    assert fake.tracks_of(mix) == ["1", "2"]
    changes.commit(store)
    remote.pull(store, service)  # the next pull keeps it
    assert [t.title for t in store.get_playlist(ids["Mix"]).tracks] == ["Song 1", "Song 2", "Not on Qobuz"]


def test_liked_songs_become_favorites(store, fake, service):
    fake.add_playlist("Mix", [1, 2])
    fake.like([3])
    ids = pulled(store, service)
    do(store, edits.plan_merge(store, [ids["Mix"]], into_id=ids["Qobuz favorites"]))
    push(store, service)
    assert sorted(str(t["id"]) for t in fake.favorites) == ["1", "2", "3"]


def test_deleting_a_followed_playlist_unfollows_it(store, fake, service):
    theirs = fake.add_playlist("Theirs", [4], owner=999)
    ids = pulled(store, service)
    do(store, edits.plan_delete(store, [ids["Theirs"]]))
    push(store, service)
    assert theirs in fake.playlists and theirs not in fake.library


def test_auto_mode_undo_puts_the_service_back(store, fake, service):
    mix = fake.add_playlist("Mix", [1, 2])
    ids = pulled(store, service)
    changes.submit(store, *edits.plan_rename(store, ids["Mix"], "Renamed"), mode="auto")
    push(store, service)
    assert fake.playlists[mix]["name"] == "Renamed"
    changes.undo(store)
    push(store, service)
    assert fake.playlists[mix]["name"] == "Mix"


def test_destinations_setting(store, fake, service):
    assert remote.destinations(store) == []
    fake.add_playlist("A", [1])
    remote.pull(store, service)
    assert remote.destinations(store) == ["qobuz"]
    remote.set_destinations(store, "qobuz, spotify", ("qobuz", "spotify"))
    assert remote.destinations(store) == ["qobuz", "spotify"]
    with pytest.raises(ValueError):
        remote.set_destinations(store, "tidal", ("qobuz", "spotify"))
    do(store, edits.plan_clone(store, store.playlist_summaries()[0].id))
    report = remote.push(store, {"qobuz": service})
    assert report.ok and report.skipped == ["'A (copy)' waits for spotify to be connected"]


def test_restoring_a_backup_changes_only_what_differs(store, fake, service, tmp_path):
    from segue import backup
    from segue.formats import json_format

    mix = fake.add_playlist("Mix", [1, 2, 3])
    dates = [e["created_at"] for e in fake.entries_of(mix)]
    ids = pulled(store, service)
    json_format.write(store.all_playlists(), tmp_path / "b.json", store.library_id)
    do(store, edits.plan_remove(store, ids["Mix"], [2]))
    push(store, service)
    changes.commit(store)
    remote.pull(store, service)
    _, ops, _ = backup.plan_restore(store, json_format.read(tmp_path / "b.json"))
    changes.submit(store, "restore", ops)
    push(store, service)
    assert fake.tracks_of(mix) == ["1", "2", "3"]
    kept = [e["created_at"] for e in fake.entries_of(mix)]
    assert kept[0] == dates[0] and kept[2] == dates[2]  # tracks that stayed weren't re-added
