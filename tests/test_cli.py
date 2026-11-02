import contextlib
import io
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from support import API_KEY, DECK_ID, HUB_ID, MACHINE_ID, FakeSyncthing
from warpzone import __version__, doctor, settings_state
from warpzone.cli import _build_parser, main


def run(*argv: str) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        try:
            code = main(list(argv))
        except SystemExit as exc:  # argparse usage errors
            code = int(exc.code or 0)
    return code, out.getvalue(), err.getvalue()


def write_config(directory: str, api_url: str, extra_device: str = "") -> str:
    path = Path(directory) / "config.toml"
    path.write_text(
        f"""
[hub]
name = "nas"
device_id = "{HUB_ID}"
root = "/data/games"
api_url = "{api_url}"
api_key = "{API_KEY}"

[devices.deck]
device_id = "{DECK_ID}"
profile = "steamos-emudeck"
folders = ["library", "tools", "saves", "frontend"]
{extra_device}
"""
    )
    return str(path)


class CliTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = self._tmp.name

    def test_usage_error_is_exit_2(self) -> None:
        self.assertEqual(run()[0], 2)
        self.assertEqual(run("provision")[0], 2)
        self.assertEqual(run("nope")[0], 2)

    def test_version_flag_prints_package_version(self) -> None:
        code, out, _ = run("--version")
        self.assertEqual(code, 0)
        self.assertEqual(out.strip(), f"warpzone {__version__}")

    def test_missing_config_is_exit_2(self) -> None:
        code, _, err = run("check", "--config", str(Path(self.tmp) / "none.toml"))
        self.assertEqual(code, 2)
        self.assertIn("config not found", err)

    def test_invalid_config_is_exit_2(self) -> None:
        path = Path(self.tmp) / "c.toml"
        path.write_text("[hub]\nname = 'x'\n")
        code, _, err = run("check", "--config", str(path))
        self.assertEqual(code, 2)
        self.assertIn("config error", err)

    def test_unknown_device_is_exit_2(self) -> None:
        path = write_config(self.tmp, "http://127.0.0.1:1")
        code, _, err = run("render-ignores", "tablet", "--config", path)
        self.assertEqual(code, 2)
        self.assertIn("unknown device 'tablet'", err)

    def test_check_offline(self) -> None:
        path = write_config(self.tmp, "http://127.0.0.1:1")
        code, out, _ = run("check", "--offline", "--config", path)
        self.assertEqual(code, 0)
        self.assertIn("config ok: 1 device(s)", out)

    def test_render_ignores(self) -> None:
        path = write_config(self.tmp, "http://127.0.0.1:1")
        code, out, _ = run("render-ignores", "deck", "--config", path)
        self.assertEqual(code, 0)
        self.assertIn("# wz-library", out)
        self.assertIn("#include patterns-deck.stignore", out)
        self.assertIn("#   !/patterns-*.stignore", out)
        self.assertIn("(?d)@eaDir", out)
        self.assertIn("/roms/wiiu/mlc01", out)
        self.assertIn("# hub .stignore of the library folder:", out)
        self.assertEqual(out.count("/bios/cemu"), 2)  # device library and hub library
        self.assertNotIn(API_KEY, out)

    def test_unreachable_hub_is_exit_1_without_key(self) -> None:
        path = write_config(self.tmp, "http://127.0.0.1:1")
        code, out, err = run("check", "--config", path)
        self.assertEqual(code, 1)
        self.assertIn("cannot reach", err)
        self.assertNotIn(API_KEY, out + err)

    def test_check_exit_code_follows_missing_whitelists(self) -> None:
        with FakeSyncthing(HUB_ID) as hub:
            path = write_config(self.tmp, hub.url)
            hub.browse["wz-library"] = ["patterns-common.stignore"]
            code, out, _ = run("check", "--config", path)
            self.assertEqual(code, 1)
            self.assertIn("MISSING patterns-deck.stignore", out)
            hub.browse["wz-library"] = ["patterns-common.stignore", "patterns-deck.stignore"]
            self.assertEqual(run("check", "--config", path)[0], 0)
            self.assertEqual(hub.writes(), [])

    def test_provision_hub_dry_run(self) -> None:
        with FakeSyncthing(HUB_ID) as hub:
            path = write_config(self.tmp, hub.url)
            code, out, err = run("provision", "hub", "--dry-run", "--config", path)
            self.assertEqual(code, 0, err)
            self.assertIn("[dry-run] hub: POST /rest/config/folders", out)
            self.assertEqual(hub.writes(), [])
            self.assertNotIn(API_KEY, out + err)

    def test_provision_hub_for_real(self) -> None:
        with FakeSyncthing(HUB_ID) as hub:
            path = write_config(self.tmp, hub.url)
            code, _, err = run("provision", "hub", "--timeout", "0", "--config", path)
            self.assertEqual(code, 0)
            self.assertIn("wz-library", hub.folders)
            self.assertIn("warning: wz-library", err)

    def test_wrong_hub_is_exit_1(self) -> None:
        with FakeSyncthing(MACHINE_ID) as hub:
            path = write_config(self.tmp, hub.url)
            code, _, err = run("provision", "hub", "--config", path)
            self.assertEqual(code, 1)
            self.assertIn("hub.device_id", err)

    def test_device_without_local_syncthing_is_exit_1(self) -> None:
        path = write_config(self.tmp, "http://127.0.0.1:1")
        missing = Path(self.tmp) / "nowhere" / "config.xml"
        code, _, err = run(
            "provision", "device", "deck", "--syncthing-config", str(missing), "--config", path
        )
        self.assertEqual(code, 1)
        self.assertIn("no Syncthing config", err)


