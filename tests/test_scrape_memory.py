"""Scraping, the pure part added with the I/O: the retry rule and the not-found memory, the
presence check of the account in Skyscraper's config, adding entries to a gamelist text, the
choice of systems and the dry-run text. No network, no Skyscraper."""

import unittest
from datetime import date, timedelta
from pathlib import Path

from library_support import game
from test_scrape import (
    BUSY,
    FAKE_CONFIG,
    FOUND,
    NOT_FOUND,
    NOT_FOUND_CANDIDATES,
    RETRY,
    TOO_LOW,
    entry,
)
from warpzone import artwork, gamelist, scrape

TODAY = date(2026, 11, 13)


class RetryTest(unittest.TestCase):
    """A game answered "not found" after a retry line is not trusted."""

    def test_only_the_game_that_followed_a_retry_is_retried(self):
        out = scrape.classify_output(NOT_FOUND + RETRY + RETRY + NOT_FOUND_CANDIDATES + TOO_LOW)
        self.assertIsNone(out.stop)
        self.assertEqual(
            out.not_found, ("Example Homebrew (World)", "Odd Name (USA)", "Kirby's Fun Pak (USA)")
        )
        self.assertEqual(out.retried, ("Odd Name (USA)",))

    def test_the_too_busy_line_counts_as_a_retry(self):
        out = scrape.classify_output(BUSY + NOT_FOUND + NOT_FOUND_CANDIDATES)
        self.assertEqual(out.retried, ("Example Homebrew (World)",))

    def test_screenscraper_down_every_game_is_retried_and_not_found(self):
        text = RETRY + RETRY + NOT_FOUND + RETRY + TOO_LOW + RETRY + NOT_FOUND_CANDIDATES
        out = scrape.classify_output(text)
        self.assertIsNone(out.stop)  # it answered "not found": not a NETWORK stop
        self.assertEqual(out.retried, out.not_found)
        self.assertEqual(out.answered, 3)

    def test_a_found_game_clears_the_retry(self):
        out = scrape.classify_output(RETRY + FOUND + NOT_FOUND)
        self.assertEqual(out.retried, ())
        self.assertEqual(out.answered, 2)

    def test_no_retry_no_taint(self):
        self.assertEqual(scrape.classify_output(NOT_FOUND + TOO_LOW).retried, ())


class RememberNotFoundTest(unittest.TestCase):
    def merge(self, path: str, result: scrape.Result, size: int = 100) -> scrape.GameMerge:
        g = game("snes", f"roms/snes/{path}", path, size=size, gamelist=f"./{path}")
        return scrape.GameMerge(g, result, False)

    def remember(self, records, merges, asked=(), retried=()):
        return scrape.remember_not_found(records, merges, set(asked), set(retried), TODAY)

    def test_a_game_skyscraper_did_not_find_is_remembered_with_size_and_day(self):
        merges = [self.merge("A.zip", scrape.Result.NOT_FOUND, size=42)]
        out = self.remember({}, merges, asked=["A"])
        self.assertEqual(out, {"./A.zip": scrape.NotFound(42, TODAY)})

    def test_a_game_answered_after_a_retry_is_not_remembered(self):
        merges = [
            self.merge("A.zip", scrape.Result.NOT_FOUND),
            self.merge("B.zip", scrape.Result.NOT_FOUND),
        ]
        out = self.remember({}, merges, asked=["A", "B"], retried=["A"])
        self.assertEqual(set(out), {"./B.zip"})

    def test_a_game_it_never_got_to_is_not_remembered(self):
        merges = [self.merge("A.zip", scrape.Result.NOT_FOUND)]
        self.assertEqual(self.remember({}, merges, asked=[]), {})

    def test_a_game_found_later_drops_its_record(self):
        old = {"./A.zip": scrape.NotFound(100, TODAY - timedelta(days=40))}
        for result in (scrape.Result.ADDED, scrape.Result.FILLED, scrape.Result.UNCHANGED):
            self.assertEqual(self.remember(old, [self.merge("A.zip", result)]), {})

    def test_other_records_are_kept_and_the_input_is_not_changed(self):
        old = {"./Z.zip": scrape.NotFound(7, TODAY)}
        out = self.remember(old, [self.merge("A.zip", scrape.Result.NOT_FOUND)], asked=["A"])
        self.assertEqual(set(out), {"./Z.zip", "./A.zip"})
        self.assertEqual(set(old), {"./Z.zip"})

    def test_a_name_in_a_subfolder_is_matched_by_its_stem(self):
        g = game("snes", "roms/snes/Hacks/A.zip", "A", gamelist="./Hacks/A.zip")
        merge = scrape.GameMerge(g, scrape.Result.NOT_FOUND, False)
        self.assertEqual(set(self.remember({}, [merge], asked=["A"])), {"./Hacks/A.zip"})


