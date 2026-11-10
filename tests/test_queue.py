"""The play queue's pure decisions: the schema, the inferred states, the views, the release
rule and `plan`. No filesystem, no network."""

import json
import unittest
import unittest.mock
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any

from library_support import config, game, games, library, managed
from warpzone import library_api as api
from warpzone import queue as q
from warpzone.library_api import ApiError

NOW = datetime(2026, 11, 10, 20, 0, 0, tzinfo=UTC)
STAMP = "2026-11-10T20:00:00Z"
ID = {(s, g.title): g.id for s, g in games()}
EXAMPLE = ID["psx", "Example Game (Europe)"]
OTHER = ID["psx", "Other Game"]
MGS = ID["psx", "Metal Gear Solid (USA)"]
MGS_GBC = ID["gbc", "Metal Gear Solid (USA)"]
TINY = ID["gba", "Tiny"]
PS4 = ID["ps4", "Example PS4"]
BAD = ID["psx", "Bad Name"]
EXAMPLE_PATH = "roms/psx/Example Game (Europe).chd"
OTHER_PATH = "roms/psx/Other Game.chd"
GONE = "0" * 16


def ref(game_id: str = EXAMPLE, **kw: str) -> q.GameRef:
    system, found = library().by_id[game_id]
    return replace(q.ref_of(system, found), **kw)


def entry(game_id: str = EXAMPLE, state: str = "queued", **kw: Any) -> q.Entry:
    fields: dict[str, Any] = {
        "owner": "default",
        "game": ref(game_id),
        "state": state,
        "added": STAMP,
        "changed": STAMP,
        "ended": STAMP if state in q.ENDED else None,
        "ra_dismissed": None,
        "devices": {},
    }
    return q.Entry(**{**fields, **kw})


def view(text: str) -> api.DeviceView:
    return api.view_of(text.encode())


def add(game_id: str = EXAMPLE, devices: list[str] | None = None, **kw: Any) -> dict[str, Any]:
    return {"op": "add", "game": game_id, "devices": devices or [], **kw}


def finish(game_id: str, state: str = "finished", **kw: Any) -> dict[str, Any]:
    return {"op": "state", "game": game_id, "state": state, "release": [], "keep": [], **kw}


def remove(game_id: str, **kw: Any) -> dict[str, Any]:
    return {"op": "remove", "game": game_id, "release": [], "keep": [], **kw}


class World:
    """A queue and two whitelists, changed by requests the way the server changes them."""

    def __init__(self, deck: str | None = None, machine: str | None = None) -> None:
        self.cfg = config()
        self.queue = q.Queue()
        self.reading = q.Reading("absent", self.queue)
        self.views = {
            "deck": view(deck if deck is not None else managed(["gba"], includes=[EXAMPLE_PATH])),
            "machine": view(machine if machine is not None else managed()),
        }
        self.etag = "absent"
        self.size = 0

    def context(self) -> q.Context:
        return q.Context(
            self.cfg, library(), self.views, api.PlayIndex(), lambda system: api.SystemExtras()
        )

    @staticmethod
    def touched(op: dict[str, Any]) -> list[str]:
        found = [*op.get("devices", []), *op.get("add", []), *op.get("release", [])]
        return found + ([op["device"]] if "device" in op else [])

    def body(self, ops: list[dict[str, Any]], **extra: Any) -> dict[str, Any]:
        names = {d for op in ops for d in self.touched(op) if d in self.views}
        return {
            "ops": ops,
            "expect_queue": self.etag,
            "expect": {n: self.views[n].etag for n in names},
            **extra,
        }

    def plan(self, ops: list[dict[str, Any]], now: datetime = NOW, **extra: Any) -> q.QueuePlan:
        body = self.body(ops, **extra)
        return q.plan(body, self.reading, self.etag, self.size, self.context(), now)

    def apply(self, ops: list[dict[str, Any]], now: datetime = NOW) -> q.QueuePlan:
        """Plan the request and take its result as the new state."""
        plan = self.plan(ops, now)
        self.set(plan.queue)
        self.views = dict(plan.views)
        return plan

    def set(self, queue: q.Queue) -> None:
        data = q.render(queue)
        self.queue, self.reading = queue, q.Reading("ok", queue)
        self.etag, self.size = q.etag_of(data), len(data)

    def entries(self) -> dict[str, q.Entry]:
        return {e.game.id: e for e in self.queue.entries}

    def code(self, ops: list[dict[str, Any]], **extra: Any) -> str:
        try:
            self.plan(ops, **extra)
        except ApiError as exc:
            return exc.code
        raise AssertionError("the request was accepted")


