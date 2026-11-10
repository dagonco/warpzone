"""The recommendation shelves: the rules are pure, so they are tested on plain candidates."""

import unittest
from dataclasses import replace

from warpzone.recommend import (
    CAP,
    DAY,
    ELSEWHERE,
    FITS,
    MIN_PLAYTIME,
    ON_CONSOLE,
    Candidate,
    activity,
    developer_key,
    finished,
    parse_genres,
    parse_year,
    pick_seed,
    resemblance,
    shelves,
    similar,
    started,
    unfinished,
)

NOW = 1_800_000_000


def ago(days: float) -> int:
    return NOW - int(days * DAY)


def cand(title: str, system: str = "snes", **kw: object) -> Candidate:
    """A candidate whose id and key come from its title and system."""
    kw.setdefault("key", title.casefold())
    return Candidate(id=f"{system}:{title}", system=system, title=title, **kw)  # type: ignore[arg-type]


def played(title: str, days: float = 30, playtime: int = 3600, **kw: object) -> Candidate:
    return cand(title, plays=1, playtime=playtime, last_played=ago(days), **kw)


def titles(found: object) -> list[str]:
    return [g.title for g in found]  # type: ignore[attr-defined]


class ParsingTests(unittest.TestCase):
    def test_genres_split_trim_and_dedupe_ignoring_case(self) -> None:
        self.assertEqual(
            parse_genres("Action / Platform, action;  Role  Playing"),
            (
                "Action",
                "Platform",
                "Role Playing",
            ),
        )
        self.assertEqual(parse_genres(""), ())
        self.assertEqual(parse_genres(None), ())
        self.assertEqual(parse_genres(" , / "), ())

    def test_year_is_the_first_four_digits_when_plausible(self) -> None:
        self.assertEqual(parse_year("19950101T000000"), 1995)
        self.assertEqual(parse_year("1995"), 1995)
        for raw in ("", None, "abc", "0000", "199", "19", "30000101T000000"):
            self.assertIsNone(parse_year(raw), raw)

    def test_developers_compare_without_case_or_extra_spaces(self) -> None:
        self.assertEqual(developer_key("  Nintendo   EAD "), developer_key("nintendo ead"))


class StateTests(unittest.TestCase):
    def test_started_needs_play_time_or_achievements(self) -> None:
        self.assertTrue(started(cand("a", playtime=MIN_PLAYTIME)))
        self.assertFalse(started(cand("a", playtime=MIN_PLAYTIME - 1)))
        self.assertFalse(started(cand("a", plays=5)))
        self.assertTrue(started(cand("a", ra_unlocked=1)))

    def test_finished_by_an_award_or_every_achievement(self) -> None:
        self.assertTrue(finished(cand("a", ra_award="beaten-softcore")))
        self.assertTrue(finished(cand("a", ra_award="mastered")))
        self.assertTrue(finished(cand("a", ra_unlocked=10, ra_total=10)))
        self.assertFalse(finished(cand("a", ra_unlocked=9, ra_total=10)))
        self.assertFalse(finished(cand("a", ra_unlocked=9, ra_total=0)))

    def test_activity_is_the_later_date_and_ignores_a_wrong_clock(self) -> None:
        self.assertEqual(activity(cand("a", last_played=10, ra_last=20), NOW), 20)
        self.assertEqual(activity(cand("a", last_played=10), NOW), 10)
        self.assertIsNone(activity(cand("a"), NOW))
        self.assertEqual(activity(cand("a", last_played=10, ra_last=NOW + 30 * DAY), NOW), 10)
        self.assertIsNone(activity(cand("a", ra_last=NOW + 30 * DAY), NOW))


