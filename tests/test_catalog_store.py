"""The catalog index on a disk: the walk, hashing, guarded writes, `warpzone catalog` and the
hook `inbox --apply` runs. Everything happens in a temporary hub root."""

import hashlib
import json
import os
import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path
from unittest import mock

from support import API_KEY, DECK_ID, HUB_ID
from test_cli import run
from warpzone import catalog, catalog_store, safefile
from warpzone.catalog_store import CatalogError

NOW = datetime(2026, 11, 6, 10, 0, 0, tzinfo=UTC)
LATER = datetime(2026, 11, 7, 10, 0, 0, tzinfo=UTC)


class StoreCase(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.roms = self.root / "library" / "roms"
        self.roms.mkdir(parents=True)
        self.lines: list[str] = []

    def put(self, rel: str, data: bytes | str = b"x", mtime: int = 1_700_000_000) -> Path:
        path = self.root / "library" / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data.encode() if isinstance(data, str) else data)
        os.utime(path, (mtime, mtime))
        return path

    def run_catalog(self, *, dry_run: bool = False, now: datetime = NOW):
        self.lines.clear()
        return catalog_store.run_catalog(self.root, dry_run=dry_run, out=self.lines.append, now=now)

    def system_file(self, system: str) -> dict:
        return json.loads((self.root / "catalog" / "systems" / f"{system}.json").read_text())


class WalkTests(StoreCase):
    def test_walk_stats_everything_and_reads_the_small_files_that_matter(self) -> None:
        self.put("roms/ps4/games/CUSA00001/eboot.bin", b"abc")
        self.put("roms/ps4/shortcuts/Example Game.desktop", "Exec=x\n")
        self.put("roms/ps4/.hidden", b"x")
        (self.root / "library" / "roms" / "ps4" / "link").symlink_to(self.roms)
        listed = catalog_store.walk(self.root / "library", "ps4")
        paths = {n.path: n for n in listed.nodes}
        self.assertEqual(paths["roms/ps4/games/CUSA00001/eboot.bin"].size, 3)
        self.assertEqual(paths["roms/ps4/games/CUSA00001/eboot.bin"].mtime, 1_700_000_000)
        self.assertEqual(paths["roms/ps4/games"].kind, "dir")
        self.assertNotIn("roms/ps4/.hidden", paths)
        self.assertNotIn("roms/ps4/link", paths)
        self.assertEqual(listed.texts, {"roms/ps4/shortcuts/Example Game.desktop": "Exec=x\n"})

    def test_a_system_without_its_folders_lists_nothing(self) -> None:
        listed = catalog_store.walk(self.root / "library", "switch")
        self.assertEqual(list(listed.nodes), [])

    def test_systems_are_the_folders_of_roms(self) -> None:
        self.put("roms/psx/a.chd")
        self.put("roms/gba/a.zip")
        (self.roms / ".stfolder").mkdir()
        (self.roms / "a-file").write_bytes(b"x")
        self.assertEqual(catalog_store.library_systems(self.root), ["gba", "psx"])


