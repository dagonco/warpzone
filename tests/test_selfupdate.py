import os
import shutil
import subprocess
import tempfile
import unittest
from collections.abc import Sequence
from pathlib import Path
from unittest import mock

from warpzone import selfupdate
from warpzone.selfupdate import (
    Decision,
    decide,
    newest_tag,
    parse_tag,
    run_self_update,
    version_key,
)

GIT = shutil.which("git")
GIT_ENV = {
    **os.environ,
    "GIT_CONFIG_GLOBAL": os.devnull,
    "GIT_CONFIG_SYSTEM": os.devnull,
    "GIT_AUTHOR_NAME": "Test",
    "GIT_AUTHOR_EMAIL": "test@example.invalid",
    "GIT_COMMITTER_NAME": "Test",
    "GIT_COMMITTER_EMAIL": "test@example.invalid",
}


class VersionTests(unittest.TestCase):
    def test_parse_tag_takes_only_plain_releases(self) -> None:
        self.assertEqual(parse_tag("v0.2.0"), (0, 2, 0))
        self.assertEqual(parse_tag("v10.0.12"), (10, 0, 12))
        for tag in ("0.2.0", "v0.2", "v0.3.0rc1", "v1.0.0-rc.1", "latest", "v1.0.0.1", ""):
            self.assertIsNone(parse_tag(tag), tag)

    def test_newest_tag_compares_numbers_not_text(self) -> None:
        tags = ["v0.9.0", "v0.10.0", "v0.2.0", "v0.11.0rc1", "nightly"]
        self.assertEqual(newest_tag(tags), "v0.10.0")
        self.assertIsNone(newest_tag(["nightly", "v1"]))
        self.assertIsNone(newest_tag([]))

    def test_version_key_puts_a_pre_release_before_its_release(self) -> None:
        assert version_key("0.3.0rc1") is not None
        assert version_key("0.3.0") is not None
        self.assertLess(version_key("0.3.0rc1"), version_key("0.3.0"))  # type: ignore[operator]
        self.assertLess(version_key("0.2.9"), version_key("0.3.0rc1"))  # type: ignore[operator]
        self.assertIsNone(version_key("dev"))

    def test_decide(self) -> None:
        self.assertEqual(decide("0.2.0", ["v0.3.0", "v0.2.0"]), Decision("update", "v0.3.0"))
        self.assertEqual(decide("0.3.0", ["v0.3.0", "v0.2.0"]), Decision("current", "v0.3.0"))
        self.assertEqual(decide("0.4.0", ["v0.3.0", "v0.2.0"]), Decision("ahead", "v0.3.0"))
        self.assertEqual(decide("0.3.0rc1", ["v0.3.0"]), Decision("update", "v0.3.0"))
        self.assertEqual(decide("0.3.0", ["v0.3.0rc1"]), Decision("no-tags", None))
        self.assertEqual(decide("0.2.0", []), Decision("no-tags", None))
        self.assertEqual(decide("weird", ["v0.3.0"]), Decision("bad-version", None))


