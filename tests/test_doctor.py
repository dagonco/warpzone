import dataclasses
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import ClassVar
from unittest import mock

from support import API_KEY, DECK_ID, HUB_ID
from test_cli import run
from warpzone import __version__, doctor, esde, selfupdate, updatetimer
from warpzone.doctor import (
    FAIL,
    GIB,
    OK,
    OPTIONAL,
    WARN,
    Context,
    Probes,
    Result,
    RunOutput,
    ToolSpec,
    check_arcade_dats,
    check_base,
    check_config,
    check_dats,
    check_flatpak_access,
    check_flycast,
    check_game_mode,
    check_hub,
    check_ra,
    check_settings,
    check_switch,
    check_syncthing,
    check_syncthing_unit,
    check_tool,
    check_update_timer,
    check_updater,
    check_vita3k,
    collapse_inbox,
    exit_code,
    hint_for,
    judge_esde_script,
    judge_event_scripts,
    judge_flycast_vmus,
    judge_game_mode,
    judge_last_apply,
    judge_settings_layers,
    judge_unit_state,
    judge_update_timer,
    judge_updater_conf,
    judge_updater_script,
    judge_updater_state,
    judge_vita3k_user,
    load_config_state,
    platform_family,
    render,
    run_checks,
    run_doctor,
    vita3k_user_id,
)
from warpzone.profiles import EmulatorSettings, Profile, SettingsFile, load_profile

RA_KEY = "ra-fake-key-1234567890"  # gitleaks:allow (test fixture, not a real key)
ST_KEY = "st-fake-key-0987654321"  # gitleaks:allow (test fixture, not a real key)
DEBIAN = {"ID": "ubuntu", "ID_LIKE": "debian", "PRETTY_NAME": "Ubuntu 24.04 LTS"}
STEAMOS = {"ID": "steamos", "ID_LIKE": "arch", "PRETTY_NAME": "SteamOS Holo"}
CHDMAN_OUT = "chdman - MAME Compressed Hunks of Data (CHD) manager 0.289 (unknown)\nUsage:\n"
BSDTAR_OUT = "bsdtar 3.5.3 - libarchive 3.7.4 zlib/1.2.12\n"
RAHASHER_OUT = "RAHasher 1.8.4\n====================\nUsage: RAHasher [-v] ...\n"


def fake_run(outputs: Mapping[str, str] | None = None):
    outputs = {
        "chdman": CHDMAN_OUT,
        "bsdtar": BSDTAR_OUT,
        "RAHasher": RAHASHER_OUT,
        **(outputs or {}),
    }

    def run_tool(command: Sequence[str]) -> RunOutput:
        name = Path(command[0]).name
        if name not in outputs:
            raise FileNotFoundError(2, "No such file or directory")
        return RunOutput(0, outputs[name])

    return run_tool


def make_probes(
    tmp: Path,
    *,
    tools: Mapping[str, str] | None = None,
    executables: Sequence[str] = (),
    **overrides,
) -> Probes:
    found = dict(tools or {})
    values = {
        "config_path": tmp / "config.toml",
        "which": found.get,
        "run": fake_run(),
        "http_status": lambda url, headers, timeout: 200,
        "env": {},
        "home": tmp / "home",
        "system": "Darwin",
        "machine": "arm64",
        "os_release": {},
        "python": (3, 12, 1),
        "is_executable": lambda path: path in executables or path in found.values(),
        "disk_free": lambda path: (500 * GIB, 400 * GIB),
    }
    values.update(overrides)
    return Probes(**values)


def context(tmp: Path, config_text: str | None = None, **kw) -> Context:
    probes = make_probes(tmp, **kw)
    if config_text is not None:
        probes.config_path.write_text(config_text)
    return Context(probes, load_config_state(probes.config_path))


def by_name(results: Sequence[Result], name: str) -> Result:
    return next(r for r in results if r.name == name)


class TmpCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)


class PlatformTests(unittest.TestCase):
    def test_families(self) -> None:
        self.assertEqual(platform_family("Darwin", {}), "macos")
        self.assertEqual(platform_family("Linux", DEBIAN), "debian")
        self.assertEqual(platform_family("Linux", {"ID": "fedora"}), "fedora")
        self.assertEqual(platform_family("Linux", {"ID": "manjaro", "ID_LIKE": "arch"}), "arch")
        self.assertEqual(platform_family("Linux", STEAMOS), "steamos")
        self.assertEqual(platform_family("Linux", {"ID": "nixos"}), "linux")
        self.assertEqual(platform_family("Windows", {}), "other")

    def test_hint_falls_back_to_linux_then_any(self) -> None:
        table = {"macos": "m", "linux": "l", "any": "a"}
        self.assertEqual(hint_for(table, "macos"), "m")
        self.assertEqual(hint_for(table, "debian"), "l")
        self.assertEqual(hint_for({"any": "a"}, "debian"), "a")
        self.assertEqual(hint_for(table, "other"), "a")


class RenderTests(unittest.TestCase):
    RESULTS = (
        Result("alpha", "First", OK, "fine"),
        Result("beta", "First", FAIL, "broken\nsecond line", "do this"),
        Result("gamma", "Second", OPTIONAL, "not set"),
        Result("delta", "Second", WARN, "old"),
    )

    def test_groups_marks_details_and_fixes(self) -> None:
        text = "\n".join(render(self.RESULTS))
        self.assertEqual(
            text,
            "\n".join(
                [
                    "First",
                    "  [ok] alpha fine",
                    "  [!!] beta  broken",
                    "             second line",
                    "             fix: do this",
                    "",
                    "Second",
                    "  [--] gamma not set",
                    "  [! ] delta old",
                    "",
                    "1 problem(s), 1 warning(s), 1 optional item(s) not set up. "
                    "Fix the [!!] lines above.",
                ]
            ),
        )

    def test_long_details_and_fixes_wrap_with_a_hanging_indent(self) -> None:
        long = Result(
            "tool",
            "G",
            FAIL,
            "one two three four five six seven eight nine ten\nsecond line stays a line",
            "alpha beta gamma delta epsilon zeta eta theta iota kappa lambda mu nu xi omicron",
        )
        lines = render([long], width=44)
        indent = " " * (7 + len("tool") + 1)
        self.assertEqual(lines[1], "  [!!] tool one two three four five six")
        self.assertEqual(lines[2], f"{indent}seven eight nine ten")
        self.assertEqual(lines[3], f"{indent}second line stays a line")
        fix = [line for line in lines if line.startswith(indent) and "fix:" in line]
        self.assertEqual(len(fix), 1)
        start = lines.index(fix[0])
        self.assertEqual(lines[start], f"{indent}fix: alpha beta gamma delta")
        self.assertTrue(lines[start + 1].startswith(f"{indent}     epsilon"))
        for line in lines[1:-2]:
            self.assertLessEqual(len(line), 44)

    def test_a_word_longer_than_the_line_is_never_cut(self) -> None:
        path = "/very/long/path/" + "x" * 80
        lines = render([Result("a", "G", FAIL, f"see {path} now", f"mount {path}")], width=40)
        self.assertEqual(sum(path in line for line in lines), 2)

    def test_narrow_terminals_do_not_collapse_the_text(self) -> None:
        lines = render([Result("a-very-long-label-name", "G", OK, "word " * 12)], width=10)
        self.assertTrue(all(len(line.split()) >= 2 for line in lines[1:-2]))

    def test_short_lines_are_left_alone_at_any_width(self) -> None:
        self.assertEqual(render(self.RESULTS, width=200), render(self.RESULTS))

    def test_exit_code_only_fails_on_fail(self) -> None:
        self.assertEqual(exit_code(self.RESULTS), 1)
        self.assertEqual(exit_code([r for r in self.RESULTS if r.status != FAIL]), 0)
        self.assertEqual(exit_code([]), 0)

    def test_clean_summary(self) -> None:
        text = "\n".join(render([Result("a", "G", OK, "x")]))
        self.assertIn("0 problem(s)", text)
        self.assertIn("Nothing required is missing.", text)


class BaseTests(TmpCase):
    def test_ok_on_macos(self) -> None:
        results = check_base(context(self.tmp))
        self.assertEqual([r.status for r in results], [OK, OK, OK])
        self.assertEqual(results[0].detail, __version__)
        self.assertEqual(results[2].detail, "macOS, arm64")

    def test_old_python_fails_with_a_fix(self) -> None:
        results = check_base(context(self.tmp, python=(3, 10, 4)))
        python = by_name(results, "Python")
        self.assertEqual(python.status, FAIL)
        self.assertIn("3.11", python.fix)

    def test_linux_names_the_distribution(self) -> None:
        results = check_base(context(self.tmp, system="Linux", os_release=DEBIAN, machine="x86_64"))
        self.assertEqual(by_name(results, "Platform").detail, "Ubuntu 24.04 LTS, x86_64")

    def test_other_systems_warn(self) -> None:
        results = check_base(context(self.tmp, system="Windows"))
        self.assertEqual(by_name(results, "Platform").status, WARN)


INBOX_ONLY = '[inbox]\nroot = "/mnt/hub"\n'
FULL = f"""
[hub]
name = "nas"
device_id = "{HUB_ID}"
root = "/data/games"
api_url = "http://127.0.0.1:1"
api_key = "{API_KEY}"

[devices.deck]
device_id = "BBBBBBB-BBBBBBB-BBBBBBB-BBBBBBB-BBBBBBB-BBBBBBB-BBBBBBB-BBBBBBB"
profile = "steamos-emudeck"
folders = ["library", "saves"]

[inbox]
root = "/mnt/hub"
"""


class ConfigTests(TmpCase):
    def test_missing_config_is_optional_and_says_what_works_without(self) -> None:
        results = check_config(context(self.tmp))
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].status, OPTIONAL)
        self.assertIn("bios works without it", results[0].detail)
        self.assertIn("config.example.toml", results[0].fix)

    def test_inbox_only_config_does_not_list_hub_and_devices_as_optional(self) -> None:
        results = check_config(context(self.tmp, INBOX_ONLY))
        self.assertEqual(by_name(results, "Config file").status, OK)
        inbox = by_name(results, "[inbox]")
        self.assertEqual(inbox.status, OK)
        self.assertIn("warpzone inbox", inbox.detail)
        self.assertNotIn(OPTIONAL, {r.status for r in results})
        self.assertFalse([r for r in results if r.name in ("[hub]", "[devices]")])
        note = by_name(results, "[hub], [devices]")
        self.assertEqual((note.status, note.fix), (OK, ""))
        self.assertEqual(len(results), 3)

    def test_inbox_only_summary_counts_no_optional_items(self) -> None:
        results = check_config(context(self.tmp, INBOX_ONLY))
        self.assertIn("0 optional item(s)", "\n".join(render(results)))

    def test_a_config_with_a_hub_still_lists_the_missing_tables(self) -> None:
        no_inbox = FULL.split("[inbox]")[0]
        results = check_config(context(self.tmp, no_inbox))
        self.assertEqual(by_name(results, "[hub]").status, OK)
        self.assertEqual(by_name(results, "[devices]").status, OK)
        inbox = by_name(results, "[inbox]")
        self.assertEqual(inbox.status, OPTIONAL)
        self.assertIn("warpzone inbox", inbox.detail)
        self.assertFalse([r for r in results if r.name == "[hub], [devices]"])

    def test_full_config_shows_all_tables(self) -> None:
        results = check_config(context(self.tmp, FULL))
        self.assertEqual([r.status for r in results], [OK, OK, OK, OK])
        self.assertIn("deck", by_name(results, "[devices]").detail)

    def test_invalid_config_reports_the_loader_errors(self) -> None:
        results = check_config(context(self.tmp, '[hub]\nname = "nas"\n'))
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].status, FAIL)
        self.assertIn("hub.device_id", results[0].detail)
        self.assertIn("[devices.<name>]", results[0].detail)

    def test_unknown_key_and_bad_toml_fail(self) -> None:
        unknown = check_config(context(self.tmp, '[inbox]\nroot = "/x"\n[extra]\na = 1\n'))
        self.assertEqual(unknown[0].status, FAIL)
        self.assertIn("'extra'", unknown[0].detail)
        broken = check_config(context(self.tmp, "[inbox\n"))
        self.assertEqual(broken[0].status, FAIL)

    def test_settings_survive_an_invalid_hub(self) -> None:
        ctx = context(self.tmp, '[hub]\nname = "nas"\n[inbox]\nroot = "/mnt/hub"\n')
        self.assertIsNotNone(ctx.config.error)
        self.assertEqual(ctx.config.settings.root, "/mnt/hub")
        self.assertTrue(ctx.inbox_configured)


