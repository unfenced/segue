import pytest

from segue.connectors import qobuz, qobuz_probe

from .fake_qobuz import FakeQobuz

TOKEN = "t" * 40


def client(fake, sleeps=None):
    return qobuz.QobuzClient({"app_id": fake.app_id, "user_auth_token": fake.token}, fake,
                             sleep=(sleeps.append if sleeps is not None else lambda s: None))


@pytest.mark.parametrize("raw", [
    f"curl 'https://www.qobuz.com/api.json/0.2/playlist/getUserPlaylists?limit=50' "
    f"-H 'accept: */*' -H 'x-app-id: 123456789' -H 'x-user-auth-token: {TOKEN}' -H 'cookie: secret=1'",
    f"x-app-id: 123456789\nx-user-auth-token: {TOKEN}\nuser-agent: Mozilla",
    f"x-app-id\n123456789\nx-user-auth-token\n{TOKEN}",
    f'{{"app_id": "123456789", "user_auth_token": "{TOKEN}"}}',
    f"https://www.qobuz.com/api.json/0.2/user/get?app_id=123456789&user_auth_token={TOKEN}",
])
def test_parse_session_keeps_only_the_two_values(raw):
    assert qobuz.parse_session(raw) == {"app_id": "123456789", "user_auth_token": TOKEN}


def test_parse_session_explains_what_is_missing():
    with pytest.raises(ValueError, match="X-User-Auth-Token"):
        qobuz.parse_session("x-app-id: 123456789")


def test_client_pages_and_retries():
    fake = FakeQobuz()
    for n in range(130):
        fake.add_playlist(f"P{n}", [])
    sleeps = []
    c = client(fake, sleeps)
    fake.rate_limit_next = 1
    assert len(c.playlists()) == 130
    assert sleeps[0] == 1.0


def test_refused_session_says_how_to_fix_it():
    fake = FakeQobuz()
    c = qobuz.QobuzClient({"app_id": fake.app_id, "user_auth_token": "wrong" * 5}, fake)
    with pytest.raises(qobuz.QobuzError, match="segue qobuz connect"):
        c.me()


def test_read_library_kinds_tracks_and_favorites():
    fake = FakeQobuz()
    mine = fake.add_playlist("Mine", [1, 2, 1])
    fake.add_playlist("Shared", [3], owner=222, collaborative=True)
    fake.add_playlist("Theirs", [4], owner=333)
    fake.like([5, 6])
    library = qobuz.QobuzService(client(fake)).read_library()
    by_name = {p.name: p for p in library}
    assert {p.name: p.kind for p in library} == {
        "Mine": "created", "Shared": "collaborative", "Theirs": "followed", "Liked songs": "liked"}
    entries = by_name["Mine"].entries
    assert [e.track.title for e in entries] == ["Song 1", "Song 2", "Song 1"]
    assert len({e.handle for e in entries}) == 3  # each copy has its own entry ID
    assert entries[0].track.isrc == "QZ0000000001" and entries[0].track.release_date == "1991-01-01"
    assert by_name["Mine"].source.id == mine
    assert [t.title for t in by_name["Liked songs"].tracks] == ["Song 6", "Song 5"]


def test_probe_reports_what_works():
    fake = FakeQobuz()
    fake.add_playlist("Mine", [1, 2, 3])
    fake.add_playlist("Theirs", [4], owner=333)
    fake.like([5])
    checks = {c.name: c for c in qobuz_probe.probe(client(fake), write=True)}
    assert checks["List playlists"].detail == "2 playlists: 1 yours, 0 collaborative, 1 followed"
    assert checks["Remove one copy"].result == "ok"
    assert checks["Move a track"].result == "ok"
    assert checks["Rename (with emoji)"].result == "ok"
    assert checks["Delete"].result == "ok"
    assert qobuz_probe.summary(list(checks.values())) == "Everything Segue needs works with this Qobuz session."
    assert sorted(p["name"] for p in fake.playlists.values()) == ["Mine", "Theirs"]


def test_probe_notices_dropped_emoji_names_and_cleans_up_on_failure():
    fake = FakeQobuz()
    fake.add_playlist("Mine", [1, 2, 3])
    fake.update_drops_emoji = True
    checks = {c.name: c for c in qobuz_probe.probe(client(fake), write=True)}
    assert checks["Rename (with emoji)"].result == "no"

    fake = FakeQobuz()
    fake.add_playlist("Mine", [1, 2, 3])
    fake.fail["playlist/updateTracksPosition"] = 500
    checks = qobuz_probe.probe(client(fake), write=True)
    assert checks[-1].name == "Clean up" and checks[-1].result == "ok"
    assert [p["name"] for p in fake.playlists.values()] == ["Mine"]


def test_read_only_probe_writes_nothing():
    fake = FakeQobuz()
    fake.add_playlist("Mine", [1, 2, 3])
    qobuz_probe.probe(client(fake), write=False)
    assert all(method == "GET" for method, _, _ in fake.calls)
