"""`warpzone self-update`: move a git checkout of warpzone to the newest release tag.

Devices install warpzone as a clone with an editable install, so updating is a checkout of
the newest `vX.Y.Z` tag. The decisions (`parse_tag`, `newest_tag`, `decide`) are pure; the
rest talks to git through a `Runner`, so the tests can use a real repository on disk
without a network. See docs/self-update.md.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

# (exit code, stdout and stderr together). Raises OSError when the program cannot run and
# subprocess.TimeoutExpired when it takes too long.
Runner = Callable[[Sequence[str]], tuple[int, str]]

TIMEOUT_SECONDS = 300
LOG_NAME = "self-update.log"
LOG_MAX_BYTES = 64 * 1024
LOG_KEEP_LINES = 200

_TAG = re.compile(r"v(\d+)\.(\d+)\.(\d+)")
_VERSION = re.compile(r"(\d+)\.(\d+)\.(\d+)(.*)")


def parse_tag(tag: str) -> tuple[int, int, int] | None:
    """`(major, minor, patch)` of a plain release tag `vX.Y.Z`; None for anything else."""
    match = _TAG.fullmatch(tag)
    if match is None:
        return None
    major, minor, patch = (int(n) for n in match.groups())
    return major, minor, patch


def version_key(version: str) -> tuple[int, int, int, int] | None:
    """A sortable key for a package version; a pre-release sorts before its release."""
    match = _VERSION.fullmatch(version)
    if match is None:
        return None
    major, minor, patch = (int(n) for n in match.groups()[:3])
    return major, minor, patch, 0 if match.group(4) else 1


def newest_tag(tags: Sequence[str]) -> str | None:
    """The highest `vX.Y.Z` tag, comparing the numbers (v0.10.0 is newer than v0.9.0)."""
    found = [(key, tag) for tag in tags if (key := parse_tag(tag)) is not None]
    return max(found)[1] if found else None


@dataclass(frozen=True)
class Decision:
    # update: the newest tag is newer than the running version; current: they match;
    # ahead: the running version is newer than every tag (never a downgrade);
    # no-tags: there is no release tag; bad-version: the running version is unreadable.
    action: str
    tag: str | None


def decide(running: str, tags: Sequence[str]) -> Decision:
    have = version_key(running)
    if have is None:
        return Decision("bad-version", None)
    tag = newest_tag(tags)
    if tag is None:
        return Decision("no-tags", None)
    target = parse_tag(tag)
    assert target is not None
    wanted = (*target, 1)
    if wanted > have:
        return Decision("update", tag)
    return Decision("current" if wanted == have else "ahead", tag)


def run_command(command: Sequence[str]) -> tuple[int, str]:
    """Run a command with a time limit and no way to ask for a password."""
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}
    proc = subprocess.run(
        list(command),
        capture_output=True,
        text=True,
        errors="replace",
        timeout=TIMEOUT_SECONDS,
        stdin=subprocess.DEVNULL,
        env=env,
        check=False,
    )
    return proc.returncode, (proc.stdout or "") + (proc.stderr or "")


def package_root() -> Path:
    """The folder that holds the `warpzone` package: the checkout for an editable install."""
    return Path(__file__).resolve().parent.parent


def log_path(state_base: Path) -> Path:
    """The log file inside `~/.local/state/warpzone/` (`state_base` is that folder)."""
    return state_base / LOG_NAME


def _first_line(text: str) -> str:
    return next((line.strip() for line in text.splitlines() if line.strip()), "no details")


def _append_log(path: Path, line: str, now: float) -> None:
    """Append one timestamped line; the file is cut to its last lines when it grows large."""
    stamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now))
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists() and path.stat().st_size > LOG_MAX_BYTES:
            kept = path.read_text(encoding="utf-8", errors="replace").splitlines()[-LOG_KEEP_LINES:]
            path.write_text("".join(f"{k}\n" for k in kept), encoding="utf-8")
        with path.open("a", encoding="utf-8") as fh:
            fh.write(f"{stamp} {line}\n")
    except OSError:
        pass  # a log that cannot be written must not stop an update


def run_self_update(
    root: Path,
    version: str,
    out: Callable[[str], None],
    *,
    dry_run: bool = False,
    log: Path | None = None,
    run: Runner = run_command,
    python: str | None = None,
    now: Callable[[], float] = time.time,
) -> int:
    """Check out the newest release tag of the checkout at `root` when it is newer than
    `version`. Returns the exit code: 0 when nothing is wrong (including no network and
    not a checkout), 1 when the update was refused or failed."""
    python = python or sys.executable
    write_log = log is not None and not dry_run

    def say(message: str) -> None:
        out(message)
        if write_log:
            assert log is not None
            _append_log(log, message, now())

    def git(*args: str) -> tuple[int, str]:
        return run(["git", "-C", str(root), *args])

    try:
        code, top = git("rev-parse", "--show-toplevel")
    except OSError as exc:
        say(f"self-update: git cannot be run ({exc}); nothing to do")
        return 0
    if code != 0 or Path(top.strip()).resolve() != root.resolve():
        say(
            f"self-update: {root} is not a git checkout, so it is not updated here "
            "(update it with the tool you installed it with)"
        )
        return 0

    try:
        code, text = git("fetch", "--tags", "--quiet")
    except (OSError, subprocess.SubprocessError) as exc:
        code, text = 1, str(exc)
    if code != 0:
        say(f"self-update: could not reach the remote ({_first_line(text)}); nothing changed")
        return 0
    code, text = git("tag", "--list", "v*")
    if code != 0:
        say(f"self-update: cannot list the tags ({_first_line(text)})")
        return 1
    decision = decide(version, text.split())
    if decision.action == "bad-version":
        say(f"self-update: cannot read the running version {version!r}")
        return 1
    if decision.action == "no-tags":
        say(f"self-update: the checkout has no release tag; staying on {version}")
        return 0
    if decision.action == "current":
        say(f"self-update: up to date ({version})")
        return 0
    if decision.action == "ahead":
        say(f"self-update: {version} is ahead of the newest release {decision.tag}; not changed")
        return 0

    tag = decision.tag
    assert tag is not None
    target = tag[1:]
    code, text = git("status", "--porcelain", "--untracked-files=no")
    if code != 0 or text.strip():
        say(
            f"self-update: {root} has local changes, so it was not updated "
            f"({version} -> {target}); commit or discard them and run it again"
        )
        return 1
    if dry_run:
        say(f"self-update: dry-run: would update {version} -> {target} (checkout of {tag})")
        return 0

    code, before = git("rev-parse", "HEAD")
    code2, text = git("checkout", "--quiet", "--detach", f"refs/tags/{tag}")
    if code2 != 0:
        say(f"self-update: could not check out {tag} ({_first_line(text)}); nothing changed")
        return 1
    say(f"self-update: updated {version} -> {target} (checked out {tag})")
    return _after_checkout(git, run, root, python, before.strip(), target, say)


def _after_checkout(
    git: Callable[..., tuple[int, str]],
    run: Runner,
    root: Path,
    python: str,
    before: str,
    target: str,
    say: Callable[[str], None],
) -> int:
    """An editable install follows the checkout by itself: `warpzone --version` reads
    `__version__` from the files at `root`. Only the install metadata (entry points, the
    package list) can go stale, and it can only change with pyproject.toml, so the package
    is installed again only then."""
    status = 0
    code, changed = git("diff", "--name-only", before, "HEAD", "--", "pyproject.toml")
    if code == 0 and changed.strip():
        command = [python, "-m", "pip", "install", "-e", str(root)]
        try:
            code, text = run(command)
        except (OSError, subprocess.SubprocessError) as exc:
            code, text = 1, str(exc)
        if code == 0:
            say("self-update: pyproject.toml changed, so the editable install was refreshed")
        else:
            manual = " ".join(command)
            say(
                f"self-update: pyproject.toml changed but the editable install could not be "
                f"refreshed ({_first_line(text)}). Run: {manual}"
            )
            status = 1
    try:
        code, shown = run([python, "-I", "-c", "import warpzone; print(warpzone.__version__)"])
    except (OSError, subprocess.SubprocessError):
        return status
    reported = shown.strip().splitlines()[-1] if code == 0 and shown.strip() else None
    if reported == target:
        say(f"self-update: warpzone --version now {reported}")
    elif reported is not None:
        say(f"self-update: warning: warpzone --version now {reported}, but the tag is v{target}")
    return status
