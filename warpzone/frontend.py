"""Pure rules for a device whose frontend and RetroArch are pointed at the library.

A profile with `[es_systems]` gets a copy of the frontend's system list in which the ROM
folders are moved to the library; a profile with `[retroarch]` gets a few keys of
`retroarch.cfg` pinned. No I/O here: `provision device` reads and writes the files and
`doctor` reads them. See development/design/profiles.md, "Frontend and RetroArch overrides".
"""

from __future__ import annotations

import html
import re
import shlex
from collections.abc import Callable, Collection, Mapping
from dataclasses import dataclass
from pathlib import Path

from warpzone import settings_formats
from warpzone.settings_formats import Value

LIBRARY_PLACEHOLDER = "{library}"


def rewrite_system_paths(
    text: str, prefix: str, target: str, renames: Mapping[str, str] | None = None
) -> str:
    """`text` with every `<path>` element that starts with `prefix` moved below `target`.

    Only the start of the element's text changes, so every other byte is kept, line endings
    included. An element that is already below `target` is left alone, which makes the rule
    safe to apply to its own output (`/roms/` and `/roms/library/roms/` overlap). `renames`
    maps a system folder of the list to the name the hub uses for it.
    """
    pattern = re.compile(r"(<path>\s*)" + re.escape(prefix))

    def move(match: re.Match[str]) -> str:
        rest = text[match.end() :]
        if (prefix + rest).startswith(target):
            return match.group(0)
        return match.group(1) + target

    moved = pattern.sub(move, text)
    for old, new in (renames or {}).items():
        moved = moved.replace(f"{target}{old}/", f"{target}{new}/")
    return moved


def systems_current(
    source: str,
    override: str | None,
    prefix: str,
    target: str,
    renames: Mapping[str, str] | None = None,
    unwrap: Collection[str] = (),
) -> bool:
    """Whether `override` is exactly what the rule makes of `source`.

    The commands of the systems in `unwrap` are compared without the save conversion wrapper
    (`wrap_systems`), so a copy that has one is still current.
    """
    return override is not None and unwrap_systems(override, unwrap) == rewrite_system_paths(
        source, prefix, target, renames
    )


# ------------------------------------------------------------------ save conversion wrapper

# A run of shell words with no `;`, except inside an XML entity.
_WORDS = r"(?:[^;&]|&(?:amp|lt|gt|quot|apos);)*?"
_CONVERT = rf"{_WORDS} -m warpzone saves convert [A-Za-z0-9_-]+ --quiet{_WORDS}\|\| true"
_WRAPPED = re.compile(rf"\A(\s*){_CONVERT}; (.*); {_CONVERT}(\s*)\Z", re.DOTALL)
_SYSTEM = re.compile(r"<system>.*?</system>", re.DOTALL)
_NAME = re.compile(r"<name>\s*([^<]*?)\s*</name>")
_COMMAND = re.compile(r"(<command(?:\s[^>]*)?>)(.*?)(</command>)", re.DOTALL)


def convert_command(python: str, device: str, config: Path | None = None) -> str:
    """The shell command that reconciles a device's saves quietly and never fails its caller."""
    words = [shlex.quote(python), "-m", "warpzone", "saves", "convert", shlex.quote(device)]
    words.append("--quiet")
    if config is not None:
        words += ["--config", shlex.quote(str(config))]
    return " ".join(words) + " || true"


def _unwrap_one(inner: str) -> str:
    match = _WRAPPED.match(inner)
    return f"{match.group(1)}{match.group(2)}{match.group(3)}" if match else inner


def _wrap_one(inner: str, command: str) -> str:
    inner = _unwrap_one(inner)
    lead = inner[: len(inner) - len(inner.lstrip())]
    trail = inner[len(inner.rstrip()) :]
    text = html.escape(command, quote=False)
    return f"{lead}{text}; {inner.strip()}; {text}{trail}"


def _map_commands(text: str, systems: Collection[str], change: Callable[[str], str]) -> str:
    def block(match: re.Match[str]) -> str:
        name = _NAME.search(match.group(0))
        if name is None or name.group(1) not in systems:
            return match.group(0)
        return _COMMAND.sub(lambda c: c.group(1) + change(c.group(2)) + c.group(3), match.group(0))

    return _SYSTEM.sub(block, text)


def wrap_systems(text: str, systems: Collection[str], command: str) -> str:
    """`text` with the `<command>` of each system in `systems` run between two `command`s.

    The system's own command keeps its bytes. Applying it again changes nothing.
    """
    return _map_commands(text, systems, lambda inner: _wrap_one(inner, command))


def unwrap_systems(text: str, systems: Collection[str]) -> str:
    """`text` without the wrapper `wrap_systems` puts around the commands of `systems`."""
    return _map_commands(text, systems, _unwrap_one) if systems else text


def wrapped_systems(text: str, systems: Collection[str]) -> set[str]:
    """The systems of `systems` in `text` whose every `<command>` has the wrapper."""
    found: dict[str, bool] = {}

    def block(match: re.Match[str]) -> str:
        name = _NAME.search(match.group(0))
        if name is not None and name.group(1) in systems:
            commands = _COMMAND.findall(match.group(0))
            found[name.group(1)] = bool(commands) and all(_WRAPPED.match(c[1]) for c in commands)
        return match.group(0)

    _SYSTEM.sub(block, text)
    return {name for name, ok in found.items() if ok}


@dataclass(frozen=True)
class RetroArchGap:
    # Key -> its value now (None when the key is absent) for every key that is not as wanted.
    differs: dict[str, Value | None]
    # Wanted keys that appear twice or are malformed: they cannot be edited safely.
    unwritable: frozenset[str]


def retroarch_gap(text: str, wants: Mapping[str, str]) -> RetroArchGap:
    read = settings_formats.handler("retroarch-cfg").read(text)
    unwritable = frozenset(k for k in wants if k in read.fixed)
    differs = {
        key: read.values.get(key)
        for key, value in wants.items()
        if key not in unwritable and read.values.get(key) != value
    }
    return RetroArchGap(differs, unwritable)


def retroarch_apply(text: str, wants: Mapping[str, str]) -> str:
    """`text` with the wanted keys set; only those values change."""
    gap = retroarch_gap(text, wants)
    changes = {key: wants[key] for key in gap.differs}
    return settings_formats.handler("retroarch-cfg").write(text, changes)
