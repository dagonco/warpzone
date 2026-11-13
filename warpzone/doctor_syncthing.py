"""The doctor checks of the local Syncthing: its REST API, Game Mode, the unit, the flatpak.

Offline and read-only; the one request goes to the loopback address. See
development/design/doctor.md ("Game Mode", "Flatpak access", "Leftover folders").
"""

from __future__ import annotations

import dataclasses
import re
import subprocess
import xml.etree.ElementTree as ET
from collections.abc import Collection, Mapping

from warpzone import localst
from warpzone.doctor_core import FAIL, G_SYNCTHING, OK, OPTIONAL, WARN, Context, Result, scrub
from warpzone.doctor_hints import SYNCTHING_HINTS, SYNCTHING_START_HINTS, hint_for
from warpzone.flatpak import (
    SYNCTHINGY_APP,
    SYNCTHINGY_EXEC,
    SYNCTHINGY_STOP,
    app_dir,
    app_ids,
    is_syncthingy_config,
    missing_apps,
    override_command,
    show_command,
)
from warpzone.paths import expand_home
from warpzone.profiles import available_profiles, load_profile
from warpzone.provision_folders import PREFIX, undescribed
from warpzone.syncthing import _is_loopback

PROBE_TIMEOUT_SECONDS = 2.0

SYSTEMD_USER_DIR = ".config/systemd/user"
AUTOSTART_DIR = ".config/autostart"
SYNCTHING_UNIT_PATH = f"~/{SYSTEMD_USER_DIR}/syncthing.service"
# Indented lines are not wrapped by `render`, so the unit can be copied as it is.
SYNCTHING_UNIT = (
    "  [Unit]\n  Description=Syncthing\n"
    f"  [Service]\n  ExecStart={SYNCTHINGY_EXEC}\n  ExecStop={SYNCTHINGY_STOP}\n"
    "  Restart=on-failure\n"
    "  [Install]\n  WantedBy=default.target"
)

_EXEC_START = re.compile(r"^\s*Exec(?:Start)?\s*=(.*)$", re.MULTILINE)


def _runs_syncthing(text: str) -> bool:
    return any("syncthing" in line.lower() for line in _EXEC_START.findall(text))


def judge_game_mode(
    units: Mapping[str, str], enabled: Collection[str], autostart: Mapping[str, str]
) -> tuple[str, str, str]:
    """(status, detail, fix) for "will Syncthing run in Game Mode?". Game Mode starts no
    desktop session, so only a systemd user unit that is enabled keeps Syncthing running
    there. `units` maps the .service files of ~/.config/systemd/user to their text,
    `enabled` holds the names linked in its default.target.wants, and `autostart` maps
    the .desktop files of ~/.config/autostart to their text."""
    running = sorted(name for name, text in units.items() if _runs_syncthing(text))
    on = [name for name in running if name in enabled]
    autostarted = sorted(name for name, text in autostart.items() if _runs_syncthing(text))
    if on:
        if autostarted:
            return (
                WARN,
                f"{on[0]} starts Syncthing in Game Mode, but the desktop autostart entry "
                f"{autostarted[0]} starts it too, so two instances compete for the same port",
                f"remove ~/{AUTOSTART_DIR}/{autostarted[0]} so only the unit starts Syncthing",
            )
        return OK, f"{on[0]} is enabled, so Syncthing also runs in Game Mode", ""
    unit = running[0] if running else "syncthing.service"
    install = (
        f"systemctl --user enable --now {unit}"
        if running
        else (
            f"create ~/{SYSTEMD_USER_DIR}/syncthing.service with this content (for the "
            f"SyncThingy flatpak):\n{SYNCTHING_UNIT}\n"
            "then run:\n  systemctl --user daemon-reload\n"
            "  systemctl --user enable --now syncthing.service"
        )
    )
    if autostarted:
        why = (
            f"Syncthing is only started by the desktop autostart entry {autostarted[0]}, "
            "which Game Mode never runs, so syncing stops whenever the console is in Game Mode"
        )
        install += (
            f"\nand remove ~/{AUTOSTART_DIR}/{autostarted[0]} so two instances do not "
            "compete for the same port"
        )
    elif running:
        why = f"{running[0]} runs Syncthing but is not enabled, so it does not start on boot"
    else:
        why = (
            "no systemd user unit starts Syncthing, so it does not run in Game Mode "
            "(or after a reboot, until someone starts it by hand)"
        )
    return WARN, why, install


