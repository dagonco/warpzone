"""The run record and the webhook payload of `inbox` (pure), the record files, and an attended
`--apply` end to end with a stand-in webhook."""

import json
import os
import tempfile
import unittest
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from unittest import mock

from notify_support import Receiver
from test_cli import run
from test_inbox import NES_ROM, SPIRIT_ROM, Base
from warpzone import inbox, inbox_unattended_io
from warpzone.inbox_core import ItemResult, Options, ScrapeNote, Tools, UnitResult
from warpzone.inbox_unattended import (
    NOTIFY_LIST_MAX,
    RECORDS_KEPT,
    build_record,
    clean_text,
    item_outcome,
    notify_payload,
    record_id,
    render_record,
    select_records_purge,
    should_notify,
)
from warpzone.report import Reporter
from warpzone.syncthing import REDACTED

START = datetime(2026, 11, 13, 10, 15, 0, tzinfo=UTC)
END = datetime(2026, 11, 13, 10, 21, 40, tzinfo=UTC)
ROOT = "/srv/hub"
ZRIF = "eNpj" + "YBgFo2AUjIJRMApGwSgYBaNg" * 8  # gitleaks:allow (a fake zRIF)


def placed(system: str = "psx", entry: str = "Game (Europe).chd") -> UnitResult:
    return UnitResult(system, entry, "placed", ra=["RA: hash not linked"])


def done_item(name: str = "Game.zip", *units: UnitResult) -> ItemResult:
    return ItemResult(
        name, list(units or [placed()]), moved_to="_done/2026-11-13/" + name, notes=["a note"]
    )


def review_item(name: str = "dump.7z") -> ItemResult:
    return ItemResult(name, reasons=["no DAT match: track01.bin"], moved_to=f"_revisar/{name}")


def held_item(name: str = "Held.zip") -> ItemResult:
    result = done_item(name)
    result.unmoved = "Permission denied"
    result.moved_to = ""
    result.reasons.append("could not move the original: Permission denied")
    return result


def record(results: list[ItemResult], **kw: Any) -> dict[str, Any]:
    args: dict[str, Any] = {
        "run_id": record_id(START),
        "mode": "attended",
        "started": START,
        "finished": END,
        "status": 0,
    }
    args.update(kw)
    return build_record(results, **args)


class IdAndPurgeTests(unittest.TestCase):
    def test_the_id_is_the_start_in_utc_with_a_suffix_for_the_same_second(self) -> None:
        self.assertEqual(record_id(START), "20261113T101500Z")
        self.assertEqual(record_id(START, 1), "20261113T101500Z")
        self.assertEqual(record_id(START, 3), "20261113T101500Z-3")

    def test_the_newest_fifty_are_kept_oldest_deleted_first(self) -> None:
        names = [f"2026110{d}T100000Z.json" for d in range(1, 10)]
        names += [f"202611{d:02d}T100000Z.json" for d in range(10, 60)]
        self.assertEqual(len(names), 59)
        gone = select_records_purge(reversed(names))
        self.assertEqual(gone, sorted(names)[: 59 - RECORDS_KEPT])
        self.assertEqual(select_records_purge(names[:RECORDS_KEPT]), [])

    def test_a_suffixed_id_is_newer_than_the_plain_one_of_its_second(self) -> None:
        names = ["20261113T101500Z.json", "20261113T101500Z-2.json", "20261113T101459Z.json"]
        self.assertEqual(select_records_purge(names, keep=2), ["20261113T101459Z.json"])
        self.assertEqual(
            select_records_purge(names, keep=1),
            ["20261113T101459Z.json", "20261113T101500Z.json"],
        )

    def test_other_files_are_never_chosen(self) -> None:
        names = ["notes.txt", ".20261113T101500Z.json.wz-tmp", "x.json", "20261113T101500Z.json"]
        self.assertEqual(select_records_purge(names, keep=0), ["20261113T101500Z.json"])


