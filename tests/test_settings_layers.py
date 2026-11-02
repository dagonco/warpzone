import tomllib
import unittest

from warpzone import settings_layers as sl
from warpzone.profiles import DataFile, EmulatorSettings, Profile, SettingsFile


def _profile() -> Profile:
    eden = EmulatorSettings(
        id="eden",
        process=("eden",),
        files={
            "qt-config": SettingsFile(
                id="qt-config",
                path="~/.config/eden/qt-config.ini",
                format="qt-ini",
                secret=("[WebService]*_username",),
            )
        },
    )
    retro = EmulatorSettings(
        id="retroarch",
        process=("retroarch",),
        files={
            "cfg": SettingsFile(id="cfg", path="~/r/retroarch.cfg", format="retroarch-cfg"),
            "opt": SettingsFile(id="opt", path="~/r/{name}/{name}.opt", format="retroarch-cfg"),
        },
    )
    ryujinx = EmulatorSettings(
        id="ryujinx",
        process=("ryujinx",),
        files={"config": SettingsFile(id="config", path="~/r/Config.json", format="json")},
    )
    xemu = EmulatorSettings(
        id="xemu",
        process=("xemu",),
        files={"config": SettingsFile(id="config", path="~/x/xemu.toml", format="toml")},
        data={"eeprom": DataFile(id="eeprom", path="~/x/eeprom.bin")},
    )
    return Profile(
        name="test",
        description="",
        settings={e.id: e for e in (eden, retro, ryujinx, xemu)},
    )


GOOD = """\
format = 1

[eden.qt-config]
"[System]language_index" = "1"

[retroarch.cfg]
savestate_auto_save = "true"

[retroarch."opt:Flycast"]
reicast_region = "Europe"

[ryujinx.config]
docked_mode = false
res_scale = 1

[xemu.config]
"general.show_welcome" = false
"""


def _layer(text: str = GOOD) -> sl.Layer:
    return sl.parse_layer(text, "common.toml")[0]


class ParseTests(unittest.TestCase):
    def test_parses_tables_and_instances(self) -> None:
        layer, warnings = sl.parse_layer(GOOD, "common.toml")
        self.assertEqual(warnings, [])
        self.assertEqual(layer.entries[("retroarch", "opt:Flycast")], {"reicast_region": "Europe"})
        self.assertEqual(
            layer.entries[("ryujinx", "config")], {"docked_mode": False, "res_scale": 1}
        )
        self.assertEqual(layer.entries[("xemu", "config")], {"general.show_welcome": False})

    def test_format_only_is_an_empty_layer(self) -> None:
        self.assertEqual(sl.parse_layer("format = 1\n", "x.toml")[0].entries, {})

    def assertRejected(self, text: str, fragment: str) -> None:
        with self.assertRaises(sl.LayerError) as ctx:
            sl.parse_layer(text, "deck.toml")
        self.assertIn(fragment, str(ctx.exception))
        self.assertIn("deck.toml", str(ctx.exception))

    def test_bad_toml(self) -> None:
        self.assertRejected("format = 1\n[eden\n", "not valid TOML")

    def test_missing_format(self) -> None:
        self.assertRejected('[eden.qt-config]\na = "b"\n', "format")

    def test_unknown_format(self) -> None:
        self.assertRejected("format = 2\n", "format")

    def test_format_of_the_wrong_kind(self) -> None:
        self.assertRejected('format = "1"\n', "format")
        self.assertRejected("format = true\n", "format")
        self.assertRejected("format = 1.0\n", "format")

    def test_top_level_value_that_is_not_a_table(self) -> None:
        self.assertRejected("format = 1\neden = 3\n", "eden")

    def test_emulator_entry_that_is_not_a_table_of_tables(self) -> None:
        self.assertRejected('format = 1\n[eden]\nkey = "v"\n', "eden.key")

    def test_non_scalar_values(self) -> None:
        self.assertRejected("format = 1\n[a.b]\nk = [1, 2]\n", "a.b")
        self.assertRejected("format = 1\n[a.b.c]\nk = 1\n", "a.b")
        self.assertRejected("format = 1\n[a.b]\nk = 1979-05-27\n", "a.b")

    def test_every_error_is_reported(self) -> None:
        with self.assertRaises(sl.LayerError) as ctx:
            sl.parse_layer("format = 1\n[a.b]\nk = [1]\nj = [2]\n", "x.toml")
        self.assertEqual(len(ctx.exception.errors), 2)

    def test_empty_tables_are_dropped(self) -> None:
        layer, _ = sl.parse_layer("format = 1\n[a.b]\n", "x.toml")
        self.assertEqual(layer.entries, {})


