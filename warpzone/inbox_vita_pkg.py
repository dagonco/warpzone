"""PS Vita `.pkg` packages for `warpzone inbox`: installed with Vita3K, filed like a dump.

`inbox_console_iso` claims every `.pkg`; one that is not a PS4 package comes here. A package
whose header says PSP/Vita (magic `\\x7fPKG`, type 2, the `\\x7fext` header at 0xC0) and whose
content type is a Vita one is handled; any other `\\x7fPKG` keeps the "not supported by inbox
yet" outcome.

Before anything runs, and also without `--apply`, the package is checked from its header and
its unencrypted metadata alone (all big-endian, as `pkg2zip` and Vita3K's `PkgHeader` read
them): the content id at 0x30, the total size at 0x18 (it must be the file's size), the
content type (metadata entry 2: 0x15 a Vita app, 0x16 DLC, 0x1F a theme, 0x18 and 0x1D
PlayStation Mobile) and the `param.sfo` (metadata entry 14, an offset and a size in the
file). Only an app whose `CATEGORY` is `gd` or `gdc` goes on: an update (`gp`), DLC, a theme
or PSM goes to review with the reason. Then the game's zRIF is looked up by content id in the
file named by `[inbox] vita_zrif` (lines of `CONTENT_ID<TAB>zRIF`, or a NoPayStation-style
TSV whose header names the columns `Content ID` and `zRIF`; nothing else is read) and decoded:
base64 of a zlib stream made with a fixed 1024-byte dictionary (`zrif_dict` of
`pkg2zip_zrif.c`, adler32 0x627d1d5d), which must give the game's 512-byte licence
(`inbox_vita.parse_license`) with the package's content id. A package without its zRIF goes
to review. warpzone never downloads keys or packages.

With `--apply`, Vita3K installs it (`--pkg <file> --zrif <zRIF>`) in a folder of its own in
the local work folder, in Vita3K's portable mode: a `portable/` folder next to a symbolic
link to the configured app bundle (`[inbox] vita3k`, default `/Applications/Vita3K.app` on
macOS), or, for a plain binary, `APPIMAGE` pointing into that folder (Vita3K on Linux reads
its portable folder next to `$APPIMAGE`). `Vita3K --version` runs first: it writes
`portable/config.yml`, without which `--pkg` fails, and if that file does not appear Vita3K is
not using the work folder and nothing is installed (Vita3K deletes `ux0/app/<TITLEID>` before
installing, so it must never run against its own data). Vita3K exits 0 even when the install
fails, so the result is judged on disk: `ux0/app/<TITLEID>` with `eboot.bin` and a
`sce_sys/param.sfo` whose title id, category and content id match the package, no
`sce_pfs`, and `ux0/license/<TITLEID>/<CONTENT_ID>.rif` equal to the decoded zRIF. The game
folder and that licence are then filed by `inbox_vita.install`, and the work folder is
deleted.

A zRIF is a key: it is never printed, logged or written to a reason file; the command line
shown has it redacted. It is passed to Vita3K on the command line, the only way Vita3K
takes it, so it is visible in the process list while Vita3K runs (under a second).

The decisions (`parse_header`, `parse_meta`, `vita_kind`, `pkg_problem`, `parse_zrif_table`,
`decode_zrif`, `license_for`, `launch_plan`, `shown_command`, `judge_installed`) are pure, and
`read_pkg` reads only small bounded pieces of a file object; the rest touches the disk, and
Vita3K runs through `RUN`, which tests replace. See development/design/inbox.md.
"""

from __future__ import annotations

import base64
import binascii
import os
import shutil
import struct
import subprocess
import tempfile
import weakref
import zlib
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import BinaryIO

from warpzone import inbox_ps4, inbox_vita
from warpzone.inbox_core import (
    VITA3K_BINARY,
    Context,
    InboxError,
    ItemFailure,
    ItemResult,
    Options,
)
from warpzone.sfo import MAX_SFO_BYTES, parse_sfo
from warpzone.syncthing import REDACTED, scrub

