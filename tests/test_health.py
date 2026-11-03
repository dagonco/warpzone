"""`warpzone health`: the third step of verifying a version, with a fake /proc and Syncthing."""

import contextlib
import io
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from support import API_KEY, DECK_ID, FakeSyncthing, config_data
from test_anbernic import ARK_ID, ark_config
from test_artwork_device import Art
from warpzone import artwork, esde, health, localst, provision, selfupdate, updatetimer
from warpzone.cli import main
from warpzone.config import Config, ConfigError, parse_config
from warpzone.model import device_specs
from warpzone.profiles import load_profile

STEAM = load_profile("steamos-emudeck")
ANBERNIC = load_profile("anbernic-rg351mp")
DECK_FOLDERS = ("library", "tools", "saves", "frontend", "settings", "play-state")


def deck_config(folders=DECK_FOLDERS, **device) -> Config:
    data = config_data()
    data["devices"] = {
        "deck": {"device_id": DECK_ID, "profile": "steamos-emudeck", "folders": list(folders)}
        | device
    }
    return parse_config(data)


class Clock:
    def __init__(self) -> None:
        self.now = 100.0

    def __call__(self) -> float:
        return self.now


def tree(root: Path) -> dict[str, bytes | None]:
    """Every path under `root` with its bytes (None for a folder), to compare before and after."""
    found: dict[str, bytes | None] = {}
    for path in sorted(root.rglob("*")):
        found[str(path.relative_to(root))] = path.read_bytes() if path.is_file() else None
    return found