class SchemaTests(unittest.TestCase):
    def full(self) -> q.Queue:
        claim = q.Claim("off", STAMP, "released", "2026-11-11T08:00:00Z")
        group = q.Group("default", "Couch co-op", STAMP, (ref(EXAMPLE), ref(OTHER)))
        return q.Queue(
            STAMP,
            (
                entry(EXAMPLE, devices={"deck": q.Claim("off", STAMP), "machine": claim}),
                entry(OTHER, "finished", ra_dismissed=STAMP),
                entry(MGS, owner="someone-else"),
            ),
            {"3f2a9c1e": group},
        )

    def doc(self) -> dict[str, Any]:
        return json.loads(q.render(self.full()))

    def test_the_file_round_trips_and_is_deterministic(self) -> None:
        queue = self.full()
        data = q.render(queue)
        self.assertEqual(q.parse(data), q.Reading("ok", queue))
        self.assertEqual(q.render(q.parse(data).queue), data)
        self.assertTrue(data.endswith(b"}\n"))
        text = data.decode()
        self.assertLess(text.index('"collections"'), text.index('"format"'))

    def test_no_file_is_absent_with_an_empty_queue(self) -> None:
        self.assertEqual(q.parse(None), q.Reading("absent", q.Queue()))
        self.assertEqual(q.etag_of(None), "absent")
        self.assertTrue(q.etag_of(b"x").startswith("sha256:"))

    def test_a_file_that_is_not_json_is_damaged(self) -> None:
        for data in (b"\xff\xfe", b"not json", b"[]", b"x" * (q.FILE_LIMIT + 1)):
            reading = q.parse(data)
            self.assertEqual((reading.state, reading.queue), ("damaged", q.Queue()))
            self.assertTrue(reading.message)

    def test_every_way_a_document_is_damaged(self) -> None:
        def key(path: str, value: Any) -> Any:
            def mutate(doc: dict[str, Any]) -> None:
                *head, last = path.split(".")
                target: Any = doc
                for part in head:
                    target = target[int(part)] if part.isdigit() else target[part]
                target[int(last) if last.isdigit() else last] = value

            return mutate

        def drop(path: str) -> Any:
            def mutate(doc: dict[str, Any]) -> None:
                del doc[path]

            return mutate

        def twice(doc: dict[str, Any]) -> None:
            doc["queue"].append(doc["queue"][0])

        def twice_in_group(doc: dict[str, Any]) -> None:
            games_ = doc["collections"]["3f2a9c1e"]["games"]
            games_.append(games_[0])

        cases: dict[str, Any] = {
            "unknown top key": key("extra", 1),
            "missing key": drop("updated"),
            "format zero": key("format", 0),
            "format true": key("format", True),
            "bad time": key("updated", "2026-11-10 20:00:00"),
            "impossible time": key("updated", "2026-13-10T20:00:00Z"),
            "entry unknown key": key("queue.0.extra", 1),
            "bad state": key("queue.0.state", "bored"),
            "bad owner": key("queue.0.owner", "Bad Owner"),
            "long owner": key("queue.0.owner", "a" * 33),
            "bad game id": key("queue.0.game.id", "xyz"),
            "game wrong type": key("queue.0.game.title", 3),
            "ended while active": key("queue.0.ended", STAMP),
            "not ended while ended": key("queue.1.ended", None),
            "bad device name": key("queue.0.devices.Bad Name", {}),
            "claim before": key("queue.0.devices.deck.before", "maybe"),
            "claim end": key("queue.0.devices.deck.end", "gone"),
            "claim end without until": key("queue.0.devices.deck.end", "kept"),
            "duplicate owner and game": twice,
            "queue not a list": key("queue", {}),
            "bad collection id": key("collections.zz", {}),
            "collection games repeat": twice_in_group,
        }
        for name, mutate in cases.items():
            doc = self.doc()
            mutate(doc)
            with self.subTest(name):
                self.assertEqual(q.parse(json.dumps(doc).encode()).state, "damaged")

    def test_the_same_game_for_two_owners_is_fine(self) -> None:
        doc = self.doc()
        doc["queue"].append({**doc["queue"][0], "owner": "someone-else"})
        self.assertEqual(q.parse(json.dumps(doc).encode()).state, "ok")

    def test_a_higher_format_is_newer_and_not_read(self) -> None:
        reading = q.parse(json.dumps({"format": 2, "anything": [1]}).encode())
        self.assertEqual((reading.state, reading.queue), ("newer", q.Queue()))
        self.assertIn("format 2", reading.message)

    def test_size_check_refuses_growth_past_the_limit_only(self) -> None:
        self.assertTrue(q.size_check(q.SIZE_LIMIT, 0))
        self.assertFalse(q.size_check(q.SIZE_LIMIT + 1, q.SIZE_LIMIT))
        self.assertTrue(q.size_check(q.SIZE_LIMIT + 1, q.SIZE_LIMIT + 1))
        self.assertTrue(q.size_check(q.SIZE_LIMIT + 5, q.SIZE_LIMIT + 9))


class ResolveTests(unittest.TestCase):
    def test_a_game_in_the_catalog_is_present(self) -> None:
        self.assertEqual(q.resolve(ref(), library()), q.Resolution("present"))

    def test_a_game_that_left_is_moved_with_same_system_candidates_first(self) -> None:
        gone = replace(ref(MGS), id=GONE, primary="roms/psx/old.cue")
        found = q.resolve(gone, library())
        self.assertEqual(found.found, "moved")
        self.assertEqual([(s, g.id) for s, g in found.candidates], [("psx", MGS), ("gbc", MGS_GBC)])
        elsewhere = q.resolve(replace(gone, system="gbc"), library())
        self.assertEqual([s for s, _ in elsewhere.candidates], ["gbc", "psx"])

    def test_games_already_in_the_queue_are_left_out_and_then_it_is_missing(self) -> None:
        gone = replace(ref(MGS), id=GONE)
        self.assertEqual([g.id for _, g in q.resolve(gone, library(), {MGS}).candidates], [MGS_GBC])
        self.assertEqual(q.resolve(gone, library(), {MGS, MGS_GBC}), q.Resolution("missing"))

    def test_a_game_with_no_key_match_is_missing(self) -> None:
        lost = replace(ref(), id=GONE, key="nothing")
        self.assertEqual(q.resolve(lost, library()).found, "missing")

    def test_at_most_ten_candidates(self) -> None:
        items = [
            (f"s{n:02d}", game(f"s{n:02d}", f"roms/s/{n}.chd", "Same Title")) for n in range(15)
        ]
        gone = replace(ref(), id=GONE, key=items[0][1].key)
        self.assertEqual(len(q.resolve(gone, library(items)).candidates), 10)


