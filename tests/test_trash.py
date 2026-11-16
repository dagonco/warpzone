"""`trash`: the pure decisions of deleting a game from the library UI. No disk, no network."""

import json
import unittest
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any

from library_support import config, game, library, managed
from warpzone import catalog, inbox_mame, library_api, progress, trash
from warpzone import whitelist as wl
from warpzone.library_api import ApiError
from warpzone.trash import (
    F_DIR,
    F_FILE,
    F_LINK,
    Arcade,
    Cleanup,
    Facts,
    Found,
    Lines,
    MediaFound,
)

NOW = datetime(2026, 11, 16, 10, 15, tzinfo=UTC)
GAME = game("psx", "roms/psx/Example Game (Europe).chd", "Example Game (Europe)", 5000)
OTHER = game("psx", "roms/psx/Other Game.chd", "Other Game", 1000)
PS4 = game(
    "ps4",
    "roms/ps4/games/CUSA00000",
    "Example PS4",
    5000,
    kind="dir",
    extra=[("roms/ps4/shortcuts/Example PS4.desktop", "file", 100)],
    gamelist="./shortcuts/Example PS4.desktop",
)
GAMELIST = """<?xml version="1.0"?>
<gameList>
\t<game>
\t\t<path>./Example Game (Europe).chd</path>
\t\t<name>Example Game</name>
\t</game>
\t<game>
\t\t<path>./Other Game.chd</path>
\t</game>
</gameList>
"""


def views(deck: str | None = None, machine: str | None = None) -> dict[str, library_api.DeviceView]:
    return {
        "deck": library_api.view_of((deck if deck is not None else managed()).encode()),
        "machine": library_api.view_of((machine if machine is not None else managed()).encode()),
    }


def found(system: str, g: Any, size: int = 5000) -> dict[str, Found]:
    return {
        p.path: Found(F_DIR if p.type == "dir" else F_FILE, p.size, 1_700_000_000, p.files or 0)
        for p in g.parts
    }


def facts(g: Any = GAME, **kw: Any) -> Facts:
    return Facts(**{"parts": found("psx", g), "gamelist": GAMELIST, **kw})


def plan(g: Any = GAME, items: Any = None, view: Any = None, **kw: Any) -> trash.DeletePlan:
    lib = library(items if items is not None else [("psx", GAME), ("psx", OTHER)])
    return trash.plan_delete(g.id, lib, config(), view or views(), facts(g, **kw))


def refusal(**kw: Any) -> ApiError:
    with unittest.TestCase().assertRaises(ApiError) as caught:
        plan(**kw)
    return caught.exception


