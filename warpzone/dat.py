"""clrmamepro DATs (Redump, No-Intro): parsing, a cached index, matching and naming.

`warpzone inbox` identifies games by content: a file matches a DAT rom when size and
CRC32 are equal (and SHA1 too, when both sides have one), and a game matches when all
its DAT roms are present. Everything except `load_dat` and `DatLibrary` is pure.
See development/design/inbox.md.
"""

from __future__ import annotations

import hashlib
import os
import re
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from warpzone import safefile

# DAT name (the file name without ".dat") -> warpzone system folder (roms/<system>).
# Names are libretro-database's metadat/redump and metadat/no-intro. Keep this small:
# a system that is not listed here is not handled by `inbox`.
SYSTEMS: dict[str, str] = {
    "Sony - PlayStation": "psx",
    "Sony - PlayStation 2": "ps2",
    "Sony - PlayStation Portable": "psp",
    "Sega - Saturn": "saturn",
    "Sega - Dreamcast": "dreamcast",
    "Nintendo - GameCube": "gc",
    "Nintendo - Nintendo Entertainment System": "nes",
    "Nintendo - Super Nintendo Entertainment System": "snes",
    "Nintendo - Nintendo 64": "n64",
    "Nintendo - Game Boy": "gb",
    "Nintendo - Game Boy Color": "gbc",
    "Nintendo - Game Boy Advance": "gba",
    "Nintendo - Nintendo DS": "nds",
    "Sega - Mega Drive - Genesis": "genesis",
    "Sega - Master System - Mark III": "mastersystem",
    "Sega - Game Gear": "gamegear",
    "Atari - 2600": "atari2600",
    "Atari - 7800": "atari7800",
    "Atari - Lynx": "atarilynx",
    "Atari - Jaguar": "atarijaguar",
    "Bandai - WonderSwan": "wonderswan",
    "Bandai - WonderSwan Color": "wonderswancolor",
}
DISC_SYSTEMS = ("psx", "ps2", "saturn", "dreamcast")  # CHD, from cue/bin, gdi or iso
FILE_SYSTEMS = ("psp", "gc")  # one image file, kept as it is
# Systems whose discs of one game go in a `<game>.m3u/` folder with a playlist.
M3U_SYSTEMS = DISC_SYSTEMS
# Extensions that point at a system, so its DAT is tried first.
EXTENSION_HINTS: dict[str, str] = {
    ".nes": "nes",
    ".smc": "snes",
    ".sfc": "snes",
    ".n64": "n64",
    ".z64": "n64",
    ".v64": "n64",
    ".gb": "gb",
    ".gbc": "gbc",
    ".gba": "gba",
    ".nds": "nds",
    ".md": "genesis",
    ".gen": "genesis",
    ".smd": "genesis",
    ".sms": "mastersystem",
    ".gg": "gamegear",
    ".a26": "atari2600",
    ".a78": "atari7800",
    ".lnx": "atarilynx",
    ".j64": "atarijaguar",
    ".jag": "atarijaguar",
    ".ws": "wonderswan",
    ".wsc": "wonderswancolor",
}

# Systems whose dumps carry a dumper's header the DAT leaves out: the extension of the DAT's
# headerless entries and the one the headered file keeps in the library.
HEADERED_EXTENSIONS: dict[str, tuple[str, str]] = {
    "nes": (".unh", ".nes"),
    "atari7800": (".bin", ".a78"),
}

CACHE_VERSION = 2
_TOKEN = re.compile(r'"((?:[^"\\]|\\.)*)"|([()])|([^\s()"]+)')
_DATE_SUFFIX = re.compile(r" \(\d{8}-\d{6}\)$")
_DISC = re.compile(r" \(Disc ([0-9A-Za-z]+)\)")
_CRC = re.compile(r"^[0-9a-f]{8}$")


@dataclass(frozen=True)
class Rom:
    name: str
    size: int
    crc: str  # 8 lowercase hex digits
    sha1: str = ""  # 40 lowercase hex digits, or empty when the DAT has none


