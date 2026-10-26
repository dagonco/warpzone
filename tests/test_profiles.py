import tempfile
import unittest
from pathlib import Path

from warpzone.profiles import (
    ProfileError,
    available_profiles,
    load_profile,
    load_profile_file,
    parse_profile,
)


class ProfileTests(unittest.TestCase):
    def test_shipped_profile_loads(self) -> None:
        profile = load_profile("steamos-emudeck")
        self.assertEqual(profile.paths["library"], "~/Emulation")
        self.assertEqual(len(profile.saves), 18)
        ryujinx = profile.saves["ryujinx-user"]
        self.assertEqual((ryujinx.emu, ryujinx.kind), ("ryujinx", "user"))
        self.assertEqual(ryujinx.ignore, ("!/save", "!/saveMeta", "*"))
        self.assertEqual(profile.saves["dolphin-gc"].ignore, ())

    def test_every_shipped_profile_is_valid(self) -> None:
        self.assertIn("steamos-emudeck", available_profiles())
        for name in available_profiles():
            self.assertEqual(load_profile(name).name, name)

    def test_unknown_and_unsafe_names(self) -> None:
        with self.assertRaises(ProfileError) as ctx:
            load_profile("nope")
        self.assertIn("available: steamos-emudeck", str(ctx.exception))
        with self.assertRaises(ProfileError):
            load_profile("../config")

    def assert_invalid(self, data: dict, needle: str) -> None:
        with self.assertRaises(ProfileError) as ctx:
            parse_profile(data, "test")
        self.assertIn(needle, str(ctx.exception))

    def test_validation(self) -> None:
        base = {"name": "p", "paths": {"library": "~/x"}}
        self.assert_invalid({**base, "name": "Bad Name"}, "`name`")
        self.assert_invalid({**base, "extra": 1}, "unknown key 'extra'")
        self.assert_invalid({**base, "paths": {"roms": "~/x"}}, "paths.roms: unknown key")
        self.assert_invalid({**base, "paths": {"library": ""}}, "paths.library")
        self.assert_invalid({**base, "saves": {"retroarch": {"path": "~/x"}}}, "<emu>-<kind>")
        self.assert_invalid({**base, "saves": {"a-b-c": {"path": "~/x"}}}, "<emu>-<kind>")
        self.assert_invalid({**base, "saves": {"a-b": {}}}, "saves.a-b.path")
        self.assert_invalid({**base, "saves": {"a-b": {"path": "~/x", "ignore": "*"}}}, "ignore")
        self.assert_invalid({**base, "saves": {"a-b": {"path": "~/x", "foo": 1}}}, "unknown key")

    def test_file_name_must_match(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "other.toml"
            path.write_text('name = "p"\n')
            with self.assertRaises(ProfileError) as ctx:
                load_profile_file(path)
            self.assertIn("must match the file name", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
