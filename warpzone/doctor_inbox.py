"""The doctor checks that only matter to the machine that runs `warpzone inbox`.

DATs, arcade DATs, Switch titledb and keys, the hub folder, where inbox runs, the
RetroAchievements key, the unattended inbox line (`check_unattended_inbox`, with its pure judge
`judge_unattended_inbox`), and `collapse_inbox` (pure), which folds the optional ones into one
line without an `[inbox]` table. See development/design/doctor.md.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from warpzone import inbox_switch as switch
from warpzone import ra
from warpzone.dat import SYSTEMS, DatLibrary
from warpzone.doctor_core import (
    FAIL,
    G_DATS,
    G_HUB,
    G_INBOX,
    G_RA,
    G_SWITCH,
    G_WAY,
    INBOX_GROUPS,
    OK,
    OPTIONAL,
    WARN,
    Context,
    Result,
    merge_verdicts,
)
from warpzone.doctor_hints import (
    DAT_CLONE_HINT,
    DAT_MOUNT_HINT,
    KEYS_HINT,
    TITLEDB_HINT,
    TITLEDB_REPO,
)
from warpzone.inbox_core import MIN_FREE_GIB
from warpzone.inbox_lock import LOCK_NAME
from warpzone.inbox_lock import parse as parse_lock
from warpzone.inbox_unattended import STATE_FILE, State, newest_record, parse_state
from warpzone.inbox_unattended_io import inbox_dir, runs_dir

DAT_MAX_AGE_DAYS = 180

GIB = 1024**3
DAY_SECONDS = 86400

UNATTENDED_NAME = "Unattended inbox"
STALE_CHECK = timedelta(hours=2)  # the heartbeat writes `checked` at least hourly
LONG_WAIT = timedelta(hours=2)  # an item waiting this long since `first` is worth a look
LONG_LOCK = timedelta(hours=6)  # a lock held this long is stuck, not working
LISTED_MAX = 3  # items named in a line; the rest are counted
NO_STATE_FIX = (
    "schedule `warpzone inbox --apply --unattended` every 15 minutes on the hub "
    "(see https://dagonco.github.io/warpzone/development/design/inbox-unattended/"
    "#what-the-user-sets-up-once)"
)


def _dat_files(library: DatLibrary) -> list[Path]:
    return [path for paths in library.paths.values() for path in paths]


def check_dats(ctx: Context) -> list[Result]:
    p = ctx.probes
    dats_dir = ctx.config.settings.dats_dir
    inbox = ctx.inbox_configured
    missing = FAIL if inbox else OPTIONAL
    clone = DAT_MOUNT_HINT if p.in_container() else DAT_CLONE_HINT
    if not dats_dir:
        return [
            Result(
                "DAT folder",
                G_DATS,
                missing,
                "dats_dir is not set in [inbox]; inbox needs the Redump and No-Intro DATs "
                "to identify games"
                if inbox
                else "no [inbox] dats_dir; only needed by inbox (or pass --dats)",
                clone,
            )
        ]
    directory = Path(dats_dir)
    if not p.is_dir(directory):
        return [Result("DAT folder", G_DATS, missing, f"{directory} does not exist", clone)]
    library = DatLibrary(directory, None)
    found = library.systems()
    known = sorted(set(SYSTEMS.values()))
    if not found:
        return [
            Result(
                "DAT folder",
                G_DATS,
                missing,
                f"{directory} holds no Redump or No-Intro DATs warpzone knows",
                f"point dats_dir at libretro-database's metadat folder. {clone}",
            )
        ]
    absent = [s for s in known if s not in found]
    detail = f"{directory}: {len(found)} of {len(known)} systems ({', '.join(found)})"
    if absent:
        detail += f"; no DAT for {', '.join(absent)}"
    results = [Result("DAT folder", G_DATS, OK, detail)]
    try:
        newest = max(p.mtime(path) for path in _dat_files(library))
    except OSError as exc:
        results.append(
            Result("DAT age", G_DATS, WARN, f"cannot read DAT file times ({exc.strerror})")
        )
        return results
    days = int((p.now() - newest) // DAY_SECONDS)
    if days > DAT_MAX_AGE_DAYS:
        results.append(
            Result(
                "DAT age",
                G_DATS,
                WARN,
                f"newest DAT is {days} days old (limit {DAT_MAX_AGE_DAYS}); newer dumps may not "
                "be recognized",
                "update your libretro-database clone: git -C <path of the clone> pull",
            )
        )
    else:
        results.append(Result("DAT age", G_DATS, OK, f"newest DAT is {max(days, 0)} days old"))
    return results


# The arcade databases `inbox` can use: ([inbox] key, name, what it is, where to get it).
ARCADE_DATS = (
    (
        "mame_dat",
        "MAME DAT",
        "MAME's -listxml (or the official mameXXXXlx.zip, read as it is)",
        "download the mameXXXXlx.zip of the MAME version your frontend runs from "
        "https://github.com/mamedev/mame/releases (the file is the zipped listxml)",
    ),
    (
        "fbneo_dat",
        "FinalBurn Neo DAT",
        "the FinalBurn Neo arcade DAT",
        "download 'FinalBurn Neo (ClrMame Pro XML, Arcade only).dat' from the dats/ folder of "
        "https://github.com/libretro/FBNeo",
    ),
    (
        "mame2003plus_dat",
        "MAME 2003-Plus DAT",
        "the MAME 2003-Plus DAT",
        "download mame2003-plus.xml from the metadata/ folder of "
        "https://github.com/libretro/mame2003-plus-libretro",
    ),
)


def check_arcade_dats(ctx: Context) -> list[Result]:
    """The optional arcade databases: each is `[--]` when not set, a problem when set
    to something that is not a file (`inbox` would refuse to start), and `[ok]` when
    the file is there."""
    results = []
    for key, name, what, fix in ARCADE_DATS:
        value = getattr(ctx.config.settings, key)
        if not value:
            results.append(
                Result(name, G_DATS, OPTIONAL, f"{key} is not set; arcade zips need {what}", fix)
            )
            continue
        path = Path(value)
        try:
            size = path.stat().st_size if path.is_file() else None
        except OSError:
            size = None
        if size is None:
            results.append(Result(name, G_DATS, FAIL, f"{path} is not a file", fix))
        else:
            results.append(Result(name, G_DATS, OK, f"{path} ({size // 2**20} MiB)"))
    return results


def check_switch(ctx: Context) -> list[Result]:
    """Presence only: the titledb clone and the keys file. The keys file is never opened."""
    p = ctx.probes
    settings = ctx.config.settings
    results: list[Result] = []
    if not settings.titledb_dir:
        results.append(
            Result(
                "titledb_dir",
                G_SWITCH,
                OPTIONAL,
                "not set; only needed for Switch games (.nsp / .nsz) in inbox",
                TITLEDB_HINT,
            )
        )
    else:
        directory = Path(settings.titledb_dir)
        if not p.is_dir(directory):
            results.append(
                Result(
                    "titledb_dir",
                    G_SWITCH,
                    FAIL,
                    f"{directory} does not exist",
                    f"clone it: {TITLEDB_HINT}",
                )
            )
        elif not (found := switch.region_files(directory)):
            results.append(
                Result(
                    "titledb_dir",
                    G_SWITCH,
                    FAIL,
                    f"{directory} has no region files (like US.en.json)",
                    f"is it a clone of {TITLEDB_REPO}? {TITLEDB_HINT}",
                )
            )
        else:
            results.append(
                Result("titledb_dir", G_SWITCH, OK, f"{directory}: {len(found)} region file(s)")
            )
    keys = Path(settings.switch_keys) if settings.switch_keys else switch.default_keys(p.home)
    if p.is_file(keys):
        results.append(
            Result("switch_keys", G_SWITCH, OK, f"{keys} (not read, only passed to nsz)")
        )
    elif settings.switch_keys:
        results.append(
            Result(
                "switch_keys",
                G_SWITCH,
                FAIL,
                f"[inbox] switch_keys = {keys} is not a file",
                f"fix the path. {KEYS_HINT}",
            )
        )
    else:
        results.append(
            Result(
                "switch_keys",
                G_SWITCH,
                OPTIONAL,
                f"{keys} not found; nsz needs it to read Switch metadata and to decompress .nsz",
                KEYS_HINT,
            )
        )
    return results


def check_hub(ctx: Context) -> list[Result]:
    p = ctx.probes
    settings = ctx.config.settings
    root = settings.root
    level = FAIL
    source = "[inbox] root"
    if not root:
        hub_root = ctx.config.hub_root
        if hub_root and p.is_dir(Path(hub_root)):
            where = "mounted in this container" if p.in_container() else "this machine is the hub"
            root, level, source = hub_root, WARN, f"[hub] root ({where})"
        else:
            return [
                Result(
                    "Hub folder",
                    G_HUB,
                    OPTIONAL,
                    "no [inbox] root; only needed by inbox (or pass --root), where it is the "
                    "hub's games share as mounted on this machine",
                    'set root = "/path/to/mounted/share" in the [inbox] table',
                )
            ]
    folder = Path(root)
    mount_fix = (
        f"mount the hub's games share on this machine, then check that {source} points at "
        "the folder that holds inbox/ and library/"
    )
    if not p.is_dir(folder):
        return [
            Result("Hub folder", G_HUB, level, f"{folder} is not reachable ({source})", mount_fix)
        ]
    needed = ("inbox", "library/roms")
    lacking = [n for n in needed if not p.is_dir(folder / n)]
    if lacking:
        return [
            Result(
                "Hub folder",
                G_HUB,
                level,
                f"{folder} lacks {', '.join(n + '/' for n in lacking)}",
                f"is {source} the hub root? It must hold inbox/ and library/roms/ "
                "(see https://dagonco.github.io/warpzone/overview/concepts/#the-hub)",
            )
        ]
    results = [Result("Hub folder", G_HUB, OK, f"{folder} has inbox/ and library/roms/")]
    try:
        total, free = p.disk_free(folder)
    except OSError as exc:
        results.append(Result("Free space", G_HUB, WARN, f"cannot read: {exc.strerror or exc}"))
        return results
    detail = f"{free / GIB:.1f} GiB free of {total / GIB:.0f} GiB"
    if free < MIN_FREE_GIB * GIB:
        results.append(
            Result(
                "Free space",
                G_HUB,
                WARN,
                f"{detail} (below {MIN_FREE_GIB} GiB)",
                "free space on the hub's volume before adding games; inbox never overwrites, "
                "but a full disk stops placing",
            )
        )
    else:
        results.append(Result("Free space", G_HUB, OK, detail))
    return results


def check_inbox_way(ctx: Context) -> list[Result]:
    """Which way `inbox` would run here, and what that way needs besides the tools: in the hub
    container the hub root is [hub] root, the DATs and the work folder have to be mounted, and
    the work folder must not be the container's memory-backed /tmp. Only with an [inbox] table."""
    if not ctx.inbox_configured:
        return []
    p = ctx.probes
    settings = ctx.config.settings
    if not p.in_container():
        return [
            Result(
                "Where inbox runs",
                G_WAY,
                OK,
                "on a computer: tools come from the [inbox] paths or PATH, the hub's share "
                "must be mounted here",
            )
        ]
    results = [
        Result(
            "Where inbox runs",
            G_WAY,
            OK,
            "in the hub container: tools come from the image; paths in [inbox] are paths "
            "inside the container",
        )
    ]
    hub_root = ctx.config.hub_root
    if not settings.root and hub_root:
        results.append(
            Result(
                "Hub root for inbox",
                G_WAY,
                WARN,
                "[inbox] root is not set; inbox needs it (or --root) even though the hub "
                "root is mounted here",
                f'set root = "{hub_root}" in the [inbox] table: the hub root is mounted at '
                "the same path as [hub] root",
            )
        )
    work = settings.work_dir
    if not work:
        results.append(
            Result(
                "Work folder",
                G_WAY,
                WARN,
                "work_dir is not set; inbox would unpack and convert in /tmp, which is "
                "memory in the container (a few GiB per game)",
                "mount a folder with room (the compose file's volumes) and set work_dir in "
                "[inbox] to it",
            )
        )
    elif p.is_dir(Path(work)):
        results.append(Result("Work folder", G_WAY, OK, work))
    elif p.is_dir(Path(work).parent) and p.is_writable(Path(work).parent):
        # inbox creates the folder itself, one level, on its first run; the parent is what has
        # to be a mounted folder.
        results.append(Result("Work folder", G_WAY, OK, f"{work} (created on the first run)"))
    elif p.is_dir(Path(work).parent):
        results.append(
            Result(
                "Work folder",
                G_WAY,
                WARN,
                f"{work} does not exist and its parent folder is not writable, so inbox "
                "cannot create it",
                "mount the parent read-write (the compose file's volumes), or point work_dir "
                "at a folder that is",
            )
        )
    else:
        results.append(
            Result(
                "Work folder",
                G_WAY,
                WARN,
                f"{work} does not exist in this container, nor does its parent folder",
                "mount a folder at that path (the compose file's volumes), or point work_dir "
                "at one that is mounted",
            )
        )
    return results


