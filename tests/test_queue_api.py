"""`GET` and `POST /api/v1/queue` over a real server, a temporary hub tree and the fake
Syncthing: auth, origin, the conflict checks, dry runs, the order of the writes and the failure
of each write step."""

import hashlib
import json
import os
import unittest
import unittest.mock
from dataclasses import replace
from pathlib import Path
from typing import Any

import test_serve
from library_support import GIB, managed
from test_serve import EXAMPLE, OTHER, PS4, TINY, ServeCase, by_title, fresh_cookie
from warpzone import queue as q
from warpzone import queue_store, serve
from warpzone import whitelist as wl
from warpzone import whitelist_store as store

MGS = by_title("psx", "Metal Gear Solid (USA)")
MGS_GBC = by_title("gbc", "Metal Gear Solid (USA)")
STAMP = "2026-11-10T20:00:00Z"


def ref(game: Any, system: str = "psx") -> q.GameRef:
    return q.ref_of(system, game)


class QueueCase(ServeCase):
    def queue_file(self) -> Path:
        return self.root / "catalog" / "queue.json"

    def read_queue(self) -> q.Queue:
        return q.parse(self.queue_file().read_bytes()).queue

    def queue_etag(self) -> str:
        return str(self.get("/api/v1/queue").json["etag"])

    def post(self, ops: list[dict[str, Any]], **extra: Any) -> test_serve.Reply:
        names = {
            n
            for op in ops
            for n in (*op.get("devices", []), *op.get("add", []), *op.get("release", []))
        } | {op["device"] for op in ops if "device" in op}
        etags = self.etags()
        body = {
            "ops": ops,
            "expect_queue": self.queue_etag(),
            "expect": {n: etags[n] for n in names},
            **extra,
        }
        return self.call("POST", "/api/v1/queue", body)

    def add(self, game: Any, devices: list[str], **kw: Any) -> dict[str, Any]:
        return {"op": "add", "game": game.id, "devices": devices, **kw}

    def finish(self, game: Any, state: str = "finished", **kw: Any) -> dict[str, Any]:
        return {"op": "state", "game": game.id, "state": state, "release": [], "keep": [], **kw}

    def whitelist(self, device: str) -> wl.Model:
        return wl.parse((self.library / f"patterns-{device}.stignore").read_text())

    def put_queue(self, queue: q.Queue) -> None:
        path = self.queue_file()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(q.render(queue))

    def entry(self, of: Any, **kw: Any) -> q.Entry:
        fields: dict[str, Any] = {
            "owner": "default",
            "game": ref(of),
            "state": "queued",
            "added": STAMP,
            "changed": STAMP,
            "ended": None,
            "ra_dismissed": None,
            "devices": {},
        }
        return q.Entry(**{**fields, **kw})

    def entries_by_id(self) -> dict[str, q.Entry]:
        return {e.game.id: e for e in self.read_queue().entries}

    def backups(self) -> list[Path]:
        return sorted((self.root / "catalog" / "queue-backups").glob("*.json"))


class AccessTests(QueueCase):
    def test_a_token_is_needed(self) -> None:
        self.assertEqual(self.get("/api/v1/queue", auth=False).status, 401)
        reply = self.call("POST", "/api/v1/queue", {"ops": []}, auth=False)
        self.assertEqual(reply.status, 401)

    def test_a_cookie_write_needs_the_pages_origin(self) -> None:
        cookie = {"Cookie": f"wz_session={fresh_cookie()}"}
        body = {"ops": [self.add(OTHER, [])], "expect_queue": "absent"}
        reply = self.call("POST", "/api/v1/queue", body, headers=cookie, auth=False)
        self.assertEqual((reply.status, self.error(reply)["code"]), (403, "bad-origin"))
        evil = {**cookie, "Origin": "http://evil.example"}
        self.assertEqual(
            self.call("POST", "/api/v1/queue", body, headers=evil, auth=False).status, 403
        )
        good = {**cookie, "Origin": f"http://127.0.0.1:{self.port}"}
        self.assertEqual(
            self.call("POST", "/api/v1/queue", body, headers=good, auth=False).status, 200
        )
        # a read needs no origin
        self.assertEqual(self.get("/api/v1/queue", headers=cookie, auth=False).status, 200)

    def test_a_write_is_json_and_small(self) -> None:
        reply = self.call(
            "POST", "/api/v1/queue", raw=b"{}", headers={"Content-Type": "text/plain"}
        )
        self.assertEqual((reply.status, self.error(reply)["code"]), (400, "bad-request"))
        big = {"ops": [self.add(OTHER, [])], "pad": "x" * 70_000}
        self.assertEqual(self.call("POST", "/api/v1/queue", big).status, 400)
        self.assertEqual(self.call("POST", "/api/v1/queue", {"ops": "no"}).status, 400)
        self.assertEqual(self.call("PUT", "/api/v1/queue", {}).status, 404)
        self.assertEqual(self.call("DELETE", "/api/v1/queue").status, 404)


