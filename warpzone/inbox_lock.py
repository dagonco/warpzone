"""The lock that keeps two `warpzone inbox --apply` runs off the same hub inbox.

`inbox/.warpzone.lock` is created atomically (O_CREAT | O_EXCL) and holds the host name, the pid,
the start time, the heartbeat interval (`beat`), the mode of the run and, when the run is not the
inbox itself, its `task` (`scrape`). While a run holds it, a
`Heartbeat` thread touches the file's modification time every `BEAT_SECONDS`, and only while the
file still holds this run's identity.

A lock in the way is live or stale (`judge`, pure). An attended run looks once: a lock with a
heartbeat is stale when its modification time is older than `STALE_BEAT_SECONDS` by this machine's
clock; a lock of an older warpzone (no `beat`) is stale when older than `LOCK_MAX_AGE`; both are
stale when held by a pid that is not running on this same host. An unattended run compares two
readings of the file instead, never its time against the clock, and never takes over a lock
without `beat`. A stale lock is taken over with `--force-lock`, or by an unattended run when it has
a heartbeat; never silently otherwise. See development/design/inbox.md and
development/design/inbox-unattended.md.
"""

from __future__ import annotations

import json
import os
import socket
import threading
import time
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from pathlib import Path

from warpzone.inbox_core import InboxError

LOCK_NAME = ".warpzone.lock"
LOCK_MAX_AGE = 12 * 3600  # seconds; the rule for a lock without a heartbeat
BEAT_SECONDS = 60  # how often a run holding the lock touches it
STOP_WAIT_SECONDS = 5  # how long releasing the lock waits for a heartbeat stuck on the share
STALE_BEAT_SECONDS = 10 * 60  # silence after which a lock with a heartbeat is stale
ATTENDED = "attended"
UNATTENDED = "unattended"
SCRAPE = "scrape"  # the task of a scrape run; a lock with no task is the inbox's own
TASKS = (SCRAPE,)
_SHOWN = f"inbox/{LOCK_NAME}"  # what messages call it: never a path outside the hub

# The verdicts of `judge`.
LIVE = "live"  # the holder is working
STALE = "stale"  # the holder is gone and the lock may be taken over
UNKNOWN = "unknown"  # unattended and one reading is not enough: treat as busy
LEGACY = "legacy"  # stale, but a lock of an older warpzone: only a person takes it over


@dataclass(frozen=True)
class LockInfo:
    host: str
    pid: int
    started: float
    beat: int | None = None  # heartbeat interval in seconds; None for a lock of an older warpzone
    mode: str | None = None  # ATTENDED or UNATTENDED; None for a lock of an older warpzone
    task: str | None = None  # SCRAPE when a scrape holds it; None for the inbox's own run


@dataclass(frozen=True)
class LockReading:
    """What one look at the lock found: its content, its modification time, and when we looked."""

    info: LockInfo | None
    mtime: float
    looked: float


@dataclass(frozen=True)
class Verdict:
    state: str
    reason: str | None = None  # why it is stale, for the messages


class LockBusy(InboxError):
    """Another run holds the lock (live, or not yet provably silent). The message names it.

    This also covers a lock created by someone else between a look and taking it, so a caller
    that wants "busy" to be a normal outcome catches this and not every `InboxError`.
    `reading` is what was seen of the lock, for the next look to compare against."""

    def __init__(self, message: str, reading: LockReading | None = None) -> None:
        super().__init__(message)
        self.reading = reading


class LockStale(InboxError):
    """A stale lock this run may not take over (no `--force-lock`, or a legacy lock unattended)."""

    def __init__(self, message: str, reading: LockReading | None = None) -> None:
        super().__init__(message)
        self.reading = reading


def hostname() -> str:
    return socket.gethostname()


def pid_alive(pid: int) -> bool:
    """Whether process `pid` exists on this machine (when unsure, it does)."""
    if os.name == "nt":  # os.kill(pid, 0) would end the process there
        return True
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except OSError:  # PermissionError: it exists, it is somebody else's
        return True
    return True


def render(info: LockInfo) -> str:
    data: dict[str, object] = {"host": info.host, "pid": info.pid, "started": info.started}
    if info.beat is not None:  # a lock of an older warpzone has neither key
        data["beat"] = info.beat
        data["mode"] = info.mode or ATTENDED
    if info.task is not None:
        data["task"] = info.task
    return json.dumps(data) + "\n"


