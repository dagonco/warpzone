"""The catalog index on a disk: the walk, hashing, guarded writes, `warpzone catalog` and the
hook `inbox --apply` runs. Everything happens in a temporary hub root."""

import hashlib
import io
import json
import os
import tempfile
import unittest
import urllib.error
from datetime import UTC, datetime
from pathlib import Path
from unittest import mock

from inbox_support import RAHASHER, make_stub
from support import API_KEY, DECK_ID, HUB_ID
from test_cli import run
from warpzone import catalog, catalog_store, ra, safefile
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


SECRET = "store-secret-key"  # gitleaks:allow (test fixture, not a real key)
LIST_12 = json.dumps(
    [{"Title": "Alpha Quest", "ID": 77, "NumAchievements": 9, "Points": 90, "Hashes": ["x" * 32]}]
).encode()


def md5_of(data: bytes) -> str:
    return hashlib.md5(data).hexdigest()


class FakeHasher:
    """A RAHasher stand-in: the md5 of the file, recorded; `None` for the names in `fail`."""

    def __init__(self, fail: tuple[str, ...] = ()) -> None:
        self.calls: list[tuple[str, str]] = []
        self.fail = fail

    def __call__(self, system: str, path: Path) -> str | None:
        self.calls.append((system, path.name))
        if path.name in self.fail:
            return None
        return md5_of(path.read_bytes())


