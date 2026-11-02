"""Play state merge rules: favourites, play counts, last played, ledgers and gamelist edits."""

import itertools
import random
import unittest
from dataclasses import dataclass, field
from datetime import timedelta, timezone

from warpzone import artwork, playstate
from warpzone.playstate import Ledger, LedgerError, Record, Seen

NOW = 1_800_000_000
KEY = ("gba", "Hacks/Beta.gba")
OTHER = ("gba", "Alpha.zip")
CET = timezone(timedelta(hours=1))
EST = timezone(timedelta(hours=-5))


@dataclass
class Replica:
    """What a gamelist says for each game. Deck and Machine share one, as `wz-esde` does."""

    shown: dict = field(default_factory=dict)  # key -> (plays, favourite, last)
    included: dict | None = None  # the element in the file; None when the file has none
    keeps_element: bool = True  # False: the frontend rewrites the file and drops it
    open: bool = False


@dataclass
class Node:
    name: str
    replica_id: str
    replica: Replica
    ledger: Ledger = None
    roms: set | None = None  # None: every game is on this device
    view: dict = field(default_factory=dict)  # device -> the ledger it has received

    def __post_init__(self) -> None:
        self.ledger = Ledger(self.name, self.replica_id, {})


class Fleet:
    """Devices that run the plan, with explicit delivery of ledgers (the Syncthing folder)."""

    def __init__(self, *nodes: Node) -> None:
        self.nodes = {n.name: n for n in nodes}
        self.time = NOW

    def __getitem__(self, name: str) -> Node:
        return self.nodes[name]

    def deliver(self, source: str, target: str) -> None:
        self[target].view[source] = self[source].ledger

    def mesh(self) -> None:
        for a, b in itertools.permutations(self.nodes, 2):
            self.deliver(a, b)

    def run(self, name: str, *, clock: int | None = None) -> playstate.Plan:
        node = self[name]
        self.time += 60
        shared = node.replica
        included = dict(shared.included) if shared.included is not None else None
        seen = {
            key: Seen(
                plays,
                last,
                favourite,
                clock if clock is not None else self.time,
                None if included is None else included.get(key, 0),
            )
            for key, (plays, favourite, last) in shared.shown.items()
        }
        roms = node.roms

        def present(key: tuple[str, str]) -> bool:
            return key in seen or roms is None or key in roms

        plan = playstate.plan(
            node.ledger, list(node.view.values()), seen, present, horizon=self.time + 86400
        )
        if shared.open:
            node.ledger = plan.observed
            return plan
        node.ledger = plan.applied
        for key, want in plan.wanted.items():
            plays, favourite, last = shared.shown.get(key, (0, False, None))
            if want.plays is not None:
                plays = want.plays
            if want.favorite is not None:
                favourite = want.favorite
            if want.last is not None:
                last = want.last
            shared.shown[key] = (plays, favourite, last)
        if shared.keeps_element:
            shared.included = plan.included
        return plan

    def sync(self, rounds: int = 3) -> None:
        for _ in range(rounds):
            for name in self.nodes:
                self.run(name)
            self.mesh()


def fleet(**kw) -> Fleet:
    esde = Replica()
    return Fleet(
        Node("deck", "wz-esde", esde),
        Node("machine", "wz-esde", esde),
        Node("ark", "ark", Replica(keeps_element=False), **kw),
    )


def play(node: Node, key=KEY, times=1, at=NOW) -> None:
    plays, favourite, _ = node.replica.shown.get(key, (0, False, None))
    node.replica.shown[key] = (plays + times, favourite, at)


def favourite(node: Node, value: bool, key=KEY) -> None:
    plays, _, last = node.replica.shown.get(key, (0, False, None))
    node.replica.shown[key] = (plays, value, last)


