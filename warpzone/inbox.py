"""Process new games dropped in `inbox/`: identify, convert, file into the library.

Report only by default; `apply` converts, places and moves the originals. Each item
(a file or folder at the top of `inbox/`) is unpacked into a local work folder, every
file is identified by content against Redump / No-Intro DATs (`dat.py`), converted to
the library's format and copied into `library/roms/<system>/` without ever replacing
an existing file. Originals go to `inbox/_done/<date>/` (purged after 7 days), items
that could not be placed go to `inbox/_revisar/` with a `<item>.reason.txt`.

The decisions (`select_purge`, `plan_units`, `cue_refs`, `gdi_refs`, `resolve_refs`) are
pure; the rest touches the disk. What the kind handlers share (errors, options, results,
staging, `skip_name`, `whitelist_line`, ...) lives in `inbox_core.py`, which the handlers
import instead of this module. See development/design/inbox.md.
"""

from __future__ import annotations

import hashlib
import os
import posixpath
import re
import shutil
import tempfile
import zipfile
import zlib
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path

from warpzone import dat as datmod
from warpzone import (
    inbox_console_iso,
    inbox_dolphin,
    inbox_lock,
    inbox_mame,
    inbox_n3ds,
    inbox_switch,
    inbox_vita,
    ra,
)
from warpzone.dat import (
    DISC_SYSTEMS,
    EXTENSION_HINTS,
    M3U_SYSTEMS,
    DatLibrary,
    DiscGroup,
    FileInfo,
    GameMatch,
)
from warpzone.inbox_core import (
    ARCHIVE_EXTENSIONS,
    CHUNK,
    INES_MAGIC,
    SIDECAR_EXTENSIONS,
    Context,
    InboxError,
    ItemFailure,
    ItemResult,
    KindHandler,
    Options,
    Staged,
    Tools,
    ToolTimeout,
    UnitResult,
    UnmatchedHandler,
    copy_verified,
    crc_of,
    destination_verdict,
    free_space_check,
    next_free,
    printable,
    refused_name,
    run,
    run_process,
    same_content,
    skip_name,
    stage_item,
    tail,
    unsupported_reason,
    unverified_reason,
    zip_members,
)
from warpzone.report import Reporter

DONE_DIR = "_done"
REVIEW_DIR = "_revisar"
UNVERIFIED_DIR = "_unverified"  # holds <system>/ folders of roms no DAT knows
DONE_KEEP_DAYS = 7
SETTLE_SECONDS = 30  # an item changed more recently than this may still be copying
COMPRESSED_GC = (".gcz",)  # .rvz and .wia are read by `inbox_dolphin`
GCZ_MAGIC = 0xB10BC001
# Files that travel with a dump but are not part of any DAT; never identified.
INES_HEADER_BYTES = 16
A78_HEADER_BYTES = 128
A78_MAGIC = b"ATARI7800"  # at offset 1 of an A78 header
ZIP_DATE = (1996, 12, 24, 23, 32, 0)  # fixed, so the same game always zips to the same bytes
DOLPHIN_HINT = "dolphin-tool convert -f rvz -c zstd -l 5 -b 131072"
_DATE_DIR = re.compile(r"^\d{4}-\d\d-\d\d$")
_CUE_FILE = re.compile(r'^\s*FILE\s+(?:"(.+)"|(\S+))\s+\w+', re.IGNORECASE | re.MULTILINE)
_GDI_TRACK = re.compile(r'^\s*\d+\s+\d+\s+\d+\s+\d+\s+(?:"(.+)"|(\S+))\s+\d+\s*$', re.MULTILINE)


# ------------------------------------------------------------------ pure decisions


def select_purge(names: Sequence[str], today: date, days: int = DONE_KEEP_DAYS) -> list[str]:
    """The `_done` folders to delete: named like a date and older than `days` days.
    Anything else in there is never selected."""
    old: list[str] = []
    for name in names:
        if not _DATE_DIR.match(name):
            continue
        try:
            folder_date = date.fromisoformat(name)
        except ValueError:
            continue
        if (today - folder_date).days > days:
            old.append(name)
    return sorted(old)


def _first(m: re.Match[str]) -> str:
    return m.group(1) or m.group(2)


def cue_refs(text: str) -> list[str]:
    return [_first(m) for m in _CUE_FILE.finditer(text)]


def gdi_refs(text: str) -> list[str]:
    return [_first(m) for m in _GDI_TRACK.finditer(text)]


def resolve_refs(
    sidecar_rel: str, refs: Sequence[str], available: Mapping[str, str]
) -> tuple[str, ...] | None:
    """The item paths a cue or gdi names, or None if one is missing or points elsewhere.
    `available` maps lower-case item paths to item paths."""
    base = sidecar_rel.rsplit("/", 1)[0] + "/" if "/" in sidecar_rel else ""
    found: list[str] = []
    for ref in refs:
        parts = [p for p in ref.replace("\\", "/").split("/") if p not in ("", ".")]
        if not parts or ".." in parts:
            return None
        rel = available.get((base + "/".join(parts)).lower())
        if rel is None:
            return None
        found.append(rel)
    return tuple(found)


@dataclass(frozen=True)
class Unit:
    """One thing that lands in a system folder: a file, or a `<game>.m3u/` folder."""

    system: str
    entry: str  # name inside roms/<system>/
    matches: tuple[GameMatch, ...]  # one, or all the discs of an m3u folder
    unverified: bool = False  # filed from `_unverified/`: the "match" describes the file itself
    sbis: tuple[str | None, ...] = ()  # item path of each disc's LibCrypt .sbi, or None

    @property
    def is_m3u(self) -> bool:
        return len(self.matches) > 1 or self.entry.endswith(".m3u")


def output_extension(system: str, from_compressed: bool = False) -> str:
    if system in DISC_SYSTEMS:
        return ".chd"
    if system == "psp":
        return ".iso"
    if system == "gc":
        return ".gcz" if from_compressed else ".iso"
    return ".zip"


