"""The pure decisions of the unattended inbox: no filesystem, no network, no clock."""

import json
import unittest
from datetime import UTC, datetime, timedelta

from warpzone import doctor_inbox, inbox_core
from warpzone.inbox_unattended import (
    DIED_REASON,
    GIB,
    LOOSE_REASON,
    MAX_ATTEMPTS,
    STABLE_SECONDS,
    Entry,
    Held,
    Listing,
    LockSeen,
    News,
    Observed,
    RunError,
    Stat,
    State,
    Verdict,
    decide,
    exit_code,
    fingerprint,
    heartbeat_due,
    loose_sheets,
    memory_fs,
    news,
    observe,
    parse_state,
    render_state,
    space_verdict,
    stale_work_dirs,
    update,
    with_attempt,
    with_checked,
    with_error,
    with_error_cleared,
    with_held,
    with_item_filed,
    with_judged,
    with_lock_seen,
)

T0 = datetime(2026, 11, 13, 10, 0, 0, tzinfo=UTC)


def at(seconds: float) -> datetime:
    return T0 + timedelta(seconds=seconds)


def walk() -> list[Stat]:
    return [
        Stat("d", 0, 100, "."),
        Stat("f", 1000, 200, "disc.cue"),
        Stat("f", 5000, 300, "disc.bin"),
    ]


def seen_at(fp: str, seconds: float, **kw: object) -> Entry:
    return Entry(fp, 1, 10, at(seconds), at(seconds), **kw)  # type: ignore[arg-type]


class FingerprintTests(unittest.TestCase):
    def test_the_order_of_the_listing_does_not_matter(self) -> None:
        self.assertEqual(fingerprint(walk()), fingerprint(reversed(walk())))

    def test_it_is_a_sha256(self) -> None:
        self.assertRegex(fingerprint(walk()), r"^sha256:[0-9a-f]{64}$")

    def test_a_size_changes_it(self) -> None:
        other = walk()
        other[2] = Stat("f", 5001, 300, "disc.bin")
        self.assertNotEqual(fingerprint(walk()), fingerprint(other))

    def test_an_mtime_changes_it_even_with_the_same_size(self) -> None:
        other = walk()
        other[2] = Stat("f", 5000, 301, "disc.bin")
        self.assertNotEqual(fingerprint(walk()), fingerprint(other))

    def test_a_new_file_changes_it(self) -> None:
        self.assertNotEqual(fingerprint(walk()), fingerprint([*walk(), Stat("f", 1, 1, "extra")]))

    def test_a_hidden_file_changes_it(self) -> None:
        hidden = Stat("f", 0, 1, ".copy.tmp")
        self.assertNotEqual(fingerprint(walk()), fingerprint([*walk(), hidden]))

    def test_a_link_is_its_own_line_and_not_a_file(self) -> None:
        as_link = [*walk(), Stat("l", 9, 5, "elsewhere")]
        as_file = [*walk(), Stat("f", 9, 5, "elsewhere")]
        self.assertNotEqual(fingerprint(as_link), fingerprint(as_file))
        self.assertEqual(observe(as_link).files, 2)

    def test_the_path_is_part_of_it(self) -> None:
        moved = [Stat(e.kind, e.size, e.mtime_ns, e.rel.replace("disc", "game")) for e in walk()]
        self.assertNotEqual(fingerprint(walk()), fingerprint(moved))

    def test_observe_counts_files_and_bytes(self) -> None:
        seen = observe(walk())
        self.assertEqual((seen.files, seen.bytes), (2, 6000))
        self.assertEqual(seen.fingerprint, fingerprint(walk()))