class ReadTests(QueueCase):
    def test_no_file_is_an_empty_queue(self) -> None:
        body = self.get("/api/v1/queue").json
        self.assertEqual(
            (body["state"], body["etag"], body["entries"], body["history"], body["size"]),
            ("absent", "absent", [], [], 0),
        )
        self.assertEqual(
            (body["history_total"], body["limit"], body["collections"]), (0, 4194304, [])
        )

    def test_the_history_parameter(self) -> None:
        ended = [
            self.entry(g, state="finished", ended=f"2026-11-0{n}T00:00:00Z")
            for n, g in enumerate((OTHER, MGS, EXAMPLE), 1)
        ]
        self.put_queue(q.Queue(STAMP, tuple(ended)))
        body = self.get("/api/v1/queue?history=2").json
        self.assertEqual([e["id"] for e in body["history"]], [EXAMPLE.id, MGS.id])
        self.assertEqual(body["history_total"], 3)
        self.assertEqual(len(self.get("/api/v1/queue").json["history"]), 3)
        self.assertEqual(self.get("/api/v1/queue?history=0").json["history"], [])
        for raw in ("-1", "x", "", "10001"):
            reply = self.get(f"/api/v1/queue?history={raw}")
            self.assertEqual((reply.status, self.error(reply)["code"]), (400, "bad-request"), raw)

    def test_the_etag_is_the_files_hash_and_a_hand_edit_is_picked_up(self) -> None:
        self.put_queue(q.Queue(STAMP, (self.entry(OTHER),)))
        digest = hashlib.sha256(self.queue_file().read_bytes()).hexdigest()
        self.assertEqual(self.get("/api/v1/queue").json["etag"], f"sha256:{digest}")
        self.put_queue(q.Queue(STAMP, ()))
        body = self.get("/api/v1/queue").json
        self.assertEqual(body["entries"], [])
        self.assertEqual(body["size"], self.queue_file().stat().st_size)

    def test_a_game_that_left_the_library_is_moved_with_candidates(self) -> None:
        gone = replace(ref(MGS), id="0" * 16)
        self.put_queue(q.Queue(STAMP, (self.entry(MGS, game=gone),)))
        (found,) = self.get("/api/v1/queue").json["entries"]
        self.assertEqual(found["found"], "moved")
        self.assertEqual([c["id"] for c in found["candidates"]], [MGS.id, MGS_GBC.id])

    def test_a_damaged_file_is_reported_and_never_overwritten(self) -> None:
        path = self.queue_file()
        path.parent.mkdir(parents=True, exist_ok=True)
        for content in (b"not json", json.dumps({"format": 1}).encode()):
            path.write_bytes(content)
            body = self.get("/api/v1/queue").json
            self.assertEqual((body["state"], body["entries"], body["history"]), ("damaged", [], []))
            self.assertIn("queue", body["message"])
            self.assertNotIn(str(self.root), body["message"])
            reply = self.call(
                "POST",
                "/api/v1/queue",
                {"ops": [self.add(OTHER, [])], "expect_queue": body["etag"]},
            )
            self.assertEqual((reply.status, self.error(reply)["code"]), (409, "queue-damaged"))
            self.assertTrue(self.error(reply)["queue"])
            self.assertEqual(path.read_bytes(), content)
        self.assertEqual(self.backups(), [])

    def test_a_newer_format_is_read_only(self) -> None:
        path = self.queue_file()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"format": 2}))
        body = self.get("/api/v1/queue").json
        self.assertEqual((body["state"], body["entries"]), ("newer", []))
        reply = self.post([self.add(OTHER, [])])
        self.assertEqual((reply.status, self.error(reply)["code"]), (409, "queue-damaged"))
        self.assertEqual(json.loads(path.read_text()), {"format": 2})

    def test_a_file_over_16_mib_is_damaged_without_being_read(self) -> None:
        path = self.queue_file()
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "wb") as fh:
            fh.truncate(q.FILE_LIMIT + 1)
        body = self.get("/api/v1/queue").json
        self.assertEqual((body["state"], body["size"]), ("damaged", q.FILE_LIMIT + 1))
        self.assertIn("16 MiB", body["message"])

    @unittest.skipIf(os.geteuid() == 0, "root reads everything")
    def test_an_unreadable_file_is_damaged_and_the_answer_names_no_path(self) -> None:
        self.put_queue(q.Queue(STAMP, ()))
        os.chmod(self.queue_file(), 0)
        self.addCleanup(os.chmod, self.queue_file(), 0o644)
        body = self.get("/api/v1/queue").json
        self.assertEqual(body["state"], "damaged")
        self.assertNotIn(str(self.root), json.dumps(body))


