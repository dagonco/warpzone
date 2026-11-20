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
    # Deletions the device still has to apply (`needDeletes`; `needItems` does not count them).
    # None: the answer did not say, so nothing is known about them.
    need_deletes: int | None = 0


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


def _count(value: object) -> int | None:
    """A count the hub gave, or None when it did not give a number."""
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return int(_number(value))


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
        need_deletes=_count(completion.get("needDeletes")),
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
    only_folders: bool | None = None,
) -> Progress:
    """The one state of a device, by the order of the table in the design: `offline`,
    `not-shared`, `paused`, `downloading` (bytes to fetch), `pending` (the whitelist was
    written less than `rescan_seconds` plus 60 s ago, so the device may not have applied it),
    `done` (completion 100). Anything else that is not complete (items and deletions to
    apply, no bytes) is `downloading` as well, unless `only_folders` is True: the hub listed
    what the device still needs (`remoteneed`) and all of it is directories, which a console
    that ignores them (EmuDeck makes empty system folders the whitelist does not take) never
    takes, so the hub counts them as needed for ever. Then, with no bytes and no deletions
    left, the device is `done`. None is a list the hub could not give: nothing is known."""
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
    if reading.completion >= 100 or (only_folders is True and reading.need_deletes == 0):
        return Progress(DONE, 100, *need)
    return Progress(DOWNLOADING, min(percent, 99), *need)


def wants_need_list(reading: Reading, current: Progress) -> bool:
    """Whether the hub should be asked what the device still needs (`remoteneed`) before the
    state is final: the device is `downloading` with no bytes to fetch and no deletions to
    apply, so only items are left, and they may all be folders it ignores."""
    return current.state == DOWNLOADING and reading.need_bytes == 0 and reading.need_deletes == 0


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