class DecideTests(unittest.TestCase):
    def setUp(self) -> None:
        self.obs = Observed("sha256:aaa", 2, 6000)

    def entry(self, seconds: float = 0, **kw: object) -> Entry:
        return Entry("sha256:aaa", 2, 6000, at(seconds), at(seconds), **kw)  # type: ignore[arg-type]

    def test_a_new_item_waits_and_is_recorded(self) -> None:
        verdict, entry = decide(None, self.obs, at(0))
        self.assertIs(verdict, Verdict.WAITING)
        self.assertEqual(entry, self.entry(0))

    def test_a_changed_item_waits_again_with_attempts_cleared(self) -> None:
        old = Entry("sha256:old", 1, 1, at(0), at(0), attempts=1)
        verdict, entry = decide(old, self.obs, at(1000))
        self.assertIs(verdict, Verdict.WAITING)
        self.assertEqual((entry.first, entry.seen, entry.attempts), (at(0), at(1000), 0))
        self.assertEqual(entry.fingerprint, "sha256:aaa")

    def test_a_stable_item_waits_until_300_seconds(self) -> None:
        before, _ = decide(self.entry(0), self.obs, at(STABLE_SECONDS - 1))
        exact, _ = decide(self.entry(0), self.obs, at(STABLE_SECONDS))
        after, kept = decide(self.entry(0), self.obs, at(STABLE_SECONDS + 600))
        self.assertEqual((before, exact, after), (Verdict.WAITING, Verdict.READY, Verdict.READY))
        self.assertEqual(kept, self.entry(0))

    def test_a_held_item_with_the_same_fingerprint_stays_held(self) -> None:
        held = self.entry(0, held=Held("because", at(10)))
        verdict, entry = decide(held, self.obs, at(99999))
        self.assertIs(verdict, Verdict.HELD)
        self.assertEqual(entry, held)

    def test_a_held_item_with_another_fingerprint_is_new_again(self) -> None:
        held = Entry("sha256:old", 1, 1, at(0), at(0), held=Held("because", at(10)))
        verdict, entry = decide(held, self.obs, at(5000))
        self.assertIs(verdict, Verdict.WAITING)
        self.assertIsNone(entry.held)
        self.assertEqual((entry.first, entry.seen), (at(5000), at(5000)))

    def test_one_attempt_left_behind_is_retried(self) -> None:
        verdict, entry = decide(self.entry(0, attempts=1), self.obs, at(900))
        self.assertIs(verdict, Verdict.READY)
        self.assertEqual(entry.attempts, 1)

    def test_two_attempts_hold_the_item_with_the_reason(self) -> None:
        verdict, entry = decide(self.entry(0, attempts=MAX_ATTEMPTS), self.obs, at(900))
        self.assertIs(verdict, Verdict.HELD)
        assert entry.held is not None
        self.assertEqual((entry.held.reason, entry.held.at), (DIED_REASON, at(900)))
        self.assertIn("twice", DIED_REASON)

    def test_a_clock_that_goes_back_never_makes_an_item_ready(self) -> None:
        verdict, entry = decide(self.entry(10_000), self.obs, at(0))
        self.assertIs(verdict, Verdict.WAITING)
        self.assertEqual((entry.first, entry.seen), (at(0), at(0)))
        later, _ = decide(entry, self.obs, at(STABLE_SECONDS))
        self.assertIs(later, Verdict.READY)

    def test_first_survives_changes_and_seen_does_not(self) -> None:
        _, e1 = decide(None, self.obs, at(0))
        _, e2 = decide(e1, Observed("sha256:bbb", 2, 7000), at(400))
        self.assertEqual((e2.first, e2.seen), (at(0), at(400)))

    def test_a_hold_reason_holds_an_item_only_once_it_is_stable(self) -> None:
        waiting, _ = decide(self.entry(0), self.obs, at(10), hold_reason=LOOSE_REASON)
        verdict, entry = decide(self.entry(0), self.obs, at(400), hold_reason=LOOSE_REASON)
        self.assertIs(waiting, Verdict.WAITING)
        self.assertIs(verdict, Verdict.HELD)
        assert entry.held is not None
        self.assertEqual(entry.held.reason, LOOSE_REASON)
        again, kept = decide(entry, self.obs, at(800), hold_reason=LOOSE_REASON)
        self.assertEqual((again, kept), (Verdict.HELD, entry))

    def test_a_name_that_is_gone_leaves_no_entry(self) -> None:
        state = State(items={"a": self.entry(0), "b": self.entry(0)})
        after = update(state, with_judged({"a": self.entry(5)}, ["a"]))
        self.assertEqual(set(after.items), {"a"})
        self.assertEqual(after.items["a"], self.entry(5))
        self.assertEqual(set(update(state, with_judged({}, [])).items), set())


