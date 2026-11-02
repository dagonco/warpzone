"""`settings apply`: the writing runner, on synthetic profiles and files."""

import hashlib
import os
import time
import unittest
from unittest import mock

from settings_support import ALPHA_INI, BETA_JSON, FAKE_TOKEN, FAKE_USER, SettingsCase
from warpzone import settings
from warpzone.config import ConfigError

ALPHA = ".config/alpha/main.ini"
BETA = ".config/beta/config.json"

COMMON = """
[alpha.main]
"[Video]gpu" = "2"
"[System]language" = "4"
"""
DEVICE_LAYER = """
[alpha.main]
"[Video]res" = "3"

[beta.config]
"video.scale" = 2
"""


class ApplyBase(SettingsCase):
    def setUp(self) -> None:
        super().setUp()
        self.put(ALPHA, ALPHA_INI)
        self.put(BETA, BETA_JSON)
        self.layer("common", COMMON)
        self.layer("deck", DEVICE_LAYER)


class FirstApplyTests(ApplyBase):
    def test_writes_only_layered_keys_and_makes_the_snapshot(self) -> None:
        code, out, err = self.run_apply()
        self.assertEqual((code, err), (0, []))
        want = ALPHA_INI.replace("res = 1", "res = 3").replace("gpu = 0", "gpu = 2")
        want = want.replace("language = 1", "language = 4")
        self.assertEqual(self.text(ALPHA), want)
        self.assertEqual(self.text(BETA), BETA_JSON.replace('"scale": 1', '"scale": 2'))
        snap = self.snapshot("alpha", "main")
        self.assertEqual(
            snap,
            {"[Video]res": "3", "[Video]gpu": "2", "[System]language": "4"},
        )  # noise and secret keys stay out
        self.assertEqual(len(self.backups("alpha", "main")), 1)
        self.assertEqual(self.backups("alpha", "main")[0].read_text(), ALPHA_INI)
        self.assertTrue(any("wrote" in line and "[Video]gpu" in line for line in out))

    def test_records_the_run_for_doctor(self) -> None:
        self.run_apply()
        info = settings.state.read_last_apply(root=self.state)
        assert info is not None
        self.assertEqual(info["exit"], 0)
        self.assertEqual(info["written"], 4)
        self.assertEqual(info["files"], 2)
        self.assertIn("time", info)

    def test_second_run_writes_nothing(self) -> None:
        self.run_apply()
        before = {
            p: p.stat().st_mtime_ns for p in (self.home / ".config").rglob("*") if p.is_file()
        }
        snap_bytes = (self.state / "snapshots" / "alpha" / "main.json").read_bytes()
        backups = self.backups("alpha", "main")
        code, out, _ = self.run_apply()
        self.assertEqual(code, 0)
        self.assertFalse([line for line in out if line.startswith(("wrote", "held"))])
        after = {p: p.stat().st_mtime_ns for p in (self.home / ".config").rglob("*") if p.is_file()}
        self.assertEqual(before, after)
        self.assertEqual(
            (self.state / "snapshots" / "alpha" / "main.json").read_bytes(), snap_bytes
        )
        self.assertEqual(self.backups("alpha", "main"), backups)

    def test_device_layer_wins_over_common(self) -> None:
        self.layer("common", '[alpha.main]\n"[Video]res" = "9"\n')
        self.run_apply()
        self.assertIn("res = 3", self.text(ALPHA))

    def test_emulator_filter(self) -> None:
        self.run_apply(emulators=["beta"])
        self.assertEqual(self.text(ALPHA), ALPHA_INI)
        self.assertIn('"scale": 2', self.text(BETA))

    def test_unknown_emulator_is_a_usage_error(self) -> None:
        with self.assertRaises(ConfigError):
            self.run_apply(emulators=["nope"])

    def test_quiet_prints_nothing(self) -> None:
        code, out, err = self.run_apply(quiet=True)
        self.assertEqual((code, out, err), (0, [], []))
        self.assertIn("gpu = 2", self.text(ALPHA))


