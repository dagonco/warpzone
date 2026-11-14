"""`warpzone scrape --unattended` end to end: nights of the scheduled run over a temporary hub with
the stand-in Skyscraper, the clock and the sleep injected, and a stand-in webhook server."""

import json
import os
import shutil
import time
import unittest
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from unittest import mock

from notify_support import Receiver
from scrape_support import CHRONO, HOMEBREW, KIRBY, TODAY, HubCase
from test_cli import run as run_cli
from warpzone import inbox_lock, scrape, scrape_io, scrape_unattended
from warpzone import scrape_unattended_run as nightly
from warpzone.errors import UsageError
from warpzone.inbox_core import InboxError
from warpzone.report import Reporter
from warpzone.scrape_unattended import State, render_state

ZELDA = "Zelda (USA).zip"
KNOWN3 = ["Kirby Super Star (USA)", "Chrono Trigger (USA)", "Example Homebrew (World)"]
KNOWN4 = [*KNOWN3, "Zelda (USA)"]
DAY = 86400
NO_VIDEOS = ["covers", "screenshots", "miximages", "marquees", "backcovers"]
SECRET = "secret-id"


class Clock:
    """A clock that only moves when a test (or the run's sleep) moves it."""

    def __init__(self, start: float) -> None:
        self.t = start
        self.slept: list[float] = []

    def __call__(self) -> float:
        return self.t

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.t += seconds


class NightCase(HubCase):
    def setUp(self) -> None:
        super().setUp()
        nice = mock.patch("os.nice")
        self.nice = nice.start()
        self.addCleanup(nice.stop)
        (self.hub.root / "library" / ".stfolder").mkdir(parents=True)
        self.clock = Clock(time.time() + DAY)
        self.out: list[str] = []
        self.err: list[str] = []
        self.sleep = self.clock.sleep

    def games(self, *names: str) -> None:
        for name in names:
            self.hub.add_game(name)

    def setup_for(self, **overrides: Any) -> nightly.Setup:
        values: dict[str, Any] = {
            "root": self.hub.root,
            "systems": ("snes",),
            "explicit": False,
            "skyscraper": str(self.hub.tool),
            "timeout": 60.0,
            "work_dir": self.hub.work,
            "cache_dir": self.hub.cache,
            "config_ini": self.hub.config_ini,
            "now": self.clock,
            "today": lambda: TODAY,
            "sleep": lambda seconds: self.sleep(seconds),
            "which": lambda name: None,
        }
        values.update(overrides)
        return nightly.Setup(**values)

    def night(self, **overrides: Any) -> int:
        """One night. The output of the earlier ones is dropped."""
        self.out.clear()
        self.err.clear()
        rep = Reporter(self.out.append, warn_sink=self.err.append)
        self.rep = rep
        return nightly.run_unattended(self.setup_for(**overrides), rep)

    def next_day(self) -> None:
        self.clock.t += DAY

    # ------------------------------------------------------------ what a night leaves

    @property
    def state_file(self) -> Path:
        return self.hub.root / "catalog" / "scrape" / "state.json"

    def state(self) -> dict[str, Any]:
        return json.loads(self.state_file.read_text())  # type: ignore[no-any-return]

    def records(self) -> list[dict[str, Any]]:
        folder = self.hub.root / "catalog" / "scrape" / "runs"
        return [json.loads(p.read_text()) for p in sorted(folder.glob("*.json"))]

    @property
    def lock_path(self) -> Path:
        return self.hub.root / "inbox" / ".warpzone.lock"

    def write_lock(self, **fields: Any) -> None:
        info = {
            "host": "other-host",
            "pid": 4242,
            "started": self.clock.t - 60,
            "beat": 60,
            "mode": "unattended",
            **fields,
        }
        self.lock_path.write_text(json.dumps(info) + "\n")

    def everything_written(self) -> str:
        """All the text a night left or printed, to look for what must never be in it."""
        parts = ["\n".join(self.out), "\n".join(self.err), "\n".join(self.rep.warnings)]
        for path in (self.hub.root / "catalog").rglob("*"):
            if path.is_file():
                parts.append(path.read_text())
        return "\n".join(parts)


