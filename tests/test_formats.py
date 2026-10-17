import pytest

from segue.formats import csv_format, json_format, m3u_format, read_playlists
from segue.model import ServiceRef
from segue.services import parse_track_link


def summary(playlists):
    """Comparable view of playlists: everything a backup must preserve."""
    return [
        (
            p.name, p.description, p.source,
            [
                (t.title, t.artists, t.album, t.duration_ms, t.isrc,
                 sorted((r.service, r.id) for r in t.refs), e.added_at)
                for e in p.entries
                for t in [e.track]
            ],
        )
        for p in playlists
    ]


def test_json_round_trip(tmp_path, sample_playlists):
    path = tmp_path / "backup.json"
    json_format.write(sample_playlists, path)
    assert summary(json_format.read(path)) == summary(sample_playlists)


def test_json_rejects_other_files(tmp_path):
    path = tmp_path / "x.json"
    path.write_text('{"hello": 1}')
    with pytest.raises(ValueError, match="Not a Segue"):
        json_format.read(path)


def test_csv_round_trip(tmp_path, sample_playlists):
    path = tmp_path / "backup.csv"
    csv_format.write(sample_playlists, path)
    loaded = csv_format.read(path)
    # CSV keeps service IDs but rebuilds URLs, so compare without them.
    assert summary(loaded) == summary(sample_playlists)
    assert [p.name for p in loaded] == ["Road trip", "Chill", "Empty one"]


def test_csv_reads_exportify(tmp_path):
    path = tmp_path / "Liked Songs.csv"
    path.write_text(
        '"Track URI","Track Name","Artist Name(s)","Album Name","Duration (ms)","Added At","ISRC"\n'
        '"spotify:track:4uLU6hMCjMI75M1A2tKUQC","Never Gonna Give You Up","Rick Astley",'
        '"Whenever You Need Somebody","213573","2020-01-01T00:00:00Z","GBARL9300135"\n'
        '"spotify:track:abc","Duet","Tyler, The Creator,Frank Ocean","LP","1000","",""\n',
        encoding="utf-8",
    )
    [playlist] = csv_format.read(path)
    assert playlist.name == "Liked Songs"
    first, second = playlist.tracks
    assert first.title == "Never Gonna Give You Up"
    assert first.refs == [ServiceRef("spotify", "4uLU6hMCjMI75M1A2tKUQC",
                                     "https://open.spotify.com/track/4uLU6hMCjMI75M1A2tKUQC")]
    assert first.duration_ms == 213573 and first.isrc == "GBARL9300135"
    assert playlist.entries[0].added_at == "2020-01-01T00:00:00Z"
    assert second.artists == ["Tyler", "The Creator", "Frank Ocean"]


def test_csv_reads_semicolons_and_mmss(tmp_path):
    path = tmp_path / "dutch-excel.csv"
    path.write_text("Playlist;Title;Artist;Duration\nZomer;Zoutelande;BLØF; Geike Arnaert;3:21\n",
                    encoding="utf-8")
    # Semicolon-separated file where an artist cell can't hold a semicolon; only the
    # first artist survives, which is the honest result for a malformed row.
    [playlist] = csv_format.read(path)
    assert playlist.name == "Zomer"
    assert playlist.tracks[0].title == "Zoutelande"
    assert playlist.tracks[0].artists == ["BLØF"]


def test_csv_semicolon_file_with_comma_artists(tmp_path):
    path = tmp_path / "x.csv"
    path.write_text("playlist;title;artists;duration\nZomer;Zoutelande;BLØF, Geike Arnaert;3:21\n",
                    encoding="utf-8")
    [playlist] = csv_format.read(path)
    assert playlist.tracks[0].artists == ["BLØF", "Geike Arnaert"]
    assert playlist.tracks[0].duration_ms == 201000


def test_csv_honours_position_column(tmp_path):
    path = tmp_path / "x.csv"
    path.write_text("playlist,position,title\nP,2,B\nP,1,A\n", encoding="utf-8")
    [playlist] = csv_format.read(path)
    assert [t.title for t in playlist.tracks] == ["A", "B"]


def test_csv_needs_a_title_column(tmp_path):
    path = tmp_path / "x.csv"
    path.write_text("foo,bar\n1,2\n", encoding="utf-8")
    with pytest.raises(ValueError, match="no title column"):
        csv_format.read(path)


def test_m3u_write_and_read(tmp_path, sample_playlists):
    road_trip = sample_playlists[0]
    path = tmp_path / "road.m3u8"
    skipped = m3u_format.write(road_trip, path, prefer=["spotify"])
    assert skipped == 1  # Café del Mar has no link or file
    text = path.read_text(encoding="utf-8")
    assert text.startswith("#EXTM3U\n#PLAYLIST:Road trip\n")
    assert "#EXTINF:431,The Beatles - Hey Jude" in text
    assert "https://open.spotify.com/track/0aym2LBJBk9DAYuHHutrIl" in text

    loaded = m3u_format.read(path)
    assert loaded.name == "Road trip"
    assert [(t.title, t.duration_ms) for t in loaded.tracks] == [("Hey Jude", 431000), ("Tyler's Song", 200000)]
    assert loaded.tracks[0].album == "Hey Jude"
    assert loaded.tracks[1].refs[0].service == "tidal"


def test_m3u_reads_local_files_and_latin1(tmp_path):
    path = tmp_path / "old.m3u"
    path.write_bytes("#EXTM3U\n#EXTINF:180,Édith Piaf - La vie en rose\nmusic/piaf.mp3\nplain.flac\n".encode("latin-1"))
    playlist = read_playlists(path)[0]
    assert playlist.name == "old"
    first, second = playlist.tracks
    assert (first.artists, first.title) == (["Édith Piaf"], "La vie en rose")
    assert first.refs == [ServiceRef("file", str((tmp_path / "music/piaf.mp3").resolve()))]
    assert second.title == "plain" and second.duration_ms is None


@pytest.mark.parametrize("link, expected", [
    ("spotify:track:abc123", ("spotify", "abc123")),
    ("https://open.spotify.com/track/abc123?si=x", ("spotify", "abc123")),
    ("https://tidal.com/browse/track/987", ("tidal", "987")),
    ("https://www.deezer.com/en/track/555", ("deezer", "555")),
    ("https://music.apple.com/nl/album/x/123?i=456", ("apple", "456")),
    ("https://music.youtube.com/watch?v=dQw4w9WgXcQ", ("youtube", "dQw4w9WgXcQ")),
    ("https://youtu.be/dQw4w9WgXcQ", ("youtube", "dQw4w9WgXcQ")),
    ("https://example.com/song.mp3", None),
    ("not a link", None),
])
def test_parse_track_link(link, expected):
    ref = parse_track_link(link)
    assert (ref and (ref.service, ref.id)) == expected
