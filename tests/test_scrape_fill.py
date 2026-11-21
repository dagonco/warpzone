"""Scraping an entry the hub gamelist already has: the metadata it lacks is filled, what it has
is kept (the pure part: selection, the merge plan, the text edit, the memory, the report).
No filesystem, no Skyscraper."""

import unittest
from datetime import date

from library_support import game
from test_scrape import ALL_KINDS, SKY_FIELDS, entry, files, media, only, plan
from warpzone import artwork, gamelist, scrape, scrape_unattended

TODAY = date(2026, 11, 13)
TEXT_FIELDS = tuple(k for k in scrape.ENTRY_FIELDS if k != "name")


def select(games, entries, present=(), **kwargs):
    kwargs.setdefault("today", TODAY)
    return scrape.select(games, list(entries), set(present), **kwargs)


class BareSelectTest(unittest.TestCase):
    def setUp(self):
        self.g = game("snes", "roms/snes/Game.zip", "Game")
        self.every_medium = media("Game", *ALL_KINDS)

    def test_a_bare_entry_with_every_medium_is_picked(self):
        sel = select([self.g], [entry("./Game.zip", name="Game")], self.every_medium)
        (pick,) = sel.picks
        self.assertEqual((pick.has_entry, pick.missing, pick.bare), (True, (), True))

    def test_play_data_and_the_emulator_do_not_make_an_entry_less_bare(self):
        bare = entry("./Game.zip", name="Game", playcount="3", altemulator="X", hidden="true")
        (pick,) = select([self.g], [bare], self.every_medium).picks
        self.assertTrue(pick.bare)

    def test_one_text_field_is_enough_to_not_be_bare(self):
        for key in TEXT_FIELDS:
            with self.subTest(key=key):
                text = entry("./Game.zip", name="Game", **{key: "x"})
                self.assertEqual(select([self.g], [text], self.every_medium).picks, ())

    def test_an_empty_text_field_does_not_count(self):
        sel = select([self.g], [entry("./Game.zip", desc="")], self.every_medium)
        self.assertEqual(len(sel.picks), 1)

    def test_an_entry_with_text_and_missing_media_is_not_bare(self):
        (pick,) = select([self.g], [entry("./Game.zip", desc="x")]).picks
        self.assertFalse(pick.bare)

    def test_a_game_with_no_entry_is_not_bare(self):
        (pick,) = select([self.g], []).picks
        self.assertFalse(pick.bare)

    def test_nomultiscrape_stays_out(self):
        text = entry("./Game.zip", nomultiscrape="true")
        self.assertEqual(select([self.g], [text], self.every_medium).picks, ())

    def test_a_bare_game_not_found_recently_is_skipped_and_counted(self):
        record = {self.g.gamelist: scrape.NotFound(self.g.size, TODAY)}
        sel = select([self.g], [entry("./Game.zip")], self.every_medium, not_found=record)
        self.assertEqual((sel.picks, sel.skipped_not_found), ((), 1))


class FillFieldsTest(unittest.TestCase):
    def test_only_what_the_entry_lacks(self):
        have = entry("./Game.zip", name="Mine", desc="My text", playcount="2")
        wanted = {"name": "Theirs", "desc": "Their text", "genre": "RPG", "players": "1-2"}
        self.assertEqual(scrape.fill_fields(have, wanted), {"genre": "RPG", "players": "1-2"})

    def test_the_name_is_replaced_only_when_it_is_the_file_name_or_its_stem(self):
        for name in ("Game", "Game.zip"):
            with self.subTest(name=name):
                have = entry("./Hacks/Game.zip", name=name)
                self.assertEqual(scrape.fill_fields(have, {"name": "A Title"}), {"name": "A Title"})
        for name in ("Chrono", "game", "Hacks/Game.zip"):
            with self.subTest(name=name):
                have = entry("./Hacks/Game.zip", name=name)
                self.assertEqual(scrape.fill_fields(have, {"name": "A Title"}), {})

    def test_a_missing_name_is_added(self):
        have = entry("./Game.zip", playcount="1")
        self.assertEqual(scrape.fill_fields(have, {"name": "A Title"}), {"name": "A Title"})

    def test_a_new_name_equal_to_the_old_one_is_no_change(self):
        self.assertEqual(scrape.fill_fields(entry("./Game.zip", name="Game"), {"name": "Game"}), {})

    def test_fields_outside_the_list_and_empty_values_are_never_taken(self):
        have = entry("./Game.zip")
        wanted = {"playcount": "99", "favorite": "true", "image": "./x.png", "desc": ""}
        self.assertEqual(scrape.fill_fields(have, wanted), {})

    def test_nomultiscrape_gets_nothing(self):
        have = entry("./Game.zip", nomultiscrape="true")
        self.assertEqual(scrape.fill_fields(have, {"desc": "x"}), {})


