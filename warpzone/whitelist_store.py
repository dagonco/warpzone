"""Whitelist files on the hub: reading with a stamp, backups, the guarded write, the hub scan,
`warpzone whitelist migrate` and its proof walk, and the `check` report of excluded systems.

The decisions are in `whitelist.py` (pure); this module touches the disk and, for the scan,
the hub's Syncthing. See "The whitelist model" in development/design/library-ui.md.
"""

from __future__ import annotations

import fnmatch
import hashlib
import os
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from warpzone import ignores, safefile
from warpzone import whitelist as wl
from warpzone.config import Config
from warpzone.errors import WarpzoneError
from warpzone.model import LIBRARY_ID
from warpzone.paths import expand_home
from warpzone.profiles import Profile
from warpzone.syncthing import Client, SyncthingError

KEEP_BACKUPS = 50
DIFF_LIMIT = 10  # the paths a refusal by the proof lists
# Names the walk never visits: Syncthing's own, and the junk `patterns-common.stignore` ignores
# on both sides.
SYNCTHING_NAMES = frozenset({".stfolder", ".stversions"})
JUNK_PATTERNS = tuple(line.removeprefix("(?d)") for line in ignores.COMMON_LINES)


def whitelist_path(root: Path, device: str) -> Path:
    return root / "library" / ignores.device_file(device)


def backups_dir(root: Path, device: str) -> Path:
    return root / "catalog" / "whitelists" / device


def utc_now() -> datetime:
    return datetime.now(UTC)


# --------------------------------------------------------------------------- reading


class WhitelistIOError(WarpzoneError):
    """A whitelist, or its backup, could not be read or written (permissions, a full disk).

    The message names the file and the cause. `strerror` is the cause alone, for an answer
    that must not carry the hub's paths (the UI's).
    """

    def __init__(self, action: str, path: Path, cause: OSError) -> None:
        self.path = path
        self.strerror = cause.strerror or type(cause).__name__
        super().__init__(f"cannot {action} {path}: {self.strerror}")


@dataclass(frozen=True)
class Snapshot:
    """A whitelist file as read: its bytes and the stamp a write compares against."""

    path: Path
    data: bytes | None  # None: the file does not exist
    stamp: safefile.FileStamp | None

    @property
    def etag(self) -> str:
        """The SHA-256 of the bytes, `"absent"` for a missing file."""
        return "absent" if self.stamp is None else self.stamp.sha256


def read(path: Path) -> Snapshot:
    """Read the file once, taking its stamp from the same bytes."""
    try:
        with open(path, "rb") as fh:
            st = os.fstat(fh.fileno())
            data = fh.read()
    except FileNotFoundError:
        return Snapshot(path, None, None)
    except OSError as exc:
        raise WhitelistIOError("read", path, exc) from exc
    return Snapshot(path, data, safefile.FileStamp(st.st_size, st.st_mtime_ns, _sha(data)))


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


# --------------------------------------------------------------------------- writing


def backup(root: Path, device: str, data: bytes, now: datetime) -> Path:
    """Copy `data` to `catalog/whitelists/<device>/<UTC time>.stignore`; keep the 50 newest."""
    directory = backups_dir(root, device)
    target = directory / (now.astimezone(UTC).strftime("%Y%m%dT%H%M%S.%fZ") + ".stignore")
    safefile.write_file(target, data, 0o644)
    for old in sorted(directory.glob("*.stignore"))[:-KEEP_BACKUPS]:
        old.unlink(missing_ok=True)
    return target


class NoLibrary(WarpzoneError):
    """A whitelist cannot be created because the `library/` folder is not there (an unmounted
    or wrong hub root); nothing is created."""


def _require_library(path: Path) -> None:
    if not path.parent.is_dir():
        raise NoLibrary(f"{path.parent} is not a folder; is the hub root mounted and right?")


@dataclass(frozen=True)
class Written:
    edit: safefile.Edit
    backup: Path | None = None

    @property
    def ok(self) -> bool:
        return self.edit is safefile.Edit.WRITTEN


def write(
    root: Path, device: str, data: bytes, snapshot: Snapshot, *, now: datetime | None = None
) -> Written:
    """Back up the file the snapshot holds, then replace it unless it changed since.

    The atomic write compares the file with the snapshot's stamp once more right before the
    rename. A missing file (`"absent"`) is created, and a file that appeared meanwhile is not
    overwritten.
    """
    if snapshot.stamp is None:
        _require_library(snapshot.path)
    saved: list[Path] = []
    failing = snapshot.path  # the file the OSError of the step in progress is about

    def save() -> None:
        nonlocal failing
        if snapshot.data is not None:
            failing = backups_dir(root, device)
            saved.append(backup(root, device, snapshot.data, now or utc_now()))
            failing = snapshot.path

    try:
        edit = safefile.guarded_edit(
            snapshot.path, data, expect=snapshot.stamp, busy=lambda: False, backup=save
        )
    except OSError as exc:
        raise WhitelistIOError("write", failing, exc) from exc
    return Written(edit, saved[0] if saved else None)


