import dataclasses
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path
from typing import Any, ClassVar
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
from warpzone import esde, provision_emulators, settings_state
from warpzone import provision_device as provision_device_module
from warpzone.config import Config, parse_config
from warpzone.model import NO_VERSIONING, hub_specs
from warpzone.probes import DeviceProbes
from warpzone.profiles import Profile, load_profile
from warpzone.provision import (
    ProvisionError,
    check_hub,
    folder_status,
    provision_device,
    provision_hub,
)
from warpzone.provision_folders import folder_patch, undescribed, wait_for_files
from warpzone.provision_hub import (
    additive_plan,
    provision_hub_additive,
    unclaimed,
    unsubscribed,
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
                "final ignores set, rescanned",
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

    def test_a_paused_folder_with_the_right_ignores_is_resumed(self) -> None:
        # What an additive run leaves when its resume failed after the ignores were set.
        with FakeSyncthing(HUB_ID) as fake:
            cfg = make_config(fake.url)
            run_hub(cfg, PROFILES, Client(fake.url, API_KEY), reporter()[0])
            fake.folders["wz-tools"]["paused"] = True
            fake.requests.clear()
            rep, lines = reporter()
            run_hub(cfg, PROFILES, Client(fake.url, API_KEY), rep)
            self.assertFalse(fake.folders["wz-tools"]["paused"])
            writes = [(m, p, b) for m, p, b in fake.writes()]
            self.assertEqual([p for _, p, _ in writes], ["/rest/config/folders/wz-tools"])
            self.assertEqual(writes[0][2], {"paused": False})
            self.assertIn("wz-tools: was paused, resumed", lines)
            self.assertFalse([w for w in rep.warnings if w.startswith("wz-tools")])

    def test_every_paused_hub_folder_is_resumed_and_running_ones_are_not_touched(self) -> None:
        with FakeSyncthing(HUB_ID) as fake:
            cfg = make_config(fake.url)
            run_hub(cfg, PROFILES, Client(fake.url, API_KEY), reporter()[0])
            for folder_id in ("wz-library", "wz-songs"):  # the whitelisted ones too
                fake.folders[folder_id]["paused"] = True
            fake.requests.clear()
            run_hub(cfg, PROFILES, Client(fake.url, API_KEY), reporter()[0])
            self.assertFalse([f for f, v in fake.folders.items() if v["paused"]])
            self.assertEqual(
                sorted(p for _, p, _ in fake.writes()),
                ["/rest/config/folders/wz-library", "/rest/config/folders/wz-songs"],
            )

    def test_a_paused_folder_is_only_reported_in_a_dry_run(self) -> None:
        with FakeSyncthing(HUB_ID) as fake:
            cfg = make_config(fake.url)
            run_hub(cfg, PROFILES, Client(fake.url, API_KEY), reporter()[0])
            fake.folders["wz-tools"]["paused"] = True
            fake.requests.clear()
            sent: list[str] = []
            client = Client(fake.url, API_KEY, label="hub", dry_run=True, sink=sent.append)
            rep, lines = reporter()
            run_hub(cfg, PROFILES, client, rep)
            self.assertEqual(fake.writes(), [])
            self.assertTrue(fake.folders["wz-tools"]["paused"])
            self.assertIn("wz-tools: was paused, resumed", lines)
            self.assertTrue(any("wz-tools" in line and "paused" in line for line in sent))

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

    def test_an_empty_hub_gets_every_folder_the_config_describes(self) -> None:
        with FakeSyncthing(HUB_ID) as fake:
            cfg = make_config(fake.url)
            run_hub(cfg, PROFILES, Client(fake.url, API_KEY), reporter()[0])
            specs = hub_specs(cfg, PROFILES)
            self.assertEqual(set(fake.folders), {spec.id for spec in specs})
            for spec in specs:
                folder = fake.folders[spec.id]
                self.assertEqual({k: folder[k] for k in spec.body()}, spec.body(), spec.id)
                self.assertFalse(folder["paused"], spec.id)
                self.assertEqual(fake.ignores[spec.id], list(spec.ignores), spec.id)

    def test_a_second_run_reports_every_folder_and_device_unchanged(self) -> None:
        with FakeSyncthing(HUB_ID) as fake:
            cfg = make_config(fake.url)
            run_hub(cfg, PROFILES, Client(fake.url, API_KEY), reporter()[0])
            names = ["patterns-common.stignore", "patterns-deck.stignore"]
            fake.browse = {fid: [*names, "patterns-machine.stignore"] for fid in fake.folders}
            rep, lines = reporter()
            run_hub(cfg, PROFILES, Client(fake.url, API_KEY), rep)
            self.assertEqual(
                lines,
                [
                    *(f"device {d.name}: already known" for d in cfg.devices.values()),
                    *(f"{spec.id}: unchanged" for spec in hub_specs(cfg, PROFILES)),
                ],
            )

    def test_hub_id_mismatch(self) -> None:
        with FakeSyncthing(DECK_ID) as fake:
            with self.assertRaises(ProvisionError) as ctx:
                run_hub(make_config(fake.url), PROFILES, Client(fake.url, API_KEY), reporter()[0])
            self.assertIn("hub.device_id", str(ctx.exception))
            self.assertEqual(fake.writes(), [])

    def test_updates_only_owned_keys_and_keeps_devices_the_config_does_not_know(self) -> None:
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

    def unshare_setup(self, fake: FakeSyncthing) -> Config:
        """A hub provisioned from the test config, with the deck also on the YARG folder."""
        cfg = make_config(fake.url)
        run_hub(cfg, PROFILES, Client(fake.url, API_KEY), reporter()[0])
        names = ["patterns-common.stignore", "patterns-deck.stignore", "patterns-machine.stignore"]
        fake.browse = {fid: names for fid in fake.folders}  # no missing-whitelist warnings
        fake.folders["wz-save-yarg-data"]["devices"] = [
            {"deviceID": HUB_ID},
            {"deviceID": MACHINE_ID},
            {"deviceID": DECK_ID},
        ]
        fake.requests.clear()
        return cfg

    @staticmethod
    def shared_with(fake: FakeSyncthing, folder_id: str) -> list[str]:
        return [d["deviceID"] for d in fake.folders[folder_id]["devices"]]

    def test_yarg_data_is_unshared_from_a_device_without_songs(self) -> None:
        with FakeSyncthing(HUB_ID) as fake:
            cfg = self.unshare_setup(fake)
            rep, lines = reporter()
            run_hub(cfg, PROFILES, Client(fake.url, API_KEY), rep)
            patches = [(p, b) for m, p, b in fake.writes() if m == "PATCH"]
            self.assertEqual(
                patches,
                [
                    (
                        "/rest/config/folders/wz-save-yarg-data",
                        {"devices": [{"deviceID": HUB_ID}, {"deviceID": MACHINE_ID}]},
                    )
                ],
            )
            self.assertEqual(self.shared_with(fake, "wz-save-yarg-data"), [HUB_ID, MACHINE_ID])
            line = next(ln for ln in lines if ln.startswith("wz-save-yarg-data:"))
            self.assertIn(
                "stopped sharing with deck (no longer subscribes; no file is deleted)", line
            )
            self.assertEqual(rep.warnings, [])

    def test_a_device_that_drops_a_group_is_unshared_from_its_folders(self) -> None:
        with FakeSyncthing(HUB_ID) as fake:
            data = config_data(fake.url)
            run_hub(parse_config(data), PROFILES, Client(fake.url, API_KEY), reporter()[0])
            data["devices"]["machine"]["folders"] = ["library", "songs"]
            fake.requests.clear()
            run_hub(parse_config(data), PROFILES, Client(fake.url, API_KEY), reporter()[0])
            saves = {f for f in fake.folders if f.startswith("wz-save-")} - {"wz-save-yarg-data"}
            self.assertIn("wz-save-ryujinx-user", saves)
            for folder_id in saves:
                self.assertEqual(self.shared_with(fake, folder_id), [DECK_ID], folder_id)
            self.assertEqual(self.shared_with(fake, "wz-save-yarg-data"), [MACHINE_ID])
            self.assertEqual(self.shared_with(fake, "wz-library"), [DECK_ID, MACHINE_ID])
            patched = {p.rsplit("/", 1)[1] for m, p, b in fake.writes() if m == "PATCH"}
            self.assertEqual(patched, saves)

    def test_kept_entries_keep_their_fields(self) -> None:
        with FakeSyncthing(HUB_ID) as fake:
            cfg = self.unshare_setup(fake)
            hub_entry = {"deviceID": HUB_ID, "introducedBy": "", "encryptionPassword": ""}
            machine_entry = {
                "deviceID": MACHINE_ID,
                "introducedBy": "SOME-INTRODUCER",
                "encryptionPassword": "fake-secret",  # gitleaks:allow (test fixture)
            }
            fake.folders["wz-save-yarg-data"]["devices"] = [
                hub_entry,
                {"deviceID": DECK_ID},
                machine_entry,
            ]
            rep, _ = reporter()
            run_hub(cfg, PROFILES, Client(fake.url, API_KEY), rep)
            self.assertEqual(
                fake.folders["wz-save-yarg-data"]["devices"], [hub_entry, machine_entry]
            )
            self.assertEqual(rep.warnings, [])

    def test_dry_run_shows_the_removal_and_leaks_no_secret(self) -> None:
        secret = "fake-secret"  # gitleaks:allow (test fixture)
        with FakeSyncthing(HUB_ID) as fake:
            cfg = self.unshare_setup(fake)
            fake.folders["wz-save-yarg-data"]["devices"][1]["encryptionPassword"] = secret
            sink: list[str] = []
            rep, lines = reporter()
            client = Client(fake.url, API_KEY, label="hub", dry_run=True, sink=sink.append)
            run_hub(cfg, PROFILES, client, rep)
            self.assertEqual(fake.writes(), [])
            self.assertTrue(
                any("PATCH /rest/config/folders/wz-save-yarg-data" in ln for ln in sink), sink
            )
            line = next(ln for ln in lines if ln.startswith("wz-save-yarg-data:"))
            self.assertIn("dry-run: would stop sharing with deck", line)
            everything = "\n".join([*sink, *lines, *rep.warnings])
            self.assertNotIn(secret, everything)
            self.assertNotIn(API_KEY, everything)
            self.assertEqual(
                self.shared_with(fake, "wz-save-yarg-data"), [HUB_ID, MACHINE_ID, DECK_ID]
            )

    def test_the_removal_is_decided_again_right_before_the_write(self) -> None:
        with FakeSyncthing(HUB_ID) as fake:
            cfg = self.unshare_setup(fake)
            original = fake.route
            reads: list[int] = []

            def route(method, path, query, body):
                result = original(method, path, query, body)
                if (method, path) == ("GET", "/rest/config/folders"):
                    reads.append(1)
                    if len(reads) == 1:
                        # The first read still lists the deck; now someone takes it off by
                        # hand. The folder is replaced, so the answer just built is unchanged.
                        folder = fake.folders["wz-save-yarg-data"]
                        devices = [d for d in folder["devices"] if d["deviceID"] != DECK_ID]
                        fake.folders["wz-save-yarg-data"] = {**folder, "devices": devices}
                return result

            fake.route = route
            rep, lines = reporter()
            run_hub(cfg, PROFILES, Client(fake.url, API_KEY), rep)
            self.assertGreaterEqual(len(reads), 2)
            self.assertEqual([w for w in fake.writes() if "wz-save-yarg-data" in w[1]], [])
            self.assertEqual(self.shared_with(fake, "wz-save-yarg-data"), [HUB_ID, MACHINE_ID])
            line = next(ln for ln in lines if ln.startswith("wz-save-yarg-data:"))
            self.assertNotIn("stop", line)

    def test_a_wz_folder_no_device_subscribes_to_is_reported_and_left_alone(self) -> None:
        with FakeSyncthing(HUB_ID) as fake:
            cfg = make_config(fake.url)
            run_hub(cfg, PROFILES, Client(fake.url, API_KEY), reporter()[0])
            old = {
                "id": "wz-old",
                "path": "/data/games/old",
                "type": "sendreceive",
                "devices": [{"deviceID": HUB_ID}, {"deviceID": DECK_ID}],
            }
            fake.folders["wz-old"] = dict(old)
            fake.requests.clear()
            rep, _ = reporter()
            run_hub(cfg, PROFILES, Client(fake.url, API_KEY), rep)
            self.assertEqual(
                [w for w in rep.warnings if "wz-old" in w],
                [
                    "wz-old: no device in the config subscribes to it, still shared with deck "
                    "(left as it is; remove them from it in the hub's Syncthing if they "
                    "should not have it)"
                ],
            )
            self.assertEqual(fake.folders["wz-old"], old)
            self.assertFalse([w for w in fake.writes() if "wz-old" in w[1]])

    def test_an_auto_accepting_device_is_unshared_with_a_warning(self) -> None:
        with FakeSyncthing(HUB_ID) as fake:
            cfg = self.unshare_setup(fake)
            for device in fake.devices:
                if device["deviceID"] == DECK_ID:
                    device["autoAcceptFolders"] = True
            rep, _ = reporter()
            run_hub(cfg, PROFILES, Client(fake.url, API_KEY), rep)
            self.assertEqual(self.shared_with(fake, "wz-save-yarg-data"), [HUB_ID, MACHINE_ID])
            self.assertEqual(
                rep.warnings,
                [
                    "wz-save-yarg-data: deck may be shared again by the hub's Syncthing "
                    "(auto-accept or an introducer); turn that off for deck on the hub"
                ],
            )

    def test_a_dropped_entry_with_an_introducer_warns_too(self) -> None:
        with FakeSyncthing(HUB_ID) as fake:
            cfg = self.unshare_setup(fake)
            fake.folders["wz-save-yarg-data"]["devices"][2]["introducedBy"] = "SOME-INTRODUCER"
            rep, _ = reporter()
            run_hub(cfg, PROFILES, Client(fake.url, API_KEY), rep)
            self.assertEqual(self.shared_with(fake, "wz-save-yarg-data"), [HUB_ID, MACHINE_ID])
            self.assertEqual(len(rep.warnings), 1)
            self.assertIn("deck may be shared again", rep.warnings[0])

    def test_a_second_run_after_a_removal_writes_nothing(self) -> None:
        with FakeSyncthing(HUB_ID) as fake:
            cfg = self.unshare_setup(fake)
            run_hub(cfg, PROFILES, Client(fake.url, API_KEY), reporter()[0])
            fake.requests.clear()
            rep, lines = reporter()
            run_hub(cfg, PROFILES, Client(fake.url, API_KEY), rep)
            self.assertEqual(fake.writes(), [])
            self.assertIn("wz-save-yarg-data: unchanged", lines)

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
        auto_hub=False,
        **extra,
    ):
        data = config_data(hub_fake.url if hub_fake else "http://127.0.0.1:1")
        if folders is not None:
            data["devices"]["deck"]["folders"] = folders
        if auto_hub:
            data["devices"]["deck"]["auto_hub"] = True
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
            profiles=PROFILES,
            wait_timeout=timeout,
            poll=1.0,
            probes=DeviceProbes(sleep=(sleeps.append if sleeps is not None else lambda s: None)),
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
            self.assertEqual(sum("skipped" in w for w in rep.warnings), 34)

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
                probes=DeviceProbes(sleep=lambda s: None),
            )
            self.assertTrue(any("pre-check" in w for w in rep.warnings))
            self.assertEqual(local.ignores["wz-library"], DECK_LIBRARY_IGNORES)

    def test_a_paused_device_folder_with_the_right_ignores_is_left_alone(self) -> None:
        # Only the hub run resumes such a folder: a device folder is paused for a reason
        # only the person at the console knows (the menu, a manual pause).
        with FakeSyncthing(DECK_ID) as local, FakeSyncthing(HUB_ID) as hub:
            self.run_device(local, hub, folders=["tools"])
            local.folders["wz-tools"]["paused"] = True
            local.requests.clear()
            self.run_device(local, hub, folders=["tools"])
            self.assertTrue(local.folders["wz-tools"]["paused"])
            self.assertEqual(local.writes(), [])

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


