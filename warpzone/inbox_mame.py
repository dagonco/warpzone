"""Arcade (MAME) sets for `warpzone inbox`.

An arcade zip is identified by the CRC32 and size of its members against up to three
set databases in XML (MAME's `-listxml`, the FinalBurn Neo arcade DAT and the MAME
2003-Plus DAT). A set "fits" an emulator when every required ROM of the set, of its
parent, of its BIOS and of its devices is present, in the dropped zip or in the BIOS and
device zips already in `library/roms/mame/`. The first emulator that fits wins: FinalBurn
Neo, then MAME (the ES-DE default, so no `<altemulator>`), then MAME 2003-Plus; the others
are recorded as a per-game `<altemulator>` in the hub's ES-DE gamelist. Sega Atomiswave
and Naomi games, which hang in MAME, go to Flycast instead. See docs/design.md.

The decisions (`evaluate`, `best_fit`, `decide`, `update_gamelist`, `parse_sets`,
`describe_miss`) are pure; `MameHandler` and the cache and gamelist file helpers touch
the disk. The module plugs into `inbox.py` through `inbox.kind_handlers()`.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import time
import weakref
import xml.etree.ElementTree as ET
import zipfile
from collections import Counter
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import IO
from xml.sax.saxutils import escape, unescape

from warpzone import inbox
from warpzone.inbox import Context, InboxError, ItemFailure, ItemResult, Options, UnitResult

SYSTEM = "mame"  # roms/<system>/ where arcade sets live
GAMELIST_REL = ("frontend", "es-de", "gamelists", "mame", "gamelist.xml")
CACHE_VERSION = 1
# Gamelist backups live outside every synced folder (wz-tools ignores /logs).
BACKUP_REL = ("tools", "logs", "gamelist-backups")
RECENT_SECONDS = 600  # a gamelist modified more recently than this triggers a warning
EMU_FBNEO = "FinalBurn Neo"  # ES-DE names, as written in <altemulator>
EMU_2003PLUS = "MAME 2003-Plus"
EMU_FLYCAST = "Flycast"  # the RetroArch core, for Atomiswave and Naomi
# Sega arcade boards MAME hangs on and Flycast runs: the BIOS set a game's `romof` chain
# ends in, and the board it stands for. Flycast reads the same zip names from bios/dc/.
FLYCAST_BIOS = {
    "awbios": "Atomiswave",
    "naomi": "Naomi",
    "naomi2": "Naomi 2",
    "naomigd": "Naomi GD-ROM",
}
BIOS_DIR_FLYCAST = ("bios", "dc")  # under library/
MAX_DEPTH = 8  # parent / BIOS chains are short; this only guards against a cycle
SHOWN_FILES = 3
# (config key, label or None to take the version from the DAT, <altemulator> value), in the
# order they are tried. FinalBurn Neo goes before MAME (the ES-DE default, no
# <altemulator>) because RetroAchievements: EmuDeck turns them off for the "MAME - Current"
# core (`cheevos_enable = "false"` in its mame.cfg) and the RA arcade sets target FinalBurn
# Neo, so a CPS1 game such as dino or ddsom only earns achievements there.
SOURCES: tuple[tuple[str, str | None, str | None], ...] = (
    ("fbneo_dat", EMU_FBNEO, EMU_FBNEO),
    ("mame_dat", None, None),
    ("mame2003plus_dat", EMU_2003PLUS, EMU_2003PLUS),
)
KIND_RANK = {"game": 0, "bios": 1, "device": 2}
Key = tuple[int, str]  # (size, crc32 as 8 lower-case hex digits)
Support = Callable[[str], "frozenset[Key] | None"]  # zip name -> its members, None if absent


class GamelistError(Exception):
    """The ES-DE gamelist cannot be updated; the message says why."""


# ------------------------------------------------------------------ the set databases


@dataclass(frozen=True)
class SetRom:
    name: str
    size: int
    crc: str
    merge: str = ""  # name of the same ROM in the parent or BIOS set
    bios: str = ""  # one of several alternative BIOS ROMs: any complete group will do

    @property
    def key(self) -> Key:
        return (self.size, self.crc)


@dataclass(frozen=True)
class Machine:
    """A set (game, BIOS or device). Only ROMs that can be checked are kept: the ones
    with a CRC32 that are not `nodump` and not `optional`."""

    name: str
    cloneof: str = ""
    romof: str = ""
    kind: str = "game"  # game | bios | device
    roms: tuple[SetRom, ...] = ()
    devices: tuple[str, ...] = ()  # every device with ROMs this one uses, transitively
    disks: tuple[str, ...] = ()  # CHD names (not checked: they are not in the zip)


@dataclass(frozen=True)
class Need:
    rom: SetRom
    owner: str  # the zip a merged set keeps this ROM in: the set that defines it
    scope: str = ""  # the set whose list names it: the game, or one of its devices


@dataclass
class SetDb:
    label: str  # for messages: "MAME 0.282"
    emulator: str | None  # the <altemulator> value; None is the ES-DE default (MAME)
    machines: dict[str, Machine]
    by_key: dict[Key, list[str]] = field(default_factory=dict)
    _needs: dict[str, tuple[tuple[Need, ...], tuple[tuple[tuple[Need, ...], ...], ...]]] = field(
        default_factory=dict
    )
    _by_name: dict[str, dict[str, SetRom]] = field(default_factory=dict)

    def __post_init__(self) -> None:
        for name, machine in self.machines.items():
            for key in dict.fromkeys(r.key for r in machine.roms):
                self.by_key.setdefault(key, []).append(name)


def _attr_int(value: str | None) -> int | None:
    try:
        return int(value) if value is not None else None
    except ValueError:
        return None


def parse_sets(stream: IO[bytes]) -> tuple[dict[str, Machine], str]:
    """Machines of a MAME `-listxml`, FinalBurn Neo or MAME 2003-Plus XML, streamed so a
    300 MB listing stays cheap. Returns the machines and the `build` text of the root.
    Machines with nothing to check are dropped, and each machine's device list is
    flattened to the devices that have ROMs."""
    raw: dict[str, Machine] = {}
    refs: dict[str, list[str]] = {}
    roms: list[SetRom] = []
    devices: list[str] = []
    disks: list[str] = []
    build = ""
    for _, el in ET.iterparse(stream, events=("end",)):
        tag = el.tag
        if tag == "rom":
            crc = (el.get("crc") or "").lower().zfill(8)
            size = _attr_int(el.get("size"))
            if (
                size is not None
                and re.fullmatch(r"[0-9a-f]{8}", crc)
                and el.get("status") != "nodump"
                and el.get("optional") != "yes"
            ):
                roms.append(
                    SetRom(el.get("name", ""), size, crc, el.get("merge", ""), el.get("bios", ""))
                )
        elif tag == "device_ref":
            devices.append(el.get("name", ""))
        elif tag == "disk":
            if el.get("status") != "nodump":
                disks.append(el.get("name", ""))
        elif tag in ("machine", "game"):
            name = el.get("name", "")
            if name:
                # 2003-Plus marks its BIOS sets only as not runnable
                kind = "bios" if el.get("isbios") == "yes" or el.get("runnable") == "no" else "game"
                if el.get("isdevice") == "yes":
                    kind = "device"
                raw[name] = Machine(
                    name,
                    el.get("cloneof", ""),
                    el.get("romof", ""),
                    kind,
                    tuple(roms),
                    (),
                    tuple(disks),
                )
                refs[name] = devices
            roms, devices, disks = [], [], []
        elif tag in ("mame", "datafile"):
            build = el.get("build", "")
        el.clear()
    has_roms = {n for n, m in raw.items() if m.roms}
    machines: dict[str, Machine] = {}
    for name, m in raw.items():
        found: list[str] = []
        seen = {name}
        todo = list(reversed(refs.get(name, ())))
        while todo:
            ref = todo.pop()
            if ref in seen:
                continue
            seen.add(ref)
            if ref in has_roms:
                found.append(ref)
            todo.extend(reversed(refs.get(ref, ())))
        if m.roms or m.disks or found:
            machines[name] = Machine(
                name, m.cloneof, m.romof, m.kind, m.roms, tuple(sorted(found)), m.disks
            )
    return machines, build


def open_dat(path: Path) -> IO[bytes]:
    """The XML of a DAT file, or of the first `.xml` / `.dat` member of a zip (MAME
    publishes its listxml zipped)."""
    if path.suffix.lower() != ".zip":
        return path.open("rb")
    archive = zipfile.ZipFile(path)
    for info in archive.infolist():
        if info.filename.lower().endswith((".xml", ".dat")) and not info.is_dir():
            return archive.open(info)
    archive.close()
    raise ValueError("the zip holds no .xml or .dat file")


def _cache_file(cache_dir: Path, dat_path: Path) -> Path:
    digest = hashlib.sha1(str(dat_path.resolve()).encode()).hexdigest()[:12]
    return cache_dir / f"sets-{digest}-{dat_path.stem}.json"


def _machine_json(m: Machine) -> list[object]:
    roms = [[r.name, r.size, r.crc, r.merge, r.bios] for r in m.roms]
    return [m.name, m.cloneof, m.romof, m.kind, roms, list(m.devices), list(m.disks)]


def _machine_from_json(row: list[object]) -> Machine:
    name, cloneof, romof, kind, roms, devices, disks = row
    return Machine(
        str(name),
        str(cloneof),
        str(romof),
        str(kind),
        tuple(SetRom(*r) for r in roms),  # type: ignore[attr-defined,arg-type]
        tuple(devices),  # type: ignore[arg-type]
        tuple(disks),  # type: ignore[arg-type]
    )


def load_sets(path: Path, cache_dir: Path | None) -> tuple[dict[str, Machine], str, bool]:
    """(machines, build text, read from the cache). The compact index is cached in
    `cache_dir`, keyed by the DAT's size and modification time."""
    stat = path.stat()
    cache = _cache_file(cache_dir, path) if cache_dir else None
    if cache:
        try:
            data = json.loads(cache.read_text(encoding="utf-8"))
            if (
                data["version"] == CACHE_VERSION
                and data["size"] == stat.st_size
                and data["mtime_ns"] == stat.st_mtime_ns
            ):
                machines = {m[0]: _machine_from_json(m) for m in data["machines"]}
                return machines, str(data["build"]), True
        except (OSError, ValueError, KeyError, TypeError, IndexError):
            pass
    try:
        with open_dat(path) as stream:
            machines, build = parse_sets(stream)
    except (ET.ParseError, ValueError, zipfile.BadZipFile) as exc:
        raise InboxError(f"{path}: not a MAME / FinalBurn Neo XML DAT ({exc})") from exc
    if not machines:
        raise InboxError(f"{path}: no sets with ROMs found; is it a listxml or an XML DAT?")
    if cache:
        data = {
            "version": CACHE_VERSION,
            "size": stat.st_size,
            "mtime_ns": stat.st_mtime_ns,
            "build": build,
            "machines": [_machine_json(m) for m in machines.values()],
        }
        tmp = cache.with_name(cache.name + ".tmp")
        try:
            cache.parent.mkdir(parents=True, exist_ok=True)
            tmp.write_text(json.dumps(data, separators=(",", ":")), encoding="utf-8")
            os.replace(tmp, cache)
        except OSError:
            pass  # a cache that cannot be written only costs time
    return machines, build, False


