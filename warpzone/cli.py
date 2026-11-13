"""Command line: argparse, exit codes, output."""

from __future__ import annotations

import argparse
import os
import sys
import urllib.parse
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import TYPE_CHECKING

from warpzone import __version__, localst, model, settings, settings_state
from warpzone.config import (
    DEFAULT_MAX_GIB,
    DEFAULT_RESCAN_TIMEOUT,
    DEFAULT_WAIT_SECONDS,
    HUB_WAIT_SECONDS,
    UNATTENDED_WAIT_SECONDS,
    Config,
    ConfigError,
    default_config_path,
    load_config,
    load_inbox_settings,
    load_ra_settings,
)
from warpzone.errors import (
    EXIT_DEFERRED,
    EXIT_OK,
    EXIT_RUNTIME,
    EXIT_USAGE,
    UsageError,
    WarpzoneError,
)
from warpzone.model import ModelError
from warpzone.profiles import Profile, ProfileError, available_profiles, load_profile

# Everything that talks to the network (provision, prune, doctor, the inbox, the Syncthing
# client) is imported inside the command that needs it, so `settings apply`, which ES-DE runs
# at every start, loads none of it (see "Speed" in development/design/emulator-settings.md).
if TYPE_CHECKING:
    from warpzone.dat import DatLibrary
    from warpzone.report import Reporter
    from warpzone.syncthing import Client


def _webhook_url(value: str) -> str:
    if urllib.parse.urlsplit(value).scheme not in ("http", "https"):
        # The URL itself can be a secret, so it is not echoed.
        raise argparse.ArgumentTypeError("must be an http:// or https:// URL")
    return value


