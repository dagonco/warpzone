"""The catalog index: games and their parts, derived from a listing of the library (pure).

A game is what the library UI turns on or off; a part is one path below `library/` that the
game needs on a console. This module decides everything that needs no disk: which entries
of a system folder make up which game (`derive`), the roots a whole system is whitelisted
by (`system_roots`), the title key that groups a title across systems (`title_key`), the
search over keys, the JSON encoding of a system file and the merge that keeps what only
`inbox` knows. `catalog_store.py` walks the disk and writes the files.
See development/design/library-ui.md, "The catalog index".
"""

from __future__ import annotations

import hashlib
import json
import posixpath
import re
import unicodedata
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from typing import Any

from warpzone import inbox, whitelist
from warpzone.inbox_switch import base_title_id

FORMAT = 1
ID_LENGTH = 16
FILE, DIR = "file", "dir"
UNLISTABLE = "unlistable"
# Systems whose games live one level down, in `roms/<system>/roms/`.
# The whitelist model owns the rule of which paths a whole system is (`whitelist.system_roots`).
INNER_ROMS = whitelist.SUBROOT_SYSTEMS
SWITCH_EXTRAS = whitelist.system_roots("switch")[1:]
# Files whose text derive reads (a launcher, a shortcut, a cue sheet).
TEXT_SUFFIXES = (".desktop", ".psvita", ".cue", ".gdi", ".m3u")
SIDECAR_SUFFIXES = (".m3u", ".cue", ".gdi")  # claim order: playlists first
JUNK_NAMES = frozenset({"@eaDir", "Thumbs.db", "systeminfo.txt", "metadata.txt"})


@dataclass(frozen=True)
class Node:
    """One file or folder of the listing: `path` is relative to `library/`, with `/`."""

    path: str
    kind: str  # FILE or DIR
    size: int = 0
    mtime: int = 0  # whole seconds


