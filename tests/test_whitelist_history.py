"""The pure side of the whitelist history: backup names, references, deltas and diffs."""

import unittest
from datetime import UTC, datetime

from warpzone import whitelist as wl
from warpzone import whitelist_history as hist

OLD = "20261106T100000.123456Z.stignore"
NEW = "20261107T080000.000001Z.stignore"
EMPTY = "20261105T090000.000000Z.stignore"


def managed(*, systems: tuple[str, ...] = (), includes: tuple[str, ...] = (), excludes=()) -> str:
    return wl.render(wl.Model(frozenset(systems), frozenset(includes), frozenset(excludes)))


class NameTests(unittest.TestCase):
    def test_a_backup_name_is_exactly_the_one_the_store_writes(self) -> None:
        self.assertTrue(hist.is_backup_name(OLD))
        for bad in (
            "",
            "../" + OLD,
            "x" + OLD,
            OLD + ".bak",
            "20261106T100000Z.stignore",
            "20261340T100000.123456Z.stignore",
            "patterns-deck.stignore",
        ):
            self.assertFalse(hist.is_backup_name(bad), bad)

    def test_the_time_comes_from_the_name(self) -> None:
        self.assertEqual(hist.backup_time(OLD), datetime(2026, 11, 6, 10, 0, 0, 123456, tzinfo=UTC))
        self.assertIsNone(hist.backup_time("nope"))
        self.assertEqual(hist.moment(OLD), "2026-11-06 10:00:00 UTC")
        self.assertEqual(hist.moment("nope"), "")

    def test_newest_first_skips_other_files(self) -> None:
        names = [OLD, "notes.txt", NEW, EMPTY, ".hidden"]
        self.assertEqual(hist.newest_first(names), [NEW, OLD, EMPTY])

    def test_resolve_takes_a_full_name_a_stem_or_a_unique_start(self) -> None:
        names = [OLD, NEW, EMPTY]
        self.assertEqual(hist.resolve(names, OLD), OLD)
        self.assertEqual(hist.resolve(names, hist.stem(OLD)), OLD)
        self.assertEqual(hist.resolve(names, "20261107"), NEW)

    def test_resolve_refuses_nothing_and_ambiguity(self) -> None:
        names = [OLD, NEW, EMPTY]
        with self.assertRaisesRegex(hist.UnknownBackup, "no backup '2025'"):
            hist.resolve(names, "2025")
        with self.assertRaisesRegex(hist.UnknownBackup, "matches 3 backups"):
            hist.resolve(names, "2026")
        with self.assertRaises(hist.UnknownBackup):
            hist.resolve(names, "")
        with self.assertRaises(hist.UnknownBackup):
            hist.resolve(names, "../etc")


class DeltaTests(unittest.TestCase):
    def test_lines_are_paths_unescaped_and_split_by_the_bang(self) -> None:
        text = (
            "// c\n#include patterns-common.stignore\n/roms/psx/Odd \\[X\\].chd\n"
            "!/roms/snes\n!/roms/psx\n*\n"
        )
        on, off = hist.lines_of(text)
        self.assertEqual(on, {"roms/snes", "roms/psx"})
        self.assertEqual(off, {"roms/psx/Odd [X].chd"})

    def test_a_line_that_is_no_anchored_path_is_not_a_path(self) -> None:
        on, off = hist.lines_of("!roms/*\nplain\n(?d).DS_Store\n")
        self.assertEqual((on, off), (frozenset(), frozenset()))

    def test_the_folders_section_names_no_path(self) -> None:
        text = managed(systems=("psx",), includes=("roms/ps4/games/CUSA00000", "bios"))
        self.assertIn("// Folders\n/roms/ps4/games/*\n!/roms/ps4/games\n", text)
        on, off = hist.lines_of(text)
        self.assertEqual(on, {"roms/psx", "roms/ps4/games/CUSA00000", "bios"})
        self.assertEqual(off, frozenset())

    def test_a_file_with_the_section_and_one_without_it_have_no_delta(self) -> None:
        text = managed(systems=("psx",), includes=("roms/ps4/games/CUSA00000", "bios"))
        without = text[: text.index("// Folders\n")] + "*\n"
        self.assertNotEqual(text, without)
        self.assertTrue(hist.delta(without, text).empty)
        self.assertTrue(hist.delta(text, without).empty)

    def test_a_pattern_with_a_wildcard_is_kept_as_written(self) -> None:
        on, _ = hist.lines_of("!/roms/*/x\n")
        self.assertEqual(on, {"/roms/*/x"})

    def test_restoring_an_older_file_turns_paths_on_and_off(self) -> None:
        current = managed(systems=("snes",), excludes=("roms/snes/Bad.sfc",))
        backup = managed(systems=("snes", "psx"), includes=("roms/gba/Tiny.gba",))
        change = hist.delta(current, backup)
        self.assertEqual(
            change.included,
            ("roms/gba/Tiny.gba", "roms/psx", "roms/snes/Bad.sfc"),
        )
        self.assertEqual(change.excluded, ())
        reverse = hist.delta(backup, current)
        self.assertEqual(reverse.excluded, ("roms/gba/Tiny.gba", "roms/psx", "roms/snes/Bad.sfc"))
        self.assertEqual(reverse.included, ())

    def test_an_exclusion_that_goes_is_an_inclusion_and_one_that_comes_is_an_exclusion(
        self,
    ) -> None:
        with_hole = managed(systems=("psx",), excludes=("roms/psx/A.chd",))
        without = managed(systems=("psx",))
        self.assertEqual(hist.delta(with_hole, without).included, ("roms/psx/A.chd",))
        self.assertEqual(hist.delta(without, with_hole).excluded, ("roms/psx/A.chd",))

    def test_equal_files_have_an_empty_delta(self) -> None:
        text = managed(systems=("psx",))
        self.assertTrue(hist.delta(text, text).empty)
        self.assertTrue(hist.delta("", "").empty)

    def test_a_missing_current_file_counts_as_naming_nothing(self) -> None:
        change = hist.delta("", managed(systems=("psx",)))
        self.assertEqual(change.included, ("roms/psx",))