class PlanTests(unittest.TestCase):
    def test_an_unknown_game_is_not_found(self) -> None:
        with self.assertRaises(ApiError) as caught:
            trash.plan_delete("0" * 16, library(), config(), views(), Facts())
        self.assertEqual((caught.exception.code, caught.exception.status), ("not-found", 404))

    def test_the_plan_lists_every_part_with_its_size(self) -> None:
        p = trash.plan_delete(
            PS4.id,
            library([("ps4", PS4)]),
            config(),
            views(),
            Facts(parts=found("ps4", PS4)),
        )
        self.assertEqual([f.path for f in p.files], [x.path for x in PS4.parts])
        self.assertEqual(
            [f.json() for f in p.files],
            [
                {"path": "roms/ps4/games/CUSA00000", "type": "dir", "size": 5000, "files": 1},
                {"path": "roms/ps4/shortcuts/Example PS4.desktop", "type": "file", "size": 100},
            ],
        )
        self.assertEqual(p.size, sum(f.size for f in p.files))
        self.assertEqual(p.absent, ())

    def test_a_part_that_is_absent_is_listed_and_skipped(self) -> None:
        parts = found("ps4", PS4)
        del parts["roms/ps4/shortcuts/Example PS4.desktop"]
        p = trash.plan_delete(
            PS4.id, library([("ps4", PS4)]), config(), views(), Facts(parts=parts)
        )
        self.assertEqual(p.absent, ("roms/ps4/shortcuts/Example PS4.desktop",))
        self.assertEqual([f.path for f in p.files], ["roms/ps4/games/CUSA00000"])

    def test_a_game_with_no_file_on_the_hub_is_not_found(self) -> None:
        error = refusal(parts={})
        self.assertEqual(error.code, "not-found")

    def test_a_link_is_unsafe(self) -> None:
        error = refusal(parts={GAME.primary: Found(F_LINK)})
        self.assertEqual((error.code, error.status), ("unsafe-path", 409))
        self.assertEqual(error.body()["error"]["paths"], [GAME.primary])

    def test_a_part_of_another_type_than_the_catalog_says_is_unsafe(self) -> None:
        error = refusal(parts={GAME.primary: Found(F_DIR, 0, 0, 0)})
        self.assertEqual(error.code, "unsafe-path")

    def test_a_part_another_game_has_or_contains_or_sits_in_is_unsafe(self) -> None:
        folder = game("psx", "roms/psx/Folder", "Folder", kind="dir")
        inside = game("psx", "roms/psx/Folder/Inside.chd", "Inside")
        same = replace(
            game("psx", "roms/psx/Same.chd", "Same"),
            parts=(catalog.Part(GAME.primary, "file", 1, 1),),
        )
        for mine, theirs in ((folder, inside), (inside, folder), (GAME, same)):
            error = refusal(
                g=mine, items=[("psx", mine), ("psx", theirs)], parts=found("psx", mine)
            )
            self.assertEqual(error.code, "unsafe-path", (mine.title, theirs.title))

    def test_a_part_too_shallow_or_outside_its_system_is_unsafe(self) -> None:
        for path in ("roms/psx", "roms/other/Game.chd", "library/roms/psx/Game.chd", "../x/y/z"):
            bad = replace(GAME, parts=(catalog.Part(path, "file", 1, 1),))
            error = refusal(g=bad, items=[("psx", bad)], parts={path: Found(F_FILE, 1, 1)})
            self.assertEqual(error.code, "unsafe-path", path)

    def test_a_system_root_is_never_a_part(self) -> None:
        for path, system in (
            ("roms/xbox360/roms", "xbox360"),
            ("roms/wiiu/roms", "wiiu"),
            ("extras/switch/updates", "switch"),
            ("extras/switch/dlc", "switch"),
        ):
            self.assertFalse(trash.safe_part(path, system), path)
            bad = replace(GAME, parts=(catalog.Part(path, "dir", 1, 1),))
            error = refusal(g=bad, items=[(system, bad)], parts={path: Found(F_DIR, 1, 1, 1)})
            self.assertEqual(error.code, "unsafe-path", path)
        # a game inside a system root is still a part
        self.assertTrue(trash.safe_part("roms/xbox360/roms/Game", "xbox360"))
        self.assertTrue(trash.safe_part("extras/switch/updates/Game.nsp", "switch"))
        self.assertTrue(trash.safe_part("roms/psx/roms", "psx"))  # not a root for this system

    def test_a_whitelist_that_needs_migration_is_not_managed(self) -> None:
        error = refusal(view=views(machine="/roms/psx/hand written\n*\n"))
        self.assertEqual((error.code, error.status, error.device), ("not-managed", 409, "machine"))

    def test_the_refusals_come_in_the_designs_order(self) -> None:
        bad_views = views(machine="/roms/psx/hand written\n*\n")
        every = {
            "parts": {GAME.primary: Found(F_LINK)},
            "view": bad_views,
            "trash_problem": "trash/ is not writable",
            "cleanups_problem": "catalog/cleanups.json could not be read",
        }
        order = [
            "unsafe-path",
            "not-managed",
            "trash-unavailable",
            "read-failed",
        ]
        for code in order:
            self.assertEqual(refusal(**every).code, code)
            if code == "unsafe-path":
                every["parts"] = found("psx", GAME)
            elif code == "not-managed":
                every["view"] = views()
            elif code == "trash-unavailable":
                every["trash_problem"] = None

    def test_the_trash_and_the_cleanups_have_their_own_errors(self) -> None:
        error = refusal(trash_problem="trash/ is on another filesystem than library/")
        self.assertEqual((error.code, error.status), ("trash-unavailable", 409))
        error = refusal(cleanups_problem="catalog/cleanups.json could not be read (nope)")
        self.assertEqual((error.code, error.status), ("read-failed", 500))
        self.assertIn("cleanups.json", error.message)

    def test_expect_depends_on_files_media_gamelist_and_whitelists(self) -> None:
        base = plan()
        self.assertEqual(base.expect, plan().expect)
        self.assertTrue(base.expect.startswith("sha256:"))
        bigger = plan(parts={GAME.primary: Found(F_FILE, 5001, 1)})
        self.assertNotEqual(base.expect, bigger.expect)
        art = plan(media=[MediaFound("psx/covers/Example Game (Europe).png", F_FILE, 9, 1)])
        self.assertNotEqual(base.expect, art.expect)
        no_entry = plan(gamelist=None)
        self.assertNotEqual(base.expect, no_entry.expect)
        lines = plan(view=views(deck=managed(includes=[GAME.primary])))
        self.assertNotEqual(base.expect, lines.expect)
        # a change that is not in the answer does not change it
        self.assertEqual(base.expect, plan(parts={GAME.primary: Found(F_FILE, 5000, 99)}).expect)

    def test_the_whitelist_lines_are_those_at_or_below_a_part(self) -> None:
        deck = managed(["gba"], includes=[PS4.primary, "roms/ps4/shortcuts"], excludes=[])
        machine = managed(["ps4"], excludes=[PS4.primary, "roms/ps4/games/other"])
        p = trash.plan_delete(
            PS4.id,
            library([("ps4", PS4)]),
            config(),
            views(deck, machine),
            Facts(parts=found("ps4", PS4)),
        )
        self.assertEqual(p.whitelists["deck"], Lines((PS4.primary,), ()))
        self.assertEqual(p.whitelists["machine"], Lines((), (PS4.primary,)))
        self.assertEqual(p.whitelists["deck"].texts(), ["!/roms/ps4/games/CUSA00000"])
        self.assertEqual(p.whitelists["machine"].texts(), ["/roms/ps4/games/CUSA00000"])
        answer = trash.plan_response(p, saves={}, busy=None, purge_after="x")
        self.assertEqual(answer["whitelists"]["deck"], ["!/roms/ps4/games/CUSA00000"])

    def test_a_device_with_no_line_is_left_out(self) -> None:
        p = plan(view=views(deck=managed(["psx"])))
        self.assertEqual(p.whitelists, {})
        self.assertEqual(trash.new_cleanup(p, "20261116T101500Z-" + GAME.id, NOW), None)

    def test_the_response_has_the_fields_of_the_design(self) -> None:
        answer = trash.plan_response(
            plan(),
            saves={"data": True},
            busy={"since": "s", "mode": "m", "task": None},
            purge_after="2026-11-23T10:15:00Z",
        )
        self.assertEqual(
            sorted(answer),
            sorted(
                [
                    "dry_run",
                    "game",
                    "expect",
                    "files",
                    "absent",
                    "size",
                    "media",
                    "media_size",
                    "gamelist",
                    "whitelists",
                    "saves",
                    "busy",
                    "purge_after",
                    "warnings",
                ]
            ),
        )
        self.assertEqual(answer["game"]["name"], "Example Game")
        self.assertTrue(answer["dry_run"])
        self.assertTrue(answer["gamelist"])

    def test_check_expect(self) -> None:
        p = plan()
        trash.check_expect(p, p.expect)
        with self.assertRaises(ApiError) as caught:
            trash.check_expect(p, None)
        self.assertEqual((caught.exception.code, caught.exception.status), ("expect-missing", 428))
        with self.assertRaises(ApiError) as caught:
            trash.check_expect(p, "sha256:00")
        self.assertEqual((caught.exception.code, caught.exception.status), ("stale", 412))

    def test_the_request_body(self) -> None:
        self.assertEqual(trash.parse_delete({"dry_run": True}), trash.DeleteRequest(True, None))
        self.assertEqual(
            trash.parse_delete({"dry_run": False, "expect": "sha256:1"}),
            trash.DeleteRequest(False, "sha256:1"),
        )
        for body in (
            {},
            {"dry_run": "yes"},
            {"dry_run": True, "x": 1},
            [],
            {"dry_run": 1},
            {"dry_run": False, "expect": 3},
        ):
            with self.assertRaises(ApiError) as caught:
                trash.parse_delete(body)
            self.assertEqual(caught.exception.code, "bad-request", body)
        trash.parse_empty({})
        with self.assertRaises(ApiError):
            trash.parse_empty({"x": 1})


