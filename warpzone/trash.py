"""Deleting a game from the library UI, as pure decisions (see "Deleting a game" in
development/design/library-ui.md).

Nothing here touches the disk, the network or a lock. `trash_store.py` lists what a plan needs
(the parts, the media, the gamelist, the arcade sets) and does the moves; `trash_run.py` takes
the locks and drives them; `serve.py` routes the requests. This module decides:

- `plan_delete`: what a deletion moves and what it refuses, in the order of the design, and the
  `expect` hash that ties a confirmation to the plan the user saw;
- the entry names, the manifest of an entry and the pending cleanups file (their encoding and
  their validation: a manifest or a file that is not this shape is damaged);
- which cleanup is ready (the hub has recorded the deletion and the console has received it),
  which lines of it are still needed, what a restore moves and what it refuses, and which
  entries are expired;
- the shape of the answers (`plan_response`, `trash_response`).
"""

from __future__ import annotations

import hashlib
import json
import math
import posixpath
import re
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from typing import Any

from warpzone import artwork, catalog, gamelist, inbox_mame, library_api, progress
from warpzone import whitelist as wl
from warpzone.config import Config
from warpzone.library_api import MANAGED, NEEDS_MIGRATION, ApiError, DeviceView, Library

TRASH_DIR = "trash"
MANIFEST = "manifest.json"
CLEANUPS_FILE = "cleanups.json"  # below `catalog/`
FORMAT = 1
TRASH_DAYS = 7  # how long an entry waits before the automatic purge
CLEANUP_SECONDS = 60  # the background task's period
LIBRARY = "library/"  # the paths of a manifest are relative to the hub root
MEDIA = "frontend/media/"
ENTRY_RE = re.compile(r"^\d{8}T\d{6}Z-[0-9a-f]{16}(-\d+)?$")
PURGING_PREFIX = ".purging-"  # `trash/.purging-<entry>`: an entry being removed
ID_RE = re.compile(r"^[0-9a-f]{16}$")
# The system folders whose games are arcade sets that may need another set (a parent, a BIOS).
ARCADE_SYSTEMS = ("mame", "arcade", "fbneo", "neogeo", "cps1", "cps2", "cps3")
MOVING, COMPLETE, RESTORING = "moving", "complete", "restoring"
MANIFEST_STATES = (MOVING, COMPLETE, RESTORING)
# What `lstat` found at a place.
F_FILE, F_DIR, F_LINK, F_OTHER, F_ABSENT = "file", "dir", "link", "other", "absent"
# The states of an entry as the API says them.
ENTRY_COMPLETE, ENTRY_INCOMPLETE, ENTRY_DAMAGED = "complete", "incomplete", "damaged"
TIME = "%Y-%m-%dT%H:%M:%SZ"
MIN_PART_DEPTH = 3  # `roms/<system>/<name>`: nothing shallower is ever one game's part


# --------------------------------------------------------------------------- paths and times


def safe_below(path: str, prefix: str) -> bool:
    """Whether `path` is a path below `prefix` (a folder of the hub, with its slash): relative,
    at least one component after the prefix and none that is empty, `.` or `..`."""
    if not path.startswith(prefix) or "\0" in path:
        return False
    return all(part not in ("", ".", "..") for part in path[len(prefix) :].split("/"))


def safe_part(path: str, system: str) -> bool:
    """Whether a catalog part path may be moved: below `library/` as `roms/<system>/...` or
    `extras/<system>/...`, with a name at least three components deep."""
    parts = path.split("/")
    if len(parts) < MIN_PART_DEPTH or any(p in ("", ".", "..") for p in parts) or "\0" in path:
        return False
    return wl.system_of(path) == system


def when_text(when: datetime) -> str:
    return when.astimezone(UTC).strftime(TIME)


def parse_when(text: object) -> datetime | None:
    if not isinstance(text, str):
        return None
    try:
        return datetime.strptime(text, TIME).replace(tzinfo=UTC)
    except ValueError:
        return None


def purge_time(deleted: datetime) -> datetime:
    return deleted + timedelta(days=TRASH_DAYS)


def days_left(purge_after: str, now: datetime) -> int | None:
    """The whole days until `purge_after`, rounded up and never below 0 (None for a time that
    does not parse)."""
    when = parse_when(purge_after)
    if when is None:
        return None
    return max(0, math.ceil((when - now).total_seconds() / 86400))


def entry_name(when: datetime, game_id: str, taken: Collection[str] = ()) -> str:
    """`<UTC time>-<game id>`, with `-2`, `-3`... when that name is taken (a second deletion
    of the same game in the same second)."""
    base = f"{when.astimezone(UTC).strftime('%Y%m%dT%H%M%SZ')}-{game_id}"
    name, count = base, 1
    while name in taken:
        count += 1
        name = f"{base}-{count}"
    return name


def valid_entry(name: str) -> bool:
    return ENTRY_RE.fullmatch(name) is not None


def purging_name(entry: str) -> str:
    """The name an entry is renamed to before it is removed: not an entry (listings, Restore
    and the manifest reader never see it), so what a failed removal leaves cannot be restored."""
    return PURGING_PREFIX + entry


