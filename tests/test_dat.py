import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from inbox_support import dat_text, write_dat
from warpzone import dat
from warpzone.dat import (
    Dat,
    DatLibrary,
    FileInfo,
    Game,
    Rom,
    dat_system,
    group_discs,
    load_dat,
    match_dat,
    parse_dat,
    split_disc,
)


def rom(name: str, data: bytes, *, sha1: bool = True) -> Rom:
    import hashlib
    import zlib

    return Rom(
        name,
        len(data),
        f"{zlib.crc32(data) & 0xFFFFFFFF:08x}",
        hashlib.sha1(data).hexdigest() if sha1 else "",
    )


def finfo(path: str, data: bytes) -> FileInfo:
    r = rom(path, data)
    return FileInfo(path, r.size, r.crc, r.sha1)


def nes_finfo(path: str, data: bytes) -> FileInfo:
    """A file the way the inbox fingerprints it: whole, plus the data after the iNES header."""
    body = rom(path, data[16:])
    return replace(finfo(path, data), without_header=(body.size, body.crc, body.sha1))


def make_dat(system: str, games: list[Game]) -> Dat:
    return Dat(system, system, games)


class ParseTests(unittest.TestCase):
    def test_names_with_parentheses_and_quotes(self) -> None:
        text = dat_text("x", [("Game (USA) (Rev A)", [("Game (USA) (Rev A).nes", b"abc")])])
        games = parse_dat(text)
        self.assertEqual([g.name for g in games], ["Game (USA) (Rev A)"])
        self.assertEqual(games[0].roms[0], rom("Game (USA) (Rev A).nes", b"abc"))

    def test_real_layout(self) -> None:
        text = (
            'clrmamepro (\n\tname "Sony - PlayStation"\n)\n\n'
            'game (\n\tname "A (Europe)"\n\tserial "SLES-1"\n'
            '\trom ( name "A (Europe).bin" size 4 crc 8ACD8FB1 md5 AA sha1 '
            '782C50827BF4CF8FE5530B64B188A2D43C75B0E0 serial "SLES-1" )\n)\n'
        )
        (game,) = parse_dat(text)
        self.assertEqual(game.roms[0].crc, "8acd8fb1")
        self.assertEqual(game.roms[0].sha1, "782c50827bf4cf8fe5530b64b188a2d43c75b0e0")

    def test_serials_come_from_the_game_or_its_roms_and_repeats_add_theirs(self) -> None:
        def block(serial: str, rom_serial: str = "") -> str:
            line = f'\tserial "{serial}"\n' if serial else ""
            extra = f' serial "{rom_serial}"' if rom_serial else ""
            return f'game (\n\tname "A"\n{line}\trom ( name "a" size 1 crc 1{extra} )\n)\n'

        text = block("T-1N") + block("MK-2, T-1N") + block("") + block("", "X-3")
        (game,) = parse_dat(text)
        self.assertEqual(game.serials, ("T-1N", "MK-2", "X-3"))
        (lone,) = parse_dat(block("", "R-1"))
        self.assertEqual(lone.serials, ("R-1",))
        (none,) = parse_dat(block(""))
        self.assertEqual(none.serials, ())

    def test_repeats_and_empty_games_are_dropped(self) -> None:
        one = ("Same", [("a.bin", b"1")])
        text = dat_text("x", [one, one, ("Empty", [])])
        self.assertEqual([g.name for g in parse_dat(text)], ["Same"])

    def test_rom_without_valid_crc_is_ignored(self) -> None:
        text = (
            'game (\n name "G"\n rom ( name "a" size 1 crc ZZ )\n rom ( name "b" size 1 crc 1 )\n)'
        )
        (game,) = parse_dat(text)
        self.assertEqual([r.name for r in game.roms], ["b"])
        self.assertEqual(game.roms[0].crc, "00000001")  # short CRCs are zero-padded

    def test_broken_dat_raises(self) -> None:
        for text in ("game (", 'game ( name "x" ) )', "( )"):
            with self.assertRaises(ValueError, msg=text):
                parse_dat(text)

    def test_system_names(self) -> None:
        self.assertEqual(dat_system("Sony - PlayStation.dat"), "psx")
        self.assertEqual(dat_system("Sony - PlayStation 2.dat"), "ps2")
        self.assertEqual(dat_system("Nintendo - Game Boy (20240101-123456).dat"), "gb")
        self.assertEqual(dat_system("Sega - Mega Drive - Genesis.dat"), "genesis")
        self.assertEqual(dat_system("Nintendo - Nintendo DS.dat"), "nds")
        self.assertIsNone(dat_system("Microsoft - Xbox.dat"))
        self.assertEqual(len(set(dat.SYSTEMS.values())), len(dat.SYSTEMS))


class CacheTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)

    def test_cache_is_used_and_invalidated(self) -> None:
        path = write_dat(self.tmp / "dats", "Sony - PlayStation", [("A", [("a.bin", b"1")])])
        cache = self.tmp / "cache"
        first = load_dat(path, "psx", cache)
        self.assertEqual(len(list(cache.glob("*.json"))), 1)
        # Same stat, different text: the cache wins (nothing was re-parsed).
        stat = path.stat()
        path.write_text(dat_text("Sony - PlayStation", [("B", [("b.bin", b"2")])]))
        import os

        os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns))
        self.assertEqual(load_dat(path, "psx", cache).games, first.games)
        # A different size is a different file.
        path.write_text(dat_text("x", [("B", [("b.bin", b"2")]), ("C", [("c.bin", b"3")])]))
        self.assertEqual([g.name for g in load_dat(path, "psx", cache).games], ["B", "C"])

    def test_the_cache_keeps_the_serials(self) -> None:
        path = self.tmp / "dats" / "Sony - PlayStation.dat"
        path.parent.mkdir()
        path.write_text(
            'game (\n\tname "A"\n\tserial "S-1"\n\trom ( name "a" size 1 crc 1 )\n)\n'
            'game (\n\tname "A"\n\tserial "S-2"\n\trom ( name "a" size 1 crc 1 )\n)\n'
        )
        cache = self.tmp / "cache"
        self.assertEqual(load_dat(path, "psx", cache).games[0].serials, ("S-1", "S-2"))
        self.assertEqual(load_dat(path, "psx", cache).games[0].serials, ("S-1", "S-2"))
        self.assertEqual(len(list(cache.glob("*.json"))), 1)

    def test_corrupt_cache_is_ignored(self) -> None:
        path = write_dat(self.tmp / "dats", "Sony - PlayStation", [("A", [("a.bin", b"1")])])
        cache = self.tmp / "cache"
        load_dat(path, "psx", cache)
        next(cache.glob("*.json")).write_text("{nope")
        self.assertEqual([g.name for g in load_dat(path, "psx", cache).games], ["A"])

    def test_unwritable_cache_only_costs_time(self) -> None:
        path = write_dat(self.tmp / "dats", "Sony - PlayStation", [("A", [("a.bin", b"1")])])
        blocker = self.tmp / "file"
        blocker.write_text("")
        self.assertEqual(len(load_dat(path, "psx", blocker / "sub").games), 1)

    def test_library_finds_flat_and_nested_dats_and_loads_lazily(self) -> None:
        dats = self.tmp / "dats"
        write_dat(dats, "Sony - PlayStation", [("A", [("a.bin", b"1")])])
        write_dat(
            dats / "no-intro",
            "Nintendo - Nintendo Entertainment System",
            [("N", [("n.nes", b"2")])],
        )
        write_dat(dats, "Microsoft - Xbox", [("X", [("x.iso", b"3")])])
        lib = DatLibrary(dats, self.tmp / "cache")
        self.assertEqual(lib.systems(), ["nes", "psx"])
        self.assertFalse(
            list((self.tmp / "cache").glob("*")) if (self.tmp / "cache").exists() else []
        )
        self.assertEqual(lib.load("nes")[0].games[0].name, "N")
        self.assertEqual(len(list((self.tmp / "cache").glob("*.json"))), 1)


