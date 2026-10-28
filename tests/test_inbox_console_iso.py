import io
import os
import tempfile
import unittest
from pathlib import Path

from console_iso_support import REDUMP, XGD2, XGD3, ps3_iso, sfo, write_xdvdfs, xdvdfs_dir
from inbox_support import age, make_stub, write_dat
from test_inbox import Base
from warpzone import inbox_console_iso as ci
from warpzone import inbox_mame
from warpzone.config import InboxSettings, parse_inbox
from warpzone.inbox import Tools, whitelist_line

PS3_NAME = "Test Game (Europe) (En,Fr).dec.iso"
XBOX_NAME = "Brick Racer (USA).xiso.iso"
X360_NAME = "Plain Shooter (USA).iso"
GAMELIST = ("frontend", "es-de", "gamelists", "ps3", "gamelist.xml")


def detect_bytes(data: bytes) -> ci.Detected | None:
    return ci.detect(io.BytesIO(data))


class PureTests(unittest.TestCase):
    def test_claimed_names(self) -> None:
        for name in ("A.dec.iso", "A.DEC.ISO", "b.xiso.iso", "c.xiso", "d/e.XISO.iso"):
            self.assertTrue(ci.claimed_name(name), name)
        for name in ("A.iso", "A.dec", "A.xiso.zip", "A.iso.xiso.bak", "decrypted.iso"):
            self.assertFalse(ci.claimed_name(name), name)

    def test_destinations(self) -> None:
        self.assertEqual(ci.destination(ci.PS3), "ps3")
        self.assertEqual(ci.destination(ci.XBOX), "xbox")
        self.assertEqual(ci.destination(ci.XBOX360), "xbox360/roms")
        self.assertIsNone(ci.destination(ci.XBOX_REDUMP))

    def test_sfo_strings_and_integers(self) -> None:
        values = ci.parse_sfo(sfo({"TITLE": "A  Game\n2", "TITLE_ID": "BLUS00001", "N": 7}))
        self.assertEqual(values, {"TITLE": "A Game 2", "TITLE_ID": "BLUS00001", "N": 7})
        self.assertEqual(ci.parse_sfo(b""), {})
        self.assertEqual(ci.parse_sfo(b"\x00PSF" + bytes(8)), {})
        self.assertEqual(ci.parse_sfo(b"not an sfo at all, just text"), {})

    def test_xdvdfs_tree_is_walked_not_scanned(self) -> None:
        names = ["default.xbe", "a.bin", "media", "zzz.dat", "Data.pak"]
        entries = ci.xdvdfs_entries(xdvdfs_dir({n: 40 + i for i, n in enumerate(names)}))
        self.assertEqual(sorted(e.name for e in entries), sorted(names))
        self.assertEqual(ci.xdvdfs_entries(b"\xff" * 2048), [])
        self.assertEqual(ci.xdvdfs_entries(b""), [])

    def test_xdvdfs_loop_does_not_hang(self) -> None:
        loop = b"\x00\x00\x00\x00" + bytes(8) + bytes([0, 1]) + b"a" + bytes(1)
        self.assertEqual(len(ci.xdvdfs_entries(loop)), 1)

    def test_ps3_verdict(self) -> None:
        found = detect_bytes(ps3_iso("BLES01234", "Some Racing Game"))
        self.assertEqual(found, ci.Detected(ci.PS3, "BLES01234", "Some Racing Game"))
        assert found
        self.assertEqual(found.describe(), "PS3 decrypted disc image: BLES01234 Some Racing Game")

    def test_a_plain_iso_without_the_ps3_files_is_not_ps3(self) -> None:
        self.assertIsNone(detect_bytes(ps3_iso(sfb=False)))
        self.assertIsNone(detect_bytes(bytes(16 * 2048) + b"\x01CD001" + bytes(4000)))

    def test_nothing_else_is_recognised(self) -> None:
        for data in (b"", b"x" * 100, bytes(0x20000), b"NES\x1a" + bytes(5000)):
            self.assertIsNone(detect_bytes(data))


