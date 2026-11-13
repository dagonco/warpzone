"""The doctor checks of the unattended work on a device: updates and pruning.

The update timer, the updater (script, settings, state) and the guard that refused a prune. The
judges are pure. See development/design/doctor.md and development/design/autonomous-updates.md.
"""

from __future__ import annotations

import os
import subprocess
from collections.abc import Mapping
from pathlib import Path

from warpzone import prune, selfupdate, settings_state, timers, updatetimer
from warpzone.doctor_core import FAIL, G_PRUNE, G_UPDATE, OK, WARN, Context, Result, local_device
from warpzone.versions import tag_key


def judge_update_timer(
    *,
    device: str,
    files: bool,
    enabled: bool | None,
    active: bool | None,
    command_ok: bool = True,
) -> tuple[str, str, str]:
    """(status, detail, fix) for the timer that starts the updater. `enabled` and `active` are
    None when systemd could not be asked; `command_ok` says the service runs the live copy of
    the updater with `sh` (an old unit ran `python -m warpzone self-update`)."""
    timer = updatetimer.TIMER
    if not files:
        return (
            WARN,
            "not installed: this device is only updated by hand",
            f"run `warpzone provision device {device}` (it installs and enables the timer). "
            "Without systemd, start `sh <updater> tick` from cron; to keep it off, set "
            f"`self_update = false` under [devices.{device}] in the config",
        )
    if not command_ok:
        return (
            WARN,
            "the service does not run the updater (an old unit?): this device is not updated",
            f"run `warpzone provision device {device}`, which rewrites the units",
        )
    if enabled is None or active is None:
        return (
            WARN,
            "the units are installed, but cannot ask systemd whether the timer is enabled",
            f"check it with `systemctl --user status {timer}`",
        )
    if not enabled or not active:
        state = "not enabled" if not enabled else "enabled but not running"
        return (
            WARN,
            f"installed, but the timer is {state}: this device is only updated by hand",
            f"run: systemctl --user enable --now {timer}",
        )
    return OK, "installed and enabled: runs the updater after boot and every 15 minutes", ""


def check_update_timer(ctx: Context) -> list[Result]:
    """On a Linux device that has not turned self-update off: is the timer installed?"""
    p = ctx.probes
    if p.system != "Linux":
        return []
    device = local_device(ctx, ctx.config.update_devices, "wz-library", "library")
    if device is None:
        return []
    units = p.home / updatetimer.UNIT_DIR
    files = all(p.is_file(units / name) for name in (updatetimer.SERVICE, updatetimer.TIMER))
    enabled: bool | None = False
    active: bool | None = False
    command_ok = True
    if files:
        live = str(selfupdate.live_path(p.home))
        words = timers.exec_command(p.read_text(units / updatetimer.SERVICE))
        command_ok = words == updatetimer.expected_command(live)
        try:
            enabled = (
                p.run(("systemctl", "--user", "is-enabled", updatetimer.TIMER)).returncode == 0
            )
            active = p.run(("systemctl", "--user", "is-active", updatetimer.TIMER)).returncode == 0
        except (OSError, subprocess.SubprocessError):
            enabled = active = None
    verdict = judge_update_timer(
        device=device.name, files=files, enabled=enabled, active=active, command_ok=command_ok
    )
    return [Result("Self-update timer", G_UPDATE, *verdict)]


UPDATER_STALE_DAYS = 14
_EXPECTED_KEYS = ("device", "config", "channel", "pin", "enabled", "frontend", "install")


def judge_updater_script(
    *, live: str | None, shipped: str | None, path: str, device: str
) -> tuple[str, str, str]:
    """(status, detail, fix) for the live copy of the updater script against the one that
    ships with this version (None when it cannot be read)."""
    fix = f"run `warpzone provision device {device}`, which installs it"
    if live is None:
        return WARN, f"the updater is not installed ({path}): this device is not updated", fix
    if shipped is None:
        return OK, f"installed ({path})", ""
    verdict = selfupdate.install_decision(shipped, live)
    if verdict == "older":
        return OK, "installed; it is newer than the one shipped here, and is kept", ""
    if verdict == "install":
        return (
            WARN,
            f"the installed updater ({path}) differs from the one shipped with this version",
            fix,
        )
    return OK, f"installed and current ({path})", ""