class RunCatalogTests(StoreCase):
    def test_a_first_run_writes_one_file_per_system_with_hashes(self) -> None:
        self.put("roms/gba/Alpha.zip", b"alpha")
        self.put("roms/psx/Sample.m3u/d1.chd", b"one")
        self.put("roms/psx/Sample.m3u/d2.chd", b"two!")
        (self.roms / "nes").mkdir()
        results = self.run_catalog()
        self.assertEqual(
            [(r.system, r.status) for r in results],
            [("gba", "written"), ("nes", "unchanged"), ("psx", "written")],
        )
        self.assertFalse((self.root / "catalog" / "systems" / "nes.json").exists())

        doc = self.system_file("gba")
        self.assertEqual(
            (doc["format"], doc["system"], doc["updated"]), (1, "gba", "2026-11-06T10:00:00Z")
        )
        (game,) = doc["games"].values()
        self.assertEqual(game["primary"], "roms/gba/Alpha.zip")
        self.assertEqual(game["added"], "2026-11-06")
        self.assertEqual(game["parts"][0]["sha1"], hashlib.sha1(b"alpha").hexdigest())
        self.assertEqual(game["parts"][0]["mtime"], 1_700_000_000)
        psx = next(iter(self.system_file("psx")["games"].values()))
        self.assertEqual(
            (psx["parts"][0]["type"], psx["parts"][0]["size"], psx["parts"][0]["files"]),
            ("dir", 7, 2),
        )
        self.assertIn("gba: 1 game(s), written (+1 -0 ~0)", self.lines)
        self.assertIn("gba: hashing 1 file(s)", self.lines)

    def test_a_second_run_writes_nothing_and_hashes_nothing(self) -> None:
        self.put("roms/gba/Alpha.zip", b"alpha")
        self.run_catalog()
        path = self.root / "catalog" / "systems" / "gba.json"
        before = (path.read_bytes(), path.stat().st_mtime_ns)
        with (
            mock.patch.object(catalog_store, "file_sha1") as sha1,
            mock.patch.object(safefile, "atomic_write") as write,
        ):
            results = self.run_catalog(now=LATER)
        sha1.assert_not_called()
        write.assert_not_called()
        self.assertEqual([r.status for r in results], ["unchanged"])
        self.assertEqual((path.read_bytes(), path.stat().st_mtime_ns), before)
        self.assertIn("gba: 1 game(s), unchanged", self.lines)

    def test_changes_are_picked_up_and_what_inbox_knows_is_kept(self) -> None:
        self.put("roms/gba/Alpha.zip", b"alpha")
        self.put("roms/gba/Beta.zip", b"beta")
        self.run_catalog()
        stored = self.system_file("gba")
        for game in stored["games"].values():
            if game["primary"].endswith("Alpha.zip"):
                game["dat"], game["added"] = "Alpha (USA)", "2026-10-01"
        (self.root / "catalog" / "systems" / "gba.json").write_text(json.dumps(stored))

        (self.roms / "gba" / "Beta.zip").unlink()
        self.put("roms/gba/Gamma.zip", b"gamma")
        self.put("roms/gba/Alpha.zip", b"alpha changed", mtime=1_700_000_500)
        results = self.run_catalog(now=LATER)
        self.assertEqual((results[0].added, results[0].removed, results[0].changed), (1, 1, 1))

        games = {g["primary"]: g for g in self.system_file("gba")["games"].values()}
        self.assertEqual(sorted(games), ["roms/gba/Alpha.zip", "roms/gba/Gamma.zip"])
        alpha = games["roms/gba/Alpha.zip"]
        self.assertEqual((alpha["dat"], alpha["added"]), ("Alpha (USA)", "2026-10-01"))
        self.assertEqual(alpha["parts"][0]["sha1"], hashlib.sha1(b"alpha changed").hexdigest())
        self.assertEqual(games["roms/gba/Gamma.zip"]["added"], "2026-11-07")

    def test_a_dry_run_says_what_would_change_and_writes_and_hashes_nothing(self) -> None:
        self.put("roms/gba/Alpha.zip", b"alpha")
        with mock.patch.object(catalog_store, "file_sha1") as sha1:
            results = self.run_catalog(dry_run=True)
        sha1.assert_not_called()
        self.assertEqual([r.status for r in results], ["would-write"])
        self.assertIn("gba: 1 game(s), would write (+1 -0 ~0)", self.lines)
        self.assertFalse((self.root / "catalog").exists())

    def test_it_writes_only_under_catalog_and_leaves_the_library_alone(self) -> None:
        self.put("roms/gba/Alpha.zip", b"alpha")
        before = sorted(
            p.relative_to(self.root / "library").as_posix()
            for p in (self.root / "library").rglob("*")
        )
        self.run_catalog()
        after = sorted(
            p.relative_to(self.root / "library").as_posix()
            for p in (self.root / "library").rglob("*")
        )
        self.assertEqual(before, after)
        self.assertEqual(
            sorted(p.relative_to(self.root).as_posix() for p in (self.root / "catalog").rglob("*")),
            ["catalog/systems", "catalog/systems/gba.json"],
        )

    def test_a_file_that_is_not_a_system_file_is_replaced(self) -> None:
        self.put("roms/gba/Alpha.zip", b"alpha")
        path = self.root / "catalog" / "systems" / "gba.json"
        path.parent.mkdir(parents=True)
        path.write_text("garbage")
        self.assertEqual([r.status for r in self.run_catalog()], ["written"])
        self.assertEqual(len(self.system_file("gba")["games"]), 1)

    def test_a_file_that_changes_between_read_and_write_is_read_again(self) -> None:
        self.put("roms/gba/Alpha.zip", b"alpha")
        self.run_catalog()
        self.put("roms/gba/Beta.zip", b"beta")
        path = self.root / "catalog" / "systems" / "gba.json"
        real = safefile.atomic_write
        calls = []

        def racing(target, data, *, expect, **kw):  # type: ignore[no-untyped-def]
            calls.append(expect)
            if len(calls) == 1:  # another writer got there first (a game of its own)
                other = self.system_file("gba")
                other["games"]["ffffffffffffffff"] = other["games"][next(iter(other["games"]))]
                path.write_text(json.dumps(other))
            return real(target, data, expect=expect, **kw)

        with mock.patch.object(safefile, "atomic_write", racing):
            results = self.run_catalog(now=LATER)
        self.assertEqual([r.status for r in results], ["written"])
        self.assertEqual(len(calls), 2)
        self.assertNotEqual(calls[0], calls[1])
        self.assertEqual(
            sorted(g["primary"] for g in self.system_file("gba")["games"].values()),
            ["roms/gba/Alpha.zip", "roms/gba/Beta.zip"],
        )

    def test_a_file_that_keeps_changing_is_given_up_after_three_tries(self) -> None:
        self.put("roms/gba/Alpha.zip", b"alpha")
        with mock.patch.object(safefile, "atomic_write", return_value=False) as write:
            results = self.run_catalog()
        self.assertEqual(write.call_count, catalog_store.WRITE_TRIES)
        self.assertEqual([r.status for r in results], ["conflict"])
        self.assertTrue(results[0].failed)
        self.assertIn("gba: conflict:", "\n".join(self.lines))

    def test_a_file_that_cannot_be_read_is_left_without_a_hash(self) -> None:
        self.put("roms/gba/Alpha.zip", b"alpha")
        self.put("roms/psx/a.chd", b"a")
        real = catalog_store.file_sha1

        def broken(path):  # type: ignore[no-untyped-def]
            if path.name == "Alpha.zip":
                raise PermissionError(13, "Permission denied")
            return real(path)

        with mock.patch.object(catalog_store, "file_sha1", broken):
            results = self.run_catalog()
        # The game is listed anyway, and the file is tried again next time.
        self.assertEqual([r.status for r in results], ["written", "written"])
        game = next(iter(self.system_file("gba")["games"].values()))
        self.assertIsNone(game["parts"][0]["sha1"])

    def test_a_root_without_library_roms_is_a_usage_error(self) -> None:
        with (
            tempfile.TemporaryDirectory() as empty,
            self.assertRaises(CatalogError) as ctx,
        ):
            catalog_store.run_catalog(Path(empty), dry_run=False, out=print)
        self.assertEqual(ctx.exception.exit_code, 2)