class LooseSheetsTests(unittest.TestCase):
    def test_a_loose_cue_and_the_tracks_it_names(self) -> None:
        held = loose_sheets(
            ["Game.cue", "Game (Track 1).bin", "Game (Track 2).bin", "Other.bin"],
            {"Game.cue": ["Game (Track 1).bin", "Game (Track 2).bin"]},
        )
        self.assertEqual(set(held), {"Game.cue", "Game (Track 1).bin", "Game (Track 2).bin"})
        self.assertEqual(set(held.values()), {LOOSE_REASON})

    def test_a_gdi_and_its_tracks(self) -> None:
        held = loose_sheets(
            ["disc.gdi", "track01.bin", "track03.raw"],
            {"disc.gdi": ["track01.bin", "track03.raw"]},
        )
        self.assertEqual(set(held), {"disc.gdi", "track01.bin", "track03.raw"})

    def test_a_cue_naming_a_file_that_is_not_there_is_held_alone(self) -> None:
        held = loose_sheets(["Game.cue"], {"Game.cue": ["missing.bin"]})
        self.assertEqual(set(held), {"Game.cue"})

    def test_a_loose_bin_alone_is_left_alone(self) -> None:
        self.assertEqual(loose_sheets(["Game.bin", "Other.iso"], {}), {})

    def test_a_cue_inside_a_folder_is_not_loose(self) -> None:
        # a folder is one item: only the top-level names are given, and it is not a sheet
        self.assertEqual(loose_sheets(["Game"], {}), {})

    def test_names_are_matched_without_regard_to_case_and_separators(self) -> None:
        held = loose_sheets(
            ["GAME.CUE", "game.bin", "x.bin"],
            {"GAME.CUE": ["Game.BIN", "sub\\x.bin", "./nothing/../x.bin"]},
        )
        self.assertEqual(set(held), {"GAME.CUE", "game.bin"})


