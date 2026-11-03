"""Pure edits for shadPS4: its install directories and the launch script ES-DE runs.

shadPS4 runs extracted games it finds in its install directories. Two config files can hold
them, depending on the version: `config.json` (v0.21 and later: `General.install_dirs`, a
list of `{"enabled": true, "path": "..."}`) and the older `config.toml` (`GUI.installDirs`,
a list of strings). A profile with `[shadps4]` has `provision device` add the folders it
declares to both, and `doctor` check them. No I/O here. See docs/design.md, "shadPS4
install directories".
"""

from __future__ import annotations

import copy
import json
import re
import tomllib
import xml.etree.ElementTree as ET
from collections.abc import Sequence
from typing import Any

from warpzone.errors import WarpzoneError

JSON, TOML = "json", "toml"
_HEADER = re.compile(r"^\s*\[([^\[\]]+)\]\s*(?:#.*)?$")
_KEY = re.compile(r"^[ \t]*installDirs[ \t]*=[ \t]*", re.MULTILINE)


class Shadps4ConfigError(WarpzoneError):
    """The file is not one this module can read or edit safely."""


def kind_of(path: str) -> str | None:
    """ "json" or "toml" from a file name, None for anything else."""
    lowered = path.lower()
    if lowered.endswith(".json"):
        return JSON
    return TOML if lowered.endswith(".toml") else None


def _norm(path: str) -> str:
    return path.rstrip("/") or "/"


# ------------------------------------------------------------------ config.json


def _json_load(text: str) -> tuple[dict[str, Any], dict[str, Any], list[dict[str, Any]]]:
    try:
        data = json.loads(text)
    except ValueError as exc:
        raise Shadps4ConfigError(f"not valid JSON ({exc})") from exc
    general = data.get("General") if isinstance(data, dict) else None
    if not isinstance(general, dict):
        raise Shadps4ConfigError("it has no General table")
    dirs = general.get("install_dirs", [])
    if not isinstance(dirs, list) or not all(
        isinstance(d, dict) and isinstance(d.get("path"), str) for d in dirs
    ):
        raise Shadps4ConfigError("General.install_dirs is not a list of {enabled, path} entries")
    return data, general, dirs


def _json_enabled(dirs: Sequence[dict[str, Any]]) -> set[str]:
    return {_norm(d["path"]) for d in dirs if d.get("enabled", True) is not False}


def _json_apply(text: str, wants: Sequence[str]) -> str:
    data, general, dirs = _json_load(text)
    # A layout other than the one shadPS4 writes would be rewritten wholesale.
    dumped = json.dumps(data, indent=2, ensure_ascii=False)
    if dumped != text.rstrip("\n") and dumped != text.rstrip("\r\n"):
        raise Shadps4ConfigError(
            "the file layout is not the one shadPS4 writes, so it was not edited"
        )
    have = _json_enabled(dirs)
    changed = False
    for want in wants:
        if _norm(want) in have:
            continue
        changed = True
        entry = next((d for d in dirs if _norm(d["path"]) == _norm(want)), None)
        if entry is not None:
            entry["enabled"] = True
        else:
            dirs.append({"enabled": True, "path": want})
    if not changed:
        return text
    general["install_dirs"] = dirs
    out = json.dumps(data, indent=2, ensure_ascii=False)
    return out + ("\n" if text.endswith("\n") else "")


# ------------------------------------------------------------------ config.toml


def _toml_load(text: str) -> tuple[dict[str, Any], list[str]]:
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise Shadps4ConfigError(f"not valid TOML ({exc})") from exc
    gui = data.get("GUI", {})
    dirs = gui.get("installDirs", []) if isinstance(gui, dict) else None
    if not isinstance(dirs, list) or not all(isinstance(d, str) for d in dirs):
        raise Shadps4ConfigError("GUI.installDirs is not a list of strings")
    return data, dirs