class ValidateTests(unittest.TestCase):
    def validate(self, text: str) -> tuple[list[str], list[str]]:
        return sl.validate(_layer(text), _profile(), "common.toml")

    def test_good_layer(self) -> None:
        self.assertEqual(self.validate(GOOD), ([], []))

    def test_unknown_emulator_is_a_warning(self) -> None:
        errors, warnings = self.validate('format = 1\n[cemu.x]\na = "b"\n')
        self.assertEqual(errors, [])
        self.assertEqual(len(warnings), 1)
        self.assertIn("cemu", warnings[0])

    def test_unknown_file_is_a_warning(self) -> None:
        errors, warnings = self.validate('format = 1\n[eden.other]\na = "b"\n')
        self.assertEqual(errors, [])
        self.assertIn("eden.other", warnings[0])

    def test_unknown_instance_base_is_a_warning(self) -> None:
        errors, warnings = self.validate('format = 1\n[eden."other:x"]\na = "b"\n')
        self.assertEqual(errors, [])
        self.assertEqual(len(warnings), 1)

    def test_raw_format_needs_strings(self) -> None:
        for value in ("1", "true", "1.5"):
            errors, _ = self.validate(f"format = 1\n[eden.qt-config]\nk = {value}\n")
            self.assertEqual(len(errors), 1, value)
            self.assertIn("eden.qt-config", errors[0])
            self.assertIn("string", errors[0])

    def test_typed_formats_take_any_scalar(self) -> None:
        text = 'format = 1\n[ryujinx.config]\na = "s"\nb = 2\nc = 2.5\nd = true\n'
        self.assertEqual(self.validate(text), ([], []))

    def test_retroarch_value_with_a_quote(self) -> None:
        errors, _ = self.validate('format = 1\n[retroarch.cfg]\nvideo_driver = "a\\"b"\n')
        self.assertEqual(len(errors), 1)
        self.assertIn("video_driver", errors[0])
        self.assertNotIn('a"b', errors[0])

    def test_quote_is_fine_in_other_raw_formats(self) -> None:
        text = 'format = 1\n[eden.qt-config]\nk = "a\\"b"\n'
        self.assertEqual(self.validate(text), ([], []))

    def test_raw_value_with_a_line_break(self) -> None:
        errors, _ = self.validate('format = 1\n[eden.qt-config]\nk = "a\\nb"\n')
        self.assertEqual(len(errors), 1)
        self.assertNotIn("a\nb", errors[0])

    def test_profile_secret_is_an_error_naming_the_key_only(self) -> None:
        text = 'format = 1\n[eden.qt-config]\n"[WebService]web_username" = "hunter2-fake"\n'
        errors, _ = self.validate(text)
        self.assertEqual(len(errors), 1)
        self.assertIn("[WebService]web_username", errors[0])
        self.assertIn("secret", errors[0])
        self.assertNotIn("hunter2-fake", errors[0])

    def test_builtin_secret_net(self) -> None:
        for key in ("[WebService]web_token", "cheevos_password", "api_key", "x.Secret"):
            text = f'format = 1\n[eden.qt-config]\n"{key}" = "s3cr3t-fake"\n'
            errors, _ = self.validate(text)
            self.assertEqual(len(errors), 1, key)
            self.assertNotIn("s3cr3t-fake", errors[0])

    def test_secret_net_applies_to_unknown_emulators_too(self) -> None:
        errors, warnings = self.validate('format = 1\n[cemu.x]\nmy_token = "s3cr3t-fake"\n')
        self.assertEqual(len(errors), 1)
        self.assertEqual(len(warnings), 1)
        self.assertNotIn("s3cr3t-fake", errors[0] + warnings[0])

    def test_secret_value_never_appears_in_any_message(self) -> None:
        text = (
            "format = 1\n"
            '[eden.qt-config]\nk_token = "VALUE-fake-1"\nn = 5\n'
            '[retroarch.cfg]\nvideo = "a\\"VALUE-fake-2"\n'
            '[cemu.x]\ny_password = "VALUE-fake-3"\n'
        )
        errors, warnings = self.validate(text)
        self.assertGreaterEqual(len(errors), 3)
        self.assertNotIn("VALUE-fake", "\n".join(errors + warnings))

    def test_instance_names(self) -> None:
        ok = 'format = 1\n[retroarch."opt:Flycast"]\nk = "v"\n'
        self.assertEqual(self.validate(ok), ([], []))
        for name in ("", "a/b", "..", "."):
            text = f'format = 1\n[retroarch."opt:{name}"]\nk = "v"\n'
            errors, _ = self.validate(text)
            self.assertEqual(len(errors), 1, repr(name))

    def test_templated_file_needs_an_instance(self) -> None:
        errors, _ = self.validate('format = 1\n[retroarch.opt]\nk = "v"\n')
        self.assertEqual(len(errors), 1)

    def test_plain_file_takes_no_instance(self) -> None:
        errors, _ = self.validate('format = 1\n[retroarch."cfg:x"]\nk = "v"\n')
        self.assertEqual(len(errors), 1)

    def test_errors_name_the_source(self) -> None:
        errors, _ = self.validate("format = 1\n[eden.qt-config]\nk = 1\n")
        self.assertTrue(errors[0].startswith("common.toml"))