class StateFileTests(unittest.TestCase):
    def full(self) -> State:
        return State(
            checked=at(30),
            items={
                "Example Game (Europe).zip": Entry(
                    "sha256:abc", 1, 52428800, at(0), at(20), attempts=1
                ),
                "Game.cue": Entry("sha256:def", 2, 9, at(1), at(2), held=Held(LOOSE_REASON, at(3))),
            },
            lock_seen=LockSeen(1_800_000_000_123, at(40)),
            error=RunError("work_dir is on memory", 5, at(50), count=3, config=False),
        )

    def test_a_state_round_trips(self) -> None:
        state = self.full()
        parsed, warning = parse_state(render_state(state))
        self.assertIsNone(warning)
        self.assertEqual(parsed, state)

    def test_an_empty_state_round_trips(self) -> None:
        self.assertEqual(parse_state(render_state(State())), (State(), None))

    def test_the_text_is_json_with_the_version_and_the_new_fields(self) -> None:
        doc = json.loads(render_state(self.full()))
        entry = doc["items"]["Example Game (Europe).zip"]
        self.assertEqual(doc["v"], 1)
        self.assertEqual(entry["first"], "2026-11-13T10:00:00Z")
        self.assertEqual(entry["seen"], "2026-11-13T10:00:20Z")
        self.assertEqual(doc["error"]["step"], 5)

    def test_no_file_is_an_empty_state_without_a_warning(self) -> None:
        self.assertEqual(parse_state(None), (State(), None))

    def test_a_damaged_state_is_empty_with_a_warning(self) -> None:
        for text in ("", "{", "[]", "null", '{"v": 2}', '{"items": {}}', "\x00\x01"):
            with self.subTest(text=text):
                state, warning = parse_state(text)
                self.assertEqual(state, State())
                self.assertIsNotNone(warning)

    def test_a_damaged_entry_is_dropped_and_the_rest_kept(self) -> None:
        doc = json.loads(render_state(self.full()))
        doc["items"]["bad"] = {"fingerprint": 5}
        doc["items"]["worse"] = "text"
        doc["items"]["bad time"] = {**doc["items"]["Game.cue"], "seen": "yesterday"}
        doc["items"]["bad bytes"] = {**doc["items"]["Game.cue"], "bytes": True}
        doc["items"]["bad hold"] = {**doc["items"]["Game.cue"], "held": {"reason": 1}}
        state, warning = parse_state(json.dumps(doc))
        self.assertEqual(set(state.items), {"Example Game (Europe).zip", "Game.cue"})
        assert warning is not None
        self.assertIn("'bad'", warning)
        self.assertEqual(state.error, self.full().error)

    def test_a_damaged_error_checked_or_lock_note_is_dropped(self) -> None:
        doc = json.loads(render_state(self.full()))
        doc["error"], doc["checked"], doc["lock_seen"] = {"message": 1}, "x", [1]
        state, warning = parse_state(json.dumps(doc))
        self.assertEqual((state.error, state.checked, state.lock_seen), (None, None, None))
        assert warning is not None
        self.assertIn("error", warning)
        self.assertEqual(len(state.items), 2)

    def test_an_entry_without_first_takes_seen(self) -> None:
        doc = json.loads(render_state(self.full()))
        del doc["items"]["Game.cue"]["first"]
        state, _ = parse_state(json.dumps(doc))
        self.assertEqual(state.items["Game.cue"].first, at(2))