def _build_parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument(
        "--config",
        type=Path,
        help="private config file (default: $XDG_CONFIG_HOME/warpzone/config.toml)",
    )
    writing = argparse.ArgumentParser(add_help=False)
    writing.add_argument(
        "--dry-run", action="store_true", help="print the REST calls it would make and make none"
    )

    parser = argparse.ArgumentParser(
        prog="warpzone",
        description="One game library on a home server, delivered to every console.",
    )
    parser.add_argument("--version", action="version", version=f"warpzone {__version__}")
    sub = parser.add_subparsers(dest="command", required=True, metavar="command")

    check = sub.add_parser(
        "check", parents=[common], help="validate config, find missing whitelists"
    )
    check.add_argument("--offline", action="store_true", help="validate files only, skip the hub")
    check.add_argument(
        "--root",
        type=Path,
        help="the hub root as mounted on this machine: also read the device whitelists in its "
        "library/ for systems the device's exclude_systems names (without it, only the copy in "
        "this machine's own library folder)",
    )

    prov = sub.add_parser("provision", help="configure Syncthing")
    prov_sub = prov.add_subparsers(dest="target", required=True, metavar="target")
    hub = prov_sub.add_parser(
        "hub", parents=[common, writing], help="configure the hub (remote API)"
    )
    hub.add_argument(
        "--timeout",
        type=float,
        default=HUB_WAIT_SECONDS,
        help="seconds a new whitelisted folder waits for its whitelists to be indexed "
        "before the full scan starts (default: %(default)s)",
    )
    hub.add_argument(
        "--additive",
        action="store_true",
        help="only create the wz-* folders the hub lacks (never a whitelisted one) and change "
        "nothing that exists; what a device with auto_hub does for itself",
    )
    dev = prov_sub.add_parser(
        "device", parents=[common, writing], help="configure this machine (run on the device)"
    )
    dev.add_argument("name", help="device name from the config")
    dev.add_argument(
        "--syncthing-config", type=Path, help="local Syncthing config.xml or its folder"
    )
    dev.add_argument(
        "--timeout",
        type=float,
        help="seconds to wait for the whitelists to arrive "
        f"(default: {DEFAULT_WAIT_SECONDS:g}, or {UNATTENDED_WAIT_SECONDS:g} with --unattended)",
    )
    dev.add_argument(
        "--unattended",
        action="store_true",
        help="for a run nobody watches: never change the type or versioning of a folder, never "
        "put the bootstrap ignores on an existing folder, skip new folders the hub lacks, "
        "abort when a folder's marker is missing, and only warn about the flatpak grant. "
        f"Exits {EXIT_DEFERRED} when a write waits for the menu to close",
    )

    pr = sub.add_parser(
        "prune",
        parents=[common],
        help="delete files this device no longer whitelists (report only unless --apply)",
    )
    pr.add_argument(
        "name", nargs="?", help="device name (default: the one matching local Syncthing)"
    )
    pr.add_argument(
        "--syncthing-config", type=Path, help="local Syncthing config.xml or its folder"
    )
    pr.add_argument("--apply", action="store_true", help="delete the leftovers (default: report)")
    pr.add_argument(
        "--max-gib",
        type=float,
        help=f"refuse to delete more than this in one run (default: {DEFAULT_MAX_GIB:g})",
    )
    pr.add_argument("--force", action="store_true", help="ignore --max-gib")
    pr.add_argument(
        "--unattended",
        action="store_true",
        help="for the prune timer (needs --apply; not with --max-gib or --force): no size cap, "
        "but delete nothing when a folder would lose more than half of what the device holds "
        "in it; a folder that is still pulling is skipped without failing",
    )
    pr.add_argument("--list", action="store_true", dest="list_all", help="list every file")
    pr.add_argument(
        "--notify-url",
        type=_webhook_url,
        help="POST a JSON summary (device, folder, files, bytes, applied) to this URL",
    )
    pr.add_argument(
        "--timeout",
        type=float,
        default=DEFAULT_RESCAN_TIMEOUT,
        help="seconds to wait for the rescan (default: %(default)s)",
    )

    fe = sub.add_parser("frontend", help="the frontend's artwork and gamelists")
    fe_sub = fe.add_subparsers(dest="frontend_command", required=True, metavar="action")
    refresh = fe_sub.add_parser(
        "refresh",
        parents=[common, writing],
        help="update the artwork scope and rewrite the frontend's gamelists from the "
        "scraped data (run on the device; never while the frontend is open)",
    )
    refresh.add_argument(
        "name", nargs="?", help="device name (default: the one matching local Syncthing)"
    )
    refresh.add_argument(
        "--syncthing-config", type=Path, help="local Syncthing config.xml or its folder"
    )

    ps = sub.add_parser("play-state", help="favourites and play statistics across the frontends")
    ps_sub = ps.add_subparsers(dest="play_state_command", required=True, metavar="action")
    ps_sync = ps_sub.add_parser(
        "sync",
        parents=[common],
        help="publish this device's favourites and play statistics and merge the others' into "
        "ES-DE's gamelists (run on the device; gamelists wait until ES-DE is closed)",
    )
    ps_sync.add_argument("name", help="device name")
    ps_sync.add_argument(
        "--dry-run", action="store_true", help="say what it would do and write nothing"
    )
    ps_sync.add_argument("--quiet", action="store_true", help="print nothing")
    ps_sync.add_argument(
        "--frontend-starting",
        action="store_true",
        help="run from ES-DE's startup script: ES-DE waits and has read no gamelist yet",
    )

    ln = sub.add_parser("links", help="link library games into an emulator's own game folder")
    ln_sub = ln.add_subparsers(dest="links_command", required=True, metavar="action")
    ln_sync = ln_sub.add_parser(
        "sync",
        parents=[common],
        help="make the folders of the profile's [links.*] rules hold a symlink per library "
        "game (run on the device; nothing changes while the emulator runs)",
    )
    ln_sync.add_argument("name", help="device name")
    ln_sync.add_argument(
        "--dry-run", action="store_true", help="say what it would do and change nothing"
    )
    ln_sync.add_argument("--quiet", action="store_true", help="print nothing")

    sv = sub.add_parser("saves", help="save files an emulator keeps in its own format")
    sv_sub = sv.add_subparsers(dest="saves_command", required=True, metavar="action")
    sv_convert = sv_sub.add_parser(
        "convert",
        parents=[common],
        help="reconcile the profile's [save_convert.*] emulators with the hub's raw saves "
        "(run on the device; nothing changes while the emulator runs)",
    )
    sv_convert.add_argument("name", help="device name")
    sv_convert.add_argument(
        "--dry-run", action="store_true", help="say what it would do and change nothing"
    )
    sv_convert.add_argument("--quiet", action="store_true", help="print nothing")

    chk = sub.add_parser(
        "bios",
        parents=[common],
        help="report which BIOS and firmware files a library has, lacks or has wrong",
    )
    chk.add_argument(
        "--root",
        type=Path,
        help="library folder holding bios/ and roms/ (default: this device's, else the hub's)",
    )
    chk.add_argument(
        "--system",
        nargs="+",
        action="extend",
        metavar="NAME",
        help="check only these systems (default: all)",
    )
    chk.add_argument("--all", action="store_true", dest="list_all", help="list unknown files too")
    chk.add_argument(
        "--overrides",
        type=Path,
        help="local file with accepted hashes (default: bios.toml next to the config file)",
    )

    inb = sub.add_parser(
        "inbox",
        parents=[common],
        help="identify, convert and file new games dropped in inbox/ (report only unless --apply)",
    )
    inb.add_argument(
        "--root",
        type=Path,
        help="the hub root as mounted on this machine, holding inbox/ and library/ "
        "(default: [inbox] root in the config)",
    )
    inb.add_argument(
        "--dats",
        type=Path,
        help="folder with the Redump and No-Intro DATs (default: [inbox] dats_dir)",
    )
    inb.add_argument(
        "--apply",
        action="store_true",
        help="convert, place and move the originals (default: report)",
    )
    inb.add_argument(
        "--force-lock",
        action="store_true",
        help="with --apply, take over a stale inbox lock (older than 12 hours, or held by a "
        "process that is gone from this machine); a live lock is never taken",
    )
    inb.add_argument(
        "--no-scrape",
        action="store_true",
        help="with --apply, do not scrape the games it placed (default: scrape them when "
        "Skyscraper is configured or found)",
    )
    inb.add_argument(
        "--work-dir",
        type=Path,
        help="local folder for unpacked archives and conversions (default: [inbox] work_dir, "
        "else the system temp folder)",
    )

    scr = sub.add_parser(
        "scrape",
        parents=[common],
        help="fill the hub's gamelists and media for the games that lack them, with Skyscraper "
        "(ScreenScraper); only adds, never changes an entry or a file",
    )
    scr.add_argument(
        "--root",
        type=Path,
        help="the hub root as mounted on this machine, holding inbox/ and library/ "
        "(default: [inbox] root in the config)",
    )
    scr.add_argument(
        "--system",
        nargs="+",
        action="extend",
        metavar="NAME",
        help="scrape only these systems (default: every system warpzone scrapes)",
    )
    scr.add_argument(
        "--retry-not-found",
        action="store_true",
        help="also try the games ScreenScraper did not know in the last 30 days",
    )
    scr.add_argument(
        "--dry-run",
        action="store_true",
        help="list the games it would scrape per system and write nothing (runs no Skyscraper)",
    )
    scr.add_argument(
        "--force-lock",
        action="store_true",
        help="take over a stale inbox lock (older than 12 hours, or held by a process that is "
        "gone from this machine); a live lock is never taken",
    )

    cat = sub.add_parser(
        "catalog",
        parents=[common],
        help="build and update the catalog index in catalog/ from library/",
    )
    cat.add_argument(
        "--dry-run",
        action="store_true",
        help="say what it would change per system and write nothing",
    )
    cat.add_argument(
        "--root",
        type=Path,
        help="the hub root as mounted on this machine, holding library/ (default: [hub] root)",
    )
    cat.add_argument(
        "--ra-hash",
        action="store_true",
        help="run RAHasher on the games of the systems RetroAchievements knows that have no "
        "RA hash yet (reads every such image once; can be stopped and run again)",
    )
    cat.add_argument(
        "--rahasher",
        metavar="PATH",
        help="RAHasher for --ra-hash (default: [inbox] rahasher)",
    )
    cat.add_argument(
        "--ra-lists",
        action="store_true",
        help="download the RetroAchievements game list of each console the library has "
        "(key from RA_API_KEY), unless the cached one is under 7 days old",
    )
    cat.add_argument(
        "--ra-achievements",
        action="store_true",
        help="download the achievements of the library's RetroAchievements games, the badge "
        "images and, with [ra] user in the config, that user's progress (key from RA_API_KEY; "
        "run --ra-lists first)",
    )
    cat.add_argument(
        "--dats",
        action="store_true",
        help="fill the DAT serials (Redump) of the games that have none, by exact DAT name or "
        "SHA-1 (what lets a Flycast memory card be matched to a game filed before serials "
        "were recorded)",
    )
    cat.add_argument(
        "--dats-dir",
        type=Path,
        metavar="PATH",
        help="the DAT folder for --dats (default: [inbox] dats_dir)",
    )
    cat.add_argument(
        "--force",
        action="store_true",
        help="with --ra-lists or --ra-achievements, download again whatever the age; with "
        "--dats, replace serials a game already has when the DAT gives it other ones",
    )

    wlst = sub.add_parser("whitelist", help="the device whitelists on the hub")
    wl_sub = wlst.add_subparsers(dest="whitelist_command", required=True, metavar="action")
    wl_migrate = wl_sub.add_parser(
        "migrate",
        parents=[common],
        help="convert hand-written patterns-<device>.stignore files to the library UI's format, "
        "proving the included paths stay the same (run where the hub root is mounted)",
    )
    wl_migrate.add_argument(
        "devices",
        nargs="*",
        metavar="device",
        help="device names (default: every device with the library folder)",
    )
    wl_migrate.add_argument(
        "--root",
        type=Path,
        help="the hub root as mounted on this machine (default: [hub] root in the config)",
    )
    wl_migrate.add_argument(
        "--dry-run",
        action="store_true",
        help="print the new files and the dropped lines, write nothing",
    )

    wl_restore = wl_sub.add_parser(
        "restore",
        parents=[common],
        help="list the backups of a device's whitelist, or restore one (run where the hub root "
        "is mounted)",
    )
    wl_restore.add_argument("device", metavar="device", help="a device name from the config")
    wl_restore.add_argument(
        "backup",
        nargs="?",
        help="the backup to restore (its name, or the start of it); without it, list them",
    )
    wl_restore.add_argument(
        "--root",
        type=Path,
        help="the hub root as mounted on this machine (default: [hub] root in the config)",
    )
    wl_restore.add_argument(
        "--dry-run",
        action="store_true",
        help="show the diff and the paths it turns on and off, write nothing",
    )

    sv_ui = sub.add_parser(
        "serve",
        parents=[common],
        help="serve the library UI and its JSON API (run where the hub root is mounted; "
        "token in WARPZONE_UI_TOKEN or [serve] token_file)",
    )
    sv_ui.add_argument(
        "--listen",
        metavar="HOST:PORT",
        help="the address to listen on (default: [serve] listen, 0.0.0.0:8780)",
    )

    st_dry_run = argparse.ArgumentParser(add_help=False)
    st_dry_run.add_argument(
        "--dry-run", action="store_true", help="print what it would do and change nothing"
    )
    st = sub.add_parser(
        "settings", help="emulator settings: apply the layers, capture changes, audit drift"
    )
    st_sub = st.add_subparsers(dest="settings_command", required=True, metavar="action")
    st_common = argparse.ArgumentParser(add_help=False)
    st_common.add_argument(
        "--device", metavar="NAME", help="device name (default: the one matching local Syncthing)"
    )
    st_common.add_argument(
        "--emulator",
        nargs="+",
        action="extend",
        metavar="ID",
        default=[],
        help="only these emulators (default: all)",
    )
    st_apply = st_sub.add_parser(
        "apply",
        parents=[common, st_common, st_dry_run],
        help="write the layered keys into the emulators' files (never while one runs)",
    )
    st_apply.add_argument("--quiet", action="store_true", help="print nothing")
    st_apply.add_argument(
        "--overwrite-local",
        action="store_true",
        help="also write keys changed in an emulator and not captured yet",
    )
    st_apply.add_argument(
        "--when-idle",
        action="store_true",
        help="unattended run (the timer): leave out the emulators that are running, leave no "
        "trace when nothing was written, and exit 0 even when a file could not be written",
    )
    st_capture = st_sub.add_parser(
        "capture",
        parents=[common, st_common, st_dry_run],
        help="take settings changed in an emulator's own interface to the layers on the hub",
    )
    st_capture.add_argument(
        "--syncthing-config", type=Path, help="local Syncthing config.xml or its folder"
    )
    st_capture.add_argument(
        "--seed",
        action="store_true",
        help="also propose the known keys (and --key ones) of files with no baseline yet",
    )
    st_capture.add_argument(
        "--key",
        nargs="+",
        action="extend",
        metavar="PATTERN",
        default=[],
        help="with --seed, also propose keys matching <emulator>/<file>/<key>",
    )
    st_capture.add_argument(
        "--include-new", action="store_true", help="also propose keys new to a file"
    )
    st_capture.add_argument(
        "--yes", action="store_true", help="without a terminal, take every pre-selection"
    )
    for flag, what in (("to-common", "common"), ("to-device", "this device's layer")):
        st_capture.add_argument(
            f"--{flag}",
            nargs="+",
            action="extend",
            metavar="PATTERN",
            default=[],
            help=f"without a terminal, send matching <emulator>/<file>/<key> to {what}",
        )
    st_capture.add_argument(
        "--discard",
        nargs="+",
        action="extend",
        metavar="PATTERN",
        default=[],
        help="without a terminal, discard matching keys (the layer value returns at apply)",
    )
    st_capture.add_argument(
        "--offline",
        action="store_true",
        help="do not ask Syncthing whether wz-settings is up to date",
    )
    st_audit = st_sub.add_parser(
        "audit",
        parents=[common, st_common],
        help="report drift between the layers and the files (read only)",
    )
    st_audit.add_argument(
        "--all", action="store_true", dest="all_keys", help="also list keys in sync"
    )

    su = sub.add_parser(
        "self-update",
        help="on a device: run one tick of the updater, as its timer does (look for a "
        "release, switch to it, verify it, go back if it fails)",
    )
    su_mode = su.add_mutually_exclusive_group()
    su_mode.add_argument(
        "--now",
        action="store_true",
        help="update now, as asked for by hand: look for a new release even if the last look "
        "was recent, and do not wait for the menu to close",
    )
    su_mode.add_argument(
        "--channel",
        choices=("stable", "rc"),
        help="instead of a tick: make this device follow stable releases or release candidates "
        "(edits the device's channel in its config, keeping a backup, and the updater's settings)",
    )
    su.add_argument(
        "--dry-run",
        action="store_true",
        help="with --channel: say what would be written and write nothing",
    )

    hl = sub.add_parser(
        "health",
        parents=[common],
        help="on a device: check that this version works (config, the dry runs of what ES-DE "
        "and the timers run, the startup scripts and units); writes nothing",
    )
    hl.add_argument(
        "name", nargs="?", help="device name (default: the one matching local Syncthing)"
    )
    hl.add_argument(
        "--syncthing-config", type=Path, help="local Syncthing config.xml or its folder"
    )

    sub.add_parser(
        "doctor",
        parents=[common],
        help="check this machine (tools, config, Syncthing, DATs, hub) and say how to fix it",
    )
    sub.add_parser("status", parents=[common], help="folder state on the hub and this machine")
    render = sub.add_parser(
        "render-ignores", parents=[common], help="print the ignore lines a device would get"
    )
    render.add_argument("name", help="device name from the config")
    return parser


