"""The nightly scrape, the pure decisions: which games a night takes.

`plan_night` takes what `scrape.select` found for every system, the incomplete memory and the
not-found memory, and returns a `Night`: the games to scrape, in four tiers and under one cap
across systems. Nothing here reads a disk or a clock. See
development/design/scrape-unattended.md ("What gets picked each night").
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import date

from warpzone import scrape

# The kinds whose absence puts a game in tier 2; any other kind missing alone is tier 4.
CORE_KINDS: tuple[str, ...] = ("covers", "screenshots", "miximages")

# The tiers, in the order a night takes them.
NEW_GAME = 1  # no gamelist entry
MISSING_CORE = 2  # an entry, but a core kind is missing
NOT_FOUND_AGAIN = 3  # ScreenScraper did not know it, and its 30 days have passed
MISSING_EXTRA = 4  # an entry and every core kind; only `videos`, `backcovers`, ... missing


@dataclass(frozen=True)
class Ranked:
    """A pick with the system it belongs to and its tier."""

    tier: int
    system: str
    pick: scrape.Pick


@dataclass(frozen=True)
class Night:
    """What `plan_night` decided.

    `systems` are the systems with a chosen game, in name order (the order a run goes through
    them). `chosen` maps each to the primary paths (library paths, `roms/<system>/<name>`) of its
    chosen games. `deferred` counts, per system, the picks the cap left for another night;
    `backlog` is their total. `skipped_incomplete` counts the picks the incomplete memory
    covers: they are never chosen and do not use the cap."""

    systems: tuple[str, ...] = ()
    chosen: Mapping[str, frozenset[str]] = field(default_factory=dict)
    deferred: Mapping[str, int] = field(default_factory=dict)
    skipped_incomplete: int = 0
    backlog: int = 0

    @property
    def empty(self) -> bool:
        return not self.chosen

    @property
    def games(self) -> int:
        return sum(len(paths) for paths in self.chosen.values())

    def only(self) -> frozenset[str]:
        """The chosen games as one set of library paths, which is what `scrape_io.Options.only`
        takes."""
        return frozenset(path for paths in self.chosen.values() for path in paths)


def tier_of(pick: scrape.Pick, record: scrape.NotFound | None, today: date) -> int:
    """The tier of a pick. `record` is the not-found record of its game, if any: one with the
    same size whose `NOT_FOUND_DAYS` have passed puts the game in tier 3 (a game with no entry,
    which a not-found game never has, would otherwise be a new one)."""
    if (
        record is not None
        and record.size == pick.game.size
        and (today - record.tried).days >= scrape.NOT_FOUND_DAYS
    ):
        return NOT_FOUND_AGAIN
    if not pick.has_entry:
        return NEW_GAME
    if any(kind in CORE_KINDS for kind in pick.missing):
        return MISSING_CORE
    return MISSING_EXTRA


def rank_picks(
    selections: Mapping[str, scrape.Selection],
    not_found: Mapping[str, Mapping[str, scrape.NotFound]],
    *,
    today: date,
) -> tuple[Ranked, ...]:
    """Every pick of every system in the order a night takes them: by tier, then system, then
    primary path."""
    ranked = [
        Ranked(
            tier_of(pick, not_found.get(system, {}).get(pick.game.gamelist), today), system, pick
        )
        for system, selection in selections.items()
        for pick in selection.picks
    ]
    ranked.sort(key=lambda r: (r.tier, r.system, r.pick.game.primary))
    return tuple(ranked)


def plan_night(
    selections: Mapping[str, scrape.Selection],
    incomplete: Mapping[str, Mapping[str, scrape.Incomplete]],
    not_found: Mapping[str, Mapping[str, scrape.NotFound]],
    *,
    max_games: int,
    today: date,
) -> Night:
    """The games a night scrapes: the first `max_games` of `rank_picks`, once the picks the
    incomplete memory covers (`scrape.skip_incomplete`) are set aside and counted.

    `selections` are the result of `scrape.select` per system, `incomplete` and `not_found` the
    two memories by system and gamelist path."""
    skipped = 0
    kept: dict[str, scrape.Selection] = {}
    for system, selection in selections.items():
        records = incomplete.get(system, {})
        picks = tuple(
            p
            for p in selection.picks
            if not scrape.skip_incomplete(p, records.get(p.game.gamelist))
        )
        skipped += len(selection.picks) - len(picks)
        kept[system] = scrape.Selection(picks, selection.skipped_not_found)
    ranked = rank_picks(kept, not_found, today=today)
    taken, left = ranked[: max(0, max_games)], ranked[max(0, max_games) :]
    chosen: dict[str, set[str]] = {}
    for item in taken:
        chosen.setdefault(item.system, set()).add(item.pick.game.primary)
    deferred: dict[str, int] = {}
    for item in left:
        deferred[item.system] = deferred.get(item.system, 0) + 1
    return Night(
        tuple(sorted(chosen)),
        {system: frozenset(paths) for system, paths in sorted(chosen.items())},
        dict(sorted(deferred.items())),
        skipped,
        len(left),
    )


def incomplete_skipper(
    incomplete: Mapping[str, Mapping[str, scrape.Incomplete]],
) -> Callable[[scrape.Pick], bool]:
    """The `skip_incomplete` input of `scrape_io.run_held` for a night: the same rule as
    `plan_night`, applied to the picks the run selects again under the lock. A pick's system is
    the one in its primary path (`roms/<system>/...`)."""

    def skip(pick: scrape.Pick) -> bool:
        parts = pick.game.primary.split("/")
        system = parts[1] if len(parts) > 2 and parts[0] == "roms" else ""
        return scrape.skip_incomplete(pick, incomplete.get(system, {}).get(pick.game.gamelist))

    return skip
