"""Console disc images that no DAT can match, for `warpzone inbox`.

Three kinds of image have no Redump / No-Intro entry, so they are identified by their
signature instead of by hash:

- PS3 decrypted discs (`*.dec.iso`): ISO9660 with `PS3_DISC.SFB` and `PS3_GAME/PARAM.SFO`,
  whose TITLE_ID and TITLE name the game. Filed in `roms/ps3/`.
- Original Xbox XISO images (`*.xiso.iso`, `*.xiso`): an XDVDFS volume at offset 0 whose
  root holds `default.xbe`. Filed in `roms/xbox/`.
- Xbox 360 disc images (plain `.iso`): an XDVDFS game partition at 0xFDA0000 (XGD2) or
  0x2080000 (XGD3), or an extracted XISO at offset 0, whose root holds `default.xex`.
  Filed in `roms/xbox360/roms/` (the folder EmuDeck's ES-DE system for the 360 reads).

A full Redump-style Xbox image (game partition at 0x18300000) is recognised but never
placed: xemu cannot boot it, so it goes to review with the instruction to convert it with
extract-xiso.

How the module hooks into `inbox.py`: `HANDLER` is both a kind handler (it claims items
whose file names end in `.dec.iso`, `.xiso.iso` or `.xiso`, in a folder or an archive) and
an `UnmatchedHandler` (a plain `.iso` that the DATs could not explain is sniffed, and if it
is one of these formats it is placed instead of going to review). PS2, PSP and GameCube
`.iso` files are therefore untouched: they are matched by the DATs first, and only an
unmatched `.iso` is ever read for a signature.

A PS3 `.iso` needs ES-DE's alternative emulator `RPCS3 ISO (Standalone)` (the system
default is a shortcut launcher), which is written per game into the hub's ES-DE gamelist
with the helpers of `inbox_mame`.

The decisions (`claimed_name`, `parse_sfo`, `iso_records`, `xdvdfs_entries`, `detect`,
`destination`) are pure functions of bytes or of a binary file object that is only read in
small bounded pieces; the rest touches the disk. See docs/design.md.
"""

from __future__ import annotations

import os
import re
import struct
import weakref
import zipfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import BinaryIO, TypeVar

from warpzone import inbox, inbox_mame
from warpzone.dat import FileInfo
from warpzone.inbox import Context, ItemFailure, ItemResult, Options, Staged, UnitResult

PS3, XBOX, XBOX360, XBOX_REDUMP = "ps3", "xbox", "xbox360", "xbox-redump"
PLATFORMS = {
    PS3: "PS3 decrypted disc image",
    XBOX: "Xbox (XISO)",
    XBOX360: "Xbox 360 disc image",
    XBOX_REDUMP: "Xbox full disc image (Redump layout)",
}
# Where each kind goes inside library/roms/.
FOLDERS = {PS3: "ps3", XBOX: "xbox", XBOX360: "xbox360/roms"}
CLAIMED_SUFFIXES = (".dec.iso", ".xiso.iso", ".xiso")
SNIFFED_SUFFIXES = (".iso", ".xiso")
EMU_RPCS3_ISO = "RPCS3 ISO (Standalone)"  # the ES-DE name, as written in <altemulator>
GAMELIST_REL = ("frontend", "es-de", "gamelists", "ps3", "gamelist.xml")
CONVERT_HINT = (
    "this is a full Xbox disc image (game partition at 0x18300000) and xemu cannot boot it: "
    "convert it to XISO with extract-xiso (for example `extract-xiso -r <image>`) and drop "
    "the result"
)