PKG_MAGIC = b"\x7fPKG"
EXT_MAGIC = b"\x7fext"
HEADER_BYTES = 0x100
TYPE_AT, META_AT, META_COUNT_AT, TOTAL_SIZE_AT = 0x06, 0x08, 0x0C, 0x18
CONTENT_ID_AT, CONTENT_ID_BYTES, EXT_AT = 0x30, 0x30, 0xC0
PKG_TYPE_PSP_VITA = 2  # PS3 packages are type 1
META_CONTENT_TYPE, META_SFO = 2, 14
MAX_META_ENTRIES = 64
MAX_META_BYTES = 0x10000
# Content types of metadata entry 2 that are Vita's; PSP and PS1 ones (0x06, 0x07, 0x0E,
# 0x0F, 0x10) and anything else keep the earlier outcome.
VITA_CONTENT_TYPES = {0x15: "app", 0x16: "dlc", 0x1F: "theme", 0x18: "psm", 0x1D: "psm"}

# `zrif_dict` of pkg2zip_zrif.c (mmozeiko/pkg2zip): the preset dictionary every zRIF's zlib
# stream is compressed with. Its adler32 is the DICTID a zRIF's stream header carries.
ZRIF_DICT = (
    bytes(880)
    + b"00009"
    + bytes(11)
    + b"000060000700008"
    + b"\0"
    + b"000030000400005"
    + b"0_00-ADDCONT00002-PCSG00000000001-PCSE000-PCSF000-PCSC000-PCSD000-PCSA000-PCSB000"
    + b"\0\x01\0\x01\0\x01\0\x02\xef\xcd\xab\x89\x67\x45\x23\x01"
)
ZRIF_DICT_ID = 0x627D1D5D
MAX_ZRIF_CHARS = 2048
MAX_TABLE_BYTES = 64 << 20
NO_KEY = frozenset({"", "MISSING", "NOT REQUIRED"})  # NoPayStation's words for "no zRIF"
TSV_CONTENT_ID, TSV_ZRIF = "Content ID", "zRIF"

APP_LINK = "Vita3K.app"  # the link to the app bundle in the work folder
APPIMAGE_NAME = "Vita3K"  # what `APPIMAGE` names in the work folder for a plain binary
PORTABLE = "portable"
CONFIG = "config.yml"
VERSION_TIMEOUT = 120.0
INSTALL_TIMEOUT = 3600.0
NO_ZRIF_FILE = (
    "set [inbox] vita_zrif to a file you keep with the game's zRIF (CONTENT_ID<TAB>zRIF "
    "lines, or a NoPayStation-style TSV); warpzone never downloads keys"
)
NO_TOOL = (
    "Vita3K is needed to install a .pkg and was not found: put Vita3K.app in /Applications "
    "(macOS), or set [inbox] vita3k to the app or its binary"
)


# ------------------------------------------------------------------ pure decisions


@dataclass(frozen=True)
class Header:
    content_id: str  # as written (NUL padding dropped); "" when it is not ASCII
    meta_offset: int
    meta_count: int
    total_size: int


def parse_header(head: bytes) -> Header | None:
    """The fields of a PSP/Vita package header (`\\x7fPKG`, type 2, `\\x7fext` at 0xC0), or None
    for any other file, a PS3 package included."""
    if len(head) < HEADER_BYTES or head[:4] != PKG_MAGIC or head[EXT_AT : EXT_AT + 4] != EXT_MAGIC:
        return None
    if struct.unpack_from(">H", head, TYPE_AT)[0] != PKG_TYPE_PSP_VITA:
        return None
    raw = head[CONTENT_ID_AT : CONTENT_ID_AT + CONTENT_ID_BYTES].split(b"\0", 1)[0]
    try:
        content_id = raw.decode("ascii")
    except UnicodeDecodeError:
        content_id = ""
    return Header(
        content_id,
        struct.unpack_from(">I", head, META_AT)[0],
        struct.unpack_from(">I", head, META_COUNT_AT)[0],
        struct.unpack_from(">Q", head, TOTAL_SIZE_AT)[0],
    )


