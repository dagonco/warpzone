"""`warpzone health`: does this version of warpzone work on this device? Writes no user data.

The third step of verifying a version after an update (docs/design.md, "Autonomous updates
(contract)"). It loads the config and the profiles, runs as dry runs what ES-DE and the timers
run on their own (`settings apply`, `play-state sync`, `links sync`, `frontend refresh`) and
checks that the startup scripts and the units are there and run a working interpreter.

It is not `doctor`: nothing here depends on DATs or optional tools, and it does not prove that
a game starts or that Syncthing syncs. The decisions are pure (`plan_steps`, `expected_files`,
`judge_step`, `judge_file`, `exit_code`); `run_health` is the thin layer that touches the disk
and the local Syncthing.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from warpzone import artwork, esde, localst, settings, settings_state, updatetimer
from warpzone.config import Config, ConfigError, Device
from warpzone.model import ModelError
from warpzone.profiles import Profile, ProfileError
from warpzone.syncthing import Client, scrub

# What ES-DE gives each of its startup scripts (`timeout 15` in the script), and so what a dry
# run of the same work may take here.
STARTUP_SECONDS = 15.0

OK, FAIL, SKIP = "ok", "FAIL", "skip"

SETTINGS = "settings apply"
PLAY_STATE = "play-state sync"
LINKS = "links sync"
REFRESH = "frontend refresh"

SCRIPT, SERVICE, TIMER = "script", "service", "timer"

Load = Callable[[], tuple[Config, dict[str, Profile]]]
Say = Callable[[str], None]


@dataclass(frozen=True)
class Finding:
    name: str
    status: str
    detail: str


@dataclass(frozen=True)
class Expected:
    """A file `provision device` installs for this device: a startup script or a unit."""

    kind: str  # SCRIPT, SERVICE or TIMER
    label: str
    path: Path


# ------------------------------------------------------------------ decisions (pure)


def plan_steps(device: Device, profile: Profile) -> tuple[str, ...]:
    """The dry runs that apply to this device, in the order ES-DE and the timers run them."""
    folders = device.folders
    steps: list[str] = []
    if "settings" in folders:
        steps.append(SETTINGS)
    # A device with `[artwork]` merges the play state in `frontend refresh`.
    if "play-state" in folders and "esde" in profile.paths and profile.artwork is None:
        steps.append(PLAY_STATE)
    if "library" in folders and profile.links:
        steps.append(LINKS)
    if profile.artwork is not None and "frontend" in folders:
        steps.append(REFRESH)
    return tuple(steps)


def expected_files(device: Device, profile: Profile, home: Path) -> list[Expected]:
    """The startup scripts and unit files that `provision device` leaves for this device."""
    folders = device.folders
    found: list[Expected] = []
    if "esde" in profile.paths:
        esde_dir = Path(_expand(profile.paths["esde"], home))
        for group, name, needed in (
            ("settings", esde.SCRIPT_NAME, True),
            ("library", esde.LINKS_SCRIPT, bool(profile.links)),
            ("play-state", esde.PLAY_STATE_SCRIPT, True),
        ):
            if group in folders and needed:
                found.append(
                    Expected(SCRIPT, "ES-DE startup script", esde.script_path(esde_dir, name))
                )
    units = home / updatetimer.UNIT_DIR

    def unit(name: str) -> None:
        found.append(Expected(SERVICE, "service unit", units / f"{name}.service"))
        found.append(Expected(TIMER, "timer unit", units / f"{name}.timer"))

    if profile.artwork is not None and "frontend" in folders and profile.artwork.refresh:
        unit(artwork.UNIT_NAME)
    if (
        "play-state" in folders
        and profile.play_state is not None
        and profile.play_state.refresh is not None
    ):
        unit(artwork.PLAY_STATE_UNIT)
    if device.self_update:
        unit(updatetimer.NAME)
    return found


def judge_step(name: str, *, error: str | None, seconds: float, warnings: int) -> Finding:
    """A dry run passes when it finished without an error inside ES-DE's time limit."""
    if error is not None:
        return Finding(name, FAIL, error)
    if seconds > STARTUP_SECONDS:
        return Finding(
            name,
            FAIL,
            f"took {seconds:.1f}s, and ES-DE gives a startup script {STARTUP_SECONDS:g}s",
        )
    note = f", {warnings} warning(s)" if warnings else ""
    return Finding(name, OK, f"dry run in {seconds:.1f}s{note}")


