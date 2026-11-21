"""Rule 4b of prune: the files the hub deleted on purpose (the record of deleted games)."""

import dataclasses
import hashlib
import os
import unittest
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from unittest import mock

from notify_support import Receiver
from test_prune import (
    MTIME,
    PROFILE,
    Device,
    entry,
    hub_copy,
    ignored_info,
    wanted_info,
)
from warpzone import prune, trash_record
from warpzone.model import device_specs
from warpzone.probes import REAL_PROBES
from warpzone.prune import (
    Entry,
    FolderReport,
    Verdict,
    apply_folder,
    classify,
    inspect_folder,
    print_report,
    sha1_ok,
)
from warpzone.report import Reporter
from warpzone.trash_record import DeletedFile, RecordEntry

PATH = "roms/n64/a.z64"
RECORD = trash_record.FILE
NOW = float(MTIME + 86_400)  # a day after the hub deleted the game
CONTENT = "12345"
SHA1 = hashlib.sha1(CONTENT.encode()).hexdigest()
OTHER_SHA1 = hashlib.sha1(b"other").hexdigest()


def deleted_file(
    path: str = PATH, size: int = 5, mtime: float = MTIME, sha1: str | None = None
) -> DeletedFile:
    return DeletedFile(path, size, int(mtime * 1_000_000_000), sha1)


def deleted_hub(**kw: Any) -> dict[str, Any]:
    return {**hub_copy(), "deleted": True, **kw}


def trashed_info(**kw: Any) -> dict[str, Any]:
    """What db/file says for a file the console turned off and the hub then deleted."""
    return ignored_info(deleted_hub(**kw))


def record_entry(
    files: Sequence[DeletedFile],
    name: str = "20231114T231320Z-5d0c1f3a9b2e7c48",
    deleted: float = MTIME + 3600,
) -> RecordEntry:
    when = datetime.fromtimestamp(deleted, UTC)
    return RecordEntry(
        name,
        trash_record.when_text(when),
        trash_record.when_text(trash_record.expiry(when)),
        tuple(files),
    )


class ClassifyTrashedTests(unittest.TestCase):
    def verdict(self, info: Any, deleted: Sequence[DeletedFile], **kw: Any) -> Verdict:
        return classify(entry(**kw), info, deleted)[0]

    def test_it_deletes_when_everything_matches(self) -> None:
        self.assertIs(self.verdict(trashed_info(), [deleted_file()]), Verdict.TRASHED)

    def test_the_default_is_no_record_and_the_old_verdicts(self) -> None:
        self.assertIs(classify(entry(), trashed_info())[0], Verdict.LOCAL_ONLY)
        self.assertIs(classify(entry(), ignored_info(hub_copy()))[0], Verdict.LEFTOVER)

    def test_a_live_hub_copy_is_rule_4a_whatever_the_record_says(self) -> None:
        live = ignored_info(hub_copy())
        self.assertIs(self.verdict(live, [deleted_file()]), Verdict.LEFTOVER)
        self.assertIs(self.verdict(live, [deleted_file()], size=6), Verdict.DIFFERS)

    def test_a_file_removed_from_the_hub_by_hand_is_kept(self) -> None:
        self.assertIs(self.verdict(trashed_info(), []), Verdict.LOCAL_ONLY)

    def test_a_different_size_is_kept(self) -> None:
        self.assertIs(self.verdict(trashed_info(), [deleted_file()], size=6), Verdict.LOCAL_ONLY)
        self.assertIs(self.verdict(trashed_info(), [deleted_file(size=6)]), Verdict.LOCAL_ONLY)

    def test_the_modification_time_is_within_two_seconds(self) -> None:
        for off, expected in (
            (1.5, Verdict.TRASHED),
            (2, Verdict.TRASHED),
            (3, Verdict.LOCAL_ONLY),
        ):
            for sign in (1, -1):
                with self.subTest(off=off, sign=sign):
                    got = self.verdict(trashed_info(), [deleted_file(mtime=MTIME + sign * off)])
                    self.assertIs(got, expected)

    def test_a_deleted_record_the_hub_marks_invalid_or_ignored_is_kept(self) -> None:
        for flag in ("invalid", "ignored"):
            with self.subTest(flag):
                got = self.verdict(trashed_info(**{flag: True}), [deleted_file()])
                self.assertIs(got, Verdict.LOCAL_ONLY)
        self.assertIs(self.verdict(ignored_info({}), [deleted_file()]), Verdict.LOCAL_ONLY)
        self.assertIs(self.verdict(ignored_info(None), [deleted_file()]), Verdict.LOCAL_ONLY)

    def test_a_symlink_is_never_trashed(self) -> None:
        got = self.verdict(trashed_info(), [deleted_file()], kind="symlink", size=5)
        self.assertIs(got, Verdict.LOCAL_ONLY)

    def test_a_file_that_is_not_ignored_or_not_known_is_not_looked_at(self) -> None:
        wanted = {"global": deleted_hub(), "local": {"ignored": False}}
        self.assertIs(self.verdict(wanted, [deleted_file()]), Verdict.WANTED)
        self.assertIs(self.verdict(None, [deleted_file()]), Verdict.UNTRACKED)

    def test_some_entry_matching_is_enough(self) -> None:
        files = [deleted_file(mtime=MTIME + 50), deleted_file(), deleted_file(size=9)]
        self.assertIs(self.verdict(trashed_info(), files), Verdict.TRASHED)

    def test_the_sha1_is_not_looked_at(self) -> None:
        self.assertIs(self.verdict(trashed_info(), [deleted_file(sha1=SHA1)]), Verdict.TRASHED)

    def test_a_name_that_is_not_utf8_matches_the_record_that_names_it_the_same(self) -> None:
        path = "roms/n64/\udcff.z64"
        item = record_entry([deleted_file(path)])
        entries = trash_record.parse(trash_record.encode([item]))
        assert entries is not None
        index = trash_record.index(entries, datetime.fromtimestamp(NOW, UTC))
        got = classify(Entry(path, "file", 5, MTIME), trashed_info(), index[path])
        self.assertIs(got[0], Verdict.TRASHED)