class LeftoverFolderTests(DeviceFixture):
    """A `wz-*` folder the device's config no longer describes is reported and left alone."""

    LEFT = "wz-songs"  # a group the deck's config does not list

    def provisioned(self, local: FakeSyncthing, hub: FakeSyncthing) -> None:
        self.hub_with_whitelists(hub)
        for name in ("patterns-common.stignore", "patterns-deck.stignore"):
            local.arrives_after[("wz-library", name)] = 0
        self.run_device(local, hub)
        for folder in local.folders.values():
            (Path(folder["path"]) / ".stfolder").mkdir(parents=True, exist_ok=True)
        local.folders[self.LEFT] = {
            "id": self.LEFT,
            "path": str(self.home / "Songs"),
            "type": "receiveonly",
            "paused": False,
        }
        local.requests.clear()

    def about_it(self, rep) -> list[str]:
        return [w for w in rep.warnings if w.startswith(f"{self.LEFT} ")]

    def writes_for_it(self, local: FakeSyncthing) -> list[tuple[str, str, Any]]:
        return [w for w in local.writes() if self.LEFT in w[1] or (w[2] and self.LEFT in str(w[2]))]

    def test_a_real_run_warns_and_sends_nothing_for_it(self) -> None:
        with FakeSyncthing(DECK_ID) as local, FakeSyncthing(HUB_ID) as hub:
            self.provisioned(local, hub)
            rep, _ = self.run_device(local, hub)
            (warning,) = self.about_it(rep)
            self.assertIn(str(self.home / "Songs"), warning)
            self.assertIn("no longer describes it", warning)
            self.assertEqual(self.writes_for_it(local), [])
            self.assertIn(self.LEFT, local.folders)

    def test_a_dry_run_warns_too(self) -> None:
        with FakeSyncthing(DECK_ID) as local, FakeSyncthing(HUB_ID) as hub:
            self.provisioned(local, hub)
            rep, _ = self.run_device(local, hub, dry_run=True)
            self.assertEqual(len(self.about_it(rep)), 1)
            self.assertEqual(local.writes(), [])

    def test_an_unattended_run_warns_and_writes_nothing(self) -> None:
        with FakeSyncthing(DECK_ID) as local, FakeSyncthing(HUB_ID) as hub:
            self.provisioned(local, hub)
            rep, _ = self.run_device(local, hub, unattended=True, timeout=0.0)
            self.assertEqual(len(self.about_it(rep)), 1)
            self.assertEqual(self.writes_for_it(local), [])

    def test_a_folder_the_config_describes_is_not_reported(self) -> None:
        with FakeSyncthing(DECK_ID) as local, FakeSyncthing(HUB_ID) as hub:
            self.provisioned(local, hub)
            rep, _ = self.run_device(local, hub)
            self.assertEqual(
                [w for w in rep.warnings if "no longer describes" in w and self.LEFT not in w],
                [],
            )


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


