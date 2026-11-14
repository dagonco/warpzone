"""What doctor reads from the machine: `Probes`, whose defaults are the real thing.

Every check reads the machine through one `Probes` object (tools, processes, files, the clock,
the HTTP probe, the container marker), so a test fakes it and runs no tool. Also the platform
family that picks an install hint. See development/design/doctor.md ("Where inbox runs").
"""

from __future__ import annotations

import os
import platform as platform_mod
import shutil
import ssl
import subprocess
import sys
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from warpzone import runners

RUN_TIMEOUT_SECONDS = 10.0

# `warpzone inbox` runs in one of two places: on a computer that mounts the hub's share (its
# tools installed there) or in the hub container, whose image ships its own tools. Doctor tells
# them apart by the files a container runtime leaves in the root of the container (Docker and
# Podman), never by asking. See "Where inbox runs" in development/design/doctor.md.
CONTAINER_MARKERS = ("/.dockerenv", "/run/.containerenv")


def platform_family(system: str, os_release: Mapping[str, str]) -> str:
    """macos, steamos, debian, fedora, arch, linux (unknown Linux) or other."""
    if system == "Darwin":
        return "macos"
    if system != "Linux":
        return "other"
    ids = {os_release.get("ID", "").lower(), *os_release.get("ID_LIKE", "").lower().split()}
    if "steamos" in ids:
        return "steamos"
    for family, names in (
        ("debian", {"debian", "ubuntu"}),
        ("fedora", {"fedora", "rhel", "centos"}),
        ("arch", {"arch"}),
    ):
        if ids & names:
            return family
    return "linux"


@dataclass(frozen=True)
class RunOutput:
    returncode: int
    output: str  # stdout and stderr together


def _run(command: Sequence[str]) -> RunOutput:
    # Through `run_tool`: a tool of the user's may be a wrapper that starts other processes, and
    # when the limit runs out all of them are killed, not only the wrapper.
    proc = runners.run_tool(command, RUN_TIMEOUT_SECONDS, stdin=subprocess.DEVNULL)
    return RunOutput(proc.returncode, (proc.stdout or "") + (proc.stderr or ""))


def _http_status(url: str, headers: Mapping[str, str], timeout: float) -> int:
    """The HTTP status of a GET; raises OSError when nothing answers. Used for loopback
    only, where Syncthing's own certificate is self-signed."""
    context = None
    if url.startswith("https://"):
        context = ssl.create_default_context()
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
    request = urllib.request.Request(url, headers=dict(headers))
    try:
        with urllib.request.urlopen(request, timeout=timeout, context=context) as response:
            return int(response.status)
    except urllib.error.HTTPError as exc:
        return exc.code


def _read_os_release() -> dict[str, str]:
    values: dict[str, str] = {}
    for name in ("/etc/os-release", "/usr/lib/os-release"):
        try:
            text = Path(name).read_text(encoding="utf-8")
        except OSError:
            continue
        for line in text.splitlines():
            key, sep, value = line.partition("=")
            if sep and key and not key.startswith("#"):
                values[key] = value.strip().strip('"')
        break
    return values


def _is_executable(path: str) -> bool:
    return os.path.isfile(path) and os.access(path, os.X_OK)


def _is_writable(path: Path) -> bool:
    """Whether this process can create entries inside the folder `path`."""
    return os.access(path, os.W_OK | os.X_OK)


def _python_version() -> tuple[int, int, int]:
    v = sys.version_info
    return v.major, v.minor, v.micro


def _mtime(path: Path) -> float:
    return path.stat().st_mtime


def _read_link(path: Path) -> str | None:
    """Where the symlink `path` points, None when it is not one."""
    try:
        return os.readlink(path)
    except OSError:
        return None


def _list_dir(path: Path) -> list[str]:
    """Names inside `path` (broken symlinks included); empty when it cannot be listed."""
    try:
        return sorted(os.listdir(path))
    except OSError:
        return []


def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def _list_tree(path: Path) -> list[str]:
    """Every file under `path`, relative and with `/`; Syncthing's own folders are skipped."""
    found: list[str] = []
    for base, dirs, files in os.walk(path):
        dirs[:] = [d for d in dirs if d not in (".stfolder", ".stversions")]
        found += [(Path(base) / name).relative_to(path).as_posix() for name in files]
    return sorted(found)


def _in_container() -> bool:
    """Whether this process runs in a container (a runtime marker in the root)."""
    return any(os.path.exists(marker) for marker in CONTAINER_MARKERS)


def _disk_free(path: Path) -> tuple[int, int]:
    """(total, free) bytes of the volume holding `path`."""
    usage = shutil.disk_usage(path)
    return usage.total, usage.free


# Memory a machine needs to convert a DVD with chdman's FLAC codecs on: the peak was 1.7 times
# the image (3.2 GB for a 1.9 GB ISO), and a DVD is up to 8.5 GB.
MEMORY_LIMIT_FILES = (
    "/sys/fs/cgroup/memory.max",  # cgroup v2
    "/sys/fs/cgroup/memory/memory.limit_in_bytes",  # cgroup v1
)
NO_LIMIT_BYTES = 1 << 60  # cgroup v1 writes a number near 2**63 for "no limit"


def parse_memory_limit(text: str) -> int | None:
    """The bytes a cgroup memory file limits to, or None for `max`, a number so large that it
    means no limit, or anything that is not a number. Pure."""
    value = text.strip()
    if not value.isdigit():
        return None
    limit = int(value)
    return limit if 0 < limit < NO_LIMIT_BYTES else None


def _memory_limit() -> int | None:
    """The memory this process may use (its cgroup's limit), or None when there is none or it
    cannot be read."""
    for name in MEMORY_LIMIT_FILES:
        try:
            with open(name, encoding="ascii") as f:
                return parse_memory_limit(f.read())
        except OSError:
            continue
    return None


@dataclass
class Probes:
    """Everything a check reads from the machine. The defaults are the real thing."""

    config_path: Path
    which: Callable[[str], str | None] = shutil.which
    run: Callable[[Sequence[str]], RunOutput] = _run
    http_status: Callable[[str, Mapping[str, str], float], int] = _http_status
    env: Mapping[str, str] = field(default_factory=lambda: os.environ)
    home: Path = field(default_factory=Path.home)
    system: str = field(default_factory=platform_mod.system)
    machine: str = field(default_factory=platform_mod.machine)
    os_release: Mapping[str, str] = field(default_factory=_read_os_release)
    python: tuple[int, int, int] = field(default_factory=_python_version)
    now: Callable[[], float] = time.time
    is_dir: Callable[[Path], bool] = Path.is_dir
    is_file: Callable[[Path], bool] = Path.is_file
    is_writable: Callable[[Path], bool] = _is_writable
    is_executable: Callable[[str], bool] = _is_executable
    mtime: Callable[[Path], float] = _mtime
    list_dir: Callable[[Path], list[str]] = _list_dir
    read_link: Callable[[Path], str | None] = _read_link
    read_text: Callable[[Path], str] = _read_text
    list_tree: Callable[[Path], list[str]] = _list_tree
    disk_free: Callable[[Path], tuple[int, int]] = _disk_free
    in_container: Callable[[], bool] = _in_container
    memory_limit: Callable[[], int | None] = _memory_limit

    @property
    def family(self) -> str:
        return platform_family(self.system, self.os_release)