class NothingToDoTest(NightCase):
    def complete(self, stem: str, name: str) -> None:
        self.hub.write_gamelist(
            f'<?xml version="1.0"?>\n<gameList>\n\t<game>\n\t\t<path>./{name}</path>\n'
            f"\t\t<name>x</name>\n\t</game>\n</gameList>\n"
        )
        for kind in scrape.MEDIA_KINDS:
            self.hub.put_media(kind, stem, ext="mp4" if kind == "videos" else "png")

    def test_it_writes_only_checked_takes_no_lock_and_starts_no_skyscraper(self) -> None:
        self.games(CHRONO)
        self.complete("Chrono Trigger (USA)", CHRONO)
        with mock.patch.object(inbox_lock, "hold", side_effect=AssertionError("took the lock")):
            self.assertEqual(self.night(), 0)
        self.assertEqual(self.hub.calls(), [])
        self.assertEqual(self.out + self.err, [])
        self.nice.assert_not_called()
        doc = self.state()
        self.assertEqual(doc["checked"], scrape_unattended.stamp(self.clock_datetime()))
        self.assertEqual(
            {k: v for k, v in doc.items() if k not in ("v", "checked")},
            {"running": None, "last": None, "backlog": None, "error": None, "trouble": None},
        )
        self.assertFalse((self.hub.root / "catalog" / "scrape" / "runs").exists())
        self.assertFalse(self.lock_path.exists())

    def clock_datetime(self) -> datetime:
        return datetime.fromtimestamp(self.clock.t, UTC)

    def test_a_game_the_incomplete_memory_covers_is_nothing_to_do(self) -> None:
        self.games(KIRBY, CHRONO)
        self.hub.canned({"known": KNOWN3[:2], "kinds": NO_VIDEOS})
        self.hub.run()  # an attended scrape records what ScreenScraper has no video for
        before = len(self.hub.calls())
        self.next_day()
        self.assertEqual(self.night(), 0)
        self.assertEqual(len(self.hub.calls()), before)
        self.assertIsNone(self.state()["last"])

    def test_it_clears_the_errors_of_the_steps_it_got_past(self) -> None:
        self.games(CHRONO)
        self.complete("Chrono Trigger (USA)", CHRONO)
        self.assertEqual(self.night(skyscraper=None), 2)
        self.assertEqual(self.state()["error"]["step"], nightly.STEP_CHECK)
        self.next_day()
        self.assertEqual(self.night(), 0)
        self.assertIsNone(self.state()["error"])


