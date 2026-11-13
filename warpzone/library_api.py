"""The library UI's API as pure functions: request validation, `plan_changes` and the shape of
every answer (see "HTTP API" in development/design/library-ui.md).

Nothing here touches the disk, the network or a socket. `serve.py` reads the catalog, the
gamelists and the whitelists and hands them in as plain data (`Library`, `SystemExtras`,
`DeviceView`), and writes the files `plan_changes` decided on.
"""

from __future__ import annotations

import json
import posixpath
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from functools import cached_property
from typing import Any

from warpzone import artwork, catalog, diskstatus, inbox_lock, playstate, ra_data, recommend
from warpzone import whitelist as wl
from warpzone import whitelist_history as hist
from warpzone.config import Config, Device
from warpzone.inbox_unattended import STABLE_SECONDS, Entry, State

MAX_BODY = 64 * 1024
MAX_CHANGES = 1000
RECENT_DEFAULT = 20
RECENT_MAX = 100
INBOX_RUNS_SHOWN = 10  # run records listed by `GET /api/v1/inbox`
SEARCH_LIMIT = 100
MEDIA_KINDS = (
    "covers",
    "miximages",
    "screenshots",
    "titlescreens",
    "marquees",
    "3dboxes",
    "physicalmedia",
    "backcovers",
    "videos",
)
MEDIA_EXTENSIONS = ("png", "jpg", "jpeg")
VIDEO_EXTENSIONS = ("mp4", "webm")
# The media kinds that are videos (served with `Range`); every other kind is an image.
VIDEO_KINDS = ("videos",)
META_FIELDS = (
    "name",
    "desc",
    "rating",
    "releasedate",
    "developer",
    "publisher",
    "genre",
    "players",
)
# The state of a whitelist file, as the API says it.
MANAGED, NEEDS_MIGRATION, MISSING = "managed", "needs-migration", "missing"

STATUS = {
    "bad-request": 400,
    "unauthorized": 401,
    "bad-origin": 403,
    "not-found": 404,
    "not-managed": 409,
    "covered": 409,
    "cannot-apply": 409,
    "unlistable": 409,
    "not-restorable": 409,
    "catalog-running": 409,
    "stale": 412,
    "unsupported": 422,
    "expect-missing": 428,
    "too-many-attempts": 429,
    "write-failed": 500,
    "read-failed": 500,
    "internal": 500,
    "hub-unreachable": 503,
}


class ApiError(Exception):
    """An answer that is an error: `{"error": {"code", "message", "device", "game"}}`."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        device: str | None = None,
        game: str | None = None,
        queue: bool = False,
        retry_after: int | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.device = device
        self.game = game
        self.queue = queue  # the error is about the queue file, not a whitelist
        self.retry_after = retry_after  # seconds, sent as `Retry-After`

    @property
    def status(self) -> int:
        return STATUS.get(self.code, 409)

    def body(self) -> dict[str, Any]:
        error: dict[str, Any] = {"code": self.code, "message": self.message}
        if self.device is not None:
            error["device"] = self.device
        if self.game is not None:
            error["game"] = self.game
        if self.queue:
            error["queue"] = True
        return {"error": error}


# --------------------------------------------------------------------------- the data


@dataclass(frozen=True)
class Library:
    """The catalog as the server holds it: the games of each system and the cached RA lists."""

    systems: Mapping[str, Mapping[str, catalog.Game]]
    ra_lists: Mapping[int, catalog.RaList] = field(default_factory=dict)
    updated: str = ""
    ra_cache: ra_data.RaData = field(default_factory=ra_data.RaData)

    @cached_property
    def by_id(self) -> dict[str, tuple[str, catalog.Game]]:
        return {g.id: (s, g) for s, games in self.systems.items() for g in games.values()}

    @cached_property
    def wl_games(self) -> tuple[wl.Game, ...]:
        return tuple(self.wl_by_id.values())

    @cached_property
    def wl_by_id(self) -> dict[str, wl.Game]:
        return {
            g.id: wl.Game(g.id, system, tuple(p.path for p in g.parts))
            for system, games in sorted(self.systems.items())
            for g in games.values()
        }

    @cached_property
    def wl_by_system(self) -> dict[str, tuple[wl.Game, ...]]:
        return {
            system: tuple(self.wl_by_id[i] for i in games) for system, games in self.systems.items()
        }

    @cached_property
    def by_path(self) -> dict[str, str]:
        """Game id by the path of one of its parts, relative to `library/`; a path that is the
        primary part of a game gives that game."""
        found: dict[str, str] = {}
        for _, games in sorted(self.systems.items()):
            for game in games.values():
                for part in game.parts:
                    found.setdefault(part.path, game.id)
        for _, games in self.systems.items():
            for game in games.values():
                found[game.primary] = game.id
        return found

    @cached_property
    def by_key(self) -> dict[str, list[tuple[str, catalog.Game]]]:
        found: dict[str, list[tuple[str, catalog.Game]]] = {}
        for system, games in self.systems.items():
            for game in games.values():
                found.setdefault(game.key, []).append((system, game))
        return found

    def size_of(self, game_ids: Sequence[str]) -> int:
        return sum(self.by_id[i][1].size for i in game_ids if i in self.by_id)


@dataclass(frozen=True)
class SystemExtras:
    """What the server reads beside the catalog for one system.

    `meta`: the system's gamelist entries by their `<path>`; `media`: for each stem the files
    below `frontend/media/<system>/<kind>/` by kind; `unclaimed`: the Switch update and DLC
    folders no base game claims."""

    meta: Mapping[str, artwork.Entry] = field(default_factory=dict)
    media: Mapping[str, Mapping[str, str]] = field(default_factory=dict)
    unclaimed: tuple[str, ...] = ()


Extras = Callable[[str], SystemExtras]


@dataclass(frozen=True)
class DeviceView:
    """One device's whitelist as read: its state, `etag` (`sha256:<hex>` or `absent`), the
    model it means (None when it cannot be known) and its text."""

    state: str
    etag: str
    model: wl.Model | None
    text: str | None = None


def api_etag(raw: str) -> str:
    """The API's form of a file's etag: `absent` or `sha256:<hex>` (`whitelist_store` and
    `whitelist.etag` give the bare hex)."""
    return raw if raw == "absent" else f"sha256:{raw}"


def raw_etag(etag: str) -> str:
    return etag.removeprefix("sha256:")


def view_of(data: bytes | None) -> DeviceView:
    """The `DeviceView` of a whitelist file's bytes (None: the file does not exist)."""
    if data is None:
        return DeviceView(MISSING, "absent", wl.Model(), None)
    etag = api_etag(wl.etag(data))
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return DeviceView(NEEDS_MIGRATION, etag, None, None)
    try:
        reading = wl.read(text)
    except (wl.Unlistable, wl.ParseError, ValueError):
        return DeviceView(NEEDS_MIGRATION, etag, None, text)
    return DeviceView(reading.status, etag, reading.model, text)


