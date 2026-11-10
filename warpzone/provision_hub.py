"""Hub provisioning: the hub's devices and folders, and the whitelist check."""

from __future__ import annotations

import posixpath
import time
from collections.abc import Callable, Collection
from dataclasses import dataclass
from typing import Any

from warpzone.config import HUB_WAIT_SECONDS, Config
from warpzone.model import FolderSpec, hub_specs
from warpzone.profiles import Profile
from warpzone.provision_folders import (
    PREFIX,
    ProvisionError,
    apply_ignores,
    ensure_device,
    in_index,
    rescan,
    same_path,
    unpause,
    upsert_folder,
    wait_pending,
)
from warpzone.report import Reporter
from warpzone.syncthing import Client, SyncthingError


def _bootstrap_hub_folder(
    client: Client,
    spec: FolderSpec,
    *,
    timeout: float,
    poll: float,
    rep: Reporter,
    sleep: Callable[[float], None],
    clock: Callable[[], float],
) -> bool:
    """A newly created whitelisted hub folder: let only the whitelists through first.

    The first full scan of a big library can take hours, and devices wait for their
    `patterns-<device>.stignore` to show up in the hub's index. With every other file
    ignored the scan reaches the whitelists at once; once they are indexed the final
    ignores go in and the full scan starts. If they never show up the final ignores
    are set anyway, so the folder is never left half-configured. Returns True when the
    whitelists were indexed (or, in dry-run, would be waited for).
    """
    assert spec.bootstrap_ignores is not None
    client.set_ignores(spec.id, list(spec.bootstrap_ignores))
    rep.note(spec.id, "bootstrap ignores set")
    unpause(client, spec, rep)
    client.scan(spec.id)
    if client.dry_run:
        rep.note(spec.id, f"dry-run: would wait up to {timeout:.0f}s for the whitelists")
        pending: list[str] = []
    else:
        pending = wait_pending(
            client,
            spec.id,
            spec.required_files,
            in_index,
            timeout=timeout,
            poll=poll,
            rep=rep,
            sleep=sleep,
            clock=clock,
        )
    client.set_ignores(spec.id, list(spec.ignores))
    rep.note(spec.id, f"final ignores set, {rescan(client, spec.id)}")
    if pending:
        rep.warn(
            f"{spec.id}: {', '.join(pending)} not in the hub's index after {timeout:.0f}s; "
            f"the final ignores are set anyway. Add them under {spec.path}, then run "
            "`warpzone check`"
        )
    return not pending


def _missing_on(client: Client, spec: FolderSpec) -> list[str]:
    names = set(client.browse(spec.id))
    return [f for f in spec.required_files if f not in names]


def unsubscribed(
    spec: FolderSpec, current: dict[str, Any], managed: Collection[str]
) -> tuple[str, ...]:
    """The devices a hub `wz-*` folder is shared with but the config no longer gives it (pure).

    `current` is the hub's folder as `GET /rest/config/folders` returns it, `managed` the
    device IDs of the config. A device the config does not name, and the hub's own entry
    (never one of the config's devices), are left out: they are not ours to take off.
    The IDs come back in the folder's order.
    """
    if not spec.id.startswith(PREFIX):
        return ()
    wanted = set(spec.devices)
    return tuple(
        d["deviceID"]
        for d in current.get("devices") or []
        if d["deviceID"] in managed and d["deviceID"] not in wanted
    )


def unclaimed(
    specs: Collection[FolderSpec], existing: dict[str, dict[str, Any]], names: dict[str, str]
) -> tuple[str, ...]:
    """Warnings for hub `wz-*` folders that no spec describes but a config device has (pure).

    Nothing is changed for them: an admin checkout older than a console's release does not
    know every folder the consoles use. `names` maps a config device ID to its name."""
    described = {spec.id for spec in specs}
    texts: list[str] = []
    for folder_id, folder in existing.items():
        if not folder_id.startswith(PREFIX) or folder_id in described:
            continue
        sharing = [
            names[d["deviceID"]] for d in folder.get("devices") or [] if d["deviceID"] in names
        ]
        if sharing:
            texts.append(
                f"{folder_id}: no device in the config subscribes to it, still shared with "
                f"{', '.join(sharing)} (left as it is; remove them from it in the hub's "
                "Syncthing if they should not have it)"
            )
    return tuple(texts)


def _hub_folder(client: Client, folder_id: str) -> dict[str, Any] | None:
    """The hub's folder as it is now, or None when it is not there."""
    return next((f for f in client.folders() if f["id"] == folder_id), None)


