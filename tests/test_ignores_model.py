import dataclasses
import unittest
from pathlib import Path

from support import DECK_ID, HUB_ID, MACHINE_ID, config_data, make_config
from warpzone import ignores
from warpzone.config import parse_config
from warpzone.model import ModelError, device_specs, hub_specs
from warpzone.profiles import Profile, load_profile

ROOT = Path(__file__).parent.parent


def pattern_lines(path: Path) -> tuple[str, ...]:
    lines = path.read_text().splitlines()
    return tuple(line for line in lines if line and not line.startswith("//"))


class IgnoreTests(unittest.TestCase):
    def test_template_matches_constants(self) -> None:
        template = pattern_lines(ROOT / "templates" / "patterns-common.stignore")
        self.assertEqual(template, ("!/patterns-*.stignore", *ignores.COMMON_LINES))

    def test_device_template_includes_common(self) -> None:
        lines = pattern_lines(ROOT / "templates" / "patterns-device.example.stignore")
        self.assertEqual(lines[0], f"#include {ignores.COMMON_FILE}")
        self.assertEqual(lines[-1], "*")

    def test_rendering(self) -> None:
        self.assertEqual(ignores.device_file("deck"), "patterns-deck.stignore")
        self.assertEqual(ignores.device_whitelist("deck"), ("#include patterns-deck.stignore",))
        self.assertEqual(ignores.hub_whitelist(), ("#include patterns-common.stignore",))
        self.assertEqual(ignores.BOOTSTRAP_LINES, ("!/patterns-*.stignore", "*"))
        self.assertEqual(ignores.inline(("*",))[-1], "*")
        self.assertEqual(
            ignores.device_whitelist("deck", ("/bios/x",)),
            ("/bios/x", "#include patterns-deck.stignore"),
        )
        self.assertEqual(
            ignores.hub_whitelist(("/bios/x",)), ("/bios/x", "#include patterns-common.stignore")
        )
        self.assertEqual(ignores.inline()[: len(ignores.COMMON_LINES)], ignores.COMMON_LINES)