class ToolTests(TmpCase):
    CHDMAN = doctor.TOOLS[0]
    RAHASHER = next(t for t in doctor.TOOLS if t.key == "RAHasher")
    DOLPHIN = next(t for t in doctor.TOOLS if t.key == "dolphin-tool")

    def test_present_prints_the_version_line_and_path(self) -> None:
        ctx = context(self.tmp, INBOX_ONLY, tools={"chdman": "/opt/bin/chdman"})
        r = check_tool(ctx, self.CHDMAN)
        self.assertEqual(r.status, OK)
        self.assertIn("manager 0.289", r.detail)
        self.assertIn("/opt/bin/chdman", r.detail)

    def test_missing_required_tool_fails_with_the_macos_hint(self) -> None:
        r = check_tool(context(self.tmp, INBOX_ONLY), self.CHDMAN)
        self.assertEqual(r.status, FAIL)
        self.assertEqual(r.fix, "brew install rom-tools")

    def test_missing_tool_is_only_optional_without_an_inbox_table(self) -> None:
        r = check_tool(context(self.tmp), self.CHDMAN)
        self.assertEqual(r.status, OPTIONAL)

    def test_hints_per_platform(self) -> None:
        cases = {
            ("Linux", tuple(DEBIAN.items())): "sudo apt install mame-tools",
            ("Linux", (("ID", "fedora"),)): "sudo dnf install mame-tools",
            ("Linux", (("ID", "arch"),)): "sudo pacman -S mame-tools",
        }
        for (system, release), expected in cases.items():
            with self.subTest(expected=expected):
                ctx = context(self.tmp, INBOX_ONLY, system=system, os_release=dict(release))
                self.assertEqual(check_tool(ctx, self.CHDMAN).fix, expected)

    def test_bsdtar_hint_on_debian(self) -> None:
        ctx = context(self.tmp, INBOX_ONLY, system="Linux", os_release=DEBIAN)
        r = check_tool(ctx, doctor.TOOLS[1])
        self.assertIn("libarchive-tools", r.fix)

    def test_steamos_hint_mentions_the_read_only_root_and_a_container(self) -> None:
        ctx = context(self.tmp, INBOX_ONLY, system="Linux", os_release=STEAMOS)
        fix = check_tool(ctx, self.CHDMAN).fix
        self.assertIn("read-only", fix)
        self.assertIn("distrobox", fix)

    def test_bad_version_output_warns(self) -> None:
        ctx = context(
            self.tmp,
            INBOX_ONLY,
            tools={"chdman": "/opt/bin/chdman"},
            run=fake_run({"chdman": "segmentation noise\n"}),
        )
        r = check_tool(ctx, self.CHDMAN)
        self.assertEqual(r.status, WARN)
        self.assertIn("no recognizable version", r.detail)

    def test_a_tool_that_cannot_run_fails(self) -> None:
        def boom(command: Sequence[str]) -> RunOutput:
            raise PermissionError(13, "Permission denied")

        ctx = context(self.tmp, INBOX_ONLY, tools={"chdman": "/opt/bin/chdman"}, run=boom)
        r = check_tool(ctx, self.CHDMAN)
        self.assertEqual(r.status, FAIL)
        self.assertIn("cannot be run", r.detail)

    def test_a_timeout_counts_as_cannot_run(self) -> None:
        def slow(command: Sequence[str]) -> RunOutput:
            raise subprocess.TimeoutExpired(command, 10)

        ctx = context(self.tmp, INBOX_ONLY, tools={"chdman": "/opt/bin/chdman"}, run=slow)
        self.assertEqual(check_tool(ctx, self.CHDMAN).status, FAIL)

    def test_configured_path_must_be_executable(self) -> None:
        ctx = context(self.tmp, '[inbox]\nchdman = "/nope/chdman"\n')
        r = check_tool(ctx, self.CHDMAN)
        self.assertEqual(r.status, FAIL)
        self.assertIn("/nope/chdman", r.detail)

    def test_configured_path_wins_over_path_lookup(self) -> None:
        ctx = context(
            self.tmp,
            '[inbox]\nchdman = "/cfg/chdman"\n',
            tools={"chdman": "/path/chdman"},
            executables=["/cfg/chdman"],
        )
        self.assertIn("/cfg/chdman", check_tool(ctx, self.CHDMAN).detail)

    def test_rahasher_on_path_but_not_configured_warns(self) -> None:
        ctx = context(self.tmp, INBOX_ONLY, tools={"RAHasher": "/opt/bin/RAHasher"})
        r = check_tool(ctx, self.RAHASHER)
        self.assertEqual(r.status, WARN)
        self.assertIn("rahasher", r.fix)

    def test_rahasher_configured_prints_its_version(self) -> None:
        ctx = context(self.tmp, '[inbox]\nrahasher = "/x/RAHasher"\n', executables=["/x/RAHasher"])
        r = check_tool(ctx, self.RAHASHER)
        self.assertEqual(r.status, OK)
        self.assertIn("RAHasher 1.8.4", r.detail)

    def test_rahasher_missing_is_optional_and_points_to_the_build(self) -> None:
        r = check_tool(context(self.tmp, INBOX_ONLY), self.RAHASHER)
        self.assertEqual(r.status, OPTIONAL)
        self.assertIn("RALibretro", r.fix)
        self.assertIn("HAVE_CHD=1", r.fix)

    def test_dolphin_tool_inside_the_app_bundle_is_found_but_not_on_path(self) -> None:
        app = "/Applications/Dolphin.app/Contents/MacOS/dolphin-tool"
        r = check_tool(context(self.tmp, INBOX_ONLY, executables=[app]), self.DOLPHIN)
        self.assertEqual(r.status, WARN)
        self.assertIn("dolphin_tool", r.fix)

    def test_a_tool_without_version_args_is_not_run(self) -> None:
        spec = ToolSpec("nsz", ("nsz",), "demo", {"any": "install it"}, version_args=None)

        def never(command: Sequence[str]) -> RunOutput:
            raise AssertionError("must not run")

        ctx = context(self.tmp, tools={"nsz": "/bin/nsz"}, run=never)
        self.assertEqual(check_tool(ctx, spec).status, OK)

    def test_nsz_is_listed_and_optional_even_with_an_inbox(self) -> None:
        nsz = next(t for t in doctor.TOOLS if t.key == "nsz")
        r = check_tool(context(self.tmp, INBOX_ONLY), nsz)
        self.assertEqual(r.status, OPTIONAL)
        self.assertIn("uv tool install nsz", r.fix)
        self.assertIn("Switch", r.detail + nsz.purpose)

    def test_nsz_can_be_pointed_at_with_the_inbox_setting(self) -> None:
        nsz = next(t for t in doctor.TOOLS if t.key == "nsz")
        ctx = context(self.tmp, '[inbox]\nnsz = "/x/nsz"\n', executables=["/x/nsz"])
        r = check_tool(ctx, nsz)
        self.assertEqual(r.status, OK)
        self.assertIn("/x/nsz", r.detail)
        r = check_tool(context(self.tmp, '[inbox]\nnsz = "/nope/nsz"\n'), nsz)
        self.assertEqual(r.status, FAIL)

    def test_orbis_pkg_util_is_listed_and_optional_even_with_an_inbox(self) -> None:
        spec = next(t for t in doctor.TOOLS if t.key == "orbis-pkg-util")
        r = check_tool(context(self.tmp, INBOX_ONLY), spec)
        self.assertEqual(r.status, OPTIONAL)
        self.assertIn("cargo install orbis-pkg-util", r.fix)
        self.assertIn("PS4", spec.purpose)

    def test_orbis_pkg_util_in_the_cargo_folder_is_found_but_not_on_path(self) -> None:
        spec = next(t for t in doctor.TOOLS if t.key == "orbis-pkg-util")
        ctx = context(self.tmp, INBOX_ONLY)
        cargo = str(ctx.probes.home / ".cargo/bin/orbis-pkg-util")
        r = check_tool(context(self.tmp, INBOX_ONLY, executables=[cargo]), spec)
        self.assertEqual(r.status, WARN)
        self.assertIn(cargo, r.detail)
        self.assertIn('orbis_pkg_util = "' + cargo + '"', r.fix)

    def test_orbis_pkg_util_can_be_pointed_at_with_the_inbox_setting(self) -> None:
        spec = next(t for t in doctor.TOOLS if t.key == "orbis-pkg-util")
        ctx = context(self.tmp, '[inbox]\norbis_pkg_util = "/x/opu"\n', executables=["/x/opu"])
        self.assertEqual(check_tool(ctx, spec).status, OK)
        bad = context(self.tmp, '[inbox]\norbis_pkg_util = "/nope/opu"\n')
        self.assertEqual(check_tool(bad, spec).status, FAIL)

    VITA3K_BIN = "/Applications/Vita3K.app/Contents/MacOS/Vita3K"

    def test_vita3k_is_listed_and_optional_and_never_run(self) -> None:
        spec = next(t for t in doctor.TOOLS if t.key == "Vita3K")
        r = check_tool(context(self.tmp, INBOX_ONLY), spec)
        self.assertEqual(r.status, OPTIONAL)
        self.assertIn("Vita3K.app", r.fix)
        self.assertIn("Vita", spec.purpose)
        self.assertIsNone(spec.version_args)

    def test_vita3k_in_applications_is_what_inbox_uses_on_macos(self) -> None:
        spec = next(t for t in doctor.TOOLS if t.key == "Vita3K")
        r = check_tool(context(self.tmp, INBOX_ONLY, executables=[self.VITA3K_BIN]), spec)
        self.assertEqual(r.status, OK)
        self.assertIn("/Applications/Vita3K.app", r.detail)
        linux = context(
            self.tmp, INBOX_ONLY, executables=[self.VITA3K_BIN], system="Linux", os_release=DEBIAN
        )
        self.assertEqual(check_tool(linux, spec).status, OPTIONAL)

    def test_vita3k_can_be_set_to_the_app_or_a_binary(self) -> None:
        spec = next(t for t in doctor.TOOLS if t.key == "Vita3K")
        app = context(
            self.tmp,
            '[inbox]\nvita3k = "/x/V.app"\n',
            executables=["/x/V.app/Contents/MacOS/Vita3K"],
        )
        self.assertEqual(check_tool(app, spec).status, OK)
        binary = context(self.tmp, '[inbox]\nvita3k = "/x/vita3k"\n', executables=["/x/vita3k"])
        self.assertEqual(check_tool(binary, spec).status, OK)
        bad = context(self.tmp, '[inbox]\nvita3k = "/nope/V.app"\n')
        self.assertEqual(check_tool(bad, spec).status, FAIL)


class SwitchCheckTests(TmpCase):
    def titledb(self) -> Path:
        folder = self.tmp / "titledb"
        folder.mkdir()
        (folder / "US.en.json").write_text("{}")
        return folder

    def test_nothing_set_is_optional_for_both(self) -> None:
        results = check_switch(context(self.tmp, INBOX_ONLY))
        self.assertEqual([r.status for r in results], [OPTIONAL, OPTIONAL])
        self.assertIn("git clone", by_name(results, "titledb_dir").fix)
        self.assertIn(".switch/prod.keys", by_name(results, "switch_keys").detail)

    def test_titledb_dir_is_checked_for_region_files(self) -> None:
        good = check_switch(context(self.tmp, f'[inbox]\ntitledb_dir = "{self.titledb()}"\n'))
        self.assertEqual(by_name(good, "titledb_dir").status, OK)
        self.assertIn("1 region file", by_name(good, "titledb_dir").detail)
        empty = self.tmp / "empty"
        empty.mkdir()
        bad = check_switch(context(self.tmp, f'[inbox]\ntitledb_dir = "{empty}"\n'))
        self.assertEqual(by_name(bad, "titledb_dir").status, FAIL)
        gone = check_switch(context(self.tmp, '[inbox]\ntitledb_dir = "/nope/titledb"\n'))
        self.assertEqual(by_name(gone, "titledb_dir").status, FAIL)

    def test_default_keys_file_is_found_but_never_read(self) -> None:
        keys = self.tmp / "home" / ".switch" / "prod.keys"
        keys.parent.mkdir(parents=True)
        keys.write_text("master_key_00 = " + "ab" * 16 + "\n")  # gitleaks:allow (fake)
        results = check_switch(context(self.tmp, INBOX_ONLY))
        r = by_name(results, "switch_keys")
        self.assertEqual(r.status, OK)
        self.assertNotIn("abab", r.detail + r.fix)

    def test_configured_keys_path_must_exist(self) -> None:
        r = by_name(
            check_switch(context(self.tmp, '[inbox]\nswitch_keys = "/nope/k"\n')), "switch_keys"
        )
        self.assertEqual(r.status, FAIL)
        self.assertIn("/nope/k", r.detail)


