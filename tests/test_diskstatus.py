"""The disk report: the pure decisions, the device writer and the hub reader."""

import contextlib
import dataclasses
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

from support import device_probes
from test_anbernic import ark_config
from test_artwork_device import Art
from test_cli import write_config
from test_provision import reporter
from test_serve import ServeCase
from warpzone import diskstatus, diskstatus_io, provision_artwork
from warpzone.cli import main
from warpzone.diskstatus import Disk
from warpzone.profiles import load_profile
from warpzone.provision_artwork import refresh_artwork

MIB = diskstatus.MIB
GIB = 1 << 30
NOW = 1_800_000_000
STEAM = load_profile("steamos-emudeck")
DAY = 24 * 3600


class MeasureTests(unittest.TestCase):
    def test_numbers_are_rounded_down_to_whole_mib(self) -> None:
        got = diskstatus.measure(512 * GIB + 5, 100 * GIB + MIB - 1, NOW)
        self.assertEqual(got, Disk(512 * GIB, 100 * GIB, NOW))

    def test_impossible_numbers_are_no_report(self) -> None:
        for total, free in ((0, 0), (10 * GIB, 11 * GIB), (-1, 0), (10 * GIB, -1)):
            self.assertIsNone(diskstatus.measure(total, free, NOW), (total, free))


class ShouldWriteTests(unittest.TestCase):
    OLD = Disk(1000 * GIB, 500 * GIB, NOW)

    def new(self, **kw: int) -> Disk:
        return dataclasses.replace(self.OLD, **kw)

    def test_the_first_report_is_written(self) -> None:
        self.assertTrue(diskstatus.should_write(None, self.OLD))

    def test_nothing_changed_writes_nothing(self) -> None:
        self.assertFalse(diskstatus.should_write(self.OLD, self.new(at=NOW + 900)))

    def test_free_space_that_moved_under_one_percent_writes_nothing(self) -> None:
        small = self.new(free=500 * GIB - 9 * GIB, at=NOW + 900)
        self.assertFalse(diskstatus.should_write(self.OLD, small))

    def test_free_space_that_moved_one_percent_is_written_both_ways(self) -> None:
        for free in (500 * GIB - 10 * GIB, 500 * GIB + 10 * GIB):
            self.assertTrue(diskstatus.should_write(self.OLD, self.new(free=free, at=NOW + 900)))

    def test_another_size_is_written(self) -> None:
        self.assertTrue(diskstatus.should_write(self.OLD, self.new(total=2000 * GIB, at=NOW + 9)))

    def test_the_heartbeat_rewrites_an_unchanged_report_once_a_day(self) -> None:
        self.assertFalse(diskstatus.should_write(self.OLD, self.new(at=NOW + DAY - 1)))
        self.assertTrue(diskstatus.should_write(self.OLD, self.new(at=NOW + DAY)))

    def test_a_report_from_the_future_is_replaced(self) -> None:
        self.assertTrue(diskstatus.should_write(self.new(at=NOW + 10 * DAY), self.OLD))


class FormatTests(unittest.TestCase):
    def test_render_is_one_line_in_a_fixed_order_and_parses_back(self) -> None:
        disk = Disk(512 * GIB, 100 * GIB, NOW)
        data = diskstatus.render("deck", disk)
        self.assertEqual(
            data.decode(),
            f'{{"v":1,"device":"deck","at":{NOW},"total":{512 * GIB},"free":{100 * GIB}}}\n',
        )
        self.assertEqual(diskstatus.parse(data, "deck"), disk)

    def test_the_library_size_is_kept_when_present(self) -> None:
        disk = Disk(512 * GIB, 100 * GIB, NOW, library=300 * GIB)
        self.assertEqual(diskstatus.parse(diskstatus.render("deck", disk), "deck"), disk)

    def test_unusable_files_are_none(self) -> None:
        good: dict[str, Any] = {"v": 1, "device": "deck", "at": NOW, "total": 10, "free": 5}
        bad: list[bytes] = [
            b"",
            b"\xff\xfe",
            b"not json",
            b"[1]",
            b'"x"',
            *(json.dumps({**good, **change}).encode() for change in (
                {"v": 2},
                {"device": "machine"},
                {"total": 0},
                {"total": "10"},
                {"total": True},
                {"free": 11},
                {"free": -1},
                {"at": 1.5},
                {"at": None},
            )),
        ]  # fmt: skip
        for data in bad:
            self.assertIsNone(diskstatus.parse(data, "deck"), data)
        for key in ("total", "free", "at"):
            self.assertIsNone(diskstatus.parse(json.dumps({**good, key: None}).encode(), "deck"))
        self.assertIsNotNone(diskstatus.parse(json.dumps(good).encode(), "deck"))

    def test_a_library_size_larger_than_the_disk_is_dropped_not_fatal(self) -> None:
        text = json.dumps(
            {"v": 1, "device": "deck", "at": NOW, "total": 10, "free": 5, "library": 99}
        )
        self.assertEqual(diskstatus.parse(text.encode(), "deck"), Disk(10, 5, NOW))


