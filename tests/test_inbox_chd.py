"""Inbox: a `.chd` dropped for a CHD system is extracted, identified and rebuilt."""

import importlib
import os
import unittest
from pathlib import Path
from types import ModuleType
from unittest import mock

from inbox_support import source_chd, stub_md5, write_dat
from test_inbox import Base, rom
from warpzone.inbox_core import Tools

PS2_ISO = b"naruto ps2 disc " * 600
PSX_BIN = b"psx single track " * 300
SPLIT_DATA = b"split data track " * 300
SPLIT_AUDIO = b"split audio track " * 150
DC_T1 = b"dc track one " * 100
DC_T2 = b"dc track two " * 100
DC_T3 = b"dc track three " * 100
D1 = b"psx multi disc one " * 100
D2 = b"psx multi disc two " * 100
NARUTO = "Naruto Test (Europe) (En,Fr,De)"
CD_INFO = """Input file:   x.chd
Logical size: 2,302,451,712 bytes
Hunk Size:    19,584 bytes
Metadata:     Tag='CHT2'  Index=0  Length=87 bytes
              TRACK:1 TYPE:MODE1 SUBTYPE:NONE FRAMES:5000 PREGAP:0 PGTYPE:
Metadata:     Tag='CHT2'  Index=1  Length=87 bytes
              TRACK:2 TYPE:AUDIO SUBTYPE:NONE FRAMES:5000 PREGAP:150 PGTYPE:
"""


def chd_module() -> ModuleType:
    return importlib.import_module("warpzone.inbox_chd")


class PureTests(unittest.TestCase):
    def test_info_of_a_cd_chd(self) -> None:
        info = chd_module().parse_info(CD_INFO)
        assert info
        self.assertEqual(info.kind, "cd")
        self.assertEqual(info.logical_size, 2_302_451_712)
        self.assertEqual(info.tracks, ("MODE1", "AUDIO"))

    def test_info_of_the_other_kinds(self) -> None:
        parse = chd_module().parse_info
        dvd = parse("Logical size: 10,240,000 bytes\nMetadata: Tag='DVD '  Index=0")
        assert dvd
        self.assertEqual((dvd.kind, dvd.tracks), ("dvd", ()))
        gd = parse(
            "Logical size: 1,000 bytes\nMetadata: Tag='CHGD'  Index=0  Length=98 bytes\n"
            "  TRACK:1 TYPE:MODE1_RAW SUBTYPE:NONE FRAMES:450 PAD:150\n"
        )
        assert gd
        self.assertEqual((gd.kind, gd.tracks), ("gd", ("MODE1_RAW",)))
        old = parse("Logical size: 2 bytes\nMetadata: Tag='CHTR'  Index=0\n")
        assert old
        self.assertEqual(old.kind, "cd")

    def test_info_of_what_is_not_a_disc(self) -> None:
        parse = chd_module().parse_info
        self.assertIsNone(parse("Logical size: 5 bytes\nMetadata: Tag='GDDD'\n"))
        self.assertIsNone(parse("Logical size: 5 bytes\n"))
        self.assertIsNone(parse("Metadata: Tag='DVD '\n"))  # no size

    def test_extraction_plans(self) -> None:
        mod = chd_module()
        plan, info = mod.plan_extraction, mod.ChdInfo
        self.assertEqual(plan(info("dvd", 1, ()), "g"), ("extractdvd", ["-o", "g.iso"]))
        self.assertEqual(
            plan(info("cd", 1, ("MODE1",)), "g"),
            ("extractcd", ["-o", "g.cue", "-ob", "g.iso"]),
        )
        self.assertEqual(
            plan(info("cd", 1, ("MODE2_RAW",)), "g"),
            ("extractcd", ["-o", "g.cue", "-ob", "g.bin"]),
        )
        self.assertEqual(
            plan(info("cd", 1, ("MODE2_RAW", "AUDIO")), "g"),
            ("extractcd", ["-o", "g.cue", "-sb"]),
        )
        self.assertEqual(plan(info("cd", 1, ()), "g")[1], ["-o", "g.cue", "-sb"])
        self.assertEqual(plan(info("gd", 1, ("MODE1_RAW",) * 3), "g")[1], ["-o", "g.gdi"])

    def test_what_a_chd_is_is_said_in_a_few_words(self) -> None:
        mod = chd_module()
        info = mod.ChdInfo
        self.assertEqual(mod.describe(info("cd", 1, ("MODE1", "AUDIO"))), "CD, 2 tracks")
        self.assertEqual(mod.describe(info("cd", 1, ("MODE2_RAW",))), "CD, 1 track")
        self.assertEqual(mod.describe(info("cd", 1, ())), "CD")
        self.assertEqual(mod.describe(info("gd", 1, ("MODE1_RAW",) * 3)), "GD-ROM, 3 tracks")
        self.assertEqual(mod.describe(info("dvd", 1, ())), "DVD")

    def test_chdman_expands_percent_t_in_output_names_so_the_stem_has_no_percent(self) -> None:
        self.assertEqual(chd_module().file_stem("100%t pure.chd"), "100_t pure")

    def test_the_stem_is_cut_to_fit_a_file_name(self) -> None:
        stem = chd_module().file_stem("é" * 200 + ".chd")
        self.assertLessEqual(len(stem.encode()), 120)
        self.assertTrue(stem.startswith("é"))
        self.assertEqual(chd_module().file_stem("Naruto (Europe).CHD"), "Naruto (Europe)")


