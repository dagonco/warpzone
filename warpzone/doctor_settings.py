"""The doctor checks of a device's emulator settings and play state.

Settings layers, the ES-DE startup script and event scripts, the last `settings apply`, and the
play-state ledger. The judges are pure. See development/design/doctor.md ("Emulator
settings") and development/design/play-state.md.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import Path

from warpzone import esde, settings_state, timers
from warpzone.doctor_core import (
    FAIL,
    G_FRONTEND,
    G_SETTINGS,
    OK,
    OPTIONAL,
    WARN,
    Context,
    Result,
    local_device,
    local_settings_device,
    merge_verdicts,
)
from warpzone.paths import expand_home
from warpzone.profiles import Profile
from warpzone.settings_layers import LayerError, conflict_copies, parse_layer, validate


# Fields of `last-apply.json` that `judge_last_apply` reads: `time` (ISO 8601 text), `exit`
# (or `exit_code`), `written` and `held` (counts). Anything missing or odd is ignored.
def _count(info: Mapping[str, object], *names: str) -> int | None:
    for name in names:
        value = info.get(name)
        if isinstance(value, int) and not isinstance(value, bool):
            return value
    return None


def judge_settings_layers(
    profile: Profile,
    *,
    device: str,
    folder: str,
    folder_exists: bool,
    marker_exists: bool,
    common: str | None,
    own: str | None,
    names: Sequence[str],
) -> tuple[str, str, str]:
    """Verdict on the settings folder and the two layers (`None` when a file is absent).

    Messages name keys and ids, never values: a rejected value may be a secret.
    """
    provision = f"warpzone provision device {device}"
    if not folder_exists:
        return (
            FAIL,
            f"{folder} does not exist, so this device has no settings layers to apply",
            f"run `{provision}`, and let Syncthing sync the folder",
        )
    if not marker_exists:
        return (
            FAIL,
            f"{folder} is not a running Syncthing folder yet (no .stfolder inside), so "
            "`settings apply` does nothing",
            f"run `{provision}` and check that Syncthing is running",
        )
    errors: list[str] = []
    warnings: list[str] = []
    given = 0
    for source, text in (("common.toml", common), (f"{device}.toml", own)):
        if text is None:
            continue
        given += 1
        try:
            layer, notes = parse_layer(text, source)
        except LayerError as exc:
            errors += exc.errors
            continue
        found, notes_more = validate(layer, profile, source)
        errors += found
        warnings += notes + notes_more
    errors += [f"{name}: Syncthing conflict copy" for name in conflict_copies(names)]
    if errors:
        return (
            FAIL,
            "\n".join(errors + warnings),
            "`settings apply` changes nothing until this is fixed: correct the file on the "
            "hub (secret keys are never layered), and for a conflict copy keep one version "
            "and delete the other",
        )
    if warnings:
        return (
            WARN,
            "\n".join(warnings),
            "check the names against the device's profile; another profile may know them",
        )
    if not given:
        return OK, f"{folder}: no layer files yet (nothing to apply)", ""
    return OK, f"{folder}: {given} layer file(s) valid", ""


def judge_esde_script(
    *,
    device: str,
    script: str,
    exists: bool,
    executable: bool,
    interpreter: str | None,
    interpreter_ok: bool,
) -> tuple[str, str, str]:
    """Verdict on the ES-DE startup script that runs `settings apply`."""
    fix = f"warpzone provision device {device}"
    if not exists:
        return (
            WARN,
            f"no startup script at {script}, so ES-DE does not apply the emulator settings "
            "when it starts",
            fix,
        )
    if not executable:
        return WARN, f"{script} is not executable, so ES-DE cannot run it", fix
    if interpreter is None:
        return (
            WARN,
            f"{script} does not run `python -m warpzone` (edited by hand?)",
            fix,
        )
    if not interpreter_ok:
        return (
            WARN,
            f"{script} runs {interpreter}, which is gone or not executable "
            "(was warpzone reinstalled elsewhere?)",
            f"{fix} (from the warpzone you want ES-DE to use)",
        )
    return OK, f"{script} runs {interpreter}", ""


def judge_event_scripts(xml_text: str | None, device: str) -> tuple[str, str, str]:
    """Verdict on `CustomEventScripts` in `es_settings.xml` (`None` when the file is absent)."""
    if xml_text is None:
        return (
            WARN,
            "es_settings.xml not found, so it is not known whether ES-DE's custom event "
            "scripts are on",
            f"start ES-DE once, quit it, then run `warpzone provision device {device}`",
        )
    if esde.event_scripts_enabled(xml_text) is True:
        return OK, "custom event scripts are on", ""
    return (
        WARN,
        "ES-DE's custom event scripts are off (or unreadable), so it never runs the startup script",
        f"quit ES-DE and run `warpzone provision device {device}` with ES-DE closed, or "
        f"turn it on in ES-DE ({esde.MENU_PATH})",
    )


def judge_last_apply(info: Mapping[str, object] | None, log_path: str) -> tuple[str, str, str]:
    """Verdict on the summary of the last `settings apply` (`None` when there is none)."""
    if info is None:
        return (
            OPTIONAL,
            "no run yet: nothing has applied the settings on this device",
            "start ES-DE, or run `warpzone settings apply`",
        )
    when = info.get("time")
    when_text = when if isinstance(when, str) and when else "at an unknown time"
    code = _count(info, "exit", "exit_code") or 0
    held = _count(info, "held") or 0
    written = _count(info, "written")
    if code != 0:
        return (
            WARN,
            f"the last run ({when_text}) failed with exit code {code}",
            f"see {log_path} (it never holds values), then run `warpzone settings apply`",
        )
    if held:
        return (
            WARN,
            f"the last run ({when_text}) held {held} key(s) changed on this device: they "
            "were not overwritten",
            "`warpzone settings capture` sends them to the hub, or `warpzone settings "
            "apply --overwrite-local` replaces them with the hub's values",
        )
    done = f", {written} written" if written is not None else ""
    return OK, f"the last run ({when_text}) succeeded{done}", ""


def check_settings(ctx: Context) -> list[Result]:
    """On a device that subscribes to `settings`: layers, ES-DE startup script, last apply."""
    device = local_settings_device(ctx)
    if device is None:
        return []
    p = ctx.probes
    profile = device.profile
    folder = Path(expand_home(profile.paths["settings"], p.home))

    def layer_text(name: str) -> str | None:
        path = folder / name
        return p.read_text(path) if p.is_file(path) else None

    exists = p.is_dir(folder)
    verdict = judge_settings_layers(
        profile,
        device=device.name,
        folder=str(folder),
        folder_exists=exists,
        marker_exists=exists and p.is_dir(folder / ".stfolder"),
        common=layer_text("common.toml") if exists else None,
        own=layer_text(f"{device.name}.toml") if exists else None,
        names=p.list_tree(folder) if exists else [],
    )
    results = [Result("Settings layers", G_SETTINGS, *verdict)]

    if "esde" in profile.paths:
        esde_dir = Path(expand_home(profile.paths["esde"], p.home))
        script = esde.script_path(esde_dir)
        found = p.is_file(script)
        interpreter = esde.script_interpreter(p.read_text(script)) if found else None
        xml = esde.settings_path(esde_dir)
        parts = [
            judge_esde_script(
                device=device.name,
                script=str(script),
                exists=found,
                executable=found and p.is_executable(str(script)),
                interpreter=interpreter,
                interpreter_ok=interpreter is not None and p.is_executable(interpreter),
            ),
            judge_event_scripts(p.read_text(xml) if p.is_file(xml) else None, device.name),
        ]
        results.append(Result("ES-DE startup script", G_SETTINGS, *merge_verdicts(parts)))

    state = settings_state.state_dir(p.env, p.home)
    verdict = judge_last_apply(settings_state.read_last_apply(state), str(state / "log"))
    results.append(Result("Last settings apply", G_SETTINGS, *verdict))
    return results


def judge_play_state(
    *, device: str, folder: str, ready: bool, ledger: bool, script: str | None, timer: str | None
) -> tuple[str, str, str]:
    """Verdict on a device's play state; `script` and `timer` name what is missing, if anything."""
    again = f"run `warpzone provision device {device}`"
    if not ready:
        return WARN, f"the folder {folder} is not set up", again
    if script is not None:
        return WARN, f"the startup script {script} is missing", again
    if timer is not None:
        return WARN, f"the timer {timer} is missing", again
    if not ledger:
        return OK, f"{folder} is set up; this device has nothing to publish yet", ""
    return OK, f"this device publishes to {folder}", ""


def check_play_state(ctx: Context) -> list[Result]:
    """On a device that shares favourites and play statistics."""
    devices = ctx.config.play_state_devices
    device = local_device(ctx, devices, "wz-play-state", "play_state")
    if device is None:
        return []
    p, profile = ctx.probes, device.profile
    folder = Path(expand_home(profile.paths["play_state"], p.home))
    units = p.home / ".config" / "systemd" / "user"
    script = timer = None
    if "esde" in profile.paths:
        path = esde.script_path(
            Path(expand_home(profile.paths["esde"], p.home)), esde.PLAY_STATE_SCRIPT
        )
        script = None if p.is_file(path) else str(path)
    if profile.play_state is not None and profile.play_state.refresh is not None:
        unit = units / f"{timers.PLAY_STATE_UNIT}.timer"
        timer = None if p.is_file(unit) else unit.name
    verdict = judge_play_state(
        device=device.name,
        folder=str(folder),
        ready=p.is_dir(folder / ".stfolder") or p.is_file(folder / ".stfolder"),
        ledger=p.is_file(folder / f"{device.name}.jsonl"),
        script=script,
        timer=timer,
    )
    return [Result("Play state", G_FRONTEND, *verdict)]
