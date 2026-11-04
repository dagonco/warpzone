import dataclasses
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

from support import (
    API_KEY,
    DECK_ID,
    DECK_LIBRARY_IGNORES,
    HUB_ID,
    MACHINE_ID,
    FakeProc,
    FakeSyncthing,
    config_data,
    make_config,
    stub_self_update,
)
from warpzone import esde
from warpzone import provision as provision_mod
from warpzone.config import Config, parse_config
from warpzone.profiles import Profile, load_profile
from warpzone.provision import (
    ProvisionError,
    check_hub,
    folder_status,
    provision_device,
    provision_hub,
    wait_for_files,
)
from warpzone.report import Reporter
from warpzone.runners import run_import_check
from warpzone.syncthing import Client

PROFILES = {"steamos-emudeck": load_profile("steamos-emudeck")}


setUpModule = stub_self_update


def reporter() -> tuple[Reporter, list[str]]:
    lines: list[str] = []
    return Reporter(lines.append), lines


def run_hub(
    cfg: Config, profiles: dict[str, Profile], client: Client, rep: Reporter, **kw: Any
) -> None:
    """`provision_hub` that does not wait for whitelists unless a test asks it to."""
    kw.setdefault("wait_timeout", 0.0)
    provision_hub(cfg, profiles, client, rep, **kw)


