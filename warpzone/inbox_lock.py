"""The lock that keeps two `warpzone inbox --apply` runs off the same hub inbox.

`inbox/.warpzone.lock` is created atomically (O_CREAT | O_EXCL) and holds the host name,
the pid and the start time of the run. A lock in the way is live, or stale: older than
`LOCK_MAX_AGE`, or held by a pid that is not running on this same host. A stale lock is
only taken over with `--force-lock`, never silently. See development/design/inbox.md.
"""

from __future__ import annotations

import json
import os
import socket
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from pathlib import Path

from warpzone.inbox_core import InboxError

LOCK_NAME = ".warpzone.lock"
LOCK_MAX_AGE = 12 * 3600  # seconds
_SHOWN = f"inbox/{LOCK_NAME}"  # what messages call it: never a path outside the hub


@dataclass(frozen=True)
class LockInfo:
    host: str
    pid: int
    started: float


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
    return json.dumps({"host": info.host, "pid": info.pid, "started": info.started}) + "\n"


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
    return LockInfo(host, pid, float(started))


def judge(
    info: LockInfo | None,
    mtime: float,
    now: float,
    here: str,
    alive: Callable[[int], bool],
) -> str | None:
    """Why a lock that is in the way is stale, or None when it is live.

    An unreadable lock (just being written, or damaged) goes by the age of the file."""
    started = mtime if info is None else info.started
    if now - started > LOCK_MAX_AGE:
        return f"older than {LOCK_MAX_AGE // 3600} hours"
    if info is not None and info.host == here and not alive(info.pid):
        return f"process {info.pid} is not running on this host"
    return None


def describe(info: LockInfo | None, mtime: float, now: float) -> str:
    started = mtime if info is None else info.started
    since = time.strftime("%Y-%m-%d %H:%M", time.localtime(started))
    minutes = max(0, int((now - started) // 60))
    ago = f"{minutes // 60} h {minutes % 60} min" if minutes >= 60 else f"{minutes} min"
    if info is None:
        return f"held by an unknown run since {since} ({ago} ago)"
    return f"held by {info.host} (pid {info.pid}) since {since} ({ago} ago)"


def _read(path: Path) -> tuple[LockInfo | None, float] | None:
    """(content, mtime), or None when the lock is gone."""
    try:
        mtime = path.stat().st_mtime
        return parse(path.read_text(encoding="utf-8", errors="replace")), mtime
    except FileNotFoundError:
        return None


def _create(path: Path, info: LockInfo) -> None:
    fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(render(info))
    except BaseException:
        path.unlink(missing_ok=True)
        raise


def _acquire(
    path: Path,
    mine: LockInfo,
    *,
    force: bool,
    now: Callable[[], float],
    out: Callable[[str], None],
) -> None:
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
        here = now()  # one clock for the verdict and the message
        who = describe(info, mtime, here)
        reason = judge(info, mtime, here, mine.host, pid_alive)
        if reason is None:
            raise InboxError(
                f"another warpzone inbox is working on this inbox: {_SHOWN} is {who}. "
                "Wait for it to finish; if that run is gone, the lock counts as stale "
                f"after {LOCK_MAX_AGE // 3600} hours (or delete {_SHOWN} yourself)"
            )
        if not force:
            raise InboxError(
                f"{_SHOWN} is {who}, and is stale ({reason}). "
                "If no other run is working on the inbox, run again with --force-lock"
            )
        if _read(path) != held:  # re-check right before acting: it changed under us
            continue
        out(f"inbox: taking over a stale lock: {who}; stale because {reason}")
        path.unlink(missing_ok=True)
    raise InboxError(f"could not take {_SHOWN}: another run keeps changing it")


def _release(path: Path, mine: LockInfo) -> None:
    held = _read(path)
    if held is not None and held[0] == mine:  # never remove somebody else's lock
        path.unlink(missing_ok=True)


@contextmanager
def hold(
    inbox: Path,
    *,
    force: bool,
    now: Callable[[], float],
    out: Callable[[str], None],
) -> Iterator[None]:
    """Hold `inbox/.warpzone.lock` for the body; it is released on any exit."""
    path = inbox / LOCK_NAME
    mine = LockInfo(hostname(), os.getpid(), now())
    _acquire(path, mine, force=force, now=now, out=out)
    try:
        yield
    finally:
        with suppress(OSError):  # a lock we cannot remove must not hide the error behind it
            _release(path, mine)
