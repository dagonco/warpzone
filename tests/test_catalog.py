"""The catalog index, pure part: derivation of games and parts, title keys, merge, JSON, search.

Listings are built in memory, so nothing here touches a filesystem.
"""

import json
import unittest
from dataclasses import replace
from datetime import UTC, datetime

from warpzone import catalog, ra
from warpzone import dat as datmod
from warpzone.catalog import Listing, Node, Part


def listing(files: dict[str, int | str], dirs: tuple[str, ...] = ()) -> Listing:
    """A listing from `path -> size` (or `path -> text`, whose size is its length). The
    folders above each file are added; `dirs` names empty folders."""
    nodes: dict[str, Node] = {}
    texts: dict[str, str] = {}
    folders = set(dirs)
    for path, value in files.items():
        if isinstance(value, str):
            texts[path] = value
            nodes[path] = Node(path, catalog.FILE, len(value), 1000)
        else:
            nodes[path] = Node(path, catalog.FILE, value, 1000)
        folder = path.rpartition("/")[0]
        while folder:
            folders.add(folder)
            folder = folder.rpartition("/")[0]
    for folder in folders:
        nodes[folder] = Node(folder, catalog.DIR, 0, 900)
    return Listing(list(nodes.values()), texts)


def by_primary(games: list[catalog.Game]) -> dict[str, catalog.Game]:
    return {g.primary: g for g in games}


def paths(game: catalog.Game) -> list[str]:
    return [p.path for p in game.parts]


class TitleKeyTests(unittest.TestCase):
    def test_no_intro_and_redump_names(self) -> None:
        key = catalog.title_key
        self.assertEqual(key("Example Quest (USA) (Rev 1)"), "example quest")
        self.assertEqual(key("Example Quest (Europe) (En,Fr,De)"), "example quest")
        self.assertEqual(key("Sample Saga (USA) (Disc 1) [!]"), "sample saga")
        self.assertEqual(key("Sample Saga [b1]"), "sample saga")

    def test_the_moves_to_the_end_in_no_intro_names(self) -> None:
        key = catalog.title_key
        self.assertEqual(key("Legend of Fakeland, The (USA)"), "legend of fakeland")
        self.assertEqual(key("The Legend of Fakeland (Europe)"), "legend of fakeland")
        self.assertEqual(
            key("Legend of Fakeland, The - A Dream of Tests (USA)"),
            key("The Legend of Fakeland - A Dream of Tests"),
        )
        self.assertEqual(
            key("Legend of Fakeland, The - A Dream of Tests"), "legend of fakeland a dream of tests"
        )

    def test_a_the_inside_the_title_stays(self) -> None:
        self.assertEqual(catalog.title_key("Fall of the Test"), "fall of the test")

    def test_accents_case_apostrophes_ampersand_and_punctuation(self) -> None:
        key = catalog.title_key
        self.assertEqual(key("Pokémon Fake"), "pokemon fake")
        self.assertEqual(key("Mr. Tester's Quest"), "mr testers quest")
        self.assertEqual(key("Tester\u2019s Quest"), "testers quest")
        self.assertEqual(key("Ratchet & Clank"), "ratchet and clank")
        self.assertEqual(key("Tests: The Sequel!"), "tests the sequel")
        self.assertEqual(key("FAKE-MAN 2"), "fake man 2")

    def test_roman_numerals_and_digits_are_different_keys(self) -> None:
        self.assertNotEqual(catalog.title_key("Fantasy VII"), catalog.title_key("Fantasy 7"))

    def test_switch_names_lose_their_ids(self) -> None:
        self.assertEqual(catalog.title_key("Example Game [0100AAAA00000000][v0]"), "example game")

    def test_a_name_that_is_only_a_group_still_has_a_key(self) -> None:
        self.assertEqual(catalog.title_key("(Bonus Disc)"), "bonus disc")


class SystemRootsTests(unittest.TestCase):
    def test_roots(self) -> None:
        self.assertEqual(catalog.system_roots("psx"), ["roms/psx"])
        self.assertEqual(catalog.system_roots("ps4"), ["roms/ps4"])
        for system in ("xbox360", "wiiu", "model2"):
            self.assertEqual(catalog.system_roots(system), [f"roms/{system}/roms"])
        self.assertEqual(
            catalog.system_roots("switch"),
            ["roms/switch", "extras/switch/updates", "extras/switch/dlc"],
        )


PLAYLIST = "# playlist\nSample (Disc 1).chd\nSample (Disc 2).chd\nGone.chd\n"


