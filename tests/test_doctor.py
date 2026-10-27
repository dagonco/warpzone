import os
import subprocess
import tempfile
import unittest
from collections.abc import Mapping, Sequence
from pathlib import Path
from unittest import mock

from support import API_KEY, HUB_ID
from test_cli import run
from warpzone import __version__, doctor
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
    check_hub,
    check_ra,
    check_syncthing,
    check_tool,
    exit_code,
    hint_for,
    load_config_state,
    platform_family,
    render,
    run_checks,
    run_doctor,
)

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
        results = run_checks(make_probes(self.tmp))
        groups = list(dict.fromkeys(r.group for r in results))
        self.assertEqual(
            groups,
            [
                doctor.G_BASE,
                doctor.G_CONFIG,
                doctor.G_SYNCTHING,
                doctor.G_TOOLS,
                doctor.G_DATS,
                doctor.G_HUB,
                doctor.G_RA,
            ],
        )

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


if __name__ == "__main__":
    unittest.main()