class RunTest(NightCase):
    def test_a_capped_night_scrapes_n_games_and_writes_the_record(self) -> None:
        self.games(KIRBY, CHRONO, HOMEBREW)
        self.hub.canned({"known": KNOWN3})
        self.assertEqual(self.night(max_games=2), 0, self.out + self.err)
        (seen,) = self.hub.gathers()
        self.assertEqual(set(seen["targets"]), {CHRONO, HOMEBREW})  # first two in path order
        (record,) = self.records()
        self.assertEqual(record["outcome"], "done")
        self.assertEqual(record["systems"]["snes"]["added"], 2)
        self.assertEqual(record["systems"]["snes"]["deferred"], 1)
        self.assertEqual(
            record["added"], [f"library/roms/snes/{CHRONO}", f"library/roms/snes/{HOMEBREW}"]
        )
        self.assertEqual((record["exit"], record["notified"], record["stop"]), (0, False, None))
        doc = self.state()
        self.assertEqual(doc["last"]["outcome"], "done")
        self.assertEqual(doc["last"]["run"], record["id"])
        self.assertEqual(doc["backlog"]["left"], 1)
        self.assertIsNone(doc["running"])
        self.assertIsNone(doc["trouble"])
        self.assertFalse(self.lock_path.exists())
        self.nice.assert_called_once_with(10)
        self.assertIn(f"run record: catalog/scrape/runs/{record['id']}.json", self.out)

    def test_the_next_night_takes_what_the_cap_left(self) -> None:
        self.games(KIRBY, CHRONO, HOMEBREW)
        self.hub.canned({"known": KNOWN3})
        self.night(max_games=2)
        self.next_day()
        self.assertEqual(self.night(max_games=2), 0)
        self.assertEqual(set(self.hub.gathers()[-1]["targets"]), {KIRBY})
        self.assertEqual(self.state()["backlog"]["left"], 0)
        self.assertEqual(len(self.records()), 2)

    def test_it_holds_the_lock_with_the_scrape_task_while_it_works(self) -> None:
        self.games(KIRBY)
        self.hub.canned({"known": KNOWN3})
        seen = []
        real = scrape_io.run_held

        def look(opts: Any, rep: Any, **kw: Any) -> Any:
            seen.append(inbox_lock.parse(self.lock_path.read_text()))
            seen.append(self.state()["running"])
            return real(opts, rep, **kw)

        with mock.patch.object(scrape_io, "run_held", look):
            self.night()
        info, running = seen
        self.assertEqual((info.mode, info.task), ("unattended", "scrape"))
        self.assertIsNotNone(running)

    def test_the_deadline_stops_before_the_second_batch(self) -> None:
        self.games(KIRBY, CHRONO, HOMEBREW, ZELDA)
        self.hub.canned({"known": KNOWN4})
        real = scrape_io._scrape_batch

        def slow(*args: Any, **kw: Any) -> Any:
            got = real(*args, **kw)
            self.clock.t += 50  # a batch of two games that took 50 seconds
            return got

        with (
            mock.patch.object(scrape, "BATCH_SIZE", 2),
            mock.patch.object(scrape_io, "_scrape_batch", slow),
        ):
            code = self.night(max_minutes=1)
        self.assertEqual(code, 0, self.out + self.err)
        (record,) = self.records()
        self.assertEqual(record["outcome"], "budget")
        self.assertEqual((record["stop"], record["budget_spent"]), ("deadline", True))
        self.assertEqual(record["systems"]["snes"]["added"], 2)
        self.assertEqual(len(self.hub.gathers()), 1)
        doc = self.state()
        self.assertEqual(doc["last"]["outcome"], "budget")
        self.assertEqual(doc["backlog"]["left"], 0)  # `Night.backlog`: what the cap deferred
        self.assertIsNone(doc["trouble"])

    def test_the_quota_exits_0_and_is_a_streak_that_is_never_news(self) -> None:
        self.games(KIRBY, CHRONO, HOMEBREW)
        self.hub.canned({"known": KNOWN3[1:2], "quota_after": 1})
        with Receiver() as hook:
            self.assertEqual(self.night(notify_url=hook.url), 0, self.out + self.err)
            self.next_day()
            self.assertEqual(self.night(notify_url=hook.url), 0)
            self.assertEqual(hook.bodies, [])
        doc = self.state()
        self.assertEqual(doc["last"]["outcome"], "quota")
        self.assertEqual(doc["trouble"]["kind"], "quota")
        self.assertEqual(doc["trouble"]["count"], 2)
        self.assertFalse(doc["trouble"]["notified"])
        self.assertEqual(self.records()[0]["stop"], "quota")

    def test_network_trouble_over_three_nights_exits_0_1_0_with_one_webhook(self) -> None:
        self.games(KIRBY, CHRONO, HOMEBREW)
        self.hub.canned({"unreachable": True})
        with Receiver() as hook:
            first = self.night(notify_url=hook.url)
            self.assertEqual(first, 0, self.out + self.err)
            self.assertEqual(self.state()["trouble"]["count"], 1)
            self.assertEqual(hook.bodies, [])
            self.next_day()
            second = self.night(notify_url=hook.url)
            self.assertEqual(second, 1, self.out + self.err)
            (body,) = hook.bodies
            self.next_day()
            third = self.night(notify_url=hook.url)
            self.assertEqual(third, 0, self.out + self.err)
            self.assertEqual(len(hook.bodies), 1)
        self.assertEqual(
            {k: body[k] for k in ("source", "mode", "problem", "added", "left")},
            {"source": "scrape", "mode": "unattended", "problem": "network", "added": 0, "left": 0},
        )
        trouble = self.state()["trouble"]
        self.assertEqual(
            (trouble["kind"], trouble["count"], trouble["notified"]), ("network", 3, True)
        )
        records = self.records()
        self.assertEqual([r["exit"] for r in records], [0, 1, 0])
        self.assertEqual([r["notified"] for r in records], [False, True, False])
        self.assertEqual(body["run"], records[1]["id"])
        self.assertNotIn(SECRET, self.everything_written())

    def test_a_night_without_trouble_ends_the_streak(self) -> None:
        self.games(KIRBY)
        self.hub.canned({"unreachable": True})
        self.night()
        self.assertEqual(self.state()["trouble"]["count"], 1)
        self.hub.canned({"known": KNOWN3})
        self.next_day()
        self.assertEqual(self.night(), 0)
        self.assertIsNone(self.state()["trouble"])

    def test_a_failed_webhook_is_a_warning_and_the_url_is_not_printed(self) -> None:
        self.games(KIRBY)
        with Receiver(status=500) as hook:
            code = self.night(skyscraper=None, notify_url=hook.url)
            self.assertEqual(len(hook.bodies), 1)
        self.assertEqual(code, 2)
        self.assertTrue(any("notification rejected" in line for line in self.err), self.err)
        self.assertNotIn(SECRET, self.everything_written())
        (record,) = self.records()
        self.assertFalse(record["notified"])

    def test_an_unexpected_failure_is_news_once_and_leaves_no_lock_or_running(self) -> None:
        self.games(KIRBY)
        boom = RuntimeError(f"boom {self.hub.root}/library/x")
        with mock.patch.object(scrape_io, "run_held", side_effect=boom):
            self.assertEqual(self.night(), 1)
            self.next_day()
            self.assertEqual(self.night(), 0)
        doc = self.state()
        self.assertEqual(doc["error"]["count"], 2)
        self.assertFalse(doc["error"]["config"])
        self.assertEqual(
            doc["error"]["message"], "unexpected failure: RuntimeError: boom library/x"
        )
        self.assertIsNone(doc["running"])
        self.assertEqual(doc["last"]["outcome"], "failed")
        self.assertFalse(self.lock_path.exists())

    def test_a_clean_night_after_an_error_clears_it(self) -> None:
        self.games(KIRBY)
        self.hub.canned({"known": KNOWN3})
        with mock.patch.object(scrape_io, "run_held", side_effect=RuntimeError("boom")):
            self.night()
        self.next_day()
        self.assertEqual(self.night(), 0)
        self.assertIsNone(self.state()["error"])

    def test_the_webhook_of_an_error_night_keeps_the_backlog_it_knew(self) -> None:
        self.games(KIRBY, CHRONO, HOMEBREW)
        self.hub.canned({"known": KNOWN3})
        self.assertEqual(self.night(max_games=1), 0, self.out + self.err)
        self.assertEqual(self.state()["backlog"]["left"], 2)
        self.next_day()
        with Receiver() as hook:
            self.assertEqual(self.night(skyscraper=None, notify_url=hook.url), 2)
            (body,) = hook.bodies
        self.assertEqual((body["problem"], body["left"]), ("error", 2))
        self.assertEqual(self.state()["backlog"]["left"], 2)  # not overwritten

    def test_an_error_before_any_backlog_is_known_says_so(self) -> None:
        self.games(KIRBY)
        with Receiver() as hook:
            self.assertEqual(self.night(skyscraper=None, notify_url=hook.url), 2)
            (body,) = hook.bodies
        self.assertIsNone(body["left"])
        self.assertIsNone(self.state()["backlog"])


