"""Scraping game metadata and media with Skyscraper: the pure decisions.

`warpzone scrape` and `inbox --apply` run Skyscraper (ScreenScraper as the source) on a staged
copy of the games and merge what it produced into the hub's ES-DE gamelists and media. This
module holds the parts that need no filesystem and no subprocess: which systems are scraped
and under which Skyscraper platform (`PLATFORMS`, `UNSCRAPED`), which games need a scrape
(`select`), the command lines (`skyscraper_args`) and what Skyscraper's output says
(`classify_output`). See development/design/scrape.md.
"""

from __future__ import annotations

import posixpath
import re
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from enum import Enum
from pathlib import PurePath
from typing import Literal

from warpzone import artwork
from warpzone.catalog import DIR, Game

# The version the flags below were tested with.
SKYSCRAPER_VERSION = "3.21.1"

# Hub system folder -> Skyscraper platform name (checked against Skyscraper's `peas.json`).
PLATFORMS: dict[str, str] = {
    **{
        name: name
        for name in (
            "nes",
            "snes",
            "n64",
            "gb",
            "gbc",
            "gba",
            "nds",
            "mastersystem",
            "gamegear",
            "atari2600",
            "atari7800",
            "atarilynx",
            "wonderswan",
            "wonderswancolor",
            "sega32x",
            "atarijaguar",
            "psx",
            "ps2",
            "psp",
            "saturn",
            "dreamcast",
            "gc",
            "wii",
            "mame",
            "ps3",
            "xbox",
            "switch",
        )
    },
    "genesis": "megadrive",
    "n3ds": "3ds",
}

# Systems `inbox` places that are not scraped yet, with the reason `scrape --system` gives.
UNSCRAPED: dict[str, str] = {
    "ps4": "its gamelist entry is a launcher whose name ScreenScraper cannot match",
    "psvita": "its gamelist entry is a launcher whose name ScreenScraper cannot match",
    "xbox360": "its games live below roms/xbox360/roms/, so their gamelist paths need a base shift",
    "wiiu": "its games live below roms/wiiu/roms/, so their gamelist paths need a base shift",
}

# The hub's media kinds a scrape fills (`titlescreens` and `physicalmedia` are not available
# from Skyscraper's ScreenScraper module) and the extensions the library UI reads for each.
MEDIA_KINDS: tuple[str, ...] = (
    "covers",
    "screenshots",
    "miximages",
    "marquees",
    "3dboxes",
    "backcovers",
    "videos",
)
IMAGE_EXTENSIONS: tuple[str, ...] = ("png", "jpg", "jpeg")
VIDEO_EXTENSIONS: tuple[str, ...] = ("mp4", "webm")
KIND_EXTENSIONS: dict[str, tuple[str, ...]] = {
    kind: VIDEO_EXTENSIONS if kind == "videos" else IMAGE_EXTENSIONS for kind in MEDIA_KINDS
}

# A game ScreenScraper did not know is not asked for again for this many days (same size).
NOT_FOUND_DAYS = 30
# The ES-DE tag that keeps a game out of scraping.
NO_MULTISCRAPE = "nomultiscrape"
# Games per Skyscraper call.
BATCH_SIZE = 100

# ------------------------------------------------------------------ select


@dataclass(frozen=True)
class NotFound:
    """A record of `scrape-not-found.json`: the size the file had and the day it was tried."""

    size: int
    tried: date


@dataclass(frozen=True)
class Pick:
    """A game to scrape. `has_entry` is False when the hub gamelist has no entry for it;
    `missing` lists the media kinds (in `MEDIA_KINDS` order) the hub has no file of."""

    game: Game
    has_entry: bool
    missing: tuple[str, ...]


@dataclass(frozen=True)
class Selection:
    """What `select` decided: the games to scrape and how many it left out because ScreenScraper
    did not know them recently."""

    picks: tuple[Pick, ...]
    skipped_not_found: int = 0


