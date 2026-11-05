"""What `warpzone` knows about the updater script (`warpzone/data/updater.sh`).

The script decides which release a device runs (see
development/design/autonomous-updates.md), so this module holds only what the rest of warpzone
needs from it: the order of release tags, where the script's live copy, settings and state are,
the settings file that `provision device` writes for it, the rule for renewing the live copy, a
reader for its state, `warpzone self-update`, which is a wrapper that runs one tick, and
`warpzone self-update --channel`, which changes the channel in the device's config. The
functions are pure except `run_tick` and `change_channel`; see operation/self-update.md.
"""

from __future__ import annotations

import calendar
import re
import shutil
import subprocess
import time
import tomllib
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from warpzone import esde, safefile

if TYPE_CHECKING:
    from warpzone.profiles import Profile

# The script that ships with this version of warpzone.
UPDATER_SOURCE = Path(__file__).resolve().parent / "data" / "updater.sh"
UPDATER_DIR = "updater"
LIVE_REL = Path(".local/share/warpzone") / UPDATER_DIR / "updater.sh"
# What `provision device` writes for the script, in this order.
CONF_KEYS = (
    "device",
    "config",
    "clone",
    "python",
    "channel",
    "pin",
    "enabled",
    "frontend",
    "install",
)
# The first UPDATER_ABI whose `tick` knows `--now` (an update asked for by hand).
NOW_ABI = 2

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
    install: str = "auto",
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
        "install": install,
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


def install_mode(profile: Profile) -> str:
    """`auto` or `manual`: whether the timer installs a new release or only notes it."""
    return profile.updater_install


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
    fetch_now: bool = False,
    run: Callable[[Sequence[str]], int] = _run,
) -> int:
    """`warpzone self-update`: run one tick of the live copy of the script, as the timer does,
    and return its exit code. `fetch_now` is an update asked for by hand: a script that knows
    `--now` (UPDATER_ABI >= NOW_ABI) gets it, so the open menu does not hold it back; an older
    one gets `--fetch-now`. Without a live copy, say what to do and return 1."""
    if not live.is_file():
        out(
            f"self-update: there is no updater at {live}. `warpzone provision device <name>` "
            "installs it from a git clone that was set up with "
            "`sh <clone>/warpzone/data/updater.sh install <name>` (see https://dagonco.github.io/warpzone/operation/self-update/)"
        )
        return 1
    flags: list[str] = []
    if fetch_now:
        try:
            abi = updater_abi(live.read_text(encoding="utf-8", errors="replace"))
        except OSError:
            abi = None
        flags = ["--now" if (abi or 0) >= NOW_ABI else "--fetch-now"]
    try:
        return run(["sh", str(live), "tick", *flags])
    except OSError as exc:
        out(f"self-update: cannot run sh {live} tick ({exc})")
        return 1


# ------------------------------------------------------------------------------ the channel

CHANNELS = ("stable", "rc")
_TABLE = re.compile(r"^\s*\[\s*devices\s*\.\s*(?P<name>[A-Za-z0-9_-]+|\"[^\"]*\"|'[^']*')\s*\]")
_ANY_TABLE = re.compile(r"^\s*\[")
_CHANNEL = re.compile(r"^(?P<lead>\s*channel\s*=\s*)(?:\"[^\"]*\"|'[^']*')(?P<tail>.*)$")


def _table_name(raw: str) -> str:
    return raw[1:-1] if raw[:1] in "\"'" else raw


