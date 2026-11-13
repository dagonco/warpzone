"""`warpzone inbox --apply --unattended` end to end: ticks of a scheduled run over a temporary
hub with stand-in tools, the clock injected (`Options.now`), and the disk side it uses."""

import json
import os
import tempfile
import time
import unittest
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from unittest import mock

from console_iso_support import ps4_sfo
from notify_support import Receiver
from test_cli import run
from test_inbox import DATA1, NES_ROM, Base, cue
from test_inbox_ps4 import PKG_NAME, TITLE_ID, UPDATE, PartsBase
from warpzone import inbox, inbox_lock, inbox_unattended_io, safefile
from warpzone.inbox_core import InboxError, ScrapeNote, Tools
from warpzone.inbox_unattended import (
    DIED_REASON,
    LOOSE_REASON,
    LockSeen,
    State,
    render_state,
    with_checked,
    with_error,
)
from warpzone.inbox_unattended_run import LEGACY_MESSAGE, LOST_MESSAGE, run_unattended
from warpzone.report import Reporter

T0 = float(int(time.time()))
STAMP = datetime.fromtimestamp(T0, UTC)


KEEP: Any = object()  # "leave the option as the test base sets it"


def minutes(n: float) -> float:
    return T0 + n * 60


class Ticks:
    """What a test of the scheduled run needs on top of `Base`: a tick at a given minute."""

    def setUp(self) -> None:
        super().setUp()  # type: ignore[misc]
        nice = mock.patch("os.nice")
        self.nice = nice.start()
        self.addCleanup(nice.stop)  # type: ignore[attr-defined]
        self.rep = Reporter(self.lines.append)  # type: ignore[attr-defined]

    def tools(self) -> Tools | None:
        return None

    def tick(self, at: float = 0, *, work_dir: Any = KEEP, **kw: Any) -> int:
        self.lines.clear()  # type: ignore[attr-defined]
        self.rep = Reporter(self.lines.append)  # type: ignore[attr-defined]
        opts = self.options(  # type: ignore[attr-defined]
            unattended=True, now=lambda: minutes(at), tools=self.tools(), **kw
        )
        if work_dir is not KEEP:
            opts = replace(opts, work_dir=work_dir)
        return run_unattended(opts, self.rep)

    @property
    def state_file(self) -> Path:
        return self.hub / "catalog" / "inbox" / "state.json"  # type: ignore[attr-defined]

    def state(self) -> dict[str, Any]:
        return json.loads(self.state_file.read_text())  # type: ignore[no-any-return]

    def records(self) -> list[dict[str, Any]]:
        folder = self.hub / "catalog" / "inbox" / "runs"  # type: ignore[attr-defined]
        return [json.loads(p.read_text()) for p in sorted(folder.glob("*.json"))]

    def lock_path(self) -> Path:
        return self.inbox / ".warpzone.lock"  # type: ignore[attr-defined,no-any-return]

    def write_lock(self, **fields: Any) -> None:
        info = {"host": "other-host", "pid": 4242, "started": minutes(-3 * 60), **fields}
        self.lock_path().write_text(json.dumps(info) + "\n")

    def done(self) -> list[str]:
        folder = self.inbox / "_done"  # type: ignore[attr-defined]
        return (
            sorted(p.name for d in folder.glob("*") for p in d.iterdir()) if folder.is_dir() else []
        )

    def review(self) -> list[str]:
        folder = self.inbox / "_revisar"  # type: ignore[attr-defined]
        return sorted(p.name for p in folder.iterdir()) if folder.is_dir() else []


