import os
import subprocess
import sys
import time
import unittest
from collections.abc import Callable
from unittest import mock

from inbox_support import age
from test_cli import run
from test_inbox import NES_ROM, Base
from warpzone import inbox, inbox_lock
from warpzone.inbox import run_inbox, scan
from warpzone.inbox_core import InboxError, Options, Tools
from warpzone.inbox_lock import LOCK_MAX_AGE, LOCK_NAME, LockInfo
from warpzone.report import Reporter

NOW = 1_800_000_000.0
HERE = "desktop-a"


def alive(pids: set[int]) -> Callable[[int], bool]:
    return lambda pid: pid in pids


class JudgeTests(unittest.TestCase):
    """The pure decision: is a lock that is in the way stale, and why."""

    def judge(
        self,
        info: LockInfo | None,
        *,
        mtime: float = NOW,
        running: tuple[int, ...] = (),
        host: str = HERE,
    ) -> str | None:
        return inbox_lock.judge(info, mtime, NOW, host, alive(set(running)))

    def test_a_recent_lock_of_a_running_process_here_is_live(self) -> None:
        self.assertIsNone(self.judge(LockInfo(HERE, 42, NOW - 60), running=(42,)))

    def test_a_lock_older_than_the_limit_is_stale(self) -> None:
        reason = self.judge(LockInfo(HERE, 42, NOW - LOCK_MAX_AGE - 1), running=(42,))
        self.assertIn("12 hours", reason or "")

    def test_a_dead_process_on_this_host_is_stale(self) -> None:
        reason = self.judge(LockInfo(HERE, 42, NOW - 60))
        self.assertIn("42", reason or "")
        self.assertIn("not running", reason or "")

    def test_a_pid_on_another_host_cannot_be_checked_so_the_lock_is_live(self) -> None:
        self.assertIsNone(self.judge(LockInfo("desktop-b", 42, NOW - 60)))

    def test_an_unreadable_lock_goes_by_the_age_of_the_file(self) -> None:
        self.assertIsNone(self.judge(None, mtime=NOW - 60))
        self.assertIn("12 hours", self.judge(None, mtime=NOW - LOCK_MAX_AGE - 1) or "")

    def test_parse_round_trips_and_rejects_garbage(self) -> None:
        info = LockInfo(HERE, 42, NOW)
        self.assertEqual(inbox_lock.parse(inbox_lock.render(info)), info)
        for text in ("", "nonsense", "{}", '{"host": "h", "pid": "x", "started": 1}'):
            self.assertIsNone(inbox_lock.parse(text), text)

    def test_pid_alive_knows_this_process_and_a_finished_one(self) -> None:
        self.assertTrue(inbox_lock.pid_alive(os.getpid()))
        done = subprocess.Popen([sys.executable, "-c", "pass"])
        done.wait()
        self.assertFalse(inbox_lock.pid_alive(done.pid))


