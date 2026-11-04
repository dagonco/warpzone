"""`settings apply` and `settings audit`: loading, I/O and output around the pure planning.

The decisions live in `settings_plan`, the file formats in `settings_formats` and
`settings_tree`, the layers in `settings_layers` and the device state in `settings_state`.
This module reads the layers and the emulators' files, asks the plan what to do, and writes
with a backup and an atomic replace. It imports no network code: `capture` is handed a client
with two read calls and asks it whether the `wz-settings` folder is up to date. Messages, log
lines and output name keys and ids, never values (a prompt shows a value, cut, for the keys the
user is asked about; secret keys are never asked about). See "Emulator settings" in
docs/design.md.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sys
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Protocol

from warpzone import procs, safefile
from warpzone import settings_formats as formats
from warpzone import settings_layers as layers_mod
from warpzone import settings_plan as plan
from warpzone import settings_state as state
from warpzone.config import ConfigError
from warpzone.errors import WarpzoneError
from warpzone.paths import expand_home
from warpzone.profiles import DataFile, EmulatorSettings, Profile, SettingsFile
from warpzone.settings_formats import Value
from warpzone.settings_layers import Layer, LayerError

MAX_DATA_BYTES = 1024 * 1024  # a system data file on the hub is at most 1 MiB
FOLDER_MARKER = ".stfolder"
SETTINGS_FOLDER = "wz-settings"  # the Syncthing folder id
COMMON_FILE = "common.toml"
DATA_DIR = "files"
_SKIP_DIRS = (FOLDER_MARKER, ".stversions")


class SettingsError(WarpzoneError):
    """A settings run cannot go on (no /proc, unreadable folder). Exit code 1."""


@dataclass(frozen=True)
class Env:
    """Where things live on this machine; tests point it at temporary directories."""

    home: Path
    state: Path
    proc: Path = Path("/proc")
    clock: Callable[[], datetime] = datetime.now


@dataclass(frozen=True)
class Context:
    env: Env
    device: str
    profile: Profile
    folder: Path  # the local copy of wz-settings


def make_context(device: str, profile: Profile, env: Env) -> Context:
    raw = profile.paths.get("settings")
    if not raw:
        raise ConfigError(f"profile {profile.name!r} has no paths.settings")
    return Context(env, device, profile, Path(expand_home(raw, env.home)))


def select_emulators(profile: Profile, ids: Iterable[str]) -> list[EmulatorSettings]:
    """The profile's emulators in profile order, limited to `ids` when given."""
    wanted = list(dict.fromkeys(ids))
    unknown = [i for i in wanted if i not in profile.settings]
    if unknown:
        known = ", ".join(sorted(profile.settings)) or "none"
        raise ConfigError(
            f"unknown emulator {', '.join(repr(i) for i in unknown)} (profile "
            f"{profile.name!r} has: {known})"
        )
    return [e for e in profile.settings.values() if not wanted or e.id in wanted]


def settings_folder_ready(ctx: Context) -> bool:
    return ctx.folder.is_dir() and (ctx.folder / FOLDER_MARKER).exists()


# ---------------------------------------------------------------------------------------------
# Layers
# ---------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Layers:
    common: Layer
    device: Layer
    effective: dict[tuple[str, str], dict[str, Value]]
    warnings: list[str]
    conflicts: list[str]  # conflict copies found in the folder, ignored
    # file name -> text as loaded (None: the file does not exist); `capture` compares these
    # with a fresh read right before it writes.
    texts: dict[str, str | None]


def _relative_names(folder: Path) -> list[str]:
    names: list[str] = []
    try:
        for entry in os.scandir(folder):
            if entry.name not in _SKIP_DIRS:
                names.append(entry.name)
    except OSError:
        return names
    for root, dirs, files in os.walk(folder / DATA_DIR):
        dirs[:] = [d for d in dirs if d not in _SKIP_DIRS]
        base = Path(root).relative_to(folder).as_posix()
        names.extend(f"{base}/{f}" for f in files)
    return names


def _read_layer(folder: Path, name: str, errors: list[str]) -> tuple[Layer, list[str], str | None]:
    path = folder / name
    try:
        data = path.read_bytes()
    except FileNotFoundError:
        return Layer(), [], None
    except OSError as exc:
        errors.append(f"{name}: cannot be read ({exc.strerror or type(exc).__name__})")
        return Layer(), [], None
    try:
        text = formats.decode(data)
        layer, warnings = layers_mod.parse_layer(text, name)
    except formats.FormatError as exc:
        errors.append(f"{name}: {exc}")
        return Layer(), [], None
    except LayerError as exc:
        errors.extend(exc.errors)
        return Layer(), [], None
    return layer, warnings, text


def load_layers(ctx: Context) -> Layers:
    """Read and validate both layers; any error in either raises `LayerError` (all or nothing)."""
    errors: list[str] = []
    warnings: list[str] = []
    texts: dict[str, str | None] = {}
    parsed: list[Layer] = []
    for name in (COMMON_FILE, f"{ctx.device}.toml"):
        layer, warns, text = _read_layer(ctx.folder, name, errors)
        warnings += warns
        texts[name] = text
        parsed.append(layer)
        errs, warns = layers_mod.validate(layer, ctx.profile, name)
        errors += errs
        warnings += warns
    if errors:
        raise LayerError(errors)
    common, device = parsed
    conflicts = layers_mod.conflict_copies(_relative_names(ctx.folder))
    return Layers(common, device, layers_mod.effective(common, device), warnings, conflicts, texts)


# ---------------------------------------------------------------------------------------------
# Files of an emulator
# ---------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Target:
    """One file of an emulator on this machine: a plain file or an instance of a templated one."""

    emulator: str
    id: str  # the file id, `<file>:<name>` for an instance
    spec: SettingsFile
    path: Path
    shown: str  # the profile's path (with the name filled in), `~` kept


def _valid_name(name: str) -> bool:
    return bool(name) and name not in (".", "..") and not any(c in name for c in "/\0\\")


