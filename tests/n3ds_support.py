"""Tiny synthetic 3DS cartridge images for the inbox tests: an NCSD container whose partition 0
is an NCCH header, with a few zeroed units of body. No real game data."""

from __future__ import annotations

import struct

UNIT = 0x200
PART0 = 0x20  # partition 0 starts at 0x4000, in media units
NO_CRYPTO = 0x04


def ncsd(
    *,
    encrypted: bool = False,
    flags: int | None = None,
    magic: bytes = b"NCSD",
    ncch_magic: bytes = b"NCCH",
    size_units: int | None = None,
    part0: int = PART0,
    body_units: int = 8,
    padding: int = 0,
) -> bytes:
    """An image whose NCSD header is at 0x100 and whose partition 0 holds an NCCH header.

    `flags` is the NCCH flags byte at 0x18F (NoCrypto is bit 2); by default NoCrypto is set
    unless `encrypted`. `size_units` overrides the image size of the header (media units of
    0x200); by default it is the real size. `padding` appends bytes the header does not count."""
    if flags is None:
        flags = 0 if encrypted else NO_CRYPTO
    ncch = bytearray(UNIT)
    ncch[0x100:0x104] = ncch_magic
    ncch[0x18F] = flags
    head = bytearray(UNIT)
    head[0x100:0x104] = magic
    body = bytes(body_units * UNIT)
    total_units = part0 + 1 + body_units
    struct.pack_into("<I", head, 0x104, total_units if size_units is None else size_units)
    struct.pack_into("<II", head, 0x120, part0, 1 + body_units)  # partition 0: offset, length
    image = bytearray(head)
    image += bytes(max(part0 * UNIT - len(image), 0))
    image += ncch + body
    return bytes(image) + bytes(padding)


def z3ds(
    *,
    magic: bytes = b"Z3DS",
    wrapped: bytes = b"NCSD",
    version: int = 1,
    header_size: int = 0x20,
    metadata_size: int = 0x80,
    compressed: int | None = None,
    uncompressed: int = 0x4000,
    payload_bytes: int = 600,
    padding: int = 0,
) -> bytes:
    """A `.zcci` shaped like Azahar's: the 0x20-byte Z3DS header, the metadata and a payload.
    The payload is not real zstd (the inbox never decompresses); `compressed` overrides the
    size the header declares (by default the real one) and `padding` appends uncounted bytes."""
    declared = payload_bytes if compressed is None else compressed
    head = struct.pack(
        "<4s4sBBHIQQ",
        magic,
        wrapped,
        version,
        0,
        header_size,
        metadata_size,
        declared,
        uncompressed,
    )
    data = head.ljust(header_size, b"\0") + bytes(metadata_size) + bytes(payload_bytes)
    return data + bytes(padding)
