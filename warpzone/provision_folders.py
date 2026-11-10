"""The Syncthing folder steps hub and device provisioning share.

Diff a folder spec against what Syncthing has, create or patch it, set its ignores and
wait for files to arrive.
"""

from __future__ import annotations

import os
import time
from collections.abc import Callable, Collection, Sequence
from typing import Any

from warpzone.errors import WarpzoneError
from warpzone.model import FolderSpec
from warpzone.report import Reporter
from warpzone.syncthing import Client, SyncthingError

PREFIX = "wz-"
NEED_KEYS = ("needFiles", "needDirectories", "needSymlinks", "needDeletes")


class ProvisionError(WarpzoneError):
    pass


def same_path(a: str, b: str) -> bool:
    return (a.rstrip("/") or "/") == (b.rstrip("/") or "/")


def is_move(spec: FolderSpec, current: dict[str, Any]) -> bool:
    """Whether Syncthing has the folder at a path the profile retired (pure)."""
    have = current.get("path", "")
    return not same_path(have, spec.path) and any(
        same_path(have, old) for old in spec.previous_paths
    )


def holds_data(path: str, marker: str = ".stfolder") -> bool:
    """Whether a directory holds anything besides Syncthing's marker and `.stignore`.

    A directory that does not exist holds nothing; one that cannot be read, or is not a
    directory, is taken to hold data."""
    try:
        names = os.listdir(path)
    except FileNotFoundError:
        return False
    except OSError:
        return True
    return any(name not in (marker, ".stignore") for name in names)


def fully_synced(status: dict[str, Any], completions: Sequence[dict[str, Any]]) -> bool:
    """Whether every file of the folder is on the other devices too (pure).

    `status` is `db/status` of the local folder, `completions` the `db/completion` of each
    device it is shared with. The local folder must be idle with nothing to pull and no local
    changes, and each remote must report 100% from a valid state."""
    need = status.get("needTotalItems")
    if need is None:
        need = sum(status.get(k, 0) for k in NEED_KEYS)
    return (
        status.get("state") == "idle"
        and need == 0
        and not status.get("receiveOnlyChangedItems")
        and bool(completions)
        and all(
            c.get("completion") == 100
            and not c.get("needItems")
            and c.get("remoteState", "valid") == "valid"
            for c in completions
        )
    )


def can_move(client: Client, spec: FolderSpec, current: dict[str, Any]) -> bool:
    """Whether the folder can be pointed at its new path without risk to any file.

    Changing a folder's path in place would look like a mass delete, so a move is done by
    removing the folder from Syncthing (the files stay on disk) and adding it again at the
    new path. That is safe when the retired path holds nothing, and when it holds files that
    are all on the hub already (a folder another console moved first, whose saves this one
    downloaded): the files then come back by themselves into the new directory. Anything
    else could be read as deletions once the files are moved, so it is left to the user.
    """
    if not is_move(spec, current):
        return False
    marker = current.get("markerName") or ".stfolder"
    if not holds_data(current.get("path", ""), marker):
        return True
    try:
        status = client.db_status(spec.id)
        completions = [client.completion(spec.id, device) for device in spec.devices]
    except SyncthingError:
        return False
    return fully_synced(status, completions)


def move_refusal(spec: FolderSpec, current: dict[str, Any]) -> str:
    """What to tell the user when a retired folder cannot be moved by itself."""
    return (
        f"{spec.id}: Syncthing has it at {current.get('path')!r} and the profile moved it to "
        f"{spec.path!r}, but the old folder holds files that are not all on the hub yet. "
        "Moving files out of a folder Syncthing still syncs would be sent as deletions: "
        "remove (or pause) the folder in Syncthing first (the files stay on disk), then move "
        f"the files into {spec.path} and run provision again"
    )


def _subset(wanted: Any, actual: Any) -> bool:
    """True if every key in `wanted` has the same value in `actual` (recursively)."""
    if isinstance(wanted, dict):
        return isinstance(actual, dict) and all(
            k in actual and _subset(v, actual[k]) for k, v in wanted.items()
        )
    return bool(wanted == actual)


