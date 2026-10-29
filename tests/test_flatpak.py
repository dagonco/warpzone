import unittest
from pathlib import Path

from warpzone import flatpak
from warpzone.flatpak import (
    SYNCTHINGY_APP,
    app_ids,
    granted_apps,
    is_syncthingy_config,
    missing_apps,
    override_command,
    show_command,
)
from warpzone.profiles import load_profile

HOME = Path("/home/x")
RA = "org.libretro.RetroArch"
DOLPHIN = "org.DolphinEmu.dolphin-emu"


def shown(*entries: str) -> str:
    return "[Context]\nfilesystems=" + ";".join(entries) + ";\n"


class AppIdTests(unittest.TestCase):
    def test_finds_the_apps_under_var_app_once_each_in_order(self) -> None:
        paths = [
            f"{HOME}/.var/app/{RA}/config/retroarch/saves",
            f"{HOME}/Emulation/saves/pcsx2/saves",
            f"{HOME}/.var/app/{DOLPHIN}/data/dolphin-emu/GC",
            f"{HOME}/.var/app/{RA}/config/retroarch/states",
        ]
        self.assertEqual(app_ids(paths, HOME), [RA, DOLPHIN])

    def test_syncthingys_own_folder_and_other_homes_are_left_out(self) -> None:
        paths = [f"{HOME}/.var/app/{SYNCTHINGY_APP}/config", "/home/y/.var/app/a.b.C/x"]
        self.assertEqual(app_ids(paths, HOME), [])
        self.assertEqual(app_ids([f"{HOME}/.var/app/"], HOME), [])

    def test_every_flatpak_path_of_the_shipped_profile_is_found(self) -> None:
        profile = load_profile("steamos-emudeck")
        paths = [s.path.replace("~", str(HOME), 1) for s in profile.saves.values()]
        paths += [b.path.replace("~", str(HOME), 1) for b in profile.bios.values()]
        self.assertEqual(
            set(app_ids(paths, HOME)),
            {
                RA,
                DOLPHIN,
                "org.ppsspp.PPSSPP",
                "io.github.shiiion.primehack",
                "org.flycast.Flycast",
            },
        )


class OverrideTests(unittest.TestCase):
    def test_a_writable_grant_is_found_in_any_spelling(self) -> None:
        for entry in (
            f"~/.var/app/{RA}",
            f"~/.var/app/{RA}/",
            f"$HOME/.var/app/{RA}",
            f"{HOME}/.var/app/{RA}",
            f"~/.var/app/{RA}:rw",
            f"~/.var/app/{RA}:create",
        ):
            self.assertEqual(granted_apps(shown("host", entry), HOME), {RA}, entry)

    def test_host_negated_and_read_only_entries_grant_nothing(self) -> None:
        self.assertEqual(granted_apps(shown("host", "home", "xdg-config/foo"), HOME), set())
        self.assertEqual(granted_apps(shown(f"!~/.var/app/{RA}"), HOME), set())
        self.assertEqual(granted_apps(shown(f"~/.var/app/{RA}:ro"), HOME), set())
        self.assertEqual(granted_apps("", HOME), set())
        self.assertEqual(granted_apps("[Context]\nshared=network;\n", HOME), set())

    def test_missing_keeps_the_order_and_skips_the_granted(self) -> None:
        out = shown(f"~/.var/app/{DOLPHIN}")
        self.assertEqual(missing_apps([RA, DOLPHIN, "a.b.C"], out, HOME), [RA, "a.b.C"])
        self.assertEqual(missing_apps([DOLPHIN], out, HOME), [])

    def test_commands(self) -> None:
        self.assertEqual(
            show_command(), ["flatpak", "override", "--user", "--show", SYNCTHINGY_APP]
        )
        self.assertEqual(
            override_command([RA, DOLPHIN]),
            [
                "flatpak",
                "override",
                "--user",
                f"--filesystem=~/.var/app/{RA}",
                f"--filesystem=~/.var/app/{DOLPHIN}",
                SYNCTHINGY_APP,
            ],
        )


class ConfigLocationTests(unittest.TestCase):
    def test_only_the_flatpaks_own_folder_counts(self) -> None:
        inside = HOME / ".var/app" / SYNCTHINGY_APP / "config/syncthing/config.xml"
        self.assertTrue(is_syncthingy_config(inside, HOME))
        self.assertFalse(is_syncthingy_config(HOME / ".local/state/syncthing/config.xml", HOME))
        other = HOME / ".var/app" / RA / "config.xml"
        self.assertFalse(is_syncthingy_config(other, HOME))

    def test_the_app_id_is_defined_once(self) -> None:
        root = Path(flatpak.__file__).parent
        users = [p.name for p in root.glob("*.py") if SYNCTHINGY_APP in p.read_text()]
        self.assertEqual(users, ["flatpak.py"])


if __name__ == "__main__":
    unittest.main()
