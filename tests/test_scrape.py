"""Scraping with Skyscraper, the pure part: the platform table, which games to scrape, the
command lines and the classification of Skyscraper's output. No network, no Skyscraper."""

import re
import unittest
from datetime import date, timedelta

from library_support import game
from warpzone import artwork, inbox_dolphin, inbox_mame, inbox_n3ds, inbox_vita, scrape
from warpzone.dat import SYSTEMS
from warpzone.inbox_console_iso import FOLDERS as CONSOLE_FOLDERS

TODAY = date(2026, 11, 13)
ALL_KINDS = scrape.MEDIA_KINDS
# The credentials of a fake config.ini: none of their words may reach a command line.
FAKE_CREDS = "fakeuser:fakepassword"  # gitleaks:allow
FAKE_CONFIG = f'[main]\nlangPrios="es,en"\n\n[screenscraper]\nuserCreds="{FAKE_CREDS}"\n'


def inbox_systems() -> set[str]:
    """Every system folder `inbox` can place a game in."""
    return {
        *SYSTEMS.values(),  # the DAT systems
        *inbox_dolphin.SYSTEMS.values(),
        inbox_n3ds.SYSTEM,
        inbox_mame.SYSTEM,
        inbox_vita.SYSTEM,
        *CONSOLE_FOLDERS,  # ps3, xbox, xbox360, wiiu, ps4 (the kinds are the system names)
        "switch",
    }


def entry(path: str, tag: str = "game", **fields: str) -> artwork.Entry:
    return artwork.Entry(tag, path, dict(fields))


def media(stem: str, *kinds: str, ext: str = "png") -> set[str]:
    return {f"{k}/{stem}.{'mp4' if k == 'videos' else ext}" for k in kinds}


class PlatformTableTest(unittest.TestCase):
    def test_every_inbox_system_is_in_exactly_one_table(self):
        both = scrape.PLATFORMS.keys() & scrape.UNSCRAPED.keys()
        self.assertEqual(both, set(), "a system is in PLATFORMS and in UNSCRAPED")
        missing = inbox_systems() - scrape.PLATFORMS.keys() - scrape.UNSCRAPED.keys()
        self.assertEqual(missing, set(), "give these a Skyscraper platform or a reason")

    def test_no_system_that_inbox_cannot_place(self):
        extra = (scrape.PLATFORMS.keys() | scrape.UNSCRAPED.keys()) - inbox_systems()
        self.assertEqual(extra, set())

    def test_names_that_differ(self):
        self.assertEqual(scrape.PLATFORMS["genesis"], "megadrive")
        self.assertEqual(scrape.PLATFORMS["n3ds"], "3ds")
        self.assertEqual(scrape.PLATFORMS["sega32x"], "sega32x")
        self.assertEqual(scrape.PLATFORMS["atarijaguar"], "atarijaguar")

    def test_left_out_systems_have_a_reason(self):
        self.assertEqual(set(scrape.UNSCRAPED), {"ps4", "psvita", "xbox360", "wiiu"})
        self.assertTrue(all(scrape.UNSCRAPED.values()))


