"""The nightly scrape, the pure decisions: which games a night takes, what it leaves in the
state, what it tells a person.

`plan_night` takes what `scrape.select` found for every system, the incomplete memory and the
not-found memory, and returns a `Night`: the games to scrape, in four tiers and under one cap
across systems. After the run, `judge_night` maps its `scrape.Summary` to an outcome and a kind
of trouble; the state (`State`, its pure `Change`s, `render_state` and `parse_state`) keeps the
streaks across nights; `news` compares the state before and after and `exit_code` turns the news
into the exit status; `build_record`, `notify_payload` and `should_notify` make the run record
and the webhook. Everything arrives as an argument: nothing here reads a disk, a clock or the
network. The disk side is `inbox_unattended_io`, which takes the folder and the functions
defined here. See development/design/scrape-unattended.md.

As in the inbox's state, a change to the state is a function `State -> State`: the writer applies
the same changes again to what it read again when the file changed in between.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import date, datetime
from enum import Enum
from pathlib import Path
from typing import Any

from warpzone import scrape
from warpzone.inbox_unattended import (
    WARNINGS_MAX,
    RunError,
    clean_text,
    is_record_id,
    parse_count,
    parse_error,
    parse_time,
    stamp,
)

# The kinds whose absence puts a game in tier 2; any other kind missing alone is tier 4.
CORE_KINDS: tuple[str, ...] = ("covers", "screenshots", "miximages")

# The tiers, in the order a night takes them.
NEW_GAME = 1  # no gamelist entry
MISSING_CORE = 2  # an entry, but a core kind is missing
NOT_FOUND_AGAIN = 3  # ScreenScraper did not know it, and its 30 days have passed
MISSING_EXTRA = 4  # an entry and every core kind; only `videos`, `backcovers`, ... missing


@dataclass(frozen=True)
class Ranked:
    """A pick with the system it belongs to and its tier."""

    tier: int
    system: str
    pick: scrape.Pick


@dataclass(frozen=True)
class Wave:
    """The chosen games of one tier across systems: one pass of the run. `systems` are the
    systems with a game in it, in name order, and `only` the primary library paths of the games."""

    tier: int
    systems: tuple[str, ...]
    only: frozenset[str]


@dataclass(frozen=True)
class Night:
    """What `plan_night` decided.

    `systems` are the systems with a chosen game, in name order. `chosen` maps each to the
    primary paths (library paths, `roms/<system>/<name>`) of its chosen games. `waves` are the
    chosen games by tier, in the order a run takes them: the run goes through one wave after the
    other, so a night cut short has done the first tiers of every system before the later ones
    of any. `deferred` counts, per system, the picks the cap left for another night; `backlog` is
    their total. `skipped_not_found` and `skipped_incomplete` count, per system (only those with
    some), the picks `select` left out because ScreenScraper did not know them recently and the
    ones the incomplete memory covers: neither is ever chosen or uses the cap."""

    systems: tuple[str, ...] = ()
    chosen: Mapping[str, frozenset[str]] = field(default_factory=dict)
    waves: tuple[Wave, ...] = ()
    deferred: Mapping[str, int] = field(default_factory=dict)
    skipped_not_found: Mapping[str, int] = field(default_factory=dict)
    skipped_incomplete: Mapping[str, int] = field(default_factory=dict)
    backlog: int = 0

    @property
    def empty(self) -> bool:
        return not self.chosen

    @property
    def games(self) -> int:
        return sum(len(paths) for paths in self.chosen.values())

    def only(self) -> frozenset[str]:
        """The chosen games as one set of library paths, which is what `scrape_io.Options.only`
        takes."""
        return frozenset(path for paths in self.chosen.values() for path in paths)


def tier_of(pick: scrape.Pick, record: scrape.NotFound | None, today: date) -> int:
    """The tier of a pick. `record` is the not-found record of its game, if any: one with the
    same size whose `NOT_FOUND_DAYS` have passed puts the game in tier 3 (a game with no entry,
    which a not-found game never has, would otherwise be a new one)."""
    if (
        record is not None
        and record.size == pick.game.size
        and (today - record.tried).days >= scrape.NOT_FOUND_DAYS
    ):
        return NOT_FOUND_AGAIN
    if not pick.has_entry:
        return NEW_GAME
    if any(kind in CORE_KINDS for kind in pick.missing):
        return MISSING_CORE
    return MISSING_EXTRA


def rank_picks(
    selections: Mapping[str, scrape.Selection],
    not_found: Mapping[str, Mapping[str, scrape.NotFound]],
    *,
    today: date,
) -> tuple[Ranked, ...]:
    """Every pick of every system in the order a night takes them: by tier, then system, then
    primary path."""
    ranked = [
        Ranked(
            tier_of(pick, not_found.get(system, {}).get(pick.game.gamelist), today), system, pick
        )
        for system, selection in selections.items()
        for pick in selection.picks
    ]
    ranked.sort(key=lambda r: (r.tier, r.system, r.pick.game.primary))
    return tuple(ranked)


def plan_night(
    selections: Mapping[str, scrape.Selection],
    incomplete: Mapping[str, Mapping[str, scrape.Incomplete]],
    not_found: Mapping[str, Mapping[str, scrape.NotFound]],
    *,
    max_games: int,
    today: date,
) -> Night:
    """The games a night scrapes: the first `max_games` of `rank_picks`, once the picks the
    incomplete memory covers (`scrape.skip_incomplete`) are set aside and counted.

    `selections` are the result of `scrape.select` per system, `incomplete` and `not_found` the
    two memories by system and gamelist path."""
    skipped: dict[str, int] = {}
    kept: dict[str, scrape.Selection] = {}
    for system, selection in selections.items():
        records = incomplete.get(system, {})
        picks = tuple(
            p
            for p in selection.picks
            if not scrape.skip_incomplete(p, records.get(p.game.gamelist))
        )
        if len(picks) < len(selection.picks):
            skipped[system] = len(selection.picks) - len(picks)
        kept[system] = scrape.Selection(picks, selection.skipped_not_found)
    ranked = rank_picks(kept, not_found, today=today)
    taken, left = ranked[: max(0, max_games)], ranked[max(0, max_games) :]
    chosen: dict[str, set[str]] = {}
    by_tier: dict[int, dict[str, set[str]]] = {}
    for item in taken:
        chosen.setdefault(item.system, set()).add(item.pick.game.primary)
        by_tier.setdefault(item.tier, {}).setdefault(item.system, set()).add(item.pick.game.primary)
    deferred: dict[str, int] = {}
    for item in left:
        deferred[item.system] = deferred.get(item.system, 0) + 1
    waves = tuple(
        Wave(
            tier,
            tuple(sorted(paths)),
            frozenset(path for group in paths.values() for path in group),
        )
        for tier, paths in sorted(by_tier.items())
    )
    return Night(
        tuple(sorted(chosen)),
        {system: frozenset(paths) for system, paths in sorted(chosen.items())},
        waves,
        dict(sorted(deferred.items())),
        {s: n.skipped_not_found for s, n in sorted(selections.items()) if n.skipped_not_found},
        dict(sorted(skipped.items())),
        len(left),
    )


def incomplete_skipper(
    incomplete: Mapping[str, Mapping[str, scrape.Incomplete]],
) -> Callable[[scrape.Pick], bool]:
    """The `skip_incomplete` input of `scrape_io.run_held` for a night: the same rule as
    `plan_night`, applied to the picks the run selects again under the lock. A pick's system is
    the one in its primary path (`roms/<system>/...`)."""

    def skip(pick: scrape.Pick) -> bool:
        parts = pick.game.primary.split("/")
        system = parts[1] if len(parts) > 2 and parts[0] == "roms" else ""
        return scrape.skip_incomplete(pick, incomplete.get(system, {}).get(pick.game.gamelist))

    return skip


# ------------------------------------------------------------------ outcomes and troubles

STATE_VERSION = 1
RECORD_VERSION = 1
SCRAPE_DIR = "catalog/scrape"  # below the hub root, outside every Syncthing folder
STATE_FILE = "state.json"
RUNS_DIR = "runs"
STATE_SHOWN = f"{SCRAPE_DIR}/{STATE_FILE}"
PATHS_MAX = 200  # added and filled game paths kept in a run record, each list
QUOTA_WARN_NIGHTS = 3  # nights of quota in a row that make doctor warn (the quota is not news)

DONE = "done"
BUDGET = "budget"
QUOTA = "quota"
BUSY = "busy"
STOPPED = "stopped"
FAILED = "failed"
NOTHING = "nothing"
OUTCOMES: tuple[str, ...] = (DONE, BUDGET, QUOTA, BUSY, STOPPED, FAILED, NOTHING)

# The kinds of trouble, which is what a streak counts. A kind that is the name of a
# `scrape.StopReason` (blacklisted, closed, busy, network, quota) is that condition of
# ScreenScraper; `lock` is a lock that stayed busy all night or was lost; `failed` is a system
# whose batches failed; `died` is a run that never wrote its end.
BLACKLISTED = "blacklisted"
NETWORK = "network"
LOCK = "lock"
DIED = "died"
CLOSED = "closed"
TROUBLES: tuple[str, ...] = (BLACKLISTED, NETWORK, BUSY, FAILED, LOCK, DIED, CLOSED, QUOTA)

# Nights in a row after which a streak is news; None is never (the quota is only shown by doctor).
NEWS_AFTER: Mapping[str, int | None] = {
    BLACKLISTED: 1,
    NETWORK: 2,
    BUSY: 2,
    FAILED: 2,
    LOCK: 2,
    DIED: 2,
    CLOSED: 3,
    QUOTA: None,
}


class LockOutcome(Enum):
    """What the night's wait for the lock came to."""

    TAKEN = "taken"
    BUSY = "busy"  # still live after `LOCK_WAIT_SECONDS`


