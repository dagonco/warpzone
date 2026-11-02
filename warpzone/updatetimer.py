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