def write_syncthing_config(home: Path, address: str = "127.0.0.1:8384") -> None:
    folder = home / ".local" / "state" / "syncthing"
    folder.mkdir(parents=True)
    (folder / "config.xml").write_text(
        f"<configuration><gui><address>{address}</address><apikey>{ST_KEY}</apikey></gui>"
        "</configuration>"
    )


class SyncthingTests(TmpCase):
    def test_not_installed_is_optional_with_a_platform_hint(self) -> None:
        ctx = context(self.tmp)
        r = check_syncthing(ctx)[0]
        self.assertEqual(r.status, OPTIONAL)
        self.assertIn("brew install syncthing", r.fix)

    def test_flatpak_install_is_found(self) -> None:
        folder = self.tmp / "home" / ".var/app/com.github.zocker_160.SyncThingy/config/syncthing"
        folder.mkdir(parents=True)
        (folder / "config.xml").write_text(
            f"<configuration><gui><address>127.0.0.1:8384</address><apikey>{ST_KEY}</apikey>"
            "</gui></configuration>"
        )
        r = check_syncthing(context(self.tmp, system="Linux", os_release=STEAMOS))[0]
        self.assertEqual(r.status, OK)

    def test_answering_api_is_ok_and_sends_the_key_only_as_a_header(self) -> None:
        write_syncthing_config(self.tmp / "home")
        seen: list[tuple[str, dict, float]] = []

        def status(url: str, headers: Mapping[str, str], timeout: float) -> int:
            seen.append((url, dict(headers), timeout))
            return 200

        r = check_syncthing(context(self.tmp, http_status=status, system="Linux"))[0]
        self.assertEqual(r.status, OK)
        self.assertEqual(seen[0][0], "http://127.0.0.1:8384/rest/system/ping")
        self.assertEqual(seen[0][1], {"X-API-Key": ST_KEY})
        self.assertLessEqual(seen[0][2], 5)
        self.assertNotIn(ST_KEY, r.detail + r.fix)

    def test_unreachable_api_fails_and_never_prints_the_key(self) -> None:
        write_syncthing_config(self.tmp / "home")

        def refuse(url: str, headers: Mapping[str, str], timeout: float) -> int:
            raise ConnectionRefusedError(61, f"Connection refused {ST_KEY}")

        r = check_syncthing(context(self.tmp, http_status=refuse, system="Linux"))[0]
        self.assertEqual(r.status, FAIL)
        self.assertIn("does not answer", r.detail)
        self.assertNotIn(ST_KEY, r.detail + r.fix)

    def test_refused_key_fails(self) -> None:
        write_syncthing_config(self.tmp / "home")
        ctx = context(self.tmp, http_status=lambda *a: 403, system="Linux")
        r = check_syncthing(ctx)[0]
        self.assertEqual(r.status, FAIL)
        self.assertIn("403", r.detail)

    def test_non_loopback_gui_is_not_probed(self) -> None:
        write_syncthing_config(self.tmp / "home", "192.0.2.10:8384")

        def never(url: str, headers: Mapping[str, str], timeout: float) -> int:
            raise AssertionError("must not probe")

        r = check_syncthing(context(self.tmp, http_status=never, system="Linux"))[0]
        self.assertEqual(r.status, WARN)

    def test_wildcard_gui_address_is_probed_on_loopback(self) -> None:
        write_syncthing_config(self.tmp / "home", "0.0.0.0:8384")
        urls: list[str] = []

        def status(url: str, headers: Mapping[str, str], timeout: float) -> int:
            urls.append(url)
            return 200

        check_syncthing(context(self.tmp, http_status=status, system="Linux"))
        self.assertEqual(urls, ["http://127.0.0.1:8384/rest/system/ping"])

    def test_config_without_an_api_key_fails(self) -> None:
        folder = self.tmp / "home" / ".local/state/syncthing"
        folder.mkdir(parents=True)
        (folder / "config.xml").write_text("<configuration><gui><address>127.0.0.1:1</address>")
        r = check_syncthing(context(self.tmp, system="Linux"))[0]
        self.assertEqual(r.status, FAIL)


def make_dats(directory: Path, names: Sequence[str], age_days: float) -> None:
    sub = directory / "redump"
    sub.mkdir(parents=True)
    for name in names:
        path = sub / f"{name}.dat"
        path.write_text("clrmamepro ()\n")
        stamp = 1_700_000_000 - age_days * 86400
        os.utime(path, (stamp, stamp))


class DatTests(TmpCase):
    NOW = 1_700_000_000.0

    def test_no_dats_dir_with_an_inbox_table_fails_with_the_clone_command(self) -> None:
        r = check_dats(context(self.tmp, INBOX_ONLY))[0]
        self.assertEqual(r.status, FAIL)
        self.assertIn("git clone --depth 1 --filter=blob:none --sparse", r.fix)
        self.assertIn("sparse-checkout set metadat/redump metadat/no-intro", r.fix)

    def test_no_dats_dir_without_an_inbox_table_is_optional(self) -> None:
        self.assertEqual(check_dats(context(self.tmp))[0].status, OPTIONAL)

    def test_missing_folder_fails(self) -> None:
        cfg = f'[inbox]\ndats_dir = "{self.tmp / "nope"}"\n'
        r = check_dats(context(self.tmp, cfg))[0]
        self.assertEqual(r.status, FAIL)
        self.assertIn("does not exist", r.detail)

    def test_folder_without_known_dats_fails(self) -> None:
        dats = self.tmp / "dats"
        make_dats(dats, ["Unknown - Thing"], 1)
        r = check_dats(context(self.tmp, f'[inbox]\ndats_dir = "{dats}"\n'))[0]
        self.assertEqual(r.status, FAIL)
        self.assertIn("no Redump or No-Intro DATs", r.detail.replace("holds no", "no"))

    def test_lists_systems_and_the_ones_without_a_dat(self) -> None:
        dats = self.tmp / "dats"
        make_dats(dats, ["Sony - PlayStation", "Sega - Saturn (20240101-000000)"], 10)
        ctx = context(self.tmp, f'[inbox]\ndats_dir = "{dats}"\n', now=lambda: self.NOW)
        folder, age = check_dats(ctx)
        self.assertEqual(folder.status, OK)
        self.assertIn("2 of 15 systems (psx, saturn)", folder.detail)
        self.assertIn("no DAT for", folder.detail)
        self.assertEqual(age.status, OK)
        self.assertIn("10 days", age.detail)

    def test_old_dats_warn_but_do_not_fail(self) -> None:
        dats = self.tmp / "dats"
        make_dats(dats, ["Sony - PlayStation"], 400)
        ctx = context(self.tmp, f'[inbox]\ndats_dir = "{dats}"\n', now=lambda: self.NOW)
        results = check_dats(ctx)
        age = by_name(results, "DAT age")
        self.assertEqual(age.status, WARN)
        self.assertIn("400 days", age.detail)
        self.assertIn("pull", age.fix)
        self.assertEqual(exit_code(results), 0)

    def test_the_newest_file_sets_the_age(self) -> None:
        dats = self.tmp / "dats"
        make_dats(dats, ["Sony - PlayStation"], 400)
        newer = dats / "redump" / "Sega - Saturn.dat"
        newer.write_text("x")
        os.utime(newer, (self.NOW - 86400, self.NOW - 86400))
        ctx = context(self.tmp, f'[inbox]\ndats_dir = "{dats}"\n', now=lambda: self.NOW)
        self.assertEqual(by_name(check_dats(ctx), "DAT age").status, OK)


class ArcadeDatTests(TmpCase):
    def test_nothing_set_is_optional_and_says_where_to_get_each_file(self) -> None:
        results = check_arcade_dats(context(self.tmp, INBOX_ONLY))
        self.assertEqual([r.status for r in results], [OPTIONAL] * 3)
        self.assertIn("mamedev/mame/releases", results[0].fix)
        self.assertIn("libretro/FBNeo", results[1].fix)
        self.assertIn("mame2003-plus-libretro", results[2].fix)
        self.assertEqual(exit_code(results), 0)

    def test_a_configured_file_is_ok_and_a_missing_one_is_a_problem(self) -> None:
        mame = self.tmp / "mame0282lx.zip"
        mame.write_bytes(b"x" * 2048)
        cfg = (
            f'[inbox]\nmame_dat = "{mame}"\nfbneo_dat = "{self.tmp / "nope.dat"}"\n'
            f'mame2003plus_dat = "{self.tmp}"\n'
        )
        results = check_arcade_dats(context(self.tmp, cfg))
        self.assertEqual([r.status for r in results], [OK, FAIL, FAIL])
        self.assertIn(str(mame), results[0].detail)
        self.assertIn("is not a file", results[1].detail)
        self.assertEqual(exit_code(results), 1)


class HubTests(TmpCase):
    def make_hub(self, *, inbox: bool = True, roms: bool = True) -> Path:
        root = self.tmp / "hub"
        root.mkdir()
        if inbox:
            (root / "inbox").mkdir()
        if roms:
            (root / "library" / "roms").mkdir(parents=True)
        return root

    def test_no_root_is_optional(self) -> None:
        r = check_hub(context(self.tmp))[0]
        self.assertEqual(r.status, OPTIONAL)

    def test_unmounted_share_fails_with_a_mount_hint(self) -> None:
        cfg = f'[inbox]\nroot = "{self.tmp / "gone"}"\n'
        r = check_hub(context(self.tmp, cfg))[0]
        self.assertEqual(r.status, FAIL)
        self.assertIn("mount", r.fix)

    def test_missing_layout_fails(self) -> None:
        root = self.make_hub(inbox=False)
        r = check_hub(context(self.tmp, f'[inbox]\nroot = "{root}"\n'))[0]
        self.assertEqual(r.status, FAIL)
        self.assertIn("inbox/", r.detail)
        self.assertNotIn("library", r.detail)

    def test_good_hub_reports_free_space(self) -> None:
        root = self.make_hub()
        results = check_hub(context(self.tmp, f'[inbox]\nroot = "{root}"\n'))
        self.assertEqual([r.status for r in results], [OK, OK])
        self.assertIn("400.0 GiB free of 500 GiB", results[1].detail)

    def test_low_space_warns(self) -> None:
        root = self.make_hub()
        ctx = context(
            self.tmp,
            f'[inbox]\nroot = "{root}"\n',
            disk_free=lambda path: (500 * GIB, 3 * GIB),
        )
        free = check_hub(ctx)[1]
        self.assertEqual(free.status, WARN)
        self.assertIn("below 20 GiB", free.detail)

    def test_unreadable_volume_warns(self) -> None:
        def broken(path: Path) -> tuple[int, int]:
            raise OSError(5, "Input/output error")

        root = self.make_hub()
        free = check_hub(context(self.tmp, f'[inbox]\nroot = "{root}"\n', disk_free=broken))[1]
        self.assertEqual(free.status, WARN)

    def test_hub_root_is_used_when_this_machine_is_the_hub(self) -> None:
        root = self.make_hub()
        cfg = FULL.replace('root = "/data/games"', f'root = "{root}"').replace(
            '[inbox]\nroot = "/mnt/hub"', "[inbox]\n"
        )
        results = check_hub(context(self.tmp, cfg))
        self.assertEqual(results[0].status, OK)


