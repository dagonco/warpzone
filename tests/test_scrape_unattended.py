"""The nightly scrape's plan, the pure part: the tiers, the global cap, what the incomplete memory
and the not-found memory change. No filesystem, no Skyscraper."""

import unittest
from datetime import date, timedelta

from library_support import game
from warpzone import artwork, scrape, scrape_unattended
from warpzone.scrape_unattended import plan_night

TODAY = date(2026, 11, 13)
CORE = ("covers", "screenshots", "miximages")
EXTRA = ("videos", "backcovers", "3dboxes", "marquees")


def pick(system: str, name: str, missing=scrape.MEDIA_KINDS, *, has_entry=True, size=100):
    g = game(system, f"roms/{system}/{name}", name, size=size, gamelist=f"./{name}")
    return scrape.Pick(g, has_entry, tuple(missing))


def sel(*picks: scrape.Pick) -> scrape.Selection:
    return scrape.Selection(tuple(picks))


def night(selections, *, incomplete=None, not_found=None, max_games=100):
    return plan_night(
        selections, incomplete or {}, not_found or {}, max_games=max_games, today=TODAY
    )


def ranked(selections, not_found=None):
    """The titles of every pick in the order the plan would take them."""
    found = scrape_unattended.rank_picks(selections, not_found or {}, today=TODAY)
    return [r.pick.game.title for r in found]


class TierTest(unittest.TestCase):
    def test_the_four_tiers_in_order_across_systems(self):
        extra = pick("snes", "A extra.zip", EXTRA)
        core = pick("snes", "B core.zip", ("covers",))
        new = pick("snes", "C new.zip", has_entry=False)
        old_nf = pick("nes", "D lost.zip", has_entry=False)
        nf = {"nes": {"./D lost.zip": scrape.NotFound(100, TODAY - timedelta(days=31))}}
        plan = night({"snes": sel(extra, core, new), "nes": sel(old_nf)}, not_found=nf)
        self.assertEqual(
            ranked({"snes": sel(extra, core, new), "nes": sel(old_nf)}, nf),
            ["C new.zip", "B core.zip", "D lost.zip", "A extra.zip"],
        )
        found = scrape_unattended.rank_picks(
            {"snes": sel(extra, core, new), "nes": sel(old_nf)}, nf, today=TODAY
        )
        self.assertEqual([r.tier for r in found], [1, 2, 3, 4])
        self.assertEqual(plan.systems, ("nes", "snes"))

    def test_inside_a_tier_by_system_then_path(self):
        picks = {
            "snes": sel(
                pick("snes", "B.zip", has_entry=False), pick("snes", "A.zip", has_entry=False)
            ),
            "nes": sel(pick("nes", "Z.zip", has_entry=False)),
        }
        plan = night(picks, max_games=2)
        self.assertEqual(
            plan.chosen,
            {"nes": frozenset({"roms/nes/Z.zip"}), "snes": frozenset({"roms/snes/A.zip"})},
        )
        self.assertEqual(plan.systems, ("nes", "snes"))

    def test_any_core_kind_makes_a_game_tier_two_and_extras_only_tier_four(self):
        core = pick("snes", "Core.zip", ("miximages", "videos"))
        extra = pick("snes", "Extra.zip", ("marquees", "3dboxes"))
        plan = night({"snes": sel(extra, core)}, max_games=1)
        self.assertEqual(plan.chosen, {"snes": frozenset({"roms/snes/Core.zip"})})

    def test_the_cap_is_global_and_the_rest_is_deferred(self):
        picks = {
            "nes": sel(*(pick("nes", f"N{i}.zip", has_entry=False) for i in range(3))),
            "snes": sel(
                pick("snes", "S0.zip", has_entry=False),
                pick("snes", "S1.zip", EXTRA),
                pick("snes", "S2.zip", EXTRA),
            ),
        }
        plan = night(picks, max_games=5)
        self.assertEqual(sum(len(v) for v in plan.chosen.values()), 5)
        # Four new games, then one of the three extras-only games, the first by path.
        self.assertEqual(plan.chosen["nes"], frozenset(f"roms/nes/N{i}.zip" for i in range(3)))
        self.assertEqual(plan.chosen["snes"], frozenset({"roms/snes/S0.zip", "roms/snes/S1.zip"}))
        self.assertEqual(plan.deferred, {"snes": 1})
        self.assertEqual(plan.backlog, 1)
        self.assertEqual(plan.skipped_incomplete, 0)

    def test_a_system_with_nothing_chosen_is_not_listed(self):
        picks = {
            "nes": sel(pick("nes", "N.zip", EXTRA)),
            "snes": sel(pick("snes", "S.zip", has_entry=False)),
        }
        plan = night(picks, max_games=1)
        self.assertEqual(plan.systems, ("snes",))
        self.assertNotIn("nes", plan.chosen)
        self.assertEqual(plan.deferred, {"nes": 1})

    def test_the_cap_does_not_defer_when_everything_fits(self):
        plan = night({"snes": sel(pick("snes", "A.zip"))}, max_games=500)
        self.assertEqual((plan.deferred, plan.backlog), ({}, 0))

    def test_nothing_to_do(self):
        plan = night({"snes": sel(), "nes": sel()})
        self.assertEqual((plan.systems, plan.chosen, plan.deferred, plan.backlog), ((), {}, {}, 0))
        self.assertTrue(plan.empty)

    def test_a_zero_cap_chooses_nothing_and_defers_everything(self):
        plan = night({"snes": sel(pick("snes", "A.zip"))}, max_games=0)
        self.assertTrue(plan.empty)
        self.assertEqual(plan.backlog, 1)

    def test_only_flattens_the_chosen_games_to_library_paths(self):
        picks = {"nes": sel(pick("nes", "N.zip")), "snes": sel(pick("snes", "S.zip"))}
        self.assertEqual(night(picks).only(), frozenset({"roms/nes/N.zip", "roms/snes/S.zip"}))