class TickTests(Ticks, Base):
    def folder_game(self) -> None:
        self.drop(
            "Crash Test",
            {
                "Crash Test.cue": cue("Crash Test (Europe) (Track 1).bin"),
                "Crash Test (Europe) (Track 1).bin": DATA1,
            },
        )

    def test_a_game_is_placed_on_the_second_tick_that_sees_it_still(self) -> None:
        self.drop("adventure.nes", NES_ROM)
        self.folder_game()
        self.assertEqual(self.tick(0), 0, self.out)
        self.assertEqual(self.library(), [])
        self.assertEqual(sorted(self.state()["items"]), ["Crash Test", "adventure.nes"])
        self.assertIn("new: adventure.nes", self.out)
        self.assertEqual(self.tick(4), 0)  # not five minutes yet
        self.assertEqual(self.library(), [])
        self.assertEqual(self.tick(5), 0, self.out)
        self.assertEqual(self.library(), ["nes/Adventure (USA).zip", "psx/Crash Test (Europe).chd"])
        self.assertEqual(self.state()["items"], {})
        self.assertEqual(self.done(), ["Crash Test", "adventure.nes"])
        (record,) = self.records()
        self.assertEqual((record["mode"], record["exit"]), ("unattended", 0))
        self.assertEqual(record["counts"]["placed"], 2)
        self.assertEqual(self.tick(20), 0)  # nothing left: silent
        self.assertEqual(self.out, "")

    def test_the_cpu_priority_is_lowered_and_stale_work_folders_are_removed(self) -> None:
        old = self.tmp / "work" / "warpzone-inbox-killed"
        old.mkdir(parents=True)
        os.utime(old, (T0 - 2 * 86400, T0 - 2 * 86400))
        young = self.tmp / "work" / "warpzone-inbox-fresh"
        young.mkdir()
        os.utime(young, (T0 - 3600, T0 - 3600))
        self.drop("adventure.nes", NES_ROM)
        self.tick(0)
        self.assertTrue(old.exists())  # nothing ready: nothing touched
        self.nice.assert_not_called()
        self.tick(5)
        self.nice.assert_called_once_with(10)
        self.assertFalse(old.exists())
        self.assertTrue(young.exists())
        self.assertIn("deleted 1 leftover folder", self.out)

    def test_a_file_that_grows_waits_and_so_does_a_folder_that_gains_a_file(self) -> None:
        self.drop("adventure.nes", NES_ROM)
        self.folder_game()
        self.tick(0)
        with (self.inbox / "adventure.nes").open("ab") as f:
            f.write(b"more")
        (self.inbox / "Crash Test" / "extra.txt").write_text("a late file")
        self.tick(5)
        self.assertEqual(self.library(), [])
        (self.inbox / "adventure.nes").write_bytes(NES_ROM)  # the copy ends
        (self.inbox / "Crash Test" / "last.txt").write_text("and the last one")
        self.tick(6)
        self.assertEqual(self.tick(10), 0)  # four minutes since it was last seen changing
        self.assertEqual(self.library(), [])
        self.tick(11)
        self.assertEqual(self.done(), ["Crash Test", "adventure.nes"])

    def test_a_first_seen_time_survives_changes(self) -> None:
        self.drop("adventure.nes", NES_ROM)
        self.tick(0)
        first = self.state()["items"]["adventure.nes"]["first"]
        with (self.inbox / "adventure.nes").open("ab") as f:
            f.write(b"more")
        self.tick(7)
        entry = self.state()["items"]["adventure.nes"]
        self.assertEqual(entry["first"], first)
        self.assertNotEqual(entry["seen"], first)

    def test_an_item_that_changed_since_it_was_judged_waits_again(self) -> None:
        self.drop("adventure.nes", NES_ROM)
        self.tick(0)
        real = inbox_unattended_io.walk
        calls = []

        def walk(item: Path) -> Any:
            calls.append(item)
            if len(calls) == 2:  # the check right before the item is processed
                with item.open("ab") as f:
                    f.write(b"late")
            return real(item)

        with mock.patch("warpzone.inbox_unattended_run.walk", walk):
            self.assertEqual(self.tick(5), 0, self.out)
        self.assertEqual(self.library(), [])
        self.assertIn("changed since it was judged ready", self.out)
        self.assertEqual(self.state()["items"]["adventure.nes"]["attempts"], 0)

    def test_an_item_that_cannot_be_read_before_processing_waits_without_failing_the_run(
        self,
    ) -> None:
        self.drop("adventure.nes", NES_ROM)
        self.drop("other.nes", NES_ROM)
        self.tick(0)
        real = inbox_unattended_io.walk

        def walk(item: Path) -> Any:
            calls.append(item.name)
            if calls.count("adventure.nes") == 2 and item.name == "adventure.nes":  # pre-check
                raise PermissionError(13, "Permission denied")
            return real(item)

        calls: list[str] = []
        with mock.patch("warpzone.inbox_unattended_run.walk", walk):
            self.assertEqual(self.tick(5), 0, self.out)
        self.assertTrue(any("cannot read it" in w for w in self.rep.warnings), self.rep.warnings)
        self.assertIsNone(self.state()["error"])
        self.assertTrue((self.inbox / "adventure.nes").exists())
        self.assertEqual(self.done(), ["other.nes"])

    def test_an_item_that_changes_while_it_is_processed_is_not_filed_away(self) -> None:
        self.drop("adventure.nes", NES_ROM)
        self.tick(0)
        real = inbox.process_item

        def process(*args: Any, **kw: Any) -> Any:
            result = real(*args, **kw)
            path = self.inbox / "adventure.nes"
            path.write_bytes(NES_ROM)  # the user copies it again over the old one
            os.utime(path, (T0 - 77, T0 - 77))
            return result

        with mock.patch.object(inbox, "process_item", process):
            self.assertEqual(self.tick(5), 0, self.out)  # no news
        self.assertTrue((self.inbox / "adventure.nes").exists())  # not moved to _done
        self.assertEqual(self.done(), [])
        entry = self.state()["items"]["adventure.nes"]
        self.assertEqual(entry["attempts"], 0)  # it waits again
        self.assertTrue(any("changed while" in w for w in self.rep.warnings), self.rep.warnings)
        self.assertEqual(self.tick(6), 0)  # not five minutes since it changed
        self.assertEqual(self.done(), [])
        self.tick(11)
        self.assertEqual(self.done(), ["adventure.nes"])

    def test_a_run_that_dies_twice_on_an_item_holds_it_and_touching_it_clears_the_hold(
        self,
    ) -> None:
        self.drop("adventure.nes", NES_ROM)
        self.tick(0)
        with mock.patch.object(inbox, "process_item", side_effect=KeyboardInterrupt):
            for at in (5, 10):
                with self.assertRaises(KeyboardInterrupt):
                    self.tick(at)
                self.assertFalse(self.lock_path().exists())
        self.assertEqual(self.state()["items"]["adventure.nes"]["attempts"], 2)
        self.assertEqual(self.tick(15), 1, self.out)  # held: news
        self.assertIn(f"held: adventure.nes: {DIED_REASON}", self.out)
        self.assertEqual(self.state()["items"]["adventure.nes"]["held"]["reason"], DIED_REASON)
        self.assertEqual((self.tick(20), self.tick(25)), (0, 0))
        self.assertEqual(self.library(), [])
        path = self.inbox / "adventure.nes"
        os.utime(path, (T0 - 100, T0 - 100))  # touched: a new item
        self.assertEqual(self.tick(30), 0)
        self.assertIsNone(self.state()["items"]["adventure.nes"]["held"])
        self.assertEqual(self.tick(35), 0, self.out)
        self.assertEqual(self.library(), ["nes/Adventure (USA).zip"])

    def test_an_attempt_that_cannot_be_written_skips_the_item_this_tick(self) -> None:
        self.drop("adventure.nes", NES_ROM)
        self.tick(0)
        real = safefile.atomic_write

        def write(target: Path, data: bytes, *, expect: Any, **kw: Any) -> bool:
            items = json.loads(data).get("items", {}) if target.name == "state.json" else {}
            if any(e["attempts"] for e in items.values()):
                return False  # the state kept changing under the write of the attempt
            return real(target, data, expect=expect, **kw)

        with (
            mock.patch.object(safefile, "atomic_write", write),
            mock.patch.object(inbox, "process_item", side_effect=AssertionError("processed")),
        ):
            self.assertEqual(self.tick(5), 0, self.out)
        self.assertTrue(any("kept changing" in w for w in self.rep.warnings), self.rep.warnings)
        self.assertEqual(self.state()["items"]["adventure.nes"]["attempts"], 0)
        self.assertEqual(self.library(), [])
        self.assertEqual(self.tick(6), 0, self.out)  # the next tick takes it
        self.assertEqual(self.library(), ["nes/Adventure (USA).zip"])

    def test_an_item_sent_to_review_whose_reason_file_fails_is_reviewed_not_held(self) -> None:
        self.drop("mystery.nes", b"?")
        self.tick(0)
        failing = mock.patch.object(inbox, "write_reason", side_effect=OSError(28, "No space"))
        with failing:
            self.assertEqual(self.tick(5), 1, self.out)
        self.assertFalse((self.inbox / "mystery.nes").exists())
        self.assertEqual(self.review(), ["mystery.nes"])
        self.assertEqual(self.state()["items"], {})  # not held: it is gone from the inbox
        self.assertTrue(any("reason file" in w for w in self.rep.warnings), self.rep.warnings)
        (record,) = self.records()
        self.assertEqual(record["items"][0]["outcome"], "review")
        self.assertEqual(self.tick(10), 0)

    def test_an_item_for_review_is_news_once_each(self) -> None:
        self.drop("mystery.nes", b"?")
        self.tick(0)
        self.assertEqual(self.tick(5), 1, self.out)
        self.assertEqual(self.review(), ["mystery.nes", "mystery.nes.reason.txt"])
        self.drop("another.nes", b"??")
        self.tick(10)
        self.assertEqual(self.tick(15), 1)
        self.assertEqual(self.tick(20), 0)

    def test_a_held_item_gives_exit_1_then_0_then_0_and_one_webhook(self) -> None:
        self.drop("adventure.nes", NES_ROM)
        with Receiver() as hook:
            self.tick(0, notify_url=hook.url)
            broken = mock.patch.object(inbox, "move_item", side_effect=OSError(13, "Denied"))
            with broken:
                self.assertEqual(self.tick(5, notify_url=hook.url), 1, self.out)
            self.assertEqual(self.library(), ["nes/Adventure (USA).zip"])
            self.assertTrue((self.inbox / "adventure.nes").exists())
            reason = "could not move the original: Denied"
            self.assertEqual(self.state()["items"]["adventure.nes"]["held"]["reason"], reason)
            self.assertEqual(self.records()[0]["items"][0]["outcome"], "held")
            self.assertEqual((self.tick(10, notify_url=hook.url), self.tick(15)), (0, 0))
            self.assertEqual(len(hook.bodies), 1)
            self.assertEqual(hook.bodies[0]["mode"], "unattended")
            self.assertEqual(hook.bodies[0]["held"], 1)
            self.assertEqual(hook.bodies[0]["review_items"], ["adventure.nes"])
            # touched, it is new again; placed again as "already", held again: news again
            os.utime(self.inbox / "adventure.nes", (T0 - 50, T0 - 50))
            self.assertEqual(self.tick(20), 0)
            with broken:
                self.assertEqual(self.tick(25), 1, self.out)

    def test_loose_disc_files_are_held_and_named_in_the_webhook(self) -> None:
        self.drop("Game.cue", cue("Game.bin"))
        self.drop("Game.bin", DATA1)
        self.drop("adventure.nes", NES_ROM)
        with Receiver() as hook:
            self.tick(0, notify_url=hook.url)
            self.assertEqual(self.tick(5, notify_url=hook.url), 1, self.out)
            self.assertEqual(self.tick(10, notify_url=hook.url), 0)
        state = self.state()["items"]
        self.assertEqual(state["Game.cue"]["held"]["reason"], LOOSE_REASON)
        self.assertEqual(state["Game.bin"]["held"]["reason"], LOOSE_REASON)
        self.assertEqual(self.library(), ["nes/Adventure (USA).zip"])
        self.assertTrue((self.inbox / "Game.cue").exists())
        self.assertEqual(len(hook.bodies), 1)  # placed a game and held two: sent once
        self.assertEqual(hook.bodies[0]["held"], 2)
        self.assertEqual(hook.bodies[0]["review_items"], ["Game.bin", "Game.cue"])

    def test_the_webhook_url_and_paths_never_reach_the_state_or_the_record(self) -> None:
        self.drop("adventure.nes", NES_ROM)
        with Receiver() as hook:
            self.tick(0, notify_url=hook.url)
            self.tick(5, notify_url=hook.url)
        for text in (self.state_file.read_text(), json.dumps(self.records())):
            self.assertNotIn("secret-id", text)
            self.assertNotIn(str(self.tmp), text)

    def test_a_scrape_that_does_not_finish_changes_nothing_else(self) -> None:
        self.drop("adventure.nes", NES_ROM)
        self.tick(0)
        note = ScrapeNote("not-finished", ("nes",), "the daily limit")
        with mock.patch("warpzone.inbox_scrape.after_apply", return_value=note):
            self.assertEqual(self.tick(5), 0, self.out)
        (record,) = self.records()
        self.assertEqual(record["scrape"]["state"], "not-finished")
        self.assertIsNone(self.state()["error"])

    def test_no_scrape_is_accepted(self) -> None:
        self.drop("adventure.nes", NES_ROM)
        self.tick(0, no_scrape=True)
        self.assertEqual(self.tick(5, no_scrape=True), 0)
        self.assertEqual(self.records()[0]["scrape"]["state"], "skipped")