def library_devices(cfg: Config) -> list[Device]:
    """The devices that list `library`, in name order."""
    return [d for _, d in sorted(cfg.devices.items()) if "library" in d.folders]


# --------------------------------------------------------------------------- states


def game_state(device: Device, view: DeviceView, system: str, game: catalog.Game) -> str:
    """`on`, `off`, `partial`, `unsupported` (the system is in `exclude_systems`) or `unknown`
    (a whitelist the migration's parser cannot read)."""
    if system in device.exclude_systems:
        return "unsupported"
    if view.model is None:
        return "unknown"
    return wl.game_state(view.model, [p.path for p in game.parts])


def _devices_of(
    cfg: Config, views: Mapping[str, DeviceView], system: str, game: catalog.Game
) -> dict[str, str]:
    return {d.name: game_state(d, views[d.name], system, game) for d in library_devices(cfg)}


def _system_devices(
    lib: Library, cfg: Config, views: Mapping[str, DeviceView], system: str
) -> dict[str, dict[str, Any]]:
    found: dict[str, dict[str, Any]] = {}
    games = lib.wl_by_system.get(system, ())
    for device in library_devices(cfg):
        view = views[device.name]
        whole = view.model is not None and system in view.model.systems
        if system in device.exclude_systems:
            state = "unsupported"
        elif view.model is None:
            state = "unknown"
        else:
            state = wl.system_state(view.model, games)
        found[device.name] = {"state": state, "whole": whole}
    return found


# --------------------------------------------------------------------------- media and meta


def gamelist_paths(game: catalog.Game) -> list[str]:
    """The `<path>` values a frontend may record for the game, best first: the catalog's own
    `gamelist`, then the same path with its leading folders dropped one by one.

    The catalog writes the path below the hub's system folder, but a frontend may read a
    folder below it (`shortcuts/` for PS4, `carts/` for pico-8: `artwork.FrontendSystem.base`)
    and then records, and names its media, without that part. The frontend's system list,
    which says which folder, is on the devices and not on the hub, so the server tries the
    shorter paths."""
    rel = artwork.relative(game.gamelist)
    if rel is None:
        return [game.gamelist]
    parts = rel.split("/")
    found = [game.gamelist]
    found.extend(f"./{'/'.join(parts[i:])}" for i in range(1, len(parts)))
    return found


def game_entry(game: catalog.Game, extras: SystemExtras) -> artwork.Entry | None:
    """The game's gamelist entry (see `gamelist_paths` for where it is looked for)."""
    for path in gamelist_paths(game):
        entry = extras.meta.get(path)
        if entry is not None:
            return entry
    return None


def media_stem(path: str, entry: artwork.Entry | None) -> str | None:
    """The stem media files carry (`artwork.build`'s rule): the gamelist path without `./`
    and without its extension; a `<folder>` entry keeps its whole name."""
    rel = artwork.relative(path)
    if rel is None:
        return None
    return rel if entry is not None and entry.tag == "folder" else posixpath.splitext(rel)[0]


def game_media(game: catalog.Game, extras: SystemExtras) -> dict[str, str]:
    """The kinds the game has an image of, with the file below `<kind>/` for each. The stem
    is that of the game's gamelist entry; without one, the first of `gamelist_paths` that has
    media."""
    entry = game_entry(game, extras)
    paths = [entry.path] if entry is not None else gamelist_paths(game)
    for path in paths:
        stem = media_stem(path, entry)
        if stem is not None and stem in extras.media:
            return dict(extras.media[stem])
    return {}


# The regions No-Intro and Redump put in a file name tag, as written there.
REGIONS = frozenset(
    [
        "Argentina",
        "Asia",
        "Australia",
        "Austria",
        "Belgium",
        "Brazil",
        "Canada",
        "Chile",
        "China",
        "Croatia",
        "Czech",
        "Denmark",
        "Europe",
        "Finland",
        "France",
        "Germany",
        "Greece",
        "Hong Kong",
        "Hungary",
        "India",
        "Ireland",
        "Israel",
        "Italy",
        "Japan",
        "Korea",
        "Latin America",
        "Mexico",
        "Netherlands",
        "New Zealand",
        "Norway",
        "Poland",
        "Portugal",
        "Russia",
        "Scandinavia",
        "South Africa",
        "Spain",
        "Sweden",
        "Switzerland",
        "Taiwan",
        "Turkey",
        "UK",
        "Ukraine",
        "United Arab Emirates",
        "USA",
        "World",
    ]
)
_TAGS = re.compile(r"\(([^()]*)\)")
_LANGUAGES = re.compile(r"[A-Z][a-z](?:-[A-Za-z]+)?(?:,[A-Z][a-z](?:-[A-Za-z]+)?)*")


def name_tags(name: str) -> tuple[list[str], list[str]]:
    """(regions, languages) from the parenthesised tags of a No-Intro or Redump name, as in
    `Example Game (USA, Europe) (En,Fr,De,Es)`: regions as written (`USA`, `Europe`), language
    codes with the first subtag in lower case (`en`, `fr`, `zh-Hant`). Tags that are neither
    (`Rev 1`, `Disc 2`, `Beta`) are skipped; a name with no such tag gives empty lists."""
    regions: list[str] = []
    languages: list[str] = []
    for tag in _TAGS.findall(name):
        parts = [p.strip() for p in tag.split(",")]
        if all(p in REGIONS for p in parts):
            regions.extend(p for p in parts if p not in regions)
        elif _LANGUAGES.fullmatch(tag.strip()):
            for code in tag.strip().split(","):
                lang = code[:2].lower() + code[2:]
                if lang not in languages:
                    languages.append(lang)
    return regions, languages


def game_facts(game: catalog.Game) -> dict[str, Any]:
    """`region` and `languages` from the name tags (the DAT's name when `inbox` recorded one,
    else the file name of the primary), and `verified: true` when `inbox` matched the dump to
    a DAT by content (`dat` is set). `verified` is left out otherwise, not false: a game filed
    before `inbox` kept the DAT name, or by hand, was not checked, which is not the same as
    failing the check."""
    regions, languages = name_tags(game.dat or posixpath.basename(game.primary))
    out: dict[str, Any] = {"region": regions, "languages": languages}
    if game.dat:
        out["verified"] = True
    return out


def game_meta(game: catalog.Game, extras: SystemExtras) -> dict[str, str]:
    entry = game_entry(game, extras)
    if entry is None:
        return {}
    return {k: entry.fields[k] for k in META_FIELDS if entry.fields.get(k)}


