"""The ArkOS Options menu entries: the script text, the profile table and the provisioning step."""

import dataclasses
import os
import shutil
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path

from support import stub_self_update
from test_anbernic import PROFILE, Device
from warpzone import options_menu
from warpzone.profiles import ProfileError, load_profile, parse_profile
from warpzone.provision_scripts import ensure_options_menu
from warpzone.report import Reporter

BASH = shutil.which("bash")
setUpModule = stub_self_update


class RenderTests(unittest.TestCase):
    def test_every_entry_is_a_bash_script_with_the_marker(self) -> None:
        for name, text in options_menu.entries().items():
            with self.subTest(name=name):
                self.assertTrue(name.endswith(".sh"))
                self.assertTrue(text.startswith("#!/bin/bash\n"))
                self.assertEqual(text.splitlines()[1], options_menu.MARKER)

    def test_the_status_reads_only_local_files_and_never_stops_on_an_error(self) -> None:
        text = options_menu.render_status_script()
        self.assertNotIn("set -e", text)
        self.assertNotIn("curl", text)
        self.assertNotIn("self-update", text)
        for needle in ("venv/bin/warpzone", "/conf", "/state", "last_fetch", "good_tag"):
            self.assertIn(needle, text)

    def test_the_update_entry_runs_self_update_now(self) -> None:
        self.assertIn('"$WZ" self-update --now', options_menu.render_update_script())

    def test_rendering_is_deterministic(self) -> None:
        self.assertEqual(options_menu.entries(), options_menu.entries())


class ProfileTests(unittest.TestCase):
    def test_only_the_anbernic_profile_has_the_table(self) -> None:
        self.assertEqual(PROFILE.options_menu.dir, "/opt/system")
        self.assertIsNone(load_profile("steamos-emudeck").options_menu)

    def test_the_table_is_validated(self) -> None:
        for bad in ({"dir": "relative"}, {"dir": 3}, {}, {"dir": "/x", "other": 1}, "text"):
            with self.subTest(bad=bad), self.assertRaises(ProfileError):
                parse_profile({"name": "x", "options_menu": bad}, "t")
        ok = parse_profile({"name": "x", "options_menu": {"dir": "~/menu"}}, "t")
        self.assertEqual(ok.options_menu.dir, "~/menu")


