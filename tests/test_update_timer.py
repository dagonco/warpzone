"""The updater's pieces on a device: the timer units that start `updater.sh tick`, the live
copy of the script, and the settings file `provision device` writes for it."""

import os
import re
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from support import API_KEY, FakeSyncthing, config_data, device_probes
from test_provision import reporter
from warpzone import selfupdate, updatetimer
from warpzone.config import parse_config
from warpzone.probes import DeviceProbes
from warpzone.profiles import load_profile
from warpzone.provision import provision_device
from warpzone.provision_updater import ensure_self_update, ensure_update_timer, ensure_updater
from warpzone.syncthing import Client

TIMER = "warpzone-self-update.timer"
PYTHON = "/home/deck/.local/share/warpzone/venv/bin/python"
UPDATER = "/home/deck/.local/share/warpzone/updater/updater.sh"
SCRIPT_V1 = '#!/bin/sh\nUPDATER_ABI=1\ncase ${1:-} in selftest) echo "selftest: ok abi=1" ;; esac\n'
SCRIPT_V2 = '#!/bin/sh\nUPDATER_ABI=2\ncase ${1:-} in selftest) echo "selftest: ok abi=2" ;; esac\n'


class FakeSystemctl:
    """`systemctl --user` with a little state: whether the timer is enabled and active."""

    def __init__(self, *, bus=True, enabled=False, active=False, fail_enable=False) -> None:
        self.bus, self.enabled, self.active = bus, enabled, active
        self.fail_enable = fail_enable
        self.calls: list[tuple[str, ...]] = []

    def __call__(self, command):
        assert list(command[:2]) == ["systemctl", "--user"], command
        verb, rest = command[2], tuple(command[3:])
        self.calls.append((verb, *rest))
        if verb == "show-environment":
            if not self.bus:
                return 1, "Failed to connect to bus: No medium found"
            return 0, "PATH=/usr/bin"
        if verb == "is-enabled":
            return (0, "enabled") if self.enabled else (1, "disabled")
        if verb == "is-active":
            return (0, "active") if self.active else (3, "inactive")
        if verb == "enable":
            if self.fail_enable:
                return 1, "Failed to enable unit"
            self.enabled = self.active = True
        return 0, ""

    def changes(self) -> list[tuple[str, ...]]:
        read_only = ("show-environment", "is-enabled", "is-active")
        return [c for c in self.calls if c[0] not in read_only]


class RenderTests(unittest.TestCase):
    def test_the_timer_starts_the_tick_after_boot_and_every_fifteen_minutes(self) -> None:
        text = updatetimer.render_timer()
        self.assertRegex(text, r"(?m)^OnBootSec=45s$")
        self.assertRegex(text, r"(?m)^OnUnitActiveSec=15min$")
        self.assertIn("WantedBy=timers.target", text)

    def test_the_timer_has_no_random_delay_and_no_calendar(self) -> None:
        # A random delay also pushes back the start after boot, when the menu may not be open.
        text = updatetimer.render_timer()
        for gone in ("RandomizedDelaySec", "OnCalendar", "Persistent", "FixedRandomDelay"):
            self.assertNotIn(gone, text)

    def test_the_service_runs_the_live_copy_with_sh_and_may_take_half_an_hour(self) -> None:
        text = updatetimer.render_service(UPDATER)
        self.assertIn("Type=oneshot", text)
        self.assertRegex(text, r"(?m)^TimeoutStartSec=30min$")
        self.assertRegex(text, rf"(?m)^ExecStart=/bin/sh {UPDATER} tick$")
        self.assertNotIn("python", text)
        self.assertNotIn("ES-DE", text)

    def test_the_unit_round_trips_through_exec_command(self) -> None:
        self.assertEqual(
            updatetimer.exec_command(updatetimer.render_service(UPDATER)),
            ["/bin/sh", UPDATER, "tick"],
        )
        self.assertEqual(updatetimer.expected_command(UPDATER), ["/bin/sh", UPDATER, "tick"])

    def test_special_characters_in_the_path_are_quoted_for_systemd(self) -> None:
        path = "/opt/my wz/100%/u$h.sh"
        text = updatetimer.render_service(path)
        self.assertIn('ExecStart=/bin/sh "/opt/my wz/100%%/u$$h.sh" tick', text)
        self.assertEqual(updatetimer.exec_command(text), ["/bin/sh", path, "tick"])

    def test_unit_names(self) -> None:
        self.assertEqual(updatetimer.TIMER, TIMER)
        self.assertEqual(updatetimer.SERVICE, "warpzone-self-update.service")
        self.assertEqual(updatetimer.NAME, "warpzone-self-update")