class HeldTests(ApplyBase):
    def setUp(self) -> None:
        super().setUp()
        self.run_apply()
        self.put(ALPHA, self.text(ALPHA).replace("gpu = 2", "gpu = 7"))  # changed in the GUI

    def test_a_change_made_here_is_held(self) -> None:
        code, out, _ = self.run_apply()
        self.assertEqual(code, 0)
        self.assertIn("gpu = 7", self.text(ALPHA))
        self.assertTrue(any(line.startswith("held") and "[Video]gpu" in line for line in out))
        self.assertEqual(self.snapshot("alpha", "main")["[Video]gpu"], "2")
        self.assertIn("held", (self.state / "log").read_text())
        # and it stays held on every later run
        self.run_apply()
        self.assertIn("gpu = 7", self.text(ALPHA))
        self.assertEqual(self.snapshot("alpha", "main")["[Video]gpu"], "2")

    def test_overwrite_local_writes_it(self) -> None:
        code, out, _ = self.run_apply(overwrite_local=True)
        self.assertEqual(code, 0)
        self.assertIn("gpu = 2", self.text(ALPHA))
        self.assertEqual(self.snapshot("alpha", "main")["[Video]gpu"], "2")
        self.assertTrue(any("overwrites" in line for line in out))

    def test_a_hub_change_is_written_with_a_backup(self) -> None:
        self.put(ALPHA, self.text(ALPHA).replace("gpu = 7", "gpu = 2"))  # back to what was synced
        self.layer("common", '[alpha.main]\n"[Video]gpu" = "5"\n')
        before = len(self.backups("alpha", "main"))
        self.clock_value = self.clock_value.replace(second=9)
        self.run_apply()
        self.assertIn("gpu = 5", self.text(ALPHA))
        self.assertEqual(len(self.backups("alpha", "main")), before + 1)

    def test_a_changed_key_that_is_not_layered_stays_visible(self) -> None:
        self.put(
            ALPHA,
            self.text(ALPHA).replace(
                "[System]\nlanguage = 4", "[System]\nlanguage = 4\nregion = 1"
            ),
        )
        self.layer("common", '[alpha.main]\n"[Video]gpu" = "2"\n')  # language is no longer layered
        self.run_apply()
        self.put(ALPHA, self.text(ALPHA).replace("language = 4", "language = 9"))
        self.run_apply()
        self.run_apply()
        self.assertEqual(self.snapshot("alpha", "main")["[System]language"], "4")


class SkipTests(ApplyBase):
    def test_a_running_emulator_is_left_alone(self) -> None:
        self.fake_process(4001, "alpha")
        code, out, _ = self.run_apply()
        self.assertEqual(code, 0)
        self.assertEqual(self.text(ALPHA), ALPHA_INI)
        self.assertIsNone(self.snapshot("alpha", "main"))
        self.assertIn('"scale": 2', self.text(BETA))
        self.assertTrue(any(line.startswith("running") and "alpha" in line for line in out))
        self.assertIn("running", (self.state / "log").read_text())

    def test_a_launcher_script_does_not_count(self) -> None:
        self.fake_process(4001, "bash", "bash /x/alpha.sh")
        self.run_apply()
        self.assertIn("gpu = 2", self.text(ALPHA))

    def test_the_emulator_starts_between_backup_and_replace(self) -> None:
        real = settings.state.backup

        def backup_then_start(*args: object, **kwargs: object):
            result = real(*args, **kwargs)
            self.fake_process(4001, "alpha")
            return result

        with mock.patch.object(settings.state, "backup", backup_then_start):
            code, out, _ = self.run_apply(emulators=["alpha"])
        self.assertEqual(code, 0)
        self.assertEqual(self.text(ALPHA), ALPHA_INI)
        self.assertIsNone(self.snapshot("alpha", "main"))
        self.assertTrue(any(line.startswith("skipped") for line in out))

    def test_a_file_modified_between_read_and_write_is_skipped(self) -> None:
        real = settings.state.backup
        edited = ALPHA_INI.replace("language = 1", "language = 8")

        def backup_then_edit(*args: object, **kwargs: object):
            result = real(*args, **kwargs)
            self.put(ALPHA, edited)
            return result

        with mock.patch.object(settings.state, "backup", backup_then_edit):
            code, out, _ = self.run_apply(emulators=["alpha"])
        self.assertEqual(code, 0)
        self.assertEqual(self.text(ALPHA), edited)
        self.assertIsNone(self.snapshot("alpha", "main"))
        self.assertTrue(any(line.startswith("skipped") for line in out))
        self.assertEqual(self.backups("alpha", "main")[0].read_text(), ALPHA_INI)
        self.run_apply(emulators=["alpha"])  # the next run catches up
        self.assertIn("gpu = 2", self.text(ALPHA))

    def test_a_missing_file_is_skipped(self) -> None:
        (self.home / BETA).unlink()
        code, _, _ = self.run_apply()
        self.assertEqual(code, 0)
        self.assertFalse((self.home / BETA).exists())
        self.assertIsNone(self.snapshot("beta", "config"))

    def test_no_proc_refuses(self) -> None:
        self.proc.rmdir()
        with self.assertRaises(settings.SettingsError):
            self.run_apply()
        self.assertEqual(self.text(ALPHA), ALPHA_INI)