class Sha1OkTests(unittest.TestCase):
    def test_match_and_mismatch(self) -> None:
        self.assertTrue(sha1_ok(deleted_file(sha1=SHA1), SHA1))
        self.assertFalse(sha1_ok(deleted_file(sha1=SHA1), OTHER_SHA1))

    def test_a_record_without_a_sha1_has_nothing_to_compare(self) -> None:
        self.assertTrue(sha1_ok(deleted_file(), SHA1))


class TrashedDevice(Device):
    """A console whose library holds a game that is ignored and that the hub deleted."""

    def put_record(self, entries: Sequence[RecordEntry]) -> Path:
        path = self.root / RECORD
        data = trash_record.encode(entries)
        path.write_bytes(data)
        self.set_record_info("HUB:7", None, len(data))
        return path

    def set_record_info(self, version: str, local_version: str | None, size: int) -> None:
        self.fake.file_info[("wz-library", RECORD)] = {
            "global": {"version": [version]},
            "local": {"version": [local_version or version], "size": size},
        }

    def inspect(self) -> FolderReport:
        spec = next(s for s in device_specs(self.cfg, "deck", PROFILE) if s.id == "wz-library")
        spec = dataclasses.replace(spec, path=str(self.root))
        configured = {f["id"]: f for f in self.client.folders()}
        return inspect_folder(
            self.client,
            spec,
            configured,
            home=self.home,
            rescan_timeout=5,
            poll=0,
            grace=0,
            probes=REAL_PROBES,
            now=lambda: NOW,
        )

    def prune(self, **kw: Any) -> int:
        kw.setdefault("now", lambda: NOW)
        return super().prune(**kw)


class TrashedTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.dev = TrashedDevice(self)

    def out(self) -> str:
        return "\n".join(self.dev.lines)

    def game(self, *, content: str = CONTENT, sha1: str | None = None) -> Path:
        """The turned-off game on the console and a record that lists it."""
        path = self.dev.put(PATH, content, info=trashed_info(size=len(content)))
        self.dev.put_record([record_entry([deleted_file(size=len(content), sha1=sha1)])])
        return path


