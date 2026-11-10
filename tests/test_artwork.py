"""Scraped artwork for a frontend that is not ES-DE: profile data, folder specs, converter."""

import dataclasses
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock
from xml.etree import ElementTree as ET

from support import config_data, make_config
from warpzone import artwork, ignores
from warpzone.config import parse_config
from warpzone.model import ModelError, device_specs, hub_specs
from warpzone.profiles import ARTWORK_FORMATS, ProfileError, load_profile, parse_profile

ARK_ID = "DDDDDDD-DDDDDDD-DDDDDDD-DDDDDDD-DDDDDDD-DDDDDDD-DDDDDDD-DDDDDDD"
PROFILE = load_profile("anbernic-rg351mp")
DECK = load_profile("steamos-emudeck")

SYSTEM_LIST = """<?xml version="1.0"?>
<systemList>
	<system><name>pokemonmini</name><path>/roms/pokemonmini/</path></system>
	<system><name>ppsspp</name><path>/roms/psp/</path></system>
	<system><name>pico-8</name><path>/roms/pico-8/carts/</path></system>
	<system><name>snes</name><path>/roms/sufami/</path></system>
	<system><name>snes</name><path>/roms/snes/</path></system>
	<system><name>gba</name><path>/roms/gba</path></system>
	<system><name>retroarch</name><path>/opt/cmds/</path></system>
</systemList>
"""
RULE = PROFILE.es_systems
RENAMES = {"pokemonmini": "pokemini", "pico-8": "pico8"}
TARGET = "/lib/roms/"


def ark_config(folders=("library", "frontend")):
    data = config_data()
    data["devices"]["ark"] = {
        "device_id": ARK_ID,
        "profile": "anbernic-rg351mp",
        "folders": list(folders),
    }
    return parse_config(data)


def source_xml(*games: tuple[str, ...]) -> str:
    body = "".join(
        f"\t<game>\n\t\t<path>{path}</path>\n\t\t<name>{name}</name>\n"
        + "".join(f"\t\t<{k}>{v}</{k}>\n" for k, v in rest)
        + "\t</game>\n"
        for path, name, *rest in games
    )
    return f'<?xml version="1.0"?>\n<gameList>\n{body}</gameList>\n'


class ProfileTests(unittest.TestCase):
    def test_the_anbernic_profile_declares_covers_for_the_library_systems(self) -> None:
        art = PROFILE.artwork
        self.assertEqual(art.format, "emulationstation")
        self.assertEqual(art.kinds, ("covers",))
        self.assertEqual(art.systems, "library")
        self.assertEqual(art.gamelists, artwork.ROMPATH)
        self.assertNotIn("media", PROFILE.paths)  # the artwork folders are not paths.*

    def test_the_steam_profile_has_none(self) -> None:
        self.assertIsNone(DECK.artwork)

    def test_every_format_the_profile_may_name_has_a_converter(self) -> None:
        self.assertEqual(set(ARTWORK_FORMATS), set(artwork.FORMATS))

    def test_every_kind_is_known_to_every_format(self) -> None:
        from warpzone.profiles import ARTWORK_KINDS

        for fmt in artwork.FORMATS.values():
            self.assertEqual(set(fmt.media_tags), set(ARTWORK_KINDS), fmt.name)


GOOD = {
    "format": "emulationstation",
    "kinds": ["covers", "screenshots"],
    "media": "/m/media",
    "metadata": "/m/meta",
    "gamelists": "~/gl",
}


