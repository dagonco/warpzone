"""The base of warpzone's own errors, and the exit codes the command line answers with.

Every error a command reports to the user derives from `WarpzoneError` and carries the exit
code of the command that fails with it: `cli.main` prints its message and returns that code, so
a new error class needs no change there. Errors that only steer the flow inside one module
(a single item that cannot be placed, a process that is not there) stay plain `Exception`s.
The module imports nothing from warpzone, so loading it costs nothing (see "Speed" in
development/design/emulator-settings.md).
"""

from __future__ import annotations

from typing import ClassVar

EXIT_OK, EXIT_RUNTIME, EXIT_USAGE = 0, 1, 2
# `provision device --unattended`: a write waits for the menu to close; try again later.
EXIT_DEFERRED = 3


class WarpzoneError(Exception):
    """An error with a message for the user; `exit_code` is what the command exits with."""

    exit_code: ClassVar[int] = EXIT_RUNTIME


class UsageError(WarpzoneError):
    """The command was given something it cannot work with (configuration, arguments)."""

    exit_code: ClassVar[int] = EXIT_USAGE
