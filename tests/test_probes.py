import dataclasses
import time
import unittest
from pathlib import Path

from warpzone import runners
from warpzone.probes import REAL_PROBES, DeviceProbes


class DeviceProbesTests(unittest.TestCase):
    def test_the_defaults_are_the_real_edges(self) -> None:
        probes = DeviceProbes()
        self.assertEqual(probes.proc, Path("/proc"))
        self.assertIs(probes.import_check, runners.run_import_check)
        self.assertIs(probes.flatpak, runners.run_flatpak)
        self.assertIs(probes.systemctl, runners.run_systemctl)
        self.assertIs(probes.user_systemctl, runners.run_user_systemctl)
        self.assertIs(probes.script_check, runners.run_script_check)
        self.assertIs(probes.sleep, time.sleep)
        self.assertIs(probes.clock, time.monotonic)
        self.assertEqual(REAL_PROBES, probes)

    def test_a_fake_replaces_one_piece_and_keeps_the_rest(self) -> None:
        fake = DeviceProbes(import_check=lambda python: True)
        self.assertTrue(fake.import_check("any python"))
        self.assertIs(fake.flatpak, runners.run_flatpak)

    def test_the_probes_are_not_changed_after_they_are_made(self) -> None:
        with self.assertRaises(dataclasses.FrozenInstanceError):
            REAL_PROBES.proc = Path("/elsewhere")  # type: ignore[misc]


if __name__ == "__main__":
    unittest.main()