def _array_end(text: str, start: int) -> int:
    """Index of the `]` that closes the array opening at `start`."""
    depth, i, size = 0, start, len(text)
    while i < size:
        char = text[i]
        if char in "\"'":
            i += 1
            while i < size and text[i] != char:
                i += 2 if char == '"' and text[i] == "\\" else 1
        elif char == "#":
            while i < size and text[i] != "\n":
                i += 1
        elif char == "[":
            depth += 1
        elif char == "]":
            depth -= 1
            if depth == 0:
                return i
        i += 1
    raise Shadps4ConfigError("GUI.installDirs is not closed")


def _gui_span(text: str) -> tuple[int, int] | None:
    """(start, end) of the lines of the `[GUI]` table, after its header."""
    start: int | None = None
    pos = 0
    for line in text.splitlines(keepends=True):
        match = _HEADER.match(line.rstrip("\r\n"))
        if match:
            if start is not None:
                return start, pos
            if match.group(1).strip() == "GUI":
                start = pos + len(line)
        pos += len(line)
    return None if start is None else (start, pos)


def _toml_apply(text: str, wants: Sequence[str]) -> str:
    data, current = _toml_load(text)
    have = {_norm(d) for d in current}
    add = [w for w in wants if _norm(w) not in have]
    if not add:
        return text
    merged = [*current, *add]
    value = "[" + ", ".join(json.dumps(d, ensure_ascii=False) for d in merged) + "]"
    eol = "\r\n" if "\r\n" in text else "\n"
    span = _gui_span(text)
    if span is None:
        glue = "" if not text or text.endswith("\n") else eol
        new = f"{text}{glue}{eol if text else ''}[GUI]{eol}installDirs = {value}{eol}"
    else:
        start, end = span
        key = _KEY.search(text, start, end)
        if key is not None:
            if text[key.end() : key.end() + 1] != "[":
                raise Shadps4ConfigError("GUI.installDirs is not an array")
            close = _array_end(text, key.end())
            new = text[: key.end()] + value + text[close + 1 :]
        else:
            line = f"installDirs = {value}"
            last = start + len(text[start:end].rstrip())
            if last == start:  # an empty table: the line goes right after the header
                lead = "" if text[:start].endswith("\n") else eol
                new = f"{text[:start]}{lead}{line}{eol}{text[start:]}"
            else:
                new = f"{text[:last]}{eol}{line}{text[last:]}"
    expected = copy.deepcopy(data)
    expected.setdefault("GUI", {})["installDirs"] = merged
    try:
        if tomllib.loads(new) != expected:
            raise Shadps4ConfigError("the edit would change more than the list, so it was not made")
    except tomllib.TOMLDecodeError as exc:
        raise Shadps4ConfigError(f"the edit would not read back ({exc})") from exc
    return new


# ------------------------------------------------------------------ both


def missing(kind: str, text: str, wants: Sequence[str]) -> list[str]:
    """The folders of `wants` that the config does not list (or lists disabled)."""
    if kind == JSON:
        have = _json_enabled(_json_load(text)[2])
    else:
        have = {_norm(d) for d in _toml_load(text)[1]}
    return [w for w in wants if _norm(w) not in have]


def apply(kind: str, text: str, wants: Sequence[str]) -> str:
    """`text` with every folder of `wants` listed; every other byte is kept."""
    return _json_apply(text, wants) if kind == JSON else _toml_apply(text, wants)


# ------------------------------------------------------------------ launch script