class FavouriteTests(unittest.TestCase):
    def test_a_favourite_reaches_the_other_replica(self) -> None:
        f = fleet()
        favourite(f["ark"], True)
        f.sync()
        self.assertTrue(f["deck"].replica.shown[KEY][1])

    def test_an_unset_is_not_resurrected_by_a_device_that_still_shows_it(self) -> None:
        f = fleet()
        favourite(f["ark"], True)
        f.sync()
        favourite(f["deck"], False)
        f.run("deck")
        f.mesh()
        f.sync()
        self.assertFalse(f["deck"].replica.shown[KEY][1])
        self.assertFalse(f["ark"].replica.shown[KEY][1])

    def test_an_unset_on_the_other_side_also_travels(self) -> None:
        f = fleet()
        favourite(f["deck"], True)
        f.sync()
        favourite(f["ark"], False)
        f.sync()
        self.assertFalse(f["machine"].replica.shown[KEY][1])

    def test_last_change_wins_between_concurrent_changes(self) -> None:
        f = fleet()
        favourite(f["ark"], True)
        f.sync()
        favourite(f["ark"], False)
        f.run("ark", clock=NOW + 1000)
        favourite(f["deck"], False)
        favourite(f["deck"], True)  # no net change on the deck: nothing to record
        f.mesh()
        f.sync()
        self.assertFalse(f["deck"].replica.shown[KEY][1])

    def test_a_later_set_beats_an_earlier_unset_of_a_device_that_was_offline(self) -> None:
        f = fleet()
        favourite(f["ark"], True)
        f.sync()
        favourite(f["ark"], False)
        f.run("ark", clock=NOW + 100)  # unset, then the device is off for days
        favourite(f["deck"], False)
        f.run("deck", clock=NOW + 200)
        favourite(f["deck"], True)
        f.run("deck", clock=NOW + 90000)
        f.mesh()
        f.sync()
        self.assertTrue(f["ark"].replica.shown[KEY][1])

    def test_a_first_run_does_not_undo_a_recorded_unset(self) -> None:
        f = fleet()
        favourite(f["deck"], True)
        f.sync()
        favourite(f["deck"], False)
        f.sync()
        late = Node("late", "late", Replica(keeps_element=False))
        late.replica.shown[KEY] = (0, True, None)  # an old favourite, never recorded
        f.nodes["late"] = late
        f.mesh()
        f.sync()
        self.assertFalse(late.replica.shown[KEY][1])
        self.assertFalse(f["deck"].replica.shown[KEY][1])

    def test_first_runs_join_the_existing_favourites(self) -> None:
        f = fleet()
        favourite(f["deck"], True)
        favourite(f["ark"], True, OTHER)
        f.sync()
        self.assertTrue(f["deck"].replica.shown[OTHER][1])
        self.assertTrue(f["ark"].replica.shown[KEY][1])

    def test_a_device_with_a_slow_clock_still_overrides_what_it_has_seen(self) -> None:
        f = fleet()
        favourite(f["deck"], True)
        f.run("deck", clock=NOW + 10_000_000)
        f.mesh()
        f.sync()
        favourite(f["ark"], False)
        f.run("ark", clock=NOW)  # its clock says the past, but it saw the favourite first
        f.mesh()
        f.sync()
        self.assertFalse(f["deck"].replica.shown[KEY][1])

    def test_a_device_with_a_fast_clock_can_be_overridden_after_it_is_seen(self) -> None:
        f = fleet()
        favourite(f["ark"], True)
        f.run("ark", clock=NOW + 10**9)  # years ahead
        f.mesh()
        f.sync()
        favourite(f["deck"], False)
        f.run("deck")
        f.mesh()
        f.sync()
        self.assertFalse(f["ark"].replica.shown[KEY][1])

    def test_same_stamp_is_decided_the_same_everywhere(self) -> None:
        records = [
            Ledger("a", "a", {KEY: Record(fav=True, fav_at=50)}),
            Ledger("b", "b", {KEY: Record(fav=False, fav_at=50)}),
        ]
        one = playstate.merge(records, KEY, "a", horizon=NOW)
        two = playstate.merge(records[::-1], KEY, "b", horizon=NOW)
        self.assertEqual((one.fav, one.fav_at), (two.fav, two.fav_at))


