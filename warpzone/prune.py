"""Remove files a device no longer whitelists, asking Syncthing what it ignores.

When a pattern leaves a device's whitelist, Syncthing stops syncing the files but
leaves the copies on disk. A file is a leftover only when Syncthing itself has
flagged it as ignored in the folder database after a full rescan (`local.ignored`
in `GET /rest/db/file`) AND the hub still has the same file (`global`). Nothing
here re-implements Syncthing's ignore matcher; see docs/design.md.
"""

from __future__ import annotations

import dataclasses
import enum
import fnmatch
import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from warpzone.config import Config
from warpzone.model import FolderSpec, device_specs
from warpzone.profiles import Profile
from warpzone.provision import Reporter, expand_home, in_sync
from warpzone.syncthing import Client, SyncthingError, scrub

GIB = 1024**3
DEFAULT_MAX_GIB = 50.0
DEFAULT_RESCAN_TIMEOUT = 600.0
# The hub's modification time is restored on the device when a file is pulled; FAT
# stores seconds in steps of two, hence the tolerance.
MTIME_TOLERANCE_S = 2.0
# Never offered for deletion, whatever the database says.
PROTECTED_ROOT = (".stfolder", ".stignore", ".stversions")
PROTECTED_ROOT_GLOBS = ("patterns-*.stignore",)
SCANNING = ("scanning", "scan-waiting")
SHOWN_GROUPS = 30
SHOWN_KEPT = 20

_TYPE_FILE = "FILE_INFO_TYPE_FILE"
_TYPE_SYMLINK = "FILE_INFO_TYPE_SYMLINK"
_TIME = re.compile(r"^(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d)(?:\.(\d+))?(Z|[+-]\d\d:\d\d)$")


class PruneError(Exception):
    pass


class Verdict(enum.Enum):
    UNTRACKED = "untracked"  # Syncthing has no record: ignored or created since the rescan
    WANTED = "wanted"  # Syncthing syncs it
    LEFTOVER = "leftover"
    LOCAL_ONLY = "local-only"  # ignored, but the hub does not have it
    DIFFERS = "differs"  # ignored, but not the hub's copy


@dataclass(frozen=True)
class Entry:
    path: str  # relative to the folder root, "/" separated, as Syncthing names it
    kind: str  # "file" or "symlink"
    size: int
    mtime: float


@dataclass
class FolderReport:
    folder_id: str
    root: str
    leftovers: list[Entry] = field(default_factory=list)
    kept: list[tuple[str, str]] = field(default_factory=list)  # (path, reason)
    untracked: int = 0
    deleted_files: int = 0
    deleted_bytes: int = 0
    removed_dirs: int = 0

    @property
    def bytes(self) -> int:
        return sum(e.size for e in self.leftovers)


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


def classify(entry: Entry, info: dict[str, Any] | None) -> tuple[Verdict, str]:
    """Pure decision from one `db/file` answer and what is on disk."""
    if not info:
        return Verdict.UNTRACKED, ""
    if not (info.get("local") or {}).get("ignored"):
        return Verdict.WANTED, ""
    hub = info.get("global") or {}
    # A local-only file that was scanned before it got ignored shows up in `global`
    # too, flagged invalid or ignored. That is not the hub's copy.
    if not hub or hub.get("deleted") or hub.get("ignored") or hub.get("invalid"):
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
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> None:
    """Full rescan, then wait for it. Ignore flags are only refreshed by a full scan."""
    before = client.db_status(folder_id).get("stateChanged")
    client.scan(folder_id)
    start = clock()
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
            seen or st.get("stateChanged") != before or clock() - start >= grace
        ):
            return
        if clock() - start >= timeout:
            raise PruneError(f"the rescan did not finish within {timeout:.0f}s")
        sleep(poll)


def _norm(path: str) -> str:
    return os.path.normpath(path)


def inspect_folder(
    client: Client,
    spec: FolderSpec,
    configured: dict[str, dict[str, Any]],
    *,
    home: Path,
    rescan_timeout: float,
    poll: float,
    grace: float,
    sleep: Callable[[float], None],
    clock: Callable[[], float],
) -> FolderReport:
    """Check the folder is in its normal whitelisted state, rescan, classify every file."""
    current = configured.get(spec.id)
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
    marker = current.get("markerName") or ".stfolder"
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

    rescan(
        client, spec.id, timeout=rescan_timeout, poll=poll, grace=grace, sleep=sleep, clock=clock
    )

    skip = {
        _norm(expand_home(f.get("path", ""), home))
        for fid, f in configured.items()
        if fid != spec.id
    }
    report = FolderReport(spec.id, root)
    for entry in walk(root, skip, marker):
        verdict, reason = classify(entry, client.db_file(spec.id, entry.path))
        if verdict is Verdict.LEFTOVER:
            report.leftovers.append(entry)
        elif verdict is Verdict.UNTRACKED:
            report.untracked += 1
        elif verdict is not Verdict.WANTED:
            report.kept.append((entry.path, reason))
    return report


def _inside(root: str, path: str) -> bool:
    real_root = os.path.realpath(root)
    return os.path.commonpath([real_root, os.path.realpath(path)]) == real_root


