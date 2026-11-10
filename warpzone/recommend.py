"""The two recommendation shelves of the library UI's home page (pure).

"Unfinished" lists games the user started and left; "Because you play X" lists unplayed games
that look like one the user played recently. Both read the same flat list of `Candidate`s, which
`library_api` builds from the play-state ledgers, the cached RetroAchievements progress and the
gamelist metadata, so nothing here touches a file or the network. The rules are in
`development/design/library-ui.md` ("Recommendations").
"""

import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass

DAY = 86400
CAP = 12  # games per shelf
MIN_PLAYTIME = 600  # seconds of recorded play time that count as having started a game
STALE_DAYS = 14  # an unfinished game was last touched at least this long ago
RECENT_DAYS = 90  # "recently": the game a similarity shelf starts from was played this lately
FUTURE_SLACK = DAY  # a time further ahead than this comes from a wrong clock
NEAR_YEARS = 2  # release years this close are the same era (2 points), up to FAR_YEARS 1 point
FAR_YEARS = 5
GENRE_POINTS = 3
DEVELOPER_POINTS = 3

ON_CONSOLE, FITS, ELSEWHERE = 2, 1, 0  # `Candidate.tier`

_GENRE_SPLIT = re.compile(r"[,/;]")
_SPACES = re.compile(r"\s+")
_YEAR = re.compile(r"^\s*(\d{4})")


@dataclass(frozen=True)
class Candidate:
    """One catalog game with everything the shelves look at.

    `key` is the title key (the versions of a game across systems share it). `tier` is how
    available the game is: `ON_CONSOLE` (on or partly on a console), `FITS` (off everywhere but
    small enough for a console that accepts it) or `ELSEWHERE`. `last_played` and `ra_last` are
    UTC epochs or None. `ra_unlocked` and `ra_total` are the achievements unlocked and available
    (0 when RetroAchievements has none for it) and `ra_award` is the user's highest award for
    it, or "" for none."""

    id: str
    system: str
    title: str
    key: str = ""
    genres: tuple[str, ...] = ()
    developer: str = ""
    year: int | None = None
    plays: int = 0
    playtime: int = 0
    last_played: int | None = None
    ra_unlocked: int = 0
    ra_total: int = 0
    ra_last: int | None = None
    ra_award: str = ""
    tier: int = ELSEWHERE


@dataclass(frozen=True)
class Match:
    """A game that resembles the seed, with what they share: the genres (as the game spells
    them), whether the developer is the same, and its release year when it is within
    `FAR_YEARS` of the seed's."""

    game: Candidate
    score: int
    genres: tuple[str, ...]
    developer: bool
    year: int | None


@dataclass(frozen=True)
class Shelves:
    unfinished: tuple[Candidate, ...] = ()
    seed: Candidate | None = None
    similar: tuple[Match, ...] = ()


# ------------------------------------------------------------------ reading metadata


def parse_genres(raw: str | None) -> tuple[str, ...]:
    """The genres of a gamelist `<genre>` (split at commas, slashes and semicolons), trimmed,
    without repeats (compared ignoring case) and in the order written."""
    found: dict[str, str] = {}
    for part in _GENRE_SPLIT.split(raw or ""):
        name = _SPACES.sub(" ", part).strip()
        if name:
            found.setdefault(name.casefold(), name)
    return tuple(found.values())


def parse_year(raw: str | None) -> int | None:
    """The year of a gamelist `<releasedate>` (`19950101T000000`), or None when it does not
    start with a plausible one."""
    m = _YEAR.match(raw or "")
    year = int(m.group(1)) if m else 0
    return year if 1950 <= year <= 2100 else None


def developer_key(name: str) -> str:
    return _SPACES.sub(" ", name).strip().casefold()


# ------------------------------------------------------------------ what a game says


def activity(game: Candidate, now: int) -> int | None:
    """When the user last touched the game: the later of the last play and the last
    achievement unlock, ignoring times from a wrong clock. None when there is no date."""
    times = [
        t for t in (game.last_played, game.ra_last) if t is not None and t <= now + FUTURE_SLACK
    ]
    return max(times) if times else None


def has_activity(game: Candidate) -> bool:
    return game.plays > 0 or game.playtime > 0 or game.ra_unlocked > 0


def started(game: Candidate) -> bool:
    """Some play time, or some achievements. A play count alone does not count: a frontend that
    records no play time would make every launched game look started."""
    return game.playtime >= MIN_PLAYTIME or game.ra_unlocked > 0


def finished(game: Candidate) -> bool:
    """The user has an award for it (beaten, completed or mastered) or has every achievement."""
    return bool(game.ra_award) or (game.ra_total > 0 and game.ra_unlocked >= game.ra_total)


