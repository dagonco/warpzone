"""A stand-in webhook receiver for the tests of `prune` and `inbox`."""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any


class Receiver:
    """A one-shot webhook receiver."""

    def __init__(self, status: int = 200) -> None:
        self.bodies: list[Any] = []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args: object) -> None:
                pass

            def do_POST(self) -> None:
                length = int(self.headers.get("Content-Length") or 0)
                outer.bodies.append(json.loads(self.rfile.read(length)))
                self.send_response(status)
                self.send_header("Content-Length", "0")
                self.end_headers()

        self.server = HTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}/api/webhook/secret-id"
        self.thread = threading.Thread(
            target=self.server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True
        )

    def __enter__(self) -> Receiver:
        self.thread.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self.server.shutdown()
        self.server.server_close()