class DefaultDerivationTests(unittest.TestCase):
    def test_each_entry_is_a_game_and_hidden_and_junk_names_are_not(self) -> None:
        games = catalog.derive(
            "gba",
            listing(
                {
                    "roms/gba/Alpha (USA).zip": 100,
                    "roms/gba/Beta (USA).zip": 200,
                    "roms/gba/.hidden.zip": 1,
                    "roms/gba/Thumbs.db": 1,
                    "roms/gba/Gamma.zip.part": 5,
                    "roms/gba/systeminfo.txt": 1,
                    "roms/gba/metadata.txt": 1,
                    "roms/gba/@eaDir/thumb.jpg": 1,
                }
            ),
        )
        self.assertEqual(
            [g.primary for g in games], ["roms/gba/Alpha (USA).zip", "roms/gba/Beta (USA).zip"]
        )
        alpha = games[0]
        self.assertEqual(alpha.title, "Alpha (USA)")
        self.assertEqual(alpha.key, "alpha")
        self.assertEqual(alpha.gamelist, "./Alpha (USA).zip")
        self.assertEqual(alpha.parts, (Part("roms/gba/Alpha (USA).zip", "file", 100, 1000),))
        self.assertEqual(alpha.size, 100)
        self.assertEqual(alpha.flags, ())
        self.assertEqual(len(alpha.id), 16)

    def test_the_id_follows_the_primary_path(self) -> None:
        a = catalog.derive("gba", listing({"roms/gba/A.zip": 1}))[0]
        b = catalog.derive("gba", listing({"roms/gba/A.zip": 999}))[0]
        c = catalog.derive("gba", listing({"roms/gba/B.zip": 1}))[0]
        self.assertEqual(a.id, b.id)
        self.assertNotEqual(a.id, c.id)

    def test_a_title_keeps_dots_that_are_not_an_extension(self) -> None:
        games = catalog.derive("gba", listing({"roms/gba/Mr. Tester": 1}))
        self.assertEqual(games[0].title, "Mr. Tester")

    def test_an_m3u_folder_is_one_game_with_one_dir_part(self) -> None:
        games = catalog.derive(
            "psx",
            listing(
                {
                    "roms/psx/Sample Saga.m3u/Sample Saga (Disc 1).chd": 300,
                    "roms/psx/Sample Saga.m3u/Sample Saga (Disc 2).chd": 400,
                    "roms/psx/Sample Saga.m3u/Sample Saga.m3u": "Sample Saga (Disc 1).chd\n",
                }
            ),
        )
        self.assertEqual(len(games), 1)
        game = games[0]
        self.assertEqual(game.title, "Sample Saga")
        self.assertEqual(game.primary, "roms/psx/Sample Saga.m3u")
        part = game.parts[0]
        self.assertEqual(
            (part.type, part.size, part.files, part.mtime), ("dir", 700 + 0 + 25, 3, 1000)
        )
        self.assertEqual(game.gamelist, "./Sample Saga.m3u")

    def test_a_ps3_folder_is_one_game_only_in_ps3(self) -> None:
        files = {"roms/ps3/Example Disc.ps3/PS3_GAME/USRDIR/a.bin": 10}
        games = catalog.derive("ps3", listing(files))
        self.assertEqual([g.primary for g in games], ["roms/ps3/Example Disc.ps3"])
        other = catalog.derive("psx", listing({"roms/psx/Example Disc.ps3/a.bin": 10}))
        self.assertEqual([g.primary for g in other], ["roms/psx/Example Disc.ps3/a.bin"])

    def test_other_folders_are_walked(self) -> None:
        games = catalog.derive(
            "snes",
            listing({"roms/snes/Hacks/One.zip": 1, "roms/snes/Hacks/More/Two.zip": 2}),
        )
        self.assertEqual(
            [g.primary for g in games],
            ["roms/snes/Hacks/More/Two.zip", "roms/snes/Hacks/One.zip"],
        )
        self.assertEqual(games[1].gamelist, "./Hacks/One.zip")

    def test_a_cue_claims_the_files_it_names(self) -> None:
        cue = (
            'FILE "Disc Game (Track 1).bin" BINARY\n  TRACK 01 MODE2/2352\n    INDEX 01 00:00:00\n'
            'FILE "disc game (track 2).BIN" BINARY\n  TRACK 02 AUDIO\n    INDEX 01 00:00:00\n'
        )
        games = catalog.derive(
            "psx",
            listing(
                {
                    "roms/psx/Disc Game.cue": cue,
                    "roms/psx/Disc Game (Track 1).bin": 5000,
                    "roms/psx/Disc Game (Track 2).bin": 3000,
                    "roms/psx/Other.chd": 100,
                }
            ),
        )
        self.assertEqual(
            [g.primary for g in games], ["roms/psx/Disc Game.cue", "roms/psx/Other.chd"]
        )
        self.assertEqual(
            paths(games[0]),
            [
                "roms/psx/Disc Game.cue",
                "roms/psx/Disc Game (Track 1).bin",
                "roms/psx/Disc Game (Track 2).bin",
            ],
        )
        self.assertEqual(games[0].size, len(cue) + 8000)

    def test_a_gdi_in_a_folder_claims_its_tracks(self) -> None:
        gdi = '2\n1 0 4 2352 "track01.bin" 0\n2 756 0 2352 "track02.raw" 0\n'
        games = catalog.derive(
            "dreamcast",
            listing(
                {
                    "roms/dreamcast/Fake Racer/disc.gdi": gdi,
                    "roms/dreamcast/Fake Racer/track01.bin": 10,
                    "roms/dreamcast/Fake Racer/track02.raw": 20,
                }
            ),
        )
        self.assertEqual(len(games), 1)
        self.assertEqual(games[0].title, "disc")
        self.assertEqual(len(games[0].parts), 3)

    def test_an_m3u_file_claims_its_discs_and_a_missing_one_is_skipped(self) -> None:
        games = catalog.derive(
            "psx",
            listing(
                {
                    "roms/psx/Sample.m3u": PLAYLIST,
                    "roms/psx/Sample (Disc 1).chd": 10,
                    "roms/psx/Sample (Disc 2).chd": 20,
                }
            ),
        )
        self.assertEqual(len(games), 1)
        self.assertEqual(
            paths(games[0]),
            ["roms/psx/Sample.m3u", "roms/psx/Sample (Disc 1).chd", "roms/psx/Sample (Disc 2).chd"],
        )

    def test_a_cue_naming_a_missing_file_still_claims_the_others(self) -> None:
        cue = 'FILE "a.bin" BINARY\n  TRACK 01 MODE1/2352\nFILE "b.bin" BINARY\n  TRACK 02 AUDIO\n'
        games = catalog.derive("psx", listing({"roms/psx/g.cue": cue, "roms/psx/a.bin": 1}))
        self.assertEqual(paths(games[0]), ["roms/psx/g.cue", "roms/psx/a.bin"])
        self.assertEqual(len(games), 1)

    def test_a_cue_cannot_reach_a_file_that_is_not_below_its_folder(self) -> None:
        cue = 'FILE "../x.bin" BINARY\n  TRACK 01 MODE1/2352\n'
        games = catalog.derive("psx", listing({"roms/psx/sub/g.cue": cue, "roms/psx/x.bin": 1}))
        self.assertEqual(
            sorted(paths(g)[0] for g in games), ["roms/psx/sub/g.cue", "roms/psx/x.bin"]
        )

    def test_the_inner_roms_systems_ignore_the_rest_of_the_folder(self) -> None:
        games = catalog.derive(
            "xbox360",
            listing(
                {
                    "roms/xbox360/roms/Example Disc.iso": 10,
                    "roms/xbox360/content/0000000000000000/save.bin": 5,
                }
            ),
        )
        self.assertEqual([g.primary for g in games], ["roms/xbox360/roms/Example Disc.iso"])
        self.assertEqual(games[0].gamelist, "./roms/Example Disc.iso")

    def test_a_path_that_cannot_be_a_whitelist_line_is_flagged(self) -> None:
        games = catalog.derive(
            "gba",
            listing(
                {
                    "roms/gba/ lead.zip": 1,
                    "roms/gba/new\nline.zip": 1,
                    "roms/gba/in ner.zip": 1,
                }
            ),
        )
        flags = {g.primary: g.flags for g in games}
        self.assertEqual(flags["roms/gba/ lead.zip"], ("unlistable",))
        self.assertEqual(flags["roms/gba/new\nline.zip"], ("unlistable",))
        self.assertEqual(flags["roms/gba/in ner.zip"], ())

    def test_unlistable(self) -> None:
        self.assertTrue(catalog.unlistable("roms/gba/a\nb.zip"))
        self.assertTrue(catalog.unlistable("roms/gba/a\rb.zip"))
        self.assertTrue(catalog.unlistable("roms/gba/a\0b.zip"))
        self.assertTrue(catalog.unlistable("roms/gba /a.zip"))
        self.assertTrue(catalog.unlistable("roms/gba/a.zip "))
        self.assertFalse(catalog.unlistable("roms/gba/a b[1].zip"))


