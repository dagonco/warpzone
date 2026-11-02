"""`warpzone doctor`: check this machine and say how to fix what is missing.

Offline and read-only: it never installs or changes anything. The only network-like
probe is one request to the local Syncthing REST API on the loopback address.

Each check is a small function that takes a `Context` and returns `Result` records.
Everything it needs from the machine (`which`, running a tool, the environment, the
filesystem, the clock, the HTTP probe) is a field of `Probes`, so the tests run with
fakes and no real tools. `render` turns the results into text and `exit_code` into the
process status; both are pure.

Statuses and what they mean for the exit code:

    ok        [ok]  fine
    warn      [! ]  works, but worth a look (old DATs, little free space); exit code unaffected
    fail      [!!]  a problem: something this machine needs is missing or broken; exit 1
    optional  [--]  not set up; only matters for the feature named in the line; exit 0
"""

from __future__ import annotations

import os
import platform as platform_mod
import re
import shutil
import ssl
import subprocess
import sys
import textwrap
import time
import tomllib
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from collections.abc import Callable, Collection, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from warpzone import (
    __version__,
    artwork,
    esde,
    frontend,
    localst,
    model,
    ra,
    settings_state,
    updatetimer,
)
from warpzone import inbox_switch as switch
from warpzone.config import (
    INBOX_KEYS,
    ConfigError,
    InboxSettings,
    parse_config,
    parse_inbox,
)
from warpzone.dat import SYSTEMS, DatLibrary
from warpzone.flatpak import (
    SYNCTHINGY_APP,
    SYNCTHINGY_EXEC,
    SYNCTHINGY_STOP,
    app_dir,
    app_ids,
    is_syncthingy_config,
    missing_apps,
    override_command,
    show_command,
)
from warpzone.model import ModelError
from warpzone.profiles import Profile, ProfileError, available_profiles, load_profile
from warpzone.settings_layers import LayerError, conflict_copies, parse_layer, validate
from warpzone.syncthing import _is_loopback

OK, WARN, FAIL, OPTIONAL = "ok", "warn", "fail", "optional"
MARKS = {OK: "[ok]", WARN: "[! ]", FAIL: "[!!]", OPTIONAL: "[--]"}

MIN_PYTHON = (3, 11)
DAT_MAX_AGE_DAYS = 180
MIN_FREE_GIB = 20
PROBE_TIMEOUT_SECONDS = 2.0
RUN_TIMEOUT_SECONDS = 10.0
GIB = 1024**3
DAY_SECONDS = 86400
FALLBACK_WIDTH = 100  # when the terminal width is unknown (a pipe, a log)
MIN_TEXT_WIDTH = 30  # never wrap narrower than this, however wide the labels are
FIX_PREFIX = "fix: "

G_BASE = "warpzone and Python"
G_CONFIG = "Config"
G_SYNCTHING = "Syncthing"
G_SAVES = "Save sync"
G_TOOLS = "Inbox tools"
G_SWITCH = "Switch"
G_DATS = "DATs"
G_HUB = "Hub folder"
G_SETTINGS = "Emulator settings"
G_FRONTEND = "Frontend"
G_RA = "RetroAchievements"
G_UPDATE = "Updates"
G_INBOX = "Inbox"
# The groups whose items only matter to the desktop that runs `warpzone inbox`.
INBOX_GROUPS = (G_TOOLS, G_SWITCH, G_DATS, G_HUB, G_RA)

# ------------------------------------------------------------------ install hints
#
# The one place that says how to install things, per platform family. A family is what
# `platform_family` returns: macos, debian (also Ubuntu and derivatives), fedora (also
# RHEL-likes), arch, steamos (Arch, but with a read-only root), or linux (anything else).
# `hint_for` falls back from the exact family to "linux" and then to "any". Only add a
# command here when it is known to be right for that family.

STEAMOS_ROOT = (
    "SteamOS has a read-only root. `warpzone inbox` is meant for a desktop that mounts the "
    "hub's share, so install this there. To run it on SteamOS anyway, use a container "
    "(distrobox with a Debian image) and the Debian command inside it"
)

REPO_URL_LIBRETRO = "https://github.com/libretro/libretro-database"
DAT_CLONE_HINT = (
    f"git clone --depth 1 --filter=blob:none --sparse {REPO_URL_LIBRETRO} && "
    "git -C libretro-database sparse-checkout set metadat/redump metadat/no-intro; "
    "then set dats_dir in [inbox] to the metadat folder of that clone"
)
RAHASHER_BUILD = (
    "build it from https://github.com/RetroAchievements/RALibretro with CHD support: "
    "`git clone --recursive https://github.com/RetroAchievements/RALibretro && "
    "cd RALibretro && make -f Makefile.RAHasher HAVE_CHD=1`; "
    "then set rahasher in [inbox] to the RAHasher binary it builds"
)

HintTable = Mapping[str, str]

CHDMAN_HINTS: HintTable = {
    "macos": "brew install rom-tools",
    "debian": "sudo apt install mame-tools",
    "fedora": "sudo dnf install mame-tools",
    "arch": "sudo pacman -S mame-tools",
    "steamos": f"{STEAMOS_ROOT}: sudo apt install mame-tools",
    "any": "install MAME's tools (the package that ships `chdman`) with your package manager",
}
BSDTAR_HINTS: HintTable = {
    "macos": "bsdtar ships with macOS (/usr/bin/bsdtar); check that it is not hidden by PATH",
    "debian": "sudo apt install libarchive-tools",
    "fedora": "sudo dnf install bsdtar",
    "arch": "sudo pacman -S libarchive",
    "steamos": f"{STEAMOS_ROOT}: sudo apt install libarchive-tools",
    "any": "install libarchive's `bsdtar` with your package manager",
}
RAHASHER_HINTS: HintTable = {"any": RAHASHER_BUILD}
DOLPHIN_HINTS: HintTable = {
    "macos": "install Dolphin (brew install --cask dolphin); dolphin-tool is inside the app: "
    "set dolphin_tool in [inbox] to /Applications/Dolphin.app/Contents/MacOS/dolphin-tool",
    "linux": "dolphin-tool ships with Dolphin; install Dolphin, or with the Flatpak use "
    "`flatpak run --command=dolphin-tool org.DolphinEmu.dolphin-emu`",
    "any": "dolphin-tool ships with Dolphin; install Dolphin",
}
NSZ_HINTS: HintTable = {"any": "uv tool install nsz (or: pipx install nsz)"}
ORBIS_PKG_UTIL_HINTS: HintTable = {
    "any": "cargo install orbis-pkg-util (the binary lands in ~/.cargo/bin; set orbis_pkg_util "
    "in [inbox] to it if that folder is not on PATH)"
}
TITLEDB_REPO = "https://github.com/blawar/titledb"
TITLEDB_HINT = (
    f"git clone --depth 1 {TITLEDB_REPO}, then set titledb_dir in [inbox] to that clone "
    "(update it with git pull); without it Switch games cannot be named"
)
KEYS_HINT = (
    "put your console's prod.keys at ~/.switch/prod.keys, or set switch_keys in [inbox] to "
    "its path; warpzone never reads the file, it only hands the path to nsz"
)
SYNCTHING_HINTS: HintTable = {
    "macos": "brew install syncthing && brew services start syncthing",
    "debian": "sudo apt install syncthing && systemctl --user enable --now syncthing",
    "fedora": "sudo dnf install syncthing && systemctl --user enable --now syncthing",
    "arch": "sudo pacman -S syncthing && systemctl --user enable --now syncthing",
    "steamos": f"flatpak install flathub {SYNCTHINGY_APP} "
    "(the read-only root rules out system packages)",
    "any": "install Syncthing from https://syncthing.net/downloads/ and start it",
}
SYNCTHING_START_HINTS: HintTable = {
    "macos": "brew services start syncthing (or open the Syncthing app)",
    "steamos": "start SyncThingy (or `systemctl --user start syncthing` if you use the service)",
    "linux": "systemctl --user enable --now syncthing",
    "any": "start Syncthing",
}

# RetroArch's Flycast core options. `reicast_per_content_vmus` is "disabled" (default),
# "VMU A1" or "All VMUs" (libretro_core_options.h in github.com/flyinghead/flycast).
FLYCAST_OPT = ".var/app/org.libretro.RetroArch/config/retroarch/config/Flycast/Flycast.opt"
FLYCAST_VMU_KEY = "reicast_per_content_vmus"
FLYCAST_VMU_FIX = (
    "in RetroArch, with a Dreamcast game running, open Quick Menu > Core Options > "
    'Per-Game Visual Memory Units/Systems (VMU) ("Per-Game VMUs") and choose "VMU A1" '
    'or "All VMUs". Existing shared VMUs are not migrated: each game starts with an '
    "empty card in RetroArch's saves folder, which retroarch-saves syncs"
)

# Vita3K's active user. The profile syncs the saves of user 00 only
# (ux0/user/00/savedata); a user made in the GUI can get another id.
VITA3K_CONFIG = ".config/Vita3K/config.yml"
VITA3K_USER = "00"

