"""Format handlers for the structured settings files: JSON and TOML.

Both are pure, text in and text out. A handler records where the value of every
writable key sits in the text and replaces only that span, so comments, spacing,
ordering and every untouched key stay as the emulator wrote them.
"""

from __future__ import annotations

import copy
import json
import math
import re
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass

from . import tomlw
from .settings_formats import FormatError, NotWritable, ReadResult, Value

_BOM = "﻿"
_SCALARS = (str, int, float, bool)


def same(a: object, b: object) -> bool:
    """Typed equality: `1`, `1.0`, `True` and `"1"` all differ; NaN equals NaN."""
    if type(a) is not type(b):
        return False
    if isinstance(a, float) and isinstance(b, float):
        return a == b or (math.isnan(a) and math.isnan(b))
    return a == b


def _deep_same(a: object, b: object) -> bool:
    if isinstance(a, dict) and isinstance(b, dict):
        return a.keys() == b.keys() and all(_deep_same(a[k], b[k]) for k in a)
    if isinstance(a, list) and isinstance(b, list):
        return len(a) == len(b) and all(_deep_same(x, y) for x, y in zip(a, b, strict=True))
    return same(a, b)


def _split_bom(text: str) -> tuple[str, str]:
    return (_BOM, text[1:]) if text.startswith(_BOM) else ("", text)


# JSON ----------------------------------------------------------------------

_JSON_WS = " \t\n\r"
_JSON_NUMBER = re.compile(r"-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?(?:[eE][+-]?[0-9]+)?")


def _refuse_constant(name: str) -> object:
    raise ValueError(f"{name} is not JSON")


def _json_load(body: str) -> object:
    try:
        return json.loads(body, parse_constant=_refuse_constant)
    except (ValueError, RecursionError) as exc:
        raise FormatError(f"not valid JSON: {exc}") from exc


class _JsonScan:
    """Spans of the scalar leaves of a text that `json.loads` already accepted."""

    def __init__(self, body: str) -> None:
        self.body = body
        self.spans: dict[str, tuple[int, int]] = {}
        self.fixed: set[str] = set()
        self._seen: set[str] = set()
        start = self._ws(0)
        if body[start] == "{":
            self._object(start, [], False)

    def _ws(self, i: int) -> int:
        body = self.body
        while i < len(body) and body[i] in _JSON_WS:
            i += 1
        return i

    def _string_end(self, i: int) -> int:
        body = self.body
        i += 1
        while body[i] != '"':
            i += 2 if body[i] == "\\" else 1
        return i + 1

    def _skip(self, i: int) -> int:
        """The end of the value that starts at `i` (arrays and nested objects too)."""
        body = self.body
        ch = body[i]
        if ch == '"':
            return self._string_end(i)
        if ch in "[{":
            depth = 0
            while True:
                ch = body[i]
                if ch == '"':
                    i = self._string_end(i)
                    continue
                if ch in "[{":
                    depth += 1
                elif ch in "]}":
                    depth -= 1
                i += 1
                if depth == 0:
                    return i
        match = _JSON_NUMBER.match(body, i)
        if match:
            return match.end()
        for word in ("true", "false", "null"):
            if body.startswith(word, i):
                return i + len(word)
        raise FormatError("unexpected JSON token")

    def _mark(self, path: str, fixed: bool, span: tuple[int, int] | None) -> None:
        if path in self._seen or fixed or span is None:
            self.fixed.add(path)
            self.spans.pop(path, None)
        else:
            self.spans[path] = span
        self._seen.add(path)

    def _object(self, i: int, parts: list[str], dotted: bool) -> None:
        body = self.body
        i = self._ws(i + 1)
        if body[i] == "}":
            return
        while True:
            key_end = self._string_end(i)
            name = json.loads(body[i:key_end])
            i = self._ws(key_end)  # the colon
            i = self._ws(i + 1)
            here = [*parts, name]
            odd = dotted or "." in name or name == ""
            path = ".".join(here)
            ch = body[i]
            end = self._skip(i)
            if ch == "{":
                self._object(i, here, odd)
            elif ch == "[" or body.startswith("null", i):
                self._mark(path, True, None)
            else:
                self._mark(path, odd, (i, end))
            i = self._ws(end)
            if body[i] == "}":
                return
            i = self._ws(i + 1)


