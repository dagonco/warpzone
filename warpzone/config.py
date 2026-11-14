"""Private configuration: loading and validation (tomllib)."""

from __future__ import annotations

import os
import re
import tomllib
import urllib.parse
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from warpzone.errors import UsageError
from warpzone.versions import tag_key

# Syncthing applies a changed whitelist only on a full scan of the folder, and a
# freshly synced whitelist file does not trigger one reliably (see
# development/design/hub-and-folders.md).
DEFAULT_RESCAN_SECONDS = 300
MIN_RESCAN_SECONDS = 5
CHANNELS = ("stable", "rc")
FOLDER_GROUPS = (
    "library",
    "songs",
    "tools",
    "saves",
    "frontend",
    "bios",
    "settings",
    "play-state",
)
_DEVICE_ID_RE = re.compile(r"^[A-Z2-7]{7}(-[A-Z2-7]{7}){7}$")
_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]*$")
_SYSTEM_RE = re.compile(r"^[a-z0-9_-]+$")
_SHA256_HEX_RE = re.compile(r"[0-9a-f]{64}")


class ConfigError(UsageError):
    pass


@dataclass(frozen=True)
class Hub:
    name: str
    device_id: str
    root: str
    api_url: str
    api_key: str = field(repr=False)
    addresses: tuple[str, ...] = ("dynamic",)
    # SHA-256 of the hub Syncthing's GUI certificate as 64 lowercase hex digits (not secret).
    # Set: the hub client speaks TLS and accepts only that certificate; see
    # development/design/configuration.md.
    api_cert_sha256: str | None = None


@dataclass(frozen=True)
class Device:
    name: str
    device_id: str
    profile: str
    folders: tuple[str, ...]
    addresses: tuple[str, ...] = ("dynamic",)
    rescan_seconds: int = DEFAULT_RESCAN_SECONDS
    # Whether `provision device` installs the timer that runs `warpzone self-update`.
    self_update: bool = True
    # Which releases the device follows: "stable" (vX.Y.Z) or "rc" (those and vX.Y.ZrcN).
    channel: str = "stable"
    # A tag (vX.Y.Z or vX.Y.ZrcN) the device stays on, whatever the channel; None for none.
    pin: str | None = None
    # Whether `provision device` creates on the hub the folders this device needs and the hub
    # lacks (never a whitelisted one, and never a change to a folder the hub has).
    auto_hub: bool = False
    # Whether `provision device` installs the timer that runs `prune --apply --unattended`.
    # It only applies to a device that lists `library`; see `prunes_unattended`.
    auto_prune: bool = True
    # Systems (folder names below library/roms/) the device does not run. The library UI never
    # turns one on for it and `check` reports a whitelist that names one. Empty: it runs all.
    exclude_systems: tuple[str, ...] = ()

    @property
    def prunes_unattended(self) -> bool:
        """Whether the device gets the unattended prune timer: it lists `library` and
        `auto_prune` is not false."""
        return self.auto_prune and "library" in self.folders


INBOX_KEYS = (
    "root",
    "dats_dir",
    "work_dir",
    "chdman",
    "bsdtar",
    "rahasher",
    "dolphin_tool",
    "mame_dat",
    "fbneo_dat",
    "mame2003plus_dat",
    "gamelist_mame",
    "gamelist_ps3",
    "nsz",
    "orbis_pkg_util",
    "vita3k",
    "vita_zrif",
    "titledb_dir",
    "switch_keys",
    "skyscraper",
)
# The one number of the table: the minutes any external tool of `inbox` may run (see below).
TOOL_TIMEOUT_KEY = "tool_timeout_minutes"
# The threads chdman may use (`--numprocessors`); unset leaves it to chdman.
CHDMAN_THREADS_KEY = "chdman_threads"
# Whether `createcd` keeps chdman's FLAC codec for CD audio (default true); see inbox_core.Tools.
CHDMAN_FLAC_KEY = "chdman_flac"
DEFAULT_TOOL_TIMEOUT_MINUTES = 240


