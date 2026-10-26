"""The desired state: config + profiles in, folder specs out. Pure: no I/O."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from warpzone import ignores
from warpzone.config import Config, Device
from warpzone.profiles import Profile

# Staggered versioning keeps 30 days of history (maxAge is in seconds).
STAGGERED: dict[str, Any] = {"type": "staggered", "params": {"maxAge": str(30 * 86400)}}
NO_VERSIONING: dict[str, Any] = {"type": ""}


class ModelError(Exception):
    pass


@dataclass(frozen=True)
class FolderSpec:
    """One Syncthing folder as warpzone wants it on one machine."""

    id: str
    label: str
    path: str
    type: str
    devices: tuple[str, ...]  # remote device IDs; Syncthing adds the local one itself
    versioning: dict[str, Any] = field(default_factory=lambda: dict(NO_VERSIONING))
    ignores: tuple[str, ...] = ()
    # Set only for folders whose ignores include a whitelist file that may not
    # have arrived yet (device side).
    bootstrap_ignores: tuple[str, ...] | None = None
    # Files that must exist in the folder root for `ignores` to load.
    required_files: tuple[str, ...] = ()
    # Save folders whose directory is missing are skipped, not created.
    skip_if_missing: bool = False
    # Only whitelisted device folders set it: it bounds how long a whitelist
    # edited on the hub takes to apply. None leaves Syncthing's default.
    rescan_interval_s: int | None = None

    def body(self) -> dict[str, Any]:
        """The folder keys warpzone owns, in Syncthing's REST shape."""
        body: dict[str, Any] = {
            "id": self.id,
            "label": self.label,
            "path": self.path,
            "type": self.type,
            "devices": [{"deviceID": d} for d in self.devices],
            "versioning": self.versioning,
            "fsWatcherEnabled": True,
        }
        if self.rescan_interval_s is not None:
            body["rescanIntervalS"] = self.rescan_interval_s
        return body


@dataclass(frozen=True)
class _Def:
    """A folder independent of which machine it lands on."""

    id: str
    hub_rel: str
    hub_type: str
    device_type: str
    path_key: str | None = None  # profile [paths] key; None for save folders
    device_path: str | None = None  # save folders carry their own path
    whitelist: bool = False
    fixed_ignores: tuple[str, ...] = ()
    save: bool = False
    skip_if_missing: bool = False


_STATIC: dict[str, tuple[_Def, ...]] = {
    "library": (
        _Def("wz-library", "library", "sendonly", "receiveonly", "library", whitelist=True),
    ),
    "songs": (_Def("wz-songs", "songs", "sendonly", "receiveonly", "songs", whitelist=True),),
    "tools": (_Def("wz-tools", "tools", "sendonly", "receiveonly", "tools"),),
    "frontend": (
        _Def("wz-media", "frontend/media", "sendreceive", "sendreceive", "media"),
        _Def(
            "wz-esde",
            "frontend/es-de",
            "sendreceive",
            "sendreceive",
            "esde",
            fixed_ignores=("!/gamelists", "!/collections", "*"),
        ),
    ),
}


def _defs_for(device: Device, profile: Profile) -> list[_Def]:
    defs: list[_Def] = []
    for group in device.folders:
        if group == "saves":
            for save in profile.saves.values():
                defs.append(
                    _Def(
                        f"wz-save-{save.emu}-{save.kind}",
                        f"saves/{save.emu}/{save.kind}",
                        "sendreceive",
                        "sendreceive",
                        device_path=save.path,
                        fixed_ignores=save.ignore,
                        save=True,
                        skip_if_missing=True,
                    )
                )
            continue
        for item in _STATIC[group]:
            if item.path_key not in profile.paths:
                raise ModelError(
                    f"device {device.name!r} subscribes to {group!r} but profile "
                    f"{profile.name!r} has no paths.{item.path_key}"
                )
            defs.append(item)
    return defs


def _versioning(item: _Def, *, hub: bool) -> dict[str, Any]:
    return dict(STAGGERED if (hub and item.save) else NO_VERSIONING)


def device_specs(cfg: Config, device_name: str, profile: Profile) -> list[FolderSpec]:
    """Folders for one device. Paths still contain `~`; the device expands them."""
    device = cfg.devices[device_name]
    specs: list[FolderSpec] = []
    for item in _defs_for(device, profile):
        path = item.device_path if item.save else profile.paths[item.path_key or ""]
        assert path is not None
        if item.whitelist:
            final = ignores.device_whitelist(device.name)
            bootstrap: tuple[str, ...] | None = ignores.BOOTSTRAP_LINES
            required = (ignores.COMMON_FILE, ignores.device_file(device.name))
        else:
            final = ignores.inline(item.fixed_ignores)
            bootstrap = None
            required = ()
        specs.append(
            FolderSpec(
                id=item.id,
                label=item.id,
                path=path,
                type=item.device_type,
                devices=(cfg.hub.device_id,),
                versioning=_versioning(item, hub=False),
                ignores=final,
                bootstrap_ignores=bootstrap,
                required_files=required,
                skip_if_missing=item.skip_if_missing,
                rescan_interval_s=device.rescan_seconds if item.whitelist else None,
            )
        )
    return specs


def hub_specs(cfg: Config, profiles: dict[str, Profile]) -> list[FolderSpec]:
    """Every folder any device subscribes to, shared with exactly its subscribers."""
    subscribers: dict[str, list[str]] = {}
    defs: dict[str, _Def] = {}
    for device in cfg.devices.values():
        for item in _defs_for(device, profiles[device.profile]):
            defs.setdefault(item.id, item)
            subscribers.setdefault(item.id, []).append(device.name)

    specs: list[FolderSpec] = []
    for folder_id, item in defs.items():
        names = subscribers[folder_id]
        if item.whitelist:
            final = ignores.hub_whitelist()
            required = (ignores.COMMON_FILE, *(ignores.device_file(n) for n in names))
        else:
            final = ignores.inline(item.fixed_ignores)
            required = ()
        specs.append(
            FolderSpec(
                id=folder_id,
                label=item.id,
                path=f"{cfg.hub.root.rstrip('/')}/{item.hub_rel}",
                type=item.hub_type,
                devices=tuple(cfg.devices[n].device_id for n in names),
                versioning=_versioning(item, hub=True),
                ignores=final,
                required_files=required,
            )
        )
    return specs