class MediaTests(unittest.TestCase):
    def media(self, *names: str, g: Any = GAME, **kw: Any) -> list[str]:
        items = [MediaFound(n, F_FILE, 10, 1) if isinstance(n, str) else n for n in names]
        return [m.path for m in plan(g=g, media=items, **kw).media]

    def test_every_extension_of_the_stem_is_taken(self) -> None:
        got = self.media(
            "psx/covers/Example Game (Europe).png",
            "psx/manuals/Example Game (Europe).pdf",
            "psx/videos/Example Game (Europe).mp4",
            "psx/fanart/Example Game (Europe).jpg",
        )
        self.assertEqual(len(got), 4)

    def test_a_longer_name_or_another_game_or_no_extension_is_left_alone(self) -> None:
        got = self.media(
            "psx/covers/Example Game (Europe).m3u.png",  # the folder form of another stem
            "psx/covers/Example Game (Europe) 2.png",
            "psx/covers/Other Game.png",
            "psx/covers/Example Game (Europe)",
            "gba/covers/Example Game (Europe).png",  # another system
        )
        self.assertEqual(got, [])

    def test_the_folder_form_of_the_stem_is_taken(self) -> None:
        got = self.media("psx/covers/Example Game (Europe).chd.png")
        self.assertEqual(got, ["psx/covers/Example Game (Europe).chd.png"])

    def test_a_stem_another_game_resolves_to_is_left_out(self) -> None:
        # the shorter path of the PS4 lookup is `./Example PS4.desktop`; another game owns it
        rival = game(
            "ps4",
            "roms/ps4/games/CUSA00001",
            "Example PS4",
            kind="dir",
            gamelist="./Example PS4.desktop",
        )
        items = [("ps4", PS4), ("ps4", rival)]
        mine = trash.plan_delete(
            PS4.id,
            library(items),
            config(),
            views(),
            Facts(
                parts=found("ps4", PS4),
                media=[
                    MediaFound("ps4/covers/shortcuts/Example PS4.png", F_FILE, 5, 1),
                    MediaFound("ps4/covers/Example PS4.png", F_FILE, 5, 1),
                ],
            ),
        )
        self.assertEqual([m.path for m in mine.media], ["ps4/covers/shortcuts/Example PS4.png"])

    def test_a_link_is_named_and_never_taken(self) -> None:
        p = plan(
            media=[MediaFound("psx/covers/Example Game (Europe).png", F_LINK)],
            media_links=["frontend/media/psx/marquees"],
        )
        self.assertEqual(p.media, ())
        self.assertEqual(len(p.warnings), 2)
        self.assertTrue(all("link" in w for w in p.warnings))

    def test_the_dirs_to_list_are_those_of_the_stems(self) -> None:
        stems = trash.media_stems(PS4)
        self.assertIn("shortcuts/Example PS4", stems)
        self.assertIn("Example PS4", stems)
        self.assertEqual(trash.media_dirs(stems), ["", "shortcuts"])
        self.assertEqual(trash.own_stems(PS4, [PS4]), frozenset())


