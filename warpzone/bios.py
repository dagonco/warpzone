"""Check a game library's BIOS and firmware files against a shipped manifest.

warpzone never downloads or ships BIOS files. The manifest (`data/bios.toml`) holds
public metadata only: file names, sizes, hashes and file signatures. The decisions
(`judge_*`, `settle`, `unknown_files`, `render`) are pure; `Library` and `check_library`
are the only parts that touch the disk. See docs/design.md.

A file entry has one of four kinds of check: `hash` (size and md5, the default),
`magic` (it starts with fixed bytes, for official firmware and disk images that have no
fixed hash), `present` (it exists and is not empty) and `keys` (a secret keys file: it
must look like one, and its content is never printed, hashed or put in a message).
"""

from __future__ import annotations

import enum
import hashlib
import os
import posixpath
import re
import tomllib
import zipfile
from collections.abc import Callable, Collection, Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from importlib import resources
from itertools import chain
from pathlib import Path
from typing import Any

from warpzone.config import Config
from warpzone.profiles import Profile
from warpzone.provision import expand_home

CHUNK = 1024 * 1024
KEYS_SCAN_BYTES = 8 * 1024 * 1024  # a keys file is read this far, to see what it looks like
KIND_HASH, KIND_MAGIC, KIND_PRESENT, KIND_KEYS = "hash", "magic", "present", "keys"
KINDS = (KIND_HASH, KIND_MAGIC, KIND_PRESENT, KIND_KEYS)
BASES = ("bios", "roms")
OVERRIDES_FILE = "bios.toml"
SHOWN_NAMES = 4
PLACEHOLDERS = ("metadata.txt", "systeminfo.txt")
NON_GAME_DIRS = ("media", "@eaDir", "#recycle", "mlc01")  # mlc01: Cemu saves and installs
JUNK_FILES = ("Thumbs.db", "desktop.ini")
_MD5 = re.compile(r"^[0-9a-f]{32}$")
_CRC32 = re.compile(r"^[0-9a-f]{8}$")
_HEX_BYTES = re.compile(r"^(?:[0-9a-f]{2})+$")
# A keys file is hex-led lines: a 32-digit key, or (in common key lists) a 16-digit id,
# each followed by optional text.
_KEY_LINE = re.compile(r"^(?:[0-9a-fA-F]{32}|[0-9a-fA-F]{16})(?:[\s#].*)?$")
_KEY_32 = re.compile(r"^[0-9a-fA-F]{32}(?:[\s#]|$)")
_KEY = re.compile(r"^[a-z0-9][a-z0-9-]*$")


class BiosError(Exception):
    pass


@dataclass(frozen=True)
class BiosFile:
    path: str  # relative to the item's base, "/" separated
    size: int | None  # None: the size is not checked (only for kinds other than hash)
    md5: frozenset[str]  # empty: only the size is checked
    kind: str = KIND_HASH
    magic: bytes = b""  # kind "magic": the file starts with these bytes


@dataclass(frozen=True)
class Item:
    """One thing to have. It is satisfied when any of its files (or its zip) is ok."""

    system: str
    name: str
    base: str
    required: bool
    any_name: bool
    note: str
    files: tuple[BiosFile, ...] = ()
    zip_path: str | None = None
    members: Mapping[str, str] = field(default_factory=dict)  # member name -> crc32


@dataclass(frozen=True)
class System:
    key: str
    title: str
    items: tuple[Item, ...]
    games: tuple[str, ...] = ()  # folders of roms/ that hold its games; empty: always judged


@dataclass(frozen=True)
class Manifest:
    systems: Mapping[str, System]
    skip_dirs: tuple[str, ...] = ()


@dataclass(frozen=True)
class Overrides:
    accept: Mapping[tuple[str, str], frozenset[str]] = field(default_factory=dict)
    unwanted: frozenset[tuple[str, str]] = frozenset()


class Status(enum.Enum):
    OK = "ok"
    LOCAL = "local"  # accepted through the user's override file
    MISSING = "missing"
    WRONG = "wrong"
    SKIPPED = "skipped"  # marked unwanted by the user


@dataclass(frozen=True)
class FileCheck:
    path: str  # the manifest path
    status: Status
    detail: str = ""
    found: str | None = None  # the file that matched, when it is not named `path`