def judge_updater_conf(
    *,
    actual: Mapping[str, str] | None,
    expected: Mapping[str, str],
    python_ok: bool,
    clone_ok: bool,
    device: str,
) -> tuple[str, str, str]:
    """(status, detail, fix) for the updater's settings against what the config says."""
    fix = f"run `warpzone provision device {device}`, which writes them"
    if actual is None:
        return WARN, "the updater's settings are missing: it does not know this device", fix
    wrong = [
        f"{key} is {actual.get(key, '(missing)')!r}, the config says {expected[key]!r}"
        for key in _EXPECTED_KEYS
        if key in expected and actual.get(key) != expected[key]
    ]
    if not python_ok:
        wrong.append(f"python {actual.get('python', '')!r} is gone or not executable")
    if not clone_ok:
        wrong.append(f"clone {actual.get('clone', '')!r} is not a git checkout")
    if wrong:
        return WARN, "the updater's settings differ: " + "; ".join(wrong), fix
    return OK, "the updater's settings match the config", ""


def judge_updater_state(
    *,
    state: Mapping[str, str] | None,
    bad: Mapping[str, selfupdate.Bad],
    now: float,
    device: str,
) -> list[tuple[str, str, str]]:
    """What the updater's state says needs a look: one (status, detail, fix) per problem, or a
    single OK line. `state` is None when the updater has not run yet; `bad` holds the tags it
    found bad and will not try again."""
    if state is None:
        return [
            (
                WARN,
                "the updater has not run yet",
                "wait for the timer (it starts 45 seconds after boot), "
                "or run `warpzone self-update`",
            )
        ]
    found: list[tuple[str, str, str]] = []
    last = state.get("last_result", "")
    limit = UPDATER_STALE_DAYS * 86400
    recent = sorted(
        tag for tag, entry in bad.items() if entry.when is None or now - entry.when <= limit
    )
    if recent or last == "rolled-back":
        names = ", ".join(f"{t} ({bad[t].reason or 'no reason kept'})" for t in recent)
        found.append(
            (
                WARN,
                "rolled back from a release that did not work"
                + (f": {names}" if names else "")
                + ". It will not try that release again",
                "see the log, ~/.local/state/warpzone/self-update.log, and `last-verify.log` "
                "in the updater's state folder",
            )
        )
    if last == "stuck":
        found.append(
            (
                FAIL,
                "the updater is stuck: it could not put the last working version back and "
                "changed nothing else",
                "look at the clone with git (status, and the log above), put it on a release "
                "tag by hand, then run `warpzone self-update`",
            )
        )
    if last in ("dirty", "dev"):
        what = (
            "has changes to tracked files"
            if last == "dirty"
            else "has commits of its own, or is not on a release"
        )
        found.append(
            (
                WARN,
                f"the checkout is {last} ({what}), so it is not moved by the updater",
                "commit or discard the changes, or put the clone on a release tag, "
                "to have it updated again",
            )
        )
    if last == "no-python":
        found.append(
            (
                WARN,
                "the updater found no Python 3.11 or newer, so it did nothing",
                "install Python 3.11 or newer, then run `warpzone self-update`",
            )
        )
    if state.get("pin_retired"):
        found.append(
            (
                WARN,
                f"the pinned release {state['pin_retired']} has been retired, and the pin "
                "keeps the device on it",
                f"change or remove `pin` under [devices.{device}] in the config, then run "
                f"`warpzone provision device {device}`",
            )
        )
    if state.get("signing") != "1":
        found.append(
            (
                WARN,
                "signatures are not required: no version this device ran lists a signer key, "
                "so a release is trusted without one",
                "see https://dagonco.github.io/warpzone/development/releasing/ on signing releases",
            )
        )
    try:
        last_ok: int | None = int(state["last_ok"])
    except (KeyError, ValueError):
        last_ok = None
    if last_ok is None:
        found.append(
            (
                WARN,
                "the updater has never finished a run successfully",
                "run `warpzone self-update` and read what it says",
            )
        )
    elif now - last_ok > limit:
        days = int((now - last_ok) // 86400)
        found.append(
            (
                WARN,
                f"the updater has not succeeded for {days} days",
                "run `warpzone self-update` and read what it says",
            )
        )
    if found:
        return found
    return [(OK, f"last run: {last or 'ok'}", "")]


def check_updater(ctx: Context) -> list[Result]:
    """On a Linux device that has not turned self-update off: is the updater script installed,
    are its settings the config's, and what does its state say?"""
    p = ctx.probes
    if p.system != "Linux":
        return []
    device = local_device(ctx, ctx.config.update_devices, "wz-library", "library")
    if device is None:
        return []
    base = settings_state.state_dir(p.env, p.home).parent
    live_path = selfupdate.live_path(p.home)
    live = p.read_text(live_path) if p.is_file(live_path) else None
    source = selfupdate.UPDATER_SOURCE
    shipped = p.read_text(source) if p.is_file(source) else None
    results = [
        Result(
            "Updater script",
            G_UPDATE,
            *judge_updater_script(
                live=live, shipped=shipped, path=str(live_path), device=device.name
            ),
        )
    ]
    conf_file = selfupdate.conf_path(base)
    actual = selfupdate.parse_kv(p.read_text(conf_file)) if p.is_file(conf_file) else None
    expected = selfupdate.parse_kv(
        selfupdate.render_conf(
            device=device.name,
            config=os.path.abspath(p.config_path),
            clone="",
            python="",
            channel=device.channel,
            pin=device.pin,
            enabled=True,
            frontend=selfupdate.frontend_names(device.profile),
            install=selfupdate.install_mode(device.profile),
        )
    )
    clone = Path(actual.get("clone", "")) if actual else Path("")
    results.append(
        Result(
            "Updater settings",
            G_UPDATE,
            *judge_updater_conf(
                actual=actual,
                expected=expected,
                python_ok=bool(actual and p.is_executable(actual.get("python", ""))),
                clone_ok=bool(actual and (p.is_dir(clone / ".git") or p.is_file(clone / ".git"))),
                device=device.name,
            ),
        )
    )
    state_file = selfupdate.state_path(base)
    state = selfupdate.parse_kv(p.read_text(state_file)) if p.is_file(state_file) else None
    bad: dict[str, selfupdate.Bad] = {}
    folder = selfupdate.bad_dir(base)
    for name in p.list_dir(folder):
        if tag_key(name) is not None:
            bad[name] = selfupdate.parse_bad(p.read_text(folder / name))
    results += [
        Result("Updater state", G_UPDATE, *found)
        for found in judge_updater_state(state=state, bad=bad, now=p.now(), device=device.name)
    ]
    return results


def judge_prune_guard(text: str | None, *, now: float, device: str) -> tuple[str, str, str]:
    """(status, detail, fix) for the state file the unattended prune leaves when its half-of-device
    guard held a removal back (`prune.guard_state_path`). `text` is None when there is no file:
    nothing is waiting."""
    if text is None:
        return OK, "the unattended prune has nothing held back", ""
    refusal = prune.parse_guard_state(text)
    if refusal is None:
        return (
            WARN,
            "the file the unattended prune leaves when it holds a removal back cannot be read",
            f"run `warpzone prune {device}` to see what is left over, then "
            f"`warpzone prune {device} --apply` by hand, which rewrites the file",
        )
    age = max(0, int(now - refusal.when))
    ago = f"{age // 86400} day(s)" if age >= 86400 else f"{age // 3600} hour(s)"
    folders = ", ".join(
        f"{folder} would lose {prune.human(delete)} of the {prune.human(held)} it holds"
        for folder, delete, held in refusal.folders
    )
    return (
        WARN,
        f"the unattended prune has refused to delete anything for {ago}: {folders}, more than "
        "half, so the files a whitelist no longer lists stay on this device",
        f"check the whitelist is what you meant, run `warpzone prune {device}` to review what "
        f"it would delete, then `warpzone prune {device} --apply` by hand",
    )


def check_prune_guard(ctx: Context) -> list[Result]:
    """On a device that runs the unattended prune: has its guard held a removal back?"""
    p = ctx.probes
    device = local_device(ctx, ctx.config.prune_devices, "wz-library", "library")
    if device is None:
        return []
    path = prune.guard_state_path(settings_state.state_dir(p.env, p.home).parent)
    text = p.read_text(path) if p.is_file(path) else None
    return [
        Result(
            "Unattended prune",
            G_PRUNE,
            *judge_prune_guard(text, now=p.now(), device=device.name),
        )
    ]