class GamelistPlanTests(unittest.TestCase):
    def test_only_this_games_entries_are_removed(self) -> None:
        p = plan()
        self.assertTrue(p.gamelist)
        self.assertEqual(len(p.blocks), 1)
        self.assertIn("Example Game (Europe).chd", p.blocks[0])
        self.assertEqual(p.name, "Example Game")

    def test_no_entry_and_no_file_remove_nothing(self) -> None:
        self.assertFalse(plan(gamelist="<gameList>\n</gameList>\n").gamelist)
        none = plan(gamelist=None)
        self.assertFalse(none.gamelist)
        self.assertFalse(none.gamelist_failed)
        self.assertEqual(none.name, "Example Game (Europe)")

    def test_an_unreadable_gamelist_is_a_warning_and_the_entry_stays(self) -> None:
        p = plan(gamelist=None, gamelist_error="it is behind a link")
        self.assertTrue(p.gamelist_failed)
        self.assertFalse(p.gamelist)
        self.assertIn("its entry stays", p.warnings[0])

    def test_a_path_another_game_has_is_not_this_games(self) -> None:
        rival = game(
            "ps4", "roms/ps4/games/CUSA00001", "Rival", kind="dir", gamelist="./Example PS4.desktop"
        )
        text = (
            "<gameList>\n\t<game>\n\t\t<path>./Example PS4.desktop</path>\n\t</game>\n"
            "\t<game>\n\t\t<path>./shortcuts/Example PS4.desktop</path>\n\t</game>\n</gameList>\n"
        )
        p = trash.plan_delete(
            PS4.id,
            library([("ps4", PS4), ("ps4", rival)]),
            config(),
            views(),
            Facts(parts=found("ps4", PS4), gamelist=text),
        )
        self.assertEqual(p.gamelist_paths, ("./shortcuts/Example PS4.desktop",))
        self.assertEqual(len(p.blocks), 1)


class ArcadeTests(unittest.TestCase):
    PARENT = game("mame", "roms/mame/pacman.zip", "Pac-Man")
    CLONE = game("mame", "roms/mame/puckman.zip", "Puckman")
    OTHER = game("mame", "roms/mame/galaga.zip", "Galaga")

    def arcade(self) -> Arcade:
        machines = {
            "pacman": inbox_mame.Machine("pacman"),
            "puckman": inbox_mame.Machine("puckman", cloneof="pacman", romof="pacman"),
            "galaga": inbox_mame.Machine("galaga", devices=("namco",)),
            "namco": inbox_mame.Machine("namco", kind="device"),
        }
        return Arcade((inbox_mame.SetDb("MAME", None, machines),))

    def plan(self, g: Any, arcade: Arcade | None) -> trash.DeletePlan:
        items = [("mame", self.PARENT), ("mame", self.CLONE), ("mame", self.OTHER)]
        return trash.plan_delete(
            g.id,
            library(items),
            config(),
            views(),
            Facts(parts=found("mame", g), arcade=arcade),
        )

    def test_a_set_others_need_is_refused_naming_them(self) -> None:
        with self.assertRaises(ApiError) as caught:
            self.plan(self.PARENT, self.arcade())
        error = caught.exception
        self.assertEqual((error.code, error.status), ("has-dependants", 409))
        self.assertEqual(
            error.body()["error"]["dependants"],
            [{"id": self.CLONE.id, "title": "Puckman", "file": "puckman.zip"}],
        )

    def test_a_clone_and_an_unrelated_set_are_deleted(self) -> None:
        self.assertEqual(self.plan(self.CLONE, self.arcade()).game.id, self.CLONE.id)
        self.assertEqual(self.plan(self.OTHER, self.arcade()).game.id, self.OTHER.id)

    def test_a_device_set_is_needed_by_the_games_that_use_it(self) -> None:
        device = game("mame", "roms/mame/namco.zip", "Namco")
        items = [("mame", device), ("mame", self.OTHER)]
        with self.assertRaises(ApiError) as caught:
            trash.plan_delete(
                device.id,
                library(items),
                config(),
                views(),
                Facts(parts=found("mame", device), arcade=self.arcade()),
            )
        self.assertEqual(caught.exception.code, "has-dependants")

    def test_no_dat_cannot_tell_so_it_refuses(self) -> None:
        for arcade in (None, Arcade()):
            with self.assertRaises(ApiError) as caught:
                self.plan(self.OTHER, arcade)
            self.assertEqual(
                (caught.exception.code, caught.exception.status), ("dats-missing", 409)
            )

    def test_a_set_no_dat_knows_has_no_dependants(self) -> None:
        stranger = game("mame", "roms/mame/unknown.zip", "Unknown")
        p = trash.plan_delete(
            stranger.id,
            library([("mame", stranger)]),
            config(),
            views(),
            Facts(parts=found("mame", stranger), arcade=self.arcade()),
        )
        self.assertEqual(p.game.id, stranger.id)

    def test_other_systems_need_no_dat(self) -> None:
        self.assertEqual(plan().game.id, GAME.id)