class ProvisionTests(Device, unittest.TestCase):
    def install(self, *, dry_run=False):
        lines: list[str] = []
        rep = Reporter(out=lines.append)
        ensure_options_menu(self.profile, self.home, rep, dry_run=dry_run)
        return rep, lines

    def test_writes_executable_scripts_and_nothing_else(self) -> None:
        other = self.options_dir / "Network Info.sh"
        other.write_text("echo hi\n")
        before = other.stat().st_mtime_ns
        rep, _ = self.install()
        self.assertEqual(rep.warnings, [])
        self.assertEqual(
            sorted(p.name for p in self.options_dir.iterdir()),
            sorted([*options_menu.entries(), "Network Info.sh"]),
        )
        for name, text in options_menu.entries().items():
            path = self.options_dir / name
            self.assertEqual(path.read_text(), text)
            self.assertTrue(path.stat().st_mode & stat.S_IXUSR)
        self.assertEqual(other.stat().st_mtime_ns, before)
        self.assertEqual(other.read_text(), "echo hi\n")

    def test_a_second_run_is_unchanged(self) -> None:
        self.install()
        path = self.options_dir / options_menu.STATUS_NAME
        before = path.stat().st_mtime_ns
        rep, lines = self.install()
        self.assertEqual(path.stat().st_mtime_ns, before)
        self.assertEqual(rep.warnings, [])
        self.assertEqual(len([line for line in lines if "unchanged" in line]), 2)

    def test_a_changed_script_is_rewritten(self) -> None:
        self.install()
        path = self.options_dir / options_menu.STATUS_NAME
        path.write_text(options_menu.MARKER + "\nold\n")
        _, lines = self.install()
        self.assertEqual(path.read_text(), options_menu.render_status_script())
        self.assertTrue(any("wrote" in line for line in lines))

    def test_a_script_that_lost_its_executable_bit_gets_it_back(self) -> None:
        self.install()
        path = self.options_dir / options_menu.STATUS_NAME
        path.chmod(0o644)
        self.install()
        self.assertTrue(os.access(path, os.X_OK))

    def test_a_missing_folder_is_skipped_with_a_warning_and_not_created(self) -> None:
        shutil.rmtree(self.options_dir)
        rep, _ = self.install()
        self.assertFalse(self.options_dir.exists())
        (warning,) = rep.warnings
        self.assertIn("does not exist", warning)

    def test_a_file_that_is_not_ours_is_left_alone(self) -> None:
        mine = self.options_dir / options_menu.STATUS_NAME
        mine.write_text("#!/bin/bash\necho mine\n")
        rep, _ = self.install()
        self.assertEqual(mine.read_text(), "#!/bin/bash\necho mine\n")
        self.assertEqual(len(rep.warnings), 1)
        self.assertTrue((self.options_dir / options_menu.UPDATE_NAME).exists())

    def test_a_dry_run_writes_nothing(self) -> None:
        _, lines = self.install(dry_run=True)
        self.assertEqual(list(self.options_dir.iterdir()), [])
        self.assertTrue(any("would write" in line for line in lines))

    def test_a_profile_without_the_table_does_nothing(self) -> None:
        profile = dataclasses.replace(self.profile, options_menu=None)
        ensure_options_menu(profile, self.home, Reporter(out=lambda _: None), dry_run=False)
        self.assertEqual(list(self.options_dir.iterdir()), [])

    def test_provision_device_installs_the_entries(self) -> None:
        rep, lines = self.provision()
        self.assertEqual(rep.warnings, [])
        self.assertTrue((self.options_dir / options_menu.STATUS_NAME).is_file())
        self.assertTrue(any("options menu: wrote" in line for line in lines))

    def test_provision_device_for_a_profile_without_the_table_writes_nothing(self) -> None:
        self.profile = dataclasses.replace(self.profile, options_menu=None)
        self.provision()
        self.assertEqual(list(self.options_dir.iterdir()), [])