SYSTEMD_USER_DIR = ".config/systemd/user"
AUTOSTART_DIR = ".config/autostart"
SYNCTHING_UNIT_PATH = f"~/{SYSTEMD_USER_DIR}/syncthing.service"
# Indented lines are not wrapped by `render`, so the unit can be copied as it is.
SYNCTHING_UNIT = (
    "  [Unit]\n  Description=Syncthing\n"
    f"  [Service]\n  ExecStart={SYNCTHINGY_EXEC}\n  ExecStop={SYNCTHINGY_STOP}\n"
    "  Restart=on-failure\n"
    "  [Install]\n  WantedBy=default.target"
)


@dataclass(frozen=True)
class ToolSpec:
    """One external tool. Data, so adding a tool is one entry in `TOOLS`."""

    key: str  # name used in the report and, if `setting`, the [inbox] key
    commands: tuple[str, ...]  # names to look up on PATH
    purpose: str
    hints: HintTable
    setting: str | None = None  # [inbox] key that can point at the binary
    # Run with these arguments to read a version line; None: do not run it, show the path.
    version_args: tuple[str, ...] | None = ()
    # A fail (not just optional) when missing and an [inbox] table exists.
    required_for_inbox: bool = False
    # `inbox` itself looks the tool up on PATH only when it is configured (RAHasher).
    inbox_uses_path: bool = True
    # Where an installer puts it without adding it to PATH.
    known_paths: tuple[str, ...] = ()
    note: str = ""


TOOLS: tuple[ToolSpec, ...] = (
    ToolSpec(
        "chdman",
        ("chdman",),
        "converts discs to CHD (PlayStation, PlayStation 2, Saturn, Dreamcast)",
        CHDMAN_HINTS,
        setting="chdman",
        required_for_inbox=True,
    ),
    ToolSpec(
        "bsdtar",
        ("bsdtar",),
        "unpacks .7z and .rar",
        BSDTAR_HINTS,
        setting="bsdtar",
        version_args=("--version",),
        required_for_inbox=True,
    ),
    ToolSpec(
        "RAHasher",
        ("RAHasher", "rahasher"),
        "RetroAchievements hash check (optional)",
        RAHASHER_HINTS,
        setting="rahasher",
        inbox_uses_path=False,
    ),
    ToolSpec(
        "dolphin-tool",
        ("dolphin-tool",),
        "only for compressed GameCube images (.rvz)",
        DOLPHIN_HINTS,
        setting="dolphin_tool",
        version_args=None,
        known_paths=("/Applications/Dolphin.app/Contents/MacOS/dolphin-tool",),
    ),
    ToolSpec(
        "nsz",
        ("nsz",),
        "Switch: reads .nsp/.nsz metadata and decompresses .nsz in inbox",
        NSZ_HINTS,
        setting="nsz",
        version_args=None,
    ),
    ToolSpec(
        "orbis-pkg-util",
        ("orbis-pkg-util",),
        "PS4: unpacks .pkg games into folders shadPS4 can run",
        ORBIS_PKG_UTIL_HINTS,
        setting="orbis_pkg_util",
        version_args=None,
        known_paths=("~/.cargo/bin/orbis-pkg-util",),
    ),
)

# What each config table enables, for the "[hub] / [devices] / [inbox]" lines.
SECTIONS: tuple[tuple[str, str, str], ...] = (
    ("hub", "[hub]", "provision hub, check, status (reaching the hub's Syncthing)"),
    ("devices", "[devices]", "provision device, prune, render-ignores (one table per console)"),
    ("inbox", "[inbox]", "warpzone inbox (this machine mounts the hub and files new games)"),
)


def platform_family(system: str, os_release: Mapping[str, str]) -> str:
    """macos, steamos, debian, fedora, arch, linux (unknown Linux) or other."""
    if system == "Darwin":
        return "macos"
    if system != "Linux":
        return "other"
    ids = {os_release.get("ID", "").lower(), *os_release.get("ID_LIKE", "").lower().split()}
    if "steamos" in ids:
        return "steamos"
    for family, names in (
        ("debian", {"debian", "ubuntu"}),
        ("fedora", {"fedora", "rhel", "centos"}),
        ("arch", {"arch"}),
    ):
        if ids & names:
            return family
    return "linux"


_EXEC_START = re.compile(r"^\s*Exec(?:Start)?\s*=(.*)$", re.MULTILINE)


def _runs_syncthing(text: str) -> bool:
    return any("syncthing" in line.lower() for line in _EXEC_START.findall(text))


def judge_game_mode(
    units: Mapping[str, str], enabled: Collection[str], autostart: Mapping[str, str]
) -> tuple[str, str, str]:
    """(status, detail, fix) for "will Syncthing run in Game Mode?". Game Mode starts no
    desktop session, so only a systemd user unit that is enabled keeps Syncthing running
    there. `units` maps the .service files of ~/.config/systemd/user to their text,
    `enabled` holds the names linked in its default.target.wants, and `autostart` maps
    the .desktop files of ~/.config/autostart to their text."""
    running = sorted(name for name, text in units.items() if _runs_syncthing(text))
    on = [name for name in running if name in enabled]
    autostarted = sorted(name for name, text in autostart.items() if _runs_syncthing(text))
    if on:
        if autostarted:
            return (
                WARN,
                f"{on[0]} starts Syncthing in Game Mode, but the desktop autostart entry "
                f"{autostarted[0]} starts it too, so two instances compete for the same port",
                f"remove ~/{AUTOSTART_DIR}/{autostarted[0]} so only the unit starts Syncthing",
            )
        return OK, f"{on[0]} is enabled, so Syncthing also runs in Game Mode", ""
    unit = running[0] if running else "syncthing.service"
    install = (
        f"systemctl --user enable --now {unit}"
        if running
        else (
            f"create ~/{SYSTEMD_USER_DIR}/syncthing.service with this content (for the "
            f"SyncThingy flatpak):\n{SYNCTHING_UNIT}\n"
            "then run:\n  systemctl --user daemon-reload\n"
            "  systemctl --user enable --now syncthing.service"
        )
    )
    if autostarted:
        why = (
            f"Syncthing is only started by the desktop autostart entry {autostarted[0]}, "
            "which Game Mode never runs, so syncing stops whenever the console is in Game Mode"
        )
        install += (
            f"\nand remove ~/{AUTOSTART_DIR}/{autostarted[0]} so two instances do not "
            "compete for the same port"
        )
    elif running:
        why = f"{running[0]} runs Syncthing but is not enabled, so it does not start on boot"
    else:
        why = (
            "no systemd user unit starts Syncthing, so it does not run in Game Mode "
            "(or after a reboot, until someone starts it by hand)"
        )
    return WARN, why, install


def _core_option(text: str, key: str) -> str | None:
    """The value of `key` in a RetroArch core options file (`key = "value"` lines)."""
    found = None
    for line in text.splitlines():
        name, sep, value = line.partition("=")
        if sep and name.strip() == key:
            found = value.strip().strip('"')
    return found


def judge_flycast_vmus(text: str) -> tuple[str, str, str]:
    """(status, detail, fix) for the Flycast core's per-game VMU option, given the text of
    Flycast.opt. Without per-game VMUs the core writes `vmu_save_*.bin` into the system
    folder (bios/dc), which the library ignores: Dreamcast saves would never sync."""
    value = _core_option(text, FLYCAST_VMU_KEY)
    if value in ("VMU A1", "All VMUs"):
        return OK, f'Flycast keeps per-game VMUs in the saves folder ("{value}")', ""
    shown = f'"{value}"' if value is not None else "unset, which means disabled"
    return (
        WARN,
        f"Flycast's per-game VMUs are off ({shown}), so the VMUs live in the system "
        "folder (bios/dc), which warpzone does not sync and a revert would not restore",
        FLYCAST_VMU_FIX,
    )


def vita3k_user_id(text: str) -> str | None:
    """The `user-id:` of Vita3K's config.yml, without quotes or a trailing comment."""
    match = re.search(r"^user-id:[ \t]*(.*)$", text, re.MULTILINE)
    if not match:
        return None
    value = match.group(1).split(" #")[0].strip().strip("\"'")
    return value or None


def judge_vita3k_user(user: str) -> tuple[str, str, str]:
    """(status, detail, fix) for Vita3K's active user id."""
    if user == VITA3K_USER:
        return OK, f"the active user is {user}, the one warpzone syncs", ""
    return (
        WARN,
        f"the active user is {user}, but warpzone syncs the saves of user {VITA3K_USER} "
        f"(ux0/user/{VITA3K_USER}/savedata), so this user's saves are not synced",
        f"in Vita3K, switch to user {VITA3K_USER} (or delete the other user and keep "
        f"{VITA3K_USER}); if the saves are in ux0/user/{user}/savedata, move them to "
        f"ux0/user/{VITA3K_USER}/savedata. Vita3K has to be closed while you do it",
    )


def parse_show(output: str) -> dict[str, str]:
    """`systemctl show` output (`Key=value` lines) as a dict."""
    values: dict[str, str] = {}
    for line in output.splitlines():
        key, sep, value = line.partition("=")
        if sep:
            values[key.strip()] = value.strip()
    return values


