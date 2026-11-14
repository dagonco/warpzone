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


ROOT = "/work/scrape/snes/input"
SKY_FIELDS = {
    "name": "Kirby Super Star",
    "desc": "A collection of eight games.",
    "rating": "0.8",
    "releasedate": "19960920T000000",
    "developer": "HAL Laboratory",
    "publisher": "Nintendo",
    "genre": "Platform",
    "players": "1-2",
}


def staged_of(*games):
    """The staged map of a batch: the staged name is the gamelist path below the folder."""
    return {artwork.relative(g.gamelist): g for g in games}


def files(*paths, kind="file"):
    return {p: kind for p in paths}


def plan(games, *, hub=(), hub_media=(), sky=(), main=None, box=None):
    return scrape.plan_merge(
        staged_of(*games), ROOT, list(hub), set(hub_media), list(sky), main or {}, box or {}
    )


def only(merge_plan):
    (merged,) = merge_plan.games
    return merged


class MergeEntriesTest(unittest.TestCase):
    def setUp(self):
        self.g = game("snes", "roms/snes/Game (USA).zip", "Game (USA)")

    def test_existing_entry_is_left_alone(self):
        for hub_path in ("./Game (USA).zip", "Game (USA).zip"):
            with self.subTest(hub_path=hub_path):
                p = plan(
                    [self.g],
                    hub=[entry(hub_path, name="My name", playcount="3")],
                    hub_media=media("Game (USA)", *ALL_KINDS),
                    sky=[entry("./Game (USA).zip", **SKY_FIELDS)],
                )
                self.assertEqual(p.entries, ())
                self.assertEqual(p.copies, ())
                self.assertEqual(only(p).result, scrape.Result.UNCHANGED)
                self.assertTrue(only(p).had_entry)

    def test_existing_folder_entry_counts_as_existing(self):
        p = plan(
            [self.g], hub=[entry("./Game (USA).zip", tag="folder")], sky=[entry("Game (USA).zip")]
        )
        self.assertEqual(p.entries, ())

    def test_new_entry_has_only_the_listed_fields(self):
        sky = entry(
            "./Game (USA).zip",
            **SKY_FIELDS,
            image="./covers/Game (USA).png",
            video="./videos/Game (USA).mp4",
            marquee="./marquees/Game (USA).png",
            playcount="9",
            favorite="true",
            sortname="zzz",
            kidgame="false",
        )
        p = plan([self.g], sky=[sky])
        new = only(p).entry
        self.assertEqual(new, artwork.Entry("game", "./Game (USA).zip", SKY_FIELDS))
        self.assertEqual(only(p).result, scrape.Result.ADDED)
        self.assertFalse(only(p).had_entry)
        self.assertEqual(p.entries, (new,))

    def test_empty_and_missing_fields_are_left_out(self):
        p = plan([self.g], sky=[entry("./Game (USA).zip", name="Game", desc=" \x07 ", genre="")])
        self.assertEqual(only(p).entry.fields, {"name": "Game"})

    def test_catalog_path_is_written_not_skyscrapers(self):
        g = game("snes", "roms/snes/Hacks/Hack.zip", "Hack", gamelist="./Hacks/Hack.zip")
        p = plan([g], sky=[entry(f"{ROOT}/Hacks/Hack.zip", name="Hack")])
        self.assertEqual(only(p).entry.path, "./Hacks/Hack.zip")

    def test_absolute_and_dot_slash_paths_are_rewritten(self):
        games = [game("snes", f"roms/snes/G{i}.zip", f"G{i}") for i in range(3)]
        sky = [
            entry(f"{ROOT}/G0.zip", name="zero"),
            entry("./G1.zip", name="one"),
            entry("G2.zip", name="two"),
        ]
        p = plan(games, sky=sky)
        self.assertEqual([e.path for e in p.entries], ["./G0.zip", "./G1.zip", "./G2.zip"])
        self.assertEqual([e.fields["name"] for e in p.entries], ["zero", "one", "two"])
        self.assertEqual(p.dropped, ())

    def test_paths_outside_the_staged_folder_or_unknown_are_dropped(self):
        sky = [
            entry("/elsewhere/Game (USA).zip", name="outside"),
            entry(f"{ROOT}2/Game (USA).zip", name="lookalike"),
            entry("../Game (USA).zip", name="climbs"),
            entry("./Sub/../Game (USA).zip", name="dots"),
            entry(f"{ROOT}/../Game (USA).zip", name="absolute climb"),
            entry("./Unknown.zip", name="not staged"),
        ]
        p = plan([self.g], sky=sky)
        self.assertEqual(p.entries, ())
        self.assertEqual(len(p.dropped), 6)
        self.assertEqual(
            [d.what for d in p.dropped], [e.path for e in sky], "each is named in the warning"
        )
        self.assertTrue(all("dropped" in d.reason for d in p.dropped))
        self.assertEqual(only(p).result, scrape.Result.NOT_FOUND)

    def test_first_entry_for_a_game_wins(self):
        p = plan(
            [self.g],
            sky=[entry("./Game (USA).zip", name="one"), entry("Game (USA).zip", name="two")],
        )
        self.assertEqual(only(p).entry.fields["name"], "one")

    def test_m3u_folder_gets_a_game_entry(self):
        g = game("psx", "roms/psx/Disc Game.m3u", "Disc Game", kind="dir")
        p = plan([g], sky=[entry("./Disc Game.m3u", tag="folder", name="Disc Game")])
        self.assertEqual(
            only(p).entry, artwork.Entry("game", "./Disc Game.m3u", {"name": "Disc Game"})
        )

    def test_games_come_in_the_order_of_the_batch(self):
        a = game("snes", "roms/snes/B.zip", "B")
        b = game("snes", "roms/snes/A.zip", "A")
        self.assertEqual([m.game for m in plan([a, b]).games], [a, b])


