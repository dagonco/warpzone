"""`warpzone serve`: the library UI's page and JSON API on one port (`http.server`).

The decisions are in `library_api.py` and `progress.py` (pure). Here is what touches the
world: the sockets, the token and the session cookie, the reads of the hub root with their
caches, the lock that serializes whitelist writes, the questions to the hub's Syncthing and
the background `catalog` runs. See "HTTP API" in development/design/library-ui.md.

The token is never printed, logged or echoed.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import signal
import socket
import socketserver
import sys
import threading
import time
import urllib.parse
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, BinaryIO

from warpzone import (
    __version__,
    artwork,
    catalog,
    catalog_store,
    diskstatus_io,
    library_api,
    playstate,
    progress,
    queue,
    queue_store,
    ra,
    ra_data,
    ra_store,
    saves_index,
)
from warpzone import whitelist_store as store
from warpzone.config import Config, ServeSettings, split_listen
from warpzone.errors import EXIT_OK, UsageError, WarpzoneError
from warpzone.library_api import ApiError
from warpzone.syncthing import Client, SyncthingError

TOKEN_ENV = "WARPZONE_UI_TOKEN"
COOKIE = "wz_session"
COOKIE_MAX_AGE = 30 * 24 * 3600  # the session's lifetime: the cookie's Max-Age and the server's
SESSION_MESSAGE = b"warpzone-ui-session"
CACHE_SECONDS = 5.0  # a file is checked for a change at most this often
MEDIA_SECONDS = 60.0  # the listing of the media folders is read again after this long
PROGRESS_SECONDS = 15.0
PROGRESS_FAILURE_SECONDS = 5.0  # a failed question to the hub's Syncthing is not asked again before
LOGIN_DELAY = 1.0
MIN_TOKEN = 16  # characters; a shorter token is refused at start
MAX_CONNECTIONS = 64  # connections served at once; the rest are closed
SOCKET_TIMEOUT = 15  # seconds a connection may stay silent, also in the middle of a body
CATALOG_PERIOD = 6 * 3600.0
RA_LISTS_PERIOD = 24 * 3600.0
UI_DIR = Path(__file__).resolve().parent / "data" / "ui"
CSP = "default-src 'self'"
CHUNK = 64 * 1024  # bytes of a media file read and sent at a time
LIBRARY_FOLDER = "wz-library"
CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".mjs": "text/javascript; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".ico": "image/x-icon",
    ".mp4": "video/mp4",
    ".webm": "video/webm",
    ".woff2": "font/woff2",
    ".txt": "text/plain; charset=utf-8",
}


class ServeError(UsageError):
    """`serve` cannot start: no token, or the hub root is not mounted."""


# --------------------------------------------------------------------------- token and session


def load_token(env: Mapping[str, str], settings: ServeSettings) -> str:
    """The UI token: `WARPZONE_UI_TOKEN`, else the first line of `[serve] token_file`."""
    token = env.get(TOKEN_ENV, "").strip()
    if token:
        return _long_enough(token, f"{TOKEN_ENV}")
    if settings.token_file:
        try:
            lines = Path(settings.token_file).read_text(encoding="utf-8").splitlines()
        except OSError as exc:
            raise ServeError(f"[serve] token_file: cannot read {settings.token_file}") from exc
        first = lines[0].strip() if lines else ""
        if first:
            return _long_enough(first, f"[serve] token_file {settings.token_file}")
        raise ServeError(
            f"[serve] token_file: {settings.token_file} has no token on its first line"
        )
    raise ServeError(f"no UI token: set {TOKEN_ENV} or `token_file` in the [serve] table")


def _long_enough(token: str, source: str) -> str:
    if len(token) < MIN_TOKEN:
        raise ServeError(
            f"{source}: the token must be at least {MIN_TOKEN} characters; make one with "
            "`openssl rand -hex 32`"
        )
    return token


class FailureLimiter:
    """One limiter for every failed authentication, whichever thread it comes from: the
    failures are answered `interval` seconds apart, so parallel guesses queue up behind each
    other instead of each sleeping on its own. A right token never comes here."""

    def __init__(
        self,
        interval: float,
        *,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] | None = None,
    ) -> None:
        self.interval = interval
        self._clock = clock
        self._sleep = sleep
        self._lock = threading.Lock()
        self._next = float("-inf")

    def failed(self) -> None:
        if not self.interval:
            return
        with self._lock:
            now = self._clock()
            wait = self.interval + max(0.0, self._next - now)
            self._next = now + wait
        (self._sleep or time.sleep)(wait)


def session_cookie(token: str, expires: int) -> str:
    """The value of the session cookie: `<expires>.<mac>`, where `expires` is the Unix time the
    session ends and `mac` the HMAC-SHA256 of a fixed message and `expires` under the token. It
    needs no server state: the server checks the mac and the time (`session_valid`)."""
    message = SESSION_MESSAGE + b"." + str(expires).encode("ascii")
    mac = hmac.new(token.encode("utf-8"), message, hashlib.sha256).hexdigest()
    return f"{expires}.{mac}"


def session_valid(token: str, value: str, now: int) -> bool:
    """Whether a cookie value was made by `session_cookie` under this token and has not
    expired, whatever the browser does with its `Max-Age`."""
    stamp, sep, _ = value.partition(".")
    if not sep or not stamp.isascii() or not stamp.isdigit():
        return False
    expires = int(stamp)
    return _same(value, session_cookie(token, expires)) and now < expires


def _same(a: str, b: str) -> bool:
    return hmac.compare_digest(a.encode("utf-8"), b.encode("utf-8"))


def cookie_values(header: str, name: str) -> list[str]:
    """Every value of the cookie `name` in a `Cookie` header, parsed by hand: other
    applications on the same host share the cookie jar, and their odd values must not hide
    ours."""
    found = []
    for part in header.split(";"):
        key, sep, value = part.partition("=")
        if sep and key.strip() == name:
            found.append(value.strip())
    return found


# --------------------------------------------------------------------------- requests


@dataclass(frozen=True)
class Request:
    method: str
    target: str  # the request target as sent: path and query
    headers: Mapping[str, str]  # keys lower-cased
    body: bytes = b""
    too_large: bool = False  # the body was over the limit and was not read

    @property
    def path(self) -> str:
        return urllib.parse.urlsplit(self.target).path

    @property
    def query(self) -> dict[str, str]:
        # Blank values are kept: `?limit=` is a value that is not a number, not a missing one.
        parsed = urllib.parse.parse_qs(
            urllib.parse.urlsplit(self.target).query, keep_blank_values=True
        )
        return {k: v[0] for k, v in parsed.items()}


@dataclass
class Response:
    status: int
    body: bytes = b""
    content_type: str = "application/json; charset=utf-8"
    headers: list[tuple[str, str]] = field(default_factory=list)
    # A file sent in chunks instead of `body`: `stream` is open and positioned at the first byte,
    # `length` bytes of it are sent, and the server closes it (HEAD sends none).
    stream: BinaryIO | None = None
    length: int = 0


def json_response(
    status: int, value: object, headers: list[tuple[str, str]] | None = None
) -> Response:
    body = json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return Response(status, body, headers=headers or [])


def error_response(error: ApiError) -> Response:
    return json_response(error.status, error.body())


# --------------------------------------------------------------------------- reading the hub


class HubFiles:
    """The hub root's files, read and kept in memory. A file is read again when its size or
    mtime changed, and checked for that at most every `interval` seconds. The whitelists are
    small and decide a write, so they are read on every request."""

    def __init__(
        self,
        root: Path,
        *,
        interval: float = CACHE_SECONDS,
        media_seconds: float = MEDIA_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.root = root
        self.interval = interval
        self.media_seconds = media_seconds
        self.clock = clock
        self._lock = threading.RLock()
        self._library: library_api.Library | None = None
        self._checked = float("-inf")
        self._stamps: dict[Path, tuple[int, int]] = {}
        self._docs: dict[Path, catalog.SystemDoc] = {}
        self._ra: dict[Path, catalog.RaList] = {}
        self._ra_files: dict[Path, tuple[str, Any]] = {}  # games, user progress, unlock dates
        self._meta: dict[str, tuple[tuple[int, int] | None, float, dict[str, artwork.Entry]]] = {}
        self._media: dict[str, tuple[float, dict[str, dict[str, str]]]] = {}
        self._unclaimed: dict[str, tuple[float, tuple[str, ...]]] = {}
        self._saves = saves_index.SavesCache(
            root / saves_index.SAVES_DIR, ttl=media_seconds, clock=clock
        )
        self._ledgers: dict[Path, tuple[tuple[int, int], playstate.Ledger | None]] = {}
        self._ledgers_checked = float("-inf")
        self._queue: tuple[tuple[int, int] | None, queue_store.Loaded] | None = None
        self._queue_checked = float("-inf")

    # ------------------------------------------------------------- the catalog

    @staticmethod
    def _stat(path: Path) -> tuple[int, int] | None:
        try:
            st = path.stat()
        except OSError:
            return None
        return st.st_size, st.st_mtime_ns

    def _files(self, directory: Path) -> list[Path]:
        try:
            return sorted(p for p in directory.glob("*.json") if p.is_file())
        except OSError:
            return []

    def invalidate(self) -> None:
        """Check the catalog files on the next read, whatever the interval says."""
        with self._lock:
            self._checked = float("-inf")

    def library(self) -> library_api.Library:
        with self._lock:
            if self._library is not None and self.clock() - self._checked < self.interval:
                return self._library
            self._checked = self.clock()
            base = self.root / catalog_store.CATALOG_DIR
            seen: dict[Path, tuple[int, int]] = {}
            changed = self._library is None
            ra_base = base / "ra"
            for path in [
                *self._files(base / "systems"),
                *self._files(ra_base),
                *self._files(ra_base / "games"),
                *self._files(ra_base / "user"),
                *self._files(ra_base / "user" / "games"),
            ]:
                stamp = self._stat(path)
                if stamp is None:
                    continue
                seen[path] = stamp
                if self._stamps.get(path) != stamp:
                    changed = True
                    self._load(path)
            if set(seen) != set(self._stamps):
                changed = True
            if changed:
                for gone in set(self._stamps) - set(seen):
                    self._docs.pop(gone, None)
                    self._ra.pop(gone, None)
                    self._ra_files.pop(gone, None)
                self._stamps = seen
                self._library = self._build()
            assert self._library is not None
            return self._library

    def _load(self, path: Path) -> None:
        self._docs.pop(path, None)
        self._ra.pop(path, None)
        self._ra_files.pop(path, None)
        kind = ra_store.kind_of(path)
        try:
            if kind == "list":
                found, _ = catalog_store.read_ra_list(path)
                if found is not None:
                    self._ra[path] = found
            elif kind is not None:
                loaded = ra_store.read_file(kind, path)
                if loaded is not None:
                    self._ra_files[path] = (kind, loaded)
            else:
                doc, _ = catalog_store.read_state(path)
                if doc is not None:
                    self._docs[path] = doc
        except OSError:
            pass  # it vanished or keeps changing: the next check looks again

    def _build(self) -> library_api.Library:
        systems = {doc.system: doc.games for doc in self._docs.values()}
        lists = {ra_list.console: ra_list for ra_list in self._ra.values()}
        updated = max((d.updated for d in self._docs.values()), default="")
        return library_api.Library(dict(sorted(systems.items())), lists, updated, self._ra_data())

    def _ra_data(self) -> ra_data.RaData:
        games: dict[int, ra_data.CachedGame] = {}
        unlocks: dict[int, ra_data.CachedUnlocks] = {}
        user: ra_data.UserSummary | None = None
        for kind, loaded in self._ra_files.values():
            if kind == "game":
                games[loaded.detail.id] = loaded
            elif kind == "unlocks":
                unlocks[loaded.game_id] = loaded
            elif kind == "user":
                user = loaded
        return ra_data.RaData(games, user, unlocks)

    def badge_path(self, name: str, variant: str) -> Path | None:
        """The cached image of a badge (`unlocked` or `locked`), or None when it is not there."""
        if variant not in ("unlocked", "locked"):
            return None
        path = ra_store.badge_path(self.root, name, locked=variant == "locked")
        return path if path is not None and path.is_file() and not path.is_symlink() else None

    # ------------------------------------------------------------- gamelists and media

    def extras(self, system: str) -> library_api.SystemExtras:
        return library_api.SystemExtras(
            self._gamelist(system), self._media_index(system), self._switch_unclaimed(system)
        )

    def _gamelist(self, system: str) -> Mapping[str, artwork.Entry]:
        path = self.root / "frontend" / "es-de" / "gamelists" / system / "gamelist.xml"
        with self._lock:
            held = self._meta.get(system)
            if held is not None and self.clock() - held[1] < self.interval:
                return held[2]
            stamp = self._stat(path)
            if held is not None and held[0] == stamp:
                self._meta[system] = (stamp, self.clock(), held[2])
                return held[2]
            entries: dict[str, artwork.Entry] = {}
            if stamp is not None:
                try:
                    for entry in artwork.parse_gamelist(path.read_bytes()):
                        entries.setdefault(entry.path, entry)
                except (OSError, artwork.ArtworkError):
                    entries = {}
            self._meta[system] = (stamp, self.clock(), entries)
            return entries

    def _media_index(self, system: str) -> Mapping[str, Mapping[str, str]]:
        with self._lock:
            held = self._media.get(system)
            if held is not None and self.clock() - held[0] < self.media_seconds:
                return held[1]
            listing: dict[str, list[str]] = {}
            for kind in library_api.MEDIA_KINDS:
                base = self.root / "frontend" / "media" / system / kind
                names: list[str] = []
                for here, _, files in os.walk(base, followlinks=False):
                    rel = Path(here).relative_to(base).as_posix()
                    names.extend(f if rel == "." else f"{rel}/{f}" for f in files)
                listing[kind] = names
            index = library_api.media_index(listing)
            self._media[system] = (self.clock(), index)
            return index

    def _switch_unclaimed(self, system: str) -> tuple[str, ...]:
        if system != "switch":
            return ()
        with self._lock:
            held = self._unclaimed.get(system)
            if held is not None and self.clock() - held[0] < self.media_seconds:
                return held[1]
            try:
                listing = catalog_store.walk(self.root / "library", system)
                found = tuple(catalog.unclaimed(system, listing))
            except OSError:
                found = ()
            self._unclaimed[system] = (self.clock(), found)
            return found

    def game_saves(self, lib: library_api.Library, game_id: str) -> dict[str, Any]:
        """The `saves` object of a game (an empty one for an unknown id)."""
        found = lib.by_id.get(game_id)
        if found is None:
            return saves_index.saves_response(saves_index.GameSaves())
        return self._saves.game(lib, found[0], found[1].primary, found[1].serials)

    def media_path(self, system: str, kind: str, name: str) -> Path | None:
        """The image file, or None when it is not a regular file inside its kind's folder
        (a symlink pointing out of it is refused)."""
        parts = library_api.safe_parts("/" + name)
        if parts is None or not parts:
            return None
        return safe_file(self.root / "frontend" / "media" / system / kind, parts)

    # ------------------------------------------------------------- play state

    def play_ledgers(self) -> tuple[playstate.Ledger, ...]:
        """The ledgers in `frontend/play-state/`, one per device, in device order. A file is
        parsed again when its size or mtime changed (checked at most every `interval`
        seconds); one that cannot be read, or trusted, is left out. Conflict copies are
        ignored, and the devices that left the config still count: their plays happened."""
        with self._lock:
            if self.clock() - self._ledgers_checked >= self.interval:
                self._ledgers_checked = self.clock()
                self._read_ledgers()
            return tuple(
                sorted(
                    (ledger for _, ledger in self._ledgers.values() if ledger is not None),
                    key=lambda ledger: ledger.device,
                )
            )

    def _read_ledgers(self) -> None:
        base = self.root / "frontend" / "play-state"
        try:
            names = sorted(p for p in base.glob("*.jsonl") if ".sync-conflict-" not in p.name)
        except OSError:
            names = []
        found: dict[Path, tuple[tuple[int, int], playstate.Ledger | None]] = {}
        for path in names:
            stamp = self._stat(path)
            if stamp is None:
                continue
            held = self._ledgers.get(path)
            if held is not None and held[0] == stamp:
                found[path] = held
                continue
            try:
                ledger: playstate.Ledger | None = playstate.parse_ledger(
                    path.read_bytes().decode("utf-8"), path.stem
                )
            except (OSError, UnicodeDecodeError, playstate.LedgerError):
                ledger = None
            found[path] = (stamp, ledger)
        self._ledgers = found

    # ------------------------------------------------------------- the queue

    def queue(self) -> queue_store.Loaded:
        """The queue file, read again when its size or mtime changed (checked at most every
        `interval` seconds). A write of the server drops it (`invalidate_queue`)."""
        with self._lock:
            if self._queue is not None and self.clock() - self._queue_checked < self.interval:
                return self._queue[1]
            self._queue_checked = self.clock()
            stamp = self._stat(queue_store.queue_path(self.root))
            if self._queue is None or self._queue[0] != stamp:
                self._queue = (stamp, queue_store.load(self.root))
            return self._queue[1]

    def invalidate_queue(self) -> None:
        with self._lock:
            self._queue = None
            self._queue_checked = float("-inf")

    # ------------------------------------------------------------- whitelists

    def views(self, cfg: Config) -> dict[str, library_api.DeviceView]:
        found = {}
        for device in library_api.library_devices(cfg):
            snapshot = store.read(store.whitelist_path(self.root, device.name))
            found[device.name] = library_api.view_of(snapshot.data)
        return found

    def backup_names(self, device: str) -> list[str]:
        try:
            return os.listdir(store.backups_dir(self.root, device))
        except OSError:
            return []


def safe_file(base: Path, parts: tuple[str, ...]) -> Path | None:
    """`base` joined with `parts` when that is a regular file whose real path (symlinks
    resolved) is still inside `base`'s real path; else None."""
    real_base = os.path.realpath(base)
    real = os.path.realpath(os.path.join(real_base, *parts))
    if not real.startswith(real_base + os.sep) or not os.path.isfile(real):
        return None
    return Path(real)


