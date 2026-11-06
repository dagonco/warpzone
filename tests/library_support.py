"""Shared fixtures for the library API tests: a small catalog, a config and a hub tree."""

from __future__ import annotations

import posixpath
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from support import config_data
from warpzone import catalog
from warpzone import whitelist as wl
from warpzone.config import Config, parse_config
from warpzone.library_api import Library

GIB = 1 << 30


def game(
    system: str,
    path: str,
    title: str,
    size: int = 1000,
    *,
    kind: str = "file",
    extra: Sequence[tuple[str, str, int]] = (),
    flags: tuple[str, ...] = (),
    gamelist: str | None = None,
) -> catalog.Game:
    """A catalog game whose primary part is `path` (below `library/`)."""
    parts = [catalog.Part(path, kind, size, 1_700_000_000, files=1 if kind == "dir" else None)]
    parts += [catalog.Part(p, k, s, 1_700_000_000) for p, k, s in extra]
    return catalog.Game(
        id=catalog.game_id(path),
        title=title,
        key=catalog.title_key(title),
        primary=path,
        parts=tuple(parts),
        gamelist=gamelist or "./" + posixpath.basename(path),
        size=sum(p.size for p in parts),
        added="2026-10-28",
        flags=flags,
    )


def games() -> list[tuple[str, catalog.Game]]:
    return [
        (
            "psx",
            game("psx", "roms/psx/Example Game (Europe).chd", "Example Game (Europe)", 5 * GIB),
        ),
        ("psx", game("psx", "roms/psx/Other Game.chd", "Other Game", GIB)),
        ("psx", game("psx", "roms/psx/Metal Gear Solid (USA).chd", "Metal Gear Solid (USA)", GIB)),
        ("gbc", game("gbc", "roms/gbc/Metal Gear Solid (USA).gbc", "Metal Gear Solid (USA)", 1000)),
        ("gba", game("gba", "roms/gba/Tiny.gba", "Tiny", 4000)),
        (
            "ps4",
            game(
                "ps4",
                "roms/ps4/games/CUSA00000",
                "Example PS4",
                10 * GIB,
                kind="dir",
                extra=[("roms/ps4/shortcuts/Example PS4.desktop", "file", 100)],
                gamelist="./shortcuts/Example PS4.desktop",
            ),
        ),
        (
            "psx",
            game(
                "psx",
                "roms/psx/Bad\nName.chd",
                "Bad Name",
                10,
                flags=(catalog.UNLISTABLE,),
            ),
        ),
    ]


def library(items: Sequence[tuple[str, catalog.Game]] | None = None) -> Library:
    systems: dict[str, dict[str, catalog.Game]] = {}
    for system, g in items if items is not None else games():
        systems.setdefault(system, {})[g.id] = g
    return Library(dict(sorted(systems.items())))


def config(root: str = "/data/games", api_url: str = "http://127.0.0.1:1") -> Config:
    """Two devices with `library`: deck runs every system; machine leaves out ps4."""
    data: dict[str, Any] = config_data(api_url)
    data["hub"]["root"] = root
    data["devices"]["machine"]["exclude_systems"] = ["ps4"]
    return parse_config(data)


def managed(
    systems: Sequence[str] = (),
    includes: Sequence[str] = (),
    excludes: Sequence[str] = (),
) -> str:
    return wl.render(wl.Model(frozenset(systems), frozenset(includes), frozenset(excludes)))


def write_catalog(root: Path, items: Sequence[tuple[str, catalog.Game]] | None = None) -> None:
    by_system: dict[str, list[catalog.Game]] = {}
    for system, g in items if items is not None else games():
        by_system.setdefault(system, []).append(g)
    folder = root / "catalog" / "systems"
    folder.mkdir(parents=True, exist_ok=True)
    for system, found in by_system.items():
        (folder / f"{system}.json").write_bytes(
            catalog.encode(system, found, "2026-11-06T10:00:00Z")
        )
