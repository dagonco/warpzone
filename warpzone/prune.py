"""Remove files a device no longer whitelists, asking Syncthing what it ignores.

When a pattern leaves a device's whitelist, Syncthing stops syncing the files but
leaves the copies on disk. A file is a leftover only when Syncthing itself has
flagged it as ignored in the folder database after a full rescan (`local.ignored`
in `GET /rest/db/file`) AND the hub still has the same file (`global`). Nothing
here re-implements Syncthing's ignore matcher; see development/design/prune.md. One more
case is deleted: a file the hub deleted on purpose, which `trash_record` says (rule 4b, "Files the
hub deleted on purpose" in the same page).
"""

from __future__ import annotations

import dataclasses
import enum
import fnmatch
import hashlib
import json
import os
import re
import stat
import time
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from warpzone import trash_record
from warpzone.config import DEFAULT_MAX_GIB, DEFAULT_RESCAN_TIMEOUT, Config
from warpzone.errors import UsageError, WarpzoneError
from warpzone.model import LIBRARY_ID, FolderSpec, device_specs
from warpzone.notify import NotifyError, notify
from warpzone.paths import expand_home
from warpzone.probes import REAL_PROBES, DeviceProbes
from warpzone.profiles import Profile
from warpzone.provision_folders import in_sync
from warpzone.report import Reporter
from warpzone.safefile import write_file
from warpzone.syncthing import Client, SyncthingError
from warpzone.trash_record import DeletedFile

GIB = 1024**3
# The hub's modification time is restored on the device when a file is pulled; FAT
# stores seconds in steps of two, hence the tolerance.
MTIME_TOLERANCE_S = 2.0
# Never offered for deletion, whatever the database says.
PROTECTED_ROOT = (".stfolder", ".stignore", ".stversions")
PROTECTED_ROOT_GLOBS = ("patterns-*.stignore",)
SCANNING = ("scanning", "scan-waiting")
SHOWN_GROUPS = 30
SHOWN_KEPT = 20
RECORD_MAX_BYTES = 64 * 1024 * 1024  # a larger record file is not read
HASH_CHUNK = 1024 * 1024
MISMATCH_STATE = "prune-compared.json"
_MISMATCH_STATE_VERSION = 1
DIFFERS_REASON = "its content differs from the deleted game"
UNREADABLE_REASON = "cannot be read to compare with the deleted game"
TRASHED_REASON = "the hub deleted it on purpose"

# Files EmuDeck and the emulators write into the library on every console; the shipped
# profile's `[library] ignore` list ignores them (data/profiles/steamos-emudeck.toml), so
# the hub never has them. See `is_generated`.
_GENERATED_PLACEHOLDERS = ("metadata.txt", "systeminfo.txt")
_GENERATED_BIOS_DIRS = (
    "PPSSPP",
    "HdPacks",
    "Mupen64plus",
    "fbneo",
    "kronos",
    "same_cdi",
    "ryujinx",
    "shadps4",
    "yuzu",
)
_GENERATED_DC_FILES = ("dc_nvmem.bin", "vulkan_pipeline.cache")

_TYPE_FILE = "FILE_INFO_TYPE_FILE"
_TYPE_SYMLINK = "FILE_INFO_TYPE_SYMLINK"
_TIME = re.compile(r"^(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d)(?:\.(\d+))?(Z|[+-]\d\d:\d\d)$")


class PruneError(WarpzoneError):
    pass


class PruneBusy(PruneError):
    """The full scan got no answer in time: the folder is still pulling."""


class Verdict(enum.Enum):
    UNTRACKED = "untracked"  # Syncthing has no record: ignored or created since the rescan
    WANTED = "wanted"  # Syncthing syncs it
    LEFTOVER = "leftover"
    LOCAL_ONLY = "local-only"  # ignored, but the hub does not have it
    DIFFERS = "differs"  # ignored, but not the hub's copy
    TRASHED = "trashed"  # ignored, and the hub deleted it on purpose (the record says so)


@dataclass(frozen=True)
class Entry:
    path: str  # relative to the folder root, "/" separated, as Syncthing names it
    kind: str  # "file" or "symlink"
    size: int
    mtime: float


@dataclass(frozen=True)
class Mismatch:
    """A copy whose content was compared with the record and differs, as it was when compared:
    the path (hashed, so the state file names no file), its identity and the SHA-1s it was
    compared with."""

    key: str
    dev: int
    ino: int
    size: int
    mtime_ns: int
    sha1s: tuple[str, ...]


@dataclass(frozen=True)
class Record:
    """The record of deleted games as it was read: the Syncthing version and the stat of the file
    (to see it did not change before a delete) and the unexpired files by path."""

    version: str  # Syncthing's local version vector of the file, as JSON
    size: int
    modified: float  # Syncthing's local modification time of the file
    mtime_ns: int
    deleted: Mapping[str, tuple[DeletedFile, ...]]


