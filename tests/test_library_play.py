"""The library API's play statistics: ledger keys to catalog games, totals and `recent`."""

import json
import unittest
from datetime import UTC, datetime
from pathlib import Path

import test_serve
from library_support import game as make_game
from library_support import games, library
from warpzone import artwork, catalog
from warpzone.library_api import (
    RECENT_DEFAULT,
    RECENT_MAX,
    ApiError,
    PlayIndex,
    SystemExtras,
    attach_play,
    game_play,
    game_totals,
    parse_limit,
    play_keys,
    recent_response,
)
from warpzone.playstate import Ledger, Record

NOW = 1_800_000_000
LIB = library()


def by_title(system: str, title: str) -> catalog.Game:
    return next(g for s, g in games() if s == system and g.title == title)


EXAMPLE = by_title("psx", "Example Game (Europe)")
OTHER = by_title("psx", "Other Game")
PS4 = by_title("ps4", "Example PS4")
TINY = by_title("gba", "Tiny")
EX = ("psx", "Example Game (Europe).chd")
OT = ("psx", "Other Game.chd")


def index(*ledgers: Ledger, horizon: int = NOW + 86400) -> PlayIndex:
    return PlayIndex(tuple(ledgers), horizon)


def none(_: str) -> SystemExtras:
    return SystemExtras()


class MappingTests(unittest.TestCase):
    def test_a_game_is_found_by_its_gamelist_path(self) -> None:
        self.assertEqual(play_keys("psx", EXAMPLE), [EX])

    def test_a_shortcut_game_is_found_under_its_shortcut_then_its_primary(self) -> None:
        self.assertEqual(
            play_keys("ps4", PS4),
            [("ps4", "shortcuts/Example PS4.desktop"), ("ps4", "games/CUSA00000")],
        )
        ledger = Ledger("deck", "wz-esde", {("ps4", "shortcuts/Example PS4.desktop"): Record(2)})
        self.assertEqual(game_totals(index(ledger), "ps4", PS4)["plays"], 2)  # type: ignore[index]

    def test_a_game_below_a_folder_keeps_its_subpath(self) -> None:
        nested = make_game("gba", "roms/gba/Hacks/Beta.gba", "Beta", gamelist="./Hacks/Beta.gba")
        self.assertEqual(play_keys("gba", nested), [("gba", "Hacks/Beta.gba")])

    def test_the_same_path_in_another_system_is_another_game(self) -> None:
        ledger = Ledger("deck", "wz-esde", {("gbc", "Other Game.chd"): Record(5)})
        self.assertIsNone(game_totals(index(ledger), "psx", OTHER))

    def test_an_unsafe_path_maps_to_nothing(self) -> None:
        odd = make_game("psx", "roms/psx/a.chd", "A", gamelist="./../x.chd")
        self.assertEqual(play_keys("psx", odd), [("psx", "a.chd")])


class TotalsTests(unittest.TestCase):
    def test_a_game_nobody_played(self) -> None:
        self.assertEqual(
            game_play(index(), "psx", EXAMPLE),
            {
                "devices": {},
                "totals": {"plays": 0, "last": None, "fav": None, "playtime": 0},
            },
        )
        self.assertIsNone(game_totals(index(), "psx", EXAMPLE))

    def test_each_device_and_the_totals(self) -> None:
        ark = Ledger("ark", "ark", {EX: Record(1, last=NOW - 100, playtime=600)})
        deck_ = Ledger(
            "deck", "wz-esde", {EX: Record(3, last=NOW, fav=True, fav_at=5, playtime=5400)}
        )
        found = game_play(index(ark, deck_), "psx", EXAMPLE)
        self.assertEqual(
            found["devices"],
            {
                "ark": {
                    "replica": "ark",
                    "plays": 1,
                    "last": NOW - 100,
                    "fav": None,
                    "playtime": 600,
                },
                "deck": {
                    "replica": "wz-esde",
                    "plays": 3,
                    "last": NOW,
                    "fav": True,
                    "playtime": 5400,
                },
            },
        )
        self.assertEqual(found["totals"], {"plays": 4, "last": NOW, "fav": True, "playtime": 6000})

    def test_devices_that_share_a_gamelist_count_once(self) -> None:
        deck_ = Ledger("deck", "wz-esde", {EX: Record(3, last=NOW, playtime=5400)})
        machine = Ledger("machine", "wz-esde", {EX: Record(2, last=NOW - 5, playtime=5000)})
        ark = Ledger("ark", "ark", {EX: Record(1, playtime=60)})
        found = game_play(index(machine, deck_, ark), "psx", EXAMPLE)
        self.assertEqual(found["totals"], {"plays": 4, "last": NOW, "fav": None, "playtime": 5460})
        self.assertEqual(list(found["devices"]), ["ark", "deck", "machine"])

    def test_the_totals_do_not_depend_on_the_order_of_the_ledgers(self) -> None:
        a = Ledger("a", "wz-esde", {EX: Record(3, last=7, playtime=9)})
        b = Ledger("b", "wz-esde", {EX: Record(2, last=8, playtime=10)})
        c = Ledger("c", "c", {EX: Record(1, fav=True, fav_at=3, playtime=1)})
        self.assertEqual(
            game_totals(index(a, b, c), "psx", EXAMPLE), game_totals(index(c, b, a), "psx", EXAMPLE)
        )

    def test_a_last_played_time_from_a_wrong_clock_is_ignored(self) -> None:
        ledger = Ledger("deck", "wz-esde", {EX: Record(1, last=NOW + 10 * 86400)})
        found = game_play(index(ledger), "psx", EXAMPLE)
        self.assertIsNone(found["devices"]["deck"]["last"])
        self.assertIsNone(found["totals"]["last"])
        self.assertEqual(found["totals"]["plays"], 1)

    def test_a_record_that_is_only_a_favourite(self) -> None:
        ledger = Ledger("ark", "ark", {EX: Record(fav=True, fav_at=1)})
        self.assertEqual(
            game_totals(index(ledger), "psx", EXAMPLE),
            {"plays": 0, "last": None, "fav": True, "playtime": 0},
        )

    def test_a_list_entry_gets_the_totals_or_null(self) -> None:
        ledger = Ledger("ark", "ark", {EX: Record(2, last=NOW, playtime=30)})
        entries = [{"id": EXAMPLE.id}, {"id": OTHER.id}, {"id": "0" * 16}]
        attach_play(entries, LIB, index(ledger))
        self.assertEqual(entries[0]["play"], {"plays": 2, "last": NOW, "fav": None, "playtime": 30})
        self.assertIsNone(entries[1]["play"])
        self.assertIsNone(entries[2]["play"])


