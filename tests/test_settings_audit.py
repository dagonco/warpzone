"""`settings audit`: the read-only report, its output and exit codes."""

import unittest

from settings_support import ALPHA_INI, BETA_JSON, FAKE_TOKEN, FAKE_USER, SettingsCase
from warpzone.config import ConfigError

ALPHA = ".config/alpha/main.ini"
BETA = ".config/beta/config.json"


class AuditBase(SettingsCase):
    def setUp(self) -> None:
        super().setUp()
        self.put(ALPHA, ALPHA_INI)
        self.put(BETA, BETA_JSON)
        self.layer("common", '[alpha.main]\n"[Video]gpu" = "2"\n"[System]language" = "1"\n')
        self.layer("deck", "")


class AuditTests(AuditBase):
    def test_pending_before_the_first_apply(self) -> None:
        code, out = self.run_audit()
        self.assertEqual(code, 1)
        self.assertIn("alpha  main  ~/.config/alpha/main.ini", out)
        self.assertRegex(out, r"pending +\[Video\]gpu +here 0, hub 2 +apply writes it")
        self.assertRegex(out, r"in sync +1 key")
        self.assertIn("summary: 1 pending", out)

    def test_in_sync_after_apply_exits_0(self) -> None:
        self.run_apply()
        code, out = self.run_audit()
        self.assertEqual(code, 0)
        self.assertTrue(out.endswith("summary: everything in sync\n"), out)

    def test_changed_here_is_not_in_sync(self) -> None:
        self.run_apply()
        self.put(ALPHA, self.text(ALPHA).replace("gpu = 2", "gpu = 7"))
        code, out = self.run_audit()
        self.assertEqual(code, 1)
        self.assertRegex(out, r"changed here +\[Video\]gpu +here 7, hub 2 +capture proposes it")
        self.assertIn("summary: 1 changed here", out)

    def test_a_key_not_on_the_hub_is_grouped_until_all(self) -> None:
        self.run_apply()
        self.put(ALPHA, self.text(ALPHA).replace("res = 1", "res = 5"))
        code, out = self.run_audit()
        self.assertEqual(code, 1)
        self.assertIn("1 key not on the hub (--all lists them)", out)
        _, full = self.run_audit(all_keys=True)
        self.assertRegex(full, r"changed here +\[Video\]res +here 5")

    def test_cannot_write(self) -> None:
        self.layer("deck", '[beta.config]\nlist = "x"\n')
        code, out = self.run_audit()
        self.assertEqual(code, 1)
        self.assertRegex(out, r"cannot write +list")

    def test_running_emulator_is_reported_and_does_not_fail(self) -> None:
        self.run_apply()
        self.fake_process(4001, "alpha")
        self.put(ALPHA, self.text(ALPHA).replace("gpu = 2", "gpu = 7"))
        code, out = self.run_audit()
        self.assertEqual(code, 0)
        self.assertIn("alpha  running: its files may not hold its latest changes", out)
        self.assertNotIn("gpu", out)

    def test_missing_files(self) -> None:
        (self.home / BETA).unlink()
        self.layer("deck", '[gamma."opt:Flycast"]\nreicast_region = "Europe"\n')
        _, out = self.run_audit()
        self.assertIn("gamma  opt:Flycast  no file (apply creates it)", out)
        self.assertIn("beta  config  no file", out)
        self.assertNotIn("invalid", out)

    def test_data_file_rows(self) -> None:
        self.put("data/gamma/eeprom.bin", b"old")
        self.run_apply()
        self.hub_file("gamma/eeprom", b"hub")
        code, out = self.run_audit()
        self.assertEqual(code, 1)
        self.assertIn(
            "gamma  eeprom  pending: the hub copy differs, unchanged here since the last apply", out
        )
        self.run_apply()
        self.put("data/gamma/eeprom.bin", b"mine")
        self.hub_file("gamma/eeprom", b"hub v2")
        _, out = self.run_audit()
        self.assertIn("changed here: changed here and the hub copy differs", out)
        self.assertNotIn("6875", out)  # no hash, no content

    def test_a_file_that_cannot_be_read_is_invalid(self) -> None:
        self.put(BETA, "{ nope")
        code, out = self.run_audit()
        self.assertEqual(code, 1)
        self.assertRegex(out, r"beta  config  invalid: cannot be parsed")

    def test_an_invalid_layer_exits_1(self) -> None:
        self.layer("deck", '[alpha.main]\n"[Web]token" = "x"\n')
        code, out = self.run_audit()
        self.assertEqual(code, 1)
        self.assertIn("invalid", out)
        self.assertNotIn('"x"', out)

    def test_no_settings_folder_is_not_drift(self) -> None:
        (self.folder / ".stfolder").rmdir()
        code, out = self.run_audit()
        self.assertEqual(code, 0)
        self.assertIn("no settings folder", out)

    def test_emulator_filter(self) -> None:
        _, out = self.run_audit(emulators=["beta"])
        self.assertNotIn("alpha", out)
        with self.assertRaises(ConfigError):
            self.run_audit(emulators=["nope"])

    def test_writes_nothing_state_included(self) -> None:
        before = {p: p.read_bytes() for p in self.home.rglob("*") if p.is_file()}
        self.run_audit()
        self.run_audit(all_keys=True)
        self.assertFalse(self.state.exists())
        self.assertEqual(before, {p: p.read_bytes() for p in self.home.rglob("*") if p.is_file()})
        self.run_apply()
        snapshots = {p: p.read_bytes() for p in self.state.rglob("*") if p.is_file()}
        self.run_audit()
        self.assertEqual(
            snapshots, {p: p.read_bytes() for p in self.state.rglob("*") if p.is_file()}
        )

    def test_fake_tokens_never_appear(self) -> None:
        self.run_apply()
        self.put(ALPHA, self.text(ALPHA).replace(FAKE_TOKEN, "tok-OTHER-FAKE-99"))  # gitleaks:allow
        _, out = self.run_audit(all_keys=True)
        for secret in (FAKE_TOKEN, FAKE_USER, "tok-OTHER-FAKE-99", "[Web]"):  # gitleaks:allow
            self.assertNotIn(secret, out)

    def test_works_without_proc(self) -> None:
        self.proc.path.rmdir()
        code, out = self.run_audit()
        self.assertEqual(code, 1)
        self.assertIn("pending", out)


if __name__ == "__main__":
    unittest.main()
