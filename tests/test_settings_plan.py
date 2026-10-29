"""Pure planning for the emulator settings layer: apply, capture and audit decisions."""

import unittest

from warpzone.profiles import SettingsFile
from warpzone.settings_plan import (
    Candidate,
    KeyPlan,
    apply_decisions,
    candidates,
    capture_snapshot,
    data_candidate,
    data_snapshot,
    decide,
    layer_diff,
    new_keys,
    plan_apply,
    plan_data,
    preselect,
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


class DataSnapshotTests(unittest.TestCase):
    def test_snapshot_hash_after_each_action(self) -> None:
        self.assertEqual(data_snapshot("write", "c", "h", "s"), "h")
        self.assertEqual(data_snapshot("ok", "h", "h", "s"), "h")
        self.assertEqual(data_snapshot("held", "c", "h", "s"), "s")

    def test_no_hub_copy_sets_the_snapshot_only_where_absent(self) -> None:
        self.assertEqual(data_snapshot("none", "c", None, None), "c")
        self.assertEqual(data_snapshot("none", "c", None, "s"), "s")
        self.assertIsNone(data_snapshot("none", None, None, None))


def _cands(**kw):
    """Candidates of one file with the shared fixtures; keyword arguments override them."""
    args = {
        "current": {},
        "fixed": frozenset(),
        "snapshot": {},
        "common": {},
        "device": {},
    }
    args.update(kw)
    flags = {k: args.pop(k) for k in ("seed", "key_patterns", "include_new") if k in args}
    return candidates(
        "eden",
        "qt-config",
        SPEC,
        args["current"],
        args["fixed"],
        args["snapshot"],
        args["common"],
        args["device"],
        **flags,
    )


class CandidatesTests(unittest.TestCase):
    def test_changed_key_is_proposed_with_both_layer_values(self) -> None:
        got, new = _cands(
            current={"[System]language": "4", "[Renderer]resolution_setup": "3"},
            snapshot={"[System]language": "1", "[Renderer]resolution_setup": "2"},
            common={"[System]language": "1"},
            device={"[Renderer]resolution_setup": "2"},
        )
        self.assertEqual(new, 0)
        self.assertEqual(
            got,
            [
                Candidate("eden", "qt-config", "[System]language", "4", "1", "1", None, "common"),
                Candidate(
                    "eden", "qt-config", "[Renderer]resolution_setup", "3", "2", None, "2", "device"
                ),
            ],
        )
        self.assertEqual(got[0].ref, "eden/qt-config/[System]language")

    def test_key_equal_to_the_effective_layered_value_is_not_proposed(self) -> None:
        # Changed since the snapshot, but already what the hub says.
        got, _ = _cands(current={"k": "2"}, snapshot={"k": "1"}, common={"k": "2"})
        self.assertEqual(got, [])
        # The device layer wins over common when working out the effective value.
        got, _ = _cands(
            current={"k": "2"}, snapshot={"k": "1"}, common={"k": "9"}, device={"k": "2"}
        )
        self.assertEqual(got, [])
        got, _ = _cands(
            current={"k": "2"}, snapshot={"k": "1"}, common={"k": "2"}, device={"k": "9"}
        )
        self.assertEqual([c.key for c in got], ["k"])

    def test_held_key_is_proposed(self) -> None:
        got, _ = _cands(current={"k": "3"}, snapshot={"k": "1"}, common={"k": "2"})
        self.assertEqual((got[0].here, got[0].before, got[0].common), ("3", "1", "2"))

    def test_unchanged_key_is_not_proposed_even_when_the_hub_differs(self) -> None:
        # C == S and C != L: that is pending for apply, not a local change.
        got, _ = _cands(current={"k": "1"}, snapshot={"k": "1"}, common={"k": "2"})
        self.assertEqual(got, [])

    def test_typed_change_is_a_change(self) -> None:
        got, _ = _cands(current={"k": 1.0}, snapshot={"k": 1})
        self.assertEqual([c.key for c in got], ["k"])

    def test_new_keys_are_only_counted(self) -> None:
        kw = {"current": {"a": "1", "new1": "x", "new2": "y"}, "snapshot": {"a": "1"}}
        got, new = _cands(**kw)
        self.assertEqual((got, new), ([], 2))
        self.assertEqual(
            new_keys("eden", "qt-config", SPEC, kw["current"], frozenset(), kw["snapshot"], {}, {}),
            ["new1", "new2"],
        )

    def test_new_key_equal_to_the_layer_is_not_counted(self) -> None:
        got, new = _cands(current={"n": "1"}, snapshot={"a": "1"}, common={"n": "1"})
        self.assertEqual((got, new), ([], 0))

    def test_include_new_proposes_new_keys_without_a_before(self) -> None:
        got, new = _cands(current={"n": "1"}, snapshot={"a": "1"}, include_new=True)
        self.assertEqual(new, 0)
        self.assertEqual([(c.key, c.before) for c in got], [("n", None)])

    def test_no_baseline_means_no_candidates_without_seed(self) -> None:
        got, new = _cands(current={"[System]language": "4", "k": "1"}, snapshot=None)
        self.assertEqual((got, new), ([], 0))

    def test_seed_proposes_known_keys_that_differ_from_the_layers(self) -> None:
        current = {"[System]language": "4", "[System]languageB": "7", "[System]other": "x"}
        got, new = _cands(
            current=current,
            snapshot=None,
            common={"[System]languageB": "7"},
            seed=True,
        )
        # `known` is [System]language*; languageB already equals the layer; other is not known.
        self.assertEqual([c.key for c in got], ["[System]language"])
        self.assertEqual((got[0].before, new), (None, 0))

    def test_seed_proposes_known_keys_even_when_unchanged_since_the_snapshot(self) -> None:
        got, _ = _cands(
            current={"[System]language": "4"},
            snapshot={"[System]language": "4"},
            seed=True,
        )
        self.assertEqual([c.key for c in got], ["[System]language"])

    def test_seed_with_key_patterns_matches_emulator_file_key(self) -> None:
        current = {"[System]other": "x", "[Audio]volume": "5", "[Misc]z": "1"}
        got, _ = _cands(
            current=current,
            snapshot=None,
            seed=True,
            key_patterns=("eden/qt-config/[Audio]*", "ryujinx/*"),
        )
        self.assertEqual([c.key for c in got], ["[Audio]volume"])

    def test_key_patterns_do_nothing_without_seed(self) -> None:
        got, _ = _cands(
            current={"[Audio]volume": "5"},
            snapshot={"[Audio]volume": "5"},
            key_patterns=("eden/*",),
        )
        self.assertEqual(got, [])

    def test_seed_key_not_in_the_snapshot_is_shown_and_not_counted_as_new(self) -> None:
        got, new = _cands(current={"[System]language": "4"}, snapshot={"a": "1"}, seed=True)
        self.assertEqual(([c.key for c in got], new), (["[System]language"], 0))

    def test_secret_noise_and_fixed_keys_are_never_candidates(self) -> None:
        current = {
            "[Controls]pad": "1",
            "[Web]n_user": "someone",
            "[Web]eden_token": "tok",  # gitleaks:allow
            "cheevos_password": "pw",  # gitleaks:allow
            "[Folders]SaveStates": "dup",
        }
        snapshot = dict.fromkeys(current, "old")
        for kw in ({}, {"seed": True, "key_patterns": ("*",)}, {"include_new": True}):
            with self.subTest(**kw):
                got, new = _cands(
                    current=current,
                    fixed=frozenset({"[Folders]SaveStates"}),
                    snapshot=snapshot,
                    **kw,
                )
                self.assertEqual((got, new), ([], 0))
        self.assertEqual(
            new_keys("e", "f", SPEC, current, frozenset({"[Folders]SaveStates"}), {}, {}, {}), []
        )

    def test_candidates_follow_file_order(self) -> None:
        got, _ = _cands(current={"b": "2", "a": "2"}, snapshot={"a": "1", "b": "1"})
        self.assertEqual([c.key for c in got], ["b", "a"])


class DataCandidateTests(unittest.TestCase):
    def cand(self, current, snapshot, hub, **kw):
        return data_candidate("xemu", "eeprom", current, 256, snapshot, hub, **kw)

    def test_changed_data_file_is_proposed_to_common_only(self) -> None:
        got = self.cand("c" * 64, "s" * 64, "h" * 64)
        self.assertEqual(
            (got.ref, got.kind, got.size, got.here, got.before, got.common, got.preselect),
            ("xemu/data/eeprom", "data", 256, "c" * 64, "s" * 64, "h" * 64, "common"),
        )

    def test_unchanged_equal_to_hub_or_without_baseline_is_not_proposed(self) -> None:
        self.assertIsNone(self.cand("a", "a", "h"))
        self.assertIsNone(self.cand("h", "s", "h"))
        self.assertIsNone(self.cand("c", None, "h"))
        self.assertIsNone(self.cand(None, "s", "h"))

    def test_data_file_with_no_hub_copy_is_proposed_when_changed(self) -> None:
        self.assertIsNotNone(self.cand("c", "s", None))

    def test_seed_proposes_a_matching_data_file_without_a_baseline(self) -> None:
        self.assertIsNone(self.cand("c", None, "h", seed=True))
        got = self.cand("c", None, "h", seed=True, key_patterns=("xemu/data/*",))
        self.assertEqual(got.before, None)
        self.assertIsNone(self.cand("h", None, "h", seed=True, key_patterns=("*",)))


class PreselectTests(unittest.TestCase):
    def test_this_devices_layer_wins(self) -> None:
        self.assertEqual(preselect("k", SPEC, True, True), "device")
        self.assertEqual(preselect("k", SPEC, False, True), "device")

    def test_common_when_common_has_the_key(self) -> None:
        self.assertEqual(preselect("[Renderer]resolution_setup", SPEC, True, False), "common")

    def test_device_when_the_key_matches_a_device_pattern(self) -> None:
        self.assertEqual(preselect("[Renderer]resolution_setup", SPEC, False, False), "device")

    def test_common_otherwise(self) -> None:
        self.assertEqual(preselect("[System]language", SPEC, False, False), "common")


class DecideTests(unittest.TestCase):
    def setUp(self) -> None:
        self.a = Candidate("eden", "qt-config", "[System]language", "4", "1", None, None, "common")
        self.b = Candidate("eden", "qt-config", "[Renderer]scale", "2", "1", None, None, "device")
        self.d = Candidate(
            "xemu", "data", "eeprom", "c", "s", None, None, "common", kind="data", size=3
        )
        self.all = [self.a, self.b, self.d]

    def test_without_flags_nothing_is_decided(self) -> None:
        self.assertEqual(decide(self.all), {})

    def test_patterns_match_the_emulator_file_key_ref(self) -> None:
        got = decide(self.all, to_common=["eden/qt-config/[System]*"], discard=["xemu/*"])
        self.assertEqual(got, {self.a.ref: "common", self.d.ref: "discard"})

    def test_flag_precedence_is_common_then_device_then_discard(self) -> None:
        got = decide(self.all, to_common=["eden/*"], to_device=["eden/*"], discard=["eden/*"])
        self.assertEqual(got, {self.a.ref: "common", self.b.ref: "common"})
        got = decide(self.all, to_device=["eden/*"], discard=["eden/*"])
        self.assertEqual(got, {self.a.ref: "device", self.b.ref: "device"})
        self.assertEqual(set(decide(self.all, discard=["eden/*"]).values()), {"discard"})

    def test_yes_takes_the_preselection_for_what_no_flag_decided(self) -> None:
        got = decide(self.all, discard=["eden/qt-config/[System]*"], yes=True)
        self.assertEqual(got, {self.a.ref: "discard", self.b.ref: "device", self.d.ref: "common"})

    def test_a_data_file_is_never_sent_to_the_device_layer(self) -> None:
        self.assertEqual(decide([self.d], to_device=["*"]), {})
        self.assertEqual(decide([self.d], to_device=["*"], discard=["*"]), {self.d.ref: "discard"})
        self.assertEqual(decide([self.d], to_device=["*"], yes=True), {self.d.ref: "common"})


E = ("eden", "qt-config")


class ApplyDecisionsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.a = Candidate(*E, "k1", "4", "1", "1", None, "common")
        self.b = Candidate(*E, "k2", "3", "2", None, "2", "device")
        self.c = Candidate(*E, "k3", "9", "8", None, None, "common")
        self.cands = [self.a, self.b, self.c]

    def test_common_sets_the_key_and_removes_it_from_this_devices_layer(self) -> None:
        common = {E: {"k1": "1"}}
        device = {E: {"k1": "old", "k2": "2"}}
        c2, d2, snap, notes = apply_decisions(common, device, {self.a.ref: "common"}, self.cands)
        self.assertEqual(c2, {E: {"k1": "4"}})
        self.assertEqual(d2, {E: {"k2": "2"}})
        self.assertEqual(snap, {E: {"k1": "4"}})
        self.assertEqual(notes, [])

    def test_device_sets_the_key_in_the_device_layer_only(self) -> None:
        common = {E: {"k2": "1"}}
        c2, d2, snap, _ = apply_decisions(common, {}, {self.b.ref: "device"}, self.cands)
        self.assertEqual(c2, common)
        self.assertEqual(d2, {E: {"k2": "3"}})
        self.assertEqual(snap, {E: {"k2": "3"}})

    def test_discard_changes_no_layer_and_sets_the_snapshot_to_here(self) -> None:
        common = {E: {"k1": "1"}}
        c2, d2, snap, _ = apply_decisions(common, {}, {self.a.ref: "discard"}, self.cands)
        self.assertEqual((c2, d2), (common, {}))
        self.assertEqual(snap, {E: {"k1": "4"}})

    def test_skipped_and_undecided_keys_change_nothing(self) -> None:
        decisions = {self.a.ref: "skip"}
        c2, d2, snap, notes = apply_decisions({E: {"k1": "1"}}, {}, decisions, self.cands)
        self.assertEqual((c2, d2, snap, notes), ({E: {"k1": "1"}}, {}, {}, []))

    def test_the_inputs_are_not_modified(self) -> None:
        common = {E: {"k1": "1"}}
        device = {E: {"k1": "x"}}
        apply_decisions(common, device, {self.a.ref: "common", self.b.ref: "device"}, self.cands)
        self.assertEqual((common, device), ({E: {"k1": "1"}}, {E: {"k1": "x"}}))

    def test_a_table_left_empty_is_dropped(self) -> None:
        _, d2, _, _ = apply_decisions({}, {E: {"k1": "x"}}, {self.a.ref: "common"}, self.cands)
        self.assertEqual(d2, {})

    def test_common_notes_other_device_layers_that_override_the_key(self) -> None:
        others = {"tv": {E: {"k1": "other", "k3": "8"}}, "pc": {E: {"k1": "4"}}, "x": {}}
        _, _, _, notes = apply_decisions(
            {}, {}, {self.a.ref: "common", self.c.ref: "common"}, self.cands, others=others
        )
        self.assertEqual(len(notes), 2)
        self.assertTrue(any("tv" in n and self.a.ref in n for n in notes))
        self.assertTrue(any("tv" in n and self.c.ref in n for n in notes))
        # "pc" has the same value as the new common one, so nothing overrides it.
        self.assertFalse(any("pc" in n for n in notes))
        # a device decision needs no note
        _, _, _, notes = apply_decisions({}, {}, {self.a.ref: "device"}, self.cands, others=others)
        self.assertEqual(notes, [])

    def test_a_data_file_decision_changes_no_layer_but_updates_the_snapshot(self) -> None:
        d = Candidate("xemu", "data", "eeprom", "newhash", "s", None, None, "common", kind="data")
        c2, d2, snap, _ = apply_decisions({}, {}, {d.ref: "common"}, [d])
        self.assertEqual((c2, d2, snap), ({}, {}, {("xemu", "data"): {"eeprom": "newhash"}}))
        _, _, snap, _ = apply_decisions({}, {}, {d.ref: "discard"}, [d])
        self.assertEqual(snap, {("xemu", "data"): {"eeprom": "newhash"}})

    def test_unknown_decision_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            apply_decisions({}, {}, {self.a.ref: "maybe"}, self.cands)
        with self.assertRaises(ValueError):
            apply_decisions({}, {}, {self.d_ref(): "device"}, [self.data_cand()])

    def data_cand(self) -> Candidate:
        return Candidate("xemu", "data", "e", "h", "s", None, None, "common", kind="data")

    def d_ref(self) -> str:
        return self.data_cand().ref

    def test_discarding_a_layered_key_makes_the_next_apply_write_the_layer_value(self) -> None:
        # capture says discard; the snapshot becomes C; apply then writes L.
        _, _, snap, _ = apply_decisions(
            {E: {"k": "hub"}},
            {},
            {"eden/qt-config/k": "discard"},
            [Candidate(*E, "k", "here", "old", "hub", None, "common")],
        )
        plans, _ = plan_apply(
            {"k": "here"}, frozenset(), {"k": "hub"}, snap[E], can_add=True, overwrite_local=False
        )
        self.assertEqual((plans[0].action, plans[0].value), ("write", "hub"))


class LayerDiffTests(unittest.TestCase):
    def test_set_and_removed_keys_by_ref(self) -> None:
        before = {E: {"a": "1", "b": "2", "c": "3"}}
        after = {E: {"a": "1", "b": 2, "d": "4"}, ("x", "y"): {"z": True}}
        set_keys, removed = layer_diff(before, after)
        self.assertEqual(set_keys, ["eden/qt-config/b", "eden/qt-config/d", "x/y/z"])
        self.assertEqual(removed, ["eden/qt-config/c"])


class CaptureSnapshotTests(unittest.TestCase):
    def snap(self, snapshot, current, updates, proposed=()):
        return capture_snapshot(snapshot, current, frozenset(), SPEC, updates, proposed=proposed)

    def test_decided_keys_take_here_and_skipped_keys_keep_their_value(self) -> None:
        got = self.snap({"a": "1", "b": "1"}, {"a": "2", "b": "2"}, {"a": "2"}, proposed=["a", "b"])
        self.assertEqual(got, {"a": "2", "b": "1"})

    def test_new_keys_take_here_but_a_skipped_proposed_new_key_stays_absent(self) -> None:
        got = self.snap({"a": "1"}, {"a": "1", "n": "x", "p": "y"}, {}, proposed=["p"])
        self.assertEqual(got, {"a": "1", "n": "x"})

    def test_a_file_without_a_snapshot_gets_every_tracked_key(self) -> None:
        current = {"a": "1", "[Controls]x": "n", "t_token": "s", "b": "2"}  # gitleaks:allow
        got = self.snap(None, current, {"a": "1"}, proposed=["a", "b"])
        self.assertEqual(got, {"a": "1", "b": "2"})

    def test_keys_the_file_no_longer_has_are_dropped(self) -> None:
        self.assertEqual(self.snap({"a": "1", "gone": "x"}, {"a": "1"}, {}), {"a": "1"})


if __name__ == "__main__":
    unittest.main()