class HubTests(unittest.TestCase):
    def test_creates_devices_and_folders(self) -> None:
        with FakeSyncthing(HUB_ID) as fake:
            cfg = make_config(fake.url)
            rep, _ = reporter()
            run_hub(cfg, PROFILES, Client(fake.url, API_KEY), rep)

            ids = {d["deviceID"] for d in fake.devices}
            self.assertEqual(ids, {HUB_ID, DECK_ID, MACHINE_ID})
            library = fake.folders["wz-library"]
            self.assertEqual(library["path"], "/data/games/library")
            self.assertEqual(library["type"], "sendonly")
            self.assertFalse(library["paused"])  # created paused, then resumed
            self.assertEqual(
                fake.ignores["wz-library"],
                [
                    *PROFILES["steamos-emudeck"].placement_ignores(),
                    "#include patterns-common.stignore",
                ],
            )
            self.assertEqual(fake.ignores["wz-tools"][-2:], ["(?d)Thumbs.db", "/logs"])
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
                    ("POST", "/rest/db/ignores"),  # bootstrap: whitelists only
                    ("PATCH", "/rest/config/folders/wz-songs"),
                    ("POST", "/rest/db/scan"),
                    ("POST", "/rest/db/ignores"),  # final
                    ("POST", "/rest/db/scan"),
                ],
            )
            created = [b for m, p, b in fake.writes() if p == "/rest/config/folders"]
            self.assertTrue(all(b["paused"] for b in created))
            self.assertTrue(any("wz-library" in w for w in rep.warnings))

    def test_new_whitelisted_folder_bootstraps_before_the_full_scan(self) -> None:
        with FakeSyncthing(HUB_ID) as fake:
            cfg = make_config(fake.url)
            names = ["patterns-common.stignore", "patterns-machine.stignore"]
            for name in names:  # the whitelists of wz-songs are indexed right away
                fake.file_info[("wz-songs", name)] = {
                    "local": {"version": ["X:1"], "deleted": False, "invalid": False},
                    "global": {"version": ["X:1"]},
                }
            rep, lines = reporter()
            now = [0.0]  # wz-library never gets its whitelists: wait for it on a fake clock

            def sleep(seconds: float) -> None:
                now[0] += seconds

            run_hub(
                cfg,
                PROFILES,
                Client(fake.url, API_KEY),
                rep,
                wait_timeout=5.0,
                sleep=sleep,
                clock=lambda: now[0],
            )
            songs = [
                (m, b)
                for m, p, b in fake.writes()
                if p.startswith("/rest/db/ignores?folder=wz-songs")
            ]
            self.assertEqual(
                [b["ignore"] for _, b in songs],
                [["!/patterns-*.stignore", "*"], fake.ignores["wz-songs"]],
            )
            self.assertEqual(fake.ignores["wz-songs"][-1], "#include patterns-common.stignore")
            self.assertFalse(fake.folders["wz-songs"]["paused"])
            self.assertFalse([w for w in rep.warnings if "wz-songs" in w])
            self.assertIn(
                "wz-songs: created at /data/games/songs, bootstrap ignores set, resumed, "
                "final ignores set, full scan started",
                lines,
            )

    def test_missing_whitelists_warn_but_still_set_the_final_ignores(self) -> None:
        with FakeSyncthing(HUB_ID) as fake:
            rep, _ = reporter()
            run_hub(make_config(fake.url), PROFILES, Client(fake.url, API_KEY), rep)
            self.assertEqual(fake.ignores["wz-library"][-1], "#include patterns-common.stignore")
            self.assertFalse(fake.folders["wz-library"]["paused"])
            warning = next(w for w in rep.warnings if w.startswith("wz-library"))
            self.assertIn("patterns-common.stignore", warning)
            self.assertIn("final ignores are set anyway", warning)

    def test_bootstrap_waits_for_the_whitelists_to_be_indexed(self) -> None:
        with FakeSyncthing(HUB_ID) as fake:
            cfg = make_config(fake.url)
            for name in ("patterns-common.stignore", "patterns-machine.stignore"):
                fake.arrives_after[("wz-songs", name)] = 2
            for name in ("patterns-common.stignore", "patterns-deck.stignore"):
                fake.file_info[("wz-library", name)] = {"local": {"version": ["X:1"]}}
            fake.file_info[("wz-library", "patterns-machine.stignore")] = {
                "local": {"version": ["X:1"]}
            }
            slept: list[float] = []
            now = [0.0]

            def sleep(seconds: float) -> None:
                slept.append(seconds)
                now[0] += seconds

            run_hub(
                cfg,
                PROFILES,
                Client(fake.url, API_KEY),
                reporter()[0],
                wait_timeout=100.0,
                poll=1.0,
                sleep=sleep,
                clock=lambda: now[0],
            )
            self.assertEqual(slept, [1.0, 1.0])  # two polls before the songs whitelists show up
            self.assertEqual(fake.ignores["wz-songs"][-1], "#include patterns-common.stignore")

    def test_existing_folder_keeps_the_direct_path(self) -> None:
        with FakeSyncthing(HUB_ID) as fake:
            cfg = make_config(fake.url)
            run_hub(cfg, PROFILES, Client(fake.url, API_KEY), reporter()[0])
            fake.ignores["wz-songs"] = []
            fake.requests.clear()
            run_hub(cfg, PROFILES, Client(fake.url, API_KEY), reporter()[0])
            ignores = [b["ignore"] for m, p, b in fake.writes() if "db/ignores" in p]
            self.assertEqual(ignores, [fake.ignores["wz-songs"]])
            self.assertEqual(fake.ignores["wz-songs"][-1], "#include patterns-common.stignore")

    def test_dry_run_prints_the_bootstrap_and_waits_for_nothing(self) -> None:
        with FakeSyncthing(HUB_ID) as fake:
            lines: list[str] = []
            client = Client(fake.url, API_KEY, label="hub", dry_run=True, sink=lines.append)
            rep, out = reporter()
            provision_hub(make_config(fake.url), PROFILES, client, rep, wait_timeout=600.0)
            self.assertEqual(fake.writes(), [])
            self.assertTrue(any('"!/patterns-*.stignore", "*"' in line for line in lines))
            self.assertTrue(any("would wait up to 600s for the whitelists" in o for o in out))

    def test_second_run_writes_nothing(self) -> None:
        with FakeSyncthing(HUB_ID) as fake:
            cfg = make_config(fake.url)
            run_hub(cfg, PROFILES, Client(fake.url, API_KEY), reporter()[0])
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
            run_hub(cfg, PROFILES, Client(fake.url, API_KEY), rep)
            self.assertEqual(fake.writes(), [])
            self.assertEqual(rep.warnings, [])

    def test_hub_id_mismatch(self) -> None:
        with FakeSyncthing(DECK_ID) as fake:
            with self.assertRaises(ProvisionError) as ctx:
                run_hub(make_config(fake.url), PROFILES, Client(fake.url, API_KEY), reporter()[0])
            self.assertIn("hub.device_id", str(ctx.exception))
            self.assertEqual(fake.writes(), [])

    def test_updates_only_owned_keys_and_never_removes_devices(self) -> None:
        with FakeSyncthing(HUB_ID) as fake:
            cfg = make_config(fake.url)
            run_hub(cfg, PROFILES, Client(fake.url, API_KEY), reporter()[0])
            other = {"deviceID": "OTHER-ID"}
            fake.folders["wz-tools"].update(
                label="renamed", rescanIntervalS=99, devices=[other, {"deviceID": DECK_ID}]
            )
            fake.requests.clear()
            run_hub(cfg, PROFILES, Client(fake.url, API_KEY), reporter()[0])
            patches = [(p, b) for m, p, b in fake.writes() if m == "PATCH"]
            self.assertEqual(patches, [("/rest/config/folders/wz-tools", {"label": "wz-tools"})])
            self.assertEqual(fake.folders["wz-tools"]["rescanIntervalS"], 99)

            fake.folders["wz-tools"]["devices"] = [other]
            fake.requests.clear()
            run_hub(cfg, PROFILES, Client(fake.url, API_KEY), reporter()[0])
            sent = next(b for m, p, b in fake.writes() if m == "PATCH")["devices"]
            self.assertEqual([d["deviceID"] for d in sent], ["OTHER-ID", DECK_ID])

    def test_foreign_folders_are_never_touched(self) -> None:
        with FakeSyncthing(HUB_ID) as fake:
            fake.folders["photos"] = {"id": "photos", "path": "/p", "type": "sendreceive"}
            fake.ignores["photos"] = ["x"]
            run_hub(make_config(fake.url), PROFILES, Client(fake.url, API_KEY), reporter()[0])
            self.assertEqual(
                fake.folders["photos"], {"id": "photos", "path": "/p", "type": "sendreceive"}
            )
            self.assertFalse([w for w in fake.writes() if "photos" in w[1]])

    def test_path_change_is_refused(self) -> None:
        with FakeSyncthing(HUB_ID) as fake:
            cfg = make_config(fake.url)
            run_hub(cfg, PROFILES, Client(fake.url, API_KEY), reporter()[0])
            fake.folders["wz-tools"]["path"] = "/somewhere/else"
            fake.requests.clear()
            with self.assertRaises(ProvisionError) as ctx:
                run_hub(cfg, PROFILES, Client(fake.url, API_KEY), reporter()[0])
            self.assertIn("mass delete", str(ctx.exception))
            self.assertEqual(fake.folders["wz-tools"]["path"], "/somewhere/else")

    def test_trailing_slash_is_not_a_path_change(self) -> None:
        with FakeSyncthing(HUB_ID) as fake:
            cfg = make_config(fake.url)
            run_hub(cfg, PROFILES, Client(fake.url, API_KEY), reporter()[0])
            fake.folders["wz-tools"]["path"] += "/"
            run_hub(cfg, PROFILES, Client(fake.url, API_KEY), reporter()[0])

    def test_dry_run_makes_no_writes(self) -> None:
        with FakeSyncthing(HUB_ID) as fake:
            lines: list[str] = []
            client = Client(fake.url, API_KEY, label="hub", dry_run=True, sink=lines.append)
            run_hub(make_config(fake.url), PROFILES, client, reporter()[0])
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
            run_hub(make_config(fake.url), PROFILES, Client(fake.url, API_KEY), reporter()[0])
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