@dataclass(frozen=True)
class Judged:
    """A night's outcome, the kind of trouble it adds to a streak (None for a night that clears
    it) and the systems involved (those whose batches failed)."""

    outcome: str
    kind: str | None = None
    systems: tuple[str, ...] = ()


# A stop of the run before every game was tried, with the kind of trouble it is.
_STOP_KINDS: Mapping[scrape.StopReason, str] = {
    scrape.StopReason.BLACKLISTED: BLACKLISTED,
    scrape.StopReason.CLOSED: CLOSED,
    scrape.StopReason.BUSY: BUSY,
    scrape.StopReason.NETWORK: NETWORK,
    scrape.StopReason.LOCK_LOST: LOCK,
}


def judge_night(summary: scrape.Summary | None, lock: LockOutcome = LockOutcome.TAKEN) -> Judged:
    """The outcome of a night and its kind of trouble, from the run's summary (None when no
    Skyscraper ran) and what the wait for the lock came to.

    - The lock stayed busy: `busy`, trouble `lock`.
    - No summary (nothing to do): `nothing`.
    - The quota was reached: `quota`, trouble `quota` (kept as a streak for doctor, never news).
    - Any other stop of the run before every game was tried, except the time budget:
      `stopped`, with the trouble named after it (`blacklisted`, `closed`, `busy`, `network`,
      or `lock` when the lock was lost).
    - No such stop but a system whose batches failed (or a run that counts as failed): `failed`,
      trouble `failed`, with the systems. This holds when the time budget ran out too.
    - The time budget ran out: `budget`, no trouble.
    - Otherwise `done`, no trouble.

    A stop wins over failed systems: the failures are most likely its effect."""
    if lock is LockOutcome.BUSY:
        return Judged(BUSY, LOCK)
    if summary is None:
        return Judged(NOTHING)
    systems = summary.failed_systems
    if summary.stop is scrape.StopReason.QUOTA:
        return Judged(QUOTA, QUOTA, systems)
    kind = _STOP_KINDS.get(summary.stop) if summary.stop is not None else None
    if kind is not None:
        return Judged(STOPPED, kind, systems)
    if summary.failed or summary.failed_systems:
        return Judged(FAILED, FAILED, systems)
    if summary.stop is scrape.StopReason.DEADLINE:
        return Judged(BUDGET)
    return Judged(DONE)


