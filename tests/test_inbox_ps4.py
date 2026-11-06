import io
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from console_iso_support import ORBIS_PKG_UTIL, ps4_pkg, ps4_sfo
from inbox_support import make_stub
from test_inbox import Base
from warpzone import inbox, inbox_core, inbox_ps4
from warpzone import inbox_console_iso as ci
from warpzone.config import InboxSettings
from warpzone.inbox_core import Tools

PKG_NAME = "Test Game EU-CUSA07934.pkg"
TITLE_ID = "CUSA07934"
GAME = f"ps4/games/{TITLE_ID}"
UPDATE = f"{TITLE_ID}-UPDATE"
DLC_CONTENT_ID = f"UP9000-{TITLE_ID}_00-SPEXPANSIONDLC03"
LABEL = "SPEXPANSIONDLC03"
EXEC = f'Exec=shadps4 "/home/deck/Emulation/roms/ps4/games/{TITLE_ID}/eboot.bin"'


class PureTests(unittest.TestCase):
    def test_a_header_tells_a_game_from_an_update_and_dlc(self) -> None:
        def kind(**kw: int) -> str | None:
            return inbox_ps4.pkg_kind(ps4_pkg(**kw)[: ci.PS4_HEADER_BYTES])

        self.assertEqual(kind(), "game")
        self.assertEqual(kind(content_type=0x1B), "dlc")
        self.assertEqual(kind(content_type=0x1C), "dlc")  # additional license, a DLC without data
        self.assertEqual(kind(content_type=0x1A, flags=0x0A000000 | 0x40000000), "update")
        self.assertEqual(kind(content_type=0x1A, flags=0x0A000000 | 0x100000), "update")
        self.assertEqual(kind(content_type=0x1A, flags=0x0A000000 | 0x60000000), "update")
        self.assertEqual(kind(content_type=0x99), "other")
        self.assertIsNone(inbox_ps4.pkg_kind(ps4_pkg()[:0x70]))
        self.assertIsNone(inbox_ps4.pkg_kind(b""))

    def test_the_header_gives_the_content_id_and_the_pfs_size(self) -> None:
        head = ps4_pkg(DLC_CONTENT_ID, content_type=0x1C, pfs_size=0)
        header = inbox_ps4.parse_header(head)
        assert header is not None
        self.assertEqual(
            (header.kind, header.content_id, header.pfs_size), ("dlc", DLC_CONTENT_ID, 0)
        )
        header = inbox_ps4.parse_header(ps4_pkg(content_type=0x1B, pfs_size=170852352))
        assert header is not None
        self.assertEqual(header.pfs_size, 170852352)
        # a header that stops before 0x420 has no PFS size
        header = inbox_ps4.parse_header(ps4_pkg()[: ci.PS4_HEADER_BYTES])
        assert header is not None
        self.assertEqual((header.kind, header.pfs_size), ("game", None))
        self.assertIsNone(inbox_ps4.parse_header(b""))

    def test_the_entitlement_label_is_the_part_after_the_second_dash(self) -> None:
        for content_id, expected in (
            (DLC_CONTENT_ID, "SPEXPANSIONDLC03"),
            ("EP9000-CUSA07934_00-A_b1", "A_b1"),
            ("EP9000-CUSA07934_00-", None),
            ("EP9000-CUSA07934_00", None),
            ("EP9000-CUSA07934_00-AB.C", None),
            ("EP9000-CUSA07934_00-../x", None),
            ("EP9000-CUSA07934_00-A B", None),
            ("", None),
        ):
            self.assertEqual(inbox_ps4.entitlement_label(content_id), expected, content_id)

    def test_app_versions_compare_numerically_by_dot_part(self) -> None:
        key = inbox_ps4.app_version_key
        self.assertEqual(key("01.09"), (1, 9))
        self.assertLess(key("01.09"), key("01.10"))  # type: ignore[operator]
        self.assertLess(key("1.9"), key("2.0"))  # type: ignore[operator]
        for bad in ("", "x", "1.x", "1..2", ".1", "1.", "-1", None, 7):
            self.assertIsNone(key(bad), repr(bad))

    def test_the_update_action(self) -> None:
        action = inbox_ps4.update_action
        self.assertEqual(action("01.09", None), "place")
        self.assertEqual(action("01.09", "01.09"), "already")
        self.assertEqual(action("1.9", "01.09"), "already")
        self.assertEqual(action("1.9", "01.09.0"), "already")
        self.assertEqual(action("01.10", "01.09"), "newer")
        self.assertEqual(action("02.00", "01.99"), "newer")
        self.assertEqual(action("01.08", "01.09"), "older")
        self.assertEqual(action("01.09", "abc"), "bad_old")
        self.assertEqual(action("01.09", ""), "bad_old")
        self.assertEqual(action(None, "01.09"), "bad_new")
        self.assertEqual(action("abc", None), "bad_new")

    def test_only_a_folder_named_exactly_like_a_title_id_names_a_game(self) -> None:
        item = inbox_ps4.item_title_id
        self.assertEqual(item("CUSA03173", True), "CUSA03173")
        self.assertIsNone(item("CUSA03173", False))
        for name in ("CUSA0317", "CUSA031730", "CUSA03173 DLC", "cusa03173", "PCSE00001", ""):
            self.assertIsNone(item(name, True), name)

    def test_the_sfo_category_tells_the_kind(self) -> None:
        for category, expected in (
            ("gd", "game"),
            ("gdo", "game"),
            ("gp", "update"),
            ("ac", "dlc"),
            ("", "other"),
            ("xx", "other"),
        ):
            self.assertEqual(inbox_ps4.category_kind(category), expected, category)

    def test_the_header_is_read_from_a_file_object(self) -> None:
        found = ci.detect_pkg(io.BytesIO(ps4_pkg()))
        self.assertEqual(found, ci.Detected(ci.PS4, TITLE_ID))

    def test_shortcut_names(self) -> None:
        cases = {
            "Teenage Mutant Ninja Turtles: Shredder's Revenge": (
                "Teenage Mutant Ninja Turtles - Shredders Revenge"
            ),
            "Jak and Daxter™: the Precursor Legacy": "Jak and Daxter - the Precursor Legacy",
            "A/B\\C*D?E": "ABCDE",
            "  Spaced   out  ": "Spaced out",
            "Dots...": "Dots",
            "R\u00e9sum\u00e9 \u2013 One": "R\u00e9sum\u00e9 - One",
            "": TITLE_ID,
            "///": TITLE_ID,
        }
        for title, expected in cases.items():
            self.assertEqual(inbox_ps4.shortcut_name(title, TITLE_ID), expected, title)
        long = inbox_ps4.shortcut_name("x" * 400, TITLE_ID)
        self.assertEqual(len(long), 100)

    def test_the_shortcut_text_is_what_the_launcher_reads(self) -> None:
        text = inbox_ps4.desktop_text("Test Game", TITLE_ID)
        self.assertEqual(
            text,
            "[Desktop Entry]\nType=Application\nName=Test Game\n" + EXEC + "\n",
        )
        # the EmuDeck launcher takes the one quoted string of the Exec line
        (exec_line,) = [x for x in text.splitlines() if x.startswith("Exec=")]
        self.assertEqual(exec_line.count('"'), 2)
        self.assertNotIn("%", exec_line)

    def test_a_title_with_a_newline_cannot_add_keys(self) -> None:
        text = inbox_ps4.desktop_text("Evil\nExec=rm -rf", TITLE_ID)
        self.assertEqual(sum(1 for x in text.splitlines() if x.startswith("Exec=")), 1)

    def test_the_tool_is_found_in_the_settings_and_on_the_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            exe = make_stub(Path(tmp), "orbis-pkg-util", "")
            found = inbox_core.find_tools(
                InboxSettings(orbis_pkg_util=exe), which=lambda name: f"/usr/bin/{name}"
            )
            self.assertEqual(found.orbis_pkg_util, exe)
            found = inbox_core.find_tools(InboxSettings(), which=lambda name: f"/usr/bin/{name}")
            self.assertEqual(found.orbis_pkg_util, "/usr/bin/orbis-pkg-util")
            with self.assertRaises(inbox_core.InboxError):
                inbox_core.find_tools(
                    InboxSettings(orbis_pkg_util=str(Path(tmp) / "nope")), which=lambda n: None
                )
        self.assertIn("orbis-pkg-util no", inbox._describe_tools(Tools()))
        self.assertIn("orbis-pkg-util yes", inbox._describe_tools(Tools(orbis_pkg_util="/x")))