class DeleteTrashedTests(TrashedTestCase):
    def test_a_game_turned_off_and_then_deleted_leaves_the_console(self) -> None:
        d = self.dev
        gone = self.game()
        d.fake.file_info[("wz-library", "roms/n64")] = ignored_info(hub_copy(0, "DIR"))
        self.assertEqual(d.prune(), 0)
        self.assertTrue(gone.exists())
        self.assertIn("wz-library: 1 file(s) the hub deleted on purpose, 5 B", self.out())
        self.assertIn("report only", self.out())
        self.assertEqual(d.prune(apply=True), 0)
        self.assertFalse(gone.exists())
        self.assertIn("deleted 1 file(s), 5 B", self.out())
        self.assertTrue((d.root / RECORD).exists())

    def test_the_files_are_listed_like_the_other_leftovers(self) -> None:
        self.game()
        self.dev.prune(list_all=True)
        self.assertIn(f"  {PATH}", self.dev.lines)
        self.dev.lines.clear()
        self.dev.prune()
        self.assertIn("  roms/n64: 1 file(s), 5 B", self.dev.lines)

    def test_an_unattended_run_names_no_file(self) -> None:
        self.game()
        self.dev.put("roms/nes/mario.nes", "x" * 50, info=wanted_info(50))
        self.assertEqual(self.dev.prune(apply=True, unattended=True), 0)
        self.assertIn("1 file(s) the hub deleted on purpose", self.out())
        self.assertNotIn(PATH, self.out())

    def test_a_file_removed_from_the_hub_by_hand_is_kept(self) -> None:
        d = self.dev
        path = d.put(PATH, info=trashed_info())
        d.put_record([record_entry([deleted_file("roms/n64/other.z64")])])
        self.assertEqual(d.prune(apply=True), 0)
        self.assertTrue(path.exists())
        self.assertIn(f"{PATH} (the hub does not have it)", self.out())
        self.assertNotIn("on purpose", self.out())

    def test_no_record_file_authorises_nothing(self) -> None:
        path = self.dev.put(PATH, info=trashed_info())
        self.assertEqual(self.dev.prune(apply=True), 0)
        self.assertTrue(path.exists())

    def test_a_record_that_does_not_parse_authorises_nothing(self) -> None:
        for data in (b"", b"not json", b'{"format": 2, "entries": []}', b'{"format": 1}'):
            with self.subTest(data=data):
                path = self.dev.put(PATH, info=trashed_info())
                (self.dev.root / RECORD).write_bytes(data)
                self.dev.set_record_info("HUB:7", None, len(data))
                self.assertEqual(self.dev.prune(apply=True), 0)
                self.assertTrue(path.exists())

    def test_a_record_that_is_not_in_sync_with_the_hub_is_ignored(self) -> None:
        d = self.dev
        path = self.game()
        d.set_record_info("HUB:7", "HUB:6", len((d.root / RECORD).read_bytes()))
        self.assertEqual(d.prune(apply=True), 0)
        self.assertTrue(path.exists())
        self.assertNotIn("on purpose", self.out())

    def test_a_record_the_hub_no_longer_has_or_that_is_unknown_is_ignored(self) -> None:
        d = self.dev
        path = self.game()
        size = len((d.root / RECORD).read_bytes())
        base = {"version": ["H:1"]}
        for info in (
            None,
            {"global": {**base, "deleted": True}, "local": {**base, "size": size}},
            {"global": base, "local": {**base, "invalid": True, "size": size}},
            {"global": base, "local": base},
            {"global": base, "local": {**base, "size": True}},
            {"global": base, "local": {"size": size}},
        ):
            with self.subTest(info=info):
                if info is None:
                    del d.fake.file_info[("wz-library", RECORD)]
                else:
                    d.fake.file_info[("wz-library", RECORD)] = info
                self.assertEqual(d.prune(apply=True), 0)
                self.assertTrue(path.exists())

    def test_a_record_whose_size_on_disk_is_not_its_local_record_is_ignored(self) -> None:
        d = self.dev
        path = self.game()
        d.set_record_info("HUB:7", None, len((d.root / RECORD).read_bytes()) + 1)
        self.assertEqual(d.prune(apply=True), 0)
        self.assertTrue(path.exists())

    def test_a_record_that_is_a_link_is_not_read(self) -> None:
        d = self.dev
        path = self.game()
        real = d.home / "elsewhere.json"
        (d.root / RECORD).rename(real)
        (d.root / RECORD).symlink_to(real)
        self.assertEqual(d.prune(apply=True), 0)
        self.assertTrue(path.exists())

    def test_an_expired_entry_authorises_nothing(self) -> None:
        d = self.dev
        path = self.game()
        late = NOW + 200 * 86_400
        self.assertEqual(d.prune(apply=True, now=lambda: late), 0)
        self.assertTrue(path.exists())
        self.assertIn(f"{PATH} (the hub does not have it)", self.out())

    def test_only_the_library_reads_the_record(self) -> None:
        d = self.dev
        d.add_songs()
        d.put_record([record_entry([deleted_file("a/song.ogg")])])
        song = d.put_song("a/song.ogg", leftover=False)
        d.fake.file_info[("wz-songs", "a/song.ogg")] = trashed_info()
        self.assertEqual(d.prune(apply=True), 0)
        self.assertTrue(song.exists())
        asked = [p for _, p, _ in d.fake.requests if RECORD in p and "wz-songs" in p]
        self.assertEqual(asked, [])

    def test_a_symlink_at_a_recorded_path_is_kept(self) -> None:
        d = self.dev
        target = d.home / "precious.txt"
        target.write_text(CONTENT)
        link = d.root / PATH
        link.parent.mkdir(parents=True)
        link.symlink_to(target)
        d.fake.file_info[("wz-library", PATH)] = trashed_info()
        d.put_record([record_entry([deleted_file()])])
        self.assertEqual(d.prune(apply=True), 0)
        self.assertTrue(link.is_symlink())
        self.assertEqual(target.read_text(), CONTENT)

    def test_a_copy_edited_on_the_console_is_kept(self) -> None:
        d = self.dev
        path = self.game()
        os.utime(path, (MTIME + 10, MTIME + 10))
        self.assertEqual(d.prune(apply=True), 0)
        self.assertTrue(path.exists())
        path.write_text("edited")
        os.utime(path, (MTIME, MTIME))
        self.assertEqual(d.prune(apply=True), 0)
        self.assertTrue(path.exists())

    def test_a_live_hub_copy_is_still_rule_4a(self) -> None:
        # The record can list a path the hub has again, or reach the console before the hub's
        # own index does: the file is then judged as before.
        d = self.dev
        path = d.put(PATH, info=ignored_info(hub_copy()))
        d.put_record([record_entry([deleted_file()])])
        self.assertEqual(d.prune(), 0)
        self.assertIn("1 leftover file(s)", self.out())
        self.assertNotIn("on purpose", self.out())
        self.assertTrue(path.exists())

    def test_the_notify_payload_keeps_its_shape(self) -> None:
        self.game()
        with Receiver() as hook:
            self.assertEqual(self.dev.prune(apply=True, notify_url=hook.url), 0)
        expected = {"device": "deck", "folder": "wz-library", "files": 1, "bytes": 5}
        self.assertEqual(hook.bodies, [{**expected, "applied": True}])


