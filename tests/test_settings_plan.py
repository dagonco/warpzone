"""Pure planning for the emulator settings layer: apply, capture and audit decisions."""

import unittest

from warpzone.profiles import SettingsFile
from warpzone.settings_plan import (
    KeyPlan,
    plan_apply,
    plan_data,
    tracked,
)

SPEC = SettingsFile(
    id="cfg",
    path="~/x.ini",
    format="ini",
    noise=(
        "[Controls]*",
        "[UI]Recent?",
    ),
    secret=("[Web]*_user",),
    device=("[Renderer]resolution*",),
    known=("[System]language*",),
)

ABSENT = object()


def _plans(plans: list[KeyPlan]) -> dict[str, KeyPlan]:
    return {p.key: p for p in plans}


class TrackedTests(unittest.TestCase):
    def test_tracked_drops_fixed_noise_secret_and_non_scalars(self) -> None:
        values = {
            "[System]language": "1",
            "[Controls]a": "x",
            "[UI]Recent1": "x",
            "[UI]Recent12": "kept: ? is one character",
            "[Web]name_user": "someone",
            "[Web]eden_token": "tok",  # gitleaks:allow
            "[Folders]SaveStates": "dup",
            "[X]list": [1, 2],
            "[X]none": None,
            "[X]flag": True,
        }
        got = tracked(values, frozenset({"[Folders]SaveStates"}), SPEC)
        self.assertEqual(
            got,
            {"[System]language": "1", "[UI]Recent12": "kept: ? is one character", "[X]flag": True},
        )

    def test_tracked_keeps_file_order(self) -> None:
        values = {"b": "1", "a": "2", "c": "3"}
        self.assertEqual(list(tracked(values, frozenset(), SPEC)), ["b", "a", "c"])


class PlanApplyTableTests(unittest.TestCase):
    """Every combination of current value C, layered value L and snapshot value S."""

    def run_case(self, c, s, *, overwrite: bool = False, can_add: bool = True, fixed=()):
        current = {} if c is ABSENT else {"k": c}
        snapshot = None if s is ABSENT else {"k": s}
        plans, new = plan_apply(
            current,
            frozenset(fixed),
            {"k": 2},
            snapshot,
            can_add=can_add,
            overwrite_local=overwrite,
        )
        self.assertEqual(len(plans), 1)
        return plans[0], new

    def test_c_equals_l_is_ok_and_snapshot_follows_c(self) -> None:
        for s in (ABSENT, 1, 2, 9):
            with self.subTest(s=s):
                plan, new = self.run_case(2, s)
                self.assertEqual((plan.action, plan.value), ("ok", None))
                self.assertEqual(new, {"k": 2})

    def test_unchanged_since_last_sync_writes_l(self) -> None:
        for s in (ABSENT, 1):
            with self.subTest(s=s):
                plan, new = self.run_case(1, s)
                self.assertEqual((plan.action, plan.value), ("write", 2))
                self.assertEqual(new, {"k": 2})

    def test_changed_in_the_emulator_is_held_and_snapshot_kept(self) -> None:
        plan, new = self.run_case(3, 1)
        self.assertEqual((plan.action, plan.value), ("held", 2))
        self.assertTrue(plan.reason)
        self.assertEqual(new, {"k": 1})

    def test_overwrite_local_writes_a_held_key(self) -> None:
        plan, new = self.run_case(3, 1, overwrite=True)
        self.assertEqual((plan.action, plan.value), ("write", 2))
        self.assertIn("overwrite", plan.reason)
        self.assertEqual(new, {"k": 2})

    def test_overwrite_local_changes_nothing_when_not_held(self) -> None:
        self.assertEqual(self.run_case(2, 1, overwrite=True)[0].action, "ok")
        self.assertEqual(self.run_case(1, 1, overwrite=True)[0].reason, "")

    def test_first_run_without_snapshot_writes_every_differing_key(self) -> None:
        current = {"a": "1", "b": "2", "c": "3"}
        plans, new = plan_apply(
            current,
            frozenset(),
            {"a": "9", "b": "2"},
            None,
            can_add=True,
            overwrite_local=False,
        )
        by_key = _plans(plans)
        self.assertEqual(by_key["a"].action, "write")
        self.assertEqual(by_key["b"].action, "ok")
        self.assertEqual(new, {"a": "9", "b": "2", "c": "3"})

    def test_missing_key_is_added_when_the_format_can_add(self) -> None:
        plan, new = self.run_case(ABSENT, ABSENT)
        self.assertEqual((plan.action, plan.value), ("write", 2))
        self.assertEqual(new, {"k": 2})

    def test_missing_key_is_added_even_if_the_snapshot_remembers_it(self) -> None:
        plan, new = self.run_case(ABSENT, 1)
        self.assertEqual(plan.action, "write")
        self.assertEqual(new, {"k": 2})

    def test_missing_key_cannot_be_written_when_the_format_cannot_add(self) -> None:
        plan, new = self.run_case(ABSENT, 1, can_add=False)
        self.assertEqual((plan.action, plan.value), ("cannot-write", None))
        self.assertTrue(plan.reason)
        self.assertEqual(new, {})

    def test_fixed_key_cannot_be_written(self) -> None:
        for c in (2, 1, ABSENT):
            with self.subTest(c=c):
                plan, new = self.run_case(c, 1, fixed=("k",))
                self.assertEqual(plan.action, "cannot-write")
                self.assertTrue(plan.reason)
                self.assertEqual(new, {})

    def test_types_are_compared_exactly(self) -> None:
        # "2", 2.0 and True are not 2: each differs from L, and from the snapshot 7.
        for c in ("2", 2.0, True):
            with self.subTest(c=c):
                plan, _ = self.run_case(c, 7)
                self.assertEqual(plan.action, "held")
        plans, _ = plan_apply(
            {"k": True}, frozenset(), {"k": 1}, {"k": True}, can_add=True, overwrite_local=False
        )
        self.assertEqual(plans[0].action, "write")