def purging_entry(name: str) -> str | None:
    """The entry a `purging_name` was made from, or None when `name` is not one."""
    if not name.startswith(PURGING_PREFIX):
        return None
    entry = name[len(PURGING_PREFIX) :]
    return entry if valid_entry(entry) else None


# --------------------------------------------------------------------------- lines


@dataclass(frozen=True)
class Lines:
    """The includes and the excludes a device's whitelist holds for a game."""

    include: tuple[str, ...] = ()
    exclude: tuple[str, ...] = ()

    def __bool__(self) -> bool:
        return bool(self.include or self.exclude)

    @property
    def count(self) -> int:
        return len(self.include) + len(self.exclude)

    def texts(self) -> list[str]:
        """The lines as the file writes them: the exclusions, then the includes with `!`."""
        return [wl.escape(e) for e in self.exclude] + ["!" + wl.escape(i) for i in self.include]

    def json(self) -> dict[str, list[str]]:
        return {"include": list(self.include), "exclude": list(self.exclude)}


def _lines_from_json(raw: object) -> Lines | None:
    if not isinstance(raw, dict) or set(raw) != {"include", "exclude"}:
        return None
    include, exclude = raw["include"], raw["exclude"]
    if not (
        isinstance(include, list)
        and isinstance(exclude, list)
        and all(isinstance(p, str) and wl.check_path(p) is None for p in (*include, *exclude))
    ):
        return None
    return Lines(tuple(include), tuple(exclude))


# --------------------------------------------------------------------------- the plan's inputs


@dataclass(frozen=True)
class Found:
    """What `lstat` found at a part's place: `kind` is one of the `F_*` constants; a folder
    carries the total size and the count of the files below it (no link followed)."""

    kind: str
    size: int = 0
    mtime_ns: int = 0
    files: int = 0


@dataclass(frozen=True)
class MediaFound:
    """One entry of a kind folder, `path` below `frontend/media/`: `<system>/<kind>/<name>`."""

    path: str
    kind: str  # F_FILE, F_LINK or F_OTHER
    size: int = 0
    mtime_ns: int = 0


@dataclass(frozen=True)
class Arcade:
    """The arcade set databases `serve` could read; none means it cannot tell what depends on a
    set."""

    dbs: tuple[inbox_mame.SetDb, ...] = ()


@dataclass(frozen=True)
class Facts:
    """What the I/O layer read for a plan."""

    parts: Mapping[str, Found] = field(default_factory=dict)  # by the part's path
    media: Sequence[MediaFound] = ()  # the entries of the folders a stem could be in
    media_links: Sequence[str] = ()  # system or kind folders that are links, left alone
    gamelist: str | None = None  # the hub gamelist's text; None when there is no such file
    gamelist_error: str | None = None  # why it cannot be read or written
    arcade: Arcade | None = None
    trash_problem: str | None = None  # why `trash/` cannot take a move
    cleanups_problem: str | None = None  # why the pending cleanups cannot be read


@dataclass(frozen=True)
class PlanFile:
    path: str  # below `library/`
    type: str  # catalog.FILE or catalog.DIR
    size: int
    files: int | None  # a folder: the files below it
    mtime_ns: int = 0

    def json(self) -> dict[str, Any]:
        out: dict[str, Any] = {"path": self.path, "type": self.type, "size": self.size}
        if self.type == catalog.DIR:
            out["files"] = self.files or 0
        return out


@dataclass(frozen=True)
class PlanMedia:
    path: str  # below `frontend/media/`
    size: int
    mtime_ns: int = 0

    def json(self) -> dict[str, Any]:
        return {"path": self.path, "size": self.size}


@dataclass(frozen=True)
class DeletePlan:
    game: catalog.Game
    system: str
    name: str  # the gamelist name, else the title
    files: tuple[PlanFile, ...]
    absent: tuple[str, ...]
    media: tuple[PlanMedia, ...]
    gamelist: bool  # whether an entry of the hub gamelist is removed
    gamelist_failed: bool  # the gamelist cannot be read or changed: its entry stays
    gamelist_paths: tuple[str, ...]  # the `<path>` values that are this game's alone
    blocks: tuple[str, ...]  # the entries as the plan found them
    whitelists: Mapping[str, Lines]
    warnings: tuple[str, ...]
    expect: str

    @property
    def size(self) -> int:
        return sum(f.size for f in self.files)

    @property
    def media_size(self) -> int:
        return sum(m.size for m in self.media)

    @property
    def paths(self) -> tuple[str, ...]:
        """Every part, absent or not: the paths whose lines the cleanup waits for."""
        return tuple(p.path for p in self.game.parts)


# --------------------------------------------------------------------------- the plan


def media_stems(game: catalog.Game) -> frozenset[str]:
    """The stems a media file of the game may carry: `media_stem` of every path the lookup
    tries (`library_api.gamelist_paths`), in the file form and in the folder form."""
    found: set[str] = set()
    for path in library_api.gamelist_paths(game):
        for entry in (None, artwork.Entry("folder", path)):
            stem = library_api.media_stem(path, entry)
            if stem:
                found.add(stem)
    return frozenset(found)