def parse(text: str) -> LockInfo | None:
    """The lock's content, or None when it is not (yet) a complete lock."""
    try:
        data = json.loads(text)
        host, pid, started = data["host"], data["pid"], data["started"]
    except (ValueError, KeyError, TypeError):
        return None
    if not isinstance(host, str) or isinstance(pid, bool) or not isinstance(pid, int):
        return None
    if isinstance(started, bool) or not isinstance(started, (int, float)):
        return None
    beat = data.get("beat")
    if beat is not None and (isinstance(beat, bool) or not isinstance(beat, int) or beat <= 0):
        return None
    mode = data.get("mode")
    if mode not in (None, ATTENDED, UNATTENDED):
        mode = None
    task = data.get("task")
    if task not in TASKS:  # a task of a newer warpzone, or junk: the lock is still a lock
        task = None
    return LockInfo(host, pid, float(started), beat, mode, task)


def same_run(a: LockInfo | None, b: LockInfo | None) -> bool:
    """Whether two readings are of the lock of one run (its host, pid and start time)."""
    if a is None or b is None:
        return False
    return (a.host, a.pid, a.started) == (b.host, b.pid, b.started)


def _clock(stamp: float) -> str:
    return time.strftime("%H:%M", time.localtime(stamp))


def _legacy_reason(
    info: LockInfo | None, mtime: float, now: float, here: str, alive: Callable[[int], bool]
) -> str | None:
    """The rules of a lock with no heartbeat: age since it started, or a dead pid on this host.

    An unreadable lock (just being written, or damaged) goes by the age of the file."""
    started = mtime if info is None else info.started
    if now - started > LOCK_MAX_AGE:
        return f"older than {LOCK_MAX_AGE // 3600} hours"
    if info is not None and info.host == here and not alive(info.pid):
        return f"process {info.pid} is not running on this host"
    return None


def judge(
    reading: LockReading,
    here: str,
    alive: Callable[[int], bool],
    *,
    unattended: bool = False,
    previous: LockReading | None = None,
) -> Verdict:
    """Whether a lock that is in the way is live or stale, and why (pure).

    Attended (one look): a lock with a heartbeat is stale when its modification time is more than
    STALE_BEAT_SECONDS old by this clock, or its pid is not running on this host; a lock without
    one keeps the rules of an older warpzone (LOCK_MAX_AGE, the pid).

    Unattended: only two readings of the same lock count. A modification time that moved is live;
    the same one at least STALE_BEAT_SECONDS later by this clock is silence (stale). With no
    earlier reading of this lock, or too short a gap, the verdict is UNKNOWN. A lock without a
    heartbeat is never judged stale here: when the old rules say it is, the verdict is LEGACY."""
    info, mtime, now = reading.info, reading.mtime, reading.looked
    if info is None or info.beat is None:
        reason = _legacy_reason(info, mtime, now, here, alive)
        if reason is None:
            return Verdict(LIVE)
        return Verdict(LEGACY if unattended else STALE, reason)
    if not unattended:
        if now - mtime > STALE_BEAT_SECONDS:
            return Verdict(STALE, f"no heartbeat since {_clock(mtime)}")
        if info.host == here and not alive(info.pid):
            return Verdict(STALE, f"process {info.pid} is not running on this host")
        return Verdict(LIVE)
    if previous is None or not same_run(previous.info, info):
        return Verdict(UNKNOWN)
    if previous.mtime != mtime:
        return Verdict(LIVE)
    if now - previous.looked >= STALE_BEAT_SECONDS:
        return Verdict(STALE, f"no heartbeat for {STALE_BEAT_SECONDS // 60} minutes")
    return Verdict(UNKNOWN)


