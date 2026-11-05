"""The ArkOS Options menu entries: the script text, the profile table and the provisioning step."""

import dataclasses
import os
import re
import shutil
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path

from support import stub_self_update
from test_anbernic import PROFILE, Device
from warpzone import options_menu, selfupdate
from warpzone.profiles import ProfileError, load_profile, parse_profile
from warpzone.provision_scripts import ensure_options_menu
from warpzone.report import Reporter

BASH = shutil.which("bash")
UPDATER = Path(__file__).resolve().parent.parent / "warpzone" / "data" / "updater.sh"
setUpModule = stub_self_update


class RenderTests(unittest.TestCase):
    def test_every_entry_is_a_bash_script_with_the_marker(self) -> None:
        for name, text in options_menu.entries().items():
            with self.subTest(name=name):
                self.assertTrue(name.endswith(".sh"))
                self.assertTrue(text.startswith("#!/bin/bash\n"))
                self.assertEqual(text.splitlines()[1], options_menu.MARKER)

    def test_there_is_one_entry(self) -> None:
        self.assertEqual(list(options_menu.entries()), ["Warpzone.sh"])

    def test_the_script_reads_local_files_and_never_stops_on_an_error(self) -> None:
        text = options_menu.render_script()
        self.assertNotIn("set -e", text)
        self.assertNotIn("curl", text)
        for needle in ("venv/bin/warpzone", "/conf", "/state", "last_fetch", "good_tag"):
            self.assertIn(needle, text)
        self.assertIn("tick --now", text)
        self.assertIn('self-update --channel "$OTHER"', text)

    def test_rendering_is_deterministic(self) -> None:
        self.assertEqual(options_menu.entries(), options_menu.entries())

    def test_the_live_updater_runs_from_the_abi_that_knows_now(self) -> None:
        (pattern,) = re.findall(r"grep -Eq '([^']*)' \"\$LIVE\"", options_menu.render_script())
        shipped = selfupdate.updater_abi(UPDATER.read_text())
        self.assertIsNotNone(shipped)
        with tempfile.TemporaryDirectory() as tmp:
            for abi, taken in (
                (selfupdate.NOW_ABI - 1, False),
                (selfupdate.NOW_ABI, True),
                (shipped, True),
                (12, True),
            ):
                with self.subTest(abi=abi):
                    live = Path(tmp) / "updater.sh"
                    live.write_text(f"#!/bin/sh\nUPDATER_ABI={abi}\n")
                    found = subprocess.run(["grep", "-Eq", pattern, str(live)], check=False)
                    self.assertEqual(found.returncode == 0, taken)

    def test_shellcheck_is_happy_when_it_is_installed(self) -> None:
        shellcheck = shutil.which("shellcheck")
        if shellcheck is None:
            self.skipTest("shellcheck is not installed")
        with tempfile.TemporaryDirectory() as tmp:
            script = Path(tmp) / options_menu.ENTRY_NAME
            script.write_text(options_menu.render_script())
            proc = subprocess.run(
                [shellcheck, "-s", "bash", str(script)],
                capture_output=True,
                text=True,
                check=False,
            )
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)


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
        path = self.options_dir / options_menu.ENTRY_NAME
        before = path.stat().st_mtime_ns
        rep, lines = self.install()
        self.assertEqual(path.stat().st_mtime_ns, before)
        self.assertEqual(rep.warnings, [])
        self.assertEqual(len([line for line in lines if "unchanged" in line]), 1)

    def test_a_changed_script_is_rewritten(self) -> None:
        self.install()
        path = self.options_dir / options_menu.ENTRY_NAME
        path.write_text(options_menu.MARKER + "\nold\n")
        _, lines = self.install()
        self.assertEqual(path.read_text(), options_menu.render_script())
        self.assertTrue(any("wrote" in line for line in lines))

    def test_a_script_that_lost_its_executable_bit_gets_it_back(self) -> None:
        self.install()
        path = self.options_dir / options_menu.ENTRY_NAME
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
        mine = self.options_dir / options_menu.ENTRY_NAME
        mine.write_text("#!/bin/bash\necho mine\n")
        rep, _ = self.install()
        self.assertEqual(mine.read_text(), "#!/bin/bash\necho mine\n")
        self.assertEqual(len(rep.warnings), 1)

    def test_the_old_entries_stay_when_the_new_one_could_not_be_written(self) -> None:
        old = self.options_dir / options_menu.OLD_NAMES[0]
        old.write_text("#!/bin/bash\n" + options_menu.MARKER + "\nold\n")
        (self.options_dir / options_menu.ENTRY_NAME).write_text("#!/bin/bash\necho mine\n")
        rep, _ = self.install()
        self.assertTrue(old.exists(), "no warpzone entry would be left")
        self.assertEqual(len(rep.warnings), 1)

    def test_the_entries_of_earlier_versions_are_removed_when_they_are_ours(self) -> None:
        ours, theirs = (self.options_dir / n for n in options_menu.OLD_NAMES)
        ours.write_text("#!/bin/bash\n" + options_menu.MARKER + "\nold\n")
        theirs.write_text("#!/bin/bash\necho mine\n")
        _, lines = self.install(dry_run=True)
        self.assertTrue(ours.exists())
        self.assertTrue(any("would remove" in line for line in lines))
        rep, lines = self.install()
        self.assertFalse(ours.exists())
        self.assertTrue(theirs.exists(), "not a warpzone file: left alone")
        self.assertTrue(any("removed" in line for line in lines))
        self.assertEqual(rep.warnings, [])

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
        self.assertTrue((self.options_dir / options_menu.ENTRY_NAME).is_file())
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

    def run_script(self, text: str, *, msgbox: bool = True, tty: Path | None = None) -> str:
        script = self.tmp / "script.sh"
        script.write_text(text)
        out = self.tmp / "msgbox.out"
        if msgbox:
            self.fake(self.bin / "msgbox", f'printf "%s" "$1" > "{out}"\n')
        env = {
            "HOME": str(self.home),
            "PATH": f"{self.bin}{os.pathsep}{os.environ['PATH']}",
            "TZ": "UTC",
            "WZ_TTY": str(tty or self.tmp / "no-such-dir" / "tty"),
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
            f"doctor) : >>\"{self.tmp}/doctor-ran\"; cat <<'EOF'\n{doctor}\nEOF\n;;\n"
            'self-update) [ "$2" = --channel ] && { echo "self-update: channel $3"; exit 0; }\n'
            "echo one; echo two; exit 3 ;;\n"
            "esac\n",
        )

    def test_the_status_shows_what_the_updater_recorded(self) -> None:
        self.install_state(
            doctor="warpzone doctor\n\nBase\n  [ok] python 3.11\n"
            "  [!!] syncthing  not found, the folders cannot sync at all today\n"
            "       fix: install it\n  [! ] disk  little free space\n  [--] flatpak  not set up\n"
        )
        shown = self.run_script(options_menu.render_script())
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
        shown = self.run_script(options_menu.render_script())
        self.assertIn("Pin: none", shown)
        self.assertTrue(shown.endswith("Doctor: no problems"))

    def test_at_most_eight_doctor_lines_each_cut_to_forty_characters(self) -> None:
        lines = "\n".join(f"  [!!] check{i} " + "x" * 80 for i in range(12))
        self.install_state(doctor=lines)
        shown = self.run_script(options_menu.render_script()).splitlines()
        doctor = shown[shown.index(next(s for s in shown if s.startswith("Doctor:"))) :]
        self.assertEqual(len(doctor), 9)  # eight problems (the first on the Doctor line) + more
        self.assertEqual(doctor[-1], "+4 more")
        self.assertTrue(all(len(s) <= 40 + len("Doctor: ") for s in doctor))

    def test_nothing_installed_shows_unknown_and_still_succeeds(self) -> None:
        shown = self.run_script(options_menu.render_script())
        for key in ("Version", "Channel", "Good tag", "Last result", "Last fetch", "Doctor"):
            self.assertIn(f"{key}: unknown", shown)
        self.assertIn("Pin: none", shown)

    def test_a_release_the_timer_found_is_shown(self) -> None:
        self.install_state(doctor="")
        with (self.updater / "state").open("a") as f:
            f.write("available=v0.3.0rc4\nlast_result=available\n")
        shown = self.run_script(options_menu.render_script())
        self.assertIn("New release: v0.3.0rc4", shown)
        self.assertIn("Last result: available", shown)

    def test_a_bad_last_fetch_shows_unknown(self) -> None:
        self.install_state(doctor="")
        (self.updater / "state").write_text("last_fetch=soon\n")
        self.assertIn("Last fetch: unknown", self.run_script(options_menu.render_script()))

    @unittest.skipIf(os.path.exists("/usr/bin/msgbox"), "this machine has a real msgbox")
    def test_without_msgbox_the_text_goes_to_standard_output(self) -> None:
        self.install_state(doctor="")
        printed = self.run_script(options_menu.render_script(), msgbox=False)
        self.assertIn("Version: warpzone 0.3.0rc2", printed)

    # --- the menu, with a fake `dialog` --------------------------------------------------

    def fake_dialog(self, *answers: str, yes: bool = True) -> Path:
        """A `dialog` that answers --menu with the next of `answers` (`CANCEL` cancels it, and
        no answer left is an empty choice), --yesno with `yes`, and logs every box it shows
        (what --gauge reads on its standard input, and the file of --textbox)."""
        log = self.tmp / "dialog.log"
        log.unlink(missing_ok=True)
        queue = self.tmp / "answers"
        queue.write_text("".join(f"{a}\n" for a in answers))
        self.fake(
            self.bin / "dialog",
            f"""log={log}
queue={queue}
while [ $# -gt 0 ]; do
	case $1 in
	--menu)
		printf 'MENU %s\\n' "$*" >>"$log"
		answer=$(head -n 1 "$queue")
		sed -i.x 1d "$queue"
		[ "$answer" != CANCEL ] || exit 1
		printf '%s\\n' "$answer" >&2
		exit 0
		;;
	--yesno) printf 'YESNO %s\\n' "$2" >>"$log"; exit {0 if yes else 1} ;;
	--msgbox) printf 'BOX %s\\n' "$2" >>"$log"; exit 0 ;;
	--infobox) printf 'INFO %s\\n' "$2" >>"$log"; exit 0 ;;
	--gauge) printf 'GAUGE %s\\n' "$2" >>"$log"; cat >>"$log"; exit 0 ;;
	--textbox) printf 'TEXT\\n' >>"$log"; cat "$2" >>"$log"; exit 0 ;;
	esac
	shift
done
""",
        )
        return log

    def menu(self, *answers: str, yes: bool = True) -> str:
        log = self.fake_dialog(*answers, yes=yes)
        tty = self.tmp / "tty"
        tty.write_text("")
        self.run_script(options_menu.render_script(), msgbox=False, tty=tty)
        return log.read_text()

    def test_the_menu_offers_install_only_when_there_is_a_release(self) -> None:
        self.install_state(doctor="")
        shown = self.menu("exit")
        self.assertIn("MENU", shown)
        self.assertNotIn("install", shown)
        self.assertIn("channel Follow stable releases", shown)
        with (self.updater / "state").open("a") as f:
            f.write("available=v0.3.0rc5\n")
        shown = self.menu("exit")
        self.assertIn("install Install v0.3.0rc5 now", shown)
        self.assertIn("New release: v0.3.0rc5", shown)

    def boxes(self, shown: str) -> list[str]:
        """The kind of each box the menu showed, in order."""
        kinds = ("MENU", "BOX", "YESNO", "INFO", "GAUGE", "TEXT")
        return [line.split(" ")[0] for line in shown.splitlines() if line.split(" ")[0] in kinds]

    def menus(self, shown: str) -> list[str]:
        """The text of each menu shown (its summary spans lines), in order."""
        return [m.split("\nBOX ")[0] for m in shown.split("MENU ")[1:]]

    def test_status_shows_the_status_box_and_comes_back_to_the_menu(self) -> None:
        self.install_state(doctor="  [!!] syncthing  not found")
        shown = self.menu("status", "exit")
        self.assertIn("BOX Version: warpzone 0.3.0rc2", shown)
        self.assertIn("Doctor: see the Doctor item", shown)
        self.assertNotIn("syncthing", shown)
        self.assertEqual(self.boxes(shown), ["MENU", "BOX", "MENU"])
        self.assertFalse((self.tmp / "doctor-ran").exists(), "the menu opens without doctor")

    def test_only_exit_or_cancel_ends_the_menu(self) -> None:
        self.install_state(doctor="")
        self.assertEqual(
            self.boxes(self.menu("status", "status", "exit", "status")),
            [
                "MENU",
                "BOX",
                "MENU",
                "BOX",
                "MENU",
            ],
        )
        self.assertEqual(
            self.boxes(self.menu("status", "CANCEL", "status")),
            [
                "MENU",
                "BOX",
                "MENU",
            ],
        )

    def test_doctor_runs_and_shows_its_whole_output_in_a_box_that_scrolls(self) -> None:
        report = "warpzone doctor\n\nBase\n  [ok] python 3.11\n  [!!] syncthing  not found"
        self.install_state(doctor=report)
        shown = self.menu("doctor", "exit")
        self.assertEqual(self.boxes(shown), ["MENU", "INFO", "TEXT", "MENU"])
        self.assertIn("INFO Running doctor...", shown)
        self.assertIn("TEXT\n" + report + "\n", shown)
        self.assertTrue((self.tmp / "doctor-ran").exists())

    def test_doctor_without_warpzone_says_so(self) -> None:
        shown = self.menu("doctor", "exit")
        self.assertIn("TEXT\nwarpzone is not installed here (unknown)\n", shown)

    def live(self, body: str) -> None:
        with (self.updater / "state").open("a") as f:
            f.write("available=v0.3.0rc5\n")
        live = self.home / ".local/share/warpzone/updater/updater.sh"
        live.parent.mkdir(parents=True)
        live.write_text(body)

    def test_install_runs_the_live_updater_by_hand(self) -> None:
        self.install_state(doctor="")
        self.live('UPDATER_ABI=2\necho "live $*"\nexit 0\n')
        shown = self.menu("install")
        self.assertIn("GAUGE Installing v0.3.0rc5", shown)
        self.assertIn("BOX Update finished (exit 0)\nlive tick --now", shown)

    def test_after_an_install_the_menu_shows_the_new_state(self) -> None:
        self.install_state(doctor="")
        state = self.updater / "state"
        # what a tick that installed the release leaves: no `available` any more
        self.live(f'UPDATER_ABI=3\nsed -i.x "/^available=/d" "{state}"\necho "live $*"\n')
        shown = self.menu("install", "exit")
        self.assertEqual(self.boxes(shown), ["MENU", "GAUGE", "BOX", "MENU"])
        first, second = self.menus(shown)
        self.assertIn("install Install v0.3.0rc5 now", first)
        self.assertNotIn("install", second)

    def test_install_shows_the_phases_of_the_update_on_a_gauge(self) -> None:
        self.install_state(doctor="")
        progress = self.updater / "progress"
        self.live(
            "UPDATER_ABI=3\n"
            f'echo "50 Checking 1/3: imports and version" >"{progress}"; sleep 2\n'
            f'echo "90 Checking 3/3: health" >"{progress}"; sleep 2\n'
            'echo "live $*"\nexit 1\n'
        )
        shown = self.menu("install")
        gauge = shown[shown.index("GAUGE") : shown.index("BOX")]
        self.assertIn("XXX\n50\nChecking 1/3: imports and version\nXXX\n", gauge)
        self.assertIn("XXX\n90\nChecking 3/3: health\nXXX\n", gauge)
        self.assertLess(gauge.index("\n50\n"), gauge.index("\n90\n"))
        self.assertIn("BOX Update finished (exit 1)\nlive tick --now", shown)

    def test_without_a_progress_file_the_gauge_says_working(self) -> None:
        self.install_state(doctor="")
        # what the last tick left is not this update's progress
        (self.updater / "progress").write_text("100 Done\n")
        self.live('UPDATER_ABI=2\nsleep 1\necho "live $*"\n')
        shown = self.menu("install")
        gauge = shown[shown.index("GAUGE") : shown.index("BOX")]
        self.assertIn("XXX\n0\nWorking...\nXXX\n", gauge)
        self.assertNotIn("Done", gauge)
        self.assertIn("BOX Update finished (exit 0)\nlive tick --now", shown)

    def test_install_with_an_old_updater_uses_warpzone(self) -> None:
        self.install_state(doctor="")
        self.live('UPDATER_ABI=1\necho "old $*"\n')
        shown = self.menu("install")
        self.assertIn("XXX\n0\nWorking...\nXXX\n", shown)
        self.assertIn("BOX Update finished (exit 3)\none\ntwo", shown)

    def test_the_channel_changes_after_a_yes(self) -> None:
        self.install_state(doctor="")
        conf = self.updater / "conf"
        self.fake(
            self.venv_bin / "warpzone",
            'case "$1" in\n--version) echo "warpzone 0.3.0rc2" ;;\n'
            f'self-update) sed -i.x "s/^channel=.*/channel=$3/" "{conf}"\n'
            'echo "self-update: channel $3" ;;\nesac\n',
        )
        shown = self.menu("channel", "exit")
        self.assertIn("YESNO Follow stable releases from now on?", shown)
        self.assertIn("BOX channel stable", shown)
        self.assertEqual(self.boxes(shown), ["MENU", "YESNO", "BOX", "MENU"])
        second = self.menus(shown)[1]
        self.assertIn("channel Follow rc releases", second, "the state is read again")

    def test_no_keeps_the_channel(self) -> None:
        self.install_state(doctor="")
        shown = self.menu("channel", "exit", yes=False)
        self.assertNotIn("BOX", shown)
        self.assertEqual(self.boxes(shown), ["MENU", "YESNO", "MENU"])

    def test_without_a_terminal_it_shows_the_status_and_does_nothing_else(self) -> None:
        self.install_state(doctor="")
        self.fake_dialog("install")
        shown = self.run_script(options_menu.render_script())
        self.assertIn("Version: warpzone 0.3.0rc2", shown)
        self.assertFalse((self.tmp / "dialog.log").exists())


if __name__ == "__main__":
    unittest.main()
