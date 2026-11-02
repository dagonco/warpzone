"""Artwork on a device: scope, gamelists, the timer, `frontend refresh` and doctor."""

import contextlib
import dataclasses
import io
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from support import API_KEY, HUB_ID, FakeSyncthing
from test_anbernic import ARK_ID, PROFILE, Device, ark_config
from test_artwork import source_xml
from test_doctor import by_name, context
from test_provision import reporter
from warpzone import artwork, settings_state
from warpzone.cli import main
from warpzone.doctor import OK, WARN, Context, check_frontend
from warpzone.model import device_specs
from warpzone.provision import (
    ensure_artwork_timer,
    ensure_gamelists,
    refresh_artwork,
    sync_artwork_scope,
)
from warpzone.syncthing import Client

SYSTEM_LIST = """<?xml version="1.0"?>
<systemList>
	<system><name>gba</name><path>/roms/gba/</path></system>
	<system><name>pokemonmini</name><path>/roms/pokemonmini/</path></system>
	<system><name>ppsspp</name><path>/roms/psp/</path></system>
	<system><name>nes</name><path>/roms/nes/</path></system>
	<system><name>retroarch</name><path>/opt/cmds/</path></system>
</systemList>
"""
GBA = source_xml(
    ("./Alpha (Europe).zip", "Alpha", ("desc", "A &amp; B"), ("rating", "0.8")),
    ("./Hacks/Beta.gba", "Beta", ("developer", "Dev")),
    ("./Missing.zip", "Missing"),
)
MINI = source_xml(("./Pocket.zip", "Pocket", ("genre", "Casual")))


class Art(Device):
    """Device with a card: library, media and ES-DE data in a temporary folder."""

    def setUp(self) -> None:
        super().setUp()
        card = Path(self._tmp.name) / "card"
        self.lib = card / "library"
        self.media = card / "media"
        self.meta = card / "es-de"
        self.gamelists = self.home / ".emulationstation/gamelists"
        self.system_list.write_text(SYSTEM_LIST)
        art = dataclasses.replace(
            PROFILE.artwork,
            media=str(self.media),
            metadata=str(self.meta),
            gamelists=str(self.gamelists),
        )
        self.profile = dataclasses.replace(
            self.profile, paths={**PROFILE.paths, "library": str(self.lib)}, artwork=art
        )
        # Games on the device and what the hub scraped for them.
        self.rom("gba", "Alpha (Europe).zip")
        self.rom("gba", "Hacks/Beta.gba")
        self.rom("pokemini", "Pocket.zip")
        self.scraped("gba", GBA)
        self.scraped("pokemini", MINI)
        self.cover("gba", "Alpha (Europe)")
        self.cover("gba", "Hacks/Beta")
        self.cover("pokemini", "Pocket")

    def rom(self, system: str, name: str) -> None:
        path = self.lib / "roms" / system / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"rom")

    def scraped(self, system: str, text: str) -> None:
        path = self.meta / "gamelists" / system / "gamelist.xml"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")

    def cover(self, system: str, stem: str, kind: str = "covers") -> Path:
        path = self.media / system / kind / f"{stem}.png"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"png")
        return path

    def out(self, system: str) -> Path:
        return self.gamelists / system / "gamelist.xml"

    def ensure(self, *, dry_run=False, **kw):
        rep, lines = reporter()
        kw.setdefault("proc", self.proc)
        ensure_gamelists("ark", self.profile, self.home, rep, dry_run=dry_run, **kw)
        return rep, lines


