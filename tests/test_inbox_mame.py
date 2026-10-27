import io
import os
import tempfile
import time
import unittest
import zipfile
import zlib
from collections.abc import Sequence
from pathlib import Path
from unittest import mock
from xml.sax.saxutils import quoteattr

import test_inbox
from test_cli import run
from warpzone import inbox_mame as mame
from warpzone.config import InboxSettings, parse_inbox
from warpzone.inbox import InboxError, run_inbox
from warpzone.inbox_mame import (
    EMU_2003PLUS,
    EMU_FBNEO,
    Fit,
    Machine,
    Outcome,
    SetDb,
    SetRom,
    best_fit,
    decide,
    describe_miss,
    evaluate,
    load_sets,
    open_dat,
    owner_of,
    parse_sets,
    update_gamelist,
)
from warpzone.provision import Reporter

# Tiny stand-ins for ROM files. Nothing here is a real dump.
BIOS_EURO = b"bios euro " * 40
BIOS_JAPAN = b"bios japan " * 40
SFIX = b"sfix " * 40
SLUG_P1 = b"slug program " * 40
SLUG_C1 = b"slug graphics " * 40
SLUGX_P1 = b"slugx program " * 40
DINO_R1 = b"dino one " * 40
DINO_R2 = b"dino two " * 40
DSP = b"qsound dsp " * 40
OLD_R1 = b"old one " * 40
OLD_R2 = b"old two " * 40
OLD_R2_NEW = b"old two revised " * 40
SOLO_R1 = b"solo one " * 40
SOLO_OPT = b"solo optional " * 40
AW_BIOS = b"atomiswave bios " * 40
AW_GAME = b"atomiswave game " * 40
NAOMI_BIOS = b"naomi bios " * 40
NAOMI_GAME = b"naomi game " * 40
NAOMI_CLONE = b"naomi clone " * 40


def crc(data: bytes) -> str:
    return f"{zlib.crc32(data) & 0xFFFFFFFF:08x}"


def rom_el(name: str, data: bytes | None, **attrs: str) -> str:
    size = len(data) if data is not None else 0
    parts = [f"name={quoteattr(name)}"]
    parts += [f"{k}={quoteattr(v)}" for k, v in attrs.items()]
    parts.append(f'size="{size}"')
    if data is not None:
        parts.append(f'crc="{crc(data)}"')
    return f"<rom {' '.join(parts)}/>"


def machine_el(tag: str, name: str, body: Sequence[str], **attrs: str) -> str:
    head = " ".join([f"name={quoteattr(name)}", *(f"{k}={quoteattr(v)}" for k, v in attrs.items())])
    inner = "\n".join(f"    {line}" for line in body)
    return f"  <{tag} {head}>\n{inner}\n  </{tag}>"


def listxml() -> str:
    """A -listxml shaped like MAME's: BIOS alternatives, merged ROMs, a device."""
    neo_bios = [
        rom_el("sp-s2.sp1", BIOS_EURO, bios="euro"),
        rom_el("vs-bios.rom", BIOS_JAPAN, bios="japan"),
        rom_el("sfix.sfix", SFIX),
    ]
    merged_bios = [
        rom_el("sp-s2.sp1", BIOS_EURO, bios="euro", merge="sp-s2.sp1"),
        rom_el("vs-bios.rom", BIOS_JAPAN, bios="japan", merge="vs-bios.rom"),
        rom_el("sfix.sfix", SFIX, merge="sfix.sfix"),
    ]
    machines = [
        machine_el("machine", "neogeo", neo_bios, isbios="yes"),
        machine_el(
            "machine",
            "mslug",
            [rom_el("201-p1.p1", SLUG_P1), rom_el("201-c1.c1", SLUG_C1), *merged_bios],
            romof="neogeo",
        ),
        machine_el(
            "machine",
            "mslugx",
            [
                rom_el("250-p1.p1", SLUGX_P1),
                rom_el("201-c1.c1", SLUG_C1, merge="201-c1.c1"),
                *merged_bios,
            ],
            cloneof="mslug",
            romof="mslug",
        ),
        machine_el(
            "machine",
            "dino",
            [
                rom_el("cde_23a.8f", DINO_R1),
                rom_el("cde_22a.7f", DINO_R2),
                '<device_ref name="qsound_hle"/>',
                '<device_ref name="speaker"/>',
            ],
        ),
        machine_el(
            "machine",
            "qsound_hle",
            [rom_el("dl-1425.bin", DSP, merge="dl-1425.bin")],
            isdevice="yes",
            runnable="no",
            romof="qsound",
        ),
        machine_el(
            "machine", "qsound", [rom_el("dl-1425.bin", DSP)], isdevice="yes", runnable="no"
        ),
        machine_el("machine", "speaker", [], isdevice="yes", runnable="no"),
        machine_el(
            "machine",
            "solo",
            [
                rom_el("s1.bin", SOLO_R1),
                rom_el("opt.bin", SOLO_OPT, optional="yes"),
                rom_el("nodump.bin", None, status="nodump"),
                '<disk name="solo_disc" sha1="aa" region="cdrom"/>',
            ],
        ),
        machine_el("machine", "empty", ["<description>no roms</description>"]),
    ]
    return (
        '<?xml version="1.0"?>\n<mame build="0.282 (mame0282)" debug="no">\n'
        + "\n".join(machines)
        + "\n</mame>\n"
    )


def fbneo_dat() -> str:
    """FinalBurn Neo has `dino` with no separate QSound device zip to look for."""
    games = [
        machine_el("game", "dino", [rom_el("cde_23a.8f", DINO_R1), rom_el("cde_22a.7f", DINO_R2)]),
    ]
    return '<?xml version="1.0"?>\n<datafile>\n' + "\n".join(games) + "\n</datafile>\n"


def plus_dat() -> str:
    """MAME 2003-Plus: `old` with the older second ROM, and a BIOS marked not runnable."""
    games = [
        machine_el("game", "old", [rom_el("o1.bin", OLD_R1), rom_el("o2.bin", OLD_R2)]),
        machine_el("game", "biosplus", [rom_el("b.bin", BIOS_EURO)], runnable="no"),
    ]
    return '<?xml version="1.0"?>\n<mame>\n' + "\n".join(games) + "\n</mame>\n"


def mame_dat_with_new_old() -> str:
    """MAME's own `old` has a revised second ROM, so a dump of the old one does not fit."""
    return listxml().replace(
        "</mame>",
        machine_el("machine", "old", [rom_el("o1.bin", OLD_R1), rom_el("o2.bin", OLD_R2_NEW)])
        + "\n</mame>",
    )


