"""Scraped artwork and metadata for a frontend that is not ES-DE. Pure: no I/O but listing.

The hub keeps ES-DE's gamelists and downloaded media. A device whose profile has `[artwork]`
receives a slice of them (`media_ignores`, `metadata_ignores`) and a converter, picked by the
profile's `format`, turns each ES-DE gamelist into the frontend's own (`build`, then the
format's `render`). A frontend that reads another file shape is one more `Format` in
`FORMATS`; the rest, including the profile and `provision`, does not change.
See development/design/frontend-artwork.md, "Frontend artwork".
"""

from __future__ import annotations

import posixpath
import re
from collections.abc import Callable, Collection, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from xml.etree import ElementTree as ET

from warpzone import frontend
from warpzone.errors import WarpzoneError

# A system folder that can be written as an ignore pattern as it is.
_PLAIN = re.compile(r"^[A-Za-z0-9_-][A-Za-z0-9._-]*$")
# Characters XML 1.0 cannot hold.
_ILLEGAL = re.compile("[\x00-\x08\x0b\x0c\x0e-\x1f\ud800-\udfff￾￿]")


STATE_TAGS = {"favorite": "favorite", "playcount": "playcount", "lastplayed": "lastplayed"}
# ES-DE also records the seconds played in `<playtime>`. A frontend whose tags name no `playtime`
# records none, and its ledger says nothing about it.
ESDE_STATE_TAGS = {**STATE_TAGS, "playtime": "playtime"}


class ArtworkError(WarpzoneError):
    pass


@dataclass(frozen=True)
class Entry:
    """One `<game>` or `<folder>` of a gamelist: its path as written and its scalar fields."""

    tag: str
    path: str
    fields: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class Format:
    """A frontend's gamelist: what it keeps from ES-DE, what it saves itself, how it is written.

    `source_fields` are copied from the ES-DE gamelist and `user_fields` belong to the device
    (what the frontend saves while the player plays): they are read back from the existing
    file and never taken from the hub. `media_tags` maps each hub media kind to the tag that
    holds its path; kinds that share a tag compete, the profile's order decides.
    """

    name: str
    source_fields: tuple[str, ...]
    user_fields: tuple[str, ...]
    media_tags: Mapping[str, str]
    render: Callable[[Sequence[Entry]], str]
    order: tuple[str, ...] = ()
    # The tags that hold the favourite, the play count and the last played time, for
    # `playstate`, which needs `true`, a number and `YYYYMMDDTHHMMSS` in local time. An optional
    # `playtime` key names the tag with the seconds played (a number); without it the format
    # records no playtime.
    state_tags: Mapping[str, str] = field(default_factory=lambda: dict(ESDE_STATE_TAGS))

    def image_tags(self) -> tuple[str, ...]:
        return tuple(dict.fromkeys(self.media_tags.values()))

    def known(self) -> frozenset[str]:
        return frozenset((*self.source_fields, *self.image_tags(), *self.user_fields))


@dataclass(frozen=True)
class FrontendSystem:
    """A system of the frontend: its name (gamelist folder) and the hub folder it reads."""

    name: str
    hub: str
    base: str  # the part of the ROM folder below the hub's system folder, `carts/` or empty


def is_plain(name: str) -> bool:
    return bool(_PLAIN.match(name))


def library_systems(roms: Path) -> list[str]:
    """The system folders below the library's `roms/` that can be written as ignore lines."""
    try:
        names = [p.name for p in roms.iterdir() if p.is_dir()]
    except OSError:
        return []
    return sorted(n for n in names if is_plain(n))


def media_ignores(systems: Collection[str], kinds: Sequence[str]) -> tuple[str, ...]:
    """Ignore lines for the hub's media folder (`<system>/<kind>/...`): just these kinds."""
    wanted = [f"!/{s}/{k}" for s in sorted(systems) if is_plain(s) for k in kinds]
    return (*wanted, "*")


def metadata_ignores(systems: Collection[str]) -> tuple[str, ...]:
    """Ignore lines for the hub's ES-DE folder: the gamelists of these systems."""
    wanted = [f"!/gamelists/{s}/gamelist.xml" for s in sorted(systems) if is_plain(s)]
    return (*wanted, "*")


# ------------------------------------------------------------------ reading


_GAMELIST_BLOCK = re.compile(rb"<gameList(?:\s[^>]*)?(?:/>|>.*</gameList\s*>)", re.DOTALL)


