"""The incomplete memory, the pure part: its place in the memory file, `skip_incomplete` and
`remember_incomplete`. No filesystem, no Skyscraper."""

import unittest
from datetime import date, timedelta
from typing import ClassVar

from library_support import game
from warpzone import artwork, scrape

TODAY = date(2026, 11, 13)


def pick(path: str, missing=scrape.MEDIA_KINDS, *, size: int = 100, has_entry: bool = True):
    g = game("snes", f"roms/snes/{path}", path, size=size, gamelist=f"./{path}")
    return scrape.Pick(g, has_entry, tuple(missing))


def merged(
    p: scrape.Pick, result: scrape.Result, *kinds: str, entry: bool = True
) -> scrape.GameMerge:
    """The merge of a pick, with copies of `kinds`; a pick with no entry gets a new one unless
    `entry` is False (nothing was written)."""
    copies = tuple(scrape.MediaCopy(k, scrape.MAIN, f"{k}/x.png", f"{k}/x.png") for k in kinds)
    new = artwork.Entry("game", p.game.gamelist, {}) if entry and not p.has_entry else None
    return scrape.GameMerge(p.game, result, p.has_entry, new, copies)


class IncompleteFileTest(unittest.TestCase):
    RECORD = scrape.Incomplete(42, ("videos", "backcovers"), TODAY)
    ROW: ClassVar[dict[str, object]] = {
        "size": 42,
        "missing": ["videos", "backcovers"],
        "tried": "2026-11-13",
    }

    def test_round_trip_next_to_the_not_found_records(self):
        nf = {"snes": {"./A.zip": scrape.NotFound(7, TODAY)}}
        inc = {"snes": {"./B.zip": self.RECORD}, "nes": {}}
        data = scrape.render_memory(nf, inc)
        self.assertEqual(
            data,
            {
                "version": 1,
                "systems": {"snes": {"./A.zip": {"size": 7, "tried": "2026-11-13"}}},
                "incomplete": {"snes": {"./B.zip": self.ROW}},
            },
        )
        self.assertEqual(scrape.parse_not_found(data), nf)
        self.assertEqual(scrape.parse_incomplete(data), {"snes": inc["snes"]})

    def test_without_incomplete_records_the_file_is_the_old_one(self):
        nf = {"snes": {"./A.zip": scrape.NotFound(7, TODAY)}}
        self.assertEqual(scrape.render_memory(nf, {}), scrape.render_not_found(nf))

    def test_a_file_without_the_key_still_parses(self):
        nf = {"snes": {"./A.zip": scrape.NotFound(7, TODAY)}}
        old = scrape.render_not_found(nf)
        self.assertNotIn("incomplete", old)
        self.assertEqual(scrape.parse_incomplete(old), {})
        self.assertEqual(scrape.parse_not_found(old), nf)

    def test_a_system_with_only_incomplete_records_is_kept(self):
        data = scrape.render_memory({}, {"snes": {"./B.zip": self.RECORD}})
        self.assertEqual(data["systems"], {})
        self.assertEqual(scrape.parse_incomplete(data), {"snes": {"./B.zip": self.RECORD}})

    def test_the_not_found_parser_ignores_the_new_key(self):
        data = scrape.render_memory({}, {"snes": {"./B.zip": self.RECORD}})
        self.assertEqual(scrape.parse_not_found(data), {})

    def test_anything_unreadable_counts_as_empty(self):
        for data in (None, [], "x", {"incomplete": []}, {"incomplete": {"snes": 3}}):
            self.assertEqual(scrape.parse_incomplete(data), {}, repr(data))

    def test_damaged_records_are_dropped(self):
        rows = {
            "./Good.zip": self.ROW,
            "./Size.zip": {**self.ROW, "size": "x"},
            "./Bool.zip": {**self.ROW, "size": True},
            "./Day.zip": {**self.ROW, "tried": "tomorrow"},
            "./Kinds.zip": {**self.ROW, "missing": "videos"},
            "./Kind.zip": {**self.ROW, "missing": ["videos", 3]},
            "./Unknown.zip": {**self.ROW, "missing": ["videos", "nonsense"]},
            "./Empty.zip": {**self.ROW, "missing": []},
            "./Row.zip": 5,
        }
        self.assertEqual(
            scrape.parse_incomplete({"incomplete": {"snes": rows}}),
            {"snes": {"./Good.zip": self.RECORD}},
        )


class SkipIncompleteTest(unittest.TestCase):
    def record(self, *missing: str, size: int = 100) -> scrape.Incomplete:
        return scrape.Incomplete(size, missing, TODAY)

    def test_the_same_kinds_missing_are_skipped(self):
        p = pick("A.zip", ("videos", "backcovers"))
        self.assertTrue(scrape.skip_incomplete(p, self.record("backcovers", "videos")))

    def test_fewer_kinds_missing_than_recorded_are_still_skipped(self):
        p = pick("A.zip", ("videos",))
        self.assertTrue(scrape.skip_incomplete(p, self.record("backcovers", "videos")))

    def test_a_new_missing_kind_picks_the_game_again(self):
        p = pick("A.zip", ("covers", "videos"))
        self.assertFalse(scrape.skip_incomplete(p, self.record("videos")))

    def test_another_size_picks_the_game_again(self):
        p = pick("A.zip", ("videos",), size=101)
        self.assertFalse(scrape.skip_incomplete(p, self.record("videos")))

    def test_no_record_no_skip(self):
        self.assertFalse(scrape.skip_incomplete(pick("A.zip", ("videos",)), None))

    def test_a_game_with_no_entry_is_never_skipped(self):
        p = pick("A.zip", ("videos",), has_entry=False)
        self.assertFalse(scrape.skip_incomplete(p, self.record("videos")))


