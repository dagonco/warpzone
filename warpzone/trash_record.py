"""The record of the games the hub deleted from the library UI: the file's format, and the
decisions about it. Pure: no disk, no network. See "The record of deleted games" in
development/design/library-ui.md.

A console reads the record to remove the copy of a game it had turned off before the hub deleted
it; the file is `library/patterns-_deleted.stignore`, which every console that has the library
folder receives (see "Ignore files and the bootstrap problem" in
development/design/hub-and-folders.md).
"""

from __future__ import annotations

import json
import re
from collections.abc import Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from typing import Protocol

FILE = "patterns-_deleted.stignore"  # in the root of `library/`
FORMAT = 1
KEEP_DAYS = 180  # how long an entry stays in the record, counted from the deletion
TIME = "%Y-%m-%dT%H:%M:%SZ"
FILE_PART = "file"  # the `type` of a catalog part that is one file
_SHA1 = re.compile(r"^[0-9a-f]{40}$")
_ENTRY_TIME = re.compile(r"^(\d{8}T\d{6}Z)-")


@dataclass(frozen=True)
class DeletedFile:
    """One regular file of a deleted game, as the hub had it: `path` is relative to `library/`,
    with `/`, as Syncthing names it."""

    path: str
    size: int
    mtime_ns: int
    sha1: str | None = None  # lowercase hex, only when the catalog knew it for this very file


@dataclass(frozen=True)
class RecordEntry:
    """What one deletion removed from the library. `deleted` and `expires` are UTC times in
    `TIME` form; `entry` is the name of the trash entry."""

    entry: str
    deleted: str
    expires: str
    files: tuple[DeletedFile, ...]


class PartLike(Protocol):
    """What `entry_from_trash` reads of a catalog part (`catalog.Part` has all of it)."""

    @property
    def path(self) -> str: ...

    @property
    def type(self) -> str: ...

    @property
    def size(self) -> int: ...

    @property
    def mtime(self) -> int: ...

    @property
    def sha1(self) -> str | None: ...


# --------------------------------------------------------------------------- times


def when_text(when: datetime) -> str:
    return when.astimezone(UTC).strftime(TIME)


def parse_when(text: object) -> datetime | None:
    if not isinstance(text, str):
        return None
    try:
        return datetime.strptime(text, TIME).replace(tzinfo=UTC)
    except ValueError:
        return None


def name_time(entry: str) -> datetime | None:
    """The time a trash entry's name carries (`<UTC time>-<game id>`), or None."""
    found = _ENTRY_TIME.match(entry)
    if found is None:
        return None
    try:
        return datetime.strptime(found.group(1), "%Y%m%dT%H%M%SZ").replace(tzinfo=UTC)
    except ValueError:
        return None


def expiry(deleted: datetime) -> datetime:
    """`KEEP_DAYS` after `deleted`, or the last second there is for a date too late to add them
    to."""
    try:
        return deleted + timedelta(days=KEEP_DAYS)
    except OverflowError:
        return datetime.max.replace(microsecond=0, tzinfo=UTC)


def expired(item: RecordEntry, now: datetime) -> bool:
    """Whether the entry is past its `expires` (an entry whose time does not parse is)."""
    when = parse_when(item.expires)
    return when is None or when <= now


# --------------------------------------------------------------------------- the file


def safe_path(path: object) -> bool:
    """Whether `path` is relative to `library/` with `/` and no empty, `.` or `..` component."""
    if not isinstance(path, str) or not path or "\0" in path or path.startswith("/"):
        return False
    return all(part not in ("", ".", "..") for part in path.split("/"))


def _order(entries: Iterable[RecordEntry]) -> list[RecordEntry]:
    return sorted(entries, key=lambda e: (e.deleted, e.entry))


