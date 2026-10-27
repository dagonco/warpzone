"""Private configuration: loading and validation (tomllib)."""

from __future__ import annotations

import os
import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# Syncthing applies a changed whitelist only on a full scan of the folder, and a
# freshly synced whitelist file does not trigger one reliably (see design.md).
DEFAULT_RESCAN_SECONDS = 300
MIN_RESCAN_SECONDS = 5
FOLDER_GROUPS = ("library", "songs", "tools", "saves", "frontend", "bios")
_DEVICE_ID_RE = re.compile(r"^[A-Z2-7]{7}(-[A-Z2-7]{7}){7}$")
_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]*$")


class ConfigError(Exception):
    pass


@dataclass(frozen=True)
class Hub:
    name: str
    device_id: str
    root: str
    api_url: str
    api_key: str = field(repr=False)
    addresses: tuple[str, ...] = ("dynamic",)


@dataclass(frozen=True)
class Device:
    name: str
    device_id: str
    profile: str
    folders: tuple[str, ...]
    addresses: tuple[str, ...] = ("dynamic",)
    rescan_seconds: int = DEFAULT_RESCAN_SECONDS


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
    "nsz",
    "titledb_dir",
    "switch_keys",
)


@dataclass(frozen=True)
class InboxSettings:
    """The optional `[inbox]` table: where the hub is mounted on this machine, the DATs
    and the external tools `warpzone inbox` uses. A tool left unset is looked up on PATH
    (RAHasher only when set). `titledb_dir` (a clone of blawar/titledb) and `switch_keys`
    (the console keys file, default ~/.switch/prod.keys) are for Switch games."""

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
    nsz: str | None = None
    titledb_dir: str | None = None
    switch_keys: str | None = None


@dataclass(frozen=True)
class Config:
    hub: Hub
    devices: dict[str, Device]
    inbox: InboxSettings = field(default_factory=InboxSettings)


def default_config_path(env: dict[str, str] | None = None) -> Path:
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
    _check_keys(raw, set(INBOX_KEYS), "inbox", errors)
    values: dict[str, str] = {}
    for key in INBOX_KEYS:
        if key not in raw:
            continue
        value = raw[key]
        if not isinstance(value, str) or not value.strip():
            errors.append(f"inbox.{key}: must be a non-empty path string")
        else:
            values[key] = str(Path(value.strip()).expanduser())
    return InboxSettings(**values)


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
    _check_keys(data, {"hub", "devices", "inbox"}, "config", errors)
    inbox = parse_inbox(data.get("inbox"), errors)

    raw_hub = data.get("hub")
    hub: Hub | None = None
    if not isinstance(raw_hub, dict):
        errors.append("[hub]: required table")
    else:
        _check_keys(
            raw_hub,
            {"name", "device_id", "root", "api_url", "api_key", "api_key_file", "addresses"},
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
        if root and len(root) > 1:
            root = root.rstrip("/")
        hub = Hub(name, device_id, root, api_url.rstrip("/"), api_key, addresses)

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
            {"device_id", "profile", "folders", "addresses", "rescan_seconds"},
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
        devices[dev_name] = Device(
            dev_name,
            device_id,
            profile,
            tuple(dict.fromkeys(folders)),
            _addresses(entry, where, errors),
            rescan,
        )

    if errors:
        raise ConfigError("\n".join(errors))
    assert hub is not None
    return Config(hub, devices, inbox)


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