class CleanTextTests(unittest.TestCase):
    def test_paths_under_the_hub_become_relative_and_others_are_replaced(self) -> None:
        text = f"cannot write {ROOT}/library/roms/psx/a.chd; work in /tmp/warpzone-inbox-x/item1"
        self.assertEqual(
            clean_text(text, [ROOT]),
            "cannot write library/roms/psx/a.chd; work in <path>",
        )

    def test_relative_names_and_whitelist_lines_are_kept(self) -> None:
        for text in ("roms/psx/a.chd", "!/roms/psx/a.chd", "Disc 1/2", "https://x/y", "a/b"):
            self.assertEqual(clean_text(text, [ROOT]), text)

    def test_secrets_and_long_tokens_are_redacted(self) -> None:
        self.assertEqual(clean_text("key abc123 used", (), ["abc123"]), f"key {REDACTED} used")
        self.assertEqual(clean_text(f"--zrif {ZRIF} failed"), f"--zrif {REDACTED} failed")
        sha = "a" * 64
        self.assertEqual(clean_text(f"sha {sha}"), f"sha {sha}")


class BuildRecordTests(unittest.TestCase):
    def test_the_shape_of_a_run_with_a_placed_a_review_and_a_waiting_item(self) -> None:
        waiting = ItemResult("copying.zip", left="changed 3 s ago")
        rec = record(
            [done_item(), review_item(), waiting],
            waiting=1,
            status=1,
            scrape=ScrapeNote("done", ("psx",)),
            purged=["2026-11-05"],
        )
        self.assertEqual(
            list(rec),
            [
                *("v", "id", "mode", "started", "finished", "exit"),
                *("items", "counts", "whitelist", "scrape", "purged", "warnings"),
            ],
        )
        self.assertEqual(rec["started"], "2026-11-13T10:15:00Z")
        self.assertEqual(rec["finished"], "2026-11-13T10:21:40Z")
        self.assertEqual(
            rec["counts"], {"placed": 1, "already": 0, "review": 1, "held": 0, "waiting": 1}
        )
        self.assertEqual([i["item"] for i in rec["items"]], ["Game.zip", "dump.7z"])
        first, second = rec["items"]
        self.assertEqual(first["outcome"], "done")
        self.assertEqual(first["moved_to"], "inbox/_done/2026-11-13/Game.zip")
        self.assertEqual(
            first["games"],
            [
                {
                    "system": "psx",
                    "entry": "Game (Europe).chd",
                    "outcome": "placed",
                    "path": "library/roms/psx/Game (Europe).chd",
                    "whitelist": ["!/roms/psx/Game (Europe).chd"],
                    "ra": ["RA: hash not linked"],
                    "notes": [],
                }
            ],
        )
        self.assertEqual(first["notes"], ["a note"])
        self.assertEqual(second["outcome"], "review")
        self.assertEqual(second["moved_to"], "inbox/_revisar/dump.7z")
        self.assertEqual(second["reasons"], ["no DAT match: track01.bin"])
        self.assertEqual(rec["whitelist"], ["!/roms/psx/Game (Europe).chd"])
        self.assertEqual(rec["scrape"], {"state": "done", "systems": ["psx"]})
        self.assertEqual(json.loads(render_record(rec)), rec)

    def test_already_and_exists_count_as_already_and_a_review_unit_has_no_path(self) -> None:
        results = [
            done_item("a.zip", UnitResult("nes", "A.zip", "already")),
            done_item("b.zip", UnitResult("nes", "B.zip", "exists")),
            ItemResult(
                "c.zip",
                [UnitResult("nes", "C.zip", "review", reason="x")],
                reasons=["x"],
                moved_to="_revisar/c.zip",
            ),
        ]
        rec = record(results)
        self.assertEqual((rec["counts"]["placed"], rec["counts"]["already"]), (0, 2))
        self.assertEqual(rec["whitelist"], ["!/roms/nes/A.zip", "!/roms/nes/B.zip"])
        game = rec["items"][2]["games"][0]
        self.assertEqual((game["outcome"], game["path"], game["whitelist"]), ("review", None, []))

    def test_an_original_that_could_not_be_moved_is_held_and_has_no_destination(self) -> None:
        rec = record([held_item()])
        item = rec["items"][0]
        self.assertEqual((item["outcome"], item["moved_to"]), ("held", None))
        self.assertEqual(item["games"][0]["outcome"], "placed")
        self.assertIn("could not move the original: Permission denied", item["reasons"])
        self.assertEqual((rec["counts"]["held"], rec["counts"]["review"]), (1, 0))
        self.assertEqual(item_outcome(held_item()), "held")

    def test_no_absolute_path_secret_or_zrif_reaches_the_record(self) -> None:
        result = review_item()
        result.reasons.append(f"cannot read {ROOT}/inbox/dump.7z: /mnt/share/x")
        result.notes.append(f"vita3k --zrif {ZRIF}")
        result.units.append(UnitResult("psx", "A.chd", "placed", notes=["token s3cr3t-key"]))
        rec = record([result], secrets=["s3cr3t-key"], roots=[ROOT], warnings=["see /var/log/x"])
        text = render_record(rec)
        self.assertNotIn(ZRIF, text)
        self.assertNotIn("s3cr3t-key", text)
        self.assertNotIn(ROOT, text)
        self.assertNotIn("/mnt", text)
        self.assertNotIn("/var", text)
        self.assertIn("cannot read inbox/dump.7z: <path>", text)

    def test_the_scrape_note_says_why_it_was_not_finished_or_skipped(self) -> None:
        late = ScrapeNote("not-finished", ("psx", "nes"), "failed (OSError) at /opt/x")
        rec = record([done_item()], scrape=late)
        self.assertEqual(
            rec["scrape"],
            {
                "state": "not-finished",
                "systems": ["psx", "nes"],
                "message": "failed (OSError) at <path>",
            },
        )
        self.assertNotIn("scrape", record([done_item()]))

    def test_warnings_are_capped(self) -> None:
        rec = record([done_item()], warnings=[f"w{n}" for n in range(50)])
        self.assertEqual(len(rec["warnings"]), 20)


