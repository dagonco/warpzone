import io
import struct
import tempfile
import unittest
from pathlib import Path

from inbox_support import age
from test_inbox import Base
from warpzone import inbox_dolphin as dl
from warpzone.config import InboxSettings
from warpzone.inbox_core import Tools, whitelist_line

WII_MAGIC = bytes.fromhex("5D1C9EA3")
GC_MAGIC = bytes.fromhex("C2339F3D")


def image(
    *,
    magic: bytes = b"RVZ\x01",
    disc_type: int = 2,
    game_id: bytes = b"RTSE01",
    title: bytes = b"Test Quest Wii",
    wii_magic: bytes | None = None,
    gc_magic: bytes | None = None,
    recorded: int | None = None,
    body: int = 0,
) -> bytes:
    """A synthetic WIA or RVZ file: the container header, the disc header, then `body` bytes.
    `recorded` is the file size written in the header (the real size by default)."""
    disc = bytearray(0x80)
    disc[:6] = game_id
    wii = WII_MAGIC if wii_magic is None and disc_type == 2 else wii_magic
    gc = GC_MAGIC if gc_magic is None and disc_type == 1 else gc_magic
    if wii:
        disc[0x18:0x1C] = wii
    if gc:
        disc[0x1C:0x20] = gc
    disc[0x20 : 0x20 + len(title)] = title
    head = bytearray(0x58)
    head[:4] = magic
    struct.pack_into(">I", head, 0x48, disc_type)
    data = bytearray(head + disc + bytes(body))
    struct.pack_into(">Q", data, 0x2C, len(data) if recorded is None else recorded)
    return bytes(data)


def judge(data: bytes) -> dl.Verdict:
    return dl.judge(data[: dl.HEADER_BYTES], len(data))