class DryRunTests(QueueCase):
    def test_a_dry_run_says_what_would_happen_and_writes_nothing(self) -> None:
        deck = (self.library / "patterns-deck.stignore").read_bytes()
        reply = self.post([self.add(OTHER, ["deck"])], dry_run=True)
        self.assertEqual(reply.status, 200, reply.body)
        found = reply.json
        self.assertTrue(found["dry_run"])
        (item,) = found["devices"]
        self.assertEqual(
            (item["device"], item["written"], item["on"], item["bytes_on"]),
            ("deck", False, [OTHER.id], GIB),
        )
        (queued,) = found["queue"]["entries"]
        self.assertEqual(
            queued["devices"]["deck"], {"before": "off", "end": None, "game": "on", "view": "on"}
        )
        self.assertFalse(self.queue_file().exists())
        self.assertEqual((self.library / "patterns-deck.stignore").read_bytes(), deck)
        self.assertEqual(self.fake.writes(), [])
        self.assertEqual(self.get("/api/v1/queue").json["entries"], [])

    def test_a_dry_run_still_checks_everything(self) -> None:
        reply = self.call(
            "POST",
            "/api/v1/queue",
            {"ops": [self.add(OTHER, ["deck"])], "expect_queue": "absent", "dry_run": True},
        )
        self.assertEqual((reply.status, self.error(reply)["code"]), (428, "expect-missing"))


