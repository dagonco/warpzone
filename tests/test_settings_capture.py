"""`settings capture`: the sync check, the prompts, the decision flags and the writes."""

import hashlib
import io
import tempfile
import unittest
from pathlib import Path
from typing import Any

from settings_support import ALPHA_INI, BETA_JSON, FAKE_TOKEN, FAKE_USER, SettingsCase, profile
from warpzone import settings
from warpzone.config import ConfigError

ALPHA = ".config/alpha/main.ini"
BETA = ".config/beta/config.json"
EEPROM = "data/gamma/eeprom.bin"

COMMON = """
[alpha.main]
"[Video]gpu" = "2"
"[System]language" = "4"
"""

IN_SYNC = {"version": ["X:1"], "deleted": False, "invalid": False}


class StubClient:
    """Just the two Syncthing calls capture makes."""

    def __init__(self, status: dict[str, Any] | None = None) -> None:
        self.status = {"state": "idle", "needTotalItems": 0} if status is None else status
        self.files: dict[str, dict[str, Any] | None] = {}
        self.calls: list[tuple[str, str]] = []

    def db_status(self, folder_id: str) -> dict[str, Any]:
        self.calls.append(("status", folder_id))
        return self.status

    def db_file(self, folder_id: str, name: str) -> dict[str, Any] | None:
        self.calls.append(("file", name))
        return self.files.get(name)


class SyncProblemTests(unittest.TestCase):
    def test_idle_with_nothing_to_pull_is_fine(self) -> None:
        status = {"state": "idle", "needTotalItems": 0}
        self.assertEqual(settings.sync_problems(status, {"common.toml": None}), [])

    def test_a_layer_in_sync_is_fine(self) -> None:
        info = {"local": IN_SYNC, "global": IN_SYNC}
        status = {"state": "idle", "needTotalItems": 0}
        self.assertEqual(settings.sync_problems(status, {"common.toml": info}), [])

    def test_busy_folder_is_refused(self) -> None:
        got = settings.sync_problems({"state": "scanning", "needTotalItems": 0}, {})
        self.assertEqual(len(got), 1)
        self.assertIn("scanning", got[0])

    def test_items_to_pull_are_refused(self) -> None:
        got = settings.sync_problems({"state": "idle", "needTotalItems": 3}, {})
        self.assertIn("3", got[0])

    def test_need_counts_are_summed_when_the_total_is_missing(self) -> None:
        status = {"state": "idle", "needFiles": 1, "needDeletes": 1}
        self.assertTrue(settings.sync_problems(status, {}))

    def test_a_folder_error_is_refused(self) -> None:
        got = settings.sync_problems({"state": "idle", "needTotalItems": 0, "error": "boom"}, {})
        self.assertTrue(got)

    def test_local_record_that_differs_from_the_global_one_is_refused(self) -> None:
        newer = {"version": ["X:2"], "deleted": False, "invalid": False}
        status = {"state": "idle", "needTotalItems": 0}
        got = settings.sync_problems(status, {"deck.toml": {"local": IN_SYNC, "global": newer}})
        self.assertEqual(len(got), 1)
        self.assertIn("deck.toml", got[0])

    def test_a_layer_missing_locally_but_present_globally_is_refused(self) -> None:
        status = {"state": "idle", "needTotalItems": 0}
        got = settings.sync_problems(status, {"common.toml": {"local": None, "global": IN_SYNC}})
        self.assertTrue(got)

    def test_an_invalid_local_record_is_refused(self) -> None:
        bad = {"version": ["X:1"], "deleted": False, "invalid": True}
        status = {"state": "idle", "needTotalItems": 0}
        got = settings.sync_problems(status, {"common.toml": {"local": bad, "global": IN_SYNC}})
        self.assertTrue(got)

    def test_deleted_on_both_sides_is_fine(self) -> None:
        gone = {"version": ["X:1"], "deleted": True, "invalid": False}
        status = {"state": "idle", "needTotalItems": 0}
        info = {"local": gone, "global": gone}
        self.assertEqual(settings.sync_problems(status, {"common.toml": info}), [])