class ParseTests(unittest.TestCase):
    def parse(self, art=None, **extra):
        data = {
            "name": "x",
            "paths": {"library": "/l"},
            "es_systems": {"source": "/a", "override": "~/b", "prefix": "/r/", "process": ["e"]},
            "artwork": GOOD if art is None else art,
            **extra,
        }
        return parse_profile(data, "t")

    def test_a_good_table_parses(self) -> None:
        art = self.parse().artwork
        self.assertEqual(art.kinds, ("covers", "screenshots"))
        self.assertEqual(art.systems, "library")
        self.assertIsNone(art.refresh)

    def test_unknown_values_and_keys_are_named(self) -> None:
        for bad, word in (
            ({**GOOD, "format": "nope"}, "artwork.format"),
            ({**GOOD, "kinds": []}, "artwork.kinds"),
            ({**GOOD, "kinds": ["covers", "covers"]}, "artwork.kinds"),
            ({**GOOD, "kinds": ["posters"]}, "artwork.kinds"),
            ({**GOOD, "systems": "everything"}, "artwork.systems"),
            ({**GOOD, "media": ""}, "artwork.media"),
            ({**GOOD, "gamelists": 3}, "artwork.gamelists"),
            ({**GOOD, "refresh": "soon"}, "artwork.refresh"),
            ({**GOOD, "extra": 1}, "artwork.extra"),
        ):
            with self.subTest(word=word, bad=bad), self.assertRaisesRegex(ProfileError, word):
                self.parse(bad)

    def test_refresh_is_a_systemd_interval(self) -> None:
        self.assertEqual(self.parse({**GOOD, "refresh": "15min"}).artwork.refresh, "15min")
        self.assertEqual(self.parse({**GOOD, "refresh": "6h"}).artwork.refresh, "6h")

    def test_the_folders_may_not_sit_inside_each_other_or_the_library(self) -> None:
        for media, meta in (("/l/media", "/m/meta"), ("/m/a", "/m/a/b"), ("/m/a", "/m/a")):
            with (
                self.subTest(media=media, meta=meta),
                self.assertRaisesRegex(ProfileError, "artwork"),
            ):
                self.parse({**GOOD, "media": media, "metadata": meta})

    def test_it_needs_the_library_and_the_system_list(self) -> None:
        with self.assertRaisesRegex(ProfileError, "paths.library"):
            parse_profile({"name": "x", "artwork": GOOD}, "t")
        with self.assertRaisesRegex(ProfileError, "es_systems"):
            parse_profile({"name": "x", "paths": {"library": "/l"}, "artwork": GOOD}, "t")

    def test_it_replaces_the_media_and_esde_paths(self) -> None:
        with self.assertRaisesRegex(ProfileError, "paths.media"):
            self.parse(paths={"library": "/l", "media": "/x"})


