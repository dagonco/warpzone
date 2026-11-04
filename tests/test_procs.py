import tempfile
import unittest
from pathlib import Path

from support import FakeProc
from warpzone import procs


class TmpCase(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)


class RunningTests(TmpCase):
    def setUp(self) -> None:
        super().setUp()
        self.proc = FakeProc(self.tmp / "proc")

    def running(self, names: list[str], **kw: object) -> bool:
        return procs.running(names, self.proc.path, **kw)  # type: ignore[arg-type]

    def test_match_by_comm(self) -> None:
        self.proc.add(100, "retroarch", [])
        self.assertTrue(self.running(["retroarch"]))

    def test_match_by_argv0_basename(self) -> None:
        self.proc.add(100, "AppRun", ["/tmp/.mount_x/usr/bin/azahar", "game.3ds"])
        self.assertTrue(self.running(["azahar"]))

    def test_match_by_truncated_comm(self) -> None:
        name = "duckstation-qt-x64-ReleaseLTO.AppImage"
        self.proc.add(100, name[:15], [])
        self.assertEqual(len(name[:15]), 15)
        self.assertTrue(self.running([name]))

    def test_no_match(self) -> None:
        self.proc.add(100, "bash", ["bash"])
        self.assertFalse(self.running(["retroarch"]))
        self.assertFalse(self.running([]))

    def test_launcher_script_is_not_the_emulator(self) -> None:
        self.proc.add(100, "bash", ["bash", "/emu/tools/launchers/retroarch.sh"])
        self.proc.add(101, "retroarch.sh", ["/bin/bash", "/x/retroarch.sh"])
        self.assertFalse(self.running(["retroarch"]))

    def test_arguments_mentioning_the_emulator_are_not_a_match(self) -> None:
        self.proc.add(100, "nano", ["nano", "/home/u/.config/retroarch/retroarch.cfg"])
        self.proc.add(101, "python3", ["python3", "-m", "warpzone", "retroarch"])
        self.proc.add(102, "bash", ["bash", "-c", "pgrep -f retroarch"])
        self.assertFalse(self.running(["retroarch"]))

    def test_own_process_is_skipped_even_when_it_looks_like_the_emulator(self) -> None:
        self.proc.add(100, "retroarch", ["/usr/bin/retroarch"])
        self.assertFalse(self.running(["retroarch"], self_pid=100))

    def test_parent_process_is_skipped(self) -> None:
        self.proc.add(100, "bash", ["bash", "-c", "warpzone"], ppid=50)
        self.proc.add(50, "retroarch", ["/usr/bin/retroarch"])
        self.assertFalse(self.running(["retroarch"], self_pid=100))
        self.assertTrue(self.running(["retroarch"], self_pid=999))

    def test_other_instance_is_still_found_next_to_self(self) -> None:
        self.proc.add(100, "retroarch", ["retroarch"])
        self.proc.add(200, "retroarch", ["retroarch"])
        self.assertTrue(self.running(["retroarch"], self_pid=100))

    def test_unreadable_entries_are_ignored(self) -> None:
        self.proc.add(100, None, None)
        (self.proc.path / "200").mkdir()
        (self.proc.path / "self").mkdir()  # not a pid
        (self.proc.path / "meminfo").write_text("x")
        self.proc.add(300, "retroarch", None)  # cmdline unreadable, comm is enough
        self.assertTrue(self.running(["retroarch"]))

    def test_empty_cmdline_of_a_kernel_thread(self) -> None:
        self.proc.add(2, "kthreadd", [])
        self.assertFalse(self.running(["retroarch"]))

    def test_missing_proc_raises(self) -> None:
        with self.assertRaises(procs.NoProc):
            procs.running(["retroarch"], self.tmp / "nope")


if __name__ == "__main__":
    unittest.main()
