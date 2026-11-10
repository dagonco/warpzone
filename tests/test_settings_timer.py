"""The timer that applies ES-DE's layered settings while ES-DE is closed."""

import tempfile
import unittest
from collections.abc import Sequence
from pathlib import Path

from support import API_KEY, DECK_ID, FakeProc, FakeSyncthing, config_data, device_probes
from test_provision import reporter
from warpzone import timers
from warpzone.config import parse_config
from warpzone.probes import DeviceProbes
from warpzone.profiles import load_profile
from warpzone.provision import provision_device
from warpzone.provision_timers import ensure_settings_timer
from warpzone.syncthing import Client

STEAM = load_profile("steamos-emudeck")
UNITS = ".config/systemd/user"
SERVICE, TIMER = "warpzone-settings.service", "warpzone-settings.timer"


def device(**keys):
    data = config_data()
    data["devices"]["deck"].update(keys)
    return parse_config(data).devices["deck"]


class UnitTextTests(unittest.TestCase):
    def test_the_service_applies_only_esde_when_idle_and_quietly(self) -> None:
        service, timer = timers.render_settings_units("/opt/wz/bin/python", "deck", None)
        self.assertIn("Type=oneshot", service)
        self.assertIn(
            "ExecStart=/opt/wz/bin/python -m warpzone settings apply --device deck "
            "--emulator esde --when-idle --quiet\n",
            service,
        )
        self.assertIn("OnBootSec=5min\n", timer)
        self.assertIn("OnUnitActiveSec=15min\n", timer)

    def test_a_non_default_config_is_named(self) -> None:
        service, _ = timers.render_settings_units("/p", "deck", Path("/srv/wz/config.toml"))
        self.assertIn("--quiet --config /srv/wz/config.toml", service)


class Install(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.home = Path(tmp.name) / "home"
        self.calls: list[tuple[str, ...]] = []

    def systemctl(self, args: Sequence[str]) -> bool:
        self.calls.append(tuple(args))
        return True

    def ensure(self, dev=None, profile=STEAM, *, import_check=lambda python: True, **kw):
        rep, lines = reporter()
        ensure_settings_timer(
            "deck",
            dev or device(folders=["settings"]),
            profile,
            self.home,
            rep,
            dry_run=kw.pop("dry_run", False),
            python="/opt/wz/bin/python",
            probes=DeviceProbes(import_check=import_check, user_systemctl=self.systemctl),
            **kw,
        )
        return rep, lines

    def unit(self, name: str) -> Path:
        return self.home / UNITS / name


class InstallTests(Install):
    def test_a_device_that_subscribes_to_settings_gets_the_timer(self) -> None:
        self.ensure()
        self.assertIn("settings apply --device deck", self.unit(SERVICE).read_text())
        self.assertEqual(
            self.calls, [("daemon-reload",), ("enable", "--now", TIMER), ("restart", TIMER)]
        )

    def test_a_second_run_calls_no_systemctl(self) -> None:
        self.ensure()
        self.calls.clear()
        self.ensure()
        self.assertEqual(self.calls, [])

    def test_a_device_without_the_settings_group_gets_none_and_loses_it(self) -> None:
        self.ensure(device(folders=["tools"]))
        self.assertFalse(self.unit(TIMER).exists())
        self.assertEqual(self.calls, [])
        self.ensure()
        self.calls.clear()
        self.ensure(device(folders=["tools"]))
        self.assertFalse(self.unit(TIMER).exists())
        self.assertFalse(self.unit(SERVICE).exists())
        self.assertIn(("disable", "--now", TIMER), self.calls)

    def test_a_profile_without_esde_settings_gets_none(self) -> None:
        profile = load_profile("anbernic-rg351mp")
        self.assertNotIn("esde", profile.settings)
        self.ensure(profile=profile)
        self.assertFalse(self.unit(TIMER).exists())

    def test_a_dry_run_writes_nothing(self) -> None:
        self.ensure(dry_run=True)
        self.assertFalse(self.unit(TIMER).exists())
        self.assertEqual(self.calls, [])

    def test_python_that_cannot_import_warpzone_installs_nothing(self) -> None:
        rep, _ = self.ensure(import_check=lambda python: False)
        self.assertFalse(self.unit(TIMER).exists())
        self.assertTrue(any("settings timer was not installed" in w for w in rep.warnings))


class ProvisionDeviceTests(unittest.TestCase):
    def test_provision_device_installs_the_timer_for_a_settings_device(self) -> None:
        for folders, wanted in ((["settings"], True), (["tools"], False)):
            with self.subTest(folders=folders), tempfile.TemporaryDirectory() as tmp:
                home = Path(tmp) / "home"
                data = config_data()
                data["devices"]["deck"].update(folders=folders, self_update=False)
                cfg = parse_config(data)
                rep, _ = reporter()
                with FakeSyncthing(DECK_ID) as local:
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


if __name__ == "__main__":
    unittest.main()
