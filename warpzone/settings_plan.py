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

from collections.abc import Mapping
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