def template_names(template: str, home: Path) -> list[str]:
    """The names for which the templated path exists as a file (every `{name}` is the same)."""
    parts = expand_home(template, home).split("/")
    found: set[str] = set()

    def walk(i: int, base: str, name: str | None) -> None:
        if i == len(parts):
            if name is not None and os.path.isfile(base):
                found.add(name)
            return
        part = parts[i]
        here = part if i == 0 else f"{base}/{part}"
        if "{name}" not in part:
            walk(i + 1, here, name)
        elif name is not None:
            walk(i + 1, f"{base}/{part.replace('{name}', name)}" if i else here, name)
        else:
            pieces = part.split("{name}")
            rx = re.escape(pieces[0]) + "(?P<n>[^/]+)"
            rx += "".join("(?P=n)" + re.escape(p) for p in pieces[1:])
            pattern = re.compile(rx)
            try:
                entries = os.listdir(base or ".")
            except OSError:
                return
            for entry in entries:
                m = pattern.fullmatch(entry)
                if m and _valid_name(m.group("n")):
                    walk(i + 1, f"{base}/{entry}" if i else entry, m.group("n"))

    walk(0, "", None)
    return sorted(found)


def targets_for(
    emulator: EmulatorSettings,
    entries: Mapping[tuple[str, str], object],
    home: Path,
    *,
    on_disk_only: bool = False,
) -> list[Target]:
    """The files of an emulator: each plain file, and for a templated one the instances that
    exist plus any instance a layer names (`entries` are layer entries; `on_disk_only` leaves the
    named ones out)."""
    targets: list[Target] = []
    for spec in emulator.files.values():
        if not spec.templated:
            targets.append(
                Target(emulator.id, spec.id, spec, Path(expand_home(spec.path, home)), spec.path)
            )
            continue
        names = set(template_names(spec.path, home))
        if not on_disk_only:
            for emu, fid in entries:
                base, sep, name = fid.partition(":")
                if emu == emulator.id and sep and base == spec.id and _valid_name(name):
                    names.add(name)
        for name in sorted(names):
            shown = spec.path.replace("{name}", name)
            targets.append(
                Target(
                    emulator.id,
                    f"{spec.id}:{name}",
                    spec,
                    Path(expand_home(shown, home)),
                    shown,
                )
            )
    return targets


class FileProblem(SettingsError):
    """One file cannot be read or parsed. The message names the file, never a value."""


@dataclass(frozen=True)
class Loaded:
    target: Target
    stamp: safefile.FileStamp  # taken before the read, so a change since then is caught
    text: str
    values: dict[str, Value]
    fixed: frozenset[str]
    handler: formats.Handler


def read_target(target: Target) -> Loaded | None:
    """Read and parse one file once; None when it does not exist."""
    handler = formats.handler(target.spec.format)
    try:
        stamp = safefile.stamp(target.path)
        data = target.path.read_bytes()
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise FileProblem(f"cannot be read ({exc.strerror or type(exc).__name__})") from exc
    try:
        text = formats.decode(data)
        result = handler.read(text)
    except formats.FormatError as exc:
        raise FileProblem(f"cannot be parsed ({exc})") from exc
    return Loaded(target, stamp, text, result.values, result.fixed, handler)


def is_running(env: Env, names: Iterable[str]) -> bool:
    try:
        return procs.running(tuple(names), proc=env.proc)
    except procs.NoProc as exc:
        raise SettingsError(
            f"cannot tell whether an emulator is running: {exc}. "
            "This command needs /proc and runs on the console"
        ) from exc


def require_proc(env: Env) -> None:
    """Refuse early where there is no /proc (macOS), as the design says."""
    is_running(env, ("warpzone-probe",))


# ---------------------------------------------------------------------------------------------
# System data files
# ---------------------------------------------------------------------------------------------


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


@dataclass(frozen=True)
class DataState:
    path: Path
    stamp: safefile.FileStamp | None  # None: the local file does not exist
    data: bytes | None
    hash: str | None
    hub_path: Path
    hub_data: bytes | None
    hub_hash: str | None
    creatable: bool  # create_when is set and exists


def read_data(ctx: Context, emulator: str, entry: DataFile) -> DataState:
    """The local file of a system data entry and its hub copy, each read once."""
    path = Path(expand_home(entry.path, ctx.env.home))
    stamp = data = None
    try:
        stamp = safefile.stamp(path)
        data = path.read_bytes()
    except FileNotFoundError:
        stamp = data = None
    except OSError as exc:
        raise FileProblem(f"cannot be read ({exc.strerror or type(exc).__name__})") from exc
    hub_path = ctx.folder / DATA_DIR / emulator / entry.id
    hub_data = None
    try:
        hub_data = hub_path.read_bytes()
    except FileNotFoundError:
        pass
    except OSError as exc:
        raise FileProblem(
            f"hub copy cannot be read ({exc.strerror or type(exc).__name__})"
        ) from exc
    if hub_data is not None and len(hub_data) > MAX_DATA_BYTES:
        raise FileProblem("hub copy is larger than 1 MiB")
    creatable = bool(entry.create_when) and os.path.isdir(
        expand_home(entry.create_when or "", ctx.env.home)
    )
    return DataState(
        path,
        stamp,
        data,
        None if data is None else sha256(data),
        hub_path,
        hub_data,
        None if hub_data is None else sha256(hub_data),
        creatable,
    )


# ---------------------------------------------------------------------------------------------
# Apply
# ---------------------------------------------------------------------------------------------


class _NullLog:
    def line(self, msg: str) -> None:
        pass


def _quiet(_: str) -> None:
    pass