class DeviceFixture(unittest.TestCase):
    """A temporary home and `provision_device` against two fake Syncthing instances."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.home = Path(self._tmp.name)
        # One emulator that was opened once: its save directory exists.
        (self.home / ".var/app/org.libretro.RetroArch/config/retroarch/saves").mkdir(parents=True)

    def run_device(
        self,
        local_fake,
        hub_fake,
        *,
        timeout=60.0,
        dry_run=False,
        sleeps=None,
        folders=None,
        **extra,
    ):
        data = config_data(hub_fake.url if hub_fake else "http://127.0.0.1:1")
        if folders is not None:
            data["devices"]["deck"]["folders"] = folders
        cfg = parse_config(data)
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
            **extra,
        )
        return rep, lines

    def hub_with_whitelists(self, hub: FakeSyncthing) -> None:
        hub.browse["wz-library"] = ["patterns-common.stignore", "patterns-deck.stignore"]


class DeviceTests(DeviceFixture):
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
            self.assertEqual(local.ignores["wz-library"], DECK_LIBRARY_IGNORES)

            ignore_writes = [
                b["ignore"]
                for m, p, b in local.writes()
                if p.startswith("/rest/db/ignores?folder=wz-library")
            ]
            self.assertEqual(ignore_writes, [["!/patterns-*.stignore", "*"], DECK_LIBRARY_IGNORES])
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
            self.assertEqual(sum("skipped" in w for w in rep.warnings), 35)

    def bios_device(self, local, hub, **kwargs):
        self.hub_with_whitelists(hub)
        for name in ("patterns-common.stignore", "patterns-deck.stignore"):
            local.arrives_after[("wz-library", name)] = 0
        return self.run_device(local, hub, folders=["library", "bios"], **kwargs)

    def test_missing_placement_targets_are_skipped_not_fatal(self) -> None:
        with FakeSyncthing(DECK_ID) as local, FakeSyncthing(HUB_ID) as hub:
            rep, _ = self.bios_device(local, hub)
            self.assertIn("wz-library", local.folders)
            self.assertFalse([f for f in local.folders if f.startswith("wz-bios-")])
            skipped = [w for w in rep.warnings if "skipped" in w]
            self.assertEqual(len(skipped), 4)
            self.assertFalse((self.home / ".local").exists())  # nothing was created

    def test_placement_lands_in_the_emulators_real_directory(self) -> None:
        cemu = self.home / ".local/share/Cemu"
        cemu.mkdir(parents=True)
        with FakeSyncthing(DECK_ID) as local, FakeSyncthing(HUB_ID) as hub:
            self.bios_device(local, hub)
            folder = local.folders["wz-bios-cemu-keys"]
            self.assertEqual(folder["path"], str(cemu))
            self.assertEqual(folder["type"], "receiveonly")
            self.assertFalse(folder["paused"])
            self.assertEqual(local.ignores["wz-bios-cemu-keys"][-2:], ["!/keys.txt", "*"])
            self.assertNotIn("wz-bios-eden-keys", local.folders)

    def test_target_is_created_only_when_the_emulator_is_installed(self) -> None:
        eden = self.home / ".local/share/eden"
        eden.mkdir(parents=True)  # Eden ran once, but never had a keys folder
        with FakeSyncthing(DECK_ID) as local, FakeSyncthing(HUB_ID) as hub:
            self.bios_device(local, hub)
            self.assertTrue((eden / "keys").is_dir())
            self.assertEqual(local.folders["wz-bios-eden-keys"]["path"], str(eden / "keys"))
            # azahar has create_when too, but its emulator folder is absent
            self.assertNotIn("wz-bios-azahar-keys", local.folders)
            self.assertFalse((self.home / ".local/share/azahar-emu").exists())

    def test_dry_run_does_not_create_the_target(self) -> None:
        eden = self.home / ".local/share/eden"
        eden.mkdir(parents=True)
        with FakeSyncthing(DECK_ID) as local, FakeSyncthing(HUB_ID) as hub:
            _, lines = self.bios_device(local, hub, dry_run=True)
            self.assertFalse((eden / "keys").exists())
            self.assertTrue(any("would create" in line for line in lines))
            self.assertEqual(local.writes(), [])

    def test_device_without_the_bios_group_has_no_placements(self) -> None:
        (self.home / ".local/share/Cemu").mkdir(parents=True)
        with FakeSyncthing(DECK_ID) as local, FakeSyncthing(HUB_ID) as hub:
            self.hub_with_whitelists(hub)
            for name in ("patterns-common.stignore", "patterns-deck.stignore"):
                local.arrives_after[("wz-library", name)] = 0
            self.run_device(local, hub)
            self.assertFalse([f for f in local.folders if f.startswith("wz-bios-")])

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
            self.assertEqual(local.ignores["wz-library"], DECK_LIBRARY_IGNORES)

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
            self.assertEqual(local.ignores["wz-library"], DECK_LIBRARY_IGNORES)
            boot = [
                b
                for m, p, b in local.writes()
                if p.startswith("/rest/db/ignores?folder=wz-library")
            ]
            self.assertEqual(boot, [{"ignore": DECK_LIBRARY_IGNORES}])

    def test_whitelisted_folder_gets_a_bounded_rescan_interval(self) -> None:
        with FakeSyncthing(DECK_ID) as local, FakeSyncthing(HUB_ID) as hub:
            self.hub_with_whitelists(hub)
            for name in ("patterns-common.stignore", "patterns-deck.stignore"):
                local.arrives_after[("wz-library", name)] = 0
            self.run_device(local, hub)
            self.assertEqual(local.folders["wz-library"]["rescanIntervalS"], 300)
            self.assertNotIn("rescanIntervalS", local.folders["wz-tools"])

            local.folders["wz-library"]["rescanIntervalS"] = 3600  # Syncthing's default
            local.requests.clear()
            self.run_device(local, hub)
            patches = [(p, b) for m, p, b in local.writes() if m == "PATCH"]
            self.assertEqual(
                patches, [("/rest/config/folders/wz-library", {"rescanIntervalS": 300})]
            )

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


LIBRARY_LISTS = ("patterns-common.stignore", "patterns-deck.stignore")


class UnattendedTests(DeviceFixture):
    """`provision device --unattended`: nothing that could lose data or fail for no reason."""

    def established(self, local: FakeSyncthing, hub: FakeSyncthing) -> None:
        """A device provisioned by hand once, with the markers Syncthing writes in each folder."""
        self.hub_with_whitelists(hub)
        for name in LIBRARY_LISTS:
            local.arrives_after[("wz-library", name)] = 0
        self.run_device(local, hub)
        for folder in local.folders.values():
            (Path(folder["path"]) / ".stfolder").mkdir(parents=True, exist_ok=True)
        local.requests.clear()

    def lists_on_disk(self, local: FakeSyncthing) -> None:
        for name in LIBRARY_LISTS:
            (Path(local.folders["wz-library"]["path"]) / name).write_text("# fake\n")

    def ignore_writes(self, local: FakeSyncthing) -> list[list[str]]:
        return [b["ignore"] for m, p, b in local.writes() if p.startswith("/rest/db/ignores")]

    def test_an_existing_whitelisted_folder_never_gets_the_bootstrap_ignores(self) -> None:
        with FakeSyncthing(DECK_ID) as local, FakeSyncthing(HUB_ID) as hub:
            self.established(local, hub)
            local.ignores["wz-library"] = ["/stale"]  # differs from the final ignores
            local.arrives_after.clear()  # the hub is out of reach: nothing would arrive
            rep, _ = self.run_device(local, hub, unattended=True, timeout=0.0)
            self.assertEqual(local.ignores["wz-library"], ["/stale"])
            self.assertEqual(local.writes(), [])
            self.assertTrue(any("wz-library" in w and "left alone" in w for w in rep.warnings))

    def test_an_existing_whitelisted_folder_takes_the_final_ignores_when_the_lists_are_on_disk(
        self,
    ) -> None:
        with FakeSyncthing(DECK_ID) as local, FakeSyncthing(HUB_ID) as hub:
            self.established(local, hub)
            local.ignores["wz-library"] = ["!/patterns-*.stignore", "*"]  # an old bootstrap
            local.arrives_after.clear()
            self.lists_on_disk(local)
            self.run_device(local, hub, unattended=True, timeout=0.0)
            self.assertEqual(self.ignore_writes(local), [DECK_LIBRARY_IGNORES])
            self.assertEqual(local.ignores["wz-library"], DECK_LIBRARY_IGNORES)

    def test_a_second_unattended_run_writes_nothing(self) -> None:
        with FakeSyncthing(DECK_ID) as local, FakeSyncthing(HUB_ID) as hub:
            self.established(local, hub)
            self.run_device(local, hub, unattended=True, timeout=0.0)
            self.assertEqual(local.writes(), [])

    def test_a_type_change_is_refused_before_anything_is_written(self) -> None:
        with FakeSyncthing(DECK_ID) as local, FakeSyncthing(HUB_ID) as hub:
            self.established(local, hub)
            local.folders["wz-library"]["type"] = "sendreceive"
            local.folders["wz-tools"]["label"] = "renamed"  # would be patched otherwise
            with self.assertRaises(ProvisionError) as ctx:
                self.run_device(local, hub, unattended=True)
            self.assertIn("wz-library", str(ctx.exception))
            self.assertIn("type", str(ctx.exception))
            self.assertEqual(local.writes(), [])
            self.assertEqual(local.folders["wz-library"]["type"], "sendreceive")

    def test_a_versioning_change_is_refused(self) -> None:
        with FakeSyncthing(DECK_ID) as local, FakeSyncthing(HUB_ID) as hub:
            self.established(local, hub)
            local.folders["wz-tools"]["versioning"] = {"type": "simple"}
            with self.assertRaises(ProvisionError) as ctx:
                self.run_device(local, hub, unattended=True)
            self.assertIn("versioning", str(ctx.exception))
            self.assertEqual(local.writes(), [])

    def test_the_manual_mode_still_changes_type_and_versioning(self) -> None:
        with FakeSyncthing(DECK_ID) as local, FakeSyncthing(HUB_ID) as hub:
            self.established(local, hub)
            local.folders["wz-library"]["type"] = "sendreceive"
            self.run_device(local, hub)
            self.assertEqual(local.folders["wz-library"]["type"], "receiveonly")

    def test_a_folder_without_its_marker_aborts_without_changes(self) -> None:
        with FakeSyncthing(DECK_ID) as local, FakeSyncthing(HUB_ID) as hub:
            self.established(local, hub)
            (Path(local.folders["wz-library"]["path"]) / ".stfolder").rmdir()  # card not mounted
            local.folders["wz-tools"]["label"] = "renamed"
            with self.assertRaises(ProvisionError) as ctx:
                self.run_device(local, hub, unattended=True)
            self.assertIn("wz-library", str(ctx.exception))
            self.assertIn(".stfolder", str(ctx.exception))
            self.assertEqual(local.writes(), [])

    def test_the_marker_name_of_the_folder_is_the_one_that_counts(self) -> None:
        with FakeSyncthing(DECK_ID) as local, FakeSyncthing(HUB_ID) as hub:
            self.established(local, hub)
            local.folders["wz-tools"]["markerName"] = ".stfolder-custom"
            with self.assertRaises(ProvisionError):
                self.run_device(local, hub, unattended=True)
            (Path(local.folders["wz-tools"]["path"]) / ".stfolder-custom").mkdir()
            self.run_device(local, hub, unattended=True)

    def test_a_new_folder_unknown_to_the_hub_is_skipped_with_a_warning(self) -> None:
        with FakeSyncthing(DECK_ID) as local, FakeSyncthing(HUB_ID) as hub:
            rep, _ = self.run_device(local, hub, unattended=True, timeout=0.0)
            self.assertNotIn("wz-library", local.folders)
            self.assertIn("wz-tools", local.folders)
            self.assertTrue(any("wz-library" in w and "provision hub" in w for w in rep.warnings))

    def test_a_new_folder_whose_whitelist_is_missing_on_the_hub_is_skipped(self) -> None:
        with FakeSyncthing(DECK_ID) as local, FakeSyncthing(HUB_ID) as hub:
            hub.browse["wz-library"] = ["patterns-common.stignore"]
            rep, _ = self.run_device(local, hub, unattended=True, timeout=0.0)
            self.assertNotIn("wz-library", local.folders)
            self.assertTrue(
                any("patterns-deck.stignore" in w and "wz-library" in w for w in rep.warnings)
            )

    def test_an_existing_folder_the_hub_lost_is_still_an_error(self) -> None:
        with FakeSyncthing(DECK_ID) as local, FakeSyncthing(HUB_ID) as hub:
            self.established(local, hub)
            hub.browse.clear()
            with self.assertRaises(ProvisionError):
                self.run_device(local, hub, unattended=True)

    def test_a_new_folder_whose_whitelists_do_not_arrive_is_a_warning_not_a_failure(self) -> None:
        with FakeSyncthing(DECK_ID) as local, FakeSyncthing(HUB_ID) as hub:
            self.hub_with_whitelists(hub)
            rep, _ = self.run_device(local, hub, unattended=True, timeout=0.0)
            # Still on the safe bootstrap ignores; the next run finishes the job.
            self.assertEqual(local.ignores["wz-library"], ["!/patterns-*.stignore", "*"])
            self.assertTrue(any("wz-library" in w and "did not arrive" in w for w in rep.warnings))
            self.assertFalse(rep.deferred)

    def test_the_wait_of_an_unattended_run_is_short_by_default(self) -> None:
        from warpzone.config import DEFAULT_WAIT_SECONDS, UNATTENDED_WAIT_SECONDS

        self.assertLess(UNATTENDED_WAIT_SECONDS, DEFAULT_WAIT_SECONDS)

    def test_the_manual_mode_still_raises_when_the_whitelists_do_not_arrive(self) -> None:
        with FakeSyncthing(DECK_ID) as local, FakeSyncthing(HUB_ID) as hub:
            self.hub_with_whitelists(hub)
            with self.assertRaises(ProvisionError):
                self.run_device(local, hub, timeout=0.0)

    def test_a_dry_run_unattended_writes_nothing(self) -> None:
        with FakeSyncthing(DECK_ID) as local, FakeSyncthing(HUB_ID) as hub:
            self.established(local, hub)
            local.ignores["wz-library"] = ["/stale"]
            self.lists_on_disk(local)
            self.run_device(local, hub, unattended=True, dry_run=True)
            self.assertEqual(local.writes(), [])


RA_APP = "org.libretro.RetroArch"
DOLPHIN_APP = "org.DolphinEmu.dolphin-emu"
SYNCTHINGY = "com.github.zocker_160.SyncThingy"


class FlatpakRun:
    """A fake `flatpak`: keeps the filesystem overrides the way `flatpak override` does."""

    def __init__(self, granted: tuple[str, ...] = (), fail_on: str | None = None) -> None:
        self.granted = list(granted)
        self.fail_on = fail_on
        self.commands: list[list[str]] = []

    def __call__(self, command):
        self.commands.append(list(command))
        if self.fail_on and self.fail_on in command:
            return 1, "error: boom"
        if "--show" in command:
            return 0, "[Context]\nfilesystems=host;" + "".join(f"{g};" for g in self.granted) + "\n"
        self.granted += [a.split("=", 1)[1] for a in command if a.startswith("--filesystem=")]
        return 0, ""

    @property
    def writes(self) -> list[list[str]]:
        return [c for c in self.commands if "--show" not in c]


class FlatpakAccessTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.home = Path(self._tmp.name)
        for app, sub in (
            (RA_APP, "config/retroarch/saves"),
            (DOLPHIN_APP, "data/dolphin-emu/GC"),
        ):
            (self.home / ".var/app" / app / sub).mkdir(parents=True)
        self.config = self.home / ".var/app" / SYNCTHINGY / "config/syncthing/config.xml"
        data = config_data()
        data["devices"]["deck"]["folders"] = ["saves"]
        self.cfg = parse_config(data)

    @staticmethod
    def flatpak_warnings(rep: Reporter) -> list[str]:
        return [w for w in rep.warnings if "skipped" not in w]

    def provision(self, flatpak, *, config="flatpak", dry_run=False, unattended=False):
        rep, lines = reporter()
        path = {"flatpak": self.config, "native": self.home / ".local/state/syncthing/config.xml"}
        with FakeSyncthing(DECK_ID) as local:
            client = Client(local.url, API_KEY, label="local", dry_run=dry_run, sink=lines.append)
            provision_device(
                self.cfg,
                "deck",
                PROFILES["steamos-emudeck"],
                client,
                None,
                rep,
                home=self.home,
                wait_timeout=0.0,
                poll=1.0,
                sleep=lambda s: None,
                syncthing_config=path.get(config),
                flatpak_run=flatpak,
                unattended=unattended,
            )
        return rep, lines

    def test_unattended_grants_nothing_and_warns_with_the_command(self) -> None:
        flatpak = FlatpakRun()
        rep, _ = self.provision(flatpak, unattended=True)
        self.assertEqual(flatpak.writes, [])
        (warning,) = self.flatpak_warnings(rep)
        self.assertIn("flatpak override --user", warning)
        self.assertIn("restart Syncthing", warning)

    def test_unattended_with_everything_granted_stays_quiet(self) -> None:
        granted = (f"~/.var/app/{RA_APP}", f"~/.var/app/{DOLPHIN_APP}")
        flatpak = FlatpakRun(granted=granted)
        rep, _ = self.provision(flatpak, unattended=True)
        self.assertEqual(flatpak.writes, [])
        self.assertEqual(self.flatpak_warnings(rep), [])

    def test_grants_every_app_folder_the_profile_uses_in_one_override(self) -> None:
        flatpak = FlatpakRun()
        rep, lines = self.provision(flatpak)
        self.assertEqual(
            flatpak.writes,
            [
                [
                    "flatpak",
                    "override",
                    "--user",
                    f"--filesystem=~/.var/app/{RA_APP}",
                    f"--filesystem=~/.var/app/{DOLPHIN_APP}",
                    SYNCTHINGY,
                ]
            ],
        )
        self.assertTrue(any(f"granted ~/.var/app/{RA_APP}" in line for line in lines))
        self.assertTrue(any("restart Syncthing" in w for w in self.flatpak_warnings(rep)))

    def test_apps_that_are_not_installed_are_not_granted(self) -> None:
        flatpak = FlatpakRun()
        self.provision(flatpak)
        flat = " ".join(" ".join(c) for c in flatpak.writes)
        self.assertNotIn("ppsspp", flat)  # its folder does not exist, so it is skipped

    def test_second_run_adds_nothing_and_asks_for_no_restart(self) -> None:
        flatpak = FlatpakRun()
        self.provision(flatpak)
        flatpak.commands.clear()
        rep, lines = self.provision(flatpak)
        self.assertEqual(flatpak.writes, [])
        self.assertEqual(self.flatpak_warnings(rep), [])
        self.assertTrue(any("already granted" in line for line in lines))

    def test_only_the_missing_override_is_added(self) -> None:
        flatpak = FlatpakRun(granted=(f"~/.var/app/{RA_APP}",))
        self.provision(flatpak)
        (write,) = flatpak.writes
        self.assertEqual(
            write,
            ["flatpak", "override", "--user", f"--filesystem=~/.var/app/{DOLPHIN_APP}", SYNCTHINGY],
        )

    def test_dry_run_reports_without_writing(self) -> None:
        flatpak = FlatpakRun()
        rep, lines = self.provision(flatpak, dry_run=True)
        self.assertEqual(flatpak.writes, [])
        self.assertEqual(self.flatpak_warnings(rep), [])
        self.assertTrue(any("dry-run: would grant" in line for line in lines))
        self.assertTrue(any("restart Syncthing" in line for line in lines))

    def test_native_syncthing_or_unknown_location_is_left_alone(self) -> None:
        for config in ("native", None):
            flatpak = FlatpakRun()
            self.provision(flatpak, config=config)
            self.assertEqual(flatpak.commands, [], config)

    def test_flatpak_that_fails_warns_with_the_command_and_provisioning_goes_on(self) -> None:
        def broken(command):
            raise FileNotFoundError(2, "No such file or directory")

        rep, lines = self.provision(broken)
        self.assertTrue(any("cannot read the flatpak overrides" in w for w in rep.warnings))
        self.assertTrue(any(f"--filesystem=~/.var/app/{RA_APP}" in w for w in rep.warnings))
        self.assertTrue(any(line.startswith("wz-save-retroarch-saves:") for line in lines))

    def test_a_failed_override_warns_with_the_command(self) -> None:
        rep, _ = self.provision(FlatpakRun(fail_on="--user"))
        self.assertTrue(any("cannot read the flatpak overrides" in w for w in rep.warnings))
        flatpak = FlatpakRun()
        real = flatpak.__call__

        def fail_write(command):
            return real(command) if "--show" in command else (1, "denied")

        rep, _ = self.provision(fail_write)
        self.assertTrue(any("cannot grant Syncthing access" in w for w in rep.warnings))
        self.assertFalse(any("restart Syncthing" in w for w in self.flatpak_warnings(rep)))


ES_SETTINGS = (
    '<?xml version="1.0"?>\n'
    '<bool name="BackgroundJoystickInput" value="false" />\n'
    '<bool name="CustomEventScripts" value="false" />\n'
    '<string name="MediaDirectory" value="/home/user/ES-DE/downloaded_media" />\n'
)
PYTHON = "/opt/wz/bin/python"


class EsdeScriptTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        tmp = Path(self._tmp.name)
        self.home = tmp / "home"
        self.state = tmp / "state"
        self.proc = FakeProc(tmp / "proc")
        self.esde = self.home / "ES-DE"
        self.script = esde.script_path(self.esde)
        self.settings = esde.settings_path(self.esde)

    def write_settings(self, text: str = ES_SETTINGS) -> None:
        self.settings.parent.mkdir(parents=True)
        self.settings.write_bytes(text.encode())

    def start_esde(self, comm: str = "es-de", argv0: str | None = None) -> None:
        self.proc.add(4242, comm, argv0 or comm)

    def provision(self, *, folders=("settings",), dry_run=False, profile=None, **kw):
        data = config_data()
        data["devices"]["deck"]["folders"] = list(folders)
        cfg = parse_config(data)
        rep, lines = reporter()
        kw.setdefault("import_check", lambda python: True)
        kw.setdefault("proc", self.proc.path)
        with FakeSyncthing(DECK_ID) as local:
            client = Client(local.url, API_KEY, label="local", dry_run=dry_run, sink=lines.append)
            provision_device(
                cfg,
                "deck",
                profile or PROFILES["steamos-emudeck"],
                client,
                None,
                rep,
                home=self.home,
                wait_timeout=0.0,
                poll=1.0,
                sleep=lambda s: None,
                python=PYTHON,
                state_root=self.state,
                **kw,
            )
        return rep, lines

    def backups(self) -> list[Path]:
        folder = self.state / "backups" / "esde" / "es_settings"
        return sorted(folder.iterdir()) if folder.is_dir() else []

    def test_writes_an_executable_script(self) -> None:
        rep, lines = self.provision()
        self.assertEqual(self.script.read_text(), esde.render_startup_script(PYTHON, "deck"))
        self.assertEqual(stat.S_IMODE(self.script.stat().st_mode), 0o755)
        self.assertTrue(any(str(self.script) in line for line in lines))
        self.assertEqual(rep.warnings, [w for w in rep.warnings if "es_settings" in w])

    def test_a_custom_config_path_is_written_into_the_script(self) -> None:
        custom = Path("/srv/wz/config.toml")
        self.provision(config_path=custom)
        self.assertEqual(
            self.script.read_text(), esde.render_startup_script(PYTHON, "deck", custom)
        )
        self.assertIn("--config '/srv/wz/config.toml'", self.script.read_text())

    def test_the_default_config_path_is_left_out_of_the_script(self) -> None:
        default = self.home / ".config" / "warpzone" / "config.toml"
        self.provision(config_path=default)
        self.assertNotIn("--config", self.script.read_text())
        self.assertEqual(self.script.read_text(), esde.render_startup_script(PYTHON, "deck"))

    def test_an_unchanged_script_is_not_rewritten(self) -> None:
        self.provision()
        before = self.script.stat()
        _, lines = self.provision()
        after = self.script.stat()
        self.assertEqual((before.st_ino, before.st_mtime_ns), (after.st_ino, after.st_mtime_ns))
        self.assertTrue(any("script unchanged" in line for line in lines))

    def test_a_different_script_is_rewritten_and_the_mode_repaired(self) -> None:
        self.script.parent.mkdir(parents=True)
        self.script.write_text("#!/bin/sh\necho old\n")
        self.script.chmod(0o644)
        self.provision()
        self.assertEqual(self.script.read_text(), esde.render_startup_script(PYTHON, "deck"))
        self.script.chmod(0o644)
        self.provision()
        self.assertEqual(stat.S_IMODE(self.script.stat().st_mode), 0o755)

    def test_other_scripts_are_never_touched(self) -> None:
        other = self.script.with_name("10-mine.sh")
        other.parent.mkdir(parents=True)
        other.write_text("#!/bin/sh\n")
        self.provision()
        self.assertEqual(other.read_text(), "#!/bin/sh\n")

    def test_dry_run_prints_the_script_and_writes_nothing(self) -> None:
        self.write_settings()
        _, lines = self.provision(dry_run=True)
        self.assertFalse(self.script.exists())
        self.assertEqual(self.settings.read_text(), ES_SETTINGS)
        self.assertEqual(self.backups(), [])
        text = "\n".join(lines)
        self.assertIn("dry-run: would write", text)
        self.assertIn("timeout 15 '/opt/wz/bin/python' -m warpzone", text)
        self.assertIn("dry-run: would set CustomEventScripts", text)

    def test_a_device_without_the_settings_group_gets_nothing(self) -> None:
        self.write_settings()
        self.provision(folders=("frontend",))
        self.assertFalse(self.script.exists())
        self.assertEqual(self.settings.read_text(), ES_SETTINGS)

    def test_a_profile_without_an_esde_path_gets_nothing(self) -> None:
        profile = PROFILES["steamos-emudeck"]
        bare = dataclasses.replace(
            profile, paths={k: v for k, v in profile.paths.items() if k != "esde"}
        )
        rep, _ = self.provision(profile=bare)
        self.assertFalse(self.script.exists())
        self.assertEqual(rep.warnings, [])

    def test_a_python_that_cannot_import_warpzone_installs_nothing(self) -> None:
        self.write_settings()
        seen: list[str] = []

        def cannot(python: str) -> bool:
            seen.append(python)
            return False

        rep, _ = self.provision(import_check=cannot)
        self.assertEqual(seen, [PYTHON])
        self.assertFalse(self.script.exists())
        self.assertEqual(self.settings.read_text(), ES_SETTINGS)
        self.assertEqual(len(rep.warnings), 1)
        self.assertIn("cannot import warpzone", rep.warnings[0])

    def test_the_real_import_check_runs_outside_the_current_folder(self) -> None:
        calls = []

        def fake_run(command, **kw):
            calls.append((command, kw))
            return subprocess.CompletedProcess(command, 0)

        with (
            mock.patch.dict(
                "os.environ", {"PYTHONPATH": "/clone", "PYTHONHOME": "/x", "KEEP": "1"}
            ),
            mock.patch("warpzone.provision.subprocess.run", fake_run),
        ):
            self.assertTrue(run_import_check(PYTHON))
        ((command, kw),) = calls
        self.assertEqual(command, [PYTHON, "-c", "import warpzone"])
        self.assertEqual(kw["cwd"], "/")
        self.assertEqual(kw["env"].get("KEEP"), "1")
        self.assertNotIn("PYTHONPATH", kw["env"])
        self.assertNotIn("PYTHONHOME", kw["env"])
        with mock.patch(
            "warpzone.provision.subprocess.run",
            lambda command, **kw: subprocess.CompletedProcess(command, 1),
        ):
            self.assertFalse(run_import_check(PYTHON))
        with mock.patch("warpzone.provision.subprocess.run", side_effect=FileNotFoundError):
            self.assertFalse(run_import_check(PYTHON))

    def test_turns_the_event_scripts_on_with_a_backup(self) -> None:
        self.write_settings()
        self.settings.chmod(0o640)
        rep, lines = self.provision()
        self.assertEqual(
            self.settings.read_text(),
            ES_SETTINGS.replace(
                '"CustomEventScripts" value="false"', '"CustomEventScripts" value="true"'
            ),
        )
        self.assertEqual(stat.S_IMODE(self.settings.stat().st_mode), 0o640)
        (backup,) = self.backups()
        self.assertEqual(backup.read_text(), ES_SETTINGS)
        self.assertEqual(stat.S_IMODE(backup.stat().st_mode), 0o600)
        self.assertTrue(backup.name.endswith("-es_settings.xml"))
        self.assertEqual(rep.warnings, [])
        self.assertTrue(any("CustomEventScripts" in line for line in lines))

    def test_a_missing_element_is_added(self) -> None:
        self.write_settings(
            ES_SETTINGS.replace('<bool name="CustomEventScripts" value="false" />\n', "")
        )
        self.provision()
        self.assertIs(esde.event_scripts_enabled(self.settings.read_text()), True)

    def test_already_on_changes_nothing(self) -> None:
        on = ES_SETTINGS.replace(
            '"CustomEventScripts" value="false"', '"CustomEventScripts" value="true"'
        )
        self.write_settings(on)
        before = self.settings.stat()
        rep, lines = self.provision()
        self.assertEqual(self.backups(), [])
        self.assertEqual(self.settings.stat().st_mtime_ns, before.st_mtime_ns)
        self.assertEqual(rep.warnings, [])
        self.assertTrue(any("already on" in line for line in lines))

    def test_second_run_writes_nothing(self) -> None:
        self.write_settings()
        self.provision()
        before = self.settings.stat()
        self.provision()
        self.assertEqual(len(self.backups()), 1)
        self.assertEqual(self.settings.stat().st_mtime_ns, before.st_mtime_ns)

    def test_left_alone_while_esde_runs(self) -> None:
        for comm, argv0 in (("es-de", None), ("AppRun.wrapped", "/x/ES-DE.AppImage")):
            with self.subTest(comm=comm):
                self.proc.clear()
                self.start_esde(comm, argv0)
                self.write_settings() if not self.settings.exists() else None
                rep, _ = self.provision()
                self.assertEqual(self.settings.read_text(), ES_SETTINGS)
                self.assertEqual(self.backups(), [])
                (warning,) = rep.warnings
                self.assertIn("quit ES-DE", warning)
                self.assertIn("provision device deck", warning)
                self.assertTrue(self.script.exists())  # the script itself is still installed

    def test_without_proc_it_is_left_alone_and_says_why(self) -> None:
        self.write_settings()
        rep, _ = self.provision(proc=self.proc.path / "missing")
        self.assertEqual(self.settings.read_text(), ES_SETTINGS)
        (warning,) = rep.warnings
        self.assertIn("cannot tell whether ES-DE is running", warning)

    def test_a_missing_settings_file_is_warned_and_never_created(self) -> None:
        rep, _ = self.provision()
        self.assertFalse(self.settings.exists())
        self.assertFalse(self.settings.parent.exists())
        (warning,) = rep.warnings
        self.assertIn("start ES-DE once", warning)

    def test_a_file_that_does_not_parse_is_left_alone(self) -> None:
        self.write_settings("<bool name=")
        rep, _ = self.provision()
        self.assertEqual(self.settings.read_text(), "<bool name=")
        self.assertEqual(self.backups(), [])
        (warning,) = rep.warnings
        self.assertIn("Enable custom event scripts", warning)

    def test_a_file_that_is_not_utf8_is_left_alone(self) -> None:
        self.settings.parent.mkdir(parents=True)
        self.settings.write_bytes(b"\xff\xfe<bool")
        rep, _ = self.provision()
        self.assertEqual(self.settings.read_bytes(), b"\xff\xfe<bool")
        self.assertEqual(len(rep.warnings), 1)

    def test_a_file_that_changes_during_the_edit_is_not_overwritten(self) -> None:
        self.write_settings()
        with mock.patch("warpzone.provision.settings_state.atomic_write", return_value=False):
            rep, _ = self.provision()
        self.assertEqual(self.settings.read_text(), ES_SETTINGS)
        (warning,) = rep.warnings
        self.assertIn("changed while", warning)

    def test_esde_started_just_before_the_write_stops_the_edit(self) -> None:
        self.write_settings()
        real = provision_mod.settings_state.backup

        def backup_then_start(*args, **kw):
            result = real(*args, **kw)
            self.start_esde()
            return result

        with mock.patch("warpzone.provision.settings_state.backup", backup_then_start):
            rep, _ = self.provision()
        self.assertEqual(self.settings.read_text(), ES_SETTINGS)
        self.assertIn("quit ES-DE", rep.warnings[0])

    def test_unattended_defers_while_esde_runs(self) -> None:
        self.write_settings()
        self.start_esde()
        rep, _ = self.provision(unattended=True)
        self.assertTrue(rep.deferred)
        self.assertEqual(self.settings.read_text(), ES_SETTINGS)

    def test_a_run_that_could_write_everything_is_not_deferred(self) -> None:
        self.write_settings()
        rep, _ = self.provision(unattended=True)
        self.assertFalse(rep.deferred)
        self.assertIn('name="CustomEventScripts" value="true"', self.settings.read_text())

    def test_without_proc_the_run_is_not_deferred(self) -> None:
        self.write_settings()
        rep, _ = self.provision(unattended=True, proc=self.proc.path / "missing")
        self.assertFalse(rep.deferred)
        self.assertTrue(rep.warnings)

    def test_esde_started_just_before_the_write_defers_too(self) -> None:
        self.write_settings()
        real = provision_mod.settings_state.backup

        def backup_then_start(*args, **kw):
            result = real(*args, **kw)
            self.start_esde()
            return result

        with mock.patch("warpzone.provision.settings_state.backup", backup_then_start):
            rep, _ = self.provision(unattended=True)
        self.assertTrue(rep.deferred)

    def test_busy_marks_the_run_as_deferred(self) -> None:
        self.start_esde()
        rep, _ = reporter()
        self.assertTrue(
            provision_mod._busy(
                esde.ESDE_PROCESSES, "ES-DE", "a file", "run again", rep, self.proc.path
            )
        )
        self.assertTrue(rep.deferred)
        rep, _ = reporter()
        self.proc.clear()
        self.assertFalse(
            provision_mod._busy(
                esde.ESDE_PROCESSES, "ES-DE", "a file", "run again", rep, self.proc.path
            )
        )
        self.assertFalse(rep.deferred)


if __name__ == "__main__":
    unittest.main()
