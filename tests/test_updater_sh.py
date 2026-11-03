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
import tempfile
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
case "$*" in
  *tomllib*) exit 0 ;;
  *"import warpzone"*)
    if has import_broken; then echo "ImportError: broken" >&2; exit 1; fi
    exit 0 ;;
  "-I -m warpzone --version")
    if has wrong_version; then echo "warpzone 0.0.0"; else echo "warpzone $version"; fi
    exit 0 ;;
  "-m warpzone provision device "*)
    [ -e "$dir/env_broken" ] && exit 1
    if has die; then kill -s KILL 0; fi
    if has die_once && [ ! -e "$dir/died" ]; then : >"$dir/died"; kill -s KILL 0; fi
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
    ) -> str:
        """Commit a release in the upstream repository, tag it and publish it."""
        if not on_main:
            git(self.work, "checkout", "-q", "--detach")
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
                self.assertIn("import warpzone.cli", steps[0])
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
                self.assertTrue(rig.calls()[-1].endswith("@ 0.1.0"), rig.calls())

    def test_a_release_that_hangs_is_cut_off_and_marked_bad(self) -> None:
        rig = self.rig
        rig.release("0.2.0", "hang")
        self.assertTick(rig.tick(WZ_STEP_TIMEOUT="2"), 1)
        self.assertEqual(rig.bad(), ["v0.2.0"])
        self.assertEqual(rig.on_tag(), "v0.1.0")

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