DESKTOP = (
    "[Desktop Entry]\nType=Application\nName=Example Game\n"
    'Exec=launcher "/lib/roms/ps4/games/{}/eboot.bin"\n'
)


class Ps4Tests(unittest.TestCase):
    def test_a_game_folder_and_the_shortcuts_that_run_it(self) -> None:
        games = catalog.derive(
            "ps4",
            listing(
                {
                    "roms/ps4/games/CUSA00001/eboot.bin": 100,
                    "roms/ps4/games/CUSA00001/sce_sys/param.sfo": 10,
                    "roms/ps4/games/CUSA00002/eboot.bin": 50,
                    "roms/ps4/shortcuts/Example Game.desktop": DESKTOP.format("CUSA00001"),
                    "roms/ps4/shortcuts/Other Game.desktop": DESKTOP.format("CUSA99999"),
                }
            ),
        )
        by = by_primary(games)
        first = by["roms/ps4/games/CUSA00001"]
        self.assertEqual(first.title, "Example Game")
        self.assertEqual(
            paths(first), ["roms/ps4/games/CUSA00001", "roms/ps4/shortcuts/Example Game.desktop"]
        )
        self.assertEqual(first.gamelist, "./shortcuts/Example Game.desktop")
        self.assertEqual((first.parts[0].files, first.parts[0].size), (2, 110))
        second = by["roms/ps4/games/CUSA00002"]
        self.assertEqual(second.title, "CUSA00002")
        self.assertEqual(paths(second), ["roms/ps4/games/CUSA00002"])
        self.assertEqual(len(games), 2)

    def test_the_update_and_the_dlc_folders_are_parts_of_the_game(self) -> None:
        games = catalog.derive(
            "ps4",
            listing(
                {
                    "roms/ps4/games/CUSA00001/eboot.bin": 100,
                    "roms/ps4/games/CUSA00001-UPDATE/eboot.bin": 20,
                    "roms/ps4/dlc/CUSA00001/DLCONE/sce_sys/param.sfo": 5,
                    "roms/ps4/dlc/CUSA00001/DLCTWO/sce_sys/param.sfo": 5,
                    "roms/ps4/dlc/CUSA00009/DLCONE/sce_sys/param.sfo": 5,
                    "roms/ps4/shortcuts/Example Game.desktop": DESKTOP.format("CUSA00001"),
                    "roms/ps4/games/CUSA00002-UPDATE/eboot.bin": 7,
                }
            ),
        )
        by = by_primary(games)
        self.assertEqual(
            sorted(by), ["roms/ps4/games/CUSA00001", "roms/ps4/games/CUSA00002-UPDATE"]
        )
        self.assertEqual(
            paths(by["roms/ps4/games/CUSA00001"]),
            [
                "roms/ps4/games/CUSA00001",
                "roms/ps4/games/CUSA00001-UPDATE",
                "roms/ps4/dlc/CUSA00001",
                "roms/ps4/shortcuts/Example Game.desktop",
            ],
        )
        parts = by["roms/ps4/games/CUSA00001"].parts
        self.assertEqual([p.size for p in parts[:3]], [100, 20, 10])

    def test_a_pkg_left_in_the_folder_is_a_game(self) -> None:
        games = catalog.derive("ps4", listing({"roms/ps4/Pkg Game.pkg": 100}))
        self.assertEqual([g.primary for g in games], ["roms/ps4/Pkg Game.pkg"])
        self.assertEqual(games[0].title, "Pkg Game")


class VitaTests(unittest.TestCase):
    def test_app_licence_and_launcher(self) -> None:
        games = catalog.derive(
            "psvita",
            listing(
                {
                    "roms/psvita/app/PCSE00000/eboot.bin": 100,
                    "roms/psvita/app/PCSE00000/sce_sys/param.sfo": 5,
                    "roms/psvita/license/PCSE00000/a.rif": 512,
                    "roms/psvita/Example Game.psvita": "PCSE00000\n",
                    "roms/psvita/app/PCSE11111/eboot.bin": 7,
                }
            ),
        )
        by = by_primary(games)
        full = by["roms/psvita/app/PCSE00000"]
        self.assertEqual(
            paths(full),
            [
                "roms/psvita/app/PCSE00000",
                "roms/psvita/license/PCSE00000",
                "roms/psvita/Example Game.psvita",
            ],
        )
        self.assertEqual(full.title, "Example Game")
        self.assertEqual(full.gamelist, "./Example Game.psvita")
        bare = by["roms/psvita/app/PCSE11111"]
        self.assertEqual(paths(bare), ["roms/psvita/app/PCSE11111"])
        self.assertEqual(bare.title, "PCSE11111")


UPDATES, DLC = "extras/switch/updates", "extras/switch/dlc"


def switch_listing() -> Listing:
    return listing(
        {
            "roms/switch/Example Game [0100AAAA00000000][v0].nsp": 1000,
            "roms/switch/Other Game [0100BBBB00000000][v0].nsp": 2000,
            f"{UPDATES}/Example Game/Example Game [0100AAAA00000800][v65536][UPD].nsp": 10,
            f"{DLC}/Example Game/Example Game - Pack [0100AAAA00001001][v0][DLC].nsp": 20,
            f"{DLC}/Example Game/Example Game - More [0100AAAA00001002][v0][DLC].nsp": 30,
            "extras/switch/updates/Lost Game/Lost Game [0100CCCC00000800][v1][UPD].nsp": 40,
        }
    )


