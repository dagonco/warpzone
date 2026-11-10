"""The `systemd --user` timer units that warpzone installs on a console. Pure: no I/O.

Four pairs of a one-shot service and a timer run a warpzone command in the background: the
frontend refresh, the play state sync, the unattended prune and the ES-DE settings. `provision
device` writes them (see `provision_timers.py`); `doctor` and `health` look for them by name. The
updater's own pair is apart, in `updatetimer.py`, but quotes its `ExecStart=` words with the same
`exec_word`. See development/design/code-and-testing.md, "Code layout", and the page of each
command for what its timer runs.
"""

from __future__ import annotations

import re
from pathlib import Path

ARTWORK_UNIT = "warpzone-artwork"
PLAY_STATE_UNIT = "warpzone-play-state"
PRUNE_UNIT = "warpzone-prune"
SETTINGS_UNIT = "warpzone-settings"
SETTINGS_EMULATOR = "esde"
SETTINGS_BOOT = "5min"
SETTINGS_INTERVAL = "15min"
PRUNE_BOOT = "15min"
PRUNE_INTERVAL = "1h"

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


def _units(
    command: list[str], config_path: Path | None, interval: str, what: str, boot: str = "5min"
) -> tuple[str, str]:
    if config_path is not None:
        command = [*command, "--config", str(config_path)]
    # Nice and the idle I/O class keep a background run from competing with a game for the CPU
    # and the disk (the I/O class only where the scheduler honours it).
    service = (
        "[Unit]\n"
        f"Description=warpzone: {what}\n"
        "\n"
        "[Service]\n"
        "Type=oneshot\n"
        f"ExecStart={' '.join(exec_word(a) for a in command)}\n"
        "Nice=10\n"
        "IOSchedulingClass=idle\n"
    )
    timer = (
        "[Unit]\n"
        f"Description=warpzone: {what} regularly\n"
        "\n"
        "[Timer]\n"
        f"OnBootSec={boot}\n"
        f"OnUnitActiveSec={interval}\n"
        "\n"
        "[Install]\n"
        "WantedBy=timers.target\n"
    )
    return service, timer


def render_artwork_units(
    python: str, name: str, config_path: Path | None, interval: str
) -> tuple[str, str]:
    """The text of the `systemd --user` service and timer that run `frontend refresh`."""
    command = [python, "-m", "warpzone", "frontend", "refresh", name]
    return _units(command, config_path, interval, "refresh the frontend gamelists")


def render_play_state_units(
    python: str, name: str, config_path: Path | None, interval: str
) -> tuple[str, str]:
    """The text of the `systemd --user` service and timer that run `play-state sync`."""
    command = [python, "-m", "warpzone", "play-state", "sync", name, "--quiet"]
    return _units(command, config_path, interval, "merge favourites and play statistics")


def render_prune_units(python: str, name: str, config_path: Path | None) -> tuple[str, str]:
    """The text of the `systemd --user` service and timer that run `prune --apply --unattended`."""
    command = [python, "-m", "warpzone", "prune", name, "--apply", "--unattended"]
    return _units(
        command,
        config_path,
        PRUNE_INTERVAL,
        "delete the files this device no longer whitelists",
        boot=PRUNE_BOOT,
    )


def render_settings_units(python: str, name: str, config_path: Path | None) -> tuple[str, str]:
    """The text of the `systemd --user` service and timer that run `settings apply` for ES-DE."""
    command = [
        python, "-m", "warpzone", "settings", "apply", "--device", name,
        "--emulator", SETTINGS_EMULATOR, "--when-idle", "--quiet",
    ]  # fmt: skip
    return _units(
        command,
        config_path,
        SETTINGS_INTERVAL,
        "apply the shared ES-DE settings while ES-DE is closed",
        boot=SETTINGS_BOOT,
    )
