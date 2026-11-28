"""The Switch emulators' update and DLC folders: pure edits, profile table, provisioning, doctor."""

import dataclasses
import json
import os
import shutil
import unittest
from pathlib import Path

from support import API_KEY, DECK_ID, HUB_ID, device_probes
from test_doctor import by_name, context
from test_shadps4 import Device as ShadDevice
from warpzone import switchcontent
from warpzone.doctor import OK, WARN, Context, check_frontend
from warpzone.profiles import ProfileError, load_profile, parse_profile

PROFILE = load_profile("steamos-emudeck")
FOLDER = "/home/deck/Emulation/extras/switch"
ARRAY = "Paths\\external_content_dirs"
# What Eden writes on a console that never used the setting (the layout is its own).
EDEN = (
    "[Controls]\n"
    "player_0_button_a=button:0\n"
    "\n"
    "[UI]\n"
    "Paths\\gamedirs\\size=1\n"
    "Paths\\gamedirs\\1\\path=/home/deck/Emulation/roms/switch\n"
    f"{ARRAY}\\size=0\n"
    "Paths\\romsPath=/home/deck\n"
    "firstStart=false\n"
    "\n"
    "[WebService]\n"
    "enable_telemetry=false\n"
)
# A `Config.json` as Ryujinx writes it: two spaces, an empty list written as `[]`.
RYUJINX = (
    "{\n"
    '  "version": 70,\n'
    '  "game_dirs": [\n'
    '    "/home/deck/Emulation/roms/switch"\n'
    "  ],\n"
    '  "autoload_dirs": [],\n'
    '  "show_confirm_exit": true\n'
    "}"
)


def eden(*entries: str, size: int | None = None, tail: str = "") -> str:
    """An Eden file whose [UI] array lists `entries` (1-based, `size` of them by default)."""
    lines = [f"{ARRAY}\\{n}\\path={p}\n" for n, p in enumerate(entries, 1)]
    count = len(entries) if size is None else size
    return f"[UI]\n{''.join(lines)}{ARRAY}\\size={count}\n{tail}"


