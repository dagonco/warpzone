"""The play queue as pure decisions: the file's schema (`parse`, `render`), the inferred states,
the per-console views, the release rule, `plan` for `POST /api/v1/queue` and the answers of both
routes (see development/design/play-queue.md).

Nothing here touches the disk, the network or a socket. `queue_store.py` reads and writes the
file; `serve.py` reads the catalog and the whitelists, hands them in as plain data (`Context`)
and writes the files `plan` decided on.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Collection, Mapping, Sequence
from dataclasses import asdict, dataclass, field, replace
from datetime import UTC, datetime
from typing import Any

from warpzone import catalog
from warpzone import library_api as api
from warpzone import whitelist as wl
from warpzone.config import Config, Device
from warpzone.library_api import ApiError

FORMAT = 1
OWNER = "default"  # the one owner while the UI has one token
FILE_LIMIT = 16 * 1024 * 1024  # a file over this is damaged
SIZE_LIMIT = 4 * 1024 * 1024  # a write over this, and larger than the file it replaces, is refused
MAX_ACTIVE = 200  # per owner
MAX_OPS = 50
MAX_CLAIMS = 8  # consoles per entry
HISTORY_DEFAULT = 50
HISTORY_MAX = 10000
CANDIDATES = 10
PLAYING_WINDOW = 14 * 24 * 3600  # seconds a game counts as being played after its last play
ACTIVE = ("queued", "playing")
ENDED = ("finished", "dropped")
STATES = (*ACTIVE, *ENDED)
AWARDS = ("beaten-softcore", "beaten-hardcore", "completed", "mastered")
TIME_FORMAT = "%Y-%m-%dT%H:%M:%SZ"
_TIME = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z")
_OWNER = re.compile(r"[a-z0-9_-]{1,32}")
_DEVICE = re.compile(r"[a-z0-9][a-z0-9_-]*")
_GAME_ID = re.compile(r"[0-9a-f]{16}")
_COLLECTION_ID = re.compile(r"[0-9a-f]{8}")


# --------------------------------------------------------------------------- times


def fmt_time(when: datetime) -> str:
    """The file's form of a time: UTC, whole seconds, `Z`."""
    return when.astimezone(UTC).strftime(TIME_FORMAT)


def parse_time(text: str) -> datetime | None:
    """A time of the file's form; None for anything else."""
    if not _TIME.fullmatch(text):
        return None
    try:
        return datetime.strptime(text, TIME_FORMAT).replace(tzinfo=UTC)
    except ValueError:
        return None


def award_time(date: str) -> datetime | None:
    """The UTC instant of a RetroAchievements award date (`2026-10-01`, `2026-10-01 12:30:00`
    or ISO with an offset), to the second; None when it is not a date."""
    try:
        when = datetime.fromisoformat(date.strip())
    except ValueError:
        return None
    return when.replace(tzinfo=when.tzinfo or UTC, microsecond=0).astimezone(UTC)


# --------------------------------------------------------------------------- the model


@dataclass(frozen=True)
class GameRef:
    """A game by catalog id, with the snapshot taken when it was written."""

    id: str
    system: str
    primary: str
    title: str
    key: str


@dataclass(frozen=True)
class Claim:
    """What the queue did on one console: `before` is the game's state there when it was queued,
    `end` is None while open, then `released` or `kept`."""

    before: str
    since: str
    end: str | None = None
    until: str | None = None


@dataclass(frozen=True)
class Entry:
    owner: str
    game: GameRef
    state: str
    added: str
    changed: str
    ended: str | None
    ra_dismissed: str | None
    devices: Mapping[str, Claim]


@dataclass(frozen=True)
class Group:
    """A collection: a manual list of games with no effect on any whitelist."""

    owner: str
    title: str
    created: str
    games: tuple[GameRef, ...]


@dataclass(frozen=True)
class Queue:
    updated: str = ""
    entries: tuple[Entry, ...] = ()
    collections: Mapping[str, Group] = field(default_factory=dict)


def holds(claim: Claim) -> bool:
    """The queue wants the game on there and it is the queue's to turn off."""
    return claim.before == "off" and claim.end is None


def ref_of(system: str, game: catalog.Game) -> GameRef:
    return GameRef(game.id, system, game.primary, game.title, game.key)


# --------------------------------------------------------------------------- the file


@dataclass(frozen=True)
class Reading:
    """A queue file as read: `state` is `ok`, `absent`, `damaged` or `newer`. A damaged or newer
    file has an empty `queue` and a `message` that says why."""

    state: str
    queue: Queue
    message: str = ""


class _Bad(Exception):
    pass


