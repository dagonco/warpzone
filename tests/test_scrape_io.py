"""`warpzone scrape` against a temporary hub, with a stand-in Skyscraper: the flow, the writer
(re-check before the rename, media never overwritten, one backup per run), `--dry-run`, the
failure rows and the exit codes. No network and no real Skyscraper."""

import os
import time
import unittest
from pathlib import Path
from unittest import mock

from scrape_support import (
    CHRONO,
    FAKE_CONFIG,
    FAKE_CREDS,
    HOMEBREW,
    KIRBY,
    TODAY,
    Hub,
    HubCase,
)
from test_cli import run as run_cli
from warpzone import artwork, inbox_lock, safefile, scrape, scrape_io
from warpzone.errors import UsageError
from warpzone.report import Reporter

CHRONO_ENTRY = (
    "\t<game>\n\t\t<path>./Chrono Trigger (USA).zip</path>\n\t\t<name>Chrono</name>\n"
    "\t\t<playcount>7</playcount>\n\t</game>\n"
)
HUB_GAMELIST = f'<?xml version="1.0"?>\n<gameList>\n{CHRONO_ENTRY}</gameList>\n'


def library(hub: Hub, *names: str) -> None:
    for name in names:
        hub.add_game(name)


class FlowTest(HubCase):
    def setUp(self) -> None:
        super().setUp()
        library(self.hub, KIRBY, CHRONO, HOMEBREW)
        self.hub.write_gamelist(HUB_GAMELIST)
        for kind in ("covers", "screenshots", "miximages", "marquees", "backcovers"):
            self.hub.put_media(kind, "Chrono Trigger (USA)", b"mine")
        self.hub.canned({"known": ["Kirby Super Star (USA)", "Chrono Trigger (USA)"]})

    def test_adds_fills_and_reports_not_found(self):
        code, out, _ = self.hub.run()
        self.assertEqual(code, 0, out)
        text = self.hub.gamelist().read_text()
        # Chrono's entry is untouched (the stand-in's playcount 99 never arrives).
        self.assertIn(CHRONO_ENTRY, text)
        self.assertNotIn("99", text)
        entries = {e.path: e.fields for e in artwork.parse_gamelist(text)}
        self.assertEqual(
            entries["./Kirby Super Star (USA).zip"]["desc"], "About Kirby Super Star (USA) & more"
        )
        self.assertNotIn("./Example Homebrew (World).zip", entries)
        # Every kind for the new game; only the missing kinds for the one with an entry.
        kirby = "Kirby Super Star (USA)"
        for kind in scrape.MEDIA_KINDS:
            ext = "mp4" if kind == "videos" else "png"
            self.assertTrue(self.hub.media(kind, kirby, ext).is_file(), kind)
        self.assertEqual(self.hub.media("3dboxes", kirby).read_bytes(), b"box:" + kirby.encode())
        self.assertEqual(
            self.hub.media("covers", kirby).read_bytes(), b"main:covers:" + kirby.encode()
        )
        chrono = "Chrono Trigger (USA)"
        self.assertEqual(
            self.hub.media("covers", chrono).read_bytes(), b"mine"
        )  # never overwritten
        self.assertTrue(self.hub.media("videos", chrono, "mp4").is_file())
        self.assertTrue(self.hub.media("3dboxes", chrono).is_file())
        # The report: warpzone's own lines.
        self.assertEqual(
            out[0], "scrape: snes: 3 games (Skyscraper 3.21.1, ScreenScraper account: anonymous)"
        )
        self.assertIn(
            "  added      Kirby Super Star (USA).zip: covers, screenshots, miximages, marquees, "
            "3dboxes, backcovers, videos",
            out,
        )
        self.assertIn("  filled     Chrono Trigger (USA).zip: 3dboxes, videos (entry kept)", out)
        self.assertIn("  not found  Example Homebrew (World).zip", out)
        self.assertEqual(out[-1], "scrape: snes: 1 added, 1 filled, 1 not found")
        self.assertNotIn("\x1b", "\n".join(out))

    def test_what_skyscraper_is_given(self):
        self.hub.run()
        calls = self.hub.calls()
        self.assertEqual([c[0] for c in calls], ["--version", "-p", "-p", "-p"])
        gather, main, box = calls[1:]
        self.assertIn("screenscraper", gather)
        self.assertEqual(main[main.index("-f") + 1], "esde")
        self.assertTrue(main[main.index("-a") + 1].endswith("artwork-esde.xml"))
        self.assertTrue(box[box.index("-a") + 1].endswith("artwork-3dbox.xml"))
        for call in calls:
            self.assertFalse({"-u", "-c", "-t"} & set(call), call)
            self.assertNotIn(FAKE_CREDS, " ".join(call))
        # Skyscraper reads and writes outside the hub, and never gets the share's path.
        for call in calls[1:]:
            for flag in ("-i", "-g", "-o"):
                if flag in call:
                    self.assertFalse(Path(call[call.index(flag) + 1]).is_relative_to(self.hub.root))
        # What it saw in the gather: links to the files on the share.
        (seen,) = self.hub.gathers()
        library = self.hub.root / "library" / "roms" / "snes"
        self.assertEqual(seen["targets"][KIRBY], os.path.realpath(library / KIRBY))
        self.assertEqual(len(seen["targets"]), 3)

    def test_nothing_staged_or_temporary_stays_and_the_log_is_private(self):
        self.hub.run()
        scratch = self.hub.work / "scrape"
        self.assertEqual(sorted(p.name for p in scratch.iterdir()), ["snes.log"])
        self.assertEqual((scratch / "snes.log").stat().st_mode & 0o777, 0o600)
        self.assertIn("not found", (scratch / "snes.log").read_text())
        leftovers = [
            p
            for p in self.hub.root.rglob("*")
            if p.name.endswith(".wz-tmp") or p.name.endswith(".part")
        ]
        self.assertEqual(leftovers, [])

    def test_one_backup_per_run_and_the_lock_is_released(self):
        self.hub.run()
        backups = list((self.hub.root / "tools" / "logs" / "gamelist-backups").iterdir())
        self.assertEqual(len(backups), 1)
        self.assertEqual(backups[0].read_text(), HUB_GAMELIST)
        self.assertFalse((self.hub.root / "inbox" / ".warpzone.lock").exists())

    def test_the_lock_it_holds_names_the_scrape_as_its_task(self):
        seen = []
        real = scrape_io._run

        def look(opts, rep):
            lock = self.hub.root / "inbox" / ".warpzone.lock"
            seen.append(inbox_lock.parse(lock.read_text()))
            return real(opts, rep)

        with mock.patch.object(scrape_io, "_run", look):
            self.hub.run()
        self.assertEqual([(i.mode, i.task) for i in seen], [("attended", "scrape")])

    def test_a_second_run_changes_nothing(self):
        self.hub.run()
        first = self.hub.snapshot()
        code, out, _ = self.hub.run()
        second = self.hub.snapshot()
        self.assertEqual(code, 0)
        changed = {k for k in second if first.get(k) != second[k]}
        # Only Skyscraper's log and the stand-in's own call log differ.
        self.assertEqual({k for k in changed if not k.endswith((".log", ".jsonl"))}, set())
        self.assertEqual(first.keys() - second.keys(), set())
        # Homebrew is remembered as not found, so it is not tried again: nothing is left to do.
        self.assertEqual(out[-1], "scrape: snes: skipped 1 not found in the last 30 days")

    def test_the_not_found_game_is_remembered_and_can_be_retried(self):
        self.hub.run()
        memory = scrape.parse_not_found(
            safefile.read_json_cache(self.hub.cache / "scrape-not-found.json")
        )
        self.assertEqual(
            memory["snes"], {"./Example Homebrew (World).zip": scrape.NotFound(3, TODAY)}
        )
        before = len(self.hub.calls())
        _, out, _ = self.hub.run()
        self.assertEqual(len(self.hub.calls()), before + 1)  # only --version
        self.assertIn("skipped 1 not found", out[0] + "".join(out))
        _, out, _ = self.hub.run(retry_not_found=True)
        self.assertIn("  not found  Example Homebrew (World).zip", out)

    def test_the_account_line_never_shows_the_credentials(self):
        self.hub.config_ini.write_text(FAKE_CONFIG)
        _, out, rep = self.hub.run()
        self.assertIn("ScreenScraper account: set", out[0])
        everything = "\n".join(out + rep.warnings)
        for path in (self.hub.work / "scrape" / "snes.log", self.hub.tool_dir / "calls.jsonl"):
            everything += path.read_text()
        self.assertNotIn(FAKE_CREDS, everything)
        self.assertNotIn("fakepassword", everything)

    def test_the_gamelist_written_a_minute_ago_gets_one_warning(self):
        _, _, rep = self.hub.run(now=time.time)
        recent = [w for w in rep.warnings if "ES-DE" in w]
        self.assertEqual(len(recent), 1)
        self.assertIn("warpzone scrape", recent[0])

    def test_a_missing_gamelist_is_created_with_its_folder(self):
        self.hub.gamelist().unlink()
        self.hub.canned({"known": ["Kirby Super Star (USA)"]})
        code, _, _ = self.hub.run()
        self.assertEqual(code, 0)
        paths = [e.path for e in artwork.parse_gamelist(self.hub.gamelist().read_bytes())]
        self.assertIn("./Kirby Super Star (USA).zip", paths)
        self.assertFalse((self.hub.root / "tools").exists())  # no gamelist to back up

    def test_a_symbolic_link_in_the_output_is_a_warning_and_not_copied(self):
        self.hub.canned({"known": ["Kirby Super Star (USA)"], "symlink_video": True})
        _, out, _ = self.hub.run()
        self.assertFalse(self.hub.media("videos", "Kirby Super Star (USA)", "mp4").exists())
        self.assertTrue(any("symbolic link" in line for line in out))

    def test_a_game_that_is_gone_from_the_share_is_skipped_with_a_warning(self):
        (self.hub.root / "library" / "roms" / "snes" / KIRBY).unlink()
        code, out, _ = self.hub.run()
        self.assertEqual(code, 0)
        self.assertTrue(any(KIRBY in line and "not on the share" in line for line in out))