@dataclass
class FolderReport:
    folder_id: str
    root: str
    leftovers: list[Entry] = field(default_factory=list)
    trashed: list[Entry] = field(default_factory=list)  # rule 4b: see `classify`
    record: Record | None = None  # what `trashed` was decided from
    verified: dict[str, tuple[int, int, int, int]] = field(default_factory=dict)  # see `_compare`
    mismatches: list[Mismatch] = field(default_factory=list)  # copies whose content differs
    uncompared: int = 0  # trashed files whose SHA-1 a report does not compare
    kept: list[tuple[str, str]] = field(default_factory=list)  # (path, reason)
    generated: int = 0  # kept too, but expected: see `is_generated`
    untracked: int = 0
    held_bytes: int = 0  # the regular files the walk found: what the device holds here
    deleted_files: int = 0
    deleted_bytes: int = 0
    removed_dirs: int = 0

    @property
    def leftover_bytes(self) -> int:
        return sum(e.size for e in self.leftovers)

    @property
    def trashed_bytes(self) -> int:
        return sum(e.size for e in self.trashed)

    @property
    def files(self) -> int:
        """What an apply would delete here: the leftovers and the trashed files."""
        return len(self.leftovers) + len(self.trashed)

    @property
    def bytes(self) -> int:
        """What an apply would delete here, in bytes: what the guards and the cap count."""
        return self.leftover_bytes + self.trashed_bytes


def parse_time(text: Any) -> float | None:
    """Syncthing's RFC 3339 timestamps carry nanoseconds, which fromisoformat rejects."""
    match = _TIME.match(text) if isinstance(text, str) else None
    if not match:
        return None
    base, fraction, zone = match.groups()
    micro = (fraction or "0")[:6].ljust(6, "0")
    try:
        return datetime.fromisoformat(
            f"{base}.{micro}{'+00:00' if zone == 'Z' else zone}"
        ).timestamp()
    except ValueError:
        return None


def record_matches(entry: Entry, deleted: Sequence[DeletedFile]) -> list[DeletedFile]:
    """The files of `deleted` that are this regular file: same size, modification time within
    `MTIME_TOLERANCE_S`. Pure; the SHA-1 is not looked at (see `sha1_ok`)."""
    if entry.kind != "file":
        return []
    return [
        d
        for d in deleted
        if d.size == entry.size and abs(d.mtime_ns / 1e9 - entry.mtime) <= MTIME_TOLERANCE_S
    ]


def sha1_ok(record_file: DeletedFile, digest: str) -> bool:
    """Whether a file with SHA-1 `digest` is `record_file`: it has no SHA-1 to compare, or the
    same one."""
    return record_file.sha1 is None or record_file.sha1 == digest


def classify(
    entry: Entry, info: dict[str, Any] | None, deleted: Sequence[DeletedFile] = ()
) -> tuple[Verdict, str]:
    """Pure decision from one `db/file` answer, what is on disk and the record's files for the
    path (`deleted`); it never hashes. See development/design/prune.md, "Safety rules"."""
    if not info:
        return Verdict.UNTRACKED, ""
    if not (info.get("local") or {}).get("ignored"):
        return Verdict.WANTED, ""
    hub = info.get("global") or {}
    # A local-only file that was scanned before it got ignored shows up in `global`
    # too, flagged invalid or ignored. That is not the hub's copy.
    if not hub or hub.get("ignored") or hub.get("invalid"):
        return Verdict.LOCAL_ONLY, "the hub does not have it"
    if hub.get("deleted"):
        if record_matches(entry, deleted):
            return Verdict.TRASHED, TRASHED_REASON
        return Verdict.LOCAL_ONLY, "the hub does not have it"
    if hub.get("type") != (_TYPE_SYMLINK if entry.kind == "symlink" else _TYPE_FILE):
        return Verdict.DIFFERS, "the hub has a different kind of object"
    if entry.kind == "file":
        if hub.get("size") != entry.size:
            return Verdict.DIFFERS, "its size differs from the hub copy"
        synced = parse_time(hub.get("modified"))
        if synced is None or abs(synced - entry.mtime) > MTIME_TOLERANCE_S:
            return Verdict.DIFFERS, "it changed after it was synced"
    return Verdict.LEFTOVER, ""


def is_generated(path: str) -> bool:
    """Whether `path` (relative to the library root, "/" separated) is a file EmuDeck or an
    emulator writes by itself and the hub never has, so keeping it is expected.

    The list mirrors the profile's library ignores, one for one; see
    development/design/prune.md, "Files the console generates".
    """
    parts = path.split("/")
    n = len(parts)
    if parts[0] == "roms":
        return n == 3 and parts[2] in _GENERATED_PLACEHOLDERS
    if parts[0] != "bios" or n < 2:
        return False
    if parts[-1] == "readme.txt":
        return True
    if n < 3:
        return False
    folder = parts[1]
    if folder in _GENERATED_BIOS_DIRS:
        return True
    if folder == "dc":
        if n == 3:
            return (
                fnmatch.fnmatchcase(parts[2], "vmu_save_*.bin") or parts[2] in _GENERATED_DC_FILES
            )
        return parts[2] == "data"
    if folder == "mame2003-plus":
        return (n == 3 and parts[2] == "hiscore.dat") or (n > 3 and parts[2] == "artwork")
    return False


def guard(totals: Mapping[str, tuple[int, int]]) -> list[str]:
    """The folders an unattended run must not touch: those that would lose more than half.

    `totals` maps a folder id to (bytes the run would delete, bytes the device holds in it).
    Exactly half passes; one byte over fails. A single name returned means the whole run
    deletes nothing, in any folder (the caller decides that, this only names the folders).
    See development/design/prune.md, "Unattended runs".
    """
    return [folder for folder, (delete, held) in totals.items() if 2 * delete > held]


