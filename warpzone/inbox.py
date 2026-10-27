"""Process new games dropped in `inbox/`: identify, convert, file into the library.

Report only by default; `apply` converts, places and moves the originals. Each item
(a file or folder at the top of `inbox/`) is unpacked into a local work folder, every
file is identified by content against Redump / No-Intro DATs (`dat.py`), converted to
the library's format and copied into `library/roms/<system>/` without ever replacing
an existing file. Originals go to `inbox/_done/<date>/` (purged after 7 days), items
that could not be placed go to `inbox/_revisar/` with a `<item>.reason.txt`.

The decisions (`skip_name`, `unsafe_member`, `select_purge`, `plan_units`,
`destination_verdict`, `whitelist_line`, `cue_refs`, `gdi_refs`, `resolve_refs`,
`unsupported_reason`) are pure; the rest touches the disk. See docs/design.md.
"""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import subprocess
import tempfile
import time
import zipfile
import zlib
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Protocol

from warpzone import dat as datmod
from warpzone import ra
from warpzone.config import InboxSettings
from warpzone.dat import (
    DISC_SYSTEMS,
    EXTENSION_HINTS,
    M3U_SYSTEMS,
    DatLibrary,
    DiscGroup,
    FileInfo,
    GameMatch,
)
from warpzone.provision import Reporter

DONE_DIR = "_done"
REVIEW_DIR = "_revisar"
DONE_KEEP_DAYS = 7
SETTLE_SECONDS = 30  # an item changed more recently than this may still be copying
MAX_ARCHIVE_DEPTH = 2
CHUNK = 8 * 1024 * 1024
ARCHIVE_EXTENSIONS = (".zip", ".7z", ".rar")
BSDTAR_EXTENSIONS = (".7z", ".rar")
COMPRESSED_GC = (".rvz", ".gcz", ".wia")
# Files that travel with a dump but are not part of any DAT; never identified.
SIDECAR_EXTENSIONS = frozenset(
    {
        *(".cue", ".gdi", ".m3u", ".txt", ".nfo", ".sfv", ".md5", ".sha1", ".dat", ".url"),
        *(".jpg", ".jpeg", ".png", ".pdf", ".ini", ".db", ".diz", ".lnk", ".xml"),
    }
)
JUNK_NAMES = frozenset({".DS_Store", "Thumbs.db", "desktop.ini", "@eaDir", "#recycle", "__MACOSX"})
ZIP_DATE = (1996, 12, 24, 23, 32, 0)  # fixed, so the same game always zips to the same bytes
UNSUPPORTED_EXTENSIONS = {
    ".vpk": "Vita",
    ".xiso": "Xbox",
    ".pkg": "PS3",
    ".rap": "PS3",
}
DOLPHIN_HINT = "dolphin-tool convert -f rvz -c zstd -l 5 -b 131072"
_DATE_DIR = re.compile(r"^\d{4}-\d\d-\d\d$")
_CUE_FILE = re.compile(r'^\s*FILE\s+(?:"(.+)"|(\S+))\s+\w+', re.IGNORECASE | re.MULTILINE)
_GDI_TRACK = re.compile(r'^\s*\d+\s+\d+\s+\d+\s+\d+\s+(?:"(.+)"|(\S+))\s+\d+\s*$', re.MULTILINE)
_DATA_SHA1 = re.compile(r"^\s*Data SHA1:\s*([0-9a-f]{40})\s*$", re.IGNORECASE | re.MULTILINE)
_SHA1 = re.compile(r"^\s*SHA1:\s*([0-9a-f]{40})\s*$", re.IGNORECASE | re.MULTILINE)
_WILDCARDS = re.compile(r"([\\*?\[\]{}])")


class InboxError(Exception):
    """The run cannot start (bad layout, no DATs). Per-item problems are reported instead."""


class ItemFailure(Exception):
    """One item or unit cannot be placed; the message is the reason given for review."""


# ------------------------------------------------------------------ pure decisions


def skip_name(name: str) -> bool:
    """Hidden files, OS junk and thumbnails are never part of an item."""
    return name.startswith(".") or name in JUNK_NAMES