# ------------------------------------------------------------------ the state


def scrape_dir(root: Path) -> Path:
    """`catalog/scrape/` of the hub."""
    return root / SCRAPE_DIR


def state_path(root: Path) -> Path:
    return scrape_dir(root) / STATE_FILE


def runs_dir(root: Path) -> Path:
    return scrape_dir(root) / RUNS_DIR


@dataclass(frozen=True)
class Running:
    """A run that holds the lock and has not written its end: a later night that finds it set
    knows the run died."""

    run: str
    started: datetime


@dataclass(frozen=True)
class Last:
    """The last run that wrote its end."""

    run: str
    finished: datetime
    outcome: str


@dataclass(frozen=True)
class Backlog:
    """The games a night left for the next ones, and when it was counted."""

    left: int
    at: datetime


@dataclass(frozen=True)
class Trouble:
    """A streak: the same kind of trouble on `count` nights in a row, since `first`. `notified`
    is set once it has been reported, so a streak is news only once."""

    kind: str
    systems: tuple[str, ...]
    first: datetime
    count: int = 1
    notified: bool = False


@dataclass(frozen=True)
class State:
    checked: datetime | None = None
    running: Running | None = None
    last: Last | None = None
    backlog: Backlog | None = None
    error: RunError | None = None
    trouble: Trouble | None = None