class WriteTests(QueueCase):
    def test_queueing_writes_the_queue_then_the_whitelist(self) -> None:
        old = (self.library / "patterns-deck.stignore").read_bytes()
        reply = self.post([self.add(OTHER, ["deck", "machine"]), self.add(MGS, [])])
        self.assertEqual(reply.status, 200, reply.body)
        found = reply.json
        self.assertFalse(found["dry_run"])
        self.assertEqual([d["device"] for d in found["devices"]], ["deck", "machine"])
        self.assertTrue(all(d["written"] for d in found["devices"]))
        self.assertEqual(found["skipped"], [])
        # the file is the rendering of the model, its etag the answer's
        data = self.queue_file().read_bytes()
        self.assertEqual(q.parse(data).state, "ok")
        self.assertEqual(found["queue"]["etag"], f"sha256:{hashlib.sha256(data).hexdigest()}")
        self.assertEqual(found["queue"]["size"], len(data))
        self.assertEqual([e["id"] for e in found["queue"]["entries"]], [OTHER.id, MGS.id])
        # the whitelists, their backups and the scans
        self.assertEqual(
            self.whitelist("deck").includes, frozenset({EXAMPLE.primary, OTHER.primary})
        )
        self.assertEqual(self.whitelist("machine").includes, frozenset({OTHER.primary}))
        (backup,) = (self.root / "catalog" / "whitelists" / "deck").glob("*.stignore")
        self.assertEqual(backup.read_bytes(), old)
        for device in ("deck", "machine"):
            self.assertIn(
                ("POST", f"/rest/db/scan?folder=wz-library&sub=patterns-{device}.stignore", None),
                self.fake.requests,
            )
        # nothing is left beside the files, and a new queue has no backup yet
        self.assertEqual(list(self.queue_file().parent.glob(".*wz-tmp")), [])
        self.assertEqual(self.backups(), [])
        self.assertEqual(self.get("/api/v1/queue").json["etag"], found["queue"]["etag"])

    def test_the_replaced_file_is_backed_up_and_the_backups_are_rotated(self) -> None:
        self.assertEqual(self.post([self.add(OTHER, [])]).status, 200)
        first = self.queue_file().read_bytes()
        self.assertEqual(self.post([self.add(MGS, [])]).status, 200)
        (backup,) = self.backups()
        self.assertEqual(backup.read_bytes(), first)
        self.assertTrue(backup.name.endswith("Z.json"))
        directory = backup.parent
        for n in range(60):
            (directory / f"2000010{n % 10}T0000{n // 10:02d}.000000Z.json").write_bytes(b"{}")
        self.assertEqual(self.post([self.add(TINY, [])]).status, 200)
        names = [p.name for p in self.backups()]
        self.assertEqual(len(names), 50)
        self.assertEqual(names, sorted(names))

    def test_the_game_page_and_the_list_see_the_whitelist_change(self) -> None:
        self.post([self.add(OTHER, ["deck"])])
        found = self.get(f"/api/v1/games/{OTHER.id}").json
        self.assertEqual(found["devices"]["deck"], "on")

    def test_finishing_releases_what_the_queue_turned_on(self) -> None:
        self.post([self.add(OTHER, ["deck", "machine"]), self.add(EXAMPLE, ["deck"])])
        reply = self.post([self.finish(OTHER, release=["deck"], keep=["machine"])])
        self.assertEqual(reply.status, 200, reply.body)
        self.assertEqual(self.whitelist("deck").includes, frozenset({EXAMPLE.primary}))
        self.assertEqual(self.whitelist("machine").includes, frozenset({OTHER.primary}))
        body = reply.json["queue"]
        self.assertEqual([e["id"] for e in body["entries"]], [EXAMPLE.id])
        (done,) = body["history"]
        self.assertEqual(
            {n: (c["end"], c["view"]) for n, c in done["devices"].items()},
            {"deck": ("released", "released"), "machine": ("kept", "kept")},
        )
        # what the user had before queueing is never turned off: EXAMPLE was on the deck
        self.assertEqual(self.entries_by_id()[EXAMPLE.id].devices["deck"].before, "on")
        refused = self.post([self.finish(EXAMPLE, release=["deck"])])
        self.assertEqual((refused.status, self.error(refused)["code"]), (409, "not-held"))
        self.assertIn(EXAMPLE.primary, self.whitelist("deck").includes)

    def test_a_system_turned_on_afterwards_covers_the_game_and_it_stays(self) -> None:
        self.post([self.add(OTHER, ["deck"])])
        reply = self.change([{"device": "deck", "system": "psx", "on": True}])
        self.assertEqual(reply.status, 200, reply.body)
        answer = self.post([self.finish(OTHER, "dropped", release=["deck"])])
        self.assertEqual(answer.status, 200, answer.body)
        self.assertEqual(
            answer.json["skipped"],
            [{"device": "deck", "system": "psx", "game": OTHER.id, "reason": "covered"}],
        )
        self.assertIn("psx", self.whitelist("deck").systems)
        self.assertEqual(self.entries_by_id()[OTHER.id].devices["deck"].end, "kept")

    def test_removing_takes_two_queue_writes_around_the_whitelists(self) -> None:
        self.post([self.add(OTHER, ["deck"]), self.add(MGS, [])])
        before = self.backups()
        reply = self.post([{"op": "remove", "game": OTHER.id, "release": ["deck"], "keep": []}])
        self.assertEqual(reply.status, 200, reply.body)
        self.assertEqual([e["id"] for e in reply.json["queue"]["entries"]], [MGS.id])
        self.assertEqual([e.game.id for e in self.read_queue().entries], [MGS.id])
        self.assertNotIn(OTHER.primary, self.whitelist("deck").includes)
        # the file before the first write and the one between the two are both kept
        self.assertEqual(len(self.backups()) - len(before), 2)
        middle = q.parse(self.backups()[-1].read_bytes()).queue
        released = next(e for e in middle.entries if e.game.id == OTHER.id)
        self.assertEqual(released.devices["deck"].end, "released")

    def test_a_repair_turns_a_game_back_on(self) -> None:
        self.post([self.add(OTHER, ["deck"])])
        self.write_whitelist("deck", managed(["gba"], includes=[EXAMPLE.primary]))  # switched off
        shown = self.get("/api/v1/queue").json["entries"][0]["devices"]["deck"]
        self.assertEqual((shown["game"], shown["view"]), ("off", "not-on"))
        before = self.queue_file().read_bytes()
        reply = self.post([{"op": "sync", "game": OTHER.id, "device": "deck"}])
        self.assertEqual(reply.status, 200, reply.body)
        self.assertEqual(self.queue_file().read_bytes(), before)  # the queue did not change
        self.assertEqual(reply.json["queue"]["entries"][0]["devices"]["deck"]["view"], "on")
        again = self.post([{"op": "sync", "game": OTHER.id, "device": "deck"}])
        self.assertEqual((again.status, self.error(again)["code"]), (409, "in-sync"))

    def test_the_switches_neither_read_nor_write_the_queue(self) -> None:
        self.post([self.add(OTHER, ["deck"])])
        before = self.queue_file().read_bytes()
        reply = self.change([{"device": "deck", "system": "psx", "game": OTHER.id, "on": False}])
        self.assertEqual(reply.status, 200, reply.body)
        self.assertEqual(self.queue_file().read_bytes(), before)
        shown = self.get("/api/v1/queue").json["entries"][0]["devices"]["deck"]
        self.assertEqual(shown["view"], "not-on")

    def test_moving_and_relinking_and_dismissing(self) -> None:
        self.post([self.add(OTHER, []), self.add(MGS, [])])
        reply = self.post([{"op": "move", "game": MGS.id, "position": 0}])
        self.assertEqual([e["id"] for e in reply.json["queue"]["entries"]], [MGS.id, OTHER.id])
        gone = self.read_queue()
        first = gone.entries[0]
        moved = replace(ref(MGS), id="0" * 16)
        self.put_queue(q.Queue(gone.updated, (replace(first, game=moved), *gone.entries[1:])))
        relinked = self.post([{"op": "relink", "game": "0" * 16, "to": MGS_GBC.id}])
        self.assertEqual(relinked.status, 200, relinked.body)
        self.assertEqual(relinked.json["queue"]["entries"][0]["id"], MGS_GBC.id)
        nothing = self.post([{"op": "dismiss", "game": OTHER.id}])
        self.assertEqual((nothing.status, self.error(nothing)["code"]), (400, "bad-request"))

    def test_an_unsupported_system_or_an_unknown_game_writes_nothing(self) -> None:
        reply = self.post([self.add(OTHER, ["deck"]), self.add(PS4, ["machine"])])
        self.assertEqual((reply.status, self.error(reply)["code"]), (422, "unsupported"))
        self.assertEqual(self.error(reply)["device"], "machine")
        self.assertFalse(self.queue_file().exists())
        self.assertNotIn(OTHER.primary, self.whitelist("deck").includes)
        missing = self.call(
            "POST", "/api/v1/queue",
            {"ops": [{"op": "add", "game": "f" * 16, "devices": []}], "expect_queue": "absent"},
        )  # fmt: skip
        self.assertEqual((missing.status, self.error(missing)["code"]), (404, "not-found"))
        self.assertEqual(self.fake.writes(), [])


