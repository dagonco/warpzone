"""The pieces of `warpzone self-update` that are not the updater script: version keys, the
settings file written for the script, the live copy and the wrapper command."""

import tempfile
import unittest
from pathlib import Path
from typing import ClassVar

from warpzone import selfupdate

UPDATER = Path(__file__).resolve().parent.parent / "warpzone" / "data" / "updater.sh"
HOME = Path("/home/deck")


class PathTests(unittest.TestCase):
    def test_the_live_copy_is_outside_the_clone_under_the_share_folder(self) -> None:
        self.assertEqual(
            selfupdate.live_path(HOME), HOME / ".local/share/warpzone/updater/updater.sh"
        )

    def test_the_settings_and_the_state_sit_in_the_updater_folder_of_the_state_base(self) -> None:
        base = HOME / ".local/state/warpzone"
        self.assertEqual(selfupdate.conf_path(base), base / "updater/conf")
        self.assertEqual(selfupdate.state_path(base), base / "updater/state")
        self.assertEqual(selfupdate.bad_dir(base), base / "updater/bad")


class ConfTests(unittest.TestCase):
    VALUES: ClassVar[dict[str, object]] = {
        "device": "deck",
        "config": "/home/deck/.config/warpzone/config.toml",
        "clone": "/home/deck/.local/share/warpzone/src",
        "python": "/home/deck/.local/share/warpzone/venv/bin/python",
        "channel": "stable",
        "pin": None,
        "enabled": True,
        "frontend": ("es-de", "ES-DE.AppImage"),
        "install": "auto",
    }

    def render(self, **change) -> str:
        return selfupdate.render_conf(**{**self.VALUES, **change})

    def test_the_settings_are_one_key_per_line_in_a_fixed_order(self) -> None:
        self.assertEqual(
            self.render(),
            "# Written by warpzone provision device, which rewrites it.\n"
            "device=deck\n"
            "config=/home/deck/.config/warpzone/config.toml\n"
            "clone=/home/deck/.local/share/warpzone/src\n"
            "python=/home/deck/.local/share/warpzone/venv/bin/python\n"
            "channel=stable\n"
            "pin=\n"
            "enabled=true\n"
            "frontend=es-de ES-DE.AppImage\n"
            "install=auto\n",
        )

    def test_install_manual_is_written(self) -> None:
        self.assertEqual(selfupdate.parse_kv(self.render(install="manual"))["install"], "manual")

    def test_install_follows_the_profile(self) -> None:
        from warpzone.profiles import Profile

        self.assertEqual(selfupdate.install_mode(Profile("p", "")), "auto")
        self.assertEqual(
            selfupdate.install_mode(Profile("p", "", updater_install="manual")), "manual"
        )

    def test_a_pin_a_channel_and_a_disabled_device(self) -> None:
        parsed = selfupdate.parse_kv(self.render(channel="rc", pin="v0.3.0rc1", enabled=False))
        self.assertEqual(
            (parsed["channel"], parsed["pin"], parsed["enabled"]), ("rc", "v0.3.0rc1", "false")
        )

    def test_a_config_that_is_not_given_leaves_the_key_empty(self) -> None:
        self.assertEqual(selfupdate.parse_kv(self.render(config=None))["config"], "")

    def test_it_reads_back_what_it_wrote_and_runs_nothing(self) -> None:
        text = self.render(device="my deck", python="/opt/$(touch x)/`id`/py")
        parsed = selfupdate.parse_kv(text)
        self.assertEqual(parsed["device"], "my deck")
        self.assertEqual(parsed["python"], "/opt/$(touch x)/`id`/py")
        self.assertEqual(
            set(parsed),
            {
                "device",
                "config",
                "clone",
                "python",
                "channel",
                "pin",
                "enabled",
                "frontend",
                "install",
            },
        )

    def test_a_value_with_a_line_break_is_refused(self) -> None:
        for key in ("device", "python", "clone", "config", "pin"):
            for bad in ("a\nenabled=false", "a\rb", "a\0b"):
                with self.subTest(key=key, bad=bad), self.assertRaises(ValueError):
                    self.render(**{key: bad})

    def test_parse_kv_skips_comments_and_lines_without_an_equals_sign(self) -> None:
        text = "# note\n\nnonsense\na=1\nb = 2\r\na=3\n=x\n"
        self.assertEqual(selfupdate.parse_kv(text), {"a": "3", "b ": " 2", "": "x"})