class LayerTests(ApplyBase):
    def assert_untouched(self) -> None:
        self.assertEqual(self.text(ALPHA), ALPHA_INI)
        self.assertEqual(self.text(BETA), BETA_JSON)
        self.assertEqual(self.backups("alpha", "main"), [])
        self.assertIsNone(self.snapshot("alpha", "main"))

    def test_an_invalid_device_layer_changes_nothing(self) -> None:
        self.folder.joinpath("deck.toml").write_text("format = 2\n")
        code, _, err = self.run_apply()
        self.assertEqual(code, 1)
        self.assert_untouched()
        self.assertTrue(any("deck.toml" in line for line in err))
        info = settings.state.read_last_apply(root=self.state)
        assert info is not None
        self.assertEqual(info["exit"], 1)

    def test_a_layer_that_is_not_toml_changes_nothing(self) -> None:
        self.folder.joinpath("common.toml").write_text("format = [\n")
        code, _, _ = self.run_apply()
        self.assertEqual(code, 1)
        self.assert_untouched()

    def test_a_secret_key_in_a_layer_is_never_applied(self) -> None:
        self.layer("deck", f'[alpha.main]\n"[Web]token" = "{FAKE_TOKEN}"\n')
        code, out, err = self.run_apply()
        self.assertEqual(code, 1)
        self.assert_untouched()
        text = "\n".join(out + err)
        self.assertIn("[Web]token", text)  # named
        self.assertNotIn(FAKE_TOKEN, text)  # never its value

    def test_missing_layers_are_fine(self) -> None:
        self.folder.joinpath("common.toml").unlink()
        self.folder.joinpath("deck.toml").unlink()
        code, _, _ = self.run_apply()
        self.assertEqual(code, 0)
        self.assertEqual(self.text(ALPHA), ALPHA_INI)
        self.assertIsNotNone(self.snapshot("alpha", "main"))  # the baseline for capture

    def test_missing_settings_folder_exits_0(self) -> None:
        (self.folder / ".stfolder").rmdir()
        code, _, _ = self.run_apply()
        self.assertEqual(code, 0)
        self.assertEqual(self.text(ALPHA), ALPHA_INI)
        self.assertIn("no settings folder", (self.state / "log").read_text())

    def test_conflict_copies_are_ignored(self) -> None:
        self.folder.joinpath("common.sync-conflict-20260101-000000-ABCDEFG.toml").write_text(
            "junk ["
        )
        code, out, _ = self.run_apply()
        self.assertEqual(code, 0)
        self.assertTrue(any("conflict copy" in line for line in out))
        self.assertIn("gpu = 2", self.text(ALPHA))

    def test_an_unknown_emulator_is_only_a_warning(self) -> None:
        self.layer("deck", DEVICE_LAYER + '\n[other.cfg]\nk = "v"\n')
        code, out, _ = self.run_apply()
        self.assertEqual(code, 0)
        self.assertTrue(any(line.startswith("warning") for line in out))


class CannotWriteTests(ApplyBase):
    def test_fixed_and_missing_keys_are_reported_not_failed(self) -> None:
        self.layer("deck", '[beta.config]\nlist = "x"\n"video.new" = 1\n"video.scale" = 3\n')
        code, out, _ = self.run_apply()
        self.assertEqual(code, 0)
        self.assertEqual(sum(1 for line in out if line.startswith("cannot write")), 2)
        self.assertIn('"scale": 3', self.text(BETA))