@dataclass(frozen=True)
class InboxSettings:
    """The optional `[inbox]` table: where the hub is mounted on this machine, the DATs
    and the external tools `warpzone inbox` uses. A tool left unset is looked up on PATH
    (RAHasher only when set). `titledb_dir` (a clone of blawar/titledb) and `switch_keys`
    (the console keys file, default ~/.switch/prod.keys) are for Switch games. `orbis_pkg_util`
    unpacks PS4 packages. `vita3k` (Vita3K.app or its binary) installs PS Vita packages with the
    zRIF keys of `vita_zrif`, a file the user keeps. `skyscraper` is the Skyscraper program that
    `warpzone scrape` runs (`Skyscraper` on PATH when unset). `tool_timeout_minutes` is how long any
    one call of an external tool may run before it is killed and its item goes to review.
    `chdman_threads` caps the threads of each chdman conversion (chdman's own count when unset).
    `chdman_flac = false` makes `createcd` leave FLAC out of its codecs: FLAC makes chdman use
    memory larger than the image (see development/design/inbox.md)."""

    root: str | None = None
    dats_dir: str | None = None
    work_dir: str | None = None
    chdman: str | None = None
    bsdtar: str | None = None
    rahasher: str | None = None
    dolphin_tool: str | None = None
    mame_dat: str | None = None
    fbneo_dat: str | None = None
    mame2003plus_dat: str | None = None
    gamelist_mame: str | None = None
    gamelist_ps3: str | None = None
    nsz: str | None = None
    orbis_pkg_util: str | None = None
    vita3k: str | None = None
    vita_zrif: str | None = None
    titledb_dir: str | None = None
    switch_keys: str | None = None
    skyscraper: str | None = None
    tool_timeout_minutes: int = DEFAULT_TOOL_TIMEOUT_MINUTES
    chdman_threads: int | None = None
    chdman_flac: bool = True


SERVE_KEYS = ("listen", "token_file")
DEFAULT_LISTEN = "0.0.0.0:8780"


@dataclass(frozen=True)
class ServeSettings:
    """The optional `[serve]` table, read only by `warpzone serve`: the address it listens on
    and the file whose first line is the UI token (else `WARPZONE_UI_TOKEN` is used)."""

    listen: str = DEFAULT_LISTEN
    token_file: str | None = None


def split_listen(value: str) -> tuple[str, int] | None:
    """`host:port` (or `[v6]:port`) as a host and a port, or None when it is not one."""
    host, sep, port = value.rpartition(":")
    host = host.removeprefix("[").removesuffix("]")
    if not sep or not host or not port.isdigit() or not 0 <= int(port) <= 65535:
        return None
    return host, int(port)


RA_KEYS = ("user",)
RA_USER = re.compile(r"^[A-Za-z0-9_.-]{1,40}$")


@dataclass(frozen=True)
class RaSettings:
    """The optional `[ra]` table: the RetroAchievements user whose progress the library UI
    shows. The user name is public on RA and is not a secret; the API key is (`RA_API_KEY`)."""

    user: str | None = None


@dataclass(frozen=True)
class Config:
    hub: Hub
    devices: dict[str, Device]
    inbox: InboxSettings = field(default_factory=InboxSettings)
    serve: ServeSettings = field(default_factory=ServeSettings)
    ra: RaSettings = field(default_factory=RaSettings)


# Defaults of the command line, kept here so that building the parser imports no command.
DEFAULT_WAIT_SECONDS = 600.0  # `provision device`: how long to wait for the whitelists
HUB_WAIT_SECONDS = 120.0  # `provision hub`: how long a new folder waits for its whitelists
UNATTENDED_WAIT_SECONDS = 30.0
DEFAULT_MAX_GIB = 50.0  # `prune`: the most it deletes in one run
DEFAULT_RESCAN_TIMEOUT = 600.0  # `prune`: how long it waits for the rescan


def default_config_path(env: Mapping[str, str] | None = None) -> Path:
    env = os.environ if env is None else env
    base = env.get("XDG_CONFIG_HOME")
    root = Path(base) if base else Path.home() / ".config"
    return root / "warpzone" / "config.toml"


def _check_keys(table: dict[str, Any], allowed: set[str], where: str, errors: list[str]) -> None:
    for key in sorted(set(table) - allowed):
        errors.append(f"{where}: unknown key {key!r}")


def _string(table: dict[str, Any], key: str, where: str, errors: list[str]) -> str:
    value = table.get(key)
    if not isinstance(value, str) or not value.strip():
        errors.append(f"{where}.{key}: required, must be a non-empty string")
        return ""
    return value.strip()


def _device_id(table: dict[str, Any], where: str, errors: list[str]) -> str:
    value = _string(table, "device_id", where, errors)
    if value and not _DEVICE_ID_RE.match(value):
        errors.append(f"{where}.device_id: not a Syncthing device ID (8 groups of 7 characters)")
    return value


def _addresses(table: dict[str, Any], where: str, errors: list[str]) -> tuple[str, ...]:
    raw = table.get("addresses", ["dynamic"])
    if not isinstance(raw, list) or not raw or not all(isinstance(a, str) and a for a in raw):
        errors.append(f"{where}.addresses: must be a non-empty list of strings")
        return ("dynamic",)
    return tuple(raw)


