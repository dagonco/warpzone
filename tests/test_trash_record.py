"""`trash_record`: the pure format and decisions of the record of deleted games. No disk, no
network."""

import fnmatch
import json
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from warpzone import catalog, config, ignores, trash_record
from warpzone.trash_record import DeletedFile, RecordEntry

NOW = datetime(2026, 11, 16, 10, 15, tzinfo=UTC)
ENTRY = "20261116T101500Z-5d0c1f3a9b2e7c48"
SHA = "a" * 40
FILE = DeletedFile("roms/psx/Game.chd", 943718400, 1730000000123456789, SHA)
OTHER = DeletedFile("roms/psx/Game.cue", 40, 1730000000000000000, None)
ITEM = RecordEntry(ENTRY, "2026-11-16T10:15:00Z", "2027-05-15T10:15:00Z", (FILE, OTHER))


def doc(**over: Any) -> bytes:
    entry: dict[str, Any] = {
        "entry": ENTRY,
        "deleted": "2026-11-16T10:15:00Z",
        "expires": "2027-05-15T10:15:00Z",
        "files": [{"path": "roms/psx/Game.chd", "size": 1, "mtime_ns": 2, "sha1": None}],
    }
    entry.update(over.pop("entry", {}))
    body: dict[str, Any] = {"format": 1, "entries": [entry]}
    body.update(over)
    return json.dumps(body).encode()


def with_path(path: Any) -> bytes:
    file = {"path": path, "size": 1, "mtime_ns": 2, "sha1": None}
    return doc(entry={"files": [file]})


class Part:
    """A catalog part as `entry_from_trash` reads it."""

    def __init__(self, path: str, size: int, mtime: int, sha1: str | None, kind: str = "file"):
        self.path, self.size, self.mtime, self.sha1, self.type = path, size, mtime, sha1, kind


class FormatTests(unittest.TestCase):
    def test_the_name_is_one_the_consoles_already_receive_and_no_device_can_have(self) -> None:
        self.assertEqual(trash_record.FILE, "patterns-_deleted.stignore")
        self.assertTrue(fnmatch.fnmatchcase(trash_record.FILE, "!/patterns-*.stignore"[2:]))
        self.assertIn("!/patterns-*.stignore", ignores.BOOTSTRAP_LINES)
        common = Path(__file__).resolve().parent.parent / "templates" / "patterns-common.stignore"
        self.assertIn("!/patterns-*.stignore", common.read_text().splitlines())
        device = trash_record.FILE.removeprefix("patterns-").removesuffix(".stignore")
        self.assertEqual(device, "_deleted")
        self.assertIsNone(config._NAME_RE.match(device))  # a device name starts with [a-z0-9]
        self.assertEqual(ignores.device_file("deck"), "patterns-deck.stignore")

    def test_the_constants(self) -> None:
        self.assertEqual((trash_record.FORMAT, trash_record.KEEP_DAYS), (1, 180))


