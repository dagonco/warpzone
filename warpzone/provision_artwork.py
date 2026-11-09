"""Frontend artwork on a device: which systems receive it, and the gamelists written from it."""

from __future__ import annotations

import dataclasses
from pathlib import Path

from warpzone import artwork, diskstatus_io, playstate_device, safefile, saveconvert_device
from warpzone.config import Config
from warpzone.model import ESDE_ID, MEDIA_ID, device_specs
from warpzone.paths import expand_home
from warpzone.probes import REAL_PROBES, DeviceProbes
from warpzone.profiles import Profile
from warpzone.provision_emulators import busy
from warpzone.report import Reporter
from warpzone.syncthing import Client


def artwork_layout(profile: Profile, home: Path) -> artwork.Layout | None:
    art = profile.artwork
    if art is None or "library" not in profile.paths:
        return None
    library = expand_home(profile.paths["library"], home).rstrip("/")
    return artwork.Layout(
        Path(library) / "roms",
        Path(expand_home(art.media, home)),
        Path(expand_home(art.metadata, home)),
        Path(expand_home(art.gamelists, home)),
    )


def artwork_scope(profile: Profile, home: Path) -> list[str]:
    """The hub systems a device with `[artwork]` receives artwork for (none without any)."""
    layout = artwork_layout(profile, home)
    if layout is None or profile.artwork is None:
        return []
    return artwork.library_systems(layout.roms)  # `systems = "library"`, the only scope


def no_scope_warning(name: str, layout: artwork.Layout) -> str:
    return (
        f"no system folder in {layout.roms} yet, so no artwork is received; it follows once "
        f"games have arrived (`warpzone frontend refresh {name}`, or the timer, picks it up)"
    )


def sync_artwork_scope(
    cfg: Config,
    name: str,
    profile: Profile,
    home: Path,
    local: Client,
    rep: Reporter,
) -> None:
    """Make the ignores of the artwork folders match the systems that are on the device now.

    A system whose games arrived after `provision device` has no artwork until its folders
    let it in. A folder that Syncthing does not have yet is left for `provision device`.
    """
    if profile.artwork is None or "frontend" not in cfg.devices[name].folders:
        return
    wanted = [
        dataclasses.replace(s, path=expand_home(s.path, home))
        for s in device_specs(cfg, name, profile, artwork_scope(profile, home))
        if s.id in (MEDIA_ID, ESDE_ID)
    ]
    existing = {f["id"]: f for f in local.folders()}
    systems = len(artwork_scope(profile, home))
    for spec in wanted:
        if spec.id not in existing:
            rep.warn(f"{spec.id} is not in Syncthing yet: run `warpzone provision device {name}`")
        elif local.get_ignores(spec.id) != list(spec.ignores):
            local.set_ignores(spec.id, list(spec.ignores))
            rep.out(f"{spec.id}: ignores updated ({systems} systems)")


