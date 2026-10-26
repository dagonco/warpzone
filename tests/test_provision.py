import tempfile
import unittest
from pathlib import Path

from support import API_KEY, DECK_ID, HUB_ID, MACHINE_ID, FakeSyncthing, make_config
from warpzone.profiles import load_profile
from warpzone.provision import (
    ProvisionError,
    Reporter,
    check_hub,
    expand_home,
    folder_status,
    provision_device,
    provision_hub,
    wait_for_files,
)
from warpzone.syncthing import Client

PROFILES = {"steamos-emudeck": load_profile("steamos-emudeck")}


def reporter() -> tuple[Reporter, list[str]]:
    lines: list[str] = []
    return Reporter(lines.append), lines


class HubTests(unittest.TestCase):
    def test_creates_devices_and_folders(self) -> None:
        with FakeSyncthing(HUB_ID) as fake:
            cfg = make_config(fake.url)
            rep, _ = reporter()
            provision_hub(cfg, PROFILES, Client(fake.url, API_KEY), rep)

            ids = {d["deviceID"] for d in fake.devices}
            self.assertEqual(ids, {HUB_ID, DECK_ID, MACHINE_ID})
            library = fake.folders["wz-library"]
            self.assertEqual(library["path"], "/data/games/library")
            self.assertEqual(library["type"], "sendonly")
            self.assertFalse(library["paused"])  # created paused, then resumed
            self.assertEqual(fake.ignores["wz-library"], ["#include patterns-common.stignore"])
            self.assertEqual(fake.ignores["wz-tools"][-1], "(?d)Thumbs.db")
            self.assertIn("wz-save-ryujinx-user", fake.folders)
            self.assertEqual(fake.ignores["wz-save-ryujinx-user"][-1], "*")
            self.assertEqual(fake.folders["wz-songs"]["devices"], [{"deviceID": MACHINE_ID}])
            # New folders: created paused with ignores set before they start.
            order = [
                (m, p.split("?")[0])
                for m, p, _ in fake.writes()
                if "wz-songs" in p or (p == "/rest/config/folders" and _["id"] == "wz-songs")
            ]
            self.assertEqual(
                order,
                [
                    ("POST", "/rest/config/folders"),
                    ("POST", "/rest/db/ignores"),
                    ("PATCH", "/rest/config/folders/wz-songs"),
                ],
            )
            created = [b for m, p, b in fake.writes() if p == "/rest/config/folders"]
            self.assertTrue(all(b["paused"] for b in created))
            self.assertTrue(any("wz-library" in w for w in rep.warnings))

    def test_second_run_writes_nothing(self) -> None:
        with FakeSyncthing(HUB_ID) as fake:
            cfg = make_config(fake.url)
            provision_hub(cfg, PROFILES, Client(fake.url, API_KEY), reporter()[0])
            fake.browse = {
                fid: [
                    "patterns-common.stignore",
                    "patterns-deck.stignore",
                    "patterns-machine.stignore",
                ]
                for fid in fake.folders
            }
            fake.requests.clear()
            rep, _ = reporter()
            provision_hub(cfg, PROFILES, Client(fake.url, API_KEY), rep)
            self.assertEqual(fake.writes(), [])
            self.assertEqual(rep.warnings, [])

    def test_hub_id_mismatch(self) -> None:
        with FakeSyncthing(DECK_ID) as fake:
            with self.assertRaises(ProvisionError) as ctx:
                provision_hub(
                    make_config(fake.url), PROFILES, Client(fake.url, API_KEY), reporter()[0]
                )
            self.assertIn("hub.device_id", str(ctx.exception))
            self.assertEqual(fake.writes(), [])

    def test_updates_only_owned_keys_and_never_removes_devices(self) -> None:
        with FakeSyncthing(HUB_ID) as fake:
            cfg = make_config(fake.url)
            provision_hub(cfg, PROFILES, Client(fake.url, API_KEY), reporter()[0])
            other = {"deviceID": "OTHER-ID"}
            fake.folders["wz-tools"].update(
                label="renamed", rescanIntervalS=99, devices=[other, {"deviceID": DECK_ID}]
            )
            fake.requests.clear()
            provision_hub(cfg, PROFILES, Client(fake.url, API_KEY), reporter()[0])
            patches = [(p, b) for m, p, b in fake.writes() if m == "PATCH"]
            self.assertEqual(patches, [("/rest/config/folders/wz-tools", {"label": "wz-tools"})])
            self.assertEqual(fake.folders["wz-tools"]["rescanIntervalS"], 99)

            fake.folders["wz-tools"]["devices"] = [other]
            fake.requests.clear()
            provision_hub(cfg, PROFILES, Client(fake.url, API_KEY), reporter()[0])
            sent = next(b for m, p, b in fake.writes() if m == "PATCH")["devices"]
            self.assertEqual([d["deviceID"] for d in sent], ["OTHER-ID", DECK_ID])

    def test_foreign_folders_are_never_touched(self) -> None:
        with FakeSyncthing(HUB_ID) as fake:
            fake.folders["photos"] = {"id": "photos", "path": "/p", "type": "sendreceive"}
            fake.ignores["photos"] = ["x"]
            provision_hub(make_config(fake.url), PROFILES, Client(fake.url, API_KEY), reporter()[0])
            self.assertEqual(
                fake.folders["photos"], {"id": "photos", "path": "/p", "type": "sendreceive"}
            )
            self.assertFalse([w for w in fake.writes() if "photos" in w[1]])

    def test_path_change_is_refused(self) -> None:
        with FakeSyncthing(HUB_ID) as fake:
            cfg = make_config(fake.url)
            provision_hub(cfg, PROFILES, Client(fake.url, API_KEY), reporter()[0])
            fake.folders["wz-tools"]["path"] = "/somewhere/else"
            fake.requests.clear()
            with self.assertRaises(ProvisionError) as ctx:
                provision_hub(cfg, PROFILES, Client(fake.url, API_KEY), reporter()[0])
            self.assertIn("mass delete", str(ctx.exception))
            self.assertEqual(fake.folders["wz-tools"]["path"], "/somewhere/else")

    def test_trailing_slash_is_not_a_path_change(self) -> None:
        with FakeSyncthing(HUB_ID) as fake:
            cfg = make_config(fake.url)
            provision_hub(cfg, PROFILES, Client(fake.url, API_KEY), reporter()[0])
            fake.folders["wz-tools"]["path"] += "/"
            provision_hub(cfg, PROFILES, Client(fake.url, API_KEY), reporter()[0])

    def test_dry_run_makes_no_writes(self) -> None:
        with FakeSyncthing(HUB_ID) as fake:
            lines: list[str] = []
            client = Client(fake.url, API_KEY, label="hub", dry_run=True, sink=lines.append)
            provision_hub(make_config(fake.url), PROFILES, client, reporter()[0])
            self.assertEqual(fake.writes(), [])
            self.assertEqual(fake.folders, {})
            self.assertTrue(any("POST /rest/config/folders" in line for line in lines))
            self.assertTrue(
                any(line.startswith("[dry-run] hub: POST /rest/config/devices") for line in lines)
            )
            self.assertNotIn(API_KEY, "\n".join(lines))

    def test_check_reports_missing_whitelists(self) -> None:
        with FakeSyncthing(HUB_ID) as fake:
            cfg = make_config(fake.url)
            client = Client(fake.url, API_KEY)
            rep, lines = reporter()
            self.assertGreater(check_hub(cfg, PROFILES, client, rep), 0)
            self.assertTrue(any("not configured on the hub" in line for line in lines))

            fake.browse = {
                "wz-library": ["patterns-common.stignore", "patterns-deck.stignore"],
                "wz-songs": ["patterns-common.stignore", "patterns-machine.stignore"],
            }
            rep, lines = reporter()
            self.assertEqual(check_hub(cfg, PROFILES, client, rep), 1)
            self.assertEqual(
                [line for line in lines if "MISSING" in line],
                ["wz-library: MISSING patterns-machine.stignore (in /data/games/library)"],
            )
            self.assertEqual(fake.writes(), [])

    def test_status(self) -> None:
        with FakeSyncthing(HUB_ID) as fake:
            provision_hub(make_config(fake.url), PROFILES, Client(fake.url, API_KEY), reporter()[0])
            fake.folders["wz-tools"]["paused"] = True
            rep, lines = reporter()
            folder_status(Client(fake.url, API_KEY), "hub", rep)
            self.assertIn("  wz-tools: paused", lines)
            self.assertIn("  wz-library: idle, 3 files, need 0", lines)


