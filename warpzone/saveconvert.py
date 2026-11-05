"""Save conversion between an emulator's own save files and the hub's raw ones. Pure: no I/O.

The hub keeps cartridge saves as raw dumps (`<game>.sav`). An emulator that stores them in
another shape needs a converter; the shape is the profile's `format`. The only one so far is
`desmume-dsv`, the `.dsv` of DraStic: the raw dump plus a 122-byte DeSmuME footer. `decide`
says what to do with one game from the two sides and the hash of the last reconciliation.
The device layer (`saveconvert_device`) reads and writes the files. See
development/design/save-conversion.md.
"""

from __future__ import annotations

import enum
import hashlib
import struct
from dataclasses import dataclass

FORMATS = ("desmume-dsv",)
RAW_SUFFIX = ".sav"
DSV_SUFFIX = ".dsv"
EMU_SUFFIXES = {"desmume-dsv": DSV_SUFFIX}

FOOTER_TEXT = b"|<--Snip above here to create a raw sav by excluding this DeSmuME savedata footer:"
COOKIE = b"|-DESMUME SAVE-|"
# text, five u32 (size, padSize, type, addr_size, mem_size), the u32 version and the cookie
_NUMBERS = struct.Struct("<5II")
FOOTER_LEN = len(FOOTER_TEXT) + _NUMBERS.size + len(COOKIE)
MIN_RAW = 512
MAX_RAW = 8 * 1024 * 1024


class SaveFormatError(ValueError):
    """A save file is not in the shape it claims; it is never converted."""


def valid_size(length: int) -> bool:
    """Whether `length` can be a cartridge save: a power of two from 512 B to 8 MiB."""
    return MIN_RAW <= length <= MAX_RAW and length & (length - 1) == 0


@dataclass(frozen=True)
class Footer:
    """The numbers of a DeSmuME footer, and the footer's bytes as they were."""

    size: int
    pad_size: int
    type: int
    addr_size: int
    mem_size: int
    version: int
    raw: bytes


def parse_footer(footer: bytes) -> Footer:
    """The footer in `footer` (exactly `FOOTER_LEN` bytes); `SaveFormatError` if it is not one."""
    if len(footer) != FOOTER_LEN:
        raise SaveFormatError(f"a footer is {FOOTER_LEN} bytes, not {len(footer)}")
    if not footer.startswith(FOOTER_TEXT):
        raise SaveFormatError("the footer does not start with the DeSmuME text")
    if not footer.endswith(COOKIE):
        raise SaveFormatError("the footer does not end with the DeSmuME cookie")
    numbers = footer[len(FOOTER_TEXT) : len(FOOTER_TEXT) + _NUMBERS.size]
    size, pad_size, kind, addr_size, mem_size, version = _NUMBERS.unpack(numbers)
    if version != 0:
        raise SaveFormatError(f"footer version {version} is not known (expected 0)")
    return Footer(size, pad_size, kind, addr_size, mem_size, version, footer)


@dataclass(frozen=True)
class Dsv:
    """A `.dsv` taken apart: the raw save and the footer it had (None: it had none)."""

    raw: bytes
    footer: Footer | None


def split_dsv(data: bytes) -> Dsv:
    """The raw save and the footer of a `.dsv`; `SaveFormatError` when it is malformed.

    A file that ends with the cookie must be a valid footer after a valid raw size. A file
    that does not is accepted only as a raw dump of a valid size (what this tool writes when
    it has no footer to keep, and what DraStic loads).
    """
    if data.endswith(COOKIE):
        if len(data) < FOOTER_LEN:
            raise SaveFormatError(f"{len(data)} bytes is shorter than a footer")
        raw, footer = data[:-FOOTER_LEN], parse_footer(data[-FOOTER_LEN:])
        if not valid_size(len(raw)):
            raise SaveFormatError(f"{len(raw)} bytes before the footer is not a save size")
        return Dsv(raw, footer)
    if not valid_size(len(data)):
        raise SaveFormatError(f"{len(data)} bytes is neither a save size nor a footer's end")
    return Dsv(data, None)