class DryRunTest(HubCase):
    def test_it_lists_the_games_and_writes_nothing(self):
        library(self.hub, KIRBY, CHRONO)
        self.hub.write_gamelist(HUB_GAMELIST)
        before = self.hub.snapshot()
        code, out, _ = self.hub.run(dry_run=True)
        self.assertEqual(code, 0)
        self.assertEqual(self.hub.snapshot(), before)
        self.assertEqual(self.hub.calls(), [])  # no Skyscraper, not even --version
        self.assertEqual(out[0], "scrape: snes: would scrape 2 games")
        self.assertIn("  no entry       Kirby Super Star (USA).zip", out)
        self.assertTrue(
            any(line.startswith("  missing media  Chrono Trigger (USA).zip: ") for line in out)
        )
        self.assertEqual(
            out[-1], "scrape: would scrape 2 games in 1 system (1 with no entry, 1 missing media)"
        )

    def test_it_needs_no_lock(self):
        library(self.hub, KIRBY)
        lock = self.hub.root / "inbox" / ".warpzone.lock"
        inbox_lock._create(
            lock, inbox_lock.LockInfo(inbox_lock.hostname(), os.getpid(), time.time())
        )
        code, _, _ = self.hub.run(dry_run=True)
        self.assertEqual(code, 0)


