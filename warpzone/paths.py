"""Path helpers shared by the commands (pure)."""

from __future__ import annotations

from pathlib import Path


def expand_home(path: str, home: Path) -> str:
    """`path` with a leading `~` or `~/` replaced by `home`; any other path is returned as is.

    `home` is a parameter, never `Path.home()`, so the paths of a console can be worked out
    anywhere and tests need no real home folder.
    """
    if path == "~":
        return str(home)
    if path.startswith("~/"):
        return str(home / path[2:])
    return path
