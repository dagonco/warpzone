"""Edits of ES-DE gamelists, as text. Pure: no I/O.

ES-DE rewrites a gamelist whenever a game is launched, and a console receives it through
Syncthing, so an edit keeps every byte it has no reason to touch: comments, unknown elements,
the line endings and the file's own indentation. Two editors work on the same files and share
what is here: the arcade emulator of a game (`update_entry`, used by `inbox_mame` and
`inbox_console_iso`) and the play state of a device (`playstate`, which adds its own tags with
`add_child`). They differ in how much of the file they trust, so the primitives take that as an
argument (`game_blocks(skip_comments=...)`, `child_indent(line_start_only=...)`).

Nothing here imports the network: `playstate` runs on ES-DE's startup path (see "Speed" in
development/design/emulator-settings.md), so `escape` and `unescape` are local instead of
`xml.sax.saxutils`'.
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET

from warpzone.errors import WarpzoneError

# What `update_entry` starts from when the file does not exist yet.
EMPTY_GAMELIST = '<?xml version="1.0"?>\n<gameList>\n</gameList>\n'

COMMENT = re.compile(r"<!--.*?-->", re.DOTALL)
GAME = re.compile(r"<game(?:\s[^>]*)?>.*?</game\s*>", re.DOTALL)
_PATH = re.compile(r"<path>(.*?)</path>", re.DOTALL)
_PATH_AT_LINE_START = re.compile(r"^([ \t]*)<path\b", re.MULTILINE)
# `<altemulator>X</altemulator>` or the self-closing `<altemulator/>` (no group 1: empty).
_ALT_TAG = r"<altemulator>.*?</altemulator>|<altemulator\s*/>"
_ALT = re.compile(r"<altemulator>(.*?)</altemulator>|<altemulator\s*/>", re.DOTALL)
_ALT_LINE = re.compile(rf"^[ \t]*(?:{_ALT_TAG})[ \t]*\r?\n?", re.DOTALL | re.MULTILINE)
_LIST_CLOSE = re.compile(r"</gameList\s*>")
_LIST_EMPTY = re.compile(r"<gameList\s*/>")


class GamelistError(WarpzoneError):
    """The ES-DE gamelist cannot be updated; the message says why."""


# ------------------------------------------------------------------ text helpers


def escape(text: str) -> str:
    """XML text: `&`, `<` and `>` escaped."""
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def unescape(text: str) -> str:
    """The inverse of `escape` (`&amp;` last, so `&amp;lt;` becomes `&lt;`)."""
    return text.replace("&lt;", "<").replace("&gt;", ">").replace("&amp;", "&")


def eol_of(text: str) -> str:
    """The line ending the file uses: CRLF as soon as it has one, else LF."""
    return "\r\n" if "\r\n" in text else "\n"


def indent_before(text: str, pos: int) -> str | None:
    """The whitespace between the start of the line and `pos`, None if there is more."""
    start = text.rfind("\n", 0, pos) + 1
    before = text[start:pos]
    return before if not before.strip() else None


def _outside_comments(text: str) -> list[tuple[int, int]]:
    """Spans of `text` that are not inside an XML comment."""
    spans, at = [], 0
    for c in COMMENT.finditer(text):
        spans.append((at, c.start()))
        at = c.end()
    spans.append((at, len(text)))
    return spans


def last_outside_comments(pattern: re.Pattern[str], text: str) -> re.Match[str] | None:
    """The last match of `pattern` in `text` that is not inside an XML comment."""
    found = None
    for start, end in _outside_comments(text):
        for m in pattern.finditer(text, start, end):
            found = m
    return found


def expand_empty_list(text: str, eol: str) -> str:
    """`text` with a self-closing `<gameList/>` opened and closed on separate lines, so
    that entries can go inside it; `text` itself when it has none."""
    return _LIST_EMPTY.sub(f"<gameList>{eol}</gameList>", text, count=1)


def game_blocks(text: str, *, skip_comments: bool = True) -> list[re.Match[str]]:
    """Every `<game>...</game>` of `text`, in order; with `skip_comments`, not those that
    sit inside an XML comment."""
    if not skip_comments:
        return list(GAME.finditer(text))
    blocks: list[re.Match[str]] = []
    for start, end in _outside_comments(text):
        blocks.extend(GAME.finditer(text, start, end))
    return blocks


def child_indent(block: str, default: str, *, line_start_only: bool = False) -> str:
    """The indentation of the entry's `<path>`, which is the one its other tags follow;
    `default` when the entry has none or it is empty.

    By default the first `<path>` decides, and a path that shares its line with other content
    gives `default`. With `line_start_only` the first path that begins a line decides, whatever
    attributes it has.
    """
    if line_start_only:
        found = _PATH_AT_LINE_START.search(block)
        return (found.group(1) or default) if found else default
    path = _PATH.search(block)
    return (indent_before(block, path.start()) if path else None) or default


def add_child(block: str, element: str, eol: str, indent: str) -> str:
    """`block` (one `<game>`) with `element` as its last child, on a line of its own after
    the last one, indented by `indent`. When `</game>` shares its line with other content
    the element goes right before it."""
    close = block.rindex("</game")
    head = block[:close].rstrip(" \t")
    if not head.endswith(("\n", "\r")):
        head += eol
    closing_indent = indent_before(block, close)
    tail = block[close:]
    if closing_indent is None:
        return f"{block[:close]}{element}{tail}"
    return f"{head}{indent}{element}{eol}{closing_indent}{tail}"


# ------------------------------------------------------------------ the emulator of a game


def entry_path(block: str) -> str:
    """The `<path>` of an entry, decoded and without the leading `./`; "" when it has none."""
    m = _PATH.search(block)
    if not m:
        return ""
    path = unescape(m.group(1).strip())
    return path[2:] if path.startswith("./") else path


def emulators(text: str) -> dict[str, str | None]:
    """File name -> its <altemulator> (None when the entry has none), for the tests and
    the report."""
    result: dict[str, str | None] = {}
    for m in game_blocks(text):
        alt = _ALT.search(m.group(0))
        result[entry_path(m.group(0))] = unescape((alt.group(1) or "").strip()) if alt else None
    return result


def well_formed(text: str) -> bool:
    """ES-DE writes more than one top-level element; wrap before parsing."""
    body = re.sub(r"^\s*<\?xml[^>]*\?>", "", text)
    try:
        ET.fromstring(f"<w>{body}</w>")
    except ET.ParseError:
        return False
    return True


def update_entry(text: str, file_name: str, emulator: str | None) -> tuple[str, str]:
    """`text` with the per-game emulator of `file_name` set to `emulator` (removed when
    None), and what was done: "unchanged", "added", "changed" or "removed".
    Everything else, comments and unknown elements included, stays as it is."""
    nl = eol_of(text)
    target = next((m for m in game_blocks(text) if entry_path(m.group(0)) == file_name), None)
    if target is None:
        if emulator is None:
            return text, "unchanged"
        new = _insert_entry(text, file_name, emulator, nl)
        action = "added"
    else:
        block = target.group(0)
        alt = _ALT.search(block)
        if emulator is None:
            if alt is None:
                return text, "unchanged"
            stripped = _ALT_LINE.sub("", block, count=1)
            if stripped == block:  # on a line with other things: take only the element
                stripped = _ALT.sub("", block, count=1)
            inner = re.sub(r"^<game(?:\s[^>]*)?>|</game\s*>$", "", stripped)
            if re.fullmatch(r"\s*<path>[^<]*</path>\s*", inner):
                new = _drop_block(text, target)
            else:
                new = text[: target.start()] + stripped + text[target.end() :]
            action = "removed"
        else:
            wanted = escape(emulator)
            if alt is not None:
                if unescape((alt.group(1) or "").strip()) == emulator:
                    return text, "unchanged"
                element = f"<altemulator>{wanted}</altemulator>"
                edited = block[: alt.start()] + element + block[alt.end() :]
                action = "changed"
            else:
                edited = add_child(
                    block,
                    f"<altemulator>{wanted}</altemulator>",
                    nl,
                    child_indent(block, "\t\t"),
                )
                action = "added"
            new = text[: target.start()] + edited + text[target.end() :]
    if well_formed(text) and not well_formed(new):
        raise GamelistError("the edited gamelist would not be well-formed XML; left untouched")
    return new, action


def _drop_block(text: str, match: re.Match[str]) -> str:
    start, end = match.start(), match.end()
    line_start = text.rfind("\n", 0, start) + 1
    if not text[line_start:start].strip():  # the entry owns its lines: take them whole
        start = line_start
        tail = re.match(r"[ \t]*\r?\n", text[end:])
        if tail:
            end += tail.end()
    return text[:start] + text[end:]


def _insert_entry(text: str, file_name: str, emulator: str, nl: str) -> str:
    text = expand_empty_list(text, nl)
    close = last_outside_comments(_LIST_CLOSE, text)
    if close is None:
        raise GamelistError("no <gameList> element found")
    blocks = game_blocks(text)
    entry, child = "\t", "\t\t"
    if blocks:
        first = blocks[0]
        entry = indent_before(text, first.start()) or entry
        path = _PATH.search(first.group(0))
        if path:
            child = indent_before(first.group(0), path.start()) or child
    else:
        wrapper = indent_before(text, close.start())
        if wrapper:
            entry = wrapper + "\t"
            child = entry + "\t"
    lines = (
        f"{entry}<game>{nl}"
        f"{child}<path>./{escape(file_name)}</path>{nl}"
        f"{child}<altemulator>{escape(emulator)}</altemulator>{nl}"
        f"{entry}</game>{nl}"
    )
    at = close.start()
    indent = indent_before(text, at)
    if indent is not None:
        return text[: at - len(indent)] + lines + text[at - len(indent) :]
    return text[:at] + nl + lines + text[at:]
