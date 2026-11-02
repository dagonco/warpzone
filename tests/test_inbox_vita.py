import os
import struct
import unittest
from pathlib import Path

from console_iso_support import sfo
from inbox_support import age
from test_inbox import Base
from warpzone import inbox, inbox_vita
from warpzone.config import InboxSettings
from warpzone.inbox import Tools
from warpzone.provision import Reporter

TITLE_ID = "PCSF00024"
CONTENT_ID = f"EP9000-{TITLE_ID}_00-PDEUAP1648614960"
APP = f"psvita/app/{TITLE_ID}"
LICENSE = f"psvita/license/{TITLE_ID}/{CONTENT_ID}.rif"


def fself(encrypted: bool = False, self_type: int = 8, size: int = 0x400) -> bytes:
    """A synthetic Vita SELF header: what Vita3K reads (`SCE\\0` magic, app info, the first
    segment's encryption flag) and nothing else."""
    data = bytearray(size)
    data[0:4] = b"SCE\x00"
    struct.pack_into("<I", data, 4, 3)  # version
    struct.pack_into("<Q", data, 0x38, 0x80)  # app info offset
    struct.pack_into("<Q", data, 0x58, 0x100)  # segment info offset
    struct.pack_into("<I", data, 0x80 + 12, self_type)
    struct.pack_into("<Q", data, 0x100 + 0x18, 1 if encrypted else 2)  # 2 = plain
    return bytes(data)


def rif(content_id: str = CONTENT_ID, size: int = 512, key: bytes = b"\x11" * 16) -> bytes:
    """A synthetic 512-byte licence: the content id at 0x10 and a key at 0x50."""
    data = bytearray(size)
    data[0x10 : 0x10 + len(content_id)] = content_id.encode()
    data[0x50:0x60] = key
    return bytes(data)


EBOOT = fself()
ENCRYPTED = fself(encrypted=True)
RIF = rif()


def vita_sfo(title_id: str = TITLE_ID, title: str = "GRAVITY RUSH™", category: str = "gd"):
    return sfo(
        {
            "APP_VER": "01.00",
            "CATEGORY": category,
            "CONTENT_ID": f"EP9000-{title_id}_00-PDEUAP1648614960",
            "TITLE": title,
            "TITLE_ID": title_id,
        }
    )


def game(prefix: str = "", eboot: bytes = EBOOT, **kw) -> dict[str, bytes]:
    """The files of a decrypted Vita dump, under an optional top folder."""
    return {
        f"{prefix}eboot.bin": eboot,
        f"{prefix}sce_sys/param.sfo": vita_sfo(**kw),
        f"{prefix}sce_sys/livearea/contents/template.xml": b"<x/>",
        f"{prefix}data/level.dat": b"DATA" * 50,
    }