Change = Callable[[State], State]


def update(state: State, *changes: Change) -> State:
    """The state after the changes, applied in order."""
    for change in changes:
        state = change(state)
    return state


def with_checked(now: datetime) -> Change:
    return lambda state: replace(state, checked=now)


def _streak(state: State, kind: str, systems: tuple[str, ...], now: datetime) -> Trouble:
    old = state.trouble
    if old is not None and old.kind == kind:
        return replace(old, systems=systems, count=old.count + 1)
    return Trouble(kind, systems, now, 1, False)


def with_started(run: str, now: datetime) -> Change:
    """The run took the lock. A `running` already set is a run that died: that night counts
    for the trouble `died` (a streak of its own, which replaces another kind), and the new run
    takes its place."""

    def change(state: State) -> State:
        trouble = state.trouble
        if state.running is not None:
            trouble = _streak(state, DIED, (), now)
        return replace(state, running=Running(run, now), trouble=trouble)

    return change


def with_night(
    run: str,
    finished: datetime,
    outcome: str,
    kind: str | None,
    systems: tuple[str, ...] = (),
    left: int | None = None,
    *,
    died: bool = False,
) -> Change:
    """The end of a night: `last`, the backlog (`left` games, kept as it was when None) and the
    streak. A `kind` of trouble raises the streak of that kind, or starts one (and so replaces
    a streak of another kind: any other trouble ends the quota's own streak this way). No kind
    (`done`, `budget`, `nothing`) clears it, except that a night that found the run before it
    dead (`died`) keeps the `died` count `with_started` made. The `running` note is cleared
    only when it is this run's."""

    def change(state: State) -> State:
        trouble = state.trouble
        if kind is not None:
            trouble = _streak(state, kind, systems, finished)
        elif not died:
            trouble = None
        running = state.running
        if running is not None and running.run == run:
            running = None
        backlog = state.backlog if left is None else Backlog(left, finished)
        return replace(
            state,
            running=running,
            last=Last(run, finished, outcome),
            backlog=backlog,
            trouble=trouble,
        )

    return change


def with_notified() -> Change:
    """Mark the streak as reported, so it is news only once."""

    def change(state: State) -> State:
        if state.trouble is None or state.trouble.notified:
            return state
        return replace(state, trouble=replace(state.trouble, notified=True))

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
    """Clear a stored error when this run got past its step (`reached` is the last step the run
    started, so an error of step `n` is cleared when `reached > n`)."""

    def change(state: State) -> State:
        if state.error is not None and reached > state.error.step:
            return replace(state, error=None)
        return state

    return change