class UnreadableTests(ApplyBase):
    def test_a_file_that_is_not_utf8_fails_alone(self) -> None:
        self.put(ALPHA, b"\xff\xfe[Video]\n")
        code, _, err = self.run_apply()
        self.assertEqual(code, 1)
        self.assertEqual((self.home / ALPHA).read_bytes(), b"\xff\xfe[Video]\n")
        self.assertIn('"scale": 2', self.text(BETA))  # the other file still applies
        self.assertTrue(any("alpha/main" in line for line in err))

    def test_a_broken_json_file_fails_alone(self) -> None:
        self.put(BETA, "{ nope")
        code, _, _ = self.run_apply()
        self.assertEqual(code, 1)
        self.assertEqual(self.text(BETA), "{ nope")
        self.assertIn("gpu = 2", self.text(ALPHA))

    def test_a_corrupt_snapshot_stops_that_file_only(self) -> None:
        self.run_apply()
        path = self.state / "snapshots" / "alpha" / "main.json"
        path.write_text("{ not json")
        self.layer("common", '[alpha.main]\n"[Video]gpu" = "5"\n')
        code, _, err = self.run_apply()
        self.assertEqual(code, 1)
        self.assertEqual(path.read_text(), "{ not json")
        self.assertIn("gpu = 2", self.text(ALPHA))
        self.assertTrue(any("snapshot is corrupt" in line for line in err))


class TemplatedAndDataTests(ApplyBase):
    def test_a_templated_file_is_created_when_a_layer_names_it(self) -> None:
        self.layer("deck", '[gamma."opt:Flycast"]\nreicast_region = "Europe"\n')
        code, out, _ = self.run_apply()
        self.assertEqual(code, 0)
        opt = self.home / ".config/gamma/Flycast/Flycast.opt"
        self.assertEqual(opt.read_text(), 'reicast_region = "Europe"\n')
        self.assertEqual(self.backups("gamma", "opt:Flycast"), [])  # nothing to back up
        self.assertEqual(self.snapshot("gamma", "opt:Flycast"), {"reicast_region": "Europe"})
        self.assertTrue(any(line.startswith("created") for line in out))
        self.run_apply()  # idempotent
        self.assertEqual(opt.read_text(), 'reicast_region = "Europe"\n')

    def test_existing_instances_are_found_and_written(self) -> None:
        self.put(".config/gamma/Dolphin/Dolphin.opt", 'region = "Asia"\n')
        self.put(".config/gamma/Other/Different.opt", 'region = "Asia"\n')  # name mismatch
        self.layer("deck", '[gamma."opt:Dolphin"]\nregion = "Europe"\n')
        self.run_apply()
        self.assertEqual(self.text(".config/gamma/Dolphin/Dolphin.opt"), 'region = "Europe"\n')
        self.assertIsNotNone(self.snapshot("gamma", "opt:Dolphin"))
        self.assertIsNone(self.snapshot("gamma", "opt:Other"))

    def test_a_file_without_create_is_not_created(self) -> None:
        self.layer("deck", '[gamma.cfg]\nk = "v"\n')
        self.run_apply()
        self.assertFalse((self.home / ".config/gamma/gamma.cfg").exists())

    def test_data_file_is_delivered_with_a_backup(self) -> None:
        self.put("data/gamma/eeprom.bin", b"old")
        self.run_apply()  # baseline: nothing on the hub yet
        self.hub_file("gamma/eeprom", b"new-hub-copy")
        code, out, _ = self.run_apply()
        self.assertEqual(code, 0)
        self.assertEqual((self.home / "data/gamma/eeprom.bin").read_bytes(), b"new-hub-copy")
        self.assertEqual(self.backups("gamma", "data:eeprom")[0].read_bytes(), b"old")
        want = hashlib.sha256(b"new-hub-copy").hexdigest()
        self.assertEqual(settings.state.Snapshots(self.state).load_data("gamma"), {"eeprom": want})
        self.assertTrue(any("gamma/data/eeprom" in line and "wrote" in line for line in out))
        before = (self.home / "data/gamma/eeprom.bin").stat().st_mtime_ns
        self.run_apply()
        self.assertEqual((self.home / "data/gamma/eeprom.bin").stat().st_mtime_ns, before)

    def test_data_file_changed_here_is_held(self) -> None:
        self.put("data/gamma/eeprom.bin", b"old")
        self.run_apply()
        self.hub_file("gamma/eeprom", b"hub")
        self.run_apply()
        self.put("data/gamma/eeprom.bin", b"changed in the emulator")
        self.hub_file("gamma/eeprom", b"hub v2")
        _, out, _ = self.run_apply()
        self.assertEqual(
            (self.home / "data/gamma/eeprom.bin").read_bytes(), b"changed in the emulator"
        )
        self.assertTrue(any(line.startswith("held") and "eeprom" in line for line in out))
        self.run_apply(overwrite_local=True)
        self.assertEqual((self.home / "data/gamma/eeprom.bin").read_bytes(), b"hub v2")

    def test_data_file_is_created_only_under_create_when(self) -> None:
        self.hub_file("gamma/eeprom", b"hub")
        self.run_apply()
        self.assertFalse((self.home / "data/gamma/eeprom.bin").exists())
        (self.home / "data/gamma").mkdir(parents=True)
        self.run_apply()
        self.assertEqual((self.home / "data/gamma/eeprom.bin").read_bytes(), b"hub")

    def test_a_data_file_over_1_mib_is_an_error(self) -> None:
        self.put("data/gamma/eeprom.bin", b"old")
        self.hub_file("gamma/eeprom", b"x" * (settings.MAX_DATA_BYTES + 1))
        code, _, _ = self.run_apply()
        self.assertEqual(code, 1)
        self.assertEqual((self.home / "data/gamma/eeprom.bin").read_bytes(), b"old")

    def test_a_running_emulator_keeps_its_data(self) -> None:
        self.put("data/gamma/eeprom.bin", b"old")
        self.hub_file("gamma/eeprom", b"hub")
        self.fake_process(4002, "gamma")
        self.run_apply()
        self.assertEqual((self.home / "data/gamma/eeprom.bin").read_bytes(), b"old")