def _hub_specs():
    from warpzone.model import hub_specs

    return hub_specs(make_config(), PROFILES)


class MovedSaveFolderTests(DeviceFixture):
    """A save folder whose profile path moved (shadPS4 keeps saves per user now)."""

    FOLDER = "wz-save-shadps4-saves"

    def setUp(self) -> None:
        super().setUp()
        self.old = self.home / ".local/share/shadPS4/savedata"
        self.new = self.home / ".local/share/shadPS4/home/1000/savedata"
        self.new.mkdir(parents=True)

    def install(self, local: FakeSyncthing, hub: FakeSyncthing, path: Path) -> None:
        """The device has the folder at `path`, as an older release left it."""
        hub.folders[self.FOLDER] = {"id": self.FOLDER}
        local.folders[self.FOLDER] = {
            "id": self.FOLDER,
            "path": str(path),
            "type": "sendreceive",
            "versioning": dict(NO_VERSIONING),
            "paused": False,
        }
        local.ignores[self.FOLDER] = []

    def folder_writes(self, local: FakeSyncthing) -> list[tuple[str, str, Any]]:
        return [
            w
            for w in local.writes()
            if self.FOLDER in w[1] or (w[2] or {}).get("id") == self.FOLDER
        ]

    def provision(self, local: FakeSyncthing, hub: FakeSyncthing, **kw: Any):
        return self.run_device(local, hub, folders=["saves"], timeout=0.0, **kw)

    def test_an_empty_old_folder_is_removed_and_added_at_the_new_path(self) -> None:
        for unattended in (False, True):
            with self.subTest(unattended=unattended):
                self.old.mkdir(parents=True, exist_ok=True)
                (self.old / ".stfolder").mkdir(exist_ok=True)
                with FakeSyncthing(DECK_ID) as local, FakeSyncthing(HUB_ID) as hub:
                    self.install(local, hub, self.old)
                    _, lines = self.provision(local, hub, unattended=unattended)
                    self.assertEqual(local.folders[self.FOLDER]["path"], str(self.new))
                    deletes = [r for r in local.writes() if r[0] == "DELETE"]
                    self.assertEqual(
                        deletes, [("DELETE", f"/rest/config/folders/{self.FOLDER}", None)]
                    )
                    # Nothing but Syncthing's configuration changed: the old directory is as it was.
                    self.assertEqual(sorted(p.name for p in self.old.iterdir()), [".stfolder"])
                    self.assertTrue(any("moves from" in line for line in lines))

    def test_a_second_run_writes_nothing(self) -> None:
        with FakeSyncthing(DECK_ID) as local, FakeSyncthing(HUB_ID) as hub:
            self.install(local, hub, self.old)
            self.provision(local, hub)
            for folder in local.folders.values():  # the markers Syncthing writes
                (Path(folder["path"]) / ".stfolder").mkdir(parents=True, exist_ok=True)
            local.requests.clear()
            self.provision(local, hub, unattended=True)
            self.assertEqual(self.folder_writes(local), [])

    SYNCED: ClassVar[dict[str, Any]] = {"completion": 100, "needItems": 0, "remoteState": "valid"}

    def old_with_files(self) -> None:
        self.old.mkdir(parents=True, exist_ok=True)
        (self.old / "CUSA00001").mkdir(exist_ok=True)
        (self.old / "CUSA00001" / "save.bin").write_text("fake")

    def test_an_old_folder_with_files_the_hub_has_is_moved_and_the_files_stay(self) -> None:
        # The first console to move uploaded the saves; this one downloaded them into its
        # old folder. Every file is on the hub, so removing the folder touches nothing.
        self.old_with_files()
        for unattended in (False, True):
            with (
                self.subTest(unattended=unattended),
                FakeSyncthing(DECK_ID) as local,
                FakeSyncthing(HUB_ID) as hub,
            ):
                self.install(local, hub, self.old)
                local.completion[(self.FOLDER, HUB_ID)] = dict(self.SYNCED)
                self.provision(local, hub, unattended=unattended)
                self.assertEqual(local.folders[self.FOLDER]["path"], str(self.new))
                self.assertTrue((self.old / "CUSA00001" / "save.bin").is_file())

    def test_an_old_folder_with_files_not_all_on_the_hub_is_refused_and_untouched(self) -> None:
        self.old_with_files()
        not_synced: dict[str, tuple[dict[str, Any] | None, dict[str, Any] | None]] = {
            "hub has no record": (None, None),
            "hub behind": ({**self.SYNCED, "completion": 80, "needItems": 2}, None),
            "hub state unknown": ({**self.SYNCED, "remoteState": "unknown"}, None),
            "local scanning": (dict(self.SYNCED), {"state": "scanning"}),
            "local needs files": (dict(self.SYNCED), {"needTotalItems": 2}),
            "local changes": (dict(self.SYNCED), {"receiveOnlyChangedItems": 1}),
        }
        for name, (completion, status) in not_synced.items():
            for unattended in (False, True):
                with (
                    self.subTest(case=name, unattended=unattended),
                    FakeSyncthing(DECK_ID) as local,
                    FakeSyncthing(HUB_ID) as hub,
                ):
                    self.install(local, hub, self.old)
                    if completion is not None:
                        local.completion[(self.FOLDER, HUB_ID)] = completion
                    if status is not None:
                        local.status[self.FOLDER] = status
                    with self.assertRaises(ProvisionError) as ctx:
                        self.provision(local, hub, unattended=unattended)
                    self.assertEqual(self.folder_writes(local), [])
                    self.assertEqual(local.folders[self.FOLDER]["path"], str(self.old))
                    self.assertTrue((self.old / "CUSA00001" / "save.bin").is_file())
                    message = str(ctx.exception)
                    self.assertIn("remove (or pause) the folder in Syncthing first", message)
                    self.assertIn("files stay on disk", message)
                    self.assertNotIn("move them into", message)

    def test_files_that_appear_before_the_removal_stop_the_move(self) -> None:
        # The old directory was empty when the move was decided; a pull then puts files in it
        # (the hub is not caught up with them), so the check is made again before acting.
        self.old.mkdir(parents=True)
        for unattended in (False, True):
            with (
                self.subTest(unattended=unattended),
                FakeSyncthing(DECK_ID) as local,
                FakeSyncthing(HUB_ID) as hub,
            ):
                self.install(local, hub, self.old)
                real = provision_device_module.ensure_device

                def ensure_device_then_files(*args: Any, real: Any = real, **kw: Any) -> None:
                    real(*args, **kw)
                    (self.old / "CUSA00002").mkdir(exist_ok=True)
                    local.completion[(self.FOLDER, HUB_ID)] = {**self.SYNCED, "completion": 40}

                with (
                    mock.patch.object(
                        provision_device_module, "ensure_device", ensure_device_then_files
                    ),
                    self.assertRaises(ProvisionError) as ctx,
                ):
                    self.provision(local, hub, unattended=unattended)
                self.assertEqual([w for w in local.writes() if w[0] == "DELETE"], [])
                self.assertEqual(local.folders[self.FOLDER]["path"], str(self.old))
                self.assertIn("remove (or pause) the folder in Syncthing first", str(ctx.exception))
                self.assertTrue((self.old / "CUSA00002").is_dir())
                (self.old / "CUSA00002").rmdir()

    def test_an_attended_run_moves_a_folder_whose_directory_it_creates(self) -> None:
        # `moving` is decided after the directories are made, so a created target does not
        # leave the old folder to be refused halfway through the run.
        self.new.rmdir()
        self.new.parent.rmdir()
        marker = mock.Mock(return_value=True)
        with FakeSyncthing(DECK_ID) as local, FakeSyncthing(HUB_ID) as hub:
            self.install(local, hub, self.old)
            with mock.patch.object(provision_device_module, "_create_target", marker):
                marker.side_effect = lambda spec, dry, rep: (
                    Path(spec.path).mkdir(parents=True, exist_ok=True) or True
                    if spec.id == self.FOLDER
                    else False
                )
                self.provision(local, hub)
            self.assertEqual(local.folders[self.FOLDER]["path"], str(self.new))

    def test_a_path_the_profile_never_retired_is_still_refused(self) -> None:
        other = self.home / "elsewhere"
        other.mkdir()
        with FakeSyncthing(DECK_ID) as local, FakeSyncthing(HUB_ID) as hub:
            self.install(local, hub, other)
            with self.assertRaises(ProvisionError):
                self.provision(local, hub)
            self.assertEqual(self.folder_writes(local), [])

    def test_the_old_folder_stays_until_the_new_directory_exists(self) -> None:
        self.new.rmdir()
        (self.new.parent).rmdir()
        with FakeSyncthing(DECK_ID) as local, FakeSyncthing(HUB_ID) as hub:
            self.install(local, hub, self.old)
            rep, _ = self.provision(local, hub, unattended=True)
            self.assertEqual(self.folder_writes(local), [])
            self.assertEqual(local.folders[self.FOLDER]["path"], str(self.old))
            self.assertTrue(any("skipped" in w for w in rep.warnings))

    def test_a_dry_run_changes_nothing(self) -> None:
        with FakeSyncthing(DECK_ID) as local, FakeSyncthing(HUB_ID) as hub:
            self.install(local, hub, self.old)
            self.provision(local, hub, dry_run=True)
            self.assertEqual(self.folder_writes(local), [])
            self.assertEqual(local.folders[self.FOLDER]["path"], str(self.old))