class RenderEntriesTest(unittest.TestCase):
    def test_text_is_escaped_and_illegal_characters_dropped(self):
        new = artwork.Entry(
            "game",
            "./Tom & Jerry <1>.zip",
            {
                "name": "Tom & Jerry <1>",
                "desc": "a\x00b\x0bc \ud800 </desc><x>&amp;",
                "genre": "Action",
            },
        )
        text = scrape.render_entries([new])
        self.assertIn("<path>./Tom &amp; Jerry &lt;1&gt;.zip</path>", text)
        self.assertIn("<name>Tom &amp; Jerry &lt;1&gt;</name>", text)
        self.assertIn("<desc>abc  &lt;/desc&gt;&lt;x&gt;&amp;amp;</desc>", text)
        self.assertNotIn("\x00", text)
        # The entries read back as the text they were made from.
        back = artwork.parse_gamelist(f"<gameList>\n{text}</gameList>\n")
        self.assertEqual(back[0].path, "./Tom & Jerry <1>.zip")
        self.assertEqual(back[0].fields["name"], "Tom & Jerry <1>")
        self.assertEqual(back[0].fields["desc"], "abc  </desc><x>&amp;")

    def test_layout_follows_the_given_indent_and_newline(self):
        new = artwork.Entry("game", "./G.zip", {"genre": "RPG", "name": "G"})
        self.assertEqual(
            scrape.render_entries([new], indent="  ", eol="\r\n"),
            "  <game>\r\n    <path>./G.zip</path>\r\n    <name>G</name>\r\n"
            "    <genre>RPG</genre>\r\n  </game>\r\n",
        )

    def test_default_layout_and_no_entries(self):
        new = artwork.Entry("game", "./G.zip", {"name": "G"})
        self.assertEqual(
            scrape.render_entries([new, new]),
            "\t<game>\n\t\t<path>./G.zip</path>\n\t\t<name>G</name>\n\t</game>\n" * 2,
        )
        self.assertEqual(scrape.render_entries([]), "")