class InferredStateTests(unittest.TestCase):
    ADDED = int(datetime(2026, 11, 1, 18, tzinfo=UTC).timestamp())

    def shown(self, last: int | None, state: str = "queued", now: int = 0) -> tuple[str, bool]:
        e = entry(state=state, added="2026-11-01T18:00:00Z")
        play = None if last is None else {"last": last}
        return q.shown_state(e, play, now or self.ADDED + 5 * 86400)

    def test_played_since_it_was_queued_and_recently_is_playing(self) -> None:
        self.assertEqual(self.shown(self.ADDED + 3600), ("playing", True))
        self.assertEqual(self.shown(self.ADDED), ("playing", True))  # at `added`

    def test_the_edges_of_the_window(self) -> None:
        self.assertEqual(self.shown(self.ADDED - 1), ("queued", False))  # before it was queued
        now = self.ADDED + 30 * 86400
        self.assertEqual(self.shown(now - 14 * 86400, now=now), ("playing", True))
        self.assertEqual(self.shown(now - 14 * 86400 - 1, now=now), ("queued", False))

    def test_unplayed_and_stored_states(self) -> None:
        self.assertEqual(self.shown(None), ("queued", False))
        self.assertEqual(self.shown(None, "playing"), ("playing", False))
        self.assertEqual(self.shown(self.ADDED + 1, "finished"), ("finished", False))
        self.assertEqual(self.shown(self.ADDED + 1, "dropped"), ("dropped", False))
        e = entry(added="2026-11-01T18:00:00Z")
        self.assertEqual(q.shown_state(e, {"last": None}, self.ADDED), ("queued", False))

    def sug(self, kind: str, date: str, **kw: Any) -> dict[str, str] | None:
        e = entry(added="2026-11-01T18:00:00Z", **kw)
        return q.suggestion(e, {"kind": kind, "date": date})

    def test_each_award_kind_suggests_finished(self) -> None:
        for kind in q.AWARDS:
            self.assertEqual(
                self.sug(kind, "2026-11-09T21:00:00+00:00"),
                {"state": "finished", "award": kind, "date": "2026-11-09T21:00:00+00:00"},
            )
        self.assertIsNone(self.sug("participated", "2026-11-09T21:00:00+00:00"))

    def test_an_award_from_before_the_queue_suggests_nothing(self) -> None:
        self.assertIsNone(self.sug("mastered", "2026-10-01T20:00:00+00:00"))
        self.assertIsNone(self.sug("mastered", "2026-11-01 17:59:59"))
        self.assertIsNotNone(self.sug("mastered", "2026-11-01 18:00:00"))
        self.assertIsNotNone(self.sug("mastered", "2026-11-01T20:00:00+02:00"))  # 18:00 UTC
        self.assertIsNone(self.sug("mastered", "2026-11-01T19:59:59+02:00"))
        self.assertIsNone(self.sug("mastered", "2026-11-01"))  # midnight, before 18:00

    def test_a_dismissed_award_is_not_suggested_again_but_a_later_one_is(self) -> None:
        dismissed = {"ra_dismissed": "2026-11-09T21:00:00Z"}
        self.assertIsNone(self.sug("beaten-hardcore", "2026-11-09T21:00:00+00:00", **dismissed))
        self.assertIsNone(self.sug("beaten-hardcore", "2026-11-09T20:00:00+00:00", **dismissed))
        self.assertIsNotNone(self.sug("mastered", "2026-11-10T09:00:00+00:00", **dismissed))

    def test_no_award_no_date_and_ended_entries_suggest_nothing(self) -> None:
        e = entry(added="2026-11-01T18:00:00Z")
        self.assertIsNone(q.suggestion(e, None))
        self.assertIsNone(q.suggestion(e, {}))
        self.assertIsNone(q.suggestion(e, {"kind": "mastered", "date": "soon"}))
        done = self.sug("mastered", "2026-11-09T21:00:00+00:00", state="finished", ended=STAMP)
        self.assertIsNone(done)
        self.assertIsNotNone(self.sug("mastered", "2026-11-09T21:00:00+00:00", state="playing"))


class ViewTests(unittest.TestCase):
    def v(self, claim: q.Claim, state: str, found: str = "present", known: bool = True) -> str:
        return q.device_view(claim, state, found, known)

    def test_a_held_claim_is_on_or_not_on(self) -> None:
        held = q.Claim("off", STAMP)
        self.assertEqual(self.v(held, "on"), "on")
        self.assertEqual(self.v(held, "off"), "not-on")
        self.assertEqual(self.v(held, "partial"), "not-on")

    def test_what_was_there_before_or_kept_is_kept(self) -> None:
        for before in ("on", "partial"):
            for state in ("on", "off", "partial"):
                self.assertEqual(self.v(q.Claim(before, STAMP), state), "kept")
        self.assertEqual(self.v(q.Claim("off", STAMP, "kept", STAMP), "on"), "kept")

    def test_a_released_claim(self) -> None:
        released = q.Claim("off", STAMP, "released", STAMP)
        self.assertEqual(self.v(released, "off"), "released")
        self.assertEqual(self.v(released, "on"), "not-released")
        self.assertEqual(self.v(released, "partial"), "not-released")

    def test_unknown(self) -> None:
        held = q.Claim("off", STAMP)
        self.assertEqual(self.v(held, "on", known=False), "unknown")
        self.assertEqual(self.v(held, "on", found="moved"), "unknown")
        self.assertEqual(self.v(held, "on", found="missing"), "unknown")
        self.assertEqual(self.v(held, "unknown"), "unknown")
        self.assertEqual(self.v(held, "unsupported"), "unknown")

    def test_holds(self) -> None:
        self.assertTrue(q.holds(q.Claim("off", STAMP)))
        self.assertFalse(q.holds(q.Claim("on", STAMP)))
        self.assertFalse(q.holds(q.Claim("off", STAMP, "kept", STAMP)))


class ReleaseChangeTests(unittest.TestCase):
    PARTS = (EXAMPLE_PATH,)

    def model(self, text: str) -> Any:
        return view(text).model

    def test_it_removes_the_games_includes_and_nothing_else(self) -> None:
        model = self.model(managed(["gba"], includes=[EXAMPLE_PATH, OTHER_PATH]))
        new = q.release_change(model, self.PARTS)
        self.assertEqual((new.systems, new.includes), (model.systems, frozenset({OTHER_PATH})))
        self.assertEqual(new.excludes, frozenset())

    def test_a_game_already_off_is_a_clean_no_change(self) -> None:
        model = self.model(managed())
        self.assertEqual(q.release_change(model, self.PARTS), model)

    def test_a_whole_system_turned_on_covers_it(self) -> None:
        self.assertIsNone(q.release_change(self.model(managed(["psx"])), self.PARTS))

    def test_a_parent_folder_include_covers_it(self) -> None:
        self.assertIsNone(q.release_change(self.model(managed(includes=["roms/psx"])), self.PARTS))


class RequestTests(unittest.TestCase):
    def test_the_shape_of_a_body(self) -> None:
        bodies: list[Any] = [
            [],
            {},
            {"ops": []},
            {"ops": {}},
            {"ops": [add()] * 51},
            {"ops": [add()], "extra": 1},
            {"ops": [add()], "expect": {"deck": 1}},
            {"ops": [add()], "expect_queue": 3},
            {"ops": [add()], "dry_run": "yes"},
            {"ops": ["add"]},
            {"ops": [{"op": "nope", "game": "x"}]},
            {"ops": [{"op": "add", "game": "x"}]},  # no devices
            {"ops": [{"op": "add", "game": "x", "devices": ["a"], "extra": 1}]},
            {"ops": [add(devices=["deck", "deck"])]},
            {"ops": [add(devices=[f"d{n}" for n in range(9)])]},
            {"ops": [add(position=-1)]},
            {"ops": [add(position=True)]},
            {"ops": [add(position="1")]},
            {"ops": [{"op": "move", "game": "x", "position": None}]},
            {"ops": [{"op": "state", "game": "x", "state": "bored"}]},
            {"ops": [{"op": "state", "game": "x", "state": "dropped", "release": "deck"}]},
            {"ops": [{"op": "sync", "game": "x"}]},
            {"ops": [{"op": "relink", "game": "x"}]},
            {"ops": [{"op": "dismiss", "game": ""}]},
        ]
        for body in bodies:
            with self.subTest(body):
                with self.assertRaises(ApiError) as caught:
                    q.parse_request(body)
                self.assertEqual(caught.exception.code, "bad-request")

    def test_a_valid_body(self) -> None:
        request = q.parse_request(
            {
                "ops": [add(devices=["deck"], position=None), {"op": "dismiss", "game": "x"}],
                "expect_queue": "absent",
                "expect": {"deck": "sha256:aa"},
                "dry_run": True,
            }
        )
        self.assertEqual(request.ops[0], q.Op("add", EXAMPLE, devices=("deck",)))
        self.assertTrue(request.dry_run)

    def test_the_history_parameter(self) -> None:
        self.assertEqual(q.parse_history(None), 50)
        self.assertEqual(q.parse_history("0"), 0)
        self.assertEqual(q.parse_history("10000"), 10000)
        for raw in ("", "-1", "10001", "x", "1.5", "٣"):
            with self.assertRaises(ApiError):
                q.parse_history(raw)


