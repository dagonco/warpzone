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
        for name in ("settings.json", "scores", "profiles", "playlists", "custom"):
            self.assertTrue(_whitelisted(yarg.ignore, name), name)
        # YARG on Linux writes `custom/` in lowercase.
        self.assertFalse(_whitelisted(yarg.ignore, "Custom"))
        for name in ("songcache.bin", "badsongs.txt", "logs", "currentSong.txt"):
            self.assertFalse(_whitelisted(yarg.ignore, name), name)

    def test_yarg_ignores_the_bak_files_before_the_whitelist(self) -> None:
        ignore = load_profile("steamos-emudeck").saves["yarg-data"].ignore
        bak = ignore.index("(?d)/profiles/*.bak")
        self.assertLess(bak, ignore.index("!/profiles"))  # the first match wins

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

    def test_library_ignores_emudeck_own_files(self) -> None:
        ignore = load_profile("steamos-emudeck").library_ignore
        for line in (
            "/bios/PPSSPP",
            "(?d)/bios/**/readme.txt",
            "(?d)/roms/*/systeminfo.txt",
            "(?d)/roms/*/metadata.txt",
        ):
            self.assertIn(line, ignore)
        for folder in ("HdPacks", "Mupen64plus", "fbneo", "kronos", "same_cdi", "ryujinx"):
            self.assertIn(f"(?d)/bios/{folder}", ignore)
        # A hub may deliver BIOS files into these two.
        self.assertNotIn("(?d)/bios/mame", ignore)
        self.assertNotIn("(?d)/bios/neocd", ignore)
        # Never a whole folder that holds games.
        self.assertNotIn("/roms", ignore)

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
        deletable = parse_profile({**base, "library": {"ignore": ["(?d)/y/*.txt"]}}, "t")
        self.assertEqual(deletable.library_ignore, ("(?d)/y/*.txt",))
        self.assert_invalid({**base, "library": {"ignore": ["(?d)roms"]}}, "library.ignore")
        self.assert_invalid({**base, "library": {"ignore": ["roms"]}}, "library.ignore")
        self.assert_invalid({**base, "library": {"ignore": "/roms"}}, "library.ignore")
        self.assert_invalid({**base, "library": {"x": 1}}, "library: unknown key")
        self.assert_invalid({**base, "library": []}, "`library` must be a table")

    def test_settings_default_to_empty_and_the_path_key_is_known(self) -> None:
        profile = load_profile("steamos-emudeck")
        self.assertEqual(profile.paths["settings"], "~/.local/share/warpzone/settings")
        self.assertEqual(parse_profile({"name": "p"}, "t").settings, {})

    def test_valid_settings_table(self) -> None:
        parsed = parse_profile(
            {
                "name": "p",
                "settings": {
                    "emu-1": {
                        "process": ["emu", "emu-bin"],
                        "verify": True,
                        "files": {
                            "main": {
                                "path": "~/.config/emu/main.ini",
                                "format": "qt-ini",
                                "noise": ["[A]*"],
                                "secret": ["*_token"],
                                "device": ["[B]size"],
                                "known": ["[C]lang"],
                            },
                            "opt": {
                                "path": "~/.config/emu/{name}/{name}.opt",
                                "format": "retroarch-cfg",
                                "create": True,
                            },
                        },
                        "data": {
                            "eeprom": {"path": "~/d/eeprom.bin", "create_when": "~/d"},
                            "plain": {"path": "~/d/plain.bin"},
                        },
                    },
                    "minimal": {"process": ["m"]},
                },
            },
            "t",
        )
        emu = parsed.settings["emu-1"]
        self.assertEqual((emu.id, emu.process, emu.verify), ("emu-1", ("emu", "emu-bin"), True))
        main, opt = emu.files["main"], emu.files["opt"]
        self.assertEqual((main.id, main.format, main.create), ("main", "qt-ini", False))
        self.assertEqual(main.noise, ("[A]*",))
        self.assertEqual(main.secret, ("*_token",))
        self.assertEqual(main.device, ("[B]size",))
        self.assertEqual(main.known, ("[C]lang",))
        self.assertFalse(main.templated)
        self.assertTrue(opt.templated)
        self.assertTrue(opt.create)
        self.assertEqual(opt.noise, ())
        self.assertEqual(emu.data["eeprom"].create_when, "~/d")
        self.assertIsNone(emu.data["plain"].create_when)
        minimal = parsed.settings["minimal"]
        self.assertEqual((minimal.files, minimal.data, minimal.verify), ({}, {}, False))

    def test_settings_validation(self) -> None:
        def prof(emu: dict | None = None, **top: object) -> dict:
            return {
                "name": "p",
                "settings": {"e": emu if emu is not None else {"process": ["e"]}},
                **top,
            }

        def with_file(**file: object) -> dict:
            base = {"path": "~/f.ini", "format": "ini"}
            return prof({"process": ["e"], "files": {"f": {**base, **file}}})

        def with_data(**data: object) -> dict:
            return prof({"process": ["e"], "data": {"d": {"path": "~/d", **data}}})

        self.assert_invalid({"name": "p", "settings": []}, "`settings` must be a table")
        self.assert_invalid(
            prof({"process": ["e"]}) | {"settings": {"E": {"process": ["e"]}}}, "emulator id"
        )
        self.assert_invalid(
            prof({"process": ["e"]}) | {"settings": {"a_b": {"process": ["e"]}}}, "emulator id"
        )
        self.assert_invalid({"name": "p", "settings": {"e": []}}, "settings.e: must be a table")
        self.assert_invalid(prof({"process": ["e"], "foo": 1}), "settings.e: unknown key 'foo'")
        for process in (None, [], "e", [""], [1], ["e", ""]):
            bad = {} if process is None else {"process": process}
            self.assert_invalid(prof(bad), "settings.e.process")
        self.assert_invalid(prof({"process": ["e"], "verify": "yes"}), "settings.e.verify")
        self.assert_invalid(prof({"process": ["e"], "files": []}), "settings.e.files")
        self.assert_invalid(prof({"process": ["e"], "files": {"F": {}}}), "file id")
        self.assert_invalid(prof({"process": ["e"], "files": {"a:b": {}}}), "file id")
        self.assert_invalid(
            prof({"process": ["e"], "files": {"data": {"path": "~/f.ini", "format": "ini"}}}),
            "file id 'data' is reserved",
        )
        self.assert_invalid(
            prof({"process": ["e"], "files": {"f": []}}), "settings.e.files.f: must be a table"
        )
        self.assert_invalid(with_file(foo=1), "settings.e.files.f: unknown key 'foo'")
        self.assert_invalid(with_file(path=""), "settings.e.files.f.path")
        self.assert_invalid(with_file(path=3), "settings.e.files.f.path")
        for fmt in ("yaml", "INI", "", None, 3):
            self.assert_invalid(with_file(format=fmt), "settings.e.files.f.format")
        for fmt in ("ini", "qt-ini", "retroarch-cfg", "yaml-flat", "json", "toml"):
            parse_profile(with_file(format=fmt), "t")
        self.assert_invalid(with_file(create="yes"), "settings.e.files.f.create")
        for name in ("noise", "secret", "device", "known"):
            self.assert_invalid(with_file(**{name: "x"}), f"settings.e.files.f.{name}")
            self.assert_invalid(with_file(**{name: ["x", 1]}), f"settings.e.files.f.{name}")
        # A templated path needs {name} in its directory part, and only that placeholder.
        self.assert_invalid(with_file(path="~/c/{name}.ini"), "directory part")
        self.assert_invalid(with_file(path="{name}"), "directory part")
        self.assert_invalid(with_file(path="~/{other}/f.ini"), "only placeholder")
        self.assert_invalid(with_file(path="~/{name}/{x}.ini"), "only placeholder")
        self.assert_invalid(with_file(path="~/{name/f.ini"), "only placeholder")
        parse_profile(with_file(path="~/c/{name}/f.ini"), "t")
        parse_profile(with_file(path="~/c/{name}/{name}.ini"), "t")
        self.assert_invalid(prof({"process": ["e"], "data": []}), "settings.e.data")
        self.assert_invalid(prof({"process": ["e"], "data": {"D": {}}}), "data id")
        self.assert_invalid(
            prof({"process": ["e"], "data": {"d": []}}), "settings.e.data.d: must be a table"
        )
        self.assert_invalid(with_data(foo=1), "settings.e.data.d: unknown key 'foo'")
        self.assert_invalid(with_data(path=""), "settings.e.data.d.path")
        self.assert_invalid(with_data(path="~/{name}/d.bin"), "cannot be templated")
        self.assert_invalid(with_data(create_when=""), "settings.e.data.d.create_when")
        self.assert_invalid(with_data(create_when=3), "settings.e.data.d.create_when")
        parse_profile(with_data(create_when="~/x"), "t")

    def test_file_name_must_match(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "other.toml"
            path.write_text('name = "p"\n')
            with self.assertRaises(ProfileError) as ctx:
                load_profile_file(path)
            self.assertIn("must match the file name", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
