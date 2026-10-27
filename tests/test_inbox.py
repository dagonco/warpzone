import json
import os
import tempfile
import unittest
import zipfile
from datetime import date
from pathlib import Path
from unittest import mock

from inbox_support import CHDMAN, RAHASHER, age, make_hub, make_stub, stub_md5, write_dat
from test_cli import run
from warpzone import dat, inbox, ra
from warpzone.config import InboxSettings
from warpzone.dat import DiscGroup, Game, GameMatch, Rom
from warpzone.inbox import (
    InboxError,
    Options,
    Staged,
    Tools,
    cue_refs,
    destination_verdict,
    extract_bsdtar,
    extract_zip,
    find_sidecar,
    find_tools,
    gdi_refs,
    plan_units,
    resolve_refs,
    run_inbox,
    scan,
    select_purge,
    skip_name,
    unsafe_member,
    unsupported_reason,
    whitelist_line,
)
from warpzone.provision import Reporter

TODAY = date(2026, 10, 27)
NES_ROM = b"NES\x1a" + b"adventure" * 100
SPIRIT_ROM = b"NES\x1a" + b"other game" * 100
DATA1 = b"data track one " * 200
AUDIO1 = b"audio track " * 100
FF_D1 = b"ff7 disc one " * 100
FF_D2 = b"ff7 disc two " * 100
LONE = b"lone disc " * 100
ISO = b"psp iso " * 500
GC_ISO = b"gamecube iso " * 500
HUB_ISO_NAME = "Some Portable Game (Europe).iso"


def rom(name: str, data: bytes) -> tuple[str, bytes]:
    return name, data


NES_GAMES = [
    ("Adventure (USA)", [rom("Adventure (USA).nes", NES_ROM)]),
    ("Spirit (USA)", [rom("Spirit (USA).nes", SPIRIT_ROM)]),
]
PSX_GAMES = [
    ("Crash Test (Europe)", [rom("Crash Test (Europe) (Track 1).bin", DATA1)]),
    ("Final Fantasy VII (Europe) (Disc 1)", [rom("ff7 d1.bin", FF_D1)]),
    ("Final Fantasy VII (Europe) (Disc 2)", [rom("ff7 d2.bin", FF_D2)]),
    ("Lone Star (USA) (Disc 1)", [rom("lone.bin", LONE)]),
]
PSP_GAMES = [("Some Portable Game (Europe)", [rom("Some Portable Game (Europe).iso", ISO)])]
GC_GAMES = [("Cube Game (USA)", [rom("Cube Game (USA).iso", GC_ISO)])]


DC_CUE = (
    b'FILE "t1.bin" BINARY\n  TRACK 01 MODE1/2352\n    INDEX 01 00:00:00\n'
    b'FILE "t2.raw" BINARY\n  TRACK 02 AUDIO\n    INDEX 01 00:00:00\n'
    b'FILE "t3.bin" BINARY\n  TRACK 03 MODE1/2352\n    INDEX 01 00:00:00\n'
)


def cue(*names: str) -> bytes:
    lines = []
    for n, name in enumerate(names, 1):
        kind = "MODE2/2352" if n == 1 else "AUDIO"
        lines += [f'FILE "{name}" BINARY', f"  TRACK {n:02d} {kind}", "    INDEX 01 00:00:00"]
    return ("\n".join(lines) + "\n").encode()