@dataclass
class _Counts:
    files: int = 0  # files written or created
    written: int = 0  # keys and data files written
    held: int = 0
    cannot: int = 0
    skipped: int = 0  # files left alone this run
    running: int = 0  # emulators skipped
    errors: int = 0

    def info(self, **extra: Any) -> dict[str, Any]:
        return {
            "files": self.files,
            "written": self.written,
            "held": self.held,
            "cannot_write": self.cannot,
            "skipped": self.skipped,
            "running": self.running,
            "errors": self.errors,
            **extra,
        }


def _same_snapshot(a: Mapping[str, Any] | None, b: Mapping[str, Any] | None) -> bool:
    if a is None or b is None:
        return a is b
    return json.dumps(a, sort_keys=True) == json.dumps(b, sort_keys=True)


def _render(
    handler: formats.Handler,
    text: str,
    plans: list[plan.KeyPlan],
    new_snap: dict[str, Value],
    old_snap: Mapping[str, Value] | None,
    current: Mapping[str, Value],
) -> tuple[str, list[plan.KeyPlan]]:
    """The new text. A value the handler refuses becomes a cannot-write key, and its snapshot
    value goes back to what it was, so the next run sees it as before."""
    plans = list(plans)
    changes = {p.key: p.value for p in plans if p.action == plan.WRITE and p.value is not None}
    while changes:
        try:
            return handler.write(text, changes), plans
        except formats.NotWritable as exc:
            if exc.key not in changes:
                raise
            del changes[exc.key]
            plans = [
                plan.KeyPlan(p.key, plan.CANNOT_WRITE, None, "the value cannot be written")
                if p.key == exc.key
                else p
                for p in plans
            ]
            if old_snap and exc.key in old_snap:
                new_snap[exc.key] = old_snap[exc.key]
            elif exc.key in current:
                new_snap[exc.key] = current[exc.key]
            else:
                new_snap.pop(exc.key, None)
    return text, plans


class _Apply:
    def __init__(
        self,
        ctx: Context,
        layers: Layers,
        *,
        dry_run: bool,
        overwrite_local: bool,
        log: state.Log | _NullLog,
        say: Callable[[str], None],
        err: Callable[[str], None],
    ) -> None:
        self.ctx = ctx
        self.env = ctx.env
        self.layers = layers
        self.dry_run = dry_run
        self.overwrite_local = overwrite_local
        self.log = log
        self.say = say
        self.err = err
        self.snapshots = state.Snapshots(ctx.env.state)
        self.counts = _Counts()

    # -- reporting ------------------------------------------------------------------------

    def _event(self, label: str, where: str, key: str = "", note: str = "") -> None:
        text = f"{label:<13}{where}" + (f"  {key}" if key else "")
        self.say(text + (f"  ({note})" if note else ""))
        self.log.line(f"{label} {where} {key}".rstrip())

    def _problem(self, where: str, message: str) -> None:
        self.counts.errors += 1
        self.err(f"error: {where}: {message}")
        self.log.line(f"error {where}: {message}")

    def _skip(self, where: str, why: str) -> None:
        self.counts.skipped += 1
        self._event("skipped", where, note=why)

    # -- emulators ------------------------------------------------------------------------

    def emulator(self, emu: EmulatorSettings) -> None:
        if is_running(self.env, emu.process):
            self.counts.running += 1
            self._event("running", emu.id, note="its files are left alone")
            return
        for target in targets_for(emu, self.layers.effective, self.env.home):
            where = f"{emu.id}/{target.id}"
            try:
                self.file(emu, target)
            except (SettingsError, OSError, ValueError, formats.NotWritable) as exc:
                self._problem(where, str(exc))
        if emu.data:
            try:
                self.data(emu)
            except (SettingsError, OSError, ValueError) as exc:
                self._problem(f"{emu.id}/data", str(exc))

    # -- config files ---------------------------------------------------------------------

    def file(self, emu: EmulatorSettings, target: Target) -> None:
        spec, fid = target.spec, target.id
        where = f"{emu.id}/{fid}"
        layered = self.layers.effective.get((emu.id, fid), {})
        try:
            old_snap = self.snapshots.load(emu.id, fid)
        except state.StateError:
            raise SettingsError(
                "snapshot is corrupt: delete it to start again, nothing was written"
            ) from None
        loaded = read_target(target)
        if loaded is None:
            if not (spec.create and layered):
                return  # never opened: nothing to do
            handler, text = formats.handler(spec.format), ""
            current: dict[str, Value] = {}
            fixed: frozenset[str] = frozenset()
        else:
            handler, text, current, fixed = loaded.handler, loaded.text, loaded.values, loaded.fixed
        plans, new_snap = plan.plan_apply(
            current,
            fixed,
            layered,
            old_snap,
            can_add=handler.can_add,
            overwrite_local=self.overwrite_local,
            spec=spec,
        )
        new_text, plans = _render(handler, text, plans, new_snap, old_snap, current)
        if new_text != text:
            if not self._replace(emu, target, loaded, new_text, where):
                return
            self.counts.files += 1
            if loaded is None:
                self._event("would create" if self.dry_run else "created", where)
        for p in plans:
            if p.action == plan.WRITE and new_text != text:
                self.counts.written += 1
                self._event("would write" if self.dry_run else "wrote", where, p.key, p.reason)
            elif p.action == plan.HELD:
                self.counts.held += 1
                self._event("held", where, p.key, p.reason)
            elif p.action == plan.CANNOT_WRITE:
                self.counts.cannot += 1
                self._event("cannot write", where, p.key, p.reason)
        if not self.dry_run and not _same_snapshot(old_snap, new_snap):
            self.snapshots.save(emu.id, fid, new_snap)

    def _replace(
        self,
        emu: EmulatorSettings,
        target: Target,
        loaded: Loaded | None,
        new_text: str,
        where: str,
    ) -> bool:
        """Back up, check once more, and replace. False: the file was left alone this run."""
        if self.dry_run:
            return True
        if loaded is not None:
            state.backup(target.path, emu.id, target.id, now=self.env.clock(), root=self.env.state)
        if is_running(self.env, emu.process):
            self._skip(where, "the emulator started meanwhile")
            return False
        expect = loaded.stamp if loaded is not None else None
        if not safefile.atomic_write(target.path, new_text.encode("utf-8"), expect=expect):
            self._skip(where, "the file changed while it was being processed")
            return False
        return True

    # -- system data files ----------------------------------------------------------------

    def data(self, emu: EmulatorSettings) -> None:
        try:
            old = self.snapshots.load_data(emu.id)
        except state.StateError:
            raise SettingsError("data snapshot is corrupt: delete it to start again") from None
        new: dict[str, str] = dict(old or {})
        for entry in emu.data.values():
            where = f"{emu.id}/data/{entry.id}"
            try:
                self._data_file(emu, entry, where, (old or {}).get(entry.id), new)
            except (SettingsError, OSError) as exc:
                self._problem(where, str(exc))
        if not self.dry_run and not _same_snapshot(old, new):
            self.snapshots.save_data(emu.id, new)

    def _data_file(
        self,
        emu: EmulatorSettings,
        entry: DataFile,
        where: str,
        snap_hash: str | None,
        new: dict[str, str],
    ) -> None:
        st = read_data(self.ctx, emu.id, entry)
        action = plan.plan_data(
            st.hash, st.hub_hash, snap_hash, overwrite_local=self.overwrite_local
        )
        if action == plan.WRITE:
            if st.data is None and not st.creatable:
                return  # not installed: skipped like a missing config file
            assert st.hub_data is not None
            if not self.dry_run:
                if st.data is not None:
                    state.backup(
                        st.path,
                        emu.id,
                        f"data:{entry.id}",
                        now=self.env.clock(),
                        root=self.env.state,
                    )
                if is_running(self.env, emu.process):
                    self._skip(where, "the emulator started meanwhile")
                    return
                if not safefile.atomic_write(st.path, st.hub_data, expect=st.stamp):
                    self._skip(where, "the file changed while it was being processed")
                    return
            self.counts.written += 1
            self.counts.files += 1
            note = plan.REASON_OVERWRITE if st.hash and snap_hash and st.hash != snap_hash else ""
            self._event("would write" if self.dry_run else "wrote", where, note=note)
        elif action == plan.HELD:
            self.counts.held += 1
            self._event("held", where, note=plan.REASON_HELD)
        keep = plan.data_snapshot(action, st.hash, st.hub_hash, snap_hash)
        if keep is None:
            new.pop(entry.id, None)
        else:
            new[entry.id] = keep


