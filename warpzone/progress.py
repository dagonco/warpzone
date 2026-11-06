"""Per-device progress from the hub's Syncthing (pure).

`GET /api/v1/progress` reads `GET /rest/system/connections` and `GET /rest/db/completion` of the
hub; this module turns those answers into one state per device. Nothing here touches the
network or the disk (`serve.py` asks the hub). See "Progress" in development/design/library-ui.md.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

OFFLINE, NOT_SHARED, PAUSED, PENDING, DOWNLOADING, DONE = (
    "offline",
    "not-shared",
    "paused",
    "pending",
    "downloading",
    "done",
)
STATES = (OFFLINE, NOT_SHARED, PAUSED, PENDING, DOWNLOADING, DONE)
PENDING_GRACE_SECONDS = (
    60  # added to the device's `rescan_seconds` before a write counts as applied
)
BACKUP_TIME = "%Y%m%dT%H%M%S.%fZ"  # the names `whitelist_store.backup` gives


@dataclass(frozen=True)
class Reading:
    """What the hub says about one device and the library folder."""

    connected: bool
    completion: float = 0.0
    need_bytes: int = 0
    need_items: int = 0
    remote_state: str = "valid"
    shared: bool = True  # False: the hub does not know the folder as shared with the device


@dataclass(frozen=True)
class Progress:
    state: str
    percent: int | None = None  # the floor of `completion`, for `downloading` and `done`
    need_bytes: int = 0
    need_items: int = 0


def _number(value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return 0.0
    return float(value) if math.isfinite(value) else 0.0


def parse(
    connection: Mapping[str, Any] | None,
    completion: Mapping[str, Any] | None,
) -> Reading:
    """A `Reading` from the hub's `connections[<id>]` entry and its `db/completion` answer.

    A device missing from the connections is not connected. `completion` is None when the hub
    answered that the folder is not shared with the device."""
    connected = bool(connection and connection.get("connected") is True)
    if completion is None:
        return Reading(connected, shared=False)
    remote = completion.get("remoteState")
    return Reading(
        connected,
        completion=_number(completion.get("completion")),
        need_bytes=int(_number(completion.get("needBytes"))),
        need_items=int(_number(completion.get("needItems"))),
        remote_state=remote if isinstance(remote, str) else "valid",
    )


def written_at(backup_names: Iterable[str]) -> datetime | None:
    """When the device's whitelist was last written: the newest backup name in
    `catalog/whitelists/<device>/` (the server keeps no other state)."""
    newest: datetime | None = None
    for name in backup_names:
        stem = name.removesuffix(".stignore")
        try:
            when = datetime.strptime(stem, BACKUP_TIME).replace(tzinfo=UTC)
        except ValueError:
            continue
        if newest is None or when > newest:
            newest = when
    return newest


def state(
    reading: Reading,
    *,
    wrote_at: datetime | None,
    rescan_seconds: int,
    now: datetime,
) -> Progress:
    """The one state of a device, by the order of the table in the design: `offline`,
    `not-shared`, `paused`, `downloading` (bytes to fetch), `pending` (the whitelist was
    written less than `rescan_seconds` plus 60 s ago, so the device may not have applied it),
    `done` (completion 100). Anything else that is not complete (items and deletions to
    apply, no bytes) is `downloading` as well."""
    percent = max(0, min(100, math.floor(reading.completion)))
    need = (reading.need_bytes, reading.need_items)
    if not reading.connected:
        return Progress(OFFLINE, None, *need)
    if not reading.shared or reading.remote_state == "notSharing":
        return Progress(NOT_SHARED, None, *need)
    if reading.remote_state == "paused":
        return Progress(PAUSED, None, *need)
    if reading.need_bytes > 0:
        return Progress(DOWNLOADING, min(percent, 99), *need)
    grace = timedelta(seconds=rescan_seconds + PENDING_GRACE_SECONDS)
    if wrote_at is not None and timedelta(0) <= now - wrote_at < grace:
        return Progress(PENDING, None, *need)
    if reading.completion >= 100:
        return Progress(DONE, 100, *need)
    return Progress(DOWNLOADING, min(percent, 99), *need)


def entry(device: str, progress: Progress, checked: datetime) -> dict[str, Any]:
    """The device's item of the `GET /api/v1/progress` answer."""
    return {
        "device": device,
        "state": progress.state,
        "percent": progress.percent,
        "need_bytes": progress.need_bytes,
        "need_items": progress.need_items,
        "checked": checked.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
