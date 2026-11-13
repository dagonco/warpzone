"""The scrape at the end of `inbox`: `--apply` scrapes what it placed (and only that) with a
stand-in Skyscraper, a failure is a warning that changes nothing else, and without `--apply` or
without Skyscraper the report says what would not happen. No network and no real Skyscraper."""

import os
import unittest
from pathlib import Path
from unittest import mock

from scrape_support import FAKE_CONFIG, FAKE_CREDS, Hub
from test_cli import run as run_cli
from test_inbox import NES_ROM, Base
from warpzone import artwork, catalog, catalog_store, inbox_core, inbox_scrape, scrape, scrape_io
from warpzone.config import InboxSettings
from warpzone.inbox import run_inbox
from warpzone.inbox_core import InboxError, ItemResult, Tools, UnitResult
from warpzone.report import Reporter

GAME = "Adventure (USA)"
ENTRY = f"./{GAME}.zip"
KINDS = ("covers", "screenshots", "miximages", "marquees", "backcovers", "videos")


def unit(system: str, entry: str, outcome: str = "placed", folder: str = "") -> ItemResult:
    result = ItemResult(entry)
    result.units.append(UnitResult(system, entry, outcome, folder=folder))
    return result


class CandidateTests(unittest.TestCase):
    def test_only_roms_of_scraped_systems_with_the_outcome_are_taken(self) -> None:
        results = [
            unit("nes", "A.zip"),
            unit("nes", "B.zip"),
            unit("nes", "C.zip", "already"),
            unit("psx", "D.m3u"),
            unit("ps4", "E", folder="roms/ps4"),
            unit("switch", "U.nsp", folder="extras/switch/updates"),
        ]
        self.assertEqual(
            inbox_scrape.candidates(results, "placed"),
            {"nes": ["roms/nes/A.zip", "roms/nes/B.zip"], "psx": ["roms/psx/D.m3u"]},
        )
        self.assertEqual(inbox_scrape.candidates(results, "planned"), {})

    def test_a_game_is_placed_when_its_primary_is_the_entry_or_inside_it(self) -> None:
        from library_support import game

        single = game("nes", "roms/nes/A.zip", "A")
        playlist = game("psx", "roms/psx/D.m3u/D.m3u", "D")
        other = game("nes", "roms/nes/AB.zip", "AB")
        placed = ["roms/nes/A.zip", "roms/psx/D.m3u"]
        self.assertTrue(scrape.is_placed(single, placed))
        self.assertTrue(scrape.is_placed(playlist, placed))
        self.assertFalse(scrape.is_placed(other, placed))

    def test_a_configured_skyscraper_that_does_not_exist_is_a_configuration_error(self) -> None:
        with self.assertRaises(InboxError):
            inbox_core.find_tools(InboxSettings(skyscraper="/nonexistent/Skyscraper"))
        found = inbox_core.find_tools(InboxSettings(), which=lambda name: f"/usr/bin/{name}")
        self.assertEqual(found.skyscraper, "/usr/bin/Skyscraper")


class ScrapeAfterInboxCase(Base):
    def setUp(self) -> None:
        super().setUp()
        self.sky = Hub(self.tmp / "sky")  # only its stand-in tool and its helpers are used
        self.sky.canned({"known": [GAME]})
        self.config_ini = self.tmp / "config.ini"
        self.config_ini.write_text(FAKE_CONFIG)
        self.cache = self.tmp / "scrape-cache"
        self.nes_gamelist = scrape_io.gamelist_path(self.hub, "nes")
        self.drop("a.nes", NES_ROM)

    def tools(self, program: str | None = "stand-in") -> Tools:
        path = str(self.sky.tool) if program == "stand-in" else program
        return Tools(chdman=self.chdman, rahasher=self.rahasher, skyscraper=path)

    def go_inbox(self, *, apply: bool = True, program: str | None = "stand-in", **kw) -> tuple:  # type: ignore[type-arg,no-untyped-def]
        self.lines.clear()
        rep = Reporter(self.lines.append)
        options = self.options(
            apply=apply,
            tools=self.tools(program),
            scrape_cache_dir=self.cache,
            skyscraper_config=self.config_ini,
            **kw,
        )
        return run_inbox(options, rep), rep

    def media(self, kind: str) -> Path:
        ext = "mp4" if kind == "videos" else "png"
        return scrape_io.media_path(self.hub, "nes") / kind / f"{GAME}.{ext}"

    def assert_not_scraped(self) -> None:
        self.assertEqual(self.sky.calls(), [])
        self.assertFalse(self.nes_gamelist.exists())
        self.assertFalse(scrape_io.media_path(self.hub, "nes").exists())

    def assert_placed(self) -> None:
        self.assertEqual(self.library(), [f"nes/{GAME}.zip"])
        self.assertEqual(self.done(), ["a.nes"])
        self.assertFalse((self.inbox / ".warpzone.lock").exists())


