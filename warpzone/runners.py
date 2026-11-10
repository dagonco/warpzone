"""The subprocess edge: the commands warpzone runs on a device, each behind a small function.

The device commands take them through `probes.DeviceProbes` (`FlatpakRunner`, `ImportCheck`,
`SystemctlRunner` and the rest), so tests pass a fake and nothing here is unit-tested without
a real command. `run_tool` is the exception: the inbox and `ra.hash_image` call it directly
and their tests run stand-in tools.
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


PIPE_DRAIN_SECONDS = 2.0  # most a killed tool's pipes are read for; tests shorten it


def kill_group(proc: subprocess.Popen[str], *, drain: bool) -> None:
    """Kill the tool's whole process group, reap the tool and close its pipes (reading first
    what is left in them when `drain`). The tool started in a session of its own, so its pid
    is the id of the group."""
    # OSError, not only ProcessLookupError: macOS answers EPERM for a group that holds only
    # a zombie (the tool ended just as the limit ran out).
    with contextlib.suppress(OSError):
        os.killpg(proc.pid, signal.SIGKILL)
    try:
        if drain:  # for no longer than a process outside the group may hold the pipes open
            proc.communicate(timeout=PIPE_DRAIN_SECONDS)
    except subprocess.TimeoutExpired:
        pass
    finally:
        for pipe in (proc.stdout, proc.stderr):
            if pipe is not None:
                pipe.close()
        proc.wait()


def run_tool(
    cmd: Sequence[str], timeout: float, *, stdin: int | None = None
) -> subprocess.CompletedProcess[str]:
    """Run a tool, capturing its output as text, for at most `timeout` seconds. A tool that
    runs longer is killed with every process it started and `subprocess.TimeoutExpired` is
    raised. The group is killed too when the wait is interrupted (Ctrl-C included), because
    a session of its own no longer gets the terminal's signals; the exception is re-raised."""
    # A session of its own has no controlling terminal: a tool that wants to prompt on it
    # fails at once, where in our session it would be stopped (SIGTTIN) for the whole limit.
    proc = subprocess.Popen(
        list(cmd),
        stdin=stdin,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        errors="replace",
        start_new_session=True,
    )
    try:
        stdout, stderr = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        kill_group(proc, drain=True)
        raise
    except BaseException:
        kill_group(proc, drain=False)
        raise
    return subprocess.CompletedProcess(proc.args, proc.returncode, stdout, stderr)


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