class AfterInboxTests(StoreCase):
    def test_the_hook_updates_the_systems_it_names_with_the_dat_names(self) -> None:
        self.put("roms/nes/Adventure (USA).zip", b"adv")
        self.put("roms/gba/Other.zip", b"other")
        warnings: list[str] = []
        catalog_store.update_after_inbox(
            self.root,
            {"nes"},
            dat={"roms/nes/Adventure (USA).zip": "Adventure (USA)"},
            out=self.lines.append,
            warn=warnings.append,
            now=NOW,
        )
        self.assertEqual(warnings, [])
        game = next(iter(self.system_file("nes")["games"].values()))
        self.assertEqual((game["dat"], game["added"]), ("Adventure (USA)", "2026-11-06"))
        self.assertFalse((self.root / "catalog" / "systems" / "gba.json").exists())

    def test_a_failure_is_a_warning(self) -> None:
        self.put("roms/nes/A.zip", b"a")
        warnings: list[str] = []
        with mock.patch.object(safefile, "atomic_write", return_value=False):
            catalog_store.update_after_inbox(
                self.root, {"nes"}, dat={}, out=self.lines.append, warn=warnings.append, now=NOW
            )
        self.assertEqual(len(warnings), 1)
        self.assertIn("nes: conflict", warnings[0])
        self.assertIn("warpzone catalog", warnings[0])


