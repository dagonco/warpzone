"""What the inbox pipeline and its kind handlers share: errors, options, results, staging.

`inbox.py` runs the pipeline (DATs, conversion, filing) and registers the handlers
(`inbox_mame.py`, `inbox_switch.py`, `inbox_console_iso.py`, `inbox_vita.py`,
`inbox_n3ds.py`, `inbox_dolphin.py`, `inbox_ps4.py`, `inbox_vita_pkg.py`). The handlers
import this module and never `inbox.py`, so the imports between the inbox modules form no
cycle. This module imports no other inbox module.

The pure decisions here (`skip_name`, `unsafe_member`, `whitelist_line`,
`unsupported_reason`, `destination_verdict`, `vita3k_executable`) have no I/O; the rest
touches the disk or runs a tool. See development/design/inbox.md.
"""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import subprocess
import sys
import time
import zipfile
import zlib
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Protocol

from warpzone import ra
from warpzone.config import InboxSettings
from warpzone.dat import DatLibrary, FileInfo
from warpzone.errors import UsageError
from warpzone.report import Reporter

MAX_ARCHIVE_DEPTH = 2
CHUNK = 8 * 1024 * 1024
ARCHIVE_EXTENSIONS = (".zip", ".7z", ".rar")
BSDTAR_EXTENSIONS = (".7z", ".rar")
SIDECAR_EXTENSIONS = frozenset(
    {
        *(".cue", ".gdi", ".m3u", ".txt", ".nfo", ".sfv", ".md5", ".sha1", ".dat", ".url"),
        *(".jpg", ".jpeg", ".png", ".pdf", ".ini", ".db", ".diz", ".lnk", ".xml"),
    }
)
JUNK_NAMES = frozenset({".DS_Store", "Thumbs.db", "desktop.ini", "@eaDir", "#recycle", "__MACOSX"})
UNSUPPORTED_EXTENSIONS = {
    ".vpk": "Vita",
    ".pkg": "PS3",
    ".rap": "PS3",
}
_DATA_SHA1 = re.compile(r"^\s*Data SHA1:\s*([0-9a-f]{40})\s*$", re.IGNORECASE | re.MULTILINE)
_SHA1 = re.compile(r"^\s*SHA1:\s*([0-9a-f]{40})\s*$", re.IGNORECASE | re.MULTILINE)
_WILDCARDS = re.compile(r"([\\*?\[\]{}])")


class InboxError(UsageError):
    """The run cannot start (bad layout, no DATs). Per-item problems are reported instead."""


class ItemFailure(Exception):
    """One item or unit cannot be placed; the message is the reason given for review."""


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


@dataclass(frozen=True)
class Tools:
    chdman: str | None = None
    bsdtar: str | None = None
    rahasher: str | None = None
    dolphin_tool: str | None = None
    nsz: str | None = None
    orbis_pkg_util: str | None = None
    vita3k: str | None = None  # Vita3K.app (a folder) or the Vita3K binary


# Where Vita3K's macOS release is installed; `inbox` uses it when `[inbox] vita3k` is unset.
VITA3K_APP = "/Applications/Vita3K.app"
VITA3K_BINARY = "Contents/MacOS/Vita3K"  # inside the app bundle
DEFAULT_VITA3K = VITA3K_APP if sys.platform == "darwin" else None


def vita3k_executable(path: str) -> str:
    """The program to run for a Vita3K path: the binary inside an app bundle (`.app`), or the
    path itself."""
    trimmed = path.rstrip("/")
    return f"{trimmed}/{VITA3K_BINARY}" if trimmed.endswith(".app") else path


def _executable(path: str) -> bool:
    return os.path.isfile(path) and os.access(path, os.X_OK)


def find_tools(
    settings: InboxSettings,
    which: Callable[[str], str | None] = shutil.which,
    vita3k_default: str | None = DEFAULT_VITA3K,
) -> Tools:
    """Configured paths must exist; unset tools are looked up on PATH (RAHasher only
    when configured). Vita3K is looked for at `vita3k_default` (the app on macOS) first."""

    def pick(key: str, configured: str | None, command: str | None) -> str | None:
        if configured:
            if not _executable(configured):
                raise InboxError(f"[inbox] {key}: {configured} is not an executable file")
            return configured
        return which(command) if command else None

    def pick_vita3k() -> str | None:
        if settings.vita3k:
            if not _executable(vita3k_executable(settings.vita3k)):
                raise InboxError(
                    f"[inbox] vita3k: {settings.vita3k} is neither Vita3K.app nor an "
                    "executable file"
                )
            return settings.vita3k
        if vita3k_default and _executable(vita3k_executable(vita3k_default)):
            return vita3k_default
        return which("vita3k") or which("Vita3K")

    return Tools(
        chdman=pick("chdman", settings.chdman, "chdman"),
        bsdtar=pick("bsdtar", settings.bsdtar, "bsdtar"),
        rahasher=pick("rahasher", settings.rahasher, None),
        dolphin_tool=pick("dolphin_tool", settings.dolphin_tool, "dolphin-tool"),
        nsz=pick("nsz", settings.nsz, "nsz"),
        orbis_pkg_util=pick("orbis_pkg_util", settings.orbis_pkg_util, "orbis-pkg-util"),
        vita3k=pick_vita3k(),
    )