def _root(data: str | bytes) -> ET.Element:
    raw = data.encode("utf-8") if isinstance(data, str) else data
    try:
        return ET.fromstring(raw)
    except ET.ParseError as exc:
        # ES-DE writes an `<alternativeEmulator>` element ahead of `<gameList>`, which makes
        # the file two documents. Read the `<gameList>` on its own.
        block = _GAMELIST_BLOCK.search(raw)
        if block is None:
            raise ArtworkError(f"not valid XML ({exc})") from exc
        try:
            return ET.fromstring(block.group(0))
        except ET.ParseError as inner:
            raise ArtworkError(f"not valid XML ({inner})") from inner


def parse_gamelist(data: str | bytes) -> list[Entry]:
    """The entries of a gamelist file; an entry without a path is skipped."""
    root = _root(data)
    if root.tag != "gameList":
        raise ArtworkError(f"the root element is <{root.tag}>, not <gameList>")
    entries: list[Entry] = []
    for node in root:
        if node.tag not in ("game", "folder"):
            continue
        path = (node.findtext("path") or "").strip()
        if not path:
            continue
        fields: dict[str, str] = {}
        for child in node:
            value = (child.text or "").strip()
            if child.tag != "path" and len(child) == 0 and value and child.tag not in fields:
                fields[child.tag] = value
        entries.append(Entry(node.tag, path, fields))
    return entries


def frontend_systems(
    text: str, prefix: str, target: str, renames: Mapping[str, str] | None = None
) -> list[FrontendSystem]:
    """The systems of the frontend's system list that read from `target` (the library).

    `text` is the OS's list; it gets the same rewrite as the override, so `[es_systems.rename]`
    (frontend folder -> hub folder) is honoured and entries outside `prefix` are left out.
    The name is the `<name>` of the entry, which is also the folder the frontend keeps its
    gamelist in, and it is not always the ROM folder (`ppsspp` reads `psp`).
    """
    moved = frontend.rewrite_system_paths(text, prefix, target, renames)
    try:
        root = ET.fromstring(moved)
    except ET.ParseError as exc:
        raise ArtworkError(f"the system list is not valid XML ({exc})") from exc
    systems: list[FrontendSystem] = []
    for node in root.iter("system"):
        name = (node.findtext("name") or "").strip()
        path = (node.findtext("path") or "").strip()
        if not name or not path.startswith(target):
            continue
        parts = [p for p in path[len(target) :].split("/") if p]
        if not parts:
            continue
        base = "/".join(parts[1:]) + "/" if len(parts) > 1 else ""
        systems.append(FrontendSystem(name, parts[0], base))
    return systems


def assign(
    systems: Sequence[FrontendSystem], hubs: Collection[str]
) -> tuple[dict[str, list[FrontendSystem]], list[FrontendSystem]]:
    """The systems to write gamelists for, by hub folder, and those left out.

    The frontend keeps one gamelist per system name. When several hub folders carry the same
    name, the one the hub names alike wins, else the first of the list; the others are
    returned as left out so the caller can say so.
    """
    candidates = [s for s in systems if s.hub in hubs]
    winners: dict[str, FrontendSystem] = {}
    for system in candidates:
        current = winners.get(system.name)
        if current is None or (system.hub == system.name and current.hub != system.name):
            winners[system.name] = system
    chosen: dict[str, list[FrontendSystem]] = {}
    dropped: list[FrontendSystem] = []
    for system in candidates:
        kept = winners[system.name]
        if system.hub == kept.hub and system.base == kept.base:
            if system not in chosen.setdefault(system.hub, []):
                chosen[system.hub].append(system)
        elif system not in dropped:
            dropped.append(system)
    return chosen, dropped


# ------------------------------------------------------------------ converting


def relative(path: str) -> str | None:
    """A gamelist path as a plain relative path below the system folder, or None.

    `./Hacks/X.gba` -> `Hacks/X.gba`. Anything that is absolute or climbs out of the folder
    is refused: the path is joined to a folder on the device.
    """
    p = path.replace("\\", "/")
    if p.startswith("./"):
        p = p[2:]
    parts = p.split("/")
    if not p or "\0" in p or p.startswith("/") or any(s in ("", ".", "..") for s in parts):
        return None
    return p