class FailureTest(HubCase):
    def setUp(self) -> None:
        super().setUp()
        library(self.hub, KIRBY, CHRONO, HOMEBREW)

    def test_the_quota_stops_the_run_and_the_cache_is_still_merged(self):
        self.hub.canned({"known": ["Chrono Trigger (USA)"], "quota_after": 1})
        code, out, _ = self.hub.run()
        self.assertEqual(code, 1)
        self.assertIn(
            "quota: ScreenScraper's daily request limit is reached; 2 games not tried. "
            "Run warpzone scrape again tomorrow.",
            out,
        )
        paths = [e.path for e in artwork.parse_gamelist(self.hub.gamelist().read_bytes())]
        self.assertEqual(paths, ["./Chrono Trigger (USA).zip"])  # the first game, from the cache
        # Games it never got to are not "not found".
        self.assertFalse((self.hub.cache / "scrape-not-found.json").exists())

    def test_the_quota_counts_the_systems_after_it(self):
        self.hub.add_game("Mario.zip", system="nes")
        self.hub.canned({"known": [], "quota_after": 0})
        code, out, _ = self.hub.run(systems=("snes", "nes"))
        self.assertEqual(code, 1)
        self.assertEqual(len(self.hub.gathers()), 1)  # nes never reached Skyscraper
        self.assertTrue(any("4 games not tried" in line for line in out), out)

    def test_screenscraper_down_remembers_nothing(self):
        self.hub.canned({"known": [], "down": True})
        code, out, _ = self.hub.run()
        self.assertEqual(code, 0)  # every game answered "not found": that is not a failure
        self.assertEqual(sum(1 for line in out if "not found  " in line), 3)
        self.assertFalse((self.hub.cache / "scrape-not-found.json").exists())

    def test_skyscraper_that_fails_skips_the_batch(self):
        self.hub.canned({"known": ["Kirby Super Star (USA)"], "gather_exit": 3})
        code, out, _ = self.hub.run()
        self.assertEqual(code, 1)
        log = self.hub.work / "scrape" / "snes.log"
        self.assertIn(f"Skyscraper failed on snes (see {log})", out)
        self.assertFalse(self.hub.gamelist().exists())

    def test_the_time_limit_kills_the_tool(self):
        self.hub.canned({"hang": True})
        started = time.monotonic()
        code, out, _ = self.hub.run(timeout=1.0)
        self.assertLess(time.monotonic() - started, 30)
        self.assertEqual(code, 1)
        self.assertTrue(any(line.startswith("Skyscraper failed on snes") for line in out))

    def test_a_gamelist_that_cannot_be_read_is_left_alone(self):
        self.hub.write_gamelist("not xml at all")
        self.hub.canned({"known": ["Kirby Super Star (USA)"]})
        code, out, _ = self.hub.run()
        self.assertEqual(code, 1)
        self.assertEqual(len(out), 1, out)
        self.assertTrue(out[0].startswith("snes: cannot read gamelist.xml"), out)
        self.assertTrue(out[0].endswith("the entries are left for the next run"), out)
        self.assertEqual(self.hub.gamelist().read_text(), "not xml at all")
        self.assertEqual(self.hub.calls(), [["--version"]])  # no gather, no generating pass

    def test_ctrl_c_removes_what_was_staged_and_releases_the_lock(self):
        self.hub.canned({"known": []})
        real = scrape_io.runners.run_tool

        def interrupt(cmd, timeout, **kw):
            if "--version" in cmd:
                return real(cmd, timeout, **kw)
            raise KeyboardInterrupt

        with (
            mock.patch.object(scrape_io.runners, "run_tool", interrupt),
            self.assertRaises(KeyboardInterrupt),
        ):
            self.hub.run()
        self.assertEqual([p.name for p in (self.hub.work / "scrape").iterdir()], ["snes.log"])
        self.assertFalse((self.hub.root / "inbox" / ".warpzone.lock").exists())