class PureTests(unittest.TestCase):
    def test_a_steam_device_runs_the_three_startup_steps_in_order(self) -> None:
        device = deck_config().devices["deck"]
        self.assertEqual(
            health.plan_steps(device, STEAM),
            (health.SETTINGS, health.PLAY_STATE, health.LINKS),
        )

    def test_a_step_is_planned_only_when_its_script_or_command_applies(self) -> None:
        bare = deck_config(("library", "saves")).devices["deck"]
        self.assertEqual(health.plan_steps(bare, STEAM), (health.LINKS,))
        nothing = deck_config(("saves",)).devices["deck"]
        self.assertEqual(health.plan_steps(nothing, STEAM), ())

    def test_an_artwork_device_refreshes_and_does_not_sync_play_state(self) -> None:
        device = ark_config(("library", "frontend", "play-state")).devices["ark"]
        self.assertEqual(health.plan_steps(device, ANBERNIC), (health.REFRESH,))

    def test_the_files_a_steam_device_needs(self) -> None:
        home = Path("/h")
        wanted = {
            (e.kind, e.path)
            for e in health.expected_files(deck_config().devices["deck"], STEAM, home)
        }
        scripts = home / "ES-DE/scripts/startup"
        units = home / ".config/systemd/user"
        self.assertEqual(
            wanted,
            {
                ("script", scripts / esde.SCRIPT_NAME),
                ("script", scripts / esde.PLAY_STATE_SCRIPT),
                ("script", scripts / esde.LINKS_SCRIPT),
                ("service", units / updatetimer.SERVICE),
                ("timer", units / updatetimer.TIMER),
                ("service", units / f"{artwork.PLAY_STATE_UNIT}.service"),
                ("timer", units / f"{artwork.PLAY_STATE_UNIT}.timer"),
            },
        )

    def test_a_device_that_turned_self_update_off_needs_no_update_unit(self) -> None:
        device = deck_config(("saves",), self_update=False).devices["deck"]
        self.assertEqual(health.expected_files(device, STEAM, Path("/h")), [])

    def test_an_artwork_device_needs_its_refresh_timer_and_no_es_de_script(self) -> None:
        device = ark_config(("library", "frontend")).devices["ark"]
        names = {e.path.name for e in health.expected_files(device, ANBERNIC, Path("/h"))}
        self.assertEqual(
            names,
            {
                updatetimer.SERVICE,
                updatetimer.TIMER,
                f"{artwork.UNIT_NAME}.service",
                f"{artwork.UNIT_NAME}.timer",
            },
        )

    def test_a_step_passes_within_the_budget(self) -> None:
        found = health.judge_step("links sync", error=None, seconds=14.9, warnings=0)
        self.assertEqual(found.status, health.OK)

    def test_a_step_that_failed_fails(self) -> None:
        found = health.judge_step("links sync", error="boom", seconds=0.1, warnings=0)
        self.assertEqual(found.status, health.FAIL)
        self.assertIn("boom", found.detail)

    def test_a_step_slower_than_es_de_allows_fails(self) -> None:
        found = health.judge_step("settings apply", error=None, seconds=15.1, warnings=0)
        self.assertEqual(found.status, health.FAIL)
        self.assertIn("15", found.detail)

    def test_the_exit_code_is_zero_only_without_a_failure(self) -> None:
        ok = health.Finding("a", health.OK, "")
        skip = health.Finding("b", health.SKIP, "")
        bad = health.Finding("c", health.FAIL, "")
        self.assertEqual(health.exit_code([ok, skip]), 0)
        self.assertEqual(health.exit_code([ok, bad]), 1)

    def judge(self, **kw):
        base = {
            "text": "#!/bin/sh\n",
            "executable": True,
            "program": "/usr/bin/python3",
            "program_ok": True,
            "imports": True,
        }
        item = health.Expected("script", "x.sh", Path("/h/x.sh"))
        return health.judge_file(item, **(base | kw))

    def test_a_script_with_a_working_interpreter_passes(self) -> None:
        self.assertEqual(self.judge().status, health.OK)

    def test_a_missing_script_fails(self) -> None:
        self.assertEqual(self.judge(text=None).status, health.FAIL)

    def test_a_script_that_is_not_executable_fails(self) -> None:
        self.assertEqual(self.judge(executable=False).status, health.FAIL)

    def test_a_script_that_runs_nothing_known_fails(self) -> None:
        self.assertEqual(self.judge(program=None).status, health.FAIL)

    def test_an_interpreter_that_is_gone_fails(self) -> None:
        self.assertEqual(self.judge(program_ok=False).status, health.FAIL)

    def test_an_interpreter_that_cannot_import_warpzone_fails(self) -> None:
        self.assertEqual(self.judge(imports=False).status, health.FAIL)

    def test_a_timer_only_has_to_be_there(self) -> None:
        item = health.Expected("timer", "timer unit", Path("/h/x.timer"))
        found = health.judge_file(
            item, text="[Timer]\n", executable=False, program=None, program_ok=False, imports=False
        )
        self.assertEqual(found.status, health.OK)

    def test_a_service_need_not_be_executable(self) -> None:
        item = health.Expected("service", "service unit", Path("/h/x.service"))
        found = health.judge_file(
            item,
            text="[Service]\n",
            executable=False,
            program="/bin/sh",
            program_ok=True,
            imports=True,
        )
        self.assertEqual(found.status, health.OK)


class ExecCommandTests(unittest.TestCase):
    def test_the_words_of_an_exec_start_line(self) -> None:
        text = "[Service]\nType=oneshot\nExecStart=/usr/bin/python3 -m warpzone self-update\n"
        self.assertEqual(
            updatetimer.exec_command(text),
            ["/usr/bin/python3", "-m", "warpzone", "self-update"],
        )

    def test_a_quoted_word_keeps_its_spaces_and_systemd_escapes_are_undone(self) -> None:
        script = "/home/a b/updater/up%dater$.sh"
        text = updatetimer.render_service(script)
        self.assertEqual(updatetimer.exec_command(text), ["/bin/sh", script, "tick"])

    def test_the_units_warpzone_writes_round_trip(self) -> None:
        service, _ = artwork.render_play_state_units(
            "/v/bin/python", "deck", Path("/c d.toml"), "5min"
        )
        self.assertEqual(
            updatetimer.exec_command(service),
            [
                "/v/bin/python",
                "-m",
                "warpzone",
                "play-state",
                "sync",
                "deck",
                "--quiet",
                "--config",
                "/c d.toml",
            ],
        )

    def test_no_exec_start_is_none(self) -> None:
        self.assertIsNone(updatetimer.exec_command("[Timer]\nOnBootSec=5min\n"))


