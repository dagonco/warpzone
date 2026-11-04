"""shadPS4's install directories: pure edits, the profile table, provisioning and doctor."""

import dataclasses
import json
import os
import shutil
import tempfile
import tomllib
import unittest
from pathlib import Path
from unittest import mock

from support import API_KEY, DECK_ID, HUB_ID, FakeProc, FakeSyncthing, config_data, stub_self_update
from test_doctor import by_name, context
from test_provision import reporter
from warpzone import settings_state, shadps4
from warpzone.config import parse_config
from warpzone.doctor import OK, WARN, Context, check_frontend
from warpzone.profiles import ProfileError, load_profile, parse_profile
from warpzone.provision import provision_device
from warpzone.syncthing import Client

setUpModule = stub_self_update


PROFILE = load_profile("steamos-emudeck")
GAMES = "/home/deck/Emulation/roms/ps4/games"
OWN = "/home/deck/Emulation/storage/shadps4/games"
JSON_TEXT = json.dumps(
    {
        "General": {
            "font_dir": "",
            "install_dirs": [{"enabled": True, "path": OWN}],
            "volume_slider": 100,
            "trophy_notification_duration": 6.0,
        },
        "GPU": {"window_height": 720},
    },
    indent=2,
    ensure_ascii=False,
)
TOML_TEXT = (
    '[General]\nlogType = "async"\n\n'
    "[GUI]\n"
    "gameTableMode = 0\n"
    'addonInstallDir = "/home/deck/Emulation/storage/shadps4/dlc"\n'
    f'installDirs = ["{OWN}"]\n'
    "theme = 0\n\n"
    "[Settings]\nconsoleLanguage = 1\n"
)


class JsonTests(unittest.TestCase):
    def test_the_missing_directory_is_added_after_the_existing_ones(self) -> None:
        self.assertEqual(shadps4.missing("json", JSON_TEXT, [GAMES]), [GAMES])
        new = shadps4.apply("json", JSON_TEXT, [GAMES])
        dirs = json.loads(new)["General"]["install_dirs"]
        self.assertEqual(dirs, [{"enabled": True, "path": OWN}, {"enabled": True, "path": GAMES}])
        self.assertEqual(shadps4.missing("json", new, [GAMES]), [])

    def test_everything_else_is_kept_byte_for_byte(self) -> None:
        new = shadps4.apply("json", JSON_TEXT, [GAMES])
        expected = JSON_TEXT.replace(
            f'"path": "{OWN}"\n      }}\n',
            f'"path": "{OWN}"\n      }},\n      {{\n        "enabled": true,\n'
            f'        "path": "{GAMES}"\n      }}\n',
        )
        self.assertEqual(new, expected)
        self.assertIn('"trophy_notification_duration": 6.0', new)

    def test_a_trailing_newline_is_kept_or_absent_as_it_was(self) -> None:
        self.assertFalse(shadps4.apply("json", JSON_TEXT, [GAMES]).endswith("\n"))
        self.assertTrue(shadps4.apply("json", JSON_TEXT + "\n", [GAMES]).endswith("\n"))

    def test_a_present_entry_changes_nothing(self) -> None:
        once = shadps4.apply("json", JSON_TEXT, [GAMES])
        self.assertEqual(shadps4.apply("json", once, [GAMES]), once)
        self.assertEqual(shadps4.missing("json", once, [GAMES + "/"]), [])

    def test_a_disabled_entry_is_enabled(self) -> None:
        data = json.loads(JSON_TEXT)
        data["General"]["install_dirs"].append({"enabled": False, "path": GAMES})
        text = json.dumps(data, indent=2, ensure_ascii=False)
        self.assertEqual(shadps4.missing("json", text, [GAMES]), [GAMES])
        new = json.loads(shadps4.apply("json", text, [GAMES]))
        self.assertEqual(
            new["General"]["install_dirs"],
            [{"enabled": True, "path": OWN}, {"enabled": True, "path": GAMES}],
        )

    def test_a_config_without_the_list_gets_one(self) -> None:
        text = json.dumps({"General": {"font_dir": ""}}, indent=2)
        new = json.loads(shadps4.apply("json", text, [GAMES]))
        self.assertEqual(new["General"]["install_dirs"], [{"enabled": True, "path": GAMES}])

    def test_a_file_it_does_not_understand_is_refused(self) -> None:
        for text in ("not json", "[]", '{"General": 3}', '{"Other": {}}', "{}"):
            with self.assertRaises(shadps4.Shadps4ConfigError, msg=text):
                shadps4.missing("json", text, [GAMES])
        with self.assertRaises(shadps4.Shadps4ConfigError):
            shadps4.missing("json", '{"General": {"install_dirs": [3]}}', [GAMES])

    def test_a_layout_that_would_be_rewritten_is_refused(self) -> None:
        compact = json.dumps(json.loads(JSON_TEXT))
        self.assertEqual(shadps4.missing("json", compact, [GAMES]), [GAMES])
        with self.assertRaisesRegex(shadps4.Shadps4ConfigError, "layout"):
            shadps4.apply("json", compact, [GAMES])