def encode(entries: Iterable[RecordEntry]) -> bytes:
    """The bytes of the file: JSON with sorted keys, entries by time and files by path, so the
    same record always gives the same bytes. It is ASCII (a name that is not UTF-8, which Python
    lists with lone surrogates, is written as an escape and read back as the same string, so a
    console compares it with the name its own filesystem gives)."""
    doc = {
        "format": FORMAT,
        "entries": [
            {
                "entry": e.entry,
                "deleted": e.deleted,
                "expires": e.expires,
                "files": [
                    {"path": f.path, "size": f.size, "mtime_ns": f.mtime_ns, "sha1": f.sha1}
                    for f in sorted(e.files, key=lambda f: f.path)
                ],
            }
            for e in _order(entries)
        ],
    }
    text = json.dumps(doc, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return (text + "\n").encode("utf-8")


def valid_sha1(value: object) -> bool:
    """Whether `value` is a SHA-1 as the record holds it: 40 lowercase hex digits."""
    return isinstance(value, str) and _SHA1.fullmatch(value) is not None


def _int(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _file(raw: object) -> DeletedFile | None:
    if not isinstance(raw, dict):
        return None
    path, sha1 = raw.get("path"), raw.get("sha1")
    size, mtime_ns = _int(raw.get("size")), _int(raw.get("mtime_ns"))
    if not isinstance(path, str) or not safe_path(path) or size is None or size < 0:
        return None
    if mtime_ns is None or not (sha1 is None or valid_sha1(sha1)):
        return None
    return DeletedFile(path, size, mtime_ns, sha1)


def _entry(raw: object) -> RecordEntry | None:
    if not isinstance(raw, dict):
        return None
    name, deleted, expires, raw_files = (
        raw.get("entry"),
        raw.get("deleted"),
        raw.get("expires"),
        raw.get("files"),
    )
    if (
        not isinstance(name, str)
        or not name
        or parse_when(deleted) is None
        or parse_when(expires) is None
        or not isinstance(raw_files, list)
    ):
        return None
    files = [_file(f) for f in raw_files]
    if any(f is None for f in files):
        return None
    assert isinstance(deleted, str) and isinstance(expires, str)
    return RecordEntry(name, deleted, expires, tuple(f for f in files if f is not None))


def parse(data: bytes) -> list[RecordEntry] | None:
    """The entries in `data`, or None when it is not a record this version reads: not JSON, a
    `format` other than `FORMAT`, a path that is absolute or has an empty, `.` or `..`
    component, or any other shape. Never raises. None authorises nothing."""
    try:
        doc = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, ValueError, RecursionError):
        return None
    if not isinstance(doc, dict) or _int(doc.get("format")) != FORMAT:
        return None
    raw = doc.get("entries")
    if not isinstance(raw, list):
        return None
    entries = [_entry(e) for e in raw]
    if any(e is None for e in entries):
        return None
    return _order(e for e in entries if e is not None)


# --------------------------------------------------------------------------- the entries


def entry_from_trash(
    entry: str,
    deleted: datetime,
    files: Iterable[DeletedFile],
    parts: Iterable[PartLike] = (),
) -> RecordEntry:
    """The record of a trash entry: `files` are the regular files of its `library/` (with no
    `sha1`). A file gets the `sha1` of the catalog part that is that file only when the part's
    size and modification time (whole seconds) are the file's own, and only when it is a
    `valid_sha1` (the reader refuses the record whole otherwise)."""
    known = {p.path: p for p in parts if p.type == FILE_PART and valid_sha1(p.sha1)}
    out = []
    for f in sorted(files, key=lambda f: f.path):
        part = known.get(f.path)
        sha1 = None
        if part is not None and part.size == f.size and part.mtime == f.mtime_ns // 1_000_000_000:
            sha1 = part.sha1
        out.append(replace(f, sha1=sha1))
    return RecordEntry(entry, when_text(deleted), when_text(expiry(deleted)), tuple(out))


def paths(entries: Iterable[RecordEntry]) -> list[str]:
    """Every path any entry lists, once."""
    return sorted({f.path for e in entries for f in e.files})


def with_entry(entries: Sequence[RecordEntry], item: RecordEntry) -> list[RecordEntry]:
    """`entries` with `item` in place of the entry of the same name (added when there is none)."""
    return _order([*(e for e in entries if e.entry != item.entry), item])


def without_entry(entries: Sequence[RecordEntry], entry: str) -> list[RecordEntry]:
    return [e for e in entries if e.entry != entry]


def compact(
    entries: Iterable[RecordEntry], now: datetime, existing: Collection[str]
) -> list[RecordEntry]:
    """What a write keeps: the entries not yet expired, without the files whose path is in
    `existing` (it is in the library again: a record of its deletion no longer holds). An entry
    left with no file stays until it expires."""
    return _order(
        replace(e, files=tuple(f for f in e.files if f.path not in existing))
        for e in entries
        if not expired(e, now)
    )


def index(entries: Iterable[RecordEntry], now: datetime) -> Mapping[str, tuple[DeletedFile, ...]]:
    """The files the unexpired entries list, by path (a path deleted twice has both records)."""
    found: dict[str, list[DeletedFile]] = {}
    for e in entries:
        if expired(e, now):
            continue
        for f in e.files:
            found.setdefault(f.path, []).append(f)
    return {path: tuple(files) for path, files in found.items()}
