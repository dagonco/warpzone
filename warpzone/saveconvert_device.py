"""Save conversion on a device: the files, the state and the backups.

`convert` reconciles, for each `[save_convert.*]` rule of the profile, the raw cartridge
saves the hub syncs with the emulator's own files, as `saveconvert.decide` says. Nothing is
written while the emulator runs (checked again right before each write), every file it
replaces is backed up first, and a write is atomic. Deletions are never propagated. See
development/design/save-conversion.md.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from warpzone import procs, safefile, saveconvert, settings_state
from warpzone.paths import expand_home
from warpzone.profiles import Profile, SaveConvert
from warpzone.saveconvert import Action, SaveFormatError, Side

Say = Callable[[str], None]

BACKUPS_KEPT = 3
# The copy a latest-wins conflict replaced is kept apart from the routine backups, whose
# rotation would otherwise drop it after a few ordinary sessions.
CONFLICT_BACKUPS_KEPT = 10
CONFLICT_SUFFIX = ".conflicts"
CONFLICTS_KEPT = 20
STATE_FILE = "state.json"


def state_root(home: Path, root: Path | None = None) -> Path:
    """The device folder of the converter's state, outside every synced folder."""
    return (root or settings_state.state_dir(home=home)).parent / "save-convert"


# ------------------------------------------------------------------ state


def parse_state(text: str | None) -> dict[str, Any]:
    """The state a file holds, with the missing parts filled in; anything unusable is empty."""
    empty: dict[str, Any] = {"bases": {}, "last": None, "conflicts": []}
    if text is None:
        return empty
    try:
        data = json.loads(text)
    except ValueError:
        return empty
    if not isinstance(data, dict):
        return empty
    bases, last, conflicts = data.get("bases"), data.get("last"), data.get("conflicts")
    return {
        "bases": bases if isinstance(bases, dict) else {},
        "last": last if isinstance(last, dict) else None,
        "conflicts": conflicts if isinstance(conflicts, list) else [],
    }


def _load_state(root: Path, warn: Say) -> dict[str, Any]:
    path = root / STATE_FILE
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return parse_state(None)
    except (OSError, UnicodeDecodeError) as exc:
        warn(f"cannot read {path} ({exc}), so every game is compared without a base")
        return parse_state(None)
    return parse_state(text)


def _save_state(root: Path, state: dict[str, Any]) -> None:
    text = json.dumps(state, indent=1, sort_keys=True) + "\n"
    safefile.write_file(root / STATE_FILE, text.encode())


# ------------------------------------------------------------------ files


@dataclass
class Outcome:
    """What a run did, for the caller to report."""

    written: int = 0
    current: int = 0
    skipped: list[tuple[str, str]] = field(default_factory=list)
    conflicts: list[tuple[str, str]] = field(default_factory=list)
    # Syncthing conflict copies found in the saves folders; they are reported, never touched.
    conflict_files: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    busy: bool = False


@dataclass
class _Run:
    """What one run shares between its rules and games."""

    say: Say
    warn: Say
    dry_run: bool
    proc: Path
    root: Path
    clock: Callable[[], datetime]
    on_busy: Callable[[], None] | None
    state: dict[str, Any]
    outcome: Outcome


def games(folder: Path, suffix: str) -> dict[str, Path]:
    """The regular save files of `folder` by game (file name without `suffix`).

    Syncthing's temporary files, conflict copies and our own temporary files are not saves.
    """
    found: dict[str, Path] = {}
    try:
        entries = sorted(folder.iterdir())
    except OSError:
        return found
    for entry in entries:
        name = entry.name
        if (
            name.startswith(".")
            or name.endswith(safefile.TMP_SUFFIX)
            or ".sync-conflict-" in name
            or not name.endswith(suffix)
            or len(name) == len(suffix)
            or "\\" in name
            or entry.is_symlink()
            or not entry.is_file()
        ):
            continue
        found[name[: -len(suffix)]] = entry
    return found


def conflict_copies(folder: Path) -> list[str]:
    """The Syncthing conflict copies (`*.sync-conflict-*`) in `folder`, by name."""
    try:
        return sorted(
            e.name for e in folder.iterdir() if ".sync-conflict-" in e.name and e.is_file()
        )
    except OSError:
        return []


def _read(path: Path | None) -> tuple[bytes, safefile.FileStamp] | None:
    if path is None:
        return None
    try:
        stamp = safefile.stamp(path)  # before the read: a change in between refuses the write
        return path.read_bytes(), stamp
    except FileNotFoundError:
        return None


