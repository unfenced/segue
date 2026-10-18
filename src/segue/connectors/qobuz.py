"""Qobuz: the web player's own API, signed in with a session copied from it.

Qobuz only issues API keys to business partners, so like SongMirror (whose research this
follows; credit in the README) Segue uses the two headers the web player sends with every
request: ``X-App-Id`` and ``X-User-Auth-Token``. Everything else in a copied request is
thrown away before anything is saved. This is unofficial and can break when Qobuz changes
its web player; see docs/qobuz.md.

Endpoints, under https://www.qobuz.com/api.json/0.2/:

- read: ``user/get``, ``playlist/getUserPlaylists``, ``playlist/get`` (``extra=tracks``),
  ``favorite/getUserFavorites`` (``type=tracks``)
- write: ``playlist/create``, ``playlist/update``, ``playlist/delete``,
  ``playlist/unsubscribe``, ``playlist/addTracks``, ``playlist/deleteTracks`` (by
  ``playlist_track_id``), ``playlist/updateTracksPosition``, ``favorite/create``,
  ``favorite/delete``

Every playlist entry has its own ``playlist_track_id``, so one copy of a duplicated track
can be removed or moved without touching the others.
"""

from __future__ import annotations

import json
import re
import time
import unicodedata
import urllib.parse
from collections.abc import Callable
from datetime import datetime, timezone

from ..model import Playlist, PlaylistEntry, PlaylistState, ServiceRef, Track
from .common import SecretFile, Transport, urllib_transport

API_URL = "https://www.qobuz.com/api.json/0.2"
SERVICE = "qobuz"
LIKED_ID = "@favorites"  # the source ID Segue gives Qobuz favorites (Liked songs)
LIKED_NAME = "Qobuz favorites"  # not "Liked songs": many libraries have a playlist by that name
PAGE = 100
WRITE_CHUNK = 50
MAX_RETRIES = 4
PAUSE = 0.25  # between writes, to stay polite


class QobuzError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(f"Qobuz said {status}: {message}")
        self.status = status
        self.message = message


# The session

# Each value can come as a header ("X-App-Id: 123", or the name and value on two lines) or as a
# query parameter ("?app_id=123").
_PATTERNS = {
    "app_id": re.compile(r"""(?:x-app-id['"]?\s*[:=]?\s*['"]?|[?&]app_id=)(\d{6,})""", re.IGNORECASE),
    "user_auth_token": re.compile(
        r"""(?:x-user-auth-token['"]?\s*[:=]?\s*['"]?|[?&]user_auth_token=)([A-Za-z0-9_\-.~]{20,})""",
        re.IGNORECASE),
    "user_id": re.compile(r"""(?:x-user-id['"]?\s*[:=]?\s*['"]?|[?&]user_id=)(\d+)""", re.IGNORECASE),
}


def parse_session(raw: str) -> dict[str, str]:
    """Pull ``app_id`` and ``user_auth_token`` (and ``user_id`` if present) out of a copied request.

    Accepts a "Copy as cURL" command, copied request headers, the header table from the
    Network panel, a URL with ``app_id=…&user_auth_token=…``, or JSON with those keys.
    """
    raw = (raw or "").strip()
    if not raw:
        raise ValueError("paste a request copied from the Qobuz web player (see docs/qobuz.md)")
    found: dict[str, str] = {}
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        data = None
    if isinstance(data, dict):
        for key in ("app_id", "user_auth_token", "user_id"):
            if data.get(key):
                found[key] = str(data[key]).strip()
    for key, pattern in _PATTERNS.items():
        if key not in found and (m := pattern.search(raw)):
            found[key] = m.group(1)
    missing = [name for key, name in (("app_id", "X-App-Id"), ("user_auth_token", "X-User-Auth-Token"))
               if not found.get(key)]
    if missing:
        raise ValueError(f"couldn't find {' and '.join(missing)} in what you pasted. Copy a request to "
                         "www.qobuz.com/api.json/0.2 from the web player's Network panel (see docs/qobuz.md)")
    return found


