"""Title links: a symlink per library game inside an emulator's own game folder.

Vita3K reads games from `ux0/app/<TITLEID>`, while the library delivers each title to
`<library>/roms/psvita/app/<TITLEID>`. A profile's `[links.<id>]` table names both folders;
`provision device`, `warpzone links sync` and ES-DE's startup script keep one symlink per
title in the second, so a game that was just whitelisted needs no manual step.

The rules (`plan`) are pure. The links are the only thing ever changed: a real folder or file
in the way is left alone, so is a link that points elsewhere, and only a dangling link that
points into the library folder (one this module made for a game that is gone) is removed.
Nothing is changed while the emulator runs, and `dry_run` changes nothing. No network code
is imported, because ES-DE waits for the startup script. See development/design.md, "Title links".
"""

from __future__ import annotations

import os
import posixpath
import re
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path

from warpzone import settings_state
from warpzone.paths import expand_home
from warpzone.profiles import Profile, TitleLinks

Say = Callable[[str], None]


@dataclass(frozen=True)
class Entry:
    """What is in the target folder under one name: a "link" (where it points, and whether
    that still exists), a real "dir" or a "file"."""

    kind: str
    dest: str = ""
    alive: bool = True


@dataclass(frozen=True)
class Plan:
    create: tuple[str, ...] = ()
    remove: tuple[str, ...] = ()  # dangling links of ours
    ok: tuple[str, ...] = ()
    kept: tuple[str, ...] = ()  # something else is in the way: left alone

    @property
    def changes(self) -> int:
        return len(self.create) + len(self.remove)


def _norm(path: str) -> str:
    return posixpath.normpath(path)


def plan(source: str, ready: Iterable[str], current: Mapping[str, Entry]) -> Plan:
    """What to do so that every `ready` title has a link to `source/<title>`.

    `current` is the target folder as found. A link is "ours" when it points straight into
    `source`; only a dangling one of ours is removed."""
    base = _norm(source)
    titles = sorted(set(ready))
    create: list[str] = []
    remove: list[str] = []
    ok: list[str] = []
    kept: list[str] = []
    for title in titles:
        entry = current.get(title)
        if entry is None:
            create.append(title)
        elif entry.kind != "link":
            kept.append(title)
        elif _norm(entry.dest) == posixpath.join(base, title) and entry.alive:
            ok.append(title)
        elif not entry.alive and posixpath.dirname(_norm(entry.dest)) == base:
            remove.append(title)
            create.append(title)
        else:
            kept.append(title)
    for name in sorted(current):
        entry = current[name]
        if (
            name not in titles
            and entry.kind == "link"
            and not entry.alive
            and posixpath.dirname(_norm(entry.dest)) == base
        ):
            remove.append(name)
    return Plan(tuple(create), tuple(sorted(remove)), tuple(ok), tuple(kept))


# ------------------------------------------------------------------ the folders


def _listdir(path: Path) -> list[str]:
    try:
        return sorted(os.listdir(path))
    except OSError:
        return []


def _readlink(path: Path) -> str | None:
    try:
        return os.readlink(path)
    except OSError:
        return None


@dataclass(frozen=True)
class Fs:
    """The reads the rules need; `doctor` passes its own probes."""

    listdir: Callable[[Path], list[str]] = _listdir
    readlink: Callable[[Path], str | None] = _readlink
    isdir: Callable[[Path], bool] = os.path.isdir
    isfile: Callable[[Path], bool] = os.path.isfile
    exists: Callable[[Path], bool] = os.path.exists


REAL = Fs()


def ready_titles(source: Path, rule: TitleLinks, fs: Fs = REAL) -> list[str]:
    """The titles of `source`: real folders whose name fully matches the rule and that hold
    every file it requires (a game that is still syncing has no `eboot.bin` yet)."""
    pattern = re.compile(rule.match)
    found = []
    for name in fs.listdir(source):
        folder = source / name
        if (
            pattern.fullmatch(name)
            and fs.readlink(folder) is None
            and fs.isdir(folder)
            and all(fs.isfile(folder / rel) for rel in rule.require)
        ):
            found.append(name)
    return found


def snapshot(target: Path, fs: Fs = REAL) -> dict[str, Entry]:
    entries: dict[str, Entry] = {}
    for name in fs.listdir(target):
        path = target / name
        dest = fs.readlink(path)
        if dest is not None:
            entries[name] = Entry("link", dest, fs.exists(path))
        else:
            entries[name] = Entry("dir" if fs.isdir(path) else "file")
    return entries


@dataclass(frozen=True)
class Resolved:
    """One `[links.*]` rule with its folders on this machine."""

    key: str
    rule: TitleLinks
    source: Path
    target: Path

    @property
    def label(self) -> str:
        return self.rule.process[0]


def _expand(path: str, home: Path) -> Path:
    return Path(expand_home(path, home))