class ModelTests(unittest.TestCase):
    SYSTEMS = ("gba", "pokemini", "snes")

    def specs(self, systems=SYSTEMS, folders=("library", "frontend")):
        specs = device_specs(ark_config(folders), "ark", PROFILE, artwork_systems=systems)
        return {s.id: s for s in specs}

    def test_the_frontend_group_is_accepted_and_lands_where_the_profile_says(self) -> None:
        specs = self.specs()
        self.assertEqual(sorted(specs), ["wz-esde", "wz-library", "wz-media"])
        art = PROFILE.artwork
        self.assertEqual(specs["wz-media"].path, art.media)
        self.assertEqual(specs["wz-esde"].path, art.metadata)
        for fid in ("wz-media", "wz-esde"):
            self.assertEqual(specs[fid].type, "receiveonly")
            self.assertTrue(specs[fid].ignore_perms)

    def test_only_the_covers_of_the_systems_on_the_device_arrive(self) -> None:
        lines = self.specs()["wz-media"].ignores
        self.assertEqual(lines[: len(ignores.COMMON_LINES)], ignores.COMMON_LINES)
        wanted = lines[len(ignores.COMMON_LINES) :]
        self.assertEqual(wanted, ("!/gba/covers", "!/pokemini/covers", "!/snes/covers", "*"))

    def test_only_the_scraped_gamelists_of_those_systems_arrive(self) -> None:
        wanted = self.specs()["wz-esde"].ignores[len(ignores.COMMON_LINES) :]
        self.assertEqual(
            wanted,
            (
                "!/gamelists/gba/gamelist.xml",
                "!/gamelists/pokemini/gamelist.xml",
                "!/gamelists/snes/gamelist.xml",
                "*",
            ),
        )

    def test_no_system_means_nothing_arrives(self) -> None:
        for fid in ("wz-media", "wz-esde"):
            self.assertEqual(self.specs(())[fid].ignores[-1], "*")
            self.assertEqual(self.specs(())[fid].ignores[:-1], ignores.COMMON_LINES)

    def test_a_name_that_would_be_a_pattern_is_left_out(self) -> None:
        lines = self.specs(("gba", "we*ird", "a b", "[x]", "ok-1.2"))["wz-media"].ignores
        self.assertEqual(
            lines[len(ignores.COMMON_LINES) :][:-1], ("!/gba/covers", "!/ok-1.2/covers")
        )

    def test_without_the_group_there_are_no_artwork_folders(self) -> None:
        self.assertEqual(sorted(self.specs(folders=("library",))), ["wz-library"])

    def test_tools_songs_and_settings_are_still_refused_by_name(self) -> None:
        for group in ("songs", "tools", "settings"):
            with self.subTest(group=group), self.assertRaisesRegex(ModelError, group):
                device_specs(ark_config(("library", group)), "ark", PROFILE)

    def test_a_profile_with_neither_artwork_nor_paths_is_refused_for_the_group(self) -> None:
        bare = dataclasses.replace(PROFILE, artwork=None)
        with self.assertRaisesRegex(ModelError, "frontend"):
            device_specs(ark_config(), "ark", bare)

    def test_the_steam_devices_keep_their_folders(self) -> None:
        cfg = make_config()
        specs = {s.id: s for s in device_specs(cfg, "deck", DECK)}
        self.assertEqual(specs["wz-media"].type, "sendreceive")
        self.assertEqual(specs["wz-esde"].ignores[-3:], ("!/gamelists", "!/collections", "*"))

    def test_the_hub_folders_do_not_depend_on_the_console_that_receives_artwork(self) -> None:
        # Shared with the Deck: the hub's config and ignores are the same with or without it,
        # whichever device is listed first.
        base = {s.id: s for s in hub_specs(make_config(), {"steamos-emudeck": DECK})}
        data = config_data()
        ark = {
            "device_id": ARK_ID,
            "profile": "anbernic-rg351mp",
            "folders": ["library", "frontend"],
        }
        for order in (("ark", "deck", "machine"), ("deck", "machine", "ark")):
            data2 = dict(data)
            data2["devices"] = {n: ark if n == "ark" else data["devices"][n] for n in order}
            cfg = parse_config(data2)
            profiles = {"steamos-emudeck": DECK, "anbernic-rg351mp": PROFILE}
            got = {s.id: s for s in hub_specs(cfg, profiles)}
            for fid in ("wz-media", "wz-esde"):
                with self.subTest(order=order, fid=fid):
                    self.assertEqual(got[fid].ignores, base[fid].ignores)
                    self.assertEqual(got[fid].type, base[fid].type)
                    self.assertEqual(got[fid].path, base[fid].path)
                    self.assertEqual(got[fid].versioning, base[fid].versioning)
                    self.assertIn(ark["device_id"], got[fid].devices)


