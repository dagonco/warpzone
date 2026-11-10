"""The library API's pure functions: validation, `plan_changes` and the answers' shapes."""

import unittest
from typing import Any

from library_support import GIB, config, games, library, managed
from library_support import game as make_game
from warpzone import artwork, catalog, ra
from warpzone import whitelist as wl
from warpzone.library_api import (
    ApiError,
    DeviceView,
    RangeNotSatisfiable,
    SystemExtras,
    api_etag,
    changes_response,
    devices_response,
    game_facts,
    game_media,
    game_meta,
    game_response,
    games_response,
    media_file,
    media_index,
    name_tags,
    parse_range,
    plan_changes,
    safe_parts,
    search_response,
    systems_response,
    view_of,
)

CFG = config()
LIB = library()


def by_title(system: str, title: str) -> catalog.Game:
    return next(g for s, g in games() if s == system and g.title == title)


EXAMPLE = by_title("psx", "Example Game (Europe)")
OTHER = by_title("psx", "Other Game")
PS4 = by_title("ps4", "Example PS4")
BAD = by_title("psx", "Bad Name")


def views(deck: str | None = None, machine: str | None = None) -> dict[str, DeviceView]:
    return {
        "deck": view_of(None if deck is None else deck.encode()),
        "machine": view_of(None if machine is None else machine.encode()),
    }


def change(device: str, system: str, on: bool, game: catalog.Game | None = None) -> dict[str, Any]:
    item: dict[str, Any] = {"device": device, "system": system, "on": on}
    if game is not None:
        item["game"] = game.id
    return item


def body(changes: list[dict[str, Any]], v: dict[str, DeviceView], **extra: Any) -> dict[str, Any]:
    names = {c["device"] for c in changes}
    expect = {n: v[n].etag for n in (v if "*" in names else names) if n in v}
    return {"changes": changes, "expect": expect, **extra}


def error_of(request: object, v: dict[str, DeviceView]) -> ApiError:
    with unittest.TestCase().assertRaises(ApiError) as caught:
        plan_changes(request, CFG, LIB, v)
    return caught.exception


class ViewTests(unittest.TestCase):
    def test_missing_managed_and_hand_written(self) -> None:
        self.assertEqual(view_of(None).state, "missing")
        self.assertEqual(view_of(None).etag, "absent")
        self.assertEqual(view_of(managed(["gba"]).encode()).state, "managed")
        hand = view_of(b"#include patterns-common.stignore\n!/roms/gba\n*\n")
        self.assertEqual(hand.state, "needs-migration")
        self.assertIsNotNone(hand.model)  # shown read-only, evaluated by the migration's parser

    def test_a_file_nothing_can_read_has_no_model(self) -> None:
        for data in (b"\xff\xfe", b"!/roms/*\n*\n"):
            found = view_of(data)
            self.assertEqual((found.state, found.model), ("needs-migration", None))

    def test_the_etag_has_the_api_form(self) -> None:
        data = managed(["gba"]).encode()
        self.assertEqual(view_of(data).etag, api_etag(wl.etag(data)))
        self.assertTrue(view_of(data).etag.startswith("sha256:"))


class PathTests(unittest.TestCase):
    def test_safe_parts(self) -> None:
        self.assertEqual(safe_parts("/"), ())
        self.assertEqual(safe_parts("/app.js"), ("app.js",))
        self.assertEqual(safe_parts("/a/b.css"), ("a", "b.css"))
        self.assertEqual(safe_parts("/a/"), ("a",))

    def test_unsafe_paths(self) -> None:
        for path in (
            "",
            "etc/passwd",
            "//etc/passwd",
            "/a//b",
            "/../x",
            "/a/../x",
            "/./x",
            "/.hidden",
            "/a\\b",
            "/a\0b",
        ):
            self.assertIsNone(safe_parts(path), path)


class ReadAnswerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.views = views(managed(["gba"], includes=[EXAMPLE.primary]))

    def test_devices(self) -> None:
        found = devices_response(CFG, self.views)
        self.assertEqual([d["name"] for d in found], ["deck", "machine"])
        self.assertEqual(found[1]["exclude_systems"], ["ps4"])
        self.assertEqual(found[0]["whitelist"]["state"], "managed")
        self.assertEqual(found[1]["whitelist"], {"state": "missing", "etag": "absent"})

    def test_systems(self) -> None:
        found = {s["system"]: s for s in systems_response(LIB, CFG, self.views)}
        self.assertEqual(sorted(found), ["gba", "gbc", "ps4", "psx"])
        self.assertEqual(found["gba"]["devices"]["deck"], {"state": "all", "whole": True})
        self.assertEqual(found["psx"]["devices"]["deck"], {"state": "some", "whole": False})
        self.assertEqual(found["psx"]["devices"]["machine"], {"state": "none", "whole": False})
        self.assertEqual(found["ps4"]["devices"]["machine"]["state"], "unsupported")
        self.assertEqual(found["psx"]["games"], 4)
        self.assertEqual(found["gba"]["size"], 4000)

    def test_games_are_compact_and_sorted(self) -> None:
        found = games_response(LIB, "psx", CFG, self.views, lambda s: SystemExtras())
        self.assertEqual(found["system"], "psx")
        self.assertEqual(
            [g["title"] for g in found["games"]][:2], ["Bad Name", "Example Game (Europe)"]
        )
        item = next(g for g in found["games"] if g["id"] == EXAMPLE.id)
        self.assertEqual(
            item,
            {
                "id": EXAMPLE.id,
                "title": "Example Game (Europe)",
                "name": "Example Game (Europe)",
                "size": 5 * GIB,
                "added": "2026-10-28",
                "cover": False,
                "ra": "no-list",
                "flags": [],
                "devices": {"deck": "on", "machine": "off"},
                "region": ["Europe"],
                "languages": [],
            },
        )
        self.assertEqual(found["unclaimed"], [])

    def test_a_scraped_name_and_cover(self) -> None:
        entry = artwork.Entry("game", EXAMPLE.gamelist, {"name": "Example Game", "desc": "d"})
        extras = SystemExtras(
            {EXAMPLE.gamelist: entry}, {"Example Game (Europe)": {"covers": "x.png"}}
        )
        found = games_response(LIB, "psx", CFG, self.views, lambda s: extras)
        item = next(g for g in found["games"] if g["id"] == EXAMPLE.id)
        self.assertEqual((item["name"], item["cover"]), ("Example Game", True))

    def test_unknown_system_and_game(self) -> None:
        for call in (
            lambda: games_response(LIB, "nope", CFG, self.views, lambda s: SystemExtras()),
            lambda: game_response(LIB, "0" * 16, CFG, self.views, lambda s: SystemExtras()),
        ):
            with self.assertRaises(ApiError) as caught:
                call()
            self.assertEqual((caught.exception.code, caught.exception.status), ("not-found", 404))

    def test_one_game_in_full(self) -> None:
        entry = artwork.Entry(
            "game", PS4.gamelist, {"name": "N", "desc": "D", "path": "x", "junk": "j"}
        )
        extras = SystemExtras(
            {PS4.gamelist: entry},
            {"shortcuts/Example PS4": {"covers": "a.png", "marquees": "b.jpg"}},
        )
        found = game_response(LIB, PS4.id, CFG, self.views, lambda s: extras)
        self.assertEqual(found["system"], "ps4")
        self.assertEqual(found["parts"][0]["path"], "roms/ps4/games/CUSA00000")
        self.assertEqual(found["meta"], {"name": "N", "desc": "D"})
        self.assertEqual(found["media"], ["covers", "marquees"])
        self.assertEqual(
            found["ra"], {"state": "no-list", "achievements": 0, "points": 0, "url": None}
        )
        self.assertEqual(found["devices"], {"deck": "off", "machine": "unsupported"})
        self.assertEqual(found["versions"], [])

    def test_versions_are_the_other_games_with_the_same_title_key(self) -> None:
        mgs = by_title("psx", "Metal Gear Solid (USA)")
        found = game_response(LIB, mgs.id, CFG, self.views, lambda s: SystemExtras())
        self.assertEqual(
            [(v["system"], v["title"]) for v in found["versions"]],
            [("gbc", "Metal Gear Solid (USA)")],
        )

    def test_search_groups_by_key(self) -> None:
        found = search_response(LIB, CFG, self.views, "metal gear")
        self.assertEqual(found["query"], "metal gear")
        (group,) = found["groups"]
        self.assertEqual(group["key"], "metal gear solid")
        self.assertEqual([g["system"] for g in group["games"]], ["gbc", "psx"])
        self.assertEqual(group["games"][0]["devices"], {"deck": "off", "machine": "off"})
        self.assertEqual(search_response(LIB, CFG, self.views, "  ")["groups"], [])

    def test_the_ra_state_comes_from_the_cached_list(self) -> None:
        hash_ = "a" * 32
        game = catalog.Game(
            **{**EXAMPLE.__dict__, "ra": {"hash": hash_, "of": "x", "size": 1, "mtime": 1}}
        )
        ra_list = catalog.RaList(12, "2026-11-06T00:00:00Z", {hash_: ra.RaGame("T", 10, 99, 50)})
        lib = library([("psx", game)])
        lib = type(lib)(lib.systems, {12: ra_list})
        found = game_response(lib, game.id, CFG, self.views, lambda s: SystemExtras())
        self.assertEqual(found["ra"]["state"], "supported")
        self.assertEqual(found["ra"]["url"], "https://retroachievements.org/game/99")

    def test_media_file_errors(self) -> None:
        extras = SystemExtras(
            media={"Example Game (Europe)": {"covers": "Example Game (Europe).png"}}
        )
        self.assertEqual(
            media_file(LIB, EXAMPLE.id, "covers", lambda s: extras),
            ("psx", "Example Game (Europe).png"),
        )
        for game_id, kind in ((EXAMPLE.id, "marquees"), (EXAMPLE.id, "nope"), ("0" * 16, "covers")):
            with self.assertRaises(ApiError) as caught:
                media_file(LIB, game_id, kind, lambda s: extras)
            self.assertEqual(caught.exception.code, "not-found")

    def test_a_folder_entry_keeps_its_whole_name_for_the_media_stem(self) -> None:
        m3u = by_title("psx", "Other Game")
        entry = artwork.Entry("folder", "./Other Game.chd", {})
        extras = SystemExtras(
            {m3u.gamelist: entry}, {"Other Game.chd": {"covers": "Other Game.chd.png"}}
        )
        self.assertEqual(
            media_file(LIB, m3u.id, "covers", lambda s: extras)[1], "Other Game.chd.png"
        )

    def test_a_system_read_from_a_subfolder_finds_its_entry_and_media(self) -> None:
        # ES-DE lists PS4 from `roms/ps4/shortcuts/`, so its gamelist and media say
        # `./Example PS4.desktop` while the catalog says `./shortcuts/Example PS4.desktop`.
        entry = artwork.Entry("game", "./Example PS4.desktop", {"name": "N", "desc": "D"})
        extras = SystemExtras({entry.path: entry}, {"Example PS4": {"covers": "Example PS4.png"}})
        found = game_response(LIB, PS4.id, CFG, self.views, lambda s: extras)
        self.assertEqual(found["meta"], {"name": "N", "desc": "D"})
        self.assertEqual(found["media"], ["covers"])
        listed = games_response(LIB, "ps4", CFG, self.views, lambda s: extras)
        item = next(g for g in listed["games"] if g["id"] == PS4.id)
        self.assertEqual((item["name"], item["cover"]), ("N", True))
        self.assertEqual(
            media_file(LIB, PS4.id, "covers", lambda s: extras), ("ps4", "Example PS4.png")
        )

    def test_the_catalogs_own_path_wins_over_the_shorter_one(self) -> None:
        full = artwork.Entry("game", PS4.gamelist, {"name": "Full"})
        short = artwork.Entry("game", "./Example PS4.desktop", {"name": "Short"})
        extras = SystemExtras(
            {full.path: full, short.path: short},
            {"shortcuts/Example PS4": {"covers": "a.png"}, "Example PS4": {"covers": "b.png"}},
        )
        self.assertEqual(game_meta(PS4, extras), {"name": "Full"})
        self.assertEqual(game_media(PS4, extras), {"covers": "a.png"})

    def test_media_without_a_gamelist_entry_is_found_below_the_subfolder_too(self) -> None:
        extras = SystemExtras(media={"Example PS4": {"covers": "Example PS4.png"}})
        self.assertEqual(game_media(PS4, extras), {"covers": "Example PS4.png"})

    def test_a_folder_entry_of_a_subfolder_system_keeps_its_whole_name(self) -> None:
        game = catalog.Game(**{**PS4.__dict__, "gamelist": "./carts/Example.m3u"})
        entry = artwork.Entry("folder", "./Example.m3u", {})
        extras = SystemExtras({entry.path: entry}, {"Example.m3u": {"covers": "c.png"}})
        self.assertEqual(game_media(game, extras), {"covers": "c.png"})

    def test_media_index_prefers_png_and_ignores_other_files(self) -> None:
        found = media_index(
            {
                "covers": ["A.jpg", "A.png", "B.gif", "sub/C.JPEG", "noext", ".png"],
                "marquees": ["A.jpg"],
                "physicalmedia": ["A.png", "sub/C.png"],
                "other": ["A.png"],
            }
        )
        self.assertEqual(
            found["A"], {"covers": "A.png", "marquees": "A.jpg", "physicalmedia": "A.png"}
        )
        self.assertEqual(found["sub/C"], {"covers": "sub/C.JPEG", "physicalmedia": "sub/C.png"})
        self.assertNotIn("B", found)