class PlanAddTests(unittest.TestCase):
    def test_queueing_for_a_console_turns_the_game_on_and_records_before(self) -> None:
        w = World()
        plan = w.apply([add(OTHER, ["deck", "machine"])])
        claims = w.entries()[OTHER].devices
        self.assertEqual(claims, {"deck": q.Claim("off", STAMP), "machine": q.Claim("off", STAMP)})
        self.assertEqual({d.device for d in plan.devices if d.changed}, {"deck", "machine"})
        self.assertEqual(plan.devices[0].turned_on, (OTHER,))
        self.assertIsNotNone(plan.first)
        self.assertIsNone(plan.second)
        self.assertEqual(plan.queue.updated, STAMP)

    def test_a_game_already_on_is_recorded_and_not_written(self) -> None:
        w = World()
        plan = w.apply([add(EXAMPLE, ["deck"])])
        self.assertEqual(w.entries()[EXAMPLE].devices["deck"].before, "on")
        self.assertFalse(plan.devices[0].changed)

    def test_a_partly_on_game_is_turned_fully_on_and_never_held(self) -> None:
        w = World(deck=managed(includes=["roms/ps4/shortcuts/Example PS4.desktop"]))
        plan = w.apply([add(PS4, ["deck"])])
        claim = w.entries()[PS4].devices["deck"]
        self.assertEqual(claim.before, "partial")
        self.assertFalse(q.holds(claim))
        self.assertEqual(plan.devices[0].turned_on, (PS4,))

    def test_a_game_for_no_console_is_just_queued(self) -> None:
        w = World()
        plan = w.apply([add(OTHER)])
        self.assertEqual(w.entries()[OTHER].devices, {})
        self.assertEqual(plan.devices, ())

    def test_the_default_position_is_last_and_a_position_inserts(self) -> None:
        w = World()
        w.apply([add(OTHER), add(MGS)])
        w.apply([add(TINY, position=0)])
        w.apply([add(EXAMPLE, position=1)])
        self.assertEqual([e.game.id for e in w.queue.entries], [TINY, EXAMPLE, OTHER, MGS])

    def test_a_position_past_the_active_entries_is_refused(self) -> None:
        w = World()
        w.apply([add(OTHER)])
        self.assertEqual(w.code([add(MGS, position=2)]), "bad-request")
        w.plan([add(MGS, position=1)])

    def test_already_queued_unsupported_unknown_and_unmanaged(self) -> None:
        w = World()
        w.apply([add(OTHER)])
        self.assertEqual(w.code([add(OTHER)]), "already-queued")
        self.assertEqual(w.code([add(PS4, ["machine"])]), "unsupported")
        self.assertEqual(w.code([add(GONE, ["deck"])]), "not-found")
        self.assertEqual(w.code([add(MGS, ["tablet"])]), "not-found")
        self.assertEqual(w.code([add(BAD, ["deck"])]), "unlistable")
        w.views["machine"] = view("tmp/*.txt\n")
        self.assertEqual(w.code([add(MGS, ["machine"])]), "not-managed")

    def test_a_game_with_an_unlistable_path_can_be_queued_for_no_console(self) -> None:
        World().plan([add(BAD)])

    def test_two_adds_in_one_request_share_the_whitelist_model(self) -> None:
        w = World()
        plan = w.apply([add(OTHER, ["deck"]), add(MGS, ["deck"])])
        (deck,) = plan.devices
        self.assertEqual(set(deck.turned_on), {OTHER, MGS})

    def test_a_dry_run_is_flagged(self) -> None:
        w = World()
        self.assertTrue(w.plan([add(OTHER, ["deck"])], dry_run=True).dry_run)
        self.assertFalse(w.plan([add(OTHER, ["deck"])]).dry_run)

    def test_the_active_limit(self) -> None:
        w = World()
        full = tuple(
            entry(OTHER, game=replace(ref(OTHER), id=f"{n:016x}")) for n in range(q.MAX_ACTIVE)
        )
        w.set(q.Queue(STAMP, full))
        self.assertEqual(w.code([add(MGS)]), "queue-full")
        # an ended entry does not count
        w.set(q.Queue(STAMP, (entry(OTHER, "dropped"), *full[1:])))
        w.plan([add(MGS)])

    def test_an_ended_entry_starts_again_with_new_claims_and_its_place(self) -> None:
        w = World()
        w.apply([add(OTHER, ["deck"]), add(MGS)])
        w.apply([finish(OTHER, release=["deck"])])
        later = NOW + timedelta(days=1)
        plan = w.apply([add(OTHER, ["machine"], position=0)], later)
        e = w.entries()[OTHER]
        self.assertEqual((e.state, e.added, e.ended), ("queued", "2026-11-11T20:00:00Z", None))
        self.assertEqual(e.devices, {"machine": q.Claim("off", "2026-11-11T20:00:00Z")})
        self.assertEqual([x.game.id for x in w.queue.entries], [OTHER, MGS])
        self.assertIsNotNone(plan.first)