def mame_dat_with_boards() -> str:
    """Plus an Atomiswave game (BIOS `awbios`), a Naomi game and its clone (the clone's
    `romof` is the parent, whose `romof` is the BIOS) and a game on an unrelated BIOS."""
    boards = [
        machine_el("machine", "awbios", [rom_el("bios.ic23", AW_BIOS)], isbios="yes"),
        machine_el(
            "machine",
            "mslug6",
            [rom_el("ic.u1", AW_GAME), rom_el("bios.ic23", AW_BIOS, merge="bios.ic23")],
            romof="awbios",
        ),
        machine_el("machine", "naomi", [rom_el("epr.bin", NAOMI_BIOS)], isbios="yes"),
        machine_el(
            "machine",
            "ngame",
            [rom_el("g1.ic", NAOMI_GAME), rom_el("epr.bin", NAOMI_BIOS, merge="epr.bin")],
            romof="naomi",
        ),
        machine_el(
            "machine",
            "ngamec",
            [rom_el("g1c.ic", NAOMI_CLONE), rom_el("g1.ic", NAOMI_GAME, merge="g1.ic")],
            cloneof="ngame",
            romof="ngame",
        ),
    ]
    return mame_dat_with_new_old().replace("</mame>", "\n".join(boards) + "\n</mame>")


def database(label: str, emulator: str | None, xml: str) -> SetDb:
    machines, _ = parse_sets(io.BytesIO(xml.encode()))
    return SetDb(label, emulator, machines)


def keys(*chunks: bytes) -> frozenset[tuple[int, str]]:
    return frozenset((len(c), crc(c)) for c in chunks)


def support_of(**zips: frozenset[tuple[int, str]]):  # type: ignore[no-untyped-def]
    return lambda name: zips.get(name)


