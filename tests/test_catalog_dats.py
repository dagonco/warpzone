"""`warpzone catalog --dats` on a disk: the serials filled in from DATs, the guarded writes, the
summary and the command line. The DATs are fakes; everything happens in a temporary hub root."""

import hashlib
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from inbox_support import write_dat
from test_catalog_store import LATER, StoreCase
from test_cli import run
from warpzone import catalog_store, safefile
from warpzone import dat as datmod
from warpzone.catalog_store import CatalogError


class FakeDats:
    """DATs by system, as `DatLibrary.load` gives them; records the systems it was asked for."""

    def __init__(self, **games: list[datmod.Game]) -> None:
        self.games = games
        self.asked: list[str] = []

    def __call__(self, system: str) -> list[datmod.Dat]:
        self.asked.append(system)
        found = self.games.get(system)
        return [datmod.Dat(system, system, found)] if found is not None else []


def dat_game(name: str, serials: tuple[str, ...], data: bytes | None = None) -> datmod.Game:
    sha1 = hashlib.sha1(data).hexdigest() if data is not None else ""
    size = len(data) if data is not None else 1
    return datmod.Game(name, (datmod.Rom(f"{name}.bin", size, "0" * 8, sha1),), serials)


class DatSerialsTests(StoreCase):
    def setUp(self) -> None:
        super().setUp()
        self.put("roms/dreamcast/Named.chd", b"named chd")
        self.put("roms/dreamcast/Lost.chd", b"lost chd")
        self.put("roms/dreamcast/Two.m3u/Two.m3u", "Two (Disc 1).chd\nTwo (Disc 2).chd\n")
        self.put("roms/dreamcast/Two.m3u/Two (Disc 1).chd", b"d1")
        self.put("roms/dreamcast/Two.m3u/Two (Disc 2).chd", b"d2")
        self.put("roms/psp/Image.iso", b"iso bytes")
        self.put("roms/switch/Thing.nsp", b"nsp")
        self.run_catalog()
        self.dats = FakeDats(
            dreamcast=[
                dat_game("Named (USA)", ("T-1",)),
                dat_game("Two (Disc 1)", ("T-2",)),
                dat_game("Two (Disc 2)", ("T-3", "T-2")),
            ],
            psp=[dat_game("Image (USA)", ("ULUS-1",), b"iso bytes")],
        )
        path = self.root / "catalog" / "systems" / "dreamcast.json"
        doc = json.loads(path.read_text())
        for game in doc["games"].values():
            if game["primary"].endswith("Named.chd"):
                game["dat"] = "Named (USA)"  # what inbox records
        path.write_text(json.dumps(doc))

    def dats_pass(self, *, force=False, dry_run=False):  # type: ignore[no-untyped-def]
        self.lines.clear()
        return catalog_store.run_dats(
            self.root, self.dats, force=force, dry_run=dry_run, out=self.lines.append, now=LATER
        )

    def serials(self, system: str) -> dict[str, list[str]]:
        games = self.system_file(system)["games"].values()
        return {g["primary"].rsplit("/", 1)[1]: g["serials"] for g in games}

    def statuses(self, results) -> dict[str, str]:  # type: ignore[no-untyped-def]
        return {r.system: r.status for r in results}

    def test_games_are_filled_by_dat_name_by_hash_and_by_their_discs(self) -> None:
        results = self.dats_pass()
        self.assertEqual(
            self.serials("dreamcast"),
            {"Named.chd": ["T-1"], "Lost.chd": [], "Two.m3u": ["T-2", "T-3"]},
        )
        self.assertEqual(self.serials("psp"), {"Image.iso": ["ULUS-1"]})
        counts = {r.system: (r.status, r.filled, r.had, r.unmatched) for r in results}
        self.assertEqual(counts["dreamcast"], ("written", 2, 0, 1))
        self.assertEqual(counts["psp"], ("written", 1, 0, 0))
        self.assertIn("dreamcast: 2 filled, 0 already had, 1 unmatched", self.lines)
        self.assertIn("  unmatched: roms/dreamcast/Lost.chd", self.lines)
        self.assertIn("psp: 1 filled, 0 already had, 0 unmatched", self.lines)

    def test_a_system_without_a_dat_with_serials_is_reported_and_left_alone(self) -> None:
        path = self.root / "catalog" / "systems" / "switch.json"
        before = path.read_bytes()
        results = self.dats_pass()
        self.assertIn("switch: no DAT with serials; 1 game(s) left without", self.lines)
        self.assertEqual(self.statuses(results)["switch"], "no-dat")
        self.assertEqual(path.read_bytes(), before)
        self.dats.games["psp"] = [dat_game("No serial here", (), b"iso bytes")]
        psp = self.root / "catalog" / "systems" / "psp.json"
        doc = json.loads(psp.read_text())
        for game in doc["games"].values():
            game["serials"] = []
        psp.write_text(json.dumps(doc))
        self.dats_pass()
        self.assertIn("psp: no DAT with serials; 1 game(s) left without", self.lines)

    def test_games_that_have_serials_are_not_looked_up_unless_forced(self) -> None:
        self.dats_pass()
        self.dats.asked.clear()
        results = self.dats_pass()
        self.assertEqual(self.statuses(results)["psp"], "unchanged")
        self.assertEqual(self.dats.asked, ["dreamcast", "switch"])  # Lost.chd and Thing.nsp
        self.assertIn("dreamcast: 0 filled, 2 already had, 1 unmatched", self.lines)
        self.assertIn("psp: 0 filled, 1 already had, 0 unmatched", self.lines)

    def test_force_replaces_serials_the_dat_gives_other_ones_for(self) -> None:
        self.dats_pass()
        self.dats.games["psp"] = [dat_game("Image (USA)", ("ULUS-2",), b"iso bytes")]
        self.dats.games["dreamcast"] = []
        self.dats_pass()
        self.assertEqual(self.serials("psp"), {"Image.iso": ["ULUS-1"]})
        results = self.dats_pass(force=True)
        self.assertEqual(self.serials("psp"), {"Image.iso": ["ULUS-2"]})
        psp = next(r for r in results if r.system == "psp")
        self.assertEqual((psp.status, psp.filled), ("written", 1))
        self.assertEqual(self.serials("dreamcast")["Named.chd"], ["T-1"])  # no match: kept

    def test_a_dry_run_says_what_it_would_fill_and_writes_nothing(self) -> None:
        before = {p: p.read_bytes() for p in (self.root / "catalog").rglob("*.json")}
        with mock.patch.object(safefile, "atomic_write") as write:
            results = self.dats_pass(dry_run=True)
        write.assert_not_called()
        self.assertEqual({p: p.read_bytes() for p in before}, before)
        self.assertEqual(self.statuses(results)["dreamcast"], "would-write")
        self.assertIn("dreamcast: 2 to fill, 0 already had, 1 unmatched", self.lines)

    def test_it_writes_only_under_catalog_and_only_the_serials(self) -> None:
        library = sorted(p.as_posix() for p in (self.root / "library").rglob("*"))
        stored = self.system_file("dreamcast")
        self.dats_pass()
        self.assertEqual(sorted(p.as_posix() for p in (self.root / "library").rglob("*")), library)
        after = self.system_file("dreamcast")
        self.assertEqual(after["updated"], "2026-11-07T10:00:00Z")
        for ident, game in after["games"].items():
            self.assertEqual({**game, "serials": []}, {**stored["games"][ident], "serials": []})

    def test_a_file_that_changes_between_read_and_write_is_read_again(self) -> None:
        path = self.root / "catalog" / "systems" / "psp.json"
        real = safefile.atomic_write
        calls = []

        def racing(target, data, *, expect, **kw):  # type: ignore[no-untyped-def]
            if Path(target) == path:
                calls.append(expect)
            if len(calls) == 1 and Path(target) == path:
                other = self.system_file("psp")
                other["games"][next(iter(other["games"]))]["title"] = "Changed by another writer"
                path.write_text(json.dumps(other))
            return real(target, data, expect=expect, **kw)

        with mock.patch.object(safefile, "atomic_write", racing):
            results = self.dats_pass()
        self.assertEqual(self.statuses(results)["psp"], "written")
        self.assertEqual(len(calls), 2)
        self.assertNotEqual(calls[0], calls[1])
        game = next(iter(self.system_file("psp")["games"].values()))
        self.assertEqual(game["title"], "Changed by another writer")
        self.assertEqual(game["serials"], ["ULUS-1"])

    def test_a_file_that_keeps_changing_is_a_conflict(self) -> None:
        with mock.patch.object(safefile, "atomic_write", return_value=False):
            results = self.dats_pass()
        self.assertEqual({r.status for r in results if r.failed}, {"conflict"})
        self.assertTrue(any(line.startswith("psp: conflict:") for line in self.lines))

    def test_a_dat_that_cannot_be_read_is_an_error_for_its_system_only(self) -> None:
        def load(system: str) -> list[datmod.Dat]:
            if system == "dreamcast":
                raise ValueError("Sega - Dreamcast.dat: not a valid clrmamepro DAT (x)")
            return self.dats(system)

        self.lines.clear()
        results = catalog_store.run_dats(
            self.root, load, force=False, dry_run=False, out=self.lines.append, now=LATER
        )
        statuses = self.statuses(results)
        self.assertEqual((statuses["dreamcast"], statuses["psp"]), ("error", "written"))
        self.assertTrue(any(line.startswith("dreamcast: error: DATs: ") for line in self.lines))

    def test_a_single_file_with_no_dat_name_is_found_by_its_file_name(self) -> None:
        self.put("roms/dreamcast/Plain (USA).chd", b"plain chd")
        self.run_catalog()
        self.dats.games["dreamcast"] = [dat_game("Plain (USA)", ("T-7",))]
        self.dats_pass()
        self.assertEqual(self.serials("dreamcast")["Plain (USA).chd"], ["T-7"])
        self.assertEqual(self.serials("dreamcast")["Lost.chd"], [])

    def test_a_name_with_two_serial_lists_is_not_guessed(self) -> None:
        self.dats.games["dreamcast"] = [
            dat_game("Named (USA)", ("T-1",)),
            dat_game("Named (USA)", ("T-9",), b"other"),
        ]
        self.dats_pass()
        self.assertEqual(self.serials("dreamcast")["Named.chd"], [])

    def test_systems_without_a_catalog_file_are_skipped(self) -> None:
        for path in (self.root / "catalog").rglob("*.json"):
            path.unlink()
        self.assertEqual(self.dats_pass(), [])
        self.assertEqual(self.lines, ["no catalog files: run `warpzone catalog` first"])

    def test_a_root_without_a_library_is_a_usage_error(self) -> None:
        with tempfile.TemporaryDirectory() as empty, self.assertRaises(CatalogError):
            catalog_store.run_dats(Path(empty), self.dats, force=False, dry_run=False, out=print)

    def test_many_unmatched_games_are_listed_up_to_a_limit(self) -> None:
        extra = 3
        for i in range(catalog_store.UNMATCHED_SHOWN + extra):
            self.put(f"roms/dreamcast/Extra {i:02}.chd", f"extra {i}".encode())
        self.run_catalog()
        self.dats_pass()
        shown = [line for line in self.lines if line.startswith("  unmatched: ")]
        self.assertEqual(len(shown), catalog_store.UNMATCHED_SHOWN)
        self.assertIn(f"  ... and {extra + 1} more unmatched", self.lines)