class NotifyPayloadTests(unittest.TestCase):
    def test_the_payload_has_the_counts_the_games_and_the_items_for_review(self) -> None:
        rec = record([done_item(), review_item(), held_item()], status=1)
        self.assertEqual(
            notify_payload(rec),
            {
                "source": "inbox",
                "mode": "attended",
                "run": "20261113T101500Z",
                "placed": 2,
                "already": 0,
                "review": 1,
                "held": 1,
                "games": ["psx/Game (Europe).chd", "psx/Game (Europe).chd"],
                "review_items": ["dump.7z", "Held.zip"],
                "error": None,
            },
        )

    def test_the_lists_hold_at_most_twenty_names_and_no_path_outside_the_hub(self) -> None:
        many = [done_item(f"g{n}.zip", placed("psx", f"G{n}.chd")) for n in range(30)]
        many += [review_item(f"r{n}.7z") for n in range(30)]
        many[0].notes.append(f"{ROOT}/x /etc/passwd")
        payload = notify_payload(record(many, roots=[ROOT]), error="no DATs")
        self.assertEqual(len(payload["games"]), NOTIFY_LIST_MAX)
        self.assertEqual(len(payload["review_items"]), NOTIFY_LIST_MAX)
        self.assertEqual(payload["error"], "no DATs")
        self.assertNotIn("/", json.dumps(payload).replace("psx/", ""))

    def test_an_attended_run_sends_only_when_an_item_needs_a_person(self) -> None:
        self.assertFalse(should_notify(record([done_item()])))
        self.assertFalse(should_notify(record([done_item()]), news_found=True))
        self.assertTrue(should_notify(record([done_item(), review_item()])))
        self.assertTrue(should_notify(record([held_item()])))

    def test_an_unattended_run_sends_for_placed_games_or_news(self) -> None:
        def unattended(results: list[ItemResult]) -> dict[str, Any]:
            return record(results, mode="unattended")

        self.assertTrue(should_notify(unattended([done_item()])))
        already = done_item("a.zip", UnitResult("nes", "A.zip", "already"))
        self.assertFalse(should_notify(unattended([already])))
        self.assertTrue(should_notify(unattended([already]), news_found=True))
        self.assertFalse(should_notify(unattended([])))


class RecordFileTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.runs = self.root / "catalog" / "inbox" / "runs"

    def write(self, started: datetime = START) -> dict[str, Any]:
        return dict(
            inbox_unattended_io.write_record(
                self.root,
                started,
                lambda rid: record([done_item()], run_id=rid, started=started),
            )
        )

    def test_a_record_is_written_with_its_id_in_it_and_mode_0644(self) -> None:
        written = self.write()
        path = self.runs / "20261113T101500Z.json"
        self.assertEqual(json.loads(path.read_text()), written)
        self.assertEqual(written["id"], "20261113T101500Z")
        self.assertEqual(path.stat().st_mode & 0o777, 0o644)
        self.assertEqual([p.name for p in self.runs.iterdir()], [path.name])

    def test_a_second_run_in_the_same_second_gets_a_suffix_and_replaces_nothing(self) -> None:
        first, second, third = self.write(), self.write(), self.write()
        self.assertEqual(
            [first["id"], second["id"], third["id"]],
            ["20261113T101500Z", "20261113T101500Z-2", "20261113T101500Z-3"],
        )
        self.assertEqual(len(list(self.runs.iterdir())), 3)
        stored = json.loads((self.runs / "20261113T101500Z-2.json").read_text())
        self.assertEqual(stored["id"], second["id"])

    def test_the_purge_keeps_the_newest_fifty_and_leaves_other_files(self) -> None:
        self.runs.mkdir(parents=True)
        for n in range(60):
            (self.runs / f"202611{n // 24 + 1:02d}T{n % 24:02d}0000Z.json").write_text("{}")
        (self.runs / "README.txt").write_text("x")
        gone = inbox_unattended_io.purge_records(self.root)
        left = sorted(p.name for p in self.runs.iterdir())
        self.assertEqual(len(gone), 10)
        self.assertEqual(len(left), 51)
        self.assertEqual(gone, sorted(gone))
        self.assertTrue(all(g < left[1] for g in gone))
        self.assertEqual(inbox_unattended_io.purge_records(self.root), [])
        self.assertEqual(inbox_unattended_io.purge_records(self.root / "nowhere"), [])


