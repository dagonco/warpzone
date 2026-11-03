"""What `warpzone` knows about the updater script (`warpzone/data/updater.sh`).

The script decides which release a device runs (see docs/design.md, "Autonomous updates
(contract)"), so this module holds only what the rest of warpzone needs from it: the order of
release tags, where the script's live copy, settings and state are, the settings file that
`provision device` writes for it, the rule for renewing the live copy, a reader for its state,
and `warpzone self-update`, which is a wrapper that runs one tick. The functions are pure
except `run_tick`; see docs/self-update.md.
"""

from __future__ import annotations

import calendar
import re
import subprocess
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from warpzone import esde

if TYPE_CHECKING:
    from warpzone.profiles import Profile

# The script that ships with this version of warpzone.
UPDATER_SOURCE = Path(__file__).resolve().parent / "data" / "updater.sh"
UPDATER_DIR = "updater"
LIVE_REL = Path(".local/share/warpzone") / UPDATER_DIR / "updater.sh"
# What `provision device` writes for the script, in this order.
CONF_KEYS = ("device", "config", "clone", "python", "channel", "pin", "enabled", "frontend")

_ABI = re.compile(r"^UPDATER_ABI=(\d+)[ \t]*$", re.MULTILINE)
_BAD_DATE = "%Y-%m-%dT%H:%M:%SZ"


# ------------------------------------------------------------------------------ locations


def package_root() -> Path:
    """The folder that holds the `warpzone` package: the checkout for a device install."""
    return Path(__file__).resolve().parent.parent


def live_path(home: Path) -> Path:
    """The live copy of the script, outside the clone, so that checking out any tag cannot
    remove or replace it."""
    return home / LIVE_REL


def conf_path(state_base: Path) -> Path:
    """The settings file; `state_base` is `~/.local/state/warpzone`."""
    return state_base / UPDATER_DIR / "conf"


def state_path(state_base: Path) -> Path:
    return state_base / UPDATER_DIR / "state"


def bad_dir(state_base: Path) -> Path:
    """One file per tag the device found bad and will not try again."""
    return state_base / UPDATER_DIR / "bad"


# ----------------------------------------------------------------------------- the settings


def render_conf(
    *,
    device: str,
    config: str | None,
    clone: str | Path,
    python: str | Path,
    channel: str,
    pin: str | None,
    enabled: bool,
    frontend: Sequence[str],
) -> str:
    """The settings the script reads: `key=value` per line, nothing it could run (it reads
    the file line by line and never sources it). A value that could end its line raises
    ValueError."""
    values = {
        "device": device,
        "config": config or "",
        "clone": str(clone),
        "python": str(python),
        "channel": channel,
        "pin": pin or "",
        "enabled": "true" if enabled else "false",
        "frontend": " ".join(frontend),
    }
    for key, value in values.items():
        if any(c in value for c in "\r\n\0"):
            raise ValueError(f"{key} has a line break or a NUL, which a settings line cannot hold")
    lines = ["# Written by warpzone provision device, which rewrites it."]
    lines += [f"{key}={values[key]}" for key in CONF_KEYS]
    return "\n".join(lines) + "\n"


def frontend_names(profile: Profile) -> tuple[str, ...]:
    """The process names that show the frontend's menu is open: the profile's own list when it
    has one (EmulationStation on dArkOS), ES-DE's otherwise."""
    rule = profile.es_systems
    return tuple(rule.process) if rule is not None else tuple(esde.ESDE_PROCESSES)


def parse_kv(text: str) -> dict[str, str]:
    """The `key=value` lines of the settings or the state, read the way the script reads them:
    comments and lines without `=` are skipped, and a later line wins."""
    found: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.rstrip("\r")
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        found[key] = value
    return found


@dataclass(frozen=True)
class Bad:
    """A tag the device marked bad: the commit, when (None when unreadable) and why."""

    sha: str
    when: int | None
    reason: str


def parse_bad(text: str) -> Bad:
    """A `bad/<tag>` file: `sha=`, `date=` (UTC, `%Y-%m-%dT%H:%M:%SZ`) and `reason=`."""
    found = parse_kv(text)
    when: int | None
    try:
        when = calendar.timegm(time.strptime(found.get("date", ""), _BAD_DATE))
    except ValueError:
        when = None
    return Bad(found.get("sha", ""), when, found.get("reason", ""))


# ------------------------------------------------------------------------ the live copy


def updater_abi(text: str) -> int | None:
    """The `UPDATER_ABI=N` line of a script (the first one), or None."""
    match = _ABI.search(text)
    return int(match.group(1)) if match else None


def install_decision(candidate: str, live: str | None) -> str:
    """What to do with a new script against the live one: `invalid` (the candidate has no
    ABI), `install` (nothing is there, or it is another script of an ABI that is not higher,
    or one with no readable ABI), `current` (the same text) or `older` (the live copy has a
    higher ABI: a rollback to an older release must not downgrade the script that runs it)."""
    new = updater_abi(candidate)
    if new is None:
        return "invalid"
    if live is None:
        return "install"
    if live == candidate:
        return "current"
    have = updater_abi(live)
    if have is not None and have > new:
        return "older"
    return "install"


# ------------------------------------------------------------------------------ the wrapper


def _run(command: Sequence[str]) -> int:
    return subprocess.run(list(command), stdin=subprocess.DEVNULL, check=False).returncode


def run_tick(
    live: Path,
    out: Callable[[str], None],
    *,
    run: Callable[[Sequence[str]], int] = _run,
) -> int:
    """`warpzone self-update`: run one tick of the live copy of the script, as the timer does,
    and return its exit code. Without a live copy, say what to do and return 1."""
    if not live.is_file():
        out(
            f"self-update: there is no updater at {live}. `warpzone provision device <name>` "
            "installs it from a git clone that was set up with "
            "`sh <clone>/warpzone/data/updater.sh install <name>` (see docs/self-update.md)"
        )
        return 1
    try:
        return run(["sh", str(live), "tick"])
    except OSError as exc:
        out(f"self-update: cannot run sh {live} tick ({exc})")
        return 1
