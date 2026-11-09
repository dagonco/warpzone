"""RetroAchievements: hash an image the way RA does, and look the hash up.

RAHasher (from RALibretro, built with CHD support) prints the RA hash of a game
image. If a CHD gives the hash of the image it came from, the conversion kept what
RA identifies a game by. The web API is optional; its key is read from the
environment by the caller and is never printed, logged or put in an error.
"""

from __future__ import annotations

import http.client
import json
import re
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, TypeVar

from warpzone import __version__

# RetroAchievements console ids, by warpzone system.
CONSOLE_IDS: dict[str, int] = {
    "psx": 12,
    "ps2": 21,
    "saturn": 39,
    "dreamcast": 40,
    "psp": 41,
    "gc": 16,
    "nes": 7,
    "snes": 3,
    "genesis": 1,
    "n64": 2,
    "gb": 4,
    "gbc": 6,
    "gba": 5,
    "mastersystem": 11,
    "gamegear": 15,
}
API_URL = "https://retroachievements.org/API/API_GetGameList.php"
GAME_EXTENDED_URL = "https://retroachievements.org/API/API_GetGameExtended.php"
COMPLETION_URL = "https://retroachievements.org/API/API_GetUserCompletionProgress.php"
USER_GAME_URL = "https://retroachievements.org/API/API_GetGameInfoAndUserProgress.php"
MEDIA_HOST = "media.retroachievements.org"
BADGE_URL = f"https://{MEDIA_HOST}/Badge/"
MAX_BADGE_BYTES = 256 * 1024  # a badge is a few KiB; anything larger is not one
PAGE_SIZE = 500  # the most API_GetUserCompletionProgress returns per call
MAX_PAGES = 40  # a pass gives up on a user with more than this many pages of games
CALL_DELAY = 0.5  # seconds between two calls of the web API
BADGE_DELAY = 0.2  # seconds between two badge downloads
USER_AGENT = f"warpzone/{__version__}"
KEY_ENV = "RA_API_KEY"
_HASH = re.compile(r"^[0-9a-f]{32}$")
_BADGE = re.compile(r"^[0-9A-Za-z]{1,20}$")
_PNG = b"\x89PNG\r\n\x1a\n"

Fetch = Callable[[str], bytes]
# (url, most bytes allowed) -> (body, Content-Type); raises when the body is larger.
BinaryFetch = Callable[[str, int], tuple[bytes, str]]
Sleep = Callable[[float], None]
_T = TypeVar("_T")


def hash_image(rahasher: str, system: str, path: Path, *, timeout: float = 3600) -> str | None:
    """The RA hash of `path`, or None when RAHasher cannot make one."""
    console = CONSOLE_IDS.get(system)
    if console is None:
        return None
    try:
        proc = subprocess.run(
            [rahasher, str(console), str(path)],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    lines = proc.stdout.strip().splitlines()
    last = lines[-1].strip().lower() if lines else ""
    return last if _HASH.match(last) else None


def cue_to_gdi(cue: str, sector_counts: list[int]) -> str:
    """A GD-ROM table for a Redump-style Dreamcast cue, which RAHasher cannot read.

    `sector_counts` is the size in 2352-byte sectors of each FILE of the cue, in order.
    Tracks 1 and 2 are the single-density area; from track 3 the high-density area
    starts at sector 45000."""
    tracks: list[tuple[str, str]] = []
    for line in cue.splitlines():
        m = re.match(r'\s*FILE\s+"(.+)"', line)
        if m:
            tracks.append((m.group(1), "MODE1/2352"))
            continue
        m = re.match(r"\s*TRACK\s+\d+\s+(\S+)", line)
        if m and tracks:
            tracks[-1] = (tracks[-1][0], m.group(1))
    if len(tracks) != len(sector_counts):
        raise ValueError("cue and sizes disagree")
    lines = [str(len(tracks))]
    lba = 0
    for n, ((name, kind), sectors) in enumerate(zip(tracks, sector_counts, strict=True), 1):
        if n == 3:
            lba = 45000
        lines.append(f'{n} {lba} {0 if kind == "AUDIO" else 4} 2352 "{name}" 0')
        lba += sectors + (150 if n == 1 else 0)
    return "\n".join(lines) + "\n"


@dataclass(frozen=True)
class RaGame:
    title: str
    achievements: int
    id: int = 0  # RA's game id, which the game page of the site is named by
    points: int = 0


def parse_game_list(data: bytes) -> dict[str, RaGame]:
    """RA hash -> game, from the answer of API_GetGameList with h=1."""
    games = json.loads(data)
    if not isinstance(games, list):
        raise ValueError("unexpected answer")
    out: dict[str, RaGame] = {}
    for game in games:
        if not isinstance(game, dict):
            continue
        entry = RaGame(
            str(game.get("Title", "?")),
            int(game.get("NumAchievements") or 0),
            int(game.get("ID") or 0),
            int(game.get("Points") or 0),
        )
        for h in game.get("Hashes") or []:
            if isinstance(h, str):
                out.setdefault(h.lower(), entry)
    return out


def http_fetch(url: str) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=120) as response:
        return response.read()