class ConfigTests(unittest.TestCase):
    def test_gamelist_ps3_is_a_known_key(self) -> None:
        errors: list[str] = []
        settings = parse_inbox({"gamelist_ps3": "/x/gl.xml"}, errors)
        self.assertEqual(errors, [])
        self.assertEqual(settings.gamelist_ps3, "/x/gl.xml")


class FileVerdictTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)

    def kind(self, name: str, base: int, exe: str) -> str | None:
        found = ci.detect_path(write_xdvdfs(self.tmp / name, base, exe))
        return found.kind if found else None

    def test_xbox_is_told_from_the_360_by_the_root_executable(self) -> None:
        self.assertEqual(self.kind("a.iso", 0, "default.xbe"), ci.XBOX)
        self.assertEqual(self.kind("b.iso", 0, "DEFAULT.XEX"), ci.XBOX360)

    def test_360_partitions(self) -> None:
        self.assertEqual(self.kind("c.iso", XGD2, "default.xex"), ci.XBOX360)
        self.assertEqual(self.kind("d.iso", XGD3, "default.xex"), ci.XBOX360)

    def test_a_redump_layout_xbox_image_is_flagged_not_placed(self) -> None:
        self.assertEqual(self.kind("e.iso", REDUMP, "default.xbe"), ci.XBOX_REDUMP)

    def test_unexpected_combinations_are_not_recognised(self) -> None:
        self.assertIsNone(self.kind("f.iso", XGD3, "default.xbe"))
        self.assertIsNone(self.kind("g.iso", REDUMP, "default.xex"))
        self.assertIsNone(self.kind("h.iso", 0, "readme.txt"))


class FlowBase(Base):
    def setUp(self) -> None:
        super().setUp()
        self.gamelist = self.hub.joinpath(*GAMELIST)

    def go(self, **kw) -> int:  # type: ignore[no-untyped-def]
        return super().go(settings=kw.pop("settings", InboxSettings()), **kw)

    def drop_link(self, name: str, source: Path) -> Path:
        """A (sparse) image in the inbox without copying it: a hard link, made old."""
        target = self.inbox / name
        os.link(source, target)
        age(target)
        return target

    def emulators(self) -> dict[str, str | None]:
        return inbox_mame.gamelist_emulators(self.gamelist.read_text(encoding="utf-8"))