def unsafe_member(name: str) -> str | None:
    """Why an archive member name must not be extracted, or None when it is fine."""
    if "\0" in name:
        return "a NUL byte in the name"
    if name.startswith(("/", "\\")) or re.match(r"^[A-Za-z]:", name):
        return "an absolute path"
    if ".." in re.split(r"[\\/]", name):
        return "a '..' component"
    return None


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


def whitelist_line(system: str, entry_name: str) -> str:
    """The Syncthing whitelist line that would deliver `roms/<system>/<entry_name>`."""
    return "!/roms/" + system + "/" + _WILDCARDS.sub(r"\\\1", entry_name)


def unsupported_reason(rels: Sequence[str]) -> str | None:
    for rel in rels:
        parts = rel.split("/")
        kind = UNSUPPORTED_EXTENSIONS.get(os.path.splitext(parts[-1])[1].lower())
        if "PS3_GAME" in parts:
            kind = "PS3"
        if parts[-1].lower() == "default.xbe":
            kind = "Xbox"
        if kind:
            return f"not supported by inbox yet ({kind}: {rel})"
    return None


def destination_verdict(exists: bool, identical: bool | None) -> str:
    """ "place", "already" (the library has the same content) or "conflict"."""
    if not exists:
        return "place"
    return "already" if identical else "conflict"


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

    @property
    def is_m3u(self) -> bool:
        return len(self.matches) > 1 or self.entry.endswith(".m3u")


def output_extension(system: str, from_compressed: bool = False) -> str:
    if system in DISC_SYSTEMS:
        return ".chd"
    if system == "psp":
        return ".iso"
    if system == "gc":
        return ".rvz" if from_compressed else ".iso"
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


# ------------------------------------------------------------------ tools and options


@dataclass(frozen=True)
class Tools:
    chdman: str | None = None
    bsdtar: str | None = None
    rahasher: str | None = None
    dolphin_tool: str | None = None
    nsz: str | None = None


def find_tools(settings: InboxSettings, which: Callable[[str], str | None] = shutil.which) -> Tools:
    """Configured paths must exist; unset tools are looked up on PATH (RAHasher only
    when configured)."""

    def pick(key: str, configured: str | None, command: str | None) -> str | None:
        if configured:
            if not (os.path.isfile(configured) and os.access(configured, os.X_OK)):
                raise InboxError(f"[inbox] {key}: {configured} is not an executable file")
            return configured
        return which(command) if command else None

    return Tools(
        chdman=pick("chdman", settings.chdman, "chdman"),
        bsdtar=pick("bsdtar", settings.bsdtar, "bsdtar"),
        rahasher=pick("rahasher", settings.rahasher, None),
        dolphin_tool=pick("dolphin_tool", settings.dolphin_tool, "dolphin-tool"),
        nsz=pick("nsz", settings.nsz, "nsz"),
    )


@dataclass(frozen=True)
class Options:
    root: Path  # the hub root as mounted here: holds library/ and inbox/
    dats_dir: Path
    tools: Tools
    apply: bool = False
    work_dir: Path | None = None
    cache_dir: Path | None = None
    ra_key: str | None = None
    ra_fetch: ra.Fetch | None = None
    settings: InboxSettings = field(default_factory=InboxSettings)  # read by kind handlers
    now: Callable[[], float] = time.time
    today: Callable[[], date] = date.today


def default_cache_dir() -> Path:
    base = os.environ.get("XDG_CACHE_HOME")
    return (Path(base) if base else Path.home() / ".cache") / "warpzone" / "dats"


# ------------------------------------------------------------------ files


def fingerprint(path: Path, rel: str) -> FileInfo:
    crc = 0
    sha1 = hashlib.sha1()
    size = 0
    with path.open("rb") as f:
        while chunk := f.read(CHUNK):
            crc = zlib.crc32(chunk, crc)
            sha1.update(chunk)
            size += len(chunk)
    return FileInfo(rel, size, f"{crc & 0xFFFFFFFF:08x}", sha1.hexdigest())


def file_sha1(path: Path) -> str:
    digest = hashlib.sha1()
    with path.open("rb") as f:
        while chunk := f.read(CHUNK):
            digest.update(chunk)
    return digest.hexdigest()


