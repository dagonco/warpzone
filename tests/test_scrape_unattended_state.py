"""The nightly scrape's memory, the pure part: the state file and its changes, the mapping from a
run's summary to an outcome and a trouble, the news and the exit code, the run record and the
webhook payload. Plus the parameterized disk helpers the inbox shares with it."""

import json
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path

from warpzone import inbox_unattended_io, scrape, scrape_unattended
from warpzone.inbox_unattended import RECORDS_KEPT, RunError, clean_text, is_record_id, record_id
from warpzone.scrape import StopReason, Summary, SystemSummary
from warpzone.scrape_unattended import (
    NEWS_AFTER,
    OUTCOMES,
    TROUBLES,
    Backlog,
    Last,
    LockOutcome,
    NewsItem,
    Running,
    State,
    Trouble,
    exit_code,
    judge_night,
    news,
    notify_payload,
    parse_record,
    parse_state,
    render_record,
    render_state,
    should_notify,
    update,
    with_checked,
    with_error,
    with_error_cleared,
    with_night,
    with_notified,
    with_started,
)

T0 = datetime(2026, 11, 14, 3, 0, 2, tzinfo=UTC)


def night(n: int) -> datetime:
    return T0 + timedelta(days=n)


RUN = "20261114T030000Z"


def full_state() -> State:
    return State(
        checked=night(0),
        running=Running(RUN, night(0)),
        last=Last("20261113T030000Z", night(-1), "budget"),
        backlog=Backlog(1240, night(-1)),
        error=RunError("no skyscraper", 3, night(-2), 2, True),
        trouble=Trouble("network", ("snes", "nes"), night(-1), 2, True),
    )


def spent(n: int, kind: str | None, outcome: str = "failed", systems=(), **kw) -> State:
    """The state after `kind` happened on `n` nights in a row."""
    state = State()
    for i in range(n):
        state = update(state, with_night(RUN, night(i), outcome, kind, systems, **kw))
    return state


class StateFileTests(unittest.TestCase):
    def test_a_full_state_round_trips(self) -> None:
        state = full_state()
        text = render_state(state)
        self.assertEqual(parse_state(text), (state, None))
        self.assertEqual(json.loads(text)["v"], 1)
        self.assertTrue(text.endswith("\n"))

    def test_the_empty_state_round_trips_and_a_missing_file_is_empty_without_a_warning(
        self,
    ) -> None:
        self.assertEqual(parse_state(render_state(State())), (State(), None))
        self.assertEqual(parse_state(None), (State(), None))
        self.assertEqual(json.loads(render_state(State()))["trouble"], None)

    def test_the_format_is_the_documented_one(self) -> None:
        doc = json.loads(render_state(full_state()))
        self.assertEqual(doc["running"], {"run": RUN, "started": "2026-11-14T03:00:02Z"})
        self.assertEqual(doc["backlog"], {"left": 1240, "at": "2026-11-13T03:00:02Z"})
        self.assertEqual(
            doc["last"],
            {"run": "20261113T030000Z", "finished": "2026-11-13T03:00:02Z", "outcome": "budget"},
        )
        self.assertEqual(
            doc["trouble"],
            {
                "kind": "network",
                "systems": ["snes", "nes"],
                "first": "2026-11-13T03:00:02Z",
                "count": 2,
                "notified": True,
            },
        )
        self.assertEqual(set(doc["error"]), {"message", "step", "first", "count", "config"})

    def test_every_damaged_part_is_dropped_and_the_rest_kept(self) -> None:
        doc = json.loads(render_state(full_state()))
        for part in ("checked", "running", "last", "backlog", "error", "trouble"):
            broken = dict(doc)
            broken[part] = "garbage" if part != "checked" else 5
            state, warning = parse_state(json.dumps(broken))
            self.assertIsNotNone(warning, part)
            assert warning is not None
            self.assertIn(part, warning)
            kept = full_state()
            self.assertEqual(getattr(state, part), None, part)
            for other in ("checked", "running", "last", "backlog", "error", "trouble"):
                if other != part:
                    self.assertEqual(getattr(state, other), getattr(kept, other), other)

    def test_a_trouble_of_an_unknown_kind_or_an_outcome_of_an_unknown_name_is_dropped(self) -> None:
        doc = json.loads(render_state(full_state()))
        doc["trouble"]["kind"] = "sunspots"
        doc["last"]["outcome"] = "great"
        state, warning = parse_state(json.dumps(doc))
        self.assertIsNone(state.trouble)
        self.assertIsNone(state.last)
        assert warning is not None
        self.assertIn("trouble", warning)
        self.assertIn("last", warning)

    def test_a_count_that_is_not_a_positive_number_drops_the_trouble(self) -> None:
        doc = json.loads(render_state(full_state()))
        doc["trouble"]["count"] = 0
        self.assertIsNone(parse_state(json.dumps(doc))[0].trouble)
        doc["trouble"]["count"] = True
        self.assertIsNone(parse_state(json.dumps(doc))[0].trouble)

    def test_an_unparsable_file_reads_as_empty_with_a_warning(self) -> None:
        for text in ("{not json", "[]", '{"v": 2}', "", "null"):
            state, warning = parse_state(text)
            self.assertEqual(state, State(), text)
            self.assertIsNotNone(warning, text)

    def test_the_time_of_a_part_that_is_not_a_time_drops_that_part(self) -> None:
        doc = json.loads(render_state(full_state()))
        doc["running"]["started"] = "yesterday"
        state, _ = parse_state(json.dumps(doc))
        self.assertIsNone(state.running)
        self.assertEqual(state.last, full_state().last)


