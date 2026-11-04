"""Play state on a device: ES-DE's shared gamelists, the generated ones, wiring and doctor."""

import contextlib
import dataclasses
import io
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from support import API_KEY, HUB_ID, FakeProc, FakeSyncthing, config_data
from test_anbernic import ARK_ID
from test_artwork import source_xml
from test_artwork_device import Art
from test_cli import write_config
from test_doctor import by_name, context
from test_provision import reporter
from warpzone import artwork, doctor, esde, playstate, playstate_device, settings_state
from warpzone.cli import main
from warpzone.config import parse_config
from warpzone.model import ESDE_ID, PLAY_STATE_ID, ModelError, device_specs, hub_specs
from warpzone.playstate import Ledger, Record
from warpzone.profiles import ProfileError, load_profile, parse_profile
from warpzone.provision import provision_device
from warpzone.provision_scripts import ensure_play_state_script
from warpzone.provision_timers import ensure_play_state_timer
from warpzone.syncthing import Client

STEAM = load_profile("steamos-emudeck")
ANBERNIC = load_profile("anbernic-rg351mp")
NOW = 1_800_000_000
PAST = 1_700_000_000
GAMELIST = (
    '<?xml version="1.0"?>\n'
    "<alternativeEmulator>\n\t<label>mGBA</label>\n</alternativeEmulator>\n"
    "<gameList>\n"
    "\t<game>\n"
    "\t\t<path>./Alpha (Europe).zip</path>\n"
    "\t\t<name>Alpha &amp; friends</name>\n"
    "\t\t<desc>Scraped, keep me\n\nas I am.</desc>\n"
    "\t\t<rating>0.8</rating>\n"
    "\t\t<favorite>true</favorite>\n"
    "\t\t<playcount>2</playcount>\n"
    "\t\t<lastplayed>20261029T112217</lastplayed>\n"
    "\t</game>\n"
    "\t<game>\n"
    "\t\t<path>./Gone.zip</path>\n"
    "\t\t<name>Not on this device</name>\n"
    "\t\t<playcount>1</playcount>\n"
    "\t</game>\n"
    "</gameList>\n"
)
ALPHA = ("gba", "Alpha (Europe).zip")