def copy_verified(src: Path, dest: Path) -> None:
    """Copy `src` to `dest` and read `dest` back: a share that loses bytes is caught here."""
    digest = hashlib.sha1()
    with src.open("rb") as r, dest.open("wb") as w:
        while chunk := r.read(CHUNK):
            digest.update(chunk)
            w.write(chunk)
    if file_sha1(dest) != digest.hexdigest():
        raise ItemFailure(f"the copy of {src.name} did not read back identical")


@dataclass(frozen=True)
class Staged:
    path: Path  # where the bytes are (the inbox itself, or the local work folder)
    rel: str  # name within the item, "/" separated
    origin: Path | None = None  # the compressed GameCube image this was expanded from


def _free_space_check(dest: Path, needed: int) -> None:
    if shutil.disk_usage(dest).free < needed * 1.05:
        raise ItemFailure(f"not enough free space in {dest} to unpack ({needed // 2**20} MiB)")


def extract_zip(archive: Path, dest: Path) -> None:
    dest.mkdir(parents=True, exist_ok=True)
    root = dest.resolve()
    try:
        with zipfile.ZipFile(archive) as z:
            infos = z.infolist()
            for info in infos:
                why = unsafe_member(info.filename)
                if why:
                    raise ItemFailure(f"{archive.name}: refused, a member has {why}")
                if (info.external_attr >> 16) & 0o170000 == 0o120000:
                    raise ItemFailure(f"{archive.name}: refused, it contains a symbolic link")
            _free_space_check(dest, sum(i.file_size for i in infos))
            for info in infos:
                target = (root / info.filename).resolve()
                if root not in target.parents and target != root:
                    raise ItemFailure(f"{archive.name}: refused, a member leaves the folder")
                if info.is_dir():
                    target.mkdir(parents=True, exist_ok=True)
                    continue
                target.parent.mkdir(parents=True, exist_ok=True)
                with z.open(info) as r, target.open("wb") as w:
                    shutil.copyfileobj(r, w, CHUNK)
    except (zipfile.BadZipFile, OSError, NotImplementedError, RuntimeError) as exc:
        raise ItemFailure(f"{archive.name}: cannot unpack ({exc})") from exc


def extract_bsdtar(bsdtar: str, archive: Path, dest: Path) -> None:
    dest.mkdir(parents=True, exist_ok=True)
    listing = subprocess.run(
        [bsdtar, "-tf", str(archive)], capture_output=True, text=True, check=False
    )
    if listing.returncode != 0:
        raise ItemFailure(f"{archive.name}: cannot read ({_tail(listing.stderr)})")
    for member in listing.stdout.splitlines():
        why = unsafe_member(member)
        if why:
            raise ItemFailure(f"{archive.name}: refused, a member has {why}")
    done = subprocess.run(
        [bsdtar, "-xf", str(archive), "-C", str(dest), "--no-same-owner", "--no-same-permissions"],
        capture_output=True,
        text=True,
        check=False,
    )
    if done.returncode != 0:
        raise ItemFailure(f"{archive.name}: cannot unpack ({_tail(done.stderr)})")
    for base, dirs, names in os.walk(dest):
        for name in [*dirs, *names]:
            if os.path.islink(os.path.join(base, name)):
                raise ItemFailure(f"{archive.name}: refused, it contains a symbolic link")


def _tail(text: str) -> str:
    lines = text.strip().splitlines()
    return lines[-1][:200] if lines else "no output"


def _nth(work: Path, label: str) -> Path:
    n = 0
    while (work / f"{label}{n}").exists():
        n += 1
    return work / f"{label}{n}"


def stage_item(item: Path, work: Path, tools: Tools) -> tuple[list[Staged], list[str], list[str]]:
    """Every file of an item, archives unpacked into `work`.

    Returns the files, notes (things skipped) and failures (items that cannot be read
    at all, such as an unsafe archive). Symbolic links are never followed."""
    files: list[Staged] = []
    notes: list[str] = []
    failures: list[str] = []

    def visit(path: Path, rel: str, depth: int) -> None:
        if path.is_symlink():
            notes.append(f"skipped symbolic link {rel}")
        elif path.is_dir():
            for child in sorted(path.iterdir(), key=lambda p: p.name):
                if not skip_name(child.name):
                    visit(child, f"{rel}/{child.name}" if rel else child.name, depth)
        elif path.suffix.lower() in ARCHIVE_EXTENSIONS and depth < MAX_ARCHIVE_DEPTH:
            target = _nth(work, "x")
            try:
                if path.suffix.lower() in BSDTAR_EXTENSIONS:
                    if not tools.bsdtar:
                        raise ItemFailure(f"{path.name}: needs bsdtar to unpack (not found)")
                    extract_bsdtar(tools.bsdtar, path, target)
                else:
                    extract_zip(path, target)
            except ItemFailure as exc:
                failures.append(str(exc))
                return
            visit(target, rel, depth + 1)
        elif path.is_file():
            files.append(Staged(path, rel))

    visit(item, "" if item.is_dir() else item.name, 0)
    return files, notes, failures


