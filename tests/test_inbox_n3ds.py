import io
import struct
import tempfile
import unittest
from pathlib import Path

from inbox_support import age
from n3ds_support import UNIT, ncsd
from test_inbox import Base
from warpzone import inbox_n3ds as n3
from warpzone.config import InboxSettings
from warpzone.inbox import whitelist_line

NAME = "Plumber Kart 3D (Europe) (En,Fr,De).3ds"
CCI = "Plumber Kart 3D (Europe) (En,Fr,De).cci"


def detect_bytes(data: bytes) -> n3.Verdict:
    return n3.detect(io.BytesIO(data))


def judge(data: bytes) -> n3.Verdict:
    return n3.judge(data[:0x200], data[0x4000:0x4200], len(data))


class PureTests(unittest.TestCase):
    def test_claimed_names(self) -> None:
        for name in ("A.3ds", "A.3DS", "b.cci", "c/d.CCI", "Game (USA).3ds"):
            self.assertTrue(n3.claimed_name(name), name)
        for name in ("A.3dsx", "A.cia", "A.3ds.zip", "A.3ds.bak", "cci", "A.cc", "A.iso"):
            self.assertFalse(n3.claimed_name(name), name)

    def test_the_target_name_is_always_cci(self) -> None:
        self.assertEqual(n3.target_name("Game (USA).3ds"), "Game (USA).cci")
        self.assertEqual(n3.target_name("Game (USA).3DS"), "Game (USA).cci")
        self.assertEqual(n3.target_name("Game (USA).cci"), "Game (USA).cci")
        self.assertEqual(n3.target_name("Game (USA).CCI"), "Game (USA).cci")
        self.assertEqual(n3.target_name("Game.3ds.3ds"), "Game.3ds.cci")

    def test_a_decrypted_image_is_accepted(self) -> None:
        verdict = judge(ncsd())
        self.assertTrue(verdict.ok)
        self.assertEqual(verdict.status, n3.OK)
        self.assertEqual(verdict.describe(), "Nintendo 3DS image (decrypted)")

    def test_a_larger_file_than_the_header_says_is_accepted(self) -> None:
        self.assertTrue(judge(ncsd(padding=3000)).ok)  # the dump may carry trailing padding

    def test_an_encrypted_image_asks_for_a_decrypted_one(self) -> None:
        verdict = judge(ncsd(encrypted=True))
        self.assertEqual(verdict.status, n3.ENCRYPTED)
        self.assertFalse(verdict.ok)
        self.assertIn("encrypted", verdict.reason)
        self.assertIn("Azahar", verdict.reason)

    def test_only_the_no_crypto_bit_counts(self) -> None:
        self.assertTrue(judge(ncsd(flags=0x04)).ok)
        self.assertTrue(judge(ncsd(flags=0x05)).ok)  # other flags may be set beside it
        self.assertTrue(judge(ncsd(flags=0xFF)).ok)
        for flags in (0x00, 0x01, 0x02, 0x03, 0x08, 0x80):
            self.assertEqual(judge(ncsd(flags=flags)).status, n3.ENCRYPTED, flags)

    def test_a_bad_header_is_invalid_with_a_reason(self) -> None:
        cases = {
            "NCSD": ncsd(magic=b"NCSX"),
            "NCCH": ncsd(ncch_magic=b"NCCX"),
            "bigger": ncsd(size_units=1 << 20),  # the file is shorter than the header says
            "empty": ncsd(size_units=0),
            "no partition": ncsd(part0=0),
        }
        for want, data in cases.items():
            verdict = judge(data)
            self.assertEqual(verdict.status, n3.INVALID, want)
            self.assertIn(want, verdict.reason, want)

    def test_a_truncated_header_is_invalid(self) -> None:
        for data in (b"", b"x" * 0x103, bytes(0x200)):
            self.assertEqual(n3.judge(data, b"", 100000).status, n3.INVALID)
        # the NCCH header of partition 0 is cut short
        data = ncsd()
        self.assertEqual(n3.judge(data[:0x200], data[0x4000:0x4100], len(data)).status, n3.INVALID)

    def test_the_header_is_read_from_the_declared_offsets(self) -> None:
        head = bytearray(0x200)
        head[0x100:0x104] = b"NCSD"
        struct.pack_into("<I", head, 0x104, 0x40)
        struct.pack_into("<II", head, 0x120, 0x30, 0x10)
        self.assertEqual(n3.partition0_offset(bytes(head)), 0x30 * UNIT)
        self.assertIsNone(n3.partition0_offset(b"\x00" * 0x200))


