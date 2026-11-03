"""The pieces of `warpzone self-update` that are not the updater script: version keys, the
settings file written for the script, the live copy and the wrapper command."""

import tempfile
import unittest
from pathlib import Path
from typing import ClassVar

from warpzone import selfupdate
from warpzone.selfupdate import tag_key, version_key

UPDATER = Path(__file__).resolve().parent.parent / "warpzone" / "data" / "updater.sh"
HOME = Path("/home/deck")


class VersionTests(unittest.TestCase):
    def test_version_key_puts_a_pre_release_before_its_release(self) -> None:
        assert version_key("0.3.0rc1") is not None
        assert version_key("0.3.0") is not None
        self.assertLess(version_key("0.3.0rc1"), version_key("0.3.0"))  # type: ignore[operator]
        self.assertLess(version_key("0.2.9"), version_key("0.3.0rc1"))  # type: ignore[operator]
        self.assertIsNone(version_key("dev"))

    def test_version_key_orders_release_candidates(self) -> None:
        order = ["0.2.9", "0.3.0rc1", "0.3.0rc2", "0.3.0rc10", "0.3.0", "0.3.1rc1", "0.10.0"]
        keys = [version_key(v) for v in order]
        self.assertNotIn(None, keys)
        self.assertEqual(keys, sorted(keys))  # type: ignore[type-var]
        self.assertEqual(len(set(keys)), len(keys), "no two versions tie")

    def test_tag_key_orders_candidate_tags(self) -> None:
        order = ["v0.2.9", "v0.3.0rc1", "v0.3.0rc2", "v0.3.0rc10", "v0.3.0", "v0.10.0rc1"]
        keys = [tag_key(t) for t in order]
        self.assertNotIn(None, keys)
        self.assertEqual(keys, sorted(keys))  # type: ignore[type-var]
        self.assertEqual(len(set(keys)), len(keys))
        shuffled = ["v0.3.0", "v0.10.0rc1", "v0.3.0rc2", "v0.2.9", "v0.3.0rc10", "v0.3.0rc1"]
        self.assertEqual(sorted(shuffled, key=tag_key), order)  # type: ignore[arg-type,return-value]

    def test_tag_key_takes_only_the_spelling_of_the_releasing_guide(self) -> None:
        for tag in (
            "0.3.0rc1",
            "v0.3",
            "v0.3.0-rc1",
            "v0.3.0-rc.1",
            "v0.3.0rc",
            "v0.3.0a1",
            "v0.3.0rc1.dev1",
            "nightly",
            "",
        ):
            self.assertIsNone(tag_key(tag), tag)

    def test_tag_key_agrees_with_the_version_key_of_the_package_version(self) -> None:
        for version in ("0.3.0", "0.3.0rc1", "10.0.12rc7"):
            self.assertEqual(tag_key(f"v{version}"), version_key(version))


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
            "frontend=es-de ES-DE.AppImage\n",
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
            {"device", "config", "clone", "python", "channel", "pin", "enabled", "frontend"},
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

    def run_tick(self, code: int = 0) -> int:
        def run(command):
            self.commands.append(list(command))
            return code

        return selfupdate.run_tick(self.live, self.lines.append, run=run)

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


if __name__ == "__main__":
    unittest.main()