class MergeFillTest(unittest.TestCase):
    def setUp(self):
        self.g = game("snes", "roms/snes/Game.zip", "Game")
        self.every_medium = media("Game", *ALL_KINDS)

    def merge(self, hub_entry, **sky_fields):
        sky = [entry("./Game.zip", **sky_fields)] if sky_fields else []
        return only(plan([self.g], hub=[hub_entry], hub_media=self.every_medium, sky=sky))

    def test_a_bare_entry_gets_the_text_and_the_real_title(self):
        merged = self.merge(entry("./Game.zip", name="Game", playcount="3"), **SKY_FIELDS)
        self.assertEqual(merged.result, scrape.Result.FILLED)
        self.assertTrue(merged.had_entry)
        self.assertIsNone(merged.entry)
        self.assertEqual(merged.copies, ())
        self.assertEqual(merged.fill, artwork.Entry("game", "./Game.zip", dict(SKY_FIELDS)))
        self.assertEqual(merged.filled_fields, scrape.ENTRY_FIELDS)
        self.assertFalse(merged.still_bare)

    def test_a_field_with_a_value_is_never_overwritten(self):
        merged = self.merge(
            entry("./Game.zip", name="Chrono", desc="Mine", genre="RPG"), **SKY_FIELDS
        )
        self.assertEqual(
            sorted(merged.fill.fields),
            ["developer", "players", "publisher", "rating", "releasedate"],
        )
        self.assertEqual(merged.fill.fields["developer"], "HAL Laboratory")

    def test_the_fill_has_the_path_of_the_game_and_never_play_data(self):
        merged = self.merge(entry("Game.zip", playcount="3"), playcount="99", desc="Text")
        self.assertEqual(merged.fill, artwork.Entry("game", "./Game.zip", {"desc": "Text"}))

    def test_an_entry_that_lacks_nothing_is_unchanged(self):
        full = entry("./Game.zip", **SKY_FIELDS)
        merged = self.merge(full, **{**SKY_FIELDS, "desc": "Other"})
        self.assertEqual((merged.result, merged.fill), (scrape.Result.UNCHANGED, None))
        self.assertFalse(merged.still_bare)

    def test_a_folder_entry_is_filled_too(self):
        merged = self.merge(entry("./Game.zip", tag="folder"), desc="Text")
        self.assertEqual(merged.fill.fields, {"desc": "Text"})
        self.assertEqual(merged.result, scrape.Result.FILLED)

    def test_nomultiscrape_stays_untouched(self):
        merged = self.merge(entry("./Game.zip", nomultiscrape="true"), **SKY_FIELDS)
        self.assertEqual((merged.result, merged.fill), (scrape.Result.UNCHANGED, None))

    def test_text_from_the_web_is_cleaned(self):
        merged = self.merge(entry("./Game.zip"), desc="  A\x01B  ")
        self.assertEqual(merged.fill.fields, {"desc": "AB"})

    def test_media_and_text_in_one_game(self):
        p = plan(
            [self.g],
            hub=[entry("./Game.zip", name="Game")],
            sky=[entry("./Game.zip", desc="Text")],
            main=files("covers/Game.png"),
        )
        merged = only(p)
        self.assertEqual(merged.result, scrape.Result.FILLED)
        self.assertEqual((merged.kinds, merged.filled_fields), (("covers",), ("desc",)))
        self.assertEqual(p.fills, (merged.fill,))
        self.assertEqual(p.entries, ())

    def test_a_game_skyscraper_has_no_text_for_stays_bare(self):
        merged = self.merge(entry("./Game.zip", name="Game"), name="Game")
        self.assertEqual((merged.result, merged.fill), (scrape.Result.UNCHANGED, None))
        self.assertTrue(merged.still_bare)

    def test_only_the_title_does_not_make_the_entry_less_bare(self):
        merged = self.merge(entry("./Game.zip", name="Game"), name="A Title")
        self.assertEqual(merged.filled_fields, ("name",))
        self.assertTrue(merged.still_bare)

    def test_a_game_with_text_is_not_still_bare(self):
        self.assertFalse(self.merge(entry("./Game.zip", desc="x"), genre="RPG").still_bare)

    def test_nothing_from_skyscraper_is_still_not_found(self):
        merged = self.merge(entry("./Game.zip", name="Game"))
        self.assertEqual(merged.result, scrape.Result.NOT_FOUND)