class PlayCountTests(unittest.TestCase):
    def test_the_shown_value_is_the_sum_across_replicas(self) -> None:
        f = fleet()
        play(f["deck"], times=2)
        play(f["ark"], times=3)
        f.sync()
        self.assertEqual(f["deck"].replica.shown[KEY][0], 5)
        self.assertEqual(f["ark"].replica.shown[KEY][0], 5)

    def test_existing_counts_are_absorbed_once(self) -> None:
        f = fleet()
        f["deck"].replica.shown[KEY] = (7, False, None)
        f["ark"].replica.shown[KEY] = (2, False, None)
        f.sync()
        f.sync()
        self.assertEqual(f["deck"].replica.shown[KEY][0], 9)
        self.assertEqual(f["ark"].replica.shown[KEY][0], 9)

    def test_devices_that_share_a_gamelist_do_not_count_it_twice(self) -> None:
        f = fleet()
        play(f["deck"], times=4)
        f.run("deck")
        f.run("machine")
        f.mesh()
        f.sync()
        self.assertEqual(f["machine"].replica.shown[KEY][0], 4)
        self.assertEqual(f["ark"].replica.shown[KEY][0], 4)

    def test_repeated_runs_change_nothing(self) -> None:
        f = fleet()
        play(f["deck"], times=2)
        play(f["ark"], times=3)
        f.sync()
        ledgers = {n: f[n].ledger for n in f.nodes}
        shown = dict(f["deck"].replica.shown), dict(f["ark"].replica.shown)
        f.sync(5)
        self.assertEqual({n: f[n].ledger for n in f.nodes}, ledgers)
        self.assertEqual((f["deck"].replica.shown, f["ark"].replica.shown), shown)

    def test_new_plays_on_top_of_a_merged_count_count_once(self) -> None:
        f = fleet()
        play(f["deck"], times=2)
        play(f["ark"], times=3)
        f.sync()
        play(f["ark"])
        play(f["machine"], times=2)
        f.sync()
        self.assertEqual(f["deck"].replica.shown[KEY][0], 8)
        self.assertEqual(f["ark"].replica.shown[KEY][0], 8)

    def test_a_peers_gamelist_arriving_before_its_ledger_is_not_counted_as_plays(self) -> None:
        # The element travels inside the shared file, so nothing depends on which folder
        # Syncthing pulls first.
        f = fleet()
        play(f["deck"], times=6)
        f.sync()
        play(f["ark"], times=3)
        f.run("ark")
        f.deliver("ark", "machine")
        f.run("machine")  # writes 9 into the shared file; the deck has seen no new ledger
        f.run("deck")
        f.deliver("deck", "machine")
        f.deliver("deck", "ark")
        f.mesh()
        f.sync()
        self.assertEqual(f["ark"].replica.shown[KEY][0], 9)
        self.assertEqual(f["deck"].replica.shown[KEY][0], 9)

    def test_without_the_element_a_shared_count_is_read_with_the_peers_ledger(self) -> None:
        f = fleet()
        f["deck"].replica.keeps_element = False
        play(f["deck"], times=2)
        play(f["ark"], times=3)
        for _ in range(4):  # one device at a time: each has the other's ledger
            for name in f.nodes:
                f.run(name)
                f.mesh()
        self.assertEqual(f["deck"].replica.shown[KEY][0], 5)
        self.assertEqual(f["ark"].replica.shown[KEY][0], 5)

    def test_without_the_element_a_count_never_runs_away(self) -> None:
        # The worst case if a frontend drops the element and both devices run before their
        # ledgers cross: one overcount, then it stays.
        f = fleet()
        f["deck"].replica.keeps_element = False
        play(f["deck"], times=2)
        play(f["ark"], times=3)
        f.sync(4)
        settled = f["deck"].replica.shown[KEY][0]
        f.sync(8)
        self.assertEqual(f["deck"].replica.shown[KEY][0], settled)
        self.assertEqual(f["ark"].replica.shown[KEY][0], settled)

    def test_an_older_shared_gamelist_does_not_lose_counts(self) -> None:
        f = fleet()
        play(f["deck"], times=4)
        f.sync()
        f["deck"].replica.shown[KEY] = (1, False, NOW)  # an old copy won the file
        f.sync()
        self.assertEqual(f["deck"].replica.shown[KEY][0], 4)

    def test_a_device_offline_for_days_keeps_what_it_counted(self) -> None:
        f = fleet()
        play(f["deck"], times=2)
        f.sync()
        play(f["ark"], times=5)  # the console is off the network
        f.run("ark")
        for _ in range(4):
            play(f["deck"])
            f.run("deck")
            f.deliver("deck", "machine")
            f.run("machine")
            f.deliver("machine", "deck")
        self.assertEqual(f["deck"].replica.shown[KEY][0], 6)
        f.mesh()
        f.sync()
        self.assertEqual(f["deck"].replica.shown[KEY][0], 11)
        self.assertEqual(f["ark"].replica.shown[KEY][0], 11)

    def test_any_delivery_order_gives_the_same_result(self) -> None:
        for seed in range(30):
            with self.subTest(seed=seed):
                rng = random.Random(seed)
                f = fleet()
                play(f["deck"], times=2)
                play(f["ark"], times=3)
                favourite(f["ark"], True)
                pairs = list(itertools.permutations(f.nodes, 2))
                for _ in range(20):
                    action = rng.random()
                    if action < 0.5:
                        f.run(rng.choice(list(f.nodes)))
                    else:
                        f.deliver(*rng.choice(pairs))
                f.mesh()
                f.sync()
                self.assertEqual(f["deck"].replica.shown[KEY][:2], (5, True))
                self.assertEqual(f["ark"].replica.shown[KEY][:2], (5, True))

    def test_a_merge_does_not_depend_on_the_order_of_the_ledgers(self) -> None:
        ledgers = [
            Ledger("deck", "wz-esde", {KEY: Record(plays=3)}),
            Ledger("machine", "wz-esde", {KEY: Record(plays=5)}),
            Ledger("ark", "ark", {KEY: Record(plays=2)}),
            Ledger("other", "other", {KEY: Record(plays=1)}),
        ]
        results = {
            playstate.merge(order, KEY, "wz-esde", horizon=NOW).total
            for order in itertools.permutations(ledgers)
        }
        self.assertEqual(results, {8})