class FlowBase(Base):
    def setUp(self) -> None:
        super().setUp()
        self.util = make_stub(self.bin, "orbis-pkg-util", ORBIS_PKG_UTIL)
        self.log = self.tmp / "util.log"
        os.environ["STUB_PKG_LOG"] = str(self.log)
        self.sfo(ps4_sfo(TITLE_ID, "Test Game"))
        self.games = self.roms / "ps4" / "games"
        self.shortcuts = self.roms / "ps4" / "shortcuts"

    def sfo(self, data: bytes) -> None:
        os.environ["STUB_PKG_SFO"] = data.hex()

    def go(self, *, tool: bool = True, **kw) -> int:  # type: ignore[no-untyped-def]
        tools = Tools(chdman=self.chdman, orbis_pkg_util=self.util if tool else None, rahasher=None)
        return super().go(tools=tools, settings=InboxSettings(), **kw)

    def shortcut_names(self) -> list[str]:
        return sorted(p.name for p in self.shortcuts.glob("*")) if self.shortcuts.is_dir() else []

    def calls(self) -> list[str]:
        return self.log.read_text(encoding="utf-8").splitlines() if self.log.exists() else []

    def review(self) -> list[str]:
        folder = self.inbox / "_revisar"
        return sorted(p.name for p in folder.iterdir()) if folder.is_dir() else []

    def reason(self, name: str) -> str:
        return (self.inbox / "_revisar" / f"{name}.reason.txt").read_text(encoding="utf-8")