class DetectTests(unittest.TestCase):
    def test_detect_reads_the_two_headers_only(self) -> None:
        reads: list[int] = []

        class Spy(io.BytesIO):
            def read(self, n: int | None = -1) -> bytes:
                reads.append(-1 if n is None else n)
                return super().read(n)

        self.assertTrue(n3.detect(Spy(ncsd(body_units=4096))).ok)
        self.assertTrue(reads and max(reads) <= 0x200, reads)

    def test_detect_agrees_with_judge(self) -> None:
        for data in (ncsd(), ncsd(encrypted=True), ncsd(magic=b"NCSX"), ncsd(size_units=1 << 20)):
            self.assertEqual(detect_bytes(data), judge(data))

    def test_a_file_too_short_for_its_partition_is_invalid(self) -> None:
        data = ncsd()
        self.assertEqual(detect_bytes(data[:0x4100]).status, n3.INVALID)
        self.assertEqual(detect_bytes(b"").status, n3.INVALID)


class FlowBase(Base):
    def go(self, **kw) -> int:  # type: ignore[no-untyped-def]
        return super().go(settings=kw.pop("settings", InboxSettings()), **kw)


class PlacementTests(FlowBase):
    def test_a_3ds_is_renamed_to_cci_with_the_same_bytes(self) -> None:
        data = ncsd()
        self.drop(NAME, data)
        self.assertEqual(self.go(), 0, self.out)
        self.assertEqual(self.library(), [f"n3ds/{CCI}"])
        self.assertEqual((self.roms / "n3ds" / CCI).read_bytes(), data)
        self.assertIn("detected: Nintendo 3DS image (decrypted)", self.out)
        self.assertIn(f"path: library/roms/n3ds/{CCI}", self.out)
        self.assertIn(whitelist_line("n3ds", CCI), self.out)
        self.assertEqual(self.done(), [NAME])
        self.assertEqual(self.review(), [])

    def test_a_cci_keeps_its_name(self) -> None:
        data = ncsd()
        self.drop(CCI, data)
        self.assertEqual(self.go(), 0, self.out)
        self.assertEqual((self.roms / "n3ds" / CCI).read_bytes(), data)

    def test_an_upper_case_extension_is_handled(self) -> None:
        self.drop("Big Game.3DS", ncsd())
        self.assertEqual(self.go(), 0, self.out)
        self.assertEqual(self.library(), ["n3ds/Big Game.cci"])

    def test_a_3ds_inside_a_folder(self) -> None:
        self.drop("pack", {f"sub/{NAME}": ncsd(), "cover.jpg": b"j", "info.txt": b"t"})
        self.assertEqual(self.go(), 0, self.out)
        self.assertEqual(self.library(), [f"n3ds/{CCI}"])

    def test_a_3ds_inside_a_zip(self) -> None:
        data = ncsd()
        self.zip_of("dump.zip", {f"x/{NAME}": data, "readme.txt": b"hi"})
        self.assertEqual(self.go(), 0, self.out)
        self.assertEqual((self.roms / "n3ds" / CCI).read_bytes(), data)
        self.assertNotIn("no DAT match", self.out)
        self.assertEqual(self.done(), ["dump.zip"])

    def test_two_games_in_one_item(self) -> None:
        self.drop("pack", {"One.3ds": ncsd(body_units=4), "Two.cci": ncsd(body_units=6)})
        self.assertEqual(self.go(), 0, self.out)
        self.assertEqual(self.library(), ["n3ds/One.cci", "n3ds/Two.cci"])

    def test_report_only_changes_nothing(self) -> None:
        self.drop(NAME, ncsd())
        self.assertEqual(self.go(apply=False), 0, self.out)
        self.assertIn("would place", self.out)
        self.assertIn(whitelist_line("n3ds", CCI), self.out)
        self.assertEqual(self.library(), [])
        self.assertFalse((self.roms / "n3ds").exists())
        self.assertEqual([p.name for p in self.inbox.iterdir()], [NAME])

    def test_report_only_does_not_create_the_folder_but_apply_does(self) -> None:
        self.drop(NAME, ncsd())
        self.assertEqual(self.go(apply=False), 0)
        self.assertFalse((self.roms / "n3ds").exists())
        self.assertEqual(self.go(), 0)
        self.assertTrue((self.roms / "n3ds" / CCI).is_file())

    def test_no_part_file_is_left_behind(self) -> None:
        self.drop(NAME, ncsd())
        self.assertEqual(self.go(), 0, self.out)
        self.assertEqual(sorted(p.name for p in (self.roms / "n3ds").iterdir()), [CCI])