class MediaKindTests(unittest.TestCase):
    def test_videos_are_mp4_or_webm_and_images_are_not_videos(self) -> None:
        found = media_index(
            {
                "videos": ["A.webm", "A.mp4", "B.png", "C.mkv", "sub/D.MP4"],
                "backcovers": ["A.png", "B.mp4"],
                "titlescreens": ["A.jpg"],
            }
        )
        self.assertEqual(
            found["A"], {"videos": "A.mp4", "backcovers": "A.png", "titlescreens": "A.jpg"}
        )
        self.assertEqual(found["sub/D"], {"videos": "sub/D.MP4"})
        self.assertNotIn("B", found)
        self.assertNotIn("C", found)


class RangeTests(unittest.TestCase):
    def test_the_three_forms(self) -> None:
        self.assertEqual(parse_range("bytes=0-99", 1000), (0, 99))
        self.assertEqual(parse_range("bytes=500-", 1000), (500, 999))
        self.assertEqual(parse_range("bytes=-100", 1000), (900, 999))
        self.assertEqual(parse_range("bytes=0-0", 1000), (0, 0))
        self.assertEqual(parse_range(" Bytes = 10 - 19 ", 1000), (10, 19))

    def test_the_end_is_cut_to_the_file(self) -> None:
        self.assertEqual(parse_range("bytes=900-5000", 1000), (900, 999))
        self.assertEqual(parse_range("bytes=-5000", 1000), (0, 999))

    def test_a_range_past_the_end_is_not_satisfiable(self) -> None:
        for header, size in (
            ("bytes=1000-", 1000),
            ("bytes=1000-1999", 1000),
            ("bytes=-0", 1000),
            ("bytes=0-", 0),
            ("bytes=-5", 0),
        ):
            with self.assertRaises(RangeNotSatisfiable, msg=header):
                parse_range(header, size)

    def test_what_cannot_be_served_as_a_range_is_ignored(self) -> None:
        for header in (
            None,
            "",
            "bytes=0-1,5-9",
            "items=0-1",
            "bytes=",
            "bytes=-",
            "bytes=a-b",
            "bytes=9-1",
            "bytes=1",
            "bytes=-1-2",
            "bytes=+1-2",
            "0-1",
        ):
            self.assertIsNone(parse_range(header, 1000), header)