class InstallTests(FlowBase):
    def test_a_game_is_extracted_into_a_folder_with_a_shortcut(self) -> None:
        self.drop(PKG_NAME, ps4_pkg())
        self.assertEqual(self.go(), 0, self.out)
        self.assertEqual(
            self.library(),
            [
                f"{GAME}/Content/data/level.dat",
                f"{GAME}/eboot.bin",
                f"{GAME}/sce_sys/param.sfo",
                "ps4/shortcuts/Test Game.desktop",
            ],
        )
        self.assertEqual((self.games / TITLE_ID / "eboot.bin").read_bytes(), b"EBOOT" * 100)
        shortcut = (self.shortcuts / "Test Game.desktop").read_text(encoding="utf-8")
        self.assertEqual(
            shortcut, "[Desktop Entry]\nType=Application\nName=Test Game\n" + EXEC + "\n"
        )
        self.assertEqual(self.done(), [PKG_NAME])
        self.assertEqual(self.review(), [])
        self.assertIn("detected: PS4 package: CUSA07934", self.out)
        self.assertIn(f"path: library/roms/{GAME}", self.out)
        self.assertIn("shortcut: library/roms/ps4/shortcuts/Test Game.desktop", self.out)
        self.assertIn("whitelist: !/roms/ps4/games/CUSA07934", self.out)
        self.assertIn("whitelist: !/roms/ps4/shortcuts/Test Game.desktop", self.out)
        # the summary lists both lines too
        self.assertIn("  !/roms/ps4/games/CUSA07934\n", self.out + "\n")
        self.assertIn("  !/roms/ps4/shortcuts/Test Game.desktop\n", self.out + "\n")
        (call,) = self.calls()
        self.assertTrue(call.startswith("extract -q -o "), call)
        self.assertTrue(call.endswith(PKG_NAME), call)

    def test_nothing_partial_is_left_in_the_library_or_the_work_folder(self) -> None:
        self.drop(PKG_NAME, ps4_pkg())
        self.assertEqual(self.go(), 0, self.out)
        leftovers = [p.name for p in self.roms.rglob("*") if p.name.endswith(".part")]
        self.assertEqual(leftovers, [])
        self.assertEqual(list((self.tmp / "work").iterdir()), [])

    def test_report_only_extracts_and_writes_nothing(self) -> None:
        self.drop(PKG_NAME, ps4_pkg())
        self.assertEqual(self.go(apply=False), 0, self.out)
        self.assertEqual(self.library(), [])
        self.assertEqual(self.calls(), [])
        self.assertIn("would place", self.out)
        self.assertIn(f"path: library/roms/{GAME}", self.out)
        self.assertIn("whitelist: !/roms/ps4/games/CUSA07934", self.out)
        self.assertIn("named after the title", self.out)
        self.assertEqual(self.done(), [])

    def test_a_game_in_the_library_is_reported_and_not_touched(self) -> None:
        folder = self.games / TITLE_ID
        (folder / "sce_sys").mkdir(parents=True)
        (folder / "eboot.bin").write_bytes(b"MINE")
        (folder / "sce_sys" / "param.sfo").write_bytes(ps4_sfo(TITLE_ID, "Test Game"))
        self.drop(PKG_NAME, ps4_pkg())
        self.assertEqual(self.go(), 0, self.out)
        self.assertEqual((folder / "eboot.bin").read_bytes(), b"MINE")
        self.assertEqual(self.calls(), [])  # no need to unpack it
        self.assertIn("already in the library", self.out)
        self.assertEqual(self.done(), [PKG_NAME])
        # a shortcut that was never written (a run that stopped) is made from the library copy
        self.assertEqual(self.shortcut_names(), ["Test Game.desktop"])
        self.assertIn("whitelist: !/roms/ps4/games/CUSA07934", self.out)

    def test_a_rerun_changes_nothing(self) -> None:
        self.drop(PKG_NAME, ps4_pkg())
        self.assertEqual(self.go(), 0, self.out)
        before = {p: p.read_bytes() for p in self.roms.rglob("*") if p.is_file()}
        self.drop(PKG_NAME, ps4_pkg())
        self.assertEqual(self.go(), 0, self.out)
        self.assertIn("already in the library", self.out)
        self.assertEqual(before, {p: p.read_bytes() for p in self.roms.rglob("*") if p.is_file()})

    def test_a_shortcut_with_the_same_name_for_another_game_is_not_replaced(self) -> None:
        self.shortcuts.mkdir(parents=True)
        other = self.shortcuts / "Test Game.desktop"
        other.write_text("[Desktop Entry]\nType=Application\nName=Other\nExec=other\n")
        self.drop(PKG_NAME, ps4_pkg())
        self.assertEqual(self.go(), 0, self.out)
        self.assertEqual(
            other.read_text(), "[Desktop Entry]\nType=Application\nName=Other\nExec=other\n"
        )
        self.assertEqual(
            self.shortcut_names(), ["Test Game [CUSA07934].desktop", "Test Game.desktop"]
        )
        self.assertIn("whitelist: !/roms/ps4/shortcuts/Test Game \\[CUSA07934\\].desktop", self.out)

    def test_an_edited_shortcut_of_the_same_game_is_kept(self) -> None:
        self.shortcuts.mkdir(parents=True)
        mine = self.shortcuts / "Test Game.desktop"
        text = f"[Desktop Entry]\nType=Application\nName=Mine\n{EXEC}\nTerminal=false\n"
        mine.write_text(text)
        self.drop(PKG_NAME, ps4_pkg())
        self.assertEqual(self.go(), 0, self.out)
        self.assertEqual(mine.read_text(), text)
        self.assertEqual(self.shortcut_names(), ["Test Game.desktop"])

    def test_a_stale_part_folder_from_an_earlier_run_is_replaced(self) -> None:
        stale = self.games / f"{TITLE_ID}.part"
        stale.mkdir(parents=True)
        (stale / "junk").write_bytes(b"x")
        self.drop(PKG_NAME, ps4_pkg())
        self.assertEqual(self.go(), 0, self.out)
        self.assertFalse(stale.exists())
        self.assertTrue((self.games / TITLE_ID / "eboot.bin").is_file())

    def test_a_zip_with_a_package(self) -> None:
        self.zip_of("pack.zip", {"sub/Game-CUSA07934.pkg": ps4_pkg(), "Notes.txt": b"hi"})
        self.assertEqual(self.go(), 0, self.out)
        self.assertTrue((self.games / TITLE_ID / "eboot.bin").is_file())

    def test_the_title_of_the_sfo_names_the_shortcut(self) -> None:
        self.sfo(ps4_sfo(TITLE_ID, "Odd:  Name™ / Edition"))
        self.drop(PKG_NAME, ps4_pkg())
        self.assertEqual(self.go(), 0, self.out)
        self.assertEqual(self.shortcut_names(), ["Odd - Name Edition.desktop"])
        text = (self.shortcuts / "Odd - Name Edition.desktop").read_text(encoding="utf-8")
        self.assertIn("Name=Odd: Name™ / Edition\n", text)