class GamelistTests(Art, unittest.TestCase):
    def test_writes_a_gamelist_per_system_under_the_frontends_name(self) -> None:
        rep, _ = self.ensure()
        self.assertEqual(rep.warnings, [])
        self.assertTrue(self.out("gba").is_file())
        self.assertTrue(self.out("pokemonmini").is_file())  # the hub calls it pokemini
        self.assertFalse(self.out("pokemini").exists())
        self.assertFalse(self.out("nes").exists())  # no games, no scrape

    def test_the_content_is_the_converted_gamelist(self) -> None:
        self.ensure()
        entries = artwork.parse_gamelist(self.out("gba").read_bytes())
        self.assertEqual([e.path for e in entries], ["./Alpha (Europe).zip", "./Hacks/Beta.gba"])
        first = entries[0].fields
        self.assertEqual(first["desc"], "A & B")
        self.assertEqual(first["image"], str(self.media / "gba/covers/Alpha (Europe).png"))
        self.assertEqual(entries[1].fields["image"], str(self.media / "gba/covers/Hacks/Beta.png"))

    def test_a_game_whose_rom_is_not_on_the_device_is_left_out(self) -> None:
        self.ensure()
        self.assertNotIn("Missing", self.out("gba").read_text())

    def test_a_system_read_from_a_differently_named_folder_gets_its_own_name(self) -> None:
        self.rom("psp", "Game.iso")
        self.scraped("psp", source_xml(("./Game.iso", "Game")))
        self.ensure()
        self.assertTrue(self.out("ppsspp").is_file())

    def test_a_second_run_writes_nothing(self) -> None:
        self.ensure()
        paths = [self.out("gba"), self.out("pokemonmini")]
        before = [(p.stat().st_mtime_ns, p.read_bytes()) for p in paths]
        rep, lines = self.ensure()
        self.assertEqual([(p.stat().st_mtime_ns, p.read_bytes()) for p in paths], before)
        self.assertEqual(rep.warnings, [])
        self.assertTrue(any("current" in line for line in lines))

    def test_a_new_scrape_is_applied_and_the_players_data_is_kept(self) -> None:
        self.ensure()
        saved = (
            self.out("gba")
            .read_text()
            .replace(
                "</game>", "\t<playcount>4</playcount>\n\t<favorite>true</favorite>\n\t</game>", 1
            )
        )
        self.out("gba").write_text(saved)
        self.scraped("gba", GBA.replace("A &amp; B", "Changed"))
        self.ensure()
        first = artwork.parse_gamelist(self.out("gba").read_bytes())[0].fields
        self.assertEqual(first["desc"], "Changed")
        self.assertEqual((first["playcount"], first["favorite"]), ("4", "true"))

    def test_what_the_frontend_saved_in_its_own_layout_is_not_rewritten(self) -> None:
        self.ensure()
        # The frontend reorders the fields when it saves; nothing the hub knows changed.
        entries = artwork.parse_gamelist(self.out("gba").read_bytes())
        reordered = artwork.FORMATS["emulationstation"].render(entries[::-1])
        self.out("gba").write_text(reordered)
        before = self.out("gba").stat().st_mtime_ns
        self.ensure()
        self.assertEqual(self.out("gba").read_text(), reordered)
        self.assertEqual(self.out("gba").stat().st_mtime_ns, before)

    def test_a_cover_that_arrives_later_is_picked_up(self) -> None:
        cover = self.media / "pokemini/covers/Pocket.png"
        cover.unlink()
        self.ensure()
        fields = artwork.parse_gamelist(self.out("pokemonmini").read_bytes())[0].fields
        self.assertNotIn("image", fields)
        self.cover("pokemini", "Pocket")
        self.ensure()
        fields = artwork.parse_gamelist(self.out("pokemonmini").read_bytes())[0].fields
        self.assertEqual(fields["image"], str(cover))

    def test_left_alone_while_emulationstation_runs(self) -> None:
        self.start("emulationstatio")
        rep, _ = self.ensure()
        self.assertFalse(self.out("gba").exists())
        (warning,) = [w for w in rep.warnings if "EmulationStation" in w]
        self.assertIn("frontend refresh ark", warning)

    def test_nothing_to_change_is_not_a_complaint_while_it_runs(self) -> None:
        self.ensure()
        self.start("emulationstatio")
        rep, _ = self.ensure()
        self.assertEqual(rep.warnings, [])

    def test_without_proc_nothing_is_written_and_it_says_why(self) -> None:
        rep, _ = self.ensure(proc=self.proc / "missing")
        self.assertFalse(self.out("gba").exists())
        self.assertTrue(any("cannot tell whether" in w for w in rep.warnings))

    def test_dry_run_writes_nothing(self) -> None:
        _, lines = self.ensure(dry_run=True)
        self.assertFalse(self.gamelists.exists())
        self.assertTrue(any("dry-run" in line and "gba" in line for line in lines))

    def test_a_broken_scrape_is_reported_and_the_others_still_go_through(self) -> None:
        self.scraped("gba", "<gameList><game>")
        rep, _ = self.ensure()
        self.assertFalse(self.out("gba").exists())
        self.assertTrue(self.out("pokemonmini").is_file())
        self.assertTrue(any("gba" in w and "XML" in w for w in rep.warnings))

    def test_a_gamelist_that_changed_while_it_was_written_is_left_alone(self) -> None:
        self.ensure()
        self.scraped("gba", GBA.replace("A &amp; B", "Changed"))
        with mock.patch.object(settings_state, "atomic_write", return_value=False):
            rep, _ = self.ensure()
        self.assertTrue(any("changed while" in w for w in rep.warnings))

    def test_no_scrape_yet_is_not_an_error(self) -> None:
        for p in (self.meta / "gamelists").rglob("*.xml"):
            p.unlink()
        rep, lines = self.ensure()
        self.assertEqual(rep.warnings, [])
        self.assertTrue(any("no scraped" in line for line in lines))

    def test_two_systems_with_one_name_are_reported(self) -> None:
        self.system_list.write_text(
            SYSTEM_LIST.replace(
                "<system><name>nes</name>",
                "<system><name>gba</name><path>/roms/sufami/</path>"
                "</system><system><name>nes</name>",
            )
        )
        self.rom("sufami", "X.zip")
        rep, _ = self.ensure()
        self.assertTrue(any("sufami" in w for w in rep.warnings))
        self.assertTrue(self.out("gba").is_file())

    def test_a_profile_without_artwork_does_nothing(self) -> None:
        rep, lines = reporter()
        bare = dataclasses.replace(self.profile, artwork=None)
        ensure_gamelists("ark", bare, self.home, rep, dry_run=False, proc=self.proc)
        self.assertEqual((rep.warnings, lines), ([], []))

    def test_a_missing_system_list_warns(self) -> None:
        self.system_list.unlink()
        rep, _ = self.ensure()
        self.assertTrue(any("system list" in w for w in rep.warnings))


