"""The `anbernic-rg351mp` profile: shipped data, folder specs, provisioning and doctor."""

import dataclasses
import tempfile
import unittest
from pathlib import Path
from unittest import addModuleCleanup, mock

from support import API_KEY, HUB_ID, FakeSyncthing, config_data
from test_doctor import by_name, context
from test_provision import reporter
from warpzone import frontend
from warpzone import provision as provision_mod
from warpzone.config import parse_config
from warpzone.doctor import CHECKS, OK, WARN, Context, check_frontend
from warpzone.model import ModelError, device_specs
from warpzone.profiles import ProfileError, load_profile, parse_profile
from warpzone.provision import provision_device
from warpzone.syncthing import Client


def setUpModule() -> None:
    """provision_device would install a systemd timer: tests/test_update_timer.py covers that."""
    patcher = mock.patch.object(provision_mod, "ensure_update_timer")
    patcher.start()
    addModuleCleanup(patcher.stop)


ARK_ID = "DDDDDDD-DDDDDDD-DDDDDDD-DDDDDDD-DDDDDDD-DDDDDDD-DDDDDDD-DDDDDDD"
PROFILE = load_profile("anbernic-rg351mp")
SOURCE = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
    "<systemList>\n"
    "\t<system>\n\t\t<name>nes</name>\n\t\t<path>/roms/nes/</path>\n\t</system>\n"
    "\t<system>\n\t\t<name>amiga</name>\n\t\t<path>/roms/amiga</path>\n\t</system>\n"
    "\t<system>\n\t\t<name>cmds</name>\n\t\t<path>/opt/cmds/</path>\n\t</system>\n"
    "</systemList>\n"
)
RETROARCH = (
    'menu_driver = "rgui"\n'
    'savefile_directory = "~/.config/retroarch/saves"\n'
    'savefiles_in_content_dir = "true"\n'
    'savestate_directory = "~/.config/retroarch/states"\n'
    'savestates_in_content_dir = "true"\n'
    'system_directory = "/roms/bios"\n'
)


def ark_config(folders=("library", "saves")):
    data = config_data()
    data["devices"] = {
        "ark": {"device_id": ARK_ID, "profile": "anbernic-rg351mp", "folders": list(folders)}
    }
    return parse_config(data)


class ProfileTests(unittest.TestCase):
    def test_the_profile_is_shipped_under_its_exact_name(self) -> None:
        self.assertEqual(PROFILE.name, "anbernic-rg351mp")
        self.assertEqual(PROFILE.paths["library"], "/roms/library")

    def test_only_the_folders_this_device_has_are_defined(self) -> None:
        for key in ("songs", "tools", "media", "esde", "settings"):
            self.assertNotIn(key, PROFILE.paths)
        self.assertEqual(PROFILE.settings, {})
        self.assertEqual(PROFILE.bios, {})

    def test_folders_without_a_path_are_refused_by_name(self) -> None:
        for group in ("songs", "tools", "frontend", "settings"):
            with self.subTest(group=group):
                cfg = ark_config(("library", group))
                with self.assertRaisesRegex(ModelError, group):
                    device_specs(cfg, "ark", PROFILE)

    def test_the_library_is_on_a_filesystem_without_permissions(self) -> None:
        self.assertTrue(PROFILE.ignore_perms)

    def test_nothing_relies_on_symlinks(self) -> None:
        # exFAT has none: saves are real directories on the root filesystem, outside the
        # library, and no library exclusion names an EmuDeck link.
        for save in PROFILE.saves.values():
            self.assertTrue(save.path.startswith("~/.config/retroarch/"), save.path)
        self.assertEqual(PROFILE.library_ignore, ("(?d)/roms/*/gamelist.xml",))

    def test_retroarch_saves_are_the_directories_retroarch_writes_to(self) -> None:
        wants = PROFILE.retroarch.wants(PROFILE.paths["library"])
        self.assertEqual(wants["savefile_directory"], PROFILE.saves["retroarch-saves"].path)
        self.assertEqual(wants["savestate_directory"], PROFILE.saves["retroarch-states"].path)
        self.assertEqual(wants["savefiles_in_content_dir"], "false")
        self.assertEqual(wants["savestates_in_content_dir"], "false")
        self.assertEqual(wants["system_directory"], "/roms/library/bios")

    def test_both_retroarch_builds_are_configured(self) -> None:
        self.assertEqual(
            PROFILE.retroarch.cfg,
            ("~/.config/retroarch/retroarch.cfg", "~/.config/retroarch32/retroarch.cfg"),
        )

    def test_the_hub_save_folders_are_the_ones_of_the_steam_devices(self) -> None:
        deck = load_profile("steamos-emudeck")
        for key in ("retroarch-saves", "retroarch-states"):
            self.assertEqual(PROFILE.saves[key].emu, deck.saves[key].emu)
            self.assertEqual(PROFILE.saves[key].kind, deck.saves[key].kind)
            self.assertEqual(PROFILE.saves[key].ignore, deck.saves[key].ignore)