class NameTagTests(unittest.TestCase):
    def test_regions_and_languages(self) -> None:
        self.assertEqual(
            name_tags("Example Game (Europe) (En,Fr,De,Es)"),
            (["Europe"], ["en", "fr", "de", "es"]),
        )
        self.assertEqual(
            name_tags("Example (USA, Europe) (Rev 1) (En,Ja)"), (["USA", "Europe"], ["en", "ja"])
        )
        self.assertEqual(name_tags("Example (Japan) (En)"), (["Japan"], ["en"]))
        self.assertEqual(name_tags("Example (Hong Kong) (Zh-Hant)"), (["Hong Kong"], ["zh-Hant"]))

    def test_other_tags_are_not_regions_or_languages(self) -> None:
        for name in (
            "Example Game",
            "Example (Rev 1) (Beta) (Disc 2) (Proto)",
            "Example (Europe-ish) (Hack) (Eng)",
            "Example [!] (Unl)",
        ):
            self.assertEqual(name_tags(name), ([], []), name)

    def test_the_game_facts_use_the_dat_name_first_and_verified_only_when_recorded(self) -> None:
        plain = by_title("psx", "Example Game (Europe)")
        self.assertEqual(game_facts(plain), {"region": ["Europe"], "languages": []})
        checked = catalog.Game(**{**plain.__dict__, "dat": "Example Game (USA) (En,Fr)"})
        self.assertEqual(
            game_facts(checked),
            {"region": ["USA"], "languages": ["en", "fr"], "verified": True},
        )

    def test_the_answers_carry_the_facts(self) -> None:
        views_ = {"deck": view_of(None), "machine": view_of(None)}
        listed = games_response(LIB, "psx", CFG, views_, lambda s: SystemExtras())
        entry = next(g for g in listed["games"] if g["id"] == EXAMPLE.id)
        self.assertEqual(entry["region"], ["Europe"])
        self.assertEqual(entry["languages"], [])
        self.assertNotIn("verified", entry)
        one = game_response(LIB, EXAMPLE.id, CFG, views_, lambda s: SystemExtras())
        self.assertEqual(one["region"], ["Europe"])
        self.assertNotIn("verified", one)


