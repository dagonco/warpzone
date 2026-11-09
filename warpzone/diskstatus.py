"""The disk a console reports to the hub: what to write, how to read it back (pure).

A device writes `<device>.status.json` into the play state folder with the size and free space
of the filesystem that holds its library; the hub reads it for the library UI. See
development/design/library-ui.md, "Disk space".
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

VERSION = 1
SUFFIX = ".status.json"
MIB = 1 << 20
# Free space that moved less than this share of the disk is not worth a write (and a sync).
CHANGE_PERCENT = 1
# A file is rewritten at least this often, so the hub can tell a console that is quiet from
# one that stopped reporting.
HEARTBEAT_SECONDS = 24 * 3600
# The hub shows nothing for a report older than this, or dated further ahead than a clock may
# be wrong by.
STALE_SECONDS = 7 * 24 * 3600
FUTURE_SLACK = 24 * 3600


@dataclass(frozen=True)
class Disk:
    """Bytes of a filesystem, whole MiB, and the epoch second they were measured at.
    `library` (bytes the library folder uses) is optional: devices do not measure it."""

    total: int
    free: int
    at: int
    library: int | None = None


def file_name(device: str) -> str:
    return f"{device}{SUFFIX}"


def measure(total: int, free: int, now: int) -> Disk | None:
    """A report from `shutil.disk_usage` numbers, rounded down to whole MiB.
    None when they cannot be a disk (no size, or more free than total)."""
    total, free = total // MIB * MIB, free // MIB * MIB
    if total <= 0 or free < 0 or free > total:
        return None
    return Disk(total, free, now)


def should_write(previous: Disk | None, new: Disk) -> bool:
    """Whether `new` is worth a write: no usable report yet, another size, free space that
    moved by CHANGE_PERCENT of the disk or more, or a report older than the heartbeat (or
    dated in the future, which a wrong clock leaves behind)."""
    if previous is None or previous.total != new.total:
        return True
    if abs(previous.free - new.free) * 100 >= CHANGE_PERCENT * new.total:
        return True
    return not 0 <= new.at - previous.at < HEARTBEAT_SECONDS


def render(device: str, disk: Disk) -> bytes:
    """The file's bytes: one JSON object, keys in a fixed order."""
    body: dict[str, Any] = {
        "v": VERSION,
        "device": device,
        "at": disk.at,
        "total": disk.total,
        "free": disk.free,
    }
    if disk.library is not None:
        body["library"] = disk.library
    return (json.dumps(body, separators=(",", ":")) + "\n").encode("utf-8")


def _count(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def parse(data: bytes, device: str) -> Disk | None:
    """The report in a file, or None when it is not one this version understands: not
    UTF-8 or JSON, another version or device, a missing number, free above total."""
    try:
        body = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return None
    if not isinstance(body, dict) or body.get("v") != VERSION or body.get("device") != device:
        return None
    total, free, at = _count(body.get("total")), _count(body.get("free")), _count(body.get("at"))
    if total is None or free is None or at is None or total == 0 or free > total:
        return None
    library = _count(body.get("library"))
    if library is not None and library > total:
        library = None
    return Disk(total, free, at, library)


def stale(disk: Disk, now: int) -> bool:
    """Whether a report is too old to show, or dated beyond what a wrong clock explains."""
    return not -FUTURE_SLACK <= now - disk.at <= STALE_SECONDS


def api_disk(disk: Disk | None, now: int) -> dict[str, Any]:
    """The `disk` object of `GET /api/v1/devices`; every field is null without a fresh report."""
    if disk is None or stale(disk, now):
        return {"total": None, "free": None, "library": None, "at": None}
    at = datetime.fromtimestamp(disk.at, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    return {"total": disk.total, "free": disk.free, "library": disk.library, "at": at}
