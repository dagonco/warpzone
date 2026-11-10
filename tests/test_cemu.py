"""Cemu's MLC path and game paths: pure edits, the profile table, provisioning and doctor."""

import dataclasses
import os
import shutil
import sys
import unittest
from pathlib import Path
from unittest import mock

from support import API_KEY, DECK_ID, HUB_ID, device_probes
from test_doctor import by_name, context
from test_shadps4 import Device as ShadDevice
from warpzone import cemu
from warpzone.doctor import OK, WARN, Context, check_frontend
from warpzone.profiles import ProfileError, load_profile, parse_profile

PROFILE = load_profile("steamos-emudeck")
MLC = "/home/deck/Emulation/roms/wiiu/mlc01"
GAMES = "/home/deck/Emulation/roms/wiiu/roms"
SD = "/run/media/mmcblk0p1/Emulation/roms/wiiu/roms"
# The shape of EmuDeck's template: spaces, with a tab-indented GamePaths block.
SETTINGS = (
    '<?xml version="1.0" encoding="UTF-8"?>\n'
    "<content>\n"
    "    <logflag>0</logflag>\n"
    "    <mlc_path></mlc_path>\n"
    "    <permanent_storage>true</permanent_storage>\n"
    "    <RecentLaunchFiles/>\n"
    "    <GamePaths>\n"
    f"\t\t<Entry>{SD}</Entry>\n"
    "\t</GamePaths>\n"
    "\t<GameCache/>\n"
    "    <GraphicPack/>\n"
    "</content>\n"
)


class ReadTests(unittest.TestCase):
    def test_the_paths_are_read(self) -> None:
        paths = cemu.read(SETTINGS)
        self.assertEqual(paths.mlc, "")
        self.assertEqual(paths.games, (SD,))

    def test_an_empty_mlc_path_means_the_default_folder(self) -> None:
        self.assertEqual(cemu.in_use(cemu.read(SETTINGS), "/d/mlc01"), "/d/mlc01")
        text = SETTINGS.replace("<mlc_path></mlc_path>", f"<mlc_path>{MLC}</mlc_path>")
        self.assertEqual(cemu.in_use(cemu.read(text), "/d/mlc01"), MLC)

    def test_missing_names_what_differs(self) -> None:
        self.assertEqual(cemu.missing(SETTINGS, MLC, GAMES), ("mlc_path", "GamePaths"))
        done = cemu.apply(SETTINGS, MLC, GAMES)
        self.assertEqual(cemu.missing(done, MLC, GAMES), ())
        self.assertEqual(cemu.missing(done, MLC + "/", GAMES + "/"), ())

    def test_an_unusable_declared_encoding_is_refused(self) -> None:
        for enc in ("bogus", "shift_jis", "big5", "utf-16-le", "idna"):
            text = f'<?xml version="1.0" encoding="{enc}"?><content/>'
            with self.subTest(encoding=enc), self.assertRaises(cemu.CemuConfigError):
                cemu.read(text)

    def test_a_file_it_does_not_understand_is_refused(self) -> None:
        for text in (
            "not xml",
            "<other><mlc_path/></other>",
            "<content><mlc_path>a</mlc_path><mlc_path>b</mlc_path></content>",
            "<content><GamePaths/><GamePaths/></content>",
        ):
            with self.assertRaises(cemu.CemuConfigError, msg=text):
                cemu.read(text)


class TitleSaveTests(unittest.TestCase):
    def test_only_files_of_games_and_demos_count(self) -> None:
        for rel in ("00050000/10142400/user/common/rp_savedata.dat", "00050002/1/user/x"):
            self.assertTrue(cemu.is_title_save(rel), rel)
        for rel in (
            "00050010/1004a000/user/common/db",
            "system/act/80000001/account.dat",
            "00050000",
            "x/00050000/a",
        ):
            self.assertFalse(cemu.is_title_save(rel), rel)