class RaHashTests(StoreCase):
    def run_hash(self, hasher):  # type: ignore[no-untyped-def]
        self.lines.clear()
        return catalog_store.run_catalog(
            self.root, dry_run=False, out=self.lines.append, now=NOW, ra_hasher=hasher
        )

    def ra_of(self, system: str) -> dict[str, dict | None]:
        return {g["primary"]: g["ra"] for g in self.system_file(system)["games"].values()}

    def test_games_of_the_ra_systems_get_a_hash_of_their_primary_part(self) -> None:
        self.put("roms/gba/Alpha.zip", b"alpha", mtime=1_700_000_100)
        self.put("roms/wii/Wii Game.rvz", b"wii")  # not in ra.CONSOLE_IDS
        hasher = FakeHasher()
        results = self.run_hash(hasher)
        self.assertEqual(hasher.calls, [("gba", "Alpha.zip")])
        self.assertEqual(
            self.ra_of("gba"),
            {
                "roms/gba/Alpha.zip": {
                    "hash": md5_of(b"alpha"),
                    "of": "roms/gba/Alpha.zip",
                    "size": 5,
                    "mtime": 1_700_000_100,
                }
            },
        )
        self.assertEqual(self.ra_of("wii"), {"roms/wii/Wii Game.rvz": None})
        self.assertEqual(
            [(r.system, r.ra_hashed, r.ra_left) for r in results], [("gba", 1, 0), ("wii", 0, 0)]
        )
        self.assertIn("gba: 1 game(s), written (+1 -0 ~0); RA: 1 hashed", self.lines)

    def test_a_second_pass_hashes_nothing_and_writes_nothing(self) -> None:
        self.put("roms/gba/Alpha.zip", b"alpha")
        self.run_hash(FakeHasher())
        again = FakeHasher()
        with mock.patch.object(safefile, "atomic_write") as write:
            results = self.run_hash(again)
        self.assertEqual(again.calls, [])
        write.assert_not_called()
        self.assertEqual([r.status for r in results], ["unchanged"])

    def test_a_part_that_changed_is_hashed_again_and_the_others_are_not(self) -> None:
        self.put("roms/gba/Alpha.zip", b"alpha")
        self.put("roms/gba/Beta.zip", b"beta")
        self.run_hash(FakeHasher())
        self.put("roms/gba/Beta.zip", b"beta two", mtime=1_700_000_900)
        again = FakeHasher()
        self.run_hash(again)
        self.assertEqual(again.calls, [("gba", "Beta.zip")])
        stored = self.ra_of("gba")["roms/gba/Beta.zip"]
        assert stored is not None
        self.assertEqual(stored["hash"], md5_of(b"beta two"))

    def test_an_m3u_folder_is_hashed_by_its_first_disc(self) -> None:
        self.put("roms/psx/Saga.m3u/Saga (Disc 1).chd", b"one")
        self.put("roms/psx/Saga.m3u/Saga (Disc 2).chd", b"two")
        self.put("roms/psx/Saga.m3u/Saga.m3u", "Saga (Disc 1).chd\nSaga (Disc 2).chd\n")
        hasher = FakeHasher()
        self.run_hash(hasher)
        self.assertEqual(hasher.calls, [("psx", "Saga (Disc 1).chd")])
        stored = self.ra_of("psx")["roms/psx/Saga.m3u"]
        assert stored is not None
        self.assertEqual(stored["of"], "roms/psx/Saga.m3u/Saga (Disc 1).chd")
        self.assertEqual(stored["hash"], md5_of(b"one"))

    def test_a_stopped_pass_keeps_what_it_finished_and_the_next_one_goes_on(self) -> None:
        for name in "ABCDE":
            self.put(f"roms/gba/{name}.zip", name.encode())

        class Stopped(FakeHasher):
            def __call__(self, system: str, path: Path) -> str | None:
                if len(self.calls) == 3:
                    raise KeyboardInterrupt
                return super().__call__(system, path)

        with mock.patch.object(catalog_store, "RA_BATCH", 2), self.assertRaises(KeyboardInterrupt):
            self.run_hash(Stopped())
        done = [p for p, stored in self.ra_of("gba").items() if stored]
        self.assertEqual(done, ["roms/gba/A.zip", "roms/gba/B.zip"])  # the first batch
        rest = FakeHasher()
        with mock.patch.object(catalog_store, "RA_BATCH", 2):
            self.run_hash(rest)
        self.assertEqual([name for _, name in rest.calls], ["C.zip", "D.zip", "E.zip"])
        self.assertTrue(all(self.ra_of("gba").values()))

    def test_each_batch_is_written_before_the_next_starts(self) -> None:
        for name in "ABCDE":
            self.put(f"roms/gba/{name}.zip", name.encode())
        with (
            mock.patch.object(catalog_store, "RA_BATCH", 2),
            mock.patch.object(safefile, "atomic_write", wraps=safefile.atomic_write) as write,
        ):
            self.run_hash(FakeHasher())
        self.assertEqual(write.call_count, 3)  # 2 + 2 + 1 games

    def test_a_game_that_cannot_be_hashed_is_tried_once_per_run(self) -> None:
        self.put("roms/gba/A.zip", b"a")
        self.put("roms/gba/B.zip", b"b")
        hasher = FakeHasher(fail=("A.zip",))
        results = self.run_hash(hasher)
        self.assertEqual(hasher.calls.count(("gba", "A.zip")), 1)
        self.assertEqual((results[0].ra_hashed, results[0].ra_failed), (1, 1))
        self.assertIn(
            "gba: 2 game(s), written (+2 -0 ~0); RA: 1 hashed, 1 could not be hashed", self.lines
        )
        self.assertEqual(
            [p for p, stored in self.ra_of("gba").items() if stored is None], ["roms/gba/A.zip"]
        )
        later = FakeHasher()
        self.run_hash(later)  # a later run tries again
        self.assertEqual(later.calls, [("gba", "A.zip")])

    def test_a_file_that_changes_while_it_is_hashed_is_left_for_the_next_run(self) -> None:
        path = self.put("roms/gba/A.zip", b"a")

        def hasher(system: str, file: Path) -> str | None:
            os.utime(path, (1_700_000_999, 1_700_000_999))
            return md5_of(b"a")

        results = self.run_hash(hasher)
        self.assertEqual((results[0].ra_hashed, results[0].ra_failed), (0, 1))
        self.assertEqual(self.ra_of("gba"), {"roms/gba/A.zip": None})

    def test_a_dry_run_runs_no_hasher_and_says_how_many_games_wait(self) -> None:
        self.put("roms/gba/A.zip", b"a")
        self.put("roms/gba/B.zip", b"b")
        self.run_catalog()
        hasher = FakeHasher()
        self.lines.clear()
        results = catalog_store.run_catalog(
            self.root, dry_run=True, out=self.lines.append, now=NOW, ra_hasher=hasher
        )
        self.assertEqual(hasher.calls, [])
        self.assertEqual(results[0].ra_left, 2)
        self.assertEqual(self.ra_of("gba"), {"roms/gba/A.zip": None, "roms/gba/B.zip": None})

    def test_without_a_hasher_no_ra_work_is_done(self) -> None:
        self.put("roms/gba/A.zip", b"a")
        results = self.run_catalog()
        self.assertEqual((results[0].ra_hashed, results[0].ra_left), (0, 0))
        self.assertEqual(self.ra_of("gba"), {"roms/gba/A.zip": None})

    def test_the_hash_inbox_knew_is_stored_without_running_the_hasher(self) -> None:
        self.put("roms/psx/Saga.m3u/Saga (Disc 1).chd", b"one", mtime=1_700_000_050)
        self.put("roms/psx/Saga.m3u/Saga.m3u", "Saga (Disc 1).chd\n")
        self.put("roms/gba/A.zip", b"a")
        warnings: list[str] = []
        catalog_store.update_after_inbox(
            self.root,
            {"psx", "gba"},
            dat={},
            ra_known={
                "roms/psx/Saga.m3u": ("f" * 32, "roms/psx/Saga.m3u/Saga (Disc 1).chd"),
                "roms/gba/A.zip": ("e" * 32, "roms/gba/Other.zip"),  # not the part we hash
            },
            out=self.lines.append,
            warn=warnings.append,
            now=NOW,
        )
        self.assertEqual(warnings, [])
        self.assertEqual(
            self.ra_of("psx")["roms/psx/Saga.m3u"],
            {
                "hash": "f" * 32,
                "of": "roms/psx/Saga.m3u/Saga (Disc 1).chd",
                "size": 3,
                "mtime": 1_700_000_050,
            },
        )
        self.assertIsNone(self.ra_of("gba")["roms/gba/A.zip"])
        hasher = FakeHasher()  # and the pass has nothing left to do for the first
        self.run_hash(hasher)
        self.assertEqual(hasher.calls, [("gba", "A.zip")])