def plan_units(
    groups: Sequence[DiscGroup], compressed: Callable[[GameMatch], bool] = lambda m: False
) -> tuple[list[Unit], list[str]]:
    """Library entries for the groups of an item, and why some cannot be placed.

    A multi-disc game of a CHD system becomes one `<game>.m3u/` folder; one of any
    other system (the library keeps GameCube discs loose) becomes one file per disc.
    Either way all discs must be present, and none claimed twice."""
    units: list[Unit] = []
    problems: list[str] = []
    for g in groups:
        if g.clash:
            problems.append(
                f"{g.system}: {g.name}: more than one file for disc {', '.join(g.clash)}"
            )
            continue
        if g.missing:
            have = len(g.matches)
            problems.append(
                f"{g.system}: {g.name}: incomplete multi-disc game, {have} disc(s) here, "
                f"missing disc {', '.join(g.missing)}"
            )
            continue
        if g.multi and g.system in M3U_SYSTEMS:
            units.append(Unit(g.system, g.name + ".m3u", g.matches))
            continue
        for m in g.matches:
            ext = output_extension(g.system, compressed(m))
            units.append(Unit(g.system, m.game.name + ext, (m,)))
    return units, problems


def unverified_match(system: str, info: FileInfo, stem: str | None = None) -> GameMatch:
    """A stand-in for a DAT match of a rom no DAT knows: a game named after the file, with
    one rom that is the file (its own name, size and hashes), so the same zipping and
    checks as for a DAT game apply. With `stem` (the name of the archive that holds the
    only rom), the game and the rom take that name, and the rom keeps its own extension."""
    base = re.split(r"[\\/]", info.path)[-1]
    if stem:
        base = stem + os.path.splitext(base)[1]
    rom = datmod.Rom(base, info.size, info.crc, info.sha1)
    return GameMatch(system, datmod.Game(os.path.splitext(base)[0], (rom,)), (info,))


# ------------------------------------------------------------------ tools and options


def default_cache_dir() -> Path:
    base = os.environ.get("XDG_CACHE_HOME")
    return (Path(base) if base else Path.home() / ".cache") / "warpzone" / "dats"


# ------------------------------------------------------------------ files


def header_bytes(head: bytes) -> int:
    """How many leading bytes of a file are a dumper's header: 16 for an iNES (NES) file,
    128 for an A78 (Atari 7800) file, 0 for any other. `head` is the start of the file."""
    if len(head) >= INES_HEADER_BYTES and head.startswith(INES_MAGIC):
        return INES_HEADER_BYTES
    if len(head) >= A78_HEADER_BYTES and head[1:10] == A78_MAGIC:
        return A78_HEADER_BYTES
    return 0


def fingerprint(path: Path, rel: str) -> FileInfo:
    """Size, CRC32 and SHA-1 of a file; for one that starts with an iNES or A78 header, also
    those of the data after it, which `dat.match_dat` tries when the whole file matches
    nothing."""
    with path.open("rb") as f:
        head = f.read(A78_HEADER_BYTES)
        skip = header_bytes(head)
        crc, sha1, size = zlib.crc32(head), hashlib.sha1(head), len(head)
        body = head[skip:]
        body_crc, body_sha1, body_size = zlib.crc32(body), hashlib.sha1(body), len(body)
        while chunk := f.read(CHUNK):
            crc = zlib.crc32(chunk, crc)
            sha1.update(chunk)
            size += len(chunk)
            if skip:
                body_crc = zlib.crc32(chunk, body_crc)
                body_sha1.update(chunk)
                body_size += len(chunk)
    without_header = (
        (body_size, f"{body_crc & 0xFFFFFFFF:08x}", body_sha1.hexdigest()) if skip else None
    )
    return FileInfo(rel, size, f"{crc & 0xFFFFFFFF:08x}", sha1.hexdigest(), without_header)


def gcz_data_size(path: Path) -> int | None:
    """The size of the image inside a GCZ, from its header (little-endian: u32 magic
    0xB10BC001, u32 sub type, u64 compressed size, u64 data size, ...), or None when the
    file does not start with the magic, which `dolphin-tool` then rejects."""
    try:
        with path.open("rb") as f:
            head = f.read(24)
    except OSError:
        return None
    if len(head) < 24 or int.from_bytes(head[:4], "little") != GCZ_MAGIC:
        return None
    return int.from_bytes(head[16:24], "little")


def expand_compressed(
    staged: list[Staged], work: Path, tools: Tools
) -> tuple[list[Staged], list[str]]:
    """GameCube .gcz images are expanded to an .iso (needs dolphin-tool) so they can be
    identified; the compressed file is what gets placed. (.rvz and .wia carry a copy of the
    disc header and are identified without a tool by `inbox_dolphin`.)"""
    out: list[Staged] = []
    failures: list[str] = []
    for s in staged:
        if s.path.suffix.lower() not in COMPRESSED_GC:
            out.append(s)
            continue
        if not tools.dolphin_tool:
            failures.append(f"{s.rel}: needs dolphin-tool to identify a .gcz GameCube image")
            continue
        size = gcz_data_size(s.path)
        if size is not None:
            try:
                free_space_check(work, size)
            except ItemFailure as exc:
                failures.append(f"{s.rel}: {exc}")
                continue
        iso = next_free(work, "d") / (s.path.stem + ".iso")
        iso.parent.mkdir(parents=True)
        try:
            proc = run_process(
                [tools.dolphin_tool, "convert", "-i", str(s.path), "-o", str(iso), "-f", "iso"],
                tools.timeout,
            )
        except ToolTimeout as exc:
            failures.append(f"{s.rel}: {exc}")
            continue
        if proc.returncode != 0 or not iso.is_file():
            failures.append(f"{s.rel}: dolphin-tool could not read it ({tail(proc.stderr)})")
            continue
        out.append(Staged(iso, s.rel.rsplit(".", 1)[0] + ".iso", origin=s.path))
    return out, failures