class ReviewTests(FlowBase):
    def assert_review(self, why: str) -> None:
        self.assertEqual(self.review(), [PKG_NAME, f"{PKG_NAME}.reason.txt"], self.out)
        self.assertIn(why, self.reason(PKG_NAME))
        self.assertEqual(self.library(), [], self.out)
        self.assertEqual(self.done(), [])

    def test_an_update_that_the_header_does_not_show_is_caught_by_the_sfo(self) -> None:
        self.sfo(ps4_sfo(TITLE_ID, "Test Game", category="gp"))
        self.drop(PKG_NAME, ps4_pkg())
        self.assertEqual(self.go(), 1, self.out)
        self.assert_review("update")

    def test_dlc_in_the_sfo_goes_to_review(self) -> None:
        self.sfo(ps4_sfo(TITLE_ID, "Test Game", category="ac"))
        self.drop(PKG_NAME, ps4_pkg())
        self.assertEqual(self.go(), 1, self.out)
        self.assert_review("DLC")

    def test_a_failed_extraction_goes_to_review(self) -> None:
        os.environ["STUB_PKG_FAIL"] = "1"
        self.drop(PKG_NAME, ps4_pkg())
        self.assertEqual(self.go(), 1, self.out)
        self.assert_review("orbis-pkg-util could not extract")
        self.assertIn("bad pkg", self.reason(PKG_NAME))

    def test_a_package_without_eboot_or_sfo_goes_to_review(self) -> None:
        for env, what in (("STUB_PKG_NO_EBOOT", "eboot.bin"), ("STUB_PKG_NO_SFO", "param.sfo")):
            with mock.patch.dict(os.environ, {env: "1"}):
                self.drop(PKG_NAME, ps4_pkg())
                self.assertEqual(self.go(), 1, self.out)
                self.assertIn(what, self.reason(PKG_NAME))
                self.assertEqual(self.library(), [])
            for p in (self.inbox / "_revisar").iterdir():
                p.unlink()

    def test_a_title_id_that_differs_from_the_header_goes_to_review(self) -> None:
        self.sfo(ps4_sfo("CUSA99999", "Other"))
        self.drop(PKG_NAME, ps4_pkg())
        self.assertEqual(self.go(), 1, self.out)
        self.assert_review("CUSA99999")

    def test_a_symbolic_link_in_the_output_goes_to_review(self) -> None:
        os.environ["STUB_PKG_LINK"] = "1"
        self.drop(PKG_NAME, ps4_pkg())
        self.assertEqual(self.go(), 1, self.out)
        self.assert_review("symbolic link")

    def test_a_copy_that_fails_leaves_nothing_behind(self) -> None:
        self.drop(PKG_NAME, ps4_pkg())
        real = inbox_core.copy_verified
        seen: list[Path] = []

        def flaky(src: Path, dest: Path) -> None:
            seen.append(dest)
            if len(seen) == 2:
                raise OSError(28, "No space left on device")
            real(src, dest)

        with mock.patch.object(inbox_core, "copy_verified", flaky):
            self.assertEqual(self.go(), 1, self.out)
        self.assert_review("No space left")
        self.assertEqual([p for p in self.roms.rglob("*") if "CUSA" in p.name], [])
        self.assertEqual(list((self.tmp / "work").iterdir()), [])

    def test_a_ps3_pkg_is_still_not_supported(self) -> None:
        self.drop("Old.pkg", ps4_pkg(magic=b"\x7fPKG"))
        self.assertEqual(self.go(), 1, self.out)
        self.assertIn("not supported by inbox yet (PS3", self.reason("Old.pkg"))
        self.assertEqual(self.calls(), [])


class PartsBase(FlowBase):
    """Updates and DLC are filed beside a base game that is already in the library."""

    def base_game(self) -> Path:
        folder = self.games / TITLE_ID
        (folder / "sce_sys").mkdir(parents=True)
        (folder / "eboot.bin").write_bytes(b"MINE")
        (folder / "sce_sys" / "param.sfo").write_bytes(ps4_sfo(TITLE_ID, "Test Game"))
        return folder

    def assert_review(self, why: str) -> None:
        self.assertEqual(self.review(), [PKG_NAME, f"{PKG_NAME}.reason.txt"], self.out)
        self.assertIn(why, self.reason(PKG_NAME))
        self.assertEqual(self.done(), [])

    def leftovers(self) -> list[str]:
        return [
            p.name
            for p in self.roms.rglob("*")
            if p.name.endswith((".part", ".old")) or p.name.startswith("ps4-")
        ]

    def snapshot(self) -> dict[str, bytes]:
        return {str(p): p.read_bytes() for p in self.roms.rglob("*") if p.is_file()}

    def clear_review(self) -> None:
        for p in (self.inbox / "_revisar").iterdir():
            p.unlink()


