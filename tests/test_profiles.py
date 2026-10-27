import fnmatch
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


def _whitelisted(patterns: tuple[str, ...], name: str) -> bool:
    """First-match-wins evaluation of the simple anchored patterns the profile uses."""
    for pattern in patterns:
        keep = pattern.startswith("!")
        glob = pattern.removeprefix("!").removeprefix("/")
        if fnmatch.fnmatchcase(name, glob):
            return keep
    return True


class ProfileTests(unittest.TestCase):
    def test_shipped_profile_loads(self) -> None:
        profile = load_profile("steamos-emudeck")
        self.assertEqual(profile.paths["library"], "~/Emulation")
        self.assertEqual(len(profile.saves), 36)
        ryujinx = profile.saves["ryujinx-user"]
        self.assertEqual((ryujinx.emu, ryujinx.kind), ("ryujinx", "user"))
        self.assertEqual(ryujinx.ignore, ("!/save", "!/saveMeta", "*"))
        self.assertEqual(profile.saves["dolphin-gc"].ignore, ())

    def test_shipped_save_entries_are_correct(self) -> None:
        saves = load_profile("steamos-emudeck").saves
        # Real directories, never EmuDeck's symlinks under ~/Emulation/saves/<emu>/<kind>.
        for key in (
            "ppsspp-saves",
            "ppsspp-states",
            "rpcs3-saves",
            "rpcs3-trophy",
            "vita3k-saves",
            "mame-saves",
            "mame-states",
            "rmg-saves",
            "rmg-states",
            "scummvm-saves",
            "primehack-gc",
            "primehack-wii",
            "primehack-states",
            "eden-profiles",
            "ryujinx-profiles",
            "xenia-native",
            "flycast-saves",
            "yarg-data",
        ):
            self.assertIn(key, saves)
        self.assertEqual(
            saves["eden-profiles"].path,
            "~/Emulation/storage/eden/nand/system/save/8000000000000010",
        )
        self.assertEqual(saves["ryujinx-profiles"].path, "~/.config/Ryujinx/bis/system/save")
        self.assertEqual(saves["xenia-content"].path, "~/Emulation/roms/xbox360/content")
        self.assertEqual(saves["xenia-native"].path, "~/.local/share/Xenia/content")

    def test_flycast_syncs_only_saves_never_the_bios(self) -> None:
        flycast = load_profile("steamos-emudeck").saves["flycast-saves"]
        self.assertEqual(flycast.path, "~/.var/app/org.flycast.Flycast/data/flycast")
        self.assertEqual(flycast.ignore[-1], "*")  # whitelist: everything else stays out
        for name in ("vmu_save_A1.bin", "SLUS_1_vmu_save_A1.bin", "dc_nvmem.bin", "g.nvmem"):
            self.assertTrue(_whitelisted(flycast.ignore, name), name)
        for name in ("dc_boot.bin", "dc_flash.bin", "mappings.cfg"):
            self.assertFalse(_whitelisted(flycast.ignore, name), name)

    def test_yarg_never_syncs_the_song_cache(self) -> None:
        yarg = load_profile("steamos-emudeck").saves["yarg-data"]
        self.assertEqual(yarg.path, "~/.config/unity3d/YARC/YARG/release")
        self.assertEqual(yarg.ignore[-1], "*")
        for name in ("settings.json", "scores", "profiles", "playlists", "Custom"):
            self.assertTrue(_whitelisted(yarg.ignore, name), name)
        for name in ("songcache.bin", "badsongs.txt", "logs", "currentSong.txt"):
            self.assertFalse(_whitelisted(yarg.ignore, name), name)

    def test_shipped_bios_placements(self) -> None:
        profile = load_profile("steamos-emudeck")
        self.assertEqual(
            {k: (p.hub, p.path) for k, p in profile.bios.items()},
            {
                "eden-keys": ("bios/eden/keys", "~/.local/share/eden/keys"),
                "eden-firmware": (
                    "bios/eden/firmware",
                    "~/Emulation/storage/eden/nand/system/Contents/registered",
                ),
                "azahar-keys": ("bios/azahar", "~/.local/share/azahar-emu/sysdata"),
                "cemu-keys": ("bios/cemu", "~/.local/share/Cemu"),
            },
        )
        self.assertEqual(profile.bios["cemu-keys"].ignore, ("!/keys.txt", "*"))
        self.assertEqual(profile.bios["azahar-keys"].ignore, ("!/aes_keys.txt", "*"))
        # Keys go nowhere but the exact files.
        for name in ("keys.txt", "prod.keys", "title.keys", "aes_keys.txt"):
            self.assertTrue(any(_whitelisted(p.ignore, name) for p in profile.bios.values()), name)
        self.assertFalse(_whitelisted(profile.bios["eden-keys"].ignore, "keys.txt"))
        self.assertFalse(_whitelisted(profile.bios["cemu-keys"].ignore, "prod.keys"))
        self.assertTrue(profile.bios["eden-firmware"].verify)
        self.assertFalse(profile.bios["cemu-keys"].verify)

    def test_library_ignore(self) -> None:
        profile = load_profile("steamos-emudeck")
        self.assertIn("/roms/wiiu/mlc01", profile.library_ignore)
        for link in ("ryujinx/keys", "flycast/bios", "shadps4/sys_modules"):
            self.assertIn(f"/bios/{link}", profile.library_ignore)
        # Placement folders are excluded as well, and nothing twice.
        excludes = profile.library_excludes()
        for hub in ("/bios/eden/keys", "/bios/eden/firmware", "/bios/azahar", "/bios/cemu"):
            self.assertIn(hub, excludes)
        self.assertEqual(len(excludes), len(set(excludes)))

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

    def test_bios_and_library_validation(self) -> None:
        base = {"name": "p", "paths": {"library": "~/x"}}
        good = {"hub": "bios/x", "path": "~/x"}
        parsed = parse_profile({**base, "bios": {"a-b": good}, "library": {"ignore": ["/y"]}}, "t")
        self.assertEqual(parsed.bios["a-b"].hub, "bios/x")
        self.assertEqual(parsed.library_excludes(), ("/y", "/bios/x"))
        self.assert_invalid({**base, "bios": {"ab": good}}, "<emu>-<kind>")
        self.assert_invalid({**base, "bios": {"a-b": {"path": "~/x"}}}, "bios.a-b.hub")
        self.assert_invalid({**base, "bios": {"a-b": {"hub": "bios/x"}}}, "bios.a-b.path")
        for hub in ("/bios/x", "bios/../x", "bios/*", "bios//x", ""):
            self.assert_invalid({**base, "bios": {"a-b": {**good, "hub": hub}}}, "bios.a-b.hub")
        self.assert_invalid({**base, "bios": {"a-b": {**good, "foo": 1}}}, "unknown key")
        self.assert_invalid({**base, "bios": {"a-b": {**good, "ignore": "*"}}}, "bios.a-b.ignore")
        self.assert_invalid({**base, "bios": {"a-b": {**good, "verify": "yes"}}}, "verify")
        self.assert_invalid({**base, "bios": {"a-b": {**good, "create_when": ""}}}, "create_when")
        self.assert_invalid({**base, "bios": []}, "`bios` must be a table")
        # A placement inside another would make two folders own the same files.
        self.assert_invalid(
            {
                **base,
                "bios": {"a-b": good, "a-c": {"hub": "bios/x/y", "path": "~/y"}},
            },
            "overlaps",
        )
        # Siblings whose names share a prefix are fine.
        parse_profile(
            {**base, "bios": {"a-b": good, "a-c": {"hub": "bios/x-2", "path": "~/y"}}}, "t"
        )
        self.assert_invalid({**base, "library": {"ignore": ["roms"]}}, "library.ignore")
        self.assert_invalid({**base, "library": {"ignore": "/roms"}}, "library.ignore")
        self.assert_invalid({**base, "library": {"x": 1}}, "library: unknown key")
        self.assert_invalid({**base, "library": []}, "`library` must be a table")

    def test_file_name_must_match(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "other.toml"
            path.write_text('name = "p"\n')
            with self.assertRaises(ProfileError) as ctx:
                load_profile_file(path)
            self.assertIn("must match the file name", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
