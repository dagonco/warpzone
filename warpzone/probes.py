"""What a command running on a device reads from the machine or runs on it.

`DeviceProbes` groups the edges `provision device`, `frontend refresh` and `prune` have to the
system: the process table, the checks that run a subprocess, the commands (`flatpak`,
`systemctl`, `sh`) and the clock. The defaults are the real thing; a test passes one object
with the pieces it fakes instead of patching each function. See "Code layout" in
development/design.md.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

from warpzone.runners import (
    FlatpakRunner,
    ImportCheck,
    SystemctlRunner,
    run_flatpak,
    run_import_check,
    run_script_check,
    run_systemctl,
    run_user_systemctl,
)


@dataclass(frozen=True)
class DeviceProbes:
    """The system edges of a device command. Every field defaults to the real thing."""

    # The process table: `procs.running` reads it to tell whether an emulator is open.
    proc: Path = Path("/proc")
    # Whether an interpreter imports warpzone from outside the current folder.
    import_check: ImportCheck = run_import_check
    # `flatpak ...`: (exit code, output).
    flatpak: FlatpakRunner = run_flatpak
    # `systemctl ...` with the whole command given: (exit code, output).
    systemctl: FlatpakRunner = run_systemctl
    # `systemctl --user <args>`: whether it succeeded.
    user_systemctl: SystemctlRunner = run_user_systemctl
    # `sh -n` and the updater's selftest: (exit code, output), cut off after the timeout.
    script_check: Callable[[Sequence[str], float], tuple[int, str]] = run_script_check
    sleep: Callable[[float], None] = time.sleep
    clock: Callable[[], float] = time.monotonic


REAL_PROBES = DeviceProbes()
