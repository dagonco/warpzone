"""The doctor checks of the external tools `inbox` runs: `ToolSpec`, `TOOLS`, `check_tool`.

Adding a tool is one `ToolSpec` entry in `TOOLS`. See development/design/doctor.md ("Where
inbox runs").
"""

from __future__ import annotations

import re
import subprocess
from collections.abc import Callable, Mapping
from dataclasses import dataclass

from warpzone import inbox_core, inbox_vita_pkg
from warpzone.config import INBOX_KEYS, InboxSettings
from warpzone.doctor_core import FAIL, G_TOOLS, OK, OPTIONAL, WARN, Context, Result
from warpzone.doctor_hints import (
    BSDTAR_HINTS,
    CHDMAN_HINTS,
    DOLPHIN_HINTS,
    IMAGE_MISSING_TOOL,
    NSZ_HINTS,
    ORBIS_PKG_UTIL_HINTS,
    RAHASHER_HINTS,
    VITA3K_HINTS,
    HintTable,
    hint_for,
)
from warpzone.paths import expand_home

# Where the hub image puts the tools it ships, by `ToolSpec.key` (docker/Dockerfile). Any other
# tool the image gets is on its PATH.
IMAGE_TOOL_PATHS: Mapping[str, str] = {"RAHasher": "/usr/local/bin/RAHasher"}


def vita3k_usable(path: str, family: str) -> str | None:
    """Why `inbox` would refuse to run Vita3K at `path` on this platform, or None."""
    system = {"macos": "darwin", "other": "other"}.get(family, "linux")
    tool = inbox_vita_pkg.resolve_tool(path)
    return inbox_vita_pkg.vita3k_problem(tool, system, inbox_vita_pkg.tool_head(tool))


@dataclass(frozen=True)
class ToolSpec:
    """One external tool. Data, so adding a tool is one entry in `TOOLS`."""

    key: str  # name used in the report and, if `setting`, the [inbox] key
    commands: tuple[str, ...]  # names to look up on PATH
    purpose: str
    hints: HintTable
    setting: str | None = None  # [inbox] key that can point at the binary
    # Run with these arguments to read a version line; None: do not run it, show the path.
    version_args: tuple[str, ...] | None = ()
    # A fail (not just optional) when missing and an [inbox] table exists.
    required_for_inbox: bool = False
    # `inbox` itself looks the tool up on PATH only when it is configured (RAHasher).
    inbox_uses_path: bool = True
    # Where an installer puts it without adding it to PATH.
    known_paths: tuple[str, ...] = ()
    note: str = ""
    # The path `inbox` uses when the setting is unset, by platform family, before PATH.
    defaults: tuple[tuple[str, str], ...] = ()
    # The program to run for a configured path (the binary inside an app bundle).
    executable: Callable[[str], str] | None = None
    # Why inbox would refuse the tool found (path, platform family), or None.
    usable: Callable[[str, str], str | None] | None = None


TOOLS: tuple[ToolSpec, ...] = (
    ToolSpec(
        "chdman",
        ("chdman",),
        "converts discs to CHD (PlayStation, PlayStation 2, Saturn, Dreamcast)",
        CHDMAN_HINTS,
        setting="chdman",
        required_for_inbox=True,
    ),
    ToolSpec(
        "bsdtar",
        ("bsdtar",),
        "unpacks .7z and .rar",
        BSDTAR_HINTS,
        setting="bsdtar",
        version_args=("--version",),
        required_for_inbox=True,
    ),
    ToolSpec(
        "RAHasher",
        ("RAHasher", "rahasher"),
        "RetroAchievements hash check (optional)",
        RAHASHER_HINTS,
        setting="rahasher",
        inbox_uses_path=False,
    ),
    ToolSpec(
        "dolphin-tool",
        ("dolphin-tool",),
        "only for compressed GameCube images (.gcz)",
        DOLPHIN_HINTS,
        setting="dolphin_tool",
        version_args=None,
        known_paths=("/Applications/Dolphin.app/Contents/MacOS/dolphin-tool",),
    ),
    ToolSpec(
        "nsz",
        ("nsz",),
        "Switch: reads .nsp/.nsz metadata and decompresses .nsz in inbox",
        NSZ_HINTS,
        setting="nsz",
        version_args=None,
    ),
    ToolSpec(
        "orbis-pkg-util",
        ("orbis-pkg-util",),
        "PS4: unpacks .pkg games into folders shadPS4 can run",
        ORBIS_PKG_UTIL_HINTS,
        setting="orbis_pkg_util",
        version_args=None,
        known_paths=("~/.cargo/bin/orbis-pkg-util",),
    ),
    ToolSpec(
        "Vita3K",
        ("vita3k", "Vita3K"),
        "PS Vita: installs .pkg games with their zRIF (vita_zrif in [inbox]); Vita3K.app on "
        "macOS, a plain binary (not an AppImage) on Linux",
        VITA3K_HINTS,
        setting="vita3k",
        version_args=None,  # it would write its config in the user's data folder
        defaults=(("macos", inbox_core.VITA3K_APP),),
        executable=inbox_core.vita3k_executable,
        usable=vita3k_usable,
    ),
)