class UnsubscribedTests(unittest.TestCase):
    """Which devices a hub folder stops being shared with (pure), and how it is applied."""

    NAMES: ClassVar[dict[str, str]] = {DECK_ID: "deck", MACHINE_ID: "machine"}

    @staticmethod
    def spec(folder_id: str = "wz-save-yarg-data"):
        # Subscribed to by the machine only.
        return {s.id: s for s in _hub_specs()}[folder_id]

    @staticmethod
    def current(*ids: str) -> dict[str, Any]:
        return {"devices": [{"deviceID": i} for i in ids]}

    def test_a_known_device_that_no_longer_subscribes_is_returned(self) -> None:
        current = self.current(HUB_ID, DECK_ID, MACHINE_ID)
        self.assertEqual(unsubscribed(self.spec(), current, self.NAMES), (DECK_ID,))
        nobody = dataclasses.replace(self.spec(), devices=())
        current = self.current(MACHINE_ID, HUB_ID, DECK_ID)
        # In the folder's order.
        self.assertEqual(unsubscribed(nobody, current, self.NAMES), (MACHINE_ID, DECK_ID))

    def test_a_subscriber_is_never_returned(self) -> None:
        current = self.current(HUB_ID, MACHINE_ID)
        self.assertEqual(unsubscribed(self.spec(), current, self.NAMES), ())

    def test_a_device_the_config_does_not_know_is_never_returned(self) -> None:
        current = self.current(HUB_ID, "OTHER-ID", MACHINE_ID)
        self.assertEqual(unsubscribed(self.spec(), current, self.NAMES), ())

    def test_the_hubs_own_entry_is_never_returned(self) -> None:
        # The hub is never one of the config's devices, whoever the spec lists.
        nobody = dataclasses.replace(self.spec(), devices=())
        current = self.current(HUB_ID)
        self.assertEqual(unsubscribed(nobody, current, self.NAMES), ())
        cfg = make_config()
        self.assertNotIn(HUB_ID, {d.device_id for d in cfg.devices.values()})

    def test_a_folder_without_the_prefix_returns_nothing(self) -> None:
        photos = dataclasses.replace(self.spec(), id="photos")
        current = self.current(HUB_ID, DECK_ID, MACHINE_ID)
        self.assertEqual(unsubscribed(photos, current, self.NAMES), ())

    def test_a_folder_without_devices_returns_nothing(self) -> None:
        self.assertEqual(unsubscribed(self.spec(), {}, self.NAMES), ())
        self.assertEqual(unsubscribed(self.spec(), {"devices": None}, self.NAMES), ())
        self.assertEqual(unsubscribed(self.spec(), {"devices": []}, self.NAMES), ())

    def test_folder_patch_drops_only_the_given_devices_and_keeps_every_field(self) -> None:
        spec = self.spec()
        hub = {"deviceID": HUB_ID, "introducedBy": "", "encryptionPassword": ""}
        machine = {
            "deviceID": MACHINE_ID,
            "introducedBy": "SOME-INTRODUCER",
            "encryptionPassword": "fake-secret",  # gitleaks:allow (test fixture)
            "extra": 1,
        }
        current = {**spec.body(), "devices": [hub, {"deviceID": DECK_ID}, machine]}
        patch = folder_patch(spec, current, drop=(DECK_ID,))
        self.assertEqual(patch, {"devices": [hub, machine]})

    def test_folder_patch_without_drop_never_removes(self) -> None:
        # What `provision device` and the unattended checks call.
        spec = self.spec()
        current = {**spec.body(), "devices": [{"deviceID": HUB_ID}, {"deviceID": DECK_ID}]}
        patch = folder_patch(spec, current)
        self.assertEqual(
            patch["devices"],
            [{"deviceID": HUB_ID}, {"deviceID": DECK_ID}, {"deviceID": MACHINE_ID}],
        )
        current["devices"].append({"deviceID": MACHINE_ID})
        self.assertEqual(folder_patch(spec, current), {})

    def test_folder_patch_adds_and_drops_in_one_list(self) -> None:
        spec = self.spec()
        current = {**spec.body(), "devices": [{"deviceID": DECK_ID}, {"deviceID": HUB_ID}]}
        patch = folder_patch(spec, current, drop=(DECK_ID,))
        self.assertEqual(patch, {"devices": [{"deviceID": HUB_ID}, {"deviceID": MACHINE_ID}]})

    def test_unclaimed_reports_a_wz_folder_shared_with_a_config_device(self) -> None:
        existing = {
            "wz-old": self.current(HUB_ID, DECK_ID, "OTHER-ID", MACHINE_ID),
            "wz-tools": self.current(HUB_ID, DECK_ID),
        }
        self.assertEqual(
            unclaimed(_hub_specs(), existing, self.NAMES),
            (
                "wz-old: no device in the config subscribes to it, still shared with "
                "deck, machine (left as it is; remove them from it in the hub's Syncthing "
                "if they should not have it)",
            ),
        )

    def test_unclaimed_ignores_foreign_and_unshared_folders(self) -> None:
        existing = {
            "photos": self.current(HUB_ID, DECK_ID),  # not ours
            "wz-old": self.current(HUB_ID, "OTHER-ID"),  # no config device on it
            "wz-empty": {"devices": None},
        }
        self.assertEqual(unclaimed(_hub_specs(), existing, self.NAMES), ())