class MatchTests(unittest.TestCase):
    def test_complete_game_matches_whatever_the_file_names(self) -> None:
        g = Game("Game (USA)", (rom("Game (USA).nes", b"rom"),))
        found = match_dat(make_dat("nes", [g]), [finfo("wrong name.bin", b"rom")])
        self.assertEqual([m.game.name for m in found.matches], ["Game (USA)"])
        self.assertEqual(found.matches[0].files[0].path, "wrong name.bin")

    def test_all_roms_are_needed(self) -> None:
        g = Game("Two", (rom("t1.bin", b"1"), rom("t2.bin", b"2")))
        self.assertEqual(match_dat(make_dat("psx", [g]), [finfo("a", b"1")]).matches, ())
        both = match_dat(make_dat("psx", [g]), [finfo("b", b"2"), finfo("a", b"1")])
        self.assertEqual([f.path for f in both.matches[0].files], ["a", "b"])  # in rom order

    def test_sha1_confirms_when_the_dat_has_one(self) -> None:
        g = Game("G", (rom("g.bin", b"data"),))
        liar = FileInfo("x", len(b"data"), g.roms[0].crc, "0" * 40)
        self.assertEqual(match_dat(make_dat("nes", [g]), [liar]).matches, ())
        no_sha = Game("G", (rom("g.bin", b"data", sha1=False),))
        self.assertEqual(len(match_dat(make_dat("nes", [no_sha]), [liar]).matches), 1)

    def test_size_must_match_too(self) -> None:
        g = Game("G", (rom("g.bin", b"data"),))
        f = FileInfo("x", 99, g.roms[0].crc, g.roms[0].sha1)
        self.assertEqual(match_dat(make_dat("nes", [g]), [f]).matches, ())

    def test_bigger_game_wins_the_shared_file(self) -> None:
        small = Game("Small", (rom("a", b"1"),))
        big = Game("Big", (rom("a", b"1"), rom("b", b"2")))
        found = match_dat(make_dat("psx", [small, big]), [finfo("f1", b"1"), finfo("f2", b"2")])
        self.assertEqual([m.game.name for m in found.matches], ["Big"])

    def test_two_names_with_the_same_files_are_ambiguous(self) -> None:
        a = Game("Alpha", (rom("a", b"1"),))
        b = Game("Beta", (rom("a", b"1"),))
        found = match_dat(make_dat("nes", [a, b]), [finfo("f", b"1")])
        self.assertEqual(found.matches, ())
        self.assertEqual(found.ambiguous, (("Alpha", "Beta"),))

    def test_two_copies_of_one_rom_are_two_matches(self) -> None:
        g = Game("G", (rom("a", b"1"),))
        found = match_dat(make_dat("nes", [g]), [finfo("x/1", b"1"), finfo("y/1", b"1")])
        self.assertEqual(len(found.matches), 2)

    def test_game_with_two_identical_roms_needs_two_files(self) -> None:
        g = Game("G", (rom("a", b"1"), rom("b", b"1")))
        self.assertEqual(match_dat(make_dat("nes", [g]), [finfo("f", b"1")]).matches, ())
        self.assertEqual(
            len(match_dat(make_dat("nes", [g]), [finfo("f", b"1"), finfo("g", b"1")]).matches), 1
        )


class HeaderlessTests(unittest.TestCase):
    BODY = bytes(range(256)) * 32
    OURS = b"NES\x1a\x01\x00\x00\x00" + b"\x00" * 8 + BODY  # a header the DAT does not list
    DAT_GAME = Game("Wrecking (World)", (rom("Wrecking (World).unh", BODY),))

    def test_header_that_differs_matches_the_headerless_entry(self) -> None:
        found = match_dat(make_dat("nes", [self.DAT_GAME]), [nes_finfo("w.nes", self.OURS)])
        self.assertEqual([m.game.name for m in found.matches], ["Wrecking (World)"])
        match = found.matches[0]
        self.assertEqual([f.path for f in match.files], ["w.nes"])
        self.assertEqual(match.files[0].size, len(self.OURS))
        # the rom describes the file as it is, so the zip and its check use the real bytes
        only = match.game.roms[0]
        self.assertEqual((only.name, only.size), ("Wrecking (World).nes", len(self.OURS)))
        self.assertEqual((only.crc, only.sha1), (match.files[0].crc, match.files[0].sha1))

    def test_whole_file_match_wins_over_the_headerless_one(self) -> None:
        headered = Game("Wrecking (World)", (rom("Wrecking (World).nes", self.OURS),))
        found = match_dat(
            make_dat("nes", [headered, self.DAT_GAME]), [nes_finfo("w.nes", self.OURS)]
        )
        self.assertEqual(found.matches[0].game, headered)

    def test_file_without_the_ines_header_is_not_tried(self) -> None:
        found = match_dat(make_dat("nes", [self.DAT_GAME]), [finfo("w.nes", self.OURS)])
        self.assertEqual(found.matches, ())

    def test_no_headerless_entry_for_the_data_is_no_match(self) -> None:
        other = Game("Other", (rom("Other.unh", b"something else"),))
        found = match_dat(make_dat("nes", [other]), [nes_finfo("w.nes", self.OURS)])
        self.assertEqual((found.matches, found.ambiguous), ((), ()))

    def test_same_crc_with_another_sha1_is_no_match(self) -> None:
        r = self.DAT_GAME.roms[0]
        liar = Game("Wrecking (World)", (replace(r, sha1="0" * 40),))
        found = match_dat(make_dat("nes", [liar]), [nes_finfo("w.nes", self.OURS)])
        self.assertEqual(found.matches, ())

    def test_only_nes_dats_are_tried(self) -> None:
        found = match_dat(make_dat("snes", [self.DAT_GAME]), [nes_finfo("w.nes", self.OURS)])
        self.assertEqual(found.matches, ())

    def test_two_names_for_the_same_data_are_ambiguous(self) -> None:
        twin = Game("Twin (World)", (rom("Twin (World).unh", self.BODY),))
        found = match_dat(make_dat("nes", [self.DAT_GAME, twin]), [nes_finfo("w.nes", self.OURS)])
        self.assertEqual(found.matches, ())
        self.assertEqual(found.ambiguous, (("Twin (World)", "Wrecking (World)"),))