class NotFoundFileTest(unittest.TestCase):
    def test_round_trip(self):
        memory = {"snes": {"./A.zip": scrape.NotFound(42, TODAY)}, "nes": {}}
        data = scrape.render_not_found(memory)
        self.assertEqual(
            data,
            {"version": 1, "systems": {"snes": {"./A.zip": {"size": 42, "tried": "2026-11-13"}}}},
        )
        self.assertEqual(scrape.parse_not_found(data), {"snes": memory["snes"]})

    def test_anything_unreadable_counts_as_empty(self):
        for data in (None, [], "x", {"systems": []}):
            self.assertEqual(scrape.parse_not_found(data), {}, repr(data))
        rows = {
            "./A.zip": {"size": "x", "tried": "2026-11-13"},
            "./B.zip": {"size": 1, "tried": "tomorrow"},
            "./C.zip": {"size": True, "tried": "2026-11-13"},
            "./D.zip": 5,
        }
        self.assertEqual(scrape.parse_not_found({"systems": {"snes": rows}}), {"snes": {}})


class CredentialsPresenceTest(unittest.TestCase):
    def has(self, text: str) -> bool:
        return scrape.has_user_creds(text.splitlines(keepends=True))

    def test_set_in_the_screenscraper_section(self):
        self.assertTrue(self.has(FAKE_CONFIG))

    def test_the_key_is_found_with_spaces_and_a_quoted_key(self):
        self.assertTrue(self.has('[screenscraper]\n  userCreds = "a:b"\n'))
        self.assertTrue(self.has('[screenscraper]\n"userCreds"=x\n'))
        self.assertTrue(self.has("[ screenscraper ]\nuserCreds=\n"))

    def test_only_in_the_screenscraper_section(self):
        self.assertFalse(self.has('[main]\nuserCreds="a:b"\n'))
        self.assertFalse(self.has('userCreds="a:b"\n[screenscraper]\nthreads=1\n'))
        self.assertFalse(self.has('[screenscraper]\n[main]\nuserCreds="a:b"\n'))
        self.assertFalse(self.has('[screenscraper_x]\nuserCreds="a:b"\n'))

    def test_comments_do_not_count(self):
        self.assertFalse(self.has('[screenscraper]\n;userCreds="user:pass"\n'))
        self.assertFalse(self.has('[screenscraper]\n# userCreds="user:pass"\n'))
        self.assertFalse(self.has('; [screenscraper]\nuserCreds="a:b"\n'))

    def test_a_value_that_mentions_the_key_is_not_the_key(self):
        self.assertFalse(self.has('[screenscraper]\nnote="userCreds=x"\n'))
        self.assertFalse(self.has("[screenscraper]\nuserCredsX=1\n"))
        self.assertFalse(self.has("[screenscraper]\nuserCreds\n"))  # no "="

    def test_the_section_comes_back_after_another(self):
        text = "[screenscraper]\na=1\n[main]\nb=2\n[screenscraper]\nuserCreds=x\n"
        self.assertTrue(self.has(text))

    def test_empty_and_odd_input(self):
        self.assertFalse(self.has(""))
        self.assertFalse(self.has("[screenscraper\nuserCreds=x\n"))
        self.assertFalse(scrape.has_user_creds([]))

    def test_the_answer_is_a_boolean_and_nothing_else(self):
        self.assertIs(scrape.has_user_creds(FAKE_CONFIG.splitlines()), True)

    def test_where_the_config_lives(self):
        home = Path("/h")
        self.assertEqual(
            scrape.config_ini_path({}, home, lambda p: p == home / ".skyscraper"),
            home / ".skyscraper" / "config.ini",
        )
        self.assertEqual(
            scrape.config_ini_path({}, home, lambda p: False),
            home / ".config" / "skyscraper" / "config.ini",
        )
        self.assertEqual(
            scrape.config_ini_path({"XDG_CONFIG_HOME": "/x"}, home, lambda p: False),
            Path("/x/skyscraper/config.ini"),
        )

    def test_version_from_the_banner(self):
        banner = "Skyscraper \r _______ __\n|:  1   |   |__|       3.21.1   |\n`---' by Lars\n"
        self.assertEqual(scrape.parse_version(banner), (3, 21, 1))
        self.assertIsNone(scrape.parse_version("no digits here"))
        self.assertEqual(scrape.tested_version(), (3, 21, 1))