def _running(names: tuple[str, ...], proc: Path) -> bool | None:
    """Whether a program of `names` runs; None when that cannot be told."""
    try:
        return procs.running(names, proc=proc)
    except procs.NoProc:
        return None


def convert(
    device: str,
    profile: Profile,
    home: Path,
    *,
    say: Say,
    warn: Say,
    dry_run: bool = False,
    proc: Path = Path("/proc"),
    state_root_dir: Path | None = None,
    now: Callable[[], datetime] | None = None,
    on_busy: Callable[[], None] | None = None,
) -> Outcome:
    """Reconcile the saves of every `[save_convert.*]` rule; return what happened.

    With `dry_run` it says what it would write and writes nothing, not even the state or the
    lock file. `on_busy` is called when the emulator runs.
    """
    root = state_root(home, state_root_dir)
    run = _Run(
        say,
        warn,
        dry_run,
        proc,
        root,
        now or (lambda: datetime.now(UTC)),
        on_busy,
        parse_state(None),
        Outcome(),
    )
    if dry_run:
        run.state = _load_state(root, warn)
        _convert_all(device, profile, home, run)
    else:
        with settings_state.lock(blocking=True, root=root):
            run.state = _load_state(root, warn)  # read inside the lock: another run may write
            _convert_all(device, profile, home, run)
    return run.outcome


def _convert_all(device: str, profile: Profile, home: Path, run: _Run) -> None:
    ran = False
    for rule in profile.save_convert.values():
        folder = Path(expand_home(profile.saves[rule.saves].path, home))
        if not folder.is_dir():
            run.say(f"saves {rule.key}: {folder} does not exist yet, nothing to convert")
            continue
        running = _running(rule.process, run.proc)
        if running is None:
            run.warn(
                f"saves {rule.key}: cannot tell whether {rule.process[0]} is running (no /proc), "
                f"so nothing was converted: run `warpzone saves convert {device}` on a system "
                "with /proc"
            )
            run.outcome.busy = True
            continue
        if running:
            run.outcome.busy = True
            if run.on_busy is not None:
                run.on_busy()
            run.say(f"saves {rule.key}: {rule.process[0]} is running, so nothing was converted")
            continue
        ran = True
        _convert_rule(rule, folder, Path(expand_home(rule.path, home)), run)
    if ran and not run.dry_run:
        run.state["last"] = {
            "when": run.clock().isoformat(timespec="seconds"),
            "written": run.outcome.written,
            "skipped": [list(item) for item in run.outcome.skipped],
            "errors": run.outcome.errors,
            "conflict_files": run.outcome.conflict_files[:CONFLICTS_KEPT],
        }
        run.state["conflicts"] = run.state["conflicts"][-CONFLICTS_KEPT:]
        try:
            _save_state(run.root, run.state)
        except OSError as exc:
            run.warn(f"cannot write the converter's state in {run.root} ({exc})")


def _convert_rule(rule: SaveConvert, raw_dir: Path, emu_dir: Path, run: _Run) -> None:
    raws = games(raw_dir, saveconvert.RAW_SUFFIX)
    emus = games(emu_dir, saveconvert.EMU_SUFFIXES[rule.format])
    for folder in (raw_dir, emu_dir):
        found = conflict_copies(folder)
        run.outcome.conflict_files += [str(folder / name) for name in found]
        if found:
            run.warn(
                f"saves {rule.key}: Syncthing left {len(found)} conflict file(s) in {folder} "
                f"({', '.join(found[:3])}), which were not touched: keep the one you want "
                "and delete the others"
            )
    for stem in sorted(set(raws) | set(emus)):
        if run.outcome.busy:
            return  # the emulator started: nothing more is written
        try:
            _convert_game(rule, stem, raws.get(stem), emus.get(stem), raw_dir, emu_dir, run)
        except OSError as exc:
            run.outcome.errors.append(f"{stem}: {exc}")
            run.warn(f"saves {rule.key}: {stem}: {exc}, so it was left as it is")


def _sides(
    raw_read: tuple[bytes, safefile.FileStamp] | None,
    emu_read: tuple[bytes, safefile.FileStamp] | None,
) -> tuple[Side | None, Side | None, saveconvert.Dsv | None]:
    raw_side: Side | None = None
    emu_side: Side | None = None
    dsv: saveconvert.Dsv | None = None
    if raw_read is not None:
        data, stamp = raw_read
        try:
            saveconvert.check_raw(data)
            raw_side = Side(
                saveconvert.digest(data), stamp.mtime_ns, len(data), saveconvert.blank(data)
            )
        except SaveFormatError as exc:
            raw_side = Side(problem=str(exc))
    if emu_read is not None:
        data, stamp = emu_read
        try:
            dsv = saveconvert.split_dsv(data)
            emu_side = Side(
                saveconvert.digest(dsv.raw),
                stamp.mtime_ns,
                len(dsv.raw),
                saveconvert.blank(dsv.raw),
            )
        except SaveFormatError as exc:
            emu_side = Side(problem=str(exc))
    return raw_side, emu_side, dsv