class BatchTest(HubCase):
    """Each batch cleans up after itself and a disk error fails the batch, not the run."""

    def setUp(self) -> None:
        super().setUp()
        library(self.hub, KIRBY, CHRONO, HOMEBREW)
        self.hub.canned({"known": ["Kirby Super Star (USA)", "Chrono Trigger (USA)"]})
        patch = mock.patch.object(scrape, "BATCH_SIZE", 2)
        patch.start()
        self.addCleanup(patch.stop)

    def test_a_batchs_output_is_gone_before_the_next_batch_starts(self):
        code, _, _ = self.hub.run()
        self.assertEqual(code, 0)
        first, second = self.hub.gathers()
        self.assertEqual(first["siblings"], ["batch-1.txt", "input"])
        self.assertEqual(second["siblings"], ["batch-2.txt", "input"])  # no out-1, no batch-1.txt
        self.assertTrue(self.hub.media("covers", "Kirby Super Star (USA)").is_file())

    def test_an_os_error_in_a_batch_is_a_reported_failure_and_the_next_batch_is_tried(self):
        full = OSError(28, "No space left on device")
        with mock.patch.object(scrape_io, "list_output", side_effect=full):
            code, out, _ = self.hub.run()
        self.assertEqual(code, 1)
        log = self.hub.work / "scrape" / "snes.log"
        self.assertEqual(
            [line for line in out if line.startswith("Skyscraper failed")],
            [f"Skyscraper failed on snes (see {log}): No space left on device"] * 2,
        )
        self.assertEqual(len(self.hub.gathers()), 2)
        self.assertEqual([p.name for p in (self.hub.work / "scrape").iterdir()], ["snes.log"])


class WriterTest(HubCase):
    """The hub gamelist is replaced only if it is what was read; media are never overwritten."""

    def setUp(self) -> None:
        super().setUp()
        self.path = self.hub.write_gamelist(HUB_GAMELIST)
        self.entry = artwork.Entry("game", "./New.zip", {"name": "New"})
        rep = Reporter(lambda line: None)
        self.run_state = scrape_io.Run(self.hub.options(), rep, None, False, {})

    def write(self, *entries):
        return scrape_io.write_entries(self.run_state, "snes", entries or [self.entry])

    def paths(self):
        return [e.path for e in artwork.parse_gamelist(self.path.read_bytes())]

    def test_writes_the_entry_and_keeps_the_rest(self):
        written, error = self.write()
        self.assertEqual((written, error), (frozenset({"New.zip"}), None))
        self.assertTrue(
            self.path.read_text().startswith(HUB_GAMELIST[: HUB_GAMELIST.index("</gameList>")])
        )
        self.assertEqual(self.paths(), ["./Chrono Trigger (USA).zip", "./New.zip"])

    def test_a_gamelist_that_changed_between_read_and_rename_is_merged_again(self):
        calls = []
        real = safefile.atomic_write

        def console_writes_first(path, data, *, expect, **kw):
            calls.append(1)
            if len(calls) == 1:  # a console's Syncthing delivers a new gamelist right now
                path.write_text(
                    path.read_text().replace(
                        "</gameList>",
                        "\t<game>\n\t\t<path>./Console.zip</path>\n\t</game>\n</gameList>",
                    )
                )
            return real(path, data, expect=expect, **kw)

        with mock.patch.object(scrape_io.safefile, "atomic_write", console_writes_first):
            written, error = self.write()
        self.assertEqual((len(calls), error), (2, None))
        self.assertEqual(written, frozenset({"New.zip"}))
        self.assertEqual(self.paths(), ["./Chrono Trigger (USA).zip", "./Console.zip", "./New.zip"])

    def test_an_entry_a_console_wrote_meanwhile_is_not_added_twice(self):
        real = safefile.atomic_write
        calls = []

        def console_writes_it(path, data, *, expect, **kw):
            calls.append(1)
            if len(calls) == 1:
                path.write_text(
                    path.read_text().replace(
                        "</gameList>",
                        "\t<game>\n\t\t<path>./New.zip</path>\n\t\t<name>Mine</name>\n\t</game>\n</gameList>",
                    )
                )
            return real(path, data, expect=expect, **kw)

        with mock.patch.object(scrape_io.safefile, "atomic_write", console_writes_it):
            written, error = self.write()
        self.assertEqual((written, error), (frozenset(), None))
        self.assertEqual(self.paths().count("./New.zip"), 1)
        self.assertIn("<name>Mine</name>", self.path.read_text())

    def test_after_three_changes_the_entries_wait_for_the_next_run(self):
        def always_changed(path, data, *, expect, **kw):
            return False

        with mock.patch.object(scrape_io.safefile, "atomic_write", always_changed):
            written, error = self.write()
        self.assertEqual(written, frozenset())
        self.assertEqual(error, "gamelist.xml changed 3 times while it was written")
        self.assertEqual(self.path.read_text(), HUB_GAMELIST)

    def test_the_backup_is_made_once_per_run(self):
        self.write()
        self.write(artwork.Entry("game", "./Other.zip", {}))
        backups = list((self.hub.root / "tools" / "logs" / "gamelist-backups").iterdir())
        self.assertEqual([b.read_text() for b in backups], [HUB_GAMELIST])

    def test_no_entries_touch_nothing(self):
        before = self.hub.snapshot()
        self.assertEqual(scrape_io.write_entries(self.run_state, "snes", []), (frozenset(), None))
        self.assertEqual(self.hub.snapshot(), before)