class Sha1Tests(TrashedTestCase):
    def test_a_matching_sha1_is_deleted(self) -> None:
        path = self.game(sha1=SHA1)
        self.assertEqual(self.dev.prune(apply=True), 0)
        self.assertFalse(path.exists())

    def test_a_different_content_with_a_sha1_is_kept(self) -> None:
        path = self.game(sha1=OTHER_SHA1)
        self.assertEqual(self.dev.prune(apply=True), 1)
        self.assertTrue(path.exists())
        self.assertIn("deleted 0 file(s)", self.out())
        self.assertTrue(any("1 file(s) were not deleted" in w for w in self.dev.warnings))

    def test_no_sha1_means_no_hashing(self) -> None:
        path = self.game()
        with mock.patch.object(prune, "_sha1", side_effect=AssertionError("hashed")):
            self.assertEqual(self.dev.prune(apply=True), 0)
        self.assertFalse(path.exists())

    def test_the_report_never_hashes(self) -> None:
        path = self.game(sha1=SHA1)
        with mock.patch.object(prune, "_sha1", side_effect=AssertionError("hashed")):
            self.assertEqual(self.dev.prune(), 0)
        self.assertTrue(path.exists())
        self.assertIn("1 file(s) the hub deleted on purpose", self.out())

    def test_a_refused_run_hashes_nothing(self) -> None:
        d = self.dev
        path = self.game(sha1=SHA1)
        with mock.patch.object(prune, "_sha1", side_effect=AssertionError("hashed")):
            self.assertEqual(d.prune(apply=True, unattended=True), 1)  # all of what it holds
            self.assertEqual(d.prune(apply=True, max_gib=0), 1)
        self.assertTrue(path.exists())

    def test_one_entry_without_a_sha1_authorises_without_hashing(self) -> None:
        d = self.dev
        path = d.put(PATH, info=trashed_info())
        d.put_record(
            [
                record_entry([deleted_file(sha1=OTHER_SHA1)], name="20231114T231320Z-aaaa"),
                record_entry([deleted_file()], name="20231114T231321Z-bbbb"),
            ]
        )
        with mock.patch.object(prune, "_sha1", side_effect=AssertionError("hashed")):
            self.assertEqual(d.prune(apply=True), 0)
        self.assertFalse(path.exists())

    def test_some_entry_with_a_sha1_must_match(self) -> None:
        d = self.dev
        path = d.put(PATH, info=trashed_info())
        d.put_record(
            [
                record_entry([deleted_file(sha1=OTHER_SHA1)], name="20231114T231320Z-aaaa"),
                record_entry([deleted_file(sha1=SHA1)], name="20231114T231321Z-bbbb"),
            ]
        )
        self.assertEqual(d.prune(apply=True), 0)
        self.assertFalse(path.exists())
        path = d.put(PATH, "zzzzz", info=trashed_info(size=5))
        self.assertEqual(d.prune(apply=True), 1)
        self.assertTrue(path.exists())