def match_for(system: str, name: str, data: bytes) -> dat.GameMatch:
    return dat.GameMatch(system, Game(name, (rom(name + ".bin", data),)), (finfo(name, data),))


class DiscTests(unittest.TestCase):
    def setUp(self) -> None:
        self.psx = make_dat(
            "psx",
            [
                Game(f"Final Fantasy VII (Europe) (Disc {n})", (rom(f"d{n}.bin", bytes([n])),))
                for n in (1, 2, 3)
            ]
            + [Game("Lone (USA) (Disc 1)", (rom("l.bin", b"l"),))]
            + [Game("Single (USA)", (rom("s.bin", b"s"),))]
            + [
                Game("MGS (Spain) (Disc 1) (Rev 1)", (rom("m1.bin", b"m1"),)),
                Game("MGS (Spain) (Disc 2) (Rev 1)", (rom("m2.bin", b"m2"),)),
            ],
        )

    def groups(self, *matches: dat.GameMatch) -> list[dat.DiscGroup]:
        return group_discs(list(matches), {"psx": [self.psx]})

    def test_split_disc(self) -> None:
        self.assertEqual(split_disc("A (Europe) (Disc 2) (Rev 1)"), ("A (Europe)", "2", " (Rev 1)"))
        self.assertEqual(split_disc("A (Europe) (Disc A)"), ("A (Europe)", "A", ""))
        self.assertIsNone(split_disc("A (Europe)"))

    def test_complete_set_is_one_multi_disc_group_in_disc_order(self) -> None:
        ms = [
            match_for("psx", f"Final Fantasy VII (Europe) (Disc {n})", bytes([n]))
            for n in (3, 1, 2)
        ]
        (g,) = self.groups(*ms)
        self.assertTrue(g.multi)
        self.assertEqual(g.name, "Final Fantasy VII (Europe)")
        self.assertEqual([m.game.name[-7:] for m in g.matches], ["Disc 1)", "Disc 2)", "Disc 3)"])
        self.assertEqual((g.missing, g.clash), ((), ()))

    def test_missing_discs_are_named(self) -> None:
        ms = [
            match_for("psx", f"Final Fantasy VII (Europe) (Disc {n})", bytes([n])) for n in (1, 3)
        ]
        (g,) = self.groups(*ms)
        self.assertEqual(g.missing, ("2",))

    def test_a_lone_disc_one_and_a_plain_game_keep_their_names(self) -> None:
        lone = match_for("psx", "Lone (USA) (Disc 1)", b"l")
        plain = match_for("psx", "Single (USA)", b"s")
        groups = self.groups(lone, plain)
        self.assertEqual(
            [(g.name, g.multi) for g in groups],
            [("Lone (USA) (Disc 1)", False), ("Single (USA)", False)],
        )

    def test_a_shared_suffix_stays_in_the_library_name(self) -> None:
        ms = [match_for("psx", f"MGS (Spain) (Disc {n}) (Rev 1)", f"m{n}".encode()) for n in (1, 2)]
        (g,) = self.groups(*ms)
        self.assertEqual(g.name, "MGS (Spain) (Rev 1)")

    def test_the_same_disc_twice_is_a_clash(self) -> None:
        a = match_for("psx", "Final Fantasy VII (Europe) (Disc 1)", bytes([1]))
        b = dat.GameMatch(a.system, a.game, (finfo("other copy", bytes([1])),))
        (g,) = self.groups(a, b)
        self.assertEqual(g.clash, ("1",))

    def test_cartridge_games_are_never_grouped(self) -> None:
        nes = make_dat("nes", [Game("Cart (USA) (Disc 1)", (rom("c", b"c"),))])
        (g,) = group_discs([match_for("nes", "Cart (USA) (Disc 1)", b"c")], {"nes": [nes]})
        self.assertFalse(g.multi)


if __name__ == "__main__":
    unittest.main()
