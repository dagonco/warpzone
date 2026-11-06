"""The unattended prune timer: the unit text, the install, the removal and the config key."""

import tempfile
import unittest
from collections.abc import Sequence
from pathlib import Path

from support import (
    API_KEY,
    DECK_ID,
    FakeProc,
    FakeSyncthing,
    config_data,
    device_probes,
    stub_self_update,
)
from test_provision import reporter
from warpzone import artwork
from warpzone.config import ConfigError, parse_config
from warpzone.probes import DeviceProbes
from warpzone.profiles import load_profile
from warpzone.provision import provision_device
from warpzone.provision_timers import ensure_prune_timer
from warpzone.syncthing import Client

setUpModule = stub_self_update

STEAM = load_profile("steamos-emudeck")
UNITS = ".config/systemd/user"
SERVICE, TIMER = "warpzone-prune.service", "warpzone-prune.timer"


def device(**keys):
    data = config_data()
    data["devices"]["deck"].update(keys)
    return parse_config(data).devices["deck"]


class ConfigTests(unittest.TestCase):
    def test_it_defaults_to_true_and_must_be_a_boolean(self) -> None:
        self.assertTrue(device().auto_prune)
        self.assertFalse(device(auto_prune=False).auto_prune)
        for bad in ("no", 0, None):
            with self.subTest(bad=bad), self.assertRaisesRegex(ConfigError, "auto_prune"):
                device(auto_prune=bad)

    def test_the_timer_is_for_a_device_that_lists_the_library(self) -> None:
        self.assertTrue(device().prunes_unattended)
        self.assertFalse(device(auto_prune=False).prunes_unattended)
        self.assertFalse(device(folders=["tools", "saves"]).prunes_unattended)
        self.assertFalse(device(folders=["tools"], auto_prune=True).prunes_unattended)


class UnitTextTests(unittest.TestCase):
    def test_the_service_runs_the_unattended_prune_of_the_device(self) -> None:
        service, timer = artwork.render_prune_units("/opt/wz/bin/python", "deck", None)
        self.assertIn("Type=oneshot", service)
        self.assertIn(
            "ExecStart=/opt/wz/bin/python -m warpzone prune deck --apply --unattended\n", service
        )
        self.assertNotIn("--config", service)
        self.assertIn("OnBootSec=15min\n", timer)
        self.assertIn("OnUnitActiveSec=1h\n", timer)
        self.assertIn("WantedBy=timers.target", timer)

    def test_a_non_default_config_is_named_and_quoted(self) -> None:
        service, _ = artwork.render_prune_units("/p", "deck", Path("/some dir/config.toml"))
        self.assertIn(
            '--apply --unattended --config "/some dir/config.toml"',
            service,
        )