class UnfinishedTests(unittest.TestCase):
    def test_qualifies_when_started_old_enough_and_not_finished(self) -> None:
        games = [
            played("old"),
            played("recent", days=13),
            played("short", playtime=60),
            cand("never"),
            played("beaten", ra_award="beaten-hardcore"),
        ]
        self.assertEqual(titles(unfinished(games, NOW)), ["old"])

    def test_exactly_the_stale_limit_counts(self) -> None:
        self.assertEqual(titles(unfinished([played("edge", days=14)], NOW)), ["edge"])
        self.assertEqual(unfinished([played("edge", days=13.9)], NOW), ())

    def test_achievements_alone_start_a_game_and_need_a_date(self) -> None:
        dated = cand("dated", ra_unlocked=3, ra_total=40, ra_last=ago(60))
        undated = cand("undated", ra_unlocked=3, ra_total=40)
        self.assertEqual(titles(unfinished([dated, undated], NOW)), ["dated"])

    def test_the_later_of_play_and_unlock_decides_how_old_it_is(self) -> None:
        game = played("mixed", days=90, ra_unlocked=2, ra_total=10, ra_last=ago(2))
        self.assertEqual(unfinished([game], NOW), ())

    def test_dates_from_a_wrong_clock_do_not_count(self) -> None:
        future = cand("future", playtime=3600, last_played=NOW + 40 * DAY)
        self.assertEqual(unfinished([future], NOW), ())

    def test_order_is_available_then_newest_then_most_played_then_title_then_id(self) -> None:
        games = [
            played("d", days=20, playtime=100_000, tier=ELSEWHERE),
            played("c", days=40, tier=FITS),
            played("b", days=30, tier=ON_CONSOLE),
            played("a", days=20, tier=ON_CONSOLE),
            played("e", days=20, playtime=5000, tier=ELSEWHERE),
        ]
        self.assertEqual(titles(unfinished(games, NOW)), ["a", "b", "c", "d", "e"])

    def test_ties_end_in_title_then_system_then_id(self) -> None:
        games = [
            played("same", system="snes", key="k1"),
            played("Same", system="gba", key="k2"),
            played("alpha", system="snes", key="k3"),
        ]
        self.assertEqual(
            [(g.title, g.system) for g in unfinished(games, NOW)],
            [("alpha", "snes"), ("Same", "gba"), ("same", "snes")],
        )

    def test_order_does_not_depend_on_the_input_order(self) -> None:
        games = [played(c, days=20 + i) for i, c in enumerate("abcdef")]
        self.assertEqual(unfinished(games, NOW), unfinished(games[::-1], NOW))

    def test_one_game_per_title_across_systems_the_best_placed(self) -> None:
        snes = played("zelda", system="snes", key="zelda", days=40)
        gba = played("zelda", system="gba", key="zelda", days=20)
        other = played("mario", key="mario", days=30)
        self.assertEqual(
            [(g.title, g.system) for g in unfinished([snes, other, gba], NOW)],
            [("zelda", "gba"), ("mario", "snes")],
        )

    def test_a_title_finished_on_any_system_is_left_out_everywhere(self) -> None:
        open_ = played("zelda", system="snes", key="zelda")
        done = cand("zelda", system="gba", key="zelda", ra_award="mastered")
        self.assertEqual(unfinished([open_, done], NOW), ())

    def test_a_game_without_a_key_dedupes_by_id(self) -> None:
        a = played("a", key="")
        b = played("b", key="")
        self.assertEqual(titles(unfinished([a, b], NOW)), ["a", "b"])

    def test_the_cap(self) -> None:
        games = [played(f"g{i:02d}", key=f"k{i:02d}") for i in range(CAP + 5)]
        found = unfinished(games, NOW)
        self.assertEqual(len(found), CAP)
        self.assertEqual(titles(found), [f"g{i:02d}" for i in range(CAP)])
        self.assertEqual(len(unfinished(games, NOW, 3)), 3)


def meta(title: str, genres: str = "", developer: str = "", year: int | None = None, **kw: object):
    return cand(title, genres=parse_genres(genres), developer=developer, year=year, **kw)