@unittest.skipUnless(BASH, "bash is not installed")
class ScriptRunTests(unittest.TestCase):
    """Run the generated scripts under bash with a fake HOME and a fake `msgbox`."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        self.home = self.tmp / "home"
        self.bin = self.tmp / "bin"
        self.bin.mkdir()
        self.updater = self.home / ".local/state/warpzone/updater"
        self.venv_bin = self.home / ".local/share/warpzone/venv/bin"

    def fake(self, path: Path, body: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("#!/bin/sh\n" + body)
        path.chmod(0o755)

    def run_script(self, text: str, *, msgbox: bool = True) -> str:
        script = self.tmp / "script.sh"
        script.write_text(text)
        out = self.tmp / "msgbox.out"
        if msgbox:
            self.fake(self.bin / "msgbox", f'printf "%s" "$1" > "{out}"\n')
        env = {
            "HOME": str(self.home),
            "PATH": f"{self.bin}{os.pathsep}{os.environ['PATH']}",
            "TZ": "UTC",
        }
        proc = subprocess.run(
            [BASH, str(script)], env=env, capture_output=True, text=True, timeout=60
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        return out.read_text() if msgbox else proc.stdout

    def install_state(self, *, doctor: str) -> None:
        self.updater.mkdir(parents=True)
        (self.updater / "conf").write_text("device=ark\nchannel=rc\npin=v0.3.0rc2\n")
        (self.updater / "state").write_text(
            "good_tag=v0.3.0rc2\nlast_result=ok\nlast_fetch=1793788800\nattempts=0\n"
        )
        self.fake(
            self.venv_bin / "warpzone",
            'case "$1" in\n--version) echo "warpzone 0.3.0rc2" ;;\n'
            f"doctor) cat <<'EOF'\n{doctor}\nEOF\n;;\nself-update) echo one; echo two; exit 3 ;;\n"
            "esac\n",
        )

    def test_the_status_shows_what_the_updater_recorded(self) -> None:
        self.install_state(
            doctor="warpzone doctor\n\nBase\n  [ok] python 3.11\n"
            "  [!!] syncthing  not found, the folders cannot sync at all today\n"
            "       fix: install it\n  [! ] disk  little free space\n  [--] flatpak  not set up\n"
        )
        shown = self.run_script(options_menu.render_status_script())
        self.assertEqual(
            shown.splitlines(),
            [
                "Version: warpzone 0.3.0rc2",
                "Channel: rc",
                "Pin: v0.3.0rc2",
                "Good tag: v0.3.0rc2",
                "New release: none",
                "Last result: ok",
                "Last fetch: 2026-11-04 10:40",
                "Doctor: [!!] syncthing  not found, the folders c",
                "[! ] disk  little free space",
            ],
        )

    def test_a_clean_doctor_and_no_pin(self) -> None:
        self.install_state(doctor="Base\n  [ok] python 3.11\n  [--] flatpak  not set up\n")
        (self.updater / "conf").write_text("channel=stable\npin=\n")
        shown = self.run_script(options_menu.render_status_script())
        self.assertIn("Pin: none", shown)
        self.assertTrue(shown.endswith("Doctor: no problems"))

    def test_at_most_eight_doctor_lines_each_cut_to_forty_characters(self) -> None:
        lines = "\n".join(f"  [!!] check{i} " + "x" * 80 for i in range(12))
        self.install_state(doctor=lines)
        shown = self.run_script(options_menu.render_status_script()).splitlines()
        doctor = shown[shown.index(next(s for s in shown if s.startswith("Doctor:"))) :]
        self.assertEqual(len(doctor), 9)  # eight problems (the first on the Doctor line) + more
        self.assertEqual(doctor[-1], "+4 more")
        self.assertTrue(all(len(s) <= 40 + len("Doctor: ") for s in doctor))

    def test_nothing_installed_shows_unknown_and_still_succeeds(self) -> None:
        shown = self.run_script(options_menu.render_status_script())
        for key in ("Version", "Channel", "Good tag", "Last result", "Last fetch", "Doctor"):
            self.assertIn(f"{key}: unknown", shown)
        self.assertIn("Pin: none", shown)

    def test_a_release_the_timer_found_is_shown(self) -> None:
        self.install_state(doctor="")
        with (self.updater / "state").open("a") as f:
            f.write("available=v0.3.0rc4\nlast_result=available\n")
        shown = self.run_script(options_menu.render_status_script())
        self.assertIn("New release: v0.3.0rc4 (Warpzone Update Now)", shown)
        self.assertIn("Last result: available", shown)

    def test_a_bad_last_fetch_shows_unknown(self) -> None:
        self.install_state(doctor="")
        (self.updater / "state").write_text("last_fetch=soon\n")
        self.assertIn("Last fetch: unknown", self.run_script(options_menu.render_status_script()))

    @unittest.skipIf(os.path.exists("/usr/bin/msgbox"), "this machine has a real msgbox")
    def test_without_msgbox_the_text_goes_to_standard_output(self) -> None:
        self.install_state(doctor="")
        printed = self.run_script(options_menu.render_status_script(), msgbox=False)
        self.assertIn("Version: warpzone 0.3.0rc2", printed)

    def test_update_now_shows_the_last_lines_and_the_exit_code(self) -> None:
        self.install_state(doctor="")
        shown = self.run_script(options_menu.render_update_script())
        self.assertEqual(shown, "Update check finished (exit 3)\none\ntwo")

    def test_update_now_without_warpzone_says_so(self) -> None:
        shown = self.run_script(options_menu.render_update_script())
        self.assertIn("not installed", shown)


if __name__ == "__main__":
    unittest.main()