# --------------------------------------------------------------------------- background runs


class Refresher:
    """The `catalog` runs of the server: one at a time, in a thread of their own.

    `job(ra_lists)` does the work and returns a short message (or raises). `trigger` starts
    a run unless one is running; `schedule` is the loop that runs at start and then every six
    hours, with the RetroAchievements lists once a day."""

    def __init__(
        self,
        job: Callable[[bool], str],
        log: Callable[[str], None],
        after: Callable[[], None] | None = None,
    ) -> None:
        self._job = job
        self._log = log
        # Called when a run ends, before `running` turns false: the server drops what it read
        # of the catalog, so a client that reloads at that moment sees the run's result.
        self.after = after
        self._busy = threading.Lock()
        self._state_lock = threading.Lock()
        self._last: dict[str, Any] | None = None
        self._running = False

    @property
    def running(self) -> bool:
        return self._running

    def status(self) -> dict[str, Any]:
        with self._state_lock:
            return {"running": self._running, "last_run": self._last}

    def _run(self, ra_lists: bool) -> None:
        started = _utc(datetime.now(UTC))
        ok, message = True, ""
        try:
            message = self._job(ra_lists)
        except WarpzoneError as exc:
            ok, message = False, str(exc)
        except Exception as exc:  # a background run must never take the server down
            ok, message = False, type(exc).__name__
        if self.after is not None:
            self.after()
        with self._state_lock:
            self._running = False
            self._last = {
                "started": started,
                "finished": _utc(datetime.now(UTC)),
                "ok": ok,
                "message": message,
            }
        self._log(f"catalog run {'finished' if ok else 'failed'}: {message}")
        self._busy.release()

    def trigger(self, ra_lists: bool = False) -> bool:
        """Start a run in a thread; False when one is running."""
        if not self._busy.acquire(blocking=False):
            return False
        with self._state_lock:
            self._running = True
        threading.Thread(target=self._run, args=(ra_lists,), daemon=True).start()
        return True

    def run_now(self, ra_lists: bool = False) -> bool:
        """Run in the calling thread; False when one is running."""
        if not self._busy.acquire(blocking=False):
            return False
        with self._state_lock:
            self._running = True
        self._run(ra_lists)
        return True

    def schedule(
        self,
        stop: threading.Event,
        *,
        key: bool,
        period: float = CATALOG_PERIOD,
        ra_period: float = RA_LISTS_PERIOD,
    ) -> None:
        last_ra = float("-inf")
        while not stop.is_set():
            due = key and time.monotonic() - last_ra >= ra_period
            if self.run_now(due) and due:
                last_ra = time.monotonic()
            if stop.wait(period):
                return