def expand_compressed(
    staged: list[Staged], work: Path, tools: Tools
) -> tuple[list[Staged], list[str]]:
    """GameCube .rvz/.gcz/.wia images are expanded to an .iso (needs dolphin-tool) so
    they can be identified; the compressed file is what gets placed."""
    out: list[Staged] = []
    failures: list[str] = []
    for s in staged:
        if s.path.suffix.lower() not in COMPRESSED_GC:
            out.append(s)
            continue
        if not tools.dolphin_tool:
            failures.append(f"{s.rel}: needs dolphin-tool to identify a compressed GameCube image")
            continue
        iso = _nth(work, "d") / (s.path.stem + ".iso")
        iso.parent.mkdir(parents=True)
        proc = subprocess.run(
            [tools.dolphin_tool, "convert", "-i", str(s.path), "-o", str(iso), "-f", "iso"],
            capture_output=True,
            text=True,
            check=False,
        )
        if proc.returncode != 0 or not iso.is_file():
            failures.append(f"{s.rel}: dolphin-tool could not read it ({_tail(proc.stderr)})")
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


def _run(cmd: Sequence[str]) -> tuple[int, str]:
    proc = subprocess.run(list(cmd), capture_output=True, text=True, check=False, errors="replace")
    return proc.returncode, (proc.stdout + proc.stderr).replace("\r", "\n")


def chd_data_sha1(chdman: str, chd: Path) -> str | None:
    code, out = _run([chdman, "info", "-i", str(chd)])
    if code != 0:
        return None
    m = _DATA_SHA1.search(out) or _SHA1.search(out)
    return m.group(1).lower() if m else None


def make_chd(chdman: str, kind: str, source: Path, out: Path) -> None:
    code, text = _run(
        [chdman, "createdvd" if kind == "dvd" else "createcd", "-i", str(source), "-o", str(out)]
    )
    if code != 0:
        raise ItemFailure(f"chdman could not convert {source.name} ({_tail(text)})")
    code, text = _run([chdman, "verify", "-i", str(out)])
    if code != 0 or "successful" not in text.lower():
        raise ItemFailure(f"chdman verify failed for {out.name} ({_tail(text)})")


def make_zip(out: Path, game: datmod.Game, sources: Sequence[Path]) -> None:
    """A zip with each rom under its DAT name, always the same bytes for the same game."""
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as z:
        for rom, src in zip(game.roms, sources, strict=True):
            info = zipfile.ZipInfo(rom.name, ZIP_DATE)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o644 << 16
            with src.open("rb") as r, z.open(info, "w") as w:
                shutil.copyfileobj(r, w, CHUNK)


def zip_members(path: Path) -> set[tuple[int, int]]:
    with zipfile.ZipFile(path) as z:
        bad = z.testzip()
        if bad:
            raise ItemFailure(f"{path.name}: damaged member {bad}")
        return {(i.file_size, i.CRC) for i in z.infolist() if not i.is_dir()}


def _crc_of(path: Path) -> int:
    crc = 0
    with path.open("rb") as f:
        while chunk := f.read(CHUNK):
            crc = zlib.crc32(chunk, crc)
    return crc & 0xFFFFFFFF


def same_content(kind: str, existing: Path, produced: Path, chdman: str | None) -> bool:
    """Is the file already in the library the same game as the one just produced?"""
    try:
        if kind == "chd":
            if not chdman:
                return False
            a, b = chd_data_sha1(chdman, existing), chd_data_sha1(chdman, produced)
            return a is not None and a == b
        if kind == "zip":
            return zip_members(existing) == zip_members(produced)
        return existing.stat().st_size == produced.stat().st_size and file_sha1(
            existing
        ) == file_sha1(produced)
    except (OSError, zipfile.BadZipFile, ItemFailure):
        return False