GUARD_STATE = "prune-guard.json"
_GUARD_STATE_VERSION = 1


@dataclass(frozen=True)
class GuardRefusal:
    """What the last unattended run held back: when, and per folder the bytes it would have
    deleted and the bytes the device holds there. No file or folder names."""

    when: float
    folders: tuple[tuple[str, int, int], ...]


def guard_state_path(state_base: Path) -> Path:
    """The file a guard refusal leaves; `state_base` is `~/.local/state/warpzone`."""
    return state_base / GUARD_STATE


def render_guard_state(refused: Mapping[str, tuple[int, int]], when: float) -> str:
    """The text of the state file for the folders `guard` named: folder -> (delete, held)."""
    return (
        json.dumps(
            {
                "version": _GUARD_STATE_VERSION,
                "time": int(when),
                "folders": {
                    folder: {"delete": delete, "held": held}
                    for folder, (delete, held) in sorted(refused.items())
                },
            },
            indent=2,
        )
        + "\n"
    )


def parse_guard_state(text: str) -> GuardRefusal | None:
    """The refusal a state file records, or None when the text is not one (never raises)."""
    try:
        data = json.loads(text)
        when = data["time"]
        raw = data["folders"]
        if not isinstance(when, int | float) or isinstance(when, bool) or not isinstance(raw, dict):
            return None
        folders = tuple(
            (str(folder), int(entry["delete"]), int(entry["held"]))
            for folder, entry in sorted(raw.items())
        )
    except (ValueError, KeyError, TypeError):
        return None
    return GuardRefusal(float(when), folders) if folders else None


def _protected(rel: str, marker: str) -> bool:
    if "/" in rel:
        return False
    return rel in (*PROTECTED_ROOT, marker) or any(
        fnmatch.fnmatchcase(rel, g) for g in PROTECTED_ROOT_GLOBS
    )


def _syncthing_temp(name: str) -> bool:
    return name.startswith("~syncthing~") or (
        name.startswith(".syncthing.") and name.endswith(".tmp")
    )


def walk(root: str, skip_dirs: set[str], marker: str) -> Iterator[Entry]:
    """Files and symlinks under `root`, never following links nor entering `skip_dirs`."""
    pending = [""]
    while pending:
        rel_dir = pending.pop()
        try:
            entries = sorted(os.scandir(os.path.join(root, rel_dir)), key=lambda e: e.name)
        except OSError:
            continue
        for item in entries:
            rel = f"{rel_dir}/{item.name}" if rel_dir else item.name
            if _protected(rel, marker) or _syncthing_temp(item.name):
                continue
            try:
                st = item.stat(follow_symlinks=False)
                if item.is_symlink():
                    yield Entry(rel, "symlink", st.st_size, st.st_mtime)
                elif item.is_dir(follow_symlinks=False):
                    if os.path.join(root, rel) not in skip_dirs:
                        pending.append(rel)
                elif item.is_file(follow_symlinks=False):
                    yield Entry(rel, "file", st.st_size, st.st_mtime)
            except OSError:
                continue


def rescan(
    client: Client,
    folder_id: str,
    *,
    timeout: float,
    poll: float,
    grace: float,
    probes: DeviceProbes = REAL_PROBES,
) -> None:
    """Full rescan, then wait for it. Ignore flags are only refreshed by a full scan."""
    before = client.db_status(folder_id).get("stateChanged")
    if not client.scan(folder_id):
        raise PruneBusy(
            "the folder is busy: Syncthing runs the scan only once it finishes pulling; "
            "run prune again when the folder is idle"
        )
    start = probes.clock()
    seen = False
    while True:
        st = client.db_status(folder_id)
        state = st.get("state")
        if state == "error":
            raise PruneError(f"the folder is in an error state: {st.get('error') or 'unknown'}")
        seen = seen or state in SCANNING
        # A scan of a small folder can start and end between two polls, so a changed
        # timestamp (or the grace period) also counts as "it ran".
        if state not in SCANNING and (
            seen or st.get("stateChanged") != before or probes.clock() - start >= grace
        ):
            return
        if probes.clock() - start >= timeout:
            raise PruneError(f"the rescan did not finish within {timeout:.0f}s")
        probes.sleep(poll)


def _norm(path: str) -> str:
    return os.path.normpath(path)


def _db_file(client: Client, folder_id: str, path: str) -> dict[str, Any] | None:
    """`db/file`, with None for a name that is not UTF-8 (a lone surrogate): Syncthing holds no
    record for it, so it counts as not known."""
    try:
        path.encode("utf-8")
    except UnicodeEncodeError:
        return None
    return client.db_file(folder_id, path)


def _shown(path: str) -> str:
    """`path` as text a report can always print, a name that is not UTF-8 included."""
    return path.encode("utf-8", "backslashreplace").decode("utf-8")