class MediaCopyTest(HubCase):
    def setUp(self) -> None:
        super().setUp()
        self.src = self.hub.base / "out" / "covers" / "Game.png"
        self.src.parent.mkdir(parents=True)
        self.src.write_bytes(b"from skyscraper")
        self.root = scrape_io.media_path(self.hub.root, "snes")

    def copy(self, dest="covers/Game.png"):
        return scrape_io.copy_media(self.src, self.root, dest)

    def test_copies_and_leaves_no_temporary_file(self):
        self.assertTrue(self.copy())
        self.assertEqual((self.root / "covers" / "Game.png").read_bytes(), b"from skyscraper")
        self.assertEqual([p.name for p in (self.root / "covers").iterdir()], ["Game.png"])

    def test_a_file_that_appears_before_the_rename_is_kept_and_the_copy_dropped(self):
        real = os.link

        def console_got_there(src, dst, **kw):
            Path(dst).write_bytes(b"the console's")
            return real(src, dst, **kw)

        with mock.patch.object(scrape_io.os, "link", console_got_there):
            self.assertFalse(self.copy())
        self.assertEqual((self.root / "covers" / "Game.png").read_bytes(), b"the console's")
        self.assertEqual([p.name for p in (self.root / "covers").iterdir()], ["Game.png"])

    def test_a_file_in_another_extension_counts(self):
        (self.root / "covers").mkdir(parents=True)
        (self.root / "covers" / "Game.jpg").write_bytes(b"jpg")
        self.assertFalse(self.copy())
        self.assertFalse((self.root / "covers" / "Game.png").exists())
        self.assertEqual([p.name for p in (self.root / "covers").iterdir()], ["Game.jpg"])

    def test_without_hard_links_it_renames_after_the_check(self):
        with mock.patch.object(scrape_io.os, "link", side_effect=OSError("no links here")):
            self.assertTrue(self.copy())
        self.assertEqual((self.root / "covers" / "Game.png").read_bytes(), b"from skyscraper")
        self.assertEqual([p.name for p in (self.root / "covers").iterdir()], ["Game.png"])

    def test_a_symbolic_link_is_refused(self):
        link = self.hub.base / "out" / "covers" / "Link.png"
        link.symlink_to(self.src)
        with self.assertRaises(OSError):
            scrape_io.copy_media(link, self.root, "covers/Link.png")
        self.assertFalse((self.root / "covers" / "Link.png").exists())


class StageTest(HubCase):
    def test_links_files_and_m3u_folders(self):
        g1 = self.hub.add_game(KIRBY)
        folder = self.hub.root / "library" / "roms" / "psx" / "Disc Game.m3u"
        folder.mkdir(parents=True)
        (folder / "Disc Game.m3u").write_text("disc.chd\n")
        from library_support import game

        g2 = game("psx", "roms/psx/Disc Game.m3u", "Disc Game", kind="dir")
        gone = game("snes", "roms/snes/Gone.zip", "Gone")
        into = self.hub.base / "input"
        into.mkdir()
        staged, dropped = scrape_io.stage(self.hub.root / "library", into, [g1, g2, gone])
        self.assertEqual(set(staged), {KIRBY, "Disc Game.m3u"})
        self.assertEqual((into / "Disc Game.m3u").resolve(), (folder / "Disc Game.m3u").resolve())
        self.assertTrue((into / KIRBY).is_symlink())
        self.assertEqual([d.what for d in dropped], ["Gone.zip"])

    def test_a_path_that_climbs_is_not_staged(self):
        from library_support import game

        bad = game("snes", "roms/snes/../../../x.zip", "x")
        staged, dropped = scrape_io.stage(self.hub.root / "library", self.hub.base, [bad])
        self.assertEqual((staged, len(dropped)), ({}, 1))


