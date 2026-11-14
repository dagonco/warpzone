"""The doctor's `Nightly scrape` line: the pure judge, one test per verdict of the design
(development/design/scrape-unattended.md, "Doctor"), and the check that reads the hub."""

import json
import unittest
from datetime import UTC, datetime, timedelta
from typing import Any

from test_doctor import TmpCase, by_name, context, make_probes
from warpzone import doctor
from warpzone.doctor import FAIL, OK, OPTIONAL, WARN
from warpzone.doctor_scrape import check_nightly_scrape, check_skyscraper, judge_nightly
from warpzone.inbox_unattended import RunError
from warpzone.scrape_unattended import (
    NEWS_AFTER,
    QUOTA_WARN_NIGHTS,
    Backlog,
    Last,
    State,
    Trouble,
    render_record,
    render_state,
)

NOW = datetime(2026, 11, 14, 12, 0, tzinfo=UTC)


def ago(**kw: float) -> datetime:
    return NOW - timedelta(**kw)


def system(**counts: int) -> dict[str, int]:
    base = {
        "added": 0,
        "filled": 0,
        "not_found": 0,
        "unchanged": 0,
        "skipped_not_found": 0,
        "skipped_incomplete": 0,
        "deferred": 0,
    }
    return base | counts


RECORD: dict[str, Any] = {
    "v": 1,
    "id": "20261114T030000Z",
    "started": "2026-11-14T03:00:02Z",
    "finished": "2026-11-14T04:23:10Z",
    "outcome": "done",
    "systems": {
        "psx": system(added=100, filled=20, not_found=3, unchanged=277, deferred=700),
        "snes": system(added=20, filled=10, not_found=1, unchanged=69, skipped_incomplete=40),
    },
    "added": [],
    "filled": [],
    "failed_systems": [],
    "stop": None,
    "budget_spent": False,
    "warnings": [],
    "exit": 0,
    "notified": False,
}


def last(outcome: str = "done") -> Last:
    return Last("20261114T030000Z", ago(hours=8), outcome)


def healthy(**kw: Any) -> State:
    fields: dict[str, Any] = {
        "checked": ago(hours=9),
        "last": last(),
        "backlog": Backlog(700, ago(hours=8)),
    }
    return State(**(fields | kw))


def streak(kind: str, count: int, systems: tuple[str, ...] = ()) -> Trouble:
    return Trouble(kind, systems, ago(days=count), count)


def judge(state: State | None, record: dict[str, Any] | None = RECORD):
    return judge_nightly(state, record, NOW)


