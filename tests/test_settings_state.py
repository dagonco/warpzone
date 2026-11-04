import json
import stat
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

from warpzone import settings_state as st


class TmpCase(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)


class StateDirTests(unittest.TestCase):
    def test_xdg_state_home_wins(self) -> None:
        got = st.state_dir({"XDG_STATE_HOME": "/xdg/state"}, Path("/home/u"))
        self.assertEqual(got, Path("/xdg/state/warpzone/settings"))

    def test_default_under_home(self) -> None:
        got = st.state_dir({}, Path("/home/u"))
        self.assertEqual(got, Path("/home/u/.local/state/warpzone/settings"))

    def test_relative_or_empty_xdg_is_ignored(self) -> None:
        for value in ("", "relative/state"):
            with self.subTest(value=value):
                got = st.state_dir({"XDG_STATE_HOME": value}, Path("/home/u"))
                self.assertEqual(got, Path("/home/u/.local/state/warpzone/settings"))


class SnapshotTests(TmpCase):
    def test_roundtrip_and_absent(self) -> None:
        snaps = st.Snapshots(self.tmp)
        self.assertIsNone(snaps.load("retroarch", "main"))
        snaps.save("retroarch", "main", {"video_vsync": True, "audio_latency": 64, "x": "y"})
        self.assertEqual(
            snaps.load("retroarch", "main"), {"video_vsync": True, "audio_latency": 64, "x": "y"}
        )
        path = self.tmp / "snapshots" / "retroarch" / "main.json"
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(self.tmp.stat().st_mode), 0o700)

    def test_empty_snapshot_differs_from_absent(self) -> None:
        snaps = st.Snapshots(self.tmp)
        snaps.save("e", "f", {})
        self.assertEqual(snaps.load("e", "f"), {})

    def test_data_hashes(self) -> None:
        snaps = st.Snapshots(self.tmp)
        self.assertIsNone(snaps.load_data("azahar"))
        snaps.save_data("azahar", {"sysdata": "ab" * 32})
        self.assertEqual(snaps.load_data("azahar"), {"sysdata": "ab" * 32})
        self.assertTrue((self.tmp / "snapshots" / "azahar" / "data.json").is_file())

    def test_corrupt_snapshot_is_an_error_not_absent(self) -> None:
        snaps = st.Snapshots(self.tmp)
        snaps.save("e", "f", {"a": 1})
        (self.tmp / "snapshots" / "e" / "f.json").write_text("{not json")
        with self.assertRaises(st.StateError):
            snaps.load("e", "f")
        (self.tmp / "snapshots" / "e" / "f.json").write_text("[1]")
        with self.assertRaises(st.StateError):
            snaps.load("e", "f")

    def test_unsafe_names_are_refused(self) -> None:
        snaps = st.Snapshots(self.tmp)
        for emu, file in (("../x", "f"), ("e", "a/b"), ("", "f"), ("e", ".hidden")):
            with self.subTest(emu=emu, file=file), self.assertRaises(ValueError):
                snaps.save(emu, file, {})

    def test_save_leaves_no_temp_files(self) -> None:
        snaps = st.Snapshots(self.tmp)
        snaps.save("e", "f", {"a": 1})
        snaps.save("e", "f", {"a": 2})
        self.assertEqual(
            sorted(p.name for p in (self.tmp / "snapshots" / "e").iterdir()), ["f.json"]
        )


