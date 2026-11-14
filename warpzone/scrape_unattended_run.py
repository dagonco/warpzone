"""`warpzone scrape --unattended`: one night of the scheduled scrape, with nobody watching.

The decisions are pure (`scrape_unattended`); the disk, the clock and the lock are handled here,
with the state file, the run records and the webhook through the helpers the unattended inbox
uses (`inbox_unattended_io`, `notify`). A night goes through the steps of "Run steps" in
development/design/scrape-unattended.md and stops at the first that says so: the usage checks
(made by `cli`), the hub layout, what the run needs, the plan without the lock (nothing to do
exits at once, with no lock and no Skyscraper), the lock with its wait, the scrape under the lock
and the finish. Every state change is a read-modify-write of `catalog/scrape/state.json`. The
exit code is 0 unless the night has news (see `scrape_unattended.news`).
"""

from __future__ import annotations

import shutil
import time
from collections.abc import Callable
from contextlib import ExitStack
from dataclasses import dataclass, field, replace
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

from warpzone import inbox, inbox_lock, scrape, scrape_io
from warpzone.errors import UsageError
from warpzone.inbox_core import InboxError
from warpzone.inbox_unattended import clean_text, record_id
from warpzone.inbox_unattended_io import (
    StateFile,
    lower_priority,
    on_memory,
    purge_records,
    read_file,
    update_file_checked,
    write_record,
)
from warpzone.notify import NotifyError, notify
from warpzone.report import Reporter
from warpzone.scrape_unattended import (
    FAILED,
    NOTHING,
    STATE_SHOWN,
    Backlog,
    Change,
    Judged,
    LockOutcome,
    NewsItem,
    Night,
    State,
    build_record,
    exit_code,
    incomplete_skipper,
    judge_night,
    news,
    notify_payload,
    parse_state,
    plan_night,
    render_record,
    render_state,
    runs_dir,
    should_notify,
    state_path,
    update,
    with_checked,
    with_error,
    with_error_cleared,
    with_night,
    with_notified,
    with_started,
)

# The steps of "Run steps": an error keeps the one where it failed. Step 1, the usage checks, is
# `cli`'s and keeps nothing.
STEP_LAYOUT, STEP_CHECK, STEP_PLAN, STEP_LOCK, STEP_RUN, STEP_FINISH = range(2, 8)

DEFAULT_MAX_GAMES = 500
DEFAULT_MAX_MINUTES = 180
LOCK_WAIT_SECONDS = 1800  # how long a night waits for a busy lock
POLL_SECONDS = 60  # between two looks at it
SCRAPE_STATE: StateFile[State] = StateFile(state_path, parse_state, render_state, STATE_SHOWN)

LEGACY_MESSAGE = (
    "inbox/.warpzone.lock is the lock of an older warpzone and is stale by its rules, which an "
    "unattended run never takes over. If no other run is working on the inbox, run "
    "`warpzone scrape --force-lock` yourself"
)
LOST_MESSAGE = "the inbox lock is no longer this run's; the scrape stopped"
DIED_WARNING = "the earlier nightly scrape ({run}, started {started}) did not finish"


@dataclass(frozen=True)
class Setup:
    """What `cli` hands the night: where the hub is, how to reach Skyscraper, the budget and the
    webhook, and the seams a test replaces (the clock, the sleep, the search for a program)."""

    root: Path
    systems: tuple[str, ...]
    explicit: bool  # the systems were named with --system
    skyscraper: str | None  # [inbox] skyscraper as configured; None to look on the path
    timeout: float  # seconds one Skyscraper call may run
    work_dir: Path | None = None
    cache_dir: Path | None = None
    config_ini: Path | None = None
    skyscraper_dir: Path | None = None  # WARPZONE_SKYSCRAPER_DIR, when set
    notify_url: str | None = field(default=None, repr=False)
    max_games: int = DEFAULT_MAX_GAMES
    max_minutes: int = DEFAULT_MAX_MINUTES
    now: Callable[[], float] = time.time
    today: Callable[[], date] = date.today
    sleep: Callable[[float], None] = time.sleep
    which: Callable[[str], str | None] = shutil.which


