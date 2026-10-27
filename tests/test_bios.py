import hashlib
import tempfile
import unittest
import zipfile
import zlib
from pathlib import Path
from typing import Any, ClassVar
from unittest import mock

from support import make_config
from test_cli import run
from warpzone import bios
from warpzone.bios import (
    BiosError,
    BiosFile,
    FileCheck,
    Item,
    ItemResult,
    Library,
    Overrides,
    Report,
    Status,
    SystemReport,
    check_library,
    default_root,
    judge_file,
    judge_zip,
    load_manifest,
    load_overrides,
    needs_hash,
    parse_manifest,
    parse_overrides,
    render,
    settle,
    unknown_files,
)
from warpzone.profiles import load_profile

# Fake contents: the tests never need a real BIOS hash.
GOOD, OTHER = b"good-bios-bytes", b"other-bios-byte"  # same length
assert len(GOOD) == len(OTHER)


def md5(data: bytes) -> str:
    return hashlib.md5(data).hexdigest()


def crc(data: bytes) -> str:
    return f"{zlib.crc32(data):08x}"


def file(
    path: str = "a.bin", data: bytes = GOOD, *hashes: str, size: int | None = None
) -> BiosFile:
    return BiosFile(path, len(data) if size is None else size, frozenset(hashes or [md5(data)]))


def fake_manifest() -> dict[str, Any]:
    return {
        "skip_dirs": ["keys"],
        "con": {
            "title": "Console",
            "item": [
                {
                    "name": "bios",
                    "note": "boots games",
                    "files": [
                        {"path": "a.bin", "size": len(GOOD), "md5": md5(GOOD)},
                        {"path": "sub/b.bin", "size": len(GOOD), "md5": md5(OTHER)},
                    ],
                },
                {
                    "name": "named by content",
                    "any_name": True,
                    "files": [{"path": "c.bin", "size": len(GOOD), "md5": md5(OTHER)}],
                },
                {
                    "name": "firmware",
                    "required": False,
                    "files": [{"path": "fw.bin", "size": 8}],
                },
            ],
        },
        "arc": {
            "title": "Arcade",
            "item": [
                {
                    "name": "set.zip",
                    "base": "roms",
                    "zip": "mame/set.zip",
                    "members": {"one.rom": crc(b"1"), "two.rom": crc(b"2")},
                }
            ],
        },
    }


class JudgeFileTests(unittest.TestCase):
    def test_missing_when_there_is_no_size(self) -> None:
        self.assertEqual(judge_file(file(), frozenset(), None, None)[0], Status.MISSING)

    def test_wrong_size_is_wrong_without_a_hash(self) -> None:
        status, detail = judge_file(file(), frozenset(), 3, None)
        self.assertEqual(status, Status.WRONG)
        self.assertIn("size 3, expected", detail)

    def test_accepted_hash_is_ok(self) -> None:
        self.assertEqual(judge_file(file(), frozenset(), len(GOOD), md5(GOOD))[0], Status.OK)

    def test_any_of_several_hashes(self) -> None:
        f = file("a.bin", GOOD, md5(GOOD), md5(OTHER))
        self.assertEqual(judge_file(f, frozenset(), len(GOOD), md5(OTHER))[0], Status.OK)

    def test_other_hash_is_wrong_and_names_both(self) -> None:
        status, detail = judge_file(file(), frozenset(), len(GOOD), md5(OTHER))
        self.assertEqual(status, Status.WRONG)
        self.assertIn(md5(OTHER), detail)
        self.assertIn(md5(GOOD), detail)

    def test_local_hash_is_accepted_local(self) -> None:
        status, _ = judge_file(file(), frozenset({md5(OTHER)}), len(GOOD), md5(OTHER))
        self.assertEqual(status, Status.LOCAL)

    def test_local_hash_does_not_override_the_size(self) -> None:
        status, _ = judge_file(file(), frozenset({md5(OTHER)}), 3, None)
        self.assertEqual(status, Status.WRONG)

    def test_size_only_entry(self) -> None:
        f = BiosFile("fw.bin", 8, frozenset())
        self.assertEqual(judge_file(f, frozenset(), 8, None)[0], Status.OK)
        self.assertEqual(judge_file(f, frozenset(), 9, None)[0], Status.WRONG)

    def test_needs_hash_only_for_the_right_size_and_a_known_hash(self) -> None:
        f = file()
        self.assertTrue(needs_hash(f, len(GOOD)))
        self.assertFalse(needs_hash(f, len(GOOD) + 1))
        self.assertFalse(needs_hash(f, None))
        self.assertFalse(needs_hash(BiosFile("fw.bin", 8, frozenset()), 8))