class RecentTests(unittest.TestCase):
    def games(self, found: dict) -> list[str]:
        return [g["id"] for g in found["games"]]

    def test_newest_first_with_the_device_that_played(self) -> None:
        ark = Ledger("ark", "ark", {OT: Record(1, last=NOW - 50)})
        deck_ = Ledger("deck", "wz-esde", {EX: Record(4, last=NOW - 10, playtime=300)})
        found = recent_response(LIB, index(ark, deck_), none)
        self.assertEqual(self.games(found), [EXAMPLE.id, OTHER.id])
        first = found["games"][0]
        self.assertEqual(
            first,
            {
                "id": EXAMPLE.id,
                "system": "psx",
                "title": "Example Game (Europe)",
                "name": "Example Game (Europe)",
                "cover": False,
                "last": NOW - 10,
                "device": "deck",
                "plays": 4,
                "playtime": 300,
            },
        )
        self.assertEqual(found["games"][1]["device"], "ark")

    def test_the_latest_device_of_a_game_is_the_one_named(self) -> None:
        ark = Ledger("ark", "ark", {EX: Record(1, last=NOW)})
        deck_ = Ledger("deck", "wz-esde", {EX: Record(1, last=NOW - 60)})
        found = recent_response(LIB, index(deck_, ark), none)
        self.assertEqual([(g["device"], g["last"]) for g in found["games"]], [("ark", NOW)])
        self.assertEqual(found["games"][0]["plays"], 2)

    def test_ties_go_by_game_id_and_the_limit_cuts(self) -> None:
        ledger = Ledger("ark", "ark", {EX: Record(1, last=NOW), OT: Record(1, last=NOW)})
        found = recent_response(LIB, index(ledger), none)
        self.assertEqual(self.games(found), sorted([EXAMPLE.id, OTHER.id]))
        self.assertEqual(len(recent_response(LIB, index(ledger), none, 1)["games"]), 1)

    def test_games_never_played_unknown_roms_and_wrong_clocks_are_left_out(self) -> None:
        ledger = Ledger(
            "ark",
            "ark",
            {
                EX: Record(fav=True, fav_at=1),  # a favourite is not a play
                OT: Record(1, last=NOW + 30 * 86400),
                ("psx", "Gone.chd"): Record(9, last=NOW),
            },
        )
        self.assertEqual(recent_response(LIB, index(ledger), none), {"games": []})

    def test_the_name_and_cover_come_from_the_extras(self) -> None:
        entry = artwork.Entry("game", EXAMPLE.gamelist, {"name": "Example Game"})
        extras = SystemExtras(
            {EXAMPLE.gamelist: entry}, {"Example Game (Europe)": {"covers": "a.png"}}
        )
        ledger = Ledger("ark", "ark", {EX: Record(1, last=NOW)})
        game = recent_response(LIB, index(ledger), lambda system: extras)["games"][0]
        self.assertEqual((game["name"], game["cover"]), ("Example Game", True))


class LimitTests(unittest.TestCase):
    def test_default_and_bounds(self) -> None:
        self.assertEqual(parse_limit(None), RECENT_DEFAULT)
        self.assertEqual(parse_limit("1"), 1)
        self.assertEqual(parse_limit(str(RECENT_MAX)), RECENT_MAX)

    def test_anything_else_is_a_bad_request(self) -> None:
        for raw in ("0", str(RECENT_MAX + 1), "-1", "x", "", "1.5", " 3", "٣"):
            with self.subTest(raw=raw), self.assertRaises(ApiError) as caught:
                parse_limit(raw)
            self.assertEqual(caught.exception.code, "bad-request")