class CommandTest(HubCase):
    def config(self, extra: str = "") -> str:
        path = self.hub.base / "config.toml"
        path.write_text(
            f'[inbox]\nroot = "{self.hub.root}"\nskyscraper = "{self.hub.tool}"\n{extra}'
        )
        return str(path)

    def scrape(self, *args: str, config: str | None = None):
        with mock.patch.dict(
            os.environ, {"XDG_CACHE_HOME": str(self.hub.cache), "HOME": str(self.hub.base / "home")}
        ):
            return run_cli("scrape", "--config", config or self.config(), *args)

    def test_dry_run_through_the_command_line(self):
        library(self.hub, KIRBY)
        code, out, err = self.scrape("--system", "snes", "--dry-run")
        self.assertEqual((code, err), (0, ""))
        self.assertIn("scrape: snes: would scrape 1 game", out)

    def test_a_real_run_through_the_command_line(self):
        library(self.hub, KIRBY)
        self.hub.canned({"known": ["Kirby Super Star (USA)"]})
        with mock.patch.dict(os.environ, {"XDG_CONFIG_HOME": str(self.hub.base / "x")}):
            code, out, err = self.scrape("--system", "snes")
        self.assertEqual((code, err), (0, ""))
        self.assertIn("scrape: snes: 1 added", out)
        self.assertTrue(self.hub.media("covers", "Kirby Super Star (USA)").is_file())

    def test_a_system_that_is_left_out_is_a_usage_error_with_the_reason(self):
        code, _, err = self.scrape("--system", "ps4")
        self.assertEqual(code, 2)
        self.assertIn("launcher", err)

    def test_an_unknown_system_is_a_usage_error(self):
        code, _, err = self.scrape("--system", "amiga")
        self.assertEqual(code, 2)
        self.assertIn("amiga", err)

    def test_a_configured_path_that_does_not_exist_is_a_config_error(self):
        config = self.hub.base / "config.toml"
        config.write_text(
            f'[inbox]\nroot = "{self.hub.root}"\nskyscraper = "/nonexistent/Skyscraper"\n'
        )
        code, _, err = self.scrape(config=str(config))
        self.assertEqual(code, 2)
        self.assertIn("skyscraper", err)

    def test_no_skyscraper_anywhere_exits_2(self):
        config = self.hub.base / "config.toml"
        config.write_text(f'[inbox]\nroot = "{self.hub.root}"\n')
        with mock.patch.dict(os.environ, {"PATH": str(self.hub.base / "empty")}):
            code, _, err = self.scrape(config=str(config))
        self.assertEqual(code, 2)
        self.assertIn("Skyscraper not found", err)

    def test_no_hub_root_exits_2(self):
        config = self.hub.base / "config.toml"
        config.write_text(f'[inbox]\nskyscraper = "{self.hub.tool}"\n')
        code, _, err = self.scrape(config=str(config))
        self.assertEqual(code, 2)
        self.assertIn("no hub root", err)

    def test_a_folder_that_is_not_a_hub_exits_2(self):
        code, _, err = self.scrape("--root", str(self.hub.base / "elsewhere"))
        self.assertEqual(code, 2)
        self.assertIn("hub root", err)

    def test_a_live_lock_exits_2(self):
        library(self.hub, KIRBY)
        lock = self.hub.root / "inbox" / ".warpzone.lock"
        inbox_lock._create(
            lock, inbox_lock.LockInfo(inbox_lock.hostname(), os.getpid(), time.time())
        )
        code, _, err = self.scrape("--system", "snes")
        self.assertEqual(code, 2)
        self.assertIn("another warpzone inbox", err)
        self.assertTrue(lock.exists())

    def test_ctrl_c_exits_130(self):
        library(self.hub, KIRBY)
        real = scrape_io.runners.run_tool

        def interrupt(cmd, timeout, **kw):
            if "--version" in cmd:
                return real(cmd, timeout, **kw)
            raise KeyboardInterrupt

        with mock.patch.object(scrape_io.runners, "run_tool", interrupt):
            code, _, err = self.scrape("--system", "snes")
        self.assertEqual(code, 130)
        self.assertIn("interrupted", err)
        self.assertFalse((self.hub.root / "inbox" / ".warpzone.lock").exists())

    def test_the_default_systems_skip_the_ones_the_library_lacks(self):
        library(self.hub, KIRBY)
        code, out, _ = self.scrape("--dry-run")
        self.assertEqual(code, 0)
        self.assertEqual(out.count("would scrape"), 2)  # snes and the total line

    def test_a_system_in_the_library_without_a_catalog_gets_the_hint(self):
        (self.hub.root / "library" / "roms" / "nes").mkdir()
        code, out, _ = self.scrape("--system", "nes", "--dry-run")
        self.assertEqual(code, 0)
        self.assertIn("scrape: nes: no catalog; run `warpzone catalog` first", out)

    def test_the_credentials_value_never_reaches_the_output(self):
        library(self.hub, KIRBY)
        home = self.hub.base / "home" / ".skyscraper"
        home.mkdir(parents=True)
        (home / "config.ini").write_text(FAKE_CONFIG)
        code, out, err = self.scrape("--system", "snes")
        self.assertEqual(code, 0)
        self.assertIn("ScreenScraper account: set", out)
        self.assertNotIn(FAKE_CREDS, out + err)