def apply(
    ctx: Context,
    *,
    emulators: Iterable[str] = (),
    dry_run: bool = False,
    quiet: bool = False,
    overwrite_local: bool = False,
    out: Callable[[str], None] = print,
    err: Callable[[str], None] = print,
) -> int:
    """Write the layered keys into the emulators' files. Exit code: 0 ok, 1 on an invalid layer
    or a file that could not be read, parsed or written."""
    selected = select_emulators(ctx.profile, emulators)
    say, complain = (_quiet, _quiet) if quiet else (out, err)
    if dry_run:
        return _apply_locked(ctx, selected, _NullLog(), say, complain, dry_run, overwrite_local)
    with state.lock(root=ctx.env.state) as got:
        log = state.Log(ctx.env.state / "log", clock=ctx.env.clock)
        if not got:
            log.line("apply: another run holds the lock, nothing done")
            say("settings: another run is in progress, nothing done")
            return 0
        return _apply_locked(ctx, selected, log, say, complain, dry_run, overwrite_local)


def unlayerable(profile: Profile, cand: plan.Candidate) -> str | None:
    """Why a captured key cannot be held by a layer (as `validate` judges it), else None.

    The reason never holds the value.
    """
    layer = Layer({(cand.emulator, cand.file): {cand.key: cand.here}})
    errors, _ = layers_mod.validate(layer, profile, "capture")
    if not errors:
        return None
    return errors[0].partition(f": {cand.key}: ")[2] or "a layer cannot hold it"


def record_refused_apply(
    root: Path, code: int, clock: Callable[[], datetime] = datetime.now
) -> None:
    """Leave a trace of an `apply` that failed before it ran (invalid or missing config).

    Doctor reads it as a failed run, so a startup script that cannot start does not look like
    "no run yet" forever. The message is fixed: it never holds a value or a path.
    """
    try:
        state.Log(root / "log", clock=clock).line(
            f"apply: refused before running (exit {code}): the config could not be used; "
            "run `warpzone check`"
        )
        state.write_last_apply(
            _Counts().info(time=clock().isoformat(timespec="seconds"), exit=code), root=root
        )
    except OSError:
        pass  # nowhere to write: the error was already printed


def _apply_locked(
    ctx: Context,
    selected: list[EmulatorSettings],
    log: state.Log | _NullLog,
    say: Callable[[str], None],
    err: Callable[[str], None],
    dry_run: bool,
    overwrite_local: bool,
) -> int:
    env = ctx.env
    if not settings_folder_ready(ctx):
        log.line("apply: no settings folder")
        say("settings: no settings folder, nothing to do")
        return 0
    require_proc(env)
    log.line(f"apply: device {ctx.device}")

    def finish(code: int, counts: _Counts) -> int:
        if not dry_run:
            state.write_last_apply(
                counts.info(time=env.clock().isoformat(timespec="seconds"), exit=code),
                root=env.state,
            )
        return code

    try:
        layers = load_layers(ctx)
    except LayerError as exc:
        counts = _Counts(errors=len(exc.errors))
        for message in exc.errors:
            err(f"error: {message}")
            log.line(f"error: {message}")
        say("settings: a layer is invalid, nothing was changed")
        log.line("apply: layers invalid, nothing changed")
        return finish(1, counts)
    for message in layers.warnings:
        say(f"warning: {message}")
        log.line(f"warning: {message}")
    for name in layers.conflicts:
        say(f"warning: ignoring conflict copy {name}")
        log.line(f"ignoring conflict copy {name}")
    run = _Apply(
        ctx,
        layers,
        dry_run=dry_run,
        overwrite_local=overwrite_local,
        log=log,
        say=say,
        err=err,
    )
    for emu in selected:
        run.emulator(emu)
    c = run.counts
    summary = (
        f"settings: {c.written} written in {c.files} file(s), {c.held} held, "
        f"{c.cannot} cannot write, {c.skipped} skipped, {c.running} emulator(s) running"
        + (f", {c.errors} error(s)" if c.errors else "")
    )
    say(("(dry run) " if dry_run else "") + summary)
    log.line(f"apply: {summary}")
    return finish(1 if c.errors else 0, c)


