"""Pure edits for shadPS4: its install and DLC directories and the launch script ES-DE runs.

shadPS4 runs extracted games it finds in its install directories. Two config files can hold
them, depending on the version: `config.json` (v0.21 and later: `General.install_dirs`, a
list of `{"enabled": true, "path": "..."}`) and the older `config.toml` (`GUI.installDirs`,
a list of strings). The same files hold the one folder it reads DLC from
(`General.addon_install_dir` in JSON, `GUI.addonInstallDir` in TOML, a path string). A
profile with `[shadps4]` has `provision device` add the install folders it declares to both
and set the DLC folder, and `doctor` check them. No I/O here. See
development/design/profiles.md, "shadPS4 install directories".
"""

from __future__ import annotations

import copy
import json
import re
import tomllib
import xml.etree.ElementTree as ET
from collections.abc import Callable, Sequence
from typing import Any

from warpzone.errors import WarpzoneError

JSON, TOML = "json", "toml"
_HEADER = re.compile(r"^\s*\[([^\[\]]+)\]\s*(?:#.*)?$")
_KEY = re.compile(r"^[ \t]*installDirs[ \t]*=[ \t]*", re.MULTILINE)
_ADDON_KEY = re.compile(r"^[ \t]*addonInstallDir[ \t]*=[ \t]*", re.MULTILINE)


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


def _same_addon(current: str, want: str) -> bool:
    """Whether the DLC folder is already `want`: unset never is, trailing slashes do not count."""
    return bool(current) and _norm(current) == _norm(want)


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


def _json_addon(general: dict[str, Any]) -> str:
    value = general.get("addon_install_dir", "")
    if not isinstance(value, str):
        raise Shadps4ConfigError("General.addon_install_dir is not a string")
    return value


def _json_apply(text: str, wants: Sequence[str], addon: str | None) -> str:
    data, general, dirs = _json_load(text)
    # A layout other than the one shadPS4 writes would be rewritten wholesale.
    dumped = json.dumps(data, indent=2, ensure_ascii=False)
    if dumped != text.rstrip("\n") and dumped != text.rstrip("\r\n"):
        raise Shadps4ConfigError(
            "the file layout is not the one shadPS4 writes, so it was not edited"
        )
    have = _json_enabled(dirs)
    changed = False
    if addon is not None and not _same_addon(_json_addon(general), addon):
        changed = True
        general["addon_install_dir"] = addon
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


def _toml_put(
    text: str,
    line: str,
    key_re: re.Pattern[str],
    value_end: Callable[[str, int], int],
    value: str,
) -> str:
    """`text` with `value` as that key of the `[GUI]` table; `line` is used when it is new.

    An existing value runs from the key's `=` to `value_end(text, start)` and is replaced; a
    missing key is added as `line` after the table's last line, and a missing table at the end.
    """
    eol = "\r\n" if "\r\n" in text else "\n"
    span = _gui_span(text)
    if span is None:
        glue = "" if not text or text.endswith("\n") else eol
        return f"{text}{glue}{eol if text else ''}[GUI]{eol}{line}{eol}"
    start, end = span
    key = key_re.search(text, start, end)
    if key is not None:
        return text[: key.end()] + value + text[value_end(text, key.end()) :]
    last = start + len(text[start:end].rstrip())
    if last == start:  # an empty table: the line goes right after the header
        lead = "" if text[:start].endswith("\n") else eol
        return f"{text[:start]}{lead}{line}{eol}{text[start:]}"
    return f"{text[:last]}{eol}{line}{text[last:]}"


def _array_value_end(text: str, start: int) -> int:
    if text[start : start + 1] != "[":
        raise Shadps4ConfigError("GUI.installDirs is not an array")
    return _array_end(text, start) + 1


