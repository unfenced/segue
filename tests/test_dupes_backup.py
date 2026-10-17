import pytest

from segue import backup, changes, dupes, edits
from segue.formats import csv_format, json_format
from segue.model import EntryState, Playlist, PlaylistEntry, ServiceRef

from .conftest import make_track


@pytest.mark.parametrize("title, core", [
    ("Hey Jude", "hey jude"),
    ("Hey Jude - Remastered 2015", "hey jude"),
    ("Hey Jude (Remastered 2009)", "hey jude"),
    ("Don't Stop Me Now - Live at Wembley '86", "don't stop me now"),
    ("Song (feat. Someone)", "song"),
    ("Song [Radio Edit]", "song"),
    ("Song feat. Someone", "song"),
    ("Song (Live) - 2011 Remaster", "song"),
    ("(I Can't Get No) Satisfaction", "(i can't get no) satisfaction"),
    ("Part 2 - Live", "part 2"),
])
def test_core_title(title, core):
    assert dupes.core_title(title) == core


def test_keep_rules():
    entries = [EntryState(1, "2022-01-01"), EntryState(1, "2020-01-01"), EntryState(1, "2024-01-01")]
    key = lambda e: e.track_id
    assert dupes.duplicate_positions(entries, key, "first") == [1, 2]
    assert dupes.duplicate_positions(entries, key, "earliest") == [0, 2]
    assert dupes.duplicate_positions(entries, key, "latest") == [0, 1]
    undated = [EntryState(1), EntryState(1)]
    assert dupes.duplicate_positions(undated, key, "latest") == [1]


def test_exact_level_keeps_different_service_ids(store):
    a = make_track("Song", duration_ms=200000, refs=[ServiceRef("spotify", "id1")])
    b = make_track("Song", duration_ms=200000, refs=[ServiceRef("spotify", "id2")])
    pid, _ = store.save_playlist(Playlist("P", [PlaylistEntry(a), PlaylistEntry(b)]))
    assert edits.plan_dedupe(store, [pid], level="exact")[1] == []
    assert edits.plan_dedupe(store, [pid], level="recording")[1] != []


def test_settings(store):
    assert not dupes.is_configured(store)
    assert dupes.level(store) == "recording"
    dupes.set_level(store, "song")
    dupes.set_keep(store, "latest")
    assert (dupes.level(store), dupes.keep(store)) == ("song", "latest")
    with pytest.raises(ValueError):
        dupes.set_level(store, "fuzzy")


def test_backup_coverage_follows_revisions(store, tmp_path):
    pid, _ = store.save_playlist(Playlist("P", [PlaylistEntry(make_track("A"))]))
    assert backup.not_backed_up(store, [pid]) == [pid]
    paths = backup.write_backup(store, tmp_path / "bk")
    assert sorted(p.suffix for p in paths) == [".csv", ".json"]
    assert backup.not_backed_up(store, [pid]) == []
    changes.submit(store, *edits.plan_rename(store, pid, "Changed"))
    assert backup.not_backed_up(store, [pid]) == [pid]


@pytest.mark.parametrize("fmt", [json_format, csv_format])
def test_restore_from_backup(store, tmp_path, fmt):
    keep, _ = store.save_playlist(Playlist("Keep", [PlaylistEntry(make_track("A")), PlaylistEntry(make_track("B"))]))
    gone, _ = store.save_playlist(Playlist("Gone", [PlaylistEntry(make_track("C"), "2021-05-05")]))
    path = tmp_path / ("backup.json" if fmt is json_format else "backup.csv")
    fmt.write(store.all_playlists(), path, store.library_id)
    originals = {pid: store.playlist_state(pid) for pid in (keep, gone)}

    changes.submit(store, *edits.plan_remove(store, keep, [1]))
    changes.submit(store, *edits.plan_delete(store, [gone]))
    changes.commit(store)

    description, ops, notes = backup.plan_restore(store, fmt.read(path))
    changes.submit(store, description, ops)
    assert store.playlist_state(keep) == originals[keep]
    assert store.playlist_state(gone) == originals[gone]  # recreated under its old ID

    # Restoring again finds nothing to do.
    _, ops, notes = backup.plan_restore(store, fmt.read(path))
    assert ops == [] and len(notes) == 2


def test_restore_from_another_library_adds_new_playlists(store, tmp_path):
    from segue.store import Store

    with Store(tmp_path / "other.db") as other:
        other.save_playlist(Playlist("Theirs", [PlaylistEntry(make_track("X"))]))
        json_format.write(other.all_playlists(), tmp_path / "other.json", other.library_id)
    mine, _ = store.save_playlist(Playlist("Mine", [PlaylistEntry(make_track("Y"))]))
    _, ops, _ = backup.plan_restore(store, json_format.read(tmp_path / "other.json"))
    [op] = ops
    assert op.playlist_id is None  # never overwrites a playlist that happens to share an ID
    assert store.get_playlist(mine).name == "Mine"