def judge_unit_state(
    output: str, *, flatpak: bool, stops_flatpak: bool
) -> tuple[str, str, str] | None:
    """(status, detail, fix) for the Syncthing user unit, from `systemctl --user show -p
    ActiveState,SubState`. None when there is nothing to say (no such unit, or stopped).
    A unit stuck in `activating (auto-restart)` is a start that keeps failing; with the
    flatpak the usual cause is the old process still holding the port, because `flatpak
    run` runs it in a scope of its own and `systemctl --user stop` does not reach it.
    `stops_flatpak`: the unit already has an `ExecStop` that kills the flatpak."""
    values = parse_show(output)
    state, sub = values.get("ActiveState", ""), values.get("SubState", "")
    if state == "active":
        return OK, f"syncthing.service is {state} ({sub})", ""
    if state != "activating" and sub != "auto-restart":
        return None
    detail = (
        f"syncthing.service is {state} ({sub}): it keeps starting and failing, usually "
        "because another Syncthing already holds the port"
    )
    if not flatpak:
        return (
            WARN,
            detail,
            "find the other Syncthing and stop it (`journalctl --user -u syncthing -n 20` "
            "shows the error), then run: systemctl --user restart syncthing",
        )
    restart = "systemctl --user daemon-reload\n  systemctl --user restart syncthing"
    kill = f"flatpak kill {SYNCTHINGY_APP}"
    if stops_flatpak:
        return (
            WARN,
            detail,
            f"the old process is still running: stop it with `{kill}`, then run:\n  {restart}",
        )
    return (
        WARN,
        detail + ". `flatpak run` starts Syncthing in a scope of its own, so `systemctl --user "
        "stop` leaves the old process running",
        f"stop the old process with `{kill}`. Then make {SYNCTHING_UNIT_PATH} stop the "
        f"flatpak too (it needs the ExecStop line):\n{SYNCTHING_UNIT}\nand run:\n  {restart}",
    )


def hint_for(table: HintTable, family: str) -> str:
    """The hint for `family`; a specific Linux family falls back to "linux", then "any"."""
    if family in table:
        return table[family]
    if family not in ("macos", "other") and "linux" in table:
        return table["linux"]
    return table["any"]


# ------------------------------------------------------------------ probes and results


@dataclass(frozen=True)
class RunOutput:
    returncode: int
    output: str  # stdout and stderr together


def _run(command: Sequence[str]) -> RunOutput:
    proc = subprocess.run(
        list(command),
        capture_output=True,
        text=True,
        errors="replace",
        timeout=RUN_TIMEOUT_SECONDS,
        stdin=subprocess.DEVNULL,
        check=False,
    )
    return RunOutput(proc.returncode, (proc.stdout or "") + (proc.stderr or ""))


def _http_status(url: str, headers: Mapping[str, str], timeout: float) -> int:
    """The HTTP status of a GET; raises OSError when nothing answers. Used for loopback
    only, where Syncthing's own certificate is self-signed."""
    context = None
    if url.startswith("https://"):
        context = ssl.create_default_context()
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
    request = urllib.request.Request(url, headers=dict(headers))
    try:
        with urllib.request.urlopen(request, timeout=timeout, context=context) as response:
            return int(response.status)
    except urllib.error.HTTPError as exc:
        return exc.code


def _read_os_release() -> dict[str, str]:
    values: dict[str, str] = {}
    for name in ("/etc/os-release", "/usr/lib/os-release"):
        try:
            text = Path(name).read_text(encoding="utf-8")
        except OSError:
            continue
        for line in text.splitlines():
            key, sep, value = line.partition("=")
            if sep and key and not key.startswith("#"):
                values[key] = value.strip().strip('"')
        break
    return values


def _is_executable(path: str) -> bool:
    return os.path.isfile(path) and os.access(path, os.X_OK)


def _python_version() -> tuple[int, int, int]:
    v = sys.version_info
    return v.major, v.minor, v.micro


def _mtime(path: Path) -> float:
    return path.stat().st_mtime


def _list_dir(path: Path) -> list[str]:
    """Names inside `path` (broken symlinks included); empty when it cannot be listed."""
    try:
        return sorted(os.listdir(path))
    except OSError:
        return []


def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def _list_tree(path: Path) -> list[str]:
    """Every file under `path`, relative and with `/`; Syncthing's own folders are skipped."""
    found: list[str] = []
    for base, dirs, files in os.walk(path):
        dirs[:] = [d for d in dirs if d not in (".stfolder", ".stversions")]
        found += [(Path(base) / name).relative_to(path).as_posix() for name in files]
    return sorted(found)


def _disk_free(path: Path) -> tuple[int, int]:
    """(total, free) bytes of the volume holding `path`."""
    usage = shutil.disk_usage(path)
    return usage.total, usage.free


@dataclass
class Probes:
    """Everything a check reads from the machine. The defaults are the real thing."""

    config_path: Path
    which: Callable[[str], str | None] = shutil.which
    run: Callable[[Sequence[str]], RunOutput] = _run
    http_status: Callable[[str, Mapping[str, str], float], int] = _http_status
    env: Mapping[str, str] = field(default_factory=lambda: os.environ)
    home: Path = field(default_factory=Path.home)
    system: str = field(default_factory=platform_mod.system)
    machine: str = field(default_factory=platform_mod.machine)
    os_release: Mapping[str, str] = field(default_factory=_read_os_release)
    python: tuple[int, int, int] = field(default_factory=_python_version)
    now: Callable[[], float] = time.time
    is_dir: Callable[[Path], bool] = Path.is_dir
    is_file: Callable[[Path], bool] = Path.is_file
    is_executable: Callable[[str], bool] = _is_executable
    mtime: Callable[[Path], float] = _mtime
    list_dir: Callable[[Path], list[str]] = _list_dir
    read_text: Callable[[Path], str] = _read_text
    list_tree: Callable[[Path], list[str]] = _list_tree
    disk_free: Callable[[Path], tuple[int, int]] = _disk_free

    @property
    def family(self) -> str:
        return platform_family(self.system, self.os_release)


@dataclass(frozen=True)
class Result:
    name: str
    group: str
    status: str
    detail: str
    fix: str = ""


@dataclass(frozen=True)
class SettingsDevice:
    """A configured device with its profile (see `ConfigState`)."""

    name: str
    device_id: str
    profile: Profile
    folders: tuple[str, ...] = ()


@dataclass(frozen=True)
class ConfigState:
    """What could be read from the config file. `settings` is salvaged even when other
    parts of the file are invalid, so the checks that depend on it still make sense."""

    path: Path
    exists: bool
    error: str | None = None
    sections: tuple[str, ...] = ()  # of hub, devices, inbox
    devices: tuple[str, ...] = ()
    settings: InboxSettings = field(default_factory=InboxSettings)
    hub_root: str | None = None  # [hub] root as written, when it is a string
    # Save and BIOS folder paths (with `~`) of the configured devices; empty when the
    # config does not load, then the checks fall back to every shipped profile.
    sync_paths: tuple[str, ...] = ()
    # The devices that subscribe to `settings`; empty when the config does not load.
    settings_devices: tuple[SettingsDevice, ...] = ()
    # The devices that subscribe to `library` whose profile has `es_systems` or `retroarch`,
    # or `artwork` with the `frontend` group.
    frontend_devices: tuple[SettingsDevice, ...] = ()
    # The devices that subscribe to `library` and have not turned `self_update` off.
    update_devices: tuple[SettingsDevice, ...] = ()


@dataclass(frozen=True)
class Context:
    probes: Probes
    config: ConfigState

    @property
    def inbox_configured(self) -> bool:
        return "inbox" in self.config.sections


# ------------------------------------------------------------------ checks


def _version_str(v: tuple[int, int, int]) -> str:
    return ".".join(str(n) for n in v)


def check_base(ctx: Context) -> list[Result]:
    p = ctx.probes
    results = [Result("warpzone", G_BASE, OK, __version__)]
    if tuple(p.python[:2]) >= MIN_PYTHON:
        results.append(Result("Python", G_BASE, OK, _version_str(p.python)))
    else:
        need = ".".join(str(n) for n in MIN_PYTHON)
        results.append(
            Result(
                "Python",
                G_BASE,
                FAIL,
                f"{_version_str(p.python)}, warpzone needs {need} or newer",
                f"install Python {need}+ and run warpzone with it "
                "(macOS: brew install python; Debian/Ubuntu: sudo apt install python3)",
            )
        )
    family = p.family
    if family == "macos":
        label = "macOS"
    elif p.system == "Linux":
        label = p.os_release.get("PRETTY_NAME") or "Linux"
    else:
        label = p.system or "unknown system"
    detail = f"{label}, {p.machine or 'unknown arch'}"
    if family == "other":
        results.append(
            Result(
                "Platform",
                G_BASE,
                WARN,
                f"{detail}; warpzone is developed for macOS and Linux",
                "use a macOS or Linux machine, or expect rough edges",
            )
        )
    else:
        results.append(Result("Platform", G_BASE, OK, detail))
    return results