class _JsonHandler:
    name = "json"
    typed = True
    can_add = False

    def _parse(self, body: str) -> tuple[object, _JsonScan]:
        doc = _json_load(body)
        try:
            return doc, _JsonScan(body)
        except (IndexError, RecursionError) as exc:
            raise FormatError(f"not valid JSON: {exc}") from exc

    def read(self, text: str) -> ReadResult:
        _, body = _split_bom(text)
        _, scan = self._parse(body)
        values: dict[str, Value] = {}
        for path, (start, end) in scan.spans.items():
            values[path] = json.loads(body[start:end])
        return ReadResult(values, frozenset(scan.fixed))

    def write(self, text: str, changes: Mapping[str, Value]) -> str:
        bom, body = _split_bom(text)
        _, scan = self._parse(body)
        if not changes:
            return text
        current = {p: json.loads(body[s:e]) for p, (s, e) in scan.spans.items()}
        edits: list[tuple[int, int, str]] = []
        expected = json.loads(body)
        for key, value in changes.items():
            if key not in scan.spans:
                raise NotWritable(key)
            if not isinstance(value, _SCALARS) or (
                isinstance(value, float) and not math.isfinite(value)
            ):
                raise NotWritable(key)
            if same(current[key], value):
                continue
            start, end = scan.spans[key]
            edits.append((start, end, json.dumps(value, ensure_ascii=False)))
            node = expected
            *parents, last = key.split(".")
            for part in parents:
                node = node[part]  # type: ignore[index]
            node[last] = value  # type: ignore[index]
        for start, end, new in sorted(edits, reverse=True):
            body = body[:start] + new + body[end:]
        if edits:
            try:
                ok = _deep_same(json.loads(body), expected)
            except ValueError:
                ok = False
            if not ok:
                raise NotWritable(next(iter(changes)))
        return bom + body


JSON = _JsonHandler()


# TOML ----------------------------------------------------------------------

_BARE_KEY = re.compile(r"[A-Za-z0-9_-]+")
_BARE_VALUE = re.compile(r"[^\s#]+")


def _toml_load(body: str) -> dict[str, object]:
    try:
        return tomllib.loads(body)
    except (tomllib.TOMLDecodeError, ValueError, RecursionError) as exc:
        raise FormatError(f"not valid TOML: {exc}") from exc


def _eol(body: str) -> str:
    return "\r\n" if "\r\n" in body else "\n"


def _canonical(parts: tuple[str, ...]) -> str:
    return ".".join(tomlw.key(part) for part in parts)


def _quoted_end(body: str, i: int) -> int:
    """The end of the single-line string that starts at `i`."""
    quote = body[i]
    i += 1
    while body[i] != quote:
        if body[i] == "\\" and quote == '"':
            i += 1
        i += 1
    return i + 1


def _string_end(body: str, i: int) -> int:
    """The end of the string (any of the four kinds) that starts at `i`."""
    quote = body[i]
    if not body.startswith(quote * 3, i):
        return _quoted_end(body, i)
    i += 3
    while True:
        if body[i] == "\\" and quote == '"':
            i += 2
        elif body[i] == quote:
            run = 1
            while body.startswith(quote, i + run):
                run += 1
            if run >= 3:
                return i + run
            i += run
        else:
            i += 1


def _bracket_end(body: str, i: int) -> int:
    """The end of the array or inline table that starts at `i`."""
    depth = 0
    while True:
        ch = body[i]
        if ch in "\"'":
            i = _string_end(body, i)
            continue
        if ch == "#":
            while body[i] != "\n":
                i += 1
            continue
        if ch in "[{":
            depth += 1
        elif ch in "]}":
            depth -= 1
        i += 1
        if depth == 0:
            return i