class ChangeTests(unittest.TestCase):
    def test_changes_are_pure_and_apply_in_order(self) -> None:
        state = State()
        again = update(state, with_checked(night(0)), with_checked(night(1)))
        self.assertEqual(state, State())
        self.assertEqual(again.checked, night(1))
        self.assertEqual(update(again), again)

    def test_applying_the_same_changes_to_another_state_gives_the_same_result(self) -> None:
        changes = (with_checked(night(2)), with_error("boom", 3, night(2)))
        base, other = State(), replace_checked(full_state())
        self.assertEqual(update(base, *changes), update(update(base), *changes))
        self.assertEqual(update(other, *changes).checked, night(2))

    def test_started_sets_running_and_a_clean_start_adds_no_trouble(self) -> None:
        state = update(State(), with_started(RUN, night(0)))
        self.assertEqual(state.running, Running(RUN, night(0)))
        self.assertIsNone(state.trouble)

    def test_a_start_that_finds_running_set_counts_a_died_night(self) -> None:
        dead = update(State(), with_started("old", night(0)))
        state = update(dead, with_started(RUN, night(1)))
        self.assertEqual(state.running, Running(RUN, night(1)))
        self.assertEqual(state.trouble, Trouble("died", (), night(1), 1, False))
        again = update(state, with_started("newer", night(2)))
        assert again.trouble is not None
        self.assertEqual((again.trouble.count, again.trouble.first), (2, night(1)))

    def test_a_died_night_replaces_another_kind_of_trouble(self) -> None:
        state = update(
            spent(2, "network"), with_started("old", night(5)), with_started(RUN, night(6))
        )
        assert state.trouble is not None
        self.assertEqual((state.trouble.kind, state.trouble.count), ("died", 1))

    def test_a_night_ends_the_run_and_records_it(self) -> None:
        state = update(
            State(),
            with_started(RUN, night(0)),
            with_night(RUN, night(0) + timedelta(hours=1), "budget", None, left=500),
        )
        self.assertIsNone(state.running)
        self.assertEqual(state.last, Last(RUN, night(0) + timedelta(hours=1), "budget"))
        self.assertEqual(state.backlog, Backlog(500, night(0) + timedelta(hours=1)))

    def test_a_night_that_does_not_say_what_is_left_keeps_the_backlog(self) -> None:
        state = update(
            State(backlog=Backlog(7, night(-1))), with_night(RUN, night(0), "busy", "lock")
        )
        self.assertEqual(state.backlog, Backlog(7, night(-1)))

    def test_a_night_that_never_took_the_lock_leaves_another_runs_running_alone(self) -> None:
        state = State(running=Running("someone else", night(0)))
        done = update(state, with_night(RUN, night(0), "busy", "lock"))
        self.assertEqual(done.running, Running("someone else", night(0)))

    def test_the_same_trouble_raises_the_count_and_keeps_the_first_night(self) -> None:
        state = spent(3, "network", "stopped", ("snes",))
        assert state.trouble is not None
        self.assertEqual(
            (state.trouble.kind, state.trouble.count, state.trouble.first),
            ("network", 3, night(0)),
        )
        self.assertEqual(state.trouble.systems, ("snes",))

    def test_another_kind_starts_a_new_streak(self) -> None:
        state = update(spent(2, "network"), with_night(RUN, night(2), "failed", "failed"))
        assert state.trouble is not None
        self.assertEqual(
            (state.trouble.kind, state.trouble.count, state.trouble.first), ("failed", 1, night(2))
        )
        self.assertFalse(state.trouble.notified)

    def test_a_good_night_clears_the_streak(self) -> None:
        for outcome in ("done", "budget", "nothing"):
            state = update(spent(2, "network"), with_night(RUN, night(2), outcome, None))
            self.assertIsNone(state.trouble, outcome)

    def test_a_quota_night_clears_another_streak_and_keeps_its_own(self) -> None:
        after_network = update(spent(2, "network"), with_night(RUN, night(2), "quota", "quota"))
        assert after_network.trouble is not None
        self.assertEqual((after_network.trouble.kind, after_network.trouble.count), ("quota", 1))
        streak = spent(3, "quota", "quota")
        assert streak.trouble is not None
        self.assertEqual((streak.trouble.kind, streak.trouble.count), ("quota", 3))
        self.assertIsNone(update(streak, with_night(RUN, night(3), "done", None)).trouble)

    def test_a_clean_night_after_a_died_one_keeps_the_died_count_it_just_made(self) -> None:
        state = update(
            State(running=Running("old", night(0))),
            with_started(RUN, night(1)),
            with_night(RUN, night(1), "done", None, died=True),
        )
        assert state.trouble is not None
        self.assertEqual((state.trouble.kind, state.trouble.count), ("died", 1))
        later = update(
            state, with_started("next", night(2)), with_night("next", night(2), "done", None)
        )
        self.assertIsNone(later.trouble)

    def test_notified_marks_the_streak_and_a_longer_streak_keeps_it(self) -> None:
        state = update(spent(2, "network"), with_notified())
        assert state.trouble is not None
        self.assertTrue(state.trouble.notified)
        more = update(state, with_night(RUN, night(2), "stopped", "network"))
        assert more.trouble is not None
        self.assertTrue(more.trouble.notified)
        self.assertEqual(update(State(), with_notified()), State())

    def test_an_error_is_kept_by_message_and_its_count_rises(self) -> None:
        state = update(State(), with_error("boom", 3, night(0)), with_error("boom", 3, night(1)))
        assert state.error is not None
        self.assertEqual((state.error.count, state.error.first), (2, night(0)))
        other = update(state, with_error("bang", 5, night(2), False))
        self.assertEqual(other.error, RunError("bang", 5, night(2), 1, False))

    def test_an_error_is_cleared_by_a_run_that_gets_past_its_step(self) -> None:
        state = State(error=RunError("no skyscraper", 3, night(0)))
        self.assertEqual(update(state, with_error_cleared(3)), state)
        self.assertIsNone(update(state, with_error_cleared(4)).error)
        self.assertEqual(update(State(), with_error_cleared(9)), State())


