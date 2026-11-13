"""The doctor's `Unattended inbox` line: the pure judge, one test per verdict of the design
(development/design/inbox-unattended.md, "Doctor"), and the check that reads the hub."""

import json
import unittest
from datetime import UTC, datetime, timedelta

from test_doctor import TmpCase, by_name, context, make_probes
from warpzone import doctor
from warpzone.doctor import (
    FAIL,
    OK,
    OPTIONAL,
    WARN,
    check_unattended_inbox,
    judge_unattended_inbox,
)
from warpzone.inbox_lock import LOCK_NAME, LockInfo
from warpzone.inbox_lock import render as render_lock
from warpzone.inbox_unattended import (
    Entry,
    Held,
    RunError,
    State,
    newest_record,
    render_record,
    render_state,
)

NOW = datetime(2026, 11, 13, 12, 0, tzinfo=UTC)
RECORD = {
    "v": 1,
    "id": "20261113T110000Z",
    "mode": "unattended",
    "started": "2026-11-13T11:00:00Z",
    "counts": {"placed": 3, "already": 0, "review": 1, "held": 1, "waiting": 0},
}


def ago(**kw: float) -> datetime:
    return NOW - timedelta(**kw)


def entry(first: datetime, held: Held | None = None) -> Entry:
    return Entry("sha256:00", 1, 10, first, first, 0, held)


def judge(state: State | None, lock: datetime | None = None, record=RECORD):
    return judge_unattended_inbox(state, record, lock, NOW)


class JudgeTests(unittest.TestCase):
    def test_no_state_is_optional_with_the_fix(self) -> None:
        status, detail, fix = judge(None, record=None)
        self.assertEqual(status, OPTIONAL)
        self.assertEqual(detail, "no unattended run has recorded anything")
        self.assertIn("warpzone inbox --apply --unattended", fix)

    def test_a_run_level_error_is_a_problem(self) -> None:
        error = RunError("[inbox] dats_dir does not exist", 1, ago(hours=5), count=4)
        status, detail, fix = judge(State(checked=ago(minutes=5), error=error))
        self.assertEqual(status, FAIL)
        self.assertIn("[inbox] dats_dir does not exist", detail)
        self.assertIn("since 2026-11-13 07:00 UTC, 4 run(s)", detail)
        self.assertIn("step 1", fix)
        self.assertIn("config", fix)

    def test_a_runtime_error_points_at_the_task_output(self) -> None:
        error = RunError("it broke", 3, ago(hours=1), config=False)
        _, _, fix = judge(State(checked=ago(minutes=5), error=error))
        self.assertIn("task's output", fix)

    def test_a_check_older_than_two_hours_is_a_warning(self) -> None:
        status, detail, fix = judge(State(checked=ago(hours=5)))
        self.assertEqual(status, WARN)
        self.assertEqual(
            detail, "the unattended inbox last ran 5 h ago; is the scheduled task enabled?"
        )
        self.assertIn("scheduled task", fix)

    def test_two_hours_exactly_is_still_fine(self) -> None:
        self.assertEqual(judge(State(checked=ago(hours=2)))[0], OK)
        self.assertEqual(judge(State())[0], WARN)

    def test_held_items_are_named_with_their_reason(self) -> None:
        held = Held("a disc dropped as loose files", ago(minutes=30))
        state = State(checked=ago(minutes=5), items={"Game.cue": entry(ago(hours=1), held)})
        status, detail, fix = judge(state)
        self.assertEqual(status, WARN)
        self.assertEqual(detail, "held: Game.cue (a disc dropped as loose files)")
        self.assertIn("`warpzone inbox --apply` by hand", fix)
        self.assertIn("replace the item to retry", fix)

    def test_an_item_waiting_more_than_two_hours_since_first_is_a_warning(self) -> None:
        # `first` counts, not `seen`: the fingerprint changed a minute ago (a copy that keeps
        # writing), and the item has been in the inbox for three hours.
        slow = Entry("sha256:01", 1, 10, ago(hours=3), ago(minutes=1))
        items = {"Big.zip": slow, "New.zip": entry(ago(hours=1))}
        status, detail, fix = judge(State(checked=ago(minutes=5), items=items))
        self.assertEqual(status, WARN)
        self.assertEqual(detail, "waiting for more than 2 h since first seen: Big.zip")
        self.assertIn("no space", fix)

    def test_a_held_item_is_not_also_waiting(self) -> None:
        held = Held("stopped twice", ago(hours=4))
        state = State(checked=ago(minutes=5), items={"a.zip": entry(ago(hours=9), held)})
        self.assertNotIn("waiting", judge(state)[1])

    def test_a_lock_held_more_than_six_hours_is_a_warning(self) -> None:
        state = State(checked=ago(minutes=5))
        status, detail, _ = judge(state, lock=ago(hours=7))
        self.assertEqual(status, WARN)
        self.assertEqual(detail, "the inbox lock has been held for 7 h")
        self.assertEqual(judge(state, lock=ago(hours=5))[0], OK)

    def test_ok_says_the_last_check_and_the_last_run(self) -> None:
        self.assertEqual(
            judge(State(checked=ago(minutes=7))),
            (OK, "last check 7 min ago; last run 2026-11-13 11:00 UTC: placed 3, for review 2", ""),
        )

    def test_ok_without_a_record(self) -> None:
        _, detail, _ = judge(State(checked=ago(minutes=7)), record=None)
        self.assertEqual(detail, "last check 7 min ago; no run has been recorded yet")

    def test_findings_share_the_line_with_the_worst_status(self) -> None:
        held = Held("stopped twice", ago(hours=1))
        state = State(
            checked=ago(hours=3),
            items={"a.zip": entry(ago(hours=1), held)},
            error=RunError("work folder is memory", 5, ago(hours=3)),
        )
        status, detail, _ = judge(state)
        self.assertEqual(status, FAIL)
        self.assertIn("work folder is memory", detail)
        self.assertIn("last ran 3 h ago", detail)
        self.assertIn("held: a.zip", detail)

    def test_many_held_items_are_counted(self) -> None:
        held = Held("x", ago(hours=1))
        items = {f"{n}.zip": entry(ago(hours=1), held) for n in range(5)}
        self.assertIn("and 2 more", judge(State(checked=ago(minutes=1), items=items))[1])

    def test_it_is_the_one_doctor_exports(self) -> None:
        self.assertIs(doctor.judge_unattended_inbox, judge_unattended_inbox)


