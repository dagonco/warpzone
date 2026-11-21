"""Deleting a game from the library UI over a real server, a temporary hub tree and the fake
Syncthing: the dry run, the deletion and its rollback, every refusal, the trash listing, the
restore and its conflicts, Delete now, the purge after 7 days and the whitelist cleanup."""

import json
import os
import shutil
import threading
import time
import unittest
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from unittest import mock
from urllib.parse import parse_qs, urlsplit

import test_serve
from library_support import games, managed, write_catalog
from support import DECK_ID, MACHINE_ID
from test_serve import EXAMPLE, OTHER, PS4, ServeCase, by_title
from warpzone import catalog, inbox_lock, prune, trash, trash_run, trash_store
from warpzone import whitelist as wl
from warpzone.syncthing import Client

MGS = by_title("psx", "Metal Gear Solid (USA)")
DONE = {
    "completion": 100,
    "needBytes": 0,
    "needItems": 0,
    "needDeletes": 0,
    "remoteState": "valid",
}
DELETED = {"global": {"deleted": True}, "local": {"deleted": True}}
LIVE = {"global": {"deleted": False}, "local": {"deleted": False}}
PACMAN = catalog.Game(
    id=catalog.game_id("roms/mame/pacman.zip"),
    title="Pac-Man",
    key="pac-man",
    primary="roms/mame/pacman.zip",
    parts=(catalog.Part("roms/mame/pacman.zip", "file", 10, 1_700_000_000),),
    gamelist="./pacman.zip",
    size=10,
    added="2026-10-28",
)
PUCKMAN = replace(
    PACMAN,
    id=catalog.game_id("roms/mame/puckman.zip"),
    title="Puckman",
    key="puckman",
    primary="roms/mame/puckman.zip",
    parts=(catalog.Part("roms/mame/puckman.zip", "file", 10, 1_700_000_000),),
    gamelist="./puckman.zip",
)
DAT = (
    '<?xml version="1.0"?>\n<mame build="0.282">\n'
    '<machine name="pacman"><rom name="a" size="1" crc="00000001"/></machine>\n'
    '<machine name="puckman" cloneof="pacman" romof="pacman">'
    '<rom name="b" size="1" crc="00000002"/></machine>\n</mame>\n'
)
PS4_GAMELIST = (
    '<?xml version="1.0"?>\n<gameList>\n\t<game>\n'
    "\t\t<path>./shortcuts/Example PS4.desktop</path>\n\t\t<name>Example PS4</name>\n"
    "\t</game>\n</gameList>\n"
)


class TrashCase(ServeCase):
    """The ServeCase hub with real files behind two games: a PS1 image and a PS4 game folder
    with its shortcut, art of kinds the page does not read, and the hub gamelist of each."""

    def setUp(self) -> None:
        super().setUp()
        write_catalog(self.root, [*games(), ("mame", PACMAN), ("mame", PUCKMAN)])
        self.put(EXAMPLE.primary, b"example game image")
        self.put(OTHER.primary, b"other game image")
        for part in PS4.parts:
            if part.type == "dir":
                self.put(part.path + "/eboot.bin", b"x" * 40)
                self.put(part.path + "/sce_sys/param.sfo", b"sfo")
            else:
                self.put(part.path, b"[Desktop Entry]\n")
        for kind, name in (
            ("covers", "shortcuts/Example PS4.png"),
            ("manuals", "shortcuts/Example PS4.pdf"),
            ("fanart", "shortcuts/Example PS4.jpg"),
            ("covers", "shortcuts/Example PS4.m3u.png"),  # the folder form: not this game's
        ):
            self.media("ps4", kind, name)
        self.media("psx", "manuals", "Example Game (Europe).pdf")
        self.media("psx", "fanart", "Other Game.jpg")
        ps4 = self.root / "frontend" / "es-de" / "gamelists" / "ps4"
        ps4.mkdir(parents=True)
        (ps4 / "gamelist.xml").write_text(PS4_GAMELIST)
        self.write_whitelist("machine", managed(includes=[EXAMPLE.primary]))
        self.start(progress_seconds=0)

    def put(self, rel: str, data: bytes = b"x") -> Path:
        path = self.library / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return path

    def media(self, system: str, kind: str, name: str, data: bytes = b"art") -> Path:
        path = self.root / "frontend" / "media" / system / kind / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return path

    # ------------------------------------------------------------- calls

    def dry(self, game: Any) -> dict[str, Any]:
        reply = self.call("POST", f"/api/v1/games/{game.id}/delete", {"dry_run": True})
        self.assertEqual(reply.status, 200, reply.body)
        found: dict[str, Any] = reply.json
        return found

    def apply(self, game: Any, **body: Any) -> test_serve.Reply:
        expect = body.pop("expect", None) or self.dry(game)["expect"]
        return self.call(
            "POST", f"/api/v1/games/{game.id}/delete", {"dry_run": False, "expect": expect, **body}
        )

    def deleted(self, game: Any) -> str:
        reply = self.apply(game)
        self.assertEqual(reply.status, 200, reply.body)
        return str(reply.json["entry"])

    def restore(self, entry: str, **kw: Any) -> test_serve.Reply:
        return self.call("POST", f"/api/v1/trash/{entry}/restore", {}, **kw)

    def purge(self, entry: str) -> test_serve.Reply:
        return self.call("POST", f"/api/v1/trash/{entry}/purge", {})

    def trash_list(self) -> dict[str, Any]:
        reply = self.get("/api/v1/trash")
        self.assertEqual(reply.status, 200, reply.body)
        found: dict[str, Any] = reply.json
        return found

    def entries(self) -> list[str]:
        folder = self.root / "trash"
        return sorted(os.listdir(folder)) if folder.is_dir() else []

    def scans(self) -> list[tuple[str, str]]:
        found = []
        for method, path, _ in self.fake.requests:
            if method == "POST" and path.startswith("/rest/db/scan"):
                query = parse_qs(urlsplit(path).query)
                found.append((query["folder"][0], query.get("sub", [""])[0]))
        return found

    def snapshot(self) -> dict[str, Any]:
        """Every file and link below `library/`, `frontend/`, `catalog/` and `tools/` with its
        bytes (or its target): what a rollback must give back."""
        found: dict[str, Any] = {}
        for top in ("library", "frontend", "catalog", "tools"):
            base = self.root / top
            for here, dirs, files in os.walk(base):
                for name in dirs:
                    if (Path(here) / name).is_symlink():
                        found[str((Path(here) / name).relative_to(self.root))] = "link"
                for name in files:
                    path = Path(here) / name
                    rel = str(path.relative_to(self.root))
                    found[rel] = os.readlink(path) if path.is_symlink() else path.read_bytes()
        return found

    def catalog_ids(self, system: str) -> set[str]:
        doc = json.loads((self.root / "catalog" / "systems" / f"{system}.json").read_text())
        return set(doc["games"])

    def deck_lines(self) -> wl.Model:
        return wl.parse((self.library / "patterns-deck.stignore").read_text())

    def cleanups(self) -> list[Any]:
        path = self.root / "catalog" / "cleanups.json"
        return json.loads(path.read_text())["cleanups"] if path.exists() else []

    def lock(self) -> Path:
        path = self.root / "inbox" / inbox_lock.LOCK_NAME
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"host": "elsewhere", "pid": 1, "started": time.time()}))
        return path