class EncodeParseTests(unittest.TestCase):
    def test_a_round_trip(self) -> None:
        later = RecordEntry(
            "20261201T000000Z-" + "1" * 16, "2026-12-01T00:00:00Z", "2027-05-30T00:00:00Z", ()
        )
        data = trash_record.encode([later, ITEM])
        self.assertEqual(
            trash_record.parse(data), [ITEM, later]
        )  # by time, whatever the order given
        self.assertEqual(trash_record.encode(trash_record.parse(data) or []), data)

    def test_the_bytes_are_sorted_json_in_utf8(self) -> None:
        odd = RecordEntry(
            ENTRY, ITEM.deleted, ITEM.expires, (DeletedFile("roms/psx/Café.chd", 1, 2),)
        )
        data = trash_record.encode([odd])
        self.assertIn("Café".encode(), data)
        parsed = json.loads(data)
        self.assertEqual(parsed["format"], 1)
        self.assertEqual(list(parsed["entries"][0]), ["deleted", "entry", "expires", "files"])
        self.assertEqual(
            list(parsed["entries"][0]["files"][0]), ["mtime_ns", "path", "sha1", "size"]
        )
        self.assertEqual(trash_record.parse(trash_record.encode([])), [])

    def test_a_valid_document(self) -> None:
        (item,) = trash_record.parse(doc()) or []
        self.assertEqual(item.files, (DeletedFile("roms/psx/Game.chd", 1, 2, None),))

    def test_nothing_that_is_not_a_record_is_read(self) -> None:
        for data in (
            b"",
            b"not json",
            b"\xff\xfe",
            b"[]",
            b"null",
            b'{"format": 1}',
            b'{"format": 1, "entries": {}}',
            b'{"format": 1, "entries": [1]}',
            b"[" * 100000,
            doc(format=2),
            doc(format="1"),
            doc(format=True),
            doc(format=1.0),
            json.dumps({"entries": []}).encode(),
            doc(entry={"entry": ""}),
            doc(entry={"entry": 5}),
            doc(entry={"deleted": "yesterday"}),
            doc(entry={"expires": None}),
            doc(entry={"files": None}),
            doc(entry={"files": ["roms/psx/Game.chd"]}),
            doc(entry={"files": [{"path": "a/b", "size": "1", "mtime_ns": 2, "sha1": None}]}),
            doc(entry={"files": [{"path": "a/b", "size": True, "mtime_ns": 2, "sha1": None}]}),
            doc(entry={"files": [{"path": "a/b", "size": -1, "mtime_ns": 2, "sha1": None}]}),
            doc(entry={"files": [{"path": "a/b", "size": 1, "mtime_ns": 2.5, "sha1": None}]}),
            doc(entry={"files": [{"path": "a/b", "size": 1, "mtime_ns": 2, "sha1": "abc"}]}),
            doc(entry={"files": [{"path": "a/b", "size": 1, "mtime_ns": 2, "sha1": 7}]}),
            doc(entry={"files": [{"size": 1, "mtime_ns": 2, "sha1": None}]}),
        ):
            self.assertIsNone(trash_record.parse(data), data[:60])

    def test_a_path_that_could_leave_the_library_is_refused(self) -> None:
        for path in (
            "../outside",
            "roms/../../outside",
            "/etc/passwd",
            "roms//psx/Game.chd",
            "roms/psx/",
            "roms/./psx",
            "",
            ".",
            "..",
            "roms/psx/a\0b",
            5,
            None,
            ["a"],
        ):
            self.assertIsNone(trash_record.parse(with_path(path)), repr(path))
        self.assertIsNotNone(trash_record.parse(with_path("roms/psx/a..b/.hidden")))

    def test_one_bad_file_refuses_the_whole_record(self) -> None:
        good = {"path": "a/b", "size": 1, "mtime_ns": 2, "sha1": None}
        bad = {"path": "../b", "size": 1, "mtime_ns": 2, "sha1": None}
        self.assertIsNone(trash_record.parse(doc(entry={"files": [good, bad]})))

    def test_a_sha1_is_forty_lowercase_hex_digits(self) -> None:
        file = {"path": "a/b", "size": 1, "mtime_ns": 2, "sha1": SHA}
        self.assertIsNotNone(trash_record.parse(doc(entry={"files": [file]})))
        self.assertIsNone(trash_record.parse(doc(entry={"files": [{**file, "sha1": "A" * 40}]})))


class EntryTests(unittest.TestCase):
    FILES = (
        DeletedFile("roms/psx/Game.chd", 100, 1_730_000_000_123_456_789),
        DeletedFile("a/z", 1, 2),
    )

    def test_the_times_are_the_deletion_and_180_days_later(self) -> None:
        item = trash_record.entry_from_trash(ENTRY, NOW, self.FILES)
        self.assertEqual(
            (item.deleted, item.expires), ("2026-11-16T10:15:00Z", "2027-05-15T10:15:00Z")
        )
        self.assertEqual(NOW + timedelta(days=180), datetime(2027, 5, 15, 10, 15, tzinfo=UTC))
        self.assertEqual([f.path for f in item.files], ["a/z", "roms/psx/Game.chd"])

    def test_the_sha1_comes_from_a_part_of_the_same_size_and_second(self) -> None:
        part = Part("roms/psx/Game.chd", 100, 1_730_000_000, SHA)
        item = trash_record.entry_from_trash(ENTRY, NOW, self.FILES, [part])
        self.assertEqual(
            {f.path: f.sha1 for f in item.files}, {"a/z": None, "roms/psx/Game.chd": SHA}
        )

    def test_no_other_part_lends_its_sha1(self) -> None:
        for part in (
            Part("roms/psx/Game.chd", 101, 1_730_000_000, SHA),  # another size
            Part("roms/psx/Game.chd", 100, 1_730_000_001, SHA),  # another second
            Part("roms/psx/Game.chd", 100, 1_730_000_000, None),  # no hash known
            Part("roms/psx/Game.chd", 100, 1_730_000_000, SHA, kind="dir"),  # a folder
            Part("roms/psx/Other.chd", 100, 1_730_000_000, SHA),  # another file
        ):
            item = trash_record.entry_from_trash(ENTRY, NOW, self.FILES, [part])
            self.assertEqual({f.sha1 for f in item.files}, {None})

    def test_a_deleted_file_keeps_nothing_of_the_input_order(self) -> None:
        item = trash_record.entry_from_trash(ENTRY, NOW, reversed(self.FILES))
        self.assertEqual([f.path for f in item.files], ["a/z", "roms/psx/Game.chd"])

    def test_the_time_in_an_entry_name(self) -> None:
        self.assertEqual(trash_record.name_time(ENTRY), datetime(2026, 11, 16, 10, 15, tzinfo=UTC))
        self.assertIsNone(trash_record.name_time("nope"))
        self.assertIsNone(trash_record.name_time("20261399T101500Z-" + "1" * 16))

    def test_adding_and_removing_an_entry(self) -> None:
        newer = RecordEntry(
            "20261201T000000Z-" + "1" * 16, "2026-12-01T00:00:00Z", "2027-05-30T00:00:00Z", ()
        )
        both = trash_record.with_entry([newer], ITEM)
        self.assertEqual([e.entry for e in both], [ITEM.entry, newer.entry])
        again = RecordEntry(ITEM.entry, ITEM.deleted, ITEM.expires, ())
        self.assertEqual(trash_record.with_entry(both, again)[0], again)
        self.assertEqual(trash_record.without_entry(both, ITEM.entry), [newer])
        self.assertEqual(trash_record.paths(both), ["roms/psx/Game.chd", "roms/psx/Game.cue"])