def parse_show(output: str) -> dict[str, str]:
    """`systemctl show` output (`Key=value` lines) as a dict."""
    values: dict[str, str] = {}
    for line in output.splitlines():
        key, sep, value = line.partition("=")
        if sep:
            values[key.strip()] = value.strip()
    return values


def judge_unit_state(
    output: str, *, flatpak: bool, stops_flatpak: bool
) -> tuple[str, str, str] | None:
    """(status, detail, fix) for the Syncthing user unit, from `systemctl --user show -p
    ActiveState,SubState`. None when there is nothing to say (no such unit, or stopped).
    A unit stuck in `activating (auto-restart)` is a start that keeps failing; with the
    flatpak the usual cause is the old process still holding the port, because `flatpak
    run` runs it in a scope of its own and `systemctl --user stop` does not reach it.
    `stops_flatpak`: the unit already has an `ExecStop` that kills the flatpak."""
    values = parse_show(output)
    state, sub = values.get("ActiveState", ""), values.get("SubState", "")
    if state == "active":
        return OK, f"syncthing.service is {state} ({sub})", ""
    if state != "activating" and sub != "auto-restart":
        return None
    detail = (
        f"syncthing.service is {state} ({sub}): it keeps starting and failing, usually "
        "because another Syncthing already holds the port"
    )
    if not flatpak:
        return (
            WARN,
            detail,
            "find the other Syncthing and stop it (`journalctl --user -u syncthing -n 20` "
            "shows the error), then run: systemctl --user restart syncthing",
        )
    restart = "systemctl --user daemon-reload\n  systemctl --user restart syncthing"
    kill = f"flatpak kill {SYNCTHINGY_APP}"
    if stops_flatpak:
        return (
            WARN,
            detail,
            f"the old process is still running: stop it with `{kill}`, then run:\n  {restart}",
        )
    return (
        WARN,
        detail + ". `flatpak run` starts Syncthing in a scope of its own, so `systemctl --user "
        "stop` leaves the old process running",
        f"stop the old process with `{kill}`. Then make {SYNCTHING_UNIT_PATH} stop the "
        f"flatpak too (it needs the ExecStop line):\n{SYNCTHING_UNIT}\nand run:\n  {restart}",
    )


