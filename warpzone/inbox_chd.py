"""Inbox: a `.chd` dropped for a CHD system is extracted so its image can be identified.

A CHD's own SHA1s describe the compressed container, not the disc, so they cannot be compared
with Redump (which lists the images). `expand_chds` reads each CHD with `chdman info`, extracts
it to the work folder (`extractcd` for a CD or GD-ROM, `extractdvd` for a DVD) and hands the
extracted files to the same identification, grouping and conversion as dropped images.

The decisions (`parse_info`, `plan_extraction`, `file_stem`, `split_unmatched`) are pure; the
rest runs chdman. See "CHD images" in development/design/inbox.md.
"""

from __future__ import annotations

import posixpath
import re
import shutil
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from warpzone.dat import FileInfo
from warpzone.inbox_core import (
    ItemFailure,
    Staged,
    Tools,
    ToolTimeout,
    free_space_check,
    next_free,
    run,
    tail,
)

STEM_BYTES = 120  # a file name holds 255 bytes, and chdman adds `(Track N).bin` or `NN.bin`
CD_TAGS = ("CHT2", "CHTR", "CHCD")  # CD metadata: track list, older track list, oldest
GD_TAGS = ("CHGD", "CHGT")  # GD-ROM metadata
DVD_TAG = "DVD "
_SIZE = re.compile(r"Logical size:\s*([\d,]+)")
_TAG = re.compile(r"Tag='([^']{4})'")
_TRACK = re.compile(r"TRACK:\d+\s+TYPE:(\S+)")
NOT_A_DISC = (
    "not a CD, GD-ROM or DVD CHD (a hard disk, laserdisc or arcade disk): inbox cannot identify it"
)


@dataclass(frozen=True)
class ChdInfo:
    kind: str  # cd | gd | dvd
    logical_size: int  # bytes of the image inside the CHD
    tracks: tuple[str, ...]  # chdman's TYPE of each track (MODE1, MODE2_RAW, AUDIO, ...); CD/GD


def parse_info(text: str) -> ChdInfo | None:
    """What `chdman info` says about a CHD, or None when it is no disc image (no size, or none of
    the CD, GD-ROM and DVD metadata tags)."""
    size = _SIZE.search(text)
    if not size:
        return None
    tags = set(_TAG.findall(text))
    tracks = tuple(_TRACK.findall(text))
    logical = int(size.group(1).replace(",", ""))
    if tags & set(GD_TAGS):
        return ChdInfo("gd", logical, tracks)
    if tags & set(CD_TAGS):
        return ChdInfo("cd", logical, tracks)
    if DVD_TAG in tags:
        return ChdInfo("dvd", logical, ())
    return None


def file_stem(name: str) -> str:
    """The base name of the files extracted from a CHD: its file name without `.chd`, cut so
    that the names chdman derives from it still fit in a file name."""
    stem = name[:-4] if name.lower().endswith(".chd") else name
    return stem.encode()[:STEM_BYTES].decode(errors="ignore")


def plan_extraction(info: ChdInfo, stem: str) -> tuple[str, list[str]]:
    """(chdman command, its output options) to extract a CHD. File names are relative: the
    caller puts them in a folder.

    A one-track CD is written as one file (`-ob`), an `.iso` when the track is a 2048-byte data
    one (a PS2 disc made into a CD-mode CHD from its `.iso` comes back as that `.iso`, which is
    what Redump lists). A CD with several tracks is split in one file per track (`-sb`, which
    cannot be combined with `-ob`) because Redump lists each track; a GD-ROM always is."""
    if info.kind == "dvd":
        return "extractdvd", ["-o", f"{stem}.iso"]
    if info.kind == "gd":
        return "extractcd", ["-o", f"{stem}.gdi"]
    if len(info.tracks) == 1:
        data = ".iso" if info.tracks[0] == "MODE1" else ".bin"
        return "extractcd", ["-o", f"{stem}.cue", "-ob", f"{stem}{data}"]
    return "extractcd", ["-o", f"{stem}.cue", "-sb"]


def unmatched_reason(chd: str) -> str:
    return (
        f"{chd}: extracted with chdman, but its content matches no Redump entry (a CHD made from "
        "a changed, trimmed or re-ripped image cannot be matched; drop the original cue/bin, "
        "gdi or iso instead)"
    )