class Shared(unittest.TestCase):
    """A Steam device: ES-DE's gamelist, the play state folder and a fake /proc."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        tmp = Path(self._tmp.name)
        self.home = tmp / "home"
        self.state = tmp / "state" / "settings"
        self.proc = FakeProc(tmp / "proc")
        self.folder = self.home / ".local/share/warpzone/play-state"
        self.folder.mkdir(parents=True)
        (self.folder / ".stfolder").mkdir()
        self.gamelist = self.home / "ES-DE/gamelists/gba/gamelist.xml"
        self.gamelist.parent.mkdir(parents=True)
        self.gamelist.write_text(GAMELIST, encoding="utf-8")
        self.rom("Alpha (Europe).zip")
        self.warnings: list[str] = []
        self.lines: list[str] = []

    def rom(self, name: str) -> None:
        path = self.home / "Emulation/roms/gba" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"rom")

    def sync(self, **kw) -> None:
        kw.setdefault("proc", self.proc.path)
        kw.setdefault("now", lambda: NOW)
        playstate_device.sync_shared(
            "deck",
            STEAM,
            self.home,
            say=self.lines.append,
            warn=self.warnings.append,
            state_root=self.state,
            **kw,
        )

    def ledger(self, name: str = "deck") -> Ledger:
        path = self.folder / f"{name}.jsonl"
        return playstate.parse_ledger(path.read_text(encoding="utf-8"), name)

    def peer(self, name: str, replica: str, records) -> None:
        text = playstate.render_ledger(Ledger(name, replica, records))
        (self.folder / f"{name}.jsonl").write_text(text, encoding="utf-8")

    def start(self, comm: str, pid: int = 4242, parent: int = 1) -> None:
        self.proc.add(pid, comm, ppid=parent)


class SharedPublishTests(Shared):
    def test_what_es_de_shows_is_published(self) -> None:
        self.sync()
        record = self.ledger().records[ALPHA]
        self.assertEqual((record.plays, record.fav), (2, True))
        self.assertEqual(self.ledger().replica, ESDE_ID)

    def test_nothing_changes_in_the_gamelist_when_nobody_else_has_news(self) -> None:
        before = self.gamelist.stat().st_mtime_ns
        self.sync()
        self.assertEqual(self.gamelist.read_text(encoding="utf-8"), GAMELIST)
        self.assertEqual(self.gamelist.stat().st_mtime_ns, before)

    def test_a_second_run_writes_nothing_at_all(self) -> None:
        self.peer("ark", "ark", {ALPHA: Record(plays=3)})
        self.sync()
        ledger = self.folder / "deck.jsonl"
        before = (ledger.stat().st_mtime_ns, self.gamelist.stat().st_mtime_ns)
        self.sync()
        self.assertEqual((ledger.stat().st_mtime_ns, self.gamelist.stat().st_mtime_ns), before)
        self.assertIn("<playcount>5</playcount>", self.gamelist.read_text(encoding="utf-8"))

    def test_without_the_folder_nothing_happens(self) -> None:
        (self.folder / ".stfolder").rmdir()
        self.sync()
        self.assertEqual(list(self.folder.glob("*.jsonl")), [])


class SharedApplyTests(Shared):
    def test_a_peers_state_is_merged_and_every_other_byte_stays(self) -> None:
        self.peer("ark", "ark", {ALPHA: Record(plays=3, fav=False, fav_at=NOW)})
        self.sync()
        text = self.gamelist.read_text(encoding="utf-8")
        self.assertIn("<playcount>5</playcount>", text)
        self.assertNotIn("<favorite>", text)
        block = playstate.element({"Alpha (Europe).zip": 3})
        restored = (
            text.replace(block, "")
            .replace("<playcount>5</playcount>", "<playcount>2</playcount>")
            .replace(
                "\t\t<rating>0.8</rating>\n",
                "\t\t<rating>0.8</rating>\n\t\t<favorite>true</favorite>\n",
            )
        )
        self.assertEqual(restored, GAMELIST)

    def test_the_old_file_is_backed_up(self) -> None:
        self.peer("ark", "ark", {ALPHA: Record(plays=3)})
        self.sync()
        backups = list((self.state.parent / "play-state/backups").rglob("*gamelist.xml"))
        self.assertEqual(len(backups), 1)
        self.assertEqual(backups[0].read_text(encoding="utf-8"), GAMELIST)

    def test_entries_of_games_not_here_stay_and_absent_state_is_ignored(self) -> None:
        self.peer("ark", "ark", {("gba", "Nowhere.zip"): Record(plays=9, fav=True, fav_at=NOW)})
        self.sync()
        text = self.gamelist.read_text(encoding="utf-8")
        self.assertIn("./Gone.zip", text)
        self.assertNotIn("Nowhere", text)
        self.assertEqual(self.gamelist.read_text(encoding="utf-8"), GAMELIST)

    def test_a_game_of_the_file_is_updated_even_if_its_rom_is_not_here(self) -> None:
        self.peer("ark", "ark", {("gba", "Gone.zip"): Record(plays=2)})
        self.sync()
        fields = {e.path: e.fields for e in artwork.parse_gamelist(self.gamelist.read_text())}
        self.assertEqual(fields["./Gone.zip"]["playcount"], "3")

    def test_a_rom_here_that_the_file_lacks_gets_an_entry(self) -> None:
        self.rom("Fresh.zip")
        self.peer("ark", "ark", {("gba", "Fresh.zip"): Record(plays=2, fav=True, fav_at=NOW)})
        self.sync()
        fields = {e.path: e.fields for e in artwork.parse_gamelist(self.gamelist.read_text())}
        self.assertEqual(fields["./Fresh.zip"], {"favorite": "true", "playcount": "2"})

    def test_a_system_es_de_has_no_gamelist_for_is_not_created(self) -> None:
        self.peer("ark", "ark", {("snes", "Game.sfc"): Record(plays=2)})
        self.sync()
        self.assertFalse((self.home / "ES-DE/gamelists/snes").exists())

    def test_the_next_device_of_the_replica_reads_the_count_from_the_file(self) -> None:
        self.peer("ark", "ark", {ALPHA: Record(plays=3)})
        self.sync()
        os.remove(self.folder / "ark.jsonl")  # its ledger is late on the next device
        os.remove(self.folder / "deck.jsonl")
        self.sync()
        self.assertEqual(self.ledger().records[ALPHA].plays, 2)

    def test_broken_files(self) -> None:
        self.peer("ark", "ark", {ALPHA: Record(plays=3)})
        (self.folder / "ark.sync-conflict-20261102-101010-ABCDEFG.jsonl").write_text("nonsense")
        (self.folder / "bad.jsonl").write_text("nonsense")
        self.sync()
        self.assertIn("<playcount>5</playcount>", self.gamelist.read_text(encoding="utf-8"))
        self.assertEqual(len(self.warnings), 1)
        self.assertIn("bad.jsonl", self.warnings[0])

    def test_a_broken_own_ledger_stops_the_run(self) -> None:
        self.peer("ark", "ark", {ALPHA: Record(plays=3)})
        (self.folder / "deck.jsonl").write_text("nonsense")
        self.sync()
        self.assertEqual((self.folder / "deck.jsonl").read_text(), "nonsense")
        self.assertEqual(self.gamelist.read_text(encoding="utf-8"), GAMELIST)
        self.assertEqual(len(self.warnings), 1)

    def test_an_unreadable_gamelist_is_left_alone(self) -> None:
        self.gamelist.write_text("<gameList><game>")
        self.peer("ark", "ark", {ALPHA: Record(plays=3)})
        self.sync()
        self.assertEqual(self.gamelist.read_text(), "<gameList><game>")
        self.assertEqual(self.ledger("ark").records[ALPHA].plays, 3)
        self.assertEqual(len(self.warnings), 1)

    def test_dry_run_writes_nothing(self) -> None:
        self.peer("ark", "ark", {ALPHA: Record(plays=3)})
        self.sync(dry_run=True)
        self.assertEqual(self.gamelist.read_text(encoding="utf-8"), GAMELIST)
        self.assertFalse((self.folder / "deck.jsonl").exists())
        self.assertTrue(any("would update" in line for line in self.lines))


class FrontendStateTests(Shared):
    def test_with_es_de_open_it_publishes_and_leaves_the_gamelist_alone(self) -> None:
        self.start("es-de")
        self.peer("ark", "ark", {ALPHA: Record(plays=3)})
        self.sync()
        self.assertEqual(self.gamelist.read_text(encoding="utf-8"), GAMELIST)
        record = self.ledger().records[ALPHA]
        self.assertEqual((record.plays, record.included), (2, 0))

    def test_es_de_started_during_the_backup_leaves_the_gamelist_alone(self) -> None:
        self.peer("ark", "ark", {ALPHA: Record(plays=3)})
        real = settings_state.backup

        def backup_then_start(*args, **kw):
            result = real(*args, **kw)
            self.start("es-de")
            return result

        with mock.patch.object(settings_state, "backup", backup_then_start):
            self.sync()
        self.assertEqual(self.gamelist.read_text(encoding="utf-8"), GAMELIST)
        self.assertEqual(len(self.warnings), 1)
        self.assertIn("ES-DE started", self.warnings[0])

    def test_a_gamelist_edited_during_the_backup_is_left_alone(self) -> None:
        self.peer("ark", "ark", {ALPHA: Record(plays=3)})
        real = settings_state.backup
        edited = GAMELIST + "<!-- by hand -->\n"

        def backup_then_edit(*args, **kw):
            result = real(*args, **kw)
            self.gamelist.write_text(edited, encoding="utf-8")
            return result

        with mock.patch.object(settings_state, "backup", backup_then_edit):
            self.sync()
        self.assertEqual(self.gamelist.read_text(encoding="utf-8"), edited)
        self.assertEqual(len(self.warnings), 1)
        self.assertIn("changed while", self.warnings[0])

    def test_the_merge_waits_and_happens_when_es_de_closes(self) -> None:
        self.start("es-de")
        self.peer("ark", "ark", {ALPHA: Record(plays=3)})
        self.sync()
        shutil.rmtree(self.proc.path / "4242")
        self.sync()
        self.assertIn("<playcount>5</playcount>", self.gamelist.read_text(encoding="utf-8"))

    def test_es_de_above_this_run_is_starting_and_has_read_nothing(self) -> None:
        self.start("ES-DE.AppImage", 4241)
        self.start("es-de", 4242, 4241)
        self.start("sh", os.getppid(), 4242)
        self.peer("ark", "ark", {ALPHA: Record(plays=3)})
        self.sync(starting=True)
        self.assertIn("<playcount>5</playcount>", self.gamelist.read_text(encoding="utf-8"))

    def test_a_second_es_de_that_is_not_above_this_run_counts(self) -> None:
        self.start("es-de", 4242)
        self.start("sh", os.getppid(), 1)
        self.peer("ark", "ark", {ALPHA: Record(plays=3)})
        self.sync(starting=True)
        self.assertEqual(self.gamelist.read_text(encoding="utf-8"), GAMELIST)

    def test_without_the_flag_es_de_above_this_run_counts(self) -> None:
        self.start("es-de", 4242)
        self.start("sh", os.getppid(), 4242)
        self.peer("ark", "ark", {ALPHA: Record(plays=3)})
        self.sync()
        self.assertEqual(self.gamelist.read_text(encoding="utf-8"), GAMELIST)

    def test_without_proc_the_gamelist_is_left_alone(self) -> None:
        self.peer("ark", "ark", {ALPHA: Record(plays=3)})
        self.sync(proc=self.proc.path / "missing")
        self.assertEqual(self.gamelist.read_text(encoding="utf-8"), GAMELIST)

    def test_a_held_lock_skips_the_run(self) -> None:
        self.peer("ark", "ark", {ALPHA: Record(plays=3)})
        root = self.state.parent / "play-state"
        with settings_state.lock(root=root) as got:
            self.assertTrue(got)
            self.sync()
        self.assertEqual(self.gamelist.read_text(encoding="utf-8"), GAMELIST)


class GeneratedTests(Art, unittest.TestCase):
    """The Anbernic shape: gamelists the generator rebuilds, on the shipped `[artwork]`."""

    def setUp(self) -> None:
        super().setUp()
        self.folder = Path(self._tmp.name) / "card/play-state"
        self.folder.mkdir(parents=True)
        (self.folder / ".stfolder").mkdir()
        self.profile = dataclasses.replace(
            self.profile, paths={**self.profile.paths, "play_state": str(self.folder)}
        )

    def sync(self):
        return self.ensure(play_state=True)

    def peer(self, name: str, replica: str, records) -> None:
        text = playstate.render_ledger(Ledger(name, replica, records))
        (self.folder / f"{name}.jsonl").write_text(text, encoding="utf-8")

    def ledger(self) -> Ledger:
        return playstate.parse_ledger((self.folder / "ark.jsonl").read_text("utf-8"), "ark")

    def fields(self, system: str = "gba") -> dict[str, dict[str, str]]:
        return {e.path: e.fields for e in artwork.parse_gamelist(self.out(system).read_bytes())}

    def save(self, system: str, **fields: str) -> None:
        """What the frontend does on exit: change the first game of its file."""
        text = self.out(system).read_text(encoding="utf-8")
        tags = "".join(f"\t\t<{k}>{v}</{k}>\n" for k, v in fields.items())
        for key in fields:
            text = artwork_strip(text, key)
        self.out(system).write_text(text.replace("\t</game>\n", tags + "\t</game>\n", 1))

    def test_it_publishes_under_the_hub_system_not_the_frontends(self) -> None:
        self.sync()
        self.save("pokemonmini", playcount=4, favorite="true")
        self.sync()
        record = self.ledger().records[("pokemini", "Pocket.zip")]
        self.assertEqual((record.plays, record.fav), (4, True))
        self.assertEqual(self.ledger().replica, "ark")

    def test_the_others_are_merged_into_the_generated_gamelist(self) -> None:
        self.peer(
            "deck",
            ESDE_ID,
            {("gba", "Alpha (Europe).zip"): Record(plays=5, fav=True, fav_at=NOW, last=PAST)},
        )
        self.sync()
        fields = self.fields()["./Alpha (Europe).zip"]
        self.assertEqual(fields["playcount"], "5")
        self.assertEqual(fields["favorite"], "true")
        self.assertEqual(fields["lastplayed"], playstate.format_time(PAST, None))
        self.assertEqual(fields["desc"], "A & B")

    def test_what_the_frontend_counts_on_top_is_this_devices_own(self) -> None:
        key = ("gba", "Alpha (Europe).zip")
        self.peer("deck", ESDE_ID, {key: Record(plays=5)})
        self.sync()
        self.save("gba", playcount=7)  # two plays on the console
        self.sync()
        self.sync()
        self.assertEqual(self.ledger().records[key].plays, 2)
        self.assertEqual(self.fields()["./Alpha (Europe).zip"]["playcount"], "7")

    def test_an_unset_elsewhere_removes_the_favourite_and_hidden_stays(self) -> None:
        key = ("gba", "Alpha (Europe).zip")
        self.sync()
        self.save("gba", favorite="true", hidden="true")
        self.sync()
        at = self.ledger().records[key].fav_at
        self.peer("deck", ESDE_ID, {key: Record(fav=False, fav_at=at + 10)})
        self.sync()
        fields = self.fields()["./Alpha (Europe).zip"]
        self.assertNotIn("favorite", fields)
        self.assertEqual(fields["hidden"], "true")

    def test_state_for_a_rom_that_is_not_here_is_ignored_and_kept(self) -> None:
        self.peer("deck", ESDE_ID, {("gba", "Missing.zip"): Record(plays=5)})
        self.sync()
        self.assertNotIn("./Missing.zip", self.fields())

    def test_a_rom_nobody_scraped_gets_an_entry(self) -> None:
        self.rom("gba", "Plain.gba")
        self.peer("deck", ESDE_ID, {("gba", "Plain.gba"): Record(plays=5)})
        self.sync()
        self.assertEqual(self.fields()["./Plain.gba"], {"playcount": "5"})

    def test_counts_in_the_hubs_gamelist_are_not_taken(self) -> None:
        self.scraped("gba", source_xml(("./Alpha (Europe).zip", "Alpha", ("playcount", "9"))))
        self.sync()
        self.assertNotIn("playcount", self.fields()["./Alpha (Europe).zip"])

    def test_with_the_frontend_open_it_publishes_and_writes_no_gamelist(self) -> None:
        self.sync()
        self.save("gba", playcount=3)
        self.peer("deck", ESDE_ID, {("gba", "Alpha (Europe).zip"): Record(plays=5)})
        before = self.out("gba").read_bytes()
        self.start("emulationstatio")
        rep, _ = self.sync()
        self.assertEqual(self.out("gba").read_bytes(), before)
        self.assertEqual(self.ledger().records[("gba", "Alpha (Europe).zip")].plays, 3)
        self.assertTrue(rep.warnings)

    def test_a_second_run_writes_nothing(self) -> None:
        self.peer("deck", ESDE_ID, {("gba", "Alpha (Europe).zip"): Record(plays=5)})
        self.sync()
        paths = [self.out("gba"), self.folder / "ark.jsonl"]
        before = [p.stat().st_mtime_ns for p in paths]
        self.sync()
        self.assertEqual([p.stat().st_mtime_ns for p in paths], before)

    def test_without_the_group_it_behaves_as_before(self) -> None:
        self.peer("deck", ESDE_ID, {("gba", "Alpha (Europe).zip"): Record(plays=5)})
        self.ensure()
        self.assertNotIn("playcount", self.fields()["./Alpha (Europe).zip"])
        self.assertFalse((self.folder / "ark.jsonl").exists())

    def test_a_card_without_the_folder_changes_nothing(self) -> None:
        (self.folder / ".stfolder").rmdir()
        self.peer("deck", ESDE_ID, {("gba", "Alpha (Europe).zip"): Record(plays=5)})
        self.sync()
        self.assertNotIn("playcount", self.fields()["./Alpha (Europe).zip"])


def artwork_strip(text: str, tag: str) -> str:
    lines = [line for line in text.splitlines(keepends=True) if f"<{tag}>" not in line]
    return "".join(lines)


class BothKindsTests(Art, unittest.TestCase):
    """A generated gamelist and a shared one, on one play state folder."""

    def setUp(self) -> None:
        super().setUp()
        self.folder = Path(self._tmp.name) / "folder"
        self.folder.mkdir()
        (self.folder / ".stfolder").mkdir()
        self.profile = dataclasses.replace(
            self.profile, paths={**self.profile.paths, "play_state": str(self.folder)}
        )
        self.steam_home = Path(self._tmp.name) / "steam"
        self.steam = dataclasses.replace(
            STEAM, paths={**STEAM.paths, "play_state": str(self.folder)}
        )
        self.list = self.steam_home / "ES-DE/gamelists/gba/gamelist.xml"
        self.list.parent.mkdir(parents=True)
        self.list.write_text(GAMELIST, encoding="utf-8")
        self.warned: list[str] = []

    def steam_run(self, name: str = "deck") -> None:
        playstate_device.sync_shared(
            name,
            self.steam,
            self.steam_home,
            say=lambda _: None,
            warn=self.warned.append,
            proc=self.proc.path,
            state_root=self.state,
        )

    def test_a_round_trip(self) -> None:
        self.ensure(play_state=True)
        self.steam_run()
        self.steam_run("machine")  # the same gamelist: counted once
        self.ensure(play_state=True)
        fields = {e.path: e.fields for e in artwork.parse_gamelist(self.out("gba").read_bytes())}
        self.assertEqual(fields["./Alpha (Europe).zip"]["playcount"], "2")
        self.assertEqual(fields["./Alpha (Europe).zip"]["favorite"], "true")
        text = self.out("gba").read_text(encoding="utf-8")
        text = text.replace("<playcount>2</playcount>", "<playcount>5</playcount>", 1)
        text = text.replace("\t\t<favorite>true</favorite>\n", "", 1)
        self.out("gba").write_text(text, encoding="utf-8")
        os.utime(self.out("gba"), (2_000_000_000, 2_000_000_000))
        self.ensure(play_state=True)
        self.steam_run()
        self.steam_run("machine")
        shared = self.list.read_text(encoding="utf-8")
        self.assertIn("<playcount>5</playcount>", shared)
        self.assertNotIn("<favorite>", shared)
        self.ensure(play_state=True)
        self.steam_run()
        self.assertIn("<playcount>5</playcount>", self.list.read_text(encoding="utf-8"))
        self.assertEqual(self.warned, [])


PROFILES = {"steamos-emudeck": STEAM, "anbernic-rg351mp": ANBERNIC}


class ModelTests(unittest.TestCase):
    def config(self, deck=("library", "frontend"), ark=("library", "frontend")):
        data = config_data()
        data["devices"] = {
            "deck": {"device_id": ARK_ID, "profile": "steamos-emudeck", "folders": list(deck)},
            "ark": {
                "device_id": "E" * 7 + ("-" + "E" * 7) * 7,
                "profile": "anbernic-rg351mp",
                "folders": list(ark),
            },
        }
        return parse_config(data)

    def test_the_group_adds_one_two_way_folder_with_history_on_the_hub(self) -> None:
        cfg = self.config(
            ("library", "frontend", "play-state"), ("library", "frontend", "play-state")
        )
        spec = {s.id: s for s in hub_specs(cfg, PROFILES)}[PLAY_STATE_ID]
        self.assertEqual(spec.type, "sendreceive")
        self.assertTrue(spec.path.endswith("/frontend/play-state"))
        self.assertEqual(spec.versioning["type"], "staggered")
        self.assertEqual(len(spec.devices), 2)

    def test_the_shared_frontend_folders_do_not_change(self) -> None:
        before = self.config()
        after = self.config(
            ("library", "frontend", "play-state"), ("library", "frontend", "play-state")
        )
        for folder in (ESDE_ID, "wz-media"):
            with self.subTest(folder=folder):
                old = {s.id: s for s in hub_specs(before, PROFILES)}[folder]
                new = {s.id: s for s in hub_specs(after, PROFILES)}[folder]
                self.assertEqual(old, new)
                old = {s.id: s for s in device_specs(before, "deck", STEAM)}[folder]
                new = {s.id: s for s in device_specs(after, "deck", STEAM)}[folder]
                self.assertEqual(old, new)

    def test_a_device_folder_is_two_way_and_on_the_profiles_path(self) -> None:
        cfg = self.config(ark=("library", "frontend", "play-state"))
        spec = {s.id: s for s in device_specs(cfg, "ark", ANBERNIC)}[PLAY_STATE_ID]
        self.assertEqual(spec.path, "/roms/warpzone/play-state")
        self.assertEqual(spec.type, "sendreceive")
        self.assertTrue(spec.ignore_perms)
        self.assertIn("*.wz-tmp", spec.ignores)

    def test_the_group_needs_the_frontend_group(self) -> None:
        cfg = self.config(("library", "play-state"))
        with self.assertRaisesRegex(ModelError, "without 'frontend'"):
            device_specs(cfg, "deck", STEAM)


class ProfileRulesTests(unittest.TestCase):
    def parse(self, paths, **extra):
        return parse_profile({"name": "x", "paths": paths, **extra}, "x.toml")

    def test_it_needs_a_gamelist_to_work_on(self) -> None:
        with self.assertRaisesRegex(ProfileError, "paths.esde or"):
            self.parse({"play_state": "/a"})

    def test_it_cannot_sit_in_the_library(self) -> None:
        with self.assertRaisesRegex(ProfileError, "library"):
            self.parse({"library": "/a", "esde": "/e", "play_state": "/a/b"})

    def test_the_table_needs_the_path_and_a_good_interval(self) -> None:
        with self.assertRaisesRegex(ProfileError, "needs paths.play_state"):
            self.parse({"esde": "/e"}, play_state={"refresh": "15min"})
        with self.assertRaisesRegex(ProfileError, "interval"):
            self.parse({"esde": "/e", "play_state": "/p"}, play_state={"refresh": "soon"})
        with self.assertRaisesRegex(ProfileError, "unknown key"):
            self.parse({"esde": "/e", "play_state": "/p"}, play_state={"nope": 1})

    def test_the_shipped_profiles(self) -> None:
        self.assertEqual(STEAM.play_state.refresh, "15min")
        self.assertIsNone(ANBERNIC.play_state)
        self.assertEqual(ANBERNIC.paths["play_state"], "/roms/warpzone/play-state")


class WiringTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.home = Path(self._tmp.name) / "home"
        self.proc = FakeProc(Path(self._tmp.name) / "proc")
        self.settings = esde.settings_path(self.home / "ES-DE")
        self.settings.parent.mkdir(parents=True)
        self.settings.write_text('<bool name="CustomEventScripts" value="false" />\n')

    def test_the_startup_script_runs_the_sync_as_starting(self) -> None:
        rep, _ = reporter()
        ensure_play_state_script(
            "deck",
            STEAM,
            self.home,
            rep,
            dry_run=False,
            python="/opt/wz/bin/python",
            import_check=lambda _: True,
            proc=self.proc.path,
            state_root=Path(self._tmp.name) / "state",
        )
        script = esde.script_path(self.home / "ES-DE", esde.PLAY_STATE_SCRIPT)
        text = script.read_text()
        self.assertIn("play-state sync 'deck' --frontend-starting --quiet", text)
        self.assertTrue(os.access(script, os.X_OK))
        self.assertTrue(esde.event_scripts_enabled(self.settings.read_text()))
        self.assertEqual(esde.script_interpreter(text), "/opt/wz/bin/python")

    def test_a_profile_without_es_de_has_no_script(self) -> None:
        rep, _ = reporter()
        ensure_play_state_script("ark", ANBERNIC, self.home, rep, dry_run=False)
        self.assertFalse((self.home / "ES-DE/scripts").exists())

    def test_the_timer_runs_the_sync_without_starting(self) -> None:
        rep, _ = reporter()
        calls: list[tuple[str, ...]] = []

        def systemctl(args) -> bool:
            calls.append(tuple(args))
            return True

        kw = {
            "dry_run": False,
            "python": "/opt/wz/bin/python",
            "import_check": lambda _: True,
            "systemctl": systemctl,
        }
        ensure_play_state_timer("deck", STEAM, self.home, rep, **kw)
        units = self.home / ".config/systemd/user"
        service = (units / "warpzone-play-state.service").read_text()
        self.assertIn("play-state sync deck --quiet", service)
        self.assertNotIn("--frontend-starting", service)
        self.assertIn("OnUnitActiveSec=15min", (units / "warpzone-play-state.timer").read_text())
        self.assertIn(("enable", "--now", "warpzone-play-state.timer"), calls)
        calls.clear()
        ensure_play_state_timer("deck", STEAM, self.home, rep, **kw)
        self.assertEqual(calls, [])

    def test_a_generated_frontend_has_no_second_timer(self) -> None:
        rep, _ = reporter()
        ensure_play_state_timer("ark", ANBERNIC, self.home, rep, dry_run=False)
        self.assertFalse((self.home / ".config/systemd").exists())

    def test_provision_device_installs_both_only_for_the_group(self) -> None:
        for folders, wanted in ((("frontend",), False), (("frontend", "play-state"), True)):
            with self.subTest(folders=folders):
                home = Path(self._tmp.name) / f"home{wanted}"
                data = config_data()
                data["devices"]["deck"]["folders"] = list(folders)
                data["devices"]["deck"]["self_update"] = False
                cfg = parse_config(data)
                rep, _ = reporter()
                with FakeSyncthing(cfg.devices["deck"].device_id) as local:
                    provision_device(
                        cfg,
                        "deck",
                        STEAM,
                        Client(local.url, API_KEY),
                        None,
                        rep,
                        home=home,
                        python="/p",
                        import_check=lambda _: True,
                        proc=self.proc.path,
                        state_root=Path(self._tmp.name) / "state",
                        systemctl=lambda _: True,
                        flatpak_run=lambda _: (0, ""),
                    )
                    self.assertEqual(PLAY_STATE_ID in local.folders, wanted)
                script = esde.script_path(home / "ES-DE", esde.PLAY_STATE_SCRIPT)
                self.assertEqual(script.exists(), wanted)
                unit = home / ".config/systemd/user/warpzone-play-state.timer"
                self.assertEqual(unit.exists(), wanted)


class CliTests(unittest.TestCase):
    def config_file(self, tmp: str, *groups: str) -> str:
        path = Path(write_config(tmp, "http://127.0.0.1:1"))
        old = '"frontend"]'
        new = ", ".join(['"frontend"', *(f'"{g}"' for g in groups)]) + "]"
        path.write_text(path.read_text().replace(old, new))
        return str(path)

    def run_cli(self, tmp: str, *argv: str) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        env = {"HOME": tmp, "XDG_STATE_HOME": f"{tmp}/state"}
        with (
            mock.patch.dict(os.environ, env),
            mock.patch.object(Path, "home", return_value=Path(tmp)),
            contextlib.redirect_stderr(err),
            contextlib.redirect_stdout(out),
        ):
            code = main(list(argv))
        return code, out.getvalue(), err.getvalue()

    def test_it_needs_the_group(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = self.config_file(tmp)
            code, _, err = self.run_cli(tmp, "play-state", "sync", "deck", "--config", config)
        self.assertNotEqual(code, 0)
        self.assertIn("play-state", err)

    def test_it_runs_and_quiet_prints_nothing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = self.config_file(tmp, "play-state")
            argv = ("play-state", "sync", "deck", "--config", config, "--quiet")
            code, out, err = self.run_cli(tmp, *argv)
        self.assertEqual((code, out, err), (0, "", ""))

    def test_the_startup_run_loads_no_network_module(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = self.config_file(tmp, "play-state")
            code = (
                "import sys\n"
                f"sys.path.insert(0, {str(Path(__file__).resolve().parent.parent)!r})\n"
                "from warpzone.cli import main\n"
                f"rc = main(['play-state', 'sync', 'deck', '--config', {config!r}, '--quiet'])\n"
                "net = sorted(m for m in ('ssl', 'socket', 'http.client', 'urllib.request')"
                " if m in sys.modules)\n"
                "print(rc, net)\n"
            )  # fmt: skip
            env = {**os.environ, "HOME": tmp, "XDG_STATE_HOME": f"{tmp}/state"}
            result = subprocess.run(
                [sys.executable, "-c", code], capture_output=True, text=True, env=env, check=False
            )
        self.assertEqual(result.stdout.strip(), "0 []", result.stderr)


class DoctorCheckTests(unittest.TestCase):
    CONFIG = f"""
