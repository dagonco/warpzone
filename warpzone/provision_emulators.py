"""Device edits for the frontend and the emulators.

ES-DE's system list, RetroArch, shadPS4, Cemu and the title links.
"""

from __future__ import annotations

import os
import sys
from collections.abc import Sequence
from functools import partial
from pathlib import Path

from warpzone import cemu, esde, frontend, procs, safefile, settings_state, shadps4, titlelinks
from warpzone.paths import expand_home
from warpzone.probes import REAL_PROBES, DeviceProbes
from warpzone.profiles import Profile
from warpzone.provision_scripts import script_config
from warpzone.report import Reporter


def busy(
    names: Sequence[str], label: str, what: str, again: str, rep: Reporter, probes: DeviceProbes
) -> bool:
    """True, with a warning, when `what` must not be written now.

    That is when a process of `names` runs (the program rewrites or keeps in memory the
    file) and when there is no /proc to tell.
    """
    try:
        running = procs.running(names, proc=probes.proc)
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
    probes: DeviceProbes = REAL_PROBES,
    state_root: Path,
    python: str | None = None,
    config_path: Path | None = None,
    saves: bool = False,
) -> None:
    """Write the frontend's system list with the ROM folders moved into the library.

    The copy is `profile.es_systems.source` with `<path>` elements below `prefix` moved to
    `<library>/roms/`, every other byte kept. It is written only while the frontend is not
    running, replacing a different copy only after backing that one up. A second run
    changes nothing. With `saves` (the device syncs saves), the `<command>` of each system
    that a `[save_convert.*]` rule names `es_system` runs `warpzone saves convert` before and
    after the original command; see development/design/save-conversion.md.
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
    new = _wrapped(name, profile, home, new, rep, python, config_path, saves, probes)
    wanted = new.encode("utf-8")
    if current == wanted:
        rep.out(f"es_systems.cfg: already current ({override})")
        return
    label, what = "EmulationStation", f"{override}"
    if busy(rule.process, label, what, again, rep, probes):
        return
    if dry_run:
        rep.out(f"es_systems.cfg: dry-run: would write {override} (ROM folders below {target})")
        return
    outcome = safefile.guarded_edit(
        override,
        wanted,
        expect=before,
        busy=lambda: busy(rule.process, label, what, again, rep, probes),  # it may have started
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


def wrapped_systems(profile: Profile) -> tuple[str, ...]:
    """The frontend systems whose command is wrapped in the save conversion."""
    return tuple(r.es_system for r in profile.save_convert.values() if r.es_system)


def _wrapped(
    name: str,
    profile: Profile,
    home: Path,
    text: str,
    rep: Reporter,
    python: str | None,
    config_path: Path | None,
    saves: bool,
    probes: DeviceProbes,
) -> str:
    """`text` with the commands of the save-converting systems wrapped (or `text` itself)."""
    systems = wrapped_systems(profile)
    if not systems or not saves:
        return text
    python = python or sys.executable
    if not probes.import_check(python):
        rep.warn(
            f"the save conversion was not added to the system list: {python} cannot import "
            "warpzone from outside the current folder (run from a clone?). Set the device up "
            f"with `updater.sh install {name}`, which provisions from a venv"
        )
        return text
    command = frontend.convert_command(python, name, script_config(config_path, home))
    wrapped = frontend.wrap_systems(text, systems, command)
    missing = set(systems) - frontend.wrapped_systems(wrapped, systems)
    for system in sorted(missing):
        rep.warn(f"the system list has no {system!r} system with a command, so it is not wrapped")
    return wrapped


def ensure_retroarch_pins(
    name: str,
    profile: Profile,
    home: Path,
    rep: Reporter,
    *,
    dry_run: bool,
    probes: DeviceProbes = REAL_PROBES,
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
    if busy(rule.process, "RetroArch", what, again, rep, probes):
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
            busy=lambda: busy(rule.process, "RetroArch", what, again, rep, probes),
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
    probes: DeviceProbes = REAL_PROBES,
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
    if not pending or busy(rule.process, "shadPS4", what, again, rep, probes):
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
                busy=lambda: busy(rule.process, "shadPS4", what, again, rep, probes),
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


def _cemu_apply(text: str, mlc: str | None, game_path: str) -> bytes:
    return cemu.apply(text, mlc, game_path).encode("utf-8")


def ensure_cemu_paths(
    name: str,
    profile: Profile,
    home: Path,
    rep: Reporter,
    *,
    dry_run: bool,
    probes: DeviceProbes = REAL_PROBES,
    state_root: Path,
) -> None:
    """Set Cemu's MLC path and add the library's Wii U folder to its game paths.

    Only an existing `settings.xml` is edited (Cemu writes it on its first start, and its
    first-start dialog shows only while there is none), only while Cemu is closed, and only
    those two elements; every other byte is kept. The MLC path stays as it is while the folder
    Cemu uses now holds saves, which would vanish from its view. The old file is backed up
    before the edit and a second run changes nothing.
    """
    rule = profile.cemu
    if rule is None or "library" not in profile.paths:
        return
    library = expand_home(profile.paths["library"], home).rstrip("/")
    mlc, game_path = (expand_home(x, home) for x in rule.wants(library))
    path = Path(expand_home(rule.settings, home))
    again = f"run `warpzone provision device {name}` again"
    try:
        text = path.read_bytes().decode("utf-8")
    except FileNotFoundError:
        rep.warn(
            f"no Cemu settings file found ({path}), so its MLC path and game folder were not "
            f"set: start Cemu once, finish its first-start dialog and close it, then {again}"
        )
        return
    except (OSError, UnicodeDecodeError) as exc:
        rep.warn(f"cannot read {path} ({exc}), so it was left alone")
        return
    try:
        current = cemu.read(text)
        gaps = cemu.missing(text, mlc, game_path)
    except cemu.CemuConfigError as exc:
        rep.warn(f"{path} was left alone: {exc}")
        return
    if not gaps:
        rep.out(f"cemu: {path} already has the MLC path and the game folder")
        return
    want_mlc: str | None = mlc if cemu.MLC in gaps else None
    if want_mlc is not None:
        old = cemu_saves_elsewhere(current, rule.default_mlc, mlc, home)
        if old is not None:
            want_mlc = None
            rep.warn(
                f"Cemu keeps game saves in {old}, which warpzone does not sync, so its mlc_path "
                f"in {path} was left as it is: move the saves in that folder to {mlc}/usr/save "
                f"with Cemu closed, then {again}. After the switch Cemu no longer sees the "
                f"titles, updates and DLC installed in {old.parent.parent}/usr/title either"
            )
    keys = [k for k in gaps if k != cemu.MLC or want_mlc is not None]
    if not keys:
        return
    what = "Cemu's settings.xml"
    if busy(rule.process, "Cemu", what, again, rep, probes):
        return
    shown = " and ".join(keys)
    if dry_run:
        rep.out(f"cemu: dry-run: would set {shown} in {path}")
        return
    try:
        outcome = safefile.guarded_edit(
            path,
            partial(_cemu_apply, text, want_mlc, game_path),
            expect=safefile.stamp(path),
            busy=lambda: busy(rule.process, "Cemu", what, again, rep, probes),
            backup=partial(settings_state.backup, path, "cemu", path.name, root=state_root),
        )
    except cemu.CemuConfigError as exc:
        rep.warn(f"{path} was left alone: {exc}")
        return
    if outcome is safefile.Edit.BUSY:
        return
    if outcome is safefile.Edit.CHANGED:
        rep.warn(f"{path} changed while it was being edited, so it was left alone: {again}")
        return
    rep.out(f"cemu: set {shown} in {path} (old file backed up)")


def cemu_saves_elsewhere(current: cemu.Paths, default: str, mlc: str, home: Path) -> Path | None:
    """The `usr/save` of the MLC folder Cemu uses now, when that is not `mlc` and holds the save
    of a game or a demo (`cemu.is_title_save`); None otherwise.

    A folder that cannot be read counts as holding saves, so `mlc_path` is not switched away
    from saves nobody has seen.
    """
    old = Path(expand_home(cemu.in_use(current, default), home))
    if os.path.realpath(old) == os.path.realpath(mlc):
        return None
    save = old / "usr" / "save"
    for top in cemu.TITLE_SAVES:
        errors: list[OSError] = []
        for _base, _dirs, files in os.walk(save / top, onerror=errors.append):
            if files:
                return save
        if any(not isinstance(e, FileNotFoundError) for e in errors):
            return save
    return None


def ensure_title_links(
    name: str,
    profile: Profile,
    home: Path,
    rep: Reporter,
    *,
    dry_run: bool,
    probes: DeviceProbes = REAL_PROBES,
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
        proc=probes.proc,
        on_busy=rep.defer,
    )


def ensure_shadps4_launch(
    name: str,
    profile: Profile,
    home: Path,
    rep: Reporter,
    *,
    dry_run: bool,
    probes: DeviceProbes = REAL_PROBES,
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
            return procs.running(esde.ESDE_PROCESSES, proc=probes.proc)
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