NES_ENTRY = (
    '<?xml version="1.0"?>\n<gameList>\n\t<game>\n\t\t<path>./{name}</path>\n'
    "\t\t<name>x</name>\n\t</game>\n</gameList>\n"
)


class TierOrderTest(NightCase):
    def setUp(self) -> None:
        super().setUp()
        # `nes` comes before `snes` by name, but its game only lacks art (tier 2) ...
        self.hub.add_game(KIRBY, "nes")
        self.hub.write_gamelist(NES_ENTRY.format(name=KIRBY), system="nes")
        self.games(CHRONO)  # ... and a new game of `snes` is tier 1
        self.hub.canned({"known": KNOWN3})

    def test_a_night_cut_short_has_done_tier_one_of_every_system_first(self) -> None:
        real = scrape_io._scrape_batch

        def slow(*args: Any, **kw: Any) -> Any:
            got = real(*args, **kw)
            self.clock.t += 55  # one batch that left too little of the minute for another
            return got

        with mock.patch.object(scrape_io, "_scrape_batch", slow):
            code = self.night(systems=("nes", "snes"), max_minutes=1)
        self.assertEqual(code, 0, self.out + self.err)
        (seen,) = self.hub.gathers()
        self.assertEqual(set(seen["targets"]), {CHRONO})
        (record,) = self.records()
        self.assertEqual((record["outcome"], record["stop"]), ("budget", "deadline"))
        self.assertEqual(record["systems"]["snes"]["added"], 1)
        self.assertNotIn(f"library/roms/nes/{KIRBY}", record["added"] + record["filled"])

    def test_the_tiers_still_cover_every_system_when_the_night_is_not_cut_short(self) -> None:
        self.assertEqual(self.night(systems=("nes", "snes")), 0, self.out + self.err)
        (record,) = self.records()
        self.assertEqual(record["outcome"], "done")
        self.assertEqual(record["systems"]["snes"]["added"], 1)
        self.assertEqual(record["systems"]["nes"]["filled"], 1)
        self.assertEqual(len(self.hub.gathers()), 2)

    def test_the_quota_stops_the_later_tiers_too(self) -> None:
        self.games(HOMEBREW)  # two new games of `snes`: the quota hits inside its tier one
        self.hub.canned({"known": KNOWN3, "quota_after": 1})
        self.assertEqual(self.night(systems=("nes", "snes")), 0, self.out + self.err)
        (record,) = self.records()
        self.assertEqual(record["stop"], "quota")
        self.assertEqual(len(self.hub.gathers()), 1)  # tier 2 of `nes` was never asked for


