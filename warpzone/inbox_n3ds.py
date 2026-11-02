"""Nintendo 3DS cartridge images for `warpzone inbox`.

No DAT is needed: a `.3ds` or `.cci` file (the two names of one format, a CTR cartridge
image) is identified by the headers at the start of the file, never by its name alone:

- the NCSD container: the magic `NCSD` at 0x100 and, at 0x104, the image size (u32,
  little-endian, in media units of 0x200 bytes), which must not be bigger than the file;
- partition 0 (offset in media units at 0x120): an NCCH header (the magic `NCCH` at +0x100)
  whose flags byte (+0x18F) has the NoCrypto bit (0x04) set.

Azahar opens only decrypted images, so an encrypted one goes to review with that reason, and so
does a file whose header is not valid. Only the two headers (0x200 bytes each) are read.

A recognised image is filed in `roms/n3ds/<name>.cci`: a `.3ds` is renamed on the way in (the
bytes do not change), because both extensions are the same format. Nothing is ever replaced: the
same content already in the library is reported, different content under that name goes to
review.

How the module hooks into `inbox.py`: `HANDLER` is a kind handler that claims an item whose file
names end in `.3ds` or `.cci`, loose, in a folder or in an archive. The decisions (`claimed_name`,
`target_name`, `partition0_offset`, `judge`) are pure functions of bytes; `detect` and
`place_image` touch the disk. See docs/design.md.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO

from warpzone import inbox, inbox_console_iso
from warpzone.inbox import Context, ItemFailure, ItemResult, Options, Staged, UnitResult

SYSTEM = "n3ds"  # the ES-DE system folder inside library/roms/
CLAIMED_SUFFIXES = (".3ds", ".cci")
TARGET_SUFFIX = ".cci"
UNIT = 0x200  # a media unit, the size NCSD counts in
HEADER_BYTES = 0x200
NCSD_MAGIC, NCSD_MAGIC_AT = b"NCSD", 0x100
IMAGE_SIZE_AT = 0x104  # u32 LE, in media units
PARTITION_TABLE_AT = 0x120  # eight (offset, length) pairs of u32 LE, in media units
NCCH_MAGIC, NCCH_MAGIC_AT = b"NCCH", 0x100
NCCH_FLAGS_AT = 0x18F  # the seventh of the eight flag bytes at 0x188
NO_CRYPTO = 0x04

OK, ENCRYPTED, INVALID = "ok", "encrypted", "invalid"


# ------------------------------------------------------------------ pure decisions


def claimed_name(name: str) -> bool:
    """Does a file name say "3DS cartridge image"?"""
    return name.lower().endswith(CLAIMED_SUFFIXES)


def target_name(name: str) -> str:
    """The name in the library: the stem of a `.3ds` or `.cci` with `.cci`."""
    for suffix in CLAIMED_SUFFIXES:
        if name.lower().endswith(suffix):
            return name[: -len(suffix)] + TARGET_SUFFIX
    return name


@dataclass(frozen=True)
class Verdict:
    status: str  # OK | ENCRYPTED | INVALID
    reason: str = ""  # why it is not OK

    @property
    def ok(self) -> bool:
        return self.status == OK

    def describe(self) -> str:
        return "Nintendo 3DS image (decrypted)" if self.ok else self.reason


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


# ------------------------------------------------------------------ disk


def _read_at(f: BinaryIO, offset: int, length: int) -> bytes:
    f.seek(offset)
    return f.read(length)


def detect(f: BinaryIO) -> Verdict:
    """Judge an image from its two headers; reads at most 0x200 bytes at a time."""
    size = f.seek(0, 2)
    head = _read_at(f, 0, HEADER_BYTES)
    offset = partition0_offset(head)
    ncch = _read_at(f, offset, UNIT) if offset is not None else b""
    return judge(head, ncch, size)


def detect_path(path: Path) -> Verdict:
    with path.open("rb") as f:
        return detect(f)


def place_image(ctx: Context, path: Path, rel: str, verdict: Verdict) -> UnitResult:
    """File one recognised image as `<stem>.cci`; never replaces a file. The outcome is
    `review` (with a reason) when it cannot be placed."""
    entry = target_name(path.name)
    res = UnitResult(SYSTEM, entry, "placed" if ctx.opts.apply else "planned")
    res.notes.append(f"detected: {verdict.describe()}")
    if entry != path.name:
        res.notes.append(f"{path.name} is filed as {entry} (same format, bytes unchanged)")
    dest = ctx.roms / SYSTEM / entry
    try:
        if dest.exists():
            if not ctx.opts.apply:
                res.outcome = "exists"
                res.notes.append("already in the library (content is compared with --apply)")
            elif inbox.destination_verdict(True, inbox_console_iso.same_file(dest, path)) == (
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
    """Items holding a `.3ds` or `.cci` (the item is taken over whole)."""

    def validate(self, opts: Options) -> None:
        return None

    def claims(self, ctx: Context, item: Path) -> bool:
        if item.is_symlink():
            return False
        return any(claimed_name(n) for n in inbox_console_iso.names_in(ctx, item))

    def process(self, ctx: Context, item: Path, work: Path, result: ItemResult) -> None:
        staged, notes, failures = inbox.stage_item(item, work, ctx.tools)
        result.notes.extend(notes)
        if failures:
            result.reasons.extend(failures)
            return
        others: list[str] = []
        targets: dict[str, str] = {}  # target name -> the file that is filed under it
        for s in staged:
            if claimed_name(s.path.name):
                self._one(ctx, s, targets, result)
            elif s.path.suffix.lower() not in inbox.SIDECAR_EXTENSIONS:
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
        target = target_name(s.path.name)
        if target in targets:
            result.reasons.append(
                f"{s.rel}: would be filed as {target} like {targets[target]} "
                "(a .3ds and a .cci are the same format): drop them as separate items"
            )
            return
        targets[target] = s.rel
        unit = place_image(ctx, s.path, s.rel, verdict)
        result.units.append(unit)
        if unit.reason:
            result.reasons.append(unit.reason)


HANDLER = N3dsHandler()