class CaptureBase(SettingsCase):
    """A console that applied the layers once and then had two keys changed in the UI."""

    def setUp(self) -> None:
        super().setUp()
        self.put(ALPHA, ALPHA_INI)
        self.put(BETA, BETA_JSON)
        self.layer("common", COMMON)
        self.client = StubClient()
        self.run_apply()
        self.edit(ALPHA, "res = 1", "res = 3")
        self.edit(ALPHA, "gpu = 2", "gpu = 5")

    def edit(self, relative: str, old: str, new: str) -> None:
        path = self.home / relative
        text = path.read_text()
        self.assertIn(old, text)
        path.write_text(text.replace(old, new))

    def capture(
        self,
        answers: list[str] | None = None,
        *,
        client: Any = "default",
        interactive: bool | None = None,
        **kwargs: Any,
    ) -> tuple[int, str, str, list[str]]:
        """-> (code, stdout, stderr, the prompts shown). An exhausted script is end of input."""
        out, err, prompts = io.StringIO(), io.StringIO(), []
        script = iter(answers or [])

        def ask(prompt: str) -> str:
            prompts.append(prompt)
            try:
                return next(script)
            except StopIteration:
                raise EOFError from None

        if client == "default":
            client = self.client
        if interactive is None:
            interactive = answers is not None
        code = settings.capture(
            self.ctx,
            client,
            interactive=interactive,
            ask=ask,
            out=lambda line: out.write(line + "\n"),
            err=lambda line: err.write(line + "\n"),
            **kwargs,
        )
        return code, out.getvalue(), err.getvalue(), prompts

    def layer_text(self, name: str) -> str:
        path = self.folder / f"{name}.toml"
        return path.read_text() if path.exists() else ""

    def every_output(self, *chunks: str) -> str:
        """The outputs given, the layers and the state (the log aside, which a refusal writes)."""
        kept = "".join(
            p.read_text()
            for p in sorted(self.state.rglob("*"))
            if p.is_file() and "backups" not in p.parts and p.name != "log"
        )
        return "".join(chunks) + self.layer_text("common") + self.layer_text("deck") + kept