class RaTests(TmpCase):
    def test_set_key_is_reported_without_its_value(self) -> None:
        r = check_ra(context(self.tmp, env={"RA_API_KEY": RA_KEY}))[0]
        self.assertEqual(r.status, OK)
        self.assertIn("set", r.detail)
        for part in (RA_KEY, RA_KEY[:4], RA_KEY[-4:]):
            self.assertNotIn(part, r.detail + r.fix)

    def test_unset_or_empty_key_is_optional(self) -> None:
        for env in ({}, {"RA_API_KEY": ""}):
            r = check_ra(context(self.tmp, env=env))[0]
            self.assertEqual(r.status, OPTIONAL)
            self.assertIn("RA_API_KEY", r.fix)


class EndToEndTests(TmpCase):
    def test_full_report_never_contains_a_key(self) -> None:
        write_syncthing_config(self.tmp / "home")
        probes = make_probes(
            self.tmp,
            tools={"chdman": "/opt/bin/chdman", "bsdtar": "/usr/bin/bsdtar"},
            env={"RA_API_KEY": RA_KEY},
            system="Linux",
            os_release=DEBIAN,
        )
        probes.config_path.write_text(FULL.replace('"/mnt/hub"', f'"{self.tmp / "hub"}"'))
        lines: list[str] = []
        code = run_doctor(probes, lines.append)
        text = "\n".join(lines)
        self.assertEqual(code, 1)  # the hub folder and DATs are not there
        for secret in (RA_KEY, ST_KEY, API_KEY):
            self.assertNotIn(secret, text)
        for group in ("warpzone and Python", "Config", "Syncthing", "Inbox tools", "DATs"):
            self.assertIn(group, text)
        self.assertIn("RA_API_KEY", text)

    def test_run_checks_covers_every_group_in_order(self) -> None:
        probes = make_probes(self.tmp)
        probes.config_path.write_text(INBOX_ONLY)
        results = run_checks(probes)
        groups = list(dict.fromkeys(r.group for r in results))
        self.assertEqual(
            groups,
            [
                doctor.G_BASE,
                doctor.G_CONFIG,
                doctor.G_SYNCTHING,
                doctor.G_TOOLS,
                doctor.G_SWITCH,
                doctor.G_DATS,
                doctor.G_HUB,
                doctor.G_RA,
            ],
        )

    def test_without_an_inbox_table_the_optional_inbox_items_are_one_line(self) -> None:
        results = run_checks(make_probes(self.tmp))
        groups = list(dict.fromkeys(r.group for r in results))
        self.assertEqual(groups, [doctor.G_BASE, doctor.G_CONFIG, doctor.G_SYNCTHING, "Inbox"])
        (line,) = [r for r in results if r.group == "Inbox"]
        self.assertEqual(line.status, OK)
        self.assertIn("not set up on this machine", line.detail)
        self.assertIn("warpzone inbox", line.detail)
        # A hub config without [inbox] collapses as well.
        probes = make_probes(self.tmp)
        probes.config_path.write_text(FULL.split("[inbox]")[0])
        again = run_checks(probes)
        self.assertEqual([r.group for r in again if r.group == "Inbox"], ["Inbox"])
        self.assertNotIn(doctor.G_TOOLS, {r.group for r in again})

    def test_collapse_keeps_found_and_broken_items(self) -> None:
        results = [
            Result("chdman", doctor.G_TOOLS, OK, "found"),
            Result("bsdtar", doctor.G_TOOLS, OPTIONAL, "not found"),
            Result("RAHasher", doctor.G_TOOLS, FAIL, "cannot run"),
            Result("Hub folder", doctor.G_HUB, WARN, "looks like the hub"),
            Result("Local Syncthing", doctor.G_SYNCTHING, OPTIONAL, "not installed"),
        ]
        out = collapse_inbox(results)
        self.assertEqual(
            [r.name for r in out], ["chdman", "RAHasher", "Hub folder", "Local Syncthing", "Inbox"]
        )
        self.assertIn("(1 optional item(s)", out[-1].detail)
        # Nothing optional to hide: nothing added.
        self.assertEqual(collapse_inbox(results[:1]), results[:1])

    def test_the_collapsed_line_does_not_count_as_optional(self) -> None:
        text = "\n".join(render(run_checks(make_probes(self.tmp))))
        self.assertIn("Inbox", text)
        self.assertIn("2 optional item(s)", text)  # config and Syncthing, not the inbox items

    def test_a_machine_with_nothing_set_up_has_no_required_failure(self) -> None:
        results = run_checks(make_probes(self.tmp))
        self.assertEqual(exit_code(results), 0)

    def test_makes_no_changes_to_the_filesystem(self) -> None:
        write_syncthing_config(self.tmp / "home")
        before = sorted(p.relative_to(self.tmp) for p in self.tmp.rglob("*"))
        run_checks(make_probes(self.tmp, system="Linux"))
        self.assertEqual(sorted(p.relative_to(self.tmp) for p in self.tmp.rglob("*")), before)


class WidthTests(TmpCase):
    def run_with(self, width: int | None) -> list[str]:
        probes = make_probes(self.tmp, system="Linux", os_release=DEBIAN)
        lines: list[str] = []
        run_doctor(probes, lines.append, width)
        return lines

    def test_an_explicit_width_is_used(self) -> None:
        lines = self.run_with(60)
        self.assertTrue(any("fix:" in line for line in lines))
        narrow = self.run_with(50)
        wide = self.run_with(200)
        self.assertGreater(len(narrow), len(wide))

    def test_the_terminal_width_falls_back_to_100(self) -> None:
        with mock.patch.dict(os.environ, {}, clear=False) as env:
            env.pop("COLUMNS", None)
            with mock.patch("shutil.get_terminal_size", return_value=os.terminal_size((100, 24))):
                self.assertEqual(doctor.terminal_width(), 100)

    def test_without_a_width_the_terminal_decides(self) -> None:
        with mock.patch.object(doctor, "terminal_width", return_value=45):
            narrow = self.run_with(None)
        self.assertEqual(narrow, self.run_with(45))
        self.assertNotEqual(narrow, self.run_with(200))


class CliTests(TmpCase):
    def test_invalid_config_exits_1_with_the_checklist_on_stdout(self) -> None:
        path = self.tmp / "config.toml"
        path.write_text('[hub]\nname = "nas"\n')
        code, out, err = run("doctor", "--config", str(path))
        self.assertEqual(code, 1)
        self.assertIn("[!!] Config file", out)
        self.assertIn("hub.device_id", out)
        self.assertIn("problem(s)", out)
        self.assertEqual(err, "")

    def test_help_lists_the_command(self) -> None:
        code, out, _ = run("--help")
        self.assertEqual(code, 0)
        self.assertIn("doctor", out)


class FlycastVmuTests(TmpCase):
    OPT = ".var/app/org.libretro.RetroArch/config/retroarch/config/Flycast/Flycast.opt"

    def test_judge_per_game_vmus_on_is_ok(self) -> None:
        for value in ("VMU A1", "All VMUs"):
            status, detail, fix = judge_flycast_vmus(f'reicast_per_content_vmus = "{value}"\n')
            self.assertEqual(status, OK)
            self.assertIn(value, detail)
            self.assertEqual(fix, "")

    def test_judge_disabled_or_unset_warns_with_the_option_to_change(self) -> None:
        for text in (
            'reicast_per_content_vmus = "disabled"\n',
            'reicast_threaded_rendering = "enabled"\n',
            "",
        ):
            status, detail, fix = judge_flycast_vmus(text)
            self.assertEqual(status, WARN)
            self.assertIn("bios/dc", detail)
            self.assertIn("Per-Game", fix)
            self.assertIn("All VMUs", fix)
        self.assertIn('"disabled"', judge_flycast_vmus('reicast_per_content_vmus = "disabled"')[1])
        self.assertIn("unset", judge_flycast_vmus("")[1])

    def test_judge_reads_the_exact_key(self) -> None:
        text = 'reicast_per_content_vmus_other = "All VMUs"\n'
        self.assertEqual(judge_flycast_vmus(text)[0], WARN)

    def test_nothing_without_the_options_file(self) -> None:
        self.assertEqual(check_flycast(context(self.tmp)), [])

    def test_reads_the_options_file_in_the_retroarch_flatpak(self) -> None:
        path = self.tmp / "home" / self.OPT
        path.parent.mkdir(parents=True)
        path.write_text('reicast_per_content_vmus = "disabled"\n')
        (r,) = check_flycast(context(self.tmp))
        self.assertEqual((r.name, r.group, r.status), ("Flycast VMUs", doctor.G_SAVES, WARN))
        path.write_text('reicast_per_content_vmus = "All VMUs"\n')
        (r,) = check_flycast(context(self.tmp))
        self.assertEqual(r.status, OK)


class Vita3kUserTests(TmpCase):
    CONFIG = ".config/Vita3K/config.yml"

    def test_user_id_is_read_in_any_yaml_spelling(self) -> None:
        self.assertEqual(vita3k_user_id('lang: 1\nuser-id: "01"\nx: 2\n'), "01")
        self.assertEqual(vita3k_user_id("user-id: 00\n"), "00")
        self.assertEqual(vita3k_user_id("user-id: '00'  # first user\n"), "00")
        self.assertEqual(vita3k_user_id("user-id:   \n"), None)
        self.assertEqual(vita3k_user_id("other-user-id: 01\n"), None)
        self.assertEqual(vita3k_user_id("  user-id: 01\n"), None)  # nested key, not ours

    def test_judge(self) -> None:
        self.assertEqual(judge_vita3k_user("00")[0], OK)
        status, detail, fix = judge_vita3k_user("01")
        self.assertEqual(status, WARN)
        self.assertIn("user 00", detail)
        self.assertIn("ux0/user/01/savedata", fix)
        self.assertIn("ux0/user/00/savedata", fix)
        self.assertEqual(judge_vita3k_user("0")[0], WARN)  # the folder is called 00

    def write(self, text: str) -> None:
        path = self.tmp / "home" / self.CONFIG
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)

    def test_nothing_without_a_config_or_a_user(self) -> None:
        self.assertEqual(check_vita3k(context(self.tmp)), [])
        self.write("lang: 1\n")
        self.assertEqual(check_vita3k(context(self.tmp)), [])

    def test_reads_the_config_in_the_home_folder(self) -> None:
        self.write('user-id: "01"\n')
        (r,) = check_vita3k(context(self.tmp))
        self.assertEqual((r.name, r.group, r.status), ("Vita3K user", doctor.G_SAVES, WARN))
        self.write('user-id: "00"\n')
        (r,) = check_vita3k(context(self.tmp))
        self.assertEqual(r.status, OK)


SETTINGS_PROFILE = Profile(
    name="test",
    description="",
    settings={
        "retroarch": EmulatorSettings(
            id="retroarch",
            process=("retroarch",),
            files={"cfg": SettingsFile(id="cfg", path="~/r/retroarch.cfg", format="retroarch-cfg")},
        )
    },
)
LAYER = 'format = 1\n\n[retroarch.cfg]\nsavestate_auto_save = "true"\n'


def layers(common: str | None = LAYER, own: str | None = LAYER, names=("common.toml", "deck.toml")):
    return judge_settings_layers(
        SETTINGS_PROFILE,
        device="deck",
        folder="/home/user/settings",
        folder_exists=True,
        marker_exists=True,
        common=common,
        own=own,
        names=names,
    )