class FillEntriesTest(unittest.TestCase):
    GAMELIST = (
        '<?xml version="1.0"?>\n<alternativeEmulator>\n  <label>X</label>\n'
        "</alternativeEmulator>\n<gameList>\n"
        "  <!-- kept -->\n"
        "  <game>\n    <path>./A.zip</path>\n    <name>A</name>\n    <playcount>7</playcount>\n"
        "    <lastplayed>20260101T000000</lastplayed>\n  </game>\n"
        "  <game>\n    <path>./B.zip</path>\n    <name>Mine</name>\n    <desc>Mine</desc>\n"
        "  </game>\n"
        "</gameList>\n"
    )

    def fill(self, text, *fills):
        return scrape.fill_entries(text, artwork.parse_gamelist(text), list(fills))

    def test_the_fields_go_in_the_files_own_style_and_the_rest_is_kept(self):
        new, done = self.fill(
            self.GAMELIST, entry("./A.zip", name="A Title", desc="x < y", genre="RPG")
        )
        self.assertEqual(done, frozenset({"A.zip"}))
        self.assertEqual(
            new,
            self.GAMELIST.replace("<name>A</name>", "<name>A Title</name>").replace(
                "    <lastplayed>20260101T000000</lastplayed>\n",
                "    <lastplayed>20260101T000000</lastplayed>\n"
                "    <desc>x &lt; y</desc>\n    <genre>RPG</genre>\n",
            ),
        )

    def test_the_other_entry_and_the_comment_stay_byte_for_byte(self):
        new, _ = self.fill(self.GAMELIST, entry("./A.zip", desc="x"))
        tail = self.GAMELIST[self.GAMELIST.index("  <game>\n    <path>./B.zip") :]
        self.assertTrue(new.endswith(tail))
        self.assertIn("  <!-- kept -->\n", new)

    def test_crlf_tabs_and_an_empty_element(self):
        text = (
            '<?xml version="1.0"?>\r\n<gameList>\r\n\t<game>\r\n\t\t<path>./A.zip</path>\r\n'
            "\t\t<desc></desc>\r\n\t\t<genre/>\r\n\t</game>\r\n</gameList>\r\n"
        )
        new, _ = self.fill(text, entry("./A.zip", desc="x", genre="RPG", players="2"))
        self.assertEqual(
            new,
            '<?xml version="1.0"?>\r\n<gameList>\r\n\t<game>\r\n\t\t<path>./A.zip</path>\r\n'
            "\t\t<desc>x</desc>\r\n\t\t<genre>RPG</genre>\r\n\t\t<players>2</players>\r\n"
            "\t</game>\r\n</gameList>\r\n",
        )

    def test_a_single_line_entry_and_a_folder(self):
        text = (
            "<gameList>\n<game><path>./A.zip</path></game>\n"
            "<folder><path>./B</path></folder>\n</gameList>\n"
        )
        new, done = self.fill(
            text, entry("./A.zip", desc="x"), entry("./B", tag="folder", desc="y")
        )
        self.assertEqual(done, frozenset({"A.zip", "B"}))
        self.assertIn("<game><path>./A.zip</path><desc>x</desc></game>", new)
        self.assertIn("<folder><path>./B</path><desc>y</desc></folder>", new)

    def test_what_the_entry_has_now_is_checked_again(self):
        new, done = self.fill(self.GAMELIST, entry("./B.zip", name="Theirs", desc="Theirs"))
        self.assertEqual((new, done), (self.GAMELIST, frozenset()))

    def test_an_entry_the_gamelist_no_longer_has_is_left_out(self):
        new, done = self.fill(self.GAMELIST, entry("./Gone.zip", desc="x"))
        self.assertEqual((new, done), (self.GAMELIST, frozenset()))

    def test_no_fills_no_change_and_the_result_is_well_formed(self):
        self.assertEqual(self.fill(self.GAMELIST), (self.GAMELIST, frozenset()))
        new, _ = self.fill(self.GAMELIST, entry("./A.zip", desc="a & b <c>"))
        self.assertTrue(gamelist.well_formed(new))
        (a, _b) = artwork.parse_gamelist(new)
        self.assertEqual(a.fields["desc"], "a & b <c>")
        self.assertEqual(a.fields["playcount"], "7")