class EmptyAndLayoutTests(Ticks, Base):
    def test_an_empty_inbox_reads_no_dats_takes_no_lock_and_writes_only_a_due_heartbeat(
        self,
    ) -> None:
        opts = replace(
            self.options(unattended=True, now=lambda: minutes(0)), dats_dir=self.tmp / "no-dats"
        )
        with (
            mock.patch.object(inbox, "open_library", side_effect=AssertionError("DATs")),
            mock.patch.object(inbox_lock, "hold", side_effect=AssertionError("lock")),
        ):
            self.assertEqual(run_unattended(opts, self.rep), 0)
            self.assertEqual(self.lines, [])
            self.assertEqual(self.state()["checked"], STAMP.strftime("%Y-%m-%dT%H:%M:%SZ"))
            before = self.state_file.stat().st_mtime_ns
            later = replace(opts, now=lambda: minutes(30))
            self.assertEqual(run_unattended(later, self.rep), 0)
            self.assertEqual(self.state_file.stat().st_mtime_ns, before)  # not due again
            hour = replace(opts, now=lambda: minutes(61))
            self.assertEqual(run_unattended(hour, self.rep), 0)
        self.assertNotEqual(self.state_file.stat().st_mtime_ns, before)
        self.assertFalse(self.lock_path().exists())

    def test_the_entries_of_items_that_are_gone_are_dropped(self) -> None:
        self.drop("adventure.nes", NES_ROM)
        self.tick(0)
        (self.inbox / "adventure.nes").unlink()
        self.assertEqual(self.tick(1), 0)
        self.assertEqual(self.state()["items"], {})

    def test_the_cli_empty_run_prints_nothing_and_exits_0(self) -> None:
        config = self.tmp / "config.toml"
        config.write_text(
            f'[inbox]\nroot = "{self.hub}"\ndats_dir = "{self.tmp / "no-dats"}"\n'
            f'work_dir = "{self.tmp / "work"}"\n'
        )
        with mock.patch.dict(os.environ, {"XDG_CACHE_HOME": str(self.tmp / "xdg")}):
            code, out, err = run("inbox", "--config", str(config), "--apply", "--unattended")
        self.assertEqual((code, out, err), (0, "", ""))
        self.assertFalse(self.lock_path().exists())

    def test_the_cli_refuses_unattended_without_apply_and_with_force_lock(self) -> None:
        config = self.tmp / "config.toml"
        config.write_text(f'[inbox]\nroot = "{self.hub}"\ndats_dir = "{self.dats}"\n')
        for args in (("--unattended",), ("--apply", "--unattended", "--force-lock")):
            code, out, err = run("inbox", "--config", str(config), *args)
            self.assertEqual(code, 2, args)
            self.assertIn("--unattended needs --apply", err)
            self.assertEqual(out, "")
        self.assertFalse(self.state_file.exists())

    def test_the_cli_exit_code_is_the_runs(self) -> None:
        self.drop("mystery.nes", b"?")
        config = self.tmp / "config.toml"
        config.write_text(
            f'[inbox]\nroot = "{self.hub}"\ndats_dir = "{self.dats}"\n'
            f'work_dir = "{self.tmp / "work"}"\nchdman = "{self.chdman}"\n'
        )
        args = ("inbox", "--config", str(config), "--apply", "--unattended")
        with mock.patch("os.nice"), mock.patch.dict(os.environ, {"XDG_CACHE_HOME": str(self.tmp)}):
            first = run(*args)
            self.assertEqual(first[0], 0, first)
            doc = self.state()  # the item has held still for an hour, by the state's account
            for entry in doc["items"].values():
                entry["first"] = entry["seen"] = "2020-01-01T00:00:00Z"
            self.state_file.write_text(json.dumps(doc))
            second = run(*args)
        self.assertEqual(second[0], 1, second)
        self.assertEqual(self.review(), ["mystery.nes", "mystery.nes.reason.txt"])

    def test_a_missing_stfolder_is_an_error_kept_in_the_state_and_cleared_by_an_empty_run(
        self,
    ) -> None:
        (self.hub / "catalog").mkdir()
        (self.hub / "library" / ".stfolder").rmdir()
        self.drop("adventure.nes", NES_ROM)
        self.assertEqual(self.tick(0), 2, self.out)
        self.assertEqual(self.state()["error"]["step"], 1)
        self.assertEqual((self.tick(1), self.tick(2)), (0, 0))
        self.assertEqual(self.state()["error"]["count"], 3)
        (self.hub / "library" / ".stfolder").mkdir()
        (self.inbox / "adventure.nes").unlink()
        self.assertEqual(self.tick(3), 0)  # an empty run gets past step 1
        self.assertIsNone(self.state()["error"])

    def test_an_unmounted_root_writes_nothing_and_fails_every_time(self) -> None:
        (self.hub / "library" / ".stfolder").rmdir()
        with self.assertRaises(InboxError):
            self.tick(0)
        self.assertFalse((self.hub / "catalog").exists())

    def test_an_attended_run_needs_the_stfolder_too(self) -> None:
        (self.hub / "library" / ".stfolder").rmdir()
        with self.assertRaises(InboxError) as caught:
            self.go()
        self.assertIn("library/.stfolder/", str(caught.exception))

    def test_a_work_folder_that_is_missing_or_on_memory_is_a_configuration_error(self) -> None:
        self.drop("adventure.nes", NES_ROM)
        self.tick(0)
        self.assertEqual(self.tick(5, work_dir=None), 2)
        self.assertEqual(self.state()["error"]["step"], 5)
        self.assertIn("work_dir", self.state()["error"]["message"])
        with mock.patch("warpzone.inbox_unattended_run.on_memory", return_value=True):
            self.assertEqual(self.tick(6), 2)  # another message: news again
            self.assertEqual(self.tick(7), 0)
        self.assertEqual(self.tick(8), 0, self.out)  # fine again: placed, error cleared
        self.assertIsNone(self.state()["error"])
        self.assertEqual(self.library(), ["nes/Adventure (USA).zip"])

    def test_dats_that_are_not_configured_are_an_error_of_the_work_kept_once(self) -> None:
        self.drop("adventure.nes", NES_ROM)
        config = self.tmp / "config.toml"
        config.write_text(f'[inbox]\nroot = "{self.hub}"\nwork_dir = "{self.tmp / "work"}"\n')
        args = ("inbox", "--config", str(config), "--apply", "--unattended")
        with mock.patch("os.nice"), mock.patch.dict(os.environ, {"XDG_CACHE_HOME": str(self.tmp)}):
            self.assertEqual(run(*args)[0], 0)  # waiting: the DATs are not needed yet
            doc = self.state()
            for entry in doc["items"].values():
                entry["first"] = entry["seen"] = "2020-01-01T00:00:00Z"
            self.state_file.write_text(json.dumps(doc))
            code, out, err = run(*args)
            self.assertEqual(code, 2, (out, err))
            self.assertEqual(self.state()["error"]["step"], 5)
            self.assertIn("dats_dir", self.state()["error"]["message"])
            self.assertEqual(run(*args)[0], 0)  # the same problem again: not news
            self.assertEqual(self.state()["error"]["count"], 2)
            (self.inbox / "adventure.nes").unlink()
            self.assertEqual(run(*args)[0], 0)  # an empty inbox is not affected

    def test_the_cli_empty_run_without_dats_exits_0(self) -> None:
        config = self.tmp / "config.toml"
        config.write_text(f'[inbox]\nroot = "{self.hub}"\nwork_dir = "{self.tmp / "work"}"\n')
        code, out, err = run("inbox", "--config", str(config), "--apply", "--unattended")
        self.assertEqual((code, out, err), (0, "", ""))

    def test_an_error_of_the_work_is_not_cleared_by_a_run_with_only_waiting_items(self) -> None:
        self.drop("adventure.nes", NES_ROM)
        self.tick(0)
        with mock.patch("warpzone.inbox_unattended_run.on_memory", return_value=True):
            self.assertEqual(self.tick(5), 2)
        (self.inbox / "adventure.nes").unlink()
        self.drop("other.nes", NES_ROM)  # new, so only waiting: the run stops at step 4
        self.assertEqual(self.tick(6), 0, self.out)
        self.assertEqual(self.state()["error"]["step"], 5)
        self.assertEqual(self.state()["error"]["count"], 1)
        self.assertEqual(self.tick(12), 0, self.out)  # ready now, and the work is fine
        self.assertIsNone(self.state()["error"])

    def test_an_item_that_does_not_fit_waits_for_space(self) -> None:
        self.drop("adventure.nes", NES_ROM)
        self.tick(0)
        with mock.patch("warpzone.inbox_unattended_run.free_bytes", return_value=0):
            self.assertEqual(self.tick(5), 0, self.out)
        self.assertRegex(self.out, r"adventure\.nes: waiting for space: 21 GiB more needed")
        self.assertEqual(self.library(), [])
        self.assertEqual(self.tick(10), 0)
        self.assertEqual(self.library(), ["nes/Adventure (USA).zip"])