class JudgeTests(unittest.TestCase):
    def test_no_state_is_optional_with_the_fix(self) -> None:
        status, detail, fix = judge(None, None)
        self.assertEqual(status, OPTIONAL)
        self.assertEqual(detail, "no nightly scrape has recorded anything")
        self.assertIn("warpzone scrape --unattended", fix)
        self.assertIn("setup/scrape/#filling-art-every-night", fix)

    def test_a_run_level_error_is_a_problem(self) -> None:
        error = RunError("[inbox] skyscraper is missing", 2, ago(hours=30), count=2)
        status, detail, fix = judge(healthy(error=error))
        self.assertEqual(status, FAIL)
        self.assertIn("[inbox] skyscraper is missing", detail)
        self.assertIn("since 2026-11-13 06:00 UTC, 2 run(s)", detail)
        self.assertIn("step 2", fix)
        self.assertIn("config", fix)

    def test_a_runtime_error_points_at_the_task_output(self) -> None:
        error = RunError("it broke", 6, ago(hours=1), config=False)
        _, _, fix = judge(healthy(error=error))
        self.assertIn("scheduled task's output", fix)

    def test_a_check_older_than_36_hours_asks_whether_the_task_runs(self) -> None:
        status, detail, fix = judge(healthy(checked=ago(hours=40)))
        self.assertEqual(status, WARN)
        self.assertEqual(
            detail, "the nightly scrape last checked 40 h ago; is the scheduled task enabled?"
        )
        self.assertIn("scheduled task", fix)

    def test_a_check_of_36_hours_is_still_fine(self) -> None:
        self.assertEqual(judge(healthy(checked=ago(hours=36)))[0], OK)

    def test_a_state_with_no_check_is_a_warning(self) -> None:
        status, detail, _ = judge(State())
        self.assertEqual(status, WARN)
        self.assertIn("records no check", detail)

    def test_a_streak_below_its_threshold_is_fine(self) -> None:
        for kind, nights in NEWS_AFTER.items():
            if nights is None or nights < 2:
                continue
            with self.subTest(kind=kind):
                state = healthy(trouble=streak(kind, nights - 1))
                self.assertEqual(judge(state)[0], OK)

    def test_a_streak_at_its_threshold_is_a_warning(self) -> None:
        for kind, nights in NEWS_AFTER.items():
            if nights is None:
                continue
            with self.subTest(kind=kind):
                status, detail, fix = judge(healthy(trouble=streak(kind, nights)))
                self.assertEqual(status, WARN)
                self.assertIn(f"{nights} night", detail)
                self.assertTrue(fix)

    def test_a_streak_past_its_threshold_still_warns(self) -> None:
        self.assertEqual(judge(healthy(trouble=streak("network", 9)))[0], WARN)

    def test_a_failed_streak_names_its_systems(self) -> None:
        state = healthy(trouble=streak("failed", 2, ("psx", "snes")))
        self.assertIn("psx, snes", judge(state)[1])

    def test_a_quota_streak_below_the_threshold_is_fine(self) -> None:
        state = healthy(trouble=streak("quota", QUOTA_WARN_NIGHTS - 1))
        self.assertEqual(judge(state)[0], OK)

    def test_a_quota_streak_at_the_threshold_warns(self) -> None:
        status, detail, fix = judge(healthy(trouble=streak("quota", QUOTA_WARN_NIGHTS)))
        self.assertEqual(status, WARN)
        self.assertIn(f"quota reached {QUOTA_WARN_NIGHTS} nights in a row", detail)
        self.assertIn("account", fix)

    def test_several_cases_share_the_line_worst_status_first(self) -> None:
        error = RunError("it broke", 6, ago(hours=1), config=False)
        state = healthy(checked=ago(hours=50), error=error, trouble=streak("network", 2, ("psx",)))
        status, detail, fix = judge(state)
        self.assertEqual(status, FAIL)
        self.assertIn("it broke", detail)
        self.assertIn("last checked 50 h ago", detail)
        self.assertIn("2 nights", detail)
        self.assertGreaterEqual(fix.count("; "), 2)

    def test_ok_says_the_last_run_with_its_counts_and_what_is_left(self) -> None:
        status, detail, fix = judge(healthy())
        self.assertEqual(status, OK)
        self.assertEqual(
            detail,
            "last run 2026-11-14 03:00 UTC: added 120, filled 30, not found 4; "
            "700 left (about 2 nights at 500 a night)",
        )
        self.assertEqual(fix, "")

    def test_the_nights_round_up(self) -> None:
        state = healthy(backlog=Backlog(1001, ago(hours=8)))
        self.assertIn("1001 left (about 3 nights at 500 a night)", judge(state)[1])

    def test_nothing_left_has_no_estimate(self) -> None:
        state = healthy(backlog=Backlog(0, ago(hours=8)))
        detail = judge(state)[1]
        self.assertTrue(detail.endswith("not found 4; nothing left"), detail)

    def test_a_budget_night_counts_like_a_done_one(self) -> None:
        state = healthy(last=last("budget"))
        self.assertIn("about 2 nights at 500 a night", judge(state)[1])

    def test_a_night_cut_short_by_a_stop_gives_no_estimate(self) -> None:
        for outcome in ("quota", "stopped", "failed", "busy"):
            with self.subTest(outcome=outcome):
                detail = judge(healthy(last=last(outcome)))[1]
                self.assertTrue(detail.endswith("700 left"), detail)

    def test_a_night_that_scraped_nothing_gives_no_estimate(self) -> None:
        record = {**RECORD, "systems": {"psx": system(deferred=700)}}
        self.assertTrue(judge(healthy(), record)[1].endswith("700 left"))

    def test_no_backlog_leaves_it_out(self) -> None:
        detail = judge(healthy(backlog=None))[1]
        self.assertEqual(detail, "last run 2026-11-14 03:00 UTC: added 120, filled 30, not found 4")

    def test_no_record_still_dates_the_run_from_the_state(self) -> None:
        detail = judge(healthy(), None)[1]
        self.assertTrue(
            detail.startswith("last run 2026-11-14 04:00 UTC: no record kept; 700 left"), detail
        )

    def test_a_night_that_never_scraped_says_so(self) -> None:
        detail = judge(State(checked=ago(hours=2)), None)[1]
        self.assertEqual(detail, "last check 2 h ago; no night has scraped anything yet")

    def test_a_damaged_state_file_is_a_warning_with_the_parsers_text(self) -> None:
        status, detail, _ = judge_nightly(healthy(), RECORD, NOW, "the state file is damaged in x")
        self.assertEqual(status, WARN)
        self.assertEqual(detail, "the state file is damaged in x")

    def test_it_is_the_one_doctor_exports(self) -> None:
        self.assertIs(doctor.judge_nightly, judge_nightly)
        self.assertIs(doctor.check_nightly_scrape, check_nightly_scrape)


