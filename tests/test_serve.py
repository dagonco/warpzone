"""`warpzone serve` over a real ThreadingHTTPServer on 127.0.0.1, a temporary hub tree and the
fake Syncthing. Every request goes through a socket."""

import http.client
import json
import os
import signal
import socket
import tempfile
import threading
import unittest
from pathlib import Path
from typing import Any, NamedTuple
from unittest import mock

from library_support import GIB, config, games, managed, write_catalog
from support import API_KEY, DECK_ID, HUB_ID, MACHINE_ID, FakeSyncthing
from test_cli import run
from warpzone import __version__, catalog, library_api, serve
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


class AuthTests(ServeCase):
    def test_no_token_and_a_wrong_token_are_401(self) -> None:
        for headers in (
            {},
            {"Authorization": "Bearer wrong"},
            {"Authorization": "Basic " + TOKEN},
            {"Authorization": "Bearer"},
            {"Cookie": "wz_session=nope"},
            {"Cookie": "other=" + serve.session_cookie(TOKEN)},
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
        value = serve.session_cookie(TOKEN)
        self.assertTrue(cookie.startswith(f"wz_session={value};"), cookie)
        for attribute in ("HttpOnly", "SameSite=Strict", "Path=/"):
            self.assertIn(attribute, cookie)
        self.assertNotIn(TOKEN, cookie)
        reply = self.get("/api/v1/devices", headers={"Cookie": f"wz_session={value}"}, auth=False)
        self.assertEqual(reply.status, 200)

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
        value = serve.session_cookie(TOKEN)
        self.assertEqual(serve.session_cookie(TOKEN), value)
        self.assertNotEqual(serve.session_cookie(TOKEN + "x"), value)
        self.start()  # a new server with the same token
        reply = self.get("/api/v1/devices", headers={"Cookie": f"wz_session={value}"}, auth=False)
        self.assertEqual(reply.status, 200)

    def test_a_write_by_cookie_needs_the_origin_of_the_page(self) -> None:
        cookie = {"Cookie": f"wz_session={serve.session_cookie(TOKEN)}"}
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
        reply = self.get("/api/v1/search")
        self.assertEqual((reply.status, self.error(reply)["code"]), (400, "bad-request"))

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


class MediaTests(ServeCase):
    def test_the_cover_is_served(self) -> None:
        reply = self.get(f"/api/v1/games/{EXAMPLE.id}/media/covers")
        self.assertEqual((reply.status, reply.body), (200, PNG))
        self.assertEqual(reply.headers["Content-Type"], "image/png")
        self.assertEqual(reply.headers["X-Content-Type-Options"], "nosniff")

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
        self.assertFalse(
            (self.root / "catalog" / "whitelists" / "machine").exists()
        )  # nothing to back up

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


def library_game(path: str, title: str) -> catalog.Game:
    from library_support import game

    return game("psx", path, title)


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

    def cookie_get(self, cookie: str) -> int:
        return self.get("/api/v1/devices", headers={"Cookie": cookie}, auth=False).status

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
        with mock.patch.object(serve._Handler, "timeout", 0.3):
            before = threading.active_count()
            idle = [socket.create_connection(("127.0.0.1", self.port)) for _ in range(6)]
            stalled = socket.create_connection(("127.0.0.1", self.port))
            stalled.sendall(
                b"POST /api/v1/changes HTTP/1.1\r\nHost: x\r\nContent-Length: 10\r\n\r\nab"
            )
            threading.Event().wait(0.2)
            self.assertGreaterEqual(threading.active_count(), before + 7)
            for _ in range(60):
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
        value = serve.session_cookie(TOKEN)
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
        value = serve.session_cookie(TOKEN)
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


class FailureLimiterTests(unittest.TestCase):
    def test_failures_are_spaced_out_for_everyone_not_per_caller(self) -> None:
        sleeps: list[float] = []
        limiter = serve.FailureLimiter(1.0, clock=lambda: 100.0, sleep=sleeps.append)
        for _ in range(3):  # three failures at the same instant, from three threads
            limiter.failed()
        self.assertEqual(sleeps, [1.0, 2.0, 3.0])

    def test_the_backlog_drains_with_time(self) -> None:
        now = [0.0]
        sleeps: list[float] = []
        limiter = serve.FailureLimiter(1.0, clock=lambda: now[0], sleep=sleeps.append)
        limiter.failed()
        now[0] = 10.0
        limiter.failed()
        self.assertEqual(sleeps, [1.0, 1.0])

    def test_no_interval_means_no_waiting(self) -> None:
        sleeps: list[float] = []
        serve.FailureLimiter(0, sleep=sleeps.append).failed()
        self.assertEqual(sleeps, [])


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