@unittest.skipUnless(GIT, "git is not installed")
class SelfUpdateTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        self.origin = self.tmp / "origin.git"
        self.root = self.tmp / "src"
        self.log = self.tmp / "state" / "warpzone" / "self-update.log"
        self.lines: list[str] = []
        self.calls: list[list[str]] = []
        self.git_in(self.tmp, "init", "--bare", "--initial-branch=main", str(self.origin))
        seed = self.tmp / "seed"
        self.git_in(self.tmp, "clone", "--quiet", str(self.origin), str(seed))
        self.seed = seed
        self.release(seed, "v0.2.0", "0.2.0")
        self.git_in(self.tmp, "clone", "--quiet", str(self.origin), str(self.root))
        self.git_in(self.root, "checkout", "--quiet", "--detach", "v0.2.0")

    def git_in(self, cwd: Path, *args: str) -> str:
        proc = subprocess.run(
            ["git", *args], cwd=cwd, env=GIT_ENV, capture_output=True, text=True, check=True
        )
        return proc.stdout.strip()

    def release(self, clone: Path, tag: str, version: str, pyproject: str = "[project]\n") -> None:
        (clone / "VERSION").write_text(version)
        (clone / "pyproject.toml").write_text(pyproject)
        self.git_in(clone, "add", "-A")
        self.git_in(clone, "commit", "--quiet", "-m", f"release {version}")
        self.git_in(clone, "tag", tag)
        self.git_in(clone, "push", "--quiet", "origin", "HEAD:main", tag)

    def head(self) -> str:
        return self.git_in(self.root, "rev-parse", "HEAD")

    def runner(self, command: Sequence[str]) -> tuple[int, str]:
        """Real git; anything else is recorded and succeeds."""
        self.calls.append(list(command))
        if command[0] == "git":
            proc = subprocess.run(
                list(command), env=GIT_ENV, capture_output=True, text=True, check=False
            )
            return proc.returncode, proc.stdout + proc.stderr
        return 0, "0.3.0\n"

    def update(self, version: str = "0.2.0", **kw) -> int:
        kw.setdefault("run", self.runner)
        return run_self_update(
            self.root, version, self.lines.append, log=self.log, python="/venv/bin/python", **kw
        )

    def said(self) -> str:
        return "\n".join(self.lines)

    def test_a_folder_that_is_not_a_checkout_exits_cleanly(self) -> None:
        plain = self.tmp / "site-packages"
        plain.mkdir()
        code = run_self_update(plain, "0.2.0", self.lines.append, log=self.log, run=self.runner)
        self.assertEqual(code, 0)
        self.assertIn("not a git checkout", self.said())

    def test_a_package_inside_some_other_repository_is_not_a_checkout(self) -> None:
        inner = self.root / "vendor" / "site-packages"
        inner.mkdir(parents=True)
        code = run_self_update(inner, "0.2.0", self.lines.append, log=self.log, run=self.runner)
        self.assertEqual(code, 0)
        self.assertIn("not a git checkout", self.said())

    def test_missing_git_exits_cleanly(self) -> None:
        def no_git(command: Sequence[str]) -> tuple[int, str]:
            raise FileNotFoundError("git")

        self.assertEqual(self.update(run=no_git), 0)
        self.assertIn("git", self.said())

    def test_moves_to_the_newest_tag(self) -> None:
        self.release(self.seed, "v0.2.5", "0.2.5")
        self.release(self.seed, "v0.3.0", "0.3.0")
        self.release(self.seed, "v0.3.1rc1", "0.3.1rc1")  # not a release
        code = self.update()
        self.assertEqual(code, 0, self.said())
        self.assertEqual(self.head(), self.git_in(self.root, "rev-parse", "v0.3.0^{commit}"))
        self.assertEqual((self.root / "VERSION").read_text(), "0.3.0")
        self.assertIn("0.2.0 -> 0.3.0", self.said())
        self.assertIn("0.2.0 -> 0.3.0", self.log.read_text())

    def test_reports_the_version_the_checkout_reports_now(self) -> None:
        self.release(self.seed, "v0.3.0", "0.3.0")
        self.update()
        probe = [c for c in self.calls if c[0] == "/venv/bin/python" and "-c" in c]
        self.assertEqual(len(probe), 1)
        self.assertIn("now 0.3.0", self.said())

    def test_a_pyproject_change_reinstalls_the_editable_package(self) -> None:
        self.release(self.seed, "v0.3.0", "0.3.0", pyproject="[project]\nname = 'x'\n")
        self.assertEqual(self.update(), 0)
        pip = [c for c in self.calls if "pip" in c]
        self.assertEqual(len(pip), 1)
        self.assertEqual(pip[0][:4], ["/venv/bin/python", "-m", "pip", "install"], pip[0])
        self.assertIn(str(self.root), pip[0])

    def test_no_reinstall_when_pyproject_is_unchanged(self) -> None:
        self.release(self.seed, "v0.3.0", "0.3.0")
        self.assertEqual(self.update(), 0)
        self.assertEqual([c for c in self.calls if "pip" in c], [])

    def test_a_failed_reinstall_says_how_to_do_it_by_hand(self) -> None:
        self.release(self.seed, "v0.3.0", "0.3.0", pyproject="[project]\nname = 'x'\n")

        def failing(command: Sequence[str]) -> tuple[int, str]:
            if "pip" in command:
                return 1, "no network"
            return self.runner(command)

        self.assertEqual(self.update(run=failing), 1)
        self.assertIn("pip install -e", self.said())

    def test_up_to_date_changes_nothing(self) -> None:
        before = self.head()
        self.assertEqual(self.update(), 0)
        self.assertEqual(self.head(), before)
        self.assertIn("up to date", self.said())

    def test_a_device_ahead_of_the_newest_tag_stays_put(self) -> None:
        self.git_in(self.root, "checkout", "--quiet", "main")
        before = self.head()
        self.assertEqual(self.update(version="0.9.0"), 0)
        self.assertEqual(self.head(), before)
        self.assertIn("ahead", self.said())

    def test_a_dirty_tree_is_refused(self) -> None:
        self.release(self.seed, "v0.3.0", "0.3.0")
        (self.root / "VERSION").write_text("edited")
        before = self.head()
        self.assertEqual(self.update(), 1)
        self.assertEqual(self.head(), before)
        self.assertEqual((self.root / "VERSION").read_text(), "edited")
        self.assertIn("local changes", self.said())

    def test_untracked_files_do_not_block_the_update(self) -> None:
        self.release(self.seed, "v0.3.0", "0.3.0")
        (self.root / "notes.txt").write_text("mine")
        self.assertEqual(self.update(), 0, self.said())
        self.assertEqual((self.root / "VERSION").read_text(), "0.3.0")

    def test_dirty_tree_is_not_mentioned_when_nothing_is_due(self) -> None:
        (self.root / "VERSION").write_text("edited")
        self.assertEqual(self.update(), 0)
        self.assertNotIn("local changes", self.said())

    def test_dry_run_reports_and_changes_nothing(self) -> None:
        self.release(self.seed, "v0.3.0", "0.3.0")
        before = self.head()
        self.assertEqual(self.update(dry_run=True), 0)
        self.assertEqual(self.head(), before)
        self.assertIn("would update 0.2.0 -> 0.3.0", self.said())
        self.assertFalse(self.log.exists())
        self.assertEqual([c for c in self.calls if c[0] != "git"], [])

    def test_no_network_is_a_short_message_and_exit_0(self) -> None:
        shutil.rmtree(self.origin)
        before = self.head()
        self.assertEqual(self.update(), 0)
        self.assertEqual(self.head(), before)
        self.assertIn("could not reach", self.said())
        self.assertIn("could not reach", self.log.read_text())

    def test_a_timeout_counts_as_no_network(self) -> None:
        def slow(command: Sequence[str]) -> tuple[int, str]:
            if "fetch" in command:
                raise subprocess.TimeoutExpired(command, 1)
            return self.runner(command)

        self.assertEqual(self.update(run=slow), 0)
        self.assertIn("could not reach", self.said())

    def test_an_unreadable_version_is_an_error(self) -> None:
        self.assertEqual(self.update(version="dev"), 1)

    def test_the_log_is_appended_with_timestamps_and_stays_small(self) -> None:
        self.log.parent.mkdir(parents=True)
        self.log.write_text("".join(f"old line {i}\n" for i in range(5000)))
        self.update()
        lines = self.log.read_text().splitlines()
        self.assertLess(len(lines), 1000)
        self.assertRegex(lines[-1], r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ ")

    def test_the_real_runner_has_a_timeout_and_never_asks_for_a_password(self) -> None:
        with mock.patch.object(selfupdate.subprocess, "run") as run:
            run.return_value = mock.Mock(returncode=0, stdout="", stderr="")
            selfupdate.run_command(["git", "fetch"])
        kwargs = run.call_args.kwargs
        self.assertGreater(kwargs["timeout"], 0)
        self.assertEqual(kwargs["env"]["GIT_TERMINAL_PROMPT"], "0")
        self.assertEqual(kwargs["stdin"], subprocess.DEVNULL)


if __name__ == "__main__":
    unittest.main()