def load_config_state(path: Path) -> ConfigState:
    """Read the config as far as it can be read; never raises."""
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return ConfigState(path, exists=False)
    except OSError as exc:
        return ConfigState(path, exists=True, error=f"{path}: {exc.strerror or exc}")
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        return ConfigState(path, exists=True, error=f"{path}: {exc}")

    sections = tuple(s for s in ("hub", "devices", "inbox") if s in data)
    raw_devices = data.get("devices")
    devices = tuple(sorted(raw_devices)) if isinstance(raw_devices, dict) else ()
    settings = parse_inbox(data.get("inbox"), [])  # keep whatever is valid
    errors: list[str] = []
    sync_paths: dict[str, None] = {}
    settings_devices: list[SettingsDevice] = []
    frontend_devices: list[SettingsDevice] = []
    update_devices: list[SettingsDevice] = []
    try:
        if "hub" in data or "devices" in data:
            cfg = parse_config(data, set(available_profiles()))
            profiles = {
                name: load_profile(name) for name in {d.profile for d in cfg.devices.values()}
            }
            for device in cfg.devices.values():
                for spec in model.device_specs(cfg, device.name, profiles[device.profile]):
                    sync_paths[spec.path] = None
                if "settings" in device.folders:
                    entry = SettingsDevice(device.name, device.device_id, profiles[device.profile])
                    settings_devices.append(entry)
                profile = profiles[device.profile]
                art = profile.artwork is not None and "frontend" in device.folders
                if "library" in device.folders and (profile.es_systems or profile.retroarch or art):
                    frontend_devices.append(
                        SettingsDevice(device.name, device.device_id, profile, device.folders)
                    )
                if "library" in device.folders and device.self_update:
                    update_devices.append(
                        SettingsDevice(device.name, device.device_id, profile, device.folders)
                    )
        else:
            unknown = sorted(set(data) - {"hub", "devices", "inbox"})
            errors += [f"config: unknown key {key!r}" for key in unknown]
            parse_inbox(data.get("inbox"), errors)
    except (ConfigError, ProfileError, ModelError) as exc:
        errors.append(str(exc))
    error = f"{path}:\n" + "\n".join(errors) if errors else None
    raw_hub = data.get("hub")
    hub_root = raw_hub.get("root") if isinstance(raw_hub, dict) else None
    if not isinstance(hub_root, str) or not hub_root:
        hub_root = None
    paths = tuple(sync_paths) if error is None else ()
    subscribed = tuple(settings_devices) if error is None else ()
    frontends = tuple(frontend_devices) if error is None else ()
    updating = tuple(update_devices) if error is None else ()
    return ConfigState(
        path,
        True,
        error,
        sections,
        devices,
        settings,
        hub_root,
        paths,
        subscribed,
        frontends,
        updating,
    )


def check_config(ctx: Context) -> list[Result]:
    cfg = ctx.config
    if not cfg.exists:
        return [
            Result(
                "Config file",
                G_CONFIG,
                OPTIONAL,
                f"not found at {cfg.path}; bios works without it, and inbox with --root and "
                "--dats, but provision, prune, check and status need one",
                "copy examples/config.example.toml from the warpzone repository to "
                f"{cfg.path} and fill in what you use (every table is optional on its own)",
            )
        ]
    if cfg.error:
        return [
            Result(
                "Config file",
                G_CONFIG,
                FAIL,
                cfg.error,
                f"edit {cfg.path} and fix the lines above (examples/config.example.toml "
                "shows every key)",
            )
        ]
    results = [Result("Config file", G_CONFIG, OK, f"{cfg.path} is valid")]
    only_inbox = cfg.sections == ("inbox",)
    for key, label, enables in SECTIONS:
        if key in cfg.sections:
            extra = f" ({', '.join(cfg.devices)})" if key == "devices" and cfg.devices else ""
            results.append(Result(label, G_CONFIG, OK, f"present{extra}; enables {enables}"))
        elif not only_inbox:
            results.append(
                Result(
                    label,
                    G_CONFIG,
                    OPTIONAL,
                    f"absent; would enable {enables}",
                    f"add a {label} table if this machine needs those commands "
                    "(see examples/config.example.toml)",
                )
            )
    if only_inbox:
        # A machine that only files games has no use for the hub and device tables, so
        # they are one note rather than two "not set up" items.
        results.append(
            Result(
                "[hub], [devices]",
                G_CONFIG,
                OK,
                "not used here; they are for the machine that runs provision, prune, check "
                "and status",
            )
        )
    return results


def check_syncthing(ctx: Context) -> list[Result]:
    p = ctx.probes
    name = "Local Syncthing"
    platform_name = "darwin" if p.system == "Darwin" else p.system.lower() or "linux"
    try:
        path = localst.find_config(env=p.env, home=p.home, platform=platform_name)
    except localst.LocalSyncthingError:
        return [
            Result(
                name,
                G_SYNCTHING,
                OPTIONAL,
                "no config.xml found (looked in the default places, SyncThingy flatpak "
                "included); only needed on a console or PC that receives the library",
                hint_for(SYNCTHING_HINTS, p.family),
            )
        ]
    try:
        local = localst.parse_config(path)
    except localst.LocalSyncthingError as exc:
        return [
            Result(
                name,
                G_SYNCTHING,
                FAIL,
                f"found {path} but cannot use it: {exc}",
                "start Syncthing once so it writes a complete config, or pass "
                "--syncthing-config PATH to the command that needs it",
            )
        ]
    host = local.base_url.split("://", 1)[1].rpartition(":")[0].strip("[]")
    if not _is_loopback(host):
        return [
            Result(
                name,
                G_SYNCTHING,
                WARN,
                f"{path}; GUI listens on {local.base_url}, not on the loopback address, "
                "so the API was not probed",
            )
        ]
    start = hint_for(SYNCTHING_START_HINTS, p.family)
    try:
        status = p.http_status(
            f"{local.base_url}/rest/system/ping",
            {"X-API-Key": local.api_key},
            PROBE_TIMEOUT_SECONDS,
        )
    except (OSError, ValueError) as exc:
        reason = getattr(exc, "reason", None) or exc
        return [
            Result(
                name,
                G_SYNCTHING,
                FAIL,
                f"config at {path}, but the REST API at {local.base_url} does not answer "
                f"({type(exc).__name__}: {_scrub(str(reason), local.api_key)})",
                f"Syncthing is probably not running: {start}",
            )
        ]
    if status == 200:
        return [
            Result(name, G_SYNCTHING, OK, f"REST API answers at {local.base_url} (config {path})")
        ]
    return [
        Result(
            name,
            G_SYNCTHING,
            FAIL,
            f"REST API at {local.base_url} answered HTTP {status} (config {path})",
            "the API key in config.xml was refused or something else owns that port; "
            "restart Syncthing and check its GUI address",
        )
    ]


def check_game_mode(ctx: Context) -> list[Result]:
    """SteamOS only, and only when Syncthing is installed: does it run in Game Mode?"""
    p = ctx.probes
    if p.family != "steamos":
        return []
    try:
        localst.find_config(env=p.env, home=p.home, platform="linux")
    except localst.LocalSyncthingError:
        return []
    unit_dir = p.home / SYSTEMD_USER_DIR
    units = {
        name: p.read_text(unit_dir / name)
        for name in p.list_dir(unit_dir)
        if name.endswith(".service")
    }
    enabled = set(p.list_dir(unit_dir / "default.target.wants"))
    autostart_dir = p.home / AUTOSTART_DIR
    autostart = {
        name: p.read_text(autostart_dir / name)
        for name in p.list_dir(autostart_dir)
        if name.endswith(".desktop")
    }
    status, detail, fix = judge_game_mode(units, enabled, autostart)
    return [Result("Game Mode", G_SYNCTHING, status, detail, fix)]


def _expand(path: str, home: Path) -> str:
    if path == "~":
        return str(home)
    return str(home / path[2:]) if path.startswith("~/") else path


def _shipped_sync_paths() -> tuple[str, ...]:
    """Save and BIOS folders of every shipped profile (with `~`)."""
    paths: list[str] = []
    for name in available_profiles():
        profile = load_profile(name)
        paths += [s.path for s in profile.saves.values()]
        paths += [b.path for b in profile.bios.values()]
    return tuple(paths)


def check_flatpak_access(ctx: Context) -> list[Result]:
    """Syncthing as the SyncThingy flatpak cannot see `~/.var/app/<app>` of other apps,
    so a save folder there looks empty to it until the flatpak is granted the folder.
    Only the apps that are installed (their folder exists) are checked."""
    p = ctx.probes
    name = "Flatpak access"
    if p.system != "Linux":
        return []
    try:
        config = localst.find_config(env=p.env, home=p.home, platform="linux")
    except localst.LocalSyncthingError:
        return []
    if not is_syncthingy_config(config, p.home):
        return []  # a native Syncthing sees whatever its user can
    paths = ctx.config.sync_paths or _shipped_sync_paths()
    apps = [
        app
        for app in app_ids((_expand(path, p.home) for path in paths), p.home)
        if p.is_dir(p.home / ".var" / "app" / app)
    ]
    if not apps:
        return []
    grants = " ".join(f"--filesystem={app_dir(app)}" for app in apps)
    try:
        out = p.run(show_command())
    except (OSError, subprocess.SubprocessError) as exc:
        return [
            Result(
                name,
                G_SYNCTHING,
                WARN,
                f"could not read the flatpak overrides ({type(exc).__name__}), so it is not "
                "known whether Syncthing can see the emulators' save folders",
                f"flatpak override --user {grants} {SYNCTHINGY_APP}",
            )
        ]
    missing = missing_apps(apps, out.output if out.returncode == 0 else "", p.home)
    if not missing:
        return [
            Result(
                name,
                G_SYNCTHING,
                OK,
                f"Syncthing is granted the data folders of {len(apps)} emulator(s) under "
                "~/.var/app",
            )
        ]
    commands = "\n".join(f"  {' '.join(override_command([app]))}" for app in missing)
    return [
        Result(
            name,
            G_SYNCTHING,
            FAIL,
            f"the Syncthing flatpak cannot see {', '.join(app_dir(a) for a in missing)}: "
            "flatpak hides other apps' data folders, so those save folders look empty and "
            "sync nothing",
            f"run:\n{commands}\nthen restart Syncthing "
            "(`systemctl --user restart syncthing`, or quit and reopen SyncThingy). "
            "`warpzone provision device` does this for you",
        )
    ]