class RecordSkipsTest(NightCase):
    def test_the_record_counts_what_planning_skipped(self) -> None:
        self.games(KIRBY, CHRONO, HOMEBREW)
        self.hub.canned({"known": KNOWN3[1:2], "kinds": NO_VIDEOS})
        self.hub.run()  # Chrono: found, without videos (incomplete); the others: not found
        self.games(ZELDA)
        self.hub.canned({"known": KNOWN4, "kinds": NO_VIDEOS})
        self.next_day()
        self.assertEqual(self.night(), 0, self.out + self.err)
        (record,) = self.records()
        counts = record["systems"]["snes"]
        self.assertEqual(counts["added"], 1)
        self.assertEqual(counts["skipped_not_found"], 2)
        self.assertEqual(counts["skipped_incomplete"], 1)

    def test_a_system_whose_games_were_all_skipped_is_in_the_record(self) -> None:
        self.hub.add_game(KIRBY, "nes")
        self.hub.canned({"known": []})
        self.hub.run(systems=("nes",))  # Kirby is not found: remembered
        self.games(CHRONO)
        self.hub.canned({"known": KNOWN3})
        self.assertEqual(self.night(systems=("nes", "snes")), 0, self.out + self.err)
        (record,) = self.records()
        self.assertEqual(record["systems"]["nes"]["skipped_not_found"], 1)
        self.assertEqual(record["systems"]["nes"]["added"], 0)


class LockTest(NightCase):
    def setUp(self) -> None:
        super().setUp()
        self.games(KIRBY)
        self.hub.canned({"known": KNOWN3})

    def beating_sleep(self) -> None:
        """A holder that is alive: every wait finds the lock touched again."""
        counter = [0]

        def sleep(seconds: float) -> None:
            self.clock.sleep(seconds)
            counter[0] += 1
            stamp = 2_000_000_000_000_000_000 + counter[0] * 1_000_000_000
            os.utime(self.lock_path, ns=(stamp, stamp))

        self.sleep = sleep

    def test_a_live_lock_gives_busy_and_exit_0_then_1_on_the_second_night(self) -> None:
        self.write_lock()
        self.beating_sleep()
        with Receiver() as hook:
            self.assertEqual(self.night(notify_url=hook.url), 0, self.out + self.err)
            self.assertEqual(
                len(self.clock.slept), nightly.LOCK_WAIT_SECONDS // nightly.POLL_SECONDS
            )
            self.assertEqual(hook.bodies, [])
            doc = self.state()
            self.assertEqual(doc["last"]["outcome"], "busy")
            self.assertEqual((doc["trouble"]["kind"], doc["trouble"]["count"]), ("lock", 1))
            self.assertIsNone(doc["running"])
            self.assertEqual(self.hub.calls(), [])  # no Skyscraper
            self.assertTrue(self.lock_path.exists())  # the other run's lock is left alone
            self.assertTrue(any(x.startswith("busy: held by") for x in self.out), self.out)
            self.next_day()
            self.assertEqual(self.night(notify_url=hook.url), 1, self.out + self.err)
            (body,) = hook.bodies
        self.assertEqual(body["problem"], "lock")
        self.assertEqual(self.records()[-1]["outcome"], "busy")

    def test_a_lock_that_goes_away_while_it_waits_is_taken(self) -> None:
        self.write_lock()
        self.beating_sleep()
        real = self.sleep

        def gone(seconds: float) -> None:
            real(seconds)
            if len(self.clock.slept) == 3:
                self.lock_path.unlink()

        self.sleep = gone
        self.assertEqual(self.night(), 0, self.out + self.err)
        self.assertEqual(len(self.clock.slept), 3)
        self.assertEqual(self.state()["last"]["outcome"], "done")
        self.assertTrue(self.hub.gathers())

    def test_a_silent_lock_is_taken_over_after_ten_minutes_of_the_fake_clock(self) -> None:
        self.write_lock()
        self.assertEqual(self.night(), 0, self.out + self.err)
        self.assertEqual(self.clock.slept, [60.0] * 10)
        self.assertTrue(any("taking over a stale lock" in line for line in self.err), self.err)
        self.assertEqual(self.state()["last"]["outcome"], "done")
        self.assertFalse(self.lock_path.exists())
        (record,) = self.records()
        self.assertTrue(any("taking over" in w for w in record["warnings"]))

    def test_a_legacy_lock_is_a_configuration_error_and_is_not_taken(self) -> None:
        self.lock_path.write_text(
            json.dumps({"host": "other-host", "pid": 4242, "started": self.clock.t - 20 * 3600})
        )
        self.assertEqual(self.night(), 2, self.out + self.err)
        self.assertEqual(self.clock.slept, [])
        self.assertTrue(self.lock_path.exists())
        doc = self.state()
        self.assertEqual(doc["error"]["step"], nightly.STEP_LOCK)
        self.assertTrue(doc["error"]["config"])
        self.assertEqual(self.hub.calls(), [])
        self.next_day()
        self.assertEqual(self.night(), 0)  # a known problem
        self.assertEqual(self.state()["error"]["count"], 2)

    def test_a_lock_made_between_the_look_and_taking_it_is_waited_for(self) -> None:
        real = inbox_lock.look
        calls = []

        def look(inbox: Path, now: float) -> Any:
            calls.append(now)
            if len(calls) == 1:
                return None  # free when looked at ...
            return real(inbox, now)

        self.write_lock()  # ... but somebody holds it when it is taken
        self.beating_sleep()
        with mock.patch.object(inbox_lock, "look", look):
            self.assertEqual(self.night(), 0, self.out + self.err)
        self.assertEqual(self.state()["last"]["outcome"], "busy")

    def test_the_webhook_of_a_busy_night_says_what_is_left(self) -> None:
        self.games(CHRONO, HOMEBREW)
        self.write_lock()
        self.beating_sleep()
        with Receiver() as hook:
            self.night(notify_url=hook.url, max_games=1)
            self.next_day()
            self.assertEqual(self.night(notify_url=hook.url, max_games=1), 1)
            (body,) = hook.bodies
        self.assertEqual((body["problem"], body["left"]), ("lock", 2))

    def test_a_lock_that_cannot_be_read_is_a_runtime_error_not_a_configuration_one(self) -> None:
        with mock.patch.object(inbox_lock, "look", side_effect=InboxError("cannot read it")):
            self.assertEqual(self.night(), 1, self.out + self.err)
        error = self.state()["error"]
        self.assertEqual(error["step"], nightly.STEP_LOCK)
        self.assertFalse(error["config"])

    def test_a_busy_lock_with_no_reading_is_a_runtime_error_not_a_configuration_one(self) -> None:
        busy = inbox_lock.LockBusy("the lock is busy", None)
        with mock.patch.object(inbox_lock, "hold", side_effect=busy):
            self.assertEqual(self.night(), 1, self.out + self.err)
        error = self.state()["error"]
        self.assertEqual(error["step"], nightly.STEP_LOCK)
        self.assertFalse(error["config"])