class JudgeZipTests(unittest.TestCase):
    MEMBERS: ClassVar[dict[str, str]] = {"one.rom": "11111111", "two.rom": "22222222"}

    def test_absent_zip_is_missing(self) -> None:
        self.assertEqual(judge_zip(self.MEMBERS, None)[0], Status.MISSING)

    def test_all_members_with_the_right_crc_is_ok_and_extras_are_ignored(self) -> None:
        crcs = {**self.MEMBERS, "extra.rom": "33333333"}
        self.assertEqual(judge_zip(self.MEMBERS, crcs)[0], Status.OK)

    def test_missing_and_different_members_are_named(self) -> None:
        status, detail = judge_zip(self.MEMBERS, {"one.rom": "deadbeef"})
        self.assertEqual(status, Status.WRONG)
        self.assertIn("two.rom", detail)
        self.assertIn("one.rom", detail)


class SettleTests(unittest.TestCase):
    def checks(self, *statuses: Status) -> list[FileCheck]:
        return [FileCheck("x", s) for s in statuses]

    def test_best_file_wins(self) -> None:
        self.assertIs(settle(self.checks(Status.MISSING, Status.WRONG, Status.OK)), Status.OK)
        self.assertIs(settle(self.checks(Status.WRONG, Status.LOCAL)), Status.LOCAL)
        self.assertIs(settle(self.checks(Status.MISSING, Status.WRONG)), Status.WRONG)
        self.assertIs(settle(self.checks(Status.MISSING)), Status.MISSING)

    def test_only_required_items_block(self) -> None:
        def result(required: bool, status: Status) -> ItemResult:
            return ItemResult(Item("s", "n", "bios", required, False, "", ()), status)

        self.assertTrue(result(True, Status.MISSING).blocking)
        self.assertTrue(result(True, Status.WRONG).blocking)
        self.assertFalse(result(True, Status.LOCAL).blocking)
        self.assertFalse(result(True, Status.SKIPPED).blocking)
        self.assertFalse(result(False, Status.MISSING).blocking)
        self.assertFalse(result(False, Status.WRONG).blocking)


class UnknownFilesTests(unittest.TestCase):
    def test_sorted_difference(self) -> None:
        self.assertEqual(unknown_files(["z", "a", "k"], {"k"}), ("a", "z"))


class DefaultRootTests(unittest.TestCase):
    def test_device_library_comes_before_the_hub(self) -> None:
        cfg = make_config()
        profiles = {"steamos-emudeck": load_profile("steamos-emudeck")}
        home = Path("/home/u")
        everything = {home / "Emulation" / "bios", Path("/data/games/library/bios")}
        self.assertEqual(
            default_root(cfg, profiles, home, lambda p: p in everything), home / "Emulation"
        )

    def test_hub_library_when_not_on_a_device(self) -> None:
        cfg = make_config()
        profiles = {"steamos-emudeck": load_profile("steamos-emudeck")}
        found = default_root(
            cfg, profiles, Path("/home/u"), lambda p: p == Path("/data/games/library/roms")
        )
        self.assertEqual(found, Path("/data/games/library"))

    def test_none_when_there_is_no_library(self) -> None:
        cfg = make_config()
        profiles = {"steamos-emudeck": load_profile("steamos-emudeck")}
        self.assertIsNone(default_root(cfg, profiles, Path("/home/u"), lambda p: False))