class NamesAndTimesTests(unittest.TestCase):
    def test_entry_names(self) -> None:
        name = trash.entry_name(NOW, "5d0c1f3a9b2e7c48")
        self.assertEqual(name, "20261116T101500Z-5d0c1f3a9b2e7c48")
        self.assertEqual(trash.entry_name(NOW, "5d0c1f3a9b2e7c48", {name}), name + "-2")
        self.assertEqual(
            trash.entry_name(NOW, "5d0c1f3a9b2e7c48", {name, name + "-2"}), name + "-3"
        )
        for good in (name, name + "-2", name + "-17"):
            self.assertTrue(trash.valid_entry(good), good)
        for bad in (
            "",
            "..",
            "x",
            name + "/",
            name + "-",
            name + "-x",
            "2026" + name,
            name.upper(),
            name + "\n",
            "../" + name,
        ):
            self.assertFalse(trash.valid_entry(bad), bad)

    def test_days_left_rounds_up_and_never_goes_below_zero(self) -> None:
        after = "2026-11-23T10:15:00Z"
        self.assertEqual(trash.days_left(after, NOW), 7)
        self.assertEqual(trash.days_left(after, NOW + timedelta(days=6, hours=1)), 1)
        self.assertEqual(trash.days_left(after, NOW + timedelta(days=7)), 0)
        self.assertEqual(trash.days_left(after, NOW + timedelta(days=9)), 0)
        self.assertIsNone(trash.days_left("tomorrow", NOW))
        self.assertEqual(trash.when_text(trash.purge_time(NOW)), after)

    def test_a_path_below_a_folder(self) -> None:
        for good in ("library/roms/psx/a.chd", "frontend/media/psx/covers/a.png"):
            self.assertTrue(trash.safe_below(good, good.split("/")[0] + "/"))
        for bad in (
            "library/",
            "library//a",
            "library/../a",
            "library/./a",
            "library/a/..",
            "other/a",
            "library/a\0",
        ):
            self.assertFalse(trash.safe_below(bad, "library/"), bad)


def made_plan(**kw: Any) -> trash.DeletePlan:
    return plan(
        view=views(deck=managed(includes=[GAME.primary])),
        media=[MediaFound("psx/covers/Example Game (Europe).png", F_FILE, 12, 1)],
        **kw,
    )


ENTRY = "20261116T101500Z-" + GAME.id


