"""Title links: one symlink per library game in an emulator's own game folder.

The pure plan, the sync on a temporary home, the profile table, the ES-DE startup script,
`provision device`, the command and `doctor`."""

import os
import tempfile
import unittest
import unittest.mock
from pathlib import Path
from unittest import addModuleCleanup, mock

import test_shadps4
from support import DECK_ID, HUB_ID, FakeProc
from test_cli import run, write_config
from test_doctor import by_name
from test_shadps4 import PROFILE
from warpzone import esde, titlelinks
from warpzone import provision as provision_mod
from warpzone.doctor import OK, WARN
from warpzone.profiles import ProfileError, parse_profile
from warpzone.titlelinks import Entry


def setUpModule() -> None:
    """provision_device would install the updater and its timer, which runs a real self-test
    of the script (seconds, inside a clone): tests/test_update_timer.py covers that."""
    patcher = mock.patch.object(provision_mod, "ensure_self_update")
    patcher.start()
    addModuleCleanup(patcher.stop)


SOURCE = "/lib/roms/psvita/app"
TITLE = "PCSF00024"


class PlanTests(unittest.TestCase):
    def plan(self, ready, current):
        return titlelinks.plan(SOURCE, ready, current)

    def test_a_title_without_a_link_gets_one(self) -> None:
        plan = self.plan([TITLE, "PCSA00001"], {})
        self.assertEqual(plan.create, ("PCSA00001", TITLE))
        self.assertEqual((plan.remove, plan.ok, plan.kept), ((), (), ()))

    def test_a_link_to_the_library_folder_is_left_as_it_is(self) -> None:
        plan = self.plan([TITLE], {TITLE: Entry("link", f"{SOURCE}/{TITLE}")})
        self.assertEqual((plan.create, plan.remove, plan.ok), ((), (), (TITLE,)))

    def test_a_real_folder_or_file_is_never_touched(self) -> None:
        plan = self.plan([TITLE, "PCSA00001"], {TITLE: Entry("dir"), "PCSA00001": Entry("file")})
        self.assertEqual((plan.create, plan.remove), ((), ()))
        self.assertEqual(plan.kept, ("PCSA00001", TITLE))

    def test_a_link_to_somewhere_else_is_kept(self) -> None:
        plan = self.plan([TITLE], {TITLE: Entry("link", "/elsewhere/x")})
        self.assertEqual((plan.create, plan.remove, plan.kept), ((), (), (TITLE,)))

    def test_a_dangling_link_of_ours_is_removed(self) -> None:
        gone = {"PCSB00002": Entry("link", f"{SOURCE}/PCSB00002", alive=False)}
        self.assertEqual(self.plan([TITLE], gone).remove, ("PCSB00002",))

    def test_a_dangling_link_that_is_not_ours_stays(self) -> None:
        theirs = {"PCSB00002": Entry("link", "/other/PCSB00002", alive=False)}
        plan = self.plan([TITLE], theirs)
        self.assertEqual((plan.remove, plan.kept), ((), ()))

    def test_a_link_of_ours_that_still_works_stays_even_if_not_ready(self) -> None:
        # a game that is still syncing keeps its link; only dangling ones go
        plan = self.plan([], {TITLE: Entry("link", f"{SOURCE}/{TITLE}")})
        self.assertEqual((plan.create, plan.remove), ((), ()))

    def test_a_dangling_link_of_ours_in_the_way_is_replaced(self) -> None:
        stale = {TITLE: Entry("link", f"{SOURCE}/PCSB00002", alive=False)}
        plan = self.plan([TITLE], stale)
        self.assertEqual((plan.remove, plan.create), ((TITLE,), (TITLE,)))

    def test_a_trailing_slash_or_dots_in_the_source_do_not_matter(self) -> None:
        plan = titlelinks.plan(SOURCE + "/", [TITLE], {TITLE: Entry("link", f"{SOURCE}/./{TITLE}")})
        self.assertEqual(plan.ok, (TITLE,))