class AlreadyThereTests(FlowBase):
    def test_identical_file_is_already_in_the_library(self) -> None:
        data = ncsd()
        self.drop(NAME, data)
        self.assertEqual(self.go(), 0, self.out)
        self.drop(NAME, data)
        self.assertEqual(self.go(), 0, self.out)
        self.assertIn("already in the library", self.out)
        self.assertEqual(self.library(), [f"n3ds/{CCI}"])
        self.assertEqual(len(self.done()), 2)

    def test_a_cci_and_a_3ds_of_the_same_game_are_the_same_file(self) -> None:
        data = ncsd()
        self.drop(CCI, data)
        self.assertEqual(self.go(), 0, self.out)
        self.drop(NAME, data)
        self.assertEqual(self.go(), 0, self.out)
        self.assertIn("already in the library", self.out)
        self.assertEqual(self.library(), [f"n3ds/{CCI}"])

    def test_a_different_file_with_the_same_name_is_never_replaced(self) -> None:
        self.drop(NAME, ncsd(body_units=4))
        self.assertEqual(self.go(), 0)
        kept = (self.roms / "n3ds" / CCI).read_bytes()
        self.drop(NAME, ncsd(body_units=6))
        self.assertEqual(self.go(), 1, self.out)
        self.assertEqual((self.roms / "n3ds" / CCI).read_bytes(), kept)
        self.assertIn("different file", self.reason(NAME))
        self.assertEqual(self.review(), [NAME, f"{NAME}.reason.txt"])
        self.assertEqual(self.library(), [f"n3ds/{CCI}"])

    def test_a_different_file_is_not_replaced_under_a_cci_name_either(self) -> None:
        (self.roms / "n3ds").mkdir()
        existing = ncsd(body_units=4)
        (self.roms / "n3ds" / CCI).write_bytes(existing)
        self.drop(CCI, ncsd(body_units=6))
        self.assertEqual(self.go(), 1, self.out)
        self.assertEqual((self.roms / "n3ds" / CCI).read_bytes(), existing)

    def test_report_only_says_it_exists_and_touches_nothing(self) -> None:
        data = ncsd()
        (self.roms / "n3ds").mkdir()
        (self.roms / "n3ds" / CCI).write_bytes(data)
        self.drop(NAME, ncsd(body_units=6))
        self.assertEqual(self.go(apply=False), 0, self.out)
        self.assertIn("already exists", self.out)
        self.assertEqual((self.roms / "n3ds" / CCI).read_bytes(), data)
        self.assertEqual([p.name for p in self.inbox.iterdir()], [NAME])

    def test_two_files_of_one_item_for_the_same_target_go_to_review(self) -> None:
        self.drop("pack", {"Same.3ds": ncsd(body_units=4), "Same.cci": ncsd(body_units=6)})
        self.assertEqual(self.go(), 1, self.out)
        self.assertEqual(len(self.library()), 1)
        self.assertIn("Same.cci", self.reason("pack"))


