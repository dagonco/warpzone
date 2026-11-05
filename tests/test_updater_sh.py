"""`warpzone/data/updater.sh`, run against real git repositories and a fake Python.

The script is POSIX sh that decides which release a device runs and puts it back when a
release does not work (development/design/autonomous-updates.md). Each test builds an upstream
repository with tagged releases, a clone of it, and a fake `python` (a script that behaves
like warpzone according to the checked-out version), then runs `updater.sh tick` the way
the timer does. They run with whatever shells exist (`sh`, `dash`, `bash`) so a macOS and a
Linux run both cover the bashisms that would break on the other.
"""

import atexit
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from typing import ClassVar

from support import FakeProc
from warpzone.procs import running
from warpzone.versions import tag_key

UPDATER = Path(__file__).resolve().parent.parent / "warpzone" / "data" / "updater.sh"
NOW = 1_700_000_000
DAY = 86400
HOUR = 3600
GIT = shutil.which("git")
SSH_KEYGEN = shutil.which("ssh-keygen")
SHELLS = [s for s in ("sh", "dash", "bash") if shutil.which(s)]

GIT_ENV = {
    "GIT_CONFIG_GLOBAL": os.devnull,
    "GIT_CONFIG_SYSTEM": os.devnull,
    "GIT_AUTHOR_NAME": "Test",
    "GIT_AUTHOR_EMAIL": "test@example.invalid",
    "GIT_COMMITTER_NAME": "Test",
    "GIT_COMMITTER_EMAIL": "test@example.invalid",
    "GIT_TERMINAL_PROMPT": "0",
}

# A script that imitates `python -m warpzone ...` for the checkout in $WZ_FAKE_CLONE. The
# checkout's `behaviour` file lists what that release does wrong; files in $WZ_FAKE_DIR
# switch the environment (and `behaviour.<version>` adds behaviour to one release later):
# `env_broken` fails provision and health for every release,
# `defer` makes provision exit 3 (the menu is open), `died` makes `die_once` die once.
FAKE_PYTHON = r"""#!/bin/sh
clone=$WZ_FAKE_CLONE
dir=$WZ_FAKE_DIR
echo "$* @ $(sed -n 's/^__version__ = "\(.*\)"$/\1/p' "$clone/warpzone/__init__.py")" >>"$dir/calls"
version=$(sed -n 's/^__version__ = "\(.*\)"$/\1/p' "$clone/warpzone/__init__.py")
beh="$(cat "$clone/behaviour" 2>/dev/null) $(cat "$dir/behaviour.$version" 2>/dev/null)"
has() { case " $beh " in *" $1 "*) return 0 ;; esac; return 1; }
# a power cut: the updater (its pid is in its lock) dies, and so does this step's group
power_cut() {
  kill -s KILL "$(cat "$WZ_STATE_DIR/lock/pid" 2>/dev/null)" 2>/dev/null
  kill -s KILL 0
}
case "$*" in
  *tomllib*) exit 0 ;;
  *"warpzone.cli"*)
    if has import_broken; then echo "ImportError: broken" >&2; exit 1; fi
    # the installed warpzone is not the one in the clone (the updater's check exits 7)
    [ -e "$dir/wrong_install" ] && [ $# -ge 4 ] && exit 7
    exit 0 ;;
  "-I -m warpzone --version")
    if has hang_tree_version; then sleep 60 & echo $! >"$dir/grandchild.pid"; exec sleep 60; fi
    if has wrong_version; then echo "warpzone 0.0.0"; else echo "warpzone $version"; fi
    exit 0 ;;
  "-m warpzone provision device "*)
    # what the real provision does: install the script of this release as the live copy
    if has install_updater; then
      cp "$clone/warpzone/data/updater.sh" "$WZ_FAKE_LIVE.new"
      mv -f "$WZ_FAKE_LIVE.new" "$WZ_FAKE_LIVE"
    fi
    # a power cut left the kept copy of the updater empty or damaged
    if has corrupt_good; then
      printf '%s' "$(cat "$dir/good_content")" >"$WZ_STATE_DIR/updater.sh.good"
    fi
    [ -e "$dir/env_broken" ] && exit 1
    if has spend; then
      # this step "takes" 99 seconds of the tick's budget (the uptime is the budget's clock)
      up=$(cut -d. -f1 "$WZ_PROC/uptime")
      echo "$((up + 99)).00 0" >"$WZ_PROC/uptime"
    fi
    if has hang_tree; then sleep 60 & echo $! >"$dir/grandchild.pid"; exec sleep 60; fi
    if has flaky && [ ! -e "$dir/flaky_done" ]; then : >"$dir/flaky_done"; exit 1; fi
    if has die; then power_cut; fi
    if has die_once && [ ! -e "$dir/died" ]; then : >"$dir/died"; power_cut; fi
    if has hang; then exec sleep 60; fi
    [ -e "$dir/defer" ] && exit 3
    if has provision_fails; then echo "provision failed" >&2; exit 1; fi
    exit 0 ;;
  "-m warpzone health "*)
    [ -e "$dir/env_broken" ] && exit 1
    if has health_fails; then exit 1; fi
    exit 0 ;;
esac
echo "fake python: unexpected arguments: $*" >&2
exit 64
"""


def git(cwd: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", "-C", str(cwd), *args],
        capture_output=True,
        text=True,
        env={**os.environ, **GIT_ENV},
        check=False,
    )
    if proc.returncode != 0:
        raise AssertionError(f"git {' '.join(args)}: {proc.stderr}")
    return proc.stdout.strip()


class SigningKey:
    """An SSH key made for one test, and the allowed-signers line that trusts it."""

    def __init__(self, folder: Path, name: str = "signer") -> None:
        folder.mkdir(parents=True, exist_ok=True)
        self.private = folder / name
        subprocess.run(
            ["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", "t", "-f", str(self.private)],
            check=True,
            capture_output=True,
        )
        public = self.private.with_suffix(".pub").read_text().strip()
        self.allowed = f'test@example.invalid namespaces="git" {public}\n'


_TEMPLATE: Path | None = None


def _repos_template() -> Path:
    """A folder holding `origin.git`, `work` and `clone` with release 0.1.0 published and the
    clone on it, built once per run: every `Rig` copies it instead of running about ten git
    processes to build the same three repositories."""
    global _TEMPLATE
    if _TEMPLATE is None:
        template = Path(tempfile.mkdtemp(prefix="wz-updater-template-"))
        atexit.register(shutil.rmtree, template, True)
        builder = Rig.__new__(Rig)  # only the repositories, none of the rest of a rig
        builder.root = template
        builder.origin = template / "origin.git"
        builder.work = template / "work"
        builder.clone = template / "clone"
        for args in (
            ["git", "init", "-q", "--bare", "-b", "main", str(builder.origin)],
            ["git", "init", "-q", "-b", "main", str(builder.work)],
        ):
            subprocess.run(args, check=True, env={**os.environ, **GIT_ENV})
        git(builder.work, "remote", "add", "origin", str(builder.origin))
        builder.release("0.1.0")
        subprocess.run(
            ["git", "clone", "-q", str(builder.origin), str(builder.clone)],
            check=True,
            env={**os.environ, **GIT_ENV},
        )
        git(builder.clone, "checkout", "-q", "--detach", "v0.1.0")
        _TEMPLATE = template
    return _TEMPLATE


class Rig:
    """An upstream repository, a clone of it, a fake Python and the updater's folders."""

    def __init__(self, test: unittest.TestCase) -> None:
        self.test = test
        self.root = Path(tempfile.mkdtemp(prefix="wz-updater-"))
        test.addCleanup(shutil.rmtree, self.root, True)
        self.origin = self.root / "origin.git"
        self.work = self.root / "work"
        self.clone = self.root / "clone"
        self.state_dir = self.root / "state" / "updater"
        self.conf = self.state_dir / "conf"
        self.proc = FakeProc(self.root / "proc")
        self.fake_dir = self.root / "fake"
        self.boot_id = self.root / "boot_id"
        self.updater = self.root / "live" / "updater.sh"
        self.python = self.root / "bin" / "python"
        for folder in (self.fake_dir, self.state_dir, self.updater.parent):
            folder.mkdir(parents=True)
        self.python.parent.mkdir()
        self.python.write_text(FAKE_PYTHON)
        self.python.chmod(0o755)
        # `sync` flushes the whole machine and is slow on a developer's laptop; nothing here
        # looks at it, so the ticks run with one that does nothing (`selftest` still needs it).
        nosync = self.python.parent / "sync"
        nosync.write_text("#!/bin/sh\nexit 0\n")
        nosync.chmod(0o755)
        self.boot_id.write_text("boot-1\n")
        shutil.copy(UPDATER, self.updater)
        self._copy_repos()
        self.write_conf()

    def _copy_repos(self) -> None:
        """Give this rig its own copy of the repositories, pointing at its own origin."""
        template = _repos_template()
        for name in ("origin.git", "work", "clone"):
            shutil.copytree(template / name, self.root / name, symlinks=True)
        # the only place a copy still names the template is where it remembers its origin
        for repo in (self.work, self.clone):
            for name in ("config", "FETCH_HEAD"):
                path = repo / ".git" / name
                if path.exists():
                    path.write_text(path.read_text().replace(str(template), str(self.root)))

    # --- the repositories -------------------------------------------------------------

    def release(
        self,
        version: str,
        behaviour: str = "",
        *,
        tag: str | None = None,
        on_main: bool = True,
        file_version: str | None = None,
        extra: dict[str, str] | None = None,
        sign: SigningKey | None = None,
        raw_tag: str | None = None,
    ) -> str:
        """Commit a release in the upstream repository, tag it and publish it."""
        if not on_main:
            git(self.work, "checkout", "-q", "--detach")
        for name, text in (extra or {}).items():
            (self.work / name).parent.mkdir(parents=True, exist_ok=True)
            (self.work / name).write_text(text)
        (self.work / "warpzone").mkdir(exist_ok=True)
        (self.work / "warpzone" / "__init__.py").write_text(
            f'"""fake"""\n\n__version__ = "{file_version or version}"\n'
        )
        (self.work / "behaviour").write_text(behaviour + "\n")
        git(self.work, "add", "-A")
        git(self.work, "commit", "-q", "-m", f"Release {version}")
        name = tag or f"v{version}"
        if raw_tag is not None:
            # A tag object written by hand ({sha} and {name} are filled in): git mktag does not
            # look at the signature, so it can hold any kind of block.
            body = raw_tag.format(sha=git(self.work, "rev-parse", "HEAD"), name=name)
            made = subprocess.run(
                ["git", "-C", str(self.work), "mktag"],
                input=body,
                capture_output=True,
                text=True,
                env={**os.environ, **GIT_ENV},
                check=True,
            )
            git(self.work, "update-ref", f"refs/tags/{name}", made.stdout.strip())
        elif sign is None:
            git(self.work, "tag", name)
        else:
            git(
                self.work,
                *("-c", "gpg.format=ssh", "-c", f"user.signingkey={sign.private}"),
                *("tag", "-s", "-m", f"warpzone {version}", name),
            )
        if on_main:
            git(self.work, "push", "-q", "origin", "main", f"refs/tags/{name}")
        else:
            git(self.work, "push", "-q", "origin", f"refs/tags/{name}")
            git(self.work, "checkout", "-q", "main")
        return git(self.work, "rev-parse", "HEAD" if on_main else f"refs/tags/{name}^{{commit}}")

    def head(self) -> str:
        return git(self.clone, "rev-parse", "HEAD")

    def retire(
        self,
        tag: str,
        reason: str = "it is withdrawn",
        *,
        sign: SigningKey | None = None,
        annotated: bool = True,
        at: str | None = None,
        nested: bool = False,
    ) -> None:
        """Retire a release the way the maintainer does: a tag `retired/<tag>` on the commit
        of the release (or of `at`), annotated and signed with `sign`, whose message is the
        reason, published. With `nested` it is made on the release's own tag, as
        `git tag -s retired/v1.2.3 v1.2.3` does, and so it points at a tag object."""
        name = f"retired/{tag}"
        target = (at or tag) if nested else f"{at or tag}^{{commit}}"
        try:
            git(self.work, "rev-parse", "-q", "--verify", target)
        except AssertionError:
            target = "HEAD"  # a name that is no release has no commit of its own
        if not annotated:
            git(self.work, "tag", name, target)
        elif sign is None:
            git(self.work, "tag", "-a", "-m", reason, name, target)
        else:
            signing = ("-c", "gpg.format=ssh", "-c", f"user.signingkey={sign.private}")
            git(self.work, *signing, "tag", "-s", "-m", reason, name, target)
        git(self.work, "push", "-q", "origin", f"refs/tags/{name}")

    def move_to(self, tag: str) -> None:
        """Put the clone on a tag by hand, as if the device had been installed there."""
        git(self.clone, "fetch", "-q", "--tags", "origin")
        git(self.clone, "checkout", "-q", "--detach", tag)

    def allow(self, *keys: SigningKey, name: str = "allowed_signers") -> Path:
        """Write an allowed-signers file that trusts `keys`, and return its path."""
        path = self.root / name
        path.write_text("# test signers\n" + "".join(k.allowed for k in keys))
        return path

    def on_tag(self) -> str:
        """The release tag the clone is on, or '' when it is not exactly on one."""
        try:
            return git(
                self.clone, "describe", "--exact-match", "--tags", "--match", "v[0-9]*", "HEAD"
            )
        except AssertionError:
            return ""

    def branch(self) -> str:
        try:
            return git(self.clone, "symbolic-ref", "-q", "--short", "HEAD")
        except AssertionError:
            return ""

    # --- the updater's inputs ---------------------------------------------------------

    def write_conf(self, **values: str) -> None:
        conf = {
            "device": "deck",
            "python": str(self.python),
            "clone": str(self.clone),
            "enabled": "true",
        }
        conf.update(values)
        self.conf.write_text("".join(f"{k}={v}\n" for k, v in conf.items() if v is not None))

    def cut_checkouts(self, mode: str) -> None:
        """Make the first checkout that writes `zz.txt` die in the middle, after the files
        before it were written: `kill` takes the whole process group down (a power cut),
        `fail` makes git stop with an error (a full disk). Later checkouts work."""
        marker = self.root / "cut-marker"
        action = "kill -s KILL 0" if mode == "kill" else "exit 1"
        script = self.root / "smudge.sh"
        script.write_text(f'if [ -e "{marker}" ]; then cat; else : >"{marker}"; {action}; fi\n')
        git(self.clone, "config", "filter.cut.smudge", f"sh {script}")
        git(self.clone, "config", "filter.cut.clean", "cat")
        git(self.clone, "config", "filter.cut.required", "true")
        info = self.clone / ".git" / "info"
        info.mkdir(exist_ok=True)
        (info / "attributes").write_text("zz.txt filter=cut\n")

    def age_index_lock(self, minutes: int = 11) -> None:
        lock = self.clone / ".git" / "index.lock"
        if lock.exists():
            old = lock.stat().st_mtime - minutes * 60
            os.utime(lock, (old, old))

    def flag(self, name: str, on: bool = True) -> None:
        path = self.fake_dir / name
        if on:
            path.touch()
        else:
            path.unlink(missing_ok=True)

    def break_version(self, version: str, behaviour: str) -> None:
        (self.fake_dir / f"behaviour.{version}").write_text(behaviour + "\n")

    def calls(self) -> list[str]:
        """What the fake Python was asked to run, without the interpreter probes."""
        path = self.fake_dir / "calls"
        lines = path.read_text().splitlines() if path.exists() else []
        return [line for line in lines if "tomllib" not in line]

    def provisions(self) -> list[str]:
        return [c for c in self.calls() if "provision device" in c]

    # --- running it -------------------------------------------------------------------

    def env(self, now: int | None = NOW, **extra: str) -> dict[str, str]:
        env = {
            **os.environ,
            **GIT_ENV,
            "HOME": str(self.root / "home"),
            "WZ_STATE_DIR": str(self.state_dir),
            "WZ_CONF": str(self.conf),
            "WZ_PROC": str(self.proc.path),
            "WZ_BOOT_ID_FILE": str(self.boot_id),
            "WZ_FAKE_CLONE": str(self.clone),
            "WZ_FAKE_DIR": str(self.fake_dir),
            "WZ_FAKE_LIVE": str(self.updater),
            "WZ_LIVE": str(self.updater),
            "WZ_FETCH_INTERVAL": "0",
            "WZ_STEP_TIMEOUT": "20",
            "PATH": f"{self.python.parent}{os.pathsep}{os.environ['PATH']}",
        }
        if now is not None:
            env["WZ_NOW"] = str(now)
        env.update(extra)
        return env

    def run(
        self, *args: str, shell: str | None = None, now: int | None = NOW, **extra: str
    ) -> subprocess.CompletedProcess:
        shell = shell or os.environ.get("WZ_TEST_SHELL", "sh")
        out = self.root / "output.txt"
        with out.open("ab") as fh:
            proc = subprocess.run(
                [shell, str(self.updater), *args],
                env=self.env(now, **extra),
                stdin=subprocess.DEVNULL,
                stdout=fh,
                stderr=subprocess.STDOUT,
                start_new_session=True,  # a "power cut" in the fake kills its own session
                timeout=120,
                check=False,
            )
        proc.output = out.read_text()  # type: ignore[attr-defined]
        return proc

    def tick(self, *args: str, **kwargs) -> subprocess.CompletedProcess:
        return self.run("tick", *args, **kwargs)

    def state(self) -> dict[str, str]:
        path = self.state_dir / "state"
        if not path.exists():
            return {}
        return dict(line.split("=", 1) for line in path.read_text().splitlines() if "=" in line)

    def write_state(self, **values: str) -> None:
        (self.state_dir / "state").write_text("".join(f"{k}={v}\n" for k, v in values.items()))

    def bad(self) -> list[str]:
        folder = self.state_dir / "bad"
        return sorted(p.name for p in folder.iterdir()) if folder.is_dir() else []


class UpdaterCase(unittest.TestCase):
    maxDiff = None

    def setUp(self) -> None:
        if os.environ.get("WARPZONE_SKIP_UPDATER_SH"):
            self.skipTest("WARPZONE_SKIP_UPDATER_SH is set (CI runs this file in its own job)")
        if not GIT:
            self.skipTest("git is not installed")
        if not UPDATER.exists():
            self.fail(f"{UPDATER} does not exist")
        self.rig = Rig(self)

    def assertTick(self, proc, code: int) -> None:
        self.assertEqual(proc.returncode, code, proc.output)


