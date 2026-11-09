"""Saves per game: the pure matching of names to games, the listing of `saves/` and the
`saves` field of the game answer."""

import os
import tempfile
import unittest
from pathlib import Path

from test_serve import EXAMPLE, PS4, ServeCase
from warpzone import saves_index as si
from warpzone.saves_index import Entry, GameSaves

T = 1_700_000_000


def entries(*paths: str, mtime: int = T) -> list[Entry]:
    return [Entry(tuple(p.split("/")), mtime) for p in paths]


def find(
    listing: list[Entry],
    system: str,
    primary: str,
    others: tuple[tuple[str, str], ...] = (),
) -> GameSaves:
    ctx = si.context_of([(system, primary), *others])
    return si.match_game(si.build_index(listing), ctx, system, primary)


class RetroArchTests(unittest.TestCase):
    def test_a_save_and_states_match_the_rom_stem(self) -> None:
        name = "Fake Game (USA)"
        listing = [
            *entries(f"retroarch/saves/{name}.srm", f"retroarch/saves/{name}.rtc"),
            *entries(f"retroarch/states/{name}.state", mtime=T + 50),
            *entries(f"retroarch/states/{name}.state1", f"retroarch/states/{name}.state.auto"),
            *entries(f"retroarch/states/{name}.state1.png", f"retroarch/states/{name}.state.png"),
        ]
        found = find(listing, "gba", f"roms/gba/{name}.gba")
        self.assertEqual(found, GameSaves(True, 3, T + 50, ("retroarch",)))

    def test_states_alone_are_not_save_data(self) -> None:
        found = find(entries("retroarch/states/Fake.state"), "nes", "roms/nes/Fake.zip")
        self.assertEqual((found.data, found.states), (False, 1))

    def test_a_core_folder_is_looked_into(self) -> None:
        listing = entries("retroarch/saves/Core/Fake.srm", "retroarch/states/Core/Fake.state2")
        found = find(listing, "snes", "roms/snes/Fake.sfc")
        self.assertEqual((found.data, found.states), (True, 1))

    def test_a_cue_chd_and_m3u_folder_use_the_name_retroarch_loads(self) -> None:
        for primary in (
            "roms/psx/Fake Disc.cue",
            "roms/psx/Fake Disc.chd",
            "roms/psx/Fake Disc.m3u",  # the folder of a multi-disc game
        ):
            found = find(entries("retroarch/saves/Fake Disc.srm"), "psx", primary)
            self.assertTrue(found.data, primary)

    def test_only_the_exact_stem_matches(self) -> None:
        listing = entries("retroarch/saves/Fake Game 2.srm", "retroarch/saves/Fake.srm.bak")
        self.assertEqual(find(listing, "gba", "roms/gba/Fake Game.gba"), GameSaves())
        self.assertEqual(find(listing, "gba", "roms/gba/fake game 2.gba"), GameSaves())
        self.assertEqual(find(listing, "gba", "roms/gba/Fake.gba"), GameSaves())

    def test_a_stem_shared_by_two_systems_is_nobodys(self) -> None:
        listing = entries("retroarch/saves/Same Name.srm")
        other = (("gbc", "roms/gbc/Same Name.gbc"),)
        self.assertEqual(find(listing, "gb", "roms/gb/Same Name.gb", other), GameSaves())

    def test_a_folder_named_after_the_system_settles_it(self) -> None:
        listing = entries("retroarch/saves/gb/Same Name.srm")
        gb = find(listing, "gb", "roms/gb/Same Name.gb", (("gbc", "roms/gbc/Same Name.gbc"),))
        gbc = find(listing, "gbc", "roms/gbc/Same Name.gbc", (("gb", "roms/gb/Same Name.gb"),))
        self.assertTrue(gb.data)
        self.assertEqual(gbc, GameSaves())

    def test_standalone_systems_never_match_a_rom_stem(self) -> None:
        listing = entries("retroarch/saves/Fake.srm")
        self.assertEqual(find(listing, "switch", "roms/switch/Fake.nsp"), GameSaves())