def _record_version(info: dict[str, Any] | None) -> tuple[str, int, float] | None:
    """The local version, size and modification time of the record file when it is in sync with
    the hub (the notion rule 2 applies to the whitelists) and has a size and a time the record
    can have; None otherwise."""
    if not in_sync(info):
        return None
    hub = (info or {}).get("global") or {}
    if hub.get("deleted") or hub.get("invalid") or hub.get("ignored"):
        return None
    local = (info or {}).get("local") or {}
    version, size = local.get("version"), local.get("size")
    modified = parse_time(local.get("modified"))
    if (
        modified is None
        or not isinstance(version, list | dict)
        or not version
        or not isinstance(size, int)
        or isinstance(size, bool)
        or not 0 <= size <= RECORD_MAX_BYTES
    ):
        return None
    return json.dumps(version, sort_keys=True), size, modified


def _read_regular(path: str, size: int) -> tuple[bytes, os.stat_result] | None:
    """The bytes of the regular file at `path` (never through a link) when it is `size` bytes
    and does not change while it is read; None otherwise."""
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0))
    except OSError:
        return None
    try:
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode) or before.st_size != size:
            return None
        data = b""
        while len(data) <= size:
            chunk = os.read(fd, HASH_CHUNK)
            if not chunk:
                break
            data += chunk
        after = os.fstat(fd)
    except OSError:
        return None
    finally:
        os.close(fd)
    same = (before.st_size, before.st_mtime_ns) == (after.st_size, after.st_mtime_ns)
    return (data, before) if same and len(data) == size else None


def read_record(
    client: Client,
    spec: FolderSpec,
    root: str,
    now: Callable[[], float] = time.time,
) -> Record | None:
    """The usable record of deleted games (`wz-library` only), read after the full scan, or None
    when it authorises nothing. See development/design/prune.md, "Files the hub deleted on
    purpose"."""
    if spec.id != LIBRARY_ID:
        return None
    try:
        first = _record_version(client.db_file(spec.id, trash_record.FILE))
        if first is None:
            return None
        version, size, modified = first
        found = _read_regular(os.path.join(root, trash_record.FILE), size)
        again = _record_version(client.db_file(spec.id, trash_record.FILE))
    except SyncthingError:
        return None
    if found is None or again != first:
        return None
    data, st = found
    # An edit in place that keeps the size must not pass: the file's time is Syncthing's record.
    if abs(st.st_mtime - modified) > MTIME_TOLERANCE_S:
        return None
    entries = trash_record.parse(data)
    if entries is None:
        return None
    index = trash_record.index(entries, datetime.fromtimestamp(now(), UTC))
    return Record(version, size, modified, st.st_mtime_ns, index)


def record_unchanged(client: Client, spec_id: str, root: str, record: Record) -> bool:
    """Whether the record file is still the version `record` was read from: in sync with the
    hub, the same Syncthing version, and the same size and modification time on disk."""
    try:
        found = _record_version(client.db_file(spec_id, trash_record.FILE))
        st = os.lstat(os.path.join(root, trash_record.FILE))
    except (OSError, SyncthingError):
        return False
    return (
        found == (record.version, record.size, record.modified)
        and stat.S_ISREG(st.st_mode)
        and (st.st_size, st.st_mtime_ns) == (record.size, record.mtime_ns)
    )


def _check_folder(client: Client, spec: FolderSpec, current: dict[str, Any] | None) -> str:
    """Rules 1 and 2 for one folder (`current` is its Syncthing config): the marker name it
    holds, or `PruneError` naming what is wrong. See development/design/prune.md, "Safety rules"."""
    if current is None:
        raise PruneError("not configured in the local Syncthing (run `warpzone provision device`)")
    if _norm(current.get("path", "")) != _norm(spec.path):
        raise PruneError(
            f"Syncthing has it at {current.get('path')!r}, the config says {spec.path!r}"
        )
    if current.get("type") != spec.type:
        raise PruneError(f"folder type is {current.get('type')!r}, expected {spec.type!r}")
    if current.get("paused"):
        raise PruneError("the folder is paused")
    root = _norm(spec.path)
    marker = str(current.get("markerName") or ".stfolder")
    if not os.path.isdir(os.path.join(root, marker)):
        raise PruneError(f"{root} has no {marker} (not mounted?)")

    # While provisioning is unfinished the ignores are the bootstrap ones, which
    # ignore everything: pruning then would wipe the library.
    state = client.ignores_state(spec.id)
    if state.get("error") or list(state.get("ignore") or []) != list(spec.ignores):
        raise PruneError(
            "the ignores are not the final whitelist include (run `warpzone provision device`)"
        )
    waiting = [f for f in spec.required_files if not in_sync(client.db_file(spec.id, f))]
    if waiting:
        raise PruneError(f"{', '.join(waiting)} not in sync with the hub yet")
    return marker


def folder_problem(client: Client, spec: FolderSpec) -> str | None:
    """Why rules 1 and 2 no longer hold for the folder right now, or None when they do."""
    try:
        configured = {f["id"]: f for f in client.folders()}
        _check_folder(client, spec, configured.get(spec.id))
    except (PruneError, SyncthingError) as exc:
        return str(exc)
    return None


def path_key(path: str) -> str:
    """What the state file keeps of a path: its SHA-256, so it names no file."""
    return hashlib.sha256(path.encode("utf-8", "surrogatepass")).hexdigest()