def resolve(profile: Profile, home: Path) -> list[Resolved]:
    library = str(_expand(profile.paths["library"], home)).rstrip("/") if profile.paths else ""
    out = []
    for key, rule in profile.links.items():
        out.append(
            Resolved(
                key,
                rule,
                _expand(rule.source_path(library), home),
                _expand(rule.target_path(library), home),
            )
        )
    return out


def creatable(item: Resolved, fs: Fs = REAL) -> bool:
    """Whether a missing `target` is made by the rule: it asks for that and the folder above
    it exists (the emulator has run)."""
    return item.rule.create_target and fs.isdir(item.target.parent)


def inspect(item: Resolved, fs: Fs = REAL) -> tuple[list[str], Plan]:
    """The ready titles of a rule and what is to be done about them."""
    ready = ready_titles(item.source, item.rule, fs)
    return ready, plan(str(item.source), ready, snapshot(item.target, fs))


# ------------------------------------------------------------------ doing it


def _make_target(item: Resolved, say: Say, warn: Say) -> bool:
    """Make the target folder (one level: its parent exists), when it is still missing."""
    try:
        item.target.mkdir(exist_ok=True)
    except OSError as exc:
        warn(f"links {item.key}: cannot create {item.target} ({exc.strerror or exc})")
        return False
    say(f"links {item.key}: created {item.target}")
    return True


def _apply(item: Resolved, todo: Plan, say: Say, warn: Say, dry_run: bool) -> int:
    """Remove the dangling links, then create the missing ones; returns how many changed."""
    changed = 0
    for name in todo.remove:
        path = item.target / name
        if dry_run:
            say(f"links {item.key}: would remove the dangling link {path}")
            changed += 1
            continue
        # Look again right before acting: only a link that is still dangling and still ours.
        dest = _readlink(path)
        if dest is None or os.path.exists(path):
            continue
        if posixpath.dirname(_norm(dest)) != _norm(str(item.source)):
            continue
        try:
            os.unlink(path)
        except OSError as exc:
            warn(f"links {item.key}: cannot remove {path} ({exc.strerror or exc})")
            continue
        say(f"links {item.key}: removed the dangling link {path}")
        changed += 1
    for name in todo.create:
        path, dest = item.target / name, item.source / name
        if dry_run:
            say(f"links {item.key}: would link {path} -> {dest}")
            changed += 1
            continue
        try:
            os.symlink(dest, path)
        except FileExistsError:
            continue  # something appeared meanwhile; the next run judges it
        except OSError as exc:
            warn(f"links {item.key}: cannot link {path} ({exc.strerror or exc})")
            continue
        say(f"links {item.key}: linked {path} -> {dest}")
        changed += 1
    return changed


def sync(
    device: str,
    profile: Profile,
    home: Path,
    *,
    say: Say,
    warn: Say,
    dry_run: bool = False,
    proc: Path = Path("/proc"),
    on_busy: Callable[[], None] | None = None,
) -> int:
    """Make the target folder of every `[links.*]` rule hold a link per ready title.

    Returns how many links it made or removed (or would, with `dry_run`). Nothing is
    changed while the emulator runs or when that cannot be told; `on_busy` is called when
    the emulator runs."""
    changed = 0
    again = f"run `warpzone links sync {device}`"
    for item in resolve(profile, home):
        if not REAL.isdir(item.source):
            say(f"links {item.key}: {item.source} does not exist yet, nothing to link")
            continue
        missing = not REAL.isdir(item.target)
        if missing and not creatable(item):
            warn(
                f"links {item.key}: {item.target} does not exist, so the {item.rule.noun}s were "
                f"not linked: start {item.label} once, then {again}"
            )
            continue
        _, todo = inspect(item)
        if missing and not todo.create:
            say(f"links {item.key}: no {item.rule.noun}s to link yet")
            continue
        for name in todo.kept:
            say(
                f"links {item.key}: {name} is a real folder or points elsewhere in "
                f"{item.target}, left alone"
            )
        if not todo.changes:
            say(f"links {item.key}: {len(todo.ok)} {item.rule.noun}(s) linked, nothing to do")
            continue
        try:
            busy = settings_state.running(item.rule.process, proc=proc)
        except settings_state.NoProc:
            warn(
                f"links {item.key}: cannot tell whether {item.label} is running (no /proc), so "
                f"the links were left alone: {again} on a system with /proc"
            )
            continue
        if busy:
            if on_busy is not None:
                on_busy()
            warn(
                f"links {item.key}: {item.label} is running, so the links were left alone: "
                f"quit {item.label} and {again}"
            )
            continue
        if missing:
            if dry_run:
                say(f"links {item.key}: would create {item.target}")
            elif not _make_target(item, say, warn):
                continue
        changed += _apply(item, todo, say, warn, dry_run)
    return changed