class FetchError(Exception):
    """A list download that failed. The text says the kind of failure only: the URL
    carries the key, so nothing of the original error is kept."""


def fetch_game_list(console: int, key: str, fetch: Fetch = http_fetch) -> dict[str, RaGame]:
    """The game list of one console (hash -> game). Raises `FetchError`, never with the key."""
    return _call(f"{API_URL}?i={console}&h=1&y={key}", parse_game_list, fetch)


def _call(url: str, parse: Callable[[bytes], _T], fetch: Fetch) -> _T:
    """`parse(fetch(url))`, with every failure turned into a `FetchError` that names its kind
    only: the URL carries the key."""
    try:
        return parse(fetch(url))
    except urllib.error.HTTPError as exc:
        raise FetchError(f"HTTP {exc.code}") from None
    except (
        urllib.error.URLError,
        http.client.HTTPException,
        OSError,
        ValueError,
        TypeError,
        AttributeError,
    ) as exc:
        raise FetchError(type(exc).__name__) from None


# ------------------------------------------------------------------ achievements and progress


@dataclass(frozen=True)
class RaAchievement:
    id: int
    title: str
    description: str
    points: int
    badge: str  # the badge name: digits, the file is `<badge>.png` (`<badge>_lock.png` locked)
    awarded: int  # players who unlocked it, in any mode
    awarded_hardcore: int
    kind: str | None = None  # progression, win_condition, missable; None when RA gives none
    order: int = 0
    true_points: int = 0  # RA's TrueRatio, the "retro" points


@dataclass(frozen=True)
class RaGameDetail:
    id: int
    title: str
    players: int  # distinct players of the game, in any mode
    players_hardcore: int
    achievements: tuple[RaAchievement, ...]


@dataclass(frozen=True)
class UserGame:
    """One line of a user's completion progress (API_GetUserCompletionProgress)."""

    game_id: int
    console_id: int = 0
    max_possible: int = 0
    awarded: int = 0
    awarded_hardcore: int = 0
    last: str = ""  # date of the most recent unlock
    award: str = ""  # beaten-softcore | beaten-hardcore | completed | mastered, or ""
    award_date: str = ""


@dataclass(frozen=True)
class Unlock:
    """When the user unlocked one achievement (None: not in that mode)."""

    soft: str | None = None
    hard: str | None = None


def _int(value: object) -> int:
    try:
        return int(value)  # type: ignore[call-overload]
    except (TypeError, ValueError):
        return 0


def _text(value: object) -> str:
    return value if isinstance(value, str) else ""


def _pick(item: dict[str, Any], *names: str) -> Any:
    """The first of `names` the item has: the API answers in PascalCase, and some of its
    documented examples in camelCase."""
    for name in names:
        if name in item:
            return item[name]
    return None


def _items(value: object) -> list[dict[str, Any]]:
    """The achievements of an answer: an object keyed by id, or a list (empty when none)."""
    raw = list(value.values()) if isinstance(value, dict) else value
    return [i for i in raw if isinstance(i, dict)] if isinstance(raw, list) else []


