"""The disk report on a device (write) and on the hub (read).

The decisions are in `diskstatus`. See development/design/library-ui.md, "Disk space".
"""

from __future__ import annotations

import contextlib
import shutil
import time
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Any

from warpzone import diskstatus, playstate_device, safefile
from warpzone.paths import expand_home
from warpzone.profiles import Profile

Say = Callable[[str], None]


def publish(
    device: str,
    profile: Profile,
    home: Path,
    *,
    say: Say,
    warn: Say,
    dry_run: bool = False,
    now: Callable[[], float] = time.time,
    usage: Callable[[Path], tuple[int, int, int]] = shutil.disk_usage,
) -> bool:
    """Write this device's disk report when it moved enough; True when it was written.

    Nothing happens without the play state folder (Syncthing has not set it up, or a card is
    not mounted) or the library folder's marker, because the disk of a path that is not the
    library would be the wrong one. A failure is a warning: the report never stops the job it
    rides on. With `dry_run` nothing is written.
    """
    where = playstate_device.folder(profile, home)
    raw = profile.paths.get("library")
    if where is None or raw is None:
        return False
    library = Path(expand_home(raw, home))
    if not (library / ".stfolder").exists():
        return False
    path = where / diskstatus.file_name(device)
    try:
        total, _, free = usage(library)
        new = diskstatus.measure(total, free, int(now()))
        if new is None:
            warn(f"disk: {library} reports no usable size, so no disk report was written")
            return False
        try:
            current: safefile.FileStamp | None = safefile.stamp(path)
            previous = diskstatus.parse(path.read_bytes(), device)
        except FileNotFoundError:
            current, previous = None, None
        if not diskstatus.should_write(previous, new):
            return False
        if dry_run:
            say(f"disk: dry-run: would write {path}")
            return False
        if not safefile.atomic_write(path, diskstatus.render(device, new), expect=current):
            warn(f"{path} changed while it was being written, so it was left alone")
            return False
    except OSError as exc:
        warn(f"disk: cannot write {path} ({exc}), so the hub keeps the last report")
        return False
    say(f"disk: wrote {path}")
    return True


def read_all(folder: Path, devices: Iterable[str], now: int) -> dict[str, dict[str, Any]]:
    """The `disk` object of each device from the play state folder on the hub. A missing,
    unreadable, malformed or stale file gives null fields; this never raises."""
    found: dict[str, dict[str, Any]] = {}
    for name in devices:
        disk = None
        with contextlib.suppress(OSError):
            disk = diskstatus.parse((folder / diskstatus.file_name(name)).read_bytes(), name)
        found[name] = diskstatus.api_disk(disk, now)
    return found
