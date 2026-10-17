from segue.cli import main
from segue.formats import json_format


def run(tmp_path, *args):
    return main(["--db", str(tmp_path / "segue.db"), *args])


def load(tmp_path, sample_playlists, capsys):
    source = tmp_path / "in.json"
    json_format.write(sample_playlists, source)
    run(tmp_path, "import", str(source))
    capsys.readouterr()
    return source


def test_import_list_show_export_backup(tmp_path, sample_playlists, capsys):
    source = tmp_path / "in.json"
    json_format.write(sample_playlists, source)

    assert run(tmp_path, "import", str(source)) == 0
    out = capsys.readouterr().out
    assert "added 'Road trip' (3 tracks)" in out
    assert "library: 3 tracks (3 new)" in out

    # Re-importing identical playlists changes nothing and isn't logged.
    run(tmp_path, "import", str(source))
    out = capsys.readouterr().out
    assert "unchanged 'Road trip'" in out and "unchanged 'Chill'" in out

    run(tmp_path, "list")
    out = capsys.readouterr().out
    assert "Road trip" in out and "spotify:pl1" in out

    run(tmp_path, "show", "road trip")
    assert "The Beatles - Hey Jude  7:11  [spotify]" in capsys.readouterr().out

    assert run(tmp_path, "export", str(tmp_path / "out.csv"), "-p", "Chill") == 0
    assert "wrote 1 playlists" in capsys.readouterr().out

    assert run(tmp_path, "export", str(tmp_path / "m3u")) == 0
    assert sorted(p.name for p in (tmp_path / "m3u").iterdir()) == [
        "Chill.m3u8", "Empty one.m3u8", "Road trip.m3u8",
    ]

    assert run(tmp_path, "backup", str(tmp_path / "bk")) == 0
    files = sorted(p.suffix for p in (tmp_path / "bk").iterdir())
    assert files == [".csv", ".json"]
    backup = next((tmp_path / "bk").glob("*.json"))
    assert len(json_format.read(backup)) == 3


def test_unknown_playlist_is_an_error(tmp_path, capsys):
    assert run(tmp_path, "show", "nope") == 1
    assert "no playlist matches 'nope'" in capsys.readouterr().err


def test_single_m3u_needs_one_playlist(tmp_path, sample_playlists, capsys):
    load(tmp_path, sample_playlists, capsys)
    assert run(tmp_path, "export", str(tmp_path / "all.m3u8")) == 1
    assert run(tmp_path, "export", str(tmp_path / "chill.m3u8"), "-p", "Chill") == 0


def test_stage_undo_commit(tmp_path, sample_playlists, capsys):
    load(tmp_path, sample_playlists, capsys)
    assert run(tmp_path, "merge", "Road trip", "--into", "Chill", "--duplicates", "recording") == 0
    assert "staged as #" in capsys.readouterr().out

    run(tmp_path, "rename", "Chill", "Everything")
    run(tmp_path, "pending")
    out = capsys.readouterr().out
    assert "net change:" in out and "'Everything': renamed from 'Chill', +1 track" in out

    run(tmp_path, "undo")
    run(tmp_path, "list")
    assert "Chill" in capsys.readouterr().out

    run(tmp_path, "commit")
    assert "final now" in capsys.readouterr().out
    assert run(tmp_path, "undo") == 1
    assert "restore a backup" in capsys.readouterr().err


def test_delete_asks_for_a_backup_first(tmp_path, sample_playlists, capsys):
    load(tmp_path, sample_playlists, capsys)
    assert run(tmp_path, "delete", "Chill") == 1
    assert "--backup to make one first" in capsys.readouterr().err

    assert run(tmp_path, "delete", "Chill", "--backup") == 0
    out = capsys.readouterr().out
    assert "backed up to" in out and "staged as #" in out

    # The merge with --delete-sources needs a backup of the current version too.
    run(tmp_path, "rename", "Empty one", "Empty")
    assert run(tmp_path, "merge", "Empty", "--into", "Road trip", "--delete-sources",
               "--duplicates", "recording") == 1
    assert run(tmp_path, "merge", "Empty", "--into", "Road trip", "--delete-sources",
               "--duplicates", "recording", "--no-backup") == 0
    assert "warning:" in capsys.readouterr().out


def test_restore_command(tmp_path, sample_playlists, capsys):
    load(tmp_path, sample_playlists, capsys)
    run(tmp_path, "backup", str(tmp_path / "bk"))
    run(tmp_path, "delete", "Chill", "--now")
    capsys.readouterr()
    backup_file = next((tmp_path / "bk").glob("*.csv"))
    assert run(tmp_path, "restore", str(backup_file), "-p", "chill") == 0
    assert "recreate 'Chill' (2 tracks)" in capsys.readouterr().out
    run(tmp_path, "list")
    assert "Chill" in capsys.readouterr().out


