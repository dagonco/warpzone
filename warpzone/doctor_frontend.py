"""The doctor checks of a device's frontend: ES-DE and RetroArch, the emulators, artwork, links.

The system list override, RetroArch's paths, shadPS4, Cemu, title links, frontend artwork and
the save-conversion line. The judges are pure. See development/design/doctor.md ("Frontend")
and development/design/save-conversion.md.
"""

from __future__ import annotations

import os
from collections.abc import Collection, Mapping, Sequence
from pathlib import Path
from typing import Any

from warpzone import (
    artwork,
    cemu,
    esde,
    frontend,
    saveconvert_device,
    settings_state,
    shadps4,
    timers,
    titlelinks,
)
from warpzone.doctor_core import (
    FAIL,
    G_FRONTEND,
    OK,
    WARN,
    Context,
    Result,
    SettingsDevice,
    local_device,
    merge_verdicts,
)
from warpzone.doctor_probes import Probes
from warpzone.paths import expand_home
from warpzone.profiles import ShadPS4Dirs, ShadPS4Launch


def judge_systems_override(
    *,
    device: str,
    source: str | None,
    override: str | None,
    source_path: str,
    override_path: str,
    prefix: str,
    renames: Mapping[str, str] | None = None,
    target: str,
    convert_systems: Collection[str] = (),
    convert_wanted: bool = False,
) -> tuple[str, str, str]:
    """Verdict on the frontend's system list copy (`None`: the file is absent).

    `convert_systems` are the systems a `[save_convert.*]` rule wraps: their commands are
    compared without the wrapper, and with `convert_wanted` (the device syncs saves) each
    must have it.
    """
    fix = f"quit EmulationStation, then run `warpzone provision device {device}`"
    if source is None:
        return (
            WARN,
            f"the system list {source_path} was not found, so there is nothing to copy "
            f"into {override_path}",
            "",
        )
    if override is None:
        return (
            WARN,
            f"{override_path} is missing, so EmulationStation lists the games in {prefix}, "
            "not the library",
            fix,
        )
    if not frontend.systems_current(source, override, prefix, target, renames, convert_systems):
        return (
            WARN,
            f"{override_path} is out of date: it no longer matches {source_path} with the "
            f"ROM folders moved below {target}",
            fix,
        )
    if convert_wanted:
        lacking = sorted(set(convert_systems) - frontend.wrapped_systems(override, convert_systems))
        if lacking:
            return (
                WARN,
                f"{override_path} does not run the save conversion around "
                f"{', '.join(lacking)}, so those saves are only converted by the timer",
                fix,
            )
    return OK, f"{override_path} moves the ROM folders from {prefix} to {target}", ""


def judge_save_convert(
    *, device: str, state: Mapping[str, Any], backups: str
) -> tuple[str, str, str]:
    """Verdict on the save conversion: the last run, games skipped, latest-wins events."""
    last = state.get("last")
    if not isinstance(last, Mapping):
        return (
            WARN,
            "the save conversion has not run yet",
            f"run `warpzone saves convert {device}` (the timer and the emulator launch do it)",
        )
    when = str(last.get("when", "?"))
    skipped = [item for item in last.get("skipped") or [] if isinstance(item, list)]
    errors = [str(e) for e in last.get("errors") or []]
    events = [e for e in state.get("conflicts") or [] if isinstance(e, Mapping)]
    news = ""
    if events:
        recent = events[-1]
        kept = f"{backups}/{recent.get('rule', '?')}{saveconvert_device.CONFLICT_SUFFIX}/"
        news = (
            f"; {len(events)} latest-wins event(s), the last on {recent.get('when', '?')}: "
            f"{recent.get('game', '?')} ({recent.get('winner', '?')} file newer; the other copy "
            f"is in {kept}{recent.get('game', '?')})"
        )
    copies = [str(c) for c in last.get("conflict_files") or []]
    if copies:
        shown = ", ".join(copies[:3])
        news += (
            f"; {len(copies)} Syncthing conflict file(s) left alone ({shown}): keep the one you "
            "want and delete the others"
        )
    if errors:
        return (
            WARN,
            f"last run {when} failed for {len(errors)} game(s): {'; '.join(errors[:3])}{news}",
            f"run `warpzone saves convert {device}` and read what it says",
        )
    if skipped:
        names = ", ".join(f"{item[0]} ({item[1]})" for item in skipped[:3] if len(item) >= 2)
        return (
            WARN,
            f"last run {when} skipped {len(skipped)} game(s) it could not read as a save: "
            f"{names}{news}",
            "fix or remove those files; a file that is not a valid save is never converted",
        )
    if copies:
        return (
            WARN,
            f"last run {when}: {last.get('written', 0)} file(s) written{news}",
            "open each conflict file next to its save, keep the better one under the save's name "
            "and delete the other (warpzone never touches them)",
        )
    return OK, f"last run {when}: {last.get('written', 0)} file(s) written{news}", ""


