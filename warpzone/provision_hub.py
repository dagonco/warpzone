"""Hub provisioning: the hub's devices and folders, and the whitelist check."""

from __future__ import annotations

import time
from collections.abc import Callable

from warpzone.config import HUB_WAIT_SECONDS, Config
from warpzone.model import FolderSpec, hub_specs
from warpzone.profiles import Profile
from warpzone.provision_folders import (
    ProvisionError,
    apply_ignores,
    ensure_device,
    in_index,
    unpause,
    upsert_folder,
    wait_pending,
)
from warpzone.report import Reporter
from warpzone.syncthing import Client, SyncthingError


def _bootstrap_hub_folder(
    client: Client,
    spec: FolderSpec,
    *,
    timeout: float,
    poll: float,
    rep: Reporter,
    sleep: Callable[[float], None],
    clock: Callable[[], float],
) -> bool:
    """A newly created whitelisted hub folder: let only the whitelists through first.

    The first full scan of a big library can take hours, and devices wait for their
    `patterns-<device>.stignore` to show up in the hub's index. With every other file
    ignored the scan reaches the whitelists at once; once they are indexed the final
    ignores go in and the full scan starts. If they never show up the final ignores
    are set anyway, so the folder is never left half-configured. Returns True when the
    whitelists were indexed (or, in dry-run, would be waited for).
    """
    assert spec.bootstrap_ignores is not None
    client.set_ignores(spec.id, list(spec.bootstrap_ignores))
    rep.note(spec.id, "bootstrap ignores set")
    unpause(client, spec, rep)
    client.scan(spec.id)
    if client.dry_run:
        rep.note(spec.id, f"dry-run: would wait up to {timeout:.0f}s for the whitelists")
        pending: list[str] = []
    else:
        pending = wait_pending(
            client,
            spec.id,
            spec.required_files,
            in_index,
            timeout=timeout,
            poll=poll,
            rep=rep,
            sleep=sleep,
            clock=clock,
        )
    client.set_ignores(spec.id, list(spec.ignores))
    client.scan(spec.id)
    rep.note(spec.id, "final ignores set, full scan started")
    if pending:
        rep.warn(
            f"{spec.id}: {', '.join(pending)} not in the hub's index after {timeout:.0f}s; "
            f"the final ignores are set anyway. Add them under {spec.path}, then run "
            "`warpzone check`"
        )
    return not pending


def _missing_on(client: Client, spec: FolderSpec) -> list[str]:
    names = set(client.browse(spec.id))
    return [f for f in spec.required_files if f not in names]


def provision_hub(
    cfg: Config,
    profiles: dict[str, Profile],
    client: Client,
    rep: Reporter,
    *,
    wait_timeout: float = HUB_WAIT_SECONDS,
    poll: float = 2.0,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> None:
    actual = client.my_id()
    if actual != cfg.hub.device_id:
        raise ProvisionError(
            f"the API at {cfg.hub.api_url} belongs to device {actual[:7]}..., "
            f"but hub.device_id starts with {cfg.hub.device_id[:7]}..."
        )
    known = {d["deviceID"] for d in client.devices()}
    for device in cfg.devices.values():
        ensure_device(client, device.device_id, device.name, device.addresses, known, rep)

    existing = {f["id"]: f for f in client.folders()}
    for spec in hub_specs(cfg, profiles):
        current = existing.get(spec.id)
        created = upsert_folder(client, spec, current, rep)
        bootstrapped = False
        if created and spec.bootstrap_ignores is not None:
            bootstrapped = True
            _bootstrap_hub_folder(
                client, spec, timeout=wait_timeout, poll=poll, rep=rep, sleep=sleep, clock=clock
            )
        else:
            apply_ignores(client, spec, current, created, rep)
        rep.flush(spec.id)
        if not spec.required_files or bootstrapped:
            if bootstrapped and client.dry_run:
                rep.warn(
                    f"{spec.id}: add {', '.join(spec.required_files)} under {spec.path}, "
                    "then run `warpzone check`"
                )
            continue
        if created:
            rep.warn(
                f"{spec.id}: add {', '.join(spec.required_files)} under {spec.path}, "
                "then run `warpzone check`"
            )
        elif not client.dry_run:
            missing = _missing_on(client, spec)
            if missing:
                rep.warn(f"{spec.id}: missing in {spec.path}: {', '.join(missing)}")


def check_hub(cfg: Config, profiles: dict[str, Profile], client: Client, rep: Reporter) -> int:
    """Report whitelist files missing on the hub. Returns the number of problems."""
    problems = 0
    for spec in hub_specs(cfg, profiles):
        if not spec.required_files:
            continue
        try:
            missing = _missing_on(client, spec)
        except SyncthingError as exc:
            if exc.status is None:
                raise
            rep.out(f"{spec.id}: not configured on the hub yet (run `warpzone provision hub`)")
            problems += 1
            continue
        if missing:
            problems += len(missing)
            rep.out(f"{spec.id}: MISSING {', '.join(missing)} (in {spec.path})")
        else:
            rep.out(f"{spec.id}: whitelists present")
    return problems
