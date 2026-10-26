"""Command line: argparse, exit codes, output."""

from __future__ import annotations

import argparse
import os
import sys
from collections.abc import Sequence
from pathlib import Path

from warpzone import __version__, ignores, localst, model, provision
from warpzone.config import Config, ConfigError, default_config_path, load_config
from warpzone.model import ModelError
from warpzone.profiles import Profile, ProfileError, available_profiles, load_profile
from warpzone.provision import ProvisionError, Reporter
from warpzone.syncthing import Client, SyncthingError

EXIT_OK, EXIT_RUNTIME, EXIT_USAGE = 0, 1, 2


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

    sub.add_parser("status", parents=[common], help="folder state on the hub and this machine")
    render = sub.add_parser(
        "render-ignores", parents=[common], help="print the ignore lines a device would get"
    )
    render.add_argument("name", help="device name from the config")
    return parser


def _load(args: argparse.Namespace) -> tuple[Config, dict[str, Profile]]:
    path = args.config or Path(os.environ.get("WARPZONE_CONFIG") or default_config_path())
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


def _eprint(message: str) -> None:
    print(message, file=sys.stderr)


def _hub_client(cfg: Config, *, dry_run: bool = False) -> Client:
    return Client(cfg.hub.api_url, cfg.hub.api_key, label="hub", dry_run=dry_run)


def _run(args: argparse.Namespace) -> int:
    rep = Reporter(print, warn_sink=_eprint)
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
    except (SyncthingError, ProvisionError, localst.LocalSyncthingError) as exc:
        _eprint(f"warpzone: error: {exc}")
        return EXIT_RUNTIME
    except KeyboardInterrupt:
        _eprint("warpzone: interrupted")
        return 130