def folder_patch(
    spec: FolderSpec, current: dict[str, Any], drop: Collection[str] = ()
) -> dict[str, Any]:
    """The owned keys that differ.

    Missing devices are added; those in `drop` are taken out; every other entry is kept as
    it is. Only the hub run passes `drop`.
    """
    body = spec.body()
    owned = ("label", "type", "fsWatcherEnabled", "rescanIntervalS", "ignorePerms")
    patch: dict[str, Any] = {
        key: body[key] for key in owned if key in body and current.get(key) != body[key]
    }
    if not _subset(spec.versioning, current.get("versioning") or {}):
        patch["versioning"] = spec.versioning
    have = list(current.get("devices") or [])
    kept = [d for d in have if d["deviceID"] not in drop]
    known = {d["deviceID"] for d in kept}
    missing = [d for d in body["devices"] if d["deviceID"] not in known]
    if missing or len(kept) != len(have):
        patch["devices"] = kept + missing
    return patch


def ensure_device(
    client: Client,
    device_id: str,
    name: str,
    addresses: tuple[str, ...],
    known: set[str],
    rep: Reporter,
) -> None:
    if device_id in known:
        rep.out(f"device {name}: already known")
        return
    client.add_device(
        {
            "deviceID": device_id,
            "name": name,
            "addresses": list(addresses),
            "autoAcceptFolders": False,
        }
    )
    known.add(device_id)
    rep.out(f"device {name}: added")


def upsert_folder(
    client: Client,
    spec: FolderSpec,
    current: dict[str, Any] | None,
    rep: Reporter,
    *,
    drop: Collection[str] = (),
) -> bool:
    """Create (paused) or patch the folder. Returns True if it was created.

    `drop` lists device IDs to take off an existing folder (see `folder_patch`)."""
    if current is None:
        client.add_folder({**spec.body(), "paused": True})
        rep.note(spec.id, f"created at {spec.path}")
        return True
    if not same_path(current.get("path", ""), spec.path):
        if is_move(spec, current):
            raise ProvisionError(move_refusal(spec, current))
        raise ProvisionError(
            f"{spec.id}: Syncthing has it at {current.get('path')!r} but the config says "
            f"{spec.path!r}. Changing a folder path would look like a mass delete, so "
            "warpzone refuses. Remove the folder in Syncthing first (files stay on disk), "
            "then run provision again."
        )
    patch = folder_patch(spec, current, drop)
    if patch:
        client.patch_folder(spec.id, patch)
        rep.note(spec.id, f"updated {', '.join(sorted(patch))}")
    return False


def unpause(client: Client, spec: FolderSpec, rep: Reporter) -> None:
    client.patch_folder(spec.id, {"paused": False})
    rep.note(spec.id, "resumed")


def rescan(client: Client, folder_id: str) -> str:
    """Full scan of a folder; says whether it ran or waits in Syncthing for a pull to end."""
    if client.scan(folder_id):
        return "rescanned"
    return "rescan queued (the folder is pulling; Syncthing scans it when the pull ends)"


def apply_ignores(
    client: Client,
    spec: FolderSpec,
    current: dict[str, Any] | None,
    created: bool,
    rep: Reporter,
    *,
    resume_paused: bool = False,
) -> None:
    """Set the final ignores directly (no bootstrap) and make sure the folder runs.

    A paused folder is resumed when its ignores had to change. With `resume_paused` (the
    hub run) it is resumed even when they were already right: that is what a creation that
    stopped after the ignores was set looks like. A device leaves such a folder alone."""
    wanted = list(spec.ignores)
    changed = created or client.get_ignores(spec.id) != wanted
    if changed:
        client.set_ignores(spec.id, wanted)
        rep.note(spec.id, "ignores set")
    paused = current is not None and bool(current.get("paused"))
    if created or (paused and (changed or resume_paused)):
        if paused and not changed:
            rep.note(spec.id, "was paused")
        unpause(client, spec, rep)


def in_sync(info: dict[str, Any] | None) -> bool:
    """True when the local copy of a file is exactly the global one."""
    if not info:
        return False
    local, global_ = info.get("local"), info.get("global")
    return bool(
        local
        and global_
        and not local.get("deleted")
        and not local.get("invalid")
        and local.get("version") == global_.get("version")
    )


