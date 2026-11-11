"""The `anbernic-rg40xxv` profile (Knulli): shipped data, the system list filter, the pins."""

import dataclasses
import tempfile
import unittest
from pathlib import Path

from support import API_KEY, FakeProc, FakeSyncthing, config_data, device_probes, stub_self_update
from test_provision import reporter
from warpzone import artwork, frontend
from warpzone.config import parse_config
from warpzone.doctor import OK, WARN, judge_retroarch_paths, judge_systems_override
from warpzone.model import ModelError, device_specs
from warpzone.profiles import ProfileError, load_profile, parse_profile
from warpzone.provision import provision_device
from warpzone.syncthing import Client

setUpModule = stub_self_update


RG40_ID = "EEEEEEE-EEEEEEE-EEEEEEE-EEEEEEE-EEEEEEE-EEEEEEE-EEEEEEE-EEEEEEE"
PROFILE = load_profile("anbernic-rg40xxv")
SOURCE = (
    '<?xml version="1.0"?>\n'
    "<systemList>\n"
    "  <system>\n    <name>nes</name>\n    <path>/userdata/roms/nes</path>\n  </system>\n"
    "  <system>\n    <name>megadrive</name>\n    <path>/userdata/roms/megadrive</path>\n"
    "  </system>\n"
    "  <system>\n    <name>ports</name>\n    <path>/userdata/roms/ports</path>\n  </system>\n"
    "  <system>\n    <name>nesdisk</name>\n    <path>/userdata/roms/nes2</path>\n  </system>\n"
    "</systemList>\n"
)
MOVED = SOURCE.replace(
    "<path>/userdata/roms/nes<", "<path>/userdata/warpzone/library/roms/nes<"
).replace("<path>/userdata/roms/megadrive<", "<path>/userdata/warpzone/library/roms/genesis<")
CONF = (
    "# ------------ A - System Options ----------- #\n"
    "system.language=es_ES\n"
    "#global.retroarch.menu_driver=rgui\n"
    "global.videomode=default\n"
)
PINNED = (
    CONF
    + "global.retroarch.system_directory=/userdata/warpzone/library/bios\n"
    + "global.retroarch.savefile_directory=/userdata/warpzone/saves/retroarch\n"
)
TARGET = "/userdata/warpzone/library/roms/"


def rg40_config(folders=("library", "saves")):
    data = config_data()
    data["devices"] = {
        "rg40": {
            "device_id": RG40_ID,
            "profile": "anbernic-rg40xxv",
            "folders": list(folders),
            "auto_prune": False,
            "self_update": False,
        }
    }
    return parse_config(data)


class ProfileTests(unittest.TestCase):
    def test_the_profile_is_shipped_under_its_exact_name(self) -> None:
        self.assertEqual(PROFILE.name, "anbernic-rg40xxv")
        self.assertEqual(PROFILE.paths["library"], "/userdata/warpzone/library")

    def test_the_library_is_on_a_filesystem_without_permissions(self) -> None:
        self.assertTrue(PROFILE.ignore_perms)

    def test_folders_without_a_path_are_refused_by_name(self) -> None:
        for group in ("songs", "tools", "settings"):
            with self.subTest(group=group), self.assertRaisesRegex(ModelError, group):
                device_specs(rg40_config(("library", group)), "rg40", PROFILE)

    def test_the_pins_go_to_knulli_conf_in_its_own_format(self) -> None:
        pins = PROFILE.retroarch
        self.assertEqual(pins.cfg, ("/userdata/system/knulli.conf",))
        self.assertEqual(pins.format, "batocera-conf")
        for key in pins.set:
            self.assertTrue(key.startswith("global.retroarch."), key)

    def test_retroarch_saves_go_to_the_synced_folder(self) -> None:
        wants = PROFILE.retroarch.wants(PROFILE.paths["library"])
        self.assertEqual(
            wants["global.retroarch.savefile_directory"], PROFILE.saves["retroarch-saves"].path
        )
        self.assertEqual(
            wants["global.retroarch.system_directory"], "/userdata/warpzone/library/bios"
        )

    def test_save_states_stay_where_the_save_state_manager_looks(self) -> None:
        self.assertNotIn("retroarch-states", PROFILE.saves)
        self.assertNotIn("global.retroarch.savestate_directory", PROFILE.retroarch.set)

    def test_the_hub_save_folder_is_the_one_of_the_steam_devices(self) -> None:
        deck = load_profile("steamos-emudeck")
        mine, theirs = PROFILE.saves["retroarch-saves"], deck.saves["retroarch-saves"]
        self.assertEqual(
            (mine.emu, mine.kind, mine.ignore), (theirs.emu, theirs.kind, theirs.ignore)
        )

    def test_ports_and_tools_keep_their_own_folders(self) -> None:
        systems = PROFILE.es_systems.systems
        self.assertIn("nes", systems)
        for local in ("ports", "tools", "pygame", "emulators"):
            self.assertNotIn(local, systems)

    def test_every_renamed_system_is_one_that_moves(self) -> None:
        self.assertLessEqual(set(PROFILE.es_systems.renames), set(PROFILE.es_systems.systems))

    def test_drastic_saves_are_converted_like_on_the_rg351mp(self) -> None:
        mine = PROFILE.save_convert["drastic"]
        theirs = load_profile("anbernic-rg351mp").save_convert["drastic"]
        self.assertEqual(
            (mine.format, mine.saves, mine.es_system), (theirs.format, "melonds-saves", "nds")
        )
        self.assertEqual(theirs.saves, "melonds-saves")


