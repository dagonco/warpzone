"""Stand-in tools that start a child of their own, to test that a call kills the whole group."""

import contextlib
import os
import tempfile
import time
import unittest
from pathlib import Path

# A tool that starts a child of its own, writes the child's PID to the file named by $1 and
# waits for it. The child inherits the tool's process group, stdout and stderr.
SPAWNER = 'sleep 60 & echo $! > "$1.tmp" && mv "$1.tmp" "$1"; wait'


def alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


class GroupKillCase(unittest.TestCase):
    """A test case with a file where a stand-in tool writes the PID of its child, and the
    checks that the child is gone afterwards."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.pidfile = Path(self._tmp.name) / "child.pid"

    def child_pid(self, deadline: float = 5.0) -> int:
        end = time.monotonic() + deadline
        while not self.pidfile.exists() and time.monotonic() < end:
            time.sleep(0.01)
        pid = int(self.pidfile.read_text())
        self.addCleanup(self.reap, pid)  # never leave a stand-in behind, whatever happens
        return pid

    @staticmethod
    def reap(pid: int) -> None:
        with contextlib.suppress(ProcessLookupError):
            os.kill(pid, 9)

    def assert_gone(self, pid: int) -> None:
        # A killed child is reparented to init, which reaps it a moment later; until then
        # kill(pid, 0) still succeeds on the zombie, so wait for the real disappearance.
        end = time.monotonic() + 5.0
        while alive(pid) and time.monotonic() < end:
            time.sleep(0.01)
        self.assertFalse(alive(pid), f"process {pid} of the killed tool is still there")