@dataclass(frozen=True)
class Listing:
    """What derive reads: every node below the system's roots, and the text of the small
    files whose content matters (`TEXT_SUFFIXES`)."""

    nodes: Sequence[Node]
    texts: Mapping[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class Part:
    path: str
    type: str  # FILE or DIR
    size: int
    mtime: int
    files: int | None = None  # DIR only: the count of files below
    sha1: str | None = None  # FILE only: filled in by the store, never by derive


@dataclass(frozen=True)
class Game:
    id: str
    title: str
    key: str
    primary: str
    parts: tuple[Part, ...]
    gamelist: str
    size: int
    added: str | None = None
    dat: str | None = None
    ra: Mapping[str, Any] | None = None
    flags: tuple[str, ...] = ()


def game_id(primary: str) -> str:
    return hashlib.sha256(primary.encode("utf-8")).hexdigest()[:ID_LENGTH]


# ------------------------------------------------------------------ roots


def system_roots(system: str) -> list[str]:
    """The paths a whole system is whitelisted by, relative to `library/`."""
    return list(whitelist.system_roots(system))


# ------------------------------------------------------------------ title key

_GROUPS = re.compile(r"\([^()]*\)|\[[^\[\]]*\]")
_SUBTITLE = re.compile(r"\s[-\u2013\u2014]\s|:(?:\s|$)")
_TRAILING_THE = re.compile(r"(?:,\s*|\s+)the\s*$")
_APOSTROPHES = re.compile("['\u2019\u2018\u02bc`]")
_NAME_EXT = re.compile(r"\.[A-Za-z0-9]{1,8}$")


def _strip_groups(text: str) -> str:
    while True:
        out = _GROUPS.sub(" ", text)
        if out == text:
            return out
        text = out


def _normalize(text: str, *, strip_groups: bool) -> str:
    if strip_groups:
        text = _strip_groups(text)
    text = unicodedata.normalize("NFKD", text)
    text = "".join(c for c in text if not unicodedata.category(c).startswith("M")).casefold()
    # The No-Intro form `Legend of Zelda, The - A Link`: a `the` that ends the title or
    # the part before the subtitle goes. It is dealt with before punctuation disappears.
    m = _SUBTITLE.search(text)
    main, rest = (text[: m.start()], text[m.start() :]) if m else (text, "")
    text = _TRAILING_THE.sub("", main.rstrip()) + rest
    text = _APOSTROPHES.sub("", text).replace("&", " and ")
    words = "".join(c if c.isalnum() else " " for c in text).split()
    if len(words) > 1 and words[0] == "the":
        words = words[1:]
    return " ".join(words)


def title_key(title: str) -> str:
    """The key that groups the versions of a game across systems (see the design)."""
    return _normalize(title, strip_groups=True) or _normalize(title, strip_groups=False)


# ------------------------------------------------------------------ listing helpers


def is_junk(name: str) -> bool:
    return name.startswith(".") or name in JUNK_NAMES


def _suffix(name: str) -> str:
    return posixpath.splitext(name)[1].lower()


def _stem(name: str) -> str:
    """The display title of a file name: its extension (up to eight characters, no spaces)
    is dropped, so `Mr. Driller` keeps its dot."""
    return _NAME_EXT.sub("", name)


def unlistable(path: str) -> bool:
    """Whether a path cannot be written as a whitelist line (see "Escaping")."""
    if any(c in path for c in "\n\r\0"):
        return True
    return any(part != part.strip() for part in path.split("/"))


class _Tree:
    """Nodes by parent, and the totals of every folder (size, files, newest mtime)."""

    def __init__(self, nodes: Iterable[Node]) -> None:
        self.nodes: dict[str, Node] = {}
        self.children: dict[str, list[Node]] = {}
        self._size: dict[str, int] = {}
        self._files: dict[str, int] = {}
        self._mtime: dict[str, int] = {}
        for node in nodes:
            self.nodes[node.path] = node
            self.children.setdefault(posixpath.dirname(node.path), []).append(node)
        for kids in self.children.values():
            kids.sort(key=lambda n: n.path)
        for node in self.nodes.values():
            if node.kind == FILE:
                parent = posixpath.dirname(node.path)
                while parent:
                    self._size[parent] = self._size.get(parent, 0) + node.size
                    self._files[parent] = self._files.get(parent, 0) + 1
                    self._mtime[parent] = max(self._mtime.get(parent, 0), node.mtime)
                    parent = posixpath.dirname(parent)

    def kids(self, path: str, kind: str | None = None) -> list[Node]:
        return [
            n
            for n in self.children.get(path, [])
            if not is_junk(posixpath.basename(n.path)) and (kind is None or n.kind == kind)
        ]

    def part(self, path: str) -> Part:
        node = self.nodes[path]
        if node.kind == FILE:
            return Part(path, FILE, node.size, node.mtime)
        return Part(
            path,
            DIR,
            self._size.get(path, 0),
            self._mtime.get(path, node.mtime),
            self._files.get(path, 0),
        )

    def files_under(self, path: str) -> dict[str, str]:
        """Lower-case path -> path of every file below a folder (what a cue can name)."""
        prefix = path + "/"
        return {
            p.lower(): p for p, n in self.nodes.items() if n.kind == FILE and p.startswith(prefix)
        }


def _build(
    tree: _Tree,
    title: str,
    primary: str,
    parts: Sequence[str],
    gamelist: str,
) -> Game:
    built = tuple(tree.part(p) for p in dict.fromkeys(parts))
    flags = (UNLISTABLE,) if any(unlistable(p.path) for p in built) else ()
    return Game(
        id=game_id(primary),
        title=title,
        key=title_key(title),
        primary=primary,
        parts=built,
        gamelist=gamelist,
        size=sum(p.size for p in built),
        flags=flags,
    )


def _gamelist_path(system: str, path: str) -> str:
    base = f"roms/{system}/"
    return "./" + (path[len(base) :] if path.startswith(base) else path)


def _m3u_refs(text: str) -> list[str]:
    return [line.strip() for line in text.splitlines() if line.strip() and line[0] != "#"]


def _refs(path: str, text: str) -> list[str]:
    suffix = _suffix(path)
    if suffix == ".cue":
        return inbox.cue_refs(text)
    if suffix == ".gdi":
        return inbox.gdi_refs(text)
    return _m3u_refs(text)


# ------------------------------------------------------------------ derivation


def _default_games(tree: _Tree, system: str, listing: Listing, roots: Sequence[str]) -> list[Game]:
    games: list[Game] = []
    claimed: set[str] = set()

    def single(node: Node, title: str) -> None:
        games.append(_build(tree, title, node.path, [node.path], _gamelist_path(system, node.path)))

    def visit(folder: str) -> None:
        files = [
            n
            for n in tree.kids(folder, FILE)
            if not n.path.endswith(".part") and n.path not in claimed
        ]
        sidecars = sorted(
            (n for n in files if _suffix(n.path) in SIDECAR_SUFFIXES),
            key=lambda n: (_suffix(n.path) != ".m3u", n.path),
        )
        available = tree.files_under(folder) if sidecars else {}
        for sidecar in sidecars:
            if sidecar.path in claimed:
                continue
            parts = [sidecar.path]
            pending = [sidecar.path]
            while pending:
                current = pending.pop()
                for ref in _refs(current, listing.texts.get(current, "")):
                    found = inbox.resolve_refs(current, [ref], available)
                    if found is None or found[0] in parts or found[0] in claimed:
                        continue
                    parts.append(found[0])
                    if _suffix(found[0]) in SIDECAR_SUFFIXES:
                        pending.append(found[0])
            claimed.update(parts)
            name = posixpath.basename(sidecar.path)
            games.append(
                _build(tree, _stem(name), sidecar.path, parts, _gamelist_path(system, sidecar.path))
            )
        for node in files:
            if node.path not in claimed:
                single(node, _stem(posixpath.basename(node.path)))
        for node in tree.kids(folder, DIR):
            name = posixpath.basename(node.path)
            lower = name.lower()
            if lower.endswith(".m3u") or (system == "ps3" and lower.endswith(".ps3")):
                single(node, name[: name.rfind(".")])
            else:
                visit(node.path)

    for root in roots:
        visit(root)
    return games


_EXEC_GAME = re.compile(r'/games/([^/"\']+)/eboot\.bin["\']?\s*$')


def _ps4_games(tree: _Tree, system: str, listing: Listing) -> list[Game]:
    shortcuts: dict[str, list[str]] = {}
    for node in tree.kids("roms/ps4/shortcuts", FILE):
        if _suffix(node.path) != ".desktop":
            continue
        for line in listing.texts.get(node.path, "").splitlines():
            if line.startswith("Exec="):
                m = _EXEC_GAME.search(line.strip())
                if m:
                    shortcuts.setdefault(m.group(1), []).append(node.path)
                break
    games = []
    for node in tree.kids("roms/ps4/games", DIR):
        cusa = posixpath.basename(node.path)
        linked = shortcuts.get(cusa, [])
        title = _stem(posixpath.basename(linked[0])) if linked else cusa
        gamelist = _gamelist_path(system, linked[0] if linked else node.path)
        games.append(_build(tree, title, node.path, [node.path, *linked], gamelist))
    for node in tree.kids("roms/ps4", FILE):  # a .pkg filed without orbis-pkg-util
        if _suffix(node.path) == ".pkg":
            title = _stem(posixpath.basename(node.path))
            games.append(
                _build(tree, title, node.path, [node.path], _gamelist_path(system, node.path))
            )
    return games


def _vita_games(tree: _Tree, system: str, listing: Listing) -> list[Game]:
    launchers: dict[str, list[str]] = {}
    for node in tree.kids("roms/psvita", FILE):
        if _suffix(node.path) == ".psvita":
            launchers.setdefault(listing.texts.get(node.path, "").strip(), []).append(node.path)
    games = []
    for node in tree.kids("roms/psvita/app", DIR):
        title_id = posixpath.basename(node.path)
        licence = f"roms/psvita/license/{title_id}"
        parts = [node.path]
        if licence in tree.nodes and tree.nodes[licence].kind == DIR:
            parts.append(licence)
        linked = launchers.get(title_id, [])
        parts.extend(linked)
        title = _stem(posixpath.basename(linked[0])) if linked else title_id
        gamelist = _gamelist_path(system, linked[0] if linked else node.path)
        games.append(_build(tree, title, node.path, parts, gamelist))
    return games


_SWITCH_ID = re.compile(r"\[([0-9A-Fa-f]{16})\]")


def _switch_extras(tree: _Tree) -> list[tuple[int, str, set[str]]]:
    """(order, folder, base TitleIDs) of every update and DLC folder."""
    found: list[tuple[int, str, set[str]]] = []
    for order, root in enumerate(SWITCH_EXTRAS):
        bases: dict[str, set[str]] = {n.path: set() for n in tree.kids(root, DIR)}
        prefix = root + "/"
        for path in tree.nodes:
            if not path.startswith(prefix):
                continue
            folder = prefix + path[len(prefix) :].split("/", 1)[0]
            if folder in bases:
                for m in _SWITCH_ID.finditer(posixpath.basename(path)):
                    bases[folder].add(base_title_id(m.group(1).upper()))
        found.extend((order, folder, ids) for folder, ids in sorted(bases.items()))
    return found


def _switch_games(tree: _Tree, system: str, listing: Listing) -> list[Game]:
    extras = _switch_extras(tree)
    games = []
    for node in tree.kids("roms/switch", FILE):
        if node.path.endswith(".part"):
            continue
        ids = [m.group(1).upper() for m in _SWITCH_ID.finditer(posixpath.basename(node.path))]
        base = next((i for i in ids if i.endswith("000")), None)
        parts = [node.path]
        if base:
            parts.extend(folder for _, folder, bases in extras if base in bases)
        title = _stem(posixpath.basename(node.path))
        games.append(_build(tree, title, node.path, parts, _gamelist_path(system, node.path)))
    return games


def unclaimed(system: str, listing: Listing) -> list[str]:
    """The update and DLC folders that no base game of the library claims (Switch only)."""
    if system != "switch":
        return []
    tree = _Tree(listing.nodes)
    claimed = {p.path for g in _switch_games(tree, system, listing) for p in g.parts}
    return sorted(folder for _, folder, _ in _switch_extras(tree) if folder not in claimed)


def derive(system: str, listing: Listing) -> list[Game]:
    """The games of one system, sorted by primary path. Pure: `listing` is all it reads."""
    tree = _Tree(listing.nodes)
    if system == "ps4":
        games = _ps4_games(tree, system, listing)
    elif system == "psvita":
        games = _vita_games(tree, system, listing)
    elif system == "switch":
        games = _switch_games(tree, system, listing)
    else:
        games = _default_games(tree, system, listing, system_roots(system))
    unique = {g.id: g for g in games}
    return sorted(unique.values(), key=lambda g: g.primary)


# ------------------------------------------------------------------ merge


def merge(
    previous: Mapping[str, Game],
    derived: Sequence[Game],
    *,
    today: str,
    dat: Mapping[str, str] | None = None,
) -> list[Game]:
    """Derived games plus what only earlier runs and `inbox` know.

    A file part keeps its stored sha1 while its size and mtime are unchanged. `added`, `dat`
    and `ra` are kept for games that stay; a new game is `added` today. `dat` maps the
    primary path of a game `inbox` just filed to its DAT name. Games no longer derived are
    dropped.
    """
    out = []
    for game in derived:
        old = previous.get(game.id)
        known = {p.path: p for p in old.parts} if old else {}
        parts = []
        for part in game.parts:
            was = known.get(part.path)
            if (
                part.type == FILE
                and part.sha1 is None
                and was is not None
                and was.type == FILE
                and (was.size, was.mtime) == (part.size, part.mtime)
            ):
                part = replace(part, sha1=was.sha1)
            parts.append(part)
        out.append(
            replace(
                game,
                parts=tuple(parts),
                added=(old.added if old and old.added else today),
                dat=(dat or {}).get(game.primary) or (old.dat if old else None),
                ra=old.ra if old else None,
            )
        )
    return out


def pending_hashes(games: Iterable[Game]) -> list[Part]:
    """The file parts that have no sha1 yet."""
    return [p for g in games for p in g.parts if p.type == FILE and p.sha1 is None]


def fill_hashes(games: Sequence[Game], hashes: Mapping[str, str]) -> list[Game]:
    """The games with the sha1 of each file part in `hashes` (by path) filled in."""
    out = []
    for game in games:
        parts = tuple(
            replace(p, sha1=hashes[p.path])
            if p.type == FILE and p.sha1 is None and p.path in hashes
            else p
            for p in game.parts
        )
        out.append(replace(game, parts=parts))
    return out


@dataclass(frozen=True)
class Diff:
    added: tuple[str, ...]
    removed: tuple[str, ...]
    changed: tuple[str, ...]

    def __bool__(self) -> bool:
        return bool(self.added or self.removed or self.changed)


def diff(previous: Mapping[str, Game], games: Sequence[Game]) -> Diff:
    """Game ids that appeared, disappeared or differ."""
    now = {g.id: g for g in games}
    return Diff(
        added=tuple(sorted(set(now) - set(previous))),
        removed=tuple(sorted(set(previous) - set(now))),
        changed=tuple(sorted(i for i in set(now) & set(previous) if now[i] != previous[i])),
    )


# ------------------------------------------------------------------ JSON


def _part_json(part: Part) -> dict[str, Any]:
    out: dict[str, Any] = {
        "path": part.path,
        "type": part.type,
        "size": part.size,
        "mtime": part.mtime,
    }
    if part.type == DIR:
        out["files"] = part.files or 0
    else:
        out["sha1"] = part.sha1
    return out


def _game_json(game: Game) -> dict[str, Any]:
    return {
        "title": game.title,
        "key": game.key,
        "primary": game.primary,
        "parts": [_part_json(p) for p in game.parts],
        "gamelist": game.gamelist,
        "size": game.size,
        "added": game.added,
        "dat": game.dat,
        "ra": dict(game.ra) if game.ra is not None else None,
        "flags": list(game.flags),
    }


def encode(system: str, games: Iterable[Game], updated: str) -> bytes:
    """The bytes of a system file: UTF-8 JSON with sorted keys, so the same catalog always
    gives the same bytes."""
    doc = {
        "format": FORMAT,
        "system": system,
        "updated": updated,
        "games": {g.id: _game_json(g) for g in games},
    }
    return (json.dumps(doc, sort_keys=True, indent=1, ensure_ascii=False) + "\n").encode("utf-8")


@dataclass(frozen=True)
class SystemDoc:
    system: str
    updated: str
    games: dict[str, Game]


def _int(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _part_from_json(raw: object) -> Part | None:
    if not isinstance(raw, dict):
        return None
    path, kind = raw.get("path"), raw.get("type")
    size, mtime = _int(raw.get("size")), _int(raw.get("mtime"))
    if not isinstance(path, str) or kind not in (FILE, DIR) or size is None or mtime is None:
        return None
    sha1 = raw.get("sha1")
    return Part(
        path,
        kind,
        size,
        mtime,
        files=_int(raw.get("files")) if kind == DIR else None,
        sha1=sha1 if kind == FILE and isinstance(sha1, str) else None,
    )


def _game_from_json(ident: str, raw: object) -> Game | None:
    if not isinstance(raw, dict):
        return None
    primary, title, gamelist = raw.get("primary"), raw.get("title"), raw.get("gamelist")
    raw_parts = raw.get("parts")
    if not (isinstance(primary, str) and isinstance(title, str) and isinstance(raw_parts, list)):
        return None
    parts = [_part_from_json(p) for p in raw_parts]
    if any(p is None for p in parts) or game_id(primary) != ident:
        return None
    built = tuple(p for p in parts if p is not None)
    added, dat, ra, flags = raw.get("added"), raw.get("dat"), raw.get("ra"), raw.get("flags")
    return Game(
        id=ident,
        title=title,
        key=title_key(title),
        primary=primary,
        parts=built,
        gamelist=gamelist if isinstance(gamelist, str) else "./" + posixpath.basename(primary),
        size=sum(p.size for p in built),
        added=added if isinstance(added, str) else None,
        dat=dat if isinstance(dat, str) else None,
        ra=ra if isinstance(ra, dict) else None,
        flags=tuple(f for f in flags if isinstance(f, str)) if isinstance(flags, list) else (),
    )


def decode(data: bytes) -> SystemDoc | None:
    """The system file in `data`, or None when it is not one of this format (a game that
    does not parse is left out; the next run derives it again)."""
    try:
        doc = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return None
    if not isinstance(doc, dict) or doc.get("format") != FORMAT:
        return None
    system, updated, raw_games = doc.get("system"), doc.get("updated"), doc.get("games")
    if not isinstance(system, str) or not isinstance(raw_games, dict):
        return None
    games: dict[str, Game] = {}
    for ident, raw in raw_games.items():
        game = _game_from_json(ident, raw)
        if game is not None:
            games[ident] = game
    return SystemDoc(system, updated if isinstance(updated, str) else "", games)


# ------------------------------------------------------------------ search


@dataclass(frozen=True)
class Group:
    key: str
    title: str
    games: tuple[tuple[str, Game], ...]  # (system, game)


def search(query: str, games: Iterable[tuple[str, Game]], *, limit: int = 100) -> list[Group]:
    """Games whose title key has, for each word of the query, a word that starts with it,
    grouped by key. Groups are ranked by key equal to the query, then key starting with it,
    then the rest, and then by key."""
    wanted = _normalize(query, strip_groups=False)
    words = wanted.split()
    if not words:
        return []
    groups: dict[str, list[tuple[str, Game]]] = {}
    for system, game in games:
        have = game.key.split()
        if all(any(h.startswith(w) for h in have) for w in words):
            groups.setdefault(game.key, []).append((system, game))

    def rank(key: str) -> tuple[int, str]:
        return (0 if key == wanted else 1 if key.startswith(wanted) else 2, key)

    out = []
    for key in sorted(groups, key=rank)[:limit]:
        members = tuple(sorted(groups[key], key=lambda sg: (sg[0], sg[1].title, sg[1].id)))
        out.append(Group(key, members[0][1].title, members))
    return out
