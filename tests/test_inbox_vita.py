import os
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
APP = f"psvita/app/{TITLE_ID}"
EBOOT = b"SCE\x00" + b"EBOOT" * 100


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


def game(prefix: str = "", **kw) -> dict[str, bytes]:
    """The files of a decrypted Vita dump, under an optional top folder."""
    return {
        f"{prefix}eboot.bin": EBOOT,
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

    def test_an_encrypted_dump_goes_to_review(self) -> None:
        files = {**game(), "sce_sys/package/work.bin": b"x" * 16}
        self.drop("enc", files)
        self.assertEqual(self.go(), 1, self.out)
        self.assert_review("enc", "work.bin", "install")

    def test_a_dump_with_sce_pfs_goes_to_review(self) -> None:
        self.drop("pfs", {**game(), "sce_pfs/files.db": b"x"})
        self.assertEqual(self.go(), 1, self.out)
        self.assert_review("pfs", "sce_pfs")

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