def _load(args: argparse.Namespace) -> tuple[Config, dict[str, Profile]]:
    path = _config_path(args)
    cfg = load_config(path, set(available_profiles()))
    profiles = {
        name: load_profile(name) for name in sorted({d.profile for d in cfg.devices.values()})
    }
    # Surface "subscribed to a folder the profile does not define" at load time.
    for device in cfg.devices.values():
        model.device_specs(cfg, device.name, profiles[device.profile])
    return cfg, profiles


def _device(cfg: Config, name: str) -> str:
    if name not in cfg.devices:
        raise ConfigError(f"unknown device {name!r} (configured: {', '.join(sorted(cfg.devices))})")
    return name


def _this_device(cfg: Config, client: Client) -> str:
    mine = client.my_id()
    names = [d.name for d in cfg.devices.values() if d.device_id == mine]
    if len(names) != 1:
        raise ConfigError("cannot tell which configured device this is; pass its name")
    return names[0]


def _eprint(message: str) -> None:
    print(message, file=sys.stderr)


def _client(
    base_url: str,
    api_key: str,
    *,
    label: str,
    dry_run: bool = False,
    cert_sha256: str | None = None,
) -> Client:
    from warpzone.syncthing import Client

    return Client(base_url, api_key, label=label, dry_run=dry_run, cert_sha256=cert_sha256)