class MergeTests(unittest.TestCase):
    def test_device_wins_per_key(self) -> None:
        common = _layer('format = 1\n[a.f]\nx = "c"\ny = "c"\n[b.f]\nz = "c"\n')
        device = _layer('format = 1\n[a.f]\ny = "d"\nw = "d"\n[c.f]\nq = "d"\n')
        self.assertEqual(
            sl.effective(common, device),
            {
                ("a", "f"): {"x": "c", "y": "d", "w": "d"},
                ("b", "f"): {"z": "c"},
                ("c", "f"): {"q": "d"},
            },
        )

    def test_a_device_cannot_unset_a_common_key(self) -> None:
        common = _layer('format = 1\n[a.f]\nx = "c"\n')
        self.assertEqual(sl.effective(common, _layer("format = 1\n")), {("a", "f"): {"x": "c"}})

    def test_merge_does_not_alias_the_inputs(self) -> None:
        common = _layer('format = 1\n[a.f]\nx = "c"\n')
        merged = sl.effective(common, sl.Layer({}))
        merged[("a", "f")]["x"] = "changed"
        self.assertEqual(common.entries[("a", "f")]["x"], "c")

    def test_empty_layers(self) -> None:
        self.assertEqual(sl.effective(sl.Layer({}), sl.Layer({})), {})