def scan(client: Client, device: str) -> str | None:
    """Ask the hub's Syncthing to scan the device's whitelist so the devices hear of it at
    once. A failure is a warning (the hub's watcher picks the file up anyway): it is returned."""
    try:
        client.scan(LIBRARY_ID, sub=ignores.device_file(device))
    except SyncthingError as exc:
        return f"could not ask the hub to scan {ignores.device_file(device)}: {exc}"
    return None


class Stale(Exception):
    """The file changed since the `etag` the caller computed its change from."""


def replace(
    root: Path,
    device: str,
    etag: str,
    build: Callable[[Snapshot], bytes],
    *,
    now: datetime | None = None,
    client: Client | None = None,
) -> list[str]:
    """The UI's write: read again, compare the `etag`, build the new file, back up, write, scan.

    `build` gets the snapshot just read and returns the bytes to write (it may raise, and then
    nothing is written). A missing file is created only inside an existing `library/`
    (`NoLibrary` otherwise). Raises `Stale` when the file differs from `etag` or changes during the
    write. Returns warnings (a failed scan). The caller holds the lock that serializes writes.
    """
    snapshot = read(whitelist_path(root, device))
    if snapshot.etag != etag:
        raise Stale(device)
    if snapshot.stamp is None:
        _require_library(snapshot.path)
    result = write(root, device, build(snapshot), snapshot, now=now)
    if not result.ok:
        raise Stale(device)
    warning = scan(client, device) if client is not None else None
    return [warning] if warning else []


# --------------------------------------------------------------------------- the proof walk


def junk(name: str) -> bool:
    return name in SYNCTHING_NAMES or any(fnmatch.fnmatchcase(name, p) for p in JUNK_PATTERNS)


def walk(library: Path, prover: wl.Prover) -> Iterator[str]:
    """Every path below `library` (files and directories, no symlink followed), relative with
    `/`, skipping Syncthing's folders and the junk names, and the directories `prover.skip`
    says hold nothing that could be on."""
    stack = [""]
    while stack:
        rel = stack.pop()
        directory = library / rel if rel else library
        try:
            with os.scandir(directory) as it:
                entries = sorted(it, key=lambda e: e.name)
        except OSError as exc:
            raise WarpzoneError(f"cannot read {directory}: {exc.strerror}") from exc
        for entry in entries:
            if junk(entry.name):
                continue
            path = f"{rel}/{entry.name}" if rel else entry.name
            yield path
            if entry.is_dir(follow_symlinks=False) and not prover.skip(path):
                stack.append(path)


def prove(library: Path, prover: wl.Prover, limit: int = DIFF_LIMIT) -> list[str]:
    """The first paths (up to `limit`) the old lines and the new model decide differently:
    every path of the walk, and the paths the lines themselves name."""
    found: list[str] = []
    for path in [*prover.probes(), *walk(library, prover)]:
        if prover.differs(path) and path not in found:
            found.append(path)
            if len(found) >= limit:
                break
    return found


# --------------------------------------------------------------------------- the migration


def _counts(model: wl.Model) -> str:
    games = sum(1 for i in model.includes if i.startswith(("roms/", "extras/switch/")))
    parts = [
        f"{len(model.systems)} system(s)",
        f"{games} game path(s)",
        f"{len(model.includes) - games} other path(s)",
        f"{len(model.excludes)} exclusion(s)",
    ]
    return ", ".join(parts)


def _line_text(line: wl.HandLine) -> str:
    return f"line {line.number}: {'!' if line.on else ''}{wl.escape(line.path)}"


