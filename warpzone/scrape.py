"""Scraping game metadata and media with Skyscraper: the pure decisions.

`warpzone scrape` and `inbox --apply` run Skyscraper (ScreenScraper as the source) on a staged
copy of the games and merge what it produced into the hub's ES-DE gamelists and media. This
module holds the parts that need no filesystem and no subprocess: which systems are scraped
and under which Skyscraper platform (`PLATFORMS`, `UNSCRAPED`), which games need a scrape
(`select`), the command lines (`skyscraper_args`), what Skyscraper's output says
(`classify_output`), what to merge into the hub (`plan_merge`) and what the run prints
(`render_report`, `stop_message`). See development/design/scrape.md.
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

from warpzone import artwork, gamelist
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


def _entries_by_path(entries: Sequence[artwork.Entry]) -> dict[str, artwork.Entry]:
    """The `<game>` and `<folder>` entries by their path below the system folder (`./` removed);
    the first one wins and a path that is not a plain relative path is left out."""
    by_path: dict[str, artwork.Entry] = {}
    for item in entries:
        rel = artwork.relative(item.path)
        if rel is not None and item.tag in ("game", "folder"):
            by_path.setdefault(rel, item)
    return by_path


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
    by_path = _entries_by_path(entries)
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

Step = Literal["gather", "main", "box"]
GATHER: Step = "gather"
MAIN: Step = "main"
BOX: Step = "box"

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


# ------------------------------------------------------------------ merge

# The text fields a new entry takes from Skyscraper's (the ones the library UI reads), in the
# order they are written.
ENTRY_FIELDS: tuple[str, ...] = (
    "name",
    "desc",
    "rating",
    "releasedate",
    "developer",
    "publisher",
    "genre",
    "players",
)

# Where Skyscraper leaves each hub kind: the pass (`MAIN` or `BOX`) and the folder of that
# pass's media folder (`3dboxes` are the `covers` of the 3D box pass).
MEDIA_SOURCES: dict[str, tuple[Step, str]] = {
    "covers": (MAIN, "covers"),
    "screenshots": (MAIN, "screenshots"),
    "miximages": (MAIN, "miximages"),
    "marquees": (MAIN, "marquees"),
    "3dboxes": (BOX, "covers"),
    "backcovers": (MAIN, "backcovers"),
    "videos": (MAIN, "videos"),
}

# What a listed file of Skyscraper's output is. Only a regular file is copied.
FileType = Literal["file", "symlink", "other"]

# Characters XML 1.0 cannot hold.
_ILLEGAL = re.compile("[\x00-\x08\x0b\x0c\x0e-\x1f\ud800-\udfff￾￿]")


class Result(Enum):
    """What a merge did for one game."""

    ADDED = "added"  # a new gamelist entry (and the media Skyscraper made)
    FILLED = "filled"  # no new entry, but media the hub lacked
    NOT_FOUND = "not found"  # Skyscraper produced nothing for the game
    UNCHANGED = "unchanged"  # nothing new to add (or `<nomultiscrape>`)


@dataclass(frozen=True)
class MediaCopy:
    """One file to copy from Skyscraper's output to the hub.

    `source` is below the media folder of the pass `step`; `dest` is below the hub's media
    folder of the system, `<kind>/<stem>.<ext>`."""

    kind: str
    step: Step
    source: str
    dest: str


@dataclass(frozen=True)
class Dropped:
    """Something the merge left out, and why (a warning for the report)."""

    what: str
    reason: str


@dataclass(frozen=True)
class GameMerge:
    """The plan for one game: the entry to add (None when the hub has one or Skyscraper gave
    none), the files to copy and the verdict."""

    game: Game
    result: Result
    had_entry: bool
    entry: artwork.Entry | None = None
    copies: tuple[MediaCopy, ...] = ()

    @property
    def kinds(self) -> tuple[str, ...]:
        return tuple(c.kind for c in self.copies)


@dataclass(frozen=True)
class MergePlan:
    """What `plan_merge` decided, for the writer to execute: media first, then the entries."""

    games: tuple[GameMerge, ...] = ()
    dropped: tuple[Dropped, ...] = ()

    @property
    def entries(self) -> tuple[artwork.Entry, ...]:
        return tuple(g.entry for g in self.games if g.entry is not None)

    @property
    def copies(self) -> tuple[MediaCopy, ...]:
        return tuple(c for g in self.games for c in g.copies)


def _clean(text: str) -> str:
    return _ILLEGAL.sub("", text)


def _staged_name(path: str, root: str) -> str | None:
    """A Skyscraper entry's `<path>` as a name below the staged folder, or None when it is
    outside it, climbs with `..` or is empty. `./Game.zip`, `Game.zip` and `<root>/Game.zip`
    are the same name."""
    p = path.strip().replace("\\", "/")
    if p.startswith("/"):
        prefix = root.rstrip("/") + "/"
        if not p.startswith(prefix):
            return None
        p = p[len(prefix) :]
    return artwork.relative(p)


def _new_entry(game: Game, source: artwork.Entry | None) -> artwork.Entry | None:
    if source is None:
        return None
    fields: dict[str, str] = {}
    for key in ENTRY_FIELDS:
        value = _clean(source.fields.get(key, "")).strip()
        if value:
            fields[key] = value
    return artwork.Entry("game", game.gamelist, fields)


def _plan_copies(
    game: Game,
    name: str,
    hub_media: Collection[str],
    main_files: Mapping[str, FileType],
    box_files: Mapping[str, FileType],
    claimed: set[str],
    dropped: list[Dropped],
) -> tuple[list[MediaCopy], bool]:
    """The copies for one game, and whether Skyscraper's output has any media for it."""
    stem = media_stem(game)
    source_stem = posixpath.splitext(name)[0]  # Skyscraper drops the last extension
    copies: list[MediaCopy] = []
    seen = False
    for kind in MEDIA_KINDS:
        step, folder = MEDIA_SOURCES[kind]
        files = main_files if step == MAIN else box_files
        have = _has_media(hub_media, kind, stem)
        for ext in KIND_EXTENSIONS[kind]:
            source = f"{folder}/{source_stem}.{ext}"
            file_type = files.get(source)
            if file_type is None:
                continue
            seen = True
            dest = f"{kind}/{stem}.{ext}"
            if have or dest in claimed:
                break
            if file_type == "symlink":
                dropped.append(
                    Dropped(
                        f"{step}/{source}",
                        "is a symbolic link (Skyscraper's `symlink` option); skipped",
                    )
                )
                continue
            if file_type != "file":
                dropped.append(Dropped(f"{step}/{source}", "is not a regular file; skipped"))
                continue
            claimed.add(dest)
            copies.append(MediaCopy(kind, step, source, dest))
            break
    return copies, seen


