"""`warpzone serve` over a real ThreadingHTTPServer on 127.0.0.1, a temporary hub tree and the
fake Syncthing. Every request goes through a socket."""

import contextlib
import http.client
import json
import os
import select
import shutil
import signal
import socket
import sys
import tempfile
import threading
import time
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, NamedTuple
from unittest import mock

from library_support import GIB, config, games, managed, write_catalog
from support import API_KEY, DECK_ID, HUB_ID, MACHINE_ID, FakeSyncthing
from test_cli import run
from warpzone import (
    __version__,
    artwork,
    catalog,
    inbox_unattended,
    library_api,
    playstate,
    playstate_device,
    serve,
)
from warpzone import whitelist as wl
from warpzone.config import ServeSettings, parse_config
from warpzone.syncthing import Client

TOKEN = "test-ui-token-not-real"  # gitleaks:allow (test fixture, not a real token)
BEARER = {"Authorization": f"Bearer {TOKEN}"}
PNG = b"\x89PNG\r\n\x1a\nfake"
GAMELIST = """<?xml version="1.0"?>
<gameList>
  <game><path>./Example Game (Europe).chd</path><name>Example Game</name><desc>A fine game.</desc>
  <rating>0.8</rating><developer>Dev</developer><junk>x</junk></game>
</gameList>
"""


class Reply(NamedTuple):
    status: int
    headers: http.client.HTTPMessage
    body: bytes

    @property
    def json(self) -> Any:
        return json.loads(self.body)


def fresh_cookie() -> str:
    """A session cookie value that is valid for an hour."""
    return serve.session_cookie(TOKEN, int(time.time()) + 3600)


def by_title(system: str, title: str) -> catalog.Game:
    return next(g for s, g in games() if s == system and g.title == title)


EXAMPLE = by_title("psx", "Example Game (Europe)")
OTHER = by_title("psx", "Other Game")
PS4 = by_title("ps4", "Example PS4")
TINY = by_title("gba", "Tiny")


class ServeCase(unittest.TestCase):
    """A hub tree with a catalog, two managed whitelists, a gamelist and covers, behind a
    server whose Syncthing is a fake."""

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        self.root = self.tmp / "hub"
        self.library = self.root / "library"
        (self.library / ".stfolder").mkdir(parents=True)
        write_catalog(self.root)
        self.write_whitelist("deck", managed(["gba"], includes=[EXAMPLE.primary]))
        self.write_whitelist("machine", managed())
        gamelist = self.root / "frontend" / "es-de" / "gamelists" / "psx" / "gamelist.xml"
        gamelist.parent.mkdir(parents=True)
        gamelist.write_text(GAMELIST)
        self.secret = self.tmp / "secret.txt"
        self.secret.write_text("do not serve")
        media = self.root / "frontend" / "media" / "psx"
        (media / "covers").mkdir(parents=True)
        (media / "covers" / "Example Game (Europe).png").write_bytes(PNG)
        (media / "marquees").mkdir()
        (media / "marquees" / "Other Game.png").symlink_to(self.secret)
        self.ui = self.tmp / "ui"
        self.ui.mkdir()
        (self.ui / "index.html").write_text("<!doctype html><title>ui</title>")
        (self.ui / "app.js").write_text("// js")
        (self.ui / "leak.txt").symlink_to(self.secret)

        self.fake = FakeSyncthing(HUB_ID)
        self.fake.__enter__()
        self.addCleanup(self.fake.__exit__, None, None, None)
        self.fake.connections = {DECK_ID: {"connected": True}}
        self.fake.completion[("wz-library", DECK_ID)] = {
            "completion": 42.7,
            "needBytes": 31_000_000_000,
            "needItems": 812,
            "remoteState": "valid",
        }
        self.cfg = config(root=str(self.root), api_url=self.fake.url)
        self.logs: list[str] = []
        self.start()

    def start(self, **options: Any) -> None:
        files = serve.HubFiles(self.root, interval=0, media_seconds=0)
        defaults: dict[str, Any] = {
            "root": self.root,
            "files": files,
            "client_factory": lambda: Client(self.fake.url, API_KEY, label="hub"),
            "ui_dir": self.ui,
            "login_delay": 0,
            "log": self.logs.append,
        }
        self.app = serve.App(self.cfg, TOKEN, **{**defaults, **options})
        server = self.server = serve.UiServer(("127.0.0.1", 0), self.app)
        self.port = server.server_address[1]
        thread = threading.Thread(
            target=server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True
        )
        thread.start()

        def stop() -> None:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)

        self.addCleanup(stop)

    def write_whitelist(self, device: str, text: str) -> Path:
        path = self.library / f"patterns-{device}.stignore"
        path.write_text(text)
        return path

    def call(
        self,
        method: str,
        path: str,
        body: Any = None,
        headers: dict[str, str] | None = None,
        raw: bytes | None = None,
        auth: bool = True,
    ) -> Reply:
        sent = dict(BEARER if auth else {})
        sent.update(headers or {})
        data = raw
        if body is not None:
            data = json.dumps(body).encode()
            sent.setdefault("Content-Type", "application/json")
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        try:
            conn.request(method, path, body=data, headers=sent)
            response = conn.getresponse()
            return Reply(response.status, response.msg, response.read())
        finally:
            conn.close()

    def get(self, path: str, **kw: Any) -> Reply:
        return self.call("GET", path, **kw)

    def etags(self) -> dict[str, str]:
        return {d["name"]: d["whitelist"]["etag"] for d in self.get("/api/v1/devices").json}

    def change(self, changes: list[dict[str, Any]], **extra: Any) -> Reply:
        names = {c["device"] for c in changes}
        etags = self.etags()
        expect = {n: etags[n] for n in (etags if "*" in names else names) if n in etags}
        return self.call("POST", "/api/v1/changes", {"changes": changes, "expect": expect, **extra})

    def error(self, reply: Reply) -> dict[str, Any]:
        return reply.json["error"]

    def cookie_get(self, cookie: str) -> int:
        return self.get("/api/v1/devices", headers={"Cookie": cookie}, auth=False).status


class AuthTests(ServeCase):
    def test_no_token_and_a_wrong_token_are_401(self) -> None:
        for headers in (
            {},
            {"Authorization": "Bearer wrong"},
            {"Authorization": "Basic " + TOKEN},
            {"Authorization": "Bearer"},
            {"Cookie": "wz_session=nope"},
            {"Cookie": "other=" + fresh_cookie()},
        ):
            reply = self.get("/api/v1/devices", headers=headers, auth=False)
            self.assertEqual(reply.status, 401, headers)
            self.assertEqual(self.error(reply)["code"], "unauthorized")

    def test_the_bearer_token_works_for_reads_and_writes_without_an_origin(self) -> None:
        self.assertEqual(self.get("/api/v1/devices").status, 200)
        reply = self.change([{"device": "deck", "system": "gba", "on": False}], dry_run=True)
        self.assertEqual(reply.status, 200)

    def test_an_unknown_api_route_is_401_without_auth_and_404_with(self) -> None:
        self.assertEqual(self.get("/api/v1/nope", auth=False).status, 401)
        reply = self.get("/api/v1/nope")
        self.assertEqual((reply.status, self.error(reply)["code"]), (404, "not-found"))
        self.assertEqual(self.get("/api/v2/devices").status, 404)
        self.assertEqual(self.call("POST", "/api/v1/devices", {}).status, 404)

    def test_login_sets_the_cookie_and_the_cookie_opens_the_api(self) -> None:
        reply = self.call("POST", "/api/v1/session", {"token": TOKEN}, auth=False)
        self.assertEqual((reply.status, reply.body), (204, b""))
        cookie = reply.headers["Set-Cookie"]
        value = cookie.split(";")[0].removeprefix("wz_session=")
        for attribute in (
            "HttpOnly",
            "SameSite=Strict",
            "Path=/",
            f"Max-Age={serve.COOKIE_MAX_AGE}",
        ):
            self.assertIn(attribute, cookie)
        # the server's own expiry is the cookie's Max-Age from now
        expires = int(value.split(".")[0])
        self.assertAlmostEqual(expires, time.time() + serve.COOKIE_MAX_AGE, delta=60)
        self.assertNotIn(TOKEN, cookie)
        reply = self.get("/api/v1/devices", headers={"Cookie": f"wz_session={value}"}, auth=False)
        self.assertEqual(reply.status, 200)

    def test_the_cookie_is_secure_only_behind_a_tls_proxy(self) -> None:
        def login(headers: dict[str, str]) -> str:
            reply = self.call("POST", "/api/v1/session", {"token": TOKEN}, headers, auth=False)
            self.assertEqual(reply.status, 204)
            return reply.headers["Set-Cookie"]

        plain = login({})
        for forwarded, secure in (
            ("https", True),
            ("HTTPS", True),
            (" https ", True),
            ("https, http", True),
            ("http", False),
            ("http, https", False),
            ("", False),
        ):
            cookie = login({"X-Forwarded-Proto": forwarded})
            self.assertEqual("; Secure" in cookie, secure, forwarded)
            # nothing else changes, whichever the header says
            self.assertEqual(cookie.replace("; Secure", "").split("; ")[1:], plain.split("; ")[1:])
        self.assertNotIn("Secure", plain)
        self.assertIn("HttpOnly", login({"X-Forwarded-Proto": "https"}))

    def test_a_wrong_token_at_login_is_401_after_a_delay(self) -> None:
        self.start(login_delay=1.0)
        with mock.patch.object(serve.time, "sleep") as sleep:
            reply = self.call("POST", "/api/v1/session", {"token": "wrong"}, auth=False)
        self.assertEqual((reply.status, self.error(reply)["code"]), (401, "unauthorized"))
        sleep.assert_called_once_with(1.0)
        self.assertNotIn("Set-Cookie", reply.headers)

    def test_login_body_is_checked(self) -> None:
        for body in ({}, {"token": 5}, {"token": TOKEN, "x": 1}, [TOKEN]):
            self.assertEqual(self.call("POST", "/api/v1/session", body, auth=False).status, 400)
        reply = self.call("POST", "/api/v1/session", raw=b"{}", auth=False)  # no content type
        self.assertEqual(reply.status, 400)
        self.assertEqual(self.get("/api/v1/session", auth=False).status, 404)

    def test_the_cookie_survives_a_restart_and_stops_working_when_the_token_changes(self) -> None:
        value = fresh_cookie()
        self.start()  # a new server with the same token
        self.assertEqual(self.cookie_get(f"wz_session={value}"), 200)
        other = serve.session_cookie(TOKEN + "x", int(time.time()) + 3600)
        self.assertEqual(self.cookie_get(f"wz_session={other}"), 401)

    def test_the_session_expires_on_the_server_even_if_the_browser_keeps_sending_it(self) -> None:
        now = [datetime(2026, 11, 10, 12, 0, tzinfo=UTC)]
        self.start(now=lambda: now[0])

        def login() -> str:
            reply = self.call("POST", "/api/v1/session", {"token": TOKEN}, auth=False)
            return reply.headers["Set-Cookie"].split(";")[0]

        cookie = login()
        self.assertEqual(self.cookie_get(cookie), 200)
        now[0] += timedelta(seconds=serve.COOKIE_MAX_AGE - 1)
        self.assertEqual(self.cookie_get(cookie), 200)
        now[0] += timedelta(seconds=1)
        self.assertEqual(self.cookie_get(cookie), 401)
        self.assertEqual(self.cookie_get(login()), 200)  # a new login starts a new session

    def test_a_cookie_cannot_be_given_a_later_expiry_or_be_the_old_stateless_one(self) -> None:
        now = int(time.time())
        expired = serve.session_cookie(TOKEN, now - 10)
        _, _, mac = expired.partition(".")
        for value in (expired, f"{now + 10**9}.{mac}", mac, f".{mac}", "x.y", f"-5.{mac}"):
            self.assertEqual(self.cookie_get(f"wz_session={value}"), 401, value)
        self.assertEqual(self.cookie_get(f"wz_session={fresh_cookie()}"), 200)

    def test_a_write_by_cookie_needs_the_origin_of_the_page(self) -> None:
        cookie = {"Cookie": f"wz_session={fresh_cookie()}"}
        body = {"changes": [{"device": "deck", "system": "gba", "on": False}], "dry_run": True}
        body["expect"] = {"deck": self.etags()["deck"]}
        host = f"127.0.0.1:{self.port}"
        for origin in (None, "http://evil.example", f"https://{host}.evil", f"http://{host}.evil"):
            headers = dict(cookie)
            if origin is not None:
                headers["Origin"] = origin
            reply = self.call("POST", "/api/v1/changes", body, headers=headers, auth=False)
            self.assertEqual(reply.status, 403, origin)
            self.assertEqual(self.error(reply)["code"], "bad-origin")
        ok = self.call(
            "POST",
            "/api/v1/changes",
            body,
            headers={**cookie, "Origin": f"http://{host}"},
            auth=False,
        )
        self.assertEqual(ok.status, 200)
        # a read needs no Origin, and a bearer write ignores a foreign one
        self.assertEqual(self.get("/api/v1/devices", headers=cookie, auth=False).status, 200)
        foreign = self.call(
            "POST", "/api/v1/changes", body, headers={"Origin": "http://evil.example"}
        )
        self.assertEqual(foreign.status, 200)

    def test_a_write_must_be_json(self) -> None:
        body = json.dumps({"changes": []}).encode()
        for kind in ("text/plain", "application/x-www-form-urlencoded", ""):
            reply = self.call("POST", "/api/v1/changes", raw=body, headers={"Content-Type": kind})
            self.assertEqual(reply.status, 400, kind)
        reply = self.call(
            "POST",
            "/api/v1/changes",
            raw=body,
            headers={"Content-Type": "application/json; charset=utf-8"},
        )
        self.assertEqual(reply.status, 400)  # JSON, but no changes
        self.assertIn("non-empty", self.error(reply)["message"])

    def test_healthz_needs_no_auth(self) -> None:
        reply = self.get("/healthz", auth=False)
        self.assertEqual((reply.status, reply.json), (200, {"ok": True, "version": __version__}))
        self.assertEqual(self.call("POST", "/healthz", {}, auth=False).status, 404)

    def test_the_token_is_never_logged_or_echoed(self) -> None:
        self.call("POST", "/api/v1/session", {"token": TOKEN}, auth=False)
        self.call("POST", "/api/v1/session", {"token": "wrong"}, auth=False)
        self.get("/api/v1/nope")
        self.get("/api/v1/progress")
        self.assertNotIn(TOKEN, "\n".join(self.logs))