class ReviewTests(FlowBase):
    def test_an_encrypted_image_goes_to_review_and_is_not_placed(self) -> None:
        self.drop("Locked.3ds", ncsd(encrypted=True))
        self.assertEqual(self.go(), 1, self.out)
        self.assertIn("encrypted", self.reason("Locked.3ds"))
        self.assertIn("Azahar", self.reason("Locked.3ds"))
        self.assertEqual(self.library(), [])
        self.assertEqual(self.review(), ["Locked.3ds", "Locked.3ds.reason.txt"])

    def test_an_encrypted_cci_goes_to_review(self) -> None:
        self.drop("Locked.cci", ncsd(encrypted=True))
        self.assertEqual(self.go(), 1, self.out)
        self.assertEqual(self.library(), [])

    def test_a_bad_header_goes_to_review_with_a_reason(self) -> None:
        self.drop("Bogus.3ds", b"just some bytes" * 1000)
        self.assertEqual(self.go(), 1, self.out)
        self.assertIn("NCSD", self.reason("Bogus.3ds"))
        self.assertNotIn("no DAT match", self.reason("Bogus.3ds"))
        self.assertEqual(self.library(), [])

    def test_a_truncated_image_goes_to_review(self) -> None:
        self.drop("Cut.cci", ncsd()[:0x4100])
        self.assertEqual(self.go(), 1, self.out)
        self.assertEqual(self.library(), [])

    def test_the_report_names_the_reason_without_applying(self) -> None:
        self.drop("Locked.3ds", ncsd(encrypted=True))
        self.assertEqual(self.go(apply=False), 1, self.out)
        self.assertIn("encrypted", self.out)
        self.assertEqual([p.name for p in self.inbox.iterdir()], ["Locked.3ds"])

    def test_one_good_and_one_encrypted_image_in_a_folder(self) -> None:
        self.drop("pack", {"Good.3ds": ncsd(), "Bad.3ds": ncsd(encrypted=True)})
        self.assertEqual(self.go(), 1, self.out)
        self.assertEqual(self.library(), ["n3ds/Good.cci"])
        self.assertIn("Bad.3ds", self.reason("pack"))

    def test_other_files_are_named_in_the_reason(self) -> None:
        self.drop("pack", {NAME: ncsd(), "update.cia": b"c", "cover.jpg": b"j"})
        self.assertEqual(self.go(), 1, self.out)
        self.assertEqual(self.library(), [f"n3ds/{CCI}"])
        self.assertIn("not a 3DS image, drop it as its own item: update.cia", self.reason("pack"))

    def test_3dsx_homebrew_is_not_claimed(self) -> None:
        self.drop("Tool.3dsx", ncsd())
        self.assertEqual(self.go(), 1, self.out)
        self.assertEqual(self.library(), [])
        self.assertIn("no DAT match", self.reason("Tool.3dsx"))

    def test_a_symlink_is_not_followed(self) -> None:
        with tempfile.TemporaryDirectory() as other:
            target = Path(other) / "real.3ds"
            target.write_bytes(ncsd())
            link = self.inbox / "Link.3ds"
            link.symlink_to(target)
            age(link)
            self.go()
        self.assertEqual(self.library(), [])


class RegistryTests(unittest.TestCase):
    def test_the_handler_is_registered(self) -> None:
        from warpzone import inbox

        self.assertIn(n3.HANDLER, inbox.kind_handlers())
