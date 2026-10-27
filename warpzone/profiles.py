"""Profile loading and validation. Profiles are TOML files shipped in the package."""

from __future__ import annotations

import re
import tomllib
from collections.abc import Callable
from dataclasses import dataclass, field
from importlib import resources
from pathlib import Path
from typing import Any

PATH_KEYS = ("library", "songs", "tools", "media", "esde")
_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")
# `<emu>-<kind>`: both halves are single words so the hub path stays unambiguous.
_SAVE_KEY_RE = re.compile(r"^([a-z0-9]+)-([a-z0-9]+)$")
# A path below the hub's library folder: plain names only, so it can be written as
# an ignore line (`/<path>`) without any pattern character in it.
_HUB_PATH_RE = re.compile(r"^[A-Za-z0-9._-]+(/[A-Za-z0-9._-]+)*$")


class ProfileError(Exception):
    pass


@dataclass(frozen=True)
class SaveFolder:
    key: str
    emu: str
    kind: str
    path: str
    ignore: tuple[str, ...] = ()


@dataclass(frozen=True)
class BiosPlacement:
    """A hub folder under `library/` that lands in the emulator's real directory.

    The library cannot deliver these files: EmuDeck puts symlinks at the same
    place in `~/Emulation/bios`, and Syncthing never follows symlinks.
    """

    key: str
    emu: str
    kind: str
    hub: str  # path below the hub's library folder, e.g. `bios/cemu`
    path: str  # the emulator's real directory on the device
    ignore: tuple[str, ...] = ()
    # Create `path` when it is missing and this directory exists (the emulator is
    # installed but never wrote the folder). Without it a missing `path` is skipped.
    create_when: str | None = None
    # True when the target directory is a best guess that was not confirmed on a device.
    verify: bool = False


@dataclass(frozen=True)
class Profile:
    name: str
    description: str
    paths: dict[str, str] = field(default_factory=dict)
    saves: dict[str, SaveFolder] = field(default_factory=dict)
    # Library paths the device never receives from the hub (anchored, e.g. `/bios/x`):
    # per-device data and symlinks that EmuDeck creates.
    library_ignore: tuple[str, ...] = ()
    bios: dict[str, BiosPlacement] = field(default_factory=dict)

    def placement_ignores(self) -> tuple[str, ...]:
        """The anchored library paths that placements deliver instead of the library."""
        return tuple(f"/{p.hub}" for p in self.bios.values())

    def library_excludes(self) -> tuple[str, ...]:
        """Everything the device's library folder must ignore, ahead of the whitelist."""
        return tuple(dict.fromkeys((*self.library_ignore, *self.placement_ignores())))


def parse_profile(data: dict[str, Any], source: str) -> Profile:
    errors: list[str] = []

    def err(msg: str) -> None:
        errors.append(f"{source}: {msg}")

    allowed = {"name", "description", "paths", "saves", "library", "bios"}
    for key in sorted(set(data) - allowed):
        err(f"unknown key {key!r}")

    name = data.get("name")
    if not isinstance(name, str) or not _NAME_RE.match(name):
        err("`name` must be a lowercase word (letters, digits, hyphens)")
        name = ""
    description = data.get("description", "")
    if not isinstance(description, str):
        err("`description` must be a string")
        description = ""

    paths: dict[str, str] = {}
    raw_paths = data.get("paths", {})
    if not isinstance(raw_paths, dict):
        err("`paths` must be a table")
        raw_paths = {}
    for key, value in raw_paths.items():
        if key not in PATH_KEYS:
            err(f"paths.{key}: unknown key (expected one of {', '.join(PATH_KEYS)})")
        elif not isinstance(value, str) or not value:
            err(f"paths.{key}: must be a non-empty string")
        else:
            paths[key] = value

    saves: dict[str, SaveFolder] = {}
    raw_saves = data.get("saves", {})
    if not isinstance(raw_saves, dict):
        err("`saves` must be a table")
        raw_saves = {}
    for key, entry in raw_saves.items():
        match = _SAVE_KEY_RE.match(key)
        if not match:
            err(f"saves.{key}: key must look like <emu>-<kind> (lowercase, one hyphen)")
            continue
        if not isinstance(entry, dict):
            err(f"saves.{key}: must be a table")
            continue
        for extra in sorted(set(entry) - {"path", "ignore"}):
            err(f"saves.{key}: unknown key {extra!r}")
        path = entry.get("path")
        if not isinstance(path, str) or not path:
            err(f"saves.{key}.path: must be a non-empty string")
            continue
        ignore = entry.get("ignore", [])
        if not isinstance(ignore, list) or not all(isinstance(i, str) for i in ignore):
            err(f"saves.{key}.ignore: must be a list of strings")
            continue
        saves[key] = SaveFolder(key, match.group(1), match.group(2), path, tuple(ignore))

    library_ignore = _parse_library(data.get("library", {}), err)
    bios = _parse_bios(data.get("bios", {}), err)

    if errors:
        raise ProfileError("\n".join(errors))
    return Profile(name, description, paths, saves, library_ignore, bios)