class PlanConflictTests(unittest.TestCase):
    ZERO = "sha256:" + "0" * 64

    def run_plan(self, w: World, body: dict[str, Any]) -> ApiError:
        with self.assertRaises(ApiError) as caught:
            q.plan(body, w.reading, "absent", 0, w.context(), NOW)
        return caught.exception

    def test_missing_and_stale_expectations(self) -> None:
        w = World()
        ops = [add(OTHER, ["deck"])]
        deck = w.views["deck"].etag
        cases = (
            ({"ops": ops, "expect": {"deck": deck}}, "expect-missing"),
            ({"ops": ops, "expect_queue": "absent"}, "expect-missing"),
            ({"ops": ops, "expect_queue": self.ZERO, "expect": {"deck": deck}}, "stale"),
            ({"ops": ops, "expect_queue": "absent", "expect": {"deck": self.ZERO}}, "stale"),
        )
        for body, code in cases:
            with self.subTest(code, body=body):
                self.assertEqual(self.run_plan(w, body).code, code)

    def test_the_error_says_whether_it_is_about_the_queue_or_a_device(self) -> None:
        w = World()
        error = self.run_plan(w, {"ops": [add(OTHER)], "expect_queue": self.ZERO}).body()["error"]
        self.assertEqual(error.get("queue"), True)
        self.assertNotIn("device", error)
        body = {
            "ops": [add(OTHER, ["deck"])],
            "expect_queue": "absent",
            "expect": {"deck": self.ZERO},
        }
        error = self.run_plan(w, body).body()["error"]
        self.assertEqual(error["device"], "deck")
        self.assertNotIn("queue", error)

    def test_a_bare_hex_etag_is_accepted(self) -> None:
        w = World()
        w.apply([add(OTHER)])
        body = w.body([add(MGS)])
        body["expect_queue"] = w.etag.removeprefix("sha256:")
        q.plan(body, w.reading, w.etag, w.size, w.context(), NOW)

    def test_a_console_the_request_does_not_touch_needs_no_expect(self) -> None:
        World().plan([add(OTHER, ["deck"])])

    def test_a_damaged_or_newer_queue_refuses_every_write(self) -> None:
        w = World()
        for state in ("damaged", "newer"):
            w.reading = q.Reading(state, q.Queue(), "because")
            with self.subTest(state):
                self.assertEqual(w.code([add(OTHER)]), "queue-damaged")


class PlanStateTests(unittest.TestCase):
    def world(self) -> World:
        w = World()
        w.apply([add(OTHER, ["deck", "machine"]), add(EXAMPLE, ["machine"])])
        return w

    def test_playing_and_back_change_only_the_state(self) -> None:
        w = self.world()
        later = NOW + timedelta(hours=1)
        plan = w.apply([finish(OTHER, "playing")], later)
        e = w.entries()[OTHER]
        self.assertEqual((e.state, e.changed, e.added), ("playing", "2026-11-10T21:00:00Z", STAMP))
        self.assertEqual(plan.devices, ())
        w.apply([finish(OTHER, "queued")], later)
        self.assertEqual(w.entries()[OTHER].state, "queued")

    def test_finishing_must_name_every_held_console(self) -> None:
        w = self.world()
        self.assertEqual(w.code([finish(OTHER)]), "bad-request")
        self.assertEqual(w.code([finish(OTHER, release=["deck"])]), "bad-request")
        both = finish(OTHER, release=["deck"], keep=["deck", "machine"])
        self.assertEqual(w.code([both]), "bad-request")

    def test_a_console_that_is_not_held_cannot_be_released(self) -> None:
        w = self.world()
        self.assertEqual(w.code([finish(EXAMPLE, release=["machine", "deck"])]), "not-held")
        self.assertEqual(w.code([finish(EXAMPLE, keep=["machine", "deck"])]), "not-held")

    def test_a_game_that_was_there_before_is_never_released(self) -> None:
        w = World()
        w.apply([add(EXAMPLE, ["deck"])])  # on the deck before: before=on
        self.assertEqual(w.code([finish(EXAMPLE, release=["deck"])]), "not-held")
        plan = w.apply([finish(EXAMPLE)])  # nothing held: it finishes and the claim ends kept
        self.assertEqual(plan.devices, ())
        e = w.entries()[EXAMPLE]
        self.assertEqual((e.state, e.ended), ("finished", STAMP))
        self.assertEqual(e.devices["deck"].end, "kept")

    def test_release_turns_the_game_off_keep_leaves_it(self) -> None:
        w = self.world()
        plan = w.apply([finish(OTHER, release=["deck"], keep=["machine"])])
        by = {d.device: d for d in plan.devices}
        self.assertEqual(by["deck"].turned_off, (OTHER,))
        self.assertNotIn("machine", by)
        e = w.entries()[OTHER]
        self.assertEqual(
            {n: (c.end, c.until) for n, c in e.devices.items()},
            {"deck": ("released", STAMP), "machine": ("kept", STAMP)},
        )
        self.assertEqual(plan.skipped, ())
        self.assertEqual((e.state, e.ended), ("finished", STAMP))
        self.assertEqual(w.views["deck"].model.includes, frozenset({EXAMPLE_PATH}))

    def test_a_whole_system_turned_on_afterwards_covers_the_game(self) -> None:
        w = self.world()
        model = w.views["deck"].model
        w.views["deck"] = view(managed(model.systems | {"psx"}, model.includes))
        plan = w.apply([finish(OTHER, release=["deck", "machine"])])
        self.assertEqual(
            plan.skipped,
            ({"device": "deck", "system": "psx", "game": OTHER, "reason": "covered"},),
        )
        self.assertEqual(w.entries()[OTHER].devices["deck"].end, "kept")
        self.assertEqual(w.entries()[OTHER].devices["machine"].end, "released")
        self.assertIn("psx", w.views["deck"].model.systems)
        self.assertEqual([d.device for d in plan.devices if d.changed], ["machine"])

    def test_another_owners_open_claim_keeps_the_game_on(self) -> None:
        w = self.world()
        other = entry(OTHER, owner="someone-else", devices={"deck": q.Claim("on", STAMP)})
        w.set(replace(w.queue, entries=(*w.queue.entries, other)))
        plan = w.apply([finish(OTHER, release=["deck", "machine"])])
        self.assertEqual([(s["device"], s["reason"]) for s in plan.skipped], [("deck", "claimed")])
        mine = w.queue.entries[0]
        self.assertEqual(mine.devices["deck"].end, "kept")
        self.assertEqual(mine.devices["machine"].end, "released")
        self.assertEqual(w.queue.entries[-1], other)  # the other owner's entry is untouched

    def test_an_ended_claim_of_another_owner_does_not_block(self) -> None:
        w = self.world()
        done = q.Claim("on", STAMP, "kept", STAMP)
        other = entry(OTHER, "finished", owner="someone-else", devices={"deck": done})
        w.set(replace(w.queue, entries=(*w.queue.entries, other)))
        plan = w.apply([finish(OTHER, release=["deck", "machine"])])
        self.assertEqual(plan.skipped, ())

    def test_ended_entries_can_swap_or_come_back_without_new_claims(self) -> None:
        w = self.world()
        w.apply([finish(OTHER, release=["deck", "machine"])])
        w.apply([finish(OTHER, "dropped")])
        e = w.entries()[OTHER]
        self.assertEqual((e.state, e.ended), ("dropped", STAMP))
        w.apply([finish(OTHER, "queued")])
        e = w.entries()[OTHER]
        self.assertEqual((e.state, e.ended), ("queued", None))
        self.assertTrue(all(c.end for c in e.devices.values()))

    def test_release_and_keep_only_come_with_finishing_an_active_entry(self) -> None:
        w = self.world()
        self.assertEqual(w.code([finish(OTHER, "playing", keep=["deck"])]), "bad-request")
        w.apply([finish(OTHER, release=["deck", "machine"])])
        self.assertEqual(w.code([finish(OTHER, "dropped", release=["deck"])]), "bad-request")

    def test_an_unknown_entry_is_not_found(self) -> None:
        self.assertEqual(self.world().code([finish(MGS)]), "not-found")

    def test_a_missing_game_can_only_be_kept(self) -> None:
        w = self.world()
        first = w.queue.entries[0]
        lost = replace(first, game=replace(first.game, id=GONE, key="zz"))
        w.set(replace(w.queue, entries=(lost, *w.queue.entries[1:])))
        self.assertEqual(w.code([finish(GONE, release=["deck", "machine"])]), "not-held")
        w.apply([finish(GONE, keep=["deck", "machine"])])
        self.assertEqual(w.entries()[GONE].state, "finished")


