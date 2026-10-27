import contextlib
import io
import tempfile
import unittest
from pathlib import Path

from support import API_KEY, DECK_ID, HUB_ID, MACHINE_ID, FakeSyncthing
from warpzone import __version__
from warpzone.cli import main


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
            code, _, err = run("provision", "hub", "--config", path)
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


if __name__ == "__main__":
    unittest.main()
