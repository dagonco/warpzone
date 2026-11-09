"""Which emulator saves on the hub belong to which game (see "Saves per game" in
development/design/library-ui.md).

The matching is pure: `build_index` takes the listing of the hub's `saves/` folder (paths below
it, modification times) and `match_game` takes a game's system and primary path, and neither
touches the disk. `scan` lists the folders and `SavesCache` keeps the index for a short time;
those are the only parts that read the disk. Nothing here writes below `saves/`, and a symlink
is never followed.

Only exact matches count: a ROM stem for the emulators that name their files after it, a
title ID for the ones that keep one folder per title. Shared memory cards and anything named
by an ID the catalog does not record are left out.
"""

from __future__ import annotations

import os
import posixpath
import re
import threading
import time
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from warpzone.library_api import Library

SAVES_DIR = "saves"
TTL_SECONDS = 60.0  # the index is listed again after this long
MAX_FILES_PER_TITLE = 2000  # files looked at below one title folder, for its newest mtime

# RetroArch's own save files next to a ROM stem: `<stem>.<ext>` in `saves/retroarch/saves`.
RETROARCH_DATA = frozenset(
    {"srm", "rtc", "sav", "mcr", "mcd", "eep", "fla", "sra", "mpk", "nv", "dsv"}
)
_RA_STATE = re.compile(r"^(?P<stem>.+)\.state(?:\d+|\.auto)?$")
_MELONDS_STATE = re.compile(r"^(?P<stem>.+)\.ml\d$")
_FLYCAST_VMU = re.compile(r"^(?P<stem>.+)_vmu_save_[A-D][1-4]\.bin$")
_FLYCAST_NVMEM = re.compile(r"^(?P<stem>.+)\.nvmem2?$")
_VITA_ID = re.compile(r"^[A-Z]{4}\d{5}$")
_PS4_ID = re.compile(r"^CUSA\d{5}$")
_PS4_FOLDER = re.compile(r"^(CUSA\d{5})(?:-UPDATE)?$")
_SWITCH_ID = re.compile(r"^[0-9A-F]{16}$")
_SWITCH_NAME = re.compile(r"\[([0-9A-Fa-f]{16})\]")

# Systems whose games a standalone emulator runs, never RetroArch: a file next to a ROM stem
# is not looked for under them.
NOT_RETROARCH = frozenset(
    {"switch", "ps4", "psvita", "ps3", "wiiu", "xbox", "xbox360", "model2", "n3ds", "3ds"}
)
MELONDS_SYSTEMS = frozenset({"nds"})
FLYCAST_SYSTEMS = frozenset({"dreamcast", "naomi", "naomi2", "atomiswave"})
# The standalone emulators that keep one folder per title, by the system whose games they run.
TITLE_EMULATORS = {"psvita": "vita3k", "ps4": "shadps4", "switch": "eden"}


@dataclass(frozen=True)
class Layout:
    """One hub folder `saves/<emulator>/<kind>` that is listed.

    `depth`: for files, how many levels of subfolders below the folder are looked into (a core
    or system folder); for title folders, how many levels sit above the title folder."""

    emulator: str
    kind: str
    depth: int
    titles: bool = False


LAYOUTS = (
    Layout("retroarch", "saves", 2),
    Layout("retroarch", "states", 2),
    Layout("melonds", "saves", 0),
    Layout("melonds", "states", 0),
    Layout("flycast", "saves", 0),
    Layout("vita3k", "saves", 0, titles=True),  # savedata/<TITLEID>
    Layout("shadps4", "saves", 1, titles=True),  # savedata/<user>/<CUSAnnnnn>
    Layout("eden", "saves", 2, titles=True),  # save/<account>/<profile>/<TitleID>
)


@dataclass(frozen=True)
class Entry:
    """One thing found below `saves/`: its path as parts, and its modification time in whole
    seconds (for a title folder, the newest file inside it)."""

    path: tuple[str, ...]
    mtime: int


@dataclass(frozen=True)
class Hit:
    emulator: str
    state: bool  # a save state, not save data
    mtime: int
    where: tuple[str, ...] = ()  # the folders between the hub folder and the file


@dataclass
class Index:
    by_stem: dict[str, list[Hit]] = field(default_factory=dict)
    by_title: dict[tuple[str, str], list[Hit]] = field(default_factory=dict)


@dataclass(frozen=True)
class GameSaves:
    data: bool = False
    states: int = 0
    newest: int | None = None
    emulators: tuple[str, ...] = ()


@dataclass(frozen=True)
class Context:
    """What the catalog says about names: the systems that have a game with each stem."""

    owners: Mapping[str, frozenset[str]]
    systems: frozenset[str]