def judge_file(
    item: Expected,
    *,
    text: str | None,
    executable: bool,
    program: str | None,
    program_ok: bool,
    imports: bool,
) -> Finding:
    """Verdict on one installed file. `program` is what it runs (a script's interpreter, a
    service's first `ExecStart` word); `imports` says that it can import warpzone, and is only
    looked at when the file runs `python -m warpzone`."""
    name, where = item.path.name, item.path
    if text is None:
        return Finding(name, FAIL, f"the {item.label} {where} is missing")
    if item.kind == TIMER:
        return Finding(name, OK, "present")
    if item.kind == SCRIPT and not executable:
        return Finding(name, FAIL, "not executable, so ES-DE cannot run it")
    if program is None:
        return Finding(name, FAIL, "does not run a program warpzone knows (edited by hand?)")
    if not program_ok:
        return Finding(name, FAIL, f"runs {program}, which is gone or not executable")
    if not imports:
        return Finding(name, FAIL, f"{program} cannot import warpzone from outside this folder")
    return Finding(name, OK, f"runs {program}")


def exit_code(findings: Sequence[Finding]) -> int:
    return 1 if any(f.status == FAIL for f in findings) else 0


def render(finding: Finding) -> str:
    return f"{finding.status:<5} {finding.name}: {finding.detail}"


def _expand(path: str, home: Path) -> str:
    if path == "~":
        return str(home)
    return str(home / path[2:]) if path.startswith("~/") else path


# ------------------------------------------------------------------ the edge


def _import_check(python: str) -> bool:
    from warpzone.provision import run_import_check

    return run_import_check(python)


def _systemctl(command: Sequence[str]) -> tuple[int, str]:
    from warpzone.provision import run_systemctl

    return run_systemctl(command)


@dataclass
class Probes:
    """What `run_health` touches. Tests replace the clock, /proc, Syncthing and the commands."""

    home: Path = field(default_factory=Path.home)
    state: Path = field(default_factory=settings_state.state_dir)
    proc: Path = Path("/proc")
    # The local Syncthing, or None when this machine has none (it raises when `--syncthing-config`
    # names one that is not usable).
    local: Callable[[], localst.LocalSyncthing | None] = lambda: None
    import_check: Callable[[str], bool] = _import_check
    systemctl: Callable[[Sequence[str]], tuple[int, str]] = _systemctl
    clock: Callable[[], float] = time.monotonic


class StepError(Exception):
    """A dry run ended in a way that is a failure without being an exception."""


def _quiet(_: str) -> None:
    pass


def _one_line(text: str, secrets: tuple[str, ...] = ()) -> str:
    return scrub(" ".join(text.split()), secrets)[:300]


def _client(local: localst.LocalSyncthing) -> Client:
    """A client that only reads: a dry run sends no write, and prints nothing."""
    return Client(
        local.base_url,
        local.api_key,
        label="local",
        dry_run=True,
        timeout=STARTUP_SECONDS,
        sink=lambda _: None,
    )


def _device_name(cfg: Config, name: str | None, probes: Probes) -> str:
    if name is not None:
        if name not in cfg.devices:
            known = ", ".join(sorted(cfg.devices))
            raise ConfigError(f"unknown device {name!r} (configured: {known})")
        return name
    local = probes.local()
    if local is None:
        raise ConfigError(
            "cannot tell which configured device this is: no local Syncthing was found; "
            "pass its name"
        )
    mine = _client(local).my_id()
    names = [d.name for d in cfg.devices.values() if d.device_id == mine]
    if len(names) != 1:
        raise ConfigError("cannot tell which configured device this is; pass its name")
    return names[0]