def _compare(
    root: str,
    entry: Entry,
    candidates: Sequence[DeletedFile],
    remembered: Mapping[str, Mismatch],
) -> tuple[str, tuple[int, int, int, int] | None, Mismatch | None]:
    """Compare a copy with the SHA-1s of the record entries it matches (rule 4b): ("ok", the
    identity it had when hashed, None) or ("differs", None, what to remember) or ("unreadable",
    None, None). A copy whose matching entry has no SHA-1 needs no comparison. A copy remembered
    as different, unchanged since, is not hashed again."""
    if any(c.sha1 is None for c in candidates):
        return "ok", None, None
    sha1s = tuple(sorted({c.sha1 for c in candidates if c.sha1 is not None}))
    full = os.path.join(root, entry.path)
    key = path_key(entry.path)
    try:
        st = os.lstat(full)
        if not stat.S_ISREG(st.st_mode):
            return "unreadable", None, None
        old = remembered.get(key)
        if (
            old
            and (old.dev, old.ino, old.size, old.mtime_ns) == _identity(st)
            and old.sha1s == sha1s
        ):
            return "differs", None, old
        digest = _sha1(full, st)
    except OSError:
        return "unreadable", None, None
    if digest is None:
        return "unreadable", None, None
    if digest in sha1s:
        return "ok", _identity(st), None
    return "differs", None, Mismatch(key, st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns, sha1s)


def inspect_folder(
    client: Client,
    spec: FolderSpec,
    configured: dict[str, dict[str, Any]],
    *,
    home: Path,
    rescan_timeout: float,
    poll: float,
    grace: float,
    probes: DeviceProbes,
    now: Callable[[], float] = time.time,
    compare: bool = False,
    remembered: Mapping[str, Mismatch] | None = None,
) -> FolderReport:
    """Check the folder is in its normal whitelisted state, rescan, classify every file. With
    `compare` (a run that may delete), the files of rule 4b are compared with the record's
    SHA-1s here, so a copy that differs is kept before the guards are computed."""
    marker = _check_folder(client, spec, configured.get(spec.id))
    root = _norm(spec.path)
    rescan(client, spec.id, timeout=rescan_timeout, poll=poll, grace=grace, probes=probes)

    skip = {
        _norm(expand_home(f.get("path", ""), home))
        for fid, f in configured.items()
        if fid != spec.id
    }
    report = FolderReport(spec.id, root)
    report.record = read_record(client, spec, root, now)
    known = report.record.deleted if report.record else {}
    for entry in walk(root, skip, marker):
        if entry.kind == "file":
            report.held_bytes += entry.size
        deleted = known.get(entry.path, ())
        verdict, reason = classify(entry, _db_file(client, spec.id, entry.path), deleted)
        if verdict is Verdict.TRASHED:
            candidates = record_matches(entry, deleted)
            if not compare:
                report.uncompared += all(c.sha1 is not None for c in candidates)
            else:
                outcome, identity, mismatch = _compare(root, entry, candidates, remembered or {})
                if identity is not None:
                    report.verified[entry.path] = identity
                if mismatch is not None:
                    report.mismatches.append(mismatch)
                if outcome != "ok":
                    verdict = Verdict.DIFFERS
                    reason = DIFFERS_REASON if outcome == "differs" else UNREADABLE_REASON
        if verdict is Verdict.LEFTOVER:
            report.leftovers.append(entry)
        elif verdict is Verdict.TRASHED:
            report.trashed.append(entry)
        elif verdict is Verdict.UNTRACKED:
            report.untracked += 1
        elif verdict is Verdict.LOCAL_ONLY and is_generated(entry.path):
            report.generated += 1
        elif verdict is not Verdict.WANTED:
            report.kept.append((entry.path, reason))
    return report


def _inside(root: str, path: str) -> bool:
    real_root = os.path.realpath(root)
    return os.path.commonpath([real_root, os.path.realpath(path)]) == real_root


def _sha1(path: str, before: os.stat_result) -> str | None:
    """The SHA-1 of the regular file at `path`, read in chunks; None when it is not the file
    `before` described or its size or modification time changes while it is read."""
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0))
    try:
        opened = os.fstat(fd)
        same = (opened.st_dev, opened.st_ino, opened.st_size, opened.st_mtime_ns) == (
            before.st_dev,
            before.st_ino,
            before.st_size,
            before.st_mtime_ns,
        )
        if not stat.S_ISREG(opened.st_mode) or not same:
            return None
        digest = hashlib.sha1(usedforsecurity=False)
        while chunk := os.read(fd, HASH_CHUNK):
            digest.update(chunk)
        after = os.fstat(fd)
    finally:
        os.close(fd)
    if (after.st_size, after.st_mtime_ns) != (opened.st_size, opened.st_mtime_ns):
        return None
    return digest.hexdigest()


def _plain_parent(root: str, rel: str) -> bool:
    """Whether the folders above `rel` are real folders inside `root`, with no link on the way."""
    parent = os.path.dirname(rel)
    real_root = os.path.realpath(root)
    expected = os.path.join(real_root, parent) if parent else real_root
    return os.path.realpath(os.path.join(root, parent)) == expected


def _identity(st: os.stat_result) -> tuple[int, int, int, int]:
    return st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns


def _delete_leftover(client: Client, report: FolderReport, entry: Entry) -> bool:
    full = os.path.join(report.root, entry.path)
    st = os.lstat(full)
    fresh = dataclasses.replace(entry, size=st.st_size, mtime=st.st_mtime)
    verdict, _ = classify(fresh, _db_file(client, report.folder_id, entry.path))
    if verdict is not Verdict.LEFTOVER or not _inside(report.root, os.path.dirname(full)):
        return False
    os.unlink(full)
    return True