class StandaloneTests(unittest.TestCase):
    def test_melonds_matches_the_rom_stem_for_nds_only(self) -> None:
        listing = entries(
            "melonds/saves/Fake DS.sav", "melonds/states/Fake DS.ml1", "melonds/states/Fake DS.ml2"
        )
        found = find(listing, "nds", "roms/nds/Fake DS.nds")
        self.assertEqual(found, GameSaves(True, 2, T, ("melonds",)))
        self.assertEqual(find(listing, "gba", "roms/gba/Fake DS.gba"), GameSaves())

    def test_flycast_matches_per_game_cards_and_arcade_flash_not_the_shared_ones(self) -> None:
        listing = entries(
            "flycast/saves/Fake DC_vmu_save_A1.bin",
            "flycast/saves/vmu_save_A1.bin",
            "flycast/saves/dc_nvmem.bin",
            "flycast/saves/fakearc.nvmem",
        )
        self.assertTrue(find(listing, "dreamcast", "roms/dreamcast/Fake DC.chd").data)
        self.assertTrue(find(listing, "naomi", "roms/naomi/fakearc.zip").data)
        self.assertEqual(find(listing, "dreamcast", "roms/dreamcast/vmu_save_A1.chd"), GameSaves())
        self.assertEqual(find(listing, "dreamcast", "roms/dreamcast/dc_nvmem.chd"), GameSaves())

    def test_vita3k_matches_the_title_folder(self) -> None:
        listing = entries("vita3k/saves/PCSE00000", mtime=T + 5) + entries("vita3k/saves/PCSE11111")
        found = find(listing, "psvita", "roms/psvita/app/PCSE00000")
        self.assertEqual(found, GameSaves(True, 0, T + 5, ("vita3k",)))
        self.assertEqual(find(listing, "psvita", "roms/psvita/app/PCSE99999"), GameSaves())

    def test_shadps4_matches_the_cusa_folder_under_a_user(self) -> None:
        listing = entries("shadps4/saves/1/CUSA00000")
        self.assertTrue(find(listing, "ps4", "roms/ps4/games/CUSA00000").data)
        self.assertTrue(find(listing, "ps4", "roms/ps4/games/CUSA00000-UPDATE").data)
        self.assertEqual(find(listing, "ps4", "roms/ps4/games/CUSA00001"), GameSaves())

    def test_eden_matches_the_title_id_in_the_file_name(self) -> None:
        folder = "eden/saves/0000000000000000/00000000000000000000000000000001/0100ABCDEF000000"
        base = "roms/switch/Fake [0100abcdef000000][v0][BASE].nsp"
        found = find(entries(folder), "switch", base)
        self.assertEqual((found.data, found.emulators), (True, ("eden",)))
        self.assertEqual(find(entries(folder), "switch", "roms/switch/Fake.nsp"), GameSaves())

    def test_emulators_without_a_reliable_name_match_nothing(self) -> None:
        listing = entries(
            "pcsx2/saves/Mcd001.ps2",
            "duckstation/saves/shared_card_1.mcd",
            "dolphin/GC/USA/Card A/01-GFZE-Fake.gci",
            "ppsspp/saves/ULUS10000DATA00/DATA.BIN",
            "ryujinx/user/0000000000000001/Fake",
            "mame/saves/fakearc/fakearc.nv",
            "rpcs3/saves/BLUS30000-AUTOSAVE/PARAM.SFO",
        )
        for system, primary in (
            ("ps2", "roms/ps2/Fake.chd"),
            ("psx", "roms/psx/Fake.chd"),
            ("gc", "roms/gc/Fake.rvz"),
            ("psp", "roms/psp/Fake.iso"),
            ("mame", "roms/mame/fakearc.zip"),
            ("ps3", "roms/ps3/Fake.ps3"),
        ):
            self.assertEqual(find(listing, system, primary), GameSaves(), system)

    def test_the_answer_shape(self) -> None:
        self.assertEqual(
            si.saves_response(GameSaves()),
            {"data": False, "states": 0, "newest": None, "emulators": []},
        )
        self.assertEqual(
            si.saves_response(GameSaves(True, 2, 1_700_000_000, ("retroarch",))),
            {
                "data": True,
                "states": 2,
                "newest": "2023-11-14T22:13:20Z",
                "emulators": ["retroarch"],
            },
        )


class ScanTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        self.saves = self.tmp / "saves"

    def put(self, rel: str, mtime: int = T) -> Path:
        path = self.saves / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("x")
        os.utime(path, (mtime, mtime))
        return path

    def test_it_lists_files_and_the_newest_file_of_a_title_folder(self) -> None:
        self.put("retroarch/saves/Fake.srm")
        self.put("retroarch/saves/Core/Fake.srm")
        self.put("retroarch/saves/Core/Deeper/Still/Fake.srm")  # past the depth
        self.put("shadps4/saves/1/CUSA00000/SAVEDATA00/a", T + 10)
        self.put("shadps4/saves/1/CUSA00000/SAVEDATA01/b", T + 30)
        self.put("shadps4/saves/1/CUSA00001/.hidden")
        (self.saves / "vita3k/saves/PCSE00000").mkdir(parents=True)  # empty: no save
        got = {e.path: e.mtime for e in si.scan(self.saves)}
        self.assertEqual(
            got,
            {
                ("retroarch", "saves", "Fake.srm"): T,
                ("retroarch", "saves", "Core", "Fake.srm"): T,
                ("shadps4", "saves", "1", "CUSA00000"): T + 30,
            },
        )

    def test_symlinks_are_never_followed(self) -> None:
        outside = self.tmp / "outside"
        outside.mkdir()
        (outside / "Fake.srm").write_text("x")
        (outside / "Leaked.srm").write_text("x")
        self.put("retroarch/saves/Real.srm")
        (self.saves / "retroarch/saves/Link.srm").symlink_to(outside / "Fake.srm")
        (self.saves / "retroarch/saves/Folder").symlink_to(outside)
        title = self.saves / "vita3k/saves/PCSE00000"
        title.mkdir(parents=True)
        (title / "link").symlink_to(outside / "Leaked.srm")
        (self.saves / "vita3k/saves/PCSE11111").symlink_to(outside)
        got = [e.path for e in si.scan(self.saves)]
        self.assertEqual(got, [("retroarch", "saves", "Real.srm")])

    def test_a_missing_saves_folder_is_empty(self) -> None:
        self.assertEqual(si.scan(self.saves), [])

    def test_the_index_is_listed_again_only_after_the_ttl(self) -> None:
        now = [0.0]
        cache = si.SavesCache(self.saves, ttl=60, clock=lambda: now[0])
        self.assertEqual(cache.index().by_stem, {})
        self.put("retroarch/saves/Fake.srm")
        now[0] = 59
        self.assertEqual(cache.index().by_stem, {})
        now[0] = 61
        self.assertIn("Fake", cache.index().by_stem)


class ApiTests(ServeCase):
    def put(self, rel: str, mtime: int = T) -> None:
        path = self.root / "saves" / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("x")
        os.utime(path, (mtime, mtime))

    def listing(self) -> list[str]:
        return sorted(p.relative_to(self.root).as_posix() for p in (self.root / "saves").rglob("*"))

    def test_a_game_without_saves_has_an_empty_object(self) -> None:
        found = self.get(f"/api/v1/games/{EXAMPLE.id}").json
        self.assertEqual(
            found["saves"], {"data": False, "states": 0, "newest": None, "emulators": []}
        )

    def test_the_game_answer_reports_its_saves(self) -> None:
        self.put("retroarch/saves/Example Game (Europe).srm", T)
        self.put("retroarch/states/Example Game (Europe).state", T + 60)
        self.put("retroarch/states/Example Game (Europe).state1", T + 30)
        self.put("retroarch/saves/Other Game.srm", T + 999)
        self.put("shadps4/saves/1/CUSA00000/SAVEDATA00/data", T + 5)
        self.assertEqual(
            self.get(f"/api/v1/games/{EXAMPLE.id}").json["saves"],
            {
                "data": True,
                "states": 2,
                "newest": "2023-11-14T22:14:20Z",
                "emulators": ["retroarch"],
            },
        )
        self.assertEqual(
            self.get(f"/api/v1/games/{PS4.id}").json["saves"],
            {
                "data": True,
                "states": 0,
                "newest": "2023-11-14T22:13:25Z",
                "emulators": ["shadps4"],
            },
        )

    def test_reading_saves_writes_nothing(self) -> None:
        self.put("retroarch/saves/Example Game (Europe).srm")
        before = self.listing()
        self.get(f"/api/v1/games/{EXAMPLE.id}")
        self.assertEqual(self.listing(), before)


if __name__ == "__main__":
    unittest.main()
