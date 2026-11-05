"""End to end: `updater.sh tick` with the real warpzone against two real Syncthing instances.

`test_updater_sh.py` covers the updater's rules with a fake Python that exits as told. What it
cannot show is what the verification of a real release answers on a device, and that is what
the differential verdict reads (development/design/autonomous-updates.md): `provision device
--unattended` of the version known to work must pass against a real Syncthing for a broken
release to be marked bad, a device whose Syncthing is down must fail for both versions so that
nothing is blamed, and going back from a retired release must leave the device's Syncthing as
the older release sets it up. The releases are commits of a throwaway repository holding this
checkout's `warpzone` package, one of them with a deliberate bug, so nothing here needs the
history or the tags of the real repository.

Skipped unless WARPZONE_E2E_SYNCTHING points at a Syncthing binary.
"""

import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from collections.abc import Callable
from pathlib import Path

from test_e2e import BINARY, SYNC_TIMEOUT, Instance, library_file, wait_for

REPO = Path(__file__).resolve().parent.parent
UPDATER = REPO / "warpzone" / "data" / "updater.sh"
DAY = 86400

GIT_ENV = {
    "GIT_CONFIG_GLOBAL": os.devnull,
    "GIT_CONFIG_SYSTEM": os.devnull,
    "GIT_AUTHOR_NAME": "Test",
    "GIT_AUTHOR_EMAIL": "test@example.invalid",
    "GIT_COMMITTER_NAME": "Test",
    "GIT_COMMITTER_EMAIL": "test@example.invalid",
    "GIT_TERMINAL_PROMPT": "0",
}

# A release whose REST client sends the API key under the wrong header: every request to a
# real Syncthing is refused (403), so its `provision device` fails. A fake Syncthing that does
# not check the key would let it pass.
BREAK_API_KEY = (
    "warpzone/syncthing.py",
    'headers = {"X-API-Key": self._api_key}',
    'headers = {"X-API-Secret": self._api_key}',
)
# A release whose device whitelist starts with `*`: the device ignores its whole library and
# receives nothing new. Syncthing takes the patterns and `health` does not look at them, so the
# release passes its verification; withdrawing it is what retirement is for.
BREAK_WHITELIST = (
    "warpzone/ignores.py",
    "return (*excluded, include(device_file(device)))",
    'return ("*", *excluded, include(device_file(device)))',
)


def git(cwd: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", "-C", str(cwd), *args],
        capture_output=True,
        text=True,
        env={**os.environ, **GIT_ENV},
        check=False,
    )
    if proc.returncode != 0:
        raise AssertionError(f"git {' '.join(args)} failed: {proc.stderr}")
    return proc.stdout.strip()


class Upstream:
    """A bare `origin` and a work tree that publishes this checkout's package as releases."""

    def __init__(self, base: Path) -> None:
        self.origin = base / "origin.git"
        self.work = base / "work"
        for args in (
            ["git", "init", "-q", "--bare", "-b", "main", str(self.origin)],
            ["git", "init", "-q", "-b", "main", str(self.work)],
        ):
            subprocess.run(args, check=True, env={**os.environ, **GIT_ENV})
        git(self.work, "remote", "add", "origin", str(self.origin))

    def release(self, version: str, bug: tuple[str, str, str] | None = None) -> None:
        """Commit the package as `version`, with `bug` (file, text, replacement) applied, tag
        it `v<version>` and publish it. Each release starts again from this checkout's files."""
        package = self.work / "warpzone"
        shutil.rmtree(package, ignore_errors=True)
        shutil.copytree(
            REPO / "warpzone", package, ignore=shutil.ignore_patterns("__pycache__", "*.pyc")
        )
        init = package / "__init__.py"
        text = init.read_text()
        start = text.index('__version__ = "')
        end = text.index('"', start + len('__version__ = "'))
        init.write_text(f'{text[:start]}__version__ = "{version}"{text[end + 1 :]}')
        # These releases are not signed: with no key in the version known to work, the updater
        # asks for no signature (the real file holds the maintainer's key).
        (package / "data" / "allowed_signers").write_text("# no keys in this test\n")
        if bug is not None:
            name, old, new = bug
            path = self.work / name
            source = path.read_text()
            if source.count(old) != 1:
                raise AssertionError(f"{name} no longer holds {old!r}: update the test's bug")
            path.write_text(source.replace(old, new))
        git(self.work, "add", "-A")
        git(self.work, "commit", "-q", "-m", f"Release {version}")
        git(self.work, "tag", f"v{version}")
        git(self.work, "push", "-q", "origin", "main", f"refs/tags/v{version}")

    def retire(self, tag: str, reason: str) -> None:
        """What the maintainer publishes to withdraw a release: an annotated `retired/<tag>`
        on its commit, whose message is the reason."""
        git(self.work, "tag", "-a", "-m", reason, f"retired/{tag}", f"{tag}^{{commit}}")
        git(self.work, "push", "-q", "origin", f"refs/tags/retired/{tag}")