# --------------------------------------------------------------------------- pure


def classify(entry: Entry) -> tuple[str, Hit] | None:
    """(key, hit) of an entry, or None when it is no save of an emulator we can attribute.
    A key is a ROM stem, or `emulator:TITLEID` for a title folder."""
    parts = entry.path
    if len(parts) < 3:
        return None
    emulator, kind, name = parts[0], parts[1], parts[-1]
    where = parts[2:-1]
    if (emulator, kind) == ("retroarch", "saves"):
        stem, dot, ext = name.rpartition(".")
        if dot and stem and ext.lower() in RETROARCH_DATA:
            return stem, Hit(emulator, False, entry.mtime, where)
    elif (emulator, kind) == ("retroarch", "states"):
        found = _RA_STATE.match(name)
        if found:
            return found["stem"], Hit(emulator, True, entry.mtime, where)
    elif (emulator, kind) == ("melonds", "saves") and not where:
        stem, dot, ext = name.rpartition(".")
        if dot and stem and ext == "sav":
            return stem, Hit(emulator, False, entry.mtime)
    elif (emulator, kind) == ("melonds", "states") and not where:
        found = _MELONDS_STATE.match(name)
        if found:
            return found["stem"], Hit(emulator, True, entry.mtime)
    elif (emulator, kind) == ("flycast", "saves") and not where:
        found = _FLYCAST_VMU.match(name) or _FLYCAST_NVMEM.match(name)
        if found:
            return found["stem"], Hit(emulator, False, entry.mtime)
    elif (emulator, kind) == ("vita3k", "saves") and len(parts) == 3:
        if _VITA_ID.match(name):
            return f"vita3k:{name}", Hit(emulator, False, entry.mtime)
    elif (emulator, kind) == ("shadps4", "saves") and len(parts) == 4:
        if _PS4_ID.match(name):
            return f"shadps4:{name}", Hit(emulator, False, entry.mtime)
    elif (
        (emulator, kind) == ("eden", "saves") and len(parts) == 5 and _SWITCH_ID.match(name.upper())
    ):
        return f"eden:{name.upper()}", Hit(emulator, False, entry.mtime)
    return None


def build_index(entries: Iterable[Entry]) -> Index:
    index = Index()
    for entry in entries:
        found = classify(entry)
        if found is None:
            continue
        key, hit = found
        if hit.emulator in TITLE_EMULATORS.values():
            emulator, _, title = key.partition(":")
            index.by_title.setdefault((emulator, title), []).append(hit)
        else:
            index.by_stem.setdefault(key, []).append(hit)
    return index


def eligible(emulator: str, system: str) -> bool:
    """Whether the emulator's files can belong to a game of the system."""
    if emulator == "retroarch":
        return system not in NOT_RETROARCH
    if emulator == "melonds":
        return system in MELONDS_SYSTEMS
    if emulator == "flycast":
        return system in FLYCAST_SYSTEMS
    return False


def stem_of(primary: str) -> str:
    """The name a ROM-named save carries: the file name of the primary part without its
    extension (a `.m3u` folder gives the playlist's name, as RetroArch loads it)."""
    return posixpath.splitext(posixpath.basename(primary))[0]


def title_id(system: str, primary: str) -> str | None:
    """The title ID the standalone emulators of the system name a save folder by, or None
    when the game's path does not carry one."""
    name = posixpath.basename(primary)
    if system == "psvita":
        return name if _VITA_ID.match(name) else None
    if system == "ps4":
        found = _PS4_FOLDER.match(name)
        return found[1] if found else None
    if system == "switch":
        found = _SWITCH_NAME.search(name)
        return found[1].upper() if found else None
    return None


def context_of(games: Iterable[tuple[str, str]]) -> Context:
    """The `Context` of (system, primary path) pairs."""
    owners: dict[str, set[str]] = {}
    for system, primary in games:
        owners.setdefault(stem_of(primary), set()).add(system)
    return Context(
        {stem: frozenset(found) for stem, found in owners.items()},
        frozenset(system for found in owners.values() for system in found),
    )


def _belongs(hit: Hit, system: str, stem: str, ctx: Context) -> bool:
    if system in hit.where:  # RetroArch sorted the file into a folder named after the system
        return True
    if any(folder in ctx.systems for folder in hit.where):  # ... or after another one
        return False
    # Flat, or in a core's folder: the stem must not belong to a game of another system that
    # the same emulator runs, or the file could be that game's.
    others = {s for s in ctx.owners.get(stem, ()) if eligible(hit.emulator, s)}
    return others <= {system}