def own_stems(game: catalog.Game, others: Sequence[catalog.Game]) -> frozenset[str]:
    """The game's stems that no other game of its system resolves to."""
    taken: set[str] = set()
    for other in others:
        taken |= media_stems(other)
    return media_stems(game) - taken


def media_dirs(stems: Collection[str]) -> list[str]:
    """The folders below a kind folder where files of `stems` would be: `""` for the kind
    folder itself, else the folders the stems name. The I/O layer lists those, for every kind
    folder that exists below `frontend/media/<system>/`."""
    return sorted({posixpath.dirname(stem) for stem in stems})


def _others(lib: Library, system: str, game: catalog.Game) -> list[catalog.Game]:
    return [g for g in lib.systems.get(system, {}).values() if g.id != game.id]


def _unsafe(path: str, why: str) -> ApiError:
    return ApiError("unsafe-path", f"{path}: {why}", extra={"paths": [path]})


def _check_parts(
    game: catalog.Game, system: str, lib: Library, facts: Facts
) -> tuple[list[PlanFile], list[str]]:
    """Every refusal about the parts (the second of the design) and the parts to move."""
    others: set[str] = set()
    for _, other in lib.by_id.values():
        if other.id != game.id:
            others.update(p.path for p in other.parts)
    above: set[str] = set()  # every folder above a part of another game, and the part itself
    for path in others:
        above.update(wl.ancestors(path))
    found: list[PlanFile] = []
    absent: list[str] = []
    for part in game.parts:
        path = part.path
        if not safe_part(path, system):
            raise _unsafe(path, "not a path this game may be moved from")
        if path in above or any(a in others for a in wl.ancestors(path)):
            raise _unsafe(path, "is, is inside or is above a part of another game")
        seen = facts.parts.get(path, Found(F_ABSENT))
        if seen.kind == F_ABSENT:
            absent.append(path)
        elif seen.kind == F_LINK:
            raise _unsafe(path, "is a symbolic link")
        elif seen.kind != part.type or seen.kind not in (catalog.FILE, catalog.DIR):
            raise _unsafe(path, f"is not a {part.type} (the catalog says it is)")
        else:
            found.append(
                PlanFile(
                    path,
                    part.type,
                    seen.size,
                    seen.files if part.type == catalog.DIR else None,
                    seen.mtime_ns,
                )
            )
    return found, absent


def _dependants(
    game: catalog.Game, system: str, lib: Library, arcade: Arcade
) -> list[dict[str, str]]:
    """The other zips of the system folder whose set needs this one as parent, BIOS or device."""
    name = _set_name(game.primary)
    if name is None:
        return []
    found = []
    for other in _others(lib, system, game):
        theirs = _set_name(other.primary)
        if theirs is None:
            continue
        for db in arcade.dbs:
            if theirs in db.machines and name in inbox_mame.pool_names(db, theirs):
                found.append(
                    {
                        "id": other.id,
                        "title": other.title,
                        "file": posixpath.basename(other.primary),
                    }
                )
                break
    return sorted(found, key=lambda d: (d["title"], d["id"]))


def _set_name(primary: str) -> str | None:
    base = posixpath.basename(primary)
    return base[:-4] if base.lower().endswith(".zip") else None