class PlanDevicesTests(unittest.TestCase):
    def world(self) -> World:
        w = World()
        w.apply([add(OTHER, ["deck"])])
        return w

    def test_a_console_can_be_added_and_taken_off(self) -> None:
        w = self.world()
        op = {"op": "devices", "game": OTHER, "add": ["machine"], "release": ["deck"], "keep": []}
        plan = w.apply([op])
        claims = w.entries()[OTHER].devices
        self.assertEqual((claims["deck"].end, claims["machine"].end), ("released", None))
        self.assertEqual(
            {d.device: (d.turned_on, d.turned_off) for d in plan.devices},
            {"machine": ((OTHER,), ()), "deck": ((), (OTHER,))},
        )

    def test_an_ended_claim_is_replaced_by_a_new_one(self) -> None:
        w = self.world()
        w.apply([{"op": "devices", "game": OTHER, "release": ["deck"]}])
        w.apply([{"op": "devices", "game": OTHER, "add": ["deck"]}])
        self.assertEqual(w.entries()[OTHER].devices["deck"], q.Claim("off", STAMP))

    def test_refusals(self) -> None:
        w = self.world()

        def devices(**kw: Any) -> list[dict[str, Any]]:
            return [{"op": "devices", "game": OTHER, **kw}]

        self.assertEqual(w.code(devices(add=["deck"])), "bad-request")
        self.assertEqual(w.code(devices(add=["deck"], release=["deck"])), "bad-request")
        self.assertEqual(w.code(devices(release=["machine"])), "not-held")
        self.assertEqual(w.code([{"op": "devices", "game": PS4, "add": ["deck"]}]), "not-found")
        w.apply([add(PS4)])
        self.assertEqual(
            w.code([{"op": "devices", "game": PS4, "add": ["machine"]}]), "unsupported"
        )
        w.apply([finish(OTHER, "dropped", release=["deck"])])
        self.assertEqual(w.code(devices(add=["machine"])), "bad-request")


class PlanMoveTests(unittest.TestCase):
    def test_moving_an_entry(self) -> None:
        w = World()
        w.apply([add(OTHER), add(MGS), add(TINY)])
        w.apply([{"op": "move", "game": TINY, "position": 0}])
        self.assertEqual([e.game.id for e in w.queue.entries], [TINY, OTHER, MGS])
        w.apply([{"op": "move", "game": TINY, "position": 2}])
        self.assertEqual([e.game.id for e in w.queue.entries], [OTHER, MGS, TINY])

    def test_ended_entries_in_between_do_not_count(self) -> None:
        w = World()
        w.apply([add(OTHER), add(MGS), add(TINY)])
        w.apply([finish(MGS, "dropped")])
        w.apply([{"op": "move", "game": TINY, "position": 1}])
        self.assertEqual([e.game.id for e in w.queue.entries if e.state == "queued"], [OTHER, TINY])

    def test_refusals(self) -> None:
        w = World()
        w.apply([add(OTHER), add(MGS)])
        self.assertEqual(w.code([{"op": "move", "game": OTHER, "position": 2}]), "bad-request")
        self.assertEqual(w.code([{"op": "move", "game": TINY, "position": 0}]), "not-found")
        w.apply([finish(MGS, "dropped")])
        self.assertEqual(w.code([{"op": "move", "game": MGS, "position": 0}]), "bad-request")


class PlanRemoveTests(unittest.TestCase):
    def test_an_entry_with_nothing_released_is_deleted_at_once(self) -> None:
        w = World()
        w.apply([add(OTHER, ["deck"])])
        plan = w.apply([remove(OTHER, keep=["deck"])])
        self.assertEqual(w.queue.entries, ())
        self.assertIsNone(plan.second)

    def test_releasing_keeps_the_entry_until_a_second_write(self) -> None:
        w = World()
        w.apply([add(OTHER, ["deck", "machine"]), add(MGS)])
        plan = w.plan([remove(OTHER, release=["deck"], keep=["machine"])])
        assert plan.first is not None and plan.second is not None
        first, second = q.parse(plan.first).queue, q.parse(plan.second).queue
        kept = next(e for e in first.entries if e.game.id == OTHER)
        self.assertEqual(kept.devices["deck"].end, "released")
        self.assertEqual(kept.state, "queued")
        self.assertEqual([e.game.id for e in second.entries], [MGS])
        self.assertEqual(plan.queue, second)
        self.assertEqual(plan.final, plan.second)

    def test_removing_needs_every_held_console_named(self) -> None:
        w = World()
        w.apply([add(OTHER, ["deck"])])
        self.assertEqual(w.code([remove(OTHER)]), "bad-request")

    def test_removing_an_ended_entry_and_one_with_no_consoles(self) -> None:
        w = World()
        w.apply([add(OTHER), add(MGS)])
        w.apply([finish(MGS, "dropped")])
        w.apply([remove(MGS), remove(OTHER)])
        self.assertEqual(w.queue.entries, ())

    def test_a_game_removed_in_a_request_is_not_usable_again_in_it(self) -> None:
        w = World()
        w.apply([add(OTHER, ["deck"])])
        self.assertEqual(w.code([remove(OTHER, release=["deck"]), add(OTHER)]), "bad-request")


