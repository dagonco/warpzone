"""`warpzone scrape --refresh-text` against a temporary hub, with a stand-in Skyscraper: only the
generating pass runs, the gamelist is edited in place and nothing else on the hub changes. No
network and no real Skyscraper."""

import os
import time
from typing import Any, ClassVar
from unittest import mock

from scrape_support import CHRONO, HOMEBREW, KIRBY, HubCase
from test_cli import run as run_cli
from warpzone import artwork, catalog_store, inbox_lock, scrape, scrape_io

KIRBY_STEM = "Kirby Super Star (USA)"
CHRONO_STEM = "Chrono Trigger (USA)"
GAMELIST = (
    '<?xml version="1.0"?>\n<gameList>\n'
    f"\t<game>\n\t\t<path>./{KIRBY}</path>\n\t\t<name>Kirby Super Star</name>\n"
    "\t\t<desc>An English text</desc>\n\t\t<genre>Platform</genre>\n"
    "\t\t<rating>0.8</rating>\n\t\t<playcount>4</playcount>\n\t\t<favorite>true</favorite>\n"
    "\t\t<altemulator>Core</altemulator>\n\t</game>\n"
    f"\t<game>\n\t\t<path>./{CHRONO}</path>\n\t\t<name>Chrono</name>\n"
    "\t\t<desc>Kept: not in the cache</desc>\n\t</game>\n"
    f"\t<game>\n\t\t<path>./{HOMEBREW}</path>\n\t\t<name>Mine</name>\n"
    "\t\t<desc>Not in the cache</desc>\n\t</game>\n"
    "</gameList>\n"
)
NAME = "Quest de Muestra"
BACKUPS = ("tools", "logs", "gamelist-backups")


class RefreshCase(HubCase):
    def setUp(self) -> None:
        super().setUp()
        for name in (KIRBY, CHRONO, HOMEBREW):
            self.hub.add_game(name)
        self.hub.write_gamelist(GAMELIST)
        # Skyscraper's cache has Kirby with a new name and text; Chrono and the homebrew are not
        # in it.
        self.hub.canned({"known": [KIRBY_STEM], "names": {KIRBY_STEM: NAME}})

    def entries(self, system="snes"):
        return {
            e.path: e.fields for e in artwork.parse_gamelist(self.hub.gamelist(system).read_bytes())
        }

    def backups(self):
        folder = self.hub.root.joinpath(*BACKUPS)
        return sorted(folder.iterdir()) if folder.exists() else []