def _utc(when: datetime) -> str:
    return when.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def catalog_job(
    root: Path,
    *,
    rahasher: str | None,
    ra_key: str | None,
    log: Callable[[str], None],
    ra_user: str | None = None,
) -> Callable[[bool], str]:
    """The work of one background run: `catalog` (with RAHasher when one is set) and, when
    asked and a key is set, `catalog --ra-lists`. With a key, every run also does a bounded
    share of `catalog --ra-achievements` (what is fresh costs no call)."""
    hasher = catalog_store.rahasher_runner(rahasher) if rahasher else None

    def job(ra_lists: bool) -> str:
        results = catalog_store.run_catalog(root, dry_run=False, out=log, ra_hasher=hasher)
        failed = sum(1 for r in results if r.failed)
        message = f"{len(results)} system(s)" + (f", {failed} failed" if failed else "")
        if ra_lists and ra_key:
            lists = catalog_store.run_ra_lists(
                root, key=ra_key, force=False, dry_run=False, out=log, warn=log
            )
            message += f"; {len(lists)} RA list(s)"
        if ra_key:
            found = ra_store.run_ra_achievements(
                root,
                key=ra_key,
                user=ra_user,
                force=False,
                dry_run=False,
                out=log,
                warn=log,
                call_limit=ra_store.CALL_BATCH,
                badge_limit=ra_store.BADGE_BATCH,
            )
            message += f"; RA achievements: {found.details_left} game(s) left"
        return message

    return job


