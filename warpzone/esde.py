"""ES-DE startup script and the `es_settings.xml` setting that turns scripts on. Pure: no I/O.

ES-DE runs every file in `<application data>/scripts/<event>/` when that event fires, but only
while *Enable custom event scripts* is on (`CustomEventScripts` in `es_settings.xml`, off by
default). `provision device` writes the startup script and turns the setting on; `doctor`
reads both. See docs/design.md, "ES-DE startup script".
"""

from __future__ import annotations

import re
import shlex
import xml.etree.ElementTree as ET
from pathlib import Path

SCRIPT_NAME = "warpzone-settings.sh"
PLAY_STATE_SCRIPT = "warpzone-play-state.sh"
LINKS_SCRIPT = "warpzone-links.sh"
SETTING = "CustomEventScripts"
MENU_PATH = "Main menu > Other settings > Enable custom event scripts"
# What the process list shows for ES-DE: `comm` of the unpacked binary, and `argv[0]` of the
# AppImage. To be confirmed on a device.
ESDE_PROCESSES = ("es-de", "ES-DE.AppImage")

_ELEMENT = f'<bool name="{SETTING}" value="true" />'
_DECLARATION = re.compile(r"\A\s*<\?xml[^>]*\?>")
# An opening `<bool ...>` tag; quoted attribute values may contain `>`.
_BOOL_TAG = re.compile(r"<bool\b(?:[^<>\"']|\"[^\"]*\"|'[^']*')*>")
_VALUE_ATTR = re.compile(r"(\bvalue\s*=\s*)(\"[^\"]*\"|'[^']*')")
_LINE = re.compile(r"[^\n]*\n|[^\n]+")

Element = tuple[str, tuple[tuple[str, str], ...]]


class EsdeError(Exception):
    """`es_settings.xml` cannot be read or edited safely."""


def script_path(esde_dir: Path, name: str = SCRIPT_NAME) -> Path:
    return esde_dir / "scripts" / "startup" / name


def settings_path(esde_dir: Path) -> Path:
    return esde_dir / "settings" / "es_settings.xml"


def _quote(value: str) -> str:
    """`value` as one single-quoted shell word (always quoted, as in the design)."""
    quoted = shlex.quote(value)
    return quoted if quoted.startswith("'") else f"'{quoted}'"


def render_startup_script(python: str, device: str, config: Path | None = None) -> str:
    """The script text; `config` is the private config file when it is not the default one."""
    where = f" --config {_quote(str(config))}" if config is not None else ""
    return (
        "#!/bin/sh\n"
        "# Written by warpzone provision device, which rewrites it. Applies the emulator\n"
        "# settings from the hub. ES-DE waits for it, so it is bounded and never fails.\n"
        "env -u LD_LIBRARY_PATH -u LD_PRELOAD -u PYTHONHOME -u PYTHONPATH \\\n"
        f"  timeout 15 {_quote(python)} -m warpzone \\\n"
        f"  settings apply --device {_quote(device)}{where} --quiet >/dev/null 2>&1\n"
        "exit 0\n"
    )


def render_play_state_script(python: str, device: str, config: Path | None = None) -> str:
    """The script that merges the play state. ES-DE fires `startup` before it reads a gamelist."""
    where = f" --config {_quote(str(config))}" if config is not None else ""
    return (
        "#!/bin/sh\n"
        "# Written by warpzone provision device, which rewrites it. Merges favourites and\n"
        "# play statistics into the gamelists, which ES-DE reads after this script ends.\n"
        "env -u LD_LIBRARY_PATH -u LD_PRELOAD -u PYTHONHOME -u PYTHONPATH \\\n"
        f"  timeout 15 {_quote(python)} -m warpzone \\\n"
        f"  play-state sync {_quote(device)}{where} --frontend-starting --quiet >/dev/null 2>&1\n"
        "exit 0\n"
    )


def render_links_script(python: str, device: str, config: Path | None = None) -> str:
    """The script that links new library games into the emulators' game folders."""
    where = f" --config {_quote(str(config))}" if config is not None else ""
    return (
        "#!/bin/sh\n"
        "# Written by warpzone provision device, which rewrites it. Links the games the library\n"
        "# delivered into the emulators' own game folders. ES-DE waits for it, so it is bounded\n"
        "# and never fails.\n"
        "env -u LD_LIBRARY_PATH -u LD_PRELOAD -u PYTHONHOME -u PYTHONPATH \\\n"
        f"  timeout 15 {_quote(python)} -m warpzone \\\n"
        f"  links sync {_quote(device)}{where} --quiet >/dev/null 2>&1\n"
        "exit 0\n"
    )