def _exclude_systems(table: dict[str, Any], where: str, errors: list[str]) -> tuple[str, ...]:
    raw = table.get("exclude_systems", [])
    key = f"{where}.exclude_systems"
    if not isinstance(raw, list) or not all(isinstance(s, str) for s in raw):
        errors.append(f"{key}: must be a list of system names")
        return ()
    bad = [s for s in raw if not _SYSTEM_RE.match(s)]
    if bad:
        errors.append(
            f"{key}: not a plain system name: {', '.join(repr(s) for s in bad)} "
            "(lower-case letters, digits, '-' and '_' only)"
        )
    repeated = sorted({s for s in raw if raw.count(s) > 1})
    if repeated:
        errors.append(f"{key}: listed more than once: {', '.join(repr(s) for s in repeated)}")
    return tuple(raw) if not bad and not repeated else ()


def normalize_cert_sha256(text: str) -> str | None:
    """The 64 lowercase hex digits in a pasted SHA-256 fingerprint, or None if it is not one.

    ':' and whitespace are ignored, so what `openssl x509 -fingerprint -sha256` prints after
    '=' (AB:CD:...), the spaced form of a browser and bare hex all give the same pin.
    """
    digits = "".join(ch for ch in text if ch != ":" and not ch.isspace()).lower()
    return digits if _SHA256_HEX_RE.fullmatch(digits) else None


def _read_cert_pin(hub: dict[str, Any], api_url: str, errors: list[str]) -> str | None:
    if "api_cert_sha256" not in hub:
        return None
    raw = hub["api_cert_sha256"]
    if not isinstance(raw, str):
        errors.append("hub.api_cert_sha256: must be a string")
        return None
    pin = normalize_cert_sha256(raw)
    if pin is None:
        errors.append(
            "hub.api_cert_sha256: must be the 64 hex digits of the certificate's SHA-256 "
            "fingerprint (':' and spaces are ignored), as printed after '=' by: "
            "openssl x509 -in https-cert.pem -noout -fingerprint -sha256"
        )
        return None
    if urllib.parse.urlsplit(api_url).scheme == "http":
        errors.append(
            "hub.api_cert_sha256: needs an https:// api_url; with http:// the API key would "
            "still cross the network in the clear"
        )
        return None
    return pin


def _read_api_key(hub: dict[str, Any], errors: list[str]) -> str:
    inline = hub.get("api_key")
    key_file = hub.get("api_key_file")
    if (inline is None) == (key_file is None):
        errors.append("hub: set exactly one of `api_key` or `api_key_file`")
        return ""
    if inline is not None:
        if not isinstance(inline, str) or not inline.strip():
            errors.append("hub.api_key: must be a non-empty string")
            return ""
        return inline.strip()
    if not isinstance(key_file, str) or not key_file:
        errors.append("hub.api_key_file: must be a path string")
        return ""
    path = Path(key_file).expanduser()
    try:
        key = path.read_text(encoding="utf-8").strip()
    except OSError as exc:
        errors.append(f"hub.api_key_file: cannot read {path} ({exc.strerror})")
        return ""
    if not key:
        errors.append(f"hub.api_key_file: {path} is empty")
    return key


def parse_inbox(raw: object, errors: list[str]) -> InboxSettings:
    if raw is None:
        return InboxSettings()
    if not isinstance(raw, dict):
        errors.append("[inbox]: must be a table")
        return InboxSettings()
    _check_keys(
        raw, {*INBOX_KEYS, TOOL_TIMEOUT_KEY, CHDMAN_THREADS_KEY, CHDMAN_FLAC_KEY}, "inbox", errors
    )
    minutes = DEFAULT_TOOL_TIMEOUT_MINUTES
    if TOOL_TIMEOUT_KEY in raw:
        given = raw[TOOL_TIMEOUT_KEY]
        if not isinstance(given, int) or isinstance(given, bool) or given < 1:
            errors.append(
                f"inbox.{TOOL_TIMEOUT_KEY}: must be a whole number of minutes, at least 1"
            )
        else:
            minutes = given
    values: dict[str, Any] = {TOOL_TIMEOUT_KEY: minutes}
    if CHDMAN_THREADS_KEY in raw:
        threads = raw[CHDMAN_THREADS_KEY]
        if not isinstance(threads, int) or isinstance(threads, bool) or threads < 1:
            errors.append(
                f"inbox.{CHDMAN_THREADS_KEY}: must be a whole number of threads, at least 1"
            )
        else:
            values[CHDMAN_THREADS_KEY] = threads
    if CHDMAN_FLAC_KEY in raw:
        flac = raw[CHDMAN_FLAC_KEY]
        if not isinstance(flac, bool):
            errors.append(f"inbox.{CHDMAN_FLAC_KEY}: must be true or false")
        else:
            values[CHDMAN_FLAC_KEY] = flac
    for key in INBOX_KEYS:
        if key not in raw:
            continue
        value = raw[key]
        if not isinstance(value, str) or not value.strip():
            errors.append(f"inbox.{key}: must be a non-empty path string")
        else:
            values[key] = str(Path(value.strip()).expanduser())
    return InboxSettings(**values)


