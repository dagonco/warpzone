"""The queue file on a temporary hub tree: reading, the backups and their rotation, the guarded
write, and the server's short cache of the parsed file."""

import os
import stat
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path

from warpzone import queue as q
from warpzone import queue_store, serve
from warpzone import whitelist_store as store

NOW = datetime(2026, 11, 10, 20, 0, 0, tzinfo=UTC)
STAMP = "2026-11-10T20:00:00Z"


def data(updated: str = STAMP) -> bytes:
    return q.render(q.Queue(updated))


class StoreCase(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        (self.root / "catalog").mkdir()
        self.path = queue_store.queue_path(self.root)


class LoadTests(StoreCase):
    def test_no_file_is_absent(self) -> None:
        loaded = queue_store.load(self.root)
        self.assertEqual((loaded.reading.state, loaded.etag, loaded.size), ("absent", "absent", 0))
        self.assertIsNotNone(loaded.snapshot)

    def test_a_file_is_read_with_its_etag_and_size(self) -> None:
        self.path.write_bytes(data())
        loaded = queue_store.load(self.root)
        self.assertEqual((loaded.reading.state, loaded.size), ("ok", len(data())))
        self.assertEqual(loaded.etag, q.etag_of(data()))

    def test_a_bad_file_is_damaged_not_an_exception(self) -> None:
        self.path.write_bytes(b"{")
        loaded = queue_store.load(self.root)
        self.assertEqual(loaded.reading.state, "damaged")
        self.assertIsNotNone(loaded.snapshot)  # its bytes still give an etag

    def test_a_file_over_16_mib_is_not_read(self) -> None:
        with open(self.path, "wb") as fh:
            fh.truncate(q.FILE_LIMIT + 1)
        loaded = queue_store.load(self.root)
        self.assertEqual((loaded.reading.state, loaded.size), ("damaged", q.FILE_LIMIT + 1))
        self.assertIsNone(loaded.snapshot)

    @unittest.skipIf(os.geteuid() == 0, "root reads everything")
    def test_an_unreadable_file_is_damaged_with_the_cause_and_no_path(self) -> None:
        self.path.write_bytes(data())
        self.path.chmod(0)
        self.addCleanup(self.path.chmod, 0o644)
        loaded = queue_store.load(self.root)
        self.assertEqual(loaded.reading.state, "damaged")
        self.assertNotIn(str(self.root), loaded.reading.message)


class ReplaceTests(StoreCase):
    def test_a_missing_file_is_created_without_a_backup(self) -> None:
        self.assertIsNone(queue_store.replace(self.root, "absent", data(), NOW))
        self.assertEqual(self.path.read_bytes(), data())
        self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o644)
        self.assertFalse(queue_store.backups_dir(self.root).exists())

    def test_the_file_replaced_is_backed_up_by_time(self) -> None:
        old = data("2026-11-01T00:00:00Z")
        self.path.write_bytes(old)
        backup = queue_store.replace(self.root, q.etag_of(old), data(), NOW)
        assert backup is not None
        self.assertEqual(backup.read_bytes(), old)
        self.assertEqual(backup.name, "20261110T200000.000000Z.json")
        self.assertEqual(self.path.read_bytes(), data())

    def test_a_different_etag_is_stale_and_nothing_changes(self) -> None:
        self.path.write_bytes(data())
        with self.assertRaises(store.Stale):
            queue_store.replace(self.root, "sha256:" + "0" * 64, b"{}\n", NOW)
        with self.assertRaises(store.Stale):
            queue_store.replace(self.root, "absent", b"{}\n", NOW)
        self.assertEqual(self.path.read_bytes(), data())
        self.assertFalse(queue_store.backups_dir(self.root).exists())

    def test_a_file_that_appeared_is_not_overwritten(self) -> None:
        self.path.write_bytes(data())
        with self.assertRaises(store.Stale):
            queue_store.replace(self.root, "absent", b"{}\n", NOW)

    def test_a_damaged_file_cannot_be_replaced(self) -> None:
        with open(self.path, "wb") as fh:
            fh.truncate(q.FILE_LIMIT + 1)
        with self.assertRaises(store.Stale):
            queue_store.replace(self.root, "damaged", b"{}\n", NOW)

    def test_no_catalog_folder_means_nothing_is_created(self) -> None:
        (self.root / "catalog").rmdir()
        with self.assertRaises(store.NoLibrary) as caught:
            queue_store.replace(self.root, "absent", data(), NOW)
        self.assertIn("catalog/", str(caught.exception))
        self.assertNotIn(str(self.root), str(caught.exception))
        self.assertFalse((self.root / "catalog").exists())

    def test_a_temporary_file_is_never_left(self) -> None:
        self.path.write_bytes(data())
        queue_store.replace(self.root, q.etag_of(data()), data("2026-11-11T00:00:00Z"), NOW)
        self.assertEqual([p.name for p in self.path.parent.glob(".*wz-tmp")], [])

    def test_two_backups_in_one_instant_get_two_names(self) -> None:
        first, second = b"1", b"2"
        a = queue_store.backup(self.root, first, NOW)
        b = queue_store.backup(self.root, second, NOW)
        self.assertNotEqual(a, b)
        self.assertEqual((a.read_bytes(), b.read_bytes()), (first, second))
        self.assertLess(a.name, b.name)

    def test_only_the_50_newest_backups_are_kept(self) -> None:
        for n in range(55):
            queue_store.backup(self.root, str(n).encode(), NOW + timedelta(seconds=n))
        names = sorted(p.name for p in queue_store.backups_dir(self.root).glob("*.json"))
        self.assertEqual(len(names), 50)
        newest = queue_store.backups_dir(self.root) / names[-1]
        self.assertEqual(newest.read_bytes(), b"54")
        oldest = queue_store.backups_dir(self.root) / names[0]
        self.assertEqual(oldest.read_bytes(), b"5")


class CacheTests(StoreCase):
    def test_the_file_is_read_again_when_it_changed_and_the_interval_passed(self) -> None:
        now = [0.0]
        files = serve.HubFiles(self.root, interval=5, clock=lambda: now[0])
        self.assertEqual(files.queue().reading.state, "absent")
        self.path.write_bytes(data())
        now[0] = 4.0
        self.assertEqual(files.queue().reading.state, "absent")  # not checked yet
        now[0] = 6.0
        self.assertEqual(files.queue().reading.state, "ok")
        held = files.queue()
        now[0] = 12.0
        self.assertIs(files.queue(), held)  # unchanged: the parsed queue is kept
        self.path.write_bytes(data("2026-11-11T00:00:00Z"))
        now[0] = 13.0
        self.assertIs(files.queue(), held)
        files.invalidate_queue()
        self.assertEqual(files.queue().reading.queue.updated, "2026-11-11T00:00:00Z")


if __name__ == "__main__":
    unittest.main()