class NewestRecordTests(unittest.TestCase):
    def test_the_newest_by_id_then_by_suffix(self) -> None:
        names = ["20261113T100000Z.json", "20261113T110000Z-2.json", "20261113T110000Z.json"]
        self.assertEqual(newest_record(names), "20261113T110000Z-2.json")

    def test_other_names_are_ignored(self) -> None:
        self.assertIsNone(newest_record(["notes.txt", ".tmp-1"]))
        self.assertIsNone(newest_record([]))


class CheckTests(TmpCase):
    def setUp(self) -> None:
        super().setUp()
        self.root = self.tmp / "hub"
        (self.root / "inbox").mkdir(parents=True)
        self.catalog = self.root / "catalog" / "inbox"
        self.config = f'[inbox]\nroot = "{self.root}"\n'

    def line(self, config: str | None = None, **kw):
        kw.setdefault("now", lambda: NOW.timestamp())
        ctx = context(self.tmp, self.config if config is None else config, **kw)
        return check_unattended_inbox(ctx)

    def write_state(self, state: State) -> None:
        self.catalog.mkdir(parents=True, exist_ok=True)
        (self.catalog / "state.json").write_text(render_state(state))

    def test_doctor_with_no_state_gives_an_optional_line(self) -> None:
        (result,) = self.line()
        self.assertEqual(
            (result.name, result.group, result.status), ("Unattended inbox", "Inbox", OPTIONAL)
        )
        self.assertTrue(result.fix)

    def test_it_reads_the_state_and_the_newest_record(self) -> None:
        self.write_state(State(checked=ago(minutes=10)))
        runs = self.catalog / "runs"
        runs.mkdir()
        older = {**RECORD, "counts": {"placed": 9}}
        (runs / "20261113T090000Z.json").write_text(render_record(older))
        (runs / "20261113T110000Z.json").write_text(render_record(RECORD))
        (result,) = self.line()
        self.assertEqual(result.status, OK)
        self.assertEqual(
            result.detail,
            "last check 10 min ago; last run 2026-11-13 11:00 UTC: placed 3, for review 2",
        )

    def test_a_record_that_does_not_parse_is_no_record(self) -> None:
        self.write_state(State(checked=ago(minutes=10)))
        runs = self.catalog / "runs"
        runs.mkdir()
        (runs / "20261113T110000Z.json").write_text("{not json")
        (result,) = self.line()
        self.assertEqual(result.status, OK)
        self.assertIn("no run has been recorded yet", result.detail)

    def test_it_reads_the_lock(self) -> None:
        self.write_state(State(checked=ago(minutes=10)))
        info = LockInfo("host", 1, ago(hours=8).timestamp(), 60, "unattended")
        (self.root / "inbox" / LOCK_NAME).write_text(render_lock(info))
        (result,) = self.line()
        self.assertEqual(result.status, WARN)
        self.assertIn("held for 8 h", result.detail)

    def test_a_lock_that_does_not_parse_counts_from_its_mtime(self) -> None:
        self.write_state(State(checked=ago(minutes=10)))
        (self.root / "inbox" / LOCK_NAME).write_text("")
        (result,) = self.line(mtime=lambda path: ago(hours=7).timestamp())
        self.assertEqual(result.status, WARN)
        self.assertIn("held for 7 h", result.detail)

    def test_a_state_that_does_not_parse_is_a_warning_not_a_crash(self) -> None:
        self.catalog.mkdir(parents=True)
        (self.catalog / "state.json").write_text(json.dumps([1]))
        (result,) = self.line()
        self.assertEqual(result.status, WARN)
        self.assertIn("records no check", result.detail)

    def test_no_line_without_an_inbox_table(self) -> None:
        self.assertEqual(self.line(config='[hub]\nroot = "/nowhere"\n'), [])

    def test_no_line_when_the_hub_root_is_not_readable(self) -> None:
        self.assertEqual(self.line(config='[inbox]\nroot = "/nowhere/at/all"\n'), [])

    def test_the_hub_root_falls_back_to_the_hub_table(self) -> None:
        (result,) = self.line(config=f'[hub]\nroot = "{self.root}"\n\n[inbox]\n')
        self.assertEqual(result.status, OPTIONAL)

    def test_it_is_one_of_the_checks_doctor_runs(self) -> None:
        self.assertIn(check_unattended_inbox, doctor.CHECKS)
        probes = make_probes(self.tmp, now=lambda: NOW.timestamp())
        probes.config_path.write_text(self.config)
        results = doctor.run_checks(probes)
        self.assertEqual(by_name(results, "Unattended inbox").status, OPTIONAL)


if __name__ == "__main__":
    unittest.main()
