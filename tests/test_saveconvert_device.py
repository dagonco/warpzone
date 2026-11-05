"""The save conversion on a device: files, state, backups, the running emulator, the command."""

import contextlib
import io
import json
import os
import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path
from unittest import mock

from support import FakeProc
from test_saveconvert import footer, save
from warpzone import cli, doctor, saveconvert_device, settings_state
from warpzone.profiles import ProfileError, parse_profile

NOW = datetime(2026, 11, 5, 12, 0, 0, tzinfo=UTC)


def make_profile(raw: Path, emu: Path, **rule):
    return parse_profile(
        {
            "name": "x",
            "saves": {"melonds-saves": {"path": str(raw), "ignore": ["*.wz-tmp"]}},
            "save_convert": {
                "drastic": {
                    "format": "desmume-dsv",
                    "saves": "melonds-saves",
                    "path": str(emu),
                    "process": ["drastic"],
                    **rule,
                }
            },
        },
        "t",
    )


class Fixture(unittest.TestCase):
    def setUp(self) -> None:
        env = mock.patch.dict(os.environ)  # the state goes below the temporary home, always
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop("XDG_STATE_HOME", None)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        self.home = self.tmp / "home"
        self.raw = self.tmp / "card" / "melonds"
        self.emu = self.tmp / "card" / "backup"
        self.raw.mkdir(parents=True)
        self.emu.mkdir(parents=True)
        self.proc = FakeProc(self.tmp / "proc")
        self.profile = make_profile(self.raw, self.emu)
        self.state = saveconvert_device.state_root(self.home)
        self.lines: list[str] = []
        self.warnings: list[str] = []

    def put(self, path: Path, data: bytes, mtime: int | None = None) -> Path:
        path.write_bytes(data)
        if mtime is not None:
            os.utime(path, (mtime, mtime))
        return path

    def convert(self, **kw) -> saveconvert_device.Outcome:
        kw.setdefault("proc", self.proc.path)
        return saveconvert_device.convert(
            "ark",
            self.profile,
            self.home,
            say=self.lines.append,
            warn=self.warnings.append,
            now=lambda: NOW,
            **kw,
        )

    def backups(self) -> list[Path]:
        folder = self.state / "backups"
        return sorted(p for p in folder.rglob("*") if p.is_file()) if folder.is_dir() else []

    def saved_state(self) -> dict:
        return saveconvert_device.parse_state((self.state / "state.json").read_text())

    def tree(self, *, state: bool = False) -> dict[str, bytes]:
        """Every file of the saves folders (and, with `state`, of the device state too)."""
        return {
            str(p.relative_to(self.tmp)): p.read_bytes()
            for p in sorted(self.tmp.rglob("*"))
            if p.is_file()
            and p.relative_to(self.tmp).parts[0] in ("card", "home")[: 2 if state else 1]
        }


