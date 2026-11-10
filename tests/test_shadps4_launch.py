"""shadPS4 launch script: its text run under bash, the find-rules edit, provisioning and doctor."""

import dataclasses
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import test_shadps4
from test_doctor import by_name
from test_shadps4 import PROFILE
from warpzone import settings_state, shadps4
from warpzone.doctor import FAIL, OK, WARN, judge_shadps4_version
from warpzone.profiles import ProfileError, parse_profile

RULES = """<?xml version="1.0"?>
<!-- This is the ES-DE find rules configuration file for Unix -->
<ruleList>
    <emulator name="RETROARCH">
        <rule type="staticpath">
            <entry>/usr/bin/retroarch</entry>
        </rule>
    </emulator>
    <emulator name="SHADPS4">
        <!-- PS4 emulator ShadPS4 -->
        <rule type="staticpath">
            <entry>/home/deck/Emulation/tools/launchers/shadps4.sh</entry>
        </rule>
    </emulator>
    <emulator name="YUZU">
        <rule type="staticpath">
            <entry>/home/deck/Emulation/tools/launchers/yuzu.sh</entry>
        </rule>
    </emulator>
</ruleList>
"""
SCRIPT = "/home/deck/.local/share/warpzone/shadps4-launch.sh"


class FindRuleTests(unittest.TestCase):
    def test_only_the_shadps4_entry_changes(self) -> None:
        new = shadps4.point_find_rule(RULES, "SHADPS4", SCRIPT)
        self.assertEqual(
            new,
            RULES.replace("/home/deck/Emulation/tools/launchers/shadps4.sh", SCRIPT),
        )
        self.assertEqual(shadps4.find_rule_target(new, "SHADPS4"), SCRIPT)
        self.assertEqual(shadps4.point_find_rule(new, "SHADPS4", SCRIPT), new)

    def test_xml_special_characters_are_escaped(self) -> None:
        new = shadps4.point_find_rule(RULES, "SHADPS4", "/a&b/x.sh")
        self.assertIn("<entry>/a&amp;b/x.sh</entry>", new)
        self.assertEqual(shadps4.find_rule_target(new, "SHADPS4"), "/a&b/x.sh")

    def test_a_file_it_does_not_understand_is_refused(self) -> None:
        for text in ("not xml", "<ruleList/>", RULES.replace("SHADPS4", "OTHER")):
            with self.assertRaises(shadps4.Shadps4ConfigError, msg=text):
                shadps4.point_find_rule(text, "SHADPS4", SCRIPT)