class ArtworkTests(unittest.TestCase):
    FMT = artwork.FORMATS["batocera"]
    SOURCE = (
        '<?xml version="1.0"?>\n<gameList>\n'
        "\t<game>\n\t\t<path>./A.zip</path>\n\t\t<name>A</name>\n\t\t<desc>new</desc>\n"
        "\t\t<playtime>99</playtime>\n\t</game>\n</gameList>\n"
    )
    SAVED = (
        '<?xml version="1.0"?>\n<gameList>\n'
        "\t<game>\n\t\t<path>./A.zip</path>\n\t\t<name>A</name>\n"
        "\t\t<favorite>true</favorite>\n\t\t<playcount>3</playcount>\n"
        "\t\t<lastplayed>20261110T235632</lastplayed>\n\t\t<gametime>2411</gametime>\n"
        "\t</game>\n</gameList>\n"
    )

    def build(self, existing: str = SAVED):
        media = {
            ("covers", "A"): "/m/snes/covers/A.png",
            ("marquees", "A"): "/m/snes/marquees/A.png",
        }
        return artwork.build(
            self.FMT,
            artwork.parse_gamelist(self.SOURCE),
            artwork.parse_gamelist(existing),
            kinds=PROFILE.artwork.kinds,
            base="",
            rom_exists=lambda rel: rel == "A.zip",
            media_path=lambda kind, rel: media.get((kind, rel)),
        )

    def test_the_profile_uses_the_batocera_converter_outside_the_library(self) -> None:
        art = PROFILE.artwork
        self.assertEqual(art.format, "batocera")
        self.assertEqual(art.gamelists, artwork.ROMPATH)
        library = PROFILE.paths["library"]
        for path in (art.media, art.metadata):
            self.assertFalse(path.startswith(library + "/"), path)
        self.assertIsNone(art.refresh)  # no systemd on Knulli

    def test_the_playtime_knulli_saved_survives_a_rebuild(self) -> None:
        (entry,) = self.build()
        self.assertEqual(entry.fields["gametime"], "2411")
        self.assertEqual(entry.fields["favorite"], "true")
        self.assertEqual(entry.fields["playcount"], "3")
        self.assertEqual(entry.fields["desc"], "new")  # scraped data is the hub's

    def test_the_hubs_playtime_is_never_copied(self) -> None:
        (entry,) = self.build(existing='<?xml version="1.0"?>\n<gameList/>\n')
        self.assertNotIn("gametime", entry.fields)
        self.assertNotIn("playtime", entry.fields)

    def test_covers_are_the_image_and_logos_the_marquee(self) -> None:
        (entry,) = self.build()
        self.assertEqual(entry.fields["image"], "/m/snes/covers/A.png")
        self.assertEqual(entry.fields["marquee"], "/m/snes/marquees/A.png")

    def test_the_written_file_keeps_gametime_and_reads_back_the_same(self) -> None:
        built = self.build()
        text = self.FMT.render(built)
        self.assertIn("<gametime>2411</gametime>", text)
        self.assertTrue(artwork.same_content(self.FMT, built, text))
        # The RetroPie converter would lose it: that is why Knulli has its own.
        self.assertNotIn("gametime", artwork.FORMATS["emulationstation"].user_fields)

    def test_play_state_reads_the_playtime_from_gametime(self) -> None:
        self.assertEqual(self.FMT.state_tags["playtime"], "gametime")

    def test_what_knullis_scraper_downloads_is_never_synced(self) -> None:
        for folder in ("images", "videos", "manuals"):
            self.assertIn(f"(?d)/roms/*/{folder}", PROFILE.library_ignore)