class IncompleteTest(unittest.TestCase):
    def record(self, size=100, *missing):
        return scrape.Incomplete(size, missing, TODAY - timedelta(days=5))

    def test_a_pick_the_memory_covers_is_skipped_counted_and_never_chosen(self):
        a = pick("snes", "A.zip", ("videos", "backcovers"))
        b = pick("snes", "B.zip", ("videos",))
        inc = {"snes": {"./A.zip": self.record(100, "videos", "backcovers", "marquees")}}
        plan = night({"snes": sel(a, b)}, incomplete=inc)
        self.assertEqual(plan.chosen, {"snes": frozenset({"roms/snes/B.zip"})})
        self.assertEqual(plan.skipped_incomplete, 1)
        self.assertEqual((plan.deferred, plan.backlog), ({}, 0))

    def test_skipped_picks_do_not_use_the_cap(self):
        a = pick("snes", "A.zip", ("videos",))
        b = pick("snes", "B.zip", ("videos",))
        inc = {"snes": {"./A.zip": self.record(100, "videos")}}
        plan = night({"snes": sel(a, b)}, incomplete=inc, max_games=1)
        self.assertEqual(plan.chosen, {"snes": frozenset({"roms/snes/B.zip"})})
        self.assertEqual((plan.skipped_incomplete, plan.backlog), (1, 0))

    def test_a_new_missing_kind_picks_the_game_again(self):
        a = pick("snes", "A.zip", ("covers", "videos"))
        inc = {"snes": {"./A.zip": self.record(100, "videos")}}
        plan = night({"snes": sel(a)}, incomplete=inc)
        self.assertEqual(plan.chosen, {"snes": frozenset({"roms/snes/A.zip"})})
        self.assertEqual(plan.skipped_incomplete, 0)

    def test_a_size_change_picks_the_game_again(self):
        a = pick("snes", "A.zip", ("videos",), size=101)
        inc = {"snes": {"./A.zip": self.record(100, "videos")}}
        plan = night({"snes": sel(a)}, incomplete=inc)
        self.assertEqual(plan.chosen, {"snes": frozenset({"roms/snes/A.zip"})})

    def test_a_record_of_another_system_does_not_apply(self):
        a = pick("snes", "A.zip", ("videos",))
        inc = {"nes": {"./A.zip": self.record(100, "videos")}}
        self.assertEqual(night({"snes": sel(a)}, incomplete=inc).skipped_incomplete, 0)

    def test_the_skipper_for_a_run_uses_the_same_rule(self):
        inc = {"snes": {"./A.zip": self.record(100, "videos")}}
        skip = scrape_unattended.incomplete_skipper(inc)
        self.assertTrue(skip(pick("snes", "A.zip", ("videos",))))
        self.assertFalse(skip(pick("snes", "A.zip", ("covers", "videos"))))
        self.assertFalse(skip(pick("nes", "A.zip", ("videos",))))