class SwitchTests(unittest.TestCase):
    def test_updates_and_dlc_are_parts_of_their_base_game(self) -> None:
        by = by_primary(catalog.derive("switch", switch_listing()))
        game = by["roms/switch/Example Game [0100AAAA00000000][v0].nsp"]
        self.assertEqual(
            paths(game),
            [
                "roms/switch/Example Game [0100AAAA00000000][v0].nsp",
                "extras/switch/updates/Example Game",
                "extras/switch/dlc/Example Game",
            ],
        )
        self.assertEqual(game.size, 1000 + 10 + 50)
        self.assertEqual(game.title, "Example Game [0100AAAA00000000][v0]")
        self.assertEqual(game.key, "example game")
        other = by["roms/switch/Other Game [0100BBBB00000000][v0].nsp"]
        self.assertEqual(len(other.parts), 1)
        self.assertEqual(len(by), 2)

    def test_a_folder_with_no_base_game_is_unclaimed_and_in_no_game(self) -> None:
        listed = switch_listing()
        self.assertEqual(catalog.unclaimed("switch", listed), ["extras/switch/updates/Lost Game"])
        every = {p for g in catalog.derive("switch", listed) for p in paths(g)}
        self.assertNotIn("extras/switch/updates/Lost Game", every)
        self.assertEqual(catalog.unclaimed("psx", listed), [])


class MergeAndJsonTests(unittest.TestCase):
    def setUp(self) -> None:
        self.derived = catalog.derive(
            "gba", listing({"roms/gba/Alpha.zip": 100, "roms/gba/Beta.zip": 200})
        )
        self.alpha, self.beta = self.derived

    def stored(self, games: list[catalog.Game]) -> dict[str, catalog.Game]:
        return {g.id: g for g in games}

    def hashed(self, game: catalog.Game, sha1: str = "a" * 40) -> catalog.Game:
        return replace(game, parts=tuple(replace(p, sha1=sha1) for p in game.parts))

    def test_new_games_are_added_today_and_unhashed(self) -> None:
        merged = catalog.merge({}, self.derived, today="2026-11-06")
        self.assertEqual([g.added for g in merged], ["2026-11-06"] * 2)
        self.assertEqual(len(catalog.pending_hashes(merged)), 2)

    def test_a_stored_hash_is_kept_while_size_and_mtime_match(self) -> None:
        old = self.stored([self.hashed(self.alpha)])
        merged = catalog.merge(old, self.derived, today="2026-11-06")
        self.assertEqual(merged[0].parts[0].sha1, "a" * 40)
        self.assertEqual([p.path for p in catalog.pending_hashes(merged)], ["roms/gba/Beta.zip"])

    def test_a_changed_file_is_hashed_again(self) -> None:
        changed = catalog.derive("gba", listing({"roms/gba/Alpha.zip": 101}))
        merged = catalog.merge(self.stored([self.hashed(self.alpha)]), changed, today="2026-11-06")
        self.assertIsNone(merged[0].parts[0].sha1)
        moved = [replace(self.alpha, parts=(replace(self.alpha.parts[0], mtime=5),))]
        merged = catalog.merge(self.stored([self.hashed(self.alpha)]), moved, today="2026-11-06")
        self.assertIsNone(merged[0].parts[0].sha1)

    def test_what_only_earlier_runs_know_is_kept_and_vanished_games_are_dropped(self) -> None:
        old = self.stored(
            [
                replace(
                    self.hashed(self.alpha),
                    added="2026-10-01",
                    dat="Alpha (USA)",
                    ra={"hash": "b" * 32, "of": "roms/gba/Alpha.zip", "size": 100, "mtime": 1000},
                ),
                self.hashed(self.beta),
            ]
        )
        merged = catalog.merge(old, self.derived[:1], today="2026-11-06")
        self.assertEqual(len(merged), 1)
        self.assertEqual(merged[0].added, "2026-10-01")
        self.assertEqual(merged[0].dat, "Alpha (USA)")
        self.assertEqual(merged[0].ra and merged[0].ra["hash"], "b" * 32)

    def test_inbox_knowledge_wins_for_the_dat_name_but_not_for_the_date(self) -> None:
        old = self.stored([replace(self.hashed(self.alpha), added="2026-10-01", dat="Old")])
        merged = catalog.merge(
            old, self.derived, today="2026-11-06", dat={"roms/gba/Alpha.zip": "Alpha (USA)"}
        )
        self.assertEqual((merged[0].added, merged[0].dat), ("2026-10-01", "Alpha (USA)"))
        self.assertEqual((merged[1].added, merged[1].dat), ("2026-11-06", None))

    def test_serials_are_kept_set_by_inbox_and_survive_the_json(self) -> None:
        old = self.stored([replace(self.hashed(self.alpha), serials=("T-1N",))])
        kept = catalog.merge(old, self.derived, today="2026-11-06")
        self.assertEqual((kept[0].serials, kept[1].serials), (("T-1N",), ()))
        set_now = catalog.merge(
            old, self.derived, today="2026-11-06", serials={"roms/gba/Alpha.zip": ("T-2N", "T-3")}
        )
        self.assertEqual(set_now[0].serials, ("T-2N", "T-3"))
        doc = catalog.decode(catalog.encode("gba", set_now, "2026-11-06T00:00:00Z"))
        assert doc is not None
        self.assertEqual(doc.games[set_now[0].id].serials, ("T-2N", "T-3"))

    def test_a_file_without_serials_reads_as_none(self) -> None:
        raw = json.loads(catalog.encode("gba", self.derived, "t"))
        for game in raw["games"].values():
            del game["serials"]
        doc = catalog.decode(json.dumps(raw).encode())
        assert doc is not None
        self.assertTrue(all(g.serials == () for g in doc.games.values()))

    def test_fill_hashes_and_diff(self) -> None:
        merged = catalog.merge({}, self.derived, today="2026-11-06")
        filled = catalog.fill_hashes(merged, {"roms/gba/Alpha.zip": "c" * 40})
        self.assertEqual(filled[0].parts[0].sha1, "c" * 40)
        self.assertIsNone(filled[1].parts[0].sha1)
        old = self.stored(filled)
        self.assertFalse(catalog.diff(old, filled))
        delta = catalog.diff(old, [replace(filled[0], title="Renamed")])
        self.assertEqual(
            (delta.added, delta.removed, delta.changed), ((), (filled[1].id,), (filled[0].id,))
        )

    def test_the_json_round_trips_and_is_the_same_bytes_every_time(self) -> None:
        games = catalog.fill_hashes(
            catalog.merge({}, self.derived, today="2026-11-06"),
            {"roms/gba/Alpha.zip": "c" * 40, "roms/gba/Beta.zip": "d" * 40},
        )
        data = catalog.encode("gba", games, "2026-11-06T10:00:00Z")
        self.assertEqual(data, catalog.encode("gba", list(reversed(games)), "2026-11-06T10:00:00Z"))
        self.assertTrue(data.endswith(b"\n"))
        doc = catalog.decode(data)
        assert doc is not None
        self.assertEqual((doc.system, doc.updated), ("gba", "2026-11-06T10:00:00Z"))
        self.assertEqual(doc.games, {g.id: g for g in games})
        raw = json.loads(data)
        self.assertEqual(raw["format"], 1)
        entry = raw["games"][self.alpha.id]
        self.assertEqual(
            entry["parts"][0],
            {
                "path": "roms/gba/Alpha.zip",
                "type": "file",
                "size": 100,
                "mtime": 1000,
                "sha1": "c" * 40,
            },
        )
        self.assertEqual(entry["ra"], None)
        self.assertEqual(entry["flags"], [])

    def test_a_dir_part_has_files_and_no_hash(self) -> None:
        game = catalog.derive(
            "psx", listing({"roms/psx/G.m3u/a.chd": 5, "roms/psx/G.m3u/b.chd": 6})
        )[0]
        part = json.loads(catalog.encode("psx", [game], "t"))["games"][game.id]["parts"][0]
        self.assertEqual(
            part, {"path": "roms/psx/G.m3u", "type": "dir", "size": 11, "files": 2, "mtime": 1000}
        )

    def test_non_ascii_names_are_written_as_utf8(self) -> None:
        game = catalog.derive("gba", listing({"roms/gba/Pokémon.zip": 1}))[0]
        self.assertIn("Pokémon".encode(), catalog.encode("gba", [game], "t"))

    def test_decode_refuses_what_is_not_a_system_file(self) -> None:
        self.assertIsNone(catalog.decode(b"not json"))
        self.assertIsNone(catalog.decode(b"[]"))
        self.assertIsNone(catalog.decode(b'{"format": 2, "system": "x", "games": {}}'))
        self.assertIsNone(catalog.decode(b"\xff\xfe"))

    def test_decode_leaves_out_a_game_that_does_not_parse_or_has_a_wrong_id(self) -> None:
        games = catalog.merge({}, self.derived, today="d")
        raw = json.loads(catalog.encode("gba", games, "t"))
        raw["games"]["0000000000000000"] = raw["games"][self.alpha.id]
        raw["games"][self.beta.id]["parts"] = [{"path": 5}]
        doc = catalog.decode(json.dumps(raw).encode())
        assert doc is not None
        self.assertEqual(list(doc.games), [self.alpha.id])