class ArgsTest(unittest.TestCase):
    def test_gather(self):
        args = scrape.skyscraper_args(
            "gather", "snes", input_dir="/work/scrape/snes/input", batch="/work/b1.txt"
        )
        self.assertEqual(
            args,
            [
                "-p",
                "snes",
                "-s",
                "screenscraper",
                "-i",
                "/work/scrape/snes/input",
                "--includefrom",
                "/work/b1.txt",
                "--flags",
                "onlymissing,videos,backcovers,unattend,nohints",
            ],
        )

    def test_main(self):
        args = scrape.skyscraper_args(
            "main",
            "megadrive",
            input_dir="/in",
            batch="/b.txt",
            gamelist_dir="/out/g",
            media_dir="/out/m",
            artwork_file="/data/artwork-esde.xml",
        )
        self.assertEqual(
            args,
            [
                "-p",
                "megadrive",
                "-f",
                "esde",
                "-i",
                "/in",
                "-g",
                "/out/g",
                "-o",
                "/out/m",
                "-a",
                "/data/artwork-esde.xml",
                "--includefrom",
                "/b.txt",
                "--flags",
                "miximages,videos,backcovers,theinfront,nobrackets,relative,unattend,nohints",
            ],
        )

    def test_box(self):
        args = scrape.skyscraper_args(
            "box",
            "3ds",
            input_dir="/in",
            batch="/b.txt",
            gamelist_dir="/box/g",
            media_dir="/box/m",
            artwork_file="/data/artwork-3dbox.xml",
        )
        self.assertEqual(
            args,
            [
                "-p",
                "3ds",
                "-f",
                "esde",
                "-i",
                "/in",
                "-g",
                "/box/g",
                "-o",
                "/box/m",
                "-a",
                "/data/artwork-3dbox.xml",
                "--includefrom",
                "/b.txt",
                "--flags",
                "theinfront,nobrackets,relative,unattend,nohints",
            ],
        )

    def test_only_gather_asks_for_the_source(self):
        gather = scrape.skyscraper_args("gather", "nes", input_dir="/i", batch="/b")
        paths = {"gamelist_dir": "/g", "media_dir": "/m", "artwork_file": "/a.xml"}
        for step in ("main", "box"):
            args = scrape.skyscraper_args(step, "nes", input_dir="/i", batch="/b", **paths)
            self.assertNotIn("-s", args)
        self.assertIn("-s", gather)
        self.assertNotIn("-g", gather)
        self.assertNotIn("-o", gather)

    def test_no_credential_option_for_any_platform_or_step(self):
        self.assertIn(FAKE_CREDS, FAKE_CONFIG)
        words = set(re.findall(r"[A-Za-z0-9]+", FAKE_CREDS))
        for platform in scrape.PLATFORMS.values():
            for step in ("gather", "main", "box"):
                args = scrape.skyscraper_args(
                    step,
                    platform,
                    input_dir="/i",
                    batch="/b",
                    gamelist_dir="/g",
                    media_dir="/m",
                    artwork_file="/a.xml",
                )
                self.assertFalse({"-u", "-c", "-t"} & set(args), (step, platform))
                self.assertFalse({"--userCreds", "--config", "--threads"} & set(args))
                line = " ".join(args)
                for word in words:
                    self.assertNotIn(word, line)

    def test_generate_steps_need_their_folders(self):
        for step in ("main", "box"):
            with self.assertRaises(ValueError):
                scrape.skyscraper_args(step, "nes", input_dir="/i", batch="/b")
        with self.assertRaises(ValueError):
            scrape.skyscraper_args("pass", "nes", input_dir="/i", batch="/b")


