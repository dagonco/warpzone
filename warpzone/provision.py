"""Hub and device provisioning: diff desired state against Syncthing, then call."""

from __future__ import annotations

import dataclasses
import os
import subprocess
import sys
import time
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from warpzone import esde, flatpak, frontend, settings_state, updatetimer
from warpzone.config import DEFAULT_WAIT_SECONDS, HUB_WAIT_SECONDS, Config
from warpzone.model import FolderSpec, device_specs, hub_specs
from warpzone.profiles import Profile
from warpzone.syncthing import Client, SyncthingError

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


ImportCheck = Callable[[str], bool]


def run_import_check(python: str) -> bool:
    """Whether `python` imports warpzone from outside the current folder.

    Run from `/` and without the variables the startup script drops, so a clone in the
    current folder or on PYTHONPATH does not make an interpreter look good that ES-DE's
    script could not use.
    """
    env = {k: v for k, v in os.environ.items() if k not in ("PYTHONPATH", "PYTHONHOME")}
    try:
        proc = subprocess.run(
            [python, "-c", "import warpzone"],
            cwd="/",
            env=env,
            capture_output=True,
            timeout=60,
            stdin=subprocess.DEVNULL,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return proc.returncode == 0


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
    esde_dir = Path(expand_home(profile.paths["esde"], home))
    if not import_check(python):
        rep.warn(
            f"the ES-DE startup script was not installed: {python} cannot import warpzone "
            "from outside the current folder (run from a clone?). Install warpzone for it "
            f"(for example with pipx), then run `warpzone provision device {name}` from there"
        )
        return
    path = esde.script_path(esde_dir)
    text = esde.render_startup_script(python, name, _script_config(config_path, home))
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


def run_systemctl(command: Sequence[str]) -> tuple[int, str]:
    """(exit code, stdout and stderr) of a `systemctl` command; raises OSError if it cannot run."""
    return run_flatpak(command)


def ensure_update_timer(
    home: Path,
    rep: Reporter,
    *,
    dry_run: bool,
    python: str | None = None,
    import_check: ImportCheck = run_import_check,
    systemctl: FlatpakRunner | None = None,
) -> None:
    """Install and enable the systemd user timer that runs `warpzone self-update`.

    The service runs the Python that runs this command, so it must import warpzone from
    outside the current folder (a venv with the editable install does). Nothing is written
    when that is not so or when there is no systemd user manager. The units are rewritten
    only when their content differs, and the timer is enabled only when it is not already.
    """
    ctl = systemctl or run_systemctl
    python = python or sys.executable
    units = home / updatetimer.UNIT_DIR
    manual = (
        "schedule `warpzone self-update` with another scheduler (cron, for example), or "
        "set `self_update = false` for this device in the config"
    )

    def call(*args: str) -> tuple[int, str]:
        return ctl(["systemctl", "--user", *args])

    if not import_check(python):
        rep.warn(
            f"the self-update timer was not installed: {python} cannot import warpzone from "
            "outside the current folder (run from a clone?). Install warpzone for it (an "
            "editable install in a venv), then run `warpzone provision device` from there"
        )
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
        units / updatetimer.SERVICE: updatetimer.render_service(python),
        units / updatetimer.TIMER: updatetimer.render_timer(),
    }

    def current(path: Path) -> str | None:
        try:
            return path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            return None

    changed = [path for path, text in wanted.items() if current(path) != text]
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
    systemctl_run: FlatpakRunner | None = None,
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

    if "library" in device.folders:
        root = state_root if state_root is not None else settings_state.state_dir(home=home)
        for ensure in (ensure_systems_override, ensure_retroarch_pins):
            ensure(name, profile, home, rep, dry_run=local.dry_run, proc=proc, state_root=root)

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

    if device.self_update:
        ensure_update_timer(
            home,
            rep,
            dry_run=local.dry_run,
            python=python,
            import_check=import_check,
            systemctl=systemctl_run,
        )
    else:
        rep.out("self-update timer: turned off (self_update = false in the config)")


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