def split_unmatched(
    unmatched: Sequence[FileInfo], staged: Mapping[str, Staged]
) -> tuple[list[FileInfo], list[str]]:
    """The unmatched files that do not come from a CHD, and one reason for each CHD that still
    has some."""
    rest: list[FileInfo] = []
    reasons: dict[str, str] = {}
    for f in unmatched:
        chd = staged[f.path].chd
        if chd is None:
            rest.append(f)
        else:
            reasons.setdefault(chd, unmatched_reason(chd))
    return rest, list(reasons.values())


def _located(args: Sequence[str], folder: Path) -> list[str]:
    """The output options with their file names put in `folder`."""
    out: list[str] = []
    for i, arg in enumerate(args):
        out.append(str(folder / arg) if i and args[i - 1] in ("-o", "-ob") else arg)
    return out


def expand_chds(
    staged: list[Staged], work: Path, tools: Tools, *, apply: bool
) -> tuple[list[Staged], list[str]]:
    """The staged files with each `.chd` replaced by the files extracted from it, and the
    reasons for the CHDs that could not be read.

    Every CHD is read first and the room is checked once for all of them: the images, plus
    (with `apply`, which builds the new CHDs in the same folder) the CHDs themselves."""
    chds = [s for s in staged if s.path.suffix.lower() == ".chd"]
    if not chds:
        return staged, []
    failures: list[str] = []
    if not tools.chdman:
        reason = "chdman not found (set [inbox] chdman or put it on PATH)"
        return [s for s in staged if s not in chds], [f"{s.rel}: {reason}" for s in chds]
    chdman = tools.chdman
    infos: dict[str, ChdInfo] = {}
    for s in chds:
        try:
            code, text = run([chdman, "info", "-i", str(s.path)], tools.timeout)
        except ToolTimeout as exc:
            failures.append(f"{s.rel}: {exc}")
            continue
        if code != 0:
            failures.append(f"{s.rel}: chdman could not read it ({tail(text)})")
            continue
        info = parse_info(text)
        if info is None:
            failures.append(f"{s.rel}: {NOT_A_DISC}")
            continue
        infos[s.rel] = info
    readable = [s for s in chds if s.rel in infos]
    needed = sum(i.logical_size for i in infos.values())
    if apply:
        needed += sum(s.path.stat().st_size for s in readable)
    try:
        if readable:
            free_space_check(work, needed)
    except ItemFailure as exc:
        failures.extend(f"{s.rel}: {exc}" for s in readable)
        readable = []
    taken = {s.rel for s in staged if s not in chds}
    extracted: dict[str, list[Staged]] = {}
    for s in readable:
        try:
            extracted[s.rel] = _extract(chdman, s, infos[s.rel], work, tools.timeout, taken)
        except ItemFailure as exc:
            failures.append(f"{s.rel}: {exc}")
            continue
        taken.update(e.rel for e in extracted[s.rel])
    out: list[Staged] = []
    for s in staged:
        if s not in chds:
            out.append(s)
        else:
            out.extend(extracted.get(s.rel, []))
    return out, failures


def _extract(
    chdman: str, chd: Staged, info: ChdInfo, work: Path, timeout: float, taken: set[str]
) -> list[Staged]:
    folder = next_free(work, "c")
    folder.mkdir(parents=True)
    command, args = plan_extraction(info, file_stem(chd.path.name))
    try:
        code, text = run([chdman, command, "-i", str(chd.path), *_located(args, folder)], timeout)
        files = sorted(p for p in folder.iterdir() if p.is_file())
        if code != 0 or not files:
            raise ItemFailure(f"chdman could not extract it ({tail(text)})")
        base = posixpath.dirname(chd.rel)
        found = [
            Staged(p, posixpath.join(base, p.name) if base else p.name, chd=chd.rel) for p in files
        ]
        clash = next((f.rel for f in found if f.rel in taken), None)
        if clash:
            raise ItemFailure(f"it holds {clash}, a name another file of the item already has")
        return found
    except ItemFailure:
        shutil.rmtree(folder, ignore_errors=True)
        raise