class FormatTests(unittest.TestCase):
    def test_a_frontend_with_other_tags_only_names_them(self) -> None:
        tags = {"favorite": "fav", "playcount": "count", "lastplayed": "seen"}
        entry = artwork.Entry("game", "./A.zip", {"fav": "true", "count": "4"})
        look = playstate.seen_games([entry], "gba", "", None, 1, CET, tags)[("gba", "A.zip")]
        self.assertEqual((look.plays, look.fav), (4, True))


class AbsentGameTests(unittest.TestCase):
    def test_state_for_a_game_the_device_lacks_is_kept_and_ignored(self) -> None:
        f = fleet(roms=set())
        play(f["deck"], times=2)
        favourite(f["deck"], True)
        f.sync()
        self.assertNotIn(KEY, f["ark"].replica.shown)
        self.assertEqual(f["deck"].replica.shown[KEY][:2], (2, True))

    def test_it_applies_once_the_game_arrives(self) -> None:
        f = fleet(roms=set())
        play(f["deck"], times=2)
        favourite(f["deck"], True)
        f.sync()
        f["ark"].roms = {KEY}
        f.sync()
        self.assertEqual(f["ark"].replica.shown[KEY][:2], (2, True))

    def test_a_removed_rom_does_not_lose_the_devices_counter(self) -> None:
        f = fleet()
        play(f["ark"], times=3)
        f.sync()
        del f["ark"].replica.shown[KEY]  # the generator left the game out
        f["ark"].roms = set()
        f.sync()
        self.assertEqual(f["deck"].replica.shown[KEY][0], 3)
        self.assertEqual(f["ark"].ledger.records[KEY].plays, 3)


class OpenFrontendTests(unittest.TestCase):
    def test_an_open_frontend_publishes_and_applies_nothing(self) -> None:
        f = fleet()
        play(f["ark"], times=2)
        f.sync()
        play(f["deck"], times=4)
        f["deck"].replica.open = True
        f.run("deck")
        f.mesh()
        self.assertEqual(f["deck"].replica.shown[KEY][0], 6)
        f.run("ark")
        self.assertEqual(f["ark"].replica.shown[KEY][0], 6)
        f.sync()
        self.assertEqual(f["ark"].replica.shown[KEY][0], 6)


class LastPlayedTests(unittest.TestCase):
    def test_the_latest_instant_wins(self) -> None:
        f = fleet()
        play(f["deck"], at=NOW)
        play(f["ark"], at=NOW + 50)
        f.sync()
        self.assertEqual(f["deck"].replica.shown[KEY][2], NOW + 50)
        self.assertEqual(f["ark"].replica.shown[KEY][2], NOW + 50)

    def test_an_earlier_time_never_replaces_a_later_one(self) -> None:
        f = fleet()
        play(f["deck"], at=NOW + 50)
        play(f["ark"], at=NOW)
        f.sync()
        self.assertEqual(f["ark"].replica.shown[KEY][2], NOW + 50)

    def test_a_time_far_in_the_future_is_not_spread(self) -> None:
        f = fleet()
        play(f["deck"], at=NOW)
        play(f["ark"], at=NOW + 10**9)
        f.sync()
        self.assertEqual(f["deck"].replica.shown[KEY][2], NOW)

    def test_zones_are_normalised(self) -> None:
        # The same instant, seen by a console on EST and one on CET.
        ark = playstate.parse_time("20261102T081034", EST)
        deck = playstate.parse_time("20261102T141034", CET)
        self.assertEqual(ark, deck)
        self.assertEqual(playstate.format_time(ark, CET), "20261102T141034")
        self.assertEqual(playstate.format_time(ark, EST), "20261102T081034")

    def test_values_that_are_no_date_are_no_time(self) -> None:
        for text in ("", "not-a-date-time", "20261302T000000", "yesterday"):
            with self.subTest(text=text):
                self.assertIsNone(playstate.parse_time(text, CET))


