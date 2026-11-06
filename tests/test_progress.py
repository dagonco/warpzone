import unittest
from datetime import UTC, datetime, timedelta

from warpzone import progress
from warpzone.progress import Reading

NOW = datetime(2026, 11, 6, 10, 0, 0, tzinfo=UTC)


def state(reading: Reading, wrote_at: datetime | None = None, rescan: int = 300):
    return progress.state(reading, wrote_at=wrote_at, rescan_seconds=rescan, now=NOW)


class StateTests(unittest.TestCase):
    def test_not_connected_is_offline_whatever_else_is_true(self) -> None:
        self.assertEqual(state(Reading(False, 50, 10, 1, "paused")).state, "offline")

    def test_not_shared_by_the_reading_or_by_the_remote_state(self) -> None:
        self.assertEqual(state(Reading(True, shared=False)).state, "not-shared")
        self.assertEqual(state(Reading(True, remote_state="notSharing")).state, "not-shared")

    def test_paused(self) -> None:
        self.assertEqual(state(Reading(True, 100, remote_state="paused")).state, "paused")

    def test_downloading_has_the_floor_of_the_completion(self) -> None:
        found = state(Reading(True, 42.9, 31_000_000_000, 812))
        self.assertEqual(
            (found.state, found.percent, found.need_bytes, found.need_items),
            ("downloading", 42, 31_000_000_000, 812),
        )

    def test_downloading_never_shows_100(self) -> None:
        self.assertEqual(state(Reading(True, 99.999, 10, 1)).percent, 99)

    def test_pending_while_the_whitelist_is_younger_than_rescan_plus_60_seconds(self) -> None:
        reading = Reading(True, 100)
        young = NOW - timedelta(seconds=359)
        old = NOW - timedelta(seconds=360)
        self.assertEqual(state(reading, young).state, "pending")
        self.assertEqual(state(reading, old).state, "done")
        self.assertEqual(state(reading, None).state, "done")

    def test_pending_waits_for_nothing_to_download(self) -> None:
        self.assertEqual(state(Reading(True, 40, 5, 1), NOW).state, "downloading")

    def test_done_at_completion_100(self) -> None:
        found = state(Reading(True, 100))
        self.assertEqual((found.state, found.percent), ("done", 100))

    def test_items_to_apply_without_bytes_are_not_done(self) -> None:
        found = state(Reading(True, 97.5, 0, 12))
        self.assertEqual((found.state, found.percent), ("downloading", 97))

    def test_a_write_in_the_future_does_not_count(self) -> None:
        self.assertEqual(state(Reading(True, 100), NOW + timedelta(hours=1)).state, "done")


class ParseTests(unittest.TestCase):
    def test_reads_the_hub_answers(self) -> None:
        reading = progress.parse(
            {"connected": True},
            {"completion": 42.5, "needBytes": 10, "needItems": 2, "remoteState": "valid"},
        )
        self.assertEqual(reading, Reading(True, 42.5, 10, 2, "valid"))

    def test_a_device_the_hub_does_not_list_is_not_connected(self) -> None:
        self.assertFalse(progress.parse(None, {"completion": 100}).connected)

    def test_no_completion_means_the_folder_is_not_shared(self) -> None:
        self.assertFalse(progress.parse({"connected": True}, None).shared)

    def test_odd_values_are_zero(self) -> None:
        reading = progress.parse(
            {"connected": True}, {"completion": "x", "needBytes": None, "remoteState": 3}
        )
        self.assertEqual(reading, Reading(True, 0.0, 0, 0, "valid"))


class WrittenAtTests(unittest.TestCase):
    def test_the_newest_backup_name(self) -> None:
        names = [
            "20261106T095000.000001Z.stignore",
            "20261106T100000.123456Z.stignore",
            "junk.txt",
            "20261105T230000.000000Z.stignore",
        ]
        self.assertEqual(
            progress.written_at(names), datetime(2026, 11, 6, 10, 0, 0, 123456, tzinfo=UTC)
        )

    def test_none_without_backups(self) -> None:
        self.assertIsNone(progress.written_at([]))


class EntryTests(unittest.TestCase):
    def test_shape(self) -> None:
        item = progress.entry("deck", progress.Progress("downloading", 42, 31, 812), NOW)
        self.assertEqual(
            item,
            {
                "device": "deck",
                "state": "downloading",
                "percent": 42,
                "need_bytes": 31,
                "need_items": 812,
                "checked": "2026-11-06T10:00:00Z",
            },
        )


if __name__ == "__main__":
    unittest.main()