class TomlTests(unittest.TestCase):
    def installs(self, text: str) -> list[str]:
        return tomllib.loads(text)["GUI"]["installDirs"]

    def test_the_directory_is_appended_to_the_line_and_the_rest_is_kept(self) -> None:
        self.assertEqual(shadps4.missing("toml", TOML_TEXT, [GAMES]), [GAMES])
        new = shadps4.apply("toml", TOML_TEXT, [GAMES])
        self.assertEqual(new, TOML_TEXT.replace(f'["{OWN}"]', f'["{OWN}", "{GAMES}"]'))
        self.assertEqual(shadps4.missing("toml", new, [GAMES]), [])
        self.assertEqual(shadps4.apply("toml", new, [GAMES]), new)

    def test_a_multi_line_array_with_comments_is_replaced(self) -> None:
        text = TOML_TEXT.replace(
            f'installDirs = ["{OWN}"]',
            f'installDirs = [\n  "{OWN}", # mine\n  "/a]b",\n]  # end',
        )
        new = shadps4.apply("toml", text, [GAMES])
        self.assertEqual(self.installs(new), [OWN, "/a]b", GAMES])
        self.assertIn("theme = 0", new)
        self.assertTrue(new.endswith("consoleLanguage = 1\n"))

    def test_a_missing_key_is_added_to_the_gui_table(self) -> None:
        text = TOML_TEXT.replace(f'installDirs = ["{OWN}"]\n', "")
        new = shadps4.apply("toml", text, [GAMES])
        self.assertEqual(self.installs(new), [GAMES])
        self.assertEqual(
            {k: v for k, v in tomllib.loads(new)["GUI"].items() if k != "installDirs"},
            tomllib.loads(text)["GUI"],
        )

    def test_a_missing_gui_table_is_added_at_the_end(self) -> None:
        text = '[General]\nlogType = "async"\n'
        new = shadps4.apply("toml", text, [GAMES])
        self.assertEqual(new, text + f'\n[GUI]\ninstallDirs = ["{GAMES}"]\n')

    def test_crlf_files_keep_their_line_ends(self) -> None:
        text = TOML_TEXT.replace("\n", "\r\n")
        new = shadps4.apply("toml", text, [GAMES])
        self.assertNotIn("\n", new.replace("\r\n", ""))
        self.assertEqual(self.installs(new), [OWN, GAMES])

    def test_a_file_it_does_not_understand_is_refused(self) -> None:
        for text in ("[GUI\ninstallDirs =", '[GUI]\ninstallDirs = "x"\n'):
            with self.assertRaises(shadps4.Shadps4ConfigError, msg=text):
                shadps4.missing("toml", text, [GAMES])

    def test_similar_keys_and_sub_tables_are_untouched(self) -> None:
        text = '[GUI]\ninstallDirs = ["a"]\ninstallDirs2 = 1\n[GUI.sub]\nx = 1\n'
        new = shadps4.apply("toml", text, [GAMES])
        self.assertEqual(self.installs(new), ["a", GAMES])
        self.assertEqual(new, text.replace('["a"]', f'["a", "{GAMES}"]'))


