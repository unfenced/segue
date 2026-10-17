import pytest

from segue.model import Playlist, PlaylistEntry, ServiceRef, Track
from segue.store import Store


@pytest.fixture
def store():
    with Store() as s:
        yield s


def make_track(title="Song", artists=("Artist",), **kw) -> Track:
    return Track(title=title, artists=list(artists), **kw)


@pytest.fixture
def sample_playlists() -> list[Playlist]:
    a = make_track("Hey Jude", ["The Beatles"], album="Hey Jude", duration_ms=431000,
                   isrc="GBAYE0601690",
                   refs=[ServiceRef("spotify", "0aym2LBJBk9DAYuHHutrIl")])
    b = make_track("Tyler's Song", ["Tyler, The Creator", "Frank Ocean"], duration_ms=200500,
                   refs=[ServiceRef("tidal", "12345", "https://tidal.com/browse/track/12345")])
    c = make_track("Café del Mar", ["Energy 52"], album="Café del Mar", duration_ms=None)
    return [
        Playlist("Road trip", [PlaylistEntry(a, "2024-05-01T10:00:00Z"), PlaylistEntry(b), PlaylistEntry(c)],
                 description="Long drives", source=ServiceRef("spotify", "pl1", "https://open.spotify.com/playlist/pl1")),
        Playlist("Chill", [PlaylistEntry(c), PlaylistEntry(a)]),
        Playlist("Empty one"),
    ]
