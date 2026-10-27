import json
import os
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any
from unittest import mock

from support import API_KEY, DECK_ID, FakeSyncthing, make_config
from test_cli import run, write_config
from warpzone.profiles import load_profile
from warpzone.provision import Reporter
from warpzone.prune import (
    Entry,
    FolderReport,
    PruneError,
    Verdict,
    apply_folder,
    classify,
    notify,
    parse_time,
    prune_device,
    rescan,
)
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
        self.fake.ignores["wz-library"] = ["#include patterns-deck.stignore"]
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
        rescan(client, "f", timeout=60, poll=1, grace=30, sleep=slept.append, clock=lambda: 0)
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
            sleep=lambda _s: None,
            clock=lambda: next(ticks),
        )

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
                sleep=lambda _s: None,
                clock=lambda: next(ticks),
            )


class Receiver:
    """A one-shot webhook receiver."""

    def __init__(self, status: int = 200) -> None:
        self.bodies: list[Any] = []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args: object) -> None:
                pass

            def do_POST(self) -> None:
                length = int(self.headers.get("Content-Length") or 0)
                outer.bodies.append(json.loads(self.rfile.read(length)))
                self.send_response(status)
                self.send_header("Content-Length", "0")
                self.end_headers()

        self.server = HTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}/api/webhook/secret-id"
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def __enter__(self) -> "Receiver":
        self.thread.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self.server.shutdown()
        self.server.server_close()


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
        with self.assertRaises(PruneError) as ctx:
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