def _delete_trashed(client: Client, report: FolderReport, entry: Entry) -> bool:
    """Rule 4b for one file, all of it checked again right before the unlink and the hashing done
    here; see development/design/prune.md, "Safety rules" and "Files the hub deleted on purpose"."""
    record = report.record
    if record is None:
        return False
    full = os.path.join(report.root, entry.path)
    known = record.deleted.get(entry.path, ())

    def still_trashed(st: os.stat_result) -> bool:
        fresh = dataclasses.replace(entry, kind="file", size=st.st_size, mtime=st.st_mtime)
        info = _db_file(client, report.folder_id, entry.path)
        return classify(fresh, info, known)[0] is Verdict.TRASHED

    st = os.lstat(full)
    if not stat.S_ISREG(st.st_mode) or not _plain_parent(report.root, entry.path):
        return False
    if not still_trashed(st):
        return False
    fresh = dataclasses.replace(entry, kind="file", size=st.st_size, mtime=st.st_mtime)
    candidates = record_matches(fresh, known)
    if all(c.sha1 is not None for c in candidates):
        # The inspection of a run that may delete compared it and noted its identity; one that
        # changed since is kept. Without that note (a caller that did not compare) hash it now.
        seen = report.verified.get(entry.path)
        if seen is not None:
            if seen != _identity(st):
                return False
        else:
            digest = _sha1(full, st)
            if digest is None or not any(sha1_ok(c, digest) for c in candidates):
                return False
    if not record_unchanged(client, report.folder_id, report.root, record):
        return False
    after = os.lstat(full)
    if _identity(after) != _identity(st) or not still_trashed(after):
        return False
    os.unlink(full)
    return True


def apply_folder(
    client: Client,
    report: FolderReport,
    marker: str = ".stfolder",
    rep: Reporter | None = None,
    still_safe: Callable[[], str | None] | None = None,
) -> None:
    """Delete the leftovers and the trashed files, re-checking each one right before it goes.
    `still_safe` says why rules 1 and 2 no longer hold; before each trashed file, the first
    answer stops the rest. See development/design/prune.md, "Safety rules"."""
    parents: set[str] = set()
    jobs = [(e, _delete_leftover) for e in report.leftovers]
    jobs += [(e, _delete_trashed) for e in report.trashed]
    stopped = False
    for entry, delete in jobs:
        if delete is _delete_trashed and still_safe is not None and not stopped:
            why = still_safe()
            if why is not None:
                stopped = True
                if rep:
                    rep.warn(f"{report.folder_id}: stopped deleting: {why}")
        if stopped and delete is _delete_trashed:
            continue
        try:
            if not delete(client, report, entry):
                continue
        except FileNotFoundError:
            continue
        except (OSError, SyncthingError) as exc:
            if rep:
                rep.warn(f"{report.folder_id}: cannot delete {_shown(entry.path)}: {exc}")
            continue
        report.deleted_files += 1
        report.deleted_bytes += entry.size
        parent = os.path.dirname(entry.path)
        while parent:
            parents.add(parent)
            parent = os.path.dirname(parent)

    # Deepest first, so a directory is looked at after its children. Only
    # directories Syncthing ignores are removed, and only when rmdir finds them empty.
    # Each directory is scanned on its own before its record is trusted (a full scan may not
    # flag it; see development/design/prune.md).
    for rel in sorted(parents, key=lambda p: p.count("/"), reverse=True):
        if _protected(rel, marker):
            continue
        try:
            client.scan(report.folder_id, sub=rel)
            info = _db_file(client, report.folder_id, rel)
            if (info or {}).get("local", {}).get("ignored"):
                os.rmdir(os.path.join(report.root, rel))
                report.removed_dirs += 1
        except (OSError, SyncthingError):
            continue


def human(size: int) -> str:
    value = float(size)
    for unit in ("B", "KiB", "MiB", "GiB"):
        if value < 1024 or unit == "GiB":
            return f"{int(value)} B" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    raise AssertionError("unreachable")


def _group(path: str) -> str:
    parts = path.split("/")
    return "/".join(parts[:2]) if len(parts) > 2 else (parts[0] if len(parts) > 1 else ".")


def _print_generated(report: FolderReport, rep: Reporter) -> None:
    if report.generated:
        rep.out(
            f"{report.folder_id}: {report.generated} file(s) emulators and EmuDeck "
            "generate themselves"
        )


def _print_entries(entries: Sequence[Entry], rep: Reporter, *, list_all: bool) -> None:
    """The files a report lists: every one with `--list`, else grouped by their first two levels."""
    if list_all:
        for entry in entries:
            rep.out(f"  {_shown(entry.path)}")
        return
    groups: dict[str, list[int]] = {}
    for entry in entries:
        count, size = groups.setdefault(_group(entry.path), [0, 0])
        groups[_group(entry.path)] = [count + 1, size + entry.size]
    ranked = sorted(groups.items(), key=lambda kv: (-kv[1][1], kv[0]))
    for name, (count, size) in ranked[:SHOWN_GROUPS]:
        rep.out(f"  {_shown(name)}: {count} file(s), {human(size)}")
    if len(ranked) > SHOWN_GROUPS:
        rep.out(f"  ... and {len(ranked) - SHOWN_GROUPS} more folder(s), use --list")