class ConflictTests(QueueCase):
    def test_a_stale_queue_is_412_and_nothing_is_written(self) -> None:
        self.post([self.add(OTHER, [])])
        etags = self.etags()
        stale = self.queue_etag()
        self.post([self.add(MGS, [])])
        body = {
            "ops": [self.add(EXAMPLE, ["deck"])],
            "expect_queue": stale,
            "expect": {"deck": etags["deck"]},
        }
        reply = self.call("POST", "/api/v1/queue", body)
        self.assertEqual((reply.status, self.error(reply)["code"]), (412, "stale"))
        self.assertTrue(self.error(reply)["queue"])
        self.assertEqual([e.game.id for e in self.read_queue().entries], [OTHER.id, MGS.id])

    def test_a_stale_whitelist_is_412_for_that_device(self) -> None:
        etags = self.etags()
        self.change([{"device": "deck", "system": "gbc", "on": True}])
        body = {
            "ops": [self.add(OTHER, ["deck"])],
            "expect_queue": "absent",
            "expect": {"deck": etags["deck"]},
        }
        reply = self.call("POST", "/api/v1/queue", body)
        self.assertEqual((reply.status, self.error(reply)["code"]), (412, "stale"))
        self.assertEqual(self.error(reply)["device"], "deck")
        self.assertFalse(self.queue_file().exists())

    def test_a_missing_expect_is_428(self) -> None:
        body = {"ops": [self.add(OTHER, ["deck"])], "expect_queue": "absent"}
        reply = self.call("POST", "/api/v1/queue", body)
        self.assertEqual((reply.status, self.error(reply)["code"]), (428, "expect-missing"))
        self.assertEqual(self.error(reply)["device"], "deck")
        reply = self.call("POST", "/api/v1/queue", {"ops": [self.add(OTHER, [])]})
        self.assertEqual((reply.status, self.error(reply)["code"]), (428, "expect-missing"))

    def test_the_file_changing_between_the_plan_and_the_write_is_stale(self) -> None:
        real = queue_store.load
        calls = []

        def racing(root: Path) -> queue_store.Loaded:
            calls.append(1)
            if len(calls) == 2:  # the re-check under the lock sees a hand edit
                self.put_queue(q.Queue(STAMP, (self.entry(MGS),)))
            return real(root)

        with unittest.mock.patch.object(queue_store, "load", racing):
            reply = self.post([self.add(OTHER, ["deck"])])
        self.assertEqual((reply.status, self.error(reply)["code"]), (412, "stale"))
        self.assertEqual([e.game.id for e in self.read_queue().entries], [MGS.id])
        self.assertNotIn(OTHER.primary, self.whitelist("deck").includes)


