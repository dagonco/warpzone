import hashlib
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from inbox_support import make_stub
from switch_support import (
    KEYS_SECRET,
    KEYS_TEXT,
    NSZ,
    entry,
    make_nsp,
    make_nsp_multi,
    make_nsz,
    pfs0,
    write_titledb,
)
from test_inbox import Base
from warpzone import inbox_switch as sw
from warpzone.config import InboxSettings
from warpzone.inbox import InboxError, Tools, whitelist_line

MK8 = "0100152000022000"  # a base game
MK8_UPD = "0100152000022800"
MK8_DLC = "0100152000023001"
PACK = "0100B0401174300C"  # a bundle, then its parts
PART1, PART2, PART3 = "0100B04011743014", "0100B04011743017", "0100B0401174301E"
TITLEDB = {
    "US.en.json": [
        entry(MK8, "MARIO KART™ 8 DELUXE"),
        entry(MK8_DLC, "Wave 1"),
        entry("0100B04011742000", "Monster Hunter Rise"),
        entry(PACK, "MONSTER HUNTER RISE Deluxe Kit", [PACK, PART1, PART2, PART3]),
        entry("0100CEA007D08000", "Crypt of the NecroDancer: Nintendo Switch Edition"),
        entry("0100CEA007D09001", "Crypt of the NecroDancer: Synchrony"),
    ],
    "GB.en.json": [entry("0100000000001000", "Only in Britain"), entry(PART1, "Wrong region")],
}


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class PureTests(unittest.TestCase):
    def test_kind_from_the_title_id(self) -> None:
        self.assertEqual(sw.classify(MK8), "BASE")
        self.assertEqual(sw.classify(MK8_UPD), "UPD")
        self.assertEqual(sw.classify(MK8_DLC), "DLC")
        self.assertEqual(sw.classify("0100B0401174301E"), "DLC")

    def test_the_game_behind_an_update_or_dlc(self) -> None:
        self.assertEqual(sw.base_title_id(MK8), MK8)
        self.assertEqual(sw.base_title_id(MK8_UPD), MK8)
        self.assertEqual(sw.base_title_id(MK8_DLC), MK8)
        self.assertEqual(sw.base_title_id("01006F80023273E8"), "01006F8002326000")
        self.assertEqual(sw.base_title_id("0100B04011743121"), "0100B04011742000")
        self.assertEqual(sw.base_title_id("0100C1F0051B6800"), "0100C1F0051B6000")

    def test_ids_from_a_file_name(self) -> None:
        self.assertEqual(
            sw.ids_from_filename("x [0100c1f0051b6800][v131072][UPD].nsp"),
            (
                "0100C1F0051B6800",
                131072,
            ),
        )
        self.assertIsNone(sw.ids_from_filename("Some Game.nsp"))
        self.assertIsNone(sw.ids_from_filename("Game [0100C1F0051B6800].nsp"))

    def test_text_is_normalized_the_way_the_library_is(self) -> None:
        cases = {
            "Mario Kart™ 8 Deluxe": "Mario Kart 8 Deluxe",
            "Pokémon™ Legends: Z-A": "Pokémon Legends - Z-A",
            "Luigi’s Mansion™ 3": "Luigi's Mansion 3",  # noqa: RUF001
            "“Ace Pilot” Cohoot outfit": "Ace Pilot Cohoot outfit",
            "Ori – and the Will": "Ori - and the Will",  # noqa: RUF001
            "MARIO + RABBIDS SPARKS OF HOPE": "Mario + Rabbids Sparks of Hope",
            "THE LEGEND OF ZELDA": "The Legend of Zelda",
            "DRAGON QUEST XI S": "Dragon Quest XI S",
            "Rabbid Mario PIT Stoppers": "Rabbid Mario Pit Stoppers",
            "BGM Yukumo Village": "BGM Yukumo Village",
            "Picross S": "Picross S",
            "DON'T STARVE": "Don't Starve",
            "Fate/EXTELLA": "Fate-Extella",
            "What? A *game*": "What A game",
            "Pre-order bonus": "Pre-order bonus",
            "Trailing colon:": "Trailing colon",
            "Tom &amp; Jerry": "Tom & Jerry",
            "  spaced   out . ": "spaced out",
        }
        for raw, expected in cases.items():
            with self.subTest(raw=raw):
                self.assertEqual(sw.normalize_text(raw), expected)

    def test_normalized_text_is_path_safe(self) -> None:
        out = sw.normalize_text('a/b\\c|d*e?f<g>h"i\x07j')
        for bad in '/\\|*?<>"\x07':
            self.assertNotIn(bad, out)

    def test_titles_stripped_from_the_start_of_a_dlc_name(self) -> None:
        self.assertEqual(
            sw.strip_title("Monster Hunter Rise", "Monster Hunter Rise Deluxe Kit"), "Deluxe Kit"
        )
        self.assertEqual(
            sw.strip_title("Monster Hunter Rise", "Monster Hunter Rise - Sunbreak Kit"),
            "Sunbreak Kit",
        )
        self.assertEqual(sw.strip_title("Game - Switch Edition", "Game - Synchrony"), "Synchrony")
        self.assertEqual(sw.strip_title("Mario", "Mariokart Pack"), "Mariokart Pack")
        self.assertEqual(sw.strip_title("Mario", "Mario"), "Mario")
        self.assertEqual(sw.strip_title("Mario", "Wave 1"), "Wave 1")

    def test_dlc_name_from_its_own_entry_or_its_bundle(self) -> None:
        self.assertEqual(sw.dlc_display_name("Mario Kart 8 Deluxe", "Wave 1", None), "Wave 1")
        bundle = ("MONSTER HUNTER RISE Deluxe Kit", 3, 6)
        self.assertEqual(
            sw.dlc_display_name("Monster Hunter Rise", None, bundle), "Deluxe Kit (part 3 of 6)"
        )
        self.assertIsNone(sw.dlc_display_name("Monster Hunter Rise", None, None))

    def test_file_and_folder_names(self) -> None:
        self.assertEqual(
            sw.build_names("BASE", MK8, 0, "Mario Kart 8 Deluxe"),
            ("roms/switch", f"Mario Kart 8 Deluxe [{MK8}][v0][BASE].nsp"),
        )
        self.assertEqual(
            sw.build_names("UPD", MK8_UPD, 196608, "Mario Kart 8 Deluxe"),
            (
                "extras/switch/updates/Mario Kart 8 Deluxe",
                f"Mario Kart 8 Deluxe [{MK8_UPD}][v196608][UPD].nsp",
            ),
        )
        self.assertEqual(
            sw.build_names("DLC", MK8_DLC, 65536, "Mario Kart 8 Deluxe", "Wave 1"),
            (
                "extras/switch/dlc/Mario Kart 8 Deluxe",
                f"Mario Kart 8 Deluxe - Wave 1 [{MK8_DLC}][v65536][DLC].nsp",
            ),
        )

    def test_very_long_names_still_fit_a_file_system(self) -> None:
        folder, name = sw.build_names("DLC", MK8_DLC, 0, "T" * 400, "D" * 400)
        self.assertLessEqual(len(name.encode()), 255)
        self.assertLessEqual(len(folder.rsplit("/", 1)[1].encode()), 255)
        self.assertTrue(name.endswith(f"[{MK8_DLC}][v0][DLC].nsp"))
        _, name = sw.build_names("BASE", MK8, 0, "é" * 400)
        self.assertLessEqual(len(name.encode()), 255)

    def test_verdicts(self) -> None:
        self.assertEqual(sw.verdict(False, None), "place")
        self.assertEqual(sw.verdict(True, True), "already")
        self.assertEqual(sw.verdict(True, False), "conflict")

    def test_parsing_what_nsz_prints(self) -> None:
        text = f"""
        NCA Archive
        titleId = 0100000000000001
        key Block = b'{KEYS_SECRET}'
            Cnmt

            titleId = 0100152000023001
            version = 10000
            titleType = 82
                ncaId = 176f9ec53e9ae14eddd37917c9818cdc,  type = 2
            File Path: AddOnContent_0100152000023001.cnmt
        titleId = 0100000000000002
            Cnmt
            titleId = 0100c1f0051b6800
            version = 20000
            titleType = 81
            File Path: Patch_0100c1f0051b6800.cnmt
        """
        self.assertEqual(
            sw.parse_nsz_info(text),
            [
                sw.Cnmt("0100152000023001", 65536, 0x82),
                sw.Cnmt("0100C1F0051B6800", 131072, 0x81),
            ],
        )
        self.assertEqual(sw.parse_nsz_info("no cnmt here\ntitleId = 0100152000023001"), [])
        self.assertEqual(sw.parse_nsz_info("Cnmt\ntitleId = nonsense\nversion = 1"), [])
        self.assertEqual(sw.parse_nsz_info("Cnmt\ntitleId = 0100152000023001\nversion = zz"), [])

    def test_scrub_drops_lines_that_mention_keys(self) -> None:
        self.assertEqual(sw.scrub("a\n\tkey Block = b'x'\nb\nMaster Keys loaded"), "a\nb")

    def test_pfs0_header(self) -> None:
        data = make_nsp(MK8)
        self.assertEqual(sw.parse_pfs0(data, len(data)), ["stub.cnmt", "data.nca"])
        bad = {
            "magic": (b"XXXX" + data[4:], len(data)),
            "truncated": (data[:100], 100),
            "member past the end": (data, len(data) - 5),
            "empty": (b"", 0),
            "no members": (b"PFS0" + bytes(12), 16),
        }
        for label, (blob, size) in bad.items():
            with self.subTest(label), self.assertRaises(sw.SwitchError):
                sw.parse_pfs0(blob, size)

    def test_bundle_parts_are_numbered_without_the_pack_itself(self) -> None:
        region = sw.parse_region({"1": entry(PACK, "Pack", [PACK, PART1, PART2, PART3])})
        self.assertEqual(region.bundles[PART1], ("Pack", 1, 3))
        self.assertEqual(region.bundles[PART3], ("Pack", 3, 3))
        self.assertNotIn(PACK, region.bundles)

    def test_a_bundle_entry_that_is_one_of_its_parts(self) -> None:
        a, b = "0100B04011743091", "0100B04011743092"
        region = sw.parse_region({"1": entry(b, "Bonus", [a, b])})
        self.assertEqual(region.bundles[a], ("Bonus", 1, 2))
        self.assertEqual(region.bundles[b], ("Bonus", 2, 2))
        self.assertNotIn(b, region.solo)

    def test_standalone_entries_and_demos(self) -> None:
        region = sw.parse_region(
            {
                "1": entry(MK8, "Game (Demo)", demo=True),
                "2": entry(MK8, "Game"),
                "3": entry(MK8_DLC, "Wave 1", [MK8_DLC]),
                "4": {"id": None, "name": "x"},
                "5": "junk",
            }
        )
        self.assertEqual(region.names[MK8], "Game")
        self.assertIn(MK8_DLC, region.solo)
        self.assertEqual(sw.parse_region([]).names, {})

    def test_region_files_in_lookup_order(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            for name in ("JP.ja.json", "GB.en.json", "US.en.json", "AU.en.json", "cnmts.json"):
                Path(tmp, name).write_text("{}")
            names = [p.name for p in sw.region_files(Path(tmp))]
        self.assertEqual(names, ["US.en.json", "AU.en.json", "GB.en.json", "JP.ja.json"])


class TitleDbTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        self.dir = write_titledb(self.tmp / "titledb", TITLEDB)

    def test_first_region_wins_and_later_ones_fill_gaps(self) -> None:
        db = sw.TitleDb(self.dir)
        self.assertEqual(db.name(MK8), "MARIO KART™ 8 DELUXE")
        self.assertEqual(len(db.loaded), 1)  # GB was not needed
        self.assertEqual(db.name("0100000000001000"), "Only in Britain")
        self.assertIsNone(db.name("0100FFFFFFFFF000"))

    def test_a_bundle_in_an_earlier_region_beats_a_name_in_a_later_one(self) -> None:
        db = sw.TitleDb(self.dir)
        own, bundle = db.dlc(PART1)
        self.assertIsNone(own)
        self.assertEqual(bundle, ("MONSTER HUNTER RISE Deluxe Kit", 1, 3))
        self.assertEqual(db.dlc(MK8_DLC), ("Wave 1", None))
        self.assertEqual(db.dlc("0100FFFFFFFFF001"), (None, None))

    def test_folder_without_region_files_is_an_error(self) -> None:
        empty = self.tmp / "empty"
        empty.mkdir()
        with self.assertRaises(sw.SwitchError):
            sw.TitleDb(empty)

    def test_the_cache_is_used_and_follows_the_file(self) -> None:
        cache = self.tmp / "cache"
        first = sw.TitleDb(self.dir, cache)
        self.assertEqual(first.name(MK8), "MARIO KART™ 8 DELUXE")
        saved = cache / "US.en.json.cache"
        self.assertTrue(saved.is_file())
        data = json.loads(saved.read_text())
        data["names"][MK8] = "From the cache"
        saved.write_text(json.dumps(data))
        self.assertEqual(sw.TitleDb(self.dir, cache).name(MK8), "From the cache")
        write_titledb(self.dir, {"US.en.json": [entry(MK8, "Changed on disk, longer")]})
        self.assertEqual(sw.TitleDb(self.dir, cache).name(MK8), "Changed on disk, longer")

    def test_a_broken_region_file_is_a_clear_error(self) -> None:
        (self.dir / "US.en.json").write_text("{not json")
        with self.assertRaises(sw.SwitchError) as ctx:
            sw.TitleDb(self.dir).name(MK8)
        self.assertIn("US.en.json", str(ctx.exception))


class SwitchBase(Base):
    """A hub, a stand-in nsz, a keys file and a titledb; `go()` runs the inbox."""

    def setUp(self) -> None:
        super().setUp()
        self.nsz = make_stub(self.bin, "nsz", NSZ)
        self.keys = self.tmp / "keys" / "prod.keys"
        self.keys.parent.mkdir()
        self.keys.write_text(KEYS_TEXT)
        self.keys.chmod(0o600)
        self.titledb = write_titledb(self.tmp / "titledb", TITLEDB)
        self.nsz_log = self.tmp / "nsz.log"
        env = mock.patch.dict(os.environ, {"STUB_NSZ_LOG": str(self.nsz_log)})
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop("STUB_NSZ_FAIL", None)
        os.environ["HOME"] = str(self.tmp / "home")
        self.extras = self.hub / "library" / "extras"

    def go(self, *, nsz: bool = True, keys: bool = True, titledb: bool = True, **kw) -> int:  # type: ignore[no-untyped-def]
        """`keys=False` leaves switch_keys unset, and HOME is empty: no keys file anywhere."""
        settings = InboxSettings(
            titledb_dir=str(self.titledb) if titledb else None,
            switch_keys=str(self.keys) if keys else None,
        )
        tools = Tools(chdman=self.chdman, nsz=self.nsz if nsz else None)
        return super().go(tools=tools, settings=settings, **kw)

    def files(self, folder: str = "") -> list[str]:
        root = self.hub / "library" / folder
        return sorted(str(p.relative_to(root)) for p in root.rglob("*") if p.is_file())

    def nsz_calls(self) -> list[str]:
        return self.nsz_log.read_text().splitlines() if self.nsz_log.exists() else []

    def read(self, rel: str) -> bytes:
        return (self.hub / "library" / rel).read_bytes()


MHR = "Monster Hunter Rise"
BASE_NAME = f"Mario Kart 8 Deluxe [{MK8}][v0][BASE].nsp"
UPD_NAME = f"Mario Kart 8 Deluxe [{MK8_UPD}][v196608][UPD].nsp"
DLC_NAME = f"Mario Kart 8 Deluxe - Wave 1 [{MK8_DLC}][v65536][DLC].nsp"


class PlacingTests(SwitchBase):
    def test_a_game_with_a_meaningless_name_is_named_from_its_contents(self) -> None:
        data = make_nsp(MK8)
        self.drop("a1b2c3.nsp", data)
        self.assertEqual(self.go(), 0, self.out)
        self.assertEqual(self.files("roms/switch"), [BASE_NAME])
        self.assertEqual(sha(self.read(f"roms/switch/{BASE_NAME}")), sha(data))
        self.assertIn(whitelist_line("switch", BASE_NAME), self.out)
        self.assertEqual(self.done(), ["a1b2c3.nsp"])
        self.assertEqual(self.review(), [])
        self.assertNotIn("taken from the file name", self.out)

    def test_updates_and_dlc_go_to_extras_folders_without_a_whitelist_line_each(self) -> None:
        upd, dlc = make_nsp(MK8_UPD, 196608), make_nsp(MK8_DLC, 65536)
        self.drop("u.nsp", upd)
        self.drop("d.nsp", dlc)
        self.assertEqual(self.go(), 0, self.out)
        self.assertEqual(
            self.files("extras/switch"),
            [
                f"dlc/Mario Kart 8 Deluxe/{DLC_NAME}",
                f"updates/Mario Kart 8 Deluxe/{UPD_NAME}",
            ],
        )
        self.assertEqual(
            sha(self.read(f"extras/switch/updates/Mario Kart 8 Deluxe/{UPD_NAME}")), sha(upd)
        )
        self.assertEqual(
            sha(self.read(f"extras/switch/dlc/Mario Kart 8 Deluxe/{DLC_NAME}")), sha(dlc)
        )
        self.assertNotIn("whitelist: ", self.out)
        self.assertNotIn("whitelist lines", self.out)
        self.assertIn("!/extras/switch/updates and !/extras/switch/dlc", self.out)
        self.assertIn("path: library/extras/switch/dlc/Mario Kart 8 Deluxe/", self.out)

    def test_a_dlc_listed_only_in_a_bundle_gets_its_part_number(self) -> None:
        self.drop("p3.nsp", make_nsp(PART3))
        self.drop("p1.nsp", make_nsp(PART1))
        self.assertEqual(self.go(), 0, self.out)
        self.assertEqual(
            self.files("extras/switch/dlc"),
            [
                f"{MHR}/{MHR} - Deluxe Kit (part 1 of 3) [{PART1}][v0][DLC].nsp",
                f"{MHR}/{MHR} - Deluxe Kit (part 3 of 3) [{PART3}][v0][DLC].nsp",
            ],
        )

    def test_a_dlc_name_that_repeats_the_title_is_not_doubled(self) -> None:
        self.drop("s.nsp", make_nsp("0100CEA007D09001"))
        self.assertEqual(self.go(), 0, self.out)
        self.assertEqual(
            self.files("extras/switch/dlc"),
            [
                "Crypt of the NecroDancer - Nintendo Switch Edition/Crypt of the NecroDancer"
                " - Nintendo Switch Edition - Synchrony [0100CEA007D09001][v0][DLC].nsp"
            ],
        )

    def test_report_only_reads_but_writes_nothing(self) -> None:
        self.drop("x.nsp", make_nsp(MK8))
        self.drop("y.nsz", make_nsz(make_nsp(MK8_UPD, 196608)))
        before = self.tree()
        self.assertEqual(self.go(apply=False), 0, self.out)
        self.assertEqual(self.tree(), before)
        self.assertIn("would place", self.out)
        self.assertIn(whitelist_line("switch", BASE_NAME), self.out)
        self.assertTrue(all(" -i " in f" {c} " for c in self.nsz_calls()), self.nsz_calls())

    def test_zip_with_a_game_inside(self) -> None:
        self.zip_of("pack.zip", {"folder/zz.nsp": make_nsp(MK8)})
        self.assertEqual(self.go(), 0, self.out)
        self.assertEqual(self.files("roms/switch"), [BASE_NAME])

    def test_games_and_other_systems_in_one_run(self) -> None:
        self.drop("a.nes", b"NES\x1a" + b"adventure" * 100)
        self.drop("sw.nsp", make_nsp(MK8))
        self.assertEqual(self.go(), 0, self.out)
        self.assertEqual(self.library(), ["nes/Adventure (USA).zip", f"switch/{BASE_NAME}"])


class AlreadyThereTests(SwitchBase):
    def test_identical_game_is_already_in_the_library(self) -> None:
        data = make_nsp(MK8)
        self.drop("one.nsp", data)
        self.assertEqual(self.go(), 0, self.out)
        self.drop("two.nsp", data)
        self.assertEqual(self.go(), 0, self.out)
        self.assertIn("already in the library", self.out)
        self.assertEqual(self.files("roms/switch"), [BASE_NAME])
        self.assertEqual(self.done(), ["one.nsp", "two.nsp"])

    def test_different_bytes_under_the_same_name_are_never_replaced(self) -> None:
        self.drop("one.nsp", make_nsp(MK8, filler=100))
        self.assertEqual(self.go(), 0)
        kept = self.read(f"roms/switch/{BASE_NAME}")
        self.drop("two.nsp", make_nsp(MK8, filler=300))
        self.assertEqual(self.go(), 1, self.out)
        self.assertEqual(self.read(f"roms/switch/{BASE_NAME}"), kept)
        self.assertEqual(self.review(), ["two.nsp", "two.nsp.reason.txt"])
        self.assertIn("different file", self.reason("two.nsp"))
        self.assertEqual(self.files("roms/switch"), [BASE_NAME])

    def test_same_title_and_version_under_another_name_counts_as_there(self) -> None:
        data = make_nsp(MK8)
        folder = self.hub / "library" / "roms" / "switch"
        folder.mkdir(parents=True)
        (folder / f"Old name [{MK8}][v0].nsp").write_bytes(data)
        self.drop("new.nsp", data)
        self.assertEqual(self.go(), 0, self.out)
        self.assertIn("already in the library", self.out)
        self.assertEqual(self.files("roms/switch"), [f"Old name [{MK8}][v0].nsp"])
        self.drop("other.nsp", make_nsp(MK8, filler=999))
        self.assertEqual(self.go(), 1, self.out)
        self.assertIn("Old name", self.reason("other.nsp"))

    def test_report_says_it_exists_and_compares_content_only_with_apply(self) -> None:
        data = make_nsp(MK8)
        self.drop("one.nsp", data)
        self.go()
        self.drop("two.nsp", data)
        self.assertEqual(self.go(apply=False), 0)
        self.assertIn("already exists", self.out)
        self.assertIn("content is compared with --apply", self.out)

    def test_a_part_file_left_by_a_crash_is_replaced(self) -> None:
        folder = self.hub / "library" / "roms" / "switch"
        folder.mkdir(parents=True)
        (folder / (BASE_NAME + ".part")).write_bytes(b"half")
        self.drop("one.nsp", make_nsp(MK8))
        self.assertEqual(self.go(), 0, self.out)
        self.assertEqual(self.files("roms/switch"), [BASE_NAME])


class CompressedTests(SwitchBase):
    def test_nsz_is_decompressed_checked_and_only_the_nsp_is_placed(self) -> None:
        nsp = make_nsp(MK8_UPD, 196608)
        self.drop("pack.nsz", make_nsz(nsp))
        self.assertEqual(self.go(), 0, self.out)
        self.assertEqual(self.files("extras/switch"), [f"updates/Mario Kart 8 Deluxe/{UPD_NAME}"])
        self.assertEqual(
            sha(self.read(f"extras/switch/updates/Mario Kart 8 Deluxe/{UPD_NAME}")), sha(nsp)
        )
        calls = self.nsz_calls()
        self.assertEqual([c.split()[0] for c in calls], ["-i", "-D", "-V"])
        self.assertIn("compressed: it is decompressed", self.out)
        self.assertEqual(self.done(), ["pack.nsz"])

    def test_a_nsz_equal_to_a_nsp_already_there(self) -> None:
        nsp = make_nsp(MK8)
        self.drop("one.nsp", nsp)
        self.go()
        self.drop("two.nsz", make_nsz(nsp))
        self.assertEqual(self.go(), 0, self.out)
        self.assertIn("already in the library", self.out)

    def test_every_decompression_failure_goes_to_review_and_leaves_nothing_behind(self) -> None:
        for mode, reason in (
            ("decompress", "could not decompress"),
            ("silent", "could not decompress"),
            ("garbage", "not a valid NSP"),
            ("verify", "failed nsz verification"),
            ("unverified", "failed nsz verification"),
        ):
            with self.subTest(mode):
                os.environ["STUB_NSZ_FAIL"] = mode
                self.drop(f"{mode}.nsz", make_nsz(make_nsp(MK8)))
                self.assertEqual(self.go(), 1, self.out)
                self.assertIn(reason, self.reason(f"{mode}.nsz"))
                self.assertEqual(self.files("roms"), [], mode)
                self.assertEqual(self.files("extras") if self.extras.exists() else [], [])

    def test_nsz_without_the_tool_or_the_keys_is_reviewed_not_guessed(self) -> None:
        self.drop("a.nsz", make_nsz(make_nsp(MK8)))
        self.assertEqual(self.go(nsz=False), 1)
        self.assertIn("needs nsz and the keys file", self.reason("a.nsz"))
        self.drop("b.nsz", make_nsz(make_nsp(MK8)))
        self.assertEqual(self.go(keys=False), 1)
        self.assertIn("needs nsz and the keys file", self.reason("b.nsz"))
        self.assertEqual(self.files(), [])

    def test_report_only_never_decompresses(self) -> None:
        self.drop("a.nsz", make_nsz(make_nsp(MK8)))
        self.assertEqual(self.go(apply=False), 0, self.out)
        self.assertIn("would place", self.out)
        self.assertNotIn("-D", " ".join(self.nsz_calls()).split())


class IdentifyingTests(SwitchBase):
    def test_without_nsz_the_file_name_is_used_and_the_report_says_so(self) -> None:
        data = make_nsp(MK8)
        self.drop(f"whatever [{MK8}][v0].nsp", data)
        self.assertEqual(self.go(nsz=False), 0, self.out)
        self.assertEqual(self.files("roms/switch"), [BASE_NAME])
        self.assertIn("taken from the file name", self.out)
        self.assertEqual(self.nsz_calls(), [])

    def test_without_the_keys_file_nsz_is_not_even_run(self) -> None:
        self.drop(f"x [{MK8}][v0].nsp", make_nsp(MK8))
        self.assertEqual(self.go(keys=False), 0, self.out)
        self.assertIn("taken from the file name", self.out)
        self.assertEqual(self.nsz_calls(), [])

    def test_no_nsz_and_no_ids_in_the_name_goes_to_review(self) -> None:
        self.drop("mystery.nsp", make_nsp(MK8))
        self.assertEqual(self.go(nsz=False), 1)
        self.assertIn("cannot tell what it is", self.reason("mystery.nsp"))

    def test_when_nsz_cannot_read_an_nsp_it_is_not_guessed_from_the_name(self) -> None:
        os.environ["STUB_NSZ_FAIL"] = "info"
        self.drop(f"x [{MK8}][v0].nsp", make_nsp(MK8))
        self.assertEqual(self.go(), 1, self.out)
        self.assertIn("nsz could not read", self.reason(f"x [{MK8}][v0].nsp"))

    def test_the_file_wins_over_a_wrong_name(self) -> None:
        self.drop(f"Wrong [{MK8_UPD}][v5][UPD].nsp", make_nsp(MK8))
        self.assertEqual(self.go(), 0, self.out)
        self.assertEqual(self.files("roms/switch"), [BASE_NAME])
        self.assertIn("the file name says", self.out)

    def test_a_cnmt_type_that_disagrees_with_the_title_id_is_reviewed(self) -> None:
        self.drop("odd.nsp", make_nsp(MK8, title_type=0x82))
        self.assertEqual(self.go(), 1)
        self.assertIn("CNMT says DLC but the TitleID says BASE", self.reason("odd.nsp"))

    def test_a_file_with_several_titles_is_reviewed(self) -> None:
        self.drop("two.nsp", make_nsp_multi([(MK8, 0), ("0100152000099000", 0)]))
        self.assertEqual(self.go(), 1)
        self.assertIn("holds 2 titles", self.reason("two.nsp"))

    def test_a_file_that_is_not_a_pfs0_is_reviewed_when_it_is_placed(self) -> None:
        self.drop(f"junk [{MK8}][v0].nsp", b"not a switch file at all")
        self.assertEqual(self.go(nsz=False), 1)
        self.assertIn("not a PFS0", self.reason(f"junk [{MK8}][v0].nsp"))
        self.assertEqual(self.files(), [])

    def test_other_files_next_to_a_game_are_named_in_the_review(self) -> None:
        self.drop("pack", {"game.nsp": make_nsp(MK8), "readme.txt": b"hi", "extra.bin": b"x"})
        self.assertEqual(self.go(), 1, self.out)
        self.assertEqual(self.files("roms/switch"), [BASE_NAME])
        self.assertIn("not a Switch file", self.reason("pack"))
        self.assertIn("extra.bin", self.reason("pack"))
        self.assertNotIn("readme.txt", self.reason("pack"))

    def test_xci_is_never_converted(self) -> None:
        self.drop("cart.xci", b"x" * 100)
        self.zip_of("cart.zip", {"inner.xcz": b"x" * 100})
        self.assertEqual(self.go(), 2, self.out)
        self.assertIn("XCI cartridge images are not converted", self.reason("cart.xci"))
        self.assertIn("XCI cartridge images are not converted", self.reason("cart.zip"))
        self.assertEqual(self.nsz_calls(), [])


class TitleDbProblemTests(SwitchBase):
    def test_title_not_in_titledb(self) -> None:
        self.drop("a.nsp", make_nsp("0100AAAAAAAAA000"))
        self.assertEqual(self.go(), 1)
        self.assertIn("is not in titledb", self.reason("a.nsp"))
        self.assertEqual(self.files(), [])

    def test_update_or_dlc_of_an_unknown_game(self) -> None:
        self.drop("u.nsp", make_nsp("0100AAAAAAAAA800", 65536))
        self.drop("d.nsp", make_nsp("0100AAAAAAAAB001"))
        self.assertEqual(self.go(), 2)
        self.assertIn("its game (0100AAAAAAAAA000) is not in titledb", self.reason("u.nsp"))
        self.assertIn("is not in titledb", self.reason("d.nsp"))

    def test_dlc_titledb_does_not_know(self) -> None:
        self.drop("d.nsp", make_nsp("0100152000023099"))
        self.assertEqual(self.go(), 1)
        self.assertIn("this DLC is not in titledb", self.reason("d.nsp"))

    def test_titledb_not_configured(self) -> None:
        self.drop("a.nsp", make_nsp(MK8))
        self.assertEqual(self.go(titledb=False), 1)
        self.assertIn("titledb_dir is not set", self.reason("a.nsp"))

    def test_configured_titledb_or_keys_that_do_not_exist_stop_the_run(self) -> None:
        self.drop("a.nsp", make_nsp(MK8))
        for settings, key in (
            (InboxSettings(titledb_dir=str(self.tmp / "gone")), "titledb_dir"),
            (InboxSettings(switch_keys=str(self.tmp / "gone")), "switch_keys"),
        ):
            with self.subTest(key), self.assertRaises(InboxError) as ctx:
                super(SwitchBase, self).go(settings=settings)
            self.assertIn(key, str(ctx.exception))
        self.assertEqual(self.files(), [])


class SecretTests(SwitchBase):
    def test_the_keys_content_is_never_shown(self) -> None:
        os.environ["STUB_NSZ_FAIL"] = "info"  # the stand-in complains about a key on stderr
        self.drop("a.nsp", make_nsp(MK8))
        self.go()
        os.environ.pop("STUB_NSZ_FAIL")
        self.drop("b.nsz", make_nsz(make_nsp(MK8)))
        os.environ["STUB_NSZ_FAIL"] = "decompress"
        self.go()
        shown = [self.out, *(p.read_text() for p in self.inbox.rglob("*.reason.txt"))]
        self.assertEqual(len(shown), 3)
        for text in shown:
            self.assertNotIn(KEYS_SECRET, text)

    def test_nsz_is_given_the_path_not_the_content(self) -> None:
        self.drop("a.nsp", make_nsp(MK8))
        self.go()
        self.assertTrue(self.nsz_calls())
        for call in self.nsz_calls():
            self.assertIn(f"--keys {self.keys}", call)
            self.assertNotIn(KEYS_SECRET, call)


class CliTests(SwitchBase):
    def cli(self, *args: str) -> tuple[int, str, str]:
        from test_cli import run

        with mock.patch.dict(os.environ, {"XDG_CACHE_HOME": str(self.tmp / "xdg")}):
            return run("inbox", *args)

    def config(self, nsz: str | None = None) -> str:
        path = self.tmp / "config.toml"
        path.write_text(
            f'[inbox]\nroot = "{self.hub}"\ndats_dir = "{self.dats}"\n'
            f'work_dir = "{self.tmp / "work"}"\nnsz = "{nsz or self.nsz}"\n'
            f'titledb_dir = "{self.titledb}"\nswitch_keys = "{self.keys}"\n'
        )
        return str(path)

    def test_the_config_keys_reach_the_run_and_the_titledb_cache_is_kept(self) -> None:
        self.drop("a.nsp", make_nsp(MK8))
        code, out, err = self.cli("--config", self.config(), "--apply")
        self.assertEqual(code, 0, out + err)
        self.assertEqual(self.files("roms/switch"), [BASE_NAME])
        self.assertTrue((self.tmp / "xdg" / "warpzone" / "titledb" / "US.en.json.cache").is_file())
        self.assertIn("nsz yes", out)

    def test_a_configured_nsz_that_does_not_exist_is_a_usage_error(self) -> None:
        code, _, err = self.cli("--config", self.config(f"{self.tmp}/nope"))
        self.assertEqual(code, 2)
        self.assertIn("nsz", err)

    def test_the_default_keys_path_is_in_the_home_folder(self) -> None:
        self.assertEqual(sw.default_keys(Path("/home/me")), Path("/home/me/.switch/prod.keys"))


class PackedHelperTests(unittest.TestCase):
    def test_pfs0_builder_round_trips(self) -> None:
        data = pfs0({"a": b"123", "b.nca": b"45"})
        self.assertEqual(sw.parse_pfs0(data, len(data)), ["a", "b.nca"])


if __name__ == "__main__":
    unittest.main()