# ------------------------------------------------------------------ fitting (pure)


def _rom_index(db: SetDb, name: str) -> dict[str, SetRom]:
    index = db._by_name.get(name)
    if index is None:
        machine = db.machines.get(name)
        index = {r.name: r for r in machine.roms} if machine else {}
        db._by_name[name] = index
    return index


def owner_of(db: SetDb, name: str, rom: SetRom, depth: int = 0) -> str:
    """The set whose zip holds `rom` in a merged collection: follow `merge` up the
    parent and BIOS chain to the set that defines the ROM itself."""
    if not rom.merge or depth > MAX_DEPTH:
        return name
    machine = db.machines.get(name)
    if machine is None:
        return name
    for parent in dict.fromkeys((machine.cloneof, machine.romof)):
        found = _rom_index(db, parent).get(rom.merge) if parent else None
        if found is not None:
            return owner_of(db, parent, found, depth + 1)
    return name


def needs(
    db: SetDb, name: str
) -> tuple[tuple[Need, ...], tuple[tuple[tuple[Need, ...], ...], ...]]:
    """What a set requires: ROMs that must all be present, and for every set with
    alternative BIOS ROMs, the alternatives (one complete group is enough). The set's
    own ROMs, its devices' and, through `merge`, those of its parent and BIOS."""
    cached = db._needs.get(name)
    if cached is not None:
        return cached
    machine = db.machines[name]
    plain: dict[Key, Need] = {}
    choices: list[tuple[tuple[Need, ...], ...]] = []
    for scope in (name, *machine.devices):
        found = db.machines.get(scope)
        if found is None:
            continue
        groups: dict[str, list[Need]] = {}
        for rom in found.roms:
            need = Need(rom, scope, scope)
            if rom.bios:
                groups.setdefault(rom.bios, []).append(need)
            else:
                plain.setdefault(rom.key, need)
        if groups:
            choices.append(tuple(tuple(g) for g in groups.values()))
    result = (tuple(plain.values()), tuple(choices))
    db._needs[name] = result
    return result