def parse_serve(raw: object, errors: list[str]) -> ServeSettings:
    if raw is None:
        return ServeSettings()
    if not isinstance(raw, dict):
        errors.append("[serve]: must be a table")
        return ServeSettings()
    _check_keys(raw, set(SERVE_KEYS), "serve", errors)
    listen = raw.get("listen", DEFAULT_LISTEN)
    if not isinstance(listen, str) or split_listen(listen.strip()) is None:
        errors.append("serve.listen: must be HOST:PORT, such as 0.0.0.0:8780")
        listen = DEFAULT_LISTEN
    token_file = raw.get("token_file")
    if "token_file" in raw and (not isinstance(token_file, str) or not token_file.strip()):
        errors.append("serve.token_file: must be a non-empty path string")
        token_file = None
    return ServeSettings(
        listen.strip(), str(Path(token_file.strip()).expanduser()) if token_file else None
    )


def parse_ra(raw: object, errors: list[str]) -> RaSettings:
    if raw is None:
        return RaSettings()
    if not isinstance(raw, dict):
        errors.append("[ra]: must be a table")
        return RaSettings()
    _check_keys(raw, set(RA_KEYS), "ra", errors)
    user = raw.get("user")
    if "user" not in raw:
        return RaSettings()
    if not isinstance(user, str) or not RA_USER.match(user.strip()):
        errors.append(
            "ra.user: must be a RetroAchievements user name "
            "(letters, digits, `_`, `-` and `.`, at most 40)"
        )
        return RaSettings()
    return RaSettings(user.strip())


def load_ra_settings(path: Path) -> RaSettings:
    """Only the `[ra]` table of the config, so `catalog` works on a machine with no Syncthing
    devices configured. A missing file means no user."""
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return RaSettings()
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise ConfigError(f"{path}: {exc}") from exc
    errors: list[str] = []
    settings = parse_ra(data.get("ra"), errors)
    if errors:
        raise ConfigError(f"{path}:\n" + "\n".join(errors))
    return settings


def load_inbox_settings(path: Path) -> InboxSettings:
    """Only the `[inbox]` table of the config, so `inbox` works on a machine that has no
    Syncthing devices configured. A missing file means all defaults."""
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return InboxSettings()
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise ConfigError(f"{path}: {exc}") from exc
    errors: list[str] = []
    settings = parse_inbox(data.get("inbox"), errors)
    if errors:
        raise ConfigError(f"{path}:\n" + "\n".join(errors))
    return settings


