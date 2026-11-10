"""Device provisioning: the subscribed folders, then everything the device needs around them."""

from __future__ import annotations

import dataclasses
import os
import subprocess
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from warpzone import flatpak, settings_state
from warpzone.config import DEFAULT_WAIT_SECONDS, Config
from warpzone.model import FolderSpec, device_specs
from warpzone.paths import expand_home
from warpzone.probes import REAL_PROBES, DeviceProbes
from warpzone.profiles import Profile
from warpzone.provision_artwork import (
    artwork_layout,
    artwork_scope,
    ensure_gamelists,
    no_scope_warning,
)
from warpzone.provision_emulators import (
    ensure_cemu_paths,
    ensure_retroarch_pins,
    ensure_shadps4_dirs,
    ensure_shadps4_launch,
    ensure_systems_override,
    ensure_title_links,
)
from warpzone.provision_folders import (
    ProvisionError,
    apply_ignores,
    can_move,
    ensure_device,
    in_sync,
    move_refusal,
    rescan,
    unattended_refusals,
    unpause,
    upsert_folder,
    wait_for_files,
    wait_pending,
)
from warpzone.provision_hub import provision_hub_additive
from warpzone.provision_scripts import (
    ensure_esde_script,
    ensure_links_script,
    ensure_options_menu,
    ensure_play_state_script,
)
from warpzone.provision_timers import (
    ensure_artwork_timer,
    ensure_play_state_timer,
    ensure_prune_timer,
)
from warpzone.provision_updater import UPDATER_SOURCE, ensure_self_update
from warpzone.report import Reporter
from warpzone.syncthing import Client, SyncthingError


def _hub_precheck(
    hub: Client, specs: list[FolderSpec], rep: Reporter, new: frozenset[str] = frozenset()
) -> set[str]:
    """Fail early if a whitelist is missing on the hub (skipped if the hub is unreachable).

    A folder named in `new` (not in this device's Syncthing yet) is not an error: it is
    warned about and its id is returned, for the caller to leave out. That is what an
    unattended run does when the config gained a folder the hub does not have yet.
    """
    skipped: set[str] = set()
    for spec in specs:
        if spec.bootstrap_ignores is None:
            continue
        try:
            names = set(hub.browse(spec.id))
        except SyncthingError as exc:
            if exc.status is None:
                rep.warn(f"hub API unreachable, skipping the whitelist pre-check ({exc})")
                return skipped
            if spec.id in new:
                rep.warn(
                    f"{spec.id} is not configured on the hub (run `warpzone provision hub`), "
                    "so the new folder was skipped"
                )
                skipped.add(spec.id)
                continue
            raise ProvisionError(
                f"{spec.id} is not configured on the hub: run `warpzone provision hub` first"
            ) from exc
        missing = [f for f in spec.required_files if f not in names]
        if missing and spec.id in new:
            rep.warn(
                f"{spec.id}: {', '.join(missing)} not found on the hub, so the new folder was "
                "skipped until they are there"
            )
            skipped.add(spec.id)
        elif missing:
            raise ProvisionError(
                f"{spec.id}: {', '.join(missing)} not found on the hub. Create it in the "
                "folder on the hub (see templates/), let the hub scan, and try again. "
                "Nothing was changed on this device."
            )
    return skipped


def _auto_hub(
    cfg: Config,
    profiles: dict[str, Profile],
    hub: Client,
    folder_ids: set[str],
    rep: Reporter,
    *,
    dry_run: bool,
) -> None:
    """`auto_hub`: create on the hub the folders this run needs and it lacks.

    Anything that goes wrong here is a warning, never a failure: the hub may be down or the
    key may be wrong, and a console must not roll back its release for that. The rest of the
    run then sees the hub as it is (a missing whitelisted folder is still reported by the
    pre-check). A `--dry-run` of the device never writes to the hub.
    """
    if dry_run and not hub.dry_run:
        rep.out("auto_hub: dry-run: the hub is not asked to create anything")
        return
    try:
        provision_hub_additive(cfg, profiles, hub, rep, only=folder_ids, report_whitelisted=False)
    except (SyncthingError, ProvisionError) as exc:
        rep.warn(f"auto_hub: {exc}; the hub was not changed beyond what is reported above")


def _missing_markers(
    specs: Iterable[FolderSpec], existing: dict[str, dict[str, Any]], moving: set[str]
) -> list[str]:
    """Folders Syncthing has whose marker is not on disk: the card is not mounted.

    A folder about to move has no marker at its new path yet."""
    problems = []
    for spec in specs:
        current = existing.get(spec.id)
        if current is None or spec.id in moving:
            continue
        marker = current.get("markerName") or ".stfolder"
        if not os.path.exists(os.path.join(spec.path, marker)):
            problems.append(f"{spec.id}: {marker} is not in {spec.path} (is the card mounted?)")
    return problems


