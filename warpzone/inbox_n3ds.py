"""Nintendo 3DS cartridge images (`.3ds`, `.cci`, `.zcci`) for `warpzone inbox`.

No DAT is needed: an image is identified by its headers, never by its name alone. `HANDLER` is a
kind handler; the decisions (`claimed_name`, `target_name`, `game_key`, `partition0_offset`,
`judge`, `judge_z3ds`) are pure functions of bytes; `detect` and `place_image` touch the disk.
See development/design/inbox.md, "3DS images".
"""

from __future__ import annotations

import struct
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO

from warpzone import inbox_console_iso, inbox_core
from warpzone.inbox_core import Context, ItemFailure, ItemResult, Options, Staged, UnitResult

SYSTEM = "n3ds"  # the ES-DE system folder inside library/roms/
CLAIMED_SUFFIXES = (".3ds", ".cci", ".zcci")
TARGET_SUFFIX = ".cci"
COMPRESSED_SUFFIX = ".zcci"  # Azahar's Z3DS-compressed cartridge image, filed as it is
UNIT = 0x200  # a media unit, the size NCSD counts in
HEADER_BYTES = 0x200
NCSD_MAGIC, NCSD_MAGIC_AT = b"NCSD", 0x100
IMAGE_SIZE_AT = 0x104  # u32 LE, in media units
PARTITION_TABLE_AT = 0x120  # eight (offset, length) pairs of u32 LE, in media units
NCCH_MAGIC, NCCH_MAGIC_AT = b"NCCH", 0x100
NCCH_FLAGS_AT = 0x18F  # the seventh of the eight flag bytes at 0x188
NO_CRYPTO = 0x04
Z3DS_MAGIC = b"Z3DS"
Z3DS_HEADER_BYTES = 0x20
Z3DS_VERSION = 1  # the only version Azahar reads

OK, ENCRYPTED, INVALID = "ok", "encrypted", "invalid"


# ------------------------------------------------------------------ pure decisions


def claimed_name(name: str) -> bool:
    """Does a file name say "3DS cartridge image"?"""
    return name.lower().endswith(CLAIMED_SUFFIXES)


def is_compressed_name(name: str) -> bool:
    return name.lower().endswith(COMPRESSED_SUFFIX)


def target_name(name: str) -> str:
    """The name in the library: the stem of a `.3ds` or `.cci` with `.cci`; a `.zcci` keeps its
    suffix (in lower case)."""
    for suffix in CLAIMED_SUFFIXES:
        if name.lower().endswith(suffix):
            keep = COMPRESSED_SUFFIX if suffix == COMPRESSED_SUFFIX else TARGET_SUFFIX
            return name[: -len(suffix)] + keep
    return name


def game_key(name: str) -> str:
    """The name of the game without its image suffix: `X.cci`, `X.3ds` and `X.zcci` are one
    game."""
    target = target_name(name)
    return target[: -len(COMPRESSED_SUFFIX if is_compressed_name(target) else TARGET_SUFFIX)]


def other_form(target: str) -> str:
    """The library name of the same game in the other form: `X.cci` <-> `X.zcci`."""
    if is_compressed_name(target):
        return target[: -len(COMPRESSED_SUFFIX)] + TARGET_SUFFIX
    return target[: -len(TARGET_SUFFIX)] + COMPRESSED_SUFFIX


@dataclass(frozen=True)
class Verdict:
    status: str  # OK | ENCRYPTED | INVALID
    reason: str = ""  # why it is not OK
    compressed: bool = False  # a Z3DS (.zcci) image, checked from its header only

    @property
    def ok(self) -> bool:
        return self.status == OK

    def describe(self) -> str:
        if not self.ok:
            return self.reason
        if self.compressed:
            return "Nintendo 3DS image (Z3DS compressed, filed as it is; encryption not checked)"
        return "Nintendo 3DS image (decrypted)"


def partition0_offset(head: bytes) -> int | None:
    """The byte offset of partition 0 from an NCSD header, None when it is not one or the
    partition is not there."""
    if len(head) < PARTITION_TABLE_AT + 8 or head[NCSD_MAGIC_AT : NCSD_MAGIC_AT + 4] != NCSD_MAGIC:
        return None
    units = struct.unpack_from("<I", head, PARTITION_TABLE_AT)[0]
    return units * UNIT if units else None