class LockTests(Base):
    def setUp(self) -> None:
        super().setUp()
        self.lock = self.inbox / LOCK_NAME
        for name, value in (("hostname", lambda: HERE), ("pid_alive", alive({4242}))):
            patch = mock.patch.object(inbox_lock, name, value)
            patch.start()
            self.addCleanup(patch.stop)

    def put_lock(self, host: str = HERE, pid: int = 4242, started: float | None = None) -> None:
        info = LockInfo(host, pid, time.time() if started is None else started)
        self.lock.write_text(inbox_lock.render(info))

    def test_apply_holds_the_lock_while_it_works_and_removes_it_after(self) -> None:
        self.drop("a.nes", NES_ROM)
        seen: list[LockInfo | None] = []
        real = inbox.process_item

        def spy(ctx, item, number):  # type: ignore[no-untyped-def]
            seen.append(inbox_lock.parse(self.lock.read_text()))
            return real(ctx, item, number)

        with mock.patch.object(inbox, "process_item", spy):
            self.assertEqual(self.go(), 0, self.out)
        held = seen[0]
        assert held is not None
        self.assertEqual((held.host, held.pid), (HERE, os.getpid()))
        self.assertFalse(self.lock.exists())

    def test_a_second_apply_refuses_and_names_the_holder(self) -> None:
        self.drop("a.nes", NES_ROM)
        self.put_lock(host="desktop-b", pid=77)
        with self.assertRaises(InboxError) as caught:
            self.go()
        message = str(caught.exception)
        self.assertIn("desktop-b", message)
        self.assertIn("77", message)
        self.assertIn("since", message)
        self.assertNotIn(str(self.tmp), message)
        self.assertEqual(self.library(), [])
        self.assertTrue(self.lock.exists(), "the holder's lock must stay")

    def test_a_report_run_ignores_the_lock_and_takes_none(self) -> None:
        self.drop("a.nes", NES_ROM)
        self.put_lock(host="desktop-b", pid=77)
        self.assertEqual(self.go(apply=False), 0, self.out)
        self.assertTrue(self.lock.exists())
        self.lock.unlink()
        self.go(apply=False)
        self.assertFalse(self.lock.exists())

    def test_the_lock_is_not_an_item(self) -> None:
        self.put_lock()
        self.drop("a.nes", NES_ROM)
        self.assertEqual([p.name for p in scan(self.inbox)], ["a.nes"])

    def test_a_dead_holder_here_is_stale_and_needs_force_lock(self) -> None:
        self.drop("a.nes", NES_ROM)
        self.put_lock(pid=99999)
        with self.assertRaises(InboxError) as caught:
            self.go()
        self.assertIn("stale", str(caught.exception))
        self.assertIn("--force-lock", str(caught.exception))
        self.assertEqual(self.library(), [])
        self.assertEqual(self.go(force_lock=True), 0, self.out)
        self.assertIn("stale lock", self.out)
        self.assertEqual(self.library(), ["nes/Adventure (USA).zip"])
        self.assertFalse(self.lock.exists())

    def test_an_old_lock_of_another_host_is_stale(self) -> None:
        self.put_lock(host="desktop-b", started=time.time() - LOCK_MAX_AGE - 60)
        with self.assertRaises(InboxError) as caught:
            self.go()
        self.assertIn("12 hours", str(caught.exception))
        self.assertEqual(self.go(force_lock=True), 0, self.out)

    def test_force_lock_never_takes_a_live_lock(self) -> None:
        self.put_lock(host="desktop-b", pid=77)
        with self.assertRaises(InboxError):
            self.go(force_lock=True)
        self.assertTrue(self.lock.exists())

    def test_force_lock_takes_an_unreadable_old_lock(self) -> None:
        self.lock.write_text("garbage")
        with self.assertRaises(InboxError):
            self.go()
        age(self.lock, LOCK_MAX_AGE + 60)
        self.assertEqual(self.go(force_lock=True), 0, self.out)

    def test_the_lock_is_removed_after_an_error_and_after_ctrl_c(self) -> None:
        self.drop("a.nes", NES_ROM)
        for exc in (RuntimeError("boom"), KeyboardInterrupt()):
            with (
                mock.patch.object(inbox, "process_item", side_effect=exc),
                self.assertRaises(type(exc)),
            ):
                self.go()
            self.assertFalse(self.lock.exists(), type(exc).__name__)

    def test_a_lock_that_is_not_ours_any_more_is_left_alone(self) -> None:
        self.drop("a.nes", NES_ROM)
        real = inbox.process_item

        def steal(ctx, item, number):  # type: ignore[no-untyped-def]
            self.put_lock(host="desktop-b", pid=77)
            return real(ctx, item, number)

        with mock.patch.object(inbox, "process_item", steal):
            self.go()
        self.assertIn("desktop-b", self.lock.read_text())

    def test_a_validation_error_leaves_no_lock(self) -> None:
        with self.assertRaises(InboxError):
            run_inbox(
                Options(self.hub, self.tmp / "nodats", Tools(), apply=True),
                Reporter(self.lines.append),
            )
        self.assertFalse(self.lock.exists())


class CliLockTests(Base):
    def cli(self, *extra: str) -> tuple[int, str, str]:
        config = self.tmp / "config.toml"
        config.write_text(f'[inbox]\nchdman = "{self.chdman}"\n')
        args = ["inbox", "--config", str(config), "--root", str(self.hub)]
        args += ["--dats", str(self.dats), "--apply", *extra]
        with mock.patch.dict(os.environ, {"XDG_CACHE_HOME": str(self.tmp / "xdg")}):
            return run(*args)

    def test_a_held_lock_is_a_usage_error_even_with_force_lock(self) -> None:
        held = LockInfo("desktop-b", 77, time.time())
        (self.inbox / LOCK_NAME).write_text(inbox_lock.render(held))
        code, _, err = self.cli()
        self.assertEqual(code, 2, err)
        self.assertIn("desktop-b", err)
        code, _, err = self.cli("--force-lock")
        self.assertEqual(code, 2, err)
        self.assertIn("desktop-b", err)

    def test_force_lock_takes_a_stale_lock(self) -> None:
        stale = LockInfo("desktop-b", 77, time.time() - LOCK_MAX_AGE - 60)
        (self.inbox / LOCK_NAME).write_text(inbox_lock.render(stale))
        self.assertEqual(self.cli()[0], 2)
        code, out, err = self.cli("--force-lock")
        self.assertEqual(code, 0, err)
        self.assertIn("stale lock", out)
        self.assertFalse((self.inbox / LOCK_NAME).exists())


if __name__ == "__main__":
    unittest.main()