class ParseTests(unittest.TestCase):
    def parse(self, **extra):
        return parse_profile({"name": "x", "paths": {"library": "/l"}, **extra}, "t")

    def test_ignore_perms_must_be_a_boolean(self) -> None:
        with self.assertRaisesRegex(ProfileError, "ignore_perms"):
            self.parse(ignore_perms="yes")

    def test_es_systems_needs_a_library_and_known_keys(self) -> None:
        good = {"source": "/a", "override": "~/b", "prefix": "/roms/", "process": ["es"]}
        self.assertEqual(self.parse(es_systems=good).es_systems.prefix, "/roms/")
        with self.assertRaisesRegex(ProfileError, "es_systems.extra"):
            self.parse(es_systems={**good, "extra": 1})
        with self.assertRaisesRegex(ProfileError, "es_systems.prefix"):
            self.parse(es_systems={**good, "prefix": "roms"})
        with self.assertRaisesRegex(ProfileError, "es_systems.process"):
            self.parse(es_systems={**good, "process": []})
        with self.assertRaisesRegex(ProfileError, "paths.library"):
            parse_profile({"name": "x", "es_systems": good}, "t")

    def test_retroarch_values_may_only_use_the_library_placeholder(self) -> None:
        good = {"cfg": ["~/a.cfg"], "process": ["retroarch"], "set": {"k": "{library}/bios"}}
        self.assertEqual(self.parse(retroarch=good).retroarch.set["k"], "{library}/bios")
        with self.assertRaisesRegex(ProfileError, "retroarch.set.k"):
            self.parse(retroarch={**good, "set": {"k": "{nope}"}})
        with self.assertRaisesRegex(ProfileError, "retroarch.set.k"):
            self.parse(retroarch={**good, "set": {"k": 1}})
        with self.assertRaisesRegex(ProfileError, "retroarch.cfg"):
            self.parse(retroarch={**good, "cfg": []})

    def test_a_save_folder_may_name_the_directory_that_marks_its_emulator(self) -> None:
        saves = {"ra-saves": {"path": "~/x/saves", "create_when": "~/x"}}
        self.assertEqual(self.parse(saves=saves).saves["ra-saves"].create_when, "~/x")
        with self.assertRaisesRegex(ProfileError, "create_when"):
            self.parse(saves={"ra-saves": {"path": "~/x", "create_when": ""}})


class ModelTests(unittest.TestCase):
    def test_every_device_folder_ignores_permissions(self) -> None:
        specs = device_specs(ark_config(), "ark", PROFILE)
        self.assertEqual(
            [s.id for s in specs],
            ["wz-library", "wz-save-retroarch-saves", "wz-save-retroarch-states"],
        )
        for spec in specs:
            self.assertTrue(spec.ignore_perms, spec.id)
            self.assertIs(spec.body()["ignorePerms"], True)

    def test_the_save_folders_are_created_once_retroarch_has_a_config_folder(self) -> None:
        specs = {s.id: s for s in device_specs(ark_config(), "ark", PROFILE)}
        for fid in ("wz-save-retroarch-saves", "wz-save-retroarch-states"):
            self.assertTrue(specs[fid].skip_if_missing)
            self.assertEqual(specs[fid].create_when, "~/.config/retroarch")

    def test_other_profiles_do_not_get_the_key(self) -> None:
        data = config_data()
        cfg = parse_config(data)
        for spec in device_specs(cfg, "deck", load_profile("steamos-emudeck")):
            self.assertFalse(spec.ignore_perms)
            self.assertNotIn("ignorePerms", spec.body())