class LoginFloodTests(ServeCase):
    """Failed sign-ins wait in a bounded queue; a full queue refuses the next attempt at once,
    and the connections it holds cannot starve the rest of the UI."""

    def setUp(self) -> None:
        super().setUp()
        self.release = threading.Event()
        self.addCleanup(self.release.set)
        self.sleeping = threading.Semaphore(0)
        self.sleeps: list[float] = []
        self.start(login_delay=1.0)
        self.app.limiter = serve.FailureLimiter(1.0, sleep=self.sleep)
        self.answers: list[Reply | BaseException] = []
        self.threads: list[threading.Thread] = []
        self.addCleanup(self.join)

    def join(self) -> None:
        self.release.set()
        for thread in self.threads:
            thread.join(timeout=5)

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.sleeping.release()
        self.release.wait(5)

    def login_request(self, token: str) -> serve.Request:
        return serve.Request(
            "POST",
            "/api/v1/session",
            {"content-type": "application/json"},
            json.dumps({"token": token}).encode(),
        )

    def fill_the_queue(self, count: int) -> None:
        """`count` wrong tokens, each asleep in the limiter."""
        for _ in range(count):
            thread = threading.Thread(
                target=self.app.handle, args=(self.login_request("wrong"),), daemon=True
            )
            thread.start()
            self.threads.append(thread)
        for _ in range(count):
            self.assertTrue(self.sleeping.acquire(timeout=5))

    def code(self, reply: serve.Response) -> str:
        return json.loads(reply.body)["error"]["code"]

    def test_a_full_queue_refuses_every_attempt_before_comparing_the_token(self) -> None:
        self.fill_the_queue(serve.MAX_WAITING)
        slept = len(self.sleeps)
        with mock.patch.object(serve, "_same") as same:
            for token in ("wrong", TOKEN):  # the right one is refused too
                reply = self.app.handle(self.login_request(token))
                self.assertEqual(
                    (reply.status, self.code(reply)), (429, "too-many-attempts"), token
                )
                self.assertIn(("Retry-After", "1"), reply.headers)
                self.assertNotIn("Set-Cookie", [name for name, _ in reply.headers])
            bearer = serve.Request("GET", "/api/v1/devices", {"authorization": f"Bearer {TOKEN}"})
            reply = self.app.handle(bearer)
            self.assertEqual((reply.status, self.code(reply)), (429, "too-many-attempts"))
            self.assertIn(("Retry-After", "1"), reply.headers)
        same.assert_not_called()
        self.assertEqual(len(self.sleeps), slept)  # refused, not queued

    def test_a_burst_compares_no_more_tokens_than_the_queue_holds(self) -> None:
        # Many wrong tokens arrive together while the queue has room for 4. Only the ones that
        # take a place may have their token compared; the rest are refused without it.
        attempts, places = 32, 4
        self.app.limiter = serve.FailureLimiter(1.0, max_waiting=places, sleep=self.sleep)
        compared: list[str] = []
        real = serve._same

        def slow_same(a: str, b: str) -> bool:
            compared.append(a)
            time.sleep(0.02)  # the comparison takes long enough for the others to arrive
            return real(a, b)

        barrier = threading.Barrier(attempts)
        replies: list[serve.Response] = []

        def attempt() -> None:
            barrier.wait(timeout=5)
            replies.append(self.app.handle(self.login_request("wrong")))

        with mock.patch.object(serve, "_same", slow_same):
            for _ in range(attempts):
                thread = threading.Thread(target=attempt, daemon=True)
                thread.start()
                self.threads.append(thread)
            deadline = time.monotonic() + 5
            while len(replies) < attempts - places and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertEqual(len(replies), attempts - places)  # the others are asleep
            self.assertLessEqual(len(compared), places)
            self.assertEqual({r.status for r in replies}, {429})
            self.assertEqual(len(self.sleeps), places)

    def test_what_does_not_offer_a_token_is_not_affected(self) -> None:
        self.fill_the_queue(serve.MAX_WAITING)
        cookie = {"cookie": f"{serve.COOKIE}={fresh_cookie()}"}
        reply = self.app.handle(serve.Request("GET", "/api/v1/devices", cookie))
        self.assertEqual(reply.status, 200)
        # a wrong bearer next to a valid cookie keeps being answered by the cookie
        both = {**cookie, "authorization": "Bearer wrong"}
        self.assertEqual(self.app.handle(serve.Request("GET", "/api/v1/devices", both)).status, 200)
        reply = self.app.handle(serve.Request("GET", "/api/v1/devices", {}))
        self.assertEqual((reply.status, self.code(reply)), (401, "unauthorized"))
        stale = {"cookie": f"{serve.COOKIE}=nope"}
        reply = self.app.handle(serve.Request("GET", "/api/v1/devices", stale))
        self.assertEqual(reply.status, 401)
        self.assertEqual(self.app.handle(serve.Request("GET", "/", {})).status, 200)
        self.assertEqual(self.app.handle(serve.Request("GET", "/healthz", {})).status, 200)

    def test_the_queue_drains_and_logins_work_again(self) -> None:
        self.fill_the_queue(serve.MAX_WAITING)
        self.release.set()
        for thread in self.threads:
            thread.join(timeout=5)
        reply = self.app.handle(self.login_request(TOKEN))
        self.assertEqual(reply.status, 204)
        reply = self.app.handle(self.login_request("wrong"))
        self.assertEqual((reply.status, self.code(reply)), (401, "unauthorized"))

    def test_a_flood_of_wrong_tokens_does_not_take_every_connection(self) -> None:
        connections = serve.MAX_WAITING + 4  # more than the queue holds, fewer than all slots
        server = serve.UiServer(("127.0.0.1", 0), self.app, max_connections=connections)
        thread = threading.Thread(
            target=server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True
        )
        thread.start()

        def stop() -> None:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)

        self.addCleanup(stop)
        port = server.server_address[1]

        def flood() -> None:
            conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
            try:
                body = json.dumps({"token": "wrong"})
                conn.request("POST", "/api/v1/session", body, {"Content-Type": "application/json"})
                response = conn.getresponse()
                self.answers.append(Reply(response.status, response.msg, response.read()))
            except BaseException as exc:
                self.answers.append(exc)
            finally:
                conn.close()

        # One at a time (the listen backlog is small): each flooder is either asleep in the
        # limiter or already answered before the next one connects.
        for sent in range(1, connections + 1):
            sender = threading.Thread(target=flood, daemon=True)
            sender.start()
            self.threads.append(sender)
            deadline = time.monotonic() + 5
            while len(self.sleeps) + len(self.answers) < sent and time.monotonic() < deadline:
                time.sleep(0.005)
            self.assertEqual(len(self.sleeps) + len(self.answers), sent)
        self.assertTrue(all(isinstance(a, Reply) for a in self.answers), self.answers)
        self.assertEqual([a.status for a in self.answers if isinstance(a, Reply)], [429] * 4)

        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
        try:
            headers = {"Cookie": f"{serve.COOKIE}={fresh_cookie()}"}
            conn.request("GET", "/api/v1/devices", headers=headers)
            self.assertEqual(conn.getresponse().status, 200)
        finally:
            conn.close()
        page = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
        try:
            page.request("GET", "/")
            self.assertEqual(page.getresponse().status, 200)
        finally:
            page.close()


class StaticTests(ServeCase):
    def test_the_page_is_served_without_auth_with_the_security_headers(self) -> None:
        for path in ("/", "/index.html"):
            reply = self.get(path, auth=False)
            self.assertEqual(reply.status, 200, path)
            self.assertEqual(reply.headers["Content-Type"], "text/html; charset=utf-8")
            self.assertEqual(reply.headers["X-Content-Type-Options"], "nosniff")
            self.assertEqual(reply.headers["Content-Security-Policy"], "default-src 'self'")
            self.assertIn(b"<title>ui</title>", reply.body)
        reply = self.get("/app.js", auth=False)
        self.assertEqual(reply.headers["Content-Type"], "text/javascript; charset=utf-8")

    def test_api_answers_carry_the_headers_too(self) -> None:
        reply = self.get("/api/v1/devices")
        self.assertEqual(reply.headers["X-Content-Type-Options"], "nosniff")
        self.assertEqual(reply.headers["Content-Type"], "application/json; charset=utf-8")
        self.assertNotIn("Access-Control-Allow-Origin", reply.headers)

    def test_traversal_absolute_paths_and_symlinks_are_refused(self) -> None:
        for path in (
            "/../secret.txt",
            "/%2e%2e/secret.txt",
            "/..%2fsecret.txt",
            "/%2e%2e%2fsecret.txt",
            "/a/../../secret.txt",
            "/a/%2e%2e/%2e%2e/secret.txt",
            "//secret.txt",
            "/%2Fetc/passwd",
            "/..%5csecret.txt",
            "/.hidden",
            "/leak.txt",  # a symlink to a file outside the folder
            "/nope.html",
            "/index.html/",
        ):
            reply = self.get(path, auth=False)
            self.assertEqual(reply.status, 404, path)
            self.assertNotIn(b"do not serve", reply.body, path)

    def test_only_get_serves_files(self) -> None:
        self.assertEqual(self.call("POST", "/index.html", {}, auth=False).status, 404)


class GamelistLimitTests(ServeCase):
    def test_a_gamelist_over_the_limit_gives_no_entries_and_is_not_read(self) -> None:
        files = serve.HubFiles(self.root, interval=0, media_seconds=0)
        with (
            mock.patch.object(artwork, "MAX_GAMELIST_BYTES", len(GAMELIST) - 1),
            mock.patch.object(
                Path, "read_bytes", autospec=True, side_effect=Path.read_bytes
            ) as read,
        ):
            self.assertEqual(files.extras("psx").meta, {})
            self.assertEqual(
                [call for call in read.call_args_list if call.args[0].name == "gamelist.xml"], []
            )
        # Within the limit the same file is read.
        self.assertEqual(
            list(serve.HubFiles(self.root, interval=0).extras("psx").meta),
            ["./Example Game (Europe).chd"],
        )


class LedgerLimitTests(ServeCase):
    def put_ledger(self, device: str = "deck") -> tuple[Path, str]:
        ledger = playstate.Ledger(
            device, device, {("psx", "Example Game (Europe).chd"): playstate.Record(plays=2)}
        )
        text = playstate.render_ledger(ledger)
        path = self.root / "frontend" / "play-state" / f"{device}.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
        return path, text

    def ledgers(self) -> tuple[playstate.Ledger, ...]:
        return serve.HubFiles(self.root, interval=0).play_ledgers()

    def test_a_ledger_over_the_limit_is_left_out_without_being_read_whole(self) -> None:
        path, text = self.put_ledger()
        size = len(text.encode())
        read: list[int] = []
        real = Path.open

        def spy(self: Path, *args: Any, **kwargs: Any) -> Any:
            handle = real(self, *args, **kwargs)
            if self.name != "deck.jsonl":
                return handle
            inner = handle.read

            def counting(n: int = -1) -> Any:
                data = inner(n)
                read.append(len(data))
                return data

            handle.read = counting
            return handle

        with (
            mock.patch.object(playstate_device, "MAX_LEDGER_BYTES", size - 1),
            mock.patch.object(Path, "open", spy),
            mock.patch.object(
                Path, "read_bytes", autospec=True, side_effect=Path.read_bytes
            ) as whole,
        ):
            self.assertEqual(self.ledgers(), ())
        self.assertEqual([c for c in whole.call_args_list if c.args[0] == path], [])
        self.assertLessEqual(sum(read), size)
        # Within the limit the same file is read.
        with mock.patch.object(playstate_device, "MAX_LEDGER_BYTES", size):
            (ledger,) = self.ledgers()
        self.assertEqual(ledger.device, "deck")

    def test_a_ledger_that_grows_after_the_size_check_is_still_bounded(self) -> None:
        path, text = self.put_ledger()
        asked: list[int] = []

        class Endless:
            def __enter__(self) -> "Endless":
                return self

            def __exit__(self, *exc: object) -> None:
                return None

            def read(self, n: int = -1) -> bytes:
                asked.append(n)
                return b"x" * (n if n >= 0 else 1 << 26)

        real = Path.open

        def grown(self: Path, *args: Any, **kwargs: Any) -> Any:
            return Endless() if self == path else real(self, *args, **kwargs)

        with (
            mock.patch.object(playstate_device, "MAX_LEDGER_BYTES", len(text.encode())),
            mock.patch.object(Path, "open", grown),
        ):
            self.assertEqual(self.ledgers(), ())
        self.assertEqual(asked, [len(text.encode()) + 1])

    def test_the_reader_names_the_limit_and_not_the_contents(self) -> None:
        path, text = self.put_ledger()
        with (
            mock.patch.object(playstate_device, "MAX_LEDGER_BYTES", len(text.encode()) - 1),
            self.assertRaises(playstate.LedgerError) as caught,
        ):
            playstate_device.read_ledger(path)
        self.assertIn(f"the limit is {len(text.encode()) - 1}", str(caught.exception))
        self.assertNotIn("Example Game", str(caught.exception))


class ReadTests(ServeCase):
    def test_devices(self) -> None:
        found = self.get("/api/v1/devices").json
        self.assertEqual([d["name"] for d in found], ["deck", "machine"])
        self.assertEqual(found[0]["profile"], "steamos-emudeck")
        self.assertEqual(found[1]["exclude_systems"], ["ps4"])
        self.assertEqual(found[0]["whitelist"]["state"], "managed")
        text = (self.library / "patterns-deck.stignore").read_bytes()
        self.assertEqual(found[0]["whitelist"]["etag"], library_api.api_etag(wl.etag(text)))

    def test_a_hand_written_or_missing_whitelist_is_reported(self) -> None:
        self.write_whitelist("deck", "!/roms/gba\n*\n")
        (self.library / "patterns-machine.stignore").unlink()
        found = {d["name"]: d["whitelist"] for d in self.get("/api/v1/devices").json}
        self.assertEqual(found["deck"]["state"], "needs-migration")
        self.assertEqual(found["machine"], {"state": "missing", "etag": "absent"})

    def test_systems_and_games(self) -> None:
        systems = {s["system"]: s for s in self.get("/api/v1/systems").json}
        self.assertEqual(sorted(systems), ["gba", "gbc", "ps4", "psx"])
        self.assertEqual(systems["gba"]["devices"]["deck"], {"state": "all", "whole": True})
        found = self.get("/api/v1/systems/psx/games").json
        item = next(g for g in found["games"] if g["id"] == EXAMPLE.id)
        self.assertEqual(item["name"], "Example Game")  # the scrape's name
        self.assertEqual(item["title"], "Example Game (Europe)")
        self.assertTrue(item["cover"])
        self.assertEqual(item["devices"], {"deck": "on", "machine": "off"})
        other = next(g for g in found["games"] if g["id"] == OTHER.id)
        self.assertEqual((other["name"], other["cover"]), ("Other Game", False))
        self.assertEqual(self.get("/api/v1/systems/nope/games").status, 404)

    def test_one_game_in_full(self) -> None:
        found = self.get(f"/api/v1/games/{EXAMPLE.id}").json
        self.assertEqual(
            found["meta"],
            {"name": "Example Game", "desc": "A fine game.", "rating": "0.8", "developer": "Dev"},
        )
        self.assertEqual(found["media"], ["covers"])
        self.assertEqual(found["parts"][0]["path"], EXAMPLE.primary)
        self.assertEqual(found["ra"]["state"], "no-list")
        self.assertEqual(self.get("/api/v1/games/" + "0" * 16).status, 404)

    def test_search(self) -> None:
        found = self.get("/api/v1/search?q=metal+gear").json
        self.assertEqual(found["groups"][0]["key"], "metal gear solid")
        self.assertEqual(len(found["groups"][0]["games"]), 2)
        for path in ("/api/v1/search", "/api/v1/search?q="):
            reply = self.get(path)
            self.assertEqual((reply.status, self.error(reply)["code"]), (400, "bad-request"), path)

    def test_changes_to_the_catalog_are_picked_up(self) -> None:
        self.assertEqual(len(self.get("/api/v1/systems").json), 4)
        (self.root / "catalog" / "systems" / "gba.json").unlink()
        self.assertEqual(len(self.get("/api/v1/systems").json), 3)

    def test_an_empty_or_missing_catalog_is_an_empty_library(self) -> None:
        for path in (self.root / "catalog" / "systems").glob("*.json"):
            path.unlink()
        self.assertEqual(self.get("/api/v1/systems").json, [])
        status = self.get("/api/v1/catalog").json
        self.assertEqual((status["ready"], status["systems"], status["games"]), (False, 0, 0))

    def test_catalog_status(self) -> None:
        status = self.get("/api/v1/catalog").json
        self.assertEqual(
            (status["ready"], status["systems"], status["games"], status["running"]),
            (True, 4, 7, False),
        )
        self.assertEqual(status["updated"], "2026-11-06T10:00:00Z")


