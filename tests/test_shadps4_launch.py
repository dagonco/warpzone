"""shadPS4 launch script: its text run under bash, the find-rules edit, provisioning and doctor."""

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
from warpzone.doctor import OK, WARN
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

    def run_script(self, arg: str | Path | None) -> subprocess.CompletedProcess[str]:
        args = ["bash", str(self.script)] + ([str(arg)] if arg is not None else [])
        return subprocess.run(
            args, capture_output=True, text=True, env={"PATH": os.environ["PATH"]}
        )

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
        self.assertIn("open the shadPS4 launcher once", done.stderr)

    def test_a_shortcut_without_exec_is_an_error(self) -> None:
        emu = self.emulator("v")
        self.ini.write_text(f"[version_manager]\nversionSelected={emu}\n")
        done = self.run_script(self.desktop("Name=x"))
        self.assertNotEqual(done.returncode, 0)
        self.assertIn("Exec=", done.stderr)
        self.assertNotEqual(self.run_script(None).returncode, 0)
        self.assertFalse((self.tmp / "v" / "args").exists())


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


if __name__ == "__main__":
    unittest.main()
