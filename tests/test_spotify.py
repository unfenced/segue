import base64
import hashlib
import socket
import stat
import threading
import time
import urllib.parse
import urllib.request

import pytest

from segue.connectors import spotify, spotify_probe

from .fake_spotify import FakeSpotify


def connected(tmp_path, fake, expires_in=3600):
    tokens = spotify.TokenStore(tmp_path / "spotify.json")
    tokens.save({"client_id": "cid", "redirect_uri": spotify.redirect_uri(), "access_token": fake.access_token,
                 "refresh_token": fake.refresh_token, "expires_at": time.time() + expires_in, "scope": ""})
    sleeps = []
    client = spotify.SpotifyClient(tokens, fake, sleep=sleeps.append)
    return client, tokens, sleeps


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


# Login

def test_pkce_challenge_matches_verifier():
    verifier, challenge = spotify.pkce_pair()
    assert 43 <= len(verifier) <= 128
    digest = hashlib.sha256(verifier.encode()).digest()
    assert challenge == base64.urlsafe_b64encode(digest).decode().rstrip("=")


def test_authorize_url_uses_pkce_and_all_scopes():
    url = spotify.authorize_url("cid", "http://127.0.0.1:8765/callback", "chal", "st")
    query = dict(urllib.parse.parse_qsl(urllib.parse.urlparse(url).query))
    assert query["code_challenge_method"] == "S256" and query["code_challenge"] == "chal"
    assert query["redirect_uri"] == "http://127.0.0.1:8765/callback"
    assert set(query["scope"].split()) == set(spotify.SCOPES)
    assert "client_secret" not in query


def _hit_callback(port, query, results):
    def go():
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{port}/callback?{query}", timeout=5).read()
        except urllib.error.HTTPError as e:
            results.append(e.code)
    threading.Thread(target=go).start()


def test_callback_server_returns_the_code():
    port = free_port()
    results = []
    code = spotify.wait_for_callback(port, "st", timeout=10,
                                     on_listening=lambda: _hit_callback(port, "code=abc&state=st", results))
    assert code == "abc"


def test_callback_server_rejects_a_wrong_state():
    port = free_port()
    results = []
    with pytest.raises(spotify.SpotifyError, match="state mismatch"):
        spotify.wait_for_callback(port, "st", timeout=10,
                                  on_listening=lambda: _hit_callback(port, "code=abc&state=evil", results))


def test_connect_saves_a_private_token_file(tmp_path):
    fake = FakeSpotify()
    port = free_port()
    tokens = spotify.TokenStore(tmp_path / "spotify.json")

    def browser(url):  # play the user: approve, and Spotify redirects back with a code
        state = dict(urllib.parse.parse_qsl(urllib.parse.urlparse(url).query))["state"]
        _hit_callback(port, f"code=abc&state={state}", [])

    me = spotify.connect(tokens, "cid", port, browser, transport=fake, show=lambda _: None, timeout=10)
    assert me["id"] == "me"
    saved = tokens.load()
    assert saved["access_token"] == fake.access_token and saved["refresh_token"] == "refresh-1"
    assert stat.S_IMODE((tmp_path / "spotify.json").stat().st_mode) == 0o600


# The client

def test_expired_token_is_refreshed_first(tmp_path):
    fake = FakeSpotify()
    client, tokens, _ = connected(tmp_path, fake, expires_in=-10)
    assert client.me()["id"] == "me"
    assert tokens.load()["access_token"] == fake.access_token != "access-1"


def test_401_refreshes_once_and_retries(tmp_path):
    fake = FakeSpotify()
    client, _, _ = connected(tmp_path, fake)
    fake.access_token = "rotated-elsewhere"  # the stored token stops working
    fake.token_count = 5
    assert client.me()["id"] == "me"


def test_rate_limit_waits_and_retries(tmp_path):
    fake = FakeSpotify()
    client, _, sleeps = connected(tmp_path, fake)
    fake.rate_limit_next = 2
    assert client.me()["id"] == "me"
    assert sleeps == [1, 1]


def test_paging_follows_next(tmp_path):
    fake = FakeSpotify()
    for n in range(120):
        fake.add_playlist(f"P{n}", [])
    client, _, _ = connected(tmp_path, fake)
    assert len(client.playlists()) == 120


def test_errors_carry_spotifys_message(tmp_path):
    fake = FakeSpotify()
    client, _, _ = connected(tmp_path, fake)
    with pytest.raises(spotify.SpotifyError, match="404: Not found"):
        client.playlist_items("nope")


def test_not_connected(tmp_path):
    with pytest.raises(spotify.SpotifyError, match="not connected"):
        spotify.SpotifyClient(spotify.TokenStore(tmp_path / "none.json"))


# The probe

def library(fake):
    fake.add_playlist("Mine", ["spotify:track:t1", "spotify:track:t2", "spotify:track:t3"])
    fake.add_playlist("Theirs", ["spotify:track:t4"], owner="someone")
    fake.like_tracks(["spotify:track:t5", "spotify:track:t6"])


def test_probe_reports_what_works(tmp_path):
    fake = FakeSpotify()
    library(fake)
    client, _, _ = connected(tmp_path, fake)
    checks = {c.name: c for c in spotify_probe.probe(client, write=True)}
    assert checks["List playlists"].detail == "2 playlists: 1 yours, 0 collaborative, 1 followed"
    assert "3 with an ISRC, 3 with a date added" in checks["Read your playlist"].detail
    assert checks["Read a followed playlist"].result == "no"
    assert checks["Move a track"].result == "ok" and "keeps" in checks["Move a track"].detail
    assert "every copy" in checks["Remove a duplicated track"].detail
    assert checks["Delete"].result == "ok"
    assert spotify_probe.summary(list(checks.values())) == "Everything Segue needs works with this app."
    assert [p["name"] for p in client.playlists()] == ["Mine", "Theirs"]  # the test playlist is gone


def test_probe_notices_one_copy_removal(tmp_path):
    fake = FakeSpotify()
    library(fake)
    fake.removes_every_copy = False
    client, _, _ = connected(tmp_path, fake)
    checks = {c.name: c for c in spotify_probe.probe(client, write=True)}
    assert checks["Remove a duplicated track"].detail == "removing a track removes one copy"


def test_probe_cleans_up_when_a_step_fails(tmp_path):
    fake = FakeSpotify()
    library(fake)
    fake.fail[("PUT", "/playlists/p3/items")] = 500
    client, _, _ = connected(tmp_path, fake)
    checks = spotify_probe.probe(client, write=True)
    assert any(c.result == "error" for c in checks)
    assert checks[-1].name == "Clean up" and checks[-1].result == "ok"
    assert [p["name"] for p in client.playlists()] == ["Mine", "Theirs"]


def test_read_only_probe_changes_nothing(tmp_path):
    fake = FakeSpotify()
    library(fake)
    client, _, _ = connected(tmp_path, fake)
    checks = spotify_probe.probe(client, write=False)
    assert checks[-1].result == "skipped"
    assert not any(method != "GET" for method, _ in fake.calls)