class UpdateTests(UpdaterCase):
    def test_a_good_release_is_installed_and_verified_in_three_steps(self) -> None:
        for shell in SHELLS:
            with self.subTest(shell=shell):
                rig = Rig(self)
                rig.release("0.2.0")
                self.assertTick(rig.tick(shell=shell), 0)
                self.assertEqual(rig.on_tag(), "v0.2.0")
                state = rig.state()
                self.assertEqual(state["good_tag"], "v0.2.0")
                self.assertEqual(state["good_sha"], rig.head())
                self.assertEqual(state["trying_tag"], "")
                self.assertEqual(state["attempts"], "0")
                self.assertEqual(state["probation"], "1")
                self.assertEqual(state["last_result"], "ok")
                self.assertEqual(rig.bad(), [])
                steps = [c.split(" @ ")[0] for c in rig.calls()]
                self.assertEqual(
                    steps,
                    [
                        steps[0],  # the import check
                        "-I -m warpzone --version",
                        "-m warpzone provision device deck --unattended",
                        "-m warpzone health deck",
                    ],
                )
                self.assertIn("import os, sys, warpzone.cli", steps[0])
                self.assertTrue(steps[0].endswith(str(rig.clone)), steps[0])
                # the checks ran on the new release, not the old one
                self.assertTrue(all(c.endswith("@ 0.2.0") for c in rig.calls() if "device" in c))

    def test_nothing_to_do_when_the_clone_is_on_the_newest_release(self) -> None:
        rig = self.rig
        self.assertTick(rig.tick(), 0)
        self.assertEqual(rig.on_tag(), "v0.1.0")
        self.assertEqual(rig.state()["good_tag"], "v0.1.0")
        self.assertEqual(rig.state()["last_result"], "ok")

    def test_the_target_is_the_highest_valid_stable_tag_by_number(self) -> None:
        rig = self.rig
        rig.release("0.9.0")
        rig.release("0.10.0")
        rig.release("0.11.0rc1", tag="v0.11.0rc1")  # a release candidate is not followed yet
        rig.release("0.12.0", on_main=False)  # not an ancestor of origin/main
        rig.release("0.13.0", file_version="0.12.9")  # the version in the tag disagrees
        self.assertTick(rig.tick(), 0)
        self.assertEqual(rig.on_tag(), "v0.10.0")

    def test_it_never_goes_down(self) -> None:
        rig = self.rig
        rig.release("0.1.5")
        rig.release("0.2.0")
        git(rig.clone, "fetch", "-q", "--tags")
        git(rig.clone, "checkout", "-q", "--detach", "v0.2.0")
        self.assertTick(rig.tick(), 0)
        self.assertEqual(rig.on_tag(), "v0.2.0")
        self.assertEqual(rig.state()["good_tag"], "v0.2.0")

    def test_an_interval_in_the_environment_wins_over_the_channel(self) -> None:
        rig = self.rig
        rig.write_conf(channel="rc")
        self.assertTick(rig.tick(WZ_FETCH_INTERVAL=str(DAY)), 0)
        rig.release("0.2.0")
        self.assertTick(rig.tick(now=NOW + HOUR, WZ_FETCH_INTERVAL=str(DAY)), 0)
        self.assertEqual(rig.on_tag(), "v0.1.0")
        self.assertTick(rig.tick(now=NOW + 26 * HOUR, WZ_FETCH_INTERVAL=str(DAY)), 0)
        self.assertEqual(rig.on_tag(), "v0.2.0")

    def test_the_rc_channel_looks_for_a_release_every_hour(self) -> None:
        rig = self.rig
        rig.write_conf(channel="rc")
        self.assertTick(rig.tick(WZ_FETCH_INTERVAL=""), 0)
        rig.release("0.2.0rc1")
        self.assertTick(rig.tick(now=NOW + HOUR // 2, WZ_FETCH_INTERVAL=""), 0)
        self.assertEqual(rig.on_tag(), "v0.1.0")
        # The delay of each device is below a twenty-fourth of the interval.
        self.assertTick(rig.tick(now=NOW + HOUR + HOUR // 24 + 1, WZ_FETCH_INTERVAL=""), 0)
        self.assertEqual(rig.on_tag(), "v0.2.0rc1")

    def test_the_stable_channel_looks_for_a_release_every_twelve_hours(self) -> None:
        rig = self.rig
        self.assertTick(rig.tick(WZ_FETCH_INTERVAL=""), 0)
        rig.release("0.2.0")
        self.assertTick(rig.tick(now=NOW + 11 * HOUR, WZ_FETCH_INTERVAL=""), 0)
        self.assertEqual(rig.on_tag(), "v0.1.0")
        self.assertTick(rig.tick(now=NOW + 12 * HOUR + HOUR // 2 + 1, WZ_FETCH_INTERVAL=""), 0)
        self.assertEqual(rig.on_tag(), "v0.2.0")

    def test_a_pin_looks_for_tags_at_the_stable_pace(self) -> None:
        rig = self.rig
        rig.write_conf(channel="rc", pin="v0.1.0")
        self.assertTick(rig.tick(WZ_FETCH_INTERVAL=""), 0)
        self.assertEqual(rig.state()["last_fetch"], str(NOW))
        self.assertTick(rig.tick(now=NOW + 2 * HOUR, WZ_FETCH_INTERVAL=""), 0)
        self.assertEqual(rig.state()["last_fetch"], str(NOW))
        later = NOW + 12 * HOUR + HOUR // 2 + 1
        self.assertTick(rig.tick(now=later, WZ_FETCH_INTERVAL=""), 0)
        self.assertEqual(rig.state()["last_fetch"], str(later))

    def test_fetch_now_looks_for_a_release_before_it_is_due(self) -> None:
        rig = self.rig
        self.assertTick(rig.tick(WZ_FETCH_INTERVAL=""), 0)
        rig.release("0.2.0")
        self.assertTick(rig.tick("--fetch-now", now=NOW + 60, WZ_FETCH_INTERVAL=""), 0)
        self.assertEqual(rig.on_tag(), "v0.2.0")

    def test_fetch_now_still_waits_while_the_menu_is_open(self) -> None:
        rig = self.rig
        self.assertTick(rig.tick(WZ_FETCH_INTERVAL=""), 0)
        rig.release("0.2.0")
        rig.proc.add(123, "es-de", ["es-de"])
        proc = rig.tick("--fetch-now", now=NOW + 60, WZ_FETCH_INTERVAL="")
        self.assertTick(proc, 0)
        self.assertIn("menu is open", proc.output)
        self.assertEqual(rig.on_tag(), "v0.1.0")

    def test_tick_refuses_an_unknown_option(self) -> None:
        proc = self.rig.tick("--bogus")
        self.assertEqual(proc.returncode, 2, proc.output)
        self.assertIn("usage: updater.sh tick [--fetch-now] [--now]", proc.output)

    def test_a_disabled_device_is_left_alone(self) -> None:
        rig = self.rig
        rig.release("0.2.0")
        rig.write_conf(enabled="false")
        self.assertTick(rig.tick(), 0)
        self.assertEqual(rig.on_tag(), "v0.1.0")
        self.assertEqual(rig.calls(), [])

    def test_state_and_conf_are_read_as_data_never_run(self) -> None:
        rig = self.rig
        marker = rig.root / "pwned"
        (rig.state_dir / "state").write_text(
            f"good_tag=$(touch {marker})\n`touch {marker}`=1\nbad key=1\n"
            f"good_sha=$(touch {marker})\nattempts=$(touch {marker})\n"
        )
        rig.write_conf(device=f"$(touch {marker})", frontend=f"`touch {marker}`")
        rig.release("0.2.0")
        rig.tick()
        self.assertFalse(marker.exists())

    def test_a_missing_python_is_reported_and_nothing_moves(self) -> None:
        rig = self.rig
        rig.release("0.2.0")
        rig.write_conf(python=str(rig.root / "nowhere" / "python"))
        proc = rig.tick(WZ_PYTHON_FALLBACKS="")
        self.assertTick(proc, 1)
        self.assertEqual(rig.state()["last_result"], "no-python")
        self.assertEqual(rig.on_tag(), "v0.1.0")
        self.assertEqual(rig.bad(), [])


class VerdictTests(UpdaterCase):
    def test_a_release_that_fails_a_check_is_marked_bad_when_the_good_one_passes(self) -> None:
        cases = {
            "import_broken": "import check failed",
            "wrong_version": "version",
            "provision_fails": "provision",
            "health_fails": "health",
        }
        for behaviour, reason in cases.items():
            with self.subTest(behaviour=behaviour):
                rig = Rig(self)
                bad_sha = rig.release("0.2.0", behaviour)
                self.assertTick(rig.tick(), 1)
                self.assertEqual(rig.on_tag(), "v0.1.0", "back on the good version")
                self.assertEqual(rig.bad(), ["v0.2.0"])
                record = (rig.state_dir / "bad" / "v0.2.0").read_text()
                self.assertIn(f"sha={bad_sha}", record)
                self.assertIn("date=", record)
                self.assertIn(reason, record)
                state = rig.state()
                self.assertEqual(state["last_result"], "rolled-back")
                self.assertEqual(state["good_tag"], "v0.1.0")
                self.assertEqual(state["trying_tag"], "")
                self.assertEqual(state["attempts"], "0")
                # the good version was checked again after the failure
                self.assertIn("health deck @ 0.1.0", " ".join(rig.calls()), rig.calls())

    def test_a_bad_tag_never_comes_back_but_a_newer_one_is_tried(self) -> None:
        rig = self.rig
        rig.release("0.2.0", "provision_fails")
        rig.tick()
        before = rig.calls()
        self.assertTick(rig.tick(now=NOW + DAY), 0)
        self.assertEqual(rig.on_tag(), "v0.1.0")
        self.assertEqual([c for c in rig.calls()[len(before) :] if "@ 0.2.0" in c], [])
        rig.release("0.3.0")
        self.assertTick(rig.tick(now=NOW + 2 * DAY), 0)
        self.assertEqual(rig.on_tag(), "v0.3.0")
        self.assertEqual(rig.bad(), ["v0.2.0"])

    def test_a_broken_environment_does_not_burn_the_tag_and_backs_off(self) -> None:
        rig = self.rig
        rig.release("0.2.0")
        rig.flag("env_broken")
        now = NOW
        for failures, days in enumerate((1, 2, 4, 7, 7), start=1):
            proc = rig.tick(now=now)
            self.assertTick(proc, 1)
            state = rig.state()
            self.assertEqual(rig.bad(), [], "the tag is not marked bad")
            self.assertEqual(rig.on_tag(), "v0.1.0", "the device stays on the good version")
            self.assertEqual(state["last_result"], "env")
            self.assertEqual(state["env_failures"], str(failures))
            self.assertEqual(state["retry_after"], str(now + days * DAY))
            self.assertEqual(state["trying_tag"], "")
            # a tick before retry_after does not try again
            calls = len(rig.calls())
            self.assertTick(rig.tick(now=now + days * DAY - 1), 0)
            self.assertEqual(len(rig.calls()), calls)
            now += days * DAY
        rig.flag("env_broken", False)
        self.assertTick(rig.tick(now=now), 0)
        self.assertEqual(rig.on_tag(), "v0.2.0")
        self.assertEqual(rig.state()["env_failures"], "0")
        self.assertEqual(rig.state()["retry_after"], "")

    def test_a_menu_that_opens_during_the_check_keeps_the_tag_and_continues_later(self) -> None:
        rig = self.rig
        rig.release("0.2.0")
        rig.flag("defer")
        self.assertTick(rig.tick(), 0)
        state = rig.state()
        self.assertEqual(rig.on_tag(), "v0.2.0", "no rollback")
        self.assertEqual(state["trying_tag"], "v0.2.0")
        self.assertEqual(state["pending"], "1")
        self.assertEqual(state["last_result"], "deferred")
        self.assertEqual(state["attempts"], "0", "a deferral is not an attempt")
        self.assertEqual(rig.bad(), [])
        for later in (1, 2, 3):  # deferring again and again never uses up the attempts
            self.assertTick(rig.tick(now=NOW + later * 600), 0)
            self.assertEqual(rig.state()["trying_tag"], "v0.2.0")
        rig.flag("defer", False)
        self.assertTick(rig.tick(now=NOW + 3600), 0)
        state = rig.state()
        self.assertEqual(state["good_tag"], "v0.2.0")
        self.assertEqual(state["trying_tag"], "")
        self.assertEqual(state["pending"], "0")


class ByHandTests(UpdaterCase):
    """`tick --now` is an update asked for by hand (`warpzone self-update --now`, the
    Options entry): the menu it is asked from does not hold it back."""

    def test_now_updates_with_the_menu_open(self) -> None:
        rig = self.rig
        rig.release("0.2.0")
        rig.proc.add(7000001, "es-de")
        proc = rig.tick("--now")
        self.assertTick(proc, 0)
        self.assertEqual(rig.on_tag(), "v0.2.0")
        self.assertEqual(rig.state()["good_tag"], "v0.2.0")
        self.assertIn("asked for by hand", proc.output)

    def test_now_counts_a_check_held_back_by_the_menu_as_passed(self) -> None:
        rig = self.rig
        rig.release("0.2.0")
        rig.proc.add(7000001, "es-de")
        rig.flag("defer")
        self.assertTick(rig.tick("--now"), 0)
        state = rig.state()
        self.assertEqual(state["good_tag"], "v0.2.0")
        self.assertEqual(state["trying_tag"], "")
        self.assertEqual(state["pending"], "1", "the write held back is still to do")

    def test_now_does_not_wait_for_retry_after(self) -> None:
        rig = self.rig
        self.assertTick(rig.tick(), 0)
        rig.release("0.2.0")
        state = rig.state()
        state["retry_after"] = str(NOW + DAY)
        rig.write_state(**state)
        self.assertTick(rig.tick(now=NOW + 60), 0)
        self.assertEqual(rig.on_tag(), "v0.1.0", "the timer still waits")
        self.assertTick(rig.tick("--now", now=NOW + 120), 0)
        self.assertEqual(rig.on_tag(), "v0.2.0")

    def test_fetch_now_alone_still_waits_for_the_menu(self) -> None:
        rig = self.rig
        rig.release("0.2.0")
        rig.proc.add(7000001, "es-de")
        self.assertTick(rig.tick("--fetch-now"), 0)
        self.assertEqual(rig.on_tag(), "v0.1.0")


class ManualInstallTests(UpdaterCase):
    """`install=manual`: the timer only looks for releases; installing is asked for by hand."""

    def test_the_timer_fetches_and_notes_the_release_without_installing_it(self) -> None:
        rig = self.rig
        rig.write_conf(install="manual")
        rig.release("0.2.0")
        proc = rig.tick()
        self.assertTick(proc, 0)
        state = rig.state()
        self.assertEqual(rig.on_tag(), "v0.1.0")
        self.assertEqual(state["available"], "v0.2.0")
        self.assertEqual(state["last_fetch"], str(NOW))
        self.assertEqual(state["trying_tag"], "")
        self.assertEqual(rig.calls(), [], "no provision, no health")
        self.assertIn("v0.2.0 is available", proc.output)

    def test_the_menu_does_not_stop_the_timer_from_looking(self) -> None:
        rig = self.rig
        rig.write_conf(install="manual")
        rig.release("0.2.0")
        rig.proc.add(7000001, "es-de")
        self.assertTick(rig.tick(), 0)
        state = rig.state()
        self.assertEqual(state["available"], "v0.2.0")
        self.assertEqual(state["deferred_since"], "")
        self.assertEqual(state["last_result"], "available")

    def test_nothing_new_leaves_available_empty(self) -> None:
        rig = self.rig
        rig.write_conf(install="manual")
        self.assertTick(rig.tick(), 0)
        self.assertEqual(rig.state().get("available", ""), "")
        self.assertEqual(rig.calls(), [])

    def test_now_installs_and_clears_available(self) -> None:
        rig = self.rig
        rig.write_conf(install="manual")
        rig.release("0.2.0")
        rig.proc.add(7000001, "es-de")
        self.assertTick(rig.tick(), 0)
        self.assertTick(rig.tick("--now", now=NOW + 60), 0)
        state = rig.state()
        self.assertEqual(rig.on_tag(), "v0.2.0")
        self.assertEqual(state["good_tag"], "v0.2.0")
        self.assertEqual(state["available"], "")

    def test_a_dev_checkout_is_not_reconciled_by_the_timer(self) -> None:
        rig = self.rig
        rig.write_conf(install="manual")
        rig.release("0.2.0")
        git(rig.clone, "checkout", "-q", "-b", "mine", "v0.1.0")
        (rig.clone / "mine.txt").write_text("mine\n")
        git(rig.clone, "add", "mine.txt")
        git(rig.clone, "commit", "-q", "-m", "My change")
        rig.write_state(pending="1", last_boot_id="old-boot")
        proc = rig.tick()
        self.assertTick(proc, 0)
        state = rig.state()
        self.assertEqual(rig.calls(), [])
        self.assertEqual((state["pending"], state["last_boot_id"]), ("1", "old-boot"))
        self.assertEqual(state["last_result"], "dev")
        self.assertNotIn("failed", proc.output)

    def test_the_timer_finishes_a_trial_that_was_cut_short(self) -> None:
        rig = self.rig
        rig.write_conf(install="manual")
        rig.release("0.2.0", "die")
        self.assertNotEqual(rig.tick("--now").returncode, 0)
        self.assertEqual(rig.state()["trying_tag"], "v0.2.0")
        self.assertNotEqual(rig.tick(now=NOW + 900).returncode, 0)
        self.assertEqual(rig.state()["attempts"], "2", "resumed by the timer")
        self.assertTick(rig.tick(now=NOW + 1800), 1)
        state = rig.state()
        self.assertEqual(rig.on_tag(), "v0.1.0", "back on the version known to work")
        self.assertEqual((state["trying_tag"], state["last_result"]), ("", "rolled-back"))

    def test_a_watch_that_fetched_is_a_success(self) -> None:
        rig = self.rig
        rig.write_conf(install="manual")
        self.assertTick(rig.tick(), 0)
        state = rig.state()
        self.assertEqual((state["last_ok"], state["last_result"]), (str(NOW), "ok"))
        rig.release("0.2.0")
        self.assertTick(rig.tick(now=NOW + 2 * HOUR), 0)
        state = rig.state()
        self.assertEqual(
            (state["last_ok"], state["last_result"]), (str(NOW + 2 * HOUR), "available")
        )

    def test_an_unknown_install_value_is_auto(self) -> None:
        rig = self.rig
        rig.write_conf(install="sometimes")
        rig.release("0.2.0")
        proc = rig.tick()
        self.assertTick(proc, 0)
        self.assertEqual(rig.on_tag(), "v0.2.0")
        self.assertIn("install=sometimes", proc.output)


class EdgeTests(UpdaterCase):
    def test_an_unreachable_remote_is_logged_and_the_local_refs_are_used(self) -> None:
        rig = self.rig
        rig.release("0.2.0")
        git(rig.clone, "fetch", "-q", "--tags")  # fetched earlier, not yet applied
        git(rig.clone, "remote", "set-url", "origin", str(rig.root / "nowhere.git"))
        proc = rig.tick()
        self.assertTick(proc, 0)
        self.assertIn("could not fetch", proc.output)
        self.assertEqual(rig.on_tag(), "v0.2.0")
        self.assertEqual(rig.state()["last_fetch"], "")

    def test_a_good_version_that_cannot_be_checked_out_is_stuck_and_nothing_else_moves(
        self,
    ) -> None:
        rig = self.rig
        sha = rig.release("0.2.0")
        git(rig.clone, "fetch", "-q", "--tags")
        git(rig.clone, "checkout", "-q", "--detach", "v0.2.0")
        rig.write_state(
            good_tag="v0.1.0",
            good_sha="0123456789abcdef0123456789abcdef01234567",
            trying_tag="v0.2.0",
            trying_sha=sha,
            attempts="2",
        )
        self.assertTick(rig.tick(), 1)
        self.assertEqual(rig.state()["last_result"], "stuck")
        self.assertEqual(rig.head(), sha)
        self.assertEqual(rig.bad(), [])

    def test_after_the_deferral_limit_a_step_that_keeps_deferring_counts_as_passed(self) -> None:
        rig = self.rig
        rig.release("0.2.0")
        rig.flag("defer")  # provision keeps answering 3
        rig.proc.add(7000001, "es-de")  # the menu has been open since the first deferral
        rig.write_state(pending="1", deferred_since=str(NOW - 80 * HOUR))
        self.assertTick(rig.tick(), 0)
        state = rig.state()
        self.assertEqual(state["good_tag"], "v0.2.0")
        self.assertEqual(state["pending"], "1", "the work is still pending")
        self.assertEqual(rig.bad(), [])


class NoCloneTests(UpdaterCase):
    def test_a_tick_that_finds_no_clone_stops_and_touches_nothing(self) -> None:
        rig = self.rig
        rig.release("0.2.0")
        git(rig.clone, "fetch", "-q", "--tags")
        rig.write_conf(clone=None)  # no clone in the settings
        # the folder two levels above the script cannot be entered: a `dirname` that names none
        fake = rig.python.parent / "dirname"
        fake.write_text("#!/bin/sh\necho /nonexistent-folder/for/the/clone\n")
        fake.chmod(0o755)
        # the tick runs from inside a checkout, which is what an empty clone would end up acting on
        proc = subprocess.run(
            ["sh", str(rig.updater), "tick"],
            cwd=rig.clone,
            env=rig.env(),
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )
        output = proc.stdout + proc.stderr
        self.assertEqual(proc.returncode, 1, output)
        self.assertIn("no clone", output)
        self.assertEqual(rig.on_tag(), "v0.1.0")
        self.assertEqual(rig.calls(), [])
        self.assertFalse((rig.state_dir / "state").exists())


class PowerCutTests(UpdaterCase):
    def test_a_power_cut_halfway_does_not_burn_the_tag(self) -> None:
        rig = self.rig
        rig.release("0.2.0", "die")
        first = rig.tick()
        self.assertNotEqual(first.returncode, 0)
        state = rig.state()
        self.assertEqual((state["trying_tag"], state["attempts"]), ("v0.2.0", "1"))
        self.assertEqual(rig.on_tag(), "v0.2.0")
        self.assertTrue((rig.state_dir / "lock").exists(), "the dead run left its lock")
        second = rig.tick(now=NOW + 900)  # takes over the dead run's lock and resumes
        self.assertNotEqual(second.returncode, 0)
        self.assertEqual(rig.state()["attempts"], "2")
        third = rig.tick(now=NOW + 1800)  # two attempts without a verdict
        self.assertTick(third, 1)
        state = rig.state()
        self.assertEqual(rig.bad(), [], "a power cut never marks the tag bad")
        self.assertEqual(rig.on_tag(), "v0.1.0")
        self.assertEqual(state["good_tag"], "v0.1.0")
        self.assertEqual(state["trying_tag"], "")
        self.assertEqual(state["attempts"], "0")
        self.assertEqual(state["retry_after"], str(NOW + 1800 + DAY))
        self.assertEqual(state["last_result"], "rolled-back")
        # and it waits a day before trying again
        calls = len(rig.calls())
        self.assertTick(rig.tick(now=NOW + 1800 + DAY - 1), 0)
        self.assertEqual(len(rig.calls()), calls)

    def test_a_run_that_died_once_resumes_and_finishes(self) -> None:
        rig = self.rig
        rig.release("0.2.0", "die_once")
        self.assertNotEqual(rig.tick().returncode, 0)
        self.assertTick(rig.tick(now=NOW + 900), 0)
        state = rig.state()
        self.assertEqual(rig.on_tag(), "v0.2.0")
        self.assertEqual((state["good_tag"], state["attempts"]), ("v0.2.0", "0"))
        self.assertEqual(rig.bad(), [])

    def test_a_lock_held_by_a_live_run_stops_the_tick(self) -> None:
        rig = self.rig
        rig.release("0.2.0")
        holder = subprocess.Popen(["sleep", "30"])
        self.addCleanup(holder.kill)
        self.addCleanup(holder.wait)
        (rig.state_dir / "lock").mkdir()
        (rig.state_dir / "lock" / "pid").write_text(f"{holder.pid}\n")
        self.assertTick(rig.tick(), 0)
        self.assertEqual(rig.on_tag(), "v0.1.0")
        self.assertEqual(rig.calls(), [])
        holder.send_signal(signal.SIGKILL)
        holder.wait()
        self.assertTick(rig.tick(), 0)  # now the lock is stale and is taken over
        self.assertEqual(rig.on_tag(), "v0.2.0")
        self.assertFalse((rig.state_dir / "lock").exists(), "released at the end")


class MenuTests(UpdaterCase):
    def test_an_open_menu_defers_for_72_hours_and_then_updates_anyway(self) -> None:
        rig = self.rig
        rig.release("0.2.0")
        rig.proc.add(7000001, "es-de")
        self.assertTick(rig.tick(), 0)
        state = rig.state()
        self.assertEqual((state["pending"], state["last_result"]), ("1", "deferred"))
        self.assertEqual(state["deferred_since"], str(NOW))
        self.assertEqual(rig.on_tag(), "v0.1.0")
        self.assertEqual(rig.calls(), [])
        self.assertTick(rig.tick(now=NOW + 71 * HOUR), 0)
        self.assertEqual(rig.on_tag(), "v0.1.0")
        self.assertEqual(rig.state()["deferred_since"], str(NOW), "the clock keeps running")
        self.assertTick(rig.tick(now=NOW + 73 * HOUR), 0)
        state = rig.state()
        self.assertEqual(rig.on_tag(), "v0.2.0")
        self.assertEqual(state["good_tag"], "v0.2.0")
        self.assertEqual(state["pending"], "0")
        self.assertEqual(state["deferred_since"], "")

    def test_a_deferral_that_ends_when_the_menu_closes_updates_at_once(self) -> None:
        rig = self.rig
        rig.release("0.2.0")
        rig.proc.add(7000001, "ES-DE.AppImage")
        rig.tick()
        shutil.rmtree(rig.proc.path / "7000001")
        self.assertTick(rig.tick(now=NOW + 900), 0)
        self.assertEqual(rig.on_tag(), "v0.2.0")
        self.assertEqual(rig.state()["deferred_since"], "")

    def test_the_process_check_agrees_with_procs_module(self) -> None:
        trees: list[tuple[list[str], list[tuple[str, list[str]]]]] = [
            (["es-de"], []),
            (["es-de"], [("es-de", ["/usr/bin/es-de"])]),
            (["es-de", "ES-DE.AppImage"], [("ES-DE.AppImage", ["/x/ES-DE.AppImage"])]),
            (["es-de", "ES-DE.AppImage"], [("sh", ["/opt/apps/ES-DE.AppImage", "--fs"])]),
            (["es-de"], [("vim", ["vim", "es-de"])]),
            (["es-de"], [("bash", ["bash", "-c", "echo es-de"])]),
            (["es-de"], [("es-de-helper", ["es-de-helper"])]),
            (["emulationstation-long"], [("emulationstatio", ["emulationstation"])]),
            (["es-de", "other"], [("a", ["a"]), ("other", ["/bin/other"])]),
        ]
        for names, procs in trees:
            with self.subTest(names=names, procs=procs):
                rig = Rig(self)
                for pid, (comm, argv) in enumerate(procs, start=7000001):
                    rig.proc.add(pid, comm, argv)
                rig.write_conf(frontend=" ".join(names))
                expected = running(names, rig.proc.path)
                proc = rig.run("menu")
                self.assertEqual(proc.returncode == 0, expected, proc.output)
                self.assertIn(proc.returncode, (0, 1))

    def test_a_missing_proc_is_not_a_reason_to_wait(self) -> None:
        rig = self.rig
        rig.release("0.2.0")
        shutil.rmtree(rig.proc.path)
        self.assertTick(rig.tick(), 0)
        self.assertEqual(rig.on_tag(), "v0.2.0")


class CheckoutKindTests(UpdaterCase):
    def snapshot(self) -> tuple[str, str, str]:
        rig = self.rig
        return (rig.head(), rig.branch(), git(rig.clone, "status", "--porcelain"))

    def test_a_branch_with_commits_of_its_own_is_dev_and_does_not_move(self) -> None:
        rig = self.rig
        rig.release("0.2.0")
        git(rig.clone, "checkout", "-q", "-b", "mine", "v0.1.0")
        (rig.clone / "mine.txt").write_text("mine\n")
        git(rig.clone, "add", "mine.txt")
        git(rig.clone, "commit", "-q", "-m", "My change")
        before = self.snapshot()
        self.assertTick(rig.tick(), 0)
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(rig.state()["last_result"], "dev")
        self.assertEqual(git(rig.clone, "stash", "list"), "")
        self.assertEqual(rig.bad(), [])
        # it is still reconciled, once per boot
        self.assertEqual(len(rig.provisions()), 1)
        self.assertTick(rig.tick(), 0)
        self.assertEqual(len(rig.provisions()), 1)
        rig.boot_id.write_text("boot-2\n")
        self.assertTick(rig.tick(), 0)
        self.assertEqual(len(rig.provisions()), 2)

    def test_a_dirty_tree_is_not_moved_or_cleaned(self) -> None:
        rig = self.rig
        rig.release("0.2.0")
        (rig.clone / "warpzone" / "__init__.py").write_text("# local edit\n")
        (rig.clone / "scratch.txt").write_text("untracked\n")
        before = self.snapshot()
        self.assertTick(rig.tick(), 0)
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(rig.on_tag(), "v0.1.0")
        self.assertEqual(rig.state()["last_result"], "dirty")
        self.assertEqual((rig.clone / "warpzone" / "__init__.py").read_text(), "# local edit\n")
        self.assertTrue((rig.clone / "scratch.txt").exists())
        self.assertEqual(git(rig.clone, "stash", "list"), "")

    def test_a_loose_commit_is_dev_and_does_not_move(self) -> None:
        rig = self.rig
        rig.release("0.2.0")
        (rig.clone / "loose.txt").write_text("x\n")
        git(rig.clone, "add", "loose.txt")
        git(rig.clone, "commit", "-q", "-m", "Loose")
        before = self.snapshot()
        self.assertTick(rig.tick(), 0)
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(rig.state()["last_result"], "dev")

    def test_a_clean_branch_inside_origin_main_is_adopted_and_moves_to_the_tag(self) -> None:
        rig = self.rig
        git(rig.clone, "checkout", "-q", "main")
        rig.release("0.2.0")
        self.assertEqual(rig.branch(), "main")
        self.assertTick(rig.tick(), 0)
        self.assertEqual(rig.on_tag(), "v0.2.0")
        self.assertEqual(rig.state()["good_tag"], "v0.2.0")

    def test_an_adopted_branch_comes_back_when_the_release_is_bad(self) -> None:
        rig = self.rig
        git(rig.clone, "checkout", "-q", "main")
        rig.release("0.2.0", "provision_fails")
        self.assertTick(rig.tick(), 1)
        self.assertEqual(rig.branch(), "main", "the branch it came from, not a loose commit")
        self.assertEqual(rig.bad(), ["v0.2.0"])
        # and it is still adopted: the next release is followed
        rig.release("0.3.0")
        self.assertTick(rig.tick(now=NOW + DAY), 0)
        self.assertEqual(rig.on_tag(), "v0.3.0")


class IndexLockTests(UpdaterCase):
    def lock(self) -> Path:
        path = self.rig.clone / ".git" / "index.lock"
        path.write_text("")
        return path

    def age(self, path: Path, minutes: int) -> None:
        old = path.stat().st_mtime - minutes * 60
        os.utime(path, (old, old))

    def test_an_old_lock_with_no_git_alive_is_removed(self) -> None:
        lock = self.lock()
        self.age(lock, 11)
        self.assertTick(self.rig.tick(), 0)
        self.assertFalse(lock.exists())

    def test_a_recent_lock_stays(self) -> None:
        lock = self.lock()
        self.age(lock, 2)
        self.assertTick(self.rig.tick(), 0)
        self.assertTrue(lock.exists())

    def test_an_old_lock_stays_while_git_is_alive(self) -> None:
        lock = self.lock()
        self.age(lock, 30)
        self.rig.proc.add(7000001, "git", ["git", "gc"])
        self.assertTick(self.rig.tick(), 0)
        self.assertTrue(lock.exists())

    def test_an_old_lock_is_removed_so_the_update_can_go_on(self) -> None:
        rig = self.rig
        rig.release("0.2.0")
        lock = self.lock()
        self.age(lock, 20)
        self.assertTick(rig.tick(), 0)
        self.assertEqual(rig.on_tag(), "v0.2.0")


class ProbationTests(UpdaterCase):
    def test_a_good_release_leaves_probation_at_the_next_boot(self) -> None:
        rig = self.rig
        rig.release("0.2.0")
        self.assertTick(rig.tick(), 0)
        self.assertEqual(rig.state()["probation"], "1")
        self.assertTick(rig.tick(now=NOW + 900), 0)
        self.assertEqual(rig.state()["probation"], "1", "same boot: still on probation")
        rig.boot_id.write_text("boot-2\n")
        self.assertTick(rig.tick(now=NOW + 1800), 0)
        self.assertEqual(rig.state()["probation"], "0")
        self.assertEqual(rig.on_tag(), "v0.2.0")

    def test_a_release_that_breaks_at_the_next_boot_goes_back_and_is_marked_bad(self) -> None:
        rig = self.rig
        rig.release("0.2.0")
        self.assertTick(rig.tick(), 0)
        rig.break_version("0.2.0", "provision_fails")  # it passed once; after the reboot it fails
        rig.boot_id.write_text("boot-2\n")
        self.assertTick(rig.tick(now=NOW + 1800), 1)
        self.assertEqual(rig.bad(), ["v0.2.0"])
        self.assertEqual(rig.on_tag(), "v0.1.0")
        state = rig.state()
        self.assertEqual((state["good_tag"], state["probation"]), ("v0.1.0", "0"))
        self.assertEqual(state["last_result"], "rolled-back")

    def test_a_broken_environment_after_the_boot_keeps_the_new_release(self) -> None:
        rig = self.rig
        rig.release("0.2.0")
        self.assertTick(rig.tick(), 0)
        rig.flag("env_broken")  # for example Syncthing is not up yet after the boot
        rig.boot_id.write_text("boot-2\n")
        self.assertTick(rig.tick(now=NOW + 1800), 1)
        self.assertEqual(rig.bad(), [])
        self.assertEqual(rig.on_tag(), "v0.2.0")
        self.assertEqual(rig.state()["last_result"], "env")


class HalfCheckoutTests(UpdaterCase):
    def status(self) -> str:
        return git(self.rig.clone, "status", "--porcelain", "--untracked-files=no")

    def test_a_power_cut_in_the_middle_of_the_checkout_is_repaired(self) -> None:
        rig = self.rig
        rig.release("0.2.0", extra={"zz.txt": "later\n"})
        rig.cut_checkouts("kill")
        self.assertNotEqual(rig.tick().returncode, 0)
        self.assertNotEqual(self.status(), "", "the cut left a mixed tree")
        self.assertEqual(rig.state()["trying_tag"], "v0.2.0")
        rig.age_index_lock()  # the killed git left its lock; the next tick comes later
        self.assertTick(rig.tick(now=NOW + 900), 0)
        self.assertEqual(self.status(), "", "the updater finished its own checkout")
        self.assertEqual(rig.on_tag(), "v0.2.0")
        state = rig.state()
        self.assertEqual((state["good_tag"], state["last_result"]), ("v0.2.0", "ok"))
        self.assertEqual(rig.bad(), [])
        self.assertTrue((rig.clone / "zz.txt").exists())

    def test_a_checkout_that_fails_in_the_middle_goes_back_to_the_good_version(self) -> None:
        rig = self.rig
        rig.release("0.2.0", extra={"zz.txt": "later\n"})
        rig.cut_checkouts("fail")
        self.assertTick(rig.tick(), 1)
        self.assertEqual(self.status(), "", "no mixed tree is left behind")
        self.assertEqual(rig.on_tag(), "v0.1.0")
        state = rig.state()
        self.assertEqual((state["last_result"], state["trying_tag"]), ("env", ""))
        self.assertEqual(rig.bad(), [])
        # and the next try, once the back-off is over, works
        self.assertTick(rig.tick(now=NOW + DAY), 0)
        self.assertEqual(rig.on_tag(), "v0.2.0")

    def test_a_mixed_tree_with_a_change_of_the_users_is_left_alone(self) -> None:
        rig = self.rig
        sha = rig.release("0.2.0")
        git(rig.clone, "fetch", "-q", "--tags")
        (rig.clone / "mine.txt").write_text("mine\n")
        git(rig.clone, "add", "mine.txt")
        rig.write_state(
            good_tag="v0.1.0",
            good_sha=rig.head(),
            trying_tag="v0.2.0",
            trying_sha=sha,
            attempts="1",
        )
        (rig.clone / "mine.txt").write_text("edited\n")
        before = rig.head()
        self.assertTick(rig.tick(), 0)
        self.assertEqual(rig.head(), before)
        self.assertEqual((rig.clone / "mine.txt").read_text(), "edited\n")
        self.assertEqual(rig.state()["last_result"], "dirty")

    def test_too_little_free_space_is_an_environment_failure_before_any_checkout(self) -> None:
        rig = self.rig
        rig.release("0.2.0")
        self.assertTick(rig.tick(WZ_MIN_FREE_KB="999999999999"), 1)
        self.assertEqual(rig.on_tag(), "v0.1.0")
        state = rig.state()
        self.assertEqual((state["last_result"], state["trying_tag"]), ("env", ""))
        self.assertEqual(state["retry_after"], str(NOW + DAY))
        self.assertEqual(rig.bad(), [])
        self.assertEqual([c for c in rig.calls() if "@ 0.2.0" in c], [])

    def test_an_ignored_file_in_the_way_is_never_overwritten(self) -> None:
        rig = self.rig
        rig.release("0.2.0", extra={"cache.txt": "theirs\n"})
        info = rig.clone / ".git" / "info"
        info.mkdir(exist_ok=True)
        (info / "exclude").write_text("cache.txt\n")
        (rig.clone / "cache.txt").write_text("mine\n")
        self.assertTick(rig.tick(), 1)
        self.assertEqual((rig.clone / "cache.txt").read_text(), "mine\n")
        self.assertEqual(rig.on_tag(), "v0.1.0")
        self.assertEqual(rig.bad(), [])
        self.assertEqual(rig.state()["last_result"], "env")


class DeferralClockTests(UpdaterCase):
    def test_a_menu_that_closes_and_opens_again_starts_a_new_deferral(self) -> None:
        rig = self.rig
        rig.proc.add(7000001, "es-de")
        self.assertTick(rig.tick(), 0)
        self.assertEqual(rig.state()["deferred_since"], str(NOW))
        shutil.rmtree(rig.proc.path / "7000001")
        self.assertTick(rig.tick(now=NOW + HOUR), 0)  # closed, nothing to update
        self.assertEqual(rig.state()["deferred_since"], "")
        rig.release("0.2.0")
        rig.proc.add(7000002, "es-de")  # days later somebody is playing
        later = NOW + 4 * DAY
        self.assertTick(rig.tick(now=later), 0)
        self.assertEqual(rig.on_tag(), "v0.1.0", "no update while the user plays")
        self.assertEqual(rig.state()["deferred_since"], str(later))

    def test_an_environment_failure_ends_the_deferral_with_the_menu_still_open(self) -> None:
        # The menu has been open for 80 hours, so the tick goes on; the lines that clear
        # `deferred_since` when the menu is seen closed cannot be what clears it here.
        rig = self.rig
        rig.release("0.2.0")
        rig.flag("env_broken")
        rig.proc.add(7000001, "es-de")
        rig.write_state(pending="1", deferred_since=str(NOW - 80 * HOUR))
        self.assertTick(rig.tick(), 1)
        self.assertEqual(rig.state()["last_result"], "env")
        self.assertEqual(rig.state()["deferred_since"], "")

    def test_a_failed_reconcile_ends_the_deferral_with_the_menu_still_open(self) -> None:
        rig = self.rig
        rig.flag("env_broken")
        rig.proc.add(7000001, "es-de")
        rig.write_state(pending="1", deferred_since=str(NOW - 80 * HOUR))
        self.assertTick(rig.tick(), 0)
        self.assertEqual(len(rig.provisions()), 1)
        self.assertEqual(rig.state()["deferred_since"], "")
        self.assertEqual(rig.state()["pending"], "0")


class ProcessGroupTests(UpdaterCase):
    def setUp(self) -> None:
        super().setUp()
        if os.environ.get("WZ_TEST_SHELL") == "dash" and not shutil.which("setsid"):
            self.skipTest("dash has no job control and this system has no setsid")

    def gone(self, pid: int) -> bool:
        for _ in range(40):
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                return True
            time.sleep(0.1)
        return False

    def test_a_step_that_is_cut_off_takes_its_grandchildren_with_it(self) -> None:
        for behaviour in ("hang_tree_version", "hang_tree"):
            with self.subTest(behaviour=behaviour):
                rig = Rig(self)
                rig.release("0.2.0", behaviour)
                # a release under suspicion gets its second look at once: one cut, not three
                git(rig.clone, "fetch", "-q", "--tags")
                rig.write_state(
                    good_tag="v0.1.0",
                    good_sha=rig.head(),
                    suspect_tag="v0.2.0",
                    suspect_sha=git(rig.work, "rev-parse", "v0.2.0^{commit}"),
                )
                started = time.monotonic()
                self.assertTick(rig.tick(WZ_STEP_TIMEOUT="1"), 1)
                self.assertLess(time.monotonic() - started, 30, "a grandchild held the pipe")
                pid = int((rig.fake_dir / "grandchild.pid").read_text())
                self.assertTrue(self.gone(pid), "the grandchild survived the cut")
                self.assertEqual(rig.bad(), ["v0.2.0"])

    def test_a_tick_that_is_terminated_takes_its_step_and_the_grandchildren_down(self) -> None:
        rig = self.rig
        rig.release("0.2.0", "hang_tree")
        shell = os.environ.get("WZ_TEST_SHELL", "sh")
        proc = subprocess.Popen(
            [shell, str(rig.updater), "tick"],
            env=rig.env(WZ_STEP_TIMEOUT="30"),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        self.addCleanup(proc.wait)
        self.addCleanup(proc.kill)
        marker = rig.fake_dir / "grandchild.pid"
        for _ in range(200):
            if marker.exists() and marker.read_text().strip():
                break
            time.sleep(0.1)
        pid = int(marker.read_text())
        proc.terminate()
        proc.wait(timeout=30)
        self.assertTrue(self.gone(pid), "the step's grandchild survived the tick")

    def test_the_fetch_cannot_be_held_by_a_grandchild_either(self) -> None:
        rig = self.rig
        hook = rig.root / "slow-ssh.sh"
        hook.write_text("sleep 60 &\nexec sleep 60\n")
        git(rig.clone, "config", "core.sshCommand", f"sh {hook}")
        git(rig.clone, "remote", "set-url", "origin", "ssh://nobody@example.invalid/repo.git")
        started = time.monotonic()
        self.assertTick(rig.tick(WZ_FETCH_TIMEOUT="1"), 0)
        self.assertLess(time.monotonic() - started, 30)


class BudgetTests(UpdaterCase):
    def test_the_tick_budget_cuts_a_step_and_counts_as_an_environment_failure(self) -> None:
        rig = self.rig
        rig.release("0.2.0", "hang")
        started = time.monotonic()
        self.assertTick(rig.tick(WZ_TICK_BUDGET="3", WZ_STEP_TIMEOUT="30"), 1)
        self.assertLess(time.monotonic() - started, 20)
        state = rig.state()
        self.assertEqual(rig.bad(), [], "running out of time says nothing about the release")
        self.assertEqual(rig.on_tag(), "v0.1.0")
        self.assertEqual((state["last_result"], state["env_failures"]), ("env", "1"))
        self.assertEqual(state["retry_after"], str(NOW + DAY))
        self.assertEqual(state["trying_tag"], "")

    def test_numbers_in_the_environment_that_are_not_numbers_do_not_kill_the_script(self) -> None:
        rig = self.rig
        rig.release("0.2.0")
        git(rig.clone, "fetch", "-q", "--tags")
        rig.write_state(last_fetch=str(NOW - 5))
        proc = rig.tick(
            WZ_STEP_TIMEOUT="abc",
            WZ_FETCH_INTERVAL="x",
            WZ_MIN_FREE_KB="y",
            WZ_TICK_BUDGET="z",
            WZ_FETCH_TIMEOUT="w",
        )
        self.assertTick(proc, 0)
        self.assertEqual(rig.on_tag(), "v0.2.0")


class VerificationTests(UpdaterCase):
    def test_a_trying_tag_that_became_bad_is_dropped_on_resume(self) -> None:
        rig = self.rig
        rig.release("0.2.0")
        rig.flag("defer")
        self.assertTick(rig.tick(), 0)
        self.assertEqual(rig.state()["trying_tag"], "v0.2.0")
        (rig.state_dir / "bad").mkdir()
        (rig.state_dir / "bad" / "v0.2.0").write_text("sha=x\ndate=x\nreason=test\n")
        rig.flag("defer", False)
        calls = len(rig.calls())
        self.assertTick(rig.tick(now=NOW + 900), 0)
        self.assertEqual(rig.on_tag(), "v0.1.0")
        self.assertEqual(rig.state()["trying_tag"], "")
        self.assertEqual([c for c in rig.calls()[calls:] if "@ 0.2.0" in c], [])

    def test_a_failure_that_does_not_repeat_does_not_burn_the_tag(self) -> None:
        rig = self.rig
        rig.release("0.2.0", "flaky")
        self.assertTick(rig.tick(), 0)
        self.assertEqual(rig.on_tag(), "v0.2.0")
        self.assertEqual(rig.bad(), [])
        self.assertEqual(rig.state()["good_tag"], "v0.2.0")

    def test_a_failure_that_repeats_is_bad_after_one_more_try(self) -> None:
        rig = self.rig
        rig.release("0.2.0", "provision_fails")
        self.assertTick(rig.tick(), 1)
        self.assertEqual(rig.bad(), ["v0.2.0"])
        provisions = [c for c in rig.calls() if "provision device" in c and "@ 0.2.0" in c]
        self.assertEqual(len(provisions), 2, "the release was tried twice")

    def test_a_release_that_fails_after_the_boot_only_once_stays(self) -> None:
        rig = self.rig
        rig.release("0.2.0")
        self.assertTick(rig.tick(), 0)
        rig.break_version("0.2.0", "flaky")
        rig.boot_id.write_text("boot-2\n")
        self.assertTick(rig.tick(now=NOW + 1800), 0)
        self.assertEqual(rig.bad(), [])
        self.assertEqual(rig.on_tag(), "v0.2.0")
        self.assertEqual(rig.state()["probation"], "0")

    def test_a_python_that_imports_warpzone_from_elsewhere_is_the_environment(self) -> None:
        rig = self.rig
        rig.release("0.2.0")
        rig.flag("wrong_install")
        self.assertTick(rig.tick(), 1)
        self.assertEqual(rig.bad(), [])
        self.assertEqual(rig.on_tag(), "v0.1.0")
        self.assertEqual(rig.state()["last_result"], "env")
        self.assertIn(str(rig.clone), "".join(c for c in rig.calls() if "warpzone.cli" in c))

    def test_the_import_check_compares_the_location_with_the_clone(self) -> None:
        code = re.search(r"^IMPORT_CODE='(.*)'$", UPDATER.read_text(), re.M)
        assert code is not None
        root = Path(tempfile.mkdtemp(prefix="wz-import-"))
        self.addCleanup(shutil.rmtree, root, True)
        for name in ("__init__", "cli", "provision", "health"):
            (root / "warpzone").mkdir(exist_ok=True)
            (root / "warpzone" / f"{name}.py").write_text("")
        elsewhere = root / "elsewhere"
        elsewhere.mkdir()
        env = {**os.environ, "PYTHONPATH": str(root)}
        for clone, expected in ((root, 0), (elsewhere, 7)):
            proc = subprocess.run(
                [sys.executable, "-c", code.group(1), str(clone)],
                env=env,
                cwd=root,
                capture_output=True,
                check=False,
            )
            self.assertEqual(proc.returncode, expected, proc.stderr)


class StateHardeningTests(UpdaterCase):
    def test_tags_and_branches_in_the_state_are_validated(self) -> None:
        rig = self.rig
        rig.write_state(
            good_tag="not-a-tag",
            trying_tag="../../escape",
            trying_sha=rig.head(),
            attempts="1",
            good_branch="-x",
        )
        self.assertTick(rig.tick(), 0)
        self.assertEqual(rig.state()["trying_tag"], "")
        self.assertEqual(rig.state()["trying_sha"], "", "the sha goes with its tag")
        self.assertEqual(rig.state()["attempts"], "0")
        self.assertEqual(rig.state()["last_result"], "ok")
        self.assertFalse((rig.state_dir.parent / "escape").exists())
        self.assertEqual(rig.bad(), [])

    def test_times_from_the_future_are_dropped(self) -> None:
        rig = self.rig
        rig.release("0.2.0")
        rig.write_state(
            last_fetch=str(NOW + 10 * DAY),
            retry_after=str(NOW + 40 * DAY),
            deferred_since=str(NOW + 5 * DAY),
        )
        self.assertTick(rig.tick(WZ_FETCH_INTERVAL=str(DAY)), 0)
        self.assertEqual(rig.on_tag(), "v0.2.0")
        self.assertEqual(rig.state()["retry_after"], "")

    def test_a_moved_tag_upstream_does_not_make_every_tick_fetch_again(self) -> None:
        rig = self.rig
        rig.release("0.2.0")
        git(rig.work, "tag", "-f", "v0.1.0")  # the maintainer moved a published tag
        git(rig.work, "push", "-q", "-f", "origin", "refs/tags/v0.1.0")
        proc = rig.tick()
        self.assertTick(proc, 0)
        self.assertEqual(rig.state()["last_fetch"], str(NOW))
        self.assertIn("moved", proc.output)

    def test_a_frontend_name_is_never_expanded_as_a_pattern(self) -> None:
        rig = self.rig
        rig.release("0.2.0")
        rig.write_conf(frontend="*")
        rig.proc.add(7000001, "behaviour")  # a file name in the clone
        self.assertTick(rig.tick(), 0)
        self.assertEqual(rig.on_tag(), "v0.2.0")


class LockTests(UpdaterCase):
    def holder(self) -> subprocess.Popen:
        proc = subprocess.Popen(["sleep", "30"])
        self.addCleanup(proc.wait)
        self.addCleanup(proc.kill)
        return proc

    def test_a_lock_from_another_boot_is_stale_even_if_its_pid_is_alive(self) -> None:
        rig = self.rig
        rig.release("0.2.0")
        holder = self.holder()  # the pid was reused after a restart
        (rig.state_dir / "lock").mkdir()
        (rig.state_dir / "lock" / "pid").write_text(f"{holder.pid}\n")
        (rig.state_dir / "lock" / "boot").write_text("boot-0\n")
        self.assertTick(rig.tick(), 0)
        self.assertEqual(rig.on_tag(), "v0.2.0")

    def test_a_live_lock_of_this_boot_is_respected(self) -> None:
        rig = self.rig
        rig.release("0.2.0")
        holder = self.holder()
        (rig.state_dir / "lock").mkdir()
        (rig.state_dir / "lock" / "pid").write_text(f"{holder.pid}\n")
        (rig.state_dir / "lock" / "boot").write_text("boot-1\n")
        self.assertTick(rig.tick(), 0)
        self.assertEqual(rig.on_tag(), "v0.1.0")

    def test_taking_over_a_stale_lock_leaves_nothing_behind(self) -> None:
        rig = self.rig
        (rig.state_dir / "lock").mkdir()
        (rig.state_dir / "lock" / "pid").write_text("2999999\n")
        self.assertTick(rig.tick(), 0)
        leftovers = [p.name for p in rig.state_dir.iterdir() if p.name.startswith("lock")]
        self.assertEqual(leftovers, [])


class UsersEditsTests(UpdaterCase):
    """A file the update changes is not the updater's just because of its path."""

    EDIT = "# my own edit\n"

    def test_an_edit_in_a_file_the_release_changes_survives_a_deferred_update(self) -> None:
        rig = self.rig
        rig.release("0.2.0")
        rig.flag("defer")  # the check is deferred for hours, `trying` stays set
        self.assertTick(rig.tick(), 0)
        init = rig.clone / "warpzone" / "__init__.py"
        init.write_text(self.EDIT)
        rig.flag("defer", False)
        self.assertTick(rig.tick(now=NOW + HOUR), 0)
        self.assertEqual(init.read_text(), self.EDIT)
        self.assertEqual(rig.state()["last_result"], "dirty")
        self.assertEqual(rig.bad(), [])

    def test_an_edit_during_probation_survives_the_next_boot(self) -> None:
        rig = self.rig
        rig.release("0.2.0")
        self.assertTick(rig.tick(), 0)
        self.assertEqual(rig.state()["probation"], "1")
        init = rig.clone / "warpzone" / "__init__.py"
        init.write_text(self.EDIT)
        rig.boot_id.write_text("boot-2\n")
        self.assertTick(rig.tick(now=NOW + DAY), 0)
        self.assertEqual(init.read_text(), self.EDIT)
        self.assertEqual(rig.state()["last_result"], "dirty")

    def test_a_deleted_file_of_the_update_is_the_updaters_to_restore(self) -> None:
        rig = self.rig
        rig.release("0.2.0", extra={"zz.txt": "later\n"})
        rig.cut_checkouts("kill")
        self.assertNotEqual(rig.tick().returncode, 0)
        (rig.clone / "warpzone" / "__init__.py").write_text("")  # cut before the file was written
        rig.age_index_lock()
        self.assertTick(rig.tick(now=NOW + 900), 0)
        self.assertEqual(rig.on_tag(), "v0.2.0")


class SuspectTests(UpdaterCase):
    def suspect(self, rig, version: str = "0.2.0") -> None:
        """The state of a device whose `version` failed once and has to be looked at again."""
        git(rig.clone, "fetch", "-q", "--tags")
        rig.write_state(
            good_tag="v0.1.0",
            good_sha=rig.head(),
            suspect_tag=f"v{version}",
            suspect_sha=git(rig.work, "rev-parse", f"v{version}^{{commit}}"),
        )

    def test_a_release_that_hangs_is_looked_at_again_with_a_whole_budget(self) -> None:
        # The budget is counted on a fake uptime that only the fake python moves (`spend`
        # takes 99 of the 100 seconds), so nothing here depends on how fast the machine is.
        rig = self.rig
        (rig.proc.path / "uptime").write_text("1000.00 0\n")
        rig.release("0.2.0", "hang spend")
        budget = {"WZ_STEP_TIMEOUT": "2", "WZ_TICK_BUDGET": "100"}
        # The first look fails at its own limit, the previous version passes (one second is
        # left, and it is enough), and the second look is cut by that second: the release
        # stays under suspicion.
        self.assertTick(rig.tick(**budget), 1)
        state = rig.state()
        self.assertEqual(rig.on_tag(), "v0.1.0")
        self.assertEqual(rig.bad(), [])
        self.assertEqual(state.get("suspect_tag"), "v0.2.0")
        self.assertEqual(state["env_failures"], "0", "this is not the environment's fault")
        self.assertEqual(state["retry_after"], "")
        # The next tick looks at it first thing, with the whole budget, and it fails again.
        self.assertTick(rig.tick(now=NOW + 900, **budget), 1)
        self.assertEqual(rig.bad(), ["v0.2.0"])
        self.assertEqual(rig.on_tag(), "v0.1.0")
        self.assertEqual(rig.state().get("suspect_tag"), "")

    def test_a_second_look_that_fails_in_a_broken_environment_burns_nothing(self) -> None:
        rig = self.rig
        rig.release("0.2.0")  # the release is fine; it was the environment that failed it
        self.suspect(rig)
        rig.flag("env_broken")
        self.assertTick(rig.tick(), 1)
        state = rig.state()
        self.assertEqual(rig.bad(), [])
        self.assertEqual(rig.on_tag(), "v0.1.0")
        self.assertEqual((state["last_result"], state["env_failures"]), ("env", "1"))
        self.assertEqual(state.get("suspect_tag"), "", "the back-off decides when to try again")

    def test_a_second_look_that_cannot_check_the_previous_version_stays_suspect(self) -> None:
        rig = self.rig
        (rig.proc.path / "uptime").write_text("1000.00 0\n")
        rig.release("0.2.0", "provision_fails spend")
        rig.break_version("0.1.0", "hang")  # the check of the previous version never ends
        self.suspect(rig)
        self.assertTick(rig.tick(WZ_STEP_TIMEOUT="2", WZ_TICK_BUDGET="100"), 1)
        self.assertEqual(rig.bad(), [], "nothing proves the release is bad")
        self.assertEqual(rig.on_tag(), "v0.1.0")
        self.assertEqual(rig.state().get("suspect_tag"), "v0.2.0")
        self.assertEqual(rig.state()["env_failures"], "0")

    def test_a_suspect_older_than_what_is_checked_out_is_dropped(self) -> None:
        rig = self.rig
        rig.release("0.1.5")
        rig.release("0.2.0")
        self.suspect(rig, "0.1.5")
        git(rig.clone, "checkout", "-q", "--detach", "v0.2.0")
        rig.write_state(
            good_tag="v0.2.0",
            good_sha=rig.head(),
            suspect_tag="v0.1.5",
            suspect_sha=git(rig.work, "rev-parse", "v0.1.5^{commit}"),
        )
        self.assertTick(rig.tick(), 0)
        self.assertEqual(rig.on_tag(), "v0.2.0", "never down to a suspect")
        self.assertEqual(rig.state().get("suspect_tag"), "")
        self.assertEqual(rig.bad(), [])

    def test_a_suspect_is_dropped_when_somebody_moves_the_checkout(self) -> None:
        rig = self.rig
        rig.release("0.2.0")
        rig.release("0.3.0")
        self.suspect(rig, "0.2.0")  # good is still v0.1.0 in the state
        git(rig.clone, "checkout", "-q", "--detach", "v0.3.0")
        self.assertTick(rig.tick(), 0)
        self.assertEqual(rig.on_tag(), "v0.3.0")
        self.assertEqual(rig.state().get("suspect_tag"), "")
        self.assertEqual(rig.bad(), [])

    def test_a_suspect_that_passes_the_second_look_is_accepted(self) -> None:
        rig = self.rig
        rig.release("0.2.0")
        rig.write_state(
            good_tag="v0.1.0",
            good_sha=rig.head(),
            suspect_tag="v0.2.0",
            suspect_sha=git(rig.work, "rev-parse", "v0.2.0^{commit}"),
        )
        git(rig.clone, "fetch", "-q", "--tags")
        self.assertTick(rig.tick(), 0)
        self.assertEqual(rig.on_tag(), "v0.2.0")
        self.assertEqual(rig.bad(), [])
        self.assertEqual(rig.state().get("suspect_tag"), "")

    def test_a_probation_that_cannot_look_again_leaves_a_consistent_state(self) -> None:
        rig = self.rig
        rig.release("0.2.0", extra={"zz.txt": "later\n"})
        self.assertTick(rig.tick(), 0)
        rig.break_version("0.2.0", "provision_fails")  # it breaks after the reboot
        rig.cut_checkouts("fail")  # and the checkout back onto it fails
        rig.boot_id.write_text("boot-2\n")
        self.assertTick(rig.tick(now=NOW + DAY), 1)
        state = rig.state()
        self.assertEqual(rig.on_tag(), "v0.1.0")
        self.assertEqual(git(rig.clone, "status", "--porcelain", "--untracked-files=no"), "")
        self.assertEqual((state["good_tag"], state["probation"]), ("v0.1.0", "0"))
        self.assertEqual(state.get("suspect_tag"), "v0.2.0")
        self.assertEqual(state["last_result"], "rolled-back")
        self.assertEqual(rig.bad(), [])
        # the next tick is not fooled into thinking somebody moved the checkout
        proc = rig.tick(now=NOW + DAY + 900)
        self.assertNotIn("moved outside", proc.output)
        self.assertEqual(rig.bad(), ["v0.2.0"])
        self.assertEqual(rig.on_tag(), "v0.1.0")


class ClockTests(UpdaterCase):
    def test_the_budget_runs_on_the_uptime_not_on_the_wall_clock(self) -> None:
        rig = self.rig
        rig.release("0.2.0", "hang")
        (rig.proc.path / "uptime").write_text("100.00 400.00\n")  # it does not advance
        git(rig.clone, "fetch", "-q", "--tags")
        rig.write_state(
            good_tag="v0.1.0",
            good_sha=rig.head(),
            suspect_tag="v0.2.0",
            suspect_sha=git(rig.work, "rev-parse", "v0.2.0^{commit}"),
        )
        # A budget of one second is the step's whole limit. The second look and the check of
        # the previous version take longer than that on the wall clock, which would have
        # cut them; the uptime did not move, so the hang is the release's own fault.
        self.assertTick(rig.tick(WZ_TICK_BUDGET="1", WZ_STEP_TIMEOUT="1"), 1)
        self.assertEqual(rig.bad(), ["v0.2.0"])

    def test_a_budget_that_is_already_spent_is_not_reported_as_a_missing_python(self) -> None:
        rig = self.rig
        rig.release("0.2.0")
        self.assertTick(rig.tick(WZ_TICK_BUDGET="0"), 1)
        self.assertEqual(rig.state()["last_result"], "env")
        self.assertEqual(rig.on_tag(), "v0.1.0")


class OrderTests(UpdaterCase):
    TAGS: ClassVar[list[str]] = [
        "v0.9.0", "v0.10.0", "v0.10.0rc1", "v0.10.0rc2", "v0.10.0rc10", "v1.0.0", "v0.2.0",
        "v0.2.0rc3", "v10.0.0rc1", "v0.3.0-rc1", "v0.3.0rc", "v1.2", "vx.y.z", "0.4.0",
        "v0.4.0.1", "v0.4.0rc1rc2", "v0.5.0beta1", "v0.6.0rc1.dev1",
    ]  # fmt: skip

    def valid_best_first(self) -> list[str]:
        return sorted((t for t in self.TAGS if tag_key(t) is not None), key=tag_key, reverse=True)

    def test_the_order_is_the_one_of_versions_tag_key(self) -> None:
        for shell in SHELLS:
            with self.subTest(shell=shell):
                proc = subprocess.run(
                    [shell, str(UPDATER), "order"],
                    input="\n".join(self.TAGS) + "\n",
                    capture_output=True,
                    text=True,
                    check=False,
                )
                self.assertEqual(proc.returncode, 0, proc.stderr)
                self.assertEqual(proc.stdout.split(), self.valid_best_first())

    def test_the_order_agrees_with_gits_version_sort(self) -> None:
        repo = self.rig.root / "ordering"
        subprocess.run(["git", "init", "-q", str(repo)], check=True, env={**os.environ, **GIT_ENV})
        git(repo, "commit", "-q", "--allow-empty", "-m", "x")
        for tag in self.TAGS:
            git(repo, "tag", tag)
        listed = git(
            repo,
            *("-c", "versionsort.suffix=rc", "for-each-ref", "--sort=-v:refname"),
            *("--format=%(refname:strip=2)", "refs/tags"),
        ).split()
        self.assertEqual([t for t in listed if tag_key(t) is not None], self.valid_best_first())


class ChannelTests(UpdaterCase):
    def test_stable_follows_releases_and_rc_follows_candidates_too(self) -> None:
        rig = self.rig
        rig.release("0.2.0rc1")
        rig.release("0.2.0rc2")
        self.assertTick(rig.tick(), 0)
        self.assertEqual(rig.on_tag(), "v0.1.0", "stable ignores candidates")
        rig.write_conf(channel="rc")
        self.assertTick(rig.tick(), 0)
        self.assertEqual(rig.on_tag(), "v0.2.0rc2")
        self.assertEqual(rig.state()["good_tag"], "v0.2.0rc2")
        self.assertIn("-m warpzone health deck @ 0.2.0rc2", rig.calls())
        rig.release("0.2.0")  # a release is after its candidates
        self.assertTick(rig.tick(), 0)
        self.assertEqual(rig.on_tag(), "v0.2.0")
        rig.release("0.3.0rc1")
        rig.write_conf()  # stable again
        self.assertTick(rig.tick(), 0)
        self.assertEqual(rig.on_tag(), "v0.2.0", "a candidate is not a release")
        rig.write_conf(channel="rc")
        self.assertTick(rig.tick(), 0)
        self.assertEqual(rig.on_tag(), "v0.3.0rc1")
        rig.release("0.2.1")  # lower than the candidate it runs: no way down
        self.assertTick(rig.tick(), 0)
        self.assertEqual(rig.on_tag(), "v0.3.0rc1")
        rig.release("0.3.0")
        rig.write_conf()  # a device on a candidate goes to the release on stable
        self.assertTick(rig.tick(), 0)
        self.assertEqual(rig.on_tag(), "v0.3.0")
        self.assertEqual(rig.bad(), [])

    def test_a_candidate_has_to_be_valid_like_any_tag(self) -> None:
        rig = self.rig
        rig.release("0.2.0rc1")
        rig.release("0.2.0rc2", file_version="0.2.0rc9")  # the version disagrees
        rig.release("0.2.0rc3", on_main=False)  # not on main
        rig.release("0.2.0rc4")  # marked bad
        (rig.state_dir / "bad").mkdir()
        (rig.state_dir / "bad" / "v0.2.0rc4").write_text("sha=0\n")
        rig.write_conf(channel="rc")
        self.assertTick(rig.tick(), 0)
        self.assertEqual(rig.on_tag(), "v0.2.0rc1")

    def test_an_unknown_channel_is_stable_and_a_pin_that_is_no_tag_is_ignored(self) -> None:
        rig = self.rig
        rig.release("0.2.0rc1")
        rig.release("0.3.0")
        rig.write_conf(channel="nightly", pin="v0.2.0-rc1")
        proc = rig.tick()
        self.assertTick(proc, 0)
        self.assertEqual(rig.on_tag(), "v0.3.0", "stable, and no pin")
        self.assertIn("channel", proc.output)
        self.assertIn("pin", proc.output)

    def test_a_candidate_that_fails_is_marked_bad_like_a_release(self) -> None:
        rig = self.rig
        rig.release("0.2.0rc1", "provision_fails")
        rig.write_conf(channel="rc")
        self.assertTick(rig.tick(), 1)
        self.assertEqual(rig.bad(), ["v0.2.0rc1"])
        self.assertEqual(rig.on_tag(), "v0.1.0")


class RetiredTests(UpdaterCase):
    def retired_ticks(self, rig, *, now: int = NOW) -> subprocess.CompletedProcess:
        proc = rig.tick(now=now)
        self.assertTick(proc, 0)
        return proc

    def test_a_retired_tag_is_never_a_candidate(self) -> None:
        rig = self.rig
        rig.release("0.2.0")
        rig.release("0.3.0")
        rig.retire("v0.3.0", "broke the settings merge")
        self.retired_ticks(rig)
        self.assertEqual(rig.on_tag(), "v0.2.0")
        self.assertEqual(rig.bad(), [], "retired is not bad")

    def test_a_retired_version_goes_to_the_best_valid_one_even_a_lower_one(self) -> None:
        rig = self.rig
        rig.release("0.2.0")
        rig.release("0.3.0")
        rig.move_to("v0.3.0")
        self.retired_ticks(rig)
        self.assertEqual(rig.state()["good_tag"], "v0.3.0")
        rig.retire("v0.3.0", "data loss")
        proc = self.retired_ticks(rig)
        self.assertEqual(rig.on_tag(), "v0.2.0")
        self.assertEqual(rig.state()["good_tag"], "v0.2.0")
        self.assertIn("retired", proc.output)
        self.assertIn("data loss", proc.output)
        self.retired_ticks(rig)
        self.assertEqual(rig.on_tag(), "v0.2.0", "it does not bounce")
        rig.retire("v0.2.0", "b")
        self.retired_ticks(rig)
        self.assertEqual(rig.on_tag(), "v0.1.0")
        rig.retire("v0.1.0", "c")
        calls = len(rig.calls())
        self.retired_ticks(rig)
        self.assertEqual(rig.on_tag(), "v0.1.0", "nothing valid is left: it stays")
        self.assertEqual(len(rig.calls()), calls)
        self.assertEqual(rig.bad(), [])

    def test_tags_that_are_no_retirement_do_not_use_up_the_limit(self) -> None:
        rig = self.rig
        rig.release("0.2.0")
        rig.release("0.3.0")
        rig.move_to("v0.3.0")
        for n in range(1, 5):  # lightweight: they sort first and are no retirement at all
            git(rig.work, "update-ref", f"refs/tags/retired/v1.0.{n}", "HEAD")
        git(rig.work, "push", "-q", "origin", "refs/tags/retired/*:refs/tags/retired/*")
        rig.retire("v0.3.0", "the real one, after four that are not")
        proc = rig.tick()
        self.assertTick(proc, 0)
        self.assertEqual(rig.on_tag(), "v0.2.0")

    def test_retirements_of_newer_releases_do_not_hide_that_of_the_running_version(self) -> None:
        rig = self.rig
        rig.release("0.2.0")
        rig.release("0.3.0")
        rig.move_to("v0.3.0")
        for n in range(1, 4):  # real releases, all retired, all sorting above it
            rig.release(f"1.0.{n}")
            rig.retire(f"v1.0.{n}", "x")
        rig.retire("v0.3.0", "the fourth by version")
        proc = rig.tick()
        self.assertTick(proc, 0)
        self.assertEqual(rig.on_tag(), "v0.2.0", proc.output)

    def test_only_an_annotated_tag_with_a_version_name_retires_and_nothing_runs(self) -> None:
        rig = self.rig
        marker = rig.root / "pwned"
        # the old RETIRED file is not read any more, even on main
        rig.release("0.2.0", extra={"RETIRED": "v0.2.0 2026-11-03 old way\n"})
        rig.release("0.3.0")
        rig.retire("v0.3.0", f"$(touch {marker}) `touch {marker}`", annotated=False)
        rig.retire("v0.2.0;touch", "a name that is no version")
        rig.retire("latest", "neither")
        rig.retire("v0.2.0-rc1", "nor the wrong spelling")
        proc = self.retired_ticks(rig)
        self.assertFalse(marker.exists())
        self.assertEqual(rig.on_tag(), "v0.3.0", "a lightweight tag has no reason: no retirement")
        self.assertRegex(proc.output, r"retire.*ignor")
        rig.retire("v0.2.0", f"$(touch {marker})")  # a reason is text
        rig.move_to("v0.2.0")
        self.retired_ticks(rig)
        self.assertFalse(marker.exists())
        self.assertEqual(rig.on_tag(), "v0.3.0", "the retired version moves up")


class PinTests(UpdaterCase):
    def test_the_pin_is_the_target_even_below_the_running_version_or_retired(self) -> None:
        rig = self.rig
        rig.release("0.2.0")
        rig.release("0.3.0")
        rig.move_to("v0.3.0")
        rig.write_conf(pin="v0.2.0")
        self.assertTick(rig.tick(), 0)
        self.assertEqual(rig.on_tag(), "v0.2.0")
        self.assertEqual(rig.state()["good_tag"], "v0.2.0")
        self.assertEqual(rig.state()["pin_retired"], "")
        calls = len(rig.calls())
        self.assertTick(rig.tick(), 0)
        self.assertEqual(rig.on_tag(), "v0.2.0", "v0.3.0 exists and the pin holds")
        self.assertEqual(len(rig.calls()), calls)
        rig.retire("v0.2.0", "bad one")
        rig.retire("v0.1.0", "old")
        proc = rig.tick()
        self.assertTick(proc, 0)
        self.assertEqual(rig.on_tag(), "v0.2.0", "the pin wins over a retirement")
        self.assertEqual(rig.state()["pin_retired"], "v0.2.0")
        self.assertIn("pin", proc.output)
        self.assertIn("retired", proc.output)
        rig.write_conf(pin="v0.1.0")  # retired too, and lower
        self.assertTick(rig.tick(), 0)
        self.assertEqual(rig.on_tag(), "v0.1.0")
        self.assertEqual(rig.state()["pin_retired"], "v0.1.0")
        rig.write_conf()  # no pin: the device goes up to the best valid release
        self.assertTick(rig.tick(), 0)
        self.assertEqual(rig.on_tag(), "v0.3.0")
        self.assertEqual(rig.state()["pin_retired"], "")

    def test_a_pin_follows_no_channel_and_must_be_a_valid_tag(self) -> None:
        rig = self.rig
        rig.release("0.2.0rc1")
        rig.release("0.3.0", on_main=False)
        rig.release("0.4.0", file_version="0.4.1")
        self.assertTick(rig.tick(), 0)  # the first run reconciles once; after it nothing runs
        calls = len(rig.calls())
        for pin in ("v9.9.9", "v0.3.0", "v0.4.0"):
            with self.subTest(pin=pin):
                rig.write_conf(pin=pin)
                self.assertTick(rig.tick(), 0)
                self.assertEqual(rig.on_tag(), "v0.1.0")
                self.assertEqual(len(rig.calls()), calls)
        rig.write_conf(pin="v0.2.0rc1")  # stable channel, a candidate
        self.assertTick(rig.tick(), 0)
        self.assertEqual(rig.on_tag(), "v0.2.0rc1")

    def test_a_pin_that_fails_is_judged_by_the_differential_verdict(self) -> None:
        rig = self.rig
        rig.release("0.2.0")
        rig.write_conf(pin="v0.2.0")
        rig.flag("env_broken")  # the good version fails too: not the pin's fault
        self.assertTick(rig.tick(), 1)
        self.assertEqual(rig.bad(), [])
        self.assertEqual(rig.state()["last_result"], "env")
        rig.flag("env_broken", False)
        rig.break_version("0.2.0", "provision_fails")
        later = NOW + 2 * DAY
        self.assertTick(rig.tick(now=later), 1)
        self.assertEqual(rig.bad(), ["v0.2.0"])
        self.assertEqual(rig.on_tag(), "v0.1.0")
        self.assertTick(rig.tick(now=later + DAY), 0)
        tried = [c for c in rig.calls() if "@ 0.2.0" in c]
        self.assertTick(rig.tick(now=later + 2 * DAY), 0)
        self.assertEqual([c for c in rig.calls() if "@ 0.2.0" in c], tried)
        self.assertEqual(rig.on_tag(), "v0.1.0", "a pin that was judged bad is not tried again")


@unittest.skipUnless(SSH_KEYGEN, "ssh-keygen is not installed")
class SignatureTests(UpdaterCase):
    def setUp(self) -> None:
        super().setUp()
        self.key = SigningKey(self.rig.root / "keys", "maintainer")
        self.other = SigningKey(self.rig.root / "keys", "stranger")

    def test_only_tags_signed_by_an_allowed_key_are_candidates(self) -> None:
        rig = self.rig
        allowed = {"WZ_ALLOWED_SIGNERS": str(rig.allow(self.key))}
        rig.release("0.2.0", sign=self.key)
        rig.release("0.3.0", sign=self.other)  # signed, by a key nobody trusts
        rig.release("0.4.0")  # not signed at all
        proc = rig.tick(**allowed)
        self.assertTick(proc, 0)
        self.assertEqual(rig.on_tag(), "v0.2.0")
        for tag in ("v0.3.0", "v0.4.0"):
            self.assertRegex(proc.output, rf"{tag}.*signature")
        self.assertEqual(rig.bad(), [])
        rig.release("0.5.0rc1", sign=self.key)
        rig.write_conf(channel="rc")
        self.assertTick(rig.tick(**allowed), 0)
        self.assertEqual(rig.on_tag(), "v0.5.0rc1", "a candidate is signed the same way")

    def test_without_a_signers_file_nothing_is_demanded_and_it_says_so(self) -> None:
        rig = self.rig
        comments = rig.root / "comments"
        comments.write_text("# no keys yet\n\n   \n")
        cases = [
            ("0.2.0", str(rig.root / "missing")),
            ("0.3.0", str(comments)),
            ("0.4.0", ""),
        ]
        for version, path in cases:
            with self.subTest(path=path):
                rig.release(version)
                proc = rig.tick(WZ_ALLOWED_SIGNERS=path)
                self.assertTick(proc, 0)
                self.assertEqual(rig.on_tag(), f"v{version}")
                self.assertRegex(proc.output, r"signatures are not required")

    def trusted(self, rig, version: str = "0.2.0") -> None:
        """A device whose running version is signed and carries the allowed signers."""
        rig.release(
            version,
            sign=self.key,
            extra={"warpzone/data/allowed_signers": self.key.allowed},
        )
        rig.move_to(f"v{version}")

    def test_the_keys_are_the_ones_of_the_running_version_not_a_file_next_to_the_script(
        self,
    ) -> None:
        rig = self.rig
        self.trusted(rig)
        self.assertFalse((rig.updater.parent / "allowed_signers").exists())
        # a release that trusts its own signer, and one that nobody signed
        rig.release(
            "0.3.0",
            sign=self.other,
            extra={"warpzone/data/allowed_signers": self.other.allowed},
        )
        rig.release("0.4.0")
        proc = rig.tick()
        self.assertTick(proc, 0)
        self.assertEqual(rig.on_tag(), "v0.2.0")
        self.assertRegex(proc.output, r"v0\.3\.0.*signature")
        rig.write_conf(pin="v0.4.0")
        self.assertTick(rig.tick(), 0)
        self.assertEqual(rig.on_tag(), "v0.2.0", "an unsigned pin is no target")
        rig.write_conf()
        # a release signed by the trusted key can change the keys for the ones after it
        both = self.key.allowed + self.other.allowed
        rig.release("0.5.0", sign=self.key, extra={"warpzone/data/allowed_signers": both})
        self.assertTick(rig.tick(), 0)
        self.assertEqual(rig.on_tag(), "v0.5.0")
        rig.release("0.6.0", sign=self.other, extra={"warpzone/data/allowed_signers": both})
        self.assertTick(rig.tick(), 0)
        self.assertEqual(rig.on_tag(), "v0.6.0")

    def test_a_retirement_needs_the_same_signature_when_there_are_keys(self) -> None:
        rig = self.rig
        allowed = {"WZ_ALLOWED_SIGNERS": str(rig.allow(self.key))}
        rig.release("0.2.0", sign=self.key)
        rig.release("0.3.0", sign=self.key)
        rig.retire("v0.3.0", "withdrawn by a stranger", sign=self.other)
        rig.retire("v0.2.0", "not signed")
        self.assertTick(rig.tick(**allowed), 0)
        self.assertEqual(rig.on_tag(), "v0.3.0", "neither retirement counts")
        rig.release("0.4.0", sign=self.key)
        rig.retire("v0.4.0", "signed by the maintainer", sign=self.key)
        self.assertTick(rig.tick(**allowed), 0)
        self.assertEqual(rig.on_tag(), "v0.3.0", "this one does")

    def test_a_tag_signed_with_openpgp_is_not_accepted(self) -> None:
        rig = self.rig
        # a gpg that vouches for anything: `verify-tag` picks the verifier by the signature
        # block, not by gpg.format, so a PGP block would never reach allowed_signers
        gpg = rig.python.parent / "gpg"
        gpg.write_text(
            "#!/bin/sh\n"
            "echo '[GNUPG:] NEWSIG'\n"
            "echo '[GNUPG:] GOODSIG 0123456789ABCDEF Maintainer <m@example.invalid>'\n"
            "echo '[GNUPG:] VALIDSIG AAAA 2023-11-14 1700000000 0 4 0 1 10 00 AAAA'\n"
            "echo '[GNUPG:] TRUST_ULTIMATE 0 pgp'\n"
        )
        gpg.chmod(0o755)
        pgp = (
            "object {sha}\ntype commit\ntag {name}\n"
            "tagger T <t@example.invalid> 1700000000 +0000\n\nrelease\n"
            "-----BEGIN PGP SIGNATURE-----\n\niQEzBAABCAAdFiEE\n=abcd\n"
            "-----END PGP SIGNATURE-----\n"
        )
        allowed = {"WZ_ALLOWED_SIGNERS": str(rig.allow(self.key))}
        rig.release("0.2.0", sign=self.key)
        rig.release("0.3.0", raw_tag=pgp)
        self.assertTick(rig.tick(**allowed), 0)
        self.assertEqual(rig.on_tag(), "v0.2.0", "v0.3.0 is not signed by an allowed key")

    def test_a_version_being_tried_that_is_not_signed_any_more_is_not_promoted(self) -> None:
        rig = self.rig
        rig.release("0.2.0")
        git(rig.clone, "fetch", "-q", "--tags")
        rig.write_state(
            good_tag="v0.1.0",
            good_sha=rig.head(),
            trying_tag="v0.2.0",
            trying_sha=git(rig.work, "rev-parse", "v0.2.0^{commit}"),
            attempts="1",
        )
        rig.tick(WZ_ALLOWED_SIGNERS=str(rig.allow(self.key)))
        self.assertEqual(rig.on_tag(), "v0.1.0")
        self.assertEqual(rig.state()["trying_tag"], "")
        self.assertEqual([c for c in rig.calls() if "@ 0.2.0" in c], [])


class SignedNameTests(UpdaterCase):
    """A signature covers a tag object, and a ref can point at any object."""

    def setUp(self) -> None:
        super().setUp()
        if not SSH_KEYGEN:
            self.skipTest("ssh-keygen is not installed")
        self.key = SigningKey(self.rig.root / "keys", "maintainer")
        self.allowed = {"WZ_ALLOWED_SIGNERS": str(self.rig.allow(self.key))}

    def publish_as(self, ref: str, source: str) -> None:
        """Publish the tag object of `source` under another ref name."""
        rig = self.rig
        git(
            rig.work,
            "update-ref",
            f"refs/tags/{ref}",
            git(rig.work, "rev-parse", f"refs/tags/{source}"),
        )
        git(rig.work, "push", "-q", "origin", f"refs/tags/{ref}")

    def test_a_signed_retirement_published_under_another_version_retires_nothing(self) -> None:
        rig = self.rig
        rig.release("0.2.0", sign=self.key)
        rig.release("0.3.0", sign=self.key)
        rig.move_to("v0.3.0")
        rig.retire("v0.2.0", "really withdrawn", sign=self.key)
        self.publish_as("retired/v0.3.0", "retired/v0.2.0")  # its signature is good
        proc = rig.tick(**self.allowed)
        self.assertTick(proc, 0)
        self.assertEqual(rig.on_tag(), "v0.3.0")
        self.assertRegex(proc.output, r"retirement.*ignored.*v0\.3\.0")

    def test_retirements_of_unsigned_releases_do_not_use_up_the_limit(self) -> None:
        rig = self.rig
        rig.release("0.2.0", sign=self.key)
        rig.release("0.3.0", sign=self.key)
        rig.move_to("v0.3.0")
        for n in range(1, 5):  # real releases on main, but nobody signed them or their retirement
            rig.release(f"1.0.{n}")
            rig.retire(f"v1.0.{n}", "fake")
        rig.retire("v0.3.0", "the real one, older than four fakes", sign=self.key)
        proc = rig.tick(**self.allowed)
        self.assertTick(proc, 0)
        self.assertEqual(rig.on_tag(), "v0.2.0", proc.output)

    def test_unsigned_retirements_of_tags_that_do_not_matter_do_not_use_up_the_limit(self) -> None:
        rig = self.rig
        rig.release("0.2.0", sign=self.key)
        for n in range(1, 5):  # real, signed candidates of the running version: lower than it
            rig.release(f"0.3.0rc{n}", sign=self.key)
            rig.retire(f"v0.3.0rc{n}", "fake")  # unsigned; it sorts above retired/v0.3.0
        rig.release("0.3.0", sign=self.key)
        rig.move_to("v0.3.0")
        rig.retire("v0.3.0", "the real one, below four fakes", sign=self.key)
        proc = rig.tick(**self.allowed)
        self.assertTick(proc, 0)
        self.assertEqual(rig.on_tag(), "v0.2.0", proc.output)

    def test_retirements_of_tags_that_are_no_release_do_not_use_up_the_limit(self) -> None:
        rig = self.rig
        rig.release("0.2.0", sign=self.key)
        rig.release("0.3.0", sign=self.key)
        rig.move_to("v0.3.0")
        for n in range(1, 5):  # a name and a commit in order, but not that version
            git(rig.work, "update-ref", f"refs/tags/v1.0.{n}", "HEAD")
            git(rig.work, "tag", "-a", "-m", "fake", f"retired/v1.0.{n}", "HEAD")
        git(rig.work, "push", "-q", "origin", "refs/tags/*:refs/tags/*")
        rig.retire("v0.3.0", "the real one, older than four fakes", sign=self.key)
        proc = rig.tick(**self.allowed)
        self.assertTick(proc, 0)
        self.assertEqual(rig.on_tag(), "v0.2.0", proc.output)

    def flood_retirements(self, count: int = 600) -> None:
        """Publish `count` retirement tags that are no retirement, sorting above every real
        one: what anyone who can push tags, and cannot sign, can do."""
        rig = self.rig
        head = git(rig.work, "rev-parse", "HEAD")
        refs = "".join(f"create refs/tags/retired/v9.0.{n} {head}\n" for n in range(count))
        subprocess.run(
            ["git", "-C", str(rig.work), "update-ref", "--stdin"],
            input=refs,
            text=True,
            env={**os.environ, **GIT_ENV},
            check=True,
        )
        git(rig.work, "push", "-q", "origin", "refs/tags/retired/*:refs/tags/retired/*")

    def test_a_flood_of_fake_retirements_does_not_hide_that_of_the_running_version(self) -> None:
        rig = self.rig
        rig.release("0.2.0", sign=self.key)
        rig.release("0.3.0", sign=self.key)
        rig.move_to("v0.3.0")
        rig.retire("v0.3.0", "the real one", sign=self.key)
        self.flood_retirements()
        proc = rig.tick(**self.allowed)
        self.assertTick(proc, 0)
        self.assertEqual(rig.on_tag(), "v0.2.0", proc.output)

    def test_a_flood_of_fake_retirements_does_not_hide_that_of_a_candidate(self) -> None:
        rig = self.rig
        rig.release("0.2.0", sign=self.key)
        rig.release("0.3.0", sign=self.key)
        rig.retire("v0.3.0", "the real one", sign=self.key)
        rig.move_to("v0.2.0")
        self.flood_retirements()
        proc = rig.tick(**self.allowed)
        self.assertTick(proc, 0)
        self.assertEqual(rig.on_tag(), "v0.2.0", proc.output)

    def test_a_signed_retirement_on_another_commit_retires_nothing(self) -> None:
        rig = self.rig
        rig.release("0.2.0", sign=self.key)
        rig.release("0.3.0", sign=self.key)
        rig.move_to("v0.3.0")
        rig.retire("v0.3.0", "it names v0.3.0 but sits on v0.2.0", sign=self.key, at="v0.2.0")
        self.assertTick(rig.tick(**self.allowed), 0)
        self.assertEqual(rig.on_tag(), "v0.3.0")

    def test_a_signed_object_published_as_another_release_is_no_release(self) -> None:
        rig = self.rig
        # the maintainer signed a tag with a name of their own on a commit whose version is
        # 0.2.1: with that signature, the version and main all in order, only the name inside
        # the tag object tells that it is not "v0.2.1"
        rig.release("0.2.1", tag="scratch-0.2.1", sign=self.key)
        self.publish_as("v0.2.1", "scratch-0.2.1")
        proc = rig.tick(**self.allowed)
        self.assertTick(proc, 0)
        self.assertEqual(rig.on_tag(), "v0.1.0")
        self.assertRegex(proc.output, r"v0\.2\.1.*signature")

    def test_signatures_stay_required_when_the_version_known_to_work_has_no_keys(self) -> None:
        rig = self.rig
        rig.release("0.1.5", sign=self.key)  # signed, but without the keys in its tree
        rig.release(
            "0.2.0",
            sign=self.key,
            extra={"warpzone/data/allowed_signers": self.key.allowed},
        )
        rig.move_to("v0.2.0")
        self.assertTick(rig.tick(), 0)
        self.assertEqual(rig.state()["signing"], "1")
        rig.write_conf(pin="v0.1.5")  # down to a release that has no allowed_signers
        self.assertTick(rig.tick(), 0)
        self.assertEqual(rig.on_tag(), "v0.1.5")
        rig.write_conf()
        rig.release("0.3.0")  # nobody signed it
        proc = rig.tick()
        self.assertTick(proc, 0)
        self.assertEqual(rig.on_tag(), "v0.2.0", "v0.3.0 is unsigned; v0.2.0 is signed")
        self.assertRegex(proc.output, r"v0\.3\.0.*signature")


class KeysNeverGoBackTests(UpdaterCase):
    def setUp(self) -> None:
        super().setUp()
        if not SSH_KEYGEN:
            self.skipTest("ssh-keygen is not installed")
        self.old = SigningKey(self.rig.root / "keys", "old")
        self.new = SigningKey(self.rig.root / "keys", "new")

    def test_going_down_to_an_older_release_does_not_trust_its_older_keys(self) -> None:
        rig = self.rig
        both = {"warpzone/data/allowed_signers": self.old.allowed + self.new.allowed}
        only_new = {"warpzone/data/allowed_signers": self.new.allowed}
        rig.release("0.3.5", sign=self.new, extra=both)  # still trusts the old key
        rig.move_to("v0.3.5")
        self.assertTick(rig.tick(), 0)
        rig.release("0.4.0", sign=self.new, extra=only_new)  # the old key is dropped
        self.assertTick(rig.tick(), 0)
        self.assertEqual(rig.on_tag(), "v0.4.0")
        rig.retire("v0.4.0", "withdrawn", sign=self.new)
        self.assertTick(rig.tick(), 0)
        self.assertEqual(rig.on_tag(), "v0.3.5", "down, to a release that lists both keys")
        # the old key may have been the one that leaked: nothing it signs is installed
        rig.release("0.9.0", sign=self.old, extra=both)
        proc = rig.tick()
        self.assertTick(proc, 0)
        self.assertEqual(rig.on_tag(), "v0.3.5")
        self.assertRegex(proc.output, r"v0\.9\.0.*signature")
        rig.release("0.9.1", sign=self.new, extra=only_new)
        self.assertTick(rig.tick(), 0)
        self.assertEqual(rig.on_tag(), "v0.9.1", "the newer key still works")

    def test_the_version_of_the_keys_is_kept_only_when_they_were_copied(self) -> None:
        rig = self.rig
        only_new = {"warpzone/data/allowed_signers": self.new.allowed}
        rig.release("0.4.0", sign=self.new, extra=only_new)
        rig.move_to("v0.4.0")
        blocked = rig.state_dir / "allowed_signers.last.new"
        blocked.mkdir()  # the copy cannot be written, as on a full disk
        self.assertTick(rig.tick(), 0)
        self.assertFalse((rig.state_dir / "allowed_signers.last").exists())
        self.assertEqual(rig.state().get("signers_version", ""), "", "no copy, no version")
        blocked.rmdir()
        self.assertTick(rig.tick(), 0)
        self.assertEqual(rig.state()["signers_version"], "0.4.0")
        self.assertTrue((rig.state_dir / "allowed_signers.last").is_file())

    def test_no_move_down_is_accepted_in_a_tick_whose_keys_could_not_be_copied(self) -> None:
        rig = self.rig
        both = {"warpzone/data/allowed_signers": self.old.allowed + self.new.allowed}
        only_new = {"warpzone/data/allowed_signers": self.new.allowed}
        rig.release("0.3.5", sign=self.new, extra=both)
        rig.release("0.4.0", sign=self.new, extra=only_new)
        rig.move_to("v0.3.5")
        self.assertTick(rig.tick(), 0)
        self.assertEqual(rig.on_tag(), "v0.4.0")
        # the version of the keys on record is still the old one: v0.4.0 dropped a key
        self.assertEqual(rig.state()["signers_version"], "0.3.5")
        rig.retire("v0.4.0", "withdrawn", sign=self.new)
        blocked = rig.state_dir / "allowed_signers.last.new"
        blocked.mkdir()  # the copy cannot be written, as on a full disk
        proc = rig.tick()
        self.assertTick(proc, 0)
        self.assertEqual(rig.on_tag(), "v0.4.0", "no way down with a stale version of the keys")
        self.assertRegex(proc.output, r"not moving down.*keys")
        blocked.rmdir()
        self.assertTick(rig.tick(), 0)
        self.assertEqual(rig.on_tag(), "v0.3.5", "with the copy made, the retirement goes down")
        self.assertEqual(rig.state()["signers_version"], "0.4.0")
        rig.release("0.9.0", sign=self.old, extra=both)
        proc = rig.tick()
        self.assertTick(proc, 0)
        self.assertEqual(rig.on_tag(), "v0.3.5", "the dropped key is still not trusted")

    def test_without_the_copy_of_the_keys_an_older_release_does_not_bring_back_older_ones(
        self,
    ) -> None:
        rig = self.rig
        both = {"warpzone/data/allowed_signers": self.old.allowed + self.new.allowed}
        only_new = {"warpzone/data/allowed_signers": self.new.allowed}
        rig.release("0.3.5", sign=self.new, extra=both)
        rig.release("0.4.0", sign=self.new, extra=only_new)
        rig.move_to("v0.4.0")
        self.assertTick(rig.tick(), 0)
        self.assertEqual(rig.state()["signers_version"], "0.4.0")
        # the copy is lost while the known-good version goes down to one that lists both keys
        (rig.state_dir / "allowed_signers.last").unlink()
        rig.move_to("v0.3.5")
        rig.write_state(
            **{**rig.state(), "good_tag": "v0.3.5", "good_sha": git(rig.clone, "rev-parse", "HEAD")}
        )
        rig.release("0.9.0", sign=self.old, extra=both)
        proc = rig.tick()
        self.assertTick(proc, 0)
        self.assertEqual(rig.on_tag(), "v0.3.5", proc.output)
        self.assertEqual(rig.state()["signers_version"], "0.4.0")

    def test_a_retirement_made_on_the_release_tag_itself_counts(self) -> None:
        rig = self.rig
        allowed = {"WZ_ALLOWED_SIGNERS": str(rig.allow(self.new))}
        rig.release("0.2.0", sign=self.new)
        rig.release("0.3.0", sign=self.new)  # annotated: `git tag -s retired/v0.3.0 v0.3.0` nests
        rig.move_to("v0.3.0")
        rig.retire("v0.3.0", "withdrawn the natural way", sign=self.new, nested=True)
        self.assertTick(rig.tick(**allowed), 0)
        self.assertEqual(rig.on_tag(), "v0.2.0")


class StaleTargetTests(UpdaterCase):
    """A version that was chosen at one tick and is checked at a later one is chosen again."""

    def prepare(self, rig, version: str, **state: str) -> None:
        git(rig.clone, "fetch", "-q", "--tags")
        rig.write_state(
            good_tag="v0.1.0",
            good_sha=rig.head(),
            **{
                k: v.format(sha=git(rig.work, "rev-parse", f"v{version}^{{commit}}"))
                for k, v in state.items()
            },
        )

    def assert_left_alone(self, rig, version: str) -> None:
        self.assertEqual(rig.on_tag(), "v0.1.0")
        state = rig.state()
        self.assertEqual(state["good_tag"], "v0.1.0")
        self.assertEqual(state["trying_tag"], "")
        self.assertEqual(state["suspect_tag"], "")
        self.assertEqual([c for c in rig.calls() if f"@ {version}" in c], [])

    def test_a_version_being_tried_that_gets_retired_is_not_promoted(self) -> None:
        rig = self.rig
        rig.release("0.2.0")
        self.prepare(rig, "0.2.0", trying_tag="v0.2.0", trying_sha="{sha}", attempts="1")
        rig.retire("v0.2.0", "found out in the meantime")
        rig.tick()
        self.assert_left_alone(rig, "0.2.0")

    def test_a_suspect_that_gets_retired_is_not_installed(self) -> None:
        rig = self.rig
        rig.release("0.2.0")
        self.prepare(rig, "0.2.0", suspect_tag="v0.2.0", suspect_sha="{sha}")
        rig.retire("v0.2.0", "found out in the meantime")
        rig.tick()
        self.assert_left_alone(rig, "0.2.0")

    def test_a_retired_release_below_a_dropped_trial_is_not_installed(self) -> None:
        rig = self.rig
        rig.release("0.2.0")
        rig.retire("v0.2.0", "withdrawn")
        rig.release("0.3.0rc1")
        rig.write_conf(channel="stable")
        self.prepare(rig, "0.3.0rc1", trying_tag="v0.3.0rc1", trying_sha="{sha}", attempts="1")
        git(rig.clone, "checkout", "-q", "--detach", "v0.3.0rc1")  # a power cut in the check
        proc = rig.tick()
        self.assertEqual(rig.on_tag(), "v0.1.0", proc.output)
        self.assertEqual(rig.state()["trying_tag"], "")
        self.assertEqual([c for c in rig.calls() if "@ 0.2.0" in c], [])

    def test_a_suspect_candidate_is_dropped_when_the_channel_goes_back_to_stable(self) -> None:
        rig = self.rig
        rig.release("0.2.0rc1")
        rig.write_conf(channel="stable")
        self.prepare(rig, "0.2.0rc1", suspect_tag="v0.2.0rc1", suspect_sha="{sha}")
        rig.tick()
        self.assert_left_alone(rig, "0.2.0rc1")


class LiveCopyTests(UpdaterCase):
    """The provision of a release under trial installs that release's updater as the live copy;
    when the release is rejected the updater that was live before comes back."""

    BROKEN = (
        "#!/bin/sh\nUPDATER_ABI=1\n"
        'case ${1:-} in selftest) echo "selftest: ok abi=1" ;; *) exit 1 ;; esac\n'
    )

    def good_copy(self) -> Path:
        return self.rig.state_dir / "updater.sh.good"

    def test_a_rejected_release_does_not_leave_its_updater_live(self) -> None:
        rig = self.rig
        original = rig.updater.read_text()
        # of a higher ABI than the live one, which "provision" would never replace
        higher = self.BROKEN.replace("ABI=1", "ABI=9").replace("abi=1", "abi=9")
        rig.release(
            "0.2.0", "install_updater health_fails", extra={"warpzone/data/updater.sh": higher}
        )
        self.assertTick(rig.tick(), 1)
        self.assertEqual(rig.bad(), ["v0.2.0"])
        self.assertEqual(rig.on_tag(), "v0.1.0")
        self.assertEqual(rig.updater.read_text(), original)
        self.assertEqual(os.stat(rig.updater).st_mode & 0o111, 0o111)
        self.assertTick(rig.tick(), 0)  # the next tick runs the restored script
        self.assertFalse(self.good_copy().exists(), "nothing left to restore")

    def test_an_environment_failure_restores_it_too(self) -> None:
        rig = self.rig
        original = rig.updater.read_text()
        rig.release("0.2.0", "install_updater", extra={"warpzone/data/updater.sh": self.BROKEN})
        rig.flag("env_broken")  # provision fails for every release: the tag is not blamed
        rig.tick()
        self.assertEqual(rig.bad(), [])
        self.assertEqual(rig.updater.read_text(), original)

    def test_a_release_that_passes_keeps_its_updater_and_the_old_one_until_probation(self) -> None:
        rig = self.rig
        original = rig.updater.read_text()
        newer = original + "# a newer script\n"
        rig.release("0.2.0", "install_updater", extra={"warpzone/data/updater.sh": newer})
        self.assertTick(rig.tick(), 0)
        self.assertEqual(rig.updater.read_text(), newer)
        self.assertEqual(self.good_copy().read_text(), original)
        rig.boot_id.write_text("boot-2\n")
        self.assertTick(rig.tick(now=NOW + 900), 0)  # probation passes
        self.assertEqual(rig.updater.read_text(), newer)
        self.assertFalse(self.good_copy().exists())

    def test_a_failure_in_probation_puts_the_previous_updater_back(self) -> None:
        rig = self.rig
        original = rig.updater.read_text()
        newer = original + "# a newer script\n"
        rig.release("0.2.0", "install_updater", extra={"warpzone/data/updater.sh": newer})
        self.assertTick(rig.tick(), 0)
        rig.boot_id.write_text("boot-2\n")
        rig.break_version("0.2.0", "health_fails")
        self.assertTick(rig.tick(now=NOW + 900), 1)
        self.assertEqual(rig.bad(), ["v0.2.0"])
        self.assertEqual(rig.updater.read_text(), original)

    def test_a_run_that_is_resumed_keeps_the_copy_it_saved_first(self) -> None:
        rig = self.rig
        original = rig.updater.read_text()
        newer = original + "# a newer script\n"
        rig.release("0.2.0", "install_updater die", extra={"warpzone/data/updater.sh": newer})
        self.assertNotEqual(rig.tick().returncode, 0)  # a power cut after the new script went live
        self.assertEqual(rig.updater.read_text(), newer)
        self.assertNotEqual(rig.tick(now=NOW + 900).returncode, 0)
        self.assertEqual(self.good_copy().read_text(), original)
        self.assertTick(rig.tick(now=NOW + 1800), 1)  # two attempts without a verdict
        self.assertEqual(rig.updater.read_text(), original)
        self.assertEqual(rig.bad(), [])

    def test_a_kept_copy_that_is_empty_or_damaged_is_not_put_back(self) -> None:
        # what an ext4 volume can hold after a power cut: a file of no bytes
        for content in ("", "UPDATER_ABI=1\nif then fi (\n", "echo no abi line\n"):
            with self.subTest(content=content):
                rig = Rig(self)
                (rig.fake_dir / "good_content").write_text(content)
                rig.release(
                    "0.2.0",
                    "install_updater corrupt_good health_fails",
                    extra={"warpzone/data/updater.sh": self.BROKEN},
                )
                proc = rig.tick()
                self.assertEqual(rig.updater.read_text(), self.BROKEN, proc.output)
                self.assertIn("not put back", proc.output)

    def test_only_the_live_copy_itself_is_ever_kept_or_replaced(self) -> None:
        # the clone's own script, reached through a symlink, is not the live copy
        rig = self.rig
        rig.release("0.2.0", "health_fails")
        inside = rig.clone / "warpzone" / "data" / "updater.sh"
        inside.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(UPDATER, inside)
        before = inside.read_bytes()
        link = rig.root / "clone-link"
        link.symlink_to(rig.clone)
        run = subprocess.run(
            ["sh", str(link / "warpzone" / "data" / "updater.sh"), "tick"],
            env=rig.env(),
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(inside.read_bytes(), before, run.stdout)
        self.assertFalse(self.good_copy().exists(), run.stdout)

    def test_a_live_copy_reached_through_a_symlink_is_still_put_back(self) -> None:
        rig = self.rig
        original = rig.updater.read_text()
        rig.release(
            "0.2.0", "install_updater health_fails", extra={"warpzone/data/updater.sh": self.BROKEN}
        )
        link = rig.root / "live-link"
        link.symlink_to(rig.updater.parent)
        subprocess.run(
            ["sh", str(link / "updater.sh"), "tick"],
            env=rig.env(),
            stdin=subprocess.DEVNULL,
            capture_output=True,
            check=False,
        )
        self.assertEqual(rig.updater.read_text(), original)


# A release that a real Python can import and run: the empty modules the import check names, and
# a `__main__` that answers `--version` and records the other commands it is given.
FAKE_MAIN = """import os, sys
import warpzone
if sys.argv[1:] == ["--version"]:
    print("warpzone " + warpzone.__version__)
    sys.exit(0)
with open(os.path.join(os.environ["WZ_FAKE_DIR"], "main-calls"), "a") as fh:
    fh.write(" ".join(sys.argv[1:]) + "\\n")
with open(os.path.join(os.environ["WZ_FAKE_DIR"], "main-prefix"), "a") as fh:
    fh.write(sys.prefix + "\\n")
sys.exit(int(os.environ.get("WZ_FAKE_EXIT", "0")))
"""
PACKAGE: dict[str, str] = {
    "warpzone/cli.py": "",
    "warpzone/provision.py": "",
    "warpzone/health.py": "",
    "warpzone/__main__.py": FAKE_MAIN,
}


def with_updater() -> dict[str, str]:
    """The files of a release that has the updater and can be run."""
    return {**PACKAGE, "warpzone/data/updater.sh": UPDATER.read_text()}


class RealPythonCase(UpdaterCase):
    """The updater with the interpreter of the tests, which can make a venv."""

    def setUp(self) -> None:
        super().setUp()
        self.base = self.rig.root / "base dir"  # a space: every path must be quoted
        self.base.mkdir()

    def env(self, **extra: str) -> dict[str, str]:
        return self.rig.env(**{"WZ_PYTHON_FALLBACKS": sys.executable, **extra})

    def venv_python(self, venv: Path) -> Path:
        return venv / "bin" / "python"

    def main_calls(self) -> list[str]:
        path = self.rig.fake_dir / "main-calls"
        return path.read_text().splitlines() if path.exists() else []


class InstallTests(RealPythonCase):
    def setUp(self) -> None:
        super().setUp()
        rig = self.rig
        rig.release("0.2.0", extra=with_updater())
        # main is ahead of the newest release, as it is for a person who has just cloned
        (rig.work / "notes.txt").write_text("work in progress\n")
        git(rig.work, "add", "-A")
        git(rig.work, "commit", "-q", "-m", "Work after the release")
        git(rig.work, "push", "-q", "origin", "main")
        self.src = self.base / "src"
        self.venv = self.base / "venv"
        subprocess.run(
            ["git", "clone", "-q", str(rig.origin), str(self.src)],
            check=True,
            env={**os.environ, **GIT_ENV},
        )

    def script(self) -> Path:
        return self.src / "warpzone" / "data" / "updater.sh"

    def install(self, *args: str, shell: str | None = None, **extra: str):
        shell = shell or os.environ.get("WZ_TEST_SHELL", "sh")
        env = self.env(WZ_CONF=str(self.rig.root / "no-conf"), **extra)
        proc = subprocess.run(
            [shell, str(self.script()), "install", *args],
            env=env,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )
        proc.output = proc.stdout + proc.stderr  # type: ignore[attr-defined]
        return proc

    def head_tag(self) -> str:
        try:
            return git(
                self.src, "describe", "--exact-match", "--tags", "--match", "v[0-9]*", "HEAD"
            )
        except AssertionError:
            return ""

    def test_it_checks_out_the_best_release_makes_the_venv_and_provisions(self) -> None:
        for shell in SHELLS:
            with self.subTest(shell=shell):
                self.rig.fake_dir.joinpath("main-calls").unlink(missing_ok=True)
                shutil.rmtree(self.venv, ignore_errors=True)
                git(self.src, "checkout", "-q", "main")
                proc = self.install("deck", "--config", "/h/config.toml", shell=shell)
                self.assertEqual(proc.returncode, 0, proc.output)
                self.assertEqual(self.head_tag(), "v0.2.0")
                self.assertTrue(self.venv_python(self.venv).exists())
                shown = subprocess.run(
                    [str(self.venv / "bin" / "warpzone"), "--version"],
                    cwd=self.rig.root,
                    capture_output=True,
                    text=True,
                    check=False,
                )
                self.assertEqual(shown.stdout.strip(), "warpzone 0.2.0", shown.stderr)
                self.assertEqual(
                    self.main_calls(), ["provision device deck --config /h/config.toml"]
                )

    def test_the_venv_has_no_pip_a_pth_to_the_clone_and_a_shim(self) -> None:
        other = (
            self.base / "elsewhere" / "venv"
        )  # --venv picks the folder; none is made beside the clone
        self.assertEqual(self.install("deck", "--venv", str(other)).returncode, 0)
        self.assertFalse(self.venv.exists())
        pths = list(other.glob("lib/python*/site-packages/*.pth"))
        self.assertEqual([p.read_text().strip() for p in pths], [str(self.src)])
        self.assertEqual(list(other.glob("lib/python*/site-packages/pip")), [])
        shim = other / "bin" / "warpzone"
        self.assertTrue(os.access(shim, os.X_OK))
        text = shim.read_text()
        self.assertTrue(text.startswith("#!/bin/sh\n"))
        self.assertIn(f"'{other}/bin/python' -m warpzone", text)

    def test_the_channel_option_picks_a_candidate(self) -> None:
        self.rig.release("0.3.0rc1", extra=with_updater())
        self.assertEqual(self.install("deck", "--channel", "rc").returncode, 0)
        self.assertEqual(self.head_tag(), "v0.3.0rc1")

    def test_the_rules_are_the_ones_of_a_tick(self) -> None:
        rig = self.rig
        rig.release("0.2.1", extra=with_updater(), on_main=False)  # not on main
        rig.release("0.2.2", extra=with_updater(), file_version="0.2.1")  # wrong version
        rig.release("0.2.3", extra=with_updater())
        rig.retire("v0.2.3")  # retired
        rig.release("0.2.4", extra=with_updater())
        rig.retire("v0.2.4", nested=True)
        proc = self.install("deck")
        self.assertEqual(proc.returncode, 0, proc.output)
        self.assertEqual(self.head_tag(), "v0.2.0")

    def test_a_release_without_the_updater_is_not_installable(self) -> None:
        rig = self.rig
        (rig.work / "warpzone" / "data" / "updater.sh").unlink()
        rig.release("0.2.5")  # newer, and its tree has no warpzone/data/updater.sh
        proc = self.install("deck")
        self.assertEqual(proc.returncode, 0, proc.output)
        self.assertEqual(self.head_tag(), "v0.2.0", "the best one that has the updater")

    def test_with_no_release_that_has_the_updater_it_changes_nothing(self) -> None:
        old_rig = Rig(self)  # releases that predate the updater
        old_rig.release("0.2.0")
        old = self.base / "old"
        subprocess.run(
            ["git", "clone", "-q", str(old_rig.origin), str(old)],
            check=True,
            env={**os.environ, **GIT_ENV},
        )
        script = old / "warpzone" / "data" / "updater.sh"
        script.parent.mkdir(parents=True)
        shutil.copy(UPDATER, script)  # untracked: no tag has it
        before = git(old, "rev-parse", "HEAD")
        proc = subprocess.run(
            ["sh", str(script), "install", "deck", "--venv", str(self.venv)],
            env=self.env(WZ_CONF=str(old_rig.root / "no-conf")),
            capture_output=True,
            text=True,
            stdin=subprocess.DEVNULL,
            check=False,
        )
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("updater", proc.stdout + proc.stderr)
        self.assertEqual(git(old, "rev-parse", "HEAD"), before)
        self.assertFalse(self.venv.exists())

    def test_a_checkout_with_changes_or_commits_of_its_own_is_refused(self) -> None:
        (self.src / "warpzone" / "__init__.py").write_text("# edited\n")
        proc = self.install("deck")
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("changes", proc.output)
        self.assertEqual((self.src / "warpzone" / "__init__.py").read_text(), "# edited\n")
        self.assertFalse(self.venv.exists())
        git(self.src, "checkout", "-q", "--", "warpzone/__init__.py")
        git(self.src, "commit", "-q", "--allow-empty", "-m", "mine")
        proc = self.install("deck")
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("commits of its own", proc.output)
        self.assertFalse(self.venv.exists())

    def test_it_asks_for_a_device_name(self) -> None:
        self.assertEqual(self.install().returncode, 2)
        self.assertEqual(self.install("--config", "/c").returncode, 2)
        self.assertEqual(self.install("deck", "--channel", "nightly").returncode, 2)
        self.assertEqual(self.install("deck", "--frobnicate").returncode, 2)

    def test_arguments_after_two_dashes_go_to_provision(self) -> None:
        proc = self.install("deck", "--config", "/c.toml", "--", "--timeout", "5")
        self.assertEqual(proc.returncode, 0, proc.output)
        self.assertEqual(self.main_calls(), ["provision device deck --config /c.toml --timeout 5"])

    def test_a_failing_provision_fails_the_install_with_its_code(self) -> None:
        proc = self.install("deck", WZ_FAKE_EXIT="1")
        self.assertEqual(proc.returncode, 1, proc.output)
        self.assertIn("provision device", proc.output)

    def test_no_usable_python_stops_before_changing_anything(self) -> None:
        proc = self.install("deck", WZ_PYTHON_FALLBACKS="/nonexistent/python")
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("Python", proc.output)
        self.assertFalse(self.venv.exists())
        self.assertEqual(git(self.src, "symbolic-ref", "-q", "--short", "HEAD"), "main")

    def test_a_healthy_venv_is_kept_on_a_second_run(self) -> None:
        self.assertEqual(self.install("deck").returncode, 0)
        marker = self.venv / "marker"
        marker.write_text("mine")
        # one that warpzone made, but whose mark a power cut kept from being written
        (self.venv / "warpzone-venv").unlink()
        (self.rig.state_dir / "venv-owned").unlink(missing_ok=True)  # dropped with the mark
        self.assertEqual(self.install("deck").returncode, 0)
        self.assertEqual(marker.read_text(), "mine")
        self.assertTrue((self.venv / "warpzone-venv").exists(), "marked now")

    def test_the_folder_that_holds_the_venv_is_never_cleared(self) -> None:
        keep = self.base / "play-state"
        keep.mkdir()
        (keep / "ledger").write_text("saves")
        self.venv.mkdir()
        (self.venv / "pyvenv.cfg").write_text("home = /nowhere\n")  # a venv that is broken
        (self.venv / "warpzone-venv").write_text("made by warpzone\n")  # and that is ours
        self.assertEqual(self.install("deck").returncode, 0)
        self.assertEqual((keep / "ledger").read_text(), "saves")

    def test_a_venv_that_warpzone_did_not_make_is_refused_and_left_alone(self) -> None:
        # the user's own venv, or the one of an older install, that does not work
        self.venv.mkdir()
        (self.venv / "pyvenv.cfg").write_text("home = /nowhere\n")
        (self.venv / "mine.txt").write_text("my packages")
        proc = self.install("deck")
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("not made by warpzone", proc.output)
        self.assertIn("--venv", proc.output)
        self.assertEqual((self.venv / "mine.txt").read_text(), "my packages")
        self.assertEqual(self.main_calls(), [])

    def test_a_venv_that_is_a_symlink_is_never_cleared(self) -> None:
        real = self.base / "real-venv"
        real.mkdir()
        (real / "pyvenv.cfg").write_text("home = /nowhere\n")
        (real / "warpzone-venv").write_text("made by warpzone\n")  # only the link stops it
        (real / "mine.txt").write_text("my packages")
        self.venv.symlink_to(real)
        proc = self.install("deck")
        self.assertNotEqual(proc.returncode, 0)
        self.assertEqual((real / "mine.txt").read_text(), "my packages")
        self.assertTrue((real / "pyvenv.cfg").exists())

    def test_a_signed_release_is_required_once_the_clone_trusts_a_key(self) -> None:
        if SSH_KEYGEN is None:
            self.skipTest("ssh-keygen is not installed")
        key = SigningKey(self.rig.root / "keys")
        allowed = self.rig.allow(key)
        self.rig.release("0.2.7", extra=with_updater(), sign=key)
        self.rig.release("0.2.8", extra=with_updater())  # not signed
        proc = self.install("deck", WZ_ALLOWED_SIGNERS=str(allowed))
        self.assertEqual(proc.returncode, 0, proc.output)
        self.assertEqual(self.head_tag(), "v0.2.7")


class RepairTests(RealPythonCase):
    """`tick` mends a venv that no longer works, without pip and without the network."""

    def setUp(self) -> None:
        super().setUp()
        rig = self.rig
        rig.release("0.2.0", extra=with_updater())
        rig.move_to("v0.2.0")
        self.venv = self.base / "venv"
        rig.write_conf(python=str(self.venv / "bin" / "python"))
        (self.base / "play-state").mkdir()
        (self.base / "play-state" / "ledger").write_text("saves")

    def tick(self, **extra: str):
        return self.rig.tick(WZ_PYTHON_FALLBACKS=sys.executable, **extra)

    def make_venv(self, *, pth: bool = False, ours: bool = True) -> None:
        subprocess.run(
            [sys.executable, "-m", "venv", "--without-pip", str(self.venv)],
            check=True,
            capture_output=True,
        )
        if ours:
            (self.venv / "warpzone-venv").write_text("made by warpzone\n")
        if pth:
            site = next(self.venv.glob("lib/python*/site-packages"))
            (site / "warpzone.pth").write_text(f"{self.rig.clone}\n")

    def assert_mended(self) -> None:
        pths = list(self.venv.glob("lib/python*/site-packages/*.pth"))
        self.assertEqual([p.read_text().strip() for p in pths], [str(self.rig.clone)])
        self.assertEqual(list(self.venv.glob("lib/python*/site-packages/pip")), [])
        shown = subprocess.run(
            [str(self.venv / "bin" / "warpzone"), "--version"],
            cwd=self.rig.root,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(shown.stdout.strip(), "warpzone 0.2.0", shown.stderr)
        self.assertEqual((self.base / "play-state" / "ledger").read_text(), "saves")
        prefixes = (self.rig.fake_dir / "main-prefix").read_text().split("\n")
        self.assertEqual(
            {os.path.realpath(p) for p in prefixes if p}, {os.path.realpath(self.venv)}
        )

    def test_a_venv_that_is_not_there_is_made(self) -> None:
        proc = self.tick()
        self.assertTick(proc, 0)
        self.assert_mended()
        self.assertTrue((self.venv / "warpzone-venv").exists(), "the mark is written")
        self.assertNotIn(str(self.venv), self.owned_notes(), "the note lasts only until the mark")
        self.assertIn("venv", proc.output)
        self.assertEqual(self.rig.state()["last_result"], "ok")

    def owned_notes(self) -> list[str]:
        path = self.rig.state_dir / "venv-owned"
        return path.read_text().splitlines() if path.exists() else []

    def test_the_note_does_not_outlive_the_venv_it_was_for(self) -> None:
        self.assertTick(self.tick(), 0)  # warpzone makes the venv: noted, then marked
        self.assertTrue((self.venv / "warpzone-venv").exists())
        shutil.rmtree(self.venv)
        self.make_venv(pth=False, ours=False)  # the user makes one of their own, in its place
        (self.venv / "mine.txt").write_text("my packages")
        proc = self.tick()
        self.assertTrue((self.venv / "mine.txt").exists(), "their venv was cleared")
        self.assertEqual((self.venv / "mine.txt").read_text(), "my packages")
        self.assertIn("not made by warpzone", proc.output)
        self.assertEqual((self.base / "play-state" / "ledger").read_text(), "saves")

    def test_a_venv_whose_python_is_gone_is_made_again(self) -> None:
        self.make_venv(pth=True)
        real = self.venv / "bin" / "python"
        real.unlink()
        real.symlink_to(self.base / "gone" / "python3")
        proc = self.tick()
        self.assertTick(proc, 0)
        self.assert_mended()

    def test_a_venv_that_cannot_import_warpzone_gets_the_pth(self) -> None:
        self.make_venv(pth=False)
        proc = self.tick()
        self.assertTick(proc, 0)
        self.assert_mended()

    def test_a_working_venv_is_left_alone(self) -> None:
        self.make_venv(pth=True)
        (self.venv / "marker").write_text("mine")
        cfg = (self.venv / "pyvenv.cfg").stat().st_mtime_ns
        proc = self.tick()
        self.assertTick(proc, 0)
        self.assertEqual((self.venv / "marker").read_text(), "mine")
        self.assertEqual((self.venv / "pyvenv.cfg").stat().st_mtime_ns, cfg)
        self.assertNotIn("repair", proc.output)

    def test_a_venv_noted_as_made_by_warpzone_is_repaired_without_its_mark(self) -> None:
        # a power cut between --clear and the mark: the note outside the venv remembers
        self.make_venv(pth=False, ours=False)
        (self.rig.state_dir / "venv-owned").write_text(f"{self.venv}\n")
        self.assertTick(self.tick(), 0)
        self.assert_mended()

    def test_a_venv_that_warpzone_did_not_make_is_left_alone(self) -> None:
        self.make_venv(pth=False, ours=False)  # it runs, but cannot find the clone
        (self.venv / "mine.txt").write_text("my packages")
        proc = self.tick()
        self.assertEqual((self.venv / "mine.txt").read_text(), "my packages")
        self.assertIn("not repairing", proc.output)
        self.assertIn("not made by warpzone", proc.output)
        self.assertEqual((self.base / "play-state" / "ledger").read_text(), "saves")

    def test_a_venv_that_is_a_symlink_is_never_cleared(self) -> None:
        real = self.base / "real-venv"
        real.mkdir()
        (real / "pyvenv.cfg").write_text("home = /nowhere\n")
        (real / "warpzone-venv").write_text("made by warpzone\n")  # only the link stops it
        (real / "mine.txt").write_text("my packages")
        self.venv.symlink_to(real)
        proc = self.tick()
        self.assertEqual((real / "mine.txt").read_text(), "my packages")
        self.assertTrue((real / "pyvenv.cfg").exists())
        self.assertIn("not repairing", proc.output)

    def test_a_clone_with_commits_of_its_own_or_changes_is_not_repaired(self) -> None:
        clone = self.rig.clone
        git(clone, "checkout", "-q", "-b", "mine")
        git(clone, "commit", "-q", "--allow-empty", "-m", "mine")
        self.tick()
        self.assertFalse(self.venv.exists(), "a dev checkout")
        git(clone, "checkout", "-q", "--detach", "v0.2.0")
        (clone / "warpzone" / "__init__.py").write_text("# edited\n")
        self.tick()
        self.assertFalse(self.venv.exists(), "a dirty checkout")

    def test_the_folder_must_be_called_venv(self) -> None:
        other = self.base / "env"
        self.rig.write_conf(python=str(other / "bin" / "python"))
        (other / "bin").mkdir(parents=True)
        (other / "pyvenv.cfg").write_text("home = /nowhere\n")
        (other / "keep").write_text("mine")
        proc = self.tick()
        self.assertEqual((other / "keep").read_text(), "mine")
        self.assertTrue((other / "pyvenv.cfg").exists())
        self.assertIn("not repairing", proc.output)

    def test_a_folder_that_is_not_a_venv_is_never_cleared(self) -> None:
        self.venv.mkdir()
        (self.venv / "notes.txt").write_text("not a venv")
        proc = self.tick()
        self.assertEqual((self.venv / "notes.txt").read_text(), "not a venv")
        self.assertIn("not repairing", proc.output)
        self.assertEqual((self.base / "play-state" / "ledger").read_text(), "saves")

    def test_with_no_other_python_nothing_is_removed(self) -> None:
        self.make_venv(pth=False)
        (self.venv / "marker").write_text("mine")
        proc = self.rig.tick(WZ_PYTHON_FALLBACKS="/nonexistent/python")
        self.assertEqual((self.venv / "marker").read_text(), "mine", proc.output)

    def test_a_clone_cannot_be_inside_the_venv(self) -> None:
        self.venv.mkdir()
        (self.venv / "pyvenv.cfg").write_text("home = /nowhere\n")
        inside = self.venv / "src"
        shutil.move(str(self.rig.clone), str(inside))
        self.rig.write_conf(python=str(self.venv / "bin" / "python"), clone=str(inside))
        proc = self.tick()
        self.assertTrue((inside / ".git").exists(), proc.output)
        self.assertIn("not repairing", proc.output)

    def test_without_a_python_in_the_settings_there_is_no_venv_to_mend(self) -> None:
        self.rig.write_conf(python="")
        proc = self.tick()
        self.assertFalse(self.venv.exists(), proc.output)


class ScriptTests(UpdaterCase):
    def test_it_parses_with_every_shell(self) -> None:
        for shell in SHELLS:
            with self.subTest(shell=shell):
                proc = subprocess.run(
                    [shell, "-n", str(UPDATER)], capture_output=True, text=True, check=False
                )
                self.assertEqual(proc.returncode, 0, proc.stderr)

    def test_shellcheck_is_happy_when_it_is_installed(self) -> None:
        shellcheck = shutil.which("shellcheck")
        if shellcheck is None:
            self.skipTest("shellcheck is not installed")
        proc = subprocess.run(
            [shellcheck, "-s", "sh", str(UPDATER)], capture_output=True, text=True, check=False
        )
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)

    def test_selftest_passes_with_every_shell(self) -> None:
        for shell in SHELLS:
            with self.subTest(shell=shell):
                proc = self.rig.run("selftest", shell=shell)
                self.assertTick(proc, 0)
                self.assertIn("selftest: ok", proc.output)
                self.assertIn("abi=", proc.output)

    def test_it_uses_no_bashisms(self) -> None:
        text = UPDATER.read_text()
        self.assertTrue(text.startswith("#!/bin/sh\n"))
        code = "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("#"))
        for needle in ("<<<", "echo -e", "echo -n", "<(", "&>", "==", "+="):
            self.assertNotIn(needle, code, needle)
        self.assertIsNone(re.search(r"(^|[;&|(!]|\b(if|while|elif))\s*\[\[\s", code, re.M))
        self.assertIsNone(re.search(r"^\s*(local|declare|typeset|source|function)\b", code, re.M))

    def test_unknown_commands_are_a_usage_error(self) -> None:
        self.assertTick(self.rig.run("frobnicate"), 2)
        self.assertTick(self.rig.run(), 2)

    def test_the_script_is_shipped_as_package_data(self) -> None:
        pyproject = (UPDATER.parent.parent.parent / "pyproject.toml").read_text()
        self.assertIn("*.sh", pyproject)

    def test_the_allowed_signers_file_is_shipped_as_package_data(self) -> None:
        pyproject = (UPDATER.parent.parent.parent / "pyproject.toml").read_text()
        self.assertIn("allowed_signers", pyproject)
        self.assertTrue((UPDATER.parent / "allowed_signers").is_file())


# What a bad release looks like to a real Python. The first breaks at import (the files are
# not valid Python), the second imports and answers `--version` but fails its `health` run.
SYNTAX_ERROR = "def broken(:\n"
_MAIN_CALLS = 'with open(os.path.join(os.environ["WZ_FAKE_DIR"], "main-calls")'
HEALTH_FAILS_MAIN = FAKE_MAIN.replace(
    _MAIN_CALLS, 'if sys.argv[1:2] == ["health"]:\n    sys.exit(1)\n' + _MAIN_CALLS, 1
)
# The environment breaks every release alike (the flag file is in the fake folder).
ENV_AWARE_MAIN = FAKE_MAIN.replace(
    _MAIN_CALLS,
    'if os.path.exists(os.path.join(os.environ["WZ_FAKE_DIR"], "env_broken")):\n'
    "    sys.exit(1)\n" + _MAIN_CALLS,
    1,
)


class BadReleaseLifecycleTests(RealPythonCase):
    """A release that fails its checks, from the first tick to the next good release, with the
    real script from the clone, a real venv and a real Python importing the checked-out
    package (the fake is only what `python -m warpzone` does after it has been imported)."""

    # The versions differ in length on purpose: Python trusts a cached `.pyc` when the source
    # has the size and the second of modification it was compiled from, and a rollback in
    # the same second as the checkout, as these tests do, would otherwise import the old one.

    def setUp(self) -> None:
        super().setUp()
        rig = self.rig
        self.src = self.base / "src"
        self.venv = self.base / "venv"
        rig.release("0.2.0", extra={**with_updater(), "warpzone/__main__.py": ENV_AWARE_MAIN})
        subprocess.run(
            ["git", "clone", "-q", str(rig.origin), str(self.src)],
            check=True,
            env={**os.environ, **GIT_ENV},
        )
        script = self.src / "warpzone" / "data" / "updater.sh"
        proc = subprocess.run(
            ["sh", str(script), "install", "deck", "--venv", str(self.venv)],
            env=self.env(WZ_CONF=str(rig.root / "no-conf")),
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        # what `provision device` does on a device: the live copy and the settings
        shutil.copy(script, rig.updater)
        rig.write_conf(clone=str(self.src), python=str(self.venv / "bin" / "python"))

    def tick(self, **kwargs) -> subprocess.CompletedProcess:
        return self.rig.tick(WZ_PYTHON_FALLBACKS=sys.executable, **kwargs)

    def on_tag(self) -> str:
        try:
            return git(
                self.src, "describe", "--exact-match", "--tags", "--match", "v[0-9]*", "HEAD"
            )
        except AssertionError:
            return ""

    def shim_version(self) -> str:
        """What the device itself reports: the shim of the venv running the clone."""
        shown = subprocess.run(
            [str(self.venv / "bin" / "warpzone"), "--version"],
            cwd=self.rig.root,
            capture_output=True,
            text=True,
            check=False,
        )
        return shown.stdout.strip()

    def publish_good(self, version: str) -> str:
        return self.rig.release(
            version,
            extra={
                **with_updater(),
                "warpzone/__main__.py": ENV_AWARE_MAIN,
                "warpzone/cli.py": "",
            },
        )

    def test_a_bad_release_is_rolled_back_marked_bad_and_a_later_good_one_is_taken(self) -> None:
        rig = self.rig
        self.assertTick(self.tick(), 0)
        self.assertEqual(rig.state()["good_tag"], "v0.2.0")
        self.assertEqual(self.shim_version(), "warpzone 0.2.0")

        bad_sha = rig.release("0.30.0", extra={**with_updater(), "warpzone/cli.py": SYNTAX_ERROR})
        self.assertTick(self.tick(now=NOW + DAY), 1)
        state = rig.state()
        self.assertEqual(self.on_tag(), "v0.2.0", "the clone is back on the good release")
        self.assertEqual(
            git(self.src, "status", "--porcelain", "--untracked-files=no"), "", "and clean"
        )
        self.assertEqual(self.shim_version(), "warpzone 0.2.0", "the device still runs")
        self.assertEqual(rig.bad(), ["v0.30.0"])
        self.assertIn(f"sha={bad_sha}", (rig.state_dir / "bad" / "v0.30.0").read_text())
        self.assertEqual(state["last_result"], "rolled-back")
        self.assertEqual(state["good_tag"], "v0.2.0")
        self.assertEqual(state["trying_tag"], "")
        self.assertEqual(state["attempts"], "0")

        # the bad tag is not tried again, on the next ticks or after a reboot
        rig.boot_id.write_text("boot-2\n")
        for later in (2, 3):
            self.assertTick(self.tick(now=NOW + later * DAY), 0)
            self.assertEqual(self.on_tag(), "v0.2.0")
            self.assertEqual(rig.bad(), ["v0.30.0"])

        # a later good release is taken, and the bad one stays bad
        self.publish_good("0.400.0")
        self.assertTick(self.tick(now=NOW + 4 * DAY), 0)
        state = rig.state()
        self.assertEqual(self.on_tag(), "v0.400.0")
        self.assertEqual(self.shim_version(), "warpzone 0.400.0")
        self.assertEqual((state["good_tag"], state["last_result"]), ("v0.400.0", "ok"))
        self.assertEqual(rig.bad(), ["v0.30.0"])

    def test_a_release_that_imports_but_fails_its_health_run_is_rolled_back_too(self) -> None:
        rig = self.rig
        rig.release("0.30.0", extra={**with_updater(), "warpzone/__main__.py": HEALTH_FAILS_MAIN})
        self.assertTick(self.tick(now=NOW + DAY), 1)
        self.assertEqual(self.on_tag(), "v0.2.0")
        self.assertEqual(self.shim_version(), "warpzone 0.2.0")
        self.assertEqual(rig.bad(), ["v0.30.0"])
        self.publish_good("0.400.0")
        self.assertTick(self.tick(now=NOW + 2 * DAY), 0)
        self.assertEqual(self.on_tag(), "v0.400.0")
        self.assertEqual(rig.bad(), ["v0.30.0"])

    def test_a_stale_bytecode_of_the_bad_release_is_not_run_after_the_rollback(self) -> None:
        rig = self.rig
        # Both releases have a __init__.py of the same size ("0.2.0" and "0.3.0"), and every
        # checkout leaves it with one and the same modification time: what a rollback in the
        # second of the checkout does. Python then takes the `.pyc` of the bad release for
        # the source of the good one.
        hook = self.src / ".git" / "hooks" / "post-checkout"
        hook.write_text("#!/bin/sh\ntouch -t 202401020000.00 warpzone/__init__.py\n")
        hook.chmod(0o755)
        init = self.src / "warpzone" / "__init__.py"
        subprocess.run(["touch", "-t", "202401010000.00", str(init)], check=True)
        rig.release("0.3.0", extra={**with_updater(), "warpzone/__main__.py": HEALTH_FAILS_MAIN})
        self.assertTick(self.tick(now=NOW + DAY), 1)
        state = rig.state()
        self.assertEqual(self.on_tag(), "v0.2.0")
        self.assertEqual(rig.bad(), ["v0.3.0"], state)
        self.assertEqual(state["last_result"], "rolled-back")
        self.assertEqual(self.shim_version(), "warpzone 0.2.0", "the shim runs the good source")

    def test_a_release_is_not_marked_bad_when_the_good_one_fails_the_same_way(self) -> None:
        rig = self.rig
        rig.release("0.30.0", extra={**with_updater(), "warpzone/__main__.py": HEALTH_FAILS_MAIN})
        rig.flag("env_broken")  # for example Syncthing is down: the good release fails too
        self.assertTick(self.tick(now=NOW + DAY), 1)
        state = rig.state()
        self.assertEqual(self.on_tag(), "v0.2.0")
        self.assertEqual(rig.bad(), [], "the tag is not burnt")
        self.assertEqual(state["last_result"], "env")
        self.assertEqual(state["retry_after"], str(NOW + 2 * DAY))
        # the environment recovers: after the back-off the release is tried again, and it
        # fails on its own merit this time
        rig.flag("env_broken", False)
        self.assertTick(self.tick(now=NOW + 2 * DAY), 1)
        self.assertEqual(rig.bad(), ["v0.30.0"])
        self.assertEqual(self.on_tag(), "v0.2.0")


if __name__ == "__main__":
    unittest.main()