def pool_names(db: SetDb, name: str) -> list[str]:
    """Names of the other zips that may hold ROMs the set needs: its parents, BIOS and
    devices (and theirs), never itself."""
    names: dict[str, None] = {}
    todo = [(name, 0), *((d, 0) for d in db.machines[name].devices)]
    while todo:
        current, depth = todo.pop()
        if current in names or depth > MAX_DEPTH:
            continue
        names[current] = None
        found = db.machines.get(current)
        if found is not None:
            todo.extend((p, depth + 1) for p in (found.cloneof, found.romof) if p)
    names.pop(name, None)
    return list(names)


@dataclass(frozen=True)
class Fit:
    label: str
    emulator: str | None
    machine: str
    kind: str
    ok: bool
    missing: tuple[Need, ...] = ()  # owner filled in
    present: frozenset[str] = frozenset()  # support zips found in the library
    used: int = 0  # members of the dropped zip that belong to this set
    disks: tuple[str, ...] = ()
    flycast_bios: str = ""  # Atomiswave / Naomi BIOS set the game derives from, if any


def flycast_bios(db: SetDb, name: str) -> str:
    """The Atomiswave or Naomi BIOS set (`awbios`, `naomi`, ...) at the end of the set's
    `romof` chain, "" for any other game. A clone's `romof` is its parent, whose own
    `romof` is the BIOS, so the chain is followed."""
    seen = {name}
    machine = db.machines.get(name)
    current = machine.romof if machine else ""
    while current and current not in seen and len(seen) <= MAX_DEPTH:
        if current in FLYCAST_BIOS:
            return current
        seen.add(current)
        found = db.machines.get(current)
        current = found.romof if found else ""
    return ""