class ProfileTests(unittest.TestCase):
    def test_the_shipped_profile_declares_the_install_directory(self) -> None:
        rule = PROFILE.shadps4
        assert rule is not None
        self.assertEqual(rule.install_dirs, ("{library}/roms/ps4/games",))
        self.assertEqual(rule.wants("/home/deck/Emulation"), [GAMES])
        self.assertIn("~/.local/share/shadPS4/config.json", rule.config)
        self.assertIn("~/.local/share/shadPS4/config.toml", rule.config)
        for name in ("shadps4", "shadPS4QtLauncher"):
            self.assertIn(name, rule.process)

    def parse(self, **table):
        base = {"name": "x", "paths": {"library": "~/lib"}, "shadps4": table}
        return parse_profile(base, "t")

    def test_a_valid_table(self) -> None:
        rule = self.parse(
            config=["~/a.json", "~/b.toml"], process=["p"], install_dirs=["{library}/g"]
        ).shadps4
        assert rule is not None
        self.assertEqual(rule.config, ("~/a.json", "~/b.toml"))

    def test_the_table_is_checked(self) -> None:
        good = {"config": ["~/a.json"], "process": ["p"], "install_dirs": ["{library}/g"]}
        for change, message in (
            ({"config": ["~/a.ini"]}, "shadps4.config"),
            ({"config": []}, "shadps4.config"),
            ({"process": []}, "shadps4.process"),
            ({"install_dirs": []}, "shadps4.install_dirs"),
            ({"install_dirs": ["{nope}/g"]}, "shadps4.install_dirs"),
            ({"install_dirs": ["rel/dir"]}, "shadps4.install_dirs"),
            ({"extra": 1}, "unknown key"),
        ):
            with self.assertRaisesRegex(ProfileError, message):
                self.parse(**{**good, **change})
        with self.assertRaisesRegex(ProfileError, "shadps4.install_dirs"):
            parse_profile({"name": "x", "shadps4": {**good, "install_dirs": ["{library}/g"]}}, "t")

    def test_the_other_shipped_profile_has_none(self) -> None:
        self.assertIsNone(load_profile("anbernic-rg351mp").shadps4)


class Device:
    """A temporary home with shadPS4's config files, and a way to run `provision device`."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        tmp = Path(self._tmp.name)
        self.home = tmp / "home"
        self.state = tmp / "state"
        self.proc = FakeProc(tmp / "proc")
        self.folder = self.home / ".local/share/shadPS4"
        self.folder.mkdir(parents=True)
        self.json = self.folder / "config.json"
        self.toml = self.folder / "config.toml"
        self.json.write_text(JSON_TEXT.replace(OWN, f"{self.home}/Emulation/storage/shadps4/games"))
        self.toml.write_text(TOML_TEXT.replace(OWN, f"{self.home}/Emulation/storage/shadps4/games"))
        self.games = f"{self.home}/Emulation/roms/ps4/games"
        # the rest of the profile needs files this test does not make
        self.profile = dataclasses.replace(PROFILE, retroarch=None, es_systems=None, artwork=None)

    def start(self, comm: str) -> None:
        self.proc.add(4242, comm)

    def after_backup(self, action):
        """Run `action(file)` right after the backup of the first file: the race the re-check
        right before the write is there for."""
        real = settings_state.backup
        done: list[Path] = []

        def backup(path, *args, **kw):
            result = real(path, *args, **kw)
            if not done:
                done.append(path)
                action(path)
            return result

        return mock.patch.object(settings_state, "backup", backup)

    @staticmethod
    def mine(rep) -> list[str]:
        """The warnings about shadPS4's config files (a Steam device warns about much else)."""
        return [w for w in rep.warnings if "config file" in w]

    def backups(self) -> list[Path]:
        folder = self.state / "backups" / "shadps4"
        return sorted(p for p in folder.rglob("*") if p.is_file()) if folder.is_dir() else []

    def provision(self, *, dry_run=False, **kw):
        cfg = parse_config(config_data())
        rep, lines = reporter()
        kw.setdefault("proc", self.proc.path)
        with FakeSyncthing(DECK_ID) as local:
            for name in ("patterns-common.stignore", "patterns-deck.stignore"):
                local.arrives_after[("wz-library", name)] = 0
            client = Client(local.url, API_KEY, label="local", dry_run=dry_run, sink=lines.append)
            provision_device(
                cfg,
                "deck",
                self.profile,
                client,
                None,
                rep,
                home=self.home,
                wait_timeout=0.0,
                poll=1.0,
                sleep=lambda s: None,
                state_root=self.state,
                **kw,
            )
        return rep, lines


