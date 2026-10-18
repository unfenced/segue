import json
import socket
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

import pytest

from segue.connectors import qobuz
from segue.web.server import App, make_handler

from .fake_qobuz import FakeQobuz


@pytest.fixture
def fake():
    f = FakeQobuz()
    f.add_playlist("Gym", [1, 2, 1])
    f.add_playlist("gym (1)", [2, 3])
    f.add_playlist("Empty", [])
    return f


@pytest.fixture
def app(tmp_path, fake):
    service = qobuz.QobuzService(qobuz.QobuzClient({"app_id": fake.app_id, "user_auth_token": fake.token}, fake,
                                                   sleep=lambda s: None))
    return App(tmp_path / "segue.db", services=lambda: {"qobuz": service})


def call(app, method, path, body=None, **query):
    status, data = app.handle(method, "/api/" + path, query, body or {})
    assert status == 200, data
    return data


def test_pull_then_browse(app):
    assert call(app, "POST", "pull", {"service": "qobuz"})["added"] == 4
    state = call(app, "GET", "state")
    assert state["services"]["qobuz"]["connected"] and state["playlists"] == 4
    rows = {r["name"]: r for r in call(app, "GET", "playlists")}
    assert rows["Gym"]["same_name"] and rows["Gym"]["dupes"] == 1 and rows["Empty"]["tracks"] == 0
    detail = call(app, "GET", f"playlists/{rows['Gym']['id']}")
    assert [t["title"] for t in detail["tracks"]] == ["Song 1", "Song 2", "Song 1"]
    assert all(t["on_service"] for t in detail["tracks"])


def test_preview_apply_commit(app, fake):
    call(app, "POST", "pull", {"service": "qobuz"})
    call(app, "POST", "settings", {"duplicates": "recording"})
    preview = call(app, "POST", "preview", {"action": "merge_names", "params": {}})
    assert preview["groups"] == [{"kept": "Gym", "merged": ["gym (1)"]}] and preview["deletes"] == 1

    first = call(app, "POST", "apply", {"action": "merge_names", "params": {}})
    assert first["needs_backup"]  # deleting 'gym (1)' needs a backup
    done = call(app, "POST", "apply", {"action": "merge_names", "params": {}, "backup": "make"})
    assert done["status"] == "staged"
    assert call(app, "GET", "state")["staged"] == 1
    assert call(app, "GET", "pending")["writes"]

    assert call(app, "POST", "commit", {})["push"]["ok"]
    gym = next(pid for pid, pl in fake.playlists.items() if pl["name"] == "Gym")
    assert fake.tracks_of(gym) == ["1", "2", "1", "3"]
    assert call(app, "GET", "state")["staged"] == 0


def test_check_fixes_are_runnable(app):
    call(app, "POST", "pull", {"service": "qobuz"})
    for finding in call(app, "GET", "check"):
        for fix in [finding["action"], *finding["line_actions"]]:
            if fix:
                call(app, "POST", "preview", fix)


def test_undo_and_settings(app):
    call(app, "POST", "pull", {"service": "qobuz"})
    gym = next(r["id"] for r in call(app, "GET", "playlists") if r["name"] == "Gym")
    call(app, "POST", "apply", {"action": "rename", "params": {"id": gym, "name": "Gym!"}})
    assert call(app, "GET", "state")["undo"]
    call(app, "POST", "undo", {})
    assert next(r["name"] for r in call(app, "GET", "playlists") if r["id"] == gym) == "Gym"
    settings = call(app, "POST", "settings", {"apply_mode": "auto", "destinations": ["qobuz"]})
    assert settings["apply_mode"] == "auto" and settings["destinations"] == ["qobuz"]


def test_backups_and_restore(app):
    call(app, "POST", "pull", {"service": "qobuz"})
    call(app, "POST", "backup", {})
    [b] = call(app, "GET", "backups")
    names = [p["name"] for p in call(app, "GET", "backups/playlists", file=b["file"])]
    assert "Gym" in names
    preview = call(app, "POST", "preview", {"action": "restore", "params": {"file": b["file"], "names": ["Gym"]}})
    assert preview["nothing"]  # nothing changed since the backup
    status, data = app.handle("POST", "/api/preview", {}, {"action": "restore", "params": {"file": "../../etc/passwd"}})
    assert status == 400


def test_errors_are_reported(app):
    status, data = app.handle("POST", "/api/apply", {}, {"action": "rename", "params": {"id": 999, "name": "x"}})
    assert status == 400 and "999" in data["error"]
    assert app.handle("GET", "/api/nope", {}, {})[0] == 404


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def test_http_needs_the_token_and_the_right_host(app):
    port = free_port()
    server = ThreadingHTTPServer(("127.0.0.1", port), make_handler(app, "secret", port))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{port}"
    try:
        page = urllib.request.urlopen(base + "/").read().decode()
        assert 'content="secret"' in page
        assert urllib.request.urlopen(base + "/app.js").status == 200

        def status(path, headers=None, data=None):
            req = urllib.request.Request(base + path, data=data, headers=headers or {})
            try:
                return urllib.request.urlopen(req).status
            except urllib.error.HTTPError as e:
                return e.code

        assert status("/api/state") == 403
        assert status("/api/state", {"X-Segue-Token": "secret"}) == 200
        assert status("/api/state", {"X-Segue-Token": "secret", "Host": "evil.example:80"}) == 403
        assert status("/api/discard", {"Content-Type": "application/json"}, b"{}") == 403
        assert status("/api/discard", {"X-Segue-Token": "secret"}, b"{}") == 200
        assert status("/../pyproject.toml") == 404
    finally:
        server.shutdown()
        server.server_close()