def print_report(
    report: FolderReport, rep: Reporter, *, list_all: bool, summary_only: bool = False
) -> None:
    n = len(report.leftovers)
    rep.out(f"{report.folder_id}: {n} leftover file(s), {human(report.leftover_bytes)}")
    if report.trashed:
        rep.out(
            f"{report.folder_id}: {len(report.trashed)} file(s) the hub deleted on purpose, "
            f"{human(report.trashed_bytes)}"
            + (
                " (not compared with the record's SHA-1 yet: --apply does)"
                if report.uncompared
                else ""
            )
        )
    if summary_only:
        # An unattended run writes to a journal: the counts, no file or folder names.
        if report.kept:
            rep.out(
                f"{report.folder_id}: kept {len(report.kept)} ignored file(s) "
                "the hub cannot restore"
            )
        _print_generated(report, rep)
        if report.untracked:
            rep.out(
                f"{report.folder_id}: {report.untracked} file(s) not known to Syncthing, left alone"
            )
        return
    _print_entries([*report.leftovers, *report.trashed], rep, list_all=list_all)
    if report.kept:
        rep.out(
            f"{report.folder_id}: kept {len(report.kept)} ignored file(s) the hub cannot restore"
        )
        for path, reason in report.kept[: len(report.kept) if list_all else SHOWN_KEPT]:
            rep.out(f"  {_shown(path)} ({reason})")
        if not list_all and len(report.kept) > SHOWN_KEPT:
            rep.out(f"  ... and {len(report.kept) - SHOWN_KEPT} more, use --list")
    _print_generated(report, rep)
    if report.untracked:
        rep.out(
            f"{report.folder_id}: {report.untracked} file(s) not known to Syncthing, left alone"
        )


def record_guard_refusal(
    state_base: Path, refused: Mapping[str, tuple[int, int]], when: float
) -> None:
    """Leave the refusal where `doctor` can see it (atomic, mode 0600)."""
    write_file(guard_state_path(state_base), render_guard_state(refused, when).encode())


def clear_guard_refusal(state_base: Path) -> None:
    guard_state_path(state_base).unlink(missing_ok=True)


def mismatch_state_path(state_base: Path) -> Path:
    return state_base / MISMATCH_STATE


def render_mismatch_state(items: Sequence[Mismatch]) -> str:
    """The text of the state file of the copies whose content differs from the record."""
    return (
        json.dumps(
            {
                "version": _MISMATCH_STATE_VERSION,
                "copies": [
                    {
                        "path": m.key,
                        "dev": m.dev,
                        "ino": m.ino,
                        "size": m.size,
                        "mtime_ns": m.mtime_ns,
                        "sha1s": list(m.sha1s),
                    }
                    for m in sorted(items, key=lambda m: m.key)
                ],
            },
            indent=2,
        )
        + "\n"
    )


def parse_mismatch_state(text: str) -> dict[str, Mismatch]:
    """The copies a state file records, by path key; empty when the text is not one (never
    raises), which only means they are hashed again."""
    try:
        raw = json.loads(text)["copies"]
        found = [
            Mismatch(
                str(c["path"]),
                int(c["dev"]),
                int(c["ino"]),
                int(c["size"]),
                int(c["mtime_ns"]),
                tuple(str(h) for h in c["sha1s"]),
            )
            for c in raw
        ]
    except (ValueError, KeyError, TypeError, AttributeError):
        return {}
    return {m.key: m for m in found}


def read_mismatches(state_base: Path) -> dict[str, Mismatch]:
    try:
        return parse_mismatch_state(mismatch_state_path(state_base).read_text())
    except (OSError, ValueError):
        return {}


def _track_mismatches(state_base: Path, rep: Reporter, items: Sequence[Mismatch]) -> None:
    """Remember the copies that differ (atomic, mode 0600), or forget them all when there are
    none. A write that fails is a warning: the next run only hashes them again."""
    path = mismatch_state_path(state_base)
    try:
        if items:
            write_file(path, render_mismatch_state(items).encode())
        else:
            path.unlink(missing_ok=True)
    except OSError as exc:
        rep.warn(f"cannot update {path}: {exc.strerror or exc}")


def _track_refusal(
    state_base: Path,
    rep: Reporter,
    refused: Mapping[str, tuple[int, int]],
    when: float,
    *,
    settled: bool,
) -> None:
    """Record the guard's refusal, or clear an old one once a run settles without it. A write
    that fails is a warning: the run itself did what it was asked."""
    try:
        if refused:
            record_guard_refusal(state_base, refused, when)
        elif settled:
            clear_guard_refusal(state_base)
    except OSError as exc:
        rep.warn(f"cannot update {guard_state_path(state_base)}: {exc.strerror or exc}")


