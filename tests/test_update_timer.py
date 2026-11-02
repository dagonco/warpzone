"""The systemd user timer that runs `warpzone self-update`."""

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from support import API_KEY, FakeSyncthing, config_data
from test_provision import reporter
from warpzone import updatetimer
from warpzone.config import parse_config
from warpzone.profiles import load_profile
from warpzone.provision import ensure_update_timer, provision_device
from warpzone.syncthing import Client

TIMER = "warpzone-self-update.timer"
PYTHON = "/home/deck/.local/share/warpzone/venv/bin/python"


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
    def test_the_timer_runs_daily_catches_up_and_runs_after_boot(self) -> None:
        text = updatetimer.render_timer()
        self.assertIn("OnCalendar=daily", text)
        self.assertIn("Persistent=true", text)
        self.assertRegex(text, r"(?m)^OnBootSec=\d+min$")
        self.assertIn("WantedBy=timers.target", text)

    def test_the_service_runs_self_update_with_the_given_python(self) -> None:
        text = updatetimer.render_service(PYTHON)
        self.assertIn("Type=oneshot", text)
        self.assertIn(f"ExecStart={PYTHON} -m warpzone self-update", text)
        self.assertNotIn("ES-DE", text)

    def test_special_characters_in_the_path_are_quoted_for_systemd(self) -> None:
        text = updatetimer.render_service("/opt/my wz/100%/py$")
        self.assertIn('ExecStart="/opt/my wz/100%%/py$$" -m warpzone self-update', text)

    def test_unit_names(self) -> None:
        self.assertEqual(updatetimer.TIMER, TIMER)
        self.assertEqual(updatetimer.SERVICE, "warpzone-self-update.service")


class EnsureTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.home = Path(self._tmp.name)
        self.units = self.home / ".config/systemd/user"
        self.rep, self.lines = reporter()

    def ensure(self, systemctl, *, dry_run=False, python=PYTHON, check=lambda p: True) -> None:
        ensure_update_timer(
            self.home,
            self.rep,
            dry_run=dry_run,
            python=python,
            import_check=check,
            systemctl=systemctl,
        )

    def test_installs_and_enables_the_timer(self) -> None:
        ctl = FakeSystemctl()
        self.ensure(ctl)
        self.assertEqual((self.units / "warpzone-self-update.timer").read_text()[:7], "[Unit]\n")
        service = (self.units / "warpzone-self-update.service").read_text()
        self.assertIn(f"ExecStart={PYTHON} -m warpzone self-update", service)
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

    def test_a_changed_unit_is_rewritten_and_the_timer_restarted(self) -> None:
        ctl = FakeSystemctl()
        self.ensure(ctl)
        ctl.calls.clear()
        self.ensure(ctl, python="/opt/other/python")
        self.assertIn(
            "ExecStart=/opt/other/python",
            (self.units / "warpzone-self-update.service").read_text(),
        )
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

    def test_no_systemd_user_manager_is_a_warning_and_writes_nothing(self) -> None:
        for ctl in (
            FakeSystemctl(bus=False),
            mock.Mock(side_effect=FileNotFoundError("systemctl")),
        ):
            rep, _ = reporter()
            ensure_update_timer(
                self.home, rep, dry_run=False, python=PYTHON, import_check=lambda p: True,
                systemctl=ctl,
            )  # fmt: skip
            self.assertEqual(len(rep.warnings), 1)
            self.assertIn("systemd", rep.warnings[0])
            self.assertIn("warpzone self-update", rep.warnings[0])
            self.assertFalse(self.units.exists())

    def test_a_python_that_cannot_import_warpzone_installs_nothing(self) -> None:
        ctl = FakeSystemctl()
        self.ensure(ctl, check=lambda p: False)
        self.assertEqual(len(self.rep.warnings), 1)
        self.assertIn("cannot import warpzone", self.rep.warnings[0])
        self.assertFalse(self.units.exists())
        self.assertEqual(ctl.calls, [])

    def test_a_failed_enable_warns_with_the_command(self) -> None:
        self.ensure(FakeSystemctl(fail_enable=True))
        (warning,) = self.rep.warnings
        self.assertIn("systemctl --user enable --now warpzone-self-update.timer", warning)


class ProvisionDeviceTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.home = Path(self._tmp.name)

    def provision(self, profile: str, device_id: str, *, self_update=True):
        data = config_data()
        data["devices"] = {
            "dev": {
                "device_id": device_id,
                "profile": profile,
                "folders": ["library"],
                "self_update": self_update,
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
                sleep=lambda s: None,
                python=PYTHON,
                import_check=lambda p: True,
                systemctl_run=ctl,
                state_root=self.home / "state",
                proc=self.home / "no-proc",
            )
        return ctl, lines

    def test_every_shipped_profile_gets_the_timer(self) -> None:
        for profile, device_id in (
            ("steamos-emudeck", "BBBBBBB-BBBBBBB-BBBBBBB-BBBBBBB-BBBBBBB-BBBBBBB-BBBBBBB-BBBBBBB"),
            ("anbernic-rg351mp", "DDDDDDD-DDDDDDD-DDDDDDD-DDDDDDD-DDDDDDD-DDDDDDD-DDDDDDD-DDDDDDD"),
        ):
            with self.subTest(profile=profile):
                home = Path(tempfile.mkdtemp(dir=self.home))
                self.home = home
                ctl, lines = self.provision(profile, device_id)
                self.assertIn(("enable", "--now", TIMER), ctl.calls)
                self.assertTrue((home / ".config/systemd/user" / TIMER).is_file())
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