class SystemMapTests(unittest.TestCase):
    def systems(self):
        return artwork.frontend_systems(SYSTEM_LIST, "/roms/", TARGET, RENAMES)

    def test_names_come_from_the_system_list_not_from_the_folder(self) -> None:
        by_hub = {s.hub: s for s in self.systems()}
        self.assertEqual(by_hub["pokemini"].name, "pokemonmini")  # [es_systems.rename]
        self.assertEqual(by_hub["psp"].name, "ppsspp")  # the folder is not the name
        self.assertEqual(by_hub["gba"].name, "gba")  # no trailing slash in the list

    def test_a_system_below_a_subfolder_remembers_it(self) -> None:
        pico = next(s for s in self.systems() if s.name == "pico-8")
        self.assertEqual((pico.hub, pico.base), ("pico8", "carts/"))

    def test_a_name_that_is_not_a_plain_folder_name_is_skipped(self) -> None:
        names = ("../../../x", "a/b", "..", ".hidden", "a b")
        entries = "".join(
            f"<system><name>{n}</name><path>/roms/gba/</path></system>" for n in names
        )
        text = f"<systemList>{entries}<system><name>ok</name><path>/roms/nes/</path></system>"
        (system,) = artwork.frontend_systems(text + "</systemList>", "/roms/", TARGET)
        self.assertEqual((system.name, system.hub), ("ok", "nes"))

    def test_entries_outside_the_prefix_are_not_systems(self) -> None:
        self.assertNotIn("retroarch", {s.name for s in self.systems()})

    def test_the_real_rename_table_is_honoured(self) -> None:
        text = (
            "<systemList><system><name>segacd</name><path>/roms/segacd/</path></system>"
            "</systemList>"
        )
        (system,) = artwork.frontend_systems(text, "/roms/", TARGET, RULE.renames)
        self.assertEqual((system.name, system.hub), ("segacd", "megacd"))

    def test_two_systems_with_one_name_keep_the_one_the_hub_names_alike(self) -> None:
        chosen, dropped = artwork.assign(self.systems(), {"sufami", "snes"})
        self.assertEqual([s.hub for s in chosen["snes"]], ["snes"])
        self.assertEqual([(s.hub, s.name) for s in dropped], [("sufami", "snes")])

    def test_a_hub_folder_can_feed_several_frontend_systems(self) -> None:
        text = (
            "<systemList><system><name>a</name><path>/roms/x/</path></system>"
            "<system><name>b</name><path>/roms/x/</path></system></systemList>"
        )
        systems = artwork.frontend_systems(text, "/roms/", TARGET, {})
        chosen, dropped = artwork.assign(systems, {"x"})
        self.assertEqual([s.name for s in chosen["x"]], ["a", "b"])
        self.assertEqual(dropped, [])