class SelectTest(unittest.TestCase):
    def pick(self, games, entries=(), present=(), **kwargs):
        kwargs.setdefault("today", TODAY)
        return scrape.select(games, list(entries), set(present), **kwargs)

    def test_game_without_entry(self):
        g = game("snes", "roms/snes/Game (USA).zip", "Game (USA)")
        sel = self.pick([g])
        self.assertEqual(len(sel.picks), 1)
        self.assertFalse(sel.picks[0].has_entry)
        self.assertEqual(sel.picks[0].missing, ALL_KINDS)
        self.assertEqual(sel.skipped_not_found, 0)

    def test_no_entry_lists_only_the_missing_kinds(self):
        g = game("snes", "roms/snes/Game.zip", "Game")
        sel = self.pick([g], present=media("Game", "covers", "videos"))
        self.assertFalse(sel.picks[0].has_entry)
        self.assertEqual(
            sel.picks[0].missing, tuple(k for k in ALL_KINDS if k not in ("covers", "videos"))
        )

    def test_entry_with_missing_kinds(self):
        g = game("snes", "roms/snes/Game.zip", "Game")
        present = media("Game", "covers", "screenshots", "marquees")
        sel = self.pick([g], [entry("./Game.zip", name="Game")], present)
        self.assertEqual(len(sel.picks), 1)
        self.assertTrue(sel.picks[0].has_entry)
        self.assertEqual(sel.picks[0].missing, ("miximages", "3dboxes", "backcovers", "videos"))

    def test_entry_without_dot_slash_still_matches(self):
        g = game("snes", "roms/snes/Game.zip", "Game")
        sel = self.pick([g], [entry("Game.zip")], media("Game", *ALL_KINDS))
        self.assertEqual(sel.picks, ())

    def test_complete_entry_is_left_alone(self):
        g = game("snes", "roms/snes/Game.zip", "Game")
        sel = self.pick([g], [entry("./Game.zip")], media("Game", *ALL_KINDS))
        self.assertEqual(sel.picks, ())

    def test_nomultiscrape_gets_nothing(self):
        g = game("snes", "roms/snes/Game.zip", "Game")
        entries = [entry("./Game.zip", nomultiscrape="true")]
        self.assertEqual(self.pick([g], entries).picks, ())
        self.assertEqual(self.pick([g], entries, media("Game", "covers")).picks, ())

    def test_nomultiscrape_false_is_an_ordinary_entry(self):
        g = game("snes", "roms/snes/Game.zip", "Game")
        sel = self.pick([g], [entry("./Game.zip", nomultiscrape="false")])
        self.assertEqual(len(sel.picks), 1)

    def test_media_in_another_extension_counts_as_present(self):
        g = game("snes", "roms/snes/Game.zip", "Game")
        present = {f"{k}/Game.jpg" for k in ALL_KINDS if k != "videos"} | {"videos/Game.webm"}
        self.assertEqual(self.pick([g], [entry("./Game.zip")], present).picks, ())
        jpeg = {f"{k}/Game.jpeg" for k in ALL_KINDS if k != "videos"} | {"videos/Game.mp4"}
        self.assertEqual(self.pick([g], [entry("./Game.zip")], jpeg).picks, ())

    def test_a_video_extension_does_not_count_for_an_image_kind(self):
        g = game("snes", "roms/snes/Game.zip", "Game")
        sel = self.pick([g], [entry("./Game.zip")], {"covers/Game.mp4"})
        self.assertIn("covers", sel.picks[0].missing)

    def test_other_games_media_does_not_count(self):
        g = game("snes", "roms/snes/Game.zip", "Game")
        sel = self.pick([g], [entry("./Game.zip")], media("Game 2", *ALL_KINDS))
        self.assertEqual(sel.picks[0].missing, ALL_KINDS)

    def test_subfolder_stem(self):
        g = game("gba", "roms/gba/Hacks/X.gba", "X", gamelist="./Hacks/X.gba")
        self.assertEqual(scrape.media_stem(g), "Hacks/X")
        sel = self.pick([g], [entry("./Hacks/X.gba")], media("Hacks/X", *ALL_KINDS))
        self.assertEqual(sel.picks, ())

    def test_m3u_folder_keeps_its_whole_name(self):
        g = game("psx", "roms/psx/Game.m3u", "Game", kind="dir", gamelist="./Game.m3u")
        self.assertEqual(scrape.media_stem(g), "Game.m3u")
        entries = [entry("./Game.m3u", tag="folder")]
        # covers/Game.png is the stem of a file, not of the folder
        self.assertEqual(len(self.pick([g], entries, media("Game", *ALL_KINDS)).picks), 1)
        self.assertEqual(self.pick([g], entries, media("Game.m3u", *ALL_KINDS)).picks, ())

    def test_m3u_file_primary_takes_the_stem_of_the_file(self):
        g = game("psx", "roms/psx/Game.m3u", "Game", kind="file", gamelist="./Game.m3u")
        self.assertEqual(scrape.media_stem(g), "Game")

    def test_order_is_kept(self):
        a = game("snes", "roms/snes/B.zip", "B")
        b = game("snes", "roms/snes/A.zip", "A")
        sel = self.pick([a, b])
        self.assertEqual([p.game for p in sel.picks], [a, b])

    def test_not_found_recently_with_the_same_size_is_skipped(self):
        g = game("snes", "roms/snes/Game.zip", "Game", size=500)
        for days in (0, 29):
            tried = TODAY - timedelta(days=days)
            sel = self.pick([g], not_found={"./Game.zip": scrape.NotFound(500, tried)})
            self.assertEqual(sel.picks, (), days)
            self.assertEqual(sel.skipped_not_found, 1, days)

    def test_not_found_31_days_ago_is_tried_again(self):
        g = game("snes", "roms/snes/Game.zip", "Game", size=500)
        record = scrape.NotFound(500, TODAY - timedelta(days=31))
        sel = self.pick([g], not_found={"./Game.zip": record})
        self.assertEqual(len(sel.picks), 1)
        self.assertEqual(sel.skipped_not_found, 0)

    def test_not_found_exactly_30_days_ago_is_tried_again(self):
        g = game("snes", "roms/snes/Game.zip", "Game", size=500)
        record = scrape.NotFound(500, TODAY - timedelta(days=30))
        self.assertEqual(len(self.pick([g], not_found={"./Game.zip": record}).picks), 1)

    def test_not_found_with_another_size_is_tried_again(self):
        g = game("snes", "roms/snes/Game.zip", "Game", size=501)
        for days in (29, 31):
            record = scrape.NotFound(500, TODAY - timedelta(days=days))
            sel = self.pick([g], not_found={"./Game.zip": record})
            self.assertEqual(len(sel.picks), 1, days)
            self.assertEqual(sel.skipped_not_found, 0, days)

    def test_retry_not_found(self):
        g = game("snes", "roms/snes/Game.zip", "Game", size=500)
        record = scrape.NotFound(500, TODAY - timedelta(days=1))
        sel = self.pick([g], not_found={"./Game.zip": record}, retry_not_found=True)
        self.assertEqual(len(sel.picks), 1)
        self.assertEqual(sel.skipped_not_found, 0)

    def test_not_found_is_counted_only_for_games_that_needed_a_scrape(self):
        done = game("snes", "roms/snes/Done.zip", "Done", size=500)
        record = scrape.NotFound(500, TODAY)
        sel = self.pick(
            [done],
            [entry("./Done.zip")],
            media("Done", *ALL_KINDS),
            not_found={"./Done.zip": record},
        )
        self.assertEqual((sel.picks, sel.skipped_not_found), ((), 0))

    def test_not_found_applies_to_a_game_with_an_entry_and_missing_media(self):
        g = game("snes", "roms/snes/Game.zip", "Game", size=500)
        record = scrape.NotFound(500, TODAY - timedelta(days=2))
        sel = self.pick([g], [entry("./Game.zip")], not_found={"./Game.zip": record})
        self.assertEqual((sel.picks, sel.skipped_not_found), ((), 1))


