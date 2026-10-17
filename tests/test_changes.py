import sqlite3

import pytest

from segue import changes, edits
from segue.model import EntryState, Playlist, PlaylistEntry, PlaylistState
from segue.ops import AddEntries, CreatePlaylist, DeletePlaylist, EditPlaylist, Operation, RemovePositions, Reorder
from segue.store import Store

from .conftest import make_track


def add(store, name, *titles):
    pid, _ = store.save_playlist(Playlist(name, [PlaylistEntry(make_track(t)) for t in titles]))
    return pid


def titles(store, pid):
    return [t.title for t in store.get_playlist(pid).tracks]


def auto(store, plan):
    description, ops = plan
    batch_id, applied = changes.submit(store, description, ops, "auto")
    assert applied
    return batch_id


# Operations

def test_operations_round_trip_through_json():
    entries = (EntryState(1, "2024-01-01", "spotify", "x"), EntryState(2))
    for op in [
        CreatePlaylist("New", "desc", entries), DeletePlaylist(3), EditPlaylist(3, name="B"),
        AddEntries(3, entries, position=1), RemovePositions(3, (0, 2)), Reorder(3, (1, 0)),
    ]:
        assert Operation.from_dict(op.to_dict()) == op


def test_operations_are_pure_state_changes():
    state = PlaylistState("P", entries=(EntryState(1), EntryState(2), EntryState(3)))
    assert AddEntries(9, (EntryState(4),), position=1).apply(state).track_ids == [1, 4, 2, 3]
    assert RemovePositions(9, (0, 2)).apply(state).track_ids == [2]
    assert Reorder(9, (2, 0, 1)).apply(state).track_ids == [3, 1, 2]
    assert EditPlaylist(9, name="Q").apply(state).name == "Q"
    assert DeletePlaylist(9).apply(state) is None
    with pytest.raises(ValueError):
        Reorder(9, (0, 0, 1)).apply(state)
    with pytest.raises(ValueError):
        RemovePositions(9, (5,)).apply(state)


# Commit mode, auto mode, stale batches

def test_commit_mode_is_the_default_and_queues(store):
    pid = add(store, "Mix", "A", "B")
    batch_id, applied = changes.submit(store, *edits.plan_rename(store, pid, "Renamed"))
    assert not applied
    assert store.get_playlist(pid).name == "Mix"
    assert [b.id for b in changes.batches(store, "pending")] == [batch_id]

    changes.commit(store, batch_id)
    assert store.get_playlist(pid).name == "Renamed"
    assert changes.get_batch(store, batch_id).status == "applied"


def test_auto_mode_setting(store):
    changes.set_apply_mode(store, "auto")
    pid = add(store, "Mix", "A")
    _, applied = changes.submit(store, *edits.plan_rename(store, pid, "X"))
    assert applied and store.get_playlist(pid).name == "X"
    with pytest.raises(ValueError):
        changes.set_apply_mode(store, "sometimes")


def test_stale_batch_is_refused(store):
    pid = add(store, "Mix", "A", "B", "C")
    batch_id, _ = changes.submit(store, *edits.plan_remove(store, pid, [1]))
    auto(store, edits.plan_remove(store, pid, [3]))  # the playlist changes in between
    with pytest.raises(changes.ChangeError, match="stale"):
        changes.commit(store, batch_id)
    assert titles(store, pid) == ["A", "B"]


def test_discard(store):
    pid = add(store, "Mix", "A")
    batch_id, _ = changes.submit(store, *edits.plan_delete(store, [pid]))
    changes.discard(store, batch_id)
    assert changes.batches(store, "pending") == []
    with pytest.raises(changes.ChangeError):
        changes.commit(store, batch_id)


def test_failed_batch_changes_nothing(store):
    pid = add(store, "Mix", "A", "B")
    batch_id = changes.plan(store, "bad", [EditPlaylist(pid, name="Half"), RemovePositions(pid, (7,))])
    with pytest.raises(changes.ChangeError):
        changes.commit(store, batch_id)
    assert store.get_playlist(pid).name == "Mix"
    assert changes.get_batch(store, batch_id).status == "pending"


# Undo and redo

def test_undo_and_redo_delete_keeps_the_same_id(store):
    pid = add(store, "Keep me", "A", "B")
    batch_id = auto(store, edits.plan_delete(store, [pid]))
    assert store.playlist_state(pid) is None

    assert changes.undo(store) == batch_id
    assert store.get_playlist(pid).name == "Keep me"
    assert titles(store, pid) == ["A", "B"]

    changes.redo(store)
    assert store.playlist_state(pid) is None


def test_undo_restores_positions_and_entry_details(store):
    a = make_track("A", refs=[])
    playlist = Playlist("P", [PlaylistEntry(a, "2020-01-01"), PlaylistEntry(make_track("B")),
                              PlaylistEntry(make_track("C"))])
    pid, _ = store.save_playlist(playlist)
    before = store.playlist_state(pid)
    auto(store, edits.plan_remove(store, pid, [1, 3]))
    assert titles(store, pid) == ["B"]
    changes.undo(store)
    assert store.playlist_state(pid) == before


def test_undo_refuses_to_overwrite_later_changes(store):
    pid = add(store, "Mix", "A", "B", "C")
    first = auto(store, edits.plan_remove(store, pid, [1]))
    second = auto(store, edits.plan_rename(store, pid, "Later"))
    with pytest.raises(changes.ChangeError, match=f"changed later by batch #{second}"):
        changes.undo(store, first)
    # Undoing in order works.
    changes.undo(store)
    changes.undo(store)
    assert store.get_playlist(pid).name == "Mix"
    assert titles(store, pid) == ["A", "B", "C"]