class ChdCase(Base):
    def setUp(self) -> None:
        super().setUp()
        self.log = self.tmp / "chdman.log"
        os.environ["STUB_CHDMAN_LOG"] = str(self.log)
        for system in ("ps2", "dreamcast"):
            (self.roms / system).mkdir()
        write_dat(
            self.dats / "redump",
            "Sony - PlayStation 2",
            [(NARUTO, [rom(f"{NARUTO}.iso", PS2_ISO)])],
        )
        write_dat(
            self.dats / "redump",
            "Sony - PlayStation",
            [
                ("Single Track (USA)", [rom("Single Track (USA).bin", PSX_BIN)]),
                (
                    "Split Disc (Europe)",
                    [
                        rom("Split Disc (Europe) (Track 1).bin", SPLIT_DATA),
                        rom("Split Disc (Europe) (Track 2).bin", SPLIT_AUDIO),
                    ],
                ),
                ("Twin (Europe) (Disc 1)", [rom("Twin (Europe) (Disc 1).bin", D1)]),
                ("Twin (Europe) (Disc 2)", [rom("Twin (Europe) (Disc 2).bin", D2)]),
            ],
        )
        write_dat(
            self.dats / "redump",
            "Sega - Dreamcast",
            [
                (
                    "Dream Test (Europe)",
                    [
                        rom("Dream Test (Europe) (Track 1).bin", DC_T1),
                        rom("Dream Test (Europe) (Track 2).raw", DC_T2),
                        rom("Dream Test (Europe) (Track 3).bin", DC_T3),
                    ],
                )
            ],
        )

    def calls(self) -> list[str]:
        return self.log.read_text().splitlines() if self.log.exists() else []

    def commands(self) -> list[str]:
        return [c.split()[0] for c in self.calls()]

    def chd(self, name: str, tag: str, tracks: list[tuple[str, bytes]], **kw: int) -> Path:
        return self.drop(name, source_chd(tag, tracks, **kw))

    def placed_md5(self, entry: str) -> str:
        return (self.roms / entry).read_text().splitlines()[1]