class MergeMediaTest(unittest.TestCase):
    def setUp(self):
        self.g = game("snes", "roms/snes/Game.zip", "Game")

    def test_every_kind_is_planned_from_its_pass(self):
        main = files(
            "covers/Game.png",
            "screenshots/Game.png",
            "miximages/Game.png",
            "marquees/Game.png",
            "backcovers/Game.png",
            "videos/Game.mp4",
        )
        box = files("covers/Game.png")
        p = plan([self.g], sky=[entry("./Game.zip")], main=main, box=box)
        by_kind = {c.kind: c for c in p.copies}
        self.assertEqual(tuple(by_kind), scrape.MEDIA_KINDS, "in MEDIA_KINDS order")
        self.assertEqual(
            by_kind["covers"],
            scrape.MediaCopy("covers", "main", "covers/Game.png", "covers/Game.png"),
        )
        self.assertEqual(
            by_kind["3dboxes"],
            scrape.MediaCopy("3dboxes", "box", "covers/Game.png", "3dboxes/Game.png"),
        )
        self.assertEqual(by_kind["videos"].dest, "videos/Game.mp4")
        self.assertEqual(only(p).kinds, scrape.MEDIA_KINDS)

    def test_the_box_pass_feeds_only_3dboxes(self):
        box = files("covers/Game.png", "screenshots/Game.png", "videos/Game.mp4")
        p = plan([self.g], box=box)
        self.assertEqual([c.kind for c in p.copies], ["3dboxes"])
        p = plan([self.g], main=files("covers/Game.png"))
        self.assertEqual([c.kind for c in p.copies], ["covers"], "main covers are not 3dboxes")

    def test_the_extension_is_kept(self):
        main = files("covers/Game.jpg", "marquees/Game.jpeg", "videos/Game.webm")
        p = plan([self.g], main=main)
        self.assertEqual(
            sorted(c.dest for c in p.copies),
            ["covers/Game.jpg", "marquees/Game.jpeg", "videos/Game.webm"],
        )

    def test_other_extensions_are_not_taken(self):
        p = plan([self.g], main=files("covers/Game.gif", "videos/Game.mkv", "covers/Game.png.tmp"))
        self.assertEqual(p.copies, ())

    def test_png_wins_over_jpg(self):
        p = plan([self.g], main=files("covers/Game.jpg", "covers/Game.png"))
        self.assertEqual([c.dest for c in p.copies], ["covers/Game.png"])

    def test_subfolder_is_kept(self):
        g = game("snes", "roms/snes/Hacks/Hack.zip", "Hack", gamelist="./Hacks/Hack.zip")
        p = plan([g], main=files("covers/Hacks/Hack.png", "videos/Hacks/Hack.mp4"))
        self.assertEqual(
            [(c.source, c.dest) for c in p.copies],
            [
                ("covers/Hacks/Hack.png", "covers/Hacks/Hack.png"),
                ("videos/Hacks/Hack.mp4", "videos/Hacks/Hack.mp4"),
            ],
        )

    def test_m3u_media_are_renamed_to_the_folder_name(self):
        g = game("psx", "roms/psx/Disc Game.m3u", "Disc Game", kind="dir")
        main = files("covers/Disc Game.png", "videos/Disc Game.mp4")
        box = files("covers/Disc Game.png")
        p = plan([g], sky=[entry("./Disc Game.m3u")], main=main, box=box)
        self.assertEqual(
            sorted((c.source, c.dest) for c in p.copies),
            [
                ("covers/Disc Game.png", "3dboxes/Disc Game.m3u.png"),
                ("covers/Disc Game.png", "covers/Disc Game.m3u.png"),
                ("videos/Disc Game.mp4", "videos/Disc Game.m3u.mp4"),
            ],
        )

    def test_symbolic_links_are_skipped_with_a_warning(self):
        main = {"covers/Game.png": "file", "videos/Game.mp4": "symlink"}
        p = plan([self.g], main=main)
        self.assertEqual([c.kind for c in p.copies], ["covers"])
        (warning,) = p.dropped
        self.assertEqual(warning.what, "main/videos/Game.mp4")
        self.assertIn("symbolic link", warning.reason)
        self.assertIn("`symlink`", warning.reason)

    def test_a_regular_file_in_another_extension_is_used_when_one_is_a_link(self):
        p = plan([self.g], main={"covers/Game.png": "symlink", "covers/Game.jpg": "file"})
        self.assertEqual([c.dest for c in p.copies], ["covers/Game.jpg"])
        self.assertEqual(len(p.dropped), 1)

    def test_other_file_types_are_skipped(self):
        p = plan([self.g], main={"covers/Game.png": "other"})
        self.assertEqual(p.copies, ())
        self.assertEqual(len(p.dropped), 1)

    def test_nothing_is_planned_over_an_existing_file(self):
        main = files("covers/Game.png", "screenshots/Game.png", "videos/Game.mp4")
        present = {"covers/Game.jpg", "videos/Game.webm"}  # other extensions of the kind count
        p = plan([self.g], hub_media=present, main=main)
        self.assertEqual([c.kind for c in p.copies], ["screenshots"])

    def test_a_link_over_an_existing_file_is_not_a_warning(self):
        p = plan([self.g], hub_media={"videos/Game.mp4"}, main={"videos/Game.mp4": "symlink"})
        self.assertEqual((p.copies, p.dropped), ((), ()))

    def test_existing_entry_gets_the_media_it_lacks(self):
        present = media(
            "Game", "covers", "screenshots", "miximages", "marquees", "3dboxes", "backcovers"
        )
        p = plan(
            [self.g],
            hub=[entry("./Game.zip", name="Mine")],
            hub_media=present,
            sky=[entry("./Game.zip", name="Theirs")],
            main=files("covers/Game.png", "videos/Game.mp4"),
        )
        merged = only(p)
        self.assertEqual(merged.result, scrape.Result.FILLED)
        self.assertTrue(merged.had_entry)
        self.assertIsNone(merged.entry)
        self.assertEqual(merged.kinds, ("videos",))

    def test_nomultiscrape_gets_nothing_at_all(self):
        p = plan(
            [self.g],
            hub=[entry("./Game.zip", nomultiscrape="true")],
            sky=[entry("./Game.zip", name="Theirs")],
            main=files("covers/Game.png"),
            box=files("covers/Game.png"),
        )
        self.assertEqual((p.entries, p.copies), ((), ()))
        self.assertEqual(only(p).result, scrape.Result.UNCHANGED)

    def test_no_entry_and_media_only_is_a_fill(self):
        p = plan([self.g], main=files("covers/Game.png"))
        merged = only(p)
        self.assertEqual(merged.result, scrape.Result.FILLED)
        self.assertFalse(merged.had_entry)
        self.assertIsNone(merged.entry)

    def test_new_entry_without_media(self):
        merged = only(plan([self.g], sky=[entry("./Game.zip", name="Game")]))
        self.assertEqual(merged.result, scrape.Result.ADDED)
        self.assertEqual(merged.copies, ())

    def test_nothing_from_skyscraper_is_not_found(self):
        self.assertEqual(only(plan([self.g])).result, scrape.Result.NOT_FOUND)
        with_entry = plan([self.g], hub=[entry("./Game.zip")])
        self.assertEqual(only(with_entry).result, scrape.Result.NOT_FOUND)

    def test_media_the_hub_already_has_is_not_a_miss(self):
        p = plan(
            [self.g],
            hub=[entry("./Game.zip")],
            hub_media={"covers/Game.png"},
            main=files("covers/Game.png"),
        )
        self.assertEqual(only(p).result, scrape.Result.UNCHANGED)

    def test_two_games_never_plan_the_same_destination(self):
        a = game("snes", "roms/snes/A.zip", "A", gamelist="./Same.zip")
        b = game("snes", "roms/snes/B.zip", "B", gamelist="./Same.zip")
        p = scrape.plan_merge(
            {"A.zip": a, "B.zip": b}, ROOT, [], set(), [], files("covers/A.png", "covers/B.png"), {}
        )
        self.assertEqual([c.dest for c in p.copies], ["covers/Same.png"])


