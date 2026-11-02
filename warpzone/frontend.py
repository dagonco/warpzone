"""Pure rules for a device whose frontend and RetroArch are pointed at the library.

A profile with `[es_systems]` gets a copy of the frontend's system list in which the ROM
folders are moved to the library; a profile with `[retroarch]` gets a few keys of
`retroarch.cfg` pinned. No I/O here: `provision device` reads and writes the files and
`doctor` reads them. See docs/design.md, "Frontend and RetroArch overrides".
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass

from warpzone import settings_formats

LIBRARY_PLACEHOLDER = "{library}"


def rewrite_system_paths(text: str, prefix: str, target: str) -> str:
    """`text` with every `<path>` element that starts with `prefix` moved below `target`.

    Only the start of the element's text changes, so every other byte is kept, line endings
    included. An element that is already below `target` is left alone, which makes the rule
    safe to apply to its own output (`/roms/` and `/roms/library/roms/` overlap).
    """
    pattern = re.compile(r"(<path>\s*)" + re.escape(prefix))

    def move(match: re.Match[str]) -> str:
        rest = text[match.end() :]
        if (prefix + rest).startswith(target):
            return match.group(0)
        return match.group(1) + target

    return pattern.sub(move, text)


def systems_current(source: str, override: str | None, prefix: str, target: str) -> bool:
    """Whether `override` is exactly what the rule makes of `source`."""
    return override is not None and override == rewrite_system_paths(source, prefix, target)


@dataclass(frozen=True)
class RetroArchGap:
    # Key -> its value now (None when the key is absent) for every key that is not as wanted.
    differs: dict[str, str | None]
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