def _provision_whitelisted(
    client: Client,
    spec: FolderSpec,
    current: dict[str, Any] | None,
    created: bool,
    *,
    timeout: float,
    poll: float,
    rep: Reporter,
    probes: DeviceProbes,
    unattended: bool = False,
) -> None:
    """Bring a whitelisted device folder to its final ignores.

    A new folder starts on the bootstrap ignores and waits for the whitelists. Unattended, a
    folder that already exists never goes through the bootstrap (its `*` would hide a
    populated library from Syncthing if the hub could not be reached to deliver the lists):
    it takes the final ignores when the lists are on disk and is otherwise left alone. A
    new folder whose lists do not arrive in time stays on the bootstrap, with a warning
    instead of an error.
    """
    assert spec.bootstrap_ignores is not None
    final, boot = list(spec.ignores), list(spec.bootstrap_ignores)
    present = [] if created else client.get_ignores(spec.id)
    if present == final:
        rep.note(spec.id, "ignores already final")
        return
    if unattended and not created:
        absent = [f for f in spec.required_files if not os.path.isfile(os.path.join(spec.path, f))]
        if absent:
            rep.warn(
                f"{spec.id}: its ignores differ from the final ones, but {', '.join(absent)} "
                f"is not in {spec.path}, so the folder was left alone; the next run finishes it "
                "once the file has arrived"
            )
            return
        client.set_ignores(spec.id, final)
        if current is not None and current.get("paused"):
            unpause(client, spec, rep)
        rep.note(spec.id, f"final ignores set (whitelists on disk), {rescan(client, spec.id)}")
        return
    if present != boot:
        client.set_ignores(spec.id, boot)
        rep.note(spec.id, "bootstrap ignores set")
    if created or (current is not None and current.get("paused")):
        unpause(client, spec, rep)
    if client.dry_run:
        rep.note(spec.id, f"dry-run: would wait up to {timeout:.0f}s for the whitelists")
    elif unattended:
        pending = wait_pending(
            client,
            spec.id,
            spec.required_files,
            in_sync,
            timeout=timeout,
            poll=poll,
            rep=rep,
            sleep=probes.sleep,
            clock=probes.clock,
        )
        if pending:
            rep.warn(
                f"{spec.id}: {', '.join(pending)} did not arrive within {timeout:.0f}s; the "
                "folder stays on the bootstrap ignores (only whitelists can arrive) and the "
                "next run finishes it"
            )
            return
    else:
        wait_for_files(
            client,
            spec,
            timeout=timeout,
            poll=poll,
            rep=rep,
            sleep=probes.sleep,
            clock=probes.clock,
        )
    client.set_ignores(spec.id, final)
    rep.note(spec.id, f"whitelist active, {rescan(client, spec.id)}")


def _create_target(spec: FolderSpec, dry_run: bool, rep: Reporter) -> bool:
    """Create a missing target directory when its emulator is installed.

    Only for entries that name an installed-marker directory (`create_when`):
    the emulator exists but never wrote this folder, and the folder is where
    the files must go. Everything else is skipped, never created.
    """
    if not spec.create_when or not os.path.isdir(spec.create_when):
        return False
    if dry_run:
        rep.out(f"{spec.id}: dry-run: would create {spec.path}")
    else:
        os.makedirs(spec.path, exist_ok=True)
        rep.note(spec.id, f"created {spec.path}")
    return True


RESTART_SYNCTHING = (
    "restart Syncthing for this to take effect (`systemctl --user restart syncthing`, or "
    "quit and reopen SyncThingy)"
)