def _hub_client(cfg: Config, *, dry_run: bool = False) -> Client:
    """The only place a client for the hub's API is built, so the certificate pin applies to
    every command that talks to it."""
    return _client(
        cfg.hub.api_url,
        cfg.hub.api_key,
        label="hub",
        dry_run=dry_run,
        cert_sha256=cfg.hub.api_cert_sha256,
    )


def _config_path(args: argparse.Namespace) -> Path:
    return args.config or Path(os.environ.get("WARPZONE_CONFIG") or default_config_path())


def _bios(args: argparse.Namespace, rep: Reporter) -> int:
    from warpzone import bios
    from warpzone.bios import BiosError

    manifest = bios.load_manifest()
    overrides = bios.load_overrides(
        args.overrides or bios.default_overrides_path(_config_path(args)),
        manifest,
        must_exist=args.overrides is not None,
    )
    root = args.root
    if root is None:
        try:
            cfg, profiles = _load(args)
        except ConfigError as exc:
            raise BiosError(f"no --root given and the config is not usable:\n{exc}") from exc
        root = bios.default_root(cfg, profiles, Path.home(), Path.is_dir)
        if root is None:
            raise BiosError("no library folder found on this machine; pass --root")
    lib = bios.Library(root, manifest.skip_dirs)
    if not lib.has_content():
        raise BiosError(f"{root} has no bios/ or roms/ folder; is --root the library folder?")
    report = bios.check_library(manifest, overrides, lib, args.system)
    rep.out(f"library: {root}")
    for line in bios.render(report, show_all=args.list_all):
        rep.out(line)
    return EXIT_RUNTIME if report.problems else EXIT_OK