def replace_checked(state: State) -> State:
    return update(state, with_checked(night(0)))


class StreakNewsTests(unittest.TestCase):
    def test_the_thresholds(self) -> None:
        self.assertEqual(NEWS_AFTER["blacklisted"], 1)
        for kind in ("network", "busy", "failed", "lock", "died"):
            self.assertEqual(NEWS_AFTER[kind], 2, kind)
        self.assertEqual(NEWS_AFTER["closed"], 3)
        self.assertIsNone(NEWS_AFTER["quota"])
        self.assertEqual(set(NEWS_AFTER), set(TROUBLES))

    def night_news(self, kind: str, nights: int) -> list[bool]:
        """Whether each of `nights` nights of `kind` is news, reported (notified) once."""
        state, found = State(), []
        for i in range(nights):
            after = update(state, with_night(RUN, night(i), "failed", kind))
            items = news(state, after)
            found.append(bool(items))
            state = update(after, with_notified()) if items else after
        return found

    def test_blacklisted_is_news_on_the_first_night_and_only_once(self) -> None:
        self.assertEqual(self.night_news("blacklisted", 3), [True, False, False])

    def test_two_night_kinds_are_news_on_the_second_night_and_only_once(self) -> None:
        for kind in ("network", "busy", "failed", "lock", "died"):
            self.assertEqual(self.night_news(kind, 4), [False, True, False, False], kind)

    def test_closed_is_news_on_the_third_night(self) -> None:
        self.assertEqual(self.night_news("closed", 4), [False, False, True, False])

    def test_quota_is_never_news(self) -> None:
        self.assertEqual(self.night_news("quota", 6), [False] * 6)

    def test_a_streak_not_yet_reported_is_news_again_until_it_is_marked(self) -> None:
        before = spent(1, "network")
        after = update(before, with_night(RUN, night(1), "stopped", "network"))
        self.assertTrue(news(before, after))
        self.assertTrue(news(after, update(after, with_checked(night(2)))))

    def test_a_good_night_clears_the_streak_so_the_next_one_is_news_again(self) -> None:
        state = update(spent(2, "network"), with_notified())
        state = update(state, with_night(RUN, night(2), "done", None))
        state = update(state, with_night(RUN, night(3), "stopped", "network"))
        again = update(state, with_night(RUN, night(4), "stopped", "network"))
        found = news(state, again)
        self.assertEqual([i.kind for i in found], ["network"])

    def test_the_item_names_the_trouble_and_its_nights(self) -> None:
        before = spent(1, "failed", systems=("snes",))
        after = update(before, with_night(RUN, night(1), "failed", "failed", ("snes",)))
        (item,) = news(before, after)
        self.assertEqual(item, NewsItem("trouble", "failed", 2, False, ("snes",)))

    def test_a_new_error_is_news_once_and_the_same_message_is_not(self) -> None:
        first = update(State(), with_error("no skyscraper", 3, night(0)))
        (item,) = news(State(), first)
        self.assertEqual(item.source, "error")
        self.assertTrue(item.config)
        self.assertEqual(item.kind, "no skyscraper")
        again = update(first, with_error("no skyscraper", 3, night(1)))
        self.assertEqual(news(first, again), [])
        self.assertEqual(len(news(first, update(first, with_error("other", 4, night(1))))), 1)

    def test_an_error_met_again_after_it_was_cleared_is_news_again(self) -> None:
        cleared = update(update(State(), with_error("boom", 3, night(0))), with_error_cleared(4))
        again = update(cleared, with_error("boom", 3, night(2)))
        self.assertEqual(len(news(cleared, again)), 1)

    def test_no_change_is_no_news(self) -> None:
        state = full_state()
        self.assertEqual(news(state, state), [])


