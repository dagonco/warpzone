"""`warpzone/data/updater.sh`, run against real git repositories and a fake Python.

The script is POSIX sh that decides which release a device runs and puts it back when a
release does not work (docs/design.md, "Autonomous updates"). Each test builds an upstream
repository with tagged releases, a clone of it, and a fake `python` (a script that behaves
like warpzone according to the checked-out version), then runs `updater.sh tick` the way
the timer does. They run with whatever shells exist (`sh`, `dash`, `bash`) so a macOS and a
Linux run both cover the bashisms that would break on the other.
"""

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

from warpzone import settings_state

UPDATER = Path(__file__).resolve().parent.parent / "warpzone" / "data" / "updater.sh"
NOW = 1_700_000_000
DAY = 86400
HOUR = 3600
GIT = shutil.which("git")
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
        self.proc = self.root / "proc"
        self.fake_dir = self.root / "fake"
        self.boot_id = self.root / "boot_id"
        self.updater = self.root / "live" / "updater.sh"
        self.python = self.root / "bin" / "python"
        for folder in (self.proc, self.fake_dir, self.state_dir, self.updater.parent):
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
        subprocess.run(
            ["git", "init", "-q", "--bare", "-b", "main", str(self.origin)],
            check=True,
            env={**os.environ, **GIT_ENV},
        )
        subprocess.run(
            ["git", "init", "-q", "-b", "main", str(self.work)],
            check=True,
            env={**os.environ, **GIT_ENV},
        )
        git(self.work, "remote", "add", "origin", str(self.origin))
        self.release("0.1.0")
        subprocess.run(
            ["git", "clone", "-q", str(self.origin), str(self.clone)],
            check=True,
            env={**os.environ, **GIT_ENV},
        )
        git(self.clone, "checkout", "-q", "--detach", "v0.1.0")
        self.write_conf()

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
    ) -> str:
        """Commit a release in the upstream repository, tag it and publish it."""
        if not on_main:
            git(self.work, "checkout", "-q", "--detach")
        for name, text in (extra or {}).items():
            (self.work / name).write_text(text)
        (self.work / "warpzone").mkdir(exist_ok=True)
        (self.work / "warpzone" / "__init__.py").write_text(
            f'"""fake"""\n\n__version__ = "{file_version or version}"\n'
        )
        (self.work / "behaviour").write_text(behaviour + "\n")
        git(self.work, "add", "-A")
        git(self.work, "commit", "-q", "-m", f"Release {version}")
        name = tag or f"v{version}"
        git(self.work, "tag", name)
        if on_main:
            git(self.work, "push", "-q", "origin", "main", f"refs/tags/{name}")
        else:
            git(self.work, "push", "-q", "origin", f"refs/tags/{name}")
            git(self.work, "checkout", "-q", "main")
        return git(self.work, "rev-parse", "HEAD" if on_main else f"refs/tags/{name}^{{commit}}")

    def head(self) -> str:
        return git(self.clone, "rev-parse", "HEAD")

    def on_tag(self) -> str:
        """The release tag the clone is on, or '' when it is not exactly on one."""
        try:
            return git(self.clone, "describe", "--exact-match", "--tags", "HEAD")
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

    def add_process(self, pid: int, comm: str, argv: list[str] | None = None) -> None:
        folder = self.proc / str(pid)
        folder.mkdir(parents=True)
        (folder / "comm").write_text(comm + "\n")
        (folder / "cmdline").write_bytes(b"\0".join(a.encode() for a in (argv or [comm])) + b"\0")

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
            "WZ_PROC": str(self.proc),
            "WZ_BOOT_ID_FILE": str(self.boot_id),
            "WZ_FAKE_CLONE": str(self.clone),
            "WZ_FAKE_DIR": str(self.fake_dir),
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

    def tick(self, **kwargs) -> subprocess.CompletedProcess:
        return self.run("tick", **kwargs)

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

    def test_the_remote_is_fetched_at_most_once_a_day(self) -> None:
        rig = self.rig
        self.assertTick(rig.tick(WZ_FETCH_INTERVAL=str(DAY)), 0)
        rig.release("0.2.0")
        self.assertTick(rig.tick(now=NOW + HOUR, WZ_FETCH_INTERVAL=str(DAY)), 0)
        self.assertEqual(rig.on_tag(), "v0.1.0")
        self.assertTick(rig.tick(now=NOW + 26 * HOUR, WZ_FETCH_INTERVAL=str(DAY)), 0)
        self.assertEqual(rig.on_tag(), "v0.2.0")

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
        rig.add_process(7000001, "es-de")  # the menu has been open since the first deferral
        rig.write_state(pending="1", deferred_since=str(NOW - 80 * HOUR))
        self.assertTick(rig.tick(), 0)
        state = rig.state()
        self.assertEqual(state["good_tag"], "v0.2.0")
        self.assertEqual(state["pending"], "1", "the work is still pending")
        self.assertEqual(rig.bad(), [])


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
        rig.add_process(7000001, "es-de")
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
        rig.add_process(7000001, "ES-DE.AppImage")
        rig.tick()
        shutil.rmtree(rig.proc / "7000001")
        self.assertTick(rig.tick(now=NOW + 900), 0)
        self.assertEqual(rig.on_tag(), "v0.2.0")
        self.assertEqual(rig.state()["deferred_since"], "")

    def test_the_process_check_agrees_with_settings_state_running(self) -> None:
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
                    rig.add_process(pid, comm, argv)
                rig.write_conf(frontend=" ".join(names))
                expected = settings_state.running(names, rig.proc)
                proc = rig.run("menu")
                self.assertEqual(proc.returncode == 0, expected, proc.output)
                self.assertIn(proc.returncode, (0, 1))

    def test_a_missing_proc_is_not_a_reason_to_wait(self) -> None:
        rig = self.rig
        rig.release("0.2.0")
        shutil.rmtree(rig.proc)
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
        self.rig.add_process(7000001, "git", ["git", "gc"])
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
        rig.add_process(7000001, "es-de")
        self.assertTick(rig.tick(), 0)
        self.assertEqual(rig.state()["deferred_since"], str(NOW))
        shutil.rmtree(rig.proc / "7000001")
        self.assertTick(rig.tick(now=NOW + HOUR), 0)  # closed, nothing to update
        self.assertEqual(rig.state()["deferred_since"], "")
        rig.release("0.2.0")
        rig.add_process(7000002, "es-de")  # days later somebody is playing
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
        rig.add_process(7000001, "es-de")
        rig.write_state(pending="1", deferred_since=str(NOW - 80 * HOUR))
        self.assertTick(rig.tick(), 1)
        self.assertEqual(rig.state()["last_result"], "env")
        self.assertEqual(rig.state()["deferred_since"], "")

    def test_a_failed_reconcile_ends_the_deferral_with_the_menu_still_open(self) -> None:
        rig = self.rig
        rig.flag("env_broken")
        rig.add_process(7000001, "es-de")
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
        rig.add_process(7000001, "behaviour")  # a file name in the clone
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
        (rig.proc / "uptime").write_text("1000.00 0\n")
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
        (rig.proc / "uptime").write_text("1000.00 0\n")
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
        (rig.proc / "uptime").write_text("100.00 400.00\n")  # it does not advance
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


if __name__ == "__main__":
    unittest.main()
