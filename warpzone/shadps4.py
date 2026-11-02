"""Pure edits of shadPS4's list of install directories.

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
from collections.abc import Sequence
from typing import Any

JSON, TOML = "json", "toml"
_HEADER = re.compile(r"^\s*\[([^\[\]]+)\]\s*(?:#.*)?$")
_KEY = re.compile(r"^[ \t]*installDirs[ \t]*=[ \t]*", re.MULTILINE)


class ConfigError(Exception):
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
        raise ConfigError(f"not valid JSON ({exc})") from exc
    general = data.get("General") if isinstance(data, dict) else None
    if not isinstance(general, dict):
        raise ConfigError("it has no General table")
    dirs = general.get("install_dirs", [])
    if not isinstance(dirs, list) or not all(
        isinstance(d, dict) and isinstance(d.get("path"), str) for d in dirs
    ):
        raise ConfigError("General.install_dirs is not a list of {enabled, path} entries")
    return data, general, dirs


def _json_enabled(dirs: Sequence[dict[str, Any]]) -> set[str]:
    return {_norm(d["path"]) for d in dirs if d.get("enabled", True) is not False}


def _json_apply(text: str, wants: Sequence[str]) -> str:
    data, general, dirs = _json_load(text)
    # A layout other than the one shadPS4 writes would be rewritten wholesale.
    dumped = json.dumps(data, indent=2, ensure_ascii=False)
    if dumped != text.rstrip("\n") and dumped != text.rstrip("\r\n"):
        raise ConfigError("the file layout is not the one shadPS4 writes, so it was not edited")
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
        raise ConfigError(f"not valid TOML ({exc})") from exc
    gui = data.get("GUI", {})
    dirs = gui.get("installDirs", []) if isinstance(gui, dict) else None
    if not isinstance(dirs, list) or not all(isinstance(d, str) for d in dirs):
        raise ConfigError("GUI.installDirs is not a list of strings")
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
    raise ConfigError("GUI.installDirs is not closed")


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
                raise ConfigError("GUI.installDirs is not an array")
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
            raise ConfigError("the edit would change more than the list, so it was not made")
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"the edit would not read back ({exc})") from exc
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