class ApplyTests(ScrapeAfterInboxCase):
    def test_the_placed_game_gets_its_entry_and_media_inside_the_inbox_lock(self) -> None:
        code, rep = self.go_inbox()
        self.assertEqual(code, 0, self.out)
        self.assertEqual(rep.warnings, [])
        self.assert_placed()
        entries = {e.path: e.fields for e in artwork.parse_gamelist(self.nes_gamelist.read_text())}
        self.assertEqual(entries[ENTRY]["desc"], f"About {GAME} & more")
        for kind in (*KINDS, "3dboxes"):
            self.assertTrue(self.media(kind).is_file(), kind)
        self.assertIn(
            "scrape: nes: 1 game (Skyscraper 3.21.1, ScreenScraper account: set)", self.out
        )
        self.assertIn("added", self.out)
        self.assertNotIn(FAKE_CREDS, self.out + "".join(rep.warnings))
        # The scrape ran before the closing summary of inbox.
        self.assertLess(self.out.index("scrape: nes"), self.out.index("items: 1;"))

    def test_only_the_games_this_run_placed_are_scraped(self) -> None:
        older = self.roms / "nes" / "Older (USA).zip"
        older.write_bytes(b"older")
        code, _ = self.go_inbox()
        self.assertEqual(code, 0, self.out)
        (gathered,) = self.sky.gathers()
        self.assertEqual(sorted(gathered["targets"]), [f"{GAME}.zip"])
        self.assertNotIn("Older", self.nes_gamelist.read_text())

    def test_a_game_already_in_the_library_is_not_scraped(self) -> None:
        self.go_inbox(program=None)
        self.drop("b.nes", NES_ROM)  # the same content again
        self.sky.canned({"known": [GAME]})
        code, _ = self.go_inbox()
        self.assertEqual(code, 0, self.out)
        self.assertIn("already in the library", self.out)
        self.assertEqual(self.sky.calls(), [])

    def test_a_full_quota_is_a_warning_and_the_game_stays_placed(self) -> None:
        self.sky.canned({"known": [GAME], "cached": [], "quota_after": 0})
        code, rep = self.go_inbox()
        self.assertEqual(code, 0, self.out)
        self.assert_placed()
        self.assertEqual(len(rep.warnings), 1, rep.warnings)
        self.assertIn("ScreenScraper's daily request limit is reached", rep.warnings[0])
        self.assertIn("scrape: not finished; run `warpzone scrape --system nes` later", self.out)
        self.assertFalse(self.nes_gamelist.exists())
        self.assertEqual(self.review(), [])

    def test_a_failing_skyscraper_is_a_warning_and_changes_neither_exit_code_nor_review(
        self,
    ) -> None:
        self.sky.canned({"known": [GAME], "gather_exit": 3})
        code, rep = self.go_inbox()
        self.assertEqual(code, 0, self.out)
        self.assert_placed()
        self.assertEqual(self.review(), [])
        self.assertTrue(any("Skyscraper failed on nes" in w for w in rep.warnings), rep.warnings)
        self.assertIn("scrape: not finished", self.out)

    def test_an_unexpected_error_in_the_scrape_is_a_warning_too(self) -> None:
        with mock.patch.object(scrape_io, "run_placed", side_effect=RuntimeError("boom")):
            code, rep = self.go_inbox()
        self.assertEqual(code, 0, self.out)
        self.assert_placed()
        self.assertEqual(rep.warnings, ["scrape: failed (RuntimeError: boom)"])
        self.assertIn("scrape: not finished; run `warpzone scrape --system nes` later", self.out)

    def test_a_game_for_review_still_returns_the_review_count_when_the_scrape_runs(self) -> None:
        self.drop("mystery.nes", b"NES\x1a" + b"unknown" * 100)
        code, rep = self.go_inbox()
        self.assertEqual(code, 1, self.out)  # inbox's own count, as without a scrape
        self.assertEqual(rep.warnings, [])
        self.assertTrue(self.media("covers").is_file())

    def assert_left_for_later(self, rep: Reporter) -> None:
        self.assert_placed()
        self.assertEqual(self.review(), [])
        self.assertEqual([c for c in self.sky.calls() if c != ["--version"]], [])  # no request
        self.assertFalse(self.nes_gamelist.exists())
        message = "scrape: nes: 1 placed game not in the catalog; run `warpzone catalog`, then "
        self.assertIn(message + "`warpzone scrape --system nes`", rep.warnings)
        self.assertIn("scrape: not finished; run `warpzone scrape --system nes` later", self.out)

    def test_a_failed_catalog_update_leaves_the_scrape_for_later(self) -> None:
        with mock.patch.object(catalog_store, "update_after_inbox", side_effect=OSError("disk")):
            code, rep = self.go_inbox()
        self.assertEqual(code, 0, self.out)
        self.assertIn("catalog: not updated", "".join(rep.warnings))
        self.assert_left_for_later(rep)

    def test_games_missing_from_an_existing_catalog_are_reported_the_same_way(self) -> None:
        from library_support import game

        target = catalog_store.system_path(self.hub, "nes")
        target.parent.mkdir(parents=True, exist_ok=True)
        older = game("nes", "roms/nes/Older (USA).zip", "Older (USA)")
        target.write_bytes(catalog.encode("nes", [older], "2026-11-13T00:00:00+00:00"))
        with mock.patch.object(catalog_store, "update_after_inbox"):
            code, rep = self.go_inbox()
        self.assertEqual(code, 0, self.out)
        self.assert_left_for_later(rep)

    def test_without_skyscraper_the_game_is_placed_and_a_note_says_it_was_not_scraped(self) -> None:
        code, rep = self.go_inbox(program=None)
        self.assertEqual(code, 0, self.out)
        self.assert_placed()
        self.assertEqual(rep.warnings, [])
        self.assertIn("scrape: Skyscraper not found; 1 game placed, not scraped.", self.out)
        self.assertIn("warpzone scrape", self.out)
        self.assert_not_scraped()

    def test_no_scrape_skips_it_for_one_run(self) -> None:
        code, _ = self.go_inbox(no_scrape=True)
        self.assertEqual(code, 0, self.out)
        self.assert_placed()
        self.assertIn(inbox_scrape.SKIPPED, self.out)
        self.assert_not_scraped()

    def test_nothing_placed_prints_nothing_about_scraping(self) -> None:
        for name in os.listdir(self.inbox):
            if name.endswith(".nes"):
                (self.inbox / name).unlink()
        code, _ = self.go_inbox()
        self.assertEqual(code, 0, self.out)
        self.assertNotIn("scrape", self.out)
        self.assertEqual(self.sky.calls(), [])

    def test_a_game_of_a_system_that_is_not_scraped_is_not_sent_to_skyscraper(self) -> None:
        with mock.patch.dict(scrape.PLATFORMS, clear=True):
            code, _ = self.go_inbox()
        self.assertEqual(code, 0, self.out)
        self.assertNotIn("scrape", self.out)
        self.assertEqual(self.sky.calls(), [])