class PureTests(unittest.TestCase):
    def test_a_vita_title_id(self) -> None:
        for good in ("PCSF00024", "PCSA00001", "PCSI99999", "PCSE00120"):
            self.assertTrue(inbox_vita.is_title_id(good), good)
        for bad in (
            "PCSJ00001",
            "PCSF0002",
            "PCSF000245",
            "pcsf00024",
            "CUSA07934",
            "BLES00000",
            "",
        ):
            self.assertFalse(inbox_vita.is_title_id(bad), bad)

    def test_the_sfo_category_tells_the_kind(self) -> None:
        for category, expected in (
            ("gd", "game"),
            ("gdc", "game"),
            ("gp", "update"),
            ("ac", "dlc"),
            ("gdo", "other"),
            ("", "other"),
            (3, "other"),
        ):
            self.assertEqual(inbox_vita.category_kind(category), expected, category)

    def test_the_game_folder_is_the_root_or_the_one_top_folder(self) -> None:
        both = ["eboot.bin", "sce_sys/param.sfo", "data/a.dat"]
        self.assertEqual(inbox_vita.game_roots(both), [""])
        self.assertEqual(inbox_vita.game_roots([f"Top/{n}" for n in both]), ["Top/"])
        self.assertEqual(inbox_vita.game_roots(["./" + n for n in both]), [""])
        self.assertEqual(inbox_vita.game_roots(["a/b/" + n for n in both]), [])
        self.assertEqual(inbox_vita.game_roots(["sce_sys/param.sfo"]), [])
        self.assertEqual(inbox_vita.game_roots(["eboot.bin"]), [])
        self.assertEqual(
            inbox_vita.game_roots([f"{p}/{n}" for p in ("A", "B") for n in both]), ["A/", "B/"]
        )

    def test_a_ps3_or_ps4_layout_is_not_a_vita_game(self) -> None:
        self.assertEqual(
            inbox_vita.game_roots(["PS3_GAME/PARAM.SFO", "PS3_GAME/USRDIR/EBOOT.BIN"]), []
        )
        self.assertEqual(inbox_vita.game_roots(["PSP_GAME/PARAM.SFO", "eboot.bin"]), [])

    def test_a_vitamin_dump_carries_the_steroid_module(self) -> None:
        base = ["eboot.bin", "sce_sys/param.sfo"]
        module = "sce_module/steroid.suprx"
        self.assertTrue(inbox_vita.is_vitamin([*base, module], ""))
        self.assertTrue(inbox_vita.is_vitamin([f"Top/{n}" for n in [*base, module]], "Top/"))
        self.assertTrue(inbox_vita.is_vitamin(["./" + n for n in [*base, module]], ""))
        self.assertTrue(inbox_vita.is_vitamin([*base, "sce_module/STEROID.SUPRX"], ""))
        self.assertFalse(inbox_vita.is_vitamin(base, ""))
        self.assertFalse(inbox_vita.is_vitamin([*base, "sce_module/other.suprx"], ""))
        self.assertFalse(inbox_vita.is_vitamin([*base, "data/sce_module/steroid.suprx"], ""))
        # the module of another game in the same item does not count for this root
        self.assertFalse(inbox_vita.is_vitamin([*base, "B/" + module], ""))
        self.assertFalse(inbox_vita.is_vitamin([f"A/{n}" for n in base] + ["B/" + module], "A/"))

    def test_a_licence_is_512_bytes_with_its_content_id(self) -> None:
        lic = inbox_vita.parse_license(RIF)
        self.assertEqual((lic.content_id, lic.title_id), (CONTENT_ID, TITLE_ID))
        for bad, word in (
            (rif(size=511), "512"),
            (rif(size=513), "512"),
            (b"", "512"),
            (rif(content_id=""), "content id"),
            (rif(content_id="not-a-content-id"), "content id"),
            (rif(content_id="EP9000-CUSA07934_00-PDEUAP1648614960"), "content id"),
            (rif(content_id="../../EP9000-PCSF00024_00-PDEUAP1648614"), "content id"),
        ):
            with self.assertRaisesRegex(ValueError, word):
                inbox_vita.parse_license(bad)

    def test_the_content_id_stops_at_the_first_nul(self) -> None:
        data = bytearray(RIF)
        data[0x10 + len(CONTENT_ID) + 1 : 0x10 + len(CONTENT_ID) + 4] = b"abc"
        self.assertEqual(inbox_vita.parse_license(bytes(data)).content_id, CONTENT_ID)

    def test_an_encrypted_app_self_needs_a_licence(self) -> None:
        # Vita3K: decrypt_fself fails with "No klic provided for encrypted App" when the first
        # segment is not plain (encryption != 2) and the app info says self_type APP (8).
        self.assertTrue(inbox_vita.eboot_needs_license(fself(encrypted=True)))
        self.assertFalse(inbox_vita.eboot_needs_license(fself(encrypted=False)))
        self.assertFalse(inbox_vita.eboot_needs_license(fself(encrypted=True, self_type=0x0D)))

    def test_only_the_sce_magic_makes_it_a_self(self) -> None:
        self.assertFalse(inbox_vita.eboot_needs_license(b"\x7fELF" + bytes(0x400)))
        self.assertFalse(inbox_vita.eboot_needs_license(b""))

    def test_a_self_header_that_cannot_be_read_is_an_error(self) -> None:
        # the magic alone does not tell: the header has to be there and make sense
        for bad in (b"SCE\x00" + b"EBOOT" * 100, b"SCE\x00", fself()[:0x7F], fself()[:0x108]):
            with self.assertRaisesRegex(ValueError, "SELF"):
                inbox_vita.eboot_needs_license(bad)
        far = bytearray(fself(encrypted=True))
        struct.pack_into("<Q", far, 0x58, 0xFFFFFF00)
        with self.assertRaisesRegex(ValueError, "SELF"):
            inbox_vita.eboot_needs_license(bytes(far))

    def test_where_a_licence_may_come_from(self) -> None:
        rel = f"license/{TITLE_ID}/{CONTENT_ID}.rif"
        names = [
            "eboot.bin",
            "sce_sys/param.sfo",
            "sce_sys/package/work.bin",
            f"{CONTENT_ID}.rif",
            rel,
            f"license/PCSB00001/{CONTENT_ID}.rif",  # another game's
            "data/x.rif",  # buried in the data
            "license/notes.txt",
        ]
        self.assertEqual(
            inbox_vita.license_sources(names, "", TITLE_ID),
            [f"{CONTENT_ID}.rif", rel, "sce_sys/package/work.bin"],
        )
        top = [f"Top/{n}" for n in names[:3]] + [f"Top/{CONTENT_ID}.rif"]
        top += [f"{CONTENT_ID}.rif", f"license/{TITLE_ID}/{CONTENT_ID}.rif", "OTHER.RIF"]
        self.assertEqual(
            inbox_vita.license_sources(top, "Top/", TITLE_ID),
            [
                f"{CONTENT_ID}.rif",
                "OTHER.RIF",
                f"Top/{CONTENT_ID}.rif",
                "Top/sce_sys/package/work.bin",
                f"license/{TITLE_ID}/{CONTENT_ID}.rif",
            ],
        )
        self.assertEqual(inbox_vita.license_sources(["eboot.bin"], "", TITLE_ID), [])

    def test_choosing_the_licence(self) -> None:
        pick = inbox_vita.choose_license
        self.assertIsNone(pick({}, TITLE_ID, CONTENT_ID))
        self.assertEqual(pick({"a.rif": RIF}, TITLE_ID, CONTENT_ID), (CONTENT_ID, RIF))
        # the same licence twice (work.bin and a .rif) is one licence
        self.assertEqual(
            pick({"a.rif": RIF, "work.bin": RIF}, TITLE_ID, CONTENT_ID), (CONTENT_ID, RIF)
        )
        self.assertEqual(pick({"a.rif": RIF}, TITLE_ID, None), (CONTENT_ID, RIF))
        other = rif("EP9000-PCSF00024_00-OTHEROTHEROTHER0")
        for found, word in (
            ({"a.rif": rif(size=100)}, "a.rif"),
            ({"a.rif": rif("EP9000-PCSB00001_00-PDEUAP1648614960")}, "PCSB00001"),
            ({"a.rif": RIF, "b.rif": rif(key=b"\x22" * 16)}, "different"),
            ({"a.rif": RIF, "b.rif": other}, "several"),
            ({"a.rif": other}, "PDEUAP1648614960"),
        ):
            with self.assertRaisesRegex(ValueError, word):
                pick(found, TITLE_ID, CONTENT_ID)

    def test_file_names(self) -> None:
        self.assertEqual(inbox_vita.file_name("GRAVITY RUSH™", TITLE_ID), "GRAVITY RUSH.psvita")
        self.assertEqual(inbox_vita.file_name("", TITLE_ID), f"{TITLE_ID}.psvita")
        self.assertEqual(
            inbox_vita.file_name("Tearaway: Unfolded", TITLE_ID), "Tearaway - Unfolded.psvita"
        )