def _catalog(args: argparse.Namespace, rep: Reporter) -> int:
    from warpzone import catalog_store, ra, ra_store
    from warpzone.catalog_store import CatalogError

    if args.rahasher and not args.ra_hash:
        raise CatalogError("--rahasher only works together with --ra-hash")
    if args.dats_dir and not args.dats:
        raise CatalogError("--dats-dir only works together with --dats")
    if args.force and not (args.ra_lists or args.ra_achievements or args.dats):
        raise CatalogError(
            "--force only works together with --ra-lists, --ra-achievements or --dats"
        )
    root = args.root
    if root is None:
        root = Path(_load(args)[0].hub.root)
    hasher = None
    if args.ra_hash:
        configured = args.rahasher or load_inbox_settings(_config_path(args)).rahasher
        hasher = catalog_store.rahasher_runner(catalog_store.resolve_rahasher(configured))
    key = os.environ.get(ra.KEY_ENV) or None
    if (args.ra_lists or args.ra_achievements) and not key:  # before the scan: costs nothing
        raise CatalogError(f"no RetroAchievements key: set {ra.KEY_ENV} in the environment")
    dat_library = _dat_library(args) if args.dats else None
    results = catalog_store.run_catalog(root, dry_run=args.dry_run, out=rep.out, ra_hasher=hasher)
    failed = any(r.failed for r in results)
    if args.ra_lists:
        lists = catalog_store.run_ra_lists(
            root,
            key=key,
            force=args.force,
            dry_run=args.dry_run,
            out=rep.out,
            warn=rep.warn,
        )
        failed = failed or any(r.failed for r in lists)
    if dat_library is not None:
        serials = catalog_store.run_dats(
            root,
            dat_library.load,
            force=args.force,
            dry_run=args.dry_run,
            out=rep.out,
        )
        failed = failed or any(r.failed for r in serials)
    if args.ra_achievements:
        found = ra_store.run_ra_achievements(
            root,
            key=key,
            user=load_ra_settings(_config_path(args)).user,
            force=args.force,
            dry_run=args.dry_run,
            out=rep.out,
            warn=rep.warn,
        )
        failed = failed or found.failed
    return EXIT_RUNTIME if failed else EXIT_OK


def _dat_library(args: argparse.Namespace) -> DatLibrary:
    """The DATs `catalog --dats` reads: the folder `inbox` uses, through its parse cache."""
    from warpzone import inbox
    from warpzone.catalog_store import CatalogError
    from warpzone.dat import DatLibrary

    configured = load_inbox_settings(_config_path(args)).dats_dir
    folder = args.dats_dir or (Path(configured) if configured else None)
    if folder is None:
        raise CatalogError("no DATs: pass --dats-dir or set `dats_dir` in the [inbox] table")
    if not folder.is_dir():
        raise CatalogError(f"DAT folder not found: {folder}")
    library = DatLibrary(folder, inbox.default_cache_dir())
    if not library.systems():
        raise CatalogError(f"no Redump or No-Intro DATs warpzone knows in {folder}")
    return library


def _inbox(args: argparse.Namespace, rep: Reporter) -> int:
    from warpzone import inbox, inbox_core, ra
    from warpzone.inbox_core import InboxError

    settings = load_inbox_settings(_config_path(args))
    root = args.root or (Path(settings.root) if settings.root else None)
    dats = args.dats or (Path(settings.dats_dir) if settings.dats_dir else None)
    if root is None:
        raise InboxError(
            "no hub root: pass --root or set `root` in the [inbox] table of the config"
        )
    if dats is None:
        raise InboxError(
            "no DATs: pass --dats or set `dats_dir` in the [inbox] table of the config"
        )
    work = args.work_dir or (Path(settings.work_dir) if settings.work_dir else None)
    options = inbox_core.Options(
        root=root,
        dats_dir=dats,
        tools=inbox_core.find_tools(settings),
        apply=args.apply,
        force_lock=args.force_lock,
        no_scrape=args.no_scrape,
        work_dir=work,
        cache_dir=inbox.default_cache_dir(),
        ra_key=os.environ.get(ra.KEY_ENV) or None,
        settings=settings,
    )
    return EXIT_RUNTIME if inbox.run_inbox(options, rep) else EXIT_OK