@dataclass(frozen=True)
class ItemResult:
    item: Item
    status: Status
    checks: tuple[FileCheck, ...] = ()

    @property
    def blocking(self) -> bool:
        return self.item.required and self.status in (Status.MISSING, Status.WRONG)


@dataclass(frozen=True)
class SystemReport:
    key: str
    title: str
    results: tuple[ItemResult, ...]
    judged: bool = True  # False: the library has no games for this system
    games: tuple[str, ...] = ()


@dataclass(frozen=True)
class Report:
    systems: tuple[SystemReport, ...]
    unknown: tuple[str, ...] | None  # None: not computed (only some systems were checked)

    @property
    def problems(self) -> list[ItemResult]:
        return [r for s in self.systems if s.judged for r in s.results if r.blocking]


# ---------------------------------------------------------------- manifest


def _path_ok(path: object) -> bool:
    if not isinstance(path, str) or not path or path.startswith("/") or "\\" in path:
        return False
    return ".." not in path.split("/") and "" not in path.split("/")


def _hashes(value: object, pattern: re.Pattern[str]) -> frozenset[str] | None:
    items = [value] if isinstance(value, str) else value
    if not isinstance(items, list) or not all(isinstance(i, str) for i in items):
        return None
    lowered = [i.lower() for i in items]
    return frozenset(lowered) if all(pattern.match(i) for i in lowered) else None


def _parse_item(system: str, raw: Any, where: str, err: Callable[[str], None]) -> Item | None:
    if not isinstance(raw, dict):
        err(f"{where}: must be a table")
        return None
    allowed = {"name", "base", "required", "any_name", "note", "files", "zip", "members"}
    for key in sorted(set(raw) - allowed):
        err(f"{where}: unknown key {key!r}")
    name = raw.get("name")
    if not isinstance(name, str) or not name.strip():
        err(f"{where}.name: required, must be a non-empty string")
        return None
    where = f"{where} ({name})"
    base = raw.get("base", "bios")
    if base not in BASES:
        err(f"{where}.base: must be one of {', '.join(BASES)}")
        base = "bios"
    flags = {}
    for key, default in (("required", True), ("any_name", False)):
        value = raw.get(key, default)
        if not isinstance(value, bool):
            err(f"{where}.{key}: must be true or false")
            value = default
        flags[key] = value
    note = raw.get("note", "")
    if not isinstance(note, str):
        err(f"{where}.note: must be a string")
        note = ""

    zip_path = raw.get("zip")
    if (zip_path is None) == ("files" not in raw):
        err(f"{where}: set exactly one of `files` or `zip`")
        return None
    if zip_path is not None:
        if not _path_ok(zip_path):
            err(f"{where}.zip: must be a relative path")
        raw_members = raw.get("members")
        members: dict[str, str] = {}
        if not isinstance(raw_members, dict) or not raw_members:
            err(f"{where}.members: required with `zip`, a table of member = crc32")
        else:
            for member, crc in raw_members.items():
                if not isinstance(crc, str) or not _CRC32.match(crc.lower()):
                    err(f"{where}.members.{member}: not a crc32 (8 hex digits)")
                else:
                    members[member] = crc.lower()
        if flags["any_name"]:
            err(f"{where}: `any_name` applies to `files` only")
        return Item(
            system, name, base, flags["required"], False, note, zip_path=zip_path, members=members
        )

    if "members" in raw:
        err(f"{where}: `members` goes with `zip`")
    raw_files = raw["files"]
    if not isinstance(raw_files, list) or not raw_files:
        err(f"{where}.files: must be a non-empty list")
        return None
    files: list[BiosFile] = []
    for entry in raw_files:
        file = _parse_file(entry, where, err)
        if file is not None:
            files.append(file)
    return Item(system, name, base, flags["required"], flags["any_name"], note, tuple(files))