class FlowBase(Base):
    def setUp(self) -> None:
        super().setUp()
        self.app = self.roms / "psvita" / "app"

    def go(self, **kw) -> int:  # type: ignore[no-untyped-def]
        return super().go(
            tools=Tools(chdman=self.chdman, rahasher=None), settings=InboxSettings(), **kw
        )

    def launcher(self, name: str = "GRAVITY RUSH.psvita") -> Path:
        return self.roms / "psvita" / name


class InstallTests(FlowBase):
    def test_a_folder_dump_is_placed_with_its_launcher_file(self) -> None:
        self.drop("GRAVITY RUSH [PCSF00024]", game())
        self.assertEqual(self.go(), 0, self.out)
        self.assertEqual(
            self.library(),
            [
                "psvita/GRAVITY RUSH.psvita",
                f"{APP}/data/level.dat",
                f"{APP}/eboot.bin",
                f"{APP}/sce_sys/livearea/contents/template.xml",
                f"{APP}/sce_sys/param.sfo",
            ],
        )
        self.assertEqual((self.app / TITLE_ID / "eboot.bin").read_bytes(), EBOOT)
        self.assertEqual(self.launcher().read_bytes(), TITLE_ID.encode())  # only the id
        self.assertEqual(self.done(), ["GRAVITY RUSH [PCSF00024]"])
        self.assertEqual(self.review(), [])
        self.assertIn(f"path: library/roms/{APP}", self.out)
        self.assertIn("detected: Vita game: PCSF00024", self.out)
        self.assertIn("launcher: library/roms/psvita/GRAVITY RUSH.psvita", self.out)
        self.assertIn("whitelist: !/roms/psvita/app/PCSF00024", self.out)
        self.assertIn("whitelist: !/roms/psvita/GRAVITY RUSH.psvita", self.out)

    def test_an_archive_with_the_game_at_its_root(self) -> None:
        self.zip_of("GRAVITY RUSH [PCSF00024] (v01.00) .zip", game())
        self.assertEqual(self.go(), 0, self.out)
        self.assertIn(f"{APP}/eboot.bin", self.library())
        self.assertEqual(self.launcher().read_bytes(), TITLE_ID.encode())

    def test_an_archive_with_one_top_folder(self) -> None:
        self.zip_of("pack.zip", game("GRAVITY RUSH/"))
        self.assertEqual(self.go(), 0, self.out)
        self.assertIn(f"{APP}/eboot.bin", self.library())  # the top folder is not part of it
        self.assertEqual([x for x in self.library() if "GRAVITY RUSH/" in x], [])
        self.assertEqual(self.launcher().read_bytes(), TITLE_ID.encode())

    def test_a_folder_with_one_top_folder(self) -> None:
        self.drop("pack", game("Inner/"))
        self.assertEqual(self.go(), 0, self.out)
        self.assertIn(f"{APP}/eboot.bin", self.library())

    def test_a_gdc_game_is_a_game(self) -> None:
        self.drop("cart", game(category="gdc"))
        self.assertEqual(self.go(), 0, self.out)
        self.assertIn(f"{APP}/eboot.bin", self.library())

    def test_report_only_writes_nothing_and_prints_both_lines(self) -> None:
        self.drop("GRAVITY RUSH [PCSF00024]", game())
        self.assertEqual(self.go(apply=False), 0, self.out)
        self.assertEqual(self.library(), [])
        self.assertEqual(self.done(), [])
        self.assertIn("would place", self.out)
        self.assertIn("whitelist: !/roms/psvita/app/PCSF00024", self.out)
        self.assertIn("whitelist: !/roms/psvita/GRAVITY RUSH.psvita", self.out)

    def test_nothing_partial_is_left_behind(self) -> None:
        self.drop("g", game())
        self.assertEqual(self.go(), 0, self.out)
        self.assertEqual([p.name for p in self.roms.rglob("*.part")], [])
        self.assertEqual(list((self.tmp / "work").iterdir()), [])

    def test_a_game_in_the_library_is_reported_and_not_touched(self) -> None:
        folder = self.app / TITLE_ID
        (folder / "sce_sys").mkdir(parents=True)
        (folder / "eboot.bin").write_bytes(b"MINE")
        (folder / "sce_sys" / "param.sfo").write_bytes(vita_sfo())
        self.drop("again", game())
        self.assertEqual(self.go(), 0, self.out)
        self.assertEqual((folder / "eboot.bin").read_bytes(), b"MINE")
        self.assertIn("already in the library", self.out)
        self.assertEqual(self.done(), ["again"])
        self.assertEqual(self.launcher().read_bytes(), TITLE_ID.encode())  # made if missing
        self.assertIn("whitelist: !/roms/psvita/app/PCSF00024", self.out)
        self.assertIn("whitelist: !/roms/psvita/GRAVITY RUSH.psvita", self.out)

    def test_a_launcher_file_that_is_there_is_reported_and_not_overwritten(self) -> None:
        self.launcher().parent.mkdir(parents=True)
        self.launcher().write_bytes(TITLE_ID.encode())
        before = self.launcher().stat().st_mtime_ns
        self.drop("g", game())
        self.assertEqual(self.go(), 0, self.out)
        self.assertEqual(self.launcher().stat().st_mtime_ns, before)
        self.assertIn("(already there)", self.out)
        self.assertIn("whitelist: !/roms/psvita/GRAVITY RUSH.psvita", self.out)

    def test_the_same_title_for_another_game_gets_the_id_in_its_name(self) -> None:
        self.launcher().parent.mkdir(parents=True)
        self.launcher().write_bytes(b"PCSB00001")
        self.drop("g", game())
        self.assertEqual(self.go(), 0, self.out)
        self.assertEqual(self.launcher().read_bytes(), b"PCSB00001")
        other = self.launcher("GRAVITY RUSH [PCSF00024].psvita")
        self.assertEqual(other.read_bytes(), TITLE_ID.encode())
        self.assertIn("whitelist: !/roms/psvita/GRAVITY RUSH \\[PCSF00024\\].psvita", self.out)

    def test_a_rerun_changes_nothing(self) -> None:
        self.drop("g", game())
        self.assertEqual(self.go(), 0, self.out)
        first = self.library()
        self.drop("g2", game())
        self.assertEqual(self.go(), 0, self.out)
        self.assertEqual(self.library(), first)


