"""The desired state: config + profiles in, folder specs out. Pure: no I/O."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from warpzone import artwork, ignores
from warpzone.config import Config, Device
from warpzone.errors import UsageError
from warpzone.profiles import Artwork, Profile

# Staggered versioning keeps 30 days of history (maxAge is in seconds).
STAGGERED: dict[str, Any] = {"type": "staggered", "params": {"maxAge": str(30 * 86400)}}
NO_VERSIONING: dict[str, Any] = {"type": ""}


LIBRARY_ID = "wz-library"
MEDIA_ID = "wz-media"
# `frontend/media/<system>/videos` is only for the hub's UI (a video is tens of MiB); the
# devices that get the whole folder ignore it, and the devices with `[artwork]` never list it.
MEDIA_DEVICE_IGNORES = ("/*/videos",)
ESDE_ID = "wz-esde"
PLAY_STATE_ID = "wz-play-state"


class ModelError(UsageError):
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
    # Set only for whitelisted folders: ignores that let only the whitelist files through.
    # A device folder starts on them until its whitelist has arrived; a hub folder starts
    # on them when it is created, so the first scan reaches the whitelists at once.
    bootstrap_ignores: tuple[str, ...] | None = None
    # Files that must exist in the folder root for `ignores` to load.
    required_files: tuple[str, ...] = ()
    # Save folders whose directory is missing are skipped, not created.
    skip_if_missing: bool = False
    # With skip_if_missing: create the missing directory when this one exists.
    create_when: str | None = None
    # Earlier device paths of a save folder: see `provision_folders.is_move`.
    previous_paths: tuple[str, ...] = ()
    # Only whitelisted device folders set it: it bounds how long a whitelist
    # edited on the hub takes to apply. None leaves Syncthing's default.
    rescan_interval_s: int | None = None
    # The folder is on a filesystem without POSIX permissions (exFAT): Syncthing neither
    # syncs nor sets them. Only device folders of a profile with `ignore_perms` carry it.
    ignore_perms: bool = False

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
        if self.ignore_perms:
            body["ignorePerms"] = True
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
    # Ignores a device adds to `fixed_ignores`; the hub's folder never takes them.
    device_ignores: tuple[str, ...] = ()
    save: bool = False
    # The hub keeps 30 days of history of this folder, as for save folders.
    history: bool = False
    skip_if_missing: bool = False
    create_when: str | None = None
    previous_paths: tuple[str, ...] = ()


_STATIC: dict[str, tuple[_Def, ...]] = {
    "library": (_Def(LIBRARY_ID, "library", "sendonly", "receiveonly", "library", whitelist=True),),
    "songs": (_Def("wz-songs", "songs", "sendonly", "receiveonly", "songs", whitelist=True),),
    # /logs holds what warpzone writes on the hub (gamelist backups): never synced.
    "tools": (
        _Def("wz-tools", "tools", "sendonly", "receiveonly", "tools", fixed_ignores=("/logs",)),
    ),
    "frontend": (
        # Videos stay on the hub: a device that receives the whole folder ignores them.
        _Def(
            MEDIA_ID,
            "frontend/media",
            "sendreceive",
            "sendreceive",
            "media",
            device_ignores=MEDIA_DEVICE_IGNORES,
        ),
        _Def(
            ESDE_ID,
            "frontend/es-de",
            "sendreceive",
            "sendreceive",
            "esde",
            fixed_ignores=("!/gamelists", "!/collections", "*"),
        ),
    ),
    # Layers captured on a device travel to the hub, so the folder is two-way; `*.wz-tmp`
    # is the temporary file of an atomic write.
    "settings": (
        _Def(
            "wz-settings",
            "settings",
            "sendreceive",
            "sendreceive",
            "settings",
            fixed_ignores=("*.wz-tmp",),
            history=True,
        ),
    ),
    # One file per device, each written by its device only, so it never conflicts. It is apart
    # from `wz-esde` so that folder stays as it is on the devices that share it.
    "play-state": (
        _Def(
            PLAY_STATE_ID,
            "frontend/play-state",
            "sendreceive",
            "sendreceive",
            "play_state",
            fixed_ignores=("*.wz-tmp",),
            history=True,
        ),
    ),
}


def _defs_for(device: Device, profile: Profile) -> list[_Def]:
    defs: list[_Def] = []
    if "play-state" in device.folders and "frontend" not in device.folders:
        raise ModelError(
            f"device {device.name!r} subscribes to 'play-state' without 'frontend': "
            "the play state lives in the gamelists that group brings"
        )
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
                        create_when=save.create_when,
                        previous_paths=save.previous_paths,
                    )
                )
            continue
        if group == "bios":
            for place in profile.bios.values():
                defs.append(
                    _Def(
                        f"wz-bios-{place.emu}-{place.kind}",
                        f"library/{place.hub}",
                        "sendonly",
                        "receiveonly",
                        device_path=place.path,
                        fixed_ignores=place.ignore,
                        skip_if_missing=True,
                        create_when=place.create_when,
                    )
                )
            continue
        for item in _STATIC[group]:
            # A profile with `[artwork]` places the frontend folders itself (`device_specs`).
            if item.path_key not in profile.paths and not (group == "frontend" and profile.artwork):
                hint = " or an [artwork] table" if group == "frontend" else ""
                raise ModelError(
                    f"device {device.name!r} subscribes to {group!r} but profile "
                    f"{profile.name!r} has no paths.{item.path_key}{hint}"
                )
            defs.append(item)
    return defs


def _versioning(item: _Def, *, hub: bool) -> dict[str, Any]:
    return dict(STAGGERED if (hub and (item.save or item.history)) else NO_VERSIONING)


def _artwork_spec(
    item: _Def, art: Artwork, systems: Sequence[str]
) -> tuple[str, str, tuple[str, ...]]:
    """Path, type and ignores of a hub frontend folder on a device that has `[artwork]`.

    The device folder is receive-only and limited to the hub folders of `systems`; the hub's
    own folder, shared with other devices, is untouched (`hub_specs` never calls this).
    """
    if item.id == MEDIA_ID:
        return art.media, "receiveonly", artwork.media_ignores(systems, art.kinds)
    return art.metadata, "receiveonly", artwork.metadata_ignores(systems)


def device_specs(
    cfg: Config,
    device_name: str,
    profile: Profile,
    artwork_systems: Sequence[str] | None = None,
) -> list[FolderSpec]:
    """Folders for one device. Paths still contain `~`; the device expands them.

    `artwork_systems` are the hub system folders a device with `[artwork]` receives
    artwork for (`artwork.library_systems`); without them its artwork folders let nothing in.
    """
    device = cfg.devices[device_name]
    specs: list[FolderSpec] = []
    for item in _defs_for(device, profile):
        art = profile.artwork if item.id in (MEDIA_ID, ESDE_ID) else None
        if art is not None:
            path, kind, wanted = _artwork_spec(item, art, artwork_systems or ())
            specs.append(
                FolderSpec(
                    id=item.id,
                    label=item.id,
                    path=path,
                    type=kind,
                    devices=(cfg.hub.device_id,),
                    versioning=_versioning(item, hub=False),
                    ignores=ignores.inline(wanted),
                    ignore_perms=profile.ignore_perms,
                )
            )
            continue
        path = item.device_path or profile.paths[item.path_key or ""]
        if item.whitelist:
            final = ignores.device_whitelist(
                device.name, profile.library_excludes() if item.id == LIBRARY_ID else ()
            )
            bootstrap: tuple[str, ...] | None = ignores.BOOTSTRAP_LINES
            required: tuple[str, ...] = (ignores.COMMON_FILE, ignores.device_file(device.name))
        else:
            final = ignores.inline(item.fixed_ignores + item.device_ignores)
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
                create_when=item.create_when,
                previous_paths=item.previous_paths,
                rescan_interval_s=device.rescan_seconds if item.whitelist else None,
                ignore_perms=profile.ignore_perms,
            )
        )
    return specs


def hub_specs(cfg: Config, profiles: dict[str, Profile]) -> list[FolderSpec]:
    """Every folder any device subscribes to, shared with exactly its subscribers."""
    subscribers: dict[str, list[str]] = {}
    defs: dict[str, _Def] = {}
    # Placement paths of every profile in use: the hub library never sends them,
    # each is delivered by its own folder instead.
    excluded: tuple[str, ...] = ()
    for device in cfg.devices.values():
        profile = profiles[device.profile]
        for item in _defs_for(device, profile):
            defs.setdefault(item.id, item)
            subscribers.setdefault(item.id, []).append(device.name)
            if item.id == LIBRARY_ID:
                excluded = tuple(dict.fromkeys((*excluded, *profile.placement_ignores())))

    specs: list[FolderSpec] = []
    for folder_id, item in defs.items():
        names = subscribers[folder_id]
        if item.whitelist:
            final = ignores.hub_whitelist(excluded if folder_id == LIBRARY_ID else ())
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
                bootstrap_ignores=ignores.BOOTSTRAP_LINES if item.whitelist else None,
                required_files=required,
            )
        )
    return specs
