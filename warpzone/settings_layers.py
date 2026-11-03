"""Layer files of the emulator settings layer: parse, validate, merge, render, edit.

A layer is `common.toml` or `<device>.toml` in the `settings` folder. It holds key-level
values only: `[<emulator>.<file>]` tables, a templated file's instance as `<file>:<name>`.
Everything here is pure: text in, text or values out. Error and warning messages name
keys and ids, never values (a rejected value may be a secret).
"""

from __future__ import annotations

import tomllib
from collections.abc import Iterable
from dataclasses import dataclass, field

from warpzone.errors import WarpzoneError
from warpzone.profiles import Profile
from warpzone.settings_formats import Value, is_builtin_secret, match
from warpzone.tomlw import key as toml_key
from warpzone.tomlw import scalar as toml_scalar

LAYER_FORMAT = 1
HEADER = "# Written by warpzone settings capture. Edit on the hub only; comments are not kept."

# Formats whose values keep their type; the others hold raw strings.
_TYPED_FORMATS = ("json", "toml")
_CONFLICT_MARK = ".sync-conflict-"

EntryKey = tuple[str, str]


class LayerError(WarpzoneError):
    """A layer cannot be used. `errors` lists every problem found."""

    def __init__(self, errors: list[str]) -> None:
        super().__init__("; ".join(errors))
        self.errors = errors


@dataclass(frozen=True)
class Layer:
    # (emulator, file instance id) -> key -> value
    entries: dict[EntryKey, dict[str, Value]] = field(default_factory=dict)


def _is_value(raw: object) -> bool:
    return isinstance(raw, (str, int, float, bool))


def parse_layer(text: str, source: str) -> tuple[Layer, list[str]]:
    """Parse a layer file. Returns (layer, warnings); raises LayerError on any error."""
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise LayerError([f"{source}: not valid TOML ({exc})"]) from exc
    errors: list[str] = []
    warnings: list[str] = []
    fmt = data.pop("format", None)
    if fmt is None:
        errors.append(f"{source}: format is missing (expected format = {LAYER_FORMAT})")
    elif type(fmt) is not int or fmt != LAYER_FORMAT:
        errors.append(f"{source}: format is not supported (expected format = {LAYER_FORMAT})")
    entries: dict[EntryKey, dict[str, Value]] = {}
    for emu, files in data.items():
        if not isinstance(files, dict):
            errors.append(f"{source}: {emu}: expected a table of file tables")
            continue
        for fid, keys in files.items():
            if not isinstance(keys, dict):
                errors.append(f"{source}: {emu}.{fid}: expected a file table, not a value")
                continue
            values: dict[str, Value] = {}
            for name, value in keys.items():
                if _is_value(value):
                    values[name] = value
                else:
                    errors.append(
                        f"{source}: {emu}.{fid}: {name}: expected a string, number or "
                        f"boolean, not {type(value).__name__}"
                    )
            if values:
                entries[(emu, fid)] = values
    if errors:
        raise LayerError(errors)
    return Layer(entries), warnings


def _instance_problem(fid: str, templated: bool) -> str | None:
    """What is wrong with a file id of a layer, given whether its declared file is templated."""
    _, sep, name = fid.partition(":")
    if not templated:
        return "is not a templated file, so it has no instances" if sep else None
    if not sep:
        return "is a templated file: name the instance as <file>:<name>"
    if not name or "/" in name or "\0" in name or name in (".", ".."):
        return "has an instance name that is empty or not a plain file name"
    return None


def validate(layer: Layer, profile: Profile, source: str) -> tuple[list[str], list[str]]:
    """Check a layer against a profile. Returns (errors, warnings)."""
    errors: list[str] = []
    warnings: list[str] = []
    unknown_emulators: set[str] = set()
    for emu, fid in sorted(layer.entries):
        keys = layer.entries[(emu, fid)]
        where = f"{source}: {emu}.{fid}"
        settings = profile.settings.get(emu)
        spec = None
        if settings is None:
            if emu not in unknown_emulators:
                unknown_emulators.add(emu)
                warnings.append(f"{source}: {emu}: not an emulator of profile {profile.name}")
        else:
            base = fid.partition(":")[0]
            spec = settings.files.get(base)
            if spec is None:
                warnings.append(f"{where}: not a file of emulator {emu} in {profile.name}")
            else:
                problem = _instance_problem(fid, spec.templated)
                if problem:
                    errors.append(f"{where}: {problem}")
        for name in sorted(keys):
            value = keys[name]
            secret = spec.secret if spec else ()
            if is_builtin_secret(name) or any(match(p, name) for p in secret):
                errors.append(f"{where}: {name}: secret keys are never layered")
                continue
            if spec is None or spec.format in _TYPED_FORMATS:
                continue
            if not isinstance(value, str):
                errors.append(f"{where}: {name}: {spec.format} holds raw text, so use a string")
            elif "\n" in value or "\r" in value:
                errors.append(f"{where}: {name}: a value cannot contain a line break")
            elif spec.format == "retroarch-cfg" and '"' in value:
                errors.append(f"{where}: {name}: retroarch values cannot contain a double quote")
    return errors, warnings


def effective(common: Layer, device: Layer) -> dict[EntryKey, dict[str, Value]]:
    """Merge two layers: the device's value wins per key; a device cannot unset a key."""
    merged = {entry: dict(keys) for entry, keys in common.entries.items()}
    for entry, keys in device.entries.items():
        merged.setdefault(entry, {}).update(keys)
    return merged


def render(layer: Layer) -> str:
    """The canonical text of a layer: sorted by emulator, file and key, under a header."""
    lines = [HEADER, f"format = {LAYER_FORMAT}"]
    for emu, fid in sorted(layer.entries):
        keys = layer.entries[(emu, fid)]
        if not keys:
            continue
        lines += ["", f"[{toml_key(emu)}.{toml_key(fid)}]"]
        lines += [f"{toml_key(name)} = {toml_scalar(keys[name])}" for name in sorted(keys)]
    return "\n".join(lines) + "\n"


def set_key(layer: Layer, emu: str, file: str, key: str, value: Value) -> Layer:
    entries = {entry: dict(keys) for entry, keys in layer.entries.items()}
    entries.setdefault((emu, file), {})[key] = value
    return Layer(entries)


def remove_key(layer: Layer, emu: str, file: str, key: str) -> Layer:
    entries = {entry: dict(keys) for entry, keys in layer.entries.items()}
    keys = entries.get((emu, file))
    if keys is not None:
        keys.pop(key, None)
        if not keys:
            del entries[(emu, file)]
    return Layer(entries)


def conflict_copies(names: Iterable[str]) -> list[str]:
    """Syncthing conflict copies among names relative to the settings folder.

    Only layer files (top-level `*.toml`) and anything under `files/` matter.
    """
    found = []
    for name in names:
        if _CONFLICT_MARK not in name:
            continue
        if name.startswith("files/") or ("/" not in name and name.endswith(".toml")):
            found.append(name)
    return sorted(found)