class DatsCliTests(StoreCase):
    def setUp(self) -> None:
        super().setUp()
        self.none = str(self.root / "no-config.toml")
        self.dats_dir = self.root / "dats"
        path = write_dat(self.dats_dir, "Sega - Dreamcast", [("Game (USA)", [("t.bin", b"track")])])
        text = path.read_text().replace(
            'name "Game (USA)"\n', 'name "Game (USA)"\n\tserial "T-1D"\n'
        )
        path.write_text(text)
        self.put("roms/dreamcast/Game (USA).chd", b"a chd")
        env = mock.patch.dict(os.environ, {"XDG_CACHE_HOME": str(self.root / "cache")})
        env.start()
        self.addCleanup(env.stop)

    def catalog(self, *args: str, config: str | None = None) -> tuple[int, str, str]:
        return run("catalog", "--config", config or self.none, "--root", str(self.root), *args)

    def mark_dat(self) -> None:
        path = self.root / "catalog" / "systems" / "dreamcast.json"
        doc = json.loads(path.read_text())
        for game in doc["games"].values():
            game["dat"] = "Game (USA)"
        path.write_text(json.dumps(doc))

    def test_dats_fills_the_serials_from_the_dat_folder_given(self) -> None:
        self.catalog()
        self.mark_dat()
        code, out, err = self.catalog("--dats", "--dats-dir", str(self.dats_dir))
        self.assertEqual((code, err), (0, ""), out)
        self.assertIn("dreamcast: 1 filled, 0 already had, 0 unmatched", out)
        game = next(iter(self.system_file("dreamcast")["games"].values()))
        self.assertEqual(game["serials"], ["T-1D"])

    def test_dats_takes_the_folder_of_the_inbox_table_and_a_dry_run_writes_nothing(self) -> None:
        config = self.root / "config.toml"
        config.write_text(f'[inbox]\ndats_dir = "{self.dats_dir}"\n')
        self.catalog()
        self.mark_dat()
        path = self.root / "catalog" / "systems" / "dreamcast.json"
        before = path.read_bytes()
        code, out, _ = self.catalog("--dats", "--dry-run", config=str(config))
        self.assertEqual(code, 0)
        self.assertIn("dreamcast: 1 to fill, 0 already had, 0 unmatched", out)
        self.assertEqual(path.read_bytes(), before)

    def test_dats_scans_the_library_first(self) -> None:
        code, out, _ = self.catalog("--dats", "--dats-dir", str(self.dats_dir))
        self.assertEqual(code, 0)
        self.assertIn("dreamcast: 1 game(s), written", out)
        self.assertIn("dreamcast: 1 filled, 0 already had, 0 unmatched", out)  # by file name

    def test_a_missing_folder_or_a_folder_without_dats_is_exit_2_before_the_scan(self) -> None:
        code, _, err = self.catalog("--dats")
        self.assertEqual(code, 2)
        self.assertIn("dats_dir", err)
        code, _, err = self.catalog("--dats", "--dats-dir", str(self.root / "nope"))
        self.assertEqual(code, 2)
        self.assertIn("DAT folder not found", err)
        (self.root / "empty").mkdir()
        code, _, err = self.catalog("--dats", "--dats-dir", str(self.root / "empty"))
        self.assertEqual(code, 2)
        self.assertIn("no Redump or No-Intro DATs", err)
        self.assertFalse((self.root / "catalog").exists())

    def test_the_options_that_need_another_are_refused(self) -> None:
        for args, text in (
            (("--dats-dir", str(self.dats_dir)), "--dats"),
            (("--force",), "--dats"),
        ):
            code, _, err = self.catalog(*args)
            self.assertEqual(code, 2)
            self.assertIn(text, err)

    def test_a_failed_write_is_exit_1(self) -> None:
        self.catalog()
        self.mark_dat()
        with mock.patch.object(safefile, "atomic_write", return_value=False):
            code, out, _ = self.catalog("--dats", "--dats-dir", str(self.dats_dir))
        self.assertEqual(code, 1)
        self.assertIn("dreamcast: conflict:", out)


if __name__ == "__main__":
    unittest.main()