class AddEntriesTest(unittest.TestCase):
    GAMELIST = (
        '<?xml version="1.0"?>\n<alternativeEmulator>\n  <label>X</label>\n'
        "</alternativeEmulator>\n<gameList>\n"
        "  <!-- kept -->\n"
        "  <game>\n    <path>./A.zip</path>\n    <name>A</name>\n  </game>\n"
        "</gameList>\n"
    )

    def test_entries_go_before_the_close_in_the_files_own_style(self):
        new = scrape.add_entries(self.GAMELIST, [entry("./B.zip", name="B & C", desc="x < y\x01")])
        self.assertEqual(
            new,
            self.GAMELIST.replace(
                "</gameList>",
                "  <game>\n    <path>./B.zip</path>\n    <name>B &amp; C</name>\n"
                "    <desc>x &lt; y</desc>\n  </game>\n</gameList>",
            ),
        )

    def test_existing_text_is_kept_byte_for_byte(self):
        new = scrape.add_entries(self.GAMELIST, [entry("./B.zip", name="B")])
        head = self.GAMELIST[: self.GAMELIST.rindex("</gameList>")]
        self.assertTrue(new.startswith(head))
        self.assertEqual([e.path for e in artwork.parse_gamelist(new)], ["./A.zip", "./B.zip"])

    def test_crlf_and_empty_lists(self):
        text = '<?xml version="1.0"?>\r\n<gameList />\r\n'
        new = scrape.add_entries(text, [entry("./B.zip", name="B")])
        self.assertEqual(
            new,
            '<?xml version="1.0"?>\r\n<gameList>\r\n\t<game>\r\n\t\t<path>./B.zip</path>\r\n'
            "\t\t<name>B</name>\r\n\t</game>\r\n</gameList>\r\n",
        )

    def test_no_entries_no_change_and_no_list_is_an_error(self):
        self.assertEqual(scrape.add_entries(self.GAMELIST, []), self.GAMELIST)
        with self.assertRaises(gamelist.GamelistError):
            scrape.add_entries("<x/>", [entry("./B.zip")])


class SystemsAndDryRunTest(unittest.TestCase):
    def test_choose_systems(self):
        self.assertEqual(scrape.choose_systems(None), sorted(scrape.PLATFORMS))
        self.assertEqual(scrape.choose_systems(["snes", "nes", "snes"]), ["snes", "nes"])

    def test_a_system_that_is_left_out_says_why(self):
        with self.assertRaises(scrape.UsageError) as caught:
            scrape.choose_systems(["ps4"])
        self.assertIn("launcher", str(caught.exception))
        with self.assertRaises(scrape.UsageError) as caught:
            scrape.choose_systems(["amiga"])
        self.assertIn("not a system", str(caught.exception))

    def test_dry_run_lists_the_games_by_reason(self):
        a = game("snes", "roms/snes/A.zip", "A")
        b = game("snes", "roms/snes/B.zip", "B")
        sel = scrape.Selection(
            (
                scrape.Pick(a, False, scrape.MEDIA_KINDS),
                scrape.Pick(b, True, ("videos", "3dboxes")),
            ),
            4,
        )
        self.assertEqual(
            scrape.render_dry_run("snes", sel),
            "scrape: snes: would scrape 2 games\n"
            "  no entry       A.zip\n"
            "  missing media  B.zip: videos, 3dboxes\n"
            "  skipped 4 not found in the last 30 days",
        )
        self.assertEqual(
            scrape.render_dry_totals({"snes": sel, "nes": scrape.Selection(())}),
            "scrape: would scrape 2 games in 1 system (1 with no entry, 1 missing media)",
        )

    def test_recent_gamelist_warning_names_the_command(self):
        text = scrape.recent_message("snes", 3)
        self.assertIn("3 min ago", text)
        self.assertIn("warpzone scrape", text)


if __name__ == "__main__":
    unittest.main()