class StaleTests(unittest.TestCase):
    DISK = Disk(10 * GIB, 5 * GIB, NOW)

    def test_fresh_old_and_future(self) -> None:
        self.assertFalse(diskstatus.stale(self.DISK, NOW + diskstatus.STALE_SECONDS))
        self.assertTrue(diskstatus.stale(self.DISK, NOW + diskstatus.STALE_SECONDS + 1))
        self.assertFalse(diskstatus.stale(self.DISK, NOW - DAY))
        self.assertTrue(diskstatus.stale(self.DISK, NOW - DAY - 1))

    def test_the_api_object(self) -> None:
        got = diskstatus.api_disk(self.DISK, NOW + 60)
        self.assertEqual(
            got, {"total": 10 * GIB, "free": 5 * GIB, "library": None, "at": "2027-01-15T08:00:00Z"}
        )

    def test_nothing_or_stale_gives_null_fields(self) -> None:
        nulls = {"total": None, "free": None, "library": None, "at": None}
        self.assertEqual(diskstatus.api_disk(None, NOW), nulls)
        self.assertEqual(diskstatus.api_disk(self.DISK, NOW + 30 * DAY), nulls)


class DeviceTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.home = Path(self._tmp.name) / "home"
        self.folder = self.home / ".local/share/warpzone/play-state"
        self.library = self.home / "Emulation"
        for here in (self.folder, self.library):
            (here / ".stfolder").mkdir(parents=True)
        self.path = self.folder / "deck.status.json"
        self.free = 100 * GIB
        self.clock = NOW
        self.lines: list[str] = []
        self.warnings: list[str] = []

    def publish(self, **kw: Any) -> bool:
        kw.setdefault("now", lambda: self.clock)
        kw.setdefault("usage", lambda path: (512 * GIB, 412 * GIB, self.free))
        return diskstatus_io.publish(
            "deck",
            STEAM,
            self.home,
            say=self.lines.append,
            warn=self.warnings.append,
            **kw,
        )

    def test_it_writes_the_report_beside_the_ledgers(self) -> None:
        self.assertTrue(self.publish())
        got = diskstatus.parse(self.path.read_bytes(), "deck")
        self.assertEqual(got, Disk(512 * GIB, 100 * GIB, NOW))
        self.assertEqual(self.warnings, [])
        self.assertEqual(
            sorted(p.name for p in self.folder.iterdir()), [".stfolder", "deck.status.json"]
        )

    def test_it_measures_the_library_folder(self) -> None:
        seen: list[Path] = []

        def usage(path: Path) -> tuple[int, int, int]:
            seen.append(path)
            return 512 * GIB, 0, self.free

        self.publish(usage=usage)
        self.assertEqual(seen, [self.library])

    def test_a_second_run_with_the_same_numbers_writes_nothing(self) -> None:
        self.publish()
        before = self.path.stat()
        self.clock += 900
        self.free -= 5 * GIB
        self.assertFalse(self.publish())
        self.assertEqual(self.path.stat().st_mtime_ns, before.st_mtime_ns)

    def test_a_real_change_and_the_heartbeat_write_again(self) -> None:
        self.publish()
        self.clock += 900
        self.free -= 20 * GIB
        self.assertTrue(self.publish())
        self.assertEqual(diskstatus.parse(self.path.read_bytes(), "deck").free, 80 * GIB)  # type: ignore[union-attr]
        self.clock += DAY
        self.assertTrue(self.publish())

    def test_a_damaged_file_is_replaced(self) -> None:
        self.path.write_text("{")
        self.assertTrue(self.publish())
        self.assertIsNotNone(diskstatus.parse(self.path.read_bytes(), "deck"))

    def test_dry_run_writes_nothing(self) -> None:
        self.assertFalse(self.publish(dry_run=True))
        self.assertFalse(self.path.exists())
        self.assertEqual(self.lines, [f"disk: dry-run: would write {self.path}"])

    def test_without_the_play_state_folder_or_the_library_marker_it_does_nothing(self) -> None:
        for marker in (self.folder / ".stfolder", self.library / ".stfolder"):
            marker.rmdir()
            self.assertFalse(self.publish(usage=self.fail), marker)
            marker.mkdir()
        self.assertFalse(self.path.exists())

    def fail(self, path: Path) -> tuple[int, int, int]:
        raise AssertionError("the disk must not be measured")

    def test_a_failure_is_a_warning_not_an_error(self) -> None:
        def broken(path: Path) -> tuple[int, int, int]:
            raise OSError("gone")

        self.assertFalse(self.publish(usage=broken))
        self.assertTrue(self.warnings[0].startswith("disk: cannot write"))
        self.assertFalse(self.path.exists())

    def test_a_size_of_zero_is_a_warning(self) -> None:
        self.assertFalse(self.publish(usage=lambda path: (0, 0, 0)))
        self.assertEqual(len(self.warnings), 1)

    def test_the_ledger_readers_do_not_see_the_report(self) -> None:
        from warpzone import playstate_device

        self.publish()
        loaded = playstate_device.load(self.folder, "deck", "deck", self.warnings.append)
        assert loaded is not None
        self.assertEqual(loaded[1], [])
        self.assertEqual(self.warnings, [])


class HubReadTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.folder = Path(self._tmp.name)

    def test_each_device_gets_its_own_report_or_nulls(self) -> None:
        (self.folder / "deck.status.json").write_bytes(
            diskstatus.render("deck", Disk(10 * GIB, 4 * GIB, NOW))
        )
        (self.folder / "old.status.json").write_bytes(
            diskstatus.render("old", Disk(10 * GIB, 4 * GIB, NOW - 30 * DAY))
        )
        (self.folder / "bad.status.json").write_text("{nope")
        (self.folder / "other.status.json").write_bytes(  # a copy under another name
            diskstatus.render("deck", Disk(10 * GIB, 4 * GIB, NOW))
        )
        got = diskstatus_io.read_all(self.folder, ["deck", "old", "bad", "other", "none"], NOW)
        self.assertEqual(got["deck"]["free"], 4 * GIB)
        nulls = diskstatus.api_disk(None, NOW)
        for name in ("old", "bad", "other", "none"):
            self.assertEqual(got[name], nulls, name)

    def test_a_missing_folder_or_a_folder_in_place_of_a_file_is_nulls(self) -> None:
        nulls = diskstatus.api_disk(None, NOW)
        self.assertEqual(diskstatus_io.read_all(self.folder / "nope", ["deck"], NOW)["deck"], nulls)
        (self.folder / "deck.status.json").mkdir()
        self.assertEqual(diskstatus_io.read_all(self.folder, ["deck"], NOW)["deck"], nulls)


class ApiTests(ServeCase):
    def play_state(self) -> Path:
        folder = self.root / "frontend" / "play-state"
        folder.mkdir(parents=True, exist_ok=True)
        return folder

    def disks(self) -> dict[str, Any]:
        reply = self.get("/api/v1/devices")
        self.assertEqual(reply.status, 200)
        return {d["name"]: d["disk"] for d in reply.json}

    def test_without_reports_every_field_is_null(self) -> None:
        nulls = {"total": None, "free": None, "library": None, "at": None}
        self.assertEqual(self.disks(), {"deck": nulls, "machine": nulls})

    def test_a_fresh_report_is_served_and_a_stale_or_damaged_one_is_null(self) -> None:
        import time

        folder = self.play_state()
        now = int(time.time())
        (folder / "deck.status.json").write_bytes(
            diskstatus.render("deck", Disk(512 * GIB, 100 * GIB, now - 60))
        )
        (folder / "machine.status.json").write_text("garbage")
        got = self.disks()
        self.assertEqual((got["deck"]["total"], got["deck"]["free"]), (512 * GIB, 100 * GIB))
        self.assertRegex(got["deck"]["at"], r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$")
        self.assertIsNone(got["machine"]["total"])
        (folder / "deck.status.json").write_bytes(
            diskstatus.render("deck", Disk(512 * GIB, 100 * GIB, now - 30 * DAY))
        )
        self.assertIsNone(self.disks()["deck"]["total"])


class WiringTests(unittest.TestCase):
    def test_play_state_sync_publishes_the_disk(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            for here in (".local/share/warpzone/play-state", "Emulation"):
                (home / here / ".stfolder").mkdir(parents=True)
            path = Path(write_config(tmp, "http://127.0.0.1:1"))
            path.write_text(path.read_text().replace('"frontend"]', '"frontend", "play-state"]'))
            env = {"HOME": tmp, "XDG_STATE_HOME": f"{tmp}/state"}
            argv = ["play-state", "sync", "deck", "--config", str(path)]
            with (
                mock.patch.dict(os.environ, env),
                mock.patch.object(Path, "home", return_value=home),
                contextlib.redirect_stderr(io.StringIO()),
                contextlib.redirect_stdout(io.StringIO()),
            ):
                self.assertEqual(main([*argv, "--dry-run"]), 0)
                report = home / ".local/share/warpzone/play-state/deck.status.json"
                self.assertFalse(report.exists())
                self.assertEqual(main(argv), 0)
            self.assertIsNotNone(diskstatus.parse(report.read_bytes(), "deck"))


class ArtworkWiringTests(Art, unittest.TestCase):
    def refresh(self, folders: tuple[str, ...]) -> list[Any]:
        rep, _ = reporter()
        with mock.patch.object(provision_artwork.diskstatus_io, "publish") as publish:
            refresh_artwork(
                ark_config(folders),
                "ark",
                self.profile,
                self.home,
                rep,
                local=None,
                dry_run=False,
                probes=device_probes(self.proc.path),
            )
        return publish.call_args_list

    def test_frontend_refresh_publishes_only_for_a_device_with_play_state(self) -> None:
        self.assertEqual(self.refresh(("library", "frontend")), [])
        (call,) = self.refresh(("library", "frontend", "play-state"))
        self.assertEqual(call.args[0], "ark")
        self.assertFalse(call.kwargs["dry_run"])


if __name__ == "__main__":
    unittest.main()