def describe(info: LockInfo | None, mtime: float, now: float) -> str:
    started = mtime if info is None else info.started
    since = time.strftime("%Y-%m-%d %H:%M", time.localtime(started))
    minutes = max(0, int((now - started) // 60))
    ago = f"{minutes // 60} h {minutes % 60} min" if minutes >= 60 else f"{minutes} min"
    if info is None:
        return f"held by an unknown run since {since} ({ago} ago)"
    if info.task == SCRAPE:
        if info.mode == UNATTENDED:
            return f"held by the nightly scrape on the hub since {since} ({ago} ago)"
        who = f"{info.host} (pid {info.pid})"
        return f"held by a scrape run by hand on {who} since {since} ({ago} ago)"
    if info.mode == UNATTENDED:
        return f"held by the unattended inbox on the hub since {since} ({ago} ago)"
    return f"held by {info.host} (pid {info.pid}) since {since} ({ago} ago)"


def _read(path: Path) -> tuple[LockInfo | None, float] | None:
    """(content, mtime), or None when the lock is gone."""
    try:
        mtime = path.stat().st_mtime
        return parse(path.read_text(encoding="utf-8", errors="replace")), mtime
    except FileNotFoundError:
        return None


def look(inbox: Path, now: float) -> LockReading | None:
    """One look at `inbox/.warpzone.lock`: None when there is none. It takes nothing, so a
    caller can tell busy from free before it starts work."""
    try:
        held = _read(inbox / LOCK_NAME)
    except OSError as exc:
        raise InboxError(f"cannot read {_SHOWN}: {exc.strerror or exc}") from exc
    return None if held is None else LockReading(held[0], held[1], now)


def holds(path: Path, mine: LockInfo) -> bool | None:
    """Whether the lock file holds this run's host, pid and start time: True, False when it is
    gone or somebody else's, None when it could not be read (a share that hiccups)."""
    try:
        held = _read(path)
    except OSError:
        return None
    return held is not None and same_run(held[0], mine)


def still_mine(path: Path, mine: LockInfo) -> bool:
    """False only when the lock is gone or somebody else's; a read that fails is not proof."""
    return holds(path, mine) is not False


def beat_once(path: Path, mine: LockInfo, touch: Callable[[Path], None] = os.utime) -> bool:
    """Touch the lock's modification time if it is still this run's.

    False when the lock is gone or not this run's (the heartbeat ends); a read or a touch that
    fails on a share that hiccups skips this beat and is retried at the next one."""
    state = holds(path, mine)
    if state is False:
        return False
    if state is None:
        return True
    try:
        touch(path)
    except FileNotFoundError:
        return False
    except OSError:
        pass
    return True


class Heartbeat:
    """A thread that touches the lock every `interval` seconds while it is this run's.

    It stops by itself when the lock is gone or somebody else's (`beat_once` is False), and on
    `stop()`. `wait(seconds)` returns True when told to stop; tests replace it."""

    def __init__(
        self,
        path: Path,
        mine: LockInfo,
        interval: float = BEAT_SECONDS,
        *,
        touch: Callable[[Path], None] = os.utime,
        wait: Callable[[float], bool] | None = None,
        stop_wait: float = STOP_WAIT_SECONDS,
    ) -> None:
        self.path = path
        self.mine = mine
        self.interval = interval
        self._stop_wait = stop_wait
        self._touch = touch
        self._stopped = threading.Event()
        self._wait = wait or self._stopped.wait
        self._thread = threading.Thread(target=self.run, name="inbox-lock-heartbeat", daemon=True)

    def run(self) -> None:
        while not self._wait(self.interval):
            if not beat_once(self.path, self.mine, self._touch):
                return

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        """End the thread; a touch stuck on a stalled share is left behind (it is a daemon)
        after `stop_wait` seconds, so releasing the lock and Ctrl-C never hang on it."""
        self._stopped.set()
        if self._thread.is_alive():
            self._thread.join(timeout=self._stop_wait)


def _create(path: Path, info: LockInfo) -> None:
    fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(render(info))
    except BaseException:
        path.unlink(missing_ok=True)
        raise


def _busy_message(who: str, info: LockInfo | None) -> str:
    if info is not None and info.beat is not None:
        rule = f"counts as stale after {STALE_BEAT_SECONDS // 60} minutes without a heartbeat"
    else:
        rule = f"counts as stale after {LOCK_MAX_AGE // 3600} hours"
    return (
        f"another warpzone inbox is working on this inbox: {_SHOWN} is {who}. "
        f"Wait for it to finish; if that run is gone, the lock {rule} (or delete {_SHOWN} yourself)"
    )


def _put_back(aside: Path, path: Path) -> None:
    """Return a lock that was moved aside by mistake to its place; the caller looks again.

    `link` puts it back without ever replacing a lock that is there now (EEXIST: that other lock
    stands, and the moved one is dropped). Some shares (SMB mounts) have no hard links, and then
    a `rename` does it, only when nothing is at `path`: that leaves a tiny window in which another
    run could create a lock that the rename then replaces. If it cannot be put back at all, the
    moved file is kept and named, never deleted: it is somebody's live lock."""
    try:
        os.link(aside, path)
    except FileExistsError:
        aside.unlink(missing_ok=True)
        return
    except OSError:
        if os.path.lexists(path):  # another lock took the place
            aside.unlink(missing_ok=True)
            return
        try:
            os.rename(aside, path)
        except OSError as exc:
            raise InboxError(
                f"cannot take over {_SHOWN}: a lock that is not stale was moved aside and cannot "
                f"be put back ({exc.strerror or exc}); it is kept as inbox/{aside.name}. "
                f"Rename it to {_SHOWN} if its run is still working, or delete it"
            ) from exc
        return
    aside.unlink(missing_ok=True)


def _take_stale(path: Path, judged: tuple[LockInfo | None, float]) -> bool:
    """Remove the stale lock that was judged, without ever removing another one.

    Two runs can judge the same lock stale; an unlink after a re-read would let the slower one
    remove the lock the faster one just created. So the file is renamed (atomic) to a name of its
    own first, and deleted only if what was renamed is what was judged. If it is not, it was
    somebody's live lock: it goes back to its place (`_put_back`) and the caller looks again."""
    aside = path.with_name(f"{path.name}.{uuid.uuid4().hex}.taken")
    try:
        os.rename(path, aside)
    except FileNotFoundError:
        return False  # released between the look and now
    except OSError as exc:
        raise InboxError(f"cannot take over {_SHOWN}: {exc.strerror or exc}") from exc
    try:
        same = _read(aside) == judged
    except OSError:  # cannot tell what it is: treat it as live
        same = False
    if same:
        aside.unlink(missing_ok=True)
        return True
    _put_back(aside, path)
    return False


def _acquire(
    path: Path,
    mine: LockInfo,
    *,
    force: bool,
    now: Callable[[], float],
    out: Callable[[str], None],
    previous: LockReading | None = None,
) -> None:
    unattended = mine.mode == UNATTENDED
    last: LockReading | None = None
    for _ in range(3):
        try:
            _create(path, mine)
            return
        except FileExistsError:
            pass
        except OSError as exc:
            raise InboxError(f"cannot create {_SHOWN}: {exc.strerror or exc}") from exc
        held = _read(path)
        if held is None:  # released between our two looks
            continue
        info, mtime = held
        reading = last = LockReading(info, mtime, now())  # one clock for verdict and message
        who = describe(info, mtime, reading.looked)
        verdict = judge(reading, mine.host, pid_alive, unattended=unattended, previous=previous)
        if verdict.state in (LIVE, UNKNOWN):
            raise LockBusy(_busy_message(who, info), reading)
        if verdict.state == LEGACY:
            raise LockStale(
                f"{_SHOWN} is {who}, and is stale ({verdict.reason}), but it is the lock of an "
                "older warpzone, which an unattended run never takes over. "
                "If no other run is working on the inbox, run warpzone inbox --apply "
                "--force-lock yourself",
                reading,
            )
        if not (force or unattended):
            raise LockStale(
                f"{_SHOWN} is {who}, and is stale ({verdict.reason}). "
                "If no other run is working on the inbox, run again with --force-lock",
                reading,
            )
        if not _take_stale(path, held):  # it changed under us: look again
            continue
        out(f"inbox: taking over a stale lock: {who}; stale because {verdict.reason}")
    raise LockBusy(f"could not take {_SHOWN}: another run keeps changing it", last)


def _release(path: Path, mine: LockInfo) -> None:
    if still_mine(path, mine):  # never remove somebody else's lock
        path.unlink(missing_ok=True)


@dataclass(frozen=True)
class Held:
    """What `hold` gives the body: a way to ask whether the lock is still this run's."""

    path: Path
    mine: LockInfo

    def still_mine(self) -> bool:
        return still_mine(self.path, self.mine)


@contextmanager
def hold(
    inbox: Path,
    *,
    force: bool,
    now: Callable[[], float],
    out: Callable[[str], None],
    mode: str = ATTENDED,
    task: str | None = None,
    previous: LockReading | None = None,
    interval: float = BEAT_SECONDS,
) -> Iterator[Held]:
    """Hold `inbox/.warpzone.lock` for the body, with a heartbeat; it is released on any exit.

    `mode` is ATTENDED or UNATTENDED; `task` is SCRAPE for a scrape run and None for the inbox
    itself. An unattended run takes over a silent lock by itself
    (judged against `previous`, the reading of an earlier run) and cannot be forced. A lock
    held by someone else raises `LockBusy`; a stale one it may not take, `LockStale`."""
    if mode not in (ATTENDED, UNATTENDED):
        raise ValueError(f"unknown lock mode {mode!r}")
    if force and mode == UNATTENDED:
        raise ValueError("an unattended run never forces a lock")
    path = inbox / LOCK_NAME
    mine = LockInfo(hostname(), os.getpid(), now(), BEAT_SECONDS, mode, task)
    _acquire(path, mine, force=force, now=now, out=out, previous=previous)
    heartbeat: Heartbeat | None = None
    try:
        heartbeat = Heartbeat(path, mine, interval)
        heartbeat.start()
        yield Held(path, mine)
    finally:
        if heartbeat is not None:
            heartbeat.stop()
        with suppress(OSError):  # a lock we cannot remove must not hide the error behind it
            _release(path, mine)