class Device:
    """A temporary home with the dArkOS files, and a way to run `provision device`."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        tmp = Path(self._tmp.name)
        self.home = tmp / "home"
        self.state = tmp / "state"
        self.proc = tmp / "proc"
        self.proc.mkdir()
        self.system_list = tmp / "etc/es_systems.cfg"
        self.system_list.parent.mkdir()
        self.system_list.write_text(SOURCE)
        self.override = self.home / ".emulationstation/es_systems.cfg"
        self.cfg = self.home / ".config/retroarch/retroarch.cfg"
        self.cfg32 = self.home / ".config/retroarch32/retroarch.cfg"
        for path in (self.cfg, self.cfg32):
            path.parent.mkdir(parents=True)
            path.write_text(RETROARCH)
        self.profile = dataclasses.replace(
            PROFILE,
            es_systems=dataclasses.replace(PROFILE.es_systems, source=str(self.system_list)),
        )

    def start(self, comm: str) -> None:
        d = self.proc / "4242"
        d.mkdir()
        (d / "comm").write_text(comm + "\n")
        (d / "cmdline").write_bytes(comm.encode() + b"\0")
        (d / "stat").write_text("4242 (x) S 1 0 0 0\n")

    def backups(self, name: str) -> list[Path]:
        folder = self.state / "backups" / name
        return sorted(folder.rglob("*")) if folder.is_dir() else []

    def provision(self, *, dry_run=False, folders=("library", "saves"), **kw):
        cfg = ark_config(folders)
        rep, lines = reporter()
        kw.setdefault("proc", self.proc)
        with FakeSyncthing(ARK_ID) as local:
            for name in ("patterns-common.stignore", "patterns-ark.stignore"):
                local.arrives_after[("wz-library", name)] = 0
            client = Client(local.url, API_KEY, label="local", dry_run=dry_run, sink=lines.append)
            provision_device(
                cfg,
                "ark",
                self.profile,
                client,
                None,
                rep,
                home=self.home,
                wait_timeout=0.0,
                poll=1.0,
                sleep=lambda s: None,
                state_root=self.state,
                **kw,
            )
            self.syncthing = local
        return rep, lines


class ProvisionTests(Device, unittest.TestCase):
    def test_writes_the_override_and_pins_both_retroarch_configs(self) -> None:
        rep, lines = self.provision()
        self.assertEqual(rep.warnings, [])
        self.assertEqual(
            self.override.read_text(),
            SOURCE.replace("<path>/roms/nes/", "<path>/roms/library/roms/nes/").replace(
                "<path>/roms/amiga<", "<path>/roms/library/roms/amiga<"
            ),
        )
        for path in (self.cfg, self.cfg32):
            self.assertEqual(
                path.read_text(),
                RETROARCH.replace('"/roms/bios"', '"/roms/library/bios"').replace(
                    'content_dir = "true"', 'content_dir = "false"'
                ),
            )
        self.assertTrue(any("es_systems.cfg" in line for line in lines))

    def test_a_second_run_changes_and_backs_up_nothing(self) -> None:
        self.provision()
        before = [p.stat().st_mtime_ns for p in (self.override, self.cfg, self.cfg32)]
        backups = self.backups("retroarch")
        self.assertEqual(len(backups), 2 * 2)  # two files, a directory each
        rep, lines = self.provision()
        self.assertEqual(
            [p.stat().st_mtime_ns for p in (self.override, self.cfg, self.cfg32)], before
        )
        self.assertEqual(self.backups("retroarch"), backups)
        self.assertEqual(rep.warnings, [])
        self.assertTrue(any("already" in line for line in lines))

    def test_the_old_retroarch_config_is_backed_up_before_the_first_edit(self) -> None:
        self.provision()
        files = [p for p in self.backups("retroarch") if p.is_file()]
        self.assertEqual(len(files), 2)
        for backup in files:
            self.assertEqual(backup.read_text(), RETROARCH)

    def test_an_existing_stale_override_is_backed_up_and_replaced(self) -> None:
        self.override.parent.mkdir(parents=True)
        self.override.write_text("<systemList/>\n")
        self.provision()
        (backup,) = [p for p in self.backups("emulationstation") if p.is_file()]
        self.assertEqual(backup.read_text(), "<systemList/>\n")
        self.assertIn("/roms/library/roms/nes/", self.override.read_text())

    def test_a_fresh_override_has_nothing_to_back_up(self) -> None:
        self.provision()
        self.assertEqual(self.backups("emulationstation"), [])

    def test_left_alone_while_emulationstation_runs(self) -> None:
        self.start("emulationstatio")
        rep, _ = self.provision()
        self.assertFalse(self.override.exists())
        (warning,) = [w for w in rep.warnings if "EmulationStation" in w]
        self.assertIn("provision device ark", warning)
        # RetroArch is not running, so its configs are still pinned.
        self.assertIn('"/roms/library/bios"', self.cfg.read_text())

    def test_retroarch_configs_are_left_alone_while_retroarch_runs(self) -> None:
        self.start("retroarch")
        rep, _ = self.provision()
        self.assertEqual(self.cfg.read_text(), RETROARCH)
        self.assertEqual(self.cfg32.read_text(), RETROARCH)
        self.assertTrue(self.override.exists())
        self.assertEqual(len([w for w in rep.warnings if "RetroArch" in w]), 1)

    def test_without_proc_nothing_is_edited_and_it_says_why(self) -> None:
        rep, _ = self.provision(proc=self.proc / "missing")
        self.assertFalse(self.override.exists())
        self.assertEqual(self.cfg.read_text(), RETROARCH)
        self.assertTrue(any("cannot tell whether" in w for w in rep.warnings))

    def test_dry_run_writes_nothing(self) -> None:
        _, lines = self.provision(dry_run=True)
        self.assertFalse(self.override.exists())
        self.assertEqual(self.cfg.read_text(), RETROARCH)
        self.assertEqual(self.backups("retroarch"), [])
        self.assertTrue(any("dry-run" in line and "es_systems.cfg" in line for line in lines))

    def test_a_missing_system_list_warns_and_writes_no_override(self) -> None:
        self.system_list.unlink()
        rep, _ = self.provision()
        self.assertFalse(self.override.exists())
        self.assertTrue(any("es_systems.cfg" in w for w in rep.warnings))

    def test_a_missing_retroarch_config_warns_and_is_never_created(self) -> None:
        self.cfg32.unlink()
        rep, _ = self.provision()
        self.assertFalse(self.cfg32.exists())
        (warning,) = [w for w in rep.warnings if "retroarch32" in w]
        self.assertIn("start RetroArch once", warning)
        self.assertIn('"/roms/library/bios"', self.cfg.read_text())

    def test_a_key_that_cannot_be_written_leaves_that_file_alone(self) -> None:
        self.cfg.write_text(RETROARCH + 'system_directory = "/dup"\n')
        rep, _ = self.provision()
        self.assertEqual(self.cfg.read_text(), RETROARCH + 'system_directory = "/dup"\n')
        self.assertTrue(any("system_directory" in w for w in rep.warnings))
        self.assertIn('"/roms/library/bios"', self.cfg32.read_text())

    def test_a_device_without_the_library_gets_no_overrides(self) -> None:
        self.provision(folders=("saves",))
        self.assertFalse(self.override.exists())
        self.assertEqual(self.cfg.read_text(), RETROARCH)

    def test_folders_are_created_with_permissions_ignored_and_the_save_dirs_exist(self) -> None:
        self.provision()
        folders = self.syncthing.folders
        self.assertEqual(
            sorted(folders), ["wz-library", "wz-save-retroarch-saves", "wz-save-retroarch-states"]
        )
        for fid, folder in folders.items():
            self.assertIs(folder["ignorePerms"], True, fid)
        self.assertEqual(folders["wz-library"]["path"], "/roms/library")
        self.assertTrue((self.home / ".config/retroarch/saves").is_dir())
        self.assertTrue((self.home / ".config/retroarch/states").is_dir())

    def test_no_warning_mentions_things_this_device_does_not_have(self) -> None:
        rep, lines = self.provision()
        text = "\n".join(rep.warnings + lines).lower()
        for word in ("es-de", "flatpak", "songs", "media", "emudeck"):
            self.assertNotIn(word, text)


class DoctorTests(Device, unittest.TestCase):
    CONFIG = f"""