def _string_value_end(text: str, start: int) -> int:
    """Index just after the single-line string that starts at `start`."""
    quote = text[start : start + 1]
    if quote not in ("'", '"') or text[start : start + 3] in ('"""', "'''"):
        raise Shadps4ConfigError("GUI.addonInstallDir is not a single-line string")
    i, size = start + 1, len(text)
    while i < size and text[i] not in (quote, "\n"):
        i += 2 if quote == '"' and text[i] == "\\" else 1
    if i >= size or text[i] != quote:
        raise Shadps4ConfigError("GUI.addonInstallDir is not closed")
    return i + 1


def _checked(new: str, expected: dict[str, Any]) -> str:
    """`new` if it reads back as `expected`, that is, the edit changed nothing else."""
    try:
        if tomllib.loads(new) != expected:
            raise Shadps4ConfigError(
                "the edit would change more than the value, so it was not made"
            )
    except tomllib.TOMLDecodeError as exc:
        raise Shadps4ConfigError(f"the edit would not read back ({exc})") from exc
    return new


def _toml_apply_dirs(text: str, wants: Sequence[str]) -> str:
    data, current = _toml_load(text)
    have = {_norm(d) for d in current}
    add = [w for w in wants if _norm(w) not in have]
    if not add:
        return text
    merged = [*current, *add]
    value = "[" + ", ".join(json.dumps(d, ensure_ascii=False) for d in merged) + "]"
    new = _toml_put(text, f"installDirs = {value}", _KEY, _array_value_end, value)
    expected = copy.deepcopy(data)
    expected.setdefault("GUI", {})["installDirs"] = merged
    return _checked(new, expected)


def _toml_addon(text: str) -> str:
    try:
        gui = tomllib.loads(text).get("GUI", {})
    except tomllib.TOMLDecodeError as exc:
        raise Shadps4ConfigError(f"not valid TOML ({exc})") from exc
    value = gui.get("addonInstallDir", "") if isinstance(gui, dict) else ""
    if not isinstance(value, str):
        raise Shadps4ConfigError("GUI.addonInstallDir is not a string")
    return value


def _toml_apply_addon(text: str, want: str) -> str:
    if _same_addon(_toml_addon(text), want):
        return text
    value = json.dumps(want, ensure_ascii=False)
    new = _toml_put(text, f"addonInstallDir = {value}", _ADDON_KEY, _string_value_end, value)
    expected = copy.deepcopy(tomllib.loads(text))
    expected.setdefault("GUI", {})["addonInstallDir"] = want
    return _checked(new, expected)


def _toml_apply(text: str, wants: Sequence[str], addon: str | None) -> str:
    new = _toml_apply_dirs(text, wants)
    return new if addon is None else _toml_apply_addon(new, addon)


# ------------------------------------------------------------------ both


def missing(kind: str, text: str, wants: Sequence[str]) -> list[str]:
    """The folders of `wants` that the config does not list (or lists disabled)."""
    if kind == JSON:
        have = _json_enabled(_json_load(text)[2])
    else:
        have = {_norm(d) for d in _toml_load(text)[1]}
    return [w for w in wants if _norm(w) not in have]


def addon_gap(kind: str, text: str, want: str | None) -> str | None:
    """The DLC folder the config has now ("" if unset) when it is not `want`, else None.

    None also when `want` is None (the profile declares no DLC folder). Trailing slashes do
    not count as a difference.
    """
    if want is None:
        return None
    current = _json_addon(_json_load(text)[1]) if kind == JSON else _toml_addon(text)
    return None if _same_addon(current, want) else current


def apply(kind: str, text: str, wants: Sequence[str], addon: str | None = None) -> str:
    """`text` with every folder of `wants` listed and the DLC folder set to `addon`.

    The DLC folder is replaced when it differs and left alone when `addon` is None; every
    other byte is kept.
    """
    return _json_apply(text, wants, addon) if kind == JSON else _toml_apply(text, wants, addon)


# ------------------------------------------------------------------ launch script