def check_syncthing_unit(ctx: Context) -> list[Result]:
    """Linux with a local Syncthing: is the `syncthing` user unit restart-looping?"""
    p = ctx.probes
    if p.system != "Linux":
        return []
    try:
        config = localst.find_config(env=p.env, home=p.home, platform="linux")
    except localst.LocalSyncthingError:
        return []
    try:
        out = p.run(("systemctl", "--user", "show", "-p", "ActiveState,SubState", "syncthing"))
    except (OSError, subprocess.SubprocessError):
        return []
    if out.returncode != 0:
        return []
    unit = p.read_text(p.home / SYSTEMD_USER_DIR / "syncthing.service")
    stops = f"flatpak kill {SYNCTHINGY_APP}" in "\n".join(
        line for line in unit.splitlines() if line.strip().startswith("ExecStop")
    )
    verdict = judge_unit_state(
        out.output, flatpak=is_syncthingy_config(config, p.home), stops_flatpak=stops
    )
    if verdict is None:
        return []
    status, detail, fix = verdict
    return [Result("Syncthing unit", G_SYNCTHING, status, detail, fix)]


def check_flycast(ctx: Context) -> list[Result]:
    """Only when RetroArch's Flycast core has run (it writes its options file then)."""
    p = ctx.probes
    path = p.home / FLYCAST_OPT
    if not p.is_file(path):
        return []
    status, detail, fix = judge_flycast_vmus(p.read_text(path))
    return [Result("Flycast VMUs", G_SAVES, status, detail, fix)]


def check_vita3k(ctx: Context) -> list[Result]:
    """Only when Vita3K has written its config."""
    p = ctx.probes
    path = p.home / VITA3K_CONFIG
    if not p.is_file(path):
        return []
    user = vita3k_user_id(p.read_text(path))
    if user is None:  # no user chosen yet: nothing to compare
        return []
    status, detail, fix = judge_vita3k_user(user)
    return [Result("Vita3K user", G_SAVES, status, detail, fix)]


# Fields of `last-apply.json` that `judge_last_apply` reads: `time` (ISO 8601 text), `exit`
# (or `exit_code`), `written` and `held` (counts). Anything missing or odd is ignored.
def _count(info: Mapping[str, object], *names: str) -> int | None:
    for name in names:
        value = info.get(name)
        if isinstance(value, int) and not isinstance(value, bool):
            return value
    return None


def judge_settings_layers(
    profile: Profile,
    *,
    device: str,
    folder: str,
    folder_exists: bool,
    marker_exists: bool,
    common: str | None,
    own: str | None,
    names: Sequence[str],
) -> tuple[str, str, str]:
    """Verdict on the settings folder and the two layers (`None` when a file is absent).

    Messages name keys and ids, never values: a rejected value may be a secret.
    """
    provision = f"warpzone provision device {device}"
    if not folder_exists:
        return (
            FAIL,
            f"{folder} does not exist, so this device has no settings layers to apply",
            f"run `{provision}`, and let Syncthing sync the folder",
        )
    if not marker_exists:
        return (
            FAIL,
            f"{folder} is not a running Syncthing folder yet (no .stfolder inside), so "
            "`settings apply` does nothing",
            f"run `{provision}` and check that Syncthing is running",
        )
    errors: list[str] = []
    warnings: list[str] = []
    given = 0
    for source, text in (("common.toml", common), (f"{device}.toml", own)):
        if text is None:
            continue
        given += 1
        try:
            layer, notes = parse_layer(text, source)
        except LayerError as exc:
            errors += exc.errors
            continue
        found, notes_more = validate(layer, profile, source)
        errors += found
        warnings += notes + notes_more
    errors += [f"{name}: Syncthing conflict copy" for name in conflict_copies(names)]
    if errors:
        return (
            FAIL,
            "\n".join(errors + warnings),
            "`settings apply` changes nothing until this is fixed: correct the file on the "
            "hub (secret keys are never layered), and for a conflict copy keep one version "
            "and delete the other",
        )
    if warnings:
        return (
            WARN,
            "\n".join(warnings),
            "check the names against the device's profile; another profile may know them",
        )
    if not given:
        return OK, f"{folder}: no layer files yet (nothing to apply)", ""
    return OK, f"{folder}: {given} layer file(s) valid", ""


def judge_esde_script(
    *,
    device: str,
    script: str,
    exists: bool,
    executable: bool,
    interpreter: str | None,
    interpreter_ok: bool,
) -> tuple[str, str, str]:
    """Verdict on the ES-DE startup script that runs `settings apply`."""
    fix = f"warpzone provision device {device}"
    if not exists:
        return (
            WARN,
            f"no startup script at {script}, so ES-DE does not apply the emulator settings "
            "when it starts",
            fix,
        )
    if not executable:
        return WARN, f"{script} is not executable, so ES-DE cannot run it", fix
    if interpreter is None:
        return (
            WARN,
            f"{script} does not run `python -m warpzone` (edited by hand?)",
            fix,
        )
    if not interpreter_ok:
        return (
            WARN,
            f"{script} runs {interpreter}, which is gone or not executable "
            "(was warpzone reinstalled elsewhere?)",
            f"{fix} (from the warpzone you want ES-DE to use)",
        )
    return OK, f"{script} runs {interpreter}", ""


def judge_event_scripts(xml_text: str | None, device: str) -> tuple[str, str, str]:
    """Verdict on `CustomEventScripts` in `es_settings.xml` (`None` when the file is absent)."""
    if xml_text is None:
        return (
            WARN,
            "es_settings.xml not found, so it is not known whether ES-DE's custom event "
            "scripts are on",
            f"start ES-DE once, quit it, then run `warpzone provision device {device}`",
        )
    if esde.event_scripts_enabled(xml_text) is True:
        return OK, "custom event scripts are on", ""
    return (
        WARN,
        "ES-DE's custom event scripts are off (or unreadable), so it never runs the startup script",
        f"quit ES-DE and run `warpzone provision device {device}` with ES-DE closed, or "
        f"turn it on in ES-DE ({esde.MENU_PATH})",
    )


def judge_systems_override(
    *,
    device: str,
    source: str | None,
    override: str | None,
    source_path: str,
    override_path: str,
    prefix: str,
    renames: Mapping[str, str] | None = None,
    target: str,
) -> tuple[str, str, str]:
    """Verdict on the frontend's system list copy (`None`: the file is absent)."""
    fix = f"quit EmulationStation, then run `warpzone provision device {device}`"
    if source is None:
        return (
            WARN,
            f"the system list {source_path} was not found, so there is nothing to copy "
            f"into {override_path}",
            "",
        )
    if override is None:
        return (
            WARN,
            f"{override_path} is missing, so EmulationStation lists the games in {prefix}, "
            "not the library",
            fix,
        )
    if not frontend.systems_current(source, override, prefix, target, renames):
        return (
            WARN,
            f"{override_path} is out of date: it no longer matches {source_path} with the "
            f"ROM folders moved below {target}",
            fix,
        )
    return OK, f"{override_path} moves the ROM folders from {prefix} to {target}", ""


def judge_artwork(
    *, device: str, plan: artwork.GamelistPlan | None, error: str | None, scraped_path: str
) -> tuple[str, str, str]:
    """Verdict on the frontend's gamelists: `plan` is None when it could not be made."""
    fix = f"quit EmulationStation, then run `warpzone frontend refresh {device}`"
    if plan is None:
        return WARN, error or "the gamelists could not be checked", ""
    if plan.problems:
        return WARN, "; ".join(plan.problems), "fix or remove the scraped gamelist on the hub"
    if not plan.items:
        if plan.waiting:
            return (
                WARN,
                f"no scraped gamelist has arrived in {scraped_path} for the systems on the "
                f"device ({', '.join(plan.waiting[:5])}), so EmulationStation shows no artwork",
                "check that the wz-esde folder syncs (`warpzone status`) and that the hub has "
                "scraped those systems",
            )
        return OK, "no system with games on the device has a frontend system", ""
    missing = [i.system for i in plan.items if i.state == "missing"]
    stale = [i.system for i in plan.items if i.state == "stale"]
    if not missing and not stale:
        return OK, f"{len(plan.items)} gamelists current", ""
    parts = []
    for word, names in (("missing", missing), ("stale", stale)):
        if names:
            shown = ", ".join(names[:6]) + (f" and {len(names) - 6} more" if len(names) > 6 else "")
            parts.append(f"{len(names)} {word} ({shown})")
    return WARN, "gamelists " + "; ".join(parts), fix


