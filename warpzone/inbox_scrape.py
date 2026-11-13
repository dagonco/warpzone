"""The scrape at the end of `inbox`: what Skyscraper gives the games a run placed (or would place).

`inbox --apply` scrapes the games it placed in this run, after the catalog update, inside the lock
it already holds, through the same entry point as `warpzone scrape` (`scrape_io.run_placed`).
It never blocks or undoes a placement: a failure of any kind is a warning, and neither the
exit code nor the review list changes. Without `--apply` it only says what would be scraped, and
runs no Skyscraper. See development/design/scrape.md ("In `inbox --apply`").
"""

from __future__ import annotations

import os
from collections.abc import Sequence
from pathlib import Path

from warpzone import scrape, scrape_io
from warpzone.inbox_core import ItemResult, Options, ScrapeNote, UnitResult
from warpzone.report import Reporter

SKIPPED = "scrape: skipped (--no-scrape)"


def candidates(results: Sequence[ItemResult], outcome: str) -> dict[str, list[str]]:
    """The units with this outcome that a scrape can take, by system: the library path of each
    (`roms/<system>/<name>`). Pure. Extras (Switch updates and DLC) and systems warpzone does not
    scrape are left out."""
    found: dict[str, list[str]] = {}
    for result in results:
        for unit in result.units:
            if unit.outcome != outcome or not _scrapable(unit):
                continue
            found.setdefault(unit.catalog_system, []).append(f"{unit.library_folder}/{unit.entry}")
    return {system: sorted(set(paths)) for system, paths in sorted(found.items())}


def _scrapable(unit: UnitResult) -> bool:
    return unit.library_folder.startswith("roms/") and unit.catalog_system in scrape.PLATFORMS


def _games(count: int) -> str:
    return f"{count} game" if count == 1 else f"{count} games"


def report_only(opts: Options, rep: Reporter, results: Sequence[ItemResult]) -> None:
    """Without `--apply`: what would be scraped, and why not when it would not. No Skyscraper
    runs."""
    would = candidates(results, "planned")
    if not would:
        return
    if opts.no_scrape:
        rep.out(SKIPPED)
    elif opts.tools.skyscraper is None:
        total = sum(len(paths) for paths in would.values())
        rep.out(f"scrape: Skyscraper not found; {_games(total)} would not be scraped. {_HINT}")
    else:
        for system, paths in would.items():
            rep.out(f"scrape: {system}: would scrape {_games(len(paths))}")


_HINT = (
    "Put Skyscraper on PATH or set `skyscraper` in the [inbox] table, then run `warpzone scrape`"
)


def after_apply(opts: Options, rep: Reporter, results: Sequence[ItemResult]) -> ScrapeNote | None:
    """After the catalog update: scrape the games this run placed. Never raises (except for
    Ctrl-C) and never changes inbox's own result. Returns the note for the run record, or None
    when nothing scrapable was placed."""
    placed = candidates(results, "placed")
    if not placed:
        return None
    systems = tuple(placed)
    if opts.no_scrape:
        rep.out(SKIPPED)
        return ScrapeNote("skipped", systems, "--no-scrape")
    program = opts.tools.skyscraper
    if program is None:
        total = sum(len(paths) for paths in placed.values())
        rep.out(f"scrape: Skyscraper not found; {_games(total)} placed, not scraped. {_HINT}")
        return ScrapeNote("skipped", systems, "Skyscraper not found")
    options = scrape_io.Options(
        root=opts.root,
        skyscraper=program,
        systems=systems,
        explicit=False,
        timeout=opts.tools.timeout,
        work_dir=opts.work_dir,
        cache_dir=opts.scrape_cache_dir,
        config_ini=opts.skyscraper_config
        or scrape.config_ini_path(os.environ, Path.home(), Path.exists),
        only=frozenset(p for paths in placed.values() for p in paths),
        as_warning=True,
        now=opts.now,
        today=opts.today,
    )
    failure = ""
    try:
        code = scrape_io.run_placed(options, rep)
    except Exception as exc:  # the games are placed; a scrape can be run again
        failure = f"failed ({type(exc).__name__})"
        rep.warn(f"scrape: failed ({type(exc).__name__}: {exc})")
        code = 1
    if code:
        rep.out(f"scrape: not finished; run `warpzone scrape --system {' '.join(systems)}` later")
        return ScrapeNote("not-finished", systems, failure or "see the warnings of the run")
    return ScrapeNote("done", systems)
