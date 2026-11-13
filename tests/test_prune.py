import json
import os
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

from notify_support import Receiver
from support import (
    API_KEY,
    DECK_ID,
    DECK_LIBRARY_IGNORES,
    FakeSyncthing,
    config_data,
    make_config,
)
from test_cli import run, write_config
from warpzone.config import parse_config
from warpzone.errors import UsageError
from warpzone.notify import NotifyError, notify
from warpzone.probes import DeviceProbes
from warpzone.profiles import load_profile
from warpzone.prune import (
    Entry,
    FolderReport,
    PruneError,
    Verdict,
    apply_folder,
    classify,
    guard,
    guard_state_path,
    is_generated,
    parse_guard_state,
    parse_time,
    prune_device,
    render_guard_state,
    rescan,
)
from warpzone.report import Reporter
from warpzone.syncthing import Client

PROFILE = load_profile("steamos-emudeck")
FILE, DIR, LINK = "FILE_INFO_TYPE_FILE", "FILE_INFO_TYPE_DIRECTORY", "FILE_INFO_TYPE_SYMLINK"
MTIME = 1_700_000_000
SYNCED_AT = "2023-11-14T22:13:20.123456789+00:00"  # == MTIME


def hub_copy(size: int = 5, kind: str = FILE, modified: str = SYNCED_AT) -> dict[str, Any]:
    return {"type": kind, "size": size, "modified": modified, "deleted": False, "ignored": False}


def ignored_info(hub: dict[str, Any] | None) -> dict[str, Any]:
    """What db/file says once the whitelist stops matching a file the hub still has."""
    return {"global": hub, "local": {"ignored": True, "invalid": True, "size": 0}}


def wanted_info(size: int) -> dict[str, Any]:
    return {"global": hub_copy(size), "local": {"ignored": False}}


def entry(size: int = 5, kind: str = "file", mtime: float = MTIME) -> Entry:
    return Entry("roms/n64/a.z64", kind, size, mtime)


class ParseTimeTests(unittest.TestCase):
    def test_nanoseconds_and_zone(self) -> None:
        self.assertEqual(parse_time(SYNCED_AT), MTIME + 0.123456)
        self.assertEqual(parse_time("2023-11-14T23:13:20+01:00"), MTIME)
        self.assertEqual(parse_time("2023-11-14T22:13:20Z"), MTIME)

    def test_garbage_is_none(self) -> None:
        for value in (None, "", "yesterday", 5):
            self.assertIsNone(parse_time(value))


class ClassifyTests(unittest.TestCase):
    def verdict(self, info: dict[str, Any] | None, **kw: Any) -> Verdict:
        return classify(entry(**kw), info)[0]

    def test_no_record_is_untracked(self) -> None:
        self.assertIs(self.verdict(None), Verdict.UNTRACKED)

    def test_synced_file_is_wanted(self) -> None:
        info = {"global": hub_copy(), "local": {"ignored": False, "size": 5}}
        self.assertIs(self.verdict(info), Verdict.WANTED)
        self.assertIs(self.verdict({"global": hub_copy(), "local": None}), Verdict.WANTED)

    def test_ignored_with_hub_copy_is_leftover(self) -> None:
        self.assertIs(self.verdict(ignored_info(hub_copy())), Verdict.LEFTOVER)

    def test_mtime_tolerance_for_coarse_filesystems(self) -> None:
        self.assertIs(self.verdict(ignored_info(hub_copy()), mtime=MTIME + 1.5), Verdict.LEFTOVER)
        self.assertIs(self.verdict(ignored_info(hub_copy()), mtime=MTIME + 3), Verdict.DIFFERS)

    def test_hub_lacking_the_file_keeps_it(self) -> None:
        for hub in (
            None,
            {**hub_copy(), "deleted": True},
            # A local-only file scanned before it was ignored is mirrored into `global`.
            {**hub_copy(), "invalid": True},
            {**hub_copy(), "ignored": True},
        ):
            self.assertIs(self.verdict(ignored_info(hub)), Verdict.LOCAL_ONLY, hub)

    def test_modified_copy_is_kept(self) -> None:
        self.assertIs(self.verdict(ignored_info(hub_copy()), size=6), Verdict.DIFFERS)
        stale = hub_copy(modified="2020-01-01T00:00:00Z")
        self.assertIs(self.verdict(ignored_info(stale)), Verdict.DIFFERS)
        self.assertIs(self.verdict(ignored_info(hub_copy(modified="?"))), Verdict.DIFFERS)

    def test_kind_mismatch_is_kept(self) -> None:
        self.assertIs(self.verdict(ignored_info(hub_copy(kind=DIR))), Verdict.DIFFERS)
        self.assertIs(self.verdict(ignored_info(hub_copy()), kind="symlink"), Verdict.DIFFERS)

    def test_symlink_has_no_size_or_mtime_to_compare(self) -> None:
        info = ignored_info(hub_copy(size=99, kind=LINK, modified="?"))
        self.assertIs(self.verdict(info, kind="symlink"), Verdict.LEFTOVER)