@dataclass(frozen=True)
class Game:
    name: str
    roms: tuple[Rom, ...]
    # The serials Redump gives the disc (a disc listed once per serial gathers them all), in
    # file order. Not part of the game's identity: two entries with the same name and roms
    # are one game.
    serials: tuple[str, ...] = field(default=(), compare=False)


@dataclass(frozen=True)
class FileInfo:
    path: str  # identifies the file within one item; "/" separated
    size: int
    crc: str
    sha1: str
    # (size, CRC32, SHA-1) of the data after the 16-byte iNES header, for a file that starts
    # with the iNES magic; None for any other file.
    without_header: tuple[int, str, str] | None = None


@dataclass(frozen=True)
class GameMatch:
    system: str
    game: Game
    files: tuple[FileInfo, ...]  # parallel to game.roms


@dataclass(frozen=True)
class Identified:
    matches: tuple[GameMatch, ...]
    ambiguous: tuple[tuple[str, ...], ...]  # sets of equally good names, nothing chosen


@dataclass
class Dat:
    system: str
    name: str
    games: list[Game]
    index: dict[tuple[int, str], list[int]] = field(default_factory=dict)
    discs: dict[str, set[str]] = field(default_factory=dict)  # prefix -> disc tags in the DAT

    def __post_init__(self) -> None:
        for i, game in enumerate(self.games):
            for rom in game.roms:
                bucket = self.index.setdefault((rom.size, rom.crc), [])
                if not bucket or bucket[-1] != i:
                    bucket.append(i)
            parts = split_disc(game.name)
            if parts:
                self.discs.setdefault(parts[0], set()).add(parts[1])


# ------------------------------------------------------------------ parsing


def _tokens(text: str) -> Iterator[str]:
    """Words and quoted strings as themselves, parentheses as "(" and ")"."""
    for m in _TOKEN.finditer(text):
        if m.group(1) is not None:
            yield "s" + m.group(1).replace('\\"', '"')
        elif m.group(2):
            yield m.group(2)
        else:
            yield "w" + m.group(3)


def _block(tokens: Iterator[str]) -> list[tuple[str, Any]]:
    """Parse the body of a `( ... )` block into (key, value) pairs; a value is a string
    or a nested block. The opening parenthesis is already consumed."""
    pairs: list[tuple[str, Any]] = []
    key: str | None = None
    for tok in tokens:
        if tok == ")":
            return pairs
        if tok == "(":
            if key is None:
                raise ValueError("block without a key")
            pairs.append((key, _block(tokens)))
            key = None
        elif key is None:
            key = tok[1:]
        else:
            pairs.append((key, tok[1:]))
            key = None
    raise ValueError("unterminated block")


def _rom(fields: list[tuple[str, Any]]) -> Rom | None:
    values = {k: v for k, v in fields if isinstance(v, str)}
    crc = values.get("crc", "").lower().zfill(8)
    try:
        size = int(values["size"])
    except (KeyError, ValueError):
        return None
    if "name" not in values or not _CRC.match(crc):
        return None
    sha1 = values.get("sha1", "").lower()
    return Rom(values["name"], size, crc, sha1 if re.fullmatch(r"[0-9a-f]{40}", sha1) else "")


def _serials(body: list[tuple[str, Any]]) -> tuple[str, ...]:
    """The serials of a game block: its own `serial` lines, else those of its roms. A value
    may hold several, separated by commas."""
    own = [v for k, v in body if k == "serial" and isinstance(v, str)]
    if not own:
        own = [
            v
            for k, rom in body
            if k == "rom" and isinstance(rom, list)
            for key, v in rom
            if key == "serial" and isinstance(v, str)
        ]
    found = (part.strip() for value in own for part in value.split(","))
    return tuple(dict.fromkeys(part for part in found if part))