class ProvisionArtTests(Art, unittest.TestCase):
    FOLDERS = ("library", "frontend")

    def provision(self, **kw):
        kw.setdefault("folders", self.FOLDERS)
        kw.setdefault("python", "/usr/bin/python3")
        kw.setdefault("import_check", lambda python: True)
        kw.setdefault("systemctl", lambda args: True)
        return super().provision(**kw)

    def test_the_artwork_folders_are_created_receive_only_with_the_scope(self) -> None:
        rep, _ = self.provision()
        folders = self.syncthing.folders
        self.assertEqual(sorted(folders), ["wz-esde", "wz-library", "wz-media"])
        for fid in ("wz-media", "wz-esde"):
            self.assertEqual(folders[fid]["type"], "receiveonly")
            self.assertIs(folders[fid]["ignorePerms"], True)
        self.assertEqual(folders["wz-media"]["path"], str(self.media))
        media = self.syncthing.ignores["wz-media"]
        self.assertEqual(media[-3:], ["!/gba/covers", "!/pokemini/covers", "*"])
        meta = self.syncthing.ignores["wz-esde"]
        self.assertIn("!/gamelists/gba/gamelist.xml", meta)
        self.assertEqual(rep.warnings, [])

    def test_provision_writes_the_gamelists_when_the_data_is_already_there(self) -> None:
        self.provision()
        self.assertTrue(self.out("gba").is_file())
        self.assertTrue(self.out("pokemonmini").is_file())

    def test_a_device_without_the_group_gets_no_artwork(self) -> None:
        self.provision(folders=("library",))
        self.assertEqual(sorted(self.syncthing.folders), ["wz-library"])
        self.assertFalse(self.gamelists.exists())

    def test_an_empty_library_is_said_and_nothing_is_received(self) -> None:
        for p in sorted((self.lib / "roms").rglob("*"), reverse=True):
            p.unlink() if p.is_file() else p.rmdir()
        rep, _ = self.provision()
        self.assertEqual(self.syncthing.ignores["wz-media"][-1], "*")
        self.assertNotIn("!/gba/covers", self.syncthing.ignores["wz-media"])
        self.assertTrue(any("no system" in w for w in rep.warnings))

    def test_a_second_run_changes_nothing(self) -> None:
        self.provision()
        mtime = self.out("gba").stat().st_mtime_ns
        self.provision()
        self.assertEqual(self.out("gba").stat().st_mtime_ns, mtime)

    def test_the_timer_is_installed_once(self) -> None:
        calls: list[tuple[str, ...]] = []

        def systemctl(args):
            calls.append(tuple(args))
            return True

        self.provision(systemctl=systemctl)
        units = self.home / ".config/systemd/user"
        service = (units / "warpzone-artwork.service").read_text()
        timer = (units / "warpzone-artwork.timer").read_text()
        self.assertIn("ExecStart=/usr/bin/python3 -m warpzone frontend refresh ark", service)
        self.assertIn("OnUnitActiveSec=15min", timer)
        self.assertIn(("daemon-reload",), calls)
        self.assertIn(("enable", "--now", "warpzone-artwork.timer"), calls)
        calls.clear()
        self.provision(systemctl=systemctl)
        self.assertEqual(calls, [])

    def test_dry_run_installs_no_timer(self) -> None:
        _, lines = self.provision(dry_run=True)
        self.assertFalse((self.home / ".config/systemd").exists())
        self.assertTrue(any("timer" in line and "dry-run" in line for line in lines))