class LedgerTests(unittest.TestCase):
    def ledger(self) -> Ledger:
        return Ledger(
            "ark",
            "ark",
            {
                KEY: Record(plays=3, included=2, last=NOW, fav=False, fav_at=NOW + 1),
                OTHER: Record(plays=1),
                ("snes", "Ñandú & co.sfc"): Record(fav=True, fav_at=1),
            },
        )

    def test_it_round_trips(self) -> None:
        ledger = self.ledger()
        self.assertEqual(playstate.parse_ledger(playstate.render_ledger(ledger), "ark"), ledger)

    def test_the_text_is_stable_so_an_unchanged_ledger_is_not_resent(self) -> None:
        ledger = self.ledger()
        shuffled = Ledger("ark", "ark", dict(reversed(list(ledger.records.items()))))
        self.assertEqual(playstate.render_ledger(ledger), playstate.render_ledger(shuffled))

    def test_it_is_one_json_object_per_line_keyed_by_system_and_path(self) -> None:
        lines = playstate.render_ledger(self.ledger()).splitlines()
        self.assertEqual(len(lines), 4)
        self.assertIn('"system":"gba"', lines[2])
        self.assertIn('"path":"Hacks/Beta.gba"', lines[2])

    def test_a_broken_ledger_is_refused(self) -> None:
        good = playstate.render_ledger(self.ledger())
        for text in (
            "not json\n",
            good + "{broken\n",
            good + '{"system":"gba"}\n',
            good + '{"system":"gba","path":"../x","plays":1}\n',
            good + '{"system":"gba","path":"a","plays":-1}\n',
            good + '{"system":"gba","path":"a","plays":true}\n',
            good + '{"system":"a/b","path":"a"}\n',
            '{"v":99,"device":"ark","replica":"ark"}\n',
        ):
            with self.subTest(text=text[-40:]), self.assertRaises(LedgerError):
                playstate.parse_ledger(text, "ark")

    def test_an_empty_file_is_an_empty_ledger(self) -> None:
        with self.assertRaises(LedgerError):
            playstate.parse_ledger("", "ark")


