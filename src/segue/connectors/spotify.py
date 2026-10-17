"""Spotify Web API: login (OAuth with PKCE) and a small client.

Written against the API as it is for development-mode apps since February 2026: playlist
contents live under ``/playlists/{id}/items``, playlists are created with ``POST
/me/playlists``, and saving or unfollowing goes through ``/me/library``. See docs/spotify.md.

Uses only the standard library. The HTTP layer is a plain function (``Transport``), so tests
can swap in a fake Spotify.
"""

from __future__ import annotations

import base64
import hashlib
import json
import secrets
import time
import urllib.parse
from collections.abc import Callable, Iterator
from http.server import BaseHTTPRequestHandler, HTTPServer

from .common import SecretFile, Transport, urllib_transport

AUTH_URL = "https://accounts.spotify.com/authorize"
TOKEN_URL = "https://accounts.spotify.com/api/token"
API_URL = "https://api.spotify.com/v1"
DEFAULT_PORT = 8765
CALLBACK_PATH = "/callback"
SCOPES = (
    "playlist-read-private",
    "playlist-read-collaborative",
    "playlist-modify-private",
    "playlist-modify-public",
    "user-library-read",
    "user-library-modify",
    "user-follow-modify",
)

MAX_RETRIES = 5
MAX_WAIT_SECONDS = 60


class SpotifyError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(f"Spotify said {status}: {message}")
        self.status = status
        self.message = message


TokenStore = SecretFile  # the Client ID and login token


# Login

def redirect_uri(port: int = DEFAULT_PORT) -> str:
    return f"http://127.0.0.1:{port}{CALLBACK_PATH}"


def pkce_pair() -> tuple[str, str]:
    """A random code verifier and its S256 challenge."""
    verifier = secrets.token_urlsafe(64)[:96]
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")
    return verifier, challenge


def authorize_url(client_id: str, redirect: str, challenge: str, state: str) -> str:
    query = urllib.parse.urlencode({
        "client_id": client_id,
        "response_type": "code",
        "redirect_uri": redirect,
        "code_challenge_method": "S256",
        "code_challenge": challenge,
        "state": state,
        "scope": " ".join(SCOPES),
    })
    return f"{AUTH_URL}?{query}"


def wait_for_callback(port: int, state: str, timeout: float = 300,
                      on_listening: Callable[[], object] | None = None) -> str:
    """Serve one login callback on 127.0.0.1 and return the authorization code.

    ``on_listening`` runs once the port is open, so the browser can't arrive too early.
    """
    result: dict = {}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            parsed = urllib.parse.urlparse(self.path)
            if parsed.path != CALLBACK_PATH:
                self.send_error(404)
                return
            params = dict(urllib.parse.parse_qsl(parsed.query))
            if params.get("state") != state:
                result["error"] = "the login answer didn't match this request (state mismatch)"
            elif "error" in params:
                result["error"] = f"Spotify refused the login: {params['error']}"
            elif "code" in params:
                result["code"] = params["code"]
            else:
                result["error"] = "Spotify's answer had no code"
            ok = "code" in result
            body = (
                "<h1>Segue is connected to Spotify.</h1><p>You can close this tab.</p>" if ok
                else f"<h1>Segue couldn't connect.</h1><p>{result['error']}</p>"
            ).encode("utf-8")
            self.send_response(200 if ok else 400)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):  # keep the terminal quiet
            pass

    try:
        server = HTTPServer(("127.0.0.1", port), Handler)
    except OSError as e:
        raise SpotifyError(0, f"can't listen on 127.0.0.1:{port} ({e.strerror}). Register another redirect "
                              f"URI, http://127.0.0.1:PORT/callback, and pass --port PORT") from None
    server.timeout = 1
    deadline = time.monotonic() + timeout
    if on_listening:
        on_listening()
    try:
        while not result and time.monotonic() < deadline:
            server.handle_request()
    finally:
        server.server_close()
    if "code" in result:
        return result["code"]
    raise SpotifyError(400, result.get("error", "timed out waiting for the login"))