def judge_artwork(
    *, device: str, plan: artwork.GamelistPlan | None, error: str | None, scraped_path: str
) -> tuple[str, str, str]:
    """Verdict on the frontend's gamelists: `plan` is None when it could not be made."""
    fix = f"quit EmulationStation, then run `warpzone frontend refresh {device}`"
    if plan is None:
        return WARN, error or "the gamelists could not be checked", ""
    if plan.problems:
        return WARN, "; ".join(plan.problems), "fix or remove the scraped gamelist on the hub"
    if not plan.items:
        if plan.waiting:
            return (
                WARN,
                f"no scraped gamelist has arrived in {scraped_path} for the systems on the "
                f"device ({', '.join(plan.waiting[:5])}), so EmulationStation shows no artwork",
                "check that the wz-esde folder syncs (`warpzone status`) and that the hub has "
                "scraped those systems",
            )
        return OK, "no system with games on the device has a frontend system", ""
    missing = [i.system for i in plan.items if i.state == "missing"]
    unreadable = [i.system for i in plan.items if i.state == "stale" and i.unreadable]
    stale = [i.system for i in plan.items if i.state == "stale" and not i.unreadable]
    if not missing and not stale and not unreadable:
        return OK, f"{len(plan.items)} gamelists current", ""
    if unreadable:
        fix += " (it keeps a copy of a gamelist it cannot read, then rewrites it)"
    parts = []
    for word, names in (("missing", missing), ("unreadable", unreadable), ("stale", stale)):
        if names:
            shown = ", ".join(names[:6]) + (f" and {len(names) - 6} more" if len(names) > 6 else "")
            parts.append(f"{len(names)} {word} ({shown})")
    return WARN, "gamelists " + "; ".join(parts), fix


def judge_retroarch_paths(
    *, device: str, files: Sequence[tuple[str, str | None]], wants: Mapping[str, str]
) -> tuple[str, str, str]:
    """Verdict on the RetroArch config files: `files` holds (path, text or None if absent)."""
    parts: list[tuple[str, str, str]] = []
    again = f"run `warpzone provision device {device}` with RetroArch closed"
    for path, text in files:
        if text is None:
            parts.append((WARN, f"{path} not found", f"start RetroArch once, then {again}"))
            continue
        gap = frontend.retroarch_gap(text, wants)
        if gap.unwritable:
            keys = ", ".join(sorted(gap.unwritable))
            parts.append(
                (WARN, f"{path}: {keys} appears more than once", "fix the duplicate by hand")
            )
        elif gap.differs:
            keys = ", ".join(
                f"{k} is {v!r}" if v is not None else f"{k} is unset"
                for k, v in gap.differs.items()
            )
            parts.append(
                (
                    WARN,
                    f"{path}: {keys}, so BIOS files or saves are not where the library syncs them",
                    again,
                )
            )
        else:
            parts.append((OK, f"{path} is set", ""))
    return merge_verdicts(parts)