class RaListsTests(StoreCase):
    def setUp(self) -> None:
        super().setUp()
        self.put("roms/gba/Alpha.zip", b"alpha")  # console 5
        self.put("roms/psx/Beta.chd", b"beta")  # console 12
        self.put("roms/switch/Gamma.nsp", b"gamma")  # not in ra.CONSOLE_IDS
        self.urls: list[str] = []
        self.warnings: list[str] = []

    def fetch(self, url: str) -> bytes:
        self.urls.append(url)
        return LIST_12

    def lists(self, *, fetch=None, force=False, dry_run=False, now=NOW, key=SECRET):  # type: ignore[no-untyped-def]
        self.lines.clear()
        return catalog_store.run_ra_lists(
            self.root,
            key=key,
            force=force,
            dry_run=dry_run,
            out=self.lines.append,
            warn=self.warnings.append,
            fetch=fetch or self.fetch,
            now=now,
        )

    def cached(self, console: int) -> dict:
        return json.loads((self.root / "catalog" / "ra" / f"{console}.json").read_text())

    def test_each_console_the_library_has_is_downloaded_and_cached(self) -> None:
        results = self.lists()
        self.assertEqual(
            [(r.console, r.status) for r in results], [(5, "fetched"), (12, "fetched")]
        )
        self.assertEqual(sorted(u.split("?")[1].split("&")[0] for u in self.urls), ["i=12", "i=5"])
        self.assertEqual(
            self.cached(12),
            {
                "format": 1,
                "console": 12,
                "fetched": "2026-11-06T10:00:00Z",
                "games": {
                    "x" * 32: {"id": 77, "title": "Alpha Quest", "achievements": 9, "points": 90}
                },
            },
        )
        self.assertFalse((self.root / "catalog" / "ra" / "3.json").exists())

    def test_a_list_under_seven_days_old_is_not_downloaded_again(self) -> None:
        self.lists()
        self.urls.clear()
        results = self.lists(now=datetime(2026, 11, 12, 9, tzinfo=UTC))
        self.assertEqual(self.urls, [])
        self.assertEqual({r.status for r in results}, {"fresh"})
        self.assertIn("RA list 12 (psx): up to date, 1 game(s)", self.lines)

    def test_force_downloads_a_fresh_list_again(self) -> None:
        self.lists()
        self.urls.clear()
        results = self.lists(force=True)
        self.assertEqual(len(self.urls), 2)
        self.assertEqual({r.status for r in results}, {"fetched"})

    def test_an_expired_list_is_downloaded_again_and_replaced(self) -> None:
        self.lists()
        later = datetime(2026, 11, 14, 10, 0, 0, tzinfo=UTC)  # 8 days
        self.lists(now=later, fetch=lambda url: b"[]")
        self.assertEqual(self.cached(12)["fetched"], "2026-11-14T10:00:00Z")
        self.assertEqual(self.cached(12)["games"], {})

    def test_a_failed_download_keeps_the_cached_list_and_warns(self) -> None:
        self.lists()
        before = (self.root / "catalog" / "ra" / "12.json").read_bytes()

        def down(url: str) -> bytes:
            raise urllib.error.URLError(f"cannot reach {url}")

        results = self.lists(now=datetime(2026, 11, 20, tzinfo=UTC), fetch=down)
        self.assertEqual({r.status for r in results}, {"kept"})
        self.assertFalse(any(r.failed for r in results))
        self.assertEqual((self.root / "catalog" / "ra" / "12.json").read_bytes(), before)
        self.assertEqual(len(self.warnings), 2)
        self.assertIn("download failed (URLError)", self.warnings[0])
        self.assertIn("keeping the list from 2026-11-06T10:00:00Z", self.warnings[0])

    def test_a_failed_download_with_no_cache_is_a_failed_result(self) -> None:
        def denied(url: str) -> bytes:
            raise urllib.error.HTTPError(url, 401, "Unauthorized", {}, io.BytesIO())  # type: ignore[arg-type]

        results = self.lists(fetch=denied)
        self.assertTrue(all(r.failed for r in results))
        self.assertIn("download failed (HTTP 401); there is no cached list", self.warnings[0])
        self.assertFalse((self.root / "catalog" / "ra").exists())

    def test_the_key_is_in_the_request_and_nowhere_else(self) -> None:
        def broken(url: str) -> bytes:
            self.urls.append(url)
            raise ValueError(f"cannot parse the answer to {url}")

        self.lists(fetch=self.fetch)
        self.lists(force=True, fetch=broken)
        self.assertTrue(all(SECRET in u for u in self.urls))
        shown = "\n".join([*self.lines, *self.warnings])
        self.assertNotIn(SECRET, shown)
        for path in (self.root / "catalog").rglob("*"):
            if path.is_file():
                self.assertNotIn(SECRET.encode(), path.read_bytes())

    def test_a_cache_that_is_not_a_list_is_replaced(self) -> None:
        bad = self.root / "catalog" / "ra" / "12.json"
        bad.parent.mkdir(parents=True)
        bad.write_text("not json")
        self.lists()
        self.assertEqual(self.cached(12)["format"], 1)

    def test_a_dry_run_downloads_and_writes_nothing(self) -> None:
        results = self.lists(dry_run=True)
        self.assertEqual({r.status for r in results}, {"would-fetch"})
        self.assertEqual(self.urls, [])
        self.assertFalse((self.root / "catalog").exists())
        self.assertIn("RA list 12 (psx): would download", self.lines)

    def test_no_key_is_a_usage_error(self) -> None:
        with self.assertRaises(CatalogError):
            self.lists(key=None)

    def test_a_root_without_a_library_is_a_usage_error(self) -> None:
        with tempfile.TemporaryDirectory() as empty, self.assertRaises(CatalogError):
            catalog_store.run_ra_lists(
                Path(empty), key=SECRET, force=False, dry_run=False, out=print, warn=print
            )