class WaitTests(unittest.TestCase):
    def test_returns_when_all_files_in_sync(self) -> None:
        with FakeSyncthing(DECK_ID) as fake:
            spec = _device_specs()["wz-library"]
            for name in spec.required_files:
                fake.arrives_after[("wz-library", name)] = 2
            sleeps: list[float] = []
            wait_for_files(
                Client(fake.url, API_KEY),
                spec,
                timeout=60,
                poll=1,
                rep=reporter()[0],
                sleep=sleeps.append,
            )
            self.assertEqual(len(sleeps), 2)

    def test_times_out_with_a_clear_error(self) -> None:
        with FakeSyncthing(DECK_ID) as fake:
            spec = _device_specs()["wz-library"]
            fake.arrives_after[("wz-library", "patterns-common.stignore")] = 0
            now = [0.0]

            def sleep(seconds: float) -> None:
                now[0] += seconds

            with self.assertRaises(ProvisionError) as ctx:
                wait_for_files(
                    Client(fake.url, API_KEY),
                    spec,
                    timeout=10,
                    poll=2,
                    rep=reporter()[0],
                    sleep=sleep,
                    clock=lambda: now[0],
                )
            self.assertIn("patterns-deck.stignore did not arrive within 10s", str(ctx.exception))
            self.assertNotIn("patterns-common", str(ctx.exception))


