"""Hub and device provisioning: diff desired state against Syncthing, then call."""

from __future__ import annotations

import dataclasses
import os
import subprocess
import time
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from warpzone import flatpak
from warpzone.config import Config
from warpzone.model import FolderSpec, device_specs, hub_specs
from warpzone.profiles import Profile
from warpzone.syncthing import Client, SyncthingError

DEFAULT_WAIT_SECONDS = 600.0
HUB_WAIT_SECONDS = 120.0  # `provision hub`: how long a new folder waits for its whitelists
PREFIX = "wz-"


class ProvisionError(Exception):
    pass


@dataclass
class Reporter:
    out: Callable[[str], None] = print
    warnings: list[str] = field(default_factory=list)
    warn_sink: Callable[[str], None] | None = None
    _notes: dict[str, list[str]] = field(default_factory=dict)

    def note(self, folder_id: str, text: str) -> None:
        """Collect what happened to one folder; `flush` prints it as a single line."""
        self._notes.setdefault(folder_id, []).append(text)

    def flush(self, folder_id: str) -> None:
        notes = self._notes.pop(folder_id, [])
        self.out(f"{folder_id}: {', '.join(notes) if notes else 'unchanged'}")

    def warn(self, message: str) -> None:
        self.warnings.append(message)
        if self.warn_sink:
            self.warn_sink(f"warning: {message}")


def expand_home(path: str, home: Path) -> str:
    if path == "~":
        return str(home)
    if path.startswith("~/"):
        return str(home / path[2:])
    return path


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
    owned = ("label", "type", "fsWatcherEnabled", "rescanIntervalS")
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


def _hub_precheck(hub: Client, specs: list[FolderSpec], rep: Reporter) -> None:
    """Fail early if a whitelist is missing on the hub (skipped if the hub is unreachable)."""
    for spec in specs:
        if spec.bootstrap_ignores is None:
            continue
        try:
            names = set(hub.browse(spec.id))
        except SyncthingError as exc:
            if exc.status is None:
                rep.warn(f"hub API unreachable, skipping the whitelist pre-check ({exc})")
                return
            raise ProvisionError(
                f"{spec.id} is not configured on the hub: run `warpzone provision hub` first"
            ) from exc
        missing = [f for f in spec.required_files if f not in names]
        if missing:
            raise ProvisionError(
                f"{spec.id}: {', '.join(missing)} not found on the hub. Create it in the "
                "folder on the hub (see templates/), let the hub scan, and try again. "
                "Nothing was changed on this device."
            )


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
) -> None:
    assert spec.bootstrap_ignores is not None
    final, boot = list(spec.ignores), list(spec.bootstrap_ignores)
    present = [] if created else client.get_ignores(spec.id)
    if present == final:
        rep.note(spec.id, "ignores already final")
        return
    if present != boot:
        client.set_ignores(spec.id, boot)
        rep.note(spec.id, "bootstrap ignores set")
    if created or (current is not None and current.get("paused")):
        _unpause(client, spec, rep)
    if client.dry_run:
        rep.note(spec.id, f"dry-run: would wait up to {timeout:.0f}s for the whitelists")
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


FlatpakRunner = Callable[[Sequence[str]], tuple[int, str]]


def run_flatpak(command: Sequence[str]) -> tuple[int, str]:
    """(exit code, stdout and stderr) of a `flatpak` command; raises OSError if it cannot run."""
    proc = subprocess.run(
        list(command),
        capture_output=True,
        text=True,
        errors="replace",
        timeout=60,
        stdin=subprocess.DEVNULL,
        check=False,
    )
    return proc.returncode, (proc.stdout or "") + (proc.stderr or "")


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
) -> None:
    """Grant the SyncThingy flatpak the data folders of the emulators it must sync.

    A flatpak cannot see `~/.var/app/<app>` of other apps, even with the `host`
    filesystem, so a save folder there looks empty to Syncthing. Does nothing unless the
    local Syncthing is that flatpak (its `config.xml` is in the flatpak's own folder).
    Only the overrides that are missing are added, so a second run changes nothing.
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
) -> None:
    device = cfg.devices[name]
    actual = local.my_id()
    if actual != device.device_id:
        raise ProvisionError(
            f"the local Syncthing is device {actual[:7]}..., but devices.{name}.device_id "
            f"starts with {device.device_id[:7]}..."
        )

    specs = [
        dataclasses.replace(
            s,
            path=expand_home(s.path, home),
            create_when=expand_home(s.create_when, home) if s.create_when else None,
        )
        for s in device_specs(cfg, name, profile)
    ]
    todo: list[FolderSpec] = []
    for spec in specs:
        if spec.skip_if_missing and not os.path.isdir(spec.path):
            if _create_target(spec, local.dry_run, rep):
                todo.append(spec)
            else:
                rep.warn(
                    f"{spec.id}: skipped, {spec.path} does not exist yet "
                    "(open the emulator once, then run provision again)"
                )
        else:
            todo.append(spec)

    if hub is not None:
        _hub_precheck(hub, todo, rep)

    ensure_flatpak_access(todo, syncthing_config, home, rep, dry_run=local.dry_run, run=flatpak_run)

    known = {d["deviceID"] for d in local.devices()}
    _ensure_device(local, cfg.hub.device_id, cfg.hub.name, cfg.hub.addresses, known, rep)

    existing = {f["id"]: f for f in local.folders()}
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
            )
        rep.flush(spec.id)


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