class ApplyPathImportTests(unittest.TestCase):
    """The ES-DE startup run must stay fast: it imports no network code (docs/design.md)."""

    def test_settings_apply_loads_no_network_module(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = write_config(tmp, "http://127.0.0.1:1")
            home = Path(tmp) / "home"
            home.mkdir()
            code = (
                "import sys\n"
                f"sys.path.insert(0, {str(Path(__file__).resolve().parent.parent)!r})\n"
                "from warpzone.cli import main\n"
                "argv = ['settings', 'apply', '--device', 'deck', '--quiet']\n"
                f"argv += ['--config', {config!r}]\n"
                "rc = main(argv)\n"
                "net = sorted(m for m in ('ssl', 'socket', 'http.client', 'urllib.request')"
                " if m in sys.modules)\n"
                "print(rc, net)\n"
            )  # fmt: skip
            env = {
                **os.environ,
                "HOME": str(home),
                "XDG_STATE_HOME": str(Path(tmp) / "state"),
            }
            result = subprocess.run(
                [sys.executable, "-I", "-c", code],
                capture_output=True,
                text=True,
                env=env,
                check=False,
            )
        self.assertEqual(result.stdout.strip(), "0 []", result.stderr)


class ProvisionConfigPathTests(unittest.TestCase):
    def test_provision_device_passes_the_config_path_on(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = write_config(tmp, "http://127.0.0.1:1")
            local = mock.Mock(base_url="http://127.0.0.1:1", api_key=API_KEY)
            with (
                mock.patch("warpzone.cli.localst.find_config"),
                mock.patch("warpzone.cli.localst.parse_config", return_value=local),
                mock.patch("warpzone.provision.provision_device") as provision_device,
            ):
                code, _, err = run("provision", "device", "deck", "--config", path)
            self.assertEqual(code, 0, err)
            self.assertEqual(provision_device.call_args.kwargs["config_path"], Path(path))


class SettingsCliTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        self.home = self.tmp / "home"
        self.home.mkdir()
        env = {"HOME": str(self.home), "XDG_STATE_HOME": str(self.tmp / "state")}
        patcher = mock.patch.dict(os.environ, env)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.config = write_config(str(self.tmp), "http://127.0.0.1:1")
        self.folder = self.home / ".local/share/warpzone/settings"

    def subscribe(self) -> None:
        self.folder.mkdir(parents=True)
        (self.folder / ".stfolder").mkdir()

    def test_settings_needs_an_action(self) -> None:
        self.assertEqual(run("settings")[0], 2)
        self.assertEqual(run("settings", "nope")[0], 2)

    def test_apply_without_a_settings_folder_is_exit_0(self) -> None:
        code, out, _ = run("settings", "apply", "--device", "deck", "--config", self.config)
        self.assertEqual(code, 0)
        self.assertIn("no settings folder", out)
        code, out, _ = run(
            "settings", "apply", "--device", "deck", "--quiet", "--config", self.config
        )
        self.assertEqual((code, out), (0, ""))

    def test_a_config_that_cannot_load_leaves_a_trace_for_doctor(self) -> None:
        missing = str(self.tmp / "nowhere.toml")
        code, _, _ = run("settings", "apply", "--device", "deck", "--quiet", "--config", missing)
        self.assertEqual(code, 2)
        info = settings_state.read_last_apply()
        assert info is not None
        self.assertEqual(info["exit"], 2)
        self.assertTrue(info["time"])
        log = (settings_state.state_dir() / "log").read_text()
        self.assertIn("config", log)
        self.assertNotIn(str(self.tmp), log)  # the log never holds paths of the user's files
        status, detail, _ = doctor.judge_last_apply(info, "LOG")
        self.assertEqual(status, doctor.WARN)
        self.assertIn("exit code 2", detail)

    def test_an_invalid_config_leaves_a_trace_too(self) -> None:
        bad = self.tmp / "bad.toml"
        bad.write_text("[hub\n")
        code, _, _ = run("settings", "apply", "--device", "deck", "--quiet", "--config", str(bad))
        self.assertEqual(code, 2)
        info = settings_state.read_last_apply()
        assert info is not None
        self.assertEqual(info["exit"], 2)

    def test_a_dry_run_leaves_no_trace(self) -> None:
        missing = str(self.tmp / "nowhere.toml")
        run("settings", "apply", "--device", "deck", "--dry-run", "--config", missing)
        self.assertIsNone(settings_state.read_last_apply())

    def test_unknown_device_is_exit_2(self) -> None:
        code, _, err = run("settings", "audit", "--device", "tablet", "--config", self.config)
        self.assertEqual(code, 2)
        self.assertIn("unknown device 'tablet'", err)

    def test_unknown_emulator_is_exit_2(self) -> None:
        code, _, err = run(
            "settings", "apply", "--device", "deck", "--emulator", "nope", "--config", self.config
        )
        self.assertEqual(code, 2)
        self.assertIn("unknown emulator 'nope'", err)

    def test_apply_flags_are_accepted(self) -> None:
        argv = ["settings", "apply", "--device", "deck", "--emulator", "eden", "retroarch"]
        args = _build_parser().parse_args(
            [*argv, "--dry-run", "--quiet", "--overwrite-local", "--config", self.config]
        )
        self.assertEqual(args.emulator, ["eden", "retroarch"])
        self.assertTrue(args.dry_run and args.quiet and args.overwrite_local)
        code, _, _ = run(
            "settings", "apply", "--device", "deck", "--dry-run", "--config", self.config
        )
        self.assertEqual(code, 0)

    def test_audit_of_an_empty_console_is_in_sync(self) -> None:
        self.subscribe()
        (self.folder / "common.toml").write_text("format = 1\n")
        code, out, _ = run(
            "settings", "audit", "--device", "deck", "--all", "--config", self.config
        )
        self.assertEqual(code, 0)
        self.assertIn("summary: everything in sync", out)
        self.assertFalse((self.tmp / "state").exists())

    def test_audit_with_an_invalid_layer_is_exit_1(self) -> None:
        self.subscribe()
        (self.folder / "common.toml").write_text("format = 7\n")
        code, out, _ = run("settings", "audit", "--device", "deck", "--config", self.config)
        self.assertEqual(code, 1)
        self.assertIn("common.toml", out)

    def test_device_defaults_to_the_local_syncthing(self) -> None:
        with FakeSyncthing(DECK_ID) as fake:
            conf = self.tmp / "syncthing" / "config.xml"
            conf.parent.mkdir()
            host, _, port = fake.url.removeprefix("http://").partition(":")
            conf.write_text(
                f"<configuration><gui><address>{host}:{port}</address>"
                f"<apikey>{API_KEY}</apikey></gui></configuration>"
            )
            with mock.patch.dict(os.environ, {"STHOMEDIR": str(conf.parent)}):
                code, out, _ = run("settings", "apply", "--config", self.config)
        self.assertEqual(code, 0)
        self.assertIn("no settings folder", out)

    def test_capture_flags_are_accepted(self) -> None:
        argv = [
            "settings", "capture", "--device", "deck", "--emulator", "eden", "--seed",
            "--key", "eden/qt-config/[System]*", "eden/qt-config/[Audio]*", "--key", "a/b/c",
            "--include-new", "--yes", "--to-common", "a/*", "--to-device", "b/*",
            "--discard", "c/*", "--offline", "--dry-run", "--config", self.config,
        ]  # fmt: skip
        args = _build_parser().parse_args(argv)
        self.assertEqual(args.key, ["eden/qt-config/[System]*", "eden/qt-config/[Audio]*", "a/b/c"])
        self.assertTrue(args.seed and args.include_new and args.yes and args.offline)
        self.assertEqual(
            (args.to_common, args.to_device, args.discard), (["a/*"], ["b/*"], ["c/*"])
        )
        self.assertTrue(args.dry_run)

    def test_capture_key_without_seed_is_exit_2(self) -> None:
        code, _, err = run(
            "settings", "capture", "--device", "deck", "--key", "a/b/c", "--config", self.config
        )
        self.assertEqual(code, 2)
        self.assertIn("--seed", err)

    def test_capture_unknown_emulator_is_exit_2(self) -> None:
        code, _, err = run(
            "settings", "capture", "--device", "deck", "--emulator", "nope", "--offline",
            "--config", self.config,
        )  # fmt: skip
        self.assertEqual(code, 2)
        self.assertIn("unknown emulator 'nope'", err)

    def test_capture_without_a_settings_folder_is_exit_1(self) -> None:
        code, _, err = run(
            "settings", "capture", "--device", "deck", "--offline", "--config", self.config
        )
        self.assertEqual(code, 1)
        self.assertIn("no settings folder", err)

    def test_capture_asks_the_local_syncthing_unless_offline(self) -> None:
        self.subscribe()
        (self.folder / "common.toml").write_text("format = 1\n")
        with FakeSyncthing(DECK_ID) as fake:
            conf = self.tmp / "syncthing" / "config.xml"
            conf.parent.mkdir()
            host, _, port = fake.url.removeprefix("http://").partition(":")
            conf.write_text(
                f"<configuration><gui><address>{host}:{port}</address>"
                f"<apikey>{API_KEY}</apikey></gui></configuration>"
            )
            with (
                mock.patch("warpzone.settings.require_proc"),
                mock.patch("warpzone.settings.is_running", return_value=False),
            ):
                code, out, _ = run(
                    "settings", "capture", "--syncthing-config", str(conf), "--config", self.config
                )
                self.assertEqual(code, 0)
                self.assertIn("report only", out)
                asked = [path for _, path, _ in fake.requests]
                self.assertTrue(any("/rest/db/status?folder=wz-settings" in p for p in asked))
                self.assertTrue(any("file=common.toml" in p for p in asked))
                seen = len(fake.requests)
                code, *_ = run(
                    "settings", "capture", "--device", "deck", "--offline", "--config", self.config
                )
                self.assertEqual(code, 0)
                self.assertEqual(len(fake.requests), seen)


if __name__ == "__main__":
    unittest.main()


class SelfUpdateCommandTests(unittest.TestCase):
    def test_runs_on_the_package_checkout_and_needs_no_config(self) -> None:
        with (
            mock.patch("warpzone.selfupdate.run_self_update", return_value=0) as update,
            mock.patch.dict(os.environ, {"WARPZONE_CONFIG": "/nonexistent/config.toml"}),
        ):
            code, _, err = run("self-update")
        self.assertEqual(code, 0, err)
        args, kwargs = update.call_args
        self.assertEqual(args[0], Path(__file__).resolve().parent.parent)
        self.assertEqual(args[1], __version__)
        self.assertFalse(kwargs["dry_run"])
        self.assertEqual(kwargs["log"].name, "self-update.log")

    def test_dry_run_flag_and_exit_code_are_passed_through(self) -> None:
        with mock.patch("warpzone.selfupdate.run_self_update", return_value=1) as update:
            code, _, _ = run("self-update", "--dry-run")
        self.assertEqual(code, 1)
        self.assertTrue(update.call_args.kwargs["dry_run"])

    def test_log_goes_to_the_state_folder(self) -> None:
        with (
            tempfile.TemporaryDirectory() as tmp,
            mock.patch.dict(os.environ, {"XDG_STATE_HOME": tmp}),
            mock.patch("warpzone.selfupdate.run_self_update", return_value=0) as update,
        ):
            run("self-update")
        self.assertEqual(update.call_args.kwargs["log"], Path(tmp) / "warpzone" / "self-update.log")