[hub]
name = "nas"
device_id = "{HUB_ID}"
root = "/data/games"
api_url = "http://127.0.0.1:1"
api_key = "{API_KEY}"

[devices.ark]
device_id = "{ARK_ID}"
profile = "anbernic-rg351mp"
folders = ["library", "saves"]
"""

    def setUp(self) -> None:
        super().setUp()
        syncthing = self.home / ".config/syncthing"
        syncthing.mkdir(parents=True)
        (syncthing / "config.xml").write_text(
            f'<configuration><folder id="wz-library" path="/roms/library/">'
            f'<device id="{HUB_ID}"/></folder><device id="{ARK_ID}" name="x"/>'
            "<gui><address>127.0.0.1:8384</address><apikey>k</apikey></gui></configuration>"
        )

    def check(self, config_text=None):
        tmp = Path(self._tmp.name)
        ctx = context(tmp, self.CONFIG if config_text is None else config_text, system="Linux")
        probes = dataclasses.replace(ctx.probes, home=self.home)
        # Point the profile at the temporary copy of dArkOS's system list.
        devices = tuple(
            dataclasses.replace(d, profile=self.profile) for d in ctx.config.frontend_devices
        )
        config = dataclasses.replace(ctx.config, frontend_devices=devices)
        return check_frontend(Context(probes, config))

    def test_a_provisioned_device_is_all_ok(self) -> None:
        self.provision()
        results = self.check()
        self.assertEqual(
            [(r.name, r.status) for r in results],
            [("ES systems override", OK), ("RetroArch paths", OK)],
        )

    def test_a_missing_override_is_a_warning_with_the_fix(self) -> None:
        self.provision()
        self.override.unlink()
        result = by_name(self.check(), "ES systems override")
        self.assertEqual(result.status, WARN)
        self.assertIn("missing", result.detail)
        self.assertIn("warpzone provision device ark", result.fix)

    def test_an_override_older_than_the_system_list_is_a_warning(self) -> None:
        self.provision()
        self.system_list.write_text(SOURCE.replace("</systemList>", "<!-- new --></systemList>"))
        result = by_name(self.check(), "ES systems override")
        self.assertEqual(result.status, WARN)
        self.assertIn("out of date", result.detail)

    def test_unpinned_retroarch_keys_are_named(self) -> None:
        self.provision()
        self.cfg32.write_text(RETROARCH)
        result = by_name(self.check(), "RetroArch paths")
        self.assertEqual(result.status, WARN)
        self.assertIn("system_directory", result.detail)
        self.assertIn("retroarch32", result.detail)
        self.assertNotIn("retroarch.cfg", result.detail.replace("retroarch32/retroarch.cfg", ""))

    def test_nothing_to_say_on_another_machine_or_another_profile(self) -> None:
        self.assertEqual(self.check(""), [])
        other = self.CONFIG.replace("anbernic-rg351mp", "steamos-emudeck")
        self.assertEqual(self.check(other), [])

    def test_the_frontend_check_is_part_of_the_run(self) -> None:
        self.assertIn(check_frontend, CHECKS)


class PureIsUsed(unittest.TestCase):
    def test_the_profile_rewrite_matches_the_pure_function(self) -> None:
        self.assertEqual(
            frontend.rewrite_system_paths(
                "<path>/roms/gb/</path>", PROFILE.es_systems.prefix, "/roms/library/roms/"
            ),
            "<path>/roms/library/roms/gb/</path>",
        )


if __name__ == "__main__":
    unittest.main()
