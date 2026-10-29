import math
import unittest

from warpzone import settings_tree
from warpzone.settings_tree import JSON, same

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


if __name__ == "__main__":
    unittest.main()
