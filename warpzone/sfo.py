"""The key/value table of a PARAM.SFO, the metadata file of PlayStation titles.

Pure: bytes in, a dict out. Used by `warpzone inbox` for PS3 discs and PS4 packages.
"""

from __future__ import annotations

import struct

MAX_SFO_BYTES = 1 << 16


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