def _obj(value: Any, keys: Collection[str], where: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise _Bad(f"{where} must be an object")
    for key in value:
        if key not in keys:
            raise _Bad(f"{where}: unknown key {key!r}")
    for key in keys:
        if key not in value:
            raise _Bad(f"{where}: missing key {key!r}")
    return value


def _str(value: Any, where: str, pattern: re.Pattern[str] | None = None) -> str:
    if not isinstance(value, str) or (pattern is not None and not pattern.fullmatch(value)):
        raise _Bad(f"{where} is not valid")
    return value


def _time(value: Any, where: str, *, null: bool = False) -> str | None:
    if value is None and null:
        return None
    if not isinstance(value, str) or parse_time(value) is None:
        raise _Bad(f"{where} must be a UTC time like 2026-11-10T20:00:00Z")
    return value


def _choice(value: Any, allowed: Collection[str], where: str) -> str:
    if not isinstance(value, str) or value not in allowed:
        raise _Bad(f"{where} must be one of {', '.join(allowed)}")
    return value


def _ref(value: Any, where: str) -> GameRef:
    found = _obj(value, ("id", "system", "primary", "title", "key"), where)
    return GameRef(
        _str(found["id"], f"{where}.id", _GAME_ID),
        _str(found["system"], f"{where}.system"),
        _str(found["primary"], f"{where}.primary"),
        _str(found["title"], f"{where}.title"),
        _str(found["key"], f"{where}.key"),
    )


def _claim(value: Any, where: str) -> Claim:
    found = _obj(value, ("before", "since", "end", "until"), where)
    end = found["end"]
    if end is not None:
        end = _choice(end, ("released", "kept"), f"{where}.end")
    until = _time(found["until"], f"{where}.until", null=True)
    if (end is None) != (until is None):
        raise _Bad(f"{where}: `end` and `until` go together")
    return Claim(
        _choice(found["before"], ("off", "on", "partial"), f"{where}.before"),
        str(_time(found["since"], f"{where}.since")),
        end,
        until,
    )


def _entry(value: Any, where: str) -> Entry:
    found = _obj(
        value,
        ("owner", "game", "state", "added", "changed", "ended", "ra_dismissed", "devices"),
        where,
    )
    state = _choice(found["state"], STATES, f"{where}.state")
    ended = _time(found["ended"], f"{where}.ended", null=True)
    if (ended is None) != (state in ACTIVE):
        raise _Bad(f"{where}: `ended` is set exactly when the state is finished or dropped")
    raw = found["devices"]
    if not isinstance(raw, dict):
        raise _Bad(f"{where}.devices must be an object")
    claims = {
        _str(name, f"{where}.devices name", _DEVICE): _claim(claim, f"{where}.devices.{name}")
        for name, claim in raw.items()
    }
    return Entry(
        _str(found["owner"], f"{where}.owner", _OWNER),
        _ref(found["game"], f"{where}.game"),
        state,
        str(_time(found["added"], f"{where}.added")),
        str(_time(found["changed"], f"{where}.changed")),
        ended,
        _time(found["ra_dismissed"], f"{where}.ra_dismissed", null=True),
        dict(sorted(claims.items())),
    )


def _group(value: Any, where: str) -> Group:
    found = _obj(value, ("owner", "title", "created", "games"), where)
    if not isinstance(found["games"], list):
        raise _Bad(f"{where}.games must be a list")
    games = tuple(_ref(g, f"{where}.games[{n}]") for n, g in enumerate(found["games"]))
    if len({g.id for g in games}) != len(games):
        raise _Bad(f"{where}.games repeats a game")
    return Group(
        _str(found["owner"], f"{where}.owner", _OWNER),
        _str(found["title"], f"{where}.title"),
        str(_time(found["created"], f"{where}.created")),
        games,
    )


def _queue(doc: Any) -> Queue:
    found = _obj(doc, ("format", "updated", "queue", "collections"), "the file")
    if found["format"] != FORMAT or isinstance(found["format"], bool):
        raise _Bad("format is not 1")
    if not isinstance(found["queue"], list) or not isinstance(found["collections"], dict):
        raise _Bad("`queue` must be a list and `collections` an object")
    entries = tuple(_entry(e, f"queue[{n}]") for n, e in enumerate(found["queue"]))
    if len({(e.owner, e.game.id) for e in entries}) != len(entries):
        raise _Bad("queue: two entries have the same owner and game")
    sets = {
        _str(cid, "a collection id", _COLLECTION_ID): _group(c, f"collections.{cid}")
        for cid, c in sorted(found["collections"].items())
    }
    return Queue(str(_time(found["updated"], "updated")), entries, sets)


def parse(data: bytes | None) -> Reading:
    """The queue a file's bytes hold (None: no file). Anything that does not match the schema
    exactly is `damaged`; a format above 1 is `newer` and not read."""
    if data is None:
        return Reading("absent", Queue())
    if len(data) > FILE_LIMIT:
        return Reading("damaged", Queue(), "the file is over 16 MiB")
    try:
        doc = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, ValueError, RecursionError):
        return Reading("damaged", Queue(), "the file is not UTF-8 JSON")
    version = doc.get("format") if isinstance(doc, dict) else None
    if isinstance(version, int) and not isinstance(version, bool) and version > FORMAT:
        return Reading("newer", Queue(), f"the file is format {version}; this warpzone reads 1")
    try:
        return Reading("ok", _queue(doc))
    except _Bad as exc:
        return Reading("damaged", Queue(), f"the file does not match the schema: {exc}")


