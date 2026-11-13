import errno
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from collections.abc import Callable
from contextlib import AbstractContextManager
from dataclasses import replace
from pathlib import Path
from unittest import mock

from inbox_support import age
from test_cli import run
from test_inbox import NES_ROM, Base
from warpzone import inbox, inbox_lock
from warpzone.inbox import run_inbox, scan
from warpzone.inbox_core import InboxError, Options, Tools
from warpzone.inbox_lock import (
    BEAT_SECONDS,
    LEGACY,
    LIVE,
    LOCK_MAX_AGE,
    LOCK_NAME,
    STALE,
    STALE_BEAT_SECONDS,
    UNKNOWN,
    LockBusy,
    LockInfo,
    LockReading,
    LockStale,
    Verdict,
)
from warpzone.report import Reporter

NOW = 1_800_000_000.0
HERE = "desktop-a"


def alive(pids: set[int]) -> Callable[[int], bool]:
    return lambda pid: pid in pids


def legacy(host: str = HERE, pid: int = 42, started: float = NOW - 60) -> LockInfo:
    """The lock of an older warpzone: no heartbeat, no mode."""
    return LockInfo(host, pid, started)


def beating(
    host: str = HERE, pid: int = 42, started: float = NOW - 3600, mode: str = "attended"
) -> LockInfo:
    return LockInfo(host, pid, started, BEAT_SECONDS, mode)