_LAUNCH_TEMPLATE = r"""#!/bin/bash
# Written by warpzone provision device, which rewrites it. ES-DE runs it with the path of a
# .desktop shortcut: it starts the selected shadPS4 emulator directly, which does not go
# through the Qt launcher.
ini=__INI__
versions=__VERSIONS__
sdl=Shadps4-sdl.AppImage

die() { echo "shadps4-launch: $*" >&2; exit 1; }

arg=$1
[ -n "$arg" ] || die "usage: $0 <shortcut.desktop>"
if [[ $arg == *.desktop ]]; then
  [ -r "$arg" ] || die "cannot read $arg"
  eboot=$(sed -n 's/^Exec=[^"]*"\(.*\)"[^"]*$/\1/p' "$arg" | head -n 1)
  [ -n "$eboot" ] || die "no quoted path in the Exec= line of $arg"
else
  eboot=$arg
fi

# A version is an AppImage, or the folder that holds one.
resolve() {
  local p=$1
  p=${p%$'\r'}
  p=${p#\"}
  p=${p%\"}
  [ -d "$p" ] && p=$p/$sdl
  [ -f "$p" ] && [ -x "$p" ] && printf '%s\n' "$p"
}

emu=
if [ -r "$ini" ]; then
  sel=$(sed -n 's/^versionSelected=//p' "$ini" | head -n 1)
  [ -n "$sel" ] && emu=$(resolve "$sel")
fi
if [ -z "$emu" ] && [ -r "$versions" ]; then
  while IFS= read -r entry; do
    emu=$(resolve "$entry") && [ -n "$emu" ] && break
    emu=
  done < <(grep -o '"path"[[:space:]]*:[[:space:]]*"[^"]*"' "$versions" |
    sed 's/^[^:]*:[[:space:]]*"//; s/"$//')
fi
[ -n "$emu" ] || die "no shadPS4 version found: open the shadPS4 launcher once and download one"

exec "$emu" -f true -g "$eboot"
"""


def _sh_quote(value: str) -> str:
    return "'" + value.replace("'", "'\\''") + "'"


def render_launch_script(qt_ini: str, versions: str) -> str:
    """The bash script text; `qt_ini` and `versions` are absolute paths of the Qt launcher."""
    return _LAUNCH_TEMPLATE.replace("__INI__", _sh_quote(qt_ini)).replace(
        "__VERSIONS__", _sh_quote(versions)
    )


def _find_entry(text: str, emulator: str) -> re.Match[str]:
    """The first `<entry>` of the first staticpath rule of the emulator's element."""
    try:
        root = ET.fromstring(text.encode())
    except ET.ParseError as exc:
        raise Shadps4ConfigError(f"not well-formed XML ({exc})") from exc
    blocks = [e for e in root.iter("emulator") if e.get("name") == emulator]
    rule = next((r for b in blocks for r in b.iter("rule") if r.get("type") == "staticpath"), None)
    if rule is None or rule.find("entry") is None:
        raise Shadps4ConfigError(f"it has no staticpath rule for {emulator}")
    block = re.search(
        rf'<emulator\b[^>]*\bname\s*=\s*["\']{re.escape(emulator)}["\'][^>]*>.*?</emulator\s*>',
        text,
        re.DOTALL,
    )
    if block is None:
        raise Shadps4ConfigError(f"it has no staticpath rule for {emulator}")
    entry = re.compile(
        r'(<rule\b[^>]*\btype\s*=\s*["\']staticpath["\'][^>]*>.*?<entry>)([^<]*)(</entry>)',
        re.DOTALL,
    ).search(text, block.start(), block.end())
    if entry is None:
        raise Shadps4ConfigError(f"it has no staticpath rule for {emulator}")
    return entry


def find_rule_target(text: str, emulator: str) -> str:
    """The path in the emulator's first staticpath entry of ES-DE's `es_find_rules.xml`."""
    return ET.fromstring(f"<x>{_find_entry(text, emulator).group(2)}</x>").text or ""


def point_find_rule(text: str, emulator: str, target: str) -> str:
    """`text` with that entry set to `target`; every other byte is kept."""
    entry = _find_entry(text, emulator)
    if find_rule_target(text, emulator) == target:
        return text
    escaped = target.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    new = text[: entry.start(2)] + escaped + text[entry.end(2) :]
    if find_rule_target(new, emulator) != target:
        raise Shadps4ConfigError("the edit would change more than the entry, so it was not made")
    return new