class DryRunTests(TrashCase):
    def test_it_lists_everything_and_writes_nothing(self) -> None:
        before = self.snapshot()
        answer = self.dry(PS4)
        self.assertEqual(self.snapshot(), before)
        self.assertFalse((self.root / "trash").exists())
        self.assertTrue(answer["dry_run"])
        self.assertEqual(answer["game"]["id"], PS4.id)
        self.assertEqual(answer["game"]["name"], "Example PS4")
        by_path = {f["path"]: f for f in answer["files"]}
        folder = by_path["roms/ps4/games/CUSA00000"]
        self.assertEqual((folder["type"], folder["size"], folder["files"]), ("dir", 43, 2))
        self.assertEqual(by_path["roms/ps4/shortcuts/Example PS4.desktop"]["size"], 16)
        self.assertEqual(answer["absent"], [])
        self.assertEqual(answer["size"], 43 + 16)
        # every kind folder, whatever its name, but only this game's stem
        self.assertEqual(
            sorted(m["path"] for m in answer["media"]),
            [
                "ps4/covers/shortcuts/Example PS4.png",
                "ps4/fanart/shortcuts/Example PS4.jpg",
                "ps4/manuals/shortcuts/Example PS4.pdf",
            ],
        )
        self.assertEqual(answer["media_size"], 9)
        self.assertTrue(answer["gamelist"])
        self.assertEqual(answer["whitelists"], {})
        self.assertIsNone(answer["busy"])
        self.assertEqual(answer["saves"]["states"], 0)
        self.assertRegex(answer["purge_after"], r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$")
        self.assertRegex(answer["expect"], r"^sha256:[0-9a-f]{64}$")
        self.assertEqual(answer["warnings"], [])

    def test_the_whitelist_lines_of_each_console_are_shown(self) -> None:
        self.write_whitelist("deck", managed(["gba"], includes=[EXAMPLE.primary]))
        answer = self.dry(EXAMPLE)
        self.assertEqual(sorted(answer["whitelists"]), ["deck", "machine"])
        self.assertTrue(
            all(len(v) == 1 and v[0].startswith("!/") for v in answer["whitelists"].values())
        )

    def test_it_says_when_the_hub_is_busy(self) -> None:
        self.lock()
        answer = self.dry(EXAMPLE)
        self.assertEqual(answer["busy"]["mode"], "attended")
        self.assertIsNone(answer["busy"]["task"])
        self.assertTrue(answer["busy"]["since"])

    def test_an_unknown_game_and_a_bad_body(self) -> None:
        reply = self.call("POST", f"/api/v1/games/{'0' * 16}/delete", {"dry_run": True})
        self.assertEqual((reply.status, self.error(reply)["code"]), (404, "not-found"))
        for body in (
            {},
            {"dry_run": "yes"},
            {"dry_run": True, "x": 1},
            {"dry_run": False, "expect": 1},
        ):
            reply = self.call("POST", f"/api/v1/games/{EXAMPLE.id}/delete", body)
            self.assertEqual((reply.status, self.error(reply)["code"]), (400, "bad-request"), body)
        reply = self.call("POST", f"/api/v1/games/{EXAMPLE.id}/delete", raw=b"{}")
        self.assertEqual(reply.status, 400)
        self.assertEqual(self.get(f"/api/v1/games/{EXAMPLE.id}/delete").status, 404)
        self.assertEqual(
            self.call("POST", f"/api/v1/games/{EXAMPLE.id}/delete", auth=False).status, 401
        )

    def test_a_cookie_write_needs_the_pages_origin(self) -> None:
        cookie = {"Cookie": f"wz_session={test_serve.fresh_cookie()}"}
        reply = self.call(
            "POST",
            f"/api/v1/games/{EXAMPLE.id}/delete",
            {"dry_run": True},
            headers=cookie,
            auth=False,
        )
        self.assertEqual((reply.status, self.error(reply)["code"]), (403, "bad-origin"))

    def test_the_apply_needs_the_expect_of_a_dry_run(self) -> None:
        reply = self.call("POST", f"/api/v1/games/{EXAMPLE.id}/delete", {"dry_run": False})
        self.assertEqual((reply.status, self.error(reply)["code"]), (428, "expect-missing"))
        self.assertTrue(self.library.joinpath(EXAMPLE.primary).exists())
        self.assertFalse((self.root / "trash").exists())

    def test_a_game_none_of_whose_files_is_there_is_not_found(self) -> None:
        self.library.joinpath(TINY_PATH).parent.mkdir(parents=True, exist_ok=True)
        reply = self.call("POST", f"/api/v1/games/{TINY.id}/delete", {"dry_run": True})
        self.assertEqual((reply.status, self.error(reply)["code"]), (404, "not-found"))


TINY = test_serve.TINY
TINY_PATH = TINY.primary


class DeleteTests(TrashCase):
    def test_a_game_goes_to_the_trash_whole(self) -> None:
        self.write_whitelist("deck", managed(["gba"], includes=[PS4.primary]))
        reply = self.apply(PS4)
        self.assertEqual(reply.status, 200, reply.body)
        answer = reply.json
        entry = answer["entry"]
        self.assertRegex(entry, rf"^\d{{8}}T\d{{6}}Z-{PS4.id}$")
        self.assertEqual(answer["moved"], {"files": 2, "media": 3, "bytes": 43 + 16 + 9})
        self.assertEqual(answer["gamelist"], "removed")
        self.assertEqual(answer["pending"], {"deck": 1})
        self.assertFalse(answer["dry_run"])
        # gone from the library and the media, in the trash at the same relative paths
        for part in PS4.parts:
            self.assertFalse((self.library / part.path).exists(), part.path)
            self.assertTrue((self.root / "trash" / entry / "library" / part.path).exists())
        base = self.root / "trash" / entry / "frontend" / "media" / "ps4"
        for rel in (
            "covers/shortcuts/Example PS4.png",
            "manuals/shortcuts/Example PS4.pdf",
            "fanart/shortcuts/Example PS4.jpg",
        ):
            self.assertTrue((base / rel).is_file(), rel)
        # the folder form of the stem is not this game's
        self.assertTrue(
            self.root.joinpath("frontend/media/ps4/covers/shortcuts/Example PS4.m3u.png").is_file()
        )
        # the manifest
        manifest = json.loads((self.root / "trash" / entry / "manifest.json").read_text())
        self.assertEqual(manifest["state"], "complete")
        self.assertEqual(manifest["id"], PS4.id)
        self.assertEqual(manifest["system"], "ps4")
        self.assertEqual(manifest["name"], "Example PS4")
        self.assertEqual(len(manifest["files"]), 2)
        self.assertEqual(len(manifest["media"]), 3)
        self.assertEqual(manifest["game"]["primary"], PS4.primary)
        self.assertEqual(manifest["game"]["added"], "2026-10-28")
        self.assertEqual(
            manifest["whitelists"], {"deck": {"include": [PS4.primary], "exclude": []}}
        )
        self.assertEqual(len(manifest["gamelist"]["entries"]), 1)
        self.assertRegex(
            manifest["gamelist"]["backup"], r"^tools/logs/gamelist-backups/ps4-gamelist-"
        )
        self.assertEqual((self.root / manifest["gamelist"]["backup"]).read_text(), PS4_GAMELIST)
        # the hub gamelist lost the entry; the catalog lost the game
        gamelist = self.root / "frontend/es-de/gamelists/ps4/gamelist.xml"
        self.assertNotIn("Example PS4.desktop", gamelist.read_text())
        self.assertNotIn(PS4.id, self.catalog_ids("ps4"))
        self.assertEqual(self.get(f"/api/v1/games/{PS4.id}").status, 404)
        # the whitelist keeps its line until the console has received the deletion
        self.assertIn(PS4.primary, self.deck_lines().includes)
        (item,) = self.cleanups()
        self.assertEqual((item["entry"], item["id"], item["recorded"]), (entry, PS4.id, None))
        self.assertEqual(item["devices"], {"deck": {"include": [PS4.primary], "exclude": []}})
        # the hub's Syncthing is asked to scan what changed
        scans = self.scans()
        for part in PS4.parts:
            self.assertIn(("wz-library", part.path), scans)
        self.assertIn(("wz-media", "ps4/covers/shortcuts/Example PS4.png"), scans)
        self.assertIn(("wz-esde", "gamelists/ps4/gamelist.xml"), scans)

    def test_saves_and_the_rest_of_the_library_stay(self) -> None:
        save = self.root / "saves" / "ps4" / "x.sav"
        save.parent.mkdir(parents=True)
        save.write_bytes(b"save")
        before = self.snapshot()
        self.deleted(PS4)
        self.assertEqual(save.read_bytes(), b"save")
        after = self.snapshot()
        gone = {k for k in before if k not in after}
        self.assertTrue(
            all(
                k.startswith(("library/roms/ps4", "frontend/media/ps4"))
                or k.startswith("frontend/es-de")
                or k.startswith("catalog")
                for k in gone
            ),
            gone,
        )
        self.assertEqual(self.library.joinpath(OTHER.primary).read_bytes(), b"other game image")
        # an emptied folder stays
        self.assertTrue((self.library / "roms" / "ps4" / "games").is_dir())

    def test_a_game_no_whitelist_names_records_no_cleanup(self) -> None:
        self.write_whitelist("machine", managed())
        self.write_whitelist("deck", managed(["gba"]))
        self.deleted(OTHER)
        self.assertEqual(self.cleanups(), [])
        self.assertFalse((self.root / "catalog" / "cleanups.json").exists())

    def test_a_plan_that_changed_since_the_dry_run_is_stale_and_carries_the_new_plan(self) -> None:
        expect = self.dry(EXAMPLE)["expect"]
        self.media("psx", "screenshots", "Example Game (Europe).png")
        before = self.snapshot()
        reply = self.apply(EXAMPLE, expect=expect)
        self.assertEqual((reply.status, self.error(reply)["code"]), (412, "stale"))
        shown = self.error(reply)["plan"]
        self.assertNotEqual(shown["expect"], expect)
        self.assertEqual(len(shown["media"]), 3)
        self.assertEqual(self.snapshot(), before)
        self.assertFalse((self.root / "trash").exists())
        self.assertEqual(self.apply(EXAMPLE, expect=shown["expect"]).status, 200)

    def test_a_whitelist_line_added_after_the_dry_run_is_stale(self) -> None:
        expect = self.dry(OTHER)["expect"]
        self.write_whitelist("deck", managed(["gba"], includes=[EXAMPLE.primary, OTHER.primary]))
        reply = self.apply(OTHER, expect=expect)
        self.assertEqual((reply.status, self.error(reply)["code"]), (412, "stale"))

    def test_an_entry_name_already_taken_gets_a_suffix(self) -> None:
        fixed = datetime(2026, 11, 16, 10, 15, tzinfo=UTC)
        self.start(now=lambda: fixed, progress_seconds=0)
        taken = trash.entry_name(fixed, EXAMPLE.id)
        (self.root / "trash" / taken).mkdir(parents=True)
        entry = self.deleted(EXAMPLE)
        self.assertEqual(entry, taken + "-2")
        self.assertTrue((self.root / "trash" / taken).is_dir())

    def test_a_missing_part_is_listed_and_the_rest_deleted(self) -> None:
        shortcut = self.library / "roms/ps4/shortcuts/Example PS4.desktop"
        shortcut.unlink()
        answer = self.dry(PS4)
        self.assertEqual(answer["absent"], ["roms/ps4/shortcuts/Example PS4.desktop"])
        self.assertEqual(self.apply(PS4).status, 200)
        self.assertFalse((self.library / PS4.primary).exists())

    def test_a_game_without_a_gamelist_entry_or_a_file(self) -> None:
        (self.root / "frontend/es-de/gamelists/ps4/gamelist.xml").unlink()
        answer = self.apply(PS4).json
        self.assertEqual(answer["gamelist"], "none")
        manifest = json.loads((self.root / "trash" / answer["entry"] / "manifest.json").read_text())
        self.assertIsNone(manifest["gamelist"])

    def test_an_unreadable_gamelist_leaves_the_entry_and_the_deletion_goes_on(self) -> None:
        path = self.root / "frontend/es-de/gamelists/ps4/gamelist.xml"
        path.write_bytes(b"\xff\xfe not utf-8")
        answer = self.apply(PS4).json
        self.assertEqual(answer["gamelist"], "failed")
        self.assertTrue(any("gamelist" in w for w in answer["warnings"]))
        self.assertEqual(path.read_bytes(), b"\xff\xfe not utf-8")
        self.assertFalse((self.library / PS4.primary).exists())

    def test_a_failed_scan_is_a_warning(self) -> None:
        with mock.patch.object(Client, "scan", side_effect=trash_run.SyncthingError("down")):
            reply = self.apply(PS4)
        self.assertEqual(reply.status, 200)
        self.assertTrue(any("scan" in w for w in reply.json["warnings"]))
        self.assertFalse((self.library / PS4.primary).exists())

    def test_a_catalog_run_is_asked_for(self) -> None:
        calls: list[int] = []
        trasher = self.app.trash
        trasher.request_run = lambda: calls.append(1)
        self.deleted(OTHER)
        self.assertEqual(calls, [1])


class RefusalTests(TrashCase):
    def refused(self, game: Any, status: int, code: str, *, dry: bool = True) -> dict[str, Any]:
        before = self.snapshot()
        if dry:
            reply = self.call("POST", f"/api/v1/games/{game.id}/delete", {"dry_run": True})
        else:
            reply = self.apply(game)
        self.assertEqual((reply.status, self.error(reply)["code"]), (status, code), reply.body)
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(self.entries(), [])
        return self.error(reply)

    def test_a_symlinked_part(self) -> None:
        target = self.tmp / "elsewhere.chd"
        target.write_bytes(b"outside")
        path = self.library / EXAMPLE.primary
        path.unlink()
        path.symlink_to(target)
        error = self.refused(EXAMPLE, 409, "unsafe-path")
        self.assertEqual(error["paths"], [EXAMPLE.primary])
        self.assertEqual(target.read_bytes(), b"outside")

    def test_a_folder_above_a_part_that_is_a_link(self) -> None:
        elsewhere = self.tmp / "outside" / "ps4"
        shutil.move(self.library / "roms" / "ps4", elsewhere)
        (self.library / "roms" / "ps4").symlink_to(elsewhere)
        self.refused(PS4, 409, "unsafe-path")
        self.assertTrue((elsewhere / "games" / "CUSA00000" / "eboot.bin").exists())

    def test_a_part_of_the_wrong_type(self) -> None:
        shutil.rmtree(self.library / PS4.primary)
        self.put(PS4.primary, b"now a file")
        self.refused(PS4, 409, "unsafe-path")

    def test_a_part_another_game_contains(self) -> None:
        inside = replace(
            OTHER,
            id=catalog.game_id("roms/ps4/games/CUSA00000/inner.bin"),
            primary="roms/ps4/games/CUSA00000/inner.bin",
            parts=(catalog.Part("roms/ps4/games/CUSA00000/inner.bin", "file", 1, 1),),
            title="Inner",
        )
        write_catalog(self.root, [*games(), ("ps4", inside)])
        # the catalog now has a ps4 game inside the other one's folder: neither can be deleted
        self.put("roms/ps4/games/CUSA00000/inner.bin")
        self.refused(PS4, 409, "unsafe-path")
        self.refused(inside, 409, "unsafe-path")

    def test_a_whitelist_that_needs_migration(self) -> None:
        self.write_whitelist("deck", "/roms/psx/hand written\n*\n")
        error = self.refused(EXAMPLE, 409, "not-managed")
        self.assertEqual(error["device"], "deck")

    def test_the_hub_is_busy(self) -> None:
        expect = self.dry(EXAMPLE)["expect"]
        lock = self.lock()
        before = self.snapshot()
        for age in (0, 99 * 3600):  # however old the lock is
            os.utime(lock, (time.time() - age, time.time() - age))
            reply = self.apply(EXAMPLE, expect=expect)
            self.assertEqual((reply.status, self.error(reply)["code"]), (409, "hub-busy"))
        self.assertEqual(self.snapshot() | {"x": 0}, before | {"x": 0})
        self.assertTrue(lock.exists())
        self.assertTrue(self.library.joinpath(EXAMPLE.primary).exists())

    def test_trash_that_cannot_take_a_move(self) -> None:
        with mock.patch.object(
            trash_store,
            "trash_problem",
            return_value="trash/ is on another filesystem than library/",
        ):
            self.refused(EXAMPLE, 409, "trash-unavailable")
        (self.root / "trash").write_text("a file in the way")
        self.refused(EXAMPLE, 409, "trash-unavailable")
        (self.root / "trash").unlink()
        (self.tmp / "elsewhere").mkdir()
        (self.root / "trash").symlink_to(self.tmp / "elsewhere")
        self.refused(EXAMPLE, 409, "trash-unavailable")

    def test_trash_on_another_filesystem_by_device(self) -> None:
        real = os.stat

        def stat(path: Any, *args: Any, **kw: Any) -> Any:
            info = real(path, *args, **kw)
            if str(path).endswith("/trash"):
                fields = list(info)
                fields[2] = info.st_dev + 1
                return os.stat_result(fields)
            return info

        (self.root / "trash").mkdir()
        with mock.patch.object(trash_store.os, "stat", stat):
            self.refused(EXAMPLE, 409, "trash-unavailable")

    def test_pending_cleanups_that_cannot_be_read(self) -> None:
        path = self.root / "catalog" / "cleanups.json"
        path.write_text("not json")
        error = self.refused(EXAMPLE, 500, "read-failed")
        self.assertIn("cleanups.json", error["message"])
        self.assertNotIn(str(self.root), error["message"])
        path.write_text(json.dumps({"format": 2}))
        self.refused(EXAMPLE, 500, "read-failed")
        path.unlink()
        path.mkdir()
        self.refused(EXAMPLE, 500, "read-failed")

    def test_refusals_apply_to_the_apply_too(self) -> None:
        expect = self.dry(EXAMPLE)["expect"]
        self.write_whitelist("deck", "/roms/psx/hand written\n*\n")
        before = self.snapshot()
        reply = self.apply(EXAMPLE, expect=expect)
        self.assertEqual((reply.status, self.error(reply)["code"]), (409, "not-managed"))
        self.assertEqual(self.snapshot(), before)


class ArcadeTests(TrashCase):
    def with_dat(self, dat: str | None = DAT) -> None:
        path = self.tmp / "mame.xml"
        if dat is not None:
            path.write_text(dat)
        inbox = replace(self.cfg.inbox, mame_dat=str(path) if dat is not None else None)
        self.cfg = replace(self.cfg, inbox=inbox)
        self.start(progress_seconds=0)
        self.put(PACMAN.primary, b"parent")
        self.put(PUCKMAN.primary, b"clone")

    def test_a_set_others_need_is_refused_naming_them(self) -> None:
        self.with_dat()
        reply = self.call("POST", f"/api/v1/games/{PACMAN.id}/delete", {"dry_run": True})
        self.assertEqual((reply.status, self.error(reply)["code"]), (409, "has-dependants"))
        self.assertEqual(
            self.error(reply)["dependants"],
            [{"id": PUCKMAN.id, "title": "Puckman", "file": "puckman.zip"}],
        )
        self.assertTrue((self.library / PACMAN.primary).exists())

    def test_a_clone_goes_alone_and_then_its_parent_can(self) -> None:
        self.with_dat()
        self.assertEqual(self.apply(PUCKMAN).status, 200)
        self.assertTrue((self.library / PACMAN.primary).exists())
        self.assertEqual(self.apply(PACMAN).status, 200)

    def test_no_readable_dat_refuses_an_arcade_deletion(self) -> None:
        self.with_dat(None)
        reply = self.call("POST", f"/api/v1/games/{PACMAN.id}/delete", {"dry_run": True})
        self.assertEqual((reply.status, self.error(reply)["code"]), (409, "dats-missing"))
        (self.tmp / "mame.xml").write_text("not xml")
        self.cfg = replace(
            self.cfg, inbox=replace(self.cfg.inbox, mame_dat=str(self.tmp / "mame.xml"))
        )
        self.start(progress_seconds=0)
        reply = self.call("POST", f"/api/v1/games/{PACMAN.id}/delete", {"dry_run": True})
        self.assertEqual(self.error(reply)["code"], "dats-missing")
        self.assertTrue(any("cannot be read" in line for line in self.logs))

    def test_the_dat_is_kept_while_it_does_not_change(self) -> None:
        self.with_dat()
        with mock.patch.object(
            trash_store.inbox_mame, "load_sets", wraps=trash_store.inbox_mame.load_sets
        ) as load:
            for _ in range(3):
                self.call("POST", f"/api/v1/games/{PUCKMAN.id}/delete", {"dry_run": True})
        self.assertEqual(load.call_count, 1)


class RollbackTests(TrashCase):
    def fail_on(self, nth: int, *, also_undo: bool = False) -> Any:
        real = trash_store._rename
        calls: list[tuple[Path, Path]] = []

        def rename(src: Path, dst: Path) -> None:
            calls.append((src, dst))
            if len(calls) == nth or (also_undo and len(calls) > nth):
                raise OSError(28, "No space left on device")
            real(src, dst)

        return mock.patch.object(trash_store, "_rename", rename)

    def test_a_taken_entry_name_is_left_alone_even_as_a_link(self) -> None:
        fixed = datetime(2026, 11, 16, 10, 0, tzinfo=UTC)
        self.start(now=lambda: fixed, progress_seconds=0)
        name = trash.entry_name(fixed, OTHER.id)
        (self.root / "trash").mkdir()
        outside = self.tmp / "outside"
        (outside / "sub").mkdir(parents=True)
        (outside / trash.MANIFEST).write_text("not yours")
        taken = self.root / "trash" / name
        for make in (
            lambda: taken.symlink_to(outside),
            lambda: shutil.copytree(outside, taken),
        ):
            make()
            with mock.patch.object(trash, "entry_name", lambda *a, **k: name):
                reply = self.apply(OTHER)
            self.assertEqual((reply.status, self.error(reply)["code"]), (500, "write-failed"))
            self.assertEqual((outside / trash.MANIFEST).read_text(), "not yours")
            self.assertTrue((outside / "sub").is_dir())
            self.assertTrue((self.library / OTHER.primary).exists())
            self.assertEqual(self.cleanups(), [])
            if taken.is_symlink():
                taken.unlink()
            else:
                shutil.rmtree(taken)

    def test_a_move_that_fails_halfway_puts_everything_back(self) -> None:
        self.write_whitelist("deck", managed(["gba"], includes=[PS4.primary]))
        before = self.snapshot()
        with self.fail_on(3):
            reply = self.apply(PS4)
        self.assertEqual((reply.status, self.error(reply)["code"]), (500, "move-failed"))
        self.assertIs(self.error(reply)["incomplete"], False)
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(self.entries(), [])
        self.assertEqual(self.cleanups(), [])
        self.assertIn(PS4.id, self.catalog_ids("ps4"))
        self.assertEqual(self.get(f"/api/v1/games/{PS4.id}").status, 200)
        self.assertEqual(self.deck_lines().includes, frozenset({PS4.primary}))
        # nothing is announced for a deletion that did not happen
        self.assertEqual(self.scans(), [])
        self.assertEqual(self.apply(PS4).status, 200)  # and it works the next time

    def test_a_move_back_that_fails_too_leaves_the_entry_incomplete(self) -> None:
        self.write_whitelist("deck", managed(["gba"], includes=[PS4.primary]))
        with self.fail_on(3, also_undo=True):
            reply = self.apply(PS4)
        error = self.error(reply)
        self.assertEqual(
            (reply.status, error["code"], error["incomplete"]), (500, "move-failed", True)
        )
        self.assertTrue(error["paths"])
        (entry,) = self.entries()
        self.assertEqual(error["entry"], entry)
        manifest = json.loads((self.root / "trash" / entry / "manifest.json").read_text())
        self.assertEqual(manifest["state"], "moving")
        listed = self.trash_list()["entries"][0]
        self.assertEqual(
            (listed["entry"], listed["state"], listed["days_left"]), (entry, "incomplete", None)
        )
        # the recorded lines stay pending
        self.assertEqual(len(self.cleanups()), 1)
        # a part that never left is still where it was
        for path in error["paths"]:
            self.assertTrue((self.root / "trash" / entry / path).exists(), path)

    def test_the_incomplete_entry_restores_what_is_there(self) -> None:
        with self.fail_on(3, also_undo=True):
            reply = self.apply(PS4)
        entry = self.error(reply)["entry"]
        restored = self.restore(entry)
        self.assertEqual(restored.status, 200, restored.body)
        for part in PS4.parts:
            self.assertTrue((self.library / part.path).exists(), part.path)
        self.assertEqual(self.entries(), [])
        self.assertEqual(self.cleanups(), [])

    def test_a_path_that_changed_before_its_move_stops_the_deletion(self) -> None:
        real = trash_store.gather

        def gather(*args: Any, **kw: Any) -> Any:
            found = real(*args, **kw)
            path = self.library / PS4.parts[1].path  # the shortcut, moved after the folder
            path.unlink()
            path.symlink_to(self.secret)
            return found

        self.write_whitelist("deck", managed(["gba"], includes=[PS4.primary]))
        expect = self.dry(PS4)["expect"]
        before = self.snapshot()
        with mock.patch.object(trash_store, "gather", gather):
            reply = self.apply(PS4, expect=expect)
        error = self.error(reply)
        self.assertEqual((reply.status, error["code"]), (409, "unsafe-path"))
        self.assertEqual(error["paths"], ["library/roms/ps4/shortcuts/Example PS4.desktop"])
        after = self.snapshot()
        after.pop("library/roms/ps4/shortcuts/Example PS4.desktop")
        before.pop("library/roms/ps4/shortcuts/Example PS4.desktop")
        self.assertEqual(after, before)
        self.assertEqual(self.entries(), [])
        self.assertEqual(self.cleanups(), [])
        self.assertEqual(self.secret.read_text(), "do not serve")

    def test_a_file_that_changed_size_before_its_move_stops_the_deletion(self) -> None:
        real = trash_store.gather

        def gather(*args: Any, **kw: Any) -> Any:
            found = real(*args, **kw)
            self.library.joinpath(EXAMPLE.primary).write_bytes(b"grew while the plan was made")
            return found

        expect = self.dry(EXAMPLE)["expect"]
        with mock.patch.object(trash_store, "gather", gather):
            reply = self.apply(EXAMPLE, expect=expect)
        self.assertEqual((reply.status, self.error(reply)["code"]), (409, "unsafe-path"))
        self.assertEqual(self.entries(), [])
        self.assertTrue(self.library.joinpath(EXAMPLE.primary).exists())

    def test_a_cleanups_file_that_cannot_be_written_stops_before_any_move(self) -> None:
        self.write_whitelist("deck", managed(["gba"], includes=[PS4.primary]))
        before = self.snapshot()
        with mock.patch.object(
            trash_store.safefile, "atomic_write", side_effect=OSError(13, "denied")
        ):
            reply = self.apply(PS4)
        self.assertEqual((reply.status, self.error(reply)["code"]), (500, "write-failed"))
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(self.entries(), [])


class RestoreTests(TrashCase):
    def test_a_restore_gives_back_the_same_bytes_and_the_catalog_record(self) -> None:
        self.write_whitelist("deck", managed(["gba"], includes=[PS4.primary]))
        before = self.snapshot()
        record = json.loads((self.root / "catalog/systems/ps4.json").read_text())["games"][PS4.id]
        mtimes = {p: p.stat().st_mtime_ns for p in (self.library / PS4.primary).rglob("*")}
        entry = self.deleted(PS4)
        reply = self.restore(entry)
        self.assertEqual(reply.status, 200, reply.body)
        answer = reply.json
        self.assertEqual(answer["entry"], entry)
        self.assertEqual(answer["game"], {"id": PS4.id, "system": "ps4", "title": "Example PS4"})
        self.assertEqual(answer["restored"], {"files": 2, "media": 3})
        self.assertEqual(answer["gamelist"], "restored")
        self.assertEqual((answer["kept"], answer["removed"]), (["deck"], []))
        after = self.snapshot()
        for key in before:
            if not key.startswith(("catalog/", "tools/", "frontend/es-de")):
                self.assertEqual(after.get(key), before[key], key)
        self.assertEqual(
            {p: p.stat().st_mtime_ns for p in (self.library / PS4.primary).rglob("*")}, mtimes
        )
        self.assertEqual(self.entries(), [])
        self.assertEqual(self.cleanups(), [])
        gamelist = (self.root / "frontend/es-de/gamelists/ps4/gamelist.xml").read_text()
        self.assertIn("<path>./shortcuts/Example PS4.desktop</path>", gamelist)
        self.assertIn("<name>Example PS4</name>", gamelist)
        # the record is back with what only the inbox knew
        again = json.loads((self.root / "catalog/systems/ps4.json").read_text())["games"][PS4.id]
        self.assertEqual(again["added"], record["added"])
        self.assertEqual(again, record)
        self.assertEqual(self.get(f"/api/v1/games/{PS4.id}").status, 200)
        self.assertIn(("wz-library", PS4.primary), self.scans())

    def test_a_restore_that_loses_a_race_with_a_purge_is_not_found(self) -> None:
        entry = self.deleted(OTHER)
        real = trash_run.Trasher._require_entry

        def check_then_purge(trasher: Any, name: str) -> None:
            real(trasher, name)  # the entry is there when the restore looks ...
            shutil.rmtree(self.root / "trash" / name)  # ... and gone once it holds the locks

        with mock.patch.object(trash_run.Trasher, "_require_entry", check_then_purge):
            reply = self.restore(entry)
        self.assertEqual((reply.status, self.error(reply)["code"]), (404, "not-found"))

    def test_a_file_that_appears_while_restoring_is_a_conflict_and_is_never_replaced(self) -> None:
        entry = self.deleted(OTHER)
        real = trash_store._rename

        def appears_first(src: Path, dst: Path) -> None:
            if dst == self.library / OTHER.primary:
                dst.write_bytes(b"filed again a moment ago")  # after the check, before the move
            real(src, dst)

        with mock.patch.object(trash_store, "_rename", appears_first):
            reply = self.restore(entry)
        self.assertEqual((reply.status, self.error(reply)["code"]), (409, "restore-conflict"))
        self.assertEqual(self.error(reply)["paths"], ["library/" + OTHER.primary])
        self.assertEqual((self.library / OTHER.primary).read_bytes(), b"filed again a moment ago")
        # everything else went back into the trash, the entry is as it was
        self.assertEqual(self.entries(), [entry])
        manifest = json.loads((self.root / "trash" / entry / "manifest.json").read_text())
        self.assertEqual(manifest["state"], "complete")
        self.assertTrue((self.root / "trash" / entry / "library" / OTHER.primary).exists())

    def test_a_move_never_replaces_what_is_at_its_target(self) -> None:
        src, dst = self.tmp / "src", self.tmp / "dst"
        src.mkdir()
        dst.mkdir()
        (src / "f").write_bytes(b"new")
        (dst / "f").write_bytes(b"old")
        with self.assertRaises(FileExistsError):
            trash_store._rename(src / "f", dst / "f")
        self.assertEqual((dst / "f").read_bytes(), b"old")
        self.assertEqual((src / "f").read_bytes(), b"new")
        (src / "d").mkdir()
        (src / "d" / "inner").write_bytes(b"x")
        (dst / "d").mkdir()  # an empty folder is refused too
        with self.assertRaises(FileExistsError):
            trash_store._rename(src / "d", dst / "d")
        self.assertTrue((src / "d" / "inner").exists())
        # a free target: the file keeps its inode and its mtime
        before = (src / "f").stat()
        trash_store._rename(src / "f", dst / "g")
        after = (dst / "g").stat()
        self.assertEqual((before.st_ino, before.st_mtime_ns), (after.st_ino, after.st_mtime_ns))
        self.assertFalse((src / "f").exists())
        self.assertEqual(after.st_nlink, 1)

    def test_a_restore_puts_the_old_record_back_and_asks_for_a_catalog_run(self) -> None:
        entry = self.deleted(OTHER)
        calls: list[int] = []
        self.app.trash.request_run = lambda: calls.append(1)
        self.assertEqual(self.restore(entry).status, 200)
        self.assertEqual(calls, [1])
        # a refused restore asks for nothing
        self.assertEqual(self.restore(entry).status, 404)
        self.assertEqual(calls, [1])

    def test_a_restore_keeps_what_a_catalog_run_made_in_the_meantime(self) -> None:
        entry = self.deleted(OTHER)
        self.put(OTHER.primary, b"filed again by hand")  # a conflict
        self.assertEqual(self.restore(entry).status, 409)
        self.library.joinpath(OTHER.primary).unlink()
        # a run that derived the game again before the restore
        derived = replace(OTHER, added="2026-11-20", dat=None)
        write_catalog(self.root, [*games(), ("psx", derived)])
        self.assertEqual(self.restore(entry).status, 200)
        record = json.loads((self.root / "catalog/systems/psx.json").read_text())["games"][OTHER.id]
        self.assertEqual(record["added"], "2026-11-20")

    def test_the_lines_of_a_console_that_already_lost_them_are_not_added_back(self) -> None:
        self.write_whitelist("deck", managed(["gba"], includes=[EXAMPLE.primary]))
        entry = self.deleted(EXAMPLE)
        self.fake.file_info[("wz-library", EXAMPLE.primary)] = DELETED
        self.fake.completion[("wz-library", DECK_ID)] = DONE
        self.assertEqual(self.app.trash.cleanup_pass(), ["deck"])
        self.assertNotIn(EXAMPLE.primary, self.deck_lines().includes)
        reply = self.restore(entry)
        answer = reply.json
        self.assertEqual((answer["kept"], answer["removed"]), (["machine"], ["deck"]))
        self.assertNotIn(EXAMPLE.primary, self.deck_lines().includes)  # off there, like a new game
        self.assertEqual(self.cleanups(), [])
        self.assertIn(
            EXAMPLE.primary,
            wl.parse((self.library / "patterns-machine.stignore").read_text()).includes,
        )

    def test_a_restore_cancels_the_pending_cleanups_of_its_entry_only(self) -> None:
        self.write_whitelist("deck", managed(["gba"], includes=[EXAMPLE.primary, OTHER.primary]))
        first = self.deleted(EXAMPLE)
        second = self.deleted(OTHER)
        self.assertEqual(self.restore(first).status, 200)
        self.assertEqual([c["entry"] for c in self.cleanups()], [second])

    def test_a_path_that_exists_again_is_a_conflict_and_nothing_moves(self) -> None:
        entry = self.deleted(PS4)
        self.put(PS4.parts[1].path, b"filed again by inbox")
        self.media("ps4", "covers", "shortcuts/Example PS4.png", b"new art")
        before = self.snapshot()
        reply = self.restore(entry)
        error = self.error(reply)
        self.assertEqual(
            (reply.status, error["code"], error["entry"]), (409, "restore-conflict", entry)
        )
        self.assertEqual(
            sorted(error["paths"]),
            [
                "frontend/media/ps4/covers/shortcuts/Example PS4.png",
                "library/roms/ps4/shortcuts/Example PS4.desktop",
            ],
        )
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(self.entries(), [entry])
        manifest = json.loads((self.root / "trash" / entry / "manifest.json").read_text())
        self.assertEqual(manifest["state"], "complete")

    def test_a_gamelist_entry_that_came_back_is_kept(self) -> None:
        entry = self.deleted(PS4)
        gamelist = self.root / "frontend/es-de/gamelists/ps4/gamelist.xml"
        gamelist.write_text(PS4_GAMELIST.replace("Example PS4</name>", "Written by ES-DE</name>"))
        answer = self.restore(entry).json
        self.assertEqual(answer["gamelist"], "present")
        self.assertIn("Written by ES-DE", gamelist.read_text())
        self.assertEqual(gamelist.read_text().count("<path>"), 1)

    def test_a_gamelist_that_is_gone_is_made_again(self) -> None:
        entry = self.deleted(PS4)
        gamelist = self.root / "frontend/es-de/gamelists/ps4/gamelist.xml"
        gamelist.unlink()
        self.assertEqual(self.restore(entry).json["gamelist"], "restored")
        self.assertIn("Example PS4.desktop", gamelist.read_text())

    def test_an_unreadable_gamelist_is_a_warning(self) -> None:
        entry = self.deleted(PS4)
        gamelist = self.root / "frontend/es-de/gamelists/ps4/gamelist.xml"
        gamelist.write_bytes(b"\xff\xfe")
        answer = self.restore(entry).json
        self.assertEqual(answer["gamelist"], "failed")
        self.assertTrue(answer["warnings"])
        self.assertTrue((self.library / PS4.primary).exists())

    def test_a_failing_move_puts_the_restored_paths_back_in_the_trash(self) -> None:
        entry = self.deleted(PS4)
        before = self.snapshot()
        real = trash_store._rename
        calls = []

        def rename(src: Path, dst: Path) -> None:
            calls.append(src)
            if len(calls) == 3:
                raise OSError(5, "I/O error")
            real(src, dst)

        with mock.patch.object(trash_store, "_rename", rename):
            reply = self.restore(entry)
        error = self.error(reply)
        self.assertEqual(
            (reply.status, error["code"], error["incomplete"]), (500, "move-failed", False)
        )
        self.assertEqual(self.snapshot(), before)
        manifest = json.loads((self.root / "trash" / entry / "manifest.json").read_text())
        self.assertEqual(manifest["state"], "complete")
        self.assertEqual(self.restore(entry).status, 200)

    def test_a_damaged_entry_is_not_restored(self) -> None:
        entry = self.deleted(OTHER)
        (self.root / "trash" / entry / "manifest.json").write_text("{")
        reply = self.restore(entry)
        self.assertEqual((reply.status, self.error(reply)["code"]), (409, "not-restorable"))
        self.assertFalse((self.library / OTHER.primary).exists())

    def test_the_hub_must_not_be_busy(self) -> None:
        entry = self.deleted(OTHER)
        self.lock()
        reply = self.restore(entry)
        self.assertEqual((reply.status, self.error(reply)["code"]), (409, "hub-busy"))
        self.assertEqual(self.entries(), [entry])

    def test_only_an_exact_entry_name_is_an_entry(self) -> None:
        entry = self.deleted(OTHER)
        for bad in ("..", "x", entry + "/", entry.lower() + "x", "%2e%2e", entry + "%0a"):
            reply = self.call("POST", f"/api/v1/trash/{bad}/restore", {})
            self.assertEqual((reply.status, self.error(reply)["code"]), (404, "not-found"), bad)
            self.assertEqual(self.call("POST", f"/api/v1/trash/{bad}/purge", {}).status, 404)
        reply = self.call("POST", "/api/v1/trash/20200101T000000Z-" + "0" * 16 + "/restore", {})
        self.assertEqual(reply.status, 404)
        reply = self.call("POST", f"/api/v1/trash/{entry}/restore", {"x": 1})
        self.assertEqual(reply.status, 400)
        self.assertEqual(self.entries(), [entry])


class TrashListTests(TrashCase):
    def test_the_listing(self) -> None:
        self.assertEqual(self.trash_list(), {"size": 0, "entries": [], "pending": []})
        self.write_whitelist("deck", managed(["gba"], includes=[EXAMPLE.primary]))
        plan = self.dry(EXAMPLE)
        entry = self.deleted(EXAMPLE)
        listing = self.trash_list()
        (row,) = listing["entries"]
        self.assertEqual(
            {
                k: row[k]
                for k in ("entry", "id", "system", "title", "name", "state", "files", "media")
            },
            {
                "entry": entry,
                "id": EXAMPLE.id,
                "system": "psx",
                "title": EXAMPLE.title,
                "name": "Example Game",
                "state": "complete",
                "files": 1,
                "media": 2,
            },
        )
        self.assertEqual(row["days_left"], 7)
        self.assertEqual(row["size"], plan["size"] + plan["media_size"])
        self.assertEqual(plan["size"], len(b"example game image"))
        self.assertEqual(listing["size"], row["size"])
        self.assertEqual(
            sorted((p["device"], p["lines"], p["waiting"]) for p in listing["pending"]),
            [("deck", 1, "hub"), ("machine", 1, "hub")],
        )

    def test_a_folder_that_is_not_an_entry_is_listed_by_nobody_and_touched_by_nobody(self) -> None:
        entry = self.deleted(OTHER)
        stray = self.root / "trash" / "notes"
        stray.mkdir()
        (stray / "keep.txt").write_text("mine")
        link = self.root / "trash" / ("20200101T000000Z-" + "0" * 16)
        link.symlink_to(self.tmp)
        self.assertEqual([e["entry"] for e in self.trash_list()["entries"]], [entry])
        self.assertEqual(self.purge(link.name).status, 404)
        self.assertEqual(self.restore(link.name).status, 404)
        self.assertTrue(self.tmp.exists() and (stray / "keep.txt").read_text() == "mine")
        self.app.trash.purge_pass()
        self.assertTrue((stray / "keep.txt").exists())

    def test_a_damaged_entry_shows_only_its_name(self) -> None:
        entry = self.deleted(OTHER)
        (self.root / "trash" / entry / "manifest.json").write_text("nope")
        (row,) = self.trash_list()["entries"]
        self.assertEqual(
            (row["entry"], row["state"], row["title"], row["days_left"]),
            (entry, "damaged", None, None),
        )

    def test_a_listing_needs_the_token(self) -> None:
        self.assertEqual(self.get("/api/v1/trash", auth=False).status, 401)
        self.assertEqual(self.call("POST", "/api/v1/trash", {}).status, 404)


class PurgeTests(TrashCase):
    def test_delete_now_removes_the_whole_entry(self) -> None:
        entry = self.deleted(PS4)
        reply = self.purge(entry)
        self.assertEqual(reply.status, 200)
        self.assertEqual((reply.json["entry"], reply.json["purged"]), (entry, True))
        self.assertGreaterEqual(reply.json["bytes"], 43 + 16 + 9)  # the manifest counts too
        self.assertEqual(self.entries(), [])
        self.assertEqual(self.purge(entry).status, 404)
        # the library and the rest are untouched
        self.assertTrue((self.library / OTHER.primary).exists())

    def test_delete_now_removes_an_incomplete_and_a_damaged_entry(self) -> None:
        first = self.deleted(OTHER)
        (self.root / "trash" / first / "manifest.json").write_text("nope")
        self.assertEqual(self.purge(first).status, 200)
        with mock.patch.object(trash_store, "_rename", side_effect=OSError(5, "no")):
            reply = self.apply(EXAMPLE)
        self.assertEqual(self.error(reply)["code"], "move-failed")
        self.assertEqual(self.entries(), [])

    def test_a_purge_that_fails_partway_says_so_and_the_next_one_finishes(self) -> None:
        entry = self.deleted(PS4)
        real = shutil.rmtree

        def broken(path: Any, *args: Any, **kw: Any) -> None:
            shutil.rmtree = real  # type: ignore[assignment]
            raise OSError(13, "Permission denied")

        with mock.patch.object(trash_store.shutil, "rmtree", broken):
            reply = self.purge(entry)
        self.assertEqual((reply.status, self.error(reply)["code"]), (500, "purge-failed"))
        self.assertEqual(self.error(reply)["entry"], entry)
        self.assertEqual(self.purge(entry).status, 200)

    def test_a_half_purged_entry_is_never_restorable(self) -> None:
        entry = self.deleted(PS4)
        tdir = self.root / "trash" / entry
        game_dir = next(p for p in PS4.parts if p.type == "dir").path

        def partial(path: Any, *args: Any, **kw: Any) -> None:
            # a rmtree that removed one file of the game, then failed
            os.unlink(Path(path) / "library" / game_dir / "eboot.bin")
            raise OSError(13, "Permission denied")

        self.assertTrue(tdir.is_dir())
        with mock.patch.object(trash_store.shutil, "rmtree", partial):
            reply = self.purge(entry)
        self.assertEqual((reply.status, self.error(reply)["code"]), (500, "purge-failed"))
        self.assertEqual([e["entry"] for e in self.trash_list()["entries"]], [])
        restored = self.restore(entry)
        self.assertEqual((restored.status, self.error(restored)["code"]), (404, "not-found"))
        self.assertFalse((self.library / game_dir).exists())
        # what is left is not an entry: the next purge pass finishes it
        self.assertEqual(self.entries(), [".purging-" + entry])
        self.app.trash.purge_pass()
        self.assertEqual(self.entries(), [])

    def test_delete_now_finishes_a_half_purged_entry(self) -> None:
        entry = self.deleted(PS4)
        with mock.patch.object(trash_store.shutil, "rmtree", side_effect=OSError(13, "no")):
            self.assertEqual(self.purge(entry).status, 500)
        self.assertEqual(self.purge(entry).status, 200)
        self.assertEqual(self.entries(), [])

    def test_the_purge_pass_leaves_a_purging_name_that_is_not_a_real_folder(self) -> None:
        outside = self.tmp / "outside"
        outside.mkdir()
        (outside / "keep.txt").write_text("keep")
        name = ".purging-20260101T000000Z-" + "2" * 16
        (self.root / "trash").mkdir(exist_ok=True)
        (self.root / "trash" / name).symlink_to(outside)
        self.app.trash.purge_pass()
        self.assertEqual(self.entries(), [name])
        self.assertTrue((outside / "keep.txt").exists())

    def test_the_purge_after_seven_days_skips_what_is_not_complete(self) -> None:
        clock = [datetime(2026, 11, 16, 10, 0, tzinfo=UTC)]
        self.start(now=lambda: clock[0], progress_seconds=0)
        old = self.deleted(OTHER)
        clock[0] += timedelta(days=3)
        young = self.deleted(EXAMPLE)
        with mock.patch.object(trash_store, "_rename", side_effect=OSError(5, "no")):
            self.assertEqual(self.error(self.apply(PS4))["code"], "move-failed")
        broken = self.deleted(PS4)
        (self.root / "trash" / broken / "manifest.json").write_text("nope")
        moving = "20260101T000000Z-" + "1" * 16
        shutil.copytree(self.root / "trash" / young, self.root / "trash" / moving)
        manifest = json.loads((self.root / "trash" / moving / "manifest.json").read_text())
        manifest.update(entry=moving, state="moving", purge_after="2026-01-08T00:00:00Z")
        (self.root / "trash" / moving / "manifest.json").write_text(json.dumps(manifest))
        clock[0] = datetime(2026, 11, 23, 9, 59, tzinfo=UTC)  # a minute before the first is due
        self.app.trash.purge_pass()
        self.assertIn(old, self.entries())
        clock[0] = datetime(2026, 11, 23, 10, 0, tzinfo=UTC)
        self.app.trash.purge_pass()
        self.assertNotIn(old, self.entries())
        self.assertIn(young, self.entries())
        clock[0] += timedelta(days=100)
        self.app.trash.purge_pass()
        self.assertEqual(self.entries(), sorted([broken, moving]))
        listing = {e["entry"]: e for e in self.trash_list()["entries"]}
        self.assertEqual(listing[moving]["state"], "incomplete")
        self.assertEqual(listing[broken]["state"], "damaged")


RECORD = "patterns-_deleted.stignore"


class RecordTests(TrashCase):
    """The record of the games the hub deleted, `library/patterns-_deleted.stignore`: written
    under the write lock after a deletion, a Delete now or a purge, taken out by a restore."""

    def record(self) -> dict[str, Any] | None:
        path = self.library / RECORD
        found: dict[str, Any] | None = (
            json.loads(path.read_text("utf-8")) if path.exists() else None
        )
        return found

    def recorded(self) -> list[str]:
        doc = self.record()
        return [] if doc is None else [e["entry"] for e in doc["entries"]]

    def item(self, entry: str) -> dict[str, Any]:
        doc = self.record()
        assert doc is not None
        (found,) = [e for e in doc["entries"] if e["entry"] == entry]
        return found

    def fail_on(self, nth: int, *, also_undo: bool = False) -> Any:
        real = trash_store._rename
        calls: list[tuple[Path, Path]] = []

        def rename(src: Path, dst: Path) -> None:
            calls.append((src, dst))
            if len(calls) == nth or (also_undo and len(calls) > nth):
                raise OSError(28, "No space left on device")
            real(src, dst)

        return mock.patch.object(trash_store, "_rename", rename)

    def trash_tree(self, entry: str) -> list[str]:
        base = self.root / "trash" / entry / "library"
        return sorted(p.relative_to(base).as_posix() for p in base.rglob("*") if p.is_file())

    def clock(self, start: datetime) -> list[datetime]:
        now = [start]
        self.start(now=lambda: now[0], progress_seconds=0)
        return now

    def test_a_deletion_records_every_file_of_the_trash_copy(self) -> None:
        entry = self.deleted(PS4)
        doc = self.record()
        assert doc is not None
        self.assertEqual(doc["format"], 1)
        item = self.item(entry)
        manifest = json.loads((self.root / "trash" / entry / "manifest.json").read_text())
        self.assertEqual(item["deleted"], manifest["deleted"])
        deleted = datetime.strptime(item["deleted"], "%Y-%m-%dT%H:%M:%SZ")
        self.assertEqual(
            item["expires"], (deleted + timedelta(days=180)).strftime("%Y-%m-%dT%H:%M:%SZ")
        )
        self.assertEqual([f["path"] for f in item["files"]], self.trash_tree(entry))
        self.assertEqual(len(item["files"]), 3)
        base = self.root / "trash" / entry / "library"
        for f in item["files"]:
            info = (base / f["path"]).stat()
            self.assertEqual(
                (f["size"], f["mtime_ns"], f["sha1"]), (info.st_size, info.st_mtime_ns, None)
            )
        self.assertIn(("wz-library", RECORD), self.scans())
        self.assertEqual([n for n in os.listdir(self.library) if n.endswith(".wz-tmp")], [])
        self.assertEqual((self.library / RECORD).stat().st_mode & 0o777, 0o644)

    def test_a_second_deletion_adds_to_the_record(self) -> None:
        first = self.deleted(OTHER)
        second = self.deleted(EXAMPLE)
        self.assertEqual(self.recorded(), sorted([first, second]))

    def test_a_rolled_back_deletion_records_nothing(self) -> None:
        with self.fail_on(3):
            reply = self.apply(PS4)
        self.assertEqual(self.error(reply)["code"], "move-failed")
        self.assertIsNone(self.record())
        self.assertNotIn(("wz-library", RECORD), self.scans())

    def test_an_incomplete_deletion_records_nothing_until_it_is_deleted_now(self) -> None:
        with self.fail_on(3, also_undo=True):
            entry = self.error(self.apply(PS4))["entry"]
        self.app.trash.purge_pass()
        self.assertIsNone(self.record())
        left = self.trash_tree(entry)
        self.assertTrue(left)
        self.assertEqual(self.purge(entry).status, 200)
        self.assertEqual([f["path"] for f in self.item(entry)["files"]], left)

    def test_a_restore_takes_the_entry_out(self) -> None:
        first = self.deleted(OTHER)
        entry = self.deleted(PS4)
        self.assertEqual(self.restore(entry).status, 200)
        self.assertEqual(self.recorded(), [first])
        self.assertGreaterEqual(self.scans().count(("wz-library", RECORD)), 3)

    def test_a_restore_that_rolls_back_puts_the_entry_back(self) -> None:
        entry = self.deleted(PS4)
        before = (self.library / RECORD).read_bytes()
        with self.fail_on(3):
            reply = self.restore(entry)
        self.assertEqual(self.error(reply)["code"], "move-failed")
        self.assertEqual((self.library / RECORD).read_bytes(), before)
        self.assertEqual(self.restore(entry).status, 200)
        self.assertEqual(self.recorded(), [])

    def test_a_restore_that_conflicts_leaves_the_record_alone(self) -> None:
        entry = self.deleted(PS4)
        before = (self.library / RECORD).read_bytes()
        self.put(PS4.parts[1].path)  # the shortcut is there again
        reply = self.restore(entry)
        self.assertEqual(self.error(reply)["code"], "restore-conflict")
        self.assertEqual((self.library / RECORD).read_bytes(), before)

    def test_a_restore_is_refused_when_the_record_cannot_be_written(self) -> None:
        entry = self.deleted(PS4)
        before = self.snapshot()
        with mock.patch.object(trash_store, "write_record", side_effect=OSError(13, "denied")):
            reply = self.restore(entry)
        self.assertEqual((reply.status, self.error(reply)["code"]), (500, "write-failed"))
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(self.entries(), [entry])

    def test_delete_now_records_what_is_in_the_trash_before_it_goes(self) -> None:
        entry = self.deleted(PS4)
        (self.library / RECORD).unlink()  # a hub that deleted it before it kept a record
        seen = self.trash_tree(entry)
        self.assertEqual(self.purge(entry).status, 200)
        self.assertEqual([f["path"] for f in self.item(entry)["files"]], seen)
        self.assertEqual(self.entries(), [])

    def test_delete_now_of_a_damaged_entry_records_it_by_the_time_in_its_name(self) -> None:
        now = self.clock(datetime(2026, 11, 16, 10, 15, 0, tzinfo=UTC))
        entry = self.deleted(OTHER)
        (self.root / "trash" / entry / "manifest.json").write_text("nope")
        (self.library / RECORD).unlink()
        now[0] += timedelta(days=2)
        self.assertEqual(self.purge(entry).status, 200)
        item = self.item(entry)
        self.assertEqual(item["deleted"], "2026-11-16T10:15:00Z")
        self.assertEqual(item["expires"], "2027-05-15T10:15:00Z")
        self.assertEqual([f["path"] for f in item["files"]], [OTHER.primary])

    def test_delete_now_does_not_purge_when_the_record_cannot_be_written(self) -> None:
        entry = self.deleted(OTHER)
        (self.library / RECORD).unlink()
        with mock.patch.object(trash_store, "write_record", side_effect=OSError(13, "denied")):
            reply = self.purge(entry)
        self.assertEqual((reply.status, self.error(reply)["code"]), (500, "write-failed"))
        self.assertEqual(self.entries(), [entry])
        self.assertTrue((self.root / "trash" / entry / "library" / OTHER.primary).exists())
        self.assertEqual(self.purge(entry).status, 200)
        self.assertEqual(self.recorded(), [entry])

    def test_delete_now_of_an_unknown_entry_is_still_not_found(self) -> None:
        self.assertEqual(self.purge("20260101T000000Z-" + "2" * 16).status, 404)
        self.assertIsNone(self.record())

    def test_the_purge_after_seven_days_records_first_and_stops_when_it_cannot(self) -> None:
        now = self.clock(datetime(2026, 11, 16, 10, 0, tzinfo=UTC))
        entry = self.deleted(OTHER)
        (self.library / RECORD).unlink()
        now[0] += timedelta(days=8)
        with mock.patch.object(trash_store, "write_record", side_effect=OSError(13, "denied")):
            self.app.trash.purge_pass()
        self.assertEqual(self.entries(), [entry])
        self.assertTrue(any("record" in line for line in self.logs))
        self.app.trash.purge_pass()
        self.assertEqual(self.entries(), [])
        self.assertEqual([f["path"] for f in self.item(entry)["files"]], [OTHER.primary])

    def test_the_background_pass_records_an_entry_made_before_the_upgrade(self) -> None:
        entry = self.deleted(OTHER)
        (self.library / RECORD).unlink()
        self.app.trash.purge_pass()
        self.assertEqual(self.recorded(), [entry])
        self.assertEqual(self.scans().count(("wz-library", RECORD)), 2)
        stamp = (self.library / RECORD).stat().st_mtime_ns
        self.app.trash.purge_pass()  # nothing new: nothing written, nothing scanned
        self.assertEqual((self.library / RECORD).stat().st_mtime_ns, stamp)
        self.assertEqual(self.scans().count(("wz-library", RECORD)), 2)

    def test_the_background_pass_does_not_record_what_a_restore_is_moving(self) -> None:
        entry = self.deleted(PS4)
        manifest = self.root / "trash" / entry / "manifest.json"
        doc = json.loads(manifest.read_text())
        doc["state"] = "restoring"
        manifest.write_text(json.dumps(doc))
        (self.library / RECORD).unlink()
        self.app.trash.purge_pass()
        self.assertIsNone(self.record())

    def test_an_entry_lives_180_days_and_is_dropped_by_the_next_write(self) -> None:
        now = self.clock(datetime(2026, 11, 16, 10, 0, tzinfo=UTC))
        old = self.deleted(OTHER)
        now[0] += timedelta(days=179)
        self.app.trash.purge_pass()
        self.assertEqual(self.recorded(), [old])
        now[0] += timedelta(days=2)
        young = self.deleted(EXAMPLE)
        self.assertEqual(self.recorded(), [young])

    def test_a_path_that_exists_again_leaves_the_record_at_the_next_write(self) -> None:
        first = self.deleted(OTHER)
        self.put(OTHER.primary)  # filed again
        second = self.deleted(EXAMPLE)
        self.assertEqual(self.item(first)["files"], [])
        self.assertEqual([f["path"] for f in self.item(second)["files"]], [EXAMPLE.primary])

    def test_a_record_that_cannot_be_read_is_never_overwritten(self) -> None:
        text = '{"format": 2, "entries": []}'
        (self.library / RECORD).write_text(text)
        entry = self.deleted(OTHER)  # the deletion itself goes on; the record is a warning
        self.assertEqual((self.library / RECORD).read_text(), text)
        self.assertTrue(any("record" in line for line in self.logs))
        reply = self.purge(entry)
        self.assertEqual((reply.status, self.error(reply)["code"]), (500, "write-failed"))
        self.assertEqual(self.entries(), [entry])

    def test_a_failed_record_is_a_warning_of_the_deletion(self) -> None:
        with mock.patch.object(trash_store, "write_record", side_effect=OSError(13, "denied")):
            reply = self.apply(OTHER)
        self.assertEqual(reply.status, 200, reply.body)
        self.assertTrue(any("record" in w for w in reply.json["warnings"]))
        self.assertIsNone(self.record())
        self.app.trash.purge_pass()  # the pass adds it
        self.assertEqual(self.recorded(), [reply.json["entry"]])

    def test_the_record_is_not_a_device_whitelist(self) -> None:
        self.deleted(OTHER)
        names = [d["name"] for d in self.get("/api/v1/devices").json]
        self.assertEqual(sorted(names), ["deck", "machine"])
        walked = [e.path for e in prune.walk(str(self.library), set(), ".stfolder")]
        self.assertNotIn(RECORD, walked)
        self.assertIn(RECORD, os.listdir(self.library))


class GamelistEditTests(TrashCase):
    NOW = datetime(2026, 11, 16, 10, 0, tzinfo=UTC)
    GAMELIST = Path("frontend/es-de/gamelists/ps4/gamelist.xml")
    CONCURRENT = PS4_GAMELIST.replace(
        "</gameList>",
        "\t<game>\n\t\t<path>./shortcuts/Concurrent.desktop</path>\n\t\t<name>Concurrent</name>\n"
        "\t</game>\n</gameList>",
    )

    def drop(self) -> trash_store.GamelistEdit:
        return trash_store.drop_gamelist(
            self.root, "ps4", ["./shortcuts/Example PS4.desktop"], self.NOW
        )

    def test_a_gamelist_replaced_after_it_was_read_is_read_again_not_overwritten(self) -> None:
        path = self.root / self.GAMELIST
        real = trash_store.read_gamelist
        calls: list[int] = []

        def read_then_syncthing_replaces(root: Path, system: str) -> tuple[str | None, str | None]:
            found = real(root, system)
            if not calls:  # once: the hub's Syncthing brings a newer file right after the read
                path.write_text(self.CONCURRENT)
            calls.append(1)
            return found

        with mock.patch.object(trash_store, "read_gamelist", read_then_syncthing_replaces):
            edit = self.drop()
        self.assertEqual(edit.status, "removed")
        text = path.read_text()
        self.assertNotIn("Example PS4.desktop", text)
        self.assertIn("Concurrent.desktop", text)  # what arrived in between is kept

    def test_a_gamelist_that_keeps_changing_is_given_up_untouched(self) -> None:
        path = self.root / self.GAMELIST
        real = trash_store.read_gamelist
        n = [0]

        def always_replaced(root: Path, system: str) -> tuple[str | None, str | None]:
            found = real(root, system)
            n[0] += 1
            path.write_text(self.CONCURRENT + "\n" * n[0])
            return found

        with mock.patch.object(trash_store, "read_gamelist", always_replaced):
            edit = self.drop()
        self.assertEqual(edit.status, "failed")
        self.assertIn("Example PS4.desktop", path.read_text())

    def test_a_backup_that_cannot_be_named_is_a_warning_and_the_deletion_completes(self) -> None:
        real = Path.exists

        def refuse(self_: Path) -> bool:
            if "gamelist-backups" in str(self_):
                raise PermissionError(13, "Permission denied")
            return real(self_)

        with mock.patch.object(Path, "exists", refuse):
            reply = self.apply(PS4)
        self.assertEqual(reply.status, 200, reply.body)
        self.assertEqual(reply.json["gamelist"], "failed")
        self.assertTrue(any("gamelist" in w for w in reply.json["warnings"]), reply.json)
        manifest = json.loads(
            (self.root / "trash" / reply.json["entry"] / "manifest.json").read_text()
        )
        self.assertEqual(manifest["state"], "complete")
        self.assertEqual(self.get(f"/api/v1/games/{PS4.id}").status, 404)


class CleanupTests(TrashCase):
    def setUp(self) -> None:
        super().setUp()
        self.write_whitelist("deck", managed(["gba"], includes=[EXAMPLE.primary]))
        self.entry = self.deleted(EXAMPLE)
        self.fake.connections = {DECK_ID: {"connected": True}}
        self.fake.completion[("wz-library", DECK_ID)] = {
            **DONE,
            "completion": 42.7,
            "needItems": 812,
            "needDeletes": 0,
        }

    def waiting(self) -> dict[str, str]:
        return {p["device"]: p["waiting"] for p in self.trash_list()["pending"]}

    def machine_lines(self) -> frozenset[str]:
        return wl.parse((self.library / "patterns-machine.stignore").read_text()).includes

    def test_nothing_is_removed_before_the_hub_has_recorded_the_deletion(self) -> None:
        self.fake.file_info[("wz-library", EXAMPLE.primary)] = LIVE
        self.fake.completion[("wz-library", DECK_ID)] = DONE
        before = (self.library / "patterns-deck.stignore").read_bytes()
        self.assertEqual(self.app.trash.cleanup_pass(), [])
        self.assertEqual((self.library / "patterns-deck.stignore").read_bytes(), before)
        (item,) = self.cleanups()
        self.assertIsNone(item["recorded"])
        self.assertEqual(self.waiting(), {"deck": "hub", "machine": "hub"})

    def test_a_console_whose_only_pending_work_is_the_deletion_keeps_its_lines(self) -> None:
        # Syncthing counts a pending deletion in `needDeletes`, not in `needItems`, and caps the
        # completion at 95 while only deletions remain.
        self.fake.file_info[("wz-library", EXAMPLE.primary)] = DELETED
        self.fake.completion[("wz-library", DECK_ID)] = {
            **DONE,
            "completion": 95,
            "needDeletes": 1,
        }
        before = (self.library / "patterns-deck.stignore").read_bytes()
        self.assertEqual(self.app.trash.cleanup_pass(), [])
        self.assertEqual((self.library / "patterns-deck.stignore").read_bytes(), before)
        self.assertEqual(self.waiting()["deck"], "syncing")
        # the same answer with the count of deletions unknown is not ready either
        self.fake.completion[("wz-library", DECK_ID)] = {
            k: v for k, v in DONE.items() if k != "needDeletes"
        }
        self.assertEqual(self.app.trash.cleanup_pass(), [])
        self.assertEqual((self.library / "patterns-deck.stignore").read_bytes(), before)
        # done: nothing left to fetch or delete
        self.fake.completion[("wz-library", DECK_ID)] = DONE
        self.assertEqual(self.app.trash.cleanup_pass(), ["deck"])

    def test_empty_folders_the_console_never_fetches_do_not_hold_its_lines(self) -> None:
        # The answer a real hub gave for a console with EmuDeck's empty system folders that
        # its whitelist does not take: the bytes are all there, nothing is left to delete,
        # and the six items are those folders, none of them the deleted game.
        self.fake.file_info[("wz-library", EXAMPLE.primary)] = DELETED
        self.fake.completion[("wz-library", DECK_ID)] = {**DONE, "needItems": 6}
        self.fake.remote_need[("wz-library", DECK_ID)] = [
            "roms/dreamcast",
            "roms/ps2",
            "roms/ps3",
            "roms/psp",
            "roms/saturn",
            "roms/switch",
        ]
        self.assertEqual(self.app.trash.cleanup_pass(), ["deck"])
        self.assertNotIn(EXAMPLE.primary, self.deck_lines().includes)

    def test_items_still_needed_at_or_below_a_deleted_path_hold_the_lines(self) -> None:
        self.fake.file_info[("wz-library", EXAMPLE.primary)] = DELETED
        self.fake.completion[("wz-library", DECK_ID)] = {**DONE, "needItems": 2}
        self.fake.remote_need[("wz-library", DECK_ID)] = ["roms/ps2", EXAMPLE.primary]
        before = (self.library / "patterns-deck.stignore").read_bytes()
        self.assertEqual(self.app.trash.cleanup_pass(), [])
        self.assertEqual((self.library / "patterns-deck.stignore").read_bytes(), before)
        self.assertEqual(self.waiting()["deck"], "syncing")

    def test_items_needed_without_a_list_of_them_hold_the_lines(self) -> None:
        # The hub could not say which items they are (no remoteneed answer): not ready.
        self.fake.file_info[("wz-library", EXAMPLE.primary)] = DELETED
        self.fake.completion[("wz-library", DECK_ID)] = {**DONE, "needItems": 6}
        before = (self.library / "patterns-deck.stignore").read_bytes()
        self.assertEqual(self.app.trash.cleanup_pass(), [])
        self.assertEqual((self.library / "patterns-deck.stignore").read_bytes(), before)

    def test_the_hub_index_is_checked_again_on_every_pass(self) -> None:
        self.fake.file_info[("wz-library", EXAMPLE.primary)] = DELETED
        self.app.trash.cleanup_pass()
        (item,) = self.cleanups()
        self.assertIsNotNone(item["recorded"])
        # the same path is filed again and deleted again: the hub's global record is live
        # until it indexes the second deletion
        self.fake.file_info[("wz-library", EXAMPLE.primary)] = LIVE
        self.fake.completion[("wz-library", DECK_ID)] = DONE
        before = (self.library / "patterns-deck.stignore").read_bytes()
        self.assertEqual(self.app.trash.cleanup_pass(), [])
        self.assertEqual((self.library / "patterns-deck.stignore").read_bytes(), before)
        (item,) = self.cleanups()
        self.assertIsNone(item["recorded"])
        self.assertEqual(self.waiting()["deck"], "hub")
        self.fake.file_info[("wz-library", EXAMPLE.primary)] = DELETED
        self.assertEqual(self.app.trash.cleanup_pass(), ["deck"])

    def test_an_index_answer_without_a_global_record_is_not_a_recorded_deletion(self) -> None:
        self.fake.file_info[("wz-library", EXAMPLE.primary)] = {"local": {"deleted": True}}
        self.fake.completion[("wz-library", DECK_ID)] = DONE
        self.assertEqual(self.app.trash.cleanup_pass(), [])
        (item,) = self.cleanups()
        self.assertIsNone(item["recorded"])

    def test_the_hub_index_is_asked_for_every_path_of_the_game(self) -> None:
        self.fake.file_info[("wz-library", EXAMPLE.primary)] = DELETED
        self.app.trash.cleanup_pass()
        asked = [p for m, p, _ in self.fake.requests if p.startswith("/rest/db/file")]
        self.assertTrue(any("Example+Game" in p or "Example%20Game" in p for p in asked), asked)
        (item,) = self.cleanups()
        self.assertRegex(item["recorded"], r"^\d{4}-\d\d-\d\dT")

    def test_a_console_that_is_not_done_keeps_its_lines(self) -> None:
        self.fake.file_info[("wz-library", EXAMPLE.primary)] = DELETED
        self.assertEqual(self.app.trash.cleanup_pass(), [])
        self.assertIn(EXAMPLE.primary, self.deck_lines().includes)
        self.assertEqual(self.waiting(), {"deck": "syncing", "machine": "offline"})
        for state, expected in (
            ({**DONE, "needItems": 1}, "syncing"),
            ({**DONE, "remoteState": "paused"}, "paused"),
            ({**DONE, "remoteState": "notSharing"}, "not-shared"),
        ):
            self.fake.completion[("wz-library", DECK_ID)] = state
            self.assertEqual(self.app.trash.cleanup_pass(), [])
            self.assertEqual(self.waiting()["deck"], expected)
        del self.fake.completion[("wz-library", DECK_ID)]
        self.assertEqual(self.app.trash.cleanup_pass(), [])
        self.assertEqual(self.waiting()["deck"], "not-shared")
        self.assertIn(EXAMPLE.primary, self.deck_lines().includes)

    def test_a_console_that_is_done_loses_its_lines_and_an_offline_one_keeps_them(self) -> None:
        self.fake.file_info[("wz-library", EXAMPLE.primary)] = DELETED
        self.fake.completion[("wz-library", DECK_ID)] = DONE
        backups = self.root / "catalog" / "whitelists" / "deck"
        before = len(list(backups.glob("*"))) if backups.exists() else 0
        self.assertEqual(self.app.trash.cleanup_pass(), ["deck"])
        self.assertNotIn(EXAMPLE.primary, self.deck_lines().includes)
        self.assertEqual(self.deck_lines().systems, frozenset({"gba"}))
        self.assertEqual(len(list(backups.glob("*"))), before + 1)  # the old file is in its history
        self.assertIn(("wz-library", "patterns-deck.stignore"), self.scans())
        (item,) = self.cleanups()
        self.assertEqual(list(item["devices"]), ["machine"])
        self.assertIn(EXAMPLE.primary, self.machine_lines())  # offline: keeps its lines
        self.assertEqual(self.waiting(), {"machine": "offline"})
        # it comes online and finishes
        self.fake.connections[MACHINE_ID] = {"connected": True}
        self.fake.completion[("wz-library", MACHINE_ID)] = DONE
        self.assertEqual(self.app.trash.cleanup_pass(), ["machine"])
        self.assertNotIn(EXAMPLE.primary, self.machine_lines())
        self.assertEqual(self.cleanups(), [])
        self.assertFalse((self.root / "catalog" / "cleanups.json").exists())
        # nothing is added: the whitelists are exactly what they would be without the game
        self.assertEqual(self.deck_lines(), wl.Model(frozenset({"gba"})))

    def test_an_exclusion_under_a_system_that_is_on_is_removed_too(self) -> None:
        self.write_whitelist("machine", managed(["psx"], excludes=[OTHER.primary]))
        other = self.deleted(OTHER)
        self.assertNotEqual(other, self.entry)
        self.fake.file_info[("wz-library", OTHER.primary)] = DELETED
        self.fake.connections[MACHINE_ID] = {"connected": True}
        self.fake.completion[("wz-library", MACHINE_ID)] = DONE
        self.assertEqual(self.app.trash.cleanup_pass(), ["machine"])
        model = wl.parse((self.library / "patterns-machine.stignore").read_text())
        self.assertEqual(model, wl.Model(frozenset({"psx"})))

    def test_the_lines_of_a_whitelist_that_needs_migration_stay(self) -> None:
        self.fake.file_info[("wz-library", EXAMPLE.primary)] = DELETED
        self.fake.completion[("wz-library", DECK_ID)] = DONE
        self.write_whitelist("deck", "/roms/psx/hand written\n*\n")
        self.assertEqual(self.app.trash.cleanup_pass(), [])
        self.assertEqual(self.waiting()["deck"], "not-managed")
        self.assertEqual(
            (self.library / "patterns-deck.stignore").read_text(), "/roms/psx/hand written\n*\n"
        )
        self.assertEqual(len(self.cleanups()), 1)

    def test_a_path_that_exists_again_keeps_its_line(self) -> None:
        self.fake.file_info[("wz-library", EXAMPLE.primary)] = DELETED
        self.fake.completion[("wz-library", DECK_ID)] = DONE
        self.put(EXAMPLE.primary, b"filed again by inbox")
        before = (self.library / "patterns-deck.stignore").read_bytes()
        self.assertEqual(self.app.trash.cleanup_pass(), [])
        self.assertEqual((self.library / "patterns-deck.stignore").read_bytes(), before)
        self.assertEqual(self.cleanups(), [])  # nothing left to wait for

    def test_a_console_that_left_the_config_is_dropped_without_a_write(self) -> None:
        devices = {k: v for k, v in self.cfg.devices.items() if k != "machine"}
        self.cfg = replace(self.cfg, devices=devices)
        self.start(progress_seconds=0)
        self.fake.file_info[("wz-library", EXAMPLE.primary)] = DELETED
        self.app.trash.cleanup_pass()
        (item,) = self.cleanups()
        self.assertEqual(list(item["devices"]), ["deck"])
        self.assertEqual([p["device"] for p in self.trash_list()["pending"]], ["deck"])

    def test_a_restore_cancels_what_is_pending(self) -> None:
        self.assertEqual(self.restore(self.entry).status, 200)
        self.fake.completion[("wz-library", DECK_ID)] = DONE
        self.assertEqual(self.app.trash.cleanup_pass(), [])
        self.assertIn(EXAMPLE.primary, self.deck_lines().includes)
        self.assertEqual(self.cleanups(), [])

    def test_a_change_that_would_turn_another_game_off_is_not_written(self) -> None:
        self.fake.file_info[("wz-library", EXAMPLE.primary)] = DELETED
        self.fake.completion[("wz-library", DECK_ID)] = DONE
        with mock.patch.object(trash, "effect_holds", return_value=False):
            self.assertEqual(self.app.trash.cleanup_pass(), [])
        self.assertIn(EXAMPLE.primary, self.deck_lines().includes)
        self.assertTrue(any("would change another game" in line for line in self.logs))
        self.assertEqual(self.app.trash.cleanup_pass(), ["deck"])  # and it is tried again

    def test_a_whitelist_that_changed_meanwhile_is_read_again_next_time(self) -> None:
        self.fake.file_info[("wz-library", EXAMPLE.primary)] = DELETED
        self.fake.completion[("wz-library", DECK_ID)] = DONE
        with mock.patch.object(
            trash_run.store, "replace", side_effect=trash_run.store.Stale("deck")
        ):
            self.assertEqual(self.app.trash.cleanup_pass(), [])
        self.assertIn(EXAMPLE.primary, self.deck_lines().includes)
        self.assertEqual(self.app.trash.cleanup_pass(), ["deck"])

    def test_no_lock_is_held_while_the_hub_is_asked(self) -> None:
        self.fake.file_info[("wz-library", EXAMPLE.primary)] = DELETED
        self.fake.completion[("wz-library", DECK_ID)] = DONE
        held: list[bool] = []
        for name in ("db_file", "connections", "completion"):
            real = getattr(Client, name)

            def wrapped(client: Any, *a: Any, real: Any = real, **kw: Any) -> Any:
                held.append(self.app.write_lock.locked())
                return real(client, *a, **kw)

            patcher = mock.patch.object(Client, name, wrapped)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.app.trash.cleanup_pass()
        self.assertTrue(held)
        self.assertFalse(any(held))

    def test_a_hub_that_does_not_answer_does_nothing_and_says_it_once(self) -> None:
        self.fake.file_info[("wz-library", EXAMPLE.primary)] = DELETED
        with mock.patch.object(Client, "db_file", side_effect=trash_run.SyncthingError("down")):
            self.assertEqual(self.app.trash.cleanup_pass(), [])
            self.assertEqual(self.app.trash.cleanup_pass(), [])
        self.assertEqual(sum("cannot ask the hub" in line for line in self.logs), 1)
        self.assertEqual(self.waiting()["deck"], "hub")
        self.assertIn(EXAMPLE.primary, self.deck_lines().includes)

    def test_the_listing_without_the_hub_says_unknown(self) -> None:
        self.fake.file_info[("wz-library", EXAMPLE.primary)] = DELETED
        self.app.trash.cleanup_pass()
        self.fake.stop = True  # type: ignore[attr-defined]
        with mock.patch.object(Client, "connections", side_effect=trash_run.SyncthingError("down")):
            self.assertEqual(self.waiting(), {"deck": "unknown", "machine": "unknown"})

    def test_the_background_task(self) -> None:
        trasher = self.app.trash
        self.assertTrue(trasher.has_work())
        passes: list[int] = []
        stop = threading.Event()

        def tick() -> None:
            passes.append(1)
            if len(passes) >= 3:
                stop.set()

        trasher.period = 0.01
        with mock.patch.object(trasher, "tick", tick):
            thread = threading.Thread(target=trasher.loop, args=(stop,))
            thread.start()
            thread.join(timeout=5)
        self.assertFalse(thread.is_alive())
        self.assertEqual(len(passes), 3)

    def test_the_task_is_idle_with_nothing_to_do_and_a_failed_pass_does_not_stop_it(self) -> None:
        self.purge(self.entry)
        (self.root / "catalog" / "cleanups.json").unlink()
        self.assertFalse(self.app.trash.has_work())
        self.app.trash.cleanup_pass()
        self.entry = self.deleted(OTHER)
        self.assertTrue(self.app.trash.has_work())
        with mock.patch.object(self.app.trash, "cleanup_pass", side_effect=RuntimeError("boom")):
            self.app.trash.tick()  # the purge pass still runs, and nothing is raised
        self.assertTrue(any("cleanup failed" in line for line in self.logs))


class LockTests(TrashCase):
    def test_the_lock_is_held_during_a_deletion_and_released_after(self) -> None:
        seen: list[Any] = []
        real = trash_store.transfer

        def transfer(*args: Any, **kw: Any) -> Any:
            lock = self.root / "inbox" / inbox_lock.LOCK_NAME
            seen.append(json.loads(lock.read_text()))
            seen.append(self.app.write_lock.locked())
            return real(*args, **kw)

        with mock.patch.object(trash_store, "transfer", transfer):
            self.assertEqual(self.apply(OTHER).status, 200)
        info, locked = seen
        self.assertEqual((info["task"], info["mode"]), ("delete", "attended"))
        self.assertTrue(locked)
        self.assertFalse((self.root / "inbox" / inbox_lock.LOCK_NAME).exists())
        self.assertFalse(self.app.write_lock.locked())

    def test_the_inbox_api_says_the_library_ui_holds_it(self) -> None:
        lock = self.root / "inbox" / inbox_lock.LOCK_NAME
        lock.parent.mkdir(exist_ok=True)
        lock.write_text(
            json.dumps(
                {
                    "host": "h",
                    "pid": 1,
                    "started": time.time(),
                    "beat": 60,
                    "mode": "attended",
                    "task": "delete",
                }
            )
        )
        busy = self.get("/api/v1/inbox").json["busy"]
        self.assertEqual((busy["task"], busy["mode"]), ("delete", "attended"))

    def test_the_lock_is_released_after_a_refusal_and_a_failure(self) -> None:
        with mock.patch.object(trash_store, "_rename", side_effect=OSError(5, "no")):
            self.apply(OTHER)
        self.assertFalse((self.root / "inbox" / inbox_lock.LOCK_NAME).exists())
        self.assertEqual(self.apply(OTHER).status, 200)
        self.assertFalse((self.root / "inbox" / inbox_lock.LOCK_NAME).exists())

    def test_a_catalog_run_asked_during_a_run_makes_another_follow(self) -> None:
        started: list[int] = []
        release = threading.Event()

        def job(ra_lists: bool) -> str:
            started.append(1)
            release.wait(5)
            return "done"

        refresher = test_serve.serve.Refresher(job, lambda text: None)
        self.assertTrue(refresher.request())
        deadline = time.time() + 5
        while not started and time.time() < deadline:
            time.sleep(0.01)
        self.assertFalse(refresher.request())  # a run is going: one more is asked for
        release.set()
        deadline = time.time() + 5
        while len(started) < 2 and time.time() < deadline:
            time.sleep(0.01)
        self.assertEqual(len(started), 2)
        deadline = time.time() + 5
        while refresher.running and time.time() < deadline:
            time.sleep(0.01)
        self.assertEqual(len(started), 2)  # and no more than one


if __name__ == "__main__":
    unittest.main()