class Helpers:
    """What the tests do on a temporary home laid out like a Steam Deck."""

    home: Path
    proc: Path
    source: Path
    target: Path
    profile: object
    said: list[str]
    warned: list[str]

    def lay_out(self) -> None:
        self.source = self.home / "Emulation/roms/psvita/app"
        self.target = self.home / "Emulation/storage/Vita3K/ux0/app"
        self.target.mkdir(parents=True)
        self.said, self.warned = [], []

    def game(self, title_id: str = TITLE, *, complete: bool = True) -> Path:
        folder = self.source / title_id
        (folder / "sce_sys").mkdir(parents=True)
        (folder / "data.bin").write_bytes(b"x")
        if complete:
            (folder / "eboot.bin").write_bytes(b"SCE\x00")
            (folder / "sce_sys" / "param.sfo").write_bytes(b"\x00PSF")
        return folder

    def sync(self, **kw) -> int:
        kw.setdefault("proc", self.proc.path)
        return titlelinks.sync(
            "deck", self.profile, self.home, say=self.said.append, warn=self.warned.append, **kw
        )

    def link(self, title_id: str = TITLE) -> Path:
        return self.target / title_id


class Home(Helpers, unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        tmp = Path(self._tmp.name)
        self.home = tmp / "home"
        self.proc = FakeProc(tmp / "proc")
        self.profile = PROFILE
        self.lay_out()

    def start(self, comm: str) -> None:
        self.proc.add(4242, comm)


class SyncTests(Home):
    def test_a_link_per_game_points_at_the_library_folder(self) -> None:
        folder = self.game()
        self.game("PCSA00001")
        self.sync()
        self.assertTrue(self.link().is_symlink())
        self.assertEqual(os.readlink(self.link()), str(folder))
        self.assertTrue(self.link("PCSA00001").is_symlink())
        self.assertEqual((self.link() / "eboot.bin").read_bytes(), b"SCE\x00")
        self.assertEqual(self.warned, [])

    def test_a_second_run_changes_nothing(self) -> None:
        self.game()
        self.sync()
        stamp = self.link().lstat().st_mtime_ns
        self.said.clear()
        self.sync()
        self.assertEqual(self.link().lstat().st_mtime_ns, stamp)
        self.assertEqual(self.warned, [])
        self.assertEqual(sorted(p.name for p in self.target.iterdir()), [TITLE])

    def test_a_real_folder_in_the_way_is_not_touched(self) -> None:
        self.game()
        real = self.link()
        real.mkdir()
        (real / "mine").write_text("keep")
        self.sync()
        self.assertFalse(real.is_symlink())
        self.assertEqual((real / "mine").read_text(), "keep")
        self.assertTrue(any(TITLE in line and "real" in line for line in self.said + self.warned))

    def test_a_link_to_somewhere_else_is_not_touched(self) -> None:
        self.game()
        os.symlink("/nowhere/at/all", self.link())
        self.sync()
        self.assertEqual(os.readlink(self.link()), "/nowhere/at/all")

    def test_a_dangling_link_of_ours_is_removed_and_the_library_is_not(self) -> None:
        folder = self.game("PCSB00002")
        self.sync()
        folder_files = sorted(p.name for p in folder.iterdir())
        for child in sorted(folder.rglob("*"), reverse=True):
            child.unlink() if child.is_file() else child.rmdir()
        folder.rmdir()
        self.sync()
        self.assertFalse(os.path.lexists(self.link("PCSB00002")))
        self.assertTrue(folder_files)  # (it had content; it is the game that was deleted)

    def test_a_dangling_link_that_is_not_ours_stays(self) -> None:
        os.symlink("/gone/for/good", self.link("PCSB00002"))
        self.sync()
        self.assertTrue(os.path.islink(self.link("PCSB00002")))

    def test_a_game_that_is_still_syncing_gets_no_link_yet(self) -> None:
        self.game(complete=False)
        self.sync()
        self.assertFalse(os.path.lexists(self.link()))
        (self.source / TITLE / "eboot.bin").write_bytes(b"x")
        self.sync()
        self.assertFalse(os.path.lexists(self.link()))  # param.sfo is still missing
        (self.source / TITLE / "sce_sys" / "param.sfo").write_bytes(b"x")
        self.sync()
        self.assertTrue(self.link().is_symlink())

    def test_only_title_id_folders_are_linked(self) -> None:
        for name in (f"{TITLE}.part", "metadata", "pcsf00024", "PCSJ00001"):
            self.game(name)
        (self.source / "stray.txt").write_text("x")
        self.sync()
        self.assertEqual(list(self.target.iterdir()), [])

    def test_a_symlink_in_the_library_is_not_a_game(self) -> None:
        real = self.game()
        os.symlink(real, self.source / "PCSA00001")
        self.sync()
        self.assertEqual(sorted(p.name for p in self.target.iterdir()), [TITLE])

    def test_dry_run_changes_nothing_and_says_what_it_would_do(self) -> None:
        self.game()
        os.symlink(f"{self.source}/PCSB00002", self.link("PCSB00002"))
        self.sync(dry_run=True)
        self.assertEqual(sorted(p.name for p in self.target.iterdir()), ["PCSB00002"])
        self.assertTrue(any("would link" in line and TITLE in line for line in self.said))
        self.assertTrue(any("would remove" in line and "PCSB00002" in line for line in self.said))

    def test_nothing_is_done_while_vita3k_runs(self) -> None:
        self.game()
        self.start("Vita3K")
        self.sync()
        self.assertEqual(list(self.target.iterdir()), [])
        self.assertTrue(any("Vita3K is running" in line for line in self.said + self.warned))

    def test_the_caller_is_told_when_the_emulator_runs(self) -> None:
        self.game()
        told: list[str] = []
        self.sync()
        self.assertEqual(told, [])
        self.start("Vita3K")
        (self.target / TITLE).unlink(missing_ok=True)
        self.sync(on_busy=lambda: told.append("busy"))
        self.assertEqual(told, ["busy"])

    def test_nothing_is_done_when_it_cannot_be_told(self) -> None:
        self.game()
        self.sync(proc=self.proc.path / "missing")
        self.assertEqual(list(self.target.iterdir()), [])

    def test_a_missing_vita3k_folder_is_not_created(self) -> None:
        self.game()
        os.rmdir(self.target)
        self.sync()
        self.assertFalse(self.target.exists())
        self.assertTrue(any("Vita3K" in line for line in self.said + self.warned))

    def test_a_missing_library_folder_is_not_an_error(self) -> None:
        self.assertEqual(self.sync(), 0)
        self.assertEqual(list(self.target.iterdir()), [])

    def test_a_link_that_appears_meanwhile_is_not_an_error(self) -> None:
        self.game()
        real_symlink = os.symlink

        def racing(src, dst, *a, **kw):
            real_symlink("/other", dst)
            raise FileExistsError(17, "exists", str(dst))

        with unittest.mock.patch("os.symlink", racing):
            self.sync()
        self.assertEqual(os.readlink(self.link()), "/other")


class LicenseSyncTests(Home):
    """`ux0/license/<TITLEID>` links to the library's licence folder, like `ux0/app`."""

    def setUp(self) -> None:
        super().setUp()
        self.lic_source = self.home / "Emulation/roms/psvita/license"
        self.lic_target = self.home / "Emulation/storage/Vita3K/ux0/license"

    def licence(self, title_id: str = TITLE) -> Path:
        folder = self.lic_source / title_id
        folder.mkdir(parents=True)
        (folder / f"EP9000-{title_id}_00-PDEUAP1648614960.rif").write_bytes(b"R" * 512)
        return folder

    def test_a_licence_folder_is_linked_and_the_target_is_made(self) -> None:
        folder = self.licence()
        self.assertFalse(self.lic_target.exists())  # Vita3K only makes ux0/license when needed
        self.sync()
        self.assertTrue((self.lic_target / TITLE).is_symlink())
        self.assertEqual(os.readlink(self.lic_target / TITLE), str(folder))
        self.assertEqual(len(list((self.lic_target / TITLE).glob("*.rif"))), 1)
        self.assertEqual(self.warned, [])

    def test_a_second_run_changes_nothing(self) -> None:
        self.licence()
        self.sync()
        stamp = (self.lic_target / TITLE).lstat().st_mtime_ns
        self.said.clear()
        self.assertEqual(self.sync(), 0)
        self.assertEqual((self.lic_target / TITLE).lstat().st_mtime_ns, stamp)

    def test_dry_run_creates_nothing(self) -> None:
        self.licence()
        self.sync(dry_run=True)
        self.assertFalse(self.lic_target.exists())
        self.assertTrue(any("would link" in line and TITLE in line for line in self.said))

    def test_a_real_folder_in_the_way_is_not_touched(self) -> None:
        self.licence()
        mine = self.lic_target / TITLE
        mine.mkdir(parents=True)
        (mine / "mine.rif").write_bytes(b"keep")
        self.sync()
        self.assertFalse(mine.is_symlink())
        self.assertEqual((mine / "mine.rif").read_bytes(), b"keep")

    def test_nothing_is_made_while_vita3k_runs(self) -> None:
        self.licence()
        self.start("Vita3K")
        self.sync()
        self.assertFalse(self.lic_target.exists())

    def test_the_target_is_not_made_when_vita3k_never_ran(self) -> None:
        self.licence()
        os.rmdir(self.target)
        os.rmdir(self.target.parent)  # no ux0
        self.sync()
        self.assertFalse(self.lic_target.exists())
        self.assertTrue(any("Vita3K" in line for line in self.said + self.warned))

    def test_no_licences_make_no_folder(self) -> None:
        self.sync()
        self.assertFalse(self.lic_target.exists())

    def test_the_games_are_still_linked_by_their_own_rule(self) -> None:
        self.game()
        self.licence()
        self.sync()
        self.assertTrue(self.link().is_symlink())
        self.assertTrue((self.lic_target / TITLE).is_symlink())


class ProfileTests(unittest.TestCase):
    def test_the_shipped_profile_links_the_vita3k_licences(self) -> None:
        rule = PROFILE.links["vita3k-license"]
        self.assertEqual(rule.source, "{library}/roms/psvita/license")
        self.assertEqual(rule.target, "~/Emulation/storage/Vita3K/ux0/license")
        self.assertEqual(rule.process, ("Vita3K",))
        self.assertEqual(rule.match, r"PCS[A-I]\d{5}")
        self.assertEqual((rule.require, rule.noun, rule.create_target), ((), "license", True))
        self.assertEqual(
            (PROFILE.links["vita3k"].noun, PROFILE.links["vita3k"].create_target), ("game", False)
        )

    def test_noun_and_create_target_are_checked(self) -> None:
        good = {"source": "{library}/x", "target": "~/t", "process": ["P"]}

        def parse(**change):
            table = {**good, **change}
            return parse_profile(
                {"name": "x", "paths": {"library": "~/L"}, "links": {"a": table}}, "t"
            )

        rule = parse(noun="license", create_target=True).links["a"]
        self.assertEqual((rule.noun, rule.create_target), ("license", True))
        for change, message in (
            ({"noun": ""}, "links.a.noun"),
            ({"noun": 3}, "links.a.noun"),
            ({"create_target": "yes"}, "links.a.create_target"),
        ):
            with self.assertRaisesRegex(ProfileError, message, msg=str(change)):
                parse(**change)

    def test_the_shipped_profile_links_vita3k(self) -> None:
        rule = PROFILE.links["vita3k"]
        self.assertEqual(rule.source, "{library}/roms/psvita/app")
        self.assertEqual(rule.target, "~/Emulation/storage/Vita3K/ux0/app")
        self.assertEqual(rule.process, ("Vita3K",))
        self.assertEqual(rule.match, r"PCS[A-I]\d{5}")
        self.assertEqual(rule.require, ("eboot.bin", "sce_sys/param.sfo"))

    def test_the_table_is_checked(self) -> None:
        good = {
            "source": "{library}/roms/x",
            "target": "~/t",
            "process": ["P"],
            "match": "A.*",
            "require": ["a"],
        }

        def parse(**change):
            table = {**good, **change}
            table = {k: v for k, v in table.items() if v is not None}
            return parse_profile(
                {"name": "x", "paths": {"library": "~/L"}, "links": {"a": table}}, "t"
            )

        self.assertEqual(parse().links["a"].match, "A.*")
        self.assertEqual(parse(match=None, require=None).links["a"].require, ())
        for change, message in (
            ({"source": None}, "links.a.source"),
            ({"target": ""}, "links.a.target"),
            ({"target": "relative"}, "links.a.target"),
            ({"process": []}, "links.a.process"),
            ({"match": "("}, "links.a.match"),
            ({"require": [1]}, "links.a.require"),
            ({"require": ["../x"]}, "links.a.require"),
            ({"extra": 1}, "unknown key"),
        ):
            with self.assertRaisesRegex(ProfileError, message, msg=str(change)):
                parse(**change)
        with self.assertRaisesRegex(ProfileError, "paths.library"):
            parse_profile({"name": "x", "links": {"a": good}}, "t")
        with self.assertRaisesRegex(ProfileError, "links"):
            parse_profile({"name": "x", "links": 3}, "t")


class ScriptTests(unittest.TestCase):
    def test_the_startup_script_is_bounded_and_never_fails(self) -> None:
        text = esde.render_links_script("/usr/bin/python3", "deck")
        self.assertTrue(text.startswith("#!/bin/sh\n"))
        self.assertIn("timeout 15 '/usr/bin/python3' -m warpzone", text)
        self.assertIn("links sync 'deck' --quiet", text)
        self.assertTrue(text.endswith("exit 0\n"))
        self.assertEqual(esde.script_interpreter(text), "/usr/bin/python3")
        with_config = esde.render_links_script("/p", "deck", Path("/x/c.toml"))
        self.assertIn("--config '/x/c.toml'", with_config)

    def test_the_script_keeps_working_when_warpzone_fails(self) -> None:
        import subprocess

        with tempfile.TemporaryDirectory() as tmp:
            script = Path(tmp) / "links.sh"
            script.write_text(esde.render_links_script("/nonexistent/python", "deck"))
            done = subprocess.run(["sh", str(script)], capture_output=True, check=False)
        self.assertEqual(done.returncode, 0)
        self.assertEqual(done.stdout + done.stderr, b"")


class ProvisionTests(Helpers, test_shadps4.Device, unittest.TestCase):
    def setUp(self) -> None:
        test_shadps4.Device.setUp(self)
        self.lay_out()
        self.esde = self.home / "ES-DE"
        (self.esde / "settings").mkdir(parents=True)
        (self.esde / "settings" / "es_settings.xml").write_text(
            '<?xml version="1.0"?>\n<bool name="CustomEventScripts" value="true" />\n'
        )
        self.script = self.esde / "scripts" / "startup" / esde.LINKS_SCRIPT

    def prov(self, **kw):
        return self.provision(import_check=lambda python: True, python="/usr/bin/python3", **kw)

    def test_provision_links_the_games_and_installs_the_startup_script(self) -> None:
        self.game()
        _, lines = self.prov()
        self.assertTrue(self.link().is_symlink())
        self.assertTrue(os.access(self.script, os.X_OK))
        self.assertIn("links sync 'deck'", self.script.read_text())
        self.assertTrue(any("links" in line for line in lines), lines)

    def test_provision_links_the_licences_too(self) -> None:
        self.game()
        lic = self.home / "Emulation/roms/psvita/license" / TITLE
        lic.mkdir(parents=True)
        (lic / "EP9000-PCSF00024_00-PDEUAP1648614960.rif").write_bytes(b"R" * 512)
        link = self.home / "Emulation/storage/Vita3K/ux0/license" / TITLE
        self.prov(dry_run=True)
        self.assertFalse(link.parent.exists())
        self.prov()
        self.assertEqual(os.readlink(link), str(lic))
        stamp = link.lstat().st_mtime_ns
        self.prov()
        self.assertEqual(link.lstat().st_mtime_ns, stamp)

    def test_a_second_provision_changes_nothing(self) -> None:
        self.game()
        self.prov()
        stamps = (self.link().lstat().st_mtime_ns, self.script.stat().st_mtime_ns)
        self.prov()
        self.assertEqual((self.link().lstat().st_mtime_ns, self.script.stat().st_mtime_ns), stamps)

    def test_dry_run_makes_no_link_and_no_script(self) -> None:
        self.game()
        self.prov(dry_run=True)
        self.assertEqual(list(self.target.iterdir()), [])
        self.assertFalse(self.script.exists())

    def test_provision_leaves_the_links_alone_while_vita3k_runs(self) -> None:
        self.game()
        self.start("Vita3K")
        rep, _ = self.prov()
        self.assertEqual(list(self.target.iterdir()), [])
        self.assertTrue(any("Vita3K is running" in w for w in rep.warnings), rep.warnings)

    def test_unattended_provision_defers_while_vita3k_runs(self) -> None:
        self.game()
        self.start("Vita3K")
        rep, _ = self.prov(unattended=True)
        self.assertTrue(rep.deferred)
        self.assertEqual(list(self.target.iterdir()), [])

    def test_unattended_provision_is_not_deferred_when_it_can_link(self) -> None:
        self.game()
        rep, _ = self.prov(unattended=True)
        self.assertFalse(rep.deferred)
        self.assertTrue(self.link().is_symlink())

    def test_the_command_links_without_the_network(self) -> None:
        self.game()
        with tempfile.TemporaryDirectory() as tmp:
            config = write_config(tmp, "http://127.0.0.1:1")
            old = os.environ.get("HOME")
            os.environ["HOME"] = str(self.home)
            try:
                with (
                    unittest.mock.patch("pathlib.Path.home", return_value=self.home),
                    unittest.mock.patch.object(titlelinks.procs, "running", return_value=False),
                ):
                    code, out, err = run("links", "sync", "deck", "--config", config, "--dry-run")
                    self.assertEqual((code, err), (0, ""), out)
                    self.assertIn("would link", out)
                    self.assertEqual(list(self.target.iterdir()), [])
                    code, out, err = run("links", "sync", "deck", "--config", config, "--quiet")
            finally:
                if old is None:
                    os.environ.pop("HOME", None)
                else:
                    os.environ["HOME"] = old
        self.assertEqual((code, out, err), (0, "", ""))
        self.assertTrue(self.link().is_symlink())

    def test_an_unknown_device_is_a_usage_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = write_config(tmp, "http://127.0.0.1:1")
            code, _, err = run("links", "sync", "nope", "--config", config)
        self.assertEqual(code, 2)
        self.assertIn("nope", err)


class DoctorTests(Helpers, test_shadps4.Device, unittest.TestCase):
    CONFIG = test_shadps4.DoctorTests.CONFIG
    check = test_shadps4.DoctorTests.check

    def setUp(self) -> None:
        test_shadps4.Device.setUp(self)
        syncthing = self.home / ".config/syncthing"
        syncthing.mkdir(parents=True)
        (syncthing / "config.xml").write_text(
            f'<configuration><folder id="wz-library" path="{self.home}/Emulation/">'
            f'<device id="{HUB_ID}"/></folder><device id="{DECK_ID}" name="x"/>'
            "<gui><address>127.0.0.1:8384</address><apikey>k</apikey></gui></configuration>"
        )
        self.lay_out()
        self.script = self.home / "ES-DE/scripts/startup" / esde.LINKS_SCRIPT
        self.script.parent.mkdir(parents=True)
        self.script.write_text("#!/bin/sh\n")

    def result(self):
        return by_name(self.check(), "Vita3K game links")

    def test_a_game_without_a_link_warns_with_the_fix(self) -> None:
        self.game()
        result = self.result()
        self.assertEqual(result.status, WARN)
        self.assertIn(TITLE, result.detail)
        self.assertIn("warpzone links sync deck", result.fix)

    def test_a_linked_game_is_ok(self) -> None:
        self.game()
        self.sync()
        result = self.result()
        self.assertEqual(result.status, OK, result.detail)
        self.assertIn("1 game", result.detail)

    def test_a_missing_startup_script_warns(self) -> None:
        self.script.unlink()
        result = self.result()
        self.assertEqual(result.status, WARN)
        self.assertIn(esde.LINKS_SCRIPT, result.detail)
        self.assertIn("warpzone provision device deck", result.fix)

    def test_a_dangling_link_warns(self) -> None:
        self.source.mkdir(parents=True)
        os.symlink(f"{self.source}/PCSB00002", self.link("PCSB00002"))
        result = self.result()
        self.assertEqual(result.status, WARN)
        self.assertIn("PCSB00002", result.detail)

    def test_a_missing_vita3k_folder_is_a_warning_to_start_it(self) -> None:
        self.game()
        os.rmdir(self.target)
        result = self.result()
        self.assertEqual(result.status, WARN)
        self.assertIn("start Vita3K", result.fix)

    def test_no_games_is_ok(self) -> None:
        self.assertEqual(self.result().status, OK)

    def licence_result(self):
        return by_name(self.check(), "Vita3K license links")

    def licence(self) -> Path:
        folder = self.home / "Emulation/roms/psvita/license" / TITLE
        folder.mkdir(parents=True)
        (folder / "EP9000-PCSF00024_00-PDEUAP1648614960.rif").write_bytes(b"R" * 512)
        return folder

    def test_licences_without_a_link_warn_with_the_fix(self) -> None:
        self.licence()  # ux0/license is not there yet: it will be made, so no "start Vita3K"
        result = self.licence_result()
        self.assertEqual(result.status, WARN)
        self.assertIn("license(s) have no link", result.detail)
        self.assertIn(TITLE, result.detail)
        self.assertIn("warpzone links sync deck", result.fix)
        self.assertNotIn("start Vita3K", result.fix)

    def test_linked_licences_are_ok(self) -> None:
        self.licence()
        self.sync()
        result = self.licence_result()
        self.assertEqual(result.status, OK, result.detail)
        self.assertIn("1 license", result.detail)

    def test_no_licences_is_ok(self) -> None:
        self.assertEqual(self.licence_result().status, OK)

    def test_a_console_where_vita3k_never_ran_warns_to_start_it(self) -> None:
        self.licence()
        os.rmdir(self.target)
        os.rmdir(self.target.parent)
        result = self.licence_result()
        self.assertEqual(result.status, WARN)
        self.assertIn("start Vita3K", result.fix)


if __name__ == "__main__":
    unittest.main()
