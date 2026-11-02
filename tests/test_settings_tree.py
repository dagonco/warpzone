import math
import unittest

from warpzone import settings_tree
from warpzone.settings_tree import JSON, TOML, same

NotWritable = settings_tree.NotWritable
FormatError = settings_tree.FormatError


def _bom(text: str) -> str:
    return "﻿" + text


def _crlf(text: str) -> str:
    return text.replace("\n", "\r\n")


class SameTests(unittest.TestCase):
    def test_equality_is_typed(self) -> None:
        self.assertTrue(same(1, 1))
        self.assertTrue(same("a", "a"))
        self.assertTrue(same(1.5, 1.5))
        self.assertTrue(same(True, True))
        self.assertFalse(same(1, 1.0))
        self.assertFalse(same(1, True))
        self.assertFalse(same(0, False))
        self.assertFalse(same("1", 1))
        self.assertFalse(same(1.0, True))

    def test_nan_equals_itself(self) -> None:
        self.assertTrue(same(math.nan, math.nan))
        self.assertFalse(same(math.nan, 1.0))


RYUJINX_LIKE = """{
  "version": 70,
  "res_scale": 1,
  "res_scale_custom": 1.5,
  "docked_mode": false,
  "system_language": "AmericanEnglish",
  "backend_threading": "Auto",
  "hotkeys": {
    "toggle_vsync": "F1",
    "screenshot": "F8"
  },
  "logging_filtered_classes": [],
  "game_dirs": ["/fake/games", "/fake/other"],
  "input_config": [
    {
      "id": "0-fake",
      "player_index": 0
    }
  ],
  "start_fullscreen": null,
  "greeting": "caf\\u00e9 \\"quoted\\" \\\\ back",
  "unicode_title": "日本語 café",
  "a.dotted": 1,
  "nested": {
    "x.y": 2,
    "deep": {
      "flag": true
    }
  }
}
"""

JSON_FIXED = {
    "logging_filtered_classes",
    "game_dirs",
    "input_config",
    "start_fullscreen",
    "a.dotted",
    "nested.x.y",
}


class JsonReadTests(unittest.TestCase):
    def test_flags(self) -> None:
        self.assertEqual(JSON.name, "json")
        self.assertTrue(JSON.typed)
        self.assertFalse(JSON.can_add)

    def test_reads_typed_scalars_by_dotted_path(self) -> None:
        values = JSON.read(RYUJINX_LIKE).values
        self.assertEqual(values["version"], 70)
        self.assertIs(type(values["version"]), int)
        self.assertIs(type(values["res_scale_custom"]), float)
        self.assertIs(values["docked_mode"], False)
        self.assertEqual(values["system_language"], "AmericanEnglish")
        self.assertEqual(values["hotkeys.screenshot"], "F8")
        self.assertIs(values["nested.deep.flag"], True)
        self.assertEqual(values["greeting"], 'café "quoted" \\ back')
        self.assertEqual(values["unicode_title"], "日本語 café")

    def test_arrays_null_and_dotted_keys_are_fixed(self) -> None:
        result = JSON.read(RYUJINX_LIKE)
        self.assertEqual(set(result.fixed), JSON_FIXED)
        self.assertTrue(JSON_FIXED.isdisjoint(result.values))

    def test_every_leaf_is_accounted_for(self) -> None:
        result = JSON.read(RYUJINX_LIKE)
        self.assertEqual(len(result.values), 11)

    def test_duplicate_object_key_is_fixed(self) -> None:
        result = JSON.read('{"a": 1, "a": 2, "b": 3}')
        self.assertEqual(result.values, {"b": 3})
        self.assertEqual(set(result.fixed), {"a"})

    def test_dotted_key_colliding_with_nested_path_is_fixed(self) -> None:
        result = JSON.read('{"a.b": 1, "a": {"b": 2}}')
        self.assertEqual(result.values, {})
        self.assertEqual(set(result.fixed), {"a.b"})

    def test_escaped_key_names_are_decoded(self) -> None:
        result = JSON.read('{"\\u0061b": {"c": 1}}')
        self.assertEqual(result.values, {"ab.c": 1})

    def test_empty_key_is_fixed(self) -> None:
        result = JSON.read('{"": 1, "a": {"": 2}}')
        self.assertEqual(result.values, {})
        self.assertEqual(set(result.fixed), {"", "a."})

    def test_bom_is_accepted(self) -> None:
        self.assertEqual(JSON.read(_bom('{"a": 1}')).values, {"a": 1})

    def test_top_level_non_object_has_no_keys(self) -> None:
        for text in ("[1, 2]", "3", '"x"', "null"):
            with self.subTest(text=text):
                result = JSON.read(text)
                self.assertEqual(result.values, {})
                self.assertEqual(set(result.fixed), set())

    def test_invalid_text_raises_format_error(self) -> None:
        for text in ("", "{", '{"a": }', '{"a": 1,}', '{"a": NaN}', "{'a': 1}"):
            with self.subTest(text=text):
                with self.assertRaises(FormatError):
                    JSON.read(text)
                with self.assertRaises(FormatError):
                    JSON.write(text, {})

    def test_deep_nesting_raises_format_error(self) -> None:
        with self.assertRaises(FormatError):
            JSON.read('{"a":' * 5000 + "1" + "}" * 5000)