class UpdateTests(PartsBase):
    def setUp(self) -> None:
        super().setUp()
        self.sfo(ps4_sfo(TITLE_ID, "Test Game", "gp", app_ver="01.09"))
        self.base_game()
        self.dest = self.games / UPDATE

    def pkg(self) -> bytes:
        return ps4_pkg(f"EP9000-{TITLE_ID}_00-TESTGAME0000EU", flags=0x62100000, pfs_size=170852352)

    def library_update(self, version: str, mark: bytes = b"OLD") -> None:
        (self.dest / "sce_sys").mkdir(parents=True)
        (self.dest / "eboot.bin").write_bytes(mark)
        (self.dest / "sce_sys" / "param.sfo").write_bytes(
            ps4_sfo(TITLE_ID, "Test Game", "gp", app_ver=version)
        )

    def test_an_update_goes_beside_the_game_in_its_own_folder(self) -> None:
        os.environ["STUB_PKG_MARK"] = "PATCH"
        self.drop(PKG_NAME, self.pkg())
        self.assertEqual(self.go(), 0, self.out)
        self.assertEqual(
            [x for x in self.library() if UPDATE in x],
            [
                f"{GAME}-UPDATE/Content/data/level.dat",
                f"{GAME}-UPDATE/eboot.bin",
                f"{GAME}-UPDATE/sce_sys/param.sfo",
            ],
        )
        self.assertEqual((self.dest / "eboot.bin").read_bytes(), b"PATCH" * 100)
        self.assertEqual((self.games / TITLE_ID / "eboot.bin").read_bytes(), b"MINE")
        self.assertEqual(self.shortcut_names(), [])  # the game's own shortcut is enough
        self.assertEqual(self.done(), [PKG_NAME])
        self.assertEqual(self.review(), [])
        self.assertIn(f"ps4: {UPDATE}: placed", self.out)
        self.assertIn(f"path: library/roms/ps4/games/{UPDATE}", self.out)
        self.assertIn(f"whitelist: !/roms/ps4/games/{UPDATE}", self.out)
        self.assertEqual(self.leftovers(), [])
        self.assertEqual(list((self.tmp / "work").iterdir()), [])
        (call,) = self.calls()
        self.assertTrue(call.startswith("extract -q -o "), call)

    def test_an_update_needs_the_game_in_the_library(self) -> None:
        shutil.rmtree(self.games / TITLE_ID)
        self.drop(PKG_NAME, self.pkg())
        self.assertEqual(self.go(), 1, self.out)
        self.assert_review("install the game first")
        self.assertIn(TITLE_ID, self.reason(PKG_NAME))
        self.assertEqual(self.library(), [])
        self.assertEqual(self.calls(), [])

    def test_report_only_does_not_extract(self) -> None:
        self.drop(PKG_NAME, self.pkg())
        self.assertEqual(self.go(apply=False), 0, self.out)
        self.assertEqual(self.calls(), [])
        self.assertFalse(self.dest.exists())
        self.assertIn(f"ps4: {UPDATE}: would place", self.out)
        self.assertIn("would extract it with orbis-pkg-util", self.out)
        self.assertIn(f"whitelist: !/roms/ps4/games/{UPDATE}", self.out)

    def test_report_only_with_an_update_in_the_library_says_it_would_compare(self) -> None:
        self.library_update("01.05")
        before = self.snapshot()
        self.drop(PKG_NAME, self.pkg())
        self.assertEqual(self.go(apply=False), 0, self.out)
        self.assertEqual(self.calls(), [])
        self.assertEqual(before, self.snapshot())
        self.assertIn("would extract it and compare its APP_VER", self.out)
        self.assertIn("v01.05", self.out)

    def test_the_same_version_is_left_alone(self) -> None:
        self.library_update("01.09")
        before = self.snapshot()
        self.drop(PKG_NAME, self.pkg())
        self.assertEqual(self.go(), 0, self.out)
        self.assertEqual(before, self.snapshot())
        self.assertIn(f"ps4: {UPDATE}: already in the library", self.out)
        self.assertIn("v01.09", self.out)
        self.assertEqual(self.done(), [PKG_NAME])
        self.assertEqual(self.leftovers(), [])

    def test_an_older_version_goes_to_review_and_the_library_is_not_touched(self) -> None:
        self.library_update("01.10")
        before = self.snapshot()
        self.drop(PKG_NAME, self.pkg())
        self.assertEqual(self.go(), 1, self.out)
        self.assert_review("older than the update in the library, v01.10")
        self.assertEqual(before, self.snapshot())
        self.assertEqual(self.leftovers(), [])

    def test_a_newer_version_replaces_the_update_in_the_library(self) -> None:
        self.library_update("01.05")
        os.environ["STUB_PKG_MARK"] = "PATCH"
        self.drop(PKG_NAME, self.pkg())
        self.assertEqual(self.go(), 0, self.out)
        self.assertEqual((self.dest / "eboot.bin").read_bytes(), b"PATCH" * 100)
        values = inbox_ps4.read_sfo(self.dest / "sce_sys" / "param.sfo")
        self.assertEqual(values["APP_VER"], "01.09")
        self.assertEqual(self.done(), [PKG_NAME])
        self.assertIn(f"ps4: {UPDATE}: placed", self.out)
        self.assertIn("replaced the update v01.05 with v01.09", self.out)
        self.assertEqual(self.leftovers(), [])
        self.assertEqual(sorted(p.name for p in self.games.iterdir()), [TITLE_ID, UPDATE])

    def test_a_library_copy_that_changes_while_copying_is_kept(self) -> None:
        self.library_update("01.05")
        self.drop(PKG_NAME, self.pkg())
        real = inbox_core.copy_verified
        sfo_path = self.dest / "sce_sys" / "param.sfo"

        def meddle(src: Path, dest: Path) -> None:
            real(src, dest)
            sfo_path.write_bytes(ps4_sfo(TITLE_ID, "Test Game", "gp", app_ver="01.07"))

        with mock.patch.object(inbox_core, "copy_verified", meddle):
            self.assertEqual(self.go(), 1, self.out)
        self.assert_review("changed in the library")
        self.assertEqual(inbox_ps4.read_sfo(sfo_path)["APP_VER"], "01.07")
        self.assertEqual((self.dest / "eboot.bin").read_bytes(), b"OLD")
        self.assertEqual(self.leftovers(), [])

    def test_a_failed_copy_keeps_the_old_update(self) -> None:
        self.library_update("01.05")
        before = self.snapshot()
        self.drop(PKG_NAME, self.pkg())
        real = inbox_core.copy_verified
        seen: list[Path] = []

        def flaky(src: Path, dest: Path) -> None:
            seen.append(dest)
            if len(seen) == 2:
                raise OSError(28, "No space left on device")
            real(src, dest)

        with mock.patch.object(inbox_core, "copy_verified", flaky):
            self.assertEqual(self.go(), 1, self.out)
        self.assert_review("No space left")
        self.assertEqual(before, self.snapshot())
        self.assertEqual(self.leftovers(), [])

    def test_a_failed_rename_into_place_brings_the_old_update_back(self) -> None:
        self.library_update("01.05")
        before = self.snapshot()
        self.drop(PKG_NAME, self.pkg())
        real = os.rename

        def rename(src: str | Path, dest: str | Path) -> None:
            if str(src).endswith(".part"):
                raise OSError(5, "Input/output error")
            real(src, dest)

        with mock.patch.object(inbox_ps4.os, "rename", rename):
            self.assertEqual(self.go(), 1, self.out)
        self.assert_review("Input/output error")
        self.assertEqual(before, self.snapshot())
        self.assertEqual(self.leftovers(), [])

    def test_a_replacement_that_was_interrupted_is_picked_up(self) -> None:
        self.library_update("01.05")
        os.rename(self.dest, self.games / f"{UPDATE}.old")  # the run stopped between renames
        self.drop(PKG_NAME, self.pkg())
        self.assertEqual(self.go(), 0, self.out)
        values = inbox_ps4.read_sfo(self.dest / "sce_sys" / "param.sfo")
        self.assertEqual(values["APP_VER"], "01.09")
        self.assertEqual(self.leftovers(), [])

    def test_a_stale_part_folder_is_replaced(self) -> None:
        stale = self.games / f"{UPDATE}.part"
        stale.mkdir()
        (stale / "junk").write_bytes(b"x")
        self.drop(PKG_NAME, self.pkg())
        self.assertEqual(self.go(), 0, self.out)
        self.assertTrue((self.dest / "eboot.bin").is_file())
        self.assertEqual(self.leftovers(), [])

    def test_the_library_update_without_a_readable_version_goes_to_review(self) -> None:
        self.library_update("01.05")
        (self.dest / "sce_sys" / "param.sfo").unlink()
        self.drop(PKG_NAME, self.pkg())
        self.assertEqual(self.go(), 1, self.out)
        self.assert_review("APP_VER")
        self.assertEqual((self.dest / "eboot.bin").read_bytes(), b"OLD")

    def test_an_update_without_a_version_goes_to_review(self) -> None:
        self.sfo(ps4_sfo(TITLE_ID, "Test Game", "gp", app_ver=None))
        self.drop(PKG_NAME, self.pkg())
        self.assertEqual(self.go(), 1, self.out)
        self.assert_review("APP_VER")
        self.assertFalse(self.dest.exists())

    def test_the_sfo_must_say_update_for_the_same_title(self) -> None:
        for sfo, why in (
            (ps4_sfo(TITLE_ID, "Test Game", "gd"), "gd"),
            (ps4_sfo(TITLE_ID, "Test Game", "ac"), "DLC"),
            (ps4_sfo("CUSA99999", "Other", "gp", app_ver="01.09"), "CUSA99999"),
        ):
            self.sfo(sfo)
            self.drop(PKG_NAME, self.pkg())
            self.assertEqual(self.go(), 1, self.out)
            self.assertIn(why, self.reason(PKG_NAME))
            self.assertFalse(self.dest.exists())
            self.clear_review()

    def test_a_symbolic_link_in_an_update_goes_to_review(self) -> None:
        os.environ["STUB_PKG_LINK"] = "1"
        self.drop(PKG_NAME, self.pkg())
        self.assertEqual(self.go(), 1, self.out)
        self.assert_review("symbolic link")
        self.assertFalse(self.dest.exists())

    def test_a_failed_extraction_of_an_update_goes_to_review(self) -> None:
        os.environ["STUB_PKG_FAIL"] = "1"
        self.drop(PKG_NAME, self.pkg())
        self.assertEqual(self.go(), 1, self.out)
        self.assert_review("orbis-pkg-util could not extract")