class GamelistEditTests(unittest.TestCase):
    TEXT = (
        '<?xml version="1.0"?>\n'
        "<alternativeEmulator>\n\t<label>Core</label>\n</alternativeEmulator>\n"
        "<gameList>\n"
        "\t<game>\n"
        "\t\t<path>./A &amp; B.zip</path>\n"
        "\t\t<name>A</name>\n"
        "\t\t<desc>Line one\n\nline &lt;two&gt;</desc>\n"
        "\t\t<rating>0.8</rating>\n"
        "\t\t<favorite>true</favorite>\n"
        "\t\t<playcount>2</playcount>\n"
        "\t\t<lastplayed>20261029T112217</lastplayed>\n"
        "\t</game>\n"
        "\t<game>\n"
        "\t\t<path>./Keep.zip</path>\n"
        "\t\t<name>Keep</name>\n"
        "\t\t<altemulator>Other</altemulator>\n"
        "\t</game>\n"
        "\t<folder>\n"
        "\t\t<path>./Hacks</path>\n"
        "\t\t<name>Hacks</name>\n"
        "\t</folder>\n"
        "</gameList>\n"
    )

    def edit(self, text=None, changes=None, included=None):
        return playstate.edit_gamelist(
            self.TEXT if text is None else text,
            changes or {},
            {} if included is None else included,
            "gba",
            CET,
        )

    def test_only_the_changed_values_change(self) -> None:
        new = self.edit(changes={"A & B.zip": playstate.Wanted(plays=5)})
        self.assertEqual(new.count("<playcount>5</playcount>"), 1)
        self.assertEqual(
            new.replace("<playcount>5</playcount>", "<playcount>2</playcount>"), self.TEXT
        )

    def test_an_unset_removes_the_line(self) -> None:
        new = self.edit(changes={"A & B.zip": playstate.Wanted(favorite=False)})
        self.assertNotIn("<favorite>", new)
        self.assertIn("\t\t<rating>0.8</rating>\n\t\t<playcount>2</playcount>", new)

    def test_a_missing_tag_is_added_inside_the_game_with_its_indentation(self) -> None:
        new = self.edit(
            changes={
                "Keep.zip": playstate.Wanted(plays=3, favorite=True, last=NOW),
            }
        )
        block = new[new.index("./Keep.zip") : new.index("<folder>")]
        self.assertIn("\t\t<altemulator>Other</altemulator>\n\t\t<", block)
        entries = {e.path: e.fields for e in artwork.parse_gamelist(new)}
        self.assertEqual(entries["./Keep.zip"]["playcount"], "3")
        self.assertEqual(entries["./Keep.zip"]["favorite"], "true")
        self.assertEqual(entries["./Keep.zip"]["lastplayed"], playstate.format_time(NOW, CET))

    def test_a_game_the_file_lacks_is_added_and_the_others_stay(self) -> None:
        new = self.edit(changes={"New/Game.zip": playstate.Wanted(plays=1, favorite=True)})
        entries = {e.path: e for e in artwork.parse_gamelist(new)}
        self.assertEqual(entries["./New/Game.zip"].fields["playcount"], "1")
        self.assertEqual(len(entries), 4)

    def test_entries_for_absent_roms_stay(self) -> None:
        new = self.edit(changes={"A & B.zip": playstate.Wanted(plays=5)})
        self.assertIn("./Keep.zip", new)

    def test_the_foreign_plays_travel_in_the_file_and_are_read_back(self) -> None:
        new = self.edit(changes={"A & B.zip": playstate.Wanted(plays=5)}, included={"A & B.zip": 3})
        self.assertEqual(playstate.included_in(new, "gba"), {("gba", "A & B.zip"): 3})

    def test_a_file_without_the_element_says_so(self) -> None:
        self.assertIsNone(playstate.included_in(self.TEXT, "gba"))

    def test_an_existing_element_is_replaced_not_repeated(self) -> None:
        once = self.edit(included={"A & B.zip": 3})
        twice = self.edit(once, included={"A & B.zip": 4})
        self.assertEqual(twice.count("<warpzonePlayState>"), 1)
        self.assertEqual(playstate.included_in(twice, "gba"), {("gba", "A & B.zip"): 4})

    def test_the_element_is_no_game_for_the_readers(self) -> None:
        new = self.edit(included={"A & B.zip": 3})
        self.assertEqual(
            [e.path for e in artwork.parse_gamelist(new)],
            [e.path for e in artwork.parse_gamelist(self.TEXT)],
        )

    def test_crlf_is_kept(self) -> None:
        text = self.TEXT.replace("\n", "\r\n")
        new = self.edit(text, {"Keep.zip": playstate.Wanted(plays=3)})
        self.assertNotIn("\n", new.replace("\r\n", ""))

    def test_nothing_to_do_returns_the_same_text(self) -> None:
        self.assertEqual(self.edit(), self.TEXT)

    def test_a_file_with_no_gamelist_is_refused(self) -> None:
        with self.assertRaises(playstate.PlayStateError):
            self.edit("<other/>\n", {"Keep.zip": playstate.Wanted(plays=3)})

    def test_seen_reads_a_gamelist(self) -> None:
        entries = artwork.parse_gamelist(self.TEXT)
        seen = playstate.seen_games(entries, "gba", "", None, 123, CET)
        one = seen[("gba", "A & B.zip")]
        self.assertEqual((one.plays, one.fav, one.clock, one.included), (2, True, 123, None))
        self.assertEqual(one.last, playstate.parse_time("20261029T112217", CET))
        self.assertEqual(seen[("gba", "Keep.zip")].plays, 0)
        self.assertNotIn(("gba", "Hacks"), seen)  # folders hold no play state

    def test_seen_uses_the_system_base(self) -> None:
        entries = artwork.parse_gamelist(self.TEXT)
        seen = playstate.seen_games(entries, "gba", "carts/", {}, 1, CET)
        self.assertIn(("gba", "carts/Keep.zip"), seen)


if __name__ == "__main__":
    unittest.main()