def plan_merge(
    staged: Mapping[str, Game],
    staged_root: str,
    hub_entries: Sequence[artwork.Entry],
    hub_media: Collection[str],
    skyscraper_entries: Sequence[artwork.Entry],
    main_files: Mapping[str, FileType],
    box_files: Mapping[str, FileType],
) -> MergePlan:
    """What to add to the hub for one batch of staged games. Pure: nothing is read or written.

    - `staged` maps a staged name (the `gamelist` path below the staged folder) to its game,
      for the games of the batch; `staged_root` is the staged folder, to read absolute paths.
    - `hub_entries` are the hub gamelist's (`artwork.parse_gamelist`); `hub_media` holds the
      hub's media paths as for `select`, `<kind>/<stem>.<ext>`.
    - `skyscraper_entries` are the entries of the main pass's gamelist.
    - `main_files` and `box_files` list the files of the two passes' media folders,
      `<folder>/<name>.<ext>` with `/`, with their type.

    It only adds: a game with an entry in the hub keeps it as it is and gets the media it lacks
    (none at all when marked `<nomultiscrape>`); a game without one gets a new entry with the
    fields in `ENTRY_FIELDS`, under the catalog's path. A media kind is never planned over a
    file the hub has, in any extension the UI reads.
    """
    dropped: list[Dropped] = []
    source: dict[str, artwork.Entry] = {}
    for item in skyscraper_entries:
        if item.tag not in ("game", "folder"):
            continue
        name = _staged_name(item.path, staged_root)
        if name is None:
            dropped.append(Dropped(item.path, "entry path is outside the staged folder; dropped"))
        elif name not in staged:
            dropped.append(Dropped(item.path, "entry is not a staged game; dropped"))
        else:
            source.setdefault(name, item)
    hub = _entries_by_path(hub_entries)
    claimed: set[str] = set()
    games: list[GameMerge] = []
    for name, game in staged.items():
        existing = hub.get(artwork.relative(game.gamelist) or "")
        if existing is not None and existing.fields.get(NO_MULTISCRAPE, "").lower() == "true":
            games.append(GameMerge(game, Result.UNCHANGED, True))
            continue
        copies, seen = _plan_copies(game, name, hub_media, main_files, box_files, claimed, dropped)
        entry = None if existing is not None else _new_entry(game, source.get(name))
        if entry is not None:
            result = Result.ADDED
        elif copies:
            result = Result.FILLED
        elif not seen and name not in source:
            result = Result.NOT_FOUND
        else:
            result = Result.UNCHANGED
        games.append(GameMerge(game, result, existing is not None, entry, tuple(copies)))
    return MergePlan(tuple(games), tuple(dropped))