def check_syncthing(ctx: Context) -> list[Result]:
    p = ctx.probes
    name = "Local Syncthing"
    platform_name = "darwin" if p.system == "Darwin" else p.system.lower() or "linux"
    try:
        path = localst.find_config(env=p.env, home=p.home, platform=platform_name)
    except localst.LocalSyncthingError:
        return [
            Result(
                name,
                G_SYNCTHING,
                OPTIONAL,
                "no config.xml found (looked in the default places, SyncThingy flatpak "
                "included); only needed on a console or PC that receives the library",
                hint_for(SYNCTHING_HINTS, p.family),
            )
        ]
    try:
        local = localst.parse_config(path)
    except localst.LocalSyncthingError as exc:
        return [
            Result(
                name,
                G_SYNCTHING,
                FAIL,
                f"found {path} but cannot use it: {exc}",
                "start Syncthing once so it writes a complete config, or pass "
                "--syncthing-config PATH to the command that needs it",
            )
        ]
    host = local.base_url.split("://", 1)[1].rpartition(":")[0].strip("[]")
    if not _is_loopback(host):
        return [
            Result(
                name,
                G_SYNCTHING,
                WARN,
                f"{path}; GUI listens on {local.base_url}, not on the loopback address, "
                "so the API was not probed",
            )
        ]
    start = hint_for(SYNCTHING_START_HINTS, p.family)
    try:
        status = p.http_status(
            f"{local.base_url}/rest/system/ping",
            {"X-API-Key": local.api_key},
            PROBE_TIMEOUT_SECONDS,
        )
    except (OSError, ValueError) as exc:
        reason = getattr(exc, "reason", None) or exc
        return [
            Result(
                name,
                G_SYNCTHING,
                FAIL,
                f"config at {path}, but the REST API at {local.base_url} does not answer "
                f"({type(exc).__name__}: {scrub(str(reason), local.api_key)})",
                f"Syncthing is probably not running: {start}",
            )
        ]
    if status == 200:
        return [
            Result(name, G_SYNCTHING, OK, f"REST API answers at {local.base_url} (config {path})")
        ]
    return [
        Result(
            name,
            G_SYNCTHING,
            FAIL,
            f"REST API at {local.base_url} answered HTTP {status} (config {path})",
            "the API key in config.xml was refused or something else owns that port; "
            "restart Syncthing and check its GUI address",
        )
    ]


def check_game_mode(ctx: Context) -> list[Result]:
    """SteamOS only, and only when Syncthing is installed: does it run in Game Mode?"""
    p = ctx.probes
    if p.family != "steamos":
        return []
    try:
        localst.find_config(env=p.env, home=p.home, platform="linux")
    except localst.LocalSyncthingError:
        return []
    unit_dir = p.home / SYSTEMD_USER_DIR
    units = {
        name: p.read_text(unit_dir / name)
        for name in p.list_dir(unit_dir)
        if name.endswith(".service")
    }
    enabled = set(p.list_dir(unit_dir / "default.target.wants"))
    autostart_dir = p.home / AUTOSTART_DIR
    autostart = {
        name: p.read_text(autostart_dir / name)
        for name in p.list_dir(autostart_dir)
        if name.endswith(".desktop")
    }
    status, detail, fix = judge_game_mode(units, enabled, autostart)
    return [Result("Game Mode", G_SYNCTHING, status, detail, fix)]


def _shipped_sync_paths() -> tuple[str, ...]:
    """Save and BIOS folders of every shipped profile (with `~`)."""
    paths: list[str] = []
    for name in available_profiles():
        profile = load_profile(name)
        paths += [s.path for s in profile.saves.values()]
        paths += [b.path for b in profile.bios.values()]
    return tuple(paths)


def check_flatpak_access(ctx: Context) -> list[Result]:
    """Syncthing as the SyncThingy flatpak cannot see `~/.var/app/<app>` of other apps,
    so a save folder there looks empty to it until the flatpak is granted the folder.
    Only the apps that are installed (their folder exists) are checked."""
    p = ctx.probes
    name = "Flatpak access"
    if p.system != "Linux":
        return []
    try:
        config = localst.find_config(env=p.env, home=p.home, platform="linux")
    except localst.LocalSyncthingError:
        return []
    if not is_syncthingy_config(config, p.home):
        return []  # a native Syncthing sees whatever its user can
    paths = ctx.config.sync_paths or _shipped_sync_paths()
    apps = [
        app
        for app in app_ids((expand_home(path, p.home) for path in paths), p.home)
        if p.is_dir(p.home / ".var" / "app" / app)
    ]
    if not apps:
        return []
    grants = " ".join(f"--filesystem={app_dir(app)}" for app in apps)
    try:
        out = p.run(show_command())
    except (OSError, subprocess.SubprocessError) as exc:
        return [
            Result(
                name,
                G_SYNCTHING,
                WARN,
                f"could not read the flatpak overrides ({type(exc).__name__}), so it is not "
                "known whether Syncthing can see the emulators' save folders",
                f"flatpak override --user {grants} {SYNCTHINGY_APP}",
            )
        ]
    missing = missing_apps(apps, out.output if out.returncode == 0 else "", p.home)
    if not missing:
        return [
            Result(
                name,
                G_SYNCTHING,
                OK,
                f"Syncthing is granted the data folders of {len(apps)} emulator(s) under "
                "~/.var/app",
            )
        ]
    commands = "\n".join(f"  {' '.join(override_command([app]))}" for app in missing)
    return [
        Result(
            name,
            G_SYNCTHING,
            FAIL,
            f"the Syncthing flatpak cannot see {', '.join(app_dir(a) for a in missing)}: "
            "flatpak hides other apps' data folders, so those save folders look empty and "
            "sync nothing",
            f"run:\n{commands}\nthen restart Syncthing "
            "(`systemctl --user restart syncthing`, or quit and reopen SyncThingy). "
            "`warpzone provision device` does this for you",
        )
    ]


