"""`warpzone inbox --apply --unattended`: one tick of the scheduled run, with nobody watching.

The decisions are pure (`inbox_unattended`); the disk and the clock are read here and in
`inbox_unattended_io`. A tick goes through the steps of "The command" in
development/design/inbox-unattended.md and stops at the first that says so: the mount check,
the listing (an empty inbox exits at once, with no DAT, tool, lock or work folder touched), the
lock in the way, the readiness of each item across ticks, what the work needs, the lock, the
items one at a time, and the finish. Every state change is a read-modify-write of
`catalog/inbox/state.json`. The exit code is 0 unless the run has news (see `news`).
"""

from __future__ import annotations

import os
import shutil
import tempfile
from collections.abc import Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path

from warpzone import inbox, inbox_lock, inbox_scrape
from warpzone.dat import DatLibrary
from warpzone.inbox_core import Context, InboxError, ItemResult, Options, ScrapeNote
from warpzone.inbox_unattended import (
    MOVE_REASON,
    SHEET_EXTENSIONS,
    WORK_PREFIX,
    Change,
    Entry,
    LockSeen,
    Observed,
    State,
    Verdict,
    decide,
    exit_code,
    heartbeat_due,
    loose_sheets,
    news,
    observe,
    space_verdict,
    update,
    with_attempt,
    with_checked,
    with_error,
    with_error_cleared,
    with_held,
    with_item_filed,
    with_judged,
    with_lock_seen,
)
from warpzone.inbox_unattended_io import (
    clean_work_dir,
    free_bytes,
    lower_priority,
    on_memory,
    read_sheet,
    read_state,
    update_state_checked,
    walk,
)
from warpzone.report import Reporter

# The steps of "The command": an error keeps the one where it failed.
STEP_MOUNT, STEP_LIST, STEP_BUSY, STEP_JUDGE, STEP_CHECK, STEP_LOCK, STEP_ITEMS, STEP_FINISH = (
    range(1, 9)
)

LEGACY_MESSAGE = (
    "inbox/.warpzone.lock is the lock of an older warpzone and is stale by its rules, which an "
    "unattended run never takes over. If no other run is working on the inbox, run "
    "`warpzone inbox --apply --force-lock` yourself"
)
LOST_MESSAGE = (
    "the inbox lock is no longer this run's; the run stopped and left the item where it is"
)
LOST_REASON = "the inbox lock is no longer this run's"
CHANGED_REASON = "it changed while it was being processed"
WAIT_LINE = "waiting until it has held still for 5 minutes"


@dataclass(frozen=True)
class Item:
    key: str  # the name as the report shows it, and the key of the state
    path: Path
    unverified: str | None


def run_unattended(opts: Options, rep: Reporter) -> int:
    """One tick. Returns the exit code: 0 with no news, 1 for news, 2 for a new configuration
    error. A problem met before the hub root is known raises `InboxError` (exit 2)."""
    return _Run(opts, rep).go()


def lock_ns(mtime: float) -> int:
    """The modification time of a lock reading as the state keeps it."""
    return round(mtime * 1e9)


def noting(ns: int, now: datetime) -> Change:
    """Note the lock seen: kept as it is when it is the same lock file as the last note (so the
    time it was first seen with this modification time is not lost), else a new note."""

    def change(state: State) -> State:
        old = state.lock_seen
        if old is not None and old.mtime_ns == ns:
            return state
        return replace(state, lock_seen=LockSeen(ns, now))

    return change


def _why(exc: Exception) -> str:
    return (getattr(exc, "strerror", None) or str(exc)).strip()