def render(queue: Queue) -> bytes:
    """The deterministic encoding: sorted keys, compact, a final newline. The order of `queue`
    is kept."""
    doc = {
        "format": FORMAT,
        "updated": queue.updated,
        "queue": [asdict(e) for e in queue.entries],
        "collections": {cid: asdict(c) for cid, c in queue.collections.items()},
    }
    text = json.dumps(doc, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return (text + "\n").encode("utf-8")


def etag_of(data: bytes | None) -> str:
    """The queue file's etag: `sha256:<hex>` of its bytes, `absent` for no file."""
    return "absent" if data is None else api.api_etag(hashlib.sha256(data).hexdigest())


def size_check(new: int, current: int) -> bool:
    """False when a write of `new` bytes over a file of `current` bytes is refused: over the
    limit and larger than the file it replaces (a write that makes room is always accepted)."""
    return new <= SIZE_LIMIT or new <= current


def mine(queue: Queue) -> list[Entry]:
    """The entries of the one owner, in queue order."""
    return [e for e in queue.entries if e.owner == OWNER]


# --------------------------------------------------------------------------- resolving


@dataclass(frozen=True)
class Resolution:
    found: str  # present, moved or missing
    candidates: tuple[tuple[str, catalog.Game], ...] = ()


def resolve(ref: GameRef, lib: api.Library, taken: Collection[str] = ()) -> Resolution:
    """Find a game reference in the catalog. `moved`: the id is gone but games with the same
    title key exist (those of the same system first, at most 10, games in `taken` left out)."""
    if ref.id in lib.by_id:
        return Resolution("present")
    same = [(s, g) for s, g in lib.by_key.get(ref.key, ()) if g.id not in taken]
    same.sort(key=lambda sg: (sg[0] != ref.system, sg[0], sg[1].title.casefold(), sg[1].id))
    if not same:
        return Resolution("missing")
    return Resolution("moved", tuple(same[:CANDIDATES]))


# --------------------------------------------------------------------------- inferred states


def shown_state(entry: Entry, play: Mapping[str, Any] | None, now: int) -> tuple[str, bool]:
    """`(state shown, inferred)`. A `queued` entry is shown as `playing` when its game was
    played (`play["last"]`, the totals' last time) at or after `added` and at most 14 days
    before `now`; a stored `playing` or an ended state is shown as stored."""
    if entry.state != "queued":
        return entry.state, False
    last = play.get("last") if play else None
    added = parse_time(entry.added)
    if (
        isinstance(last, int)
        and added is not None
        and last >= added.timestamp()
        and now - last <= PLAYING_WINDOW
    ):
        return "playing", True
    return "queued", False


def suggestion(entry: Entry, award: Mapping[str, str] | None) -> dict[str, str] | None:
    """`{"state": "finished", "award", "date"}` for an active entry whose game has a
    RetroAchievements award dated at or after `added` and later than `ra_dismissed`."""
    if entry.state not in ACTIVE or not award or award.get("kind") not in AWARDS:
        return None
    date = award.get("date", "")
    when, added = award_time(date), parse_time(entry.added)
    if when is None or added is None or when < added:
        return None
    dismissed = parse_time(entry.ra_dismissed) if entry.ra_dismissed else None
    if dismissed is not None and when <= dismissed:
        return None
    return {"state": "finished", "award": str(award["kind"]), "date": date}


def device_view(claim: Claim, game: str, found: str, device_known: bool) -> str:
    """What a claim looks like against the game's state on its console (`game`: `on`, `off`,
    `partial`, `unsupported` or `unknown`): `on`, `not-on`, `kept`, `released`, `not-released`
    or `unknown`."""
    if not device_known or found != "present" or game in ("unknown", "unsupported"):
        return "unknown"
    if holds(claim):
        return "on" if game == "on" else "not-on"
    if claim.end == "released" and claim.before == "off":
        return "released" if game == "off" else "not-released"
    return "kept"


def release_change(model: wl.Model, parts: Sequence[str]) -> wl.Model | None:
    """The model with the game's includes removed and nothing else (never an exclusion line),
    or None when a part stays on anyway (the whole system or a parent folder is on)."""
    includes = frozenset(i for i in model.includes if not any(wl.covers(p, i) for p in parts))
    new = wl.normalize(wl.Model(model.systems, includes, model.excludes))
    return None if any(wl.decide(new, p) for p in parts) else new


# --------------------------------------------------------------------------- the request


@dataclass(frozen=True)
class Op:
    op: str
    game: str
    devices: tuple[str, ...] = ()  # `add`: the consoles to queue for
    add: tuple[str, ...] = ()  # `devices`: consoles to add
    release: tuple[str, ...] = ()
    keep: tuple[str, ...] = ()
    position: int | None = None
    state: str | None = None
    device: str | None = None  # `sync`
    to: str | None = None  # `relink`


@dataclass(frozen=True)
class Request:
    ops: tuple[Op, ...]
    expect_queue: str | None
    expect: Mapping[str, str]
    dry_run: bool


# op -> (allowed fields, required fields)
_FIELDS: dict[str, tuple[set[str], set[str]]] = {
    "add": ({"game", "devices", "position"}, {"game", "devices"}),
    "devices": ({"game", "add", "release", "keep"}, {"game"}),
    "move": ({"game", "position"}, {"game", "position"}),
    "state": ({"game", "state", "release", "keep"}, {"game", "state"}),
    "remove": ({"game", "release", "keep"}, {"game"}),
    "sync": ({"game", "device"}, {"game", "device"}),
    "relink": ({"game", "to"}, {"game", "to"}),
    "dismiss": ({"game"}, {"game"}),
}


def _bad(message: str) -> ApiError:
    return ApiError("bad-request", message)


def _names(value: Any, where: str) -> tuple[str, ...]:
    if not isinstance(value, list) or not all(isinstance(v, str) and v for v in value):
        raise _bad(f"{where} must be a list of device names")
    if len(set(value)) != len(value) or len(value) > MAX_CLAIMS:
        raise _bad(f"{where} must hold at most {MAX_CLAIMS} different names")
    return tuple(value)


def _op(raw: Any, where: str) -> Op:
    if not isinstance(raw, dict) or raw.get("op") not in _FIELDS:
        raise _bad(f"{where}.op must be one of {', '.join(_FIELDS)}")
    allowed, required = _FIELDS[raw["op"]]
    for key in raw:
        if key != "op" and key not in allowed:
            raise _bad(f"{where}: unknown key {key!r} for {raw['op']}")
    for key in sorted(required - set(raw)):
        raise _bad(f"{where}: `{key}` is missing")
    fields: dict[str, Any] = {"op": raw["op"]}
    for key, value in raw.items():
        if key == "op":
            continue
        if key in ("game", "to", "device"):
            if not isinstance(value, str) or not value:
                raise _bad(f"{where}.{key} must be a non-empty string")
        elif key == "state":
            if value not in STATES:
                raise _bad(f"{where}.state must be one of {', '.join(STATES)}")
        elif key == "position":
            if value is not None and (
                not isinstance(value, int) or isinstance(value, bool) or value < 0
            ):
                raise _bad(f"{where}.position must be a whole number or null")
            if value is None and raw["op"] == "move":
                raise _bad(f"{where}.position must be a whole number")
        else:
            value = _names(value, f"{where}.{key}")
        fields[key] = value
    return Op(**fields)


def parse_request(body: object) -> Request:
    """The shape of a `POST /api/v1/queue` body and its limits; `bad-request` otherwise."""
    if not isinstance(body, dict):
        raise _bad("the body must be a JSON object")
    unknown = sorted(set(body) - {"ops", "expect_queue", "expect", "dry_run"})
    if unknown:
        raise _bad(f"unknown key {unknown[0]!r}")
    ops = body.get("ops")
    if not isinstance(ops, list) or not ops:
        raise _bad("`ops` must be a non-empty list")
    if len(ops) > MAX_OPS:
        raise _bad(f"`ops` holds more than {MAX_OPS} operations")
    expect_queue = body.get("expect_queue")
    if expect_queue is not None and not isinstance(expect_queue, str):
        raise _bad("`expect_queue` must be the queue's etag")
    expect = body.get("expect", {})
    if not isinstance(expect, dict) or not all(
        isinstance(k, str) and isinstance(v, str) for k, v in expect.items()
    ):
        raise _bad("`expect` must map device names to etags")
    dry_run = body.get("dry_run", False)
    if not isinstance(dry_run, bool):
        raise _bad("`dry_run` must be true or false")
    parsed = tuple(_op(op, f"ops[{n}]") for n, op in enumerate(ops, 1))
    return Request(parsed, expect_queue, dict(expect), dry_run)


def parse_history(raw: str | None) -> int:
    """The `history` parameter of `GET /api/v1/queue`: a whole number from 0 to 10000."""
    if raw is None:
        return HISTORY_DEFAULT
    if not raw.isascii() or not raw.isdigit() or int(raw) > HISTORY_MAX:
        raise _bad(f"`history` must be a whole number from 0 to {HISTORY_MAX}")
    return int(raw)


# --------------------------------------------------------------------------- the plan


@dataclass(frozen=True)
class Context:
    """What the answers read beside the queue: the catalog, the config, the whitelists as read,
    the play totals and the gamelists."""

    cfg: Config
    lib: api.Library
    views: Mapping[str, api.DeviceView]
    index: api.PlayIndex
    extras: api.Extras


@dataclass(frozen=True)
class QueuePlan:
    """The outcome of a request. `first` is the queue file to write before the whitelists (None:
    the queue does not change), `second` the one to write after them (a `remove` that ended
    claims). `queue` is the queue after everything and `views` the whitelists after it; `reads`
    is the etag of every whitelist the plan looked at, written or not, for the re-check."""

    dry_run: bool
    queue: Queue
    first: bytes | None
    second: bytes | None
    devices: tuple[api.DevicePlan, ...]
    skipped: tuple[dict[str, Any], ...]
    views: Mapping[str, api.DeviceView]
    reads: Mapping[str, str]  # every whitelist the plan read, with the etag it read

    @property
    def final(self) -> bytes | None:
        """The bytes the queue file ends with, None when it is not written."""
        return self.second if self.second is not None else self.first


def _end(claim: Claim, how: str, now: str) -> Claim:
    return replace(claim, end=how, until=now)


class _Work:
    """The request being applied, operation by operation, to one model of the queue and one
    model per touched whitelist."""

    def __init__(self, queue: Queue, ctx: Context, now: datetime) -> None:
        self.ctx = ctx
        self.lib = ctx.lib
        self.devices = {d.name: d for d in api.library_devices(ctx.cfg)}
        self.entries = list(queue.entries)
        self.now = fmt_time(now)
        self.models: dict[str, wl.Model] = {}
        self.read: set[str] = set()  # the whitelists looked at, written or not
        self.doomed: set[str] = set()  # entries a second write deletes
        self.skipped: list[dict[str, Any]] = []

    # ------------------------------------------------------------- helpers

    def peek(self, name: str) -> wl.Model | None:
        """A console's model as it stands, without making it a console the request writes."""
        self.read.add(name)
        return self.models.get(name) or self.ctx.views[name].model

    def pending(self, game: str, name: str, claim: Claim) -> bool:
        """A claim ended `released` whose game is still on: the whitelist write after the queue
        write failed, so the queue still owes the console a turn-off."""
        found = self.lib.by_id.get(game)
        if claim.end != "released" or claim.before != "off" or found is None:
            return False
        model = self.peek(name) if name in self.devices else None
        return model is not None and wl.game_state(model, [p.path for p in found[1].parts]) != "off"

    def owed(self, game: str, name: str, claim: Claim) -> bool:
        """The queue holds the game there, or a failed release left it pending."""
        return holds(claim) or self.pending(game, name, claim)

    def model(self, name: str) -> wl.Model:
        self.read.add(name)
        if name not in self.models:
            model = self.ctx.views[name].model
            assert model is not None  # a managed or missing whitelist always has a model
            self.models[name] = model
        return self.models[name]

    def find(self, game: str) -> int | None:
        return next(
            (i for i, e in enumerate(self.entries) if e.owner == OWNER and e.game.id == game), None
        )

    def index(self, game: str) -> int:
        found = self.find(game)
        if found is None:
            raise ApiError("not-found", f"no queue entry for game {game!r}", game=game)
        if game in self.doomed:
            raise _bad(f"{game!r} was removed earlier in this request")
        return found

    def active(self) -> list[int]:
        return [i for i, e in enumerate(self.entries) if e.owner == OWNER and e.state in ACTIVE]

    def taken(self) -> set[str]:
        return {e.game.id for e in self.entries if e.owner == OWNER}

    def insert(self, entry: Entry, position: int | None) -> None:
        active = self.active()
        if position is not None and position < len(active):
            at = active[position]
        else:
            at = active[-1] + 1 if active else len(self.entries)
        self.entries.insert(at, entry)

    def check_room(self, position: int | None = None, extra: int = 0) -> None:
        active = len(self.active())
        if active >= MAX_ACTIVE:
            raise ApiError("queue-full", f"the queue already has {MAX_ACTIVE} active entries")
        if position is not None and position > active - extra:
            raise _bad(f"`position` must be from 0 to {active - extra}")

    def skip(self, name: str, ref: GameRef, reason: str) -> None:
        self.skipped.append(
            {"device": name, "system": ref.system, "game": ref.id, "reason": reason}
        )

    def turn_on(
        self, name: str, system: str, game: catalog.Game, prior: Claim | None = None
    ) -> str:
        """Turn a game on for a console; the game's state there before. A game still on after a
        release that did not reach the whitelist (`prior`) was the queue's: it stays `off`."""
        device = self.devices[name]
        if system in device.exclude_systems:
            raise ApiError(
                "unsupported", f"{name} does not run {system!r}", device=name, game=game.id
            )
        model = self.model(name)
        before = wl.game_state(model, [p.path for p in game.parts])
        if before != "on":
            item = api.ChangeItem(name, system, game.id, True)
            self.models[name] = api.apply_item(device, model, item, self.lib)
        if (
            before == "on"
            and prior is not None
            and prior.before == "off"
            and prior.end == "released"
        ):
            return "off"
        return before

    def release(self, entry: Entry, name: str, claim: Claim) -> Claim:
        """End a held claim: the game goes off, unless another claim has the console or a part
        stays on; then the claim ends `kept` and the console is reported in `skipped`."""
        ref = entry.game
        found = self.lib.by_id.get(ref.id)
        if found is None:
            raise ApiError(
                "not-held",
                f"{ref.title!r} is no longer in the library; its consoles can only be kept",
                device=name,
                game=ref.id,
            )
        system, game = found
        shared = any(
            (e.owner, e.game.id) != (entry.owner, ref.id)
            and e.game.id == ref.id
            and name in e.devices
            and e.devices[name].end is None
            for e in self.entries
        )
        if shared:
            self.skip(name, ref, "claimed")
            return _end(claim, "kept", self.now)
        model = self.model(name)
        if release_change(model, [p.path for p in game.parts]) is None:
            self.skip(name, ref, "covered")
            return _end(claim, "kept", self.now)
        item = api.ChangeItem(name, system, ref.id, False)
        self.models[name] = api.apply_item(self.devices[name], model, item, self.lib)
        return _end(claim, "released", self.now)

    def end_claims(
        self, i: int, release: Sequence[str], keep: Sequence[str], *, complete: bool
    ) -> list[str]:
        """End claims of entry `i`: the `release` ones turn the game off, the `keep` ones leave
        it. With `complete`, every console the queue owes (held, or pending after a failed
        release) must be named and the open claims that hold nothing end `kept`. Returns the
        consoles released."""
        game = self.entries[i].game.id
        entry = self.entries[i]
        if set(release) & set(keep):
            raise _bad("a console is in both `release` and `keep`")
        claims = dict(entry.devices)
        if complete:
            left = [
                n for n, c in claims.items() if n not in (*release, *keep) and self.owed(game, n, c)
            ]
            if left:
                raise _bad(f"name every console the queue holds in `release` or `keep`: {left[0]}")
        for name in release:
            if name not in claims or not self.owed(game, name, claims[name]):
                raise ApiError(
                    "not-held",
                    f"the queue does not hold {entry.game.title!r} on {name}",
                    device=name,
                    game=entry.game.id,
                )
        for name in keep:
            if name not in claims or not (
                claims[name].end is None or self.pending(game, name, claims[name])
            ):
                raise ApiError(
                    "not-held",
                    f"{name} has no open claim on {entry.game.title!r}",
                    device=name,
                    game=entry.game.id,
                )
        released = []
        for name in release:
            claims[name] = self.release(entry, name, claims[name])
            if claims[name].end == "released":
                released.append(name)
        for name in keep:
            claims[name] = _end(claims[name], "kept", self.now)
        if complete:
            claims = {
                n: _end(c, "kept", self.now) if c.end is None else c for n, c in claims.items()
            }
        self.entries[i] = replace(entry, devices=dict(sorted(claims.items())))
        return released

    # ------------------------------------------------------------- the operations

    def do(self, op: Op) -> None:
        getattr(self, f"op_{op.op}")(op)

    def op_add(self, op: Op) -> None:
        system, game = self.lib.by_id[op.game]
        i = self.find(op.game)
        if op.game in self.doomed:
            raise _bad(f"{op.game!r} was removed earlier in this request")
        if i is not None:
            if self.entries[i].state in ACTIVE:
                raise ApiError("already-queued", f"{game.title!r} is already queued", game=op.game)
            old = self.entries.pop(i).devices
        else:
            old = {}
        self.check_room(op.position)
        # The old claims are all ended; one whose release never reached the whitelist is still
        # owed and stays on the entry.
        claims = {n: c for n, c in old.items() if self.pending(op.game, n, c)}
        for name in op.devices:
            claims[name] = Claim(self.turn_on(name, system, game, old.get(name)), self.now)
        entry = Entry(
            OWNER,
            ref_of(system, game),
            "queued",
            self.now,
            self.now,
            None,
            None,
            dict(sorted(claims.items())),
        )
        self.insert(entry, op.position)

    def op_devices(self, op: Op) -> None:
        i = self.index(op.game)
        entry = self.entries[i]
        if entry.state not in ACTIVE:
            raise _bad(f"{entry.game.title!r} is not in the queue; queue it again first")
        if set(op.add) & {*op.release, *op.keep}:
            raise _bad("a console is in both `add` and `release` or `keep`")
        self.end_claims(i, op.release, op.keep, complete=False)
        entry = self.entries[i]
        claims = dict(entry.devices)
        for name in op.add:
            if name in claims and claims[name].end is None:
                raise _bad(f"{name} already has an open claim on {entry.game.title!r}")
        if op.add:
            found = self.lib.by_id.get(op.game)
            if found is None:
                raise _bad(f"{entry.game.title!r} is no longer in the library")
            for name in op.add:
                claims[name] = Claim(self.turn_on(name, *found, claims.get(name)), self.now)
        if len(claims) > MAX_CLAIMS:
            raise _bad(f"an entry has at most {MAX_CLAIMS} consoles")
        self.entries[i] = replace(entry, devices=dict(sorted(claims.items())))

    def op_move(self, op: Op) -> None:
        i = self.index(op.game)
        if self.entries[i].state not in ACTIVE:
            raise _bad("only an active entry can be moved")
        entry = self.entries.pop(i)
        active = len(self.active())
        if op.position is None or op.position > active:
            raise _bad(f"`position` must be from 0 to {active}")
        self.insert(entry, op.position)

    def op_state(self, op: Op) -> None:
        i = self.index(op.game)
        entry = self.entries[i]
        target = op.state
        assert target is not None
        if target in ENDED and entry.state in ACTIVE:
            self.end_claims(i, op.release, op.keep, complete=True)
            self.entries[i] = replace(
                self.entries[i], state=target, changed=self.now, ended=self.now
            )
            return
        if op.release or op.keep:
            raise _bad("`release` and `keep` apply only to finishing or dropping an active entry")
        if target in ACTIVE and entry.state in ENDED:
            self.check_room()
            self.entries[i] = replace(entry, state=target, changed=self.now, ended=None)
        elif entry.state != target:
            self.entries[i] = replace(entry, state=target, changed=self.now)

    def op_remove(self, op: Op) -> None:
        i = self.index(op.game)
        released = self.end_claims(i, op.release, op.keep, complete=True)
        if released:
            self.doomed.add(op.game)
        else:
            del self.entries[i]

    def op_sync(self, op: Op) -> None:
        i = self.index(op.game)
        entry = self.entries[i]
        name = op.device
        assert name is not None
        claim = entry.devices.get(name)
        found = self.lib.by_id.get(op.game)
        view = "unknown"
        if claim is None:
            raise ApiError(
                "not-held",
                f"{name} has no claim on {entry.game.title!r}",
                device=name,
                game=op.game,
            )
        if found is not None:
            state = wl.game_state(self.model(name), [p.path for p in found[1].parts])
            view = device_view(claim, state, "present", True)
        if view not in ("not-on", "not-released"):
            raise ApiError(
                "in-sync",
                f"{name} needs no repair for {entry.game.title!r}",
                device=name,
                game=op.game,
            )
        assert found is not None
        if view == "not-on":
            self.turn_on(name, *found)
            return
        claims = dict(entry.devices)
        claims[name] = self.release(entry, name, claim)
        self.entries[i] = replace(entry, devices=claims)

    def op_relink(self, op: Op) -> None:
        i = self.index(op.game)
        entry = self.entries[i]
        if resolve(entry.game, self.lib).found == "present":
            raise _bad(f"{entry.game.title!r} is still in the library")
        assert op.to is not None
        found = self.lib.by_id.get(op.to)
        if found is None:
            raise ApiError("not-found", f"no game {op.to!r}", game=op.to)
        if self.find(op.to) is not None:
            raise ApiError("already-queued", f"{found[1].title!r} already has an entry", game=op.to)
        claims = {
            n: _end(c, "kept", self.now) if c.end is None else c for n, c in entry.devices.items()
        }
        self.entries[i] = replace(entry, game=ref_of(*found), devices=claims)

    def op_dismiss(self, op: Op) -> None:
        i = self.index(op.game)
        entry = self.entries[i]
        found = self.lib.by_id.get(op.game)
        award = api.game_award(self.lib, self.ctx.cfg, *found) if found else None
        shown = suggestion(entry, award)
        when = award_time(shown["date"]) if shown else None
        if when is None:
            raise _bad(f"{entry.game.title!r} has no suggestion to dismiss")
        self.entries[i] = replace(entry, ra_dismissed=fmt_time(when))


def _touched(request: Request, devices: Mapping[str, Device], ctx: Context) -> list[str]:
    """The consoles an operation reads or writes (a `keep` only ends a claim), after checking
    that each is known, that each `add` names a catalog game and that each whitelist is
    managed."""
    touched: dict[str, None] = {}
    for op in request.ops:
        if op.op == "add" and op.game not in ctx.lib.by_id:
            raise ApiError("not-found", f"no game {op.game!r}", game=op.game)
        for name in (*op.devices, *op.add, *op.release, *([op.device] if op.device else [])):
            if name not in devices:
                raise ApiError("not-found", f"no device {name!r}", device=name)
            touched[name] = None
    for name in touched:
        if ctx.views[name].state == api.NEEDS_MIGRATION:
            raise ApiError(
                "not-managed",
                f"{name}: the whitelist is not in the UI's format "
                "(run `warpzone whitelist migrate`)",
                device=name,
            )
    return list(touched)


def plan(
    body: object, reading: Reading, etag: str, size: int, ctx: Context, now: datetime
) -> QueuePlan:
    """Check a queue request and compute the files it writes.

    `etag` and `size` are the queue file's now. Raises the first `ApiError`: the shape of the
    body and its limits; the queue is not damaged or newer; every game and device is known and
    each whitelist managed; an `expect_queue` and an `expect` for every touched console that
    equal the current etags; then the operations in order (each game, position and release
    rule); the size limit last, since it needs the rendered file. Nothing is written here."""
    request = parse_request(body)
    if reading.state in ("damaged", "newer"):
        raise ApiError("queue-damaged", f"queue.json: {reading.message}", queue=True)
    devices = {d.name: d for d in api.library_devices(ctx.cfg)}
    touched = _touched(request, devices, ctx)
    if request.expect_queue is None:
        raise ApiError("expect-missing", "no `expect_queue`", queue=True)
    for name in touched:
        if name not in request.expect:
            raise ApiError("expect-missing", f"no `expect` for {name!r}", device=name)
    if api.canonical_etag(request.expect_queue) != etag:
        raise ApiError("stale", "the queue changed since it was read", queue=True)
    for name in touched:
        if api.canonical_etag(request.expect[name]) != ctx.views[name].etag:
            raise ApiError("stale", f"{name}: the whitelist changed since it was read", device=name)

    work = _Work(reading.queue, ctx, now)
    for op in request.ops:
        work.do(op)

    entries = tuple(work.entries)
    queue = replace(reading.queue, updated=fmt_time(now), entries=entries)
    first: bytes | None = None
    second: bytes | None = None
    final = queue
    if entries != reading.queue.entries or work.doomed:
        first = render(queue)
        if not size_check(len(first), size):
            raise ApiError(
                "queue-too-large",
                f"the queue file would be {len(first)} bytes, over the {SIZE_LIMIT} limit "
                f"and larger than its {size}; remove ended entries or collection games to "
                "make room",
                queue=True,
            )
        if work.doomed:
            final = replace(
                queue,
                entries=tuple(
                    e for e in entries if not (e.owner == OWNER and e.game.id in work.doomed)
                ),
            )
            second = render(final)
    plans = tuple(
        api.finish_device(devices[n], ctx.views[n], model, ctx.lib)
        for n, model in work.models.items()
    )
    after = dict(ctx.views)
    for dp in plans:
        if dp.changed:
            after[dp.device] = api.DeviceView(
                api.MANAGED, dp.new_etag, work.models[dp.device], dp.new_text
            )
    reads = {n: ctx.views[n].etag for n in sorted(work.read)}
    return QueuePlan(
        request.dry_run, final, first, second, plans, tuple(work.skipped), after, reads
    )


# --------------------------------------------------------------------------- the answers


def _card(ctx: Context, system: str, game: catalog.Game) -> dict[str, Any]:
    extra = ctx.extras(system)
    meta = api.game_entry(game, extra)
    return {
        "name": (meta.fields.get("name") if meta else None) or game.title,
        "cover": "covers" in api.game_media(game, extra),
    }


def _entry_body(e: Entry, ctx: Context, taken: Collection[str], now: int) -> dict[str, Any]:
    res = resolve(e.game, ctx.lib, taken)
    body: dict[str, Any] = {
        "id": e.game.id,
        "system": e.game.system,
        "title": e.game.title,
        "found": res.found,
        "candidates": [
            {"id": g.id, "system": s, "title": g.title, "name": _card(ctx, s, g)["name"]}
            for s, g in res.candidates
        ],
    }
    present = ctx.lib.by_id.get(e.game.id)
    play = None
    award = None
    if present is not None:
        system, game = present
        play = api.game_totals(ctx.index, system, game)
        award = api.game_award(ctx.lib, ctx.cfg, system, game)
        body.update(system=system, title=game.title, size=game.size, **_card(ctx, system, game))
        body["play"] = play
    shown, inferred = shown_state(e, play, now)
    body.update(
        state=e.state,
        shown=shown,
        inferred=inferred,
        suggestion=suggestion(e, award),
        added=e.added,
        changed=e.changed,
        ended=e.ended,
    )
    claims = {}
    configured = {d.name: d for d in api.library_devices(ctx.cfg)}
    for name, claim in e.devices.items():
        device = configured.get(name)
        state = "unknown"
        if present is not None and device is not None:
            state = api.game_state(device, ctx.views[name], *present)
        claims[name] = {
            "before": claim.before,
            "end": claim.end,
            "game": state,
            "view": device_view(claim, state, res.found, device is not None),
        }
    body["devices"] = claims
    return body


def queue_response(
    reading: Reading,
    queue: Queue,
    etag: str,
    size: int,
    ctx: Context,
    now: int,
    history: int = HISTORY_DEFAULT,
) -> dict[str, Any]:
    """`GET /api/v1/queue`: the queue resolved against the catalog, the whitelists, the play
    totals and the RA progress. `queue` is the queue to show (the reading's, or the one a
    request leaves)."""
    own = mine(queue)
    taken = {e.game.id for e in own}
    active = [e for e in own if e.state in ACTIVE]
    ended = sorted((e for e in own if e.state in ENDED), key=lambda e: e.ended or "", reverse=True)
    return {
        "state": reading.state,
        **({"message": f"queue.json: {reading.message}"} if reading.message else {}),
        "etag": etag,
        "updated": queue.updated or None,
        "entries": [_entry_body(e, ctx, taken, now) for e in active],
        "history": [_entry_body(e, ctx, taken, now) for e in ended[:history]],
        "history_total": len(ended),
        "size": size,
        "limit": SIZE_LIMIT,
        "collections": [
            {"id": cid, "title": c.title, "games": len(c.games)}
            for cid, c in sorted(
                queue.collections.items(), key=lambda item: (item[1].title, item[0])
            )
            if c.owner == OWNER
        ],
    }


def plan_response(
    plan_: QueuePlan,
    etag: str,
    size: int,
    ctx: Context,
    now: int,
    written: Mapping[str, bool] | None = None,
    warnings: Mapping[str, Sequence[str]] | None = None,
) -> dict[str, Any]:
    """The answer to a queue request: the queue after the change, and the whitelists as in the
    answer of `POST /api/v1/changes`. `etag` and `size` are the queue file's before it."""
    data = plan_.final
    if data is not None:
        etag, size = etag_of(data), len(data)
    after = replace(ctx, views=plan_.views)
    shown = Reading("ok", plan_.queue)
    done = api.changes_response(
        api.Plan(plan_.dry_run, plan_.devices, plan_.skipped), written, warnings
    )
    return {
        "dry_run": plan_.dry_run,
        "queue": queue_response(shown, plan_.queue, etag, size, after, now),
        "devices": done["devices"],
        "skipped": done["skipped"],
    }
