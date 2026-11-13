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
        self.assertEqual(sum(1 for i in specs if i.startswith("wz-save-")), 35)
        self.assertIn("wz-save-ryujinx-user", specs)

    def yarg_cfg(self, *folders: str):
        data = config_data()
        data["devices"]["deck"]["folders"] = list(folders)
        return parse_config(data)

    def test_yarg_data_travels_with_songs_not_saves(self) -> None:
        both = self.by_id(device_specs(self.yarg_cfg("saves", "songs"), "deck", self.profile))
        saves_only = self.by_id(device_specs(self.yarg_cfg("saves"), "deck", self.profile))
        songs_only = self.by_id(device_specs(self.yarg_cfg("songs"), "deck", self.profile))
        self.assertIn("wz-save-yarg-data", both)
        self.assertIn("wz-songs", both)
        self.assertNotIn("wz-save-yarg-data", saves_only)
        self.assertIn("wz-save-ryujinx-user", saves_only)
        self.assertIn("wz-save-yarg-data", songs_only)
        self.assertEqual([i for i in songs_only if i.startswith("wz-save-")], ["wz-save-yarg-data"])
        # The folder keeps its ID, path and type wherever it lands.
        yarg = both["wz-save-yarg-data"]
        self.assertEqual(yarg.path, "~/.config/unity3d/YARC/YARG/release")
        self.assertEqual(yarg.type, "sendreceive")
        self.assertTrue(yarg.skip_if_missing)

    def test_hub_shares_yarg_data_only_with_the_devices_that_list_songs(self) -> None:
        # In the default config the deck has saves without songs; the machine has both.
        hub = self.by_id(hub_specs(self.cfg, self.profiles))
        self.assertEqual(hub["wz-save-yarg-data"].devices, (MACHINE_ID,))
        self.assertEqual(hub["wz-save-yarg-data"].path, "/data/games/saves/yarg/data")
        self.assertEqual(hub["wz-save-ryujinx-user"].devices, (DECK_ID, MACHINE_ID))

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

    def test_videos_stay_on_the_hub(self) -> None:
        # A device that receives the whole media folder ignores each system's `videos`...
        device = self.by_id(device_specs(self.cfg, "deck", self.profile))["wz-media"]
        self.assertEqual(device.ignores, ignores.inline(("*.wz-tmp", "/*/videos")))
        # ...and the hub folder, shared with the other devices, does not.
        hub = self.by_id(hub_specs(self.cfg, self.profiles))["wz-media"]
        self.assertEqual(hub.ignores, ignores.inline(("*.wz-tmp",)))
        # The ES-DE folder next to it is not touched.
        esde = self.by_id(device_specs(self.cfg, "deck", self.profile))["wz-esde"]
        self.assertNotIn("/*/videos", esde.ignores)

    def test_the_temporary_file_of_an_atomic_write_never_syncs(self) -> None:
        # `scrape` writes media and gamelists through `.<name>.wz-tmp`, on the hub and on devices.
        for specs in (
            device_specs(self.cfg, "deck", self.profile),
            hub_specs(self.cfg, self.profiles),
        ):
            by_id = self.by_id(specs)
            for folder in ("wz-media", "wz-esde"):
                ignores_of = by_id[folder].ignores
                self.assertEqual(ignores_of[ignores_of.index("*.wz-tmp")], "*.wz-tmp", folder)
                # Before the whitelist, so the first matching line is the one that ignores it.
                if folder == "wz-esde":
                    self.assertLess(ignores_of.index("*.wz-tmp"), ignores_of.index("!/gamelists"))

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

    def test_flycast_vmus_in_the_system_folder_are_never_received_or_flagged(self) -> None:
        # RetroArch's Flycast core writes the shared VMUs and the console flash into
        # bios/dc, inside the receive-only library. They are per-device data: ignored
        # ahead of the whitelist, and without (?d) so a hub-side delete of the folder
        # cannot take them with it.
        lines = self.by_id(device_specs(self.cfg, "deck", self.profile))["wz-library"].ignores
        include = lines.index("#include patterns-deck.stignore")
        for line in ("/bios/dc/vmu_save_*.bin", "/bios/dc/dc_nvmem.bin"):
            self.assertIn(line, lines)
            self.assertLess(lines.index(line), include)
        library = self.by_id(hub_specs(self.cfg, self.profiles))["wz-library"]
        self.assertNotIn("/bios/dc/dc_nvmem.bin", library.ignores)

    def test_files_cores_write_into_the_system_folder_are_never_received_or_flagged(self) -> None:
        # Seen on a Steam Machine: the Flycast core's pipeline cache and data folder, and
        # mame2003-plus's high score list and artwork folder. They are not on the hub.
        lines = self.by_id(device_specs(self.cfg, "deck", self.profile))["wz-library"].ignores
        include = lines.index("#include patterns-deck.stignore")
        for line in (
            "/bios/dc/vulkan_pipeline.cache",
            "/bios/dc/data",
            "(?d)/bios/mame2003-plus/hiscore.dat",
            "(?d)/bios/mame2003-plus/artwork",
        ):
            self.assertIn(line, lines)
            self.assertLess(lines.index(line), include)

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
        self.assertEqual(cemu.create_when, "~/.config/Cemu")

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

    def settings_cfg(self):
        data = config_data()
        data["devices"]["deck"]["folders"] = ["library", "settings"]
        return parse_config(data)

    def test_settings_folder_is_two_way_with_the_atomic_write_ignore(self) -> None:
        device = self.by_id(device_specs(self.settings_cfg(), "deck", self.profile))["wz-settings"]
        self.assertEqual(device.type, "sendreceive")
        self.assertEqual(device.path, "~/.local/share/warpzone/settings")
        self.assertEqual(device.ignores, ignores.inline(("*.wz-tmp",)))
        self.assertEqual(device.ignores[-1], "*.wz-tmp")
        self.assertEqual(device.versioning, {"type": ""})
        self.assertFalse(device.skip_if_missing)  # warpzone creates the folder
        self.assertIsNone(device.bootstrap_ignores)
        self.assertEqual(device.required_files, ())
        self.assertIsNone(device.rescan_interval_s)

    def test_settings_folder_on_the_hub_keeps_30_days_of_history(self) -> None:
        specs = self.by_id(hub_specs(self.settings_cfg(), self.profiles))
        hub = specs["wz-settings"]
        self.assertEqual(hub.path, "/data/games/settings")
        self.assertEqual(hub.type, "sendreceive")
        self.assertEqual(hub.devices, (DECK_ID,))
        self.assertEqual(hub.ignores, ignores.inline(("*.wz-tmp",)))
        self.assertEqual(
            hub.versioning, {"type": "staggered", "params": {"maxAge": str(30 * 86400)}}
        )

    def test_settings_group_needs_the_profile_path(self) -> None:
        bare = Profile("steamos-emudeck", "", {"library": "~/x"}, {})
        with self.assertRaises(ModelError) as ctx:
            device_specs(self.settings_cfg(), "deck", bare)
        self.assertIn("paths.settings", str(ctx.exception))

    def test_devices_without_the_settings_group_get_no_settings_folder(self) -> None:
        self.assertNotIn("wz-settings", self.by_id(device_specs(self.cfg, "deck", self.profile)))
        self.assertNotIn("wz-settings", self.by_id(hub_specs(self.cfg, self.profiles)))

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