# ------------------------------------------------------------------ identification


def identify(
    library: DatLibrary, files: Sequence[FileInfo]
) -> tuple[list[GameMatch], list[tuple[str, ...]], list[FileInfo]]:
    """Match files against the DATs, most likely system first, until every file that
    is not a sidecar is explained. Returns matches, ambiguous name sets and unmatched files."""
    remaining = list(files)
    matches: list[GameMatch] = []
    ambiguous: list[tuple[str, ...]] = []

    def significant(f: FileInfo) -> bool:
        return os.path.splitext(f.path)[1].lower() not in SIDECAR_EXTENSIONS

    hinted = [
        EXTENSION_HINTS[e]
        for e in dict.fromkeys(os.path.splitext(f.path)[1].lower() for f in files)
        if e in EXTENSION_HINTS
    ]
    big = any(f.size > 64 * 2**20 for f in files)
    discs = [*DISC_SYSTEMS, "psp", "gc"]
    rest = [s for s in library.systems() if s not in discs]
    order = [*hinted, *(discs + rest if big else rest + discs)]
    for system in dict.fromkeys(order):
        if system not in library.paths or not any(significant(f) for f in remaining):
            continue
        for dat in library.load(system):
            found = datmod.match_dat(dat, remaining)
            matches.extend(found.matches)
            ambiguous.extend(found.ambiguous)
            used = {f.path for m in found.matches for f in m.files}
            remaining = [f for f in remaining if f.path not in used]
    return matches, ambiguous, [f for f in remaining if significant(f)]


# ------------------------------------------------------------------ conversion


def make_chd(
    chdman: str,
    kind: str,
    source: Path,
    out: Path,
    timeout: float,
    threads: int | None = None,
) -> None:
    """Convert a disc image to a CHD and verify it. `threads` is chdman's `--numprocessors`
    (None leaves chdman to count the cores)."""
    command = [chdman, "createdvd" if kind == "dvd" else "createcd", "-i", str(source)]
    command += ["-o", str(out)]
    if threads is not None:
        command += ["--numprocessors", str(threads)]
    code, text = run(command, timeout)
    if code != 0:
        raise ItemFailure(f"chdman could not convert {source.name} ({tail(text)})")
    code, text = run([chdman, "verify", "-i", str(out)], timeout)
    if code != 0 or "successful" not in text.lower():
        raise ItemFailure(f"chdman verify failed for {out.name} ({tail(text)})")


def make_zip(out: Path, game: datmod.Game, sources: Sequence[Path]) -> None:
    """A zip with each rom under its DAT name, always the same bytes for the same game."""
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as z:
        for rom, src in zip(game.roms, sources, strict=True):
            info = zipfile.ZipInfo(rom.name, ZIP_DATE)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o644 << 16
            with src.open("rb") as r, z.open(info, "w") as w:
                shutil.copyfileobj(r, w, CHUNK)


# ------------------------------------------------------------------ one item


@dataclass
class Prepared:
    kind: str  # cd | dvd | zip | iso | rvz (a compressed image kept as it is)
    sources: tuple[Path, ...]  # one per rom of the game
    chd_input: Path | None  # what chdman reads (cue, gdi or iso)
    ra_input: Path | None
    out_name: str


# ------------------------------------------------------------------ kind handlers


def kind_handlers() -> list[KindHandler]:
    """The registry: one line per handler."""
    return [
        inbox_mame.HANDLER,
        inbox_switch.HANDLER,
        inbox_console_iso.HANDLER,
        inbox_vita.HANDLER,
        inbox_n3ds.HANDLER,
        inbox_dolphin.HANDLER,
    ]


def unmatched_handlers() -> list[UnmatchedHandler]:
    return [inbox_console_iso.HANDLER]


def _link_tracks(directory: Path, tracks: Sequence[Path]) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    for t in tracks:
        link = directory / t.name
        if not link.exists():
            link.symlink_to(t.resolve())


def find_sidecar(
    match: GameMatch, staged: Mapping[str, Staged]
) -> tuple[str, tuple[str, ...]] | None:
    """The cue or gdi of a disc: (its item path, the item paths of all its tracks).

    The DATs list only the data track of many discs, so the tracks of the sidecar are
    a superset of the matched files; the audio tracks travel with the game. A sidecar
    that names a file the item does not have, or one outside the item, is never used."""
    wanted = {f.path for f in match.files}
    available = {rel.lower(): rel for rel in staged}
    best: tuple[str, tuple[str, ...]] | None = None
    for rel in sorted(staged):
        ext = os.path.splitext(rel)[1].lower()
        if ext not in (".cue", ".gdi"):
            continue
        try:
            text = staged[rel].path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        found = resolve_refs(rel, gdi_refs(text) if ext == ".gdi" else cue_refs(text), available)
        if found and wanted <= set(found) and (best is None or len(found) < len(best[1])):
            best = (rel, found)
    return best


def find_sbi(match: GameMatch, staged: Mapping[str, Staged]) -> str | None:
    """The LibCrypt subchannel file of a PlayStation disc: an `.sbi` in the folder of the
    disc's cue (or of its data track when there is no cue) named like that cue, that track
    or the DAT game. Redump ships one for each protected PAL disc; a CHD does not keep the
    subchannel, and the emulators read `<disc>.sbi` next to the disc instead."""
    if match.system != "psx":
        return None
    found = find_sidecar(match, staged)
    anchor = found[0] if found else match.files[0].path
    folder = posixpath.dirname(anchor)
    stems = {
        posixpath.splitext(posixpath.basename(anchor))[0].lower(),
        posixpath.splitext(posixpath.basename(match.files[0].path))[0].lower(),
        match.game.name.lower(),
    }
    for rel in sorted(staged):
        stem, ext = posixpath.splitext(rel)
        if (
            ext.lower() == ".sbi"
            and posixpath.dirname(rel) == folder
            and posixpath.basename(stem).lower() in stems
        ):
            return rel
    return None