class PlacementTests(FlowBase):
    def test_ps3_is_placed_with_its_emulator_in_the_gamelist(self) -> None:
        data = ps3_iso("BLES01234", "Some Racing Game")
        self.drop(PS3_NAME, data)
        self.assertEqual(self.go(), 0, self.out)
        self.assertEqual((self.roms / "ps3" / PS3_NAME).read_bytes(), data)
        self.assertEqual(self.emulators(), {PS3_NAME: ci.EMU_RPCS3_ISO})
        self.assertIn("detected: PS3 decrypted disc image: BLES01234 Some Racing Game", self.out)
        self.assertIn(f"path: library/roms/ps3/{PS3_NAME}", self.out)
        self.assertIn(whitelist_line("ps3", PS3_NAME), self.out)
        self.assertEqual(self.done(), [PS3_NAME])
        self.assertEqual(self.review(), [])

    def test_existing_gamelist_entries_are_kept(self) -> None:
        self.gamelist.parent.mkdir(parents=True)
        self.gamelist.write_text(
            '<?xml version="1.0"?>\n<gameList>\n\t<game>\n\t\t<path>./Old.iso</path>\n'
            "\t\t<name>Old</name>\n\t</game>\n</gameList>\n",
            encoding="utf-8",
        )
        self.drop(PS3_NAME, ps3_iso())
        self.assertEqual(self.go(), 0, self.out)
        self.assertEqual(self.emulators(), {"Old.iso": None, PS3_NAME: ci.EMU_RPCS3_ISO})
        self.assertIn("<name>Old</name>", self.gamelist.read_text(encoding="utf-8"))
        backups = self.hub / "tools" / "logs" / "gamelist-backups"
        (backup,) = backups.iterdir()
        self.assertRegex(backup.name, r"^ps3-gamelist-\d{8}-\d{6}\.xml$")
        self.assertIn("<name>Old</name>", backup.read_text(encoding="utf-8"))
        self.assertFalse(self.gamelist.with_name("gamelist.xml.bak").exists())

    def test_the_gamelist_path_can_be_configured(self) -> None:
        elsewhere = self.tmp / "gl" / "ps3.xml"
        self.drop(PS3_NAME, ps3_iso())
        self.assertEqual(self.go(settings=InboxSettings(gamelist_ps3=str(elsewhere))), 0)
        self.assertEqual(
            inbox_mame.gamelist_emulators(elsewhere.read_text(encoding="utf-8")),
            {PS3_NAME: ci.EMU_RPCS3_ISO},
        )
        self.assertFalse(self.gamelist.exists())

    def test_xbox_xiso_iso_goes_to_xbox_without_a_gamelist(self) -> None:
        source = write_xdvdfs(self.tmp / "src.iso", 0, "default.xbe")
        self.drop(XBOX_NAME, source.read_bytes())
        self.assertEqual(self.go(), 0, self.out)
        self.assertEqual((self.roms / "xbox" / XBOX_NAME).read_bytes(), source.read_bytes())
        self.assertIn("detected: Xbox (XISO)", self.out)
        self.assertIn(whitelist_line("xbox", XBOX_NAME), self.out)
        self.assertFalse(self.gamelist.exists())

    def test_a_bare_xiso_is_claimed_by_name(self) -> None:
        source = write_xdvdfs(self.tmp / "src.iso", 0, "default.xbe")
        self.drop("Brick Racer (USA).xiso", source.read_bytes())
        self.assertEqual(self.go(), 0, self.out)
        self.assertEqual(self.library(), ["xbox/Brick Racer (USA).xiso"])

    def test_a_plain_360_iso_is_placed_after_the_dats_do_not_match(self) -> None:
        source = write_xdvdfs(self.tmp / "src.iso", XGD3, "default.xex")
        self.drop(X360_NAME, source.read_bytes())
        self.assertEqual(self.go(), 0, self.out)
        self.assertEqual(self.library(), [f"xbox360/roms/{X360_NAME}"])
        self.assertIn("detected: Xbox 360 disc image", self.out)
        self.assertIn(whitelist_line("xbox360/roms", X360_NAME), self.out)
        self.assertIn("!/roms/xbox360/roms/Plain Shooter (USA).iso", self.out)

    def test_a_plain_ps3_iso_is_placed_after_the_dats_do_not_match(self) -> None:
        self.drop("Game.iso", ps3_iso("NPUB00001", "Digital Game"))
        self.assertEqual(self.go(), 0, self.out)
        self.assertEqual(self.library(), ["ps3/Game.iso"])
        self.assertEqual(self.emulators(), {"Game.iso": ci.EMU_RPCS3_ISO})

    def test_report_only_changes_nothing(self) -> None:
        self.drop(PS3_NAME, ps3_iso())
        self.assertEqual(self.go(apply=False), 0, self.out)
        self.assertIn("would place", self.out)
        self.assertIn(f"would add the emulator of {PS3_NAME}", self.out)
        self.assertEqual(self.library(), [])
        self.assertFalse(self.gamelist.exists())
        self.assertEqual([p.name for p in self.inbox.iterdir()], [PS3_NAME])

    def test_two_images_in_a_folder_and_leftovers_named_in_the_reason(self) -> None:
        self.drop("pack", {PS3_NAME: ps3_iso(), "cover.jpg": b"j", "notes.bin": b"n"})
        self.assertEqual(self.go(), 1, self.out)
        self.assertEqual(self.library(), [f"ps3/{PS3_NAME}"])
        self.assertIn("not a console disc image, drop it as its own item: notes.bin", self.out)