class OwnersTests(QueueCase):
    def test_another_owners_entries_are_kept_byte_for_byte(self) -> None:
        theirs = self.entry(
            MGS, owner="someone-else", devices={"deck": q.Claim("off", STAMP)}, ra_dismissed=STAMP
        )
        done = self.entry(OTHER, owner="someone-else", state="dropped", ended=STAMP)
        self.put_queue(q.Queue(STAMP, (theirs, done)))
        reply = self.post([self.add(EXAMPLE, [])])
        self.assertEqual(reply.status, 200, reply.body)
        body = reply.json["queue"]
        self.assertEqual([e["id"] for e in body["entries"]], [EXAMPLE.id])
        self.assertEqual(body["history_total"], 0)
        raw = json.loads(self.queue_file().read_text())["queue"]
        expect = json.loads(q.render(q.Queue(STAMP, (theirs, done))))["queue"]
        self.assertEqual((raw[0], raw[1]), (expect[0], expect[1]))
        denied = self.post([self.finish(MGS, "dropped")])
        self.assertEqual((denied.status, self.error(denied)["code"]), (404, "not-found"))

    def test_two_owners_holding_one_game_keep_it_on_the_console(self) -> None:
        other = self.entry(OTHER, owner="someone-else", devices={"deck": q.Claim("off", STAMP)})
        self.put_queue(q.Queue(STAMP, (other,)))
        self.post([self.add(OTHER, ["deck", "machine"])])
        reply = self.post([self.finish(OTHER, release=["deck", "machine"])])
        self.assertEqual(reply.status, 200, reply.body)
        self.assertEqual(
            reply.json["skipped"],
            [{"device": "deck", "system": "psx", "game": OTHER.id, "reason": "claimed"}],
        )
        self.assertEqual(
            self.whitelist("deck").includes, frozenset({EXAMPLE.primary, OTHER.primary})
        )
        self.assertNotIn(OTHER.primary, self.whitelist("machine").includes)


class LimitTests(QueueCase):
    def big(self, size: int) -> q.Queue:
        group = q.Group("default", "x" * size, STAMP, ())
        return q.Queue(STAMP, (), {"aaaaaaaa": group})

    def test_a_write_over_the_size_limit_is_refused_with_nothing_written(self) -> None:
        self.put_queue(self.big(q.SIZE_LIMIT - 300))
        before = self.queue_file().read_bytes()
        deck = (self.library / "patterns-deck.stignore").read_bytes()
        reply = self.post([self.add(OTHER, ["deck"]), self.add(MGS, ["deck"])])
        self.assertEqual((reply.status, self.error(reply)["code"]), (409, "queue-too-large"))
        self.assertTrue(self.error(reply)["queue"])
        self.assertEqual(self.queue_file().read_bytes(), before)
        self.assertEqual((self.library / "patterns-deck.stignore").read_bytes(), deck)
        self.assertEqual(self.fake.writes(), [])
        self.assertEqual(self.backups(), [])

    def test_a_write_that_shrinks_a_file_past_the_limit_is_accepted(self) -> None:
        group = q.Group("default", "x" * (q.SIZE_LIMIT + 5000), STAMP, ())
        ended = tuple(self.entry(g, state="finished", ended=STAMP) for g in (OTHER, MGS))
        self.put_queue(q.Queue(STAMP, ended, {"aaaaaaaa": group}))
        self.assertGreater(self.queue_file().stat().st_size, q.SIZE_LIMIT)
        grow = self.post([self.add(EXAMPLE, [])])
        self.assertEqual((grow.status, self.error(grow)["code"]), (409, "queue-too-large"))
        shrink = self.post([{"op": "remove", "game": OTHER.id, "release": [], "keep": []}])
        self.assertEqual(shrink.status, 200, shrink.body)
        self.assertEqual([e.game.id for e in self.read_queue().entries], [MGS.id])

    def test_the_active_limit_and_the_operation_limit(self) -> None:
        full = tuple(
            self.entry(OTHER, game=q.GameRef(f"{n:016x}", "psx", "roms/psx/x.chd", "X", "x"))
            for n in range(1, q.MAX_ACTIVE + 1)
        )
        self.put_queue(q.Queue(STAMP, full))
        reply = self.post([self.add(OTHER, [])])
        self.assertEqual((reply.status, self.error(reply)["code"]), (409, "queue-full"))
        many = self.post([self.add(OTHER, [])] * (q.MAX_OPS + 1))
        self.assertEqual((many.status, self.error(many)["code"]), (400, "bad-request"))


