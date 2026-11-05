"""Pure edits for Cemu: its MLC path and game paths in `settings.xml`.

Cemu (Wii U) keeps installs and saves below its MLC folder, `<mlc_path>` in `settings.xml`;
an empty value means its user data folder (`~/.local/share/Cemu/mlc01` on Linux). The games it
lists come from `<GamePaths>`, a list of `<Entry>` elements. Both are children of the root
`<content>`. A profile with `[cemu]` has `provision device` point the MLC path at the folder
whose `usr/save` it syncs and add the library's Wii U folder to the game paths, and `doctor`
check them. Only those two elements are edited, as text, so every other byte stays as it is;
the result is parsed back and must differ from the original only there. No I/O here. See
development/design/profiles.md, "Cemu paths".
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from typing import Any
from xml.sax.saxutils import escape

from warpzone.errors import WarpzoneError

ROOT, MLC, GAME_PATHS, ENTRY = "content", "mlc_path", "GamePaths", "Entry"

_MLC_RE = re.compile(r"<mlc_path\s*/>|<mlc_path\s*>[^<]*</mlc_path\s*>")
_GAMES_RE = re.compile(r"<GamePaths\s*/>|<GamePaths\s*>(.*?)</GamePaths\s*>", re.DOTALL)
_ENTRY_RE = re.compile(r"<Entry\s*/>|<Entry\b[^>]*>.*?</Entry\s*>", re.DOTALL)
_ROOT_OPEN = re.compile(r"<content\b[^>]*(?<!/)>")
_ROOT_CLOSE = re.compile(r"</content\s*>")


class CemuConfigError(WarpzoneError):
    """The file is not one this module can read or edit safely."""


@dataclass(frozen=True)
class Paths:
    mlc: str  # "" when unset: Cemu then uses its user data folder
    games: tuple[str, ...]


def _norm(path: str) -> str:
    return path.rstrip("/") or "/"


def _parse(text: str) -> ET.Element:
    try:
        root = ET.fromstring(text.encode("utf-8"))
    except ET.ParseError as exc:
        raise CemuConfigError(f"not well-formed XML ({exc})") from exc
    if root.tag != ROOT:
        raise CemuConfigError(f"its root element is <{root.tag}>, not <{ROOT}>")
    for tag in (MLC, GAME_PATHS):
        if len(root.findall(tag)) > 1:
            raise CemuConfigError(f"it has more than one <{tag}>")
    return root


def _paths(root: ET.Element) -> Paths:
    mlc = root.find(MLC)
    block = root.find(GAME_PATHS)
    games = () if block is None else tuple(e.text or "" for e in block.findall(ENTRY))
    return Paths("" if mlc is None else mlc.text or "", games)


def read(text: str) -> Paths:
    """The MLC path and the game paths of a `settings.xml`."""
    return _paths(_parse(text))


def in_use(paths: Paths, default: str) -> str:
    """The MLC folder Cemu uses: its `mlc_path`, or `default` when that is empty."""
    return paths.mlc or default


# The folders of `usr/save` that hold the saves of games (00050000) and demos (00050002). Cemu
# itself creates others on every start (the Mii Maker's 00050010/1004a*, system/act), so their
# presence says nothing about a played game.
TITLE_SAVES = ("00050000", "00050002")


def is_title_save(rel: str) -> bool:
    """Whether a file at `rel`, relative to an MLC's `usr/save`, is a game's or a demo's save."""
    top, sep, rest = rel.partition("/")
    return top in TITLE_SAVES and bool(sep and rest)


def missing(text: str, mlc: str, game_path: str) -> tuple[str, ...]:
    """Which of `mlc_path` and `GamePaths` do not hold the wanted value yet."""
    paths = read(text)
    gaps = []
    if _norm(paths.mlc) != _norm(mlc):
        gaps.append(MLC)
    if _norm(game_path) not in {_norm(g) for g in paths.games}:
        gaps.append(GAME_PATHS)
    return tuple(gaps)


def _eol(text: str) -> str:
    return "\r\n" if "\r\n" in text else "\n"


def _indent_before(text: str, pos: int) -> str | None:
    """The blanks between the start of the line and `pos`, or None if other text is there."""
    start = text.rfind("\n", 0, pos) + 1
    lead = text[start:pos]
    return lead if lead.strip(" \t") == "" else None