class GeneratedTests(unittest.TestCase):
    def test_what_emudeck_and_emulators_write_is_recognised(self) -> None:
        for path in (
            "roms/psx/metadata.txt",
            "roms/psx/systeminfo.txt",
            "bios/readme.txt",
            "bios/PPSSPP/ppge_atlas.zim",
            "bios/PPSSPP/assets/lang/es_ES.ini",
            "bios/mame/readme.txt",
            "bios/dc/readme.txt",
            "bios/dc/vmu_save_A1.bin",
            "bios/dc/vmu_save_B2.bin",
            "bios/dc/dc_nvmem.bin",
            "bios/dc/vulkan_pipeline.cache",
            "bios/dc/data/sub/file.bin",
            "bios/HdPacks/pack/a.png",
            "bios/Mupen64plus/mupen64plus.cfg",
            "bios/fbneo/hiscore.dat",
            "bios/kronos/a",
            "bios/same_cdi/a",
            "bios/ryujinx/a",
            "bios/shadps4/a",
            "bios/yuzu/a",
            "bios/mame2003-plus/hiscore.dat",
            "bios/mame2003-plus/artwork/x/y.png",
        ):
            with self.subTest(path=path):
                self.assertTrue(is_generated(path))

    def test_everything_else_stays_unusual(self) -> None:
        for path in (
            "roms/psx/mine.txt",
            "roms/psx/sub/metadata.txt",
            "roms/metadata.txt",
            "metadata.txt",
            "roms/psx/Metadata.txt",
            "bios/scph5501.bin",
            "bios/dc/dc_boot.bin",
            "bios/dc/vmu_save_A1.sav",
            "bios/dc/my_vmu_save_A1.bin",
            "bios/dc/sub/vmu_save_A1.bin",
            "bios/mame2003-plus/samples/a.wav",
            "bios/mame2003-plus/artwork",
            "bios/PPSSPP",
            "bios/HdPacks",
            "bios/PPSSPPx/a",
            "bios/other/readme.md",
            "saves/psx/vmu_save_A1.bin",
            "media/psx/readme.txt",
        ):
            with self.subTest(path=path):
                self.assertFalse(is_generated(path))


class Device:
    """A temp home with a fake local Syncthing configured like a provisioned deck."""

    def __init__(self, test: unittest.TestCase) -> None:
        tmp = tempfile.TemporaryDirectory()
        test.addCleanup(tmp.cleanup)
        self.home = Path(tmp.name)
        self.root = self.home / "Emulation"
        (self.root / ".stfolder").mkdir(parents=True)
        self.fake = FakeSyncthing(DECK_ID)
        self.fake.__enter__()
        test.addCleanup(self.fake.__exit__)
        self.fake.folders["wz-library"] = {
            "id": "wz-library",
            "path": str(self.root),
            "type": "receiveonly",
            "paused": False,
        }
        self.fake.ignores["wz-library"] = list(DECK_LIBRARY_IGNORES)
        for name in ("patterns-common.stignore", "patterns-deck.stignore"):
            self.fake.file_info[("wz-library", name)] = {
                "global": {"version": ["X:1"]},
                "local": {"version": ["X:1"]},
            }
            (self.root / name).write_text("x")
        self.cfg = make_config(self.fake.url)
        self.client = Client(self.fake.url, API_KEY)
        self.lines: list[str] = []
        self.warnings: list[str] = []
        self.rep = Reporter(self.lines.append, warn_sink=self.warnings.append)

    def add_songs(self) -> Path:
        """Make the deck also subscribe to the whitelisted songs folder."""
        songs = self.home / "Songs"
        (songs / ".stfolder").mkdir(parents=True)
        self.fake.folders["wz-songs"] = {
            "id": "wz-songs",
            "path": str(songs),
            "type": "receiveonly",
            "paused": False,
        }
        self.fake.ignores["wz-songs"] = ["#include patterns-deck.stignore"]
        for name in ("patterns-common.stignore", "patterns-deck.stignore"):
            self.fake.file_info[("wz-songs", name)] = {
                "global": {"version": ["X:1"]},
                "local": {"version": ["X:1"]},
            }
            (songs / name).write_text("x")
        data = config_data(self.fake.url)
        data["devices"]["deck"]["folders"] = ["library", "songs"]
        self.cfg = parse_config(data)
        return songs

    def put_song(self, rel: str, content: str = "12345", leftover: bool = True) -> Path:
        path = self.home / "Songs" / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
        os.utime(path, (MTIME, MTIME))
        if leftover:
            info: dict[str, Any] = ignored_info(hub_copy(len(content)))
        else:
            info = wanted_info(len(content))
        self.fake.file_info[("wz-songs", rel)] = info
        return path

    def put(self, rel: str, content: str = "12345", info: Any = "leftover") -> Path:
        path = self.root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
        os.utime(path, (MTIME, MTIME))
        if info == "leftover":
            info = ignored_info(hub_copy(len(content)))
        if info is not None:
            self.fake.file_info[("wz-library", rel)] = info
        return path

    def prune(self, **kw: Any) -> int:
        kw.setdefault("grace", 0.0)
        return prune_device(self.cfg, "deck", PROFILE, self.client, self.rep, home=self.home, **kw)


class PruneDeviceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.dev = Device(self)

    def test_report_only_deletes_nothing(self) -> None:
        leftover = self.dev.put("roms/n64/a.z64")
        self.assertEqual(self.dev.prune(), 0)
        self.assertTrue(leftover.exists())
        out = "\n".join(self.dev.lines)
        self.assertIn("wz-library: 1 leftover file(s), 5 B", out)
        self.assertIn("roms/n64: 1 file(s)", out)
        self.assertIn("report only", out)
        # Syncthing is only asked to rescan; nothing else is written.
        self.assertEqual(
            [(m, p) for m, p, _ in self.dev.fake.writes()],
            [("POST", "/rest/db/scan?folder=wz-library")],
        )

    def test_apply_deletes_only_leftovers(self) -> None:
        d = self.dev
        gone = d.put("roms/n64/a.z64")
        deep = d.put("roms/n64/sub/deeper/b.z64")
        kept_here = d.put("roms/n64/mine.txt", info=ignored_info(None))
        modified = d.put("roms/n64/patched.z64", info=ignored_info(hub_copy(size=99)))
        wanted = d.put(
            "roms/nes/mario.nes",
            info={"global": hub_copy(), "local": {"ignored": False}},
        )
        untracked = d.put("tools/mine.sh", info=None)
        # The empty directories are ignored by Syncthing, so they may go.
        d.fake.file_info[("wz-library", "roms/n64/sub")] = ignored_info(hub_copy(0, DIR))
        d.fake.file_info[("wz-library", "roms/n64/sub/deeper")] = ignored_info(hub_copy(0, DIR))

        self.assertEqual(d.prune(apply=True), 0)

        self.assertFalse(gone.exists())
        self.assertFalse(deep.exists())
        self.assertFalse(deep.parent.exists())
        self.assertFalse(deep.parent.parent.exists())
        for survivor in (kept_here, modified, wanted, untracked):
            self.assertTrue(survivor.exists(), survivor)
        out = "\n".join(d.lines)
        self.assertIn("deleted 2 file(s), 10 B, removed 2 empty folder(s)", out)
        self.assertIn("roms/n64/mine.txt (the hub does not have it)", out)
        self.assertIn("roms/n64/patched.z64 (its size differs from the hub copy)", out)
        self.assertIn("1 file(s) not known to Syncthing", out)

    def test_generated_files_are_counted_apart_and_kept(self) -> None:
        d = self.dev
        generated = [
            d.put("roms/psx/metadata.txt", info=ignored_info(None)),
            d.put("roms/psx/systeminfo.txt", info=ignored_info(None)),
            d.put("bios/PPSSPP/ppge_atlas.zim", info=ignored_info(None)),
            d.put("bios/dc/vmu_save_A1.bin", info=ignored_info(None)),
        ]
        unusual = d.put("roms/n64/mine.txt", info=ignored_info(None))
        # The hub has a different copy of one: that is not "generated", it is listed.
        edited = d.put("roms/nes/metadata.txt", info=ignored_info(hub_copy(size=99)))

        self.assertEqual(d.prune(apply=True), 0)

        for survivor in (*generated, unusual, edited):
            self.assertTrue(survivor.exists(), survivor)
        out = "\n".join(d.lines)
        self.assertIn("wz-library: kept 2 ignored file(s) the hub cannot restore", out)
        self.assertIn("roms/n64/mine.txt (the hub does not have it)", out)
        self.assertIn("roms/nes/metadata.txt (its size differs from the hub copy)", out)
        self.assertIn("wz-library: 4 file(s) emulators and EmuDeck generate themselves", out)
        for path in ("roms/psx/metadata.txt", "bios/PPSSPP", "vmu_save"):
            self.assertNotIn(path, out)

    def test_only_generated_files_print_no_kept_line(self) -> None:
        d = self.dev
        d.put("roms/psx/metadata.txt", info=ignored_info(None))
        d.prune()
        out = "\n".join(d.lines)
        self.assertNotIn("cannot restore", out)
        self.assertIn("wz-library: 1 file(s) emulators and EmuDeck generate themselves", out)

    def test_directory_flagged_ignored_only_by_its_own_scan_is_removed(self) -> None:
        d = self.dev
        deep = d.put("roms/n64/sub/b.z64")
        stale = {"global": hub_copy(0, DIR), "local": {"ignored": False}}
        d.fake.file_info[("wz-library", "roms/n64/sub")] = stale
        d.fake.after_sub_scan[("wz-library", "roms/n64/sub")] = ignored_info(hub_copy(0, DIR))

        self.assertEqual(d.prune(apply=True), 0)

        self.assertFalse(deep.parent.exists())
        self.assertIn("removed 1 empty folder(s)", "\n".join(d.lines))

    def test_non_ignored_directory_is_never_removed(self) -> None:
        d = self.dev
        d.put("roms/psx/a.cue")
        d.fake.file_info[("wz-library", "roms/psx")] = {
            "global": hub_copy(0, DIR),
            "local": {"ignored": False},
        }
        d.prune(apply=True)
        self.assertTrue((d.root / "roms/psx").is_dir())
        self.assertFalse((d.root / "roms/psx/a.cue").exists())

    def test_protected_names_survive_even_if_syncthing_says_ignored(self) -> None:
        d = self.dev
        protected = [
            d.put(".stignore", "#include patterns-deck.stignore"),
            d.put("patterns-other.stignore"),
            d.put(".stversions/old.z64"),
            d.put(".stfolder/marker.txt"),
            d.put(".syncthing.partial.tmp"),
            d.put("~syncthing~partial.tmp"),
        ]
        # Same names below the root are ordinary files.
        ordinary = d.put("roms/.stignore")
        d.prune(apply=True)
        for path in protected:
            self.assertTrue(path.exists(), path)
        self.assertTrue((d.root / "patterns-deck.stignore").exists())
        self.assertFalse(ordinary.exists())

    def test_other_syncthing_folders_inside_the_root_are_skipped(self) -> None:
        d = self.dev
        save = d.put("saves/slot1.srm")
        d.fake.folders["wz-save-x"] = {"id": "wz-save-x", "path": str(d.root / "saves")}
        d.fake.folders["unrelated"] = {"id": "unrelated", "path": str(d.home / "elsewhere")}
        d.prune(apply=True)
        self.assertTrue(save.exists())

    def test_symlink_is_unlinked_and_its_target_kept(self) -> None:
        d = self.dev
        target = d.home / "outside.txt"
        target.write_text("precious")
        link = d.root / "roms/link"
        link.parent.mkdir(parents=True)
        link.symlink_to(target)
        d.fake.file_info[("wz-library", "roms/link")] = ignored_info(hub_copy(0, LINK))
        d.prune(apply=True)
        self.assertFalse(link.is_symlink())
        self.assertEqual(target.read_text(), "precious")

    def test_symlinked_directory_is_not_followed(self) -> None:
        d = self.dev
        outside = d.home / "outside"
        outside.mkdir()
        (outside / "x.z64").write_text("precious")
        (d.root / "roms").mkdir()
        (d.root / "roms/dir").symlink_to(outside, target_is_directory=True)
        d.fake.file_info[("wz-library", "roms/dir/x.z64")] = ignored_info(hub_copy(8))
        d.prune(apply=True)
        self.assertTrue((outside / "x.z64").exists())

    def test_size_cap_refuses_without_force(self) -> None:
        d = self.dev
        path = d.put("roms/n64/a.z64")
        self.assertEqual(d.prune(apply=True, max_gib=0), 1)
        self.assertTrue(path.exists())
        self.assertIn("refusing to delete", d.warnings[0])
        self.assertEqual(d.prune(apply=True, max_gib=0, force=True), 0)
        self.assertFalse(path.exists())

    def test_cap_is_in_gib(self) -> None:
        d = self.dev
        d.put("a.z64", "x" * 600_000)
        self.assertEqual(d.prune(apply=True, max_gib=0.5 / 1024), 1)  # 0.5 MiB cap
        self.assertTrue((d.root / "a.z64").exists())

    def test_refuses_while_bootstrap_ignores_are_active(self) -> None:
        d = self.dev
        path = d.put("roms/n64/a.z64")
        d.fake.ignores["wz-library"] = ["!/patterns-*.stignore", "*"]
        self.assertEqual(d.prune(apply=True), 1)
        self.assertTrue(path.exists())
        self.assertIn("not the final whitelist", d.warnings[0])

    def test_refuses_when_ignores_failed_to_load(self) -> None:
        d = self.dev
        path = d.put("roms/n64/a.z64")
        d.fake.ignores_error = "open patterns-deck.stignore: no such file"
        self.assertEqual(d.prune(apply=True), 1)
        self.assertTrue(path.exists())

    def test_refuses_when_the_whitelist_is_not_in_sync(self) -> None:
        d = self.dev
        path = d.put("roms/n64/a.z64")
        d.fake.file_info[("wz-library", "patterns-deck.stignore")] = {
            "global": {"version": ["X:2"]},
            "local": {"version": ["X:1"]},
        }
        self.assertEqual(d.prune(apply=True), 1)
        self.assertTrue(path.exists())
        self.assertIn("patterns-deck.stignore not in sync", d.warnings[0])

    def test_refuses_unsafe_folder_states(self) -> None:
        d = self.dev
        path = d.put("roms/n64/a.z64")
        folder = d.fake.folders["wz-library"]
        for patch, expected in (
            ({"paused": True}, "paused"),
            ({"type": "sendreceive"}, "folder type"),
            ({"path": str(d.home / "elsewhere")}, "the config says"),
        ):
            with self.subTest(expected):
                saved = dict(folder)
                folder.update(patch)
                d.warnings.clear()
                self.assertEqual(d.prune(apply=True), 1)
                self.assertIn(expected, d.warnings[0])
                self.assertTrue(path.exists())
                folder.clear()
                folder.update(saved)

    def test_refuses_when_the_marker_is_missing(self) -> None:
        d = self.dev
        path = d.put("roms/n64/a.z64")
        (d.root / ".stfolder").rmdir()
        self.assertEqual(d.prune(apply=True), 1)
        self.assertTrue(path.exists())
        self.assertIn("not mounted", d.warnings[0])

    def test_unknown_folder_is_skipped(self) -> None:
        del self.dev.fake.folders["wz-library"]
        self.assertEqual(self.dev.prune(), 1)
        self.assertIn("not configured", self.dev.warnings[0])

    def test_wrong_local_device_is_an_error(self) -> None:
        self.dev.fake.my_id = "Z" * 7 + "-" + "Z" * 7
        with self.assertRaisesRegex(PruneError, "devices.deck.device_id"):
            self.dev.prune()

    def test_only_whitelisted_folders_are_pruned(self) -> None:
        # The deck also has tools, saves and media; none of them is ever asked about.
        self.dev.prune()
        asked = {
            p.split("folder=")[1].split("&")[0]
            for _, p, _ in self.dev.fake.requests
            if "folder=" in p
        }
        self.assertEqual(asked, {"wz-library"})

    def test_list_prints_every_file(self) -> None:
        for i in range(3):
            self.dev.put(f"roms/n64/g{i}.z64")
        self.dev.prune(list_all=True)
        self.assertIn("  roms/n64/g2.z64", self.dev.lines)


