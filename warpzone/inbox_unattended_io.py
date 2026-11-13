"""The disk side of the unattended inbox: the run records under `catalog/inbox/runs/`.

A record is written atomically (a name that exists is never replaced: the id gets `-2`, `-3`),
mode 0644, and the writer then deletes the records beyond the newest 50. The decisions (the id,
the text, what to purge) are pure and live in `inbox_unattended.py`. See
development/design/inbox-unattended.md ("The run record and the library UI").
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import datetime
from pathlib import Path
from typing import Any

from warpzone import safefile
from warpzone.inbox_unattended import (
    RECORDS_KEPT,
    record_id,
    render_record,
    select_records_purge,
)

CATALOG_DIR = "catalog"
INBOX_DIR = "inbox"
RUNS_DIR = "runs"
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
        if safefile.atomic_write(target, render_record(record).encode(), expect=None):
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