class AbiTests(unittest.TestCase):
    def test_the_abi_is_read_from_the_script(self) -> None:
        self.assertEqual(selfupdate.updater_abi("#!/bin/sh\nUPDATER_ABI=7\nset -u\n"), 7)

    def test_the_shipped_script_has_one(self) -> None:
        abi = selfupdate.updater_abi(UPDATER.read_text(encoding="utf-8"))
        self.assertIsNotNone(abi)
        self.assertGreaterEqual(abi, 1)

    def test_no_abi_or_a_garbled_one_is_none(self) -> None:
        for text in ("", "echo hi\n", "UPDATER_ABI=\n", "UPDATER_ABI=x1\n", "  UPDATER_ABI=2\n"):
            self.assertIsNone(selfupdate.updater_abi(text), repr(text))

    def test_only_the_first_assignment_counts(self) -> None:
        self.assertEqual(selfupdate.updater_abi("UPDATER_ABI=1\nUPDATER_ABI=9\n"), 1)


class InstallDecisionTests(unittest.TestCase):
    def decide(self, candidate: str, live: str | None) -> str:
        return selfupdate.install_decision(candidate, live)

    def test_no_live_copy_installs(self) -> None:
        self.assertEqual(self.decide("UPDATER_ABI=1\n", None), "install")

    def test_the_same_text_is_current(self) -> None:
        self.assertEqual(self.decide("UPDATER_ABI=1\nx\n", "UPDATER_ABI=1\nx\n"), "current")

    def test_a_higher_or_equal_abi_with_other_text_replaces(self) -> None:
        self.assertEqual(self.decide("UPDATER_ABI=2\nb\n", "UPDATER_ABI=1\na\n"), "install")
        self.assertEqual(self.decide("UPDATER_ABI=1\nb\n", "UPDATER_ABI=1\na\n"), "install")

    def test_a_lower_abi_never_replaces_a_higher_one(self) -> None:
        self.assertEqual(self.decide("UPDATER_ABI=1\nb\n", "UPDATER_ABI=2\na\n"), "older")

    def test_a_live_copy_with_no_abi_is_replaced(self) -> None:
        self.assertEqual(self.decide("UPDATER_ABI=1\n", "garbage\n"), "install")

    def test_a_candidate_with_no_abi_is_refused_whatever_is_there(self) -> None:
        self.assertEqual(self.decide("echo\n", None), "invalid")
        self.assertEqual(self.decide("echo\n", "UPDATER_ABI=1\n"), "invalid")


class StateTests(unittest.TestCase):
    def test_the_state_is_read_like_the_script_reads_it(self) -> None:
        text = "schema=1\ngood_tag=v0.3.0\nlast_result=ok\nlast_ok=1700000000\n"
        self.assertEqual(
            selfupdate.parse_kv(text),
            {"schema": "1", "good_tag": "v0.3.0", "last_result": "ok", "last_ok": "1700000000"},
        )

    def test_a_bad_tag_file(self) -> None:
        text = "sha=abc123\ndate=2026-11-01T10:00:00Z\nreason=health failed (exit 1)\n"
        bad = selfupdate.parse_bad(text)
        self.assertEqual(bad.sha, "abc123")
        self.assertEqual(bad.reason, "health failed (exit 1)")
        self.assertEqual(bad.when, 1_793_527_200)

    def test_a_bad_tag_file_with_no_readable_date(self) -> None:
        self.assertIsNone(selfupdate.parse_bad("sha=a\ndate=yesterday\nreason=x\n").when)
        self.assertIsNone(selfupdate.parse_bad("").when)


class WrapperTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.live = Path(self._tmp.name) / "updater.sh"
        self.lines: list[str] = []
        self.commands: list[list[str]] = []

    def run_tick(self, code: int = 0, *, fetch_now: bool = False) -> int:
        def run(command):
            self.commands.append(list(command))
            return code

        return selfupdate.run_tick(self.live, self.lines.append, fetch_now=fetch_now, run=run)

    def test_fetch_now_asks_an_old_script_to_fetch(self) -> None:
        self.live.write_text("UPDATER_ABI=1\n")
        self.assertEqual(self.run_tick(0, fetch_now=True), 0)
        self.assertEqual(self.commands, [["sh", str(self.live), "tick", "--fetch-now"]])

    def test_fetch_now_is_an_update_by_hand_for_a_script_that_knows_it(self) -> None:
        self.live.write_text("#!/bin/sh\nUPDATER_ABI=2\n")
        self.assertEqual(self.run_tick(0, fetch_now=True), 0)
        self.assertEqual(self.commands, [["sh", str(self.live), "tick", "--now"]])

    def test_the_shipped_script_knows_now(self) -> None:
        text = selfupdate.UPDATER_SOURCE.read_text()
        self.assertGreaterEqual(selfupdate.updater_abi(text) or 0, selfupdate.NOW_ABI)

    def test_it_runs_the_live_copy_with_sh_and_returns_its_exit_code(self) -> None:
        self.live.write_text("UPDATER_ABI=1\n")
        self.assertEqual(self.run_tick(0), 0)
        self.assertEqual(self.commands, [["sh", str(self.live), "tick"]])
        self.assertEqual(self.run_tick(3), 3)
        self.assertEqual(self.lines, [])

    def test_without_a_live_copy_it_says_so_and_runs_nothing(self) -> None:
        self.assertEqual(self.run_tick(), 1)
        self.assertEqual(self.commands, [])
        (message,) = self.lines
        self.assertIn(str(self.live), message)
        self.assertIn("warpzone provision device", message)
        self.assertIn("updater.sh install", message)

    def test_a_shell_that_cannot_be_started_is_a_failure_with_a_message(self) -> None:
        self.live.write_text("UPDATER_ABI=1\n")

        def run(command):
            raise OSError("sh: not found")

        self.assertEqual(selfupdate.run_tick(self.live, self.lines.append, run=run), 1)
        self.assertIn("not found", self.lines[0])


CONFIG = """# my consoles
[hub]
url = "https://hub.example:8384"

[devices.ark]  # the handheld
profile = "anbernic-rg351mp"
folders = ["library", "saves"]
channel = "stable"  # follow stable

[devices.deck]
profile = "steamos-emudeck"
folders = ["library"]
"""