class ExitCodeTests(unittest.TestCase):
    def test_no_news_is_zero(self) -> None:
        self.assertEqual(exit_code([]), 0)

    def test_runtime_news_is_one(self) -> None:
        self.assertEqual(exit_code([NewsItem("trouble", "network", 2)]), 1)
        self.assertEqual(exit_code([NewsItem("error", "lock lost", 1, False)]), 1)

    def test_a_configuration_error_is_two_and_wins_over_runtime_news(self) -> None:
        config = NewsItem("error", "no skyscraper", 1, True)
        runtime = NewsItem("trouble", "network", 2)
        self.assertEqual(exit_code([config]), 2)
        self.assertEqual(exit_code([runtime, config]), 2)
        self.assertEqual(exit_code([config, runtime]), 2)


def systems(*names: str, **kw) -> tuple[SystemSummary, ...]:
    return tuple(SystemSummary(n, **kw) for n in names)


class JudgeNightTests(unittest.TestCase):
    def test_a_summary_with_no_stop_and_no_failure_is_done(self) -> None:
        verdict = judge_night(Summary(systems("snes")))
        self.assertEqual((verdict.outcome, verdict.kind), ("done", None))

    def test_no_summary_is_nothing(self) -> None:
        verdict = judge_night(None)
        self.assertEqual((verdict.outcome, verdict.kind), ("nothing", None))

    def test_every_stop_reason(self) -> None:
        expected = {
            StopReason.QUOTA: ("quota", "quota"),
            StopReason.DEADLINE: ("budget", None),
            StopReason.BLACKLISTED: ("stopped", "blacklisted"),
            StopReason.CLOSED: ("stopped", "closed"),
            StopReason.BUSY: ("stopped", "busy"),
            StopReason.NETWORK: ("stopped", "network"),
            StopReason.LOCK_LOST: ("stopped", "lock"),
        }
        self.assertEqual(set(expected), set(StopReason))
        for stop, (outcome, kind) in expected.items():
            verdict = judge_night(Summary(systems("snes"), stop=stop, not_tried=3))
            self.assertEqual((verdict.outcome, verdict.kind), (outcome, kind), stop)

    def test_failed_systems_with_no_stop_is_failed_and_names_them(self) -> None:
        verdict = judge_night(Summary(systems("snes", "nes"), failed_systems=("nes",), failed=True))
        self.assertEqual(
            (verdict.outcome, verdict.kind, verdict.systems), ("failed", "failed", ("nes",))
        )

    def test_a_failure_with_no_system_named_is_still_failed(self) -> None:
        verdict = judge_night(Summary(systems("snes"), failed=True))
        self.assertEqual((verdict.outcome, verdict.kind, verdict.systems), ("failed", "failed", ()))

    def test_a_night_that_ran_out_of_time_with_a_failed_system_is_failed(self) -> None:
        verdict = judge_night(
            Summary(
                systems("snes"), stop=StopReason.DEADLINE, failed_systems=("snes",), failed=True
            )
        )
        self.assertEqual((verdict.outcome, verdict.kind), ("failed", "failed"))

    def test_a_stop_of_screenscraper_or_the_quota_wins_over_failed_systems(self) -> None:
        for stop, outcome in ((StopReason.QUOTA, "quota"), (StopReason.NETWORK, "stopped")):
            verdict = judge_night(
                Summary(systems("snes"), stop=stop, failed_systems=("snes",), failed=True)
            )
            self.assertEqual(verdict.outcome, outcome, stop)

    def test_a_busy_lock_is_busy_and_the_trouble_lock(self) -> None:
        verdict = judge_night(None, LockOutcome.BUSY)
        self.assertEqual((verdict.outcome, verdict.kind), ("busy", "lock"))

    def test_the_names_are_the_documented_ones(self) -> None:
        self.assertEqual(
            OUTCOMES, ("done", "budget", "quota", "busy", "stopped", "failed", "nothing")
        )
        self.assertEqual(
            set(TROUBLES),
            {"blacklisted", "network", "busy", "failed", "lock", "died", "closed", "quota"},
        )