class GuardTests(unittest.TestCase):
    def test_exactly_half_passes_and_one_byte_over_fails(self) -> None:
        self.assertEqual(guard({"a": (50, 100)}), [])
        self.assertEqual(guard({"a": (51, 100)}), ["a"])
        self.assertEqual(guard({"a": (5, 9)}), ["a"])
        self.assertEqual(guard({"a": (4, 9)}), [])

    def test_everything_or_nothing_held_is_an_edge_not_a_pass(self) -> None:
        self.assertEqual(guard({"a": (100, 100)}), ["a"])
        self.assertEqual(guard({"a": (1, 0)}), ["a"])
        self.assertEqual(guard({"a": (0, 0)}), [])
        self.assertEqual(guard({}), [])

    def test_names_every_folder_that_exceeds(self) -> None:
        totals = {"ok": (10, 100), "big": (80, 100), "half": (50, 100), "bigger": (3, 4)}
        self.assertEqual(guard(totals), ["big", "bigger"])


class GuardStateTests(unittest.TestCase):
    """The pure part of the trace a guard refusal leaves for `doctor`."""

    def test_a_refusal_round_trips(self) -> None:
        text = render_guard_state({"wz-songs": (5, 5), "wz-library": (80, 100)}, 1_700_000_000.9)
        refusal = parse_guard_state(text)
        assert refusal is not None
        self.assertEqual(refusal.when, 1_700_000_000)
        self.assertEqual(refusal.folders, (("wz-library", 80, 100), ("wz-songs", 5, 5)))

    def test_it_holds_sizes_and_folder_ids_only(self) -> None:
        text = render_guard_state({"wz-library": (80, 100)}, 1)
        self.assertEqual(set(json.loads(text)), {"version", "time", "folders"})
        self.assertEqual(json.loads(text)["folders"], {"wz-library": {"delete": 80, "held": 100}})

    def test_text_that_is_not_a_refusal_is_none(self) -> None:
        for text in (
            "",
            "not json",
            "[]",
            "{}",
            '{"time": 1}',
            '{"time": 1, "folders": {}}',
            '{"time": "x", "folders": {"a": {"delete": 1, "held": 1}}}',
            '{"time": 1, "folders": {"a": {"delete": 1}}}',
            '{"time": 1, "folders": {"a": 3}}',
            '{"time": true, "folders": {"a": {"delete": 1, "held": 1}}}',
        ):
            self.assertIsNone(parse_guard_state(text), text)