class InboxTests(ServeCase):
    """`GET /api/v1/inbox` and `GET /api/v1/inbox/runs/{id}` over files the server only reads."""

    RUN = "20261113T101500Z"

    def inbox_dir(self) -> Path:
        path = self.root / "catalog" / "inbox"
        (path / "runs").mkdir(parents=True, exist_ok=True)
        return path

    def write_run(self, run_id: str, text: str | None = None, **extra: Any) -> Path:
        placed = {
            "system": "psx",
            "entry": "Example Game (Europe).chd",
            "outcome": "placed",
            "path": "library/roms/psx/Example Game (Europe).chd",
            "whitelist": [],
            "ra": [],
            "notes": [],
        }
        doc = {
            "v": 1,
            "id": run_id,
            "mode": "unattended",
            "started": "2026-11-13T10:15:00Z",
            "finished": "2026-11-13T10:21:40Z",
            "exit": 0,
            "items": [
                {
                    "item": "a.zip",
                    "outcome": "done",
                    "moved_to": "inbox/_done/a.zip",
                    "games": [
                        placed,
                        {**placed, "entry": "New.chd", "path": "library/roms/psx/New.chd"},
                    ],
                    "reasons": [],
                    "notes": [],
                }
            ],
            "counts": {"placed": 2, "already": 0, "review": 0, "held": 0, "waiting": 0},
            "whitelist": [],
            "purged": [],
            "warnings": [],
            **extra,
        }
        path = self.inbox_dir() / "runs" / f"{run_id}.json"
        path.write_text(json.dumps(doc) if text is None else text)
        return path

    def test_no_files_is_an_empty_answer_not_an_error(self) -> None:
        reply = self.get("/api/v1/inbox")
        self.assertEqual(reply.status, 200)
        self.assertEqual(
            reply.json,
            {
                "checked": None,
                "busy": None,
                "error": None,
                "waiting": [],
                "held": [],
                "runs": [],
                "last": None,
            },
        )

    def test_the_newest_record_comes_in_full_with_catalog_ids(self) -> None:
        self.write_run("20261112T090000Z")
        self.write_run(self.RUN)
        body = self.get("/api/v1/inbox").json
        self.assertEqual([r["id"] for r in body["runs"]], [self.RUN, "20261112T090000Z"])
        self.assertEqual(body["runs"][0]["placed"], 2)
        games = body["last"]["items"][0]["games"]
        self.assertEqual(body["last"]["id"], self.RUN)
        self.assertEqual(games[0]["id"], EXAMPLE.id)
        self.assertIsNone(games[1]["id"], "filed, but the catalog has no such game yet")

    def test_one_record_by_its_id(self) -> None:
        self.write_run("20261112T090000Z")
        reply = self.get("/api/v1/inbox/runs/20261112T090000Z")
        self.assertEqual(reply.status, 200)
        self.assertEqual(reply.json["id"], "20261112T090000Z")
        self.assertEqual(reply.json["items"][0]["games"][0]["id"], EXAMPLE.id)

    def test_an_unknown_run_is_404(self) -> None:
        self.write_run(self.RUN)
        for run_id in ("20250101T000000Z", "20261113T101500Z-2"):
            reply = self.get(f"/api/v1/inbox/runs/{run_id}")
            self.assertEqual(reply.status, 404, run_id)
            self.assertEqual(self.error(reply)["code"], "not-found")

    def test_an_id_that_is_not_a_record_id_is_404_and_reads_nothing_else(self) -> None:
        self.write_run(self.RUN)
        (self.inbox_dir() / "state.json").write_text("{}")
        for run_id in ("..", "%2e%2e%2fstate", "state", f"{self.RUN}.json", "2026111", "x%2Fy"):
            reply = self.get(f"/api/v1/inbox/runs/{run_id}")
            self.assertEqual(reply.status, 404, run_id)

    def test_a_record_of_another_id_or_damaged_is_not_served(self) -> None:
        self.write_run(self.RUN, "{ not json")
        self.assertEqual(self.get(f"/api/v1/inbox/runs/{self.RUN}").status, 404)
        self.write_run("20261113T111500Z", json.dumps({"v": 1, "id": "20261113T121500Z"}))
        self.assertEqual(self.get("/api/v1/inbox/runs/20261113T111500Z").status, 404)
        body = self.get("/api/v1/inbox").json
        self.assertEqual(body["runs"], [])
        self.assertIsNone(body["last"])

    def test_a_damaged_record_does_not_hide_the_others(self) -> None:
        self.write_run("20261112T090000Z")
        self.write_run(self.RUN, "[]")
        body = self.get("/api/v1/inbox").json
        self.assertEqual([r["id"] for r in body["runs"]], ["20261112T090000Z"])

    def test_a_symlinked_record_is_not_read(self) -> None:
        outside = self.tmp / "elsewhere.json"
        outside.write_text(json.dumps({"v": 1, "id": self.RUN, "items": []}))
        (self.inbox_dir() / "runs" / f"{self.RUN}.json").symlink_to(outside)
        self.assertEqual(self.get(f"/api/v1/inbox/runs/{self.RUN}").status, 404)
        self.assertEqual(self.get("/api/v1/inbox").json["runs"], [])

    def test_the_state_file_is_read_and_a_damaged_one_is_empty(self) -> None:
        t0 = datetime(2026, 11, 13, 9, 0, tzinfo=UTC)
        state = inbox_unattended.State(
            checked=t0 + timedelta(hours=1),
            items={
                "Other Game.zip": inbox_unattended.Entry(
                    "sha256:x", 1, 500, t0, t0, 0, inbox_unattended.Held("died twice", t0)
                ),
                "New.zip": inbox_unattended.Entry("sha256:y", 2, 900, t0, t0),
            },
            error=inbox_unattended.RunError("no DATs", 5, t0, 2, True),
        )
        (self.inbox_dir() / "state.json").write_text(inbox_unattended.render_state(state))
        body = self.get("/api/v1/inbox").json
        self.assertEqual(body["checked"], "2026-11-13T10:00:00Z")
        self.assertEqual(body["error"]["message"], "no DATs")
        self.assertEqual(body["held"][0]["item"], "Other Game.zip")
        self.assertEqual(body["held"][0]["reason"], "died twice")
        self.assertEqual(
            body["waiting"],
            [{"item": "New.zip", "since": "2026-11-13T09:00:00Z", "bytes": 900, "why": "space"}],
        )
        (self.inbox_dir() / "state.json").write_text("{ torn")
        body = self.get("/api/v1/inbox").json
        self.assertEqual(
            (body["checked"], body["error"], body["waiting"], body["held"]), (None, None, [], [])
        )

    def test_a_lock_makes_the_inbox_busy(self) -> None:
        lock = self.root / "inbox"
        lock.mkdir(parents=True)
        info = {"host": "h", "pid": 4, "started": 1_794_000_000, "beat": 60, "mode": "unattended"}
        (lock / ".warpzone.lock").write_text(json.dumps(info))
        busy = self.get("/api/v1/inbox").json["busy"]
        self.assertEqual(busy["mode"], "unattended")
        self.assertRegex(busy["since"], r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$")
        (lock / ".warpzone.lock").unlink()
        self.assertIsNone(self.get("/api/v1/inbox").json["busy"])

    def test_a_symlinked_lock_is_busy_and_never_followed(self) -> None:
        folder = self.root / "inbox"
        folder.mkdir(parents=True)
        outside = self.tmp / "lock-elsewhere"
        outside.write_text(json.dumps({"host": "h", "pid": 4, "started": 1_794_000_000}))
        (folder / ".warpzone.lock").symlink_to(outside)
        busy = self.get("/api/v1/inbox").json["busy"]
        self.assertEqual(busy["mode"], "unknown")

    @unittest.skipUnless(hasattr(os, "mkfifo"), "needs named pipes")
    def test_a_fifo_lock_does_not_hang_the_server(self) -> None:
        folder = self.root / "inbox"
        folder.mkdir(parents=True)
        os.mkfifo(folder / ".warpzone.lock")
        done: list[Reply] = []
        thread = threading.Thread(
            target=lambda: done.append(self.get("/api/v1/inbox")), daemon=True
        )
        thread.start()
        thread.join(5)
        self.assertFalse(thread.is_alive(), "the request hung on the named pipe")
        self.assertEqual(done[0].json["busy"]["mode"], "unknown")

    def test_a_huge_lock_is_busy_with_an_unknown_mode(self) -> None:
        folder = self.root / "inbox"
        folder.mkdir(parents=True)
        (folder / ".warpzone.lock").write_text(" " * (serve.INBOX_LOCK_MAX + 1))
        self.assertEqual(self.get("/api/v1/inbox").json["busy"]["mode"], "unknown")

    def test_the_routes_need_a_session(self) -> None:
        self.assertEqual(self.get("/api/v1/inbox", auth=False).status, 401)
        self.assertEqual(self.get(f"/api/v1/inbox/runs/{self.RUN}", auth=False).status, 401)

    def test_nothing_is_written(self) -> None:
        self.write_run(self.RUN)
        before = sorted(p for p in self.root.rglob("*") if p.is_file())
        self.get("/api/v1/inbox")
        self.get(f"/api/v1/inbox/runs/{self.RUN}")
        self.assertEqual(sorted(p for p in self.root.rglob("*") if p.is_file()), before)

    def test_a_changed_file_is_read_again(self) -> None:
        path = self.write_run(self.RUN)
        self.assertEqual(self.get("/api/v1/inbox").json["runs"][0]["placed"], 2)
        doc = json.loads(path.read_text())
        doc["counts"]["placed"] = 7
        path.write_text(json.dumps(doc, indent=2))
        self.assertEqual(self.get("/api/v1/inbox").json["runs"][0]["placed"], 7)


class MediaTests(ServeCase):
    def test_the_cover_is_served(self) -> None:
        reply = self.get(f"/api/v1/games/{EXAMPLE.id}/media/covers")
        self.assertEqual((reply.status, reply.body), (200, PNG))
        self.assertEqual(reply.headers["Content-Type"], "image/png")
        self.assertEqual(reply.headers["X-Content-Type-Options"], "nosniff")

    def test_a_ps4_game_finds_its_scrape_below_the_shortcuts_folder(self) -> None:
        # The catalog says `./shortcuts/Example PS4.desktop`; ES-DE reads that folder, so
        # the gamelist and the media on the hub are named without it.
        gamelist = self.root / "frontend" / "es-de" / "gamelists" / "ps4" / "gamelist.xml"
        gamelist.parent.mkdir(parents=True)
        gamelist.write_text(
            "<gameList><game><path>./Example PS4.desktop</path><name>Scraped</name>"
            "<desc>Text.</desc></game></gameList>"
        )
        covers = self.root / "frontend" / "media" / "ps4" / "covers"
        covers.mkdir(parents=True)
        (covers / "Example PS4.png").write_bytes(PNG)
        found = self.get(f"/api/v1/games/{PS4.id}").json
        self.assertEqual(found["meta"], {"name": "Scraped", "desc": "Text."})
        self.assertEqual(found["media"], ["covers"])
        reply = self.get(f"/api/v1/games/{PS4.id}/media/covers")
        self.assertEqual((reply.status, reply.body), (200, PNG))
        listed = self.get("/api/v1/systems/ps4/games").json
        item = next(g for g in listed["games"] if g["id"] == PS4.id)
        self.assertEqual((item["name"], item["cover"]), ("Scraped", True))

    def test_the_physical_media_scan_is_served_and_listed(self) -> None:
        folder = self.root / "frontend" / "media" / "psx" / "physicalmedia"
        folder.mkdir()
        (folder / "Example Game (Europe).png").write_bytes(PNG)
        found = self.get(f"/api/v1/games/{EXAMPLE.id}").json
        self.assertEqual(found["media"], ["covers", "physicalmedia"])
        reply = self.get(f"/api/v1/games/{EXAMPLE.id}/media/physicalmedia")
        self.assertEqual((reply.status, reply.body), (200, PNG))
        self.assertEqual(reply.headers["Content-Type"], "image/png")

    def video(self, data: bytes = bytes(range(256)) * 4, name: str = "mp4") -> bytes:
        folder = self.root / "frontend" / "media" / "psx" / "videos"
        folder.mkdir(exist_ok=True)
        (folder / f"Example Game (Europe).{name}").write_bytes(data)
        return data

    def test_a_video_is_listed_and_served_whole_with_its_type(self) -> None:
        data = self.video()
        self.assertIn("videos", self.get(f"/api/v1/games/{EXAMPLE.id}").json["media"])
        reply = self.get(f"/api/v1/games/{EXAMPLE.id}/media/videos")
        self.assertEqual((reply.status, reply.body), (200, data))
        self.assertEqual(reply.headers["Content-Type"], "video/mp4")
        self.assertEqual(reply.headers["Accept-Ranges"], "bytes")
        self.assertEqual(reply.headers["Content-Length"], str(len(data)))
        self.assertNotIn("Content-Range", reply.headers)

    def test_a_webm_video_has_its_type_and_the_other_kinds_are_served(self) -> None:
        self.video(b"webm", "webm")
        reply = self.get(f"/api/v1/games/{EXAMPLE.id}/media/videos")
        self.assertEqual(reply.headers["Content-Type"], "video/webm")
        for kind in ("backcovers", "titlescreens"):
            folder = self.root / "frontend" / "media" / "psx" / kind
            folder.mkdir()
            (folder / "Example Game (Europe).jpg").write_bytes(b"jpg")
            reply = self.get(f"/api/v1/games/{EXAMPLE.id}/media/{kind}")
            self.assertEqual((reply.status, reply.body), (200, b"jpg"), kind)
            self.assertEqual(reply.headers["Content-Type"], "image/jpeg")

    def test_a_range_answers_206_with_the_bytes_asked_for(self) -> None:
        data = self.video()
        path = f"/api/v1/games/{EXAMPLE.id}/media/videos"
        size = len(data)
        for header, first, last in (
            ("bytes=0-9", 0, 9),
            ("bytes=1000-", 1000, size - 1),
            ("bytes=-24", size - 24, size - 1),
            ("bytes=1020-99999", 1020, size - 1),
        ):
            reply = self.get(path, headers={"Range": header})
            self.assertEqual(reply.status, 206, header)
            self.assertEqual(reply.body, data[first : last + 1], header)
            self.assertEqual(reply.headers["Content-Range"], f"bytes {first}-{last}/{size}")
            self.assertEqual(reply.headers["Content-Length"], str(last - first + 1))
            self.assertEqual(reply.headers["Accept-Ranges"], "bytes")
            self.assertEqual(reply.headers["Content-Type"], "video/mp4")
            self.assertEqual(reply.headers["X-Content-Type-Options"], "nosniff")

    def test_a_range_past_the_end_answers_416(self) -> None:
        data = self.video()
        reply = self.get(
            f"/api/v1/games/{EXAMPLE.id}/media/videos", headers={"Range": f"bytes={len(data)}-"}
        )
        self.assertEqual(reply.status, 416)
        self.assertEqual(reply.headers["Content-Range"], f"bytes */{len(data)}")
        self.assertEqual(reply.body, b"")

    def test_a_range_that_is_ignored_answers_the_whole_file(self) -> None:
        data = self.video()
        for header in ("bytes=0-1,5-9", "items=0-1", "bytes=9-1"):
            reply = self.get(f"/api/v1/games/{EXAMPLE.id}/media/videos", headers={"Range": header})
            self.assertEqual((reply.status, reply.body), (200, data), header)

    def test_head_has_the_headers_and_no_body_and_images_take_ranges_too(self) -> None:
        data = self.video()
        reply = self.call("HEAD", f"/api/v1/games/{EXAMPLE.id}/media/videos")
        self.assertEqual((reply.status, reply.body), (200, b""))
        self.assertEqual(reply.headers["Content-Length"], str(len(data)))
        reply = self.get(f"/api/v1/games/{EXAMPLE.id}/media/covers", headers={"Range": "bytes=0-3"})
        self.assertEqual((reply.status, reply.body), (206, PNG[:4]))

    def test_a_video_is_sent_in_chunks_and_needs_auth(self) -> None:
        data = self.video(os.urandom(serve.CHUNK * 3 + 17))
        reply = self.get(f"/api/v1/games/{EXAMPLE.id}/media/videos")
        self.assertEqual(reply.body, data)
        reply = self.get(
            f"/api/v1/games/{EXAMPLE.id}/media/videos",
            headers={"Range": f"bytes={serve.CHUNK - 5}-{serve.CHUNK * 2 + 4}"},
        )
        self.assertEqual(reply.body, data[serve.CHUNK - 5 : serve.CHUNK * 2 + 5])
        self.assertEqual(
            self.get(f"/api/v1/games/{EXAMPLE.id}/media/videos", auth=False).status, 401
        )

    def test_media_needs_auth(self) -> None:
        self.assertEqual(
            self.get(f"/api/v1/games/{EXAMPLE.id}/media/covers", auth=False).status, 401
        )

    def test_missing_images_unknown_kinds_and_games_are_404(self) -> None:
        for path in (
            f"/api/v1/games/{OTHER.id}/media/covers",
            f"/api/v1/games/{EXAMPLE.id}/media/miximages",
            f"/api/v1/games/{EXAMPLE.id}/media/..%2f..%2fsecret",
            f"/api/v1/games/{EXAMPLE.id}/media/nope",
            f"/api/v1/games/{'0' * 16}/media/covers",
            f"/api/v1/games/..%2f{EXAMPLE.id}/media/covers",
        ):
            self.assertEqual(self.get(path).status, 404, path)

    def test_a_symlink_that_leaves_the_media_folder_is_not_served(self) -> None:
        found = self.get(f"/api/v1/games/{OTHER.id}").json
        self.assertEqual(found["media"], ["marquees"])  # the index sees it
        reply = self.get(f"/api/v1/games/{OTHER.id}/media/marquees")
        self.assertEqual(reply.status, 404)
        self.assertNotIn(b"do not serve", reply.body)

    def test_media_path_is_safe_for_names_with_dots_and_symlinked_folders(self) -> None:
        files = self.app.files
        self.assertIsNone(files.media_path("psx", "covers", "../covers/Example Game (Europe).png"))
        self.assertIsNone(files.media_path("psx", "covers", "/etc/passwd"))
        self.assertIsNone(files.media_path("psx", "covers", ".hidden.png"))
        self.assertIsNotNone(files.media_path("psx", "covers", "Example Game (Europe).png"))
        outside = self.tmp / "outside"
        outside.mkdir()
        (outside / "x.png").write_bytes(b"x")
        (self.root / "frontend" / "media" / "psx" / "covers" / "dir").symlink_to(outside)
        self.assertIsNone(files.media_path("psx", "covers", "dir/x.png"))


class MediaJailTests(ServeCase):
    """A symbolic link that Syncthing replicates into `frontend/media/` must not move the
    folder the media route trusts, and must not be followed after the check."""

    def setUp(self) -> None:
        super().setUp()
        self.media = self.root / "frontend" / "media"
        self.outside = self.tmp / "outside-photos"
        (self.outside / "covers").mkdir(parents=True)
        (self.outside / "covers" / "Other Game.png").write_bytes(b"PRIVATE")
        (self.outside / "Other Game.png").write_bytes(b"PRIVATE")

    def cover_url(self, game: Any = OTHER) -> str:
        return f"/api/v1/games/{game.id}/media/covers"

    def swapping(self, swap: Any) -> Any:
        """Patch `media_path` so `swap(path)` runs right after the check, before the open."""
        original = serve.HubFiles.media_path

        def media_path(files: serve.HubFiles, *args: str) -> Path | None:
            path = original(files, *args)
            if path is not None:
                swap(path)
            return path

        return mock.patch.object(serve.HubFiles, "media_path", media_path)

    def test_a_kind_folder_that_is_a_link_out_is_not_served_or_listed(self) -> None:
        shutil.rmtree(self.media / "psx" / "covers")
        (self.media / "psx" / "covers").symlink_to(self.outside)
        reply = self.get(self.cover_url())
        self.assertEqual(reply.status, 404)
        self.assertNotIn(b"PRIVATE", reply.body)
        self.assertNotIn("covers", self.get(f"/api/v1/games/{OTHER.id}").json["media"])
        self.assertIsNone(self.app.files.media_path("psx", "covers", "Other Game.png"))

    def test_a_system_folder_that_is_a_link_out_is_not_served_or_listed(self) -> None:
        shutil.rmtree(self.media / "psx")
        (self.media / "psx").symlink_to(self.outside)
        self.assertEqual(self.get(self.cover_url()).status, 404)
        self.assertEqual(self.get(self.cover_url(EXAMPLE)).status, 404)
        self.assertNotIn("covers", self.get(f"/api/v1/games/{OTHER.id}").json["media"])
        self.assertIsNone(self.app.files.media_path("psx", "covers", "Other Game.png"))

    def test_a_link_to_a_folder_inside_the_media_root_is_refused_too(self) -> None:
        # The simple rule: no link anywhere between the media root and the file.
        shutil.rmtree(self.media / "psx" / "marquees")
        (self.media / "psx" / "marquees").symlink_to(self.media / "psx" / "covers")
        self.assertIsNone(self.app.files.media_path("psx", "marquees", "Example Game (Europe).png"))
        self.assertNotIn("marquees", self.get(f"/api/v1/games/{EXAMPLE.id}").json["media"])
        covers = self.media / "psx" / "covers"
        (covers / "real").mkdir()
        (covers / "real" / "x.png").write_bytes(PNG)
        (covers / "alias").symlink_to(covers / "real")
        self.assertIsNotNone(self.app.files.media_path("psx", "covers", "real/x.png"))
        self.assertIsNone(self.app.files.media_path("psx", "covers", "alias/x.png"))

    def test_a_file_that_is_a_link_is_refused(self) -> None:
        covers = self.media / "psx" / "covers"
        (covers / "Other Game.png").symlink_to(covers / "Example Game (Europe).png")
        self.assertIsNone(self.app.files.media_path("psx", "covers", "Other Game.png"))
        self.assertEqual(self.get(self.cover_url()).status, 404)

    def test_safe_file_keeps_everything_inside_the_root_it_is_given(self) -> None:
        root = self.tmp / "root"
        (root / "a" / "b").mkdir(parents=True)
        (root / "a" / "b" / "f.png").write_bytes(PNG)
        self.assertEqual(
            serve.safe_file(root, root / "a" / "b", ("f.png",)), root.resolve() / "a/b/f.png"
        )
        self.assertEqual(
            serve.safe_file(root, root, ("a", "b", "f.png")), root.resolve() / "a/b/f.png"
        )
        self.assertIsNone(serve.safe_file(root, root / "a" / "b", ("..", "b", "f.png")))
        self.assertIsNone(serve.safe_file(root, root / "a" / "b", ("/etc/passwd",)))
        self.assertIsNone(serve.safe_file(root, root / "a" / "b", ()))
        self.assertIsNone(serve.safe_file(root, self.tmp, ("root", "a", "b", "f.png")))
        (root / "a" / "up").symlink_to(self.tmp)
        self.assertIsNone(serve.safe_file(root, root / "a" / "up", ("root", "a", "b", "f.png")))
        (root / "a" / "in").symlink_to(root / "a" / "b")
        self.assertIsNone(serve.safe_file(root, root / "a" / "in", ("f.png",)))
        self.assertIsNone(serve.safe_file(root, root / "a", ("in", "f.png")))

    def test_a_root_reached_through_a_link_still_works(self) -> None:
        real = self.tmp / "real"
        (real / "k").mkdir(parents=True)
        (real / "k" / "f.png").write_bytes(PNG)
        alias = self.tmp / "alias"
        alias.symlink_to(real)
        self.assertEqual(
            serve.safe_file(alias, alias / "k", ("f.png",)), real.resolve() / "k" / "f.png"
        )

    def test_the_static_route_follows_no_link_either(self) -> None:
        (self.ui / "real").mkdir()
        (self.ui / "real" / "x.js").write_text("// x")
        (self.ui / "alias").symlink_to(self.ui / "real")
        (self.ui / "out").symlink_to(self.outside)
        self.assertEqual(self.get("/real/x.js", auth=False).status, 200)
        for path in ("/alias/x.js", "/out/Other%20Game.png", "/leak.txt"):
            self.assertEqual(self.get(path, auth=False).status, 404, path)

    def test_a_file_swapped_for_a_link_after_the_check_is_not_opened(self) -> None:
        def swap(path: Path) -> None:
            path.unlink()
            path.symlink_to(self.secret)

        with self.swapping(swap):
            reply = self.get(self.cover_url(EXAMPLE))
        self.assertEqual(reply.status, 404)
        self.assertNotIn(b"do not serve", reply.body)

    def test_a_file_swapped_for_a_fifo_after_the_check_does_not_block(self) -> None:
        def swap(path: Path) -> None:
            path.unlink()
            os.mkfifo(path)

        with self.swapping(swap):
            reply = self.get(self.cover_url(EXAMPLE))
        self.assertEqual(reply.status, 404)

    def test_open_regular_opens_a_regular_file_and_nothing_else(self) -> None:
        plain = self.tmp / "plain.png"
        plain.write_bytes(PNG)
        opened = serve.open_regular(plain)
        assert opened is not None
        handle, size = opened
        with handle:
            self.assertEqual((size, handle.read()), (len(PNG), PNG))
        link = self.tmp / "link.png"
        link.symlink_to(plain)
        fifo = self.tmp / "fifo.png"
        os.mkfifo(fifo)
        for refused in (link, fifo, self.tmp, self.tmp / "missing.png"):
            self.assertIsNone(serve.open_regular(refused), refused.name)

    def test_a_regular_file_is_still_served_with_a_range(self) -> None:
        reply = self.get(self.cover_url(EXAMPLE), headers={"Range": "bytes=0-3"})
        self.assertEqual((reply.status, reply.body), (206, PNG[:4]))
        self.assertEqual(reply.headers["Content-Range"], f"bytes 0-3/{len(PNG)}")
        self.assertEqual(reply.headers["Cache-Control"], "private, max-age=3600")


class UnreadableWhitelistTests(ServeCase):
    """Synology ACLs: the file is there and the server's process cannot open it."""

    def setUp(self) -> None:
        super().setUp()
        self.path = self.library / "patterns-deck.stignore"
        self.path.chmod(0)
        self.addCleanup(self.path.chmod, 0o644)

    @unittest.skipIf(sys.platform == "win32" or os.geteuid() == 0, "root reads any file")
    def test_a_read_is_a_json_error_that_names_the_file_not_an_internal_one(self) -> None:
        reply = self.get("/api/v1/devices")
        self.assertEqual(reply.status, 500)
        error = self.error(reply)
        self.assertEqual(error["code"], "read-failed")
        self.assertIn("patterns-deck.stignore", error["message"])
        self.assertIn("Permission denied", error["message"])
        self.assertNotIn(str(self.root), error["message"])
        self.assertTrue(any("cannot read" in line and "Permission" in line for line in self.logs))
        self.assertFalse([line for line in self.logs if "internal" in line])


class ChangeTests(ServeCase):
    def whitelist(self, device: str) -> wl.Model:
        return wl.parse((self.library / f"patterns-{device}.stignore").read_text())

    def test_a_dry_run_says_what_would_change_and_writes_nothing(self) -> None:
        before = (self.library / "patterns-deck.stignore").read_bytes()
        reply = self.change(
            [{"device": "deck", "system": "psx", "game": OTHER.id, "on": True}], dry_run=True
        )
        self.assertEqual(reply.status, 200)
        found = reply.json
        self.assertTrue(found["dry_run"])
        (item,) = found["devices"]
        self.assertEqual(
            (
                item["device"],
                item["written"],
                item["on"],
                item["off"],
                item["bytes_on"],
                item["bytes_off"],
            ),
            ("deck", False, [OTHER.id], [], GIB, 0),
        )
        self.assertEqual(item["etag"], self.etags()["deck"])
        self.assertEqual((self.library / "patterns-deck.stignore").read_bytes(), before)
        self.assertFalse((self.root / "catalog" / "whitelists").exists())
        self.assertEqual(self.fake.writes(), [])

    def test_a_system_dry_run_reports_the_bytes_it_adds_per_device(self) -> None:
        reply = self.change([{"device": "*", "system": "ps4", "on": True}], dry_run=True)
        found = reply.json
        self.assertEqual([d["device"] for d in found["devices"]], ["deck"])
        self.assertEqual(found["devices"][0]["bytes_on"], 10 * GIB + 100)
        self.assertEqual(
            found["skipped"], [{"device": "machine", "system": "ps4", "reason": "unsupported"}]
        )

    def test_a_change_round_trip_edits_the_whitelist(self) -> None:
        old = (self.library / "patterns-deck.stignore").read_bytes()
        reply = self.change([{"device": "deck", "system": "psx", "game": OTHER.id, "on": True}])
        self.assertEqual(reply.status, 200, reply.body)
        (item,) = reply.json["devices"]
        self.assertTrue(item["written"])
        self.assertEqual(item["on"], [OTHER.id])
        # the file is the managed rendering of the new model, and the etag is its hash
        path = self.library / "patterns-deck.stignore"
        self.assertEqual(
            self.whitelist("deck").includes, frozenset({EXAMPLE.primary, OTHER.primary})
        )
        self.assertTrue(wl.is_managed(path.read_text()))
        self.assertEqual(item["etag"], self.etags()["deck"])
        # the old file is kept, and the hub is asked to scan the new one
        (backup,) = (self.root / "catalog" / "whitelists" / "deck").glob("*.stignore")
        self.assertEqual(backup.read_bytes(), old)
        self.assertIn(
            ("POST", "/rest/db/scan?folder=wz-library&sub=patterns-deck.stignore", None),
            self.fake.requests,
        )
        # no temporary file is left beside the whitelist
        self.assertEqual([p.name for p in self.library.glob(".*wz-tmp")], [])
        # and the games endpoint shows it
        found = self.get("/api/v1/systems/psx/games").json
        self.assertEqual(
            next(g for g in found["games"] if g["id"] == OTHER.id)["devices"]["deck"], "on"
        )

    def test_a_game_off_in_a_system_that_is_on_adds_an_exclusion(self) -> None:
        self.write_whitelist("deck", managed(["psx"]))
        reply = self.change([{"device": "deck", "system": "psx", "game": EXAMPLE.id, "on": False}])
        self.assertEqual(reply.status, 200, reply.body)
        self.assertEqual(self.whitelist("deck").excludes, frozenset({EXAMPLE.primary}))
        self.assertEqual(reply.json["devices"][0]["off"], [EXAMPLE.id])
        self.assertEqual(reply.json["devices"][0]["bytes_off"], 5 * GIB)

    def test_one_request_writes_each_whitelist_once(self) -> None:
        reply = self.change(
            [
                {"device": "*", "system": "gbc", "on": True},
                {"device": "deck", "system": "psx", "game": OTHER.id, "on": True},
            ]
        )
        self.assertEqual(reply.status, 200, reply.body)
        self.assertEqual([d["device"] for d in reply.json["devices"]], ["deck", "machine"])
        self.assertEqual(len(list((self.root / "catalog" / "whitelists" / "deck").glob("*"))), 1)
        self.assertEqual(self.whitelist("machine").systems, frozenset({"gbc"}))

    def test_a_missing_whitelist_is_created(self) -> None:
        (self.library / "patterns-machine.stignore").unlink()
        reply = self.change([{"device": "machine", "system": "gba", "on": True}])
        self.assertEqual(reply.status, 200, reply.body)
        self.assertEqual(self.whitelist("machine").systems, frozenset({"gba"}))
        # nothing to back up, but the write is recorded (an empty backup), for `pending`
        (backup,) = (self.root / "catalog" / "whitelists" / "machine").glob("*.stignore")
        self.assertEqual(backup.read_bytes(), b"")

    def test_a_created_whitelist_is_pending_like_any_edit(self) -> None:
        (self.library / "patterns-deck.stignore").unlink()
        self.fake.completion[("wz-library", DECK_ID)] = {"completion": 100, "needBytes": 0}
        self.start(progress_seconds=0)
        self.assertEqual(self.get("/api/v1/progress").json[0]["state"], "done")
        reply = self.change([{"device": "deck", "system": "gba", "on": True}])
        self.assertEqual(reply.status, 200, reply.body)
        self.assertEqual(self.get("/api/v1/progress").json[0]["state"], "pending")

    def test_a_stale_expect_is_412_and_nothing_is_written(self) -> None:
        etags = self.etags()
        self.write_whitelist("deck", managed(["gba", "gbc"]))  # someone else edited it
        before = {d: (self.library / f"patterns-{d}.stignore").read_bytes() for d in etags}
        body = {
            "changes": [
                {"device": "machine", "system": "gba", "on": True},
                {"device": "deck", "system": "psx", "on": True},
            ],
            "expect": etags,
        }
        reply = self.call("POST", "/api/v1/changes", body)
        error = self.error(reply)
        self.assertEqual((reply.status, error["code"], error["device"]), (412, "stale", "deck"))
        for device, data in before.items():
            self.assertEqual((self.library / f"patterns-{device}.stignore").read_bytes(), data)
        self.assertEqual(self.fake.writes(), [])

    def test_a_file_that_changes_between_the_check_and_the_write_is_stale(self) -> None:
        real = serve.store.replace

        def racing(root: Path, device: str, etag: str, build: Any, **kw: Any) -> list[str]:
            self.write_whitelist(device, managed(["gba", "gbc"]))
            return real(root, device, etag, build, **kw)

        with mock.patch.object(serve.store, "replace", racing):
            reply = self.change([{"device": "deck", "system": "psx", "on": True}])
        self.assertEqual((reply.status, self.error(reply)["code"]), (412, "stale"))
        self.assertEqual(self.whitelist("deck").systems, frozenset({"gba", "gbc"}))

    def test_expect_is_required_for_every_touched_device(self) -> None:
        body = {"changes": [{"device": "deck", "system": "psx", "on": True}]}
        reply = self.call("POST", "/api/v1/changes", body)
        error = self.error(reply)
        self.assertEqual(
            (reply.status, error["code"], error["device"]), (428, "expect-missing", "deck")
        )

    def test_turning_on_an_unsupported_system_is_422(self) -> None:
        for change in (
            {"device": "machine", "system": "ps4", "on": True},
            {"device": "machine", "system": "ps4", "game": PS4.id, "on": True},
        ):
            reply = self.change([change])
            self.assertEqual((reply.status, self.error(reply)["code"]), (422, "unsupported"))
        self.assertEqual(self.fake.writes(), [])

    def test_turning_off_an_unsupported_system_is_allowed(self) -> None:
        self.write_whitelist("machine", managed(["ps4"]))
        reply = self.change([{"device": "machine", "system": "ps4", "on": False}])
        self.assertEqual(reply.status, 200, reply.body)
        self.assertEqual(self.whitelist("machine").systems, frozenset())

    def test_a_hand_written_whitelist_is_not_managed(self) -> None:
        text = "#include patterns-common.stignore\n!/roms/gba\n*\n"
        self.write_whitelist("deck", text)
        reply = self.call(
            "POST",
            "/api/v1/changes",
            {"changes": [{"device": "deck", "system": "psx", "on": True}], "expect": {}},
        )
        error = self.error(reply)
        self.assertEqual(
            (reply.status, error["code"], error["device"]), (409, "not-managed", "deck")
        )
        self.assertEqual((self.library / "patterns-deck.stignore").read_text(), text)

    def test_an_unlistable_game_is_409(self) -> None:
        bad = by_title("psx", "Bad Name")
        reply = self.change([{"device": "deck", "system": "psx", "game": bad.id, "on": True}])
        error = self.error(reply)
        self.assertEqual((reply.status, error["code"], error["game"]), (409, "unlistable", bad.id))

    def test_covered_is_409(self) -> None:
        write_catalog(self.root, [("psx", library_game("roms/psx/Sub/Deep.chd", "Deep"))])
        self.write_whitelist("deck", managed(["psx"], excludes=["roms/psx/Sub"]))
        deep = library_game("roms/psx/Sub/Deep.chd", "Deep")
        reply = self.change([{"device": "deck", "system": "psx", "game": deep.id, "on": True}])
        self.assertEqual((reply.status, self.error(reply)["code"]), (409, "covered"))

    def test_unknown_things_are_404_and_bad_bodies_400(self) -> None:
        etags = self.etags()
        for change in (
            {"device": "tv", "system": "psx", "on": True},
            {"device": "deck", "system": "nope", "on": True},
            {"device": "deck", "system": "psx", "game": "0" * 16, "on": True},
            {"device": "deck", "system": "gba", "game": EXAMPLE.id, "on": True},
        ):
            reply = self.call("POST", "/api/v1/changes", {"changes": [change], "expect": etags})
            self.assertEqual(reply.status, 404, change)
        for raw in (b"{", b"[]", b'{"changes": [{}]}', b"\xff"):
            reply = self.call(
                "POST", "/api/v1/changes", raw=raw, headers={"Content-Type": "application/json"}
            )
            self.assertEqual((reply.status, self.error(reply)["code"]), (400, "bad-request"), raw)

    def test_a_body_over_64_kib_is_400(self) -> None:
        raw = json.dumps({"changes": [], "pad": "x" * 70_000}).encode()
        reply = self.call(
            "POST", "/api/v1/changes", raw=raw, headers={"Content-Type": "application/json"}
        )
        self.assertEqual((reply.status, self.error(reply)["code"]), (400, "bad-request"))

    def test_a_failure_after_a_write_is_500_with_the_results_of_each_device(self) -> None:
        real = serve.store.replace
        calls: list[str] = []

        def breaking(root: Path, device: str, etag: str, build: Any, **kw: Any) -> list[str]:
            calls.append(device)
            if len(calls) == 2:
                raise OSError(28, "No space left on device")
            return real(root, device, etag, build, **kw)

        with mock.patch.object(serve.store, "replace", breaking):
            reply = self.change([{"device": "*", "system": "gbc", "on": True}])
        self.assertEqual(reply.status, 500)
        found = reply.json
        self.assertEqual(found["error"]["code"], "write-failed")
        self.assertEqual(found["error"]["device"], "machine")
        self.assertEqual(
            [(d["device"], d["written"]) for d in found["devices"]],
            [("deck", True), ("machine", False)],
        )
        self.assertEqual(self.whitelist("deck").systems, frozenset({"gba", "gbc"}))
        self.assertEqual(self.whitelist("machine").systems, frozenset())  # whole, as it was

    def test_a_failed_scan_is_a_warning_not_an_error(self) -> None:
        self.start(client_factory=lambda: Client("http://127.0.0.1:1", API_KEY, label="hub"))
        reply = self.change([{"device": "deck", "system": "psx", "on": True}])
        self.assertEqual(reply.status, 200, reply.body)
        (item,) = reply.json["devices"]
        self.assertTrue(item["written"])
        self.assertIn("could not ask the hub to scan", item["warnings"][0])
        self.assertNotIn(API_KEY, reply.body.decode())

    def test_concurrent_requests_do_not_lose_an_update(self) -> None:
        # Two clients read the same etag; exactly one wins, the other is told it is stale.
        etags = self.etags()
        results: list[int] = []

        def post(system: str) -> None:
            body = {"changes": [{"device": "deck", "system": system, "on": True}], "expect": etags}
            results.append(self.call("POST", "/api/v1/changes", body).status)

        threads = [threading.Thread(target=post, args=(s,)) for s in ("psx", "gbc")]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(sorted(results), [200, 412])

    def test_the_hub_is_asked_outside_the_write_lock(self) -> None:
        held: list[bool] = []
        real_scan = serve.store.scan

        def scan(client: Any, device: str) -> Any:
            held.append(self.app.write_lock.locked())
            return real_scan(client, device)

        def factory() -> Client:
            held.append(self.app.write_lock.locked())
            return Client(self.fake.url, API_KEY, label="hub")

        self.start(client_factory=factory)
        with mock.patch.object(serve.store, "scan", scan):
            reply = self.change([{"device": "*", "system": "gbc", "on": True}])
        self.assertEqual(reply.status, 200, reply.body)
        self.assertEqual(held, [False, False, False])  # the client, then a scan per device
        self.assertFalse(self.app.write_lock.locked())

    def test_a_scan_that_hangs_does_not_hold_up_another_write(self) -> None:
        entered, release = threading.Event(), threading.Event()
        real_scan = serve.store.scan

        def scan(client: Any, device: str) -> Any:
            entered.set()
            release.wait(5)
            return real_scan(client, device)

        results: list[int] = []

        def first() -> None:
            results.append(self.change([{"device": "deck", "system": "psx", "on": True}]).status)

        with mock.patch.object(serve.store, "scan", scan):
            thread = threading.Thread(target=first)
            thread.start()
            self.assertTrue(entered.wait(5))
            # the first write is on disk and its scan is waiting: the lock is free
            self.assertTrue(self.app.write_lock.acquire(timeout=1))
            self.app.write_lock.release()
            release.set()
            thread.join(5)
        self.assertEqual(results, [200])

    def test_a_stale_file_is_refused_without_writing_or_asking_the_hub(self) -> None:
        etags = self.etags()
        self.write_whitelist("deck", managed(["gba", "gbc"]))
        before = self.fake.requests[:]
        body = {"changes": [{"device": "deck", "system": "psx", "on": True}], "expect": etags}
        reply = self.call("POST", "/api/v1/changes", body)
        self.assertEqual((reply.status, self.error(reply)["code"]), (412, "stale"))
        self.assertEqual(self.fake.requests, before)
        self.assertEqual(self.whitelist("deck").systems, frozenset({"gba", "gbc"}))


def library_game(path: str, title: str) -> catalog.Game:
    from library_support import game

    return game("psx", path, title)


class HistoryTests(ServeCase):
    """The backups of a whitelist, their diff and the restore."""

    def setUp(self) -> None:
        super().setUp()
        self.path = self.library / "patterns-deck.stignore"
        self.original = self.path.read_bytes()
        self.backups = self.root / "catalog" / "whitelists" / "deck"
        # turn the example game off: the original is kept as the one backup
        reply = self.change([{"device": "deck", "system": "psx", "game": EXAMPLE.id, "on": False}])
        self.assertEqual(reply.status, 200, reply.body)
        self.edited = self.path.read_bytes()
        (found,) = self.backups.glob("*.stignore")
        self.name = found.name
        self.base = f"/api/v1/devices/deck/backups/{self.name}"

    def restore(self, name: str | None = None, **extra: Any) -> Reply:
        body = {"backup": name or self.name, "expect": self.etags()["deck"], **extra}
        return self.call("POST", "/api/v1/devices/deck/restore", body)

    def test_the_backups_are_listed_newest_first_against_the_current_file(self) -> None:
        reply = self.get("/api/v1/devices/deck/backups")
        self.assertEqual(reply.status, 200, reply.body)
        found = reply.json
        self.assertEqual((found["device"], found["etag"]), ("deck", self.etags()["deck"]))
        (item,) = found["backups"]
        self.assertEqual(item["name"], self.name)
        self.assertRegex(item["time"], r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$")
        self.assertEqual(
            (item["empty"], item["same"], item["included"], item["excluded"]),
            (False, False, 1, 0),
        )
        self.assertIn("back on", item["summary"])
        newer = self.backups / "29990101T000000.000000Z.stignore"
        newer.write_bytes(self.edited)
        names = [b["name"] for b in self.get("/api/v1/devices/deck/backups").json["backups"]]
        self.assertEqual(names, [newer.name, self.name])

    def test_a_device_without_backups_has_an_empty_list(self) -> None:
        found = self.get("/api/v1/devices/machine/backups").json
        self.assertEqual(found["backups"], [])

    def test_one_backup_is_shown_with_its_diff_and_the_games_it_changes(self) -> None:
        reply = self.get(self.base)
        self.assertEqual(reply.status, 200, reply.body)
        found = reply.json
        self.assertEqual(found["backup"], self.name)
        self.assertEqual(found["etag"], self.etags()["deck"])
        self.assertFalse(found["same"])
        self.assertTrue(found["managed"])
        self.assertEqual(found["included"], [EXAMPLE.primary])
        self.assertEqual(found["excluded"], [])
        self.assertEqual((found["on"], found["off"]), ([EXAMPLE.id], []))
        self.assertEqual((found["bytes_on"], found["bytes_off"]), (5 * GIB, 0))
        self.assertIn("+!/" + EXAMPLE.primary, found["diff"])
        self.assertIn("back on", found["warning"])
        self.assertEqual(self.path.read_bytes(), self.edited)

    def test_a_restore_writes_the_backup_after_backing_up_the_current_file(self) -> None:
        reply = self.restore()
        self.assertEqual(reply.status, 200, reply.body)
        found = reply.json
        self.assertTrue(found["written"])
        self.assertFalse(found["dry_run"])
        self.assertEqual(found["on"], [EXAMPLE.id])
        self.assertEqual(self.path.read_bytes(), self.original)
        self.assertEqual(found["etag"], self.etags()["deck"])
        names = sorted(p.name for p in self.backups.glob("*.stignore"))
        self.assertEqual(len(names), 2)
        self.assertEqual((self.backups / names[-1]).read_bytes(), self.edited)
        self.assertIn(
            ("POST", "/rest/db/scan?folder=wz-library&sub=patterns-deck.stignore", None),
            self.fake.requests,
        )
        games = self.get("/api/v1/systems/psx/games").json["games"]
        self.assertEqual(next(g for g in games if g["id"] == EXAMPLE.id)["devices"]["deck"], "on")
        self.assertEqual([p.name for p in self.library.glob(".*wz-tmp")], [])

    def test_a_dry_run_writes_nothing(self) -> None:
        before = len(self.fake.requests)
        reply = self.restore(dry_run=True)
        self.assertEqual(reply.status, 200, reply.body)
        self.assertEqual((reply.json["written"], reply.json["dry_run"]), (False, True))
        self.assertEqual(reply.json["etag"], self.etags()["deck"])
        self.assertEqual(self.path.read_bytes(), self.edited)
        self.assertEqual(len(self.fake.requests), before)
        self.assertEqual(len(list(self.backups.glob("*.stignore"))), 1)

    def test_restoring_what_is_current_writes_nothing(self) -> None:
        current = self.backups / "29990101T000000.000000Z.stignore"
        current.write_bytes(self.edited)
        reply = self.restore(current.name)
        self.assertEqual(reply.status, 200, reply.body)
        self.assertEqual((reply.json["written"], reply.json["same"]), (False, True))
        self.assertEqual(len(list(self.backups.glob("*.stignore"))), 2)

    def test_the_etag_is_required_and_checked(self) -> None:
        reply = self.call("POST", "/api/v1/devices/deck/restore", {"backup": self.name})
        error = self.error(reply)
        self.assertEqual(
            (reply.status, error["code"], error["device"]), (428, "expect-missing", "deck")
        )
        stale = {"backup": self.name, "expect": self.etags()["machine"]}
        reply = self.call("POST", "/api/v1/devices/deck/restore", stale)
        self.assertEqual((reply.status, self.error(reply)["code"]), (412, "stale"))
        self.assertEqual(self.path.read_bytes(), self.edited)

    def test_a_file_that_changes_between_the_check_and_the_write_is_stale(self) -> None:
        real = serve.store.replace

        def racing(root: Path, device: str, etag: str, build: Any, **kw: Any) -> list[str]:
            self.write_whitelist(device, managed(["gba", "gbc"]))
            return real(root, device, etag, build, **kw)

        with mock.patch.object(serve.store, "replace", racing):
            reply = self.restore()
        self.assertEqual((reply.status, self.error(reply)["code"]), (412, "stale"))
        self.assertEqual(self.path.read_text(), managed(["gba", "gbc"]))

    def test_an_empty_backup_is_not_restorable(self) -> None:
        name = "20250101T000000.000000Z.stignore"
        (self.backups / name).write_bytes(b"")
        reply = self.restore(name)
        self.assertEqual((reply.status, self.error(reply)["code"]), (409, "not-restorable"))
        reply = self.get(f"/api/v1/devices/deck/backups/{name}")
        self.assertEqual((reply.status, self.error(reply)["code"]), (409, "not-restorable"))
        listed = self.get("/api/v1/devices/deck/backups").json["backups"]
        self.assertTrue(next(b for b in listed if b["name"] == name)["empty"])
        self.assertEqual(self.path.read_bytes(), self.edited)

    def test_a_hand_written_backup_can_be_restored_and_says_it_is_not_managed(self) -> None:
        hand = b"#include patterns-common.stignore\n!/roms/gba\n*\n"
        name = "20250102T000000.000000Z.stignore"
        (self.backups / name).write_bytes(hand)
        found = self.get(f"/api/v1/devices/deck/backups/{name}").json
        self.assertFalse(found["managed"])
        self.assertEqual(self.restore(name).status, 200)
        self.assertEqual(self.path.read_bytes(), hand)
        reply = self.change([{"device": "deck", "system": "psx", "on": True}])
        self.assertEqual((reply.status, self.error(reply)["code"]), (409, "not-managed"))

    def test_a_missing_whitelist_is_created_from_the_backup(self) -> None:
        self.path.unlink()
        reply = self.restore()
        self.assertEqual(reply.status, 200, reply.body)
        self.assertEqual(self.path.read_bytes(), self.original)

    def test_unknown_devices_backups_and_bad_names(self) -> None:
        for path in (
            "/api/v1/devices/tablet/backups",
            "/api/v1/devices/tablet/backups/" + self.name,
            "/api/v1/devices/deck/backups/29990101T000000.000000Z.stignore",
            "/api/v1/devices/deck/backups/notes.txt",
            "/api/v1/devices/deck/backups/..%2Fpatterns-deck.stignore",
        ):
            reply = self.get(path)
            self.assertEqual((reply.status, self.error(reply)["code"]), (404, "not-found"), path)
        body = {"backup": "29990101T000000.000000Z.stignore", "expect": self.etags()["deck"]}
        reply = self.call("POST", "/api/v1/devices/deck/restore", body)
        self.assertEqual((reply.status, self.error(reply)["code"]), (404, "not-found"))
        reply = self.call("POST", "/api/v1/devices/tablet/restore", body)
        self.assertEqual((reply.status, self.error(reply)["code"]), (404, "not-found"))
        for bad in (
            {},
            {"backup": "../x", "expect": "absent"},
            {"backup": self.name, "expect": 3},
            {"backup": self.name, "expect": "absent", "dry_run": "yes"},
            {"backup": self.name, "expect": "absent", "extra": 1},
            [1],
        ):
            reply = self.call("POST", "/api/v1/devices/deck/restore", bad)
            self.assertEqual((reply.status, self.error(reply)["code"]), (400, "bad-request"), bad)
        self.assertEqual(self.path.read_bytes(), self.edited)

    def test_the_same_auth_and_origin_rules_as_every_write(self) -> None:
        body = {"backup": self.name, "expect": self.etags()["deck"]}
        for path in ("/api/v1/devices/deck/backups", self.base):
            self.assertEqual(self.get(path, auth=False).status, 401)
        reply = self.call("POST", "/api/v1/devices/deck/restore", body, auth=False)
        self.assertEqual(reply.status, 401)
        cookie = {"Cookie": f"wz_session={fresh_cookie()}"}
        reply = self.call("POST", "/api/v1/devices/deck/restore", body, headers=cookie, auth=False)
        self.assertEqual((reply.status, self.error(reply)["code"]), (403, "bad-origin"))
        good = {**cookie, "Origin": f"http://127.0.0.1:{self.port}"}
        reply = self.call("POST", "/api/v1/devices/deck/restore", body, headers=good, auth=False)
        self.assertEqual(reply.status, 200, reply.body)
        self.assertEqual(self.path.read_bytes(), self.original)

    def test_a_get_on_restore_and_a_post_on_a_backup_are_not_routes(self) -> None:
        self.assertEqual(self.get("/api/v1/devices/deck/restore").status, 404)
        self.assertEqual(self.call("POST", self.base, {}).status, 404)

    def test_a_failed_scan_is_a_warning(self) -> None:
        self.start(client_factory=lambda: Client("http://127.0.0.1:1", API_KEY, label="hub"))
        reply = self.restore()
        self.assertEqual(reply.status, 200, reply.body)
        self.assertEqual(len(reply.json["warnings"]), 1)
        self.assertEqual(self.path.read_bytes(), self.original)
        self.assertNotIn(API_KEY, reply.body.decode())


class ProgressTests(ServeCase):
    def test_states_from_the_hub(self) -> None:
        found = {p["device"]: p for p in self.get("/api/v1/progress").json}
        self.assertEqual(
            found["deck"] | {"checked": "x"},
            {
                "device": "deck",
                "state": "downloading",
                "percent": 42,
                "need_bytes": 31_000_000_000,
                "need_items": 812,
                "checked": "x",
            },
        )
        self.assertEqual(found["machine"]["state"], "offline")

    def test_a_device_the_hub_does_not_share_the_folder_with(self) -> None:
        self.fake.connections[MACHINE_ID] = {"connected": True}  # no completion: a 404
        found = {p["device"]: p["state"] for p in self.get("/api/v1/progress").json}
        self.assertEqual(found["machine"], "not-shared")

    def test_a_recent_write_is_pending(self) -> None:
        self.fake.completion[("wz-library", DECK_ID)] = {"completion": 100, "needBytes": 0}
        self.change([{"device": "deck", "system": "psx", "on": True}])
        self.assertEqual(self.get("/api/v1/progress").json[0]["state"], "pending")

    def test_the_answer_is_cached_for_15_seconds(self) -> None:
        clock = [0.0]
        self.start(clock=lambda: clock[0])
        self.get("/api/v1/progress")
        asked = len(self.fake.requests)
        self.get("/api/v1/progress")
        self.assertEqual(len(self.fake.requests), asked)
        clock[0] = 16.0
        self.get("/api/v1/progress")
        self.assertGreater(len(self.fake.requests), asked)

    def test_an_unreachable_hub_is_503(self) -> None:
        self.start(client_factory=lambda: Client("http://127.0.0.1:1", API_KEY, label="hub"))
        reply = self.get("/api/v1/progress")
        self.assertEqual((reply.status, self.error(reply)["code"]), (503, "hub-unreachable"))
        self.assertNotIn(API_KEY, reply.body.decode())

    def test_a_failure_is_cached_for_a_few_seconds(self) -> None:
        clock = [0.0]
        calls = []

        def factory() -> Client:
            calls.append(clock[0])
            return Client("http://127.0.0.1:1", API_KEY, label="hub")

        self.start(client_factory=factory, clock=lambda: clock[0])
        for _ in range(3):
            self.assertEqual(self.get("/api/v1/progress").status, 503)
        self.assertEqual(calls, [0.0])  # one question; the other polls got the cached failure
        clock[0] = 4.0
        reply = self.get("/api/v1/progress")
        self.assertEqual((reply.status, self.error(reply)["code"]), (503, "hub-unreachable"))
        self.assertEqual(calls, [0.0])
        clock[0] = 6.0
        self.assertEqual(self.get("/api/v1/progress").status, 503)
        self.assertEqual(calls, [0.0, 6.0])  # asked again once the failure expired

    def test_the_hub_coming_back_is_noticed_after_the_failure_expires(self) -> None:
        clock = [0.0]
        up = [False]

        def factory() -> Client:
            url = self.fake.url if up[0] else "http://127.0.0.1:1"
            return Client(url, API_KEY, label="hub")

        self.start(client_factory=factory, clock=lambda: clock[0])
        self.assertEqual(self.get("/api/v1/progress").status, 503)
        up[0] = True
        self.assertEqual(self.get("/api/v1/progress").status, 503)  # still the cached failure
        clock[0] = 6.0
        self.assertEqual(self.get("/api/v1/progress").status, 200)
        up[0] = False
        clock[0] = 7.0
        self.assertEqual(self.get("/api/v1/progress").status, 200)  # a success keeps its 15 s

    def test_no_hub_client_is_503(self) -> None:
        self.start(client_factory=None)
        self.assertEqual(self.get("/api/v1/progress").status, 503)


class RefreshTests(ServeCase):
    def test_a_refresh_starts_a_run_and_a_second_one_is_409_while_it_runs(self) -> None:
        release = threading.Event()
        started = threading.Event()

        def job(ra_lists: bool) -> str:
            started.set()
            release.wait(10)
            return "1 system(s)"

        self.start(refresher=serve.Refresher(job, self.logs.append))
        reply = self.call("POST", "/api/v1/catalog/refresh", {})
        self.assertEqual((reply.status, reply.json), (202, {"started": True}))
        self.assertTrue(started.wait(5))
        self.assertTrue(self.get("/api/v1/catalog").json["running"])
        reply = self.call("POST", "/api/v1/catalog/refresh", {})
        self.assertEqual((reply.status, self.error(reply)["code"]), (409, "catalog-running"))
        release.set()
        for _ in range(100):
            if not self.get("/api/v1/catalog").json["running"]:
                break
            threading.Event().wait(0.05)
        last = self.get("/api/v1/catalog").json["last_run"]
        self.assertEqual((last["ok"], last["message"]), (True, "1 system(s)"))
        self.assertEqual(self.call("POST", "/api/v1/catalog/refresh", {}).status, 202)

    def test_a_finished_run_shows_in_the_next_read_not_after_the_cache_interval(self) -> None:
        # The page reloads the moment `running` turns false; the server must not answer from a
        # catalog it read before the run, even when it checked a moment ago.
        root = self.tmp / "fresh"
        (root / "library" / ".stfolder").mkdir(parents=True)
        (root / "library" / "roms" / "gba").mkdir(parents=True)
        (root / "library" / "roms" / "gba" / "Tiny.gba").write_bytes(b"x")
        files = serve.HubFiles(root, interval=3600, media_seconds=0)
        job = serve.catalog_job(root, rahasher=None, ra_key=None, log=lambda text: None)
        refresher = serve.Refresher(job, lambda text: None)
        self.start(root=root, files=files, refresher=refresher)
        self.assertEqual(self.get("/api/v1/systems").json, [])  # read once, before the run
        self.assertFalse(self.get("/api/v1/catalog").json["ready"])
        refresher.run_now()
        self.assertEqual([s["system"] for s in self.get("/api/v1/systems").json], ["gba"])
        self.assertTrue(self.get("/api/v1/catalog").json["ready"])

    def test_a_failing_run_is_recorded(self) -> None:
        done = threading.Event()

        def job(ra_lists: bool) -> str:
            try:
                raise ValueError("boom with a secret")
            finally:
                done.set()

        refresher = serve.Refresher(job, self.logs.append)
        self.start(refresher=refresher)
        self.call("POST", "/api/v1/catalog/refresh", {})
        done.wait(5)
        for _ in range(100):
            if not refresher.running:
                break
            threading.Event().wait(0.02)
        last = self.get("/api/v1/catalog").json["last_run"]
        self.assertEqual((last["ok"], last["message"]), (False, "ValueError"))  # the kind only

    def test_refresh_needs_json_and_auth(self) -> None:
        self.assertEqual(
            self.call("POST", "/api/v1/catalog/refresh", auth=False, raw=b"{}").status, 401
        )
        reply = self.call(
            "POST", "/api/v1/catalog/refresh", raw=b"{}", headers={"Content-Type": "text/plain"}
        )
        self.assertEqual(reply.status, 400)


class RefresherTests(unittest.TestCase):
    def test_the_job_gets_the_ra_flag_and_the_schedule_runs_until_stopped(self) -> None:
        seen: list[bool] = []
        stop = threading.Event()

        def job(ra_lists: bool) -> str:
            seen.append(ra_lists)
            if len(seen) == 3:
                stop.set()
            return "ok"

        refresher = serve.Refresher(job, lambda text: None)
        refresher.schedule(stop, key=True, period=0.01, ra_period=3600)
        self.assertEqual(seen, [True, False, False])

    def test_without_a_key_the_lists_are_never_asked_for(self) -> None:
        seen: list[bool] = []
        stop = threading.Event()

        def job(ra_lists: bool) -> str:
            seen.append(ra_lists)
            stop.set()
            return "ok"

        serve.Refresher(job, lambda text: None).schedule(stop, key=False, period=0.01)
        self.assertEqual(seen, [False])

    def test_catalog_job_scans_the_library(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "library" / "roms" / "gba").mkdir(parents=True)
            (root / "library" / "roms" / "gba" / "Tiny.gba").write_bytes(b"x")
            lines: list[str] = []
            message = serve.catalog_job(root, rahasher=None, ra_key=None, log=lines.append)(False)
            self.assertEqual(message, "1 system(s)")
            self.assertTrue((root / "catalog" / "systems" / "gba.json").is_file())


class HardeningTests(ServeCase):
    def raw(self, data: bytes, wait: float = 5.0) -> bytes:
        sock = socket.create_connection(("127.0.0.1", self.port), timeout=wait)
        try:
            sock.sendall(data)
            chunks = []
            while True:
                try:
                    part = sock.recv(65536)
                except OSError:
                    break
                if not part:
                    break
                chunks.append(part)
            return b"".join(chunks)
        finally:
            sock.close()

    # --- 1. brute force

    def test_a_wrong_bearer_is_delayed_like_a_wrong_login(self) -> None:
        self.start(login_delay=1.0)
        wrong = {"Authorization": "Bearer wrong"}
        with mock.patch.object(serve.time, "sleep") as sleep:
            reply = self.get("/api/v1/devices", headers=wrong, auth=False)
        self.assertEqual(reply.status, 401)
        sleep.assert_called_once_with(1.0)

    def test_no_credentials_and_a_right_token_are_not_delayed(self) -> None:
        self.start(login_delay=1.0)
        with mock.patch.object(serve.time, "sleep") as sleep:
            self.get("/api/v1/devices", auth=False)
            self.get("/api/v1/devices")
        sleep.assert_not_called()

    def test_wrong_bearers_and_wrong_logins_share_one_limiter(self) -> None:
        self.start(login_delay=1.0)
        with mock.patch.object(serve.time, "sleep") as sleep:
            self.get("/api/v1/devices", headers={"Authorization": "Bearer a"}, auth=False)
            self.call("POST", "/api/v1/session", {"token": "b"}, auth=False)
        first, second = (c.args[0] for c in sleep.call_args_list)
        self.assertEqual(first, 1.0)
        self.assertAlmostEqual(second, 2.0, delta=0.5)  # queued behind the first

    # --- 2. slow and idle connections

    def test_the_handler_times_out_by_default(self) -> None:
        self.assertEqual(serve._Handler.timeout, 15)

    def test_idle_and_stalled_connections_free_their_threads(self) -> None:
        # The timeout is long enough for a slow runner to start all seven threads before the
        # first one gives up, and the peak is polled for rather than read after a fixed wait.
        with mock.patch.object(serve._Handler, "timeout", 1.5):
            before = threading.active_count()
            idle = [socket.create_connection(("127.0.0.1", self.port)) for _ in range(6)]
            stalled = socket.create_connection(("127.0.0.1", self.port))
            stalled.sendall(
                b"POST /api/v1/changes HTTP/1.1\r\nHost: x\r\nContent-Length: 10\r\n\r\nab"
            )
            peak = threading.active_count()
            for _ in range(40):
                if peak >= before + 7:
                    break
                threading.Event().wait(0.025)
                peak = max(peak, threading.active_count())
            self.assertGreaterEqual(peak, before + 7)
            for _ in range(100):
                if threading.active_count() <= before:
                    break
                threading.Event().wait(0.1)
            self.assertLessEqual(threading.active_count(), before)
            for sock in [*idle, stalled]:
                sock.close()
        self.assertTrue(any("connection" in line for line in self.logs), self.logs)

    def test_connections_above_the_cap_are_closed_at_once(self) -> None:
        server = serve.UiServer(("127.0.0.1", 0), self.app, max_connections=2)
        thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.02})
        thread.daemon = True
        thread.start()
        port = server.server_address[1]
        held = [socket.create_connection(("127.0.0.1", port)) for _ in range(2)]
        try:
            threading.Event().wait(0.3)
            extra = socket.create_connection(("127.0.0.1", port), timeout=3)
            self.assertEqual(extra.recv(10), b"")  # closed, not served
            extra.close()
            for sock in held:
                sock.close()
            threading.Event().wait(0.5)  # their threads end on EOF and give the slots back
            conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
            conn.request("GET", "/healthz")
            self.assertEqual(conn.getresponse().status, 200)
            conn.close()
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)

    # --- 3. cookies

    def test_the_session_cookie_is_found_among_other_cookies(self) -> None:
        value = fresh_cookie()
        for header in (
            f"pref=a b; wz_session={value}",
            f'st={{"t":1}}; wz_session={value}',
            f"wz_session=bad; wz_session={value}",
            f"a=1;wz_session={value};b=2",
            f"  wz_session = {value}  ",
        ):
            self.assertEqual(self.cookie_get(header), 200, header)
        for header in ("pref=a b; wz_session=nope", "wz_session", f"x_wz_session={value}"):
            self.assertEqual(self.cookie_get(header), 401, header)

    # --- 4. bad request lines

    def test_a_malformed_request_line_is_answered_and_does_not_crash_the_log(self) -> None:
        reply = self.raw(b"NONSENSE\r\n\r\n")
        self.assertTrue(reply.startswith(b"HTTP/1.0 400"), reply[:40])
        reply = self.raw(b"GET /" + b"a" * 70000 + b" HTTP/1.1\r\n\r\n")
        self.assertTrue(reply.startswith(b"HTTP/1.0 414"), reply[:40])
        self.assertFalse([line for line in self.logs if "internal" in line or "Error" in line])

    def test_control_characters_never_reach_the_log(self) -> None:
        self.raw(b"GET /\x1b[31mred\x07 HTTP/1.1\r\nHost: x\r\n\r\n")
        self.assertTrue(self.logs)
        for line in self.logs:
            self.assertTrue(line.isprintable(), repr(line))
        self.assertTrue(any("\\x1b" in line for line in self.logs), self.logs)

    # --- 5. TLS proxy

    def test_a_cookie_write_from_the_https_origin_of_the_host_is_accepted(self) -> None:
        value = fresh_cookie()
        body = {"changes": [], "expect": {}, "dry_run": True}
        for origin, ok in (
            ("https://example.lan", True),
            ("http://example.lan", True),
            ("https://evil.example", False),
            ("ftp://example.lan", False),
        ):
            reply = self.call(
                "POST",
                "/api/v1/changes",
                body,
                headers={"Cookie": f"wz_session={value}", "Origin": origin, "Host": "example.lan"},
                auth=False,
            )
            self.assertEqual(reply.status == 403, not ok, (origin, reply.status))

    # --- the cheap ones

    def test_only_post_writes(self) -> None:
        for method in ("PUT", "PATCH", "DELETE"):
            for path in ("/api/v1/changes", "/api/v1/catalog/refresh"):
                reply = self.call(method, path, {})
                self.assertEqual((reply.status, self.error(reply)["code"]), (404, "not-found"))

    def test_a_bad_content_length_is_400_not_a_traceback(self) -> None:
        for length in (b"abc", b"-5", b"9" * 5000, b"99999999999999999999", b"\xc2\xb2"):
            reply = self.raw(
                b"POST /api/v1/changes HTTP/1.1\r\nHost: x\r\nAuthorization: Bearer "
                + TOKEN.encode()
                + b"\r\nContent-Type: application/json\r\nContent-Length: "
                + length
                + b"\r\n\r\n{}"
            )
            self.assertTrue(reply.startswith(b"HTTP/1.0 400"), (length[:20], reply[:40]))
        self.assertFalse([line for line in self.logs if "internal" in line])

    def oversized_post(self, announced: int, sent: int, pause: float = 0.3) -> bytes:
        """POST `announced` bytes of JSON but send `sent` of them, wait until the server has had
        time to answer, send the rest, and read to the end. A reset while reading raises, which
        is what a browser shows as a network error."""
        head = (
            b"POST /api/v1/changes HTTP/1.1\r\nHost: x\r\nAuthorization: Bearer "
            + TOKEN.encode()
            + b"\r\nContent-Type: application/json\r\nContent-Length: "
            + str(announced).encode()
            + b"\r\n\r\n"
        )
        body = b'{"pad": "' + b"x" * (announced - 11) + b'"}'  # exactly `announced` bytes
        sock = socket.create_connection(("127.0.0.1", self.port), timeout=5)
        try:
            sock.sendall(head + body[:sent])
            time.sleep(pause)
            with contextlib.suppress(BrokenPipeError, ConnectionResetError):
                sock.sendall(body[sent:])  # the server may have closed: the reply is what matters
            chunks = []
            while True:
                part = sock.recv(65536)
                if not part:
                    return b"".join(chunks)
                chunks.append(part)
        finally:
            sock.close()

    def test_a_body_over_the_limit_that_arrives_late_still_gets_its_400(self) -> None:
        # (announced, sent before the pause, pause): the whole body at once, a part of it and
        # the rest after the server answered, and a body that is a good deal over the limit.
        cases = [
            (70_000, 70_000, 0.0),
            (70_000, 10_000, 0.2),
            (900_000, 5_000, 0.2),
            (900_000, 900_000, 0.0),
        ]
        for announced, sent, pause in cases * 5:
            reply = self.oversized_post(announced, sent, pause)
            case = (announced, sent, pause)
            self.assertTrue(reply.startswith(b"HTTP/1.0 400"), (case, reply[:60]))
            self.assertIn(b'"code":"bad-request"', reply, case)
            self.assertIn(b"the body is over 65536 bytes", reply, case)

    def test_a_body_over_the_drain_cap_is_not_read_and_the_server_goes_on(self) -> None:
        # Only 1000 bytes of the announced body are ever sent, and the handler would wait 15
        # seconds for the rest: an answer within two seconds shows that it did not try to read it.
        for announced in (serve.MAX_DRAIN + 1, 999_999_999, 10**12):
            sock = socket.create_connection(("127.0.0.1", self.port), timeout=2)
            try:
                sock.sendall(
                    b"POST /api/v1/changes HTTP/1.1\r\nHost: x\r\nAuthorization: Bearer "
                    + TOKEN.encode()
                    + b"\r\nContent-Type: application/json\r\nContent-Length: "
                    + str(announced).encode()
                    + b"\r\n\r\n"
                    + b"x" * 1000
                )
                reply = b""
                while chunk := sock.recv(65536):  # to the end: the connection is closed
                    reply += chunk
            finally:
                sock.close()
            self.assertTrue(reply.startswith(b"HTTP/1.0 400"), (announced, reply[:60]))
            self.assertIn(b"the body is over 65536 bytes", reply)
        self.assertFalse([line for line in self.logs if "body not received" in line])
        self.assertEqual(self.get("/api/v1/devices").status, 200)

    def test_a_body_that_stalls_while_it_is_discarded_is_dropped_and_logged(self) -> None:
        with mock.patch.object(serve._Handler, "timeout", 0.5):
            sock = socket.create_connection(("127.0.0.1", self.port), timeout=5)
            try:
                sock.sendall(
                    b"POST /api/v1/changes HTTP/1.1\r\nHost: x\r\nAuthorization: Bearer "
                    + TOKEN.encode()
                    + b"\r\nContent-Type: application/json\r\nContent-Length: 70000"
                    + b"\r\n\r\n"
                    + b"x" * 1000
                )
                self.assertEqual(sock.recv(65536), b"")  # closed without an answer
            finally:
                sock.close()
        self.assertTrue(any("body not received" in line for line in self.logs), self.logs)
        self.assertFalse([line for line in self.logs if "internal" in line])
        self.assertEqual(self.get("/api/v1/devices").status, 200)

    def drip(self, announced: int, give_up: float = 5.0) -> tuple[bool, float]:
        """POST a body of `announced` bytes announced but sent one byte at a time, 0.1 s apart,
        for at most `give_up` seconds. Returns whether the server closed the connection and how
        long the client waited. The socket stays quiet for far less than the per-read timeout."""
        head = (
            b"POST /api/v1/changes HTTP/1.1\r\nHost: x\r\nAuthorization: Bearer "
            + TOKEN.encode()
            + b"\r\nContent-Type: application/json\r\nContent-Length: "
            + str(announced).encode()
            + b"\r\n\r\n"
        )
        started = time.monotonic()
        sock = socket.create_connection(("127.0.0.1", self.port), timeout=5)
        try:
            sock.sendall(head)
            while time.monotonic() - started < give_up:
                try:
                    sock.sendall(b"x")
                    if select.select([sock], [], [], 0.1)[0] and sock.recv(65536) == b"":
                        return True, time.monotonic() - started
                except (BrokenPipeError, ConnectionResetError):
                    return True, time.monotonic() - started
            return False, time.monotonic() - started
        finally:
            sock.close()

    def test_a_body_dripped_past_the_total_limit_is_cut_off(self) -> None:
        # The limit is 0.6 s, the per-read timeout 15 s: only a total limit ends this.
        self.start(body_seconds=0.6)
        for announced in (60_000, 70_000, serve.MAX_DRAIN):
            closed, waited = self.drip(announced)
            self.assertTrue(closed, (announced, waited))
            self.assertLess(waited, 4.0, announced)
        self.assertTrue(any("body not received" in line for line in self.logs), self.logs)
        self.assertFalse([line for line in self.logs if "internal" in line])
        self.assertEqual(self.get("/api/v1/devices").status, 200)

    def test_a_body_that_fits_the_total_limit_is_read_as_before(self) -> None:
        self.start(body_seconds=0.6)
        self.assertEqual(self.get("/api/v1/devices").status, 200)
        reply = self.call("POST", "/api/v1/changes", {"changes": [], "expect": {}})
        self.assertEqual(reply.status, 400)  # an empty change is refused, after the body was read
        self.assertNotIn("body not received", "".join(self.logs))
        reply = self.oversized_post(70_000, 10_000, 0.2)
        self.assertTrue(reply.startswith(b"HTTP/1.0 400"), reply[:60])
        self.assertIn(b"the body is over 65536 bytes", reply)
        self.assertNotIn("body not received", "".join(self.logs))