def _device_specs():
    from warpzone.model import device_specs

    return {s.id: s for s in device_specs(make_config(), "deck", PROFILES["steamos-emudeck"])}


class DeviceTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.home = Path(self._tmp.name)
        # One emulator that was opened once: its save directory exists.
        (self.home / ".var/app/org.libretro.RetroArch/config/retroarch/saves").mkdir(parents=True)

    def run_device(self, local_fake, hub_fake, *, timeout=60.0, dry_run=False, sleeps=None):
        cfg = make_config(hub_fake.url if hub_fake else "http://127.0.0.1:1")
        rep, lines = reporter()
        hub = Client(hub_fake.url, API_KEY, label="hub") if hub_fake else None
        local = Client(local_fake.url, API_KEY, label="local", dry_run=dry_run, sink=lines.append)
        provision_device(
            cfg,
            "deck",
            PROFILES["steamos-emudeck"],
            local,
            hub,
            rep,
            home=self.home,
            wait_timeout=timeout,
            poll=1.0,
            sleep=(sleeps.append if sleeps is not None else lambda s: None),
        )
        return rep, lines

    def hub_with_whitelists(self, hub: FakeSyncthing) -> None:
        hub.browse["wz-library"] = ["patterns-common.stignore", "patterns-deck.stignore"]

    def test_full_flow_with_bootstrap(self) -> None:
        with FakeSyncthing(DECK_ID) as local, FakeSyncthing(HUB_ID) as hub:
            self.hub_with_whitelists(hub)
            for name in ("patterns-common.stignore", "patterns-deck.stignore"):
                local.arrives_after[("wz-library", name)] = 3
            sleeps: list[float] = []
            rep, _ = self.run_device(local, hub, sleeps=sleeps)

            self.assertEqual(len(sleeps), 3)
            self.assertIn(HUB_ID, {d["deviceID"] for d in local.devices})
            library = local.folders["wz-library"]
            self.assertEqual(library["path"], str(self.home / "Emulation"))
            self.assertEqual(library["type"], "receiveonly")
            self.assertFalse(library["paused"])
            self.assertEqual(local.ignores["wz-library"], ["#include patterns-deck.stignore"])

            ignore_writes = [
                b["ignore"]
                for m, p, b in local.writes()
                if p.startswith("/rest/db/ignores?folder=wz-library")
            ]
            self.assertEqual(
                ignore_writes, [["!/patterns-*.stignore", "*"], ["#include patterns-deck.stignore"]]
            )
            wl = [
                (m, p.split("?")[0])
                for m, p, b in local.writes()
                if "wz-library" in p or (b and b.get("id") == "wz-library")
            ]
            self.assertEqual(
                wl,
                [
                    ("POST", "/rest/config/folders"),
                    ("POST", "/rest/db/ignores"),
                    ("PATCH", "/rest/config/folders/wz-library"),  # resume
                    ("POST", "/rest/db/ignores"),
                    ("POST", "/rest/db/scan"),
                ],
            )
            # Inline-ignore folders and the only existing save directory.
            self.assertIn("wz-tools", local.folders)
            self.assertIn("wz-save-retroarch-saves", local.folders)
            self.assertNotIn("wz-save-dolphin-gc", local.folders)
            self.assertNotIn("wz-songs", local.folders)
            self.assertEqual(sum("skipped" in w for w in rep.warnings), 17)

    def test_second_run_writes_nothing(self) -> None:
        with FakeSyncthing(DECK_ID) as local, FakeSyncthing(HUB_ID) as hub:
            self.hub_with_whitelists(hub)
            for name in ("patterns-common.stignore", "patterns-deck.stignore"):
                local.arrives_after[("wz-library", name)] = 0
            self.run_device(local, hub)
            local.requests.clear()
            self.run_device(local, hub)
            self.assertEqual(local.writes(), [])

    def test_missing_whitelist_on_hub_stops_before_any_write(self) -> None:
        with FakeSyncthing(DECK_ID) as local, FakeSyncthing(HUB_ID) as hub:
            hub.browse["wz-library"] = ["patterns-common.stignore"]
            with self.assertRaises(ProvisionError) as ctx:
                self.run_device(local, hub)
            self.assertIn("patterns-deck.stignore not found on the hub", str(ctx.exception))
            self.assertEqual(local.writes(), [])

    def test_folder_unknown_to_hub(self) -> None:
        with FakeSyncthing(DECK_ID) as local, FakeSyncthing(HUB_ID) as hub:
            with self.assertRaises(ProvisionError) as ctx:
                self.run_device(local, hub)
            self.assertIn("provision hub", str(ctx.exception))
            self.assertEqual(local.writes(), [])

    def test_unreachable_hub_warns_and_continues_safely(self) -> None:
        with FakeSyncthing(DECK_ID) as local, FakeSyncthing(HUB_ID) as hub:
            self.hub_with_whitelists(hub)
            url = hub.url
        # The hub fake is shut down now: its port refuses connections.
        cfg = make_config(url)
        rep, _ = reporter()
        with FakeSyncthing(DECK_ID) as local:
            for name in ("patterns-common.stignore", "patterns-deck.stignore"):
                local.arrives_after[("wz-library", name)] = 0
            provision_device(
                cfg,
                "deck",
                PROFILES["steamos-emudeck"],
                Client(local.url, API_KEY),
                Client(url, API_KEY, timeout=2),
                rep,
                home=self.home,
                sleep=lambda s: None,
            )
            self.assertTrue(any("pre-check" in w for w in rep.warnings))
            self.assertEqual(local.ignores["wz-library"], ["#include patterns-deck.stignore"])

    def test_timeout_leaves_folder_on_bootstrap_ignores(self) -> None:
        with FakeSyncthing(DECK_ID) as local, FakeSyncthing(HUB_ID) as hub:
            self.hub_with_whitelists(hub)
            with self.assertRaises(ProvisionError):
                self.run_device(local, hub, timeout=0.0)
            self.assertEqual(local.ignores["wz-library"], ["!/patterns-*.stignore", "*"])
            self.assertFalse(local.folders["wz-library"]["paused"])

    def test_resumes_after_interrupted_bootstrap(self) -> None:
        with FakeSyncthing(DECK_ID) as local, FakeSyncthing(HUB_ID) as hub:
            self.hub_with_whitelists(hub)
            with self.assertRaises(ProvisionError):
                self.run_device(local, hub, timeout=0.0)
            for name in ("patterns-common.stignore", "patterns-deck.stignore"):
                local.arrives_after[("wz-library", name)] = 0
            local.requests.clear()
            self.run_device(local, hub)
            self.assertEqual(local.ignores["wz-library"], ["#include patterns-deck.stignore"])
            boot = [
                b
                for m, p, b in local.writes()
                if p.startswith("/rest/db/ignores?folder=wz-library")
            ]
            self.assertEqual(boot, [{"ignore": ["#include patterns-deck.stignore"]}])

    def test_device_id_mismatch(self) -> None:
        with FakeSyncthing(MACHINE_ID) as local:
            with self.assertRaises(ProvisionError) as ctx:
                self.run_device(local, None)
            self.assertIn("devices.deck.device_id", str(ctx.exception))
            self.assertEqual(local.writes(), [])

    def test_path_change_is_refused(self) -> None:
        with FakeSyncthing(DECK_ID) as local:
            local.folders["wz-tools"] = {
                "id": "wz-tools",
                "path": "/elsewhere",
                "type": "receiveonly",
            }
            for name in ("patterns-common.stignore", "patterns-deck.stignore"):
                local.arrives_after[("wz-library", name)] = 0
            with self.assertRaises(ProvisionError):
                self.run_device(local, None)
            self.assertEqual(local.folders["wz-tools"]["path"], "/elsewhere")

    def test_dry_run_makes_no_writes_and_does_not_wait(self) -> None:
        with FakeSyncthing(DECK_ID) as local, FakeSyncthing(HUB_ID) as hub:
            self.hub_with_whitelists(hub)
            sleeps: list[float] = []
            _, lines = self.run_device(local, hub, dry_run=True, sleeps=sleeps)
            self.assertEqual(local.writes(), [])
            self.assertEqual(sleeps, [])
            self.assertTrue(any("would wait" in line for line in lines))
            self.assertTrue(
                any("POST /rest/db/ignores?folder=wz-library" in line for line in lines)
            )
            self.assertNotIn(API_KEY, "\n".join(lines))


class ExpandHomeTests(unittest.TestCase):
    def test_expand_home(self) -> None:
        home = Path("/home/x")
        self.assertEqual(expand_home("~/Emulation", home), "/home/x/Emulation")
        self.assertEqual(expand_home("~", home), "/home/x")
        self.assertEqual(expand_home("/abs/~/x", home), "/abs/~/x")


if __name__ == "__main__":
    unittest.main()
