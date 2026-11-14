"""The doctor checks of scraping: Skyscraper, the ScreenScraper account and the nightly scrape.

Like the other optional inbox tools, Skyscraper is `[ok]` with its version when found, `[--]`
when it is not (`inbox` then does not scrape), and a problem when a configured path is wrong.
The hub image ships it and keeps its `config.ini` and cache on the volume mounted at
`/skyscraper` (named by `WARPZONE_SKYSCRAPER_DIR`): without the volume the line is `[--]`.
The account line says whether `userCreds` is set in `[screenscraper]` of Skyscraper's
`config.ini`; the judgment is a pure function of the file's lines (`scrape.has_user_creds`)
and the value is never kept, printed or logged. See development/design/scrape.md ("Doctor").

The `Nightly scrape` line (`check_nightly_scrape`, with its pure judge `judge_nightly`) reads
the state and the newest run record of the unattended scrape on the hub. See
development/design/scrape-unattended.md ("Doctor").
"""

from __future__ import annotations

import subprocess
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from typing import Any

from warpzone import scrape
from warpzone.doctor_core import (
    FAIL,
    G_TOOLS,
    OK,
    OPTIONAL,
    WARN,
    Context,
    Result,
    merge_verdicts,
)
from warpzone.doctor_inbox import _ago, unattended_root
from warpzone.inbox_unattended import newest_record
from warpzone.scrape_unattended import (
    NEWS_AFTER,
    QUOTA,
    QUOTA_WARN_NIGHTS,
    State,
    Trouble,
    parse_record,
    parse_state,
    runs_dir,
    state_path,
)

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
    volume = scrape.skyscraper_dir(p.env)
    if volume is not None and not p.is_dir(volume):
        # The hub image keeps Skyscraper's config.ini and cache on a volume; without it the
        # program stops at start, so it is not run.
        return [
            Result(
                SKYSCRAPER,
                G_TOOLS,
                OPTIONAL,
                f"{path} is in the image, but nothing is mounted at {volume}, where it keeps its "
                "config.ini and cache: inbox will not scrape",
                f"mount a folder at {volume} (`./skyscraper:{volume}` in the compose service, "
                "owned by the container's uid)",
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


NIGHTLY = "Nightly scrape"
STALE_NIGHTLY = timedelta(hours=36)  # one run a day, and a day and a half before it is late
NIGHTLY_FIX = (
    "schedule `warpzone scrape --unattended` once a night on the hub (see "
    "https://dagonco.github.io/warpzone/development/design/scrape-unattended/"
    "#the-scheduled-task)"
)
SCRAPED = ("added", "filled", "not_found", "unchanged")  # the games Skyscraper was asked about
CAPPED = ("done", "budget")  # the outcomes of a night that ended on its cap or its time budget

# What a streak of each kind says, and what to do about it.
TROUBLE_TEXT: Mapping[str, tuple[str, str]] = {
    "blacklisted": (
        "ScreenScraper blacklisted this client",
        "read ScreenScraper's message in the scheduled task's output; the ban usually lifts by "
        "itself, and fewer games a night (`--max-games`) or an account avoids it",
    ),
    "network": (
        "ScreenScraper could not be reached",
        "check the hub's network and DNS, and that ScreenScraper is up",
    ),
    "busy": (
        "ScreenScraper was too busy",
        "wait; if it lasts, move the scheduled task to another hour",
    ),
    "failed": (
        "the batches of some systems failed",
        "run `warpzone scrape --system NAME` by hand for those systems to see the error",
    ),
    "lock": (
        "the inbox lock was held all night",
        "look for a run of the inbox that never ends (`warpzone inbox` says who holds the lock)",
    ),
    "died": (
        "the run was killed before it finished",
        "the hub's memory or the task's time limit may be too small; read the task's output",
    ),
    "closed": (
        "ScreenScraper was closed for maintenance",
        "wait; ScreenScraper's forum says when it is back",
    ),
}


def _trouble_part(trouble: Trouble) -> tuple[str, str, str] | None:
    """The finding of a streak that has reached its threshold (None when it has not)."""
    nights = f"{trouble.count} night{'s' if trouble.count != 1 else ''} in a row"
    if trouble.kind == QUOTA:
        if trouble.count < QUOTA_WARN_NIGHTS:
            return None
        return (
            WARN,
            f"ScreenScraper's daily quota reached {nights}",
            "the library is larger than the account covers in a night, so it fills over more "
            "nights; a ScreenScraper account (userCreds) raises the quota",
        )
    threshold = NEWS_AFTER.get(trouble.kind)
    if threshold is None or trouble.count < threshold:
        return None
    what, fix = TROUBLE_TEXT.get(trouble.kind, (trouble.kind, ""))
    systems = f" ({', '.join(trouble.systems)})" if trouble.systems else ""
    return WARN, f"{what}{systems}, {nights}", fix


def _total(systems: Mapping[str, Mapping[str, int]], key: str) -> int:
    return sum(counts.get(key, 0) for counts in systems.values())


def _last_night(state: State, record: Mapping[str, Any] | None, now: datetime) -> str:
    """The ok text: when the last run was, what it did and what is left. The nights it still
    takes come from the size of the last night (the games Skyscraper was asked about, which the
    record holds) and only when that night ended on its cap or its time budget: a night ended by
    a stop does not show what a night can do. Pure."""
    if record is None and state.last is None:
        ago = f"last check {_ago(now - state.checked)} ago; " if state.checked else ""
        return f"{ago}no night has scraped anything yet"
    systems: Mapping[str, Mapping[str, int]] = record["systems"] if record else {}
    if record is not None:
        when = str(record["started"])[:16].replace("T", " ")
        added, filled, missing = (_total(systems, k) for k in ("added", "filled", "not_found"))
        text = f"last run {when} UTC: added {added}, filled {filled}, not found {missing}"
    else:
        assert state.last is not None
        when = state.last.finished.astimezone(UTC).strftime("%Y-%m-%d %H:%M")
        text = f"last run {when} UTC: no record kept"
    if state.backlog is None:
        return text
    left = state.backlog.left
    if left == 0:
        return f"{text}; nothing left"
    text += f"; {left} left"
    per_night = sum(_total(systems, k) for k in SCRAPED)
    if state.last is not None and state.last.outcome in CAPPED and per_night > 0:
        text += f" (about {-(-left // per_night)} nights at {per_night} a night)"
    return text


def judge_nightly(
    state: State | None,
    record: Mapping[str, Any] | None,
    now: datetime,
    damaged: str | None = None,
) -> tuple[str, str, str]:
    """(status, detail, fix) of the nightly scrape, from its state (None when the file does not
    exist), the newest run record (None when there is none), the time and, when the parser left
    something of the state file out, its warning. Several findings share the one line, the worst
    status first. Pure."""
    if state is None:
        return OPTIONAL, "no nightly scrape has recorded anything", NIGHTLY_FIX
    parts: list[tuple[str, str, str]] = []
    err = state.error
    if err is not None:
        since = err.first.astimezone(UTC).strftime("%Y-%m-%d %H:%M")
        if err.config:
            fix = (
                "fix what the message says (the config, the mounts; `warpzone doctor` on the "
                f"hub lists them); the next run that gets past step {err.step} clears it"
            )
        else:
            fix = (
                "read the scheduled task's output on the hub for the cause; the next run that "
                f"gets past step {err.step} clears it"
            )
        parts.append((FAIL, f"{err.message} (since {since} UTC, {err.count} run(s))", fix))
    if state.checked is None:
        parts.append(
            (
                WARN,
                "the state file records no check; the nightly scrape may never have run",
                "is the scheduled task enabled? Run it once by hand to see its output",
            )
        )
    elif now - state.checked > STALE_NIGHTLY:
        parts.append(
            (
                WARN,
                f"the nightly scrape last checked {_ago(now - state.checked)} ago; is the "
                "scheduled task enabled?",
                "enable the scheduled task on the hub, or run it once by hand to see why it stops",
            )
        )
    found = _trouble_part(state.trouble) if state.trouble is not None else None
    if found is not None:
        parts.append(found)
    if damaged:
        parts.append((WARN, damaged, "the next run rewrites the state file"))
    if parts:
        return merge_verdicts(parts)
    return OK, _last_night(state, record, now), ""


def check_nightly_scrape(ctx: Context) -> list[Result]:
    """The `Nightly scrape` line: with an [inbox] table and a readable hub root (the state is on
    the share, so a computer sees it as the container does). A state file that cannot be parsed
    reads as empty and its warning is shown; a record that cannot be parsed is no record."""
    root = unattended_root(ctx)
    if root is None:
        return []
    p = ctx.probes
    path = state_path(root)
    state, warning = parse_state(p.read_text(path)) if p.is_file(path) else (None, None)
    folder = runs_dir(root)
    name = newest_record(p.list_dir(folder))
    record = parse_record(p.read_text(folder / name)) if name else None
    status, detail, fix = judge_nightly(
        state, record, datetime.fromtimestamp(p.now(), UTC), warning
    )
    return [Result(NIGHTLY, G_TOOLS, status, detail, fix)]