class PromptTests(CaptureBase):
    def test_enter_takes_the_preselection(self) -> None:
        code, out, _, prompts = self.capture(["", ""])
        self.assertEqual(code, 0)
        # res matches a `device` pattern, gpu is in common
        self.assertIn('"[Video]res" = "3"', self.layer_text("deck"))
        self.assertIn('"[Video]gpu" = "5"', self.layer_text("common"))
        self.assertIn('"[System]language" = "4"', self.layer_text("common"))
        self.assertEqual(len(prompts), 2)
        self.assertIn("[D]evice (deck)", prompts[0])
        self.assertIn("[c]ommon", prompts[0])
        self.assertIn("alpha  main  [Video]gpu", out)
        self.assertEqual(
            self.snapshot("alpha", "main"),
            {"[Video]res": "3", "[Video]gpu": "5", "[System]language": "4"},
        )

    def test_the_capital_letter_follows_the_preselection(self) -> None:
        _, _, _, prompts = self.capture(["q"])
        # candidates come in file order: res (device) first
        self.assertIn("[D]evice", prompts[0])
        self.assertIn("[c]ommon", prompts[0])
        _, _, _, prompts = self.capture(["s", "q"])
        self.assertIn("[C]ommon", prompts[1])
        self.assertIn("[d]evice", prompts[1])

    def test_choice_common(self) -> None:
        code, *_ = self.capture(["c", "c"])
        self.assertEqual(code, 0)
        self.assertIn('"[Video]res" = "3"', self.layer_text("common"))
        self.assertFalse((self.folder / "deck.toml").exists())

    def test_choice_device(self) -> None:
        self.capture(["d", "d"])
        self.assertIn('"[Video]gpu" = "5"', self.layer_text("deck"))
        self.assertIn('"[Video]res" = "3"', self.layer_text("deck"))

    def test_choice_discard_writes_nothing_to_the_hub_but_moves_the_snapshot(self) -> None:
        before = self.layer_text("common")
        self.capture(["x", "x"])
        self.assertEqual(self.layer_text("common"), before)
        self.assertFalse((self.folder / "deck.toml").exists())
        self.assertEqual(self.snapshot("alpha", "main")["[Video]gpu"], "5")

    def test_discard_then_apply_writes_the_layer_value_back(self) -> None:
        self.capture(["x", "x"])
        self.run_apply()
        self.assertIn("gpu = 2", self.text(ALPHA))

    def test_choice_skip_proposes_again(self) -> None:
        self.capture(["s", "s"])
        self.assertFalse((self.folder / "deck.toml").exists())
        self.assertEqual(self.snapshot("alpha", "main")["[Video]res"], "1")
        _, out, _, prompts = self.capture(["q"])
        self.assertEqual(len(prompts), 1)
        self.assertIn("[Video]res", out)

    def test_quit_skips_everything_left(self) -> None:
        self.capture(["c", "q"])
        self.assertIn('"[Video]res" = "3"', self.layer_text("common"))
        self.assertNotIn('"[Video]gpu" = "5"', self.layer_text("common"))
        self.assertEqual(self.snapshot("alpha", "main")["[Video]gpu"], "2")

    def test_end_of_input_quits(self) -> None:
        code, *_ = self.capture(["c"])
        self.assertEqual(code, 0)
        self.assertIn('"[Video]res" = "3"', self.layer_text("common"))
        self.assertEqual(self.snapshot("alpha", "main")["[Video]gpu"], "2")

    def test_an_unknown_answer_is_asked_again(self) -> None:
        _, _, _, prompts = self.capture(["zzz", "q"])
        self.assertEqual(len(prompts), 2)

    def test_common_removes_the_key_from_this_devices_layer(self) -> None:
        self.layer("deck", '[alpha.main]\n"[Video]gpu" = "7"\n')
        self.run_apply(overwrite_local=True)
        self.edit(ALPHA, "gpu = 7", "gpu = 9")
        # res and the new gpu change; answer common for gpu even though the device has it
        _, _, _, prompts = self.capture(["s", "c"])
        self.assertIn("[D]evice", prompts[1])
        self.assertIn('"[Video]gpu" = "9"', self.layer_text("common"))
        self.assertNotIn("gpu", self.layer_text("deck"))

    def test_note_when_another_device_overrides_the_key(self) -> None:
        (self.folder / "tablet.toml").write_text('format = 1\n[alpha.main]\n"[Video]gpu" = "8"\n')
        _, out, _, _ = self.capture(["s", "c"])
        self.assertIn("tablet", out)
        self.assertIn("alpha/main/[Video]gpu", out)

    def test_summary_names_keys_set_and_removed(self) -> None:
        _, out, _, _ = self.capture(["", ""])
        self.assertIn("deck.toml", out)
        self.assertIn("common.toml", out)
        self.assertIn("alpha/main/[Video]res", out)

    def test_prompts_show_values_cut_to_40_characters(self) -> None:
        self.edit(ALPHA, "language = 4", "language = " + "x" * 90)
        _, out, _, _ = self.capture(["q"])
        self.assertNotIn("x" * 41, out)