class UndescribedTests(unittest.TestCase):
    """Which `wz-*` folders a device's Syncthing has that its config no longer describes."""

    @staticmethod
    def specs():
        return [s for s in _hub_specs() if s.id in ("wz-library", "wz-tools")]

    @staticmethod
    def folders(*ids: str) -> dict[str, dict[str, Any]]:
        return {i: {"id": i, "path": f"/data/{i}"} for i in ids}

    def test_a_wz_folder_no_spec_describes_gives_one_warning_naming_it(self) -> None:
        existing = self.folders("wz-library", "wz-old")
        (text,) = undescribed(self.specs(), existing)
        self.assertEqual(
            text,
            "wz-old (/data/wz-old): this device's config no longer describes it, so warpzone "
            "leaves it as it is (remove it in this device's Syncthing if it should go; its "
            "files stay on disk)",
        )

    def test_a_folder_with_a_spec_gives_none(self) -> None:
        existing = self.folders("wz-library", "wz-tools")
        self.assertEqual(undescribed(self.specs(), existing), ())

    def test_a_folder_that_is_not_wz_gives_none(self) -> None:
        existing = self.folders("photos", "default", "wzx-old")
        self.assertEqual(undescribed(self.specs(), existing), ())

    def test_several_leftovers_come_in_a_stable_order(self) -> None:
        one = undescribed(self.specs(), self.folders("wz-b", "wz-library", "wz-a", "wz-c"))
        two = undescribed(self.specs(), self.folders("wz-c", "wz-a", "wz-library", "wz-b"))
        self.assertEqual(one, two)
        self.assertEqual([t.split(" ")[0] for t in one], ["wz-a", "wz-b", "wz-c"])

    def test_a_folder_without_a_path_is_still_named(self) -> None:
        (text,) = undescribed(self.specs(), {"wz-old": {"id": "wz-old"}})
        self.assertTrue(text.startswith("wz-old: "))