class ParseTests(unittest.TestCase):
    def parse(self, **extra):
        return parse_profile({"name": "x", "paths": {"library": "/l"}, **extra}, "t")

    def test_es_systems_may_name_the_systems_that_move(self) -> None:
        good = {"source": "/a", "override": "~/b", "prefix": "/roms/", "process": ["es"]}
        self.assertIsNone(self.parse(es_systems=good).es_systems.systems)
        named = self.parse(es_systems={**good, "systems": ["nes", "snes"]}).es_systems
        self.assertEqual(named.systems, ("nes", "snes"))
        for bad in ([], "nes", ["nes", ""], [1]):
            with self.subTest(bad=bad), self.assertRaisesRegex(ProfileError, "es_systems.systems"):
                self.parse(es_systems={**good, "systems": bad})

    def test_retroarch_format_is_one_of_the_two(self) -> None:
        good = {"cfg": ["~/a.cfg"], "process": ["retroarch"], "set": {"k": "v"}}
        self.assertEqual(self.parse(retroarch=good).retroarch.format, "retroarch-cfg")
        conf = self.parse(retroarch={**good, "format": "batocera-conf"}).retroarch
        self.assertEqual(conf.format, "batocera-conf")
        with self.assertRaisesRegex(ProfileError, "retroarch.format"):
            self.parse(retroarch={**good, "format": "ini"})


class SystemListTests(unittest.TestCase):
    def rewrite(self, only):
        rule = PROFILE.es_systems
        return frontend.rewrite_system_paths(SOURCE, rule.prefix, TARGET, rule.renames, only)

    def test_only_the_named_systems_move(self) -> None:
        self.assertEqual(self.rewrite(PROFILE.es_systems.systems), MOVED)

    def test_a_folder_that_starts_like_a_named_one_stays(self) -> None:
        self.assertIn("<path>/userdata/roms/nes2<", self.rewrite(["nes"]))

    def test_without_a_filter_every_system_moves(self) -> None:
        self.assertNotIn("<path>/userdata/roms/", self.rewrite(None))

    def test_applying_it_again_changes_nothing(self) -> None:
        rule = PROFILE.es_systems
        again = frontend.rewrite_system_paths(
            MOVED, rule.prefix, TARGET, rule.renames, rule.systems
        )
        self.assertEqual(again, MOVED)

    def test_doctor_compares_with_the_same_filter(self) -> None:
        rule = PROFILE.es_systems
        common = {
            "device": "rg40",
            "source": SOURCE,
            "source_path": rule.source,
            "override_path": rule.override,
            "prefix": rule.prefix,
            "renames": rule.renames,
            "target": TARGET,
        }
        ok = judge_systems_override(override=MOVED, systems=rule.systems, **common)
        self.assertEqual(ok[0], OK)
        # A copy that moved every system is not the one this profile makes.
        stale = judge_systems_override(override=self.rewrite(None), systems=rule.systems, **common)
        self.assertEqual(stale[0], WARN)