def evaluate(db: SetDb, name: str, own: frozenset[Key], support: Support) -> Fit:
    """Does set `name` have every required ROM, counting the dropped zip (`own`) and the
    support zips already in the library?"""
    pool: set[Key] = set(own)
    present: set[str] = set()
    for other in pool_names(db, name):
        members = support(other)
        if members is not None:
            present.add(other)
            pool |= members
    plain, choices = needs(db, name)
    missing = [n for n in plain if n.rom.key not in pool]
    for alternatives in choices:
        lacking = [[n for n in group if n.rom.key not in pool] for group in alternatives]
        best = min(lacking, key=len)
        missing.extend(best)
    resolved = tuple(Need(n.rom, owner_of(db, n.scope, n.rom), n.scope) for n in missing)
    machine = db.machines[name]
    used = len(own & {r.key for r in machine.roms})
    return Fit(
        db.label,
        db.emulator,
        name,
        machine.kind,
        not resolved,
        resolved,
        frozenset(present),
        used,
        machine.disks,
        flycast_bios(db, name) if machine.kind == "game" else "",
    )


def _cloneof_chain(db: SetDb, name: str) -> Iterable[str]:
    seen = {name}
    current = db.machines[name].cloneof
    while current and current in db.machines and current not in seen:
        seen.add(current)
        yield current
        current = db.machines[current].cloneof


def best_fit(db: SetDb, own: frozenset[Key], stem: str, support: Support) -> Fit | None:
    """The set of `db` the dropped zip is. A file named like a set that shares members
    with it is that set, fit or not (a clone that fits does not make `galpanic.zip` a
    different game). Otherwise the sets that fit come first: the top parent of them (a
    merged zip fits its parent and every clone), the one that explains most members,
    games before BIOS and devices. When none fits, the one lacking the fewest files."""
    hits: Counter[str] = Counter()
    for key in own:
        hits.update(db.by_key.get(key, ()))
    if not hits:
        return None
    named = stem.lower()
    if named in hits:  # the file says which set it is: judge it as that, fit or not
        return evaluate(db, named, own, support)
    fits = {name: evaluate(db, name, own, support) for name in hits}
    ok = {n: f for n, f in fits.items() if f.ok}
    if ok:
        roots = [f for n, f in ok.items() if not any(a in ok for a in _cloneof_chain(db, n))]
        return min(roots, key=lambda f: (-f.used, KIND_RANK[f.kind], f.machine))
    return min(
        fits.values(),
        key=lambda f: (len(f.missing), -f.used, KIND_RANK[f.kind], f.machine),
    )


# ------------------------------------------------------------------ the decision (pure)


@dataclass(frozen=True)
class Outcome:
    """What one database says about the dropped zip. `fit` is None when no set of it
    shares a member with the zip."""

    label: str
    emulator: str | None
    fit: Fit | None


@dataclass(frozen=True)
class Decision:
    ok: bool
    set_name: str = ""
    emulator: str | None = None  # None: the ES-DE default
    label: str = ""
    kind: str = "game"
    reason: str = ""  # when not ok: why, with the missing files
    why: tuple[str, ...] = ()  # when ok: what earlier emulators lacked
    disks: tuple[str, ...] = ()
    flycast_bios: str = ""  # when Flycast was chosen: the BIOS set it needs in bios/dc/


def _files(needs_: Sequence[Need]) -> str:
    shown = ", ".join(f"{n.rom.name} {n.rom.crc}" for n in needs_[:SHOWN_FILES])
    more = f", ... ({len(needs_)} in all)" if len(needs_) > SHOWN_FILES else ""
    return f"{shown}{more}"


def describe_miss(outcome: Outcome) -> str:
    """One line on why an emulator does not fit: which files are missing and which zip
    they belong to ("needs neogeo.zip")."""
    fit = outcome.fit
    if fit is None:
        return "no set shares a file with this zip"
    owners: dict[tuple[str, str], list[Need]] = {}
    for need in fit.missing:
        owners.setdefault((need.owner, need.scope), []).append(need)
    parts = []
    for (owner, scope), found in owners.items():
        n = len(found)
        if owner == fit.machine:
            parts.append(f"{owner}.zip lacks {n} file(s): {_files(found)}")
        elif owner in fit.present:
            parts.append(f"{owner}.zip in the library lacks {n} file(s): {_files(found)}")
        else:
            zips = owner if scope in (owner, fit.machine) else f"{scope}.zip or {owner}"
            parts.append(f"needs {zips}.zip ({n} file(s): {_files(found)})")
    return f"{fit.machine}: " + "; ".join(parts)