class AdditivePlanTests(unittest.TestCase):
    """The decision of `provision hub --additive`, with no network."""

    def plan(self, existing=None, only=None):
        names = {DECK_ID: "deck", MACHINE_ID: "machine"}
        return additive_plan(_hub_specs(), existing or {}, names, only)

    def test_an_empty_hub_gets_every_folder_that_is_not_whitelisted(self) -> None:
        plan = self.plan()
        created = {s.id for s in plan.create}
        self.assertNotIn("wz-library", created)
        self.assertNotIn("wz-songs", created)
        self.assertEqual({s.id for s in plan.whitelisted}, {"wz-library", "wz-songs"})
        self.assertIn("wz-tools", created)
        self.assertIn("wz-save-ryujinx-user", created)
        self.assertEqual(plan.differences, ())

    def test_a_folder_the_hub_has_is_never_created_again(self) -> None:
        by_id = {s.id: s for s in _hub_specs()}
        tools = by_id["wz-tools"]
        existing = {
            "wz-tools": {
                "path": tools.path,
                "type": tools.type,
                "devices": [{"deviceID": d} for d in tools.devices],
            }
        }
        plan = self.plan(existing)
        self.assertNotIn("wz-tools", {s.id for s in plan.create})
        self.assertEqual(plan.differences, ())

    def test_differences_are_reported_and_never_acted_on(self) -> None:
        by_id = {s.id: s for s in _hub_specs()}
        tools = by_id["wz-tools"]
        existing = {
            "wz-tools": {
                "path": "/elsewhere",
                "type": "sendreceive",
                "devices": [{"deviceID": MACHINE_ID}],
            }
        }
        plan = self.plan(existing)
        self.assertNotIn("wz-tools", {s.id for s in plan.create})
        text = "\n".join(plan.differences)
        self.assertIn("'/elsewhere'", text)
        self.assertIn(f"not {tools.path!r}", text)
        self.assertIn("type 'sendreceive'", text)
        self.assertIn("not shared with deck", text)

    def test_a_device_that_no_longer_subscribes_is_a_difference(self) -> None:
        yarg = {s.id: s for s in _hub_specs()}["wz-save-yarg-data"]
        existing = {
            "wz-save-yarg-data": {
                "path": yarg.path,
                "type": yarg.type,
                "devices": [{"deviceID": MACHINE_ID}, {"deviceID": DECK_ID}],
            }
        }
        plan = self.plan(existing)
        self.assertEqual(
            plan.differences,
            (
                "wz-save-yarg-data: still shared on the hub with deck "
                "(no longer subscribed in the config)",
            ),
        )

    def test_only_limits_the_run_to_those_folders(self) -> None:
        plan = self.plan(only={"wz-tools", "wz-library"})
        self.assertEqual([s.id for s in plan.create], ["wz-tools"])
        self.assertEqual([s.id for s in plan.whitelisted], ["wz-library"])

    def test_a_trailing_slash_is_not_a_difference(self) -> None:
        tools = {s.id: s for s in _hub_specs()}["wz-tools"]
        existing = {
            "wz-tools": {
                "path": tools.path + "/",
                "type": tools.type,
                "devices": [{"deviceID": d} for d in tools.devices],
            }
        }
        self.assertEqual(self.plan(existing).differences, ())

    def test_a_folder_is_not_created_where_another_folder_already_is(self) -> None:
        tools = {s.id: s for s in _hub_specs()}["wz-tools"]
        # Another ID at the same directory, spelt with a trailing slash and another case.
        existing = {"old-tools": {"id": "old-tools", "path": tools.path.upper() + "/"}}
        plan = self.plan(existing)
        self.assertNotIn("wz-tools", {s.id for s in plan.create})
        self.assertEqual(len(plan.clashes), 1)
        self.assertIn("wz-tools", plan.clashes[0])
        self.assertIn("old-tools", plan.clashes[0])


