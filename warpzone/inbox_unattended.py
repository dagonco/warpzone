"""The pure decisions of the unattended inbox (`warpzone inbox --apply --unattended`).

What a scheduled run decides without touching anything: the fingerprint of an item and whether
it is ready, waiting or held; which loose disc files to hold; the state file (render, parse, and
the changes made to it); the free space an item needs; whether a work folder is on memory; which
leftover work folders are stale; what counts as news and the exit code; the record of a run
and its webhook payload. Everything arrives as an argument (a listing, the text of a file, the
time now): no filesystem, network, thread or clock is read here. The thin layer that touches the
disk is `inbox_unattended_io.py` (so far the record writes; the walk and the state file come with
the unattended run). See development/design/inbox-unattended.md.

A change to the state is a function `State -> State` (`Change`). The writer reads the file,
applies the changes, writes against the stamp it read, and when the file changed in between it
reads again and applies the same changes to what it read: each change is therefore a pure
function of the state it is given.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable, Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from enum import Enum
from typing import Any, NamedTuple

from warpzone.inbox_core import MIN_FREE_GIB, ItemResult, ScrapeNote, UnitResult
from warpzone.syncthing import REDACTED, scrub

STABLE_SECONDS = 300  # an item is ready once its fingerprint has held this long
HEARTBEAT_SECONDS = 3600  # an empty run writes `checked` at most this often
MAX_ATTEMPTS = 2  # runs that may die on one item before it is held
STATE_VERSION = 1
STATE_FILE = "state.json"  # in `catalog/inbox/` of the hub: the unattended runs' memory
STALE_WORK_SECONDS = 24 * 3600
WORK_PREFIX = "warpzone-inbox-"
GIB = 1024**3
MEMORY_FILESYSTEMS = frozenset({"tmpfs", "ramfs"})
SHEET_EXTENSIONS = (".cue", ".gdi")

LOOSE_REASON = "a disc dropped as loose files: put the sheet and its tracks in one folder"
DIED_REASON = (
    "the run stopped twice while working on this item; it may need more memory than the "
    "container's limit (`mem_limit`), or run inbox on a computer"
)
MOVE_REASON = "could not move the original: {}"

RECORD_VERSION = 1
RECORDS_KEPT = 50  # run records the writer keeps; the older ones are deleted
NOTIFY_LIST_MAX = 20  # names in each list of the webhook payload
WARNINGS_MAX = 20  # warnings kept in a run record

_TIME_FORMAT = "%Y-%m-%dT%H:%M:%SZ"


# ------------------------------------------------------------------ fingerprint


class Stat(NamedTuple):
    """One entry of a walk under an item: `kind` is "f" (file), "d" (directory) or "l" (link).
    `rel` is the path relative to the item ("." for the item itself)."""

    kind: str
    size: int
    mtime_ns: int
    rel: str


@dataclass(frozen=True)
class Observed:
    """What a walk of one item shows: its fingerprint, its file count and its bytes."""

    fingerprint: str
    files: int
    bytes: int


def fingerprint(entries: Iterable[Stat]) -> str:
    """The hash of the lines `<kind> <size> <mtime_ns> <rel>`, sorted and joined with NUL.
    The order of the listing does not matter; hidden files count; a link is its own line
    (the walk never follows it)."""
    lines = sorted(f"{e.kind} {e.size} {e.mtime_ns} {e.rel}" for e in entries)
    digest = hashlib.sha256("\0".join(lines).encode("utf-8", "surrogateescape")).hexdigest()
    return f"sha256:{digest}"


def observe(entries: Sequence[Stat]) -> Observed:
    """The fingerprint with the count and total size of the files (links and folders do not
    add bytes)."""
    files = [e for e in entries if e.kind == "f"]
    return Observed(fingerprint(entries), len(files), sum(e.size for e in files))


# ------------------------------------------------------------------ the state


@dataclass(frozen=True)
class Held:
    reason: str
    at: datetime


@dataclass(frozen=True)
class Entry:
    """One item in the inbox. `first` is when the item was first seen (what "waiting since"
    and doctor's "waiting more than two hours" use); `seen` is when its current fingerprint was
    first seen, reset on every change."""

    fingerprint: str
    files: int
    bytes: int
    first: datetime
    seen: datetime
    attempts: int = 0
    held: Held | None = None


@dataclass(frozen=True)
class RunError:
    """A run-level error kept across runs. `step` is the step of the command where it failed;
    only a run that gets past it clears the error. `config` tells a configuration error (exit
    2) from a runtime one (exit 1)."""

    message: str
    step: int
    first: datetime
    count: int = 1
    config: bool = True


@dataclass(frozen=True)
class LockSeen:
    """What a busy run saw of a lock: its modification time and when it looked."""

    mtime_ns: int
    at: datetime


@dataclass(frozen=True)
class State:
    checked: datetime | None = None
    items: Mapping[str, Entry] = field(default_factory=dict)
    lock_seen: LockSeen | None = None
    error: RunError | None = None


Change = Callable[[State], State]


def update(state: State, *changes: Change) -> State:
    """The state after the changes, applied in order."""
    for change in changes:
        state = change(state)
    return state


def _with_items(state: State, items: Mapping[str, Entry]) -> State:
    return replace(state, items=dict(items))


def with_checked(now: datetime) -> Change:
    return lambda state: replace(state, checked=now)


def with_lock_seen(seen: LockSeen | None) -> Change:
    return lambda state: replace(state, lock_seen=seen)


def with_judged(judged: Mapping[str, Entry], present: Collection[str]) -> Change:
    """Record the judged entries and drop every entry whose name is not in `present` (the
    names now in the inbox): an item that is gone leaves no entry."""

    def change(state: State) -> State:
        items = {n: e for n, e in state.items.items() if n in present}
        items.update({n: e for n, e in judged.items() if n in present})
        return _with_items(state, items)

    return change


def with_attempt(name: str, fingerprint_now: str) -> Change:
    """Count one more attempt on an item, right before it is processed. Nothing happens when
    the entry is missing or belongs to another fingerprint."""

    def change(state: State) -> State:
        entry = state.items.get(name)
        if entry is None or entry.fingerprint != fingerprint_now:
            return state
        return _with_items(
            state, {**state.items, name: replace(entry, attempts=entry.attempts + 1)}
        )

    return change


def with_item_filed(name: str) -> Change:
    """Remove the entry of an item that was filed away (`_done` or `_revisar`)."""

    def change(state: State) -> State:
        return _with_items(state, {n: e for n, e in state.items.items() if n != name})

    return change


def with_held(name: str, reason: str, now: datetime) -> Change:
    """Hold an item (for example an original that could not be moved after its games were
    placed). An entry already held with the same reason keeps its time; a missing entry is
    left alone."""

    def change(state: State) -> State:
        entry = state.items.get(name)
        if entry is None:
            return state
        if entry.held is not None and entry.held.reason == reason:
            return state
        return _with_items(state, {**state.items, name: replace(entry, held=Held(reason, now))})

    return change


def with_error(message: str, step: int, now: datetime, config: bool = True) -> Change:
    """Record a run-level error: the same message as the stored one only raises its count, a
    different one replaces it."""

    def change(state: State) -> State:
        old = state.error
        if old is not None and old.message == message:
            return replace(state, error=replace(old, count=old.count + 1))
        return replace(state, error=RunError(message, step, now, 1, config))

    return change


def with_error_cleared(reached: int) -> Change:
    """Clear a stored error when this run got past its step: `reached` is the last step the run
    started, so an error of step `n` is cleared when `reached > n`. A run that finds the inbox
    empty reaches step 2 and can only clear the errors of step 1."""

    def change(state: State) -> State:
        if state.error is not None and reached > state.error.step:
            return replace(state, error=None)
        return state

    return change


def heartbeat_due(state: State, now: datetime) -> bool:
    """Whether an empty run writes `checked`: it is missing, older than an hour, or in the
    future (a clock that went back)."""
    if state.checked is None:
        return True
    age = (now - state.checked).total_seconds()
    return age < 0 or age >= HEARTBEAT_SECONDS


# ------------------------------------------------------------------ readiness


class Verdict(Enum):
    WAITING = "waiting"
    READY = "ready"
    HELD = "held"


def decide(
    entry: Entry | None,
    observed: Observed,
    now: datetime,
    hold_reason: str | None = None,
) -> tuple[Verdict, Entry]:
    """Judge one item: the verdict and the entry to record.

    No entry, or another fingerprint: waiting, with `seen` now and attempts back to zero (a
    held item whose fingerprint changed is new again, so `first` restarts too). The same
    fingerprint: held stays held; two attempts that died hold it; a clock that went back waits
    and restarts `seen`; ready once `STABLE_SECONDS` have passed since `seen`. `hold_reason`
    (loose disc files) holds an item at the moment it would be ready."""
    if entry is None:
        return Verdict.WAITING, _new_entry(observed, now, now)
    if entry.fingerprint != observed.fingerprint:
        first = now if entry.held is not None else min(entry.first, now)
        return Verdict.WAITING, _new_entry(observed, first, now)
    if entry.held is not None:
        return Verdict.HELD, entry
    if entry.attempts >= MAX_ATTEMPTS:
        return Verdict.HELD, replace(entry, held=Held(DIED_REASON, now))
    if now < entry.seen:
        return Verdict.WAITING, replace(entry, first=min(entry.first, now), seen=now)
    if (now - entry.seen).total_seconds() < STABLE_SECONDS:
        return Verdict.WAITING, entry
    if hold_reason is not None:
        return Verdict.HELD, replace(entry, held=Held(hold_reason, now))
    return Verdict.READY, entry


def _new_entry(observed: Observed, first: datetime, seen: datetime) -> Entry:
    return Entry(observed.fingerprint, observed.files, observed.bytes, first, seen)


def _track(ref: str, lowered: Mapping[str, str]) -> str | None:
    parts = [p for p in ref.replace("\\", "/").split("/") if p not in ("", ".")]
    if len(parts) != 1:
        return None  # a track inside a folder is not a loose file
    return lowered.get(parts[0].lower())


def loose_sheets(top_names: Sequence[str], refs: Mapping[str, Sequence[str]]) -> dict[str, str]:
    """The top-level files of `inbox/` to hold, with the reason: every `.cue` or `.gdi`, and each
    top-level file it names. `top_names` are the names of the files at the root (not folders,
    not `_`-prefixed ones); `refs` maps a sheet's name to the file names written in it. A name a
    sheet gives that is not there, or sits in a folder, is ignored; a loose `.bin` that no sheet
    names is left alone."""
    lowered = {n.lower(): n for n in top_names}
    held: dict[str, str] = {}
    for name in top_names:
        if not name.lower().endswith(SHEET_EXTENSIONS):
            continue
        held[name] = LOOSE_REASON
        for ref in refs.get(name, ()):
            target = _track(ref, lowered)
            if target is not None:
                held[target] = LOOSE_REASON
    return held


# ------------------------------------------------------------------ the state file


def _stamp(moment: datetime) -> str:
    return moment.astimezone(UTC).strftime(_TIME_FORMAT)


def _item_json(entry: Entry) -> dict[str, object]:
    held = (
        None if entry.held is None else {"reason": entry.held.reason, "at": _stamp(entry.held.at)}
    )
    return {
        "fingerprint": entry.fingerprint,
        "files": entry.files,
        "bytes": entry.bytes,
        "first": _stamp(entry.first),
        "seen": _stamp(entry.seen),
        "attempts": entry.attempts,
        "held": held,
    }


def render_state(state: State) -> str:
    error = state.error
    lock = state.lock_seen
    doc: dict[str, object] = {
        "v": STATE_VERSION,
        "checked": None if state.checked is None else _stamp(state.checked),
        "items": {name: _item_json(e) for name, e in sorted(state.items.items())},
        "lock_seen": None if lock is None else {"mtime_ns": lock.mtime_ns, "at": _stamp(lock.at)},
        "error": None
        if error is None
        else {
            "message": error.message,
            "step": error.step,
            "first": _stamp(error.first),
            "count": error.count,
            "config": error.config,
        },
    }
    return json.dumps(doc, indent=2, sort_keys=True) + "\n"


def _time(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        return datetime.strptime(value, _TIME_FORMAT).replace(tzinfo=UTC)
    except ValueError:
        return None


def _count(value: object, minimum: int = 0) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        return None
    return value


def _parse_entry(raw: object) -> Entry | None:
    if not isinstance(raw, dict):
        return None
    fp = raw.get("fingerprint")
    files = _count(raw.get("files"))
    size = _count(raw.get("bytes"))
    seen = _time(raw.get("seen"))
    first = _time(raw.get("first")) or seen
    attempts = _count(raw.get("attempts", 0))
    if not isinstance(fp, str) or not fp or None in (files, size, seen, first, attempts):
        return None
    held = None
    raw_held = raw.get("held")
    if raw_held is not None:
        if not isinstance(raw_held, dict):
            return None
        reason, at = raw_held.get("reason"), _time(raw_held.get("at"))
        if not isinstance(reason, str) or at is None:
            return None
        held = Held(reason, at)
    assert files is not None and size is not None and attempts is not None
    assert seen is not None and first is not None
    return Entry(fp, files, size, first, seen, attempts, held)


def _parse_error(raw: object) -> RunError | None:
    if not isinstance(raw, dict):
        return None
    message, step = raw.get("message"), _count(raw.get("step"), 1)
    first, count = _time(raw.get("first")), _count(raw.get("count", 1), 1)
    config = raw.get("config", True)
    if not isinstance(message, str) or step is None or first is None or count is None:
        return None
    if not isinstance(config, bool):
        return None
    return RunError(message, step, first, count, config)


def _parse_lock_seen(raw: object) -> LockSeen | None:
    if not isinstance(raw, dict):
        return None
    mtime, at = _count(raw.get("mtime_ns")), _time(raw.get("at"))
    if mtime is None or at is None:
        return None
    return LockSeen(mtime, at)


def parse_state(text: str | None) -> tuple[State, str | None]:
    """The state in `text` and a warning, or None when there is nothing to warn about.

    A missing file (`text` None) is an empty state with no warning. A file that is not JSON, not
    an object or of another version is an empty state with a warning. A damaged part (an entry,
    the error, the lock note) is left out with a warning and the rest is kept."""
    if text is None:
        return State(), None
    try:
        doc = json.loads(text)
    except ValueError:
        return State(), "the inbox state file cannot be parsed; starting from an empty one"
    if not isinstance(doc, dict) or doc.get("v") != STATE_VERSION:
        return State(), "the inbox state file is of an unknown form; starting from an empty one"
    damaged: list[str] = []
    items: dict[str, Entry] = {}
    raw_items = doc.get("items")
    if raw_items is not None and not isinstance(raw_items, dict):
        damaged.append("items")
        raw_items = {}
    for name, raw in (raw_items or {}).items():
        entry = _parse_entry(raw)
        if entry is None or not name:
            damaged.append(f"item {name!r}")
        else:
            items[name] = entry
    checked = None
    if doc.get("checked") is not None:
        checked = _time(doc["checked"])
        if checked is None:
            damaged.append("checked")
    lock_seen = None
    if doc.get("lock_seen") is not None:
        lock_seen = _parse_lock_seen(doc["lock_seen"])
        if lock_seen is None:
            damaged.append("lock_seen")
    error = None
    if doc.get("error") is not None:
        error = _parse_error(doc["error"])
        if error is None:
            damaged.append("error")
    warning = None
    if damaged:
        warning = (
            "the inbox state file is damaged in " + ", ".join(damaged) + "; those were dropped"
        )
    return State(checked, items, lock_seen, error), warning


# ------------------------------------------------------------------ space, memory, leftovers


@dataclass(frozen=True)
class Space:
    fits: bool
    needed: int  # bytes the filesystem must have free for this item
    missing: int  # bytes short (0 when it fits)
    reason: str | None  # "waiting for space: N GiB more needed", None when it fits


def space_verdict(free_bytes: int, item_bytes: int, reserve_gib: int = MIN_FREE_GIB) -> Space:
    """Whether the hub has room for an item: twice its bytes plus the reserve (the doctor's
    threshold). Exactly enough fits. The reason rounds the shortfall up to whole GiB."""
    needed = 2 * item_bytes + reserve_gib * GIB
    missing = max(0, needed - free_bytes)
    if missing == 0:
        return Space(True, needed, 0, None)
    gib = -(-missing // GIB)
    return Space(False, needed, missing, f"waiting for space: {gib} GiB more needed")


_OCTAL = re.compile(r"\\([0-7]{3})")


def _unescape(field: str) -> str:
    return _OCTAL.sub(lambda m: chr(int(m.group(1), 8)), field)


def _under(path: str, mount: str) -> bool:
    if mount == "/":
        return True
    return path == mount or path.startswith(mount + "/")


def memory_fs(mountinfo: str, path: str) -> bool:
    """Whether `path` (absolute, normalized) lives on `tmpfs` or `ramfs`, judged by the deepest
    mount in `mountinfo` (the text of `/proc/self/mountinfo`) that contains it. Text with no
    matching mount, or a relative path, says no."""
    if not path.startswith("/"):
        return False
    best_len, best_type = -1, ""
    for line in mountinfo.splitlines():
        head, sep, tail = line.partition(" - ")
        fields = head.split()
        if not sep or len(fields) < 5 or not tail.split():
            continue
        mount = _unescape(fields[4])
        if _under(path, mount) and len(mount) > best_len:
            best_len, best_type = len(mount), tail.split()[0]
    return best_type in MEMORY_FILESYSTEMS


class Listing(NamedTuple):
    """One entry of the work folder as a listing shows it (`lstat`)."""

    name: str
    mtime: float
    is_dir: bool
    is_symlink: bool


def stale_work_dirs(
    listing: Iterable[Listing], now: float, max_age: float = STALE_WORK_SECONDS
) -> list[str]:
    """The `warpzone-inbox-*` folders older than `max_age` seconds: a killed run's leftovers.
    A link, a plain file, another name and a folder modified in the future are never chosen."""
    return sorted(
        e.name
        for e in listing
        if e.name.startswith(WORK_PREFIX)
        and e.is_dir
        and not e.is_symlink
        and now - e.mtime > max_age
    )


# ------------------------------------------------------------------ news and exit codes


@dataclass(frozen=True)
class News:
    review: tuple[str, ...] = ()  # items this run sent to `_revisar/`
    held: tuple[str, ...] = ()  # items newly held
    error: RunError | None = None  # a run-level error that is new

    def __bool__(self) -> bool:
        return bool(self.review or self.held or self.error)


def news(before: State, after: State, reviewed: Sequence[str] = ()) -> News:
    """What a person has not been told yet: `before` is the state as the run read it at its
    start, `after` the one it writes, `reviewed` the items this run sent to review. A held
    item is news when it was not held before with the same fingerprint; an error is news when
    its message differs from the stored one (a run that meets the same message only raised
    `count`, and one that got past the step cleared it, so the same error later is news)."""
    held = []
    for name, entry in sorted(after.items.items()):
        if entry.held is None:
            continue
        old = before.items.get(name)
        if old is None or old.held is None or old.fingerprint != entry.fingerprint:
            held.append(name)
    error = after.error
    if error is not None and before.error is not None and before.error.message == error.message:
        error = None
    return News(tuple(reviewed), tuple(held), error)


def exit_code(found: News) -> int:
    """`0` with no news, `2` when the news holds a configuration error, else `1`."""
    if found.error is not None and found.error.config:
        return 2
    return 1 if found else 0


# ------------------------------------------------------------------ run records and the webhook

_RECORD_NAME = re.compile(r"^(\d{8}T\d{6}Z)(?:-(\d+))?\.json$")
# An absolute path (a POSIX one after a space, a quote or the start, or a drive letter): the
# part of a message that says where a file is on this machine, which a record never holds.
# A `/` starts a path only at the start of the text or after whitespace, a quote, `=`, `:`, `(`
# or `[`, so `Foo (Disc 1)/bar.cue` and `!/roms/psx/a.chd` are names, not paths. `~/` is a home.
_ABS_PATH = re.compile(
    r"(?<![^\s'\"`=:(\[])(?:/(?=[\w.~@%+-])|~/)[^\s'\"`,;)\]]*|\b[A-Za-z]:\\[^\s'\"]*"
)
# A zRIF or any other long base64 token (a licence, a key), whether or not it is known.
_LONG_TOKEN = re.compile(r"[A-Za-z0-9+/=_-]{128,}")


def record_id(started: datetime, n: int = 1) -> str:
    """The id of a run record: the start in UTC (`20261113T101500Z`), with `-2`, `-3` for the
    runs that started in the same second."""
    base = started.astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")
    return base if n <= 1 else f"{base}-{n}"


def select_records_purge(names: Iterable[str], keep: int = RECORDS_KEPT) -> list[str]:
    """The file names of `runs/` to delete so the newest `keep` records remain, oldest first.
    Only names of the form `<id>.json` count; anything else is left alone."""
    found = []
    for name in names:
        match = _RECORD_NAME.match(name)
        if match:
            found.append(((match.group(1), int(match.group(2) or 1)), name))
    found.sort()
    return [name for _, name in found[: max(len(found) - keep, 0)]]


def clean_text(text: str, roots: Sequence[str] = (), secrets: Sequence[str] = ()) -> str:
    """`text` as a record or a webhook may hold it: each secret redacted, the hub root cut off
    the paths under it (they become relative to it), any other absolute path replaced by
    `<path>`, and any long base64 token (a zRIF the handler did not know) redacted."""
    text = scrub(text, tuple(secrets))
    for root in sorted({r.rstrip("/") for r in roots if r}, key=len, reverse=True):
        text = text.replace(root + "/", "").replace(root, ".")
    text = _ABS_PATH.sub("<path>", text)
    return _LONG_TOKEN.sub(REDACTED, text)


def item_outcome(result: ItemResult) -> str:
    """`held` (the original could not be filed away and is still in the inbox), `review`
    (sent to `_revisar/`) or `done` (filed in `_done/`)."""
    if result.unmoved:
        return "held"
    return "review" if result.review else "done"


def _game(unit: UnitResult, clean: Callable[[str], str]) -> dict[str, Any]:
    listed = unit.outcome in ("placed", "already", "planned", "exists")
    return {
        "system": clean(unit.system),
        "entry": clean(unit.entry),
        "outcome": unit.outcome,
        "path": unit.path if unit.outcome != "review" else None,
        "whitelist": [clean(x) for x in unit.whitelist_lines] if listed else [],
        "ra": [clean(x) for x in unit.ra],
        "notes": [clean(x) for x in unit.notes],
    }


def _item(result: ItemResult, clean: Callable[[str], str]) -> dict[str, Any]:
    return {
        "item": clean(result.name),
        "outcome": item_outcome(result),
        "moved_to": f"inbox/{result.moved_to}" if result.moved_to else None,
        "games": [_game(u, clean) for u in result.units],
        "reasons": [clean(x) for x in result.reasons],
        "notes": [clean(x) for x in result.notes],
    }


def build_record(
    results: Sequence[ItemResult],
    *,
    run_id: str,
    mode: str,
    started: datetime,
    finished: datetime,
    status: int,
    roots: Sequence[str] = (),
    secrets: Sequence[str] = (),
    waiting: int = 0,
    scrape: ScrapeNote | None = None,
    purged: Sequence[str] = (),
    warnings: Sequence[str] = (),
) -> dict[str, Any]:
    """The run record, from the same `ItemResult` values the printed report comes from. Pure.
    Items left in the inbox untouched (still being copied) are not in `items`: they are
    counted in `waiting`. Every text goes through `clean_text`, so the record holds no absolute
    path, no secret and no zRIF."""

    def clean(text: str) -> str:
        return clean_text(text, roots, secrets)

    processed = [r for r in results if not r.left]
    items = [_item(r, clean) for r in processed]
    units = [u for r in processed for u in r.units]
    listed = [u for u in units if u.outcome in ("placed", "already", "planned", "exists")]
    outcomes = [i["outcome"] for i in items]
    record: dict[str, Any] = {
        "v": RECORD_VERSION,
        "id": run_id,
        "mode": mode,
        "started": _stamp(started),
        "finished": _stamp(finished),
        "exit": status,
        "items": items,
        "counts": {
            "placed": sum(u.outcome in ("placed", "planned") for u in units),
            "already": sum(u.outcome in ("already", "exists") for u in units),
            "review": outcomes.count("review"),
            "held": outcomes.count("held"),
            "waiting": waiting,
        },
        "whitelist": sorted({clean(line) for u in listed for line in u.whitelist_lines}),
    }
    if scrape is not None:
        note: dict[str, Any] = {"state": scrape.state, "systems": list(scrape.systems)}
        if scrape.state != "done" and scrape.message:
            note["message"] = clean(scrape.message)
        record["scrape"] = note
    record["purged"] = list(purged)
    record["warnings"] = [clean(w) for w in warnings][:WARNINGS_MAX]
    return record


def render_record(record: Mapping[str, Any]) -> str:
    """The text of a record file."""
    return json.dumps(record, indent=2, ensure_ascii=False) + "\n"


def should_notify(record: Mapping[str, Any], news_found: bool = False) -> bool:
    """Whether a run sends the webhook. An attended run sends it only when some item needs a
    person: one sent to `_revisar/`, or one whose original stayed in the inbox (the report
    calls both review). An unattended run sends it for placed games or news (`news_found`).
    An empty, busy or waiting-only run has no record and sends nothing."""
    counts = record["counts"]
    if record["mode"] == "attended":
        return bool(counts["review"] or counts["held"])
    return bool(counts["placed"] or news_found)


def notify_payload(record: Mapping[str, Any], error: str | None = None) -> dict[str, Any]:
    """The JSON the webhook gets: the counts, up to `NOTIFY_LIST_MAX` placed games
    (`<system>/<entry>`) and items that need a person, and the run-level `error` message (the
    caller passes it through `clean_text`). It is built from the record, so it holds no
    absolute path, key or URL."""
    items = record["items"]
    games = [
        f"{g['system']}/{g['entry']}"
        for item in items
        for g in item["games"]
        if g["outcome"] in ("placed", "planned")
    ]
    attention = [item["item"] for item in items if item["outcome"] in ("review", "held")]
    counts = record["counts"]
    return {
        "source": "inbox",
        "mode": record["mode"],
        "run": record["id"],
        "placed": counts["placed"],
        "already": counts["already"],
        "review": counts["review"],
        "held": counts["held"],
        "games": games[:NOTIFY_LIST_MAX],
        "review_items": attention[:NOTIFY_LIST_MAX],
        "error": error,
    }
