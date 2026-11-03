"""Order of warpzone versions and release tags (pure)."""

from __future__ import annotations

import re

_VERSION = re.compile(r"(\d+)\.(\d+)\.(\d+)(?:(rc)(\d+)|(.+))?", re.ASCII)

# The last two numbers of a version key: the stage and the pre-release number. Any other
# suffix sorts before every `rcN`; a plain release sorts after all of them.
_STAGE_OTHER, _STAGE_RC, _STAGE_RELEASE = 0, 1, 2

VersionKey = tuple[int, int, int, int, int]


def version_key(version: str) -> VersionKey | None:
    """A sortable key for a package version: `0.3.0rc1` < `0.3.0rc2` < `0.3.0`. Only the
    PEP 440 spelling `rcN` is ordered; any other suffix sorts before every `rcN`."""
    match = _VERSION.fullmatch(version)
    if match is None:
        return None
    major, minor, patch = (int(n) for n in match.groups()[:3])
    if match.group(4):
        return major, minor, patch, _STAGE_RC, int(match.group(5))
    if match.group(6):
        return major, minor, patch, _STAGE_OTHER, 0
    return major, minor, patch, _STAGE_RELEASE, 0


def tag_key(tag: str) -> VersionKey | None:
    """The sort key of a release tag, `vX.Y.Z` or `vX.Y.ZrcN` (the tag is `v` plus
    `__version__`, see docs/releasing.md); None for any other spelling, such as `v0.3.0-rc1`."""
    if not tag.startswith("v") or _VERSION.fullmatch(tag[1:]) is None:
        return None
    key = version_key(tag[1:])
    return key if key is not None and key[3] != _STAGE_OTHER else None