class Install(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.home = Path(tmp.name) / "home"
        self.calls: list[tuple[str, ...]] = []
        self.ok = True

    def systemctl(self, args: Sequence[str]) -> bool:
        self.calls.append(tuple(args))
        return self.ok

    def ensure(self, dev=None, *, import_check=lambda python: True, **kw):
        rep, lines = reporter()
        kw.setdefault("python", "/opt/wz/bin/python")
        probes = DeviceProbes(import_check=import_check, user_systemctl=self.systemctl)
        ensure_prune_timer(
            "deck",
            dev or device(),
            self.home,
            rep,
            dry_run=kw.pop("dry_run", False),
            probes=probes,
            **kw,
        )
        return rep, lines

    def unit(self, name: str) -> Path:
        return self.home / UNITS / name


class InstallTests(Install):
    def test_writes_both_units_and_starts_the_timer(self) -> None:
        self.ensure()
        self.assertIn("-m warpzone prune deck --apply --unattended", self.unit(SERVICE).read_text())
        self.assertIn("OnUnitActiveSec=1h", self.unit(TIMER).read_text())
        self.assertEqual(
            self.calls,
            [("daemon-reload",), ("enable", "--now", TIMER), ("restart", TIMER)],
        )

    def test_a_second_run_changes_nothing_and_calls_no_systemctl(self) -> None:
        self.ensure()
        before = [self.unit(n).stat().st_mtime_ns for n in (SERVICE, TIMER)]
        self.calls.clear()
        _, lines = self.ensure()
        self.assertEqual(self.calls, [])
        self.assertEqual([self.unit(n).stat().st_mtime_ns for n in (SERVICE, TIMER)], before)
        self.assertTrue(any("unchanged" in line for line in lines))

    def test_a_changed_unit_is_rewritten_and_restarted(self) -> None:
        self.ensure()
        self.calls.clear()
        self.ensure(python="/other/python")
        self.assertIn("/other/python", self.unit(SERVICE).read_text())
        self.assertIn(("restart", TIMER), self.calls)

    def test_a_non_default_config_path_is_passed_on(self) -> None:
        self.ensure(config_path=Path("/etc/wz/config.toml"))
        self.assertIn("--config /etc/wz/config.toml", self.unit(SERVICE).read_text())
        self.calls.clear()
        self.ensure(config_path=self.home / ".config/warpzone/config.toml")
        self.assertNotIn("--config", self.unit(SERVICE).read_text())

    def test_a_dry_run_writes_nothing_and_calls_nothing(self) -> None:
        _, lines = self.ensure(dry_run=True)
        self.assertFalse((self.home / ".config").exists())
        self.assertEqual(self.calls, [])
        self.assertTrue(any("dry-run: would write" in line for line in lines))

    def test_python_that_cannot_import_warpzone_installs_nothing(self) -> None:
        rep, _ = self.ensure(import_check=lambda python: False)
        self.assertEqual(self.calls, [])
        self.assertFalse((self.home / ".config").exists())
        self.assertTrue(any("prune timer was not installed" in w for w in rep.warnings))

    def test_a_systemctl_that_fails_is_a_warning(self) -> None:
        self.ok = False
        rep, _ = self.ensure()
        self.assertTrue(any("systemctl --user daemon-reload" in w for w in rep.warnings))

    def test_a_device_without_the_library_gets_no_timer(self) -> None:
        self.ensure(device(folders=["tools", "saves"]))
        self.assertEqual(self.calls, [])
        self.assertFalse((self.home / ".config").exists())


class RemovalTests(Install):
    def test_auto_prune_false_removes_the_units(self) -> None:
        self.ensure()
        self.calls.clear()
        _, lines = self.ensure(device(auto_prune=False))
        self.assertFalse(self.unit(SERVICE).exists())
        self.assertFalse(self.unit(TIMER).exists())
        self.assertEqual(self.calls, [("disable", "--now", TIMER), ("daemon-reload",)])
        self.assertTrue(any("removed" in line for line in lines))

    def test_nothing_to_remove_makes_no_systemctl_call(self) -> None:
        self.ensure(device(auto_prune=False))
        self.assertEqual(self.calls, [])

    def test_dropping_the_library_removes_them_too(self) -> None:
        self.ensure()
        self.calls.clear()
        self.ensure(device(folders=["tools"]))
        self.assertFalse(self.unit(TIMER).exists())

    def test_a_dry_run_removes_nothing(self) -> None:
        self.ensure()
        self.calls.clear()
        _, lines = self.ensure(device(auto_prune=False), dry_run=True)
        self.assertTrue(self.unit(TIMER).exists())
        self.assertEqual(self.calls, [])
        self.assertTrue(any("would remove" in line for line in lines))

    def test_the_other_timers_are_left_alone(self) -> None:
        other = self.unit("warpzone-artwork.timer")
        other.parent.mkdir(parents=True)
        other.write_text("x")
        self.ensure(device(auto_prune=False))
        self.assertTrue(other.exists())


class ProvisionDeviceTests(unittest.TestCase):
    def test_provision_device_installs_the_timer_unless_it_is_turned_off(self) -> None:
        for keys, wanted in (({}, True), ({"auto_prune": False}, False)):
            with self.subTest(keys=keys), tempfile.TemporaryDirectory() as tmp:
                home = Path(tmp) / "home"
                data = config_data()
                data["devices"]["deck"].update(folders=["library"], self_update=False, **keys)
                cfg = parse_config(data)
                rep, _ = reporter()
                with FakeSyncthing(DECK_ID) as local:
                    for name in ("patterns-common.stignore", "patterns-deck.stignore"):
                        local.arrives_after[("wz-library", name)] = 0
                    provision_device(
                        cfg,
                        "deck",
                        STEAM,
                        Client(local.url, API_KEY),
                        None,
                        rep,
                        home=home,
                        wait_timeout=0.0,
                        poll=1.0,
                        python="/p",
                        probes=device_probes(
                            FakeProc(Path(tmp) / "proc").path,
                            import_check=lambda _: True,
                            user_systemctl=lambda _: True,
                            flatpak=lambda _: (0, ""),
                        ),
                        state_root=Path(tmp) / "state",
                    )
                self.assertEqual((home / UNITS / TIMER).exists(), wanted)
                self.assertEqual((home / UNITS / SERVICE).exists(), wanted)


if __name__ == "__main__":
    unittest.main()