class DirectionTests(Fixture):
    def test_a_hub_save_becomes_a_dsv_without_a_footer(self) -> None:
        self.put(self.raw / "Game.sav", save(8192, 7))
        outcome = self.convert()
        self.assertEqual((self.emu / "Game.dsv").read_bytes(), save(8192, 7))
        self.assertEqual(outcome.written, 1)
        self.assertEqual(self.warnings, [])

    def test_a_dsv_becomes_a_raw_save_without_its_footer(self) -> None:
        self.put(self.emu / "Game.dsv", save(65536, 9) + footer(65536))
        self.convert()
        self.assertEqual((self.raw / "Game.sav").read_bytes(), save(65536, 9))

    def test_the_footer_the_dsv_had_is_kept_when_it_fits(self) -> None:
        self.put(self.emu / "Game.dsv", save(8192, 1) + footer(8192), 1000)
        self.put(self.raw / "Game.sav", save(8192, 1), 1000)
        self.convert()  # equal content: the base is recorded
        self.put(self.raw / "Game.sav", save(8192, 2), 2000)
        self.convert()
        self.assertEqual((self.emu / "Game.dsv").read_bytes(), save(8192, 2) + footer(8192))

    def test_a_second_run_changes_nothing(self) -> None:
        self.put(self.raw / "A.sav", save(8192, 1))
        self.put(self.emu / "B.dsv", save(512, 2) + footer(512))
        self.convert()
        before = self.tree()
        mtimes = {p: p.stat().st_mtime_ns for p in (self.emu / "A.dsv", self.raw / "B.sav")}
        outcome = self.convert()
        self.assertEqual(self.tree(), before)
        self.assertEqual({p: p.stat().st_mtime_ns for p in mtimes}, mtimes)
        self.assertEqual(outcome.written, 0)

    def test_only_the_side_that_changed_is_copied(self) -> None:
        self.put(self.raw / "Game.sav", save(8192, 1), 1000)
        self.convert()
        # The emulator saves; the hub copy is untouched, however recent it is.
        self.put(self.emu / "Game.dsv", save(8192, 2), 500)
        self.convert()
        self.assertEqual((self.raw / "Game.sav").read_bytes(), save(8192, 2))
        # Now the hub changes.
        self.put(self.raw / "Game.sav", save(8192, 3), 100)
        self.convert()
        self.assertEqual((self.emu / "Game.dsv").read_bytes(), save(8192, 3))
        self.assertEqual(self.warnings, [])
        self.assertEqual(self.saved_state()["conflicts"], [])

    def test_a_deleted_file_is_restored_not_propagated(self) -> None:
        self.put(self.raw / "Game.sav", save(8192, 1))
        self.convert()
        (self.emu / "Game.dsv").unlink()
        self.convert()
        self.assertEqual((self.emu / "Game.dsv").read_bytes(), save(8192, 1))
        (self.raw / "Game.sav").unlink()
        self.convert()
        self.assertEqual((self.raw / "Game.sav").read_bytes(), save(8192, 1))


class DatesTests(Fixture):
    """Syncthing settles a conflict by the newest date, so a copy keeps its source's date."""

    def test_a_file_written_to_the_emulator_keeps_the_date_of_the_hub_file(self) -> None:
        self.put(self.raw / "Game.sav", save(8192, 1), 1000)
        self.convert()
        self.assertEqual((self.emu / "Game.dsv").stat().st_mtime, 1000)

    def test_a_file_written_to_the_hub_keeps_the_date_of_the_dsv(self) -> None:
        self.put(self.emu / "Game.dsv", save(8192, 1) + footer(8192), 2000)
        self.convert()
        self.assertEqual((self.raw / "Game.sav").stat().st_mtime, 2000)

    def test_an_overwrite_keeps_the_source_date_too(self) -> None:
        self.put(self.raw / "Game.sav", save(8192, 1), 1000)
        self.convert()
        self.put(self.emu / "Game.dsv", save(8192, 2), 3000)
        self.convert()
        self.assertEqual((self.raw / "Game.sav").stat().st_mtime, 3000)

    def test_a_save_syncthing_is_still_receiving_is_not_created(self) -> None:
        self.put(self.emu / "Game.dsv", save(8192, 1) + footer(8192))
        self.put(self.raw / ".syncthing.Game.sav.tmp", b"partial")
        self.convert()
        self.assertFalse((self.raw / "Game.sav").exists())
        self.assertTrue(any("still receiving" in line for line in self.lines))
        (self.raw / ".syncthing.Game.sav.tmp").unlink()
        self.convert()
        self.assertTrue((self.raw / "Game.sav").exists())