def summary_with_games() -> Summary:
    return Summary(
        (
            SystemSummary(
                "snes",
                added=2,
                filled=1,
                unchanged=5,
                not_found=1,
                added_paths=("A (USA).zip", "B (Europe).zip"),
                filled_paths=("C (Japan).zip",),
                warnings=("a.zip: dropped",),
            ),
            SystemSummary("nes", added=1, added_paths=("D.zip",)),
        ),
        stop=StopReason.DEADLINE,
        not_tried=7,
    )


def build(**kw):
    args = dict(
        run_id=RUN,
        started=night(0),
        finished=night(0) + timedelta(minutes=83),
        outcome="budget",
        status=0,
        deferred={"snes": 6, "psx": 4},
        skipped_not_found={"snes": 3, "gba": 2},
        skipped_incomplete={"snes": 4},
    )
    args.update(kw)
    summary = args.pop("summary", summary_with_games())
    return scrape_unattended.build_record(summary, **args)


class RecordTests(unittest.TestCase):
    def test_the_record_holds_the_counts_per_system(self) -> None:
        record = build()
        self.assertEqual(record["v"], 1)
        self.assertEqual(record["id"], RUN)
        self.assertEqual(record["outcome"], "budget")
        self.assertEqual(record["started"], "2026-11-14T03:00:02Z")
        self.assertEqual(record["finished"], "2026-11-14T04:23:02Z")
        self.assertEqual(
            record["systems"]["snes"],
            {
                "added": 2,
                "filled": 1,
                "not_found": 1,
                "unchanged": 5,
                "skipped_not_found": 3,
                "skipped_incomplete": 4,
                "deferred": 6,
            },
        )
        self.assertEqual(record["systems"]["nes"]["deferred"], 0)
        self.assertEqual(record["systems"]["psx"]["deferred"], 4)  # deferred, never reached
        self.assertEqual(record["systems"]["psx"]["added"], 0)
        # skipped in planning, never reached: still in the record
        self.assertEqual(
            record["systems"]["gba"],
            {key: 0 for key in record["systems"]["psx"]} | {"skipped_not_found": 2},
        )
        self.assertEqual(record["systems"]["nes"]["skipped_incomplete"], 0)
        self.assertEqual((record["stop"], record["budget_spent"]), ("deadline", True))
        self.assertEqual((record["exit"], record["notified"]), (0, False))
        self.assertEqual(record["warnings"], ["a.zip: dropped"])

    def test_the_paths_are_the_library_paths_below_the_hub_root(self) -> None:
        record = build()
        self.assertEqual(
            record["added"],
            [
                "library/roms/snes/A (USA).zip",
                "library/roms/snes/B (Europe).zip",
                "library/roms/nes/D.zip",
            ],
        )
        self.assertEqual(record["filled"], ["library/roms/snes/C (Japan).zip"])

    def test_the_paths_and_the_warnings_are_capped(self) -> None:
        many = tuple(f"G{n}.zip" for n in range(300))
        summary = Summary((SystemSummary("snes", added=300, added_paths=many, warnings=many),))
        record = build(summary=summary, outcome="done")
        self.assertEqual(len(record["added"]), scrape_unattended.PATHS_MAX)
        self.assertEqual(scrape_unattended.PATHS_MAX, 200)
        self.assertEqual(len(record["warnings"]), scrape_unattended.WARNINGS_MAX)
        self.assertEqual(record["systems"]["snes"]["added"], 300)  # the count is not capped

    def test_extra_warnings_come_last_and_are_cleaned(self) -> None:
        record = build(
            warnings=["could not send /srv/hub/inbox/x to https://example.test/hook?k=1"],
            roots=["/srv/hub"],
        )
        self.assertEqual(record["warnings"][0], "a.zip: dropped")
        text = " ".join(record["warnings"])
        self.assertNotIn("/srv/hub", text)

    def test_the_failed_systems_and_the_stop_are_there(self) -> None:
        summary = Summary(
            systems("snes", "nes"), stop=StopReason.LOCK_LOST, failed_systems=("nes",), failed=True
        )
        record = build(summary=summary, outcome="stopped", status=1, notified=True)
        self.assertEqual(record["stop"], "lock lost")
        self.assertEqual(record["failed_systems"], ["nes"])
        self.assertFalse(record["budget_spent"])
        self.assertEqual((record["exit"], record["notified"]), (1, True))

    def test_a_record_round_trips(self) -> None:
        record = build()
        text = render_record(record)
        self.assertEqual(parse_record(text), record)
        self.assertTrue(text.endswith("\n"))

    def test_a_record_that_is_not_one_reads_as_none(self) -> None:
        good = build()
        for broken in (
            None,
            "{nope",
            "[]",
            json.dumps({**good, "v": 9}),
            json.dumps({**good, "id": "../x"}),
            json.dumps({**good, "outcome": "great"}),
            json.dumps({**good, "systems": []}),
        ):
            self.assertIsNone(parse_record(broken), broken)

    def test_a_damaged_system_is_dropped_and_the_rest_kept(self) -> None:
        doc = build()
        doc["systems"]["nes"] = {"added": "many"}
        parsed = parse_record(json.dumps(doc))
        assert parsed is not None
        self.assertNotIn("nes", parsed["systems"])
        self.assertIn("snes", parsed["systems"])

    def test_the_record_id_is_the_inbox_one(self) -> None:
        self.assertTrue(is_record_id(record_id(night(0))))


