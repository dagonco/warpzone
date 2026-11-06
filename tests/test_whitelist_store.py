import os
import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path
from unittest import mock

from support import API_KEY, DECK_ID, HUB_ID, MACHINE_ID, FakeSyncthing, config_data
from test_cli import run
from warpzone import safefile, whitelist_store
from warpzone import whitelist as wl
from warpzone.config import parse_config
from warpzone.profiles import available_profiles, load_profile
from warpzone.syncthing import Client

HAND = """\
// deck
#include patterns-common.stignore

!/roms/snes
!/roms/psx
!/roms/wii/Example Game \\[ABCD01\\].rvz
!/roms/ps4/games/CUSA00000
!/roms/ps4/shortcuts/Example Game.desktop
!/roms/psvita/app/PCSE00000
!/roms/psvita/license/PCSE00000
!/roms/psvita/Example Vita Game.psvita
!/bios

*
"""

LIBRARY_FILES = [
    "patterns-common.stignore",
    "roms/snes/Example (USA).sfc",
    "roms/snes/disc/Extra.bin",
    "roms/psx/Example Game (Europe).chd",
    "roms/n64/Example (USA).z64",
    "roms/wii/Example Game [ABCD01].rvz",
    "roms/wii/Other Game [WXYZ01].rvz",
    "roms/ps4/games/CUSA00000/eboot.bin",
    "roms/ps4/games/CUSA00001/eboot.bin",
    "roms/ps4/shortcuts/Example Game.desktop",
    "roms/ps4/shortcuts/Other Game.desktop",
    "roms/psvita/app/PCSE00000/eboot.bin",
    "roms/psvita/license/PCSE00000/license.rif",
    "roms/psvita/Example Vita Game.psvita",
    "roms/psvita/Another Game.psvita",
    "bios/psx/scph-fake.bin",
    "roms/snes/.DS_Store",
    "roms/n64/._Example (USA).z64",
    "roms/n64/@eaDir/thumb.jpg",
]

NOW = datetime(2026, 11, 6, 10, 0, 0, 123456, tzinfo=UTC)


class StoreCase(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name) / "hub"
        self.library = self.root / "library"
        for name in LIBRARY_FILES:
            path = self.library / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("x")
        self.log: list[str] = []
        self.warnings: list[str] = []

    def put(self, device: str, text: str) -> Path:
        path = whitelist_store.whitelist_path(self.root, device)
        path.write_text(text)
        return path

    def migrate(self, *devices: str, dry_run: bool = False, client: Client | None = None) -> int:
        return whitelist_store.migrate(
            self.root,
            list(devices),
            dry_run=dry_run,
            out=self.log.append,
            warn=self.warnings.append,
            scan_client=(lambda: client) if client else None,
            now=NOW,
        )