class RaCliTests(StoreCase):
    def setUp(self) -> None:
        super().setUp()
        self.none = str(self.root / "no-config.toml")
        self.bin = self.root / "bin"
        env = mock.patch.dict(os.environ)
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop(ra.KEY_ENV, None)

    def catalog(self, *args: str) -> tuple[int, str, str]:
        return run("catalog", "--config", self.none, "--root", str(self.root), *args)

    def test_ra_hash_runs_the_rahasher_given_on_the_command_line(self) -> None:
        self.put("roms/psp/Game.iso", b"iso bytes")
        stub = make_stub(self.bin, "RAHasher", RAHASHER)
        code, out, err = self.catalog("--ra-hash", "--rahasher", stub)
        self.assertEqual((code, err), (0, ""), out)
        self.assertIn("psp: 1 game(s), written (+1 -0 ~0); RA: 1 hashed", out)
        game = next(iter(self.system_file("psp")["games"].values()))
        self.assertEqual(game["ra"]["hash"], md5_of(b"iso bytes"))

    def test_ra_hash_takes_the_rahasher_of_the_inbox_table(self) -> None:
        self.put("roms/psp/Game.iso", b"iso bytes")
        stub = make_stub(self.bin, "RAHasher", RAHASHER)
        config = self.root / "config.toml"
        config.write_text(f'[inbox]\nrahasher = "{stub}"\n')
        code, out, err = run(
            "catalog", "--config", str(config), "--root", str(self.root), "--ra-hash"
        )
        self.assertEqual((code, err), (0, ""), out)
        self.assertIn("RA: 1 hashed", out)

    def test_ra_hash_without_a_rahasher_or_with_a_bad_one_is_exit_2(self) -> None:
        self.put("roms/psp/Game.iso", b"iso bytes")
        code, _, err = self.catalog("--ra-hash")
        self.assertEqual(code, 2)
        self.assertIn("no RAHasher", err)
        code, _, err = self.catalog("--ra-hash", "--rahasher", str(self.root / "nope"))
        self.assertEqual(code, 2)
        self.assertIn("not an executable file", err)
        self.assertFalse((self.root / "catalog").exists())

    def test_the_options_that_need_another_are_refused(self) -> None:
        self.put("roms/psp/Game.iso", b"iso bytes")
        for args, text in ((("--rahasher", "x"), "--ra-hash"), (("--force",), "--ra-lists")):
            code, _, err = self.catalog(*args)
            self.assertEqual(code, 2)
            self.assertIn(text, err)

    def test_ra_lists_without_a_key_is_exit_2_before_anything_is_scanned(self) -> None:
        self.put("roms/psp/Game.iso", b"iso bytes")
        code, _, err = self.catalog("--ra-lists")
        self.assertEqual(code, 2)
        self.assertIn(ra.KEY_ENV, err)
        self.assertFalse((self.root / "catalog").exists())

    def test_ra_lists_uses_the_key_of_the_environment_and_never_prints_it(self) -> None:
        self.put("roms/psp/Game.iso", b"iso bytes")
        os.environ[ra.KEY_ENV] = SECRET
        seen: list[str] = []
        with mock.patch.object(ra, "http_fetch", lambda url: seen.append(url) or LIST_12):
            code, out, err = self.catalog("--ra-lists")
        self.assertEqual((code, err), (0, ""), out)
        self.assertEqual(len(seen), 1)
        self.assertIn("i=41&h=1&y=" + SECRET, seen[0])
        self.assertNotIn(SECRET, out)
        self.assertIn("RA list 41 (psp): fetched, 1 game(s)", out)
        self.assertTrue((self.root / "catalog" / "ra" / "41.json").is_file())

    def test_a_failed_download_is_exit_1_only_when_there_is_no_cached_list(self) -> None:
        self.put("roms/psp/Game.iso", b"iso bytes")
        os.environ[ra.KEY_ENV] = SECRET

        def down(url: str) -> bytes:
            raise urllib.error.URLError(f"cannot reach {url}")

        with mock.patch.object(ra, "http_fetch", down):
            code, out, err = self.catalog("--ra-lists")
        self.assertEqual(code, 1)
        self.assertIn("download failed (URLError)", err)
        self.assertNotIn(SECRET, out + err)
        with mock.patch.object(ra, "http_fetch", lambda url: LIST_12):
            self.assertEqual(self.catalog("--ra-lists")[0], 0)
        with mock.patch.object(ra, "http_fetch", down):
            code, out, err = self.catalog("--ra-lists", "--force")
        self.assertEqual(code, 0)
        self.assertIn("cached list kept", out)
        self.assertNotIn(SECRET, out + err)

    def test_a_dry_run_with_both_options_changes_nothing(self) -> None:
        self.put("roms/psp/Game.iso", b"iso bytes")
        os.environ[ra.KEY_ENV] = SECRET
        stub = make_stub(self.bin, "RAHasher", RAHASHER)
        with mock.patch.object(ra, "http_fetch", lambda url: self.fail("no download")):
            code, out, _ = self.catalog("--dry-run", "--ra-hash", "--rahasher", stub, "--ra-lists")
        self.assertEqual(code, 0)
        self.assertIn("psp: 1 game(s), would write (+1 -0 ~0); 1 game(s) to RA-hash", out)
        self.assertIn("RA list 41 (psp): would download", out)
        self.assertFalse((self.root / "catalog").exists())


if __name__ == "__main__":
    unittest.main()
