import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from warpzone import safefile as sf


class TmpCase(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)


class AtomicWriteTests(TmpCase):
    def setUp(self) -> None:
        super().setUp()
        self.path = self.tmp / "a.cfg"
        self.path.write_bytes(b"old\n")
        self.path.chmod(0o640)

    def test_stamp(self) -> None:
        s = sf.stamp(self.path)
        self.assertEqual(s.size, 4)
        self.assertEqual(len(s.sha256), 64)
        self.assertEqual(s, sf.stamp(self.path))
        self.path.write_bytes(b"new\n")
        self.assertNotEqual(s, sf.stamp(self.path))

    def test_writes_and_preserves_mode(self) -> None:
        self.assertTrue(sf.atomic_write(self.path, b"new\n", expect=sf.stamp(self.path)))
        self.assertEqual(self.path.read_bytes(), b"new\n")
        self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o640)
        self.assertEqual([p.name for p in self.tmp.iterdir()], ["a.cfg"])

    def test_refuses_when_the_file_changed_after_stamp(self) -> None:
        expect = sf.stamp(self.path)
        self.path.write_bytes(b"edited elsewhere\n")
        self.assertFalse(sf.atomic_write(self.path, b"new\n", expect=expect))
        self.assertEqual(self.path.read_bytes(), b"edited elsewhere\n")
        self.assertEqual([p.name for p in self.tmp.iterdir()], ["a.cfg"])

    def test_refuses_when_only_the_mtime_changed(self) -> None:
        expect = sf.stamp(self.path)
        os.utime(self.path, ns=(1, expect.mtime_ns + 5_000_000_000))
        self.assertFalse(sf.atomic_write(self.path, b"new\n", expect=expect))
        self.assertEqual(self.path.read_bytes(), b"old\n")

    def test_refuses_when_the_file_vanished(self) -> None:
        expect = sf.stamp(self.path)
        self.path.unlink()
        self.assertFalse(sf.atomic_write(self.path, b"new\n", expect=expect))
        self.assertFalse(self.path.exists())

    def test_writes_through_a_symlink(self) -> None:
        target_dir = self.tmp / "real"
        target_dir.mkdir()
        target = target_dir / "real.cfg"
        target.write_bytes(b"old\n")
        link = self.tmp / "link.cfg"
        link.symlink_to(target)
        self.assertTrue(sf.atomic_write(link, b"new\n", expect=sf.stamp(link)))
        self.assertTrue(link.is_symlink())
        self.assertEqual(target.read_bytes(), b"new\n")
        self.assertEqual(sorted(p.name for p in target_dir.iterdir()), ["real.cfg"])

    def test_stale_temp_file_is_replaced(self) -> None:
        (self.tmp / ".a.cfg.wz-tmp").write_bytes(b"junk")
        self.assertTrue(sf.atomic_write(self.path, b"new\n", expect=sf.stamp(self.path)))
        self.assertEqual([p.name for p in self.tmp.iterdir()], ["a.cfg"])

    def test_create_new_file_with_missing_directories(self) -> None:
        new = self.tmp / "deep" / "er" / "n.cfg"
        self.assertTrue(sf.atomic_write(new, b"x", expect=None))
        self.assertEqual(new.read_bytes(), b"x")
        self.assertEqual(stat.S_IMODE(new.stat().st_mode) & 0o600, 0o600)

    def test_create_refuses_when_the_file_appeared(self) -> None:
        self.assertFalse(sf.atomic_write(self.path, b"new\n", expect=None))
        self.assertEqual(self.path.read_bytes(), b"old\n")


