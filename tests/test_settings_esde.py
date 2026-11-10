"""ES-DE's `es_settings.xml` in the settings layer: the shipped profile entry, end to end."""

import unittest

from settings_support import DEVICE, SettingsCase
from warpzone import esde, settings
from warpzone.profiles import load_profile

PATH = "ES-DE/settings/es_settings.xml"
XML = (
    '<?xml version="1.0"?>\n'
    '<bool name="CustomEventScripts" value="true" />\n'
    '<bool name="ScrapeVideos" value="true" />\n'
    '<bool name="ScrapeCovers" value="true" />\n'
    '<int name="ScraperConnectionTimeout" value="30" />\n'
    '<string name="ScraperRegion" value="eu" />\n'
    '<string name="ScraperUsernameScreenScraper" value="fake-user" />\n'
    '<string name="ScraperPasswordScreenScraper" value="fake-pass" />\n'  # gitleaks:allow
    '<string name="ROMDirectory" value="/fake/roms" />\n'
)


class EsdeSettingsTests(SettingsCase):
    def setUp(self) -> None:
        super().setUp()
        self.profile = load_profile("steamos-emudeck")
        self.ctx = settings.make_context(DEVICE, self.profile, self.env)
        self.folder = self.ctx.folder
        self.folder.mkdir(parents=True)
        (self.folder / ".stfolder").mkdir()
        self.put(PATH, XML)

    def test_the_profile_entry(self) -> None:
        emu = self.profile.settings["esde"]
        self.assertEqual(emu.process, esde.ESDE_PROCESSES)
        spec = emu.files["es-settings"]
        self.assertEqual((spec.path, spec.format), ("~/ES-DE/settings/es_settings.xml", "esde-xml"))

    def test_a_layered_scraper_toggle_is_written_and_nothing_else(self) -> None:
        self.layer("common", "[esde.es-settings]\nScrapeVideos = false\nScraperRegion = 'us'\n")
        code, out, err = self.run_apply(emulators=["esde"])
        self.assertEqual((code, err), (0, []))
        want = XML.replace('"ScrapeVideos" value="true"', '"ScrapeVideos" value="false"')
        want = want.replace('"ScraperRegion" value="eu"', '"ScraperRegion" value="us"')
        self.assertEqual(self.text(PATH), want)
        self.assertEqual(len(self.backups("esde", "es-settings")), 1)
        self.assertTrue(any("wrote" in line and "ScrapeVideos" in line for line in out))
        # A second run writes nothing.
        code, out, _ = self.run_apply(emulators=["esde"])
        self.assertEqual(self.text(PATH), want)
        self.assertEqual(len(self.backups("esde", "es-settings")), 1)

    def test_what_warpzone_owns_and_paths_are_never_written(self) -> None:
        self.layer(
            "common",
            "[esde.es-settings]\nCustomEventScripts = false\nROMDirectory = '/other'\n"
            "ScrapeCovers = false\n",
        )
        code, out, _ = self.run_apply(emulators=["esde"])
        self.assertEqual(code, 0)
        want = XML.replace('"ScrapeCovers" value="true"', '"ScrapeCovers" value="false"')
        self.assertEqual(self.text(PATH), want)
        for key in ("CustomEventScripts", "ROMDirectory"):
            self.assertTrue(any(line.startswith("cannot write") and key in line for line in out))

    def test_enable_event_scripts_and_the_layer_do_not_fight(self) -> None:
        off = XML.replace('"CustomEventScripts" value="true"', '"CustomEventScripts" value="false"')
        self.put(PATH, off)
        self.layer(
            "common", "[esde.es-settings]\nCustomEventScripts = false\nScrapeVideos = false\n"
        )
        self.run_apply(emulators=["esde"])
        self.assertIn('"CustomEventScripts" value="false"', self.text(PATH))
        self.put(PATH, esde.enable_event_scripts(self.text(PATH)))
        self.run_apply(emulators=["esde"])
        self.assertIn('"CustomEventScripts" value="true"', self.text(PATH))
        self.assertIn('"ScrapeVideos" value="false"', self.text(PATH))

    def test_a_credential_in_a_layer_stops_the_whole_apply(self) -> None:
        for key in ("ScraperUsernameScreenScraper", "ScraperPasswordScreenScraper"):
            with self.subTest(key=key):
                self.layer("common", f"[esde.es-settings]\nScrapeVideos = false\n{key} = 'x'\n")
                code, _, err = self.run_apply(emulators=["esde"])
                self.assertEqual(code, 1)
                self.assertTrue(any("secret keys are never layered" in line for line in err))
                self.assertEqual(self.text(PATH), XML)

    def test_the_credentials_are_never_proposed_or_snapshotted(self) -> None:
        self.layer("common", "[esde.es-settings]\nScrapeVideos = false\n")
        self.run_apply(emulators=["esde"])
        snap = self.snapshot("esde", "es-settings")
        assert snap is not None
        self.assertIn("ScrapeVideos", snap)
        for key in ("ScraperUsernameScreenScraper", "ScraperPasswordScreenScraper", "ROMDirectory"):
            self.assertNotIn(key, snap)
        self.assertNotIn("fake-pass", self.state_text())

    def test_a_running_es_de_is_left_alone(self) -> None:
        self.layer("common", "[esde.es-settings]\nScrapeVideos = false\n")
        for pid, comm, argv in ((4001, "es-de", None), (4002, "bash", "/x/ES-DE.AppImage")):
            with self.subTest(comm=comm):
                self.fake_process(pid, comm, argv)
                code, out, _ = self.run_apply(emulators=["esde"])
                self.assertEqual(code, 0)
                self.assertEqual(self.text(PATH), XML)
                self.assertTrue(any(line.startswith("running") and "esde" in line for line in out))
                self.proc.clear()

    def test_a_value_of_the_wrong_type_cannot_be_written(self) -> None:
        self.layer("common", "[esde.es-settings]\nScrapeVideos = 'false'\nScrapeCovers = false\n")
        code, out, _ = self.run_apply(emulators=["esde"])
        self.assertEqual(code, 0)
        self.assertIn('"ScrapeVideos" value="true"', self.text(PATH))
        self.assertIn('"ScrapeCovers" value="false"', self.text(PATH))
        self.assertTrue(
            any(line.startswith("cannot write") and "ScrapeVideos" in line for line in out)
        )

    def test_a_key_es_de_does_not_have_is_not_added(self) -> None:
        self.layer("common", "[esde.es-settings]\nScrapeTypo = true\n")
        code, out, _ = self.run_apply(emulators=["esde"])
        self.assertEqual(code, 0)
        self.assertEqual(self.text(PATH), XML)
        self.assertTrue(
            any(line.startswith("cannot write") and "ScrapeTypo" in line for line in out)
        )


