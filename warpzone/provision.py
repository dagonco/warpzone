"""Hub and device provisioning: diff desired state against Syncthing, then call."""

from __future__ import annotations

import dataclasses
import os
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable, Iterable, Sequence
from pathlib import Path
from typing import Any

from warpzone import (
    artwork,
    esde,
    flatpak,
    frontend,
    playstate_device,
    selfupdate,
    settings_state,
    shadps4,
    titlelinks,
    updatetimer,
)
from warpzone.config import DEFAULT_WAIT_SECONDS, HUB_WAIT_SECONDS, Config, Device
from warpzone.errors import WarpzoneError
from warpzone.model import ESDE_ID, MEDIA_ID, FolderSpec, device_specs, hub_specs
from warpzone.paths import expand_home
from warpzone.profiles import Profile
from warpzone.report import Reporter
from warpzone.runners import (
    FlatpakRunner,
    ImportCheck,
    SystemctlRunner,
    run_flatpak,
    run_import_check,
    run_script_check,
    run_systemctl,
    run_user_systemctl,
)
from warpzone.syncthing import Client, SyncthingError

PREFIX = "wz-"


class ProvisionError(WarpzoneError):
    pass


def _same_path(a: str, b: str) -> bool:
    return (a.rstrip("/") or "/") == (b.rstrip("/") or "/")


def _subset(wanted: Any, actual: Any) -> bool:
    """True if every key in `wanted` has the same value in `actual` (recursively)."""
    if isinstance(wanted, dict):
        return isinstance(actual, dict) and all(
            k in actual and _subset(v, actual[k]) for k, v in wanted.items()
        )
    return bool(wanted == actual)


def _folder_patch(spec: FolderSpec, current: dict[str, Any]) -> dict[str, Any]:
    """The owned keys that differ. Devices are only ever added, never removed."""
    body = spec.body()
    owned = ("label", "type", "fsWatcherEnabled", "rescanIntervalS", "ignorePerms")
    patch: dict[str, Any] = {
        key: body[key] for key in owned if key in body and current.get(key) != body[key]
    }
    if not _subset(spec.versioning, current.get("versioning") or {}):
        patch["versioning"] = spec.versioning
    have = list(current.get("devices") or [])
    known = {d["deviceID"] for d in have}
    missing = [d for d in body["devices"] if d["deviceID"] not in known]
    if missing:
        patch["devices"] = have + missing
    return patch


def _ensure_device(
    client: Client,
    device_id: str,
    name: str,
    addresses: tuple[str, ...],
    known: set[str],
    rep: Reporter,
) -> None:
    if device_id in known:
        rep.out(f"device {name}: already known")
        return
    client.add_device(
        {
            "deviceID": device_id,
            "name": name,
            "addresses": list(addresses),
            "autoAcceptFolders": False,
        }
    )
    known.add(device_id)
    rep.out(f"device {name}: added")


def _upsert_folder(
    client: Client, spec: FolderSpec, current: dict[str, Any] | None, rep: Reporter
) -> bool:
    """Create (paused) or patch the folder. Returns True if it was created."""
    if current is None:
        client.add_folder({**spec.body(), "paused": True})
        rep.note(spec.id, f"created at {spec.path}")
        return True
    if not _same_path(current.get("path", ""), spec.path):
        raise ProvisionError(
            f"{spec.id}: Syncthing has it at {current.get('path')!r} but the config says "
            f"{spec.path!r}. Changing a folder path would look like a mass delete, so "
            "warpzone refuses. Remove the folder in Syncthing first (files stay on disk), "
            "then run provision again."
        )
    patch = _folder_patch(spec, current)
    if patch:
        client.patch_folder(spec.id, patch)
        rep.note(spec.id, f"updated {', '.join(sorted(patch))}")
    return False


def _unpause(client: Client, spec: FolderSpec, rep: Reporter) -> None:
    client.patch_folder(spec.id, {"paused": False})
    rep.note(spec.id, "resumed")