class SnapshotAndWriteTests(StoreCase):
    def test_snapshot_etag_and_absent(self) -> None:
        path = whitelist_store.whitelist_path(self.root, "deck")
        missing = whitelist_store.read(path)
        self.assertIsNone(missing.data)
        self.assertEqual(missing.etag, "absent")
        path.write_bytes(b"abc\n")
        snap = whitelist_store.read(path)
        self.assertEqual(snap.data, b"abc\n")
        self.assertEqual(snap.etag, wl.etag(b"abc\n"))
        self.assertEqual(snap.stamp, safefile.stamp(path))

    def test_write_backs_up_the_old_file_and_replaces_it(self) -> None:
        path = self.put("deck", "old\n")
        snap = whitelist_store.read(path)
        result = whitelist_store.write(self.root, "deck", b"new\n", snap, now=NOW)
        self.assertTrue(result.ok)
        self.assertEqual(path.read_bytes(), b"new\n")
        assert result.backup is not None
        self.assertEqual(result.backup.read_bytes(), b"old\n")
        self.assertEqual(
            result.backup,
            self.root / "catalog/whitelists/deck/20261106T100000.123456Z.stignore",
        )

    def test_write_refuses_a_file_that_changed_since_it_was_read(self) -> None:
        path = self.put("deck", "old\n")
        snap = whitelist_store.read(path)
        path.write_text("someone else\n")
        result = whitelist_store.write(self.root, "deck", b"new\n", snap, now=NOW)
        self.assertFalse(result.ok)
        self.assertEqual(path.read_text(), "someone else\n")

    def test_a_missing_file_is_created_and_one_that_appeared_is_not_overwritten(self) -> None:
        path = whitelist_store.whitelist_path(self.root, "deck")
        snap = whitelist_store.read(path)
        self.assertTrue(whitelist_store.write(self.root, "deck", b"first\n", snap).ok)
        self.assertEqual(path.read_bytes(), b"first\n")
        self.assertFalse((self.root / "catalog").exists())  # nothing to back up
        self.assertFalse(whitelist_store.write(self.root, "deck", b"second\n", snap).ok)
        self.assertEqual(path.read_bytes(), b"first\n")

    def test_the_fifty_newest_backups_are_kept(self) -> None:
        directory = whitelist_store.backups_dir(self.root, "deck")
        for n in range(53):
            stamp = NOW.replace(second=0, microsecond=0, minute=0, hour=n % 24, day=1 + n // 24)
            whitelist_store.backup(self.root, "deck", f"v{n}".encode(), stamp)
        names = sorted(p.name for p in directory.iterdir())
        self.assertEqual(len(names), 50)
        self.assertEqual((directory / names[-1]).read_bytes(), b"v52")
        self.assertEqual((directory / names[0]).read_bytes(), b"v3")

    def test_replace_checks_the_etag_and_scans(self) -> None:
        path = self.put("deck", "old\n")
        with FakeSyncthing(HUB_ID) as hub:
            client = Client(hub.url, API_KEY, label="hub")
            etag = wl.etag(b"old\n")
            with self.assertRaises(whitelist_store.Stale):
                whitelist_store.replace(self.root, "deck", "0" * 64, lambda s: b"x\n", now=NOW)
            self.assertEqual(path.read_text(), "old\n")
            warnings = whitelist_store.replace(
                self.root, "deck", etag, lambda s: b"new\n", now=NOW, client=client
            )
            self.assertEqual(warnings, [])
            self.assertEqual(path.read_text(), "new\n")
            scans = [r for r in hub.requests if r[1].startswith("/rest/db/scan")]
            self.assertEqual(len(scans), 1)
            self.assertIn("folder=wz-library", scans[0][1])
            self.assertIn("sub=patterns-deck.stignore", scans[0][1])

    def test_a_failed_scan_is_a_warning_not_an_error(self) -> None:
        self.put("deck", "old\n")
        client = Client("http://127.0.0.1:1", API_KEY, label="hub")
        warnings = whitelist_store.replace(
            self.root, "deck", wl.etag(b"old\n"), lambda s: b"new\n", now=NOW, client=client
        )
        self.assertEqual(len(warnings), 1)
        self.assertIn("could not ask the hub to scan", warnings[0])
        self.assertNotIn(API_KEY, warnings[0])


class MigrationTests(StoreCase):
    def test_a_real_shaped_whitelist_is_migrated_with_a_backup(self) -> None:
        path = self.put("deck", HAND)
        self.assertEqual(self.migrate("deck"), 0)
        new = path.read_text()
        self.assertTrue(wl.is_managed(new))
        self.assertIn("!/roms/psx\n!/roms/snes\n", new)
        self.assertIn("!/roms/wii/Example Game \\[ABCD01\\].rvz", new)
        self.assertNotIn("// deck", new)  # hand comments are lost
        backups = list(whitelist_store.backups_dir(self.root, "deck").iterdir())
        self.assertEqual([b.read_text() for b in backups], [HAND])
        self.assertTrue(self.log[0].startswith("deck: migrated (2 system(s)"), self.log)
        self.assertIn(".stignore", self.log[0].split("saved as")[1])
        # Idempotent: a managed file is left alone and not backed up again.
        self.log.clear()
        self.assertEqual(self.migrate("deck"), 0)
        self.assertEqual(self.log, ["deck: already managed, left alone"])
        self.assertEqual(len(list(whitelist_store.backups_dir(self.root, "deck").iterdir())), 1)

    def test_the_included_set_of_the_library_is_unchanged(self) -> None:
        path = self.put("deck", HAND)
        old = wl.LineMatcher(wl.parse_hand(HAND).lines)
        self.migrate("deck")
        model = wl.parse(path.read_text())
        for rel in LIBRARY_FILES:
            self.assertEqual(old.on(rel), wl.decide(model, rel), rel)

    def test_dry_run_prints_the_file_and_the_dropped_lines_and_writes_nothing(self) -> None:
        text = HAND.replace("!/roms/snes\n", "!/roms/snes\n!/roms/snes/disc\n/roms/n64/Gone.z64\n")
        path = self.put("deck", text.replace("/roms/n64/Gone.z64\n", ""))
        text = path.read_text() + ""
        self.assertEqual(self.migrate("deck", dry_run=True), 0)
        self.assertEqual(path.read_text(), text)
        self.assertFalse((self.root / "catalog").exists())
        shown = "\n".join(self.log)
        self.assertIn("deck: would migrate", shown)
        self.assertIn("dropped line 5: !/roms/snes/disc (shadowed by an earlier line)", shown)
        self.assertIn("| // warpzone-whitelist 1", shown)
        self.assertIn("| !/roms/psx", shown)

    def test_a_redundant_exclusion_is_reported(self) -> None:
        text = HAND.replace("!/bios\n", "/roms/n64/Gone.z64\n!/bios\n")
        self.put("deck", text)
        self.migrate("deck", dry_run=True)
        self.assertTrue(
            any("(an exclusion `*` already makes)" in line for line in self.log), self.log
        )

    def test_a_wildcard_refuses_the_file_with_its_line_and_exit_1(self) -> None:
        text = HAND.replace("!/roms/psx\n", "!/roms/psx\n!/roms/ps4/shortcuts/*.desktop\n")
        path = self.put("deck", text)
        self.assertEqual(self.migrate("deck"), 1)
        self.assertEqual(path.read_text(), text)
        self.assertFalse((self.root / "catalog").exists())
        shown = "\n".join(self.log)
        self.assertIn("deck: refused", shown)
        self.assertIn("line 6: unescaped '*'", shown)

    def test_the_proof_refuses_a_shape_the_model_cannot_hold(self) -> None:
        text = HAND.replace(
            "!/roms/snes\n", "!/roms/snes/disc/Extra.bin\n/roms/snes/disc\n!/roms/snes\n"
        )
        path = self.put("deck", text)
        self.assertEqual(self.migrate("deck"), 1)
        self.assertEqual(path.read_text(), text)
        shown = "\n".join(self.log)
        self.assertIn("would not leave the same paths on", shown)
        self.assertIn("roms/snes/disc/Extra.bin", shown)

    def test_one_refused_file_does_not_stop_the_others(self) -> None:
        self.put("deck", "!/roms/*\n")
        other = self.put("machine", HAND)
        self.assertEqual(self.migrate("deck", "machine"), 1)
        self.assertTrue(wl.is_managed(other.read_text()))

    def test_a_missing_file_is_a_note_and_an_empty_one_is_refused(self) -> None:
        self.assertEqual(self.migrate("deck"), 0)
        self.assertIn("nothing to migrate", self.log[0])
        self.put("deck", "")
        self.log.clear()
        self.assertEqual(self.migrate("deck"), 1)  # an empty file is not a whitelist
        self.assertIn("does not end with *", "\n".join(self.log))

    def test_a_root_without_a_library_is_refused(self) -> None:
        path = self.put("deck", HAND)
        elsewhere = self.root.parent / "elsewhere"
        elsewhere.mkdir()
        whitelist_store.migrate(
            elsewhere, ["deck"], dry_run=False, out=self.log.append, warn=self.warnings.append
        )
        self.assertIn("no patterns-deck.stignore", self.log[0])
        self.log.clear()
        (elsewhere / "library").mkdir()
        (elsewhere / "library" / "patterns-deck.stignore").write_text(HAND)
        self.assertEqual(
            whitelist_store.migrate(elsewhere, ["deck"], dry_run=True, out=self.log.append), 0
        )
        self.assertEqual(path.read_text(), HAND)

    def test_a_symlink_is_not_followed_by_the_proof(self) -> None:
        outside = self.root / "outside"
        outside.mkdir()
        (outside / "secret.txt").write_text("x")
        (self.library / "roms/n64/link").symlink_to(outside)
        self.put("deck", HAND)
        self.assertEqual(self.migrate("deck"), 0)

    def test_the_file_changing_during_the_migration_is_refused(self) -> None:
        path = self.put("deck", HAND)
        original = whitelist_store.read
        calls = []

        def racing(p: Path) -> whitelist_store.Snapshot:
            snap = original(p)
            if not calls:
                calls.append(1)
                p.write_text(HAND + "// edited meanwhile\n")
            return snap

        whitelist_store.read, saved = racing, whitelist_store.read
        try:
            self.assertEqual(self.migrate("deck"), 1)
        finally:
            whitelist_store.read = saved
        self.assertIn("changed while it was being migrated", "\n".join(self.log))
        self.assertTrue(path.read_text().endswith("// edited meanwhile\n"))

    def test_the_hub_is_asked_to_scan_the_migrated_file(self) -> None:
        self.put("deck", HAND)
        with FakeSyncthing(HUB_ID) as hub:
            client = Client(hub.url, API_KEY, label="hub")
            self.assertEqual(self.migrate("deck", client=client), 0)
            scans = [r[1] for r in hub.requests if r[1].startswith("/rest/db/scan")]
            self.assertEqual(len(scans), 1)
            self.assertIn("sub=patterns-deck.stignore", scans[0])
            hub.requests.clear()
            self.migrate("deck", dry_run=True, client=client)
            self.assertEqual(hub.requests, [])

    def test_junk_names(self) -> None:
        for name in (".stfolder", ".stversions", "@eaDir", ".DS_Store", "._x", "Thumbs.db"):
            self.assertTrue(whitelist_store.junk(name), name)
        for name in ("roms", "bios", "Thumbs.db.bak", "a._b"):
            self.assertFalse(whitelist_store.junk(name), name)


class ExcludedSystemsTests(StoreCase):
    def config(self, **deck: object) -> tuple[object, dict[str, object]]:
        data = config_data()
        data["hub"]["root"] = str(self.root)
        data["devices"]["deck"].update(deck)
        profiles = {n: load_profile(n) for n in available_profiles()}
        return parse_config(data, set(profiles)), profiles

    MANAGED = wl.render(
        wl.Model(
            frozenset({"psx", "switch"}),
            frozenset({"roms/ps4/games/CUSA00000", "roms/wii/Example Game [ABCD01].rvz"}),
            frozenset({"roms/psx/Hole.chd"}),
        )
    )

    def test_a_managed_whitelist_naming_an_excluded_system_is_reported(self) -> None:
        cfg, profiles = self.config(exclude_systems=["ps4", "switch", "n64"])
        self.put("deck", self.MANAGED)
        problems, notes = whitelist_store.excluded_system_report(
            cfg, profiles, root=self.root, home=Path("/nonexistent-home")
        )
        self.assertEqual(notes, [])
        self.assertEqual(len(problems), 1)
        self.assertIn("deck: patterns-deck.stignore names ps4, switch", problems[0])
        self.assertIn("roms/ps4/games/CUSA00000 (include)", problems[0])
        self.assertIn("roms/switch (system)", problems[0])

    def test_a_hand_written_whitelist_is_read_with_the_migration_parser(self) -> None:
        cfg, profiles = self.config(exclude_systems=["ps4"])
        self.put("deck", HAND)
        problems, _ = whitelist_store.excluded_system_report(
            cfg, profiles, root=self.root, home=Path("/nonexistent-home")
        )
        self.assertEqual(len(problems), 1)
        self.assertIn("roms/ps4/games/CUSA00000", problems[0])

    def test_nothing_is_reported_without_exclusions_or_a_readable_file(self) -> None:
        cfg, profiles = self.config()
        self.put("deck", self.MANAGED)
        self.assertEqual(
            whitelist_store.excluded_system_report(
                cfg, profiles, root=self.root, home=Path("/nonexistent-home")
            ),
            ([], []),
        )
        cfg, profiles = self.config(exclude_systems=["ps4"])
        self.assertEqual(
            whitelist_store.excluded_system_report(
                cfg, profiles, root=None, home=Path("/nonexistent-home")
            ),
            ([], []),
        )
        self.put("deck", "!/roms/*\n")
        _, notes = whitelist_store.excluded_system_report(
            cfg, profiles, root=self.root, home=Path("/nonexistent-home")
        )
        self.assertEqual(len(notes), 1)
        self.assertIn("whitelist migrate", notes[0])

    def test_the_devices_own_library_folder_is_read_when_there_is_no_root(self) -> None:
        cfg, profiles = self.config(exclude_systems=["ps4"])
        home = self.root.parent / "home"
        library = home / "Emulation"
        library.mkdir(parents=True)
        (library / "patterns-deck.stignore").write_text(self.MANAGED)
        problems, _ = whitelist_store.excluded_system_report(cfg, profiles, root=None, home=home)
        self.assertEqual(len(problems), 1)


class CommandTests(StoreCase):
    def write_config(self, extra: str = "") -> str:
        path = self.root.parent / "config.toml"
        path.write_text(
            f"""
[hub]
name = "nas"
device_id = "{HUB_ID}"
root = "{self.root}"
api_url = "http://127.0.0.1:1"
api_key = "{API_KEY}"

[devices.deck]
device_id = "{DECK_ID}"
profile = "steamos-emudeck"
folders = ["library", "tools"]
{extra}

[devices.machine]
device_id = "{MACHINE_ID}"
profile = "steamos-emudeck"
folders = ["library"]

[devices.saves-only]
device_id = "{"D" * 7 + ("-" + "D" * 7) * 7}"
profile = "steamos-emudeck"
folders = ["saves"]
"""
        )
        return str(path)

    def test_whitelist_migrate_runs_for_every_device_with_a_library(self) -> None:
        config = self.write_config()
        deck = self.put("deck", HAND)
        machine = self.put("machine", "!/roms/*\n")
        code, out, err = run("whitelist", "migrate", "--config", config)
        self.assertEqual(code, 1)
        self.assertTrue(wl.is_managed(deck.read_text()))
        self.assertEqual(machine.read_text(), "!/roms/*\n")
        self.assertIn("deck: migrated", out)
        self.assertIn("machine: refused", out)
        self.assertNotIn("saves-only", out)
        self.assertNotIn(API_KEY, out + err)
        # The hub is not reachable here: the scan is a warning, never a failure.
        self.assertIn("warning: could not ask the hub to scan", err)

    def test_whitelist_migrate_dry_run_a_named_device_and_root(self) -> None:
        config = self.write_config()
        deck = self.put("deck", HAND)
        code, out, err = run(
            "whitelist",
            "migrate",
            "deck",
            "--dry-run",
            "--root",
            str(self.root),
            "--config",
            config,
        )
        self.assertEqual((code, err), (0, ""))
        self.assertIn("deck: would migrate", out)
        self.assertEqual(deck.read_text(), HAND)
        code, _, err = run("whitelist", "migrate", "tablet", "--config", config)
        self.assertEqual(code, 2)
        self.assertIn("unknown device 'tablet'", err)

    def test_check_reports_a_whitelist_that_names_an_excluded_system(self) -> None:
        config = self.write_config('exclude_systems = ["ps4"]')
        self.put("deck", HAND)
        code, out, _ = run("check", "--offline", "--root", str(self.root), "--config", config)
        self.assertEqual(code, 1)
        self.assertIn("deck: patterns-deck.stignore names ps4, in exclude_systems", out)
        with mock.patch.dict(os.environ, {"HOME": str(self.root.parent / "empty-home")}):
            code, out, _ = run("check", "--offline", "--config", config)
        self.assertEqual(code, 0)  # no --root and no library folder on this machine
        config = self.write_config('exclude_systems = ["n64"]')
        code, out, _ = run("check", "--offline", "--root", str(self.root), "--config", config)
        self.assertEqual(code, 0)
        self.assertNotIn("exclude_systems", out)


if __name__ == "__main__":
    unittest.main()