class SyncthingConflictFileTests(Fixture):
    NAME = "Game.sync-conflict-20260101-000000-ABCDEFG.sav"

    def test_conflict_files_are_reported_and_left_alone(self) -> None:
        self.put(self.raw / "Game.sav", save(8192, 1), 1000)
        self.put(self.raw / self.NAME, save(8192, 9), 2000)
        self.put(self.emu / "Other.sync-conflict-20260101-000000-ABCDEFG.dsv", save(512))
        outcome = self.convert()
        self.assertEqual((self.raw / self.NAME).read_bytes(), save(8192, 9))
        self.assertEqual(len(outcome.conflict_files), 2)
        self.assertTrue(any("conflict file" in w and self.NAME in w for w in self.warnings))

    def test_they_are_recorded_for_doctor_and_named_by_it(self) -> None:
        self.put(self.raw / self.NAME, save(8192, 9))
        self.convert()
        last = self.saved_state()["last"]
        self.assertEqual(last["conflict_files"], [str(self.raw / self.NAME)])
        status, detail, fix = doctor.judge_save_convert(
            device="ark", state=self.saved_state(), backups="/b"
        )
        self.assertEqual(status, doctor.WARN)
        self.assertIn(self.NAME, detail)
        self.assertIn("delete the other", fix)

    def test_no_conflict_files_no_mention(self) -> None:
        self.put(self.raw / "Game.sav", save(8192, 1))
        self.convert()
        self.assertEqual(self.saved_state()["last"]["conflict_files"], [])


class ConflictTests(Fixture):
    def both_changed(self, hub_mtime: int, emu_mtime: int) -> None:
        self.put(self.raw / "Game.sav", save(8192, 1), 1000)
        self.convert()
        self.put(self.raw / "Game.sav", save(8192, 2), hub_mtime)
        self.put(self.emu / "Game.dsv", save(8192, 3), emu_mtime)
        self.convert()

    def test_the_newer_emulator_file_wins_and_the_hub_file_is_kept(self) -> None:
        self.both_changed(2000, 3000)
        self.assertEqual((self.raw / "Game.sav").read_bytes(), save(8192, 3))
        self.assertEqual([b.read_bytes() for b in self.backups()], [save(8192, 2)])
        self.assertTrue(self.backups()[0].name.endswith("Game.sav"))

    def test_the_newer_hub_file_wins_and_the_emulator_file_is_kept(self) -> None:
        self.both_changed(3000, 2000)
        self.assertEqual((self.emu / "Game.dsv").read_bytes(), save(8192, 2))
        self.assertEqual([b.read_bytes() for b in self.backups()], [save(8192, 3)])
        self.assertTrue(self.backups()[0].name.endswith("Game.dsv"))

    def test_the_event_is_reported_and_remembered(self) -> None:
        self.both_changed(2000, 3000)
        self.assertTrue(any("both sides changed" in w for w in self.warnings))
        (event,) = self.saved_state()["conflicts"]
        self.assertEqual((event["game"], event["winner"]), ("Game", "emulator"))

    def test_the_loser_of_a_conflict_outlives_the_routine_rotation(self) -> None:
        self.both_changed(2000, 3000)  # the hub's copy (fill 2) loses
        for n in range(4, 4 + 2 * saveconvert_device.BACKUPS_KEPT):
            self.put(self.raw / "Game.sav", save(8192, n), 4000 + n)
            self.convert()
        kept = self.state / "backups" / "drastic.conflicts" / "Game"
        self.assertEqual([p.read_bytes() for p in kept.iterdir()], [save(8192, 2)])
        routine = self.state / "backups" / "drastic" / "Game"
        self.assertEqual(len(list(routine.iterdir())), saveconvert_device.BACKUPS_KEPT)

    def test_the_report_says_where_the_loser_is(self) -> None:
        self.both_changed(2000, 3000)
        self.assertTrue(
            any(str(self.state / "backups" / "drastic.conflicts") in w for w in self.warnings)
        )
        _, detail, _ = doctor.judge_save_convert(
            device="ark", state=self.saved_state(), backups=str(self.state / "backups")
        )
        self.assertIn(str(self.state / "backups" / "drastic.conflicts" / "Game"), detail)

    def test_conflict_losers_have_their_own_limit(self) -> None:
        self.put(self.raw / "Game.sav", save(8192, 1), 1000)
        self.convert()
        for n in range(2, 2 + saveconvert_device.CONFLICT_BACKUPS_KEPT + 3):
            self.put(self.raw / "Game.sav", save(8192, n), 2000 + n)
            self.put(self.emu / "Game.dsv", save(8192, 100 + n), 3000 + n)
            self.convert()
        kept = self.state / "backups" / "drastic.conflicts" / "Game"
        self.assertEqual(len(list(kept.iterdir())), saveconvert_device.CONFLICT_BACKUPS_KEPT)

    def test_every_overwrite_keeps_the_previous_content(self) -> None:
        self.put(self.raw / "Game.sav", save(8192, 1), 1000)
        self.convert()
        self.put(self.raw / "Game.sav", save(8192, 2), 2000)
        self.convert()
        self.assertEqual([b.read_bytes() for b in self.backups()], [save(8192, 1)])

    def test_only_the_last_few_backups_of_a_game_are_kept(self) -> None:
        self.put(self.raw / "Game.sav", save(8192, 1), 1000)
        self.convert()
        for n in range(2, 9):
            self.put(self.raw / "Game.sav", save(8192, n), 1000 + n)
            self.convert()
        self.assertEqual(len(self.backups()), saveconvert_device.BACKUPS_KEPT)

    def test_a_new_file_has_nothing_to_back_up(self) -> None:
        self.put(self.raw / "Game.sav", save(8192, 1))
        self.convert()
        self.assertEqual(self.backups(), [])