def render_entries(entries: Sequence[artwork.Entry], *, indent: str = "\t", eol: str = "\n") -> str:
    """The text of new gamelist entries, to go before `</gameList>`: each one's `<path>` and
    its fields in `ENTRY_FIELDS` order, XML-escaped, with the characters XML 1.0 cannot hold
    dropped. `indent` is the file's own indentation of an entry and `eol` its line ending."""
    lines: list[str] = []
    for item in entries:
        lines.append(f"{indent}<{item.tag}>")
        lines.append(f"{indent * 2}<path>{gamelist.escape(_clean(item.path))}</path>")
        for key in ENTRY_FIELDS:
            if key in item.fields:
                text = gamelist.escape(_clean(item.fields[key]))
                lines.append(f"{indent * 2}<{key}>{text}</{key}>")
        lines.append(f"{indent}</{item.tag}>")
    return "".join(line + eol for line in lines)


# ------------------------------------------------------------------ report


@dataclass(frozen=True)
class Report:
    """One system's scrape, to print. `merges` are the games of every batch merged;
    `account` is whether `userCreds` is set in Skyscraper's config."""

    system: str
    merges: tuple[GameMerge, ...]
    dropped: tuple[Dropped, ...] = ()
    skipped_not_found: int = 0
    version: str | None = None
    account: bool = False


def _label(merge: GameMerge) -> str:
    return artwork.relative(merge.game.gamelist) or merge.game.gamelist


def render_report(report: Report) -> str:
    """The lines `scrape` prints for a system (no trailing newline). Games with an entry and
    nothing new are counted, not listed."""
    count = len(report.merges)
    tool = "Skyscraper" + (f" {report.version}" if report.version else "")
    account = "set" if report.account else "anonymous"
    noun = "game" if count == 1 else "games"
    lines = [f"scrape: {report.system}: {count} {noun} ({tool}, ScreenScraper account: {account})"]
    totals = {result: 0 for result in Result}
    for merge in report.merges:
        totals[merge.result] += 1
        if merge.result is Result.ADDED:
            lines.append(f"  {'added':<9}  {_label(merge)}: {', '.join(merge.kinds) or 'no media'}")
        elif merge.result is Result.FILLED:
            kept = " (entry kept)" if merge.had_entry else ""
            lines.append(f"  {'filled':<9}  {_label(merge)}: {', '.join(merge.kinds)}{kept}")
        elif merge.result is Result.NOT_FOUND:
            lines.append(f"  {'not found':<9}  {_label(merge)}")
    lines += [f"  {'warning':<9}  {d.what}: {d.reason}" for d in report.dropped]
    parts = [f"{totals[r]} {r.value}" for r in Result if totals[r]]
    if report.skipped_not_found:
        parts.append(
            f"skipped {report.skipped_not_found} not found in the last {NOT_FOUND_DAYS} days"
        )
    lines.append(f"scrape: {report.system}: {', '.join(parts) or 'nothing to do'}")
    return "\n".join(lines)


# The reasons a gather stops, besides the quota (see "Failures" in the design).
_STOP_REASONS: dict[Stop, str] = {
    Stop.CLOSED: "ScreenScraper's API is closed",
    Stop.BLACKLISTED: "ScreenScraper refused Skyscraper",
    Stop.BUSY: "ScreenScraper is closed or too busy for anonymous users",
    Stop.NETWORK: "ScreenScraper could not be reached",
}


def stop_message(stop: Stop, *, not_tried: int = 0, account: bool = False) -> str:
    """The line for a condition that stops the gather. `not_tried` is the games left;
    `account` is whether `userCreds` is already set, which makes the hint for a busy
    ScreenScraper pointless."""
    if stop is Stop.QUOTA:
        noun = "game" if not_tried == 1 else "games"
        return (
            "quota: ScreenScraper's daily request limit is reached; "
            f"{not_tried} {noun} not tried. Run warpzone scrape again tomorrow."
        )
    text = _STOP_REASONS[stop]
    if stop is Stop.BUSY and not account:
        text += "; add `userCreds` to `[screenscraper]` in Skyscraper's config.ini"
    return text


def failed_message(system: str, log: str) -> str:
    """The line for a Skyscraper run that failed, cannot run or hit the time limit."""
    return f"Skyscraper failed on {system} (see {log})"


def gamelist_message(system: str, reason: str) -> str:
    """The line for a hub gamelist that cannot be written (it changed three times during the
    write, or cannot be read): the entries wait for the next run."""
    return f"{system}: {reason}; the entries are left for the next run"