def _parse_key(body: str, i: int) -> tuple[tuple[str, ...], int]:
    """A bare, quoted or dotted key at `i`: its parts and the end, past any spaces."""
    parts: list[str] = []
    while True:
        while body[i] in " \t":
            i += 1
        if body[i] in "\"'":
            end = _quoted_end(body, i)
            (name,) = tomllib.loads(body[i:end] + " = 0")
            parts.append(name)
        else:
            match = _BARE_KEY.match(body, i)
            if not match:
                raise FormatError("unexpected TOML key")
            end = match.end()
            parts.append(match.group())
        i = end
        while body[i] in " \t":
            i += 1
        if body[i] != ".":
            return tuple(parts), i
        i += 1


@dataclass
class _Table:
    header_end: int
    last_end: int | None = None


class _TomlScan:
    """Where the keys of a text that `tomllib` already accepted sit.

    `spans` holds the value span of every writable key (a single, not dotted, key
    with a single-line scalar under a standard table); `fixed` holds the other
    keys that have a value. `tables` maps every standard table (the root is
    `()`) to its header line and its last key line, and `arrays` is the set of
    arrays of tables, whose entries are not addressable by a path.
    """

    def __init__(self, body: str) -> None:
        self.body = body
        self.spans: dict[tuple[str, ...], tuple[int, int]] = {}
        self.fixed: set[tuple[str, ...]] = set()
        self.tables: dict[tuple[str, ...], _Table] = {(): _Table(0)}
        self.arrays: set[tuple[str, ...]] = set()
        self._scan()

    def in_array(self, table: tuple[str, ...]) -> bool:
        return any(table[:n] in self.arrays for n in range(1, len(table) + 1))

    def _line_end(self, i: int) -> int:
        end = self.body.find("\n", i)
        return len(self.body) if end < 0 else end + 1

    def _scan(self) -> None:
        body = self.body
        n = len(body)
        pos = 0
        table: tuple[str, ...] = ()
        while pos < n:
            while pos < n and body[pos] in " \t":
                pos += 1
            if pos >= n:
                break
            ch = body[pos]
            if ch in "\r\n#":
                pos = self._line_end(pos)
            elif ch == "[":
                double = body.startswith("[[", pos)
                table, after = _parse_key(body, pos + (2 if double else 1))
                if double:
                    self.arrays.add(table)
                else:
                    self.tables[table] = _Table(self._line_end(after))
                pos = self._line_end(after)
            else:
                pos = self._statement(pos, table)

    def _statement(self, pos: int, table: tuple[str, ...]) -> int:
        body = self.body
        parts, i = _parse_key(body, pos)
        i += 1  # the `=`
        while body[i] in " \t":
            i += 1
        ch = body[i]
        simple = False
        if body.startswith(('"""', "'''"), i):
            end = _string_end(body, i)
        elif ch in "\"'":
            end = _quoted_end(body, i)
            simple = True
        elif ch in "[{":
            end = _bracket_end(body, i)
        else:
            end = _BARE_VALUE.match(body, i).end()  # type: ignore[union-attr]
            simple = True
        line_end = self._line_end(end)
        path = (*table, *parts)
        if table in self.tables and not self.in_array(table):
            self.tables[table].last_end = line_end
        if simple and len(parts) == 1 and not self.in_array(table):
            self.spans[path] = (i, end)
        else:
            self.fixed.add(path)
        return line_end


def _lookup(doc: object, parts: tuple[str, ...]) -> object:
    node = doc
    for part in parts:
        node = node[part]  # type: ignore[index]
    return node