class NonInteractiveTests(CaptureBase):
    def test_without_a_terminal_or_flags_it_is_a_report(self) -> None:
        before = self.every_output()
        code, out, _, prompts = self.capture()
        self.assertEqual(code, 0)
        self.assertEqual(prompts, [])
        self.assertIn("alpha/main/[Video]gpu", out)
        self.assertIn("[Video]res", out)
        self.assertEqual(self.every_output(), before)
        self.assertEqual(self.snapshot("alpha", "main")["[Video]gpu"], "2")

    def test_yes_takes_the_preselection(self) -> None:
        code, *_ = self.capture(yes=True)
        self.assertEqual(code, 0)
        self.assertIn('"[Video]res" = "3"', self.layer_text("deck"))
        self.assertIn('"[Video]gpu" = "5"', self.layer_text("common"))

    def test_flags_decide_in_order_and_the_rest_waits(self) -> None:
        code, *_ = self.capture(to_device=["alpha/main/[Video]*"], discard=["alpha/main/*"])
        self.assertEqual(code, 0)
        self.assertIn('"[Video]res" = "3"', self.layer_text("deck"))
        self.assertIn('"[Video]gpu" = "5"', self.layer_text("deck"))

    def test_to_common_wins_over_the_other_flags(self) -> None:
        self.capture(to_common=["alpha/main/[Video]gpu"], to_device=["alpha/main/*"])
        self.assertIn('"[Video]gpu" = "5"', self.layer_text("common"))
        self.assertIn('"[Video]res" = "3"', self.layer_text("deck"))

    def test_discard_flag(self) -> None:
        self.capture(discard=["alpha/main/[Video]*"])
        self.assertNotIn("res", self.layer_text("common"))
        self.assertEqual(self.snapshot("alpha", "main")["[Video]res"], "3")

    def test_undecided_keys_are_skipped_without_yes(self) -> None:
        self.capture(to_common=["alpha/main/[Video]gpu"])
        self.assertFalse((self.folder / "deck.toml").exists())
        self.assertEqual(self.snapshot("alpha", "main")["[Video]res"], "1")

    def test_flags_decide_even_on_a_terminal(self) -> None:
        _, _, _, prompts = self.capture(["q"], to_common=["alpha/main/[Video]res"])
        self.assertEqual(len(prompts), 1)
        self.assertIn('"[Video]res" = "3"', self.layer_text("common"))

    def test_dry_run_decides_but_writes_nothing(self) -> None:
        before = self.every_output()
        log = (self.state / "log").read_text()
        code, out, _, _ = self.capture(yes=True, dry_run=True)
        self.assertEqual(code, 0)
        self.assertEqual(self.every_output(), before)
        self.assertIn("would", out)
        self.assertEqual(self.snapshot("alpha", "main")["[Video]gpu"], "2")
        self.assertEqual((self.state / "log").read_text(), log)

    def test_key_patterns_need_seed(self) -> None:
        with self.assertRaises(ConfigError):
            self.capture(key=["alpha/main/*"])

    def test_unknown_emulator_is_a_usage_error(self) -> None:
        with self.assertRaises(ConfigError):
            self.capture(emulators=["nope"])

    def test_emulator_filter(self) -> None:
        self.capture(yes=True, emulators=["beta"])
        self.assertFalse((self.folder / "deck.toml").exists())
        self.assertEqual(self.snapshot("alpha", "main")["[Video]gpu"], "2")


class NewKeyTests(CaptureBase):
    def setUp(self) -> None:
        super().setUp()
        self.edit(ALPHA, "[System]\n", "[System]\nfresh = 1\n")

    def test_new_keys_are_counted_not_shown(self) -> None:
        _, out, _, _ = self.capture(yes=True)
        self.assertNotIn("alpha/main/[System]fresh", out)
        self.assertIn("1 new key", out)
        self.assertEqual(self.snapshot("alpha", "main")["[System]fresh"], "1")

    def test_a_report_does_not_absorb_new_keys(self) -> None:
        self.capture()
        self.assertNotIn("[System]fresh", self.snapshot("alpha", "main"))

    def test_include_new_proposes_them(self) -> None:
        self.capture(yes=True, include_new=True)
        self.assertIn('"[System]fresh" = "1"', self.layer_text("common"))

    def test_a_skipped_new_key_stays_unknown(self) -> None:
        self.capture(["s", "s", "s"], include_new=True)
        self.assertNotIn("[System]fresh", self.snapshot("alpha", "main"))


