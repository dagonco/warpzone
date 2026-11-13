"""The doctor checks of the save-sync settings that live in an emulator's own files.

Flycast's per-game VMUs and Vita3K's active user. See development/design/doctor.md
("Flycast VMUs", "Vita3K user").
"""

from __future__ import annotations

import re

from warpzone.doctor_core import G_SAVES, OK, WARN, Context, Result

# RetroArch's Flycast core options. `reicast_per_content_vmus` is "disabled" (default),
# "VMU A1" or "All VMUs" (libretro_core_options.h in github.com/flyinghead/flycast).
FLYCAST_OPT = ".var/app/org.libretro.RetroArch/config/retroarch/config/Flycast/Flycast.opt"
FLYCAST_VMU_KEY = "reicast_per_content_vmus"
FLYCAST_VMU_FIX = (
    "in RetroArch, with a Dreamcast game running, open Quick Menu > Core Options > "
    'Per-Game Visual Memory Units/Systems (VMU) ("Per-Game VMUs") and choose "VMU A1" '
    'or "All VMUs". Existing shared VMUs are not migrated: each game starts with an '
    "empty card in RetroArch's saves folder, which retroarch-saves syncs"
)

# Vita3K's active user. The profile syncs the saves of user 00 only
# (ux0/user/00/savedata); a user made in the GUI can get another id.
VITA3K_CONFIG = ".config/Vita3K/config.yml"
VITA3K_USER = "00"


def _core_option(text: str, key: str) -> str | None:
    """The value of `key` in a RetroArch core options file (`key = "value"` lines)."""
    found = None
    for line in text.splitlines():
        name, sep, value = line.partition("=")
        if sep and name.strip() == key:
            found = value.strip().strip('"')
    return found


def judge_flycast_vmus(text: str) -> tuple[str, str, str]:
    """(status, detail, fix) for the Flycast core's per-game VMU option, given the text of
    Flycast.opt. Without per-game VMUs the core writes `vmu_save_*.bin` into the system
    folder (bios/dc), which the library ignores: Dreamcast saves would never sync."""
    value = _core_option(text, FLYCAST_VMU_KEY)
    if value in ("VMU A1", "All VMUs"):
        return OK, f'Flycast keeps per-game VMUs in the saves folder ("{value}")', ""
    shown = f'"{value}"' if value is not None else "unset, which means disabled"
    return (
        WARN,
        f"Flycast's per-game VMUs are off ({shown}), so the VMUs live in the system "
        "folder (bios/dc), which warpzone does not sync and a revert would not restore",
        FLYCAST_VMU_FIX,
    )


def vita3k_user_id(text: str) -> str | None:
    """The `user-id:` of Vita3K's config.yml, without quotes or a trailing comment."""
    match = re.search(r"^user-id:[ \t]*(.*)$", text, re.MULTILINE)
    if not match:
        return None
    value = match.group(1).split(" #")[0].strip().strip("\"'")
    return value or None


def judge_vita3k_user(user: str) -> tuple[str, str, str]:
    """(status, detail, fix) for Vita3K's active user id."""
    if user == VITA3K_USER:
        return OK, f"the active user is {user}, the one warpzone syncs", ""
    return (
        WARN,
        f"the active user is {user}, but warpzone syncs the saves of user {VITA3K_USER} "
        f"(ux0/user/{VITA3K_USER}/savedata), so this user's saves are not synced",
        f"in Vita3K, switch to user {VITA3K_USER} (or delete the other user and keep "
        f"{VITA3K_USER}); if the saves are in ux0/user/{user}/savedata, move them to "
        f"ux0/user/{VITA3K_USER}/savedata. Vita3K has to be closed while you do it",
    )


def check_flycast(ctx: Context) -> list[Result]:
    """Only when RetroArch's Flycast core has run (it writes its options file then)."""
    p = ctx.probes
    path = p.home / FLYCAST_OPT
    if not p.is_file(path):
        return []
    status, detail, fix = judge_flycast_vmus(p.read_text(path))
    return [Result("Flycast VMUs", G_SAVES, status, detail, fix)]


def check_vita3k(ctx: Context) -> list[Result]:
    """Only when Vita3K has written its config."""
    p = ctx.probes
    path = p.home / VITA3K_CONFIG
    if not p.is_file(path):
        return []
    user = vita3k_user_id(p.read_text(path))
    if user is None:  # no user chosen yet: nothing to compare
        return []
    status, detail, fix = judge_vita3k_user(user)
    return [Result("Vita3K user", G_SAVES, status, detail, fix)]