def media_stem(game: Game) -> str:
    """The name the hub's media files of a game have, without kind folder and extension.

    It is the gamelist path without `./` and without its extension, except that a folder (an
    `.m3u` folder, a PS3 `.ps3` folder) keeps its whole name, as the library UI reads it.
    """
    rel = artwork.relative(game.gamelist) or game.gamelist
    primary = next((p for p in game.parts if p.path == game.primary), None)
    if primary is not None and primary.type == DIR:
        return rel
    return posixpath.splitext(rel)[0]


def _has_media(media: Collection[str], kind: str, stem: str) -> bool:
    return any(f"{kind}/{stem}.{ext}" in media for ext in KIND_EXTENSIONS[kind])


def _known_recently(
    game: Game, not_found: Mapping[str, NotFound], today: date, retry_not_found: bool
) -> bool:
    record = not_found.get(game.gamelist)
    if record is None or retry_not_found or record.size != game.size:
        return False
    return (today - record.tried).days < NOT_FOUND_DAYS


def select(
    games: Sequence[Game],
    entries: Sequence[artwork.Entry],
    media: Collection[str],
    *,
    not_found: Mapping[str, NotFound] | None = None,
    today: date,
    retry_not_found: bool = False,
) -> Selection:
    """The games of one system that a scrape should try, in the order given.

    `entries` are the hub gamelist's entries (`artwork.parse_gamelist`); `media` holds the
    paths of the hub's media files below the system's media folder in lower-case-extension
    form, `<kind>/<stem>.<ext>` with `/` (a file in another extension of the kind counts as
    present). A game is picked when it has no entry, or has one that is not marked
    `<nomultiscrape>` and lacks a file of a kind in `MEDIA_KINDS`. A game picked that
    ScreenScraper did not know less than `NOT_FOUND_DAYS` days ago, with the same size, is
    left out and counted, unless `retry_not_found`; `not_found` is keyed by gamelist path.
    """
    by_path: dict[str, artwork.Entry] = {}
    for item in entries:
        rel = artwork.relative(item.path)
        if rel is not None and item.tag in ("game", "folder"):
            by_path.setdefault(rel, item)
    records = not_found or {}
    picks: list[Pick] = []
    skipped = 0
    for game in games:
        entry = by_path.get(artwork.relative(game.gamelist) or "")
        if entry is not None and entry.fields.get(NO_MULTISCRAPE, "").lower() == "true":
            continue
        stem = media_stem(game)
        missing = tuple(k for k in MEDIA_KINDS if not _has_media(media, k, stem))
        if entry is not None and not missing:
            continue
        if _known_recently(game, records, today, retry_not_found):
            skipped += 1
            continue
        picks.append(Pick(game, entry is not None, missing))
    return Selection(tuple(picks), skipped)


# ------------------------------------------------------------------ command lines

GATHER = "gather"
MAIN = "main"
BOX = "box"
Step = Literal["gather", "main", "box"]

# The artwork files warpzone ships in `warpzone/data/skyscraper/`.
ARTWORK_ESDE = "artwork-esde.xml"
ARTWORK_3DBOX = "artwork-3dbox.xml"

_FLAGS: dict[str, tuple[str, ...]] = {
    GATHER: ("onlymissing", "videos", "backcovers", "unattend", "nohints"),
    MAIN: (
        "miximages",
        "videos",
        "backcovers",
        "theinfront",
        "nobrackets",
        "relative",
        "unattend",
        "nohints",
    ),
    BOX: ("theinfront", "nobrackets", "relative", "unattend", "nohints"),
}