class ReviewTests(FlowBase):
    def assert_review(self, item: str, *words: str) -> None:
        self.assertEqual(self.review(), [item, f"{item}.reason.txt"])
        text = self.reason(item)
        for word in words:
            self.assertIn(word, text)
        self.assertEqual(self.library(), [])

    def test_an_update_goes_to_review(self) -> None:
        self.drop("patch", game(category="gp"))
        self.assertEqual(self.go(), 1, self.out)
        self.assert_review("patch", "update", TITLE_ID)

    def test_dlc_goes_to_review(self) -> None:
        self.drop("dlc", game(category="ac"))
        self.assertEqual(self.go(), 1, self.out)
        self.assert_review("dlc", "DLC", TITLE_ID)

    def test_another_category_goes_to_review(self) -> None:
        self.drop("odd", game(category="zz"))
        self.assertEqual(self.go(), 1, self.out)
        self.assert_review("odd", "not a game")

    def test_a_title_id_that_is_not_a_vita_one_goes_to_review(self) -> None:
        self.drop("ps4", game(title_id="CUSA07934"))
        self.assertEqual(self.go(), 1, self.out)
        self.assert_review("ps4", "CUSA07934", "Vita title id")

    def test_a_work_bin_that_is_not_a_licence_goes_to_review(self) -> None:
        files = {**game(), "sce_sys/package/work.bin": b"x" * 16}
        self.drop("enc", files)
        self.assertEqual(self.go(), 1, self.out)
        self.assert_review("enc", "work.bin", "512")

    def test_a_dump_with_sce_pfs_goes_to_review(self) -> None:
        self.drop("pfs", {**game(), "sce_pfs/files.db": b"x"})
        self.assertEqual(self.go(), 1, self.out)
        self.assert_review("pfs", "sce_pfs")

    def test_a_vitamin_dump_goes_to_review_in_a_folder(self) -> None:
        self.drop("vitamin", {**game(), "sce_module/steroid.suprx": b"x" * 16})
        self.assertEqual(self.go(), 1, self.out)
        self.assert_review("vitamin", "Vitamin", "steroid.suprx", "Vita3K", "work.bin", "NoNpDrm")

    def test_a_vitamin_dump_goes_to_review_in_an_archive(self) -> None:
        self.zip_of("vitamin.zip", {**game("Top/"), "Top/sce_module/steroid.suprx": b"x" * 16})
        self.assertEqual(self.go(), 1, self.out)
        self.assert_review("vitamin.zip", "Vitamin", "steroid.suprx")

    def test_a_vitamin_dump_is_never_placed_in_report_mode_either(self) -> None:
        self.drop("vitamin", {**game(), "sce_module/steroid.suprx": b"x" * 16})
        self.assertEqual(self.go(apply=False), 1, self.out)
        self.assertEqual(self.library(), [])
        self.assertNotIn("would place", self.out)

    def test_a_vitamin_dump_leaves_what_the_library_holds_untouched(self) -> None:
        folder = self.app / TITLE_ID
        (folder / "sce_sys").mkdir(parents=True)
        (folder / "eboot.bin").write_bytes(b"MINE")
        (folder / "sce_sys" / "param.sfo").write_bytes(vita_sfo())
        before = self.library()
        self.drop("vitamin", {**game(), "sce_module/steroid.suprx": b"x" * 16})
        self.assertEqual(self.go(), 1, self.out)
        self.assertEqual(self.library(), before)
        self.assertEqual((folder / "eboot.bin").read_bytes(), b"MINE")
        self.assertFalse(self.launcher().exists())
        self.assertIn("Vitamin", self.reason("vitamin"))

    def test_two_games_in_one_item_go_to_review(self) -> None:
        files = {**game("A/"), **game("B/", title_id="PCSF00025", title="Other")}
        self.drop("both", files)
        self.assertEqual(self.go(), 1, self.out)
        self.assert_review("both", "2 games")

    def test_a_symbolic_link_is_refused(self) -> None:
        path = self.drop("link", game())
        os.symlink("/etc/hosts", path / "data" / "hosts")
        age(path)
        self.assertEqual(self.go(), 1, self.out)
        self.assert_review("link", "symbolic link")

    def test_a_dump_without_a_readable_sfo_goes_to_review(self) -> None:
        self.drop("bad", {**game(), "sce_sys/param.sfo": b"junk"})
        self.assertEqual(self.go(), 1, self.out)
        self.assert_review("bad", "param.sfo")

    def test_a_ps3_folder_is_not_taken_for_a_vita_game(self) -> None:
        self.drop(
            "ps3",
            {"PS3_GAME/PARAM.SFO": vita_sfo("BLES00000"), "PS3_GAME/USRDIR/EBOOT.BIN": b"x"},
        )
        self.assertEqual(self.go(), 1, self.out)
        self.assertIn("PS3", self.reason("ps3"))
        self.assertNotIn("Vita", self.reason("ps3").replace("Vita3K", ""))

    def test_a_vpk_keeps_going_to_review(self) -> None:
        self.drop("game.vpk", b"PK\x03\x04vpk")
        self.assertEqual(self.go(), 1, self.out)
        self.assertIn("Vita", self.reason("game.vpk"))
        self.assertEqual(self.library(), [])
        self.assertIn("not supported by inbox yet", self.reason("game.vpk"))