class SettingsJudgeTests(unittest.TestCase):
    def test_valid_layers_are_ok(self) -> None:
        status, detail, fix = layers()
        self.assertEqual(status, OK)
        self.assertIn("2 layer file(s)", detail)
        self.assertEqual(fix, "")

    def test_no_layer_files_yet_is_ok(self) -> None:
        status, detail, _ = layers(None, None, ())
        self.assertEqual(status, OK)
        self.assertIn("no layer files", detail)

    def test_a_missing_folder_or_marker_is_a_problem(self) -> None:
        kw = dict(device="deck", folder="/h/s", common=None, own=None, names=())
        status, detail, fix = judge_settings_layers(
            SETTINGS_PROFILE, folder_exists=False, marker_exists=False, **kw
        )
        self.assertEqual(status, FAIL)
        self.assertIn("/h/s does not exist", detail)
        self.assertIn("warpzone provision device deck", fix)
        status, detail, fix = judge_settings_layers(
            SETTINGS_PROFILE, folder_exists=True, marker_exists=False, **kw
        )
        self.assertEqual(status, FAIL)
        self.assertIn(".stfolder", detail)

    def test_a_layer_that_does_not_parse_or_validate_is_a_problem(self) -> None:
        for text in ("not = [toml", "[retroarch.cfg]\nk = 1\n", "format = 2\n"):
            with self.subTest(text=text):
                status, detail, fix = layers(own=text)
                self.assertEqual(status, FAIL)
                self.assertIn("deck.toml", detail)
                self.assertIn("changes nothing", fix)

    def test_a_secret_key_is_a_problem_named_without_its_value(self) -> None:
        secret = "hunter2-fake-value"  # gitleaks:allow (test fixture, not a real secret)
        status, detail, fix = layers(
            common=f'format = 1\n\n[retroarch.cfg]\ncheevos_token = "{secret}"\n'
        )
        self.assertEqual(status, FAIL)
        self.assertIn("cheevos_token", detail)
        self.assertNotIn(secret, detail + fix)

    def test_a_conflict_copy_is_a_problem(self) -> None:
        status, detail, _ = layers(
            names=("common.toml", "common.sync-conflict-20260101-000000-ABCDEFG.toml")
        )
        self.assertEqual(status, FAIL)
        self.assertIn("sync-conflict", detail)

    def test_an_unknown_emulator_or_file_is_only_a_warning(self) -> None:
        status, detail, _ = layers(own='format = 1\n\n[nothere.cfg]\nk = "v"\n')
        self.assertEqual(status, WARN)
        self.assertIn("nothere", detail)
        status, detail, _ = layers(own='format = 1\n\n[retroarch.other]\nk = "v"\n')
        self.assertEqual(status, WARN)

    def test_problems_outrank_warnings(self) -> None:
        text = 'format = 1\n\n[nothere.cfg]\nk = "v"\n[retroarch.cfg]\napi_key = "x"\n'
        status, detail, _ = layers(own=text)
        self.assertEqual(status, FAIL)
        self.assertIn("api_key", detail)


class EsdeJudgeTests(unittest.TestCase):
    def script(self, **kw):
        values = dict(
            device="deck",
            script="/h/ES-DE/scripts/startup/warpzone-settings.sh",
            exists=True,
            executable=True,
            interpreter="/opt/wz/bin/python",
            interpreter_ok=True,
        )
        values.update(kw)
        return judge_esde_script(**values)

    def test_installed_and_working_is_ok(self) -> None:
        status, detail, fix = self.script()
        self.assertEqual(status, OK)
        self.assertIn("/opt/wz/bin/python", detail)
        self.assertEqual(fix, "")

    def test_every_gap_is_a_warning_with_provision_as_the_fix(self) -> None:
        for change in (
            dict(exists=False, executable=False, interpreter=None, interpreter_ok=False),
            dict(executable=False),
            dict(interpreter=None, interpreter_ok=False),
            dict(interpreter_ok=False),
        ):
            with self.subTest(change=change):
                status, detail, fix = self.script(**change)
                self.assertEqual(status, WARN)
                self.assertTrue(detail)
                self.assertIn("warpzone provision device deck", fix)

    def test_the_setting_on_is_ok(self) -> None:
        text = '<bool name="CustomEventScripts" value="true" />\n'
        self.assertEqual(judge_event_scripts(text, "deck")[0], OK)

    def test_the_setting_off_absent_or_unreadable_is_a_warning(self) -> None:
        for text in (
            '<bool name="CustomEventScripts" value="false" />\n',
            '<bool name="Other" value="true" />\n',
            "<bool name=",
        ):
            with self.subTest(text=text):
                status, _, fix = judge_event_scripts(text, "deck")
                self.assertEqual(status, WARN)
                self.assertIn("warpzone provision device deck", fix)
                self.assertIn("Enable custom event scripts", fix)
                self.assertIn("ES-DE closed", fix)

    def test_a_missing_file_asks_to_start_esde_once(self) -> None:
        status, detail, fix = judge_event_scripts(None, "deck")
        self.assertEqual(status, WARN)
        self.assertIn("es_settings.xml", detail)
        self.assertIn("start ES-DE once", fix)


class LastApplyJudgeTests(unittest.TestCase):
    LOG = "/h/.local/state/warpzone/settings/log"

    def test_no_run_yet_is_optional(self) -> None:
        status, detail, _ = judge_last_apply(None, self.LOG)
        self.assertEqual(status, OPTIONAL)
        self.assertIn("no run yet", detail)

    def test_a_clean_run_is_ok(self) -> None:
        info = {"time": "2026-10-29T10:00:00", "exit": 0, "written": 3, "held": 0}
        status, detail, _ = judge_last_apply(info, self.LOG)
        self.assertEqual(status, OK)
        self.assertIn("2026-10-29T10:00:00", detail)

    def test_a_failed_run_points_at_the_log(self) -> None:
        for key in ("exit", "exit_code"):
            status, detail, fix = judge_last_apply({"time": "t", key: 1}, self.LOG)
            self.assertEqual(status, WARN)
            self.assertIn("failed", detail)
            self.assertIn(self.LOG, fix)

    def test_held_keys_point_at_capture(self) -> None:
        status, detail, fix = judge_last_apply({"time": "t", "exit": 0, "held": 2}, self.LOG)
        self.assertEqual(status, WARN)
        self.assertIn("2 key(s)", detail)
        self.assertIn("warpzone settings capture", fix)

    def test_odd_content_does_not_raise(self) -> None:
        status, _, _ = judge_last_apply({"exit": "x", "held": None, "time": 5}, self.LOG)
        self.assertEqual(status, OK)


class SettingsCheckTests(TmpCase):
    CONFIG = FULL.replace(
        'folders = ["library", "saves"]', 'folders = ["saves", "settings", "frontend"]'
    )

    def setUp(self) -> None:
        super().setUp()
        self.home = self.tmp / "home"
        self.folder = self.home / ".local/share/warpzone/settings"
        self.esde = self.home / "ES-DE"
        self.state = self.home / ".local/state/warpzone/settings"

    def syncthing(self, path: Path | None = None, ids=(DECK_ID, HUB_ID)) -> None:
        folder = self.home / ".config/syncthing"
        folder.mkdir(parents=True, exist_ok=True)
        devices = "".join(f'<device id="{i}" name="x"/>' for i in ids)
        shared = f'<device id="{HUB_ID}"/>'
        wz = f'<folder id="wz-settings" path="{path or self.folder}/">{shared}</folder>'
        (folder / "config.xml").write_text(
            f"<configuration>{wz}{devices}<gui><address>127.0.0.1:8384</address>"
            f"<apikey>{ST_KEY}</apikey></gui></configuration>"
        )

    def set_up_device(self) -> None:
        self.syncthing()
        (self.folder / ".stfolder").mkdir(parents=True)
        (self.folder / "common.toml").write_text("format = 1\n")
        script = esde.script_path(self.esde)
        script.parent.mkdir(parents=True)
        script.write_text(esde.render_startup_script(sys.executable, "deck"))
        script.chmod(0o755)
        settings = esde.settings_path(self.esde)
        settings.parent.mkdir(parents=True)
        settings.write_text('<bool name="CustomEventScripts" value="true" />\n')
        self.state.mkdir(parents=True)
        (self.state / "last-apply.json").write_text(
            json.dumps({"time": "2026-10-29T10:00:00", "exit": 0, "written": 1, "held": 0})
        )

    def check(self, config_text: str | None = None, **kw):
        kw.setdefault("executables", [str(esde.script_path(self.esde)), sys.executable])
        return check_settings(
            context(
                self.tmp, self.CONFIG if config_text is None else config_text, system="Linux", **kw
            )
        )

    def test_a_working_device_has_three_ok_lines(self) -> None:
        self.set_up_device()
        results = self.check()
        self.assertEqual(
            [(r.name, r.group, r.status) for r in results],
            [
                ("Settings layers", doctor.G_SETTINGS, OK),
                ("ES-DE startup script", doctor.G_SETTINGS, OK),
                ("Last settings apply", doctor.G_SETTINGS, OK),
            ],
        )

    def test_each_gap_shows_on_its_own_line(self) -> None:
        self.set_up_device()
        esde.script_path(self.esde).unlink()
        esde.settings_path(self.esde).write_text('<bool name="CustomEventScripts" value="false" />')
        (self.state / "last-apply.json").unlink()
        (self.folder / "common.toml").write_text("format = 9\n")
        results = self.check()
        self.assertEqual([r.status for r in results], [FAIL, WARN, OPTIONAL])
        script = by_name(results, "ES-DE startup script")
        self.assertIn("no startup script", script.detail)
        self.assertIn("custom event scripts are off", script.detail)
        self.assertEqual(exit_code(results), 1)

    def test_an_interpreter_that_is_gone_is_a_warning(self) -> None:
        self.set_up_device()
        esde.script_path(self.esde).write_text(esde.render_startup_script("/gone/python", "deck"))
        script = by_name(self.check(), "ES-DE startup script")
        self.assertEqual(script.status, WARN)
        self.assertIn("/gone/python", script.detail)

    def test_a_missing_esde_settings_file_is_a_warning(self) -> None:
        self.set_up_device()
        esde.settings_path(self.esde).unlink()
        script = by_name(self.check(), "ES-DE startup script")
        self.assertEqual(script.status, WARN)
        self.assertIn("start ES-DE once", script.fix)

    def test_no_secret_value_reaches_the_output(self) -> None:
        self.set_up_device()
        secret = "hunter2-fake-value"  # gitleaks:allow (test fixture, not a real secret)
        (self.folder / "deck.toml").write_text(
            f'format = 1\n\n[retroarch.cfg]\ncheevos_token = "{secret}"\n'
        )
        (self.state / "last-apply.json").write_text(json.dumps({"exit": 0, "token": secret}))
        text = "\n".join(render(self.check()))
        self.assertIn("cheevos_token", text)
        self.assertNotIn(secret, text)

    def test_conflict_copies_are_found_in_the_folder(self) -> None:
        self.set_up_device()
        (self.folder / "files/xemu").mkdir(parents=True)
        (self.folder / "files/xemu/eeprom.sync-conflict-20260101-000000-ABCDEFG.bin").write_text(
            "x"
        )
        layers_result = by_name(self.check(), "Settings layers")
        self.assertEqual(layers_result.status, FAIL)
        self.assertIn("sync-conflict", layers_result.detail)

    def test_a_missing_folder_is_a_problem(self) -> None:
        self.set_up_device()
        shutil.rmtree(self.folder)
        r = by_name(self.check(), "Settings layers")
        self.assertEqual(r.status, FAIL)
        self.assertIn("warpzone provision device deck", r.fix)

    def test_nothing_to_say_when_this_machine_is_not_a_settings_device(self) -> None:
        self.set_up_device()
        no_settings = FULL.replace('folders = ["library", "saves"]', 'folders = ["saves"]')
        self.assertEqual(self.check(no_settings), [])  # the device does not subscribe
        self.assertEqual(self.check(""), [])  # no config
        self.assertEqual(self.check(FULL.replace("[devices.deck]", "[devices.other]")), [])
        # the hub: it knows the device, but its settings folder lives elsewhere
        self.syncthing(path=self.tmp / "data/games/settings")
        self.assertEqual(self.check(), [])
        # no Syncthing at all
        (self.home / ".config/syncthing/config.xml").unlink()
        self.assertEqual(self.check(), [])

    def test_another_device_in_the_config_is_not_this_one(self) -> None:
        self.set_up_device()
        self.syncthing(ids=(HUB_ID,))
        self.assertEqual(self.check(), [])

    def test_a_profile_without_an_esde_path_skips_that_line(self) -> None:
        self.set_up_device()
        with_settings = load_profile("steamos-emudeck")
        bare = dataclasses.replace(
            with_settings, paths={k: v for k, v in with_settings.paths.items() if k != "esde"}
        )
        ctx = context(self.tmp, self.CONFIG, system="Linux")
        device = dataclasses.replace(ctx.config.settings_devices[0], profile=bare)
        ctx = Context(ctx.probes, dataclasses.replace(ctx.config, settings_devices=(device,)))
        self.assertEqual(
            [r.name for r in check_settings(ctx)], ["Settings layers", "Last settings apply"]
        )

    def test_the_run_includes_the_settings_checks(self) -> None:
        self.set_up_device()
        probes = make_probes(self.tmp, system="Linux")
        probes.config_path.write_text(self.CONFIG)
        names = [r.name for r in run_checks(probes)]
        self.assertIn("Settings layers", names)