class LostLockTest(NightCase):
    def test_a_lost_lock_stops_the_run_with_exit_1_and_leaves_the_other_lock(self) -> None:
        self.games(KIRBY, CHRONO, HOMEBREW, ZELDA)
        self.hub.canned({"known": KNOWN4})
        real = scrape_io._scrape_batch

        def steal(*args: Any, **kw: Any) -> Any:
            got = real(*args, **kw)
            self.lock_path.write_text(
                json.dumps(
                    {"host": "other-host", "pid": 1, "started": 1.0, "beat": 60, "mode": "attended"}
                )
            )
            return got

        with (
            mock.patch.object(scrape, "BATCH_SIZE", 2),
            mock.patch.object(scrape_io, "_scrape_batch", steal),
            Receiver() as hook,
        ):
            code = self.night(notify_url=hook.url)
            (body,) = hook.bodies
        self.assertEqual(code, 1, self.out + self.err)
        self.assertEqual(len(self.hub.gathers()), 1)  # the second batch never started
        self.assertEqual(json.loads(self.lock_path.read_text())["host"], "other-host")
        doc = self.state()
        self.assertEqual(doc["error"]["message"], nightly.LOST_MESSAGE)
        self.assertFalse(doc["error"]["config"])
        self.assertEqual(doc["last"]["outcome"], "stopped")
        self.assertEqual(doc["trouble"]["kind"], "lock")
        self.assertIsNone(doc["running"])
        (record,) = self.records()
        self.assertEqual((record["stop"], record["exit"]), ("lock lost", 1))
        self.assertEqual(body["problem"], "error")  # the lock streak is not news yet
        self.assertEqual(body["added"], 2)


class Killed(BaseException):
    """A run that dies: nothing in the night catches it, as with a kill or a power cut."""