def _parse_library(raw: Any, err: Callable[[str], None]) -> tuple[str, ...]:
    if not isinstance(raw, dict):
        err("`library` must be a table")
        return ()
    for extra in sorted(set(raw) - {"ignore"}):
        err(f"library: unknown key {extra!r}")
    ignore = raw.get("ignore", [])
    if not isinstance(ignore, list) or not all(isinstance(i, str) for i in ignore):
        err("library.ignore: must be a list of strings")
        return ()
    bad = [i for i in ignore if not i.startswith("/") or i == "/"]
    for item in bad:
        err(f"library.ignore: {item!r} must be a path below the library starting with `/`")
    return () if bad else tuple(ignore)


def _parse_bios(raw: Any, err: Callable[[str], None]) -> dict[str, BiosPlacement]:
    placements: dict[str, BiosPlacement] = {}
    if not isinstance(raw, dict):
        err("`bios` must be a table")
        return placements
    for key, entry in raw.items():
        match = _SAVE_KEY_RE.match(key)
        if not match:
            err(f"bios.{key}: key must look like <emu>-<kind> (lowercase, one hyphen)")
            continue
        if not isinstance(entry, dict):
            err(f"bios.{key}: must be a table")
            continue
        known = {"hub", "path", "ignore", "create_when", "verify"}
        for extra in sorted(set(entry) - known):
            err(f"bios.{key}: unknown key {extra!r}")
        hub, path = entry.get("hub"), entry.get("path")
        if not isinstance(hub, str) or not _HUB_PATH_RE.match(hub) or ".." in hub.split("/"):
            err(f"bios.{key}.hub: must be a plain relative path below the library, like bios/x")
            continue
        if not isinstance(path, str) or not path:
            err(f"bios.{key}.path: must be a non-empty string")
            continue
        ignore = entry.get("ignore", [])
        if not isinstance(ignore, list) or not all(isinstance(i, str) for i in ignore):
            err(f"bios.{key}.ignore: must be a list of strings")
            continue
        create_when = entry.get("create_when")
        if create_when is not None and (not isinstance(create_when, str) or not create_when):
            err(f"bios.{key}.create_when: must be a non-empty string")
            continue
        verify = entry.get("verify", False)
        if not isinstance(verify, bool):
            err(f"bios.{key}.verify: must be true or false")
            continue
        placements[key] = BiosPlacement(
            key, match.group(1), match.group(2), hub, path, tuple(ignore), create_when, verify
        )
    items = sorted((p.hub, p.key) for p in placements.values())
    for i, (outer, outer_key) in enumerate(items):
        for inner, inner_key in items[i + 1 :]:
            if inner == outer or inner.startswith(outer + "/"):
                err(f"bios.{inner_key}.hub: overlaps bios.{outer_key}.hub")
    return placements


def load_profile_file(path: Path) -> Profile:
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise ProfileError(f"{path}: {exc}") from exc
    profile = parse_profile(data, str(path))
    if profile.name != path.stem:
        raise ProfileError(f"{path}: `name` ({profile.name!r}) must match the file name")
    return profile


def available_profiles() -> list[str]:
    root = resources.files("warpzone") / "data" / "profiles"
    return sorted(p.name.removesuffix(".toml") for p in root.iterdir() if p.name.endswith(".toml"))


def load_profile(name: str) -> Profile:
    if not _NAME_RE.match(name):
        raise ProfileError(f"invalid profile name {name!r}")
    resource = resources.files("warpzone") / "data" / "profiles" / f"{name}.toml"
    if not resource.is_file():
        known = ", ".join(available_profiles())
        raise ProfileError(f"unknown profile {name!r} (available: {known})")
    try:
        data = tomllib.loads(resource.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as exc:
        raise ProfileError(f"profile {name}: {exc}") from exc
    profile = parse_profile(data, f"profile {name}")
    if profile.name != name:
        raise ProfileError(f"profile {name}: `name` ({profile.name!r}) must match the file name")
    return profile