class ReportOnlyTests(ScrapeAfterInboxCase):
    def test_the_report_says_how_many_games_would_be_scraped_and_runs_nothing(self) -> None:
        code, _ = self.go_inbox(apply=False)
        self.assertEqual(code, 0, self.out)
        self.assertIn("scrape: nes: would scrape 1 game", self.out)
        self.assertEqual(self.library(), [])
        self.assert_not_scraped()

    def test_without_skyscraper_it_says_why_not(self) -> None:
        self.go_inbox(apply=False, program=None)
        self.assertIn("scrape: Skyscraper not found; 1 game would not be scraped.", self.out)
        self.assert_not_scraped()

    def test_no_scrape_is_reported(self) -> None:
        self.go_inbox(apply=False, no_scrape=True)
        self.assertIn(inbox_scrape.SKIPPED, self.out)
        self.assert_not_scraped()


class CliTests(ScrapeAfterInboxCase):
    def cli(self, *args: str, skyscraper: bool = True) -> tuple[int, str, str]:
        config = self.tmp / "config.toml"
        sky = f'skyscraper = "{self.sky.tool}"\n' if skyscraper else ""
        config.write_text(
            f'[inbox]\nroot = "{self.hub}"\ndats_dir = "{self.dats}"\n'
            f'work_dir = "{self.tmp / "work"}"\nchdman = "{self.chdman}"\n{sky}'
        )
        env = {"XDG_CACHE_HOME": str(self.tmp / "xdg"), "HOME": str(self.tmp / "home")}
        with mock.patch.dict(os.environ, env):
            return run_cli("inbox", "--config", str(config), *args)

    def test_apply_scrapes_through_the_configured_skyscraper(self) -> None:
        code, out, err = self.cli("--apply")
        self.assertEqual(code, 0, out + err)
        self.assertIn(
            "scrape: nes: 1 game (Skyscraper 3.21.1, ScreenScraper account: anonymous)", out
        )
        self.assertTrue(self.media("covers").is_file())

    def test_no_scrape_flag(self) -> None:
        code, out, err = self.cli("--apply", "--no-scrape")
        self.assertEqual(code, 0, out + err)
        self.assertIn(inbox_scrape.SKIPPED, out)
        self.assert_not_scraped()

    def test_a_missing_configured_skyscraper_is_a_configuration_error(self) -> None:
        self.sky.tool.unlink()
        code, out, err = self.cli("--apply")
        self.assertEqual(code, 2, out + err)
        self.assertIn("[inbox] skyscraper", err)
        self.assertEqual(self.library(), [])  # nothing was placed


if __name__ == "__main__":
    unittest.main()
