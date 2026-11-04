"""Hub and device provisioning: diff desired state against Syncthing, then call."""

from __future__ import annotations

import dataclasses
import os
import subprocess
import time
from collections.abc import Callable, Iterable, Sequence
from functools import partial
from pathlib import Path
from typing import Any

from warpzone import (
    artwork,
    esde,
    flatpak,
    frontend,
    playstate_device,
    procs,
    safefile,
    settings_state,
    shadps4,
    titlelinks,
)
from warpzone.config import DEFAULT_WAIT_SECONDS, Config
from warpzone.model import ESDE_ID, MEDIA_ID, FolderSpec, device_specs
from warpzone.paths import expand_home
from warpzone.profiles import Profile
from warpzone.provision_folders import (
    PREFIX,
    ProvisionError,
    apply_ignores,
    ensure_device,
    folder_status,
    in_sync,
    unattended_refusals,
    unpause,
    upsert_folder,
    wait_for_files,
    wait_pending,
)
from warpzone.provision_hub import check_hub, provision_hub
from warpzone.provision_scripts import (
    ensure_esde_script,
    ensure_links_script,
    ensure_play_state_script,
)
from warpzone.provision_timers import ensure_artwork_timer, ensure_play_state_timer
from warpzone.provision_updater import UPDATER_SOURCE, ensure_self_update
from warpzone.report import Reporter
from warpzone.runners import (
    FlatpakRunner,
    ImportCheck,
    SystemctlRunner,
    run_flatpak,
    run_import_check,
    run_user_systemctl,
)
from warpzone.syncthing import Client, SyncthingError

__all__ = [
    "PREFIX",
    "ProvisionError",
    "check_hub",
    "folder_status",
    "provision_hub",
    "wait_for_files",
]


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
            unpause(client, spec, rep)
        client.scan(spec.id)
        rep.note(spec.id, "final ignores set (whitelists on disk), rescanning")
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


def busy(
    names: Sequence[str], label: str, what: str, again: str, rep: Reporter, proc: Path
) -> bool:
    """True, with a warning, when `what` must not be written now.

    That is when a process of `names` runs (the program rewrites or keeps in memory the
    file) and when there is no /proc to tell.
    """
    try:
        running = procs.running(names, proc=proc)
    except procs.NoProc:
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
        before: safefile.FileStamp | None = safefile.stamp(override)
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
    if busy(rule.process, label, what, again, rep, proc):
        return
    if dry_run:
        rep.out(f"es_systems.cfg: dry-run: would write {override} (ROM folders below {target})")
        return
    outcome = safefile.guarded_edit(
        override,
        wanted,
        expect=before,
        busy=lambda: busy(rule.process, label, what, again, rep, proc),  # it may have started
        backup=(
            None
            if before is None
            else lambda: settings_state.backup(
                override, "emulationstation", "es_systems", root=state_root
            )
        ),
    )
    if outcome is safefile.Edit.BUSY:
        return
    if outcome is safefile.Edit.CHANGED:
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
    if busy(rule.process, "RetroArch", what, again, rep, proc):
        return
    for path, text, gap in pending:
        keys = ", ".join(sorted(gap.differs))
        if dry_run:
            rep.out(f"retroarch: dry-run: would set {keys} in {path}")
            continue
        outcome = safefile.guarded_edit(
            path,
            frontend.retroarch_apply(text, wants).encode("utf-8"),
            expect=safefile.stamp(path),
            busy=lambda: busy(rule.process, "RetroArch", what, again, rep, proc),
            backup=partial(
                settings_state.backup, path, "retroarch", path.parent.name, root=state_root
            ),
        )
        if outcome is safefile.Edit.BUSY:
            return
        if outcome is safefile.Edit.CHANGED:
            rep.warn(f"{path} changed while it was being edited, so it was left alone: {again}")
            continue
        rep.out(f"retroarch: set {keys} in {path} (old file backed up)")


