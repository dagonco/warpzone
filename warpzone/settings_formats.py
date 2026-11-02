"""Format handlers of the settings layer: config file text in, canonical keys out.

A handler is pure (text in, text out). This module holds the line-oriented formats
(`ini`, `qt-ini`, `retroarch-cfg`, `yaml-flat`); `json` and `toml` live in
`settings_tree` and are imported on demand. See "Format handlers" in docs/design.md.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from functools import lru_cache
from typing import Protocol

Value = str | int | float | bool

BUILTIN_SECRET_WORDS = ("token", "password", "passwd", "secret", "apikey", "api_key")


@dataclass(frozen=True)
class ReadResult:
    # Canonical key -> value, for every key that can be read and written.
    values: dict[str, Value]
    # Keys present in the file that cannot be written (not in `values`).
    fixed: frozenset[str]


class NotWritable(Exception):
    def __init__(self, key: str, reason: str = "") -> None:
        super().__init__(f"{key}: {reason}" if reason else key)
        self.key = key


class FormatError(Exception):
    """The text cannot be parsed (including text that is not UTF-8)."""


class Handler(Protocol):
    name: str
    typed: bool
    can_add: bool

    def read(self, text: str) -> ReadResult: ...

    def write(self, text: str, changes: Mapping[str, Value]) -> str: ...


def decode(data: bytes) -> str:
    """UTF-8 text; a BOM stays in the string as U+FEFF."""
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise FormatError(f"not UTF-8 text: {exc.reason} at byte {exc.start}") from exc


@lru_cache(maxsize=512)
def _pattern(pattern: str) -> re.Pattern[str]:
    parts = []
    for ch in pattern:
        if ch == "*":
            parts.append(".*")
        elif ch == "?":
            parts.append(".")
        else:
            parts.append(re.escape(ch))
    return re.compile("".join(parts), re.DOTALL)


def match(pattern: str, key: str) -> bool:
    """`*` is any run of characters and `?` one character; everything else is literal."""
    return _pattern(pattern).fullmatch(key) is not None


def is_builtin_secret(key: str) -> bool:
    """A key whose last part (after `]`, then after the last `.`) names a secret."""
    last = key.rsplit("]", 1)[-1].rsplit(".", 1)[-1].lower()
    return any(word in last for word in BUILTIN_SECRET_WORDS)


def handler(name: str) -> Handler:
    if name in _HANDLERS:
        return _HANDLERS[name]
    if name in ("json", "toml"):
        from . import settings_tree

        return settings_tree.JSON if name == "json" else settings_tree.TOML
    raise ValueError(f"unknown settings format {name!r}")


# --- Line helpers ---------------------------------------------------------------------

_BOM = "﻿"
_LINE = re.compile(r"[^\n]*\n|[^\n]+")


def _lines(text: str) -> tuple[str, list[str]]:
    """The BOM and the lines, each with its own terminator (LF or CRLF, or none)."""
    bom = _BOM if text.startswith(_BOM) else ""
    return bom, _LINE.findall(text, len(bom))


def _body(line: str) -> str:
    if line.endswith("\r\n"):
        return line[:-2]
    return line[:-1] if line.endswith("\n") else line


def _eol(line: str) -> str:
    return line[len(_body(line)) :]


def _dominant_eol(lines: list[str]) -> str:
    crlf = sum(1 for line in lines if line.endswith("\r\n"))
    lf = sum(1 for line in lines if line.endswith("\n")) - crlf
    return "\r\n" if crlf > lf else "\n"


def _replace_span(line: str, start: int, end: int, new: str) -> str:
    body = _body(line)
    return body[:start] + new + body[end:] + _eol(line)


def _insert_after(lines: list[str], inserts: dict[int, list[str]], eol: str) -> list[str]:
    """Insert the given bodies after the given line indexes, with the file's line ending.

    A last line without a terminator gets one, and the last inserted line takes its place
    as the unterminated one, so a missing final newline stays missing.
    """
    out: list[str] = []
    last = len(lines) - 1
    for i, line in enumerate(lines):
        out.append(line)
        new = inserts.get(i)
        if not new:
            continue
        if i == last and not _eol(line):
            out[-1] = line + eol
            out.extend(n + eol for n in new[:-1])
            out.append(new[-1])
        else:
            out.extend(n + eol for n in new)
    return out


def _append(lines: list[str], inserts: dict[int, list[str]], tail: list[str]) -> list[str]:
    """Apply `inserts`, then append `tail` bodies at the end of the file."""
    eol = _dominant_eol(lines)
    if tail:
        if lines:
            inserts.setdefault(len(lines) - 1, []).extend(tail)
        else:
            return [t + eol for t in tail]
    return _insert_after(lines, inserts, eol)


def _check_text_value(key: str, value: object) -> str:
    if not isinstance(value, str):
        raise NotWritable(key, "this format holds text values only")
    if "\n" in value or "\r" in value:
        raise NotWritable(key, "a value cannot hold a line break")
    return value


def _verify(handler_: Handler, out: str, changes: Mapping[str, Value]) -> None:
    """The result must read back as asked; otherwise the value cannot be stored."""
    values = handler_.read(out).values
    for key, value in changes.items():
        got = values.get(key)
        if got != value or type(got) is not type(value):
            raise NotWritable(key, "the value would not read back as written")


# --- ini and qt-ini -------------------------------------------------------------------

_SECTION = re.compile(r"\s*\[([^\]]*)\]\s*(?:[;#].*)?")
_INI_KEY = re.compile(r"(?P<key>[^=\s](?:[^=]*[^=\s])?)(?P<sep>\s*=\s*)(?P<value>.*?)\s*")
_COMPANION = "\\default"


@dataclass(frozen=True)
class _KeyLine:
    index: int
    start: int  # offset of the value in the line's body
    end: int
    value: str
    sep: str


class _IniDoc:
    """One pass over an `ini` or `qt-ini` file."""

    def __init__(self, lines: list[str], *, qt: bool) -> None:
        self.keys: dict[str, list[_KeyLine]] = {}
        self.companions: dict[str, list[_KeyLine]] = {}
        self.orphans: set[str] = set()
        self.insert_at: dict[str, int] = {}  # section -> line after which a key is added
        self.styles: dict[str, str] = {}  # section -> the sep of its last key with a value
        self.style: str | None = None  # the same, for the whole file
        section: str | None = None
        for i, line in enumerate(lines):
            body = _body(line)
            stripped = body.strip()
            if not stripped or stripped[0] in ";#":
                continue
            header = _SECTION.fullmatch(body)
            if header:
                section = header[1]
                self.insert_at.setdefault(section, i)
                continue
            m = _INI_KEY.fullmatch(body.lstrip())
            if not m:
                continue
            offset = len(body) - len(body.lstrip())
            kl = _KeyLine(
                i, offset + m.start("value"), offset + m.end("value"), m["value"], m["sep"]
            )
            name = m["key"]
            if section is None:
                self.orphans.add(f"[]{name}")
                continue
            self.insert_at[section] = i
            if qt and name.endswith(_COMPANION) and len(name) > len(_COMPANION):
                canonical = f"[{section}]{name[: -len(_COMPANION)]}"
                self.companions.setdefault(canonical, []).append(kl)
                continue
            self.keys.setdefault(f"[{section}]{name}", []).append(kl)
            if m["value"]:
                self.styles[section] = m["sep"]
                self.style = m["sep"]

    @property
    def fixed(self) -> frozenset[str]:
        dup = {k for k, occ in self.keys.items() if len(occ) > 1}
        dup_companion = {k for k, occ in self.companions.items() if len(occ) > 1 and k in self.keys}
        return frozenset(dup | dup_companion | self.orphans)

    @property
    def values(self) -> dict[str, Value]:
        fixed = self.fixed
        return {k: occ[0].value for k, occ in self.keys.items() if k not in fixed}


class _IniHandler:
    typed = False
    can_add = True

    def __init__(self, name: str, *, qt: bool) -> None:
        self.name = name
        self.qt = qt
        self._default_sep = "=" if qt else " = "

    def read(self, text: str) -> ReadResult:
        _, lines = _lines(text)
        doc = _IniDoc(lines, qt=self.qt)
        return ReadResult(doc.values, doc.fixed)

    def write(self, text: str, changes: Mapping[str, Value]) -> str:
        if not changes:
            return text
        bom, lines = _lines(text)
        doc = _IniDoc(lines, qt=self.qt)
        fixed = doc.fixed
        has_companions = bool(doc.companions)
        edits: dict[int, tuple[int, int, str]] = {}  # line -> span and new text
        inserts: dict[int, list[str]] = {}
        tail: list[str] = []
        new_sections: dict[str, list[str]] = {}

        existing = []
        added = []
        for key, raw in changes.items():
            if key in fixed:
                raise NotWritable(key, "the key is present more than once or outside a section")
            value = _check_text_value(key, raw)
            (existing if key in doc.keys else added).append((key, value))

        for key, value in existing:
            kl = doc.keys[key][0]
            edits[kl.index] = (kl.start, kl.end, self._spaced(kl, value))
            companion = doc.companions.get(key)
            if companion:
                if companion[0].value != "false":
                    edits[companion[0].index] = (companion[0].start, companion[0].end, "false")
            elif has_companions:
                section, name = _split_canonical(key)
                sep = self._sep(doc, section)
                inserts.setdefault(kl.index, []).append(f"{name}{_COMPANION}{sep}false")

        for key, value in added:
            section, name = _split_canonical(key)
            if section == "" or (self.qt and name.endswith(_COMPANION)):
                raise NotWritable(key, "a key cannot be added here")
            if name[0] in ";#[" or name != name.strip() or re.search(r"[=\r\n]", name):
                raise NotWritable(key, "not a valid key name")
            sep = self._sep(doc, section)
            new = [f"{name}{sep}{value}"]
            if self.qt and has_companions:
                new.append(f"{name}{_COMPANION}{sep}false")
            if section in doc.insert_at:
                inserts.setdefault(doc.insert_at[section], []).extend(new)
            else:
                new_sections.setdefault(section, []).extend(new)

        for index, (start, end, new_text) in edits.items():
            lines[index] = _replace_span(lines[index], start, end, new_text)
        for section, body in new_sections.items():
            if (tail and tail[-1] != "") or (not tail and not _ends_blank(lines, inserts)):
                tail.append("")
            tail.append(f"[{section}]")
            tail.extend(body)
        out = bom + "".join(_append(lines, inserts, tail))
        _verify(self, out, dict(existing) | dict(added))
        return out

    def _sep(self, doc: _IniDoc, section: str) -> str:
        return doc.styles.get(section) or doc.style or self._default_sep

    @staticmethod
    def _spaced(kl: _KeyLine, value: str) -> str:
        # `Key =` has no room before the value: give it the space `Key = x` would have.
        if not kl.value and kl.sep.endswith("=") and kl.sep != "=":
            return " " + value
        return value


def _ends_blank(lines: list[str], inserts: dict[int, list[str]]) -> bool:
    """Does the file, with the inserts applied, end in a blank line (or have no content)?"""
    if not lines:
        return True
    pending = inserts.get(len(lines) - 1)
    last = pending[-1] if pending else _body(lines[-1])
    return not last.strip()


def _split_canonical(key: str) -> tuple[str, str]:
    """`[Section]Name` -> (Section, Name); NotWritable when the key has another shape."""
    if not key.startswith("["):
        raise NotWritable(key, "not a [Section]Key name")
    close = key.find("]")
    name = key[close + 1 :]
    if close < 0 or not name.strip():
        raise NotWritable(key, "not a [Section]Key name")
    return key[1:close], name


# --- retroarch-cfg --------------------------------------------------------------------

_RA_KEY = re.compile(r'(?P<key>[^\s=#"]\S*?)(?P<sep>\s*=\s*)(?P<rest>.*)')
_RA_NEW_KEY = re.compile(r'[^\s=#"][^\s="]*')


@dataclass(frozen=True)
class _RaLine:
    index: int
    start: int  # span of the value (inside the quotes when quoted)
    end: int
    value: str
    quoted: bool


def _ra_parse(lines: list[str]) -> tuple[dict[str, list[_RaLine]], set[str]]:
    keys: dict[str, list[_RaLine]] = {}
    broken: set[str] = set()
    for i, line in enumerate(lines):
        body = _body(line)
        stripped = body.lstrip()
        if not stripped or stripped[0] == "#":
            continue
        m = _RA_KEY.fullmatch(stripped)
        if not m:
            continue
        offset = len(body) - len(stripped)
        rest_at = offset + m.start("rest")
        rest = m["rest"]
        if rest.startswith('"'):
            close = rest.find('"', 1)
            if close < 0:
                broken.add(m["key"])
                continue
            kl = _RaLine(i, rest_at + 1, rest_at + close, rest[1:close], True)
        else:
            value = rest.rstrip()
            kl = _RaLine(i, rest_at, rest_at + len(value), value, False)
        keys.setdefault(m["key"], []).append(kl)
    return keys, broken


class _RetroArchHandler:
    name = "retroarch-cfg"
    typed = False
    can_add = True

    def read(self, text: str) -> ReadResult:
        _, lines = _lines(text)
        keys, broken = _ra_parse(lines)
        fixed = {k for k, occ in keys.items() if len(occ) > 1} | broken
        return ReadResult(
            {k: occ[0].value for k, occ in keys.items() if k not in fixed}, frozenset(fixed)
        )

    def write(self, text: str, changes: Mapping[str, Value]) -> str:
        if not changes:
            return text
        bom, lines = _lines(text)
        keys, broken = _ra_parse(lines)
        tail: list[str] = []
        checked: dict[str, Value] = {}
        for key, raw in changes.items():
            value = _check_text_value(key, raw)
            if '"' in value:
                raise NotWritable(key, "RetroArch cannot store a double quote in a value")
            if key in broken or len(keys.get(key, ())) > 1:
                raise NotWritable(key, "the key is present more than once or malformed")
            checked[key] = value
            if key in keys:
                kl = keys[key][0]
                if kl.quoted:
                    lines[kl.index] = _replace_span(lines[kl.index], kl.start, kl.end, value)
                else:
                    lines[kl.index] = _replace_span(lines[kl.index], kl.start, kl.end, f'"{value}"')
            elif _RA_NEW_KEY.fullmatch(key):
                tail.append(f'{key} = "{value}"')
            else:
                raise NotWritable(key, "not a valid key name")
        out = bom + "".join(_append(lines, {}, tail))
        _verify(self, out, checked)
        return out


# --- yaml-flat ------------------------------------------------------------------------

_YAML_KEY = re.compile(
    r"(?P<key>(?:[^\s#\-]|-[^\s])[^\r\n]*?):(?:(?P<sep>[ \t]+)(?P<value>.*?))?[ \t]*"
)
_YAML_MARKER = re.compile(r"(---|\.\.\.)(\s.*)?")
_YAML_OPENERS = "[{|>&*!#%@`"


@dataclass(frozen=True)
class _YamlLine:
    index: int
    start: int
    end: int
    value: str
    colon: int  # offset right after the key's colon


def _yaml_parse(lines: list[str]) -> tuple[dict[str, list[_YamlLine]], set[str]]:
    keys: dict[str, list[_YamlLine]] = {}
    fixed: set[str] = set()
    current: str | None = None
    for i, line in enumerate(lines):
        body = _body(line)
        stripped = body.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if _YAML_MARKER.fullmatch(body):
            current = None
            continue
        m = _YAML_KEY.fullmatch(body) if not body[0].isspace() else None
        if not m:
            # An indented line, a sequence item or anything else belongs to the key above.
            if current is not None:
                fixed.add(current)
            continue
        key = m["key"]
        current = key
        value = m["value"] or ""
        start = m.start("value") if m["value"] is not None else m.end("key") + 1
        keys.setdefault(key, []).append(
            _YamlLine(i, start, start + len(value), value, m.end("key") + 1)
        )
        if _yaml_value_is_complex(value):
            fixed.add(key)
    return keys, fixed


def _yaml_value_is_complex(value: str) -> bool:
    if not value:
        return False
    if value[0] in _YAML_OPENERS or " #" in value:
        return True
    return value[0] in "\"'" and (len(value) < 2 or value[-1] != value[0])


class _YamlFlatHandler:
    name = "yaml-flat"
    typed = False
    can_add = False

    def read(self, text: str) -> ReadResult:
        _, lines = _lines(text)
        keys, fixed = _yaml_parse(lines)
        fixed |= {k for k, occ in keys.items() if len(occ) > 1}
        return ReadResult(
            {k: occ[0].value for k, occ in keys.items() if k not in fixed}, frozenset(fixed)
        )

    def write(self, text: str, changes: Mapping[str, Value]) -> str:
        if not changes:
            return text
        bom, lines = _lines(text)
        keys, fixed = _yaml_parse(lines)
        fixed |= {k for k, occ in keys.items() if len(occ) > 1}
        checked: dict[str, Value] = {}
        for key, raw in changes.items():
            value = _check_text_value(key, raw)
            if key in fixed:
                raise NotWritable(key, "the key holds more than a single scalar")
            if key not in keys:
                raise NotWritable(key, "this format cannot add a key")
            kl = keys[key][0]
            if kl.start == kl.colon:  # `key:` with nothing after the colon
                lines[kl.index] = _replace_span(lines[kl.index], kl.colon, kl.end, " " + value)
            else:
                lines[kl.index] = _replace_span(lines[kl.index], kl.start, kl.end, value)
            checked[key] = value
        out = bom + "".join(lines)
        _verify(self, out, checked)
        return out


_HANDLERS: dict[str, Handler] = {
    "ini": _IniHandler("ini", qt=False),
    "qt-ini": _IniHandler("qt-ini", qt=True),
    "retroarch-cfg": _RetroArchHandler(),
    "yaml-flat": _YamlFlatHandler(),
}