class RefreshRunTest(RefreshCase):
    def test_the_three_fields_are_replaced_and_nothing_else_changes(self):
        code, out, _ = self.hub.run(refresh_text=True)
        self.assertEqual(code, 0, out)
        text = self.hub.gamelist().read_text()
        self.assertEqual(
            text,
            GAMELIST.replace("Kirby Super Star</name>", f"{NAME}</name>").replace(
                "An English text", "About Kirby Super Star (USA) &amp; more"
            ),
        )
        self.assertEqual(
            out,
            [
                "scrape: snes: refreshed the text of 1 of 3 entries (name 1, desc 1); "
                "2 not in the cache",
                f'  renamed    {KIRBY}: "Kirby Super Star" -> "{NAME}"',
            ],
        )

    def test_a_field_the_cache_lacks_and_an_entry_not_in_the_cache_are_kept(self):
        self.hub.run(refresh_text=True)
        entries = self.entries()
        self.assertEqual(entries[f"./{KIRBY}"]["genre"], "Platform")  # the stand-in has no genre
        self.assertEqual(entries[f"./{CHRONO}"]["name"], "Chrono")
        self.assertEqual(entries[f"./{CHRONO}"]["desc"], "Kept: not in the cache")
        self.assertEqual(entries[f"./{HOMEBREW}"]["desc"], "Not in the cache")

    def test_a_cache_without_text_leaves_the_text_as_it_is(self):
        self.hub.canned({"known": [KIRBY_STEM], "no_text": True, "names": {KIRBY_STEM: NAME}})
        self.hub.run(refresh_text=True)
        entries = self.entries()
        self.assertEqual(entries[f"./{KIRBY}"]["desc"], "An English text")
        self.assertEqual(entries[f"./{KIRBY}"]["name"], NAME)

    def test_a_game_without_an_entry_is_not_added(self):
        extra = self.hub.add_game("Sample Quest (World).zip")
        self.hub.canned({"known": [KIRBY_STEM, "Sample Quest (World)"]})
        self.hub.run(refresh_text=True)
        self.assertNotIn(artwork.relative(extra.gamelist), {k[2:] for k in self.entries()})

    def test_no_media_reaches_the_hub_and_nothing_stays_behind(self):
        self.hub.run(refresh_text=True)
        self.assertFalse((self.hub.root / "frontend" / "media").exists())
        self.assertEqual([p.name for p in (self.hub.work / "scrape").iterdir()], ["snes.log"])
        self.assertFalse((self.hub.root / "inbox" / ".warpzone.lock").exists())
        stray = [p for p in self.hub.root.rglob("*") if p.name.endswith(".wz-tmp")]
        self.assertEqual(stray, [])

    def test_only_the_generating_pass_runs_and_it_makes_no_request(self):
        self.hub.run(refresh_text=True, region="sp", language="es")
        (call,) = self.hub.calls()
        self.assertEqual(self.hub.gathers(), [])
        self.assertNotIn("-s", call)
        self.assertFalse({"--region", "--lang", "-u", "-c", "-t"} & set(call), call)
        self.assertEqual(call[call.index("-f") + 1], "esde")
        self.assertTrue(call[call.index("-a") + 1].endswith("artwork-esde.xml"))
        self.assertFalse(
            any(
                os.path.commonpath([self.hub.root, p]) == str(self.hub.root)
                for p in (call[call.index(f) + 1] for f in ("-g", "-o"))
            )
        )

    def test_one_backup_is_made_and_holds_the_old_file(self):
        self.hub.run(refresh_text=True)
        (backup,) = self.backups()
        self.assertEqual(backup.read_text(), GAMELIST)

    def test_a_second_run_changes_nothing_and_makes_no_backup(self):
        self.hub.run(refresh_text=True)
        first = self.hub.gamelist().read_bytes()
        code, out, _ = self.hub.run(refresh_text=True)
        self.assertEqual(code, 0)
        self.assertEqual(self.hub.gamelist().read_bytes(), first)
        self.assertEqual(len(self.backups()), 1)
        self.assertEqual(
            out, ["scrape: snes: the text of 3 entries is up to date; 2 not in the cache"]
        )

    def test_a_gamelist_that_needs_no_change_is_not_rewritten_or_backed_up(self):
        text = GAMELIST.replace("Kirby Super Star</name>", f"{NAME}</name>").replace(
            "An English text", "About Kirby Super Star (USA) & more".replace("&", "&amp;")
        )
        path = self.hub.write_gamelist(text)
        os.utime(path, (1, 1))
        self.hub.run(refresh_text=True)
        self.assertEqual(path.stat().st_mtime, 1)
        self.assertEqual(self.backups(), [])

    def test_the_memories_are_left_alone(self):
        memory = self.hub.cache / scrape_io.NOT_FOUND_FILE
        self.hub.run(refresh_text=True)
        self.assertFalse(memory.exists())
        memory.parent.mkdir(parents=True, exist_ok=True)
        memory.write_text('{"snes": {"./Old.zip": {"size": 1, "tried": "2026-11-01"}}}')
        before = memory.read_bytes()
        self.hub.run(refresh_text=True)
        self.assertEqual(memory.read_bytes(), before)

    def test_nomultiscrape_is_not_refreshed_or_staged(self):
        marked = GAMELIST.replace(
            "\t\t<playcount>4</playcount>",
            "\t\t<nomultiscrape>true</nomultiscrape>\n\t\t<playcount>4</playcount>",
        )
        self.hub.write_gamelist(marked)
        self.hub.run(refresh_text=True)
        self.assertEqual(self.entries()[f"./{KIRBY}"]["name"], "Kirby Super Star")

    def test_only_the_named_systems_are_refreshed(self):
        self.hub.add_game("Other Quest (USA).zip", system="nes")
        nes = (
            '<?xml version="1.0"?>\n<gameList>\n\t<game>\n\t\t<path>./Other Quest (USA).zip'
            "</path>\n\t\t<name>Other</name>\n\t</game>\n</gameList>\n"
        )
        self.hub.write_gamelist(nes, "nes")
        self.hub.canned({"known": [KIRBY_STEM, "Other Quest (USA)"]})
        self.hub.run(refresh_text=True, systems=("nes",))
        self.assertEqual(self.hub.gamelist().read_text(), GAMELIST)
        self.assertIn("About Other Quest", self.hub.gamelist("nes").read_text())
        self.assertEqual(len(self.hub.calls()), 1)

    def test_several_systems_get_a_totals_line(self):
        self.hub.add_game("Other Quest (USA).zip", system="nes")
        self.hub.write_gamelist(
            '<?xml version="1.0"?>\n<gameList>\n\t<game>\n\t\t<path>./Other Quest (USA).zip'
            "</path>\n\t\t<name>Other</name>\n\t</game>\n</gameList>\n",
            "nes",
        )
        self.hub.canned(
            {
                "known": [KIRBY_STEM, "Other Quest (USA)"],
                "names": {KIRBY_STEM: NAME, "Other Quest (USA)": "Otra Mision"},
            }
        )
        _, out, _ = self.hub.run(refresh_text=True, systems=("snes", "nes"))
        self.assertEqual(
            out[-1], "scrape: refreshed the text of 2 entries in 2 systems (name 2, desc 2)"
        )

    def test_batches_are_generated_one_after_the_other(self):
        with mock.patch.object(scrape, "BATCH_SIZE", 2):
            code, out, _ = self.hub.run(refresh_text=True)
        self.assertEqual((code, len(self.hub.calls())), (0, 2))
        self.assertEqual(
            out[0].split(" (")[0], "scrape: snes: refreshed the text of 1 of 3 entries"
        )

    def test_without_the_option_the_entries_are_not_touched(self):
        self.hub.run()
        entries = self.entries()
        self.assertEqual(entries[f"./{KIRBY}"]["name"], "Kirby Super Star")
        self.assertEqual(entries[f"./{KIRBY}"]["desc"], "An English text")
        self.assertEqual(len(self.hub.gathers()), 1)