class NotFoundTest(unittest.TestCase):
    """`select` lets a not-found game through after 30 days; the plan puts it in tier 3."""

    def nf(self, days: int, size: int = 100):
        return {"snes": {"./A.zip": scrape.NotFound(size, TODAY - timedelta(days=days))}}

    def games(self):
        return [game("snes", "roms/snes/A.zip", "A", size=100, gamelist="./A.zip")]

    def select(self, days: int):
        return scrape.select(self.games(), [], set(), not_found=self.nf(days)["snes"], today=TODAY)

    def test_at_29_days_select_leaves_the_game_out(self):
        selection = self.select(29)
        self.assertEqual(selection.picks, ())
        self.assertEqual(selection.skipped_not_found, 1)
        plan = night({"snes": selection}, not_found=self.nf(29))
        self.assertTrue(plan.empty)

    def test_at_31_days_it_comes_back_as_tier_three(self):
        selection = self.select(31)
        self.assertEqual(len(selection.picks), 1)
        extra = pick("snes", "B.zip", EXTRA)
        new = pick("snes", "C.zip", has_entry=False)
        both = scrape.Selection((*selection.picks, extra, new))
        plan = night({"snes": both}, not_found=self.nf(31))
        self.assertEqual(ranked({"snes": both}, self.nf(31)), ["C.zip", "A", "B.zip"])
        self.assertEqual(
            plan.chosen,
            {"snes": frozenset({"roms/snes/A.zip", "roms/snes/B.zip", "roms/snes/C.zip"})},
        )

    def test_exactly_30_days_counts(self):
        selection = self.select(30)
        self.assertEqual(len(selection.picks), 1)
        extra = pick("snes", "B.zip", EXTRA)
        both = scrape.Selection((*selection.picks, extra))
        self.assertEqual(ranked({"snes": both}, self.nf(30)), ["A", "B.zip"])

    def test_a_record_of_another_size_is_a_new_dump_and_a_new_game(self):
        a = pick("snes", "A.zip", has_entry=False, size=100)
        other = pick("snes", "B.zip", ("covers",))
        nf = {"snes": {"./A.zip": scrape.NotFound(99, TODAY - timedelta(days=90))}}
        self.assertEqual(ranked({"snes": sel(other, a)}, nf), ["A.zip", "B.zip"])  # tier 1, then 2

    def test_a_not_found_game_comes_after_a_game_missing_a_core_kind(self):
        a = pick("snes", "A.zip", has_entry=False)
        core = pick("snes", "Core.zip", ("covers",))
        self.assertEqual(ranked({"snes": sel(a, core)}, self.nf(40)), ["Core.zip", "A.zip"])


class AttendedSelectTest(unittest.TestCase):
    def test_select_does_not_know_the_incomplete_memory(self):
        g = game("snes", "roms/snes/A.zip", "A", size=100, gamelist="./A.zip")
        entries = [artwork.Entry("game", "./A.zip", {})]
        present = {f"{k}/A.png" for k in scrape.MEDIA_KINDS if k != "videos"}
        picks = scrape.select([g], entries, present, today=TODAY).picks
        self.assertEqual([p.missing for p in picks], [("videos",)])


if __name__ == "__main__":
    unittest.main()