def check_syncthing_unit(ctx: Context) -> list[Result]:
    """Linux with a local Syncthing: is the `syncthing` user unit restart-looping?"""
    p = ctx.probes
    if p.system != "Linux":
        return []
    try:
        config = localst.find_config(env=p.env, home=p.home, platform="linux")
    except localst.LocalSyncthingError:
        return []
    try:
        out = p.run(("systemctl", "--user", "show", "-p", "ActiveState,SubState", "syncthing"))
    except (OSError, subprocess.SubprocessError):
        return []
    if out.returncode != 0:
        return []
    unit = p.read_text(p.home / SYSTEMD_USER_DIR / "syncthing.service")
    stops = f"flatpak kill {SYNCTHINGY_APP}" in "\n".join(
        line for line in unit.splitlines() if line.strip().startswith("ExecStop")
    )
    verdict = judge_unit_state(
        out.output, flatpak=is_syncthingy_config(config, p.home), stops_flatpak=stops
    )
    if verdict is None:
        return []
    status, detail, fix = verdict
    return [Result("Syncthing unit", G_SYNCTHING, status, detail, fix)]


def check_undescribed_folders(ctx: Context) -> list[Result]:
    """On a configured device: `wz-*` folders in its Syncthing that its config no longer describes.

    A console that stops subscribing to a group keeps that folder in its own Syncthing, and
    `provision device` only walks the folders the config describes. Doctor is offline, so it
    finds the device in the local `config.xml` the way `local_device` does: the configured
    device whose ID is listed there and one of whose folders is there at the path the config
    gives it (the hub lists the same IDs but keeps its folders under its own root). Any other
    machine says nothing. The text comes from `provision_folders.undescribed`."""
    p = ctx.probes
    if not ctx.config.device_folders:
        return []
    platform_name = "darwin" if p.system == "Darwin" else p.system.lower() or "linux"
    try:
        config = localst.find_config(env=p.env, home=p.home, platform=platform_name)
        root = ET.fromstring(p.read_text(config))
    except (localst.LocalSyncthingError, ET.ParseError):
        return []
    ids = {d.get("id") for d in root.findall("device")}
    existing = {
        f.get("id") or "": {"path": (f.get("path") or "").rstrip("/")}
        for f in root.findall("folder")
        if (f.get("id") or "").startswith(PREFIX)
    }
    for device in ctx.config.device_folders:
        if device.device_id not in ids:
            continue
        specs = [
            dataclasses.replace(spec, path=expand_home(spec.path, p.home).rstrip("/"))
            for spec in device.specs
        ]
        if not any(
            spec.id in existing and existing[spec.id]["path"] == spec.path for spec in specs
        ):
            continue
        texts = undescribed(specs, existing)
        if not texts:
            return []
        return [
            Result(
                "Leftover folders",
                G_SYNCTHING,
                WARN,
                "; ".join(texts),
                "if this device should still have them, put their group back in "
                f"devices.{device.name}.folders and run `warpzone provision device "
                f"{device.name}`; otherwise remove them in this device's Syncthing",
            )
        ]
    return []
