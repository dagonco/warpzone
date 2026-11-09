"""`warpzone catalog --ra-achievements`: download the achievements of the library's games, the
user's progress in them and the badge images into `catalog/ra/`.

What to keep and how to merge it is in `ra_data.py` (pure); the web API is parsed by `ra.py`.
Here is what touches the disk and the network, politely: one call at a time with a pause
between two, a failure that never fails the whole pass (three in a row stop the pass, since
the cause - a wrong key, a rate limit - will not go away by itself), and the key kept out of
every message. See "Achievements and progress" in development/design/library-ui.md.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from warpzone import catalog, catalog_store, ra, ra_data, safefile
from warpzone.catalog_store import CatalogError

CALL_BATCH = 60  # API calls of one run of the server (a CLI pass has no limit)
BADGE_BATCH = 300  # badge downloads of one run of the server
STOP_AFTER = 3  # failures in a row after which a section gives up
OUT_OF_BUDGET = "OutOfBudget"
NOT_FOUND = "HTTP 404"  # a game or badge RA does not have; remembered, not a failure


def ra_dir(root: Path) -> Path:
    return root / catalog_store.CATALOG_DIR / "ra"


def game_path(root: Path, game_id: int) -> Path:
    return ra_dir(root) / "games" / f"{game_id}.json"


def user_path(root: Path) -> Path:
    return ra_dir(root) / "user" / "summary.json"


def unlocks_path(root: Path, game_id: int) -> Path:
    return ra_dir(root) / "user" / "games" / f"{game_id}.json"


def badge_path(root: Path, name: str, *, locked: bool) -> Path | None:
    """Where the image of a badge is cached, or None for a name that is not a badge name."""
    if ra.badge_url(name) is None:
        return None
    return ra_dir(root) / "badges" / f"{name}{'_lock' if locked else ''}.png"


def missing_game_path(root: Path, game_id: int) -> Path:
    """The note that RA answered 404 for a game, so the next pass does not ask at once."""
    return ra_dir(root) / "games" / f"{game_id}.missing"


def missing_badge_path(image: Path) -> Path:
    return image.with_suffix(".missing")


def kind_of(path: Path) -> str | None:
    """What a cache file under `catalog/ra/` is, by where it is: list, game, user, unlocks."""
    parent, grand = path.parent.name, path.parent.parent.name
    if parent == "ra":
        return "list"
    if parent == "games" and grand == "ra":
        return "game"
    if parent == "games" and grand == "user":
        return "unlocks"
    if parent == "user" and path.name == "summary.json":
        return "user"
    return None


def read_file(kind: str, path: Path) -> object | None:
    """The decoded cache file of that kind, or None when it is absent or not of its format."""
    try:
        data = path.read_bytes()
    except OSError:
        return None
    if kind == "game":
        return ra_data.decode_game(data)
    if kind == "user":
        return ra_data.decode_user(data)
    if kind == "unlocks":
        return ra_data.decode_unlocks(data)
    return None


def read_game(root: Path, game_id: int) -> ra_data.CachedGame | None:
    found = read_file("game", game_path(root, game_id))
    return found if isinstance(found, ra_data.CachedGame) else None


def read_user(root: Path) -> ra_data.UserSummary | None:
    found = read_file("user", user_path(root))
    return found if isinstance(found, ra_data.UserSummary) else None


def read_unlocks(root: Path, game_id: int) -> ra_data.CachedUnlocks | None:
    found = read_file("unlocks", unlocks_path(root, game_id))
    return found if isinstance(found, ra_data.CachedUnlocks) else None


def _write(path: Path, data: bytes) -> bool:
    """Replace the file unless it changed while we were at it (tried a few times)."""
    for _ in range(catalog_store.WRITE_TRIES):
        try:
            before = safefile.stamp(path)
        except FileNotFoundError:
            before = None
        if safefile.atomic_write(path, data, expect=before):
            return True
    return False


def library_ids(root: Path) -> list[int]:
    """The RA game ids of the library's games that have achievements, from the catalog and
    the cached game lists (nothing is downloaded), in id order."""
    lists: dict[int, catalog.RaList] = {}
    for console, _ in catalog_store.ra_list_consoles(root):
        found, _ = catalog_store.read_ra_list(catalog_store.ra_list_path(root, console))
        if found is not None:
            lists[console] = found
    systems = {}
    for system in catalog_store.library_systems(root):
        doc, _ = catalog_store.read_state(catalog_store.system_path(root, system))
        if doc is not None:
            systems[system] = doc.games
    found_ids = {i for games in ra_data.library_games(systems, lists).values() for i in games}
    return sorted(found_ids)


@dataclass
class AchievementsResult:
    games: int = 0  # library games RA has achievements for
    details_fetched: int = 0
    details_failed: int = 0
    details_left: int = 0  # still to download after this pass (all of them, in a dry run)
    user_fetched: bool = False
    user_failed: bool = False
    unlocks_fetched: int = 0
    unlocks_failed: int = 0
    unlocks_left: int = 0
    badges_fetched: int = 0
    badges_failed: int = 0
    details_missing: int = 0  # games RA answered 404 for (asked again after 14 days)
    badges_missing: int = 0  # images RA answered 404 for (not asked again)
    badges_left: int = 0

    @property
    def failed(self) -> bool:
        return bool(self.details_failed or self.user_failed or self.unlocks_failed) or bool(
            self.badges_failed
        )


class _Pass:
    """One run: the clock, the pause between calls and the budget of API calls."""

    def __init__(
        self,
        root: Path,
        key: str,
        fetch: ra.Fetch,
        sleep: ra.Sleep,
        calls: int | None,
        now: datetime,
        warn: Callable[[str], None],
    ) -> None:
        self.root, self.key, self.fetch, self.sleep = root, key, fetch, sleep
        self.calls, self.now, self.warn = calls, now, warn
        self.time = now.strftime(ra_data.TIME)
        self._first = True
        self.aborted = False

    def has_budget(self) -> bool:
        return self.calls is None or self.calls > 0

    def pause(self, delay: float) -> None:
        if not self._first:
            self.sleep(delay)
        self._first = False

    def spend(self) -> None:
        if self.calls is not None:
            self.calls -= 1


def run_ra_achievements(
    root: Path,
    *,
    key: str | None,
    user: str | None,
    force: bool,
    dry_run: bool,
    out: Callable[[str], None],
    warn: Callable[[str], None],
    fetch: ra.Fetch | None = None,
    fetch_binary: ra.BinaryFetch | None = None,
    sleep: ra.Sleep = time.sleep,
    now: datetime | None = None,
    call_limit: int | None = None,
    badge_limit: int | None = None,
) -> AchievementsResult:
    """Download what the library's RA games lack: the achievements of each (kept 14 days), the
    user's completion progress (kept 5 hours), the unlock dates of each game the user has
    played (again when the progress says something new happened) and the badge images.

    `call_limit` and `badge_limit` bound a run; what is left is done by the next one. Without
    `user` there is no progress. A failed call is reported by kind only and the pass goes on;
    `STOP_AFTER` in a row stop the section. Needs the cached game lists (`--ra-lists`)."""
    if not key:
        raise CatalogError(f"no RetroAchievements key: set {ra.KEY_ENV} in the environment")
    if not (root / "library" / "roms").is_dir():
        raise CatalogError(f"{root} has no library/roms/ folder; is --root the hub root?")
    run = _Pass(
        root, key, fetch or ra.http_fetch, sleep, call_limit, now or datetime.now(UTC), warn
    )
    ids = library_ids(root)
    result = AchievementsResult(games=len(ids))
    if not ids:
        out("RA achievements: no library game has achievements (are the game lists cached?)")
        return result
    summary = _user(run, user, force, dry_run, result, out) if user else None
    _details(run, ids, force, dry_run, result, out)
    if user and summary is not None:
        _unlocks(run, ids, user, summary, dry_run, result, out)
    _badges(
        root,
        ids,
        summary,
        fetch_binary or ra.http_fetch_binary,
        sleep,
        badge_limit,
        dry_run,
        result,
        out,
        warn,
    )
    return result


def _details(
    run: _Pass,
    ids: list[int],
    force: bool,
    dry_run: bool,
    result: AchievementsResult,
    out: Callable[[str], None],
) -> None:
    missing, stale = [], []
    for game_id in ids:
        cached = read_game(run.root, game_id)
        if cached is None and _recently_missing(run, game_id):
            continue
        if cached is None:
            missing.append(game_id)
        elif force or not ra_data.fresh(
            cached.fetched, run.now, timedelta(days=ra_data.DETAIL_DAYS)
        ):
            stale.append(game_id)
    todo = missing + stale
    result.details_left = len(todo)
    if dry_run:
        out(f"RA achievements: would download {len(todo)} game(s) of {len(ids)}")
        return
    failures = 0
    for game_id in todo:
        if not run.has_budget():
            break
        run.pause(ra.CALL_DELAY)
        run.spend()
        try:
            detail = ra.fetch_game_detail(game_id, run.key, run.fetch)
        except ra.FetchError as exc:
            if str(exc) == NOT_FOUND:
                result.details_missing += 1
                result.details_left -= 1
                run.warn(f"RA game {game_id}: RA has no such game (HTTP 404); not asked again soon")
                _write(missing_game_path(run.root, game_id), run.time.encode())
                continue
            result.details_failed += 1
            failures += 1
            run.warn(f"RA game {game_id}: download failed ({exc})")
            if failures >= STOP_AFTER:
                run.warn("RA achievements: stopping after repeated failures")
                run.aborted = True
                break
            continue
        failures = 0
        if _write(game_path(run.root, game_id), ra_data.encode_game(detail, run.time)):
            result.details_fetched += 1
            result.details_left -= 1
        else:
            result.details_failed += 1
            run.warn(f"RA game {game_id}: the cache file kept changing, try again")
    out(
        f"RA achievements: {result.details_fetched} game(s) downloaded, "
        f"{result.details_left} left of {len(ids)}"
    )


def _recently_missing(run: _Pass, game_id: int) -> bool:
    try:
        noted = missing_game_path(run.root, game_id).read_text(encoding="utf-8")
    except OSError:
        return False
    return ra_data.fresh(noted.strip(), run.now, timedelta(days=ra_data.DETAIL_DAYS))


def _user(
    run: _Pass,
    user: str,
    force: bool,
    dry_run: bool,
    result: AchievementsResult,
    out: Callable[[str], None],
) -> ra_data.UserSummary | None:
    cached = read_user(run.root)
    if cached is not None and not ra_data.same_user(cached.user, user):
        cached = None  # the configured user changed: the old progress is not theirs
    current = (
        cached is not None
        and not force
        and ra_data.fresh(cached.fetched, run.now, timedelta(hours=ra_data.USER_HOURS))
    )
    if current:
        return cached
    if dry_run:
        out("RA progress: would download the completion progress")
        return cached
    if run.aborted or not run.has_budget():
        return cached

    def charge(page: int) -> None:
        """Every page is a call: one the budget cannot pay ends the download, and the
        half-read progress is dropped (the cached one stays, the next run tries again)."""
        if not run.has_budget():
            raise ra.FetchError(OUT_OF_BUDGET)
        if page == 0:  # the later pages are paused by `fetch_completion`
            run.pause(ra.CALL_DELAY)
        run.spend()

    try:
        games = ra.fetch_completion(user, run.key, run.fetch, sleep=run.sleep, before_page=charge)
    except ra.FetchError as exc:
        if str(exc) == OUT_OF_BUDGET:
            run.warn("RA progress: out of calls for this run; it goes on in the next one")
            return cached
        result.user_failed = True
        run.warn(
            f"RA progress: download failed ({exc})" + ("; keeping the cache" if cached else "")
        )
        return cached
    if not _write(user_path(run.root), ra_data.encode_user(user, run.time, games)):
        result.user_failed = True
        run.warn("RA progress: the cache file kept changing, try again")
        return cached
    result.user_fetched = True
    out(f"RA progress: {len(games)} game(s) of the user")
    return ra_data.UserSummary(user, run.time, games)


def _unlocks(
    run: _Pass,
    ids: list[int],
    user: str,
    summary: ra_data.UserSummary,
    dry_run: bool,
    result: AchievementsResult,
    out: Callable[[str], None],
) -> None:
    todo = []
    for game_id in ids:
        line = summary.games.get(game_id)
        if (
            line is not None
            and ra_data.played(line)
            and not ra_data.unlocks_current(read_unlocks(run.root, game_id), user, line)
        ):
            todo.append((game_id, line))
    result.unlocks_left = len(todo)
    if dry_run:
        out(f"RA progress: would download the unlock dates of {len(todo)} game(s)")
        return
    failures = 0
    for game_id, line in todo:
        if run.aborted or not run.has_budget():
            break
        run.pause(ra.CALL_DELAY)
        run.spend()
        try:
            unlocks = ra.fetch_user_unlocks(game_id, user, run.key, run.fetch)
        except ra.FetchError as exc:
            result.unlocks_failed += 1
            failures += 1
            run.warn(f"RA progress of game {game_id}: download failed ({exc})")
            if failures >= STOP_AFTER:
                run.warn("RA progress: stopping after repeated failures")
                run.aborted = True
                break
            continue
        failures = 0
        data = ra_data.encode_unlocks(user, game_id, run.time, line, unlocks)
        if _write(unlocks_path(run.root, game_id), data):
            result.unlocks_fetched += 1
            result.unlocks_left -= 1
        else:
            result.unlocks_failed += 1
            run.warn(f"RA progress of game {game_id}: the cache file kept changing")
    out(
        f"RA progress: unlock dates of {result.unlocks_fetched} game(s) downloaded, "
        f"{result.unlocks_left} left"
    )


def _badges(
    root: Path,
    ids: list[int],
    summary: ra_data.UserSummary | None,
    fetch: ra.BinaryFetch,
    sleep: ra.Sleep,
    limit: int | None,
    dry_run: bool,
    result: AchievementsResult,
    out: Callable[[str], None],
    warn: Callable[[str], None],
) -> None:
    """Both images of every badge of the cached games, those of the games the user has
    played first."""
    played = {i for i, line in (summary.games.items() if summary else ()) if ra_data.played(line)}
    todo: list[tuple[str, bool, Path]] = []
    seen: set[Path] = set()
    for game_id in sorted(ids, key=lambda i: (i not in played, i)):
        cached = read_game(root, game_id)
        for achievement in cached.detail.achievements if cached else ():
            for locked in (False, True):
                path = badge_path(root, achievement.badge, locked=locked)
                if (
                    path is not None
                    and path not in seen
                    and not path.is_file()
                    and not missing_badge_path(path).is_file()
                ):
                    seen.add(path)
                    todo.append((achievement.badge, locked, path))
    result.badges_left = len(todo)
    if dry_run:
        out(f"RA badges: would download {len(todo)} image(s)")
        return
    failures = 0
    done = 0
    for name, locked, path in todo:
        if limit is not None and done >= limit:
            break
        if done:
            sleep(ra.BADGE_DELAY)
        done += 1
        try:
            data = ra.fetch_badge(name, locked=locked, fetch=fetch)
        except ra.FetchError as exc:
            label = f"RA badge {name}{' (locked)' if locked else ''}"
            if str(exc) == NOT_FOUND:  # RA does not have it: never ask again
                result.badges_missing += 1
                result.badges_left -= 1
                warn(f"{label}: RA has no such image (HTTP 404); not asked again")
                _write(missing_badge_path(path), b"404\n")
                continue
            result.badges_failed += 1
            failures += 1
            warn(f"{label}: download failed ({exc})")
            if failures >= STOP_AFTER:
                warn("RA badges: stopping after repeated failures")
                break
            continue
        failures = 0
        try:
            safefile.atomic_write(path, data, expect=None)  # False: it is there already
        except OSError as exc:
            result.badges_failed += 1
            warn(f"RA badge {name}: {exc.strerror or type(exc).__name__}")
            continue
        result.badges_fetched += 1
        result.badges_left -= 1
    out(f"RA badges: {result.badges_fetched} image(s) downloaded, {result.badges_left} left")