# ------------------------------------------------------------------ the state file


def render_state(state: State) -> str:
    error, trouble = state.error, state.trouble
    doc: dict[str, object] = {
        "v": STATE_VERSION,
        "checked": None if state.checked is None else stamp(state.checked),
        "running": None
        if state.running is None
        else {"run": state.running.run, "started": stamp(state.running.started)},
        "last": None
        if state.last is None
        else {
            "run": state.last.run,
            "finished": stamp(state.last.finished),
            "outcome": state.last.outcome,
        },
        "backlog": None
        if state.backlog is None
        else {"left": state.backlog.left, "at": stamp(state.backlog.at)},
        "error": None
        if error is None
        else {
            "message": error.message,
            "step": error.step,
            "first": stamp(error.first),
            "count": error.count,
            "config": error.config,
        },
        "trouble": None
        if trouble is None
        else {
            "kind": trouble.kind,
            "systems": list(trouble.systems),
            "first": stamp(trouble.first),
            "count": trouble.count,
            "notified": trouble.notified,
        },
    }
    return json.dumps(doc, indent=2, sort_keys=True) + "\n"


def _parse_running(raw: object) -> Running | None:
    if not isinstance(raw, dict):
        return None
    run, started = raw.get("run"), parse_time(raw.get("started"))
    if not isinstance(run, str) or not run or started is None:
        return None
    return Running(run, started)


def _parse_last(raw: object) -> Last | None:
    if not isinstance(raw, dict):
        return None
    run, finished, outcome = raw.get("run"), parse_time(raw.get("finished")), raw.get("outcome")
    if not isinstance(run, str) or not run or finished is None or outcome not in OUTCOMES:
        return None
    return Last(run, finished, str(outcome))


def _parse_backlog(raw: object) -> Backlog | None:
    if not isinstance(raw, dict):
        return None
    left, at = parse_count(raw.get("left")), parse_time(raw.get("at"))
    if left is None or at is None:
        return None
    return Backlog(left, at)


def _parse_trouble(raw: object) -> Trouble | None:
    if not isinstance(raw, dict):
        return None
    kind, first = raw.get("kind"), parse_time(raw.get("first"))
    count, systems = parse_count(raw.get("count", 1), 1), raw.get("systems", [])
    notified = raw.get("notified", False)
    if kind not in TROUBLES or first is None or count is None or not isinstance(notified, bool):
        return None
    if not isinstance(systems, list) or not all(isinstance(x, str) for x in systems):
        return None
    return Trouble(str(kind), tuple(systems), first, count, notified)


def parse_state(text: str | None) -> tuple[State, str | None]:
    """The state in `text` and a warning, or None when there is nothing to warn about.

    A missing file (`text` None) is an empty state with no warning. A file that is not JSON, not
    an object or of another version is an empty state with a warning. A damaged part is left
    out with a warning and the rest is kept."""
    if text is None:
        return State(), None
    try:
        doc = json.loads(text)
    except (ValueError, RecursionError):
        return State(), "the scrape state file cannot be parsed; starting from an empty one"
    if not isinstance(doc, dict) or doc.get("v") != STATE_VERSION:
        return State(), "the scrape state file is of an unknown form; starting from an empty one"
    damaged: list[str] = []

    def part(name: str, parse: Callable[[object], Any]) -> Any:
        raw = doc.get(name)
        if raw is None:
            return None
        found = parse(raw)
        if found is None:
            damaged.append(name)
        return found

    state = State(
        part("checked", parse_time),
        part("running", _parse_running),
        part("last", _parse_last),
        part("backlog", _parse_backlog),
        part("error", parse_error),
        part("trouble", _parse_trouble),
    )
    warning = None
    if damaged:
        warning = (
            "the scrape state file is damaged in " + ", ".join(damaged) + "; those were dropped"
        )
    return state, warning


# ------------------------------------------------------------------ news and exit codes