class DlcTests(PartsBase):
    def setUp(self) -> None:
        super().setUp()
        self.sfo(ps4_sfo(TITLE_ID, "Test DLC", "ac", content_id=DLC_CONTENT_ID))
        self.base_game()
        self.dest = self.roms / "ps4" / "dlc" / TITLE_ID / LABEL

    def pkg(self, **kw) -> bytes:  # type: ignore[no-untyped-def]
        kw.setdefault("content_type", 0x1B)
        kw.setdefault("pfs_size", 4096)
        return ps4_pkg(DLC_CONTENT_ID, **kw)

    def test_dlc_with_data_goes_under_its_title_and_label(self) -> None:
        os.environ["STUB_PKG_NO_EBOOT"] = "1"
        os.environ["STUB_PKG_LICENSE"] = "1"
        self.drop(PKG_NAME, self.pkg())
        self.assertEqual(self.go(), 0, self.out)
        self.assertEqual(
            [x for x in self.library() if "/dlc/" in x],
            [
                f"ps4/dlc/{TITLE_ID}/{LABEL}/Content/data/level.dat",
                f"ps4/dlc/{TITLE_ID}/{LABEL}/sce_sys/license.dat",
                f"ps4/dlc/{TITLE_ID}/{LABEL}/sce_sys/param.sfo",
            ],
        )
        self.assertEqual(self.done(), [PKG_NAME])
        self.assertIn(f"ps4: {TITLE_ID}/{LABEL}: placed", self.out)
        self.assertIn(f"path: library/roms/ps4/dlc/{TITLE_ID}/{LABEL}", self.out)
        self.assertIn(f"whitelist: !/roms/ps4/dlc/{TITLE_ID}", self.out)
        self.assertEqual(self.leftovers(), [])
        self.assertEqual(self.shortcut_names(), [])
        self.assertEqual(list((self.tmp / "work").iterdir()), [])

    def test_dlc_without_data_is_accepted_when_the_tool_stops_on_the_empty_pfs(self) -> None:
        os.environ["STUB_PKG_NO_DATA"] = "1"
        self.drop(PKG_NAME, self.pkg(content_type=0x1C, flags=0x08000000, pfs_size=0))
        self.assertEqual(self.go(), 0, self.out)
        self.assertEqual(
            [x for x in self.library() if "/dlc/" in x],
            [
                f"ps4/dlc/{TITLE_ID}/{LABEL}/sce_sys/license.dat",
                f"ps4/dlc/{TITLE_ID}/{LABEL}/sce_sys/param.sfo",
            ],
        )
        self.assertEqual(self.done(), [PKG_NAME])
        self.assertIn(f"whitelist: !/roms/ps4/dlc/{TITLE_ID}", self.out)
        self.assertEqual(self.leftovers(), [])

    def test_the_tool_failing_on_a_dlc_that_has_data_goes_to_review(self) -> None:
        os.environ["STUB_PKG_NO_DATA"] = "1"
        self.drop(PKG_NAME, self.pkg(pfs_size=4096))
        self.assertEqual(self.go(), 1, self.out)
        self.assert_review("orbis-pkg-util could not extract")
        self.assertFalse(self.dest.exists())

    def test_a_header_without_a_pfs_size_does_not_excuse_the_failure(self) -> None:
        os.environ["STUB_PKG_NO_DATA"] = "1"
        self.drop(PKG_NAME, ps4_pkg(DLC_CONTENT_ID, content_type=0x1C)[: ci.PS4_HEADER_BYTES])
        self.assertEqual(self.go(), 1, self.out)
        self.assert_review("orbis-pkg-util could not extract")

    def test_a_dlc_without_data_and_without_an_sfo_goes_to_review(self) -> None:
        os.environ["STUB_PKG_NO_DATA"] = "1"
        os.environ["STUB_PKG_NO_SFO"] = "1"
        self.drop(PKG_NAME, self.pkg(content_type=0x1C, pfs_size=0))
        self.assertEqual(self.go(), 1, self.out)
        self.assert_review("orbis-pkg-util could not extract")
        self.assertFalse(self.dest.exists())

    def test_dlc_for_a_game_that_is_not_in_the_library_goes_to_review(self) -> None:
        shutil.rmtree(self.games / TITLE_ID)
        self.drop(PKG_NAME, self.pkg())
        self.assertEqual(self.go(), 1, self.out)
        self.assert_review(f"DLC is for {TITLE_ID}, which is not in the library")
        self.assertIn("inbox/CUSAnnnnn/", self.reason(PKG_NAME))
        self.assertNotIn("region", self.reason(PKG_NAME))
        self.assertEqual(self.library(), [])
        self.assertEqual(self.calls(), [])

    def test_report_only_does_not_extract(self) -> None:
        self.drop(PKG_NAME, self.pkg())
        self.assertEqual(self.go(apply=False), 0, self.out)
        self.assertEqual(self.calls(), [])
        self.assertFalse(self.dest.exists())
        self.assertIn(f"ps4: {TITLE_ID}/{LABEL}: would place", self.out)
        self.assertIn("would extract it with orbis-pkg-util", self.out)
        self.assertIn(f"whitelist: !/roms/ps4/dlc/{TITLE_ID}", self.out)

    def test_an_entry_in_the_library_is_not_compared_or_touched(self) -> None:
        self.dest.mkdir(parents=True)
        (self.dest / "mine").write_bytes(b"MINE")
        before = self.snapshot()
        self.drop(PKG_NAME, self.pkg())
        self.assertEqual(self.go(), 0, self.out)
        self.assertEqual(before, self.snapshot())
        self.assertEqual(self.calls(), [])
        self.assertIn("already in the library", self.out)
        self.assertIn("not compared", self.out)
        self.assertIn(f"whitelist: !/roms/ps4/dlc/{TITLE_ID}", self.out)
        self.assertEqual(self.done(), [PKG_NAME])

    def test_a_rerun_changes_nothing(self) -> None:
        os.environ["STUB_PKG_NO_DATA"] = "1"
        data = self.pkg(content_type=0x1C, pfs_size=0)
        self.drop(PKG_NAME, data)
        self.assertEqual(self.go(), 0, self.out)
        before = self.snapshot()
        self.drop(PKG_NAME, data)
        self.assertEqual(self.go(), 0, self.out)
        self.assertEqual(before, self.snapshot())

    def test_the_sfo_must_say_dlc_for_the_same_title_and_content(self) -> None:
        other = f"UP9000-{TITLE_ID}_00-OTHERLABEL"
        for sfo, why in (
            (ps4_sfo(TITLE_ID, "Test DLC", "gd", content_id=DLC_CONTENT_ID), "gd"),
            (ps4_sfo(TITLE_ID, "Test DLC", "gp", content_id=DLC_CONTENT_ID), "update"),
            (ps4_sfo("CUSA99999", "Other", "ac", content_id=DLC_CONTENT_ID), "CUSA99999"),
            (ps4_sfo(TITLE_ID, "Test DLC", "ac", content_id=other), "OTHERLABEL"),
        ):
            self.sfo(sfo)
            self.drop(PKG_NAME, self.pkg())
            self.assertEqual(self.go(), 1, self.out)
            self.assertIn(why, self.reason(PKG_NAME))
            self.assertFalse(self.dest.exists())
            self.clear_review()

    def test_an_unsafe_label_goes_to_review_before_anything_is_extracted(self) -> None:
        self.drop(PKG_NAME, ps4_pkg(f"UP9000-{TITLE_ID}_00-AB.C", content_type=0x1B, pfs_size=1))
        self.assertEqual(self.go(), 1, self.out)
        self.assert_review("entitlement label")
        self.assertEqual(self.calls(), [])
        self.assertEqual([x for x in self.library() if "/dlc/" in x], [])

    def test_a_symbolic_link_in_a_dlc_goes_to_review(self) -> None:
        os.environ["STUB_PKG_LINK"] = "1"
        self.drop(PKG_NAME, self.pkg())
        self.assertEqual(self.go(), 1, self.out)
        self.assert_review("symbolic link")
        self.assertFalse(self.dest.exists())

    def other_title_dlc(self) -> bytes:
        """The DLC of another title id (CUSA00900), to be filed for the game in the library."""
        self.sfo(
            ps4_sfo(
                "CUSA00900",
                "Bloodborne The Old Hunters",
                "ac",
                content_id="UP9000-CUSA00900_00-SPEXPANSIONDLC03",
            )
        )
        os.environ["STUB_PKG_NO_DATA"] = "1"
        return ps4_pkg(
            "UP9000-CUSA00900_00-SPEXPANSIONDLC03",
            content_type=0x1C,
            flags=0x08000000,
            pfs_size=0,
        )

    def test_an_item_folder_named_after_a_game_files_the_dlc_for_that_game(self) -> None:
        self.drop("CUSA03173", {"sub/dlc.pkg": self.other_title_dlc()})
        (self.games / "CUSA03173").mkdir()  # the game of the folder, whatever it holds
        self.assertEqual(self.go(), 0, self.out)
        self.assertEqual(
            [x for x in self.library() if "/dlc/" in x],
            [
                "ps4/dlc/CUSA03173/SPEXPANSIONDLC03/sce_sys/license.dat",
                "ps4/dlc/CUSA03173/SPEXPANSIONDLC03/sce_sys/param.sfo",
            ],
        )
        self.assertIn("ps4: CUSA03173/SPEXPANSIONDLC03: placed", self.out)
        self.assertIn("whitelist: !/roms/ps4/dlc/CUSA03173", self.out)
        self.assertNotIn("!/roms/ps4/dlc/CUSA00900", self.out)
        self.assertFalse((self.roms / "ps4" / "dlc" / "CUSA00900").exists())
        self.assertEqual(self.leftovers(), [])

    def test_the_game_of_the_item_folder_must_be_in_the_library(self) -> None:
        self.drop("CUSA03173", {"dlc.pkg": self.other_title_dlc()})
        self.assertEqual(self.go(), 1, self.out)
        reason = self.reason("CUSA03173")
        self.assertIn("DLC is for CUSA03173, which is not in the library", reason)
        self.assertIn("inbox/CUSAnnnnn/", reason)
        self.assertNotIn("region", reason)
        self.assertEqual([x for x in self.library() if "/dlc/" in x], [])
        self.assertEqual(self.calls(), [])

    def test_the_own_game_does_not_stand_in_for_the_one_of_the_folder(self) -> None:
        self.drop("CUSA03173", {"dlc.pkg": self.pkg()})  # its own game is in the library
        self.assertEqual(self.go(), 1, self.out)
        self.assertIn("CUSA03173", self.reason("CUSA03173"))
        self.assertEqual([x for x in self.library() if "/dlc/" in x], [])

    def test_a_folder_that_is_not_a_title_id_files_the_dlc_for_its_own_title(self) -> None:
        for name in ("Pack", "CUSA0317", "CUSA03173 DLC", "cusa03173"):
            self.drop(name, {"dlc.pkg": self.pkg()})
            self.assertEqual(self.go(), 0, self.out)
            self.assertTrue((self.dest / "sce_sys" / "param.sfo").is_file(), name)
            shutil.rmtree(self.roms / "ps4" / "dlc")

    def test_a_zip_named_after_a_game_does_not_count_as_a_folder(self) -> None:
        self.zip_of("CUSA03173.zip", {"dlc.pkg": self.pkg()})
        self.assertEqual(self.go(), 0, self.out)
        self.assertTrue((self.dest / "sce_sys" / "param.sfo").is_file())

    def test_dlc_in_a_zip(self) -> None:
        os.environ["STUB_PKG_NO_DATA"] = "1"
        self.zip_of("dlc.zip", {"x/dlc.pkg": self.pkg(content_type=0x1C, pfs_size=0)})
        self.assertEqual(self.go(), 0, self.out)
        self.assertTrue((self.dest / "sce_sys" / "param.sfo").is_file())


class WithoutTheToolTests(FlowBase):
    def test_the_package_is_placed_as_it_is_with_a_note(self) -> None:
        data = ps4_pkg()
        self.drop(PKG_NAME, data)
        self.assertEqual(self.go(tool=False), 0, self.out)
        self.assertEqual(self.library(), [f"ps4/{PKG_NAME}"])
        self.assertEqual((self.roms / "ps4" / PKG_NAME).read_bytes(), data)
        self.assertIn("orbis-pkg-util is not configured", self.out)
        self.assertIn("[inbox] orbis_pkg_util", self.out)
        self.assertIn(f"whitelist: !/roms/ps4/{PKG_NAME}", self.out)
        self.assertEqual(self.calls(), [])
        self.assertIn("orbis-pkg-util no", self.out)
        self.assertEqual(self.done(), [PKG_NAME])


if __name__ == "__main__":
    unittest.main()