def judge_shadps4_dirs(
    *,
    device: str,
    files: Sequence[tuple[str, str, str | None]],
    wants: Sequence[str],
    addon: str | None = None,
) -> tuple[str, str, str]:
    """Verdict on shadPS4's install and DLC folders.

    `files` holds (path, kind, text or None if absent); `addon` is the DLC folder the profile
    declares, None when it declares none.
    """
    again = f"run `warpzone provision device {device}` with shadPS4 closed"
    present = [(path, kind, text) for path, kind, text in files if text is not None]
    if not present:
        shown = " or ".join(path for path, _, _ in files)
        return (
            WARN,
            f"no shadPS4 config file found ({shown})",
            f"start shadPS4 once, then {again}",
        )
    parts: list[tuple[str, str, str]] = []
    for path, kind, text in present:
        try:
            gap = shadps4.missing(kind, text, wants)
            old = shadps4.addon_gap(kind, text, addon)
        except shadps4.Shadps4ConfigError as exc:
            parts.append((WARN, f"{path}: {exc}", "fix the file by hand, or start shadPS4 again"))
            continue
        if gap:
            parts.append(
                (
                    WARN,
                    f"{path} does not list {', '.join(gap)}, so shadPS4 does not find the games "
                    "that inbox extracts there",
                    again,
                )
            )
        if old is not None:
            parts.append(
                (
                    WARN,
                    f"{path} reads DLC from {old or 'its default folder'}, not {addon}, so "
                    "shadPS4 does not find the DLC that inbox files there",
                    again,
                )
            )
        if not gap and old is None:
            parts.append((OK, f"{path} lists the install folder", ""))
    return merge_verdicts(parts)


def judge_cemu_paths(
    *,
    device: str,
    path: str,
    text: str | None,
    mlc: str,
    default_mlc: str,
    game_path: str,
    held: str | None = None,
) -> tuple[str, str, str]:
    """Verdict on Cemu's MLC path and game paths in its `settings.xml` (None if absent).

    `held` is the `usr/save` of the MLC folder Cemu uses now when it holds game saves and is
    not `mlc`: `provision` then keeps `mlc_path`, so the fix is to move those saves first.
    """
    again = f"run `warpzone provision device {device}` with Cemu closed"
    if text is None:
        return (
            WARN,
            f"no Cemu settings file found ({path})",
            f"start Cemu once, finish its first-start dialog and close it, then {again}",
        )
    try:
        paths = cemu.read(text)
        gaps = cemu.missing(text, mlc, game_path)
    except cemu.CemuConfigError as exc:
        return WARN, f"{path}: {exc}", "fix the file by hand, or start Cemu again"
    if not gaps:
        return OK, f"{path} uses {mlc} and lists {game_path}", ""
    parts: list[str] = []
    if cemu.MLC in gaps:
        now = "is empty" if not paths.mlc else f"is {paths.mlc}"
        parts.append(
            f"its mlc_path {now}, so Cemu keeps saves in {cemu.in_use(paths, default_mlc)}, "
            f"not in {mlc}/usr/save, which warpzone syncs"
        )
    if cemu.GAME_PATHS in gaps:
        parts.append(f"its GamePaths lack {game_path}, so Cemu's own game list misses the library")
    if cemu.MLC in gaps and held is not None:
        fix = (
            f"move the saves in {held} to {mlc}/usr/save with Cemu closed, then run "
            f"`warpzone provision device {device}`"
        )
        return WARN, f"{path}: {'; '.join(parts)}", fix
    return WARN, f"{path}: {'; '.join(parts)}", again


def judge_shadps4_launch(
    *,
    device: str,
    script: str,
    script_text: str | None,
    expected: str,
    executable: bool,
    rules_path: str,
    rules_text: str | None,
    emulator: str,
) -> tuple[str, str, str]:
    """Verdict on the shadPS4 launch script and the ES-DE rule that should run it."""
    again = f"quit ES-DE and run `warpzone provision device {device}`"
    problems: list[str] = []
    if script_text is None:
        problems.append(f"{script} does not exist")
    elif script_text != expected:
        problems.append(f"{script} is not the script warpzone writes")
    elif not executable:
        problems.append(f"{script} is not executable")
    if rules_text is None:
        problems.append(f"{rules_path} does not exist, so ES-DE uses EmuDeck's launcher")
    else:
        try:
            target = shadps4.find_rule_target(rules_text, emulator)
        except shadps4.Shadps4ConfigError as exc:
            problems.append(f"{rules_path}: {exc}")
        else:
            if target != script:
                problems.append(
                    f"the {emulator} rule in {rules_path} runs {target}, not the launch script "
                    "(EmuDeck may have rewritten the file), so PS4 games start through the "
                    "Qt launcher, which can crash"
                )
    if problems:
        return WARN, "; ".join(problems), again
    return OK, f"ES-DE starts shadPS4 through {script}", ""