# The client

class QobuzClient:
    def __init__(self, session: SecretFile, transport: Transport = urllib_transport,
                 sleep: Callable[[float], None] = time.sleep):
        self.session = session.load() if isinstance(session, SecretFile) else session
        if not self.session or not self.session.get("user_auth_token"):
            raise QobuzError(401, "not connected; run: segue qobuz connect")
        self.transport = transport
        self.sleep = sleep

    def request(self, method: str, endpoint: str, params: dict | None = None) -> dict:
        params = {k: v for k, v in (params or {}).items() if v is not None}
        url = f"{API_URL}/{endpoint}"
        body = None
        headers = {"X-App-Id": self.session["app_id"], "X-User-Auth-Token": self.session["user_auth_token"]}
        if method == "GET":
            if params:
                url += "?" + urllib.parse.urlencode(params)
        else:
            body = urllib.parse.urlencode(params).encode("utf-8")
            headers["Content-Type"] = "application/x-www-form-urlencoded"
        for attempt in range(MAX_RETRIES + 1):
            status, response_headers, raw = self.transport(method, url, headers, body)
            if status == 429 or (status >= 500 and method == "GET"):
                if attempt < MAX_RETRIES:
                    wait = float(response_headers.get("retry-after") or 2 ** attempt)
                    self.sleep(min(wait, 30))
                    continue
            try:
                data = json.loads(raw) if raw else {}
            except json.JSONDecodeError:
                data = {"message": raw.decode("utf-8", "replace")[:200]}
            if status in (401, 403):
                raise QobuzError(status, "the session was refused; it may have expired. "
                                         "Copy a fresh request and run: segue qobuz connect")
            if status >= 400:
                raise QobuzError(status, str(data.get("message") or data)[:200])
            if isinstance(data, dict) and data.get("status") == "error":
                raise QobuzError(int(data.get("code") or 400), str(data.get("message")))
            return data
        raise QobuzError(status, "gave up after retries")

    def _paged(self, endpoint: str, key: str, params: dict) -> tuple[dict, list[dict]]:
        """All items of a paged list; returns the first page's container too."""
        items: list[dict] = []
        first = None
        offset = 0
        while True:
            data = self.request("GET", endpoint, {**params, "limit": PAGE, "offset": offset})
            first = first or data
            container = data.get(key) or {}
            page = container.get("items") or []
            items.extend(page)
            offset += len(page)
            total = container.get("total")
            if not page or (total is not None and offset >= int(total)) or (total is None and len(page) < PAGE):
                return first, items

    # Reading

    def me(self) -> dict:
        data = self.request("GET", "user/get")
        return data.get("user") or data

    def playlists(self) -> list[dict]:
        return self._paged("playlist/getUserPlaylists", "playlists", {})[1]

    def playlist(self, playlist_id: str) -> tuple[dict, list[dict]]:
        """The playlist's details and all its entries, in order."""
        return self._paged("playlist/get", "tracks", {"playlist_id": playlist_id, "extra": "tracks"})

    def favorites(self) -> list[dict]:
        return self._paged("favorite/getUserFavorites", "tracks", {"type": "tracks"})[1]

    # Changing

    def create_playlist(self, name: str, description: str = "", public: bool = False) -> dict:
        return self.request("POST", "playlist/create", {"name": name, "description": description or "",
                                                         "is_public": "true" if public else "false",
                                                         "is_collaborative": "false"})

    def update_playlist(self, playlist_id: str, name: str | None = None, description: str | None = None) -> None:
        self.request("POST", "playlist/update", {"playlist_id": playlist_id, "name": name,
                                                  "description": description})

    def delete_playlist(self, playlist_id: str) -> None:
        self.request("POST", "playlist/delete", {"playlist_id": playlist_id})

    def unsubscribe(self, playlist_id: str) -> None:
        self.request("POST", "playlist/unsubscribe", {"playlist_id": playlist_id})

    def add_tracks(self, playlist_id: str, track_ids: list[str]) -> None:
        """Append tracks. A track that appears twice goes in a later request: Qobuz can drop a
        repeated ID within one request."""
        remaining = list(track_ids)
        while remaining:
            seen: set[str] = set()
            this_round, later = [], []
            for track_id in remaining:
                (later if track_id in seen else this_round).append(track_id)
                seen.add(track_id)
            for chunk in _chunks(this_round):
                self.request("POST", "playlist/addTracks", {"playlist_id": playlist_id,
                                                             "track_ids": ",".join(chunk), "no_duplicate": "false"})
                self.sleep(PAUSE)
            remaining = later

    def delete_tracks(self, playlist_id: str, playlist_track_ids: list[str]) -> None:
        for chunk in _chunks(playlist_track_ids):
            self.request("POST", "playlist/deleteTracks", {"playlist_id": playlist_id,
                                                            "playlist_track_ids": ",".join(chunk)})
            self.sleep(PAUSE)

    def move_tracks(self, playlist_id: str, playlist_track_ids: list[str], insert_before: int) -> None:
        self.request("POST", "playlist/updateTracksPosition", {
            "playlist_id": playlist_id, "playlist_track_ids": ",".join(playlist_track_ids),
            "insert_before": insert_before,
        })
        self.sleep(PAUSE)

    def favorite(self, track_ids: list[str]) -> None:
        for chunk in _chunks(track_ids):
            self.request("POST", "favorite/create", {"track_ids": ",".join(chunk)})
            self.sleep(PAUSE)

    def unfavorite(self, track_ids: list[str]) -> None:
        for chunk in _chunks(track_ids):
            self.request("POST", "favorite/delete", {"track_ids": ",".join(chunk)})
            self.sleep(PAUSE)