class NotifyTests(unittest.TestCase):
    def test_the_payload_is_the_documented_one(self) -> None:
        record = build()
        items = [NewsItem("trouble", "network", 2)]
        self.assertEqual(
            notify_payload(record, items, left=1240),
            {
                "source": "scrape",
                "mode": "unattended",
                "run": RUN,
                "added": 3,
                "filled": 1,
                "not_found": 1,
                "left": 1240,
                "problem": "network",
            },
        )

    def test_an_error_is_a_problem_called_error_and_its_text_is_not_in_the_payload(self) -> None:
        payload = notify_payload(
            build(), [NewsItem("error", "no Skyscraper at /opt/x", 1, True)], left=0
        )
        self.assertEqual(payload["problem"], "error")
        self.assertNotIn("/opt", json.dumps(payload))

    def test_a_trouble_is_named_before_an_error(self) -> None:
        items = [NewsItem("error", "boom", 1, False), NewsItem("trouble", "failed", 2)]
        self.assertEqual(notify_payload(build(), items, left=0)["problem"], "failed")

    def test_the_payload_holds_no_url_and_no_absolute_path(self) -> None:
        record = build(warnings=["see https://example.test/hook?token=abc and /srv/hub/x"])
        payload = json.dumps(notify_payload(record, [NewsItem("trouble", "network", 2)], left=5))
        self.assertNotIn("http", payload)
        self.assertNotIn("/srv", payload)
        self.assertNotIn("token", payload)

    def test_only_news_is_sent(self) -> None:
        self.assertFalse(should_notify([]))
        self.assertTrue(should_notify([NewsItem("trouble", "network", 2)]))
        self.assertTrue(should_notify([NewsItem("error", "boom", 1, True)]))

    def test_clean_text_is_the_inbox_one(self) -> None:
        self.assertEqual(clean_text("at /etc/x"), "at <path>")