if __name__ == "__main__":
    unittest.main()


UNIT = (
    "[Unit]\nDescription=Syncthing\n\n[Service]\n"
    "ExecStart=/usr/bin/flatpak run --command=syncthing com.github.zocker_160.SyncThingy serve\n"
    "Restart=on-failure\n\n[Install]\nWantedBy=default.target\n"
)
DESKTOP = """[Desktop Entry]
Type=Application
Name=SyncThingy
Exec=flatpak run com.github.zocker_160.SyncThingy
"""
OTHER_DESKTOP = "[Desktop Entry]\nType=Application\nExec=/usr/bin/steam\n"


class JudgeGameModeTests(unittest.TestCase):
    def test_enabled_unit_is_ok(self) -> None:
        status, detail, fix = judge_game_mode(
            {"syncthing.service": UNIT}, {"syncthing.service"}, {}
        )
        self.assertEqual(status, OK)
        self.assertIn("Game Mode", detail)
        self.assertEqual(fix, "")

    def test_enabled_unit_plus_autostart_warns_about_two_instances(self) -> None:
        status, detail, fix = judge_game_mode(
            {"syncthing.service": UNIT}, {"syncthing.service"}, {"syncthingy.desktop": DESKTOP}
        )
        self.assertEqual(status, WARN)
        self.assertIn("two instances", detail)
        self.assertIn("remove ~/.config/autostart/syncthingy.desktop", fix)

    def test_autostart_only_warns_and_shows_the_unit(self) -> None:
        status, detail, fix = judge_game_mode({}, set(), {"syncthingy.desktop": DESKTOP})
        self.assertEqual(status, WARN)
        self.assertIn("desktop autostart", detail)
        self.assertIn("never runs", detail)
        self.assertIn(doctor.SYNCTHINGY_EXEC, fix)
        self.assertIn("systemctl --user enable --now syncthing.service", fix)
        self.assertIn("remove ~/.config/autostart/syncthingy.desktop", fix)
        self.assertIn("two instances", fix)

    def test_nothing_at_all_warns_without_an_autostart_step(self) -> None:
        status, detail, fix = judge_game_mode({}, set(), {})
        self.assertEqual(status, WARN)
        self.assertIn("no systemd user unit", detail)
        self.assertIn("ExecStart=" + doctor.SYNCTHINGY_EXEC, fix)
        self.assertNotIn("remove", fix)

    def test_a_unit_that_is_not_enabled_warns_and_names_it(self) -> None:
        status, detail, fix = judge_game_mode({"st.service": UNIT}, set(), {})
        self.assertEqual(status, WARN)
        self.assertIn("st.service", detail)
        self.assertEqual(fix, "systemctl --user enable --now st.service")

    def test_other_units_and_entries_do_not_count(self) -> None:
        other = "[Service]\nExecStart=/usr/bin/other\n"
        status, _, _ = judge_game_mode(
            {"other.service": other}, {"other.service"}, {"steam.desktop": OTHER_DESKTOP}
        )
        self.assertEqual(status, WARN)

    def test_a_comment_or_description_mentioning_syncthing_does_not_count(self) -> None:
        text = "[Unit]\nDescription=Syncthing helper\n[Service]\nExecStart=/usr/bin/other\n"
        status, _, _ = judge_game_mode({"x.service": text}, {"x.service"}, {})
        self.assertEqual(status, WARN)


class CodeLineTests(unittest.TestCase):
    def test_indented_fix_lines_are_never_wrapped(self) -> None:
        command = "ExecStart=" + "word " * 30
        lines = render([Result("x", "G", WARN, "d", f"intro\n  {command}")], 60)
        (shown,) = [line for line in lines if "ExecStart=" in line]
        self.assertTrue(shown.strip().startswith(command.rstrip()))


class GameModeTests(TmpCase):
    def setUp(self) -> None:
        super().setUp()
        self.home = self.tmp / "home"
        write_syncthing_config(self.home)

    def put(self, rel: str, text: str = "") -> None:
        path = self.home / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)

    def check(self, **kw):
        return check_game_mode(context(self.tmp, system="Linux", os_release=STEAMOS, **kw))

    def test_only_on_steamos(self) -> None:
        self.assertEqual(check_game_mode(context(self.tmp, system="Linux", os_release=DEBIAN)), [])
        self.assertEqual(check_game_mode(context(self.tmp)), [])

    def test_skipped_when_syncthing_is_not_installed(self) -> None:
        (self.home / ".local/state/syncthing/config.xml").unlink()
        self.assertEqual(self.check(), [])

    def test_reads_units_and_autostart_entries_from_the_home_folder(self) -> None:
        self.put(".config/autostart/syncthingy.desktop", DESKTOP)
        (r,) = self.check()
        self.assertEqual((r.name, r.status), ("Game Mode", WARN))
        self.put(".config/systemd/user/syncthing.service", UNIT)
        link = self.home / ".config/systemd/user/default.target.wants/syncthing.service"
        link.parent.mkdir(parents=True)
        link.symlink_to(self.home / ".config/systemd/user/syncthing.service")
        (r,) = self.check()
        self.assertEqual(r.status, WARN)  # the autostart entry is still there
        self.assertIn("two instances", r.detail)
        (self.home / ".config/autostart/syncthingy.desktop").unlink()
        (r,) = self.check()
        self.assertEqual(r.status, OK)

    def test_a_broken_symlink_still_counts_as_enabled_link_name(self) -> None:
        self.put(".config/systemd/user/syncthing.service", UNIT)
        link = self.home / ".config/systemd/user/default.target.wants/syncthing.service"
        link.parent.mkdir(parents=True)
        link.symlink_to(self.tmp / "gone")
        (r,) = self.check()
        self.assertEqual(r.status, OK)

    def test_run_checks_includes_it_in_the_syncthing_group(self) -> None:
        results = run_checks(make_probes(self.tmp, system="Linux", os_release=STEAMOS))
        self.assertEqual(by_name(results, "Game Mode").group, doctor.G_SYNCTHING)


LOOPING = "ActiveState=activating\nSubState=auto-restart\n"
RUNNING = "ActiveState=active\nSubState=running\n"
STOPPED = "ActiveState=inactive\nSubState=dead\n"
UNIT_WITH_STOP = UNIT + "ExecStop=/usr/bin/flatpak kill com.github.zocker_160.SyncThingy\n"


class JudgeUnitStateTests(unittest.TestCase):
    def test_running_is_ok(self) -> None:
        status, detail, fix = judge_unit_state(RUNNING, flatpak=True, stops_flatpak=False)
        self.assertEqual((status, fix), (OK, ""))
        self.assertIn("running", detail)

    def test_stopped_or_unknown_says_nothing(self) -> None:
        self.assertIsNone(judge_unit_state(STOPPED, flatpak=True, stops_flatpak=False))
        self.assertIsNone(judge_unit_state("", flatpak=True, stops_flatpak=False))

    def test_restart_loop_with_the_flatpak_shows_the_unit_with_exec_stop(self) -> None:
        status, detail, fix = judge_unit_state(LOOPING, flatpak=True, stops_flatpak=False)
        self.assertEqual(status, WARN)
        self.assertIn("auto-restart", detail)
        self.assertIn("scope of its own", detail)
        self.assertIn("flatpak kill com.github.zocker_160.SyncThingy", fix)
        self.assertIn("ExecStart=" + doctor.SYNCTHINGY_EXEC, fix)
        self.assertIn("ExecStop=/usr/bin/flatpak kill com.github.zocker_160.SyncThingy", fix)
        self.assertIn("systemctl --user restart syncthing", fix)

    def test_restart_loop_with_a_unit_that_already_stops_the_flatpak(self) -> None:
        status, _, fix = judge_unit_state(LOOPING, flatpak=True, stops_flatpak=True)
        self.assertEqual(status, WARN)
        self.assertNotIn("ExecStart=", fix)
        self.assertIn("flatpak kill", fix)
        self.assertIn("systemctl --user restart syncthing", fix)

    def test_restart_loop_without_the_flatpak_does_not_mention_it(self) -> None:
        status, detail, fix = judge_unit_state(LOOPING, flatpak=False, stops_flatpak=False)
        self.assertEqual(status, WARN)
        self.assertNotIn("flatpak", detail + fix)
        self.assertIn("journalctl", fix)

    def test_a_bare_activating_state_counts_too(self) -> None:
        out = "ActiveState=activating\nSubState=start\n"
        self.assertEqual(judge_unit_state(out, flatpak=True, stops_flatpak=False)[0], WARN)

    def test_the_unit_shown_by_the_game_mode_fix_has_exec_stop(self) -> None:
        self.assertIn("ExecStop=" + doctor.SYNCTHINGY_STOP, doctor.SYNCTHING_UNIT)


class SyncthingUnitTests(TmpCase):
    def setUp(self) -> None:
        super().setUp()
        self.home = self.tmp / "home"
        write_syncthing_config(self.home)
        self.commands: list[tuple[str, ...]] = []

    def check(self, output: str = LOOPING, code: int = 0, **kw):
        def run(command):
            self.commands.append(tuple(command))
            return RunOutput(code, output)

        return check_syncthing_unit(context(self.tmp, system="Linux", run=run, **kw))

    def test_asks_systemd_for_the_user_unit(self) -> None:
        self.check()
        self.assertEqual(
            self.commands,
            [("systemctl", "--user", "show", "-p", "ActiveState,SubState", "syncthing")],
        )

    def test_warns_in_the_syncthing_group(self) -> None:
        (r,) = self.check()
        self.assertEqual((r.name, r.group, r.status), ("Syncthing unit", doctor.G_SYNCTHING, WARN))
        self.assertIn("journalctl", r.fix)  # config.xml is not the flatpak's here

    def test_flatpak_config_gets_the_flatpak_fix(self) -> None:
        (self.home / ".local/state/syncthing/config.xml").unlink()
        folder = self.home / ".var/app/com.github.zocker_160.SyncThingy/config/syncthing"
        folder.mkdir(parents=True)
        (folder / "config.xml").write_text(
            f"<configuration><gui><address>127.0.0.1:8384</address><apikey>{ST_KEY}</apikey>"
            "</gui></configuration>"
        )
        (r,) = self.check()
        self.assertIn("ExecStop=", r.fix)
        unit = self.home / ".config/systemd/user/syncthing.service"
        unit.parent.mkdir(parents=True)
        unit.write_text(UNIT_WITH_STOP)
        (r,) = self.check()
        self.assertNotIn("ExecStart=", r.fix)

    def test_running_unit_is_ok_and_a_stopped_one_is_silent(self) -> None:
        self.assertEqual(self.check(RUNNING)[0].status, OK)
        self.assertEqual(self.check(STOPPED), [])

    def test_skipped_off_linux_without_syncthing_or_without_systemctl(self) -> None:
        self.assertEqual(check_syncthing_unit(context(self.tmp)), [])  # macOS
        self.assertEqual(self.check(code=1), [])  # no user manager
        (self.home / ".local/state/syncthing/config.xml").unlink()
        self.assertEqual(self.check(), [])
        self.home.joinpath(".local/state/syncthing/config.xml").write_text("<configuration/>")
        self.assertEqual(
            check_syncthing_unit(context(self.tmp, system="Linux")), []
        )  # no systemctl