@dataclass(frozen=True)
class NewsItem:
    """One thing a person has not been told yet. `source` is "trouble" (`kind` is the kind of
    trouble, `nights` the length of its streak, `systems` the systems involved) or "error"
    (`kind` is the message and `config` tells a configuration error from a runtime one)."""

    source: str
    kind: str
    nights: int = 1
    config: bool = False
    systems: tuple[str, ...] = ()


def news(before: State, after: State) -> list[NewsItem]:
    """What a person has not been told yet: `before` is the state as the run read it, `after`
    the one it writes. A new run-level error is news (one with the message already stored is
    not: it only raised `count`, and one that got past its step was cleared, so the same error
    later is news again). A streak is news once it reaches `NEWS_AFTER` nights and has not been
    marked `notified`."""
    found: list[NewsItem] = []
    error = after.error
    if error is not None and (before.error is None or before.error.message != error.message):
        found.append(NewsItem("error", error.message, error.count, error.config))
    trouble = after.trouble
    if trouble is not None and not trouble.notified:
        threshold = NEWS_AFTER.get(trouble.kind)
        if threshold is not None and trouble.count >= threshold:
            found.append(NewsItem("trouble", trouble.kind, trouble.count, False, trouble.systems))
    return found


def exit_code(found: Sequence[NewsItem]) -> int:
    """`0` with no news, `2` when the news holds a configuration error, else `1`."""
    if any(item.source == "error" and item.config for item in found):
        return 2
    return 1 if found else 0


# ------------------------------------------------------------------ run records and the webhook

_COUNTS = (
    "added",
    "filled",
    "not_found",
    "unchanged",
    "skipped_not_found",
    "skipped_incomplete",
    "deferred",
)


def _library_path(system: str, rel: str) -> str:
    return f"library/roms/{system}/{rel}"


def build_record(
    summary: scrape.Summary,
    *,
    run_id: str,
    started: datetime,
    finished: datetime,
    outcome: str,
    status: int,
    deferred: Mapping[str, int] | None = None,
    skipped_not_found: Mapping[str, int] | None = None,
    skipped_incomplete: Mapping[str, int] | None = None,
    notified: bool = False,
    warnings: Sequence[str] = (),
    roots: Sequence[str] = (),
    secrets: Sequence[str] = (),
) -> dict[str, Any]:
    """The run record, from the run's `Summary` and what the night's plan deferred and skipped
    per system (the run only sees the games the plan chose, so it cannot count those). A system
    that only has deferred or skipped games is in the record with zero counts. Pure.
    The paths of the added and filled games are library paths below the hub root
    (`library/roms/<system>/<name>`), `PATHS_MAX` of each; the warnings are the systems' and the
    `warnings` given, `WARNINGS_MAX` of them. Every text goes through `clean_text`, so the
    record holds no absolute path, no key and no URL a message carried."""

    def clean(text: str) -> str:
        return clean_text(text, roots, secrets)

    left = dict(deferred or {})
    missing = dict(skipped_not_found or {})
    covered = dict(skipped_incomplete or {})
    systems: dict[str, dict[str, int]] = {}
    added: list[str] = []
    filled: list[str] = []
    notes: list[str] = []
    for item in summary.systems:
        systems[item.system] = {
            "added": item.added,
            "filled": item.filled,
            "not_found": item.not_found,
            "unchanged": item.unchanged,
            "skipped_not_found": missing.pop(item.system, 0),
            "skipped_incomplete": covered.pop(item.system, 0),
            "deferred": left.pop(item.system, 0),
        }
        added += [clean(_library_path(item.system, p)) for p in item.added_paths]
        filled += [clean(_library_path(item.system, p)) for p in item.filled_paths]
        notes += [clean(w) for w in item.warnings]
    for name in {*left, *missing, *covered}:  # deferred or skipped in the plan, never reached
        systems[name] = {key: 0 for key in _COUNTS} | {
            "skipped_not_found": missing.get(name, 0),
            "skipped_incomplete": covered.get(name, 0),
            "deferred": left.get(name, 0),
        }
    notes += [clean(w) for w in warnings]
    return {
        "v": RECORD_VERSION,
        "id": run_id,
        "started": stamp(started),
        "finished": stamp(finished),
        "outcome": outcome,
        "systems": dict(sorted(systems.items())),
        "added": added[:PATHS_MAX],
        "filled": filled[:PATHS_MAX],
        "failed_systems": list(summary.failed_systems),
        "stop": None if summary.stop is None else summary.stop.value,
        "budget_spent": summary.budget_spent,
        "warnings": notes[:WARNINGS_MAX],
        "exit": status,
        "notified": notified,
    }


