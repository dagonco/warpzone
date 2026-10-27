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
import time
import tomllib
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from warpzone import __version__, localst, model, ra
from warpzone.config import (
    INBOX_KEYS,
    ConfigError,
    InboxSettings,
    parse_config,
    parse_inbox,
)
from warpzone.dat import SYSTEMS, DatLibrary
from warpzone.model import ModelError
from warpzone.profiles import ProfileError, available_profiles, load_profile
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

G_BASE = "warpzone and Python"
G_CONFIG = "Config"
G_SYNCTHING = "Syncthing"
G_TOOLS = "Inbox tools"
G_DATS = "DATs"
G_HUB = "Hub folder"
G_RA = "RetroAchievements"

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
SYNCTHING_HINTS: HintTable = {
    "macos": "brew install syncthing && brew services start syncthing",
    "debian": "sudo apt install syncthing && systemctl --user enable --now syncthing",
    "fedora": "sudo dnf install syncthing && systemctl --user enable --now syncthing",
    "arch": "sudo pacman -S syncthing && systemctl --user enable --now syncthing",
    "steamos": "flatpak install flathub com.github.zocker_160.SyncThingy "
    "(the read-only root rules out system packages)",
    "any": "install Syncthing from https://syncthing.net/downloads/ and start it",
}
SYNCTHING_START_HINTS: HintTable = {
    "macos": "brew services start syncthing (or open the Syncthing app)",
    "steamos": "start SyncThingy (or `systemctl --user start syncthing` if you use the service)",
    "linux": "systemctl --user enable --now syncthing",
    "any": "start Syncthing",
}


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
        "Switch .nsp/.xci compression; not used by inbox yet",
        NSZ_HINTS,
        version_args=None,
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
    is_executable: Callable[[str], bool] = _is_executable
    mtime: Callable[[Path], float] = _mtime
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
    try:
        if "hub" in data or "devices" in data:
            cfg = parse_config(data, set(available_profiles()))
            profiles = {
                name: load_profile(name) for name in {d.profile for d in cfg.devices.values()}
            }
            for device in cfg.devices.values():
                model.device_specs(cfg, device.name, profiles[device.profile])
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
    return ConfigState(path, True, error, sections, devices, settings, hub_root)


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
    for key, label, enables in SECTIONS:
        if key in cfg.sections:
            extra = f" ({', '.join(cfg.devices)})" if key == "devices" and cfg.devices else ""
            results.append(Result(label, G_CONFIG, OK, f"present{extra}; enables {enables}"))
        else:
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
            for known in spec.known_paths:
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


CHECKS: tuple[Callable[[Context], list[Result]], ...] = (
    check_base,
    check_config,
    check_syncthing,
    check_tools,
    check_dats,
    check_hub,
    check_ra,
)


def run_checks(probes: Probes) -> list[Result]:
    ctx = Context(probes, load_config_state(probes.config_path))
    results: list[Result] = []
    for check in CHECKS:
        results += check(ctx)
    return results


# ------------------------------------------------------------------ output


def render(results: Sequence[Result]) -> list[str]:
    """The report as lines: groups in the order they first appear, one line per check,
    continuation lines for details and fixes."""
    lines: list[str] = []
    width = max((len(r.name) for r in results), default=0)
    indent = " " * (7 + width + 1)
    groups = list(dict.fromkeys(r.group for r in results))
    for group in groups:
        if lines:
            lines.append("")
        lines.append(group)
        for r in (r for r in results if r.group == group):
            detail = r.detail.splitlines() or [""]
            lines.append(f"  {MARKS[r.status]} {r.name:<{width}} {detail[0]}".rstrip())
            lines += [f"{indent}{extra}" for extra in detail[1:]]
            if r.fix:
                lines.append(f"{indent}fix: {r.fix}")
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


def run_doctor(probes: Probes, out: Callable[[str], None] = print) -> int:
    results = run_checks(probes)
    out(f"warpzone doctor (config: {probes.config_path})")
    out("")
    for line in render(results):
        out(line)
    return exit_code(results)
