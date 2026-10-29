import math
import tomllib
import unittest

from warpzone import tomlw


def _roundtrip_value(value: str | int | float | bool) -> object:
    return tomllib.loads(f"v = {tomlw.scalar(value)}")["v"]


class ScalarTests(unittest.TestCase):
    def test_strings_round_trip(self) -> None:
        samples = [
            "",
            "plain",
            'say "hi"',
            "back\\slash",
            "\\\\server\\share",
            "café 日本 \U0001f600",
            "tab\there\nline\rreturn\x08\x0c",
            "".join(chr(c) for c in range(0x20)),
            "\x7f",
            "[Section]key = 'x'",
            "a" * 500,
        ]
        for text in samples:
            with self.subTest(text=text):
                self.assertEqual(_roundtrip_value(text), text)

    def test_string_uses_basic_string_escapes(self) -> None:
        self.assertEqual(tomlw.scalar('a"b\\c\n'), '"a\\"b\\\\c\\n"')
        self.assertEqual(tomlw.scalar("\x01\x7f"), '"\\u0001\\u007F"')
        self.assertEqual(tomlw.scalar("\b\t\f\r"), '"\\b\\t\\f\\r"')

    def test_lone_surrogate_is_refused(self) -> None:
        with self.assertRaises(ValueError):
            tomlw.scalar("\ud800")

    def test_numbers_and_bools(self) -> None:
        for value in (0, 7, -3, 2**40, 1.5, -0.25, 1e300, 1e-7, 3.0):
            with self.subTest(value=value):
                self.assertEqual(_roundtrip_value(value), value)
                self.assertIs(type(_roundtrip_value(value)), type(value))
        self.assertIs(_roundtrip_value(True), True)
        self.assertIs(_roundtrip_value(False), False)
        self.assertEqual(tomlw.scalar(True), "true")

    def test_special_floats(self) -> None:
        self.assertEqual(tomlw.scalar(float("inf")), "inf")
        self.assertEqual(tomlw.scalar(float("-inf")), "-inf")
        self.assertEqual(tomlw.scalar(float("nan")), "nan")
        self.assertEqual(_roundtrip_value(float("inf")), math.inf)
        self.assertEqual(_roundtrip_value(float("-inf")), -math.inf)
        self.assertTrue(math.isnan(_roundtrip_value(float("nan"))))  # type: ignore[arg-type]

    def test_other_types_are_refused(self) -> None:
        for bad in (None, [1], {"a": 1}, b"x"):
            with self.subTest(bad=bad), self.assertRaises(TypeError):
                tomlw.scalar(bad)  # type: ignore[arg-type]


class KeyTests(unittest.TestCase):
    def test_bare_keys(self) -> None:
        for name in ("a", "reicast_region", "x-1", "ABC_9"):
            self.assertEqual(tomlw.key(name), name)

    def test_quoted_keys_round_trip(self) -> None:
        names = [
            "[System]language_index",
            "[UI]Paths\\recentFiles",
            "general.show_welcome",
            "with space",
            'quote"d',
            "",
            "café",
            "tab\tkey",
            "a]b[c",
        ]
        for name in names:
            with self.subTest(name=name):
                parsed = tomllib.loads(f"{tomlw.key(name)} = 1")
                self.assertEqual(parsed, {name: 1})

    def test_quoted_key_in_table_header(self) -> None:
        text = f"[{tomlw.key('eden')}.{tomlw.key('opt:Flycast')}]\nk = 1\n"
        self.assertEqual(tomllib.loads(text), {"eden": {"opt:Flycast": {"k": 1}}})


if __name__ == "__main__":
    unittest.main()
