"""The autonomous update of a device: its settings file, the live updater script and its timer."""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from collections.abc import Callable, Sequence
from pathlib import Path

from warpzone import safefile, selfupdate, settings_state, updatetimer
from warpzone.config import Device
from warpzone.profiles import Profile
from warpzone.report import Reporter
from warpzone.runners import (
    FlatpakRunner,
    ImportCheck,
    run_import_check,
    run_script_check,
    run_systemctl,
)

UPDATER_SOURCE = selfupdate.UPDATER_SOURCE


# `sh -n` and the script's `selftest` each get this long; the selftest takes a few seconds.
UPDATER_CHECK_SECONDS = 120


def _read_text(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None


def _first_lines(text: str, count: int = 3) -> str:
    return " / ".join([line.strip() for line in text.splitlines() if line.strip()][:count])


def ensure_updater(
    home: Path,
    rep: Reporter,
    *,
    dry_run: bool,
    source: Path = UPDATER_SOURCE,
    run: Callable[[Sequence[str], float], tuple[int, str]] | None = None,
) -> bool:
    """Put `source` (the script that ships with this version) in place as the live copy
    outside the clone, and say whether there is a usable live copy afterwards.

    The copy is written next to the live one and replaces it atomically (a tick that is running
    keeps reading the file it started with), only after `sh -n` and `sh <copy> selftest` pass,
    and the old one stays as `updater.sh.prev`. A copy of a lower `UPDATER_ABI` than the live
    one is never installed: a rollback to an older release must not downgrade the script.
    """
    live = selfupdate.live_path(home)
    check = run or run_script_check
    candidate = _read_text(source)
    if candidate is None:
        rep.warn(f"the updater was not installed: cannot read {source}")
        return live.is_file()
    current = _read_text(live)
    verdict = selfupdate.install_decision(candidate, current)
    if verdict == "invalid":
        rep.warn(f"the updater was not installed: {source} has no UPDATER_ABI line")
        return current is not None
    if verdict == "current":
        rep.out(f"updater: already current ({live})")
        return True
    if verdict == "older":
        have, new = selfupdate.updater_abi(current or ""), selfupdate.updater_abi(candidate)
        rep.out(
            f"updater: keeping {live}: its abi {have} is higher than the abi {new} of this version"
        )
        return True
    if dry_run:
        rep.out(f"updater: dry-run: would install {live}")
        return current is not None

    new_abi = selfupdate.updater_abi(candidate)
    tmp: Path | None = None
    try:
        # A name of its own (two provisions at once must not share a file that one of them
        # has checked and the other has not) in the folder of the live copy, so that the final
        # rename stays on one file system.
        live.parent.mkdir(parents=True, exist_ok=True)
        fd, name = tempfile.mkstemp(dir=live.parent, prefix=f".{live.name}.", suffix=".new")
        tmp = Path(name)
        with os.fdopen(fd, "wb") as fh:
            fh.write(candidate.encode())
            fh.flush()
            os.fsync(fh.fileno())
        os.chmod(tmp, 0o755)
        code, shown = check(["sh", "-n", str(tmp)], UPDATER_CHECK_SECONDS)
        if code != 0:
            rep.warn(
                f"the updater was not installed: the new script has a syntax error "
                f"({_first_lines(shown) or f'exit {code}'}); {live} is unchanged"
            )
            return current is not None
        code, shown = check(["sh", str(tmp), "selftest"], UPDATER_CHECK_SECONDS)
        if code != 0 or f"selftest: ok abi={new_abi}" not in shown.splitlines():
            why = "ran out of time" if code == 124 else _first_lines(shown) or f"exit {code}"
            rep.warn(
                f"the updater was not installed: its selftest failed ({why}); {live} is unchanged"
            )
            return current is not None
        # The checks take seconds: judge again against what is live now, not what was live
        # before them.
        latest = _read_text(live)
        if latest != current:
            if selfupdate.install_decision(candidate, latest) != "install":
                rep.out(f"updater: {live} changed while the new script was checked; keeping it")
                return latest is not None
            current = latest
        if current is not None:
            safefile.write_script(live.with_name(f"{live.name}.prev"), current)
        os.replace(tmp, live)
    except OSError as exc:
        rep.warn(f"the updater was not installed: {exc}")
        return live.is_file()
    finally:
        if tmp is not None:
            tmp.unlink(missing_ok=True)
    rep.out(f"updater: installed {live}" + (" (the old one is kept as .prev)" if current else ""))
    return True


def ensure_updater_conf(conf: Path, text: str, rep: Reporter, *, dry_run: bool) -> None:
    """Write the settings file of the script when its content differs."""
    if _read_text(conf) == text:
        rep.out(f"updater settings: already current ({conf})")
        return
    if dry_run:
        rep.out(f"updater settings: dry-run: would write {conf}")
        return
    try:
        safefile.write_script(conf, text, 0o644)
    except OSError as exc:
        rep.warn(f"the updater settings were not written ({exc})")
        return
    rep.out(f"updater settings: wrote {conf}")


def ensure_update_timer(
    home: Path,
    rep: Reporter,
    *,
    dry_run: bool,
    updater: Path,
    systemctl: FlatpakRunner | None = None,
) -> None:
    """Install and enable the systemd user timer that starts one tick of the updater.

    The service runs `sh <updater> tick` with the live copy of the script, so nothing is
    written when that file is not there (outside a dry run) or when there is no systemd user
    manager. The units are rewritten only when their content differs, and the timer is enabled
    only when it is not already.
    """
    ctl = systemctl or run_systemctl
    units = home / updatetimer.UNIT_DIR
    manual = (
        f"run `sh {updater} tick` from another scheduler (cron, for example), or "
        "set `self_update = false` for this device in the config"
    )

    def call(*args: str) -> tuple[int, str]:
        return ctl(["systemctl", "--user", *args])

    if not dry_run and not updater.is_file():
        rep.warn(f"the self-update timer was not installed: there is no updater at {updater}")
        return
    try:
        code, shown = call("show-environment")
        if code != 0:
            raise OSError(shown.strip().splitlines()[0] if shown.strip() else f"exit code {code}")
    except (OSError, subprocess.SubprocessError) as exc:
        rep.warn(
            f"the self-update timer was not installed: no systemd user manager ({exc}); {manual}"
        )
        return

    wanted = {
        units / updatetimer.SERVICE: updatetimer.render_service(str(updater)),
        units / updatetimer.TIMER: updatetimer.render_timer(),
    }
    changed = [path for path, text in wanted.items() if _read_text(path) != text]
    try:
        enabled = call("is-enabled", updatetimer.TIMER)[0] == 0
        active = call("is-active", updatetimer.TIMER)[0] == 0
    except (OSError, subprocess.SubprocessError):
        enabled = active = False
    if not changed and enabled and active:
        rep.out("self-update timer: already installed and enabled")
        return
    if dry_run:
        if changed:
            rep.out(f"self-update timer: dry-run: would write {', '.join(map(str, changed))}")
        rep.out(f"self-update timer: dry-run: would enable {updatetimer.TIMER}")
        return
    enable = f"systemctl --user enable --now {updatetimer.TIMER}"
    try:
        for path in changed:
            safefile.write_script(path, wanted[path], 0o644)
        steps = [("daemon-reload",)] if changed else []
        steps.append(("enable", "--now", updatetimer.TIMER))
        if changed and active:
            steps.append(("restart", updatetimer.TIMER))
        for step in steps:
            code, shown = call(*step)
            if code != 0:
                raise OSError(shown.strip() or f"exit code {code}")
    except (OSError, subprocess.SubprocessError) as exc:
        rep.warn(f"the self-update timer could not be enabled ({exc}). Run `{enable}`")
        return
    rep.out(f"self-update timer: installed and enabled ({updatetimer.TIMER})")


def ensure_self_update(
    device: Device,
    profile: Profile,
    home: Path,
    rep: Reporter,
    *,
    dry_run: bool,
    python: str | None = None,
    import_check: ImportCheck = run_import_check,
    clone: Path | None = None,
    config_path: Path | None = None,
    state_root: Path | None = None,
    systemctl: FlatpakRunner | None = None,
    updater_source: Path = UPDATER_SOURCE,
) -> None:
    """Everything the autonomous update needs on this device: the settings file written from
    the config, the live copy of the updater script and the timer that starts it.

    It needs a git clone (the device install: `updater.sh install`) and a Python that imports
    warpzone from outside the current folder (the venv); without them it installs nothing and
    says so. A device with `self_update = false` gets no updater; one that already has settings
    gets `enabled=false`, so its timer's ticks stop.
    """
    python = python or sys.executable
    clone = clone if clone is not None else selfupdate.package_root()
    base = (state_root if state_root is not None else settings_state.state_dir(home=home)).parent
    conf = selfupdate.conf_path(base)
    live = selfupdate.live_path(home)

    def conf_text(enabled: bool) -> str:
        return selfupdate.render_conf(
            device=device.name,
            config=os.path.abspath(config_path) if config_path is not None else None,
            clone=clone,
            python=python,
            channel=device.channel,
            pin=device.pin,
            enabled=enabled,
            frontend=selfupdate.frontend_names(profile),
        )

    try:
        if not device.self_update:
            if conf.is_file():
                ensure_updater_conf(conf, conf_text(False), rep, dry_run=dry_run)
            rep.out("self-update timer: turned off (self_update = false in the config)")
            return
        text = conf_text(True)
    except ValueError as exc:
        rep.warn(f"the updater was not installed: {exc}")
        return
    if not (clone / ".git").exists():
        rep.warn(
            f"the updater was not installed: {clone} is not a git checkout. A device runs "
            "warpzone from a clone made with `sh <clone>/warpzone/data/updater.sh install "
            f"{device.name}` (see https://dagonco.github.io/warpzone/operation/self-update/)"
        )
        return
    if not import_check(python):
        rep.warn(
            f"the updater was not installed: {python} cannot import warpzone from outside the "
            "current folder (run from the clone's venv?). Set the device up with "
            f"`sh {clone}/warpzone/data/updater.sh install {device.name}`"
        )
        return
    ensure_updater_conf(conf, text, rep, dry_run=dry_run)
    installed = ensure_updater(home, rep, dry_run=dry_run, source=updater_source)
    if installed or dry_run:
        ensure_update_timer(home, rep, dry_run=dry_run, updater=live, systemctl=systemctl)