class ChangeTests(unittest.TestCase):
    """The state is updated by read-modify-write: a change is a pure function of the state."""

    def test_changes_apply_in_order_and_can_be_reapplied_to_another_state(self) -> None:
        changes = [with_checked(at(5)), with_error("boom", 1, at(5))]
        one = update(State(), *changes)
        other = update(State(items={"x": seen_at("sha256:x", 0)}), *changes)
        self.assertEqual((one.checked, one.error), (at(5), RunError("boom", 1, at(5))))
        self.assertEqual((other.checked, other.error), (one.checked, one.error))
        self.assertEqual(set(other.items), {"x"})
        self.assertEqual(update(State(), *changes), one)

    def test_a_change_never_mutates_the_state_it_is_given(self) -> None:
        state = State(items={"x": seen_at("sha256:x", 0)})
        update(state, with_attempt("x", "sha256:x"), with_item_filed("x"))
        self.assertEqual(state.items["x"].attempts, 0)
        self.assertEqual(set(state.items), {"x"})

    def test_an_attempt_counts_only_for_the_same_fingerprint(self) -> None:
        state = State(items={"x": seen_at("sha256:x", 0)})
        self.assertEqual(update(state, with_attempt("x", "sha256:x")).items["x"].attempts, 1)
        self.assertEqual(update(state, with_attempt("x", "sha256:other")), state)
        self.assertEqual(update(state, with_attempt("gone", "sha256:x")), state)

    def test_a_filed_item_loses_its_entry(self) -> None:
        state = State(items={"x": seen_at("sha256:x", 0), "y": seen_at("sha256:y", 0)})
        self.assertEqual(set(update(state, with_item_filed("x")).items), {"y"})

    def test_an_original_that_could_not_be_moved_is_held(self) -> None:
        state = State(items={"x": seen_at("sha256:x", 0, attempts=1)})
        held = update(state, with_held("x", "could not move the original: busy", at(9)))
        assert held.items["x"].held is not None
        self.assertEqual(held.items["x"].held.reason, "could not move the original: busy")
        self.assertEqual(held.items["x"].attempts, 1)
        again = update(held, with_held("x", "could not move the original: busy", at(99)))
        assert again.items["x"].held is not None
        self.assertEqual(again.items["x"].held.at, at(9))
        self.assertEqual(update(state, with_held("gone", "r", at(1))), state)

    def test_the_same_error_raises_the_count_and_keeps_the_first_time(self) -> None:
        state = update(State(), with_error("boom", 1, at(0)))
        state = update(state, with_error("boom", 1, at(900)))
        assert state.error is not None
        self.assertEqual((state.error.count, state.error.first), (2, at(0)))

    def test_another_message_replaces_the_error(self) -> None:
        state = update(State(), with_error("boom", 1, at(0)), with_error("bang", 6, at(9), False))
        self.assertEqual(state.error, RunError("bang", 6, at(9), 1, False))

    def test_an_error_keeps_its_step_until_a_run_gets_past_it(self) -> None:
        state = State(error=RunError("work folder", 5, at(0)))
        self.assertEqual(update(state, with_error_cleared(2)), state)  # an empty inbox
        self.assertEqual(update(state, with_error_cleared(4)), state)  # only waiting items
        self.assertEqual(update(state, with_error_cleared(5)), state)  # failed there again
        self.assertIsNone(update(state, with_error_cleared(6)).error)
        self.assertIsNone(update(state, with_error_cleared(8)).error)

    def test_an_empty_run_clears_only_a_step_one_error(self) -> None:
        one = State(error=RunError("no stfolder", 1, at(0)))
        two = State(error=RunError("dats", 5, at(0)))
        self.assertIsNone(update(one, with_error_cleared(2)).error)
        self.assertIsNotNone(update(two, with_error_cleared(2)).error)

    def test_no_error_stays_none_when_cleared(self) -> None:
        self.assertEqual(update(State(), with_error_cleared(8)), State())

    def test_the_lock_note_is_set_and_cleared(self) -> None:
        seen = LockSeen(5, at(1))
        state = update(State(), with_lock_seen(seen))
        self.assertEqual(state.lock_seen, seen)
        self.assertIsNone(update(state, with_lock_seen(None)).lock_seen)

    def test_the_heartbeat_is_due_hourly(self) -> None:
        self.assertTrue(heartbeat_due(State(), at(0)))
        state = State(checked=at(0))
        self.assertFalse(heartbeat_due(state, at(3599)))
        self.assertTrue(heartbeat_due(state, at(3600)))
        self.assertTrue(heartbeat_due(state, at(-60)))  # a clock that went back


class SpaceVerdictTests(unittest.TestCase):
    def test_the_threshold_is_the_doctors_and_lives_in_inbox_core(self) -> None:
        self.assertIs(doctor_inbox.MIN_FREE_GIB, inbox_core.MIN_FREE_GIB)
        self.assertEqual(inbox_core.MIN_FREE_GIB, 20)

    def test_exactly_enough_fits(self) -> None:
        need = 2 * 5 * GIB + 20 * GIB
        verdict = space_verdict(need, 5 * GIB)
        self.assertTrue(verdict.fits)
        self.assertEqual((verdict.needed, verdict.missing, verdict.reason), (need, 0, None))

    def test_one_byte_short_waits_and_says_how_much(self) -> None:
        need = 2 * 5 * GIB + 20 * GIB
        verdict = space_verdict(need - 1, 5 * GIB)
        self.assertFalse(verdict.fits)
        self.assertEqual(verdict.missing, 1)
        self.assertEqual(verdict.reason, "waiting for space: 1 GiB more needed")

    def test_the_shortfall_rounds_up_to_whole_gib(self) -> None:
        verdict = space_verdict(10 * GIB, 0)
        self.assertEqual(verdict.reason, "waiting for space: 10 GiB more needed")
        self.assertEqual(
            space_verdict(10 * GIB - 1, 0).reason, "waiting for space: 11 GiB more needed"
        )

    def test_an_empty_item_needs_only_the_reserve(self) -> None:
        self.assertTrue(space_verdict(20 * GIB, 0).fits)
        self.assertFalse(space_verdict(20 * GIB - 1, 0).fits)

    def test_no_free_space_at_all(self) -> None:
        self.assertEqual(space_verdict(0, 0).reason, "waiting for space: 20 GiB more needed")