class PinTests(unittest.TestCase):
    WANTS = PROFILE.retroarch.wants(PROFILE.paths["library"])

    def test_missing_keys_are_added_at_the_end_and_nothing_else_changes(self) -> None:
        self.assertEqual(frontend.retroarch_apply(CONF, self.WANTS, "batocera-conf"), PINNED)

    def test_applying_it_again_changes_nothing(self) -> None:
        self.assertEqual(frontend.retroarch_apply(PINNED, self.WANTS, "batocera-conf"), PINNED)
        self.assertEqual(frontend.retroarch_gap(PINNED, self.WANTS, "batocera-conf").differs, {})

    def test_another_value_is_replaced_in_place(self) -> None:
        text = CONF.replace(
            "global.videomode=default\n",
            "global.retroarch.savefile_directory=/userdata/saves\nglobal.videomode=default\n",
        )
        out = frontend.retroarch_apply(text, self.WANTS, "batocera-conf")
        self.assertIn(
            "global.retroarch.savefile_directory=/userdata/warpzone/saves/retroarch\n"
            "global.videomode=default\n",
            out,
        )
        self.assertEqual(out.count("savefile_directory"), 1)

    def test_a_key_present_twice_is_not_edited(self) -> None:
        line = "global.retroarch.savefile_directory=/a\n"
        gap = frontend.retroarch_gap(CONF + line + line, self.WANTS, "batocera-conf")
        self.assertEqual(gap.unwritable, {"global.retroarch.savefile_directory"})

    def test_doctor_reads_knulli_conf_in_its_format(self) -> None:
        path = "/userdata/system/knulli.conf"
        common = {"device": "rg40", "wants": self.WANTS, "fmt": "batocera-conf"}
        self.assertEqual(judge_retroarch_paths(files=[(path, PINNED)], **common)[0], OK)
        status, message, _ = judge_retroarch_paths(files=[(path, CONF)], **common)
        self.assertEqual(status, WARN)
        self.assertIn("global.retroarch.savefile_directory is unset", message)


class ProvisionTests(unittest.TestCase):
    """A temporary card with Knulli's files, and `provision device` run on it."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        tmp = Path(self._tmp.name)
        self.home = tmp / "userdata/system"
        self.state = tmp / "state"
        self.proc = FakeProc(tmp / "proc")
        self.system_list = tmp / "usr/es_systems.cfg"
        self.system_list.parent.mkdir(parents=True)
        self.system_list.write_text(SOURCE)
        self.override = self.home / "configs/emulationstation/es_systems.cfg"
        self.conf = self.home / "knulli.conf"
        self.conf.parent.mkdir(parents=True)
        self.conf.write_text(CONF)
        self.profile = dataclasses.replace(
            PROFILE,
            es_systems=dataclasses.replace(
                PROFILE.es_systems, source=str(self.system_list), override=str(self.override)
            ),
            retroarch=dataclasses.replace(PROFILE.retroarch, cfg=(str(self.conf),)),
            # The save folders live in /userdata, outside the temporary folder.
            saves={},
            save_convert={},
        )

    def provision(self, **kw):
        rep, lines = reporter()
        kw.setdefault("probes", device_probes(self.proc.path))
        with FakeSyncthing(RG40_ID) as local:
            for name in ("patterns-common.stignore", "patterns-rg40.stignore"):
                local.arrives_after[("wz-library", name)] = 0
            client = Client(local.url, API_KEY, label="local", sink=lines.append)
            provision_device(
                rg40_config(),
                "rg40",
                self.profile,
                client,
                None,
                rep,
                home=self.home,
                wait_timeout=0.0,
                poll=1.0,
                state_root=self.state,
                **kw,
            )
        return rep, lines

    def test_writes_the_filtered_override_and_pins_knulli_conf(self) -> None:
        rep, _ = self.provision()
        self.assertEqual(rep.warnings, [])
        self.assertEqual(self.override.read_text(), MOVED)
        self.assertEqual(self.conf.read_text(), PINNED)

    def test_a_second_run_changes_nothing(self) -> None:
        self.provision()
        before = [p.stat().st_mtime_ns for p in (self.override, self.conf)]
        rep, lines = self.provision()
        self.assertEqual([p.stat().st_mtime_ns for p in (self.override, self.conf)], before)
        self.assertEqual(rep.warnings, [])
        self.assertTrue(any("already" in line for line in lines))

    def test_knulli_conf_is_backed_up_before_the_edit(self) -> None:
        self.provision()
        backups = [p for p in (self.state / "backups").rglob("*") if p.is_file()]
        self.assertEqual([p.read_text() for p in backups], [CONF])

    def test_the_system_list_waits_while_emulationstation_runs(self) -> None:
        self.proc.add(4242, "emulationstatio")
        rep, _ = self.provision()
        self.assertFalse(self.override.exists())
        self.assertEqual(len([w for w in rep.warnings if "EmulationStation" in w]), 1)
        self.assertEqual(self.conf.read_text(), PINNED)


if __name__ == "__main__":
    unittest.main()