class SafetyTests(Fixture):
    def test_invalid_files_are_skipped_with_a_warning_and_left_alone(self) -> None:
        self.put(self.raw / "Odd.sav", save(1000))
        self.put(self.emu / "Bad.dsv", save(1000) + footer(1000))
        self.put(self.emu / "Junk.dsv", b"not a save")
        self.put(self.raw / "Good.sav", save(512, 4))
        before = self.tree()
        outcome = self.convert()
        self.assertEqual({name for name, _ in outcome.skipped}, {"Odd", "Bad", "Junk"})
        self.assertEqual(len(self.warnings), 3)
        after = self.tree()
        self.assertEqual(
            {k: v for k, v in after.items() if "Good" not in k},
            {k: v for k, v in before.items() if "Good" not in k},
        )
        self.assertEqual((self.emu / "Good.dsv").read_bytes(), save(512, 4))

    def test_sides_of_different_sizes_are_left_alone(self) -> None:
        self.put(self.raw / "Game.sav", save(8192, 1))
        self.put(self.emu / "Game.dsv", save(65536, 2) + footer(65536))
        outcome = self.convert()
        self.assertEqual(outcome.skipped[0][0], "Game")
        self.assertEqual((self.raw / "Game.sav").read_bytes(), save(8192, 1))
        self.assertEqual((self.emu / "Game.dsv").read_bytes(), save(65536, 2) + footer(65536))

    def test_temporary_and_conflict_files_are_never_saves(self) -> None:
        for name in (
            ".Game.sav.wz-tmp",
            ".syncthing.Game.sav.tmp",
            "Game.sync-conflict-20260101-000000-ABCDEFG.sav",
            ".hidden.sav",
            ".sav",
        ):
            self.put(self.raw / name, save(8192))
        self.put(self.emu / "Game.sync-conflict-20260101-000000-ABCDEFG.dsv", save(8192))
        self.put(self.emu / ".Game.dsv.wz-tmp", save(8192))
        self.convert()
        self.assertEqual(
            sorted(p.name for p in self.emu.iterdir()),
            sorted(["Game.sync-conflict-20260101-000000-ABCDEFG.dsv", ".Game.dsv.wz-tmp"]),
        )
        self.assertEqual(self.lines, [])

    def test_directories_and_symlinks_are_not_saves(self) -> None:
        (self.raw / "Dir.sav").mkdir()
        target = self.put(self.tmp / "elsewhere", save(8192))
        (self.raw / "Link.sav").symlink_to(target)
        self.convert()
        self.assertEqual(list(self.emu.iterdir()), [])

    def test_the_temporary_file_of_a_write_is_gone_afterwards(self) -> None:
        self.put(self.emu / "Game.dsv", save(8192, 1) + footer(8192))
        self.convert()
        self.assertEqual([p.name for p in self.raw.iterdir()], ["Game.sav"])

    def test_nothing_is_written_while_the_emulator_runs(self) -> None:
        self.proc.add(4242, "drastic")
        self.put(self.raw / "Game.sav", save(8192, 1))
        before = self.tree()
        busy = []
        outcome = self.convert(on_busy=lambda: busy.append(1))
        self.assertEqual(self.tree(), before)
        self.assertFalse((self.state / "state.json").exists())
        self.assertTrue(outcome.busy and busy)
        self.assertTrue(any("drastic is running" in line for line in self.lines))

    def test_nothing_is_written_when_the_process_table_cannot_be_read(self) -> None:
        self.put(self.raw / "Game.sav", save(8192, 1))
        outcome = self.convert(proc=self.tmp / "no-proc")
        self.assertEqual(list(self.emu.iterdir()), [])
        self.assertTrue(outcome.busy)
        self.assertTrue(any("no /proc" in w for w in self.warnings))

    def test_the_emulator_starting_right_before_a_write_stops_the_run(self) -> None:
        for name in ("A", "B"):
            self.put(self.raw / f"{name}.sav", save(8192, 1))
        real = saveconvert_device._running
        calls = []

        def starts_later(names, proc):
            calls.append(1)
            if len(calls) > 2:  # the check at the start of the rule and A's re-check pass
                self.proc.add(4242, "drastic")
            return real(names, proc)

        saveconvert_device._running = starts_later
        self.addCleanup(setattr, saveconvert_device, "_running", real)
        outcome = self.convert()
        self.assertTrue(outcome.busy)
        self.assertEqual(sorted(p.name for p in self.emu.iterdir()), ["A.dsv"])

    def test_a_missing_hub_folder_is_not_an_error(self) -> None:
        self.raw.rmdir()
        outcome = self.convert()
        self.assertEqual((outcome.written, self.warnings), (0, []))
        self.assertTrue(any("does not exist yet" in line for line in self.lines))

    def test_a_missing_emulator_folder_is_made_when_there_is_a_save_for_it(self) -> None:
        self.emu.rmdir()
        self.put(self.raw / "Game.sav", save(8192, 1))
        self.convert()
        self.assertEqual((self.emu / "Game.dsv").read_bytes(), save(8192, 1))

    def test_a_file_that_changed_while_it_was_read_is_not_overwritten(self) -> None:
        self.put(self.raw / "Game.sav", save(8192, 1), 1000)
        self.convert()
        real = saveconvert_device._read

        def read_then_change(path):
            result = real(path)
            if path is not None and path.name == "Game.dsv":
                self.put(path, save(8192, 9), 5000)  # DraStic-like write after the read
            return result

        self.put(self.raw / "Game.sav", save(8192, 2), 2000)
        saveconvert_device._read = read_then_change
        self.addCleanup(setattr, saveconvert_device, "_read", real)
        outcome = self.convert()
        self.assertEqual((self.emu / "Game.dsv").read_bytes(), save(8192, 9))
        self.assertTrue(outcome.errors)