class UpdateTimerJudgeTests(unittest.TestCase):
    def judge(self, **kw):
        values = {"device": "deck", "files": True, "enabled": True, "active": True}
        return judge_update_timer(**{**values, **kw})

    def test_installed_and_running_is_ok(self) -> None:
        status, detail, fix = self.judge()
        self.assertEqual((status, fix), (OK, ""))
        self.assertIn("15 minutes", detail)

    def test_missing_units_are_a_warning_that_names_the_fix(self) -> None:
        status, detail, fix = self.judge(files=False, enabled=False, active=False)
        self.assertEqual(status, WARN)
        self.assertIn("not installed", detail)
        self.assertIn("warpzone provision device deck", fix)
        self.assertIn("self_update = false", fix)

    def test_units_that_are_not_enabled_or_not_active_are_a_warning(self) -> None:
        for enabled, active in ((False, False), (True, False), (False, True)):
            status, _, fix = self.judge(enabled=enabled, active=active)
            self.assertEqual(status, WARN, (enabled, active))
            self.assertIn("systemctl --user enable --now warpzone-self-update.timer", fix)

    def test_a_systemd_that_cannot_be_asked_is_a_warning(self) -> None:
        status, detail, _ = self.judge(enabled=None, active=None)
        self.assertEqual(status, WARN)
        self.assertIn("cannot ask systemd", detail)

    def test_a_service_that_does_not_run_the_updater_is_a_warning(self) -> None:
        # An old unit that still runs `python -m warpzone self-update` every day.
        status, detail, fix = self.judge(command_ok=False)
        self.assertEqual(status, WARN)
        self.assertIn("updater", detail)
        self.assertIn("warpzone provision device deck", fix)


class UpdateTimerCheckTests(TmpCase):
    def setUp(self) -> None:
        super().setUp()
        self.home = self.tmp / "home"
        folder = self.home / ".config/syncthing"
        folder.mkdir(parents=True)
        (folder / "config.xml").write_text(
            f'<configuration><folder id="wz-library" path="{self.home}/Emulation/">'
            f'<device id="{HUB_ID}"/></folder><device id="{DECK_ID}" name="x"/>'
            f"<gui><address>127.0.0.1:8384</address><apikey>{ST_KEY}</apikey></gui>"
            "</configuration>"
        )
        self.commands: list[tuple[str, ...]] = []

    def units(self, *names: str) -> None:
        folder = self.home / ".config/systemd/user"
        folder.mkdir(parents=True, exist_ok=True)
        for name in names:
            (folder / name).write_text("[Unit]\n")

    def check(self, *, enabled=True, active=True, bus=True, config=None, **kw):
        def run(command):
            self.commands.append(tuple(command))
            if not bus:
                raise FileNotFoundError("systemctl")
            verb = command[2]
            if verb == "is-enabled":
                return RunOutput(0 if enabled else 1, "enabled" if enabled else "disabled")
            return RunOutput(0 if active else 3, "active" if active else "inactive")

        text = FULL.replace(
            "BBBBBBB-BBBBBBB-BBBBBBB-BBBBBBB-BBBBBBB-BBBBBBB-BBBBBBB-BBBBBBB", DECK_ID
        )
        return check_update_timer(
            context(self.tmp, config or text, system="Linux", run=run, home=self.home, **kw)
        )

    def both(self) -> None:
        self.units("warpzone-self-update.timer")
        live = selfupdate.live_path(self.home)
        (self.home / updatetimer.UNIT_DIR / updatetimer.SERVICE).write_text(
            updatetimer.render_service(str(live))
        )

    def test_a_device_with_the_timer_running_is_ok(self) -> None:
        self.both()
        (r,) = self.check()
        self.assertEqual((r.name, r.group, r.status), ("Self-update timer", doctor.G_UPDATE, OK))
        self.assertIn(
            ("systemctl", "--user", "is-enabled", "warpzone-self-update.timer"), self.commands
        )

    def test_a_service_that_runs_something_else_gets_a_warning(self) -> None:
        self.units("warpzone-self-update.timer")
        (self.home / updatetimer.UNIT_DIR / updatetimer.SERVICE).write_text(
            "[Service]\nExecStart=/usr/bin/python3 -m warpzone self-update\n"
        )
        (r,) = self.check()
        self.assertEqual(r.status, WARN)
        self.assertIn("updater", r.detail)

    def test_a_device_without_the_timer_gets_a_warning(self) -> None:
        (r,) = self.check(enabled=False, active=False)
        self.assertEqual(r.status, WARN)
        self.assertIn("provision device deck", r.fix)

    def test_a_timer_that_is_switched_off_gets_a_warning(self) -> None:
        self.both()
        (r,) = self.check(enabled=False, active=False)
        self.assertEqual(r.status, WARN)
        self.assertIn("enable --now", r.fix)

    def test_without_systemctl_the_units_are_judged_by_their_files(self) -> None:
        (r,) = self.check(bus=False)
        self.assertEqual(r.status, WARN)
        self.both()
        (r,) = self.check(bus=False)
        self.assertEqual(r.status, WARN)
        self.assertIn("cannot ask systemd", r.detail)

    def test_silent_when_the_config_turns_it_off(self) -> None:
        off = FULL.replace(
            'folders = ["library", "saves"]', 'folders = ["library", "saves"]\nself_update = false'
        ).replace("BBBBBBB-BBBBBBB-BBBBBBB-BBBBBBB-BBBBBBB-BBBBBBB-BBBBBBB-BBBBBBB", DECK_ID)
        self.assertEqual(self.check(config=off), [])

    def test_silent_off_linux_and_on_a_machine_that_is_not_a_device(self) -> None:
        self.assertEqual(check_update_timer(context(self.tmp, FULL, home=self.home)), [])  # macOS
        (self.home / ".config/syncthing/config.xml").unlink()
        self.assertEqual(self.check(), [])

    def test_runs_with_the_other_checks(self) -> None:
        self.assertIn(check_update_timer, doctor.CHECKS)


NOW = 1_800_000_000  # 2027-01-15T08:00:00Z
DAY = 86400


class UpdaterStateJudgeTests(unittest.TestCase):
    STATE: ClassVar[dict[str, str]] = {
        "last_result": "ok",
        "last_ok": str(NOW - 3600),
        "signing": "1",
        "pin_retired": "",
    }

    def judge(self, state="default", *, bad=None, now=NOW):
        state = dict(self.STATE) if state == "default" else state
        return judge_updater_state(state=state, bad=bad or {}, now=now, device="deck")

    def statuses(self, *args, **kw) -> list[str]:
        return [status for status, _, _ in self.judge(*args, **kw)]

    def test_a_healthy_updater_is_one_ok_line(self) -> None:
        ((status, detail, fix),) = self.judge()
        self.assertEqual((status, fix), (OK, ""))
        self.assertIn("ok", detail)

    def test_an_updater_that_has_not_run_yet_is_a_warning(self) -> None:
        ((status, detail, fix),) = self.judge(None)
        self.assertEqual(status, WARN)
        self.assertIn("has not run", detail)
        self.assertIn("warpzone self-update", fix)

    def test_a_recent_rollback_names_the_tag_and_the_reason(self) -> None:
        bad = {"v0.3.1": selfupdate.Bad("abc", NOW - 2 * DAY, "health failed (exit 1)")}
        found = self.judge(bad=bad)
        self.assertEqual([s for s, _, _ in found], [WARN])
        self.assertIn("v0.3.1", found[0][1])
        self.assertIn("health failed", found[0][1])

    def test_a_last_result_of_rolled_back_is_a_warning_too(self) -> None:
        state = {**self.STATE, "last_result": "rolled-back"}
        self.assertEqual(self.statuses(state), [WARN])

    def test_an_old_bad_tag_is_not_a_recent_rollback(self) -> None:
        bad = {"v0.3.1": selfupdate.Bad("abc", NOW - 20 * DAY, "x")}
        self.assertEqual(self.statuses(bad=bad), [OK])

    def test_a_bad_tag_with_no_date_counts_as_recent(self) -> None:
        bad = {"v0.3.1": selfupdate.Bad("abc", None, "x")}
        self.assertEqual(self.statuses(bad=bad), [WARN])

    def test_stuck_is_a_failure_that_needs_a_person(self) -> None:
        found = self.judge({**self.STATE, "last_result": "stuck"})
        self.assertEqual(found[0][0], FAIL)
        self.assertIn("stuck", found[0][1])
        self.assertIn("git", found[0][2])

    def test_a_dirty_or_dev_checkout_is_a_warning_that_says_it_is_not_moved(self) -> None:
        for result in ("dirty", "dev"):
            with self.subTest(result=result):
                found = self.judge({**self.STATE, "last_result": result})
                self.assertEqual([s for s, _, _ in found], [WARN])
                self.assertIn(result, found[0][1])
                self.assertIn("not moved", found[0][1])

    def test_no_python_is_a_warning(self) -> None:
        self.assertEqual(self.statuses({**self.STATE, "last_result": "no-python"}), [WARN])

    def test_a_retired_pin_is_a_warning_that_names_it(self) -> None:
        found = self.judge({**self.STATE, "pin_retired": "v0.3.0"})
        self.assertEqual([s for s, _, _ in found], [WARN])
        self.assertIn("v0.3.0", found[0][1])
        self.assertIn("pin", found[0][1])

    def test_signatures_that_are_not_required_are_a_warning(self) -> None:
        found = self.judge({**self.STATE, "signing": "0"})
        self.assertEqual([s for s, _, _ in found], [WARN])
        self.assertIn("signature", found[0][1])

    def test_more_than_fourteen_days_without_a_success_is_a_warning(self) -> None:
        old = {**self.STATE, "last_ok": str(NOW - 15 * DAY)}
        found = self.judge(old)
        self.assertEqual([s for s, _, _ in found], [WARN])
        self.assertIn("15 days", found[0][1])
        self.assertEqual(self.statuses({**self.STATE, "last_ok": str(NOW - 13 * DAY)}), [OK])

    def test_no_success_at_all_is_a_warning(self) -> None:
        state = {k: v for k, v in self.STATE.items() if k != "last_ok"}
        found = self.judge(state)
        self.assertEqual([s for s, _, _ in found], [WARN])
        self.assertIn("never", found[0][1])

    def test_a_deferred_or_env_last_result_is_not_a_problem_by_itself(self) -> None:
        for result in ("deferred", "env"):
            self.assertEqual(self.statuses({**self.STATE, "last_result": result}), [OK])

    def test_several_problems_give_several_lines(self) -> None:
        state = {**self.STATE, "last_result": "dirty", "signing": "0"}
        self.assertEqual(self.statuses(state), [WARN, WARN])

    def test_a_last_ok_from_the_future_is_not_old(self) -> None:
        self.assertEqual(self.statuses({**self.STATE, "last_ok": str(NOW + 5 * DAY)}), [OK])


class UpdaterFilesJudgeTests(unittest.TestCase):
    EXPECTED: ClassVar[dict[str, str]] = {
        "device": "deck",
        "config": "/c/config.toml",
        "channel": "stable",
        "pin": "",
        "enabled": "true",
        "frontend": "es-de ES-DE.AppImage",
        "install": "auto",
    }

    def conf(self, **change):
        return {**self.EXPECTED, "clone": "/s", "python": "/v/python", **change}

    def judge_conf(self, actual="same", **kw):
        values = {"python_ok": True, "clone_ok": True, "device": "deck"}
        actual = self.conf() if actual == "same" else actual
        return judge_updater_conf(actual=actual, expected=self.EXPECTED, **{**values, **kw})

    def test_matching_settings_are_ok(self) -> None:
        status, _, fix = self.judge_conf()
        self.assertEqual((status, fix), (OK, ""))

    def test_missing_settings_are_a_warning_with_the_fix(self) -> None:
        status, detail, fix = self.judge_conf(None)
        self.assertEqual(status, WARN)
        self.assertIn("missing", detail)
        self.assertIn("warpzone provision device deck", fix)

    def test_settings_that_differ_name_the_keys(self) -> None:
        status, detail, fix = self.judge_conf(self.conf(channel="rc", pin="v0.3.0"))
        self.assertEqual(status, WARN)
        self.assertIn("channel", detail)
        self.assertIn("pin", detail)
        self.assertNotIn("device", detail)
        self.assertIn("warpzone provision device deck", fix)

    def test_a_python_or_a_clone_that_is_gone_is_a_warning(self) -> None:
        for kw, word in (({"python_ok": False}, "python"), ({"clone_ok": False}, "clone")):
            status, detail, _ = self.judge_conf(**kw)
            self.assertEqual(status, WARN)
            self.assertIn(word, detail)

    def test_an_install_mode_that_differs_is_a_difference(self) -> None:
        status, detail, _ = self.judge_conf(self.conf(install="manual"))
        self.assertEqual(status, WARN)
        self.assertIn("install", detail)

    def test_a_missing_key_is_a_difference(self) -> None:
        actual = self.conf()
        del actual["frontend"]
        status, detail, _ = self.judge_conf(actual)
        self.assertEqual(status, WARN)
        self.assertIn("frontend", detail)

    def test_the_live_copy_is_ok_when_it_is_the_shipped_one(self) -> None:
        status, _, fix = judge_updater_script(
            live="UPDATER_ABI=1\nx\n", shipped="UPDATER_ABI=1\nx\n", path="/u", device="deck"
        )
        self.assertEqual((status, fix), (OK, ""))

    def test_a_missing_live_copy_is_a_warning_with_the_fix(self) -> None:
        status, detail, fix = judge_updater_script(
            live=None, shipped="UPDATER_ABI=1\n", path="/u/updater.sh", device="deck"
        )
        self.assertEqual(status, WARN)
        self.assertIn("/u/updater.sh", detail)
        self.assertIn("warpzone provision device deck", fix)

    def test_a_live_copy_of_a_higher_abi_is_fine_after_a_rollback(self) -> None:
        status, detail, _ = judge_updater_script(
            live="UPDATER_ABI=2\n", shipped="UPDATER_ABI=1\n", path="/u", device="deck"
        )
        self.assertEqual(status, OK)
        self.assertIn("newer", detail)

    def test_a_live_copy_that_differs_from_the_shipped_one_is_a_warning(self) -> None:
        status, detail, fix = judge_updater_script(
            live="UPDATER_ABI=1\na\n", shipped="UPDATER_ABI=1\nb\n", path="/u", device="deck"
        )
        self.assertEqual(status, WARN)
        self.assertIn("differs", detail)
        self.assertIn("provision device deck", fix)

    def test_a_shipped_script_that_cannot_be_read_is_not_a_reason_to_warn(self) -> None:
        status, _, _ = judge_updater_script(
            live="UPDATER_ABI=1\n", shipped=None, path="/u", device="deck"
        )
        self.assertEqual(status, OK)