class TimerTests(Art, unittest.TestCase):
    def install(self, profile=None, **kw):
        rep, lines = reporter()
        calls: list[tuple[str, ...]] = []
        kw.setdefault("python", "/opt/py thon/bin/python3")
        kw.setdefault("import_check", lambda python: True)
        kw.setdefault("systemctl", lambda args: calls.append(tuple(args)) or True)
        ensure_artwork_timer("ark", profile or self.profile, self.home, rep, dry_run=False, **kw)
        return rep, lines, calls

    def test_the_service_quotes_what_systemd_would_split(self) -> None:
        self.install(config_path=Path("/some dir/config.toml"))
        service = (self.home / ".config/systemd/user/warpzone-artwork.service").read_text()
        self.assertIn(
            'ExecStart="/opt/py thon/bin/python3" -m warpzone frontend refresh ark '
            '--config "/some dir/config.toml"',
            service,
        )

    def test_the_default_config_location_is_not_named(self) -> None:
        self.install(config_path=self.home / ".config/warpzone/config.toml")
        service = (self.home / ".config/systemd/user/warpzone-artwork.service").read_text()
        self.assertNotIn("--config", service)

    def test_a_profile_without_an_interval_installs_nothing(self) -> None:
        art = dataclasses.replace(self.profile.artwork, refresh=None)
        _, _, calls = self.install(dataclasses.replace(self.profile, artwork=art))
        self.assertEqual(calls, [])
        self.assertFalse((self.home / ".config/systemd").exists())

    def test_python_that_cannot_import_warpzone_installs_nothing(self) -> None:
        rep, _, calls = self.install(import_check=lambda python: False)
        self.assertEqual(calls, [])
        self.assertTrue(any("cannot import warpzone" in w for w in rep.warnings))

    def test_a_systemctl_that_fails_is_a_warning_naming_the_command(self) -> None:
        rep, _, _ = self.install(systemctl=lambda args: False)
        self.assertTrue(any("systemctl --user daemon-reload" in w for w in rep.warnings))

    def test_the_unit_texts_are_pure(self) -> None:
        service, timer = artwork.render_units("/p", "ark", None, "6h")
        self.assertIn("Type=oneshot", service)
        self.assertIn("OnUnitActiveSec=6h", timer)
        self.assertIn("WantedBy=timers.target", timer)