class AttendedRunTests(Base):
    def runs(self) -> list[Path]:
        folder = self.hub / "catalog" / "inbox" / "runs"
        if not folder.is_dir():
            return []

        def age_of(path: Path) -> tuple[str, int]:
            stamp, _, n = path.stem.partition("-")
            return stamp, int(n or 1)

        return sorted(folder.glob("*.json"), key=age_of)

    def last(self) -> dict[str, Any]:
        found: dict[str, Any] = json.loads(self.runs()[-1].read_text())
        return found

    def test_an_apply_writes_a_record_without_an_absolute_path(self) -> None:
        self.drop("a.nes", NES_ROM)
        self.drop("mystery.nes", b"?")
        self.assertEqual(self.go(), 1)
        self.assertEqual(len(self.runs()), 1, self.out)
        text = self.runs()[0].read_text()
        for tree in (str(self.tmp), str(self.tmp.resolve())):
            self.assertNotIn(tree, text)
        rec = json.loads(text)
        self.assertEqual(rec["mode"], "attended")
        self.assertEqual(rec["exit"], 1)
        self.assertEqual(rec["counts"]["placed"], 1)
        self.assertEqual(rec["counts"]["review"], 1)
        by_name = {i["item"]: i for i in rec["items"]}
        self.assertEqual(by_name["a.nes"]["outcome"], "done")
        self.assertEqual(by_name["a.nes"]["moved_to"], "inbox/_done/2026-10-27/a.nes")
        game = by_name["a.nes"]["games"][0]
        self.assertEqual(game["path"], "library/roms/nes/Adventure (USA).zip")
        self.assertEqual(by_name["mystery.nes"]["outcome"], "review")
        self.assertEqual(rec["whitelist"], ["!/roms/nes/Adventure (USA).zip"])
        self.assertIn(f"run record: catalog/inbox/runs/{rec['id']}.json", self.out)

    def test_a_report_and_a_run_with_nothing_to_do_write_no_record(self) -> None:
        self.drop("a.nes", NES_ROM)
        self.go(apply=False)
        self.assertEqual(self.runs(), [])
        self.assertFalse((self.hub / "catalog").exists())
        self.go()  # places it
        self.assertEqual(len(self.runs()), 1)
        self.go()  # an empty inbox
        self.assertEqual(len(self.runs()), 1)

    def test_an_item_still_being_copied_is_waiting_and_alone_writes_nothing(self) -> None:
        (self.inbox / "fresh.nes").write_bytes(NES_ROM)
        self.go()
        self.assertEqual(self.runs(), [])
        self.drop("b.nes", NES_ROM)
        self.go()
        self.assertEqual(self.last()["counts"]["waiting"], 1)
        self.assertEqual([i["item"] for i in self.last()["items"]], ["b.nes"])

    def test_fifty_records_are_kept(self) -> None:
        folder = self.hub / "catalog" / "inbox" / "runs"
        folder.mkdir(parents=True)
        for n in range(55):
            (folder / f"20260101T0000{n:02d}Z.json").write_text("{}")
        self.drop("a.nes", NES_ROM)
        self.go()
        names = [p.name for p in self.runs()]
        self.assertEqual(len(names), 50)
        self.assertNotIn("20260101T000000Z.json", names)
        self.assertEqual(self.last()["mode"], "attended")

    def test_an_original_that_cannot_be_moved_is_held_in_the_record_review_in_the_run(self) -> None:
        self.drop("a.nes", NES_ROM)
        with mock.patch.object(inbox, "move_item", side_effect=OSError(13, "Permission denied")):
            self.assertEqual(self.go(), 1)
        self.assertIn("could not move the original: Permission denied", self.out)
        item = self.last()["items"][0]
        self.assertEqual((item["outcome"], item["moved_to"]), ("held", None))
        self.assertEqual(item["games"][0]["outcome"], "placed")
        self.assertEqual(self.last()["counts"]["held"], 1)

    def test_a_zrif_and_a_path_in_an_item_never_reach_the_record(self) -> None:
        self.drop("a.nes", NES_ROM)

        def fake(ctx: object, item: Path, n: int, unverified: str | None = None) -> ItemResult:
            result = ItemResult(item.name)
            result.reasons.append(f"vita3k failed: --zrif {ZRIF}")
            result.notes.append(f"read {self.hub}/inbox/{item.name} and {self.tmp}/work/x")
            return result

        with mock.patch.object(inbox, "process_item", fake):
            self.go()
        text = self.runs()[0].read_text()
        self.assertNotIn(ZRIF, text)
        self.assertNotIn(str(self.tmp), text)
        self.assertIn(REDACTED, text)

    def test_the_scrape_note_is_in_the_record(self) -> None:
        self.drop("a.nes", NES_ROM)
        self.go(no_scrape=True)
        self.assertEqual(self.last()["scrape"]["state"], "skipped")
        self.drop("b.nes", SPIRIT_ROM)
        self.go(tools=Tools(chdman=self.chdman, rahasher=self.rahasher, skyscraper=None))
        self.assertEqual(self.last()["scrape"]["message"], "Skyscraper not found")