class ReportTest(unittest.TestCase):
    def merges(self):
        kirby = game("snes", "roms/snes/Kirby Super Star (USA).zip", "Kirby Super Star (USA)")
        chrono = game("snes", "roms/snes/Chrono Trigger (USA).zip", "Chrono Trigger (USA)")
        homebrew = game("snes", "roms/snes/Example Homebrew (World).zip", "Example Homebrew")
        done = game("snes", "roms/snes/Done (USA).zip", "Done (USA)")
        mc = scrape.MediaCopy
        return (
            scrape.GameMerge(
                kirby,
                scrape.Result.ADDED,
                False,
                artwork.Entry("game", kirby.gamelist),
                tuple(mc(k, "main", "x", "y") for k in scrape.MEDIA_KINDS),
            ),
            scrape.GameMerge(
                chrono,
                scrape.Result.FILLED,
                True,
                None,
                (mc("3dboxes", "box", "x", "y"), mc("videos", "main", "x", "y")),
            ),
            scrape.GameMerge(homebrew, scrape.Result.NOT_FOUND, False),
            scrape.GameMerge(done, scrape.Result.UNCHANGED, True),
        )

    def test_the_designed_report(self):
        merges = self.merges()
        report = scrape.Report("snes", merges[:3], version="3.21.1", account=True)
        self.assertEqual(
            scrape.render_report(report),
            "scrape: snes: 3 games (Skyscraper 3.21.1, ScreenScraper account: set)\n"
            "  added      Kirby Super Star (USA).zip: covers, screenshots, miximages, marquees, "
            "3dboxes, backcovers, videos\n"
            "  filled     Chrono Trigger (USA).zip: 3dboxes, videos (entry kept)\n"
            "  not found  Example Homebrew (World).zip\n"
            "scrape: snes: 1 added, 1 filled, 1 not found",
        )

    def test_a_complete_game_is_counted_and_not_listed(self):
        text = scrape.render_report(scrape.Report("snes", self.merges(), version="3.21.1"))
        self.assertNotIn("Done (USA)", text)
        self.assertIn("ScreenScraper account: anonymous", text)
        self.assertTrue(text.endswith("scrape: snes: 1 added, 1 filled, 1 not found, 1 unchanged"))
        self.assertTrue(text.startswith("scrape: snes: 4 games"))

    def test_skipped_not_found_is_counted(self):
        report = scrape.Report("snes", self.merges()[:1], skipped_not_found=4)
        self.assertTrue(
            scrape.render_report(report).endswith(
                "scrape: snes: 1 added, skipped 4 not found in the last 30 days"
            )
        )

    def test_nothing_to_do(self):
        self.assertEqual(
            scrape.render_report(scrape.Report("gba", ())),
            "scrape: gba: 0 games (Skyscraper, ScreenScraper account: anonymous)\n"
            "scrape: gba: nothing to do",
        )
        text = scrape.render_report(scrape.Report("gba", self.merges()[3:], skipped_not_found=2))
        self.assertTrue(text.startswith("scrape: gba: 1 game ("))

    def test_added_without_media_and_filled_without_an_entry(self):
        g = game("snes", "roms/snes/Hacks/Hack.zip", "Hack", gamelist="./Hacks/Hack.zip")
        a = scrape.GameMerge(g, scrape.Result.ADDED, False, artwork.Entry("game", g.gamelist))
        f = scrape.GameMerge(
            g, scrape.Result.FILLED, False, None, (scrape.MediaCopy("covers", "main", "x", "y"),)
        )
        lines = scrape.render_report(scrape.Report("snes", (a, f))).splitlines()
        self.assertEqual(lines[1], "  added      Hacks/Hack.zip: no media")
        self.assertEqual(lines[2], "  filled     Hacks/Hack.zip: covers")

    def test_dropped_things_are_warnings(self):
        dropped = (
            scrape.Dropped(
                "main/videos/Game.mp4",
                "is a symbolic link (Skyscraper's `symlink` option); skipped",
            ),
        )
        text = scrape.render_report(scrape.Report("snes", (), dropped))
        self.assertIn(
            "  warning    main/videos/Game.mp4: is a symbolic link "
            "(Skyscraper's `symlink` option); skipped",
            text,
        )

    def test_report_from_a_plan(self):
        g = game("snes", "roms/snes/Game.zip", "Game")
        p = plan([g], sky=[entry("./Game.zip", name="Game")], main=files("covers/Game.png"))
        text = scrape.render_report(scrape.Report("snes", p.games, p.dropped))
        self.assertIn("  added      Game.zip: covers", text)