class ChannelEditTests(unittest.TestCase):
    def test_the_channel_line_of_the_device_is_replaced_and_nothing_else(self) -> None:
        out = selfupdate.set_channel_text(CONFIG, "ark", "rc")
        self.assertEqual(
            out, CONFIG.replace('channel = "stable"  # follow', 'channel = "rc"  # follow')
        )

    def test_a_device_without_a_channel_gets_one_under_its_header(self) -> None:
        out = selfupdate.set_channel_text(CONFIG, "deck", "rc")
        self.assertIn('[devices.deck]\nchannel = "rc"\nprofile = "steamos-emudeck"', out)
        self.assertIn('channel = "stable"  # follow stable', out, "the other device is untouched")

    def test_a_quoted_table_name_and_single_quotes(self) -> None:
        text = "[devices.\"my deck\"]\nchannel = 'rc'\n"
        self.assertEqual(
            selfupdate.set_channel_text(text, "my deck", "stable"),
            '[devices."my deck"]\nchannel = "stable"\n',
        )

    def test_the_same_channel_changes_nothing(self) -> None:
        self.assertEqual(selfupdate.set_channel_text(CONFIG, "ark", "stable"), CONFIG)

    def test_what_it_cannot_edit_safely_is_refused(self) -> None:
        cases = {
            "no such device": (CONFIG, "nope"),
            "inline table": ('[devices]\nark = { profile = "x", channel = "stable" }\n', "ark"),
            "two tables": ("[devices.ark]\nchannel = 'rc'\n[devices.ark]\n", "ark"),
            "not toml": ("[devices.ark\n", "ark"),
        }
        for label, (text, device) in cases.items():
            with self.subTest(label), self.assertRaises(ValueError):
                selfupdate.set_channel_text(text, device, "rc")

    def test_an_unknown_channel_is_refused(self) -> None:
        with self.assertRaises(ValueError):
            selfupdate.set_channel_text(CONFIG, "ark", "beta")

    def test_conf_channel_line(self) -> None:
        conf = "# Written by warpzone\ndevice=ark\nchannel=stable\npin=\n"
        self.assertEqual(
            selfupdate.set_conf_channel(conf, "rc"),
            "# Written by warpzone\ndevice=ark\nchannel=rc\npin=\n",
        )
        self.assertEqual(
            selfupdate.set_conf_channel("device=ark\n", "rc"), "device=ark\nchannel=rc\n"
        )


class ChannelCommandTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.config = self.root / "config.toml"
        self.config.write_text(CONFIG)
        self.conf = self.root / "state" / "updater" / "conf"
        self.conf.parent.mkdir(parents=True)
        self.conf.write_text(f"device=ark\nconfig={self.config}\nchannel=stable\npin=\n")
        self.lines: list[str] = []

    def change(self, channel: str, version: str = "0.3.0rc4") -> int:
        return selfupdate.change_channel(
            self.conf, channel, self.lines.append, version=version, now=1793800000
        )

    def test_it_edits_the_config_with_a_backup_and_the_updater_settings(self) -> None:
        self.assertEqual(self.change("rc"), 0)
        self.assertIn('channel = "rc"  # follow stable', self.config.read_text())
        self.assertIn("channel=rc\n", self.conf.read_text())
        backups = sorted(p.name for p in self.root.glob("config.toml.bak-*"))
        self.assertEqual(len(backups), 1)
        self.assertEqual((self.root / backups[0]).read_text(), CONFIG)
        self.assertIn("rc", self.lines[-1])

    def test_going_to_stable_from_a_candidate_says_it_stays_until_a_newer_stable(self) -> None:
        self.config.write_text(CONFIG.replace('channel = "stable"', 'channel = "rc"'))
        self.assertEqual(self.change("stable", version="0.3.0rc4"), 0)
        self.assertTrue(any("0.3.0rc4" in line and "stable" in line for line in self.lines))

    def test_the_same_channel_writes_nothing(self) -> None:
        before = self.conf.read_text()
        self.assertEqual(self.change("stable"), 0)
        self.assertEqual(list(self.root.glob("config.toml.bak-*")), [])
        self.assertEqual(self.conf.read_text(), before)
        self.assertIn("already", self.lines[-1])

    def test_without_updater_settings_or_a_config_it_fails_with_a_message(self) -> None:
        self.conf.unlink()
        self.assertEqual(self.change("rc"), 1)
        self.assertIn("provision device", self.lines[-1])

    def test_a_config_it_cannot_edit_is_left_alone(self) -> None:
        self.config.write_text('[devices]\nark = { profile = "x" }\n')
        self.assertEqual(self.change("rc"), 1)
        self.assertEqual(self.config.read_text(), '[devices]\nark = { profile = "x" }\n')
        self.assertEqual(list(self.root.glob("config.toml.bak-*")), [])


if __name__ == "__main__":
    unittest.main()