class CompactTests(unittest.TestCase):
    def test_an_entry_lives_until_its_expiry(self) -> None:
        before = datetime(2027, 5, 15, 10, 14, 59, tzinfo=UTC)
        at = datetime(2027, 5, 15, 10, 15, tzinfo=UTC)
        self.assertEqual(trash_record.compact([ITEM], before, set()), [ITEM])
        self.assertEqual(trash_record.compact([ITEM], at, set()), [])
        self.assertTrue(trash_record.expired(ITEM, at))
        self.assertFalse(trash_record.expired(ITEM, before))

    def test_a_path_that_exists_again_leaves_its_entry(self) -> None:
        (kept,) = trash_record.compact([ITEM], NOW, {"roms/psx/Game.chd", "elsewhere"})
        self.assertEqual(kept.files, (OTHER,))
        (empty,) = trash_record.compact([ITEM], NOW, {FILE.path, OTHER.path})
        self.assertEqual((empty.entry, empty.files), (ENTRY, ()))

    def test_the_input_is_not_changed_and_the_result_is_ordered(self) -> None:
        newer = RecordEntry(
            "20261201T000000Z-" + "1" * 16, "2026-12-01T00:00:00Z", "2027-05-30T00:00:00Z", ()
        )
        entries = [newer, ITEM]
        self.assertEqual(trash_record.compact(entries, NOW, set()), [ITEM, newer])
        self.assertEqual(entries, [newer, ITEM])

    def test_an_entry_whose_time_is_not_one_counts_as_expired(self) -> None:
        broken = RecordEntry(ENTRY, ITEM.deleted, "never", (FILE,))
        self.assertEqual(trash_record.compact([broken], NOW, set()), [])


class IndexTests(unittest.TestCase):
    def test_the_files_of_the_unexpired_entries_by_path(self) -> None:
        found = trash_record.index([ITEM], NOW)
        self.assertEqual(found, {FILE.path: (FILE,), OTHER.path: (OTHER,)})

    def test_an_expired_entry_authorises_nothing(self) -> None:
        late = datetime(2027, 5, 16, tzinfo=UTC)
        self.assertEqual(trash_record.index([ITEM], late), {})

    def test_a_path_deleted_twice_has_both_records(self) -> None:
        again = RecordEntry(
            "20261201T000000Z-" + "1" * 16, "2026-12-01T00:00:00Z", "2027-05-30T00:00:00Z", (FILE,)
        )
        found = trash_record.index([ITEM, again], NOW)
        self.assertEqual(found[FILE.path], (FILE, FILE))
        late = datetime(2027, 5, 20, tzinfo=UTC)
        self.assertEqual(trash_record.index([ITEM, again], late), {FILE.path: (FILE,)})


class CatalogPartTests(unittest.TestCase):
    def test_a_catalog_part_has_what_entry_from_trash_reads(self) -> None:
        part = catalog.Part("roms/psx/Game.chd", "file", 100, 1_730_000_000, sha1=SHA)
        files = [DeletedFile("roms/psx/Game.chd", 100, 1_730_000_000_999_999_999)]
        item = trash_record.entry_from_trash(ENTRY, NOW, files, [part])
        self.assertEqual(item.files[0].sha1, SHA)


if __name__ == "__main__":
    unittest.main()