def skyscraper_args(
    step: Step,
    platform: str,
    *,
    input_dir: str | PurePath,
    batch: str | PurePath,
    gamelist_dir: str | PurePath | None = None,
    media_dir: str | PurePath | None = None,
    artwork_file: str | PurePath | None = None,
) -> list[str]:
    """The arguments of one Skyscraper call (without the program), for `step`:

    - `gather`: ask ScreenScraper for the games of `batch` and fill Skyscraper's cache; it
      writes nothing else.
    - `main` and `box`: generate, from the cache and with no request, the ES-DE gamelist
      (`gamelist_dir`) and media (`media_dir`) of the batch with the artwork file
      `artwork_file` (`ARTWORK_ESDE` for `main`, `ARTWORK_3DBOX` for `box`).

    No credential option can be built here: warpzone never passes `-u`, `-c` or `-t`, so the
    account stays in Skyscraper's own `config.ini` (see "Credentials" in the design).
    """
    if step not in _FLAGS:
        raise ValueError(f"unknown step {step!r}")
    head = ["-p", platform]
    tail = ["--includefrom", str(batch), "--flags", ",".join(_FLAGS[step])]
    if step == GATHER:
        return [*head, "-s", "screenscraper", "-i", str(input_dir), *tail]
    if gamelist_dir is None or media_dir is None or artwork_file is None:
        raise ValueError(f"step {step!r} needs gamelist_dir, media_dir and artwork_file")
    return [
        *head,
        "-f",
        "esde",
        "-i",
        str(input_dir),
        "-g",
        str(gamelist_dir),
        "-o",
        str(media_dir),
        "-a",
        str(artwork_file),
        *tail,
    ]


# ------------------------------------------------------------------ Skyscraper's output


class Stop(Enum):
    """A condition that ends the gather for every system (see "Failures" in the design)."""

    QUOTA = "quota"  # ScreenScraper's daily request limit
    CLOSED = "closed"  # ScreenScraper's API is closed
    BLACKLISTED = "blacklisted"  # ScreenScraper refused Skyscraper
    BUSY = "busy"  # closed or too busy for anonymous users
    NETWORK = "network"  # ScreenScraper could not be reached


@dataclass(frozen=True)
class Outcome:
    """What a Skyscraper run said: the condition that stops the scrape, if any, and the names
    (staged file names without their last extension) of the games it reported as not found."""

    stop: Stop | None = None
    not_found: tuple[str, ...] = ()


# Skyscraper 3.21.1 (src/screenscraper.cpp, src/scraperworker.cpp), after dropping the colour codes.
_ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
_HARD_STOPS: tuple[tuple[Stop, str], ...] = (
    (Stop.QUOTA, "Your daily ScreenScraper request limit has been reached"),
    (Stop.BLACKLISTED, "Skyscraper has apparently been blacklisted at ScreenScraper"),
    (Stop.CLOSED, "The ScreenScraper API is currently closed"),
)
# These lines are printed on every try of a request, so they stop the run only when the output
# has no sign that ScreenScraper answered: no game found and none reported as not found.
_SOFT_STOPS: tuple[tuple[Stop, str], ...] = (
    (Stop.BUSY, "too busy to handle requests from unregistered and inactive users"),
    (Stop.NETWORK, "Retrying request with timeout of"),
)
_FOUND = re.compile(r"^-{4} Game '.*' found! :\) -{4}$")
_NOT_FOUND = re.compile(
    r"^-{4} Game '(?P<name>.*)' (?:not found|match \('.*', score: \d+, threshold: \d+\) too low)"
)


def classify_output(text: str) -> Outcome:
    """Classify the output of a Skyscraper run. Text that matches nothing known gives an
    `Outcome` with no stop and no games: an unknown output is not a failure."""
    lines = [_ANSI.sub("", line).strip() for line in text.splitlines()]
    joined = "\n".join(lines)
    stop: Stop | None = next((s for s, needle in _HARD_STOPS if needle in joined), None)
    names = tuple(m["name"] for line in lines if (m := _NOT_FOUND.match(line)))
    answered = bool(names) or any(_FOUND.match(line) for line in lines)
    if stop is None and not answered:
        stop = next((s for s, needle in _SOFT_STOPS if needle in joined), None)
    return Outcome(stop, names)