def check_ra(ctx: Context) -> list[Result]:
    # The value is never read into a result: only whether it is there.
    if ctx.probes.env.get(ra.KEY_ENV):
        return [Result(ra.KEY_ENV, G_RA, OK, "set (hash lookups enabled)")]
    return [
        Result(
            ra.KEY_ENV,
            G_RA,
            OPTIONAL,
            "not set; inbox still hashes with RAHasher, but cannot look hashes up",
            f"export {ra.KEY_ENV}=<your key> (from your retroachievements.org settings) in the "
            "shell or service that runs warpzone; keep it out of the config and the repository",
        )
    ]


def _ago(delta: timedelta) -> str:
    minutes = max(int(delta.total_seconds() // 60), 0)
    return f"{minutes} min" if minutes < 120 else f"{minutes // 60} h"


def _named(names: Sequence[str]) -> str:
    shown = ", ".join(names[:LISTED_MAX])
    return shown + (f" and {len(names) - LISTED_MAX} more" if len(names) > LISTED_MAX else "")


def _count(counts: Mapping[str, Any], key: str) -> int:
    """A count of a run record; a missing or damaged one (not a whole number) is 0."""
    value = counts.get(key)
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def _last_run(record: Mapping[str, Any] | None) -> str:
    """ "last run <date>: placed X, for review Y" from a run record."""
    counts = record.get("counts") if record else None
    started = record.get("started") if record else None
    if not isinstance(started, str) or not isinstance(counts, dict):
        return "no run has been recorded yet"
    when = started[:16].replace("T", " ") + " UTC"
    placed = _count(counts, "placed")
    review = _count(counts, "review") + _count(counts, "held")
    return f"last run {when}: placed {placed}, for review {review}"


def judge_unattended_inbox(
    state: State | None,
    record: Mapping[str, Any] | None,
    lock_since: datetime | None,
    now: datetime,
) -> tuple[str, str, str]:
    """(status, detail, fix) of the unattended inbox, from its state (None when the file does not
    exist), the newest run record (None when there is none), when the lock was taken (None when
    there is no lock) and the time. Several findings share the one line, the worst status
    first. Pure."""
    if state is None:
        return OPTIONAL, "no unattended run has recorded anything", NO_STATE_FIX
    parts: list[tuple[str, str, str]] = []
    err = state.error
    if err is not None:
        since = err.first.astimezone(UTC).strftime("%Y-%m-%d %H:%M")
        if err.config:
            fix = (
                "fix what the message says (the config, the mounts; `warpzone doctor` on the "
                f"hub lists them); the next run that gets past step {err.step} clears it"
            )
        else:
            fix = (
                "read the scheduled task's output on the hub for the cause; the next run that "
                f"gets past step {err.step} clears it"
            )
        parts.append((FAIL, f"{err.message} (since {since} UTC, {err.count} run(s))", fix))
    if state.checked is None:
        parts.append(
            (
                WARN,
                "the state file records no check; the unattended inbox may never have run",
                "is the scheduled task enabled? Run it once by hand to see its output",
            )
        )
    elif now - state.checked > STALE_CHECK:
        parts.append(
            (
                WARN,
                f"the unattended inbox last ran {_ago(now - state.checked)} ago; is the "
                "scheduled task enabled?",
                "enable the scheduled task on the hub, or run it once by hand to see why it stops",
            )
        )
    held = [f"{name} ({e.held.reason})" for name, e in state.items.items() if e.held]
    if held:
        parts.append(
            (
                WARN,
                f"held: {_named(held)}",
                "look at it, run `warpzone inbox` to see the report, then `warpzone inbox "
                "--apply` by hand, or replace the item to retry",
            )
        )
    waiting = [
        name for name, e in state.items.items() if e.held is None and now - e.first > LONG_WAIT
    ]
    if waiting:
        parts.append(
            (
                WARN,
                f"waiting for more than 2 h since first seen: {_named(waiting)}",
                "a copy that keeps writing, a file a program keeps open, or no space: finish "
                "or stop what writes it, and free space on the hub's volume if it is low",
            )
        )
    if lock_since is not None and now - lock_since > LONG_LOCK:
        parts.append(
            (
                WARN,
                f"the inbox lock has been held for {_ago(now - lock_since)}",
                "a run that never ends: stop it (or `warpzone inbox --force-lock` if its "
                "holder is gone), then look at its output",
            )
        )
    if parts:
        return merge_verdicts(parts)
    assert state.checked is not None
    return OK, f"last check {_ago(now - state.checked)} ago; {_last_run(record)}", ""


def _read_record(ctx: Context, root: Path) -> Mapping[str, Any] | None:
    p = ctx.probes
    folder = runs_dir(root)
    name = newest_record(p.list_dir(folder))
    if name is None:
        return None
    try:
        doc = json.loads(p.read_text(folder / name))
    except ValueError:
        return None
    return doc if isinstance(doc, dict) else None


def _lock_since(ctx: Context, root: Path) -> datetime | None:
    p = ctx.probes
    path = root / "inbox" / LOCK_NAME
    try:
        if not p.is_file(path):
            return None
        info = parse_lock(p.read_text(path))
        started = info.started if info else p.mtime(path)
    except OSError:
        return None
    return datetime.fromtimestamp(started, UTC)


def check_unattended_inbox(ctx: Context) -> list[Result]:
    """The `Unattended inbox` line: with an [inbox] table and a readable hub root (the state is on
    the share, so a computer sees it as the container does)."""
    if not ctx.inbox_configured:
        return []
    p = ctx.probes
    root_text = ctx.config.settings.root or ctx.config.hub_root
    if not root_text or not p.is_dir(Path(root_text)):
        return []  # `Hub folder` says so
    root = Path(root_text)
    state_path = inbox_dir(root) / STATE_FILE
    state = parse_state(p.read_text(state_path))[0] if p.is_file(state_path) else None
    status, detail, fix = judge_unattended_inbox(
        state,
        _read_record(ctx, root),
        _lock_since(ctx, root),
        datetime.fromtimestamp(p.now(), UTC),
    )
    return [Result(UNATTENDED_NAME, G_INBOX, status, detail, fix)]


def collapse_inbox(results: Sequence[Result]) -> list[Result]:
    """Without an [inbox] table, the optional inbox-only items become one line. Items that
    are ok, warn or fail stay: the machine has that tool or folder, and a broken one is worth
    saying. Pure."""
    kept = [r for r in results if not (r.group in INBOX_GROUPS and r.status == OPTIONAL)]
    hidden = len(results) - len(kept)
    if not hidden:
        return kept
    return [
        *kept,
        Result(
            "Inbox",
            G_INBOX,
            OK,
            f"not set up on this machine ({hidden} optional item(s) not listed): only the "
            "machine that runs `warpzone inbox` (a computer or the hub container) needs its "
            "tools, Switch titledb and keys, "
            "DATs, hub folder and RetroAchievements key",
            "add an [inbox] table to the config to check them here "
            "(see examples/config.example.toml)",
        ),
    ]
