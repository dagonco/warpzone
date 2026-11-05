"""The pure part of the save conversion: the `.dsv` footer and the three-way decision."""

import struct
import unittest

from warpzone import saveconvert
from warpzone.saveconvert import Action, SaveFormatError, Side, decide


def footer(size: int = 65536, *, pad: int | None = None, version: int = 0) -> bytes:
    numbers = struct.pack("<6I", size, size if pad is None else pad, 2, 2, size, version)
    return saveconvert.FOOTER_TEXT + numbers + saveconvert.COOKIE


def save(size: int = 8192, fill: int = 0x11) -> bytes:
    return bytes([fill]) * size


class FooterTests(unittest.TestCase):
    def test_the_footer_is_122_bytes(self) -> None:
        self.assertEqual(saveconvert.FOOTER_LEN, 122)
        self.assertEqual(len(footer()), 122)

    def test_a_footer_is_read_field_by_field(self) -> None:
        parsed = saveconvert.parse_footer(footer(65536))
        self.assertEqual(
            (parsed.size, parsed.pad_size, parsed.type, parsed.addr_size, parsed.mem_size),
            (65536, 65536, 2, 2, 65536),
        )
        self.assertEqual(parsed.raw, footer(65536))

    def test_a_malformed_footer_is_refused(self) -> None:
        good = footer()
        for label, data in (
            ("short", good[:-1]),
            ("long", good + b"\0"),
            ("text", b"x" + good[1:]),
            ("cookie", good[:-1] + b"?"),
            ("version", footer(version=1)),
        ):
            with self.subTest(label), self.assertRaises(SaveFormatError):
                saveconvert.parse_footer(data)

    def test_split_strips_the_footer(self) -> None:
        raw = save(65536)
        dsv = saveconvert.split_dsv(raw + footer(65536))
        self.assertEqual(dsv.raw, raw)
        self.assertEqual(dsv.footer.raw, footer(65536))

    def test_split_accepts_a_raw_dump_without_a_footer(self) -> None:
        dsv = saveconvert.split_dsv(save(8192))
        self.assertEqual((dsv.raw, dsv.footer), (save(8192), None))

    def test_split_refuses_what_is_not_a_save(self) -> None:
        cases = {
            "not a size": save(1000),
            "cookie but a footer-sized body": saveconvert.COOKIE,
            "footer after an odd size": save(1000) + footer(1000),
            "footer after nothing": footer(),
            "bad footer version": save(8192) + footer(8192, version=2),
            "empty": b"",
            "too small": save(256),
            "too big": save(16 * 1024 * 1024),
        }
        for label, data in cases.items():
            with self.subTest(label), self.assertRaises(SaveFormatError):
                saveconvert.split_dsv(data)

    def test_the_valid_sizes_are_powers_of_two_from_512_bytes_to_8_mib(self) -> None:
        good = [512 << n for n in range(15)]
        self.assertEqual(good[-1], 8 * 1024 * 1024)
        for size in good:
            self.assertTrue(saveconvert.valid_size(size), size)
        for size in (0, 1, 256, 511, 513, 3 * 512, 8 * 1024 * 1024 + 1, 16 * 1024 * 1024):
            self.assertFalse(saveconvert.valid_size(size), size)

    def test_build_keeps_a_footer_that_describes_the_same_size(self) -> None:
        previous = saveconvert.parse_footer(footer(8192))
        self.assertEqual(
            saveconvert.build_dsv(save(8192, 0x22), previous), save(8192, 0x22) + footer(8192)
        )

    def test_build_writes_the_raw_dump_when_the_footer_is_for_another_size(self) -> None:
        previous = saveconvert.parse_footer(footer(65536))
        self.assertEqual(saveconvert.build_dsv(save(8192), previous), save(8192))
        self.assertEqual(saveconvert.build_dsv(save(8192), None), save(8192))

    def test_build_refuses_a_raw_save_of_an_invalid_size(self) -> None:
        with self.assertRaises(SaveFormatError):
            saveconvert.build_dsv(save(1000), None)

    def test_a_round_trip_gives_the_same_bytes(self) -> None:
        raw = save(65536, 0x5A)
        original = raw + footer(65536)
        dsv = saveconvert.split_dsv(original)
        self.assertEqual(saveconvert.build_dsv(dsv.raw, dsv.footer), original)

    def test_blank_means_erased_flash(self) -> None:
        self.assertTrue(saveconvert.blank(save(512, 0xFF)))
        self.assertFalse(saveconvert.blank(save(512, 0x00)))
        self.assertFalse(saveconvert.blank(b"\xff" * 511 + b"\x00"))


