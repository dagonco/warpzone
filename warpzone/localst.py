"""Find and parse the local Syncthing config.xml to reach its REST API."""

from __future__ import annotations

import os
import sys
import xml.etree.ElementTree as ET
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path


class LocalSyncthingError(Exception):
    pass


@dataclass(frozen=True)
class LocalSyncthing:
    config_path: Path
    base_url: str
    api_key: str = field(repr=False)


def candidate_paths(
    env: Mapping[str, str] | None = None,
    home: Path | None = None,
    platform: str | None = None,
) -> list[Path]:
    """Default config.xml locations, most specific first."""
    env = os.environ if env is None else env
    home = Path.home() if home is None else home
    platform = sys.platform if platform is None else platform

    found: list[Path] = []
    for var in ("STHOMEDIR", "STCONFDIR"):
        if env.get(var):
            found.append(Path(env[var]) / "config.xml")
    if platform == "darwin":
        found.append(home / "Library" / "Application Support" / "Syncthing" / "config.xml")
    state = Path(env["XDG_STATE_HOME"]) if env.get("XDG_STATE_HOME") else home / ".local" / "state"
    config = Path(env["XDG_CONFIG_HOME"]) if env.get("XDG_CONFIG_HOME") else home / ".config"
    found += [state / "syncthing" / "config.xml", config / "syncthing" / "config.xml"]
    # SyncThingy (Flatpak) runs Syncthing with the sandbox's own XDG dirs.
    flatpak = home / ".var" / "app" / "com.github.zocker_160.SyncThingy"
    found += [
        flatpak / ".local" / "state" / "syncthing" / "config.xml",
        flatpak / "config" / "syncthing" / "config.xml",
    ]
    return found


def find_config(
    explicit: Path | None = None,
    *,
    env: Mapping[str, str] | None = None,
    home: Path | None = None,
    platform: str | None = None,
) -> Path:
    if explicit is not None:
        path = explicit / "config.xml" if explicit.is_dir() else explicit
        if not path.is_file():
            raise LocalSyncthingError(f"no Syncthing config at {path}")
        return path
    candidates = candidate_paths(env, home, platform)
    for path in candidates:
        if path.is_file():
            return path
    searched = "\n  ".join(str(p) for p in candidates)
    raise LocalSyncthingError(
        f"local Syncthing config.xml not found; searched:\n  {searched}\n"
        "pass --syncthing-config PATH to point at it"
    )


def parse_config(path: Path) -> LocalSyncthing:
    try:
        root = ET.parse(path).getroot()
    except (OSError, ET.ParseError) as exc:
        raise LocalSyncthingError(f"cannot read {path}: {exc}") from exc
    gui = root.find("gui")
    if gui is None:
        raise LocalSyncthingError(f"{path}: no <gui> section")
    api_key = (gui.findtext("apikey") or "").strip()
    address = (gui.findtext("address") or "").strip()
    if not api_key:
        raise LocalSyncthingError(f"{path}: <gui> has no <apikey>")
    if not address or address.startswith("/"):
        raise LocalSyncthingError(f"{path}: unsupported GUI address {address!r}")
    host, _, port = address.rpartition(":")
    if not host or not port.isdigit():
        raise LocalSyncthingError(f"{path}: unsupported GUI address {address!r}")
    if host in ("0.0.0.0", "[::]", "::"):  # listening everywhere: loopback still works
        host = "127.0.0.1"
    scheme = "https" if gui.get("tls") == "true" else "http"
    return LocalSyncthing(path, f"{scheme}://{host}:{port}", api_key)