def parse_dat(text: str) -> list[Game]:
    """Games of a clrmamepro DAT. Entries without roms are dropped, and so are repeats
    of the same name and roms (Redump lists a disc once per serial); the repeat's serials
    are added to the game kept."""
    tokens = _tokens(text)
    games: list[Game] = []
    seen: dict[Game, int] = {}
    key: str | None = None
    for tok in tokens:
        if tok == "(":
            if key is None:
                raise ValueError("block without a key")
            body = _block(tokens)
            if key in ("game", "machine"):
                name = next((v for k, v in body if k == "name" and isinstance(v, str)), "")
                roms = tuple(r for k, v in body if k == "rom" and (r := _rom(v)) is not None)
                game = Game(name, roms, _serials(body))
                if name and roms:
                    at = seen.get(game)
                    if at is None:
                        seen[game] = len(games)
                        games.append(game)
                    elif game.serials:
                        known = games[at]
                        merged = tuple(dict.fromkeys((*known.serials, *game.serials)))
                        games[at] = replace(known, serials=merged)
            key = None
        elif tok == ")":
            raise ValueError("unbalanced parenthesis")
        else:
            key = tok[1:]
    return games


def dat_system(file_name: str) -> str | None:
    """The warpzone system for a DAT file name; None when `inbox` does not handle it.
    A trailing No-Intro date stamp, "(20240101-123456)", is ignored."""
    stem = file_name[:-4] if file_name.lower().endswith(".dat") else file_name
    return SYSTEMS.get(_DATE_SUFFIX.sub("", stem))


# ------------------------------------------------------------------ cache and loading


def _cache_file(cache_dir: Path, dat_path: Path) -> Path:
    digest = hashlib.sha1(str(dat_path.resolve()).encode()).hexdigest()[:12]
    return cache_dir / f"{digest}-{dat_path.stem}.json"


def _read_cache(cache: Path, stat: os.stat_result) -> list[Game] | None:
    data = safefile.read_json_cache(cache)
    if data is None:
        return None
    try:
        if (
            data["version"] != CACHE_VERSION
            or data["size"] != stat.st_size
            or data["mtime_ns"] != stat.st_mtime_ns
        ):
            return None
        return [
            Game(n, tuple(Rom(*r) for r in roms), tuple(serials))
            for n, roms, serials in data["games"]
        ]
    except (ValueError, KeyError, TypeError):
        return None


def _write_cache(cache: Path, stat: os.stat_result, games: Sequence[Game]) -> None:
    data = {
        "version": CACHE_VERSION,
        "size": stat.st_size,
        "mtime_ns": stat.st_mtime_ns,
        "games": [
            [g.name, [[r.name, r.size, r.crc, r.sha1] for r in g.roms], list(g.serials)]
            for g in games
        ],
    }
    safefile.write_json_cache(cache, data)


def load_dat(path: Path, system: str, cache_dir: Path | None) -> Dat:
    stat = path.stat()
    games: list[Game] | None = None
    cache = _cache_file(cache_dir, path) if cache_dir else None
    if cache:
        games = _read_cache(cache, stat)
    if games is None:
        try:
            games = parse_dat(path.read_text(encoding="utf-8", errors="replace"))
        except ValueError as exc:
            raise ValueError(f"{path.name}: not a valid clrmamepro DAT ({exc})") from exc
        if cache:
            _write_cache(cache, stat, games)
    return Dat(system, path.stem, games)


class DatLibrary:
    """The DAT files of a folder, parsed only when a system is first asked for."""

    def __init__(self, directory: Path, cache_dir: Path | None) -> None:
        self.directory = directory
        self.cache_dir = cache_dir
        self.paths: dict[str, list[Path]] = {}
        self._loaded: dict[str, list[Dat]] = {}
        for path in sorted([*directory.glob("*.dat"), *directory.glob("*/*.dat")]):
            system = dat_system(path.name)
            if system:
                self.paths.setdefault(system, []).append(path)

    def systems(self) -> list[str]:
        return sorted(self.paths)

    def load(self, system: str) -> list[Dat]:
        if system not in self._loaded:
            self._loaded[system] = [
                load_dat(p, system, self.cache_dir) for p in self.paths.get(system, [])
            ]
        return self._loaded[system]