def decide(outcomes: Sequence[Outcome]) -> Decision:
    """The first emulator, in the order given (FinalBurn Neo, MAME, MAME 2003-Plus),
    whose set has everything it needs; otherwise a review with what each one lacks. An
    Atomiswave or Naomi game that is complete per MAME's database goes to Flycast whatever
    the order, since MAME hangs on those boards."""
    for outcome in outcomes:
        fit = outcome.fit
        if outcome.emulator is None and fit is not None and fit.ok and fit.flycast_bios:
            return Decision(
                True,
                fit.machine,
                EMU_FLYCAST,
                outcome.label,
                fit.kind,
                disks=fit.disks,
                flycast_bios=fit.flycast_bios,
            )
    skipped: list[str] = []
    for outcome in outcomes:
        fit = outcome.fit
        if fit is not None and fit.ok:
            return Decision(
                True,
                fit.machine,
                outcome.emulator,
                outcome.label,
                fit.kind,
                why=tuple(skipped),
                disks=fit.disks,
            )
        skipped.append(f"{outcome.label}: {describe_miss(outcome)}")
    lines = ["does not fit any configured emulator:", *(f"    {s}" for s in skipped)]
    return Decision(False, reason="\n".join(lines))


# ------------------------------------------------------------------ the gamelist (pure)

_COMMENT = re.compile(r"<!--.*?-->", re.DOTALL)
_GAME = re.compile(r"<game(?:\s[^>]*)?>.*?</game\s*>", re.DOTALL)
_PATH = re.compile(r"<path>(.*?)</path>", re.DOTALL)
_ALT = re.compile(r"<altemulator>(.*?)</altemulator>", re.DOTALL)
_ALT_LINE = re.compile(
    r"^[ \t]*<altemulator>.*?</altemulator>[ \t]*\r?\n?", re.DOTALL | re.MULTILINE
)
_LIST_CLOSE = re.compile(r"</gameList\s*>")
_LIST_EMPTY = re.compile(r"<gameList\s*/>")
EMPTY_GAMELIST = '<?xml version="1.0"?>\n<gameList>\n</gameList>\n'


def _outside_comments(text: str) -> list[tuple[int, int]]:
    """Spans of `text` that are not inside an XML comment."""
    spans, at = [], 0
    for c in _COMMENT.finditer(text):
        spans.append((at, c.start()))
        at = c.end()
    spans.append((at, len(text)))
    return spans


def _game_blocks(text: str) -> list[re.Match[str]]:
    blocks = []
    for start, end in _outside_comments(text):
        blocks.extend(_GAME.finditer(text, start, end))
    return blocks


def _entry_path(block: str) -> str:
    m = _PATH.search(block)
    if not m:
        return ""
    path = unescape(m.group(1).strip())
    return path[2:] if path.startswith("./") else path


def gamelist_emulators(text: str) -> dict[str, str | None]:
    """File name -> its <altemulator> (None when the entry has none), for the tests and
    the report."""
    result: dict[str, str | None] = {}
    for m in _game_blocks(text):
        alt = _ALT.search(m.group(0))
        result[_entry_path(m.group(0))] = unescape(alt.group(1).strip()) if alt else None
    return result


def _well_formed(text: str) -> bool:
    """ES-DE writes more than one top-level element; wrap before parsing."""
    body = re.sub(r"^\s*<\?xml[^>]*\?>", "", text)
    try:
        ET.fromstring(f"<w>{body}</w>")
    except ET.ParseError:
        return False
    return True


def update_gamelist(text: str, set_name: str, emulator: str | None) -> tuple[str, str]:
    """`text` with the per-game emulator of `<set_name>.zip` set to `emulator` (removed
    when None), and what was done: "unchanged", "added", "changed" or "removed".
    Everything else, comments and unknown elements included, stays as it is."""
    return update_gamelist_entry(text, f"{set_name}.zip", emulator)


def update_gamelist_entry(text: str, file_name: str, emulator: str | None) -> tuple[str, str]:
    """`update_gamelist` for any game file name (`inbox_console_iso` uses it for PS3)."""
    nl = "\r\n" if "\r\n" in text else "\n"
    target = next((m for m in _game_blocks(text) if _entry_path(m.group(0)) == file_name), None)
    if target is None:
        if emulator is None:
            return text, "unchanged"
        new = _insert_entry(text, file_name, emulator, nl)
        action = "added"
    else:
        block = target.group(0)
        alt = _ALT.search(block)
        if emulator is None:
            if alt is None:
                return text, "unchanged"
            stripped = _ALT_LINE.sub("", block, count=1)
            if stripped == block:  # on a line with other things: take only the element
                stripped = _ALT.sub("", block, count=1)
            inner = re.sub(r"^<game(?:\s[^>]*)?>|</game\s*>$", "", stripped)
            if re.fullmatch(r"\s*<path>[^<]*</path>\s*", inner):
                new = _drop_block(text, target)
            else:
                new = text[: target.start()] + stripped + text[target.end() :]
            action = "removed"
        else:
            wanted = escape(emulator)
            if alt is not None:
                if unescape(alt.group(1).strip()) == emulator:
                    return text, "unchanged"
                edited = block[: alt.start(1)] + wanted + block[alt.end(1) :]
                action = "changed"
            else:
                edited = _add_to_block(block, wanted, nl)
                action = "added"
            new = text[: target.start()] + edited + text[target.end() :]
    if _well_formed(text) and not _well_formed(new):
        raise GamelistError("the edited gamelist would not be well-formed XML; left untouched")
    return new, action