class ExtractionTests(ChdCase):
    def test_a_ps2_chd_made_in_cd_mode_from_an_iso_is_rebuilt_as_a_dvd_chd(self) -> None:
        item = self.chd(f"{NARUTO}.chd", "CHT2", [("MODE1", PS2_ISO)])
        self.assertEqual(self.go(), 0, self.out)
        self.assertEqual(self.library(), [f"ps2/{NARUTO}.chd"])
        self.assertEqual(self.placed_md5(f"ps2/{NARUTO}.chd"), stub_md5(PS2_ISO))
        self.assertEqual(self.commands(), ["info", "extractcd", "createdvd", "verify"])
        self.assertIn("-ob", self.calls()[1])
        self.assertEqual(self.done(), [item.name])
        self.assertEqual(os.listdir(self.tmp / "work"), [])

    def test_createdvd_uses_the_codecs_without_flac(self) -> None:
        self.chd(f"{NARUTO}.chd", "CHT2", [("MODE1", PS2_ISO)])
        self.assertEqual(self.go(), 0, self.out)
        create = [c for c in self.calls() if c.startswith("createdvd ")]
        self.assertEqual(len(create), 1, self.calls())
        self.assertIn(" -c lzma,zlib,huff", create[0])

    def test_createdvd_never_uses_flac_whatever_the_setting(self) -> None:
        for flac in (True, False):
            self.log.unlink(missing_ok=True)
            self.chd(f"{NARUTO}.chd", "CHT2", [("MODE1", PS2_ISO)])
            tools = Tools(chdman=self.chdman, rahasher=self.rahasher, chdman_flac=flac)
            self.assertEqual(self.go(tools=tools), 0, self.out)
            create = [c for c in self.calls() if c.startswith("createdvd ")]
            self.assertEqual(len(create), 1, self.calls())
            self.assertIn(" -c lzma,zlib,huff", create[0])
            (self.roms / "ps2" / f"{NARUTO}.chd").unlink()

    def test_createcd_without_flac_leaves_flac_out_of_a_rebuilt_chd(self) -> None:
        self.chd("whatever.chd", "CHT2", [("MODE2_RAW", PSX_BIN)])
        tools = Tools(chdman=self.chdman, rahasher=self.rahasher, chdman_flac=False)
        self.assertEqual(self.go(tools=tools), 0, self.out)
        create = [c for c in self.calls() if c.startswith("createcd ")]
        self.assertEqual(len(create), 1, self.calls())
        self.assertIn(" -c cdlz,cdzl", create[0])

    def test_a_ps2_chd_in_dvd_mode_is_extracted_with_extractdvd(self) -> None:
        self.chd("naruto.chd", "DVD ", [("DVD", PS2_ISO)])
        self.assertEqual(self.go(), 0, self.out)
        self.assertEqual(self.library(), [f"ps2/{NARUTO}.chd"])
        self.assertEqual(self.commands(), ["info", "extractdvd", "createdvd", "verify"])

    def test_a_psx_chd_with_one_track_is_rebuilt_with_createcd(self) -> None:
        self.chd("whatever.chd", "CHT2", [("MODE2_RAW", PSX_BIN)])
        self.assertEqual(self.go(), 0, self.out)
        self.assertEqual(self.library(), ["psx/Single Track (USA).chd"])
        self.assertEqual(self.commands(), ["info", "extractcd", "createcd", "verify"])
        self.assertEqual(self.placed_md5("psx/Single Track (USA).chd"), stub_md5(PSX_BIN))

    def test_a_chd_with_several_tracks_is_split_so_each_track_matches_the_dat(self) -> None:
        self.chd("split.chd", "CHT2", [("MODE2_RAW", SPLIT_DATA), ("AUDIO", SPLIT_AUDIO)])
        self.assertEqual(self.go(), 0, self.out)
        self.assertEqual(self.library(), ["psx/Split Disc (Europe).chd"])
        self.assertIn("-sb", self.calls()[1])
        self.assertNotIn("-ob", self.calls()[1])
        self.assertEqual(
            self.placed_md5("psx/Split Disc (Europe).chd"), stub_md5(SPLIT_DATA, SPLIT_AUDIO)
        )

    def test_a_dreamcast_chd_is_extracted_to_a_gdi(self) -> None:
        tracks = [("MODE1_RAW", DC_T1), ("AUDIO", DC_T2), ("MODE1_RAW", DC_T3)]
        self.chd("dc.chd", "CHGD", tracks)
        self.assertEqual(self.go(), 0, self.out)
        self.assertEqual(self.library(), ["dreamcast/Dream Test (Europe).chd"])
        self.assertIn(".gdi", self.calls()[1])
        self.assertEqual(self.commands(), ["info", "extractcd", "createcd", "verify"])

    def test_the_two_discs_of_a_game_in_chd_go_in_one_m3u_folder(self) -> None:
        self.drop(
            "twin",
            {
                "d1.chd": source_chd("CHT2", [("MODE2_RAW", D1)]),
                "d2.chd": source_chd("CHT2", [("MODE2_RAW", D2)]),
            },
        )
        self.assertEqual(self.go(), 0, self.out)
        self.assertEqual(
            self.library(),
            [
                "psx/Twin (Europe).m3u/Twin (Europe) (Disc 1).chd",
                "psx/Twin (Europe).m3u/Twin (Europe) (Disc 2).chd",
                "psx/Twin (Europe).m3u/Twin (Europe).m3u",
            ],
        )

    def test_a_libcrypt_sbi_next_to_the_chd_travels_with_its_disc(self) -> None:
        self.drop(
            "lc",
            {
                "Single Track (USA).sbi": b"SBI\x00" + b"\x01" * 14,
                "x.chd": source_chd("CHT2", [("MODE2_RAW", PSX_BIN)]),
            },
        )
        self.assertEqual(self.go(), 0, self.out)
        self.assertEqual(
            self.library(),
            [
                "psx/Single Track (USA).m3u/Single Track (USA).chd",
                "psx/Single Track (USA).m3u/Single Track (USA).m3u",
                "psx/Single Track (USA).m3u/Single Track (USA).sbi",
            ],
        )

    def test_a_percent_in_the_chd_name_never_reaches_a_chdman_output_name(self) -> None:
        self.chd("100%t pure.chd", "CHT2", [("MODE2_RAW", PSX_BIN)])
        self.assertEqual(self.go(), 0, self.out)
        self.assertEqual(self.library(), ["psx/Single Track (USA).chd"])
        self.assertIn("100_t pure.cue", self.calls()[1])
        self.assertNotIn("%t pure.cue", self.calls()[1])

    def test_the_original_chd_is_never_changed(self) -> None:
        item = self.chd("keep.chd", "CHT2", [("MODE2_RAW", PSX_BIN)])
        before = item.read_bytes()
        self.go()
        self.assertEqual((self.inbox / "_done" / "2026-10-27" / "keep.chd").read_bytes(), before)