class AdditiveHubTests(unittest.TestCase):
    def additive(self, fake, **kw):
        rep, lines = reporter()
        created = provision_hub_additive(
            make_config(fake.url), PROFILES, Client(fake.url, API_KEY), rep, **kw
        )
        return created, rep, lines

    def test_creates_the_missing_folders_but_no_whitelisted_one(self) -> None:
        with FakeSyncthing(HUB_ID) as fake:
            created, rep, _ = self.additive(fake)
            self.assertIn("wz-tools", created)
            self.assertNotIn("wz-library", fake.folders)
            self.assertNotIn("wz-songs", fake.folders)
            self.assertEqual(fake.folders["wz-tools"]["type"], "sendonly")
            self.assertFalse(fake.folders["wz-tools"]["paused"])
            self.assertEqual(fake.ignores["wz-tools"][-1], "/logs")
            self.assertEqual({d["deviceID"] for d in fake.devices}, {HUB_ID, DECK_ID, MACHINE_ID})
            self.assertTrue(any("wz-library" in w for w in rep.warnings))

    def test_a_folder_the_hub_has_is_left_exactly_as_it_is(self) -> None:
        with FakeSyncthing(HUB_ID) as fake:
            fake.folders["wz-tools"] = {
                "id": "wz-tools",
                "path": "/elsewhere",
                "type": "sendreceive",
                "label": "mine",
                "devices": [],
            }
            fake.ignores["wz-tools"] = ["custom"]
            before = {k: dict(v) for k, v in fake.folders.items()}
            _, rep, _ = self.additive(fake)
            self.assertEqual(fake.folders["wz-tools"], before["wz-tools"])
            self.assertEqual(fake.ignores["wz-tools"], ["custom"])
            self.assertFalse([w for w in fake.writes() if "wz-tools/" in w[1]])
            self.assertTrue(any("wz-tools" in w and "/elsewhere" in w for w in rep.warnings))

    def test_a_device_that_no_longer_subscribes_is_reported_and_kept(self) -> None:
        with FakeSyncthing(HUB_ID) as fake:
            self.additive(fake)
            fake.folders["wz-save-yarg-data"]["devices"].append({"deviceID": DECK_ID})
            fake.requests.clear()
            created, rep, _ = self.additive(fake)
            self.assertEqual(created, [])
            self.assertEqual(fake.writes(), [])
            self.assertEqual(
                [d["deviceID"] for d in fake.folders["wz-save-yarg-data"]["devices"]],
                [MACHINE_ID, DECK_ID],
            )
            self.assertEqual(
                [w for w in rep.warnings if "yarg" in w],
                [
                    "wz-save-yarg-data: still shared on the hub with deck "
                    "(no longer subscribed in the config) (left as it is; "
                    "`warpzone provision hub` changes it)"
                ],
            )

    def test_the_second_run_writes_nothing(self) -> None:
        with FakeSyncthing(HUB_ID) as fake:
            self.additive(fake)
            fake.requests.clear()
            created, _, _ = self.additive(fake)
            self.assertEqual(created, [])
            self.assertEqual(fake.writes(), [])

    def test_a_folder_that_appears_before_the_write_is_not_created(self) -> None:
        with FakeSyncthing(HUB_ID) as fake:
            original = fake.route
            reads: list[int] = []

            def route(method, path, query, body):
                result = original(method, path, query, body)
                if (method, path) == ("GET", "/rest/config/folders"):
                    reads.append(1)
                    if len(reads) == 1:
                        # The first read answered without it; now someone makes it.
                        fake.folders["wz-tools"] = {
                            "id": "wz-tools",
                            "path": "/theirs",
                            "type": "sendonly",
                            "devices": [],
                        }
                return result

            fake.route = route
            created, _, lines = self.additive(fake)
            self.assertNotIn("wz-tools", created)
            self.assertEqual(fake.folders["wz-tools"]["path"], "/theirs")
            self.assertIn("wz-tools: already on the hub, left as it is", lines)

    def test_a_folder_that_appears_while_devices_are_added_is_not_replaced(self) -> None:
        # POST /rest/config/folders replaces a folder with the same ID, so the last look
        # must come after the device writes, right before the folder is created.
        with FakeSyncthing(HUB_ID) as fake:
            original = fake.route

            def route(method, path, query, body):
                result = original(method, path, query, body)
                if (method, path) == ("POST", "/rest/config/devices"):
                    fake.folders.setdefault(
                        "wz-tools",
                        {"id": "wz-tools", "path": "/theirs", "type": "sendonly", "devices": []},
                    )
                return result

            fake.route = route
            created, _, lines = self.additive(fake, only={"wz-tools"})
            self.assertEqual(created, [])
            self.assertEqual(fake.folders["wz-tools"]["path"], "/theirs")
            self.assertIn("wz-tools: already on the hub, left as it is", lines)

    def test_a_folder_at_the_path_of_another_is_not_created(self) -> None:
        with FakeSyncthing(HUB_ID) as fake:
            tools = {s.id: s for s in _hub_specs()}["wz-tools"]
            fake.folders["old-tools"] = {"id": "old-tools", "path": tools.path, "devices": []}
            created, rep, _ = self.additive(fake, only={"wz-tools"})
            self.assertEqual(created, [])
            self.assertNotIn("wz-tools", fake.folders)
            self.assertEqual(fake.writes(), [])
            self.assertTrue(any("old-tools" in w for w in rep.warnings))

    def test_a_paused_folder_is_reported_and_left_paused(self) -> None:
        with FakeSyncthing(HUB_ID) as fake:
            self.additive(fake)
            fake.folders["wz-tools"]["paused"] = True
            fake.requests.clear()
            created, rep, _ = self.additive(fake)
            self.assertEqual(created, [])
            self.assertEqual(fake.writes(), [])
            self.assertTrue(fake.folders["wz-tools"]["paused"])
            warned = [w for w in rep.warnings if w.startswith("wz-tools") and "paused" in w]
            self.assertEqual(len(warned), 1)
            self.assertIn("provision hub", warned[0])

    def test_a_running_folder_is_not_reported_as_paused(self) -> None:
        with FakeSyncthing(HUB_ID) as fake:
            self.additive(fake)
            _, rep, _ = self.additive(fake)
            self.assertFalse([w for w in rep.warnings if "paused" in w])

    def test_a_folder_that_fails_half_way_is_one_clean_warning(self) -> None:
        with FakeSyncthing(HUB_ID) as fake:
            original = fake.route

            def route(method, path, query, body):
                if (method, path, query.get("folder")) == ("POST", "/rest/db/ignores", "wz-tools"):
                    return 500, "boom"
                return original(method, path, query, body)

            fake.route = route
            created, rep, lines = self.additive(fake, only={"wz-tools"})
            self.assertEqual(created, [])
            self.assertTrue(fake.folders["wz-tools"]["paused"])
            warned = [w for w in rep.warnings if w.startswith("wz-tools")]
            self.assertEqual(len(warned), 1)
            self.assertIn("paused", warned[0])
            self.assertIn("provision hub", warned[0])
            self.assertFalse([ln for ln in lines if ln.startswith("wz-tools: created")])
            # Nothing pending is left to be printed into a later line for the same ID.
            rep.flush("wz-tools")
            self.assertEqual(lines[-1], "wz-tools: unchanged")

    def test_an_api_that_is_not_the_hub_is_refused_before_any_write(self) -> None:
        with FakeSyncthing(DECK_ID) as fake:
            with self.assertRaises(ProvisionError):
                self.additive(fake)
            self.assertEqual(fake.writes(), [])

    def test_dry_run_makes_no_writes(self) -> None:
        with FakeSyncthing(HUB_ID) as fake:
            out: list[str] = []
            rep, _ = reporter()
            provision_hub_additive(
                make_config(fake.url),
                PROFILES,
                Client(fake.url, API_KEY, dry_run=True, sink=out.append),
                rep,
            )
            self.assertEqual(fake.writes(), [])
            self.assertTrue(any("POST /rest/config/folders" in o for o in out))