class AlreadyThereTests(FlowBase):
    def test_identical_file_is_already_in_the_library(self) -> None:
        data = ps3_iso()
        self.drop(PS3_NAME, data)
        self.assertEqual(self.go(), 0, self.out)
        self.drop(PS3_NAME, data)
        self.assertEqual(self.go(), 0, self.out)
        self.assertIn("already in the library", self.out)
        self.assertEqual(self.library(), [f"ps3/{PS3_NAME}"])
        self.assertEqual(len(self.done()), 2)

    def test_a_different_file_with_the_same_name_is_never_replaced(self) -> None:
        self.drop(PS3_NAME, ps3_iso("BLES00001", "One"))
        self.assertEqual(self.go(), 0)
        kept = (self.roms / "ps3" / PS3_NAME).read_bytes()
        self.drop(PS3_NAME, ps3_iso("BLES00002", "Two"))
        self.assertEqual(self.go(), 1, self.out)
        self.assertEqual((self.roms / "ps3" / PS3_NAME).read_bytes(), kept)
        self.assertIn("different file", self.reason(PS3_NAME))
        self.assertEqual(self.review(), [PS3_NAME, f"{PS3_NAME}.reason.txt"])

    def test_report_only_says_it_exists(self) -> None:
        self.drop(PS3_NAME, ps3_iso())
        self.assertEqual(self.go(), 0)
        self.drop(PS3_NAME, ps3_iso())
        self.assertEqual(self.go(apply=False), 0, self.out)
        self.assertIn("already exists", self.out)

    def test_an_existing_ps3_game_gets_its_missing_gamelist_entry(self) -> None:
        data = ps3_iso()
        (self.roms / "ps3").mkdir()
        (self.roms / "ps3" / PS3_NAME).write_bytes(data)
        self.drop(PS3_NAME, data)
        self.assertEqual(self.go(), 0, self.out)
        self.assertEqual(self.emulators(), {PS3_NAME: ci.EMU_RPCS3_ISO})


class ReviewTests(FlowBase):
    def test_a_redump_layout_xbox_image_asks_for_a_conversion(self) -> None:
        source = write_xdvdfs(self.tmp / "redump.iso", REDUMP, "default.xbe")
        self.drop_link("Full Dump (USA).xiso.iso", source)
        self.assertEqual(self.go(), 1, self.out)
        self.assertIn("extract-xiso", self.reason("Full Dump (USA).xiso.iso"))
        self.assertEqual(self.library(), [])
        self.assertIn("0x18300000", self.reason("Full Dump (USA).xiso.iso"))

    def test_the_same_image_under_a_plain_name_is_flagged_too(self) -> None:
        source = write_xdvdfs(self.tmp / "redump.iso", REDUMP, "default.xbe")
        self.drop_link("Full Dump (USA).iso", source)
        self.assertEqual(self.go(), 1, self.out)
        self.assertIn("extract-xiso", self.reason("Full Dump (USA).iso"))
        self.assertNotIn("no DAT match", self.reason("Full Dump (USA).iso"))
        self.assertEqual(self.library(), [])

    def test_a_claimed_name_with_the_wrong_content_is_not_placed(self) -> None:
        self.drop("Bogus.dec.iso", b"just some bytes" * 100)
        self.assertEqual(self.go(), 1, self.out)
        self.assertIn("not recognised", self.reason("Bogus.dec.iso"))
        self.assertEqual(self.library(), [])

    def test_an_encrypted_looking_ps3_iso_without_param_sfo_is_not_placed(self) -> None:
        self.drop("Locked.dec.iso", ps3_iso(sfb=False))
        self.assertEqual(self.go(), 1, self.out)
        self.assertIn("not recognised", self.reason("Locked.dec.iso"))

    def test_a_ps2_iso_in_a_dat_keeps_its_normal_route(self) -> None:
        ps2 = b"ps2 dvd image " * 5000
        write_dat(
            self.dats / "redump", "Sony - PlayStation 2", [("Old Pad (USA)", [("o.iso", ps2)])]
        )
        (self.roms / "ps2").mkdir()
        self.drop("whatever.iso", ps2)
        self.assertEqual(self.go(), 0, self.out)
        self.assertEqual(self.library(), ["ps2/Old Pad (USA).chd"])
        self.assertNotIn("detected:", self.out)

    def test_a_psp_iso_in_a_dat_keeps_its_normal_route(self) -> None:
        from test_inbox import HUB_ISO_NAME, ISO

        self.drop(HUB_ISO_NAME, ISO)
        self.assertEqual(self.go(), 0, self.out)
        self.assertEqual(self.library(), [f"psp/{HUB_ISO_NAME}"])
        self.assertNotIn("detected:", self.out)

    def test_an_unknown_iso_is_not_claimed_and_stays_a_dat_miss(self) -> None:
        self.drop("Mystery PS2 Game.iso", b"ps2 dvd image " * 5000)
        self.assertEqual(self.go(), 1, self.out)
        self.assertIn("no DAT match", self.reason("Mystery PS2 Game.iso"))
        self.assertEqual(self.library(), [])