class CliTests(StoreCase):
    def test_catalog_with_root_needs_no_config(self) -> None:
        self.put("roms/gba/Alpha.zip", b"alpha")
        code, out, _ = run("catalog", "--root", str(self.root), "--dry-run")
        self.assertEqual(code, 0)
        self.assertIn("gba: 1 game(s), would write (+1 -0 ~0)", out)
        self.assertFalse((self.root / "catalog").exists())
        code, out, _ = run("catalog", "--root", str(self.root))
        self.assertEqual(code, 0)
        self.assertTrue((self.root / "catalog" / "systems" / "gba.json").is_file())

    def test_the_root_defaults_to_the_hub_root_of_the_config(self) -> None:
        self.put("roms/gba/Alpha.zip", b"alpha")
        config = self.root / "config.toml"
        config.write_text(
            f"""
[hub]
name = "nas"
device_id = "{HUB_ID}"
root = "{self.root}"
api_url = "http://127.0.0.1:1"
api_key = "{API_KEY}"

[devices.deck]
device_id = "{DECK_ID}"
profile = "steamos-emudeck"
folders = ["library"]
"""
        )
        code, out, err = run("catalog", "--config", str(config))
        self.assertEqual((code, err), (0, ""), out)
        self.assertTrue((self.root / "catalog" / "systems" / "gba.json").is_file())

    def test_a_root_without_a_library_is_exit_2_and_a_failed_system_exit_1(self) -> None:
        with tempfile.TemporaryDirectory() as empty:
            code, _, err = run("catalog", "--root", empty)
        self.assertEqual(code, 2)
        self.assertIn("library/roms", err)
        self.put("roms/gba/Alpha.zip", b"alpha")
        with mock.patch.object(safefile, "atomic_write", return_value=False):
            code, _, _ = run("catalog", "--root", str(self.root))
        self.assertEqual(code, 1)


class DerivationOnDiskTests(StoreCase):
    def test_the_layouts_inbox_files_come_out_as_games(self) -> None:
        self.put("roms/ps4/games/CUSA00001/eboot.bin", b"e")
        self.put(
            "roms/ps4/shortcuts/Example Game.desktop",
            'Exec=launcher "/lib/roms/ps4/games/CUSA00001/eboot.bin"\n',
        )
        self.put("roms/psvita/app/PCSE00000/eboot.bin", b"e")
        self.put("roms/psvita/license/PCSE00000/a.rif", b"r")
        self.put("roms/psvita/Example Vita.psvita", "PCSE00000\n")
        self.put("roms/switch/Example [0100AAAA00000000][v0].nsp", b"s")
        self.put("extras/switch/updates/Example/Example [0100AAAA00000800][v1][UPD].nsp", b"u")
        self.put("roms/xbox360/roms/Example Disc.iso", b"x")
        self.put("roms/xbox360/content/save.bin", b"c")
        self.run_catalog()
        games = {
            s: list(self.system_file(s)["games"].values())
            for s in ("ps4", "psvita", "switch", "xbox360")
        }
        self.assertEqual(
            [p["path"] for p in games["ps4"][0]["parts"]],
            ["roms/ps4/games/CUSA00001", "roms/ps4/shortcuts/Example Game.desktop"],
        )
        self.assertEqual(len(games["psvita"][0]["parts"]), 3)
        self.assertEqual(len(games["switch"][0]["parts"]), 2)
        self.assertEqual(
            [g["primary"] for g in games["xbox360"]], ["roms/xbox360/roms/Example Disc.iso"]
        )
        self.assertEqual(
            list(self.system_file("ps4")["games"]), [catalog.game_id("roms/ps4/games/CUSA00001")]
        )


if __name__ == "__main__":
    unittest.main()