class ApplyRecheckTrashedTests(TrashedTestCase):
    def test_a_record_that_changes_between_the_report_and_the_delete_stops_it(self) -> None:
        d = self.dev
        path = self.game()
        report = d.inspect()
        self.assertEqual(len(report.trashed), 1)
        # Syncthing delivers another version of the record.
        d.set_record_info("HUB:8", None, len((d.root / RECORD).read_bytes()))
        apply_folder(d.client, report)
        self.assertTrue(path.exists())
        self.assertEqual(report.deleted_files, 0)

    def test_a_record_rewritten_on_disk_stops_it(self) -> None:
        d = self.dev
        path = self.game()
        report = d.inspect()
        record = d.root / RECORD
        os.utime(record, (MTIME, MTIME))
        apply_folder(d.client, report)
        self.assertTrue(path.exists())

    def test_a_record_that_grew_on_disk_stops_it(self) -> None:
        d = self.dev
        path = self.game()
        report = d.inspect()
        record = d.root / RECORD
        record.write_bytes(record.read_bytes() + b" ")
        apply_folder(d.client, report)
        self.assertTrue(path.exists())

    def test_the_unchanged_record_deletes(self) -> None:
        d = self.dev
        path = self.game()
        report = d.inspect()
        apply_folder(d.client, report)
        self.assertFalse(path.exists())
        self.assertEqual((report.deleted_files, report.deleted_bytes), (1, 5))

    def test_a_hub_copy_that_came_back_stops_it(self) -> None:
        d = self.dev
        path = self.game()
        report = d.inspect()
        d.fake.file_info[("wz-library", PATH)] = ignored_info(hub_copy())
        apply_folder(d.client, report)
        self.assertTrue(path.exists())

    def test_a_file_that_became_wanted_or_invalid_stops_it(self) -> None:
        d = self.dev
        path = self.game()
        report = d.inspect()
        for info in (
            {"global": deleted_hub(), "local": {"ignored": False}},
            trashed_info(invalid=True),
            trashed_info(ignored=True),
            None,
        ):
            with self.subTest(info=info):
                if info is None:
                    del d.fake.file_info[("wz-library", PATH)]
                else:
                    d.fake.file_info[("wz-library", PATH)] = info
                apply_folder(d.client, report)
                self.assertTrue(path.exists())

    def test_a_file_edited_after_the_report_is_kept(self) -> None:
        d = self.dev
        path = self.game()
        report = d.inspect()
        path.write_text("edited meanwhile")
        apply_folder(d.client, report)
        self.assertTrue(path.exists())

    def test_a_file_replaced_by_a_link_after_the_report_is_kept(self) -> None:
        d = self.dev
        path = self.game()
        report = d.inspect()
        target = d.home / "precious.txt"
        target.write_text(CONTENT)
        path.unlink()
        path.symlink_to(target)
        apply_folder(d.client, report)
        self.assertTrue(path.is_symlink())
        self.assertEqual(target.read_text(), CONTENT)

    def test_a_file_that_changes_while_it_is_hashed_is_kept(self) -> None:
        d = self.dev
        path = self.game(sha1=SHA1)
        report = d.inspect()
        real = os.read
        calls: list[int] = []

        def read(fd: int, n: int) -> bytes:
            if not calls:
                calls.append(fd)
                with open(path, "ab") as f:
                    f.write(b"x")
            return real(fd, n)

        with mock.patch.object(prune.os, "read", read):
            apply_folder(d.client, report)
        self.assertEqual(len(calls), 1)
        self.assertTrue(path.exists())
        self.assertEqual(report.deleted_files, 0)

    def test_a_file_that_changes_after_it_was_hashed_is_kept(self) -> None:
        d = self.dev
        path = self.game(sha1=SHA1)
        report = d.inspect()
        real = prune.record_unchanged

        def changed(*args: Any) -> bool:
            os.utime(path, (MTIME + 60, MTIME + 60))
            return real(*args)

        with mock.patch.object(prune, "record_unchanged", changed):
            apply_folder(d.client, report)
        self.assertTrue(path.exists())

    def test_a_large_file_is_hashed_in_chunks(self) -> None:
        d = self.dev
        content = "abcdefghij" * 5
        path = self.game(content=content, sha1=hashlib.sha1(content.encode()).hexdigest())
        report = d.inspect()
        sizes: list[int] = []
        real = os.read

        def read(fd: int, n: int) -> bytes:
            sizes.append(n)
            return real(fd, n)

        with (
            mock.patch.object(prune, "HASH_CHUNK", 7),
            mock.patch.object(prune.os, "read", read),
        ):
            apply_folder(d.client, report)
        self.assertFalse(path.exists())
        self.assertEqual(set(sizes), {7})
        self.assertGreater(len(sizes), 5)