# ------------------------------------------------------------------ matching


def _assign(
    game: Game, by_key: dict[tuple[int, str], list[FileInfo]], claimed: set[str]
) -> tuple[FileInfo, ...] | None:
    """One distinct file per rom of the game, not among `claimed`, or None when a rom is
    missing."""
    used: set[str] = set(claimed)
    chosen: list[FileInfo] = []
    for rom in game.roms:
        for f in by_key.get((rom.size, rom.crc), []):
            if f.path not in used and (not rom.sha1 or not f.sha1 or rom.sha1 == f.sha1):
                used.add(f.path)
                chosen.append(f)
                break
        else:
            return None
    return tuple(chosen)


def _resolve(dat: Dat, files: Sequence[FileInfo]) -> tuple[Identified, set[str]]:
    """`match_dat` on the files as given, and the paths left out as ambiguous."""
    by_key: dict[tuple[int, str], list[FileInfo]] = {}
    for f in files:
        by_key.setdefault((f.size, f.crc), []).append(f)
    candidates = sorted({i for key in by_key for i in dat.index.get(key, ())})
    complete: list[tuple[Game, tuple[FileInfo, ...]]] = []
    for i in candidates:
        game = dat.games[i]
        claimed: set[str] = set()
        while found := _assign(game, by_key, claimed):  # a second copy is a second match
            complete.append((game, found))
            claimed |= {f.path for f in found}
    complete.sort(key=lambda c: (-len(c[0].roms), c[0].name))

    chosen: list[GameMatch] = []
    ambiguous: list[tuple[str, ...]] = []
    taken: set[str] = set()
    blocked: set[str] = set()
    for game, found in complete:
        paths = {f.path for f in found}
        if paths & (taken | blocked):
            continue
        rivals = sorted(
            {g.name for g, other in complete if {f.path for f in other} == paths and g != game}
            - {game.name}
        )
        if rivals:
            ambiguous.append(tuple(sorted({game.name, *rivals})))
            blocked |= paths
            continue
        taken |= paths
        chosen.append(GameMatch(dat.system, game, found))
    chosen.sort(key=lambda m: m.files[0].path)
    return Identified(tuple(chosen), tuple(ambiguous)), blocked


def match_dat(dat: Dat, files: Sequence[FileInfo]) -> Identified:
    """The games of `dat` whose roms are all among `files`.

    Where several complete games compete for the same files, the one with more roms
    wins; two different names with exactly the same files cannot be told apart, so
    they are returned as `ambiguous` and nothing is chosen for those files.

    In a NES or Atari 7800 DAT, a file with a dumper's header (iNES, A78) that nothing
    matched whole is tried again without it (see `_match_headerless`)."""
    found, blocked = _resolve(dat, files)
    if dat.system not in HEADERED_EXTENSIONS:
        return found
    used = {f.path for m in found.matches for f in m.files} | blocked
    left = [f for f in files if f.without_header is not None and f.path not in used]
    if not left:
        return found
    extra = _match_headerless(dat, left)
    return Identified((*found.matches, *extra.matches), (*found.ambiguous, *extra.ambiguous))