def _child_indent(text: str) -> str:
    """The indentation of the root's first child, four spaces if there is none on its own line."""
    opening = _ROOT_OPEN.search(text)
    if opening is not None:
        match = re.compile(r"\r?\n([ \t]*)<").match(text, opening.end())
        if match is not None:
            return match.group(1)
    return "    "


def _single(pattern: re.Pattern[str], text: str, tag: str) -> re.Match[str] | None:
    found = list(pattern.finditer(text))
    if len(found) > 1:
        raise CemuConfigError(f"<{tag}> appears more than once in the text, so it was not edited")
    return found[0] if found else None


def _insert_child(text: str, element: str) -> str:
    """`text` with `element` as the last child of the root, on its own line."""
    closes = list(_ROOT_CLOSE.finditer(text))
    if len(closes) != 1:
        raise CemuConfigError(f"</{ROOT}> is not found once, so the file was not edited")
    pos = closes[0].start()
    eol, indent = _eol(text), _child_indent(text)
    lead = _indent_before(text, pos)
    if lead is not None:
        start = pos - len(lead)
        return f"{text[:start]}{indent}{element}{eol}{text[start:]}"
    return f"{text[:pos]}{eol}{indent}{element}{eol}{text[pos:]}"


def _set_mlc(text: str, value: str) -> str:
    new = f"<{MLC}>{escape(value)}</{MLC}>"
    match = _single(_MLC_RE, text, MLC)
    if match is None:
        return _insert_child(text, new)
    return text[: match.start()] + new + text[match.end() :]


def _add_game_path(text: str, value: str) -> str:
    entry = f"<{ENTRY}>{escape(value)}</{ENTRY}>"
    eol = _eol(text)
    match = _single(_GAMES_RE, text, GAME_PATHS)
    if match is None:
        indent = _child_indent(text)
        step = "\t" if indent.startswith("\t") else "    "
        block = f"<{GAME_PATHS}>{eol}{indent}{step}{entry}{eol}{indent}</{GAME_PATHS}>"
        return _insert_child(text, block)
    body = match.group(1)
    entries = [] if body is None else list(_ENTRY_RE.finditer(body))
    if entries:
        # after the last entry, with its indentation
        start, close = (match.start(1) + pos for pos in entries[-1].span())
        lead = _indent_before(text, start)
        if lead is None:
            return text[:close] + entry + text[close:]
        return f"{text[:close]}{eol}{lead}{entry}{text[close:]}"
    indent = _indent_before(text, match.start()) or ""
    step = "\t" if indent.startswith("\t") else "    "
    block = f"<{GAME_PATHS}>{eol}{indent}{step}{entry}{eol}{indent}</{GAME_PATHS}>"
    return text[: match.start()] + block + text[match.end() :]


def _shape(element: ET.Element) -> Any:
    return (
        element.tag,
        sorted(element.attrib.items()),
        (element.text or "").strip(),
        (element.tail or "").strip(),
        [_shape(child) for child in element],
    )


def _expected(text: str, mlc: str | None, game_path: str | None) -> Any:
    root = _parse(text)
    if mlc is not None:
        node = root.find(MLC)
        if node is None:
            node = ET.SubElement(root, MLC)
        node.text = mlc
    if game_path is not None and _norm(game_path) not in {_norm(g) for g in _paths(root).games}:
        block = root.find(GAME_PATHS)
        if block is None:
            block = ET.SubElement(root, GAME_PATHS)
        ET.SubElement(block, ENTRY).text = game_path
    return _shape(root)


def apply(text: str, mlc: str | None, game_path: str | None) -> str:
    """`text` with `mlc_path` set to `mlc` and `game_path` among the game paths.

    None leaves that element alone. Every other byte is kept, and the result must parse to
    the original with only those changes, or CemuConfigError is raised.
    """
    paths = read(text)
    new = text
    if mlc is not None and _norm(paths.mlc) != _norm(mlc):
        new = _set_mlc(new, mlc)
    else:
        mlc = None
    if game_path is not None and _norm(game_path) not in {_norm(g) for g in paths.games}:
        new = _add_game_path(new, game_path)
    else:
        game_path = None
    if new == text:
        return text
    try:
        same = _shape(_parse(new)) == _expected(text, mlc, game_path)
    except CemuConfigError as exc:
        raise CemuConfigError(f"the edit would not read back ({exc})") from exc
    if not same:
        raise CemuConfigError("the edit would change more than the two paths, so it was not made")
    return new