def _date(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def parse_game_extended(data: bytes) -> RaGameDetail:
    """The achievements of a game, from API_GetGameExtended, in RA's display order."""
    doc = json.loads(data)
    if not isinstance(doc, dict) or not _int(_pick(doc, "ID", "id")):
        raise ValueError("unexpected answer")
    found = []
    for item in _items(_pick(doc, "Achievements", "achievements")):
        ident = _int(_pick(item, "ID", "id"))
        badge = str(_pick(item, "BadgeName", "badgeName") or "")
        if not ident or not _BADGE.match(badge):
            continue
        kind = _pick(item, "type", "Type")
        found.append(
            RaAchievement(
                ident,
                _text(_pick(item, "Title", "title")) or "?",
                _text(_pick(item, "Description", "description")),
                _int(_pick(item, "Points", "points")),
                badge,
                _int(_pick(item, "NumAwarded", "numAwarded")),
                _int(_pick(item, "NumAwardedHardcore", "numAwardedHardcore")),
                kind if isinstance(kind, str) and kind else None,
                _int(_pick(item, "DisplayOrder", "displayOrder")),
                _int(_pick(item, "TrueRatio", "trueRatio")),
            )
        )
    found.sort(key=lambda a: (a.order, a.id))
    return RaGameDetail(
        _int(_pick(doc, "ID", "id")),
        _text(_pick(doc, "Title", "title")) or "?",
        _int(_pick(doc, "NumDistinctPlayers", "numDistinctPlayers")),
        _int(_pick(doc, "NumDistinctPlayersHardcore", "numDistinctPlayersHardcore")),
        tuple(found),
    )


def parse_completion(data: bytes) -> tuple[int, list[UserGame]]:
    """(total games of the user, the games of this page), from API_GetUserCompletionProgress."""
    doc = json.loads(data)
    if not isinstance(doc, dict):
        raise ValueError("unexpected answer")
    games = []
    for item in _items(_pick(doc, "Results", "results")):
        ident = _int(_pick(item, "GameID", "gameId"))
        if not ident:
            continue
        games.append(
            UserGame(
                ident,
                _int(_pick(item, "ConsoleID", "consoleId")),
                _int(_pick(item, "MaxPossible", "maxPossible")),
                _int(_pick(item, "NumAwarded", "numAwarded")),
                _int(_pick(item, "NumAwardedHardcore", "numAwardedHardcore")),
                _text(_pick(item, "MostRecentAwardedDate", "mostRecentAwardedDate")),
                _text(_pick(item, "HighestAwardKind", "highestAwardKind")),
                _text(_pick(item, "HighestAwardDate", "highestAwardDate")),
            )
        )
    return _int(_pick(doc, "Total", "total")), games


def parse_user_unlocks(data: bytes) -> dict[int, Unlock]:
    """Achievement id -> when the user unlocked it, from API_GetGameInfoAndUserProgress.
    Only the achievements the user has are listed."""
    doc = json.loads(data)
    if not isinstance(doc, dict):
        raise ValueError("unexpected answer")
    out: dict[int, Unlock] = {}
    for item in _items(_pick(doc, "Achievements", "achievements")):
        ident = _int(_pick(item, "ID", "id"))
        soft = _date(_pick(item, "DateEarned", "dateEarned"))
        hard = _date(_pick(item, "DateEarnedHardcore", "dateEarnedHardcore"))
        if ident and (soft or hard):
            out[ident] = Unlock(soft, hard)
    return out


def _url(base: str, **params: str | int) -> str:
    return f"{base}?{urllib.parse.urlencode(params)}"


def fetch_game_detail(game_id: int, key: str, fetch: Fetch = http_fetch) -> RaGameDetail:
    return _call(_url(GAME_EXTENDED_URL, i=game_id, y=key), parse_game_extended, fetch)


def fetch_user_unlocks(
    game_id: int, user: str, key: str, fetch: Fetch = http_fetch
) -> dict[int, Unlock]:
    return _call(_url(USER_GAME_URL, g=game_id, u=user, y=key), parse_user_unlocks, fetch)


def fetch_completion(
    user: str,
    key: str,
    fetch: Fetch = http_fetch,
    *,
    sleep: Sleep = time.sleep,
    delay: float = CALL_DELAY,
) -> dict[int, UserGame]:
    """Every game of a user's completion progress, `PAGE_SIZE` a call (game id -> line)."""
    out: dict[int, UserGame] = {}
    offset = 0
    for page in range(MAX_PAGES):
        if page:
            sleep(delay)
        url = _url(COMPLETION_URL, u=user, c=PAGE_SIZE, o=offset, y=key)
        total, games = _call(url, parse_completion, fetch)
        for game in games:
            out.setdefault(game.game_id, game)
        offset += PAGE_SIZE
        if not games or offset >= total:
            break
    return out


# ------------------------------------------------------------------ badges


def badge_url(name: str, *, locked: bool = False) -> str | None:
    """Where RA serves a badge, or None for a name that is not a badge name."""
    if not _BADGE.match(name):
        return None
    return f"{BADGE_URL}{name}{'_lock' if locked else ''}.png"


def is_media_url(url: str) -> bool:
    """Whether `url` is https on RA's media host and nothing else (no user info, no port)."""
    parts = urllib.parse.urlsplit(url)
    return parts.scheme == "https" and parts.netloc == MEDIA_HOST and parts.hostname == MEDIA_HOST


def check_badge(data: bytes, content_type: str) -> bool:
    """Whether a download is a PNG of a size a badge can have."""
    return (
        0 < len(data) <= MAX_BADGE_BYTES
        and content_type.split(";")[0].strip().lower() == "image/png"
        and data.startswith(_PNG)
    )


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args: Any, **kwargs: Any) -> None:
        return None  # a redirect becomes an HTTPError: the media host does not need one