# ---------------------------------------------------------------------------------------------
# Audit
# ---------------------------------------------------------------------------------------------


def audit_report(
    ctx: Context, *, emulators: Iterable[str] = ()
) -> tuple[list[plan.AuditRow], list[str]]:
    """The audit rows and the warnings. Reads only; raises `LayerError` for an invalid layer."""
    selected = select_emulators(ctx.profile, emulators)
    layers = load_layers(ctx)
    env = ctx.env
    snapshots = state.Snapshots(env.state)
    rows: list[plan.AuditRow] = []
    for emu in selected:
        try:
            busy = procs.running(emu.process, proc=env.proc)
        except procs.NoProc:
            busy = False  # nothing to compare with; the files are still read
        if busy:
            rows.append(plan.row_running(emu.id))
            continue
        for target in targets_for(emu, layers.effective, env.home):
            rows += _audit_file(emu, target, layers, snapshots)
        for entry in emu.data.values():
            rows += _audit_data(ctx, emu, entry, snapshots)
    return rows, layers.warnings


def _audit_file(
    emu: EmulatorSettings, target: Target, layers: Layers, snapshots: state.Snapshots
) -> list[plan.AuditRow]:
    layered = layers.effective.get((emu.id, target.id), {})
    try:
        loaded = read_target(target)
    except FileProblem as exc:
        return [plan.row_invalid(emu.id, target.id, str(exc))]
    if loaded is None:
        return [plan.row_no_file(emu.id, target.id, creates=target.spec.create and bool(layered))]
    try:
        snapshot = snapshots.load(emu.id, target.id)
    except (state.StateError, ValueError):
        return [plan.row_invalid(emu.id, target.id, "the snapshot is corrupt")]
    return plan.audit_rows(
        emu.id,
        target.id,
        target.shown,
        target.spec,
        loaded.values,
        loaded.fixed,
        layered,
        snapshot,
        can_add=loaded.handler.can_add,
    )


def _audit_data(
    ctx: Context, emu: EmulatorSettings, entry: DataFile, snapshots: state.Snapshots
) -> list[plan.AuditRow]:
    try:
        st = read_data(ctx, emu.id, entry)
        snapshot = snapshots.load_data(emu.id)
    except FileProblem as exc:
        return [plan.row_invalid(emu.id, entry.id, str(exc))]
    except (state.StateError, ValueError):
        return [plan.row_invalid(emu.id, entry.id, "the snapshot is corrupt")]
    return plan.audit_data(
        emu.id,
        entry.id,
        st.hash,
        st.hub_hash,
        (snapshot or {}).get(entry.id),
        creatable=st.creatable,
    )


def audit(
    ctx: Context,
    *,
    emulators: Iterable[str] = (),
    all_keys: bool = False,
    out: Callable[[str], None] = print,
) -> int:
    """Report drift between the layers and the files. Writes nothing, state included."""
    select_emulators(ctx.profile, emulators)  # a usage error comes before any output
    if not settings_folder_ready(ctx):
        out("settings: no settings folder, nothing to audit")
        return 0
    try:
        rows, warnings = audit_report(ctx, emulators=emulators)
    except LayerError as exc:
        for message in exc.errors:
            out(f"invalid  {message}")
        out("summary: a layer is invalid")
        return 1
    for message in warnings:
        out(f"warning: {message}")
    out(plan.render_audit(rows, all_keys=all_keys))
    return plan.audit_exit_code(rows)


# ---------------------------------------------------------------------------------------------
# Capture
# ---------------------------------------------------------------------------------------------


class SyncClient(Protocol):
    """The two read calls of the Syncthing client that `capture` needs."""

    def db_status(self, folder_id: str) -> dict[str, Any]: ...

    def db_file(self, folder_id: str, name: str) -> dict[str, Any] | None: ...


def _items_needed(status: Mapping[str, Any]) -> int:
    total = status.get("needTotalItems")
    if isinstance(total, int):
        return total
    return sum(int(status.get(k) or 0) for k in ("needFiles", "needDirectories", "needDeletes"))


def _record_differs(info: Mapping[str, Any]) -> bool:
    local, global_ = info.get("local"), info.get("global")
    if local is None and global_ is None:
        return False
    if local is None or global_ is None or local.get("invalid"):
        return True
    if local.get("deleted") and global_.get("deleted"):
        return False
    return bool(local.get("deleted")) != bool(global_.get("deleted")) or local.get(
        "version"
    ) != global_.get("version")


def sync_problems(
    status: Mapping[str, Any], infos: Mapping[str, Mapping[str, Any] | None]
) -> list[str]:
    """Why the local copy of `wz-settings` cannot be trusted to be current (pure).

    `status` is `GET /rest/db/status`, `infos` the `GET /rest/db/file` answer per layer file
    (None: Syncthing has no such object yet, which is fine for a layer not written so far).
    """
    problems: list[str] = []
    state_name = status.get("state")
    if state_name != "idle":
        problems.append(f"{SETTINGS_FOLDER} is {state_name or 'in an unknown state'}, not idle")
    need = _items_needed(status)
    if need:
        problems.append(f"{SETTINGS_FOLDER} still has {need} item(s) to sync")
    if status.get("error"):
        problems.append(f"{SETTINGS_FOLDER} reports an error")
    for name, info in infos.items():
        if info is not None and _record_differs(info):
            problems.append(f"{name}: the local copy is not the one on the hub yet")
    return problems


