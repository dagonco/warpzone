"""The systemd user timers that run the frontend refresh, the play state sync and the prune."""

from __future__ import annotations

import sys
from pathlib import Path

from warpzone import safefile, timers
from warpzone.config import Device
from warpzone.probes import REAL_PROBES, DeviceProbes
from warpzone.profiles import Profile
from warpzone.provision_scripts import script_config
from warpzone.report import Reporter
from warpzone.runners import SystemctlRunner


def ensure_artwork_timer(
    name: str,
    profile: Profile,
    home: Path,
    rep: Reporter,
    *,
    dry_run: bool,
    python: str | None = None,
    config_path: Path | None = None,
    probes: DeviceProbes = REAL_PROBES,
) -> None:
    """Install and start the `systemd --user` timer that runs `frontend refresh`.

    Only for a profile whose `[artwork]` sets `refresh`. The units run the Python that runs
    this command, which must import warpzone from outside the current folder. They are
    rewritten only when their text differs; a second run makes no systemctl call.
    """
    art = profile.artwork
    if art is None or art.refresh is None:
        return
    python = python or sys.executable
    if not probes.import_check(python):
        rep.warn(
            f"the artwork timer was not installed: {python} cannot import warpzone from "
            "outside the current folder (run from a clone?). Set the device up with "
            f"`updater.sh install {name}`, which provisions from a venv"
        )
        return
    service, timer = timers.render_artwork_units(
        python, name, script_config(config_path, home), art.refresh
    )
    _install_timer(
        timers.ARTWORK_UNIT,
        service,
        timer,
        art.refresh,
        "artwork",
        home,
        rep,
        dry_run,
        probes.user_systemctl,
    )


def ensure_play_state_timer(
    name: str,
    profile: Profile,
    home: Path,
    rep: Reporter,
    *,
    dry_run: bool,
    python: str | None = None,
    config_path: Path | None = None,
    probes: DeviceProbes = REAL_PROBES,
) -> None:
    """Install and start the `systemd --user` timer that runs `play-state sync`.

    Only for a profile whose `[play_state]` sets `refresh`; a device with `[artwork]` has its
    own timer. Between ES-DE starts it publishes what ES-DE saved, so the other devices get it
    without waiting for the next start.
    """
    rule = profile.play_state
    if rule is None or rule.refresh is None:
        return
    python = python or sys.executable
    if not probes.import_check(python):
        rep.warn(
            f"the play state timer was not installed: {python} cannot import warpzone from "
            "outside the current folder (run from a clone?). Set the device up with "
            f"`updater.sh install {name}`, which provisions from a venv"
        )
        return
    service, timer = timers.render_play_state_units(
        python, name, script_config(config_path, home), rule.refresh
    )
    _install_timer(
        timers.PLAY_STATE_UNIT,
        service,
        timer,
        rule.refresh,
        "play state",
        home,
        rep,
        dry_run,
        probes.user_systemctl,
    )


def ensure_prune_timer(
    name: str,
    device: Device,
    home: Path,
    rep: Reporter,
    *,
    dry_run: bool,
    python: str | None = None,
    config_path: Path | None = None,
    probes: DeviceProbes = REAL_PROBES,
) -> None:
    """Install and start the `systemd --user` timer that runs `prune --apply --unattended`.

    Only for a device that lists `library` and does not set `auto_prune = false`; for any other
    the units are removed if they exist. So a game turned off on the hub leaves the console by
    itself (see development/design/prune.md, "Unattended runs"). Like the other timers the units
    run the Python that runs this command, are rewritten only when their text differs, and a
    second run makes no systemctl call.
    """
    if not device.prunes_unattended:
        _remove_timer(timers.PRUNE_UNIT, "prune", home, rep, dry_run, probes.user_systemctl)
        return
    python = python or sys.executable
    if not probes.import_check(python):
        rep.warn(
            f"the prune timer was not installed: {python} cannot import warpzone from "
            "outside the current folder (run from a clone?). Set the device up with "
            f"`updater.sh install {name}`, which provisions from a venv"
        )
        return
    service, timer = timers.render_prune_units(python, name, script_config(config_path, home))
    _install_timer(
        timers.PRUNE_UNIT,
        service,
        timer,
        timers.PRUNE_INTERVAL,
        "prune",
        home,
        rep,
        dry_run,
        probes.user_systemctl,
        "deleting only what the hub still has and the device no longer whitelists",
    )


