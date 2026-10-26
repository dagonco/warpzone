"""Ignore-pattern rendering. Pure: no I/O.

Whitelisted folders (library, songs) read their patterns from files that live
inside the synced folder on the hub. Every other folder gets the same fixed
lines inline, so it needs no file at all.
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


def hub_whitelist() -> tuple[str, ...]:
    return (include(COMMON_FILE),)


def device_whitelist(device: str) -> tuple[str, ...]:
    return (include(device_file(device)),)


def inline(extra: tuple[str, ...] = ()) -> tuple[str, ...]:
    return COMMON_LINES + extra