def script_interpreter(text: str) -> str | None:
    """The Python the script runs (`<python> -m warpzone`), or None if it is not our script."""
    try:
        tokens = shlex.split(text.replace("\\\n", " "), comments=True)
    except ValueError:
        return None
    for i in range(1, len(tokens) - 1):
        if tokens[i] == "-m" and tokens[i + 1] == "warpzone":
            return tokens[i - 1]
    return None


def _elements(text: str) -> list[Element]:
    """Every element of the file as (tag, sorted attributes).

    ES-DE writes a list of elements after the XML declaration, with no root element, so
    the body is wrapped in one to be parsed. A file that does have a root parses too.
    """
    body = _DECLARATION.sub("", text.lstrip("﻿"), count=1)
    try:
        root = ET.fromstring(f"<settings>{body}</settings>")
    except ET.ParseError as exc:
        raise EsdeError(f"es_settings.xml is not well-formed XML ({exc})") from exc
    return [(el.tag, tuple(sorted(el.attrib.items()))) for el in root.iter() if el is not root]


def _is_setting(element: Element) -> bool:
    return element[0] == "bool" and dict(element[1]).get("name") == SETTING


def event_scripts_enabled(xml_text: str) -> bool | None:
    """Whether `CustomEventScripts` is on; None when it is absent, odd or the file is unreadable."""
    try:
        elements = _elements(xml_text)
    except EsdeError:
        return None
    found: bool | None = None
    for element in elements:
        if _is_setting(element):
            value = dict(element[1]).get("value")
            found = {"true": True, "false": False}.get(value or "")
    return found


def _set_existing(text: str) -> tuple[str, int]:
    count = 0

    def flip(match: re.Match[str]) -> str:
        nonlocal count
        tag = match.group(0)
        probe = tag if tag.endswith("/>") else f"{tag}</bool>"
        try:
            attrs = ET.fromstring(probe).attrib
        except ET.ParseError:
            return tag
        if attrs.get("name") != SETTING:
            return tag
        if not _VALUE_ATTR.search(tag):
            raise EsdeError(f"{SETTING} in es_settings.xml has no value attribute")
        count += 1
        return _VALUE_ATTR.sub(lambda m: f"{m.group(1)}{m.group(2)[0]}true{m.group(2)[0]}", tag, 1)

    return _BOOL_TAG.sub(flip, text), count


def _add_line(text: str) -> str:
    lines = _LINE.findall(text)
    anchor = next((i for i in range(len(lines) - 1, -1, -1) if "<bool" in lines[i]), None)
    if anchor is None:
        anchor = next((i for i in range(len(lines) - 1, -1, -1) if lines[i].strip()), None)
    default_eol = "\r\n" if "\r\n" in text else "\n"
    if anchor is None:
        return text + _ELEMENT + default_eol
    line = lines[anchor]
    ending = line[len(line.rstrip("\r\n")) :]
    indent = line[: len(line) - len(line.lstrip(" \t"))]
    if ending:
        lines.insert(anchor + 1, f"{indent}{_ELEMENT}{ending}")
    else:  # the file ends without a newline: the new line takes that place
        lines[anchor] = line + default_eol
        lines.insert(anchor + 1, f"{indent}{_ELEMENT}")
    return "".join(lines)


def enable_event_scripts(text: str) -> str:
    """`text` with `CustomEventScripts` set to true; every other byte is kept.

    Changes only the `value` of the existing element, or adds that one line after the last
    `<bool .../>` line with its indentation and line ending. Raises EsdeError when the file
    does not parse, or when the result would not be the same settings plus that one.
    """
    before = _elements(text)
    if event_scripts_enabled(text) is True:
        return text
    new, count = _set_existing(text)
    if not count:
        new = _add_line(text)
    on: tuple[tuple[str, str], ...] = (("name", SETTING), ("value", "true"))
    expected = [("bool", on) if _is_setting(e) else e for e in before]
    if not count:
        expected.append(("bool", on))
    if sorted(_elements(new)) != sorted(expected):
        raise EsdeError("the edit would change something else in es_settings.xml")
    return new