class TmpHome(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.home = Path(self._tmp.name)
        self.units = self.home / ".config/systemd/user"
        self.rep, self.lines = reporter()


class EnsureTests(TmpHome):
    def setUp(self) -> None:
        super().setUp()
        self.updater = self.home / "live/updater.sh"
        self.updater.parent.mkdir()
        self.updater.write_text(SCRIPT_V1)

    def ensure(self, systemctl, *, dry_run=False, updater=None) -> None:
        ensure_update_timer(
            self.home,
            self.rep,
            dry_run=dry_run,
            updater=updater or self.updater,
            probes=DeviceProbes(systemctl=systemctl),
        )

    def test_installs_and_enables_the_timer(self) -> None:
        ctl = FakeSystemctl()
        self.ensure(ctl)
        self.assertEqual((self.units / TIMER).read_text(), updatetimer.render_timer())
        service = (self.units / "warpzone-self-update.service").read_text()
        self.assertIn(f"ExecStart=/bin/sh {self.updater} tick", service)
        self.assertEqual(ctl.changes(), [("daemon-reload",), ("enable", "--now", TIMER)], ctl.calls)
        self.assertEqual(self.rep.warnings, [])
        self.assertTrue(any("self-update timer: installed and enabled" in x for x in self.lines))
        self.assertEqual((self.units / TIMER).stat().st_mode & 0o777, 0o644)

    def test_a_second_run_changes_nothing(self) -> None:
        ctl = FakeSystemctl()
        self.ensure(ctl)
        ctl.calls.clear()
        self.lines.clear()
        self.ensure(ctl)
        self.assertEqual(ctl.changes(), [])
        self.assertEqual(self.lines, ["self-update timer: already installed and enabled"])

    def test_an_old_unit_that_ran_python_is_rewritten_and_the_timer_restarted(self) -> None:
        ctl = FakeSystemctl()
        self.units.mkdir(parents=True)
        (self.units / "warpzone-self-update.service").write_text(
            f"[Service]\nType=oneshot\nExecStart={PYTHON} -m warpzone self-update\n"
        )
        (self.units / TIMER).write_text("[Timer]\nOnCalendar=daily\nRandomizedDelaySec=30min\n")
        ctl.enabled = ctl.active = True
        self.ensure(ctl)
        self.assertIn(
            f"ExecStart=/bin/sh {self.updater} tick",
            (self.units / "warpzone-self-update.service").read_text(),
        )
        self.assertNotIn("RandomizedDelaySec", (self.units / TIMER).read_text())
        self.assertEqual(
            ctl.changes(),
            [("daemon-reload",), ("enable", "--now", TIMER), ("restart", TIMER)],
        )

    def test_a_timer_that_was_switched_off_is_enabled_again(self) -> None:
        ctl = FakeSystemctl()
        self.ensure(ctl)
        ctl.enabled = ctl.active = False
        ctl.calls.clear()
        self.ensure(ctl)
        self.assertEqual(ctl.changes(), [("enable", "--now", TIMER)])

    def test_dry_run_writes_and_changes_nothing(self) -> None:
        ctl = FakeSystemctl()
        self.ensure(ctl, dry_run=True)
        self.assertFalse(self.units.exists())
        self.assertEqual(ctl.changes(), [])
        self.assertTrue(any("dry-run: would write" in x and TIMER in x for x in self.lines))

    def test_dry_run_does_not_need_the_live_copy_yet(self) -> None:
        ctl = FakeSystemctl()
        self.ensure(ctl, dry_run=True, updater=self.home / "not-yet/updater.sh")
        self.assertEqual(self.rep.warnings, [])
        self.assertTrue(any("dry-run: would write" in x for x in self.lines))

    def test_no_systemd_user_manager_is_a_warning_and_writes_nothing(self) -> None:
        for ctl in (
            FakeSystemctl(bus=False),
            mock.Mock(side_effect=FileNotFoundError("systemctl")),
        ):
            rep, _ = reporter()
            probes = DeviceProbes(systemctl=ctl)
            ensure_update_timer(self.home, rep, dry_run=False, updater=self.updater, probes=probes)
            self.assertEqual(len(rep.warnings), 1)
            self.assertIn("systemd", rep.warnings[0])
            self.assertIn("updater.sh tick", rep.warnings[0])
            self.assertFalse(self.units.exists())

    def test_without_the_live_copy_no_unit_is_written(self) -> None:
        ctl = FakeSystemctl()
        self.ensure(ctl, updater=self.home / "gone/updater.sh")
        (warning,) = self.rep.warnings
        self.assertIn("gone/updater.sh", warning)
        self.assertFalse(self.units.exists())
        self.assertEqual(ctl.calls, [])

    def test_a_failed_enable_warns_with_the_command(self) -> None:
        self.ensure(FakeSystemctl(fail_enable=True))
        (warning,) = self.rep.warnings
        self.assertIn("systemctl --user enable --now warpzone-self-update.timer", warning)


class EnsureUpdaterTests(TmpHome):
    def setUp(self) -> None:
        super().setUp()
        self.source = self.home / "src" / "updater.sh"
        self.source.parent.mkdir()
        self.live = selfupdate.live_path(self.home)

    def offer(self, text: str, *, dry_run=False) -> bool:
        self.source.write_text(text)
        return ensure_updater(self.home, self.rep, dry_run=dry_run, source=self.source)

    def leftovers(self) -> list[str]:
        return sorted(p.name for p in self.live.parent.iterdir())

    def test_the_first_install_copies_the_script_outside_any_clone(self) -> None:
        self.assertTrue(self.offer(SCRIPT_V1))
        self.assertEqual(self.live.read_text(), SCRIPT_V1)
        self.assertTrue(os.access(self.live, os.X_OK))
        self.assertEqual(self.leftovers(), ["updater.sh"])
        self.assertTrue(any("updater: installed" in x for x in self.lines), self.lines)

    def test_a_renewal_replaces_it_atomically_and_keeps_the_old_one_as_prev(self) -> None:
        self.offer(SCRIPT_V1)
        before = self.live.stat().st_ino
        held = self.live.open()  # a tick that is running keeps reading the file it started with
        self.addCleanup(held.close)
        self.offer(SCRIPT_V2)
        self.assertEqual(self.live.read_text(), SCRIPT_V2)
        self.assertEqual((self.live.parent / "updater.sh.prev").read_text(), SCRIPT_V1)
        self.assertNotEqual(self.live.stat().st_ino, before, "replaced, not rewritten in place")
        self.assertEqual(held.read(), SCRIPT_V1)
        self.assertEqual(self.leftovers(), ["updater.sh", "updater.sh.prev"])

    def test_the_same_script_changes_nothing(self) -> None:
        self.offer(SCRIPT_V1)
        stamp = self.live.stat().st_ino
        self.lines.clear()
        self.assertTrue(self.offer(SCRIPT_V1))
        self.assertEqual(self.live.stat().st_ino, stamp)
        self.assertFalse((self.live.parent / "updater.sh.prev").exists())
        self.assertEqual(self.lines, [f"updater: already current ({self.live})"])

    def test_a_script_of_a_lower_abi_is_never_installed_over_a_higher_one(self) -> None:
        # A rollback to an older release must not downgrade the script that is running it.
        self.offer(SCRIPT_V2)
        self.lines.clear()
        self.assertTrue(self.offer(SCRIPT_V1))
        self.assertEqual(self.live.read_text(), SCRIPT_V2)
        self.assertFalse((self.live.parent / "updater.sh.prev").exists())
        self.assertEqual(self.rep.warnings, [])
        self.assertTrue(any("abi 2" in x and "abi 1" in x for x in self.lines), self.lines)

    def test_the_same_abi_with_other_text_is_replaced(self) -> None:
        self.offer(SCRIPT_V1)
        self.offer(SCRIPT_V1 + "# a fix\n")
        self.assertTrue(self.live.read_text().endswith("# a fix\n"))

    def test_a_script_with_a_syntax_error_is_not_installed(self) -> None:
        self.offer(SCRIPT_V1)
        self.assertTrue(self.offer("UPDATER_ABI=2\nif then fi (\n"))  # the old one still works
        self.assertEqual(self.live.read_text(), SCRIPT_V1)
        (warning,) = self.rep.warnings
        self.assertIn("syntax", warning)
        self.assertEqual(self.leftovers(), ["updater.sh"])

    def test_a_script_whose_selftest_fails_is_not_installed(self) -> None:
        self.offer(SCRIPT_V1)
        broken = 'UPDATER_ABI=2\ncase ${1:-} in selftest) echo "missing: git"; exit 1 ;; esac\n'
        self.assertTrue(self.offer(broken))
        self.assertEqual(self.live.read_text(), SCRIPT_V1)
        (warning,) = self.rep.warnings
        self.assertIn("selftest", warning)
        self.assertIn("missing: git", warning)
        self.assertEqual(self.leftovers(), ["updater.sh"])

    def test_a_selftest_that_reports_another_abi_is_not_installed(self) -> None:
        text = 'UPDATER_ABI=2\ncase ${1:-} in selftest) echo "selftest: ok abi=1" ;; esac\n'
        self.assertFalse(self.offer(text))
        self.assertFalse(self.live.exists())
        self.assertEqual(len(self.rep.warnings), 1)

    def test_a_script_without_an_abi_is_not_installed(self) -> None:
        self.assertFalse(self.offer("#!/bin/sh\nexit 0\n"))
        self.assertFalse(self.live.exists())
        self.assertIn("UPDATER_ABI", self.rep.warnings[0])

    def test_with_nothing_installed_and_a_bad_script_it_says_so(self) -> None:
        self.assertFalse(self.offer("UPDATER_ABI=1\n(\n"))
        self.assertEqual(len(self.rep.warnings), 1)

    def test_a_script_that_hangs_in_its_selftest_is_cut_off(self) -> None:
        text = "UPDATER_ABI=2\ncase ${1:-} in selftest) sleep 30 ;; esac\n"
        self.source.write_text(text)
        with mock.patch("warpzone.provision_updater.UPDATER_CHECK_SECONDS", 1):
            self.assertFalse(ensure_updater(self.home, self.rep, dry_run=False, source=self.source))
        self.assertIn("selftest", self.rep.warnings[0])
        self.assertFalse(self.live.exists())

    def checked_by(self, script_check) -> bool:
        probes = DeviceProbes(script_check=script_check)
        return ensure_updater(self.home, self.rep, dry_run=False, source=self.source, probes=probes)

    def runner(self, during=None):
        """A fake `sh -n` and `selftest`; `during` runs at the moment of the selftest."""

        def run(command, timeout):
            if "selftest" in command:
                if during is not None:
                    during()
                abi = selfupdate.updater_abi(Path(command[1]).read_text())
                return 0, f"selftest: ok abi={abi}\n"
            return 0, ""

        return run

    def test_the_temporary_file_has_a_name_of_its_own_every_time(self) -> None:
        names: list[list[str]] = []
        self.live.parent.mkdir(parents=True)
        self.source.write_text(SCRIPT_V1)
        run = self.runner(lambda: names.append(self.leftovers()))
        self.assertTrue(self.checked_by(run))
        (temp,) = names[0]
        self.assertNotEqual(temp, ".updater.sh.new")
        self.assertTrue(temp.startswith(".updater.sh."), temp)
        self.assertEqual(self.leftovers(), ["updater.sh"])

    def test_a_live_copy_that_changed_during_the_checks_is_judged_again(self) -> None:
        # another provision installed a script of a higher ABI while this one was being checked
        self.offer(SCRIPT_V1)
        self.source.write_text(SCRIPT_V1 + "# a fix\n")
        run = self.runner(lambda: self.live.write_text(SCRIPT_V2))
        self.assertTrue(self.checked_by(run))
        self.assertEqual(self.live.read_text(), SCRIPT_V2)
        self.assertFalse((self.live.parent / "updater.sh.prev").exists())
        self.assertEqual(self.leftovers(), ["updater.sh"])

    def test_the_prev_copy_is_the_one_that_was_live_at_the_last_moment(self) -> None:
        self.offer(SCRIPT_V1)
        self.source.write_text(SCRIPT_V1 + "# a fix\n")
        midway = SCRIPT_V1 + "# installed meanwhile\n"
        run = self.runner(lambda: self.live.write_text(midway))
        self.assertTrue(self.checked_by(run))
        self.assertTrue(self.live.read_text().endswith("# a fix\n"))
        self.assertEqual((self.live.parent / "updater.sh.prev").read_text(), midway)

    def test_the_script_that_ships_with_warpzone_passes_its_own_checks(self) -> None:
        # not a fake: sh -n, selftest and the line that says its ABI, as they are
        self.assertTrue(ensure_updater(self.home, self.rep, dry_run=False))
        self.assertEqual(self.rep.warnings, [])
        self.assertEqual(self.live.read_text(), selfupdate.UPDATER_SOURCE.read_text())
        abi = selfupdate.updater_abi(self.live.read_text())
        shown = subprocess.run(
            ["sh", str(self.live), "selftest"], capture_output=True, text=True, check=False
        )
        self.assertEqual(shown.returncode, 0, shown.stderr)
        self.assertRegex(shown.stdout, rf"(?m)^selftest: ok abi={abi}$")
        self.assertIsNotNone(re.search(r"UPDATER_ABI=\d+", self.live.read_text()))

    def test_dry_run_writes_nothing(self) -> None:
        self.assertFalse(self.offer(SCRIPT_V1, dry_run=True))
        self.assertFalse(self.live.parent.exists())
        self.assertTrue(any("dry-run: would install" in x for x in self.lines), self.lines)

    def test_dry_run_reports_a_live_copy_that_is_there(self) -> None:
        self.offer(SCRIPT_V1)
        self.assertTrue(self.offer(SCRIPT_V2, dry_run=True))
        self.assertEqual(self.live.read_text(), SCRIPT_V1)


class FakeSource(TmpHome):
    """A device home, a fake clone and a fake script to install."""

    def setUp(self) -> None:
        super().setUp()
        self.clone = self.home / "src"
        (self.clone / ".git").mkdir(parents=True)
        self.source = self.home / "shipped-updater.sh"
        self.source.write_text(SCRIPT_V1)
        self.state_root = self.home / ".local/state/warpzone/settings"
        self.conf = self.home / ".local/state/warpzone/updater/conf"


class EnsureSelfUpdateTests(FakeSource):
    def setUp(self) -> None:
        super().setUp()
        profile = load_profile("steamos-emudeck")
        self.profile = profile
        data = config_data()
        data["devices"] = {
            "dev": {
                "device_id": "BBBBBBB-BBBBBBB-BBBBBBB-BBBBBBB-BBBBBBB-BBBBBBB-BBBBBBB-BBBBBBB",
                "profile": "steamos-emudeck",
                "folders": ["library"],
                "channel": "rc",
                "pin": "v0.3.0rc1",
            }
        }
        self.cfg = parse_config(data)

    def ensure(self, *, dry_run=False, ctl=None, device=None, clone=None, check=lambda p: True):
        ctl = ctl if ctl is not None else FakeSystemctl()
        ensure_self_update(
            device or self.cfg.devices["dev"],
            self.profile,
            self.home,
            self.rep,
            dry_run=dry_run,
            python=PYTHON,
            clone=clone or self.clone,
            config_path=Path("/home/deck/.config/warpzone/config.toml"),
            state_root=self.state_root,
            probes=DeviceProbes(import_check=check, systemctl=ctl),
            updater_source=self.source,
        )
        return ctl

    def test_it_writes_the_settings_the_copy_and_the_units(self) -> None:
        ctl = self.ensure()
        parsed = selfupdate.parse_kv(self.conf.read_text())
        self.assertEqual(parsed["device"], "dev")
        self.assertEqual(parsed["clone"], str(self.clone))
        self.assertEqual(parsed["python"], PYTHON)
        self.assertEqual(parsed["config"], "/home/deck/.config/warpzone/config.toml")
        self.assertEqual(
            (parsed["channel"], parsed["pin"], parsed["enabled"]), ("rc", "v0.3.0rc1", "true")
        )
        self.assertEqual(parsed["frontend"], "es-de ES-DE.AppImage")
        live = selfupdate.live_path(self.home)
        self.assertEqual(live.read_text(), SCRIPT_V1)
        service = (self.home / updatetimer.UNIT_DIR / updatetimer.SERVICE).read_text()
        self.assertIn(f"ExecStart=/bin/sh {live} tick", service)
        self.assertIn(("enable", "--now", TIMER), ctl.calls)
        self.assertEqual(self.rep.warnings, [])

    def test_a_second_run_changes_nothing(self) -> None:
        self.ensure()
        before = (self.conf.stat().st_ino, selfupdate.live_path(self.home).stat().st_ino)
        self.lines.clear()
        ctl = self.ensure(ctl=FakeSystemctl(enabled=True, active=True))
        self.assertEqual(
            (self.conf.stat().st_ino, selfupdate.live_path(self.home).stat().st_ino), before
        )
        self.assertEqual(ctl.changes(), [])
        self.assertTrue(any("settings: already current" in x for x in self.lines), self.lines)

    def test_a_frontend_that_the_profile_names_is_used(self) -> None:
        self.profile = load_profile("anbernic-rg351mp")
        self.ensure()
        frontend = selfupdate.parse_kv(self.conf.read_text())["frontend"]
        self.assertEqual(frontend, "emulationstatio emulationstation")

    def test_settings_are_replaced_when_the_config_changes(self) -> None:
        self.ensure()
        data = config_data()
        data["devices"] = {
            "dev": {
                "device_id": "BBBBBBB-BBBBBBB-BBBBBBB-BBBBBBB-BBBBBBB-BBBBBBB-BBBBBBB-BBBBBBB",
                "profile": "steamos-emudeck",
                "folders": ["library"],
            }
        }
        self.ensure(device=parse_config(data).devices["dev"])
        parsed = selfupdate.parse_kv(self.conf.read_text())
        self.assertEqual((parsed["channel"], parsed["pin"]), ("stable", ""))

    def test_dry_run_writes_nothing(self) -> None:
        ctl = self.ensure(dry_run=True)
        self.assertFalse(self.conf.exists())
        self.assertFalse(selfupdate.live_path(self.home).exists())
        self.assertFalse((self.home / updatetimer.UNIT_DIR).exists())
        self.assertEqual(ctl.changes(), [])
        self.assertTrue(any("settings: dry-run: would write" in x for x in self.lines))

    def test_a_clone_that_is_not_a_git_checkout_installs_nothing(self) -> None:
        plain = self.home / "site-packages"
        plain.mkdir()
        ctl = self.ensure(clone=plain)
        (warning,) = self.rep.warnings
        self.assertIn("not a git checkout", warning)
        self.assertIn("updater.sh install", warning)
        self.assertFalse(self.conf.exists())
        self.assertFalse(selfupdate.live_path(self.home).exists())
        self.assertEqual(ctl.calls, [])

    def test_a_python_that_cannot_import_warpzone_installs_nothing(self) -> None:
        ctl = self.ensure(check=lambda p: False)
        (warning,) = self.rep.warnings
        self.assertIn("cannot import warpzone", warning)
        self.assertFalse(self.conf.exists())
        self.assertEqual(ctl.calls, [])

    def test_a_copy_that_cannot_be_installed_leaves_no_timer(self) -> None:
        self.source.write_text("#!/bin/sh\n(\n")
        ctl = self.ensure()
        self.assertTrue(self.rep.warnings)
        self.assertFalse((self.home / updatetimer.UNIT_DIR).exists())
        self.assertEqual(ctl.calls, [])

    def test_a_device_that_turned_it_off_stops_an_updater_it_had(self) -> None:
        self.ensure()
        data = config_data()
        data["devices"] = {
            "dev": {
                "device_id": "BBBBBBB-BBBBBBB-BBBBBBB-BBBBBBB-BBBBBBB-BBBBBBB-BBBBBBB-BBBBBBB",
                "profile": "steamos-emudeck",
                "folders": ["library"],
                "self_update": False,
            }
        }
        ctl = self.ensure(device=parse_config(data).devices["dev"], ctl=FakeSystemctl())
        self.assertEqual(selfupdate.parse_kv(self.conf.read_text())["enabled"], "false")
        self.assertEqual(ctl.calls, [])

    def test_a_device_that_never_had_it_gets_no_files_when_it_is_off(self) -> None:
        data = config_data()
        data["devices"] = {
            "dev": {
                "device_id": "BBBBBBB-BBBBBBB-BBBBBBB-BBBBBBB-BBBBBBB-BBBBBBB-BBBBBBB-BBBBBBB",
                "profile": "steamos-emudeck",
                "folders": ["library"],
                "self_update": False,
            }
        }
        ctl = self.ensure(device=parse_config(data).devices["dev"])
        self.assertFalse(self.conf.exists())
        self.assertFalse(selfupdate.live_path(self.home).exists())
        self.assertEqual(ctl.calls, [])
        self.assertIn(
            "self-update timer: turned off (self_update = false in the config)", self.lines
        )


class ProvisionDeviceTests(FakeSource):
    def provision(self, profile: str, device_id: str, *, self_update=True):
        data = config_data()
        data["devices"] = {
            "dev": {
                "device_id": device_id,
                "profile": profile,
                "folders": ["library"],
                "self_update": self_update,
                "auto_prune": False,  # tests/test_prune_timer.py covers the prune timer
            }
        }
        cfg = parse_config(data)
        rep, lines = reporter()
        ctl = FakeSystemctl()
        with FakeSyncthing(device_id) as local:
            for name in ("patterns-common.stignore", "patterns-dev.stignore"):
                local.arrives_after[("wz-library", name)] = 0
            client = Client(local.url, API_KEY, label="local", sink=lines.append)
            provision_device(
                cfg,
                "dev",
                load_profile(profile),
                client,
                None,
                rep,
                home=self.home,
                wait_timeout=0.0,
                poll=1.0,
                python=PYTHON,
                probes=device_probes(
                    self.home / "no-proc", import_check=lambda p: True, systemctl=ctl
                ),
                state_root=self.state_root,
                clone=self.clone,
                updater_source=self.source,
            )
        return ctl, lines

    def test_every_shipped_profile_gets_the_updater_and_the_timer(self) -> None:
        for profile, device_id in (
            ("steamos-emudeck", "BBBBBBB-BBBBBBB-BBBBBBB-BBBBBBB-BBBBBBB-BBBBBBB-BBBBBBB-BBBBBBB"),
            ("anbernic-rg351mp", "DDDDDDD-DDDDDDD-DDDDDDD-DDDDDDD-DDDDDDD-DDDDDDD-DDDDDDD-DDDDDDD"),
        ):
            with self.subTest(profile=profile):
                home = Path(tempfile.mkdtemp(dir=self.home))
                self.home = home
                self.state_root = home / ".local/state/warpzone/settings"
                ctl, lines = self.provision(profile, device_id)
                self.assertIn(("enable", "--now", TIMER), ctl.calls)
                self.assertTrue((home / ".config/systemd/user" / TIMER).is_file())
                self.assertTrue(selfupdate.live_path(home).is_file())
                self.assertTrue((home / ".local/state/warpzone/updater/conf").is_file())
                self.assertTrue(any(x.startswith("self-update timer:") for x in lines))

    def test_the_device_can_turn_it_off_in_the_config(self) -> None:
        ctl, lines = self.provision(
            "steamos-emudeck",
            "BBBBBBB-BBBBBBB-BBBBBBB-BBBBBBB-BBBBBBB-BBBBBBB-BBBBBBB-BBBBBBB",
            self_update=False,
        )
        self.assertEqual(ctl.calls, [])
        self.assertFalse((self.home / ".config/systemd/user").exists())
        self.assertIn("self-update timer: turned off (self_update = false in the config)", lines)


if __name__ == "__main__":
    unittest.main()