class SeedTests(SettingsCase):
    def setUp(self) -> None:
        super().setUp()
        self.put(ALPHA, ALPHA_INI)
        self.layer("common", "")

    def capture(self, **kwargs: Any) -> tuple[int, str]:
        out = io.StringIO()
        code = settings.capture(
            self.ctx,
            StubClient(),
            interactive=False,
            out=lambda line: out.write(line + "\n"),
            err=lambda line: out.write(line + "\n"),
            **kwargs,
        )
        return code, out.getvalue()

    def test_a_file_without_a_baseline_is_skipped_with_a_note(self) -> None:
        code, out = self.capture(yes=True)
        self.assertEqual(code, 0)
        self.assertIn("no baseline", out)
        self.assertIn("alpha/main", out)
        self.assertIsNone(self.snapshot("alpha", "main"))

    def test_seed_proposes_known_keys_and_creates_the_baseline(self) -> None:
        code, _ = self.capture(seed=True, yes=True)
        self.assertEqual(code, 0)
        text = (self.folder / "common.toml").read_text()
        self.assertIn('"[System]language" = "1"', text)
        self.assertNotIn("gpu", text)
        self.assertEqual(
            self.snapshot("alpha", "main"),
            {"[Video]res": "1", "[Video]gpu": "0", "[System]language": "1"},
        )

    def test_seed_with_key_patterns(self) -> None:
        self.capture(seed=True, yes=True, key=["alpha/main/[Video]gpu"])
        text = (self.folder / "common.toml").read_text()
        self.assertIn('"[Video]gpu" = "0"', text)
        self.assertIn('"[System]language" = "1"', text)

    def test_seed_in_a_report_writes_no_baseline(self) -> None:
        self.capture(seed=True)
        self.assertIsNone(self.snapshot("alpha", "main"))

    def test_secrets_and_noise_are_never_seeded(self) -> None:
        self.capture(seed=True, yes=True, key=["alpha/main/*"])
        written = (self.folder / "common.toml").read_text() + self.state_text()
        self.assertNotIn(FAKE_TOKEN, written)
        self.assertNotIn(FAKE_USER, written)
        self.assertNotIn("Controls", written)


class RefusalTests(CaptureBase):
    def assertUntouched(self, before: str) -> None:
        self.assertEqual(self.every_output(), before)

    def test_refuses_when_syncthing_is_not_idle(self) -> None:
        before = self.every_output()
        client = StubClient({"state": "syncing", "needTotalItems": 2})
        code, _, err, prompts = self.capture(["c", "c"], client=client)
        self.assertEqual(code, 1)
        self.assertIn("syncing", err)
        self.assertEqual(prompts, [])
        self.assertUntouched(before)

    def test_refuses_when_a_layer_is_behind_the_hub(self) -> None:
        before = self.every_output()
        self.client.files["common.toml"] = {
            "local": IN_SYNC,
            "global": {"version": ["X:2"], "deleted": False, "invalid": False},
        }
        code, _, err, _ = self.capture(yes=True)
        self.assertEqual(code, 1)
        self.assertIn("common.toml", err)
        self.assertUntouched(before)

    def test_asks_syncthing_about_both_layers(self) -> None:
        self.capture(yes=True)
        asked = {name for kind, name in self.client.calls if kind == "file"}
        self.assertEqual(asked, {"common.toml", "deck.toml"})

    def test_a_layer_unknown_to_syncthing_is_fine(self) -> None:
        code, *_ = self.capture(yes=True)
        self.assertEqual(code, 0)

    def test_refuses_with_a_conflict_copy_and_names_it(self) -> None:
        before = self.every_output()
        name = "common.sync-conflict-20260101-000000-ABCDEFG.toml"
        (self.folder / name).write_text("format = 1\n")
        code, _, err, _ = self.capture(yes=True)
        self.assertEqual(code, 1)
        self.assertIn(name, err)
        self.assertUntouched(before)

    def test_offline_skips_syncthing_but_not_the_conflict_check(self) -> None:
        code, *_ = self.capture(yes=True, client=None, offline=True)
        self.assertEqual(code, 0)
        self.assertEqual(self.client.calls, [])
        (self.folder / "deck.sync-conflict-20260101-000000-ABCDEFG.toml").write_text("x")
        self.edit(ALPHA, "gpu = 5", "gpu = 6")
        code, *_ = self.capture(yes=True, client=None, offline=True)
        self.assertEqual(code, 1)

    def test_online_without_a_client_is_refused(self) -> None:
        with self.assertRaises(settings.SettingsError):
            self.capture(yes=True, client=None)

    def test_an_invalid_layer_is_refused(self) -> None:
        (self.folder / "common.toml").write_text("format = 9\n")
        code, _, err, _ = self.capture(yes=True)
        self.assertEqual(code, 1)
        self.assertIn("common.toml", err)

    def test_refuses_when_a_layer_changed_between_load_and_write(self) -> None:
        marker = '"[Video]gpu" = "99"'

        def sabotage(prompt: str) -> str:
            if marker not in self.layer_text("common"):
                with (self.folder / "common.toml").open("a") as fh:
                    fh.write(marker + "\n")
            return "c"

        out, err = io.StringIO(), io.StringIO()
        code = settings.capture(
            self.ctx,
            self.client,
            interactive=True,
            ask=sabotage,
            out=lambda line: out.write(line + "\n"),
            err=lambda line: err.write(line + "\n"),
        )
        self.assertEqual(code, 1)
        self.assertIn("changed", err.getvalue())
        self.assertFalse((self.folder / "deck.toml").exists())
        self.assertEqual(self.layer_text("common").count("99"), 1)
        self.assertEqual(self.snapshot("alpha", "main")["[Video]res"], "1")

    def test_no_settings_folder_is_a_refusal(self) -> None:
        (self.folder / ".stfolder").rmdir()
        code, _, err, _ = self.capture(yes=True)
        self.assertEqual(code, 1)
        self.assertIn("no settings folder", err)

    def test_running_emulator_is_skipped_with_a_note(self) -> None:
        self.fake_process(7, "alpha")
        code, out, _, _ = self.capture(yes=True)
        self.assertEqual(code, 0)
        self.assertIn("running", out)
        self.assertFalse((self.folder / "deck.toml").exists())
        self.assertEqual(self.snapshot("alpha", "main")["[Video]gpu"], "2")

    def test_another_run_holding_the_lock_is_a_refusal(self) -> None:
        with settings.state.lock(root=self.state) as got:
            self.assertTrue(got)
            code, _, err, _ = self.capture(yes=True)
        self.assertEqual(code, 1)
        self.assertIn("in progress", err)

    def test_an_unparsable_file_is_an_error_and_the_rest_goes_on(self) -> None:
        self.put(BETA, "{ not json")
        code, _, err, _ = self.capture(yes=True)
        self.assertEqual(code, 1)
        self.assertIn("beta/config", err)
        self.assertIn('"[Video]gpu" = "5"', self.layer_text("common"))