def ensure_settings_timer(
    name: str,
    device: Device,
    profile: Profile,
    home: Path,
    rep: Reporter,
    *,
    dry_run: bool,
    python: str | None = None,
    config_path: Path | None = None,
    probes: DeviceProbes = REAL_PROBES,
) -> None:
    """Install and start the `systemd --user` timer that applies the layered ES-DE settings.

    Only for a device that subscribes to `settings` and whose profile declares the `esde`
    settings; for any other the units are removed if they exist. ES-DE is a Steam app that is
    closed most of the time and rewrites `es_settings.xml` when it quits, so its keys cannot
    be applied by its own startup script: the timer runs `settings apply --emulator esde
    --when-idle`, which does nothing while ES-DE runs (see development/design/emulator-settings.md,
    "ES-DE settings"). The units run the Python that runs this command, are rewritten only when
    their text differs, and a second run makes no systemctl call.
    """
    if "settings" not in device.folders or timers.SETTINGS_EMULATOR not in profile.settings:
        _remove_timer(timers.SETTINGS_UNIT, "settings", home, rep, dry_run, probes.user_systemctl)
        return
    python = python or sys.executable
    if not probes.import_check(python):
        rep.warn(
            f"the settings timer was not installed: {python} cannot import warpzone from "
            "outside the current folder (run from a clone?). Set the device up with "
            f"`updater.sh install {name}`, which provisions from a venv"
        )
        return
    service, timer = timers.render_settings_units(python, name, script_config(config_path, home))
    _install_timer(
        timers.SETTINGS_UNIT,
        service,
        timer,
        timers.SETTINGS_INTERVAL,
        "settings",
        home,
        rep,
        dry_run,
        probes.user_systemctl,
        "applying ES-DE's settings only while ES-DE is closed",
    )


def _remove_timer(
    unit_name: str,
    label: str,
    home: Path,
    rep: Reporter,
    dry_run: bool,
    systemctl: SystemctlRunner,
) -> None:
    """Stop the timer and delete its two units, when they exist."""
    folder = home / ".config" / "systemd" / "user"
    present = [
        p for p in (folder / f"{unit_name}.timer", folder / f"{unit_name}.service") if p.exists()
    ]
    if not present:
        return
    if dry_run:
        for path in present:
            rep.out(f"timer: dry-run: would remove {path}")
        return
    if not systemctl(("disable", "--now", f"{unit_name}.timer")):
        rep.warn(
            f"`systemctl --user disable --now {unit_name}.timer` failed, so the {label} timer "
            "may still be loaded until the next login: run it by hand"
        )
    for path in present:
        try:
            path.unlink()
        except OSError as exc:
            rep.warn(f"cannot remove {path} ({exc})")
            return
        rep.out(f"timer: removed {path}")
    if not systemctl(("daemon-reload",)):
        rep.warn("`systemctl --user daemon-reload` failed: run it by hand")


def _install_timer(
    unit_name: str,
    service: str,
    timer: str,
    interval: str,
    label: str,
    home: Path,
    rep: Reporter,
    dry_run: bool,
    systemctl: SystemctlRunner,
    detail: str = "skipping while the frontend is open",
) -> None:
    folder = home / ".config" / "systemd" / "user"
    wanted = {f"{unit_name}.service": service, f"{unit_name}.timer": timer}
    changed = False
    for filename, text in wanted.items():
        path = folder / filename
        try:
            before: safefile.FileStamp | None = safefile.stamp(path)
            current: str | None = path.read_text(encoding="utf-8")
        except FileNotFoundError:
            before, current = None, None
        except (OSError, UnicodeDecodeError) as exc:
            rep.warn(f"cannot read {path} ({exc}), so the {label} timer was left alone")
            return
        if current == text:
            continue
        changed = True
        if dry_run:
            rep.out(f"timer: dry-run: would write {path}")
        elif not safefile.atomic_write(path, text.encode("utf-8"), expect=before):
            rep.warn(f"{path} changed while it was being written, so it was left alone: run again")
            return
        else:
            rep.out(f"timer: wrote {path}")
    if not changed:
        rep.out(f"timer: {unit_name} unchanged (every {interval})")
        return
    if dry_run:
        return
    unit = f"{unit_name}.timer"
    for args in (("daemon-reload",), ("enable", "--now", unit), ("restart", unit)):
        if not systemctl(args):
            rep.warn(
                f"`systemctl --user {' '.join(args)}` failed, so the {label} timer may not "
                f"run: run it by hand (is a user session running? `loginctl enable-linger`)"
            )
            return
    rep.out(f"timer: {unit} runs every {interval}, {detail}")