class PureTests(unittest.TestCase):
    def test_claimed_names(self) -> None:
        for name in ("A.rvz", "A.RVZ", "b.wia", "c/d.WIA", "Game (USA).rvz"):
            self.assertTrue(dl.claimed_name(name), name)
        for name in ("A.gcz", "A.iso", "A.rvz.zip", "A.rvz.bak", "rvz", "A.rv"):
            self.assertFalse(dl.claimed_name(name), name)

    def test_a_wii_rvz(self) -> None:
        v = judge(image())
        self.assertTrue(v.ok)
        self.assertEqual(
            (v.system, v.game_id, v.title, v.container), ("wii", "RTSE01", "Test Quest Wii", "RVZ")
        )
        self.assertEqual(v.describe(), "Wii disc image (RVZ), game ID RTSE01 Test Quest Wii")

    def test_a_gamecube_rvz(self) -> None:
        v = judge(image(disc_type=1, game_id=b"GTSE01", title=b"Test Cube"))
        self.assertEqual((v.system, v.game_id, v.title), ("gc", "GTSE01", "Test Cube"))

    def test_wia_has_the_same_layout(self) -> None:
        v = judge(image(magic=b"WIA\x01"))
        self.assertEqual((v.system, v.game_id, v.container), ("wii", "RTSE01", "WIA"))
        v = judge(image(magic=b"WIA\x01", disc_type=1, game_id=b"GTSP01"))
        self.assertEqual((v.system, v.container), ("gc", "WIA"))

    def test_the_offsets_of_a_real_header(self) -> None:
        # the first bytes of a real Wii RVZ, with the title and ID replaced by fakes
        data = bytearray(image(body=100))
        self.assertEqual(data[0x48:0x4C], b"\x00\x00\x00\x02")
        self.assertEqual(data[0x58:0x5E], b"RTSE01")
        self.assertEqual(data[0x70:0x74], WII_MAGIC)
        self.assertEqual(data[0x78:0x86], b"Test Quest Wii")

    def test_a_trailing_size_that_is_not_recorded_is_accepted(self) -> None:
        self.assertTrue(judge(image(recorded=0, body=10)).ok)

    def test_a_file_of_another_size_than_recorded_is_a_truncated_copy(self) -> None:
        data = image(recorded=5000, body=10)
        v = judge(data)
        self.assertFalse(v.ok)
        self.assertIn("truncated", v.reason)

    def test_wrong_magic(self) -> None:
        for data in (image(magic=b"RVZ\x02"), image(magic=b"GCZ\x01"), b"\x00" * 300, b""):
            v = dl.judge(data[: dl.HEADER_BYTES], len(data))
            self.assertEqual(v.status, dl.INVALID)
            self.assertIn("no WIA or RVZ magic", v.reason)

    def test_a_truncated_header(self) -> None:
        data = image()
        for cut in (4, 0x48, 0x58, dl.HEADER_BYTES - 1):
            v = dl.judge(data[:cut], cut)
            self.assertEqual(v.status, dl.INVALID, cut)
            self.assertIn("cut short", v.reason)

    def test_disc_type_and_magic_must_agree(self) -> None:
        cases = {
            "wii says, no magic": image(disc_type=2, wii_magic=b"\x00" * 4),
            "gc says, no magic": image(disc_type=1, gc_magic=b"\x00" * 4),
            "gc says, wii magic": image(disc_type=1, gc_magic=b"\x00" * 4, wii_magic=WII_MAGIC),
            "wii says, gc magic": image(disc_type=2, wii_magic=b"\x00" * 4, gc_magic=GC_MAGIC),
            "both magics": image(disc_type=2, gc_magic=GC_MAGIC),
        }
        for what, data in cases.items():
            v = judge(data)
            self.assertEqual(v.status, dl.INVALID, what)
            self.assertIn("matching magic", v.reason, what)

    def test_an_unknown_disc_type(self) -> None:
        v = judge(image(disc_type=3))
        self.assertEqual(v.status, dl.INVALID)
        self.assertIn("unknown disc type (3)", v.reason)

    def test_a_bad_game_id(self) -> None:
        for game_id in (b"\x00" * 6, b"rtse01", b"RT E01", b"RTS\xe901"):
            v = judge(image(game_id=game_id))
            self.assertEqual(v.status, dl.INVALID, game_id)
            self.assertIn("game ID", v.reason)

    def test_the_title_is_made_safe_for_a_file_name(self) -> None:
        self.assertEqual(dl.clean_title(b"Zelda: The Game\x00junk"), "Zelda - The Game")
        self.assertEqual(dl.clean_title(b'a/b\\c*d?e"f<g>h|i'), "abcdefghi")
        self.assertEqual(dl.clean_title(b"  Two   words.  "), "Two words")
        self.assertEqual(dl.clean_title(b"Caf\xe9 Wii"), "Caf Wii")
        self.assertEqual(dl.clean_title(b"\x00" * 64), "")
        self.assertEqual(dl.clean_title(b"..."), "")

    def test_the_file_name_names_the_image(self) -> None:
        v = judge(image())
        self.assertEqual(
            dl.target_name(v, "Test Quest (USA) (En,Fr,Es).rvz"),
            "Test Quest (USA) (En,Fr,Es) [RTSE01].rvz",
        )
        self.assertEqual(dl.target_name(v, "Test Quest.WIA"), "Test Quest [RTSE01].wia")
        self.assertEqual(dl.target_name(v, "Café: Wii.rvz"), "Café - Wii [RTSE01].rvz")
        self.assertEqual(dl.target_name(v, 'a*b?c"d<e>f|g.rvz'), "abcdefg [RTSE01].rvz")

    def test_a_name_that_already_carries_the_game_id_keeps_it_once(self) -> None:
        v = judge(image())
        self.assertEqual(dl.target_name(v, "Test Quest [RTSE01].rvz"), "Test Quest [RTSE01].rvz")
        self.assertEqual(dl.target_name(v, "Test Quest [rtse01].rvz"), "Test Quest [RTSE01].rvz")

    def test_the_header_title_names_an_image_whose_file_name_says_nothing(self) -> None:
        v = judge(image())
        for name in ("RTSE01.rvz", "rtse01.RVZ", "[RTSE01].rvz", " ..rvz", ".rvz"):
            self.assertEqual(dl.target_name(v, name), "Test Quest Wii [RTSE01].rvz", name)
        untitled = judge(image(title=b""))
        self.assertEqual(dl.target_name(untitled, "RTSE01.rvz"), "[RTSE01].rvz")