class RefreshDryRunTest(RefreshCase):
    def test_it_lists_the_changes_and_writes_nothing_on_the_hub(self):
        before = {k: v for k, v in self.hub.snapshot().items() if k.startswith("hub")}
        code, out, _ = self.hub.run(refresh_text=True, dry_run=True)
        after = {k: v for k, v in self.hub.snapshot().items() if k.startswith("hub")}
        self.assertEqual((code, after), (0, before))
        self.assertEqual(self.backups(), [])
        self.assertEqual(
            out,
            [
                "scrape: snes: would refresh the text of 1 of 3 entries (name 1, desc 1); "
                "2 not in the cache",
                f'  renamed    {KIRBY}: "Kirby Super Star" -> "{NAME}"',
            ],
        )

    def test_every_name_change_is_listed(self):
        self.hub.canned(
            {
                "known": [KIRBY_STEM, CHRONO_STEM],
                "names": {KIRBY_STEM: NAME, CHRONO_STEM: "Chrono Disparador"},
            }
        )
        _, out, _ = self.hub.run(refresh_text=True, dry_run=True)
        self.assertIn(f'  renamed    {KIRBY}: "Kirby Super Star" -> "{NAME}"', out)
        self.assertIn(f'  renamed    {CHRONO}: "Chrono" -> "Chrono Disparador"', out)

    def test_it_needs_no_lock(self):
        lock = self.hub.root / "inbox" / ".warpzone.lock"
        inbox_lock._create(
            lock, inbox_lock.LockInfo(inbox_lock.hostname(), os.getpid(), time.time())
        )
        code, _, _ = self.hub.run(refresh_text=True, dry_run=True)
        self.assertEqual(code, 0)

    def test_the_dry_run_and_the_real_run_agree(self):
        _, dry, _ = self.hub.run(refresh_text=True, dry_run=True)
        _, real, _ = self.hub.run(refresh_text=True)
        self.assertEqual([x.replace("would refresh", "refreshed") for x in dry], real)


