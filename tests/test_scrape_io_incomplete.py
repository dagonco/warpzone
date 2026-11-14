"""The incomplete memory on disk: every scrape writes it next to the not-found memory, the nightly
run's filter reads it, an attended scrape does not. Temporary hub and stand-in Skyscraper."""

import time
from datetime import timedelta
from unittest import mock

from scrape_support import CHRONO, HOMEBREW, KIRBY, TODAY, HubCase
from test_scrape_io import library
from warpzone import safefile, scrape, scrape_io, scrape_unattended
from warpzone.report import Reporter

NO_VIDEOS = ["covers", "screenshots", "miximages", "marquees", "backcovers"]
KNOWN = ["Kirby Super Star (USA)", "Chrono Trigger (USA)"]


class IncompleteMemoryCase(HubCase):
    """Three games; ScreenScraper knows two of them but has no video for either."""

    def setUp(self) -> None:
        super().setUp()
        library(self.hub, KIRBY, CHRONO, HOMEBREW)
        self.hub.canned({"known": KNOWN, "kinds": NO_VIDEOS})

    def memory_file(self):
        return safefile.read_json_cache(self.hub.cache / scrape_io.NOT_FOUND_FILE)

    def incomplete(self):
        return scrape.parse_incomplete(self.memory_file())

    def not_found(self):
        return scrape.parse_not_found(self.memory_file())

    def held(self, **inputs) -> scrape.Summary:
        opts = self.hub.options(now=lambda: time.time() + 86400)
        return scrape_io.run_held(opts, Reporter(lambda text: None), **inputs)


class WriteTest(IncompleteMemoryCase):
    def test_an_attended_run_records_what_the_games_still_lack(self):
        code, _, _ = self.hub.run()
        self.assertEqual(code, 0)
        self.assertEqual(
            self.incomplete(),
            {
                "snes": {
                    f"./{name}": scrape.Incomplete(len(b"rom"), ("videos",), TODAY)
                    for name in (CHRONO, KIRBY)
                }
            },
        )

    def test_the_not_found_game_has_only_its_not_found_record(self):
        self.hub.run()
        self.assertEqual(set(self.not_found()["snes"]), {f"./{HOMEBREW}"})
        self.assertNotIn(f"./{HOMEBREW}", self.incomplete()["snes"])

    def test_the_inbox_scrape_records_too(self):
        opts = self.hub.options(only=frozenset({f"roms/snes/{KIRBY}"}))
        scrape_io.run_placed(opts, Reporter(lambda text: None))
        self.assertEqual(set(self.incomplete()["snes"]), {f"./{KIRBY}"})

    def test_a_dry_run_writes_nothing(self):
        self.hub.run(dry_run=True)
        self.assertFalse((self.hub.cache / scrape_io.NOT_FOUND_FILE).exists())

    def test_a_game_that_gets_its_missing_kind_loses_the_record(self):
        self.hub.run()
        self.hub.canned({"known": KNOWN})  # now ScreenScraper has the videos
        self.hub.run()
        self.assertEqual(self.incomplete(), {})
        self.assertNotIn("incomplete", self.memory_file())
        self.assertTrue(self.hub.media("videos", "Kirby Super Star (USA)", "mp4").exists())

    def test_an_old_file_without_the_key_keeps_its_records_and_gets_the_new_ones(self):
        old = {"snes": {f"./{HOMEBREW}": scrape.NotFound(3, TODAY - timedelta(days=2))}}
        safefile.write_json_cache(
            self.hub.cache / scrape_io.NOT_FOUND_FILE, scrape.render_not_found(old)
        )
        self.hub.run()
        self.assertEqual(self.not_found(), old)
        self.assertEqual(set(self.incomplete()["snes"]), {f"./{CHRONO}", f"./{KIRBY}"})

    def test_a_dropped_copy_records_nothing(self):
        real = scrape_io.copy_media

        def copy(source, media_root, dest):
            return False if dest.startswith("backcovers/") else real(source, media_root, dest)

        with mock.patch.object(scrape_io, "copy_media", copy):
            self.hub.run()
        self.assertEqual(self.incomplete(), {})

    def test_a_gamelist_that_could_not_be_written_records_nothing(self):
        with mock.patch.object(scrape_io, "write_entries", return_value=(frozenset(), "boom")):
            code, _, _ = self.hub.run()
        self.assertEqual(code, 1)
        self.assertEqual(self.incomplete(), {})


class ReadTest(IncompleteMemoryCase):
    def test_an_attended_run_ignores_the_memory(self):
        self.hub.run()
        self.assertEqual(len(self.hub.gathers()), 1)
        self.hub.run()
        again = self.hub.gathers()
        self.assertEqual(len(again), 2)
        self.assertEqual(set(again[1]["targets"]), {KIRBY, CHRONO})

    def test_the_nightly_filter_skips_the_recorded_games_and_counts_them(self):
        self.hub.run()
        skip = scrape_unattended.incomplete_skipper(self.incomplete())
        found = self.held(skip_incomplete=skip)
        (snes,) = found.systems
        self.assertEqual(snes.skipped_incomplete, 2)
        self.assertEqual(snes.skipped_not_found, 1)
        self.assertEqual(len(self.hub.gathers()), 1)  # nothing new went to Skyscraper

    def test_a_game_with_a_new_missing_kind_goes_to_the_tool_again(self):
        self.hub.run()
        self.hub.media("covers", "Kirby Super Star (USA)").unlink()
        skip = scrape_unattended.incomplete_skipper(self.incomplete())
        found = self.held(skip_incomplete=skip)
        self.assertEqual(found.systems[0].skipped_incomplete, 1)
        self.assertEqual(set(self.hub.gathers()[-1]["targets"]), {KIRBY})
        self.assertEqual(found.systems[0].filled, 1)

    def test_the_memory_is_read_back_from_the_file(self):
        self.hub.run()
        opts = self.hub.options()
        not_found, incomplete = scrape_io.read_memory(opts)
        self.assertEqual(set(not_found["snes"]), {f"./{HOMEBREW}"})
        self.assertEqual(set(incomplete["snes"]), {f"./{CHRONO}", f"./{KIRBY}"})