class AtomicWriteRaceTests(TmpCase):
    """The file changes after the temporary copy is written, right before the move."""

    def setUp(self) -> None:
        super().setUp()
        self.path = self.tmp / "a.cfg"
        self.path.write_bytes(b"old\n")

    def test_a_file_edited_after_the_copy_was_written_is_not_replaced(self) -> None:
        expect = sf.stamp(self.path)
        real = sf.stamp

        def edit_then_stamp(path: Path) -> sf.FileStamp:
            path.write_bytes(b"edited meanwhile\n")
            return real(path)

        with mock.patch.object(sf, "stamp", edit_then_stamp):
            self.assertFalse(sf.atomic_write(self.path, b"new\n", expect=expect))
        self.assertEqual(self.path.read_bytes(), b"edited meanwhile\n")
        self.assertEqual([p.name for p in self.tmp.iterdir()], ["a.cfg"])

    def test_a_file_removed_after_the_copy_was_written_is_not_recreated(self) -> None:
        expect = sf.stamp(self.path)

        def remove_then_stamp(path: Path) -> sf.FileStamp:
            path.unlink()
            raise FileNotFoundError(path)

        with mock.patch.object(sf, "stamp", remove_then_stamp):
            self.assertFalse(sf.atomic_write(self.path, b"new\n", expect=expect))
        self.assertEqual(list(self.tmp.iterdir()), [])

    def test_a_file_that_appeared_after_the_copy_was_written_is_not_replaced(self) -> None:
        new = self.tmp / "new.cfg"
        real = os.fsync

        def fsync_then_create(fd: int) -> None:
            real(fd)
            new.write_bytes(b"someone else\n")

        with mock.patch.object(sf.os, "fsync", fsync_then_create):
            self.assertFalse(sf.atomic_write(new, b"mine\n", expect=None))
        self.assertEqual(new.read_bytes(), b"someone else\n")
        self.assertEqual(sorted(p.name for p in self.tmp.iterdir()), ["a.cfg", "new.cfg"])

    def test_a_failed_write_leaves_no_temporary_file(self) -> None:
        with (
            mock.patch.object(sf.os, "replace", side_effect=OSError("disk")),
            self.assertRaises(OSError),
        ):
            sf.atomic_write(self.path, b"new\n", expect=sf.stamp(self.path))
        self.assertEqual([p.name for p in self.tmp.iterdir()], ["a.cfg"])
        self.assertEqual(self.path.read_bytes(), b"old\n")


class WriteFileTests(TmpCase):
    def test_creates_directories_and_sets_the_mode(self) -> None:
        path = self.tmp / "deep" / "x.sh"
        sf.write_script(path, "echo hi\n")
        self.assertEqual(path.read_text(), "echo hi\n")
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o755)
        sf.write_script(path, "echo again\n", 0o644)
        self.assertEqual(path.read_text(), "echo again\n")
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o644)
        self.assertEqual([p.name for p in path.parent.iterdir()], ["x.sh"])

    def test_private_by_default_and_a_stale_temporary_file_is_replaced(self) -> None:
        path = self.tmp / "state.json"
        (self.tmp / ".state.json.wz-tmp").write_bytes(b"junk")
        sf.write_file(path, b"{}")
        self.assertEqual(path.read_bytes(), b"{}")
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        self.assertEqual([p.name for p in self.tmp.iterdir()], ["state.json"])

    def test_a_failed_write_keeps_the_old_file_and_leaves_no_temporary_file(self) -> None:
        path = self.tmp / "x.sh"
        path.write_text("old")
        with (
            mock.patch.object(sf.os, "replace", side_effect=OSError("disk")),
            self.assertRaises(OSError),
        ):
            sf.write_script(path, "new")
        self.assertEqual(path.read_text(), "old")
        self.assertEqual([p.name for p in self.tmp.iterdir()], ["x.sh"])