def _shadps4_apply(kind: str, text: str, wants: Sequence[str]) -> bytes:
    return shadps4.apply(kind, text, wants).encode("utf-8")


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
    if not pending or busy(rule.process, "shadPS4", what, again, rep, proc):
        return
    for path, kind, text, gap in pending:
        folders = ", ".join(gap)
        if dry_run:
            rep.out(f"shadps4: dry-run: would add the install folder {folders} to {path}")
            continue
        try:
            outcome = safefile.guarded_edit(
                path,
                partial(_shadps4_apply, kind, text, wants),
                expect=safefile.stamp(path),
                busy=lambda: busy(rule.process, "shadPS4", what, again, rep, proc),
                backup=partial(settings_state.backup, path, "shadps4", path.name, root=state_root),
            )
        except shadps4.Shadps4ConfigError as exc:
            rep.warn(f"{path} was left alone: {exc}")
            continue
        if outcome is safefile.Edit.BUSY:
            return
        if outcome is safefile.Edit.CHANGED:
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
        before = safefile.stamp(path)
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
        safefile.write_script(script, text)
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
            return procs.running(esde.ESDE_PROCESSES, proc=proc)
        except procs.NoProc:
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
    outcome = safefile.guarded_edit(
        path,
        new.encode("utf-8"),
        expect=before,
        busy=refuse,  # ES-DE may have started while the backup was made
        backup=lambda: settings_state.backup(path, "shadps4", path.name, root=state_root),
    )
    if outcome is safefile.Edit.BUSY:
        return
    if outcome is safefile.Edit.CHANGED:
        rep.warn(f"{path} changed while it was being edited, so it was left alone: {again}")
        return
    rep.out(
        f"shadps4: pointed the {launch.emulator} rule of {path} at {script} (old file backed up)"
    )


def artwork_layout(profile: Profile, home: Path) -> artwork.Layout | None:
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
    layout = artwork_layout(profile, home)
    if layout is None or profile.artwork is None:
        return []
    return artwork.library_systems(layout.roms)  # `systems = "library"`, the only scope


def no_scope_warning(name: str, layout: artwork.Layout) -> str:
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
    data the frontend saved in a gamelist is carried over; see development/design.md, "Frontend
    artwork". With `play_state` that data also goes to the other devices and theirs is merged
    in; see development/design.md, "Play state".
    """
    art, rule = profile.artwork, profile.es_systems
    layout = artwork_layout(profile, home)
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
    stamps: dict[Path, safefile.FileStamp] = {}

    def read(path: Path) -> bytes | None:
        try:
            stamp = safefile.stamp(path)
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
    if busy(rule.process, label, what, again, rep, proc):
        publish(applied=False)
        return
    # First the ledger: a run cut short between the two loses at worst the plays made in that
    # moment, where the other order would count every merged play as played here.
    publish(applied=True)
    for item in pending:
        if dry_run:
            rep.out(f"gamelists: dry-run: would write {item.path} ({item.games} games)")
        else:
            outcome = safefile.guarded_edit(
                item.path,
                item.data,
                expect=stamps.get(item.path),
                busy=lambda: busy(rule.process, label, what, again, rep, proc),  # it may start
            )
            if outcome is safefile.Edit.BUSY:
                return
            if outcome is safefile.Edit.CHANGED:
                rep.warn(
                    f"{item.path} changed while it was being written, so it was left alone: {again}"
                )
                continue
            rep.out(f"gamelists: wrote {item.path} ({item.games} games, was {item.state})")
    if not dry_run:
        rep.out(f"gamelists: {len(plan.items) - len(pending)} current")


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
    layout = artwork_layout(profile, home)
    if layout is not None and not artwork_scope(profile, home):
        rep.warn(no_scope_warning(name, layout))
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
    ensure_device(local, cfg.hub.device_id, cfg.hub.name, cfg.hub.addresses, known, rep)

    for spec in todo:
        current = existing.get(spec.id)
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
        layout = artwork_layout(profile, home)
        if layout is not None and not scope:
            rep.warn(no_scope_warning(name, layout))
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