class TokenTests(unittest.TestCase):
    def test_the_environment_wins_then_the_first_line_of_the_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "ui.token"
            path.write_text("from-file-0123456789\nsecond line\n")
            settings = ServeSettings(token_file=str(path))
            self.assertEqual(
                serve.load_token(
                    {"WARPZONE_UI_TOKEN": " from-env-0123456789 "},  # gitleaks:allow
                    settings,
                ),
                "from-env-0123456789",
            )
            self.assertEqual(serve.load_token({}, settings), "from-file-0123456789")
            path.write_text("\n")
            with self.assertRaises(serve.ServeError):
                serve.load_token({}, settings)
            with self.assertRaises(serve.ServeError):
                serve.load_token({}, ServeSettings(token_file=str(Path(tmp) / "missing")))
        with self.assertRaises(serve.ServeError):
            serve.load_token({}, ServeSettings())
        with self.assertRaises(serve.ServeError):
            serve.load_token({"WARPZONE_UI_TOKEN": "  "}, ServeSettings())

    def test_a_short_token_is_refused_whichever_way_it_comes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "ui.token"
            path.write_text("0123456789abcde\n")  # 15 characters
            for env, settings in (
                ({"WARPZONE_UI_TOKEN": "0123456789abcde"}, ServeSettings()),  # gitleaks:allow
                ({}, ServeSettings(token_file=str(path))),
                ({"WARPZONE_UI_TOKEN": "x"}, ServeSettings()),
            ):
                with self.assertRaises(serve.ServeError) as caught:
                    serve.load_token(env, settings)
                self.assertIn("at least 16", str(caught.exception))
                self.assertNotIn("0123456789abcde", str(caught.exception))
            path.write_text("0123456789abcdef\n")  # 16 characters
            settings = ServeSettings(token_file=str(path))
            self.assertEqual(serve.load_token({}, settings), "0123456789abcdef")