class ProvisionTests(Device, unittest.TestCase):
    def test_both_config_files_get_the_directory_and_keep_their_own(self) -> None:
        rep, lines = self.provision()
        self.assertEqual(self.mine(rep), [])
        dirs = json.loads(self.json.read_text())["General"]["install_dirs"]
        self.assertEqual([d["path"] for d in dirs][1:], [self.games])
        self.assertEqual(len(dirs), 2)
        self.assertEqual(
            tomllib.loads(self.toml.read_text())["GUI"]["installDirs"][1:], [self.games]
        )
        self.assertTrue(any("shadps4" in line for line in lines))

    def test_the_old_files_are_backed_up_before_the_edit(self) -> None:
        old_json, old_toml = self.json.read_text(), self.toml.read_text()
        self.provision()
        texts = sorted(p.read_text() for p in self.backups())
        self.assertEqual(texts, sorted([old_json, old_toml]))

    def test_a_second_run_changes_and_backs_up_nothing(self) -> None:
        self.provision()
        stamps = [p.stat().st_mtime_ns for p in (self.json, self.toml)]
        backups = self.backups()
        rep, lines = self.provision()
        self.assertEqual([p.stat().st_mtime_ns for p in (self.json, self.toml)], stamps)
        self.assertEqual(self.backups(), backups)
        self.assertEqual(self.mine(rep), [])
        self.assertTrue(any("already" in line for line in lines))

    def test_nothing_is_edited_while_shadps4_runs(self) -> None:
        for comm in ("shadps4", "shadPS4QtLaunch"):
            with self.subTest(comm=comm):
                self.start(comm)
                before = self.json.read_text(), self.toml.read_text()
                rep, _ = self.provision()
                self.assertEqual((self.json.read_text(), self.toml.read_text()), before)
                (warning,) = [w for w in rep.warnings if "is running" in w and "shadPS4" in w]
                self.assertIn("provision device deck", warning)
                self.assertEqual(self.backups(), [])
                shutil.rmtree(self.proc.path / "4242")

    def test_shadps4_started_during_the_backup_stops_the_edits(self) -> None:
        before = self.json.read_text(), self.toml.read_text()
        with self.after_backup(lambda path: self.start("shadps4")):
            rep, _ = self.provision()
        self.assertEqual((self.json.read_text(), self.toml.read_text()), before)
        self.assertEqual(len([w for w in rep.warnings if "is running" in w]), 1)

    def test_a_config_edited_during_the_backup_is_left_alone_and_the_next_goes_on(self) -> None:
        edited: list[Path] = []

        def edit(path: Path) -> None:
            edited.append(path)
            path.write_text(path.read_text() + "\n")

        with self.after_backup(edit):
            rep, _ = self.provision()
        (first,) = edited
        (other,) = {self.json, self.toml} - {first}
        self.assertNotIn(self.games, first.read_text())
        self.assertIn(self.games, other.read_text())
        (warning,) = [w for w in rep.warnings if "changed while" in w]
        self.assertIn(str(first), warning)

    def test_an_edit_that_fails_after_the_check_leaves_the_file_alone(self) -> None:
        before = self.json.read_text(), self.toml.read_text()
        error = shadps4.Shadps4ConfigError("cannot add it")
        with mock.patch.object(shadps4, "apply", side_effect=error):
            rep, _ = self.provision()
        self.assertEqual((self.json.read_text(), self.toml.read_text()), before)
        self.assertEqual(len([w for w in rep.warnings if "was left alone: cannot add" in w]), 2)

    def test_without_proc_nothing_is_edited_and_it_says_why(self) -> None:
        before = self.json.read_text()
        rep, _ = self.provision(proc=self.proc.path / "missing")
        self.assertEqual(self.json.read_text(), before)
        self.assertTrue(any("/proc" in w and "shadPS4" in w for w in rep.warnings))

    def test_dry_run_writes_nothing(self) -> None:
        before = self.json.read_text(), self.toml.read_text()
        _rep, lines = self.provision(dry_run=True)
        self.assertEqual((self.json.read_text(), self.toml.read_text()), before)
        self.assertEqual(self.backups(), [])
        self.assertTrue(any("would" in line and "install" in line for line in lines))

    def test_only_the_file_that_exists_is_edited(self) -> None:
        self.toml.unlink()
        rep, _ = self.provision()
        self.assertEqual(self.mine(rep), [])
        self.assertIn(self.games, self.json.read_text())
        self.assertFalse(self.toml.exists())

    def test_no_config_at_all_warns_and_creates_nothing(self) -> None:
        self.json.unlink()
        self.toml.unlink()
        rep, _ = self.provision()
        (warning,) = self.mine(rep)
        self.assertIn("start shadPS4 once", warning)
        self.assertFalse(self.json.exists() or self.toml.exists())

    def test_a_file_it_does_not_understand_is_left_alone(self) -> None:
        self.json.write_text("not json")
        rep, _ = self.provision()
        self.assertEqual(self.json.read_text(), "not json")
        self.assertTrue(any("config.json" in w for w in rep.warnings))
        # the other file is still fixed
        self.assertIn(self.games, self.toml.read_text())

    def test_a_device_without_the_library_does_not_touch_shadps4(self) -> None:
        before = self.json.read_text()
        cfg_data = config_data()
        cfg_data["devices"]["deck"]["folders"] = ["saves"]
        cfg = parse_config(cfg_data)
        rep, lines = reporter()
        with FakeSyncthing(DECK_ID) as local:
            client = Client(local.url, API_KEY, label="local", sink=lines.append)
            provision_device(
                cfg, "deck", self.profile, client, None, rep, home=self.home,
                wait_timeout=0.0, poll=1.0, sleep=lambda s: None, state_root=self.state,
                proc=self.proc.path,
            )  # fmt: skip
        self.assertEqual(self.json.read_text(), before)


