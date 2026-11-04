"""Device state of the settings layer: snapshots, backups, log and lock.

Everything here lives on the device, outside every synced folder, under
`$XDG_STATE_HOME/warpzone/settings/`. Backups are whole files and may hold an
emulator's login tokens, so every file written here is mode 0600 and the state
directory is 0700. Nothing in this module touches the network or Syncthing.
See development/design.md, "Device state".
"""

from __future__ import annotations

import fcntl
import json
import os
import re
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any

from warpzone.errors import WarpzoneError
from warpzone.safefile import write_file

BACKUPS_KEPT = 10
LOG_MAX_BYTES = 1024 * 1024


class StateError(WarpzoneError):
    """A state file exists but cannot be trusted (corrupt, wrong shape)."""


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


def _write_json(path: Path, value: object) -> None:
    write_file(path, (json.dumps(value, indent=2, sort_keys=True) + "\n").encode())


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