class JudgeTests(unittest.TestCase):
    """The pure decision: is a lock that is in the way stale, and why."""

    def judge(
        self,
        info: LockInfo | None,
        *,
        mtime: float = NOW,
        running: tuple[int, ...] = (),
        host: str = HERE,
        unattended: bool = False,
        previous: LockReading | None = None,
    ) -> Verdict:
        return inbox_lock.judge(
            LockReading(info, mtime, NOW),
            host,
            alive(set(running)),
            unattended=unattended,
            previous=previous,
        )

    def test_a_recent_lock_of_a_running_process_here_is_live(self) -> None:
        self.assertEqual(self.judge(legacy(pid=42), running=(42,)).state, LIVE)

    def test_a_lock_older_than_the_limit_is_stale(self) -> None:
        verdict = self.judge(legacy(started=NOW - LOCK_MAX_AGE - 1), running=(42,))
        self.assertEqual(verdict.state, STALE)
        self.assertIn("12 hours", verdict.reason or "")

    def test_a_dead_process_on_this_host_is_stale(self) -> None:
        verdict = self.judge(legacy())
        self.assertEqual(verdict.state, STALE)
        self.assertIn("42", verdict.reason or "")
        self.assertIn("not running", verdict.reason or "")

    def test_a_pid_on_another_host_cannot_be_checked_so_the_lock_is_live(self) -> None:
        self.assertEqual(self.judge(legacy(host="desktop-b")).state, LIVE)

    def test_an_unreadable_lock_goes_by_the_age_of_the_file(self) -> None:
        self.assertEqual(self.judge(None, mtime=NOW - 60).state, LIVE)
        old = self.judge(None, mtime=NOW - LOCK_MAX_AGE - 1)
        self.assertEqual(old.state, STALE)
        self.assertIn("12 hours", old.reason or "")

    def test_attended_looks_once_and_a_beat_older_than_ten_minutes_is_stale(self) -> None:
        lock = beating(host="desktop-b")
        self.assertEqual(self.judge(lock, mtime=NOW - 9 * 60).state, LIVE)
        verdict = self.judge(lock, mtime=NOW - 10 * 60 - 1)
        self.assertEqual(verdict.state, STALE)
        self.assertIn("no heartbeat since", verdict.reason or "")

    def test_a_lock_with_a_beat_is_not_stale_for_running_longer_than_twelve_hours(self) -> None:
        lock = beating(host="desktop-b", started=NOW - LOCK_MAX_AGE - 3600)
        self.assertEqual(self.judge(lock, mtime=NOW - 30).state, LIVE)

    def test_attended_still_uses_the_pid_of_this_host_for_a_lock_with_a_beat(self) -> None:
        self.assertEqual(self.judge(beating(pid=42), running=(42,)).state, LIVE)
        self.assertEqual(self.judge(beating(pid=42)).state, STALE)

    def test_unattended_with_no_earlier_reading_does_not_know(self) -> None:
        verdict = self.judge(beating(host="desktop-b"), mtime=NOW - 3600, unattended=True)
        self.assertEqual(verdict.state, UNKNOWN)

    def test_unattended_a_beat_that_moved_between_readings_is_live(self) -> None:
        lock = beating(host="desktop-b")
        before = LockReading(lock, NOW - 3600, NOW - 2 * STALE_BEAT_SECONDS)
        verdict = self.judge(lock, mtime=NOW - 30, unattended=True, previous=before)
        self.assertEqual(verdict.state, LIVE)

    def test_unattended_silence_across_two_readings_is_stale_on_any_host(self) -> None:
        for host in ("desktop-b", HERE):
            lock = beating(host=host)
            before = LockReading(lock, NOW - 3600, NOW - STALE_BEAT_SECONDS)
            verdict = self.judge(lock, mtime=NOW - 3600, unattended=True, previous=before)
            self.assertEqual(verdict.state, STALE, host)
            self.assertEqual(verdict.reason, "no heartbeat for 10 minutes")

    def test_unattended_judges_the_two_readings_not_the_mtime_against_the_clock(self) -> None:
        lock = beating(host="desktop-b")
        mtime = NOW + 86400  # the file's time is a day ahead of this clock: only the readings count
        before = LockReading(lock, mtime, NOW - STALE_BEAT_SECONDS + 1)
        too_soon = self.judge(lock, mtime=mtime, unattended=True, previous=before)
        self.assertEqual(too_soon.state, UNKNOWN)
        before = LockReading(lock, mtime, NOW - STALE_BEAT_SECONDS)
        verdict = self.judge(lock, mtime=mtime, unattended=True, previous=before)
        self.assertEqual(verdict.state, STALE)

    def test_unattended_a_clock_that_went_back_does_not_make_a_lock_stale(self) -> None:
        lock = beating(host="desktop-b")
        before = LockReading(lock, NOW - 3600, NOW + 5 * STALE_BEAT_SECONDS)
        verdict = self.judge(lock, mtime=NOW - 3600, unattended=True, previous=before)
        self.assertEqual(verdict.state, UNKNOWN)

    def test_unattended_a_reading_of_another_run_proves_nothing(self) -> None:
        before = LockReading(beating(host="desktop-b", pid=1), NOW - 3600, NOW - 2 * 3600)
        verdict = self.judge(
            beating(host="desktop-b", pid=2), mtime=NOW - 3600, unattended=True, previous=before
        )
        self.assertEqual(verdict.state, UNKNOWN)

    def test_unattended_never_takes_a_lock_without_a_beat(self) -> None:
        old = legacy(host="desktop-b", started=NOW - LOCK_MAX_AGE - 1)
        verdict = self.judge(old, unattended=True)
        self.assertEqual(verdict.state, LEGACY)
        self.assertIn("12 hours", verdict.reason or "")
        dead = self.judge(legacy(), unattended=True)  # a dead pid here, as for a person
        self.assertEqual(dead.state, LEGACY)
        unreadable = self.judge(None, mtime=NOW - LOCK_MAX_AGE - 1, unattended=True)
        self.assertEqual(unreadable.state, LEGACY)
        # two readings do not change it, however long the lock stays as it is
        before = LockReading(old, NOW, NOW - 10 * STALE_BEAT_SECONDS)
        self.assertEqual(self.judge(old, unattended=True, previous=before).state, LEGACY)

    def test_unattended_a_recent_lock_without_a_beat_is_live(self) -> None:
        self.assertEqual(self.judge(legacy(host="desktop-b"), unattended=True).state, LIVE)

    def test_parse_round_trips_and_rejects_garbage(self) -> None:
        for info in (LockInfo(HERE, 42, NOW), beating(mode="unattended"), beating()):
            self.assertEqual(inbox_lock.parse(inbox_lock.render(info)), info)
        for text in (
            "",
            "nonsense",
            "{}",
            '{"host": "h", "pid": "x", "started": 1}',
            '{"host": "h", "pid": 1, "started": 1, "beat": "60"}',
            '{"host": "h", "pid": 1, "started": 1, "beat": 0}',
        ):
            self.assertIsNone(inbox_lock.parse(text), text)

    def test_a_lock_of_an_older_warpzone_parses_with_no_beat_and_renders_as_it_was(self) -> None:
        text = '{"host": "h", "pid": 7, "started": 5.0}\n'
        self.assertEqual(inbox_lock.parse(text), LockInfo("h", 7, 5.0))
        self.assertEqual(inbox_lock.render(LockInfo("h", 7, 5.0)), text)

    def test_an_unknown_mode_is_read_as_none(self) -> None:
        text = '{"host": "h", "pid": 7, "started": 5.0, "beat": 60, "mode": "robot"}'
        self.assertEqual(inbox_lock.parse(text), LockInfo("h", 7, 5.0, 60, None))

    def test_describe_names_the_unattended_inbox_by_its_mode(self) -> None:
        text = inbox_lock.describe(beating(host="abc123", mode="unattended"), NOW, NOW)
        self.assertIn("unattended inbox on the hub", text)
        self.assertNotIn("abc123", text)
        self.assertIn("abc123", inbox_lock.describe(beating(host="abc123"), NOW, NOW))

    def test_pid_alive_knows_this_process_and_a_finished_one(self) -> None:
        self.assertTrue(inbox_lock.pid_alive(os.getpid()))
        done = subprocess.Popen([sys.executable, "-c", "pass"])
        done.wait()
        self.assertFalse(inbox_lock.pid_alive(done.pid))