def _match_headerless(dat: Dat, files: Sequence[FileInfo]) -> Identified:
    """Match headered files by the data after the header against the DAT's headerless
    entries (No-Intro lists NES ones as `.unh`, 16 bytes smaller than the iNES dump, and
    Atari 7800 ones as `.bin`, 128 bytes smaller than the A78 dump). The headers of
    the same verified dump differ between dumpers (iNES against NES 2.0 fields, junk in the
    reserved bytes), the data does not. Only a SHA-1 equal to the DAT's counts, so a CRC32
    collision cannot place a wrong game.

    The match describes the file as it is: the DAT roms are replaced by roms with the size,
    hashes and `.nes` or `.a78` name of the file, so the library keeps the file with its own
    header and everything downstream checks the bytes that are placed."""
    real = {f.path: f for f in files}
    views = [FileInfo(f.path, *f.without_header) for f in files if f.without_header is not None]
    found, _ = _resolve(dat, views)
    chosen: list[GameMatch] = []
    for m in found.matches:
        if not all(r.sha1 and r.sha1 == v.sha1 for r, v in zip(m.game.roms, m.files, strict=True)):
            continue
        whole = tuple(real[v.path] for v in m.files)
        roms = tuple(
            Rom(_headered_name(r.name, dat.system), f.size, f.crc, f.sha1)
            for r, f in zip(m.game.roms, whole, strict=True)
        )
        chosen.append(GameMatch(m.system, replace(m.game, roms=roms), whole))
    return Identified(tuple(chosen), found.ambiguous)


def _headered_name(name: str, system: str) -> str:
    """The name of a headerless rom with the extension of a headered one."""
    stem, ext = os.path.splitext(name)
    headerless, headered = HEADERED_EXTENSIONS[system]
    return stem + headered if ext.lower() == headerless else name


# ------------------------------------------------------------------ naming and discs


def split_disc(name: str) -> tuple[str, str, str] | None:
    """(text before, disc tag, text after) for the first "(Disc N)" of a game name."""
    m = _DISC.search(name)
    return (name[: m.start()], m.group(1), name[m.end() :]) if m else None


def disc_sort_key(tag: str) -> tuple[int, int | str]:
    return (0, int(tag)) if tag.isdigit() else (1, tag)


@dataclass(frozen=True)
class DiscGroup:
    """Matches that belong to one game: a lone game, or the discs of a multi-disc one."""

    system: str
    name: str  # the library name: the game without its disc tag
    matches: tuple[GameMatch, ...]  # in disc order
    multi: bool
    missing: tuple[str, ...] = ()  # disc tags the DAT knows and the item lacks
    clash: tuple[str, ...] = ()  # disc tags that more than one match claims


def group_discs(matches: Sequence[GameMatch], dats: Mapping[str, Sequence[Dat]]) -> list[DiscGroup]:
    """Group the matches of an item into library entries.

    A game whose name has "(Disc N)" joins the other discs with the same text before
    the tag. The group is complete when every disc tag the DAT lists for that text is
    present; a game that is the only disc its DAT knows is a plain game. The library
    name is the text before the tag plus the text after it when all discs agree on it."""
    groups: list[DiscGroup] = []
    pending: dict[tuple[str, str], list[tuple[GameMatch, str, str]]] = {}
    for m in matches:
        parts = split_disc(m.game.name)
        if parts and (m.system in M3U_SYSTEMS or m.system in FILE_SYSTEMS):
            pending.setdefault((m.system, parts[0]), []).append((m, parts[1], parts[2]))
            continue
        groups.append(DiscGroup(m.system, m.game.name, (m,), False))
    for (system, prefix), found in pending.items():
        known: set[str] = set()
        for dat in dats.get(system, ()):
            known |= dat.discs.get(prefix, set())
        tags = [tag for _, tag, _ in found]
        found.sort(key=lambda t: disc_sort_key(t[1]))
        have = set(tags)
        if len(known) <= 1 and len(found) == 1:
            m = found[0][0]
            groups.append(DiscGroup(system, m.game.name, (m,), False))
            continue
        suffixes = {s for _, _, s in found}
        name = prefix + (next(iter(suffixes)) if len(suffixes) == 1 else "")
        groups.append(
            DiscGroup(
                system,
                name,
                tuple(m for m, _, _ in found),
                True,
                missing=tuple(sorted(known - have, key=disc_sort_key)),
                clash=tuple(sorted({t for t in tags if tags.count(t) > 1}, key=disc_sort_key)),
            )
        )
    groups.sort(key=lambda g: (g.system, g.name))
    return groups
