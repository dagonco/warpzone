"""The systemd user timer that starts one tick of the updater. Pure: no I/O.

`provision device` writes the two units and enables the timer on every device, whatever its
profile; `doctor` looks for them. The unit keeps the name `warpzone-self-update`, so writing it
leaves no orphan unit. The timer is a piece of its own, apart from the script it starts
(`updater.sh tick` does not know what ran it): a platform without systemd can start the same
tick from another launcher. See operation/self-update.md.
"""

from __future__ import annotations

from warpzone.timers import exec_word

NAME = "warpzone-self-update"
SERVICE = f"{NAME}.service"
TIMER = f"{NAME}.timer"
UNIT_DIR = ".config/systemd/user"
# No random delay: it also pushes back the start after boot, when the menu may not be open yet.
BOOT_SEC = "45s"
INTERVAL = "15min"
TIMEOUT_START = "30min"
SHELL = "/bin/sh"


def expected_command(updater: str) -> list[str]:
    """The words of the `ExecStart=` line `render_service` writes."""
    return [SHELL, updater, "tick"]


def render_service(updater: str) -> str:
    """The one-shot service: one tick of the live copy of the script, run by `sh` so that the
    script needs no execute bit and no interpreter line. The tick keeps itself inside 25
    minutes, under `TimeoutStartSec`."""
    return (
        "[Unit]\n"
        "Description=Keep warpzone on a release that works\n"
        "# Written by warpzone provision device, which rewrites it.\n"
        "\n"
        "[Service]\n"
        "Type=oneshot\n"
        f"TimeoutStartSec={TIMEOUT_START}\n"
        f"ExecStart={SHELL} {exec_word(updater)} tick\n"
    )


def render_timer() -> str:
    return (
        "[Unit]\n"
        "Description=Run the warpzone updater after boot and every few minutes\n"
        "# Written by warpzone provision device, which rewrites it.\n"
        "\n"
        "[Timer]\n"
        f"OnBootSec={BOOT_SEC}\n"
        f"OnUnitActiveSec={INTERVAL}\n"
        "\n"
        "[Install]\n"
        "WantedBy=timers.target\n"
    )