def judge(head: bytes, ncch: bytes, file_size: int) -> Verdict:
    """The verdict from the first 0x200 bytes of the file (`head`), the 0x200 bytes at the
    offset of partition 0 (`ncch`, empty when there is none) and the size of the file."""
    if head[:4] == Z3DS_MAGIC:
        return Verdict(
            INVALID,
            "a Z3DS compressed image under an uncompressed name (no NCSD magic at 0x100): "
            "drop it as .zcci",
        )
    if len(head) < HEADER_BYTES or head[NCSD_MAGIC_AT : NCSD_MAGIC_AT + 4] != NCSD_MAGIC:
        return Verdict(INVALID, "not a 3DS cartridge image (no NCSD magic at 0x100)")
    image = struct.unpack_from("<I", head, IMAGE_SIZE_AT)[0] * UNIT
    if image == 0:
        return Verdict(INVALID, "the NCSD header says the image is empty")
    if image > file_size:
        return Verdict(
            INVALID,
            f"the NCSD header says the image is {image} bytes, bigger than the file "
            f"({file_size} bytes): a truncated dump",
        )
    offset = partition0_offset(head)
    if offset is None:
        return Verdict(INVALID, "the NCSD header has no partition 0")
    if offset + UNIT > image:
        return Verdict(INVALID, "partition 0 of the NCSD header lies outside the image")
    if len(ncch) < UNIT or ncch[NCCH_MAGIC_AT : NCCH_MAGIC_AT + 4] != NCCH_MAGIC:
        return Verdict(INVALID, "partition 0 is not an NCCH (no NCCH magic at 0x100)")
    if not ncch[NCCH_FLAGS_AT] & NO_CRYPTO:
        return Verdict(
            ENCRYPTED,
            "encrypted 3DS image (NCCH NoCrypto flag not set): Azahar opens only decrypted "
            "images, so decrypt it and drop the result",
        )
    return Verdict(OK)


def judge_z3ds(head: bytes, file_size: int) -> Verdict:
    """The verdict on a `.zcci` from the first 0x20 bytes of the file and its size: a Z3DS
    header wrapping an NCSD image, whose sizes add up to the file (Azahar reads the seek table
    from the end of the file, so a cut or padded file does not open)."""
    if len(head) < Z3DS_HEADER_BYTES or head[:4] != Z3DS_MAGIC:
        return Verdict(INVALID, "not a Z3DS compressed image (no Z3DS magic at 0)", True)
    wrapped = head[4:8]
    if wrapped != NCSD_MAGIC:
        shown = wrapped.decode("ascii", "replace") if wrapped.isascii() else wrapped.hex()
        return Verdict(
            INVALID,
            f"a Z3DS file that wraps {shown!r}, not a 3DS cartridge image (NCSD)",
            True,
        )
    version = head[8]
    if version != Z3DS_VERSION:
        return Verdict(
            INVALID,
            f"unsupported Z3DS version {version} (Azahar reads version {Z3DS_VERSION})",
            True,
        )
    header_size, metadata_size, compressed, uncompressed = struct.unpack_from("<HIQQ", head, 10)
    if header_size < Z3DS_HEADER_BYTES:
        return Verdict(INVALID, f"the Z3DS header size ({header_size}) is too small", True)
    if uncompressed < HEADER_BYTES or compressed == 0:
        return Verdict(INVALID, "the Z3DS header says the image is empty", True)
    expected = header_size + metadata_size + compressed
    if expected > file_size:
        return Verdict(
            INVALID,
            f"the Z3DS header says the file is {expected} bytes, bigger than the file "
            f"({file_size} bytes): a truncated copy",
            True,
        )
    if expected < file_size:
        return Verdict(
            INVALID,
            f"the Z3DS header says the file is {expected} bytes, smaller than the file "
            f"({file_size} bytes): extra bytes after the data, which Azahar cannot open",
            True,
        )
    return Verdict(OK, compressed=True)


# ------------------------------------------------------------------ disk


def _read_at(f: BinaryIO, offset: int, length: int) -> bytes:
    f.seek(offset)
    return f.read(length)