class WhenIdleTests(SettingsCase):
    """The unattended run of the timer: `apply --when-idle` for ES-DE."""

    def setUp(self) -> None:
        super().setUp()
        self.ctx = settings.make_context(DEVICE, load_profile("steamos-emudeck"), self.env)
        self.folder = self.ctx.folder
        self.folder.mkdir(parents=True)
        (self.folder / ".stfolder").mkdir()
        self.put(PATH, XML)
        self.layer("common", "[esde.es-settings]\nScrapeVideos = false\n")

    def idle(self, **kw: object) -> tuple[int, list[str], list[str]]:
        return self.run_apply(emulators=["esde"], when_idle=True, **kw)

    def test_es_de_running_means_nothing_is_read_written_or_logged(self) -> None:
        for comm, argv in (("es-de", None), ("ES-DE.AppImage", None), ("bash", "/x/es-de")):
            with self.subTest(comm=comm):
                self.proc.clear()
                self.fake_process(4001, comm, argv)
                code, out, err = self.idle()
                self.assertEqual((code, out, err), (0, [], []))
                self.assertEqual(self.text(PATH), XML)
                self.assertFalse(self.state.exists())

    def test_es_de_closed_applies_and_logs_what_changed(self) -> None:
        code, out, err = self.idle()
        self.assertEqual((code, err), (0, []))
        self.assertIn('"ScrapeVideos" value="false"', self.text(PATH))
        self.assertTrue(any("wrote" in line and "ScrapeVideos" in line for line in out))
        log = (self.state / "log").read_text()
        self.assertIn("ScrapeVideos", log)
        info = settings.state.read_last_apply(root=self.state)
        assert info is not None
        self.assertEqual((info["exit"], info["written"]), (0, 1))

    def test_a_run_with_nothing_to_do_leaves_no_trace(self) -> None:
        self.idle()
        log = (self.state / "log").read_text()
        last = (self.state / "last-apply.json").read_text()
        backups = self.backups("esde", "es-settings")
        code, out, _ = self.idle(quiet=True)
        self.assertEqual(code, 0)
        self.assertEqual((self.state / "log").read_text(), log)
        self.assertEqual((self.state / "last-apply.json").read_text(), last)
        self.assertEqual(self.backups("esde", "es-settings"), backups)
        self.assertEqual(out, [])

    def test_a_held_key_is_not_a_trace(self) -> None:
        self.idle()
        log = (self.state / "log").read_text()
        # ES-DE's own value differs from the one last synced (it was changed in ES-DE).
        self.put(PATH, XML)
        code, _, _ = self.idle(quiet=True)
        self.assertEqual(code, 0)
        self.assertIn('"ScrapeVideos" value="true"', self.text(PATH))
        self.assertEqual((self.state / "log").read_text(), log)

    def test_an_invalid_layer_is_logged_but_never_fails_the_timer(self) -> None:
        self.layer("common", "[esde.es-settings]\nScraperPasswordScreenScraper = 'x'\n")
        code, _, _ = self.idle()
        self.assertEqual(code, 0)
        self.assertEqual(self.text(PATH), XML)
        self.assertIn("secret keys are never layered", (self.state / "log").read_text())
        info = settings.state.read_last_apply(root=self.state)
        assert info is not None
        self.assertEqual(info["exit"], 1)

    def test_a_held_lock_is_silent(self) -> None:
        with settings.state.lock(root=self.state) as got:
            self.assertTrue(got)
            code, out, _ = self.idle()
        self.assertEqual((code, out), (0, []))
        self.assertEqual(self.text(PATH), XML)

    def test_other_emulators_still_follow_their_own_rule(self) -> None:
        # Without --when-idle a running ES-DE is reported, as for any emulator.
        self.fake_process(4001, "es-de")
        code, out, _ = self.run_apply(emulators=["esde"])
        self.assertEqual(code, 0)
        self.assertTrue(any(line.startswith("running") for line in out))


if __name__ == "__main__":
    unittest.main()
