"""Scraped artwork and metadata for a frontend that is not ES-DE. Pure: no I/O but listing.

The hub keeps ES-DE's gamelists and downloaded media. A device whose profile has `[artwork]`
receives a slice of them (`media_ignores`, `metadata_ignores`) and a converter, picked by the
profile's `format`, turns each ES-DE gamelist into the frontend's own (`build`, then the
format's `render`). A frontend that reads another file shape is one more `Format` in
`FORMATS`; the rest, including the profile and `provision`, does not change.
See docs/design.md, "Frontend artwork".
"""

from __future__ import annotations

import posixpath
import re
from collections.abc import Callable, Collection, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from xml.etree import ElementTree as ET

from warpzone import frontend

# A system folder that can be written as an ignore pattern as it is.
_PLAIN = re.compile(r"^[A-Za-z0-9_-][A-Za-z0-9._-]*$")
# Characters XML 1.0 cannot hold.
_ILLEGAL = re.compile("[\x00-\x08\x0b\x0c\x0e-\x1f\ud800-\udfff￾￿]")


class ArtworkError(Exception):
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


def parse_gamelist(data: str | bytes) -> list[Entry]:
    """The entries of a gamelist file; an entry without a path is skipped."""
    try:
        root = ET.fromstring(data)
    except ET.ParseError as exc:
        raise ArtworkError(f"not valid XML ({exc})") from exc
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
) -> list[Entry]:
    """The frontend's entries for one system.

    `rom_exists(rel)` and `media_path(kind, rel_without_extension)` take paths relative to the
    hub's system folder (the ES-DE gamelist's own); `media_path` returns an absolute path of
    a file that exists, or None. An entry is kept only when its ROM exists. The user fields
    come from `existing` (the file the frontend has been saving to), never from `source`.
    Entries of `existing` that the hub does not know, with user data on a ROM that is still
    there, are kept with just that data. The order is the hub's, then those, by path.
    """
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
        built.append(Entry(entry.tag, emitted, fields))
    for old in sorted(existing, key=lambda e: (e.path, e.tag)):
        key = (old.tag, old.path)
        rel = relative(old.path)
        mine = {k: old.fields[k] for k in fmt.user_fields if old.fields.get(k)}
        if key in seen or rel is None or not mine or not rom_exists(base + rel):
            continue
        seen.add(key)
        built.append(Entry(old.tag, "./" + rel, mine))
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
    return _normal(fmt, built) == _normal(fmt, existing)


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
)

FORMATS: dict[str, Format] = {EMULATIONSTATION.name: EMULATIONSTATION}