class BackupTests(TmpCase):
    def setUp(self) -> None:
        super().setUp()
        self.src = self.tmp / "emu" / "retroarch.cfg"
        self.src.parent.mkdir()
        self.src.write_text('video_vsync = "true"\n')
        self.src.chmod(0o644)
        self.root = self.tmp / "state"
        self.t0 = datetime(2026, 1, 2, 3, 4, 5)

    def backup(self, now: datetime | None = None, **kw: object) -> Path:
        return st.backup(self.src, "retroarch", "main", root=self.root, now=now or self.t0, **kw)  # type: ignore[arg-type]

    def test_name_content_and_mode(self) -> None:
        got = self.backup()
        self.assertEqual(
            got, self.root / "backups" / "retroarch" / "main" / "20260102-030405-retroarch.cfg"
        )
        self.assertEqual(got.read_text(), 'video_vsync = "true"\n')
        self.assertEqual(stat.S_IMODE(got.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(self.root.stat().st_mode), 0o700)

    def test_same_second_collisions_get_a_counter(self) -> None:
        a = self.backup()
        self.src.write_text("two\n")
        b = self.backup()
        self.src.write_text("three\n")
        c = self.backup()
        self.assertEqual(
            [p.name for p in (a, b, c)],
            [
                "20260102-030405-retroarch.cfg",
                "20260102-030405-2-retroarch.cfg",
                "20260102-030405-3-retroarch.cfg",
            ],
        )
        self.assertEqual(
            [p.read_text() for p in (a, b, c)], ['video_vsync = "true"\n', "two\n", "three\n"]
        )

    def test_keeps_the_newest_ten(self) -> None:
        made = [self.backup(self.t0 + timedelta(seconds=i)) for i in range(13)]
        # two more in the same second as the last: the counter must sort after the plain name
        made.append(self.backup(self.t0 + timedelta(seconds=12)))
        left = sorted(p.name for p in made[0].parent.iterdir())
        self.assertEqual(len(left), 10)
        self.assertEqual(made[-1].name, "20260102-030417-2-retroarch.cfg")
        self.assertIn(made[-1].name, left)
        self.assertIn(made[-2].name, left)
        self.assertFalse(made[0].exists())
        self.assertFalse(made[3].exists())
        self.assertTrue(made[4].exists())

    def test_keep_is_per_file_and_per_emulator(self) -> None:
        for i in range(3):
            self.backup(self.t0 + timedelta(seconds=i), keep=2)
        other = st.backup(self.src, "other", "main", root=self.root, now=self.t0, keep=2)
        self.assertTrue(other.exists())
        self.assertEqual(len(list((self.root / "backups" / "retroarch" / "main").iterdir())), 2)

    def test_backup_of_a_symlink_copies_the_target(self) -> None:
        link = self.tmp / "emu" / "link.cfg"
        link.symlink_to(self.src)
        got = st.backup(link, "retroarch", "main", root=self.root, now=self.t0)
        self.assertFalse(got.is_symlink())
        self.assertEqual(got.read_text(), 'video_vsync = "true"\n')

    def test_missing_source_raises(self) -> None:
        with self.assertRaises(FileNotFoundError):
            st.backup(self.tmp / "none", "e", "f", root=self.root, now=self.t0)


class LogTests(TmpCase):
    def test_lines_and_mode(self) -> None:
        path = self.tmp / "s" / "log"
        log = st.Log(path, clock=lambda: datetime(2026, 1, 2, 3, 4, 5))
        log.line("apply start")
        log.line("two\nlines")
        self.assertEqual(
            path.read_text().splitlines(),
            ["2026-01-02T03:04:05 apply start", "2026-01-02T03:04:05 two lines"],
        )
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)

    def test_rotation_keeps_one_old_copy(self) -> None:
        path = self.tmp / "log"
        log = st.Log(path, max_bytes=200)
        for i in range(60):
            log.line(f"line {i:03d} " + "x" * 20)
        old = self.tmp / "log.1"
        self.assertTrue(old.is_file())
        self.assertEqual(sorted(p.name for p in self.tmp.iterdir()), ["log", "log.1"])
        self.assertLessEqual(path.stat().st_size, 200 + 80)
        self.assertIn("line 059", path.read_text())
        self.assertNotIn("line 000", path.read_text() + old.read_text())
        self.assertEqual(stat.S_IMODE(old.stat().st_mode), 0o600)

    def test_default_limit_is_one_mebibyte(self) -> None:
        self.assertEqual(st.LOG_MAX_BYTES, 1024 * 1024)


class LockTests(TmpCase):
    def test_contention_returns_false(self) -> None:
        with st.lock(root=self.tmp) as first:
            self.assertTrue(first)
            with st.lock(root=self.tmp) as second:
                self.assertFalse(second)
        with st.lock(root=self.tmp) as again:
            self.assertTrue(again)
        self.assertEqual(stat.S_IMODE((self.tmp / "lock").stat().st_mode), 0o600)

    def test_released_on_exception(self) -> None:
        with self.assertRaises(RuntimeError), st.lock(root=self.tmp):
            raise RuntimeError("boom")
        with st.lock(root=self.tmp) as again:
            self.assertTrue(again)


class LastApplyTests(TmpCase):
    def test_roundtrip(self) -> None:
        self.assertIsNone(st.read_last_apply(root=self.tmp))
        info = {"time": "2026-01-02T03:04:05", "exit": 0, "written": 3, "held": 1}
        st.write_last_apply(info, root=self.tmp)
        self.assertEqual(st.read_last_apply(root=self.tmp), info)
        self.assertEqual(json.loads((self.tmp / "last-apply.json").read_text()), info)

    def test_invalid_file_reads_as_none(self) -> None:
        (self.tmp / "last-apply.json").write_text("nope")
        self.assertIsNone(st.read_last_apply(root=self.tmp))


class ImportTests(unittest.TestCase):
    def test_no_network_imports(self) -> None:
        text = Path(st.__file__).read_text()
        for word in ("urllib", "http", "socket"):
            self.assertNotIn(f"import {word}", text)
            self.assertNotIn(f"from {word}", text)


if __name__ == "__main__":
    unittest.main()