class _TomlHandler:
    name = "toml"
    typed = True
    can_add = True

    def _parse(
        self, body: str
    ) -> tuple[dict[str, object], _TomlScan, dict[tuple[str, ...], Value]]:
        doc = _toml_load(body)
        try:
            scan = _TomlScan(body)
        except (IndexError, RecursionError, ValueError) as exc:
            raise FormatError(f"not valid TOML: {exc}") from exc
        values: dict[tuple[str, ...], Value] = {}
        for path in list(scan.spans):
            value = _lookup(doc, path)
            if isinstance(value, _SCALARS):
                values[path] = value
            else:  # a date or time
                del scan.spans[path]
                scan.fixed.add(path)
        return doc, scan, values

    def read(self, text: str) -> ReadResult:
        _, body = _split_bom(text)
        _, scan, values = self._parse(body)
        return ReadResult(
            {_canonical(path): value for path, value in values.items()},
            frozenset(_canonical(path) for path in scan.fixed),
        )

    def write(self, text: str, changes: Mapping[str, Value]) -> str:
        bom, body = _split_bom(text)
        doc, scan, values = self._parse(body)
        if not changes:
            return text
        try:
            return bom + self._write(body, doc, scan, values, changes)
        except _CheckFailed:
            pass
        for key, value in changes.items():  # name the key that cannot be written
            try:
                self._write(body, doc, scan, values, {key: value})
            except _CheckFailed:
                raise NotWritable(key) from None
        raise NotWritable(next(iter(changes)))

    def _write(
        self,
        body: str,
        doc: dict[str, object],
        scan: _TomlScan,
        values: dict[tuple[str, ...], Value],
        changes: Mapping[str, Value],
    ) -> str:
        by_name = {_canonical(path): path for path in values}
        fixed = {_canonical(path) for path in scan.fixed}
        expected = copy.deepcopy(doc)
        edits: list[tuple[int, int, str]] = []
        adds: dict[tuple[str, ...], list[tuple[str, Value]]] = {}
        for key, value in changes.items():
            if key in fixed:
                raise NotWritable(key)
            if not isinstance(value, _SCALARS):
                raise NotWritable(key)
            try:
                literal = tomlw.scalar(value)
            except ValueError:
                raise NotWritable(key) from None
            path = by_name.get(key)
            if path is None:
                path = self._split(key)
                adds.setdefault(path[:-1], []).append((path[-1], value))
                if scan.in_array(path[:-1]):
                    raise NotWritable(key)
            elif same(values[path], value):
                continue
            else:
                start, end = scan.spans[path]
                edits.append((start, end, literal))
            node = expected
            for part in path[:-1]:
                node = node.setdefault(part, {})  # type: ignore[assignment]
                if not isinstance(node, dict):
                    raise NotWritable(key)
            node[path[-1]] = value
        eol = _eol(body)
        for table, entries in adds.items():
            info = scan.tables.get(table)
            if info is None:
                continue
            at = info.last_end if info.last_end is not None else info.header_end
            lines = [f"{tomlw.key(name)} = {tomlw.scalar(v)}" for name, v in entries]
            if at > 0 and body[at - 1] != "\n":  # the last line has no line ending
                block = eol + eol.join(lines)
            else:
                block = eol.join(lines) + eol
                if at == 0 and body and not body.startswith(("\n", "\r")):
                    block += eol  # keys of a file that starts with a table
            edits.append((at, at, block))
        for start, end, new in sorted(edits, key=lambda e: (e[0], e[1]), reverse=True):
            body = body[:start] + new + body[end:]
        tail = ""
        for table, entries in adds.items():
            if table in scan.tables:
                continue
            tail += eol
            tail += f"[{_canonical(table)}]" + eol
            tail += "".join(f"{tomlw.key(name)} = {tomlw.scalar(v)}{eol}" for name, v in entries)
        if tail:
            if body and not body.endswith("\n"):
                body += eol
            body += tail if body else tail[len(eol) :]
        try:
            ok = _deep_same(tomllib.loads(body), expected)
        except (tomllib.TOMLDecodeError, ValueError):
            ok = False
        if not ok:
            raise _CheckFailed
        return body

    @staticmethod
    def _split(key: str) -> tuple[str, ...]:
        try:
            parts, _ = _parse_key(key + "=", 0)
        except (IndexError, FormatError, ValueError):
            raise NotWritable(key) from None
        if _canonical(parts) != key:
            raise NotWritable(key)
        return parts


class _CheckFailed(Exception):
    """The edited text does not parse to the expected document."""


TOML = _TomlHandler()
