"""Format handler `esde-xml` of the settings layer: ES-DE's `es_settings.xml`. Pure.

The file is a flat list of `<bool name="Key" value="true" />`, `<int ...>`, `<float ...>` and
`<string ...>` elements after an XML declaration (see `esde.parse_elements`). The canonical
key is the `name` attribute and the value is the `value` attribute read as the tag says. A
write replaces only the text of that attribute, so every other byte stays. See "Format
handlers" in development/design/emulator-settings.md.
"""

from __future__ import annotations

import math
import re
import xml.etree.ElementTree as ET
from collections.abc import Mapping
from dataclasses import dataclass

from warpzone import esde
from warpzone.settings_formats import FormatError, NotWritable, ReadResult, Value

# An opening `<bool ...>`, `<int ...>`, `<float ...>` or `<string ...>` tag; quoted attribute
# values may contain `>`. Group 1 is the tag name and group 2 what follows it.
_TAG = re.compile(r"<(bool|int|float|string)(?=[\s/>])((?:[^<>\"']|\"[^\"]*\"|'[^']*')*)>")
_ATTR = re.compile(r"""([^\s=/>"']+)\s*=\s*(\"[^\"]*\"|'[^']*')""")
# Comments, CDATA sections and processing instructions (the XML declaration): not settings.
_INERT = re.compile(r"<!--.*?-->|<!\[CDATA\[.*?\]\]>|<\?.*?\?>", re.DOTALL)
_INT = re.compile(r"[+-]?\d+")
_FLOAT = re.compile(r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?")
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")

# Settings warpzone owns, so a layer must not fight it: provision turns the scripts on.
OWNED = frozenset({esde.SETTING})
# A setting that names a place on this machine is never shared. ES-DE spells them
# `ROMDirectory`, `MediaDirectory` and so on.
_LOCAL_SUFFIXES = ("Directory", "Dir", "Path", "Paths")


def is_machine_setting(name: str) -> bool:
    """Whether a layer must never carry this key: warpzone owns it or it is a path."""
    return name in OWNED or name.endswith(_LOCAL_SUFFIXES)


@dataclass(frozen=True)
class _Setting:
    tag: str
    raw: str | None  # the value attribute, unescaped; None when the element has none
    start: int  # span of the attribute's text, inside its quotes
    end: int
    quote: str


def _blank(match: re.Match[str]) -> str:
    return re.sub(r"[^\n]", " ", match[0])


def _scan(text: str) -> dict[str, list[_Setting]]:
    """Every setting element by name, in file order. FormatError when the file is not XML."""
    try:
        esde.parse_elements(text)
    except esde.EsdeError as exc:
        raise FormatError(str(exc)) from exc
    found: dict[str, list[_Setting]] = {}
    for tag in _TAG.finditer(_INERT.sub(_blank, text)):
        source = tag[0]
        try:
            attrs = ET.fromstring(source if source.endswith("/>") else f"{source}</{tag[1]}>")
        except ET.ParseError:
            continue
        name = attrs.attrib.get("name")
        if name is None:
            continue
        span = None
        for attr in _ATTR.finditer(tag[2]):
            if attr[1] == "value":
                at = tag.start(2) + attr.start(2)
                span = (at + 1, tag.start(2) + attr.end(2) - 1, text[at])
        if span is None:
            found.setdefault(name, []).append(_Setting(tag[1], None, 0, 0, '"'))
        else:
            found.setdefault(name, []).append(
                _Setting(tag[1], attrs.attrib["value"], span[0], span[1], span[2])
            )
    return found


def _typed(tag: str, raw: str) -> Value | None:
    """The value as its tag reads it, or None when the text is not a value of that kind."""
    if tag == "bool":
        return {"true": True, "false": False}.get(raw)
    if tag == "int":
        return int(raw) if _INT.fullmatch(raw) else None
    if tag == "float":
        if not _FLOAT.fullmatch(raw):
            return None
        number = float(raw)
        return number if math.isfinite(number) else None
    return raw


def _classify(found: dict[str, list[_Setting]]) -> tuple[dict[str, Value], set[str]]:
    values: dict[str, Value] = {}
    fixed: set[str] = set()
    for name, occurrences in found.items():
        first = occurrences[0]
        typed = None if first.raw is None else _typed(first.tag, first.raw)
        if len(occurrences) > 1 or is_machine_setting(name) or typed is None:
            fixed.add(name)
        else:
            values[name] = typed
    return values, fixed


def _text(tag: str, value: Value, quote: str) -> str | None:
    """`value` as the text of a `tag` attribute quoted with `quote`; None for another kind."""
    if tag == "bool":
        return ("true" if value else "false") if type(value) is bool else None
    if tag == "int":
        return str(value) if type(value) is int else None
    if tag == "float":
        if type(value) is not float or not math.isfinite(value):
            return None
        return repr(value)
    if type(value) is not str or _CONTROL.search(value):
        return None
    escaped = value.replace("&", "&amp;").replace("<", "&lt;")
    return escaped.replace(quote, "&quot;" if quote == '"' else "&apos;")


class _EsdeXmlHandler:
    name = "esde-xml"
    typed = True
    # ES-DE writes every setting it knows when it quits, so a key the file lacks is a typo or
    # belongs to a newer ES-DE; adding it would only keep the typo in the file.
    can_add = False

    def read(self, text: str) -> ReadResult:
        values, fixed = _classify(_scan(text))
        return ReadResult(values, frozenset(fixed))

    def write(self, text: str, changes: Mapping[str, Value]) -> str:
        if not changes:
            return text
        found = _scan(text)
        values, fixed = _classify(found)
        edits: list[tuple[int, int, str]] = []
        expected = dict(values)
        for key, value in changes.items():
            if key in fixed:
                raise NotWritable(key, "the setting is owned by warpzone, local or not plain")
            if key not in found:
                raise NotWritable(key, "this format cannot add a key")
            setting = found[key][0]
            new = _text(setting.tag, value, setting.quote)
            if new is None:
                raise NotWritable(key, f"the setting is a <{setting.tag}>: the value does not fit")
            expected[key] = value
            if values[key] != value or type(values[key]) is not type(value):
                edits.append((setting.start, setting.end, new))
        for start, end, new in sorted(edits, reverse=True):
            text = text[:start] + new + text[end:]
        after = self.read(text)
        if after.values != expected or after.fixed != frozenset(fixed):
            raise NotWritable(next(iter(changes)), "the value would not read back as written")
        return text


ESDE_XML = _EsdeXmlHandler()