class ParseManifestTests(unittest.TestCase):
    def test_test_manifest_parses(self) -> None:
        manifest = parse_manifest(fake_manifest())
        self.assertEqual(list(manifest.systems), ["con", "arc"])
        self.assertEqual(manifest.skip_dirs, ("keys",))
        self.assertFalse(manifest.systems["con"].items[2].required)
        self.assertEqual(manifest.systems["arc"].items[0].zip_path, "mame/set.zip")

    def rejects(self, mutate: Any, text: str) -> None:
        data = fake_manifest()
        mutate(data)
        with self.assertRaises(BiosError) as caught:
            parse_manifest(data)
        self.assertIn(text, str(caught.exception))

    def test_validation(self) -> None:
        item = lambda d: d["con"]["item"][0]  # noqa: E731
        self.rejects(lambda d: item(d)["files"][0].update(md5="xyz"), "md5 must be 32 hex")
        self.rejects(lambda d: item(d)["files"][0].update(size=0), "positive integer")
        self.rejects(lambda d: item(d)["files"][0].update(path="../x"), "bad path")
        self.rejects(lambda d: item(d).update(base="elsewhere"), "base")
        self.rejects(lambda d: item(d).update(bogus=1), "unknown key 'bogus'")
        self.rejects(lambda d: item(d).update(zip="a.zip"), "exactly one of")
        self.rejects(lambda d: d["con"]["item"].append(dict(item(d))), "two items are named")
        self.rejects(lambda d: d["arc"]["item"][0].update(members={"x": "zz"}), "not a crc32")
        self.rejects(lambda d: d["con"].update(title=""), "title")

    def test_shipped_manifest_is_valid_and_has_the_expected_systems(self) -> None:
        manifest = load_manifest()
        self.assertTrue(
            {"psx", "ps2", "saturn", "dreamcast", "nds", "neogeo", "atomiswave", "naomi"}
            <= set(manifest.systems)
        )
        zips = {
            i.zip_path for s in manifest.systems.values() for i in s.items if i.zip_path is not None
        }
        self.assertTrue(
            {f"mame/{n}.zip" for n in ("neogeo", "awbios", "naomi", "bubsys", "ym2413")} <= zips
        )
        self.assertIn("eden", manifest.skip_dirs)


