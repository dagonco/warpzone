"""Profile loading and validation. Profiles are TOML files shipped in the package."""

from __future__ import annotations

import re
import tomllib
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from importlib import resources
from pathlib import Path
from typing import Any, TypeGuard

from warpzone.errors import UsageError
from warpzone.shadps4 import kind_of

PATH_KEYS = ("library", "songs", "tools", "media", "esde", "settings", "play_state")
# The handlers of the settings layer (`settings_formats.handler`); keep in step.
SETTINGS_FORMATS = ("ini", "qt-ini", "retroarch-cfg", "yaml-flat", "json", "toml")
_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")
# `<emu>-<kind>`: both halves are single words so the hub path stays unambiguous.
_SAVE_KEY_RE = re.compile(r"^([a-z0-9]+)-([a-z0-9]+)$")
# A path below the hub's library folder: plain names only, so it can be written as
# an ignore line (`/<path>`) without any pattern character in it.
_HUB_PATH_RE = re.compile(r"^[A-Za-z0-9._-]+(/[A-Za-z0-9._-]+)*$")
# Emulator, file and data ids of the settings layer.
_ID_RE = re.compile(r"^[a-z0-9-]+$")
# The converters of `warpzone.artwork.FORMATS` and the hub's artwork folders; keep in step.
ARTWORK_FORMATS = ("emulationstation",)
ARTWORK_KINDS = ("covers", "screenshots", "miximages", "marquees", "3dboxes", "physicalmedia")
# Which systems get artwork: `library` is every system folder below the library's `roms/`.
ARTWORK_SYSTEMS = ("library",)
# A systemd timer interval: a number and `min` or `h`.
_INTERVAL_RE = re.compile(r"^[1-9][0-9]*(min|h)$")


class ProfileError(UsageError):
    pass


@dataclass(frozen=True)
class SaveFolder:
    key: str
    emu: str
    kind: str
    path: str
    ignore: tuple[str, ...] = ()
    # Create `path` when it is missing and this directory exists (the emulator is
    # installed but has not written its save folder yet). Without it a missing `path`
    # is skipped.
    create_when: str | None = None


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
class SettingsFile:
    """One settings file of an emulator, or a template for several (`{name}` in the path)."""

    id: str
    path: str
    format: str
    create: bool = False
    noise: tuple[str, ...] = ()
    secret: tuple[str, ...] = ()
    device: tuple[str, ...] = ()
    known: tuple[str, ...] = ()

    @property
    def templated(self) -> bool:
        return "{name}" in self.path


@dataclass(frozen=True)
class DataFile:
    """A system data file synced whole, the same on every device."""

    id: str
    path: str
    create_when: str | None = None


@dataclass(frozen=True)
class EmulatorSettings:
    id: str
    process: tuple[str, ...]
    files: dict[str, SettingsFile] = field(default_factory=dict)
    data: dict[str, DataFile] = field(default_factory=dict)
    # True when a path or process name is a best guess not confirmed on a device.
    verify: bool = False