def match_game(index: Index, ctx: Context, system: str, primary: str) -> GameSaves:
    """The saves of one game. Exact stem or title ID only; see the module docstring."""
    hits: list[Hit] = []
    stem = stem_of(primary)
    for hit in index.by_stem.get(stem, ()):
        if eligible(hit.emulator, system) and _belongs(hit, system, stem, ctx):
            hits.append(hit)
    emulator = TITLE_EMULATORS.get(system)
    ident = title_id(system, primary)
    if emulator is not None and ident is not None:
        hits.extend(index.by_title.get((emulator, ident), ()))
    if not hits:
        return GameSaves()
    return GameSaves(
        data=any(not h.state for h in hits),
        states=sum(1 for h in hits if h.state),
        newest=max(h.mtime for h in hits),
        emulators=tuple(sorted({h.emulator for h in hits})),
    )


def saves_response(found: GameSaves) -> dict[str, Any]:
    """The `saves` object of a game's answer."""
    newest = (
        None
        if found.newest is None
        else datetime.fromtimestamp(found.newest, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    )
    return {
        "data": found.data,
        "states": found.states,
        "newest": newest,
        "emulators": list(found.emulators),
    }


# --------------------------------------------------------------------------- the disk


def _children(path: Path) -> list[os.DirEntry[str]]:
    try:
        with os.scandir(path) as it:
            return sorted(it, key=lambda e: e.name)
    except OSError:
        return []


def _is(entry: os.DirEntry[str], directory: bool) -> bool:
    """A real file or directory, never a symlink to one."""
    try:
        if entry.is_symlink():
            return False
        return entry.is_dir(follow_symlinks=False) if directory else entry.is_file()
    except OSError:
        return False


def _files(base: Path, rel: tuple[str, ...], depth: int, out: list[Entry]) -> None:
    for child in _children(base):
        if child.name.startswith("."):
            continue
        if _is(child, False):
            try:
                out.append(
                    Entry((*rel, child.name), int(child.stat(follow_symlinks=False).st_mtime))
                )
            except OSError:
                continue
        elif depth > 0 and _is(child, True):
            _files(base / child.name, (*rel, child.name), depth - 1, out)


def _newest(folder: Path) -> int | None:
    """The newest mtime of the files below a title folder (None: it holds no file)."""
    newest: int | None = None
    seen = 0
    for here, dirs, files in os.walk(folder, followlinks=False):
        dirs[:] = [d for d in dirs if not d.startswith(".") and not os.path.islink(f"{here}/{d}")]
        for name in files:
            path = f"{here}/{name}"
            if name.startswith(".") or os.path.islink(path):
                continue
            try:
                mtime = int(os.lstat(path).st_mtime)
            except OSError:
                continue
            newest = mtime if newest is None else max(newest, mtime)
            seen += 1
            if seen >= MAX_FILES_PER_TITLE:
                return newest
    return newest


def _titles(base: Path, rel: tuple[str, ...], levels: int, out: list[Entry]) -> None:
    for child in _children(base):
        if child.name.startswith(".") or not _is(child, True):
            continue
        path = base / child.name
        if levels > 0:
            _titles(path, (*rel, child.name), levels - 1, out)
        else:
            newest = _newest(path)
            if newest is not None:
                out.append(Entry((*rel, child.name), newest))


def scan(saves: Path) -> list[Entry]:
    """List the folders of `LAYOUTS` below the hub's `saves/` (read-only)."""
    out: list[Entry] = []
    for layout in LAYOUTS:
        base = saves / layout.emulator / layout.kind
        rel = (layout.emulator, layout.kind)
        if layout.titles:
            _titles(base, rel, layout.depth, out)
        else:
            _files(base, rel, layout.depth, out)
    return out


class SavesCache:
    """The index of a hub's `saves/` folder, listed again after `ttl` seconds (not on every
    request), and the catalog's name context, rebuilt when the library object changes."""

    def __init__(
        self,
        saves: Path,
        *,
        ttl: float = TTL_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.saves = saves
        self.ttl = ttl
        self.clock = clock
        self._lock = threading.Lock()
        self._built = float("-inf")
        self._index = Index()
        self._ctx: tuple[object, Context] | None = None

    def index(self) -> Index:
        with self._lock:
            if self.clock() - self._built >= self.ttl:
                self._index = build_index(scan(self.saves))
                self._built = self.clock()
            return self._index

    def context(self, lib: Library) -> Context:
        with self._lock:
            if self._ctx is None or self._ctx[0] is not lib:
                pairs = ((s, g.primary) for s, games in lib.systems.items() for g in games.values())
                self._ctx = (lib, context_of(pairs))
            return self._ctx[1]

    def game(self, lib: Library, system: str, primary: str) -> dict[str, Any]:
        return saves_response(match_game(self.index(), self.context(lib), system, primary))