class Base(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        self.hub = make_hub(self.tmp / "hub")
        self.inbox = self.hub / "inbox"
        self.roms = self.hub / "library" / "roms"
        self.dats = self.tmp / "dats"
        write_dat(self.dats / "no-intro", "Nintendo - Nintendo Entertainment System", NES_GAMES)
        write_dat(self.dats / "redump", "Sony - PlayStation", PSX_GAMES)
        write_dat(self.dats / "redump", "Sony - PlayStation Portable", PSP_GAMES)
        write_dat(self.dats / "redump", "Nintendo - GameCube", GC_GAMES)
        self.bin = self.tmp / "bin"
        self.chdman = make_stub(self.bin, "chdman", CHDMAN)
        self.rahasher = make_stub(self.bin, "RAHasher", RAHASHER)
        self.lines: list[str] = []
        env = mock.patch.dict(os.environ)
        env.start()
        self.addCleanup(env.stop)
        for key in ("STUB_CHDMAN_FAIL", "STUB_RA_MODE", ra.KEY_ENV):
            os.environ.pop(key, None)

    def drop(self, name: str, content: bytes | dict[str, bytes]) -> Path:
        """An item in the inbox, old enough that it is not taken for a copy in progress."""
        path = self.inbox / name
        if isinstance(content, bytes):
            path.write_bytes(content)
        else:
            for rel, data in content.items():
                target = path / rel
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(data)
        age(path)
        return path

    def zip_of(self, name: str, members: dict[str, bytes]) -> Path:
        path = self.inbox / name
        with zipfile.ZipFile(path, "w") as z:
            for member, data in members.items():
                z.writestr(member, data)
        age(path)
        return path

    def options(self, *, apply: bool = True, tools: Tools | None = None, **kw) -> Options:  # type: ignore[no-untyped-def]
        return Options(
            root=self.hub,
            dats_dir=self.dats,
            tools=tools or Tools(chdman=self.chdman, rahasher=self.rahasher),
            apply=apply,
            work_dir=self.tmp / "work",
            cache_dir=self.tmp / "cache",
            today=lambda: TODAY,
            **kw,
        )

    def go(self, **kw) -> int:  # type: ignore[no-untyped-def]
        self.lines.clear()
        return run_inbox(self.options(**kw), Reporter(self.lines.append))

    @property
    def out(self) -> str:
        return "\n".join(self.lines)

    def library(self) -> list[str]:
        return sorted(str(p.relative_to(self.roms)) for p in self.roms.rglob("*") if not p.is_dir())

    def done(self) -> list[str]:
        folder = self.inbox / "_done" / TODAY.isoformat()
        return sorted(p.name for p in folder.iterdir()) if folder.is_dir() else []

    def review(self) -> list[str]:
        folder = self.inbox / "_revisar"
        return sorted(p.name for p in folder.iterdir()) if folder.is_dir() else []

    def reason(self, item: str) -> str:
        return (self.inbox / "_revisar" / f"{item}.reason.txt").read_text()

    def tree(self) -> list[str]:
        return sorted(str(p.relative_to(self.hub)) for p in self.hub.rglob("*"))


class PureTests(unittest.TestCase):
    def test_names_never_processed(self) -> None:
        for name in (".hidden", ".DS_Store", "@eaDir", "Thumbs.db", "__MACOSX", "#recycle"):
            self.assertTrue(skip_name(name), name)
        self.assertFalse(skip_name("Game (USA).zip"))
        self.assertFalse(skip_name("_not_hidden_by_this_rule"))  # `_*` folders are handled by scan

    def test_unsafe_members(self) -> None:
        for name in ("/etc/passwd", "\\win", "C:\\x", "../x", "a/../../x", "a\\..\\x", "a\0b"):
            self.assertIsNotNone(unsafe_member(name), name)
        for name in ("a.nes", "dir/a.nes", "..hidden", "a..b/c", "weird name (1).bin"):
            self.assertIsNone(unsafe_member(name), name)

    def test_purge_only_picks_old_date_folders(self) -> None:
        names = [
            "2026-10-19",
            "2026-10-20",
            "2026-10-27",
            "2026-13-40",
            "notes",
            "2020-01-01x",
            "2030-01-01",
        ]
        self.assertEqual(select_purge(names, TODAY), ["2026-10-19"])
        self.assertEqual(select_purge(names, TODAY, days=0), ["2026-10-19", "2026-10-20"])
        self.assertEqual(select_purge([], TODAY), [])

    def test_whitelist_line_escapes_syncthing_wildcards(self) -> None:
        self.assertEqual(
            whitelist_line("psx", "Silent Hill (Europe) (En,Fr,De,Es,It).chd"),
            "!/roms/psx/Silent Hill (Europe) (En,Fr,De,Es,It).chd",
        )
        self.assertEqual(
            whitelist_line("nes", "A [b] {x} *? \\.zip"),
            "!/roms/nes/A \\[b\\] \\{x\\} \\*\\? \\\\.zip",
        )
        self.assertEqual(
            whitelist_line("psx", "Final Fantasy VII (Europe).m3u"),
            "!/roms/psx/Final Fantasy VII (Europe).m3u",
        )

    def test_unsupported_kinds(self) -> None:
        self.assertIsNone(unsupported_reason(["a/game.NSP", "b/game.xci"]))  # Switch has a handler
        self.assertIn("Vita", unsupported_reason(["x.vpk"]) or "")
        self.assertIn("PS3", unsupported_reason(["g/PS3_GAME/USRDIR/EBOOT.BIN"]) or "")
        self.assertIn("Xbox", unsupported_reason(["g/default.xbe"]) or "")
        self.assertIn("not supported by inbox yet", unsupported_reason(["x.vpk"]) or "")
        self.assertIsNone(unsupported_reason(["Game.nes", "Game.cue"]))

    def test_destination_verdicts(self) -> None:
        self.assertEqual(destination_verdict(False, None), "place")
        self.assertEqual(destination_verdict(True, True), "already")
        self.assertEqual(destination_verdict(True, False), "conflict")
        self.assertEqual(destination_verdict(True, None), "conflict")

    def test_cue_and_gdi_references(self) -> None:
        self.assertEqual(
            cue_refs('FILE "a b.bin" BINARY\nFILE c.bin BINARY\n  TRACK 01 MODE1/2352'),
            ["a b.bin", "c.bin"],
        )
        gdi = (
            "3\n1 0 4 2352 track01.bin 0\n"
            '2 600 0 2352 "track 02.raw" 0\n'
            "3 45000 4 2352 track03.bin 0\n"
        )
        self.assertEqual(gdi_refs(gdi), ["track01.bin", "track 02.raw", "track03.bin"])

    def test_references_resolve_inside_the_item_only(self) -> None:
        available = {"d/a.bin": "d/a.bin", "d/sub/b.bin": "d/sub/b.bin", "a.bin": "A.bin"}
        self.assertEqual(
            resolve_refs("d/x.cue", ["A.BIN", "sub\\b.bin"], available), ("d/a.bin", "d/sub/b.bin")
        )
        self.assertEqual(resolve_refs("x.cue", ["a.bin"], available), ("A.bin",))
        self.assertIsNone(resolve_refs("d/x.cue", ["../a.bin"], available))
        self.assertIsNone(resolve_refs("d/x.cue", ["/etc/passwd"], available))
        self.assertIsNone(resolve_refs("d/x.cue", ["missing.bin"], available))

    def test_plan_units_names_and_problems(self) -> None:
        def m(system: str, name: str) -> GameMatch:
            return GameMatch(system, Game(name, (Rom("r", 1, "0" * 8),)), ())

        single = DiscGroup("psx", "Solo (USA)", (m("psx", "Solo (USA)"),), False)
        multi = DiscGroup(
            "psx",
            "Big (USA)",
            (m("psx", "Big (USA) (Disc 1)"), m("psx", "Big (USA) (Disc 2)")),
            True,
        )
        loose = DiscGroup(
            "gc", "Two (USA)", (m("gc", "Two (USA) (Disc 1)"), m("gc", "Two (USA) (Disc 2)")), True
        )
        cart = DiscGroup("nes", "Cart (USA)", (m("nes", "Cart (USA)"),), False)
        units, problems = plan_units([single, multi, loose, cart])
        self.assertEqual(problems, [])
        self.assertEqual(
            [(u.system, u.entry, len(u.matches)) for u in units],
            [
                ("psx", "Solo (USA).chd", 1),
                ("psx", "Big (USA).m3u", 2),
                ("gc", "Two (USA) (Disc 1).iso", 1),
                ("gc", "Two (USA) (Disc 2).iso", 1),
                ("nes", "Cart (USA).zip", 1),
            ],
        )
        self.assertTrue(units[1].is_m3u and not units[0].is_m3u)
        units, _ = plan_units([cart], lambda match: True)
        self.assertEqual(units[0].entry, "Cart (USA).zip")
        units, _ = plan_units([DiscGroup("gc", "G", (m("gc", "G"),), False)], lambda match: True)
        self.assertEqual(units[0].entry, "G.rvz")

        incomplete = DiscGroup("psx", "Big (USA)", multi.matches[:1], True, missing=("2",))
        clash = DiscGroup("psx", "Big (USA)", multi.matches, True, clash=("1",))
        units, problems = plan_units([incomplete, clash])
        self.assertEqual(units, [])
        self.assertIn("missing disc 2", problems[0])
        self.assertIn("more than one file for disc 1", problems[1])

    def test_find_tools_checks_configured_paths_and_looks_up_the_rest(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            exe = make_stub(Path(tmp), "chdman", "")
            tools = find_tools(InboxSettings(chdman=exe), which=lambda name: f"/usr/bin/{name}")
            self.assertEqual(tools.chdman, exe)
            self.assertEqual(tools.bsdtar, "/usr/bin/bsdtar")
            self.assertEqual(tools.dolphin_tool, "/usr/bin/dolphin-tool")
            self.assertIsNone(tools.rahasher)  # only when configured
            with self.assertRaises(InboxError):
                find_tools(InboxSettings(chdman=str(Path(tmp) / "nope")), which=lambda name: None)
            self.assertEqual(find_tools(InboxSettings(), which=lambda name: None), Tools())


class ArchiveTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)

    def make_zip(self, entries: list[tuple[str, bytes] | zipfile.ZipInfo]) -> Path:
        path = self.tmp / "a.zip"
        with zipfile.ZipFile(path, "w") as z:
            for entry in entries:
                if isinstance(entry, zipfile.ZipInfo):
                    z.writestr(entry, "target")
                else:
                    z.writestr(*entry)
        return path

    def test_extracts_nested_members(self) -> None:
        extract_zip(self.make_zip([("dir/a.bin", b"1"), ("b.bin", b"2")]), self.tmp / "out")
        self.assertEqual((self.tmp / "out/dir/a.bin").read_bytes(), b"1")

    def test_refuses_traversal_absolute_paths_and_links_without_extracting_anything(self) -> None:
        link = zipfile.ZipInfo("link")
        link.external_attr = 0o120777 << 16
        for entries in (
            [("ok.bin", b"1"), ("../evil", b"x")],
            [("/abs", b"x")],
            [("a/../../evil", b"x")],
            [("ok.bin", b"1"), link],
        ):
            out = self.tmp / "out"
            with self.assertRaises(inbox.ItemFailure, msg=str(entries)):
                extract_zip(self.make_zip(entries), out)
            self.assertFalse((out / "ok.bin").exists())
        self.assertFalse((self.tmp / "evil").exists())

    def test_damaged_zip_is_a_failure(self) -> None:
        bad = self.tmp / "bad.zip"
        bad.write_bytes(b"not a zip at all")
        with self.assertRaises(inbox.ItemFailure):
            extract_zip(bad, self.tmp / "out")

    def stub_bsdtar(self, listing: str, *, link: bool = False) -> str:
        source = (
            "import os, sys\n"
            "a = sys.argv[1:]\n"
            f"if a[0] == '-tf':\n    print({listing!r}); sys.exit(0)\n"
            "dest = a[a.index('-C') + 1]\n"
            "open(os.path.join(dest, 'x.bin'), 'w').write('x')\n"
            + ("os.symlink('/etc/passwd', os.path.join(dest, 'l'))\n" if link else "")
        )
        return make_stub(self.tmp / "bin", "bsdtar", source)

    def test_bsdtar_lists_before_it_extracts(self) -> None:
        archive = self.tmp / "a.7z"
        archive.write_bytes(b"7z")
        extract_bsdtar(self.stub_bsdtar("x.bin"), archive, self.tmp / "ok")
        self.assertTrue((self.tmp / "ok/x.bin").exists())
        with self.assertRaises(inbox.ItemFailure):
            extract_bsdtar(self.stub_bsdtar("../x.bin"), archive, self.tmp / "bad")
        self.assertFalse((self.tmp / "bad/x.bin").exists())
        with self.assertRaises(inbox.ItemFailure):
            extract_bsdtar(self.stub_bsdtar("x.bin", link=True), archive, self.tmp / "link")

    def test_sidecar_must_name_the_games_files(self) -> None:
        def staged(*names: str) -> dict[str, Staged]:
            return {n: Staged(self.tmp / n.replace("/", "_"), n) for n in names}

        files = staged("g/a.cue", "g/a.bin", "g/t2.bin", "other.bin")
        (self.tmp / "g_a.cue").write_text('FILE "A.BIN" BINARY\nFILE "t2.bin" BINARY\n')
        match = GameMatch(
            "psx", Game("G", (Rom("a", 1, "0" * 8),)), (dat.FileInfo("g/a.bin", 1, "0" * 8, ""),)
        )
        self.assertEqual(find_sidecar(match, files), ("g/a.cue", ("g/a.bin", "g/t2.bin")))
        other = GameMatch(
            "psx", Game("G", (Rom("a", 1, "0" * 8),)), (dat.FileInfo("other.bin", 1, "0" * 8, ""),)
        )
        self.assertIsNone(find_sidecar(other, files))
        (self.tmp / "g_a.cue").write_text('FILE "../a.bin" BINARY\n')
        self.assertIsNone(find_sidecar(match, files))


class LayoutTests(Base):
    def test_scan_skips_hidden_review_done_and_underscore_folders(self) -> None:
        self.drop("game.zip", b"1")
        self.drop("folder", {"a.bin": b"1"})
        for name in (".hidden", ".DS_Store", "@eaDir"):
            (self.inbox / name).write_bytes(b"")
        for name in ("_revisar", "_done", "_anything", "@eaDir2"):
            (self.inbox / name).mkdir()
        (self.inbox / "_file.zip").write_bytes(b"1")
        self.assertEqual(
            [p.name for p in scan(self.inbox)], ["@eaDir2", "_file.zip", "folder", "game.zip"]
        )

    def test_wrong_root_is_an_error(self) -> None:
        with self.assertRaises(InboxError):
            run_inbox(Options(self.tmp, self.dats, Tools()), Reporter(self.lines.append))
        with self.assertRaises(InboxError):
            run_inbox(Options(self.hub, self.tmp / "nodats", Tools()), Reporter(self.lines.append))
        empty = self.tmp / "empty"
        empty.mkdir()
        with self.assertRaises(InboxError):
            run_inbox(Options(self.hub, empty, Tools()), Reporter(self.lines.append))


class CartridgeTests(Base):
    def test_zip_with_a_wrong_name_end_to_end_with_no_external_tools(self) -> None:
        self.zip_of(
            "whatever it was called.zip", {"some/path/xyz.nes": NES_ROM, "readme.txt": b"hi"}
        )
        before = self.tree()

        self.assertEqual(self.go(apply=False, tools=Tools()), 0)
        self.assertEqual(self.tree(), before, "report only changes nothing in the hub")
        self.assertIn("would place", self.out)
        self.assertIn("library/roms/nes/Adventure (USA).zip", self.out)
        self.assertIn("would move to inbox/_done/2026-10-27/", self.out)
        self.assertIn("report only: run again with --apply", self.out)

        self.assertEqual(self.go(tools=Tools()), 0)
        self.assertEqual(self.library(), ["nes/Adventure (USA).zip"])
        with zipfile.ZipFile(self.roms / "nes" / "Adventure (USA).zip") as z:
            self.assertEqual(z.namelist(), ["Adventure (USA).nes"])
            self.assertEqual(z.read("Adventure (USA).nes"), NES_ROM)
        self.assertEqual(self.done(), ["whatever it was called.zip"])
        self.assertEqual(self.review(), [])
        self.assertIn("placed", self.out)
        self.assertIn("!/roms/nes/Adventure (USA).zip", self.out)
        self.assertNotIn(".part", "".join(self.library()))

    def test_bare_rom_and_folder_with_two_games(self) -> None:
        self.drop("bare.nes", NES_ROM)
        self.drop("pack", {"a/one.bin": SPIRIT_ROM, "notes.txt": b"x"})
        self.assertEqual(self.go(tools=Tools()), 0)
        self.assertEqual(self.library(), ["nes/Adventure (USA).zip", "nes/Spirit (USA).zip"])
        self.assertEqual(self.done(), ["bare.nes", "pack"])

    def test_same_zip_bytes_every_time(self) -> None:
        self.drop("a.nes", NES_ROM)
        self.go(tools=Tools())
        first = (self.roms / "nes" / "Adventure (USA).zip").read_bytes()
        out = self.tmp / "again.zip"
        inbox.make_zip(
            out,
            Game("x", (Rom("Adventure (USA).nes", len(NES_ROM), "0" * 8),)),
            [self.inbox / "_done" / TODAY.isoformat() / "a.nes"],
        )
        self.assertEqual(out.read_bytes(), first)

    def test_second_drop_is_already_in_library_and_the_library_is_untouched(self) -> None:
        self.drop("a.nes", NES_ROM)
        self.go(tools=Tools())
        target = self.roms / "nes" / "Adventure (USA).zip"
        stamp = (target.stat().st_mtime_ns, target.read_bytes())
        self.zip_of("again.zip", {"x.nes": NES_ROM})
        self.assertEqual(self.go(tools=Tools()), 0)
        self.assertIn("already in the library", self.out)
        self.assertEqual((target.stat().st_mtime_ns, target.read_bytes()), stamp)
        self.assertEqual(self.done(), ["a.nes", "again.zip"])

    def test_different_file_with_that_name_is_a_conflict_and_is_never_replaced(self) -> None:
        target = self.roms / "nes" / "Adventure (USA).zip"
        with zipfile.ZipFile(target, "w") as z:
            z.writestr("Adventure (USA).nes", b"a hacked version")
        before = target.read_bytes()
        self.drop("a.nes", NES_ROM)
        self.assertEqual(self.go(tools=Tools()), 1)
        self.assertEqual(target.read_bytes(), before)
        self.assertEqual(self.library(), ["nes/Adventure (USA).zip"])
        self.assertEqual(self.review(), ["a.nes", "a.nes.reason.txt"])
        self.assertIn(
            "different file with that name is already in the library", self.reason("a.nes")
        )

    def test_unknown_file_goes_to_review_with_a_reason(self) -> None:
        self.drop("mystery.nes", b"nobody knows this one")
        self.assertEqual(self.go(tools=Tools()), 1)
        self.assertEqual(self.review(), ["mystery.nes", "mystery.nes.reason.txt"])
        self.assertIn("no DAT match: mystery.nes", self.reason("mystery.nes"))
        self.assertEqual(self.library(), [])

    def test_known_game_next_to_an_unknown_file_is_placed_and_the_item_reviewed(self) -> None:
        self.drop("mixed", {"good.nes": NES_ROM, "mystery.bin": b"???"})
        self.assertEqual(self.go(tools=Tools()), 1)
        self.assertEqual(self.library(), ["nes/Adventure (USA).zip"])
        self.assertIn("mystery.bin", self.reason("mixed"))
        self.assertEqual(self.done(), [])

    def test_review_names_do_not_collide(self) -> None:
        self.drop("m.nes", b"one")
        self.go(tools=Tools())
        self.drop("m.nes", b"two")
        self.go(tools=Tools())
        self.assertEqual(
            self.review(), ["m (2).nes", "m (2).nes.reason.txt", "m.nes", "m.nes.reason.txt"]
        )

    def test_ambiguous_dat_match_is_reviewed(self) -> None:
        write_dat(
            self.dats / "no-intro",
            "Nintendo - Game Boy",
            [("Twin A", [rom("a.gb", b"twin")]), ("Twin B", [rom("b.gb", b"twin")])],
        )
        self.drop("twin.gb", b"twin")
        self.assertEqual(self.go(tools=Tools()), 1)
        self.assertIn(
            "ambiguous DAT match, cannot tell apart: Twin A | Twin B", self.reason("twin.gb")
        )

    def test_unsupported_kinds_never_get_unpacked_or_hashed(self) -> None:
        self.drop("game.vpk", b"vita")
        self.zip_of("pack.zip", {"x/default.xbe": b"xbox"})
        self.assertEqual(self.go(tools=Tools()), 2)
        self.assertIn("not supported by inbox yet (Vita", self.reason("game.vpk"))
        self.assertIn("not supported by inbox yet (Xbox", self.reason("pack.zip"))

    def test_unsafe_archive_is_refused_with_its_reason(self) -> None:
        self.zip_of("evil.zip", {"../escape.nes": NES_ROM})
        self.assertEqual(self.go(tools=Tools()), 1)
        self.assertIn("refused", self.reason("evil.zip"))
        self.assertFalse((self.tmp / "escape.nes").exists())
        self.assertEqual(self.library(), [])

    def test_seven_zip_without_bsdtar_says_so_and_other_items_continue(self) -> None:
        self.drop("packed.7z", b"7z")
        self.drop("fine.nes", NES_ROM)
        self.assertEqual(self.go(tools=Tools()), 1)
        self.assertIn("needs bsdtar", self.reason("packed.7z"))
        self.assertEqual(self.library(), ["nes/Adventure (USA).zip"])

    def test_item_still_being_copied_is_left_alone(self) -> None:
        path = self.drop("fresh.nes", NES_ROM)
        now = path.stat().st_mtime + 5
        self.assertEqual(self.go(now=lambda: now), 0)
        self.assertIn("still being copied", self.out)
        self.assertEqual([p.name for p in self.inbox.iterdir()], ["fresh.nes"])
        self.assertEqual(self.library(), [])

    def test_symbolic_links_are_not_followed(self) -> None:
        outside = self.tmp / "outside.nes"
        outside.write_bytes(NES_ROM)
        folder = self.drop("linky", {"note.txt": b"x"})
        (folder / "l.nes").symlink_to(outside)
        age(folder)
        self.go(tools=Tools())
        self.assertEqual(self.library(), [])
        self.assertIn("skipped symbolic link", self.out)


class PurgeTests(Base):
    def test_only_old_date_folders_are_deleted_and_only_with_apply(self) -> None:
        done = self.inbox / "_done"
        for name in ("2026-10-01", "2026-10-19", "2026-10-20", "notes", "2026-99-99"):
            (done / name).mkdir(parents=True)
            (done / name / "f").write_bytes(b"1")
        (done / "loose-file").write_bytes(b"1")

        self.go(apply=False)
        self.assertIn("would delete 2 folder(s)", self.out)
        self.assertEqual(
            sorted(p.name for p in done.iterdir()),
            ["2026-10-01", "2026-10-19", "2026-10-20", "2026-99-99", "loose-file", "notes"],
        )

        self.go()
        self.assertIn("deleted 2 folder(s)", self.out)
        self.assertEqual(
            sorted(p.name for p in done.iterdir()),
            ["2026-10-20", "2026-99-99", "loose-file", "notes"],
        )

    def test_a_symbolic_link_named_like_a_date_is_not_followed(self) -> None:
        outside = self.tmp / "precious"
        outside.mkdir()
        (outside / "f").write_bytes(b"1")
        (self.inbox / "_done").mkdir()
        (self.inbox / "_done" / "2026-01-01").symlink_to(outside)
        self.go()
        self.assertTrue((outside / "f").exists())


class DiscTests(Base):
    def crash_dump(self, name: str = "crash") -> Path:
        return self.drop(
            name,
            {
                "Crash Test.cue": cue("Crash Test (Track 1).bin", "Crash Test (Track 2).bin"),
                "Crash Test (Track 1).bin": DATA1,
                "Crash Test (Track 2).bin": AUDIO1,
            },
        )

    def test_cue_bin_becomes_a_verified_chd_and_the_audio_track_travels_with_it(self) -> None:
        self.crash_dump()
        self.assertEqual(self.go(), 0)
        chd = self.roms / "psx" / "Crash Test (Europe).chd"
        self.assertEqual(chd.read_text().split("\n")[1], stub_md5(DATA1, AUDIO1))
        self.assertEqual(self.library(), ["psx/Crash Test (Europe).chd"])
        self.assertEqual(self.done(), ["crash"])
        self.assertIn("!/roms/psx/Crash Test (Europe).chd", self.out)
        self.assertNotIn("files not in any DAT", self.out)

    def test_ra_hash_is_compared_before_and_after_and_looked_up(self) -> None:
        self.crash_dump()
        md5 = stub_md5(DATA1, AUDIO1)
        listing = json.dumps(
            [{"Title": "Crash Test", "NumAchievements": 5, "Hashes": [md5]}]
        ).encode()
        seen: list[str] = []

        def fetch(url: str) -> bytes:
            seen.append(url)
            return listing

        self.assertEqual(self.go(ra_key="k", ra_fetch=fetch), 0)
        self.assertIn("RA: supported (Crash Test) [hash kept]", self.out)
        self.assertEqual(len(seen), 1)
        self.assertNotIn("y=k", self.out)

    def test_ra_without_a_key_prints_the_hash_and_says_why(self) -> None:
        self.crash_dump()
        self.go()
        self.assertIn(
            f"RA: hash {stub_md5(DATA1, AUDIO1)} (no RA_API_KEY: not looked up) [hash kept]",
            self.out,
        )

    def test_ra_not_linked(self) -> None:
        self.crash_dump()
        self.go(ra_key="k", ra_fetch=lambda url: b"[]")
        self.assertIn("RA: hash not linked", self.out)

    def test_ra_hash_changed_by_the_conversion_blocks_the_placement(self) -> None:
        self.crash_dump()
        os.environ["STUB_RA_MODE"] = "change"
        self.assertEqual(self.go(), 1)
        self.assertEqual(self.library(), [])
        self.assertIn("RA hash changed by the conversion", self.reason("crash"))
        self.assertEqual(self.review()[0], "crash")

    def test_without_rahasher_nothing_about_ra_is_claimed(self) -> None:
        self.crash_dump()
        self.go(tools=Tools(chdman=self.chdman))
        self.assertIn("RA: not checked", self.out)
        self.assertNotIn("RA: hash", self.out)

    def test_verify_failure_places_nothing_and_leaves_no_part_file(self) -> None:
        self.crash_dump()
        os.environ["STUB_CHDMAN_FAIL"] = "verify"
        self.assertEqual(self.go(), 1)
        self.assertEqual(self.library(), [])
        self.assertIn("chdman verify failed", self.reason("crash"))
        self.assertEqual(self.review(), ["crash", "crash.reason.txt"])

    def test_create_failure_goes_to_review(self) -> None:
        self.crash_dump()
        os.environ["STUB_CHDMAN_FAIL"] = "create"
        self.assertEqual(self.go(), 1)
        self.assertIn("chdman could not convert", self.reason("crash"))

    def test_missing_chdman_reviews_that_item_only(self) -> None:
        self.crash_dump()
        self.drop("cart.nes", NES_ROM)
        self.assertEqual(self.go(tools=Tools()), 1)
        self.assertIn("chdman not found", self.reason("crash"))
        self.assertEqual(self.library(), ["nes/Adventure (USA).zip"])

    def test_lone_bin_without_a_cue_gets_a_generated_one(self) -> None:
        self.drop("bare", {"whatever.bin": DATA1})
        self.assertEqual(self.go(), 0)
        self.assertEqual(
            (self.roms / "psx" / "Crash Test (Europe).chd").read_text().split("\n")[1],
            stub_md5(DATA1),
        )

    def test_cue_that_points_elsewhere_is_never_followed(self) -> None:
        secret = self.tmp / "secret.bin"
        secret.write_bytes(b"not part of the item")
        self.drop("sneaky", {"a.bin": DATA1, "a.cue": cue("../../secret.bin")})
        self.assertEqual(self.go(), 0)  # the generated cue is used instead
        chd = self.roms / "psx" / "Crash Test (Europe).chd"
        self.assertEqual(chd.read_text().split("\n")[1], stub_md5(DATA1))

    def test_audio_track_without_its_cue_keeps_the_item_in_review(self) -> None:
        self.drop("nocue", {"a.bin": DATA1, "audio.bin": AUDIO1})
        self.assertEqual(self.go(), 1)
        self.assertIn("audio.bin", self.reason("nocue"))

    def test_report_only_does_not_convert(self) -> None:
        self.crash_dump()
        before = self.tree()
        self.assertEqual(self.go(apply=False), 0)
        self.assertEqual(self.tree(), before)
        self.assertIn("would place", self.out)
        self.assertIn(f"RA: hash {stub_md5(DATA1, AUDIO1)}", self.out)
        self.assertNotIn("[hash kept]", self.out)

    def test_existing_identical_chd_is_already_in_the_library(self) -> None:
        self.crash_dump()
        self.go()
        chd = self.roms / "psx" / "Crash Test (Europe).chd"
        stamp = chd.stat().st_mtime_ns
        self.crash_dump("crash again")
        self.assertEqual(self.go(), 0)
        self.assertIn("already in the library", self.out)
        self.assertEqual(chd.stat().st_mtime_ns, stamp)
        self.assertEqual(self.done(), ["crash", "crash again"])

    def test_existing_different_chd_is_a_conflict(self) -> None:
        chd = self.roms / "psx" / "Crash Test (Europe).chd"
        chd.write_text("CHD createcd\nsomething else\n")
        self.crash_dump()
        self.assertEqual(self.go(), 1)
        self.assertEqual(chd.read_text(), "CHD createcd\nsomething else\n")
        self.assertIn("different file with that name", self.reason("crash"))

    def test_multi_disc_game_goes_in_an_m3u_folder_with_its_playlist(self) -> None:
        self.drop("ff7", {"d1.bin": FF_D1, "d2.bin": FF_D2})
        self.assertEqual(self.go(), 0)
        self.assertEqual(
            self.library(),
            [
                "psx/Final Fantasy VII (Europe).m3u/Final Fantasy VII (Europe) (Disc 1).chd",
                "psx/Final Fantasy VII (Europe).m3u/Final Fantasy VII (Europe) (Disc 2).chd",
                "psx/Final Fantasy VII (Europe).m3u/Final Fantasy VII (Europe).m3u",
            ],
        )
        playlist = (
            self.roms / "psx" / "Final Fantasy VII (Europe).m3u" / "Final Fantasy VII (Europe).m3u"
        )
        self.assertEqual(
            playlist.read_text().splitlines(),
            ["Final Fantasy VII (Europe) (Disc 1).chd", "Final Fantasy VII (Europe) (Disc 2).chd"],
        )
        self.assertIn("RA disc 1:", self.out)
        self.assertIn("RA disc 2:", self.out)
        self.assertEqual(
            self.out.count("!/roms/psx/Final Fantasy VII (Europe).m3u"), 2
        )  # item and summary
        self.assertEqual(self.done(), ["ff7"])

    def test_multi_disc_with_a_disc_missing_goes_to_review_and_places_nothing(self) -> None:
        self.drop("ff7", {"d1.bin": FF_D1})
        self.assertEqual(self.go(), 1)
        self.assertEqual(self.library(), [])
        self.assertIn(
            "incomplete multi-disc game, 1 disc(s) here, missing disc 2", self.reason("ff7")
        )

    def test_discs_dropped_in_separate_items_are_each_incomplete(self) -> None:
        self.drop("one.bin", FF_D1)
        self.drop("two.bin", FF_D2)
        self.assertEqual(self.go(), 2)
        self.assertEqual(self.library(), [])

    def test_existing_m3u_folder_is_never_merged_into(self) -> None:
        folder = self.roms / "psx" / "Final Fantasy VII (Europe).m3u"
        folder.mkdir()
        (folder / "Final Fantasy VII (Europe) (Disc 1).chd").write_text("CHD createcd\nold\n")
        self.drop("ff7", {"d1.bin": FF_D1, "d2.bin": FF_D2})
        self.assertEqual(self.go(), 1)
        self.assertEqual(
            [p.name for p in folder.iterdir()], ["Final Fantasy VII (Europe) (Disc 1).chd"]
        )
        self.assertIn("different folder with that name", self.reason("ff7"))

    def test_identical_m3u_folder_is_already_in_the_library(self) -> None:
        self.drop("ff7", {"d1.bin": FF_D1, "d2.bin": FF_D2})
        self.go()
        self.drop("ff7 again", {"x1.bin": FF_D1, "x2.bin": FF_D2})
        self.assertEqual(self.go(), 0)
        self.assertIn("already in the library", self.out)

    def test_lone_disc_one_is_a_plain_chd(self) -> None:
        self.drop("lone.bin", LONE)
        self.assertEqual(self.go(), 0)
        self.assertEqual(self.library(), ["psx/Lone Star (USA) (Disc 1).chd"])

    def test_dreamcast_cue_gets_a_gdi_for_ra_and_a_gdi_is_used_as_it_is(self) -> None:
        t1, t2, t3 = b"x" * 2352 * 4, b"y" * 2352 * 2, b"z" * 2352 * 8
        write_dat(
            self.dats / "redump",
            "Sega - Dreamcast",
            [("DC Game (Europe)", [rom("DC Game (Track 3).bin", t3)])],
        )
        item = self.drop(
            "dc",
            {"d.cue": DC_CUE, "t1.bin": t1, "t2.raw": t2, "t3.bin": t3},
        )
        library = dat.DatLibrary(self.dats, None)
        files = {p.name: Staged(p, p.name) for p in item.iterdir()}
        match = dat.match_dat(
            library.load("dreamcast")[0], [inbox.fingerprint(files["t3.bin"].path, "t3.bin")]
        ).matches[0]
        chd_input, ra_input = inbox.find_cd_input(match, files, self.tmp / "scratch")
        self.assertEqual(chd_input.name, "d.cue")
        self.assertEqual(ra_input.name, "disc.gdi")
        self.assertIn('3 45000 4 2352 "t3.bin" 0', ra_input.read_text())
        (item / "d.gdi").write_text(
            "3\n1 0 4 2352 t1.bin 0\n2 600 0 2352 t2.raw 0\n3 45000 4 2352 t3.bin 0\n"
        )
        files["d.gdi"] = Staged(item / "d.gdi", "d.gdi")
        chd_input, ra_input = inbox.find_cd_input(match, files, self.tmp / "scratch2")
        self.assertEqual(
            (chd_input.name, ra_input.name), ("d.cue", "disc.gdi")
        )  # cue and gdi tie: the first wins


class FileSystemTests(Base):
    def test_psp_iso_is_copied_and_checked_against_the_dat(self) -> None:
        self.zip_of("psp.zip", {"renamed.iso": ISO})
        self.assertEqual(self.go(tools=Tools(rahasher=self.rahasher)), 0)
        placed = self.roms / "psp" / HUB_ISO_NAME
        self.assertEqual(placed.read_bytes(), ISO)
        self.assertEqual(self.library(), [f"psp/{HUB_ISO_NAME}"])
        self.assertIn("RA: hash", self.out)

    def test_psp_existing_identical_iso_is_already_there(self) -> None:
        (self.roms / "psp" / HUB_ISO_NAME).write_bytes(ISO)
        self.drop("x.iso", ISO)
        self.assertEqual(self.go(tools=Tools()), 0)
        self.assertIn("already in the library", self.out)

    def test_gc_iso_stays_an_iso_and_the_report_says_how_to_convert(self) -> None:
        self.drop("cube.iso", GC_ISO)
        self.assertEqual(self.go(tools=Tools()), 0)
        self.assertEqual(self.library(), ["gc/Cube Game (USA).iso"])
        self.assertIn("dolphin-tool convert -f rvz -c zstd -l 5 -b 131072", self.out)

    def test_compressed_gamecube_image_needs_dolphin_tool_and_is_kept_as_it_is(self) -> None:
        rvz = b"RVZ\x01 compressed container"
        self.drop("cube.rvz", rvz)
        self.assertEqual(self.go(tools=Tools()), 1)
        self.assertIn("needs dolphin-tool", self.reason("cube.rvz"))
        (self.inbox / "_revisar" / "cube.rvz").rename(self.inbox / "cube.rvz")
        age(self.inbox / "cube.rvz")
        source = (
            "import shutil, sys\n"
            "a = sys.argv[1:]\n"
            "assert a[0] == 'convert' and a[a.index('-f') + 1] == 'iso'\n"
            f"open(a[a.index('-o') + 1], 'wb').write({GC_ISO!r})\n"
        )
        dolphin = make_stub(self.bin, "dolphin-tool", source)
        self.assertEqual(self.go(tools=Tools(dolphin_tool=dolphin)), 0)
        self.assertEqual(self.library(), ["gc/Cube Game (USA).rvz"])
        self.assertEqual((self.roms / "gc" / "Cube Game (USA).rvz").read_bytes(), rvz)
        self.assertNotIn("could be converted", self.out)

    def test_part_file_from_an_earlier_crash_is_replaced(self) -> None:
        (self.roms / "nes" / "Adventure (USA).zip.part").write_bytes(b"half")
        self.drop("a.nes", NES_ROM)
        self.assertEqual(self.go(tools=Tools()), 0)
        self.assertEqual(self.library(), ["nes/Adventure (USA).zip"])


class CliTests(Base):
    def config(self, extra: str = "") -> str:
        path = self.tmp / "config.toml"
        path.write_text(
            f'[inbox]\nroot = "{self.hub}"\ndats_dir = "{self.dats}"\n'
            f'work_dir = "{self.tmp / "work"}"\n'
            f'chdman = "{self.chdman}"\n{extra}'
        )
        return str(path)

    def cli(self, *args: str) -> tuple[int, str, str]:
        with mock.patch.dict(os.environ, {"XDG_CACHE_HOME": str(self.tmp / "xdg")}):
            return run("inbox", *args)

    def test_config_alone_is_enough_and_apply_is_opt_in(self) -> None:
        self.drop("a.nes", NES_ROM)
        code, out, _ = self.cli("--config", self.config())
        self.assertEqual(code, 0, out)
        self.assertIn("report only", out)
        self.assertEqual(self.library(), [])
        code, out, _ = self.cli("--config", self.config(), "--apply")
        self.assertEqual(code, 0, out)
        self.assertEqual(self.library(), ["nes/Adventure (USA).zip"])

    def test_flags_override_the_config_and_need_no_config_file(self) -> None:
        self.drop("a.nes", NES_ROM)
        code, out, _ = self.cli(
            "--config",
            str(self.tmp / "missing.toml"),
            "--root",
            str(self.hub),
            "--dats",
            str(self.dats),
        )
        self.assertEqual(code, 0, out)
        self.assertIn("would place", out)

    def test_items_for_review_make_the_exit_code_1(self) -> None:
        self.drop("mystery.nes", b"?")
        code, out, _ = self.cli("--config", self.config(), "--apply")
        self.assertEqual(code, 1)
        self.assertIn("items for review: 1", out)

    def test_missing_root_or_dats_is_a_usage_error(self) -> None:
        empty = self.tmp / "empty.toml"
        empty.write_text("")
        code, _, err = self.cli("--config", str(empty), "--dats", str(self.dats))
        self.assertEqual(code, 2)
        self.assertIn("no hub root", err)
        code, _, err = self.cli("--config", str(empty), "--root", str(self.hub))
        self.assertEqual(code, 2)
        self.assertIn("no DATs", err)
        code, _, err = self.cli(
            "--config", str(empty), "--root", str(self.tmp), "--dats", str(self.dats)
        )
        self.assertEqual(code, 2)
        self.assertIn("inbox/", err)

    def test_configured_tool_that_does_not_exist_is_a_usage_error(self) -> None:
        code, _, err = self.cli("--config", self.config(f'rahasher = "{self.tmp}/nope"\n'))
        self.assertEqual(code, 2)
        self.assertIn("rahasher", err)

    def test_the_api_key_is_read_from_the_environment_and_never_printed(self) -> None:
        self.drop("crash", {"a.bin": DATA1})
        secret = "environment-secret-key"  # gitleaks:allow (test fixture, not a real key)
        rahasher = make_stub(self.bin, "RAHasher", RAHASHER)
        seen: list[str] = []
        with (
            mock.patch.dict(os.environ, {ra.KEY_ENV: secret}),
            mock.patch.object(ra, "http_fetch", lambda url: seen.append(url) or b"[]"),
        ):
            code, out, err = self.cli("--config", self.config(f'rahasher = "{rahasher}"\n'))
        self.assertEqual(code, 0, out + err)
        self.assertEqual(len(seen), 1)
        self.assertIn(secret, seen[0])
        self.assertNotIn(secret, out + err)
        self.assertIn("RA: hash not linked", out)


if __name__ == "__main__":
    unittest.main()