class CheckTests(TmpCase):
    def setUp(self) -> None:
        super().setUp()
        self.root = self.tmp / "hub"
        (self.root / "inbox").mkdir(parents=True)
        self.scrape = self.root / "catalog" / "scrape"
        self.config = f'[inbox]\nroot = "{self.root}"\n'

    def line(self, config: str | None = None, **kw):
        kw.setdefault("now", lambda: NOW.timestamp())
        ctx = context(self.tmp, self.config if config is None else config, **kw)
        return check_nightly_scrape(ctx)

    def write_state(self, state: State) -> None:
        self.scrape.mkdir(parents=True, exist_ok=True)
        (self.scrape / "state.json").write_text(render_state(state))

    def test_with_no_state_it_gives_an_optional_line_in_the_tools_group(self) -> None:
        (result,) = self.line()
        self.assertEqual(
            (result.name, result.group, result.status),
            ("Nightly scrape", "Inbox tools", OPTIONAL),
        )
        self.assertTrue(result.fix)

    def test_it_reads_the_state_and_the_newest_record(self) -> None:
        self.write_state(healthy())
        runs = self.scrape / "runs"
        runs.mkdir()
        older = {**RECORD, "id": "20261113T030000Z", "started": "2026-11-13T03:00:02Z"}
        (runs / "20261113T030000Z.json").write_text(render_record(older))
        (runs / "20261114T030000Z.json").write_text(render_record(RECORD))
        (result,) = self.line()
        self.assertEqual(result.status, OK)
        self.assertEqual(
            result.detail,
            "last run 2026-11-14 03:00 UTC: added 120, filled 30, not found 4; "
            "700 left (about 2 nights at 500 a night)",
        )

    def test_a_record_that_does_not_parse_is_no_record(self) -> None:
        self.write_state(healthy())
        runs = self.scrape / "runs"
        runs.mkdir()
        (runs / "20261114T030000Z.json").write_text("{not json")
        (result,) = self.line()
        self.assertEqual(result.status, OK)
        self.assertIn("no record kept", result.detail)

    def test_a_state_that_does_not_parse_is_a_warning_with_the_parsers_text(self) -> None:
        self.scrape.mkdir(parents=True)
        (self.scrape / "state.json").write_text(json.dumps([1]))
        (result,) = self.line()
        self.assertEqual(result.status, WARN)
        self.assertIn("records no check", result.detail)
        self.assertIn("scrape state file is of an unknown form", result.detail)

    def test_a_damaged_part_is_shown_with_the_parsers_text(self) -> None:
        self.write_state(healthy())
        path = self.scrape / "state.json"
        doc = json.loads(path.read_text())
        doc["backlog"] = "many"
        path.write_text(json.dumps(doc))
        (result,) = self.line()
        self.assertEqual(result.status, WARN)
        self.assertIn("damaged in backlog", result.detail)

    def test_no_line_without_an_inbox_table(self) -> None:
        self.write_state(healthy())
        self.assertEqual(self.line(config='[hub]\nroot = "/nowhere"\n'), [])

    def test_no_line_when_the_hub_root_is_not_readable(self) -> None:
        self.assertEqual(self.line(config='[inbox]\nroot = "/nowhere/at/all"\n'), [])

    def test_the_hub_root_falls_back_to_the_hub_table(self) -> None:
        (result,) = self.line(config=f'[hub]\nroot = "{self.root}"\n\n[inbox]\n')
        self.assertEqual(result.status, OPTIONAL)

    def test_it_runs_right_after_skyscraper(self) -> None:
        checks = list(doctor.CHECKS)
        self.assertEqual(checks.index(check_nightly_scrape), checks.index(check_skyscraper) + 1)

    def test_doctor_shows_the_line_with_the_hub_state(self) -> None:
        self.write_state(healthy())
        probes = make_probes(self.tmp, now=lambda: NOW.timestamp())
        probes.config_path.write_text(self.config)
        results = doctor.run_checks(probes)
        self.assertEqual(by_name(results, "Nightly scrape").status, OK)
        names = [r.name for r in results]
        between = names[names.index("Skyscraper") + 1 : names.index("Nightly scrape")]
        self.assertLessEqual(set(between), {"ScreenScraper account"})

    def test_doctor_shows_no_line_without_an_inbox_table(self) -> None:
        self.write_state(healthy())
        probes = make_probes(self.tmp, now=lambda: NOW.timestamp())
        probes.config_path.write_text('[hub]\nroot = "/nowhere"\n')
        results = doctor.run_checks(probes)
        self.assertNotIn("Nightly scrape", [r.name for r in results])


if __name__ == "__main__":
    unittest.main()