def _token_request(transport: Transport, fields: dict) -> dict:
    body = urllib.parse.urlencode(fields).encode("ascii")
    status, _, raw = transport("POST", TOKEN_URL, {"Content-Type": "application/x-www-form-urlencoded"}, body)
    try:
        data = json.loads(raw or b"{}")
    except json.JSONDecodeError:
        data = {"error": raw.decode("utf-8", "replace")[:200]}
    if status != 200:
        raise SpotifyError(status, data.get("error_description") or data.get("error") or "token request failed")
    return data


def exchange_code(transport: Transport, client_id: str, code: str, redirect: str, verifier: str) -> dict:
    return _token_request(transport, {
        "grant_type": "authorization_code", "code": code, "redirect_uri": redirect,
        "client_id": client_id, "code_verifier": verifier,
    })


def token_record(client_id: str, redirect: str, response: dict, now: float, old: dict | None = None) -> dict:
    """What TokenStore keeps: a refresh response may leave out the refresh token, so keep the old one."""
    return {
        "client_id": client_id,
        "redirect_uri": redirect,
        "access_token": response["access_token"],
        "refresh_token": response.get("refresh_token") or (old or {}).get("refresh_token"),
        "expires_at": now + int(response.get("expires_in", 3600)),
        "scope": response.get("scope") or (old or {}).get("scope", ""),
    }


# The API client