class DataFileTests(CaptureBase):
    def setUp(self) -> None:
        super().setUp()
        self.put(EEPROM, b"EEPROM-v1")
        self.run_apply()
        self.put(EEPROM, b"EEPROM-v2-" + b"\x00\x01" * 8)
        self.digest = hashlib.sha256((self.home / EEPROM).read_bytes()).hexdigest()

    def test_prompt_shows_size_and_hash_prefix_never_content(self) -> None:
        code, out, _, prompts = self.capture(["q"], emulators=["gamma"])
        self.assertEqual(code, 0)
        shown = out + "".join(prompts)
        self.assertIn(self.digest[:12], shown)
        self.assertNotIn(self.digest[:13], shown)
        self.assertIn("26", shown)
        self.assertNotIn("EEPROM", shown)
        self.assertNotIn("evice", prompts[0])

    def test_accepted_data_file_is_copied_and_hashed(self) -> None:
        code, *_ = self.capture(["c"], emulators=["gamma"])
        self.assertEqual(code, 0)
        copy = self.folder / "files" / "gamma" / "eeprom"
        self.assertEqual(copy.read_bytes(), (self.home / EEPROM).read_bytes())
        self.assertEqual(
            settings.state.Snapshots(self.state).load_data("gamma"), {"eeprom": self.digest}
        )
        self.assertFalse(list(self.folder.rglob("*.wz-tmp")))

    def test_device_is_not_offered_for_a_data_file(self) -> None:
        _, _, _, prompts = self.capture(["d", "q"], emulators=["gamma"])
        self.assertEqual(len(prompts), 2)
        self.assertFalse((self.folder / "files").exists())

    def test_discard_and_skip(self) -> None:
        self.capture(["x"], emulators=["gamma"])
        self.assertFalse((self.folder / "files").exists())
        snap = settings.state.Snapshots(self.state).load_data("gamma")
        self.assertEqual(snap, {"eeprom": self.digest})

    def test_skip_keeps_the_snapshot(self) -> None:
        self.capture(["s"], emulators=["gamma"])
        snap = settings.state.Snapshots(self.state).load_data("gamma")
        self.assertNotEqual(snap, {"eeprom": self.digest})

    def test_to_device_does_not_apply_to_a_data_file(self) -> None:
        self.capture(to_device=["gamma/data/*"], emulators=["gamma"])
        self.assertFalse((self.folder / "files").exists())

    def test_yes_copies_it_and_a_second_run_proposes_nothing(self) -> None:
        self.capture(yes=True, emulators=["gamma"])
        _, out, _, prompts = self.capture(["q"], emulators=["gamma"])
        self.assertEqual(prompts, [])
        self.assertNotIn("eeprom", out)

    def test_a_hub_copy_that_changed_meanwhile_is_refused(self) -> None:
        self.hub_file("gamma/eeprom", b"old-hub")

        def sabotage(prompt: str) -> str:
            (self.folder / "files" / "gamma" / "eeprom").write_bytes(b"newer-hub")
            return "c"

        err = io.StringIO()
        code = settings.capture(
            self.ctx,
            self.client,
            emulators=["gamma"],
            interactive=True,
            ask=sabotage,
            out=lambda line: None,
            err=lambda line: err.write(line + "\n"),
        )
        self.assertEqual(code, 1)
        self.assertEqual((self.folder / "files" / "gamma" / "eeprom").read_bytes(), b"newer-hub")

    def test_a_file_over_1_mib_is_left_out_with_a_note(self) -> None:
        self.put(EEPROM, b"x" * (settings.MAX_DATA_BYTES + 1))
        _, out, _, prompts = self.capture(yes=True, emulators=["gamma"])
        self.assertEqual(prompts, [])
        self.assertIn("1 MiB", out)
        self.assertFalse((self.folder / "files").exists())