def parse_meta(data: bytes, count: int) -> dict[int, bytes]:
    """The metadata entries (`u32 id, u32 size, data`) found in `data`, up to `count` of them;
    an entry cut off by the end of `data` ends the list."""
    entries: dict[int, bytes] = {}
    pos = 0
    for _ in range(min(count, MAX_META_ENTRIES)):
        if pos + 8 > len(data):
            break
        ident, size = struct.unpack_from(">II", data, pos)
        if pos + 8 + size > len(data):
            break
        entries.setdefault(ident, data[pos + 8 : pos + 8 + size])
        pos += 8 + size
    return entries


def vita_kind(content_type: int) -> str | None:
    """ "app", "dlc", "theme" or "psm" for a Vita content type; None for any other."""
    return VITA_CONTENT_TYPES.get(content_type)


@dataclass(frozen=True)
class PkgInfo:
    content_id: str
    kind: str  # vita_kind
    total_size: int
    sfo: Mapping[str, str | int] = field(default_factory=dict)  # {} when none or unreadable

    @property
    def title_id(self) -> str:
        """The title id inside a Vita content id (`XXnnnn-PCSAnnnnn_nn-...`), else ""."""
        found = inbox_vita._CONTENT_ID.fullmatch(self.content_id.encode("ascii", "replace"))
        return found.group(1).decode("ascii") if found else ""

    @property
    def title(self) -> str:
        value = self.sfo.get("TITLE")
        return value if isinstance(value, str) else ""


def _read_at(f: BinaryIO, offset: int, length: int) -> bytes:
    f.seek(offset)
    return f.read(length)


def read_pkg(f: BinaryIO) -> PkgInfo | None:
    """What a Vita package is, from its header, its metadata and its `param.sfo` (none of them
    encrypted); None when it is not a Vita package. Reads at most a few dozen KiB."""
    header = parse_header(_read_at(f, 0, HEADER_BYTES))
    if header is None:
        return None
    meta = parse_meta(_read_at(f, header.meta_offset, MAX_META_BYTES), header.meta_count)
    raw_type = meta.get(META_CONTENT_TYPE, b"")
    kind = vita_kind(struct.unpack(">I", raw_type[:4])[0]) if len(raw_type) >= 4 else None
    if kind is None:
        return None
    values: dict[str, str | int] = {}
    where = meta.get(META_SFO, b"")
    if len(where) >= 8:
        offset, size = struct.unpack(">II", where[:8])
        if 0 < size <= MAX_SFO_BYTES:
            data = _read_at(f, offset, size)
            values = parse_sfo(data) if len(data) == size else {}
    return PkgInfo(header.content_id, kind, header.total_size, values)


def pkg_problem(rel: str, info: PkgInfo, file_size: int) -> str | None:
    """Why a Vita package cannot be installed as a game, from what `read_pkg` found, or None."""
    shown = info.content_id or "no readable content id"
    title_id = info.title_id
    if not title_id:
        return f"{rel}: its content id ({shown}) is not a Vita one (XXnnnn-PCSAnnnnn_nn-...)"
    if info.total_size != file_size:
        return (
            f"{rel}: the package header says {info.total_size} bytes but the file has "
            f"{file_size}, so it is incomplete or damaged"
        )
    if info.kind == "dlc":
        return inbox_vita._fail_kind(rel, "dlc", title_id)
    if info.kind == "theme":
        return f"{rel}: this is a theme for the Vita's home screen, which inbox does not install"
    if info.kind == "psm":
        return f"{rel}: this is a PlayStation Mobile package, which inbox does not install"
    if not info.sfo:
        return f"{rel}: the param.sfo inside the package cannot be read"
    found = info.sfo.get("TITLE_ID")
    if found != title_id:
        return (
            f"{rel}: its param.sfo names {found or 'no title id'}, but the package header says "
            f"{title_id}"
        )
    sfo_content = info.sfo.get("CONTENT_ID")
    if sfo_content and sfo_content != info.content_id:
        return (
            f"{rel}: its param.sfo says content id {sfo_content}, but the package header says "
            f"{info.content_id}"
        )
    kind = inbox_vita.category_kind(info.sfo.get("CATEGORY"))
    if kind != "game":
        return inbox_vita._fail_kind(rel, kind, title_id)
    return None


