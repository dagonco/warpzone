"""Pure edits for the Switch emulators' lists of folders that hold updates and DLC.

Eden keeps the list in `qt-config.ini` (`[UI]`, the Qt array `Paths\\external_content_dirs`),
Ryujinx in `Config.json` (`autoload_dirs`). Both scan the folder and its subfolders for the
update and DLC files, so one folder holds the library's `extras/switch`. A profile with
`[switch_content.*]` has `provision device` add that folder to the list, keeping every other
entry and every other byte, and `doctor` check it. The edit is made as text and the result is
read back, and must differ from the original only by the new entry. No I/O here. See
development/design/profiles.md, "Switch update and DLC folders".
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from warpzone.errors import WarpzoneError

EDEN, RYUJINX = "eden", "ryujinx"
EMULATORS = (EDEN, RYUJINX)
LABELS = {EDEN: "Eden", RYUJINX: "Ryujinx"}

_BOM = "﻿"


class ContentConfigError(WarpzoneError):
    """The file is not one this module can read or edit safely."""


def _norm(path: str) -> str:
    return path.rstrip("/") or "/"


def _eol(text: str) -> str:
    return "\r\n" if "\r\n" in text else "\n"


# ------------------------------------------------------------------ Eden: qt-config.ini

_EDEN_SECTION = "UI"
_ARRAY = "Paths\\external_content_dirs"
_HEADER = re.compile(r"\[([^\]]*)\]\s*")
_ENTRY = re.compile(r"[ \t]*Paths\\external_content_dirs\\(\d+)\\path[ \t]*=[ \t]*(.*?)[ \t]*")
_SIZE = re.compile(r"[ \t]*Paths\\external_content_dirs\\size[ \t]*=[ \t]*(.*?)[ \t]*")
# What Qt writes without quotes or escapes; any other path is left to the user.
_PLAIN = re.compile(r"[A-Za-z0-9_./+-]+(?: [A-Za-z0-9_./+-]+)*")
_ESCAPE = re.compile(r"\\(.)")


@dataclass(frozen=True)
class _QtArray:
    entries: dict[int, tuple[int, str]]  # array index -> (line, value as written)
    size: tuple[int, int] | None  # (line, the count)
    end: int  # the line after the last non-blank line of [UI]


def _body(line: str) -> str:
    return line.rstrip("\r\n")


def _qt_parse(lines: list[str]) -> _QtArray:
    section: str | None = None
    seen = 0
    entries: dict[int, tuple[int, str]] = {}
    size: tuple[int, int] | None = None
    end = 0
    for i, line in enumerate(lines):
        body = _body(line)
        header = _HEADER.fullmatch(body.lstrip(_BOM) if i == 0 else body)
        if header:
            section = header[1]
            if section == _EDEN_SECTION:
                seen += 1
                end = i + 1
            continue
        if section != _EDEN_SECTION:
            continue
        if body.strip():
            end = i + 1
        if m := _ENTRY.fullmatch(body):
            if int(m[1]) in entries:
                raise ContentConfigError(f"{_ARRAY}\\{m[1]}\\path appears more than once")
            entries[int(m[1])] = (i, m[2])
        elif m := _SIZE.fullmatch(body):
            if size is not None:
                raise ContentConfigError(f"{_ARRAY}\\size appears more than once")
            if not m[1].isascii() or not m[1].isdigit():
                raise ContentConfigError(f"{_ARRAY}\\size is not a number")
            size = (i, int(m[1]))
    if seen != 1:
        raise ContentConfigError(
            f"it has no [{_EDEN_SECTION}] section"
            if not seen
            else f"it has more than one [{_EDEN_SECTION}] section"
        )
    return _QtArray(entries, size, end)


def _unquote(value: str) -> str:
    if len(value) >= 2 and value[0] == value[-1] == '"':
        return _ESCAPE.sub(r"\1", value[1:-1])
    return value


def _qt_listed(array: _QtArray) -> tuple[str, ...]:
    """The entries Eden reads: indices 1 to `size`, empty ones skipped."""
    count = array.size[1] if array.size is not None else 0
    values = (_unquote(array.entries[n][1]) for n in range(1, count + 1) if n in array.entries)
    return tuple(v for v in values if v)


def _is_array_line(body: str) -> bool:
    return bool(_ENTRY.fullmatch(body) or _SIZE.fullmatch(body))


def _rest(lines: list[str]) -> list[str]:
    """The lines other than those of the array in [UI]."""
    kept, section = [], None
    for line in lines:
        body = _body(line)
        header = _HEADER.fullmatch(body.lstrip(_BOM))
        if header:
            section = header[1]
        elif section == _EDEN_SECTION and _is_array_line(body):
            continue
        kept.append(line)
    return kept


def _qt_value(folder: str) -> str:
    if not _PLAIN.fullmatch(folder):
        raise ContentConfigError(
            f"{folder} has characters that Qt escapes in an ini file, so it was not added"
        )
    return folder


def _insert(lines: list[str], at: int, new: list[str], eol: str) -> None:
    """Insert `new` (lines without an end of line) before line `at`, ending like the file."""
    block = [n + eol for n in new]
    if at >= len(lines) and lines and not lines[-1].endswith("\n"):
        lines[-1] += eol  # the file did not end with a newline: it still will not
        block[-1] = new[-1]
    lines[at:at] = block


def _qt_apply(text: str, folder: str) -> str:
    lines = text.splitlines(keepends=True)
    array = _qt_parse(lines)
    listed = _qt_listed(array)
    if _norm(folder) in {_norm(v) for v in listed}:
        return text
    value, eol = _qt_value(folder), _eol(text)
    number = (array.size[1] if array.size is not None else 0) + 1
    entry = f"{_ARRAY}\\{number}\\path={value}"
    stale = array.entries.get(number)
    if stale is not None:
        # past the end of the array, so Eden ignores it: this one takes its place
        line = lines[stale[0]]
        lines[stale[0]] = entry + line[len(_body(line)) :]
        if array.size is None:
            _insert(lines, stale[0] + 1, [f"{_ARRAY}\\size={number}"], eol)
    elif array.size is not None:
        _insert(lines, array.size[0], [entry], eol)
    else:
        last = max((n[0] for n in array.entries.values()), default=None)
        at = array.end if last is None else last + 1
        _insert(lines, at, [entry, f"{_ARRAY}\\size={number}"], eol)
    if array.size is not None:
        # the line moved down by one when the entry went above it
        at = array.size[0] + (0 if stale is not None else 1)
        line = lines[at]
        body = _body(line)
        match = _SIZE.fullmatch(body)
        assert match is not None
        lines[at] = body[: match.start(1)] + str(number) + body[match.end(1) :] + line[len(body) :]
    new = "".join(lines)
    try:
        new_lines = new.splitlines(keepends=True)
        same = _qt_listed(_qt_parse(new_lines)) == (*listed, value)
        same = same and _rest(new_lines) == _rest(text.splitlines(keepends=True))
    except ContentConfigError as exc:
        raise ContentConfigError(f"the edit would not read back ({exc})") from exc
    if not same:
        raise ContentConfigError(
            "the edit would change more than the new entry, so it was not made"
        )
    return new


def _qt_read(text: str) -> tuple[str, ...]:
    return _qt_listed(_qt_parse(text.splitlines(keepends=True)))


# ------------------------------------------------------------------ Ryujinx: Config.json

_KEY = "autoload_dirs"
_JSON = json.JSONDecoder()


def _skip(text: str, i: int) -> int:
    while i < len(text) and text[i] in " \t\r\n":
        i += 1
    return i


def _json_members(text: str) -> dict[str, list[tuple[int, int, Any]]]:
    """Each top-level key of the object `text` holds: where its value starts and ends, and it."""
    members: dict[str, list[tuple[int, int, Any]]] = {}
    try:
        i = _skip(text, 1 if text.startswith(_BOM) else 0)
        if text[i : i + 1] != "{":
            raise ContentConfigError("it is not a JSON object")
        i = _skip(text, i + 1)
        while text[i : i + 1] != "}":
            key, i = _JSON.raw_decode(text, i)
            i = _skip(text, i)
            if not isinstance(key, str) or text[i : i + 1] != ":":
                raise ValueError("a key is not followed by a colon")
            start = _skip(text, i + 1)
            value, i = _JSON.raw_decode(text, start)
            members.setdefault(key, []).append((start, i, value))
            i = _skip(text, i)
            if text[i : i + 1] == ",":
                i = _skip(text, i + 1)
            elif text[i : i + 1] != "}":
                raise ValueError("a value is not followed by a comma")
        if _skip(text, i + 1) != len(text):
            raise ValueError("there is text after the object")
    except ValueError as exc:
        raise ContentConfigError(f"not valid JSON ({exc})") from exc
    return members


def _json_dirs(text: str) -> tuple[int, int, list[str]]:
    found = _json_members(text).get(_KEY)
    if not found:
        raise ContentConfigError(f"it has no {_KEY} list")
    if len(found) > 1:
        raise ContentConfigError(f"{_KEY} appears more than once")
    start, end, value = found[0]
    if not isinstance(value, list) or not all(isinstance(d, str) for d in value):
        raise ContentConfigError(f"{_KEY} is not a list of strings")
    return start, end, value


def _json_read(text: str) -> tuple[str, ...]:
    return tuple(_json_dirs(text)[2])


def _json_apply(text: str, folder: str) -> str:
    start, end, dirs = _json_dirs(text)
    if _norm(folder) in {_norm(d) for d in dirs}:
        return text
    item = json.dumps(folder, ensure_ascii=False)
    eol = _eol(text)
    line = text.rfind("\n", 0, start) + 1
    key_line = text[line:start]
    indent = key_line[: len(key_line) - len(key_line.lstrip(" \t"))]
    if not dirs:
        if key_line.strip().startswith('"') and indent:
            new = f"[{eol}{indent}{indent}{item}{eol}{indent}]"
        else:
            new = f"[{item}]"
        edited = text[:start] + new + text[end:]
    else:
        last = start + len(text[start : end - 1].rstrip())
        row = text.rfind("\n", 0, last) + 1
        lead = text[row:last]
        shown = lead.lstrip(" \t")
        own_line = row > start and shown.startswith('"')
        if own_line:
            pad = lead[: len(lead) - len(shown)]
            edited = f"{text[:last]},{eol}{pad}{item}{text[last:]}"
        else:
            edited = f"{text[:last]}, {item}{text[last:]}"
    try:
        expected = json.loads(text.removeprefix(_BOM))
        expected[_KEY] = [*dirs, folder]
        same = json.loads(edited.removeprefix(_BOM)) == expected
    except ValueError as exc:
        raise ContentConfigError(f"the edit would not read back ({exc})") from exc
    if not same:
        raise ContentConfigError(
            "the edit would change more than the new entry, so it was not made"
        )
    return edited


# ------------------------------------------------------------------ the two together

_READ: dict[str, Callable[[str], tuple[str, ...]]] = {EDEN: _qt_read, RYUJINX: _json_read}
_APPLY: dict[str, Callable[[str, str], str]] = {EDEN: _qt_apply, RYUJINX: _json_apply}


def read(emulator: str, text: str) -> tuple[str, ...]:
    """The folders the emulator's file lists (the ones it reads, for Eden)."""
    return _READ[emulator](text)


def missing(emulator: str, text: str, folder: str) -> bool:
    """Whether `folder` is not listed yet (trailing slashes do not count)."""
    return _norm(folder) not in {_norm(d) for d in read(emulator, text)}


def apply(emulator: str, text: str, folder: str) -> str:
    """`text` with `folder` among the emulator's folders.

    Every other byte is kept, and the result must read back as the original plus `folder`, or
    ContentConfigError is raised.
    """
    return _APPLY[emulator](text, folder)
