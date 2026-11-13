"""What every doctor check shares: statuses, the check groups, `Result`, `ConfigState`, `Context`.

The config as the checks see it, and the helpers that find this machine's device in the local
Syncthing config and merge several judgments into one line. See development/design/doctor.md.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path

from warpzone import localst
from warpzone.config import InboxSettings
from warpzone.doctor_probes import Probes
from warpzone.model import FolderSpec
from warpzone.paths import expand_home
from warpzone.profiles import Profile

OK, WARN, FAIL, OPTIONAL = "ok", "warn", "fail", "optional"
MARKS = {OK: "[ok]", WARN: "[! ]", FAIL: "[!!]", OPTIONAL: "[--]"}

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
G_PRUNE = "Pruning"
G_INBOX = "Inbox"
G_WAY = "Where inbox runs"
# The groups whose items only matter to the machine that runs `warpzone inbox`.
INBOX_GROUPS = (G_WAY, G_TOOLS, G_SWITCH, G_DATS, G_HUB, G_RA)


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
    channel: str = "stable"
    pin: str | None = None


@dataclass(frozen=True)
class DeviceFolders:
    """A configured device and the folders its config describes (paths still with `~`)."""

    name: str
    device_id: str
    specs: tuple[FolderSpec, ...]


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
    # The devices that subscribe to `play-state`; empty when the config does not load.
    play_state_devices: tuple[SettingsDevice, ...] = ()
    # The devices that subscribe to `library` and have not turned `self_update` off.
    update_devices: tuple[SettingsDevice, ...] = ()
    # The devices that subscribe to `library` and have not turned `auto_prune` off.
    prune_devices: tuple[SettingsDevice, ...] = ()
    # Every configured device with the folders its config describes; empty when the config
    # does not load.
    device_folders: tuple[DeviceFolders, ...] = ()
    # [hub] api_url and whether [hub] api_cert_sha256 is set; None/False when the config does
    # not load.
    hub_api_url: str | None = None
    hub_cert_pinned: bool = False


@dataclass(frozen=True)
class Context:
    probes: Probes
    config: ConfigState

    @property
    def inbox_configured(self) -> bool:
        return "inbox" in self.config.sections


def local_device(
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
        wanted = expand_home(device.profile.paths[path_key], p.home).rstrip("/")
        if device.device_id in ids and wanted == here:
            return device
    return None


def local_settings_device(ctx: Context) -> SettingsDevice | None:
    return local_device(ctx, ctx.config.settings_devices, "wz-settings", "settings")


def merge_verdicts(parts: Sequence[tuple[str, str, str]]) -> tuple[str, str, str]:
    """One verdict from several: the worst status, and the text of every part that is not ok."""
    bad = [part for part in parts if part[0] != OK]
    if not bad:
        return OK, "; ".join(part[1] for part in parts), ""
    status = FAIL if any(part[0] == FAIL for part in bad) else WARN
    return status, "; ".join(part[1] for part in bad), "; ".join(p[2] for p in bad if p[2])


def scrub(text: str, key: str) -> str:
    return text.replace(key, "***") if key else text