def _parse_file(entry: object, where: str, err: Callable[[str], None]) -> BiosFile | None:
    allowed = {"path", "size", "md5", "check", "magic"}
    if not isinstance(entry, dict) or set(entry) - allowed:
        err(f"{where}.files: each entry is a table with path, size, md5, check and magic")
        return None
    path, size = entry.get("path"), entry.get("size")
    if not _path_ok(path):
        err(f"{where}.files: bad path {path!r}")
        return None
    kind = entry.get("check", KIND_HASH)
    if kind not in KINDS:
        err(f"{where}.files.{path}: check must be one of {', '.join(KINDS)}")
        return None
    if size is not None and (not isinstance(size, int) or isinstance(size, bool) or size <= 0):
        err(f"{where}.files.{path}: size must be a positive integer")
        return None
    if kind == KIND_HASH:
        if size is None:
            err(f"{where}.files.{path}: size must be a positive integer")
            return None
        if "magic" in entry:
            err(f'{where}.files.{path}: `magic` goes with check = "magic"')
            return None
        md5 = _hashes(entry["md5"], _MD5) if "md5" in entry else frozenset()
        if md5 is None:
            err(f"{where}.files.{path}: md5 must be 32 hex digits (or a list of them)")
            return None
        return BiosFile(path, size, md5)
    if "md5" in entry:
        err(f'{where}.files.{path}: `md5` goes with check = "hash"')
        return None
    magic = b""
    if kind == KIND_MAGIC:
        raw_magic = entry.get("magic")
        if not isinstance(raw_magic, str) or not _HEX_BYTES.match(raw_magic.lower()):
            err(f"{where}.files.{path}: magic must be the first bytes in hex (even digit count)")
            return None
        magic = bytes.fromhex(raw_magic)
    elif "magic" in entry or size is not None:
        err(f'{where}.files.{path}: check = "{kind}" takes only a path')
        return None
    return BiosFile(path, size, frozenset(), kind, magic)


def parse_manifest(data: dict[str, Any], source: str = "bios manifest") -> Manifest:
    errors: list[str] = []

    def err(msg: str) -> None:
        errors.append(f"{source}: {msg}")

    skip = data.get("skip_dirs", [])
    if not isinstance(skip, list) or not all(isinstance(d, str) and d for d in skip):
        err("skip_dirs: must be a list of folder names")
        skip = []
    systems: dict[str, System] = {}
    for key, table in data.items():
        if key == "skip_dirs":
            continue
        if not _KEY.match(key) or not isinstance(table, dict):
            err(f"{key}: expected a system table with a lowercase name")
            continue
        for extra in sorted(set(table) - {"title", "item", "games"}):
            err(f"{key}: unknown key {extra!r}")
        title = table.get("title")
        if not isinstance(title, str) or not title:
            err(f"{key}.title: required, must be a non-empty string")
            title = key
        games = table.get("games", [])
        if not isinstance(games, list) or not all(
            isinstance(g, str) and g and "/" not in g for g in games
        ):
            err(f"{key}.games: must be a list of folder names under roms/")
            games = []
        raw_items = table.get("item")
        if not isinstance(raw_items, list) or not raw_items:
            err(f"{key}.item: required, at least one [[{key}.item]]")
            continue
        items: list[Item] = []
        for index, raw in enumerate(raw_items, 1):
            item = _parse_item(key, raw, f"{key}.item #{index}", err)
            if item is None:
                continue
            if any(i.name == item.name for i in items):
                err(f"{key}: two items are named {item.name!r}")
            items.append(item)
        systems[key] = System(key, title, tuple(items), tuple(games))
    if errors:
        raise BiosError("\n".join(errors))
    return Manifest(systems, tuple(skip))