class ShapeTests(unittest.TestCase):
    def test_bad_requests(self) -> None:
        v = views()
        good = {"device": "deck", "system": "gba", "on": True}
        for request in (
            [],
            "x",
            {},
            {"changes": []},
            {"changes": [good], "extra": 1},
            {"changes": ["x"]},
            {"changes": [{**good, "extra": 1}]},
            {"changes": [{**good, "device": 1}]},
            {"changes": [{**good, "system": ""}]},
            {"changes": [{**good, "game": 5}]},
            {"changes": [{**good, "on": "yes"}]},
            {"changes": [{"device": "deck", "system": "gba"}]},
            {"changes": [good], "expect": []},
            {"changes": [good], "expect": {"deck": 1}},
            {"changes": [good], "dry_run": "no"},
            {"changes": [good] * 1001},
        ):
            found = error_of(request, v)
            self.assertEqual((found.code, found.status), ("bad-request", 400), request)


class PlanTests(unittest.TestCase):
    def test_unknown_things_are_not_found(self) -> None:
        v = views()
        cases = [
            (change("tv", "gba", True), "device"),
            (change("deck", "nope", True), None),
            ({**change("deck", "gba", True), "game": "0" * 16}, "game"),
            # a game must belong to the system named with it
            ({**change("deck", "gba", True), "game": EXAMPLE.id}, "game"),
        ]
        for item, field_name in cases:
            found = error_of(body([item], v), v)
            self.assertEqual((found.code, found.status), ("not-found", 404), item)
            if field_name:
                self.assertIsNotNone(getattr(found, field_name))

    def test_a_device_that_is_not_configured_is_not_found(self) -> None:
        v = views()
        data = {"changes": [change("nope", "gba", True)], "expect": {}}
        self.assertEqual(error_of(data, v).code, "not-found")

    def test_not_managed_comes_before_expect(self) -> None:
        v = views(deck="!/roms/gba\n*\n")
        found = error_of({"changes": [change("deck", "gba", True)]}, v)
        self.assertEqual((found.code, found.status, found.device), ("not-managed", 409, "deck"))

    def test_a_missing_whitelist_is_created(self) -> None:
        v = views()
        plan = plan_changes(body([change("deck", "gba", True)], v), CFG, LIB, v)
        (dp,) = plan.devices
        self.assertEqual((dp.etag, dp.changed), ("absent", True))
        self.assertEqual(wl.parse(dp.new_text).systems, frozenset({"gba"}))

    def test_expect_missing_and_stale(self) -> None:
        v = views(managed(["gba"]))
        found = error_of({"changes": [change("deck", "gba", False)], "expect": {}}, v)
        self.assertEqual((found.code, found.status), ("expect-missing", 428))
        found = error_of(
            {"changes": [change("deck", "gba", False)], "expect": {"deck": "sha256:" + "0" * 64}}, v
        )
        self.assertEqual((found.code, found.status, found.device), ("stale", 412, "deck"))

    def test_expect_may_be_the_bare_hash(self) -> None:
        v = views(managed(["gba"]))
        bare = v["deck"].etag.removeprefix("sha256:")
        plan = plan_changes(
            {"changes": [change("deck", "gba", False)], "expect": {"deck": bare}}, CFG, LIB, v
        )
        self.assertEqual(len(plan.devices), 1)

    def test_every_touched_device_needs_an_expect_but_an_untouched_one_does_not(self) -> None:
        v = views(managed(["gba"]), managed())
        request = {
            "changes": [change("deck", "gba", False), change("machine", "gba", True)],
            "expect": {"deck": v["deck"].etag},
        }
        self.assertEqual(error_of(request, v).device, "machine")
        plan = plan_changes(
            {"changes": [change("deck", "gba", False)], "expect": {"deck": v["deck"].etag}},
            CFG,
            LIB,
            v,
        )
        self.assertEqual([d.device for d in plan.devices], ["deck"])

    def test_unsupported_for_a_named_device_is_422_before_anything_applies(self) -> None:
        v = views(managed(), managed())
        found = error_of(body([change("machine", "ps4", True, PS4)], v), v)
        self.assertEqual((found.code, found.status, found.device), ("unsupported", 422, "machine"))
        self.assertEqual(found.game, PS4.id)
        found = error_of(body([change("machine", "ps4", True)], v), v)
        self.assertEqual(found.code, "unsupported")

    def test_turning_off_an_unsupported_system_is_allowed(self) -> None:
        v = views(managed(), managed(["ps4"]))
        plan = plan_changes(body([change("machine", "ps4", False)], v), CFG, LIB, v)
        self.assertEqual(wl.parse(plan.devices[0].new_text).systems, frozenset())

    def test_star_skips_the_devices_that_do_not_run_the_system(self) -> None:
        v = views(managed(), managed())
        plan = plan_changes(body([change("*", "ps4", True)], v), CFG, LIB, v)
        self.assertEqual([d.device for d in plan.devices], ["deck"])
        self.assertEqual(
            plan.skipped, ({"device": "machine", "system": "ps4", "reason": "unsupported"},)
        )
        plan = plan_changes(body([change("*", "gba", True)], v), CFG, LIB, v)
        self.assertEqual([d.device for d in plan.devices], ["deck", "machine"])
        self.assertEqual(plan.skipped, ())

    def test_a_game_on_adds_its_parts_and_reports_the_bytes_it_adds(self) -> None:
        v = views(managed(), managed())
        plan = plan_changes(body([change("deck", "ps4", True, PS4)], v), CFG, LIB, v)
        (dp,) = plan.devices
        self.assertEqual(dp.turned_on, (PS4.id,))
        self.assertEqual((dp.bytes_on, dp.bytes_off), (10 * GIB + 100, 0))
        model = wl.parse(dp.new_text)
        self.assertEqual(
            model.includes,
            frozenset({"roms/ps4/games/CUSA00000", "roms/ps4/shortcuts/Example PS4.desktop"}),
        )

    def test_a_game_off_inside_a_system_adds_an_exclusion(self) -> None:
        v = views(managed(["psx"]), managed())
        plan = plan_changes(body([change("deck", "psx", False, EXAMPLE)], v), CFG, LIB, v)
        (dp,) = plan.devices
        self.assertEqual((dp.turned_off, dp.bytes_off), ((EXAMPLE.id,), 5 * GIB))
        self.assertEqual(wl.parse(dp.new_text).excludes, frozenset({EXAMPLE.primary}))

    def test_a_system_on_turns_on_every_game_of_it(self) -> None:
        v = views(managed(includes=[OTHER.primary]), managed())
        plan = plan_changes(body([change("deck", "psx", True)], v), CFG, LIB, v)
        (dp,) = plan.devices
        self.assertNotIn(OTHER.id, dp.turned_on)
        self.assertIn(EXAMPLE.id, dp.turned_on)
        self.assertEqual(dp.bytes_on, sum(g.size for g in LIB.systems["psx"].values()) - OTHER.size)

    def test_changes_apply_in_order_and_the_effect_is_the_whole_request(self) -> None:
        v = views(managed(), managed())
        plan = plan_changes(
            body(
                [change("deck", "psx", True), change("deck", "psx", False, EXAMPLE)],
                v,
            ),
            CFG,
            LIB,
            v,
        )
        (dp,) = plan.devices
        self.assertNotIn(EXAMPLE.id, dp.turned_on)
        self.assertEqual(wl.parse(dp.new_text).excludes, frozenset({EXAMPLE.primary}))

    def test_no_change_is_not_changed(self) -> None:
        v = views(managed(["gba"]), managed())
        plan = plan_changes(body([change("deck", "gba", True)], v), CFG, LIB, v)
        self.assertFalse(plan.devices[0].changed)
        self.assertEqual(plan.devices[0].turned_on, ())

    def test_an_unlistable_game_is_refused(self) -> None:
        v = views(managed(["psx"]), managed())
        found = error_of(body([change("deck", "psx", False, BAD)], v), v)
        self.assertEqual((found.code, found.status, found.game), ("unlistable", 409, BAD.id))

    def test_covered_cannot_apply_and_nothing_is_planned(self) -> None:
        sub = make_game("psx", "roms/psx/Sub/Deep.chd", "Deep")
        lib = library([("psx", sub)])
        v = views(managed(["psx"], excludes=["roms/psx/Sub"]), managed())
        request = body([change("deck", "psx", True, sub)], v)
        with self.assertRaises(ApiError) as caught:
            plan_changes(request, CFG, lib, v)
        self.assertEqual((caught.exception.code, caught.exception.status), ("covered", 409))

    def test_the_first_error_in_the_documented_order_wins(self) -> None:
        # unknown device (404) before the not-managed one (409) before stale (412)
        v = views(deck="!/roms/gba\n*\n")
        request = {
            "changes": [change("deck", "gba", True), change("tv", "gba", True)],
            "expect": {"deck": "sha256:x"},
        }
        self.assertEqual(error_of(request, v).code, "not-found")