class ClaimingTests(FlowBase):
    def claims(self, item: Path) -> bool:
        from warpzone.inbox import Context, DatLibrary

        ctx = Context(self.options(), None, DatLibrary(self.dats, None), self.tmp)  # type: ignore[arg-type]
        return ci.HANDLER.claims(ctx, item)

    def test_only_the_named_formats_are_claimed(self) -> None:
        self.assertTrue(self.claims(self.drop("a.dec.iso", b"x")))
        self.assertTrue(self.claims(self.drop("b.xiso.iso", b"x")))
        self.assertTrue(self.claims(self.drop("c.xiso", b"x")))
        self.assertFalse(self.claims(self.drop("d.iso", b"x")))
        self.assertFalse(self.claims(self.drop("e.zip", b"x")))
        self.assertTrue(self.claims(self.drop("dir", {"sub/f.dec.iso": b"x"})))
        self.assertFalse(self.claims(self.drop("dir2", {"sub/f.iso": b"x"})))
        self.assertTrue(self.claims(self.zip_of("f.zip", {"x/G.xiso.iso": b"x"})))
        self.assertFalse(self.claims(self.zip_of("g.zip", {"x/G.iso": b"x"})))


class ArchiveTests(FlowBase):
    def test_a_zip_with_a_ps3_image(self) -> None:
        data = ps3_iso("BLUS30000", "Zipped")
        self.zip_of("pack.zip", {f"folder/{PS3_NAME}": data, "readme.txt": b"hi"})
        self.assertEqual(self.go(), 0, self.out)
        self.assertEqual((self.roms / "ps3" / PS3_NAME).read_bytes(), data)
        self.assertEqual(self.emulators(), {PS3_NAME: ci.EMU_RPCS3_ISO})
        self.assertEqual(self.done(), ["pack.zip"])

    def test_a_plain_iso_inside_a_zip_goes_through_the_dat_hook(self) -> None:
        source = write_xdvdfs(self.tmp / "x.iso", XGD3, "default.xex")
        self.zip_of("pack.zip", {X360_NAME: source.read_bytes()})
        self.assertEqual(self.go(), 0, self.out)
        self.assertEqual(self.library(), [f"xbox360/roms/{X360_NAME}"])

    def test_a_seven_zip_is_listed_and_unpacked_with_bsdtar(self) -> None:
        data = ps3_iso("BLES09999", "Packed")
        payload = self.tmp / "payload"
        payload.write_bytes(data)
        source = (
            "import shutil, sys\n"
            "a = sys.argv[1:]\n"
            f"if a[0] == '-tf':\n    print({PS3_NAME!r}); sys.exit(0)\n"
            "dest = a[a.index('-C') + 1]\n"
            f"shutil.copy({str(payload)!r}, dest + '/' + {PS3_NAME!r})\n"
        )
        bsdtar = make_stub(self.tmp / "bin", "bsdtar", source)
        self.drop("pack.7z", b"7z")
        tools = Tools(chdman=self.chdman, bsdtar=bsdtar)
        self.assertEqual(self.go(tools=tools), 0, self.out)
        self.assertEqual((self.roms / "ps3" / PS3_NAME).read_bytes(), data)

    def test_a_seven_zip_without_bsdtar_is_not_claimed_and_says_so(self) -> None:
        self.drop("pack.7z", b"7z")
        self.assertEqual(self.go(tools=Tools()), 1)
        self.assertIn("needs bsdtar", self.reason("pack.7z"))


if __name__ == "__main__":
    unittest.main()