class GuardTraceTests(unittest.TestCase):
    """`prune_device` leaves a trace of a guard refusal and clears it on the next good run."""

    def setUp(self) -> None:
        self.dev = Device(self)
        self.base = self.dev.home / "state"
        self.path = guard_state_path(self.base)

    def run_prune(self, *, unattended: bool = True, **kw: Any) -> int:
        return self.dev.prune(
            apply=True, unattended=unattended, state_base=self.base, now=lambda: 1234.0, **kw
        )

    def refuse(self) -> None:
        self.dev.put("roms/n64/a.z64", "12345")
        self.dev.put("roms/nes/mario.nes", "abcd", info=wanted_info(4))
        self.assertEqual(self.run_prune(), 1)

    def test_a_refusal_is_recorded_with_the_folders_and_sizes(self) -> None:
        self.refuse()
        refusal = parse_guard_state(self.path.read_text())
        assert refusal is not None
        self.assertEqual((refusal.when, refusal.folders), (1234.0, (("wz-library", 5, 9),)))
        self.assertEqual(oct(self.path.stat().st_mode & 0o777), "0o600")

    def test_the_next_run_that_deletes_clears_it(self) -> None:
        self.refuse()
        self.dev.put("roms/nes/big.nes", "x" * 50, info=wanted_info(50))
        self.assertEqual(self.run_prune(), 0)
        self.assertFalse(self.path.exists())

    def test_a_run_with_nothing_to_delete_clears_it(self) -> None:
        self.refuse()
        (self.dev.root / "roms/n64/a.z64").unlink()
        self.assertEqual(self.run_prune(), 0)
        self.assertFalse(self.path.exists())

    def test_a_busy_run_leaves_it_alone(self) -> None:
        self.refuse()
        with mock.patch.object(Client, "scan", return_value=False):
            self.assertEqual(self.run_prune(), 0)
        self.assertTrue(self.path.exists())

    def test_a_report_only_run_leaves_it_alone(self) -> None:
        self.refuse()
        self.dev.prune(state_base=self.base)
        self.assertTrue(self.path.exists())

    def test_a_manual_apply_that_deletes_clears_it(self) -> None:
        self.refuse()
        self.assertEqual(self.run_prune(unattended=False), 0)
        self.assertFalse(self.path.exists())

    def test_the_size_cap_of_an_attended_run_is_not_a_guard_refusal(self) -> None:
        self.dev.put("roms/n64/a.z64", "12345")
        self.assertEqual(self.run_prune(unattended=False, max_gib=0), 1)
        self.assertFalse(self.path.exists())

    def test_without_a_state_folder_nothing_is_written(self) -> None:
        self.dev.put("roms/n64/a.z64", "12345")
        self.dev.put("roms/nes/mario.nes", "abcd", info=wanted_info(4))
        self.assertEqual(self.dev.prune(apply=True, unattended=True), 1)
        self.assertFalse(self.base.exists())

    def test_a_state_folder_that_cannot_be_written_is_a_warning(self) -> None:
        blocked = self.dev.home / "blocked"
        blocked.write_text("a file, not a folder")
        self.dev.put("roms/n64/a.z64", "12345")
        self.dev.put("roms/nes/mario.nes", "abcd", info=wanted_info(4))
        self.assertEqual(self.dev.prune(apply=True, unattended=True, state_base=blocked), 1)
        self.assertTrue(any("cannot update" in w for w in self.dev.warnings))