class RenderTests(unittest.TestCase):
    def test_header_and_format_first(self) -> None:
        lines = sl.render(_layer()).splitlines()
        self.assertEqual(
            lines[0],
            "# Written by warpzone settings capture. Edit on the hub only; comments are not kept.",
        )
        self.assertEqual(lines[1], "format = 1")

    def test_round_trip(self) -> None:
        layer = _layer()
        self.assertEqual(sl.parse_layer(sl.render(layer), "x.toml")[0], layer)

    def test_stable_and_sorted(self) -> None:
        a = _layer('format = 1\n[z.f]\nb = "1"\na = "2"\n[a.g]\nk = "1"\n[a.f]\nk = "1"\n')
        b = _layer('format = 1\n[a.f]\nk = "1"\n[a.g]\nk = "1"\n[z.f]\na = "2"\nb = "1"\n')
        self.assertEqual(sl.render(a), sl.render(b))
        text = sl.render(a)
        self.assertLess(text.index("[a.f]"), text.index("[a.g]"))
        self.assertLess(text.index("[a.g]"), text.index("[z.f]"))
        self.assertLess(text.index("a = "), text.index("b = "))
        self.assertEqual(sl.render(sl.parse_layer(text, "x")[0]), text)

    def test_awkward_keys_and_values_survive(self) -> None:
        layer = sl.Layer(
            {
                ("eden", "qt-config"): {
                    "[UI]Paths\\recent files": 'a "b" \\ c\n',
                    "[System]x%20y": "é日本",
                },
                ("retroarch", "opt:Fly cast"): {"k": "v"},
                ("xemu", "config"): {"a.b.c": 1.5, "n": -3, "t": True},
            }
        )
        text = sl.render(layer)
        tomllib.loads(text)
        self.assertEqual(sl.parse_layer(text, "x")[0], layer)

    def test_empty_layer(self) -> None:
        text = sl.render(sl.Layer({}))
        self.assertEqual(sl.parse_layer(text, "x")[0].entries, {})
        self.assertTrue(text.endswith("\n"))

    def test_empty_tables_are_not_written(self) -> None:
        text = sl.render(sl.Layer({("a", "f"): {}}))
        self.assertNotIn("[a.f]", text)


class EditTests(unittest.TestCase):
    def test_set_key_is_immutable(self) -> None:
        layer = _layer()
        new = sl.set_key(layer, "eden", "qt-config", "[System]region_index", "2")
        self.assertEqual(layer, _layer())
        self.assertEqual(new.entries[("eden", "qt-config")]["[System]region_index"], "2")
        self.assertEqual(new.entries[("eden", "qt-config")]["[System]language_index"], "1")

    def test_set_key_replaces_and_creates_tables(self) -> None:
        layer = sl.set_key(sl.Layer({}), "a", "f", "k", 1)
        layer = sl.set_key(layer, "a", "f", "k", 2)
        layer = sl.set_key(layer, "b", "g:x", "j", False)
        self.assertEqual(layer.entries, {("a", "f"): {"k": 2}, ("b", "g:x"): {"j": False}})

    def test_remove_key_is_immutable_and_drops_empty_tables(self) -> None:
        layer = sl.set_key(sl.Layer({}), "a", "f", "k", 1)
        gone = sl.remove_key(layer, "a", "f", "k")
        self.assertEqual(layer.entries, {("a", "f"): {"k": 1}})
        self.assertEqual(gone.entries, {})

    def test_remove_missing_key_is_a_no_op(self) -> None:
        layer = _layer()
        self.assertEqual(sl.remove_key(layer, "eden", "qt-config", "nope"), layer)
        self.assertEqual(sl.remove_key(layer, "nope", "x", "nope"), layer)


class ConflictTests(unittest.TestCase):
    def test_finds_conflict_copies(self) -> None:
        names = [
            "common.toml",
            "deck.toml",
            "common.sync-conflict-20260101-120000-ABCDEFG.toml",
            "deck.sync-conflict-20260101-120000-ABCDEFG.toml",
            "files/xemu/eeprom",
            "files/xemu/eeprom.sync-conflict-20260101-120000-ABCDEFG",
            ".stfolder",
            "notes.txt",
        ]
        self.assertEqual(
            sl.conflict_copies(names),
            [
                "common.sync-conflict-20260101-120000-ABCDEFG.toml",
                "deck.sync-conflict-20260101-120000-ABCDEFG.toml",
                "files/xemu/eeprom.sync-conflict-20260101-120000-ABCDEFG",
            ],
        )

    def test_conflict_directory_under_files(self) -> None:
        names = ["files/xemu.sync-conflict-20260101-120000-ABCDEFG/eeprom"]
        self.assertEqual(sl.conflict_copies(names), names)

    def test_none(self) -> None:
        self.assertEqual(sl.conflict_copies(["common.toml", "files/a/b"]), [])
        self.assertEqual(sl.conflict_copies([]), [])

    def test_accepts_any_iterable(self) -> None:
        got = sl.conflict_copies(iter(["x.sync-conflict-1-2-3.toml"]))
        self.assertEqual(got, ["x.sync-conflict-1-2-3.toml"])


if __name__ == "__main__":
    unittest.main()