def judge_retroarch_paths(
    *, device: str, files: Sequence[tuple[str, str | None]], wants: Mapping[str, str]
) -> tuple[str, str, str]:
    """Verdict on the RetroArch config files: `files` holds (path, text or None if absent)."""
    parts: list[tuple[str, str, str]] = []
    again = f"run `warpzone provision device {device}` with RetroArch closed"
    for path, text in files:
        if text is None:
            parts.append((WARN, f"{path} not found", f"start RetroArch once, then {again}"))
            continue
        gap = frontend.retroarch_gap(text, wants)
        if gap.unwritable:
            keys = ", ".join(sorted(gap.unwritable))
            parts.append(
                (WARN, f"{path}: {keys} appears more than once", "fix the duplicate by hand")
            )
        elif gap.differs:
            keys = ", ".join(
                f"{k} is {v!r}" if v is not None else f"{k} is unset"
                for k, v in gap.differs.items()
            )
            parts.append(
                (
                    WARN,
                    f"{path}: {keys}, so BIOS files or saves are not where the library syncs them",
                    again,
                )
            )
        else:
            parts.append((OK, f"{path} is set", ""))
    return _merge(parts)


def judge_last_apply(info: Mapping[str, object] | None, log_path: str) -> tuple[str, str, str]:
    """Verdict on the summary of the last `settings apply` (`None` when there is none)."""
    if info is None:
        return (
            OPTIONAL,
            "no run yet: nothing has applied the settings on this device",
            "start ES-DE, or run `warpzone settings apply`",
        )
    when = info.get("time")
    when_text = when if isinstance(when, str) and when else "at an unknown time"
    code = _count(info, "exit", "exit_code") or 0
    held = _count(info, "held") or 0
    written = _count(info, "written")
    if code != 0:
        return (
            WARN,
            f"the last run ({when_text}) failed with exit code {code}",
            f"see {log_path} (it never holds values), then run `warpzone settings apply`",
        )
    if held:
        return (
            WARN,
            f"the last run ({when_text}) held {held} key(s) changed on this device: they "
            "were not overwritten",
            "`warpzone settings capture` sends them to the hub, or `warpzone settings "
            "apply --overwrite-local` replaces them with the hub's values",
        )
    done = f", {written} written" if written is not None else ""
    return OK, f"the last run ({when_text}) succeeded{done}", ""


def _local_device(
    ctx: Context, candidates: Sequence[SettingsDevice], folder_id: str, path_key: str
) -> SettingsDevice | None:
    """The configured device among `candidates` this machine is, or None.

    Doctor is offline, so it reads the local Syncthing `config.xml`: this machine is the
    device whose ID is listed there and whose `path_key` path is where Syncthing keeps the
    `folder_id` folder. The hub lists the same device IDs but keeps that folder under its
    own root, so it is not mistaken for a console.
    """
    p = ctx.probes
    if not candidates:
        return None
    platform_name = "darwin" if p.system == "Darwin" else p.system.lower() or "linux"
    try:
        config = localst.find_config(env=p.env, home=p.home, platform=platform_name)
        root = ET.fromstring(p.read_text(config))
    except (localst.LocalSyncthingError, ET.ParseError):
        return None
    ids = {d.get("id") for d in root.findall("device")}
    folder = next((f for f in root.findall("folder") if f.get("id") == folder_id), None)
    if folder is None:
        return None
    here = (folder.get("path") or "").rstrip("/")
    for device in candidates:
        wanted = _expand(device.profile.paths[path_key], p.home).rstrip("/")
        if device.device_id in ids and wanted == here:
            return device
    return None


def _local_settings_device(ctx: Context) -> SettingsDevice | None:
    return _local_device(ctx, ctx.config.settings_devices, "wz-settings", "settings")


def _merge(parts: Sequence[tuple[str, str, str]]) -> tuple[str, str, str]:
    """One verdict from several: the worst status, and the text of every part that is not ok."""
    bad = [part for part in parts if part[0] != OK]
    if not bad:
        return OK, "; ".join(part[1] for part in parts), ""
    status = FAIL if any(part[0] == FAIL for part in bad) else WARN
    return status, "; ".join(part[1] for part in bad), "; ".join(p[2] for p in bad if p[2])


def check_settings(ctx: Context) -> list[Result]:
    """On a device that subscribes to `settings`: layers, ES-DE startup script, last apply."""
    device = _local_settings_device(ctx)
    if device is None:
        return []
    p = ctx.probes
    profile = device.profile
    folder = Path(_expand(profile.paths["settings"], p.home))

    def layer_text(name: str) -> str | None:
        path = folder / name
        return p.read_text(path) if p.is_file(path) else None

    exists = p.is_dir(folder)
    verdict = judge_settings_layers(
        profile,
        device=device.name,
        folder=str(folder),
        folder_exists=exists,
        marker_exists=exists and p.is_dir(folder / ".stfolder"),
        common=layer_text("common.toml") if exists else None,
        own=layer_text(f"{device.name}.toml") if exists else None,
        names=p.list_tree(folder) if exists else [],
    )
    results = [Result("Settings layers", G_SETTINGS, *verdict)]

    if "esde" in profile.paths:
        esde_dir = Path(_expand(profile.paths["esde"], p.home))
        script = esde.script_path(esde_dir)
        found = p.is_file(script)
        interpreter = esde.script_interpreter(p.read_text(script)) if found else None
        xml = esde.settings_path(esde_dir)
        parts = [
            judge_esde_script(
                device=device.name,
                script=str(script),
                exists=found,
                executable=found and p.is_executable(str(script)),
                interpreter=interpreter,
                interpreter_ok=interpreter is not None and p.is_executable(interpreter),
            ),
            judge_event_scripts(p.read_text(xml) if p.is_file(xml) else None, device.name),
        ]
        results.append(Result("ES-DE startup script", G_SETTINGS, *_merge(parts)))

    state = settings_state.state_dir(p.env, p.home)
    verdict = judge_last_apply(settings_state.read_last_apply(state), str(state / "log"))
    results.append(Result("Last settings apply", G_SETTINGS, *verdict))
    return results


def check_frontend(ctx: Context) -> list[Result]:
    """On a device whose profile moves the frontend and RetroArch to the library."""
    device = _local_device(ctx, ctx.config.frontend_devices, "wz-library", "library")
    if device is None:
        return []
    p = ctx.probes
    profile = device.profile
    library = _expand(profile.paths["library"], p.home).rstrip("/")
    results: list[Result] = []
    if (rule := profile.es_systems) is not None:
        source, override = (Path(_expand(x, p.home)) for x in (rule.source, rule.override))
        verdict = judge_systems_override(
            device=device.name,
            source=p.read_text(source) if p.is_file(source) else None,
            override=p.read_text(override) if p.is_file(override) else None,
            source_path=str(source),
            override_path=str(override),
            prefix=rule.prefix,
            renames=rule.renames,
            target=f"{library}/roms/",
        )
        results.append(Result("ES systems override", G_FRONTEND, *verdict))
    if profile.artwork is not None and "frontend" in device.folders:
        results += _check_artwork(ctx, device, library)
    if (pins := profile.retroarch) is not None:
        paths = [Path(_expand(raw, p.home)) for raw in pins.cfg]
        verdict = judge_retroarch_paths(
            device=device.name,
            files=[(str(x), p.read_text(x) if p.is_file(x) else None) for x in paths],
            wants=pins.wants(library),
        )
        results.append(Result("RetroArch paths", G_FRONTEND, *verdict))
    return results


def _check_artwork(ctx: Context, device: SettingsDevice, library: str) -> list[Result]:
    """The gamelists of a device with `[artwork]`, and its refresh timer."""
    p = ctx.probes
    profile = device.profile
    art, rule = profile.artwork, profile.es_systems
    assert art is not None
    layout = artwork.Layout(
        Path(library) / "roms",
        Path(_expand(art.media, p.home)),
        Path(_expand(art.metadata, p.home)),
        Path(_expand(art.gamelists, p.home)),
    )
    plan: artwork.GamelistPlan | None = None
    error: str | None = None
    if rule is None:
        error = "the profile has no [es_systems], so the systems of the frontend are unknown"
    else:
        source = Path(_expand(rule.source, p.home))
        if not p.is_file(source):
            error = f"the system list {source} was not found, so the gamelists cannot be checked"
        else:
            hubs = [
                n
                for n in p.list_dir(layout.roms)
                if artwork.is_plain(n) and p.is_dir(layout.roms / n)
            ]
            files = artwork.Files(
                read=lambda path: p.read_text(path).encode("utf-8") if p.is_file(path) else None,
                is_file=p.is_file,
                exists=lambda path: p.is_file(path) or p.is_dir(path),
            )
            try:
                plan = artwork.plan_gamelists(
                    artwork.FORMATS[art.format],
                    art.kinds,
                    layout,
                    p.read_text(source),
                    prefix=rule.prefix,
                    renames=rule.renames,
                    hubs=hubs,
                    files=files,
                )
            except artwork.ArtworkError as exc:
                error = f"{source}: {exc}"
    verdict = judge_artwork(
        device=device.name, plan=plan, error=error, scraped_path=str(layout.metadata)
    )
    results = [Result("Frontend artwork", G_FRONTEND, *verdict)]
    if art.refresh is not None:
        unit = p.home / ".config" / "systemd" / "user" / f"{artwork.UNIT_NAME}.timer"
        if p.is_file(unit):
            results.append(
                Result(
                    "Artwork timer", G_FRONTEND, OK, f"{unit.name} installed (every {art.refresh})"
                )
            )
        else:
            results.append(
                Result(
                    "Artwork timer",
                    G_FRONTEND,
                    WARN,
                    f"{unit} is missing, so new scrapes are applied only by hand",
                    f"run `warpzone provision device {device.name}`",
                )
            )
    return results