@dataclass(frozen=True)
class ZrifTable:
    keys: Mapping[str, str]  # content id -> zRIF
    doubled: frozenset[str] = frozenset()  # content ids listed with different zRIFs


def parse_zrif_table(text: str) -> ZrifTable:
    """The zRIFs of a key file: `CONTENT_ID<TAB>zRIF` lines (blank lines and `#` comments
    skipped), or a TSV whose first line names the columns `Content ID` and `zRIF`, of which
    only those two are read. Rows whose zRIF is empty, `MISSING` or `NOT REQUIRED` are left
    out. A line that cannot be read raises `ValueError` naming its number, never its text."""
    lines = text.lstrip("﻿").splitlines()
    first = next((n for n, line in enumerate(lines) if line.strip()), None)
    columns = lines[first].rstrip("\r").split("\t") if first is not None else []
    rows: list[tuple[str, str]] = []
    if TSV_CONTENT_ID in columns and TSV_ZRIF in columns:
        assert first is not None
        cid_at, zrif_at = columns.index(TSV_CONTENT_ID), columns.index(TSV_ZRIF)
        for line in lines[first + 1 :]:
            cells = line.rstrip("\r").split("\t")
            if len(cells) > max(cid_at, zrif_at):
                rows.append((cells[cid_at].strip(), cells[zrif_at].strip()))
    else:
        for n, line in enumerate(lines, 1):
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            cells = [c.strip() for c in stripped.split("\t")]
            if len(cells) != 2 or not cells[0]:
                raise ValueError(f"line {n} is not CONTENT_ID<TAB>zRIF")
            rows.append((cells[0], cells[1]))
    keys: dict[str, str] = {}
    doubled: set[str] = set()
    for content_id, zrif in rows:
        if not content_id or zrif in NO_KEY:
            continue
        if keys.setdefault(content_id, zrif) != zrif:
            doubled.add(content_id)
    return ZrifTable({k: v for k, v in keys.items() if k not in doubled}, frozenset(doubled))


def decode_zrif(text: str) -> bytes:
    """The licence a zRIF holds, or `ValueError` with the reason (which never quotes it)."""
    if len(text) > MAX_ZRIF_CHARS:
        raise ValueError("is far too long for a zRIF")
    try:
        raw = base64.b64decode(text, validate=True)
    except (binascii.Error, ValueError):
        raise ValueError("is not base64, as a zRIF is") from None
    if len(raw) < 6 or raw[0] & 0x0F != 8 or not raw[1] & 0x20:
        raise ValueError("is not a zRIF (no zlib stream with a preset dictionary)")
    if struct.unpack(">I", raw[2:6])[0] != ZRIF_DICT_ID:
        raise ValueError("is not a zRIF (its zlib stream uses another dictionary)")
    limit = 2 * inbox_vita.LICENSE_SIZE + 1
    try:
        d = zlib.decompressobj(zdict=ZRIF_DICT)
        data = d.decompress(raw, limit)
        if not d.unconsumed_tail:
            data += d.flush()
    except zlib.error:
        raise ValueError("is damaged (its zlib stream does not decompress)") from None
    if d.unconsumed_tail or len(data) >= limit:
        raise ValueError("decodes to more than a licence holds")
    if not d.eof:
        raise ValueError("is cut short (its zlib stream does not end)")
    if len(data) != inbox_vita.LICENSE_SIZE:
        raise ValueError(
            f"decodes to {len(data)} bytes, not to the {inbox_vita.LICENSE_SIZE}-byte licence of "
            "a Vita game"
        )
    return data


def license_for(zrif: str, content_id: str) -> bytes:
    """The game's licence from its zRIF, which must be the licence of `content_id`."""
    data = decode_zrif(zrif)
    try:
        lic = inbox_vita.parse_license(data)
    except ValueError as exc:
        raise ValueError(f"decodes to a licence that cannot be used: {exc}") from None
    if lic.content_id != content_id:
        raise ValueError(f"is the licence of {lic.content_id}")
    return data