def in_index(info: dict[str, Any] | None) -> bool:
    """True when this machine's index holds a live (not deleted, not invalid) copy."""
    local = (info or {}).get("local")
    return bool(local and not local.get("deleted") and not local.get("invalid"))


def wait_pending(
    client: Client,
    folder_id: str,
    files: tuple[str, ...],
    ready: Callable[[dict[str, Any] | None], bool],
    *,
    timeout: float,
    poll: float,
    rep: Reporter,
    sleep: Callable[[float], None],
    clock: Callable[[], float],
) -> list[str]:
    """Poll until every file is `ready`; returns the ones still pending at the deadline."""
    deadline = clock() + timeout
    pending = list(files)
    announced = False
    while True:
        pending = [f for f in pending if not ready(client.db_file(folder_id, f))]
        if not pending or clock() >= deadline:
            return pending
        if not announced:
            rep.out(f"{folder_id}: waiting for {', '.join(pending)} (up to {timeout:.0f}s)")
            announced = True
        sleep(poll)


def wait_for_files(
    client: Client,
    spec: FolderSpec,
    *,
    timeout: float,
    poll: float,
    rep: Reporter,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> None:
    pending = wait_pending(
        client,
        spec.id,
        spec.required_files,
        in_sync,
        timeout=timeout,
        poll=poll,
        rep=rep,
        sleep=sleep,
        clock=clock,
    )
    if pending:
        raise ProvisionError(
            f"{spec.id}: {', '.join(pending)} did not arrive within {timeout:.0f}s. "
            "The folder stays on the safe bootstrap ignores (only whitelists can arrive). "
            "Check that the hub is online and has these files, then run provision again."
        )


def unattended_refusals(
    spec: FolderSpec, current: dict[str, Any], *, moving: bool = False
) -> list[str]:
    """Why an unattended run must not touch a folder Syncthing already has (pure).

    `type` decides what a folder does with changes: `receiveonly` to `sendreceive` would send
    a console's deletions to the hub. `versioning` decides what Syncthing keeps. Neither is
    ever changed without a person, and neither is a failure a rollback could fix. A path
    differs only for a person too, except for a `moving` folder (`can_move`): its old
    directory is empty, so it is removed from Syncthing and added at the new path.
    """
    problems = []
    patch = folder_patch(spec, current)
    if "type" in patch:
        problems.append(
            f"{spec.id}: type is {current.get('type')!r}, the config says {spec.type!r}"
        )
    if "versioning" in patch:
        problems.append(f"{spec.id}: versioning differs from the config")
    if not moving and not same_path(current.get("path", ""), spec.path):
        problems.append(
            move_refusal(spec, current)
            if is_move(spec, current)
            else f"{spec.id}: Syncthing has it at {current.get('path')!r}, not {spec.path!r}"
        )
    return problems


def folder_status(client: Client, title: str, rep: Reporter) -> None:
    """One line per wz-* folder: state, how much is outstanding, local changes."""
    rep.out(f"{title}:")
    folders = [f for f in client.folders() if f["id"].startswith(PREFIX)]
    if not folders:
        rep.out("  no warpzone folders")
    for folder in sorted(folders, key=lambda f: f["id"]):
        if folder.get("paused"):
            rep.out(f"  {folder['id']}: paused")
            continue
        st = client.db_status(folder["id"])
        need = sum(st.get(k, 0) for k in ("needFiles", "needDirectories", "needDeletes"))
        parts = [str(st.get("state", "?")), f"{st.get('globalFiles', 0)} files", f"need {need}"]
        errors = st.get("errors", 0) + st.get("pullErrors", 0)
        if errors:
            parts.append(f"{errors} errors")
        if folder.get("type") == "receiveonly":
            changed = st.get("receiveOnlyTotalItems", 0)
            if changed:
                parts.append(f"{changed} local changes")
        if st.get("error"):
            parts.append(f"error: {st['error']}")
        rep.out(f"  {folder['id']}: {', '.join(parts)}")