class EntryTests(unittest.TestCase):
    def test_an_entry_compares_the_backup_with_the_current_bytes(self) -> None:
        current = managed(systems=("snes",)).encode()
        backup = managed(systems=("psx",)).encode()
        item = hist.entry(OLD, backup, current)
        self.assertEqual((item.empty, item.same, item.size), (False, False, len(backup)))
        self.assertEqual(item.delta.included, ("roms/psx",))
        self.assertEqual(item.delta.excluded, ("roms/snes",))
        self.assertEqual(hist.describe(item), "1 path(s) back on, 1 path(s) off")
        self.assertTrue(hist.entry(OLD, current, current).same)
        self.assertEqual(
            hist.describe(hist.entry(OLD, current, current)), "same as the current whitelist"
        )

    def test_a_difference_in_comments_only_is_said_so(self) -> None:
        current = b"// a\n!/roms/snes\n*\n"
        item = hist.entry(OLD, b"// b\n!/roms/snes\n*\n", current)
        self.assertEqual(hist.describe(item), "differs only in comments or layout")

    def test_an_empty_backup_is_refused(self) -> None:
        item = hist.entry(EMPTY, b"", b"!/roms/snes\n*\n")
        self.assertTrue(item.empty)
        assert hist.refusal(item) is not None
        self.assertIn("empty", hist.refusal(item) or "")
        self.assertIn("cannot be restored", hist.describe(item))
        self.assertIsNone(hist.refusal(hist.entry(OLD, b"!/roms/snes\n*\n", None)))

    def test_a_bad_name_is_a_programming_error(self) -> None:
        with self.assertRaises(ValueError):
            hist.entry("nope", b"x", None)

    def test_the_warning_appears_only_when_something_comes_back_on(self) -> None:
        self.assertIsNone(hist.warning(hist.Delta()))
        self.assertIsNone(hist.warning(hist.Delta(excluded=("roms/psx",))))
        said = hist.warning(hist.Delta(included=("roms/psx", "roms/snes")))
        assert said is not None
        self.assertIn("2 path(s) back on", said)
        self.assertIn("download", said)


class DiffTests(unittest.TestCase):
    def test_the_unified_diff_names_both_sides_and_marks_the_changes(self) -> None:
        lines = hist.unified_diff("a\nb\nc\n", "a\nx\nc\n", OLD)
        self.assertEqual(lines[0], "--- current")
        self.assertEqual(lines[1], f"+++ {hist.stem(OLD)}")
        self.assertIn("-b", lines)
        self.assertIn("+x", lines)

    def test_equal_texts_have_no_diff(self) -> None:
        self.assertEqual(hist.unified_diff("a\n", "a\n", OLD), [])

    def test_text_of_replaces_bytes_that_are_not_utf8(self) -> None:
        self.assertEqual(hist.text_of(None), "")
        self.assertEqual(hist.text_of(b"ok\xff"), "ok�")


if __name__ == "__main__":
    unittest.main()