def ensure_flatpak_access(
    specs: Iterable[FolderSpec],
    config_path: Path | None,
    home: Path,
    rep: Reporter,
    *,
    dry_run: bool,
    probes: DeviceProbes = REAL_PROBES,
    unattended: bool = False,
) -> None:
    """Grant the SyncThingy flatpak the data folders of the emulators it must sync.

    A flatpak cannot see `~/.var/app/<app>` of other apps, even with the `host`
    filesystem, so a save folder there looks empty to Syncthing. Does nothing unless the
    local Syncthing is that flatpak (its `config.xml` is in the flatpak's own folder).
    Only the overrides that are missing are added, so a second run changes nothing. An
    unattended run grants nothing: the grant needs a Syncthing restart that nobody is there
    to do, so it only warns with the command.
    """
    if config_path is None or not flatpak.is_syncthingy_config(config_path, home):
        return
    apps = flatpak.app_ids((s.path for s in specs), home)
    if not apps:
        return
    manual = " ".join(flatpak.override_command(apps))
    try:
        code, shown = probes.flatpak(flatpak.show_command())
        if code != 0:
            raise OSError(shown.strip() or f"exit code {code}")
    except (OSError, subprocess.SubprocessError) as exc:
        rep.warn(
            f"cannot read the flatpak overrides ({exc}); Syncthing may not see the emulators' "
            f"save folders. Run `{manual}`, then restart Syncthing"
        )
        return
    missing = flatpak.missing_apps(apps, shown, home)
    if not missing:
        rep.out(f"syncthing flatpak: access to {len(apps)} emulator folder(s) already granted")
        return
    names = ", ".join(flatpak.app_dir(a) for a in missing)
    command = flatpak.override_command(missing)
    if unattended:
        rep.warn(
            f"Syncthing may not see the save folders of {names}: an unattended run does not "
            f"grant access, because that needs a restart. Run `{' '.join(command)}`, then "
            f"{RESTART_SYNCTHING}"
        )
        return
    if dry_run:
        rep.out(f"syncthing flatpak: dry-run: would grant {names} with: {' '.join(command)}")
        rep.out(f"syncthing flatpak: dry-run: then you would need to {RESTART_SYNCTHING}")
        return
    try:
        code, out = probes.flatpak(command)
        if code != 0:
            raise OSError(out.strip() or f"exit code {code}")
    except (OSError, subprocess.SubprocessError) as exc:
        rep.warn(f"cannot grant Syncthing access to {names} ({exc}). Run `{' '.join(command)}`")
        return
    rep.out(f"syncthing flatpak: granted {names}")
    rep.warn(RESTART_SYNCTHING)


