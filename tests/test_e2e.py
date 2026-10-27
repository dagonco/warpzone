"""End to end: two real Syncthing instances, driven only through the warpzone CLI.

Skipped unless WARPZONE_E2E_SYNCTHING points at a Syncthing binary.
"""

import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import unittest
import xml.etree.ElementTree as ET
from collections.abc import Callable
from pathlib import Path

from support import API_KEY
from warpzone.syncthing import Client

REPO = Path(__file__).resolve().parent.parent
BINARY = os.environ.get("WARPZONE_E2E_SYNCTHING", "")
SYNC_TIMEOUT = 180.0


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def wait_for(check: Callable[[], bool], what: str, timeout: float = SYNC_TIMEOUT) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            if check():
                return
        except Exception:
            pass
        time.sleep(0.5)
    raise AssertionError(f"timed out after {timeout:.0f}s waiting for {what}")


class Instance:
    """One Syncthing process with its own home, ports and log."""

    def __init__(self, name: str, base: Path) -> None:
        self.name = name
        self.home = base / f"{name}-st"
        self.log = base / f"{name}.log"
        self.gui_port, self.sync_port = free_port(), free_port()
        self.api_key = f"{name}-{API_KEY}"
        self.proc: subprocess.Popen[bytes] | None = None
        self._log_file = None
        self.env = {
            **os.environ,
            "STNOUPGRADE": "1",
            "STNORESTART": "1",
            "STNOBROWSER": "1",
        }

    @property
    def config_xml(self) -> Path:
        return self.home / "config.xml"

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.gui_port}"

    @property
    def address(self) -> str:
        return f"tcp://127.0.0.1:{self.sync_port}"

    def client(self) -> Client:
        return Client(self.url, self.api_key, label=self.name, timeout=10)

    def start(self) -> None:
        subprocess.run(
            [BINARY, "generate", f"--home={self.home}"],
            check=True,
            capture_output=True,
            env=self.env,
        )
        tree = ET.parse(self.config_xml)
        root = tree.getroot()
        gui = root.find("gui")
        assert gui is not None
        gui.set("tls", "false")
        gui.find("address").text = f"127.0.0.1:{self.gui_port}"  # type: ignore[union-attr]
        gui.find("apikey").text = self.api_key  # type: ignore[union-attr]
        options = root.find("options")
        assert options is not None
        # Strictly local: no discovery, relays, NAT traversal, telemetry or upgrades.
        for tag in ("globalAnnounceEnabled", "localAnnounceEnabled", "relaysEnabled", "natEnabled"):
            options.find(tag).text = "false"  # type: ignore[union-attr]
        options.find("urAccepted").text = "-1"  # type: ignore[union-attr]
        options.find("crashReportingEnabled").text = "false"  # type: ignore[union-attr]
        listens = options.findall("listenAddress")
        listens[0].text = self.address
        for extra in listens[1:]:
            options.remove(extra)
        tree.write(self.config_xml, encoding="utf-8", xml_declaration=True)

        self._log_file = self.log.open("wb")
        self.proc = subprocess.Popen(
            [BINARY, "serve", "--no-browser", f"--home={self.home}"],
            stdout=self._log_file,
            stderr=subprocess.STDOUT,
            env=self.env,
        )
        wait_for(lambda: self.client().my_id() != "", f"{self.name} API", 60)

    def stop(self) -> None:
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=30)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait()
        if self._log_file:
            self._log_file.close()