[hub]
name = "nas"
device_id = "{HUB_ID}"
root = "/data/games"
api_url = "http://127.0.0.1:1"
api_key = "{API_KEY}"

[devices.deck]
device_id = "{ARK_ID}"
profile = "steamos-emudeck"
folders = ["frontend", "play-state"]
"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        self.home = self.tmp / "home"
        self.folder = self.home / ".local/share/warpzone/play-state"
        syncthing = self.home / ".config/syncthing"
        syncthing.mkdir(parents=True)
        (syncthing / "config.xml").write_text(
            f'<configuration><folder id="wz-play-state" path="{self.folder}/">'
            f'<device id="{HUB_ID}"/></folder><device id="{ARK_ID}" name="x"/>'
            "<gui><address>127.0.0.1:8384</address><apikey>k</apikey></gui></configuration>"
        )

    def check(self):
        ctx = context(self.tmp, self.CONFIG, system="Linux")
        probes = dataclasses.replace(ctx.probes, home=self.home)
        return doctor.check_play_state(doctor.Context(probes, ctx.config))

    def test_it_names_what_is_missing_until_all_is_there(self) -> None:
        self.assertEqual(by_name(self.check(), "Play state").status, doctor.WARN)
        (self.folder / ".stfolder").mkdir(parents=True)
        self.assertIn("startup script", by_name(self.check(), "Play state").detail)
        script = esde.script_path(self.home / "ES-DE", esde.PLAY_STATE_SCRIPT)
        script.parent.mkdir(parents=True)
        script.write_text("#!/bin/sh\n")
        self.assertIn("timer", by_name(self.check(), "Play state").detail)
        unit = self.home / ".config/systemd/user/warpzone-play-state.timer"
        unit.parent.mkdir(parents=True)
        unit.write_text("[Timer]\n")
        self.assertEqual(by_name(self.check(), "Play state").status, doctor.OK)

    def test_a_device_without_the_group_has_no_line(self) -> None:
        self.CONFIG = self.CONFIG.replace(', "play-state"', "")
        self.assertEqual(self.check(), [])


class DoctorTests(unittest.TestCase):
    def test_verdicts(self) -> None:
        base = {"device": "deck", "folder": "/f", "script": None, "timer": None}
        ok, warn = doctor.OK, doctor.WARN
        judged = doctor.judge_play_state(ready=False, ledger=False, **base)
        self.assertEqual(judged[0], warn)
        self.assertIn("provision device deck", judged[2])
        self.assertEqual(doctor.judge_play_state(ready=True, ledger=True, **base)[0], ok)
        self.assertEqual(doctor.judge_play_state(ready=True, ledger=False, **base)[0], ok)
        missing = {**base, "script": "/s"}
        self.assertEqual(doctor.judge_play_state(ready=True, ledger=True, **missing)[0], warn)
        missing = {**base, "timer": "t.timer"}
        self.assertEqual(doctor.judge_play_state(ready=True, ledger=True, **missing)[0], warn)


if __name__ == "__main__":
    unittest.main()