def prune_device(
    cfg: Config,
    name: str,
    profile: Profile,
    local: Client,
    rep: Reporter,
    *,
    home: Path,
    apply: bool = False,
    max_gib: float = DEFAULT_MAX_GIB,
    force: bool = False,
    list_all: bool = False,
    notify_url: str | None = None,
    rescan_timeout: float = DEFAULT_RESCAN_TIMEOUT,
    poll: float = 1.0,
    grace: float = 5.0,
    probes: DeviceProbes = REAL_PROBES,
    unattended: bool = False,
    state_base: Path | None = None,
    now: Callable[[], float] = time.time,
) -> int:
    """Report (default) or delete leftovers in every whitelisted folder. Returns problems.

    `unattended` is what the prune timer runs: it has no size cap (`max_gib` and `force` do
    not apply), has the half-of-device guard, and treats a folder that is still pulling as "try
    again later". `state_base` is where a guard refusal is recorded; None records nothing. See
    development/design/prune.md, "Unattended runs".
    """
    if unattended and not apply:
        raise UsageError("--unattended needs --apply")
    device = cfg.devices[name]
    actual = local.my_id()
    if actual != device.device_id:
        raise PruneError(
            f"the local Syncthing is device {actual[:7]}..., but devices.{name}.device_id "
            f"starts with {device.device_id[:7]}..."
        )
    specs = [
        dataclasses.replace(s, path=expand_home(s.path, home))
        for s in device_specs(cfg, name, profile)
        if s.bootstrap_ignores is not None  # only whitelisted folders are ever pruned
    ]
    if not specs:
        rep.out(f"device {name} subscribes to no whitelisted folder, nothing to prune")
        return 0

    configured = {f["id"]: f for f in local.folders()}
    remembered = read_mismatches(state_base) if apply and state_base is not None else {}
    problems = 0
    busy: list[str] = []
    skipped = 0
    refused: dict[str, tuple[int, int]] = {}
    reports: list[FolderReport] = []
    for spec in specs:
        try:
            report = inspect_folder(
                local,
                spec,
                configured,
                home=home,
                rescan_timeout=rescan_timeout,
                poll=poll,
                grace=grace,
                probes=probes,
                now=now,
                compare=apply,
                remembered=remembered,
            )
        except PruneBusy as exc:
            if not unattended:
                rep.warn(f"{spec.id}: skipped, {exc}")
                problems += 1
                skipped += 1
                continue
            busy.append(spec.id)
            rep.out(f"{spec.id}: busy, still pulling; not pruned this time")
            continue
        except PruneError as exc:
            rep.warn(f"{spec.id}: skipped, {exc}")
            problems += 1
            skipped += 1
            continue
        print_report(report, rep, list_all=list_all and not unattended, summary_only=unattended)
        reports.append(report)

    if apply and state_base is not None and not busy and not skipped:
        _track_mismatches(state_base, rep, [m for r in reports for m in r.mismatches])

    total_files = sum(r.files for r in reports)
    total_bytes = sum(r.bytes for r in reports)
    rep.out(f"total: {total_files} leftover file(s), {human(total_bytes)}")

    deleting = apply and total_files > 0
    if deleting and unattended:
        # A device that is still pulling in one folder deletes nothing in any: what the
        # index says is not settled, and the next tick tries again.
        if busy:
            rep.out("not deleting anything while a folder is busy; the next run tries again")
            deleting = False
        else:
            totals = {r.folder_id: (r.bytes, r.held_bytes) for r in reports}
            over = guard(totals)
            if over:
                refused = {folder: totals[folder] for folder in over}
                rep.warn(
                    "refusing to delete anything: "
                    + ", ".join(
                        f"{r.folder_id} would lose {human(r.bytes)} of the {human(r.held_bytes)} "
                        "it holds"
                        for r in reports
                        if r.folder_id in over
                    )
                    + ", more than half. Check the whitelist, then run `warpzone prune --apply` "
                    "by hand"
                )
                problems += 1
                deleting = False
    elif deleting and total_bytes > max_gib * GIB and not force:
        rep.warn(
            f"refusing to delete {human(total_bytes)}: more than --max-gib {max_gib:g}. "
            "Check the report, then raise --max-gib or pass --force"
        )
        problems += 1
        deleting = False
    if deleting:
        for report in reports:
            marker = (configured[report.folder_id].get("markerName")) or ".stfolder"
            spec = next(sp for sp in specs if sp.id == report.folder_id)

            def still_safe(spec: FolderSpec = spec) -> str | None:
                return folder_problem(local, spec)

            apply_folder(local, report, marker, rep, still_safe=still_safe)
            rep.out(
                f"{report.folder_id}: deleted {report.deleted_files} file(s), "
                f"{human(report.deleted_bytes)}, removed {report.removed_dirs} empty folder(s)"
            )
            if report.deleted_files < report.files:
                problems += 1
                rep.warn(
                    f"{report.folder_id}: {report.files - report.deleted_files} "
                    "file(s) were not deleted (changed meanwhile or a delete failed)"
                )
    elif total_files and not apply:
        rep.out("report only: run again with --apply to delete")

    if state_base is not None and apply:
        _track_refusal(
            state_base,
            rep,
            refused,
            now(),
            settled=not busy and not skipped and (deleting or total_files == 0),
        )

    if notify_url:
        for report in reports:
            if not report.files:
                continue
            done = deleting
            payload = {
                "device": name,
                "folder": report.folder_id,
                "files": report.deleted_files if done else report.files,
                "bytes": report.deleted_bytes if done else report.bytes,
                "applied": done,
            }
            try:
                notify(notify_url, payload)
            except NotifyError as exc:
                rep.warn(str(exc))
    return problems
