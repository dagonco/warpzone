"""Profile loading and validation. Profiles are TOML files shipped in the package."""

from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass, field
from importlib import resources
from pathlib import Path
from typing import Any

PATH_KEYS = ("library", "songs", "tools", "media", "esde")
_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")
# `<emu>-<kind>`: both halves are single words so the hub path stays unambiguous.
_SAVE_KEY_RE = re.compile(r"^([a-z0-9]+)-([a-z0-9]+)$")


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
class Profile:
    name: str
    description: str
    paths: dict[str, str] = field(default_factory=dict)
    saves: dict[str, SaveFolder] = field(default_factory=dict)


def parse_profile(data: dict[str, Any], source: str) -> Profile:
    errors: list[str] = []

    def err(msg: str) -> None:
        errors.append(f"{source}: {msg}")

    allowed = {"name", "description", "paths", "saves"}
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

    if errors:
        raise ProfileError("\n".join(errors))
    return Profile(name, description, paths, saves)


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
    root = resources.files("warpzone") / "profiles"
    return sorted(p.name.removesuffix(".toml") for p in root.iterdir() if p.name.endswith(".toml"))


def load_profile(name: str) -> Profile:
    if not _NAME_RE.match(name):
        raise ProfileError(f"invalid profile name {name!r}")
    resource = resources.files("warpzone") / "profiles" / f"{name}.toml"
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