class WebhookTests(Base):
    def test_it_fires_when_an_item_went_to_review_and_says_what_happened(self) -> None:
        self.drop("a.nes", NES_ROM)
        self.drop("mystery.nes", b"?")
        with Receiver() as hook:
            self.assertEqual(self.go(notify_url=hook.url), 1)
        self.assertEqual(len(hook.bodies), 1)
        body = hook.bodies[0]
        self.assertEqual(
            {k: body[k] for k in ("source", "mode", "placed", "already", "review", "held")},
            {
                "source": "inbox",
                "mode": "attended",
                "placed": 1,
                "already": 0,
                "review": 1,
                "held": 0,
            },
        )
        self.assertEqual(body["games"], ["nes/Adventure (USA).zip"])
        self.assertEqual(body["review_items"], ["mystery.nes"])
        self.assertRegex(body["run"], r"^\d{8}T\d{6}Z$")
        self.assertIsNone(body["error"])
        self.assertNotIn(str(self.tmp), json.dumps(body))

    def test_it_does_not_fire_when_everything_was_placed_or_with_nothing_to_do(self) -> None:
        self.drop("a.nes", NES_ROM)
        with Receiver() as hook:
            self.assertEqual(self.go(notify_url=hook.url), 0)
            self.go(notify_url=hook.url)  # an empty inbox
        self.assertEqual(hook.bodies, [])

    def test_a_report_never_fires_and_neither_does_a_run_that_fails_to_start(self) -> None:
        self.drop("mystery.nes", b"?")
        with Receiver() as hook:
            self.go(apply=False, notify_url=hook.url)
            broken = replace(self.options(notify_url=hook.url), dats_dir=self.tmp / "nope")
            with self.assertRaises(inbox.InboxError):
                inbox.run_inbox(broken, Reporter(self.lines.append))
        self.assertEqual(hook.bodies, [])

    def test_a_failing_webhook_is_a_warning_without_the_url_and_the_run_goes_on(self) -> None:
        self.drop("mystery.nes", b"?")
        with Receiver(status=500) as hook:
            self.lines.clear()
            rep = Reporter(self.lines.append)
            code = inbox.run_inbox(self.options(notify_url=hook.url), rep)
        self.assertEqual(code, 1)
        self.assertEqual(rep.warnings, ["notification rejected: HTTP 500"])
        self.assertNotIn("secret-id", self.out + "".join(rep.warnings))
        self.assertEqual(self.review(), ["mystery.nes", "mystery.nes.reason.txt"])

    def test_the_url_is_not_in_the_record_or_the_options_repr(self) -> None:
        self.drop("mystery.nes", b"?")
        with Receiver() as hook:
            opts: Options = self.options(notify_url=hook.url)
            self.assertNotIn("secret-id", repr(opts))
            inbox.run_inbox(opts, Reporter(self.lines.append))
        record_text = next((self.hub / "catalog" / "inbox" / "runs").glob("*.json")).read_text()
        self.assertNotIn("secret-id", record_text + self.out)


class CliTests(Base):
    def cli(self, *args: str) -> tuple[int, str, str]:
        config = self.tmp / "config.toml"
        config.write_text(
            f'[inbox]\nroot = "{self.hub}"\ndats_dir = "{self.dats}"\n'
            f'work_dir = "{self.tmp / "work"}"\nchdman = "{self.chdman}"\n'
        )
        with mock.patch.dict(os.environ, {"XDG_CACHE_HOME": str(self.tmp / "xdg")}):
            return run("inbox", "--config", str(config), *args)

    def test_notify_url_posts_for_a_review_and_a_bad_scheme_is_a_usage_error(self) -> None:
        self.drop("mystery.nes", b"?")
        with Receiver() as hook:
            code, out, err = self.cli("--apply", "--notify-url", hook.url)
        self.assertEqual(code, 1, out + err)
        self.assertEqual(hook.bodies[0]["review_items"], ["mystery.nes"])
        self.assertNotIn("secret-id", out + err)
        code, _, err = self.cli("--apply", "--notify-url", "ftp://example.invalid/secret-id")
        self.assertEqual(code, 2)
        self.assertNotIn("secret-id", err)


if __name__ == "__main__":
    unittest.main()