def judge_update_timer(
    *, device: str, files: bool, enabled: bool | None, active: bool | None
) -> tuple[str, str, str]:
    """(status, detail, fix) for the `warpzone self-update` timer. `enabled` and `active` are
    None when systemd could not be asked."""
    timer = updatetimer.TIMER
    if not files:
        return (
            WARN,
            "not installed: this device is only updated by hand",
            f"run `warpzone provision device {device}` (it installs and enables the timer). "
            "Without systemd, schedule `warpzone self-update` with cron; to keep it off, set "
            f"`self_update = false` under [devices.{device}] in the config",
        )
    if enabled is None or active is None:
        return (
            WARN,
            "the units are installed, but cannot ask systemd whether the timer is enabled",
            f"check it with `systemctl --user status {timer}`",
        )
    if not enabled or not active:
        state = "not enabled" if not enabled else "enabled but not running"
        return (
            WARN,
            f"installed, but the timer is {state}: this device is only updated by hand",
            f"run: systemctl --user enable --now {timer}",
        )
    return OK, "installed and enabled: looks for a new release daily and after boot", ""


def check_update_timer(ctx: Context) -> list[Result]:
    """On a Linux device that has not turned self-update off: is the timer installed?"""
    p = ctx.probes
    if p.system != "Linux":
        return []
    device = _local_device(ctx, ctx.config.update_devices, "wz-library", "library")
    if device is None:
        return []
    units = p.home / updatetimer.UNIT_DIR
    files = all(p.is_file(units / name) for name in (updatetimer.SERVICE, updatetimer.TIMER))
    enabled: bool | None = False
    active: bool | None = False
    if files:
        try:
            enabled = (
                p.run(("systemctl", "--user", "is-enabled", updatetimer.TIMER)).returncode == 0
            )
            active = p.run(("systemctl", "--user", "is-active", updatetimer.TIMER)).returncode == 0
        except (OSError, subprocess.SubprocessError):
            enabled = active = None
    verdict = judge_update_timer(device=device.name, files=files, enabled=enabled, active=active)
    return [Result("Self-update timer", G_UPDATE, *verdict)]


def _scrub(text: str, key: str) -> str:
    return text.replace(key, "***") if key else text


_VERSION = re.compile(r"\d+(?:\.\d+)+")


def _first_line(text: str) -> str:
    for line in text.splitlines():
        if line.strip():
            return line.strip()[:100]
    return ""


def _configured_path(spec: ToolSpec, settings: InboxSettings) -> str | None:
    return getattr(settings, spec.setting) if spec.setting in INBOX_KEYS else None


def check_tool(ctx: Context, spec: ToolSpec) -> Result:
    p = ctx.probes
    inbox = ctx.inbox_configured
    required = spec.required_for_inbox and inbox
    missing = FAIL if required else OPTIONAL
    hint = hint_for(spec.hints, p.family)
    where = "required by inbox" if required else spec.purpose
    configured = _configured_path(spec, ctx.config.settings)

    path: str | None = None
    note = ""
    if configured:
        if not p.is_executable(configured):
            return Result(
                spec.key,
                G_TOOLS,
                FAIL,
                f"[inbox] {spec.setting} = {configured} is not an executable file",
                f"fix the path, or remove the setting to look {spec.key} up on PATH. "
                f"To install it: {hint}",
            )
        path = configured
    else:
        for command in spec.commands:
            path = p.which(command)
            if path:
                break
        if path and not spec.inbox_uses_path:
            note = (
                f"found on PATH, but inbox only uses {spec.key} when [inbox] {spec.setting} is set"
            )
        if not path:
            for raw in spec.known_paths:
                known = str(p.home / raw[2:]) if raw.startswith("~/") else raw
                if p.is_executable(known):
                    return Result(
                        spec.key,
                        G_TOOLS,
                        WARN,
                        f"found at {known}, which is not on PATH",
                        f'set {spec.setting} = "{known}" in the [inbox] table',
                    )
    if not path:
        return Result(spec.key, G_TOOLS, missing, f"not found ({where})", hint)

    if spec.version_args is None:
        return Result(spec.key, G_TOOLS, OK, f"{path} ({spec.purpose})")
    try:
        out = p.run([path, *spec.version_args])
    except (OSError, subprocess.SubprocessError) as exc:
        reason = getattr(exc, "strerror", None) or type(exc).__name__
        return Result(
            spec.key,
            G_TOOLS,
            FAIL,
            f"{path} cannot be run ({reason})",
            f"reinstall it for this machine ({p.machine or 'this architecture'}): {hint}",
        )
    line = _first_line(out.output)
    if not _VERSION.search(line):
        shown = f": {line}" if line else ""
        return Result(
            spec.key,
            G_TOOLS,
            WARN,
            f"{path} ran but printed no recognizable version{shown}",
            f"check that it is the right program; to reinstall: {hint}",
        )
    if note:
        return Result(
            spec.key,
            G_TOOLS,
            WARN,
            f"{line} ({path}); {note}",
            f'set {spec.setting} = "{path}" in the [inbox] table',
        )
    return Result(spec.key, G_TOOLS, OK, f"{line} ({path})")


def check_tools(ctx: Context) -> list[Result]:
    return [check_tool(ctx, spec) for spec in TOOLS]


def _dat_files(library: DatLibrary) -> list[Path]:
    return [path for paths in library.paths.values() for path in paths]