def _convert_game(
    rule: SaveConvert,
    stem: str,
    raw_path: Path | None,
    emu_path: Path | None,
    raw_dir: Path,
    emu_dir: Path,
    run: _Run,
) -> None:
    bases = run.state["bases"].setdefault(rule.key, {})
    if raw_path is None and (raw_dir / f".syncthing.{stem}{saveconvert.RAW_SUFFIX}.tmp").exists():
        # Syncthing is receiving this save: writing one now would make a conflict.
        run.say(f"saves {rule.key}: {stem}: Syncthing is still receiving it, left for later")
        return
    raw_read, emu_read = _read(raw_path), _read(emu_path)
    raw_side, emu_side, dsv = _sides(raw_read, emu_read)
    decision = saveconvert.decide(raw_side, emu_side, bases.get(stem))
    label = f"saves {rule.key}: {stem}"

    if decision.action is Action.SKIP:
        run.outcome.skipped.append((stem, decision.reason))
        run.warn(f"{label}: {decision.reason}, so it was not converted")
        return
    if decision.action is Action.NOTHING:
        run.outcome.current += 1
        if decision.base and bases.get(stem) != decision.base and not run.dry_run:
            bases[stem] = decision.base
        return

    if decision.action is Action.TO_EMU:
        assert raw_read is not None
        suffix = saveconvert.EMU_SUFFIXES[rule.format]
        target = emu_dir / f"{stem}{suffix}"
        payload = saveconvert.build_dsv(raw_read[0], dsv.footer if dsv else None)
        expect = emu_read[1] if emu_read else None
        loser, winner = "emulator", "hub"
        mtime_ns = raw_side.mtime_ns if raw_side else None
    else:
        assert emu_read is not None and dsv is not None
        target = raw_dir / f"{stem}{saveconvert.RAW_SUFFIX}"
        payload = dsv.raw
        expect = raw_read[1] if raw_read else None
        loser, winner = "hub", "emulator"
        mtime_ns = emu_side.mtime_ns if emu_side else None
    note = f" ({decision.reason})"
    if run.dry_run:
        run.say(f"{label}: dry-run: would write {target}{note}")
        if decision.conflict:
            run.say(f"{label}: both sides changed; the {loser} file would be backed up first")
        return

    def busy() -> bool:
        running = _running(rule.process, run.proc)
        if running is not False:  # it started, or cannot be told
            run.outcome.busy = True
        return running is not False

    kept = rule.key + (CONFLICT_SUFFIX if decision.conflict else "")

    def backup() -> object:
        return settings_state.backup(
            target,
            kept,
            stem,
            keep=CONFLICT_BACKUPS_KEPT if decision.conflict else BACKUPS_KEPT,
            root=run.root,
            now=run.clock(),
        )

    if expect is None:
        target.parent.mkdir(parents=True, exist_ok=True)
    result = safefile.guarded_edit(
        target,
        payload,
        expect=expect,
        busy=busy,
        backup=None if expect is None else backup,
        mtime_ns=mtime_ns,  # not now: Syncthing settles a conflict by the newest date
    )
    if result is safefile.Edit.BUSY:
        run.say(f"{label}: {rule.process[0]} started, so nothing more was converted")
        return
    if result is safefile.Edit.CHANGED:
        run.outcome.errors.append(f"{stem}: changed while it was being written")
        run.warn(f"{label}: {target} changed while it was being written, so it was left alone")
        return
    assert decision.base is not None
    bases[stem] = decision.base
    run.outcome.written += 1
    run.say(f"{label}: wrote {target}{note}")
    if decision.conflict:
        run.outcome.conflicts.append((stem, winner))
        run.state["conflicts"].append(
            {
                "when": run.clock().isoformat(timespec="seconds"),
                "rule": rule.key,
                "game": stem,
                "winner": winner,
            }
        )
        run.warn(
            f"{label}: both sides changed since the last time; the {winner} file was newer and "
            f"won, and the {loser} file is backed up in "
            f"{run.root / 'backups' / kept / stem}"
        )
