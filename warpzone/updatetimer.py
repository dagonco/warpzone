"""The systemd user timer that starts one tick of the updater. Pure: no I/O.

`provision device` writes the two units and enables the timer on every device, whatever its
profile; `doctor` looks for them. The unit keeps the name `warpzone-self-update`, so writing it
leaves no orphan unit. The timer is a piece of its own, apart from the script it starts
(`updater.sh tick` does not know what ran it): a platform without systemd can start the same
tick from another launcher. See operation/self-update.md.
"""

from __future__ import annotations

import re

NAME = "warpzone-self-update"
SERVICE = f"{NAME}.service"
TIMER = f"{NAME}.timer"
UNIT_DIR = ".config/systemd/user"
# No random delay: it also pushes back the start after boot, when the menu may not be open yet.
BOOT_SEC = "45s"
INTERVAL = "15min"
TIMEOUT_START = "30min"
SHELL = "/bin/sh"

_PLAIN = re.compile(r"[A-Za-z0-9_@+=:,./-]+")


def exec_word(value: str) -> str:
    """`value` as one word of an `ExecStart=` line: quoted when it needs it, with the `%`
    and `$` that systemd expands doubled."""
    if _PLAIN.fullmatch(value):
        return value
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return '"' + escaped.replace("%", "%%").replace("$", "$$") + '"'


def exec_command(text: str) -> list[str] | None:
    """The words of the first `ExecStart=` line of a unit, or None when it has none.

    Undoes `exec_word`: double quotes group a word and `\\` escapes inside them, and systemd's
    `%%` and `$$` are one `%` and one `$`.
    """
    for line in text.splitlines():
        key, sep, value = line.partition("=")
        if sep and key.strip() == "ExecStart":
            return _words(value.strip())
    return None


def _words(line: str) -> list[str]:
    words: list[str] = []
    word = ""
    open_word = quoted = escaped = False
    for char in line:
        if escaped:
            word += char
            escaped = False
        elif quoted and char == "\\":
            escaped = True
        elif char == '"':
            quoted = not quoted
            open_word = True
        elif char.isspace() and not quoted:
            if open_word:
                words.append(word)
                word, open_word = "", False
        else:
            word += char
            open_word = True
    if open_word:
        words.append(word)
    return [w.replace("%%", "%").replace("$$", "$") for w in words]


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