def test_dry_run_auto_mode_and_config(tmp_path, sample_playlists, capsys):
    load(tmp_path, sample_playlists, capsys)
    run(tmp_path, "rename", "Chill", "Calm", "--dry-run")
    assert "dry run" in capsys.readouterr().out
    run(tmp_path, "config", "apply-mode", "auto")
    run(tmp_path, "rename", "Chill", "Calm")
    assert "applied as #" in capsys.readouterr().out
    run(tmp_path, "undo")
    assert "undid" in capsys.readouterr().out
    run(tmp_path, "setup", "--duplicates", "song", "--keep", "earliest")
    out = capsys.readouterr().out
    assert "duplicates = song" in out and "keep = earliest" in out and "apply-mode = auto" in out


def test_followed_playlists_are_skipped_by_name(tmp_path, capsys):
    from segue.model import Playlist, PlaylistEntry
    from .conftest import make_track

    source = tmp_path / "in.json"
    json_format.write([
        Playlist("Gym", [PlaylistEntry(make_track("A"))]),
        Playlist("Gym", [PlaylistEntry(make_track("B"))], kind="followed"),
    ], source)
    run(tmp_path, "import", str(source))
    capsys.readouterr()
    run(tmp_path, "dedupe", "gym", "--duplicates", "recording")
    assert "skipping followed playlist 'Gym'" in capsys.readouterr().out
    run(tmp_path, "list")
    assert "followed" in capsys.readouterr().out


def test_organizing_commands(tmp_path, capsys):
    from segue.model import Playlist, PlaylistEntry
    from .conftest import make_track

    source = tmp_path / "in.json"
    json_format.write([
        Playlist("Gym", [PlaylistEntry(make_track("A")), PlaylistEntry(make_track("B"))]),
        Playlist("gym (1)", [PlaylistEntry(make_track("B")), PlaylistEntry(make_track("C"))]),
        Playlist("Empty"),
    ], source)
    run(tmp_path, "import", str(source))
    run(tmp_path, "setup", "--duplicates", "recording")
    capsys.readouterr()

    run(tmp_path, "check")
    out = capsys.readouterr().out
    assert "fix: segue delete --empty" in out and "fix: segue merge-names --dry-run" in out

    run(tmp_path, "list", "--empty")
    out = capsys.readouterr().out
    assert "Empty" in out and "Gym" not in out

    assert run(tmp_path, "merge-names", "--backup") == 0
    out = capsys.readouterr().out
    assert "'Gym' ← 'gym (1)'" in out and "staged as #" in out

    assert run(tmp_path, "delete", "--empty", "--backup") == 0
    run(tmp_path, "sort", "Gym", "--by", "title", "--reverse")
    run(tmp_path, "show", "Gym")
    out = capsys.readouterr().out
    assert out.index("- C") < out.index("- B") < out.index("- A")

    run(tmp_path, "find", "B")
    assert "'Gym' (#1) at 2" in capsys.readouterr().out

    assert run(tmp_path, "delete") == 1
    assert "choose them with a filter" in capsys.readouterr().err


def test_qobuz_pull_organize_commit(tmp_path, capsys, monkeypatch):
    from segue import cli
    from segue.connectors import qobuz as qobuz_module
    from .fake_qobuz import FakeQobuz

    fake = FakeQobuz()
    gym = fake.add_playlist("Gym", [1, 2, 1])
    dup = fake.add_playlist("gym (1)", [3])
    service = qobuz_module.QobuzService(qobuz_module.QobuzClient(
        {"app_id": fake.app_id, "user_auth_token": fake.token}, fake, sleep=lambda s: None))
    monkeypatch.setattr(cli, "_services", lambda args: {"qobuz": service})

    assert run(tmp_path, "pull", "qobuz") == 0
    assert "3 added" in capsys.readouterr().out  # two playlists and Liked songs
    run(tmp_path, "setup", "--duplicates", "recording")
    run(tmp_path, "dedupe", "Gym")
    run(tmp_path, "merge-names", "--backup")
    run(tmp_path, "pending")
    out = capsys.readouterr().out
    assert "commit will write:" in out and "Qobuz: delete 'gym (1)'" in out

    # The merge made a backup, but a fresh pull since would need another; here it's covered.
    assert run(tmp_path, "commit") == 0
    out = capsys.readouterr().out
    assert "Qobuz: wrote 'Gym'" in out and "final now" in out
    assert fake.tracks_of(gym) == ["1", "2", "3"] and dup not in fake.playlists

    # Auto mode writes right away, and asks for a backup after a new pull.
    run(tmp_path, "pull", "qobuz")
    run(tmp_path, "config", "apply-mode", "auto")
    capsys.readouterr()
    assert run(tmp_path, "rename", "Gym", "Gym 2") == 1
    assert "no backup since you last pulled from qobuz" in capsys.readouterr().err.lower()
    assert fake.playlists[gym]["name"] == "Gym"
    assert run(tmp_path, "rename", "Gym", "Gym 2", "--backup") == 0
    assert fake.playlists[gym]["name"] == "Gym 2"
