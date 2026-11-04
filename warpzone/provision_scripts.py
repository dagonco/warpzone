"""The ES-DE startup scripts a device runs warpzone from, and turning ES-DE's event scripts on."""

from __future__ import annotations

import os
import sys
from pathlib import Path

from warpzone import esde, procs, safefile, settings_state
from warpzone.paths import expand_home
from warpzone.probes import REAL_PROBES, DeviceProbes
from warpzone.profiles import Profile
from warpzone.report import Reporter


def _enable_event_scripts(
    name: str,
    esde_dir: Path,
    rep: Reporter,
    *,
    dry_run: bool,
    probes: DeviceProbes,
    state_root: Path,
) -> None:
    """Turn ES-DE's `CustomEventScripts` on, only while ES-DE is not running.

    ES-DE keeps its settings in memory and rewrites the file when it quits, so an edit made
    while it runs would be lost. The old file is backed up and the new one written
    atomically, refusing when the file changed after it was read.
    """
    path = esde.settings_path(esde_dir)
    again = f"quit ES-DE and run `warpzone provision device {name}` again"
    by_hand = f"or turn it on in ES-DE ({esde.MENU_PATH})"
    if not path.is_file():
        rep.warn(
            f"{path} does not exist, so ES-DE's custom event scripts were not turned on: "
            f"start ES-DE once, then run `warpzone provision device {name}` again ({by_hand})"
        )
        return
    try:
        before = safefile.stamp(path)
        text = path.read_bytes().decode("utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        rep.warn(f"cannot read {path} ({exc}); {by_hand}")
        return
    if esde.event_scripts_enabled(text) is True:
        rep.out("es-de: custom event scripts already on")
        return

    def running() -> bool | None:
        try:
            return procs.running(esde.ESDE_PROCESSES, proc=probes.proc)
        except procs.NoProc:
            return None

    def refuse(state: bool | None) -> bool:
        if state is None:
            rep.warn(
                "cannot tell whether ES-DE is running (no /proc), so its custom event scripts "
                f"were not turned on: {again} on a system with /proc ({by_hand})"
            )
        elif state:
            rep.defer()
            rep.warn(
                f"ES-DE is running, so its custom event scripts were not turned on (it "
                f"rewrites {path.name} when it quits): {again}"
            )
        return state is not False

    if refuse(running()):
        return
    try:
        new = esde.enable_event_scripts(text)
    except esde.EsdeError as exc:
        rep.warn(f"{path} was left alone: {exc}; {by_hand}")
        return
    if dry_run:
        rep.out(f"es-de: dry-run: would set CustomEventScripts to true in {path}")
        return
    outcome = safefile.guarded_edit(
        path,
        new.encode(),
        expect=before,
        busy=lambda: refuse(running()),  # ES-DE may have started while the backup was made
        backup=lambda: settings_state.backup(path, "esde", "es_settings", root=state_root),
    )
    if outcome is safefile.Edit.BUSY:
        return
    if outcome is safefile.Edit.CHANGED:
        rep.warn(f"{path} changed while it was being edited, so it was left alone: run again")
        return
    rep.out(f"es-de: set CustomEventScripts to true in {path} (old file backed up)")


def script_config(config_path: Path | None, home: Path) -> Path | None:
    """The config path the startup script has to pass, or None for the default location."""
    if config_path is None:
        return None
    absolute = Path(os.path.abspath(config_path))
    return None if absolute == home / ".config" / "warpzone" / "config.toml" else absolute


def ensure_esde_script(
    name: str,
    profile: Profile,
    home: Path,
    rep: Reporter,
    *,
    dry_run: bool,
    python: str | None = None,
    probes: DeviceProbes = REAL_PROBES,
    state_root: Path | None = None,
    config_path: Path | None = None,
) -> None:
    """Install ES-DE's startup script for `settings apply` and turn ES-DE's scripts on.

    The script runs the Python that runs this command, so that interpreter must import
    warpzone from outside the current folder; run from a clone it warns and installs
    nothing. The script is rewritten only when its content differs. Does nothing when the
    profile has no ES-DE folder. `config_path` is the private config file this run used: the
    script names it unless it is the default one, since ES-DE does not have the user's
    environment (`WARPZONE_CONFIG`, `XDG_CONFIG_HOME`).
    """
    if "esde" not in profile.paths:
        return
    python = python or sys.executable
    text = esde.render_startup_script(python, name, script_config(config_path, home))
    _install_startup_script(
        name,
        profile,
        home,
        rep,
        esde.SCRIPT_NAME,
        text,
        dry_run=dry_run,
        python=python,
        probes=probes,
        state_root=state_root,
    )


def ensure_play_state_script(
    name: str,
    profile: Profile,
    home: Path,
    rep: Reporter,
    *,
    dry_run: bool,
    python: str | None = None,
    probes: DeviceProbes = REAL_PROBES,
    state_root: Path | None = None,
    config_path: Path | None = None,
) -> None:
    """Install ES-DE's startup script for `play-state sync`, as `ensure_esde_script` does."""
    if "esde" not in profile.paths:
        return
    python = python or sys.executable
    text = esde.render_play_state_script(python, name, script_config(config_path, home))
    _install_startup_script(
        name,
        profile,
        home,
        rep,
        esde.PLAY_STATE_SCRIPT,
        text,
        dry_run=dry_run,
        python=python,
        probes=probes,
        state_root=state_root,
    )


def ensure_links_script(
    name: str,
    profile: Profile,
    home: Path,
    rep: Reporter,
    *,
    dry_run: bool,
    python: str | None = None,
    probes: DeviceProbes = REAL_PROBES,
    state_root: Path | None = None,
    config_path: Path | None = None,
) -> None:
    """Install ES-DE's startup script for `links sync`, as `ensure_esde_script` does."""
    if "esde" not in profile.paths or not profile.links:
        return
    python = python or sys.executable
    text = esde.render_links_script(python, name, script_config(config_path, home))
    _install_startup_script(
        name,
        profile,
        home,
        rep,
        esde.LINKS_SCRIPT,
        text,
        dry_run=dry_run,
        python=python,
        probes=probes,
        state_root=state_root,
    )


def _install_startup_script(
    name: str,
    profile: Profile,
    home: Path,
    rep: Reporter,
    script: str,
    text: str,
    *,
    dry_run: bool,
    python: str,
    probes: DeviceProbes,
    state_root: Path | None,
) -> None:
    esde_dir = Path(expand_home(profile.paths["esde"], home))
    if not probes.import_check(python):
        rep.warn(
            f"the ES-DE startup script was not installed: {python} cannot import warpzone "
            "from outside the current folder (run from a clone?). Set the device up with "
            f"`updater.sh install {name}`, which provisions from a venv"
        )
        return
    path = esde.script_path(esde_dir, script)
    try:
        current: str | None = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        current = None
    if current == text:
        if not dry_run and not os.access(path, os.X_OK):
            path.chmod(0o755)
            rep.out(f"es-de: made {path} executable")
        else:
            rep.out(f"es-de: script unchanged ({path})")
    elif dry_run:
        rep.out(f"es-de: dry-run: would write {path}:")
        for line in text.splitlines():
            rep.out(f"  {line}")
    else:
        safefile.write_script(path, text)
        rep.out(f"es-de: wrote {path}")
    root = state_root if state_root is not None else settings_state.state_dir(home=home)
    _enable_event_scripts(name, esde_dir, rep, dry_run=dry_run, probes=probes, state_root=root)