# ------------------------------------------------------------------ one item


@dataclass
class UnitResult:
    system: str
    entry: str
    outcome: str  # placed | already | planned | exists | review
    ra: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    reason: str = ""
    folder: str = ""  # library folder when it is not roms/<system> (Switch updates and DLC)

    @property
    def path(self) -> str:
        return f"library/{self.folder or 'roms/' + self.system}/{self.entry}"

    @property
    def whitelist(self) -> str:
        """The Syncthing line for a file in roms/; none for a folder-whitelisted extra."""
        return "" if self.folder else whitelist_line(self.system, self.entry)


@dataclass
class ItemResult:
    name: str
    units: list[UnitResult] = field(default_factory=list)
    reasons: list[str] = field(default_factory=list)  # why the item needs review
    notes: list[str] = field(default_factory=list)
    left: str = ""  # set when the item stays in the inbox untouched
    moved_to: str = ""

    @property
    def review(self) -> bool:
        return bool(self.reasons) or any(u.outcome == "review" for u in self.units)


@dataclass
class Prepared:
    kind: str  # cd | dvd | zip | iso | rvz
    sources: tuple[Path, ...]  # one per rom of the game
    chd_input: Path | None  # what chdman reads (cue, gdi or iso)
    ra_input: Path | None
    out_name: str


class Context:
    def __init__(self, opts: Options, rep: Reporter, library: DatLibrary, work: Path) -> None:
        self.opts = opts
        self.rep = rep
        self.library = library
        self.work = work
        self.roms = opts.root / "library" / "roms"
        self.tools = opts.tools
        self.lookup = ra.Lookup(opts.ra_key, opts.ra_fetch or ra.http_fetch)


# ------------------------------------------------------------------ kind handlers


class KindHandler(Protocol):
    """A kind of content that does not go through DAT matching and conversion (arcade
    sets, see `inbox_mame.py`). `validate` rejects a bad configuration before anything
    runs (raise `InboxError`); `claims` says whether the handler takes over an item;
    `process` identifies and places it, filling `result` like `_identify_and_place`."""

    def validate(self, opts: Options) -> None: ...

    def claims(self, ctx: Context, item: Path) -> bool: ...

    def process(self, ctx: Context, item: Path, work: Path, result: ItemResult) -> None: ...


def kind_handlers() -> list[KindHandler]:
    """The registry: one line per handler (imported late, they import this module)."""
    from warpzone import inbox_mame, inbox_switch

    return [inbox_mame.HANDLER, inbox_switch.HANDLER]


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
        if part.stat().st_size != rom.size or f"{_crc_of(part):08x}" != rom.crc:
            raise ItemFailure(f"{match.game.name}: the copy does not match the DAT (size/CRC32)")
    elif prepared.kind == "zip":
        wanted = {(r.size, int(r.crc, 16)) for r in match.game.roms}
        if zip_members(part) != wanted:
            raise ItemFailure(f"{match.game.name}: the zip does not match the DAT (size/CRC32)")


def _ra_lines(
    ctx: Context, system: str, prepared: Prepared, label: str, produced: Path | None
) -> tuple[list[str], str]:
    """RA status lines for one disc, and a failure reason if the conversion changed the hash."""
    tools = ctx.tools
    if not tools.rahasher or system not in ra.CONSOLE_IDS or prepared.kind == "rvz":
        return [], ""
    if prepared.ra_input is None:
        return [], ""
    before = ra.hash_image(tools.rahasher, system, prepared.ra_input)
    if before is None:
        return [f"RA{label}: source hash unavailable"], ""
    if ctx.lookup.enabled:
        text = ctx.lookup.status(system, before)
    else:
        text = f"hash {before} (no {ra.KEY_ENV}: not looked up)"
    lines = [f"RA{label}: {text}"]
    if produced is not None and prepared.kind in ("cd", "dvd", "zip"):
        after = ra.hash_image(tools.rahasher, system, produced)
        if after is None:
            return lines, f"RA hash of {produced.name} could not be computed after the conversion"
        if after != before:
            return (
                lines,
                f"RA hash changed by the conversion of {produced.name}: {before} -> {after}",
            )
        lines[0] += " [hash kept]"
    return lines, ""