class LicenseTests(FlowBase):
    """A dump whose eboot.bin is still encrypted runs only with its licence in ux0/license."""

    def lic(self, name: str = f"{CONTENT_ID}.rif") -> Path:
        return self.roms / "psvita" / "license" / TITLE_ID / name

    def assert_review(self, item: str, *words: str) -> None:
        self.assertEqual(self.review(), [item, f"{item}.reason.txt"])
        for word in words:
            self.assertIn(word, self.reason(item))
        self.assertEqual(self.library(), [])

    def test_a_licence_in_work_bin_is_filed_under_its_content_id(self) -> None:
        self.drop("g", {**game(eboot=ENCRYPTED), "sce_sys/package/work.bin": RIF})
        self.assertEqual(self.go(), 0, self.out)
        self.assertEqual(self.lic().read_bytes(), RIF)
        self.assertIn(f"{APP}/eboot.bin", self.library())
        self.assertIn(f"{APP}/sce_sys/package/work.bin", self.library())  # the dump is as it is
        self.assertIn(LICENSE, self.library())
        self.assertIn(f"licence: library/roms/{LICENSE}", self.out)
        self.assertIn("whitelist: !/roms/psvita/app/PCSF00024", self.out)
        self.assertIn("whitelist: !/roms/psvita/license/PCSF00024", self.out)
        self.assertIn("whitelist: !/roms/psvita/GRAVITY RUSH.psvita", self.out)
        self.assertEqual([p.name for p in self.roms.rglob("*.part")], [])

    def test_a_rif_in_the_license_folder_of_the_item(self) -> None:
        files = {**game(eboot=ENCRYPTED), f"license/{TITLE_ID}/{CONTENT_ID}.rif": RIF}
        self.drop("g", files)
        self.assertEqual(self.go(), 0, self.out)
        self.assertEqual(self.lic().read_bytes(), RIF)

    def test_a_rif_next_to_the_game_folder(self) -> None:
        self.drop("g", {**game("Top/", eboot=ENCRYPTED), "Any name.rif": RIF})
        self.assertEqual(self.go(), 0, self.out)
        self.assertEqual(self.lic().read_bytes(), RIF)  # filed by its content id, not its name

    def test_a_rif_in_an_archive(self) -> None:
        self.zip_of("g.zip", {**game("Top/", eboot=ENCRYPTED), f"{CONTENT_ID}.rif": RIF})
        self.assertEqual(self.go(), 0, self.out)
        self.assertEqual(self.lic().read_bytes(), RIF)

    def test_a_game_that_needs_a_licence_and_has_none_goes_to_review(self) -> None:
        self.drop("g", game(eboot=ENCRYPTED))
        self.assertEqual(self.go(), 1, self.out)
        self.assert_review("g", "licence", "klic", "work.bin", ".rif")

    def test_that_review_is_also_the_report(self) -> None:
        self.drop("g", game(eboot=ENCRYPTED))
        self.assertEqual(self.go(apply=False), 1, self.out)
        self.assertNotIn("would place", self.out)
        self.assertEqual(self.library(), [])

    def test_a_game_that_does_not_need_one_places_without(self) -> None:
        self.drop("g", game())
        self.assertEqual(self.go(), 0, self.out)
        self.assertNotIn("licence", self.out)
        self.assertNotIn("psvita/license", self.out)
        self.assertFalse((self.roms / "psvita" / "license").exists())

    def test_a_licence_that_comes_along_is_kept_even_if_not_needed(self) -> None:
        self.drop("g", {**game(), f"{CONTENT_ID}.rif": RIF})
        self.assertEqual(self.go(), 0, self.out)
        self.assertEqual(self.lic().read_bytes(), RIF)

    def test_a_licence_of_another_game_goes_to_review(self) -> None:
        other = rif("EP9000-PCSB00360_00-PDEUAP1648614960")
        self.drop("g", {**game(eboot=ENCRYPTED), f"{CONTENT_ID}.rif": other})
        self.assertEqual(self.go(), 1, self.out)
        self.assert_review("g", "PCSB00360", TITLE_ID)

    def test_a_licence_for_another_build_goes_to_review(self) -> None:
        other = rif("EP9000-PCSF00024_00-OTHEROTHEROTHER0")
        self.drop("g", {**game(eboot=ENCRYPTED), "x.rif": other})
        self.assertEqual(self.go(), 1, self.out)
        self.assert_review("g", "OTHEROTHEROTHER0", CONTENT_ID)

    def test_a_licence_of_the_wrong_size_goes_to_review(self) -> None:
        self.drop("g", {**game(eboot=ENCRYPTED), "x.rif": rif(size=256)})
        self.assertEqual(self.go(), 1, self.out)
        self.assert_review("g", "x.rif", "512")

    def test_an_eboot_header_that_cannot_be_read_goes_to_review(self) -> None:
        self.drop("g", game(eboot=b"SCE\x00" + b"EBOOT" * 100))
        self.assertEqual(self.go(), 1, self.out)
        self.assert_review("g", "eboot.bin", "SELF")

    def test_a_licence_already_in_the_library_is_kept(self) -> None:
        self.lic().parent.mkdir(parents=True)
        self.lic().write_bytes(RIF)
        before = self.lic().stat().st_mtime_ns
        self.drop("g", {**game(eboot=ENCRYPTED), "x.rif": RIF})
        self.assertEqual(self.go(), 0, self.out)
        self.assertEqual(self.lic().stat().st_mtime_ns, before)
        self.assertIn("(already there)", self.out)
        self.assertIn("whitelist: !/roms/psvita/license/PCSF00024", self.out)

    def test_a_different_licence_in_the_library_is_never_overwritten(self) -> None:
        mine = rif(key=b"\x99" * 16)
        self.lic().parent.mkdir(parents=True)
        self.lic().write_bytes(mine)
        self.drop("g", {**game(eboot=ENCRYPTED), "x.rif": RIF})
        self.assertEqual(self.go(), 1, self.out)
        self.assertEqual(self.lic().read_bytes(), mine)
        self.assertEqual(self.review(), ["g", "g.reason.txt"])
        self.assertIn("different", self.reason("g"))
        self.assertFalse((self.app / TITLE_ID).exists())  # nothing placed before the check
        self.assertFalse(self.launcher().exists())

    def test_a_game_in_the_library_gets_its_missing_licence(self) -> None:
        folder = self.app / TITLE_ID
        (folder / "sce_sys").mkdir(parents=True)
        (folder / "eboot.bin").write_bytes(ENCRYPTED)
        (folder / "sce_sys" / "param.sfo").write_bytes(vita_sfo())
        self.drop("g", {**game(eboot=ENCRYPTED), "x.rif": RIF})
        self.assertEqual(self.go(), 0, self.out)
        self.assertEqual((folder / "eboot.bin").read_bytes(), ENCRYPTED)
        self.assertEqual(self.lic().read_bytes(), RIF)

    def test_report_only_prints_the_licence_line_and_writes_nothing(self) -> None:
        self.drop("g", {**game(eboot=ENCRYPTED), "x.rif": RIF})
        self.assertEqual(self.go(apply=False), 0, self.out)
        self.assertEqual(self.library(), [])
        self.assertIn(f"licence: library/roms/{LICENSE} (would be written)", self.out)
        self.assertIn("whitelist: !/roms/psvita/license/PCSF00024", self.out)

    def test_a_rerun_changes_nothing(self) -> None:
        self.drop("g", {**game(eboot=ENCRYPTED), "x.rif": RIF})
        self.assertEqual(self.go(), 0, self.out)
        first = self.library()
        self.drop("g2", {**game(eboot=ENCRYPTED), "x.rif": RIF})
        self.assertEqual(self.go(), 0, self.out)
        self.assertEqual(self.library(), first)

    def test_sce_pfs_still_goes_to_review_even_with_a_licence(self) -> None:
        self.drop("g", {**game(eboot=ENCRYPTED), "sce_pfs/files.db": b"x", "x.rif": RIF})
        self.assertEqual(self.go(), 1, self.out)
        self.assert_review("g", "sce_pfs")


class ClaimTests(FlowBase):
    def claims(self, item: Path) -> bool:
        ctx = inbox.Context(self.options(), Reporter(self.lines.append), None, self.tmp)  # type: ignore[arg-type]
        return inbox_vita.HANDLER.claims(ctx, item)

    def test_only_a_game_layout_is_claimed(self) -> None:
        yes = self.drop("yes", game())
        no = self.drop("no", {"readme.txt": b"hi", "sce_sys/param.sfo": vita_sfo()})
        self.assertTrue(self.claims(yes))
        self.assertFalse(self.claims(no))
        self.assertFalse(self.claims(self.zip_of("n.zip", {"a.nes": b"x"})))
        self.assertTrue(self.claims(self.zip_of("y.zip", game("Top/"))))


if __name__ == "__main__":
    unittest.main()