class ResponseTests(unittest.TestCase):
    def test_shape_for_a_dry_run_and_a_write(self) -> None:
        v = views(managed(["gba"]), managed())
        plan = plan_changes(
            body([change("deck", "gba", False), change("*", "ps4", True)], v), CFG, LIB, v
        )
        dry = changes_response(plan)
        self.assertEqual(dry["dry_run"], False)
        (item,) = dry["devices"]
        self.assertEqual((item["written"], item["etag"]), (False, v["deck"].etag))
        self.assertEqual(
            set(item), {"device", "written", "etag", "on", "off", "bytes_on", "bytes_off"}
        )
        done = changes_response(plan, {"deck": True}, {"deck": ["warn"]})
        self.assertEqual(done["devices"][0]["etag"], plan.devices[0].new_etag)
        self.assertEqual(done["devices"][0]["warnings"], ["warn"])
        self.assertEqual(
            done["skipped"], [{"device": "machine", "system": "ps4", "reason": "unsupported"}]
        )


class ErrorTests(unittest.TestCase):
    def test_every_code_has_the_documented_status(self) -> None:
        expected = {
            "bad-request": 400,
            "unauthorized": 401,
            "bad-origin": 403,
            "not-found": 404,
            "not-managed": 409,
            "covered": 409,
            "cannot-apply": 409,
            "unlistable": 409,
            "stale": 412,
            "too-many-attempts": 429,
            "unsupported": 422,
            "expect-missing": 428,
            "hub-unreachable": 503,
        }
        for code, status in expected.items():
            self.assertEqual(ApiError(code, "m").status, status, code)

    def test_body(self) -> None:
        self.assertEqual(
            ApiError("stale", "m", device="deck", game="g").body(),
            {"error": {"code": "stale", "message": "m", "device": "deck", "game": "g"}},
        )
        self.assertEqual(
            ApiError("stale", "m").body(), {"error": {"code": "stale", "message": "m"}}
        )


if __name__ == "__main__":
    unittest.main()
