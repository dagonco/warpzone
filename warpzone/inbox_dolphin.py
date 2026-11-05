"""Compressed Wii and GameCube images (`.rvz`, `.wia`) for `warpzone inbox`.

Dolphin's WIA and RVZ containers keep a copy of the disc header, so the inbox tells a Wii
disc from a GameCube one, and reads the game ID and the title, from the first 0xD8 bytes of
the file. No tool is run and no DAT is used: the compressed data cannot be hashed without
expanding it, so the name comes from the header instead.

- The container header: the magic `RVZ\\x01` or `WIA\\x01` at 0, and at 0x2C the size of the
  file as the writer saw it (u64, big-endian; 0 means "not recorded"), which must equal the
  real size (a smaller file is a copy that stopped early).
- The disc: at 0x48 the disc type (u32, big-endian; 1 GameCube, 2 Wii) and at 0x58 the first
  0x80 bytes of the disc: the game ID (6 ASCII characters), at +0x18 the Wii magic
  `5D1C9EA3`, at +0x1C the GameCube magic `C2339F3D`, and the title (64 bytes) at +0x20. The
  disc type and the magic must agree.

A recognised image is filed in `roms/wii/` or `roms/gc/` (the ES-DE systems, which Dolphin
opens as they are, compressed) as `<name> [<GAME ID>].rvz` (or `.wia`), where the name is
the file's own and the header's title stands in when the file name says nothing; the bytes
are not changed. Nothing is ever replaced: the same content already in the library is reported,
different content under that name goes to review. `.gcz` is another format with no copy of
the disc header and is not handled here.

How the module hooks into `inbox.py`: `HANDLER` is a kind handler that claims an item whose
file names end in `.rvz` or `.wia`, loose, in a folder or in an archive. The decisions
(`claimed_name`, `judge`, `target_name`) are pure functions of bytes; `detect` and
`place_image` touch the disk. See development/design/inbox.md.
"""

from __future__ import annotations

import re
import struct
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO

from warpzone import inbox_console_iso, inbox_core
from warpzone.inbox_core import Context, ItemFailure, ItemResult, Options, Staged, UnitResult

CLAIMED_SUFFIXES = (".rvz", ".wia")
MAGICS = {b"RVZ\x01": "RVZ", b"WIA\x01": "WIA"}
FILE_SIZE_AT = 0x2C  # u64 BE: the size of the whole file, 0 when not recorded
DISC_TYPE_AT = 0x48  # u32 BE
DISC_HEADER_AT = 0x58  # the first 0x80 bytes of the disc
DISC_HEADER_BYTES = 0x80
HEADER_BYTES = DISC_HEADER_AT + DISC_HEADER_BYTES
GAME_ID_BYTES = 6
WII_MAGIC_AT, GC_MAGIC_AT = 0x18, 0x1C  # inside the disc header
WII_MAGIC, GC_MAGIC = bytes.fromhex("5D1C9EA3"), bytes.fromhex("C2339F3D")
TITLE_AT, TITLE_BYTES = 0x20, 0x40  # inside the disc header
GC_TYPE, WII_TYPE = 1, 2
SYSTEMS = {GC_TYPE: "gc", WII_TYPE: "wii"}  # the ES-DE system folder inside library/roms/
LABELS = {"gc": "GameCube", "wii": "Wii"}

OK, INVALID = "ok", "invalid"

_GAME_ID = re.compile(r"^[A-Z0-9]{6}$")
_UNSAFE = re.compile(r'[\x00-\x1f\x7f/\\*?"<>|]')


# ------------------------------------------------------------------ pure decisions


def claimed_name(name: str) -> bool:
    """Does a file name say "compressed Dolphin image"?"""
    return name.lower().endswith(CLAIMED_SUFFIXES)


@dataclass(frozen=True)
class Verdict:
    status: str  # OK | INVALID
    reason: str = ""  # why it is not OK
    system: str = ""  # "wii" or "gc"
    game_id: str = ""
    title: str = ""
    container: str = ""  # "RVZ" or "WIA"

    @property
    def ok(self) -> bool:
        return self.status == OK

    def describe(self) -> str:
        if not self.ok:
            return self.reason
        shown = f" {self.title}" if self.title else ""
        return f"{LABELS[self.system]} disc image ({self.container}), game ID {self.game_id}{shown}"


def _invalid(reason: str) -> Verdict:
    return Verdict(INVALID, reason)


def clean_title(raw: bytes) -> str:
    """The title of a disc header as text that is safe in a file name: ASCII only, no
    path or control characters, `:` written as ` -`, blanks collapsed."""
    text = raw.split(b"\x00", 1)[0].decode("ascii", errors="ignore")
    text = _UNSAFE.sub("", text.replace(":", " -"))
    return re.sub(r"\s+", " ", text).strip(" .")


