"""Process detection: whether a program is running, read from `/proc`.

Nothing here touches the network or Syncthing. See docs/design.md, "Process detection".
"""

from __future__ import annotations

import os
from collections.abc import Iterable
from pathlib import Path

COMM_MAX = 15  # the kernel keeps at most 15 characters of a process name


class NoProc(Exception):
    """There is no /proc to walk (macOS)."""


def _parent_of(proc: Path, pid: int) -> int | None:
    try:
        text = (proc / str(pid) / "stat").read_text()
        return int(text.rpartition(")")[2].split()[1])
    except (OSError, ValueError, IndexError):
        return None


def running(
    names: Iterable[str],
    proc: Path = Path("/proc"),
    self_pid: int | None = None,
    parent_pid: int | None = None,
    *,
    skip_ancestors: bool = False,
) -> bool:
    """Whether a process is one of `names`, judged by `comm` and the basename of `argv[0]`.

    The rest of a command line is never searched, so a shell, an editor or a launcher
    script that merely mentions the emulator does not count. Our own process and its
    parent are skipped, and with `skip_ancestors` every process above it: a program that
    runs this from a script and waits for it. Entries that cannot be read are ignored.
    """
    wanted = set(names)
    wanted_comm = {n[:COMM_MAX] for n in wanted}
    if not wanted:
        return False
    if not proc.is_dir():
        raise NoProc(f"{proc} is not available")
    if self_pid is None:
        self_pid = os.getpid()
        if parent_pid is None:
            parent_pid = os.getppid()
    elif parent_pid is None:
        parent_pid = _parent_of(proc, self_pid)
    skip = {self_pid, parent_pid}
    while skip_ancestors and parent_pid and parent_pid > 1:
        parent_pid = _parent_of(proc, parent_pid)
        if parent_pid in skip:
            break
        skip.add(parent_pid)
    try:
        entries = list(proc.iterdir())
    except OSError as exc:
        raise NoProc(f"{proc} cannot be read: {exc}") from exc
    for entry in entries:
        if not entry.name.isdigit() or int(entry.name) in skip:
            continue
        try:
            comm = (entry / "comm").read_text(errors="replace").rstrip("\n")
        except OSError:
            comm = None
        if comm is not None and comm in wanted_comm:
            return True
        try:
            raw = (entry / "cmdline").read_bytes()
        except OSError:
            continue
        argv0 = raw.split(b"\0", 1)[0].decode(errors="replace")
        if argv0 and os.path.basename(argv0) in wanted:
            return True
    return False
