"""The systemd user timers that run the frontend refresh and the play state sync."""

from __future__ import annotations

import sys
from pathlib import Path

from warpzone import (
    artwork,
    safefile,
)
from warpzone.profiles import Profile
from warpzone.provision_scripts import script_config
from warpzone.report import Reporter
from warpzone.runners import (
    ImportCheck,
    SystemctlRunner,
    run_import_check,
    run_user_systemctl,
)


def ensure_artwork_timer(
    name: str,
    profile: Profile,
    home: Path,
    rep: Reporter,
    *,
    dry_run: bool,
    python: str | None = None,
    import_check: ImportCheck = run_import_check,
    config_path: Path | None = None,
    systemctl: SystemctlRunner = run_user_systemctl,
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
    if not import_check(python):
        rep.warn(
            f"the artwork timer was not installed: {python} cannot import warpzone from "
            "outside the current folder (run from a clone?). Set the device up with "
            f"`updater.sh install {name}`, which provisions from a venv"
        )
        return
    service, timer = artwork.render_units(
        python, name, script_config(config_path, home), art.refresh
    )
    _install_timer(
        artwork.UNIT_NAME, service, timer, art.refresh, "artwork", home, rep, dry_run, systemctl
    )


def ensure_play_state_timer(
    name: str,
    profile: Profile,
    home: Path,
    rep: Reporter,
    *,
    dry_run: bool,
    python: str | None = None,
    import_check: ImportCheck = run_import_check,
    config_path: Path | None = None,
    systemctl: SystemctlRunner = run_user_systemctl,
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
    if not import_check(python):
        rep.warn(
            f"the play state timer was not installed: {python} cannot import warpzone from "
            "outside the current folder (run from a clone?). Set the device up with "
            f"`updater.sh install {name}`, which provisions from a venv"
        )
        return
    service, timer = artwork.render_play_state_units(
        python, name, script_config(config_path, home), rule.refresh
    )
    _install_timer(
        artwork.PLAY_STATE_UNIT,
        service,
        timer,
        rule.refresh,
        "play state",
        home,
        rep,
        dry_run,
        systemctl,
    )


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
    rep.out(f"timer: {unit} runs every {interval}, skipping while the frontend is open")