class ReportTest(unittest.TestCase):
    def merge(self, kinds=(), fill=None):
        g = game("snes", "roms/snes/Game.zip", "Game")
        copies = tuple(scrape.MediaCopy(k, "main", "x", "y") for k in kinds)
        return scrape.GameMerge(g, scrape.Result.FILLED, True, None, copies, fill)

    def test_the_filled_line_names_the_text_fields(self):
        fill = artwork.Entry("game", "./Game.zip", {"desc": "x", "genre": "y"})
        text = scrape.render_report(scrape.Report("snes", (self.merge(fill=fill),)))
        self.assertIn("  filled     Game.zip: fields desc, genre\n", text + "\n")
        self.assertTrue(text.endswith("scrape: snes: 1 filled"))

    def test_media_and_text_together(self):
        fill = artwork.Entry("game", "./Game.zip", {"desc": "x"})
        text = scrape.render_report(scrape.Report("snes", (self.merge(("videos",), fill),)))
        self.assertIn("  filled     Game.zip: videos; fields desc\n", text + "\n")

    def test_media_only_keeps_its_line(self):
        text = scrape.render_report(scrape.Report("snes", (self.merge(("videos",)),)))
        self.assertIn("  filled     Game.zip: videos (entry kept)\n", text + "\n")

    def test_the_summary_counts_it_as_filled(self):
        fill = artwork.Entry("game", "./Game.zip", {"desc": "x"})
        summary = scrape.summarize_system(scrape.Report("snes", (self.merge(fill=fill),)))
        self.assertEqual((summary.filled, summary.filled_paths), (1, ("Game.zip",)))

    def test_the_dry_run_lists_a_bare_entry(self):
        a = game("snes", "roms/snes/A.zip", "A")
        b = game("snes", "roms/snes/B.zip", "B")
        c = game("snes", "roms/snes/C.zip", "C")
        sel = scrape.Selection(
            (
                scrape.Pick(a, True, (), True),
                scrape.Pick(b, True, ("videos",), True),
                scrape.Pick(c, True, ("videos",)),
            )
        )
        self.assertEqual(
            scrape.render_dry_run("snes", sel),
            "scrape: snes: would scrape 3 games\n"
            "  bare entry     A.zip\n"
            "  missing media  B.zip: videos\n"
            "  bare entry     B.zip\n"
            "  missing media  C.zip: videos",
        )
        self.assertEqual(
            scrape.render_dry_totals({"snes": sel}),
            "scrape: would scrape 3 games in 1 system "
            "(0 with no entry, 2 missing media, 1 with a bare entry)",
        )