class DryRunTests(Fixture):
    def test_a_dry_run_writes_nothing_at_all(self) -> None:
        self.put(self.raw / "A.sav", save(8192, 1), 1000)
        self.put(self.emu / "B.dsv", save(512, 2) + footer(512))
        self.convert()
        self.put(self.raw / "A.sav", save(8192, 5), 2000)  # a change to propagate
        self.put(self.emu / "C.dsv", save(512, 3))
        everything = self.tree(state=True)
        self.lines.clear()
        outcome = self.convert(dry_run=True)
        self.assertEqual(self.tree(state=True), everything)
        self.assertEqual(outcome.written, 0)
        self.assertTrue(any("dry-run: would write" in line for line in self.lines))

    def test_a_dry_run_on_a_fresh_device_creates_no_state(self) -> None:
        self.put(self.raw / "A.sav", save(8192, 1))
        self.convert(dry_run=True)
        self.assertFalse(self.state.exists())
        self.assertEqual(list(self.emu.iterdir()), [])


class StateTests(Fixture):
    def test_the_last_run_is_recorded(self) -> None:
        self.put(self.raw / "A.sav", save(8192, 1))
        self.put(self.raw / "Bad.sav", save(1000))
        self.convert()
        last = self.saved_state()["last"]
        self.assertEqual(last["when"], "2026-11-05T12:00:00+00:00")
        self.assertEqual(last["written"], 1)
        self.assertEqual(last["skipped"][0][0], "Bad")

    def test_the_state_is_private(self) -> None:
        self.put(self.raw / "A.sav", save(8192, 1))
        self.convert()
        self.assertEqual((self.state / "state.json").stat().st_mode & 0o777, 0o600)

    def test_a_corrupt_state_is_treated_as_empty(self) -> None:
        self.state.mkdir(parents=True)
        (self.state / "state.json").write_text("{not json")
        self.put(self.raw / "A.sav", save(8192, 1))
        self.convert()
        self.assertEqual(self.saved_state()["last"]["written"], 1)

    def test_parse_state_fills_in_what_is_missing(self) -> None:
        for text in (None, "", "[]", '{"bases": 1, "last": 2, "conflicts": 3}'):
            with self.subTest(text=text):
                self.assertEqual(
                    saveconvert_device.parse_state(text),
                    {"bases": {}, "last": None, "conflicts": []},
                )

    def test_the_state_lives_beside_the_settings_state(self) -> None:
        self.assertEqual(
            self.state, settings_state.state_dir(home=self.home).parent / "save-convert"
        )