class DatSerialsTests(unittest.TestCase):
    """Which serials a game gets from the DATs: exact identity only. Fake DATs, in memory."""

    SHA = "c" * 40

    def dat(self, *games: datmod.Game) -> catalog.SerialIndex:
        return catalog.serial_index([datmod.Dat("dreamcast", "dc", list(games))])

    def disc(self, name: str, serials: tuple[str, ...], sha1: str = "") -> datmod.Game:
        return datmod.Game(name, (datmod.Rom(f"{name}.bin", 10, "0" * 8, sha1),), serials)

    def game(self, primary: str, *, dat: str | None = None, sha1: str | None = None):  # type: ignore[no-untyped-def]
        built = catalog.derive("dreamcast", listing({primary: 10}))[0]
        part = replace(built.parts[0], sha1=sha1) if built.parts[0].type == catalog.FILE else None
        return replace(built, dat=dat, parts=(part,) if part else built.parts)

    def test_the_stored_dat_name_is_looked_up_exactly(self) -> None:
        index = self.dat(self.disc("Game (USA)", ("T-1",)))
        by_name = self.game("roms/dreamcast/Other.chd", dat="Game (USA)")
        self.assertEqual(catalog.dat_serials(by_name, index), ("T-1",))
        for wrong in ("game (usa)", "Game (USA) ", "Game (Europe)"):
            self.assertIsNone(
                catalog.dat_serials(self.game("roms/dreamcast/Other.chd", dat=wrong), index), wrong
            )

    def test_a_single_file_is_found_by_its_exact_name_without_the_extension(self) -> None:
        index = self.dat(self.disc("Game (USA)", ("T-1",)))
        for name in ("Game (USA).chd", "Game (USA).iso", "Game (USA).cue"):
            self.assertEqual(
                catalog.dat_serials(self.game(f"roms/dreamcast/{name}"), index), ("T-1",), name
            )
        for name in ("game (usa).chd", "Game (USA) .chd", "Game (USA) (Rev A).chd", "Game.chd"):
            self.assertIsNone(catalog.dat_serials(self.game(f"roms/dreamcast/{name}"), index), name)
        nested = self.game("roms/dreamcast/sub/Game (USA).chd")
        self.assertEqual(catalog.dat_serials(nested, index), ("T-1",))

    def test_a_file_name_with_two_serial_lists_is_not_guessed(self) -> None:
        index = self.dat(
            self.disc("Game (USA)", ("T-1",)), self.disc("Game (USA)", ("T-2",), "e" * 40)
        )
        self.assertIsNone(catalog.dat_serials(self.game("roms/dreamcast/Game (USA).chd"), index))

    def test_the_hash_wins_over_the_file_name(self) -> None:
        index = self.dat(self.disc("Game (USA)", ("T-1",)), self.disc("Other", ("T-2",), self.SHA))
        game = self.game("roms/dreamcast/Game (USA).iso", sha1=self.SHA)
        self.assertEqual(catalog.dat_serials(game, index), ("T-2",))

    def test_the_sha1_and_size_of_the_primary_file_find_a_one_rom_game(self) -> None:
        index = self.dat(self.disc("Game (USA)", ("T-1",), self.SHA))
        game = self.game("roms/dreamcast/Whatever.iso", sha1=self.SHA)
        self.assertEqual(catalog.dat_serials(game, index), ("T-1",))
        other_size = replace(game, parts=(replace(game.parts[0], size=11),))
        self.assertIsNone(catalog.dat_serials(other_size, index))
        self.assertIsNone(
            catalog.dat_serials(self.game("roms/dreamcast/W.iso", sha1="d" * 40), index)
        )
        self.assertIsNone(catalog.dat_serials(self.game("roms/dreamcast/W.iso"), index))

    def test_a_game_of_several_roms_is_not_found_by_one_of_them(self) -> None:
        rom = datmod.Rom("t1.bin", 10, "0" * 8, self.SHA)
        two = datmod.Game("Two Tracks", (rom, datmod.Rom("t2.bin", 5, "1" * 8, "e" * 40)), ("T-2",))
        game = self.game("roms/dreamcast/W.iso", sha1=self.SHA)
        self.assertIsNone(catalog.dat_serials(game, self.dat(two)))

    def test_the_name_wins_over_the_hash_and_a_missing_name_falls_back_to_it(self) -> None:
        index = self.dat(self.disc("Named", ("T-1",)), self.disc("Hashed", ("T-2",), self.SHA))
        named = self.game("roms/dreamcast/W.iso", dat="Named", sha1=self.SHA)
        self.assertEqual(catalog.dat_serials(named, index), ("T-1",))
        gone = self.game("roms/dreamcast/W.iso", dat="Not in this DAT", sha1=self.SHA)
        self.assertEqual(catalog.dat_serials(gone, index), ("T-2",))

    def test_a_key_with_two_different_answers_gives_none(self) -> None:
        index = self.dat(
            self.disc("Same Name", ("T-1",), self.SHA), self.disc("Same Name", ("T-2",), self.SHA)
        )
        self.assertIsNone(
            catalog.dat_serials(self.game("roms/dreamcast/W.iso", dat="Same Name"), index)
        )
        self.assertIsNone(
            catalog.dat_serials(self.game("roms/dreamcast/W.iso", sha1=self.SHA), index)
        )
        twice = self.dat(self.disc("Same Name", ("T-1",)), self.disc("Same Name", ("T-1",)))
        self.assertEqual(
            catalog.dat_serials(self.game("roms/dreamcast/W.iso", dat="Same Name"), twice), ("T-1",)
        )

    def test_the_index_says_whether_any_dat_game_has_serials(self) -> None:
        index = self.dat(self.disc("Bare", ()))
        self.assertFalse(index)
        self.assertTrue(self.dat(self.disc("Has", ("T-1",))))

    def test_an_m3u_gets_the_union_of_its_discs_and_needs_all_of_them(self) -> None:
        index = self.dat(
            self.disc("Two (Disc 1)", ("T-1", "T-1B")), self.disc("Two (Disc 2)", ("T-2", "T-1"))
        )
        folder = catalog.derive("dreamcast", listing({"roms/dreamcast/Two.m3u/Two.m3u": "x"}))[0]
        self.assertEqual(folder.parts[0].type, catalog.DIR)
        discs = ["Two (Disc 1)", "Two (Disc 2)"]
        self.assertEqual(catalog.dat_serials(folder, index, discs), ("T-1", "T-1B", "T-2"))
        self.assertIsNone(catalog.dat_serials(folder, index, [*discs, "Two (Disc 3)"]))
        self.assertIsNone(catalog.dat_serials(folder, index, ["two (disc 1)"]))
        self.assertIsNone(catalog.dat_serials(folder, index))

    def test_disc_names_come_from_the_playlist_entries(self) -> None:
        text = "# comment\nTwo (Disc 1).chd\n\nsub\\Mr. Two (v1.1) (Disc 2).chd\n"
        self.assertEqual(catalog.m3u_disc_names(text), ["Two (Disc 1)", "Mr. Two (v1.1) (Disc 2)"])

    def test_a_game_the_dat_knows_without_a_serial_is_identified_with_none(self) -> None:
        index = self.dat(
            self.disc("Bare (USA)", (), self.SHA), self.disc("Has (USA)", ("T-1",), "d" * 40)
        )
        by_dat = self.game("roms/dreamcast/W.chd", dat="Bare (USA)")
        by_hash = self.game("roms/dreamcast/W.iso", sha1=self.SHA)
        by_file = self.game("roms/dreamcast/Bare (USA).chd")
        for game in (by_dat, by_hash, by_file):
            self.assertEqual(catalog.identify(game, index), (), game.primary)
            self.assertIsNone(catalog.dat_serials(game, index), game.primary)
        unknown = self.game("roms/dreamcast/Nope.chd", dat="Nope (USA)")
        self.assertIsNone(catalog.identify(unknown, index))

    def test_a_serial_among_duplicates_without_one_is_the_answer(self) -> None:
        index = self.dat(self.disc("Dup", ()), self.disc("Dup", ("T-1",), "d" * 40))
        self.assertEqual(catalog.identify(self.game("roms/dreamcast/Dup.chd"), index), ("T-1",))

    def test_discs_the_dat_knows_without_serials_give_none_but_a_missing_disc_is_unmatched(
        self,
    ) -> None:
        index = self.dat(self.disc("Two (Disc 1)", ()), self.disc("Two (Disc 2)", ()))
        folder = catalog.derive("dreamcast", listing({"roms/dreamcast/Two.m3u/Two.m3u": "x"}))[0]
        self.assertEqual(catalog.identify(folder, index, ["Two (Disc 1)", "Two (Disc 2)"]), ())
        self.assertIsNone(catalog.identify(folder, index, ["Two (Disc 1)", "Two (Disc 3)"]))

    def test_fill_serials_tells_no_serial_in_the_dat_from_unmatched(self) -> None:
        index = self.dat(self.disc("Found", ("T-1",)), self.disc("Bare", ()))
        found = self.game("roms/dreamcast/A.chd", dat="Found")
        bare = self.game("roms/dreamcast/B.chd", dat="Bare")
        lost = self.game("roms/dreamcast/C.chd", dat="Nope")
        plan = catalog.fill_serials([found, bare, lost], index, {})
        self.assertEqual(
            (plan.filled, plan.no_serial, plan.unmatched), ((found.id,), (bare.id,), (lost.id,))
        )
        self.assertEqual([g.serials for g in plan.games], [("T-1",), (), ()])

    def test_force_keeps_the_serials_of_a_game_the_dat_lists_none_for(self) -> None:
        index = self.dat(self.disc("Bare", ()))
        game = replace(self.game("roms/dreamcast/B.chd", dat="Bare"), serials=("OLD",))
        plan = catalog.fill_serials([game], index, {}, force=True)
        self.assertEqual((plan.had, plan.no_serial, plan.filled), ((game.id,), (), ()))
        self.assertEqual(plan.games[0].serials, ("OLD",))

    def test_fill_serials_sorts_games_into_filled_had_and_unmatched(self) -> None:
        index = self.dat(self.disc("Found", ("T-1",)), self.disc("Had", ("T-9",)))
        found = self.game("roms/dreamcast/A.chd", dat="Found")
        lost = self.game("roms/dreamcast/B.chd", dat="Nope")
        had = replace(self.game("roms/dreamcast/C.chd", dat="Had"), serials=("OLD",))
        plan = catalog.fill_serials([found, lost, had], index, {})
        self.assertEqual(
            (plan.filled, plan.had, plan.unmatched), ((found.id,), (had.id,), (lost.id,))
        )
        self.assertEqual([g.serials for g in plan.games], [("T-1",), (), ("OLD",)])

    def test_force_replaces_serials_on_an_exact_match_and_keeps_them_otherwise(self) -> None:
        index = self.dat(self.disc("Had", ("T-9",)), self.disc("Same", ("T-3",)))
        changed = replace(self.game("roms/dreamcast/A.chd", dat="Had"), serials=("OLD",))
        same = replace(self.game("roms/dreamcast/B.chd", dat="Same"), serials=("T-3",))
        gone = replace(self.game("roms/dreamcast/C.chd", dat="Nope"), serials=("KEEP",))
        plan = catalog.fill_serials([changed, same, gone], index, {}, force=True)
        self.assertEqual((plan.filled, plan.had), ((changed.id,), (same.id, gone.id)))
        self.assertEqual([g.serials for g in plan.games], [("T-9",), ("T-3",), ("KEEP",)])
        self.assertEqual(plan.unmatched, ())