_VERSION = re.compile(r"\d+(?:\.\d+)+")


def _first_line(text: str) -> str:
    for line in text.splitlines():
        if line.strip():
            return line.strip()[:100]
    return ""


def _configured_path(spec: ToolSpec, settings: InboxSettings) -> str | None:
    return getattr(settings, spec.setting) if spec.setting in INBOX_KEYS else None


def known_tool_paths(spec: ToolSpec, container: bool) -> tuple[str, ...]:
    """Where `spec` may sit without being on PATH: the places an installer uses on a computer,
    and in the hub container the path the image puts it at. Pure."""
    image = IMAGE_TOOL_PATHS.get(spec.key)
    if container:
        return (image, *spec.known_paths) if image else spec.known_paths
    return spec.known_paths


def check_tool(ctx: Context, spec: ToolSpec) -> Result:
    p = ctx.probes
    inbox = ctx.inbox_configured
    required = spec.required_for_inbox and inbox
    missing = FAIL if required else OPTIONAL
    container = p.in_container()
    hint = IMAGE_MISSING_TOOL if container else hint_for(spec.hints, p.family)
    where = "required by inbox" if required else spec.purpose
    configured = _configured_path(spec, ctx.config.settings)
    program = spec.executable or (lambda path: path)

    path: str | None = None
    note = ""
    if configured:
        if not p.is_executable(program(configured)):
            return Result(
                spec.key,
                G_TOOLS,
                FAIL,
                f"[inbox] {spec.setting} = {configured} is not an executable file",
                f"fix the path, or remove the setting to look {spec.key} up on PATH. "
                f"To install it: {hint}",
            )
        path = configured
    else:
        default = dict(spec.defaults).get(p.family)
        if default and p.is_executable(program(default)):
            path = default
        for command in () if path else spec.commands:
            path = p.which(command)
            if path:
                break
        if path and not spec.inbox_uses_path:
            note = (
                f"found on PATH, but inbox only uses {spec.key} when [inbox] {spec.setting} is set"
            )
        if not path:
            for raw in known_tool_paths(spec, container):
                known = expand_home(raw, p.home)
                if p.is_executable(known):
                    return Result(
                        spec.key,
                        G_TOOLS,
                        WARN,
                        f"found at {known}, which is not on PATH",
                        f'set {spec.setting} = "{known}" in the [inbox] table',
                    )
    if not path:
        return Result(spec.key, G_TOOLS, missing, f"not found ({where})", hint)
    refused = spec.usable(path, p.family) if spec.usable else None
    if refused:
        return Result(spec.key, G_TOOLS, WARN, f"{path}: inbox will not run it: {refused}", hint)

    if spec.version_args is None:
        return Result(spec.key, G_TOOLS, OK, f"{path} ({spec.purpose})")
    try:
        out = p.run([path, *spec.version_args])
    except (OSError, subprocess.SubprocessError) as exc:
        reason = getattr(exc, "strerror", None) or type(exc).__name__
        return Result(
            spec.key,
            G_TOOLS,
            FAIL,
            f"{path} cannot be run ({reason})",
            "build the image again for this machine's architecture "
            f"({p.machine or 'unknown'}), or run inbox on a computer"
            if container
            else f"reinstall it for this machine ({p.machine or 'this architecture'}): {hint}",
        )
    line = _first_line(out.output)
    if not _VERSION.search(line):
        shown = f": {line}" if line else ""
        return Result(
            spec.key,
            G_TOOLS,
            WARN,
            f"{path} ran but printed no recognizable version{shown}",
            "check that it is the right program" + ("" if container else f"; to reinstall: {hint}"),
        )
    if note:
        return Result(
            spec.key,
            G_TOOLS,
            WARN,
            f"{line} ({path}); {note}",
            f'set {spec.setting} = "{path}" in the [inbox] table',
        )
    return Result(spec.key, G_TOOLS, OK, f"{line} ({path})")


def check_tools(ctx: Context) -> list[Result]:
    return [check_tool(ctx, spec) for spec in TOOLS]
