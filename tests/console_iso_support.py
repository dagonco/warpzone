"""Tiny synthetic disc images for the console ISO tests: ISO9660 with the PS3 files, and
XDVDFS volumes at the offsets real Xbox / Xbox 360 images use. No real game data."""

from __future__ import annotations

import struct
from pathlib import Path

SECTOR = 2048
XGD2, XGD3, REDUMP = 0xFDA0000, 0x2080000, 0x18300000


def sfo(values: dict[str, str | int]) -> bytes:
    """A PARAM.SFO holding the given strings (utf-8, NUL terminated) and integers."""
    keys, data, index = b"", b"", b""
    for key, value in values.items():
        if isinstance(value, int):
            fmt, raw = 0x0404, struct.pack("<I", value)
        else:
            fmt, raw = 0x0204, value.encode() + b"\x00"
        index += struct.pack("<HHIII", len(keys), fmt, len(raw), len(raw), len(data))
        keys += key.encode() + b"\x00"
        data += raw
    key_start = 20 + len(index)
    data_start = key_start + len(keys)
    head = b"\x00PSF" + struct.pack("<IIII", 0x101, key_start, data_start, len(values))
    return head + index + keys + data


def _iso_record(name: str, extent: int, size: int, is_dir: bool) -> bytes:
    raw = name.encode("latin-1")
    length = 33 + len(raw) + (len(raw) + 1) % 2
    rec = bytearray(length)
    rec[0] = length
    rec[2:6] = struct.pack("<I", extent)
    rec[6:10] = struct.pack(">I", extent)
    rec[10:14] = struct.pack("<I", size)
    rec[14:18] = struct.pack(">I", size)
    rec[25] = 2 if is_dir else 0
    rec[32] = len(raw)
    rec[33 : 33 + len(raw)] = raw
    return bytes(rec)


def _iso_dir(records: list[bytes]) -> bytes:
    body = b"".join(records)
    return body + bytes(-len(body) % SECTOR)


def ps3_iso(title_id: str = "BLES00000", title: str = "Test Game", *, sfb: bool = True) -> bytes:
    """Sectors 0-15 empty, 16 the PVD, 18 root, 19 PS3_GAME, 20 PS3_DISC.SFB, 21 PARAM.SFO."""
    param = sfo({"TITLE": title, "TITLE_ID": title_id, "PS3_SYSTEM_VER": "04.8100"})
    dot, dotdot = _iso_record("\x00", 0, 0, True), _iso_record("\x01", 0, 0, True)
    root = _iso_dir(
        [
            dot,
            dotdot,
            _iso_record("PS3_GAME", 19, SECTOR, True),
            *([_iso_record("PS3_DISC.SFB;1", 20, 16, False)] if sfb else []),
        ]
    )
    game = _iso_dir([dot, dotdot, _iso_record("PARAM.SFO;1", 21, len(param), False)])
    pvd = bytearray(SECTOR)
    pvd[0:6] = b"\x01CD001"
    pvd[6] = 1
    pvd[128:130] = struct.pack("<H", SECTOR)
    pvd[156:190] = _iso_record("\x00", 18, SECTOR, True)[:34]
    image = bytearray(16 * SECTOR) + pvd + bytes(SECTOR) + root + game
    image += b".SFB" + bytes(SECTOR - 4) + param
    return bytes(image)


def _entry(left: int, right: int, sector: int, size: int, attrs: int, name: str) -> bytes:
    raw = name.encode("latin-1")
    body = struct.pack("<HHIIBB", left, right, sector, size, attrs, len(raw)) + raw
    return body + bytes(-len(body) % 4)


def xdvdfs_dir(files: dict[str, int]) -> bytes:
    """A directory of files (name -> first sector) as a balanced binary tree, name-sorted."""
    names = sorted(files, key=str.lower)
    nodes: list[tuple[str, int, int]] = []  # name, left index, right index (-1 none)

    def build(lo: int, hi: int) -> int:
        if lo >= hi:
            return -1
        mid = (lo + hi) // 2
        slot = len(nodes)
        nodes.append((names[mid], -1, -1))
        left, right = build(lo, mid), build(mid + 1, hi)
        nodes[slot] = (names[mid], left, right)
        return slot

    build(0, len(names))
    sizes = [len(_entry(0, 0, 0, 0, 0, n)) for n, _, _ in nodes]
    offsets = [sum(sizes[:i]) // 4 for i in range(len(nodes))]
    out = b""
    for (name, left, right), _ in zip(nodes, sizes, strict=True):
        out += _entry(
            offsets[left] if left >= 0 else 0,
            offsets[right] if right >= 0 else 0,
            files[name],
            16,
            0x20,
            name,
        )
    return out + b"\xff" * (-len(out) % SECTOR or 0)


def write_xdvdfs(path: Path, base: int, exe: str, extra: tuple[str, ...] = ("a.xbe",)) -> Path:
    """An image whose XDVDFS partition starts at `base`: descriptor at +0x10000, a root
    directory in sector 33 holding `exe` (and `extra`), so the file is sparse for big bases."""
    files = {exe: 40, **{n: 41 + i for i, n in enumerate(extra)}}
    root = xdvdfs_dir(files)
    desc = b"MICROSOFT*XBOX*MEDIA" + struct.pack("<II", 33, len(root))
    with path.open("wb") as f:
        f.seek(base + 0x10000)
        f.write(desc)
        f.seek(base + 33 * SECTOR)
        f.write(root)
        f.seek(base + 40 * SECTOR)
        f.write(b"EXE")
    return path
