"""Device state of the settings layer: snapshots, backups, log, lock and safe writes.

Everything here lives on the device, outside every synced folder, under
`$XDG_STATE_HOME/warpzone/settings/`. Backups are whole files and may hold an
emulator's login tokens, so every file written here is mode 0600 and the state
directory is 0700. Nothing in this module touches the network or Syncthing.
See docs/design.md, "Device state" and "Process detection".
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import stat as stat_mod
from collections.abc import Callable, Iterable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

BACKUPS_KEPT = 10
LOG_MAX_BYTES = 1024 * 1024
TMP_SUFFIX = ".wz-tmp"
COMM_MAX = 15  # the kernel keeps at most 15 characters of a process name


class StateError(Exception):
    """A state file exists but cannot be trusted (corrupt, wrong shape)."""


class NoProc(Exception):
    """There is no /proc to walk (macOS)."""


def state_dir(env: Mapping[str, str] | None = None, home: Path | None = None) -> Path:
    env = os.environ if env is None else env
    xdg = env.get("XDG_STATE_HOME", "")
    if xdg and os.path.isabs(xdg):
        base = Path(xdg)
    else:
        base = (home if home is not None else Path.home()) / ".local" / "state"
    return base / "warpzone" / "settings"


def _root(root: Path | None) -> Path:
    return root if root is not None else state_dir()


def _ensure_root(root: Path) -> None:
    root.mkdir(parents=True, exist_ok=True)
    os.chmod(root, 0o700)


def _safe(name: str) -> str:
    if not name or name.startswith(".") or "/" in name or "\0" in name or "\\" in name:
        raise ValueError(f"unsafe state name: {name!r}")
    return name


def _fsync_dir(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _write_private(path: Path, data: bytes) -> None:
    """Replace `path` atomically with `data`, mode 0600 (state files, not user files)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}{TMP_SUFFIX}")
    tmp.unlink(missing_ok=True)
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    _fsync_dir(path.parent)


def _write_json(path: Path, value: object) -> None:
    _write_private(path, (json.dumps(value, indent=2, sort_keys=True) + "\n").encode())


def _read_json(path: Path) -> Any:
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    try:
        return json.loads(text)
    except ValueError as exc:
        raise StateError(f"{path}: not valid JSON ({exc})") from exc


class Snapshots:
    """What each tracked key held when warpzone last synchronised it."""

    def __init__(self, root: Path | None = None) -> None:
        self.root = _root(root)

    def _path(self, emu: str, name: str) -> Path:
        return self.root / "snapshots" / _safe(emu) / f"{_safe(name)}.json"

    def _load(self, path: Path) -> dict[str, Any] | None:
        data = _read_json(path)
        if data is None:
            return None
        if not isinstance(data, dict):
            raise StateError(f"{path}: expected a JSON object")
        return data

    def _save(self, path: Path, values: Mapping[str, Any]) -> None:
        _ensure_root(self.root)
        _write_json(path, dict(values))

    def load(self, emu: str, file: str) -> dict[str, Any] | None:
        """The snapshot, or None when there is none yet (an empty one is `{}`)."""
        return self._load(self._path(emu, file))

    def save(self, emu: str, file: str, values: Mapping[str, Any]) -> None:
        self._save(self._path(emu, file), values)

    def load_data(self, emu: str) -> dict[str, str] | None:
        return self._load(self._path(emu, "data"))

    def save_data(self, emu: str, hashes: Mapping[str, str]) -> None:
        self._save(self._path(emu, "data"), hashes)


_BACKUP_NAME = re.compile(r"^(\d{8}-\d{6})(?:-(\d+))?-")


def _backup_order(name: str) -> tuple[str, int, str]:
    m = _BACKUP_NAME.match(name)
    if not m:
        return ("", 0, name)
    return (m.group(1), int(m.group(2) or 1), name)