def judge(head: bytes, file_size: int) -> Verdict:
    """The verdict from the first 0xD8 bytes of the file and the size of the file."""
    container = MAGICS.get(head[:4])
    if container is None:
        return _invalid("not a WIA or RVZ image (no WIA or RVZ magic at 0)")
    if len(head) < HEADER_BYTES:
        return _invalid(f"the {container} header is cut short: a truncated file")
    recorded = struct.unpack_from(">Q", head, FILE_SIZE_AT)[0]
    if recorded and recorded != file_size:
        return _invalid(
            f"the {container} header says the file is {recorded} bytes, but it is {file_size} "
            "bytes: a truncated or damaged copy"
        )
    disc_type = struct.unpack_from(">I", head, DISC_TYPE_AT)[0]
    system = SYSTEMS.get(disc_type)
    if system is None:
        return _invalid(f"the {container} header has an unknown disc type ({disc_type})")
    disc = head[DISC_HEADER_AT:HEADER_BYTES]
    wii = disc[WII_MAGIC_AT : WII_MAGIC_AT + 4] == WII_MAGIC
    gc = disc[GC_MAGIC_AT : GC_MAGIC_AT + 4] == GC_MAGIC
    if (system == "wii" and not wii) or (system == "gc" and not gc) or (wii and gc):
        return _invalid(
            f"the {container} header says {LABELS[system]} but its disc header does not "
            "carry the matching magic"
        )
    game_id = disc[:GAME_ID_BYTES].decode("ascii", errors="replace")
    if not _GAME_ID.match(game_id):
        return _invalid(f"the {container} disc header has no valid game ID")
    title = clean_title(disc[TITLE_AT : TITLE_AT + TITLE_BYTES])
    return Verdict(OK, system=system, game_id=game_id, title=title, container=container)


def clean_file_stem(stem: str) -> str:
    """A file name stem made safe the way `clean_title` makes a title safe, keeping
    non-ASCII letters (a file name is already text)."""
    text = _UNSAFE.sub("", stem.replace(":", " -"))
    return re.sub(r"\s+", " ", text).strip(" .")


def target_name(verdict: Verdict, file_name: str) -> str:
    """The name in the library: `<name> [<GAME ID>]` and the extension of `file_name` in
    lower case (`.rvz` or `.wia`). The name is the file's own (a set name like
    `Game (USA) (En,Fr,Es)`), without a trailing `[<GAME ID>]` it may carry already; the
    disc header's title stands in when the file name is empty or only the game ID."""
    dot = file_name.rfind(".")
    stem, extension = (file_name[:dot], file_name[dot:]) if dot >= 0 else (file_name, "")
    tag = f"[{verdict.game_id}]"
    if stem.upper().rstrip().endswith(tag):
        stem = stem.rstrip()[: -len(tag)]
    name = clean_file_stem(stem)
    if not name or name.upper() == verdict.game_id:
        name = verdict.title
    return (f"{name} {tag}" if name else tag) + extension.lower()


# ------------------------------------------------------------------ disk


def detect(f: BinaryIO) -> Verdict:
    """Judge an image from its header; reads at most 0xD8 bytes."""
    size = f.seek(0, 2)
    f.seek(0)
    return judge(f.read(HEADER_BYTES), size)


def detect_path(path: Path) -> Verdict:
    with path.open("rb") as f:
        return detect(f)


def place_image(ctx: Context, path: Path, rel: str, verdict: Verdict) -> UnitResult:
    """File one recognised image under its header name; never replaces a file. The outcome
    is `review` (with a reason) when it cannot be placed."""
    entry = target_name(verdict, path.name)
    res = UnitResult(verdict.system, entry, "placed" if ctx.opts.apply else "planned")
    res.notes.append(f"detected: {verdict.describe()}")
    if entry != path.name:
        res.notes.append(f"{path.name} is filed as {entry} (with its game ID)")
    dest = ctx.roms / verdict.system / entry
    try:
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


class DolphinHandler:
    """Items holding a `.rvz` or `.wia` (the item is taken over whole)."""

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
        targets: dict[tuple[str, str], str] = {}  # (system, target name) -> the file filed there
        for s in staged:
            if claimed_name(s.path.name):
                self._one(ctx, s, targets, result)
            elif s.path.suffix.lower() not in inbox_core.SIDECAR_EXTENSIONS:
                others.append(s.rel)
        if others:
            shown = ", ".join(others[:5]) + (", ..." if len(others) > 5 else "")
            result.reasons.append(f"not a Wii or GameCube image, drop it as its own item: {shown}")

    def _one(
        self, ctx: Context, s: Staged, targets: dict[tuple[str, str], str], result: ItemResult
    ) -> None:
        try:
            verdict = detect_path(s.path)
        except OSError as exc:
            result.reasons.append(f"{s.rel}: {exc.strerror or exc}")
            return
        if not verdict.ok:
            result.reasons.append(f"{s.rel}: {verdict.reason}")
            return
        key = (verdict.system, target_name(verdict, s.path.name))
        if key in targets:
            result.reasons.append(
                f"{s.rel}: would be filed as {key[1]} like {targets[key]} "
                "(the same name and game ID): drop them as separate items"
            )
            return
        targets[key] = s.rel
        unit = place_image(ctx, s.path, s.rel, verdict)
        result.units.append(unit)
        if unit.reason:
            result.reasons.append(unit.reason)


HANDLER = DolphinHandler()