# The lines of Skyscraper 3.21.1 (src/screenscraper.cpp, src/scraperworker.cpp), with the
# colour codes it prints.
QUOTA = (
    "\033[1;31mYour daily ScreenScraper request limit has been reached, "
    "exiting nicely...\033[0m\n\n"
)
CLOSED = "\033[1;31mThe ScreenScraper API is currently closed, exiting nicely...\033[0m\n\n"
BLACKLISTED = (
    "\033[1;31mSkyscraper has apparently been blacklisted at ScreenScraper, "
    "exiting nicely...\033[0m\n\n"
)
BUSY = (
    "\033[1;31mThe screenscraper service is currently closed or too busy to handle requests from "
    "unregistered and inactive users. Sign up for an account at https://www.screenscraper.fr "
    "and contribute to gain more threads. Then use the credentials with Skyscraper using the "
    "'-u user:pass' command line option or by setting 'userCreds=\"user:pass\"' in "
    "'/home/user/.skyscraper/config.ini'.\033[0m\n\n"
)
RETRY = "\033[1;33mRetrying request with timeout of 60s...\033[0m\n\n"
NOT_FOUND = "\033[1;33m---- Game 'Example Homebrew (World)' not found :( ----\033[0m\n\n"
NOT_FOUND_CANDIDATES = (
    "\033[1;33m---- Game 'Odd Name (USA)' not found, but 2 candidates: Use --verbosity 3 or "
    "--flags interactive or --query '...' or set an alias (aliasMap.csv). =8^) ----\033[0m\n\n"
)
TOO_LOW = (
    "\033[1;33m---- Game 'Kirby's Fun Pak (USA)' match ('Kirby Fun', score: 33, threshold: 65) "
    "too low :| ----\033[0m\n\n"
)
FOUND = "\033[1;34m---- Game 'Chrono Trigger (USA)' found! :) ----\033[0m\n"