class JsonWriteTests(unittest.TestCase):
    def test_empty_changes_are_byte_identical(self) -> None:
        compact = '{"a":1,"b":{"c":[1,2,{"d":null}]},"e":"x"}'
        samples = [
            RYUJINX_LIKE,
            RYUJINX_LIKE.rstrip("\n"),
            _crlf(RYUJINX_LIKE),
            _bom(RYUJINX_LIKE),
            _bom(_crlf(RYUJINX_LIKE)),
            compact,
        ]
        for text in samples:
            with self.subTest(text=text[:30]):
                self.assertEqual(JSON.write(text, {}), text)

    def test_only_the_value_span_changes(self) -> None:
        cases = [
            ("docked_mode", True, '"docked_mode": false', '"docked_mode": true'),
            (
                "system_language",
                "Japanese",
                '"system_language": "AmericanEnglish"',
                '"system_language": "Japanese"',
            ),
            ("res_scale", 2, '"res_scale": 1,', '"res_scale": 2,'),
            (
                "res_scale_custom",
                2.25,
                '"res_scale_custom": 1.5',
                '"res_scale_custom": 2.25',
            ),
            ("hotkeys.screenshot", "F9", '"screenshot": "F8"', '"screenshot": "F9"'),
            ("nested.deep.flag", False, '"flag": true', '"flag": false'),
        ]
        for key, value, old, new in cases:
            with self.subTest(key=key):
                self.assertEqual(RYUJINX_LIKE.count(old), 1)
                expected = RYUJINX_LIKE.replace(old, new)
                self.assertEqual(JSON.write(RYUJINX_LIKE, {key: value}), expected)

    def test_values_read_back_and_others_keep_theirs(self) -> None:
        changes = {"res_scale": 3, "docked_mode": True, "hotkeys.screenshot": "F12"}
        before = JSON.read(RYUJINX_LIKE)
        after = JSON.read(JSON.write(RYUJINX_LIKE, changes))
        for key, value in before.values.items():
            expected = changes.get(key, value)
            self.assertTrue(same(after.values[key], expected), key)
        self.assertEqual(set(after.values), set(before.values))
        self.assertEqual(after.fixed, before.fixed)

    def test_type_can_change(self) -> None:
        out = JSON.write(RYUJINX_LIKE, {"res_scale": "auto"})
        self.assertIn('"res_scale": "auto",', out)
        self.assertEqual(JSON.read(out).values["res_scale"], "auto")
        out = JSON.write(RYUJINX_LIKE, {"res_scale": 1.0})
        self.assertIn('"res_scale": 1.0,', out)
        self.assertIs(type(JSON.read(out).values["res_scale"]), float)
        out = JSON.write(RYUJINX_LIKE, {"res_scale": True})
        self.assertIn('"res_scale": true,', out)

    def test_same_typed_value_leaves_text_alone(self) -> None:
        text = '{"a": 1e2, "b": "\\u00e9"}'
        self.assertEqual(JSON.write(text, {"a": 100.0, "b": "é"}), text)

    def test_strings_with_quotes_escapes_and_unicode(self) -> None:
        value = 'say "hi"\\\n\t日本 \U0001f600'
        out = JSON.write(RYUJINX_LIKE, {"system_language": value})
        self.assertEqual(JSON.read(out).values["system_language"], value)
        self.assertIn("日本", out)
        self.assertNotIn("\\u65e5", out)

    def test_bom_and_crlf_survive_a_change(self) -> None:
        text = _bom(_crlf(RYUJINX_LIKE))
        out = JSON.write(text, {"docked_mode": True})
        self.assertEqual(out, text.replace('"docked_mode": false', '"docked_mode": true'))

    def test_compact_text(self) -> None:
        text = '{"a":1,"b":{"c":"x"}}'
        self.assertEqual(JSON.write(text, {"a": 2, "b.c": "y"}), '{"a":2,"b":{"c":"y"}}')

    def test_several_changes_in_one_write(self) -> None:
        text = '{"a": "long value here", "b": 1, "c": true}'
        out = JSON.write(text, {"a": "s", "b": 12345, "c": False})
        self.assertEqual(out, '{"a": "s", "b": 12345, "c": false}')

    def test_fixed_keys_are_not_writable(self) -> None:
        for key in JSON_FIXED:
            with self.subTest(key=key):
                with self.assertRaises(NotWritable) as ctx:
                    JSON.write(RYUJINX_LIKE, {key: 1})
                self.assertEqual(ctx.exception.key, key)

    def test_missing_key_is_not_writable(self) -> None:
        with self.assertRaises(NotWritable) as ctx:
            JSON.write(RYUJINX_LIKE, {"brand_new": 1})
        self.assertEqual(ctx.exception.key, "brand_new")
        with self.assertRaises(NotWritable):
            JSON.write(RYUJINX_LIKE, {"hotkeys.new": "F2"})

    def test_a_refused_key_refuses_the_whole_write(self) -> None:
        with self.assertRaises(NotWritable):
            JSON.write(RYUJINX_LIKE, {"res_scale": 2, "brand_new": 1})

    def test_values_json_cannot_hold_are_refused(self) -> None:
        for value in (math.nan, math.inf):
            with self.subTest(value=value), self.assertRaises(NotWritable):
                JSON.write(RYUJINX_LIKE, {"res_scale": value})