@dataclass(frozen=True)
class Launch:
    """How Vita3K runs in its own work folder: the program, the link to make first (`link`
    pointing at `target`), extra environment, and the portable folder it then uses."""

    program: str
    portable: Path
    link: Path | None = None
    target: str = ""
    env: Mapping[str, str] = field(default_factory=dict)


def _bundle_of(tool: str) -> tuple[str, str] | None:
    """(the app bundle, the binary's path inside it) for an app or a binary inside one."""
    trimmed = tool.rstrip("/")
    if trimmed.endswith(".app"):
        return trimmed, VITA3K_BINARY
    parts = Path(trimmed).parts
    if len(parts) >= 4 and parts[-4].endswith(".app") and parts[-3:-1] == ("Contents", "MacOS"):
        return str(Path(*parts[:-3])), "/".join(parts[-3:])
    return None


def launch_plan(tool: str, run: Path) -> Launch:
    """Vita3K on macOS takes its portable folder four levels above `Contents/Resources` of the
    bundle it runs from, and does not resolve a link to the bundle, so a link in `run` makes
    `run/portable` its folder. Elsewhere it takes the folder next to `$APPIMAGE`."""
    bundle = _bundle_of(tool)
    portable = run / PORTABLE
    if bundle:
        link = run / APP_LINK
        return Launch(str(link / bundle[1]), portable, link, bundle[0])
    return Launch(tool, portable, env={"APPIMAGE": str(run / APPIMAGE_NAME)})


def version_command(launch: Launch) -> list[str]:
    return [launch.program, "--version"]


def install_command(launch: Launch, pkg: Path, zrif: str) -> list[str]:
    return [launch.program, "--pkg", str(pkg), "--zrif", zrif]


def shown_command(argv: Sequence[str], zrif: str) -> str:
    """A command line to show, with the zRIF redacted wherever it appears."""
    return " ".join(REDACTED if a == zrif else scrub(a, (zrif,)) for a in argv)


def failure_line(output: str, zrif: str) -> str:
    """The line of Vita3K's output that best says why it failed, with the zRIF redacted."""
    lines = [scrub(x.strip(), (zrif,)) for x in output.splitlines() if x.strip()]
    for line in reversed(lines):
        low = line.lower()
        if "fail" in low or "invalid" in low or "error" in low:
            return line[:200]
    return lines[-1][:200] if lines else "no output"


@dataclass(frozen=True)
class Installed:
    """What Vita3K left in its `ux0`, as read from the disk."""

    folder: bool  # ux0/app/<TITLEID> is a folder
    eboot: bool = False
    sfo: Mapping[str, str | int] = field(default_factory=dict)
    pfs: bool = False  # an sce_pfs folder is left
    licence: bytes | None = None  # ux0/license/<TITLEID>/<CONTENT_ID>.rif


def judge_installed(rel: str, info: PkgInfo, licence: bytes, found: Installed) -> str | None:
    """Why what Vita3K left is not the game of the package, or None when it is."""
    title_id = info.title_id
    if not found.folder:
        return f"{rel}: Vita3K did not install it (nothing in ux0/app/{title_id})"
    if not found.eboot:
        return f"{rel}: what Vita3K installed has no eboot.bin"
    if not found.sfo:
        return f"{rel}: what Vita3K installed has no readable sce_sys/param.sfo"
    if found.sfo.get("TITLE_ID") != title_id:
        return (
            f"{rel}: what Vita3K installed names {found.sfo.get('TITLE_ID') or 'no title id'} in "
            f"its param.sfo, but the package header says {title_id}"
        )
    if inbox_vita.category_kind(found.sfo.get("CATEGORY")) != "game":
        return f"{rel}: what Vita3K installed is not a game, by its param.sfo"
    if found.sfo.get("CONTENT_ID") != info.content_id:
        return (
            f"{rel}: what Vita3K installed has content id "
            f"{found.sfo.get('CONTENT_ID') or 'none'} in its param.sfo, but the package "
            f"header says {info.content_id}"
        )
    if found.pfs:
        return f"{rel}: what Vita3K installed still holds {inbox_vita.PFS}"
    if found.licence is None:
        return f"{rel}: Vita3K wrote no licence for {info.content_id}"
    if found.licence != licence:
        return f"{rel}: the licence Vita3K wrote is not the one the zRIF decodes to"
    return None