class SpotifyClient:
    def __init__(self, store: TokenStore, transport: Transport = urllib_transport,
                 sleep: Callable[[float], None] = time.sleep, clock: Callable[[], float] = time.time):
        self.store = store
        self.transport = transport
        self.sleep = sleep
        self.clock = clock
        self._token = store.load()
        if not self._token or not self._token.get("access_token"):
            raise SpotifyError(401, "not connected; run: segue spotify connect --client-id ID")

    # Plumbing

    def _refresh(self) -> None:
        if not self._token.get("refresh_token"):
            raise SpotifyError(401, "the login has expired; run: segue spotify connect")
        response = _token_request(self.transport, {
            "grant_type": "refresh_token", "refresh_token": self._token["refresh_token"],
            "client_id": self._token["client_id"],
        })
        self._token = token_record(self._token["client_id"], self._token["redirect_uri"], response,
                                   self.clock(), self._token)
        self.store.save(self._token)

    def request(self, method: str, path: str, params: dict | None = None, body=None):
        """Call the API and return the decoded JSON (or None). Refreshes and retries as needed."""
        url = path if path.startswith("https://") else f"{API_URL}{path}"
        if params:
            url += ("&" if "?" in url else "?") + urllib.parse.urlencode(params)
        data = None if body is None else json.dumps(body).encode("utf-8")
        refreshed = False
        for attempt in range(MAX_RETRIES + 1):
            if self._token.get("expires_at", 0) - 60 < self.clock():
                self._refresh()
            headers = {"Authorization": f"Bearer {self._token['access_token']}"}
            if data is not None:
                headers["Content-Type"] = "application/json"
            status, response_headers, raw = self.transport(method, url, headers, data)
            if status == 401 and not refreshed:
                self._refresh()
                refreshed = True
                continue
            if status == 429 or status >= 500:
                if attempt == MAX_RETRIES:
                    break
                wait = int(response_headers.get("retry-after", "1") or 1) if status == 429 else 2 ** attempt
                if wait > MAX_WAIT_SECONDS:
                    raise SpotifyError(429, f"rate limited for {wait} seconds; try again later")
                self.sleep(wait)
                continue
            if status >= 400:
                raise SpotifyError(status, _error_message(raw))
            return json.loads(raw) if raw else None
        raise SpotifyError(status, _error_message(raw) or "gave up after retries")

    def paged(self, path: str, params: dict | None = None) -> Iterator[dict]:
        page = self.request("GET", path, params)
        while page:
            yield from page.get("items") or []
            page = self.request("GET", page["next"]) if page.get("next") else None

    # Reading

    def me(self) -> dict:
        return self.request("GET", "/me")

    def playlists(self) -> list[dict]:
        return list(self.paged("/me/playlists", {"limit": 50}))

    def playlist(self, playlist_id: str) -> dict:
        return self.request("GET", f"/playlists/{playlist_id}", {"fields": "id,name,description,snapshot_id"})

    def playlist_items(self, playlist_id: str) -> list[dict]:
        return list(self.paged(f"/playlists/{playlist_id}/items", {"limit": 50, "market": "from_token"}))

    def saved_tracks(self, limit: int | None = None) -> list[dict]:
        if limit:
            return (self.request("GET", "/me/tracks", {"limit": limit}) or {}).get("items") or []
        return list(self.paged("/me/tracks", {"limit": 50}))

    # Changing

    def create_playlist(self, name: str, description: str = "", public: bool = False) -> dict:
        return self.request("POST", "/me/playlists", body={"name": name, "description": description, "public": public})

    def add_items(self, playlist_id: str, uris: list[str], position: int | None = None) -> str:
        snapshot = None
        for start in range(0, len(uris), 100):
            body = {"uris": uris[start:start + 100]}
            if position is not None:
                body["position"] = position + start
            snapshot = self.request("POST", f"/playlists/{playlist_id}/items", body=body)["snapshot_id"]
        return snapshot

    def remove_items(self, playlist_id: str, uris: list[str], snapshot_id: str | None = None) -> str:
        snapshot = snapshot_id
        for start in range(0, len(uris), 100):
            body = {"items": [{"uri": uri} for uri in uris[start:start + 100]]}
            if snapshot:
                body["snapshot_id"] = snapshot
            snapshot = self.request("DELETE", f"/playlists/{playlist_id}/items", body=body)["snapshot_id"]
        return snapshot

    def move_items(self, playlist_id: str, range_start: int, insert_before: int, range_length: int = 1,
                   snapshot_id: str | None = None) -> str:
        body = {"range_start": range_start, "insert_before": insert_before, "range_length": range_length}
        if snapshot_id:
            body["snapshot_id"] = snapshot_id
        return self.request("PUT", f"/playlists/{playlist_id}/items", body=body)["snapshot_id"]

    def change_details(self, playlist_id: str, name: str | None = None, description: str | None = None) -> None:
        body = {k: v for k, v in (("name", name), ("description", description)) if v is not None}
        self.request("PUT", f"/playlists/{playlist_id}", body=body)

    def unfollow_playlist(self, playlist_id: str) -> None:
        """Delete (unfollow) a playlist. Spotify keeps deleted playlists recoverable for a while."""
        self.request("DELETE", "/me/library", {"uris": f"spotify:playlist:{playlist_id}"})

    def like(self, uris: list[str]) -> None:
        for start in range(0, len(uris), 40):
            self.request("PUT", "/me/library", {"uris": ",".join(uris[start:start + 40])})

    def unlike(self, uris: list[str]) -> None:
        for start in range(0, len(uris), 40):
            self.request("DELETE", "/me/library", {"uris": ",".join(uris[start:start + 40])})


def _error_message(raw: bytes) -> str:
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return (raw or b"").decode("utf-8", "replace")[:200]
    error = data.get("error")
    if isinstance(error, dict):
        return error.get("message") or json.dumps(error)
    return data.get("error_description") or str(error or data)[:200]


def connect(store: TokenStore, client_id: str, port: int = DEFAULT_PORT, open_browser: Callable[[str], object] | None = None,
            transport: Transport = urllib_transport, show: Callable[[str], None] = print,
            timeout: float = 300) -> dict:
    """Log in through the browser and save the token. Returns the Spotify user (``GET /me``)."""
    redirect = redirect_uri(port)
    verifier, challenge = pkce_pair()
    state = secrets.token_urlsafe(24)
    url = authorize_url(client_id, redirect, challenge, state)

    def start() -> None:
        show(f"Opening Spotify's login page. If it doesn't open, visit:\n{url}")
        if open_browser:
            open_browser(url)

    code = wait_for_callback(port, state, timeout, on_listening=start)
    response = exchange_code(transport, client_id, code, redirect, verifier)
    store.save(token_record(client_id, redirect, response, time.time()))
    return SpotifyClient(store, transport).me()