def _indent_before(text: str, pos: int) -> str | None:
    """The whitespace between the start of the line and `pos`, None if there is more."""
    start = text.rfind("\n", 0, pos) + 1
    before = text[start:pos]
    return before if not before.strip() else None


def _add_to_block(block: str, emulator: str, nl: str) -> str:
    close = block.rindex("</game")
    path = _PATH.search(block)
    child = (_indent_before(block, path.start()) if path else None) or "\t\t"
    head = block[:close].rstrip(" \t")
    if not head.endswith(("\n", "\r")):
        head += nl
    closing_indent = _indent_before(block, close)
    tail = block[close:]
    if closing_indent is None:  # "</game>" shares a line with other content
        return f"{block[:close]}<altemulator>{emulator}</altemulator>{tail}"
    return f"{head}{child}<altemulator>{emulator}</altemulator>{nl}{closing_indent}{tail}"


def _drop_block(text: str, match: re.Match[str]) -> str:
    start, end = match.start(), match.end()
    line_start = text.rfind("\n", 0, start) + 1
    if not text[line_start:start].strip():  # the entry owns its lines: take them whole
        start = line_start
        tail = re.match(r"[ \t]*\r?\n", text[end:])
        if tail:
            end += tail.end()
    return text[:start] + text[end:]


def _insert_entry(text: str, file_name: str, emulator: str, nl: str) -> str:
    if _LIST_EMPTY.search(text):
        text = _LIST_EMPTY.sub(f"<gameList>{nl}</gameList>", text, count=1)
    close = None
    for start, end in _outside_comments(text):
        for m in _LIST_CLOSE.finditer(text, start, end):
            close = m
    if close is None:
        raise GamelistError("no <gameList> element found")
    blocks = _game_blocks(text)
    entry, child = "\t", "\t\t"
    if blocks:
        first = blocks[0]
        entry = _indent_before(text, first.start()) or entry
        path = _PATH.search(first.group(0))
        if path:
            child = _indent_before(first.group(0), path.start()) or child
    else:
        wrapper = _indent_before(text, close.start())
        if wrapper:
            entry = wrapper + "\t"
            child = entry + "\t"
    lines = (
        f"{entry}<game>{nl}"
        f"{child}<path>./{escape(file_name)}</path>{nl}"
        f"{child}<altemulator>{escape(emulator)}</altemulator>{nl}"
        f"{entry}</game>{nl}"
    )
    at = close.start()
    indent = _indent_before(text, at)
    if indent is not None:
        return text[: at - len(indent)] + lines + text[at - len(indent) :]
    return text[:at] + nl + lines + text[at:]


# ------------------------------------------------------------------ disk


def read_zip(path: Path, verify: bool) -> tuple[list[str], frozenset[Key]]:
    """Member names and (size, crc32) of a zip, from its directory. With `verify` every
    member is also read back (a directory can lie)."""
    with zipfile.ZipFile(path) as z:
        if verify:
            bad = z.testzip()
            if bad:
                raise ItemFailure(f"{path.name}: damaged member {bad}")
        infos = [i for i in z.infolist() if not i.is_dir()]
    return [i.filename for i in infos], frozenset((i.file_size, f"{i.CRC:08x}") for i in infos)


def backup_path(root: Path, system: str, when: float) -> Path:
    """Where the gamelist of `system` is backed up before a run edits it: outside every
    synced folder (`tools/logs` is ignored by `wz-tools`), so it never reaches a console."""
    stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime(when))
    return root.joinpath(*BACKUP_REL, f"{system}-gamelist-{stamp}.xml")


def recently_written(mtime: float | None, now: float, window: float = RECENT_SECONDS) -> bool:
    """Was the gamelist modified within `window` seconds before `now`? A console that
    just played writes it back through Syncthing, so a recent change means ES-DE may
    still be open there with its own copy in memory."""
    return mtime is not None and now - mtime < window