class PlanSyncTests(unittest.TestCase):
    def sync(self, game_id: str, device: str) -> list[dict[str, Any]]:
        return [{"op": "sync", "game": game_id, "device": device}]

    def test_not_on_turns_the_game_on_again(self) -> None:
        w = World()
        w.apply([add(OTHER, ["deck"])])
        before = w.queue
        w.views["deck"] = view(managed(["gba"]))  # the user switched it off
        plan = w.apply(self.sync(OTHER, "deck"))
        self.assertEqual(plan.devices[0].turned_on, (OTHER,))
        self.assertIsNone(plan.first)  # the queue does not change
        self.assertEqual(w.queue, before)

    def test_not_released_turns_it_off_again(self) -> None:
        w = World()
        w.apply([add(OTHER, ["deck"])])
        w.apply([finish(OTHER, "dropped", release=["deck"])])
        model = w.views["deck"].model
        w.views["deck"] = view(managed(model.systems, [*model.includes, OTHER_PATH]))
        plan = w.apply(self.sync(OTHER, "deck"))
        self.assertEqual(plan.devices[0].turned_off, (OTHER,))
        self.assertEqual(w.entries()[OTHER].devices["deck"].end, "released")

    def test_not_released_but_covered_ends_kept(self) -> None:
        w = World()
        w.apply([add(OTHER, ["deck"])])
        w.apply([finish(OTHER, "dropped", release=["deck"])])
        w.views["deck"] = view(managed(["psx"]))
        plan = w.apply(self.sync(OTHER, "deck"))
        self.assertEqual([s["reason"] for s in plan.skipped], ["covered"])
        self.assertEqual(w.entries()[OTHER].devices["deck"].end, "kept")

    def test_in_sync_and_unknown_claims(self) -> None:
        w = World()
        w.apply([add(OTHER, ["deck"])])
        self.assertEqual(w.code(self.sync(OTHER, "deck")), "in-sync")
        self.assertEqual(w.code(self.sync(OTHER, "machine")), "not-held")
        self.assertEqual(w.code(self.sync(MGS, "deck")), "not-found")
        self.assertEqual(w.code(self.sync(OTHER, "tablet")), "not-found")


class PlanRelinkTests(unittest.TestCase):
    def moved(self) -> World:
        w = World()
        w.apply([add(OTHER, ["deck", "machine"]), add(MGS)])
        w.apply([finish(OTHER, "playing")])
        first = w.queue.entries[0]
        gone = replace(first.game, id=GONE, key=ref(MGS).key, system="psx")
        w.set(replace(w.queue, entries=(replace(first, game=gone), *w.queue.entries[1:])))
        return w

    @staticmethod
    def relink(to: str) -> list[dict[str, Any]]:
        return [{"op": "relink", "game": GONE, "to": to}]

    def test_relinking_keeps_state_dates_and_order_and_ends_open_claims(self) -> None:
        w = self.moved()
        w.apply(self.relink(MGS_GBC))
        e = w.queue.entries[0]
        self.assertEqual((e.game, e.state, e.added), (ref(MGS_GBC), "playing", STAMP))
        self.assertEqual({c.end for c in e.devices.values()}, {"kept"})

    def test_refusals(self) -> None:
        w = self.moved()
        self.assertEqual(w.code(self.relink(MGS)), "already-queued")  # in the queue
        self.assertEqual(w.code(self.relink("1" * 16)), "not-found")
        w.apply([finish(MGS, "dropped")])
        self.assertEqual(w.code(self.relink(MGS)), "already-queued")  # ended entries count too
        present = World()
        present.apply([add(OTHER)])
        self.assertEqual(present.code([{"op": "relink", "game": OTHER, "to": MGS}]), "bad-request")


class PlanDismissTests(unittest.TestCase):
    def world(self, date: str) -> World:
        w = World()
        w.apply([add(OTHER)])
        award = {"kind": "beaten-hardcore", "date": date}
        patch = unittest.mock.patch.object(api, "game_award", lambda lib, cfg, s, g: award)
        patch.start()
        self.addCleanup(patch.stop)
        return w

    def test_dismissing_stores_the_awards_date(self) -> None:
        w = self.world("2026-11-10T21:00:00+00:00")
        w.apply([{"op": "dismiss", "game": OTHER}])
        self.assertEqual(w.entries()[OTHER].ra_dismissed, "2026-11-10T21:00:00Z")
        self.assertEqual(w.code([{"op": "dismiss", "game": OTHER}]), "bad-request")

    def test_nothing_to_dismiss(self) -> None:
        w = self.world("2026-11-09T21:00:00+00:00")  # before it was queued
        self.assertEqual(w.code([{"op": "dismiss", "game": OTHER}]), "bad-request")
        self.assertEqual(w.code([{"op": "dismiss", "game": MGS}]), "not-found")


class PlanSizeTests(unittest.TestCase):
    def test_a_write_over_the_limit_is_refused(self) -> None:
        w = World()
        big = q.Group("default", "x" * (q.SIZE_LIMIT - 200), STAMP, ())
        w.set(q.Queue(STAMP, (), {"aaaaaaaa": big}))  # a collection title fills the file
        with self.assertRaises(ApiError) as caught:
            w.plan([add(OTHER), add(MGS)])
        self.assertEqual((caught.exception.code, caught.exception.status), ("queue-too-large", 409))
        self.assertTrue(caught.exception.queue)

    def test_a_write_over_the_limit_that_does_not_grow_the_file_is_accepted(self) -> None:
        w = World()
        w.apply([add(OTHER), add(MGS)])
        w.size = q.SIZE_LIMIT + 1000  # a hand-made file past the limit
        plan = w.plan([remove(MGS)])
        assert plan.first is not None
        self.assertLess(len(plan.first), w.size)


class OtherOwnersTests(unittest.TestCase):
    def test_entries_of_another_owner_are_kept_byte_for_byte_and_never_shown(self) -> None:
        w = World()
        theirs = entry(
            MGS, owner="someone-else", devices={"deck": q.Claim("off", STAMP)}, ra_dismissed=STAMP
        )
        done = entry(TINY, "finished", owner="someone-else")
        w.set(q.Queue(STAMP, (theirs, entry(OTHER), done)))

        def raw(queue: q.Queue) -> list[str]:
            return [json.dumps(e, sort_keys=True) for e in json.loads(q.render(queue))["queue"]]

        before = raw(w.queue)
        w.apply([add(EXAMPLE), remove(OTHER)])
        after = raw(w.queue)
        self.assertEqual((after[0], after[-1]), (before[0], before[2]))
        self.assertEqual(w.code([{"op": "move", "game": MGS, "position": 0}]), "not-found")
        self.assertEqual(w.code([finish(TINY, "dropped")]), "not-found")
        body = q.queue_response(w.reading, w.queue, w.etag, w.size, w.context(), 0)
        self.assertEqual([e["id"] for e in body["entries"]], [EXAMPLE])
        self.assertEqual(body["history_total"], 0)

    def test_the_same_game_can_be_queued_by_each_owner(self) -> None:
        w = World()
        w.set(q.Queue(STAMP, (entry(OTHER, owner="someone-else"),)))
        w.apply([add(OTHER)])
        self.assertEqual(len(w.queue.entries), 2)

    def test_the_active_limit_counts_only_the_owners_entries(self) -> None:
        w = World()
        theirs = tuple(
            entry(OTHER, owner="someone-else", game=replace(ref(OTHER), id=f"{n:016x}"))
            for n in range(q.MAX_ACTIVE)
        )
        w.set(q.Queue(STAMP, theirs))
        w.plan([add(OTHER)])