class ModelTests(unittest.TestCase):
    def setUp(self) -> None:
        self.cfg = make_config()
        self.profile = load_profile("steamos-emudeck")
        self.profiles = {"steamos-emudeck": self.profile}

    def by_id(self, specs):
        return {s.id: s for s in specs}

    def test_device_folders_expand_groups(self) -> None:
        specs = self.by_id(device_specs(self.cfg, "deck", self.profile))
        self.assertIn("wz-library", specs)
        self.assertIn("wz-tools", specs)
        self.assertIn("wz-media", specs)
        self.assertIn("wz-esde", specs)
        self.assertNotIn("wz-songs", specs)
        self.assertEqual(sum(1 for i in specs if i.startswith("wz-save-")), 36)
        self.assertIn("wz-save-ryujinx-user", specs)

    def test_device_folder_properties(self) -> None:
        specs = self.by_id(device_specs(self.cfg, "deck", self.profile))
        library = specs["wz-library"]
        self.assertEqual(library.type, "receiveonly")
        self.assertEqual(library.path, "~/Emulation")
        self.assertEqual(library.devices, (HUB_ID,))
        # Exclusions first (the first match wins), then the hub's whitelist.
        self.assertEqual(library.ignores[-1], "#include patterns-deck.stignore")
        self.assertEqual(library.ignores[:-1], self.profile.library_excludes())
        self.assertEqual(library.bootstrap_ignores, ("!/patterns-*.stignore", "*"))
        self.assertEqual(
            library.required_files, ("patterns-common.stignore", "patterns-deck.stignore")
        )
        self.assertEqual(library.versioning, {"type": ""})
        self.assertEqual(library.rescan_interval_s, 300)
        self.assertEqual(library.body()["rescanIntervalS"], 300)
        self.assertIsNone(specs["wz-tools"].rescan_interval_s)
        self.assertNotIn("rescanIntervalS", specs["wz-tools"].body())
        self.assertEqual(specs["wz-tools"].ignores, ignores.inline(("/logs",)))
        self.assertIsNone(specs["wz-tools"].bootstrap_ignores)
        self.assertEqual(specs["wz-media"].type, "sendreceive")
        self.assertEqual(specs["wz-esde"].ignores[-3:], ("!/gamelists", "!/collections", "*"))
        save = specs["wz-save-ryujinx-user"]
        self.assertEqual(save.type, "sendreceive")
        self.assertTrue(save.skip_if_missing)
        self.assertEqual(save.ignores[-3:], ("!/save", "!/saveMeta", "*"))
        self.assertEqual(save.versioning, {"type": ""})

    def test_hub_folders(self) -> None:
        specs = self.by_id(hub_specs(self.cfg, self.profiles))
        library = specs["wz-library"]
        self.assertEqual(library.path, "/data/games/library")
        self.assertEqual(library.type, "sendonly")
        self.assertEqual(library.devices, (DECK_ID, MACHINE_ID))
        self.assertEqual(
            library.ignores,
            (*self.profile.placement_ignores(), "#include patterns-common.stignore"),
        )
        self.assertEqual(
            library.required_files,
            ("patterns-common.stignore", "patterns-deck.stignore", "patterns-machine.stignore"),
        )
        self.assertEqual(specs["wz-songs"].devices, (MACHINE_ID,))
        self.assertEqual(specs["wz-tools"].devices, (DECK_ID,))
        self.assertEqual(specs["wz-media"].path, "/data/games/frontend/media")
        self.assertEqual(specs["wz-esde"].path, "/data/games/frontend/es-de")
        save = specs["wz-save-dolphin-gc"]
        self.assertEqual(save.path, "/data/games/saves/dolphin/gc")
        self.assertEqual(save.devices, (DECK_ID, MACHINE_ID))
        self.assertEqual(save.type, "sendreceive")
        self.assertEqual(
            save.versioning, {"type": "staggered", "params": {"maxAge": str(30 * 86400)}}
        )
        self.assertEqual(specs["wz-library"].versioning, {"type": ""})
        self.assertIsNone(specs["wz-library"].rescan_interval_s)  # hub needs no bound
        self.assertEqual(specs["wz-media"].versioning, {"type": ""})

    def test_library_ignores_symlinks_and_per_device_data_on_the_device(self) -> None:
        lines = self.by_id(device_specs(self.cfg, "deck", self.profile))["wz-library"].ignores
        # Cemu's install and saves are per-device data.
        self.assertIn("/roms/wiiu/mlc01", lines)
        # Every symlink EmuDeck creates inside bios/, whether or not a placement covers it.
        for link in (
            "/bios/eden/keys",
            "/bios/eden/firmware",
            "/bios/azahar",
            "/bios/ryujinx/keys",
            "/bios/flycast/bios",
            "/bios/shadps4/sys_modules",
        ):
            self.assertIn(link, lines)
        self.assertEqual(lines[-1], "#include patterns-deck.stignore")
        self.assertEqual(len(lines), len(set(lines)))

    def test_hub_library_ignores_what_placements_deliver(self) -> None:
        library = self.by_id(hub_specs(self.cfg, self.profiles))["wz-library"]
        self.assertEqual(
            library.ignores,
            (
                "/bios/eden/keys",
                "/bios/eden/firmware",
                "/bios/azahar",
                "/bios/cemu",
                "#include patterns-common.stignore",
            ),
        )
        # Per-device data is not a hub concern: the hub still holds and sends it.
        self.assertNotIn("/roms/wiiu/mlc01", library.ignores)
        # Songs has no exclusions.
        self.assertEqual(
            self.by_id(hub_specs(self.cfg, self.profiles))["wz-songs"].ignores,
            ("#include patterns-common.stignore",),
        )

    def bios_cfg(self):
        data = config_data()
        data["devices"]["deck"]["folders"] = ["library", "bios"]
        return parse_config(data)

    def test_bios_placements_are_receive_only_folders_into_the_real_directory(self) -> None:
        specs = self.by_id(device_specs(self.bios_cfg(), "deck", self.profile))
        self.assertEqual(
            sorted(i for i in specs if i.startswith("wz-bios-")),
            [
                "wz-bios-azahar-keys",
                "wz-bios-cemu-keys",
                "wz-bios-eden-firmware",
                "wz-bios-eden-keys",
            ],
        )
        cemu = specs["wz-bios-cemu-keys"]
        self.assertEqual(cemu.path, "~/.local/share/Cemu")
        self.assertEqual(cemu.type, "receiveonly")
        self.assertEqual(cemu.devices, (HUB_ID,))
        self.assertEqual(cemu.versioning, {"type": ""})
        self.assertTrue(cemu.skip_if_missing)
        self.assertIsNone(cemu.bootstrap_ignores)
        self.assertEqual(cemu.ignores[-2:], ("!/keys.txt", "*"))
        self.assertEqual(
            specs["wz-bios-eden-keys"].ignores[-3:], ("!/prod.keys", "!/title.keys", "*")
        )
        self.assertEqual(specs["wz-bios-azahar-keys"].ignores[-2:], ("!/aes_keys.txt", "*"))
        self.assertEqual(specs["wz-bios-eden-keys"].create_when, "~/.local/share/eden")
        self.assertIsNone(cemu.create_when)

    def test_bios_placements_on_the_hub(self) -> None:
        specs = self.by_id(hub_specs(self.bios_cfg(), self.profiles))
        cemu = specs["wz-bios-cemu-keys"]
        self.assertEqual(cemu.path, "/data/games/library/bios/cemu")
        self.assertEqual(cemu.type, "sendonly")
        self.assertEqual(cemu.devices, (DECK_ID,))  # only the device that opted in
        self.assertEqual(cemu.ignores[-2:], ("!/keys.txt", "*"))  # the hub never offers more
        self.assertEqual(
            specs["wz-bios-eden-firmware"].path, "/data/games/library/bios/eden/firmware"
        )

    def test_devices_without_the_bios_group_get_no_placements(self) -> None:
        specs = device_specs(self.cfg, "deck", self.profile)
        self.assertFalse([s for s in specs if s.id.startswith("wz-bios-")])
        hub = hub_specs(self.cfg, self.profiles)
        self.assertFalse([s for s in hub if s.id.startswith("wz-bios-")])

    def test_hub_root_slash(self) -> None:
        cfg = dataclasses.replace(self.cfg, hub=dataclasses.replace(self.cfg.hub, root="/"))
        self.assertEqual(self.by_id(hub_specs(cfg, self.profiles))["wz-library"].path, "/library")

    def test_folder_body(self) -> None:
        body = self.by_id(hub_specs(self.cfg, self.profiles))["wz-songs"].body()
        self.assertEqual(body["id"], "wz-songs")
        self.assertEqual(body["devices"], [{"deviceID": MACHINE_ID}])
        self.assertTrue(body["fsWatcherEnabled"])

    def test_profile_without_needed_path(self) -> None:
        bare = Profile("steamos-emudeck", "", {"library": "~/x"}, {})
        with self.assertRaises(ModelError) as ctx:
            device_specs(self.cfg, "deck", bare)
        self.assertIn("paths.tools", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