def migrate_device(
    root: Path,
    device: str,
    *,
    dry_run: bool,
    now: datetime | None,
    out: Callable[[str], None],
    scan_client: Callable[[], Client] | None = None,
    warn: Callable[[str], None] = print,
) -> bool:
    """Migrate one device's whitelist; True when the file was refused."""
    path = whitelist_path(root, device)
    snapshot = read(path)

    def refuse(*reasons: str) -> bool:
        out(f"{device}: refused, {path.name} is left as it is")
        for reason in reasons:
            out(f"  {reason}")
        return True

    if snapshot.data is None:
        out(f"{device}: no {path.name} in {path.parent}; nothing to migrate")
        return False
    try:
        text = snapshot.data.decode("utf-8")
    except UnicodeDecodeError:
        return refuse("the file is not UTF-8 text")
    if wl.is_managed(text):
        out(f"{device}: already managed, left alone")
        return False
    hand = wl.parse_hand(text)
    if hand.errors:
        return refuse(*hand.errors, "rewrite those lines as literal paths and run it again")
    plan = wl.plan_migration(hand)
    try:
        new_text = wl.render(plan.model)
    except wl.Unlistable as exc:
        return refuse(str(exc))
    library = root / "library"
    if not library.is_dir():
        return refuse(f"{library} is not a folder; is --root the hub root?")
    prover = wl.Prover(hand.lines, plan.model)
    different = prove(library, prover)
    if different:
        return refuse(
            "the new file would not leave the same paths on; the first that differ:",
            *(f"  {p}" for p in different),
        )
    dropped = [
        *(f"dropped {_line_text(ln)} (shadowed by an earlier line)" for ln in plan.shadowed),
        *(f"dropped {_line_text(ln)} (an exclusion `*` already makes)" for ln in plan.redundant),
    ]
    if dry_run:
        out(f"{device}: would migrate ({_counts(plan.model)})")
        for line in dropped:
            out(f"  {line}")
        out(f"  new {path.name}:")
        for line in new_text.splitlines():
            out(f"  | {line}")
        return False
    result = write(root, device, new_text.encode("utf-8"), snapshot, now=now)
    if not result.ok:
        return refuse(f"{path.name} changed while it was being migrated; run it again")
    out(f"{device}: migrated ({_counts(plan.model)}); the old file is saved as {result.backup}")
    for line in dropped:
        out(f"  {line}")
    if scan_client is not None:
        try:
            warning = scan(scan_client(), device)
        except WarpzoneError as exc:  # the client could not even be built
            warning = str(exc)
        if warning:
            warn(warning)
    return False


def migrate(
    root: Path,
    devices: list[str],
    *,
    dry_run: bool,
    out: Callable[[str], None] = print,
    warn: Callable[[str], None] = print,
    scan_client: Callable[[], Client] | None = None,
    now: datetime | None = None,
) -> int:
    """Migrate the named devices' whitelists. Exit code: 1 when any file was refused."""
    refused = 0
    for device in devices:
        if migrate_device(
            root, device, dry_run=dry_run, now=now, out=out, scan_client=scan_client, warn=warn
        ):
            refused += 1
    return 1 if refused else 0


# --------------------------------------------------------------------------- check


def read_model(snapshot: Snapshot) -> wl.Model | None:
    """What a whitelist means, for a managed file or one the migration's parser reads."""
    if snapshot.data is None:
        return None
    try:
        return wl.read(snapshot.data.decode("utf-8")).model
    except UnicodeDecodeError:
        return None


def excluded_system_report(
    cfg: Config,
    profiles: Mapping[str, Profile],
    *,
    root: Path | None,
    home: Path,
) -> tuple[list[str], list[str]]:
    """Whitelists that name a system of the device's `exclude_systems`.

    Reads `<root>/library/patterns-<device>.stignore` when `root` is given, else the copy in
    the device's own library folder on this machine, where there is one. Returns the lines to
    print for the whitelists that do, and notes about those that could not be read.
    """
    problems: list[str] = []
    notes: list[str] = []
    for name, device in sorted(cfg.devices.items()):
        if not device.exclude_systems or "library" not in device.folders:
            continue
        candidates: list[Path] = []
        if root is not None:
            candidates.append(whitelist_path(root, name))
        library = profiles[device.profile].paths.get("library")
        if library:
            candidates.append(Path(expand_home(library, home)) / ignores.device_file(name))
        try:
            snapshot = next((s for s in map(read, candidates) if s.data is not None), None)
        except WhitelistIOError as exc:
            notes.append(f"{name}: {exc} (its whitelist was not checked for excluded systems)")
            continue
        if snapshot is None:
            continue
        model = read_model(snapshot)
        if model is None:
            notes.append(
                f"{name}: {snapshot.path.name} was not checked for excluded systems "
                "(not a whitelist warpzone can read; see `warpzone whitelist migrate`)"
            )
            continue
        found = wl.unsupported(model, device.exclude_systems)
        if not found:
            continue
        shown = ", ".join(f"{u.path} ({u.kind})" for u in found[:5])
        more = f" and {len(found) - 5} more" if len(found) > 5 else ""
        systems = ", ".join(sorted({u.system for u in found}))
        problems.append(
            f"{name}: {snapshot.path.name} names {systems}, in exclude_systems: {shown}{more}"
        )
    return problems, notes