class SearchTests(unittest.TestCase):
    def games(self) -> list[tuple[str, catalog.Game]]:
        def game(system: str, title: str) -> tuple[str, catalog.Game]:
            return system, catalog.derive(system, listing({f"roms/{system}/{title}.zip": 1}))[0]

        return [
            game("gbc", "Metal Gear Solid (USA)"),
            game("psx", "Metal Gear Solid (Europe)"),
            game("psx", "Metal Gear Solid 2"),
            game("psx", "Gear Fighter"),
            game("nes", "Metal Storm (USA)"),
            game("nes", "Ultimate Metal Gear Tribute"),
        ]

    def test_words_are_prefixes_and_groups_are_ranked(self) -> None:
        groups = catalog.search("metal gear", self.games())
        self.assertEqual(
            [g.key for g in groups],
            ["metal gear solid", "metal gear solid 2", "ultimate metal gear tribute"],
        )
        first = groups[0]
        self.assertEqual(first.title, "Metal Gear Solid (USA)")
        self.assertEqual([s for s, _ in first.games], ["gbc", "psx"])

    def test_an_exact_key_ranks_first(self) -> None:
        groups = catalog.search("Metal Gear Solid", self.games())
        self.assertEqual(groups[0].key, "metal gear solid")

    def test_word_order_does_not_matter_and_prefixes_do(self) -> None:
        self.assertEqual(
            [g.key for g in catalog.search("gear met", self.games())],
            ["metal gear solid", "metal gear solid 2", "ultimate metal gear tribute"],
        )
        self.assertEqual(catalog.search("zzz", self.games()), [])
        self.assertEqual(catalog.search("   ", self.games()), [])

    def test_at_most_limit_groups(self) -> None:
        self.assertEqual(len(catalog.search("metal", self.games(), limit=2)), 2)