class EdenTests(unittest.TestCase):
    def test_the_real_state_gets_the_manual_fix(self) -> None:
        new = switchcontent.apply("eden", EDEN, FOLDER)
        self.assertEqual(
            new,
            EDEN.replace(
                f"{ARRAY}\\size=0\n",
                f"{ARRAY}\\1\\path={FOLDER}\n{ARRAY}\\size=1\n",
            ),
        )
        self.assertEqual(switchcontent.read("eden", new), (FOLDER,))

    def test_a_second_edit_changes_nothing(self) -> None:
        new = switchcontent.apply("eden", EDEN, FOLDER)
        self.assertEqual(switchcontent.apply("eden", new, FOLDER), new)
        self.assertFalse(switchcontent.missing("eden", new, FOLDER))
        self.assertFalse(switchcontent.missing("eden", new, FOLDER + "/"))
        self.assertTrue(switchcontent.missing("eden", EDEN, FOLDER))

    def test_the_entries_already_there_stay_and_the_count_follows(self) -> None:
        text = eden("/a", "/b", tail="x=1\n")
        new = switchcontent.apply("eden", text, FOLDER)
        self.assertEqual(new, eden("/a", "/b", FOLDER, tail="x=1\n"))

    def test_an_entry_beyond_the_count_is_ignored_and_replaced(self) -> None:
        text = eden("/a", size=0)  # Eden reads none of them
        self.assertEqual(switchcontent.read("eden", text), ())
        new = switchcontent.apply("eden", text, FOLDER)
        self.assertEqual(new, eden(FOLDER))

    def test_a_missing_count_is_added_after_the_entries(self) -> None:
        text = f"[UI]\n{ARRAY}\\1\\path=/a\nx=1\n"
        new = switchcontent.apply("eden", text, FOLDER)
        self.assertEqual(switchcontent.read("eden", new), (FOLDER,))
        self.assertEqual(new, f"[UI]\n{ARRAY}\\1\\path={FOLDER}\n{ARRAY}\\size=1\nx=1\n")

    def test_no_array_at_all_adds_one_at_the_end_of_the_section(self) -> None:
        text = "[UI]\nx=1\n\n[Other]\ny=2\n"
        new = switchcontent.apply("eden", text, FOLDER)
        self.assertEqual(
            new, f"[UI]\nx=1\n{ARRAY}\\1\\path={FOLDER}\n{ARRAY}\\size=1\n\n[Other]\ny=2\n"
        )

    def test_the_end_of_the_file_and_its_line_endings_are_kept(self) -> None:
        bare = f"[UI]\n{ARRAY}\\size=0"
        self.assertEqual(
            switchcontent.apply("eden", bare, FOLDER),
            f"[UI]\n{ARRAY}\\1\\path={FOLDER}\n{ARRAY}\\size=1",
        )
        crlf = EDEN.replace("\n", "\r\n")
        new = switchcontent.apply("eden", crlf, FOLDER)
        self.assertEqual(new.replace("\r\n", "\n"), switchcontent.apply("eden", EDEN, FOLDER))
        self.assertNotIn("\n", new.replace("\r\n", ""))

    def test_a_quoted_entry_counts_and_other_sections_are_not_read(self) -> None:
        text = f'[UI]\n{ARRAY}\\1\\path="{FOLDER}"\n{ARRAY}\\size=1\n'
        self.assertEqual(switchcontent.apply("eden", text, FOLDER), text)
        other = f"[Other]\n{ARRAY}\\1\\path={FOLDER}\n{ARRAY}\\size=1\n[UI]\nx=1\n"
        self.assertTrue(switchcontent.missing("eden", other, FOLDER))

    def test_a_file_it_cannot_edit_safely_is_refused(self) -> None:
        for text, message in (
            ("[General]\nx=1\n", "no \\[UI\\]"),
            ("[UI]\nx=1\n[UI]\ny=2\n", "more than one"),
            (f"[UI]\n{ARRAY}\\size=x\n", "not a number"),
            (f"[UI]\n{ARRAY}\\size=0\n{ARRAY}\\size=1\n", "size appears more than once"),
            (f"[UI]\n{ARRAY}\\1\\path=/a\n{ARRAY}\\1\\path=/b\n", "appears more than once"),
        ):
            with (
                self.subTest(text=text),
                self.assertRaisesRegex(switchcontent.ContentConfigError, message),
            ):
                switchcontent.apply("eden", text, FOLDER)

    def test_a_path_qt_would_escape_is_not_written(self) -> None:
        for folder in ("/home/d,eck/x", '/home/"x"', "/home/é/x", "/home/x/ ", "/a\\b"):
            with (
                self.subTest(folder=folder),
                self.assertRaisesRegex(switchcontent.ContentConfigError, "escapes"),
            ):
                switchcontent.apply("eden", EDEN, folder)

    def test_a_path_with_a_space_inside_is_written_as_it_is(self) -> None:
        new = switchcontent.apply("eden", EDEN, "/home/deck/My Games/switch")
        self.assertIn(f"{ARRAY}\\1\\path=/home/deck/My Games/switch\n", new)