def judge_shadps4_version(
    *,
    emulator: str | None,
    games: bool,
    looked_in: Sequence[str],
    log: str,
) -> tuple[str, str, str]:
    """Verdict on the shadPS4 emulator version the launch script would run.

    `emulator` is what `shadps4.select_version` finds (None when no version is installed or
    selected), `games` whether the device holds PS4 games and `log` the file the script
    appends its failures to. Without a version PS4 games do not start, and ES-DE shows
    nothing of the script's error: a warning while there are no PS4 games yet, a problem when
    there are.
    """
    if emulator is not None:
        return OK, f"the launch script would run {emulator}", ""
    why = (
        f"no shadPS4 version is installed or selected (looked in {' and '.join(looked_in)}), "
        "so PS4 games do not start"
    )
    fix = f"{shadps4.NO_VERSION_FIX}. Failed launches are logged in {log}"
    return (FAIL if games else WARN), why, fix


def judge_title_links(
    *,
    device: str,
    label: str,
    target: str,
    target_exists: bool,
    todo: titlelinks.Plan,
    script: str | None,
    noun: str = "game",
) -> tuple[str, str, str]:
    """Verdict on the links of one `[links.*]` rule (`noun` says what they are: games,
    licenses); `script` names the ES-DE startup script when it is missing."""
    again = f"run `warpzone links sync {device}` (or start ES-DE) with {label} closed"
    parts: list[tuple[str, str, str]] = []
    if not target_exists:
        parts.append(
            (
                WARN,
                f"{target} does not exist, so {label} cannot use the library {noun}s",
                f"start {label} once, then run `warpzone links sync {device}`",
            )
        )
    else:
        if todo.create:
            parts.append(
                (
                    WARN,
                    f"{len(todo.create)} library {noun}(s) have no link in {target}, so "
                    f"{label} does not use them: {', '.join(todo.create)}",
                    again,
                )
            )
        stale = [name for name in todo.remove if name not in todo.create]
        if stale:
            parts.append(
                (WARN, f"dangling links in {target}: {', '.join(stale)}", again),
            )
        if not parts:
            n = len(todo.ok)
            what = f"{n} {noun}(s) linked in {target}" if n else f"no {noun}s to link yet"
            parts.append((OK, what, ""))
    if script is not None:
        parts.append(
            (
                WARN,
                f"the startup script {script} is missing, so new games are not linked when "
                "ES-DE starts",
                f"run `warpzone provision device {device}`",
            )
        )
    return merge_verdicts(parts)