def check_dats(ctx: Context) -> list[Result]:
    p = ctx.probes
    dats_dir = ctx.config.settings.dats_dir
    inbox = ctx.inbox_configured
    missing = FAIL if inbox else OPTIONAL
    clone = DAT_CLONE_HINT
    if not dats_dir:
        return [
            Result(
                "DAT folder",
                G_DATS,
                missing,
                "dats_dir is not set in [inbox]; inbox needs the Redump and No-Intro DATs "
                "to identify games"
                if inbox
                else "no [inbox] dats_dir; only needed by inbox (or pass --dats)",
                clone,
            )
        ]
    directory = Path(dats_dir)
    if not p.is_dir(directory):
        return [Result("DAT folder", G_DATS, missing, f"{directory} does not exist", clone)]
    library = DatLibrary(directory, None)
    found = library.systems()
    known = sorted(set(SYSTEMS.values()))
    if not found:
        return [
            Result(
                "DAT folder",
                G_DATS,
                missing,
                f"{directory} holds no Redump or No-Intro DATs warpzone knows",
                f"point dats_dir at libretro-database's metadat folder. {clone}",
            )
        ]
    absent = [s for s in known if s not in found]
    detail = f"{directory}: {len(found)} of {len(known)} systems ({', '.join(found)})"
    if absent:
        detail += f"; no DAT for {', '.join(absent)}"
    results = [Result("DAT folder", G_DATS, OK, detail)]
    try:
        newest = max(p.mtime(path) for path in _dat_files(library))
    except OSError as exc:
        results.append(
            Result("DAT age", G_DATS, WARN, f"cannot read DAT file times ({exc.strerror})")
        )
        return results
    days = int((p.now() - newest) // DAY_SECONDS)
    if days > DAT_MAX_AGE_DAYS:
        results.append(
            Result(
                "DAT age",
                G_DATS,
                WARN,
                f"newest DAT is {days} days old (limit {DAT_MAX_AGE_DAYS}); newer dumps may not "
                "be recognized",
                "update your libretro-database clone: git -C <path of the clone> pull",
            )
        )
    else:
        results.append(Result("DAT age", G_DATS, OK, f"newest DAT is {max(days, 0)} days old"))
    return results


# The arcade databases `inbox` can use: ([inbox] key, name, what it is, where to get it).
ARCADE_DATS = (
    (
        "mame_dat",
        "MAME DAT",
        "MAME's -listxml (or the official mameXXXXlx.zip, read as it is)",
        "download the mameXXXXlx.zip of the MAME version your frontend runs from "
        "https://github.com/mamedev/mame/releases (the file is the zipped listxml)",
    ),
    (
        "fbneo_dat",
        "FinalBurn Neo DAT",
        "the FinalBurn Neo arcade DAT",
        "download 'FinalBurn Neo (ClrMame Pro XML, Arcade only).dat' from the dats/ folder of "
        "https://github.com/libretro/FBNeo",
    ),
    (
        "mame2003plus_dat",
        "MAME 2003-Plus DAT",
        "the MAME 2003-Plus DAT",
        "download mame2003-plus.xml from the metadata/ folder of "
        "https://github.com/libretro/mame2003-plus-libretro",
    ),
)


def check_arcade_dats(ctx: Context) -> list[Result]:
    """The optional arcade databases: each is `[--]` when not set, a problem when set
    to something that is not a file (`inbox` would refuse to start), and `[ok]` when
    the file is there."""
    results = []
    for key, name, what, fix in ARCADE_DATS:
        value = getattr(ctx.config.settings, key)
        if not value:
            results.append(
                Result(name, G_DATS, OPTIONAL, f"{key} is not set; arcade zips need {what}", fix)
            )
            continue
        path = Path(value)
        try:
            size = path.stat().st_size if path.is_file() else None
        except OSError:
            size = None
        if size is None:
            results.append(Result(name, G_DATS, FAIL, f"{path} is not a file", fix))
        else:
            results.append(Result(name, G_DATS, OK, f"{path} ({size // 2**20} MiB)"))
    return results


def check_switch(ctx: Context) -> list[Result]:
    """Presence only: the titledb clone and the keys file. The keys file is never opened."""
    p = ctx.probes
    settings = ctx.config.settings
    results: list[Result] = []
    if not settings.titledb_dir:
        results.append(
            Result(
                "titledb_dir",
                G_SWITCH,
                OPTIONAL,
                "not set; only needed for Switch games (.nsp / .nsz) in inbox",
                TITLEDB_HINT,
            )
        )
    else:
        directory = Path(settings.titledb_dir)
        if not p.is_dir(directory):
            results.append(
                Result(
                    "titledb_dir",
                    G_SWITCH,
                    FAIL,
                    f"{directory} does not exist",
                    f"clone it: {TITLEDB_HINT}",
                )
            )
        elif not (found := switch.region_files(directory)):
            results.append(
                Result(
                    "titledb_dir",
                    G_SWITCH,
                    FAIL,
                    f"{directory} has no region files (like US.en.json)",
                    f"is it a clone of {TITLEDB_REPO}? {TITLEDB_HINT}",
                )
            )
        else:
            results.append(
                Result("titledb_dir", G_SWITCH, OK, f"{directory}: {len(found)} region file(s)")
            )
    keys = Path(settings.switch_keys) if settings.switch_keys else switch.default_keys(p.home)
    if p.is_file(keys):
        results.append(
            Result("switch_keys", G_SWITCH, OK, f"{keys} (not read, only passed to nsz)")
        )
    elif settings.switch_keys:
        results.append(
            Result(
                "switch_keys",
                G_SWITCH,
                FAIL,
                f"[inbox] switch_keys = {keys} is not a file",
                f"fix the path. {KEYS_HINT}",
            )
        )
    else:
        results.append(
            Result(
                "switch_keys",
                G_SWITCH,
                OPTIONAL,
                f"{keys} not found; nsz needs it to read Switch metadata and to decompress .nsz",
                KEYS_HINT,
            )
        )
    return results


def check_hub(ctx: Context) -> list[Result]:
    p = ctx.probes
    settings = ctx.config.settings
    root = settings.root
    level = FAIL
    source = "[inbox] root"
    if not root:
        hub_root = ctx.config.hub_root
        if hub_root and p.is_dir(Path(hub_root)):
            root, level, source = hub_root, WARN, "[hub] root (this machine looks like the hub)"
        else:
            return [
                Result(
                    "Hub folder",
                    G_HUB,
                    OPTIONAL,
                    "no [inbox] root; only needed by inbox (or pass --root), where it is the "
                    "hub's games share as mounted on this machine",
                    'set root = "/path/to/mounted/share" in the [inbox] table',
                )
            ]
    folder = Path(root)
    mount_fix = (
        f"mount the hub's games share on this machine, then check that {source} points at "
        "the folder that holds inbox/ and library/"
    )
    if not p.is_dir(folder):
        return [
            Result("Hub folder", G_HUB, level, f"{folder} is not reachable ({source})", mount_fix)
        ]
    needed = ("inbox", "library/roms")
    lacking = [n for n in needed if not p.is_dir(folder / n)]
    if lacking:
        return [
            Result(
                "Hub folder",
                G_HUB,
                level,
                f"{folder} lacks {', '.join(n + '/' for n in lacking)}",
                f"is {source} the hub root? It must hold inbox/ and library/roms/ "
                "(see 'Hub layout' in the README)",
            )
        ]
    results = [Result("Hub folder", G_HUB, OK, f"{folder} has inbox/ and library/roms/")]
    try:
        total, free = p.disk_free(folder)
    except OSError as exc:
        results.append(Result("Free space", G_HUB, WARN, f"cannot read: {exc.strerror or exc}"))
        return results
    detail = f"{free / GIB:.1f} GiB free of {total / GIB:.0f} GiB"
    if free < MIN_FREE_GIB * GIB:
        results.append(
            Result(
                "Free space",
                G_HUB,
                WARN,
                f"{detail} (below {MIN_FREE_GIB} GiB)",
                "free space on the hub's volume before adding games; inbox never overwrites, "
                "but a full disk stops placing",
            )
        )
    else:
        results.append(Result("Free space", G_HUB, OK, detail))
    return results


def check_ra(ctx: Context) -> list[Result]:
    # The value is never read into a result: only whether it is there.
    if ctx.probes.env.get(ra.KEY_ENV):
        return [Result(ra.KEY_ENV, G_RA, OK, "set (hash lookups enabled)")]
    return [
        Result(
            ra.KEY_ENV,
            G_RA,
            OPTIONAL,
            "not set; inbox still hashes with RAHasher, but cannot look hashes up",
            f"export {ra.KEY_ENV}=<your key> (from your retroachievements.org settings) in the "
            "shell or service that runs warpzone; keep it out of the config and the repository",
        )
    ]


def collapse_inbox(results: Sequence[Result]) -> list[Result]:
    """Without an [inbox] table, the optional inbox-only items become one line. Items that
    are ok, warn or fail stay: the machine has that tool or folder, and a broken one is worth
    saying. Pure."""
    kept = [r for r in results if not (r.group in INBOX_GROUPS and r.status == OPTIONAL)]
    hidden = len(results) - len(kept)
    if not hidden:
        return kept
    return [
        *kept,
        Result(
            "Inbox",
            G_INBOX,
            OK,
            f"not set up on this machine ({hidden} optional item(s) not listed): only the "
            "desktop that runs `warpzone inbox` needs its tools, Switch titledb and keys, "
            "DATs, hub folder and RetroAchievements key",
            "add an [inbox] table to the config to check them here "
            "(see examples/config.example.toml)",
        ),
    ]


CHECKS: tuple[Callable[[Context], list[Result]], ...] = (
    check_base,
    check_config,
    check_syncthing,
    check_game_mode,
    check_flatpak_access,
    check_syncthing_unit,
    check_flycast,
    check_vita3k,
    check_settings,
    check_frontend,
    check_update_timer,
    check_tools,
    check_switch,
    check_dats,
    check_arcade_dats,
    check_hub,
    check_ra,
)


def run_checks(probes: Probes) -> list[Result]:
    ctx = Context(probes, load_config_state(probes.config_path))
    results: list[Result] = []
    for check in CHECKS:
        results += check(ctx)
    return results if ctx.inbox_configured else collapse_inbox(results)


# ------------------------------------------------------------------ output


def _wrap(text: str, width: int) -> list[str]:
    """`text` as lines of at most `width` characters (a word longer than that stays whole,
    so a path or a command is never cut). Newlines in `text` start new lines, and a line
    that starts with a space is a code line: it is kept as it is, however long."""
    lines: list[str] = []
    for line in text.splitlines() or [""]:
        if line.startswith(" "):
            lines.append(line.rstrip())
            continue
        lines += textwrap.wrap(line, width, break_long_words=False, break_on_hyphens=False) or [""]
    return lines


def render(results: Sequence[Result], width: int = FALLBACK_WIDTH) -> list[str]:
    """The report as lines no wider than `width` where the words allow it: groups in the
    order they first appear, one line per check, continuation lines for details and
    fixes. Wrapped lines hang under the start of the detail; a fix hangs under its text."""
    lines: list[str] = []
    label_width = max((len(r.name) for r in results), default=0)
    indent = " " * (7 + label_width + 1)
    room = max(width - len(indent), MIN_TEXT_WIDTH)
    groups = list(dict.fromkeys(r.group for r in results))
    for group in groups:
        if lines:
            lines.append("")
        lines.append(group)
        for r in (r for r in results if r.group == group):
            detail = _wrap(r.detail, room)
            lines.append(f"  {MARKS[r.status]} {r.name:<{label_width}} {detail[0]}".rstrip())
            lines += [f"{indent}{extra}" for extra in detail[1:]]
            if r.fix:
                hang = " " * len(FIX_PREFIX)
                fix = _wrap(r.fix, room - len(FIX_PREFIX))
                lines.append(f"{indent}{FIX_PREFIX}{fix[0]}")
                lines += [f"{indent}{hang}{extra}" for extra in fix[1:]]
    lines += ["", summary(results)]
    return lines


def summary(results: Sequence[Result]) -> str:
    count = {s: sum(1 for r in results if r.status == s) for s in MARKS}
    parts = [
        f"{count[FAIL]} problem(s)",
        f"{count[WARN]} warning(s)",
        f"{count[OPTIONAL]} optional item(s) not set up",
    ]
    return f"{', '.join(parts)}. " + (
        "Fix the [!!] lines above." if count[FAIL] else "Nothing required is missing."
    )


def exit_code(results: Sequence[Result]) -> int:
    return 1 if any(r.status == FAIL for r in results) else 0


def terminal_width() -> int:
    return shutil.get_terminal_size((FALLBACK_WIDTH, 24)).columns


def run_doctor(probes: Probes, out: Callable[[str], None] = print, width: int | None = None) -> int:
    results = run_checks(probes)
    out(f"warpzone doctor (config: {probes.config_path})")
    out("")
    for line in render(results, terminal_width() if width is None else width):
        out(line)
    return exit_code(results)