def build(
    fmt: Format,
    source: Sequence[Entry],
    existing: Sequence[Entry],
    *,
    kinds: Sequence[str],
    base: str,
    rom_exists: Callable[[str], bool],
    media_path: Callable[[str, str], str | None],
    overlay: Mapping[str, Mapping[str, str]] | None = None,
) -> list[Entry]:
    """The frontend's entries for one system.

    `rom_exists(rel)` and `media_path(kind, rel_without_extension)` take paths relative to the
    hub's system folder (the ES-DE gamelist's own); `media_path` returns an absolute path of
    a file that exists, or None. An entry is kept only when its ROM exists. The user fields
    come from `existing` (the file the frontend has been saving to), never from `source`.
    Entries of `existing` that the hub does not know, with user data on a ROM that is still
    there, are kept with just that data. The order is the hub's, then those, by path.

    `overlay` holds, by hub-relative path, user fields merged from the other devices: they
    replace the saved ones (an empty value removes the field). A game it names that has no
    entry gets one when its ROM is here, after the rest.
    """
    extra = overlay or {}

    def merged(rel: str, fields: dict[str, str]) -> dict[str, str]:
        changes = extra.get(rel, {})
        return {k: v for k, v in {**fields, **changes}.items() if v}

    saved = {(e.tag, e.path): e for e in existing}
    built: list[Entry] = []
    seen: set[tuple[str, str]] = set()
    for entry in source:
        rel = relative(entry.path)
        if rel is None or (base and not rel.startswith(base)):
            continue
        emitted = "./" + rel[len(base) :]
        key = (entry.tag, emitted)
        if rel == base or key in seen or not rom_exists(rel):
            continue
        seen.add(key)
        fields = {k: entry.fields[k] for k in fmt.source_fields if entry.fields.get(k)}
        stem = rel if entry.tag == "folder" else posixpath.splitext(rel)[0]
        for tag in fmt.image_tags():
            for kind in kinds:
                found = media_path(kind, stem) if fmt.media_tags.get(kind) == tag else None
                if found:
                    fields[tag] = found
                    break
        old = saved.get(key)
        if old is not None:
            fields.update({k: old.fields[k] for k in fmt.user_fields if old.fields.get(k)})
        built.append(Entry(entry.tag, emitted, merged(rel, fields)))
    for old in sorted(existing, key=lambda e: (e.path, e.tag)):
        key = (old.tag, old.path)
        rel = relative(old.path)
        mine = {k: old.fields[k] for k in fmt.user_fields if old.fields.get(k)}
        if key in seen or rel is None or not mine or not rom_exists(base + rel):
            continue
        seen.add(key)
        built.append(Entry(old.tag, "./" + rel, merged(base + rel, mine)))
    for rel in sorted(extra):
        extra_rel = rel[len(base) :] if rel.startswith(base) else None
        key = ("game", f"./{extra_rel}")
        fields = merged(rel, {})
        if extra_rel and key not in seen and fields and rom_exists(rel):
            seen.add(key)
            built.append(Entry("game", f"./{extra_rel}", fields))
    return built


def _normal(fmt: Format, entries: Sequence[Entry]) -> dict[tuple[str, str], dict[str, str]]:
    known = fmt.known()
    return {
        (e.tag, e.path): {k: v for k, v in e.fields.items() if k in known and v} for e in entries
    }


def same_content(fmt: Format, built: Sequence[Entry], existing_text: str | bytes) -> bool:
    """Whether the file says what `built` says, whatever its layout and extra fields."""
    try:
        existing = parse_gamelist(existing_text)
    except ArtworkError:
        return False
    wanted = _normal(fmt, built)
    # A frontend that saves on exit also lists the games nobody scraped. Those carry
    # nothing of the player's, so they are not a difference.
    seen = {
        key: fields
        for key, fields in _normal(fmt, existing).items()
        if key in wanted or any(f in fields for f in fmt.user_fields)
    }
    return wanted == seen


# ------------------------------------------------------------------ planning


@dataclass(frozen=True)
class Files:
    """The few reads a plan makes, so a check can use its own (and a test fake ones)."""

    read: Callable[[Path], bytes | None]  # None when the file is missing
    is_file: Callable[[Path], bool]
    exists: Callable[[Path], bool]


def _read(path: Path) -> bytes | None:
    try:
        return path.read_bytes()
    except FileNotFoundError:
        return None


REAL_FILES = Files(_read, Path.is_file, Path.exists)


# `[artwork].gamelists` value: the gamelist goes in the ROM folder of each system.
ROMPATH = "{rompath}"


@dataclass(frozen=True)
class Layout:
    """Where things are on the device, all expanded."""

    roms: Path  # <library>/roms
    media: Path
    metadata: Path
    gamelists: Path


@dataclass(frozen=True)
class Planned:
    """One gamelist the converter wants: for the frontend's `system`, from the hub's `hub`."""

    system: str
    hub: str
    path: Path
    data: bytes
    state: str  # `current`, `missing` or `stale`
    games: int