class BareMemoryTest(unittest.TestCase):
    def pick(self, *, bare=True, missing=(), size=100):
        g = game("snes", "roms/snes/A.zip", "A", size=size, gamelist="./A.zip")
        return scrape.Pick(g, True, tuple(missing), bare)

    def test_a_bare_pick_is_not_skipped_by_a_record_that_does_not_say_bare(self):
        old = scrape.Incomplete(100, ("videos",), TODAY)
        self.assertFalse(scrape.skip_incomplete(self.pick(missing=("videos",)), old))

    def test_a_bare_pick_is_skipped_by_a_record_that_says_it_stayed_bare(self):
        record = scrape.Incomplete(100, (), TODAY, bare=True)
        self.assertTrue(scrape.skip_incomplete(self.pick(), record))

    def test_a_game_that_is_bare_again_with_another_size_comes_back(self):
        record = scrape.Incomplete(100, (), TODAY, bare=True)
        self.assertFalse(scrape.skip_incomplete(self.pick(size=101), record))

    def test_a_record_of_a_bare_game_does_not_hide_a_new_missing_kind(self):
        record = scrape.Incomplete(100, (), TODAY, bare=True)
        self.assertFalse(scrape.skip_incomplete(self.pick(missing=("covers",)), record))

    def test_a_game_that_is_not_bare_now_is_skipped_as_before(self):
        record = scrape.Incomplete(100, ("videos",), TODAY, bare=True)
        self.assertTrue(scrape.skip_incomplete(self.pick(bare=False, missing=("videos",)), record))

    def remember(self, merge, pick, written=True):
        return scrape.remember_incomplete({}, [merge], {pick.game.gamelist: pick}, written, TODAY)

    def merge(self, pick, result, *, still_bare=False, fill=None, kinds=()):
        copies = tuple(scrape.MediaCopy(k, scrape.MAIN, f"{k}/x.png", f"{k}/x.png") for k in kinds)
        return scrape.GameMerge(pick.game, result, True, None, copies, fill, still_bare)

    def test_a_game_that_stays_bare_is_recorded_without_missing_kinds(self):
        p = self.pick()
        out = self.remember(self.merge(p, scrape.Result.UNCHANGED, still_bare=True), p)
        self.assertEqual(out, {"./A.zip": scrape.Incomplete(100, (), TODAY, bare=True)})

    def test_a_game_that_got_its_text_loses_the_record(self):
        p = self.pick()
        fill = artwork.Entry("game", "./A.zip", {"desc": "x"})
        merge = self.merge(p, scrape.Result.FILLED, fill=fill)
        old = {"./A.zip": scrape.Incomplete(100, (), TODAY, bare=True)}
        out = scrape.remember_incomplete(old, [merge], {"./A.zip": p}, True, TODAY)
        self.assertEqual(out, {})

    def test_bare_and_missing_media_are_recorded_together(self):
        p = self.pick(missing=("videos", "covers"))
        merge = self.merge(p, scrape.Result.FILLED, still_bare=True, kinds=("covers",))
        out = self.remember(merge, p)
        self.assertEqual(out, {"./A.zip": scrape.Incomplete(100, ("videos",), TODAY, bare=True)})

    def test_a_failed_gamelist_write_records_nothing(self):
        p = self.pick()
        merge = self.merge(p, scrape.Result.UNCHANGED, still_bare=True)
        self.assertEqual(self.remember(merge, p, written=False), {})

    def test_the_file_round_trip_and_the_old_shape(self):
        record = scrape.Incomplete(42, (), TODAY, bare=True)
        both = scrape.Incomplete(42, ("videos",), TODAY, bare=True)
        data = scrape.render_memory({}, {"snes": {"./A.zip": record, "./B.zip": both}})
        self.assertEqual(
            data["incomplete"]["snes"]["./A.zip"],
            {"size": 42, "missing": [], "bare": True, "tried": "2026-11-13"},
        )
        self.assertEqual(
            scrape.parse_incomplete(data), {"snes": {"./A.zip": record, "./B.zip": both}}
        )
        old = scrape.render_memory(
            {}, {"snes": {"./C.zip": scrape.Incomplete(1, ("videos",), TODAY)}}
        )
        self.assertNotIn("bare", old["incomplete"]["snes"]["./C.zip"])

    def test_a_record_that_says_nothing_is_still_dropped(self):
        row = {"size": 1, "missing": [], "tried": "2026-11-13"}
        for extra in ({}, {"bare": False}, {"bare": "yes"}, {"bare": 1}):
            data = {"incomplete": {"snes": {"./A.zip": {**row, **extra}}}}
            self.assertEqual(scrape.parse_incomplete(data), {"snes": {}}, extra)


class NightTest(unittest.TestCase):
    def pick(self, name, *, bare, missing=()):
        g = game("snes", f"roms/snes/{name}", name, size=100, gamelist=f"./{name}")
        return scrape.Pick(g, True, tuple(missing), bare)

    def test_a_bare_game_with_every_medium_is_in_the_core_tier(self):
        pick = self.pick("A.zip", bare=True)
        self.assertEqual(
            scrape_unattended.tier_of(pick, None, TODAY), scrape_unattended.MISSING_CORE
        )

    def test_the_night_picks_a_bare_game_the_memory_does_not_cover(self):
        pick = self.pick("A.zip", bare=True)
        old = {"snes": {"./A.zip": scrape.Incomplete(100, ("videos",), TODAY)}}
        night = scrape_unattended.plan_night(
            {"snes": scrape.Selection((pick,))}, old, {}, max_games=10, today=TODAY
        )
        self.assertEqual((night.games, night.skipped_incomplete), (1, {}))

    def test_the_night_skips_a_game_that_stayed_bare(self):
        pick = self.pick("A.zip", bare=True)
        memory = {"snes": {"./A.zip": scrape.Incomplete(100, (), TODAY, bare=True)}}
        night = scrape_unattended.plan_night(
            {"snes": scrape.Selection((pick,))}, memory, {}, max_games=10, today=TODAY
        )
        self.assertEqual((night.games, night.skipped_incomplete), (0, {"snes": 1}))


if __name__ == "__main__":
    unittest.main()