def run_unattended(setup: Setup, rep: Reporter) -> int:
    """One night. Returns the exit code: 0 with no news, 1 for news, 2 for a new configuration
    error. A problem met before the hub root is known to be one raises `UsageError` (exit 2)."""
    return _Night(setup, rep).go()


def _why(exc: Exception) -> str:
    return (getattr(exc, "strerror", None) or str(exc)).strip()


def _stamp(moment: datetime) -> str:
    return moment.strftime("%Y-%m-%d %H:%M UTC")


class _Night:
    def __init__(self, setup: Setup, rep: Reporter) -> None:
        self.setup = setup
        self.rep = rep
        self.root = setup.root
        self.started = self.clock()
        self.run_id = record_id(self.started)
        self.deadline = setup.now() + setup.max_minutes * 60
        self.before = State()  # the state as this night read it at its start
        self.state = State()  # the state as this night last wrote it
        self.night = Night()
        self.options: scrape_io.Options | None = None
        self.announced: list[NewsItem] = []  # news sent when the run started, not again at its end
        self.roots = [str(self.root), str(self.root.resolve())]
        self.secrets = [setup.notify_url] if setup.notify_url else []

    def clock(self) -> datetime:
        return datetime.fromtimestamp(self.setup.now(), UTC)

    # ------------------------------------------------------------ state and outcome

    def commit(self, *changes: Change) -> State:
        state, warnings, _ = update_file_checked(self.root, SCRAPE_STATE, *changes)
        for warning in warnings:
            self.rep.warn(warning)
        self.state = state
        return state

    def clean(self, text: str) -> str:
        return clean_text(text, self.roots, self.secrets)

    def fail(self, message: str, step: int, *, config: bool = True) -> int:
        """A run-level error: kept in the state with its step. Meeting the same message again
        raises its count and is not news."""
        message = self.clean(message)
        self.rep.warn(f"scrape: {message}")
        now = self.clock()
        return self.conclude(
            with_checked(now), with_error(message, step, now, config), left=self.known_left()
        )

    def known_left(self) -> int | None:
        """What is left for the next nights as far as this night knows: its own plan once it has
        made one, else the backlog the state holds (None when no night has counted it)."""
        if not self.night.empty:
            return self.night.backlog
        backlog = self.state.backlog
        return None if backlog is None else backlog.left

    def record(
        self,
        summary: scrape.Summary | None,
        run_id: str,
        outcome: str,
        status: int,
        notified: bool,
    ) -> dict[str, Any]:
        return build_record(
            summary or scrape.Summary(),
            run_id=run_id,
            started=self.started,
            finished=self.clock(),
            outcome=outcome,
            status=status,
            deferred=self.night.deferred,
            skipped_not_found=self.night.skipped_not_found,
            skipped_incomplete=self.night.skipped_incomplete,
            notified=notified,
            warnings=self.rep.warnings,
            roots=self.roots,
            secrets=self.secrets,
        )

    def conclude(
        self,
        *changes: Change,
        summary: scrape.Summary | None = None,
        outcome: str = FAILED,
        left: int | None = 0,
        ran: bool = False,
    ) -> int:
        """Finish the night: the news and the exit code, the webhook, the record (when Skyscraper
        started or there is news), then the state. A record or a webhook that fails is a warning."""
        found = news(self.before, update(self.state, *changes))
        code = exit_code([*self.announced, *found])

        def build(run_id: str, notified: bool) -> dict[str, Any]:
            return self.record(summary, run_id, outcome, code, notified)

        sent = self.send(found, build(self.run_id, False), left)
        if ran or found:
            self.write(lambda run_id: build(run_id, sent or bool(self.announced)))
        extra: list[Change] = []
        if any(item.source == "trouble" for item in found):
            extra.append(with_notified())  # reported, whether or not a webhook was sent
        try:
            self.commit(*changes, *extra)
        except InboxError as exc:
            self.rep.warn(str(exc))
        return code

    def send(self, found: list[NewsItem], record: dict[str, Any], left: int | None) -> bool:
        url = self.setup.notify_url
        if not url or not should_notify(found):
            return False
        try:
            notify(url, notify_payload(record, found, left))
        except NotifyError as exc:
            self.rep.warn(self.clean(str(exc)))
            return False
        return True

    def write(self, build: Callable[[str], dict[str, Any]]) -> None:
        folder = runs_dir(self.root)
        try:
            record = write_record(
                self.root, self.started, build, folder=folder, render=render_record
            )
        except (OSError, ValueError) as exc:  # the state and the webhook still tell what happened
            self.rep.warn(f"run record: not written ({_why(exc)})")
            return
        self.rep.out(f"run record: catalog/scrape/runs/{record['id']}.json")
        try:
            purge_records(self.root, folder=folder)
        except OSError as exc:
            self.rep.warn(f"run record: old records not deleted ({_why(exc)})")

    # ------------------------------------------------------------ steps 2 to 4

    def go(self) -> int:
        self.before, warning = scrape_state(self.root)
        self.state = self.before
        if warning:
            self.rep.warn(warning)
        problem = inbox.check_layout(self.root)
        if not (self.root / "catalog").is_dir():  # not the hub: write nothing there
            raise UsageError(
                problem
                or "the hub root has no catalog/ folder; run `warpzone catalog`, or is --root "
                "the hub root?"
            )
        if problem:
            return self.fail(problem, STEP_LAYOUT)
        try:
            self.options = self.check()
        except UsageError as exc:
            return self.fail(str(exc), STEP_CHECK)
        return self.plan(self.options)

    def check(self) -> scrape_io.Options:
        """Step 3: what the run needs. Raises `UsageError` with the reason it cannot go on."""
        setup = self.setup
        program = scrape_io.find_skyscraper(setup.skyscraper, setup.which)
        if program is None:
            raise UsageError(
                "Skyscraper not found: put it on PATH or set `skyscraper` in the [inbox] table "
                "(https://github.com/Gemba/skyscraper/releases)"
            )
        if setup.skyscraper_dir is not None and not setup.skyscraper_dir.is_dir():
            raise UsageError(
                f"{scrape.SKYSCRAPER_DIR_ENV} is set but is not a folder: mount the folder of "
                "Skyscraper's config.ini and cache there"
            )
        options = scrape_io.Options(
            root=self.root,
            skyscraper=program,
            systems=setup.systems,
            explicit=setup.explicit,
            timeout=setup.timeout,
            work_dir=setup.work_dir,
            cache_dir=setup.cache_dir,
            config_ini=setup.config_ini,
            as_warning=True,
            now=setup.now,
            today=setup.today,
        )
        if on_memory(options.scratch):
            raise UsageError(
                "the scratch folder is on a memory filesystem (tmpfs or ramfs); set [inbox] "
                "work_dir to a folder on a mounted volume"
            )
        return options

    def plan(self, options: scrape_io.Options) -> int:
        """Step 4: the games of the night, decided without the lock. With none, only the time of
        the check is written (and what the night got past is cleared): no lock, no Skyscraper."""
        memory, incomplete = scrape_io.read_memory(options)
        selections, problems = scrape_io.select_all(options, memory)
        for problem in problems:
            self.rep.warn(problem)
        self.night = plan_night(
            selections,
            incomplete,
            memory,
            max_games=self.setup.max_games,
            today=options.today(),
        )
        if not self.night.empty:
            return self.take_lock(options)
        now = self.clock()
        changes: list[Change] = [with_checked(now), with_error_cleared(STEP_PLAN)]
        state = self.state
        if state.trouble is not None:  # a night with nothing to do is a night with no trouble
            changes.append(lambda s: replace(s, trouble=None))
        if state.backlog is not None and state.backlog.left:
            changes.append(lambda s: replace(s, backlog=Backlog(0, now)))
        return self.conclude(*changes, outcome=NOTHING)

    # ------------------------------------------------------------ step 5

    def take_lock(self, options: scrape_io.Options) -> int:
        """Take the lock, waiting for a busy one: a look every `POLL_SECONDS` for at most
        `LOCK_WAIT_SECONDS`, the first reading of a lock being the `previous` the verdict of an
        unattended run compares with (a lock silent for 10 minutes is taken over)."""
        setup = self.setup
        inbox_dir = self.root / "inbox"
        previous: inbox_lock.LockReading | None = None
        first: float | None = None
        while True:
            try:
                reading = inbox_lock.look(inbox_dir, setup.now())
            except InboxError as exc:  # a lock that cannot be read is trouble of the run
                return self.fail(str(exc), STEP_LOCK, config=False)
            if reading is not None:
                verdict = inbox_lock.judge(
                    reading,
                    inbox_lock.hostname(),
                    inbox_lock.pid_alive,
                    unattended=True,
                    previous=previous,
                )
                if verdict.state == inbox_lock.LEGACY:
                    return self.fail(LEGACY_MESSAGE, STEP_LOCK)
                if verdict.state in (inbox_lock.LIVE, inbox_lock.UNKNOWN):
                    previous = self.noting(previous, reading)
                    first = reading.looked if first is None else first
                    if self.waited(first, reading):
                        return self.busy(reading)
                    continue
            stack = ExitStack()
            try:
                held = stack.enter_context(
                    inbox_lock.hold(
                        inbox_dir,
                        force=False,
                        now=setup.now,
                        out=self.rep.warn,  # a lock taken over is a warning: it shows in the record
                        mode=inbox_lock.UNATTENDED,
                        task=inbox_lock.SCRAPE,
                        previous=previous,
                    )
                )
            except inbox_lock.LockBusy as exc:  # made between the look and taking it
                if exc.reading is None:
                    return self.fail(str(exc), STEP_LOCK, config=False)
                previous = self.noting(previous, exc.reading)
                first = exc.reading.looked if first is None else first
                if self.waited(first, exc.reading):
                    return self.busy(exc.reading)
                continue
            except inbox_lock.LockStale:
                return self.fail(LEGACY_MESSAGE, STEP_LOCK)
            except InboxError as exc:
                return self.fail(str(exc), STEP_LOCK)
            with stack:
                return self.work(options, held)

    @staticmethod
    def noting(
        previous: inbox_lock.LockReading | None, reading: inbox_lock.LockReading
    ) -> inbox_lock.LockReading:
        """The reading to compare the next look with: the first of this lock, or the last one at
        which its modification time moved (so silence is counted from the last sign of life)."""
        if (
            previous is None
            or not inbox_lock.same_run(previous.info, reading.info)
            or previous.mtime != reading.mtime
        ):
            return reading
        return previous

    def waited(self, first: float, reading: inbox_lock.LockReading) -> bool:
        """Whether the wait is over (True), else sleeps until the next look."""
        if self.setup.now() - first >= LOCK_WAIT_SECONDS:
            return True
        self.setup.sleep(POLL_SECONDS)
        return False

    def busy(self, reading: inbox_lock.LockReading) -> int:
        """The lock was still live after the wait: the night is `busy`, trouble `lock`."""
        self.rep.out(f"busy: {inbox_lock.describe(reading.info, reading.mtime, reading.looked)}")
        now = self.clock()
        judged = judge_night(None, LockOutcome.BUSY)
        return self.conclude(
            with_checked(now),
            with_error_cleared(STEP_LOCK),
            with_night(self.run_id, now, judged.outcome, judged.kind),
            outcome=judged.outcome,
            left=self.night.backlog,
        )

    # ------------------------------------------------------------ steps 6 and 7

    def work(self, options: scrape_io.Options, held: inbox_lock.Held) -> int:
        rep = self.rep
        started = self.clock()
        previous = self.state.running
        died = previous is not None
        if previous is not None:
            rep.warn(DIED_WARNING.format(run=previous.run, started=_stamp(previous.started)))
        try:
            self.commit(
                with_checked(started),
                with_started(self.run_id, started),
                with_error_cleared(STEP_RUN),
            )
        except InboxError as exc:  # without `running` a run that dies is not noticed: say so
            rep.warn(str(exc))
        self.announce_died()
        failure = lower_priority()
        if failure:
            rep.warn(f"could not lower the CPU priority: {failure}")
        summary: scrape.Summary | None = None
        failed: str | None = None
        try:
            _, incomplete = scrape_io.read_memory(options)
            for wave in self.night.waves:  # tier by tier, each across the systems
                part = scrape_io.run_held(
                    replace(options, systems=wave.systems, explicit=False, only=wave.only),
                    rep,
                    deadline=self.deadline,
                    still_mine=held.still_mine,
                    skip_incomplete=incomplete_skipper(incomplete),
                    batches=0 if summary is None else summary.batches,
                )
                summary = part if summary is None else scrape.merge_summaries(summary, part)
                if part.stop is not None:  # the quota, the budget, a lost lock ...: no more waves
                    break
        except Exception as exc:  # a failure of the run, not of a game
            failed = self.clean(f"unexpected failure: {type(exc).__name__}: {_why(exc)}")
            rep.warn(f"scrape: {failed}")
        lost = (summary is not None and summary.stop is scrape.StopReason.LOCK_LOST) or (
            failed is None and not held.still_mine()
        )
        return self.finish(summary, failed, lost, died)

    def announce_died(self) -> None:
        """Send a `died` streak that has just become news now, and mark it reported, so that a run
        that dies as well has still told it. Only with a webhook: without one the exit code at the
        end of the run is the only report, and a run that dies has none to give."""
        if not self.setup.notify_url:
            return
        found = [item for item in news(self.before, self.state) if item.source == "trouble"]
        if not found:
            return
        record = self.record(None, self.run_id, FAILED, exit_code(found), False)
        self.send(found, record, self.known_left())
        self.announced = found
        try:
            self.commit(with_notified())
        except InboxError as exc:
            self.rep.warn(str(exc))

    def finish(
        self, summary: scrape.Summary | None, failed: str | None, lost: bool, died: bool
    ) -> int:
        """Step 7: the outcome, the record, the state and the webhook."""
        now = self.clock()
        judged = Judged(FAILED) if failed is not None else judge_night(summary)
        if lost and (summary is None or summary.stop is not scrape.StopReason.LOCK_LOST):
            self.rep.warn(f"scrape: {LOST_MESSAGE}")
        changes: list[Change] = [
            with_checked(now),
            with_night(
                self.run_id,
                now,
                judged.outcome,
                judged.kind,
                judged.systems,
                self.night.backlog,
                died=died,
            ),
        ]
        if lost:
            changes.append(with_error(LOST_MESSAGE, STEP_RUN, now, False))
        elif failed is not None:
            changes.append(with_error(failed, STEP_RUN, now, False))
        else:
            changes.append(with_error_cleared(STEP_FINISH))
        return self.conclude(
            *changes,
            summary=summary,
            outcome=judged.outcome,
            left=self.night.backlog,
            ran=True,
        )


def scrape_state(root: Path) -> tuple[State, str | None]:
    """The nightly scrape's state and a warning, or an empty state when there is no file."""
    return read_file(root, SCRAPE_STATE)
