"""Shared test helpers: fake config and a fake Syncthing REST server."""

from __future__ import annotations

import json
import socketserver
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlsplit

from warpzone.config import Config, parse_config

HUB_ID = "AAAAAAA-AAAAAAA-AAAAAAA-AAAAAAA-AAAAAAA-AAAAAAA-AAAAAAA-AAAAAAA"
DECK_ID = "BBBBBBB-BBBBBBB-BBBBBBB-BBBBBBB-BBBBBBB-BBBBBBB-BBBBBBB-BBBBBBB"
MACHINE_ID = "CCCCCCC-CCCCCCC-CCCCCCC-CCCCCCC-CCCCCCC-CCCCCCC-CCCCCCC-CCCCCCC"
API_KEY = "fake-key"  # gitleaks:allow (test fixture, not a real key)


def config_data(api_url: str = "http://127.0.0.1:1") -> dict[str, Any]:
    return {
        "hub": {
            "name": "nas",
            "device_id": HUB_ID,
            "root": "/data/games",
            "api_url": api_url,
            "api_key": API_KEY,
        },
        "devices": {
            "deck": {
                "device_id": DECK_ID,
                "profile": "steamos-emudeck",
                "folders": ["library", "tools", "saves", "frontend"],
            },
            "machine": {
                "device_id": MACHINE_ID,
                "profile": "steamos-emudeck",
                "folders": ["library", "songs", "saves"],
            },
        },
    }


def make_config(api_url: str = "http://127.0.0.1:1") -> Config:
    return parse_config(config_data(api_url))


class _Server(ThreadingHTTPServer):
    daemon_threads = True

    def server_bind(self) -> None:
        # HTTPServer.server_bind calls getfqdn(), which can stall for seconds on
        # machines with odd DNS setups. The name is irrelevant here.
        socketserver.TCPServer.server_bind(self)
        self.server_name, self.server_port = "127.0.0.1", self.server_address[1]


class FakeSyncthing:
    """Just enough of Syncthing's REST API, backed by dicts. Records every request."""

    def __init__(self, my_id: str, api_key: str = API_KEY) -> None:
        self.my_id = my_id
        self.api_key = api_key
        self.devices: list[dict[str, Any]] = [{"deviceID": my_id, "name": "self"}]
        self.folders: dict[str, dict[str, Any]] = {}
        self.ignores: dict[str, list[str]] = {}
        # What the folder holds at its top level (db/browse).
        self.browse: dict[str, list[str]] = {}
        # (folder, name) -> number of db/file polls before it reports in sync.
        self.arrives_after: dict[tuple[str, str], int] = {}
        self.polls: dict[tuple[str, str], int] = {}
        # (folder, name) -> a full db/file answer, taking precedence over arrives_after.
        self.file_info: dict[tuple[str, str], dict[str, Any]] = {}
        # (folder, sub) -> the db/file answer for `sub` once a scan of that sub ran.
        # Syncthing on Linux only flags a newly ignored directory when it is scanned itself.
        self.after_sub_scan: dict[tuple[str, str], dict[str, Any]] = {}
        self.ignores_error: str | None = None  # what db/ignores reports for a bad include
        self.scans = 0  # shows up as db/status stateChanged, like a finished scan
        self.requests: list[tuple[str, str, Any]] = []
        self._server = _Server(("127.0.0.1", 0), self._handler())
        self._thread = threading.Thread(
            target=self._server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True
        )

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self._server.server_address[1]}"

    def __enter__(self) -> FakeSyncthing:
        self._thread.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=5)

    def writes(self) -> list[tuple[str, str, Any]]:
        return [r for r in self.requests if r[0] != "GET"]

    def _handler(self) -> type[BaseHTTPRequestHandler]:
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args: object) -> None:
                pass

            def _send(self, code: int, body: Any = None) -> None:
                raw = b"" if body is None else json.dumps(body).encode()
                if isinstance(body, str):
                    raw = body.encode()
                self.send_response(code)
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def _handle(self) -> None:
                length = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(length)) if length else None
                url = urlsplit(self.path)
                query = {k: v[0] for k, v in parse_qs(url.query).items()}
                fake.requests.append((self.command, self.path, body))
                if self.headers.get("X-API-Key") != fake.api_key:
                    self._send(403, "CSRF Error")
                    return
                code, payload = fake.route(self.command, url.path, query, body)
                self._send(code, payload)

            do_GET = do_POST = do_PATCH = _handle

        return Handler

    def route(self, method: str, path: str, query: dict[str, str], body: Any) -> tuple[int, Any]:
        folder = query.get("folder", "")
        if (method, path) == ("GET", "/rest/system/status"):
            return 200, {"myID": self.my_id}
        if path == "/rest/config/devices":
            if method == "POST":
                self.devices.append(body)
                return 200, None
            return 200, self.devices
        if path == "/rest/config/folders":
            if method == "POST":
                self.folders[body["id"]] = {"paused": False, **body}
                self.ignores.setdefault(body["id"], [])
                return 200, None
            return 200, list(self.folders.values())
        if path.startswith("/rest/config/folders/") and method == "PATCH":
            fid = path.rsplit("/", 1)[1]
            if fid not in self.folders:
                return 404, "No folder with given ID"
            self.folders[fid].update(body)
            return 200, None
        if path == "/rest/db/ignores":
            if folder not in self.folders:
                return 500, "no such folder"
            if method == "POST":
                self.ignores[folder] = list(body["ignore"])
            return 200, {
                "ignore": self.ignores[folder],
                "expanded": [],
                "error": self.ignores_error,
            }
        if path == "/rest/db/status":
            return 200, {
                "state": "idle",
                "stateChanged": str(self.scans),
                "globalFiles": 3,
                "needFiles": 0,
            }
        if path == "/rest/db/browse":
            if folder not in self.browse and folder not in self.folders:
                return 500, "no such folder"
            names = self.browse.get(folder, [])
            return 200, [{"name": n, "type": "FILE_INFO_TYPE_FILE"} for n in names]
        if path == "/rest/db/file":
            key = (folder, query["file"])
            if key in self.file_info:
                return 200, self.file_info[key]
            if key not in self.arrives_after:
                return 404, "No such object in the index"
            self.polls[key] = self.polls.get(key, 0) + 1
            if self.polls[key] <= self.arrives_after[key]:
                return 200, {"global": {"version": ["X:1"]}, "local": None}
            info = {"version": ["X:1"], "deleted": False, "invalid": False}
            return 200, {"global": info, "local": info}
        if (method, path) == ("POST", "/rest/db/scan"):
            self.scans += 1
            key = (folder, query.get("sub", ""))
            if key in self.after_sub_scan:
                self.file_info[key] = self.after_sub_scan[key]
            return 200, None
        return 404, "not found"