NOW_DT = datetime.fromtimestamp(NOW, UTC)


class ServeTests(test_serve.ServeCase):
    def setUp(self) -> None:
        super().setUp()
        self.start(now=lambda: NOW_DT)
        self.folder = self.root / "frontend" / "play-state"
        self.folder.mkdir(parents=True)

    def ledger(self, device: str, replica: str, *rows: dict, name: str | None = None) -> Path:
        lines = [{"v": 1, "device": device, "replica": replica}, *rows]
        path = self.folder / (name or f"{device}.jsonl")
        path.write_text("\n".join(json.dumps(r) for r in lines) + "\n")
        return path

    def test_the_game_in_full_has_its_play_object(self) -> None:
        self.ledger(
            "deck",
            "wz-esde",
            {"system": "psx", "path": EX[1], "plays": 3, "last": NOW - 5, "playtime": 90},
        )
        self.ledger("ark", "ark", {"system": "psx", "path": EX[1], "plays": 1, "fav": True})
        play = self.get(f"/api/v1/games/{EXAMPLE.id}").json["play"]
        self.assertEqual(play["totals"], {"plays": 4, "last": NOW - 5, "fav": True, "playtime": 90})
        self.assertEqual(sorted(play["devices"]), ["ark", "deck"])
        self.assertEqual(play["devices"]["deck"]["playtime"], 90)
        other = self.get(f"/api/v1/games/{OTHER.id}").json["play"]
        self.assertEqual(other["devices"], {})

    def test_the_list_carries_the_totals(self) -> None:
        self.ledger("deck", "wz-esde", {"system": "psx", "path": EX[1], "plays": 2, "playtime": 7})
        games = {g["id"]: g for g in self.get("/api/v1/systems/psx/games").json["games"]}
        self.assertEqual(
            games[EXAMPLE.id]["play"], {"plays": 2, "last": None, "fav": None, "playtime": 7}
        )
        self.assertIsNone(games[OTHER.id]["play"])

    def test_no_ledgers_is_no_statistics(self) -> None:
        self.folder.rmdir()
        self.assertEqual(self.get("/api/v1/recent").json, {"games": []})
        self.assertEqual(self.get(f"/api/v1/games/{EXAMPLE.id}").json["play"]["devices"], {})

    def test_recent(self) -> None:
        self.ledger(
            "deck", "wz-esde", {"system": "psx", "path": EX[1], "plays": 1, "last": NOW - 9}
        )
        self.ledger("ark", "ark", {"system": "psx", "path": OT[1], "plays": 1, "last": NOW - 3})
        found = self.get("/api/v1/recent").json["games"]
        self.assertEqual(
            [(g["id"], g["device"]) for g in found], [(OTHER.id, "ark"), (EXAMPLE.id, "deck")]
        )
        self.assertEqual(len(self.get("/api/v1/recent?limit=1").json["games"]), 1)

    def test_recent_validates_limit_and_needs_auth(self) -> None:
        for query in ("limit=0", "limit=101", "limit=x"):
            reply = self.get(f"/api/v1/recent?{query}")
            self.assertEqual((reply.status, self.error(reply)["code"]), (400, "bad-request"), query)
        self.assertEqual(self.get("/api/v1/recent", auth=False).status, 401)
        self.assertEqual(self.call("POST", "/api/v1/recent", {}).status, 404)

    def test_broken_conflict_and_foreign_files_are_left_out(self) -> None:
        self.ledger("deck", "wz-esde", {"system": "psx", "path": EX[1], "plays": 1, "last": NOW})
        (self.folder / "ark.jsonl").write_text("not json\n")
        self.ledger(
            "machine", "wz-esde", {"system": "psx", "path": EX[1], "plays": 9}, name="other.jsonl"
        )
        self.ledger(
            "deck",
            "wz-esde",
            {"system": "psx", "path": EX[1], "plays": 9},
            name="deck.sync-conflict-1.jsonl",
        )
        play = self.get(f"/api/v1/games/{EXAMPLE.id}").json["play"]
        self.assertEqual(list(play["devices"]), ["deck"])
        self.assertEqual(play["totals"]["plays"], 1)

    def test_a_ledger_is_read_again_when_it_changes(self) -> None:
        path = self.ledger("deck", "wz-esde", {"system": "psx", "path": EX[1], "plays": 1})
        url = f"/api/v1/games/{EXAMPLE.id}"
        self.assertEqual(self.get(url).json["play"]["totals"]["plays"], 1)
        self.ledger("deck", "wz-esde", {"system": "psx", "path": EX[1], "plays": 6, "last": NOW})
        self.assertEqual(self.get(url).json["play"]["totals"]["plays"], 6)
        path.unlink()
        self.assertEqual(self.get(url).json["play"]["totals"]["plays"], 0)


if __name__ == "__main__":
    unittest.main()