def _run_step(
    step: str,
    probes: Probes,
    cfg: Config,
    name: str,
    profile: Profile,
) -> Finding:
    warnings: list[str] = []
    secrets: list[str] = []

    def settings_apply() -> None:
        env = settings.Env(probes.home, probes.state, probes.proc)
        errors: list[str] = []
        code = settings.apply(
            settings.make_context(name, profile, env), dry_run=True, out=_quiet, err=errors.append
        )
        if code != 0:
            detail = f": {errors[0]}" if errors else ""
            raise StepError(f"exited {code}{detail}")

    def play_state_sync() -> None:
        from warpzone import playstate_device

        playstate_device.sync_shared(
            name,
            profile,
            probes.home,
            say=_quiet,
            warn=warnings.append,
            dry_run=True,
            starting=True,  # as the startup script runs it
            proc=probes.proc,
            state_root=probes.state,
        )

    def links_sync() -> None:
        from warpzone import titlelinks

        titlelinks.sync(
            name,
            profile,
            probes.home,
            say=_quiet,
            warn=warnings.append,
            dry_run=True,
            proc=probes.proc,
        )

    def frontend_refresh() -> None:
        from warpzone import provision

        local = probes.local()
        if local is not None:
            secrets.append(local.api_key)
        rep = provision.Reporter(_quiet)
        provision.refresh_artwork(
            cfg,
            name,
            profile,
            probes.home,
            rep,
            local=_client(local) if local is not None else None,
            dry_run=True,
            proc=probes.proc,
        )
        warnings.extend(rep.warnings)

    run = {
        SETTINGS: settings_apply,
        PLAY_STATE: play_state_sync,
        LINKS: links_sync,
        REFRESH: frontend_refresh,
    }[step]
    started = probes.clock()
    error: str | None = None
    try:
        run()
    except Exception as exc:  # a crash is what this command exists to find
        text = (
            str(exc) if isinstance(exc, StepError | ConfigError) else f"{type(exc).__name__}: {exc}"
        )
        error = _one_line(text, tuple(secrets)) or type(exc).__name__
    return judge_step(step, error=error, seconds=probes.clock() - started, warnings=len(warnings))


def _read(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None


def _has_user_manager(probes: Probes) -> bool:
    try:
        return probes.systemctl(["systemctl", "--user", "show-environment"])[0] == 0
    except (OSError, subprocess.SubprocessError):
        return False


def _check_files(
    device: Device, profile: Profile, probes: Probes, emit: Callable[[Finding], None]
) -> None:
    items = expected_files(device, profile, probes.home)
    manager: bool | None = None
    imports: dict[str, bool] = {}
    for item in items:
        if item.kind != SCRIPT:
            if manager is None:
                manager = _has_user_manager(probes)
            if not manager:
                emit(Finding(item.path.name, SKIP, "no systemd user manager on this machine"))
                continue
        text = _read(item.path)
        program: str | None = None
        runs_warpzone = False
        if text is not None and item.kind == SCRIPT:
            program, runs_warpzone = esde.script_interpreter(text), True
        elif text is not None and item.kind == SERVICE:
            words = updatetimer.exec_command(text) or []
            program = words[0] if words else None
            runs_warpzone = words[1:3] == ["-m", "warpzone"]
        program_ok = program is not None and shutil.which(program) is not None
        works = True
        if program_ok and runs_warpzone:
            assert program is not None
            if program not in imports:
                imports[program] = probes.import_check(program)
            works = imports[program]
        emit(
            judge_file(
                item,
                text=text,
                executable=os.access(item.path, os.X_OK),
                program=program,
                program_ok=program_ok,
                imports=works,
            )
        )


def run_health(load: Load, name: str | None, probes: Probes, out: Say = print) -> int:
    """Run every check, print one line each and return the exit code (0 ok, 1 failed)."""
    findings: list[Finding] = []

    def emit(finding: Finding) -> None:
        findings.append(finding)
        out(render(finding))

    def finish() -> int:
        failed = sum(f.status == FAIL for f in findings)
        out("health: ok" if not failed else f"health: {failed} check(s) failed")
        return exit_code(findings)

    try:
        cfg, profiles = load()
    except (ConfigError, ProfileError, ModelError) as exc:
        emit(Finding("config and profiles", FAIL, _one_line(str(exc))))
        return finish()
    emit(Finding("config and profiles", OK, f"{len(cfg.devices)} device(s)"))
    try:
        device_name = _device_name(cfg, name, probes)
    except Exception as exc:
        emit(Finding("device", FAIL, _one_line(str(exc))))
        return finish()
    device = cfg.devices[device_name]
    profile = profiles[device.profile]
    emit(Finding("device", OK, f"{device_name} ({profile.name})"))
    for step in plan_steps(device, profile):
        emit(_run_step(step, probes, cfg, device_name, profile))
    _check_files(device, profile, probes, emit)
    return finish()