class ManifestTests(unittest.TestCase):
    def manifest(self, **kw: Any) -> trash.Manifest:
        return trash.new_manifest(made_plan(), ENTRY, NOW, **kw)

    def test_it_round_trips(self) -> None:
        m = self.manifest(state=trash.COMPLETE, backup="tools/logs/gamelist-backups/psx-x.xml")
        again = trash.parse_manifest(trash.encode_manifest(m), ENTRY)
        self.assertEqual(again, m)
        assert again is not None
        self.assertEqual(
            again.paths,
            (
                "library/roms/psx/Example Game (Europe).chd",
                "frontend/media/psx/covers/Example Game (Europe).png",
            ),
        )
        self.assertEqual(again.size, 5000 + 12)
        self.assertEqual(again.purge_after, "2026-11-23T10:15:00Z")
        self.assertEqual(again.game, GAME)
        self.assertEqual(again.whitelists["deck"], Lines((GAME.primary,), ()))
        self.assertIsNotNone(again.gamelist)

    def test_the_bytes_are_sorted_json(self) -> None:
        data = trash.encode_manifest(self.manifest())
        doc = json.loads(data)
        self.assertEqual(list(doc), sorted(doc))
        self.assertEqual(doc["format"], 1)
        self.assertEqual(doc["state"], "moving")

    def test_a_manifest_that_is_not_this_shape_is_damaged(self) -> None:
        good = json.loads(trash.encode_manifest(self.manifest()))

        def damaged(change: Any) -> bool:
            doc = json.loads(json.dumps(good))
            change(doc)
            return trash.parse_manifest(json.dumps(doc).encode(), ENTRY) is None

        self.assertIsNone(trash.parse_manifest(b"not json", ENTRY))
        self.assertIsNone(trash.parse_manifest(b"\xff", ENTRY))
        self.assertIsNone(trash.parse_manifest(b"[]", ENTRY))
        self.assertIsNone(
            trash.parse_manifest(trash.encode_manifest(self.manifest()), ENTRY + "-2")
        )
        cases = {
            "format": lambda d: d.update(format=2),
            "state": lambda d: d.update(state="done"),
            "system": lambda d: d.update(system="../x"),
            "id": lambda d: d.update(id="zz"),
            "deleted": lambda d: d.update(deleted="yesterday"),
            "game": lambda d: d.update(game={"title": 1}),
            "game id": lambda d: d.update(id="0" * 16),
            "outside": lambda d: d["files"][0].update(path="roms/psx/a.chd"),
            "dotdot": lambda d: d["files"][0].update(path="library/../etc/passwd"),
            "absolute": lambda d: d["files"][0].update(path="/library/roms/psx/a"),
            "media outside": lambda d: d["media"][0].update(path="library/roms/psx/a.png"),
            "empty component": lambda d: d["files"][0].update(path="library/roms//a"),
            "type": lambda d: d["files"][0].update(type="link"),
            "size": lambda d: d["files"][0].update(size=-1),
            "nested": lambda d: d["files"].append(
                {"path": d["files"][0]["path"] + "/inner", "type": "file", "size": 1}
            ),
            "duplicate": lambda d: d["files"].append(dict(d["files"][0])),
            "gamelist path": lambda d: d["gamelist"].update(path="library/x"),
            "gamelist entries": lambda d: d["gamelist"].update(entries=[1]),
            "whitelists": lambda d: d.update(whitelists={"deck": {"include": "x"}}),
            "whitelist path": lambda d: d.update(
                whitelists={"deck": {"include": ["a/../b"], "exclude": []}}
            ),
        }
        for name, change in cases.items():
            self.assertTrue(damaged(change), name)

    def test_no_gamelist_entry_is_null(self) -> None:
        m = trash.new_manifest(plan(gamelist=None), ENTRY, NOW)
        self.assertIsNone(m.gamelist)
        self.assertIsNone(json.loads(trash.encode_manifest(m))["gamelist"])

    def test_the_blocks_of_the_removal_replace_the_plans(self) -> None:
        m = self.manifest(state=trash.COMPLETE, blocks=["<game>x</game>"])
        assert m.gamelist is not None
        self.assertEqual(m.gamelist.entries, ("<game>x</game>",))
        self.assertIsNone(self.manifest(blocks=[]).gamelist)

    def test_the_state_of_an_entry_and_when_it_expires(self) -> None:
        done = trash.Listed(ENTRY, self.manifest(state=trash.COMPLETE))
        moving = trash.Listed(ENTRY, self.manifest())
        restoring = trash.Listed(ENTRY, trash.with_state(self.manifest(), trash.RESTORING))
        broken = trash.Listed(ENTRY, None)
        self.assertEqual(
            [trash.entry_state(i) for i in (done, moving, restoring, broken)],
            ["complete", "incomplete", "incomplete", "damaged"],
        )
        late = NOW + timedelta(days=7)
        self.assertFalse(trash.expired(done, late - timedelta(seconds=1)))
        self.assertTrue(trash.expired(done, late))
        # only a complete entry is purged by itself
        self.assertFalse(
            any(trash.expired(i, late + timedelta(days=99)) for i in (moving, restoring, broken))
        )

    def test_the_trash_answer(self) -> None:
        done = trash.Listed(ENTRY, self.manifest(state=trash.COMPLETE))
        older = trash.Listed("20250101T000000Z-" + GAME.id, None)
        pending = [{"device": "deck", "entry": ENTRY, "waiting": "hub"}]
        answer = trash.trash_response([older, done], pending, NOW + timedelta(days=1))
        self.assertEqual([e["entry"] for e in answer["entries"]], [ENTRY, older.entry])
        row = answer["entries"][0]
        self.assertEqual(
            (row["state"], row["days_left"], row["files"], row["media"], row["size"]),
            ("complete", 6, 1, 1, 5012),
        )
        self.assertEqual(answer["size"], 5012)
        broken = answer["entries"][1]
        self.assertEqual(
            (broken["state"], broken["title"], broken["days_left"], broken["size"]),
            ("damaged", None, None, None),
        )
        self.assertEqual(answer["pending"], pending)
        moving = trash.trash_response([trash.Listed(ENTRY, self.manifest())], [], NOW)
        self.assertEqual(
            (moving["entries"][0]["state"], moving["entries"][0]["days_left"]), ("incomplete", None)
        )