def _chunks(values: list[str], size: int = WRITE_CHUNK):
    for start in range(0, len(values), size):
        yield values[start:start + size]


# Qobuz data as Segue data

def to_track(item: dict) -> Track:
    title = item.get("title") or ""
    version = (item.get("version") or "").strip()
    if version and version.lower() not in title.lower():
        title = f"{title} ({version})"
    album = item.get("album") or {}
    artist = (item.get("performer") or {}).get("name") or (album.get("artist") or {}).get("name")
    duration = item.get("duration")
    released = album.get("release_date_original") or album.get("release_date_stream")
    if not released and isinstance(album.get("released_at"), (int, float)):
        released = datetime.fromtimestamp(album["released_at"], timezone.utc).date().isoformat()
    track_id = str(item["id"])
    return Track(
        title=title,
        artists=[artist] if artist else [],
        album=album.get("title"),
        duration_ms=int(duration * 1000) if isinstance(duration, (int, float)) else None,
        isrc=item.get("isrc"),
        release_date=released,
        refs=[ServiceRef(SERVICE, track_id, f"https://open.qobuz.com/track/{track_id}")],
    )


def _when(value) -> str | None:
    if isinstance(value, (int, float)) and value > 0:
        return datetime.fromtimestamp(value, timezone.utc).isoformat(timespec="seconds")
    return str(value) if value else None


def to_entry(item: dict) -> PlaylistEntry:
    handle = item.get("playlist_track_id")
    return PlaylistEntry(to_track(item), _when(item.get("created_at") or item.get("favorited_at")),
                         str(handle) if handle is not None else None)


def kind_of(playlist: dict, my_id: str | None) -> str:
    owner = str((playlist.get("owner") or {}).get("id") or "")
    if my_id and owner == my_id:
        return "created"
    if playlist.get("is_collaborative"):
        return "collaborative"
    return "followed" if my_id else "created"


def plain_name(name: str) -> str:
    """A name without emoji: Qobuz saves an empty name when creating one with emoji."""
    plain = "".join(ch for ch in name if unicodedata.category(ch) not in ("So", "Sk", "Cf")
                    and not "︀" <= ch <= "️")
    return " ".join(plain.split()) or "Untitled"