def apply_folder(
    client: Client, report: FolderReport, marker: str = ".stfolder", rep: Reporter | None = None
) -> None:
    """Delete the leftovers, re-checking each one right before it goes."""
    parents: set[str] = set()
    for entry in report.leftovers:
        full = os.path.join(report.root, entry.path)
        try:
            st = os.lstat(full)
            fresh = dataclasses.replace(entry, size=st.st_size, mtime=st.st_mtime)
            verdict, _ = classify(fresh, client.db_file(report.folder_id, entry.path))
            if verdict is not Verdict.LEFTOVER or not _inside(report.root, os.path.dirname(full)):
                continue
            os.unlink(full)
        except FileNotFoundError:
            continue
        except (OSError, SyncthingError) as exc:
            if rep:
                rep.warn(f"{report.folder_id}: cannot delete {entry.path}: {exc}")
            continue
        report.deleted_files += 1
        report.deleted_bytes += entry.size
        parent = os.path.dirname(entry.path)
        while parent:
            parents.add(parent)
            parent = os.path.dirname(parent)

    # Deepest first, so a directory is looked at after its children. Only
    # directories Syncthing ignores are removed, and only when rmdir finds them empty.
    for rel in sorted(parents, key=lambda p: p.count("/"), reverse=True):
        if _protected(rel, marker):
            continue
        try:
            info = client.db_file(report.folder_id, rel)
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


def print_report(report: FolderReport, rep: Reporter, *, list_all: bool) -> None:
    n = len(report.leftovers)
    rep.out(f"{report.folder_id}: {n} leftover file(s), {human(report.bytes)}")
    if list_all:
        for entry in report.leftovers:
            rep.out(f"  {entry.path}")
    else:
        groups: dict[str, list[int]] = {}
        for entry in report.leftovers:
            count, size = groups.setdefault(_group(entry.path), [0, 0])
            groups[_group(entry.path)] = [count + 1, size + entry.size]
        ranked = sorted(groups.items(), key=lambda kv: (-kv[1][1], kv[0]))
        for name, (count, size) in ranked[:SHOWN_GROUPS]:
            rep.out(f"  {name}: {count} file(s), {human(size)}")
        if len(ranked) > SHOWN_GROUPS:
            rep.out(f"  ... and {len(ranked) - SHOWN_GROUPS} more folder(s), use --list")
    if report.kept:
        rep.out(
            f"{report.folder_id}: kept {len(report.kept)} ignored file(s) the hub cannot restore"
        )
        for path, reason in report.kept[: len(report.kept) if list_all else SHOWN_KEPT]:
            rep.out(f"  {path} ({reason})")
        if not list_all and len(report.kept) > SHOWN_KEPT:
            rep.out(f"  ... and {len(report.kept) - SHOWN_KEPT} more, use --list")
    if report.untracked:
        rep.out(
            f"{report.folder_id}: {report.untracked} file(s) not known to Syncthing, left alone"
        )


def notify(url: str, payload: dict[str, Any], *, timeout: float = 10.0) -> None:
    """POST a JSON summary. The URL may embed a secret (webhook id): it is never echoed."""
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            resp.read()
    except urllib.error.HTTPError as exc:
        exc.close()
        raise PruneError(f"notification rejected: HTTP {exc.code}") from None
    except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
        reason = scrub(str(getattr(exc, "reason", exc)), (url, urllib.parse.urlsplit(url).path))
        raise PruneError(f"notification failed: {reason}") from None


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
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> int:
    """Report (default) or delete leftovers in every whitelisted folder. Returns problems."""
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
    problems = 0
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
                sleep=sleep,
                clock=clock,
            )
        except PruneError as exc:
            rep.warn(f"{spec.id}: skipped, {exc}")
            problems += 1
            continue
        print_report(report, rep, list_all=list_all)
        reports.append(report)

    total_files = sum(len(r.leftovers) for r in reports)
    total_bytes = sum(r.bytes for r in reports)
    rep.out(f"total: {total_files} leftover file(s), {human(total_bytes)}")

    deleting = apply and total_files > 0
    if deleting and total_bytes > max_gib * GIB and not force:
        rep.warn(
            f"refusing to delete {human(total_bytes)}: more than --max-gib {max_gib:g}. "
            "Check the report, then raise --max-gib or pass --force"
        )
        problems += 1
        deleting = False
    if deleting:
        for report in reports:
            marker = (configured[report.folder_id].get("markerName")) or ".stfolder"
            apply_folder(local, report, marker, rep)
            rep.out(
                f"{report.folder_id}: deleted {report.deleted_files} file(s), "
                f"{human(report.deleted_bytes)}, removed {report.removed_dirs} empty folder(s)"
            )
            if report.deleted_files < len(report.leftovers):
                problems += 1
                rep.warn(
                    f"{report.folder_id}: {len(report.leftovers) - report.deleted_files} "
                    "file(s) were not deleted (changed meanwhile or a delete failed)"
                )
    elif total_files and not apply:
        rep.out("report only: run again with --apply to delete")

    if notify_url:
        for report in reports:
            if not report.leftovers:
                continue
            done = deleting
            payload = {
                "device": name,
                "folder": report.folder_id,
                "files": report.deleted_files if done else len(report.leftovers),
                "bytes": report.deleted_bytes if done else report.bytes,
                "applied": done,
            }
            try:
                notify(notify_url, payload)
            except PruneError as exc:
                rep.warn(str(exc))
    return problems