class SecretTests(CaptureBase):
    def test_fake_secrets_never_reach_prompts_layers_snapshots_or_output(self) -> None:
        self.edit(ALPHA, FAKE_TOKEN, FAKE_TOKEN + "-rotated")
        self.edit(ALPHA, FAKE_USER, FAKE_USER + "-2")
        code, out, err, prompts = self.capture(["", ""], include_new=True)
        self.assertEqual(code, 0)
        everything = self.every_output(out, err, *prompts)
        self.assertNotIn("-rotated", everything)
        self.assertNotIn(FAKE_USER, everything)
        self.assertNotIn(FAKE_TOKEN, everything)

    def test_flags_that_match_secret_keys_do_nothing(self) -> None:
        self.edit(ALPHA, FAKE_TOKEN, "tok-OTHER-1")  # gitleaks:allow
        self.capture(to_common=["*"], to_device=["*"])
        self.assertNotIn("tok-OTHER-1", self.every_output())  # gitleaks:allow
        self.assertNotIn("token", self.layer_text("common"))


class EndToEndTests(CaptureBase):
    def test_apply_change_capture_then_a_second_device_applies_it(self) -> None:
        self.capture(["", ""])
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            env = settings.Env(root / "home", root / "state", self.proc, lambda: self.clock_value)
            env.home.mkdir()
            ctx = settings.make_context("tablet", profile(), env)
            ctx.folder.mkdir(parents=True)
            (ctx.folder / ".stfolder").mkdir()
            for name in ("common.toml", "deck.toml"):
                if (self.folder / name).exists():
                    (ctx.folder / name).write_text((self.folder / name).read_text())
            path = env.home / ALPHA
            path.parent.mkdir(parents=True)
            path.write_text(ALPHA_INI)
            self.assertEqual(settings.apply(ctx, out=lambda _: None, err=lambda _: None), 0)
            text = path.read_text()
            self.assertIn("gpu = 5", text)  # captured to common here, applied there
            self.assertIn("res = 1", text)  # captured to deck's layer: not for the tablet
            self.assertIn("language = 4", text)

    def test_a_change_made_on_the_hub_side_reaches_the_first_console_again(self) -> None:
        self.capture(["", ""])
        (self.folder / "common.toml").write_text(
            self.layer_text("common").replace('"[Video]gpu" = "5"', '"[Video]gpu" = "6"')
        )
        self.run_apply()
        self.assertIn("gpu = 6", self.text(ALPHA))
        self.assertIn("res = 3", self.text(ALPHA))


if __name__ == "__main__":
    unittest.main()