class ProfileTests(unittest.TestCase):
    def parse(self, **rule):
        raw = Path("/r/melonds")
        base = {
            "format": "desmume-dsv",
            "saves": "melonds-saves",
            "path": "/r/backup",
            "process": ["drastic"],
        }
        return parse_profile(
            {
                "name": "x",
                "saves": {"melonds-saves": {"path": str(raw), "ignore": ["*.wz-tmp"]}},
                "save_convert": {"drastic": {**base, **rule}},
            },
            "t",
        )

    def test_a_valid_rule(self) -> None:
        rule = self.parse().save_convert["drastic"]
        self.assertEqual(
            (rule.format, rule.saves, rule.path, rule.process, rule.es_system),
            ("desmume-dsv", "melonds-saves", "/r/backup", ("drastic",), None),
        )

    def test_errors_name_the_key(self) -> None:
        cases = {
            "format": {"format": "zip"},
            "saves": {"saves": "nope-saves"},
            "path": {"path": "relative"},
            "process": {"process": []},
            "extra": {"extra": 1},
            "es_system": {"es_system": ""},
        }
        for key, rule in cases.items():
            with self.subTest(key), self.assertRaisesRegex(ProfileError, key):
                self.parse(**rule)

    def test_es_system_needs_es_systems(self) -> None:
        with self.assertRaisesRegex(ProfileError, "es_system needs"):
            self.parse(es_system="nds")

    def test_the_saves_folder_must_ignore_the_temporary_files(self) -> None:
        with self.assertRaisesRegex(ProfileError, r"wz-tmp"):
            parse_profile(
                {
                    "name": "x",
                    "saves": {"melonds-saves": {"path": "/r/m"}},
                    "save_convert": {
                        "d": {
                            "format": "desmume-dsv",
                            "saves": "melonds-saves",
                            "path": "/r/b",
                            "process": ["d"],
                        }
                    },
                },
                "t",
            )

    def test_the_two_folders_must_be_separate(self) -> None:
        with self.assertRaisesRegex(ProfileError, "must not be the saves folder"):
            self.parse(path="/r/melonds/backup")

    def test_a_profile_without_the_table_has_no_rules(self) -> None:
        self.assertEqual(parse_profile({"name": "x"}, "t").save_convert, {})