def check_frontend(ctx: Context) -> list[Result]:
    """On a device whose profile moves the frontend and RetroArch to the library."""
    device = local_device(ctx, ctx.config.frontend_devices, "wz-library", "library")
    if device is None:
        return []
    p = ctx.probes
    profile = device.profile
    library = expand_home(profile.paths["library"], p.home).rstrip("/")
    results: list[Result] = []
    if (rule := profile.es_systems) is not None:
        source, override = (Path(expand_home(x, p.home)) for x in (rule.source, rule.override))
        verdict = judge_systems_override(
            device=device.name,
            source=p.read_text(source) if p.is_file(source) else None,
            override=p.read_text(override) if p.is_file(override) else None,
            source_path=str(source),
            override_path=str(override),
            prefix=rule.prefix,
            renames=rule.renames,
            target=f"{library}/roms/",
            convert_systems=[r.es_system for r in profile.save_convert.values() if r.es_system],
            convert_wanted="saves" in device.folders,
        )
        results.append(Result("ES systems override", G_FRONTEND, *verdict))
    if profile.artwork is not None and "frontend" in device.folders:
        results += _check_artwork(ctx, device, library)
    if profile.save_convert and "saves" in device.folders:
        state = saveconvert_device.state_root(p.home, settings_state.state_dir(p.env, p.home))
        path = state / saveconvert_device.STATE_FILE
        verdict = judge_save_convert(
            device=device.name,
            state=saveconvert_device.parse_state(p.read_text(path) if p.is_file(path) else None),
            backups=str(state / "backups"),
        )
        results.append(Result("Save conversion", G_FRONTEND, *verdict))
    if (pins := profile.retroarch) is not None:
        paths = [Path(expand_home(raw, p.home)) for raw in pins.cfg]
        verdict = judge_retroarch_paths(
            device=device.name,
            files=[(str(x), p.read_text(x) if p.is_file(x) else None) for x in paths],
            wants=pins.wants(library),
        )
        results.append(Result("RetroArch paths", G_FRONTEND, *verdict))
    if (dirs := profile.shadps4) is not None:
        files = []
        for raw in dirs.config:
            path = Path(expand_home(raw, p.home))
            files.append(
                (
                    str(path),
                    shadps4.kind_of(raw) or shadps4.JSON,
                    p.read_text(path) if p.is_file(path) else None,
                )
            )
        verdict = judge_shadps4_dirs(
            device=device.name,
            files=files,
            wants=dirs.wants(library),
            addon=dirs.wants_addon(library),
        )
        results.append(Result("shadPS4 install folder", G_FRONTEND, *verdict))
        if (launch := dirs.launch) is not None:
            script, rules = (
                Path(expand_home(x, p.home)) for x in (launch.script, launch.find_rules)
            )
            qt_ini, versions = (expand_home(x, p.home) for x in (launch.qt_ini, launch.versions))
            verdict = judge_shadps4_launch(
                device=device.name,
                script=str(script),
                script_text=p.read_text(script) if p.is_file(script) else None,
                expected=shadps4.render_launch_script(qt_ini, versions),
                executable=p.is_executable(str(script)),
                rules_path=str(rules),
                rules_text=p.read_text(rules) if p.is_file(rules) else None,
                emulator=launch.emulator,
            )
            results.append(Result("shadPS4 launcher", G_FRONTEND, *verdict))
            results.append(
                Result(
                    "shadPS4 version", G_FRONTEND, *_check_shadps4_version(p, dirs, launch, library)
                )
            )
    if (wiiu := profile.cemu) is not None:
        settings = Path(expand_home(wiiu.settings, p.home))
        mlc, game_path = (expand_home(x, p.home) for x in wiiu.wants(library))
        default_mlc = expand_home(wiiu.default_mlc, p.home)
        text = p.read_text(settings) if p.is_file(settings) else None
        verdict = judge_cemu_paths(
            device=device.name,
            path=str(settings),
            text=text,
            mlc=mlc,
            default_mlc=default_mlc,
            game_path=game_path,
            held=_cemu_held(p, text, default_mlc, mlc),
        )
        results.append(Result("Cemu paths", G_FRONTEND, *verdict))
    if profile.links:
        results += _check_links(ctx, device)
    return results


def _check_shadps4_version(
    p: Probes, dirs: ShadPS4Dirs, launch: ShadPS4Launch, library: str
) -> tuple[str, str, str]:
    """Look up the emulator the way the launch script does, and whether PS4 games are here."""
    qt_ini, versions = (Path(expand_home(x, p.home)) for x in (launch.qt_ini, launch.versions))

    def resolve(entry: str) -> str | None:
        # A version is an AppImage, or the folder that holds one.
        path = Path(entry)
        if p.is_dir(path):
            path = path / shadps4.SDL_APPIMAGE
        return str(path) if p.is_file(path) and p.is_executable(str(path)) else None

    emulator = shadps4.select_version(
        p.read_text(qt_ini) if p.is_file(qt_ini) else None,
        p.read_text(versions) if p.is_file(versions) else None,
        resolve,
    )
    games = any(p.list_dir(Path(d)) for d in dirs.wants(library))
    log = settings_state.state_dir(p.env, p.home).parent / shadps4.LAUNCH_LOG
    return judge_shadps4_version(
        emulator=emulator, games=games, looked_in=[str(qt_ini), str(versions)], log=str(log)
    )