def with_sbis(unit: Unit, staged: Mapping[str, Staged]) -> Unit:
    """The unit with the `.sbi` of each disc. A PlayStation game with one goes in a
    `<game>.m3u/` folder, so the subchannel file travels with its disc as one library entry."""
    if unit.system != "psx" or unit.unverified:
        return unit
    sbis = tuple(find_sbi(m, staged) for m in unit.matches)
    if not any(sbis):
        return unit
    entry = unit.entry if unit.is_m3u else unit.matches[0].game.name + ".m3u"
    return Unit(unit.system, entry, unit.matches, unit.unverified, sbis)


def sbi_name(out_name: str) -> str:
    return posixpath.splitext(out_name)[0] + ".sbi"


def find_cd_input(
    match: GameMatch, staged: Mapping[str, Staged], scratch: Path
) -> tuple[Path, Path]:
    """(what chdman reads, what RAHasher reads) for a disc image. Without a cue or gdi,
    a lone .bin gets a generated cue (PlayStation only)."""
    found = find_sidecar(match, staged)
    if found:
        rel, tracks = found
        sidecar = staged[rel].path
        ra_input = sidecar
        if match.system == "dreamcast" and sidecar.suffix.lower() == ".cue":
            paths = [staged[r].path for r in tracks]
            link_dir = scratch / "gdi"
            _link_tracks(link_dir, paths)
            sectors = [t.stat().st_size // 2352 for t in paths]
            try:
                cue_text = sidecar.read_text(encoding="utf-8", errors="replace")
                (link_dir / "disc.gdi").write_text(
                    ra.cue_to_gdi(cue_text, sectors), encoding="utf-8"
                )
                ra_input = link_dir / "disc.gdi"
            except ValueError:
                pass
        return sidecar, ra_input
    rels = [f.path for f in match.files]
    only = staged[rels[0]].path
    if match.system == "psx" and len(rels) == 1 and only.suffix.lower() == ".bin":
        link_dir = scratch / "cue"
        _link_tracks(link_dir, [only])
        cue = link_dir / (only.stem + ".cue")
        cue.write_text(
            f'FILE "{only.name}" BINARY\n  TRACK 01 MODE2/2352\n    INDEX 01 00:00:00\n',
            encoding="utf-8",
        )
        return cue, cue
    raise ItemFailure(f"{match.game.name}: no .cue or .gdi in the item names its track file(s)")


def prepare_disc(
    system: str, match: GameMatch, staged: Mapping[str, Staged], scratch: Path
) -> Prepared:
    files = [staged[f.path] for f in match.files]
    sources = tuple(s.path for s in files)
    name = match.game.name
    if system in DISC_SYSTEMS:
        first = sources[0].suffix.lower()
        if system == "ps2" and first == ".iso":
            return Prepared("dvd", sources, sources[0], sources[0], name + ".chd")
        chd_in, ra_in = find_cd_input(match, staged, scratch)
        return Prepared("cd", sources, chd_in, ra_in, name + ".chd")
    if system == "psp":
        return Prepared("iso", sources, None, sources[0], name + ".iso")
    if system == "gc":
        if files[0].origin:
            return Prepared(
                "rvz", (files[0].origin,), None, None, name + files[0].origin.suffix.lower()
            )
        return Prepared("iso", sources, None, sources[0], name + ".iso")
    return Prepared("zip", sources, None, sources[0], name + ".zip")


def _verify_placed(prepared: Prepared, match: GameMatch, part: Path) -> None:
    """Check the bytes that reached the library before they get their final name."""
    if prepared.kind == "iso":
        rom = match.game.roms[0]
        if part.stat().st_size != rom.size or f"{crc_of(part):08x}" != rom.crc:
            raise ItemFailure(f"{match.game.name}: the copy does not match the DAT (size/CRC32)")
    elif prepared.kind == "zip":
        wanted = {(r.size, int(r.crc, 16)) for r in match.game.roms}
        if zip_members(part) != wanted:
            raise ItemFailure(f"{match.game.name}: the zip does not match the DAT (size/CRC32)")


def _ra_lines(
    ctx: Context, system: str, prepared: Prepared, label: str, produced: Path | None
) -> tuple[list[str], str, str | None]:
    """RA status lines for one disc, a failure reason if the conversion changed the hash, and
    the RA hash of what is placed (None when there is none to record)."""
    tools = ctx.tools
    if not tools.rahasher or system not in ra.CONSOLE_IDS or prepared.kind == "rvz":
        return [], "", None
    if prepared.ra_input is None:
        return [], "", None
    before = ra.hash_image(tools.rahasher, system, prepared.ra_input, timeout=tools.timeout)
    if before is None:
        return [f"RA{label}: source hash unavailable"], "", None
    if ctx.lookup.enabled:
        text = ctx.lookup.status(system, before)
    else:
        text = f"hash {before} (no {ra.KEY_ENV}: not looked up)"
    lines = [f"RA{label}: {text}"]
    if produced is not None and prepared.kind in ("cd", "dvd", "zip"):
        after = ra.hash_image(tools.rahasher, system, produced, timeout=tools.timeout)
        if after is None:
            return (
                lines,
                f"RA hash of {produced.name} could not be computed after the conversion",
                None,
            )
        if after != before:
            return (
                lines,
                f"RA hash changed by the conversion of {produced.name}: {before} -> {after}",
                None,
            )
        lines[0] += " [hash kept]"
        return lines, "", after
    # An "iso" is the source itself, copied and verified: its hash is the placed file's.
    return lines, "", before if prepared.kind == "iso" else None


def unsafe_names(unit: Unit) -> str | None:
    """Why a unit is not filed because of one of its names, or None when all are fine.

    The entry is joined to the system folder, each disc becomes a file named after its game and
    each rom name a zip member, and all of them come from a DAT (or, for an unverified rom,
    from the file): every one has to be a plain file name (`inbox_core.unsafe_entry`)."""
    source = "the item" if unit.unverified else "the DAT"
    names: list[str] = []
    for match in unit.matches:
        names.append(match.game.name)
        names.extend(rom.name for rom in match.game.roms)
    for name in (*names, unit.entry):  # the names as their source gives them come first
        refused = refused_name(name, source)
        if refused:
            return refused
    return None


def process_unit(
    ctx: Context, unit: Unit, staged: Mapping[str, Staged], scratch: Path
) -> UnitResult:
    refused = unsafe_names(unit)
    if refused:  # before anything is prepared: no path is ever built from such a name
        return UnitResult(
            unit.system, printable(unit.entry), "review", reason=refused, unverified=unit.unverified
        )
    res = UnitResult(unit.system, unit.entry, "planned" if not ctx.opts.apply else "placed")
    res.unverified = unit.unverified
    if len(unit.matches) == 1 and not unit.entry.endswith(".m3u") and not unit.unverified:
        res.dat = unit.matches[0].game.name
    res.serials = tuple(dict.fromkeys(x for m in unit.matches for x in m.game.serials))
    sysdir = ctx.roms / unit.system
    dest = sysdir / unit.entry
    try:
        if unit.system in DISC_SYSTEMS and not ctx.tools.chdman:
            raise ItemFailure("chdman not found (set [inbox] chdman or put it on PATH)")
        multi = len(unit.matches) > 1
        prepared = [
            prepare_disc(unit.system, m, staged, scratch / f"d{i}")
            for i, m in enumerate(unit.matches)
        ]
        if unit.system == "gc" and prepared[0].kind == "iso":
            res.notes.append(f"could be converted: {DOLPHIN_HINT} -i <iso> -o <rvz>")
        sbis = {
            sbi_name(p.out_name): staged[rel].path
            for p, rel in zip(prepared, unit.sbis, strict=False)
            if rel
        }
        for name in sbis:
            res.notes.append(f"LibCrypt: {name} goes with its disc")
        out_dir = scratch / "out"
        out_dir.mkdir(parents=True, exist_ok=True)
        produced: list[Path] = []
        failure = ""
        for i, (m, p) in enumerate(zip(unit.matches, prepared, strict=True)):
            label = f" disc {i + 1}" if multi else ""
            out: Path | None = None
            if ctx.opts.apply:
                out = _produce(ctx, p, m, out_dir)
                produced.append(out)
            lines, bad, ra_hash = _ra_lines(ctx, unit.system, p, label, out)
            res.ra.extend(lines)
            failure = failure or bad
            if i == 0 and ra_hash and not bad:  # the catalog keeps the hash of the first disc
                res.ra_hash = ra_hash
                res.ra_of = f"{unit.entry}/{p.out_name}" if unit.is_m3u else unit.entry
        if failure:
            raise ItemFailure(failure)
        if not ctx.opts.apply:
            res.outcome = "exists" if dest.exists() else "planned"
            if res.outcome == "exists":
                res.notes.append("already in the library (content is compared with --apply)")
            return res
        _place(ctx, unit, prepared, produced, dest, res, sbis)
    except ItemFailure as exc:
        res.outcome = "review"
        res.reason = str(exc)
    except OSError as exc:
        res.outcome = "review"
        res.reason = f"{unit.entry}: {exc.strerror or exc}"
    return res


def _produce(ctx: Context, p: Prepared, m: GameMatch, out_dir: Path) -> Path:
    """The file to place for one disc: converted, zipped, or the source itself."""
    if p.kind in ("cd", "dvd"):
        assert ctx.tools.chdman and p.chd_input
        out = out_dir / p.out_name
        make_chd(
            ctx.tools.chdman,
            p.kind,
            p.chd_input,
            out,
            ctx.tools.timeout,
            ctx.tools.chdman_threads,
        )
        return out
    if p.kind == "zip":
        out = out_dir / p.out_name
        make_zip(out, m.game, p.sources)
        return out
    return p.sources[0]


def _kind_of(p: Prepared) -> str:
    return {"cd": "chd", "dvd": "chd"}.get(p.kind, p.kind)


def _place(
    ctx: Context,
    unit: Unit,
    prepared: Sequence[Prepared],
    produced: Sequence[Path],
    dest: Path,
    res: UnitResult,
    sbis: Mapping[str, Path] | None = None,
) -> None:
    """Copy a unit into the library. `sbis` are the LibCrypt files of an m3u folder's discs,
    by the name they get in it."""
    sbis = sbis or {}
    chdman = ctx.tools.chdman
    timeout = ctx.tools.timeout
    if not unit.is_m3u:
        p, out = prepared[0], produced[0]
        identical = same_content(_kind_of(p), dest, out, chdman, timeout) if dest.exists() else None
        verdict = destination_verdict(dest.exists(), identical)
        if verdict == "already":
            res.outcome = "already"
        elif verdict == "conflict":
            raise ItemFailure(
                f"{unit.entry}: a different file with that name is already in the library"
            )
        else:
            part = dest.with_name(dest.name + ".part")
            part.unlink(missing_ok=True)
            try:
                copy_verified(out, part)
                _verify_placed(p, unit.matches[0], part)
                if dest.exists():
                    raise ItemFailure(f"{unit.entry}: appeared in the library while copying")
                os.rename(part, dest)
            finally:
                part.unlink(missing_ok=True)
        return
    names = [p.out_name for p in prepared]
    playlist = "".join(n + "\n" for n in names)
    inner = unit.entry  # the playlist file has the folder's name
    if dest.exists():
        same = (
            dest.is_dir()
            and sorted(os.listdir(dest)) == sorted([*names, *sbis, inner])
            and (dest / inner).read_text(encoding="utf-8", errors="replace") == playlist
            and all((dest / n).read_bytes() == src.read_bytes() for n, src in sbis.items())
            and all(
                same_content(_kind_of(p), dest / p.out_name, out, chdman, timeout)
                for p, out in zip(prepared, produced, strict=True)
            )
        )
        if destination_verdict(True, same) == "already":
            res.outcome = "already"
            return
        raise ItemFailure(
            f"{unit.entry}: a different folder with that name is already in the library"
        )
    part = dest.with_name(dest.name + ".part")
    if part.exists():
        shutil.rmtree(part)
    part.mkdir()
    try:
        for p, out in zip(prepared, produced, strict=True):
            copy_verified(out, part / p.out_name)
        for name, src in sbis.items():
            copy_verified(src, part / name)
        (part / inner).write_text(playlist, encoding="utf-8")
        if dest.exists():
            raise ItemFailure(f"{unit.entry}: appeared in the library while copying")
        os.rename(part, dest)
    finally:
        if part.exists():
            shutil.rmtree(part, ignore_errors=True)


def process_item(
    ctx: Context, item: Path, number: int, unverified: str | None = None
) -> ItemResult:
    """Identify and file one item. `unverified` is the system of the `_unverified/<system>/`
    folder the item was dropped in: a rom no DAT knows may then be filed as it is."""
    result = ItemResult(f"{UNVERIFIED_DIR}/{unverified}/{item.name}" if unverified else item.name)
    work = ctx.work / f"item{number}"
    try:
        newest = max(
            (p.stat().st_mtime for p in [item, *item.rglob("*")] if not p.is_symlink()),
            default=0,
        )
        age = ctx.opts.now() - newest
        if age < SETTLE_SECONDS:
            result.left = f"changed {int(max(age, 0))} s ago, still being copied? left in the inbox"
            return result
        work.mkdir(parents=True)
        _identify_and_place(ctx, item, work, result, unverified)
    except ItemFailure as exc:
        result.reasons.append(str(exc))
    except OSError as exc:
        result.reasons.append(f"{item.name}: {exc.strerror or exc}")
    finally:
        shutil.rmtree(work, ignore_errors=True)
    return result


def _identify_and_place(
    ctx: Context, item: Path, work: Path, result: ItemResult, unverified: str | None = None
) -> None:
    for handler in kind_handlers():
        if handler.claims(ctx, item):
            handler.process(ctx, item, work, result)
            return
    early = unsupported_reason([item.name])
    if early:
        result.reasons.append(early)
        return
    staged, notes, failures = stage_item(item, work, ctx.tools)
    result.notes.extend(notes)
    if failures:
        result.reasons.extend(failures)
        return
    unsupported = unsupported_reason([s.rel for s in staged])
    if unsupported:
        result.reasons.append(unsupported)
        return
    staged, unreadable = expand_compressed(staged, work, ctx.tools)
    result.reasons.extend(unreadable)
    infos: list[FileInfo] = []
    by_rel: dict[str, Staged] = {}
    for s in staged:
        by_rel[s.rel] = s
        if s.path.suffix.lower() not in SIDECAR_EXTENSIONS:
            infos.append(fingerprint(s.path, s.rel))
    matches, ambiguous, unmatched = identify(ctx.library, infos)
    owned = {
        track
        for m in matches
        if m.system in DISC_SYSTEMS and (found := find_sidecar(m, by_rel))
        for track in found[1]
    }
    owned |= {sbi for m in matches if (sbi := find_sbi(m, by_rel))}
    unmatched = [f for f in unmatched if f.path not in owned]
    for second in unmatched_handlers():
        taken = second.adopt(ctx, by_rel, unmatched, result)
        unmatched = [f for f in unmatched if f.path not in taken]
    for names in ambiguous:
        result.reasons.append("ambiguous DAT match, cannot tell apart: " + " | ".join(names))
    extra: list[Unit] = []
    if unverified and unmatched:
        extra, unmatched = _unverified_units(unverified, item, by_rel, unmatched, result)
    if unmatched:
        shown = ", ".join(f.path for f in unmatched[:5]) + (", ..." if len(unmatched) > 5 else "")
        what = "no DAT match" if not matches else "files not in any DAT (the rest was placed)"
        result.reasons.append(f"{what}: {shown}")
    units: list[Unit] = []
    if matches:
        dats = {s: ctx.library.load(s) for s in {m.system for m in matches}}
        groups = datmod.group_discs(matches, dats)
        units, problems = plan_units(groups, lambda m: by_rel[m.files[0].path].origin is not None)
        units = [with_sbis(u, by_rel) for u in units]
        result.reasons.extend(problems)
    units.extend(extra)
    for i, unit in enumerate(units):
        scratch = work / f"u{i}"
        unit_result = process_unit(ctx, unit, by_rel, scratch)
        result.units.append(unit_result)
        if unit_result.outcome == "review":
            result.reasons.append(unit_result.reason)


def _unverified_units(
    system: str,
    item: Path,
    staged: Mapping[str, Staged],
    unmatched: Sequence[FileInfo],
    result: ItemResult,
) -> tuple[list[Unit], list[FileInfo]]:
    """The files no DAT knows that were dropped in `_unverified/<system>/`: one unit for each
    that looks like a rom of the system, a review reason for each that does not. Returns the
    units and the files not dealt with (none: each one is filed or explained).

    When the item is an archive and exactly one of its files is a rom to file, the game takes
    the archive's name (the curated one); otherwise each rom keeps its own file name."""
    units: list[Unit] = []
    roms: list[FileInfo] = []
    for info in unmatched:
        with staged[info.path].path.open("rb") as f:
            head = f.read(INES_HEADER_BYTES)
        why = unverified_reason(system, info.path, head, info.size)
        if why:
            result.reasons.append(why)
            continue
        roms.append(info)
    stem = os.path.splitext(item.name)[0] if _is_archive(item) and len(roms) == 1 else None
    for info in roms:
        match = unverified_match(system, info, stem)
        units.append(Unit(system, match.game.name + ".zip", (match,), unverified=True))
    return units, []


def _is_archive(item: Path) -> bool:
    return item.is_file() and item.suffix.lower() in ARCHIVE_EXTENSIONS


# ------------------------------------------------------------------ inbox folder


def scan_unverified(inbox: Path) -> list[tuple[Path, str]]:
    """The items of `inbox/_unverified/<system>/`, with the folder's system name: the files
    and folders inside each system folder. Anything else in `_unverified/` is no item."""
    root = inbox / UNVERIFIED_DIR
    if not root.is_dir() or root.is_symlink():
        return []
    items: list[tuple[Path, str]] = []
    for folder in sorted(root.iterdir(), key=lambda q: q.name):
        if skip_name(folder.name) or folder.is_symlink() or not folder.is_dir():
            continue
        for p in sorted(folder.iterdir(), key=lambda q: q.name):
            if not skip_name(p.name) and not p.is_symlink():
                items.append((p, folder.name))
    return items


def _stray_unverified(inbox: Path) -> list[str]:
    """Names directly in `_unverified/` that are not system folders: they are left alone."""
    root = inbox / UNVERIFIED_DIR
    if not root.is_dir() or root.is_symlink():
        return []
    return [
        p.name
        for p in sorted(root.iterdir(), key=lambda q: q.name)
        if not skip_name(p.name) and (p.is_symlink() or not p.is_dir())
    ]


def scan(inbox: Path) -> list[Path]:
    items: list[Path] = []
    for p in sorted(inbox.iterdir(), key=lambda q: q.name):
        if skip_name(p.name) or (p.is_dir() and p.name.startswith("_")):
            continue
        if p.is_symlink():
            continue
        items.append(p)
    return items


def _unique(folder: Path, name: str) -> Path:
    """`folder/name`, or `folder/name (2)` and so on when that is taken (or has a reason file)."""
    target = folder / name
    stem, ext = os.path.splitext(name)
    n = 2
    while target.exists() or (folder / (target.name + ".reason.txt")).exists():
        target = folder / f"{stem} ({n}){ext}"
        n += 1
    return target


def move_item(item: Path, folder: Path) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    target = _unique(folder, item.name)
    shutil.move(str(item), str(target))
    return target


def write_reason(target: Path, reasons: Sequence[str], notes: Sequence[str]) -> None:
    lines = ["warpzone inbox could not place this item.", "", *(f"- {r}" for r in reasons)]
    if notes:
        lines += ["", *notes]
    sidecar = target.with_name(target.name + ".reason.txt")
    sidecar.write_text("\n".join(lines) + "\n", encoding="utf-8")


def purge_done(inbox: Path, today: date) -> list[str]:
    done = inbox / DONE_DIR
    if not done.is_dir():
        return []
    gone = []
    for name in select_purge(sorted(os.listdir(done)), today):
        folder = done / name
        if folder.is_dir() and not folder.is_symlink():
            shutil.rmtree(folder)
            gone.append(name)
    return gone


def _describe_tools(tools: Tools) -> str:
    items = (
        ("chdman", tools.chdman),
        ("bsdtar", tools.bsdtar),
        ("rahasher", tools.rahasher),
        ("dolphin-tool", tools.dolphin_tool),
        ("nsz", tools.nsz),
        ("orbis-pkg-util", tools.orbis_pkg_util),
        ("vita3k", tools.vita3k),
    )
    return ", ".join(f"{n} {'yes' if p else 'no'}" for n, p in items)


def run_inbox(opts: Options, rep: Reporter) -> int:
    """Process the inbox; returns how many items need attention (0 when all are placed)."""
    inbox = opts.root / "inbox"
    if not inbox.is_dir() or not (opts.root / "library" / "roms").is_dir():
        raise InboxError(
            f"{opts.root} has no inbox/ and library/roms/ folders; is --root the hub root?"
        )
    if not opts.dats_dir.is_dir():
        raise InboxError(f"DAT folder not found: {opts.dats_dir} (set [inbox] dats_dir or --dats)")
    for handler in kind_handlers():
        handler.validate(opts)
    library = DatLibrary(opts.dats_dir, opts.cache_dir)
    if not library.systems():
        raise InboxError(f"no Redump or No-Intro DATs warpzone knows in {opts.dats_dir}")
    if not opts.apply:  # a report reads only, so it needs no lock
        return _run(opts, rep, inbox, library)
    with inbox_lock.hold(inbox, force=opts.force_lock, now=opts.now, out=rep.out):
        return _run(opts, rep, inbox, library)


def _run(opts: Options, rep: Reporter, inbox: Path, library: DatLibrary) -> int:
    items = [(p, None) for p in scan(inbox)] + [(p, s) for p, s in scan_unverified(inbox)]
    mode = "apply" if opts.apply else "report only"
    rep.out(f"inbox: {inbox} ({mode}), {len(items)} item(s)")
    rep.out(f"tools: {_describe_tools(opts.tools)}; DATs: {', '.join(library.systems())}")
    for name in _stray_unverified(inbox):
        rep.out(f"{UNVERIFIED_DIR}: ignored {name} (put it in {UNVERIFIED_DIR}/<system>/)")
    if not opts.tools.rahasher:
        rep.out("RA: not checked (set [inbox] rahasher to hash images)")
    elif not opts.ra_key:
        rep.out(f"RA: hashes only (set {ra.KEY_ENV} to look them up)")

    if opts.work_dir:
        opts.work_dir.mkdir(parents=True, exist_ok=True)
    work = Path(tempfile.mkdtemp(prefix="warpzone-inbox-", dir=opts.work_dir))
    ctx = Context(opts, rep, library, work)
    today = opts.today()
    results: list[ItemResult] = []
    try:
        for n, (item, unverified) in enumerate(items, 1):
            result = process_item(ctx, item, n, unverified)
            results.append(result)
            if opts.apply and not result.left:
                try:
                    _file_away(inbox, item, result, today)
                except OSError as exc:
                    result.reasons.append(f"could not move the original: {exc.strerror or exc}")
            _print_item(rep, n, len(items), result, opts.apply, today)
    finally:
        shutil.rmtree(work, ignore_errors=True)

    purged = []
    if opts.apply:
        purged = purge_done(inbox, today)
    else:
        done = inbox / DONE_DIR
        stale = select_purge(sorted(os.listdir(done)), today) if done.is_dir() else []
        if stale:
            rep.out(
                f"{DONE_DIR}: would delete {len(stale)} folder(s) older than {DONE_KEEP_DAYS} days"
            )
    if purged:
        rep.out(f"{DONE_DIR}: deleted {len(purged)} folder(s) older than {DONE_KEEP_DAYS} days")
    from warpzone import (
        inbox_scrape,
    )  # late: it imports `scrape`, and `catalog` imports this module

    if opts.apply:
        _update_catalog(opts, rep, results)
        inbox_scrape.after_apply(opts, rep, results)
    else:
        inbox_scrape.report_only(opts, rep, results)
    return _summary(rep, results, opts.apply)


def _update_catalog(opts: Options, rep: Reporter, results: Sequence[ItemResult]) -> None:
    """Bring the catalog index up to date for the systems this run placed something in.
    A failure is a warning, never a review: the next `warpzone catalog` picks the game up."""
    from warpzone import catalog_store

    placed = [u for r in results for u in r.units if u.outcome == "placed"]
    systems = {u.catalog_system for u in placed}
    if not systems:
        return
    dat = {f"{u.library_folder}/{u.entry}": u.dat for u in placed if u.dat}
    serials = {f"{u.library_folder}/{u.entry}": u.serials for u in placed if u.serials}
    unverified = {f"{u.library_folder}/{u.entry}" for u in placed if u.unverified}
    ra_known = {
        f"{u.library_folder}/{u.entry}": (u.ra_hash, f"{u.library_folder}/{u.ra_of}")
        for u in placed
        if u.ra_hash
    }
    try:
        catalog_store.update_after_inbox(
            opts.root,
            systems,
            dat=dat,
            serials=serials,
            unverified=unverified,
            ra_known=ra_known,
            out=rep.out,
            warn=rep.warn,
            now=datetime.fromtimestamp(opts.now(), UTC),
        )
    except Exception as exc:  # the files are filed; the index can be rebuilt
        rep.warn(f"catalog: not updated ({type(exc).__name__}: {exc}); run `warpzone catalog`")


def _file_away(inbox: Path, item: Path, result: ItemResult, today: date) -> None:
    if result.review:
        target = move_item(item, inbox / REVIEW_DIR)
        write_reason(target, result.reasons or ["see the unit reasons"], result.notes)
        result.moved_to = f"{REVIEW_DIR}/{target.name}"
    else:
        target = move_item(item, inbox / DONE_DIR / today.isoformat())
        result.moved_to = f"{DONE_DIR}/{today.isoformat()}/{target.name}"


def _print_item(
    rep: Reporter, n: int, total: int, result: ItemResult, apply: bool, today: date
) -> None:
    rep.out(f"[{n}/{total}] {result.name}")
    if result.left:
        rep.out(f"  left: {result.left}")
        return
    words = {
        "placed": "placed",
        "already": "already in the library",
        "planned": "would place",
        "exists": "already exists",
        "review": "REVIEW",
    }
    for u in result.units:
        word = "would place (unverified)" if u.unverified and u.outcome == "planned" else None
        tag = (
            ", not in any DAT (filed from _unverified)"
            if u.unverified and u.outcome != "review"
            else ""
        )
        rep.out(f"  {u.system}: {u.entry}: {word or words[u.outcome]}{tag}")
        if u.outcome != "review":
            rep.out(f"    path: {u.path}")
        for line in u.ra:
            rep.out(f"    {line}")
        for note in u.notes:
            rep.out(f"    note: {note}")
        if u.outcome in ("placed", "already", "planned", "exists"):
            for line in u.whitelist_lines:
                rep.out(f"    whitelist: {line}")
    for note in result.notes:
        rep.out(f"  note: {note}")
    for reason in result.reasons:
        rep.out(f"  review: {reason}")
    if apply and not result.moved_to:
        rep.out("  original left in the inbox")
    elif apply:
        rep.out(f"  original -> inbox/{result.moved_to}")
    elif result.review:
        rep.out(f"  would move to inbox/{REVIEW_DIR}/ with a reason file")
    else:
        rep.out(f"  would move to inbox/{DONE_DIR}/{today.isoformat()}/")


def _summary(rep: Reporter, results: Sequence[ItemResult], apply: bool) -> int:
    units = [u for r in results for u in r.units]
    placed = [u for u in units if u.outcome in ("placed", "planned")]
    already = [u for u in units if u.outcome in ("already", "exists")]
    review = [r for r in results if r.review]
    left = [r for r in results if r.left]
    lines = sorted(
        {
            line
            for u in units
            if u.outcome in ("placed", "already", "planned", "exists")
            for line in u.whitelist_lines
        }
    )
    if any(u.folder.startswith("extras/") and u.outcome != "review" for u in units):
        rep.out(
            "Switch updates and DLC are usually whitelisted by folder, for example "
            "!/extras/switch/updates and !/extras/switch/dlc"
        )
    if lines:
        rep.out("whitelist lines (warpzone never edits a whitelist; add what you want):")
        for line in lines:
            rep.out(f"  {line}")
    verb = "placed" if apply else "to place"
    rep.out(
        f"items: {len(results)}; games {verb}: {len(placed)}, already in the library: "
        f"{len(already)}; items for review: {len(review)}"
        + (f"; left in the inbox: {len(left)}" if left else "")
    )
    if not apply and results:
        rep.out("report only: run again with --apply to convert and place")
    return len(review)