class AutoHubTests(DeviceFixture):
    """A device with `auto_hub = true` creates the hub folders it needs."""

    def test_the_hub_gets_the_device_folders_that_are_not_whitelisted(self) -> None:
        with FakeSyncthing(DECK_ID) as local, FakeSyncthing(HUB_ID) as hub:
            self.hub_with_whitelists(hub)
            hub.folders["wz-library"] = {
                "id": "wz-library",
                "path": "/data/games/library",
                "type": "sendonly",
                "devices": [{"deviceID": DECK_ID}],
            }
            for name in ("patterns-common.stignore", "patterns-deck.stignore"):
                local.arrives_after[("wz-library", name)] = 0
            self.run_device(local, hub, auto_hub=True)
            self.assertIn("wz-tools", hub.folders)
            self.assertIn("wz-media", hub.folders)
            self.assertIn("wz-save-retroarch-saves", hub.folders)
            self.assertIn("wz-tools", local.folders)
            # What the device does not have is not made, and neither is another device's.
            self.assertNotIn("wz-songs", hub.folders)
            self.assertNotIn("wz-save-ryujinx-user", hub.folders)  # its directory is not there

    def test_without_auto_hub_the_hub_is_not_written(self) -> None:
        with FakeSyncthing(DECK_ID) as local, FakeSyncthing(HUB_ID) as hub:
            self.hub_with_whitelists(hub)
            hub.folders["wz-library"] = {"id": "wz-library", "path": "/x", "devices": []}
            for name in ("patterns-common.stignore", "patterns-deck.stignore"):
                local.arrives_after[("wz-library", name)] = 0
            self.run_device(local, hub)
            self.assertEqual(hub.writes(), [])

    def test_a_missing_whitelisted_folder_is_still_for_a_person(self) -> None:
        with FakeSyncthing(DECK_ID) as local, FakeSyncthing(HUB_ID) as hub:
            rep, _ = self.run_device(local, hub, unattended=True, auto_hub=True, timeout=0.0)
            self.assertNotIn("wz-library", hub.folders)
            self.assertNotIn("wz-library", local.folders)
            self.assertTrue(any("wz-library" in w and "provision hub" in w for w in rep.warnings))
            self.assertIn("wz-tools", hub.folders)
            self.assertEqual(
                [w for w in rep.warnings if "whitelisted folders are made by" in w], []
            )

    def test_a_hub_that_answers_as_another_device_is_a_warning_not_a_failure(self) -> None:
        with FakeSyncthing(DECK_ID) as local, FakeSyncthing(MACHINE_ID) as hub:
            rep, _ = self.run_device(local, hub, unattended=True, auto_hub=True, timeout=0.0)
            self.assertEqual(hub.writes(), [])
            self.assertTrue(any(w.startswith("auto_hub:") for w in rep.warnings))
            self.assertIn("wz-tools", local.folders)

    def test_a_hub_that_refuses_the_key_is_a_warning_not_a_failure(self) -> None:
        with FakeSyncthing(DECK_ID) as local, FakeSyncthing(HUB_ID, api_key="other") as hub:
            rep, _ = self.run_device(local, hub, unattended=True, auto_hub=True, timeout=0.0)
            self.assertTrue(any(w.startswith("auto_hub:") for w in rep.warnings))

    def test_a_dry_run_of_the_device_never_writes_to_the_hub(self) -> None:
        with FakeSyncthing(DECK_ID) as local, FakeSyncthing(HUB_ID) as hub:
            self.hub_with_whitelists(hub)
            hub.folders["wz-library"] = {"id": "wz-library", "path": "/x", "devices": []}
            self.run_device(local, hub, dry_run=True, auto_hub=True)
            self.assertEqual(hub.writes(), [])
            self.assertEqual(local.writes(), [])

    def test_an_existing_hub_folder_is_not_changed_by_a_device_with_another_view(self) -> None:
        with FakeSyncthing(DECK_ID) as local, FakeSyncthing(HUB_ID) as hub:
            self.hub_with_whitelists(hub)
            hub.folders["wz-library"] = {"id": "wz-library", "path": "/x", "devices": []}
            hub.folders["wz-tools"] = {
                "id": "wz-tools",
                "path": "/elsewhere",
                "type": "sendreceive",
                "devices": [],
            }
            for name in ("patterns-common.stignore", "patterns-deck.stignore"):
                local.arrives_after[("wz-library", name)] = 0
            before = dict(hub.folders["wz-tools"])
            self.run_device(local, hub, auto_hub=True)
            self.assertEqual(hub.folders["wz-tools"], before)


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
                probes=DeviceProbes(sleep=lambda s: None, flatpak=flatpak),
                syncthing_config=path.get(config),
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

    def probes(self, **fakes: Any) -> DeviceProbes:
        return DeviceProbes(
            **{"import_check": lambda python: True, "proc": self.proc.path, "sleep": lambda s: None}
            | fakes
        )

    def provision(self, *, folders=("settings",), dry_run=False, profile=None, **kw):
        data = config_data()
        data["devices"]["deck"]["folders"] = list(folders)
        cfg = parse_config(data)
        rep, lines = reporter()
        kw.setdefault("probes", self.probes())
        # The settings timer has its own tests (test_settings_timer.py).
        with (
            mock.patch("warpzone.provision_device.ensure_settings_timer"),
            FakeSyncthing(DECK_ID) as local,
        ):
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

        rep, _ = self.provision(probes=self.probes(import_check=cannot))
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
            mock.patch("warpzone.runners.subprocess.run", fake_run),
        ):
            self.assertTrue(run_import_check(PYTHON))
        ((command, kw),) = calls
        self.assertEqual(command, [PYTHON, "-c", "import warpzone"])
        self.assertEqual(kw["cwd"], "/")
        self.assertEqual(kw["env"].get("KEEP"), "1")
        self.assertNotIn("PYTHONPATH", kw["env"])
        self.assertNotIn("PYTHONHOME", kw["env"])
        with mock.patch(
            "warpzone.runners.subprocess.run",
            lambda command, **kw: subprocess.CompletedProcess(command, 1),
        ):
            self.assertFalse(run_import_check(PYTHON))
        with mock.patch("warpzone.runners.subprocess.run", side_effect=FileNotFoundError):
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
        rep, _ = self.provision(probes=self.probes(proc=self.proc.path / "missing"))
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
        with mock.patch("warpzone.safefile.atomic_write", return_value=False):
            rep, _ = self.provision()
        self.assertEqual(self.settings.read_text(), ES_SETTINGS)
        (warning,) = rep.warnings
        self.assertIn("changed while", warning)

    def test_esde_started_just_before_the_write_stops_the_edit(self) -> None:
        self.write_settings()
        real = settings_state.backup

        def backup_then_start(*args, **kw):
            result = real(*args, **kw)
            self.start_esde()
            return result

        with mock.patch("warpzone.settings_state.backup", backup_then_start):
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
        rep, _ = self.provision(
            unattended=True, probes=self.probes(proc=self.proc.path / "missing")
        )
        self.assertFalse(rep.deferred)
        self.assertTrue(rep.warnings)

    def test_esde_started_just_before_the_write_defers_too(self) -> None:
        self.write_settings()
        real = settings_state.backup

        def backup_then_start(*args, **kw):
            result = real(*args, **kw)
            self.start_esde()
            return result

        with mock.patch("warpzone.settings_state.backup", backup_then_start):
            rep, _ = self.provision(unattended=True)
        self.assertTrue(rep.deferred)

    def test_busy_marks_the_run_as_deferred(self) -> None:
        self.start_esde()
        rep, _ = reporter()
        self.assertTrue(
            provision_emulators.busy(
                esde.ESDE_PROCESSES,
                "ES-DE",
                "a file",
                "run again",
                rep,
                DeviceProbes(proc=self.proc.path),
            )
        )
        self.assertTrue(rep.deferred)
        rep, _ = reporter()
        self.proc.clear()
        self.assertFalse(
            provision_emulators.busy(
                esde.ESDE_PROCESSES,
                "ES-DE",
                "a file",
                "run again",
                rep,
                DeviceProbes(proc=self.proc.path),
            )
        )
        self.assertFalse(rep.deferred)


if __name__ == "__main__":
    unittest.main()