class LockTests(Ticks, Base):
    def test_a_lock_without_a_heartbeat_is_an_error_once_exit_2_then_0_and_0(self) -> None:
        self.drop("adventure.nes", NES_ROM)
        self.write_lock(started=minutes(-13 * 60))  # an older warpzone's, 13 hours old
        with Receiver() as hook:
            codes = [self.tick(at, notify_url=hook.url) for at in (0, 1, 2)]
        self.assertEqual(codes, [2, 0, 0], self.out)
        self.assertEqual(len(hook.bodies), 1)
        self.assertIn("older warpzone", hook.bodies[0]["error"])
        self.assertEqual(self.state()["error"]["count"], 3)
        self.assertEqual(self.state()["error"]["message"], LEGACY_MESSAGE)
        self.assertTrue(self.lock_path().exists())
        self.assertEqual(self.library(), [])

    def test_a_live_lock_is_busy_and_exit_0(self) -> None:
        self.drop("adventure.nes", NES_ROM)
        self.write_lock(started=minutes(-1), beat=60, mode="unattended")
        self.assertEqual(self.tick(0), 0)
        self.assertIn("busy: held by the unattended inbox on the hub", self.out)
        self.assertEqual(self.tick(1), 0)
        self.assertEqual(self.state()["lock_seen"]["at"], STAMP.strftime("%Y-%m-%dT%H:%M:%SZ"))
        self.assertEqual(self.state()["items"], {})  # busy before the items are judged
        self.assertTrue(self.lock_path().exists())

    def test_a_lock_that_moves_stays_live_however_long(self) -> None:
        self.drop("adventure.nes", NES_ROM)
        self.write_lock(beat=60, mode="attended")
        self.tick(0)
        os.utime(self.lock_path(), (T0 + 1, T0 + 1))  # the holder's heartbeat
        self.assertEqual(self.tick(15), 0)
        self.assertEqual(self.state()["items"], {})
        self.assertIn("busy", self.out)

    def test_a_silent_lock_with_a_heartbeat_is_taken_over_after_ten_minutes(self) -> None:
        self.drop("adventure.nes", NES_ROM)
        self.write_lock(beat=60, mode="attended")
        self.assertEqual(self.tick(0), 0)
        self.assertEqual(self.tick(5), 0)
        self.assertIn("busy", self.out)
        self.assertEqual(self.tick(11), 0)  # stale: judged the first time
        self.assertNotIn("busy", self.out)
        self.assertIn("adventure.nes", self.state()["items"])
        self.assertIsNotNone(self.state()["lock_seen"])  # kept: the stale lock is still there
        self.assertEqual(self.tick(16), 0, self.out)
        self.assertEqual(self.library(), ["nes/Adventure (USA).zip"])
        self.assertFalse(self.lock_path().exists())
        self.assertIsNone(self.state()["lock_seen"])
        self.assertTrue(any("taking over a stale lock" in w for w in self.rep.warnings))
        self.assertTrue(any("taking over" in w for w in self.records()[0]["warnings"]))

    def test_a_lock_made_between_the_look_and_taking_it_is_busy_exit_0_and_no_webhook(
        self,
    ) -> None:
        self.drop("adventure.nes", NES_ROM)
        self.tick(0)
        self.write_lock(started=minutes(5), beat=60, mode="unattended")
        with Receiver() as hook, mock.patch.object(inbox_lock, "look", return_value=None):
            self.assertEqual(self.tick(5, notify_url=hook.url), 0, self.out)
        self.assertEqual(hook.bodies, [])
        self.assertIn("busy: held by the unattended inbox", self.out)
        self.assertEqual(self.library(), [])
        self.assertTrue(self.lock_path().exists())
        self.assertIsNotNone(self.state()["lock_seen"])
        self.assertEqual(self.state()["items"]["adventure.nes"]["attempts"], 0)

    def test_the_lock_is_released_when_the_run_ends(self) -> None:
        self.drop("adventure.nes", NES_ROM)
        self.tick(0)
        self.tick(5)
        self.assertFalse(self.lock_path().exists())

    def test_a_lock_taken_away_stops_the_run_and_leaves_the_item(self) -> None:
        self.drop("adventure.nes", NES_ROM)
        self.drop("other.nes", NES_ROM)
        self.tick(0)
        real = inbox.process_item

        def process(*args: Any, **kw: Any) -> Any:
            result = real(*args, **kw)
            self.lock_path().write_text(  # somebody else took the lock meanwhile
                json.dumps({"host": "z", "pid": 1, "started": 1.0, "beat": 60}) + "\n"
            )
            return result

        with mock.patch.object(inbox, "process_item", process):
            self.assertEqual(self.tick(5), 1, self.out)
        self.assertIn(LOST_MESSAGE, self.out + "".join(self.rep.warnings))
        self.assertTrue((self.inbox / "adventure.nes").exists())  # not moved
        self.assertTrue((self.inbox / "other.nes").exists())  # never started
        self.assertEqual(self.state()["error"]["message"], LOST_MESSAGE)
        self.assertTrue(self.lock_path().exists())  # the other's lock is left alone
        self.lock_path().unlink()
        self.assertEqual(self.tick(10), 0, self.out)  # the rerun finds it already in the library
        self.assertEqual(self.done(), ["adventure.nes", "other.nes"])
        self.assertIsNone(self.state()["error"])