def load_manifest() -> Manifest:
    resource = resources.files("warpzone") / "data" / "bios.toml"
    try:
        data = tomllib.loads(resource.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as exc:
        raise BiosError(f"bios manifest: {exc}") from exc
    return parse_manifest(data)


# --------------------------------------------------------------- overrides


def default_overrides_path(config_path: Path) -> Path:
    """The local file sits next to the private config."""
    return config_path.parent / OVERRIDES_FILE


def parse_overrides(data: dict[str, Any], manifest: Manifest, source: str) -> Overrides:
    errors: list[str] = []

    def err(msg: str) -> None:
        errors.append(f"{source}: {msg}")

    accept: dict[tuple[str, str], frozenset[str]] = {}
    unwanted: set[tuple[str, str]] = set()
    for key, table in data.items():
        system = manifest.systems.get(key)
        if system is None or not isinstance(table, dict):
            known = ", ".join(sorted(manifest.systems))
            err(f"{key}: unknown system (known: {known})")
            continue
        for extra in sorted(set(table) - {"accept", "unwanted"}):
            err(f"{key}: unknown key {extra!r}")
        paths = {
            f.path for i in system.items for f in i.files if f.kind == KIND_HASH
        }  # a zip is accepted or not as a whole
        raw_accept = table.get("accept", {})
        if not isinstance(raw_accept, dict):
            err(f"{key}.accept: must be a table of path = [md5, ...]")
            raw_accept = {}
        for path, value in raw_accept.items():
            hashes = _hashes(value, _MD5)
            if path not in paths:
                err(f"{key}.accept.{path}: not a file of the {key} manifest")
            elif hashes is None:
                err(f"{key}.accept.{path}: must be md5 hashes (32 hex digits)")
            else:
                accept[(key, path)] = hashes
        names = {i.name for i in system.items}
        raw_unwanted = table.get("unwanted", [])
        if not isinstance(raw_unwanted, list) or not all(isinstance(n, str) for n in raw_unwanted):
            err(f"{key}.unwanted: must be a list of item names")
            raw_unwanted = []
        for name in raw_unwanted:
            if name in names:
                unwanted.add((key, name))
            else:
                err(f"{key}.unwanted: no item named {name!r} (items: {', '.join(sorted(names))})")
    if errors:
        raise BiosError("\n".join(errors))
    return Overrides(accept, frozenset(unwanted))


def load_overrides(path: Path, manifest: Manifest, *, must_exist: bool) -> Overrides:
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        if must_exist:
            raise BiosError(f"overrides file not found: {path}") from exc
        return Overrides()
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise BiosError(f"{path}: {exc}") from exc
    try:
        return parse_overrides(data, manifest, str(path))
    except BiosError as exc:
        raise BiosError(f"{path}:\n{exc}") from exc


# -------------------------------------------------------- pure decisions


def _names(names: Sequence[str]) -> str:
    shown = ", ".join(names[:SHOWN_NAMES])
    return f"{shown}, ... ({len(names)} in all)" if len(names) > SHOWN_NAMES else shown


def needs_hash(file: BiosFile, size: int | None) -> bool:
    """Only a file of the expected size is worth reading, and only if the md5 matters."""
    return file.kind == KIND_HASH and size == file.size and bool(file.md5)


def needs_head(file: BiosFile, size: int | None) -> bool:
    """A `magic` file is opened only when it exists and, if a size is given, has that size."""
    return file.kind == KIND_MAGIC and size is not None and file.size in (None, size)


def needs_scan(file: BiosFile, size: int | None) -> bool:
    return file.kind == KIND_KEYS and bool(size)


def judge_file(
    file: BiosFile, extra: frozenset[str], size: int | None, md5: str | None
) -> tuple[Status, str]:
    if size is None:
        return Status.MISSING, ""
    if size != file.size:
        return Status.WRONG, f"size {size}, expected {file.size}"
    if not file.md5 or md5 in file.md5:
        return Status.OK, ""
    if md5 in extra:
        return Status.LOCAL, ""
    expected = sorted(file.md5)
    shown = expected[0] if len(expected) == 1 else f"{expected[0]} or {len(expected) - 1} more"
    return Status.WRONG, f"md5 {md5}, expected {shown}"


def judge_magic(file: BiosFile, size: int | None, head: bytes | None) -> tuple[Status, str]:
    """`head` is the start of the file (at least `len(file.magic)` bytes), None if not read."""
    if size is None:
        return Status.MISSING, ""
    if file.size is not None and size != file.size:
        return Status.WRONG, f"size {size}, expected {file.size}"
    if head is not None and head.startswith(file.magic):
        return Status.OK, ""
    return Status.WRONG, f"does not start with the expected signature (hex {file.magic.hex()})"


def judge_present(size: int | None) -> tuple[Status, str]:
    if size is None:
        return Status.MISSING, ""
    return (Status.OK, "") if size > 0 else (Status.WRONG, "the file is empty")


def looks_like_keys(text: str) -> bool:
    """Whether text is a keys file: every line that is not blank or a `#` comment starts
    with a 32-digit hex key (or a 16-digit hex id), and at least one has a 32-digit key.
    It returns a verdict and never part of the text."""
    keys = 0
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if not _KEY_LINE.match(line):
            return False
        keys += bool(_KEY_32.match(line))
    return keys > 0


def judge_keys(size: int | None, looks_like: bool | None) -> tuple[Status, str]:
    """The detail is fixed text: nothing from the file may end up in a message."""
    if size is None:
        return Status.MISSING, ""
    if size == 0:
        return Status.WRONG, "the file is empty"
    if looks_like:
        return Status.OK, ""
    return Status.WRONG, "does not look like a keys file (hex-led lines)"


def judge_zip(members: Mapping[str, str], crcs: Mapping[str, str] | None) -> tuple[Status, str]:
    if crcs is None:
        return Status.MISSING, ""
    missing = [m for m in members if m not in crcs]
    bad = [m for m, crc in members.items() if m in crcs and crcs[m] != crc]
    if not missing and not bad:
        return Status.OK, ""
    parts = []
    if missing:
        parts.append(f"{len(missing)} member(s) missing: {_names(missing)}")
    if bad:
        parts.append(f"{len(bad)} member(s) with a different crc32: {_names(bad)}")
    return Status.WRONG, "; ".join(parts)


def settle(checks: Sequence[FileCheck]) -> Status:
    """An item is as good as its best file."""
    statuses = {c.status for c in checks}
    for status in (Status.OK, Status.LOCAL, Status.WRONG):
        if status in statuses:
            return status
    return Status.MISSING


def is_game_file(rel: str, bios_zips: Collection[str] = ()) -> bool:
    """Whether a file under roms/<folder> counts as a game.

    Not games: the placeholders EmuDeck leaves in every system folder, downloaded
    media, hidden and OS junk files, and the BIOS zips this tool checks.
    """
    parts = rel.split("/")  # folder, [subfolders...], file
    name = parts[-1]
    if rel in bios_zips or any(p in NON_GAME_DIRS for p in parts[1:-1]):
        return False
    if len(parts) == 2 and name in PLACEHOLDERS:
        return False
    return not any(p.startswith(".") for p in parts[1:]) and name not in JUNK_FILES


def has_games(rels: Iterable[str], bios_zips: Collection[str] = ()) -> bool:
    """`rels` are paths under roms/, e.g. "psx/Game.chd". Stops at the first game."""
    return any(is_game_file(rel, bios_zips) for rel in rels)


def unknown_files(present: Sequence[str], known: set[str]) -> tuple[str, ...]:
    return tuple(sorted(p for p in present if p not in known))


def default_root(
    cfg: Config, profiles: Mapping[str, Profile], home: Path, is_dir: Callable[[Path], bool]
) -> Path | None:
    """The library to check when `--root` is not given: a device's own, else the hub's."""
    candidates = [
        Path(expand_home(p.paths["library"], home))
        for _, p in sorted(profiles.items())
        if "library" in p.paths
    ]
    candidates.append(Path(cfg.hub.root) / "library")
    return next((c for c in candidates if is_dir(c / "bios") or is_dir(c / "roms")), None)


# ------------------------------------------------------------------- disk


class Library:
    """Read-only view of a library folder. It opens only the files it is asked about."""

    def __init__(self, root: Path, skip_dirs: Sequence[str] = ()) -> None:
        self.root = root
        self.skip_dirs = frozenset(skip_dirs)
        self._index: dict[str, int] | None = None
        self._md5: dict[tuple[str, str], str] = {}

    def has_content(self) -> bool:
        return any((self.root / base).is_dir() for base in BASES)

    def bios_index(self) -> dict[str, int]:
        """Every file under bios/ (relative path -> size), skipping `skip_dirs`."""
        if self._index is None:
            index: dict[str, int] = {}
            top = self.root / "bios"
            for dirpath, dirnames, filenames in os.walk(top):
                if Path(dirpath) == top:
                    dirnames[:] = [d for d in dirnames if d not in self.skip_dirs]
                for name in filenames:
                    full = Path(dirpath) / name
                    try:
                        index[full.relative_to(top).as_posix()] = full.stat().st_size
                    except OSError:
                        continue
            self._index = index
        return self._index

    def rom_paths(self, folder: str) -> Iterator[str]:
        """Files under roms/<folder>, as "folder/..." paths, without opening any."""
        top = self.root / "roms" / folder
        for dirpath, dirnames, filenames in os.walk(top):
            dirnames[:] = [d for d in dirnames if d not in NON_GAME_DIRS]
            here = Path(dirpath).relative_to(self.root / "roms").as_posix()
            for name in filenames:
                yield f"{here}/{name}"

    def size(self, base: str, rel: str) -> int | None:
        if base == "bios":
            return self.bios_index().get(rel)
        try:
            return (self.root / base / rel).stat().st_size
        except OSError:
            return None

    def md5(self, base: str, rel: str) -> str:
        key = (base, rel)
        if key not in self._md5:
            digest = hashlib.md5(usedforsecurity=False)
            with (self.root / base / rel).open("rb") as handle:
                while chunk := handle.read(CHUNK):
                    digest.update(chunk)
            self._md5[key] = digest.hexdigest()
        return self._md5[key]

    def head(self, base: str, rel: str, count: int) -> bytes:
        with (self.root / base / rel).open("rb") as handle:
            return handle.read(count)

    def looks_like_keys(self, base: str, rel: str) -> bool:
        """Reads the file only to answer yes or no; nothing of it is kept."""
        data = self.head(base, rel, KEYS_SCAN_BYTES)
        if len(data) == KEYS_SCAN_BYTES:  # cut mid-line: leave the partial last line out
            data = data[: data.rfind(b"\n") + 1]
        return looks_like_keys(data.decode("utf-8", "replace"))

    def zip_crcs(self, rel: str) -> dict[str, str] | None:
        """Member name -> crc32, read from the zip's directory (nothing is unpacked)."""
        path = self.root / "roms" / rel
        if not path.is_file():
            return None
        with zipfile.ZipFile(path) as archive:
            return {i.filename: f"{i.CRC:08x}" for i in archive.infolist() if not i.is_dir()}


def _check_zip(item: Item, lib: Library) -> tuple[FileCheck, ...]:
    assert item.zip_path is not None
    try:
        status, detail = judge_zip(item.members, lib.zip_crcs(item.zip_path))
    except (zipfile.BadZipFile, OSError) as exc:
        status, detail = Status.WRONG, f"cannot read it as a zip ({exc})"
    return (FileCheck(item.zip_path, status, detail),)


def _judge_on_disk(
    file: BiosFile, extra: frozenset[str], size: int | None, lib: Library, base: str
) -> tuple[Status, str]:
    """Open the file only as far as its kind of check needs, then let a pure judge decide."""
    if file.kind == KIND_MAGIC:
        head = lib.head(base, file.path, len(file.magic)) if needs_head(file, size) else None
        return judge_magic(file, size, head)
    if file.kind == KIND_PRESENT:
        return judge_present(size)
    if file.kind == KIND_KEYS:
        looks = lib.looks_like_keys(base, file.path) if needs_scan(file, size) else None
        return judge_keys(size, looks)
    md5 = lib.md5(base, file.path) if needs_hash(file, size) else None
    return judge_file(file, extra, size, md5)


def _check_files(
    item: Item, lib: Library, accept: Mapping[tuple[str, str], frozenset[str]]
) -> tuple[FileCheck, ...]:
    def extra(file: BiosFile) -> frozenset[str]:
        return accept.get((item.system, file.path), frozenset())

    checks: list[FileCheck] = []
    for file in item.files:
        size = lib.size(item.base, file.path)
        try:
            status, detail = _judge_on_disk(file, extra(file), size, lib, item.base)
        except OSError as exc:
            checks.append(FileCheck(file.path, Status.WRONG, f"cannot read it ({exc.strerror})"))
            continue
        checks.append(FileCheck(file.path, status, detail))
    if not item.any_name or settle(checks) in (Status.OK, Status.LOCAL):
        return tuple(checks)

    # Emulators that identify a BIOS by content do not care what it is called.
    named = {f.path for f in item.files}
    for rel, size in sorted(lib.bios_index().items()):
        if rel in named:
            continue
        candidates = [
            f
            for f in item.files
            if f.kind == KIND_HASH
            and f.size == size
            and posixpath.dirname(f.path) == posixpath.dirname(rel)
        ]
        if not candidates:
            continue
        try:
            md5 = lib.md5("bios", rel)
        except OSError:
            continue
        for file in candidates:
            status, _ = judge_file(file, extra(file), size, md5)
            if status in (Status.OK, Status.LOCAL):
                checks.append(FileCheck(file.path, status, found=rel))
                break
    return tuple(checks)


def check_item(
    item: Item, lib: Library, accept: Mapping[tuple[str, str], frozenset[str]]
) -> ItemResult:
    checks = _check_zip(item, lib) if item.zip_path else _check_files(item, lib, accept)
    return ItemResult(item, settle(checks), checks)


def _has_games(system: System, lib: Library, bios_zips: Collection[str]) -> bool:
    return has_games(chain.from_iterable(lib.rom_paths(f) for f in system.games), bios_zips)


def check_library(
    manifest: Manifest, overrides: Overrides, lib: Library, systems: Sequence[str] | None = None
) -> Report:
    unknown_system = [s for s in systems or () if s not in manifest.systems]
    if unknown_system:
        raise BiosError(
            f"unknown system {', '.join(unknown_system)} (known: {', '.join(manifest.systems)})"
        )
    bios_zips = {i.zip_path for s in manifest.systems.values() for i in s.items if i.zip_path}
    reports: list[SystemReport] = []
    for key, system in manifest.systems.items():
        if systems and key not in systems:
            continue
        judged = bool(systems) or not system.games or _has_games(system, lib, bios_zips)
        results = []
        for item in system.items:
            if (key, item.name) in overrides.unwanted:
                results.append(ItemResult(item, Status.SKIPPED))
            else:
                results.append(check_item(item, lib, overrides.accept))
        reports.append(SystemReport(key, system.title, tuple(results), judged, system.games))

    unknown = None
    if not systems:
        known = {f.path for s in manifest.systems.values() for i in s.items for f in i.files}
        known |= {
            c.found
            for r in (r for rep in reports for r in rep.results)
            for c in r.checks
            if c.found
        }
        unknown = unknown_files(list(lib.bios_index()), known)
    return Report(tuple(reports), unknown)


# ------------------------------------------------------------------ output

_TAGS = {
    Status.OK: "ok",
    Status.LOCAL: "accepted (local)",
}
TAG_WIDTH = 20


def _tag(status: Status, item: Item, satisfied: bool) -> str:
    if status in _TAGS:
        return _TAGS[status]
    if satisfied:
        return f"{status.value} (unused)"
    return f"{status.value} ({'required' if item.required else 'optional'})"


def _shown(item: Item, path: str) -> str:
    return path if item.base == "bios" else f"{item.base}/{path}"


def render_item(result: ItemResult) -> list[str]:
    item = result.item
    satisfied = result.status in (Status.OK, Status.LOCAL)
    lines: list[str] = []

    def add(status: Status, text: str) -> None:
        lines.append(f"  {_tag(status, item, satisfied):<{TAG_WIDTH}}{item.name}: {text}")

    if result.status is Status.SKIPPED:
        return lines
    shown_files: set[str] = set()
    for check in result.checks:
        actual = check.found or check.path
        if check.status in (Status.OK, Status.LOCAL) and actual not in shown_files:
            shown_files.add(actual)
            text = _shown(item, actual)
            if check.found:
                text += f" (matches {check.path})"
            add(check.status, text)
        elif check.status is Status.WRONG:
            add(check.status, f"{_shown(item, check.path)}: {check.detail}")
    if result.status is Status.MISSING:
        paths = [_shown(item, c.path) for c in result.checks]
        add(Status.MISSING, paths[0] if len(paths) == 1 else f"one of {_names(paths)}")
    if not satisfied and item.note:
        lines.append(f"  {'':<{TAG_WIDTH}}unlocks: {item.note}")
    return lines


def render(report: Report, *, show_all: bool = False) -> list[str]:
    lines: list[str] = []
    counts = dict.fromkeys(Status, 0)
    for system in report.systems:
        if not system.judged:
            lines.append(
                f"{system.key}: {system.title}: no games in roms/{{{','.join(system.games)}}}, "
                "not checked"
            )
            continue
        body = [line for r in system.results for line in render_item(r)]
        lines.append(f"{system.key}: {system.title}")
        lines.extend(body or ["  (all items marked unwanted)"])
        for r in system.results:
            counts[r.status] += 1
    if report.unknown:
        lines.append(f"unknown files in bios/: {len(report.unknown)}")
        if show_all:
            lines.extend(f"  {path}" for path in report.unknown)
        else:
            lines.append("  (list them with --all)")
    summary = (
        f"{counts[Status.OK] + counts[Status.LOCAL]} ok, {counts[Status.MISSING]} missing, "
        f"{counts[Status.WRONG]} wrong"
    )
    if counts[Status.SKIPPED]:
        summary += f", {counts[Status.SKIPPED]} skipped as unwanted"
    lines.append(f"items: {summary}")
    if report.problems:
        lines.append(f"{len(report.problems)} required item(s) missing or wrong")
    if any(
        c.detail.startswith("md5 ")
        for s in report.systems
        if s.judged
        for r in s.results
        for c in r.checks
    ):
        lines.append(
            "to keep a modified file on purpose, list its md5 under "
            f"[<system>.accept] in {OVERRIDES_FILE}"
        )
    return lines