def test_undo_defaults_to_the_most_recent_change(store):
    pid = add(store, "Mix", "A")
    auto(store, edits.plan_rename(store, pid, "One"))
    second = auto(store, edits.plan_rename(store, pid, "Two"))
    assert changes.undo(store) == second
    assert store.get_playlist(pid).name == "One"


def test_nothing_to_undo(store):
    with pytest.raises(changes.ChangeError, match="nothing to undo"):
        changes.undo(store)
    with pytest.raises(changes.ChangeError, match="nothing to redo"):
        changes.redo(store)


def test_undo_a_create(store):
    a = add(store, "A", "x")
    b = add(store, "B", "y")
    auto(store, edits.plan_merge(store, [a, b], into_name="Both"))
    [both] = store.find_playlists("Both")
    assert titles(store, both.id) == ["x", "y"]
    changes.undo(store)
    assert store.find_playlists("Both") == []


def test_import_replace_can_be_undone(store):
    from segue.model import ServiceRef

    src = ServiceRef("spotify", "pl1")
    old = Playlist("Old", [PlaylistEntry(make_track("A"))], source=src)
    new = Playlist("New", [PlaylistEntry(make_track("B"))], source=src)
    pid, _, _ = changes.record_import(store, old, "a.json")
    _, status, batch_id = changes.record_import(store, new, "b.json")
    assert status == "replaced"
    changes.undo(store, batch_id)
    assert store.get_playlist(pid).name == "Old"

    # Re-importing the same content isn't logged as a change.
    changes.record_import(store, new, "b.json")
    assert changes.record_import(store, new, "b.json")[1:] == ("unchanged", None)


# Planners

def test_dedupe_keeps_first_occurrence(store):
    pid = add(store, "Mix", "A", "B", "A", "C", "B")
    auto(store, edits.plan_dedupe(store, [pid]))
    assert titles(store, pid) == ["A", "B", "C"]
    _, ops = edits.plan_dedupe(store, [pid])
    assert ops == []


def test_merge_into_existing_appends_only_missing_tracks(store):
    target = add(store, "Main", "A", "B")
    src1 = add(store, "Src1", "B", "C", "C")
    src2 = add(store, "Src2", "D", "A")
    auto(store, edits.plan_merge(store, [src1, src2], into_id=target, delete_sources=True))
    assert titles(store, target) == ["A", "B", "C", "D"]
    assert {s.name for s in store.playlist_summaries()} == {"Main"}
    changes.undo(store)
    assert {s.name for s in store.playlist_summaries()} == {"Main", "Src1", "Src2"}
    assert titles(store, target) == ["A", "B"]


def test_merge_needs_a_source(store):
    target = add(store, "Main", "A")
    with pytest.raises(ValueError):
        edits.plan_merge(store, [target], into_id=target)


def test_preview_describes_without_changing(store):
    pid = add(store, "Mix", "A", "B", "A")
    _, ops = edits.plan_dedupe(store, [pid])
    assert changes.preview(store, ops) == ["remove 1 track from 'Mix': Artist - A"]
    assert titles(store, pid) == ["A", "B", "A"]


# Schema upgrade

V1_SCHEMA = """
CREATE TABLE tracks (id INTEGER PRIMARY KEY, title TEXT NOT NULL, artists TEXT NOT NULL,
    album TEXT, duration_ms INTEGER, isrc TEXT, norm_title TEXT NOT NULL, norm_artists TEXT NOT NULL);
CREATE TABLE track_refs (track_id INTEGER NOT NULL REFERENCES tracks(id) ON DELETE CASCADE,
    service TEXT NOT NULL, service_id TEXT NOT NULL, url TEXT, PRIMARY KEY (service, service_id));
CREATE TABLE playlists (id INTEGER PRIMARY KEY, name TEXT NOT NULL, description TEXT,
    source_service TEXT, source_id TEXT, source_url TEXT, created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL, UNIQUE (source_service, source_id));
CREATE TABLE playlist_entries (playlist_id INTEGER NOT NULL REFERENCES playlists(id) ON DELETE CASCADE,
    position INTEGER NOT NULL, track_id INTEGER NOT NULL REFERENCES tracks(id), added_at TEXT,
    ref_service TEXT, ref_id TEXT, PRIMARY KEY (playlist_id, position));
INSERT INTO tracks VALUES (1, 'Song', '["Artist"]', NULL, NULL, NULL, 'song', 'artist');
INSERT INTO playlists VALUES (5, 'Old list', NULL, NULL, NULL, NULL, 'x', 'x');
INSERT INTO playlist_entries VALUES (5, 0, 1, NULL, NULL, NULL);
PRAGMA user_version = 1;
"""


def test_upgrades_a_version_1_library_without_losing_playlists(tmp_path):
    path = tmp_path / "old.db"
    con = sqlite3.connect(path)
    con.executescript(V1_SCHEMA)
    con.close()
    with Store(path) as s:
        assert titles(s, 5) == ["Song"]
        assert s.revision(5) == 0
        auto(s, edits.plan_rename(s, 5, "Upgraded"))
        changes.undo(s)
        assert s.get_playlist(5).name == "Old list"
