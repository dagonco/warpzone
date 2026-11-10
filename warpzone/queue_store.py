"""The queue file on the hub: reading with a stamp, the backups and their rotation, the guarded
atomic write.

The decisions are in `queue.py` (pure); this module touches the disk. The guarded write is
`safefile.guarded_edit`, as for the whitelists. See "The file" in
development/design/play-queue.md.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from warpzone import queue as q
from warpzone import safefile
from warpzone import whitelist_store as store

KEEP_BACKUPS = 50


def queue_path(root: Path) -> Path:
    return root / "catalog" / "queue.json"


def backups_dir(root: Path) -> Path:
    return root / "catalog" / "queue-backups"


@dataclass(frozen=True)
class Loaded:
    """The queue file as read: the snapshot a write compares against (None when the file could
    not be read), what it means, its etag and its size in bytes."""

    snapshot: store.Snapshot | None
    reading: q.Reading
    etag: str
    size: int


def load(root: Path) -> Loaded:
    """Read and parse the queue file. A file that cannot be read, or is over 16 MiB, is
    `damaged` (never an exception): the answer names the cause, not the hub's path."""
    path = queue_path(root)
    try:
        size = path.stat().st_size
    except FileNotFoundError:
        size = 0
    except OSError as exc:
        return _damaged(f"the file cannot be read ({exc.strerror or type(exc).__name__})", 0)
    if size > q.FILE_LIMIT:
        return _damaged("the file is over 16 MiB", size)
    try:
        snapshot = store.read(path)
    except store.WhitelistIOError as exc:
        return _damaged(f"the file cannot be read ({exc.strerror})", 0)
    data = snapshot.data
    return Loaded(snapshot, q.parse(data), q.etag_of(data), len(data or b""))


def _damaged(message: str, size: int) -> Loaded:
    return Loaded(None, q.Reading("damaged", q.Queue(), message), "damaged", size)


def backup(root: Path, data: bytes, now: datetime) -> Path:
    """Copy `data` to `catalog/queue-backups/<UTC time>.json`; keep the 50 newest. Two backups
    in one instant (a request writes the queue twice) get names a microsecond apart."""
    directory = backups_dir(root)
    when = now.astimezone(UTC)
    while True:
        target = directory / (when.strftime("%Y%m%dT%H%M%S.%fZ") + ".json")
        if not target.exists():
            break
        when += timedelta(microseconds=1)
    safefile.write_file(target, data, 0o644)
    for old in sorted(directory.glob("*.json"))[:-KEEP_BACKUPS]:
        old.unlink(missing_ok=True)
    return target


def replace(root: Path, etag: str, data: bytes, now: datetime) -> Path | None:
    """The queue's write: read again, compare the `etag`, back up the file being replaced (when
    there is one), write unless it changed meanwhile. Returns the backup's path.

    Raises `store.Stale` when the file differs from `etag` or changes during the write, and
    `store.NoLibrary` when the hub's `catalog/` folder is not there (an unmounted or wrong hub
    root): nothing is created. The caller holds the lock that serializes writes."""
    loaded = load(root)
    snapshot = loaded.snapshot
    if snapshot is None or loaded.etag != etag:
        raise store.Stale("queue")
    path = snapshot.path
    if snapshot.stamp is None and not path.parent.is_dir():
        raise store.NoLibrary(
            "the hub's catalog/ folder is not there; is the hub root mounted and right?"
        )
    saved: list[Path] = []
    failing = path

    def save() -> None:
        nonlocal failing
        failing = backups_dir(root)
        saved.append(backup(root, snapshot.data or b"", now))
        failing = path

    try:
        edit = safefile.guarded_edit(
            path,
            data,
            expect=snapshot.stamp,
            busy=lambda: False,
            backup=save if snapshot.data is not None else None,
        )
    except OSError as exc:
        raise store.WhitelistIOError("write", failing, exc) from exc
    if edit is not safefile.Edit.WRITTEN:
        raise store.Stale("queue")
    return saved[0] if saved else None