def ensure_gamelists(
    name: str,
    profile: Profile,
    home: Path,
    rep: Reporter,
    *,
    dry_run: bool,
    probes: DeviceProbes = REAL_PROBES,
    play_state: bool = False,
) -> None:
    """Write the frontend's gamelists from the scraped data that Syncthing delivered.

    Each system on the device that has a scraped gamelist gets one for the frontend, under
    the system's name in the frontend's list (`artwork.plan_gamelists`). Nothing is written
    for a gamelist that already says the same, so a second run, and a run while the frontend
    is open and has nothing to change, do nothing. A file is written only while the frontend
    is not running, atomically, and refused when it changed after it was read. The user
    data the frontend saved in a gamelist is carried over; see
    development/design/frontend-artwork.md. With `play_state` that data also goes to the other
    devices and theirs is merged in; see development/design/play-state.md.
    """
    art, rule = profile.artwork, profile.es_systems
    layout = artwork_layout(profile, home)
    if art is None or rule is None or layout is None:
        return
    fmt = artwork.FORMATS[art.format]
    again = f"run `warpzone frontend refresh {name}` again"
    hubs = artwork.library_systems(layout.roms)
    if not hubs:
        rep.out(f"gamelists: no system has games in {layout.roms} yet")
        return
    source = Path(expand_home(rule.source, home))
    try:
        system_list = source.read_bytes().decode("utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        rep.warn(f"cannot read the system list {source} ({exc}), so no gamelist was written")
        return
    stamps: dict[Path, safefile.FileStamp] = {}

    def read(path: Path) -> bytes | None:
        try:
            stamp = safefile.stamp(path)
            data = path.read_bytes()
        except FileNotFoundError:
            return None
        stamps[path] = stamp
        return data

    files = dataclasses.replace(artwork.REAL_FILES, read=read)
    state: playstate_device.Generated | None = None
    try:
        if play_state:
            state = playstate_device.prepare_generated(
                name,
                profile,
                home,
                layout,
                artwork.gamelist_systems(
                    layout, system_list, prefix=rule.prefix, renames=rule.renames, hubs=hubs
                ),
                files,
                warn=rep.warn,
            )
        plan = artwork.plan_gamelists(
            fmt,
            art.kinds,
            layout,
            system_list,
            prefix=rule.prefix,
            renames=rule.renames,
            hubs=hubs,
            files=files,
            overlay=state.overlay() if state is not None else None,
        )
    except artwork.ArtworkError as exc:
        rep.warn(f"{source}: {exc}, so no gamelist was written")
        return
    for problem in plan.problems:
        rep.warn(f"{problem}, so the gamelists of that system were left as they are")
    for system in plan.dropped:
        rep.warn(
            f"{system.hub}: the frontend has another system called {system.name!r}, "
            "so no gamelist was written for this one"
        )
    pending = plan.pending()

    def publish(*, applied: bool) -> None:
        if state is not None and not dry_run and state.publish(applied=applied):
            rep.out(f"play state: published {state.where}")

    if not plan.items:
        rep.out("gamelists: no scraped gamelist for the systems on the device yet")
        publish(applied=False)
        return
    if not pending:
        rep.out(f"gamelists: {len(plan.items)} current")
        publish(applied=True)
        return
    label, what = "EmulationStation", "its gamelists"
    if busy(rule.process, label, what, again, rep, probes):
        publish(applied=False)
        return
    # First the ledger: a run cut short between the two loses at worst the plays made in that
    # moment, where the other order would count every merged play as played here.
    publish(applied=True)
    for item in pending:
        if dry_run:
            rep.out(f"gamelists: dry-run: would write {item.path} ({item.games} games)")
        else:
            outcome = safefile.guarded_edit(
                item.path,
                item.data,
                expect=stamps.get(item.path),
                busy=lambda: busy(rule.process, label, what, again, rep, probes),  # it may start
            )
            if outcome is safefile.Edit.BUSY:
                return
            if outcome is safefile.Edit.CHANGED:
                rep.warn(
                    f"{item.path} changed while it was being written, so it was left alone: {again}"
                )
                continue
            rep.out(f"gamelists: wrote {item.path} ({item.games} games, was {item.state})")
    if not dry_run:
        rep.out(f"gamelists: {len(plan.items) - len(pending)} current")


def refresh_artwork(
    cfg: Config,
    name: str,
    profile: Profile,
    home: Path,
    rep: Reporter,
    *,
    local: Client | None,
    dry_run: bool,
    probes: DeviceProbes = REAL_PROBES,
) -> None:
    """`frontend refresh`: reconcile the saves, update the artwork scope, then the gamelists.

    A device whose profile has `[save_convert.*]` and that syncs saves reconciles them
    (`saveconvert_device.convert`), which is how the timer reaches them. That comes first, so a
    failure of the Syncthing API in the artwork steps never skips it, and it needs no `frontend`
    folder group: a device without one only converts. The artwork steps are for a device that
    lists `frontend`.
    """
    folders = cfg.devices[name].folders
    if profile.converts_saves(folders):
        try:
            saveconvert_device.convert(
                name,
                profile,
                home,
                say=rep.out,
                warn=rep.warn,
                dry_run=dry_run,
                proc=probes.proc,
                on_busy=rep.defer,
            )
        except OSError as exc:  # the games after a failed one are still tried by the next run
            rep.warn(f"saves: {exc}, so the saves were left as they are")
    if "frontend" not in folders:
        return
    layout = artwork_layout(profile, home)
    if layout is not None and not artwork_scope(profile, home):
        rep.warn(no_scope_warning(name, layout))
    if local is None:
        rep.warn("local Syncthing not found, so which systems receive artwork was not checked")
    else:
        sync_artwork_scope(cfg, name, profile, home, local, rep)
    ensure_gamelists(
        name,
        profile,
        home,
        rep,
        dry_run=dry_run,
        probes=probes,
        play_state="play-state" in folders,
    )
    if "play-state" in folders:
        diskstatus_io.publish(name, profile, home, say=rep.out, warn=rep.warn, dry_run=dry_run)