class _Run:
    def __init__(self, opts: Options, rep: Reporter) -> None:
        self.opts = opts
        self.rep = rep
        self.root = opts.root
        self.inbox = opts.root / "inbox"
        self.started = self.clock()
        self.before = State()  # the state as this run read it at its start
        self.state = State()  # the state as this run last wrote it
        self.total = 0  # items in the inbox
        self.present: frozenset[str] = frozenset()  # the names the state keeps entries for
        self.wrote = True  # whether the last `commit` reached the file

    def clock(self) -> datetime:
        return datetime.fromtimestamp(self.opts.now(), UTC)

    # ------------------------------------------------------------ state and outcome

    def commit(self, *changes: Change) -> State:
        state, warnings, self.wrote = update_state_checked(self.root, *changes)
        for warning in warnings:
            self.rep.warn(warning)
        self.state = state
        return state

    def conclude(
        self,
        *changes: Change,
        results: Sequence[ItemResult] = (),
        reviewed: Sequence[str] = (),
        note: ScrapeNote | None = None,
        purged: Sequence[str] = (),
    ) -> int:
        """Finish the run: the news and the exit code, the record and the webhook, then the
        state (the record first, as the page orders; both are warnings when they fail)."""
        found = news(self.before, update(self.state, *changes), reviewed)
        code = exit_code(found)
        inbox._record_run(
            self.opts,
            self.rep,
            results,
            self.started,
            code,
            note,
            purged,
            mode="unattended",
            status=code,
            waiting=max(self.total - len(results), 0),
            news_found=bool(found),
            error=None if found.error is None else found.error.message,
            held=found.held,
        )
        try:
            self.commit(*changes)
        except InboxError as exc:
            self.rep.warn(str(exc))
        return code

    def fail(self, message: str, step: int, *, config: bool = True) -> int:
        """A run-level error: kept in the state with its step. Meeting the same message again
        raises its count and is not news."""
        self.rep.warn(f"inbox: {message}")
        now = self.clock()
        return self.conclude(with_checked(now), with_error(message, step, now, config))

    # ------------------------------------------------------------ the steps

    def go(self) -> int:
        self.before, warning = read_state(self.root)
        self.state = self.before
        if warning:
            self.rep.warn(warning)
        problem = inbox.check_layout(self.root)
        if problem:
            if not (self.root / "catalog").is_dir():  # not the hub: write nothing there
                raise InboxError(problem)
            return self.fail(problem, STEP_MOUNT)
        items = self.list_items()
        if not items:
            return self.empty()
        self.total = len(items)
        return self.tick(items)

    def list_items(self) -> list[Item]:
        items = [Item(p.name, p, None) for p in inbox.scan(self.inbox)]
        items += [
            Item(f"{inbox.UNVERIFIED_DIR}/{system}/{p.name}", p, system)
            for p, system in inbox.scan_unverified(self.inbox)
        ]
        return items

    def empty(self) -> int:
        """Step 2: nothing to do. Only the heartbeat (at most hourly), the entries of items that
        are gone and the errors of step 1 are written; no DAT, tool, lock or work folder."""
        now = self.clock()
        changes: list[Change] = []
        if self.state.items:
            changes.append(with_judged({}, ()))
        error = self.state.error
        if error is not None and error.step < STEP_LIST:
            changes.append(with_error_cleared(STEP_LIST))
        if not changes and not heartbeat_due(self.state, now):
            return 0
        return self.conclude(with_checked(now), *changes)

    def previous(self, reading: inbox_lock.LockReading) -> inbox_lock.LockReading | None:
        """The earlier look at this lock that the state kept, as a reading to compare with."""
        seen = self.state.lock_seen
        if seen is None:
            return None
        ns = lock_ns(reading.mtime)
        mtime = reading.mtime if ns == seen.mtime_ns else seen.mtime_ns / 1e9
        return inbox_lock.LockReading(reading.info, mtime, seen.at.timestamp())

    def busy(self, reading: inbox_lock.LockReading | None) -> int:
        now = self.clock()
        changes: list[Change] = [with_checked(now), with_error_cleared(STEP_BUSY)]
        if reading is None:
            self.rep.out("busy: another run holds the inbox lock")
        else:
            self.rep.out(
                f"busy: {inbox_lock.describe(reading.info, reading.mtime, reading.looked)}"
            )
            changes.append(noting(lock_ns(reading.mtime), now))
        return self.conclude(*changes)

    def tick(self, items: list[Item]) -> int:
        reading = inbox_lock.look(self.inbox, self.opts.now())
        previous = None
        if reading is not None:
            previous = self.previous(reading)
            verdict = inbox_lock.judge(
                reading,
                inbox_lock.hostname(),
                inbox_lock.pid_alive,
                unattended=True,
                previous=previous,
            )
            if verdict.state in (inbox_lock.LIVE, inbox_lock.UNKNOWN):
                return self.busy(reading)
            if verdict.state == inbox_lock.LEGACY:
                return self.fail(LEGACY_MESSAGE, STEP_BUSY)
            # stale by silence: taken over at step 6
        ready, changes = self.judge(items, lock_in_the_way=reading is not None)
        if not ready:
            return self.conclude(*changes)
        self.commit(*changes)
        try:
            library = self.check_work()
        except InboxError as exc:
            return self.fail(str(exc), STEP_CHECK)
        return self.locked(ready, library, previous)

    # ---- step 4

    def loose(self, items: Sequence[Item]) -> dict[str, str]:
        top = [i.path.name for i in items if i.unverified is None and i.path.is_file()]
        refs: dict[str, list[str]] = {}
        for name in top:
            lower = name.lower()
            if not lower.endswith(SHEET_EXTENSIONS):
                continue
            try:
                text = read_sheet(self.inbox / name)
            except OSError:
                continue
            refs[name] = inbox.cue_refs(text) if lower.endswith(".cue") else inbox.gdi_refs(text)
        return loose_sheets(top, refs)

    def judge(
        self, items: Sequence[Item], lock_in_the_way: bool
    ) -> tuple[list[tuple[Item, Entry]], list[Change]]:
        """Step 4: the verdict of each item across ticks. Returns the items that are ready, in
        scan order, and the changes that record what was seen in the state. A stale lock that is
        still in the way keeps its note, so the next tick can see it stay silent."""
        now = self.clock()
        loose = self.loose(items)
        judged: dict[str, Entry] = {}
        present: set[str] = set()
        ready: list[tuple[Item, Entry]] = []
        for item in items:
            try:
                entries = walk(item.path)
            except OSError as exc:
                self.rep.warn(f"{item.key}: cannot read it ({_why(exc)}); it waits")
                present.add(item.key)
                continue
            if entries is None:  # gone since the listing
                continue
            present.add(item.key)
            old = self.state.items.get(item.key)
            verdict, entry = decide(old, observe(entries), now, loose.get(item.key))
            judged[item.key] = entry
            if verdict is Verdict.READY:
                ready.append((item, entry))
            if old is None:
                self.rep.out(f"new: {item.key}: {WAIT_LINE}")
            elif entry.held is not None and old.held is None:
                self.rep.out(f"held: {item.key}: {entry.held.reason}")
        self.present = frozenset(present)
        changes: list[Change] = [
            with_checked(now),
            with_judged(judged, self.present),
            with_error_cleared(STEP_JUDGE),
        ]
        if not lock_in_the_way:
            changes.append(with_lock_seen(None))
        return ready, changes

    # ---- steps 5 and 6

    def check_work(self) -> DatLibrary:
        """Step 5: what the work needs, as an attended run, and the work folder."""
        work = self.opts.work_dir
        if work is None:
            raise InboxError(
                "--unattended needs [inbox] work_dir, a folder on a mounted volume "
                "(the container's own /tmp is memory)"
            )
        try:
            work.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise InboxError(f"cannot create the work folder: {_why(exc)}") from exc
        if on_memory(work):
            raise InboxError(
                "the work folder is on a memory filesystem (tmpfs or ramfs); set [inbox] "
                "work_dir to a folder on a mounted volume"
            )
        return inbox.open_library(self.opts)

    def locked(
        self,
        ready: list[tuple[Item, Entry]],
        library: DatLibrary,
        previous: inbox_lock.LockReading | None,
    ) -> int:
        try:
            with inbox_lock.hold(
                self.inbox,
                force=False,
                now=self.opts.now,
                out=self.rep.warn,  # a lock taken over is a warning: it shows in the record
                mode=inbox_lock.UNATTENDED,
                previous=previous,
            ) as held:
                return self.work(held, ready, library)
        except inbox_lock.LockBusy as exc:  # one created between the look and taking it
            return self.busy(exc.reading)
        except inbox_lock.LockStale:
            return self.fail(LEGACY_MESSAGE, STEP_LOCK)
        except InboxError as exc:
            return self.fail(str(exc), STEP_LOCK)

    # ---- steps 7 and 8

    def work(
        self, held: inbox_lock.Held, ready: list[tuple[Item, Entry]], library: DatLibrary
    ) -> int:
        opts, rep = self.opts, self.rep
        failure = lower_priority()
        if failure:
            rep.warn(f"could not lower the CPU priority: {failure}")
        assert opts.work_dir is not None
        gone = clean_work_dir(opts.work_dir, opts.now())
        if gone:
            rep.out(f"work folder: deleted {len(gone)} leftover folder(s) of a killed run")
        inbox.announce(opts, rep, self.inbox, library, "apply, unattended", len(ready))
        results: list[ItemResult] = []
        reviewed: list[str] = []
        failed: tuple[str, bool] | None = None  # message, whether it is a configuration error
        work = Path(tempfile.mkdtemp(prefix=WORK_PREFIX, dir=opts.work_dir))
        ctx = Context(opts, rep, library, work)
        try:
            for n, (item, entry) in enumerate(ready, 1):
                try:
                    stop = self.one(held, ctx, n, len(ready), item, entry, results, reviewed)
                except Exception as exc:  # a failure of the run, not of the item
                    failed = (f"unexpected failure: {type(exc).__name__}: {_why(exc)}", False)
                    break
                if stop:
                    failed = (LOST_MESSAGE, False)
                    break
        finally:
            shutil.rmtree(work, ignore_errors=True)
        if failed is not None:
            rep.warn(f"inbox: {failed[0]}")
        return self.finish(results, reviewed, failed)

    def one(
        self,
        held: inbox_lock.Held,
        ctx: Context,
        n: int,
        total: int,
        item: Item,
        entry: Entry,
        results: list[ItemResult],
        reviewed: list[str],
    ) -> bool:
        """Step 7 for one item. True when the run must stop (the lock is no longer ours)."""
        opts, rep = self.opts, self.rep
        if not held.still_mine():
            return True
        try:
            entries = walk(item.path)
        except OSError as exc:  # as in `judge`: the item waits, the run goes on
            rep.warn(f"{item.key}: cannot read it ({_why(exc)}); it waits")
            return False
        if entries is None:  # gone while the run worked on others
            return False
        observed: Observed = observe(entries)
        if observed.fingerprint != entry.fingerprint:  # changed since it was judged ready
            _, again = decide(entry, observed, self.clock())
            self.commit(with_judged({item.key: again}, self.present))
            rep.out(f"{item.key}: changed since it was judged ready; waiting again")
            return False
        space = space_verdict(free_bytes(self.root / "library"), entry.bytes)
        if not space.fits:
            rep.out(f"{item.key}: {space.reason}")
            return False
        self.commit(with_attempt(item.key, entry.fingerprint))
        if not self.wrote:  # without the count, a run that dies twice would never hold it
            rep.warn(f"{item.key}: the attempt could not be noted; it waits for the next run")
            return False
        result = inbox.process_item(ctx, item.path, n, item.unverified)
        results.append(result)
        lost = not held.still_mine()
        if lost:  # the item stays where it is; a rerun finds its games "already in the library"
            result.unmoved = LOST_REASON
            result.reasons.append(MOVE_REASON.format(LOST_REASON))
        elif self.changed(item, entry):
            # it was copied over (or is still being written) while the tools worked: the games
            # it held when judged are placed, but this content was never processed
            result.unmoved = CHANGED_REASON
            result.reasons.append(MOVE_REASON.format(CHANGED_REASON))
            rep.warn(f"{item.key}: {CHANGED_REASON}; it stays in the inbox and waits again")
        else:
            try:
                inbox._file_away(self.inbox, item.path, result, opts.today())
            except OSError as exc:
                self.filing_failed(item, result, exc, reviewed)
            else:
                self.commit(with_item_filed(item.key))
                if result.review:
                    reviewed.append(item.key)
        inbox._print_item(rep, n, total, result, True, opts.today())
        return lost

    def changed(self, item: Item, entry: Entry) -> bool:
        """Whether the item is no longer what was judged ready, looked at again after the
        work. A change resets its entry through `decide`, so it waits again; an item that
        cannot be read waits too, because it cannot be shown to be unchanged."""
        try:
            entries = walk(item.path)
        except OSError:
            entries = []
        if entries is None:  # gone: nothing to file away, nothing to wait for
            return False
        observed = observe(entries)
        if entries and observed.fingerprint == entry.fingerprint:
            return False
        _, again = decide(entry, observed, self.clock())
        self.commit(with_judged({item.key: again}, self.present))
        return True

    def filing_failed(
        self, item: Item, result: ItemResult, exc: OSError, reviewed: list[str]
    ) -> None:
        """`_file_away` raised. When the item is already gone it was moved to `_revisar/` and
        only its reason file failed: it is reviewed, with a warning, and not held."""
        if not os.path.lexists(item.path):
            target = self.inbox / inbox.REVIEW_DIR / item.path.name
            where = f"{inbox.REVIEW_DIR}/{item.path.name}"
            result.moved_to = where if os.path.lexists(target) else f"{inbox.REVIEW_DIR}/"
            self.rep.warn(
                f"{item.key}: moved to {inbox.REVIEW_DIR}/ but its reason file was not "
                f"written ({_why(exc)})"
            )
            self.commit(with_item_filed(item.key))
            reviewed.append(item.key)
            return
        result.unmoved = _why(exc)
        reason = MOVE_REASON.format(result.unmoved)
        result.reasons.append(reason)
        self.commit(with_held(item.key, reason, self.clock()))

    def finish(
        self,
        results: list[ItemResult],
        reviewed: list[str],
        failed: tuple[str, bool] | None,
    ) -> int:
        """Step 8: the purge, the catalog, the scrape, the record, the state and the webhook."""
        opts, rep = self.opts, self.rep
        lost = failed is not None and failed[0] == LOST_MESSAGE
        purged: list[str] = []
        if not lost:
            try:
                purged = inbox.purge_done(self.inbox, opts.today())
            except OSError as exc:
                rep.warn(f"{inbox.DONE_DIR}: not purged ({_why(exc)})")
        if purged:
            rep.out(
                f"{inbox.DONE_DIR}: deleted {len(purged)} folder(s) older than "
                f"{inbox.DONE_KEEP_DAYS} days"
            )
        inbox._update_catalog(opts, rep, results)
        note = None if lost else inbox_scrape.after_apply(opts, rep, results)
        inbox._summary(rep, results, True)
        now = self.clock()
        changes: list[Change] = [with_checked(now), with_lock_seen(None)]
        if failed is None:
            changes.append(with_error_cleared(STEP_FINISH))
        else:
            changes.append(with_error(failed[0], STEP_ITEMS, now, failed[1]))
        return self.conclude(*changes, results=results, reviewed=reviewed, note=note, purged=purged)