def media_file(lib: Library, game_id: str, kind: str, extras: Extras) -> tuple[str, str]:
    """(system, file below `frontend/media/<system>/<kind>/`) of the game's image of `kind`;
    `not-found` for an unknown game or kind, or a game with no image of it."""
    found = lib.by_id.get(game_id)
    if found is None:
        raise ApiError("not-found", f"no game {game_id!r}", game=game_id)
    system, game = found
    if kind not in MEDIA_KINDS:
        raise ApiError("not-found", f"no media kind {kind!r}", game=game_id)
    name = game_media(game, extras(system)).get(kind)
    if name is None:
        raise ApiError("not-found", f"the game has no {kind} image", game=game_id)
    return system, name


def kind_extensions(kind: str) -> tuple[str, ...]:
    """The file extensions that count for a media kind, best first (`videos` are `mp4` and
    `webm`, the other kinds are images)."""
    return VIDEO_EXTENSIONS if kind in VIDEO_KINDS else MEDIA_EXTENSIONS


def media_index(listing: Mapping[str, Sequence[str]]) -> dict[str, dict[str, str]]:
    """Stem -> kind -> file, from the file names found below each kind's folder (relative
    paths with `/`). Only the kinds and extensions the UI knows count; where a stem has more
    than one extension, `png` wins over `jpg` over `jpeg`."""
    index: dict[str, dict[str, str]] = {}
    for kind in MEDIA_KINDS:
        best: dict[str, tuple[int, str]] = {}
        for name in listing.get(kind, ()):
            stem, dot, ext = name.rpartition(".")
            allowed = kind_extensions(kind)
            if not dot or not stem or ext.lower() not in allowed:
                continue
            rank = allowed.index(ext.lower())
            if stem not in best or rank < best[stem][0]:
                best[stem] = (rank, name)
        for stem, (_, name) in best.items():
            index.setdefault(stem, {})[kind] = name
    return index


# --------------------------------------------------------------------------- byte ranges


class RangeNotSatisfiable(Exception):
    """A `Range` header that asks for bytes the file does not have (HTTP `416`)."""


def parse_range(header: str | None, size: int) -> tuple[int, int] | None:
    """The `(first, last)` bytes (both inclusive) that a `Range` header asks of a file of
    `size` bytes, or None to answer the whole file (no header, another unit, several ranges or
    a header that is not a range: all of those are allowed to be ignored). One range only:
    `bytes=a-b`, `bytes=a-` and `bytes=-n` (the last n bytes). A `last` past the end is cut to
    the end. Raises `RangeNotSatisfiable` when the range starts at or past the end, or asks for
    the last 0 bytes, or the file is empty."""
    if header is None:
        return None
    unit, eq, spec = header.strip().partition("=")
    if unit.strip().lower() != "bytes" or not eq or "," in spec:
        return None
    first_text, dash, last_text = spec.strip().partition("-")
    first_text, last_text = first_text.strip(), last_text.strip()
    if not dash or not (first_text or last_text):
        return None
    if not all(t.isascii() and (t.isdigit() or t == "") for t in (first_text, last_text)):
        return None
    if not first_text:  # the last n bytes
        count = int(last_text)
        if count == 0 or size == 0:
            raise RangeNotSatisfiable
        return max(0, size - count), size - 1
    first = int(first_text)
    last = int(last_text) if last_text else size - 1
    if last_text and last < first:
        return None  # an invalid range is ignored (RFC 9110, 14.1.2)
    if first >= size:
        raise RangeNotSatisfiable
    return first, min(last, size - 1)


# --------------------------------------------------------------------------- paths


def safe_parts(path: str) -> tuple[str, ...] | None:
    """The components of an already URL-decoded request path, or None when it is not safe to
    join to a folder: a doubled or leading extra slash (an absolute path), `.`, `..`, a hidden
    name, a backslash or a NUL. A trailing slash is allowed (the folder); `/` is no parts."""
    if not path.startswith("/") or "\0" in path or "\\" in path:
        return None
    parts = path[1:].split("/")
    if parts and parts[-1] == "":
        parts.pop()
    if any(not p or p.startswith(".") for p in parts):
        return None
    return tuple(parts)


# --------------------------------------------------------------------------- reads


