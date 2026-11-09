"""RetroAchievements data of the library UI: the cached achievements of each game, the user's
progress, and the pure functions that merge them into the answers of the API.

Nothing here touches the disk or the network (`ra_store.py` does, `ra.py` parses what the web
API sends). See "Achievements and progress" in development/design/library-ui.md for the
cache files and the shape of the answers.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from warpzone import catalog, ra

FORMAT = 1
DETAIL_DAYS = 14  # a cached achievement list younger than this is not downloaded again
USER_HOURS = 5  # nor is the user's completion progress, when younger than this
TIME = "%Y-%m-%dT%H:%M:%SZ"
MASTERED = ("mastered", "completed")  # HighestAwardKind of a hardcore and a softcore mastery
GAME_URL = "https://retroachievements.org/game/"
USER_URL = "https://retroachievements.org/user/"


@dataclass(frozen=True)
class CachedGame:
    fetched: str  # UTC time, `TIME`
    detail: ra.RaGameDetail


@dataclass(frozen=True)
class UserSummary:
    """The completion progress of the user: one line per game the user has on RA."""

    user: str
    fetched: str
    games: Mapping[int, ra.UserGame]


@dataclass(frozen=True)
class CachedUnlocks:
    """When the user unlocked each achievement of one game, with the line of the summary the
    list was fetched for (a different line later means something new happened)."""

    user: str
    game_id: int
    fetched: str
    seen: ra.UserGame
    unlocks: Mapping[int, ra.Unlock]


@dataclass(frozen=True)
class RaData:
    """Everything cached about achievements, as the server holds it."""

    games: Mapping[int, CachedGame] = field(default_factory=dict)
    user: UserSummary | None = None
    unlocks: Mapping[int, CachedUnlocks] = field(default_factory=dict)


# ------------------------------------------------------------------ JSON of the cache files


def _dump(doc: dict[str, Any]) -> bytes:
    return (json.dumps(doc, sort_keys=True, ensure_ascii=False) + "\n").encode("utf-8")


def _load(data: bytes) -> dict[str, Any] | None:
    try:
        doc = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return None
    return doc if isinstance(doc, dict) and doc.get("format") == FORMAT else None


def _int(value: object) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def _str(value: object) -> str:
    return value if isinstance(value, str) else ""


def encode_game(detail: ra.RaGameDetail, fetched: str) -> bytes:
    return _dump(
        {
            "format": FORMAT,
            "game": detail.id,
            "fetched": fetched,
            "title": detail.title,
            "players": detail.players,
            "players_hardcore": detail.players_hardcore,
            "achievements": [
                {
                    "id": a.id,
                    "title": a.title,
                    "description": a.description,
                    "points": a.points,
                    "true_points": a.true_points,
                    "badge": a.badge,
                    "awarded": a.awarded,
                    "awarded_hardcore": a.awarded_hardcore,
                    "type": a.kind,
                    "order": a.order,
                }
                for a in detail.achievements
            ],
        }
    )


def decode_game(data: bytes) -> CachedGame | None:
    doc = _load(data)
    if doc is None or not _int(doc.get("game")) or not isinstance(doc.get("fetched"), str):
        return None
    found = []
    for item in doc.get("achievements") or []:
        if not isinstance(item, dict) or not _int(item.get("id")):
            continue
        kind = item.get("type")
        found.append(
            ra.RaAchievement(
                _int(item.get("id")),
                _str(item.get("title")) or "?",
                _str(item.get("description")),
                _int(item.get("points")),
                _str(item.get("badge")),
                _int(item.get("awarded")),
                _int(item.get("awarded_hardcore")),
                kind if isinstance(kind, str) and kind else None,
                _int(item.get("order")),
                _int(item.get("true_points")),
            )
        )
    detail = ra.RaGameDetail(
        _int(doc.get("game")),
        _str(doc.get("title")) or "?",
        _int(doc.get("players")),
        _int(doc.get("players_hardcore")),
        tuple(found),
    )
    return CachedGame(doc["fetched"], detail)


def _line_json(line: ra.UserGame) -> dict[str, Any]:
    return {
        "console": line.console_id,
        "max": line.max_possible,
        "awarded": line.awarded,
        "awarded_hardcore": line.awarded_hardcore,
        "last": line.last,
        "award": line.award,
        "award_date": line.award_date,
    }


def _line_from(game_id: int, item: object) -> ra.UserGame | None:
    if not isinstance(item, dict):
        return None
    return ra.UserGame(
        game_id,
        _int(item.get("console")),
        _int(item.get("max")),
        _int(item.get("awarded")),
        _int(item.get("awarded_hardcore")),
        _str(item.get("last")),
        _str(item.get("award")),
        _str(item.get("award_date")),
    )


def encode_user(user: str, fetched: str, games: Mapping[int, ra.UserGame]) -> bytes:
    return _dump(
        {
            "format": FORMAT,
            "user": user,
            "fetched": fetched,
            "games": {str(i): _line_json(g) for i, g in games.items()},
        }
    )


def decode_user(data: bytes) -> UserSummary | None:
    doc = _load(data)
    if doc is None or not isinstance(doc.get("user"), str):
        return None
    if not isinstance(doc.get("fetched"), str) or not isinstance(doc.get("games"), dict):
        return None
    games: dict[int, ra.UserGame] = {}
    for key, item in doc["games"].items():
        ident = int(key) if isinstance(key, str) and key.isdigit() else 0
        line = _line_from(ident, item) if ident else None
        if line is not None:
            games[ident] = line
    return UserSummary(doc["user"], doc["fetched"], games)


def encode_unlocks(
    user: str,
    game_id: int,
    fetched: str,
    seen: ra.UserGame,
    unlocks: Mapping[int, ra.Unlock],
) -> bytes:
    return _dump(
        {
            "format": FORMAT,
            "user": user,
            "game": game_id,
            "fetched": fetched,
            "seen": _line_json(seen),
            "unlocks": {str(i): {"soft": u.soft, "hard": u.hard} for i, u in unlocks.items()},
        }
    )


def decode_unlocks(data: bytes) -> CachedUnlocks | None:
    doc = _load(data)
    if doc is None or not isinstance(doc.get("user"), str) or not _int(doc.get("game")):
        return None
    seen = _line_from(_int(doc["game"]), doc.get("seen"))
    if seen is None or not isinstance(doc.get("fetched"), str):
        return None
    unlocks: dict[int, ra.Unlock] = {}
    raw = doc.get("unlocks")
    for key, item in (raw if isinstance(raw, dict) else {}).items():
        if isinstance(key, str) and key.isdigit() and isinstance(item, dict):
            soft, hard = item.get("soft"), item.get("hard")
            unlocks[int(key)] = ra.Unlock(
                soft if isinstance(soft, str) else None, hard if isinstance(hard, str) else None
            )
    return CachedUnlocks(doc["user"], _int(doc["game"]), doc["fetched"], seen, unlocks)


# ------------------------------------------------------------------ freshness


def fresh(fetched: str, now: datetime, age: timedelta) -> bool:
    """Whether something fetched at `fetched` is younger than `age`. A time that does not
    parse, or one in the future, counts as stale."""
    try:
        when = datetime.strptime(fetched, TIME).replace(tzinfo=UTC)
    except ValueError:
        return False
    return timedelta(0) <= now - when < age


def same_user(a: str | None, b: str | None) -> bool:
    """RA user names are not case sensitive."""
    return bool(a) and bool(b) and a.casefold() == b.casefold()  # type: ignore[union-attr]


def played(line: ra.UserGame) -> bool:
    return line.awarded > 0 or line.awarded_hardcore > 0


def unlocks_current(cached: CachedUnlocks | None, user: str, line: ra.UserGame) -> bool:
    """Whether the cached unlock dates are of this user and of the game as the summary says
    it is now (the counts and the date of the last unlock are the same)."""
    return (
        cached is not None
        and same_user(cached.user, user)
        and (cached.seen.awarded, cached.seen.awarded_hardcore, cached.seen.last)
        == (line.awarded, line.awarded_hardcore, line.last)
    )


# ------------------------------------------------------------------ what the library has


def ra_game(
    system: str, stored: Mapping[str, Any] | None, lists: Mapping[int, catalog.RaList]
) -> ra.RaGame | None:
    """RA's game for a library game, when its hash is in the cached list and has an id and
    achievements."""
    console = ra.CONSOLE_IDS.get(system)
    ra_list = lists.get(console) if console is not None else None
    ra_hash = stored.get("hash") if stored else None
    if ra_list is None or not isinstance(ra_hash, str):
        return None
    game = ra_list.games.get(ra_hash.lower())
    return game if game is not None and game.id and game.achievements else None


def library_games(
    systems: Mapping[str, Mapping[str, catalog.Game]], lists: Mapping[int, catalog.RaList]
) -> dict[str, dict[int, ra.RaGame]]:
    """system -> RA game id -> RA game, for the library games RA has achievements for."""
    out: dict[str, dict[int, ra.RaGame]] = {}
    for system, games in systems.items():
        for game in games.values():
            found = ra_game(system, game.ra, lists)
            if found is not None:
                out.setdefault(system, {})[found.id] = found
    return out


# ------------------------------------------------------------------ the answers


def percent(awarded: int, players: int) -> float:
    """The share of players that unlocked an achievement, in percent with one decimal."""
    if players <= 0 or awarded <= 0:
        return 0.0
    return round(min(awarded / players, 1.0) * 100, 1)


def _progress(data: RaData, user: str | None) -> UserSummary | None:
    mine = data.user
    return mine if mine is not None and same_user(mine.user, user) else None


def _unlocks_of(
    data: RaData, user: str | None, game_id: int, line: ra.UserGame | None
) -> CachedUnlocks | None:
    """The cached unlock dates of a game when they are usable: of this user, of the game as
    its progress line says it is now, and with the game's achievements downloaded (without
    them the dates have no points to add up)."""
    found = data.unlocks.get(game_id)
    if (
        found is None
        or line is None
        or user is None
        or game_id not in data.games
        or not unlocks_current(found, user, line)
    ):
        return None
    return found


def _award(line: ra.UserGame | None) -> dict[str, str] | None:
    if line is None or not line.award:
        return None
    return {"kind": line.award, "date": line.award_date}


def game_view(game: ra.RaGame, data: RaData, user: str | None) -> dict[str, Any]:
    """`achievements` of `GET /api/v1/games/{id}`, for a game RA has achievements for."""
    cached = data.games.get(game.id)
    detail = cached.detail if cached else None
    summary = _progress(data, user)
    line = summary.games.get(game.id) if summary else None
    mine = _unlocks_of(data, user, game.id, line)
    items = []
    unlocked = hard = soft_points = hard_points = 0
    for a in detail.achievements if detail else ():
        when = mine.unlocks.get(a.id) if mine else None
        if when is not None:
            unlocked += 1
            soft_points += a.points
            if when.hard:
                hard += 1
                hard_points += a.points
        items.append(
            {
                "id": a.id,
                "title": a.title,
                "description": a.description,
                "points": a.points,
                "true_points": a.true_points,
                "badge": a.badge,
                "type": a.kind,
                "order": a.order,
                "rarity": {
                    "players": a.awarded,
                    "hardcore_players": a.awarded_hardcore,
                    "percent": percent(a.awarded, detail.players if detail else 0),
                    "hardcore_percent": percent(
                        a.awarded_hardcore,
                        (detail.players_hardcore or detail.players) if detail else 0,
                    ),
                },
                "unlocked": {"softcore": when.soft, "hardcore": when.hard} if when else None,
            }
        )
    totals: dict[str, Any] = {
        "count": len(items) if detail else game.achievements,
        "points": sum(a.points for a in detail.achievements) if detail else game.points,
        "unlocked": None,
        "unlocked_hardcore": None,
        "points_unlocked": None,
        "points_unlocked_hardcore": None,
    }
    if mine is not None:
        totals.update(
            unlocked=unlocked,
            unlocked_hardcore=hard,
            points_unlocked=soft_points,
            points_unlocked_hardcore=hard_points,
        )
    elif summary is not None:  # the dates are not downloaded: the counts of the summary line
        totals.update(
            unlocked=max(line.awarded, line.awarded_hardcore) if line else 0,
            unlocked_hardcore=line.awarded_hardcore if line else 0,
        )
    return {
        "available": detail is not None,
        "fetched": cached.fetched if cached else None,
        "players": detail.players if detail else 0,
        "players_hardcore": detail.players_hardcore if detail else 0,
        "totals": totals,
        "progress": (
            {
                "user": summary.user,
                "fetched": summary.fetched,
                "dates": mine is not None,
                "award": _award(line),
                "profile": f"{USER_URL}{summary.user}",
            }
            if summary
            else None
        ),
        "list": items,
    }


_ROW_USER = (
    "played",
    "beaten",
    "mastered",
    "unlocked",
    "unlocked_hardcore",
    "points_unlocked",
    "points_unlocked_hardcore",
)


def _row(counts: dict[str, int], complete: bool, with_user: bool) -> dict[str, Any]:
    row: dict[str, Any] = {
        "games": counts["games"],
        "achievements": counts["achievements"],
        "points": counts["points"],
    }
    for name in _ROW_USER:
        row[name] = counts[name] if with_user else None
    row["points_complete"] = complete if with_user else None
    return row


def _empty() -> dict[str, int]:
    return dict.fromkeys(("games", "achievements", "points", *_ROW_USER), 0)


def summary_view(
    systems: Mapping[str, Mapping[str, catalog.Game]],
    lists: Mapping[int, catalog.RaList],
    data: RaData,
    user: str | None,
) -> dict[str, Any]:
    """`GET /api/v1/ra/summary`: the library's RA games and the user's progress in them, per
    system and in all. `played` games have an unlock; `beaten` and `mastered` are the games
    whose highest award is that (a mastered game is not also counted as beaten).
    `points_unlocked` is the sum over the games whose unlock dates are cached
    (`points_complete` says all the played ones are)."""
    summary = _progress(data, user)
    total, total_ids = _empty(), set[int]()
    total_complete = True
    rows = []
    for system, games in sorted(library_games(systems, lists).items()):
        counts, complete = _empty(), True
        for game_id, game in sorted(games.items()):
            first = game_id not in total_ids
            total_ids.add(game_id)
            targets = [counts, total] if first else [counts]
            for target in targets:
                target["games"] += 1
                target["achievements"] += game.achievements
                target["points"] += game.points
            line = summary.games.get(game_id) if summary else None
            if line is None:
                continue
            kind = line.award
            mine = _unlocks_of(data, user, game_id, line)
            cached = data.games.get(game_id)
            points = hard_points = 0
            if mine is not None and cached is not None:
                by_id = {a.id: a.points for a in cached.detail.achievements}
                for ident, when in mine.unlocks.items():
                    points += by_id.get(ident, 0)
                    hard_points += by_id.get(ident, 0) if when.hard else 0
            elif played(line):
                complete = False
                if first:
                    total_complete = False
            for target in targets:
                target["played"] += 1 if played(line) else 0
                target["beaten"] += 1 if kind.startswith("beaten") else 0
                target["mastered"] += 1 if kind in MASTERED else 0
                target["unlocked"] += max(line.awarded, line.awarded_hardcore)
                target["unlocked_hardcore"] += line.awarded_hardcore
                target["points_unlocked"] += points
                target["points_unlocked_hardcore"] += hard_points
        rows.append({"system": system, **_row(counts, complete, summary is not None)})
    return {
        "user": user or None,
        "fetched": summary.fetched if summary else None,
        "total": _row(total, total_complete, summary is not None),
        "systems": rows,
    }
