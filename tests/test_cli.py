from segue.cli import main
from segue.formats import json_format


def run(tmp_path, *args):
    return main(["--db", str(tmp_path / "segue.db"), *args])


def test_import_list_show_export_backup(tmp_path, sample_playlists, capsys):
    source = tmp_path / "in.json"
    json_format.write(sample_playlists, source)

    assert run(tmp_path, "import", str(source)) == 0
    out = capsys.readouterr().out
    assert "added 'Road trip' (3 tracks)" in out
    assert "library: 3 tracks (3 new)" in out

    # Re-importing: playlists with a source are replaced, identical ones are skipped.
    run(tmp_path, "import", str(source))
    out = capsys.readouterr().out
    assert "replaced 'Road trip'" in out and "unchanged 'Chill'" in out

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
    source = tmp_path / "in.json"
    json_format.write(sample_playlists, source)
    run(tmp_path, "import", str(source))
    assert run(tmp_path, "export", str(tmp_path / "all.m3u8")) == 1
    assert run(tmp_path, "export", str(tmp_path / "chill.m3u8"), "-p", "Chill") == 0