@dataclass(frozen=True)
class GamelistPlan:
    items: tuple[Planned, ...] = ()
    # Systems left out because another one has the same name.
    dropped: tuple[FrontendSystem, ...] = ()
    # Hub systems on the device, known to the frontend, with no scraped gamelist yet.
    waiting: tuple[str, ...] = ()
    # Scraped gamelists that cannot be read, as messages.
    problems: tuple[str, ...] = ()

    def pending(self) -> list[Planned]:
        return [i for i in self.items if i.state != "current"]


def gamelist_path(layout: Layout, system: FrontendSystem) -> Path:
    """Where the frontend keeps the gamelist of `system`."""
    if str(layout.gamelists) == ROMPATH:
        return layout.roms / system.hub / "gamelist.xml"
    return layout.gamelists / system.name / "gamelist.xml"


def gamelist_systems(
    layout: Layout,
    system_list: str,
    *,
    prefix: str,
    renames: Mapping[str, str],
    hubs: Collection[str],
) -> dict[str, list[FrontendSystem]]:
    """The frontend's systems that get a gamelist, by hub folder, as `plan_gamelists` picks them."""
    systems = frontend_systems(system_list, prefix, f"{layout.roms}/", renames)
    return assign(systems, set(hubs))[0]


def plan_gamelists(
    fmt: Format,
    kinds: Sequence[str],
    layout: Layout,
    system_list: str,
    *,
    prefix: str,
    renames: Mapping[str, str],
    hubs: Collection[str],
    files: Files = REAL_FILES,
    overlay: Mapping[str, Mapping[str, str]] | None = None,
) -> GamelistPlan:
    """What the gamelists of the device should be, and whether each already is.

    `hubs` are the hub system folders on the device; `overlay` is `build`'s, by hub-relative
    path prefixed with `<hub>/`. Raises `ArtworkError` when the system list is unreadable.
    """
    systems = frontend_systems(system_list, prefix, f"{layout.roms}/", renames)
    chosen, dropped = assign(systems, set(hubs))
    items: list[Planned] = []
    waiting: list[str] = []
    problems: list[str] = []
    for hub in sorted(chosen):
        scraped = layout.metadata / "gamelists" / hub / "gamelist.xml"
        data = files.read(scraped)
        if data is None:
            waiting.append(hub)
            continue
        try:
            source = parse_gamelist(data)
        except ArtworkError as exc:
            problems.append(f"{scraped}: {exc}")
            continue

        def rom_exists(rel: str, hub: str = hub) -> bool:
            return files.exists(layout.roms / hub / rel)

        def media_path(kind: str, stem: str, hub: str = hub) -> str | None:
            path = layout.media / hub / kind / f"{stem}.png"
            return str(path) if files.is_file(path) else None

        folder = f"{hub}/"
        mine = {k[len(folder) :]: v for k, v in (overlay or {}).items() if k.startswith(folder)}
        for system in chosen[hub]:
            path = gamelist_path(layout, system)
            before = files.read(path)
            try:
                existing = parse_gamelist(before) if before is not None else []
            except ArtworkError:
                existing = []
            built = build(
                fmt,
                source,
                existing,
                kinds=kinds,
                base=system.base,
                rom_exists=rom_exists,
                media_path=media_path,
                overlay=mine,
            )
            wanted = fmt.render(built).encode("utf-8")
            if before is None:
                state = "missing"
            elif before == wanted or same_content(fmt, built, before):
                state = "current"
            else:
                state = "stale"
            items.append(Planned(system.name, hub, path, wanted, state, len(built)))
    return GamelistPlan(tuple(items), tuple(dropped), tuple(waiting), tuple(problems))


# ------------------------------------------------------------------ the timer


def _unit_arg(arg: str) -> str:
    if re.fullmatch(r"[A-Za-z0-9_@+=:,./-]+", arg):
        return arg
    quoted = arg.replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%")
    return f'"{quoted}"'


def _units(
    command: list[str], config_path: Path | None, interval: str, what: str, boot: str = "5min"
) -> tuple[str, str]:
    if config_path is not None:
        command = [*command, "--config", str(config_path)]
    service = (
        "[Unit]\n"
        f"Description=warpzone: {what}\n"
        "\n"
        "[Service]\n"
        "Type=oneshot\n"
        f"ExecStart={' '.join(_unit_arg(a) for a in command)}\n"
        "Nice=10\n"
    )
    timer = (
        "[Unit]\n"
        f"Description=warpzone: {what} regularly\n"
        "\n"
        "[Timer]\n"
        f"OnBootSec={boot}\n"
        f"OnUnitActiveSec={interval}\n"
        "\n"
        "[Install]\n"
        "WantedBy=timers.target\n"
    )
    return service, timer