def _scrape(args: argparse.Namespace, rep: Reporter) -> int:
    from warpzone import scrape, scrape_io
    from warpzone.inbox_core import InboxError

    settings = load_inbox_settings(_config_path(args))
    root = args.root or (Path(settings.root) if settings.root else None)
    if root is None:
        raise InboxError(
            "no hub root: pass --root or set `root` in the [inbox] table of the config"
        )
    systems = scrape.choose_systems(args.system)
    program = scrape_io.find_skyscraper(settings.skyscraper)
    if program is None:
        raise scrape_io.ScrapeError(
            "Skyscraper not found: put it on PATH or set `skyscraper` in the [inbox] table "
            "(https://github.com/Gemba/skyscraper/releases)"
        )
    options = scrape_io.Options(
        root=root,
        skyscraper=program,
        systems=tuple(systems),
        explicit=bool(args.system),
        timeout=settings.tool_timeout_minutes * 60.0,
        work_dir=Path(settings.work_dir) if settings.work_dir else None,
        config_ini=scrape.config_ini_path(os.environ, Path.home(), Path.exists),
        dry_run=args.dry_run,
        retry_not_found=args.retry_not_found,
        force_lock=args.force_lock,
    )
    return scrape_io.run_scrape(options, rep)


def _settings(args: argparse.Namespace, cfg: Config, profiles: dict[str, Profile]) -> int:
    capturing = args.settings_command == "capture"
    if capturing and args.key and not args.seed:
        raise ConfigError("--key only works together with --seed")
    if args.device:
        name = _device(cfg, args.device)
    client: Client | None = None
    if not args.device or (capturing and not args.offline):
        local = localst.parse_config(localst.find_config(getattr(args, "syncthing_config", None)))
        client = _client(local.base_url, local.api_key, label="local")
    if not args.device:
        assert client is not None
        name = _this_device(cfg, client)
    ctx = settings.make_context(
        name,
        profiles[cfg.devices[name].profile],
        settings.Env(Path.home(), settings_state.state_dir()),
    )
    if args.settings_command == "audit":
        return settings.audit(ctx, emulators=args.emulator, all_keys=args.all_keys)
    if capturing:
        return settings.capture(
            ctx,
            None if args.offline else client,
            emulators=args.emulator,
            seed=args.seed,
            key=args.key,
            include_new=args.include_new,
            yes=args.yes,
            to_common=args.to_common,
            to_device=args.to_device,
            discard=args.discard,
            offline=args.offline,
            dry_run=args.dry_run,
            out=print,
            err=_eprint,
        )
    return settings.apply(
        ctx,
        emulators=args.emulator,
        dry_run=args.dry_run,
        quiet=args.quiet,
        overwrite_local=args.overwrite_local,
        when_idle=args.when_idle,
        out=print,
        err=_eprint,
    )


def _frontend(
    args: argparse.Namespace, cfg: Config, profiles: dict[str, Profile], rep: Reporter
) -> int:
    from warpzone import provision

    name = _device(cfg, args.name) if args.name else None
    client: Client | None = None
    try:
        local = localst.parse_config(localst.find_config(args.syncthing_config))
        client = _client(local.base_url, local.api_key, label="local", dry_run=args.dry_run)
    except localst.LocalSyncthingError:
        if args.syncthing_config is not None or name is None:
            raise
    if name is None:
        assert client is not None
        name = _this_device(cfg, client)
    device, profile = cfg.devices[name], profiles[cfg.devices[name].profile]
    if profile.artwork is None:
        raise ConfigError(f"profile {profile.name!r} has no [artwork] table: nothing to refresh")
    if "frontend" not in device.folders and not profile.converts_saves(device.folders):
        raise ConfigError(
            f"device {name!r} does not list the 'frontend' folder group, so it receives no artwork"
        )
    provision.refresh_artwork(
        cfg,
        name,
        profile,
        Path.home(),
        rep,
        local=client,
        dry_run=args.dry_run,
    )
    return EXIT_OK


def _play_state(args: argparse.Namespace, cfg: Config, profiles: dict[str, Profile]) -> int:
    from warpzone import diskstatus_io, playstate_device

    name = _device(cfg, args.name)
    device, profile = cfg.devices[name], profiles[cfg.devices[name].profile]
    if "play-state" not in device.folders:
        raise ConfigError(f"device {name!r} does not list the 'play-state' folder group")
    if profile.artwork is not None:
        raise ConfigError(f"device {name!r} merges with `warpzone frontend refresh {name}`")
    quiet = args.quiet

    def say(text: str) -> None:
        if not quiet:
            print(text)

    def warn(text: str) -> None:
        if not quiet:
            _eprint(f"warning: {text}")

    playstate_device.sync_shared(
        name,
        profile,
        Path.home(),
        say=say,
        warn=warn,
        dry_run=args.dry_run,
        starting=args.frontend_starting,
    )
    diskstatus_io.publish(name, profile, Path.home(), say=say, warn=warn, dry_run=args.dry_run)
    return EXIT_OK


def _links(args: argparse.Namespace, cfg: Config, profiles: dict[str, Profile]) -> int:
    from warpzone import titlelinks

    name = _device(cfg, args.name)
    device, profile = cfg.devices[name], profiles[cfg.devices[name].profile]
    if "library" not in device.folders:
        raise ConfigError(f"device {name!r} does not list the 'library' folder group")
    if not profile.links:
        raise ConfigError(f"profile {profile.name!r} has no [links.*] table: nothing to link")
    quiet = args.quiet

    def say(text: str) -> None:
        if not quiet:
            print(text)

    def warn(text: str) -> None:
        if not quiet:
            _eprint(f"warning: {text}")

    titlelinks.sync(name, profile, Path.home(), say=say, warn=warn, dry_run=args.dry_run)
    return EXIT_OK