class UnattendedTests(unittest.TestCase):
    def setUp(self) -> None:
        self.dev = Device(self)

    def run_prune(self, **kw: Any) -> int:
        return self.dev.prune(apply=True, unattended=True, **kw)

    def test_deletes_with_no_size_cap(self) -> None:
        d = self.dev
        gone = d.put("roms/ps4/game/data.bin", "x" * 10)
        kept = d.put("roms/nes/mario.nes", "y" * 40, info=wanted_info(40))
        # The attended cap would refuse; unattended has none (max_gib is not consulted).
        self.assertEqual(self.run_prune(max_gib=0), 0)
        self.assertFalse(gone.exists())
        self.assertTrue(kept.exists())
        self.assertIn("deleted 1 file(s), 10 B", "\n".join(d.lines))

    def test_exactly_half_of_the_folder_is_deleted(self) -> None:
        d = self.dev
        gone = d.put("roms/n64/a.z64", "12345")
        d.put("roms/nes/mario.nes", "abcde", info=wanted_info(5))
        self.assertEqual(self.run_prune(), 0)
        self.assertFalse(gone.exists())

    def test_just_over_half_deletes_nothing(self) -> None:
        d = self.dev
        leftover = d.put("roms/n64/a.z64", "12345")
        d.put("roms/nes/mario.nes", "abcd", info=wanted_info(4))
        self.assertEqual(self.run_prune(), 1)
        self.assertTrue(leftover.exists())
        self.assertIn("refusing to delete anything", d.warnings[0])
        self.assertIn("wz-library", d.warnings[0])
        self.assertIn("by hand", d.warnings[0])
        self.assertNotIn("deleted", "\n".join(d.lines))

    def test_a_device_that_holds_only_leftovers_is_left_alone(self) -> None:
        leftover = self.dev.put("roms/n64/a.z64")
        self.assertEqual(self.run_prune(), 1)
        self.assertTrue(leftover.exists())

    def test_symlinks_and_nested_folders_do_not_count_as_held(self) -> None:
        d = self.dev
        leftover = d.put("roms/n64/a.z64", "12345")
        # 4 bytes held in a wanted file; a symlink and a nested Syncthing folder add nothing.
        d.put("roms/nes/mario.nes", "abcd", info=wanted_info(4))
        nested = d.root / "saves"
        nested.mkdir()
        (nested / "big.sav").write_text("z" * 1000)
        d.fake.folders["wz-saves"] = {
            "id": "wz-saves",
            "path": str(nested),
            "type": "sendreceive",
        }
        (d.root / "link").symlink_to(nested / "big.sav")
        self.assertEqual(self.run_prune(), 1)
        self.assertTrue(leftover.exists())

    def test_one_folder_over_half_stops_the_other_folders_too(self) -> None:
        d = self.dev
        d.add_songs()
        library = d.put("roms/n64/a.z64", "12345")
        d.put("roms/nes/mario.nes", "x" * 95, info=wanted_info(95))  # library: 5 of 100
        song = d.put_song("a/song.ogg", "12345")  # songs: 5 of 5
        self.assertEqual(self.run_prune(), 1)
        self.assertTrue(library.exists())
        self.assertTrue(song.exists())
        self.assertIn("wz-songs would lose", d.warnings[0])
        self.assertNotIn("wz-library would lose", d.warnings[0])

    def test_every_folder_under_the_line_is_pruned(self) -> None:
        d = self.dev
        d.add_songs()
        library = d.put("roms/n64/a.z64", "12345")
        d.put("roms/nes/mario.nes", "x" * 95, info=wanted_info(95))
        song = d.put_song("a/song.ogg", "12345")
        d.put_song("b/keep.ogg", "y" * 20, leftover=False)
        self.assertEqual(self.run_prune(), 0)
        self.assertFalse(library.exists())
        self.assertFalse(song.exists())

    def test_busy_deletes_nothing_and_is_not_a_failure(self) -> None:
        d = self.dev
        leftover = d.put("roms/n64/a.z64", "12345")
        d.put("roms/nes/mario.nes", "x" * 95, info=wanted_info(95))
        with mock.patch.object(Client, "scan", return_value=False):
            self.assertEqual(self.run_prune(), 0)
        self.assertTrue(leftover.exists())
        self.assertEqual(d.warnings, [])
        self.assertIn("busy", "\n".join(d.lines))

    def test_busy_in_one_folder_holds_back_the_others(self) -> None:
        d = self.dev
        d.add_songs()
        library = d.put("roms/n64/a.z64", "12345")
        d.put("roms/nes/mario.nes", "x" * 95, info=wanted_info(95))
        song = d.put_song("a/song.ogg", "12345")
        d.put_song("b/keep.ogg", "y" * 20, leftover=False)
        real_scan = Client.scan

        def scan(client: Client, folder_id: str, sub: str | None = None) -> bool:
            if folder_id == "wz-songs" and sub is None:
                return False
            return real_scan(client, folder_id, sub)

        with mock.patch.object(Client, "scan", scan):
            self.assertEqual(self.run_prune(), 0)
        self.assertTrue(library.exists())
        self.assertTrue(song.exists())

    def test_busy_is_still_a_failure_when_a_person_runs_it(self) -> None:
        d = self.dev
        d.put("roms/n64/a.z64")
        with mock.patch.object(Client, "scan", return_value=False):
            self.assertEqual(d.prune(apply=True), 1)
        self.assertIn("busy", d.warnings[0])

    def test_another_unsafe_folder_still_fails(self) -> None:
        d = self.dev
        d.put("roms/n64/a.z64")
        (d.root / ".stfolder").rmdir()
        self.assertEqual(self.run_prune(), 1)
        self.assertIn("not mounted", d.warnings[0])

    def test_bootstrap_ignores_still_fail(self) -> None:
        self.dev.put("roms/n64/a.z64")
        self.dev.fake.ignores["wz-library"] = ["*"]
        self.assertEqual(self.run_prune(), 1)
        self.assertIn("ignores are not the final", self.dev.warnings[0])

    def test_output_is_the_summary_and_list_is_ignored(self) -> None:
        d = self.dev
        d.put("roms/n64/a.z64", "12345")
        d.put("roms/n64/mine.txt", info=ignored_info(None))
        d.put("roms/nes/mario.nes", "x" * 95, info=wanted_info(95))
        self.run_prune(list_all=True)
        out = "\n".join(d.lines)
        self.assertIn("wz-library: 1 leftover file(s), 5 B", out)
        self.assertIn("kept 1 ignored file(s)", out)
        self.assertNotIn("roms/n64", out)
        self.assertNotIn("mine.txt", out)

    def test_unattended_output_counts_generated_files_without_names(self) -> None:
        d = self.dev
        d.put("roms/psx/metadata.txt", info=ignored_info(None))
        d.put("bios/PPSSPP/a.zim", info=ignored_info(None))
        self.run_prune()
        out = "\n".join(d.lines)
        self.assertIn("wz-library: 2 file(s) emulators and EmuDeck generate themselves", out)
        self.assertNotIn("cannot restore", out)
        self.assertNotIn("PPSSPP", out)
        self.assertNotIn("metadata", out)

    def test_it_needs_apply(self) -> None:
        with self.assertRaises(UsageError):
            self.dev.prune(unattended=True)

    def test_notify_reports_a_refused_run_as_not_applied(self) -> None:
        d = self.dev
        d.put("roms/n64/a.z64")
        with Receiver() as hook:
            self.run_prune(notify_url=hook.url)
        self.assertFalse(hook.bodies[0]["applied"])
        self.assertEqual(hook.bodies[0]["files"], 1)