class RestorePlanTests(unittest.TestCase):
    def test_what_moves_what_conflicts_and_what_is_skipped(self) -> None:
        m = trash.new_manifest(made_plan(), ENTRY, NOW)
        part, art = m.paths
        self.assertEqual(
            trash.plan_restore(m, set(), {part, art}), trash.RestorePlan((part, art), (), ())
        )
        self.assertEqual(
            trash.plan_restore(m, {part}, {part, art}), trash.RestorePlan((art,), (), (part,))
        )
        self.assertEqual(
            trash.plan_restore(m, set(), {part}), trash.RestorePlan((part,), (art,), ())
        )
        # a path that never left needs nothing and is no conflict
        self.assertEqual(trash.plan_restore(m, {part}, {art}), trash.RestorePlan((art,), (), ()))


def cleanup(**kw: Any) -> Cleanup:
    fields: dict[str, Any] = {
        "entry": ENTRY,
        "id": GAME.id,
        "system": "psx",
        "title": "Example Game (Europe)",
        "deleted": "2026-11-16T10:15:00Z",
        "recorded": None,
        "paths": (GAME.primary,),
        "devices": {"deck": Lines((GAME.primary,), ()), "machine": Lines((), (GAME.primary,))},
    }
    return Cleanup(**{**fields, **kw})