class PlanApplySnapshotTests(unittest.TestCase):
    def apply(self, current, layered, snapshot, **kw):
        kw.setdefault("can_add", True)
        kw.setdefault("overwrite_local", False)
        return plan_apply(current, frozenset(), layered, snapshot, spec=SPEC, **kw)

    def test_unlayered_key_gets_the_snapshot_only_where_absent(self) -> None:
        plans, new = self.apply({"a": "now", "b": "now"}, {}, {"a": "before"})
        self.assertEqual(plans, [])
        self.assertEqual(new, {"a": "before", "b": "now"})

    def test_a_change_made_in_the_interface_survives_any_number_of_applies(self) -> None:
        snap = {"a": "before"}
        for _ in range(3):
            _, snap = self.apply({"a": "changed"}, {}, snap)
        self.assertEqual(snap, {"a": "before"})

    def test_noise_and_secret_keys_never_enter_the_snapshot(self) -> None:
        current = {
            "[Controls]a": "1",
            "[Web]n_user": "u",
            "cheevos_token": "t",
            "[S]k": "v",
        }  # gitleaks:allow
        _, new = self.apply(current, {}, None)
        self.assertEqual(new, {"[S]k": "v"})

    def test_layered_noise_key_is_applied_without_a_snapshot(self) -> None:
        plans, new = self.apply({"[Controls]a": "1"}, {"[Controls]a": "2"}, None)
        self.assertEqual((plans[0].action, plans[0].value), ("write", "2"))
        self.assertEqual(new, {})

    def test_layered_secret_key_is_ignored(self) -> None:
        plans, new = self.apply({"[Web]n_user": "u"}, {"[Web]n_user": "x"}, None)
        self.assertEqual(plans, [])
        self.assertEqual(new, {})

    def test_snapshot_drops_keys_the_file_no_longer_has(self) -> None:
        _, new = self.apply({"a": "1"}, {}, {"a": "1", "gone": "x"})
        self.assertEqual(new, {"a": "1"})

    def test_held_then_discarded_writes_the_layer_value_next_time(self) -> None:
        layered = {"k": "hub"}
        plans, snap = self.apply({"k": "here"}, layered, {"k": "old"})
        self.assertEqual(plans[0].action, "held")
        # capture --discard sets S := C; the next apply writes L.
        plans, snap = self.apply({"k": "here"}, layered, {"k": "here"})
        self.assertEqual((plans[0].action, plans[0].value), ("write", "hub"))
        self.assertEqual(snap, {"k": "hub"})

    def test_second_run_after_a_write_is_idempotent(self) -> None:
        layered = {"k": "hub", "m": "x"}
        _, snap = self.apply({"k": "old", "m": "old"}, layered, None)
        plans, again = self.apply({"k": "hub", "m": "x"}, layered, snap)
        self.assertEqual({p.action for p in plans}, {"ok"})
        self.assertEqual(again, snap)

    def test_plans_are_sorted_by_key(self) -> None:
        plans, _ = self.apply({}, {"b": "1", "a": "1", "c": "1"}, None)
        self.assertEqual([p.key for p in plans], ["a", "b", "c"])


class PlanDataTests(unittest.TestCase):
    def plan(self, current, hub, snapshot, overwrite=False):
        return plan_data(current, hub, snapshot, overwrite_local=overwrite)

    def test_nothing_on_the_hub_means_nothing_to_do(self) -> None:
        self.assertEqual(self.plan("a", None, "a"), "none")
        self.assertEqual(self.plan(None, None, None), "none")

    def test_same_hash_is_ok(self) -> None:
        for snap in (None, "a", "z"):
            with self.subTest(snap=snap):
                self.assertEqual(self.plan("h", "h", snap), "ok")

    def test_unchanged_here_is_written(self) -> None:
        self.assertEqual(self.plan("a", "h", "a"), "write")
        self.assertEqual(self.plan("a", "h", None), "write")

    def test_changed_here_is_held_unless_overwrite_local(self) -> None:
        self.assertEqual(self.plan("c", "h", "a"), "held")
        self.assertEqual(self.plan("c", "h", "a", overwrite=True), "write")

    def test_missing_local_file_is_written(self) -> None:
        for snap in (None, "a"):
            self.assertEqual(self.plan(None, "h", snap), "write")


if __name__ == "__main__":
    unittest.main()
