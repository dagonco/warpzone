"""`warpzone doctor`: check this machine and say how to fix what is missing.

Offline and read-only: it never installs or changes anything. The only network-like
probe is one request to the local Syncthing REST API on the loopback address.

Each check is a small function that takes a `Context` and returns `Result` records.
Everything it needs from the machine (`which`, running a tool, the environment, the
filesystem, the clock, the HTTP probe) is a field of `Probes`, so the tests run with
fakes and no real tools. `render` turns the results into text and `exit_code` into the
process status; both are pure.

Statuses and what they mean for the exit code:

    ok        [ok]  fine
    warn      [! ]  works, but worth a look (old DATs, little free space); exit code unaffected
    fail      [!!]  a problem: something this machine needs is missing or broken; exit 1
    optional  [--]  not set up; only matters for the feature named in the line; exit 0

The checks live in the `doctor_*` modules, by area (see "Code layout" in
development/design/code-and-testing.md); this module is the name `cli` and the tests import. It
holds the list of checks (`CHECKS`, run in this order), the runner and the output. To add a
check, write it in the module of its area (or a new `doctor_*` module) and add it to `CHECKS`.
"""

from __future__ import annotations

import shutil
import textwrap
from collections.abc import Callable, Sequence

from warpzone.doctor_config import (
    check_base,
    check_config,
    check_hub_api,
    judge_hub_api,
    load_config_state,
)
from warpzone.doctor_core import (
    FAIL,
    G_BASE,
    G_CONFIG,
    G_DATS,
    G_FRONTEND,
    G_HUB,
    G_INBOX,
    G_PRUNE,
    G_RA,
    G_SAVES,
    G_SETTINGS,
    G_SWITCH,
    G_SYNCTHING,
    G_TOOLS,
    G_UPDATE,
    G_WAY,
    MARKS,
    OK,
    OPTIONAL,
    WARN,
    ConfigState,
    Context,
    DeviceFolders,
    Result,
    SettingsDevice,
)
from warpzone.doctor_frontend import (
    check_frontend,
    judge_artwork,
    judge_cemu_paths,
    judge_retroarch_paths,
    judge_save_convert,
    judge_shadps4_dirs,
    judge_shadps4_launch,
    judge_shadps4_version,
    judge_systems_override,
    judge_title_links,
)
from warpzone.doctor_hints import (
    hint_for,
)
from warpzone.doctor_inbox import (
    GIB,
    check_arcade_dats,
    check_dats,
    check_hub,
    check_inbox_way,
    check_ra,
    check_switch,
    collapse_inbox,
)
from warpzone.doctor_probes import (
    Probes,
    RunOutput,
    platform_family,
)
from warpzone.doctor_saves import (
    check_flycast,
    check_vita3k,
    judge_flycast_vmus,
    judge_vita3k_user,
    vita3k_user_id,
)
from warpzone.doctor_scrape import check_skyscraper
from warpzone.doctor_settings import (
    check_play_state,
    check_settings,
    judge_esde_script,
    judge_event_scripts,
    judge_last_apply,
    judge_play_state,
    judge_settings_layers,
)
from warpzone.doctor_syncthing import (
    SYNCTHING_UNIT,
    check_flatpak_access,
    check_game_mode,
    check_syncthing,
    check_syncthing_unit,
    check_undescribed_folders,
    judge_game_mode,
    judge_unit_state,
    parse_show,
)
from warpzone.doctor_tools import (
    TOOLS,
    ToolSpec,
    check_tool,
    check_tools,
    known_tool_paths,
    vita3k_usable,
)
from warpzone.doctor_updates import (
    check_prune_guard,
    check_update_timer,
    check_updater,
    judge_prune_guard,
    judge_update_timer,
    judge_updater_conf,
    judge_updater_script,
    judge_updater_state,
)

__all__ = [
    "CHECKS",
    "FAIL",
    "GIB",
    "G_BASE",
    "G_CONFIG",
    "G_DATS",
    "G_FRONTEND",
    "G_HUB",
    "G_INBOX",
    "G_PRUNE",
    "G_RA",
    "G_SAVES",
    "G_SETTINGS",
    "G_SWITCH",
    "G_SYNCTHING",
    "G_TOOLS",
    "G_UPDATE",
    "G_WAY",
    "MARKS",
    "OK",
    "OPTIONAL",
    "SYNCTHING_UNIT",
    "TOOLS",
    "WARN",
    "ConfigState",
    "Context",
    "DeviceFolders",
    "Probes",
    "Result",
    "RunOutput",
    "SettingsDevice",
    "ToolSpec",
    "check_arcade_dats",
    "check_base",
    "check_config",
    "check_dats",
    "check_flatpak_access",
    "check_flycast",
    "check_frontend",
    "check_game_mode",
    "check_hub",
    "check_hub_api",
    "check_inbox_way",
    "check_play_state",
    "check_prune_guard",
    "check_ra",
    "check_settings",
    "check_skyscraper",
    "check_switch",
    "check_syncthing",
    "check_syncthing_unit",
    "check_tool",
    "check_tools",
    "check_undescribed_folders",
    "check_update_timer",
    "check_updater",
    "check_vita3k",
    "collapse_inbox",
    "exit_code",
    "hint_for",
    "judge_artwork",
    "judge_cemu_paths",
    "judge_esde_script",
    "judge_event_scripts",
    "judge_flycast_vmus",
    "judge_game_mode",
    "judge_hub_api",
    "judge_last_apply",
    "judge_play_state",
    "judge_prune_guard",
    "judge_retroarch_paths",
    "judge_save_convert",
    "judge_settings_layers",
    "judge_shadps4_dirs",
    "judge_shadps4_launch",
    "judge_shadps4_version",
    "judge_systems_override",
    "judge_title_links",
    "judge_unit_state",
    "judge_update_timer",
    "judge_updater_conf",
    "judge_updater_script",
    "judge_updater_state",
    "judge_vita3k_user",
    "known_tool_paths",
    "load_config_state",
    "parse_show",
    "platform_family",
    "render",
    "run_checks",
    "run_doctor",
    "summary",
    "terminal_width",
    "vita3k_usable",
    "vita3k_user_id",
]


