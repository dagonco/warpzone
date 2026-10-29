"""Pure planning for the emulator settings layer: apply, capture and audit decisions.

Nothing here touches a disk or a network. `plan_apply` and `plan_data` decide what `settings
apply` writes, `candidates`, `preselect`, `decide` and `apply_decisions` decide what `settings
capture` proposes and what each answer changes, and `audit_rows` and `render_audit` describe the
state for `settings audit`. See "Emulator settings" in docs/design.md.

Values are `str | int | float | bool` and are compared by type and value (`1`, `1.0`, `true`
and `"1"` all differ). Secret keys never appear in any result: they are filtered by `tracked`
(and ignored when layered), so no value of one reaches a plan, a candidate, a snapshot or a row.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass

from warpzone.profiles import SettingsFile

Value = str | int | float | bool

# TEMPORARY, replace at integration: `match` and `is_builtin_secret` belong to
# warpzone/settings_formats.py (T2) and `same` to warpzone/settings_tree.py (T3). The fallbacks
# below follow their fixed interfaces so that this branch stands alone; once both modules are
# merged, delete the two `try` blocks' `except` bodies and keep only the imports.
try:
    from warpzone.settings_formats import is_builtin_secret, match
except ImportError:  # pragma: no cover - only until T2 is merged
    import re

    _SECRET_WORDS = ("token", "password", "passwd", "secret", "apikey", "api_key")

    def match(pattern: str, key: str) -> bool:
        parts = (".*" if c == "*" else "." if c == "?" else re.escape(c) for c in pattern)
        return re.fullmatch("".join(parts), key, re.DOTALL) is not None

    def is_builtin_secret(key: str) -> bool:
        last = key.rsplit("]", 1)[-1].rsplit(".", 1)[-1].lower()
        return any(word in last for word in _SECRET_WORDS)


try:
    from warpzone.settings_tree import same
except ImportError:  # pragma: no cover - only until T3 is merged

    def same(a: object, b: object) -> bool:
        return type(a) is type(b) and a == b


# Actions of a `KeyPlan`.
WRITE = "write"
HELD = "held"
OK = "ok"
CANNOT_WRITE = "cannot-write"

REASON_HELD = "changed here since the last sync, not captured yet"
REASON_OVERWRITE = "overwrites a change made here (--overwrite-local)"
REASON_FIXED = "not writable in the file"
REASON_NO_ADD = "not in the file, and the format cannot add keys"

_SCALARS = (str, int, float, bool)


@dataclass(frozen=True)
class KeyPlan:
    """What `apply` does with one layered key.

    `value` is the layered value for `write` and `held`, otherwise None. `reason` is empty for
    the plain cases and never holds a value.
    """

    key: str
    action: str  # "write" | "held" | "ok" | "cannot-write"
    value: Value | None = None
    reason: str = ""


def _is_secret(key: str, spec: SettingsFile | None) -> bool:
    if is_builtin_secret(key):
        return True
    return spec is not None and any(match(p, key) for p in spec.secret)


def _is_noise(key: str, spec: SettingsFile | None) -> bool:
    return spec is not None and any(match(p, key) for p in spec.noise)


def _is_tracked_key(key: str, spec: SettingsFile | None) -> bool:
    return not _is_secret(key, spec) and not _is_noise(key, spec)


def tracked(
    values: Mapping[str, object], fixed: frozenset[str], spec: SettingsFile | None
) -> dict[str, Value]:
    """The keys of a read file that warpzone follows: readable, writable, not noise, not secret
    and scalar. File order is kept."""
    return {
        key: value
        for key, value in values.items()
        if key not in fixed and isinstance(value, _SCALARS) and _is_tracked_key(key, spec)
    }


def plan_apply(
    current: Mapping[str, Value],
    fixed: frozenset[str],
    layered: Mapping[str, Value],
    snapshot: Mapping[str, Value] | None,
    *,
    can_add: bool,
    overwrite_local: bool,
    spec: SettingsFile | None = None,
) -> tuple[list[KeyPlan], dict[str, Value]]:
    """Decide every layered key of one file, and the snapshot to keep afterwards.

    `current` holds every value the handler read (noise included: a layered noise key is still
    applied), `layered` the effective layer values for this file, `snapshot` the values at the
    last sync (None when the file has no snapshot yet). With `spec`, noise and secret keys stay
    out of the snapshot and a layered secret key is ignored.

    For a layered key with layered value L, current value C and snapshot value S:

    - C == L: nothing to write, S := C.
    - C != L and (S absent or C == S): the console has not changed it since the last sync, so
      write L, S := L.
    - C != L and C != S: changed in the emulator and not captured yet: held, S kept; with
      `overwrite_local` it is written, S := L.
    - a fixed key, or a missing one where the format cannot add keys: cannot write.

    A missing key that the format can add is written whatever the snapshot says. A tracked key
    that is not layered gets S := C only where S is absent, so a change made in the interface
    stays visible to `capture` through any number of applies. Keys the file no longer has are
    dropped from the snapshot.
    """
    snap = snapshot or {}
    new: dict[str, Value] = {}
    plans: list[KeyPlan] = []
    for key in sorted(layered):
        if _is_secret(key, spec):
            continue
        layer_value = layered[key]
        follow = _is_tracked_key(key, spec)
        if key in fixed:
            plans.append(KeyPlan(key, CANNOT_WRITE, None, REASON_FIXED))
        elif key not in current:
            if can_add:
                plans.append(KeyPlan(key, WRITE, layer_value))
                if follow:
                    new[key] = layer_value
            else:
                plans.append(KeyPlan(key, CANNOT_WRITE, None, REASON_NO_ADD))
        elif same(current[key], layer_value):
            plans.append(KeyPlan(key, OK))
            if follow:
                new[key] = current[key]
        elif key not in snap or same(current[key], snap[key]):
            plans.append(KeyPlan(key, WRITE, layer_value))
            if follow:
                new[key] = layer_value
        elif overwrite_local:
            plans.append(KeyPlan(key, WRITE, layer_value, REASON_OVERWRITE))
            if follow:
                new[key] = layer_value
        else:
            plans.append(KeyPlan(key, HELD, layer_value, REASON_HELD))
            if follow:
                new[key] = snap[key]
    for key, value in tracked(current, fixed, spec).items():
        if key not in layered:
            new[key] = snap.get(key, value)
    return plans, new


def plan_data(
    current_hash: str | None,
    hub_hash: str | None,
    snapshot_hash: str | None,
    *,
    overwrite_local: bool,
) -> str:
    """The same rules as `plan_apply`, on a whole system data file by hash.

    Returns "none" (no hub copy), "ok", "write" or "held". A missing local file is "write": the
    caller decides whether it may be created (`create_when`).
    """
    if hub_hash is None:
        return "none"
    if current_hash == hub_hash:
        return OK
    if current_hash is None or snapshot_hash is None or current_hash == snapshot_hash:
        return WRITE
    return WRITE if overwrite_local else HELD


def data_snapshot(
    action: str, current_hash: str | None, hub_hash: str | None, snapshot_hash: str | None
) -> str | None:
    """The snapshot hash to keep after `plan_data` returned `action`: S := hub after a write,
    S := C when in sync, S kept when held, and S := C for a file with no hub copy only where S is
    absent."""
    if action == WRITE:
        return hub_hash
    if action == OK:
        return current_hash
    if action == HELD:
        return snapshot_hash
    return snapshot_hash if snapshot_hash is not None else current_hash


# ---------------------------------------------------------------------------------------------
# Capture
# ---------------------------------------------------------------------------------------------

# Entries of a layer, as in `settings_layers.Layer.entries`: (emulator, file instance id) -> key
# -> value. System data files use the file id "data" and the data id as the key.
Entries = Mapping[tuple[str, str], Mapping[str, Value]]

DATA_FILE = "data"
DECISIONS = ("common", "device", "discard", "skip")


@dataclass(frozen=True)
class Candidate:
    """One key (or one system data file) that `capture` proposes.

    `here` is the current value, `before` the snapshot value (None for a key new to the file),
    `common` and `device` what the two layers hold (None when absent). For a data file
    (`kind == "data"`, file `"data"`, key = the data id) `here`, `before` and `common` are sha256
    hex digests, `device` is always None and `size` is the file size in bytes.
    """

    emulator: str
    file: str
    key: str
    here: Value
    before: Value | None
    common: Value | None
    device: Value | None
    preselect: str  # "common" | "device"
    kind: str = "key"  # "key" | "data"
    size: int | None = None

    @property
    def ref(self) -> str:
        """What `--key` and the decision flags match: `<emulator>/<file>/<key>`."""
        return f"{self.emulator}/{self.file}/{self.key}"


def preselect(key: str, spec: SettingsFile, in_common: bool, in_device: bool) -> str:
    """The layer a changed key is pre-selected for: this device's layer when it has the key,
    common when common has it, the device layer when the key matches a `device` pattern, common
    otherwise."""
    if in_device:
        return "device"
    if in_common:
        return "common"
    if any(match(pattern, key) for pattern in spec.device):
        return "device"
    return "common"


def _scan(
    emulator: str,
    file_id: str,
    spec: SettingsFile,
    current: Mapping[str, Value],
    fixed: frozenset[str],
    snapshot: Mapping[str, Value] | None,
    common: Mapping[str, Value],
    device: Mapping[str, Value],
    seed: bool,
    key_patterns: Iterable[str],
    include_new: bool,
) -> tuple[list[Candidate], list[str]]:
    effective = {**common, **device}
    patterns = tuple(key_patterns)
    found: list[Candidate] = []
    fresh: list[str] = []
    for key, here in tracked(current, fixed, spec).items():
        differs = key not in effective or not same(here, effective[key])
        if not differs:
            continue
        in_snapshot = snapshot is not None and key in snapshot
        before = snapshot[key] if snapshot is not None and in_snapshot else None
        ref = f"{emulator}/{file_id}/{key}"
        seeded = seed and (
            any(match(p, key) for p in spec.known) or any(match(p, ref) for p in patterns)
        )
        changed = in_snapshot and not same(here, before)
        is_new = snapshot is not None and not in_snapshot
        if changed or seeded or (is_new and include_new):
            found.append(
                Candidate(
                    emulator,
                    file_id,
                    key,
                    here,
                    before,
                    common.get(key),
                    device.get(key),
                    preselect(key, spec, key in common, key in device),
                )
            )
        elif is_new:
            fresh.append(key)
    return found, fresh


def candidates(
    emulator: str,
    file_id: str,
    spec: SettingsFile,
    current: Mapping[str, Value],
    fixed: frozenset[str],
    snapshot: Mapping[str, Value] | None,
    common: Mapping[str, Value],
    device: Mapping[str, Value],
    *,
    seed: bool = False,
    key_patterns: Iterable[str] = (),
    include_new: bool = False,
) -> tuple[list[Candidate], int]:
    """The keys of one file that `capture` proposes, and how many new keys it only counts.

    Among tracked keys (see `tracked`), a key is proposed when it is in the snapshot, differs
    from it, and differs from the effective layered value (the device layer's, else common's).
    A key new to the file (not in the snapshot) is only counted, unless `include_new`; a new key
    equal to the layered value is not counted. With `seed`, every tracked key that matches the
    file's `known` patterns, or a `key_patterns` entry (matched over
    `<emulator>/<file_id>/<key>`), and differs from the effective layered value is also proposed,
    snapshot or not. `key_patterns` do nothing without `seed`. A file with no snapshot proposes
    nothing without `seed`. `common` and `device` are this file's keys in each layer; `device` is
    this device's layer. Candidates follow the file's order.
    """
    found, fresh = _scan(
        emulator,
        file_id,
        spec,
        current,
        fixed,
        snapshot,
        common,
        device,
        seed,
        key_patterns,
        include_new,
    )
    return found, len(fresh)


def new_keys(
    emulator: str,
    file_id: str,
    spec: SettingsFile,
    current: Mapping[str, Value],
    fixed: frozenset[str],
    snapshot: Mapping[str, Value] | None,
    common: Mapping[str, Value],
    device: Mapping[str, Value],
) -> list[str]:
    """The keys behind the count `candidates` returns without `include_new`."""
    return _scan(
        emulator, file_id, spec, current, fixed, snapshot, common, device, False, (), False
    )[1]


def data_candidate(
    emulator: str,
    data_id: str,
    current_hash: str | None,
    size: int | None,
    snapshot_hash: str | None,
    hub_hash: str | None,
    *,
    seed: bool = False,
    key_patterns: Iterable[str] = (),
) -> Candidate | None:
    """A system data file as a candidate: its hash differs from the snapshot and from the hub
    copy. Without a snapshot it is proposed only when `seed` and a pattern matches its ref
    (`<emulator>/data/<id>`). A data file goes to common only."""
    if current_hash is None or current_hash == hub_hash:
        return None
    candidate = Candidate(
        emulator,
        DATA_FILE,
        data_id,
        current_hash,
        snapshot_hash,
        hub_hash,
        None,
        "common",
        kind="data",
        size=size,
    )
    if snapshot_hash is not None and current_hash != snapshot_hash:
        return candidate
    if seed and any(match(p, candidate.ref) for p in key_patterns):
        return candidate
    return None


def decide(
    cands: Iterable[Candidate],
    *,
    to_common: Iterable[str] = (),
    to_device: Iterable[str] = (),
    discard: Iterable[str] = (),
    yes: bool = False,
) -> dict[str, str]:
    """Decide candidates without a terminal: ref -> "common" | "device" | "discard".

    The first flag that matches the ref wins, in the order to-common, to-device, discard; a
    candidate that no flag matches takes its pre-selection with `yes` and stays undecided
    (absent from the result, as if skipped) otherwise. A data file is never sent to the device
    layer: `to_device` does not apply to it.
    """
    to_common, to_device, discard = tuple(to_common), tuple(to_device), tuple(discard)
    decisions: dict[str, str] = {}
    for cand in cands:
        ref = cand.ref
        if any(match(p, ref) for p in to_common):
            decisions[ref] = "common"
        elif cand.kind == "key" and any(match(p, ref) for p in to_device):
            decisions[ref] = "device"
        elif any(match(p, ref) for p in discard):
            decisions[ref] = "discard"
        elif yes:
            decisions[ref] = cand.preselect
    return decisions


def _copy(entries: Entries) -> dict[tuple[str, str], dict[str, Value]]:
    return {table: dict(keys) for table, keys in entries.items()}


def apply_decisions(
    common: Entries,
    device: Entries,
    decisions: Mapping[str, str],
    cands: Iterable[Candidate],
    *,
    others: Mapping[str, Entries] | None = None,
) -> tuple[
    dict[tuple[str, str], dict[str, Value]],
    dict[tuple[str, str], dict[str, Value]],
    dict[tuple[str, str], dict[str, Value]],
    list[str],
]:
    """The effects of the decisions: (common', device', snapshot updates, notes).

    `common` and `device` are layer entries (this device's layer for `device`); they are not
    modified. `decisions` maps a ref to "common", "device", "discard" or "skip" (a ref that is
    absent is skipped too). The effects:

    - common: the key is set in common and removed from this device's layer, where it would keep
      winning. `others` (device name -> entries of that device's layer) gives the notes: one for
      every other device layer that holds the key with a different value.
    - device: the key is set in this device's layer.
    - discard: no layer changes.
    - skip: nothing.

    The snapshot updates, `{(emulator, file): {key: value}}`, set S := here for every key decided
    common, device or discard; for a data file the file is "data", the key is the data id and the
    value its hash (a data file chosen for common is copied to the hub by the caller). A table
    left empty is dropped from a layer.
    """
    common_out, device_out = _copy(common), _copy(device)
    updates: dict[tuple[str, str], dict[str, Value]] = {}
    notes: list[str] = []
    for cand in cands:
        decision = decisions.get(cand.ref, "skip")
        if decision not in DECISIONS:
            raise ValueError(f"unknown decision {decision!r} for {cand.ref}")
        if decision == "device" and cand.kind == "data":
            raise ValueError(f"a system data file cannot go to a device layer: {cand.ref}")
        if decision == "skip":
            continue
        table = (cand.emulator, cand.file)
        updates.setdefault(table, {})[cand.key] = cand.here
        if cand.kind == "data" or decision == "discard":
            continue
        if decision == "device":
            device_out.setdefault(table, {})[cand.key] = cand.here
            continue
        common_out.setdefault(table, {})[cand.key] = cand.here
        device_out.get(table, {}).pop(cand.key, None)
        if not device_out.get(table, True):
            del device_out[table]
        for name in sorted(others or {}):
            theirs = (others or {})[name].get(table, {})
            if cand.key in theirs and not same(theirs[cand.key], cand.here):
                notes.append(
                    f"the layer of {name} has its own value for {cand.ref}, "
                    "which wins over common on that device"
                )
    return common_out, device_out, updates, notes


def layer_diff(before: Entries, after: Entries) -> tuple[list[str], list[str]]:
    """What changed in one layer: (refs set or changed, refs removed), sorted. A ref is
    `<emulator>/<file>/<key>`."""
    set_refs: list[str] = []
    removed: list[str] = []
    for (emulator, file_id), keys in after.items():
        old = before.get((emulator, file_id), {})
        for key, value in keys.items():
            if key not in old or not same(old[key], value):
                set_refs.append(f"{emulator}/{file_id}/{key}")
    for (emulator, file_id), keys in before.items():
        new = after.get((emulator, file_id), {})
        removed.extend(f"{emulator}/{file_id}/{key}" for key in keys if key not in new)
    return sorted(set_refs), sorted(removed)


def capture_snapshot(
    snapshot: Mapping[str, Value] | None,
    current: Mapping[str, Value],
    fixed: frozenset[str],
    spec: SettingsFile,
    updates: Mapping[str, Value],
    *,
    proposed: Iterable[str] = (),
) -> dict[str, Value]:
    """The snapshot of one file after a capture: S := C for every decided key (`updates`, from
    `apply_decisions`) and for every new key counted; a proposed key that was skipped keeps its
    value (absent when new). A file with no snapshot gets one for all its tracked keys. Keys the
    file no longer tracks are dropped."""
    now = tracked(current, fixed, spec)
    if snapshot is None:
        new = dict(now)
    else:
        skipped = set(proposed)
        new = {key: snapshot[key] for key in now if key in snapshot}
        new.update(
            {key: value for key, value in now.items() if key not in snapshot and key not in skipped}
        )
    new.update({key: value for key, value in updates.items() if key in now})
    return new