def _cemu_held(p: Probes, text: str | None, default_mlc: str, mlc: str) -> str | None:
    """The `usr/save` of the MLC folder Cemu uses now, when it is not `mlc` and holds the save
    of a game or a demo; None otherwise (also when the file does not read)."""
    try:
        current = cemu.read(text) if text is not None else None
    except cemu.CemuConfigError:
        current = None
    if current is None:
        return None
    old = cemu.in_use(current, default_mlc)
    if os.path.realpath(old) == os.path.realpath(mlc):
        return None
    save = Path(old) / "usr" / "save"
    return str(save) if any(cemu.is_title_save(f) for f in p.list_tree(save)) else None


def _check_links(ctx: Context, device: SettingsDevice) -> list[Result]:
    """The game links of each `[links.*]` rule, and the ES-DE script that keeps them."""
    p = ctx.probes
    fs = titlelinks.Fs(
        listdir=p.list_dir,
        readlink=p.read_link,
        isdir=p.is_dir,
        isfile=p.is_file,
        exists=lambda path: p.is_dir(path) or p.is_file(path),
    )
    script = None
    if "esde" in device.profile.paths:
        path = esde.script_path(
            Path(expand_home(device.profile.paths["esde"], p.home)), esde.LINKS_SCRIPT
        )
        script = None if p.is_file(path) else str(path)
    results = []
    for item in titlelinks.resolve(device.profile, p.home):
        exists = p.is_dir(item.target) or titlelinks.creatable(item, fs)
        todo = titlelinks.inspect(item, fs)[1] if exists and p.is_dir(item.source) else None
        verdict = judge_title_links(
            device=device.name,
            label=item.label,
            target=str(item.target),
            target_exists=exists,
            todo=todo or titlelinks.Plan(),
            script=script,
            noun=item.rule.noun,
        )
        results.append(Result(f"{item.label} {item.rule.noun} links", G_FRONTEND, *verdict))
    return results


def _check_artwork(ctx: Context, device: SettingsDevice, library: str) -> list[Result]:
    """The gamelists of a device with `[artwork]`, and its refresh timer."""
    p = ctx.probes
    profile = device.profile
    art, rule = profile.artwork, profile.es_systems
    assert art is not None
    layout = artwork.Layout(
        Path(library) / "roms",
        Path(expand_home(art.media, p.home)),
        Path(expand_home(art.metadata, p.home)),
        Path(expand_home(art.gamelists, p.home)),
    )
    plan: artwork.GamelistPlan | None = None
    error: str | None = None
    if rule is None:
        error = "the profile has no [es_systems], so the systems of the frontend are unknown"
    else:
        source = Path(expand_home(rule.source, p.home))
        if not p.is_file(source):
            error = f"the system list {source} was not found, so the gamelists cannot be checked"
        else:
            hubs = [
                n
                for n in p.list_dir(layout.roms)
                if artwork.is_plain(n) and p.is_dir(layout.roms / n)
            ]
            files = artwork.Files(
                read=lambda path: p.read_text(path).encode("utf-8") if p.is_file(path) else None,
                is_file=p.is_file,
                exists=lambda path: p.is_file(path) or p.is_dir(path),
            )
            try:
                plan = artwork.plan_gamelists(
                    artwork.FORMATS[art.format],
                    art.kinds,
                    layout,
                    p.read_text(source),
                    prefix=rule.prefix,
                    renames=rule.renames,
                    hubs=hubs,
                    files=files,
                )
            except artwork.ArtworkError as exc:
                error = f"{source}: {exc}"
    verdict = judge_artwork(
        device=device.name, plan=plan, error=error, scraped_path=str(layout.metadata)
    )
    results = [Result("Frontend artwork", G_FRONTEND, *verdict)]
    if art.refresh is not None:
        unit = p.home / ".config" / "systemd" / "user" / f"{timers.ARTWORK_UNIT}.timer"
        if p.is_file(unit):
            results.append(
                Result(
                    "Artwork timer", G_FRONTEND, OK, f"{unit.name} installed (every {art.refresh})"
                )
            )
        else:
            results.append(
                Result(
                    "Artwork timer",
                    G_FRONTEND,
                    WARN,
                    f"{unit} is missing, so new scrapes are applied only by hand",
                    f"run `warpzone provision device {device.name}`",
                )
            )
    return results