class ParseOverridesTests(unittest.TestCase):
    def setUp(self) -> None:
        self.manifest = parse_manifest(fake_manifest())

    def parse(self, data: dict[str, Any]) -> Overrides:
        return parse_overrides(data, self.manifest, "bios.toml")

    def test_accept_and_unwanted(self) -> None:
        ov = self.parse({"con": {"accept": {"a.bin": [md5(OTHER)]}, "unwanted": ["firmware"]}})
        self.assertEqual(ov.accept[("con", "a.bin")], frozenset({md5(OTHER)}))
        self.assertEqual(ov.unwanted, frozenset({("con", "firmware")}))

    def test_a_single_hash_string_is_enough(self) -> None:
        ov = self.parse({"con": {"accept": {"a.bin": md5(OTHER).upper()}}})
        self.assertEqual(ov.accept[("con", "a.bin")], frozenset({md5(OTHER)}))

    def test_typos_are_errors_not_silence(self) -> None:
        for data, text in (
            ({"nope": {}}, "unknown system"),
            ({"con": {"accept": {"zzz.bin": [md5(OTHER)]}}}, "not a file of the con manifest"),
            ({"con": {"accept": {"a.bin": ["abc"]}}}, "md5 hashes"),
            ({"con": {"unwanted": ["nope"]}}, "no item named"),
            ({"con": {"extra": 1}}, "unknown key"),
        ):
            with self.subTest(text), self.assertRaises(BiosError) as caught:
                self.parse(data)
            self.assertIn(text, str(caught.exception))

    def test_load_overrides_file_handling(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "bios.toml"
            self.assertEqual(load_overrides(path, self.manifest, must_exist=False), Overrides())
            with self.assertRaises(BiosError):
                load_overrides(path, self.manifest, must_exist=True)
            path.write_text("this is = not [valid")
            with self.assertRaises(BiosError):
                load_overrides(path, self.manifest, must_exist=False)
            path.write_text(f'[con.accept]\n"a.bin" = ["{md5(OTHER)}"]\n')
            ov = load_overrides(path, self.manifest, must_exist=False)
            self.assertIn(("con", "a.bin"), ov.accept)

    def test_the_shipped_example_is_valid_against_the_shipped_manifest(self) -> None:
        example = Path(__file__).resolve().parent.parent / "examples" / "bios.example.toml"
        ov = load_overrides(example, load_manifest(), must_exist=True)
        self.assertIn(("saturn", "sega_101.bin"), ov.accept)
        self.assertIn(("nds", "firmware"), ov.unwanted)

    def test_default_path_is_next_to_the_config(self) -> None:
        self.assertEqual(
            bios.default_overrides_path(Path("/etc/wz/config.toml")), Path("/etc/wz/bios.toml")
        )


class RenderTests(unittest.TestCase):
    def report(self) -> Report:
        manifest = parse_manifest(fake_manifest())
        con = manifest.systems["con"].items
        results = (
            ItemResult(con[0], Status.MISSING, (FileCheck("a.bin", Status.MISSING),)),
            ItemResult(
                con[1],
                Status.OK,
                (FileCheck("c.bin", Status.OK, found="renamed.bin"),),
            ),
            ItemResult(
                con[2], Status.WRONG, (FileCheck("fw.bin", Status.WRONG, "size 9, expected 8"),)
            ),
        )
        return Report((SystemReport("con", "Console", results),), ("x.txt", "y.txt"))

    def test_lines(self) -> None:
        text = "\n".join(render(self.report()))
        self.assertIn("missing (required)", text)
        self.assertIn("unlocks: boots games", text)
        self.assertIn("renamed.bin (matches c.bin)", text)
        self.assertIn("wrong (optional)", text)
        self.assertIn("unknown files in bios/: 2", text)
        self.assertNotIn("x.txt", text)
        self.assertIn("1 required item(s) missing or wrong", text)

    def test_all_lists_the_unknown_files(self) -> None:
        text = "\n".join(render(self.report(), show_all=True))
        self.assertIn("  x.txt", text)
        self.assertNotIn("--all", text)


class FilesystemTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.manifest = parse_manifest(fake_manifest())

    def put(self, rel: str, data: bytes) -> None:
        path = self.root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)

    def put_zip(self, rel: str, members: dict[str, bytes]) -> None:
        path = self.root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(path, "w") as archive:
            for name, data in members.items():
                archive.writestr(name, data)

    def check(
        self, overrides: Overrides | None = None, systems: list[str] | None = None
    ) -> tuple[Report, Library]:
        lib = Library(self.root, self.manifest.skip_dirs)
        return check_library(self.manifest, overrides or Overrides(), lib, systems), lib

    def results(self, report: Report, system: str = "con") -> dict[str, ItemResult]:
        found = next(s for s in report.systems if s.key == system)
        return {r.item.name: r for r in found.results}

    def test_empty_bios_folder_misses_everything_required(self) -> None:
        (self.root / "bios").mkdir()
        report, _ = self.check()
        self.assertEqual(
            [r.item.name for r in report.problems], ["bios", "named by content", "set.zip"]
        )
        self.assertEqual(self.results(report)["firmware"].status, Status.MISSING)

    def test_ok_wrong_and_missing(self) -> None:
        self.put("bios/a.bin", GOOD)
        self.put("bios/sub/b.bin", GOOD)  # right size, not the md5 of b.bin
        report, _ = self.check(systems=["con"])
        bios_item = self.results(report)["bios"]
        self.assertEqual(bios_item.status, Status.OK)  # a.bin is enough
        self.assertEqual([c.status for c in bios_item.checks], [Status.OK, Status.WRONG])
        self.assertIsNone(report.unknown)  # not computed for a subset

    def test_hashing_only_files_of_the_expected_size(self) -> None:
        self.put("bios/a.bin", GOOD + b"!")  # wrong size
        self.put("bios/sub/b.bin", OTHER)  # right size and md5
        with mock.patch.object(Library, "md5", autospec=True, side_effect=Library.md5) as spy:
            report, _ = self.check(systems=["con"])
        hashed = [call.args[2] for call in spy.call_args_list]
        self.assertNotIn("a.bin", hashed)
        self.assertIn("sub/b.bin", hashed)
        a_check = self.results(report)["bios"].checks[0]
        self.assertEqual(a_check.status, Status.WRONG)
        self.assertIn("size", a_check.detail)

    def test_override_accepts_a_modified_file_as_local(self) -> None:
        self.put("bios/a.bin", OTHER)
        plain, _ = self.check(systems=["con"])
        self.assertEqual(self.results(plain)["bios"].status, Status.WRONG)
        overrides = Overrides({("con", "a.bin"): frozenset({md5(OTHER)})})
        report, _ = self.check(overrides, systems=["con"])
        item = self.results(report)["bios"]
        self.assertEqual(item.status, Status.LOCAL)
        self.assertFalse(item.blocking)

    def test_unwanted_items_are_skipped(self) -> None:
        overrides = Overrides(unwanted=frozenset({("con", "bios")}))
        report, _ = self.check(overrides, systems=["con"])
        self.assertEqual(self.results(report)["bios"].status, Status.SKIPPED)
        self.assertNotIn("bios", [r.item.name for r in report.problems])

    def test_any_name_finds_a_renamed_file_by_content(self) -> None:
        self.put("bios/Whatever.BIN", OTHER)
        report, _ = self.check()
        item = self.results(report)["named by content"]
        self.assertEqual(item.status, Status.OK)
        self.assertEqual(item.checks[-1].found, "Whatever.BIN")
        self.assertNotIn("Whatever.BIN", report.unknown or ())

    def test_any_name_ignores_files_of_another_size_or_folder(self) -> None:
        self.put("bios/other.bin", OTHER + b"!")
        self.put("bios/sub/other.bin", OTHER)
        report, _ = self.check()
        self.assertEqual(self.results(report)["named by content"].status, Status.MISSING)

    def test_item_without_any_name_does_not_search(self) -> None:
        self.put("bios/Renamed.bin", GOOD)
        report, _ = self.check(systems=["con"])
        self.assertEqual(self.results(report)["bios"].status, Status.MISSING)

    def test_zip_members_are_checked_by_name_and_crc(self) -> None:
        self.put_zip("roms/mame/set.zip", {"one.rom": b"1", "two.rom": b"2", "more.rom": b"3"})
        report, _ = self.check(systems=["arc"])
        self.assertEqual(self.results(report, "arc")["set.zip"].status, Status.OK)
        self.put_zip("roms/mame/set.zip", {"one.rom": b"1", "two.rom": b"changed"})
        report, _ = self.check(systems=["arc"])
        self.assertEqual(self.results(report, "arc")["set.zip"].status, Status.WRONG)
        self.put("roms/mame/set.zip", b"not a zip")
        report, _ = self.check(systems=["arc"])
        item = self.results(report, "arc")["set.zip"]
        self.assertEqual(item.status, Status.WRONG)
        self.assertIn("zip", item.checks[0].detail)

    def test_skipped_folders_are_neither_listed_nor_opened(self) -> None:
        self.put("bios/keys/prod.keys", b"secret")
        self.put("bios/notes.txt", b"hi")
        with mock.patch.object(Library, "md5", autospec=True) as spy:
            report, lib = self.check()
        self.assertEqual(report.unknown, ("notes.txt",))
        self.assertNotIn("keys/prod.keys", lib.bios_index())
        for call in spy.call_args_list:
            self.assertNotIn("keys", call.args[2])

    def test_unknown_files_are_reported_when_every_system_is_checked(self) -> None:
        self.put("bios/a.bin", GOOD)
        self.put("bios/sub/readme.txt", b"hi")
        report, _ = self.check()
        self.assertEqual(report.unknown, ("sub/readme.txt",))

    def test_unreadable_file_is_wrong_not_a_crash(self) -> None:
        self.put("bios/a.bin", GOOD)
        with mock.patch.object(Library, "md5", side_effect=PermissionError(13, "denied")):
            report, _ = self.check(systems=["con"])
        item = self.results(report)["bios"]
        self.assertEqual(item.checks[0].status, Status.WRONG)
        self.assertIn("denied", item.checks[0].detail)

    def test_unknown_system_name(self) -> None:
        with self.assertRaises(BiosError):
            self.check(systems=["nope"])


class CliTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        self.config = str(self.tmp / "none.toml")

    def test_exit_1_and_report_when_required_files_are_missing(self) -> None:
        (self.tmp / "lib" / "bios").mkdir(parents=True)
        code, out, _ = run("bios", "--root", str(self.tmp / "lib"), "--config", self.config)
        self.assertEqual(code, 1)
        self.assertIn("missing (required)", out)
        self.assertIn("psx: Sony PlayStation", out)

    def test_exit_0_when_nothing_required_is_missing(self) -> None:
        (self.tmp / "lib" / "bios").mkdir(parents=True)
        code, out, _ = run(
            "bios",
            "--root",
            str(self.tmp / "lib"),
            "--system",
            "gba",
            "--config",
            self.config,
        )
        self.assertEqual(code, 0)
        self.assertIn("missing (optional)", out)
        self.assertNotIn("psx:", out)

    def test_system_accepts_several_names_and_repeats(self) -> None:
        (self.tmp / "lib" / "bios").mkdir(parents=True)
        root = str(self.tmp / "lib")
        args = ("bios", "--root", root, "--system", "gba", "lynx", "--system", "nds")
        code, out, _ = run(*args, "--config", self.config)
        self.assertEqual(code, 1)  # lynx boot rom is required
        for key in ("gba:", "lynx:", "nds:"):
            self.assertIn(key, out)

    def test_overrides_file_is_read(self) -> None:
        lib = self.tmp / "lib"
        (lib / "bios").mkdir(parents=True)
        blank = b"\0" * 512
        (lib / "bios" / "lynxboot.img").write_bytes(blank)
        manifest = load_manifest()
        lynx = manifest.systems["lynx"].items[0].files[0]
        self.assertEqual(lynx.size, 512)
        args = ("bios", "--root", str(lib), "--system", "lynx", "--config", self.config)
        code, out, _ = run(*args)
        self.assertEqual(code, 1)
        self.assertIn("wrong (required)", out)
        self.assertIn("[<system>.accept]", out)
        overrides = self.tmp / "bios.toml"
        overrides.write_text(f'[lynx.accept]\n"lynxboot.img" = ["{md5(blank)}"]\n')
        code, out, _ = run(*args, "--overrides", str(overrides))
        self.assertEqual(code, 0)
        self.assertIn("accepted (local)", out)
        # The default location is next to the config file.
        code, _, _ = run(*args[:-2], "--config", str(self.tmp / "config.toml"))
        self.assertEqual(code, 0)

    def test_usage_errors_are_exit_2(self) -> None:
        lib = self.tmp / "lib"
        lib.mkdir()
        code, _, err = run("bios", "--root", str(lib), "--config", self.config)
        self.assertEqual(code, 2)
        self.assertIn("no bios/ or roms/", err)
        (lib / "bios").mkdir()
        code, _, err = run("bios", "--root", str(lib), "--system", "nope", "--config", self.config)
        self.assertEqual((code, "unknown system" in err), (2, True))
        code, _, err = run("bios", "--root", str(lib), "--overrides", str(self.tmp / "x.toml"))
        self.assertEqual((code, "not found" in err), (2, True))

    def test_without_root_the_config_is_needed(self) -> None:
        code, _, err = run("bios", "--config", self.config)
        self.assertEqual(code, 2)
        self.assertIn("--root", err)


if __name__ == "__main__":
    unittest.main()