class FindAndConfigTest(unittest.TestCase):
    def test_find_skyscraper(self):
        self.assertEqual(
            scrape_io.find_skyscraper(None, lambda name: f"/bin/{name}"), "/bin/Skyscraper"
        )
        self.assertIsNone(scrape_io.find_skyscraper(None, lambda name: None))
        with self.assertRaises(UsageError):
            scrape_io.find_skyscraper("/nonexistent/Skyscraper", lambda name: "/bin/x")

    def test_the_credentials_check_reads_a_file_and_answers_a_boolean(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            ini = Path(tmp) / "config.ini"
            ini.write_text(FAKE_CONFIG)
            self.assertIs(scrape_io.credentials_set(ini), True)
            ini.write_text("[main]\nthreads=1\n")
            self.assertIs(scrape_io.credentials_set(ini), False)
            self.assertIs(scrape_io.credentials_set(Path(tmp) / "none.ini"), False)
            self.assertIs(scrape_io.credentials_set(None), False)

    def test_the_artwork_files_ship_and_say_what_the_design_says(self):
        import xml.etree.ElementTree as ET

        esde = ET.parse(scrape_io.ARTWORK_DIR / scrape.ARTWORK_ESDE).getroot()
        outputs = {(o.get("type"), o.get("resource")) for o in esde.findall("output")}
        self.assertEqual(outputs, {("cover", "cover"), ("marquee", "wheel"), ("screenshot", None)})
        miximage = next(o for o in esde.findall("output") if o.get("type") == "screenshot")
        self.assertEqual((miximage.get("width"), miximage.get("height")), ("1280", "960"))
        self.assertEqual(
            [x.get("resource") for x in miximage.findall("layer")], ["screenshot", "cover", "wheel"]
        )
        box = ET.parse(scrape_io.ARTWORK_DIR / scrape.ARTWORK_3DBOX).getroot()
        (cover,) = box.findall("output")
        self.assertEqual(cover.get("type"), "cover")
        self.assertEqual(cover.find("layer/gamebox").get("side"), "wheel")

    def test_the_package_data_includes_the_artwork(self):
        import tomllib

        data = tomllib.loads(
            (Path(__file__).resolve().parent.parent / "pyproject.toml").read_text()
        )
        self.assertIn(
            "skyscraper/*.xml", data["tool"]["setuptools"]["package-data"]["warpzone.data"]
        )


if __name__ == "__main__":
    unittest.main()


class RunHeldCase(HubCase):
    """Three games, `run_held` and an injected clock."""

    def setUp(self) -> None:
        super().setUp()
        library(self.hub, KIRBY, CHRONO, HOMEBREW)
        self.hub.canned({"known": ["Kirby Super Star (USA)", "Chrono Trigger (USA)"]})
        self.clock = time.time() + 86400

    def summary(self, options=None, **inputs) -> tuple[scrape.Summary, list[str]]:
        lines: list[str] = []
        opts = self.hub.options(now=lambda: self.clock, **(options or {}))
        found = scrape_io.run_held(opts, Reporter(lines.append), **inputs)
        return found, [line for chunk in lines for line in chunk.splitlines()]


class SummaryTest(RunHeldCase):
    """`run_held` returns what the run did: the counts per system, why it stopped, what it did not
    try. The printed report and the exit code are the attended command's, unchanged."""

    def test_the_counts_match_the_printed_report(self):
        found, out = self.summary()
        (snes,) = found.systems
        self.assertEqual(snes.system, "snes")
        self.assertEqual((snes.added, snes.filled, snes.unchanged, snes.not_found), (2, 0, 0, 1))
        self.assertEqual(out[-1], "scrape: snes: 2 added, 1 not found")
        self.assertEqual(
            snes.added_paths, ("Chrono Trigger (USA).zip", "Kirby Super Star (USA).zip")
        )

    def test_a_normal_run_with_each_verdict(self):
        self.hub.write_gamelist(HUB_GAMELIST)
        for kind in ("covers", "screenshots", "miximages", "marquees", "backcovers"):
            self.hub.put_media(kind, "Chrono Trigger (USA)", b"mine")
        found, out = self.summary()
        (snes,) = found.systems
        self.assertEqual((snes.added, snes.filled, snes.not_found), (1, 1, 1))
        self.assertEqual(snes.added_paths, ("Kirby Super Star (USA).zip",))
        self.assertEqual(snes.filled_paths, ("Chrono Trigger (USA).zip",))
        self.assertEqual(out[-1], "scrape: snes: 1 added, 1 filled, 1 not found")
        self.assertIsNone(found.stop)
        self.assertEqual((found.not_tried, found.failed_systems, found.failed), (0, (), False))
        self.assertFalse(found.budget_spent)
        self.assertEqual(found.exit_code, 0)

    def test_the_skipped_not_found_are_counted(self):
        self.summary()
        found, out = self.summary()
        self.assertEqual(found.systems[0].skipped_not_found, 1)
        self.assertIn("skipped 1 not found", out[-1])

    def test_the_printed_report_is_the_attended_one(self):
        _, out = self.summary()
        self.assertEqual(
            [line for line in out if not line.startswith("  added")],
            [
                "scrape: snes: 3 games (Skyscraper 3.21.1, ScreenScraper account: anonymous)",
                "  not found  Example Homebrew (World).zip",
                "scrape: snes: 2 added, 1 not found",
            ],
        )

    def test_the_attended_command_still_returns_the_exit_code(self):
        code, _, _ = self.hub.run()
        self.assertEqual(code, 0)

    def test_the_quota_is_the_stop_reason(self):
        self.hub.canned({"known": ["Chrono Trigger (USA)"], "quota_after": 1})
        found, _ = self.summary()
        self.assertIs(found.stop, scrape.StopReason.QUOTA)
        self.assertEqual(found.not_tried, 2)
        self.assertTrue(found.failed)
        self.assertEqual(found.exit_code, 1)

    def test_a_system_whose_batches_failed_is_named(self):
        with mock.patch.object(scrape_io, "list_output", side_effect=OSError(28, "No space")):
            found, _ = self.summary()
        self.assertEqual(found.failed_systems, ("snes",))
        self.assertEqual(found.exit_code, 1)

    def test_a_dry_run_summary_has_no_systems(self):
        found, _ = self.summary({"dry_run": True})
        self.assertEqual((found.systems, found.stop, found.exit_code), ((), None, 0))


class RunInputsTest(RunHeldCase):
    """The deadline, `still_mine`, `skip_incomplete` and `only` of the nightly run. Three games
    in batches of two, so there is a second batch to stop before."""

    def setUp(self) -> None:
        super().setUp()
        patch = mock.patch.object(scrape, "BATCH_SIZE", 2)
        patch.start()
        self.addCleanup(patch.stop)

    def test_without_the_inputs_both_batches_run(self):
        found, _ = self.summary()
        self.assertEqual(len(self.hub.gathers()), 2)
        self.assertEqual(found.systems[0].added, 2)

    def test_the_deadline_stops_before_the_second_batch(self):
        # One game still to do needs 10 seconds and only 5 are left.
        found, out = self.summary(deadline=self.clock + 5)
        self.assertEqual(len(self.hub.gathers()), 1)
        self.assertIs(found.stop, scrape.StopReason.DEADLINE)
        self.assertTrue(found.budget_spent)
        self.assertEqual(found.not_tried, 1)
        self.assertEqual(found.systems[0].added, 1)
        self.assertEqual(found.exit_code, 0)
        self.assertIn("scrape: time budget spent; 1 game not tried", out)

    def test_a_deadline_that_fits_stops_nothing(self):
        found, _ = self.summary(deadline=self.clock + 10)
        self.assertEqual(len(self.hub.gathers()), 2)
        self.assertIsNone(found.stop)
        self.assertFalse(found.budget_spent)

    def test_the_first_batch_is_allowed_even_past_the_deadline(self):
        found, _ = self.summary(deadline=self.clock - 100)
        self.assertEqual(len(self.hub.gathers()), 1)
        self.assertIs(found.stop, scrape.StopReason.DEADLINE)
        self.assertEqual(found.not_tried, 1)

    def test_the_deadline_leaves_the_next_systems_untried(self):
        self.hub.add_game("Mario.zip", system="nes")
        found, _ = self.summary({"systems": ("snes", "nes")}, deadline=self.clock + 5)
        self.assertEqual(found.not_tried, 2)  # the third snes game and the nes one
        self.assertEqual(len(self.hub.gathers()), 1)
        self.assertEqual([s.system for s in found.systems], ["snes", "nes"])

    def test_a_false_still_mine_stops_before_the_next_batch(self):
        answers = iter([True, False])
        found, out = self.summary(still_mine=lambda: next(answers))
        self.assertEqual(len(self.hub.gathers()), 1)
        self.assertIs(found.stop, scrape.StopReason.LOCK_LOST)
        self.assertEqual(found.not_tried, 1)
        self.assertEqual(found.exit_code, 1)
        self.assertTrue(any("lock" in line for line in out), out)

    def test_still_mine_is_asked_before_each_batch_and_a_true_changes_nothing(self):
        calls: list[int] = []
        found, _ = self.summary(still_mine=lambda: calls.append(1) or True)
        self.assertEqual(len(calls), 2)
        self.assertEqual(found.systems[0].added, 2)

    def test_skip_incomplete_excludes_picks_and_counts_them(self):
        found, out = self.summary(skip_incomplete=lambda pick: pick.game.title.startswith("Kirby"))
        (snes,) = found.systems
        self.assertEqual(snes.skipped_incomplete, 1)
        self.assertEqual(snes.added, 1)
        self.assertEqual(snes.not_found, 1)
        self.assertEqual(out[0].split(": ")[2].split()[0], "2")  # two games went to the tool
        (seen,) = self.hub.gathers()
        self.assertNotIn(KIRBY, seen["targets"])

    def test_only_restricts_the_selection(self):
        found, _ = self.summary({"only": frozenset({f"roms/snes/{CHRONO}"})})
        (snes,) = found.systems
        self.assertEqual(snes.added, 1)
        (seen,) = self.hub.gathers()
        self.assertEqual(list(seen["targets"]), [CHRONO])


class BatchFitsTest(unittest.TestCase):
    def test_the_estimate_is_ten_seconds_a_game(self):
        self.assertEqual(scrape.EST_SECONDS_PER_GAME, 10)

    def test_a_batch_that_ends_at_the_deadline_fits(self):
        self.assertTrue(scrape.batch_fits(100.0, 150.0, 5))

    def test_a_batch_that_ends_after_it_does_not(self):
        self.assertFalse(scrape.batch_fits(100.0, 149.9, 5))

    def test_a_past_deadline_fits_nothing(self):
        self.assertFalse(scrape.batch_fits(100.0, 99.0, 1))