def _apply_ignores(
    client: Client, spec: FolderSpec, current: dict[str, Any] | None, created: bool, rep: Reporter
) -> None:
    """Set the final ignores directly (no bootstrap) and make sure the folder runs."""
    wanted = list(spec.ignores)
    changed = created or client.get_ignores(spec.id) != wanted
    if changed:
        client.set_ignores(spec.id, wanted)
        rep.note(spec.id, "ignores set")
    if created or (changed and current is not None and current.get("paused")):
        _unpause(client, spec, rep)


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
    _unpause(client, spec, rep)
    client.scan(spec.id)
    if client.dry_run:
        rep.note(spec.id, f"dry-run: would wait up to {timeout:.0f}s for the whitelists")
        pending: list[str] = []
    else:
        pending = _wait_pending(
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
    client.scan(spec.id)
    rep.note(spec.id, "final ignores set, full scan started")
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
    known = {d["deviceID"] for d in client.devices()}
    for device in cfg.devices.values():
        _ensure_device(client, device.device_id, device.name, device.addresses, known, rep)

    existing = {f["id"]: f for f in client.folders()}
    for spec in hub_specs(cfg, profiles):
        current = existing.get(spec.id)
        created = _upsert_folder(client, spec, current, rep)
        bootstrapped = False
        if created and spec.bootstrap_ignores is not None:
            bootstrapped = True
            _bootstrap_hub_folder(
                client, spec, timeout=wait_timeout, poll=poll, rep=rep, sleep=sleep, clock=clock
            )
        else:
            _apply_ignores(client, spec, current, created, rep)
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


def in_sync(info: dict[str, Any] | None) -> bool:
    """True when the local copy of a file is exactly the global one."""
    if not info:
        return False
    local, global_ = info.get("local"), info.get("global")
    return bool(
        local
        and global_
        and not local.get("deleted")
        and not local.get("invalid")
        and local.get("version") == global_.get("version")
    )


def in_index(info: dict[str, Any] | None) -> bool:
    """True when this machine's index holds a live (not deleted, not invalid) copy."""
    local = (info or {}).get("local")
    return bool(local and not local.get("deleted") and not local.get("invalid"))


def _wait_pending(
    client: Client,
    folder_id: str,
    files: tuple[str, ...],
    ready: Callable[[dict[str, Any] | None], bool],
    *,
    timeout: float,
    poll: float,
    rep: Reporter,
    sleep: Callable[[float], None],
    clock: Callable[[], float],
) -> list[str]:
    """Poll until every file is `ready`; returns the ones still pending at the deadline."""
    deadline = clock() + timeout
    pending = list(files)
    announced = False
    while True:
        pending = [f for f in pending if not ready(client.db_file(folder_id, f))]
        if not pending or clock() >= deadline:
            return pending
        if not announced:
            rep.out(f"{folder_id}: waiting for {', '.join(pending)} (up to {timeout:.0f}s)")
            announced = True
        sleep(poll)


def wait_for_files(
    client: Client,
    spec: FolderSpec,
    *,
    timeout: float,
    poll: float,
    rep: Reporter,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> None:
    pending = _wait_pending(
        client,
        spec.id,
        spec.required_files,
        in_sync,
        timeout=timeout,
        poll=poll,
        rep=rep,
        sleep=sleep,
        clock=clock,
    )
    if pending:
        raise ProvisionError(
            f"{spec.id}: {', '.join(pending)} did not arrive within {timeout:.0f}s. "
            "The folder stays on the safe bootstrap ignores (only whitelists can arrive). "
            "Check that the hub is online and has these files, then run provision again."
        )


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


def unattended_refusals(spec: FolderSpec, current: dict[str, Any]) -> list[str]:
    """Why an unattended run must not touch a folder Syncthing already has (pure).

    `type` decides what a folder does with changes: `receiveonly` to `sendreceive` would send
    a console's deletions to the hub. `versioning` decides what Syncthing keeps. Neither is
    ever changed without a person, and neither is a failure a rollback could fix.
    """
    problems = []
    patch = _folder_patch(spec, current)
    if "type" in patch:
        problems.append(
            f"{spec.id}: type is {current.get('type')!r}, the config says {spec.type!r}"
        )
    if "versioning" in patch:
        problems.append(f"{spec.id}: versioning differs from the config")
    if not _same_path(current.get("path", ""), spec.path):
        problems.append(
            f"{spec.id}: Syncthing has it at {current.get('path')!r}, not {spec.path!r}"
        )
    return problems


def _missing_markers(specs: Iterable[FolderSpec], existing: dict[str, dict[str, Any]]) -> list[str]:
    """Folders Syncthing has whose marker is not on disk: the card is not mounted."""
    problems = []
    for spec in specs:
        current = existing.get(spec.id)
        if current is None:
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
    sleep: Callable[[float], None],
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
            _unpause(client, spec, rep)
        client.scan(spec.id)
        rep.note(spec.id, "final ignores set (whitelists on disk), rescanning")
        return
    if present != boot:
        client.set_ignores(spec.id, boot)
        rep.note(spec.id, "bootstrap ignores set")
    if created or (current is not None and current.get("paused")):
        _unpause(client, spec, rep)
    if client.dry_run:
        rep.note(spec.id, f"dry-run: would wait up to {timeout:.0f}s for the whitelists")
    elif unattended:
        pending = _wait_pending(
            client,
            spec.id,
            spec.required_files,
            in_sync,
            timeout=timeout,
            poll=poll,
            rep=rep,
            sleep=sleep,
            clock=time.monotonic,
        )
        if pending:
            rep.warn(
                f"{spec.id}: {', '.join(pending)} did not arrive within {timeout:.0f}s; the "
                "folder stays on the bootstrap ignores (only whitelists can arrive) and the "
                "next run finishes it"
            )
            return
    else:
        wait_for_files(client, spec, timeout=timeout, poll=poll, rep=rep, sleep=sleep)
    client.set_ignores(spec.id, final)
    client.scan(spec.id)
    rep.note(spec.id, "whitelist active, rescanning")


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
    run: FlatpakRunner = run_flatpak,
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
        code, shown = run(flatpak.show_command())
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
        code, out = run(command)
        if code != 0:
            raise OSError(out.strip() or f"exit code {code}")
    except (OSError, subprocess.SubprocessError) as exc:
        rep.warn(f"cannot grant Syncthing access to {names} ({exc}). Run `{' '.join(command)}`")
        return
    rep.out(f"syncthing flatpak: granted {names}")
    rep.warn(RESTART_SYNCTHING)


def _write_script(path: Path, text: str, mode: int = 0o755) -> None:
    """Write a file (executable by default) atomically, creating its directories."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}{settings_state.TMP_SUFFIX}")
    tmp.unlink(missing_ok=True)
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
    try:
        with os.fdopen(fd, "wb") as fh:
            os.fchmod(fh.fileno(), mode)
            fh.write(text.encode())
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def _enable_event_scripts(
    name: str,
    esde_dir: Path,
    rep: Reporter,
    *,
    dry_run: bool,
    proc: Path,
    state_root: Path,
) -> None:
    """Turn ES-DE's `CustomEventScripts` on, only while ES-DE is not running.

    ES-DE keeps its settings in memory and rewrites the file when it quits, so an edit made
    while it runs would be lost. The old file is backed up and the new one written
    atomically, refusing when the file changed after it was read.
    """
    path = esde.settings_path(esde_dir)
    again = f"quit ES-DE and run `warpzone provision device {name}` again"
    by_hand = f"or turn it on in ES-DE ({esde.MENU_PATH})"
    if not path.is_file():
        rep.warn(
            f"{path} does not exist, so ES-DE's custom event scripts were not turned on: "
            f"start ES-DE once, then run `warpzone provision device {name}` again ({by_hand})"
        )
        return
    try:
        before = settings_state.stamp(path)
        text = path.read_bytes().decode("utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        rep.warn(f"cannot read {path} ({exc}); {by_hand}")
        return
    if esde.event_scripts_enabled(text) is True:
        rep.out("es-de: custom event scripts already on")
        return

    def running() -> bool | None:
        try:
            return settings_state.running(esde.ESDE_PROCESSES, proc=proc)
        except settings_state.NoProc:
            return None

    def refuse(state: bool | None) -> bool:
        if state is None:
            rep.warn(
                "cannot tell whether ES-DE is running (no /proc), so its custom event scripts "
                f"were not turned on: {again} on a system with /proc ({by_hand})"
            )
        elif state:
            rep.defer()
            rep.warn(
                f"ES-DE is running, so its custom event scripts were not turned on (it "
                f"rewrites {path.name} when it quits): {again}"
            )
        return state is not False

    if refuse(running()):
        return
    try:
        new = esde.enable_event_scripts(text)
    except esde.EsdeError as exc:
        rep.warn(f"{path} was left alone: {exc}; {by_hand}")
        return
    if dry_run:
        rep.out(f"es-de: dry-run: would set CustomEventScripts to true in {path}")
        return
    settings_state.backup(path, "esde", "es_settings", root=state_root)
    if refuse(running()):  # ES-DE may have started while the backup was made
        return
    if not settings_state.atomic_write(path, new.encode(), expect=before):
        rep.warn(f"{path} changed while it was being edited, so it was left alone: run again")
        return
    rep.out(f"es-de: set CustomEventScripts to true in {path} (old file backed up)")


def _script_config(config_path: Path | None, home: Path) -> Path | None:
    """The config path the startup script has to pass, or None for the default location."""
    if config_path is None:
        return None
    absolute = Path(os.path.abspath(config_path))
    return None if absolute == home / ".config" / "warpzone" / "config.toml" else absolute


def ensure_esde_script(
    name: str,
    profile: Profile,
    home: Path,
    rep: Reporter,
    *,
    dry_run: bool,
    python: str | None = None,
    import_check: ImportCheck = run_import_check,
    proc: Path = Path("/proc"),
    state_root: Path | None = None,
    config_path: Path | None = None,
) -> None:
    """Install ES-DE's startup script for `settings apply` and turn ES-DE's scripts on.

    The script runs the Python that runs this command, so that interpreter must import
    warpzone from outside the current folder; run from a clone it warns and installs
    nothing. The script is rewritten only when its content differs. Does nothing when the
    profile has no ES-DE folder. `config_path` is the private config file this run used: the
    script names it unless it is the default one, since ES-DE does not have the user's
    environment (`WARPZONE_CONFIG`, `XDG_CONFIG_HOME`).
    """
    if "esde" not in profile.paths:
        return
    python = python or sys.executable
    text = esde.render_startup_script(python, name, _script_config(config_path, home))
    _install_startup_script(
        name,
        profile,
        home,
        rep,
        esde.SCRIPT_NAME,
        text,
        dry_run=dry_run,
        python=python,
        import_check=import_check,
        proc=proc,
        state_root=state_root,
    )


def ensure_play_state_script(
    name: str,
    profile: Profile,
    home: Path,
    rep: Reporter,
    *,
    dry_run: bool,
    python: str | None = None,
    import_check: ImportCheck = run_import_check,
    proc: Path = Path("/proc"),
    state_root: Path | None = None,
    config_path: Path | None = None,
) -> None:
    """Install ES-DE's startup script for `play-state sync`, as `ensure_esde_script` does."""
    if "esde" not in profile.paths:
        return
    python = python or sys.executable
    text = esde.render_play_state_script(python, name, _script_config(config_path, home))
    _install_startup_script(
        name,
        profile,
        home,
        rep,
        esde.PLAY_STATE_SCRIPT,
        text,
        dry_run=dry_run,
        python=python,
        import_check=import_check,
        proc=proc,
        state_root=state_root,
    )


def ensure_links_script(
    name: str,
    profile: Profile,
    home: Path,
    rep: Reporter,
    *,
    dry_run: bool,
    python: str | None = None,
    import_check: ImportCheck = run_import_check,
    proc: Path = Path("/proc"),
    state_root: Path | None = None,
    config_path: Path | None = None,
) -> None:
    """Install ES-DE's startup script for `links sync`, as `ensure_esde_script` does."""
    if "esde" not in profile.paths or not profile.links:
        return
    python = python or sys.executable
    text = esde.render_links_script(python, name, _script_config(config_path, home))
    _install_startup_script(
        name,
        profile,
        home,
        rep,
        esde.LINKS_SCRIPT,
        text,
        dry_run=dry_run,
        python=python,
        import_check=import_check,
        proc=proc,
        state_root=state_root,
    )


def _install_startup_script(
    name: str,
    profile: Profile,
    home: Path,
    rep: Reporter,
    script: str,
    text: str,
    *,
    dry_run: bool,
    python: str,
    import_check: ImportCheck,
    proc: Path,
    state_root: Path | None,
) -> None:
    esde_dir = Path(expand_home(profile.paths["esde"], home))
    if not import_check(python):
        rep.warn(
            f"the ES-DE startup script was not installed: {python} cannot import warpzone "
            "from outside the current folder (run from a clone?). Install warpzone for it "
            f"(for example with pipx), then run `warpzone provision device {name}` from there"
        )
        return
    path = esde.script_path(esde_dir, script)
    try:
        current: str | None = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        current = None
    if current == text:
        if not dry_run and not os.access(path, os.X_OK):
            path.chmod(0o755)
            rep.out(f"es-de: made {path} executable")
        else:
            rep.out(f"es-de: script unchanged ({path})")
    elif dry_run:
        rep.out(f"es-de: dry-run: would write {path}:")
        for line in text.splitlines():
            rep.out(f"  {line}")
    else:
        _write_script(path, text)
        rep.out(f"es-de: wrote {path}")
    root = state_root if state_root is not None else settings_state.state_dir(home=home)
    _enable_event_scripts(name, esde_dir, rep, dry_run=dry_run, proc=proc, state_root=root)


UPDATER_SOURCE = selfupdate.UPDATER_SOURCE
# `sh -n` and the script's `selftest` each get this long; the selftest takes a few seconds.
UPDATER_CHECK_SECONDS = 120


def _read_text(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None


def _first_lines(text: str, count: int = 3) -> str:
    return " / ".join([line.strip() for line in text.splitlines() if line.strip()][:count])


def ensure_updater(
    home: Path,
    rep: Reporter,
    *,
    dry_run: bool,
    source: Path = UPDATER_SOURCE,
    run: Callable[[Sequence[str], float], tuple[int, str]] | None = None,
) -> bool:
    """Put `source` (the script that ships with this version) in place as the live copy
    outside the clone, and say whether there is a usable live copy afterwards.

    The copy is written next to the live one and replaces it atomically (a tick that is running
    keeps reading the file it started with), only after `sh -n` and `sh <copy> selftest` pass,
    and the old one stays as `updater.sh.prev`. A copy of a lower `UPDATER_ABI` than the live
    one is never installed: a rollback to an older release must not downgrade the script.
    """
    live = selfupdate.live_path(home)
    check = run or run_script_check
    candidate = _read_text(source)
    if candidate is None:
        rep.warn(f"the updater was not installed: cannot read {source}")
        return live.is_file()
    current = _read_text(live)
    verdict = selfupdate.install_decision(candidate, current)
    if verdict == "invalid":
        rep.warn(f"the updater was not installed: {source} has no UPDATER_ABI line")
        return current is not None
    if verdict == "current":
        rep.out(f"updater: already current ({live})")
        return True
    if verdict == "older":
        have, new = selfupdate.updater_abi(current or ""), selfupdate.updater_abi(candidate)
        rep.out(
            f"updater: keeping {live}: its abi {have} is higher than the abi {new} of this version"
        )
        return True
    if dry_run:
        rep.out(f"updater: dry-run: would install {live}")
        return current is not None

    new_abi = selfupdate.updater_abi(candidate)
    tmp: Path | None = None
    try:
        # A name of its own (two provisions at once must not share a file that one of them
        # has checked and the other has not) in the folder of the live copy, so that the final
        # rename stays on one file system.
        live.parent.mkdir(parents=True, exist_ok=True)
        fd, name = tempfile.mkstemp(dir=live.parent, prefix=f".{live.name}.", suffix=".new")
        tmp = Path(name)
        with os.fdopen(fd, "wb") as fh:
            fh.write(candidate.encode())
            fh.flush()
            os.fsync(fh.fileno())
        os.chmod(tmp, 0o755)
        code, shown = check(["sh", "-n", str(tmp)], UPDATER_CHECK_SECONDS)
        if code != 0:
            rep.warn(
                f"the updater was not installed: the new script has a syntax error "
                f"({_first_lines(shown) or f'exit {code}'}); {live} is unchanged"
            )
            return current is not None
        code, shown = check(["sh", str(tmp), "selftest"], UPDATER_CHECK_SECONDS)
        if code != 0 or f"selftest: ok abi={new_abi}" not in shown.splitlines():
            why = "ran out of time" if code == 124 else _first_lines(shown) or f"exit {code}"
            rep.warn(
                f"the updater was not installed: its selftest failed ({why}); {live} is unchanged"
            )
            return current is not None
        # The checks take seconds: judge again against what is live now, not what was live
        # before them.
        latest = _read_text(live)
        if latest != current:
            if selfupdate.install_decision(candidate, latest) != "install":
                rep.out(f"updater: {live} changed while the new script was checked; keeping it")
                return latest is not None
            current = latest
        if current is not None:
            _write_script(live.with_name(f"{live.name}.prev"), current)
        os.replace(tmp, live)
    except OSError as exc:
        rep.warn(f"the updater was not installed: {exc}")
        return live.is_file()
    finally:
        if tmp is not None:
            tmp.unlink(missing_ok=True)
    rep.out(f"updater: installed {live}" + (" (the old one is kept as .prev)" if current else ""))
    return True


def ensure_updater_conf(conf: Path, text: str, rep: Reporter, *, dry_run: bool) -> None:
    """Write the settings file of the script when its content differs."""
    if _read_text(conf) == text:
        rep.out(f"updater settings: already current ({conf})")
        return
    if dry_run:
        rep.out(f"updater settings: dry-run: would write {conf}")
        return
    try:
        _write_script(conf, text, 0o644)
    except OSError as exc:
        rep.warn(f"the updater settings were not written ({exc})")
        return
    rep.out(f"updater settings: wrote {conf}")


def ensure_update_timer(
    home: Path,
    rep: Reporter,
    *,
    dry_run: bool,
    updater: Path,
    systemctl: FlatpakRunner | None = None,
) -> None:
    """Install and enable the systemd user timer that starts one tick of the updater.

    The service runs `sh <updater> tick` with the live copy of the script, so nothing is
    written when that file is not there (outside a dry run) or when there is no systemd user
    manager. The units are rewritten only when their content differs, and the timer is enabled
    only when it is not already.
    """
    ctl = systemctl or run_systemctl
    units = home / updatetimer.UNIT_DIR
    manual = (
        f"run `sh {updater} tick` from another scheduler (cron, for example), or "
        "set `self_update = false` for this device in the config"
    )

    def call(*args: str) -> tuple[int, str]:
        return ctl(["systemctl", "--user", *args])

    if not dry_run and not updater.is_file():
        rep.warn(f"the self-update timer was not installed: there is no updater at {updater}")
        return
    try:
        code, shown = call("show-environment")
        if code != 0:
            raise OSError(shown.strip().splitlines()[0] if shown.strip() else f"exit code {code}")
    except (OSError, subprocess.SubprocessError) as exc:
        rep.warn(
            f"the self-update timer was not installed: no systemd user manager ({exc}); {manual}"
        )
        return

    wanted = {
        units / updatetimer.SERVICE: updatetimer.render_service(str(updater)),
        units / updatetimer.TIMER: updatetimer.render_timer(),
    }
    changed = [path for path, text in wanted.items() if _read_text(path) != text]
    try:
        enabled = call("is-enabled", updatetimer.TIMER)[0] == 0
        active = call("is-active", updatetimer.TIMER)[0] == 0
    except (OSError, subprocess.SubprocessError):
        enabled = active = False
    if not changed and enabled and active:
        rep.out("self-update timer: already installed and enabled")
        return
    if dry_run:
        if changed:
            rep.out(f"self-update timer: dry-run: would write {', '.join(map(str, changed))}")
        rep.out(f"self-update timer: dry-run: would enable {updatetimer.TIMER}")
        return
    enable = f"systemctl --user enable --now {updatetimer.TIMER}"
    try:
        for path in changed:
            _write_script(path, wanted[path], 0o644)
        steps = [("daemon-reload",)] if changed else []
        steps.append(("enable", "--now", updatetimer.TIMER))
        if changed and active:
            steps.append(("restart", updatetimer.TIMER))
        for step in steps:
            code, shown = call(*step)
            if code != 0:
                raise OSError(shown.strip() or f"exit code {code}")
    except (OSError, subprocess.SubprocessError) as exc:
        rep.warn(f"the self-update timer could not be enabled ({exc}). Run `{enable}`")
        return
    rep.out(f"self-update timer: installed and enabled ({updatetimer.TIMER})")


def ensure_self_update(
    device: Device,
    profile: Profile,
    home: Path,
    rep: Reporter,
    *,
    dry_run: bool,
    python: str | None = None,
    import_check: ImportCheck = run_import_check,
    clone: Path | None = None,
    config_path: Path | None = None,
    state_root: Path | None = None,
    systemctl: FlatpakRunner | None = None,
    updater_source: Path = UPDATER_SOURCE,
) -> None:
    """Everything the autonomous update needs on this device: the settings file written from
    the config, the live copy of the updater script and the timer that starts it.

    It needs a git clone (the device install: `updater.sh install`) and a Python that imports
    warpzone from outside the current folder (the venv); without them it installs nothing and
    says so. A device with `self_update = false` gets no updater; one that already has settings
    gets `enabled=false`, so its timer's ticks stop.
    """
    python = python or sys.executable
    clone = clone if clone is not None else selfupdate.package_root()
    base = (state_root if state_root is not None else settings_state.state_dir(home=home)).parent
    conf = selfupdate.conf_path(base)
    live = selfupdate.live_path(home)

    def conf_text(enabled: bool) -> str:
        return selfupdate.render_conf(
            device=device.name,
            config=os.path.abspath(config_path) if config_path is not None else None,
            clone=clone,
            python=python,
            channel=device.channel,
            pin=device.pin,
            enabled=enabled,
            frontend=selfupdate.frontend_names(profile),
        )

    try:
        if not device.self_update:
            if conf.is_file():
                ensure_updater_conf(conf, conf_text(False), rep, dry_run=dry_run)
            rep.out("self-update timer: turned off (self_update = false in the config)")
            return
        text = conf_text(True)
    except ValueError as exc:
        rep.warn(f"the updater was not installed: {exc}")
        return
    if not (clone / ".git").exists():
        rep.warn(
            f"the updater was not installed: {clone} is not a git checkout. A device runs "
            "warpzone from a clone made with `sh <clone>/warpzone/data/updater.sh install "
            f"{device.name}` (see docs/self-update.md)"
        )
        return
    if not import_check(python):
        rep.warn(
            f"the updater was not installed: {python} cannot import warpzone from outside the "
            "current folder (run from the clone's venv?). Set the device up with "
            f"`sh {clone}/warpzone/data/updater.sh install {device.name}`"
        )
        return
    ensure_updater_conf(conf, text, rep, dry_run=dry_run)
    installed = ensure_updater(home, rep, dry_run=dry_run, source=updater_source)
    if installed or dry_run:
        ensure_update_timer(home, rep, dry_run=dry_run, updater=live, systemctl=systemctl)


def _busy(
    names: Sequence[str], label: str, what: str, again: str, rep: Reporter, proc: Path
) -> bool:
    """True, with a warning, when `what` must not be written now.

    That is when a process of `names` runs (the program rewrites or keeps in memory the
    file) and when there is no /proc to tell.
    """
    try:
        running = settings_state.running(names, proc=proc)
    except settings_state.NoProc:
        rep.warn(
            f"cannot tell whether {label} is running (no /proc), so {what} was left alone: "
            f"{again} on a system with /proc"
        )
        return True
    if running:
        rep.defer()
        rep.warn(f"{label} is running, so {what} was left alone: quit {label} and {again}")
    return running


def ensure_systems_override(
    name: str,
    profile: Profile,
    home: Path,
    rep: Reporter,
    *,
    dry_run: bool,
    proc: Path = Path("/proc"),
    state_root: Path,
) -> None:
    """Write the frontend's system list with the ROM folders moved into the library.

    The copy is `profile.es_systems.source` with `<path>` elements below `prefix` moved to
    `<library>/roms/`, every other byte kept. It is written only while the frontend is not
    running, replacing a different copy only after backing that one up. A second run
    changes nothing.
    """
    rule = profile.es_systems
    if rule is None or "library" not in profile.paths:
        return
    source, override = Path(expand_home(rule.source, home)), Path(expand_home(rule.override, home))
    again = f"run `warpzone provision device {name}` again"
    target = expand_home(profile.paths["library"], home).rstrip("/") + "/roms/"
    try:
        text = source.read_bytes().decode("utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        rep.warn(f"cannot read the system list {source} ({exc}), so {override} was not written")
        return
    new = frontend.rewrite_system_paths(text, rule.prefix, target, rule.renames)
    if new == text:
        rep.warn(f"{source} has no <path> below {rule.prefix}, so {override} was not written")
        return
    try:
        before: settings_state.FileStamp | None = settings_state.stamp(override)
        current: bytes | None = override.read_bytes()
    except FileNotFoundError:
        before, current = None, None
    except OSError as exc:
        rep.warn(f"cannot read {override} ({exc}), so it was left alone")
        return
    wanted = new.encode("utf-8")
    if current == wanted:
        rep.out(f"es_systems.cfg: already current ({override})")
        return
    label, what = "EmulationStation", f"{override}"
    if _busy(rule.process, label, what, again, rep, proc):
        return
    if dry_run:
        rep.out(f"es_systems.cfg: dry-run: would write {override} (ROM folders below {target})")
        return
    if before is not None:
        settings_state.backup(override, "emulationstation", "es_systems", root=state_root)
        if _busy(rule.process, label, what, again, rep, proc):  # it may have started meanwhile
            return
    if not settings_state.atomic_write(override, wanted, expect=before):
        rep.warn(f"{override} changed while it was being written, so it was left alone: {again}")
        return
    rep.out(f"es_systems.cfg: wrote {override}" + (" (old file backed up)" if before else ""))


def ensure_retroarch_pins(
    name: str,
    profile: Profile,
    home: Path,
    rep: Reporter,
    *,
    dry_run: bool,
    proc: Path = Path("/proc"),
    state_root: Path,
) -> None:
    """Set the profile's keys in each `retroarch.cfg`, only while RetroArch is not running.

    Every other byte of the file is kept. A file that is missing is not created (RetroArch
    writes it on its first start); one with a wanted key present twice is left alone. The
    old file is backed up before each edit and a second run changes nothing.
    """
    rule = profile.retroarch
    if rule is None or "library" not in profile.paths:
        return
    wants = rule.wants(expand_home(profile.paths["library"], home).rstrip("/"))
    again = f"run `warpzone provision device {name}` again"
    pending: list[tuple[Path, str, frontend.RetroArchGap]] = []
    for raw in rule.cfg:
        path = Path(expand_home(raw, home))
        try:
            text = path.read_bytes().decode("utf-8")
        except FileNotFoundError:
            rep.warn(
                f"{path} does not exist, so RetroArch's paths were not set: start RetroArch "
                f"once, then {again}"
            )
            continue
        except (OSError, UnicodeDecodeError) as exc:
            rep.warn(f"cannot read {path} ({exc}), so it was left alone")
            continue
        gap = frontend.retroarch_gap(text, wants)
        if gap.unwritable:
            keys = ", ".join(sorted(gap.unwritable))
            rep.warn(f"{path} was left alone: {keys} appears more than once; fix it by hand")
        elif gap.differs:
            pending.append((path, text, gap))
        else:
            rep.out(f"retroarch: {path} already set")
    if not pending:
        return
    what = "RetroArch's config files"
    if _busy(rule.process, "RetroArch", what, again, rep, proc):
        return
    for path, text, gap in pending:
        keys = ", ".join(sorted(gap.differs))
        if dry_run:
            rep.out(f"retroarch: dry-run: would set {keys} in {path}")
            continue
        before = settings_state.stamp(path)
        settings_state.backup(path, "retroarch", path.parent.name, root=state_root)
        if _busy(rule.process, "RetroArch", what, again, rep, proc):
            return
        new = frontend.retroarch_apply(text, wants)
        if not settings_state.atomic_write(path, new.encode("utf-8"), expect=before):
            rep.warn(f"{path} changed while it was being edited, so it was left alone: {again}")
            continue
        rep.out(f"retroarch: set {keys} in {path} (old file backed up)")


def ensure_shadps4_dirs(
    name: str,
    profile: Profile,
    home: Path,
    rep: Reporter,
    *,
    dry_run: bool,
    proc: Path = Path("/proc"),
    state_root: Path,
) -> None:
    """List the profile's install folders in shadPS4's config files, while it is closed.

    Each file of `shadps4.config` that exists gets the folders it lacks, next to the entries
    it already has; every other byte is kept. A file that is missing is never created
    (shadPS4 writes it on its first start); one that does not read as expected is left
    alone. The old file is backed up before each edit and a second run changes nothing.
    """
    rule = profile.shadps4
    if rule is None or "library" not in profile.paths:
        return
    wants = rule.wants(expand_home(profile.paths["library"], home).rstrip("/"))
    again = f"run `warpzone provision device {name}` again"
    pending: list[tuple[Path, str, str, list[str]]] = []
    seen = 0
    for raw in rule.config:
        path, kind = Path(expand_home(raw, home)), shadps4.kind_of(raw) or shadps4.JSON
        try:
            text = path.read_bytes().decode("utf-8")
        except FileNotFoundError:
            continue
        except (OSError, UnicodeDecodeError) as exc:
            seen += 1
            rep.warn(f"cannot read {path} ({exc}), so it was left alone")
            continue
        seen += 1
        try:
            gap = shadps4.missing(kind, text, wants)
        except shadps4.Shadps4ConfigError as exc:
            rep.warn(f"{path} was left alone: {exc}")
            continue
        if gap:
            pending.append((path, kind, text, gap))
        else:
            rep.out(f"shadps4: {path} already lists the install folder")
    if not seen:
        shown = " or ".join(expand_home(raw, home) for raw in rule.config)
        rep.warn(
            f"no shadPS4 config file found ({shown}), so its install folder was not set: "
            f"start shadPS4 once, then {again}"
        )
        return
    what = "shadPS4's config files"
    if not pending or _busy(rule.process, "shadPS4", what, again, rep, proc):
        return
    for path, kind, text, gap in pending:
        folders = ", ".join(gap)
        if dry_run:
            rep.out(f"shadps4: dry-run: would add the install folder {folders} to {path}")
            continue
        before = settings_state.stamp(path)
        settings_state.backup(path, "shadps4", path.name, root=state_root)
        if _busy(rule.process, "shadPS4", what, again, rep, proc):  # it may have started meanwhile
            return
        try:
            new = shadps4.apply(kind, text, wants)
        except shadps4.Shadps4ConfigError as exc:
            rep.warn(f"{path} was left alone: {exc}")
            continue
        if not settings_state.atomic_write(path, new.encode("utf-8"), expect=before):
            rep.warn(f"{path} changed while it was being edited, so it was left alone: {again}")
            continue
        rep.out(f"shadps4: added the install folder {folders} to {path} (old file backed up)")


def ensure_title_links(
    name: str,
    profile: Profile,
    home: Path,
    rep: Reporter,
    *,
    dry_run: bool,
    proc: Path = Path("/proc"),
    state_root: Path | None = None,
) -> None:
    """Link every library game of each `[links.*]` rule into the emulator's game folder,
    while the emulator is closed. Only links are changed; see `titlelinks`."""
    if not profile.links:
        return
    titlelinks.sync(
        name,
        profile,
        home,
        say=rep.out,
        warn=rep.warn,
        dry_run=dry_run,
        proc=proc,
        on_busy=rep.defer,
    )


def ensure_shadps4_launch(
    name: str,
    profile: Profile,
    home: Path,
    rep: Reporter,
    *,
    dry_run: bool,
    proc: Path = Path("/proc"),
    state_root: Path,
) -> None:
    """Install the shadPS4 launch script and point ES-DE's find rule at it.

    The script is rewritten only when its text differs. The rule is edited in
    `es_find_rules.xml` (that entry only, every other byte kept, old file backed up) while
    ES-DE is closed; a missing file is never created. A second run changes nothing.
    """
    rule = profile.shadps4
    launch = rule.launch if rule is not None else None
    if launch is None:
        return
    again = f"quit ES-DE and run `warpzone provision device {name}` again"
    script = Path(expand_home(launch.script, home))
    text = shadps4.render_launch_script(
        expand_home(launch.qt_ini, home), expand_home(launch.versions, home)
    )
    path = Path(expand_home(launch.find_rules, home))
    try:
        before = settings_state.stamp(path)
        rules = path.read_bytes().decode("utf-8")
    except FileNotFoundError:
        rep.warn(
            f"{path} does not exist, so ES-DE still starts shadPS4 through EmuDeck's launcher: "
            f"start ES-DE once, then {again}"
        )
        return
    except (OSError, UnicodeDecodeError) as exc:
        rep.warn(f"cannot read {path} ({exc}), so it was left alone")
        return
    try:
        current: str | None = script.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        current = None
    if current == text:
        if not dry_run and not os.access(script, os.X_OK):
            script.chmod(0o755)
        rep.out(f"shadps4: launch script unchanged ({script})")
    elif dry_run:
        rep.out(f"shadps4: dry-run: would write the launch script {script}")
    else:
        _write_script(script, text)
        rep.out(f"shadps4: wrote the launch script {script}")
    try:
        new = shadps4.point_find_rule(rules, launch.emulator, str(script))
    except shadps4.Shadps4ConfigError as exc:
        rep.warn(f"{path} was left alone: {exc}")
        return
    if new == rules:
        rep.out(f"shadps4: {path} already runs the launch script")
        return

    def running() -> bool | None:
        try:
            return settings_state.running(esde.ESDE_PROCESSES, proc=proc)
        except settings_state.NoProc:
            return None

    def refuse() -> bool:
        state = running()
        if state is None:
            rep.warn(f"cannot tell whether ES-DE is running (no /proc), so {path} was left alone")
        elif state:
            rep.defer()
            rep.warn(f"ES-DE is running, so {path} was left alone: {again}")
        return state is not False

    if refuse():
        return
    if dry_run:
        rep.out(f"shadps4: dry-run: would point the {launch.emulator} rule of {path} at {script}")
        return
    settings_state.backup(path, "shadps4", path.name, root=state_root)
    if refuse():  # ES-DE may have started while the backup was made
        return
    if not settings_state.atomic_write(path, new.encode("utf-8"), expect=before):
        rep.warn(f"{path} changed while it was being edited, so it was left alone: {again}")
        return
    rep.out(
        f"shadps4: pointed the {launch.emulator} rule of {path} at {script} (old file backed up)"
    )


def _artwork_layout(profile: Profile, home: Path) -> artwork.Layout | None:
    art = profile.artwork
    if art is None or "library" not in profile.paths:
        return None
    library = expand_home(profile.paths["library"], home).rstrip("/")
    return artwork.Layout(
        Path(library) / "roms",
        Path(expand_home(art.media, home)),
        Path(expand_home(art.metadata, home)),
        Path(expand_home(art.gamelists, home)),
    )


def artwork_scope(profile: Profile, home: Path) -> list[str]:
    """The hub systems a device with `[artwork]` receives artwork for (none without any)."""
    layout = _artwork_layout(profile, home)
    if layout is None or profile.artwork is None:
        return []
    return artwork.library_systems(layout.roms)  # `systems = "library"`, the only scope


def _no_scope_warning(name: str, layout: artwork.Layout) -> str:
    return (
        f"no system folder in {layout.roms} yet, so no artwork is received; it follows once "
        f"games have arrived (`warpzone frontend refresh {name}`, or the timer, picks it up)"
    )


def sync_artwork_scope(
    cfg: Config,
    name: str,
    profile: Profile,
    home: Path,
    local: Client,
    rep: Reporter,
) -> None:
    """Make the ignores of the artwork folders match the systems that are on the device now.

    A system whose games arrived after `provision device` has no artwork until its folders
    let it in. A folder that Syncthing does not have yet is left for `provision device`.
    """
    if profile.artwork is None or "frontend" not in cfg.devices[name].folders:
        return
    wanted = [
        dataclasses.replace(s, path=expand_home(s.path, home))
        for s in device_specs(cfg, name, profile, artwork_scope(profile, home))
        if s.id in (MEDIA_ID, ESDE_ID)
    ]
    existing = {f["id"]: f for f in local.folders()}
    systems = len(artwork_scope(profile, home))
    for spec in wanted:
        if spec.id not in existing:
            rep.warn(f"{spec.id} is not in Syncthing yet: run `warpzone provision device {name}`")
        elif local.get_ignores(spec.id) != list(spec.ignores):
            local.set_ignores(spec.id, list(spec.ignores))
            rep.out(f"{spec.id}: ignores updated ({systems} systems)")


def ensure_gamelists(
    name: str,
    profile: Profile,
    home: Path,
    rep: Reporter,
    *,
    dry_run: bool,
    proc: Path = Path("/proc"),
    play_state: bool = False,
) -> None:
    """Write the frontend's gamelists from the scraped data that Syncthing delivered.

    Each system on the device that has a scraped gamelist gets one for the frontend, under
    the system's name in the frontend's list (`artwork.plan_gamelists`). Nothing is written
    for a gamelist that already says the same, so a second run, and a run while the frontend
    is open and has nothing to change, do nothing. A file is written only while the frontend
    is not running, atomically, and refused when it changed after it was read. The user
    data the frontend saved in a gamelist is carried over; see docs/design.md, "Frontend
    artwork". With `play_state` that data also goes to the other devices and theirs is merged
    in; see docs/design.md, "Play state".
    """
    art, rule = profile.artwork, profile.es_systems
    layout = _artwork_layout(profile, home)
    if art is None or rule is None or layout is None:
        return
    fmt = artwork.FORMATS[art.format]
    again = f"run `warpzone frontend refresh {name}` again"
    hubs = artwork.library_systems(layout.roms)
    if not hubs:
        rep.out(f"gamelists: no system has games in {layout.roms} yet")
        return
    source = Path(expand_home(rule.source, home))
    try:
        system_list = source.read_bytes().decode("utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        rep.warn(f"cannot read the system list {source} ({exc}), so no gamelist was written")
        return
    stamps: dict[Path, settings_state.FileStamp] = {}

    def read(path: Path) -> bytes | None:
        try:
            stamp = settings_state.stamp(path)
            data = path.read_bytes()
        except FileNotFoundError:
            return None
        stamps[path] = stamp
        return data

    files = dataclasses.replace(artwork.REAL_FILES, read=read)
    state: playstate_device.Generated | None = None
    try:
        if play_state:
            state = playstate_device.prepare_generated(
                name,
                profile,
                home,
                layout,
                artwork.gamelist_systems(
                    layout, system_list, prefix=rule.prefix, renames=rule.renames, hubs=hubs
                ),
                files,
                warn=rep.warn,
            )
        plan = artwork.plan_gamelists(
            fmt,
            art.kinds,
            layout,
            system_list,
            prefix=rule.prefix,
            renames=rule.renames,
            hubs=hubs,
            files=files,
            overlay=state.overlay() if state is not None else None,
        )
    except artwork.ArtworkError as exc:
        rep.warn(f"{source}: {exc}, so no gamelist was written")
        return
    for problem in plan.problems:
        rep.warn(f"{problem}, so the gamelists of that system were left as they are")
    for system in plan.dropped:
        rep.warn(
            f"{system.hub}: the frontend has another system called {system.name!r}, "
            "so no gamelist was written for this one"
        )
    pending = plan.pending()

    def publish(*, applied: bool) -> None:
        if state is not None and not dry_run and state.publish(applied=applied):
            rep.out(f"play state: published {state.where}")

    if not plan.items:
        rep.out("gamelists: no scraped gamelist for the systems on the device yet")
        publish(applied=False)
        return
    if not pending:
        rep.out(f"gamelists: {len(plan.items)} current")
        publish(applied=True)
        return
    label, what = "EmulationStation", "its gamelists"
    if _busy(rule.process, label, what, again, rep, proc):
        publish(applied=False)
        return
    # First the ledger: a run cut short between the two loses at worst the plays made in that
    # moment, where the other order would count every merged play as played here.
    publish(applied=True)
    for item in pending:
        if dry_run:
            rep.out(f"gamelists: dry-run: would write {item.path} ({item.games} games)")
        elif _busy(rule.process, label, what, again, rep, proc):  # it may have started
            return
        elif not settings_state.atomic_write(item.path, item.data, expect=stamps.get(item.path)):
            rep.warn(
                f"{item.path} changed while it was being written, so it was left alone: {again}"
            )
        else:
            rep.out(f"gamelists: wrote {item.path} ({item.games} games, was {item.state})")
    if not dry_run:
        rep.out(f"gamelists: {len(plan.items) - len(pending)} current")


def ensure_artwork_timer(
    name: str,
    profile: Profile,
    home: Path,
    rep: Reporter,
    *,
    dry_run: bool,
    python: str | None = None,
    import_check: ImportCheck = run_import_check,
    config_path: Path | None = None,
    systemctl: SystemctlRunner = run_user_systemctl,
) -> None:
    """Install and start the `systemd --user` timer that runs `frontend refresh`.

    Only for a profile whose `[artwork]` sets `refresh`. The units run the Python that runs
    this command, which must import warpzone from outside the current folder. They are
    rewritten only when their text differs; a second run makes no systemctl call.
    """
    art = profile.artwork
    if art is None or art.refresh is None:
        return
    python = python or sys.executable
    if not import_check(python):
        rep.warn(
            f"the artwork timer was not installed: {python} cannot import warpzone from "
            "outside the current folder (run from a clone?). Install warpzone for it (for "
            f"example with pipx), then run `warpzone provision device {name}` from there"
        )
        return
    service, timer = artwork.render_units(
        python, name, _script_config(config_path, home), art.refresh
    )
    _install_timer(
        artwork.UNIT_NAME, service, timer, art.refresh, "artwork", home, rep, dry_run, systemctl
    )


def ensure_play_state_timer(
    name: str,
    profile: Profile,
    home: Path,
    rep: Reporter,
    *,
    dry_run: bool,
    python: str | None = None,
    import_check: ImportCheck = run_import_check,
    config_path: Path | None = None,
    systemctl: SystemctlRunner = run_user_systemctl,
) -> None:
    """Install and start the `systemd --user` timer that runs `play-state sync`.

    Only for a profile whose `[play_state]` sets `refresh`; a device with `[artwork]` has its
    own timer. Between ES-DE starts it publishes what ES-DE saved, so the other devices get it
    without waiting for the next start.
    """
    rule = profile.play_state
    if rule is None or rule.refresh is None:
        return
    python = python or sys.executable
    if not import_check(python):
        rep.warn(
            f"the play state timer was not installed: {python} cannot import warpzone from "
            "outside the current folder (run from a clone?). Install warpzone for it (for "
            f"example with pipx), then run `warpzone provision device {name}` from there"
        )
        return
    service, timer = artwork.render_play_state_units(
        python, name, _script_config(config_path, home), rule.refresh
    )
    _install_timer(
        artwork.PLAY_STATE_UNIT,
        service,
        timer,
        rule.refresh,
        "play state",
        home,
        rep,
        dry_run,
        systemctl,
    )


def _install_timer(
    unit_name: str,
    service: str,
    timer: str,
    interval: str,
    label: str,
    home: Path,
    rep: Reporter,
    dry_run: bool,
    systemctl: SystemctlRunner,
) -> None:
    folder = home / ".config" / "systemd" / "user"
    wanted = {f"{unit_name}.service": service, f"{unit_name}.timer": timer}
    changed = False
    for filename, text in wanted.items():
        path = folder / filename
        try:
            before: settings_state.FileStamp | None = settings_state.stamp(path)
            current: str | None = path.read_text(encoding="utf-8")
        except FileNotFoundError:
            before, current = None, None
        except (OSError, UnicodeDecodeError) as exc:
            rep.warn(f"cannot read {path} ({exc}), so the {label} timer was left alone")
            return
        if current == text:
            continue
        changed = True
        if dry_run:
            rep.out(f"timer: dry-run: would write {path}")
        elif not settings_state.atomic_write(path, text.encode("utf-8"), expect=before):
            rep.warn(f"{path} changed while it was being written, so it was left alone: run again")
            return
        else:
            rep.out(f"timer: wrote {path}")
    if not changed:
        rep.out(f"timer: {unit_name} unchanged (every {interval})")
        return
    if dry_run:
        return
    unit = f"{unit_name}.timer"
    for args in (("daemon-reload",), ("enable", "--now", unit), ("restart", unit)):
        if not systemctl(args):
            rep.warn(
                f"`systemctl --user {' '.join(args)}` failed, so the {label} timer may not "
                f"run: run it by hand (is a user session running? `loginctl enable-linger`)"
            )
            return
    rep.out(f"timer: {unit} runs every {interval}, skipping while the frontend is open")


def refresh_artwork(
    cfg: Config,
    name: str,
    profile: Profile,
    home: Path,
    rep: Reporter,
    *,
    local: Client | None,
    dry_run: bool,
    proc: Path = Path("/proc"),
) -> None:
    """`frontend refresh`: bring the artwork scope up to date, then the gamelists."""
    layout = _artwork_layout(profile, home)
    if layout is not None and not artwork_scope(profile, home):
        rep.warn(_no_scope_warning(name, layout))
    if local is None:
        rep.warn("local Syncthing not found, so which systems receive artwork was not checked")
    else:
        sync_artwork_scope(cfg, name, profile, home, local, rep)
    ensure_gamelists(
        name,
        profile,
        home,
        rep,
        dry_run=dry_run,
        proc=proc,
        play_state="play-state" in cfg.devices[name].folders,
    )


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
    sleep: Callable[[float], None] = time.sleep,
    syncthing_config: Path | None = None,
    flatpak_run: FlatpakRunner = run_flatpak,
    python: str | None = None,
    import_check: ImportCheck = run_import_check,
    proc: Path = Path("/proc"),
    state_root: Path | None = None,
    config_path: Path | None = None,
    systemctl: SystemctlRunner = run_user_systemctl,
    systemctl_run: FlatpakRunner | None = None,
    unattended: bool = False,
    clone: Path | None = None,
    updater_source: Path = UPDATER_SOURCE,
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
        )
        for s in device_specs(cfg, name, profile, scope)
    ]
    existing = {f["id"]: f for f in local.folders()}
    if unattended:
        present = [s for s in specs if not (s.skip_if_missing and not os.path.isdir(s.path))]
        problems = [
            problem
            for spec in present
            if spec.id in existing
            for problem in unattended_refusals(spec, existing[spec.id])
        ] + _missing_markers(present, existing)
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
        run=flatpak_run,
        unattended=unattended,
    )

    known = {d["deviceID"] for d in local.devices()}
    _ensure_device(local, cfg.hub.device_id, cfg.hub.name, cfg.hub.addresses, known, rep)

    for spec in todo:
        current = existing.get(spec.id)
        created = _upsert_folder(local, spec, current, rep)
        if spec.bootstrap_ignores is None:
            _apply_ignores(local, spec, current, created, rep)
        else:
            _provision_whitelisted(
                local,
                spec,
                current,
                created,
                timeout=wait_timeout,
                poll=poll,
                rep=rep,
                sleep=sleep,
                unattended=unattended,
            )
        rep.flush(spec.id)

    if "library" in device.folders:
        root = state_root if state_root is not None else settings_state.state_dir(home=home)
        for ensure in (
            ensure_systems_override,
            ensure_retroarch_pins,
            ensure_shadps4_dirs,
            ensure_shadps4_launch,
            ensure_title_links,
        ):
            ensure(name, profile, home, rep, dry_run=local.dry_run, proc=proc, state_root=root)

    if with_artwork:
        layout = _artwork_layout(profile, home)
        if layout is not None and not scope:
            rep.warn(_no_scope_warning(name, layout))
        ensure_gamelists(
            name,
            profile,
            home,
            rep,
            dry_run=local.dry_run,
            proc=proc,
            play_state="play-state" in device.folders,
        )
        ensure_artwork_timer(
            name,
            profile,
            home,
            rep,
            dry_run=local.dry_run,
            python=python,
            import_check=import_check,
            config_path=config_path,
            systemctl=systemctl,
        )

    if "settings" in device.folders:
        ensure_esde_script(
            name,
            profile,
            home,
            rep,
            dry_run=local.dry_run,
            python=python,
            import_check=import_check,
            proc=proc,
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
            import_check=import_check,
            proc=proc,
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
            import_check=import_check,
            proc=proc,
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
            import_check=import_check,
            config_path=config_path,
            systemctl=systemctl,
        )

    ensure_self_update(
        device,
        profile,
        home,
        rep,
        dry_run=local.dry_run,
        python=python,
        import_check=import_check,
        clone=clone,
        config_path=config_path,
        state_root=state_root,
        systemctl=systemctl_run,
        updater_source=updater_source,
    )


def folder_status(client: Client, title: str, rep: Reporter) -> None:
    """One line per wz-* folder: state, how much is outstanding, local changes."""
    rep.out(f"{title}:")
    folders = [f for f in client.folders() if f["id"].startswith(PREFIX)]
    if not folders:
        rep.out("  no warpzone folders")
    for folder in sorted(folders, key=lambda f: f["id"]):
        if folder.get("paused"):
            rep.out(f"  {folder['id']}: paused")
            continue
        st = client.db_status(folder["id"])
        need = sum(st.get(k, 0) for k in ("needFiles", "needDirectories", "needDeletes"))
        parts = [str(st.get("state", "?")), f"{st.get('globalFiles', 0)} files", f"need {need}"]
        errors = st.get("errors", 0) + st.get("pullErrors", 0)
        if errors:
            parts.append(f"{errors} errors")
        if folder.get("type") == "receiveonly":
            changed = st.get("receiveOnlyTotalItems", 0)
            if changed:
                parts.append(f"{changed} local changes")
        if st.get("error"):
            parts.append(f"error: {st['error']}")
        rep.out(f"  {folder['id']}: {', '.join(parts)}")
