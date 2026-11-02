"""The systemd user timer that runs `warpzone self-update`. Pure: no I/O.

`provision device` writes the two units and enables the timer on every device, whatever its
profile; `doctor` looks for them. See docs/self-update.md.
"""

from __future__ import annotations

import re

NAME = "warpzone-self-update"
SERVICE = f"{NAME}.service"
TIMER = f"{NAME}.timer"
UNIT_DIR = ".config/systemd/user"
BOOT_DELAY_MINUTES = 5
SPREAD_MINUTES = 30

_PLAIN = re.compile(r"[A-Za-z0-9_@+=:,./-]+")


def exec_word(value: str) -> str:
    """`value` as one word of an `ExecStart=` line: quoted when it needs it, with the `%`
    and `$` that systemd expands doubled."""
    if _PLAIN.fullmatch(value):
        return value
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return '"' + escaped.replace("%", "%%").replace("$", "$$") + '"'


def render_service(python: str) -> str:
    return (
        "[Unit]\n"
        "Description=Update warpzone to the newest release\n"
        "# Written by warpzone provision device, which rewrites it.\n"
        "\n"
        "[Service]\n"
        "Type=oneshot\n"
        f"ExecStart={exec_word(python)} -m warpzone self-update\n"
    )


def render_timer() -> str:
    return (
        "[Unit]\n"
        "Description=Look for a new warpzone release every day\n"
        "# Written by warpzone provision device, which rewrites it.\n"
        "\n"
        "[Timer]\n"
        "OnCalendar=daily\n"
        # A device that was off at the usual time runs it as soon as it is on again.
        "Persistent=true\n"
        f"OnBootSec={BOOT_DELAY_MINUTES}min\n"
        f"RandomizedDelaySec={SPREAD_MINUTES}min\n"
        "\n"
        "[Install]\n"
        "WantedBy=timers.target\n"
    )
