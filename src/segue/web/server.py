"""A small local web server for the interface.

It listens on 127.0.0.1 only and serves the page plus a JSON API. Requests that change
anything must carry a token that's only in the page Segue served, and the Host header must
be Segue's own address, so other websites open in the browser can't call it.
"""

from __future__ import annotations

import json
import secrets
import threading
import urllib.parse
import webbrowser
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from .. import __version__, actions, analysis, backup, changes, dupes, remote, select
from ..changes import ChangeError
from ..connectors import qobuz, qobuz_probe
from ..connectors.common import SecretFile
from ..formats import read_playlists
from ..remote import RemoteError
from ..store import Store

STATIC = Path(__file__).parent / "static"
DEFAULT_PORT = 8790
SERVICES = ("qobuz", "spotify")
TYPES = {".html": "text/html; charset=utf-8", ".js": "text/javascript; charset=utf-8",
         ".css": "text/css; charset=utf-8", ".svg": "image/svg+xml"}


class App:
    """The API, independent of HTTP so tests can call it directly."""

    def __init__(self, db_path: Path, services=None):
        self.db_path = Path(db_path)
        self.lock = threading.Lock()
        self._services = services  # for tests: a function returning {name: service}

    # Plumbing

    def store(self) -> Store:
        return Store(self.db_path)

    def services(self) -> dict:
        if self._services is not None:
            return self._services()
        session = SecretFile(self.db_path.parent / "qobuz.json")
        return {"qobuz": qobuz.QobuzService(qobuz.QobuzClient(session))} if session.load() else {}

    @property
    def backup_folder(self) -> Path:
        return backup.default_folder(self.db_path)

    def handle(self, method: str, path: str, query: dict, body: dict) -> tuple[int, object]:
        parts = [urllib.parse.unquote(p) for p in path.strip("/").split("/")[1:]]  # drop "api"
        name = "_".join([method.lower()] + [p for p in parts if not p.isdigit()])
        args = [int(p) for p in parts if p.isdigit()]
        if args:
            name += "_"  # GET /api/playlists/5 -> get_playlists_(store, 5)
        handler = getattr(self, name, None)
        if handler is None:
            return 404, {"error": f"no such API: {method} {path}"}
        with self.lock:
            try:
                with self.store() as store:
                    return 200, handler(store, *args, query=query, body=body)
            except (ValueError, KeyError, ChangeError, RemoteError, qobuz.QobuzError) as e:
                return 400, {"error": e.args[0] if isinstance(e, KeyError) else str(e)}

    # Overview

    def get_state(self, store: Store, query, body):
        services = self._service_status(store)
        staged = changes.batches(store, ["staged"])
        undo_id, redo_id = changes.next_undo(store), changes.next_redo(store)
        undo = changes.get_batch(store, undo_id) if undo_id else None
        redo = changes.get_batch(store, redo_id) if redo_id else None
        return {
            "version": __version__,
            "mode": changes.apply_mode(store),
            "playlists": len(store.playlist_summaries()),
            "staged": len(staged),
            "undo": {"id": undo.id, "description": undo.description} if undo and changes.can_undo(store, undo) else None,
            "redo": {"id": redo.id, "description": redo.description} if redo else None,
            "services": services,
            "duplicates_configured": dupes.is_configured(store),
            "last_backup": backup.last_backup_time(store),
        }

    def _service_status(self, store: Store) -> dict:
        return {
            "qobuz": {"connected": "qobuz" in self.services(), "last_pull": remote.last_pull(store, "qobuz")},
            "spotify": {"connected": False, "available": False},
        }

    # Library

    def get_playlists(self, store: Store, query, body):
        summaries = store.playlist_summaries()
        all_ids = [s.id for s in summaries]
        same_name = {pid for group in select.name_groups(store, all_ids) for pid in group}
        inside = {small for small, _ in select.subsets(store, [s.id for s in summaries if s.kind != "liked"])}
        key = dupes.key_function(store, dupes.level(store))
        keep = dupes.keep(store)
        rows = []
        for s in summaries:
            state = store.playlist_state(s.id)
            rows.append({
                "id": s.id, "name": s.name, "kind": s.kind, "tracks": s.track_count,
                "service": s.source.service if s.source else "local",
                "same_name": s.id in same_name, "inside": s.id in inside,
                "dupes": len(dupes.duplicate_positions(state.entries, key, keep)) if s.kind != "followed" else 0,
            })
        return rows

    def get_playlists_(self, store: Store, pid: int, query, body):  # GET /api/playlists/<id>
        playlist = store.get_playlist(pid)
        state = store.playlist_state(pid)
        service = playlist.source.service if playlist.source else None
        tracks = []
        for position, (entry, stored) in enumerate(zip(playlist.entries, state.entries), start=1):
            t = entry.track
            tracks.append({
                "position": position, "title": t.title, "artists": t.artists, "album": t.album,
                "duration_ms": t.duration_ms, "added_at": entry.added_at, "release_date": t.release_date,
                "services": list(dict.fromkeys(r.service for r in t.refs)),
                "on_service": service is None or service == "file" or t.ref(service) is not None,
            })
        return {"id": pid, "name": playlist.name, "kind": playlist.kind, "description": playlist.description,
                "service": service, "url": playlist.source.url if playlist.source else None, "tracks": tracks}

    def get_check(self, store: Store, query, body):
        return [{"title": f.title, "lines": f.lines, "action": f.action,
                 "line_actions": f.line_actions or [None] * len(f.lines)} for f in analysis.check(store)]

    # Actions

    def post_preview(self, store: Store, query, body):
        params = self._params(body)
        result = actions.preview(store, body["action"], params)
        result["mode"] = changes.apply_mode(store)
        return result

    def post_apply(self, store: Store, query, body):
        return actions.apply(store, body["action"], self._params(body), self.services(), self.backup_folder,
                             body.get("backup"))

    def _params(self, body: dict) -> dict:
        params = dict(body.get("params") or {})
        if body.get("action") == "restore":  # restore only from files in Segue's backups folder
            path = (self.backup_folder / Path(str(params.get("file", ""))).name).resolve()
            if path.parent != self.backup_folder.resolve() or not path.exists():
                raise ValueError("choose one of the backups listed")
            params["path"] = str(path)
        return params

    # The change log

    def get_pending(self, store: Store, query, body):
        staged = changes.batches(store, ["staged"])
        return {
            "batches": [{"id": b.id, "description": b.description, "lines": changes.describe_batch(store, b.id)}
                        for b in staged],
            "net": changes.describe_staged(store),
            "writes": remote.describe_pending(store, self.services()),
        }

    def get_history(self, store: Store, query, body):
        rows = changes.batches(store, ["staged", "committed", "undone"], limit=int(query.get("n", 50)))
        return [{"id": b.id, "description": b.description, "status": b.status, "mode": b.mode,
                 "when": (b.undone_at if b.status == "undone" else b.applied_at), "can_undo": changes.can_undo(store, b)}
                for b in rows]

    def post_commit(self, store: Store, query, body):
        return actions.commit(store, self.services(), self.backup_folder, body.get("backup"), bool(body.get("local")))

    def post_discard(self, store: Store, query, body):
        return {"discarded": changes.discard(store)}

    def post_undo(self, store: Store, query, body):
        return actions.undo(store, self.services(), self.backup_folder, body.get("backup"))

    def post_redo(self, store: Store, query, body):
        return actions.undo(store, self.services(), self.backup_folder, body.get("backup"), redo=True)

    # Backups

    def get_backups(self, store: Store, query, body):
        folder = self.backup_folder
        files = sorted(folder.glob("segue-backup-*.json"), reverse=True) if folder.exists() else []
        return [{"file": f.name, "when": datetime.fromtimestamp(f.stat().st_mtime).isoformat(timespec="minutes"),
                 "csv": f.with_suffix(".csv").name if f.with_suffix(".csv").exists() else None} for f in files]

    def get_backups_playlists(self, store: Store, query, body):
        path = (self.backup_folder / Path(query.get("file", "")).name)
        if not path.exists():
            raise ValueError("no such backup")
        return [{"name": p.name, "tracks": len(p.entries)} for p in read_playlists(path)]

    def post_backup(self, store: Store, query, body):
        return {"files": [str(p) for p in backup.write_backup(store, self.backup_folder)]}

    # Settings

    def get_settings(self, store: Store, query, body):
        return {
            "apply_mode": changes.apply_mode(store), "duplicates": dupes.level(store), "keep": dupes.keep(store),
            "undo_limit": changes.undo_limit(store), "destinations": remote.destinations(store),
            "choices": {"apply_mode": {"safe": "Stage changes; undo freely; nothing is final until you commit",
                                       "auto": "Apply each change right away; the last few can still be undone"},
                        "duplicates": dupes.LEVEL_HELP, "keep": dupes.KEEP_HELP, "destinations": list(SERVICES)},
        }

    def post_settings(self, store: Store, query, body):
        setters = {"apply_mode": changes.set_apply_mode, "duplicates": dupes.set_level, "keep": dupes.set_keep,
                   "undo_limit": changes.set_undo_limit,
                   "destinations": lambda st, v: remote.set_destinations(st, ",".join(v), SERVICES)}
        for key, value in body.items():
            if key not in setters:
                raise ValueError(f"unknown setting {key!r}")
            setters[key](store, value)
        return self.get_settings(store, query, body)

    # Services

    def post_pull(self, store: Store, query, body):
        service = self.services().get(body.get("service", "qobuz"))
        if service is None:
            raise ValueError("connect Qobuz first")
        r = remote.pull(store, service)
        return {"added": r.added, "updated": r.updated, "removed": r.removed, "unchanged": r.unchanged}

    def post_qobuz_connect(self, store: Store, query, body):
        session = qobuz.parse_session(body.get("raw", ""))
        client = qobuz.QobuzClient(session)
        me = client.me()
        client.playlists()
        if not session.get("user_id") and me.get("id"):
            session["user_id"] = str(me["id"])
        SecretFile(self.db_path.parent / "qobuz.json").save(session)
        return {"connected": True, "name": me.get("display_name") or me.get("login") or session.get("user_id")}

    def post_qobuz_disconnect(self, store: Store, query, body):
        return {"disconnected": SecretFile(self.db_path.parent / "qobuz.json").clear()}

    def post_qobuz_probe(self, store: Store, query, body):
        service = self.services().get("qobuz")
        if service is None:
            raise ValueError("connect Qobuz first")
        checks = qobuz_probe.probe(service.client, bool(body.get("write")))
        return {"checks": [c.__dict__ for c in checks], "summary": qobuz_probe.summary(checks)}