class HeartbeatTests(unittest.TestCase):
    """The beat and the thread around it, with the file system and no real waiting."""

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = Path(tmp.name) / LOCK_NAME
        self.mine = beating(pid=os.getpid(), started=NOW)
        self.path.write_text(inbox_lock.render(self.mine))
        age(self.path, 3600)
        self.touches = 0

    def touch(self, path: Path) -> None:
        self.touches += 1
        os.utime(path)

    def test_still_mine_goes_by_host_pid_and_start_not_by_beat_or_mtime(self) -> None:
        self.assertTrue(inbox_lock.still_mine(self.path, self.mine))
        self.assertTrue(inbox_lock.still_mine(self.path, replace(self.mine, beat=5, mode=None)))
        for other in (
            replace(self.mine, pid=self.mine.pid + 1),
            replace(self.mine, host="desktop-b"),
            replace(self.mine, started=NOW + 1),
        ):
            self.assertFalse(inbox_lock.still_mine(self.path, other))
        self.path.write_text("garbage")
        self.assertFalse(inbox_lock.still_mine(self.path, self.mine))
        self.path.unlink()
        self.assertFalse(inbox_lock.still_mine(self.path, self.mine))

    def test_a_beat_moves_the_modification_time_of_this_runs_lock(self) -> None:
        before = self.path.stat().st_mtime
        self.assertTrue(inbox_lock.beat_once(self.path, self.mine, self.touch))
        self.assertGreater(self.path.stat().st_mtime, before + 3000)
        self.assertEqual(self.touches, 1)

    def test_a_beat_leaves_a_lock_that_is_not_this_runs_untouched(self) -> None:
        other = LockInfo("desktop-b", 77, NOW, BEAT_SECONDS, "attended")
        self.path.write_text(inbox_lock.render(other))
        age(self.path, 3600)
        before = self.path.stat().st_mtime
        self.assertFalse(inbox_lock.beat_once(self.path, self.mine, self.touch))
        self.assertEqual(self.path.stat().st_mtime, before)
        self.assertEqual(self.touches, 0)

    def test_a_beat_that_finds_no_lock_is_false(self) -> None:
        self.path.unlink()
        self.assertFalse(inbox_lock.beat_once(self.path, self.mine, self.touch))

    def test_a_share_that_fails_one_touch_does_not_end_the_heartbeat(self) -> None:
        def failing(path: Path) -> None:
            raise PermissionError("busy")

        self.assertTrue(inbox_lock.beat_once(self.path, self.mine, failing))

    def test_the_loop_beats_each_interval_until_told_to_stop(self) -> None:
        waits: list[float] = []

        def wait(seconds: float) -> bool:
            waits.append(seconds)
            return len(waits) > 3

        inbox_lock.Heartbeat(self.path, self.mine, 7, touch=self.touch, wait=wait).run()
        self.assertEqual(waits, [7, 7, 7, 7])
        self.assertEqual(self.touches, 3)

    def test_the_loop_stops_once_the_lock_is_no_longer_this_runs(self) -> None:
        waits: list[float] = []

        def wait(seconds: float) -> bool:
            waits.append(seconds)
            if len(waits) == 2:  # between the first and the second beat somebody takes the lock
                self.path.write_text(inbox_lock.render(LockInfo("desktop-b", 77, NOW, 60, None)))
            return len(waits) > 10

        inbox_lock.Heartbeat(self.path, self.mine, 7, touch=self.touch, wait=wait).run()
        self.assertEqual(waits, [7, 7])
        self.assertEqual(self.touches, 1)
        self.assertIn("desktop-b", self.path.read_text())

    def test_a_read_error_skips_one_beat_and_the_heartbeat_goes_on(self) -> None:
        real = inbox_lock._read
        reads = 0

        def flaky(path: Path):  # type: ignore[no-untyped-def]
            nonlocal reads
            reads += 1
            if reads == 2:  # the share hiccups on the second beat only
                raise OSError("input/output error")
            return real(path)

        waits: list[float] = []

        def wait(seconds: float) -> bool:
            waits.append(seconds)
            return len(waits) > 3

        with mock.patch.object(inbox_lock, "_read", flaky):
            inbox_lock.Heartbeat(self.path, self.mine, 7, touch=self.touch, wait=wait).run()
        self.assertEqual(len(waits), 4)
        self.assertEqual(self.touches, 2)  # beats 1 and 3; beat 2 was skipped

    def test_still_mine_is_not_false_when_the_lock_cannot_be_read(self) -> None:
        with mock.patch.object(inbox_lock, "_read", side_effect=OSError("stale file handle")):
            self.assertTrue(inbox_lock.still_mine(self.path, self.mine))

    def test_stop_does_not_hang_on_a_touch_stuck_on_a_stalled_share(self) -> None:
        entered = threading.Event()
        release = threading.Event()
        self.addCleanup(release.set)

        def stuck(path: Path) -> None:
            entered.set()
            release.wait(30)

        beat = inbox_lock.Heartbeat(self.path, self.mine, 0.01, touch=stuck, stop_wait=0.2)
        beat.start()
        self.assertTrue(entered.wait(10))
        started = time.monotonic()
        beat.stop()  # the thread is still inside the touch
        self.assertLess(time.monotonic() - started, 5)
        self.assertTrue(beat._thread.is_alive())
        self.assertTrue(beat._thread.daemon)

    def test_the_thread_beats_for_real_and_stop_ends_it(self) -> None:
        before = self.path.stat().st_mtime
        beat = inbox_lock.Heartbeat(self.path, self.mine, 0.01)
        beat.start()
        deadline = time.monotonic() + 10
        while self.path.stat().st_mtime == before and time.monotonic() < deadline:
            time.sleep(0.01)
        beat.stop()
        self.assertGreater(self.path.stat().st_mtime, before)
        self.assertFalse(beat._thread.is_alive())


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

        def spy(ctx, item, number, unverified=None):  # type: ignore[no-untyped-def]
            seen.append(inbox_lock.parse(self.lock.read_text()))
            return real(ctx, item, number, unverified)

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

        def steal(ctx, item, number, unverified=None):  # type: ignore[no-untyped-def]
            self.put_lock(host="desktop-b", pid=77)
            return real(ctx, item, number, unverified)

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

    def hold(
        self,
        mode: str = "attended",
        force: bool = False,
        previous: LockReading | None = None,
    ) -> AbstractContextManager[inbox_lock.Held]:
        return inbox_lock.hold(
            self.inbox,
            force=force,
            now=time.time,
            out=self.lines.append,
            mode=mode,
            previous=previous,
        )

    def put_beating(self, mode: str = "attended", age_seconds: float = 0) -> LockInfo:
        info = LockInfo("desktop-b", 77, time.time() - 3600, BEAT_SECONDS, mode)
        self.lock.write_text(inbox_lock.render(info))
        age(self.lock, age_seconds)
        return info

    def test_a_held_lock_records_the_beat_and_the_mode(self) -> None:
        for mode in ("attended", "unattended"):
            with self.hold(mode):
                held = inbox_lock.parse(self.lock.read_text())
            assert held is not None
            self.assertEqual((held.host, held.pid), (HERE, os.getpid()))
            self.assertEqual((held.beat, held.mode), (BEAT_SECONDS, mode))
            self.assertFalse(self.lock.exists())

    def test_the_body_can_ask_whether_the_lock_is_still_its_own(self) -> None:
        with self.hold() as held:
            self.assertTrue(held.still_mine())
            self.put_lock(host="desktop-b", pid=77)
            self.assertFalse(held.still_mine())

    def test_the_heartbeat_runs_while_the_lock_is_held_and_ends_with_it(self) -> None:
        threads: list[inbox_lock.Heartbeat] = []
        real = inbox_lock.Heartbeat

        def spy(*args, **kw):  # type: ignore[no-untyped-def]
            threads.append(real(*args, **kw))
            return threads[-1]

        with mock.patch.object(inbox_lock, "Heartbeat", spy), self.hold():
            self.assertTrue(threads[0]._thread.is_alive())
        self.assertFalse(threads[0]._thread.is_alive())

    def test_a_live_lock_is_busy_for_the_two_modes_and_names_the_holder(self) -> None:
        self.put_beating(age_seconds=30)
        for mode in ("attended", "unattended"):
            with self.assertRaises(LockBusy) as caught:
                self.hold(mode).__enter__()
            self.assertIn("desktop-b", str(caught.exception))
            self.assertIn("10 minutes without a heartbeat", str(caught.exception))
            self.assertIsNotNone(caught.exception.reading)
        self.assertTrue(self.lock.exists())

    def test_attended_a_lock_with_a_silent_beat_is_stale_and_needs_force(self) -> None:
        self.put_beating(age_seconds=11 * 60)
        with self.assertRaises(LockStale) as caught:
            self.hold().__enter__()
        self.assertIn("no heartbeat since", str(caught.exception))
        self.assertIn("--force-lock", str(caught.exception))
        with self.hold(force=True):
            pass
        self.assertIn("taking over a stale lock", "\n".join(self.lines))

    def test_unattended_needs_two_readings_to_take_a_silent_lock(self) -> None:
        info = self.put_beating("unattended", age_seconds=3600)
        with self.assertRaises(LockBusy) as caught:
            self.hold("unattended").__enter__()
        first = caught.exception.reading
        assert first is not None
        self.assertEqual(first.info, info)
        self.assertIn("unattended inbox on the hub", str(caught.exception))
        # the same lock, unchanged, ten minutes later by this clock
        later = LockReading(first.info, first.mtime, time.time() - STALE_BEAT_SECONDS)
        with self.hold("unattended", previous=later):
            taken = inbox_lock.parse(self.lock.read_text())
            assert taken is not None
            self.assertEqual(taken.pid, os.getpid())
        self.assertIn("no heartbeat for 10 minutes", "\n".join(self.lines))
        self.assertFalse(self.lock.exists())

    def test_unattended_does_not_take_a_lock_that_beat_in_between(self) -> None:
        info = self.put_beating("unattended", age_seconds=3600)
        older = LockReading(
            info, os.stat(self.lock).st_mtime - 5, time.time() - 2 * STALE_BEAT_SECONDS
        )
        with self.assertRaises(LockBusy):
            self.hold("unattended", previous=older).__enter__()
        self.assertTrue(self.lock.exists())

    def test_unattended_never_takes_a_lock_of_an_older_warpzone(self) -> None:
        self.put_lock(host="desktop-b", started=time.time() - LOCK_MAX_AGE - 60)
        age(self.lock, LOCK_MAX_AGE + 60)
        with self.assertRaises(LockStale) as caught:
            self.hold("unattended").__enter__()
        self.assertIn("older warpzone", str(caught.exception))
        self.assertIn("--force-lock", str(caught.exception))
        self.assertTrue(self.lock.exists())

    def test_an_unattended_run_cannot_be_forced_and_modes_are_checked(self) -> None:
        with self.assertRaises(ValueError):
            self.hold("unattended", force=True).__enter__()
        with self.assertRaises(ValueError):
            self.hold("robot").__enter__()

    def test_a_lock_created_between_the_look_and_the_take_is_busy_not_an_error(self) -> None:
        real = inbox_lock._create

        def race(path, info):  # type: ignore[no-untyped-def]
            self.put_beating(age_seconds=1)  # somebody else took it first
            real(path, info)

        with mock.patch.object(inbox_lock, "_create", race), self.assertRaises(LockBusy) as caught:
            self.hold("unattended").__enter__()
        self.assertIn("desktop-b", str(caught.exception))
        self.assertTrue(self.lock.exists())

    def fresh_lock_after(self, call: int):  # type: ignore[no-untyped-def]
        """A `_read` that, once the given call has returned, lets another run replace the lock
        (atomically) with a live one of its own: the moment two takers of a stale lock race."""
        real = inbox_lock._read
        calls = 0

        def read(path: Path):  # type: ignore[no-untyped-def]
            nonlocal calls
            result = real(path)
            calls += 1
            if calls == call:
                fresh = self.inbox / "other-run.tmp"
                fresh.write_text(
                    inbox_lock.render(
                        LockInfo("desktop-c", 99, time.time(), BEAT_SECONDS, "attended")
                    )
                )
                os.replace(fresh, self.lock)
            return result

        return read

    def test_a_fresh_lock_that_replaces_the_stale_one_after_it_was_checked_survives(self) -> None:
        self.put_beating(age_seconds=11 * 60)
        with mock.patch.object(inbox_lock, "_read", self.fresh_lock_after(2)):
            try:
                with self.hold(force=True):
                    pass
            except LockBusy:
                pass
        self.assertIn("desktop-c", self.lock.read_text())

    def test_a_fresh_lock_that_replaces_the_stale_one_before_the_takeover_is_put_back(self) -> None:
        self.put_beating(age_seconds=11 * 60)
        with (
            mock.patch.object(inbox_lock, "_read", self.fresh_lock_after(1)),
            self.assertRaises(LockBusy),
        ):
            self.hold(force=True).__enter__()
        self.assertIn("desktop-c", self.lock.read_text())
        self.assertEqual(
            [p.name for p in self.inbox.iterdir() if p.name.startswith(LOCK_NAME)], [LOCK_NAME]
        )

    def test_a_live_lock_moved_aside_survives_a_share_that_has_no_hard_links(self) -> None:
        self.put_beating(age_seconds=11 * 60)
        no_links = OSError(errno.ENOTSUP, "Operation not supported")
        with (
            mock.patch.object(inbox_lock, "_read", self.fresh_lock_after(1)),
            mock.patch.object(os, "link", side_effect=no_links),
            self.assertRaises(LockBusy),
        ):
            self.hold(force=True).__enter__()
        self.assertIn("desktop-c", self.lock.read_text())
        self.assertEqual(
            [p.name for p in self.inbox.iterdir() if p.name.startswith(LOCK_NAME)], [LOCK_NAME]
        )

    def test_a_live_lock_that_cannot_be_put_back_is_kept_aside_and_named(self) -> None:
        self.put_beating(age_seconds=11 * 60)
        real_rename = os.rename
        renames = 0

        def rename(src, dst):  # type: ignore[no-untyped-def]
            nonlocal renames
            renames += 1
            if renames == 2:  # the way back
                raise PermissionError(errno.EPERM, "Operation not permitted")
            real_rename(src, dst)

        with (
            mock.patch.object(inbox_lock, "_read", self.fresh_lock_after(1)),
            mock.patch.object(os, "link", side_effect=OSError(errno.ENOTSUP, "no links")),
            mock.patch.object(os, "rename", rename),
            self.assertRaises(InboxError) as caught,
        ):
            self.hold(force=True).__enter__()
        kept = [p for p in self.inbox.iterdir() if p.name.endswith(".taken")]
        self.assertEqual(len(kept), 1)
        self.assertIn("desktop-c", kept[0].read_text())
        self.assertIn(kept[0].name, str(caught.exception))

    def test_busy_and_stale_are_inbox_errors_so_an_attended_run_still_exits_2(self) -> None:
        self.assertTrue(issubclass(LockBusy, InboxError))
        self.assertTrue(issubclass(LockStale, InboxError))


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
