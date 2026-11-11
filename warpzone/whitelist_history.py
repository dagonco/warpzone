"""The history of a device's whitelist: its backups listed, compared and checked (pure).

Every write of a whitelist keeps the file it replaced in `catalog/whitelists/<device>/`
(`whitelist_store.backup`). Nothing here touches the disk or the network: the names and bytes
come in, the decisions go out. `whitelist_store.py` reads the files and writes the restore; the
library UI's API (`library_api.py`) and `warpzone whitelist restore` show what is computed here.
See "History and restore" in development/design/library-ui.md.
"""

from __future__ import annotations

import difflib
import re
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime

from warpzone import progress
from warpzone import whitelist as wl

SUFFIX = ".stignore"
_NAME = re.compile(r"^\d{8}T\d{6}\.\d{6}Z\.stignore$")
_COMMENT_OR_FIXED = ("//", "#")


class UnknownBackup(Exception):
    """A backup reference that names no backup, or more than one."""


def is_backup_name(name: str) -> bool:
    """Whether `name` is exactly what `whitelist_store.backup` names a file (so it can be joined
    to the backups folder without leaving it)."""
    if not _NAME.match(name):
        return False
    return backup_time(name) is not None


def backup_time(name: str) -> datetime | None:
    """When the backup was taken, from its name (UTC); None for a name that is not one."""
    try:
        when = datetime.strptime(name.removesuffix(SUFFIX), progress.BACKUP_TIME)
    except ValueError:
        return None
    return when.replace(tzinfo=UTC)


def newest_first(names: Iterable[str]) -> list[str]:
    """The backup names among `names`, newest first (other files in the folder are not backups)."""
    return sorted((n for n in names if is_backup_name(n)), reverse=True)


def resolve(names: Iterable[str], ref: str) -> str:
    """The backup `ref` names: its full name, the name without `.stignore`, or the start of the
    name when that is enough to tell it from the others (`20261106T1000`).

    Raises `UnknownBackup` with a message for the user when none or several match.
    """
    known = newest_first(names)
    wanted = ref.strip()
    for exact in (wanted, wanted + SUFFIX):
        if exact in known:
            return exact
    matches = [n for n in known if wanted and n.startswith(wanted)]
    if len(matches) == 1:
        return matches[0]
    if not matches:
        raise UnknownBackup(f"no backup {ref!r}; run the command without a backup to list them")
    shown = ", ".join(n.removesuffix(SUFFIX) for n in matches[:3])
    raise UnknownBackup(f"{ref!r} matches {len(matches)} backups ({shown}, ...); give more of it")


def stem(name: str) -> str:
    """The name of a backup without `.stignore`, as the command line shows and accepts it."""
    return name.removesuffix(SUFFIX)


def moment(name: str) -> str:
    """The time of a backup for people: `2026-11-06 10:00:00 UTC` (empty for a bad name)."""
    when = backup_time(name)
    return when.strftime("%Y-%m-%d %H:%M:%S UTC") if when else ""


def text_of(data: bytes | None) -> str:
    """The file as text for comparing and showing (a byte that is not UTF-8 is replaced; the
    restore itself writes the exact bytes)."""
    return "" if data is None else data.decode("utf-8", errors="replace")


# --------------------------------------------------------------------------- comparing


def lines_of(text: str) -> tuple[frozenset[str], frozenset[str]]:
    """The paths a whitelist file names: (those it turns on with `!`, those it excludes).

    Comments, `#include`, the final `*` and any line that is not an anchored path are not
    paths. A path is shown unescaped when it can be. The lines of a managed file's
    `// Folders` section (from that comment up to the `*`) are derived, not paths turned on
    or off, so they are skipped.
    """
    on: set[str] = set()
    off: set[str] = set()
    in_folders = False
    for raw in text.splitlines():
        line = raw.strip()
        if line == "// Folders":
            in_folders = True
        elif line == "*":
            in_folders = False
        if in_folders or not line or line.startswith(_COMMENT_OR_FIXED) or line == "*":
            continue
        target = on if line.startswith("!") else off
        pattern = line.removeprefix("!")
        if not pattern.startswith("/"):
            continue
        try:
            target.add(wl.unescape(pattern))
        except ValueError:
            target.add(pattern)
    return frozenset(on), frozenset(off)


@dataclass(frozen=True)
class Delta:
    """What writing a backup over the current file does to the paths the file names.

    `included`: paths that become on (a `!` line that is added, or an exclusion that goes), so
    the console downloads them again. `excluded`: paths that become off. Compared as lines, not
    as files: a path under a folder that is on is not looked up.
    """

    included: tuple[str, ...] = ()
    excluded: tuple[str, ...] = ()

    @property
    def empty(self) -> bool:
        return not self.included and not self.excluded


def delta(current: str, backup: str) -> Delta:
    """The paths `backup` turns on and off compared with `current`."""
    cur_on, cur_off = lines_of(current)
    new_on, new_off = lines_of(backup)
    return Delta(
        included=tuple(sorted((new_on - cur_on) | (cur_off - new_off))),
        excluded=tuple(sorted((cur_on - new_on) | (new_off - cur_off))),
    )


def unified_diff(current: str, backup: str, name: str) -> list[str]:
    """The unified diff from the current file to the backup, one string per line (no newline)."""
    return list(
        difflib.unified_diff(
            current.splitlines(),
            backup.splitlines(),
            fromfile="current",
            tofile=stem(name),
            lineterm="",
        )
    )


@dataclass(frozen=True)
class Entry:
    """One backup as listed: when, whether it is the current file again, and what it changes."""

    name: str
    when: datetime
    size: int
    empty: bool  # taken when the whitelist did not exist: it holds nothing
    same: bool  # the bytes of the current file
    delta: Delta


def entry(name: str, data: bytes, current: bytes | None) -> Entry:
    """The listing entry of the backup `name` holding `data`, against the current file's bytes
    (None: there is no current file)."""
    when = backup_time(name)
    if when is None:
        raise ValueError(f"{name!r} is not a backup name")
    return Entry(
        name=name,
        when=when,
        size=len(data),
        empty=not data,
        same=data == (current or b""),
        delta=delta(text_of(current), text_of(data)),
    )


def refusal(item: Entry) -> str | None:
    """Why the backup cannot be restored, or None. An empty backup is the file as it was before
    the whitelist existed; written back it would be an empty ignore file, which lets everything
    through to the console."""
    if item.empty:
        return (
            "that backup is empty (the whitelist did not exist yet when it was taken); an empty "
            "whitelist would let every file through"
        )
    return None


def describe(item: Entry) -> str:
    """A short line for a listing: how the backup differs from the current file."""
    if item.empty:
        return "empty (taken before the whitelist existed), cannot be restored"
    if item.same:
        return "same as the current whitelist"
    if item.delta.empty:
        return "differs only in comments or layout"
    parts = []
    if item.delta.included:
        parts.append(f"{len(item.delta.included)} path(s) back on")
    if item.delta.excluded:
        parts.append(f"{len(item.delta.excluded)} path(s) off")
    return ", ".join(parts)


def warning(change: Delta) -> str | None:
    """The sentence to show before a restore that turns paths back on: the console will download
    them again (when the library still holds them), and it may be games it no longer has."""
    if not change.included:
        return None
    return (
        f"restoring turns {len(change.included)} path(s) back on: the console will download them "
        "again, including games or systems you removed from it since"
    )