class RefusalTests(ChdCase):
    def test_a_chd_that_matches_nothing_goes_to_review_naming_it(self) -> None:
        self.chd("Rip of Something.chd", "CHT2", [("MODE1", b"a different image " * 100)])
        self.assertEqual(self.go(), 1)
        self.assertEqual(self.library(), [])
        reason = self.reason("Rip of Something.chd")
        self.assertIn(
            "Rip of Something.chd: extracted with chdman (CD, 1 track), but its content matches "
            "no Redump entry (a CHD made from a changed, trimmed or re-ripped image cannot be "
            "matched; drop the original cue/bin, gdi or iso instead)",
            reason,
        )
        self.assertNotIn("no DAT match:", reason)
        self.assertNotIn("createdvd", self.commands())
        self.assertIn("Rip of Something.chd", self.review())

    def test_a_chd_that_is_not_a_disc_is_refused_without_extracting(self) -> None:
        self.chd("disk.chd", "GDDD", [("MODE1", b"x")])
        self.assertEqual(self.go(), 1)
        self.assertIn("disk.chd", self.reason("disk.chd"))
        self.assertIn("not a CD, GD-ROM or DVD", self.reason("disk.chd"))
        self.assertEqual(self.commands(), ["info"])

    def test_a_chd_chdman_cannot_extract_is_reviewed_with_its_message(self) -> None:
        self.chd("bad.chd", "CHT2", [("MODE2_RAW", PSX_BIN)])
        os.environ["STUB_CHDMAN_FAIL"] = "extract"
        self.assertEqual(self.go(), 1)
        self.assertIn("bad.chd: chdman could not extract it", self.reason("bad.chd"))
        self.assertEqual(self.library(), [])

    def test_without_chdman_the_chd_is_reviewed(self) -> None:
        self.chd("none.chd", "CHT2", [("MODE2_RAW", PSX_BIN)])
        self.assertEqual(self.go(tools=Tools()), 1)
        self.assertIn("chdman not found", self.reason("none.chd"))

    def test_without_room_nothing_is_extracted(self) -> None:
        self.chd("huge.chd", "CHT2", [("MODE2_RAW", PSX_BIN)], logical=2**40)
        self.assertEqual(self.go(), 1)
        self.assertIn("huge.chd: not enough free space", self.reason("huge.chd"))
        self.assertEqual(self.commands(), ["info"])

    def test_the_room_for_the_new_chd_is_counted_only_when_applying(self) -> None:
        self.chd("edge.chd", "CHT2", [("MODE2_RAW", PSX_BIN)])
        usage = mock.Mock(free=int((len(PSX_BIN) + 1) * 1.05) + 1)
        with mock.patch("shutil.disk_usage", return_value=usage):
            self.assertEqual(self.go(apply=False), 0, self.out)  # the image alone fits
            self.assertEqual(self.go(), 1)  # the image plus the new CHD does not
        self.assertIn("not enough free space", self.reason("edge.chd"))


class ReportOnlyTests(ChdCase):
    def test_the_report_extracts_but_converts_and_writes_nothing(self) -> None:
        item = self.chd(f"{NARUTO}.chd", "CHT2", [("MODE1", PS2_ISO)])
        self.assertEqual(self.go(apply=False), 0, self.out)
        self.assertIn(f"{NARUTO}.chd", self.out)
        self.assertIn(f"ps2/{NARUTO}.chd", self.out)
        self.assertEqual(self.commands(), ["info", "extractcd"])
        self.assertEqual(self.library(), [])
        self.assertTrue(item.exists())
        self.assertEqual(self.done(), [])
        self.assertEqual(os.listdir(self.tmp / "work"), [])

    def test_the_report_of_a_chd_that_matches_nothing_says_so(self) -> None:
        self.chd("odd.chd", "CHT2", [("MODE1", b"odd " * 100)])
        self.assertEqual(self.go(apply=False), 1)
        self.assertIn("odd.chd", self.out)
        self.assertIn("matches no Redump entry", self.out)


if __name__ == "__main__":
    unittest.main()