class DetectTests(unittest.TestCase):
    def test_detect_reads_the_header_only(self) -> None:
        reads: list[int] = []

        class Spy(io.BytesIO):
            def read(self, n: int | None = -1) -> bytes:
                reads.append(-1 if n is None else n)
                return super().read(n)

        self.assertTrue(dl.detect(Spy(image(body=100000))).ok)
        self.assertEqual(reads, [dl.HEADER_BYTES])

    def test_detect_agrees_with_judge(self) -> None:
        for data in (image(), image(disc_type=1), image(magic=b"XXXX"), image(recorded=9)):
            self.assertEqual(dl.detect(io.BytesIO(data)), judge(data))
        self.assertEqual(dl.detect(io.BytesIO(b"")).status, dl.INVALID)


class FlowBase(Base):
    def go(self, **kw) -> int:  # type: ignore[no-untyped-def]
        return super().go(settings=kw.pop("settings", InboxSettings()), **kw)


WII = "Test Quest Wii [RTSE01].rvz"
CUBE = "Test Cube [GTSE01].rvz"


class PlacementTests(FlowBase):
    def test_a_wii_rvz_is_filed_in_wii_under_its_file_name(self) -> None:
        data = image()
        self.drop("Test Quest (USA) (En,Fr,Es).rvz", data)
        self.assertEqual(self.go(), 0, self.out)
        named = "Test Quest (USA) (En,Fr,Es) [RTSE01].rvz"
        self.assertEqual(self.library(), [f"wii/{named}"])
        self.assertEqual((self.roms / "wii" / named).read_bytes(), data)
        self.assertIn("detected: Wii disc image (RVZ), game ID RTSE01 Test Quest Wii", self.out)
        self.assertIn(f"path: library/roms/wii/{named}", self.out)
        self.assertIn(whitelist_line("wii", named), self.out)
        self.assertNotIn("no DAT match", self.out)
        self.assertEqual(self.done(), ["Test Quest (USA) (En,Fr,Es).rvz"])

    def test_a_file_named_by_its_game_id_takes_the_header_title(self) -> None:
        self.drop("RTSE01.rvz", image())
        self.assertEqual(self.go(), 0, self.out)
        self.assertEqual(self.library(), [f"wii/{WII}"])
        self.assertIn(f"RTSE01.rvz is filed as {WII}", self.out)

    def test_a_gamecube_rvz_is_filed_in_gc(self) -> None:
        data = image(disc_type=1, game_id=b"GTSE01", title=b"Test Cube")
        self.drop("Test Cube.rvz", data)
        self.assertEqual(self.go(), 0, self.out)
        self.assertEqual(self.library(), [f"gc/{CUBE}"])
        self.assertEqual((self.roms / "gc" / CUBE).read_bytes(), data)

    def test_a_wia_keeps_its_extension(self) -> None:
        self.drop("Test Quest Wii.WIA", image(magic=b"WIA\x01"))
        self.assertEqual(self.go(), 0, self.out)
        self.assertEqual(self.library(), ["wii/Test Quest Wii [RTSE01].wia"])

    def test_inside_a_folder_and_a_zip(self) -> None:
        self.drop("pack", {"sub/Test Quest Wii.rvz": image(), "cover.jpg": b"j", "info.txt": b"t"})
        self.assertEqual(self.go(), 0, self.out)
        self.zip_of("dump.zip", {"x/Other.rvz": image(game_id=b"RTSP01", title=b"Other")})
        self.assertEqual(self.go(), 0, self.out)
        self.assertEqual(self.library(), ["wii/Other [RTSP01].rvz", f"wii/{WII}"])

    def test_report_only_changes_nothing(self) -> None:
        self.drop("Test Quest Wii.rvz", image())
        self.assertEqual(self.go(apply=False), 0, self.out)
        self.assertIn("would place", self.out)
        self.assertIn(whitelist_line("wii", WII), self.out)
        self.assertFalse((self.roms / "wii").exists())
        self.assertEqual([p.name for p in self.inbox.iterdir()], ["Test Quest Wii.rvz"])

    def test_no_part_file_is_left_behind(self) -> None:
        self.drop("Test Quest Wii.rvz", image())
        self.assertEqual(self.go(), 0, self.out)
        self.assertEqual(sorted(p.name for p in (self.roms / "wii").iterdir()), [WII])

    def test_no_tool_is_needed(self) -> None:
        self.drop("Test Quest Wii.rvz", image())
        self.assertEqual(self.go(tools=Tools()), 0, self.out)
        self.assertEqual(self.library(), [f"wii/{WII}"])