MOUNTINFO = r"""22 1 0:21 / / rw,relatime - overlay overlay rw,lowerdir=/x
31 22 0:25 / /proc rw,nosuid - proc proc rw
40 22 0:30 / /tmp rw,nosuid - tmpfs tmpfs rw,size=65536k
41 40 8:1 /work /tmp/disk rw,relatime - ext4 /dev/sda1 rw
50 22 8:1 /volume1/games /games rw,relatime - btrfs /dev/vg1/volume_1 rw
51 50 0:31 / /games/ram\040disk rw - ramfs ramfs rw
"""


class MemoryFsTests(unittest.TestCase):
    def test_a_volume_is_not_memory(self) -> None:
        self.assertFalse(memory_fs(MOUNTINFO, "/games/work"))
        self.assertFalse(memory_fs(MOUNTINFO, "/games"))

    def test_tmpfs_is_memory_for_the_folder_and_what_is_under_it(self) -> None:
        self.assertTrue(memory_fs(MOUNTINFO, "/tmp"))
        self.assertTrue(memory_fs(MOUNTINFO, "/tmp/warpzone/work"))

    def test_a_nested_mount_wins_over_the_one_around_it(self) -> None:
        self.assertFalse(memory_fs(MOUNTINFO, "/tmp/disk/work"))
        self.assertTrue(memory_fs(MOUNTINFO, "/games/ram disk/work"))  # escaped space, ramfs

    def test_a_name_that_only_starts_like_a_mount_is_not_under_it(self) -> None:
        self.assertFalse(memory_fs(MOUNTINFO, "/tmpfiles/work"))

    def test_the_root_filesystem_covers_everything_else(self) -> None:
        self.assertFalse(memory_fs(MOUNTINFO, "/var/work"))
        self.assertTrue(memory_fs("1 0 0:1 / / rw - tmpfs tmpfs rw\n", "/anything"))

    def test_unreadable_text_or_a_relative_path_says_no(self) -> None:
        self.assertFalse(memory_fs("", "/tmp"))
        self.assertFalse(memory_fs("garbage\nno dash here\n", "/tmp"))
        self.assertFalse(memory_fs(MOUNTINFO, "tmp"))


class StaleWorkDirsTests(unittest.TestCase):
    NOW = 1_800_000_000.0

    def test_only_old_work_folders_are_chosen(self) -> None:
        day = 24 * 3600
        listing = [
            Listing("warpzone-inbox-old", self.NOW - day - 1, True, False),
            Listing("warpzone-inbox-edge", self.NOW - day, True, False),
            Listing("warpzone-inbox-new", self.NOW - 60, True, False),
            Listing("warpzone-inbox-future", self.NOW + 60, True, False),
            Listing("warpzone-inbox-a", self.NOW - 3 * day, True, False),
        ]
        self.assertEqual(
            stale_work_dirs(listing, self.NOW), ["warpzone-inbox-a", "warpzone-inbox-old"]
        )

    def test_a_link_a_file_and_other_names_are_never_chosen(self) -> None:
        old = self.NOW - 10 * 24 * 3600
        listing = [
            Listing("warpzone-inbox-link", old, True, True),
            Listing("warpzone-inbox-file", old, False, False),
            Listing("cache", old, True, False),
            Listing("my-warpzone-inbox-x", old, True, False),
        ]
        self.assertEqual(stale_work_dirs(listing, self.NOW), [])