@dataclass(frozen=True)
class Options:
    root: Path  # the hub root as mounted here: holds library/ and inbox/
    dats_dir: Path
    tools: Tools
    apply: bool = False
    force_lock: bool = False  # take over a stale inbox lock (see inbox_lock)
    work_dir: Path | None = None
    cache_dir: Path | None = None
    ra_key: str | None = None
    ra_fetch: ra.Fetch | None = None
    settings: InboxSettings = field(default_factory=InboxSettings)  # read by kind handlers
    now: Callable[[], float] = time.time
    today: Callable[[], date] = date.today


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
    origin: Path | None = None  # the .gcz image this was expanded from


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
        raise ItemFailure(f"{archive.name}: cannot read ({tail(listing.stderr)})")
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
        raise ItemFailure(f"{archive.name}: cannot unpack ({tail(done.stderr)})")
    for base, dirs, names in os.walk(dest):
        for name in [*dirs, *names]:
            if os.path.islink(os.path.join(base, name)):
                raise ItemFailure(f"{archive.name}: refused, it contains a symbolic link")


def tail(text: str) -> str:
    lines = text.strip().splitlines()
    return lines[-1][:200] if lines else "no output"


def next_free(work: Path, label: str) -> Path:
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
            target = next_free(work, "x")
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


def run(cmd: Sequence[str]) -> tuple[int, str]:
    proc = subprocess.run(list(cmd), capture_output=True, text=True, check=False, errors="replace")
    return proc.returncode, (proc.stdout + proc.stderr).replace("\r", "\n")


def chd_data_sha1(chdman: str, chd: Path) -> str | None:
    code, out = run([chdman, "info", "-i", str(chd)])
    if code != 0:
        return None
    m = _DATA_SHA1.search(out) or _SHA1.search(out)
    return m.group(1).lower() if m else None


def zip_members(path: Path) -> set[tuple[int, int]]:
    with zipfile.ZipFile(path) as z:
        bad = z.testzip()
        if bad:
            raise ItemFailure(f"{path.name}: damaged member {bad}")
        return {(i.file_size, i.CRC) for i in z.infolist() if not i.is_dir()}


def crc_of(path: Path) -> int:
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


@dataclass
class UnitResult:
    system: str
    entry: str
    outcome: str  # placed | already | planned | exists | review
    ra: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    reason: str = ""
    folder: str = ""  # library folder when it is not roms/<system> (Switch updates, PS4 games)
    lines: list[str] = field(default_factory=list)  # whitelist lines, when `whitelist` is not it

    @property
    def path(self) -> str:
        return f"library/{self.folder or 'roms/' + self.system}/{self.entry}"

    @property
    def whitelist(self) -> str:
        """The Syncthing line for a file in roms/; none for a folder-whitelisted extra."""
        return "" if self.folder else whitelist_line(self.system, self.entry)

    @property
    def whitelist_lines(self) -> list[str]:
        """Every line that delivers this unit: the ones it names, else the one for its file."""
        return self.lines or ([self.whitelist] if self.whitelist else [])


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


class Context:
    def __init__(self, opts: Options, rep: Reporter, library: DatLibrary, work: Path) -> None:
        self.opts = opts
        self.rep = rep
        self.library = library
        self.work = work
        self.roms = opts.root / "library" / "roms"
        self.tools = opts.tools
        self.lookup = ra.Lookup(opts.ra_key, opts.ra_fetch or ra.http_fetch)


class KindHandler(Protocol):
    """A kind of content that does not go through DAT matching and conversion (arcade
    sets, see `inbox_mame.py`). `validate` rejects a bad configuration before anything
    runs (raise `InboxError`); `claims` says whether the handler takes over an item;
    `process` identifies and places it, filling `result` like `_identify_and_place`."""

    def validate(self, opts: Options) -> None: ...

    def claims(self, ctx: Context, item: Path) -> bool: ...

    def process(self, ctx: Context, item: Path, work: Path, result: ItemResult) -> None: ...


class UnmatchedHandler(Protocol):
    """A second chance for files the DATs did not explain. `adopt` gets the staged files
    of the item and the unmatched ones; it places (or sends to review) the ones it can
    tell by their content, fills `result` and returns their item paths, which are then no
    longer reported as "no DAT match"."""

    def adopt(
        self,
        ctx: Context,
        staged: Mapping[str, Staged],
        unmatched: Sequence[FileInfo],
        result: ItemResult,
    ) -> set[str]: ...