class RefreshParityTest(RefreshCase):
    """A dry run reports what a real run does, entries it cannot edit included."""

    CASES: ClassVar[dict[str, Any]] = {
        "plain": "\t\t<name>Old</name>\n",
        "cdata name": "\t\t<name><![CDATA[Old]]></name>\n",
        "attributes": '\t\t<name>Old</name>\n\t\t<desc lang="es">Old text</desc>\n',
        "empty cdata": "\t\t<name>Old</name>\n\t\t<desc><![CDATA[]]></desc>\n",
    }

    def gamelist_with(self, body):
        return (
            f'<?xml version="1.0"?>\n<gameList>\n\t<game>\n\t\t<path>./{KIRBY}</path>\n'
            f"{body}\t</game>\n</gameList>\n"
        )

    def test_the_same_changes_and_the_same_count_in_both_modes(self):
        for label, body in self.CASES.items():
            with self.subTest(label):
                text = self.gamelist_with(body)
                self.hub.write_gamelist(text)
                _, dry, _ = self.hub.run(refresh_text=True, dry_run=True)
                _, real, _ = self.hub.run(refresh_text=True)
                self.assertEqual([x.replace("would refresh", "refreshed") for x in dry], real)
                if label == "plain":
                    self.assertIn(f'  renamed    {KIRBY}: "Old" -> "{NAME}"', real)
                    self.assertNotIn("left as they are", real[0])
                else:
                    self.assertTrue(real[0].endswith("; 1 left as they are (not editable)"), real)
                    self.assertFalse(any("renamed" in line for line in real))
                    self.assertEqual(self.hub.gamelist().read_text(), text)
                    self.assertEqual(self.backups(), [])
                for backup in self.backups():
                    backup.unlink()


class RefreshEntryKindsTest(RefreshCase):
    def test_a_folder_entry_is_never_changed(self):
        text = GAMELIST.replace(f"<game>\n\t\t<path>./{KIRBY}", f"<folder>\n\t\t<path>./{KIRBY}", 1)
        text = text.replace(
            "<altemulator>Core</altemulator>\n\t</game>",
            "<altemulator>Core</altemulator>\n\t</folder>",
            1,
        )
        self.hub.write_gamelist(text)
        self.hub.run(refresh_text=True)
        self.assertEqual(self.hub.gamelist().read_text(), text)

    def test_a_name_that_is_the_file_name_never_replaces_a_title(self):
        self.hub.canned({"known": [KIRBY_STEM], "names": {KIRBY_STEM: KIRBY_STEM}})
        self.hub.run(refresh_text=True)
        entry = self.entries()[f"./{KIRBY}"]
        self.assertEqual(entry["name"], "Kirby Super Star")
        self.assertEqual(entry["desc"], "About Kirby Super Star (USA) & more")


class RefreshScratchTest(RefreshCase):
    def test_a_dry_run_leaves_the_folder_and_log_a_locked_run_uses_alone(self):
        scratch = self.hub.work / "scrape"
        (scratch / "snes" / "input").mkdir(parents=True)
        (scratch / "snes" / "input" / "staged").write_text("another run's link")
        (scratch / "snes.log").write_text("another run's log")
        self.hub.run(refresh_text=True, dry_run=True)
        self.assertEqual((scratch / "snes" / "input" / "staged").read_text(), "another run's link")
        self.assertEqual((scratch / "snes.log").read_text(), "another run's log")
        self.assertEqual(sorted(p.name for p in scratch.iterdir()), ["snes", "snes.log"])

    def test_a_failed_dry_run_keeps_its_own_log_and_names_it(self):
        self.hub.canned({"hang": True})
        code, out, _ = self.hub.run(refresh_text=True, dry_run=True, timeout=1.0)
        self.assertEqual(code, 1)
        (log,) = (self.hub.work / "scrape").iterdir()
        self.assertTrue(log.name.startswith("snes.dry-") and log.suffix == ".log")
        self.assertIn(f"Skyscraper failed on snes (see {log})", out)

    def test_two_dry_runs_use_different_folders(self):
        seen = []
        real = scrape_io.stage

        def look(library, input_dir, games):
            seen.append(input_dir)
            return real(library, input_dir, games)

        with mock.patch.object(scrape_io, "stage", look):
            self.hub.run(refresh_text=True, dry_run=True)
            self.hub.run(refresh_text=True, dry_run=True)
        self.assertEqual(len(set(seen)), 2)
        self.assertEqual(list((self.hub.work / "scrape").iterdir()), [])


