"""The disk side of the unattended inbox: the walk of an item, the state file, the run records
under `catalog/inbox/runs/`, the work folder and the free space.

A record is written atomically (a name that exists is never replaced: the id gets `-2`, `-3`),
mode 0644, and the writer then deletes the records beyond the newest 50. The state file is
changed by read, modify, write: the changes are pure functions (`inbox_unattended.update`), and
a file that changed between the read and the write is read again and the same changes applied
to it. The decisions (the id, the text, what to purge, what is stale) are pure and live in
`inbox_unattended.py`. See development/design/inbox-unattended.md.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import stat
import time
from collections.abc import Callable, Mapping
from datetime import datetime
from pathlib import Path
from typing import Any

from warpzone import safefile
from warpzone.inbox_core import InboxError
from warpzone.inbox_unattended import (
    RECORDS_KEPT,
    STATE_FILE,
    Change,
    Listing,
    Stat,
    State,
    memory_fs,
    parse_state,
    record_id,
    render_record,
    render_state,
    select_records_purge,
    stale_work_dirs,
    update,
)

CATALOG_DIR = "catalog"
INBOX_DIR = "inbox"
RUNS_DIR = "runs"
STATE_TRIES = 3  # reads and writes of the state file before giving up
RETRY_PAUSE = 0.05  # seconds, times the number of the retry, between two tries
MOUNTINFO = "/proc/self/mountinfo"
NICE = 10  # how much lower the run's own CPU priority is made; the tools inherit it
MAX_SAME_SECOND = 1000  # a loop that cannot find a free id is a bug, not a busy hub


def inbox_dir(root: Path) -> Path:
    """`catalog/inbox/` of the hub: outside every Syncthing folder."""
    return root / CATALOG_DIR / INBOX_DIR


def runs_dir(root: Path) -> Path:
    return inbox_dir(root) / RUNS_DIR


def write_record(
    root: Path, started: datetime, build: Callable[[str], Mapping[str, Any]]
) -> Mapping[str, Any]:
    """Write `catalog/inbox/runs/<id>.json` and return the record written. `build` gets the id
    (the start in UTC, with `-2`, `-3` when a record of that second exists) and returns the
    record, which holds it."""
    folder = runs_dir(root)
    for n in range(1, MAX_SAME_SECOND):
        record = build(record_id(started, n))
        target = folder / f"{record['id']}.json"
        # A name that is not valid text (undecodable bytes) holds a lone surrogate: it is
        # written as the JSON escape `\\udcff`, which parses back to the same name.
        data = render_record(record).encode("utf-8", errors="backslashreplace")
        if safefile.atomic_write(target, data, expect=None):
            return record
    raise OSError(f"no free record name in {RUNS_DIR}/ for {record_id(started)}")


def purge_records(root: Path, keep: int = RECORDS_KEPT) -> list[str]:
    """Delete the records older than the newest `keep`; returns the names deleted. A record
    that cannot be deleted stays (the next run tries again)."""
    folder = runs_dir(root)
    if not folder.is_dir():
        return []
    gone = []
    for name in select_records_purge((p.name for p in folder.iterdir()), keep):
        try:
            (folder / name).unlink()
        except OSError:
            continue
        gone.append(name)
    return gone


# ------------------------------------------------------------------ the state file


def state_path(root: Path) -> Path:
    return inbox_dir(root) / STATE_FILE


def _read_with_stamp(path: Path) -> tuple[str | None, safefile.FileStamp | None]:
    """The text of the file and the stamp of those very bytes; (None, None) when it is missing."""
    try:
        with open(path, "rb") as fh:
            info = os.fstat(fh.fileno())
            data = fh.read()
    except FileNotFoundError:
        return None, None
    stamp = safefile.FileStamp(info.st_size, info.st_mtime_ns, hashlib.sha256(data).hexdigest())
    return data.decode("utf-8", errors="replace"), stamp


def read_state(root: Path) -> tuple[State, str | None]:
    """The state and a warning (a file that cannot be parsed, a damaged part), or an empty state
    when there is no file."""
    try:
        text, _ = _read_with_stamp(state_path(root))
    except OSError as exc:
        raise InboxError(f"cannot read {_SHOWN}: {exc.strerror or exc}") from exc
    return parse_state(text)


def update_state(root: Path, *changes: Change) -> tuple[State, list[str]]:
    """Apply the changes to the state file: read it, apply them, write it against the stamp of
    what was read. A file that changed in between is read again and the same changes are applied
    to it (three tries, then a warning). Returns the state written (or, after the warning, the
    one that could not be) and the warnings; an OSError is an `InboxError`."""
    state, warnings, _ = update_state_checked(root, *changes)
    return state, warnings


def update_state_checked(root: Path, *changes: Change) -> tuple[State, list[str], bool]:
    """`update_state` that also says whether the file was written. Concurrent writers share the
    temporary name of `safefile.atomic_write`, so a collision on it (the file exists, or was
    taken from under this writer) is retried like a changed file; any other OSError, or a
    collision on the last try, is an `InboxError`."""
    path = state_path(root)
    warnings: list[str] = []
    new = State()
    collision: OSError | None = None
    for attempt in range(STATE_TRIES):
        if attempt:
            time.sleep(RETRY_PAUSE * attempt)
        collision = None
        try:
            text, stamp = _read_with_stamp(path)
            old, warning = parse_state(text)
            new = update(old, *changes)
            if warning and warning not in warnings:
                warnings.append(warning)
            if safefile.atomic_write(path, render_state(new).encode("utf-8"), expect=stamp):
                return new, warnings, True
        except (FileNotFoundError, FileExistsError) as exc:
            collision = exc
        except OSError as exc:
            raise InboxError(f"cannot write {_SHOWN}: {exc.strerror or exc}") from exc
    if collision is not None:
        raise InboxError(f"cannot write {_SHOWN}: {collision.strerror or collision}")
    warnings.append(f"{_SHOWN} was not written: it kept changing")
    return new, warnings, False


_SHOWN = f"{CATALOG_DIR}/{INBOX_DIR}/{STATE_FILE}"


# ------------------------------------------------------------------ the walk


def _kind(mode: int) -> str:
    if stat.S_ISLNK(mode):
        return "l"
    return "d" if stat.S_ISDIR(mode) else "f"


def walk(item: Path) -> list[Stat] | None:
    """What `fingerprint` needs of one item: the item and every entry under it, by `lstat`
    (a link is a line of its own and is never followed; hidden files count; nothing is read).
    None when the item is gone. An entry that vanishes during the walk (a copier's temporary
    file) is skipped: the change shows in the next fingerprint anyway."""
    try:
        top = os.lstat(item)
    except FileNotFoundError:
        return None
    entries = [Stat(_kind(top.st_mode), top.st_size, top.st_mtime_ns, ".")]
    if not stat.S_ISDIR(top.st_mode):
        return entries
    pending = [(item, "")]
    while pending:
        folder, rel = pending.pop()
        try:
            with os.scandir(folder) as listing:
                found = list(listing)
        except FileNotFoundError:
            continue
        for entry in found:
            try:
                info = entry.stat(follow_symlinks=False)
            except FileNotFoundError:
                continue
            path = f"{rel}{entry.name}"
            kind = _kind(info.st_mode)
            entries.append(Stat(kind, info.st_size, info.st_mtime_ns, path))
            if kind == "d":
                pending.append((Path(entry.path), path + "/"))
    return entries


def read_sheet(path: Path, limit: int = 1 << 20) -> str:
    """The text of a cue or gdi sheet at the top of the inbox (at most `limit` bytes)."""
    with open(path, "rb") as fh:
        return fh.read(limit).decode("utf-8", errors="replace")


# ------------------------------------------------------------------ the machine


def free_bytes(path: Path) -> int:
    """The free space of the filesystem that holds `path`."""
    return shutil.disk_usage(path).free


def lower_priority(nice: Callable[[int], int] | None = None) -> str | None:
    """Lower this run's CPU priority (the tools it starts inherit it). None when done or when
    the system has no such call; otherwise why it could not be."""
    call = nice or getattr(os, "nice", None)
    if call is None:
        return None
    try:
        call(NICE)
    except OSError as exc:
        return exc.strerror or str(exc)
    return None


def on_memory(path: Path, mountinfo: str = MOUNTINFO) -> bool:
    """Whether `path` is on `tmpfs` or `ramfs`; false where there is no `/proc/self/mountinfo`."""
    try:
        text = Path(mountinfo).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False
    return memory_fs(text, os.path.realpath(path))


def clean_work_dir(work_dir: Path, now: float) -> list[str]:
    """Delete the `warpzone-inbox-*` folders of `work_dir` older than 24 hours (a killed run's
    leftovers; they may hold a Vita licence). Never follows a link. Returns the names deleted;
    one that cannot be deleted stays."""
    try:
        names = list(os.scandir(work_dir))
    except OSError:
        return []
    listing = []
    for entry in names:
        try:
            info = entry.stat(follow_symlinks=False)
        except OSError:
            continue
        listing.append(
            Listing(
                entry.name, info.st_mtime, stat.S_ISDIR(info.st_mode), stat.S_ISLNK(info.st_mode)
            )
        )
    gone = []
    for name in stale_work_dirs(listing, now):
        shutil.rmtree(work_dir / name, ignore_errors=True)
        if not (work_dir / name).exists():
            gone.append(name)
    return gone