class ClassifyTest(unittest.TestCase):
    def test_quota(self):
        self.assertEqual(scrape.classify_output(QUOTA).stop, scrape.Stop.QUOTA)

    def test_closed(self):
        self.assertEqual(scrape.classify_output(CLOSED).stop, scrape.Stop.CLOSED)

    def test_blacklisted(self):
        self.assertEqual(scrape.classify_output(BLACKLISTED).stop, scrape.Stop.BLACKLISTED)

    def test_too_busy(self):
        self.assertEqual(scrape.classify_output(BUSY).stop, scrape.Stop.BUSY)
        self.assertEqual(scrape.classify_output(BUSY + BUSY).stop, scrape.Stop.BUSY)

    def test_busy_that_cleared_is_not_a_stop(self):
        text = BUSY + FOUND + NOT_FOUND
        self.assertIsNone(scrape.classify_output(text).stop)

    def test_no_network(self):
        out = scrape.classify_output(RETRY + RETRY)
        self.assertEqual(out, scrape.Outcome(scrape.Stop.NETWORK, ()))

    def test_a_retry_among_answered_games_is_not_a_stop(self):
        for retry in (RETRY, BUSY):
            out = scrape.classify_output(NOT_FOUND + retry + TOO_LOW)
            self.assertIsNone(out.stop)
            self.assertEqual(out.not_found, ("Example Homebrew (World)", "Kirby's Fun Pak (USA)"))

    def test_a_retry_that_worked_is_not_a_stop(self):
        self.assertIsNone(scrape.classify_output(RETRY + FOUND).stop)

    def test_quota_wins_over_a_game_found_before_it(self):
        out = scrape.classify_output(FOUND + QUOTA)
        self.assertEqual(out.stop, scrape.Stop.QUOTA)

    def test_not_found_lines(self):
        out = scrape.classify_output(FOUND + NOT_FOUND + NOT_FOUND_CANDIDATES + TOO_LOW)
        self.assertIsNone(out.stop)
        self.assertEqual(
            out.not_found,
            ("Example Homebrew (World)", "Odd Name (USA)", "Kirby's Fun Pak (USA)"),
        )

    def test_works_without_colour_codes_and_with_carriage_returns(self):
        plain = re.sub(r"\033\[[0-9;]*m", "", QUOTA)
        self.assertEqual(scrape.classify_output(plain).stop, scrape.Stop.QUOTA)
        text = "Response after 12s\r\n" + re.sub(r"\033\[[0-9;]*m", "", NOT_FOUND).replace(
            "\n", "\r\n"
        )
        self.assertEqual(scrape.classify_output(text).not_found, ("Example Homebrew (World)",))

    def test_unknown_text_is_not_a_failure(self):
        for text in ("", "Skyscraper v3.21.1\nSomething else entirely\n", "\033[1;32mdone\033[0m"):
            out = scrape.classify_output(text)
            self.assertEqual(out, scrape.Outcome(None, ()), repr(text))

    def test_found_games_alone(self):
        self.assertEqual(scrape.classify_output(FOUND), scrape.Outcome(None, ()))


if __name__ == "__main__":
    unittest.main()