class Base:
    """A Steam device in a temporary home, a fake /proc and the files provision writes."""

    def make_home(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        tmp = Path(self._tmp.name)
        self.home = tmp / "home"
        self.state = tmp / "state" / "settings"
        self.proc = tmp / "proc"
        self.proc.mkdir()
        self.home.mkdir()
        self.python = sys.executable
        self.clock = Clock()
        self.manager = True
        self.imports = True
        self.local: localst.LocalSyncthing | None = None
        self.checked: list[str] = []

    def systemctl(self, command):
        return (0 if self.manager else 1), ""

    def import_check(self, python: str) -> bool:
        self.checked.append(python)
        return self.imports

    def probes(self) -> health.Probes:
        return health.Probes(
            home=self.home,
            state=self.state,
            proc=self.proc,
            local=lambda: self.local,
            import_check=self.import_check,
            systemctl=self.systemctl,
            clock=self.clock,
        )

    def health(self, name: str | None, cfg: Config, profiles) -> tuple[int, list[str]]:
        lines: list[str] = []
        code = health.run_health(lambda: (cfg, profiles), name, self.probes(), lines.append)
        return code, lines

    def status(self, lines: list[str], text: str) -> str:
        (line,) = [x for x in lines if text in x]
        return line.split()[0]


class DeckBase(Base):
    def setUp(self) -> None:
        self.make_home()
        self.cfg = deck_config()
        self.profiles = {"steamos-emudeck": STEAM}
        self.install()

    def install(self, *, scripts=True, units=True) -> None:
        esde_dir = self.home / "ES-DE"
        if scripts:
            for name, text in (
                (esde.SCRIPT_NAME, esde.render_startup_script(self.python, "deck")),
                (esde.PLAY_STATE_SCRIPT, esde.render_play_state_script(self.python, "deck")),
                (esde.LINKS_SCRIPT, esde.render_links_script(self.python, "deck")),
            ):
                provision._write_script(esde.script_path(esde_dir, name), text)
        if units:
            folder = self.home / updatetimer.UNIT_DIR
            live = selfupdate.live_path(self.home)
            provision._write_script(live, "UPDATER_ABI=1\n")
            service, timer = artwork.render_play_state_units(self.python, "deck", None, "15min")
            for name, text in (
                (updatetimer.SERVICE, updatetimer.render_service(str(live))),
                (updatetimer.TIMER, updatetimer.render_timer()),
                (f"{artwork.PLAY_STATE_UNIT}.service", service),
                (f"{artwork.PLAY_STATE_UNIT}.timer", timer),
            ):
                provision._write_script(folder / name, text, 0o644)


class DeckTests(DeckBase, unittest.TestCase):
    def test_a_working_install_passes(self) -> None:
        code, lines = self.health("deck", self.cfg, self.profiles)
        self.assertEqual(code, 0, "\n".join(lines))
        for step in ("settings apply", "play-state sync", "links sync"):
            self.assertEqual(self.status(lines, step), health.OK)
        self.assertIn("health: ok", lines[-1])

    def test_it_writes_nothing_under_the_home_or_the_state_folder(self) -> None:
        # Folders a run would write to: the play state ledger, the links, the settings files.
        play = self.home / ".local/share/warpzone/play-state"
        (play / ".stfolder").mkdir(parents=True)
        gamelist = self.home / "ES-DE/gamelists/gba/gamelist.xml"
        gamelist.parent.mkdir(parents=True)
        gamelist.write_text(
            "<gameList><game><path>./A.zip</path><playcount>2</playcount></game></gameList>\n",
            encoding="utf-8",
        )
        (self.home / "Emulation/roms/gba").mkdir(parents=True)
        (self.home / "Emulation/roms/gba/A.zip").write_bytes(b"rom")
        (self.home / "Emulation/roms/psvita/app/PCSA00001/sce_sys").mkdir(parents=True)
        for rel in ("eboot.bin", "sce_sys/param.sfo"):
            (self.home / "Emulation/roms/psvita/app/PCSA00001" / rel).write_bytes(b"x")
        (self.home / "Emulation/storage/Vita3K/ux0/app").mkdir(parents=True)
        settings = self.home / ".local/share/warpzone/settings"
        (settings / ".stfolder").mkdir(parents=True)
        before = tree(self.home)
        code, lines = self.health("deck", self.cfg, self.profiles)
        self.assertEqual(code, 0, "\n".join(lines))
        self.assertEqual(tree(self.home), before)
        self.assertFalse(self.state.parent.exists())

    def test_the_play_state_step_takes_no_lock_file(self) -> None:
        (self.home / ".local/share/warpzone/play-state/.stfolder").mkdir(parents=True)
        self.health("deck", self.cfg, self.profiles)
        self.assertEqual(list((self.home / ".local/share/warpzone").rglob("lock")), [])
        self.assertEqual(list(self.state.parent.rglob("lock")), [])

    def test_a_missing_startup_script_fails(self) -> None:
        os.remove(esde.script_path(self.home / "ES-DE", esde.LINKS_SCRIPT))
        code, lines = self.health("deck", self.cfg, self.profiles)
        self.assertEqual(code, 1)
        self.assertEqual(self.status(lines, esde.LINKS_SCRIPT), health.FAIL)
        self.assertEqual(self.status(lines, esde.SCRIPT_NAME), health.OK)

    def test_a_script_that_cannot_be_executed_fails(self) -> None:
        esde.script_path(self.home / "ES-DE", esde.SCRIPT_NAME).chmod(0o644)
        code, lines = self.health("deck", self.cfg, self.profiles)
        self.assertEqual(code, 1)
        self.assertEqual(self.status(lines, esde.SCRIPT_NAME), health.FAIL)

    def test_a_script_whose_interpreter_is_gone_fails(self) -> None:
        text = esde.render_links_script(str(self.home / "gone/bin/python"), "deck")
        provision._write_script(esde.script_path(self.home / "ES-DE", esde.LINKS_SCRIPT), text)
        code, lines = self.health("deck", self.cfg, self.profiles)
        self.assertEqual(code, 1)
        self.assertEqual(self.status(lines, esde.LINKS_SCRIPT), health.FAIL)

    def test_an_interpreter_that_cannot_import_warpzone_fails_everywhere_it_is_used(self) -> None:
        self.imports = False
        code, lines = self.health("deck", self.cfg, self.profiles)
        self.assertEqual(code, 1)
        failed = [x for x in lines if x.split()[0] == health.FAIL]
        # 3 scripts and the play-state service; timers run nothing, and the self-update
        # service runs `sh`, not python
        self.assertEqual(len(failed), 4)
        self.assertEqual(set(self.checked), {self.python})
        self.assertEqual(len(self.checked), 1, "one import check per interpreter")

    def test_a_missing_unit_fails(self) -> None:
        os.remove(self.home / updatetimer.UNIT_DIR / updatetimer.TIMER)
        code, lines = self.health("deck", self.cfg, self.profiles)
        self.assertEqual(code, 1)
        self.assertEqual(self.status(lines, updatetimer.TIMER), health.FAIL)

    def test_a_unit_that_runs_a_missing_program_fails(self) -> None:
        gone = str(self.home / "gone/bin/python")
        provision._write_script(
            self.home / updatetimer.UNIT_DIR / f"{artwork.PLAY_STATE_UNIT}.service",
            f"[Service]\nExecStart={gone} -m warpzone play-state sync deck\n",
            0o644,
        )
        code, lines = self.health("deck", self.cfg, self.profiles)
        self.assertEqual(code, 1)
        self.assertEqual(self.status(lines, f"{artwork.PLAY_STATE_UNIT}.service"), health.FAIL)

    def test_a_self_update_unit_whose_script_is_gone_fails(self) -> None:
        os.remove(selfupdate.live_path(self.home))
        code, lines = self.health("deck", self.cfg, self.profiles)
        self.assertEqual(code, 1)
        self.assertEqual(self.status(lines, updatetimer.SERVICE), health.FAIL)
        (line,) = [x for x in lines if updatetimer.SERVICE in x]
        self.assertIn("updater.sh", line)

    def test_a_self_update_unit_that_runs_the_script_with_sh_passes(self) -> None:
        code, lines = self.health("deck", self.cfg, self.profiles)
        self.assertEqual(code, 0, "\n".join(lines))
        self.assertEqual(self.status(lines, updatetimer.SERVICE), health.OK)

    def test_without_a_systemd_user_manager_the_units_are_not_required(self) -> None:
        self.manager = False
        for path in (self.home / updatetimer.UNIT_DIR).iterdir():
            path.unlink()
        code, lines = self.health("deck", self.cfg, self.profiles)
        self.assertEqual(code, 0, "\n".join(lines))
        self.assertEqual(self.status(lines, updatetimer.TIMER), health.SKIP)

    def test_a_step_that_raises_fails_and_the_others_still_run(self) -> None:
        with mock.patch("warpzone.titlelinks.sync", side_effect=RuntimeError("broken")):
            code, lines = self.health("deck", self.cfg, self.profiles)
        self.assertEqual(code, 1)
        self.assertEqual(self.status(lines, "links sync"), health.FAIL)
        self.assertIn("broken", next(x for x in lines if "links sync" in x))
        self.assertEqual(self.status(lines, "settings apply"), health.OK)

    def test_a_settings_apply_that_exits_nonzero_fails(self) -> None:
        with mock.patch("warpzone.settings.apply", return_value=1):
            code, lines = self.health("deck", self.cfg, self.profiles)
        self.assertEqual(code, 1)
        self.assertEqual(self.status(lines, "settings apply"), health.FAIL)

    def test_a_step_slower_than_es_de_allows_fails(self) -> None:
        def slow(*args, **kw):
            self.clock.now += 16
            return 0

        with mock.patch("warpzone.titlelinks.sync", side_effect=slow):
            code, lines = self.health("deck", self.cfg, self.profiles)
        self.assertEqual(code, 1)
        self.assertEqual(self.status(lines, "links sync"), health.FAIL)

    def test_the_dry_run_steps_are_dry(self) -> None:
        with (
            mock.patch("warpzone.settings.apply", return_value=0) as apply,
            mock.patch("warpzone.playstate_device.sync_shared") as sync,
            mock.patch("warpzone.titlelinks.sync", return_value=0) as links,
        ):
            self.health("deck", self.cfg, self.profiles)
        self.assertTrue(apply.call_args.kwargs["dry_run"])
        self.assertTrue(sync.call_args.kwargs["dry_run"])
        self.assertTrue(links.call_args.kwargs["dry_run"])

    def test_a_config_that_does_not_load_fails_before_any_step(self) -> None:
        def broken():
            raise ConfigError("bad config")

        lines: list[str] = []
        with mock.patch("warpzone.settings.apply") as apply:
            code = health.run_health(broken, "deck", self.probes(), lines.append)
        self.assertEqual(code, 1)
        apply.assert_not_called()
        self.assertEqual(self.status(lines, "config"), health.FAIL)

    def test_an_unknown_device_fails(self) -> None:
        code, lines = self.health("tablet", self.cfg, self.profiles)
        self.assertEqual(code, 1)
        self.assertIn("tablet", "\n".join(lines))

    def test_without_a_name_and_without_local_syncthing_it_cannot_tell_the_device(self) -> None:
        code, lines = self.health(None, self.cfg, self.profiles)
        self.assertEqual(code, 1)
        self.assertIn("pass its name", "\n".join(lines))

    def test_without_a_name_the_device_is_the_one_the_local_syncthing_is(self) -> None:
        with FakeSyncthing(DECK_ID) as local:
            self.local = localst.LocalSyncthing(Path("/x/config.xml"), local.url, API_KEY)
            code, lines = self.health(None, self.cfg, self.profiles)
            self.assertEqual(local.writes(), [])
        self.assertEqual(code, 0, "\n".join(lines))

    def test_the_api_key_never_reaches_the_output(self) -> None:
        with FakeSyncthing(DECK_ID) as local:
            self.local = localst.LocalSyncthing(Path("/x/config.xml"), local.url, API_KEY)
            local.api_key = "other"  # every request is refused, and the error is printed
            code, lines = self.health(None, self.cfg, self.profiles)
        self.assertEqual(code, 1)
        self.assertNotIn(API_KEY, "\n".join(lines))


class RefreshTests(Art, Base, unittest.TestCase):
    """A device whose frontend has artwork: `frontend refresh` as a dry run."""

    def setUp(self) -> None:
        super().setUp()
        self.make_home_over_art()

    def make_home_over_art(self) -> None:
        # Art made the home, the fake /proc and the profile; Base adds the rest.
        self.clock = Clock()
        self.manager = False  # no units installed here: they are not what these tests are about
        self.imports = True
        self.local = None
        self.checked = []
        self.cfg = ark_config(("library", "frontend"))
        self.profiles = {"anbernic-rg351mp": self.profile}
        self.state = Path(self._tmp.name) / "state/settings"

    def test_the_gamelists_are_planned_and_not_written(self) -> None:
        code, lines = self.health("ark", self.cfg, self.profiles)
        self.assertEqual(code, 0, "\n".join(lines))
        self.assertEqual(self.status(lines, "frontend refresh"), health.OK)
        self.assertFalse(self.out("gba").exists())

    def test_the_scope_is_checked_against_the_local_syncthing_and_never_changed(self) -> None:
        with FakeSyncthing(ARK_ID) as local:
            initial = artwork.library_systems(self.lib / "roms")
            for spec in device_specs(self.cfg, "ark", self.profile, initial):
                if spec.id in ("wz-media", "wz-esde"):
                    local.folders[spec.id] = {"id": spec.id, **spec.body()}
                    local.ignores[spec.id] = ["/stale"]
            self.local = localst.LocalSyncthing(Path("/x/config.xml"), local.url, API_KEY)
            _, lines = self.health("ark", self.cfg, self.profiles)
            self.assertEqual(local.writes(), [])
            self.assertEqual(local.ignores["wz-media"], ["/stale"])
        self.assertEqual(self.status(lines, "frontend refresh"), health.OK, "\n".join(lines))

    def test_a_local_syncthing_that_cannot_be_asked_fails_the_step(self) -> None:
        self.local = localst.LocalSyncthing(Path("/x/config.xml"), "http://127.0.0.1:1", API_KEY)
        _, lines = self.health("ark", self.cfg, self.profiles)
        self.assertEqual(self.status(lines, "frontend refresh"), health.FAIL)


class CliTests(unittest.TestCase):
    def test_the_command_hands_the_name_and_the_config_to_health(self) -> None:
        out = io.StringIO()
        with (
            mock.patch("warpzone.health.run_health", return_value=7) as run,
            contextlib.redirect_stdout(out),
        ):
            code = main(["health", "deck", "--config", "/nowhere.toml"])
        self.assertEqual(code, 7)
        self.assertEqual(run.call_args.args[1], "deck")

    def test_the_name_is_optional(self) -> None:
        with mock.patch("warpzone.health.run_health", return_value=0) as run:
            main(["health"])
        self.assertIsNone(run.call_args.args[1])

    def test_a_config_that_is_not_there_is_a_failed_check_not_a_traceback(self) -> None:
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = main(["health", "deck", "--config", "/nowhere/config.toml"])
        self.assertEqual(code, 1)
        self.assertIn("config not found", out.getvalue())

    def test_a_valid_config_reaches_the_steps(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.toml"
            path.write_text(
                "[hub]\nname='nas'\ndevice_id='AAAAAAA-AAAAAAA-AAAAAAA-AAAAAAA-AAAAAAA-AAAAAAA"
                "-AAAAAAA-AAAAAAA'\nroot='/data'\napi_url='http://127.0.0.1:1'\napi_key='k'\n"
                f"[devices.deck]\ndevice_id='{DECK_ID}'\nprofile='steamos-emudeck'\n"
                "folders=['saves']\nself_update=false\n"
            )
            out = io.StringIO()
            with (
                mock.patch("pathlib.Path.home", return_value=Path(tmp) / "home"),
                contextlib.redirect_stdout(out),
            ):
                code = main(["health", "deck", "--config", str(path)])
        self.assertEqual(code, 0, out.getvalue())
        self.assertIn("health: ok", out.getvalue())


if __name__ == "__main__":
    unittest.main()