class QobuzService:
    """What the commit and pull code need from Qobuz, in Segue's terms."""

    name = SERVICE
    display = "Qobuz"

    def __init__(self, client: QobuzClient):
        self.client = client
        self._my_id: str | None = None

    def wait(self) -> None:
        """Pause briefly, e.g. while Qobuz still serves a cached copy of a playlist."""
        self.client.sleep(1.5)

    def my_id(self) -> str | None:
        if self._my_id is None:
            try:
                self._my_id = str(self.client.me().get("id") or "") or None
            except QobuzError:
                self._my_id = self.client.session.get("user_id")
        return self._my_id

    def read_library(self) -> list[Playlist]:
        """Every playlist in your Qobuz library, with tracks, plus your favorite tracks."""
        result = []
        for summary in self.client.playlists():
            result.append(self.read_playlist(str(summary["id"]), summary=summary))
        result.append(self.read_liked())
        return result

    def read_playlist(self, remote_id: str, summary: dict | None = None) -> Playlist:
        if remote_id == LIKED_ID:
            return self.read_liked()
        details, items = self.client.playlist(remote_id)
        info = summary or details
        return Playlist(
            name=info.get("name") or details.get("name") or "",
            entries=[to_entry(item) for item in items if item.get("id") is not None],
            description=info.get("description") or None,
            source=ServiceRef(SERVICE, str(remote_id), f"https://open.qobuz.com/playlist/{remote_id}"),
            kind=kind_of(info, self.my_id()),
        )

    def read_liked(self) -> Playlist:
        items = self.client.favorites()
        return Playlist(LIKED_NAME, [to_entry(item) for item in items if item.get("id") is not None],
                        source=ServiceRef(SERVICE, LIKED_ID), kind="liked")

    def write(self, remote_id: str | None, remote: PlaylistState | None, target: PlaylistState | None,
              track_id: Callable[[int, str | None, str | None], str | None]) -> tuple[str | None, list[str]]:
        """Make the Qobuz playlist look like ``target``. Returns its (possibly new) ID and notes.

        ``remote`` is what Qobuz had at the last read; ``track_id(track_id, ref_service, ref_id)``
        gives the Qobuz track ID for a library track, or None if it has no match on Qobuz.
        """
        notes: list[str] = []
        kind = (target or remote).kind
        if target is None:
            if kind == "followed":
                self.client.unsubscribe(remote_id)
            elif kind != "liked":
                self.client.delete_playlist(remote_id)
            return None, notes
        if kind == "liked":
            return LIKED_ID, self._write_liked(remote, target, track_id)

        if remote_id is None:
            plain = plain_name(target.name)
            remote_id = str(self.client.create_playlist(plain, target.description or "")["id"])
            remote = PlaylistState(plain, target.description, entries=(), kind=target.kind)
            self.client.sleep(PAUSE)
        notes += self._write_details(remote_id, remote, target)

        # Entries are matched by Qobuz's own entry ID (handle).
        target_handles = {e.handle for e in target.entries if e.handle}
        remote_handles = [e.handle for e in remote.entries if e.handle]
        drop = [h for h in remote_handles if h not in target_handles]
        if drop:
            self.client.delete_tracks(remote_id, drop)
        kept = set(remote_handles) - set(drop)

        to_add: list[int] = []  # target positions of entries Qobuz doesn't have yet
        add_ids: list[str] = []
        unmatched = 0
        for position, entry in enumerate(target.entries):
            if entry.handle in kept:
                continue
            qobuz_id = track_id(entry.track_id, entry.ref_service, entry.ref_id)
            if qobuz_id is None:
                unmatched += 1
                continue
            to_add.append(position)
            add_ids.append(qobuz_id)
        if add_ids:
            self.client.add_tracks(remote_id, add_ids)
        if unmatched:
            notes.append(f"{unmatched} track{'s' if unmatched != 1 else ''} have no match on Qobuz yet; "
                         "they stay in Segue's copy")

        # Read back to learn the new entries' IDs, then put everything in the target order.
        # New entries are paired with target positions by track ID, in order.
        items = self._read_settled(remote_id, expected=len(kept) + len(add_ids))
        current = [str(item.get("playlist_track_id")) for item in items]
        fresh: dict[str, list[str]] = {}
        for item in items:
            handle = str(item.get("playlist_track_id"))
            if handle not in kept:
                fresh.setdefault(str(item.get("id")), []).append(handle)
        assigned = {}
        for position, qobuz_id in zip(to_add, add_ids):
            if fresh.get(qobuz_id):
                assigned[position] = fresh[qobuz_id].pop(0)
        missing = len(add_ids) - len(assigned)
        if missing:
            notes.append(f"Qobuz didn't keep {missing} of the added tracks (it may refuse a track that's "
                         "already in the playlist)")
        desired = []
        for position, entry in enumerate(target.entries):
            if entry.handle in kept:
                desired.append(entry.handle)
            elif position in assigned:
                desired.append(assigned[position])
        moves = self._reorder(remote_id, current, desired)
        if moves:
            notes.append(f"moved {moves} track{'s' if moves != 1 else ''} into place")
        return remote_id, notes

    def _read_settled(self, remote_id: str, expected: int, tries: int = 3) -> list[dict]:
        """Read a playlist, waiting briefly if Qobuz still serves the version from before the change."""
        _, items = self.client.playlist(remote_id)
        for _ in range(tries - 1):
            if len(items) == expected:
                break
            self.client.sleep(1.5)
            _, items = self.client.playlist(remote_id)
        return items

    def _write_details(self, remote_id: str, remote: PlaylistState, target: PlaylistState) -> list[str]:
        if (remote.name, remote.description or "") == (target.name, target.description or ""):
            return []
        self.client.update_playlist(remote_id, name=target.name, description=target.description or "")
        if plain_name(target.name) != target.name:
            # Qobuz can lose names with emoji; check, and fall back to the plain name.
            details, _ = self.client.playlist(remote_id)
            if not (details.get("name") or "").strip():
                self.client.update_playlist(remote_id, name=plain_name(target.name))
                return [f"Qobuz doesn't keep emoji in names; saved as {plain_name(target.name)!r}"]
        return []

    def _reorder(self, remote_id: str, current: list[str], desired: list[str]) -> int:
        """Move entries so ``current`` becomes ``desired``, one run of entries per call.

        Works front to back and only ever moves an entry towards the front, so
        ``insert_before`` means the same whether Qobuz counts before or after removing it.
        """
        present = set(current)
        desired = [h for h in desired if h in present]
        calls = 0
        for i, handle in enumerate(desired):
            if current[i] == handle:
                continue
            j = current.index(handle)
            run = 1
            while i + run < len(desired) and j + run < len(current) and current[j + run] == desired[i + run]:
                run += 1
            moving = current[j:j + run]
            self.client.move_tracks(remote_id, moving, insert_before=i)
            del current[j:j + run]
            current[i:i] = moving
            calls += 1
        return calls

    def _write_liked(self, remote: PlaylistState | None, target: PlaylistState, track_id) -> list[str]:
        have = {e.ref_id for e in (remote.entries if remote else ()) if e.ref_service == SERVICE}
        want: list[str] = []
        unmatched = 0
        for entry in target.entries:
            qobuz_id = track_id(entry.track_id, entry.ref_service, entry.ref_id)
            if qobuz_id is None:
                unmatched += 1
            else:
                want.append(qobuz_id)
        add = [q for q in dict.fromkeys(want) if q not in have]
        remove = [q for q in have if q not in set(want)]
        if add:
            self.client.favorite(add)
        if remove:
            self.client.unfavorite(remove)
        notes = []
        if unmatched:
            notes.append(f"{unmatched} liked track{'s' if unmatched != 1 else ''} have no match on Qobuz yet")
        return notes