SDL_APPIMAGE = "Shadps4-sdl.AppImage"
LAUNCH_LOG = "shadps4-launch.log"  # in ~/.local/state/warpzone/, appended to by the script
NO_VERSION_FIX = (
    "open the shadPS4 Qt launcher, go to Version Manager, then download and select a version"
)
_SELECTED = re.compile(r"^versionSelected=(.*)$", re.MULTILINE)
_VERSION_PATH = re.compile(r'"path"\s*:\s*"([^"]*)"')

_LAUNCH_TEMPLATE = r"""#!/bin/bash
# Written by warpzone provision device, which rewrites it. ES-DE runs it with the path of a
# .desktop shortcut: it starts the selected shadPS4 emulator directly, which does not go
# through the Qt launcher.
ini=__INI__
versions=__VERSIONS__
sdl=__SDL__
state=${XDG_STATE_HOME:-}
case $state in /*) ;; *) state=$HOME/.local/state ;; esac
log=$state/warpzone/__LOG__

# ES-DE does not show stderr, so a failure is also appended to the log and, in a graphical
# session, shown in a dialog that closes by itself (zenity, else kdialog, else notify-send).
notify() {
  local title="warpzone: shadPS4 did not start" limit=
  [ -n "${DISPLAY:-}${WAYLAND_DISPLAY:-}" ] || return 0
  command -v timeout >/dev/null 2>&1 && limit="timeout 60"
  if command -v zenity >/dev/null 2>&1; then
    $limit zenity --error --no-markup --timeout=60 --title="$title" --text="$1" >/dev/null 2>&1
  elif command -v kdialog >/dev/null 2>&1; then
    $limit kdialog --error "$1" --title "$title" >/dev/null 2>&1
  elif command -v notify-send >/dev/null 2>&1; then
    $limit notify-send -u critical -t 30000 "$title" "$1" >/dev/null 2>&1
  fi
  return 0
}

die() {
  echo "shadps4-launch: $*" >&2
  if mkdir -p "${log%/*}" 2>/dev/null; then
    printf '%s %s\n' "$(date '+%Y-%m-%dT%H:%M:%S%z')" "$*" >>"$log" 2>/dev/null
  fi
  notify "$*"
  exit 1
}

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
[ -n "$emu" ] || die "no shadPS4 version found: __NO_VERSION_FIX__"

exec "$emu" -f true -g "$eboot"
"""


def _sh_quote(value: str) -> str:
    return "'" + value.replace("'", "'\\''") + "'"


def render_launch_script(qt_ini: str, versions: str) -> str:
    """The bash script text; `qt_ini` and `versions` are absolute paths of the Qt launcher."""
    return (
        _LAUNCH_TEMPLATE.replace("__INI__", _sh_quote(qt_ini))
        .replace("__VERSIONS__", _sh_quote(versions))
        .replace("__SDL__", SDL_APPIMAGE)
        .replace("__LOG__", LAUNCH_LOG)
        .replace("__NO_VERSION_FIX__", NO_VERSION_FIX)
    )


def select_version(
    ini_text: str | None,
    versions_text: str | None,
    resolve: Callable[[str], str | None],
) -> str | None:
    """The emulator the launch script would run, None when it finds none.

    Mirrors the script: the `versionSelected=` line of the Qt launcher's `qt_ui.ini` first,
    then the entries of `versions.json` in order, each through `resolve` (a path to the
    AppImage when it is, or holds, an executable one; None otherwise). Test fixtures in
    tests/test_shadps4_launch.py run both on the same files to keep them in step.
    """
    candidates: list[str] = []
    if ini_text is not None and (match := _SELECTED.search(ini_text)):
        candidates.append(match.group(1))
    if versions_text is not None:
        candidates += _VERSION_PATH.findall(versions_text)
    for raw in candidates:
        entry = raw.removesuffix("\r").removeprefix('"').removesuffix('"')
        if entry and (found := resolve(entry)):
            return found
    return None


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