def _saves(args: argparse.Namespace, cfg: Config, profiles: dict[str, Profile]) -> int:
    from warpzone import saveconvert_device

    name = _device(cfg, args.name)
    device, profile = cfg.devices[name], profiles[cfg.devices[name].profile]
    if "saves" not in device.folders:
        raise ConfigError(f"device {name!r} does not list the 'saves' folder group")
    if not profile.save_convert:
        raise ConfigError(
            f"profile {profile.name!r} has no [save_convert.*] table: nothing to convert"
        )
    quiet = args.quiet

    def say(text: str) -> None:
        if not quiet:
            print(text)

    def warn(text: str) -> None:
        if not quiet:
            _eprint(f"warning: {text}")

    outcome = saveconvert_device.convert(
        name, profile, Path.home(), say=say, warn=warn, dry_run=args.dry_run
    )
    return EXIT_RUNTIME if outcome.errors else EXIT_OK


def _self_update(args: argparse.Namespace, rep: Reporter) -> int:
    from warpzone import selfupdate

    if args.dry_run and args.channel is None:
        _eprint("warpzone self-update: --dry-run goes only with --channel (a tick has no dry run)")
        return EXIT_USAGE
    if args.channel is not None:
        conf = selfupdate.conf_path(settings_state.state_dir().parent)
        return selfupdate.change_channel(
            conf,
            args.channel,
            rep.out,
            version=__version__,
            default_config=Path(os.environ.get("WARPZONE_CONFIG") or default_config_path()),
            dry_run=args.dry_run,
        )
    return selfupdate.run_tick(selfupdate.live_path(Path.home()), rep.out, fetch_now=args.now)


def _health(args: argparse.Namespace, rep: Reporter) -> int:
    from warpzone import health

    def local() -> localst.LocalSyncthing | None:
        try:
            return localst.parse_config(localst.find_config(args.syncthing_config))
        except localst.LocalSyncthingError:
            if args.syncthing_config is not None:
                raise
            return None  # this machine has no Syncthing: the checks that need it say so

    probes = health.Probes(home=Path.home(), state=settings_state.state_dir(), local=local)
    return health.run_health(lambda: _load(args), args.name, probes, rep.out)


def _whitelist(
    args: argparse.Namespace, cfg: Config, profiles: dict[str, Profile], rep: Reporter
) -> int:
    from warpzone import whitelist_store

    root = args.root or Path(cfg.hub.root)
    if args.whitelist_command == "restore":
        refused = whitelist_store.restore_device(
            root,
            _device(cfg, args.device),
            args.backup,
            dry_run=args.dry_run,
            now=None,
            out=print,
            scan_client=lambda: _hub_client(cfg),
            warn=lambda text: _eprint(f"warning: {text}"),
        )
        return 1 if refused else 0
    names = [_device(cfg, n) for n in args.devices] or [
        n for n, d in sorted(cfg.devices.items()) if "library" in d.folders
    ]
    return whitelist_store.migrate(
        root,
        names,
        dry_run=args.dry_run,
        out=print,
        warn=lambda text: _eprint(f"warning: {text}"),
        scan_client=lambda: _hub_client(cfg),
    )


def _doctor(args: argparse.Namespace, rep: Reporter) -> int:
    from warpzone import doctor

    return doctor.run_doctor(doctor.Probes(config_path=_config_path(args)), rep.out)


def _serve(
    args: argparse.Namespace, cfg: Config, profiles: dict[str, Profile], rep: Reporter
) -> int:
    from warpzone import serve

    return serve.run(cfg, listen=args.listen, client_factory=lambda: _hub_client(cfg), log=_eprint)


def _check(
    args: argparse.Namespace, cfg: Config, profiles: dict[str, Profile], rep: Reporter
) -> int:
    from warpzone import provision, whitelist_store

    rep.out(f"config ok: {len(cfg.devices)} device(s), profiles: {', '.join(profiles)}")
    excluded, notes = whitelist_store.excluded_system_report(
        cfg, profiles, root=args.root, home=Path.home()
    )
    for text in [*notes, *excluded]:
        rep.out(text)
    problems = len(excluded)
    if not args.offline:
        problems += provision.check_hub(cfg, profiles, _hub_client(cfg), rep)
    return EXIT_RUNTIME if problems else EXIT_OK


def _render_ignores(
    args: argparse.Namespace, cfg: Config, profiles: dict[str, Profile], rep: Reporter
) -> int:
    from warpzone import provision

    name = _device(cfg, args.name)
    profile = profiles[cfg.devices[name].profile]
    # Artwork folders let in the systems that have a folder in this machine's library.
    scope = provision.artwork_scope(profile, Path.home())
    for spec in model.device_specs(cfg, name, profile, scope):
        rep.out(f"# {spec.id}")
        if spec.bootstrap_ignores is not None:
            rep.out("# bootstrap, replaced once the whitelist has arrived:")
            rep.out("\n".join(f"#   {line}" for line in spec.bootstrap_ignores))
        rep.out("\n".join(spec.ignores))
    hub_library = {s.id: s for s in model.hub_specs(cfg, profiles)}.get(model.LIBRARY_ID)
    if hub_library is not None:
        rep.out("# hub .stignore of the library folder:")
        rep.out("\n".join(hub_library.ignores))
    return EXIT_OK