class ParseTests(unittest.TestCase):
    def setUp(self) -> None:
        self.machines, self.build = parse_sets(io.BytesIO(listxml().encode()))

    def test_kinds_parents_and_build(self) -> None:
        m = self.machines
        self.assertEqual(self.build, "0.282 (mame0282)")
        self.assertEqual(
            (m["neogeo"].kind, m["mslug"].kind, m["qsound_hle"].kind), ("bios", "game", "device")
        )
        self.assertEqual((m["mslugx"].cloneof, m["mslugx"].romof), ("mslug", "mslug"))
        self.assertEqual(m["mslug"].romof, "neogeo")

    def test_nodump_optional_and_empty_sets_are_dropped(self) -> None:
        solo = self.machines["solo"]
        self.assertEqual([r.name for r in solo.roms], ["s1.bin"])
        self.assertEqual(solo.disks, ("solo_disc",))
        self.assertNotIn("empty", self.machines)
        self.assertNotIn("speaker", self.machines)  # a device without ROMs

    def test_devices_are_flattened_to_the_ones_with_roms(self) -> None:
        self.assertEqual(self.machines["dino"].devices, ("qsound_hle",))
        self.assertEqual(self.machines["mslug"].devices, ())

    def test_a_chain_of_device_refs_reaches_the_device_with_roms(self) -> None:
        xml = (
            '<mame>\n<machine name="a"><rom name="x" size="1" crc="00000001"/>'
            '<device_ref name="b"/></machine>\n'
            '<machine name="b"><device_ref name="c"/><device_ref name="a"/></machine>\n'
            '<machine name="c"><rom name="y" size="1" crc="00000002"/></machine>\n</mame>'
        )
        machines, _ = parse_sets(io.BytesIO(xml.encode()))
        self.assertEqual(machines["a"].devices, ("c",))  # through b, cycles ignored

    def test_the_xml_can_come_from_a_zip(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "mame0282lx.zip"
            with zipfile.ZipFile(path, "w") as z:
                z.writestr("readme.txt", "not this")
                z.writestr("mame0282.xml", listxml())
            with open_dat(path) as stream:
                machines, _ = parse_sets(stream)
        self.assertIn("mslug", machines)

    def test_the_index_is_cached_and_read_back_identical(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            dat = Path(tmp) / "list.xml"
            dat.write_text(listxml())
            first = load_sets(dat, Path(tmp) / "cache")
            second = load_sets(dat, Path(tmp) / "cache")
            dat.write_text(listxml() + "\n")  # a different size: the cache is stale
            third = load_sets(dat, Path(tmp) / "cache")
        self.assertEqual((first[2], second[2], third[2]), (False, True, False))
        self.assertEqual(first[0], second[0])

    def test_garbage_is_a_configuration_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            dat = Path(tmp) / "list.xml"
            dat.write_text("this is not xml")
            with self.assertRaises(InboxError):
                load_sets(dat, None)
            dat.write_text("<mame></mame>")
            with self.assertRaises(InboxError):
                load_sets(dat, None)


class FitTests(unittest.TestCase):
    def setUp(self) -> None:
        self.mame = database("MAME 0.282", None, listxml())
        self.fbneo = database(EMU_FBNEO, EMU_FBNEO, fbneo_dat())
        self.plus = database(EMU_2003PLUS, EMU_2003PLUS, plus_dat())
        self.bios = keys(BIOS_EURO, SFIX)

    def test_a_game_fits_when_its_bios_zip_is_in_the_library(self) -> None:
        own = keys(SLUG_P1, SLUG_C1)
        fit = evaluate(self.mame, "mslug", own, support_of(neogeo=self.bios))
        self.assertTrue(fit.ok)
        self.assertEqual(fit.present, {"neogeo"})

    def test_without_the_bios_zip_it_says_which_zip_is_needed(self) -> None:
        own = keys(SLUG_P1, SLUG_C1)
        fit = evaluate(self.mame, "mslug", own, support_of())
        self.assertFalse(fit.ok)
        self.assertEqual({n.owner for n in fit.missing}, {"neogeo"})
        text = describe_miss(Outcome("MAME 0.282", None, fit))
        self.assertIn("needs neogeo.zip", text)
        self.assertIn(f"sfix.sfix {crc(SFIX)}", text)

    def test_one_complete_bios_alternative_is_enough(self) -> None:
        own = keys(SLUG_P1, SLUG_C1)
        japan = keys(BIOS_JAPAN, SFIX)
        self.assertTrue(evaluate(self.mame, "mslug", own, support_of(neogeo=japan)).ok)
        broken = keys(SFIX)  # the shared file but no complete BIOS
        self.assertFalse(evaluate(self.mame, "mslug", own, support_of(neogeo=broken)).ok)

    def test_the_bios_zip_itself_fits_as_a_bios(self) -> None:
        fit = evaluate(self.mame, "neogeo", self.bios, support_of())
        self.assertTrue(fit.ok)
        self.assertEqual(fit.kind, "bios")

    def test_a_clone_is_completed_by_the_parent_zip_in_the_library(self) -> None:
        own = keys(SLUGX_P1)
        parent = keys(SLUG_P1, SLUG_C1)
        self.assertFalse(evaluate(self.mame, "mslugx", own, support_of(neogeo=self.bios)).ok)
        fit = evaluate(self.mame, "mslugx", own, support_of(neogeo=self.bios, mslug=parent))
        self.assertTrue(fit.ok)

    def test_a_device_rom_is_found_in_the_device_zip_or_its_parent_zip(self) -> None:
        own = keys(DINO_R1, DINO_R2)
        none = evaluate(self.mame, "dino", own, support_of())
        self.assertEqual([n.owner for n in none.missing], ["qsound"])
        self.assertEqual([n.scope for n in none.missing], ["qsound_hle"])
        self.assertIn("needs qsound_hle.zip or qsound.zip", describe_miss(Outcome("M", None, none)))
        for name in ("qsound_hle", "qsound"):
            fit = evaluate(self.mame, "dino", own, support_of(**{name: keys(DSP)}))
            self.assertTrue(fit.ok, name)

    def test_the_set_itself_in_the_library_is_not_counted(self) -> None:
        own = keys(DINO_R1)
        fit = evaluate(self.mame, "dino", own, support_of(dino=keys(DINO_R2), qsound=keys(DSP)))
        self.assertFalse(fit.ok)

    def test_a_partly_present_support_zip_is_reported_as_lacking(self) -> None:
        fit = evaluate(self.mame, "mslug", keys(SLUG_P1, SLUG_C1), support_of(neogeo=keys(SFIX)))
        self.assertIn("neogeo.zip in the library lacks", describe_miss(Outcome("M", None, fit)))

    def test_optional_and_nodump_roms_are_not_required_and_disks_are_listed(self) -> None:
        fit = evaluate(self.mame, "solo", keys(SOLO_R1), support_of())
        self.assertTrue(fit.ok)
        self.assertEqual(fit.disks, ("solo_disc",))

    def test_owner_follows_merge_up_the_chain(self) -> None:
        rom = self.mame.machines["mslugx"].roms[1]
        self.assertEqual(owner_of(self.mame, "mslugx", rom), "mslug")
        bios = self.mame.machines["mslugx"].roms[2]
        self.assertEqual(owner_of(self.mame, "mslugx", bios), "neogeo")


class BestFitTests(unittest.TestCase):
    def setUp(self) -> None:
        self.mame = database("MAME 0.282", None, listxml())
        self.neogeo = support_of(neogeo=keys(BIOS_EURO, SFIX))

    def test_nothing_in_common_is_none(self) -> None:
        self.assertIsNone(best_fit(self.mame, keys(b"unrelated"), "x", self.neogeo))

    def test_the_set_is_found_by_content_whatever_the_file_is_called(self) -> None:
        fit = best_fit(self.mame, keys(SLUG_P1, SLUG_C1), "Metal Slug (my copy)", self.neogeo)
        assert fit is not None
        self.assertEqual((fit.machine, fit.ok), ("mslug", True))

    def test_a_merged_zip_is_its_parent_not_the_clone_with_most_members(self) -> None:
        fit = best_fit(self.mame, keys(SLUG_P1, SLUG_C1, SLUGX_P1), "whatever", self.neogeo)
        assert fit is not None
        self.assertEqual(fit.machine, "mslug")

    def test_a_split_clone_zip_is_the_clone_when_the_parent_is_in_the_library(self) -> None:
        support = support_of(neogeo=keys(BIOS_EURO, SFIX), mslug=keys(SLUG_P1, SLUG_C1))
        fit = best_fit(self.mame, keys(SLUGX_P1), "x", support)
        assert fit is not None
        self.assertEqual((fit.machine, fit.ok), ("mslugx", True))

    def test_a_file_named_like_a_set_is_judged_as_that_set_even_if_it_does_not_fit(self) -> None:
        mame = database("MAME 0.282", None, mame_dat_with_new_old())
        fit = best_fit(mame, keys(OLD_R1, OLD_R2), "old", support_of())
        assert fit is not None
        self.assertEqual((fit.machine, fit.ok), ("old", False))
        self.assertEqual([n.rom.name for n in fit.missing], ["o2.bin"])

    def test_a_file_named_like_a_set_that_shares_nothing_is_identified_by_content(self) -> None:
        fit = best_fit(self.mame, keys(DINO_R1, DINO_R2, DSP), "mslug", support_of())
        assert fit is not None
        self.assertEqual(fit.machine, "dino")

    def test_when_nothing_fits_the_closest_set_is_reported(self) -> None:
        fit = best_fit(self.mame, keys(SLUG_P1), "x", self.neogeo)
        assert fit is not None
        self.assertFalse(fit.ok)
        self.assertEqual(fit.machine, "mslug")
        self.assertEqual([n.rom.name for n in fit.missing], ["201-c1.c1"])


class DecideTests(unittest.TestCase):
    def setUp(self) -> None:
        self.mame = database("MAME 0.282", None, mame_dat_with_new_old())
        self.fbneo = database(EMU_FBNEO, EMU_FBNEO, fbneo_dat())
        self.plus = database(EMU_2003PLUS, EMU_2003PLUS, plus_dat())

    def outcomes(self, own: frozenset[tuple[int, str]], stem: str, support=None):  # type: ignore[no-untyped-def]
        support = support or support_of()
        return [
            Outcome(db.label, db.emulator, best_fit(db, own, stem, support))
            for db in (self.fbneo, self.mame, self.plus)  # the order SOURCES gives
        ]

    def test_the_order_is_fbneo_then_mame_then_2003_plus(self) -> None:
        self.assertEqual(
            [(key, emulator) for key, _, emulator in mame.SOURCES],
            [("fbneo_dat", EMU_FBNEO), ("mame_dat", None), ("mame2003plus_dat", EMU_2003PLUS)],
        )

    def test_fbneo_wins_over_mame_when_both_fit(self) -> None:
        # CPS1 games only earn RetroAchievements in FinalBurn Neo.
        d = decide(self.outcomes(keys(DINO_R1, DINO_R2), "dino", support_of(qsound_hle=keys(DSP))))
        self.assertEqual((d.ok, d.set_name, d.emulator, d.why), (True, "dino", EMU_FBNEO, ()))

    def test_mame_is_the_default_when_fbneo_does_not_have_the_set(self) -> None:
        own = keys(SLUG_P1, SLUG_C1)
        d = decide(self.outcomes(own, "mslug", support_of(neogeo=keys(BIOS_EURO, SFIX))))
        self.assertEqual((d.ok, d.set_name, d.emulator), (True, "mslug", None))
        self.assertEqual(d.why, ("FinalBurn Neo: no set shares a file with this zip",))

    def test_the_miss_of_an_earlier_emulator_is_explained(self) -> None:
        d = decide(self.outcomes(keys(DINO_R1, DINO_R2), "dino")[1:2])  # MAME alone
        self.assertFalse(d.ok)
        self.assertIn("MAME 0.282: dino: needs qsound_hle.zip or qsound.zip", d.reason)

    def test_2003_plus_is_the_last_resort(self) -> None:
        d = decide(self.outcomes(keys(OLD_R1, OLD_R2), "old"))
        self.assertEqual((d.ok, d.emulator), (True, EMU_2003PLUS))
        self.assertEqual(len(d.why), 2)

    def test_atomiswave_and_naomi_games_go_to_flycast_when_mame_says_they_are_complete(
        self,
    ) -> None:
        self.mame = database("MAME 0.282", None, mame_dat_with_boards())
        cases = [
            ("mslug6", keys(AW_GAME), {"awbios": keys(AW_BIOS)}, "awbios"),
            ("ngame", keys(NAOMI_GAME), {"naomi": keys(NAOMI_BIOS)}, "naomi"),
            # the clone's romof is its parent; the BIOS is two steps up
            (
                "ngamec",
                keys(NAOMI_CLONE),
                {"naomi": keys(NAOMI_BIOS), "ngame": keys(NAOMI_GAME)},
                "naomi",
            ),
        ]
        for stem, own, zips, bios in cases:
            d = decide(self.outcomes(own, stem, support_of(**zips)))
            self.assertEqual(
                (d.ok, d.set_name, d.emulator, d.flycast_bios), (True, stem, "Flycast", bios)
            )
            self.assertEqual(d.label, "MAME 0.282")

    def test_flycast_needs_the_set_to_be_complete_per_mame(self) -> None:
        self.mame = database("MAME 0.282", None, mame_dat_with_boards())
        d = decide(self.outcomes(keys(AW_GAME), "mslug6", support_of()))
        self.assertFalse(d.ok)
        self.assertIn("MAME 0.282: mslug6: needs awbios.zip", d.reason)
        self.assertEqual(d.emulator, None)

    def test_other_games_are_not_flycast(self) -> None:
        d = decide(
            self.outcomes(keys(SLUG_P1, SLUG_C1), "mslug", support_of(neogeo=keys(BIOS_EURO, SFIX)))
        )
        self.assertEqual((d.emulator, d.flycast_bios), (None, ""))
        self.assertEqual(mame.flycast_bios(self.mame, "mslug"), "")

    def test_the_boards_flycast_runs(self) -> None:
        self.assertEqual(set(mame.FLYCAST_BIOS), {"awbios", "naomi", "naomi2", "naomigd"})

    def test_the_set_name_comes_from_the_emulator_that_fits(self) -> None:
        d = decide(self.outcomes(keys(OLD_R1, OLD_R2), "my old game"))
        self.assertEqual(d.set_name, "old")

    def test_review_lists_what_every_emulator_lacks(self) -> None:
        d = decide(self.outcomes(keys(DINO_R1), "dino"))
        self.assertFalse(d.ok)
        self.assertIn("MAME 0.282: dino: dino.zip lacks 1 file(s): cde_22a.7f", d.reason)
        self.assertIn("FinalBurn Neo: dino:", d.reason)
        self.assertIn("MAME 2003-Plus: no set shares a file with this zip", d.reason)

    def test_a_bios_set_needs_no_emulator_entry(self) -> None:
        d = decide(self.outcomes(keys(BIOS_EURO, SFIX), "neogeo"))
        self.assertEqual((d.ok, d.kind, d.emulator), (True, "bios", None))

    def test_a_2003_plus_bios_is_a_bios(self) -> None:
        self.assertEqual(self.plus.machines["biosplus"].kind, "bios")

    def test_fit_dataclass_is_frozen(self) -> None:
        with self.assertRaises(AttributeError):
            Fit("l", None, "m", "game", True).ok = False  # type: ignore[misc]


GAMELIST = """<?xml version="1.0"?>
<alternativeEmulator>
\t<label>MAME - Current</label>
</alternativeEmulator>
<gameList>
\t<!-- hand written: <game><path>./ghost.zip</path></game> stays as it is -->
\t<game>
\t\t<path>./old.zip</path>
\t\t<name>Old &amp; Gold</name>
\t\t<altemulator>MAME 2003-Plus</altemulator>
\t\t<custom attr="1">kept</custom>
\t</game>
\t<game>
\t\t<path>./dino.zip</path>
\t\t<altemulator>FinalBurn Neo</altemulator>
\t</game>
\t<game id="7" source="x">
\t\t<path>./mslug.zip</path>
\t\t<playcount>3</playcount>
\t</game>
</gameList>
"""


class RecentGamelistTests(unittest.TestCase):
    def test_only_a_change_within_the_window_counts(self) -> None:
        now = 10_000.0
        self.assertFalse(mame.recently_written(None, now))
        self.assertTrue(mame.recently_written(now - 1, now))
        self.assertTrue(mame.recently_written(now - 599, now))
        self.assertFalse(mame.recently_written(now - 600, now))
        self.assertFalse(mame.recently_written(now - 86_400, now))
        self.assertTrue(mame.recently_written(now + 30, now))  # a clock a little ahead
        self.assertTrue(mame.recently_written(now - 40, now, window=60))
        self.assertFalse(mame.recently_written(now - 80, now, window=60))

    def test_the_warning_says_how_long_ago_and_what_to_do(self) -> None:
        text = mame.recent_warning("a/gamelist.xml", 1_000.0, 1_000.0 + 7 * 60 + 5)
        self.assertIn("a/gamelist.xml was modified 7 min ago", text)
        self.assertIn("quit ES-DE on every console", text)

    def test_the_backup_name_and_place(self) -> None:
        when = time.mktime((2026, 1, 2, 3, 4, 5, 0, 0, -1))
        path = mame.backup_path(Path("/hub"), "ps3", when)
        self.assertEqual(
            path, Path("/hub/tools/logs/gamelist-backups/ps3-gamelist-20260102-030405.xml")
        )


class GamelistTests(unittest.TestCase):
    def test_a_missing_entry_is_added_before_the_end_with_the_files_indentation(self) -> None:
        new, action = update_gamelist(GAMELIST, "solo", EMU_FBNEO)
        self.assertEqual(action, "added")
        self.assertEqual(
            new,
            GAMELIST.replace(
                "</gameList>",
                "\t<game>\n\t\t<path>./solo.zip</path>\n"
                "\t\t<altemulator>FinalBurn Neo</altemulator>\n\t</game>\n</gameList>",
            ),
        )

    def test_changing_an_emulator_touches_only_that_value(self) -> None:
        new, action = update_gamelist(GAMELIST, "old", EMU_FBNEO)
        self.assertEqual(action, "changed")
        self.assertEqual(
            new,
            GAMELIST.replace(
                "<altemulator>MAME 2003-Plus</altemulator>",
                "<altemulator>FinalBurn Neo</altemulator>",
            ),
        )

    def test_the_same_value_changes_nothing(self) -> None:
        self.assertEqual(update_gamelist(GAMELIST, "dino", EMU_FBNEO), (GAMELIST, "unchanged"))
        self.assertEqual(update_gamelist(GAMELIST, "nothere", None), (GAMELIST, "unchanged"))
        self.assertEqual(update_gamelist(GAMELIST, "mslug", None), (GAMELIST, "unchanged"))

    def test_an_entry_with_other_data_keeps_it_when_the_override_goes(self) -> None:
        new, action = update_gamelist(GAMELIST, "old", None)
        self.assertEqual(action, "removed")
        self.assertEqual(
            new, GAMELIST.replace("\t\t<altemulator>MAME 2003-Plus</altemulator>\n", "")
        )

    def test_an_entry_that_only_held_the_override_is_removed_whole(self) -> None:
        new, action = update_gamelist(GAMELIST, "dino", None)
        self.assertEqual(action, "removed")
        self.assertEqual(
            new,
            GAMELIST.replace(
                "\t<game>\n\t\t<path>./dino.zip</path>\n"
                "\t\t<altemulator>FinalBurn Neo</altemulator>\n\t</game>\n",
                "",
            ),
        )

    def test_an_entry_without_an_override_gets_one_inside_its_own_tags(self) -> None:
        new, action = update_gamelist(GAMELIST, "mslug", EMU_2003PLUS)
        self.assertEqual(action, "added")
        self.assertEqual(
            new,
            GAMELIST.replace(
                "\t\t<playcount>3</playcount>\n",
                "\t\t<playcount>3</playcount>\n\t\t<altemulator>MAME 2003-Plus</altemulator>\n",
            ),
        )

    def test_a_game_in_a_comment_is_not_an_entry(self) -> None:
        new, action = update_gamelist(GAMELIST, "ghost", EMU_FBNEO)
        self.assertEqual(action, "added")
        self.assertIn("<!-- hand written: <game><path>./ghost.zip</path></game> stays", new)
        self.assertEqual(new.count("./ghost.zip"), 2)

    def test_every_other_entry_survives_and_the_result_parses(self) -> None:
        new, _ = update_gamelist(GAMELIST, "solo", EMU_FBNEO)
        before, after = mame.gamelist_emulators(GAMELIST), mame.gamelist_emulators(new)
        self.assertEqual(after, {**before, "solo.zip": EMU_FBNEO})
        self.assertTrue(mame._well_formed(new))
        self.assertIn('<custom attr="1">kept</custom>', new)
        self.assertIn("<name>Old &amp; Gold</name>", new)

    def test_crlf_files_stay_crlf(self) -> None:
        crlf = GAMELIST.replace("\n", "\r\n")
        new, _ = update_gamelist(crlf, "solo", EMU_FBNEO)
        self.assertEqual(new.count("\n"), new.count("\r\n"))
        self.assertIn("<path>./solo.zip</path>\r\n", new)

    def test_an_empty_or_new_gamelist_gets_its_first_entry(self) -> None:
        for text in (mame.EMPTY_GAMELIST, '<?xml version="1.0"?>\n<gameList />\n'):
            new, action = update_gamelist(text, "dino", EMU_FBNEO)
            self.assertEqual(action, "added")
            self.assertEqual(mame.gamelist_emulators(new), {"dino.zip": EMU_FBNEO})
            self.assertTrue(mame._well_formed(new))

    def test_names_are_escaped(self) -> None:
        new, _ = update_gamelist(GAMELIST, "a&b", "X<Y")
        self.assertIn("<path>./a&amp;b.zip</path>", new)
        self.assertEqual(mame.gamelist_emulators(new)["a&b.zip"], "X<Y")

    def test_no_game_list_is_refused(self) -> None:
        with self.assertRaises(mame.GamelistError):
            update_gamelist("<other/>", "dino", EMU_FBNEO)


class FlowBase(test_inbox.Base):
    def setUp(self) -> None:
        super().setUp()
        self.dat_dir = self.tmp / "arcade"
        self.dat_dir.mkdir()
        self.mame_dat = self.dat_dir / "mame.xml"
        self.mame_dat.write_text(mame_dat_with_new_old())
        self.fbneo_dat = self.dat_dir / "fbneo.dat"
        self.fbneo_dat.write_text(fbneo_dat())
        self.plus_dat = self.dat_dir / "plus.xml"
        self.plus_dat.write_text(plus_dat())
        self.mame_dir = self.roms / "mame"
        self.mame_dir.mkdir()
        self.gamelist = self.hub / "frontend" / "es-de" / "gamelists" / "mame" / "gamelist.xml"

    def settings(self, **kw: str) -> InboxSettings:
        values = {
            "mame_dat": str(self.mame_dat),
            "fbneo_dat": str(self.fbneo_dat),
            "mame2003plus_dat": str(self.plus_dat),
            **kw,
        }
        return InboxSettings(root=str(self.hub), **values)

    def go(self, **kw) -> int:  # type: ignore[no-untyped-def]
        kw.setdefault("settings", self.settings())
        return super().go(**kw)

    def put_library(self, name: str, **files: bytes) -> Path:
        path = self.mame_dir / name
        with zipfile.ZipFile(path, "w") as z:
            for member, data in files.items():
                z.writestr(member.replace("__", "."), data)
        return path

    def write_gamelist(self, text: str) -> None:
        self.gamelist.parent.mkdir(parents=True, exist_ok=True)
        self.gamelist.write_text(text)

    def emulators(self) -> dict[str, str | None]:
        return mame.gamelist_emulators(self.gamelist.read_text())

    def backup_files(self) -> list[Path]:
        folder = self.hub / "tools" / "logs" / "gamelist-backups"
        return sorted(folder.iterdir()) if folder.is_dir() else []

    def mslug_zip(self, name: str = "mslug.zip") -> Path:
        return self.zip_of(name, {"201-p1.p1": SLUG_P1, "201-c1.c1": SLUG_C1})


class FlowTests(FlowBase):
    def test_report_writes_nothing_and_shows_what_it_would_do(self) -> None:
        self.put_library("neogeo.zip", **{"sp-s2__sp1": BIOS_EURO, "sfix__sfix": SFIX})
        self.write_gamelist(GAMELIST)
        self.mslug_zip("Metal Slug (my copy).zip")
        before = self.tree()
        self.assertEqual(self.go(apply=False), 0, self.out)
        self.assertEqual(self.tree(), before)
        self.assertIn("mame: mslug.zip: would place", self.out)
        self.assertIn(
            "renamed from Metal Slug (my copy).zip (the set name in MAME 0.282)", self.out
        )
        self.assertIn("emulator: default (MAME 0.282)", self.out)
        self.assertIn("whitelist: !/roms/mame/mslug.zip", self.out)

    def test_apply_places_renames_and_files_the_original(self) -> None:
        self.put_library("neogeo.zip", **{"sp-s2__sp1": BIOS_EURO, "sfix__sfix": SFIX})
        src = self.mslug_zip("Metal Slug (my copy).zip")
        original = src.read_bytes()
        self.assertEqual(self.go(), 0, self.out)
        self.assertEqual((self.mame_dir / "mslug.zip").read_bytes(), original)
        self.assertEqual(self.done(), ["Metal Slug (my copy).zip"])
        self.assertFalse((self.hub / "frontend").exists())  # default fits: no gamelist needed

    def test_fbneo_goes_first_and_the_override_is_added_keeping_every_other_entry(self) -> None:
        self.write_gamelist(GAMELIST.replace("<altemulator>FinalBurn Neo</altemulator>", ""))
        self.put_library("qsound_hle.zip", **{"dl-1425__bin": DSP})  # MAME would fit too
        self.zip_of("dino.zip", {"cde_23a.8f": DINO_R1, "cde_22a.7f": DINO_R2})
        before = mame.gamelist_emulators(self.gamelist.read_text())
        self.assertEqual(self.go(apply=False), 0, self.out)
        self.assertIn("emulator: FinalBurn Neo", self.out)
        self.assertIn("would add the emulator of dino.zip: FinalBurn Neo", self.out)
        self.assertEqual(mame.gamelist_emulators(self.gamelist.read_text()), before)
        self.assertEqual(self.go(), 0, self.out)
        self.assertEqual(self.emulators()["dino.zip"], EMU_FBNEO)
        self.assertEqual(self.emulators()["old.zip"], EMU_2003PLUS)
        self.assertIn('<custom attr="1">kept</custom>', self.gamelist.read_text())
        self.assertEqual(
            self.backup_files()[0].read_text(),
            GAMELIST.replace("<altemulator>FinalBurn Neo</altemulator>", ""),
        )
        self.assertFalse(self.gamelist.with_name("gamelist.xml.bak").exists())
        self.assertFalse(self.gamelist.with_name("gamelist.xml.part").exists())

    def test_mame_is_the_default_after_fbneo_and_removes_a_stale_override(self) -> None:
        self.write_gamelist(
            GAMELIST.replace(
                "<path>./mslug.zip</path>",
                "<path>./mslug.zip</path>\n\t\t<altemulator>MAME 2003-Plus</altemulator>",
            )
        )
        self.put_library("neogeo.zip", **{"sp-s2__sp1": BIOS_EURO, "sfix__sfix": SFIX})
        self.mslug_zip()
        self.assertEqual(self.go(), 0, self.out)
        self.assertIsNone(self.emulators()["mslug.zip"])
        self.assertIn("<playcount>3</playcount>", self.gamelist.read_text())
        self.assertIn("removed the emulator override of mslug.zip (the default fits)", self.out)
        self.assertEqual(self.emulators()["old.zip"], EMU_2003PLUS)

    def test_atomiswave_goes_to_flycast_and_a_missing_dc_bios_is_noted(self) -> None:
        self.mame_dat.write_text(mame_dat_with_boards())
        self.put_library("awbios.zip", **{"bios__ic23": AW_BIOS})
        self.zip_of("mslug6.zip", {"ic.u1": AW_GAME})
        self.assertEqual(self.go(), 0, self.out)
        self.assertEqual(self.emulators(), {"mslug6.zip": "Flycast"})
        self.assertIn("emulator: Flycast", self.out)
        self.assertIn("Flycast needs awbios.zip in bios/dc/: copy it from roms/mame/", self.out)
        self.assertIn("warpzone never copies BIOS files", self.out)
        self.assertFalse((self.hub / "library" / "bios").exists())  # nothing copied

    def test_no_dc_bios_note_when_the_bios_is_already_there(self) -> None:
        self.mame_dat.write_text(mame_dat_with_boards())
        self.put_library("naomi.zip", **{"epr__bin": NAOMI_BIOS})
        self.put_library("ngame.zip", **{"g1__ic": NAOMI_GAME})
        bios = self.hub / "library" / "bios" / "dc"
        bios.mkdir(parents=True)
        (bios / "naomi.zip").write_bytes(b"whatever")
        self.zip_of("ngamec.zip", {"g1c.ic": NAOMI_CLONE})
        self.assertEqual(self.go(), 0, self.out)
        self.assertEqual(self.emulators(), {"ngamec.zip": "Flycast"})
        self.assertNotIn("bios/dc", self.out)

    def test_an_incomplete_atomiswave_game_goes_to_review_not_to_flycast(self) -> None:
        self.mame_dat.write_text(mame_dat_with_boards())
        self.zip_of("mslug6.zip", {"ic.u1": AW_GAME})
        self.assertEqual(self.go(), 1)
        self.assertIn("needs awbios.zip", self.reason("mslug6.zip"))
        self.assertFalse(self.gamelist.exists())

    def test_2003_plus_fallback_and_a_new_gamelist(self) -> None:
        self.zip_of("old.zip", {"o1.bin": OLD_R1, "o2.bin": OLD_R2})
        self.assertEqual(self.go(), 0, self.out)
        self.assertEqual((self.mame_dir / "old.zip").exists(), True)
        self.assertEqual(self.emulators(), {"old.zip": EMU_2003PLUS})

    def test_gamelist_path_can_be_configured(self) -> None:
        elsewhere = self.tmp / "es-de" / "gl.xml"
        self.zip_of("old.zip", {"o1.bin": OLD_R1, "o2.bin": OLD_R2})
        self.assertEqual(self.go(settings=self.settings(gamelist_mame=str(elsewhere))), 0)
        self.assertEqual(mame.gamelist_emulators(elsewhere.read_text()), {"old.zip": EMU_2003PLUS})

    def test_backup_keeps_the_state_before_the_run_outside_the_synced_tree(self) -> None:
        self.write_gamelist(mame.EMPTY_GAMELIST)
        self.zip_of("old.zip", {"o1.bin": OLD_R1, "o2.bin": OLD_R2})
        self.zip_of("dino.zip", {"cde_23a.8f": DINO_R1, "cde_22a.7f": DINO_R2})
        stamp = time.time()
        name = time.strftime("mame-gamelist-%Y%m%d-%H%M%S.xml", time.localtime(stamp))
        self.assertEqual(self.go(now=lambda: stamp), 0, self.out)
        self.assertEqual(self.emulators(), {"old.zip": EMU_2003PLUS, "dino.zip": EMU_FBNEO})
        folder = self.hub / "tools" / "logs" / "gamelist-backups"
        self.assertEqual([p.name for p in folder.iterdir()], [name])
        self.assertEqual(self.backup_files()[0].read_text(), mame.EMPTY_GAMELIST)
        self.assertEqual(sorted(p.name for p in self.gamelist.parent.iterdir()), ["gamelist.xml"])

    def test_a_recently_modified_gamelist_warns_once_and_is_still_edited(self) -> None:
        self.write_gamelist(mame.EMPTY_GAMELIST)  # written just now
        self.zip_of("old.zip", {"o1.bin": OLD_R1, "o2.bin": OLD_R2})
        self.zip_of("dino.zip", {"cde_23a.8f": DINO_R1, "cde_22a.7f": DINO_R2})
        self.assertEqual(self.go(), 0, self.out)
        self.assertEqual(self.out.count("quit ES-DE on every console"), 1)
        self.assertIn(
            "gamelist frontend/es-de/gamelists/mame/gamelist.xml was modified 0 min ago", self.out
        )
        self.assertEqual(self.emulators(), {"old.zip": EMU_2003PLUS, "dino.zip": EMU_FBNEO})

    def test_an_old_gamelist_and_a_report_run_do_not_warn(self) -> None:
        self.write_gamelist(mame.EMPTY_GAMELIST)
        self.zip_of("old.zip", {"o1.bin": OLD_R1, "o2.bin": OLD_R2})
        self.assertEqual(self.go(apply=False), 0, self.out)  # report only: nothing is edited
        self.assertNotIn("quit ES-DE", self.out)
        test_inbox.age(self.gamelist, 3600)
        self.assertEqual(self.go(), 0, self.out)
        self.assertNotIn("quit ES-DE", self.out)

    def test_the_same_zip_in_the_library_is_already_there_and_the_gamelist_is_reconciled(
        self,
    ) -> None:
        self.put_library("old.zip", **{"o1__bin": OLD_R1, "o2__bin": OLD_R2})
        self.zip_of("old.zip", {"o1.bin": OLD_R1, "o2.bin": OLD_R2})
        self.assertEqual(self.go(), 0, self.out)
        self.assertIn("old.zip: already in the library", self.out)
        self.assertEqual(self.done(), ["old.zip"])
        self.assertEqual(self.emulators(), {"old.zip": EMU_2003PLUS})

    def test_a_different_zip_with_that_name_goes_to_review_and_nothing_is_replaced(self) -> None:
        existing = self.put_library("old.zip", **{"o1__bin": OLD_R1})
        before = existing.read_bytes()
        self.zip_of("old.zip", {"o1.bin": OLD_R1, "o2.bin": OLD_R2})
        self.assertEqual(self.go(), 1)
        self.assertEqual(existing.read_bytes(), before)
        self.assertIn(
            "a different file with that name is already in the library", self.reason("old.zip")
        )
        self.assertFalse(self.gamelist.exists())

    def test_a_set_that_fits_nowhere_goes_to_review_with_the_exact_missing_files(self) -> None:
        self.mslug_zip()
        self.assertEqual(self.go(), 1)
        reason = self.reason("mslug.zip")
        self.assertIn("does not fit any configured emulator", reason)
        self.assertIn("MAME 0.282: mslug: needs neogeo.zip (2 file(s):", reason)
        self.assertIn(f"sp-s2.sp1 {crc(BIOS_EURO)}", reason)
        self.assertEqual(self.library(), [])
        self.assertEqual(self.review(), ["mslug.zip", "mslug.zip.reason.txt"])

    def test_a_zip_with_a_damaged_member_is_not_placed(self) -> None:
        path = self.zip_of("old.zip", {"o1.bin": OLD_R1, "o2.bin": OLD_R2})
        data = bytearray(path.read_bytes())
        data[data.index(OLD_R1[:8]) + 2] ^= 0xFF  # flip a byte of stored data
        path.write_bytes(bytes(data))
        test_inbox.age(path)
        self.assertEqual(self.go(), 1)
        self.assertIn("damaged member", self.reason("old.zip"))
        self.assertEqual(self.library(), [])

    def test_members_in_subfolders_still_match(self) -> None:
        # Recent MAME keeps the clones of a merged set in sub-folders of the parent zip.
        self.put_library("neogeo.zip", **{"sp-s2__sp1": BIOS_EURO, "sfix__sfix": SFIX})
        self.zip_of(
            "mslug.zip", {"201-p1.p1": SLUG_P1, "mslugx/250-p1.p1": SLUGX_P1, "201-c1.c1": SLUG_C1}
        )
        self.assertEqual(self.go(), 0, self.out)
        self.assertEqual(self.library(), ["mame/mslug.zip", "mame/neogeo.zip"])

    def test_bios_and_device_zips_are_filed_without_an_emulator_entry(self) -> None:
        self.zip_of("neogeo.zip", {"sp-s2.sp1": BIOS_EURO, "sfix.sfix": SFIX})
        self.zip_of("qsound_hle.zip", {"dl-1425.bin": DSP})
        self.assertEqual(self.go(), 0, self.out)
        self.assertEqual(self.library(), ["mame/neogeo.zip", "mame/qsound_hle.zip"])
        self.assertIn("bios set (MAME 0.282); no emulator entry needed", self.out)
        self.assertFalse(self.gamelist.exists())

    def test_a_folder_of_zips_is_one_item_with_one_unit_per_zip(self) -> None:
        (self.inbox / "batch").mkdir()
        for name, files in (
            ("old.zip", {"o1.bin": OLD_R1, "o2.bin": OLD_R2}),
            ("qsound_hle.zip", {"dl-1425.bin": DSP}),
        ):
            with zipfile.ZipFile(self.inbox / "batch" / name, "w") as z:
                for m, d in files.items():
                    z.writestr(m, d)
        test_inbox.age(self.inbox / "batch")
        self.assertEqual(self.go(), 0, self.out)
        self.assertEqual(self.library(), ["mame/old.zip", "mame/qsound_hle.zip"])
        self.assertEqual(self.done(), ["batch"])

    def test_a_cartridge_zip_is_not_taken_for_an_arcade_set(self) -> None:
        self.zip_of("adventure.zip", {"Adventure (USA).nes": test_inbox.NES_ROM})
        self.assertEqual(self.go(), 0, self.out)
        self.assertEqual(self.library(), ["nes/Adventure (USA).zip"])
        self.assertNotIn("arcade", self.out.split("[1/1]")[1])

    def test_without_arcade_dats_configured_zips_take_the_usual_path(self) -> None:
        self.mslug_zip()
        self.assertEqual(self.go(settings=InboxSettings()), 1)
        self.assertIn("no DAT match", self.reason("mslug.zip"))

    def test_only_the_dats_that_are_configured_are_used(self) -> None:
        self.zip_of("old.zip", {"o1.bin": OLD_R1, "o2.bin": OLD_R2})
        settings = InboxSettings(root=str(self.hub), mame_dat=str(self.mame_dat))
        self.assertEqual(self.go(settings=settings), 1)
        self.assertIn("does not fit", self.reason("old.zip"))
        self.assertNotIn("FinalBurn", self.reason("old.zip"))

    def test_a_configured_dat_that_does_not_exist_is_a_configuration_error(self) -> None:
        with self.assertRaises(InboxError) as caught:
            run_inbox(
                self.options(settings=self.settings(fbneo_dat=str(self.tmp / "nope.dat"))),
                Reporter(self.lines.append),
            )
        self.assertIn("[inbox] fbneo_dat", str(caught.exception))

    def test_a_gamelist_that_cannot_be_updated_is_reported_after_the_zip_is_placed(self) -> None:
        self.write_gamelist("<other/>")
        self.zip_of("old.zip", {"o1.bin": OLD_R1, "o2.bin": OLD_R2})
        self.assertEqual(self.go(), 1)
        self.assertTrue((self.mame_dir / "old.zip").exists())
        self.assertIn("is in the library but the gamelist was not updated", self.reason("old.zip"))
        self.assertEqual(self.gamelist.read_text(), "<other/>")
        self.write_gamelist(mame.EMPTY_GAMELIST)  # fixed: a rerun retries the gamelist
        self.zip_of("old.zip", {"o1.bin": OLD_R1, "o2.bin": OLD_R2})
        self.assertEqual(self.go(), 0, self.out)
        self.assertIn("old.zip: already in the library", self.out)
        self.assertEqual(self.emulators(), {"old.zip": EMU_2003PLUS})


class ConfigAndCliTests(FlowBase):
    def test_new_keys_are_read_and_expanded(self) -> None:
        errors: list[str] = []
        s = parse_inbox(
            {
                "mame_dat": "~/dats/mame.xml",
                "fbneo_dat": "/x/fbneo.dat",
                "mame2003plus_dat": "/x/plus.xml",
                "gamelist_mame": "/x/gl.xml",
            },
            errors,
        )
        self.assertEqual(errors, [])
        self.assertEqual(s.fbneo_dat, "/x/fbneo.dat")
        self.assertEqual(s.mame_dat, str(Path("~/dats/mame.xml").expanduser()))
        parse_inbox({"mame_dat": ""}, errors)
        self.assertTrue(errors)

    def test_the_cli_passes_the_settings_to_inbox(self) -> None:
        self.zip_of("old.zip", {"o1.bin": OLD_R1, "o2.bin": OLD_R2})
        config = self.tmp / "config.toml"
        config.write_text(
            f'[inbox]\nroot = "{self.hub}"\ndats_dir = "{self.dats}"\n'
            f'mame_dat = "{self.mame_dat}"\nfbneo_dat = "{self.fbneo_dat}"\n'
            f'mame2003plus_dat = "{self.plus_dat}"\n'
        )
        with mock.patch.dict(os.environ, {"XDG_CACHE_HOME": str(self.tmp / "xdg")}):
            code, out, err = run("inbox", "--config", str(config), "--apply")
        self.assertEqual(code, 0, out + err)
        self.assertIn("emulator: MAME 2003-Plus", out)
        self.assertEqual(self.emulators(), {"old.zip": EMU_2003PLUS})

    def test_a_missing_dat_is_a_usage_error_on_the_command_line(self) -> None:
        config = self.tmp / "config.toml"
        config.write_text(
            f'[inbox]\nroot = "{self.hub}"\ndats_dir = "{self.dats}"\n'
            f'mame_dat = "{self.tmp / "nope.xml"}"\n'
        )
        code, _, err = run("inbox", "--config", str(config))
        self.assertEqual(code, 2)
        self.assertIn("mame_dat", err)


class MachineTests(unittest.TestCase):
    def test_machine_and_rom_are_plain_data(self) -> None:
        rom = SetRom("a", 1, "00000001")
        self.assertEqual(rom.key, (1, "00000001"))
        self.assertEqual(Machine("x").kind, "game")


if __name__ == "__main__":
    unittest.main()