class GuardedEditTests(TmpCase):
    """Every way a guarded edit can end, and the order of its steps."""

    def setUp(self) -> None:
        super().setUp()
        self.path = self.tmp / "settings.cfg"
        self.path.write_bytes(b"old\n")
        self.steps: list[str] = []

    def edit(self, *, busy=False, backup=True, expect="stamp", data=b"new\n", edit_in=None):
        def do_backup() -> None:
            self.steps.append("backup")
            if edit_in == "backup":
                self.path.write_bytes(b"edited meanwhile\n")

        def is_busy() -> bool:
            self.steps.append("busy")
            return busy

        return sf.guarded_edit(
            self.path,
            data,
            expect=sf.stamp(self.path) if expect == "stamp" else expect,
            busy=is_busy,
            backup=do_backup if backup else None,
        )

    def test_writes_after_the_backup_and_the_recheck(self) -> None:
        self.assertIs(self.edit(), sf.Edit.WRITTEN)
        self.assertEqual(self.path.read_bytes(), b"new\n")
        self.assertEqual(self.steps, ["backup", "busy"])

    def test_a_program_that_started_during_the_backup_stops_the_write(self) -> None:
        self.assertIs(self.edit(busy=True), sf.Edit.BUSY)
        self.assertEqual(self.path.read_bytes(), b"old\n")
        self.assertEqual(self.steps, ["backup", "busy"])

    def test_a_file_edited_during_the_backup_is_left_alone(self) -> None:
        self.assertIs(self.edit(edit_in="backup"), sf.Edit.CHANGED)
        self.assertEqual(self.path.read_bytes(), b"edited meanwhile\n")
        self.assertEqual([p.name for p in self.tmp.iterdir()], ["settings.cfg"])

    def test_without_a_backup_the_recheck_still_runs(self) -> None:
        self.assertIs(self.edit(backup=False, busy=True), sf.Edit.BUSY)
        self.assertEqual(self.steps, ["busy"])
        self.assertIs(self.edit(backup=False), sf.Edit.WRITTEN)

    def test_a_new_file_is_created_and_refused_when_one_appeared(self) -> None:
        self.path.unlink()
        self.assertIs(self.edit(expect=None, backup=False), sf.Edit.WRITTEN)
        self.assertEqual(self.path.read_bytes(), b"new\n")
        self.assertIs(self.edit(expect=None, backup=False), sf.Edit.CHANGED)

    def test_the_data_function_runs_after_the_recheck_and_only_then(self) -> None:
        def make() -> bytes:
            self.steps.append("make")
            return b"made\n"

        self.assertIs(self.edit(data=make), sf.Edit.WRITTEN)
        self.assertEqual(self.steps, ["backup", "busy", "make"])
        self.assertEqual(self.path.read_bytes(), b"made\n")
        self.steps.clear()
        self.assertIs(self.edit(data=make, busy=True), sf.Edit.BUSY)
        self.assertEqual(self.steps, ["backup", "busy"])

    def test_what_the_data_function_raises_propagates_with_the_file_untouched(self) -> None:
        def broken() -> bytes:
            raise ValueError("cannot build")

        with self.assertRaises(ValueError):
            self.edit(data=broken)
        self.assertEqual(self.path.read_bytes(), b"old\n")


class JsonCacheTests(TmpCase):
    def test_round_trip_in_compact_form(self) -> None:
        path = self.tmp / "cache" / "x.json"
        sf.write_json_cache(path, {"a": [1, 2], "b": "c"})
        self.assertEqual(path.read_text(), '{"a":[1,2],"b":"c"}')
        self.assertEqual(sf.read_json_cache(path), {"a": [1, 2], "b": "c"})
        self.assertEqual([p.name for p in path.parent.iterdir()], ["x.json"])

    def test_a_missing_unreadable_or_corrupt_cache_reads_as_none(self) -> None:
        path = self.tmp / "x.json"
        self.assertIsNone(sf.read_json_cache(path))
        path.write_text("{not json")
        self.assertIsNone(sf.read_json_cache(path))
        path.write_bytes(b"\xff\xfe")
        self.assertIsNone(sf.read_json_cache(path))
        path.unlink()
        path.mkdir()
        self.assertIsNone(sf.read_json_cache(path))

    def test_a_cache_that_cannot_be_written_is_not_an_error(self) -> None:
        blocker = self.tmp / "file"
        blocker.write_text("not a directory")
        sf.write_json_cache(blocker / "x.json", {"a": 1})
        self.assertEqual(blocker.read_text(), "not a directory")

    def test_a_new_write_replaces_the_old_cache(self) -> None:
        path = self.tmp / "x.json"
        sf.write_json_cache(path, [1])
        sf.write_json_cache(path, [2])
        self.assertEqual(sf.read_json_cache(path), [2])


class ImportTests(unittest.TestCase):
    def test_no_network_imports(self) -> None:
        text = Path(sf.__file__).read_text()
        for word in ("urllib", "http", "socket"):
            self.assertNotIn(f"import {word}", text)
            self.assertNotIn(f"from {word}", text)


if __name__ == "__main__":
    unittest.main()