def render_record(record: Mapping[str, Any]) -> str:
    """The text of a record file."""
    return json.dumps(record, indent=2, ensure_ascii=False) + "\n"


def _strings(raw: object, cap: int | None = None) -> list[str] | None:
    if not isinstance(raw, list) or not all(isinstance(x, str) for x in raw):
        return None
    return list(raw[:cap])


def parse_record(text: str | None) -> dict[str, Any] | None:
    """The record in `text`, or None when it is not one (not JSON, not an object, another
    version, an id or an outcome that is not valid, a time that is not a time). A system whose
    counts are damaged is left out, and the lists are capped again."""
    if text is None:
        return None
    try:
        doc = json.loads(text)
    except (ValueError, RecursionError):
        return None
    if not isinstance(doc, dict) or doc.get("v") != RECORD_VERSION:
        return None
    if not is_record_id(doc.get("id")) or doc.get("outcome") not in OUTCOMES:
        return None
    started, finished = parse_time(doc.get("started")), parse_time(doc.get("finished"))
    raw_systems = doc.get("systems")
    if started is None or finished is None or not isinstance(raw_systems, dict):
        return None
    systems: dict[str, dict[str, int]] = {}
    for name, raw in raw_systems.items():
        counts = {k: parse_count(raw.get(k)) for k in _COUNTS} if isinstance(raw, dict) else {}
        if counts and None not in counts.values():
            systems[name] = {k: v for k, v in counts.items() if v is not None}
    added = _strings(doc.get("added", []), PATHS_MAX)
    filled = _strings(doc.get("filled", []), PATHS_MAX)
    failed = _strings(doc.get("failed_systems", []))
    notes = _strings(doc.get("warnings", []), WARNINGS_MAX)
    stop, spent, status = doc.get("stop"), doc.get("budget_spent", False), doc.get("exit", 0)
    notified = doc.get("notified", False)
    if added is None or filled is None or failed is None or notes is None:
        return None
    if stop is not None and not isinstance(stop, str):
        return None
    if not isinstance(spent, bool) or not isinstance(notified, bool):
        return None
    if isinstance(status, bool) or not isinstance(status, int):
        return None
    return {
        "v": RECORD_VERSION,
        "id": doc["id"],
        "started": stamp(started),
        "finished": stamp(finished),
        "outcome": doc["outcome"],
        "systems": dict(sorted(systems.items())),
        "added": added,
        "filled": filled,
        "failed_systems": failed,
        "stop": stop,
        "budget_spent": spent,
        "warnings": notes,
        "exit": status,
        "notified": notified,
    }


def should_notify(found: Sequence[NewsItem]) -> bool:
    """Whether the run sends the webhook: only for news, never for a night that only added art."""
    return bool(found)


def notify_payload(
    record: Mapping[str, Any], found: Sequence[NewsItem], left: int | None
) -> dict[str, Any]:
    """The JSON the webhook gets: the run, the counts over every system, what is `left` (None
    when no night has counted it yet) and the `problem`, which is the kind of the first streak in
    the news, or `error` for a run-level error. It is built from the record and the news kinds,
    so it holds no URL, key or path (the text of an error is not in it)."""
    totals = {
        key: sum(counts[key] for counts in record["systems"].values())
        for key in ("added", "filled", "not_found")
    }
    problems = [i.kind for i in found if i.source == "trouble"]
    if not problems and any(i.source == "error" for i in found):
        problems = ["error"]
    return {
        "source": "scrape",
        "mode": "unattended",
        "run": record["id"],
        **totals,
        "left": left,
        "problem": problems[0] if problems else None,
    }