class CleanupTests(unittest.TestCase):
    def test_it_round_trips_and_a_missing_file_is_empty(self) -> None:
        items = [cleanup(), cleanup(entry=ENTRY + "-2", recorded="2026-11-16T10:16:00Z")]
        self.assertEqual(trash.parse_cleanups(trash.encode_cleanups(items)), items)
        self.assertEqual(trash.parse_cleanups(None), [])
        self.assertEqual(trash.parse_cleanups(trash.encode_cleanups([])), [])

    def test_a_file_that_is_not_one_is_refused(self) -> None:
        good = json.loads(trash.encode_cleanups([cleanup()]))
        for data in (
            b"x",
            b"\xff",
            b"[]",
            json.dumps({"format": 2}).encode(),
            json.dumps({"format": 1}).encode(),
        ):
            with self.assertRaises(trash.CleanupsError):
                trash.parse_cleanups(data)
        for change in (
            lambda c: c.update(entry="x"),
            lambda c: c.update(id="x"),
            lambda c: c.update(paths=["roms/psx"]),
            lambda c: c.update(recorded="soon"),
            lambda c: c.update(devices={"deck": {"include": [1], "exclude": []}}),
            lambda c: c.update(devices=[]),
        ):
            doc = json.loads(json.dumps(good))
            change(doc["cleanups"][0])
            with self.assertRaises(trash.CleanupsError):
                trash.parse_cleanups(json.dumps(doc).encode())

    def test_the_hub_has_recorded_the_deletion(self) -> None:
        deleted = {"global": {"deleted": True}, "local": {"deleted": True}}
        live = {"global": {"deleted": False}, "local": {"deleted": False}}
        self.assertTrue(trash.in_index({"a": deleted, "b": None}))
        # an answer that does not say what the global record is, is not a recorded deletion
        self.assertFalse(trash.in_index({"a": {"global": None}}))
        self.assertFalse(trash.in_index({"a": {"local": {"deleted": True}}}))
        self.assertFalse(trash.in_index({"a": {}}))
        self.assertFalse(trash.in_index({"a": {"global": {}}}))
        self.assertFalse(trash.in_index({"a": {"global": "x"}}))
        self.assertFalse(trash.in_index({"a": deleted, "b": live}))
        self.assertFalse(trash.in_index({"a": {"global": {"version": ["X:1"]}}}))
        self.assertTrue(trash.in_index({}))

    def reading(self, **kw: Any) -> progress.Reading:
        base: dict[str, Any] = {
            "connected": True,
            "completion": 100.0,
            "need_items": 0,
            "need_deletes": 0,
        }
        return progress.Reading(**{**base, **kw})

    def test_what_the_lines_wait_for(self) -> None:
        def w(**kw: Any) -> str | None:
            args: dict[str, Any] = {"recorded": True, "managed": True, "reading": self.reading()}
            return trash.waiting(**{**args, **kw})

        self.assertIsNone(w())
        self.assertEqual(w(managed=False), "not-managed")
        self.assertEqual(w(recorded=False), "hub")
        self.assertEqual(w(reading=None), "unknown")
        self.assertEqual(w(reading=self.reading(connected=False)), "offline")
        self.assertEqual(w(reading=self.reading(shared=False)), "not-shared")
        self.assertEqual(w(reading=self.reading(remote_state="notSharing")), "not-shared")
        self.assertEqual(w(reading=self.reading(remote_state="paused")), "paused")
        self.assertEqual(w(reading=self.reading(completion=42.0)), "syncing")
        self.assertEqual(w(reading=self.reading(need_items=1)), "syncing")
        self.assertEqual(w(reading=self.reading(need_bytes=1)), "syncing")
        # a pending deletion is `needDeletes`, which `needItems` does not count
        self.assertEqual(w(reading=self.reading(need_deletes=1)), "syncing")
        self.assertEqual(w(reading=self.reading(completion=95.0, need_deletes=1)), "syncing")
        self.assertEqual(w(reading=self.reading(need_deletes=None)), "unknown")
        # the hub comes before an offline console
        self.assertEqual(w(recorded=False, reading=self.reading(connected=False)), "hub")

    def test_the_listing_says_it_from_the_cached_state(self) -> None:
        def w(state: str | None, **kw: Any) -> str:
            args: dict[str, Any] = {"recorded": True, "managed": True}
            return trash.waiting_from_state(state, **{**args, **kw})

        self.assertEqual(w("offline"), "offline")
        self.assertEqual(w("paused"), "paused")
        self.assertEqual(w("not-shared"), "not-shared")
        for state in ("downloading", "pending", "done"):
            self.assertEqual(w(state), "syncing")
        self.assertEqual(w(None), "unknown")
        self.assertEqual(w("done", recorded=False), "hub")
        self.assertEqual(w("done", managed=False), "not-managed")

    def test_the_rows_of_the_trash_answer(self) -> None:
        rows = trash.pending_rows(
            [cleanup()], {(ENTRY, "deck"): "offline", (ENTRY, "machine"): "hub"}
        )
        self.assertEqual(
            rows,
            [
                {
                    "device": "deck",
                    "entry": ENTRY,
                    "title": "Example Game (Europe)",
                    "lines": 1,
                    "since": "2026-11-16T10:15:00Z",
                    "waiting": "offline",
                },
                {
                    "device": "machine",
                    "entry": ENTRY,
                    "title": "Example Game (Europe)",
                    "lines": 1,
                    "since": "2026-11-16T10:15:00Z",
                    "waiting": "hub",
                },
            ],
        )

    def test_a_path_that_exists_again_takes_its_lines_with_it(self) -> None:
        c = cleanup()
        self.assertIs(trash.reappeared(c, set()), c)
        self.assertIsNone(trash.reappeared(c, {GAME.primary}))
        two = cleanup(
            paths=(GAME.primary, "roms/psx/Other.chd"),
            devices={"deck": Lines((GAME.primary, "roms/psx/Other.chd"), ())},
        )
        left = trash.reappeared(two, {GAME.primary})
        assert left is not None
        self.assertEqual(left.paths, ("roms/psx/Other.chd",))
        self.assertEqual(left.devices["deck"], Lines(("roms/psx/Other.chd",), ()))

    def test_devices_that_left_are_dropped(self) -> None:
        c = cleanup()
        only = trash.only_devices(c, {"deck"})
        assert only is not None
        self.assertEqual(list(only.devices), ["deck"])
        self.assertIsNone(trash.only_devices(c, {"other"}))
        after = trash.without_device(c, "deck")
        assert after is not None
        self.assertEqual(list(after.devices), ["machine"])
        self.assertIsNone(trash.without_device(after, "machine"))

    def test_the_lines_come_out_of_the_model_and_nothing_is_added(self) -> None:
        model = wl.Model(frozenset({"psx"}), frozenset(), frozenset({GAME.primary}))
        c = cleanup(devices={"deck": Lines((), (GAME.primary,))})
        after = trash.cleaned_model(model, c, "deck")
        self.assertEqual(after, wl.Model(frozenset({"psx"})))
        lib = library([("psx", OTHER)])
        self.assertTrue(trash.effect_holds(model, after, lib))
        # a drop that would turn another game on or off is not made
        other_off = wl.Model(frozenset({"psx"}), frozenset(), frozenset({OTHER.primary}))
        stray = cleanup(devices={"deck": Lines((), (OTHER.primary,))})
        self.assertFalse(
            trash.effect_holds(other_off, trash.cleaned_model(other_off, stray, "deck"), lib)
        )

    def test_the_cleanup_of_a_plan(self) -> None:
        c = trash.new_cleanup(made_plan(), ENTRY, NOW)
        assert c is not None
        self.assertEqual(
            (c.entry, c.id, c.system, c.recorded, c.paths),
            (ENTRY, GAME.id, "psx", None, (GAME.primary,)),
        )
        self.assertEqual(c.devices, {"deck": Lines((GAME.primary,), ())})


if __name__ == "__main__":
    unittest.main()