def recent_warning(shown: str, mtime: float, now: float) -> str:
    minutes = max(0, int((now - mtime) // 60))
    return (
        f"gamelist {shown} was modified {minutes} min ago. ES-DE on a console keeps its "
        "gamelist in memory and rewrites it when a game is launched, which would overwrite "
        "this edit. If the change does not stick, quit ES-DE on every console and run "
        "inbox again"
    )


def write_gamelist(path: Path, text: str, backup: Path | None) -> None:
    """Replace the gamelist with `text`; with `backup`, first copy the current file there."""
    path.parent.mkdir(parents=True, exist_ok=True)
    part = path.with_name(path.name + ".part")
    part.unlink(missing_ok=True)
    try:
        part.write_bytes(text.encode("utf-8"))
        if path.exists():
            shutil.copymode(path, part)
            if backup is not None:
                backup.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(path, backup)
        os.replace(part, path)
    finally:
        part.unlink(missing_ok=True)


def save_gamelist(
    ctx: Context, path: Path, text: str, system: str, shown: str, seen: set[Path]
) -> list[str]:
    """Write an edited gamelist (only with --apply). The first write of a path in a run
    backs the file up under `tools/logs/gamelist-backups/` and returns the warning notes
    when the file was modified a few minutes ago. `seen` holds the paths done already."""
    notes: list[str] = []
    first = path not in seen
    if first:
        now = ctx.opts.now()
        try:
            mtime: float | None = path.stat().st_mtime
        except OSError:
            mtime = None
        if mtime is not None and recently_written(mtime, now):
            notes.append(recent_warning(shown, mtime, now))
    backup = backup_path(ctx.opts.root, system, ctx.opts.now()) if first else None
    try:
        write_gamelist(path, text, backup)
    except OSError as exc:
        raise GamelistError(f"cannot write {path}: {exc.strerror or exc}") from exc
    seen.add(path)
    return notes


def read_gamelist(path: Path) -> str | None:
    try:
        return path.read_bytes().decode("utf-8")
    except FileNotFoundError:
        return None
    except (OSError, UnicodeDecodeError) as exc:
        raise GamelistError(f"cannot read {path}: {getattr(exc, 'strerror', None) or exc}") from exc


@dataclass
class RunState:
    dbs: list[SetDb] | None = None
    support: dict[str, frozenset[Key] | None] = field(default_factory=dict)
    backed_up: set[Path] = field(default_factory=set)


_STATE: weakref.WeakKeyDictionary[Context, RunState] = weakref.WeakKeyDictionary()


def _state(ctx: Context) -> RunState:
    if ctx not in _STATE:
        _STATE[ctx] = RunState()
    return _STATE[ctx]


class MameHandler:
    """Arcade zips: a `.zip` file in the inbox, or a folder of nothing but `.zip` files,
    where at least one member matches a configured arcade DAT."""

    def validate(self, opts: Options) -> None:
        for key, _, _ in SOURCES:
            value = getattr(opts.settings, key)
            if value and not Path(value).is_file():
                raise InboxError(f"[inbox] {key}: {value} is not a file")

    def _dbs(self, ctx: Context) -> list[SetDb]:
        state = _state(ctx)
        if state.dbs is None:
            dbs = []
            for key, label, emulator in SOURCES:
                value = getattr(ctx.opts.settings, key)
                if not value:
                    continue
                path = Path(value)
                machines, build, cached = load_sets(path, ctx.opts.cache_dir)
                if label is None:
                    version = re.match(r"\d+(?:\.\d+)*", build)
                    label = f"MAME {version.group(0)}" if version else "MAME"
                how = "cached" if cached else "indexed now, cached for the next run"
                ctx.rep.out(f"arcade DAT: {label}, {len(machines)} sets ({how})")
                dbs.append(SetDb(label, emulator, machines))
            state.dbs = dbs
        return state.dbs

    def _zips(self, item: Path) -> list[Path] | None:
        if item.is_symlink():
            return None
        if item.is_file():
            return [item] if item.suffix.lower() == ".zip" else None
        if not item.is_dir():
            return None
        zips: list[Path] = []
        for base, dirs, names in os.walk(item):
            dirs[:] = [d for d in dirs if not inbox.skip_name(d)]
            for name in sorted(names):
                if inbox.skip_name(name):
                    continue
                path = Path(base) / name
                if path.is_symlink():
                    return None
                if path.suffix.lower() == ".zip":
                    zips.append(path)
                elif path.suffix.lower() not in inbox.SIDECAR_EXTENSIONS:
                    return None
        return sorted(zips) or None

    def claims(self, ctx: Context, item: Path) -> bool:
        s = ctx.opts.settings
        if not (s.mame_dat or s.fbneo_dat or s.mame2003plus_dat):
            return False
        zips = self._zips(item)
        if not zips:
            return False
        dbs = self._dbs(ctx)
        for path in zips:
            try:
                _, keys = read_zip(path, verify=False)
            except (zipfile.BadZipFile, OSError):
                continue
            if any(key in db.by_key for db in dbs for key in keys):
                return True
        return False

    def process(self, ctx: Context, item: Path, work: Path, result: ItemResult) -> None:
        zips = self._zips(item) or []
        dbs = self._dbs(ctx)
        folder = ctx.roms / SYSTEM
        support = self._support(ctx, folder)
        for path in zips:
            rel = path.relative_to(item.parent).as_posix()
            try:
                unit = self._one(ctx, dbs, path, rel, folder, support)
            except ItemFailure as exc:
                result.reasons.append(f"{rel}: {exc}")
                continue
            except (zipfile.BadZipFile, OSError) as exc:
                result.reasons.append(
                    f"{rel}: cannot read it as a zip ({getattr(exc, 'strerror', None) or exc})"
                )
                continue
            result.units.append(unit)
            if unit.reason:
                result.reasons.append(unit.reason)

    def _support(self, ctx: Context, folder: Path) -> Support:
        cache = _state(ctx).support

        def lookup(name: str) -> frozenset[Key] | None:
            if name not in cache:
                try:
                    cache[name] = read_zip(folder / f"{name}.zip", verify=False)[1]
                except (OSError, zipfile.BadZipFile):
                    cache[name] = None
            return cache[name]

        return lookup

    def _one(
        self,
        ctx: Context,
        dbs: Sequence[SetDb],
        path: Path,
        rel: str,
        folder: Path,
        support: Support,
    ) -> UnitResult:
        own = read_zip(path, verify=True)[1]
        outcomes = [
            Outcome(db.label, db.emulator, best_fit(db, own, path.stem, support)) for db in dbs
        ]
        decision = decide(outcomes)
        if not decision.ok:
            res = UnitResult(SYSTEM, path.name, "review", reason=f"{rel}: {decision.reason}")
            return res
        entry = f"{decision.set_name}.zip"
        res = UnitResult(SYSTEM, entry, "planned" if not ctx.opts.apply else "placed")
        if entry != path.name:
            res.notes.append(f"renamed from {path.name} (the set name in {decision.label})")
        res.notes.append(self._emulator_note(decision))
        if decision.flycast_bios:
            res.notes.extend(self._flycast_notes(ctx, decision.flycast_bios))
        for why in decision.why:
            res.notes.append(f"not {why}")
        if decision.disks:
            res.notes.append(
                f"also needs CHD disk(s) not checked here: {', '.join(decision.disks[:3])}"
            )
        dest = folder / entry
        if dest.exists():
            same = inbox.same_content("zip", dest, path, None)
            verdict = inbox.destination_verdict(True, same)
            if verdict == "conflict":
                res.outcome = "review"
                res.reason = f"{entry}: a different file with that name is already in the library"
                return res
            res.outcome = "already" if ctx.opts.apply else "exists"
            if not ctx.opts.apply:
                res.notes.append("already in the library (same content)")
        elif ctx.opts.apply:
            self._place(path, folder, dest, own)
        if decision.kind == "game":
            try:
                self._gamelist(ctx, decision, res)
            except GamelistError as exc:  # the zip is placed; a rerun retries the gamelist
                res.reason = f"{entry} is in the library but the gamelist was not updated: {exc}"
        return res

    @staticmethod
    def _flycast_notes(ctx: Context, bios: str) -> list[str]:
        """Flycast takes its BIOS from `library/bios/dc/`, not from `roms/mame/`."""
        notes = [f"{FLYCAST_BIOS[bios]} game: hangs in MAME, runs in Flycast"]
        if not ctx.opts.root.joinpath("library", *BIOS_DIR_FLYCAST, f"{bios}.zip").is_file():
            notes.append(
                f"Flycast needs {bios}.zip in bios/dc/: copy it from roms/mame/ "
                "(warpzone never copies BIOS files)"
            )
        return notes

    @staticmethod
    def _emulator_note(decision: Decision) -> str:
        if decision.kind != "game":
            return f"{decision.kind} set ({decision.label}); no emulator entry needed"
        if decision.emulator is None:
            return f"emulator: default ({decision.label})"
        return f"emulator: {decision.emulator}"

    def _place(self, src: Path, folder: Path, dest: Path, own: frozenset[Key]) -> None:
        folder.mkdir(parents=True, exist_ok=True)
        part = dest.with_name(dest.name + ".part")
        part.unlink(missing_ok=True)
        try:
            inbox.copy_verified(src, part)
            if read_zip(part, verify=True)[1] != own:
                raise ItemFailure(f"{dest.name}: the copy does not match the original")
            if dest.exists():
                raise ItemFailure(f"{dest.name}: appeared in the library while copying")
            os.rename(part, dest)
        finally:
            part.unlink(missing_ok=True)

    def _gamelist(self, ctx: Context, decision: Decision, res: UnitResult) -> None:
        settings = ctx.opts.settings
        path = (
            Path(settings.gamelist_mame)
            if settings.gamelist_mame
            else ctx.opts.root.joinpath(*GAMELIST_REL)
        )
        text = read_gamelist(path)
        if text is None:
            if decision.emulator is None:
                return
            text = EMPTY_GAMELIST
        new, action = update_gamelist(text, decision.set_name, decision.emulator)
        try:
            shown = path.relative_to(ctx.opts.root).as_posix()
        except ValueError:
            shown = str(path)
        if action == "unchanged":
            if decision.emulator is not None:
                res.notes.append(f"gamelist {shown}: already sets {decision.emulator}")
            return
        what = _gamelist_words(action, decision, ctx.opts.apply)
        if not ctx.opts.apply:
            res.notes.append(f"gamelist {shown}: would {what}")
            return
        res.notes.extend(
            save_gamelist(ctx, path, new, SYSTEM, shown, _state(ctx).backed_up),
        )
        res.notes.append(f"gamelist {shown}: {what}")


def _gamelist_words(action: str, decision: Decision, done: bool) -> str:
    """What the gamelist change is, as "add ..." before it happens and "added ..." after."""
    verbs = {"added": "add", "changed": "change", "removed": "remove"}
    verb = action if done else verbs[action]
    name = f"{decision.set_name}.zip"
    if action == "removed":
        return f"{verb} the emulator override of {name} (the default fits)"
    return f"{verb} the emulator of {name}: {decision.emulator}"


HANDLER = MameHandler()