def backup(
    path: Path,
    emu: str,
    file: str,
    *,
    keep: int = BACKUPS_KEPT,
    now: datetime | None = None,
    root: Path | None = None,
) -> Path:
    """Copy `path` (a symlink is followed) to the backups, mode 0600; keep the newest `keep`."""
    data = path.read_bytes()
    base = _root(root)
    _ensure_root(base)
    dest_dir = base / "backups" / _safe(emu) / _safe(file)
    dest_dir.mkdir(parents=True, exist_ok=True)
    stamp_text = (now or datetime.now()).strftime("%Y%m%d-%H%M%S")
    counter = 1
    while True:
        infix = stamp_text if counter == 1 else f"{stamp_text}-{counter}"
        dest = dest_dir / f"{infix}-{path.name}"
        try:
            fd = os.open(dest, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            break
        except FileExistsError:
            counter += 1
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
    except BaseException:
        dest.unlink(missing_ok=True)
        raise
    old = sorted(
        (p for p in dest_dir.iterdir() if p.is_file()), key=lambda p: _backup_order(p.name)
    )
    for stale in old[: max(0, len(old) - keep)]:
        stale.unlink(missing_ok=True)
    return dest


@dataclass(frozen=True)
class FileStamp:
    size: int
    mtime_ns: int
    sha256: str


def stamp(path: Path) -> FileStamp:
    """Size, modification time and sha256 of the file (a symlink is followed)."""
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        st = os.fstat(fh.fileno())
        for chunk in iter(lambda: fh.read(1 << 16), b""):
            digest.update(chunk)
    return FileStamp(st.st_size, st.st_mtime_ns, digest.hexdigest())


def atomic_write(path: Path, data: bytes, *, expect: FileStamp | None) -> bool:
    """Replace `path` with `data` unless it changed since `expect` was taken.

    `expect=None` means the file must not exist (it is created, with its directories).
    Returns False, leaving everything as it was, when the file changed, vanished or
    appeared. A symlink is written through to its target and stays a link. The
    replacement keeps the original mode; a temporary `.<name>.wz-tmp` beside it is
    synced, compared against `expect` one last time, then moved over the target.
    """
    target = Path(os.path.realpath(path))
    if expect is None:
        if os.path.lexists(target):
            return False
        mode = 0o644
        target.parent.mkdir(parents=True, exist_ok=True)
    else:
        try:
            mode = stat_mod.S_IMODE(target.stat().st_mode)
        except FileNotFoundError:
            return False
    tmp = target.with_name(f".{target.name}{TMP_SUFFIX}")
    tmp.unlink(missing_ok=True)
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        with os.fdopen(fd, "wb") as fh:
            os.fchmod(fh.fileno(), mode)
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        if expect is None:
            if os.path.lexists(target):
                tmp.unlink(missing_ok=True)
                return False
        else:
            try:
                unchanged = stamp(target) == expect
            except FileNotFoundError:
                unchanged = False
            if not unchanged:
                tmp.unlink(missing_ok=True)
                return False
        os.replace(tmp, target)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    _fsync_dir(target.parent)
    return True


class Log:
    """One line per run and per change, never a value. Rotates at `max_bytes`, one old copy."""

    def __init__(
        self,
        path: Path | None = None,
        *,
        max_bytes: int = LOG_MAX_BYTES,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.path = path if path is not None else state_dir() / "log"
        self.max_bytes = max_bytes
        self.clock = clock or datetime.now

    def line(self, msg: str) -> None:
        text = f"{self.clock().isoformat(timespec='seconds')} {' '.join(msg.split())}\n"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            if self.path.stat().st_size >= self.max_bytes:
                os.replace(self.path, self.path.with_name(self.path.name + ".1"))
        except FileNotFoundError:
            pass
        fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        with os.fdopen(fd, "a", encoding="utf-8") as fh:
            fh.write(text)


@contextmanager
def lock(blocking: bool = False, root: Path | None = None) -> Iterator[bool]:
    """Hold the advisory lock; yields False at once when someone else holds it."""
    base = _root(root)
    _ensure_root(base)
    fd = os.open(base / "lock", os.O_RDWR | os.O_CREAT, 0o600)
    got = False
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
            got = True
        except BlockingIOError:
            pass
        yield got
    finally:
        if got:
            fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def _parent_of(proc: Path, pid: int) -> int | None:
    try:
        text = (proc / str(pid) / "stat").read_text()
        return int(text.rpartition(")")[2].split()[1])
    except (OSError, ValueError, IndexError):
        return None


def running(
    names: Iterable[str],
    proc: Path = Path("/proc"),
    self_pid: int | None = None,
    parent_pid: int | None = None,
    *,
    skip_ancestors: bool = False,
) -> bool:
    """Whether a process is one of `names`, judged by `comm` and the basename of `argv[0]`.

    The rest of a command line is never searched, so a shell, an editor or a launcher
    script that merely mentions the emulator does not count. Our own process and its
    parent are skipped, and with `skip_ancestors` every process above it: a program that
    runs this from a script and waits for it. Entries that cannot be read are ignored.
    """
    wanted = set(names)
    wanted_comm = {n[:COMM_MAX] for n in wanted}
    if not wanted:
        return False
    if not proc.is_dir():
        raise NoProc(f"{proc} is not available")
    if self_pid is None:
        self_pid = os.getpid()
        if parent_pid is None:
            parent_pid = os.getppid()
    elif parent_pid is None:
        parent_pid = _parent_of(proc, self_pid)
    skip = {self_pid, parent_pid}
    while skip_ancestors and parent_pid and parent_pid > 1:
        parent_pid = _parent_of(proc, parent_pid)
        if parent_pid in skip:
            break
        skip.add(parent_pid)
    try:
        entries = list(proc.iterdir())
    except OSError as exc:
        raise NoProc(f"{proc} cannot be read: {exc}") from exc
    for entry in entries:
        if not entry.name.isdigit() or int(entry.name) in skip:
            continue
        try:
            comm = (entry / "comm").read_text(errors="replace").rstrip("\n")
        except OSError:
            comm = None
        if comm is not None and comm in wanted_comm:
            return True
        try:
            raw = (entry / "cmdline").read_bytes()
        except OSError:
            continue
        argv0 = raw.split(b"\0", 1)[0].decode(errors="replace")
        if argv0 and os.path.basename(argv0) in wanted:
            return True
    return False


def write_last_apply(info: Mapping[str, Any], root: Path | None = None) -> None:
    base = _root(root)
    _ensure_root(base)
    _write_json(base / "last-apply.json", dict(info))


def read_last_apply(root: Path | None = None) -> dict[str, Any] | None:
    """The last apply summary, or None when missing or unreadable."""
    try:
        data = _read_json(_root(root) / "last-apply.json")
    except (StateError, OSError, UnicodeDecodeError):
        return None
    return data if isinstance(data, dict) else None