SECTOR = 2048
ISO_PVD_OFFSET = 16 * SECTOR
XDVDFS_MAGIC = b"MICROSOFT*XBOX*MEDIA"
XDVDFS_DESCRIPTOR = 0x10000  # the volume descriptor, from the start of the game partition
# Game partition offsets: an extracted XISO, XGD2, XGD3 and the Redump layout of Xbox.
XDVDFS_PARTITIONS = (0, 0xFDA0000, 0x2080000, 0x18300000)
REDUMP_PARTITION = 0x18300000
MAX_DIR_BYTES = 1 << 20  # a directory bigger than this is not a game disc
MAX_DIR_ENTRIES = 1 << 14
MAX_SFO_BYTES = 1 << 16
_VERSION_SUFFIX = re.compile(r";\d+$")


# ------------------------------------------------------------------ pure decisions


def claimed_name(name: str) -> bool:
    """Does a file name say "console image" strongly enough to take the item over?"""
    return name.lower().endswith(CLAIMED_SUFFIXES)


def sniffable_name(name: str) -> bool:
    return name.lower().endswith(SNIFFED_SUFFIXES)


def destination(kind: str) -> str | None:
    """The folder inside `library/roms/` for a kind, None for one that is never placed."""
    return FOLDERS.get(kind)


def parse_sfo(data: bytes) -> dict[str, str | int]:
    """The key/value table of a PARAM.SFO; empty when `data` is not one."""
    if len(data) < 20 or data[:4] != b"\x00PSF":
        return {}
    key_start, data_start, count = struct.unpack_from("<III", data, 8)
    if count > 512 or 20 + 16 * count > len(data):
        return {}
    out: dict[str, str | int] = {}
    for n in range(count):
        key_off, fmt, length, _max, data_off = struct.unpack_from("<HHIII", data, 20 + 16 * n)
        key_at = key_start + key_off
        end = data.find(b"\x00", key_at)
        if key_at >= len(data) or end < 0:
            continue
        key = data[key_at:end].decode("ascii", "replace")
        raw = data[data_start + data_off : data_start + data_off + length]
        if fmt in (0x0004, 0x0204):
            out[key] = " ".join(raw.split(b"\x00")[0].decode("utf-8", "replace").split())
        elif fmt == 0x0404 and len(raw) >= 4:
            out[key] = struct.unpack("<I", raw[:4])[0]
    return out


@dataclass(frozen=True)
class IsoRecord:
    name: str  # without the ";1" version suffix
    extent: int  # first logical block
    size: int
    is_dir: bool


