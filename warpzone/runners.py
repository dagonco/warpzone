"""The subprocess edge: the commands warpzone runs on a device, each behind a small function.

The callers take these functions as parameters (`FlatpakRunner`, `ImportCheck`,
`SystemctlRunner`), so tests pass a fake and nothing here is unit-tested without a real
command.
"""

from __future__ import annotations

import contextlib
import os
import signal
import subprocess
import tempfile
from collections.abc import Callable, Sequence

FlatpakRunner = Callable[[Sequence[str]], tuple[int, str]]


def run_flatpak(command: Sequence[str]) -> tuple[int, str]:
    """(exit code, stdout and stderr) of a `flatpak` command; raises OSError if it cannot run."""
    proc = subprocess.run(
        list(command),
        capture_output=True,
        text=True,
        errors="replace",
        timeout=60,
        stdin=subprocess.DEVNULL,
        check=False,
    )
    return proc.returncode, (proc.stdout or "") + (proc.stderr or "")


ImportCheck = Callable[[str], bool]


def run_import_check(python: str) -> bool:
    """Whether `python` imports warpzone from outside the current folder.

    Run from `/` and without the variables the startup script drops, so a clone in the
    current folder or on PYTHONPATH does not make an interpreter look good that ES-DE's
    script could not use.
    """
    env = {k: v for k, v in os.environ.items() if k not in ("PYTHONPATH", "PYTHONHOME")}
    try:
        proc = subprocess.run(
            [python, "-c", "import warpzone"],
            cwd="/",
            env=env,
            capture_output=True,
            timeout=60,
            stdin=subprocess.DEVNULL,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return proc.returncode == 0


def run_systemctl(command: Sequence[str]) -> tuple[int, str]:
    """(exit code, stdout and stderr) of a `systemctl` command; raises OSError if it cannot run."""
    return run_flatpak(command)


def run_script_check(command: Sequence[str], timeout: float) -> tuple[int, str]:
    """(exit code, output) of a command that is cut off after `timeout` seconds, with all
    its children: its output goes to a file, so a child that outlives it cannot hold a pipe
    open. The exit code is 124 when it ran out of time."""
    with tempfile.TemporaryFile() as out:
        proc = subprocess.Popen(
            list(command),
            stdin=subprocess.DEVNULL,
            stdout=out,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        try:
            code = proc.wait(timeout)
        except subprocess.TimeoutExpired:
            with contextlib.suppress(OSError):
                os.killpg(proc.pid, signal.SIGKILL)
            proc.wait()
            code = 124
        out.seek(0)
        text = out.read().decode("utf-8", errors="replace")
    return code, text


SystemctlRunner = Callable[[Sequence[str]], bool]


def run_user_systemctl(args: Sequence[str]) -> bool:
    """Whether `systemctl --user <args>` succeeded."""
    try:
        proc = subprocess.run(
            ["systemctl", "--user", *args],
            capture_output=True,
            timeout=60,
            stdin=subprocess.DEVNULL,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return proc.returncode == 0