def side(content: bytes, mtime: int = 100, *, blank: bool = False) -> Side:
    return Side(saveconvert.digest(content), mtime, len(content), blank)


A, B, C = save(8192, 1), save(8192, 2), save(8192, 3)
BASE = saveconvert.digest(A)


class DecideTests(unittest.TestCase):
    def test_nothing_on_either_side_is_nothing(self) -> None:
        self.assertEqual(decide(None, None, None).action, Action.NOTHING)
        self.assertEqual(decide(None, None, BASE).action, Action.NOTHING)

    def test_a_new_hub_file_goes_to_the_emulator(self) -> None:
        d = decide(side(A), None, None)
        self.assertEqual((d.action, d.conflict, d.base), (Action.TO_EMU, False, side(A).digest))

    def test_a_new_emulator_file_goes_to_the_hub(self) -> None:
        d = decide(None, side(A), None)
        self.assertEqual((d.action, d.conflict, d.base), (Action.TO_RAW, False, side(A).digest))

    def test_a_deleted_file_is_restored_never_propagated(self) -> None:
        # The base says both sides once held A; one is gone: the other refills it.
        self.assertEqual(decide(side(A), None, BASE).action, Action.TO_EMU)
        self.assertEqual(decide(None, side(A), BASE).action, Action.TO_RAW)

    def test_equal_sides_need_nothing_and_set_the_base(self) -> None:
        d = decide(side(A, 1), side(A, 9), None)
        self.assertEqual((d.action, d.base), (Action.NOTHING, side(A).digest))
        self.assertEqual(decide(side(A), side(A), BASE).action, Action.NOTHING)

    def test_only_the_emulator_changed(self) -> None:
        d = decide(side(A, 500), side(B, 100), BASE)  # dates do not matter
        self.assertEqual((d.action, d.conflict, d.base), (Action.TO_RAW, False, side(B).digest))

    def test_only_the_hub_changed(self) -> None:
        d = decide(side(B, 100), side(A, 500), BASE)
        self.assertEqual((d.action, d.conflict, d.base), (Action.TO_EMU, False, side(B).digest))

    def test_both_changed_the_newer_emulator_wins(self) -> None:
        d = decide(side(B, 100), side(C, 200), BASE)
        self.assertEqual((d.action, d.conflict, d.base), (Action.TO_RAW, True, side(C).digest))

    def test_both_changed_the_newer_hub_wins(self) -> None:
        d = decide(side(B, 300), side(C, 200), BASE)
        self.assertEqual((d.action, d.conflict, d.base), (Action.TO_EMU, True, side(B).digest))

    def test_both_changed_with_the_same_date_the_hub_wins(self) -> None:
        d = decide(side(B, 200), side(C, 200), BASE)
        self.assertEqual((d.action, d.conflict), (Action.TO_EMU, True))

    def test_without_a_base_different_sides_count_as_both_changed(self) -> None:
        d = decide(side(B, 100), side(C, 200), None)
        self.assertEqual((d.action, d.conflict), (Action.TO_RAW, True))

    def test_without_a_base_an_erased_file_never_wins(self) -> None:
        erased = b"\xff" * 8192
        d = decide(side(B, 100), side(erased, 900, blank=True), None)
        self.assertEqual((d.action, d.conflict), (Action.TO_EMU, False))
        d = decide(side(erased, 900, blank=True), side(B, 100), None)
        self.assertEqual((d.action, d.conflict), (Action.TO_RAW, False))

    def test_an_unusable_side_skips_the_game(self) -> None:
        bad = Side(problem="1000 bytes is not a save size")
        for raw, emu in ((bad, side(A)), (side(A), bad), (bad, None), (None, bad)):
            with self.subTest(raw=raw, emu=emu):
                d = decide(raw, emu, BASE)
                self.assertEqual(d.action, Action.SKIP)
                self.assertIn("not a save size", d.reason)

    def test_sides_of_different_sizes_are_skipped(self) -> None:
        d = decide(side(save(8192)), side(save(65536)), BASE)
        self.assertEqual(d.action, Action.SKIP)
        self.assertIn("size", d.reason)


if __name__ == "__main__":
    unittest.main()
