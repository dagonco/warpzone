"""Format handlers for the structured settings files: JSON and TOML.

Both are pure, text in and text out. A handler records where the value of every
writable key sits in the text and replaces only that span, so comments, spacing,
ordering and every untouched key stay as the emulator wrote them.
"""

from __future__ import annotations

import json
import math
import re
from collections.abc import Mapping
from dataclasses import dataclass

try:
    from .settings_formats import FormatError, NotWritable, ReadResult, Value
except ImportError:  # settings_formats.py is not on this branch yet
    Value = str | int | float | bool

    @dataclass(frozen=True)
    class ReadResult:
        values: dict[str, Value]
        fixed: frozenset[str]

    class NotWritable(Exception):
        def __init__(self, key: str) -> None:
            super().__init__(key)
            self.key = key

    class FormatError(Exception):
        """The text cannot be parsed."""


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
