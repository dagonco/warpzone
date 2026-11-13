"""The doctor checks of the base install and the config: Python, warpzone, the config file.

Also `load_config_state`, which reads the config into the `ConfigState` every other check takes,
and the hub API line (`judge_hub_api`, pure). See development/design/doctor.md.
"""

from __future__ import annotations

import tomllib
import urllib.parse
from pathlib import Path

from warpzone import __version__, model
from warpzone.config import ConfigError, parse_config, parse_inbox
from warpzone.doctor_core import (
    FAIL,
    G_BASE,
    G_CONFIG,
    OK,
    OPTIONAL,
    WARN,
    ConfigState,
    Context,
    DeviceFolders,
    Result,
    SettingsDevice,
)
from warpzone.model import ModelError
from warpzone.profiles import ProfileError, available_profiles, load_profile
from warpzone.syncthing import _is_loopback

MIN_PYTHON = (3, 11)

# What each config table enables, for the "[hub] / [devices] / [inbox]" lines.
SECTIONS: tuple[tuple[str, str, str], ...] = (
    ("hub", "[hub]", "provision hub, check, status (reaching the hub's Syncthing)"),
    ("devices", "[devices]", "provision device, prune, render-ignores (one table per console)"),
    ("inbox", "[inbox]", "warpzone inbox (this machine mounts the hub and files new games)"),
)


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
    play_state_devices: list[SettingsDevice] = []
    update_devices: list[SettingsDevice] = []
    prune_devices: list[SettingsDevice] = []
    device_folders: list[DeviceFolders] = []
    hub_api_url: str | None = None
    hub_cert_pinned = False
    try:
        if "hub" in data or "devices" in data:
            cfg = parse_config(data, set(available_profiles()))
            hub_api_url = cfg.hub.api_url
            hub_cert_pinned = cfg.hub.api_cert_sha256 is not None
            profiles = {
                name: load_profile(name) for name in {d.profile for d in cfg.devices.values()}
            }
            for device in cfg.devices.values():
                specs = model.device_specs(cfg, device.name, profiles[device.profile])
                for spec in specs:
                    sync_paths[spec.path] = None
                device_folders.append(DeviceFolders(device.name, device.device_id, tuple(specs)))
                if "settings" in device.folders:
                    entry = SettingsDevice(device.name, device.device_id, profiles[device.profile])
                    settings_devices.append(entry)
                profile = profiles[device.profile]
                art = profile.artwork is not None and "frontend" in device.folders
                if "library" in device.folders and (
                    profile.es_systems
                    or profile.retroarch
                    or profile.shadps4
                    or profile.cemu
                    or profile.links
                    or art
                ):
                    frontend_devices.append(
                        SettingsDevice(device.name, device.device_id, profile, device.folders)
                    )
                if "play-state" in device.folders:
                    play_state_devices.append(
                        SettingsDevice(device.name, device.device_id, profile, device.folders)
                    )
                if device.prunes_unattended:
                    prune_devices.append(
                        SettingsDevice(device.name, device.device_id, profile, device.folders)
                    )
                if "library" in device.folders and device.self_update:
                    update_devices.append(
                        SettingsDevice(
                            device.name,
                            device.device_id,
                            profile,
                            device.folders,
                            device.channel,
                            device.pin,
                        )
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
    sharing = tuple(play_state_devices) if error is None else ()
    updating = tuple(update_devices) if error is None else ()
    pruning = tuple(prune_devices) if error is None else ()
    described = tuple(device_folders) if error is None else ()
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
        sharing,
        updating,
        pruning,
        described,
        hub_api_url if error is None else None,
        hub_cert_pinned and error is None,
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
    results += check_hub_api(cfg)
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


def judge_hub_api(api_url: str, pinned: bool) -> Result | None:
    """The "Hub API" line for `[hub] api_url` and whether the certificate is pinned; None
    for plain http on loopback, which has nothing to say."""
    parsed = urllib.parse.urlsplit(api_url)
    loopback = _is_loopback(parsed.hostname or "")
    if parsed.scheme == "https":
        if pinned:
            detail = (
                "https with the certificate pinned (hub.api_cert_sha256); "
                "`warpzone check` proves it matches"
            )
        elif loopback:
            detail = "https on loopback"
        else:
            detail = (
                "https, verified against the system's trusted certificates; Syncthing's own "
                "self-signed certificate fails this, set hub.api_cert_sha256"
            )
        return Result("Hub API", G_CONFIG, OK, detail)
    if loopback:
        return None
    return Result(
        "Hub API",
        G_CONFIG,
        WARN,
        f"{api_url} is plain http, so the API key crosses the network in the clear",
        "use https:// in [hub] api_url and set hub.api_cert_sha256 to the SHA-256 fingerprint "
        "of the hub Syncthing's https-cert.pem (see reference/configuration.md)",
    )


def check_hub_api(cfg: ConfigState) -> list[Result]:
    if cfg.hub_api_url is None:
        return []
    line = judge_hub_api(cfg.hub_api_url, cfg.hub_cert_pinned)
    return [] if line is None else [line]