class ResponseTests(unittest.TestCase):
    def body(self, w: World, now: int = 0, history: int = 50) -> dict[str, Any]:
        return q.queue_response(w.reading, w.queue, w.etag, w.size, w.context(), now, history)

    def test_the_shape_of_an_entry(self) -> None:
        w = World()
        w.apply([add(OTHER, ["deck"]), add(EXAMPLE, ["deck", "machine"])])
        body = self.body(w)
        self.assertEqual(
            (body["state"], body["etag"], body["limit"], body["history"], body["history_total"]),
            ("ok", w.etag, q.SIZE_LIMIT, [], 0),
        )
        first, second = body["entries"]
        self.assertEqual(
            first,
            {
                "id": OTHER,
                "system": "psx",
                "title": "Other Game",
                "name": "Other Game",
                "cover": False,
                "size": 1 << 30,
                "found": "present",
                "candidates": [],
                "state": "queued",
                "shown": "queued",
                "inferred": False,
                "suggestion": None,
                "added": STAMP,
                "changed": STAMP,
                "ended": None,
                "play": None,
                "devices": {"deck": {"before": "off", "end": None, "game": "on", "view": "on"}},
            },
        )
        self.assertEqual(
            second["devices"],
            {
                "deck": {"before": "on", "end": None, "game": "on", "view": "kept"},
                "machine": {"before": "off", "end": None, "game": "on", "view": "on"},
            },
        )

    def test_views_follow_the_whitelists(self) -> None:
        w = World()
        w.apply([add(OTHER, ["deck"])])
        w.views["deck"] = view(managed(["gba"]))
        self.assertEqual(self.body(w)["entries"][0]["devices"]["deck"]["view"], "not-on")
        w.views["deck"] = view("tmp/*.txt\n")
        self.assertEqual(self.body(w)["entries"][0]["devices"]["deck"]["view"], "unknown")

    def test_a_moved_and_a_missing_entry(self) -> None:
        w = World()
        gone = replace(ref(MGS), id=GONE)
        lost = replace(ref(OTHER), id="1" * 16, key="nothing like it")
        w.set(q.Queue(STAMP, (entry(game=gone), entry(game=lost))))
        moved, missing = self.body(w)["entries"]
        self.assertEqual(moved["found"], "moved")
        self.assertEqual([c["id"] for c in moved["candidates"]], [MGS, MGS_GBC])
        self.assertEqual(moved["title"], gone.title)
        self.assertNotIn("name", moved)
        self.assertNotIn("size", moved)
        self.assertEqual((missing["found"], missing["candidates"]), ("missing", []))

    def test_history_is_newest_first_and_limited(self) -> None:
        w = World()
        ended = [
            entry(OTHER, "finished", ended="2026-11-02T00:00:00Z"),
            entry(MGS, "dropped", ended="2026-11-03T00:00:00Z"),
            entry(TINY, "finished", ended="2026-11-01T00:00:00Z"),
        ]
        w.set(q.Queue(STAMP, tuple(ended)))
        body = self.body(w, history=2)
        self.assertEqual([e["id"] for e in body["history"]], [MGS, OTHER])
        self.assertEqual(body["history_total"], 3)
        self.assertEqual(body["entries"], [])
        self.assertEqual(self.body(w, history=0)["history"], [])

    def test_a_damaged_or_absent_queue(self) -> None:
        w = World()
        body = self.body(w)
        self.assertEqual(
            (body["state"], body["etag"], body["updated"], body["entries"]),
            ("absent", "absent", None, []),
        )
        reading = q.Reading("damaged", q.Queue(), "the file is not UTF-8 JSON")
        body = q.queue_response(reading, reading.queue, "damaged", 7, w.context(), 0)
        self.assertEqual(
            (body["state"], body["message"], body["entries"], body["size"]),
            ("damaged", f"queue.json: {reading.message}", [], 7),
        )

    def test_collections_are_listed_by_title(self) -> None:
        w = World()
        group = q.Group("default", "Couch co-op", STAMP, (ref(OTHER),))
        late = q.Group("default", "Zed", STAMP, ())
        w.set(q.Queue(STAMP, (), {"aaaaaaaa": late, "3f2a9c1e": group}))
        self.assertEqual(
            self.body(w)["collections"],
            [
                {"id": "3f2a9c1e", "title": "Couch co-op", "games": 1},
                {"id": "aaaaaaaa", "title": "Zed", "games": 0},
            ],
        )

    def test_the_inferred_state_and_the_suggestion_reach_the_answer(self) -> None:
        w = World()
        w.apply([add(OTHER)])
        now = int(NOW.timestamp()) + 3600
        totals = {"plays": 1, "last": now - 10, "fav": None, "playtime": 5}
        award = {"kind": "mastered", "date": "2026-11-10T20:30:00+00:00"}
        with (
            unittest.mock.patch.object(api, "game_totals", lambda index, s, g: totals),
            unittest.mock.patch.object(api, "game_award", lambda lib, cfg, s, g: award),
        ):
            body = self.body(w, now)
        found = body["entries"][0]
        self.assertEqual(
            (found["shown"], found["inferred"], found["state"]), ("playing", True, "queued")
        )
        self.assertEqual(found["suggestion"]["award"], "mastered")
        self.assertEqual(found["play"]["plays"], 1)

    def test_the_plan_answer_shows_the_queue_after_the_change(self) -> None:
        w = World()
        plan = w.plan([add(OTHER, ["deck"])], dry_run=True)
        body = q.plan_response(plan, w.etag, w.size, w.context(), 0)
        self.assertTrue(body["dry_run"])
        self.assertEqual(body["queue"]["entries"][0]["devices"]["deck"]["view"], "on")
        self.assertEqual(body["queue"]["etag"], q.etag_of(plan.first))
        self.assertEqual(body["queue"]["size"], len(plan.first or b""))
        (dev,) = body["devices"]
        self.assertEqual((dev["device"], dev["written"], dev["on"]), ("deck", False, [OTHER]))
        self.assertEqual(body["skipped"], [])


if __name__ == "__main__":
    unittest.main()