class SeedTests(unittest.TestCase):
    def test_the_most_recent_game_with_something_to_compare(self) -> None:
        games = [
            meta("old", "Platform", last_played=ago(50), plays=1),
            meta("new", "RPG", last_played=ago(5), plays=1),
            cand("bare", last_played=ago(1), plays=1),
        ]
        seed = pick_seed(games, NOW)
        self.assertIsNotNone(seed)
        self.assertEqual(seed.title, "new")  # type: ignore[union-attr]

    def test_an_achievement_date_counts_as_playing(self) -> None:
        games = [
            meta("a", "RPG", last_played=ago(20), plays=1),
            meta("b", "RPG", ra_last=ago(3), ra_unlocked=1),
        ]
        self.assertEqual(pick_seed(games, NOW).title, "b")  # type: ignore[union-attr]

    def test_nothing_when_not_recent_or_unplayed(self) -> None:
        self.assertIsNone(pick_seed([meta("a", "RPG", last_played=ago(91), plays=1)], NOW))
        self.assertIsNone(pick_seed([meta("a", "RPG")], NOW))
        self.assertIsNone(pick_seed([], NOW))
        self.assertIsNotNone(pick_seed([meta("a", "RPG", last_played=ago(90), plays=1)], NOW))

    def test_ties_go_by_play_time_then_title_then_id(self) -> None:
        a = meta("b", "RPG", last_played=ago(5), plays=1, playtime=10)
        b = meta("a", "RPG", last_played=ago(5), plays=1, playtime=10)
        c = meta("z", "RPG", last_played=ago(5), plays=1, playtime=99)
        self.assertEqual(pick_seed([a, b], NOW).title, "a")  # type: ignore[union-attr]
        self.assertEqual(pick_seed([a, b, c], NOW).title, "z")  # type: ignore[union-attr]
        self.assertEqual(pick_seed([b, a], NOW), pick_seed([a, b], NOW))


SEED = meta("seed", "Platform, Action", "Studio One", 1995, plays=1, last_played=ago(2))


class ResemblanceTests(unittest.TestCase):
    def test_points_for_genres_developer_and_era(self) -> None:
        both = resemblance(SEED, meta("x", "platform, Puzzle", "studio  one", 1996))
        assert both is not None
        self.assertEqual(
            (both.score, both.genres, both.developer, both.year),
            (3 + 3 + 2, ("platform",), True, 1996),
        )
        far = resemblance(SEED, meta("y", "Action", "", 1990))
        assert far is not None
        self.assertEqual((far.score, far.year), (3 + 1, 1990))
        out = resemblance(SEED, meta("z", "Action", "", 1980))
        assert out is not None
        self.assertEqual((out.score, out.year), (3, None))

    def test_two_shared_genres_beat_one(self) -> None:
        two = resemblance(SEED, meta("x", "Platform, Action"))
        one = resemblance(SEED, meta("y", "Platform"))
        assert two is not None and one is not None
        self.assertGreater(two.score, one.score)

    def test_year_alone_or_an_empty_developer_never_matches(self) -> None:
        self.assertIsNone(resemblance(SEED, meta("x", "Racing", "Other", 1995)))
        self.assertIsNone(resemblance(SEED, meta("x", "", "", 1995)))
        blank = replace(SEED, developer="")
        self.assertIsNone(resemblance(blank, meta("x", "Racing", "")))

    def test_a_missing_year_adds_nothing(self) -> None:
        match = resemblance(SEED, meta("x", "Action"))
        assert match is not None
        self.assertEqual((match.score, match.year), (3, None))