class LockAndDryRunTests(ApplyBase):
    def test_a_held_lock_exits_0_and_logs(self) -> None:
        with settings.state.lock(root=self.state) as got:
            self.assertTrue(got)
            code, _, _ = self.run_apply()
        self.assertEqual(code, 0)
        self.assertEqual(self.text(ALPHA), ALPHA_INI)
        self.assertIn("lock", (self.state / "log").read_text())

    def test_dry_run_writes_nothing_at_all(self) -> None:
        self.hub_file("gamma/eeprom", b"hub")
        (self.home / "data/gamma").mkdir(parents=True)
        code, out, _ = self.run_apply(dry_run=True)
        self.assertEqual(code, 0)
        self.assertEqual(self.text(ALPHA), ALPHA_INI)
        self.assertEqual(self.text(BETA), BETA_JSON)
        self.assertFalse(self.state.exists())
        self.assertFalse((self.home / "data/gamma/eeprom.bin").exists())
        self.assertFalse(list(self.home.rglob("*.wz-tmp")))
        self.assertTrue(any(line.startswith("would write") for line in out))

    def test_dry_run_does_not_need_the_lock(self) -> None:
        with settings.state.lock(root=self.state):
            code, out, _ = self.run_apply(dry_run=True)
        self.assertEqual(code, 0)
        self.assertTrue(any(line.startswith("would write") for line in out))


class SecretTests(ApplyBase):
    def test_fake_tokens_never_reach_output_log_or_snapshots(self) -> None:
        self.layer("deck", '[alpha.main]\n"[Video]res" = "3"\n')
        for kwargs in ({}, {"overwrite_local": True}, {"dry_run": True}):
            _, out, err = self.run_apply(**kwargs)
            for line in out + err:
                self.assertNotIn(FAKE_TOKEN, line)
                self.assertNotIn(FAKE_USER, line)
        state_text = self.state_text()
        self.assertNotIn(FAKE_TOKEN, state_text)
        self.assertNotIn(FAKE_USER, state_text)
        self.assertNotIn("[Web]", state_text)  # not even the key names
        self.assertIn(FAKE_TOKEN, self.text(ALPHA))  # and the file keeps them


class SpeedTests(ApplyBase):
    @unittest.skipIf(os.environ.get("WARPZONE_SKIP_TIMING"), "timing test disabled")
    def test_apply_is_fast(self) -> None:
        self.run_apply()
        started = time.perf_counter()
        for _ in range(3):
            self.run_apply()
        self.assertLess((time.perf_counter() - started) / 3, 0.5)


if __name__ == "__main__":
    unittest.main()