class UpdaterCheckTests(TmpCase):
    def setUp(self) -> None:
        super().setUp()
        self.home = self.tmp / "home"
        folder = self.home / ".config/syncthing"
        folder.mkdir(parents=True)
        (folder / "config.xml").write_text(
            f'<configuration><folder id="wz-library" path="{self.home}/Emulation/">'
            f'<device id="{HUB_ID}"/></folder><device id="{DECK_ID}" name="x"/>'
            f"<gui><address>127.0.0.1:8384</address><apikey>{ST_KEY}</apikey></gui>"
            "</configuration>"
        )
        self.clone = self.tmp / "clone"
        (self.clone / ".git").mkdir(parents=True)
        self.python = self.tmp / "venv/bin/python"
        self.python.parent.mkdir(parents=True)
        self.python.write_text("")
        self.config = FULL.replace(
            "BBBBBBB-BBBBBBB-BBBBBBB-BBBBBBB-BBBBBBB-BBBBBBB-BBBBBBB-BBBBBBB", DECK_ID
        )
        self.base = self.home / ".local/state/warpzone"
        self.live = selfupdate.live_path(self.home)
        self.shipped = selfupdate.UPDATER_SOURCE.read_text(encoding="utf-8")

    def install(self, *, state: str | None = "default", conf: str | None = "default") -> None:
        self.live.parent.mkdir(parents=True, exist_ok=True)
        self.live.write_text(self.shipped)
        selfupdate.conf_path(self.base).parent.mkdir(parents=True, exist_ok=True)
        if conf == "default":
            conf = selfupdate.render_conf(
                device="deck",
                config=str(self.tmp / "config.toml"),
                clone=str(self.clone),
                python=str(self.python),
                channel="stable",
                pin=None,
                enabled=True,
                frontend=("es-de", "ES-DE.AppImage"),
            )
        if conf is not None:
            selfupdate.conf_path(self.base).write_text(conf)
        if state == "default":
            state = f"schema=1\nlast_result=ok\nlast_ok={NOW - 60}\nsigning=1\n"
        if state is not None:
            selfupdate.state_path(self.base).write_text(state)

    def check(self, **kw):
        return check_updater(
            context(
                self.tmp,
                kw.pop("config", self.config),
                system="Linux",
                home=self.home,
                now=lambda: NOW,
                executables=[str(self.python)] if self.python.exists() else [],
                **kw,
            )
        )

    def test_a_working_install_has_three_ok_lines(self) -> None:
        self.install()
        results = self.check()
        self.assertEqual([r.status for r in results], [OK, OK, OK], results)
        self.assertEqual(
            [r.name for r in results], ["Updater script", "Updater settings", "Updater state"]
        )
        self.assertTrue(all(r.group == doctor.G_UPDATE for r in results))

    def test_nothing_installed_is_warnings_with_the_fix(self) -> None:
        results = self.check()
        self.assertEqual([r.status for r in results], [WARN, WARN, WARN])
        self.assertIn("warpzone provision device deck", results[0].fix)

    def test_settings_that_say_another_channel_are_a_warning(self) -> None:
        self.install()
        self.config = self.config.replace(
            'folders = ["library", "saves"]', 'folders = ["library", "saves"]\nchannel = "rc"'
        )
        results = self.check()
        self.assertEqual(by_name(results, "Updater settings").status, WARN)
        self.assertIn("channel", by_name(results, "Updater settings").detail)

    def test_a_python_that_is_gone_is_a_warning(self) -> None:
        self.install()
        self.python.unlink()
        self.assertEqual(by_name(self.check(), "Updater settings").status, WARN)

    def test_a_state_with_a_stuck_updater_fails(self) -> None:
        self.install(state=f"last_result=stuck\nlast_ok={NOW}\nsigning=1\n")
        results = [r for r in self.check() if r.name == "Updater state"]
        self.assertEqual([r.status for r in results], [FAIL])

    def test_recent_bad_tags_are_read_from_the_state_folder(self) -> None:
        self.install()
        bad = selfupdate.bad_dir(self.base)
        bad.mkdir()
        (bad / "v0.3.1").write_text(
            "sha=abc\ndate=2027-01-15T07:59:00Z\nreason=health failed (exit 1)\n"
        )
        (bad / ".v0.3.2.tmp.1").write_text("sha=x\n")
        results = [r for r in self.check() if r.name == "Updater state"]
        self.assertEqual([r.status for r in results], [WARN])
        self.assertIn("v0.3.1", results[0].detail)
        self.assertNotIn("v0.3.2", results[0].detail)

    def test_silent_when_the_config_turns_it_off(self) -> None:
        self.install()
        off = self.config.replace(
            'folders = ["library", "saves"]', 'folders = ["library", "saves"]\nself_update = false'
        )
        self.assertEqual(self.check(config=off), [])

    def test_silent_off_linux_and_on_a_machine_that_is_not_a_device(self) -> None:
        self.install()
        self.assertEqual(check_updater(context(self.tmp, self.config, home=self.home)), [])
        (self.home / ".config/syncthing/config.xml").unlink()
        self.assertEqual(self.check(), [])

    def test_runs_with_the_other_checks(self) -> None:
        self.assertIn(check_updater, doctor.CHECKS)


RA_APP = "org.libretro.RetroArch"
DOLPHIN_APP = "org.DolphinEmu.dolphin-emu"
SYNCTHINGY = "com.github.zocker_160.SyncThingy"


class FlatpakAccessTests(TmpCase):
    def setUp(self) -> None:
        super().setUp()
        self.home = self.tmp / "home"
        self.commands: list[tuple[str, ...]] = []

    def syncthingy(self) -> None:
        folder = self.home / ".var/app" / SYNCTHINGY / "config/syncthing"
        folder.mkdir(parents=True)
        (folder / "config.xml").write_text(
            f"<configuration><gui><address>127.0.0.1:8384</address><apikey>{ST_KEY}</apikey>"
            "</gui></configuration>"
        )

    def installed(self, *apps: str) -> None:
        for app in apps:
            (self.home / ".var/app" / app).mkdir(parents=True, exist_ok=True)

    def check(self, shown: str = "", code: int = 0, config_text: str | None = None, **kw):
        def run(command):
            self.commands.append(tuple(command))
            return RunOutput(code, shown)

        return check_flatpak_access(context(self.tmp, config_text, system="Linux", run=run, **kw))

    def test_missing_overrides_fail_with_one_command_per_app(self) -> None:
        self.syncthingy()
        self.installed(RA_APP, DOLPHIN_APP)
        (r,) = self.check("[Context]\nfilesystems=host;\n")
        self.assertEqual((r.name, r.group, r.status), ("Flatpak access", doctor.G_SYNCTHING, FAIL))
        self.assertIn(f"~/.var/app/{RA_APP}", r.detail)
        self.assertIn(f"~/.var/app/{DOLPHIN_APP}", r.detail)
        self.assertIn(
            f"  flatpak override --user --filesystem=~/.var/app/{RA_APP} {SYNCTHINGY}", r.fix
        )
        self.assertIn(
            f"  flatpak override --user --filesystem=~/.var/app/{DOLPHIN_APP} {SYNCTHINGY}", r.fix
        )
        self.assertIn("restart Syncthing", r.fix)
        self.assertEqual(self.commands, [("flatpak", "override", "--user", "--show", SYNCTHINGY)])

    def test_only_the_missing_apps_are_named(self) -> None:
        self.syncthingy()
        self.installed(RA_APP, DOLPHIN_APP)
        (r,) = self.check(f"[Context]\nfilesystems=host;~/.var/app/{RA_APP};\n")
        self.assertEqual(r.status, FAIL)
        self.assertNotIn(RA_APP, r.detail + r.fix)
        self.assertIn(DOLPHIN_APP, r.fix)

    def test_all_granted_is_ok(self) -> None:
        self.syncthingy()
        self.installed(RA_APP, DOLPHIN_APP)
        shown = f"[Context]\nfilesystems=~/.var/app/{RA_APP};~/.var/app/{DOLPHIN_APP};\n"
        (r,) = self.check(shown)
        self.assertEqual(r.status, OK)

    def test_apps_that_are_not_installed_are_not_checked(self) -> None:
        self.syncthingy()
        self.installed(RA_APP)
        (r,) = self.check()
        self.assertEqual(r.status, FAIL)
        self.assertNotIn(DOLPHIN_APP, r.detail + r.fix)

    def test_nothing_to_say_without_the_flatpak_or_any_installed_app(self) -> None:
        self.assertEqual(self.check(), [])  # no Syncthing at all
        self.syncthingy()
        self.assertEqual(self.check(), [])  # no emulator flatpak installed
        self.assertEqual(self.commands, [])  # and flatpak was not even asked
        self.assertEqual(check_flatpak_access(context(self.tmp, home=self.home)), [])  # macOS

    def test_native_syncthing_needs_nothing(self) -> None:
        write_syncthing_config(self.home)
        self.installed(RA_APP)
        self.assertEqual(self.check(), [])
        self.assertEqual(self.commands, [])

    def test_flatpak_that_cannot_be_run_warns_with_the_command(self) -> None:
        self.syncthingy()
        self.installed(RA_APP)

        def run(command):
            raise FileNotFoundError(2, "No such file or directory")

        (r,) = check_flatpak_access(context(self.tmp, system="Linux", run=run))
        self.assertEqual(r.status, WARN)
        self.assertIn(f"--filesystem=~/.var/app/{RA_APP}", r.fix)

    def test_failed_show_counts_as_nothing_granted(self) -> None:
        self.syncthingy()
        self.installed(RA_APP)
        (r,) = self.check("error: no such app", code=1)
        self.assertEqual(r.status, FAIL)

    def test_only_the_folders_the_config_subscribes_to_count(self) -> None:
        self.syncthingy()
        self.installed(RA_APP)
        (r,) = self.check(config_text=FULL)  # the device has `saves`
        self.assertEqual(r.status, FAIL)
        no_saves = FULL.replace('["library", "saves"]', '["library"]')
        self.assertEqual(self.check(config_text=no_saves), [])

    def test_run_checks_includes_it(self) -> None:
        self.syncthingy()
        self.installed(RA_APP)
        probes = make_probes(
            self.tmp, system="Linux", os_release=STEAMOS, run=lambda c: RunOutput(0, "")
        )
        results = run_checks(probes)
        self.assertEqual(by_name(results, "Flatpak access").status, FAIL)