class ApplyRecheckTests(unittest.TestCase):
    def test_a_file_that_became_wanted_again_is_not_deleted(self) -> None:
        d = Device(self)
        path = d.put("roms/n64/a.z64")
        report = FolderReport("wz-library", str(d.root), leftovers=[entry()])
        report.leftovers[0] = Entry("roms/n64/a.z64", "file", 5, MTIME)
        d.fake.file_info[("wz-library", "roms/n64/a.z64")] = {
            "global": hub_copy(),
            "local": {"ignored": False},
        }
        apply_folder(d.client, report)
        self.assertTrue(path.exists())
        self.assertEqual(report.deleted_files, 0)

    def test_a_file_edited_after_the_report_is_not_deleted(self) -> None:
        d = Device(self)
        path = d.put("roms/n64/a.z64")
        report = FolderReport("wz-library", str(d.root), leftovers=[entry()])
        path.write_text("edited meanwhile")
        apply_folder(d.client, report)
        self.assertTrue(path.exists())


class RescanTests(unittest.TestCase):
    def test_waits_for_the_scan_to_finish(self) -> None:
        states = iter(["idle", "idle", "scanning", "scanning", "idle"])
        client = mock.Mock()
        client.db_status.side_effect = lambda _f: {"state": next(states), "stateChanged": "t"}
        slept: list[float] = []
        rescan(
            client,
            "f",
            timeout=60,
            poll=1,
            grace=30,
            probes=DeviceProbes(sleep=slept.append, clock=lambda: 0),
        )
        client.scan.assert_called_once_with("f")
        self.assertEqual(slept, [1, 1, 1])

    def test_returns_after_the_grace_period_if_no_scan_is_seen(self) -> None:
        ticks = iter(range(0, 100))
        client = mock.Mock()
        client.db_status.return_value = {"state": "idle", "stateChanged": "t"}
        rescan(
            client,
            "f",
            timeout=60,
            poll=1,
            grace=3,
            probes=DeviceProbes(sleep=lambda _s: None, clock=lambda: next(ticks)),
        )

    def test_a_scan_queued_behind_a_pull_stops_prune(self) -> None:
        # The index is not refreshed until the pull ends; prune must not act on it.
        client = mock.Mock()
        client.db_status.return_value = {"state": "syncing", "stateChanged": "t"}
        client.scan.return_value = False
        with self.assertRaisesRegex(PruneError, "busy"):
            rescan(client, "f", timeout=60, poll=0, grace=0)

    def test_error_state_and_timeout(self) -> None:
        client = mock.Mock()
        client.db_status.return_value = {"state": "error", "error": "folder path missing"}
        with self.assertRaisesRegex(PruneError, "folder path missing"):
            rescan(client, "f", timeout=1, poll=0, grace=0)
        client.db_status.return_value = {"state": "scanning"}
        ticks = iter(range(0, 100))
        with self.assertRaisesRegex(PruneError, "did not finish"):
            rescan(
                client,
                "f",
                timeout=5,
                poll=0,
                grace=0,
                probes=DeviceProbes(sleep=lambda _s: None, clock=lambda: next(ticks)),
            )