class Ps4Tests(Ticks, PartsBase):
    def setUp(self) -> None:
        super().setUp()
        self.sfo(ps4_sfo(TITLE_ID, "Test Game", "gp", app_ver="01.09"))
        self.base_game()
        self.dest = self.games / UPDATE

    def tools(self) -> Tools:
        return Tools(chdman=self.chdman, orbis_pkg_util=self.util, rahasher=None)

    def test_a_newer_update_replaces_the_one_in_the_library_unattended_too(self) -> None:
        from console_iso_support import ps4_pkg

        (self.dest / "sce_sys").mkdir(parents=True)
        (self.dest / "eboot.bin").write_bytes(b"OLD")
        (self.dest / "sce_sys" / "param.sfo").write_bytes(
            ps4_sfo(TITLE_ID, "Test Game", "gp", app_ver="01.05")
        )
        os.environ["STUB_PKG_MARK"] = "PATCH"
        self.drop(
            PKG_NAME,
            ps4_pkg(f"EP9000-{TITLE_ID}_00-TESTGAME0000EU", flags=0x62100000, pfs_size=170852352),
        )
        self.assertEqual(self.tick(0), 0, self.out)
        self.assertEqual((self.dest / "eboot.bin").read_bytes(), b"OLD")
        self.assertEqual(self.tick(5), 0, self.out)
        self.assertEqual((self.dest / "eboot.bin").read_bytes(), b"PATCH" * 100)
        self.assertIn("replaced the update v01.05 with v01.09", self.out)
        self.assertEqual(self.done(), [PKG_NAME])
        (record,) = self.records()
        self.assertIn("replaced the update v01.05 with v01.09", json.dumps(record))
        self.assertEqual(self.leftovers(), [])


class StateFileTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)

    def test_a_missing_file_is_empty_and_a_damaged_one_is_replaced_with_a_warning(self) -> None:
        state, warning = inbox_unattended_io.read_state(self.root)
        self.assertEqual((state, warning), (State(), None))
        path = inbox_unattended_io.state_path(self.root)
        path.parent.mkdir(parents=True)
        path.write_text("{not json")
        state, warnings = inbox_unattended_io.update_state(self.root, with_checked(STAMP))
        self.assertEqual(state.checked, STAMP)
        self.assertIn("cannot be parsed", warnings[0])
        self.assertEqual(inbox_unattended_io.read_state(self.root)[0].checked, STAMP)

    def test_a_file_that_changed_between_the_read_and_the_write_gets_the_same_changes(self) -> None:
        inbox_unattended_io.update_state(self.root, with_checked(STAMP))
        path = inbox_unattended_io.state_path(self.root)
        real = safefile.atomic_write
        calls: list[int] = []

        def racing(target: Path, data: bytes, *, expect: Any) -> bool:
            if not calls:  # somebody else writes first
                calls.append(1)
                other = State(checked=STAMP, lock_seen=LockSeen(5, STAMP))
                path.write_text(render_state(other))
                return False
            return real(target, data, expect=expect)

        with mock.patch.object(safefile, "atomic_write", racing):
            state, warnings = inbox_unattended_io.update_state(
                self.root, with_error("boom", 3, STAMP)
            )
        self.assertEqual(warnings, [])
        self.assertEqual(state.lock_seen, LockSeen(5, STAMP))  # what the other wrote is kept
        self.assertEqual(state.error.message, "boom")  # type: ignore[union-attr]
        self.assertEqual(inbox_unattended_io.read_state(self.root)[0], state)

    def test_a_file_that_keeps_changing_is_a_warning_after_three_tries(self) -> None:
        with mock.patch.object(safefile, "atomic_write", return_value=False) as write:
            _, warnings = inbox_unattended_io.update_state(self.root, with_checked(STAMP))
        self.assertEqual(write.call_count, 3)
        self.assertIn("kept changing", warnings[0])

    def test_a_collision_on_the_temporary_file_is_retried(self) -> None:
        real = safefile.atomic_write
        errors = [FileExistsError(17, "File exists"), FileNotFoundError(2, "No such file")]

        def racing(target: Path, data: bytes, *, expect: Any) -> bool:
            if errors:  # concurrent writers share the fixed temporary name
                raise errors.pop(0)
            return real(target, data, expect=expect)

        with mock.patch.object(safefile, "atomic_write", racing):
            state, warnings = inbox_unattended_io.update_state(self.root, with_checked(STAMP))
        self.assertEqual(warnings, [])
        self.assertEqual(inbox_unattended_io.read_state(self.root)[0], state)

    def test_a_collision_that_does_not_end_is_an_inbox_error_after_three_tries(self) -> None:
        broken = mock.patch.object(
            safefile, "atomic_write", side_effect=FileExistsError(17, "File exists")
        )
        with broken as write, self.assertRaises(InboxError):
            inbox_unattended_io.update_state(self.root, with_checked(STAMP))
        self.assertEqual(write.call_count, 3)

    def test_a_state_that_cannot_be_written_is_an_inbox_error(self) -> None:
        broken = mock.patch.object(safefile, "atomic_write", side_effect=OSError(30, "Read-only"))
        with broken, self.assertRaises(InboxError):
            inbox_unattended_io.update_state(self.root, with_checked(STAMP))


class WalkTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)

    def test_a_link_is_a_line_of_its_own_and_is_never_followed(self) -> None:
        outside = self.tmp / "outside"
        (outside / "deep").mkdir(parents=True)
        (outside / "deep" / "big.bin").write_bytes(b"x" * 100)
        item = self.tmp / "item"
        item.mkdir()
        (item / ".hidden").write_bytes(b"h")
        (item / "link").symlink_to(outside)
        (item / "sub").mkdir()
        (item / "sub" / "a.bin").write_bytes(b"abc")
        entries = inbox_unattended_io.walk(item)
        assert entries is not None
        shown = {(e.kind, e.rel) for e in entries}
        self.assertEqual(
            shown, {("d", "."), ("f", ".hidden"), ("l", "link"), ("d", "sub"), ("f", "sub/a.bin")}
        )

    def test_a_file_a_missing_item_and_a_link_item(self) -> None:
        (self.tmp / "a.zip").write_bytes(b"12345")
        entries = inbox_unattended_io.walk(self.tmp / "a.zip")
        assert entries is not None
        self.assertEqual([(e.kind, e.size, e.rel) for e in entries], [("f", 5, ".")])
        self.assertIsNone(inbox_unattended_io.walk(self.tmp / "gone"))
        (self.tmp / "l").symlink_to(self.tmp / "a.zip")
        entries = inbox_unattended_io.walk(self.tmp / "l")
        assert entries is not None
        self.assertEqual(entries[0].kind, "l")


class MachineTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)

    def test_stale_work_folders_go_and_links_and_young_ones_stay(self) -> None:
        old = self.tmp / "warpzone-inbox-old"
        old.mkdir()
        (old / "licence").write_text("x")
        os.utime(old, (1000, 1000))
        young = self.tmp / "warpzone-inbox-young"
        young.mkdir()
        target = self.tmp / "target"
        target.mkdir()
        os.utime(target, (1000, 1000))
        link = self.tmp / "warpzone-inbox-link"
        link.symlink_to(target)
        other = self.tmp / "other"
        other.mkdir()
        os.utime(other, (1000, 1000))
        gone = inbox_unattended_io.clean_work_dir(self.tmp, time.time())
        self.assertEqual(gone, ["warpzone-inbox-old"])
        self.assertFalse(old.exists())
        self.assertTrue(young.exists() and link.is_symlink() and target.exists())
        self.assertTrue(other.exists())
        self.assertEqual(inbox_unattended_io.clean_work_dir(self.tmp / "none", 0), [])

    def test_a_work_folder_on_a_memory_filesystem_is_seen_in_mountinfo(self) -> None:
        info = self.tmp / "mountinfo"
        info.write_text(
            "20 1 8:1 / / rw - ext4 /dev/sda1 rw\n"
            "21 20 0:20 / /ramdisk rw - tmpfs tmpfs rw\n"
            "22 20 8:2 / /data rw - ext4 /dev/sdb rw\n"
        )
        self.assertTrue(inbox_unattended_io.on_memory(Path("/ramdisk/work"), str(info)))
        self.assertFalse(inbox_unattended_io.on_memory(Path("/data/work"), str(info)))
        self.assertFalse(inbox_unattended_io.on_memory(Path("/ramdisk"), str(self.tmp / "none")))

    def test_the_priority_call_is_made_and_a_refusal_is_reported(self) -> None:
        calls: list[int] = []
        self.assertIsNone(inbox_unattended_io.lower_priority(lambda n: calls.append(n) or 0))
        self.assertEqual(calls, [10])

        def refuse(n: int) -> int:
            raise OSError(1, "Operation not permitted")

        self.assertEqual(inbox_unattended_io.lower_priority(refuse), "Operation not permitted")

    def test_free_bytes_reads_the_filesystem(self) -> None:
        self.assertGreater(inbox_unattended_io.free_bytes(self.tmp), 0)


if __name__ == "__main__":
    unittest.main()