def _identity(game: Candidate) -> str:
    return game.key or game.id


def _pick(ordered: Iterable[Candidate], skip: frozenset[str], limit: int) -> tuple[Candidate, ...]:
    """The first `limit` games of `ordered` with a title key not in `skip`, one per key."""
    seen = set(skip)
    out: list[Candidate] = []
    for game in ordered:
        ident = _identity(game)
        if ident in seen:
            continue
        seen.add(ident)
        out.append(game)
        if len(out) == limit:
            break
    return tuple(out)


# ------------------------------------------------------------------ Unfinished


def unfinished(games: Sequence[Candidate], now: int, limit: int = CAP) -> tuple[Candidate, ...]:
    """Games started and not finished, last touched at least `STALE_DAYS` ago.

    A game qualifies when it was `started`, has a date (`activity`) at or before `now` minus
    `STALE_DAYS`, and no version of its title (any system) is `finished`. Order: available
    first (`tier`), then the most recently touched, then the most play time, then title and
    id. One game per title key, the best placed."""
    done = frozenset(_identity(g) for g in games if finished(g))
    cutoff = now - STALE_DAYS * DAY
    found: list[tuple[Candidate, int]] = []
    for game in games:
        seen = activity(game, now)
        if started(game) and seen is not None and seen <= cutoff:
            found.append((game, seen))
    found.sort(key=lambda item: (-item[0].tier, -item[1], -item[0].playtime, *_tail(item[0])))
    return _pick((g for g, _ in found), done, limit)


def _tail(game: Candidate) -> tuple[str, str, str]:
    return (game.title.casefold(), game.system, game.id)


# ------------------------------------------------------------------ Because you play X


def pick_seed(games: Sequence[Candidate], now: int) -> Candidate | None:
    """The game a similarity shelf starts from: the one touched most recently (within
    `RECENT_DAYS`) that has a genre or a developer to compare. Ties: most play time, then
    title and id."""
    cutoff = now - RECENT_DAYS * DAY
    found: list[tuple[Candidate, int]] = []
    for game in games:
        seen = activity(game, now)
        if seen is not None and seen >= cutoff and (game.genres or game.developer):
            found.append((game, seen))
    if not found:
        return None
    return min(found, key=lambda item: (-item[1], -item[0].playtime, *_tail(item[0])))[0]


def resemblance(seed: Candidate, other: Candidate) -> Match | None:
    """How much `other` looks like `seed`, or None when they share no genre and no developer.

    Each shared genre is worth `GENRE_POINTS`, the same developer `DEVELOPER_POINTS`, and a
    release year within `NEAR_YEARS` of the seed's 2 points (within `FAR_YEARS`, 1). The year
    only adds to a match, it never makes one."""
    wanted = {g.casefold() for g in seed.genres}
    shared = tuple(g for g in other.genres if g.casefold() in wanted)
    same = bool(seed.developer) and developer_key(seed.developer) == developer_key(other.developer)
    if not shared and not same:
        return None
    score = GENRE_POINTS * len(shared) + (DEVELOPER_POINTS if same else 0)
    year = None
    if seed.year is not None and other.year is not None:
        gap = abs(seed.year - other.year)
        if gap <= FAR_YEARS:
            year = other.year
            score += 2 if gap <= NEAR_YEARS else 1
    return Match(other, score, shared, same, year)


def similar(
    games: Sequence[Candidate],
    seed: Candidate,
    skip: frozenset[str] = frozenset(),
    limit: int = CAP,
) -> tuple[Match, ...]:
    """Games that resemble `seed`, best first.

    Left out: any game with a play, play time or achievement of its own, any title the user
    already has activity in (on any system), the seed's title, and the title keys in `skip`
    (the other shelf). Order: available first (`tier`), then the higher score, then title and
    id. One game per title key, the best placed."""
    touched = frozenset(_identity(g) for g in games if has_activity(g))
    excluded = touched | skip | {_identity(seed)}
    found = [m for g in games if _identity(g) not in excluded and (m := resemblance(seed, g))]
    found.sort(key=lambda m: (-m.game.tier, -m.score, *_tail(m.game)))
    by_id = {m.game.id: m for m in found}
    return tuple(by_id[g.id] for g in _pick((m.game for m in found), frozenset(), limit))


# ------------------------------------------------------------------ both shelves


def shelves(games: Sequence[Candidate], now: int, limit: int = CAP) -> Shelves:
    """The two shelves. `similar` is empty when no seed qualifies, and never repeats a title
    already in `unfinished`."""
    first = unfinished(games, now, limit)
    seed = pick_seed(games, now)
    if seed is None:
        return Shelves(first)
    skip = frozenset(_identity(g) for g in first)
    return Shelves(first, seed, similar(games, seed, skip, limit))