class BodyBudgetTests(unittest.TestCase):
    def test_a_chunk_and_the_time_left_while_there_is_time(self) -> None:
        self.assertEqual(serve.body_budget(10, 100.0, 100.0, 60.0, 15.0), (10, 15.0))
        self.assertEqual(serve.body_budget(10**6, 100.0, 130.0, 60.0, 15.0), (serve.CHUNK, 15.0))

    def test_the_read_timeout_shrinks_to_the_time_left(self) -> None:
        count, timeout = serve.body_budget(10, 100.0, 155.0, 60.0, 15.0) or (0, 0.0)
        self.assertEqual(count, 10)
        self.assertAlmostEqual(timeout, 5.0)

    def test_no_budget_once_the_limit_has_run_out(self) -> None:
        self.assertIsNone(serve.body_budget(10, 100.0, 160.0, 60.0, 15.0))
        self.assertIsNone(serve.body_budget(10, 100.0, 999.0, 60.0, 15.0))


class FailureLimiterTests(unittest.TestCase):
    def test_failures_are_spaced_out_for_everyone_not_per_caller(self) -> None:
        sleeps: list[float] = []
        limiter = serve.FailureLimiter(1.0, clock=lambda: 100.0, sleep=sleeps.append)
        for _ in range(3):  # three failures at the same instant, from three threads
            limiter.attempt(lambda: False)
        self.assertEqual(sleeps, [1.0, 2.0, 3.0])

    def test_the_backlog_drains_with_time(self) -> None:
        now = [0.0]
        sleeps: list[float] = []
        limiter = serve.FailureLimiter(1.0, clock=lambda: now[0], sleep=sleeps.append)
        limiter.attempt(lambda: False)
        now[0] = 10.0
        limiter.attempt(lambda: False)
        self.assertEqual(sleeps, [1.0, 1.0])

    def test_no_interval_means_no_waiting(self) -> None:
        sleeps: list[float] = []
        serve.FailureLimiter(0, sleep=sleeps.append).attempt(lambda: False)
        self.assertEqual(sleeps, [])