class FailureTests(QueueCase):
    def test_a_failure_writing_the_queue_writes_no_whitelist(self) -> None:
        with unittest.mock.patch.object(
            queue_store.safefile, "guarded_edit", side_effect=OSError(28, "No space left on device")
        ):
            reply = self.post([self.add(OTHER, ["deck"])])
        self.assertEqual((reply.status, self.error(reply)["code"]), (500, "write-failed"))
        self.assertTrue(self.error(reply)["queue"])
        self.assertNotIn(str(self.root), reply.body.decode())
        self.assertFalse(self.queue_file().exists())
        self.assertNotIn(OTHER.primary, self.whitelist("deck").includes)
        self.assertEqual(self.fake.writes(), [])

    def test_a_failure_writing_a_whitelist_leaves_a_claim_the_next_get_repairs(self) -> None:
        with unittest.mock.patch.object(
            store,
            "replace",
            side_effect=store.WhitelistIOError(
                "write", Path("x"), OSError(13, "Permission denied")
            ),
        ):
            reply = self.post([self.add(OTHER, ["deck", "machine"])])
        self.assertEqual((reply.status, self.error(reply)["code"]), (500, "write-failed"))
        self.assertEqual(self.error(reply)["device"], "deck")
        self.assertTrue(reply.json["queue"])
        self.assertTrue(all(not d["written"] for d in reply.json["devices"]))
        self.assertEqual(self.read_queue().entries[0].game.id, OTHER.id)
        self.assertNotIn(OTHER.primary, self.whitelist("deck").includes)
        shown = self.get("/api/v1/queue").json["entries"][0]["devices"]
        self.assertEqual(
            {n: c["view"] for n, c in shown.items()}, {"deck": "not-on", "machine": "not-on"}
        )
        # one click repairs it
        fixed = self.post([{"op": "sync", "game": OTHER.id, "device": "deck"}])
        self.assertEqual(fixed.status, 200, fixed.body)
        self.assertIn(OTHER.primary, self.whitelist("deck").includes)

    def test_a_partial_failure_lists_the_whitelists_already_written(self) -> None:
        real = store.replace
        calls = []

        def second_fails(*args: Any, **kw: Any) -> Any:
            calls.append(args[1])
            if len(calls) == 2:
                raise store.WhitelistIOError("write", Path("x"), OSError(5, "Input/output error"))
            return real(*args, **kw)

        with unittest.mock.patch.object(store, "replace", second_fails):
            reply = self.post([self.add(OTHER, ["deck", "machine"])])
        self.assertEqual(reply.status, 500)
        written = {d["device"]: d["written"] for d in reply.json["devices"]}
        self.assertEqual(written, {"deck": True, "machine": False})
        self.assertEqual(self.error(reply)["device"], "machine")
        self.assertIn(OTHER.primary, self.whitelist("deck").includes)

    def test_a_failure_in_the_second_queue_write_leaves_the_entry(self) -> None:
        self.post([self.add(OTHER, ["deck"])])
        real = queue_store.replace
        calls = []

        def second_fails(*args: Any, **kw: Any) -> Any:
            calls.append(1)
            if len(calls) == 2:
                raise OSError(5, "Input/output error")
            return real(*args, **kw)

        with unittest.mock.patch.object(queue_store, "replace", second_fails):
            reply = self.post([{"op": "remove", "game": OTHER.id, "release": ["deck"], "keep": []}])
        self.assertEqual((reply.status, self.error(reply)["code"]), (500, "write-failed"))
        self.assertTrue(self.error(reply)["queue"])
        self.assertTrue(reply.json["queue"])
        self.assertTrue(reply.json["devices"][0]["written"])
        self.assertNotIn(OTHER.primary, self.whitelist("deck").includes)
        (found,) = self.get("/api/v1/queue").json["entries"]
        self.assertEqual(found["devices"]["deck"]["view"], "released")
        # the entry can now be removed with nothing left to release
        done = self.post([{"op": "remove", "game": OTHER.id, "release": [], "keep": []}])
        self.assertEqual(done.status, 200, done.body)
        self.assertEqual(self.read_queue().entries, ())