def check_raw(data: bytes) -> None:
    """`SaveFormatError` unless `data` can be a raw cartridge save."""
    if not valid_size(len(data)):
        raise SaveFormatError(f"{len(data)} bytes is not a save size")


def build_dsv(raw: bytes, previous: Footer | None) -> bytes:
    """The `.dsv` content for `raw`: with the footer the file had if it describes this size.

    A footer whose `size` is another length belongs to another save, so it is dropped and the
    raw dump is written alone, which DraStic loads as well.
    """
    check_raw(raw)
    if previous is not None and previous.size == len(raw):
        return raw + previous.raw
    return raw


def digest(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def blank(raw: bytes) -> bool:
    """Whether a save is erased flash (every byte 0xFF): nothing was ever saved in it."""
    return raw.count(0xFF) == len(raw)


# ------------------------------------------------------------------ decisions


class Action(enum.Enum):
    NOTHING = "nothing"  # the sides agree (or both are missing)
    TO_EMU = "to-emu"  # write the raw side into the emulator's file
    TO_RAW = "to-raw"  # write the emulator's save into the raw file
    SKIP = "skip"  # leave the game alone and say why (`Decision.reason`)


@dataclass(frozen=True)
class Side:
    """One side of a game as the device layer read it. `problem` set: the side is unusable."""

    digest: str = ""
    mtime_ns: int = 0
    size: int = 0
    blank: bool = False
    problem: str | None = None


@dataclass(frozen=True)
class Decision:
    action: Action
    reason: str = ""
    # Both sides changed since the last reconciliation: the newest won and the other side's
    # content is backed up before it is replaced.
    conflict: bool = False
    # Hash of the raw data both sides hold once the action is done (the new base).
    base: str | None = None


def decide(raw: Side | None, emu: Side | None, base: str | None) -> Decision:
    """What to do with one game. `raw` and `emu` are None when that file does not exist.

    `base` is the hash of the raw data at the last reconciliation. Only one side changed:
    copy it over the other. Neither: nothing. Both: the most recently modified wins (a tie
    goes to the raw side, the hub's copy). A side that exists alone is copied to the other
    side; a missing file is never taken as a deletion. Sides of different sizes, and a side
    with a `problem`, are skipped untouched. With no base, an erased side loses to one that
    holds data, whatever the dates say.
    """
    for label, side in (("hub file", raw), ("emulator file", emu)):
        if side is not None and side.problem:
            return Decision(Action.SKIP, f"{label}: {side.problem}")
    if raw is None and emu is None:
        return Decision(Action.NOTHING)
    if emu is None:
        assert raw is not None
        return Decision(Action.TO_EMU, "new on the hub side", base=raw.digest)
    if raw is None:
        return Decision(Action.TO_RAW, "new on the emulator side", base=emu.digest)
    if raw.digest == emu.digest:
        return Decision(Action.NOTHING, base=raw.digest)
    if raw.size != emu.size:
        return Decision(
            Action.SKIP,
            f"the sides differ in size ({raw.size} and {emu.size} bytes), so neither was replaced",
        )
    if base == raw.digest:
        return Decision(Action.TO_RAW, "changed in the emulator", base=emu.digest)
    if base == emu.digest:
        return Decision(Action.TO_EMU, "changed on the hub side", base=raw.digest)
    if base is None and raw.blank != emu.blank:
        if emu.blank:
            return Decision(Action.TO_EMU, "the emulator's file is erased", base=raw.digest)
        return Decision(Action.TO_RAW, "the hub's file is erased", base=emu.digest)
    if emu.mtime_ns > raw.mtime_ns:
        return Decision(Action.TO_RAW, "both changed; the emulator's is newer", True, emu.digest)
    return Decision(Action.TO_EMU, "both changed; the hub's is newer", True, raw.digest)
