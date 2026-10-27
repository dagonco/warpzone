"""Helpers for the Switch inbox tests: tiny PFS0 files, a titledb folder and a stand-in nsz."""

from __future__ import annotations

import json
import struct
from collections.abc import Mapping, Sequence
from pathlib import Path

NSZ_MAGIC = b"NSZSTUB"
KEYS_TEXT = "master_key_00 = 0123456789abcdef0123456789abcdef\n"  # gitleaks:allow (fake)
KEYS_SECRET = "0123456789abcdef0123456789abcdef"  # gitleaks:allow (fake)


def pfs0(members: Mapping[str, bytes]) -> bytes:
    """A PFS0 container holding `members`."""
    names = b"".join(n.encode() + b"\0" for n in members)
    entries = b""
    offset = 0
    name_at = 0
    for name, data in members.items():
        entries += struct.pack("<QQII", offset, len(data), name_at, 0)
        offset += len(data)
        name_at += len(name.encode()) + 1
    head = b"PFS0" + struct.pack("<II", len(members), len(names)) + b"\0" * 4
    return head + entries + names + b"".join(members.values())


def make_nsp(
    title_id: str, version: int = 0, *, title_type: int | None = None, filler: int = 200
) -> bytes:
    """A fake NSP: a valid PFS0 whose `stub.cnmt` carries what the stand-in nsz reports."""
    kind = {"000": 0x80, "800": 0x81}.get(title_id[-3:], 0x82)
    meta = {"tid": title_id, "version": version, "type": title_type or kind}
    return pfs0(
        {
            "stub.cnmt": json.dumps(meta).encode(),
            "data.nca": (title_id + str(version)).encode() * filler,
        }
    )


def make_nsp_multi(ids: Sequence[tuple[str, int]]) -> bytes:
    members = {}
    for i, (tid, version) in enumerate(ids):
        members[f"stub{i}.cnmt"] = json.dumps(
            {"tid": tid, "version": version, "type": 0x80}
        ).encode()
    return pfs0(members)


def make_nsz(nsp: bytes) -> bytes:
    return NSZ_MAGIC + nsp


def write_titledb(directory: Path, regions: Mapping[str, Sequence[dict]]) -> Path:
    """`regions` maps a file name (`US.en.json`) to its entries; keys are made up."""
    directory.mkdir(parents=True, exist_ok=True)
    for name, entries in regions.items():
        data = {str(70010000000000 + i): entry for i, entry in enumerate(entries)}
        (directory / name).write_text(json.dumps(data), encoding="utf-8")
    return directory


def entry(tid: str, name: str, ids: Sequence[str] | None = None, demo: bool = False) -> dict:
    return {"id": tid, "name": name, "ids": list(ids) if ids else None, "isDemo": demo}


# A stand-in for nsz. It understands `-i --depth 3`, `-D -o DIR` and `-V`, each with
# `--keys PATH`, over files made by `make_nsp` / `make_nsz`. STUB_NSZ_FAIL picks a failure:
#   info        `-i` exits 1 (and complains about a key on stderr)
#   decompress  `-D` exits 1 and leaves a partial file
#   silent      `-D` exits 0 but never says [DONE]
#   garbage     `-D` says [DONE] but writes something that is not a PFS0
#   verify      `-V` exits 1
#   unverified  `-V` exits 0 without saying [VERIFIED]
# Every call is appended to STUB_NSZ_LOG (arguments only). Like the real nsz, `-i` prints
# key material; the stand-in prints the content of the keys file to prove it is never shown.
NSZ = r"""
import json, os, struct, sys

args = sys.argv[1:]
log = os.environ.get("STUB_NSZ_LOG")
if log:
    with open(log, "a") as f:
        f.write(" ".join(args) + "\n")
fail = os.environ.get("STUB_NSZ_FAIL", "")

def opt(name):
    return args[args.index(name) + 1]

def load(path):
    data = open(path, "rb").read()
    return data[7:] if data.startswith(b"NSZSTUB") else data

def members(data):
    n, sl = struct.unpack("<II", data[4:12])
    table = 16 + 24 * n
    out = {}
    for i in range(n):
        off, size, so, _ = struct.unpack("<QQII", data[16 + 24 * i: 40 + 24 * i])
        name = data[table + so: data.index(b"\0", table + so)].decode()
        out[name] = data[table + sl + off: table + sl + off + size]
    return out

keys_text = open(opt("--keys")).read().strip()
path = args[-1]
if "-i" in args:
    if fail == "info":
        print("Error reading key " + keys_text, file=sys.stderr)
        sys.exit(1)
    print("NSZ stub")
    print("\tkey Block = b'" + keys_text + "'")
    for name, blob in members(load(path)).items():
        if not name.endswith(".cnmt"):
            continue
        meta = json.loads(blob)
        print("\t\t\tCnmt")
        print("")
        print("\t\t\ttitleId = " + meta["tid"].lower())
        print("\t\t\tversion = %x" % meta["version"])
        print("\t\t\ttitleType = %x" % meta["type"])
        print("\t\t\tFile Path: " + name)
        print("\ttitleId = 0100000000000000")
elif "-D" in args:
    out = os.path.join(opt("-o"), os.path.splitext(os.path.basename(path))[0] + ".nsp")
    if fail == "decompress":
        open(out, "wb").write(b"PFS0")
        print("OSError: read returned empty 0x4c4b40", file=sys.stderr)
        sys.exit(1)
    if fail == "silent":
        open(out, "wb").write(load(path))
        sys.exit(0)
    data = b"this is not a PFS0 container" if fail == "garbage" else load(path)
    open(out, "wb").write(data)
    print("[DONE]       " + out)
elif "-V" in args:
    if fail == "verify":
        print("Error while verifying file: " + path, file=sys.stderr)
        sys.exit(1)
    if fail != "unverified":
        print("[VERIFIED] x.nca")
else:
    sys.exit(2)
"""