class SimilarTests(unittest.TestCase):
    def test_only_unplayed_games_that_share_a_genre_or_developer(self) -> None:
        games = [
            SEED,
            meta("genre", "Platform"),
            meta("dev", "", "Studio One"),
            meta("nothing", "Racing", "Other"),
            meta("touched", "Platform", plays=1),
            meta("unlocked", "Platform", ra_unlocked=1),
            meta("timed", "Platform", playtime=5),
        ]
        self.assertEqual(titles(m.game for m in similar(games, SEED)), ["dev", "genre"])

    def test_order_is_available_then_score_then_title(self) -> None:
        games = [
            meta("far", "Platform", tier=ON_CONSOLE),
            meta("best", "Platform, Action", "Studio One", 1995, tier=ELSEWHERE),
            meta("fit", "Platform", tier=FITS),
            meta("alpha", "Platform", tier=ELSEWHERE),
            meta("beta", "Platform", tier=ELSEWHERE),
            meta("two", "Platform, Action", tier=ON_CONSOLE),
        ]
        self.assertEqual(
            titles(m.game for m in similar(games, SEED)),
            ["two", "far", "fit", "best", "alpha", "beta"],
        )

    def test_a_title_the_user_touched_on_any_system_is_left_out(self) -> None:
        mine = played("zelda", system="gba", key="zelda")
        other = meta("zelda", "Platform", key="zelda", system="snes")
        self.assertEqual(similar([SEED, mine, other], SEED), ())

    def test_the_seed_title_on_other_systems_is_left_out(self) -> None:
        port = meta("seed", "Platform", key="seed", system="gba")
        self.assertEqual(similar([SEED, port], SEED), ())

    def test_one_game_per_title_across_systems_the_best_placed(self) -> None:
        a = meta("dup", "Platform", key="dup", system="snes", tier=ELSEWHERE)
        b = meta("dup", "Platform", key="dup", system="gba", tier=ON_CONSOLE)
        found = similar([SEED, a, b], SEED)
        self.assertEqual([m.game.system for m in found], ["gba"])

    def test_skipped_titles_are_left_out(self) -> None:
        a = meta("a", "Platform", key="a")
        b = meta("b", "Platform", key="b")
        self.assertEqual(titles(m.game for m in similar([a, b], SEED, frozenset({"a"}))), ["b"])

    def test_the_cap_and_independence_of_input_order(self) -> None:
        games = [meta(f"g{i:02d}", "Platform", key=f"k{i:02d}") for i in range(CAP + 4)]
        found = similar(games, SEED)
        self.assertEqual(len(found), CAP)
        self.assertEqual(found, similar(games[::-1], SEED))
        self.assertEqual(len(similar(games, SEED, limit=2)), 2)


class ShelvesTests(unittest.TestCase):
    def test_both_shelves(self) -> None:
        games = [
            SEED,
            played("left", days=40, key="left"),
            meta("next", "Action", key="next"),
        ]
        found = shelves(games, NOW)
        self.assertEqual(titles(found.unfinished), ["left"])
        self.assertEqual(found.seed, SEED)
        self.assertEqual(titles(m.game for m in found.similar), ["next"])

    def test_nothing_to_show(self) -> None:
        found = shelves([cand("a"), meta("b", "RPG")], NOW)
        self.assertEqual((found.unfinished, found.seed, found.similar), ((), None, ()))
        self.assertEqual(shelves([], NOW), found)

    def test_a_title_of_the_first_shelf_never_appears_in_the_second(self) -> None:
        # The seed is recent, so another version of the same title that sits unfinished is
        # skipped by the second shelf (the user already has a place for it).
        old_port = played("left", system="gba", key="left", days=40, genres=("Platform",))
        unplayed_port = meta("left", "Platform", key="left", system="snes")
        found = shelves([SEED, old_port, unplayed_port], NOW)
        self.assertEqual(titles(found.unfinished), ["left"])
        self.assertEqual(found.similar, ())

    def test_only_one_shelf_may_be_filled(self) -> None:
        found = shelves([played("left", days=40)], NOW)
        self.assertEqual(titles(found.unfinished), ["left"])
        self.assertIsNone(found.seed)
        self.assertEqual(found.similar, ())


if __name__ == "__main__":
    unittest.main()