def http_fetch_binary(url: str, limit: int) -> tuple[bytes, str]:
    """GET `url` (https on the media host only, no redirects), at most `limit` bytes."""
    if not is_media_url(url):
        raise ValueError("not the media host")
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    opener = urllib.request.build_opener(_NoRedirect)
    with opener.open(request, timeout=30) as response:
        body = response.read(limit + 1)
        if len(body) > limit:
            raise ValueError("too large")
        return body, response.headers.get("Content-Type", "")


def fetch_badge(name: str, *, locked: bool, fetch: BinaryFetch = http_fetch_binary) -> bytes:
    """The PNG of a badge. Raises `FetchError` (a kind only) when it cannot be had or is not
    a small PNG."""
    url = badge_url(name, locked=locked)
    if url is None:
        raise FetchError("BadName")
    try:
        data, content_type = fetch(url, MAX_BADGE_BYTES)
    except urllib.error.HTTPError as exc:
        raise FetchError(f"HTTP {exc.code}") from None
    except (urllib.error.URLError, http.client.HTTPException, OSError, ValueError) as exc:
        raise FetchError(type(exc).__name__) from None
    if not check_badge(data, content_type):
        raise FetchError("BadImage")
    return data


class Lookup:
    """Hash -> status text, with one list download per console per run."""

    def __init__(self, key: str | None, fetch: Fetch = http_fetch) -> None:
        self._key = key
        self._fetch = fetch
        self._lists: dict[int, dict[str, RaGame] | str] = {}

    @property
    def enabled(self) -> bool:
        return bool(self._key)

    def _list(self, console: int) -> dict[str, RaGame] | str:
        if console not in self._lists:
            assert self._key
            try:
                self._lists[console] = fetch_game_list(console, self._key, self._fetch)
            except FetchError as exc:
                self._lists[console] = str(exc)
        return self._lists[console]

    def status(self, system: str, ra_hash: str) -> str:
        console = CONSOLE_IDS.get(system)
        if console is None or not self._key:
            return "lookup skipped"
        games = self._list(console)
        if isinstance(games, str):
            return f"lookup failed ({games})"
        game = games.get(ra_hash)
        if game is None:
            return "hash not linked"
        if game.achievements:
            return f"supported ({game.title})"
        return f"linked, no achievements yet ({game.title})"
