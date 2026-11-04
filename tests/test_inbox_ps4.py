import io
import os
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
EXEC = f'Exec=shadps4 "/home/deck/Emulation/roms/ps4/games/{TITLE_ID}/eboot.bin"'


class PureTests(unittest.TestCase):
    def test_a_header_tells_a_game_from_an_update_and_dlc(self) -> None:
        def kind(**kw: int) -> str | None:
            return inbox_ps4.pkg_kind(ps4_pkg(**kw)[: ci.PS4_HEADER_BYTES])

        self.assertEqual(kind(), "game")
        self.assertEqual(kind(content_type=0x1B), "dlc")
        self.assertEqual(kind(content_type=0x1A, flags=0x0A000000 | 0x40000000), "update")
        self.assertEqual(kind(content_type=0x1A, flags=0x0A000000 | 0x100000), "update")
        self.assertEqual(kind(content_type=0x1A, flags=0x0A000000 | 0x60000000), "update")
        self.assertEqual(kind(content_type=0x99), "other")
        self.assertIsNone(inbox_ps4.pkg_kind(ps4_pkg()[:0x70]))
        self.assertIsNone(inbox_ps4.pkg_kind(b""))

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

    def test_an_update_goes_to_review(self) -> None:
        self.drop(PKG_NAME, ps4_pkg(flags=0x0A000000 | 0x40000000))
        self.assertEqual(self.go(), 1, self.out)
        self.assert_review("update")
        self.assertIn("not merged", self.reason(PKG_NAME))
        self.assertEqual(self.calls(), [])

    def test_an_update_goes_to_review_even_with_the_base_game_in_the_library(self) -> None:
        (self.games / TITLE_ID).mkdir(parents=True)
        (self.games / TITLE_ID / "eboot.bin").write_bytes(b"MINE")
        self.drop(PKG_NAME, ps4_pkg(flags=0x0A000000 | 0x40000000))
        self.assertEqual(self.go(), 1, self.out)
        self.assertEqual((self.games / TITLE_ID / "eboot.bin").read_bytes(), b"MINE")
        self.assertIn("update", self.reason(PKG_NAME))

    def test_dlc_goes_to_review(self) -> None:
        self.drop(PKG_NAME, ps4_pkg(content_type=0x1B))
        self.assertEqual(self.go(), 1, self.out)
        self.assert_review("DLC")

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