def provision_device(
    cfg: Config,
    name: str,
    profile: Profile,
    local: Client,
    hub: Client | None,
    rep: Reporter,
    *,
    home: Path,
    wait_timeout: float = DEFAULT_WAIT_SECONDS,
    poll: float = 2.0,
    syncthing_config: Path | None = None,
    python: str | None = None,
    probes: DeviceProbes = REAL_PROBES,
    state_root: Path | None = None,
    config_path: Path | None = None,
    unattended: bool = False,
    clone: Path | None = None,
    updater_source: Path = UPDATER_SOURCE,
    profiles: dict[str, Profile] | None = None,
) -> None:
    device = cfg.devices[name]
    actual = local.my_id()
    if actual != device.device_id:
        raise ProvisionError(
            f"the local Syncthing is device {actual[:7]}..., but devices.{name}.device_id "
            f"starts with {device.device_id[:7]}..."
        )

    with_artwork = profile.artwork is not None and "frontend" in device.folders
    scope = artwork_scope(profile, home) if with_artwork else None
    specs = [
        dataclasses.replace(
            s,
            path=expand_home(s.path, home),
            create_when=expand_home(s.create_when, home) if s.create_when else None,
            previous_paths=tuple(expand_home(p, home) for p in s.previous_paths),
        )
        for s in device_specs(cfg, name, profile, scope)
    ]
    existing = {f["id"]: f for f in local.folders()}

    # Folders Syncthing has at a path the profile retired, whose old directory is empty or
    # whose files are all on the hub: they are removed from Syncthing (no file is touched)
    # and added again at the new path. Decided over the folders whose directory exists.
    def movable(candidates: Iterable[FolderSpec]) -> set[str]:
        return {
            s.id
            for s in candidates
            if s.id in existing
            and not (s.skip_if_missing and not os.path.isdir(s.path))
            and can_move(local, s, existing[s.id])
        }

    moving: set[str] = set()
    if unattended:
        present = [s for s in specs if not (s.skip_if_missing and not os.path.isdir(s.path))]
        moving = movable(present)
        problems = [
            problem
            for spec in present
            if spec.id in existing
            for problem in unattended_refusals(spec, existing[spec.id], moving=spec.id in moving)
        ] + _missing_markers(present, existing, moving)
        if problems:
            raise ProvisionError(
                "an unattended run does not change these folders, so nothing was changed. "
                f"Fix them and run `warpzone provision device {name}` by hand:\n  "
                + "\n  ".join(problems)
            )
    todo: list[FolderSpec] = []
    for spec in specs:
        if spec.skip_if_missing and not os.path.isdir(spec.path):
            # An unattended run does not make the directory of a folder Syncthing already has.
            may_create = not (unattended and spec.id in existing)
            if may_create and _create_target(spec, local.dry_run, rep):
                todo.append(spec)
            else:
                rep.warn(
                    f"{spec.id}: skipped, {spec.path} does not exist yet "
                    "(open the emulator once, then run provision again)"
                )
        else:
            todo.append(spec)

    if not unattended:
        # Directories were made just above, so a folder whose new directory is new can move.
        moving = movable(todo)

    if hub is not None and device.auto_hub and profiles is not None:
        _auto_hub(cfg, profiles, hub, {s.id for s in todo}, rep, dry_run=local.dry_run)

    if hub is not None:
        new = frozenset(s.id for s in todo if s.id not in existing) if unattended else frozenset()
        skipped = _hub_precheck(hub, todo, rep, new)
        todo = [s for s in todo if s.id not in skipped]

    ensure_flatpak_access(
        todo,
        syncthing_config,
        home,
        rep,
        dry_run=local.dry_run,
        probes=probes,
        unattended=unattended,
    )

    known = {d["deviceID"] for d in local.devices()}
    ensure_device(local, cfg.hub.device_id, cfg.hub.name, cfg.hub.addresses, known, rep)

    for spec in todo:
        current = existing.get(spec.id)
        if current is not None and spec.id in moving:
            # Look again right before acting: whitelist waits and the other folders came
            # between the decision and now, and files may have been written meanwhile.
            if not can_move(local, spec, current):
                raise ProvisionError(move_refusal(spec, current))
            rep.note(
                spec.id,
                f"moves from {current.get('path')} to {spec.path}: removed from Syncthing, no "
                "file touched, added again at the new path (files already on the hub come "
                "back into it; the old directory is left as it is)",
            )
            local.remove_folder(spec.id)
            current = None
        created = upsert_folder(local, spec, current, rep)
        if spec.bootstrap_ignores is None:
            apply_ignores(local, spec, current, created, rep)
        else:
            _provision_whitelisted(
                local,
                spec,
                current,
                created,
                timeout=wait_timeout,
                poll=poll,
                rep=rep,
                probes=probes,
                unattended=unattended,
            )
        rep.flush(spec.id)

    if "library" in device.folders:
        root = state_root if state_root is not None else settings_state.state_dir(home=home)
        ensure_systems_override(
            name,
            profile,
            home,
            rep,
            dry_run=local.dry_run,
            probes=probes,
            state_root=root,
            python=python,
            config_path=config_path,
            saves="saves" in device.folders,
        )
        for ensure in (
            ensure_retroarch_pins,
            ensure_shadps4_dirs,
            ensure_shadps4_launch,
            ensure_cemu_paths,
            ensure_title_links,
        ):
            ensure(name, profile, home, rep, dry_run=local.dry_run, probes=probes, state_root=root)

    if with_artwork:
        layout = artwork_layout(profile, home)
        if layout is not None and not scope:
            rep.warn(no_scope_warning(name, layout))
        ensure_gamelists(
            name,
            profile,
            home,
            rep,
            dry_run=local.dry_run,
            probes=probes,
            play_state="play-state" in device.folders,
        )

    # The refresh timer also converts saves, so a device that only syncs saves (no `frontend`)
    # gets it too, when the profile has an `[artwork] refresh` interval to run it at.
    if with_artwork or profile.converts_saves(device.folders):
        ensure_artwork_timer(
            name,
            profile,
            home,
            rep,
            dry_run=local.dry_run,
            python=python,
            config_path=config_path,
            probes=probes,
        )

    if "settings" in device.folders:
        ensure_esde_script(
            name,
            profile,
            home,
            rep,
            dry_run=local.dry_run,
            python=python,
            probes=probes,
            state_root=state_root,
            config_path=config_path,
        )

    if "library" in device.folders:
        ensure_links_script(
            name,
            profile,
            home,
            rep,
            dry_run=local.dry_run,
            python=python,
            probes=probes,
            state_root=state_root,
            config_path=config_path,
        )

    if "play-state" in device.folders:
        ensure_play_state_script(
            name,
            profile,
            home,
            rep,
            dry_run=local.dry_run,
            python=python,
            probes=probes,
            state_root=state_root,
            config_path=config_path,
        )
        ensure_play_state_timer(
            name,
            profile,
            home,
            rep,
            dry_run=local.dry_run,
            python=python,
            config_path=config_path,
            probes=probes,
        )

    ensure_prune_timer(
        name,
        device,
        home,
        rep,
        dry_run=local.dry_run,
        python=python,
        config_path=config_path,
        probes=probes,
    )

    ensure_options_menu(profile, home, rep, dry_run=local.dry_run)

    ensure_self_update(
        device,
        profile,
        home,
        rep,
        dry_run=local.dry_run,
        python=python,
        clone=clone,
        config_path=config_path,
        state_root=state_root,
        probes=probes,
        updater_source=updater_source,
    )