class RaTargetTests(unittest.TestCase):
    def targets(self, system: str, files: dict[str, int | str]):  # type: ignore[no-untyped-def]
        listed = listing(files)
        games = catalog.derive(system, listed)
        found = catalog.ra_targets(system, games, listed)
        return {g.primary: found[g.id] for g in games if g.id in found}, games

    def test_a_file_game_is_hashed_by_its_primary_part(self) -> None:
        targets, _ = self.targets("psx", {"roms/psx/A.chd": 10})
        self.assertEqual(targets["roms/psx/A.chd"], catalog.RaTarget("roms/psx/A.chd", 10, 1000))

    def test_an_m3u_folder_is_hashed_by_its_first_disc(self) -> None:
        targets, _ = self.targets(
            "psx",
            {
                "roms/psx/G.m3u/G (Disc 2).chd": 20,
                "roms/psx/G.m3u/G (Disc 1).chd": 10,
                "roms/psx/G.m3u/G.m3u": "G (Disc 1).chd\nG (Disc 2).chd\n",
            },
        )
        self.assertEqual(
            targets["roms/psx/G.m3u"], catalog.RaTarget("roms/psx/G.m3u/G (Disc 1).chd", 10, 1000)
        )

    def test_the_playlist_decides_which_disc_is_first_not_the_name(self) -> None:
        targets, _ = self.targets(
            "psx",
            {
                "roms/psx/G.m3u/a.chd": 20,
                "roms/psx/G.m3u/b.chd": 10,
                "roms/psx/G.m3u/G.m3u": "b.chd\na.chd\n",
            },
        )
        self.assertEqual(targets["roms/psx/G.m3u"].of, "roms/psx/G.m3u/b.chd")

    def test_an_m3u_folder_without_a_usable_playlist_has_no_target(self) -> None:
        targets, games = self.targets(
            "psx", {"roms/psx/G.m3u/a.chd": 20, "roms/psx/G.m3u/G.m3u": "missing.chd\n"}
        )
        self.assertEqual((len(games), targets), (1, {}))

    def test_a_system_ra_does_not_know_has_no_targets(self) -> None:
        targets, games = self.targets("switch", {"roms/switch/A [0100000000000000].nsp": 10})
        self.assertEqual((len(games), targets), (1, {}))

    def test_a_cue_game_is_hashed_by_its_cue(self) -> None:
        targets, _ = self.targets(
            "psx", {"roms/psx/A.cue": 'FILE "A.bin" BINARY\n', "roms/psx/A.bin": 100}
        )
        self.assertEqual(list(targets), ["roms/psx/A.cue"])

    def test_pending_means_no_hash_or_a_hash_of_a_part_that_changed(self) -> None:
        listed = listing({"roms/gba/A.zip": 10, "roms/gba/B.zip": 10, "roms/gba/C.zip": 10})
        games = catalog.derive("gba", listed)
        targets = catalog.ra_targets("gba", games, listed)
        by = by_primary(games)
        fresh = catalog.ra_entry("a" * 32, targets[by["roms/gba/A.zip"].id])
        stale = {**fresh, "size": 11}
        games = catalog.with_ra(
            games, {by["roms/gba/A.zip"].id: fresh, by["roms/gba/B.zip"].id: stale}
        )
        self.assertEqual(
            [g.primary for g in catalog.ra_pending(games, targets)],
            ["roms/gba/B.zip", "roms/gba/C.zip"],
        )

    def test_ra_survives_the_json_round_trip_and_a_merge(self) -> None:
        listed = listing({"roms/gba/A.zip": 10})
        games = catalog.derive("gba", listed)
        targets = catalog.ra_targets("gba", games, listed)
        entry = catalog.ra_entry("a" * 32, targets[games[0].id])
        with_ra = catalog.with_ra(games, {games[0].id: entry})
        doc = catalog.decode(catalog.encode("gba", with_ra, "2026-11-06T00:00:00Z"))
        assert doc is not None
        self.assertEqual(
            doc.games[games[0].id].ra,
            {"hash": "a" * 32, "of": "roms/gba/A.zip", "size": 10, "mtime": 1000},
        )
        merged = catalog.merge(doc.games, games, today="2026-11-07")
        self.assertEqual(merged[0].ra, doc.games[games[0].id].ra)


