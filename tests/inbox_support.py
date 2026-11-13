"""Helpers for the inbox tests: tiny DATs, a fake hub tree and stand-ins for the tools."""

from __future__ import annotations

import hashlib
import json
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
    (root / "library" / ".stfolder").mkdir(parents=True, exist_ok=True)
    for system in systems:
        (root / "library" / "roms" / system).mkdir(parents=True, exist_ok=True)
    return root


def age(path: Path, seconds: float = 3600) -> None:
    """Make a file or folder look old, so the inbox does not think it is still copying."""
    import time

    stamp = time.time() - seconds
    for p in [path, *(path.rglob("*") if path.is_dir() else [])]:
        os.utime(p, (stamp, stamp), follow_symlinks=False)


# A stand-in for chdman: a CHD it makes is "CHD <kind>\n<md5 of the tracks>\n"; it understands
# createcd, createdvd, verify and info, and fails on demand through STUB_CHDMAN_FAIL. A CHD a
# test drops is "CHD source\n<json>" (see `source_chd`): info describes it like chdman does and
# extractcd / extractdvd write its tracks the way chdman 0.289 does (`-sb` and `-ob` together
# fail, a `.gdi` output is always split, a many-track cue without `-sb` is one bin).
CHDMAN = r"""
import hashlib, json, os, re, sys

args = sys.argv[1:]
def opt(name):
    return args[args.index(name) + 1]

def tracks(path):
    if path.endswith(".cue"):
        base = os.path.dirname(path)
        names = re.findall(r'FILE "(.+)"', open(path).read())
        return [os.path.join(base, n) for n in names]
    return [path]

def source(path):
    head, _, body = open(path).read().partition("\n")
    return json.loads(body) if head == "CHD source" else None

cmd = args[0]
if os.environ.get("STUB_CHDMAN_LOG"):
    with open(os.environ["STUB_CHDMAN_LOG"], "a") as log:
        log.write(" ".join(args) + "\n")
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
elif cmd == "info" and source(opt("-i")):
    doc = source(opt("-i"))
    print("Input file:   " + opt("-i"))
    print("Logical size: {:,} bytes".format(doc["logical"]))
    for n, (kind, _data) in enumerate(doc["tracks"] if doc["tag"] != "DVD " else []):
        print("Metadata:     Tag='%s'  Index=%d  Length=87 bytes" % (doc["tag"], n))
        print("              TRACK:%d TYPE:%s SUBTYPE:NONE FRAMES:10" % (n + 1, kind))
    if doc["tag"] == "DVD ":
        print("Metadata:     Tag='DVD '  Index=0  Length=1 bytes")
        print("              .")
elif cmd in ("extractcd", "extractdvd"):
    if os.environ.get("STUB_CHDMAN_FAIL") == "extract":
        print("Error opening input file: Invalid data", file=sys.stderr)
        sys.exit(1)
    doc = source(opt("-i"))
    out = opt("-o")
    folder, stem = os.path.dirname(out), os.path.splitext(os.path.basename(out))[0]
    tracks_ = [(kind, bytes.fromhex(h)) for kind, h in doc["tracks"]]
    def put(name, data):
        with open(os.path.join(folder, name), "wb") as f:
            f.write(data)
    if cmd == "extractdvd":
        put(os.path.basename(out), tracks_[0][1])
    elif out.endswith(".gdi"):
        rows = [str(len(tracks_))]
        for n, (kind, data) in enumerate(tracks_, 1):
            name = "%s%02d.%s" % (stem, n, "raw" if kind == "AUDIO" else "bin")
            put(name, data)
            rows.append("%d 0 %d 2352 %s 0" % (n, 0 if kind == "AUDIO" else 4, name))
        put(os.path.basename(out), ("\n".join(rows) + "\n").encode())
    else:
        if "-sb" in args and "-ob" in args:
            print("Fatal error occurred: 1", file=sys.stderr)
            sys.exit(1)
        modes = {"MODE1": "MODE1/2048", "MODE2_RAW": "MODE2/2352", "AUDIO": "AUDIO"}
        rows = []
        if "-sb" in args:
            files = [("%s (Track %d).bin" % (stem, n), [t]) for n, t in enumerate(tracks_, 1)]
        else:
            name = os.path.basename(opt("-ob")) if "-ob" in args else stem + ".bin"
            files = [(name, tracks_)]
        n = 0
        for name, parts in files:
            put(name, b"".join(d for _k, d in parts))
            rows.append('FILE "%s" BINARY' % name)
            for kind, _d in parts:
                n += 1
                rows += ["  TRACK %02d %s" % (n, modes[kind]), "    INDEX 01 00:00:00"]
        put(os.path.basename(out), ("\n".join(rows) + "\n").encode())
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


def source_chd(tag: str, tracks: Sequence[tuple[str, bytes]], logical: int | None = None) -> bytes:
    """A CHD for the stand-in chdman to read: `tag` is the metadata tag (`CHT2` and `CHGD` for
    a CD and a GD-ROM, `DVD ` for a DVD, `GDDD` for a hard disk), `tracks` the (type, bytes) of
    each track, `logical` the size `info` reports (default: the bytes of the tracks)."""
    size = logical if logical is not None else sum(len(d) for _t, d in tracks)
    doc = {"tag": tag, "logical": size, "tracks": [(t, d.hex()) for t, d in tracks]}
    return b"CHD source\n" + json.dumps(doc).encode()


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