class SummaryTest(unittest.TestCase):
    def test_the_counts_are_the_ones_the_report_prints(self):
        merges = ReportTest().merges()
        dropped = (scrape.Dropped("covers/x.png", "appeared meanwhile; kept as it is"),)
        report = scrape.Report("snes", merges, dropped, skipped_not_found=2)
        found = scrape.summarize_system(report, skipped_incomplete=3)
        self.assertEqual(
            found,
            scrape.SystemSummary(
                "snes",
                added=1,
                filled=1,
                unchanged=1,
                not_found=1,
                skipped_not_found=2,
                skipped_incomplete=3,
                added_paths=("Kirby Super Star (USA).zip",),
                filled_paths=("Chrono Trigger (USA).zip",),
                warnings=("covers/x.png: appeared meanwhile; kept as it is",),
            ),
        )

    def test_two_runs_one_after_the_other_merge_into_one_summary(self):
        first = scrape.Summary(
            (
                scrape.SystemSummary("nes", added=1, added_paths=("A.zip",)),
                scrape.SystemSummary("snes", filled=2, filled_paths=("B.zip", "C.zip")),
            ),
            not_tried=1,
            failed_systems=("nes",),
            batches=2,
        )
        then = scrape.Summary(
            (
                scrape.SystemSummary("snes", added=1, not_found=1, added_paths=("D.zip",)),
                scrape.SystemSummary("gba", unchanged=3),
            ),
            stop=scrape.StopReason.DEADLINE,
            not_tried=4,
            failed_systems=("nes", "gba"),
            failed=True,
            batches=1,
        )
        merged = scrape.merge_summaries(first, then)
        self.assertEqual([s.system for s in merged.systems], ["nes", "snes", "gba"])
        snes = merged.systems[1]
        self.assertEqual((snes.added, snes.filled, snes.not_found), (1, 2, 1))
        self.assertEqual((snes.added_paths, snes.filled_paths), (("D.zip",), ("B.zip", "C.zip")))
        self.assertEqual(merged.stop, scrape.StopReason.DEADLINE)
        self.assertEqual((merged.not_tried, merged.batches, merged.failed), (5, 3, True))
        self.assertEqual(merged.failed_systems, ("nes", "gba"))
        first_stop = scrape.merge_summaries(
            scrape.Summary(stop=scrape.StopReason.QUOTA), scrape.Summary(stop=then.stop)
        )
        self.assertEqual(first_stop.stop, scrape.StopReason.QUOTA)

    def test_a_stop_maps_to_its_reason(self):
        for stop in scrape.Stop:
            self.assertEqual(scrape.StopReason.of(stop).value, stop.value)

    def test_the_budget_and_the_exit_code(self):
        self.assertTrue(scrape.Summary(stop=scrape.StopReason.DEADLINE).budget_spent)
        self.assertFalse(scrape.Summary(stop=scrape.StopReason.QUOTA).budget_spent)
        self.assertEqual(scrape.Summary().exit_code, 0)
        self.assertEqual(scrape.Summary(failed=True).exit_code, 1)

    def test_the_messages_count_the_games(self):
        self.assertEqual(scrape.budget_message(1), "scrape: time budget spent; 1 game not tried")
        self.assertIn("2 games not tried", scrape.lock_lost_message(2))