class ApplyTests(unittest.TestCase):
    def test_only_the_two_keys_change_and_every_other_byte_is_kept(self) -> None:
        new = cemu.apply(SETTINGS, MLC, GAMES)
        expected = SETTINGS.replace("<mlc_path></mlc_path>", f"<mlc_path>{MLC}</mlc_path>").replace(
            f"\t\t<Entry>{SD}</Entry>\n", f"\t\t<Entry>{SD}</Entry>\n\t\t<Entry>{GAMES}</Entry>\n"
        )
        self.assertEqual(new, expected)

    def test_a_second_run_changes_nothing(self) -> None:
        once = cemu.apply(SETTINGS, MLC, GAMES)
        self.assertEqual(cemu.apply(once, MLC, GAMES), once)

    def test_none_leaves_that_key_alone(self) -> None:
        new = cemu.apply(SETTINGS, None, GAMES)
        self.assertEqual(cemu.read(new).mlc, "")
        self.assertEqual(cemu.read(new).games, (SD, GAMES))
        new = cemu.apply(SETTINGS, MLC, None)
        self.assertEqual(cemu.read(new), cemu.Paths(MLC, (SD,)))

    def test_another_mlc_path_is_replaced(self) -> None:
        text = SETTINGS.replace("<mlc_path></mlc_path>", "<mlc_path>/elsewhere</mlc_path>")
        self.assertEqual(cemu.read(cemu.apply(text, MLC, None)).mlc, MLC)

    def test_self_closing_and_missing_elements_are_filled_in(self) -> None:
        text = "<content>\n    <mlc_path/>\n    <GamePaths/>\n    <GraphicPack/>\n</content>\n"
        new = cemu.apply(text, MLC, GAMES)
        self.assertEqual(cemu.read(new), cemu.Paths(MLC, (GAMES,)))
        self.assertIn("    <GraphicPack/>\n</content>\n", new)
        bare = "<content>\n    <logflag>0</logflag>\n</content>\n"
        new = cemu.apply(bare, MLC, GAMES)
        self.assertEqual(cemu.read(new), cemu.Paths(MLC, (GAMES,)))
        self.assertTrue(new.endswith("</content>\n"))
        self.assertIn("    <logflag>0</logflag>\n", new)

    def test_an_entry_is_added_after_a_self_closing_one(self) -> None:
        text = "<content>\n    <GamePaths>\n        <Entry/>\n    </GamePaths>\n</content>\n"
        new = cemu.apply(text, None, GAMES)
        self.assertEqual(
            new, text.replace("<Entry/>\n", f"<Entry/>\n        <Entry>{GAMES}</Entry>\n")
        )

    def test_values_are_escaped(self) -> None:
        new = cemu.apply(SETTINGS, "/a&b/mlc01", "/a<b>/roms")
        self.assertIn("<mlc_path>/a&amp;b/mlc01</mlc_path>", new)
        self.assertEqual(cemu.read(new), cemu.Paths("/a&b/mlc01", (SD, "/a<b>/roms")))

    def test_crlf_files_keep_their_line_ends(self) -> None:
        text = SETTINGS.replace("\n", "\r\n")
        new = cemu.apply(text, MLC, GAMES)
        self.assertNotIn("\n", new.replace("\r\n", ""))

    def test_a_key_hidden_in_a_comment_or_nested_is_refused(self) -> None:
        commented = SETTINGS.replace(
            "<logflag>", "<!-- <mlc_path>old</mlc_path> -->\n    <logflag>"
        )
        nested = SETTINGS.replace(
            "<GraphicPack/>", "<GraphicPack><mlc_path>x</mlc_path></GraphicPack>"
        )
        for text in (commented, nested):
            with self.assertRaises(cemu.CemuConfigError):
                cemu.apply(text, MLC, GAMES)


class ProfileTests(unittest.TestCase):
    def test_the_shipped_profile_matches_the_save_folder_it_syncs(self) -> None:
        rule = PROFILE.cemu
        assert rule is not None
        self.assertEqual(rule.settings, "~/.config/Cemu/settings.xml")
        self.assertEqual(rule.default_mlc, "~/.local/share/Cemu/mlc01")
        self.assertIn("Cemu", rule.process)
        library = PROFILE.paths["library"]
        mlc, games = rule.wants(library)
        self.assertEqual(f"{mlc}/usr/save", PROFILE.saves["cemu-saves"].path)
        self.assertEqual(games, "~/Emulation/roms/wiiu/roms")
        self.assertIn("/roms/wiiu/mlc01", PROFILE.library_ignore)

    def test_the_other_shipped_profile_has_none(self) -> None:
        self.assertIsNone(load_profile("anbernic-rg351mp").cemu)

    def parse(self, **table):
        return parse_profile({"name": "x", "paths": {"library": "~/lib"}, "cemu": table}, "t")

    def test_the_table_is_checked(self) -> None:
        good = {
            "settings": "~/.config/Cemu/settings.xml",
            "process": ["Cemu"],
            "mlc": "{library}/mlc01",
            "default_mlc": "~/.local/share/Cemu/mlc01",
            "game_path": "{library}/roms",
        }
        rule = self.parse(**good).cemu
        assert rule is not None
        self.assertEqual(rule.wants("/l"), ("/l/mlc01", "/l/roms"))
        for change, message in (
            ({"settings": "rel/settings.xml"}, "cemu.settings"),
            ({"process": []}, "cemu.process"),
            ({"mlc": "{nope}/m"}, "cemu.mlc"),
            ({"game_path": "rel"}, "cemu.game_path"),
            ({"default_mlc": ""}, "cemu.default_mlc"),
            ({"extra": 1}, "unknown key"),
        ):
            with self.assertRaisesRegex(ProfileError, message):
                self.parse(**{**good, **change})
        with self.assertRaisesRegex(ProfileError, "cemu.mlc"):
            parse_profile({"name": "x", "cemu": good}, "t")


