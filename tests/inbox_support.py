"""Helpers for the inbox tests: tiny DATs, a fake hub tree and stand-ins for the tools."""

from __future__ import annotations

import hashlib
import os
import stat
import sys
import zlib
from collections.abc import Sequence
from pathlib import Path

Rom = tuple[str, bytes]  # (rom name in the DAT, content)
Game = tuple[str, Sequence[Rom]]


def dat_text(name: str, games: Sequence[Game]) -> str:
    lines = [f'clrmamepro (\n\tname "{name}"\n\tversion "test"\n)\n']
    for game_name, roms in games:
        lines.append(f'game (\n\tname "{game_name}"')
        for rom_name, data in roms:
            crc = f"{zlib.crc32(data) & 0xFFFFFFFF:08X}"
            sha1 = hashlib.sha1(data).hexdigest().upper()
            lines.append(f'\trom ( name "{rom_name}" size {len(data)} crc {crc} sha1 {sha1} )')
        lines.append(")\n")
    return "\n".join(lines)


def write_dat(directory: Path, name: str, games: Sequence[Game]) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{name}.dat"
    path.write_text(dat_text(name, games), encoding="utf-8")
    return path


def make_hub(root: Path, systems: Sequence[str] = ("nes", "psx", "psp", "gc")) -> Path:
    (root / "inbox").mkdir(parents=True, exist_ok=True)
    for system in systems:
        (root / "library" / "roms" / system).mkdir(parents=True, exist_ok=True)
    return root


def age(path: Path, seconds: float = 3600) -> None:
    """Make a file or folder look old, so the inbox does not think it is still copying."""
    import time

    stamp = time.time() - seconds
    for p in [path, *(path.rglob("*") if path.is_dir() else [])]:
        os.utime(p, (stamp, stamp), follow_symlinks=False)


# A stand-in for chdman: a CHD is "CHD <kind>\n<md5 of the tracks>\n"; it understands
# createcd, createdvd, verify and info, and fails on demand through STUB_CHDMAN_FAIL.
CHDMAN = r"""
import hashlib, os, re, sys

args = sys.argv[1:]
def opt(name):
    return args[args.index(name) + 1]

def tracks(path):
    if path.endswith(".cue"):
        base = os.path.dirname(path)
        names = re.findall(r'FILE "(.+)"', open(path).read())
        return [os.path.join(base, n) for n in names]
    return [path]

cmd = args[0]
if cmd in ("createcd", "createdvd"):
    if os.environ.get("STUB_CHDMAN_FAIL") == "create":
        print("Error: compression failed", file=sys.stderr)
        sys.exit(1)
    md5 = hashlib.md5()
    for t in tracks(opt("-i")):
        md5.update(open(t, "rb").read())
    with open(opt("-o"), "w") as f:
        f.write("CHD %s\n%s\n" % (cmd, md5.hexdigest()))
elif cmd == "verify":
    if os.environ.get("STUB_CHDMAN_FAIL") == "verify":
        print("Error: raw SHA1 mismatch", file=sys.stderr)
        sys.exit(1)
    print("Raw SHA1 verification successful!")
    print("Overall SHA1 verification successful!")
elif cmd == "info":
    payload = open(opt("-i")).read().split("\n")[1]
    print("SHA1:         " + hashlib.sha1(("meta" + payload).encode()).hexdigest())
    print("Data SHA1:    " + hashlib.sha1(payload.encode()).hexdigest())
else:
    sys.exit(2)
"""

# A stand-in for RAHasher: the "hash" of a cue is the md5 of its tracks, of a stub CHD
# the md5 stored in it, of anything else the md5 of its bytes (zip: of its first member).
RAHASHER = r"""
import hashlib, os, re, sys, zipfile

path = sys.argv[2]
if os.environ.get("STUB_RA_MODE") == "change" and path.endswith(".chd"):
    print("0" * 32)
    sys.exit(0)
if path.endswith(".chd"):
    print(open(path).read().split("\n")[1])
elif path.endswith(".cue"):
    md5 = hashlib.md5()
    base = os.path.dirname(path)
    for n in re.findall(r'FILE "(.+)"', open(path).read()):
        md5.update(open(os.path.join(base, n), "rb").read())
    print(md5.hexdigest())
elif path.endswith(".zip"):
    z = zipfile.ZipFile(path)
    print(hashlib.md5(z.read(z.namelist()[0])).hexdigest())
else:
    print(hashlib.md5(open(path, "rb").read()).hexdigest())
"""


def make_stub(directory: Path, name: str, source: str) -> str:
    directory.mkdir(parents=True, exist_ok=True)
    script = directory / f"{name}.py"
    script.write_text(source, encoding="utf-8")
    wrapper = directory / name
    wrapper.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{script}" "$@"\n', encoding="utf-8")
    wrapper.chmod(wrapper.stat().st_mode | stat.S_IXUSR)
    return str(wrapper)


def stub_md5(*chunks: bytes) -> str:
    digest = hashlib.md5()
    for chunk in chunks:
        digest.update(chunk)
    return digest.hexdigest()