def render_units(
    python: str, name: str, config_path: Path | None, interval: str
) -> tuple[str, str]:
    """The text of the `systemd --user` service and timer that run `frontend refresh`."""
    command = [python, "-m", "warpzone", "frontend", "refresh", name]
    return _units(command, config_path, interval, "refresh the frontend gamelists")


def render_play_state_units(
    python: str, name: str, config_path: Path | None, interval: str
) -> tuple[str, str]:
    """The text of the `systemd --user` service and timer that run `play-state sync`."""
    command = [python, "-m", "warpzone", "play-state", "sync", name, "--quiet"]
    return _units(command, config_path, interval, "merge favourites and play statistics")


def render_prune_units(python: str, name: str, config_path: Path | None) -> tuple[str, str]:
    """The text of the `systemd --user` service and timer that run `prune --apply --unattended`."""
    command = [python, "-m", "warpzone", "prune", name, "--apply", "--unattended"]
    return _units(
        command,
        config_path,
        PRUNE_INTERVAL,
        "delete the files this device no longer whitelists",
        boot=PRUNE_BOOT,
    )


def render_settings_units(python: str, name: str, config_path: Path | None) -> tuple[str, str]:
    """The text of the `systemd --user` service and timer that run `settings apply` for ES-DE."""
    command = [
        python, "-m", "warpzone", "settings", "apply", "--device", name,
        "--emulator", SETTINGS_EMULATOR, "--when-idle", "--quiet",
    ]  # fmt: skip
    return _units(
        command,
        config_path,
        SETTINGS_INTERVAL,
        "apply the shared ES-DE settings while ES-DE is closed",
        boot=SETTINGS_BOOT,
    )


UNIT_NAME = "warpzone-artwork"
PLAY_STATE_UNIT = "warpzone-play-state"
PRUNE_UNIT = "warpzone-prune"
SETTINGS_UNIT = "warpzone-settings"
SETTINGS_EMULATOR = "esde"
SETTINGS_BOOT = "5min"
SETTINGS_INTERVAL = "15min"
PRUNE_BOOT = "15min"
PRUNE_INTERVAL = "1h"


# ------------------------------------------------------------------ formats


def _clean(text: str) -> str:
    return _ILLEGAL.sub("", text)


def _escape(text: str) -> str:
    return _clean(text).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _render_xml(order: Sequence[str], entries: Sequence[Entry]) -> str:
    rank = {name: i for i, name in enumerate(order)}
    lines = ['<?xml version="1.0"?>', "<gameList>"]
    for entry in entries:
        lines += [f"\t<{entry.tag}>", f"\t\t<path>{_escape(entry.path)}</path>"]
        for key in sorted(entry.fields, key=lambda k: (rank.get(k, len(rank)), k)):
            lines.append(f"\t\t<{key}>{_escape(entry.fields[key])}</{key}>")
        lines.append(f"\t</{entry.tag}>")
    lines.append("</gameList>")
    return "\n".join(lines) + "\n"


_ES_ORDER = (
    "name",
    "desc",
    "image",
    "thumbnail",
    "marquee",
    "rating",
    "releasedate",
    "developer",
    "publisher",
    "genre",
    "players",
    "playcount",
    "lastplayed",
    "favorite",
    "hidden",
    "kidgame",
)


def _render_emulationstation(entries: Sequence[Entry]) -> str:
    return _render_xml(_ES_ORDER, entries)


# EmulationStation (RetroPie lineage, the one dArkOS ships). The source fields are the ones
# ES-DE and this frontend both have, so ES-DE-only tags (`altemulator`, `label`, `playtime`)
# are not copied.
EMULATIONSTATION = Format(
    name="emulationstation",
    source_fields=(
        "name",
        "desc",
        "rating",
        "releasedate",
        "developer",
        "publisher",
        "genre",
        "players",
    ),
    user_fields=("favorite", "hidden", "kidgame", "playcount", "lastplayed"),
    media_tags={
        "covers": "image",
        "3dboxes": "image",
        "miximages": "image",
        "screenshots": "image",
        "physicalmedia": "thumbnail",
        "marquees": "marquee",
    },
    render=_render_emulationstation,
    order=_ES_ORDER,
    state_tags=STATE_TAGS,  # RetroPie's EmulationStation has no playtime
)

FORMATS: dict[str, Format] = {EMULATIONSTATION.name: EMULATIONSTATION}
