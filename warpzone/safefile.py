"""Safe writes: atomic replacement, stamps, the guarded edit and the JSON cache.

Everything that writes a file warpzone does not fully own goes through here. Nothing in this
module touches the network or Syncthing. See "Device state" in
development/design/emulator-settings.md.
"""

from __future__ import annotations

import enum
import hashlib
import json
import os
import stat as stat_mod
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

TMP_SUFFIX = ".wz-tmp"


def fsync_dir(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def write_file(path: Path, data: bytes, mode: int = 0o600) -> None:
    """Replace `path` atomically with `data` and `mode`, creating its directories.

    The data goes to a temporary `.<name>.wz-tmp` beside the file, is synced, and is moved
    over the target, so a reader sees the old file or the new one and never half of it.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}{TMP_SUFFIX}")
    tmp.unlink(missing_ok=True)
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
    try:
        with os.fdopen(fd, "wb") as fh:
            os.fchmod(fh.fileno(), mode)
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    fsync_dir(path.parent)


def write_script(path: Path, text: str, mode: int = 0o755) -> None:
    """Write a text file (executable by default) atomically, creating its directories."""
    write_file(path, text.encode(), mode)


@dataclass(frozen=True)
class FileStamp:
    size: int
    mtime_ns: int
    sha256: str


def stamp(path: Path) -> FileStamp:
    """Size, modification time and sha256 of the file (a symlink is followed)."""
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        st = os.fstat(fh.fileno())
        for chunk in iter(lambda: fh.read(1 << 16), b""):
            digest.update(chunk)
    return FileStamp(st.st_size, st.st_mtime_ns, digest.hexdigest())


def atomic_write(
    path: Path, data: bytes, *, expect: FileStamp | None, mtime_ns: int | None = None
) -> bool:
    """Replace `path` with `data` unless it changed since `expect` was taken.

    `expect=None` means the file must not exist (it is created, with its directories).
    Returns False, leaving everything as it was, when the file changed, vanished or
    appeared. A symlink is written through to its target and stays a link. The
    replacement keeps the original mode; a temporary `.<name>.wz-tmp` beside it is
    synced, compared against `expect` one last time, then moved over the target.
    With `mtime_ns` the new file carries that modification time instead of now.
    """
    target = Path(os.path.realpath(path))
    if expect is None:
        if os.path.lexists(target):
            return False
        mode = 0o644
        target.parent.mkdir(parents=True, exist_ok=True)
    else:
        try:
            mode = stat_mod.S_IMODE(target.stat().st_mode)
        except FileNotFoundError:
            return False
    tmp = target.with_name(f".{target.name}{TMP_SUFFIX}")
    tmp.unlink(missing_ok=True)
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        with os.fdopen(fd, "wb") as fh:
            os.fchmod(fh.fileno(), mode)
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        if mtime_ns is not None:
            os.utime(tmp, ns=(mtime_ns, mtime_ns))
        if expect is None:
            if os.path.lexists(target):
                tmp.unlink(missing_ok=True)
                return False
        else:
            try:
                unchanged = stamp(target) == expect
            except FileNotFoundError:
                unchanged = False
            if not unchanged:
                tmp.unlink(missing_ok=True)
                return False
        os.replace(tmp, target)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    fsync_dir(target.parent)
    return True


class Edit(enum.Enum):
    """How a `guarded_edit` ended."""

    WRITTEN = "written"
    BUSY = "busy"  # the program that owns the file started: nothing was written
    CHANGED = "changed"  # the file changed since it was read: nothing was written


def guarded_edit(
    path: Path,
    data: bytes | Callable[[], bytes],
    *,
    expect: FileStamp | None,
    busy: Callable[[], bool],
    backup: Callable[[], object] | None = None,
    mtime_ns: int | None = None,
) -> Edit:
    """Edit a file another program may also touch, re-checking right before writing.

    `expect` is the stamp taken when the file was read (None: it must not exist yet).
    The steps, in order: `backup()` (when given), `busy()` once more (the backup takes
    time, and the program may have started meanwhile; True refuses, and `busy` is the one
    that tells the user why), then the atomic write, which refuses when the file changed
    since `expect`. `data` may be a function, called after the `busy` re-check; what it
    raises propagates. The caller does the first checks and the reporting.
    """
    if backup is not None:
        backup()
    if busy():
        return Edit.BUSY
    payload = data() if callable(data) else data
    written = atomic_write(path, payload, expect=expect, mtime_ns=mtime_ns)
    return Edit.WRITTEN if written else Edit.CHANGED


def read_json_cache(path: Path) -> Any | None:
    """The JSON a cache file holds, or None when it is missing, unreadable or not JSON."""
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def write_json_cache(path: Path, value: object) -> None:
    """Write a cache file (compact JSON) beside its final name and move it into place.

    A cache that cannot be written only costs time, so an `OSError` is swallowed.
    """
    tmp = path.with_name(path.name + ".tmp")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_text(json.dumps(value, separators=(",", ":")), encoding="utf-8")
        os.replace(tmp, path)
    except OSError:
        pass
