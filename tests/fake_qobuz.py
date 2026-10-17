"""An in-memory Qobuz web API for tests, following what SongMirror and others document."""

from __future__ import annotations

import json
import unicodedata
import urllib.parse


def has_emoji(text: str) -> bool:
    return any(unicodedata.category(ch) == "So" for ch in text)


class FakeQobuz:
    def __init__(self, user_id=111):
        self.user_id = user_id
        self.app_id = "123456789"
        self.token = "t" * 40
        self.clock = 1_700_000_000
        self.next_playlist = 1000
        self.next_entry = 1
        self.catalog = {str(n): self._track(n) for n in range(1, 41)}
        self.playlists: dict[str, dict] = {}
        self.library: list[str] = []
        self.favorites: list[dict] = []
        self.update_drops_emoji = False
        self.fail: dict[str, int] = {}  # endpoint -> HTTP status
        self.calls: list[tuple[str, str, dict]] = []
        self.rate_limit_next = 0

    @staticmethod
    def _track(n: int) -> dict:
        return {
            "id": n, "title": f"Song {n}", "version": None, "duration": 200 + n, "isrc": f"QZ{n:010d}",
            "performer": {"name": f"Artist {n % 7}"},
            "album": {"title": f"Album {n % 5}", "artist": {"name": f"Artist {n % 7}"},
                      "release_date_original": f"{1990 + n}-01-01"},
        }

    # Setup helpers

    def add_playlist(self, name, track_ids, owner=None, collaborative=False, description="") -> str:
        pid = str(self.next_playlist)
        self.next_playlist += 1
        self.playlists[pid] = {"id": int(pid), "name": name, "description": description,
                               "owner": {"id": owner or self.user_id, "name": "someone"},
                               "is_collaborative": collaborative, "entries": []}
        self.library.append(pid)
        self._append(pid, [str(t) for t in track_ids])
        return pid

    def like(self, track_ids):
        for t in track_ids:
            self.clock += 1
            self.favorites.insert(0, {**self.catalog[str(t)], "favorited_at": self.clock})

    def tracks_of(self, pid) -> list[str]:
        return [str(e["track"]["id"]) for e in self.playlists[pid]["entries"]]

    def entries_of(self, pid) -> list[dict]:
        return self.playlists[pid]["entries"]

    def _append(self, pid, track_ids):
        for t in track_ids:
            self.clock += 1
            self.playlists[pid]["entries"].append(
                {"playlist_track_id": self.next_entry, "created_at": self.clock, "track": self.catalog[t]})
            self.next_entry += 1

    # HTTP

    def __call__(self, method, url, headers, body):
        parsed = urllib.parse.urlparse(url)
        endpoint = parsed.path.split("/api.json/0.2/", 1)[1]
        params = dict(urllib.parse.parse_qsl(parsed.query if method == "GET" else (body or b"").decode()))
        self.calls.append((method, endpoint, params))
        if headers.get("X-App-Id") != self.app_id or headers.get("X-User-Auth-Token") != self.token:
            return self._json(401, {"status": "error", "code": 401, "message": "User authentication is required"})
        if self.rate_limit_next:
            self.rate_limit_next -= 1
            return 429, {"retry-after": "1"}, b""
        if endpoint in self.fail:
            return self._json(self.fail[endpoint], {"status": "error", "message": "simulated failure"})
        handler = getattr(self, "_" + endpoint.replace("/", "_"), None)
        if handler is None:
            return self._json(404, {"status": "error", "code": 404, "message": f"no endpoint {endpoint}"})
        return handler(params)

    def _user_get(self, p):
        return self._json(200, {"id": self.user_id, "login": "tester", "display_name": "Test User"})

    def _summary(self, pl):
        return {k: pl[k] for k in ("id", "name", "description", "owner", "is_collaborative")} | {
            "tracks_count": len(pl["entries"])}

    def _playlist_getUserPlaylists(self, p):
        rows = [self._summary(self.playlists[pid]) for pid in self.library]
        return self._page("playlists", rows, p)

    def _playlist_get(self, p):
        pl = self.playlists.get(p["playlist_id"])
        if pl is None:
            return self._json(404, {"status": "error", "code": 404, "message": "Playlist not found"})
        rows = [{**e["track"], "playlist_track_id": e["playlist_track_id"], "created_at": e["created_at"]}
                for e in pl["entries"]]
        data = json.loads(self._page("tracks", rows, p)[2])
        return self._json(200, self._summary(pl) | data)

    def _favorite_getUserFavorites(self, p):
        return self._page("tracks", self.favorites, p)

    def _playlist_create(self, p):
        name = "" if has_emoji(p["name"]) else p["name"]
        pid = self.add_playlist(name, [], description=p.get("description", ""))
        return self._json(200, self._summary(self.playlists[pid]))

    def _playlist_update(self, p):
        pl = self.playlists[p["playlist_id"]]
        if "name" in p:
            pl["name"] = "" if (self.update_drops_emoji and has_emoji(p["name"])) else p["name"]
        if "description" in p:
            pl["description"] = p["description"]
        return self._json(200, self._summary(pl))

    def _playlist_delete(self, p):
        self.playlists.pop(p["playlist_id"], None)
        if p["playlist_id"] in self.library:
            self.library.remove(p["playlist_id"])
        return self._json(200, {"status": "success"})

    def _playlist_unsubscribe(self, p):
        self.library.remove(p["playlist_id"])
        return self._json(200, {"status": "success"})

    def _playlist_addTracks(self, p):
        self._append(p["playlist_id"], p["track_ids"].split(","))
        return self._json(200, self._summary(self.playlists[p["playlist_id"]]))

    def _playlist_deleteTracks(self, p):
        drop = set(p["playlist_track_ids"].split(","))
        pl = self.playlists[p["playlist_id"]]
        pl["entries"] = [e for e in pl["entries"] if str(e["playlist_track_id"]) not in drop]
        return self._json(200, self._summary(pl))

    def _playlist_updateTracksPosition(self, p):
        pl = self.playlists[p["playlist_id"]]
        ids = p["playlist_track_ids"].split(",")
        moving = [e for e in pl["entries"] if str(e["playlist_track_id"]) in ids]
        rest = [e for e in pl["entries"] if str(e["playlist_track_id"]) not in ids]
        at = int(p["insert_before"])
        pl["entries"] = rest[:at] + moving + rest[at:]
        return self._json(200, self._summary(pl))

    def _favorite_create(self, p):
        have = {str(t["id"]) for t in self.favorites}
        self.like([t for t in p["track_ids"].split(",") if t not in have])
        return self._json(200, {"status": "success"})

    def _favorite_delete(self, p):
        drop = set(p["track_ids"].split(","))
        self.favorites = [t for t in self.favorites if str(t["id"]) not in drop]
        return self._json(200, {"status": "success"})

    def _page(self, key, rows, p):
        limit, offset = int(p.get("limit", 50)), int(p.get("offset", 0))
        return self._json(200, {key: {"items": rows[offset:offset + limit], "total": len(rows),
                                      "limit": limit, "offset": offset}})

    @staticmethod
    def _json(status, data):
        return status, {"content-type": "application/json"}, json.dumps(data).encode()