def set_channel_text(text: str, device: str, channel: str) -> str:
    """The config with `channel` set in the table `[devices.<device>]` (pure).

    Only that one line changes (or is added right under the table's header); comments and
    the rest stay as they are. The result is parsed again and must differ from the original
    in that one value only. Anything else (no such table, an inline table, a table given
    twice, a file that is not TOML) raises ValueError: change it by hand then."""
    if channel not in CHANNELS:
        raise ValueError(f"the channel must be stable or rc, not {channel!r}")
    try:
        before = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise ValueError(f"the config is not valid TOML ({exc})") from exc
    entry = (
        before.get("devices", {}).get(device) if isinstance(before.get("devices"), dict) else None
    )
    if not isinstance(entry, dict):
        raise ValueError(f"the config has no device {device!r}")
    if entry.get("channel", "stable") == channel:
        return text
    lines = text.splitlines(keepends=True)
    headers = [
        i
        for i, line in enumerate(lines)
        if (m := _TABLE.match(line)) and _table_name(m["name"]) == device
    ]
    if len(headers) != 1:
        raise ValueError(f"[devices.{device}] is not one table of its own in the config")
    start = headers[0] + 1
    end = next((i for i in range(start, len(lines)) if _ANY_TABLE.match(lines[i])), len(lines))
    found = [i for i in range(start, end) if _CHANNEL.match(lines[i].rstrip("\r\n"))]
    if len(found) > 1:
        raise ValueError(f"[devices.{device}] sets channel more than once")
    if found:
        i = found[0]
        body = lines[i].rstrip("\r\n")
        ending = lines[i][len(body) :]
        m = _CHANNEL.match(body)
        assert m is not None
        lines[i] = f'{m["lead"]}"{channel}"{m["tail"]}{ending}'
    else:
        ending = "\r\n" if lines[headers[0]].endswith("\r\n") else "\n"
        if not lines[headers[0]].endswith(("\n", "\r")):
            lines[headers[0]] += ending
        lines.insert(start, f'channel = "{channel}"{ending}')
    out = "".join(lines)
    expected = {**before, "devices": {**before["devices"], device: {**entry, "channel": channel}}}
    try:
        if tomllib.loads(out) != expected:
            raise ValueError("the edited config does not read back as expected")
    except tomllib.TOMLDecodeError as exc:
        raise ValueError(f"the edited config is not valid TOML ({exc})") from exc
    return out


def set_conf_channel(text: str, channel: str) -> str:
    """The updater's settings with `channel=` replaced (or added at the end) (pure)."""
    lines = text.splitlines(keepends=True)
    found = False
    for i, line in enumerate(lines):
        if line.startswith("channel="):
            lines[i] = f"channel={channel}\n"
            found = True
    if found:
        return "".join(lines)
    if lines and not lines[-1].endswith("\n"):
        lines[-1] += "\n"
    return "".join(lines) + f"channel={channel}\n"


def change_channel(
    conf: Path,
    channel: str,
    out: Callable[[str], None],
    *,
    version: str,
    now: float | None = None,
    default_config: Path | None = None,
    dry_run: bool = False,
) -> int:
    """`warpzone self-update --channel`: set the channel of this device in its config (with a
    backup next to it, `<config>.bak-<date>`) and in the updater's settings, so it holds from
    the next tick and `provision device` keeps it; `dry_run` says what it would write. Returns
    0, or 1 with a message."""
    if channel not in CHANNELS:
        out(f"self-update: the channel must be stable or rc, not {channel!r}")
        return 1
    try:
        settings = conf.read_text(encoding="utf-8")
    except OSError:
        out(
            f"self-update: there are no updater settings at {conf}; run "
            "`warpzone provision device <name>` on this device first"
        )
        return 1
    kv = parse_kv(settings)
    device = kv.get("device", "")
    config = Path(kv["config"]) if kv.get("config") else default_config
    if not device or config is None:
        out(
            f"self-update: {conf} names no device or config; run `warpzone provision device <name>`"
        )
        return 1
    try:
        stamp = safefile.stamp(config)
        text = config.read_text(encoding="utf-8")
        new = set_channel_text(text, device, channel)
    except (OSError, UnicodeDecodeError, ValueError) as exc:
        out(f"self-update: cannot change the channel in {config}: {exc}; edit it by hand")
        return 1
    if new == text and kv.get("channel") == channel:
        out(f"self-update: {device} already follows {channel}")
        return 0
    if dry_run:
        if new != text:
            out(f'self-update: dry-run: would set channel = "{channel}" for {device} in {config}')
        out(f"self-update: dry-run: would set channel={channel} in {conf}")
        return 0
    when = time.strftime("%Y%m%d-%H%M%S", time.localtime(time.time() if now is None else now))
    try:
        if new != text:
            shutil.copy2(config, config.with_name(f"{config.name}.bak-{when}"))
            if not safefile.atomic_write(config, new.encode("utf-8"), expect=stamp):
                out(f"self-update: {config} changed while it was being edited; nothing was written")
                return 1
        safefile.write_file(conf, set_conf_channel(settings, channel).encode("utf-8"))
    except OSError as exc:
        out(f"self-update: cannot write the channel ({exc})")
        return 1
    out(f"self-update: {device} now follows {channel}")
    if channel == "stable" and "rc" in version:
        out(
            f"self-update: it stays on {version} (an update never goes down) until a stable "
            "release newer than it comes out"
        )
    return 0