class GuardTrashedTests(TrashedTestCase):
    def test_the_guard_counts_these_bytes(self) -> None:
        d = self.dev
        path = self.game()
        d.put("roms/nes/mario.nes", "abcd", info=wanted_info(4))  # 5 of 9
        self.assertEqual(d.prune(apply=True, unattended=True), 1)
        self.assertTrue(path.exists())
        self.assertIn("wz-library would lose 5 B of the 9 B it holds", d.warnings[0])

    def test_exactly_half_is_deleted(self) -> None:
        d = self.dev
        path = self.game()
        d.put("roms/nes/mario.nes", "abcde", info=wanted_info(5))
        self.assertEqual(d.prune(apply=True, unattended=True), 0)
        self.assertFalse(path.exists())

    def test_leftovers_and_trashed_files_add_up(self) -> None:
        d = self.dev
        trashed = self.game()
        leftover = d.put("roms/nes/b.nes", "abc")
        d.put("roms/nes/mario.nes", "x" * 5, info=wanted_info(5))  # 8 of 13
        self.assertEqual(d.prune(apply=True, unattended=True), 1)
        self.assertTrue(trashed.exists())
        self.assertTrue(leftover.exists())
        self.assertIn("total: 2 leftover file(s), 8 B", self.out())

    def test_the_size_cap_counts_these_bytes(self) -> None:
        path = self.game()
        self.assertEqual(self.dev.prune(apply=True, max_gib=0), 1)
        self.assertTrue(path.exists())
        self.assertIn("refusing to delete", self.dev.warnings[0])
        self.assertEqual(self.dev.prune(apply=True, max_gib=0, force=True), 0)
        self.assertFalse(path.exists())


class NameThatIsNotUtf8Tests(TrashedTestCase):
    def test_a_report_with_a_lone_surrogate_prints(self) -> None:
        path = "roms/n64/\udcff.z64"
        report = FolderReport("wz-library", "/x")
        report.trashed.append(Entry(path, "file", 5, MTIME))
        report.leftovers.append(Entry(path + "2", "file", 5, MTIME))
        report.kept.append((path + "3", "the hub does not have it"))
        lines: list[str] = []
        for list_all in (False, True):
            lines.clear()
            print_report(report, Reporter(lines.append), list_all=list_all)
            for line in lines:
                line.encode("utf-8")  # a terminal or a journal can write it
        self.assertIn("  roms/n64/\\udcff.z64", lines)

    def test_such_a_name_is_not_asked_about(self) -> None:
        client = mock.Mock()
        self.assertIsNone(prune._db_file(client, "wz-library", "roms/\udcff"))
        client.db_file.assert_not_called()
        prune._db_file(client, "wz-library", "roms/ok")
        client.db_file.assert_called_once()

    def test_the_walk_keeps_such_a_name_out_of_the_deletions(self) -> None:
        d = self.dev
        self.game()
        found = Entry("roms/n64/\udcff.z64", "file", 5, MTIME)
        with mock.patch.object(prune, "walk", return_value=iter([found])):
            report = d.inspect()
        self.assertEqual((report.trashed, report.leftovers, report.untracked), ([], [], 1))
