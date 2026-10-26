"""RetroAchievements: hash an image the way RA does, and look the hash up.

RAHasher (from RALibretro, built with CHD support) prints the RA hash of a game
image. If a CHD gives the hash of the image it came from, the conversion kept what
RA identifies a game by. The web API is optional; its key is read from the
environment by the caller and is never printed, logged or put in an error.
"""

from __future__ import annotations

import json
import re
import subprocess
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

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
USER_AGENT = f"warpzone/{__version__}"
KEY_ENV = "RA_API_KEY"
_HASH = re.compile(r"^[0-9a-f]{32}$")

Fetch = Callable[[str], bytes]


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


def parse_game_list(data: bytes) -> dict[str, RaGame]:
    """RA hash -> game, from the answer of API_GetGameList with h=1."""
    games = json.loads(data)
    if not isinstance(games, list):
        raise ValueError("unexpected answer")
    out: dict[str, RaGame] = {}
    for game in games:
        if not isinstance(game, dict):
            continue
        entry = RaGame(str(game.get("Title", "?")), int(game.get("NumAchievements") or 0))
        for h in game.get("Hashes") or []:
            if isinstance(h, str):
                out.setdefault(h.lower(), entry)
    return out


def http_fetch(url: str) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=120) as response:
        return response.read()


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
            url = f"{API_URL}?i={console}&h=1&y={self._key}"
            try:
                self._lists[console] = parse_game_list(self._fetch(url))
            except urllib.error.HTTPError as exc:
                self._lists[console] = f"HTTP {exc.code}"
            except (urllib.error.URLError, OSError, ValueError, TypeError) as exc:
                # The URL carries the key, so only the kind of failure is kept.
                self._lists[console] = type(exc).__name__
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