# --------------------------------------------------------------------------- the app


class App:
    """The routes of the server, as a function from `Request` to `Response`."""

    def __init__(
        self,
        cfg: Config,
        token: str,
        *,
        root: Path | None = None,
        files: HubFiles | None = None,
        client_factory: Callable[[], Client] | None = None,
        refresher: Refresher | None = None,
        ui_dir: Path = UI_DIR,
        login_delay: float = LOGIN_DELAY,
        progress_seconds: float = PROGRESS_SECONDS,
        progress_failure_seconds: float = PROGRESS_FAILURE_SECONDS,
        log: Callable[[str], None] = lambda text: None,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.cfg = cfg
        self.root = root or Path(cfg.hub.root)
        self.files = files or HubFiles(self.root)
        self._token = token
        self._client_factory = client_factory
        self.refresher = refresher
        if refresher is not None:
            refresher.after = self.files.invalidate
        self.ui_dir = ui_dir
        self.login_delay = login_delay
        self.limiter = FailureLimiter(login_delay)
        self.progress_seconds = progress_seconds
        self.progress_failure_seconds = progress_failure_seconds
        self.log = log
        self.now = now
        self._clock = clock
        self.write_lock = threading.Lock()
        self._progress_lock = threading.Lock()
        self._progress: tuple[float, list[dict[str, Any]]] | None = None
        self._progress_failed: float | None = None  # when the last question failed

    # ------------------------------------------------------------- dispatch

    def handle(self, request: Request) -> Response:
        try:
            response = self._route(request)
        except ApiError as exc:
            response = error_response(exc)
        except store.WhitelistIOError as exc:  # the answer names the file, not the hub's path
            self.log(f"error: {exc}")
            message = f"{exc.path.name} could not be read ({exc.strerror})"
            response = error_response(ApiError("read-failed", message))
        except Exception as exc:  # the answer says nothing; the log names the kind
            self.log(f"internal error: {type(exc).__name__}")
            response = error_response(ApiError("internal", "internal error"))
        return self.secure(response)

    @staticmethod
    def secure(response: Response) -> Response:
        response.headers.append(("X-Content-Type-Options", "nosniff"))
        response.headers.append(("Content-Security-Policy", CSP))
        return response

    def _route(self, request: Request) -> Response:
        path, method = request.path, request.method
        if path == "/healthz":
            if method not in ("GET", "HEAD"):
                raise ApiError("not-found", "no such route")
            return json_response(200, {"ok": True, "version": __version__})
        if path.startswith("/api/"):
            return self._api(request, path)
        return self._static(request, path)

    # ------------------------------------------------------------- authentication

    def _authenticated(self, request: Request) -> str | None:
        """`bearer` or `cookie` when the request carries the token in one of the two ways."""
        auth = request.headers.get("authorization", "")
        scheme, _, given = auth.partition(" ")
        given = given.strip()
        bearer = scheme.lower() == "bearer" and bool(given)
        if bearer and _same(given, self._token):
            return "bearer"
        # Every `wz_session` value is checked: a client may send others it does not know of.
        now = int(self.now().timestamp())
        if any(
            session_valid(self._token, value, now)
            for value in cookie_values(request.headers.get("cookie", ""), COOKIE)
        ):
            return "cookie"
        if bearer:  # a token was offered and it is wrong: the same wait as a wrong login
            self.limiter.failed()
        return None

    def _json_body(self, request: Request) -> object:
        content_type = request.headers.get("content-type", "").split(";")[0].strip().lower()
        if content_type != "application/json":
            raise ApiError("bad-request", "a write must be sent as application/json")
        if request.too_large:
            raise ApiError("bad-request", f"the body is over {library_api.MAX_BODY} bytes")
        try:
            return json.loads(request.body.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            raise ApiError("bad-request", "the body is not valid JSON") from None

    def _session(self, request: Request) -> Response:
        if request.method != "POST":
            raise ApiError("not-found", "no such route")
        body = self._json_body(request)
        if (
            not isinstance(body, dict)
            or set(body) != {"token"}
            or not isinstance(body["token"], str)
        ):
            raise ApiError("bad-request", 'the body must be {"token": "..."}')
        if not _same(body["token"], self._token):
            self.limiter.failed()
            raise ApiError("unauthorized", "wrong token")
        value = session_cookie(self._token, int(self.now().timestamp()) + COOKIE_MAX_AGE)
        cookie = f"{COOKIE}={value}; HttpOnly; SameSite=Strict; Path=/; Max-Age={COOKIE_MAX_AGE}"
        return Response(204, b"", headers=[("Set-Cookie", cookie)])

    # ------------------------------------------------------------- the API

    def _api(self, request: Request, path: str) -> Response:
        if path == "/api/v1/session":
            return self._session(request)
        who = self._authenticated(request)
        if who is None:
            raise ApiError("unauthorized", "a session cookie or a bearer token is needed")
        if request.method not in ("GET", "HEAD", "POST"):
            raise ApiError("not-found", "no such route")
        write = request.method == "POST"
        if write and who == "cookie":
            # The page may be behind a TLS proxy that keeps the Host header: either scheme.
            host = request.headers.get("host", "")
            origin = request.headers.get("origin")
            if not host or origin not in (f"http://{host}", f"https://{host}"):
                raise ApiError("bad-origin", "the request does not come from this page")
        segments = [urllib.parse.unquote(s) for s in path.removeprefix("/api/v1").split("/")[1:]]
        if not path.startswith("/api/v1/") or not segments or "" in segments:
            raise ApiError("not-found", "no such route")
        if write:
            return self._api_write(request, segments)
        if request.method not in ("GET", "HEAD"):
            raise ApiError("not-found", "no such route")
        return self._api_read(request, segments)

    def _api_read(self, request: Request, segments: list[str]) -> Response:
        cfg = self.cfg
        match segments:
            case ["devices"]:
                return json_response(
                    200,
                    library_api.devices_response(cfg, self.files.views(cfg), self._disks()),
                )
            case ["devices", device, "backups"]:
                return self._backups(device)
            case ["devices", device, "backups", name]:
                self._known_device(device)
                return json_response(
                    200, library_api.restore_response(self._restore_plan(device, name))
                )
            case ["systems"]:
                lib = self.files.library()
                return json_response(
                    200, library_api.systems_response(lib, cfg, self.files.views(cfg))
                )
            case ["systems", system, "games"]:
                lib = self.files.library()
                games = library_api.games_response(
                    lib, system, cfg, self.files.views(cfg), self.files.extras
                )
                library_api.attach_play(games["games"], lib, self._play_index())
                return json_response(200, games)
            case ["games", game_id]:
                lib = self.files.library()
                game = library_api.game_response(
                    lib, game_id, cfg, self.files.views(cfg), self.files.extras
                )
                game["play"] = library_api.game_play(self._play_index(), *lib.by_id[game_id])
                game["saves"] = self.files.game_saves(lib, game_id)
                return json_response(200, game)
            case ["recent"]:
                limit = library_api.parse_limit(request.query.get("limit"))
                return json_response(
                    200,
                    library_api.recent_response(
                        self.files.library(), self._play_index(), self.files.extras, limit
                    ),
                )
            case ["recommendations"]:
                return json_response(
                    200,
                    library_api.recommendations_response(
                        self.files.library(),
                        cfg,
                        self.files.views(cfg),
                        self._play_index(),
                        self.files.extras,
                        self._disks(),
                        int(self.now().timestamp()),
                    ),
                )
            case ["games", game_id, "media", kind]:
                return self._media(request, game_id, kind)
            case ["search"]:
                query = request.query.get("q")
                if not query:
                    raise ApiError("bad-request", "the `q` parameter is missing")
                lib = self.files.library()
                return json_response(
                    200, library_api.search_response(lib, cfg, self.files.views(cfg), query)
                )
            case ["ra", "summary"]:
                return json_response(
                    200, library_api.ra_summary_response(self.files.library(), cfg)
                )
            case ["ra", "badges", name, variant]:
                return self._badge(name, variant)
            case ["progress"]:
                return json_response(200, self._progress_entries())
            case ["catalog"]:
                return json_response(200, self._catalog_status())
            case ["queue"]:
                return self._queue_get(request)
        raise ApiError("not-found", "no such route")

    def _api_write(self, request: Request, segments: list[str]) -> Response:
        match segments:
            case ["changes"]:
                return self._changes(self._json_body(request))
            case ["queue"]:
                return self._queue_post(self._json_body(request))
            case ["devices", device, "restore"]:
                self._known_device(device)
                return self._restore(device, self._json_body(request))
            case ["catalog", "refresh"]:
                self._json_body(request)
                if self.refresher is None or not self.refresher.trigger():
                    raise ApiError("catalog-running", "a catalog run is already running")
                return json_response(202, {"started": True})
        raise ApiError("not-found", "no such route")

    def _play_index(self) -> library_api.PlayIndex:
        horizon = int(self.now().timestamp()) + playstate.FUTURE_SLACK
        return library_api.PlayIndex(self.files.play_ledgers(), horizon)

    def _catalog_status(self) -> dict[str, Any]:
        lib = self.files.library()
        status = self.refresher.status() if self.refresher else {"running": False, "last_run": None}
        return {
            "ready": bool(lib.systems),
            "systems": len(lib.systems),
            "games": sum(len(g) for g in lib.systems.values()),
            "updated": lib.updated or None,
            **status,
        }

    # ------------------------------------------------------------- media and static files

    def _media(self, request: Request, game_id: str, kind: str) -> Response:
        lib = self.files.library()
        system, name = library_api.media_file(lib, game_id, kind, self.files.extras)
        path = self.files.media_path(system, kind, name)
        if path is None:
            raise ApiError("not-found", "the image is not available", game=game_id)
        try:
            handle = path.open("rb")
        except OSError:
            raise ApiError("not-found", "the image is not available", game=game_id) from None
        try:
            size = os.fstat(handle.fileno()).st_size
            headers = [("Cache-Control", "private, max-age=3600"), ("Accept-Ranges", "bytes")]
            ctype = CONTENT_TYPES.get(path.suffix.lower(), "application/octet-stream")
            try:
                span = library_api.parse_range(request.headers.get("range"), size)
            except library_api.RangeNotSatisfiable:
                handle.close()
                return Response(416, b"", ctype, [("Content-Range", f"bytes */{size}"), *headers])
            if span is None:
                return Response(200, b"", ctype, headers, handle, size)
            first, last = span
            handle.seek(first)
            headers.append(("Content-Range", f"bytes {first}-{last}/{size}"))
            return Response(206, b"", ctype, headers, handle, last - first + 1)
        except BaseException:
            handle.close()
            raise

    def _badge(self, name: str, variant: str) -> Response:
        path = self.files.badge_path(name, variant)
        if path is None:
            raise ApiError("not-found", "the badge is not available")
        try:
            data = path.read_bytes()
        except OSError:
            raise ApiError("not-found", "the badge is not available") from None
        return Response(200, data, "image/png", [("Cache-Control", "private, max-age=86400")])

    def _static(self, request: Request, path: str) -> Response:
        if request.method not in ("GET", "HEAD"):
            raise ApiError("not-found", "no such route")
        parts = library_api.safe_parts(urllib.parse.unquote(path))
        if parts is None or (parts and path.endswith("/")):
            raise ApiError("not-found", "no such file")
        found = safe_file(self.ui_dir, parts or ("index.html",))
        if found is None:
            raise ApiError("not-found", "no such file")
        try:
            data = found.read_bytes()
        except OSError:
            raise ApiError("not-found", "no such file") from None
        return Response(
            200,
            data,
            CONTENT_TYPES.get(found.suffix.lower(), "application/octet-stream"),
            [("Cache-Control", "no-cache")],
        )

    # ------------------------------------------------------------- changes

    def _changes(self, body: object) -> Response:
        lib = self.files.library()
        plan = library_api.plan_changes(body, self.cfg, lib, self.files.views(self.cfg))
        if plan.dry_run:
            return json_response(200, library_api.changes_response(plan))
        written: dict[str, bool] = {}
        warnings: dict[str, list[str]] = {}
        failure: ApiError | None = None
        # The lock covers the re-check and the writes only: the hub's Syncthing (its client and
        # the scans) is asked after it is released, so a slow or down Syncthing blocks no write.
        with self.write_lock:
            todo = [dp for dp in plan.devices if dp.changed]
            for dp in todo:  # every file is checked before the first one is written
                snapshot = store.read(store.whitelist_path(self.root, dp.device))
                if library_api.api_etag(snapshot.etag) != dp.etag:
                    raise ApiError(
                        "stale",
                        f"{dp.device}: the whitelist changed since it was read",
                        device=dp.device,
                    )
            for dp in todo:
                try:
                    text = dp.new_text.encode("utf-8")

                    def build(_snapshot: store.Snapshot, data: bytes = text) -> bytes:
                        return data

                    store.replace(
                        self.root,
                        dp.device,
                        library_api.raw_etag(dp.etag),
                        build,
                        now=self.now(),
                    )
                except (store.Stale, store.NoLibrary, OSError, WarpzoneError) as exc:
                    stale = isinstance(exc, store.Stale)
                    code = "stale" if stale else "write-failed"
                    message = (
                        f"{dp.device}: the whitelist changed while it was being written"
                        if stale
                        else f"{dp.device}: the whitelist could not be written ({_reason(exc)})"
                    )
                    failure = ApiError(code, message, device=dp.device)
                    break
                written[dp.device] = True
        if failure is not None and not written:
            raise failure
        self._scan_written(list(written), warnings)
        if failure is not None:
            partial = library_api.changes_response(plan, written, warnings)
            partial["error"] = failure.body()["error"]
            return json_response(500, partial)
        return json_response(200, library_api.changes_response(plan, written, warnings))

    # ------------------------------------------------------------- the queue

    def _queue_context(self) -> queue.Context:
        return queue.Context(
            self.cfg,
            self.files.library(),
            self.files.views(self.cfg),
            self._play_index(),
            self.files.extras,
        )

    def _queue_get(self, request: Request) -> Response:
        history = queue.parse_history(request.query.get("history"))
        loaded = self.files.queue()
        body = queue.queue_response(
            loaded.reading,
            loaded.reading.queue,
            loaded.etag,
            loaded.size,
            self._queue_context(),
            int(self.now().timestamp()),
            history,
        )
        return json_response(200, body)

    def _queue_post(self, body: object) -> Response:
        ctx = self._queue_context()
        loaded = queue_store.load(self.root)  # read fresh: the plan decides what is written
        now = self.now()
        stamp = int(now.timestamp())
        plan = queue.plan(body, loaded.reading, loaded.etag, loaded.size, ctx, now)
        if plan.dry_run:
            return json_response(
                200, queue.plan_response(plan, loaded.etag, loaded.size, ctx, stamp)
            )
        written: dict[str, bool] = {}
        warnings: dict[str, list[str]] = {}
        failure: ApiError | None = None
        queue_written = False
        with self.write_lock:
            # Step 1: every file is checked before the first one is written.
            if queue_store.load(self.root).etag != loaded.etag:
                raise ApiError("stale", "the queue changed since it was read", queue=True)
            todo = [dp for dp in plan.devices if dp.changed]
            for name, etag in plan.reads.items():  # every whitelist the plan read, written or not
                snapshot = store.read(store.whitelist_path(self.root, name))
                if library_api.api_etag(snapshot.etag) != etag:
                    raise ApiError(
                        "stale", f"{name}: the whitelist changed since it was read", device=name
                    )
            # Step 4: the queue first, so a failure after it leaves a claim the page can show.
            if plan.first is not None:
                self._write_queue(loaded.etag, plan.first, now)
                queue_written = True
            # Step 5: the whitelists.
            for dp in todo:
                try:
                    text = dp.new_text.encode("utf-8")

                    def build(_snapshot: store.Snapshot, data: bytes = text) -> bytes:
                        return data

                    store.replace(
                        self.root,
                        dp.device,
                        library_api.raw_etag(dp.etag),
                        build,
                        now=now,
                    )
                except (store.Stale, store.NoLibrary, OSError, WarpzoneError) as exc:
                    stale = isinstance(exc, store.Stale)
                    code = "stale" if stale else "write-failed"
                    message = (
                        f"{dp.device}: the whitelist changed while it was being written"
                        if stale
                        else f"{dp.device}: the whitelist could not be written ({_reason(exc)})"
                    )
                    failure = ApiError(code, message, device=dp.device)
                    break
                written[dp.device] = True
            # Step 6: a `remove` that ended claims deletes the entry once every whitelist is out.
            if failure is None and plan.second is not None and plan.first is not None:
                try:
                    self._write_queue(queue.etag_of(plan.first), plan.second, now)
                except ApiError as exc:
                    failure = exc
        self._scan_written(list(written), warnings)
        if failure is not None:
            if not written and not queue_written:
                raise failure
            partial = library_api.changes_response(
                library_api.Plan(False, plan.devices, plan.skipped), written, warnings
            )
            partial["queue"] = queue_written
            partial["error"] = failure.body()["error"]
            return json_response(500, partial)
        answer = queue.plan_response(plan, loaded.etag, loaded.size, ctx, stamp, written, warnings)
        return json_response(200, answer)

    def _write_queue(self, etag: str, data: bytes, now: datetime) -> None:
        """One guarded write of the queue file, under the write lock; `ApiError` when it fails."""
        try:
            queue_store.replace(self.root, etag, data, now)
        except store.Stale:
            raise ApiError(
                "stale", "the queue changed while it was being written", queue=True
            ) from None
        except store.NoLibrary as exc:  # the answer names the folder, the log the hub root
            self.log(f"error: {exc} (hub root {self.root})")
            raise ApiError(
                "write-failed", f"the queue could not be written ({exc})", queue=True
            ) from None
        except (OSError, WarpzoneError) as exc:
            message = f"the queue could not be written ({_reason(exc)})"
            raise ApiError("write-failed", message, queue=True) from None
        finally:
            self.files.invalidate_queue()

    # ------------------------------------------------------------- history and restore

    def _known_device(self, device: str) -> None:
        if device not in {d.name for d in library_api.library_devices(self.cfg)}:
            raise ApiError("not-found", f"no device {device!r}", device=device)

    def _backups(self, device: str) -> Response:
        self._known_device(device)
        snapshot = store.read(store.whitelist_path(self.root, device))
        items = store.entries(self.root, device, snapshot.data)
        view = library_api.view_of(snapshot.data)
        return json_response(200, library_api.backups_response(device, view, items))

    def _restore_plan(self, device: str, name: str) -> library_api.RestorePlan:
        """The current whitelist read once, the backup `name` read, and what restoring does."""
        snapshot = store.read(store.whitelist_path(self.root, device))
        try:
            data = store.read_backup(self.root, device, name)
        except store.BackupNotFound as exc:
            raise ApiError("not-found", str(exc), device=device) from None
        view = library_api.view_of(snapshot.data)
        return library_api.plan_restore(
            device, view, snapshot.data, name, data, self.files.library()
        )

    def _restore(self, device: str, body: object) -> Response:
        request = library_api.parse_restore(body)
        plan = self._restore_plan(device, request.backup)
        library_api.check_restore(plan.etag, request.expect, device)
        if request.dry_run or not plan.changed:
            answer = library_api.restore_response(plan, written=False, dry_run=request.dry_run)
            return json_response(200, answer)
        with self.write_lock:  # the same lock, backup, re-check and atomic write as a change
            try:
                store.replace(
                    self.root,
                    device,
                    library_api.raw_etag(plan.etag),
                    lambda _snapshot: plan.data,
                    now=self.now(),
                )
            except store.Stale:
                raise ApiError(
                    "stale",
                    f"{device}: the whitelist changed while it was being written",
                    device=device,
                ) from None
            except (store.NoLibrary, OSError, WarpzoneError) as exc:
                message = f"{device}: the whitelist could not be written ({_reason(exc)})"
                raise ApiError("write-failed", message, device=device) from None
        warnings: dict[str, list[str]] = {}
        self._scan_written([device], warnings)
        return json_response(
            200, library_api.restore_response(plan, written=True, warnings=warnings.get(device, []))
        )

    def _scan_written(self, devices: list[str], warnings: dict[str, list[str]]) -> None:
        """Ask the hub's Syncthing to scan each written whitelist, without the write lock. A
        failure is a warning: the file is written, and the hub's watcher picks it up anyway."""
        client = self._scan_client() if devices else None
        if client is None:
            return
        for device in devices:
            note = store.scan(client, device)
            if note:
                warnings[device] = [note]
                self.log(f"warning: {note}")

    def _scan_client(self) -> Client | None:
        if self._client_factory is None:
            return None
        try:
            return self._client_factory()
        except WarpzoneError as exc:
            self.log(f"warning: no client for the hub's Syncthing ({_reason(exc)})")
            return None

    # ------------------------------------------------------------- disks

    def _disks(self) -> dict[str, dict[str, Any]]:
        """The disk each device reported through the play state folder (small files, read on
        every request; a missing or stale one is null fields)."""
        names = [d.name for d in library_api.library_devices(self.cfg)]
        folder = self.root / "frontend" / "play-state"
        return diskstatus_io.read_all(folder, names, int(self.now().timestamp()))

    # ------------------------------------------------------------- progress

    def _progress_entries(self) -> list[dict[str, Any]]:
        with self._progress_lock:
            held = self._progress
            if held is not None and self._clock() - held[0] < self.progress_seconds:
                return held[1]
            failed = self._progress_failed
            if failed is not None and self._clock() - failed < self.progress_failure_seconds:
                raise ApiError("hub-unreachable", "the hub's Syncthing did not answer")
            try:
                entries = self._read_progress()
            except ApiError:  # a down Syncthing is not asked again by every poll
                self._progress_failed = self._clock()
                raise
            self._progress_failed = None
            self._progress = (self._clock(), entries)
            return entries

    def _read_progress(self) -> list[dict[str, Any]]:
        unreachable = ApiError("hub-unreachable", "the hub's Syncthing did not answer")
        if self._client_factory is None:
            raise unreachable
        try:
            client = self._client_factory()
            connections = client.connections()
        except WarpzoneError:
            raise unreachable from None
        now = self.now()
        entries = []
        for device in library_api.library_devices(self.cfg):
            completion: dict[str, Any] | None
            try:
                completion = client.completion(LIBRARY_FOLDER, device.device_id)
            except SyncthingError as exc:
                if exc.status != 404:
                    raise unreachable from None
                completion = None  # the folder is not shared with the device
            reading = progress.parse(connections.get(device.device_id), completion)
            wrote = progress.written_at(self.files.backup_names(device.name))
            state = progress.state(
                reading, wrote_at=wrote, rescan_seconds=device.rescan_seconds, now=now
            )
            entries.append(progress.entry(device.name, state, now))
        return entries


def _reason(exc: BaseException) -> str:
    if isinstance(exc, store.NoLibrary):  # its message holds the hub's path
        return "a folder of the hub is not there"
    return getattr(exc, "strerror", None) or str(exc) or type(exc).__name__


# --------------------------------------------------------------------------- sockets


class UiServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(
        self, address: tuple[str, int], app: App, max_connections: int = MAX_CONNECTIONS
    ) -> None:
        self.app = app
        self._slots = threading.BoundedSemaphore(max_connections)
        if ":" in address[0]:
            self.address_family = socket.AF_INET6
        super().__init__(address, _Handler)

    def process_request(self, request: Any, client_address: Any) -> None:
        # One thread per connection, so the number of connections is capped: above it the
        # connection is closed at once, not queued.
        if not self._slots.acquire(blocking=False):
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except BaseException:
            self._slots.release()
            raise

    def process_request_thread(self, request: Any, client_address: Any) -> None:
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._slots.release()

    def handle_error(self, request: Any, client_address: Any) -> None:
        # The default prints a traceback (with the client's address) to stderr.
        kind = type(sys.exc_info()[1]).__name__
        self.app.log(f"connection error: {kind}")

    def server_bind(self) -> None:
        # HTTPServer.server_bind asks DNS for the host's name, which can stall for seconds.
        socketserver.TCPServer.server_bind(self)
        host, port = self.server_address[:2]
        self.server_name, self.server_port = str(host), port


def _printable(text: str) -> str:
    """`text` with every control or non-ASCII character escaped: it comes from a stranger and
    goes to a terminal or a log."""
    return text.encode("unicode_escape").decode("ascii")


class _Handler(BaseHTTPRequestHandler):
    server: UiServer
    server_version = "warpzone"
    sys_version = ""
    timeout = SOCKET_TIMEOUT  # a connection silent for this long, also mid-body, is dropped
    # An error for a request line that cannot be parsed still gets a status line: Python 3.11
    # answers such a line HTTP/0.9 style (a bare body) unless the default version is 1.0.
    default_request_version = "HTTP/1.0"

    def log_message(self, format: str, *args: Any) -> None:
        # Called for errors before a request line was parsed: command and path may not exist.
        if len(args) < 2:
            self.server.app.log(_printable(f"connection: {format % args}")[:300])
            return
        command = getattr(self, "command", None) or "-"
        raw = getattr(self, "path", "") or ""
        try:
            path = urllib.parse.urlsplit(raw).path
        except ValueError:
            path = raw
        self.server.app.log(_printable(f"{command} {path[:200]} {args[1]}"))

    def _send(self, response: Response) -> None:
        self.send_response(response.status)
        if response.status != 204:
            self.send_header("Content-Type", response.content_type)
        length = response.length if response.stream is not None else len(response.body)
        self.send_header("Content-Length", str(length))
        for name, value in response.headers:
            self.send_header(name, value)
        if not response.headers or not any(n == "Cache-Control" for n, _ in response.headers):
            self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        self.end_headers()
        try:
            if self.command == "HEAD":
                return
            if response.stream is None:
                self.wfile.write(response.body)
                return
            left = response.length
            while left > 0:
                chunk = response.stream.read(min(CHUNK, left))
                if not chunk:  # the file shrank: the length was promised, so drop the connection
                    self.close_connection = True
                    break
                self.wfile.write(chunk)
                left -= len(chunk)
        except (BrokenPipeError, ConnectionResetError):
            self.close_connection = True  # a player seeking drops the connection: not an error
        finally:
            if response.stream is not None:
                response.stream.close()

    def _handle(self) -> None:
        length_text = self.headers.get("Content-Length")
        body, too_large = b"", False
        if length_text is not None:
            text = length_text.strip()
            if not (text.isascii() and text.isdigit()):
                bad = ApiError("bad-request", "the Content-Length is not a number")
                self._send(self.server.app.secure(error_response(bad)))
                return
            if len(text) > 9 or int(text) > library_api.MAX_BODY:
                too_large = True
            else:
                try:
                    body = self.rfile.read(int(text))
                except OSError as exc:  # the client stalled or went away mid-body
                    self.server.app.log(f"connection: body not received ({type(exc).__name__})")
                    self.close_connection = True
                    return
        request = Request(
            self.command,
            self.path,
            {k.lower(): v for k, v in self.headers.items()},
            body,
            too_large,
        )
        self._send(self.server.app.handle(request))

    do_GET = do_HEAD = do_POST = do_PUT = do_PATCH = do_DELETE = _handle


# --------------------------------------------------------------------------- running


def run(
    cfg: Config,
    *,
    listen: str | None = None,
    env: Mapping[str, str] | None = None,
    client_factory: Callable[[], Client] | None = None,
    out: Callable[[str], None] = print,
    log: Callable[[str], None] | None = None,
    ready: Callable[[UiServer], None] | None = None,
) -> int:
    """`warpzone serve`: refuse to start without a token (exit 2) or without the hub's
    `library/.stfolder`, then serve until stopped (SIGINT or SIGTERM)."""
    env = os.environ if env is None else env
    token = load_token(env, cfg.serve)
    root = Path(cfg.hub.root)
    if not (root / "library" / ".stfolder").is_dir():
        raise ServeError(
            f"{root / 'library' / '.stfolder'} is not there: is the hub root mounted at "
            "[hub] root, and is library/ a Syncthing folder?"
        )
    address = split_listen(listen or cfg.serve.listen)
    if address is None:
        raise ServeError(f"not a HOST:PORT address: {listen!r}")
    log = log or (lambda text: print(text, flush=True))
    rahasher: str | None = None
    if cfg.inbox.rahasher:
        try:
            rahasher = catalog_store.resolve_rahasher(cfg.inbox.rahasher)
        except UsageError as exc:
            log(f"warning: {exc}; the catalog runs without RA hashing")
    ra_key = env.get(ra.KEY_ENV) or None
    refresher = Refresher(
        catalog_job(root, rahasher=rahasher, ra_key=ra_key, log=log, ra_user=cfg.ra.user), log
    )
    app = App(cfg, token, root=root, client_factory=client_factory, refresher=refresher, log=log)
    try:
        server = UiServer(address, app)
    except OSError as exc:
        raise ServeError(f"cannot listen on {listen or cfg.serve.listen}: {_reason(exc)}") from exc
    stop = threading.Event()
    threading.Thread(
        target=refresher.schedule, args=(stop,), kwargs={"key": ra_key is not None}, daemon=True
    ).start()

    def terminate(signum: int, frame: object) -> None:
        threading.Thread(target=server.shutdown, daemon=True).start()

    if threading.current_thread() is threading.main_thread():
        signal.signal(signal.SIGTERM, terminate)
    shown = server.server_address
    out(f"warpzone serve: listening on {shown[0]!s}:{shown[1]}")
    if ready is not None:
        ready(server)
    try:
        server.serve_forever()
    finally:
        stop.set()
        server.server_close()
    return EXIT_OK