class HostileGamelistTests(unittest.TestCase):
    """A gamelist another machine wrote must fail as a gamelist, never as a traceback."""

    ENCODINGS = ("bogus", "shift_jis", "big5", "utf-16-le", "idna", "rot13", "utf-7")

    def test_an_unusable_declared_encoding_is_an_artwork_error(self) -> None:
        for enc in self.ENCODINGS:
            data = f'<?xml version="1.0" encoding="{enc}"?><gameList/>'
            for form in (data, data.encode()):
                with (
                    self.subTest(encoding=enc, form=type(form).__name__),
                    self.assertRaises(artwork.ArtworkError),
                ):
                    artwork.parse_gamelist(form)

    def test_the_block_fallback_does_not_hide_an_unusable_encoding(self) -> None:
        text = (
            '<?xml version="1.0" encoding="bogus"?><alternativeEmulator/>'
            "<gameList><game><path>./A.zip</path></game></gameList>"
        )
        with self.assertRaises(artwork.ArtworkError):
            artwork.parse_gamelist(text.encode())

    def test_the_fallback_finds_the_block_after_other_elements(self) -> None:
        game = "<game><path>./A.zip</path><name>A</name></game>"
        for text in (
            f"<alternativeEmulator><label>x</label></alternativeEmulator><gameList>{game}</gameList>",
            f"<a/><b>text</b><gameList version='1'>{game}</gameList  >\n",
            f"<a/><gameList>{game}</gameList>",
        ):
            with self.subTest(text=text):
                (entry,) = artwork.parse_gamelist(text)
                self.assertEqual(entry.path, "./A.zip")
        self.assertEqual(artwork.parse_gamelist("<a/><gameList/>"), [])
        self.assertEqual(artwork.parse_gamelist("<a/><gameList />"), [])

    def test_the_fallback_does_not_take_a_longer_tag_for_the_block(self) -> None:
        with self.assertRaises(artwork.ArtworkError):
            artwork.parse_gamelist("<a/><gameListX><game/></gameListX>")
        with self.assertRaises(artwork.ArtworkError):
            artwork.parse_gamelist("<a/><gameList><game>")

    def test_many_gamelist_starts_are_read_in_linear_time(self) -> None:
        data = b"<gameList>" * 40000 + b"<oops"
        started = time.monotonic()
        with self.assertRaises(artwork.ArtworkError):
            artwork.parse_gamelist(data)
        self.assertLess(time.monotonic() - started, 2.0)

    def test_a_gamelist_over_the_limit_is_refused_before_parsing(self) -> None:
        text = "<gameList><game><path>./A.zip</path></game></gameList>"
        with (
            mock.patch.object(artwork, "MAX_GAMELIST_BYTES", len(text) - 1),
            mock.patch.object(artwork.ET, "fromstring") as parse,
        ):
            for form in (text, text.encode()):
                with self.assertRaisesRegex(artwork.ArtworkError, "too large"):
                    artwork.parse_gamelist(form)
            parse.assert_not_called()
        with mock.patch.object(artwork, "MAX_GAMELIST_BYTES", len(text)):
            self.assertEqual(len(artwork.parse_gamelist(text)), 1)

    def test_the_limit_is_32_mib(self) -> None:
        self.assertEqual(artwork.MAX_GAMELIST_BYTES, 32 * 1024 * 1024)

    def test_a_file_over_the_limit_is_refused_without_reading_it(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "gamelist.xml"
            path.write_text("<gameList/>" + " " * 100)
            with (
                mock.patch.object(artwork, "MAX_GAMELIST_BYTES", 50),
                mock.patch.object(Path, "read_bytes") as read,
            ):
                with self.assertRaisesRegex(artwork.ArtworkError, "too large"):
                    artwork.read_gamelist(path)
                read.assert_not_called()
            self.assertEqual(artwork.read_gamelist(path), path.read_bytes())

    def test_a_name_with_a_final_newline_is_not_plain(self) -> None:
        self.assertTrue(artwork.is_plain("gba"))
        for name in ("gba\n", "gba\r\n", "g\nba", "", ".hidden", "a/b", "..", "a b"):
            with self.subTest(name=name):
                self.assertFalse(artwork.is_plain(name))


class BuildTests(unittest.TestCase):
    FMT = artwork.FORMATS["emulationstation"]

    def build(self, source, existing="", *, base="", roms=None, media=None, kinds=("covers",)):
        roms = set(roms if roms is not None else [])
        media = dict(media or {})
        built = artwork.build(
            self.FMT,
            artwork.parse_gamelist(source),
            artwork.parse_gamelist(existing) if existing else [],
            kinds=kinds,
            base=base,
            rom_exists=lambda rel: rel in roms,
            media_path=lambda kind, rel: media.get((kind, rel)),
        )
        return built

    def test_it_copies_the_scraped_fields_and_adds_the_absolute_cover(self) -> None:
        src = source_xml(
            ("./A.zip", "A", ("desc", "d"), ("rating", "0.8"), ("altemulator", "x")),
        )
        built = self.build(src, roms={"A.zip"}, media={("covers", "A"): "/m/gba/covers/A.png"})
        (entry,) = built
        self.assertEqual(
            entry.fields,
            {"name": "A", "desc": "d", "rating": "0.8", "image": "/m/gba/covers/A.png"},
        )  # altemulator is ES-DE's, not this frontend's

    def test_a_rom_that_is_not_on_the_device_is_skipped(self) -> None:
        src = source_xml(("./A.zip", "A"), ("./B.zip", "B"))
        built = self.build(src, roms={"B.zip"})
        self.assertEqual([e.path for e in built], ["./B.zip"])

    def test_subfolders_are_kept_in_the_rom_and_the_cover(self) -> None:
        src = source_xml(("./Hacks/X.gba", "X"))
        built = self.build(
            src, roms={"Hacks/X.gba"}, media={("covers", "Hacks/X"): "/m/gba/covers/Hacks/X.png"}
        )
        self.assertEqual(built[0].fields["image"], "/m/gba/covers/Hacks/X.png")

    def test_an_entry_without_a_cover_has_no_image(self) -> None:
        built = self.build(source_xml(("./A.zip", "A")), roms={"A.zip"})
        self.assertNotIn("image", built[0].fields)

    def test_the_first_kind_that_exists_wins(self) -> None:
        src = source_xml(("./A.zip", "A"))
        media = {("screenshots", "A"): "/m/s.png"}
        built = self.build(src, roms={"A.zip"}, media=media, kinds=("covers", "screenshots"))
        self.assertEqual(built[0].fields["image"], "/m/s.png")
        media[("covers", "A")] = "/m/c.png"
        built = self.build(src, roms={"A.zip"}, media=media, kinds=("covers", "screenshots"))
        self.assertEqual(built[0].fields["image"], "/m/c.png")

    def test_a_system_below_a_subfolder_gets_paths_relative_to_it(self) -> None:
        src = source_xml(("./carts/G.p8", "G"), ("./other/H.p8", "H"))
        built = self.build(
            src, base="carts/", roms={"carts/G.p8"}, media={("covers", "carts/G"): "/m/G.png"}
        )
        self.assertEqual([e.path for e in built], ["./G.p8"])
        self.assertEqual(built[0].fields["image"], "/m/G.png")

    def test_paths_that_escape_the_system_folder_are_dropped(self) -> None:
        src = source_xml(("./../x.zip", "x"), ("/abs/y.zip", "y"), (".//z.zip", "z"))
        self.assertEqual(self.build(src, roms={"../x.zip", "/abs/y.zip", "z.zip", "x.zip"}), [])

    def test_user_data_the_frontend_saved_survives_a_rebuild(self) -> None:
        src = source_xml(("./A.zip", "A", ("desc", "new")))
        old = source_xml(
            (
                "./A.zip",
                "A",
                ("desc", "old"),
                ("favorite", "true"),
                ("playcount", "3"),
                ("lastplayed", "20260101T101010"),
            )
        )
        (entry,) = self.build(src, old, roms={"A.zip"})
        self.assertEqual(entry.fields["desc"], "new")  # scraped data is the hub's
        self.assertEqual(entry.fields["favorite"], "true")
        self.assertEqual(entry.fields["playcount"], "3")
        self.assertEqual(entry.fields["lastplayed"], "20260101T101010")

    def test_the_hubs_own_play_statistics_are_not_copied(self) -> None:
        src = source_xml(("./A.zip", "A", ("playcount", "9"), ("favorite", "true")))
        (entry,) = self.build(src, roms={"A.zip"})
        self.assertNotIn("playcount", entry.fields)
        self.assertNotIn("favorite", entry.fields)

    def test_a_game_the_hub_never_scraped_keeps_what_the_user_did_with_it(self) -> None:
        old = source_xml(("./Z.zip", "Z", ("playcount", "2")), ("./Gone.zip", "Gone"))
        built = self.build(source_xml(("./A.zip", "A")), old, roms={"A.zip", "Z.zip", "Gone.zip"})
        self.assertEqual([e.path for e in built], ["./A.zip", "./Z.zip"])
        self.assertEqual(built[1].fields, {"playcount": "2"})

    def test_user_data_of_a_rom_that_left_the_device_is_dropped(self) -> None:
        old = source_xml(("./Z.zip", "Z", ("playcount", "2")))
        built = self.build(source_xml(("./A.zip", "A")), old, roms={"A.zip"})
        self.assertEqual([e.path for e in built], ["./A.zip"])

    def test_the_result_is_deterministic_and_idempotent(self) -> None:
        src = source_xml(("./B.zip", "B", ("genre", "g")), ("./A.zip", "A", ("desc", "d")))
        roms = {"A.zip", "B.zip"}
        media = {("covers", "A"): "/m/A.png"}
        first = self.FMT.render(self.build(src, roms=roms, media=media))
        again = self.FMT.render(self.build(src, first, roms=roms, media=media))
        self.assertEqual(first, again)
        self.assertEqual(first, self.FMT.render(self.build(src, roms=roms, media=media)))

    def test_a_duplicated_path_is_listed_once(self) -> None:
        src = source_xml(("./A.zip", "first"), ("./A.zip", "second"))
        (entry,) = self.build(src, roms={"A.zip"})
        self.assertEqual(entry.fields["name"], "first")


class RenderTests(unittest.TestCase):
    FMT = artwork.FORMATS["emulationstation"]

    def entry(self, **fields):
        return artwork.Entry("game", "./A & B.zip", fields)

    def test_text_is_escaped_and_the_file_parses_back(self) -> None:
        text = self.FMT.render(
            [self.entry(name='Q&A <1> "x"', desc="a\x01b ]]> é", image="/m/a&b.png")]
        )
        root = ET.fromstring(text)
        game = root.find("game")
        self.assertEqual(game.findtext("path"), "./A & B.zip")
        self.assertEqual(game.findtext("name"), 'Q&A <1> "x"')
        self.assertEqual(game.findtext("desc"), "ab ]]> é")  # the control character is dropped
        self.assertEqual(game.findtext("image"), "/m/a&b.png")

    def test_the_layout_is_fixed(self) -> None:
        text = self.FMT.render([self.entry(image="/m/i.png", name="N", favorite="true", desc="d")])
        order = [c.tag for c in ET.fromstring(text).find("game")]
        self.assertEqual(order, ["path", "name", "desc", "image", "favorite"])
        self.assertTrue(text.startswith('<?xml version="1.0"?>\n<gameList>\n'))
        self.assertTrue(text.endswith("</gameList>\n"))

    def test_an_empty_list_is_still_a_gamelist(self) -> None:
        self.assertEqual(ET.fromstring(self.FMT.render([])).tag, "gameList")

    def test_a_folder_entry_is_written_as_a_folder(self) -> None:
        text = self.FMT.render([artwork.Entry("folder", "./Sub", {"name": "Sub"})])
        self.assertIsNotNone(ET.fromstring(text).find("folder"))

    def test_es_de_puts_an_alternative_emulator_element_before_the_game_list(self) -> None:
        # Seen in the hub's real gamelists: two top-level elements, so not a valid document.
        text = (
            '<?xml version="1.0"?>\n<alternativeEmulator>\n\t<label>melonDS</label>\n'
            "</alternativeEmulator>\n<gameList>\n\t<game><path>./A.zip</path>"
            "<name>A</name></game>\n</gameList>\n"
        )
        (entry,) = artwork.parse_gamelist(text)
        self.assertEqual((entry.path, entry.fields), ("./A.zip", {"name": "A"}))
        (entry,) = artwork.parse_gamelist(text.encode())
        self.assertEqual(entry.path, "./A.zip")

    def test_a_file_that_is_not_xml_is_an_error(self) -> None:
        with self.assertRaises(artwork.ArtworkError):
            artwork.parse_gamelist("<gameList><game>")
        with self.assertRaises(artwork.ArtworkError):
            artwork.parse_gamelist("<other/>")

    def test_the_same_content_ignores_layout_and_fields_the_frontend_does_not_use(self) -> None:
        built = [artwork.Entry("game", "./A.zip", {"name": "A", "rating": "0.8"})]
        saved = (
            "<gameList><game><rating>0.8</rating><name>A</name><path>./A.zip</path>"
            "<sortname>zz</sortname></game></gameList>"
        )
        self.assertTrue(artwork.same_content(self.FMT, built, saved))
        # On exit the frontend lists games the hub never scraped, with nothing of the player's.
        extra = saved.replace(
            "</gameList>", "<game><path>./B.zip</path><name>B</name></game></gameList>"
        )
        self.assertTrue(artwork.same_content(self.FMT, built, extra))
        played = saved.replace(
            "</gameList>", "<game><path>./B.zip</path><playcount>1</playcount></game></gameList>"
        )
        self.assertFalse(artwork.same_content(self.FMT, built, played))
        other = saved.replace("0.8", "0.9")
        self.assertFalse(artwork.same_content(self.FMT, built, other))
        self.assertFalse(artwork.same_content(self.FMT, built, "<gameList/>"))
        self.assertFalse(artwork.same_content(self.FMT, built, "not xml"))


class RompathGamelistTests(unittest.TestCase):
    """EmulationStation prefers `<rom folder>/gamelist.xml`, so the generated one goes there."""

    def test_the_gamelist_goes_next_to_the_roms_and_keeps_saved_data(self) -> None:
        roms = Path("/lib/roms")
        store = {
            Path("/es/gamelists/gba/gamelist.xml"): (
                b"<gameList><game><path>./A.zip</path><name>Alpha</name></game></gameList>"
            ),
            roms / "gba/gamelist.xml": (
                b"<gameList><game><path>./A.zip</path><name>A</name>"
                b"<favorite>true</favorite></game></gameList>"
            ),
            roms / "gba/A.zip": b"x",
        }
        files = artwork.Files(
            read=store.get, is_file=lambda p: p in store, exists=lambda p: p in store
        )
        plan = artwork.plan_gamelists(
            artwork.FORMATS["emulationstation"],
            ("covers",),
            artwork.Layout(roms, Path("/m"), Path("/es"), Path(artwork.ROMPATH)),
            "<systemList><system><name>gba</name><path>/lib/roms/gba/</path></system></systemList>",
            prefix="/lib/roms/",
            renames={},
            hubs=["gba"],
            files=files,
        )
        self.assertEqual([i.path for i in plan.items], [roms / "gba/gamelist.xml"])
        self.assertIn(b"<favorite>true</favorite>", plan.items[0].data)
        self.assertIn(b"<name>Alpha</name>", plan.items[0].data)


class UnreadableGamelistTests(unittest.TestCase):
    """The plan says why the device's own gamelist could not be read, and leaves the I/O out."""

    ROOMS = Path("/lib/roms")
    OWN = ROOMS / "gba/gamelist.xml"

    def plan(self, own: bytes | None) -> artwork.GamelistPlan:
        store = {
            Path("/es/gamelists/gba/gamelist.xml"): (
                b"<gameList><game><path>./A.zip</path><name>Alpha</name></game></gameList>"
            ),
            self.ROOMS / "gba/A.zip": b"x",
        }
        if own is not None:
            store[self.OWN] = own
        files = artwork.Files(
            read=store.get, is_file=lambda p: p in store, exists=lambda p: p in store
        )
        return artwork.plan_gamelists(
            artwork.FORMATS["emulationstation"],
            ("covers",),
            artwork.Layout(self.ROOMS, Path("/m"), Path("/es"), Path(artwork.ROMPATH)),
            "<systemList><system><name>gba</name><path>/lib/roms/gba/</path></system></systemList>",
            prefix="/lib/roms/",
            renames={},
            hubs=["gba"],
            files=files,
        )

    def test_a_truncated_file_carries_the_reason(self) -> None:
        (item,) = self.plan(b"<gameList><game><path>./A.zip</pa").items
        self.assertEqual(item.state, "stale")
        with self.assertRaises(artwork.ArtworkError) as raised:
            artwork.parse_gamelist(b"<gameList><game><path>./A.zip</pa")
        self.assertEqual(item.unreadable, str(raised.exception))
        self.assertNotEqual(item.unreadable, "")

    def test_an_unusable_encoding_carries_the_reason(self) -> None:
        (item,) = self.plan(b'<?xml version="1.0" encoding="bogus"?><gameList/>').items
        self.assertEqual(item.state, "stale")
        self.assertIn("bogus", item.unreadable)

    def test_a_missing_file_and_a_readable_one_carry_none(self) -> None:
        (missing,) = self.plan(None).items
        self.assertEqual((missing.state, missing.unreadable), ("missing", ""))
        (readable,) = self.plan(
            b"<gameList><game><path>./A.zip</path><name>Old</name></game></gameList>"
        ).items
        self.assertEqual((readable.state, readable.unreadable), ("stale", ""))


if __name__ == "__main__":
    unittest.main()