MELONDS_LIKE = """# fake melonDS-like config
Language = 1
LimitFPS = true
TargetFPS = 60.0
UITheme = ""
RecentROM = ["/fake/a.nds", "/fake/b.nds"]
LastROMFolder = "/fake/roms"  # trailing comment
HexValue = 0x10

[DSi]
BIOS9Path = ""
FullBIOSBoot = true

[Instance0.Firmware]
Language = 1
Username = "fake"   # name
Birthday.Month = 5
"odd key" = 7
'lit.key' = 'raw\\path'

# comment before the next table

[Instance0.Camera]
InputType = 0
"""

MELONDS_VALUES = {
    "Language": 1,
    "LimitFPS": True,
    "TargetFPS": 60.0,
    "UITheme": "",
    "LastROMFolder": "/fake/roms",
    "HexValue": 16,
    "DSi.BIOS9Path": "",
    "DSi.FullBIOSBoot": True,
    "Instance0.Firmware.Language": 1,
    "Instance0.Firmware.Username": "fake",
    'Instance0.Firmware."odd key"': 7,
    'Instance0.Firmware."lit.key"': "raw\\path",
    "Instance0.Camera.InputType": 0,
}

MELONDS_FIXED = {"RecentROM", "Instance0.Firmware.Birthday.Month"}

XEMU_LIKE = """[general]
show_welcome = true
last_viewed_menu_index = 0

[general.misc]
skip_boot_anim = false

[input]
gamepad_mappings = [
  { gamepad_id = "00-fake", a = 1, b = "]" },
  # a comment with [brackets] and "quotes
  { gamepad_id = "01-fake", a = 2 },
]  # after the array

[input.bindings]
port1_driver = 'usb-xbox'
port1 = "00-fake"

[display.quality]
surface_scale = 1

[sys]
mem_limit = '64'

[sys.files]
hdd_path = "/fake/hdd.qcow2"
"""