def process_unit(
    ctx: Context, unit: Unit, staged: Mapping[str, Staged], scratch: Path
) -> UnitResult:
    res = UnitResult(unit.system, unit.entry, "planned" if not ctx.opts.apply else "placed")
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
            lines, bad = _ra_lines(ctx, unit.system, p, label, out)
            res.ra.extend(lines)
            failure = failure or bad
        if failure:
            raise ItemFailure(failure)
        if not ctx.opts.apply:
            res.outcome = "exists" if dest.exists() else "planned"
            if res.outcome == "exists":
                res.notes.append("already in the library (content is compared with --apply)")
            return res
        _place(ctx, unit, prepared, produced, dest, res)
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
        make_chd(ctx.tools.chdman, p.kind, p.chd_input, out)
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
) -> None:
    chdman = ctx.tools.chdman
    if not unit.is_m3u:
        p, out = prepared[0], produced[0]
        identical = same_content(_kind_of(p), dest, out, chdman) if dest.exists() else None
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
            and sorted(os.listdir(dest)) == sorted([*names, inner])
            and (dest / inner).read_text(encoding="utf-8", errors="replace") == playlist
            and all(
                same_content(_kind_of(p), dest / p.out_name, out, chdman)
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
        (part / inner).write_text(playlist, encoding="utf-8")
        if dest.exists():
            raise ItemFailure(f"{unit.entry}: appeared in the library while copying")
        os.rename(part, dest)
    finally:
        if part.exists():
            shutil.rmtree(part, ignore_errors=True)


def process_item(ctx: Context, item: Path, number: int) -> ItemResult:
    result = ItemResult(item.name)
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
        _identify_and_place(ctx, item, work, result)
    except ItemFailure as exc:
        result.reasons.append(str(exc))
    except OSError as exc:
        result.reasons.append(f"{item.name}: {exc.strerror or exc}")
    finally:
        shutil.rmtree(work, ignore_errors=True)
    return result


def _identify_and_place(ctx: Context, item: Path, work: Path, result: ItemResult) -> None:
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
    unmatched = [f for f in unmatched if f.path not in owned]
    for names in ambiguous:
        result.reasons.append("ambiguous DAT match, cannot tell apart: " + " | ".join(names))
    if unmatched:
        shown = ", ".join(f.path for f in unmatched[:5]) + (", ..." if len(unmatched) > 5 else "")
        what = "no DAT match" if not matches else "files not in any DAT (the rest was placed)"
        result.reasons.append(f"{what}: {shown}")
    if not matches:
        return
    dats = {s: ctx.library.load(s) for s in {m.system for m in matches}}
    groups = datmod.group_discs(matches, dats)
    units, problems = plan_units(groups, lambda m: by_rel[m.files[0].path].origin is not None)
    result.reasons.extend(problems)
    for i, unit in enumerate(units):
        scratch = work / f"u{i}"
        unit_result = process_unit(ctx, unit, by_rel, scratch)
        result.units.append(unit_result)
        if unit_result.outcome == "review":
            result.reasons.append(unit_result.reason)


# ------------------------------------------------------------------ inbox folder


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
    items = scan(inbox)
    mode = "apply" if opts.apply else "report only"
    rep.out(f"inbox: {inbox} ({mode}), {len(items)} item(s)")
    rep.out(f"tools: {_describe_tools(opts.tools)}; DATs: {', '.join(library.systems())}")
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
        for n, item in enumerate(items, 1):
            result = process_item(ctx, item, n)
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
    return _summary(rep, results, opts.apply)


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
        rep.out(f"  {u.system}: {u.entry}: {words[u.outcome]}")
        if u.outcome != "review":
            rep.out(f"    path: {u.path}")
        for line in u.ra:
            rep.out(f"    {line}")
        for note in u.notes:
            rep.out(f"    note: {note}")
        if u.outcome in ("placed", "already", "planned", "exists") and u.whitelist:
            rep.out(f"    whitelist: {u.whitelist}")
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
            u.whitelist
            for u in units
            if u.outcome in ("placed", "already", "planned", "exists") and u.whitelist
        }
    )
    if any(u.folder and u.outcome != "review" for u in units):
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