# ------------------------------------------------------------------ disk

# Runs one Vita3K command: (argv, working folder, extra environment, timeout in seconds) ->
# (exit code, output). Tests replace `RUN`.
Runner = Callable[[Sequence[str], Path, Mapping[str, str], float], tuple[int, str]]


def run_vita3k(
    argv: Sequence[str], cwd: Path, env: Mapping[str, str], timeout: float
) -> tuple[int, str]:
    try:
        proc = subprocess.run(
            list(argv),
            cwd=cwd,
            env={**os.environ, **env},
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            errors="replace",
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return -1, f"Vita3K did not finish within {int(timeout)} seconds"
    return proc.returncode, (proc.stdout + proc.stderr).replace("\r", "\n")


RUN: Runner = run_vita3k


def validate(opts: Options) -> None:
    """A key file that is set must be there (a configuration error otherwise)."""
    path = opts.settings.vita_zrif
    if path and not os.path.isfile(path):
        raise InboxError(f"[inbox] vita_zrif: {path} is not a file")


@dataclass
class _Keys:
    table: ZrifTable | None = None
    error: str = ""


_KEYS: weakref.WeakKeyDictionary[Context, _Keys] = weakref.WeakKeyDictionary()


def _keys(ctx: Context) -> _Keys:
    """The key file, read once per run."""
    if ctx in _KEYS:
        return _KEYS[ctx]
    keys = _Keys()
    path = ctx.opts.settings.vita_zrif
    if path:
        try:
            if os.path.getsize(path) > MAX_TABLE_BYTES:
                raise ValueError(f"is larger than {MAX_TABLE_BYTES >> 20} MiB")
            text = Path(path).read_text(encoding="utf-8", errors="replace")
            keys.table = parse_zrif_table(text)
        except ValueError as exc:
            keys.error = f"[inbox] vita_zrif ({Path(path).name}): {exc}"
        except OSError as exc:
            keys.error = f"[inbox] vita_zrif ({Path(path).name}): {exc.strerror or exc}"
    _KEYS[ctx] = keys
    return keys


def _zrif(ctx: Context, rel: str, content_id: str) -> str:
    keys = _keys(ctx)
    if keys.error:
        raise ItemFailure(f"{rel}: {keys.error}")
    if keys.table is None:
        raise ItemFailure(f"{rel}: no zRIF for {content_id}: {NO_ZRIF_FILE}")
    name = Path(ctx.opts.settings.vita_zrif or "").name
    if content_id in keys.table.doubled:
        raise ItemFailure(f"{rel}: {name} lists {content_id} more than once, with different zRIFs")
    zrif = keys.table.keys.get(content_id)
    if zrif is None:
        raise ItemFailure(
            f"{rel}: no zRIF for {content_id} in {name} ([inbox] vita_zrif); add the game's "
            "zRIF there (warpzone never downloads keys)"
        )
    return zrif


def _read_licence(path: Path) -> bytes | None:
    if path.is_symlink() or not path.is_file():
        return None
    with path.open("rb") as f:
        return f.read(inbox_vita.LICENSE_SIZE + 1)


def _found(ux0: Path, info: PkgInfo) -> Installed:
    app = ux0 / "app" / info.title_id
    if app.is_symlink() or not app.is_dir():
        return Installed(False)
    return Installed(
        True,
        eboot=(app / inbox_vita.EBOOT).is_file(),
        sfo=inbox_ps4.read_sfo(app / inbox_vita.SFO),
        pfs=(app / inbox_vita.PFS).exists(),
        licence=_read_licence(ux0 / "license" / info.title_id / f"{info.content_id}.rif"),
    )


def _work_folder(ctx: Context, pkg: Path, rel: str) -> Path:
    """A new folder in the work folder for one Vita3K install."""
    ctx.work.mkdir(parents=True, exist_ok=True)
    need = pkg.stat().st_size
    if shutil.disk_usage(ctx.work).free < need * 1.1:
        raise ItemFailure(f"{rel}: not enough free space to install it ({need // 2**20} MiB)")
    return Path(tempfile.mkdtemp(prefix="vita3k-", dir=ctx.work))


def _install_with_vita3k(
    tool: str, run: Path, pkg: Path, rel: str, info: PkgInfo, zrif: str, licence: bytes
) -> tuple[Path, str]:
    """Run Vita3K in `run`; (the game folder it installed, the command shown)."""
    launch = launch_plan(tool, run)
    launch.portable.mkdir()
    if launch.link is not None:
        launch.link.symlink_to(launch.target)
    code, out = RUN(version_command(launch), run, launch.env, VERSION_TIMEOUT)
    if code != 0:
        raise ItemFailure(f"{rel}: Vita3K --version failed ({failure_line(out, zrif)})")
    if not (launch.portable / CONFIG).is_file():
        raise ItemFailure(
            f"{rel}: Vita3K did not use its work folder (no {PORTABLE}/{CONFIG} after "
            "--version), so it would install into its own data; nothing was installed"
        )
    argv = install_command(launch, pkg.absolute(), zrif)
    shown = shown_command([Path(argv[0]).name, "--pkg", pkg.name, "--zrif", zrif], zrif)
    code, out = RUN(argv, run, launch.env, INSTALL_TIMEOUT)
    ux0 = launch.portable / "fs" / "ux0"
    problem = judge_installed(rel, info, licence, _found(ux0, info))
    if code != 0 or problem:
        detail = f"exit code {code}, " if code != 0 else ""
        why = problem or f"{rel}: Vita3K failed"
        raise ItemFailure(f"{why} ({detail}{failure_line(out, zrif)})")
    return ux0 / "app" / info.title_id, shown


def process(ctx: Context, path: Path, rel: str, result: ItemResult) -> bool:
    """Handle one `.pkg` if it is a Vita package (False otherwise, nothing done)."""
    try:
        with path.open("rb") as f:
            info = read_pkg(f)
        if info is None:
            return False
        size = path.stat().st_size
    except OSError as exc:
        result.reasons.append(f"{rel}: {exc.strerror or exc}")
        return True
    result.notes.append(f"detected: Vita package: {info.content_id}")
    run: Path | None = None
    try:
        problem = pkg_problem(rel, info, size)
        if problem:
            raise ItemFailure(problem)
        zrif = _zrif(ctx, rel, info.content_id)
        try:
            licence = license_for(zrif, info.content_id)
        except ValueError as exc:
            raise ItemFailure(f"{rel}: the zRIF listed for {info.content_id} {exc}") from None
        title_id, lic = info.title_id, (info.content_id, licence)
        dest = ctx.roms.joinpath(*inbox_vita.APP.split("/")) / title_id
        note = ""
        root: Path | None = None
        if not dest.exists():
            tool = ctx.tools.vita3k
            if not tool:
                raise ItemFailure(f"{rel}: {NO_TOOL}")
            if not ctx.opts.apply:
                argv = ["Vita3K", "--pkg", path.name, "--zrif", zrif]
                note = f"would install it with Vita3K: {shown_command(argv, zrif)}"
            else:
                run = _work_folder(ctx, path, rel)
                root, shown = _install_with_vita3k(tool, run, path, rel, info, zrif, licence)
                note = f"installed with Vita3K: {shown}"
        unit = inbox_vita.install(ctx, root, rel, title_id, info.title, lic)
        if note:
            unit.notes.insert(1, note)
        result.units.append(unit)
        if unit.reason:
            result.reasons.append(unit.reason)
    except ItemFailure as exc:
        result.reasons.append(str(exc))
    except OSError as exc:
        result.reasons.append(f"{rel}: {exc.strerror or exc}")
    finally:
        if run is not None:
            shutil.rmtree(run, ignore_errors=True)
    return True