XEMU_VALUES = {
    "general.show_welcome": True,
    "general.last_viewed_menu_index": 0,
    "general.misc.skip_boot_anim": False,
    "input.bindings.port1_driver": "usb-xbox",
    "input.bindings.port1": "00-fake",
    "display.quality.surface_scale": 1,
    "sys.mem_limit": "64",
    "sys.files.hdd_path": "/fake/hdd.qcow2",
}

ODD_LIKE = """title = "x"
text = \"\"\"
[not.a.header]
fake = 1
\"\"\"
lit = \'\'\'
[also.not] # nor this
\'\'\'
when = 1979-05-27T07:32:00Z
inline = { a = 1, b = { c = "}" } }
hash = "a # b"  # real comment
closing = "a]b" # [x]
quotes = \"\"\"he said ""hi""\"\"\"
after = 5

[[items]]
name = "one"

[[items]]
name = "two"

[items.sub]
deep = 1

[plain]
v = 1
"""

ODD_VALUES = {"title": "x", "hash": "a # b", "closing": "a]b", "after": 5, "plain.v": 1}
ODD_FIXED = {
    "text",
    "lit",
    "when",
    "inline",
    "quotes",
    "items.name",
    "items.sub.deep",
}


class TomlReadTests(unittest.TestCase):
    def test_flags(self) -> None:
        self.assertEqual(TOML.name, "toml")
        self.assertTrue(TOML.typed)
        self.assertTrue(TOML.can_add)

    def test_melonds_like(self) -> None:
        result = TOML.read(MELONDS_LIKE)
        self.assertEqual(result.values, MELONDS_VALUES)
        self.assertEqual(set(result.fixed), MELONDS_FIXED)

    def test_types_are_kept(self) -> None:
        values = TOML.read(MELONDS_LIKE).values
        self.assertIs(type(values["Language"]), int)
        self.assertIs(type(values["LimitFPS"]), bool)
        self.assertIs(type(values["TargetFPS"]), float)
        self.assertIs(type(values["UITheme"]), str)

    def test_xemu_like_multiline_array_is_fixed(self) -> None:
        result = TOML.read(XEMU_LIKE)
        self.assertEqual(result.values, XEMU_VALUES)
        self.assertEqual(set(result.fixed), {"input.gamepad_mappings"})

    def test_strings_arrays_and_tables_that_look_like_keys(self) -> None:
        result = TOML.read(ODD_LIKE)
        self.assertEqual(result.values, ODD_VALUES)
        self.assertEqual(set(result.fixed), ODD_FIXED)

    def test_table_names_are_quoted_when_not_bare(self) -> None:
        text = '["a.b"]\nk = 1\n[ x . "y z" ]\nj = 2\n[\'lit\']\nm = 3\n'
        result = TOML.read(text)
        self.assertEqual(result.values, {'"a.b".k': 1, 'x."y z".j': 2, "lit.m": 3})

    def test_bom_and_crlf(self) -> None:
        for text in (_bom(XEMU_LIKE), _crlf(XEMU_LIKE), _bom(_crlf(XEMU_LIKE))):
            with self.subTest(text=text[:12]):
                result = TOML.read(text)
                self.assertEqual(result.values, XEMU_VALUES)
                self.assertEqual(set(result.fixed), {"input.gamepad_mappings"})
        self.assertEqual(TOML.read(_crlf(MELONDS_LIKE)).values, MELONDS_VALUES)

    def test_invalid_text_raises_format_error(self) -> None:
        for text in ("a = ", "[x", "a = 1\na = 2\n", "a = 'unterminated\n"):
            with self.subTest(text=text):
                with self.assertRaises(FormatError):
                    TOML.read(text)
                with self.assertRaises(FormatError):
                    TOML.write(text, {})

    def test_empty_text(self) -> None:
        result = TOML.read("")
        self.assertEqual(result.values, {})
        self.assertEqual(set(result.fixed), set())


