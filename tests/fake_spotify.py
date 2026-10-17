"""An in-memory Spotify Web API for tests, following the documented 2026 behavior."""

from __future__ import annotations

import json
import urllib.parse


def track(n: int) -> dict:
    return {
        "id": f"t{n}", "uri": f"spotify:track:t{n}", "name": f"Song {n}",
        "artists": [{"name": f"Artist {n}"}], "duration_ms": 180000 + n,
        "album": {"name": f"Album {n}", "release_date": "2001-01-01"},
        "external_ids": {"isrc": f"ISRC{n:08d}"},
    }


class FakeSpotify:
    def __init__(self, user="me"):
        self.user = user
        self.clock = 0
        self.access_token = "access-1"
        self.refresh_token = "refresh-1"
        self.token_count = 1
        self.rate_limit_next = 0
        self.fail = {}  # (method, path) -> status, to simulate errors
        self.calls: list[tuple[str, str]] = []
        self.tracks = {f"spotify:track:t{n}": track(n) for n in range(1, 21)}
        self.playlists: dict[str, dict] = {}
        self.library: list[str] = []  # playlist IDs the user has in their library
        self.liked: list[dict] = []
        self.removes_every_copy = True
        self.next_id = 1

    # Setup helpers

    def add_playlist(self, name, uris, owner=None, collaborative=False) -> str:
        pid = f"p{self.next_id}"
        self.next_id += 1
        self.playlists[pid] = {
            "id": pid, "name": name, "description": "", "owner": {"id": owner or self.user},
            "collaborative": collaborative, "snapshot": 1,
            "items": [self._entry(uri) for uri in uris],
        }
        self.library.append(pid)
        return pid

    def like_tracks(self, uris):
        for uri in uris:
            self.liked.insert(0, self._entry(uri))

    def _entry(self, uri):
        self.clock += 1
        return {"uri": uri, "added_at": f"2026-01-01T00:00:{self.clock:02d}Z"}

    # The HTTP interface

    def __call__(self, method, url, headers, body):
        parsed = urllib.parse.urlparse(url)
        path = parsed.path.removeprefix("/v1")
        query = dict(urllib.parse.parse_qsl(parsed.query))
        self.calls.append((method, path))
        if url.startswith("https://accounts.spotify.com/api/token"):
            return self._token(dict(urllib.parse.parse_qsl(body.decode())))
        if headers.get("Authorization") != f"Bearer {self.access_token}":
            return self._json(401, {"error": {"status": 401, "message": "The access token expired"}})
        if self.rate_limit_next:
            self.rate_limit_next -= 1
            return 429, {"retry-after": "1"}, b""
        if (method, path) in self.fail:
            return self._json(self.fail[(method, path)], {"error": {"message": "simulated failure"}})
        data = json.loads(body) if body else {}
        return self._route(method, path, query, data)

    def _token(self, form):
        if form.get("grant_type") == "authorization_code" and form.get("code_verifier"):
            pass
        elif form.get("grant_type") == "refresh_token" and form.get("refresh_token") == self.refresh_token:
            pass
        else:
            return self._json(400, {"error": "invalid_grant"})
        self.token_count += 1
        self.access_token = f"access-{self.token_count}"
        return self._json(200, {"access_token": self.access_token, "token_type": "Bearer", "expires_in": 3600,
                                "refresh_token": self.refresh_token, "scope": "playlist-read-private"})

    def _route(self, method, path, query, data):
        parts = path.strip("/").split("/")
        if path == "/me":
            return self._json(200, {"id": self.user, "display_name": "Test User"})
        if path == "/me/playlists" and method == "GET":
            rows = [self._summary(self.playlists[pid]) for pid in self.library]
            return self._page(rows, query, path)
        if path == "/me/playlists" and method == "POST":
            pid = self.add_playlist(data["name"], [])
            self.playlists[pid]["description"] = data.get("description", "")
            return self._json(201, self._summary(self.playlists[pid]))
        if path == "/me/tracks":
            rows = [{"added_at": e["added_at"], "track": self.tracks[e["uri"]]} for e in self.liked]
            return self._page(rows, query, path)
        if path == "/me/library":
            uris = query["uris"].split(",")
            for uri in uris:
                if uri.startswith("spotify:playlist:") and method == "DELETE":
                    pid = uri.rsplit(":", 1)[1]
                    if pid in self.library:
                        self.library.remove(pid)
                elif method == "DELETE":
                    self.liked = [e for e in self.liked if e["uri"] != uri]
                elif method == "PUT":
                    self.like_tracks([uri])
            return 200, {}, b""
        if parts[0] == "playlists" and len(parts) >= 2:
            pl = self.playlists.get(parts[1])
            if pl is None:
                return self._json(404, {"error": {"message": "Not found"}})
            if len(parts) == 2 and method == "GET":
                return self._json(200, {"id": pl["id"], "name": pl["name"], "description": pl["description"],
                                        "snapshot_id": str(pl["snapshot"])})
            if len(parts) == 2 and method == "PUT":
                pl.update({k: v for k, v in data.items() if k in ("name", "description")})
                return 200, {}, b""
            if parts[2:] == ["items"]:
                return self._items(method, pl, query, data, path)
        return self._json(404, {"error": {"message": f"no route {method} {path}"}})

    def _items(self, method, pl, query, data, path):
        mine = pl["owner"]["id"] == self.user or pl["collaborative"]
        if method == "GET":
            if not mine:
                return self._json(403, {"error": {"status": 403, "message": "Forbidden"}})
            rows = [{"added_at": e["added_at"], "is_local": False, "item": self.tracks[e["uri"]]} for e in pl["items"]]
            return self._page(rows, query, path)
        if not mine:
            return self._json(403, {"error": {"message": "Forbidden"}})
        if method == "POST":
            new = [self._entry(uri) for uri in data["uris"]]
            at = data.get("position", len(pl["items"]))
            pl["items"][at:at] = new
        elif method == "DELETE":
            for item in data["items"]:
                if self.removes_every_copy:
                    pl["items"] = [e for e in pl["items"] if e["uri"] != item["uri"]]
                else:
                    index = next(i for i, e in enumerate(pl["items"]) if e["uri"] == item["uri"])
                    del pl["items"][index]
        elif method == "PUT":
            start, length, before = data["range_start"], data.get("range_length", 1), data["insert_before"]
            moving = pl["items"][start:start + length]
            rest = pl["items"][:start] + pl["items"][start + length:]
            at = before if before <= start else before - length
            pl["items"] = rest[:at] + moving + rest[at:]
        pl["snapshot"] += 1
        return self._json(200, {"snapshot_id": str(pl["snapshot"])})

    def _summary(self, pl):
        return {"id": pl["id"], "name": pl["name"], "owner": pl["owner"], "collaborative": pl["collaborative"],
                "items": {"total": len(pl["items"])}, "snapshot_id": str(pl["snapshot"])}

    def _page(self, rows, query, path):
        limit, offset = int(query.get("limit", 20)), int(query.get("offset", 0))
        page = rows[offset:offset + limit]
        nxt = None
        if offset + limit < len(rows):
            params = dict(query, offset=offset + limit)
            nxt = f"https://api.spotify.com/v1{path}?{urllib.parse.urlencode(params)}"
        return self._json(200, {"items": page, "next": nxt, "total": len(rows)})

    @staticmethod
    def _json(status, data):
        return status, {"content-type": "application/json"}, json.dumps(data).encode()