class RefreshFailureTest(RefreshCase):
    def test_skyscraper_that_fails_leaves_the_gamelist_and_exits_1(self):
        self.hub.canned({"hang": True})
        code, out, _ = self.hub.run(refresh_text=True, timeout=1.0)
        self.assertEqual(code, 1)
        self.assertTrue(any(line.startswith("Skyscraper failed on snes") for line in out))
        self.assertEqual(self.hub.gamelist().read_text(), GAMELIST)
        self.assertEqual(self.backups(), [])

    def test_a_gamelist_that_cannot_be_read_is_reported(self):
        self.hub.write_gamelist("not xml at all")
        code, out, _ = self.hub.run(refresh_text=True)
        self.assertEqual(code, 1)
        self.assertTrue(any("gamelist" in line for line in out), out)
        self.assertEqual(self.hub.calls(), [])

    def test_a_system_without_a_catalog_is_skipped_with_the_hint(self):
        catalog_store.system_path(self.hub.root, "snes").unlink()
        code, out, _ = self.hub.run(refresh_text=True)
        self.assertEqual(code, 0)
        self.assertEqual(out, ["scrape: snes: no catalog; run `warpzone catalog` first"])

    def test_ctrl_c_removes_what_was_staged_and_releases_the_lock(self):
        interrupt = mock.patch.object(scrape_io.runners, "run_tool", side_effect=KeyboardInterrupt)
        with interrupt, self.assertRaises(KeyboardInterrupt):
            self.hub.run(refresh_text=True)
        self.assertEqual([p.name for p in (self.hub.work / "scrape").iterdir()], ["snes.log"])
        self.assertFalse((self.hub.root / "inbox" / ".warpzone.lock").exists())


class RefreshCommandTest(RefreshCase):
    def config(self) -> str:
        path = self.hub.base / "config.toml"
        path.write_text(f'[inbox]\nroot = "{self.hub.root}"\nskyscraper = "{self.hub.tool}"\n')
        return str(path)

    def scrape(self, *args: str):
        env = {"XDG_CACHE_HOME": str(self.hub.cache), "HOME": str(self.hub.base / "home")}
        with mock.patch.dict(os.environ, env):
            return run_cli("scrape", "--config", self.config(), *args)

    def test_a_real_run_through_the_command_line(self):
        code, out, err = self.scrape("--system", "snes", "--refresh-text")
        # The file was written a moment ago, so the ES-DE warning names this command.
        self.assertEqual(code, 0)
        self.assertIn("run `warpzone scrape --refresh-text` again", err)
        self.assertIn("scrape: snes: refreshed the text of 1 of 3 entries", out)
        self.assertEqual(self.entries()[f"./{KIRBY}"]["name"], NAME)

    def test_a_dry_run_through_the_command_line(self):
        code, out, err = self.scrape("--system", "snes", "--refresh-text", "--dry-run")
        self.assertEqual((code, err), (0, ""))
        self.assertIn("would refresh the text of 1 of 3 entries", out)
        self.assertEqual(self.hub.gamelist().read_text(), GAMELIST)

    def test_unattended_is_a_usage_error(self):
        code, _, err = self.scrape("--unattended", "--refresh-text")
        self.assertEqual(code, 2)
        self.assertIn("--unattended cannot be used with --refresh-text", err)
        self.assertEqual(self.hub.calls(), [])

    def test_retry_not_found_is_a_usage_error(self):
        code, _, err = self.scrape("--refresh-text", "--retry-not-found")
        self.assertEqual(code, 2)
        self.assertIn("--refresh-text cannot be used with --retry-not-found", err)

    def test_a_live_lock_exits_2(self):
        lock = self.hub.root / "inbox" / ".warpzone.lock"
        inbox_lock._create(
            lock, inbox_lock.LockInfo(inbox_lock.hostname(), os.getpid(), time.time())
        )
        code, _, err = self.scrape("--system", "snes", "--refresh-text")
        self.assertEqual(code, 2)
        self.assertIn("another warpzone inbox", err)
        self.assertEqual(self.hub.gamelist().read_text(), GAMELIST)

    def test_the_option_is_off_by_default(self):
        self.assertFalse(scrape_io.Options.__dataclass_fields__["refresh_text"].default)