@unittest.skipUnless(BINARY, "set WARPZONE_E2E_SYNCTHING to a syncthing binary to run")
class EndToEndTests(unittest.TestCase):
    def setUp(self) -> None:
        # Short path: some platforms limit socket and database path lengths.
        self.base = Path(tempfile.mkdtemp(prefix="wz-e2e-"))
        self.addCleanup(shutil.rmtree, self.base, True)
        self.hub = Instance("hub", self.base)
        self.deck = Instance("deck", self.base)
        self.addCleanup(self.hub.stop)
        self.addCleanup(self.deck.stop)
        self.output: list[str] = []

    def dump_logs(self) -> None:
        for inst in (self.hub, self.deck):
            if inst.log.exists():
                tail = inst.log.read_text(errors="replace")[-6000:]
                print(f"\n--- {inst.name} syncthing log (tail) ---\n{tail}", file=sys.stderr)
        print("\n--- warpzone output ---\n" + "\n".join(self.output), file=sys.stderr)

    def wz(self, *args: str, expect: int = 0) -> str:
        env = {**os.environ, "HOME": str(self.home), "PYTHONPATH": str(REPO)}
        proc = subprocess.run(
            [sys.executable, "-m", "warpzone", *args],
            capture_output=True,
            text=True,
            env=env,
            cwd=self.base,
            timeout=SYNC_TIMEOUT + 60,
        )
        text = f"$ warpzone {' '.join(args)}\n{proc.stdout}{proc.stderr}[exit {proc.returncode}]"
        self.output.append(text)
        self.assertEqual(proc.returncode, expect, text)
        return proc.stdout

    def write(self, path: Path, content: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)

    def test_library_and_saves(self) -> None:
        try:
            self.scenario()
        except BaseException:
            self.dump_logs()
            raise
        if os.environ.get("WARPZONE_E2E_VERBOSE"):
            self.dump_logs()

    def scenario(self) -> None:
        self.hub.start()
        self.deck.start()
        hub_id, deck_id = self.hub.client().my_id(), self.deck.client().my_id()

        # Hub content: one whitelisted system, one whitelisted single game, two others.
        root = self.base / "hub-root"
        library = root / "library"
        shutil.copy(
            REPO / "templates" / "patterns-common.stignore", library_file(library, "common")
        )
        shutil.copy(
            REPO / "templates" / "patterns-device.example.stignore", library_file(library, "deck")
        )
        self.write(library / "roms/nes/mario.nes", "nes rom")
        self.write(library / "roms/psx/Some Game (USA)/game.cue", "cue")
        self.write(library / "roms/n64/other.z64", "not whitelisted")
        self.write(library / "roms/n64/sub/deep.z64", "nested")
        self.write(library / "roms/psx/Other Game (USA)/other.cue", "not whitelisted")
        # BIOS: a plain file travels with the library; Cemu's keys.txt only through
        # its placement (the library ignores bios/cemu on both sides).
        self.write(library / "bios/psx/scph-fake.bin", "plain bios")
        self.write(library / "bios/cemu/keys.txt", "fake keys, not real")
        self.write(library / "bios/cemu/other.txt", "never delivered")
        self.write(library / "bios/ryujinx/keys/prod.keys", "fake keys, not real")

        self.home = self.base / "home"
        saves = self.home / ".var/app/org.libretro.RetroArch/config/retroarch/saves"
        saves.mkdir(parents=True)
        cemu = self.home / ".local/share/Cemu"
        cemu.mkdir(parents=True)
        # EmuDeck's symlink where the hub has a real folder: Syncthing must leave it be.
        emulation = self.home / "Emulation"
        keys_target = self.home / ".config/Ryujinx/system"
        keys_target.mkdir(parents=True)
        (emulation / "bios/ryujinx").mkdir(parents=True)
        (emulation / "bios/ryujinx/keys").symlink_to(keys_target)
        config = self.base / "config.toml"
        config.write_text(
            f"""
[hub]
name = "nas"
device_id = "{hub_id}"
root = "{root}"
api_url = "{self.hub.url}"
api_key = "{self.hub.api_key}"
addresses = ["{self.hub.address}"]

[devices.deck]
device_id = "{deck_id}"
profile = "steamos-emudeck"
folders = ["library", "saves", "bios"]
addresses = ["{self.deck.address}"]
rescan_seconds = 10
"""
        )
        cfg = ["--config", str(config)]
        device_args = [*cfg, "--syncthing-config", str(self.deck.config_xml), "--timeout", "150"]

        self.wz("provision", "hub", "--dry-run", *cfg)
        self.assertEqual(self.hub.client().folders(), [])  # dry-run wrote nothing
        self.wz("provision", "hub", *cfg)
        self.wz("provision", "hub", *cfg)  # idempotent

        # The device refuses to start until the hub has scanned the whitelists.
        wait_for(lambda: self.wz_ok("check", *cfg), "hub to scan its whitelists", 120)

        self.wz("provision", "device", "deck", *device_args, "--dry-run")
        self.assertEqual(self.deck.client().folders(), [])
        out = self.wz("provision", "device", "deck", *device_args)
        self.assertIn("whitelist active", out)
        out = self.wz("provision", "device", "deck", *device_args)  # idempotent
        self.assertIn("wz-library: ignores already final", out)

        stignore = (emulation / ".stignore").read_text().splitlines()
        self.assertEqual(stignore[-1], "#include patterns-deck.stignore")
        for excluded in ("/roms/wiiu/mlc01", "/bios/ryujinx/keys", "/bios/cemu"):
            self.assertIn(excluded, stignore)

        # Whitelisted content arrives; the rest does not.
        wait_for(
            lambda: (emulation / "roms/nes/mario.nes").exists(), "whitelisted ROM on the device"
        )
        wait_for(
            lambda: (emulation / "roms/psx/Some Game (USA)/game.cue").exists(),
            "whitelisted single game on the device",
        )
        wait_for(lambda: self.settled(self.deck, "wz-library"), "device library to settle")
        self.assertFalse((emulation / "roms/n64").exists())
        self.assertFalse((emulation / "roms/psx/Other Game (USA)").exists())
        self.assertEqual((emulation / "roms/nes/mario.nes").read_text(), "nes rom")
        self.bios_scenario(emulation, cemu, keys_target)

        # Editing the whitelist on the hub changes what the device receives, with
        # no command run on the device. Syncthing applies a changed whitelist only
        # on a full folder scan, and whether the filesystem watcher causes one
        # differs per OS (that made this step flaky on Linux CI). Switching the
        # watcher off makes the scheduled rescan the only trigger, so this pins
        # the behaviour warpzone guarantees: the device applies the edit within
        # rescan_seconds.
        self.deck.client().patch_folder("wz-library", {"fsWatcherEnabled": False})
        whitelist = library_file(library, "deck")
        whitelist.write_text(whitelist.read_text().replace("*\n", "!/roms/n64\n*\n"))
        wait_for(
            lambda: (emulation / "roms/n64/other.z64").exists(),
            "newly whitelisted system to reach the device",
        )
        self.assertFalse((emulation / "roms/psx/Other Game (USA)").exists())
        wait_for(
            lambda: (emulation / "roms/n64/sub/deep.z64").exists(), "nested file on the device"
        )
        self.prune_scenario(cfg, library, emulation)

        # A save created on the device reaches the hub.
        self.write(saves / "slot1.srm", "save data")
        hub_save = root / "saves/retroarch/saves/slot1.srm"
        wait_for(hub_save.exists, "save to reach the hub")
        self.assertEqual(hub_save.read_text(), "save data")

        self.wz("status", *cfg)

    def bios_scenario(self, emulation: Path, cemu: Path, keys_target: Path) -> None:
        """A placement delivers into the emulator's own folder, the library stays out of it."""
        wait_for(
            lambda: (emulation / "bios/psx/scph-fake.bin").exists(),
            "a plain BIOS file to arrive with the library",
        )
        wait_for(lambda: (cemu / "keys.txt").exists(), "keys.txt in the Cemu folder")
        self.assertEqual((cemu / "keys.txt").read_text(), "fake keys, not real")
        wait_for(lambda: self.settled(self.deck, "wz-bios-cemu-keys"), "the placement to settle")
        self.assertFalse((cemu / "other.txt").exists())  # only the listed file
        self.assertFalse((emulation / "bios/cemu").exists())  # not through the library
        # The symlink survived and nothing was written through it.
        self.assertTrue((emulation / "bios/ryujinx/keys").is_symlink())
        self.assertEqual(list(keys_target.iterdir()), [])
        # Eden's folder does not exist here, so its placements were skipped.
        self.assertNotIn("wz-bios-eden-keys", {f["id"] for f in self.deck.client().folders()})

    def prune_scenario(self, cfg: list[str], library: Path, emulation: Path) -> None:
        """Take n64 off the whitelist again: its files stay on the device until pruned."""
        n64 = emulation / "roms/n64"
        wait_for(lambda: self.settled(self.deck, "wz-library"), "device library to settle")
        # Only on the device, and recorded by Syncthing before it becomes ignored.
        self.write(n64 / "mine.txt", "device only")
        self.deck.client().scan("wz-library")
        wait_for(
            lambda: self.deck.client().db_file("wz-library", "roms/n64/mine.txt") is not None,
            "Syncthing to record the device-only file",
        )

        whitelist = library_file(library, "deck")
        whitelist.write_text(whitelist.read_text().replace("!/roms/n64\n", ""))
        wait_for(
            lambda: "!/roms/n64" not in (emulation / "patterns-deck.stignore").read_text(),
            "the shorter whitelist to reach the device",
        )
        # prune does not wait for rescan_seconds: it asks for a full rescan itself.
        args = [*cfg, "--syncthing-config", str(self.deck.config_xml)]

        out = self.wz("prune", "deck", *args)
        self.assertIn("wz-library: 2 leftover file(s)", out)
        self.assertIn("roms/n64/mine.txt (the hub does not have it)", out)
        self.assertIn("report only", out)
        self.assertTrue((n64 / "other.z64").exists())  # report only

        # The cap holds even with --apply.
        self.wz("prune", "--apply", "--max-gib", "0", *args, expect=1)
        self.assertTrue((n64 / "other.z64").exists())

        out = self.wz("prune", "--apply", *args)  # also: the device name is inferred
        self.assertIn("deleted 2 file(s)", out)
        self.assertFalse((n64 / "other.z64").exists())
        self.assertFalse((n64 / "sub").exists())  # left empty, so removed
        self.assertEqual((n64 / "mine.txt").read_text(), "device only")
        self.assertEqual((emulation / "roms/nes/mario.nes").read_text(), "nes rom")
        self.assertEqual((emulation / "roms/psx/Some Game (USA)/game.cue").read_text(), "cue")
        for protected in ("patterns-common.stignore", "patterns-deck.stignore", ".stignore"):
            self.assertTrue((emulation / protected).is_file(), protected)
        self.assertTrue((emulation / ".stfolder").is_dir())

        # Nothing left to do, and the hub still has its files.
        self.assertIn("0 leftover file(s)", self.wz("prune", "--apply", *args))
        self.assertTrue((library / "roms/n64/other.z64").exists())

    def wz_ok(self, *args: str) -> bool:
        env = {**os.environ, "HOME": str(self.home), "PYTHONPATH": str(REPO)}
        proc = subprocess.run(
            [sys.executable, "-m", "warpzone", *args],
            capture_output=True,
            text=True,
            env=env,
            timeout=60,
        )
        self.output.append(f"$ warpzone {' '.join(args)}\n{proc.stdout}{proc.stderr}")
        return proc.returncode == 0

    @staticmethod
    def settled(inst: Instance, folder: str) -> bool:
        st = inst.client().db_status(folder)
        need = st.get("needTotalItems", st.get("needFiles", 0))
        return st.get("state") == "idle" and need == 0 and st.get("globalFiles", 0) > 0


def library_file(library: Path, which: str) -> Path:
    library.mkdir(parents=True, exist_ok=True)
    return library / f"patterns-{which}.stignore"


if __name__ == "__main__":
    unittest.main()