@dataclass(frozen=True)
class EsSystems:
    """The frontend's system list, copied with the ROM folders moved into the library.

    `source` is the list that ships with the OS and `override` the copy the frontend
    reads first. Every `<path>` element that starts with `prefix` moves below the
    profile's library folder (`<library>/roms/`). `process` names the frontend: the copy
    is written only while it is not running.
    """

    source: str
    override: str
    prefix: str
    process: tuple[str, ...]
    renames: Mapping[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class RetroArchPins:
    """Keys of `retroarch.cfg` (one file per build) that `provision device` sets.

    A value may contain `{library}`, replaced by the profile's library path. `process`
    names RetroArch: a file is edited only while none of them runs.
    """

    cfg: tuple[str, ...]
    process: tuple[str, ...]
    set: dict[str, str]

    def wants(self, library: str) -> dict[str, str]:
        return {k: v.replace("{library}", library) for k, v in self.set.items()}


@dataclass(frozen=True)
class CemuPaths:
    """Cemu's MLC folder and game path, which `provision device` sets in its `settings.xml`.

    `mlc` is where Cemu keeps installs and saves (the profile syncs its `usr/save`), and
    `default_mlc` the folder Cemu uses while `mlc_path` is empty. `game_path` is added to the
    game paths, keeping the ones already there. `mlc` and `game_path` may contain `{library}`,
    replaced by the profile's library path. `process` names Cemu: the file is edited only
    while none of them runs. See development/design/profiles.md, "Cemu paths".
    """

    settings: str
    process: tuple[str, ...]
    mlc: str
    default_mlc: str
    game_path: str

    def wants(self, library: str) -> tuple[str, str]:
        return self.mlc.replace("{library}", library), self.game_path.replace("{library}", library)


@dataclass(frozen=True)
class ShadPS4Launch:
    """A launch script that starts shadPS4's emulator directly, and the rule that runs it.

    `provision device` writes the bash script to `script` (read `qt_ini` for the version
    selected in the Qt launcher, `versions` for the installed ones) and points the staticpath
    entry of `emulator` in ES-DE's `find_rules` at it.
    """

    qt_ini: str
    versions: str
    script: str
    find_rules: str
    emulator: str


@dataclass(frozen=True)
class ShadPS4Dirs:
    """Install directories that `provision device` adds to shadPS4's config files.

    shadPS4 runs the extracted games it finds in its install directories. `config` lists the
    files that can hold them (`config.json` of v0.21 and later, the older `config.toml`),
    each edited only if it exists; existing entries are kept. A directory may contain
    `{library}`, replaced by the profile's library path. `process` names shadPS4 and its
    launcher: a file is edited only while none of them runs.
    """

    config: tuple[str, ...]
    process: tuple[str, ...]
    install_dirs: tuple[str, ...]
    launch: ShadPS4Launch | None = None

    def wants(self, library: str) -> list[str]:
        return [d.replace("{library}", library) for d in self.install_dirs]


@dataclass(frozen=True)
class TitleLinks:
    """One symlink per game folder of the library, inside an emulator's own game folder.

    `source` holds a folder per title (`{library}` is replaced by the profile's library
    path); `target` is where the emulator looks for them. A title is a folder of `source`
    whose name fully matches `match` and that holds every file of `require`. `process` names
    the emulator: nothing is changed while it runs. `noun` is what a title is called in
    messages (`game`, `license`). With `create_target`, a missing `target` folder is made
    when its parent exists (the emulator has run, but does not make this folder until it
    needs it). See development/design/title-links.md, "Title links".
    """

    source: str
    target: str
    process: tuple[str, ...]
    match: str = ".+"
    require: tuple[str, ...] = ()
    noun: str = "game"
    create_target: bool = False

    def source_path(self, library: str) -> str:
        return self.source.replace("{library}", library)

    def target_path(self, library: str) -> str:
        return self.target.replace("{library}", library)


@dataclass(frozen=True)
class Artwork:
    """Scraped artwork and metadata for a frontend that is not ES-DE.

    The hub's `wz-media` and `wz-esde` folders arrive receive-only at `media` and
    `metadata`, limited to `kinds` of the systems selected by `systems`. A converter named
    by `format` turns the ES-DE gamelists there into the frontend's own gamelists, written
    below `gamelists`. With `refresh`, `provision device` installs a user timer that runs
    `warpzone frontend refresh` at that interval (systemd syntax, like `15min`).
    """

    format: str
    kinds: tuple[str, ...]
    media: str
    metadata: str
    gamelists: str
    systems: str = "library"
    refresh: str | None = None


@dataclass(frozen=True)
class PlayState:
    """`[play_state]`: with `refresh`, `provision device` installs a user timer that publishes
    this device's favourites and play statistics while the frontend stays closed or open.
    A device with `[artwork]` needs none: its `frontend refresh` timer does the same.
    """

    refresh: str | None = None


@dataclass(frozen=True)
class OptionsMenu:
    """`[options_menu]`: the folder whose executable `.sh` files the device's Options menu
    lists (ArkOS: `/opt/system`). `provision device` adds its "Warpzone" entries there.
    """

    dir: str


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
    settings: dict[str, EmulatorSettings] = field(default_factory=dict)
    # The device's folders sit on a filesystem without POSIX permissions (exFAT): its
    # Syncthing folders ignore permissions.
    ignore_perms: bool = False
    es_systems: EsSystems | None = None
    retroarch: RetroArchPins | None = None
    artwork: Artwork | None = None
    play_state: PlayState | None = None
    shadps4: ShadPS4Dirs | None = None
    links: dict[str, TitleLinks] = field(default_factory=dict)
    options_menu: OptionsMenu | None = None
    # `[updater] install`: "auto" (the timer installs a new release) or "manual" (the timer
    # only looks for one; it is installed by hand, with `warpzone self-update --now`).
    updater_install: str = "auto"
    cemu: CemuPaths | None = None

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

    allowed = {
        "name",
        "description",
        "ignore_perms",
        "paths",
        "saves",
        "library",
        "bios",
        "settings",
        "es_systems",
        "retroarch",
        "artwork",
        "play_state",
        "shadps4",
        "cemu",
        "links",
        "options_menu",
        "updater",
    }
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

    ignore_perms = data.get("ignore_perms", False)
    if not isinstance(ignore_perms, bool):
        err("`ignore_perms` must be true or false")
        ignore_perms = False

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
        for extra in sorted(set(entry) - {"path", "ignore", "create_when"}):
            err(f"saves.{key}: unknown key {extra!r}")
        path = entry.get("path")
        if not isinstance(path, str) or not path:
            err(f"saves.{key}.path: must be a non-empty string")
            continue
        ignore = entry.get("ignore", [])
        if not isinstance(ignore, list) or not all(isinstance(i, str) for i in ignore):
            err(f"saves.{key}.ignore: must be a list of strings")
            continue
        create_when = entry.get("create_when")
        if create_when is not None and (not isinstance(create_when, str) or not create_when):
            err(f"saves.{key}.create_when: must be a non-empty string")
            continue
        saves[key] = SaveFolder(
            key, match.group(1), match.group(2), path, tuple(ignore), create_when
        )

    library_ignore = _parse_library(data.get("library", {}), err)
    bios = _parse_bios(data.get("bios", {}), err)
    settings = _parse_settings(data.get("settings", {}), err)
    es_systems = _parse_es_systems(data.get("es_systems"), "library" in paths, err)
    retroarch = _parse_retroarch(data.get("retroarch"), "library" in paths, err)
    artwork = _parse_artwork(data.get("artwork"), paths, es_systems is not None, err)
    play_state = _parse_play_state(data.get("play_state"), paths, artwork, err)
    shadps4 = _parse_shadps4(data.get("shadps4"), "library" in paths, err)
    cemu = _parse_cemu(data.get("cemu"), "library" in paths, err)
    links = _parse_links(data.get("links"), "library" in paths, err)
    options_menu = _parse_options_menu(data.get("options_menu"), err)
    updater_install = _parse_updater(data.get("updater"), err)

    if errors:
        raise ProfileError("\n".join(errors))
    return Profile(
        name,
        description,
        paths,
        saves,
        library_ignore,
        bios,
        settings,
        ignore_perms,
        es_systems,
        retroarch,
        artwork,
        play_state,
        shadps4,
        links,
        options_menu,
        updater_install,
        cemu,
    )


def _names(raw: Any) -> TypeGuard[list[str]]:
    return isinstance(raw, list) and bool(raw) and all(isinstance(p, str) and p for p in raw)


def _parse_es_systems(raw: Any, has_library: bool, err: Callable[[str], None]) -> EsSystems | None:
    if raw is None:
        return None
    if not isinstance(raw, dict):
        err("`es_systems` must be a table")
        return None
    extras = sorted(set(raw) - {"source", "override", "prefix", "process", "rename"})
    for extra in extras:
        err(f"es_systems.{extra}: unknown key")
    problems = len(extras)
    for key in ("source", "override"):
        if not isinstance(raw.get(key), str) or not raw.get(key):
            err(f"es_systems.{key}: must be a non-empty string")
            problems += 1
    prefix = raw.get("prefix")
    if not isinstance(prefix, str) or not prefix.startswith("/") or not prefix.endswith("/"):
        err("es_systems.prefix: must be an absolute folder starting and ending with `/`")
        problems += 1
    if not _names(raw.get("process")):
        err("es_systems.process: required, a non-empty list of non-empty strings")
        problems += 1
    if not has_library:
        err("es_systems needs paths.library (the ROM folders move below it)")
        problems += 1
    if problems:
        return None
    renames = raw.get("rename", {})
    if not isinstance(renames, dict) or not all(
        isinstance(k, str) and k and isinstance(v, str) and v for k, v in renames.items()
    ):
        err("es_systems.rename: must map folder names to folder names")
        return None
    return EsSystems(
        raw["source"], raw["override"], raw["prefix"], tuple(raw["process"]), dict(renames)
    )


def _nested(a: str, b: str) -> bool:
    """Whether folder `a` is `b` or sits inside it (literal paths, `~` unexpanded)."""
    a, b = a.rstrip("/") + "/", b.rstrip("/") + "/"
    return a.startswith(b)


UPDATER_INSTALL = ("auto", "manual")


def _parse_updater(raw: Any, err: Callable[[str], None]) -> str:
    if raw is None:
        return "auto"
    if not isinstance(raw, dict):
        err("`updater` must be a table")
        return "auto"
    for extra in sorted(set(raw) - {"install"}):
        err(f"updater.{extra}: unknown key")
    install = raw.get("install", "auto")
    if install not in UPDATER_INSTALL:
        err('updater.install: "auto" or "manual"')
        return "auto"
    return str(install)


def _parse_options_menu(raw: Any, err: Callable[[str], None]) -> OptionsMenu | None:
    if raw is None:
        return None
    if not isinstance(raw, dict):
        err("`options_menu` must be a table")
        return None
    for extra in sorted(set(raw) - {"dir"}):
        err(f"options_menu.{extra}: unknown key")
    folder = raw.get("dir")
    if not isinstance(folder, str) or not (folder.startswith("/") or folder.startswith("~/")):
        err("options_menu.dir: required, an absolute folder (or one starting with `~/`)")
        return None
    return OptionsMenu(folder)


def _parse_play_state(
    raw: Any, paths: dict[str, str], artwork: Artwork | None, err: Callable[[str], None]
) -> PlayState | None:
    folder = paths.get("play_state")
    if folder is not None:
        if "esde" not in paths and artwork is None:
            err("paths.play_state needs paths.esde or [artwork] (a gamelist to read and write)")
        if "library" in paths and (
            _nested(folder, paths["library"]) or _nested(paths["library"], folder)
        ):
            err("paths.play_state: must not be the library folder or sit inside it")
    if raw is None:
        return None
    if not isinstance(raw, dict):
        err("`play_state` must be a table")
        return None
    for extra in sorted(set(raw) - {"refresh"}):
        err(f"play_state.{extra}: unknown key")
    if folder is None:
        err("[play_state] needs paths.play_state")
    if artwork is not None:
        err("[play_state] is not for a profile with [artwork]: artwork.refresh does this")
    refresh = raw.get("refresh")
    if refresh is not None and (not isinstance(refresh, str) or not _INTERVAL_RE.match(refresh)):
        err("play_state.refresh: must be an interval like 15min or 6h")
        return None
    return PlayState(refresh)


def _parse_artwork(
    raw: Any, paths: dict[str, str], has_es_systems: bool, err: Callable[[str], None]
) -> Artwork | None:
    if raw is None:
        return None
    if not isinstance(raw, dict):
        err("`artwork` must be a table")
        return None
    known = {"format", "kinds", "systems", "media", "metadata", "gamelists", "refresh"}
    problems = 0
    for extra in sorted(set(raw) - known):
        err(f"artwork.{extra}: unknown key")
        problems += 1
    fmt = raw.get("format")
    if fmt not in ARTWORK_FORMATS:
        err(f"artwork.format: must be one of {', '.join(ARTWORK_FORMATS)}")
        problems += 1
    kinds = raw.get("kinds")
    if (
        not isinstance(kinds, list)
        or not kinds
        or len(set(map(str, kinds))) != len(kinds)
        or not all(k in ARTWORK_KINDS for k in kinds)
    ):
        err(
            "artwork.kinds: required, a non-empty list without repeats of "
            f"{', '.join(ARTWORK_KINDS)}"
        )
        problems += 1
    systems = raw.get("systems", "library")
    if systems not in ARTWORK_SYSTEMS:
        err(f"artwork.systems: must be one of {', '.join(ARTWORK_SYSTEMS)}")
        problems += 1
    folders: dict[str, str] = {}
    for key in ("media", "metadata", "gamelists"):
        value = raw.get(key)
        if not isinstance(value, str) or not value:
            err(f"artwork.{key}: must be a non-empty string")
            problems += 1
        else:
            folders[key] = value
    refresh = raw.get("refresh")
    if refresh is not None and (not isinstance(refresh, str) or not _INTERVAL_RE.match(refresh)):
        err("artwork.refresh: must be an interval like 15min or 6h")
        problems += 1
    if "library" not in paths:
        err("artwork needs paths.library (the ROMs it decorates are there)")
        problems += 1
    if fmt == "emulationstation" and not has_es_systems:
        err(
            "artwork.format emulationstation needs [es_systems] (the system list names the systems)"
        )
        problems += 1
    for key in ("media", "esde"):
        if key in paths:
            err(f"artwork replaces paths.{key}: remove it from `paths`")
            problems += 1
    if len(folders) == 3 and "library" in paths:
        mine = [("media", folders["media"]), ("metadata", folders["metadata"])]
        for key, value in mine:
            if _nested(value, paths["library"]) or _nested(paths["library"], value):
                err(f"artwork.{key}: must not be the library folder or sit inside it")
                problems += 1
        if _nested(mine[0][1], mine[1][1]) or _nested(mine[1][1], mine[0][1]):
            err("artwork.media and artwork.metadata must be separate folders")
            problems += 1
    if problems:
        return None
    return Artwork(
        raw["format"],
        tuple(raw["kinds"]),
        folders["media"],
        folders["metadata"],
        folders["gamelists"],
        systems,
        refresh,
    )


def _parse_retroarch(
    raw: Any, has_library: bool, err: Callable[[str], None]
) -> RetroArchPins | None:
    if raw is None:
        return None
    if not isinstance(raw, dict):
        err("`retroarch` must be a table")
        return None
    extras = sorted(set(raw) - {"cfg", "process", "set"})
    for extra in extras:
        err(f"retroarch.{extra}: unknown key")
    problems = len(extras)
    if not _names(raw.get("cfg")):
        err("retroarch.cfg: required, a non-empty list of non-empty strings")
        problems += 1
    if not _names(raw.get("process")):
        err("retroarch.process: required, a non-empty list of non-empty strings")
        problems += 1
    values = raw.get("set")
    if not isinstance(values, dict) or not values:
        err("retroarch.set: required, a non-empty table of key = value strings")
        return None
    for key, value in values.items():
        bad = not isinstance(value, str) or "{" in value.replace("{library}", "")
        if bad or '"' in str(value):
            err(f"retroarch.set.{key}: must be a string; {{library}} is the only placeholder")
            problems += 1
        elif "{library}" in value and not has_library:
            err(f"retroarch.set.{key}: uses {{library}} but there is no paths.library")
            problems += 1
    if problems:
        return None
    return RetroArchPins(tuple(raw["cfg"]), tuple(raw["process"]), dict(values))


def _parse_shadps4(raw: Any, has_library: bool, err: Callable[[str], None]) -> ShadPS4Dirs | None:
    if raw is None:
        return None
    if not isinstance(raw, dict):
        err("`shadps4` must be a table")
        return None
    extras = sorted(set(raw) - {"config", "process", "install_dirs", "launch"})
    for extra in extras:
        err(f"shadps4.{extra}: unknown key")
    problems = len(extras)
    config: Any = raw.get("config")
    if not _names(config) or not all(kind_of(c) for c in config):
        err("shadps4.config: required, a non-empty list of paths ending in .json or .toml")
        problems += 1
    if not _names(raw.get("process")):
        err("shadps4.process: required, a non-empty list of non-empty strings")
        problems += 1
    dirs = raw.get("install_dirs")
    if not _names(dirs):
        err("shadps4.install_dirs: required, a non-empty list of non-empty strings")
        return None
    for d in dirs:
        stripped = d.replace("{library}", "")
        if "{" in stripped or '"' in d or not d.startswith(("/", "~", "{library}")):
            err(
                f"shadps4.install_dirs: {d!r} must be an absolute path; "
                "{library} is the only placeholder"
            )
            problems += 1
        elif "{library}" in d and not has_library:
            err(f"shadps4.install_dirs: {d!r} uses {{library}} but there is no paths.library")
            problems += 1
    launch = None
    if "launch" in raw:
        launch = _parse_shadps4_launch(raw["launch"], err)
        problems += launch is None
    if problems:
        return None
    return ShadPS4Dirs(tuple(config), tuple(raw["process"]), tuple(dirs), launch)


_LAUNCH_KEYS = ("qt_ini", "versions", "script", "find_rules", "emulator")


def _parse_shadps4_launch(raw: Any, err: Callable[[str], None]) -> ShadPS4Launch | None:
    if not isinstance(raw, dict):
        err("`shadps4.launch` must be a table")
        return None
    problems = 0
    for extra in sorted(set(raw) - set(_LAUNCH_KEYS)):
        err(f"shadps4.launch.{extra}: unknown key")
        problems += 1
    for key in _LAUNCH_KEYS:
        value = raw.get(key)
        if not isinstance(value, str) or not value or '"' in value or "\n" in value:
            err(f"shadps4.launch.{key}: required, a non-empty string")
            problems += 1
        elif key != "emulator" and not value.startswith(("/", "~/")):
            err(f"shadps4.launch.{key}: {value!r} must be an absolute path or start with ~/")
            problems += 1
    if problems:
        return None
    return ShadPS4Launch(*(raw[k] for k in _LAUNCH_KEYS))


_CEMU_PATHS = ("settings", "mlc", "default_mlc", "game_path")


def _parse_cemu(raw: Any, has_library: bool, err: Callable[[str], None]) -> CemuPaths | None:
    if raw is None:
        return None
    if not isinstance(raw, dict):
        err("`cemu` must be a table")
        return None
    extras = sorted(set(raw) - {*_CEMU_PATHS, "process"})
    for extra in extras:
        err(f"cemu.{extra}: unknown key")
    problems = len(extras)
    if not _names(raw.get("process")):
        err("cemu.process: required, a non-empty list of non-empty strings")
        problems += 1
    for key in _CEMU_PATHS:
        value = raw.get(key)
        # only the two paths that follow the library may use it
        library_ok = key in ("mlc", "game_path")
        starts = ("/", "~/", "{library}/") if library_ok else ("/", "~/")
        bare = value.replace("{library}", "") if isinstance(value, str) and library_ok else value
        if (
            not isinstance(value, str)
            or not isinstance(bare, str)
            or any(c in bare for c in '{}"\n')
            or not value.startswith(starts)
        ):
            hint = "; {library} is the only placeholder" if library_ok else ""
            err(f"cemu.{key}: required, an absolute path or one starting with ~/{hint}")
            problems += 1
        elif "{library}" in value and not has_library:
            err(f"cemu.{key}: {value!r} uses {{library}} but there is no paths.library")
            problems += 1
    if problems:
        return None
    return CemuPaths(
        raw["settings"], tuple(raw["process"]), raw["mlc"], raw["default_mlc"], raw["game_path"]
    )


_LINK_KEYS = {"source", "target", "process", "match", "require", "noun", "create_target"}


def _parse_links(raw: Any, has_library: bool, err: Callable[[str], None]) -> dict[str, TitleLinks]:
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        err("`links` must be a table")
        return {}
    found: dict[str, TitleLinks] = {}
    for key, entry in raw.items():
        where = f"links.{key}"
        if not isinstance(entry, dict):
            err(f"{where}: must be a table")
            continue
        problems = 0
        for extra in sorted(set(entry) - _LINK_KEYS):
            err(f"{where}: unknown key {extra!r}")
            problems += 1
        for name in ("source", "target"):
            value = entry.get(name)
            stripped = value.replace("{library}", "") if isinstance(value, str) else ""
            if (
                not isinstance(value, str)
                or "{" in stripped
                or not value.startswith(("/", "~/", "{library}"))
                or '"' in value
                or "\n" in value
            ):
                err(
                    f"{where}.{name}: required, an absolute path or one starting with ~/ "
                    "({library} is the only placeholder)"
                )
                problems += 1
            elif "{library}" in value and not has_library:
                err(f"{where}.{name}: uses {{library}} but there is no paths.library")
                problems += 1
        if not _names(entry.get("process")):
            err(f"{where}.process: required, a non-empty list of non-empty strings")
            problems += 1
        pattern = entry.get("match", ".+")
        try:
            if not isinstance(pattern, str) or not pattern:
                raise re.error("not a string")
            re.compile(pattern)
        except re.error:
            err(f"{where}.match: must be a regular expression")
            problems += 1
        require = entry.get("require", [])
        if not isinstance(require, list) or not all(
            isinstance(r, str) and r and not r.startswith("/") and ".." not in r.split("/")
            for r in require
        ):
            err(f"{where}.require: must be a list of relative file paths")
            problems += 1
        noun = entry.get("noun", "game")
        if not isinstance(noun, str) or not noun.strip() or "\n" in noun:
            err(f"{where}.noun: must be a non-empty string")
            problems += 1
        create = entry.get("create_target", False)
        if not isinstance(create, bool):
            err(f"{where}.create_target: must be true or false")
            problems += 1
        if not problems:
            found[key] = TitleLinks(
                entry["source"],
                entry["target"],
                tuple(entry["process"]),
                pattern,
                tuple(require),
                noun,
                create,
            )
    return found


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
    # An optional `(?d)` prefix lets Syncthing delete the file with its parent folder.
    bad = [i for i in ignore if not (i.removeprefix("(?d)").startswith("/") and i != "/")]
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


def _string_list(raw: Any) -> TypeGuard[list[str]]:
    return isinstance(raw, list) and all(isinstance(i, str) for i in raw)


def _parse_settings(raw: Any, err: Callable[[str], None]) -> dict[str, EmulatorSettings]:
    result: dict[str, EmulatorSettings] = {}
    if not isinstance(raw, dict):
        err("`settings` must be a table")
        return result
    for emu, entry in raw.items():
        where = f"settings.{emu}"
        if not _ID_RE.match(emu):
            err(f"{where}: emulator id must be lowercase letters, digits and hyphens")
            continue
        if not isinstance(entry, dict):
            err(f"{where}: must be a table")
            continue
        for extra in sorted(set(entry) - {"process", "files", "data", "verify"}):
            err(f"{where}: unknown key {extra!r}")
        ok = True
        process: Any = entry.get("process")
        if (
            not isinstance(process, list)
            or not process
            or not all(isinstance(p, str) and p for p in process)
        ):
            err(f"{where}.process: required, a non-empty list of non-empty strings")
            ok = False
        verify = entry.get("verify", False)
        if not isinstance(verify, bool):
            err(f"{where}.verify: must be true or false")
            ok = False
        files, files_ok = _parse_settings_files(entry.get("files", {}), where, err)
        data, data_ok = _parse_settings_data(entry.get("data", {}), where, err)
        if ok and files_ok and data_ok:
            result[emu] = EmulatorSettings(emu, tuple(process), files, data, verify)
    return result


def _parse_settings_files(
    raw: Any, where: str, err: Callable[[str], None]
) -> tuple[dict[str, SettingsFile], bool]:
    files: dict[str, SettingsFile] = {}
    if not isinstance(raw, dict):
        err(f"{where}.files: must be a table")
        return files, False
    all_ok = True
    for fid, entry in raw.items():
        at = f"{where}.files.{fid}"
        if not _ID_RE.match(fid):
            err(f"{at}: file id must be lowercase letters, digits and hyphens")
            all_ok = False
            continue
        if fid == "data":
            # The device state keeps data-file hashes under this name.
            err(f"{at}: file id 'data' is reserved")
            all_ok = False
            continue
        if not isinstance(entry, dict):
            err(f"{at}: must be a table")
            all_ok = False
            continue
        known = {"path", "format", "create", "noise", "secret", "device", "known"}
        for extra in sorted(set(entry) - known):
            err(f"{at}: unknown key {extra!r}")
            all_ok = False
        ok = True
        path: Any = entry.get("path")
        if not isinstance(path, str) or not path:
            err(f"{at}.path: must be a non-empty string")
            ok = False
        else:
            problem = _template_problem(path)
            if problem:
                err(f"{at}.path: {problem}")
                ok = False
        fmt: Any = entry.get("format")
        if fmt not in SETTINGS_FORMATS:
            err(f"{at}.format: must be one of {', '.join(SETTINGS_FORMATS)}")
            ok = False
        create = entry.get("create", False)
        if not isinstance(create, bool):
            err(f"{at}.create: must be true or false")
            ok = False
        lists: dict[str, tuple[str, ...]] = {}
        for name in ("noise", "secret", "device", "known"):
            value = entry.get(name, [])
            if not _string_list(value):
                err(f"{at}.{name}: must be a list of strings")
                ok = False
            else:
                lists[name] = tuple(value)
        if ok:
            files[fid] = SettingsFile(
                fid,
                path,
                fmt,
                create,
                lists["noise"],
                lists["secret"],
                lists["device"],
                lists["known"],
            )
        else:
            all_ok = False
    return files, all_ok


def _template_problem(path: str) -> str | None:
    """Why a settings path is not a valid plain or `{name}` path, else None."""
    rest = path.replace("{name}", "")
    if "{" in rest or "}" in rest:
        return "the only placeholder allowed is {name}"
    if "{name}" in path and "{name}" not in path.rpartition("/")[0]:
        return "{name} must appear in the directory part of the path"
    return None


def _parse_settings_data(
    raw: Any, where: str, err: Callable[[str], None]
) -> tuple[dict[str, DataFile], bool]:
    data: dict[str, DataFile] = {}
    if not isinstance(raw, dict):
        err(f"{where}.data: must be a table")
        return data, False
    all_ok = True
    for did, entry in raw.items():
        at = f"{where}.data.{did}"
        if not _ID_RE.match(did):
            err(f"{at}: data id must be lowercase letters, digits and hyphens")
            all_ok = False
            continue
        if not isinstance(entry, dict):
            err(f"{at}: must be a table")
            all_ok = False
            continue
        for extra in sorted(set(entry) - {"path", "create_when"}):
            err(f"{at}: unknown key {extra!r}")
            all_ok = False
        ok = True
        path: Any = entry.get("path")
        if not isinstance(path, str) or not path:
            err(f"{at}.path: must be a non-empty string")
            ok = False
        elif "{" in path or "}" in path:
            err(f"{at}.path: a data file cannot be templated")
            ok = False
        create_when = entry.get("create_when")
        if create_when is not None and (not isinstance(create_when, str) or not create_when):
            err(f"{at}.create_when: must be a non-empty string")
            ok = False
        if ok:
            data[did] = DataFile(did, path, create_when)
        else:
            all_ok = False
    return data, all_ok


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
