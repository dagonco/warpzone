"""A temporary hub and a stand-in `Skyscraper` for the scraping tests (no network, no real tool).

The stand-in is a Python script that behaves like the three calls warpzone makes: `--version`, the
gather (prints the lines Skyscraper prints for each game of the `--includefrom` list) and the
two generating passes (write a gamelist with absolute paths, as Skyscraper does, and media files
into the `-g` and `-o` it is given). What it does is set by a `canned.json` beside it, and every
call is logged to `calls.jsonl`. It refuses the credential options.
"""

from __future__ import annotations

import io
import json
import os
import sys
import tempfile
import time
import unittest
from datetime import date
from pathlib import Path
from typing import Any

from library_support import game
from warpzone import catalog, catalog_store, scrape_io
from warpzone.report import Reporter

TODAY = date(2026, 11, 13)
KIRBY = "Kirby Super Star (USA).zip"
CHRONO = "Chrono Trigger (USA).zip"
HOMEBREW = "Example Homebrew (World).zip"
FAKE_CREDS = "fakeuser:fakepassword"  # gitleaks:allow
FAKE_CONFIG = f'[main]\nlangPrios="es,en"\n\n[screenscraper]\nuserCreds="{FAKE_CREDS}"\n'
BANNER = "Skyscraper \r _______ __\n|:  1   |   |__|       3.21.1   |\n`---' by contributors\n"

STAND_IN = r"""#!{python}
import json, os, sys
from pathlib import Path

here = Path(__file__).resolve().parent
canned = json.loads((here / "canned.json").read_text())
args = sys.argv[1:]
with open(here / "calls.jsonl", "a") as log:
    log.write(json.dumps(args) + "\n")
if "--version" in args:
    sys.stdout.write({banner!r})
    sys.exit(0)
if any(a in args for a in ("-u", "-c", "-t")):
    sys.exit(9)


def opt(name):
    return args[args.index(name) + 1] if name in args else None


def stem(path):
    return os.path.splitext(os.path.basename(path))[0]


staged = [l for l in Path(opt("--includefrom")).read_text().splitlines() if l]
if canned.get("hang"):
    import time
    time.sleep(120)
if "-s" in args:  # the gather
    targets = {{os.path.basename(p): os.path.realpath(p) for p in staged}}
    siblings = sorted(os.listdir(os.path.dirname(opt("-i"))))
    with open(here / "gather.jsonl", "a") as log:
        entry = {{"input": opt("-i"), "targets": targets, "siblings": siblings}}
        log.write(json.dumps(entry) + "\n")
    known = set(canned.get("known", []))
    for n, path in enumerate(staged):
        if canned.get("quota_after") is not None and n >= canned["quota_after"]:
            print("\033[1;31mYour daily ScreenScraper request limit has been reached, "
                  "exiting nicely...\033[0m\n")
            break
        if canned.get("down"):
            print("\033[1;33mRetrying request with timeout of 60s...\033[0m\n")
        if stem(path) in known:
            print("\033[1;34m---- Game '%s' found! :) ----\033[0m" % stem(path))
        else:
            print("\033[1;33m---- Game '%s' not found :( ----\033[0m\n" % stem(path))
    sys.exit(canned.get("gather_exit", 0))

cached = [p for p in staged if stem(p) in set(canned.get("cached", canned.get("known", [])))]
out = Path(opt("-o"))
gl = Path(opt("-g"))
art = os.path.basename(opt("-a"))


def put(rel, data):
    target = out / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(data)


if art == "artwork-esde.xml":
    lines = ["<?xml version='1.0' encoding='UTF-8'?>", "<gameList>"]
    for path in cached:
        name = stem(path)
        lines += [
            "<game>",
            "<path>%s</path>" % path,
            "<name>%s</name>" % canned.get("names", {{}}).get(name, name),
            "<desc>About %s &amp; more</desc>" % name,
            "<image>./covers/%s.png</image>" % name,
            "<playcount>99</playcount>",
            "</game>",
        ]
        for kind, ext in (("covers", "png"), ("screenshots", "png"), ("miximages", "png"),
                          ("marquees", "png"), ("backcovers", "png"), ("videos", "mp4")):
            if kind in canned.get("kinds", ["covers", "screenshots", "miximages", "marquees",
                                            "backcovers", "videos"]):
                put("%s/%s.%s" % (kind, name, ext), ("main:%s:%s" % (kind, name)).encode())
        if canned.get("symlink_video"):
            (out / "videos" / (name + ".mp4")).unlink()
            os.symlink("/nonexistent", out / "videos" / (name + ".mp4"))
    lines.append("</gameList>")
    gl.mkdir(parents=True, exist_ok=True)
    (gl / "gamelist.xml").write_text("\n".join(lines) + "\n")
else:
    for path in cached:
        put("covers/%s.png" % stem(path), ("box:%s" % stem(path)).encode())
"""