class DoctorTests(Device, unittest.TestCase):
    CONFIG = f"""
[hub]
name = "nas"
device_id = "{HUB_ID}"
root = "/data/games"
api_url = "http://127.0.0.1:1"
api_key = "{API_KEY}"

[devices.deck]
device_id = "{DECK_ID}"
profile = "steamos-emudeck"
folders = ["library", "saves"]
"""

    def setUp(self) -> None:
        super().setUp()
        syncthing = self.home / ".config/syncthing"
        syncthing.mkdir(parents=True)
        (syncthing / "config.xml").write_text(
            f'<configuration><folder id="wz-library" path="{self.home}/Emulation/">'
            f'<device id="{HUB_ID}"/></folder><device id="{DECK_ID}" name="x"/>'
            "<gui><address>127.0.0.1:8384</address><apikey>k</apikey></gui></configuration>"
        )

    def check(self, config_text=None):
        tmp = Path(self._tmp.name)
        ctx = context(tmp, self.CONFIG if config_text is None else config_text, system="Linux")
        probes = dataclasses.replace(
            ctx.probes,
            home=self.home,
            is_executable=lambda x: os.path.isfile(x) and os.access(x, os.X_OK),
        )
        devices = tuple(
            dataclasses.replace(d, profile=self.profile) for d in ctx.config.frontend_devices
        )
        config = dataclasses.replace(ctx.config, frontend_devices=devices)
        return check_frontend(Context(probes, config))

    def test_a_provisioned_device_is_ok(self) -> None:
        self.provision()
        result = by_name(self.check(), "shadPS4 install folder")
        self.assertEqual(result.status, OK)

    def test_a_missing_directory_is_a_warning_with_the_fix(self) -> None:
        result = by_name(self.check(), "shadPS4 install folder")
        self.assertEqual(result.status, WARN)
        self.assertIn(self.games, result.detail)
        self.assertIn("config.json", result.detail)
        self.assertIn("warpzone provision device deck", result.fix)

    def test_only_the_file_with_the_gap_is_named(self) -> None:
        self.provision()
        self.toml.write_text(TOML_TEXT)
        result = by_name(self.check(), "shadPS4 install folder")
        self.assertEqual(result.status, WARN)
        self.assertIn("config.toml", result.detail)
        self.assertNotIn("config.json", result.detail)

    def test_no_config_yet_says_to_start_shadps4(self) -> None:
        self.json.unlink()
        self.toml.unlink()
        result = by_name(self.check(), "shadPS4 install folder")
        self.assertEqual(result.status, WARN)
        self.assertIn("start shadPS4", result.fix)

    def test_a_file_it_cannot_read_is_a_warning(self) -> None:
        self.json.write_text("not json")
        self.provision()
        result = by_name(self.check(), "shadPS4 install folder")
        self.assertEqual(result.status, WARN)
        self.assertIn("config.json", result.detail)


if __name__ == "__main__":
    unittest.main()