class ForwardedHttpsTests(unittest.TestCase):
    def test_only_a_first_value_of_https_counts(self) -> None:
        for value, expected in (
            ("https", True),
            ("HTTPS", True),
            ("Https", True),
            ("  https  ", True),
            ("https,http", True),
            ("https , http", True),
            ("http", False),
            ("http, https", False),
            ("", False),
            (None, False),
            ("httpss", False),
            ("wss", False),
            (",https", False),
        ):
            self.assertIs(serve.forwarded_https(value), expected, value)

    def test_the_cookie_line_carries_secure_when_asked(self) -> None:
        plain = serve.session_cookie_line("v", secure=False)
        self.assertEqual(
            plain,
            f"{serve.COOKIE}=v; HttpOnly; SameSite=Strict; Path=/; Max-Age={serve.COOKIE_MAX_AGE}",
        )
        self.assertEqual(serve.session_cookie_line("v", secure=True), plain + "; Secure")


class WaitingQueueTests(unittest.TestCase):
    """The limiter keeps at most `max_waiting` failures asleep; the rest are refused."""

    def setUp(self) -> None:
        self.release = threading.Event()
        self.entered = threading.Semaphore(0)
        self.sleeps: list[float] = []
        self.addCleanup(self.release.set)

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.entered.release()
        self.release.wait(5)

    def fill(self, limiter: serve.FailureLimiter, count: int) -> list[threading.Thread]:
        threads = [
            threading.Thread(target=limiter.attempt, args=(lambda: False,), daemon=True)
            for _ in range(count)
        ]
        for thread in threads:
            thread.start()
        for _ in threads:
            self.assertTrue(self.entered.acquire(timeout=5))
        return threads

    def test_a_full_queue_refuses_at_once_and_drains(self) -> None:
        limiter = serve.FailureLimiter(1.0, max_waiting=3, clock=lambda: 0.0, sleep=self.sleep)
        self.assertFalse(limiter.full())
        threads = self.fill(limiter, 3)
        self.assertTrue(limiter.full())
        self.assertEqual(self.sleeps, [1.0, 2.0, 3.0])
        with self.assertRaises(serve.QueueFull):
            limiter.attempt(lambda: False)
        self.assertEqual(len(self.sleeps), 3)  # the refused one did not sleep
        self.release.set()
        for thread in threads:
            thread.join(timeout=5)
        self.assertFalse(limiter.full())
        limiter.attempt(lambda: False)  # accepted again
        self.assertEqual(len(self.sleeps), 4)

    def test_a_full_queue_does_not_call_the_comparison_right_or_wrong(self) -> None:
        limiter = serve.FailureLimiter(1.0, max_waiting=1, clock=lambda: 0.0, sleep=self.sleep)
        self.fill(limiter, 1)
        calls: list[bool] = []
        for outcome in (False, True):
            with self.assertRaises(serve.QueueFull):
                limiter.attempt(lambda outcome=outcome: calls.append(outcome) or outcome)
        self.assertEqual(calls, [])

    def test_a_right_answer_is_returned_at_once_and_takes_no_place(self) -> None:
        limiter = serve.FailureLimiter(1.0, max_waiting=1, clock=lambda: 0.0, sleep=self.sleep)
        self.assertTrue(limiter.attempt(lambda: True))
        self.assertEqual(self.sleeps, [])
        self.assertFalse(limiter.full())

    def test_a_burst_calls_the_comparison_at_most_max_waiting_times(self) -> None:
        attempts, places = 32, 4
        limiter = serve.FailureLimiter(1.0, max_waiting=places, clock=lambda: 0.0, sleep=self.sleep)
        barrier = threading.Barrier(attempts)
        calls: list[int] = []
        refused: list[int] = []

        def right() -> bool:
            calls.append(1)
            time.sleep(0.01)
            return False

        def one() -> None:
            barrier.wait(timeout=5)
            try:
                limiter.attempt(right)
            except serve.QueueFull:
                refused.append(1)

        threads = [threading.Thread(target=one, daemon=True) for _ in range(attempts)]
        for thread in threads:
            thread.start()
        deadline = time.monotonic() + 5
        while len(refused) < attempts - places and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertEqual((len(refused), len(calls)), (attempts - places, places))
        self.release.set()
        for thread in threads:
            thread.join(timeout=5)
        self.assertEqual(len(calls), places)

    def test_the_wait_ahead_of_a_refused_attempt_is_not_extended(self) -> None:
        limiter = serve.FailureLimiter(1.0, max_waiting=1, clock=lambda: 0.0, sleep=self.sleep)
        threads = self.fill(limiter, 1)
        for _ in range(5):
            with self.assertRaises(serve.QueueFull):
                limiter.attempt(lambda: False)
        self.release.set()
        threads[0].join(timeout=5)
        # a refusal does not push the next failure further out
        limiter.attempt(lambda: False)
        self.assertEqual(self.sleeps, [1.0, 2.0])

    def test_retry_after_is_whole_seconds_and_at_least_one(self) -> None:
        self.assertEqual(serve.FailureLimiter(1.0).retry_after, 1)
        self.assertEqual(serve.FailureLimiter(0.2).retry_after, 1)
        self.assertEqual(serve.FailureLimiter(2.5).retry_after, 3)

    def test_without_an_interval_nothing_waits_and_nothing_is_full(self) -> None:
        limiter = serve.FailureLimiter(0, max_waiting=1, sleep=self.sleep)
        for _ in range(3):
            limiter.attempt(lambda: False)
        self.assertFalse(limiter.full())
        self.assertEqual(self.sleeps, [])


class RunTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name) / "hub"
        self.root.mkdir()
        data = {
            "hub": {
                "name": "nas",
                "device_id": HUB_ID,
                "root": str(self.root),
                "api_url": "http://127.0.0.1:1",
                "api_key": API_KEY,
            },
            "devices": {
                "deck": {"device_id": DECK_ID, "profile": "steamos-emudeck", "folders": ["library"]}
            },
        }
        self.cfg = parse_config(data)
        old = signal.getsignal(signal.SIGTERM)
        self.addCleanup(signal.signal, signal.SIGTERM, old)

    def test_it_refuses_to_start_without_a_token(self) -> None:
        (self.root / "library" / ".stfolder").mkdir(parents=True)
        with self.assertRaises(serve.ServeError) as caught:
            serve.run(self.cfg, env={}, out=lambda t: None)
        self.assertEqual(caught.exception.exit_code, 2)

    def test_it_refuses_to_start_when_the_hub_is_not_mounted(self) -> None:
        with self.assertRaises(serve.ServeError) as caught:
            serve.run(self.cfg, env={"WARPZONE_UI_TOKEN": TOKEN}, out=lambda t: None)
        self.assertEqual(caught.exception.exit_code, 2)
        self.assertNotIn(TOKEN, str(caught.exception))

    def test_it_serves_until_stopped_and_prints_no_token(self) -> None:
        (self.root / "library" / ".stfolder").mkdir(parents=True)
        lines: list[str] = []
        answers: list[int] = []

        def ready(server: serve.UiServer) -> None:
            port = server.server_address[1]

            def probe() -> None:
                conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
                conn.request("GET", "/healthz")
                answers.append(conn.getresponse().status)
                conn.close()
                server.shutdown()

            threading.Thread(target=probe, daemon=True).start()

        code = serve.run(
            self.cfg,
            listen="127.0.0.1:0",
            env={"WARPZONE_UI_TOKEN": TOKEN},
            out=lines.append,
            log=lines.append,
            ready=ready,
        )
        self.assertEqual((code, answers), (0, [200]))
        # The background catalog run may log before the listening line; look for it anywhere.
        self.assertTrue(
            any(line.startswith("warpzone serve: listening on 127.0.0.1:") for line in lines), lines
        )
        self.assertNotIn(TOKEN, "\n".join(lines))

    def test_a_bad_address_is_a_usage_error(self) -> None:
        (self.root / "library" / ".stfolder").mkdir(parents=True)
        with self.assertRaises(serve.ServeError):
            serve.run(self.cfg, listen="nonsense", env={"WARPZONE_UI_TOKEN": TOKEN})


class CliTests(unittest.TestCase):
    def test_serve_without_a_token_exits_2(self) -> None:
        from test_cli import write_config

        with tempfile.TemporaryDirectory() as tmp:
            path = write_config(tmp, "http://127.0.0.1:1")
            env = {k: v for k, v in os.environ.items() if k != "WARPZONE_UI_TOKEN"}
            with mock.patch.dict(os.environ, env, clear=True):
                code, _, err = run("serve", "--config", path)
        self.assertEqual(code, 2)
        self.assertIn("no UI token", err)


if __name__ == "__main__":
    unittest.main()