class RyujinxTests(unittest.TestCase):
    def test_an_empty_list_gets_the_folder_and_nothing_else_changes(self) -> None:
        new = switchcontent.apply("ryujinx", RYUJINX, FOLDER)
        self.assertEqual(
            new,
            RYUJINX.replace('"autoload_dirs": []', f'"autoload_dirs": [\n    "{FOLDER}"\n  ]'),
        )
        self.assertEqual(json.loads(new)["autoload_dirs"], [FOLDER])

    def test_a_second_edit_changes_nothing(self) -> None:
        new = switchcontent.apply("ryujinx", RYUJINX, FOLDER)
        self.assertEqual(switchcontent.apply("ryujinx", new, FOLDER), new)
        self.assertFalse(switchcontent.missing("ryujinx", new, FOLDER + "/"))
        self.assertTrue(switchcontent.missing("ryujinx", RYUJINX, FOLDER))

    def test_a_list_with_entries_gets_one_more_in_its_own_layout(self) -> None:
        text = RYUJINX.replace('"autoload_dirs": []', '"autoload_dirs": [\n    "/a"\n  ]')
        new = switchcontent.apply("ryujinx", text, FOLDER)
        self.assertEqual(new, text.replace('"/a"\n', f'"/a",\n    "{FOLDER}"\n'))
        inline = RYUJINX.replace('"autoload_dirs": []', '"autoload_dirs": ["/a", "/b"]')
        new = switchcontent.apply("ryujinx", inline, FOLDER)
        self.assertEqual(new, inline.replace('"/b"]', f'"/b", "{FOLDER}"]'))

    def test_other_layouts_are_kept(self) -> None:
        for text in (
            '{"autoload_dirs":[]}',
            '{"autoload_dirs": [ "/a" ], "x": [1, 2]}',
            RYUJINX.replace("\n", "\r\n"),
            "﻿" + RYUJINX,
            RYUJINX.replace("  ", "\t"),
            RYUJINX + "\n",
        ):
            with self.subTest(text=text):
                new = switchcontent.apply("ryujinx", text, FOLDER)
                self.assertIn(FOLDER, json.loads(new.removeprefix("﻿"))["autoload_dirs"])
                self.assertEqual(
                    {**json.loads(text.removeprefix("﻿")), "autoload_dirs": None},
                    {**json.loads(new.removeprefix("﻿")), "autoload_dirs": None},
                )
        crlf = switchcontent.apply("ryujinx", RYUJINX.replace("\n", "\r\n"), FOLDER)
        self.assertNotIn("\n", crlf.replace("\r\n", ""))

    def test_a_key_inside_a_value_is_not_the_key(self) -> None:
        text = '{"note": "\\"autoload_dirs\\": [\\"x\\"]", "autoload_dirs": []}'
        new = switchcontent.apply("ryujinx", text, FOLDER)
        self.assertEqual(json.loads(new)["autoload_dirs"], [FOLDER])
        self.assertEqual(json.loads(new)["note"], json.loads(text)["note"])

    def test_a_file_it_cannot_edit_safely_is_refused(self) -> None:
        for text, message in (
            ("not json", "not a JSON object"),
            ("[1]", "not a JSON object"),
            ('{"autoload_dirs": []} x', "not valid JSON"),
            ('{"game_dirs": []}', "no autoload_dirs"),
            ('{"autoload_dirs": "x"}', "not a list of strings"),
            ('{"autoload_dirs": [1]}', "not a list of strings"),
            ('{"autoload_dirs": [], "autoload_dirs": []}', "more than once"),
        ):
            with (
                self.subTest(text=text),
                self.assertRaisesRegex(switchcontent.ContentConfigError, message),
            ):
                switchcontent.apply("ryujinx", text, FOLDER)


class ProfileTests(unittest.TestCase):
    def test_the_shipped_profile_names_the_library_folder_both_emulators_scan(self) -> None:
        rules = PROFILE.switch_content
        self.assertEqual(set(rules), {"eden", "ryujinx"})
        library = PROFILE.paths["library"]
        for rule in rules.values():
            self.assertEqual(rule.wants(library), "~/Emulation/extras/switch")
        self.assertEqual(rules["eden"].config, "~/.config/eden/qt-config.ini")
        self.assertEqual(rules["ryujinx"].config, "~/.config/Ryujinx/Config.json")
        self.assertIn("eden", rules["eden"].process)
        self.assertIn("Ryujinx", rules["ryujinx"].process)

    def test_eden_settings_layer_never_touches_the_array(self) -> None:
        noise = PROFILE.settings["eden"].files["qt-config"].noise
        self.assertIn("[UI]Paths\\external_content_dirs*", noise)

    def test_the_other_shipped_profile_has_none(self) -> None:
        self.assertEqual(load_profile("darkos").switch_content, {})

    def parse(self, **tables):
        return parse_profile(
            {"name": "x", "paths": {"library": "~/lib"}, "switch_content": tables}, "t"
        )

    def test_the_table_is_checked(self) -> None:
        good = {"config": "~/a.ini", "process": ["eden"], "folder": "{library}/extras/switch"}
        rule = self.parse(eden=good).switch_content["eden"]
        self.assertEqual(rule.wants("/l"), "/l/extras/switch")
        for change, message in (
            ({"config": "rel.ini"}, "switch_content.eden.config"),
            ({"config": "{library}/a.ini"}, "switch_content.eden.config"),
            ({"process": []}, "switch_content.eden.process"),
            ({"folder": "rel"}, "switch_content.eden.folder"),
            ({"folder": "{nope}/x"}, "switch_content.eden.folder"),
            ({"extra": 1}, "unknown key"),
        ):
            with self.assertRaisesRegex(ProfileError, message):
                self.parse(eden={**good, **change})
        with self.assertRaisesRegex(ProfileError, "unknown emulator"):
            self.parse(yuzu=good)
        with self.assertRaisesRegex(ProfileError, "switch_content.eden.folder"):
            parse_profile({"name": "x", "switch_content": {"eden": good}}, "t")


