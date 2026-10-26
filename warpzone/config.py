"""Private configuration: loading and validation (tomllib)."""

from __future__ import annotations

import os
import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

FOLDER_GROUPS = ("library", "songs", "tools", "saves", "frontend")
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


@dataclass(frozen=True)
class Config:
    hub: Hub
    devices: dict[str, Device]


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


def parse_config(data: dict[str, Any], known_profiles: set[str] | None = None) -> Config:
    """Validate a decoded config. `known_profiles` enables the profile-name check."""
    errors: list[str] = []
    _check_keys(data, {"hub", "devices"}, "config", errors)

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
        _check_keys(entry, {"device_id", "profile", "folders", "addresses"}, where, errors)
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
        devices[dev_name] = Device(
            dev_name,
            device_id,
            profile,
            tuple(dict.fromkeys(folders)),
            _addresses(entry, where, errors),
        )

    if errors:
        raise ConfigError("\n".join(errors))
    assert hub is not None
    return Config(hub, devices)


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
