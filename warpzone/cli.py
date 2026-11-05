"""Command line: argparse, exit codes, output."""

from __future__ import annotations

import argparse
import os
import sys
import urllib.parse
from collections.abc import Sequence
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
)
from warpzone.errors import EXIT_DEFERRED, EXIT_OK, EXIT_RUNTIME, EXIT_USAGE, WarpzoneError
from warpzone.model import ModelError
from warpzone.profiles import Profile, ProfileError, available_profiles, load_profile

# Everything that talks to the network (provision, prune, doctor, the inbox, the Syncthing
# client) is imported inside the command that needs it, so `settings apply`, which ES-DE runs
# at every start, loads none of it (see "Speed" in development/design/emulator-settings.md).
if TYPE_CHECKING:
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
        default=DEFAULT_MAX_GIB,
        help="refuse to delete more than this in one run (default: %(default)s)",
    )
    pr.add_argument("--force", action="store_true", help="ignore --max-gib")
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
        "--work-dir",
        type=Path,
        help="local folder for unpacked archives and conversions (default: [inbox] work_dir, "
        "else the system temp folder)",
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


def _client(base_url: str, api_key: str, *, label: str, dry_run: bool = False) -> Client:
    from warpzone.syncthing import Client

    return Client(base_url, api_key, label=label, dry_run=dry_run)


def _hub_client(cfg: Config, *, dry_run: bool = False) -> Client:
    return _client(cfg.hub.api_url, cfg.hub.api_key, label="hub", dry_run=dry_run)


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
        work_dir=work,
        cache_dir=inbox.default_cache_dir(),
        ra_key=os.environ.get(ra.KEY_ENV) or None,
        settings=settings,
    )
    return EXIT_RUNTIME if inbox.run_inbox(options, rep) else EXIT_OK


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
        out=print,
        err=_eprint,
    )


def _frontend(args: argparse.Namespace, cfg: Config, profiles: dict[str, Profile]) -> int:
    from warpzone import provision
    from warpzone.report import Reporter

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
        Reporter(print, warn_sink=_eprint),
        local=client,
        dry_run=args.dry_run,
    )
    return EXIT_OK


def _play_state(args: argparse.Namespace, cfg: Config, profiles: dict[str, Profile]) -> int:
    from warpzone import playstate_device

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


def _self_update(rep: Reporter, args: argparse.Namespace) -> int:
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


def _run(args: argparse.Namespace) -> int:
    if args.command == "settings":  # the apply path: no network code
        return _settings(args, *_load(args))
    if args.command == "play-state":  # ES-DE waits for it too: no network code either
        return _play_state(args, *_load(args))
    if args.command == "links":  # and so does this one
        return _links(args, *_load(args))
    if args.command == "saves":  # ES runs it around the emulator: no network code either
        return _saves(args, *_load(args))
    from warpzone import doctor, provision, prune
    from warpzone.report import Reporter

    rep = Reporter(print, warn_sink=_eprint)
    if args.command == "self-update":  # needs no config
        return _self_update(rep, args)
    if args.command == "health":  # a failed check is a result, not a usage error
        return _health(args, rep)
    if args.command == "doctor":  # offline, read-only, needs no valid config
        return doctor.run_doctor(doctor.Probes(config_path=_config_path(args)), rep.out)
    if args.command == "bios":  # works without a config when --root is given
        return _bios(args, rep)
    if args.command == "inbox":  # needs at most the [inbox] table
        return _inbox(args, rep)
    cfg, profiles = _load(args)

    if args.command == "check":
        rep.out(f"config ok: {len(cfg.devices)} device(s), profiles: {', '.join(profiles)}")
        if args.offline:
            return EXIT_OK
        problems = provision.check_hub(cfg, profiles, _hub_client(cfg), rep)
        return EXIT_RUNTIME if problems else EXIT_OK

    if args.command == "render-ignores":
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

    if args.command == "status":
        provision.folder_status(_hub_client(cfg), "hub", rep)
        try:
            local = localst.parse_config(localst.find_config())
        except localst.LocalSyncthingError:
            return EXIT_OK  # no Syncthing on this machine: hub status is enough
        client = _client(local.base_url, local.api_key, label="local")
        provision.folder_status(client, "this machine", rep)
        return EXIT_OK

    if args.command == "frontend":
        return _frontend(args, cfg, profiles)

    if args.command == "prune":
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
            max_gib=args.max_gib,
            force=args.force,
            list_all=args.list_all,
            notify_url=args.notify_url,
            rescan_timeout=args.timeout,
        )
        return EXIT_RUNTIME if problems else EXIT_OK

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