class NewsTests(unittest.TestCase):
    def held(self, fp: str = "sha256:x") -> Entry:
        return Entry(fp, 1, 1, at(0), at(0), held=Held("why", at(5)))

    def test_a_quiet_run_has_no_news(self) -> None:
        self.assertEqual(news(State(), State()), News())
        self.assertFalse(news(State(), State()))
        self.assertEqual(exit_code(news(State(), State())), 0)

    def test_an_item_sent_to_review_is_news(self) -> None:
        found = news(State(), State(), ["some dump.7z"])
        self.assertEqual(found.review, ("some dump.7z",))
        self.assertEqual(exit_code(found), 1)

    def test_an_item_held_now_and_not_before_is_news(self) -> None:
        before = State(items={"a": seen_at("sha256:x", 0)})
        after = State(items={"a": self.held()})
        self.assertEqual(news(before, after).held, ("a",))
        self.assertEqual(news(State(), after).held, ("a",))
        self.assertEqual(exit_code(news(before, after)), 1)

    def test_an_item_held_before_with_the_same_fingerprint_is_not_news(self) -> None:
        state = State(items={"a": self.held()})
        self.assertFalse(news(state, state))
        self.assertEqual(exit_code(news(state, state)), 0)

    def test_an_item_held_before_with_another_fingerprint_is_news(self) -> None:
        before = State(items={"a": self.held("sha256:old")})
        after = State(items={"a": self.held("sha256:new")})
        self.assertEqual(news(before, after).held, ("a",))

    def test_an_item_whose_hold_was_cleared_and_held_again_is_news(self) -> None:
        held_once = State(items={"a": self.held()})
        cleared = State(items={"a": seen_at("sha256:y", 0)})
        self.assertFalse(news(held_once, cleared))
        self.assertEqual(news(cleared, State(items={"a": self.held("sha256:y")})).held, ("a",))

    def test_a_new_error_is_news_and_a_configuration_one_exits_2(self) -> None:
        after = State(error=RunError("no dats", 5, at(0)))
        found = news(State(), after)
        self.assertEqual(found.error, after.error)
        self.assertEqual(exit_code(found), 2)

    def test_a_runtime_error_exits_1(self) -> None:
        after = State(error=RunError("the lock was taken away", 7, at(0), config=False))
        self.assertEqual(exit_code(news(State(), after)), 1)

    def test_the_same_error_again_raises_the_count_and_is_not_news(self) -> None:
        first = update(State(), with_error("no dats", 5, at(0)))
        second = update(first, with_error("no dats", 5, at(900)))
        assert second.error is not None
        self.assertEqual(second.error.count, 2)
        self.assertFalse(news(first, second))
        self.assertEqual(exit_code(news(first, second)), 0)

    def test_a_changed_error_is_news(self) -> None:
        before = State(error=RunError("no dats", 5, at(0)))
        after = update(before, with_error("work folder", 5, at(900)))
        self.assertEqual(news(before, after).error, after.error)

    def test_an_error_cleared_and_back_is_news_again(self) -> None:
        err = State(error=RunError("no dats", 5, at(0)))
        cleared = update(err, with_error_cleared(8))
        self.assertFalse(news(err, cleared))
        back = update(cleared, with_error("no dats", 5, at(2000)))
        self.assertEqual(news(cleared, back).error, back.error)

    def test_a_configuration_error_wins_over_the_other_news(self) -> None:
        after = State(
            items={"a": self.held()},
            error=RunError("no dats", 5, at(0)),
        )
        found = news(State(), after, ["x.zip"])
        self.assertEqual((found.review, found.held), (("x.zip",), ("a",)))
        self.assertEqual(exit_code(found), 2)
        runtime = State(items={"a": self.held()}, error=RunError("gone", 7, at(0), config=False))
        self.assertEqual(exit_code(news(State(), runtime, ["x.zip"])), 1)

    def test_waiting_items_and_a_busy_note_are_no_news(self) -> None:
        after = State(
            checked=at(5),
            items={"w": seen_at("sha256:w", 0, attempts=1)},
            lock_seen=LockSeen(1, at(1)),
        )
        self.assertFalse(news(State(), after))


if __name__ == "__main__":
    unittest.main()