def make_handler(app: App, token: str, port: int):
    allowed_hosts = {f"127.0.0.1:{port}", f"localhost:{port}"}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def _send(self, status: int, body: bytes, content_type: str) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Content-Security-Policy", "default-src 'self'; style-src 'self' 'unsafe-inline'")
            self.end_headers()
            self.wfile.write(body)

        def _json(self, status: int, data) -> None:
            self._send(status, json.dumps(data).encode("utf-8"), "application/json")

        def _host_ok(self) -> bool:
            return self.headers.get("Host") in allowed_hosts  # blocks DNS-rebinding tricks

        def do_GET(self):
            if not self._host_ok():
                return self._json(403, {"error": "wrong host"})
            url = urllib.parse.urlparse(self.path)
            if url.path.startswith("/api/"):
                if self.headers.get("X-Segue-Token") != token:
                    return self._json(403, {"error": "missing token; reload the page"})
                query = dict(urllib.parse.parse_qsl(url.query))
                return self._json(*app.handle("GET", url.path, query, {}))
            name = "index.html" if url.path in ("/", "/index.html") else url.path.lstrip("/")
            file = (STATIC / name).resolve()
            if file.parent != STATIC.resolve() or not file.is_file():
                return self._send(404, b"not found", "text/plain")
            data = file.read_bytes()
            if name == "index.html":
                data = data.replace(b"__SEGUE_TOKEN__", token.encode("ascii"))
            self._send(200, data, TYPES.get(file.suffix, "application/octet-stream"))

        def do_POST(self):
            if not self._host_ok() or self.headers.get("X-Segue-Token") != token:
                return self._json(403, {"error": "missing token; reload the page"})
            url = urllib.parse.urlparse(self.path)
            length = int(self.headers.get("Content-Length") or 0)
            try:
                body = json.loads(self.rfile.read(length) or b"{}")
            except json.JSONDecodeError:
                return self._json(400, {"error": "bad JSON"})
            self._json(*app.handle("POST", url.path, {}, body if isinstance(body, dict) else {}))

    return Handler


def serve(db_path: Path, port: int = DEFAULT_PORT, open_browser: bool = True) -> None:
    token = secrets.token_urlsafe(24)
    app = App(db_path)
    try:
        server = ThreadingHTTPServer(("127.0.0.1", port), make_handler(app, token, port))
    except OSError:
        raise ValueError(f"port {port} is in use; is Segue already open? Or start it with --port") from None
    url = f"http://127.0.0.1:{port}/"
    print(f"Segue is running at {url}")
    print("Keep this window open while you use it. Close it (or press Ctrl+C) to stop Segue.")
    if open_browser:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nSegue stopped.")
    finally:
        server.server_close()