class Device(ShadDevice):
    """A temporary home with Cemu's settings.xml as EmuDeck leaves it."""

    def setUp(self) -> None:
        super().setUp()
        self.config = self.home / ".config/Cemu"
        self.config.mkdir(parents=True)
        self.settings = self.config / "settings.xml"
        self.settings.write_text(SETTINGS)
        self.mlc = f"{self.home}/Emulation/roms/wiiu/mlc01"
        self.games = f"{self.home}/Emulation/roms/wiiu/roms"
        self.default = self.home / ".local/share/Cemu/mlc01"
        self.profile = dataclasses.replace(self.profile, shadps4=None)

    @staticmethod
    def mine(rep) -> list[str]:
        return [w for w in rep.warnings if "Cemu" in w]

    def backups(self) -> list[Path]:
        folder = self.state / "backups" / "cemu"
        return sorted(p for p in folder.rglob("*") if p.is_file()) if folder.is_dir() else []


class ProvisionTests(Device, unittest.TestCase):
    def test_the_paths_are_set_and_the_rest_is_kept(self) -> None:
        rep, lines = self.provision()
        self.assertEqual(self.mine(rep), [])
        self.assertEqual(
            cemu.read(self.settings.read_text()), cemu.Paths(self.mlc, (SD, self.games))
        )
        self.assertEqual(self.settings.read_text(), cemu.apply(SETTINGS, self.mlc, self.games))
        self.assertTrue(any(line.startswith("cemu: set") for line in lines))

    def test_the_old_file_is_backed_up_and_a_second_run_changes_nothing(self) -> None:
        self.provision()
        self.assertEqual([p.read_text() for p in self.backups()], [SETTINGS])
        stamp = self.settings.stat().st_mtime_ns
        rep, lines = self.provision()
        self.assertEqual(self.settings.stat().st_mtime_ns, stamp)
        self.assertEqual(len(self.backups()), 1)
        self.assertEqual(self.mine(rep), [])
        self.assertTrue(any("already" in line and "cemu" in line for line in lines))

    def test_nothing_is_edited_while_cemu_runs(self) -> None:
        for comm in ("Cemu", "Cemu.AppImage"):
            with self.subTest(comm=comm):
                self.start(comm)
                rep, _ = self.provision()
                self.assertEqual(self.settings.read_text(), SETTINGS)
                (warning,) = [w for w in rep.warnings if "Cemu is running" in w]
                self.assertIn("provision device deck", warning)
                self.assertEqual(self.backups(), [])
                shutil.rmtree(self.proc.path / "4242")

    def test_cemu_started_during_the_backup_stops_the_edit(self) -> None:
        with self.after_backup(lambda path: self.start("Cemu")):
            rep, _ = self.provision()
        self.assertEqual(self.settings.read_text(), SETTINGS)
        self.assertEqual(len([w for w in rep.warnings if "Cemu is running" in w]), 1)

    def test_a_file_edited_during_the_backup_is_left_alone(self) -> None:
        def edit(path: Path) -> None:
            path.write_text(path.read_text() + "\n")

        with self.after_backup(edit):
            rep, _ = self.provision()
        self.assertEqual(self.settings.read_text(), SETTINGS + "\n")
        self.assertTrue(any("changed while" in w for w in rep.warnings))

    def test_without_proc_nothing_is_edited(self) -> None:
        rep, _ = self.provision(probes=device_probes(self.proc.path / "missing"))
        self.assertEqual(self.settings.read_text(), SETTINGS)
        self.assertTrue(any("/proc" in w and "Cemu" in w for w in rep.warnings))

    def test_dry_run_writes_nothing(self) -> None:
        _rep, lines = self.provision(dry_run=True)
        self.assertEqual(self.settings.read_text(), SETTINGS)
        self.assertEqual(self.backups(), [])
        self.assertTrue(any("dry-run: would set" in line and "mlc_path" in line for line in lines))

    def test_a_missing_file_is_not_created_and_it_says_so(self) -> None:
        self.settings.unlink()
        rep, _ = self.provision()
        self.assertFalse(self.settings.exists())
        (warning,) = [w for w in self.mine(rep) if "settings" in w]
        self.assertIn("start Cemu once", warning)

    def test_a_file_it_does_not_understand_is_left_alone(self) -> None:
        self.settings.write_text("not xml")
        rep, _ = self.provision()
        self.assertEqual(self.settings.read_text(), "not xml")
        self.assertTrue(any("settings.xml" in w and "left alone" in w for w in rep.warnings))

    def title_save(self, mlc: Path, top: str = "00050000") -> Path:
        save = mlc / f"usr/save/{top}/10142400/user/common/rp_savedata.dat"
        save.parent.mkdir(parents=True)
        save.write_bytes(b"save")
        return save

    def skeleton(self, mlc: Path) -> None:
        """What Cemu creates in its MLC folder on every start, before any game runs."""
        for title in ("1004a000", "1004a100", "1004a200"):
            (mlc / f"usr/save/00050010/{title}/user/common/db").mkdir(parents=True)
        account = mlc / "usr/save/system/act/80000001/account.dat"
        account.parent.mkdir(parents=True)
        account.write_text("AccountInstance_20120705\n")

    def test_saves_in_the_folder_cemu_uses_now_keep_its_mlc_path(self) -> None:
        for top in ("00050000", "00050002"):
            with self.subTest(top=top):
                self.settings.write_text(SETTINGS)
                save = self.title_save(self.default, top)
                rep, _ = self.provision()
                paths = cemu.read(self.settings.read_text())
                self.assertEqual(paths, cemu.Paths("", (SD, self.games)))
                (warning,) = [w for w in self.mine(rep) if "mlc_path" in w]
                self.assertIn(str(self.default / "usr/save"), warning)
                self.assertIn(f"{self.mlc}/usr/save", warning)
                self.assertIn("usr/title", warning)
                shutil.rmtree(self.default)
                del save

    def test_the_folders_cemu_creates_on_every_start_do_not_hold_it_back(self) -> None:
        self.skeleton(self.default)
        rep, _ = self.provision()
        self.assertEqual(cemu.read(self.settings.read_text()).mlc, self.mlc)
        self.assertEqual([w for w in self.mine(rep) if "mlc_path" in w], [])

    @unittest.skipIf(
        sys.platform == "win32" or os.geteuid() == 0, "root reads a folder without permissions"
    )
    def test_a_save_folder_it_cannot_read_holds_it_back(self) -> None:
        self.skeleton(self.default)
        games = self.default / "usr/save/00050000"
        games.mkdir()
        games.chmod(0)
        self.addCleanup(games.chmod, 0o755)
        rep, _ = self.provision()
        self.assertEqual(cemu.read(self.settings.read_text()).mlc, "")
        self.assertEqual(len([w for w in self.mine(rep) if "mlc_path" in w]), 1)

    def test_an_old_folder_that_is_the_synced_one_is_not_another(self) -> None:
        self.title_save(Path(self.mlc))
        self.default.parent.mkdir(parents=True)
        self.default.symlink_to(self.mlc)
        rep, _ = self.provision()
        self.assertEqual(cemu.read(self.settings.read_text()).mlc, self.mlc)
        self.assertEqual([w for w in self.mine(rep) if "mlc_path" in w], [])

    def test_home_relative_paths_are_expanded(self) -> None:
        rule = dataclasses.replace(self.profile.cemu, mlc="~/wiiu/mlc01", game_path="~/wiiu/roms")
        self.profile = dataclasses.replace(self.profile, cemu=rule)
        self.provision()
        paths = cemu.read(self.settings.read_text())
        self.assertEqual(
            paths, cemu.Paths(f"{self.home}/wiiu/mlc01", (SD, f"{self.home}/wiiu/roms"))
        )

    def test_an_empty_save_folder_does_not_hold_it_back(self) -> None:
        (self.default / "usr/save").mkdir(parents=True)
        self.provision()
        self.assertEqual(cemu.read(self.settings.read_text()).mlc, self.mlc)

    def test_an_edit_that_fails_after_the_check_leaves_the_file_alone(self) -> None:
        with mock.patch.object(cemu, "apply", side_effect=cemu.CemuConfigError("cannot set it")):
            rep, _ = self.provision()
        self.assertEqual(self.settings.read_text(), SETTINGS)
        self.assertTrue(any("left alone: cannot set it" in w for w in rep.warnings))