class DiedTest(NightCase):
    def leave_running(self) -> None:
        """What a run that died leaves: `running` still set, next to whatever else was there."""
        doc = self.state() if self.state_file.exists() else json.loads(render_state(State()))
        doc["running"] = {"run": "20261112T030000Z", "started": "2026-11-12T03:00:00Z"}
        self.state_file.parent.mkdir(parents=True, exist_ok=True)
        self.state_file.write_text(json.dumps(doc))

    def test_a_leftover_running_is_reported_as_died_and_the_second_one_is_news(self) -> None:
        self.games(KIRBY, CHRONO, HOMEBREW)
        self.hub.canned({"known": KNOWN3})
        self.leave_running()
        self.assertEqual(self.night(max_games=1), 0, self.out + self.err)
        self.assertTrue(any("did not finish" in line for line in self.err), self.err)
        doc = self.state()
        self.assertEqual((doc["trouble"]["kind"], doc["trouble"]["count"]), ("died", 1))
        self.assertIsNone(doc["running"])
        self.assertEqual(doc["last"]["outcome"], "done")  # the night itself was clean
        self.next_day()
        self.leave_running()
        with Receiver() as hook:
            self.assertEqual(self.night(max_games=1, notify_url=hook.url), 1)
            (body,) = hook.bodies
        self.assertEqual(body["problem"], "died")
        self.assertEqual(self.state()["trouble"]["count"], 2)
        self.next_day()
        self.assertEqual(self.night(max_games=1), 0)  # a night after that is clean
        self.assertIsNone(self.state()["trouble"])

    def test_runs_that_all_die_are_reported_once_by_the_run_that_finds_the_streak(self) -> None:
        self.games(KIRBY)
        self.hub.canned({"known": KNOWN3})
        with Receiver() as hook:
            with mock.patch.object(scrape_io, "run_held", side_effect=Killed()):
                for _ in range(3):  # the third finds two earlier deaths and dies as well
                    with self.assertRaises(Killed):
                        self.night(notify_url=hook.url)
                    self.next_day()
            (body,) = hook.bodies
            self.assertEqual(body["problem"], "died")
            self.assertEqual(self.state()["trouble"]["count"], 2)
            self.assertTrue(self.state()["trouble"]["notified"])
            self.assertEqual(self.night(notify_url=hook.url), 0, self.out + self.err)
            self.assertEqual(len(hook.bodies), 1)  # once, not again

    def test_a_died_streak_that_is_news_is_sent_when_the_run_starts_not_when_it_ends(self) -> None:
        self.games(KIRBY, CHRONO, HOMEBREW)
        self.hub.canned({"known": KNOWN3})
        self.leave_running()
        self.night(max_games=1)
        self.next_day()
        self.leave_running()
        seen: list[int] = []
        real = scrape_io.run_held

        with Receiver() as hook:

            def look(opts: Any, rep: Any, **kw: Any) -> Any:
                seen.append(len(hook.bodies))
                return real(opts, rep, **kw)

            with mock.patch.object(scrape_io, "run_held", look):
                code = self.night(notify_url=hook.url, max_games=1)
            self.assertEqual(code, 1)  # the run that found the news still exits on it
            self.assertEqual(seen, [1])
            self.assertEqual(len(hook.bodies), 1)
        self.assertTrue(self.records()[-1]["notified"])


class ChecksTest(NightCase):
    def setUp(self) -> None:
        super().setUp()
        self.games(KIRBY)

    def test_a_configuration_error_over_three_nights_exits_2_0_0(self) -> None:
        with Receiver() as hook:
            codes = []
            for _ in range(3):
                codes.append(self.night(skyscraper=None, notify_url=hook.url))
                self.next_day()
            self.assertEqual(len(hook.bodies), 1)
        self.assertEqual(codes, [2, 0, 0])
        error = self.state()["error"]
        self.assertEqual((error["count"], error["config"]), (3, True))
        self.assertIn("Skyscraper not found", error["message"])
        self.assertEqual(self.hub.calls(), [])
        self.assertEqual(hook.bodies[0]["problem"], "error")
        self.assertNotIn("message", hook.bodies[0])
        self.assertNotIn(SECRET, self.everything_written())

    def test_a_configured_program_that_is_not_there_is_a_configuration_error(self) -> None:
        missing = str(self.hub.base / "nope" / "Skyscraper")
        self.assertEqual(self.night(skyscraper=missing), 2)
        message = self.state()["error"]["message"]
        self.assertNotIn(str(self.hub.base), message)  # no path of this machine in the state
        self.assertIn("not an executable file", message)

    def test_the_program_is_looked_for_on_the_path_when_not_configured(self) -> None:
        self.hub.canned({"known": KNOWN3})
        self.assertEqual(self.night(skyscraper=None, which=lambda name: str(self.hub.tool)), 0)
        self.assertTrue(self.hub.gathers())

    def test_a_skyscraper_folder_that_is_not_a_folder_is_a_configuration_error(self) -> None:
        self.assertEqual(self.night(skyscraper_dir=self.hub.base / "nowhere"), 2)
        self.assertIn("WARPZONE_SKYSCRAPER_DIR", self.state()["error"]["message"])

    def test_a_scratch_folder_on_memory_is_a_configuration_error(self) -> None:
        with mock.patch.object(nightly, "on_memory", return_value=True):
            self.assertEqual(self.night(), 2)
        self.assertIn("memory filesystem", self.state()["error"]["message"])
        self.assertEqual(self.hub.calls(), [])

    def test_a_hub_that_is_not_mounted_is_a_configuration_error(self) -> None:
        (self.hub.root / "library" / ".stfolder").rmdir()
        self.assertEqual(self.night(), 2)
        self.assertEqual(self.state()["error"]["step"], nightly.STEP_LAYOUT)
        self.assertIn("is the share mounted", self.state()["error"]["message"])

    def test_a_folder_with_no_catalog_is_not_the_hub_and_nothing_is_written(self) -> None:
        shutil.rmtree(self.hub.root / "catalog")
        with self.assertRaises(UsageError):  # `cli.main` turns it into exit 2, on every run
            self.night()
        self.assertFalse((self.hub.root / "catalog").exists())

    def test_a_folder_that_is_not_a_hub_at_all_raises_a_usage_error(self) -> None:
        elsewhere = self.hub.base / "elsewhere"
        elsewhere.mkdir()
        with self.assertRaises(UsageError):
            self.night(root=elsewhere)

    def test_the_setup_does_not_show_the_webhook_url(self) -> None:
        self.assertNotIn(SECRET, repr(self.setup_for(notify_url=f"http://h/{SECRET}")))