class PathsTests(unittest.TestCase):
    def test_the_paths_are_below_catalog_scrape(self) -> None:
        root = Path("/hub")
        self.assertEqual(scrape_unattended.state_path(root), root / "catalog/scrape/state.json")
        self.assertEqual(scrape_unattended.runs_dir(root), root / "catalog/scrape/runs")


class SharedIoTests(unittest.TestCase):
    """The helpers of `inbox_unattended_io` with the nightly scrape's folder and functions."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.store = inbox_unattended_io.StateFile(
            scrape_unattended.state_path,
            scrape_unattended.parse_state,
            scrape_unattended.render_state,
            scrape_unattended.STATE_SHOWN,
        )

    def write(self, started: datetime):
        return inbox_unattended_io.write_record(
            self.root,
            started,
            lambda rid: build(run_id=rid, started=started),
            folder=scrape_unattended.runs_dir(self.root),
            render=scrape_unattended.render_record,
        )

    def test_records_go_to_the_scrape_folder_and_never_replace_one(self) -> None:
        first, second = self.write(night(0)), self.write(night(0))
        runs = scrape_unattended.runs_dir(self.root)
        rid = record_id(night(0))
        self.assertEqual(sorted(p.name for p in runs.iterdir()), [f"{rid}-2.json", f"{rid}.json"])
        self.assertEqual(second["id"], f"{rid}-2")
        self.assertEqual(parse_record((runs / f"{rid}.json").read_text()), first)
        self.assertFalse((self.root / "catalog" / "inbox").exists())

    def test_the_purge_keeps_the_newest_fifty_of_the_scrape_folder(self) -> None:
        runs = scrape_unattended.runs_dir(self.root)
        runs.mkdir(parents=True)
        for n in range(60):
            (runs / f"202611{n // 24 + 1:02d}T{n % 24:02d}0000Z.json").write_text("{}")
        (runs / "README.txt").write_text("x")
        gone = inbox_unattended_io.purge_records(self.root, folder=runs)
        self.assertEqual(len(gone), 60 - RECORDS_KEPT)
        self.assertEqual(len(list(runs.iterdir())), RECORDS_KEPT + 1)
        self.assertEqual(inbox_unattended_io.purge_records(self.root, folder=runs), [])
        self.assertEqual(inbox_unattended_io.purge_records(self.root, folder=self.root / "no"), [])

    def test_the_state_is_read_modified_and_written_with_its_own_functions(self) -> None:
        state, warnings, wrote = inbox_unattended_io.update_file_checked(
            self.root, self.store, with_checked(night(0)), with_started(RUN, night(0))
        )
        self.assertTrue(wrote)
        self.assertEqual(warnings, [])
        path = scrape_unattended.state_path(self.root)
        self.assertEqual(parse_state(path.read_text()), (state, None))
        again, _, _ = inbox_unattended_io.update_file_checked(
            self.root, self.store, with_night(RUN, night(0), "done", None, left=0)
        )
        self.assertEqual(again.last, Last(RUN, night(0), "done"))
        self.assertEqual(again.checked, night(0))
        self.assertEqual(inbox_unattended_io.read_file(self.root, self.store), (again, None))
        self.assertFalse((self.root / "catalog" / "inbox").exists())

    def test_a_damaged_state_file_is_read_with_a_warning_of_the_scrape(self) -> None:
        path = scrape_unattended.state_path(self.root)
        path.parent.mkdir(parents=True)
        path.write_text("{broken")
        state, warnings, wrote = inbox_unattended_io.update_file_checked(
            self.root, self.store, with_checked(night(0))
        )
        self.assertTrue(wrote)
        self.assertEqual(state.checked, night(0))
        self.assertIn("scrape state file", warnings[0])

    def test_the_inbox_calls_still_work_without_the_new_arguments(self) -> None:
        from warpzone.inbox_unattended import with_checked as inbox_checked

        state, _, wrote = inbox_unattended_io.update_state_checked(
            self.root, inbox_checked(night(0))
        )
        self.assertTrue(wrote)
        self.assertTrue((self.root / "catalog/inbox/state.json").is_file())
        self.assertEqual(inbox_unattended_io.read_state(self.root)[0], state)


class UsesTheSameNamesAsTheDesignTests(unittest.TestCase):
    def test_the_scrape_module_exports_what_the_run_will_import(self) -> None:
        for name in (
            "State",
            "Change",
            "update",
            "news",
            "exit_code",
            "notify_payload",
            "should_notify",
            "render_state",
            "parse_state",
            "render_record",
            "parse_record",
            "build_record",
            "judge_night",
            "NEWS_AFTER",
        ):
            self.assertTrue(hasattr(scrape_unattended, name), name)
        self.assertTrue(hasattr(scrape, "Summary"))


if __name__ == "__main__":
    unittest.main()