class ScopeTests(Art, unittest.TestCase):
    def sync(self, systems_after=None):
        cfg = ark_config(("library", "frontend"))
        rep, lines = reporter()
        initial = artwork.library_systems(self.lib / "roms")
        with FakeSyncthing(ARK_ID) as local:
            client = Client(local.url, API_KEY, label="local", sink=lines.append)
            for spec in device_specs(cfg, "ark", self.profile, initial):
                if spec.id in ("wz-media", "wz-esde"):
                    local.folders[spec.id] = {"id": spec.id, **spec.body()}
                    local.ignores[spec.id] = list(spec.ignores)
            if systems_after:
                for system in systems_after:
                    self.rom(system, "x.zip")
            sync_artwork_scope(cfg, "ark", self.profile, self.home, client, rep)
            self.ignores = dict(local.ignores)
        return rep, lines

    def test_a_system_that_arrived_since_widens_the_scope(self) -> None:
        self.sync(["nes"])
        self.assertIn("!/nes/covers", self.ignores["wz-media"])
        self.assertIn("!/gamelists/nes/gamelist.xml", self.ignores["wz-esde"])

    def test_a_scope_that_is_current_changes_nothing(self) -> None:
        _, lines = self.sync()
        self.assertFalse(any("ignores" in line and "updated" in line for line in lines))

    def test_a_folder_syncthing_does_not_have_is_a_warning_to_provision(self) -> None:
        cfg = ark_config(("library", "frontend"))
        rep, _ = reporter()
        with FakeSyncthing(ARK_ID) as local:
            client = Client(local.url, API_KEY, label="local")
            sync_artwork_scope(cfg, "ark", self.profile, self.home, client, rep)
        self.assertTrue(any("provision device ark" in w for w in rep.warnings))

    def test_refresh_does_both_steps(self) -> None:
        cfg = ark_config(("library", "frontend"))
        rep, _ = reporter()
        refresh_artwork(
            cfg, "ark", self.profile, self.home, rep, local=None, dry_run=False, proc=self.proc
        )
        self.assertTrue(self.out("gba").is_file())
        self.assertTrue(any("Syncthing" in w for w in rep.warnings))  # no client: scope unchecked


class DoctorArtTests(Art, unittest.TestCase):
    CONFIG = f"""
[hub]
name = "nas"
device_id = "{HUB_ID}"
root = "/data/games"
api_url = "http://127.0.0.1:1"
api_key = "{API_KEY}"

[devices.ark]
device_id = "{ARK_ID}"
profile = "anbernic-rg351mp"
folders = ["library", "frontend"]
"""

    def setUp(self) -> None:
        super().setUp()
        syncthing = self.home / ".config/syncthing"
        syncthing.mkdir(parents=True)
        (syncthing / "config.xml").write_text(
            f'<configuration><folder id="wz-library" path="{self.lib}/">'
            f'<device id="{HUB_ID}"/></folder><device id="{ARK_ID}" name="x"/>'
            "<gui><address>127.0.0.1:8384</address><apikey>k</apikey></gui></configuration>"
        )

    def check(self, config_text=None):
        ctx = context(
            Path(self._tmp.name),
            self.CONFIG if config_text is None else config_text,
            system="Linux",
        )
        probes = dataclasses.replace(ctx.probes, home=self.home)
        devices = tuple(
            dataclasses.replace(d, profile=self.profile) for d in ctx.config.frontend_devices
        )
        return check_frontend(
            Context(probes, dataclasses.replace(ctx.config, frontend_devices=devices))
        )

    def artwork(self, results=None):
        return by_name(results or self.check(), "Frontend artwork")

    def test_a_device_with_current_gamelists_is_ok(self) -> None:
        self.ensure()
        result = self.artwork()
        self.assertEqual(result.status, OK)
        self.assertIn("2 gamelists", result.detail)

    def test_missing_gamelists_are_named_with_the_fix(self) -> None:
        result = self.artwork()
        self.assertEqual(result.status, WARN)
        self.assertIn("missing", result.detail)
        self.assertIn("pokemonmini", result.detail)
        self.assertIn("warpzone frontend refresh ark", result.fix)

    def test_a_gamelist_older_than_the_scrape_is_stale(self) -> None:
        self.ensure()
        self.scraped("gba", GBA.replace("A &amp; B", "Changed"))
        result = self.artwork()
        self.assertEqual(result.status, WARN)
        self.assertIn("stale", result.detail)
        self.assertIn("gba", result.detail)

    def test_only_the_kinds_of_the_profile_matter(self) -> None:
        self.ensure()
        self.cover("gba", "Alpha (Europe)", "screenshots")  # not in the profile's kinds
        self.assertEqual(self.artwork().status, OK)
        (self.media / "gba/covers/Alpha (Europe).png").unlink()
        self.assertEqual(self.artwork().status, WARN)

    def test_no_scraped_data_yet_is_a_warning_about_the_sync(self) -> None:
        for p in (self.meta / "gamelists").rglob("*.xml"):
            p.unlink()
        result = self.artwork()
        self.assertEqual(result.status, WARN)
        self.assertIn("no scraped gamelist", result.detail)

    def test_the_timer_is_checked_when_the_profile_wants_one(self) -> None:
        self.ensure()
        result = by_name(self.check(), "Artwork timer")
        self.assertEqual(result.status, WARN)
        self.assertIn("provision device ark", result.fix)
        units = self.home / ".config/systemd/user"
        units.mkdir(parents=True)
        (units / "warpzone-artwork.timer").write_text("[Timer]\n")
        self.assertEqual(by_name(self.check(), "Artwork timer").status, OK)

    def test_a_device_without_the_frontend_group_gets_no_artwork_line(self) -> None:
        config = self.CONFIG.replace('"library", "frontend"', '"library"')
        self.assertNotIn("Frontend artwork", [r.name for r in self.check(config)])


class CliTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        (self.tmp / "home").mkdir()
        self.config = self.tmp / "config.toml"
        self.write(DoctorArtTests.CONFIG)

    def write(self, text: str) -> None:
        self.config.write_text(text)

    def run_cli(self, *argv: str) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        env = {"HOME": str(self.tmp / "home"), "XDG_CONFIG_HOME": "", "XDG_STATE_HOME": ""}
        with (
            mock.patch.dict(os.environ, env),
            contextlib.redirect_stdout(out),
            contextlib.redirect_stderr(err),
        ):
            try:
                code = main([*argv, "--config", str(self.config)])
            except SystemExit as exc:
                code = int(exc.code or 0)
        return code, out.getvalue(), err.getvalue()

    def test_refresh_on_a_device_with_no_games_yet_succeeds(self) -> None:
        code, out, err = self.run_cli("frontend", "refresh", "ark")
        self.assertEqual(code, 0, err)
        self.assertIn("no system", out + err)

    def test_an_unknown_device_is_a_config_error(self) -> None:
        code, _, err = self.run_cli("frontend", "refresh", "nope")
        self.assertEqual(code, 2)
        self.assertIn("unknown device", err)

    def test_a_device_without_the_frontend_group_is_refused_by_name(self) -> None:
        self.write(DoctorArtTests.CONFIG.replace('"library", "frontend"', '"library"'))
        code, _, err = self.run_cli("frontend", "refresh", "ark")
        self.assertEqual(code, 2)
        self.assertIn("frontend", err)

    def test_a_profile_without_artwork_is_refused_by_name(self) -> None:
        self.write(DoctorArtTests.CONFIG.replace("anbernic-rg351mp", "steamos-emudeck"))
        code, _, err = self.run_cli("frontend", "refresh", "ark")
        self.assertEqual(code, 2)
        self.assertIn("artwork", err)

    def test_render_ignores_shows_the_artwork_folders(self) -> None:
        code, out, err = self.run_cli("render-ignores", "ark")
        self.assertEqual(code, 0, err)
        self.assertIn("# wz-media", out)
        self.assertIn("# wz-esde", out)

    def test_it_is_in_the_help(self) -> None:
        code, out, _ = self.run_cli("frontend", "--help")
        self.assertEqual(code, 0)
        self.assertIn("refresh", out)

    def test_the_shipped_profile_is_valid_for_the_command(self) -> None:
        self.assertEqual(PROFILE.artwork.refresh, "15min")


if __name__ == "__main__":
    unittest.main()