def check_sync(ctx: Context, client: SyncClient) -> list[str]:
    names = (COMMON_FILE, f"{ctx.device}.toml")
    status = client.db_status(SETTINGS_FOLDER)
    return sync_problems(status, {name: client.db_file(SETTINGS_FOLDER, name) for name in names})


_ANSWERS = {"c": "common", "d": "device", "x": "discard", "s": "skip", "q": "quit"}


def _short(value: Value | None) -> str:
    return plan._cell(value)


def _digest(value: Value | None) -> str:
    return "-" if value is None else str(value)[:12]


def describe(cand: plan.Candidate, device: str) -> str:
    """The values line of a candidate: cut values for a key, size and hash prefixes (never the
    content) for a system data file."""
    if cand.kind == "data":
        return (
            f"here {_digest(cand.here)} ({cand.size} bytes)   before {_digest(cand.before)}"
            f"   hub {_digest(cand.common)}"
        )
    return (
        f"here {_short(cand.here)}   before {_short(cand.before)}   "
        f"common {_short(cand.common)}   {device} {_short(cand.device)}"
    )


def _choices(cand: plan.Candidate, device: str) -> str:
    common = "[C]ommon" if cand.preselect == "common" else "[c]ommon"
    parts = [common]
    if cand.kind == "key":
        parts.append(
            f"[D]evice ({device})" if cand.preselect == "device" else f"[d]evice ({device})"
        )
    parts += ["[x] discard", "[s]kip", "[q]uit"]
    return "  ".join(parts) + " > "


def _read_answer(
    cand: plan.Candidate, device: str, ask: Callable[[str], str], out: Callable[[str], None]
) -> str:
    """One decision; an empty answer takes the pre-selection, end of input quits."""
    allowed = {k: v for k, v in _ANSWERS.items() if cand.kind == "key" or v != "device"}
    while True:
        try:
            raw = ask("  " + _choices(cand, device)).strip().lower()
        except EOFError:
            return "quit"
        if not raw:
            return cand.preselect
        if raw in allowed:
            return allowed[raw]
        out("  answer " + ", ".join(allowed) + " or press Enter")


def prompt_decisions(
    cands: Iterable[plan.Candidate],
    device: str,
    ask: Callable[[str], str],
    out: Callable[[str], None],
) -> dict[str, str]:
    """Ask for every candidate, in order; `q` (or end of input) leaves the rest undecided."""
    decisions: dict[str, str] = {}
    for cand in cands:
        out(f"{cand.emulator}  {cand.file}  {cand.key}")
        out("  " + describe(cand, device))
        answer = _read_answer(cand, device, ask, out)
        if answer == "quit":
            break
        decisions[cand.ref] = answer
    return decisions


@dataclass
class _Scanned:
    """A file read by capture, kept for the snapshot written afterwards."""

    emulator: str
    id: str
    spec: SettingsFile
    loaded: Loaded
    snapshot: dict[str, Value] | None
    proposed: list[str]