class PendingTests(QueueCase):
    def failing_write(self) -> Any:
        err = store.WhitelistIOError("write", Path("x"), OSError(5, "Input/output error"))
        return unittest.mock.patch.object(store, "replace", side_effect=err)

    def test_a_release_that_never_reached_the_whitelist_is_not_forgotten(self) -> None:
        self.post([self.add(OTHER, ["deck"])])
        with self.failing_write():
            failed = self.post([self.finish(OTHER, release=["deck"])])
        self.assertEqual(failed.status, 500)
        self.assertIn(OTHER.primary, self.whitelist("deck").includes)
        shown = self.get("/api/v1/queue").json["history"][0]["devices"]["deck"]
        self.assertEqual((shown["end"], shown["view"]), ("released", "not-released"))
        # queueing it again must not record the game as the user's
        again = self.post([self.add(OTHER, ["deck"])])
        self.assertEqual(again.status, 200, again.body)
        self.assertEqual(self.entries_by_id()[OTHER.id].devices["deck"].before, "off")
        finished = self.post([self.finish(OTHER, release=["deck"])])
        self.assertEqual(finished.status, 200, finished.body)
        self.assertNotIn(OTHER.primary, self.whitelist("deck").includes)

    def test_removing_after_a_failed_release_needs_the_console_named(self) -> None:
        self.post([self.add(OTHER, ["deck"])])
        gone = {"op": "remove", "game": OTHER.id, "keep": []}
        with self.failing_write():
            failed = self.post([{**gone, "release": ["deck"]}])
        self.assertEqual(failed.status, 500)
        refused = self.post([{**gone, "release": []}])
        self.assertEqual((refused.status, self.error(refused)["code"]), (400, "bad-request"))
        self.assertEqual([e.game.id for e in self.read_queue().entries], [OTHER.id])
        done = self.post([{**gone, "release": ["deck"]}])
        self.assertEqual(done.status, 200, done.body)
        self.assertEqual(self.read_queue().entries, ())
        self.assertNotIn(OTHER.primary, self.whitelist("deck").includes)


class RaceTests(QueueCase):
    def test_a_whitelist_read_but_not_written_is_rechecked_under_the_lock(self) -> None:
        # EXAMPLE is on the deck: queueing it writes no whitelist, only records before=on
        real = queue_store.load
        calls = []

        def racing(root: Path) -> queue_store.Loaded:
            calls.append(1)
            if len(calls) == 2:  # between the plan and the lock, a switch turns it off
                self.write_whitelist("deck", managed(["gba"]))
            return real(root)

        with unittest.mock.patch.object(queue_store, "load", racing):
            reply = self.post([self.add(EXAMPLE, ["deck"])])
        self.assertEqual((reply.status, self.error(reply)["code"]), (412, "stale"))
        self.assertEqual(self.error(reply)["device"], "deck")
        self.assertFalse(self.queue_file().exists())


class NoCatalogTests(QueueCase):
    def test_the_answer_names_the_folder_and_not_the_hub_path(self) -> None:
        self.start(files=serve.HubFiles(self.root, interval=1000, media_seconds=0))
        self.assertEqual(self.get("/api/v1/systems").status, 200)  # the catalog is cached
        (self.root / "catalog").rename(self.tmp / "catalog-away")
        reply = self.post([self.add(OTHER, [])])
        self.assertEqual((reply.status, self.error(reply)["code"]), (500, "write-failed"))
        self.assertTrue(self.error(reply)["queue"])
        self.assertIn("catalog/", self.error(reply)["message"])
        self.assertNotIn(str(self.root), reply.body.decode())
        self.assertTrue(any(str(self.root) in line for line in self.logs))
        self.assertFalse((self.root / "catalog").exists())


if __name__ == "__main__":
    unittest.main()