class AlreadyThereTests(FlowBase):
    def test_identical_file_is_already_in_the_library(self) -> None:
        data = image(body=50)
        self.drop("Test Quest Wii.rvz", data)
        self.assertEqual(self.go(), 0, self.out)
        self.drop("Test Quest Wii [RTSE01].rvz", data)
        self.assertEqual(self.go(), 0, self.out)
        self.assertIn("already in the library", self.out)
        self.assertEqual(self.library(), [f"wii/{WII}"])
        self.assertEqual(len(self.done()), 2)

    def test_a_different_file_with_the_same_name_is_never_replaced(self) -> None:
        self.drop("Test Quest Wii.rvz", image(body=50))
        self.assertEqual(self.go(), 0)
        kept = (self.roms / "wii" / WII).read_bytes()
        self.drop("RTSE01.rvz", image(body=80))
        self.assertEqual(self.go(), 1, self.out)
        self.assertEqual((self.roms / "wii" / WII).read_bytes(), kept)
        self.assertIn("different file", self.reason("RTSE01.rvz"))

    def test_report_only_says_it_exists_and_touches_nothing(self) -> None:
        (self.roms / "wii").mkdir()
        (self.roms / "wii" / WII).write_bytes(image(body=50))
        self.drop("Test Quest Wii.rvz", image(body=80))
        self.assertEqual(self.go(apply=False), 0, self.out)
        self.assertIn("already exists", self.out)
        self.assertEqual([p.name for p in self.inbox.iterdir()], ["Test Quest Wii.rvz"])

    def test_two_files_of_one_item_with_the_same_target_go_to_review(self) -> None:
        self.drop(
            "pack", {"one/Test Quest Wii.rvz": image(body=5), "two/RTSE01.rvz": image(body=9)}
        )
        self.assertEqual(self.go(), 1, self.out)
        self.assertEqual(len(self.library()), 1)
        self.assertIn("RTSE01.rvz", self.reason("pack"))


class ReviewTests(FlowBase):
    def test_a_file_that_is_not_an_image_goes_to_review_with_a_reason(self) -> None:
        self.drop("fake.rvz", b"just some bytes" * 100)
        self.assertEqual(self.go(), 1, self.out)
        self.assertIn("no WIA or RVZ magic", self.reason("fake.rvz"))
        self.assertNotIn("no DAT match", self.reason("fake.rvz"))
        self.assertEqual(self.library(), [])
        self.assertEqual(self.review(), ["fake.rvz", "fake.rvz.reason.txt"])

    def test_a_truncated_copy_goes_to_review(self) -> None:
        self.drop("cut.rvz", image(recorded=10**9, body=64))
        self.assertEqual(self.go(), 1, self.out)
        self.assertIn("truncated", self.reason("cut.rvz"))
        self.assertEqual(self.library(), [])

    def test_a_cut_header_goes_to_review(self) -> None:
        self.drop("cut.rvz", image()[:0x60])
        self.assertEqual(self.go(), 1, self.out)
        self.assertIn("cut short", self.reason("cut.rvz"))

    def test_one_good_and_one_bad_image_in_a_folder(self) -> None:
        self.drop("pack", {"Test Quest Wii.rvz": image(), "bad.rvz": image(magic=b"XXXX")})
        self.assertEqual(self.go(), 1, self.out)
        self.assertEqual(self.library(), [f"wii/{WII}"])
        self.assertIn("bad.rvz", self.reason("pack"))

    def test_other_files_are_named_in_the_reason(self) -> None:
        self.drop("pack", {"a.rvz": image(), "extra.bin": b"n"})
        self.assertEqual(self.go(), 1, self.out)
        self.assertIn(
            "not a Wii or GameCube image, drop it as its own item: extra.bin", self.reason("pack")
        )

    def test_a_symlink_is_not_followed(self) -> None:
        with tempfile.TemporaryDirectory() as other:
            target = Path(other) / "real.rvz"
            target.write_bytes(image())
            link = self.inbox / "Link.rvz"
            link.symlink_to(target)
            age(link)
            self.go()
        self.assertEqual(self.library(), [])


class RegistryTests(unittest.TestCase):
    def test_the_handler_is_registered(self) -> None:
        from warpzone import inbox

        self.assertIn(dl.HANDLER, inbox.kind_handlers())