class _Capture:
    def __init__(
        self,
        ctx: Context,
        layers: Layers,
        *,
        seed: bool,
        key_patterns: tuple[str, ...],
        include_new: bool,
        log: state.Log | _NullLog,
        say: Callable[[str], None],
        err: Callable[[str], None],
    ) -> None:
        self.ctx = ctx
        self.env = ctx.env
        self.layers = layers
        self.seed = seed
        self.key_patterns = key_patterns
        self.include_new = include_new
        self.log = log
        self.say = say
        self.err = err
        self.snapshots = state.Snapshots(ctx.env.state)
        self.cands: list[plan.Candidate] = []
        self.scanned: list[_Scanned] = []
        self.data_states: dict[str, DataState] = {}
        self.data_old: dict[str, dict[str, str]] = {}
        self.fresh = 0
        self.errors = 0

    def note(self, where: str, text: str) -> None:
        self.say(f"note: {where}: {text}")
        self.log.line(f"note {where}: {text}")

    def problem(self, where: str, message: str) -> None:
        self.errors += 1
        self.err(f"error: {where}: {message}")
        self.log.line(f"error {where}: {message}")

    # -- reading --------------------------------------------------------------------------

    def scan(self, emu: EmulatorSettings) -> None:
        if is_running(self.env, emu.process):
            self.note(emu.id, "running, its files may not hold its latest changes; skipped")
            return
        for target in targets_for(emu, {}, self.env.home):
            try:
                self.scan_file(emu, target)
            except (SettingsError, OSError, ValueError) as exc:
                self.problem(f"{emu.id}/{target.id}", str(exc))
        if emu.data:
            self.scan_data(emu)

    def scan_file(self, emu: EmulatorSettings, target: Target) -> None:
        where = f"{emu.id}/{target.id}"
        loaded = read_target(target)
        if loaded is None:
            return
        try:
            snap = self.snapshots.load(emu.id, target.id)
        except (state.StateError, ValueError):
            raise SettingsError("snapshot is corrupt: delete it to start again") from None
        if snap is None and not self.seed:
            self.note(where, "no baseline: run `settings apply` first, or `capture --seed`")
            return
        table = (emu.id, target.id)
        found, fresh = plan.candidates(
            emu.id,
            target.id,
            target.spec,
            loaded.values,
            loaded.fixed,
            snap,
            self.layers.common.entries.get(table, {}),
            self.layers.device.entries.get(table, {}),
            seed=self.seed,
            key_patterns=self.key_patterns,
            include_new=self.include_new,
        )
        self.cands += found
        self.fresh += fresh
        self.scanned.append(
            _Scanned(emu.id, target.id, target.spec, loaded, snap, [c.key for c in found])
        )

    def scan_data(self, emu: EmulatorSettings) -> None:
        try:
            old = self.snapshots.load_data(emu.id)
        except (state.StateError, ValueError):
            self.problem(f"{emu.id}/data", "data snapshot is corrupt: delete it to start again")
            return
        self.data_old[emu.id] = dict(old or {})
        for entry in emu.data.values():
            where = f"{emu.id}/data/{entry.id}"
            try:
                st = read_data(self.ctx, emu.id, entry)
            except (SettingsError, OSError) as exc:
                self.problem(where, str(exc))
                continue
            if st.data is None or st.hash == st.hub_hash:
                continue
            if len(st.data) > MAX_DATA_BYTES:
                self.note(where, "larger than 1 MiB; it cannot go to the hub")
                continue
            snap_hash = self.data_old[emu.id].get(entry.id)
            cand = plan.data_candidate(
                emu.id,
                entry.id,
                st.hash,
                len(st.data),
                snap_hash,
                st.hub_hash,
                seed=self.seed,
                key_patterns=self.key_patterns,
            )
            if cand is None:
                if snap_hash is None:
                    self.note(
                        where,
                        f"no baseline: run `capture --seed --key '{emu.id}/data/{entry.id}'` "
                        "(not `settings apply`, which would take this file as the baseline)",
                    )
                continue
            self.cands.append(cand)
            self.data_states[cand.ref] = st

    def other_layers(self) -> dict[str, plan.Entries]:
        """The layers of the other devices, for the notes only; unreadable ones are left out."""
        own = {COMMON_FILE, f"{self.ctx.device}.toml"}
        found: dict[str, plan.Entries] = {}
        try:
            names = sorted(os.listdir(self.ctx.folder))
        except OSError:
            return found
        for name in names:
            if not name.endswith(".toml") or name in own or ".sync-conflict-" in name:
                continue
            try:
                text = formats.decode((self.ctx.folder / name).read_bytes())
                found[name.removesuffix(".toml")] = layers_mod.parse_layer(text, name)[0].entries
            except (OSError, formats.FormatError, LayerError):
                continue
        return found

    # -- writing --------------------------------------------------------------------------

    def verify_and_write(
        self,
        changes: list[tuple[str, dict[tuple[str, str], dict[str, Value]]]],
        copies: list[plan.Candidate],
    ) -> bool:
        """Check that nothing on the hub moved since it was read, then write it all; False
        (with an error shown) means nothing was written."""
        todo: list[tuple[Path, bytes, safefile.FileStamp | None]] = []
        for cand in copies:
            st = self.data_states[cand.ref]
            where = f"{cand.emulator}/data/{cand.key}"
            try:
                data = st.path.read_bytes()
            except OSError:
                self.problem(where, "the file changed while it was being processed")
                return False
            if sha256(data) != cand.here:
                self.problem(where, "the file changed while it was being processed")
                return False
            hub_stamp = None
            try:
                hub_stamp = safefile.stamp(st.hub_path)
                hub = st.hub_path.read_bytes()
            except FileNotFoundError:
                hub = None
            if (None if hub is None else sha256(hub)) != st.hub_hash:
                self.problem(where, "the hub copy changed while it was being processed")
                return False
            todo.append((st.hub_path, data, hub_stamp if hub is not None else None))
        for name, entries in changes:
            path = self.ctx.folder / name
            try:
                stamp = safefile.stamp(path)
                current: str | None = formats.decode(path.read_bytes())
            except FileNotFoundError:
                stamp, current = None, None
            except (OSError, formats.FormatError) as exc:
                self.problem(name, f"cannot be read again ({exc})")
                return False
            if current != self.layers.texts.get(name):
                self.problem(
                    name, "changed on the hub while it was being processed; nothing written"
                )
                return False
            todo.append((path, layers_mod.render(Layer(entries)).encode("utf-8"), stamp))
        for path, data, stamp in todo:
            if not safefile.atomic_write(path, data, expect=stamp):
                self.problem(path.name, "changed while it was being written; stopped")
                return False
        return True

    def save_snapshots(self, updates: Mapping[tuple[str, str], Mapping[str, Value]]) -> None:
        for sc in self.scanned:
            new = plan.capture_snapshot(
                sc.snapshot,
                sc.loaded.values,
                sc.loaded.fixed,
                sc.spec,
                updates.get((sc.emulator, sc.id), {}),
                proposed=sc.proposed,
            )
            if not _same_snapshot(sc.snapshot, new):
                self.snapshots.save(sc.emulator, sc.id, new)
        for emulator, old in self.data_old.items():
            new_hashes = {
                **old,
                **{k: str(v) for k, v in updates.get((emulator, "data"), {}).items()},
            }
            if new_hashes != old:
                self.snapshots.save_data(emulator, new_hashes)


def _report(run: _Capture, device: str) -> None:
    for cand in run.cands:
        run.say(cand.ref)
        run.say("  " + describe(cand, device))
    run.say(
        f"settings capture: report only, {len(run.cands)} key(s) would be proposed, "
        f"{run.fresh} new key(s) not shown. Run on a terminal, or pass --yes, --to-common, "
        "--to-device or --discard to decide"
    )


