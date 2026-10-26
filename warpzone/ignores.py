"""Ignore-pattern rendering. Pure: no I/O.

Whitelisted folders (library, songs) read their patterns from files that live
inside the synced folder on the hub (the library adds fixed exclusions ahead of
them). Every other folder gets the same fixed lines inline, so it needs no file at all.
"""

COMMON_FILE = "patterns-common.stignore"

# Mirrors templates/patterns-common.stignore (a test keeps them in sync).
COMMON_LINES: tuple[str, ...] = (
    "(?d)@eaDir",
    "(?d).DS_Store",
    "(?d)._*",
    "(?d)Thumbs.db",
)

# Only whitelist files may arrive while a fresh device bootstraps.
BOOTSTRAP_LINES: tuple[str, ...] = ("!/patterns-*.stignore", "*")


def device_file(device: str) -> str:
    return f"patterns-{device}.stignore"


def include(filename: str) -> str:
    return f"#include {filename}"


def hub_whitelist(excluded: tuple[str, ...] = ()) -> tuple[str, ...]:
    """The hub library's lines. `excluded` paths go first: the first match wins."""
    return (*excluded, include(COMMON_FILE))


def device_whitelist(device: str, excluded: tuple[str, ...] = ()) -> tuple[str, ...]:
    """The device library's lines: what the device never receives, then its whitelist.

    The whitelist lives on the hub and ends with `*`, so anything that must stay
    out regardless of it (per-device data, EmuDeck symlinks, files delivered by
    another folder) is listed before the include.
    """
    return (*excluded, include(device_file(device)))


def inline(extra: tuple[str, ...] = ()) -> tuple[str, ...]:
    return COMMON_LINES + extra