RA_H = "a" * 32
RA_LISTS = {
    12: catalog.RaList(
        12,
        "2026-11-01T00:00:00Z",
        {RA_H: ra.RaGame("Example", 20, 77, 200), "b" * 32: ra.RaGame("Empty", 0, 78, 0)},
    )
}


class RaStateTests(unittest.TestCase):
    H = RA_H
    LISTS = RA_LISTS

    def state(self, system: str, stored, lists=None):  # type: ignore[no-untyped-def]
        return catalog.ra_state(system, stored, self.LISTS if lists is None else lists)

    def test_supported_carries_count_points_and_link(self) -> None:
        got = self.state("psx", {"hash": self.H})
        self.assertEqual(
            (got.state, got.title, got.achievements, got.points, got.url),
            ("supported", "Example", 20, 200, "https://retroachievements.org/game/77"),
        )

    def test_linked_is_a_known_hash_without_achievements(self) -> None:
        self.assertEqual(self.state("psx", {"hash": "b" * 32}).state, "linked")

    def test_not_linked_is_a_hash_the_list_does_not_have(self) -> None:
        self.assertEqual(self.state("psx", {"hash": "c" * 32}).state, "not-linked")

    def test_no_hash_when_the_list_is_there_and_the_game_has_none(self) -> None:
        self.assertEqual(self.state("psx", None).state, "no-hash")

    def test_no_list_for_a_console_without_a_cached_list_or_outside_the_table(self) -> None:
        self.assertEqual(self.state("gba", {"hash": self.H}).state, "no-list")
        self.assertEqual(self.state("switch", None).state, "no-list")
        self.assertEqual(self.state("psx", {"hash": self.H}, {}).state, "no-list")

    def test_the_hash_is_compared_in_lower_case(self) -> None:
        self.assertEqual(self.state("psx", {"hash": self.H.upper()}).state, "supported")


class RaListTests(unittest.TestCase):
    def test_the_list_round_trips_with_ids_and_points(self) -> None:
        games = {"a" * 32: ra.RaGame("Example", 20, 77, 200)}
        data = catalog.encode_ra_list(12, games, "2026-11-06T10:00:00Z")
        self.assertEqual(
            json.loads(data),
            {
                "format": 1,
                "console": 12,
                "fetched": "2026-11-06T10:00:00Z",
                "games": {
                    "a" * 32: {"id": 77, "title": "Example", "achievements": 20, "points": 200}
                },
            },
        )
        self.assertEqual(data, catalog.encode_ra_list(12, games, "2026-11-06T10:00:00Z"))
        decoded = catalog.decode_ra_list(data)
        assert decoded is not None
        self.assertEqual((decoded.console, decoded.fetched), (12, "2026-11-06T10:00:00Z"))
        self.assertEqual(decoded.games, games)

    def test_what_is_not_a_list_decodes_to_none(self) -> None:
        for data in (b"", b"[]", b"\xff", b'{"format": 2}', b'{"format": 1, "console": 12}'):
            self.assertIsNone(catalog.decode_ra_list(data), data)

    def test_a_list_is_fresh_for_seven_days(self) -> None:
        at = "2026-11-01T00:00:00Z"
        self.assertTrue(catalog.ra_list_fresh(at, datetime(2026, 11, 1, tzinfo=UTC)))
        self.assertTrue(catalog.ra_list_fresh(at, datetime(2026, 11, 7, 23, tzinfo=UTC)))
        self.assertFalse(catalog.ra_list_fresh(at, datetime(2026, 11, 8, tzinfo=UTC)))
        self.assertFalse(catalog.ra_list_fresh(at, datetime(2026, 11, 30, tzinfo=UTC)))

    def test_a_time_that_does_not_parse_or_is_in_the_future_is_stale(self) -> None:
        now = datetime(2026, 11, 6, tzinfo=UTC)
        self.assertFalse(catalog.ra_list_fresh("yesterday", now))
        self.assertFalse(catalog.ra_list_fresh("2026-11-09T00:00:00Z", now))


if __name__ == "__main__":
    unittest.main()