class ScriptTests(unittest.TestCase):
    """The script text, run by a real bash against fixture files."""

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        self.ini = self.tmp / "qt_ui.ini"
        self.versions = self.tmp / "versions.json"
        self.script = self.tmp / "launch.sh"
        self.script.write_text(shadps4.render_launch_script(str(self.ini), str(self.versions)))
        self.eboot = self.tmp / "games dir" / "CUSA1" / "eboot.bin"

    def emulator(self, folder: str, *, name: str = "Shadps4-sdl.AppImage") -> Path:
        """A fake emulator that records its arguments."""
        path = self.tmp / folder / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('#!/bin/bash\nprintf "%s\\n" "$@" > "$(dirname "$0")/args"\n')
        path.chmod(0o755)
        return path

    def desktop(self, exec_line: str | None = None) -> Path:
        path = self.tmp / "Game.desktop"
        line = exec_line if exec_line is not None else f'Exec=shadps4 "{self.eboot}"'
        path.write_text(f"[Desktop Entry]\nType=Application\nName=A Game\n{line}\n")
        return path

    def run_script(self, arg: str | Path | None, **env: str) -> subprocess.CompletedProcess[str]:
        args = ["bash", str(self.script)] + ([str(arg)] if arg is not None else [])
        base = {"PATH": os.environ["PATH"], "HOME": str(self.tmp / "home")}
        return subprocess.run(args, capture_output=True, text=True, env={**base, **env})

    def fake_tool(self, name: str, *, extra: str = "") -> Path:
        """A fake dialog tool that records its arguments; returns the folder it is in."""
        folder = self.tmp / "bin"
        folder.mkdir(exist_ok=True)
        tool = folder / name
        tool.write_text(f'#!/bin/sh\nprintf "%s\\n" "$@" > "{folder}/{name}.args"\n{extra}')
        tool.chmod(0o755)
        return folder

    def tool_args(self, name: str) -> list[str]:
        return (self.tmp / "bin" / f"{name}.args").read_text().splitlines()

    def called(self, name: str) -> bool:
        return (self.tmp / "bin" / f"{name}.args").exists()

    def log(self, state: str | None = None) -> Path:
        base = Path(state) if state else self.tmp / "home" / ".local" / "state"
        return base / "warpzone" / shadps4.LAUNCH_LOG

    def args(self, folder: str) -> list[str]:
        return (self.tmp / folder / "args").read_text().splitlines()

    def test_the_selected_appimage_runs_with_the_eboot(self) -> None:
        emu = self.emulator("v 1 - Name - 2026")
        self.ini.write_text(f"[main_window]\nx=1\n[version_manager]\nversionSelected={emu}\n")
        done = self.run_script(self.desktop())
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(self.args("v 1 - Name - 2026"), ["-f", "true", "-g", str(self.eboot)])

    def test_a_selected_folder_runs_its_sdl_appimage(self) -> None:
        emu = self.emulator("v2")
        self.ini.write_text(f"[version_manager]\nversionSelected={emu.parent}\n")
        self.assertEqual(self.run_script(self.desktop()).returncode, 0)
        self.assertEqual(self.args("v2"), ["-f", "true", "-g", str(self.eboot)])

    def test_the_first_listed_version_is_used_when_none_is_selected(self) -> None:
        emu = self.emulator("first")
        self.emulator("second")
        self.ini.write_text("[version_manager]\nversionSelected=\n")
        self.versions.write_text(
            f'[\n  {{\n    "name": "a",\n    "path": "{emu}",\n    "type": 0\n  }},\n'
            f'  {{\n    "path": "{self.tmp}/second/Shadps4-sdl.AppImage"\n  }}\n]\n'
        )
        self.assertEqual(self.run_script(self.desktop()).returncode, 0)
        self.assertEqual(self.args("first"), ["-f", "true", "-g", str(self.eboot)])

    def test_a_selection_that_is_gone_falls_back_to_the_list(self) -> None:
        emu = self.emulator("kept")
        self.ini.write_text(f"[version_manager]\nversionSelected={self.tmp}/gone/x.AppImage\n")
        self.versions.write_text(f'[{{"path": "{emu}"}}]')
        self.assertEqual(self.run_script(self.desktop()).returncode, 0)
        self.assertEqual(self.args("kept"), ["-f", "true", "-g", str(self.eboot)])

    def test_no_version_is_a_clear_error(self) -> None:
        done = self.run_script(self.desktop())
        self.assertNotEqual(done.returncode, 0)
        self.assertIn("no shadPS4 version found", done.stderr)
        self.assertIn("Version Manager", done.stderr)

    def test_the_error_is_appended_to_the_log(self) -> None:
        self.run_script(self.desktop())
        self.run_script(self.desktop())
        lines = self.log().read_text().splitlines()
        self.assertEqual(len(lines), 2)
        self.assertRegex(lines[0], r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d[+-]\d{4} no shadPS4 version")

    def test_the_log_follows_an_absolute_xdg_state_home(self) -> None:
        state = str(self.tmp / "xdg")
        self.run_script(self.desktop(), XDG_STATE_HOME=state)
        self.assertIn("no shadPS4 version", self.log(state).read_text())
        self.assertFalse(self.log().exists())

    def test_a_log_that_cannot_be_written_does_not_hide_the_error(self) -> None:
        (self.tmp / "home").write_text("a file, so the state folder cannot be made")
        done = self.run_script(self.desktop())
        self.assertEqual(done.returncode, 1)
        self.assertIn("no shadPS4 version found", done.stderr)

    def test_zenity_shows_the_error_in_a_graphical_session(self) -> None:
        bin_dir = self.fake_tool("zenity")
        self.fake_tool("kdialog")
        done = self.run_script(self.desktop(), DISPLAY=":0", PATH=f"{bin_dir}:{os.environ['PATH']}")
        self.assertEqual(done.returncode, 1)
        args = self.tool_args("zenity")
        self.assertIn("--error", args)
        self.assertTrue(any(a.startswith("--timeout=") for a in args), args)
        self.assertTrue(any("Version Manager" in a for a in args), args)
        self.assertFalse(self.called("kdialog"))

    def test_kdialog_then_notify_send_stand_in_for_zenity(self) -> None:
        bin_dir = self.fake_tool("kdialog")
        self.fake_tool("notify-send")
        path = f"{bin_dir}:/usr/bin:/bin"
        self.run_script(self.desktop(), WAYLAND_DISPLAY="wayland-0", PATH=path)
        self.assertIn("--error", self.tool_args("kdialog"))
        self.assertFalse(self.called("notify-send"))
        (bin_dir / "kdialog").unlink()
        self.run_script(self.desktop(), DISPLAY=":0", PATH=path)
        self.assertIn("critical", self.tool_args("notify-send"))

    def test_no_display_or_no_tool_still_exits_with_the_error(self) -> None:
        bin_dir = self.fake_tool("zenity")
        no_display = self.run_script(self.desktop(), PATH=f"{bin_dir}:{os.environ['PATH']}")
        self.assertEqual(no_display.returncode, 1)
        self.assertFalse(self.called("zenity"))
        no_tool = self.run_script(self.desktop(), DISPLAY=":0", PATH="/usr/bin:/bin")
        self.assertEqual(no_tool.returncode, 1)
        self.assertIn("no shadPS4 version found", no_tool.stderr)

    def test_a_dialog_that_hangs_is_cut_short(self) -> None:
        if not any((Path(d) / "timeout").exists() for d in os.environ["PATH"].split(":")):
            self.skipTest("no timeout(1) here")
        bin_dir = self.fake_tool("kdialog", extra="sleep 300\n")
        self.script.write_text(self.script.read_text().replace("timeout 60", "timeout 1"))
        done = self.run_script(self.desktop(), DISPLAY=":0", PATH=f"{bin_dir}:{os.environ['PATH']}")
        self.assertEqual(done.returncode, 1)

    def test_a_good_launch_writes_no_log_and_shows_nothing(self) -> None:
        emu = self.emulator("ok")
        self.ini.write_text(f"[version_manager]\nversionSelected={emu}\n")
        bin_dir = self.fake_tool("zenity")
        done = self.run_script(self.desktop(), DISPLAY=":0", PATH=f"{bin_dir}:{os.environ['PATH']}")
        self.assertEqual(done.returncode, 0)
        self.assertFalse(self.log().exists())
        self.assertFalse(self.called("zenity"))

    def test_a_shortcut_without_exec_is_an_error(self) -> None:
        emu = self.emulator("v")
        self.ini.write_text(f"[version_manager]\nversionSelected={emu}\n")
        done = self.run_script(self.desktop("Name=x"))
        self.assertNotEqual(done.returncode, 0)
        self.assertIn("Exec=", done.stderr)
        self.assertNotEqual(self.run_script(None).returncode, 0)
        self.assertFalse((self.tmp / "v" / "args").exists())


class JudgeVersionTests(unittest.TestCase):
    """The doctor verdict on the emulator version, without a filesystem."""

    def judge(self, emulator, games):
        return judge_shadps4_version(
            emulator=emulator,
            games=games,
            looked_in=["/q/qt_ui.ini", "/q/versions.json"],
            log="/s/l.log",
        )

    def test_a_version_is_ok_with_or_without_games(self) -> None:
        for games in (False, True):
            status, detail, fix = self.judge("/q/v/Shadps4-sdl.AppImage", games)
            self.assertEqual((status, fix), (OK, ""))
            self.assertIn("/q/v/Shadps4-sdl.AppImage", detail)

    def test_no_version_and_no_games_is_a_warning_with_the_fix(self) -> None:
        status, detail, fix = self.judge(None, False)
        self.assertEqual(status, WARN)
        self.assertIn("/q/qt_ui.ini and /q/versions.json", detail)
        self.assertIn("Version Manager", fix)
        self.assertIn("download and select a version", fix)
        self.assertIn("/s/l.log", fix)

    def test_no_version_with_games_is_a_problem(self) -> None:
        status, detail, fix = self.judge(None, True)
        self.assertEqual(status, FAIL)
        self.assertIn("PS4 games do not start", detail)
        self.assertIn("Version Manager", fix)


class SelectVersionTests(unittest.TestCase):
    @staticmethod
    def resolve(entry: str) -> str | None:
        return entry if entry.startswith("/ok/") else None

    def test_the_selection_wins_over_the_list(self) -> None:
        got = shadps4.select_version(
            "[version_manager]\nversionSelected=/ok/a\n", '[{"path": "/ok/b"}]', self.resolve
        )
        self.assertEqual(got, "/ok/a")

    def test_a_selection_that_does_not_resolve_falls_back_to_the_first_that_does(self) -> None:
        text = '[{"path": "/no/x"}, {"name": "n", "path": "/ok/b"}, {"path": "/ok/c"}]'
        got = shadps4.select_version("versionSelected=/no/gone\n", text, self.resolve)
        self.assertEqual(got, "/ok/b")

    def test_quotes_and_carriage_returns_are_dropped(self) -> None:
        got = shadps4.select_version('versionSelected="/ok/q"\r\n', None, self.resolve)
        self.assertEqual(got, "/ok/q")

    def test_nothing_is_none(self) -> None:
        for ini, versions in ((None, None), ("", "[]"), ("versionSelected=\n", "not json")):
            self.assertIsNone(shadps4.select_version(ini, versions, self.resolve))


class ProfileTests(unittest.TestCase):
    def test_the_shipped_profile_declares_the_launcher(self) -> None:
        rule = PROFILE.shadps4
        assert rule is not None and rule.launch is not None
        self.assertEqual(rule.launch.emulator, "SHADPS4")
        self.assertEqual(rule.launch.script, "~/.local/share/warpzone/shadps4-launch.sh")
        self.assertTrue(rule.launch.find_rules.endswith("custom_systems/es_find_rules.xml"))

    def test_the_table_is_checked(self) -> None:
        good = {
            "qt_ini": "~/q.ini",
            "versions": "~/v.json",
            "script": "~/s.sh",
            "find_rules": "~/r.xml",
            "emulator": "SHADPS4",
        }
        base = {"config": ["~/a.json"], "process": ["p"], "install_dirs": ["/g"]}

        def parse(**launch):
            return parse_profile({"name": "x", "shadps4": {**base, "launch": launch}}, "t")

        rule = parse(**good).shadps4
        assert rule is not None and rule.launch is not None
        self.assertEqual(rule.launch.script, "~/s.sh")
        for change, message in (
            ({"script": ""}, "shadps4.launch.script"),
            ({"emulator": 3}, "shadps4.launch.emulator"),
            ({"extra": 1}, "unknown key"),
        ):
            with self.assertRaisesRegex(ProfileError, message):
                parse(**{**good, **change})
        with self.assertRaisesRegex(ProfileError, "shadps4.launch.find_rules"):
            parse(**{k: v for k, v in good.items() if k != "find_rules"})


class LaunchDevice(test_shadps4.Device):
    def setUp(self) -> None:
        super().setUp()
        self.rules = self.home / "ES-DE/custom_systems/es_find_rules.xml"
        self.rules.parent.mkdir(parents=True)
        self.rules.write_text(RULES)
        self.script_path = self.home / ".local/share/warpzone/shadps4-launch.sh"

    def backups_of_rules(self) -> list[Path]:
        folder = self.state / "backups" / "shadps4"
        return [p for p in folder.rglob("*") if p.is_file() and "es_find_rules" in p.name]


class ProvisionTests(LaunchDevice, unittest.TestCase):
    def test_the_script_is_installed_and_the_rule_points_at_it(self) -> None:
        self.provision()
        self.assertTrue(os.access(self.script_path, os.X_OK))
        self.assertIn(
            str(self.home / ".local/share/shadPS4QtLauncher/qt_ui.ini"),
            self.script_path.read_text(),
        )
        self.assertEqual(
            self.rules.read_text(),
            RULES.replace("/home/deck/Emulation/tools/launchers/shadps4.sh", str(self.script_path)),
        )
        self.assertEqual(len(self.backups_of_rules()), 1)

    def test_a_second_run_changes_and_backs_up_nothing(self) -> None:
        self.provision()
        text, mtime = self.rules.read_text(), self.script_path.stat().st_mtime_ns
        self.provision()
        self.assertEqual(self.rules.read_text(), text)
        self.assertEqual(self.script_path.stat().st_mtime_ns, mtime)
        self.assertEqual(len(self.backups_of_rules()), 1)

    def test_the_rules_are_left_alone_while_es_de_runs(self) -> None:
        self.start("es-de")
        rep, _ = self.provision()
        self.assertEqual(self.rules.read_text(), RULES)
        self.assertTrue(any("ES-DE is running" in w for w in rep.warnings), rep.warnings)

    def test_es_de_started_during_the_backup_stops_the_edit(self) -> None:
        real = settings_state.backup

        def backup(path, *args, **kw):
            result = real(path, *args, **kw)
            if path == self.rules:
                self.start("es-de")
            return result

        with mock.patch.object(settings_state, "backup", backup):
            rep, _ = self.provision()
        self.assertEqual(self.rules.read_text(), RULES)
        self.assertEqual(len(self.backups_of_rules()), 1)
        self.assertTrue(any("ES-DE is running" in w for w in rep.warnings), rep.warnings)

    def test_rules_edited_during_the_backup_are_left_alone(self) -> None:
        edited = RULES + "<!-- by hand -->\n"
        real = settings_state.backup

        def backup(path, *args, **kw):
            result = real(path, *args, **kw)
            if path == self.rules:
                path.write_text(edited)
            return result

        with mock.patch.object(settings_state, "backup", backup):
            rep, _ = self.provision()
        self.assertEqual(self.rules.read_text(), edited)
        (warning,) = [w for w in rep.warnings if "changed while" in w]
        self.assertIn(str(self.rules), warning)

    def test_unattended_defers_while_es_de_runs(self) -> None:
        self.start("es-de")
        rep, _ = self.provision(unattended=True)
        self.assertTrue(rep.deferred)
        self.assertEqual(self.rules.read_text(), RULES)

    def test_dry_run_writes_nothing(self) -> None:
        self.provision(dry_run=True)
        self.assertFalse(self.script_path.exists())
        self.assertEqual(self.rules.read_text(), RULES)

    def test_missing_rules_warn_and_are_not_created(self) -> None:
        self.rules.unlink()
        rep, _ = self.provision()
        self.assertFalse(self.rules.exists())
        self.assertTrue(any("es_find_rules.xml" in w for w in rep.warnings), rep.warnings)


class DoctorLaunchTests(LaunchDevice, test_shadps4.DoctorTests):
    def result(self):
        return by_name(self.check(), "shadPS4 launcher")

    def test_a_provisioned_device_is_ok(self) -> None:
        self.provision()
        result = self.result()
        self.assertEqual(result.status, OK, result.detail)

    def test_an_unprovisioned_device_warns_with_the_fix(self) -> None:
        result = self.result()
        self.assertEqual(result.status, WARN)
        self.assertIn("warpzone provision device deck", result.fix)

    def test_a_rule_that_emudeck_put_back_warns(self) -> None:
        self.provision()
        self.rules.write_text(RULES)
        result = self.result()
        self.assertEqual(result.status, WARN)
        self.assertIn("es_find_rules.xml", result.detail)

    def test_a_script_that_is_not_executable_warns(self) -> None:
        self.provision()
        self.script_path.chmod(0o644)
        self.assertEqual(self.result().status, WARN)


class DoctorVersionTests(LaunchDevice, test_shadps4.DoctorTests):
    def setUp(self) -> None:
        super().setUp()
        self.qt = self.home / ".local/share/shadPS4QtLauncher"
        self.qt.mkdir(parents=True)

    def result(self):
        return by_name(self.check(), "shadPS4 version")

    def emulator(self, folder: str) -> Path:
        path = self.qt / "versions" / folder / "Shadps4-sdl.AppImage"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('#!/bin/bash\nprintf "%s\\n" "$@" > "$(dirname "$0")/args"\n')
        path.chmod(0o755)
        return path

    def add_game(self) -> None:
        eboot = Path(self.games) / "CUSA00001" / "eboot.bin"
        eboot.parent.mkdir(parents=True)
        eboot.write_text("x")

    def test_a_selected_version_is_ok(self) -> None:
        emu = self.emulator("v1")
        (self.qt / "qt_ui.ini").write_text(f"[version_manager]\nversionSelected={emu}\n")
        result = self.result()
        self.assertEqual(result.status, OK, result.detail)
        self.assertIn(str(emu), result.detail)

    def test_an_empty_version_list_is_a_warning_without_ps4_games(self) -> None:
        (self.qt / "versions.json").write_text("[]")
        result = self.result()
        self.assertEqual(result.status, WARN)
        self.assertIn("Version Manager", result.fix)
        self.assertIn("shadps4-launch.log", result.fix)

    def test_an_empty_version_list_is_a_problem_with_ps4_games(self) -> None:
        (self.qt / "versions.json").write_text("[]")
        self.add_game()
        result = self.result()
        self.assertEqual(result.status, FAIL)
        self.assertIn("PS4 games do not start", result.detail)

    def test_a_version_that_is_not_executable_does_not_count(self) -> None:
        emu = self.emulator("v1")
        emu.chmod(0o644)
        (self.qt / "versions.json").write_text(f'[{{"path": "{emu}"}}]')
        self.assertEqual(self.result().status, WARN)

    def test_a_device_without_the_launch_table_has_no_version_line(self) -> None:
        assert self.profile.shadps4 is not None
        self.profile = dataclasses.replace(
            self.profile, shadps4=dataclasses.replace(self.profile.shadps4, launch=None)
        )
        self.assertNotIn("shadPS4 version", [r.name for r in self.check()])

    def test_the_script_and_doctor_pick_the_same_emulator(self) -> None:
        """Both read the same fixtures: the AppImage that runs is the one doctor reports."""
        self.provision()
        game = self.home / "Game.desktop"
        game.write_text('[Desktop Entry]\nExec=shadps4 "/games/CUSA1/eboot.bin"\n')
        v1, v2, plain = self.emulator("v1"), self.emulator("v2"), self.emulator("plain")
        plain.chmod(0o644)
        ini, versions = self.qt / "qt_ui.ini", self.qt / "versions.json"

        def listed(*paths: Path) -> str:
            return json.dumps([{"name": "n", "path": str(p)} for p in paths])

        cases = [
            (f"[version_manager]\nversionSelected={v1}\n", "", v1),
            (f"[version_manager]\nversionSelected={v2.parent}\n", "", v2),
            (f"versionSelected={self.qt}/gone\n", listed(v1, v2), v1),
            ("versionSelected=\n", listed(plain, v2), v2),
            (f'versionSelected="{v1}"\r\n', "", v1),
            ("versionSelected=\n", "[]", None),
            (f"versionSelected={plain}\n", listed(plain), None),
            ("", "", None),
        ]
        env = {"PATH": os.environ["PATH"], "HOME": str(self.home)}
        for ini_text, versions_text, want in cases:
            with self.subTest(ini=ini_text, versions=versions_text):
                ini.write_text(ini_text, newline="")
                versions.write_text(versions_text)
                for args in self.qt.glob("versions/*/args"):
                    args.unlink()
                done = subprocess.run(
                    ["bash", str(self.script_path), str(game)],
                    capture_output=True,
                    text=True,
                    env=env,
                )
                ran = [a.parent / "Shadps4-sdl.AppImage" for a in self.qt.glob("versions/*/args")]
                result = self.result()
                if want is None:
                    self.assertNotEqual(done.returncode, 0)
                    self.assertEqual(ran, [])
                    self.assertNotEqual(result.status, OK)
                else:
                    self.assertEqual(done.returncode, 0, done.stderr)
                    self.assertEqual(ran, [want])
                    self.assertEqual(result.status, OK)
                    self.assertIn(str(want), result.detail)


if __name__ == "__main__":
    unittest.main()