def _status(
    args: argparse.Namespace, cfg: Config, profiles: dict[str, Profile], rep: Reporter
) -> int:
    from warpzone import provision

    provision.folder_status(_hub_client(cfg), "hub", rep)
    try:
        local = localst.parse_config(localst.find_config())
    except localst.LocalSyncthingError:
        return EXIT_OK  # no Syncthing on this machine: hub status is enough
    client = _client(local.base_url, local.api_key, label="local")
    provision.folder_status(client, "this machine", rep)
    return EXIT_OK


def _prune(
    args: argparse.Namespace, cfg: Config, profiles: dict[str, Profile], rep: Reporter
) -> int:
    from warpzone import prune

    if args.unattended and (not args.apply or args.max_gib is not None or args.force):
        raise UsageError("prune --unattended needs --apply and takes no --max-gib or --force")
    local = localst.parse_config(localst.find_config(args.syncthing_config))
    client = _client(local.base_url, local.api_key, label="local")
    name = args.name or _this_device(cfg, client)
    problems = prune.prune_device(
        cfg,
        _device(cfg, name),
        profiles[cfg.devices[name].profile],
        client,
        rep,
        home=Path.home(),
        apply=args.apply,
        max_gib=DEFAULT_MAX_GIB if args.max_gib is None else args.max_gib,
        force=args.force,
        list_all=args.list_all,
        notify_url=args.notify_url,
        rescan_timeout=args.timeout,
        unattended=args.unattended,
        state_base=settings_state.state_dir().parent,
    )
    return EXIT_RUNTIME if problems else EXIT_OK


def _provision(
    args: argparse.Namespace, cfg: Config, profiles: dict[str, Profile], rep: Reporter
) -> int:
    from warpzone import provision

    if args.target == "hub" and args.additive:
        provision.provision_hub_additive(cfg, profiles, _hub_client(cfg, dry_run=args.dry_run), rep)
        return EXIT_OK

    if args.target == "hub":
        provision.provision_hub(
            cfg,
            profiles,
            _hub_client(cfg, dry_run=args.dry_run),
            rep,
            wait_timeout=args.timeout,
        )
        return EXIT_OK

    name = _device(cfg, args.name)
    local = localst.parse_config(localst.find_config(args.syncthing_config))
    client = _client(local.base_url, local.api_key, label="local", dry_run=args.dry_run)
    wait = args.timeout
    if wait is None:
        wait = UNATTENDED_WAIT_SECONDS if args.unattended else DEFAULT_WAIT_SECONDS
    provision.provision_device(
        cfg,
        name,
        profiles[cfg.devices[name].profile],
        client,
        _hub_client(cfg, dry_run=args.dry_run),
        rep,
        home=Path.home(),
        profiles=profiles,
        wait_timeout=wait,
        syncthing_config=local.config_path,
        config_path=_config_path(args),
        unattended=args.unattended,
    )
    return EXIT_DEFERRED if args.unattended and rep.deferred else EXIT_OK


# Dispatch: the command argparse parsed (`args.command`) to the function that runs it, in three
# tables by what the command needs before it can run. A new command is a parser in
# `_build_parser`, a handler function, and one line in the table that fits; a test fails when a
# parser has no handler.
#
# These need the config but no network code: ES-DE runs them around the emulator, so they load
# none of it (see "Speed" in development/design/emulator-settings.md).
_LOCAL_COMMANDS: dict[str, Callable[[argparse.Namespace, Config, dict[str, Profile]], int]] = {
    "settings": _settings,
    "play-state": _play_state,
    "links": _links,
    "saves": _saves,
}
# These work without a valid config (some need none) and get the `Reporter`.
_STANDALONE_COMMANDS: dict[str, Callable[[argparse.Namespace, Reporter], int]] = {
    "self-update": _self_update,  # needs no config
    "health": _health,  # a failed check is a result, not a usage error
    "doctor": _doctor,  # offline, read-only, needs no valid config
    "bios": _bios,  # works without a config when --root is given
    "inbox": _inbox,  # needs at most the [inbox] table
    "scrape": _scrape,  # the same: the [inbox] table only
    "catalog": _catalog,  # needs the config only for the hub root
}
# These load and validate the config first. Every handler takes the same four arguments,
# whether it uses them all or not.
_CONFIG_COMMANDS: dict[
    str, Callable[[argparse.Namespace, Config, dict[str, Profile], Reporter], int]
] = {
    "whitelist": _whitelist,
    "serve": _serve,
    "check": _check,
    "render-ignores": _render_ignores,
    "status": _status,
    "frontend": _frontend,
    "prune": _prune,
    "provision": _provision,
}


def _run(args: argparse.Namespace) -> int:
    command: str = args.command
    if command in _LOCAL_COMMANDS:
        return _LOCAL_COMMANDS[command](args, *_load(args))
    from warpzone.report import Reporter

    rep = Reporter(print, warn_sink=_eprint)
    if command in _STANDALONE_COMMANDS:
        return _STANDALONE_COMMANDS[command](args, rep)
    cfg, profiles = _load(args)
    return _CONFIG_COMMANDS[command](args, cfg, profiles, rep)


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        return _run(args)
    except (ConfigError, ProfileError, ModelError) as exc:
        _eprint(f"warpzone: config error: {exc}")
        if args.command == "settings" and args.settings_command == "apply" and not args.dry_run:
            settings.record_refused_apply(settings_state.state_dir(), EXIT_USAGE)
        return EXIT_USAGE
    except WarpzoneError as exc:
        _eprint(f"warpzone: error: {exc}")
        return exc.exit_code
    except KeyboardInterrupt:
        _eprint("warpzone: interrupted")
        return 130