class Device(ShadDevice):
    """A temporary home with Eden's and Ryujinx's settings files, as a console leaves them."""

    def setUp(self) -> None:
        super().setUp()
        self.eden = self.home / ".config/eden/qt-config.ini"
        self.ryujinx = self.home / ".config/Ryujinx/Config.json"
        for path, text in ((self.eden, EDEN), (self.ryujinx, RYUJINX)):
            path.parent.mkdir(parents=True)
            path.write_text(text)
        self.folder_ = f"{self.home}/Emulation/extras/switch"
        self.profile = dataclasses.replace(self.profile, shadps4=None, cemu=None)

    @staticmethod
    def mine(rep, label: str) -> list[str]:
        return [w for w in rep.warnings if label in w and not w.startswith("wz-")]

    def backups(self, emulator: str) -> list[Path]:
        folder = self.state / "backups" / emulator
        return sorted(p for p in folder.rglob("*") if p.is_file()) if folder.is_dir() else []


class ProvisionTests(Device, unittest.TestCase):
    def test_both_files_get_the_folder_and_nothing_else_changes(self) -> None:
        rep, lines = self.provision()
        self.assertEqual(self.mine(rep, "Eden") + self.mine(rep, "Ryujinx"), [])
        self.assertEqual(self.eden.read_text(), switchcontent.apply("eden", EDEN, self.folder_))
        self.assertEqual(
            self.ryujinx.read_text(), switchcontent.apply("ryujinx", RYUJINX, self.folder_)
        )
        self.assertEqual(switchcontent.read("eden", self.eden.read_text()), (self.folder_,))
        self.assertEqual(json.loads(self.ryujinx.read_text())["autoload_dirs"], [self.folder_])
        self.assertTrue(any(line.startswith("eden: added") for line in lines))
        self.assertTrue(any(line.startswith("ryujinx: added") for line in lines))

    def test_the_old_files_are_backed_up_and_a_second_run_changes_nothing(self) -> None:
        self.provision()
        self.assertEqual([p.read_text() for p in self.backups("eden")], [EDEN])
        self.assertEqual([p.read_text() for p in self.backups("ryujinx")], [RYUJINX])
        stamps = [p.stat().st_mtime_ns for p in (self.eden, self.ryujinx)]
        rep, lines = self.provision()
        self.assertEqual([p.stat().st_mtime_ns for p in (self.eden, self.ryujinx)], stamps)
        self.assertEqual(len(self.backups("eden")) + len(self.backups("ryujinx")), 2)
        self.assertEqual(self.mine(rep, "Eden") + self.mine(rep, "Ryujinx"), [])
        self.assertTrue(any("eden" in line and "already" in line for line in lines))
        self.assertTrue(any("ryujinx" in line and "already" in line for line in lines))

    def test_a_running_emulator_keeps_only_its_own_file_untouched(self) -> None:
        for comm, label, path, text in (
            ("eden", "Eden", self.eden, EDEN),
            ("Eden.AppImage", "Eden", self.eden, EDEN),
            ("Ryujinx", "Ryujinx", self.ryujinx, RYUJINX),
        ):
            with self.subTest(comm=comm):
                self.start(comm)
                rep, _ = self.provision()
                self.assertEqual(path.read_text(), text)
                (warning,) = [w for w in rep.warnings if f"{label} is running" in w]
                self.assertIn("provision device deck", warning)
                self.assertEqual(self.backups(label.lower()), [])
                other = self.ryujinx if path == self.eden else self.eden
                self.assertIn(self.folder_, other.read_text())
                shutil.rmtree(self.proc.path / "4242")
                self.eden.write_text(EDEN)
                self.ryujinx.write_text(RYUJINX)
                shutil.rmtree(self.state, ignore_errors=True)

    def test_an_emulator_started_during_the_backup_stops_the_edit(self) -> None:
        with self.after_backup(lambda path: self.start("eden")):
            rep, _ = self.provision()
        self.assertEqual(self.eden.read_text(), EDEN)
        self.assertEqual(len(self.mine(rep, "Eden is running")), 1)

    def test_a_file_edited_during_the_backup_is_left_alone(self) -> None:
        def edit(path: Path) -> None:
            path.write_text(path.read_text() + "\n")

        with self.after_backup(edit):
            rep, _ = self.provision()
        self.assertEqual(self.eden.read_text(), EDEN + "\n")
        self.assertTrue(any("changed while" in w for w in rep.warnings))

    def test_without_proc_nothing_is_edited(self) -> None:
        rep, _ = self.provision(probes=device_probes(self.proc.path / "missing"))
        self.assertEqual(self.eden.read_text(), EDEN)
        self.assertEqual(self.ryujinx.read_text(), RYUJINX)
        self.assertTrue(any("/proc" in w and "Eden" in w for w in rep.warnings))

    def test_dry_run_writes_nothing(self) -> None:
        _rep, lines = self.provision(dry_run=True)
        self.assertEqual(self.eden.read_text(), EDEN)
        self.assertEqual(self.ryujinx.read_text(), RYUJINX)
        self.assertEqual(self.backups("eden") + self.backups("ryujinx"), [])
        self.assertTrue(any("eden: dry-run: would add" in line for line in lines))
        self.assertTrue(any("ryujinx: dry-run: would add" in line for line in lines))

    def test_a_missing_file_is_not_created_and_it_says_so(self) -> None:
        self.eden.unlink()
        rep, _ = self.provision()
        self.assertFalse(self.eden.exists())
        (warning,) = self.mine(rep, "Eden settings")
        self.assertIn("start Eden once", warning)
        self.assertIn(self.folder_, self.ryujinx.read_text())

    def test_a_file_it_does_not_understand_is_left_alone(self) -> None:
        self.ryujinx.write_text("not json")
        rep, _ = self.provision()
        self.assertEqual(self.ryujinx.read_text(), "not json")
        self.assertTrue(any("Config.json" in w and "left alone" in w for w in rep.warnings))
        self.assertIn(self.folder_, self.eden.read_text())

    def test_a_library_path_qt_would_escape_leaves_eden_alone_with_a_warning(self) -> None:
        self.profile = dataclasses.replace(
            self.profile, paths={**self.profile.paths, "library": "~/Emu,lation"}
        )
        rep, _ = self.provision()
        self.assertEqual(self.eden.read_text(), EDEN)
        self.assertTrue(any("qt-config.ini" in w and "escapes" in w for w in rep.warnings))


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

    def check(self):
        ctx = context(Path(self._tmp.name), self.CONFIG, system="Linux")
        probes = dataclasses.replace(
            ctx.probes,
            home=self.home,
            is_executable=lambda x: os.path.isfile(x) and os.access(x, os.X_OK),
        )
        devices = tuple(
            dataclasses.replace(d, profile=self.profile) for d in ctx.config.frontend_devices
        )
        return check_frontend(
            Context(probes, dataclasses.replace(ctx.config, frontend_devices=devices))
        )

    def test_a_provisioned_device_is_ok(self) -> None:
        self.provision()
        for name in ("Eden update and DLC folder", "Ryujinx update and DLC folder"):
            result = by_name(self.check(), name)
            self.assertEqual(result.status, OK, result.detail)

    def test_files_as_the_emulators_left_them_are_a_warning_with_the_fix(self) -> None:
        for name, label in (
            ("Eden update and DLC folder", "Eden"),
            ("Ryujinx update and DLC folder", "Ryujinx"),
        ):
            result = by_name(self.check(), name)
            self.assertEqual(result.status, WARN)
            self.assertIn(self.folder_, result.detail)
            self.assertIn(f"warpzone provision device deck` with {label} closed", result.fix)

    def test_no_settings_file_yet_says_to_start_the_emulator(self) -> None:
        self.eden.unlink()
        result = by_name(self.check(), "Eden update and DLC folder")
        self.assertEqual(result.status, WARN)
        self.assertIn("start Eden once", result.fix)

    def test_a_file_it_cannot_read_is_a_warning(self) -> None:
        self.ryujinx.write_text("not json")
        result = by_name(self.check(), "Ryujinx update and DLC folder")
        self.assertEqual(result.status, WARN)
        self.assertIn("Config.json", result.detail)


if __name__ == "__main__":
    unittest.main()