def iso_records(data: bytes, block: int = SECTOR) -> list[IsoRecord]:
    """The entries of an ISO9660 directory extent (without `.` and `..`)."""
    records: list[IsoRecord] = []
    pos = 0
    while pos < len(data) and len(records) < MAX_DIR_ENTRIES:
        length = data[pos]
        if length == 0:  # records never cross a block: the rest of it is padding
            pos = (pos // block + 1) * block
            continue
        if length < 34 or pos + length > len(data):
            break
        extent, size = (
            struct.unpack_from("<I", data, pos + 2)[0],
            struct.unpack_from("<I", data, pos + 10)[0],
        )
        flags, name_len = data[pos + 25], data[pos + 32]
        raw = data[pos + 33 : pos + 33 + name_len]
        pos += length
        if raw in (b"\x00", b"\x01"):
            continue
        name = _VERSION_SUFFIX.sub("", raw.decode("latin-1")).rstrip(".")
        records.append(IsoRecord(name, extent, size, bool(flags & 2)))
    return records


@dataclass(frozen=True)
class XdvdfsEntry:
    name: str
    sector: int
    size: int
    is_dir: bool


def xdvdfs_entries(data: bytes) -> list[XdvdfsEntry]:
    """The entries of an XDVDFS directory, walking its binary tree. Each node is
    `u16 left, u16 right` (offsets in 4-byte units), `u32 sector, u32 size`, `u8 attributes,
    u8 name length`, the name. Offset 0 means no child; an all-0xFF node is an empty directory."""
    entries: list[XdvdfsEntry] = []
    seen: set[int] = set()
    todo = [0]
    while todo and len(entries) < MAX_DIR_ENTRIES:
        off = todo.pop()
        if off in seen or off * 4 + 14 > len(data):
            continue
        seen.add(off)
        at = off * 4
        left, right, sector, size, attrs, name_len = struct.unpack_from("<HHIIBB", data, at)
        if left == 0xFFFF and right == 0xFFFF and sector == 0xFFFFFFFF:
            continue
        raw = data[at + 14 : at + 14 + name_len]
        if name_len == 0 or len(raw) < name_len:
            continue
        entries.append(XdvdfsEntry(raw.decode("latin-1"), sector, size, bool(attrs & 0x10)))
        todo.extend(child for child in (left, right) if child)
    return entries


@dataclass(frozen=True)
class Detected:
    kind: str
    title_id: str = ""
    title: str = ""

    @property
    def platform(self) -> str:
        return PLATFORMS[self.kind]

    def describe(self) -> str:
        if self.kind == PS3:
            return f"{self.platform}: {self.title_id} {self.title}".rstrip()
        return self.platform


def _read_at(f: BinaryIO, offset: int, length: int) -> bytes:
    f.seek(offset)
    return f.read(length)


_Entry = TypeVar("_Entry", IsoRecord, XdvdfsEntry)


def _find(entries: Sequence[_Entry], name: str) -> _Entry | None:
    return next((e for e in entries if e.name.lower() == name.lower()), None)


def _xdvdfs_root(f: BinaryIO, base: int) -> list[XdvdfsEntry] | None:
    head = _read_at(f, base + XDVDFS_DESCRIPTOR, 0x20)
    if len(head) < 0x1C or head[:20] != XDVDFS_MAGIC:
        return None
    sector, size = struct.unpack_from("<II", head, 0x14)
    if not 0 < size <= MAX_DIR_BYTES:
        return None
    data = _read_at(f, base + sector * SECTOR, size)
    return xdvdfs_entries(data) if len(data) == size else None


def _detect_xdvdfs(f: BinaryIO) -> Detected | None:
    for base in XDVDFS_PARTITIONS:
        root = _xdvdfs_root(f, base)
        if root is None:
            continue
        names = {e.name.lower() for e in root if not e.is_dir}
        if base == REDUMP_PARTITION:
            if "default.xbe" in names:
                return Detected(XBOX_REDUMP)
        elif base == 0 and "default.xbe" in names:
            return Detected(XBOX)
        elif "default.xex" in names:
            return Detected(XBOX360)
    return None


def _iso_dir(f: BinaryIO, block: int, record: IsoRecord) -> list[IsoRecord] | None:
    if not 0 < record.size <= MAX_DIR_BYTES:
        return None
    data = _read_at(f, record.extent * block, record.size)
    return iso_records(data, block) if len(data) == record.size else None


def _detect_ps3(f: BinaryIO) -> Detected | None:
    pvd = _read_at(f, ISO_PVD_OFFSET, SECTOR)
    if len(pvd) < SECTOR or pvd[0] != 1 or pvd[1:6] != b"CD001":
        return None
    block = struct.unpack_from("<H", pvd, 128)[0]
    if block < SECTOR or block & (block - 1):
        block = SECTOR
    root_rec = pvd[156:190]
    root = IsoRecord(
        "",
        struct.unpack_from("<I", root_rec, 2)[0],
        struct.unpack_from("<I", root_rec, 10)[0],
        True,
    )
    top = _iso_dir(f, block, root)
    if top is None:
        return None
    sfb = _find(top, "PS3_DISC.SFB")
    game = _find(top, "PS3_GAME")
    if sfb is None or game is None or not game.is_dir:
        return None
    sfo = _find(_iso_dir(f, block, game) or [], "PARAM.SFO")
    if sfo is None or not 0 < sfo.size <= MAX_SFO_BYTES:
        return None
    values = parse_sfo(_read_at(f, sfo.extent * block, sfo.size))
    title_id, title = values.get("TITLE_ID"), values.get("TITLE")
    if not isinstance(title_id, str) or not title_id:
        return None
    return Detected(PS3, title_id, title if isinstance(title, str) else "")


def detect(f: BinaryIO) -> Detected | None:
    """What a disc image is, from its signatures; None when it is none of the supported
    kinds. Reads a few kilobytes at fixed offsets and never the whole image."""
    return _detect_xdvdfs(f) or _detect_ps3(f)


def detect_path(path: Path) -> Detected | None:
    with path.open("rb") as f:
        return detect(f)


# ------------------------------------------------------------------ disk


@dataclass
class RunState:
    backed_up: set[Path] = field(default_factory=set)


_STATE: weakref.WeakKeyDictionary[Context, RunState] = weakref.WeakKeyDictionary()


def _state(ctx: Context) -> RunState:
    if ctx not in _STATE:
        _STATE[ctx] = RunState()
    return _STATE[ctx]


def gamelist_path(opts: Options) -> Path:
    configured = opts.settings.gamelist_ps3
    return Path(configured) if configured else opts.root.joinpath(*GAMELIST_REL)


def _gamelist(ctx: Context, entry: str, res: UnitResult) -> None:
    """Record RPCS3's ISO launcher as the emulator of one PS3 game (only with --apply)."""
    path = gamelist_path(ctx.opts)
    text = inbox_mame.read_gamelist(path)
    new, action = inbox_mame.update_gamelist_entry(
        inbox_mame.EMPTY_GAMELIST if text is None else text, entry, EMU_RPCS3_ISO
    )
    try:
        shown = path.relative_to(ctx.opts.root).as_posix()
    except ValueError:
        shown = str(path)
    if action == "unchanged":
        res.notes.append(f"gamelist {shown}: already sets {EMU_RPCS3_ISO}")
        return
    verb = {"added": "add", "changed": "change"}[action]
    if not ctx.opts.apply:
        res.notes.append(f"gamelist {shown}: would {verb} the emulator of {entry}: {EMU_RPCS3_ISO}")
        return
    res.notes.extend(inbox_mame.save_gamelist(ctx, path, new, "ps3", shown, _state(ctx).backed_up))
    res.notes.append(f"gamelist {shown}: {action} the emulator of {entry}: {EMU_RPCS3_ISO}")


def _same_file(a: Path, b: Path) -> bool:
    try:
        return a.stat().st_size == b.stat().st_size and inbox.file_sha1(a) == inbox.file_sha1(b)
    except OSError:
        return False


def place_image(ctx: Context, path: Path, rel: str, found: Detected) -> UnitResult:
    """File one recognised image under its own name; never replaces a file. The outcome is
    `review` (with a reason) when it cannot be placed."""
    folder = destination(found.kind)
    if folder is None:  # a full Xbox disc image
        return UnitResult("xbox", path.name, "review", reason=f"{rel}: {CONVERT_HINT}")
    entry = path.name
    res = UnitResult(folder, entry, "planned" if not ctx.opts.apply else "placed")
    res.notes.append(f"detected: {found.describe()}")
    dest = ctx.roms.joinpath(*folder.split("/")) / entry
    try:
        if dest.exists():
            if not ctx.opts.apply:
                res.outcome = "exists"
                res.notes.append("already in the library (content is compared with --apply)")
            elif inbox.destination_verdict(True, _same_file(dest, path)) == "already":
                res.outcome = "already"
            else:
                raise ItemFailure(
                    f"{entry}: a different file with that name is already in the library"
                )
        elif ctx.opts.apply:
            _place(path, dest)
        if found.kind == PS3:
            try:
                _gamelist(ctx, entry, res)
            except inbox_mame.GamelistError as exc:  # placed; a rerun retries the gamelist
                res.reason = f"{entry} is in the library but the gamelist was not updated: {exc}"
    except ItemFailure as exc:
        res.outcome, res.reason = "review", str(exc)
    except OSError as exc:
        res.outcome, res.reason = "review", f"{entry}: {exc.strerror or exc}"
    return res


def _place(src: Path, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    part = dest.with_name(dest.name + ".part")
    part.unlink(missing_ok=True)
    try:
        inbox.copy_verified(src, part)
        if dest.exists():
            raise ItemFailure(f"{dest.name}: appeared in the library while copying")
        os.rename(part, dest)
    finally:
        part.unlink(missing_ok=True)


def _file_one(ctx: Context, path: Path, rel: str, found: Detected, result: ItemResult) -> None:
    unit = place_image(ctx, path, rel, found)
    result.units.append(unit)
    if unit.reason:
        result.reasons.append(unit.reason)


# ------------------------------------------------------------------ the inbox handler


def _names_in(ctx: Context, item: Path) -> list[str]:
    """File names inside a folder or an archive (zip, or 7z / rar with bsdtar)."""
    if item.is_dir():
        names: list[str] = []
        for _, dirs, files in os.walk(item):
            dirs[:] = [d for d in dirs if not inbox.skip_name(d)]
            names.extend(n for n in files if not inbox.skip_name(n))
        return names
    if item.suffix.lower() not in inbox.ARCHIVE_EXTENSIONS:
        return [item.name]
    try:
        if item.suffix.lower() == ".zip":
            with zipfile.ZipFile(item) as z:
                return z.namelist()
        if ctx.tools.bsdtar:
            code, text = inbox._run([ctx.tools.bsdtar, "-tf", str(item)])
            return text.splitlines() if code == 0 else []
    except (zipfile.BadZipFile, OSError):
        pass
    return []


class ConsoleIsoHandler:
    """Items holding a `.dec.iso`, `.xiso.iso` or `.xiso` (the item is taken over whole), and
    plain `.iso` files the DATs could not match (see `adopt`)."""

    def validate(self, opts: Options) -> None:
        return None

    def claims(self, ctx: Context, item: Path) -> bool:
        if item.is_symlink():
            return False
        return any(claimed_name(n) for n in _names_in(ctx, item))

    def process(self, ctx: Context, item: Path, work: Path, result: ItemResult) -> None:
        staged, notes, failures = inbox.stage_item(item, work, ctx.tools)
        result.notes.extend(notes)
        if failures:
            result.reasons.extend(failures)
            return
        others: list[str] = []
        for s in staged:
            if sniffable_name(s.path.name):
                self._one(ctx, s, result)
            elif s.path.suffix.lower() not in inbox.SIDECAR_EXTENSIONS:
                others.append(s.rel)
        if others:
            shown = ", ".join(others[:5]) + (", ..." if len(others) > 5 else "")
            result.reasons.append(f"not a console disc image, drop it as its own item: {shown}")

    def _one(self, ctx: Context, s: Staged, result: ItemResult) -> None:
        try:
            found = detect_path(s.path)
        except OSError as exc:
            result.reasons.append(f"{s.rel}: {exc.strerror or exc}")
            return
        if found is None:
            result.reasons.append(
                f"{s.rel}: not recognised as a PS3 decrypted disc (PS3_DISC.SFB and "
                "PS3_GAME/PARAM.SFO), an Xbox XISO or an Xbox 360 disc image"
            )
            return
        _file_one(ctx, s.path, s.rel, found, result)

    def adopt(
        self,
        ctx: Context,
        staged: Mapping[str, Staged],
        unmatched: Sequence[FileInfo],
        result: ItemResult,
    ) -> set[str]:
        """Sniff the `.iso` files no DAT explained; place the ones that are PS3 or Xbox
        images and send a full Xbox image to review. Anything else stays unmatched."""
        taken: set[str] = set()
        for info in unmatched:
            s = staged.get(info.path)
            if s is None or not sniffable_name(s.path.name):
                continue
            try:
                found = detect_path(s.path)
            except OSError:
                continue
            if found is not None:
                taken.add(info.path)
                _file_one(ctx, s.path, s.rel, found, result)
        return taken


HANDLER = ConsoleIsoHandler()