def parse_config(data: dict[str, Any], known_profiles: set[str] | None = None) -> Config:
    """Validate a decoded config. `known_profiles` enables the profile-name check."""
    errors: list[str] = []
    _check_keys(data, {"hub", "devices", "inbox", "serve", "ra"}, "config", errors)
    inbox = parse_inbox(data.get("inbox"), errors)
    serve = parse_serve(data.get("serve"), errors)
    ra_settings = parse_ra(data.get("ra"), errors)

    raw_hub = data.get("hub")
    hub: Hub | None = None
    if not isinstance(raw_hub, dict):
        errors.append("[hub]: required table")
    else:
        _check_keys(
            raw_hub,
            {
                "name",
                "device_id",
                "root",
                "api_url",
                "api_key",
                "api_key_file",
                "addresses",
                "api_cert_sha256",
            },
            "hub",
            errors,
        )
        name = _string(raw_hub, "name", "hub", errors)
        device_id = _device_id(raw_hub, "hub", errors)
        root = _string(raw_hub, "root", "hub", errors)
        api_url = _string(raw_hub, "api_url", "hub", errors)
        if api_url and not api_url.startswith(("http://", "https://")):
            errors.append("hub.api_url: must start with http:// or https://")
        addresses = _addresses(raw_hub, "hub", errors)
        api_key = _read_api_key(raw_hub, errors)
        cert_pin = _read_cert_pin(raw_hub, api_url, errors)
        if root and not root.startswith("/"):
            errors.append(f"hub.root: must be an absolute path (starting with /), not {root!r}")
        if root and len(root) > 1:
            root = root.rstrip("/")
        hub = Hub(name, device_id, root, api_url.rstrip("/"), api_key, addresses, cert_pin)

    devices: dict[str, Device] = {}
    raw_devices = data.get("devices")
    if not isinstance(raw_devices, dict) or not raw_devices:
        errors.append("[devices.<name>]: at least one device is required")
        raw_devices = {}
    seen_ids = {hub.device_id: "hub"} if hub and hub.device_id else {}
    for dev_name, entry in raw_devices.items():
        where = f"devices.{dev_name}"
        if not _NAME_RE.match(dev_name):
            errors.append(f"{where}: name must be lowercase letters, digits, '-' or '_'")
        if not isinstance(entry, dict):
            errors.append(f"{where}: must be a table")
            continue
        _check_keys(
            entry,
            {
                "device_id",
                "profile",
                "folders",
                "addresses",
                "rescan_seconds",
                "self_update",
                "channel",
                "pin",
                "auto_hub",
                "auto_prune",
                "exclude_systems",
            },
            where,
            errors,
        )
        device_id = _device_id(entry, where, errors)
        if device_id in seen_ids:
            errors.append(f"{where}.device_id: same ID as {seen_ids[device_id]}")
        elif device_id:
            seen_ids[device_id] = dev_name
        profile = _string(entry, "profile", where, errors)
        if profile and known_profiles is not None and profile not in known_profiles:
            errors.append(
                f"{where}.profile: unknown profile {profile!r} "
                f"(available: {', '.join(sorted(known_profiles))})"
            )
        folders = entry.get("folders")
        if (
            not isinstance(folders, list)
            or not folders
            or not all(isinstance(f, str) for f in folders)
        ):
            errors.append(f"{where}.folders: required, a non-empty list of strings")
            folders = []
        else:
            for group in folders:
                if group not in FOLDER_GROUPS:
                    errors.append(
                        f"{where}.folders: unknown folder {group!r} "
                        f"(expected {', '.join(FOLDER_GROUPS)})"
                    )
        rescan = entry.get("rescan_seconds", DEFAULT_RESCAN_SECONDS)
        if not isinstance(rescan, int) or isinstance(rescan, bool) or rescan < MIN_RESCAN_SECONDS:
            errors.append(f"{where}.rescan_seconds: must be an integer >= {MIN_RESCAN_SECONDS}")
            rescan = DEFAULT_RESCAN_SECONDS
        self_update = entry.get("self_update", True)
        if not isinstance(self_update, bool):
            errors.append(f"{where}.self_update: must be true or false")
            self_update = True
        channel = entry.get("channel", "stable")
        if not isinstance(channel, str) or channel not in CHANNELS:
            errors.append(f"{where}.channel: must be 'stable' or 'rc'")
            channel = "stable"
        pin = entry.get("pin")
        if "pin" in entry and (not isinstance(pin, str) or tag_key(pin) is None):
            errors.append(f"{where}.pin: must be a release tag such as v0.3.0 or v0.3.0rc1")
            pin = None
        auto_hub = entry.get("auto_hub", False)
        if not isinstance(auto_hub, bool):
            errors.append(f"{where}.auto_hub: must be true or false")
            auto_hub = False
        auto_prune = entry.get("auto_prune", True)
        if not isinstance(auto_prune, bool):
            errors.append(f"{where}.auto_prune: must be true or false")
            auto_prune = True
        exclude_systems = _exclude_systems(entry, where, errors)
        devices[dev_name] = Device(
            dev_name,
            device_id,
            profile,
            tuple(dict.fromkeys(folders)),
            _addresses(entry, where, errors),
            rescan,
            self_update,
            channel,
            pin,
            auto_hub,
            auto_prune,
            exclude_systems,
        )

    if errors:
        raise ConfigError("\n".join(errors))
    assert hub is not None
    return Config(hub, devices, inbox, serve, ra_settings)


def load_config(path: Path, known_profiles: set[str] | None = None) -> Config:
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ConfigError(
            f"config not found: {path}\n"
            "copy examples/config.example.toml there and fill in your device IDs"
        ) from exc
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise ConfigError(f"{path}: {exc}") from exc
    try:
        return parse_config(data, known_profiles)
    except ConfigError as exc:
        raise ConfigError(f"{path}:\n{exc}") from exc
