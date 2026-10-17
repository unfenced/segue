import sqlite3

import pytest

from segue import changes, edits
from segue.model import EntryState, Playlist, PlaylistEntry, PlaylistState, ServiceRef
from segue.ops import (
    AddEntries, CreatePlaylist, DeletePlaylist, EditPlaylist, Operation, RemovePositions, Reorder,
    RestorePlaylist,
)
from segue.store import Store

from .conftest import make_track


def add(store, name, *titles, kind="created"):
    pid, _ = store.save_playlist(Playlist(name, [PlaylistEntry(make_track(t)) for t in titles], kind=kind))
    return pid


def titles(store, pid):
    return [t.title for t in store.get_playlist(pid).tracks]


def do(store, plan, mode="safe"):
    batch_id, _ = changes.submit(store, *plan, mode=mode)
    return batch_id


# Operations

def test_operations_round_trip_through_json():
    entries = (EntryState(1, "2024-01-01", "spotify", "x"), EntryState(2))
    for op in [
        CreatePlaylist("New", "desc", entries), DeletePlaylist(3), EditPlaylist(3, name="B"),
        AddEntries(3, entries, position=1), RemovePositions(3, (0, 2)), Reorder(3, (1, 0)),
        RestorePlaylist(3, PlaylistState("P", entries=entries, kind="followed")),
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


def test_followed_and_liked_rules():
    followed = PlaylistState("Theirs", entries=(EntryState(1),), kind="followed")
    liked = PlaylistState("Liked Songs", entries=(EntryState(1),), kind="liked")
    with pytest.raises(ValueError, match="followed"):
        AddEntries(1, (EntryState(2),)).apply(followed)
    with pytest.raises(ValueError, match="can't be renamed"):
        EditPlaylist(1, name="X").apply(liked)
    with pytest.raises(ValueError, match="can't be deleted"):
        DeletePlaylist(1).apply(liked)
    assert DeletePlaylist(1).apply(followed) is None  # unfollow
    # Liked songs is a set.
    assert AddEntries(1, (EntryState(1), EntryState(2), EntryState(2))).apply(liked).track_ids == [1, 2]


# Safe mode: staged until commit

def test_safe_mode_is_the_default_and_applies_right_away(store):
    pid = add(store, "Mix", "A", "B")
    batch_id, status = changes.submit(store, *edits.plan_rename(store, pid, "Renamed"))
    assert status == "staged"
    assert store.get_playlist(pid).name == "Renamed"  # visible immediately


def test_changes_build_on_staged_changes(store):
    a = add(store, "A", "x", "y")
    b = add(store, "B", "y", "z")
    do(store, edits.plan_merge(store, [b], into_id=a, delete_sources=True))
    do(store, edits.plan_rename(store, a, "Both"))
    assert titles(store, a) == ["x", "y", "z"] and store.get_playlist(a).name == "Both"
    assert changes.describe_staged(store) == [
        f"#{a} 'Both': renamed from 'A', +1 track", f"#{b} delete 'B' (2 tracks)",
    ]


def test_undo_and_redo_staged_changes(store):
    pid = add(store, "Mix", "A", "B", "C")
    first = do(store, edits.plan_remove(store, pid, [1]))
    second = do(store, edits.plan_rename(store, pid, "Renamed"))
    assert changes.undo(store) == second
    assert changes.undo(store) == first
    assert titles(store, pid) == ["A", "B", "C"] and store.get_playlist(pid).name == "Mix"
    assert changes.redo(store) == first
    assert titles(store, pid) == ["B", "C"]
    assert changes.get_batch(store, first).status == "staged"


def test_commit_makes_changes_final(store):
    pid = add(store, "Mix", "A", "B")
    batch_id = do(store, edits.plan_delete(store, [pid]))
    assert changes.commit(store) == [batch_id]
    with pytest.raises(changes.ChangeError, match="final; restore a backup"):
        changes.undo(store)
    assert store.playlist_state(pid) is None
    assert changes.commit(store) == []


def test_discard_reverts_all_staged(store):
    pid = add(store, "Mix", "A", "B")
    do(store, edits.plan_rename(store, pid, "X"))
    do(store, edits.plan_remove(store, pid, [2]))
    assert len(changes.discard(store)) == 2
    assert store.get_playlist(pid).name == "Mix" and titles(store, pid) == ["A", "B"]
    with pytest.raises(changes.ChangeError, match="nothing to redo"):
        changes.redo(store)


def test_a_new_change_clears_redo(store):
    pid = add(store, "Mix", "A")
    do(store, edits.plan_rename(store, pid, "One"))
    changes.undo(store)
    do(store, edits.plan_rename(store, pid, "Two"))
    with pytest.raises(changes.ChangeError, match="nothing to redo"):
        changes.redo(store)


def test_undo_refuses_to_overwrite_later_changes(store):
    pid = add(store, "Mix", "A", "B", "C")
    first = do(store, edits.plan_remove(store, pid, [1]))
    second = do(store, edits.plan_rename(store, pid, "Later"))
    with pytest.raises(changes.ChangeError, match=f"changed later by #{second}"):
        changes.undo(store, first)
    assert not changes.can_undo(store, changes.get_batch(store, first))
    assert changes.can_undo(store, changes.get_batch(store, second))


def test_failed_batch_changes_nothing(store):
    pid = add(store, "Mix", "A", "B")
    with pytest.raises(changes.ChangeError):
        changes.submit(store, "bad", [EditPlaylist(pid, name="Half"), RemovePositions(pid, (7,))])
    assert store.get_playlist(pid).name == "Mix"
    assert changes.batches(store) == []


def test_undo_delete_brings_back_the_same_playlist(store):
    a = make_track("A")
    pid, _ = store.save_playlist(Playlist("Keep me", [PlaylistEntry(a, "2020-01-01"), PlaylistEntry(make_track("B"))]))
    before = store.playlist_state(pid)
    do(store, edits.plan_delete(store, [pid]))
    changes.undo(store)
    assert store.playlist_state(pid) == before


# Auto mode: final at once, recent ones undoable

def test_auto_mode_commits_but_recent_changes_can_be_undone(store):
    changes.set_apply_mode(store, "auto")
    pid = add(store, "Mix", "A")
    batch_id, status = changes.submit(store, *edits.plan_rename(store, pid, "X"))
    assert status == "committed"
    assert changes.undo(store) == batch_id
    assert store.get_playlist(pid).name == "Mix"
    changes.redo(store)
    assert changes.get_batch(store, batch_id).status == "committed"


def test_auto_mode_undo_limit(store):
    changes.set_undo_limit(store, 2)
    pid = add(store, "Mix", "A")
    old = do(store, edits.plan_rename(store, pid, "1"), mode="auto")
    do(store, edits.plan_rename(store, pid, "2"), mode="auto")
    do(store, edits.plan_rename(store, pid, "3"), mode="auto")
    with pytest.raises(changes.ChangeError, match="only the last 2"):
        changes.undo(store, old)
    changes.undo(store)
    changes.undo(store)
    assert store.get_playlist(pid).name == "1"


def test_nothing_to_undo(store):
    with pytest.raises(changes.ChangeError, match="nothing to undo"):
        changes.undo(store)


def test_import_replace_can_be_undone(store):
    src = ServiceRef("spotify", "pl1")
    old = Playlist("Old", [PlaylistEntry(make_track("A"))], source=src)
    new = Playlist("New", [PlaylistEntry(make_track("B"))], source=src)
    pid, _, _ = changes.record_import(store, old, "a.json")
    _, status, batch_id = changes.record_import(store, new, "b.json")
    assert status == "replaced"
    changes.undo(store, batch_id)
    assert store.get_playlist(pid).name == "Old"
    changes.record_import(store, new, "b.json")
    assert changes.record_import(store, new, "b.json")[1:] == ("unchanged", None)


# Planners

def test_dedupe_default_keeps_first_occurrence(store):
    pid = add(store, "Mix", "A", "B", "A", "C", "B")
    do(store, edits.plan_dedupe(store, [pid]))
    assert titles(store, pid) == ["A", "B", "C"]
    assert edits.plan_dedupe(store, [pid])[1] == []


def test_dedupe_song_level_catches_versions(store):
    pid = add(store, "Mix", "Hey Jude", "Hey Jude - Remastered 2015", "Hey Jude (Live)")
    assert edits.plan_dedupe(store, [pid], level="recording")[1] == []
    do(store, edits.plan_dedupe(store, [pid], level="song"))
    assert titles(store, pid) == ["Hey Jude"]


def test_followed_playlists_cant_be_deduped_or_merged_into(store):
    theirs = add(store, "Theirs", "A", "A", kind="followed")
    mine = add(store, "Mine", "B")
    with pytest.raises(ValueError, match="followed"):
        edits.plan_dedupe(store, [theirs])
    with pytest.raises(ValueError, match="followed"):
        edits.plan_merge(store, [mine], into_id=theirs)
    # Copying out of a followed playlist is fine.
    do(store, edits.plan_merge(store, [theirs], into_id=mine))
    assert titles(store, mine) == ["B", "A"]


def test_merge_into_existing_appends_only_missing_tracks(store):
    target = add(store, "Main", "A", "B")
    src1 = add(store, "Src1", "B", "C", "C")
    src2 = add(store, "Src2", "D", "A")
    do(store, edits.plan_merge(store, [src1, src2], into_id=target, delete_sources=True))
    assert titles(store, target) == ["A", "B", "C", "D"]
    assert {s.name for s in store.playlist_summaries()} == {"Main"}
    changes.undo(store)
    assert {s.name for s in store.playlist_summaries()} == {"Main", "Src1", "Src2"}


def test_merge_into_new_playlist(store):
    a = add(store, "A", "x")
    b = add(store, "B", "x", "y")
    do(store, edits.plan_merge(store, [a, b], into_name="Both"))
    [both] = store.find_playlists("Both")
    assert titles(store, both.id) == ["x", "y"]


def test_liked_songs_cant_be_deleted_by_a_merge(store):
    liked = add(store, "Liked Songs", "A", kind="liked")
    mine = add(store, "Mine", "B")
    with pytest.raises(ValueError, match="Liked songs can't be deleted"):
        edits.plan_merge(store, [liked], into_id=mine, delete_sources=True)


def test_preview_describes_without_changing(store):
    pid = add(store, "Mix", "A", "B", "A")
    _, ops = edits.plan_dedupe(store, [pid])
    assert changes.preview(store, ops) == ["remove 1 track from 'Mix': Artist - A"]
    assert titles(store, pid) == ["A", "B", "A"]


# Schema upgrades

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


def test_upgrades_a_version_1_library(tmp_path):
    path = tmp_path / "old.db"
    con = sqlite3.connect(path)
    con.executescript(V1_SCHEMA)
    con.close()
    with Store(path) as s:
        assert titles(s, 5) == ["Song"]
        assert s.get_playlist(5).kind == "created"
        do(s, edits.plan_rename(s, 5, "Upgraded"))
        changes.undo(s)
        assert s.get_playlist(5).name == "Old list"


def test_upgrades_a_version_2_change_log(tmp_path):
    from segue.store import _MIGRATIONS

    path = tmp_path / "v2.db"
    con = sqlite3.connect(path)
    con.executescript(V1_SCHEMA.replace("PRAGMA user_version = 1;", "") + _MIGRATIONS[2] + """
        INSERT INTO settings VALUES ('apply_mode', 'commit');
        INSERT INTO batches (id, description, status, created_at, changed_seq)
            VALUES (1, 'old applied', 'applied', 'x', 1), (2, 'old pending', 'pending', 'x', 2);
        PRAGMA user_version = 2;
    """)
    con.close()
    with Store(path) as s:
        assert changes.apply_mode(s) == "safe"
        assert [(b.id, b.status, b.mode) for b in changes.batches(s)] == [
            (1, "committed", "auto"), (2, "discarded", "auto"),
        ]