class Hub:
    """A temporary hub with a `snes` library, its catalog, and the stand-in tool."""

    def __init__(self, base: Path) -> None:
        self.base = base
        self.root = base / "hub"
        self.cache = base / "cache"
        self.work = base / "work"
        self.tool_dir = base / "tool"
        self.config_ini = base / "config.ini"
        for folder in (self.root / "inbox", self.root / "library" / "roms" / "snes", self.tool_dir):
            folder.mkdir(parents=True)
        self.tool = self.tool_dir / "Skyscraper"
        self.tool.write_text(STAND_IN.format(python=sys.executable, banner=BANNER))
        self.tool.chmod(0o755)
        self.canned({})
        self.games: list[catalog.Game] = []

    # ------------------------------------------------------------ the tool

    def canned(self, values: dict[str, Any]) -> None:
        (self.tool_dir / "canned.json").write_text(json.dumps(values))

    def calls(self) -> list[list[str]]:
        log = self.tool_dir / "calls.jsonl"
        return [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []

    def gathers(self) -> list[dict[str, Any]]:
        log = self.tool_dir / "gather.jsonl"
        return [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []

    # ------------------------------------------------------------ the library

    def add_game(self, name: str, system: str = "snes", content: bytes = b"rom") -> catalog.Game:
        path = f"roms/{system}/{name}"
        (self.root / "library" / "roms" / system).mkdir(parents=True, exist_ok=True)
        (self.root / "library" / path).write_bytes(content)
        g = game(system, path, name.rsplit(".", 1)[0], size=len(content))
        self.games.append(g)
        self.write_catalog(system)
        return g

    def write_catalog(self, system: str = "snes") -> None:
        target = catalog_store.system_path(self.root, system)
        target.parent.mkdir(parents=True, exist_ok=True)
        mine = [g for g in self.games if g.primary.startswith(f"roms/{system}/")]
        target.write_bytes(catalog.encode(system, mine, "2026-11-13T00:00:00+00:00"))

    # ------------------------------------------------------------ the hub's frontend files

    def gamelist(self, system: str = "snes") -> Path:
        return scrape_io.gamelist_path(self.root, system)

    def write_gamelist(self, text: str, system: str = "snes") -> Path:
        path = self.gamelist(system)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
        return path

    def media(self, kind: str, stem: str, ext: str = "png", system: str = "snes") -> Path:
        return scrape_io.media_path(self.root, system) / kind / f"{stem}.{ext}"

    def put_media(self, kind: str, stem: str, data: bytes = b"hub", ext: str = "png") -> Path:
        path = self.media(kind, stem, ext)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return path

    def snapshot(self) -> dict[str, bytes | None]:
        """Every file and folder below the base, to see that a run changed nothing."""
        found: dict[str, bytes | None] = {}
        for folder, dirs, files in os.walk(self.base):
            for name in dirs:
                found[str((Path(folder) / name).relative_to(self.base))] = None
            for name in files:
                path = Path(folder) / name
                found[str(path.relative_to(self.base))] = (
                    path.read_bytes() if path.exists() else b""
                )
        return found

    # ------------------------------------------------------------ running

    def options(self, **overrides: Any) -> scrape_io.Options:
        values: dict[str, Any] = {
            "root": self.root,
            "skyscraper": str(self.tool),
            "systems": ("snes",),
            "explicit": True,
            "timeout": 60.0,
            "work_dir": self.work,
            "cache_dir": self.cache,
            "config_ini": self.config_ini,
            "today": lambda: TODAY,
            # A day later than the files the test just wrote, so no gamelist counts as "recent".
            "now": lambda: time.time() + 86400,
        }
        values.update(overrides)
        return scrape_io.Options(**values)

    def run(self, **overrides: Any) -> tuple[int, list[str], Reporter]:
        lines: list[str] = []
        rep = Reporter(lines.append)
        code = scrape_io.run_scrape(self.options(**overrides), rep)
        return code, [line for chunk in lines for line in chunk.splitlines()], rep


class HubCase(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.hub = Hub(Path(tmp.name))
        self.stream = io.StringIO()