def devices_response(
    cfg: Config,
    views: Mapping[str, DeviceView],
    disks: Mapping[str, Mapping[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """`disks` is the `disk` object of each device (`diskstatus.api_disk`); a device without
    one gets null fields."""
    return [
        {
            "name": d.name,
            "profile": d.profile,
            "exclude_systems": list(d.exclude_systems),
            "whitelist": {"state": views[d.name].state, "etag": views[d.name].etag},
            "disk": dict((disks or {}).get(d.name) or diskstatus.api_disk(None, 0)),
        }
        for d in library_devices(cfg)
    ]


def systems_response(
    lib: Library, cfg: Config, views: Mapping[str, DeviceView]
) -> list[dict[str, Any]]:
    return [
        {
            "system": system,
            "games": len(games),
            "size": sum(g.size for g in games.values()),
            "devices": _system_devices(lib, cfg, views, system),
        }
        for system, games in sorted(lib.systems.items())
    ]


def _compact(
    lib: Library,
    cfg: Config,
    views: Mapping[str, DeviceView],
    system: str,
    game: catalog.Game,
    extras: SystemExtras,
) -> dict[str, Any]:
    entry = game_entry(game, extras)
    return {
        "id": game.id,
        "title": game.title,
        "name": (entry.fields.get("name") if entry else None) or game.title,
        "size": game.size,
        "added": game.added,
        "cover": "covers" in game_media(game, extras),
        "ra": catalog.ra_state(system, game.ra, lib.ra_lists).state,
        "flags": list(game.flags),
        "devices": _devices_of(cfg, views, system, game),
        **game_facts(game),
    }


def games_response(
    lib: Library, system: str, cfg: Config, views: Mapping[str, DeviceView], extras: Extras
) -> dict[str, Any]:
    games = lib.systems.get(system)
    if games is None:
        raise ApiError("not-found", f"no system {system!r}")
    extra = extras(system)
    return {
        "system": system,
        "games": [
            _compact(lib, cfg, views, system, g, extra)
            for g in sorted(games.values(), key=lambda g: (g.title.casefold(), g.primary))
        ],
        "unclaimed": list(extra.unclaimed),
    }


def game_response(
    lib: Library, game_id: str, cfg: Config, views: Mapping[str, DeviceView], extras: Extras
) -> dict[str, Any]:
    found = lib.by_id.get(game_id)
    if found is None:
        raise ApiError("not-found", f"no game {game_id!r}", game=game_id)
    system, game = found
    extra = extras(system)
    state = catalog.ra_state(system, game.ra, lib.ra_lists)
    out = _compact(lib, cfg, views, system, game, extra)
    out.update(
        {
            "system": system,
            "primary": game.primary,
            "parts": [{"path": p.path, "type": p.type, "size": p.size} for p in game.parts],
            "dat": game.dat,
            "meta": game_meta(game, extra),
            "media": [k for k in MEDIA_KINDS if k in game_media(game, extra)],
            "ra": {
                "state": state.state,
                "achievements": state.achievements,
                "points": state.points,
                "url": state.url,
            },
            "achievements": _achievements(lib, system, game, cfg),
            "versions": [
                {"id": g.id, "system": s, "title": g.title}
                for s, g in sorted(lib.by_key.get(game.key, ()), key=lambda sg: (sg[0], sg[1].id))
                if g.id != game.id
            ],
        }
    )
    return out


def _achievements(lib: Library, system: str, game: catalog.Game, cfg: Config) -> Any:
    """`achievements` of a game: null unless RetroAchievements has some for it."""
    found = ra_data.ra_game(system, game.ra, lib.ra_lists)
    if found is None:
        return None
    return ra_data.game_view(found, lib.ra_cache, cfg.ra.user)


def ra_summary_response(lib: Library, cfg: Config) -> dict[str, Any]:
    """`GET /api/v1/ra/summary`: the RA games of the library and the user's progress in them."""
    return ra_data.summary_view(lib.systems, lib.ra_lists, lib.ra_cache, cfg.ra.user)


def search_response(
    lib: Library, cfg: Config, views: Mapping[str, DeviceView], query: str
) -> dict[str, Any]:
    pairs = ((s, g) for s, games in lib.systems.items() for g in games.values())
    groups = catalog.search(query, pairs, limit=SEARCH_LIMIT)
    return {
        "query": query,
        "groups": [
            {
                "key": group.key,
                "title": group.title,
                "games": [
                    {
                        "id": g.id,
                        "system": s,
                        "title": g.title,
                        "devices": _devices_of(cfg, views, s, g),
                    }
                    for s, g in group.games
                ],
            }
            for group in groups
        ],
    }


# --------------------------------------------------------------------------- changes


@dataclass(frozen=True)
class ChangeItem:
    device: str  # a device name, or `*`
    system: str
    game: str | None
    on: bool


@dataclass(frozen=True)
class ChangeRequest:
    changes: tuple[ChangeItem, ...]
    expect: Mapping[str, str]
    dry_run: bool


def _bad(message: str) -> ApiError:
    return ApiError("bad-request", message)


def parse_changes(body: object) -> ChangeRequest:
    """The shape of a `POST /api/v1/changes` body; `bad-request` for anything else."""
    if not isinstance(body, dict):
        raise _bad("the body must be a JSON object")
    unknown = sorted(set(body) - {"changes", "expect", "dry_run"})
    if unknown:
        raise _bad(f"unknown key {unknown[0]!r}")
    raw = body.get("changes")
    if not isinstance(raw, list) or not raw:
        raise _bad("`changes` must be a non-empty list")
    if len(raw) > MAX_CHANGES:
        raise _bad(f"`changes` holds more than {MAX_CHANGES} items")
    items = []
    for number, item in enumerate(raw, 1):
        where = f"changes[{number}]"
        if not isinstance(item, dict):
            raise _bad(f"{where} must be an object")
        extra = sorted(set(item) - {"device", "system", "game", "on"})
        if extra:
            raise _bad(f"{where}: unknown key {extra[0]!r}")
        device, system, game, on = (item.get(k) for k in ("device", "system", "game", "on"))
        if not isinstance(device, str) or not device:
            raise _bad(f"{where}.device must be a device name or *")
        if not isinstance(system, str) or not system:
            raise _bad(f"{where}.system must be a system name")
        if game is not None and (not isinstance(game, str) or not game):
            raise _bad(f"{where}.game must be a game id")
        if not isinstance(on, bool):
            raise _bad(f"{where}.on must be true or false")
        items.append(ChangeItem(device, system, game, on))
    expect = body.get("expect", {})
    if not isinstance(expect, dict) or not all(
        isinstance(k, str) and isinstance(v, str) for k, v in expect.items()
    ):
        raise _bad("`expect` must map device names to etags")
    dry_run = body.get("dry_run", False)
    if not isinstance(dry_run, bool):
        raise _bad("`dry_run` must be true or false")
    return ChangeRequest(tuple(items), dict(expect), dry_run)


_HEX64 = re.compile(r"^[0-9a-f]{64}$")


def canonical_etag(value: str) -> str:
    return f"sha256:{value}" if _HEX64.match(value) else value


@dataclass(frozen=True)
class DevicePlan:
    """The outcome of the changes for one device: the file to write and what it does."""

    device: str
    etag: str  # of the file the plan was computed from
    new_text: str
    new_etag: str
    changed: bool  # False: the file already is what the changes ask for
    turned_on: tuple[str, ...]
    turned_off: tuple[str, ...]
    bytes_on: int
    bytes_off: int


@dataclass(frozen=True)
class Plan:
    dry_run: bool
    devices: tuple[DevicePlan, ...]
    skipped: tuple[dict[str, Any], ...] = ()


def plan_changes(body: object, cfg: Config, lib: Library, views: Mapping[str, DeviceView]) -> Plan:
    """Check a changes request and compute the new whitelist of each device it touches.

    Raises the first `ApiError`, in the order of the design: the shape of the body; every
    device, system and game is known; each device's whitelist is managed; an `expect` for
    every touched device that equals its etag; nothing of an unsupported system turned on for
    a named device; each change applies and has the effect asked for. Nothing is written
    here, and a request that fails writes nothing."""
    request = parse_changes(body)
    devices = {d.name: d for d in library_devices(cfg)}
    for item in request.changes:
        if item.device != "*" and item.device not in devices:
            raise ApiError("not-found", f"no device {item.device!r}", device=item.device)
        games = lib.systems.get(item.system)
        if games is None:
            raise ApiError("not-found", f"no system {item.system!r}")
        if item.game is not None and item.game not in games:
            raise ApiError("not-found", f"no game {item.game!r} in {item.system!r}", game=item.game)

    per_device: dict[str, list[ChangeItem]] = {}
    skipped: list[dict[str, Any]] = []
    for item in request.changes:
        for name in sorted(devices) if item.device == "*" else [item.device]:
            if item.device == "*" and item.system in devices[name].exclude_systems:
                note: dict[str, Any] = {
                    "device": name,
                    "system": item.system,
                    "reason": "unsupported",
                }
                if item.game is not None:
                    note["game"] = item.game
                if note not in skipped:
                    skipped.append(note)
                continue
            per_device.setdefault(name, []).append(item)

    for name in per_device:
        if views[name].state == NEEDS_MIGRATION:
            raise ApiError(
                "not-managed",
                f"{name}: the whitelist is not in the UI's format "
                "(run `warpzone whitelist migrate`)",
                device=name,
            )
    for name in per_device:
        if name not in request.expect:
            raise ApiError("expect-missing", f"no `expect` for {name!r}", device=name)
    for name in per_device:
        if canonical_etag(request.expect[name]) != views[name].etag:
            raise ApiError("stale", f"{name}: the whitelist changed since it was read", device=name)
    for name, items in per_device.items():
        for item in items:
            if item.on and item.device != "*" and item.system in devices[name].exclude_systems:
                raise ApiError(
                    "unsupported",
                    f"{name} does not run {item.system!r}",
                    device=name,
                    game=item.game,
                )

    plans = tuple(
        _plan_device(devices[name], views[name], items, lib) for name, items in per_device.items()
    )
    return Plan(request.dry_run, plans, tuple(skipped))


def apply_item(device: Device, model: wl.Model, item: ChangeItem, lib: Library) -> wl.Model:
    """Apply one change item to a device's model, checking its effect; `ApiError` when it does
    not hold. The queue plans its whitelist effects with this, so they obey the same rules."""
    change: wl.Change
    if item.game is None:
        change = wl.SystemChange(item.system, item.on)
    else:
        game = lib.systems[item.system][item.game]
        if catalog.UNLISTABLE in game.flags:
            raise ApiError(
                "unlistable",
                f"{game.title!r} has a path that cannot be written as a whitelist line",
                device=device.name,
                game=item.game,
            )
        change = wl.GameChange(lib.wl_by_id[item.game], item.on)
    try:
        return wl.apply(model, change, lib.wl_games, device.exclude_systems).model
    except wl.Refused as exc:
        raise ApiError(exc.code, exc.message, device=device.name, game=item.game) from None


def finish_device(device: Device, view: DeviceView, model: wl.Model, lib: Library) -> DevicePlan:
    """The `DevicePlan` of a device whose whitelist `view` becomes `model`."""
    assert view.model is not None  # a managed or missing whitelist always has a model
    try:
        text = wl.render(model)
    except wl.Unlistable as exc:
        raise ApiError("unlistable", str(exc), device=device.name) from None
    effect = wl.effect(view.model, model, lib.wl_games)
    return DevicePlan(
        device=device.name,
        etag=view.etag,
        new_text=text,
        new_etag=api_etag(wl.etag(text.encode("utf-8"))),
        changed=text != view.text,
        turned_on=effect.turned_on,
        turned_off=effect.turned_off,
        bytes_on=lib.size_of(effect.turned_on),
        bytes_off=lib.size_of(effect.turned_off),
    )


def _plan_device(
    device: Device, view: DeviceView, items: Sequence[ChangeItem], lib: Library
) -> DevicePlan:
    assert view.model is not None  # a managed or missing whitelist always has a model
    model = view.model
    for item in items:
        model = apply_item(device, model, item, lib)
    return finish_device(device, view, model, lib)


def changes_response(
    plan: Plan,
    written: Mapping[str, bool] | None = None,
    warnings: Mapping[str, Sequence[str]] | None = None,
) -> dict[str, Any]:
    """The answer to a changes request. `written` says which devices' files were written (none
    for a dry run); the etag is the new file's for those and the current one for the rest."""
    done = written or {}
    notes = warnings or {}
    devices = []
    for dp in plan.devices:
        was_written = done.get(dp.device, False)
        item: dict[str, Any] = {
            "device": dp.device,
            "written": was_written,
            "etag": dp.new_etag if was_written else dp.etag,
            "on": list(dp.turned_on),
            "off": list(dp.turned_off),
            "bytes_on": dp.bytes_on,
            "bytes_off": dp.bytes_off,
        }
        if notes.get(dp.device):
            item["warnings"] = list(notes[dp.device])
        devices.append(item)
    return {"dry_run": plan.dry_run, "devices": devices, "skipped": list(plan.skipped)}


# --------------------------------------------------------------------------- history


def _iso(when: datetime) -> str:
    return when.strftime("%Y-%m-%dT%H:%M:%SZ")


def backups_response(device: str, view: DeviceView, items: Sequence[hist.Entry]) -> dict[str, Any]:
    """The backups of a device's whitelist, newest first, with the `etag` of the current file
    they were compared with."""
    return {
        "device": device,
        "etag": view.etag,
        "backups": [
            {
                "name": item.name,
                "time": _iso(item.when),
                "size": item.size,
                "empty": item.empty,
                "same": item.same,
                "included": len(item.delta.included),
                "excluded": len(item.delta.excluded),
                "summary": hist.describe(item),
            }
            for item in items
        ],
    }


@dataclass(frozen=True)
class RestoreRequest:
    backup: str
    expect: str | None
    dry_run: bool


def parse_restore(body: object) -> RestoreRequest:
    """The shape of a `POST /api/v1/devices/{device}/restore` body; `bad-request` otherwise."""
    if not isinstance(body, dict):
        raise _bad("the body must be a JSON object")
    unknown = sorted(set(body) - {"backup", "expect", "dry_run"})
    if unknown:
        raise _bad(f"unknown key {unknown[0]!r}")
    backup = body.get("backup")
    if not isinstance(backup, str) or not hist.is_backup_name(backup):
        raise _bad("`backup` must be the name of a backup")
    expect = body.get("expect")
    if expect is not None and not isinstance(expect, str):
        raise _bad("`expect` must be the etag of the current whitelist")
    dry_run = body.get("dry_run", False)
    if not isinstance(dry_run, bool):
        raise _bad("`dry_run` must be true or false")
    return RestoreRequest(backup, expect, dry_run)


@dataclass(frozen=True)
class RestorePlan:
    """What restoring one backup over the device's current whitelist does."""

    device: str
    etag: str  # of the file the plan was computed from
    new_etag: str
    data: bytes
    item: hist.Entry
    diff: tuple[str, ...]
    managed: bool  # the backup is in the UI's format (a hand-written one makes the UI read-only)
    on: tuple[str, ...] | None  # games it turns on and off; None when a side cannot be read
    off: tuple[str, ...] | None
    bytes_on: int
    bytes_off: int

    @property
    def changed(self) -> bool:
        return not self.item.same


def plan_restore(
    device: str,
    view: DeviceView,
    current: bytes | None,
    name: str,
    data: bytes,
    lib: Library,
) -> RestorePlan:
    """Compare the backup `name` (holding `data`) with the current whitelist (`current`, whose
    view is `view`). Raises `not-restorable` for an empty backup. Pure: nothing is written."""
    item = hist.entry(name, data, current)
    why = hist.refusal(item)
    if why:
        raise ApiError("not-restorable", why, device=device)
    after = view_of(data)
    on: tuple[str, ...] | None = None
    off: tuple[str, ...] | None = None
    size_on = size_off = 0
    if view.model is not None and after.model is not None:
        effect = wl.effect(view.model, after.model, lib.wl_games)
        on, off = effect.turned_on, effect.turned_off
        size_on, size_off = lib.size_of(on), lib.size_of(off)
    return RestorePlan(
        device=device,
        etag=view.etag,
        new_etag=after.etag,
        data=data,
        item=item,
        diff=tuple(hist.unified_diff(hist.text_of(current), hist.text_of(data), name)),
        managed=after.state == MANAGED,
        on=on,
        off=off,
        bytes_on=size_on,
        bytes_off=size_off,
    )


def check_restore(etag: str, expect: str | None, device: str) -> None:
    """The conflict check of a restore: `expect-missing` without an etag, `stale` when it is not
    the current file's (`etag`, in the API's form)."""
    if not expect:
        raise ApiError("expect-missing", f"no `expect` for {device!r}", device=device)
    if canonical_etag(expect) != etag:
        raise ApiError("stale", f"{device}: the whitelist changed since it was read", device=device)


def restore_response(
    plan: RestorePlan,
    *,
    written: bool | None = None,
    dry_run: bool = False,
    warnings: Sequence[str] = (),
) -> dict[str, Any]:
    """The answer for one backup: the detail (`GET`, `written` None) or the outcome of a
    restore. The etag is the new file's once it is written, else the current one."""
    body: dict[str, Any] = {
        "device": plan.device,
        "backup": plan.item.name,
        "time": _iso(plan.item.when),
        "etag": plan.new_etag if written else plan.etag,
        "same": plan.item.same,
        "managed": plan.managed,
        "diff": list(plan.diff),
        "included": list(plan.item.delta.included),
        "excluded": list(plan.item.delta.excluded),
        "on": None if plan.on is None else list(plan.on),
        "off": None if plan.off is None else list(plan.off),
        "bytes_on": plan.bytes_on,
        "bytes_off": plan.bytes_off,
        "warning": hist.warning(plan.item.delta),
    }
    if written is not None:
        body["written"] = written
        body["dry_run"] = dry_run
        if warnings:
            body["warnings"] = list(warnings)
    return body


# --------------------------------------------------------------------------- play statistics


@dataclass(frozen=True)
class PlayIndex:
    """The play-state ledgers of every device, as read, and the instant the reader trusts
    `last` times up to (`playstate.merge`'s horizon: later ones come from a wrong clock)."""

    ledgers: tuple[playstate.Ledger, ...] = ()
    horizon: int = 0

    @cached_property
    def keys(self) -> frozenset[playstate.Key]:
        return frozenset(k for ledger in self.ledgers for k in ledger.records)


def play_keys(system: str, game: catalog.Game) -> list[playstate.Key]:
    """The ledger keys (hub system, ROM path below it) a game's state may be under: its
    gamelist path, which is what a frontend records, then its primary path below the system's
    folder (they differ for a game with a shortcut, such as PS4)."""
    found: list[playstate.Key] = []
    base = f"roms/{system}/"
    primary = game.primary[len(base) :] if game.primary.startswith(base) else game.primary
    for path in (game.gamelist, primary):
        rel = artwork.relative(path)
        if rel is not None and (system, rel) not in found:
            found.append((system, rel))
    return found


def _key_of(index: PlayIndex, system: str, game: catalog.Game) -> playstate.Key | None:
    return next((k for k in play_keys(system, game) if k in index.keys), None)


def _seen_at(record: playstate.Record, index: PlayIndex) -> int | None:
    return record.last if record.last is not None and record.last <= index.horizon else None


def _totals(index: PlayIndex, key: playstate.Key) -> dict[str, Any]:
    """Plays, last played, favourite and playtime of a game across devices.

    Devices that share a gamelist report one counter, so a replica counts once (its highest
    value) and replicas add up: the rule of `playstate.merge`, which gives the plays, the last
    played time and the favourite. The same goes for the playtime."""
    merged = playstate.merge(index.ledgers, key, "", horizon=index.horizon)
    time: dict[str, int] = {}
    for ledger in index.ledgers:
        record = ledger.records.get(key)
        if record is not None:
            time[ledger.replica] = max(time.get(ledger.replica, 0), record.playtime)
    return {
        "plays": merged.total,
        "last": merged.last,
        "fav": merged.fav,
        "playtime": sum(time.values()),
    }


def game_totals(index: PlayIndex, system: str, game: catalog.Game) -> dict[str, Any] | None:
    """`{"plays", "last", "fav", "playtime"}`, or None when no device has a record of it."""
    key = _key_of(index, system, game)
    return None if key is None else _totals(index, key)


def game_play(index: PlayIndex, system: str, game: catalog.Game) -> dict[str, Any]:
    """The `play` object of a game: what each device says (`devices`, by device name) and the
    `totals`. A game nobody has played has no devices and zero totals."""
    key = _key_of(index, system, game)
    if key is None:
        return {
            "devices": {},
            "totals": {"plays": 0, "last": None, "fav": None, "playtime": 0},
        }
    devices = {
        ledger.device: {
            "replica": ledger.replica,
            "plays": record.plays,
            "last": _seen_at(record, index),
            "fav": record.fav,
            "playtime": record.playtime,
        }
        for ledger in sorted(index.ledgers, key=lambda ledger: ledger.device)
        if (record := ledger.records.get(key)) is not None
    }
    return {"devices": devices, "totals": _totals(index, key)}


def game_award(lib: Library, cfg: Config, system: str, game: catalog.Game) -> dict[str, str] | None:
    """The user's highest RetroAchievements award on a game, `{"kind", "date"}`, or None
    (no `[ra] user`, no downloaded progress, or no award): `progress.award` of the game page."""
    found = ra_data.ra_game(system, game.ra, lib.ra_lists)
    if found is None:
        return None
    return ra_data.award_of(lib.ra_cache, cfg.ra.user, found.id)


def attach_play(
    entries: Sequence[dict[str, Any]], lib: Library, index: PlayIndex
) -> list[dict[str, Any]]:
    """Adds `play` (the totals, or null) to the compact game entries of a list, by their `id`."""
    for entry in entries:
        found = lib.by_id.get(entry["id"])
        entry["play"] = None if found is None else game_totals(index, *found)
    return list(entries)


def parse_limit(raw: str | None) -> int:
    """The `limit` parameter of `recent`: a whole number from 1 to `RECENT_MAX`."""
    if raw is None:
        return RECENT_DEFAULT
    if not raw.isascii() or not raw.isdigit() or not 1 <= int(raw) <= RECENT_MAX:
        raise _bad(f"`limit` must be a whole number from 1 to {RECENT_MAX}")
    return int(raw)


def recent_response(
    lib: Library, index: PlayIndex, extras: Extras, limit: int = RECENT_DEFAULT
) -> dict[str, Any]:
    """The games played most recently on any device, newest first, with the device that played
    each and when. Records for a ROM the catalog does not have are left out."""
    found: list[tuple[int, str, str, catalog.Game, str, playstate.Key]] = []
    for system, games in lib.systems.items():
        for game in games.values():
            key = _key_of(index, system, game)
            if key is None:
                continue
            latest = [
                (seen, ledger.device)
                for ledger in index.ledgers
                if (record := ledger.records.get(key)) is not None
                and (seen := _seen_at(record, index)) is not None
            ]
            if latest:
                last, device = max(latest, key=lambda item: (item[0], item[1]))
                found.append((last, game.id, system, game, device, key))
    found.sort(key=lambda item: (-item[0], item[1]))
    out = []
    for last, _, system, game, device, key in found[:limit]:
        extra = extras(system)
        entry = game_entry(game, extra)
        totals = _totals(index, key)
        out.append(
            {
                "id": game.id,
                "system": system,
                "title": game.title,
                "name": (entry.fields.get("name") if entry else None) or game.title,
                "cover": "covers" in game_media(game, extra),
                "last": last,
                "device": device,
                "plays": totals["plays"],
                "playtime": totals["playtime"],
            }
        )
    return {"games": out}


# --------------------------------------------------------------------------- recommendations


def _epoch(date: str) -> int | None:
    """The UTC epoch of a RetroAchievements date (`2026-10-01` or `2026-10-01 12:30:00`), read
    to the day; None when it is empty or not a date."""
    try:
        return int(datetime.strptime(date.strip()[:10], "%Y-%m-%d").replace(tzinfo=UTC).timestamp())
    except ValueError:
        return None


def _availability(
    cfg: Config,
    views: Mapping[str, DeviceView],
    disks: Mapping[str, Mapping[str, Any]],
    system: str,
    game: catalog.Game,
) -> int:
    """`recommend.ON_CONSOLE` when some console has the game (or part of it), `FITS` when a
    console that takes the system and has a fresh disk report has room for it, else
    `ELSEWHERE`. A game the UI cannot switch (`unlistable`) never fits."""
    best = recommend.ELSEWHERE
    for device in library_devices(cfg):
        state = game_state(device, views[device.name], system, game)
        if state in ("on", "partial"):
            return recommend.ON_CONSOLE
        free = (disks.get(device.name) or {}).get("free")
        if (
            state == "off"
            and "unlistable" not in game.flags
            and isinstance(free, int)
            and 0 < game.size <= free
        ):
            best = recommend.FITS
    return best


def _ra_progress(
    lib: Library, cfg: Config, system: str, game: catalog.Game
) -> tuple[int, int, str, int | None]:
    """The user's achievements unlocked, the achievements available, the highest award and the
    date of the last unlock, from the cached progress. Zeros when RetroAchievements has no
    achievements for the game or the cache holds no progress of the configured user."""
    found = ra_data.ra_game(system, game.ra, lib.ra_lists)
    mine = lib.ra_cache.user
    if found is None or mine is None or not ra_data.same_user(mine.user, cfg.ra.user):
        return 0, 0, "", None
    line = mine.games.get(found.id)
    if line is None:
        return 0, found.achievements, "", None
    return (
        max(line.awarded, line.awarded_hardcore),
        found.achievements,
        line.award,
        _epoch(line.last),
    )


def candidates(
    lib: Library,
    cfg: Config,
    views: Mapping[str, DeviceView],
    index: PlayIndex,
    extras: Extras,
    disks: Mapping[str, Mapping[str, Any]],
) -> list[recommend.Candidate]:
    """Every game that has some activity or some metadata to compare, with what the shelves
    read: its play totals, its RA progress, its genre, developer and year, and its availability.
    A game with neither is left out (no shelf could use it)."""
    found: list[recommend.Candidate] = []
    for system, games in sorted(lib.systems.items()):
        extra = extras(system)
        for game in sorted(games.values(), key=lambda g: g.id):
            entry = game_entry(game, extra)
            fields = entry.fields if entry else {}
            genres = recommend.parse_genres(fields.get("genre"))
            developer = (fields.get("developer") or "").strip()
            totals = game_totals(index, system, game)
            unlocked, total, award, ra_last = _ra_progress(lib, cfg, system, game)
            if not (genres or developer or totals or unlocked):
                continue
            found.append(
                recommend.Candidate(
                    id=game.id,
                    system=system,
                    title=game.title,
                    key=game.key,
                    genres=genres,
                    developer=developer,
                    year=recommend.parse_year(fields.get("releasedate")),
                    plays=totals["plays"] if totals else 0,
                    playtime=totals["playtime"] if totals else 0,
                    last_played=totals["last"] if totals else None,
                    ra_unlocked=unlocked,
                    ra_total=total,
                    ra_last=ra_last,
                    ra_award=award,
                    tier=_availability(cfg, views, disks, system, game),
                )
            )
    return found


def _shelf_game(lib: Library, extras: Extras, c: recommend.Candidate) -> dict[str, Any]:
    system, game = lib.by_id[c.id]
    extra = extras(system)
    entry = game_entry(game, extra)
    return {
        "id": game.id,
        "system": system,
        "title": game.title,
        "name": (entry.fields.get("name") if entry else None) or game.title,
        "cover": "covers" in game_media(game, extra),
    }


def recommendations_response(
    lib: Library,
    cfg: Config,
    views: Mapping[str, DeviceView],
    index: PlayIndex,
    extras: Extras,
    disks: Mapping[str, Mapping[str, Any]],
    now: int,
) -> dict[str, Any]:
    """`GET /api/v1/recommendations`: the "Unfinished" shelf and the "Because you play X" shelf
    (see "Recommendations" in development/design/library-ui.md)."""
    picked = recommend.shelves(candidates(lib, cfg, views, index, extras, disks), now)
    unfinished = []
    for c in picked.unfinished:
        item = _shelf_game(lib, extras, c)
        item.update(
            {
                "last": recommend.activity(c, now),
                "plays": c.plays,
                "playtime": c.playtime,
                "ra": {"unlocked": c.ra_unlocked, "total": c.ra_total} if c.ra_total else None,
            }
        )
        unfinished.append(item)
    similar = None
    if picked.seed is not None and picked.similar:
        games = []
        for m in picked.similar:
            item = _shelf_game(lib, extras, m.game)
            item.update(
                {
                    "genres": list(m.genres),
                    "developer": m.game.developer if m.developer else None,
                    "year": m.year,
                }
            )
            games.append(item)
        similar = {"to": _shelf_game(lib, extras, picked.seed), "games": games}
    return {"unfinished": unfinished, "similar": similar}


# --------------------------------------------------------------------------- the unattended inbox

_RECORD_VERSION = 1
_COUNTS = ("placed", "already", "review", "held", "waiting")


def _strings(value: object) -> list[str]:
    return [x for x in value if isinstance(x, str)] if isinstance(value, list) else []


def _text(value: object) -> str | None:
    return value if isinstance(value, str) else None


def _whole(value: object) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else 0


def _parse_game(raw: object) -> dict[str, Any] | None:
    if not isinstance(raw, dict) or not isinstance(raw.get("entry"), str):
        return None
    return {
        "system": _text(raw.get("system")) or "",
        "entry": raw["entry"],
        "outcome": _text(raw.get("outcome")) or "",
        "path": _text(raw.get("path")),
        "whitelist": _strings(raw.get("whitelist")),
        "ra": _strings(raw.get("ra")),
        "notes": _strings(raw.get("notes")),
    }


def _parse_item(raw: object) -> dict[str, Any] | None:
    if not isinstance(raw, dict) or not isinstance(raw.get("item"), str):
        return None
    games = raw.get("games")
    return {
        "item": raw["item"],
        "outcome": _text(raw.get("outcome")) or "",
        "moved_to": _text(raw.get("moved_to")),
        "games": [g for g in map(_parse_game, games) if g] if isinstance(games, list) else [],
        "reasons": _strings(raw.get("reasons")),
        "notes": _strings(raw.get("notes")),
    }


def parse_record(text: str | None, expected_id: str) -> dict[str, Any] | None:
    """The run record in `text` (the file `<expected_id>.json`), cleaned to the shape the API
    serves, or None when there is none: no file, not JSON, not an object, of another version, or
    one whose `id` is not its file's. A damaged part (an item, a game, a count) is dropped or
    zeroed; the rest is kept."""
    if text is None:
        return None
    try:
        doc = json.loads(text)
    except (ValueError, RecursionError):
        return None
    if not isinstance(doc, dict) or doc.get("v") != _RECORD_VERSION or doc.get("id") != expected_id:
        return None
    items = doc.get("items")
    counts = doc.get("counts")
    scrape = doc.get("scrape")
    record: dict[str, Any] = {
        "v": _RECORD_VERSION,
        "id": expected_id,
        "mode": _text(doc.get("mode")) or "attended",
        "started": _text(doc.get("started")),
        "finished": _text(doc.get("finished")),
        "exit": doc["exit"]
        if isinstance(doc.get("exit"), int) and not isinstance(doc["exit"], bool)
        else None,
        "items": [i for i in map(_parse_item, items) if i] if isinstance(items, list) else [],
        "counts": {k: _whole(counts.get(k)) if isinstance(counts, dict) else 0 for k in _COUNTS},
        "whitelist": _strings(doc.get("whitelist")),
        "scrape": None,
        "purged": _strings(doc.get("purged")),
        "warnings": _strings(doc.get("warnings")),
    }
    if isinstance(scrape, dict) and isinstance(scrape.get("state"), str):
        record["scrape"] = {
            "state": scrape["state"],
            "systems": _strings(scrape.get("systems")),
            "message": _text(scrape.get("message")),
        }
    return record


def inbox_busy(lock_text: str | None, mtime: float | None) -> dict[str, str] | None:
    """`busy` of the inbox answer: null without a lock file (`mtime` None); else when the run
    began (the lock's own start, or the file's time when it cannot be read) and its mode
    (`unknown` when the lock says nothing)."""
    if mtime is None:
        return None
    info = inbox_lock.parse(lock_text) if lock_text is not None else None
    started = info.started if info is not None else mtime
    mode = (info.mode or inbox_lock.ATTENDED) if info is not None else "unknown"
    try:
        since = _iso(datetime.fromtimestamp(started, UTC))
    except (OverflowError, OSError, ValueError):
        return None
    return {"since": since, "mode": mode}


def _with_ids(record: Mapping[str, Any], lib: Library) -> dict[str, Any]:
    """The record with the catalog `id` of each game that has a path (null when the catalog
    has no game there yet)."""
    out = dict(record)
    out["items"] = [
        {
            **item,
            "games": [
                {
                    **game,
                    "id": lib.by_path.get(game["path"].removeprefix("library/"))
                    if game["path"]
                    else None,
                }
                for game in item["games"]
            ],
        }
        for item in record["items"]
    ]
    return out


def run_response(record: Mapping[str, Any], lib: Library) -> dict[str, Any]:
    """`GET /api/v1/inbox/runs/{id}`: a parsed record in full, each game with its catalog id."""
    return _with_ids(record, lib)


def _run_row(record: Mapping[str, Any]) -> dict[str, Any]:
    counts = record["counts"]
    return {
        "id": record["id"],
        "mode": record["mode"],
        "finished": record["finished"],
        "placed": counts["placed"],
        "already": counts["already"],
        "review": counts["review"],
        "held": counts["held"],
        "exit": record["exit"],
    }


def _stamp_of(when: datetime) -> str:
    return _iso(when.astimezone(UTC))


def _waiting_why(entry: Entry, state: State, busy: Mapping[str, str] | None) -> str:
    """`space` for an item that had been stable for the time the last check judged it and is
    still there with no run working (a ready item is processed unless there is no room);
    `settling` otherwise."""
    if busy is None and state.checked is not None:
        stable = (state.checked - entry.seen).total_seconds() >= STABLE_SECONDS
        return "space" if stable else "settling"
    return "settling"


def inbox_response(
    state: State,
    records: Sequence[Mapping[str, Any]],
    lib: Library,
    busy: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """`GET /api/v1/inbox`: the unattended runs' state and the newest records (parsed ones,
    newest first). A missing or damaged file is no state or no record, never an error."""
    held = sorted(
        (e.held.at, name, e.held.reason) for name, e in state.items.items() if e.held is not None
    )
    waiting = [
        {
            "item": name,
            "since": _stamp_of(e.first),
            "bytes": e.bytes,
            "why": _waiting_why(e, state, busy),
        }
        for name, e in sorted(state.items.items(), key=lambda kv: (kv[1].first, kv[0]))
        if e.held is None
    ]
    error = state.error
    return {
        "checked": None if state.checked is None else _stamp_of(state.checked),
        "busy": None if busy is None else dict(busy),
        "error": None
        if error is None
        else {
            "message": error.message,
            "since": _stamp_of(error.first),
            "count": error.count,
            "config": error.config,
        },
        "waiting": waiting,
        "held": [{"item": n, "since": _stamp_of(at), "reason": why} for at, n, why in held],
        "runs": [_run_row(r) for r in records[:INBOX_RUNS_SHOWN]],
        "last": run_response(records[0], lib) if records else None,
    }