class NotifyTests(unittest.TestCase):
    def test_posts_the_summary_for_the_report_and_for_the_apply(self) -> None:
        d = Device(self)
        d.put("roms/n64/a.z64")
        with Receiver() as hook:
            d.prune(notify_url=hook.url)
            d.prune(notify_url=hook.url, apply=True)
        self.assertEqual(
            hook.bodies,
            [
                {
                    "device": "deck",
                    "folder": "wz-library",
                    "files": 1,
                    "bytes": 5,
                    "applied": False,
                },
                {"device": "deck", "folder": "wz-library", "files": 1, "bytes": 5, "applied": True},
            ],
        )

    def test_nothing_to_report_sends_nothing(self) -> None:
        d = Device(self)
        with Receiver() as hook:
            d.prune(notify_url=hook.url)
        self.assertEqual(hook.bodies, [])

    def test_a_refused_apply_reports_applied_false(self) -> None:
        d = Device(self)
        d.put("roms/n64/a.z64")
        with Receiver() as hook:
            d.prune(notify_url=hook.url, apply=True, max_gib=0)
        self.assertFalse(hook.bodies[0]["applied"])

    def test_failure_warns_without_the_url_and_does_not_fail_the_run(self) -> None:
        d = Device(self)
        d.put("roms/n64/a.z64")
        with Receiver(status=500) as hook:
            self.assertEqual(d.prune(notify_url=hook.url, apply=True), 0)
        self.assertIn("HTTP 500", d.warnings[0])
        self.assertNotIn("secret-id", " ".join(d.warnings + d.lines))
        with self.assertRaises(NotifyError) as ctx:
            notify("http://127.0.0.1:1/api/webhook/secret-id", {})
        self.assertNotIn("secret-id", str(ctx.exception))


XML = """<configuration><gui tls="false"><address>{addr}</address>
<apikey>{key}</apikey></gui></configuration>"""


class CliTests(unittest.TestCase):
    def setUp(self) -> None:
        self.dev = Device(self)
        self.tmp = str(self.dev.home)
        host = self.dev.fake.url.removeprefix("http://")
        self.st_config = self.dev.home / "st" / "config.xml"
        self.st_config.parent.mkdir()
        self.st_config.write_text(XML.format(addr=host, key=API_KEY))
        self.config = write_config(self.tmp, self.dev.fake.url)
        env = mock.patch.dict(os.environ, {"HOME": self.tmp})
        env.start()
        self.addCleanup(env.stop)

    def prune(self, *args: str) -> tuple[int, str, str]:
        return run(
            "prune", "--config", self.config, "--syncthing-config", str(self.st_config), *args
        )

    def test_report_then_apply_infers_the_device(self) -> None:
        leftover = self.dev.put("roms/n64/a.z64")
        code, out, err = self.prune("--timeout", "5")
        self.assertEqual((code, err), (0, ""))
        self.assertIn("1 leftover file(s)", out)
        self.assertTrue(leftover.exists())
        code, out, _ = self.prune("deck", "--apply", "--timeout", "5")
        self.assertEqual(code, 0)
        self.assertIn("deleted 1 file(s)", out)
        self.assertFalse(leftover.exists())
        self.assertNotIn(API_KEY, out)

    def test_cap_is_exit_1(self) -> None:
        leftover = self.dev.put("roms/n64/a.z64")
        code, _, err = self.prune("--apply", "--max-gib", "0")
        self.assertEqual(code, 1)
        self.assertIn("refusing to delete", err)
        self.assertTrue(leftover.exists())

    def test_unattended_applies_with_no_cap(self) -> None:
        gone = self.dev.put("roms/n64/a.z64")
        self.dev.put("roms/nes/mario.nes", "x" * 95, info=wanted_info(95))
        code, out, err = self.prune("deck", "--apply", "--unattended", "--timeout", "5")
        self.assertEqual((code, err), (0, ""))
        self.assertIn("deleted 1 file(s)", out)
        self.assertFalse(gone.exists())

    def test_unattended_guard_is_exit_1(self) -> None:
        leftover = self.dev.put("roms/n64/a.z64")
        code, _, err = self.prune("deck", "--apply", "--unattended", "--timeout", "5")
        self.assertEqual(code, 1)
        self.assertIn("refusing to delete anything", err)
        self.assertTrue(leftover.exists())

    def test_unattended_usage_errors_are_exit_2(self) -> None:
        leftover = self.dev.put("roms/n64/a.z64")
        for args in (
            ("--unattended",),
            ("--apply", "--unattended", "--max-gib", "5"),
            ("--apply", "--unattended", "--max-gib", "50"),
            ("--apply", "--unattended", "--force"),
        ):
            with self.subTest(args=args):
                code, _, err = self.prune("deck", *args, "--timeout", "5")
                self.assertEqual(code, 2)
                self.assertIn("--unattended", err)
        self.assertTrue(leftover.exists())

    def test_usage_errors_are_exit_2(self) -> None:
        self.assertEqual(self.prune("--notify-url", "ftp://x/y")[0], 2)
        code, _, err = self.prune("tablet")
        self.assertEqual(code, 2)
        self.assertIn("unknown device", err)

    def test_unknown_local_device_needs_a_name(self) -> None:
        self.dev.fake.my_id = "Q" * 7
        code, _, err = self.prune()
        self.assertEqual(code, 2)
        self.assertIn("pass its name", err)

    def test_missing_local_syncthing_is_exit_1(self) -> None:
        missing = Path(self.tmp) / "nowhere" / "config.xml"
        code, _, err = run("prune", "--config", self.config, "--syncthing-config", str(missing))
        self.assertEqual(code, 1)
        self.assertIn("no Syncthing config", err)


if __name__ == "__main__":
    unittest.main()