class RememberIncompleteTest(unittest.TestCase):
    def remember(self, records, merges, picks, written=True, lost=()):
        by_path = {p.game.gamelist: p for p in picks}
        return scrape.remember_incomplete(records, merges, by_path, written, TODAY, lost)

    def test_the_kinds_still_missing_after_the_merge_are_recorded(self):
        p = pick("A.zip")
        m = merged(p, scrape.Result.ADDED, "covers", "screenshots", "miximages", "marquees")
        out = self.remember({}, [m], [p])
        self.assertEqual(
            out, {"./A.zip": scrape.Incomplete(100, ("3dboxes", "backcovers", "videos"), TODAY)}
        )

    def test_added_filled_and_unchanged_are_recorded(self):
        for result in (scrape.Result.ADDED, scrape.Result.FILLED, scrape.Result.UNCHANGED):
            p = pick("A.zip", ("videos",), has_entry=result is not scrape.Result.ADDED)
            out = self.remember({}, [merged(p, result)], [p])
            self.assertEqual(out, {"./A.zip": scrape.Incomplete(100, ("videos",), TODAY)}, result)

    def test_not_found_is_not_recorded(self):
        p = pick("A.zip", has_entry=False)
        m = merged(p, scrape.Result.NOT_FOUND, entry=False)
        self.assertEqual(self.remember({}, [m], [p]), {})

    def test_a_game_with_a_dropped_copy_is_not_recorded(self):
        p = pick("A.zip", ("covers", "videos"))
        m = merged(p, scrape.Result.UNCHANGED)
        self.assertEqual(self.remember({}, [m], [p], lost=["./A.zip"]), {})

    def test_a_dropped_copy_leaves_an_old_record_as_it_is(self):
        old = {"./A.zip": scrape.Incomplete(100, ("videos",), TODAY - timedelta(days=3))}
        p = pick("A.zip", ("videos",))
        self.assertEqual(
            self.remember(old, [merged(p, scrape.Result.UNCHANGED)], [p], lost=["./A.zip"]), old
        )

    def test_a_failed_gamelist_write_records_nothing_and_changes_nothing(self):
        old = {"./Z.zip": scrape.Incomplete(1, ("videos",), TODAY)}
        p = pick("A.zip", ("videos",), has_entry=False)
        m = merged(p, scrape.Result.UNCHANGED, entry=False)
        self.assertEqual(self.remember(old, [m], [p], written=False), old)
        q = pick("B.zip", ("videos",))
        self.assertEqual(self.remember(old, [merged(q, scrape.Result.UNCHANGED)], [q], False), old)

    def test_a_game_that_still_has_no_entry_is_not_recorded(self):
        p = pick("A.zip", ("videos",), has_entry=False)
        m = merged(p, scrape.Result.UNCHANGED, entry=False)
        self.assertEqual(self.remember({}, [m], [p]), {})

    def test_a_game_that_lacks_nothing_now_loses_its_record(self):
        old = {"./A.zip": scrape.Incomplete(100, ("videos",), TODAY - timedelta(days=3))}
        p = pick("A.zip", ("videos",))
        m = merged(p, scrape.Result.FILLED, "videos")
        self.assertEqual(self.remember(old, [m], [p]), {})

    def test_a_not_found_verdict_keeps_a_record_the_game_has(self):
        old = {"./A.zip": scrape.Incomplete(100, ("videos",), TODAY)}
        p = pick("A.zip", ("videos",))
        m = merged(p, scrape.Result.NOT_FOUND)
        self.assertEqual(self.remember(old, [m], [p]), old)

    def test_other_records_are_kept_and_the_input_is_not_changed(self):
        old = {"./Z.zip": scrape.Incomplete(1, ("videos",), TODAY)}
        p = pick("A.zip", ("videos",))
        out = self.remember(old, [merged(p, scrape.Result.UNCHANGED)], [p])
        self.assertEqual(set(out), {"./Z.zip", "./A.zip"})
        self.assertEqual(set(old), {"./Z.zip"})

    def test_a_record_is_rewritten_with_the_new_size_and_day(self):
        old = {
            "./A.zip": scrape.Incomplete(50, ("videos", "backcovers"), TODAY - timedelta(days=9))
        }
        p = pick("A.zip", ("videos",), size=100)
        out = self.remember(old, [merged(p, scrape.Result.UNCHANGED)], [p])
        self.assertEqual(out, {"./A.zip": scrape.Incomplete(100, ("videos",), TODAY)})

    def test_a_merge_with_no_pick_is_ignored(self):
        p = pick("A.zip", ("videos",))
        self.assertEqual(self.remember({}, [merged(p, scrape.Result.UNCHANGED)], []), {})


if __name__ == "__main__":
    unittest.main()
