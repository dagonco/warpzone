"""The doctor checks of scraping: Skyscraper and the ScreenScraper account.

Like the other optional inbox tools, Skyscraper is `[ok]` with its version when found, `[--]`
when it is not (`inbox` then does not scrape), and a problem when a configured path is wrong.
The account line says whether `userCreds` is set in `[screenscraper]` of Skyscraper's
`config.ini`; the judgment is a pure function of the file's lines (`scrape.has_user_creds`)
and the value is never kept, printed or logged. See development/design/scrape.md ("Doctor").
"""

from __future__ import annotations

import subprocess

from warpzone import scrape
from warpzone.doctor_core import FAIL, G_TOOLS, OK, OPTIONAL, WARN, Context, Result

SKYSCRAPER = "Skyscraper"
ACCOUNT = "ScreenScraper account"
RELEASES = "https://github.com/Gemba/skyscraper/releases"
INSTALL_HINT = (
    f"get Skyscraper from {RELEASES}; put it on PATH or set `skyscraper` in the [inbox] table"
)
ANONYMOUS = (
    "anonymous: 1 thread, 10,000 requests a day; a free ScreenScraper account doubles the requests"
)
ACCOUNT_FIX = (
    'add userCreds="user:pass" under [screenscraper] in Skyscraper\'s config.ini '
    "(warpzone never reads the value)"
)


def judge_version(version: tuple[int, int, int] | None) -> tuple[str, str, str]:
    """(status, detail, fix) for the version `Skyscraper --version` printed (None: none found).
    Older than the one warpzone was tested with is a warning: the flags it passes may not exist."""
    tested = ".".join(str(n) for n in scrape.tested_version())
    if version is None:
        return WARN, "ran but printed no recognizable version", "check that it is Skyscraper"
    shown = ".".join(str(n) for n in version)
    if version < scrape.tested_version():
        return (
            WARN,
            f"{shown} is older than {tested}, which warpzone was tested with: the flags it "
            "passes may not exist there",
            f"update Skyscraper: {INSTALL_HINT}",
        )
    return OK, shown, ""


def judge_account(text: str) -> tuple[str, str, str]:
    """(status, detail, fix) for the text of Skyscraper's `config.ini` ("" when there is none)."""
    if scrape.has_user_creds(text.splitlines()):
        return OK, "userCreds set in [screenscraper]", ""
    return OPTIONAL, ANONYMOUS, ACCOUNT_FIX


def check_skyscraper(ctx: Context) -> list[Result]:
    p = ctx.probes
    if p.in_container():
        return [
            Result(
                SKYSCRAPER,
                G_TOOLS,
                OPTIONAL,
                "not in the hub image: scraping runs on a computer that mounts the share",
                "run scrape on a computer that mounts the share",
            )
        ]
    configured = ctx.config.settings.skyscraper
    if configured:
        if not p.is_executable(configured):
            return [
                Result(
                    SKYSCRAPER,
                    G_TOOLS,
                    FAIL,
                    f"[inbox] skyscraper = {configured} is not an executable file",
                    "fix the path, or remove the setting to look Skyscraper up on PATH. "
                    f"To install it: {INSTALL_HINT}",
                )
            ]
        path = configured
    else:
        path = p.which(SKYSCRAPER) or ""
    if not path:
        return [
            Result(
                SKYSCRAPER,
                G_TOOLS,
                OPTIONAL,
                "not found: inbox will not scrape new games, and `warpzone scrape` needs it",
                INSTALL_HINT,
            )
        ]
    try:
        out = p.run([path, "--version"])
    except (OSError, subprocess.SubprocessError) as exc:
        reason = getattr(exc, "strerror", None) or type(exc).__name__
        return [
            Result(
                SKYSCRAPER,
                G_TOOLS,
                FAIL,
                f"{path} cannot be run ({reason})",
                f"reinstall it for this machine ({p.machine or 'this architecture'}): "
                f"{INSTALL_HINT}",
            )
        ]
    status, detail, fix = judge_version(scrape.parse_version(out.output))
    results = [Result(SKYSCRAPER, G_TOOLS, status, f"{detail} ({path})", fix)]
    config = scrape.config_ini_path(p.env, p.home, p.is_dir)
    status, detail, fix = judge_account(p.read_text(config))
    results.append(Result(ACCOUNT, G_TOOLS, status, detail, fix))
    return results