class JudgeTests(unittest.TestCase):
    def judge(self, state):
        return doctor.judge_save_convert(
            device="ark", state=saveconvert_device.parse_state(json.dumps(state)), backups="/b"
        )

    def test_never_run_is_a_warning_with_the_command(self) -> None:
        status, detail, fix = doctor.judge_save_convert(
            device="ark", state=saveconvert_device.parse_state(None), backups="/b"
        )
        self.assertEqual(status, doctor.WARN)
        self.assertIn("has not run yet", detail)
        self.assertIn("warpzone saves convert ark", fix)

    def test_a_clean_run_is_ok(self) -> None:
        status, detail, _ = self.judge({"last": {"when": "T1", "written": 2}})
        self.assertEqual(status, doctor.OK)
        self.assertIn("T1", detail)
        self.assertIn("2 file(s) written", detail)

    def test_skipped_games_are_named_with_the_reason(self) -> None:
        status, detail, fix = self.judge(
            {"last": {"when": "T1", "skipped": [["Odd", "1000 bytes is not a save size"]]}}
        )
        self.assertEqual(status, doctor.WARN)
        self.assertIn("Odd (", detail)
        self.assertIn("1000 bytes", detail)
        self.assertIn("never converted", fix)

    def test_errors_are_a_warning(self) -> None:
        status, detail, _ = self.judge({"last": {"when": "T1", "errors": ["Game: boom"]}})
        self.assertEqual(status, doctor.WARN)
        self.assertIn("Game: boom", detail)

    def test_latest_wins_events_are_reported_with_where_the_other_copy_is(self) -> None:
        events = [
            {"when": "T0", "rule": "drastic", "game": "Old", "winner": "hub"},
            {"when": "T1", "rule": "drastic", "game": "Game", "winner": "emulator"},
        ]
        status, detail, _ = self.judge({"last": {"when": "T2"}, "conflicts": events})
        self.assertEqual(status, doctor.OK)
        self.assertIn("2 latest-wins event(s)", detail)
        self.assertIn("Game (emulator file newer", detail)
        self.assertIn("/b", detail)


class CommandTests(Fixture):
    """`warpzone saves convert`, through the command line."""

    def setUp(self) -> None:
        super().setUp()
        self.config = self.tmp / "config.toml"
        self.config.write_text(
            "[hub]\n"
            'name = "nas"\n'
            'device_id = "AAAAAAA-AAAAAAA-AAAAAAA-AAAAAAA-AAAAAAA-AAAAAAA-AAAAAAA-AAAAAAA"\n'
            'root = "/data/games"\n'
            'api_url = "http://127.0.0.1:1"\n'
            'api_key = "k"\n'
            "[devices.ark]\n"
            'device_id = "DDDDDDD-DDDDDDD-DDDDDDD-DDDDDDD-DDDDDDD-DDDDDDD-DDDDDDD-DDDDDDD"\n'
            'profile = "anbernic-rg351mp"\n'
            'folders = ["library", "saves"]\n'
        )

    def run_cli(self, *argv: str) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        env = {"HOME": str(self.home), "XDG_STATE_HOME": str(self.home / "state")}
        with (
            contextlib.redirect_stdout(out),
            contextlib.redirect_stderr(err),
            mock.patch.dict(os.environ, env),
            mock.patch("pathlib.Path.home", return_value=self.home),
        ):
            code = cli.main(["saves", "convert", "ark", "--config", str(self.config), *argv])
        return code, out.getvalue(), err.getvalue()

    def test_quiet_prints_nothing(self) -> None:
        # The shipped profile's folders are in /roms, which does not exist here.
        self.assertEqual(self.run_cli("--quiet"), (0, "", ""))

    def test_it_says_what_it_found(self) -> None:
        code, out, _ = self.run_cli("--dry-run")
        self.assertEqual(code, 0)
        self.assertIn("does not exist yet", out)

    def test_a_device_without_the_saves_group_is_refused(self) -> None:
        self.config.write_text(self.config.read_text().replace('"library", "saves"', '"library"'))
        code, _, err = self.run_cli()
        self.assertEqual(code, 2)
        self.assertIn("'saves' folder group", err)

    def test_a_profile_without_a_rule_is_refused(self) -> None:
        self.config.write_text(
            self.config.read_text().replace("anbernic-rg351mp", "steamos-emudeck")
        )
        code, _, err = self.run_cli()
        self.assertEqual(code, 2)
        self.assertIn("save_convert", err)


if __name__ == "__main__":
    unittest.main()