class TomlWriteTests(unittest.TestCase):
    def test_empty_changes_are_byte_identical(self) -> None:
        samples = []
        for base in (MELONDS_LIKE, XEMU_LIKE, ODD_LIKE):
            samples += [
                base,
                base.rstrip("\n"),
                _crlf(base),
                _bom(base),
                _bom(_crlf(base)),
            ]
        for text in samples:
            with self.subTest(text=text[:20]):
                self.assertEqual(TOML.write(text, {}), text)

    def test_only_the_value_span_changes(self) -> None:
        cases = [
            ("Language", 2, "Language = 1\nLimit", "Language = 2\nLimit"),
            ("LimitFPS", False, "LimitFPS = true", "LimitFPS = false"),
            ("TargetFPS", 30.5, "TargetFPS = 60.0", "TargetFPS = 30.5"),
            ("UITheme", "dark", 'UITheme = ""', 'UITheme = "dark"'),
            (
                "LastROMFolder",
                "/fake/other",
                '"/fake/roms"  # trailing comment',
                '"/fake/other"  # trailing comment',
            ),
            ("HexValue", 32, "HexValue = 0x10", "HexValue = 32"),
            (
                "Instance0.Firmware.Username",
                "other",
                'Username = "fake"   # name',
                'Username = "other"   # name',
            ),
            (
                'Instance0.Firmware."lit.key"',
                "p",
                "'lit.key' = 'raw\\path'",
                "'lit.key' = \"p\"",
            ),
            ("DSi.FullBIOSBoot", False, "FullBIOSBoot = true", "FullBIOSBoot = false"),
        ]
        for key, value, old, new in cases:
            with self.subTest(key=key):
                self.assertEqual(MELONDS_LIKE.count(old), 1, old)
                expected = MELONDS_LIKE.replace(old, new)
                self.assertEqual(TOML.write(MELONDS_LIKE, {key: value}), expected)

    def test_literal_string_becomes_basic(self) -> None:
        out = TOML.write(XEMU_LIKE, {"input.bindings.port1_driver": "usb-fake"})
        self.assertEqual(out, XEMU_LIKE.replace("'usb-xbox'", '"usb-fake"'))

    def test_values_read_back_and_others_keep_theirs(self) -> None:
        changes = {
            "Language": 5,
            "LimitFPS": False,
            "Instance0.Firmware.Username": 'quo"te\n',
            "DSi.BIOS9Path": "/fake/bios",
        }
        before = TOML.read(MELONDS_LIKE)
        after = TOML.read(TOML.write(MELONDS_LIKE, changes))
        self.assertEqual(set(after.values), set(before.values))
        self.assertEqual(after.fixed, before.fixed)
        for key, value in before.values.items():
            self.assertTrue(same(after.values[key], changes.get(key, value)), key)

    def test_type_can_change(self) -> None:
        out = TOML.write(MELONDS_LIKE, {"Language": "es"})
        self.assertIn('Language = "es"\n', out)
        out = TOML.write(MELONDS_LIKE, {"Language": 1.0})
        self.assertIn("Language = 1.0\n", out)
        self.assertIs(type(TOML.read(out).values["Language"]), float)
        out = TOML.write(MELONDS_LIKE, {"Language": True})
        self.assertIn("Language = true\n", out)

    def test_same_typed_value_leaves_text_alone(self) -> None:
        self.assertEqual(TOML.write(MELONDS_LIKE, {"HexValue": 16}), MELONDS_LIKE)

    def test_changes_with_bom_crlf_and_no_final_newline(self) -> None:
        for text in (_bom(MELONDS_LIKE), _crlf(MELONDS_LIKE), MELONDS_LIKE.rstrip("\n")):
            with self.subTest(text=text[:12]):
                out = TOML.write(text, {"Language": 2})
                self.assertEqual(out, text.replace("Language = 1", "Language = 2", 1))

    def test_adds_a_key_after_the_last_key_of_its_table(self) -> None:
        out = TOML.write(MELONDS_LIKE, {"Instance0.Firmware.Region": "EU"})
        self.assertEqual(
            out,
            MELONDS_LIKE.replace(
                "'lit.key' = 'raw\\path'\n",
                "'lit.key' = 'raw\\path'\nRegion = \"EU\"\n",
            ),
        )
        self.assertEqual(TOML.read(out).values["Instance0.Firmware.Region"], "EU")

    def test_adds_a_root_key_before_the_first_table(self) -> None:
        out = TOML.write(MELONDS_LIKE, {"Brand": 1})
        self.assertEqual(
            out, MELONDS_LIKE.replace("HexValue = 0x10\n", "HexValue = 0x10\nBrand = 1\n")
        )

    def test_adds_a_root_key_when_the_file_starts_with_a_table(self) -> None:
        out = TOML.write("[a]\nx = 1\n", {"top": True})
        self.assertEqual(out, "top = true\n\n[a]\nx = 1\n")

    def test_adds_after_a_multiline_value(self) -> None:
        out = TOML.write(XEMU_LIKE, {"input.extra": "v"})
        expected = XEMU_LIKE.replace(
            "]  # after the array\n", ']  # after the array\nextra = "v"\n'
        )
        self.assertEqual(out, expected)

    def test_adds_to_a_table_with_only_a_header(self) -> None:
        out = TOML.write("[a]\n\n[b]\nx = 1\n", {"a.k": 1})
        self.assertEqual(out, "[a]\nk = 1\n\n[b]\nx = 1\n")

    def test_adds_several_keys_to_one_table_in_order(self) -> None:
        out = TOML.write(XEMU_LIKE, {"sys.b": 2, "sys.a": 1, "general.misc.z": "z"})
        self.assertEqual(
            out,
            XEMU_LIKE.replace("mem_limit = '64'\n", "mem_limit = '64'\nb = 2\na = 1\n").replace(
                "skip_boot_anim = false\n", 'skip_boot_anim = false\nz = "z"\n'
            ),
        )

    def test_adds_a_missing_table_at_the_end(self) -> None:
        out = TOML.write(XEMU_LIKE, {"audio.volume": 0.5, "audio.mute": False})
        self.assertEqual(out, XEMU_LIKE + "\n[audio]\nvolume = 0.5\nmute = false\n")

    def test_adds_a_missing_nested_and_quoted_table(self) -> None:
        out = TOML.write("a = 1\n", {'x."y z".k': 1, "x.m": 2})
        self.assertEqual(out, 'a = 1\n\n[x."y z"]\nk = 1\n\n[x]\nm = 2\n')
        self.assertEqual(TOML.read(out).values, {"a": 1, 'x."y z".k': 1, "x.m": 2})

    def test_adds_a_super_table_after_its_sub_table(self) -> None:
        out = TOML.write("[a.b]\nx = 1\n", {"a.k": 1})
        self.assertEqual(out, "[a.b]\nx = 1\n\n[a]\nk = 1\n")

    def test_adds_without_final_newline(self) -> None:
        out = TOML.write("[a]\nx = 1", {"a.k": 2, "b.j": 3})
        self.assertEqual(out, "[a]\nx = 1\nk = 2\n\n[b]\nj = 3\n")
        out = TOML.write("[a]\nx = 1", {"a.k": 2})
        self.assertEqual(out, "[a]\nx = 1\nk = 2")

    def test_adds_use_the_line_endings_of_the_file(self) -> None:
        text = _bom(_crlf(XEMU_LIKE))
        out = TOML.write(text, {"sys.a": 1, "audio.volume": 1})
        expected = _bom(
            _crlf(
                XEMU_LIKE.replace("mem_limit = '64'\n", "mem_limit = '64'\na = 1\n")
                + "\n[audio]\nvolume = 1\n"
            )
        )
        self.assertEqual(out, expected)
        self.assertNotIn("\n", out.replace("\r\n", ""))

    def test_adds_to_an_empty_text(self) -> None:
        self.assertEqual(TOML.write("", {"a": 1}), "a = 1\n")
        self.assertEqual(TOML.write("", {"t.a": 1}), "[t]\na = 1\n")

    def test_adds_next_to_tricky_lines(self) -> None:
        out = TOML.write(ODD_LIKE, {"plain.w": 2, "after2": 1})
        self.assertEqual(
            out,
            ODD_LIKE.replace("v = 1\n", "v = 1\nw = 2\n").replace(
                "after = 5\n", "after = 5\nafter2 = 1\n"
            ),
        )
        self.assertEqual(TOML.read(out).values["plain.w"], 2)

    def test_fixed_keys_are_not_writable(self) -> None:
        cases = [
            (MELONDS_LIKE, MELONDS_FIXED),
            (XEMU_LIKE, {"input.gamepad_mappings"}),
            (ODD_LIKE, ODD_FIXED),
        ]
        for text, fixed in cases:
            for key in fixed:
                with self.subTest(key=key):
                    with self.assertRaises(NotWritable) as ctx:
                        TOML.write(text, {key: 1})
                    self.assertEqual(ctx.exception.key, key)

    def test_a_table_that_cannot_take_keys_refuses(self) -> None:
        cases = [
            (ODD_LIKE, "items.extra"),
            (ODD_LIKE, "items.sub.extra"),
            (XEMU_LIKE, "input.gamepad_mappings.x"),
            ("a.b = 1\n", "a.c"),
            ("t = { x = 1 }\n", "t.y"),
            ("a = 1\n", "a.b"),
        ]
        for text, key in cases:
            with self.subTest(key=key):
                with self.assertRaises(NotWritable) as ctx:
                    TOML.write(text, {key: 1})
                self.assertEqual(ctx.exception.key, key)

    def test_a_refused_key_refuses_the_whole_write(self) -> None:
        with self.assertRaises(NotWritable) as ctx:
            TOML.write(ODD_LIKE, {"after": 6, "when": 1})
        self.assertEqual(ctx.exception.key, "when")

    def test_values_toml_cannot_hold_are_refused(self) -> None:
        with self.assertRaises(NotWritable):
            TOML.write(MELONDS_LIKE, {"Language": "\ud800"})

    def test_nan_and_inf_round_trip(self) -> None:
        out = TOML.write(MELONDS_LIKE, {"TargetFPS": math.inf})
        self.assertEqual(TOML.read(out).values["TargetFPS"], math.inf)
        again = TOML.write(out, {"TargetFPS": math.nan})
        self.assertTrue(math.isnan(TOML.read(again).values["TargetFPS"]))  # type: ignore[arg-type]
        self.assertEqual(TOML.write(again, {"TargetFPS": math.nan}), again)

    def test_xemu_like_edit_keeps_the_array_untouched(self) -> None:
        out = TOML.write(XEMU_LIKE, {"sys.mem_limit": "128", "general.show_welcome": False})
        self.assertEqual(
            out,
            XEMU_LIKE.replace("mem_limit = '64'", 'mem_limit = "128"').replace(
                "show_welcome = true", "show_welcome = false"
            ),
        )


class EveryWritableKeyTests(unittest.TestCase):
    """Changing any one key changes its value and nothing else."""

    def _check(self, handler, text: str) -> None:
        before = handler.read(text)
        self.assertTrue(before.values)
        for key, value in before.values.items():
            if isinstance(value, bool):
                new: object = not value
            elif isinstance(value, int):
                new = value + 1
            elif isinstance(value, float):
                new = value + 0.5
            else:
                new = value + "x"
            with self.subTest(key=key):
                out = handler.write(text, {key: new})
                after = handler.read(out)
                self.assertEqual(after.fixed, before.fixed)
                self.assertEqual(set(after.values), set(before.values))
                for other, old in before.values.items():
                    self.assertTrue(same(after.values[other], new if other == key else old))
                self.assertEqual(handler.write(out, {}), out)

    def test_json(self) -> None:
        self._check(JSON, RYUJINX_LIKE)
        self._check(JSON, _bom(_crlf(RYUJINX_LIKE)))

    def test_toml(self) -> None:
        for text in (MELONDS_LIKE, XEMU_LIKE, ODD_LIKE, _bom(_crlf(XEMU_LIKE))):
            self._check(TOML, text)


if __name__ == "__main__":
    unittest.main()