def provision_hub(
    cfg: Config,
    profiles: dict[str, Profile],
    client: Client,
    rep: Reporter,
    *,
    wait_timeout: float = HUB_WAIT_SECONDS,
    poll: float = 2.0,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> None:
    actual = client.my_id()
    if actual != cfg.hub.device_id:
        raise ProvisionError(
            f"the API at {cfg.hub.api_url} belongs to device {actual[:7]}..., "
            f"but hub.device_id starts with {cfg.hub.device_id[:7]}..."
        )
    hub_devices = client.devices()
    known = {d["deviceID"] for d in hub_devices}
    auto_accept = {d["deviceID"] for d in hub_devices if d.get("autoAcceptFolders")}
    names = {d.device_id: d.name for d in cfg.devices.values()}
    for device in cfg.devices.values():
        ensure_device(client, device.device_id, device.name, device.addresses, known, rep)

    existing = {f["id"]: f for f in client.folders()}
    specs = hub_specs(cfg, profiles)
    for spec in specs:
        current = existing.get(spec.id)
        gone: tuple[str, ...] = ()
        if current is not None and unsubscribed(spec, current, names):
            # The list was read before the waits of earlier folders, and a PATCH replaces
            # the whole list: decide again on the folder as it is now.
            current = _hub_folder(client, spec.id) or current
            gone = unsubscribed(spec, current, names)
        created = upsert_folder(client, spec, current, rep, drop=gone)
        for device_id in gone:
            name = names[device_id]
            if client.dry_run:
                rep.note(
                    spec.id,
                    f"dry-run: would stop sharing with {name} "
                    "(no longer subscribes; no file is deleted)",
                )
            else:
                rep.note(
                    spec.id,
                    f"stopped sharing with {name} (no longer subscribes; no file is deleted)",
                )
            entry = next(d for d in (current or {})["devices"] if d["deviceID"] == device_id)
            if device_id in auto_accept or entry.get("introducedBy"):
                rep.warn(
                    f"{spec.id}: {name} may be shared again by the hub's Syncthing "
                    "(auto-accept or an introducer); turn that off for "
                    f"{name} on the hub"
                )
        bootstrapped = False
        if created and spec.bootstrap_ignores is not None:
            bootstrapped = True
            _bootstrap_hub_folder(
                client, spec, timeout=wait_timeout, poll=poll, rep=rep, sleep=sleep, clock=clock
            )
        else:
            apply_ignores(client, spec, current, created, rep, resume_paused=True)
        rep.flush(spec.id)
        if not spec.required_files or bootstrapped:
            if bootstrapped and client.dry_run:
                rep.warn(
                    f"{spec.id}: add {', '.join(spec.required_files)} under {spec.path}, "
                    "then run `warpzone check`"
                )
            continue
        if created:
            rep.warn(
                f"{spec.id}: add {', '.join(spec.required_files)} under {spec.path}, "
                "then run `warpzone check`"
            )
        elif not client.dry_run:
            missing = _missing_on(client, spec)
            if missing:
                rep.warn(f"{spec.id}: missing in {spec.path}: {', '.join(missing)}")
    for text in unclaimed(specs, existing, names):
        rep.warn(text)


@dataclass(frozen=True)
class AdditivePlan:
    """What an additive run does, decided from the specs and the hub's folders."""

    create: tuple[FolderSpec, ...]  # missing and not whitelisted: the only ones it makes
    whitelisted: tuple[FolderSpec, ...]  # missing and whitelisted: a person makes these
    differences: tuple[str, ...]  # folders the hub has that differ from the config; left alone
    clashes: tuple[str, ...] = ()  # missing, but another hub folder is at that path: not made
    paused: tuple[str, ...] = ()  # the hub has them paused (a creation that stopped half-way?)


def _path_key(path: str) -> str:
    """A folder path as compared for clashes: no trailing slash, any case."""
    return posixpath.normpath(path).casefold() if path else ""


def additive_plan(
    specs: list[FolderSpec],
    existing: dict[str, dict[str, Any]],
    names: dict[str, str],
    only: Collection[str] | None = None,
) -> AdditivePlan:
    """Which hub folders an additive run creates (pure).

    Only a folder that is not on the hub and is not whitelisted is created. A folder the hub
    has is never touched, whatever it looks like: its differences are only reported. `names`
    maps a device ID to its name, for the report; `only` limits the run to those folder IDs.
    A missing folder whose path another hub folder already has (compared without a trailing
    slash and in any case) is not created either: two folders on one directory would each
    sync it their own way.
    """
    create: list[FolderSpec] = []
    whitelisted: list[FolderSpec] = []
    differences: list[str] = []
    clashes: list[str] = []
    paused: list[str] = []
    taken = {_path_key(f.get("path", "")): fid for fid, f in existing.items() if f.get("path")}
    for spec in specs:
        if not spec.id.startswith(PREFIX) or (only is not None and spec.id not in only):
            continue
        current = existing.get(spec.id)
        if current is None:
            if spec.bootstrap_ignores is not None:
                whitelisted.append(spec)
                continue
            other = taken.get(_path_key(spec.path))
            if other is not None:
                clashes.append(
                    f"{spec.id}: not created, the hub's folder {other!r} is already at "
                    f"{spec.path!r}"
                )
                continue
            taken[_path_key(spec.path)] = spec.id
            create.append(spec)
            continue
        if current.get("paused"):
            paused.append(spec.id)
        if not same_path(current.get("path", ""), spec.path):
            differences.append(
                f"{spec.id}: the hub has it at {current.get('path')!r}, not {spec.path!r}"
            )
        if current.get("type") != spec.type:
            differences.append(
                f"{spec.id}: the hub has type {current.get('type')!r}, not {spec.type!r}"
            )
        shared = {d["deviceID"] for d in current.get("devices") or []}
        lacking = [names.get(d, d[:7]) for d in spec.devices if d not in shared]
        if lacking:
            differences.append(f"{spec.id}: not shared with {', '.join(lacking)} on the hub")
        extra = [names[d] for d in unsubscribed(spec, current, names)]
        if extra:
            differences.append(
                f"{spec.id}: still shared on the hub with {', '.join(extra)} "
                "(no longer subscribed in the config)"
            )
    return AdditivePlan(
        tuple(create), tuple(whitelisted), tuple(differences), tuple(clashes), tuple(paused)
    )


def provision_hub_additive(
    cfg: Config,
    profiles: dict[str, Profile],
    client: Client,
    rep: Reporter,
    *,
    only: Collection[str] | None = None,
    report_whitelisted: bool = True,
) -> list[str]:
    """Create the `wz-*` folders the hub lacks, and change nothing else on it.

    Used by `provision hub --additive` and by a device with `auto_hub = true`. A folder the
    hub already has is never patched (so a console with an old config or channel reverts
    nothing); a whitelisted folder is left to a person. The hub's folders are read again right
    before each creation, and the API must answer as the hub. A new folder is created paused,
    gets its ignores, and is resumed; if that fails the folder stays paused and a warning
    says so (a later run warns about it too). Returns the IDs it created.
    """
    actual = client.my_id()
    if actual != cfg.hub.device_id:
        raise ProvisionError(
            f"the API at {cfg.hub.api_url} belongs to device {actual[:7]}..., "
            f"but hub.device_id starts with {cfg.hub.device_id[:7]}..."
        )
    devices = {d.device_id: d for d in cfg.devices.values()}
    plan = additive_plan(
        hub_specs(cfg, profiles),
        {f["id"]: f for f in client.folders()},
        {i: d.name for i, d in devices.items()},
        only,
    )
    for text in plan.differences:
        rep.warn(f"{text} (left as it is; `warpzone provision hub` changes it)")
    for text in plan.clashes:
        rep.warn(text)
    for folder_id in plan.paused:
        rep.warn(
            f"{folder_id}: paused on the hub (left paused; a creation that stopped half-way "
            "looks like this, `warpzone provision hub` finishes it)"
        )
    if report_whitelisted:
        for spec in plan.whitelisted:
            rep.warn(
                f"{spec.id} is not on the hub: whitelisted folders are made by "
                "`warpzone provision hub`, not by an additive run"
            )
    created: list[str] = []
    for spec in plan.create:
        known = {d["deviceID"] for d in client.devices()}
        for device_id in spec.devices:
            device = devices[device_id]
            ensure_device(client, device_id, device.name, device.addresses, known, rep)
        # The last look before writing, after the device writes: POST /rest/config/folders
        # replaces a folder with the same ID, and someone may have made it since the list
        # was read.
        if any(f["id"] == spec.id for f in client.folders()):
            rep.out(f"{spec.id}: already on the hub, left as it is")
            continue
        upsert_folder(client, spec, None, rep)
        try:
            apply_ignores(client, spec, None, True, rep)
        except SyncthingError as exc:
            # The folder exists but is still paused: say so once, and keep its half-written
            # notes out of any later line for this ID.
            rep.discard(spec.id)
            rep.warn(
                f"{spec.id}: created on the hub but not finished ({exc}); it stays paused, "
                "run `warpzone provision hub` to finish it"
            )
            continue
        rep.flush(spec.id)
        created.append(spec.id)
    if not plan.create:
        rep.out("hub: no folder to create")
    return created


def check_hub(cfg: Config, profiles: dict[str, Profile], client: Client, rep: Reporter) -> int:
    """Report whitelist files missing on the hub. Returns the number of problems."""
    problems = 0
    for spec in hub_specs(cfg, profiles):
        if not spec.required_files:
            continue
        try:
            missing = _missing_on(client, spec)
        except SyncthingError as exc:
            if exc.status is None:
                raise
            rep.out(f"{spec.id}: not configured on the hub yet (run `warpzone provision hub`)")
            problems += 1
            continue
        if missing:
            problems += len(missing)
            rep.out(f"{spec.id}: MISSING {', '.join(missing)} (in {spec.path})")
        else:
            rep.out(f"{spec.id}: whitelists present")
    return problems