class FailureMessageTest(unittest.TestCase):
    def test_quota(self):
        self.assertEqual(
            scrape.stop_message(scrape.Stop.QUOTA, not_tried=12),
            "quota: ScreenScraper's daily request limit is reached; 12 games not tried. "
            "Run warpzone scrape again tomorrow.",
        )
        self.assertIn("1 game not tried", scrape.stop_message(scrape.Stop.QUOTA, not_tried=1))

    def test_busy_hints_at_an_account_only_without_one(self):
        anonymous = scrape.stop_message(scrape.Stop.BUSY)
        self.assertIn("too busy for anonymous users", anonymous)
        self.assertIn("userCreds", anonymous)
        self.assertIn("[screenscraper]", anonymous)
        with_account = scrape.stop_message(scrape.Stop.BUSY, account=True)
        self.assertNotIn("userCreds", with_account)

    def test_the_other_reasons(self):
        self.assertIn("closed", scrape.stop_message(scrape.Stop.CLOSED))
        self.assertIn("refused", scrape.stop_message(scrape.Stop.BLACKLISTED))
        self.assertEqual(
            scrape.stop_message(scrape.Stop.NETWORK), "ScreenScraper could not be reached"
        )

    def test_every_stop_has_a_message_and_none_holds_a_credential(self):
        for stop in scrape.Stop:
            text = scrape.stop_message(stop, not_tried=3)
            self.assertTrue(text, stop)
            self.assertNotIn(FAKE_CREDS, text)

    def test_skyscraper_failed(self):
        self.assertEqual(
            scrape.failed_message("snes", "/work/scrape/snes.log"),
            "Skyscraper failed on snes (see /work/scrape/snes.log)",
        )

    def test_gamelist_not_written(self):
        self.assertEqual(
            scrape.gamelist_message(
                "snes", "the gamelist changed three times while it was written"
            ),
            "snes: the gamelist changed three times while it was written; "
            "the entries are left for the next run",
        )


if __name__ == "__main__":
    unittest.main()