def _canonical(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def plan_expect(
    files: Sequence[PlanFile],
    media: Sequence[PlanMedia],
    gamelist_removed: bool,
    whitelists: Mapping[str, Lines],
) -> str:
    """`sha256:<hex>` of the canonical JSON of what the dry run shows: `files`, `media`,
    `gamelist` and `whitelists`."""
    body = {
        "files": [f.json() for f in files],
        "media": [m.json() for m in media],
        "gamelist": gamelist_removed,
        "whitelists": {d: lines.texts() for d, lines in sorted(whitelists.items()) if lines},
    }
    return "sha256:" + hashlib.sha256(_canonical(body).encode("utf-8")).hexdigest()


def plan_delete(
    game_id: str,
    lib: Library,
    cfg: Config,
    views: Mapping[str, DeviceView],
    facts: Facts,
) -> DeletePlan:
    """What deleting one catalog game does, or the first thing that refuses it (pure).

    The refusals, in the order of the design: an unknown game (`not-found`); a part that is a
    link, of another type than the catalog says, or that another game shares or contains
    (`unsafe-path`); a device whose whitelist needs migration (`not-managed`); an arcade set
    that others need (`has-dependants`, or `dats-missing` when nothing tells); a `trash/` that
    cannot take a move (`trash-unavailable`); pending cleanups that cannot be read
    (`read-failed`). A game none of whose files is on the hub is `not-found` too: the catalog
    is out of date and a run drops it."""
    found = lib.by_id.get(game_id)
    if found is None:
        raise ApiError("not-found", f"no game {game_id!r}", game=game_id)
    system, game = found
    files, absent = _check_parts(game, system, lib, facts)
    if not files:
        raise ApiError(
            "not-found",
            "none of the game's files is on the hub: the catalog is out of date",
            game=game_id,
        )
    devices = library_api.library_devices(cfg)
    for device in devices:
        if views[device.name].state == NEEDS_MIGRATION:
            raise ApiError(
                "not-managed",
                f"{device.name}: the whitelist needs `warpzone whitelist migrate` first",
                device=device.name,
            )
    if system in ARCADE_SYSTEMS:
        if facts.arcade is None or not facts.arcade.dbs:
            raise ApiError(
                "dats-missing",
                "no arcade DAT can be read by the server, so it cannot tell whether other "
                "sets need this one",
                game=game_id,
            )
        dependants = _dependants(game, system, lib, facts.arcade)
        if dependants:
            names = ", ".join(d["title"] for d in dependants)
            raise ApiError(
                "has-dependants",
                f"other sets need this one ({names}); delete them first or keep it",
                game=game_id,
                extra={"dependants": dependants},
            )
    if facts.trash_problem:
        raise ApiError("trash-unavailable", facts.trash_problem, game=game_id)
    if facts.cleanups_problem:
        raise ApiError("read-failed", facts.cleanups_problem, game=game_id)
    warnings = [f"{path} is a link and was left alone" for path in facts.media_links]
    media = _media_of(game, system, lib, facts, warnings)
    removed, failed, blocks, paths, name = _gamelist_of(game, system, lib, facts, warnings)
    whitelists = {}
    for device in devices:
        model = views[device.name].model
        lines = Lines(*wl.lines_below(model, [p.path for p in game.parts])) if model else Lines()
        if lines:
            whitelists[device.name] = lines
    return DeletePlan(
        game,
        system,
        name or game.title,
        tuple(files),
        tuple(absent),
        tuple(media),
        removed,
        failed,
        paths,
        blocks,
        whitelists,
        tuple(warnings),
        plan_expect(files, media, removed, whitelists),
    )


def _media_of(
    game: catalog.Game, system: str, lib: Library, facts: Facts, warnings: list[str]
) -> list[PlanMedia]:
    stems = own_stems(game, _others(lib, system, game))
    media: list[PlanMedia] = []
    for item in sorted(facts.media, key=lambda m: m.path):
        parts = item.path.split("/", 2)
        if len(parts) != 3 or parts[0] != system or not safe_below(item.path, ""):
            continue
        stem, ext = posixpath.splitext(parts[2])
        if not ext or stem not in stems:
            continue
        if item.kind == F_FILE:
            media.append(PlanMedia(item.path, item.size, item.mtime_ns))
        elif item.kind == F_LINK:
            warnings.append(f"{MEDIA}{item.path} is a link and was left alone")
    return media


def _gamelist_of(
    game: catalog.Game, system: str, lib: Library, facts: Facts, warnings: list[str]
) -> tuple[bool, bool, tuple[str, ...], tuple[str, ...], str | None]:
    """(an entry is removed, the gamelist cannot be changed, the blocks, the `<path>` values
    that are only this game's, the gamelist name)."""
    if facts.gamelist_error:
        warnings.append(f"the gamelist cannot be changed ({facts.gamelist_error}): its entry stays")
        return False, True, (), (), None
    if facts.gamelist is None:
        return False, False, (), (), None
    theirs: set[str] = set()
    for other in _others(lib, system, game):
        theirs.update(library_api.gamelist_paths(other))
    paths = tuple(p for p in library_api.gamelist_paths(game) if p not in theirs)
    name = None
    try:
        entries = {e.path: e for e in artwork.parse_gamelist(facts.gamelist)}
        entry = library_api.game_entry(game, library_api.SystemExtras(meta=entries))
        name = entry.fields.get("name") if entry is not None else None
    except artwork.ArtworkError:
        pass
    try:
        _, blocks = gamelist.drop_entries(facts.gamelist, paths)
    except gamelist.GamelistError as exc:
        warnings.append(f"the gamelist cannot be changed ({exc}): its entry stays")
        return False, True, (), (), name
    return bool(blocks), False, tuple(blocks), paths, name


def plan_response(
    plan: DeletePlan,
    *,
    saves: Mapping[str, Any],
    busy: Mapping[str, Any] | None,
    purge_after: str,
) -> dict[str, Any]:
    """The answer of a dry run."""
    return {
        "dry_run": True,
        "game": {
            "id": plan.game.id,
            "system": plan.system,
            "title": plan.game.title,
            "name": plan.name,
        },
        "expect": plan.expect,
        "files": [f.json() for f in plan.files],
        "absent": list(plan.absent),
        "size": plan.size,
        "media": [m.json() for m in plan.media],
        "media_size": plan.media_size,
        "gamelist": plan.gamelist,
        "whitelists": {d: lines.texts() for d, lines in sorted(plan.whitelists.items())},
        "saves": dict(saves),
        "busy": dict(busy) if busy is not None else None,
        "purge_after": purge_after,
        "warnings": list(plan.warnings),
    }


def check_expect(plan: DeletePlan, expect: str | None) -> None:
    """`428 expect-missing` without an `expect`, `412 stale` when it is not the plan's."""
    if expect is None:
        raise ApiError("expect-missing", "a deletion is applied with the `expect` of its dry run")
    if expect != plan.expect:
        raise ApiError(
            "stale",
            "the game changed since the dry run: look at the plan again",
            game=plan.game.id,
        )


@dataclass(frozen=True)
class DeleteRequest:
    dry_run: bool
    expect: str | None = None


def parse_delete(body: object) -> DeleteRequest:
    """The body of `POST /api/v1/games/{id}/delete`: `dry_run` (required) and `expect`."""
    if not isinstance(body, dict) or set(body) - {"dry_run", "expect"}:
        raise ApiError("bad-request", 'the body must be {"dry_run": ..., "expect": ...}')
    dry_run, expect = body.get("dry_run"), body.get("expect")
    if not isinstance(dry_run, bool):
        raise ApiError("bad-request", "`dry_run` must be true or false")
    if expect is not None and not isinstance(expect, str):
        raise ApiError("bad-request", "`expect` must be a string")
    return DeleteRequest(dry_run, expect)


def parse_empty(body: object) -> None:
    """The body of a restore or a purge: an empty object."""
    if body != {}:
        raise ApiError("bad-request", "the body must be {}")


# --------------------------------------------------------------------------- the manifest


@dataclass(frozen=True)
class Moved:
    """One path of a manifest, relative to the hub root."""

    path: str
    type: str  # catalog.FILE or catalog.DIR
    size: int
    files: int | None = None


@dataclass(frozen=True)
class SavedGamelist:
    path: str  # `frontend/es-de/gamelists/<system>/gamelist.xml`
    entries: tuple[str, ...]
    backup: str | None = None


@dataclass(frozen=True)
class Manifest:
    entry: str
    state: str
    deleted: str
    purge_after: str
    system: str
    id: str
    name: str
    game: catalog.Game
    files: tuple[Moved, ...]
    media: tuple[Moved, ...]
    gamelist: SavedGamelist | None
    whitelists: Mapping[str, Lines]

    @property
    def paths(self) -> tuple[str, ...]:
        """Every path to move, parts first, then media."""
        return tuple(m.path for m in (*self.files, *self.media))

    @property
    def size(self) -> int:
        return sum(m.size for m in (*self.files, *self.media))


def gamelist_path(system: str) -> str:
    return f"frontend/es-de/gamelists/{system}/gamelist.xml"


def new_manifest(
    plan: DeletePlan,
    entry: str,
    when: datetime,
    *,
    state: str = MOVING,
    blocks: Sequence[str] | None = None,
    backup: str | None = None,
) -> Manifest:
    """The manifest of a deletion: every path to move, and the gamelist blocks (the plan's
    until the removal found the real ones)."""
    use = tuple(plan.blocks if blocks is None else blocks)
    saved = SavedGamelist(gamelist_path(plan.system), use, backup) if use else None
    return Manifest(
        entry,
        state,
        when_text(when),
        when_text(purge_time(when)),
        plan.system,
        plan.game.id,
        plan.name,
        plan.game,
        tuple(Moved(LIBRARY + f.path, f.type, f.size, f.files) for f in plan.files),
        tuple(Moved(MEDIA + m.path, catalog.FILE, m.size) for m in plan.media),
        saved,
        dict(plan.whitelists),
    )


def encode_manifest(m: Manifest) -> bytes:
    doc = {
        "format": FORMAT,
        "entry": m.entry,
        "state": m.state,
        "deleted": m.deleted,
        "purge_after": m.purge_after,
        "system": m.system,
        "id": m.id,
        "name": m.name,
        "game": catalog.encode_game(m.game),
        "files": [
            {"path": f.path, "type": f.type, "size": f.size}
            | ({"files": f.files or 0} if f.type == catalog.DIR else {})
            for f in m.files
        ],
        "media": [{"path": f.path, "size": f.size} for f in m.media],
        "gamelist": None
        if m.gamelist is None
        else {
            "path": m.gamelist.path,
            "entries": list(m.gamelist.entries),
            "backup": m.gamelist.backup,
        },
        "whitelists": {d: lines.json() for d, lines in sorted(m.whitelists.items())},
    }
    return (json.dumps(doc, sort_keys=True, indent=1, ensure_ascii=False) + "\n").encode("utf-8")


def _int(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


def _moved(raw: object, prefix: str, *, media: bool) -> Moved | None:
    if not isinstance(raw, dict):
        return None
    path, size = raw.get("path"), _int(raw.get("size"))
    if not isinstance(path, str) or not safe_below(path, prefix) or size is None:
        return None
    if media:
        return Moved(path, catalog.FILE, size)
    kind = raw.get("type")
    if kind not in (catalog.FILE, catalog.DIR):
        return None
    count = _int(raw.get("files")) if kind == catalog.DIR else None
    if kind == catalog.DIR and count is None:
        return None
    return Moved(path, kind, size, count)


def parse_manifest(data: bytes, entry: str) -> Manifest | None:
    """The manifest in `data` for the entry folder named `entry`, or None when it is not one
    (the entry is then damaged): not this format, another entry's, a path outside `library/`
    and `frontend/media/`, a path inside another of its paths."""
    try:
        doc = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return None
    if not isinstance(doc, dict) or doc.get("format") != FORMAT or doc.get("entry") != entry:
        return None
    state, system, ident, name = doc.get("state"), doc.get("system"), doc.get("id"), doc.get("name")
    deleted, purge_after = doc.get("deleted"), doc.get("purge_after")
    if (
        state not in MANIFEST_STATES
        or not isinstance(system, str)
        or not wl.SYSTEM_RE.fullmatch(system)
        or not isinstance(ident, str)
        or not ID_RE.fullmatch(ident)
        or not isinstance(name, str)
        or parse_when(deleted) is None
        or parse_when(purge_after) is None
    ):
        return None
    game = catalog.decode_game(ident, doc.get("game"))
    raw_files, raw_media = doc.get("files"), doc.get("media")
    if game is None or not isinstance(raw_files, list) or not isinstance(raw_media, list):
        return None
    files = [_moved(f, LIBRARY, media=False) for f in raw_files]
    media = [_moved(f, MEDIA, media=True) for f in raw_media]
    if any(f is None for f in files) or any(f is None for f in media):
        return None
    kept = tuple(f for f in files if f is not None)
    kept_media = tuple(f for f in media if f is not None)
    every = [f.path for f in (*kept, *kept_media)]
    if len(set(every)) != len(every) or any(
        a != b and (b.startswith(a + "/")) for a in every for b in every
    ):
        return None
    try:
        saved = _saved_gamelist(doc.get("gamelist"), system)
    except ValueError:
        return None
    raw_lists = doc.get("whitelists")
    if not isinstance(raw_lists, dict):
        return None
    lists = {}
    for device, raw in raw_lists.items():
        lines = _lines_from_json(raw)
        if lines is None:
            return None
        lists[device] = lines
    return Manifest(
        entry,
        state,
        str(deleted),
        str(purge_after),
        system,
        ident,
        name,
        game,
        kept,
        kept_media,
        saved,
        lists,
    )


def _saved_gamelist(raw: object, system: str) -> SavedGamelist | None:
    """The saved gamelist entries (None for `null`); `ValueError` for a shape that is not one."""
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise ValueError("gamelist")
    entries, backup = raw.get("entries"), raw.get("backup")
    if (
        raw.get("path") != gamelist_path(system)
        or not isinstance(entries, list)
        or not all(isinstance(e, str) for e in entries)
        or not (backup is None or (isinstance(backup, str) and safe_below(backup, "")))
    ):
        raise ValueError("gamelist")
    return SavedGamelist(gamelist_path(system), tuple(entries), backup)


def with_state(m: Manifest, state: str) -> Manifest:
    return replace(m, state=state)


# --------------------------------------------------------------------------- the entries


@dataclass(frozen=True)
class Listed:
    """One entry folder of `trash/` as the I/O layer read it: `manifest` is None for a damaged
    one."""

    entry: str
    manifest: Manifest | None


def entry_state(item: Listed) -> str:
    if item.manifest is None:
        return ENTRY_DAMAGED
    return ENTRY_COMPLETE if item.manifest.state == COMPLETE else ENTRY_INCOMPLETE


def expired(item: Listed, now: datetime) -> bool:
    """Whether the automatic purge removes the entry: complete, and past its `purge_after`.
    An incomplete or damaged entry waits for Delete now."""
    m = item.manifest
    if m is None or m.state != COMPLETE:
        return False
    when = parse_when(m.purge_after)
    return when is not None and when <= now


def _entry_row(item: Listed, now: datetime) -> dict[str, Any]:
    m = item.manifest
    state = entry_state(item)
    if m is None:
        return {
            "entry": item.entry,
            "id": None,
            "system": None,
            "title": None,
            "name": None,
            "deleted": None,
            "purge_after": None,
            "days_left": None,
            "size": None,
            "files": None,
            "media": None,
            "state": state,
        }
    return {
        "entry": item.entry,
        "id": m.id,
        "system": m.system,
        "title": m.game.title,
        "name": m.name,
        "deleted": m.deleted,
        "purge_after": m.purge_after,
        "days_left": days_left(m.purge_after, now) if state == ENTRY_COMPLETE else None,
        "size": m.size,
        "files": len(m.files),
        "media": len(m.media),
        "state": state,
    }


def trash_response(
    listed: Sequence[Listed], pending: Sequence[Mapping[str, Any]], now: datetime
) -> dict[str, Any]:
    """The answer of `GET /api/v1/trash`: the entries, newest first, and the cleanups that
    wait."""
    rows = [_entry_row(item, now) for item in sorted(listed, key=lambda i: i.entry, reverse=True)]
    return {
        "size": sum(item.manifest.size for item in listed if item.manifest is not None),
        "entries": rows,
        "pending": [dict(p) for p in pending],
    }


def restore_response(
    m: Manifest,
    moved: tuple[int, int],
    gamelist_result: str,
    kept: Sequence[str],
    removed: Sequence[str],
    warnings: Sequence[str],
) -> dict[str, Any]:
    return {
        "entry": m.entry,
        "game": {"id": m.id, "system": m.system, "title": m.game.title},
        "restored": {"files": moved[0], "media": moved[1]},
        "gamelist": gamelist_result,
        "kept": sorted(kept),
        "removed": sorted(removed),
        "warnings": list(warnings),
    }


# --------------------------------------------------------------------------- restore


@dataclass(frozen=True)
class RestorePlan:
    moves: tuple[str, ...]  # in the trash and not yet at its place: to move back
    skipped: tuple[str, ...]  # in neither place
    conflicts: tuple[str, ...]  # at its place again and in the trash too


def plan_restore(m: Manifest, at_place: Collection[str], in_trash: Collection[str]) -> RestorePlan:
    """What a restore does with each path of the manifest, from where `lstat` found it. A path
    in both places is a conflict (a copy filed again, new art from a scrape); one in neither is
    skipped (an incomplete entry); one only at its place never left and needs nothing."""
    moves: list[str] = []
    skipped: list[str] = []
    conflicts: list[str] = []
    for path in m.paths:
        here, there = path in at_place, path in in_trash
        if here and there:
            conflicts.append(path)
        elif there:
            moves.append(path)
        elif not here:
            skipped.append(path)
    return RestorePlan(tuple(moves), tuple(skipped), tuple(conflicts))


# --------------------------------------------------------------------------- the cleanups


class CleanupsError(ValueError):
    """The pending cleanups file is not one."""


@dataclass(frozen=True)
class Cleanup:
    """The whitelist lines of one deleted game that wait for the consoles."""

    entry: str
    id: str
    system: str
    title: str
    deleted: str
    recorded: str | None  # when the hub's index was seen to hold the deletion
    paths: tuple[str, ...]  # the game's parts, below `library/`
    devices: Mapping[str, Lines]

    def json(self) -> dict[str, Any]:
        return {
            "entry": self.entry,
            "id": self.id,
            "system": self.system,
            "title": self.title,
            "deleted": self.deleted,
            "recorded": self.recorded,
            "paths": list(self.paths),
            "devices": {d: lines.json() for d, lines in sorted(self.devices.items())},
        }


def new_cleanup(plan: DeletePlan, entry: str, when: datetime) -> Cleanup | None:
    """The cleanup a deletion records, or None when no whitelist names the game."""
    if not plan.whitelists:
        return None
    return Cleanup(
        entry,
        plan.game.id,
        plan.system,
        plan.game.title,
        when_text(when),
        None,
        plan.paths,
        dict(plan.whitelists),
    )


def encode_cleanups(items: Sequence[Cleanup]) -> bytes:
    doc = {"format": FORMAT, "cleanups": [c.json() for c in items]}
    return (json.dumps(doc, sort_keys=True, indent=1, ensure_ascii=False) + "\n").encode("utf-8")


def _cleanup_from_json(raw: object) -> Cleanup | None:
    if not isinstance(raw, dict):
        return None
    entry, ident, system, title = (raw.get(k) for k in ("entry", "id", "system", "title"))
    deleted, recorded, paths = raw.get("deleted"), raw.get("recorded"), raw.get("paths")
    raw_devices = raw.get("devices")
    if (
        not (isinstance(entry, str) and valid_entry(entry))
        or not (isinstance(ident, str) and ID_RE.fullmatch(ident))
        or not (isinstance(system, str) and wl.SYSTEM_RE.fullmatch(system))
        or not isinstance(title, str)
        or parse_when(deleted) is None
        or not (recorded is None or parse_when(recorded) is not None)
        or not (
            isinstance(paths, list)
            and all(isinstance(p, str) and safe_part(p, system) for p in paths)
        )
        or not isinstance(raw_devices, dict)
    ):
        return None
    devices = {}
    for device, rawlines in raw_devices.items():
        lines = _lines_from_json(rawlines)
        if lines is None:
            return None
        devices[device] = lines
    return Cleanup(
        entry,
        ident,
        system,
        title,
        str(deleted),
        recorded,
        tuple(paths),
        devices,
    )


def parse_cleanups(data: bytes | None) -> list[Cleanup]:
    """The cleanups in the file's bytes (None: no file, none waiting); `CleanupsError` when it
    is not a cleanups file."""
    if data is None:
        return []
    try:
        doc = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise CleanupsError("it is not UTF-8 JSON") from exc
    if not isinstance(doc, dict) or doc.get("format") != FORMAT:
        raise CleanupsError("it is not a cleanups file of this version")
    raw = doc.get("cleanups")
    if not isinstance(raw, list):
        raise CleanupsError("it has no list of cleanups")
    found = [_cleanup_from_json(r) for r in raw]
    if any(c is None for c in found):
        raise CleanupsError("one of its cleanups is not in the right shape")
    return [c for c in found if c is not None]


def in_index(answers: Mapping[str, Mapping[str, Any] | None]) -> bool:
    """Whether the hub's Syncthing has recorded the deletion of every path: `db/file` gave no
    record (None: it does not know the path) or a global record that is `deleted`. An answer
    that has no global record to read (none, not a mapping, an empty one) is not a recorded
    deletion: this fails closed."""
    for answer in answers.values():
        if answer is None:
            continue
        record = answer.get("global")
        if not isinstance(record, Mapping) or record.get("deleted") is not True:
            return False
    return True


# What a device's lines wait for, as the API says it.
W_HUB, W_OFFLINE, W_PAUSED, W_NOT_SHARED, W_SYNCING = (
    "hub",
    "offline",
    "paused",
    "not-shared",
    "syncing",
)
W_NOT_MANAGED, W_UNKNOWN = "not-managed", "unknown"


def waiting(*, recorded: bool, managed: bool, reading: progress.Reading | None) -> str | None:
    """What a device's lines wait for, or None when they can go: the whitelist is managed, the
    hub has recorded the deletion, and a fresh reading of the hub shows the device connected,
    the folder shared and not paused, and nothing left to fetch or delete: completion 100,
    `needItems` 0, `needBytes` 0 and `needDeletes` 0 (Syncthing counts a pending deletion only
    there, and caps the completion at 95 while only deletions remain). A `needDeletes` the hub
    did not give is unknown, so not ready."""
    if not managed:
        return W_NOT_MANAGED
    if not recorded:
        return W_HUB
    if reading is None:
        return W_UNKNOWN
    if not reading.connected:
        return W_OFFLINE
    if not reading.shared or reading.remote_state == "notSharing":
        return W_NOT_SHARED
    if reading.remote_state == "paused":
        return W_PAUSED
    if (
        reading.completion < 100
        or reading.need_items > 0
        or reading.need_bytes > 0
        or (reading.need_deletes or 0) > 0
    ):
        return W_SYNCING
    return W_UNKNOWN if reading.need_deletes is None else None


def waiting_from_state(state: str | None, *, recorded: bool, managed: bool) -> str:
    """The same words for the listing, from the cached progress state of the device (the
    listing does not ask the hub again for every page); None is a hub that did not answer."""
    if not managed:
        return W_NOT_MANAGED
    if not recorded:
        return W_HUB
    if state is None:
        return W_UNKNOWN
    return {
        progress.OFFLINE: W_OFFLINE,
        progress.NOT_SHARED: W_NOT_SHARED,
        progress.PAUSED: W_PAUSED,
    }.get(state, W_SYNCING)


def pending_rows(
    cleanups: Sequence[Cleanup], why: Mapping[tuple[str, str], str]
) -> list[dict[str, Any]]:
    """The `pending` list of the trash answer: one row per device of each cleanup, `why`
    giving what it waits for by (entry, device)."""
    rows = []
    for c in sorted(cleanups, key=lambda c: c.entry):
        for device, lines in sorted(c.devices.items()):
            rows.append(
                {
                    "device": device,
                    "entry": c.entry,
                    "title": c.title,
                    "lines": lines.count,
                    "since": c.deleted,
                    "waiting": why.get((c.entry, device), W_UNKNOWN),
                }
            )
    return rows


def reappeared(c: Cleanup, existing: Collection[str]) -> Cleanup | None:
    """The cleanup without the paths that exist on the hub again (restored, or filed again by
    `inbox`) and the lines at or below them: those lines now serve the game that is there.
    None when no line is left."""
    back = [p for p in c.paths if p in existing]
    if not back:
        return c

    def keep(paths: tuple[str, ...]) -> tuple[str, ...]:
        return tuple(p for p in paths if not any(wl.covers(b, p) for b in back))

    devices = {}
    for device, lines in c.devices.items():
        left = Lines(keep(lines.include), keep(lines.exclude))
        if left:
            devices[device] = left
    if not devices:
        return None
    return replace(c, paths=tuple(p for p in c.paths if p not in back), devices=devices)


def only_devices(c: Cleanup, kept: Collection[str]) -> Cleanup | None:
    """The cleanup limited to `kept` devices; None when none is left (a device that left the
    config, or no longer lists `library`, is dropped without a write)."""
    devices = {d: lines for d, lines in c.devices.items() if d in kept}
    return replace(c, devices=devices) if devices else None


def without_device(c: Cleanup, device: str) -> Cleanup | None:
    devices = {d: lines for d, lines in c.devices.items() if d != device}
    return replace(c, devices=devices) if devices else None


def effect_holds(old: wl.Model, new: wl.Model, lib: Library) -> bool:
    """Whether dropping lines leaves every catalog game in the state it had: a write that would
    turn a game on or off is not made."""
    return not wl.effect(old, new, lib.wl_games).changed


def cleaned_model(model: wl.Model, c: Cleanup, device: str) -> wl.Model:
    """The device's model without the lines recorded for the deleted game."""
    lines = c.devices[device]
    return wl.drop_lines(model, lines.include, lines.exclude)


def is_managed(view: DeviceView) -> bool:
    return view.state == MANAGED
