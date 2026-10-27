"""Command line: argparse, exit codes, output."""

from __future__ import annotations

import argparse
import os
import sys
import urllib.parse
from collections.abc import Sequence
from pathlib import Path

from warpzone import __version__, bios, ignores, inbox, localst, model, provision, prune, ra
from warpzone.bios import BiosError
from warpzone.config import (
    Config,
    ConfigError,
    default_config_path,
    load_config,
    load_inbox_settings,
)
from warpzone.inbox import InboxError
from warpzone.model import ModelError
from warpzone.profiles import Profile, ProfileError, available_profiles, load_profile
from warpzone.provision import ProvisionError, Reporter
from warpzone.prune import PruneError
from warpzone.syncthing import Client, SyncthingError

EXIT_OK, EXIT_RUNTIME, EXIT_USAGE = 0, 1, 2


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
    prov_sub.add_parser("hub", parents=[common, writing], help="configure the hub (remote API)")
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
        default=provision.DEFAULT_WAIT_SECONDS,
        help="seconds to wait for the whitelists to arrive (default: %(default)s)",
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
        default=prune.DEFAULT_MAX_GIB,
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
        default=prune.DEFAULT_RESCAN_TIMEOUT,
        help="seconds to wait for the rescan (default: %(default)s)",
    )

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
        "--work-dir",
        type=Path,
        help="local folder for unpacked archives and conversions (default: [inbox] work_dir, "
        "else the system temp folder)",
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


def _hub_client(cfg: Config, *, dry_run: bool = False) -> Client:
    return Client(cfg.hub.api_url, cfg.hub.api_key, label="hub", dry_run=dry_run)


def _config_path(args: argparse.Namespace) -> Path:
    return args.config or Path(os.environ.get("WARPZONE_CONFIG") or default_config_path())


def _bios(args: argparse.Namespace, rep: Reporter) -> int:
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
    options = inbox.Options(
        root=root,
        dats_dir=dats,
        tools=inbox.find_tools(settings),
        apply=args.apply,
        work_dir=work,
        cache_dir=inbox.default_cache_dir(),
        ra_key=os.environ.get(ra.KEY_ENV) or None,
    )
    return EXIT_RUNTIME if inbox.run_inbox(options, rep) else EXIT_OK


def _run(args: argparse.Namespace) -> int:
    rep = Reporter(print, warn_sink=_eprint)
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
        for spec in model.device_specs(cfg, name, profiles[cfg.devices[name].profile]):
            rep.out(f"# {spec.id}")
            if spec.bootstrap_ignores is not None:
                rep.out("# bootstrap, replaced once the whitelist has arrived:")
                rep.out("\n".join(f"#   {line}" for line in spec.bootstrap_ignores))
            rep.out("\n".join(spec.ignores))
        rep.out(f"# hub .stignore for whitelisted folders: {ignores.hub_whitelist()[0]}")
        return EXIT_OK

    if args.command == "status":
        provision.folder_status(_hub_client(cfg), "hub", rep)
        try:
            local = localst.parse_config(localst.find_config())
        except localst.LocalSyncthingError:
            return EXIT_OK  # no Syncthing on this machine: hub status is enough
        client = Client(local.base_url, local.api_key, label="local")
        provision.folder_status(client, "this machine", rep)
        return EXIT_OK

    if args.command == "prune":
        local = localst.parse_config(localst.find_config(args.syncthing_config))
        client = Client(local.base_url, local.api_key, label="local")
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

    if args.target == "hub":
        provision.provision_hub(cfg, profiles, _hub_client(cfg, dry_run=args.dry_run), rep)
        return EXIT_OK

    name = _device(cfg, args.name)
    local = localst.parse_config(localst.find_config(args.syncthing_config))
    client = Client(local.base_url, local.api_key, label="local", dry_run=args.dry_run)
    provision.provision_device(
        cfg,
        name,
        profiles[cfg.devices[name].profile],
        client,
        _hub_client(cfg),
        rep,
        home=Path.home(),
        wait_timeout=args.timeout,
    )
    return EXIT_OK


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        return _run(args)
    except (ConfigError, ProfileError, ModelError) as exc:
        _eprint(f"warpzone: config error: {exc}")
        return EXIT_USAGE
    except (BiosError, InboxError) as exc:
        _eprint(f"warpzone: error: {exc}")
        return EXIT_USAGE
    except (SyncthingError, ProvisionError, PruneError, localst.LocalSyncthingError) as exc:
        _eprint(f"warpzone: error: {exc}")
        return EXIT_RUNTIME
    except KeyboardInterrupt:
        _eprint("warpzone: interrupted")
        return 130