class DoctorTests(Device, unittest.TestCase):
    CONFIG = f"""
[hub]
name = "nas"
device_id = "{HUB_ID}"
root = "/data/games"
api_url = "http://127.0.0.1:1"
api_key = "{API_KEY}"

[devices.deck]
device_id = "{DECK_ID}"
profile = "steamos-emudeck"
folders = ["library", "saves"]
"""

    def setUp(self) -> None:
        super().setUp()
        syncthing = self.home / ".config/syncthing"
        syncthing.mkdir(parents=True)
        (syncthing / "config.xml").write_text(
            f'<configuration><folder id="wz-library" path="{self.home}/Emulation/">'
            f'<device id="{HUB_ID}"/></folder><device id="{DECK_ID}" name="x"/>'
            "<gui><address>127.0.0.1:8384</address><apikey>k</apikey></gui></configuration>"
        )

    def check(self):
        ctx = context(Path(self._tmp.name), self.CONFIG, system="Linux")
        probes = dataclasses.replace(
            ctx.probes,
            home=self.home,
            is_executable=lambda x: os.path.isfile(x) and os.access(x, os.X_OK),
        )
        devices = tuple(
            dataclasses.replace(d, profile=self.profile) for d in ctx.config.frontend_devices
        )
        return check_frontend(
            Context(probes, dataclasses.replace(ctx.config, frontend_devices=devices))
        )

    def test_a_provisioned_device_is_ok(self) -> None:
        self.provision()
        result = by_name(self.check(), "Cemu paths")
        self.assertEqual(result.status, OK, result.detail)

    def test_paths_left_as_emudeck_wrote_them_are_a_warning_with_the_fix(self) -> None:
        result = by_name(self.check(), "Cemu paths")
        self.assertEqual(result.status, WARN)
        self.assertIn("mlc_path is empty", result.detail)
        self.assertIn(str(self.default), result.detail)
        self.assertIn(self.games, result.detail)
        self.assertIn("warpzone provision device deck", result.fix)

    def test_only_what_differs_is_named(self) -> None:
        self.provision()
        text = self.settings.read_text().replace(self.mlc, "/elsewhere")
        self.settings.write_text(text)
        result = by_name(self.check(), "Cemu paths")
        self.assertEqual(result.status, WARN)
        self.assertIn("/elsewhere", result.detail)
        self.assertNotIn("game", result.detail)

    def test_no_settings_file_yet_says_to_start_cemu(self) -> None:
        self.settings.unlink()
        result = by_name(self.check(), "Cemu paths")
        self.assertEqual(result.status, WARN)
        self.assertIn("start Cemu once", result.fix)

    def test_a_file_it_cannot_read_is_a_warning(self) -> None:
        self.settings.write_text("not xml")
        result = by_name(self.check(), "Cemu paths")
        self.assertEqual(result.status, WARN)
        self.assertIn("settings.xml", result.detail)

    def test_saves_in_the_old_folder_say_to_move_them(self) -> None:
        save = self.default / "usr/save/00050000/10142400/user/common/rp_savedata.dat"
        save.parent.mkdir(parents=True)
        save.write_bytes(b"save")
        result = by_name(self.check(), "Cemu paths")
        self.assertEqual(result.status, WARN)
        self.assertIn(f"move the saves in {self.default / 'usr/save'}", result.fix)
        self.assertIn(f"{self.mlc}/usr/save", result.fix)
        self.assertNotIn("with Cemu closed;", result.fix)

    def test_the_folders_cemu_creates_on_every_start_keep_the_usual_fix(self) -> None:
        for title in ("1004a000", "1004a100", "1004a200"):
            (self.default / f"usr/save/00050010/{title}/user/common/db").mkdir(parents=True)
        result = by_name(self.check(), "Cemu paths")
        self.assertEqual(result.fix, "run `warpzone provision device deck` with Cemu closed")


if __name__ == "__main__":
    unittest.main()