@unittest.skipUnless(BINARY, "set WARPZONE_E2E_SYNCTHING to a syncthing binary to run")
class UpdaterEndToEndTests(unittest.TestCase):
    maxDiff = None

    def setUp(self) -> None:
        if not shutil.which("git"):
            self.skipTest("git is not installed")
        self.base = Path(tempfile.mkdtemp(prefix="wz-e2eu-"))
        self.addCleanup(shutil.rmtree, self.base, True)
        self.hub = Instance("hub", self.base)
        self.deck = Instance("deck", self.base)
        self.addCleanup(self.hub.stop)
        self.addCleanup(self.deck.stop)
        self.output: list[str] = []
        self.home = self.base / "home"
        self.emulation = self.home / "Emulation"
        self.library = self.base / "hub-root" / "library"
        self.clone = self.base / "device" / "warpzone"
        self.state_dir = self.base / "updater-state"
        self.live = self.base / "live" / "updater.sh"
        self.proc = self.base / "proc"  # no process in it: the menu is closed
        self.boot_id = self.base / "boot_id"
        self.now = int(time.time())

    def dump_logs(self) -> None:
        for inst in (self.hub, self.deck):
            if inst.log.exists():
                tail = inst.log.read_text(errors="replace")[-4000:]
                print(f"\n--- {inst.name} syncthing log (tail) ---\n{tail}", file=sys.stderr)
        print("\n--- commands ---\n" + "\n".join(self.output), file=sys.stderr)

    def scenario(self, body: Callable[[], None]) -> None:
        try:
            body()
        except BaseException:
            self.dump_logs()
            raise
        if os.environ.get("WARPZONE_E2E_VERBOSE"):
            self.dump_logs()

    # --- the device and its updater -----------------------------------------------------

    def env(self) -> dict[str, str]:
        env = {
            **os.environ,
            **GIT_ENV,
            "HOME": str(self.home),
            "STHOMEDIR": str(self.deck.home),  # where warpzone finds the device's config.xml
            "WZ_STATE_DIR": str(self.state_dir),
            "WZ_LOG": str(self.base / "self-update.log"),
            "WZ_LIVE": str(self.live),
            "WZ_PROC": str(self.proc),
            "WZ_BOOT_ID_FILE": str(self.boot_id),
            "WZ_NOW": str(self.now),
            "WZ_FETCH_INTERVAL": "0",
            "WZ_PYTHON_FALLBACKS": sys.executable,
        }
        for name in ("PYTHONPATH", "PYTHONHOME", "XDG_STATE_HOME", "XDG_CONFIG_HOME"):
            env.pop(name, None)
        return env

    def sh(self, *args: str) -> subprocess.CompletedProcess:
        proc = subprocess.run(
            ["sh", *args],
            capture_output=True,
            text=True,
            env=self.env(),
            cwd=self.base,
            stdin=subprocess.DEVNULL,
            timeout=SYNC_TIMEOUT + 300,
            check=False,
        )
        verify_log = self.state_dir / "last-verify.log"
        detail = verify_log.read_text(errors="replace") if verify_log.exists() else ""
        proc.text = (  # type: ignore[attr-defined]
            f"$ sh {' '.join(args)}  (now {self.now})\n{proc.stdout}{proc.stderr}"
            f"[exit {proc.returncode}]\n--- last-verify.log ---\n{detail}"
        )
        self.output.append(proc.text)  # type: ignore[attr-defined]
        return proc

    def tick(self, code: int) -> str:
        proc = self.sh(str(self.live), "tick")
        self.assertEqual(proc.returncode, code, proc.text)  # type: ignore[attr-defined]
        return proc.stdout

    def on_tag(self) -> str:
        return git(self.clone, "describe", "--exact-match", "--tags", "--match", "v[0-9]*")

    def state(self) -> dict[str, str]:
        lines = (self.state_dir / "state").read_text().splitlines()
        return dict(line.split("=", 1) for line in lines if "=" in line)

    def bad(self) -> list[str]:
        folder = self.state_dir / "bad"
        return sorted(p.name for p in folder.iterdir()) if folder.is_dir() else []

    def device_ignores(self) -> list[str]:
        return self.deck.client().get_ignores("wz-library")

    def install(self) -> Upstream:
        """A hub with a library, and a device installed from a clone on release 1.0.0 the way
        a person does it (`updater.sh install`), its updater settings written."""
        self.hub.start()
        self.deck.start()
        hub_id, deck_id = self.hub.client().my_id(), self.deck.client().my_id()
        for which, source in (("common", "patterns-common"), ("deck", "patterns-device.example")):
            shutil.copy(
                REPO / "templates" / f"{source}.stignore", library_file(self.library, which)
            )
        (self.library / "roms" / "nes").mkdir(parents=True)
        (self.library / "roms" / "nes" / "mario.nes").write_text("nes rom")
        self.emulation.mkdir(parents=True)
        self.config = self.base / "config.toml"
        self.config.write_text(
            f"""
[hub]
name = "nas"
device_id = "{hub_id}"
root = "{self.library.parent}"
api_url = "{self.hub.url}"
api_key = "{self.hub.api_key}"
addresses = ["{self.hub.address}"]

[devices.deck]
device_id = "{deck_id}"
profile = "steamos-emudeck"
folders = ["library"]
addresses = ["{self.deck.address}"]
rescan_seconds = 10
# The updater's settings are written by the test, and no systemd timer is installed.
self_update = false
"""
        )
        hub = subprocess.run(
            [sys.executable, "-m", "warpzone", "provision", "hub", "--config", str(self.config)],
            capture_output=True,
            text=True,
            env={**os.environ, "HOME": str(self.home), "PYTHONPATH": str(REPO)},
            cwd=self.base,
            timeout=SYNC_TIMEOUT + 60,
            check=False,
        )
        self.output.append(f"$ warpzone provision hub\n{hub.stdout}{hub.stderr}")
        self.assertEqual(hub.returncode, 0, hub.stdout + hub.stderr)

        upstream = Upstream(self.base)
        upstream.release("1.0.0")
        self.clone.parent.mkdir(parents=True)
        subprocess.run(
            ["git", "clone", "-q", str(upstream.origin), str(self.clone)],
            check=True,
            env={**os.environ, **GIT_ENV},
        )
        self.proc.mkdir()
        self.boot_id.write_text("boot-1\n")
        self.live.parent.mkdir(parents=True)
        shutil.copy(UPDATER, self.live)  # the live copy the timer runs: this checkout's script

        script = self.clone / "warpzone" / "data" / "updater.sh"
        installed = self.sh(str(script), "install", "deck", "--config", str(self.config))
        self.assertEqual(installed.returncode, 0, installed.text)  # type: ignore[attr-defined]
        self.assertEqual(self.on_tag(), "v1.0.0")
        wait_for(
            lambda: (self.emulation / "roms/nes/mario.nes").exists(), "the library on the device"
        )
        self.final = self.device_ignores()
        self.assertEqual(self.final[-1], "#include patterns-deck.stignore")

        self.state_dir.mkdir(exist_ok=True)
        python = self.clone.parent / "venv" / "bin" / "python"
        (self.state_dir / "conf").write_text(
            f"device=deck\npython={python}\nclone={self.clone}\nconfig={self.config}\n"
            "enabled=true\n"
        )
        # The first tick takes the checkout as the version known to work and provisions once.
        self.tick(0)
        self.assertEqual(self.state()["good_tag"], "v1.0.0")
        return upstream

    # --- the tests ----------------------------------------------------------------------

    def test_a_release_that_breaks_against_syncthing_is_bad_and_a_stopped_one_is_not(
        self,
    ) -> None:
        self.scenario(self.bad_release_scenario)

    def test_a_retired_release_is_left_and_the_older_one_sets_the_device_up_again(
        self,
    ) -> None:
        self.scenario(self.retired_scenario)

    def bad_release_scenario(self) -> None:
        upstream = self.install()

        # 1.1.0 cannot talk to Syncthing. The version known to work, checked again against the
        # same Syncthing, passes: the release is bad, and the device stays as it was.
        upstream.release("1.1.0", BREAK_API_KEY)
        self.now += 600
        self.tick(1)
        self.assertEqual(self.on_tag(), "v1.0.0")
        self.assertEqual(self.bad(), ["v1.1.0"])
        self.assertIn(
            "provision device --unattended failed", (self.state_dir / "bad/v1.1.0").read_text()
        )
        state = self.state()
        self.assertEqual(state["last_result"], "rolled-back")
        self.assertEqual(state["good_tag"], "v1.0.0")
        self.assertEqual(state["env_failures"], "0")
        self.assertEqual(self.device_ignores(), self.final)

        # The device's Syncthing is down when a good release comes out: both versions fail, so
        # the environment is at fault. Nothing is marked bad and the device waits a day.
        upstream.release("1.2.0")
        self.deck.stop()
        self.now += 600
        self.tick(1)
        self.assertEqual(self.on_tag(), "v1.0.0")
        self.assertEqual(self.bad(), ["v1.1.0"])
        state = self.state()
        self.assertEqual(state["last_result"], "env")
        self.assertEqual(state["env_failures"], "1")
        self.assertEqual(state["retry_after"], str(self.now + DAY))

        # Syncthing back, and the day gone by: the release that was not blamed is installed.
        self.deck.serve()
        wait_for(lambda: self.deck.client().my_id() != "", "the device's Syncthing again", 60)
        self.now += DAY
        self.tick(0)
        self.assertEqual(self.on_tag(), "v1.2.0")
        state = self.state()
        self.assertEqual(state["good_tag"], "v1.2.0")
        self.assertEqual(state["env_failures"], "0")
        self.assertEqual(self.bad(), ["v1.1.0"])

    def retired_scenario(self) -> None:
        upstream = self.install()

        # 1.1.0 passes its checks and is installed, and its whitelist hides the library.
        upstream.release("1.1.0", BREAK_WHITELIST)
        self.now += 600
        self.tick(0)
        self.assertEqual(self.on_tag(), "v1.1.0")
        self.assertEqual(self.state()["good_tag"], "v1.1.0")
        self.assertEqual(self.device_ignores()[0], "*")

        # A new game on the hub, and the maintainer withdraws 1.1.0. The device goes down to
        # 1.0.0, whose provision puts its own ignores back on the device's Syncthing, so the
        # game arrives. Retired is not bad.
        (self.library / "roms/nes/zelda.nes").write_text("another nes rom")
        self.hub.client().scan("wz-library")
        upstream.retire("v1.1.0", "it hides the library on every device")
        self.now += 600
        out = self.tick(0)
        self.assertIn("it hides the library on every device", out)
        self.assertEqual(self.on_tag(), "v1.0.0")
        self.assertEqual(self.state()["good_tag"], "v1.0.0")
        self.assertEqual(self.bad(), [])
        self.assertEqual(self.device_ignores(), self.final)
        wait_for(
            lambda: (self.emulation / "roms/nes/zelda.nes").exists(),
            "the new game on the device once 1.1.0 is left",
        )
        self.assertEqual((self.emulation / "roms/nes/zelda.nes").read_text(), "another nes rom")


if __name__ == "__main__":
    unittest.main()