class CommandTest(NightCase):
    """Through `cli.main`: the flags and the usage errors that come before anything is read."""

    def config(self) -> str:
        path = self.hub.base / "config.toml"
        path.write_text(
            f'[inbox]\nroot = "{self.hub.root}"\nskyscraper = "{self.hub.tool}"\n'
            f'work_dir = "{self.hub.work}"\n'
        )
        return str(path)

    def scrape(self, *args: str) -> tuple[int, str, str]:
        env = {"XDG_CACHE_HOME": str(self.hub.cache), "HOME": str(self.hub.base / "home")}
        with mock.patch.dict(os.environ, env):
            return run_cli("scrape", "--config", self.config(), *args)

    def test_a_night_through_the_command_line(self) -> None:
        self.games(KIRBY)
        self.hub.canned({"known": KNOWN3})
        code, out, err = self.scrape("--unattended", "--max-games", "5", "--max-minutes", "30")
        self.assertEqual((code, err), (0, ""), out)
        self.assertIn("scrape: snes: 1 added", out)
        self.assertEqual(self.state()["last"]["outcome"], "done")

    def test_the_flags_that_belong_to_the_nightly_run_need_it(self) -> None:
        for flags in (
            ["--max-games", "5"],
            ["--max-minutes", "5"],
            ["--notify-url", f"http://h/{SECRET}"],
        ):
            with self.subTest(flags=flags), mock.patch("warpzone.cli.load_inbox_settings") as load:
                code, out, err = self.scrape(*flags)
                self.assertEqual(code, 2)
                self.assertIn("--unattended", err)
                self.assertNotIn(SECRET, out + err)
                load.assert_not_called()

    def test_the_nightly_run_refuses_the_attended_flags_before_reading_anything(self) -> None:
        for flag in ("--dry-run", "--force-lock", "--retry-not-found"):
            with self.subTest(flag=flag), mock.patch("warpzone.cli.load_inbox_settings") as load:
                code, _, err = self.scrape("--unattended", flag)
                self.assertEqual(code, 2)
                self.assertIn(flag, err)
                load.assert_not_called()
        self.assertFalse(self.state_file.exists())

    def test_the_budget_must_be_a_positive_number(self) -> None:
        for flags in (["--max-games", "0"], ["--max-minutes", "-1"], ["--max-games", "many"]):
            with self.subTest(flags=flags):
                code, _, _ = self.scrape("--unattended", *flags)
                self.assertEqual(code, 2)

    def test_a_missing_hub_root_exits_2_on_every_run(self) -> None:
        config = self.hub.base / "config.toml"
        config.write_text(f'[inbox]\nskyscraper = "{self.hub.tool}"\n')
        code, _, err = run_cli("scrape", "--config", str(config), "--unattended")
        self.assertEqual(code, 2)
        self.assertIn("no hub root", err)

    def test_the_webhook_url_of_the_command_line_is_used_and_never_printed(self) -> None:
        self.games(KIRBY)
        config = self.hub.base / "config.toml"
        config.write_text(f'[inbox]\nroot = "{self.hub.root}"\n')  # no Skyscraper anywhere
        with Receiver() as hook, mock.patch.dict(os.environ, {"PATH": str(self.hub.base / "none")}):
            code, out, err = run_cli(
                "scrape", "--config", str(config), "--unattended", "--notify-url", hook.url
            )
            self.assertEqual(len(hook.bodies), 1)
        self.assertEqual(code, 2)
        self.assertNotIn(SECRET, out + err)


if __name__ == "__main__":
    unittest.main()