def capture(
    ctx: Context,
    client: SyncClient | None,
    *,
    emulators: Iterable[str] = (),
    seed: bool = False,
    key: Iterable[str] = (),
    include_new: bool = False,
    yes: bool = False,
    to_common: Iterable[str] = (),
    to_device: Iterable[str] = (),
    discard: Iterable[str] = (),
    offline: bool = False,
    dry_run: bool = False,
    interactive: bool | None = None,
    ask: Callable[[str], str] = input,
    out: Callable[[str], None] = print,
    err: Callable[[str], None] = print,
) -> int:
    """Take the keys changed in the emulators' own interface to the layers on the hub.

    Exit code: 0 ok (also for a report), 1 for a refusal (Syncthing not in sync, a conflict copy,
    an invalid layer, a layer that changed meanwhile) or an error on a file. `interactive`
    defaults to whether stdin is a terminal; `ask` reads one answer.
    """
    selected = select_emulators(ctx.profile, emulators)
    patterns = tuple(key)
    if patterns and not seed:
        raise ConfigError("--key only works together with --seed")
    if client is None and not offline:
        raise SettingsError(
            "no Syncthing to ask whether wz-settings is up to date (--offline skips it)"
        )
    flags = (tuple(to_common), tuple(to_device), tuple(discard))
    if interactive is None:
        interactive = sys.stdin.isatty()
    report = not interactive and not (yes or any(flags))
    if dry_run or report:
        return _capture_locked(
            ctx, client, selected, _NullLog(), out, err, patterns=patterns, flags=flags,
            seed=seed, include_new=include_new, yes=yes, offline=offline, dry_run=dry_run,
            report=report, interactive=interactive, ask=ask,
        )  # fmt: skip
    with state.lock(root=ctx.env.state) as got:
        log = state.Log(ctx.env.state / "log", clock=ctx.env.clock)
        if not got:
            log.line("capture: another run holds the lock, nothing done")
            err("settings: another run is in progress, nothing done")
            return 1
        return _capture_locked(
            ctx, client, selected, log, out, err, patterns=patterns, flags=flags,
            seed=seed, include_new=include_new, yes=yes, offline=offline, dry_run=dry_run,
            report=report, interactive=interactive, ask=ask,
        )  # fmt: skip


def _capture_locked(
    ctx: Context,
    client: SyncClient | None,
    selected: list[EmulatorSettings],
    log: state.Log | _NullLog,
    out: Callable[[str], None],
    err: Callable[[str], None],
    *,
    patterns: tuple[str, ...],
    flags: tuple[tuple[str, ...], tuple[str, ...], tuple[str, ...]],
    seed: bool,
    include_new: bool,
    yes: bool,
    offline: bool,
    dry_run: bool,
    report: bool,
    interactive: bool,
    ask: Callable[[str], str],
) -> int:
    def refuse(message: str) -> int:
        err(f"error: {message}")
        log.line(f"capture: refused: {message}")
        return 1

    if not settings_folder_ready(ctx):
        return refuse("no settings folder: this device does not subscribe to settings")
    require_proc(ctx.env)
    log.line(f"capture: device {ctx.device}")
    if not offline:
        assert client is not None
        problems = check_sync(ctx, client)
        if problems:
            for message in problems:
                err(f"error: {message}")
            return refuse(
                "the settings folder is not up to date; try again when Syncthing has finished "
                "(--offline skips this check)"
            )
    try:
        layers = load_layers(ctx)
    except LayerError as exc:
        for message in exc.errors:
            err(f"error: {message}")
        return refuse("a layer is invalid, nothing was changed")
    if layers.conflicts:
        for name in layers.conflicts:
            err(f"error: conflict copy {name}")
        return refuse(
            "a layer has a conflict copy: keep one version on the hub and delete the other"
        )
    for message in layers.warnings:
        out(f"warning: {message}")
    run = _Capture(
        ctx, layers, seed=seed, key_patterns=patterns, include_new=include_new,
        log=log, say=out, err=err,
    )  # fmt: skip
    for emu in selected:
        run.scan(emu)
    if report:
        _report(run, ctx.device)
        return 1 if run.errors else 0
    to_common, to_device, discard = flags
    decisions = plan.decide(
        run.cands, to_common=to_common, to_device=to_device, discard=discard, yes=yes
    )
    pending = [c for c in run.cands if c.ref not in decisions]
    if interactive and pending:
        decisions.update(prompt_decisions(pending, ctx.device, ask, out))
    for cand in run.cands:
        if cand.kind == "key" and decisions.get(cand.ref) in ("common", "device"):
            reason = unlayerable(ctx.profile, cand)
            if reason:
                run.problem(cand.ref, f"not captured: {reason}")
                decisions[cand.ref] = "skip"
    others = run.other_layers() if "common" in decisions.values() else {}
    common_after, device_after, updates, notes = plan.apply_decisions(
        layers.common.entries, layers.device.entries, decisions, run.cands, others=others
    )
    for message in notes:
        out(f"note: {message}")
    for name, entries in ((COMMON_FILE, common_after), (f"{ctx.device}.toml", device_after)):
        errors, _ = layers_mod.validate(Layer(entries), ctx.profile, name)
        if errors:  # the per-key check above should have caught it; never write an invalid layer
            for message in errors:
                err(f"error: {message}")
            return refuse("the result would be an invalid layer, nothing was written")
    prefix = "would " if dry_run else ""
    device_name = f"{ctx.device}.toml"
    changes: list[tuple[str, dict[tuple[str, str], dict[str, Value]]]] = []
    n_set = n_removed = 0
    for name, before, after in (
        (COMMON_FILE, layers.common.entries, common_after),
        (device_name, layers.device.entries, device_after),
    ):
        set_refs, removed = plan.layer_diff(before, after)
        if set_refs or removed:
            changes.append((name, after))
        n_set, n_removed = n_set + len(set_refs), n_removed + len(removed)
        for ref in set_refs:
            out(f"{prefix}set      {name}  {ref}")
        for ref in removed:
            out(f"{prefix}removed  {name}  {ref}")
    copies = [c for c in run.cands if c.kind == "data" and decisions.get(c.ref) == "common"]
    for cand in copies:
        out(f"{prefix}copy     {DATA_DIR}/{cand.emulator}/{cand.key}  ({cand.size} bytes)")
    values = list(decisions.values())
    summary = (
        f"settings capture: {n_set} set, {n_removed} removed, {len(copies)} data file(s) copied, "
        f"{values.count('discard')} discarded, {len(run.cands) - len(decisions)} skipped, "
        f"{run.fresh} new key(s) not shown"
    )
    if not dry_run:
        if not run.verify_and_write(changes, copies):
            return refuse("nothing more was written")
        run.save_snapshots(updates)
    out(("(dry run) " if dry_run else "") + summary)
    log.line(summary)
    return 1 if run.errors else 0