def detect(f: BinaryIO, compressed: bool = False) -> Verdict:
    """Judge an image from its two headers (a `.zcci`, `compressed`, from its Z3DS header);
    reads at most 0x200 bytes at a time."""
    size = f.seek(0, 2)
    head = _read_at(f, 0, HEADER_BYTES)
    if compressed:
        return judge_z3ds(head, size)
    offset = partition0_offset(head)
    ncch = _read_at(f, offset, UNIT) if offset is not None else b""
    return judge(head, ncch, size)


def detect_path(path: Path) -> Verdict:
    with path.open("rb") as f:
        return detect(f, is_compressed_name(path.name))


def place_image(ctx: Context, path: Path, rel: str, verdict: Verdict) -> UnitResult:
    """File one recognised image as `<stem>.cci` (a `.zcci` as `<stem>.zcci`); never replaces a
    file. The outcome is `review` (with a reason) when it cannot be placed."""
    entry = target_name(path.name)
    res = UnitResult(SYSTEM, entry, "placed" if ctx.opts.apply else "planned")
    res.notes.append(f"detected: {verdict.describe()}")
    if entry != path.name:
        res.notes.append(f"{path.name} is filed as {entry} (same format, bytes unchanged)")
    dest = ctx.roms / SYSTEM / entry
    twin = other_form(entry)
    try:
        if (ctx.roms / SYSTEM / twin).exists():
            raise ItemFailure(
                f"{entry}: {twin} is already in the library, the same game in "
                f"{'uncompressed' if is_compressed_name(entry) else 'compressed'} form: not "
                "filed as a second copy (they cannot be compared without decompressing); "
                "keep one of the two"
            )
        if dest.exists():
            if not ctx.opts.apply:
                res.outcome = "exists"
                res.notes.append("already in the library (content is compared with --apply)")
            elif inbox_core.destination_verdict(True, inbox_console_iso.same_file(dest, path)) == (
                "already"
            ):
                res.outcome = "already"
            else:
                raise ItemFailure(
                    f"{entry}: a different file with that name is already in the library"
                )
        elif ctx.opts.apply:
            inbox_console_iso.place_file(path, dest)
    except ItemFailure as exc:
        res.outcome, res.reason = "review", str(exc)
    except OSError as exc:
        res.outcome, res.reason = "review", f"{entry}: {exc.strerror or exc}"
    return res


# ------------------------------------------------------------------ the inbox handler


class N3dsHandler:
    """Items holding a `.3ds`, `.cci` or `.zcci` (the item is taken over whole)."""

    def validate(self, opts: Options) -> None:
        return None

    def claims(self, ctx: Context, item: Path) -> bool:
        if item.is_symlink():
            return False
        return any(claimed_name(n) for n in inbox_console_iso.names_in(ctx, item))

    def process(self, ctx: Context, item: Path, work: Path, result: ItemResult) -> None:
        staged, notes, failures = inbox_core.stage_item(item, work, ctx.tools)
        result.notes.extend(notes)
        if failures:
            result.reasons.extend(failures)
            return
        others: list[str] = []
        targets: dict[str, str] = {}  # game key -> the file that is filed for it
        for s in staged:
            if claimed_name(s.path.name):
                self._one(ctx, s, targets, result)
            elif s.path.suffix.lower() not in inbox_core.SIDECAR_EXTENSIONS:
                others.append(s.rel)
        if others:
            shown = ", ".join(others[:5]) + (", ..." if len(others) > 5 else "")
            result.reasons.append(f"not a 3DS image, drop it as its own item: {shown}")

    def _one(self, ctx: Context, s: Staged, targets: dict[str, str], result: ItemResult) -> None:
        try:
            verdict = detect_path(s.path)
        except OSError as exc:
            result.reasons.append(f"{s.rel}: {exc.strerror or exc}")
            return
        if not verdict.ok:
            result.reasons.append(f"{s.rel}: {verdict.reason}")
            return
        key = game_key(s.path.name)
        if key in targets:
            result.reasons.append(
                f"{s.rel}: is the same game as {targets[key]} (a .3ds and a .cci are the same "
                "format, a .zcci is the same image compressed): drop them as separate items"
            )
            return
        targets[key] = s.rel
        unit = place_image(ctx, s.path, s.rel, verdict)
        result.units.append(unit)
        if unit.reason:
            result.reasons.append(unit.reason)


HANDLER = N3dsHandler()