FALLBACK_WIDTH = 100  # when the terminal width is unknown (a pipe, a log)
MIN_TEXT_WIDTH = 30  # never wrap narrower than this, however wide the labels are
FIX_PREFIX = "fix: "

CHECKS: tuple[Callable[[Context], list[Result]], ...] = (
    check_base,
    check_config,
    check_syncthing,
    check_game_mode,
    check_flatpak_access,
    check_syncthing_unit,
    check_undescribed_folders,
    check_flycast,
    check_vita3k,
    check_settings,
    check_frontend,
    check_play_state,
    check_update_timer,
    check_updater,
    check_prune_guard,
    check_inbox_way,
    check_tools,
    check_skyscraper,
    check_switch,
    check_dats,
    check_arcade_dats,
    check_hub,
    check_ra,
)


def run_checks(probes: Probes) -> list[Result]:
    ctx = Context(probes, load_config_state(probes.config_path))
    results: list[Result] = []
    for check in CHECKS:
        results += check(ctx)
    return results if ctx.inbox_configured else collapse_inbox(results)


def _wrap(text: str, width: int) -> list[str]:
    """`text` as lines of at most `width` characters (a word longer than that stays whole,
    so a path or a command is never cut). Newlines in `text` start new lines, and a line
    that starts with a space is a code line: it is kept as it is, however long."""
    lines: list[str] = []
    for line in text.splitlines() or [""]:
        if line.startswith(" "):
            lines.append(line.rstrip())
            continue
        lines += textwrap.wrap(line, width, break_long_words=False, break_on_hyphens=False) or [""]
    return lines


def render(results: Sequence[Result], width: int = FALLBACK_WIDTH) -> list[str]:
    """The report as lines no wider than `width` where the words allow it: groups in the
    order they first appear, one line per check, continuation lines for details and
    fixes. Wrapped lines hang under the start of the detail; a fix hangs under its text."""
    lines: list[str] = []
    label_width = max((len(r.name) for r in results), default=0)
    indent = " " * (7 + label_width + 1)
    room = max(width - len(indent), MIN_TEXT_WIDTH)
    groups = list(dict.fromkeys(r.group for r in results))
    for group in groups:
        if lines:
            lines.append("")
        lines.append(group)
        for r in (r for r in results if r.group == group):
            detail = _wrap(r.detail, room)
            lines.append(f"  {MARKS[r.status]} {r.name:<{label_width}} {detail[0]}".rstrip())
            lines += [f"{indent}{extra}" for extra in detail[1:]]
            if r.fix:
                hang = " " * len(FIX_PREFIX)
                fix = _wrap(r.fix, room - len(FIX_PREFIX))
                lines.append(f"{indent}{FIX_PREFIX}{fix[0]}")
                lines += [f"{indent}{hang}{extra}" for extra in fix[1:]]
    lines += ["", summary(results)]
    return lines


def summary(results: Sequence[Result]) -> str:
    count = {s: sum(1 for r in results if r.status == s) for s in MARKS}
    parts = [
        f"{count[FAIL]} problem(s)",
        f"{count[WARN]} warning(s)",
        f"{count[OPTIONAL]} optional item(s) not set up",
    ]
    return f"{', '.join(parts)}. " + (
        "Fix the [!!] lines above." if count[FAIL] else "Nothing required is missing."
    )


def exit_code(results: Sequence[Result]) -> int:
    return 1 if any(r.status == FAIL for r in results) else 0


def terminal_width() -> int:
    return shutil.get_terminal_size((FALLBACK_WIDTH, 24)).columns


def run_doctor(probes: Probes, out: Callable[[str], None] = print, width: int | None = None) -> int:
    results = run_checks(probes)
    out(f"warpzone doctor (config: {probes.config_path})")
    out("")
    for line in render(results, terminal_width() if width is None else width):
        out(line)
    return exit_code(results)
