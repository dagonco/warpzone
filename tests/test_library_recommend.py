"""The recommendation shelves of the library API: candidates from the ledgers, the RA cache,
the gamelists and the consoles, and the `GET /api/v1/recommendations` answer."""

import json
import unittest
from dataclasses import replace
from datetime import UTC, datetime
from typing import Any

import test_serve
from library_support import GIB, config, games, library, managed
from warpzone import artwork, catalog, ra, ra_data, recommend
from warpzone.config import RaSettings
from warpzone.library_api import (
    DeviceView,
    Library,
    PlayIndex,
    SystemExtras,
    _availability,
    _epoch,
    candidates,
    recommendations_response,
    view_of,
)
from warpzone.playstate import Ledger, Record

NOW = 1_800_000_000
DAY = 86400
USER = "ExampleUser"
CFG = replace(config(), ra=RaSettings(USER))


def by_title(system: str, title: str) -> catalog.Game:
    return next(g for s, g in games() if s == system and g.title == title)


EXAMPLE = by_title("psx", "Example Game (Europe)")
OTHER = by_title("psx", "Other Game")
MGS_PSX = by_title("psx", "Metal Gear Solid (USA)")
MGS_GBC = by_title("gbc", "Metal Gear Solid (USA)")
TINY = by_title("gba", "Tiny")
PS4 = by_title("ps4", "Example PS4")
BAD = by_title("psx", "Bad Name")

META = {
    "psx": {
        EXAMPLE: {"genre": "Platform", "developer": "Dev", "releasedate": "19950101T000000"},
        OTHER: {"genre": "Platform, Action", "developer": "Dev", "releasedate": "19960601T000000"},
        MGS_PSX: {"genre": "Action", "developer": "dev"},
        BAD: {"genre": "Platform"},
    },
    "gbc": {MGS_GBC: {"genre": "Action", "developer": "Dev"}},
    "gba": {TINY: {"genre": "Platform"}},
    "ps4": {PS4: {"genre": "Platform"}},
}


def extras(system: str) -> SystemExtras:
    entries = {
        g.gamelist: artwork.Entry("game", g.gamelist, {"name": f"{g.title}!", **fields})
        for g, fields in META.get(system, {}).items()
    }
    return SystemExtras(entries, {"Tiny": {"covers": "t.png"}})


def no_extras(system: str) -> SystemExtras:
    return SystemExtras()


def ledgers() -> tuple[Ledger, ...]:
    return (
        Ledger(
            "deck",
            "wz-esde",
            {
                ("psx", "Example Game (Europe).chd"): Record(3, last=NOW - 40 * DAY, playtime=7200),
                ("psx", "Other Game.chd"): Record(1, last=NOW - 2 * DAY, playtime=100),
            },
        ),
    )


def views() -> dict[str, DeviceView]:
    deck = managed(["gba"], includes=[EXAMPLE.primary, MGS_PSX.primary])
    return {"deck": view_of(deck.encode()), "machine": view_of(managed().encode())}


def disks(deck: int | None = 100 * GIB, machine: int | None = None) -> dict[str, Any]:
    return {"deck": {"free": deck}, "machine": {"free": machine}}


def play(*found: Ledger) -> PlayIndex:
    return PlayIndex(found, NOW + DAY)


def answer(
    lib: Library | None = None,
    index: PlayIndex | None = None,
    extra: Any = extras,
) -> dict[str, Any]:
    return recommendations_response(
        lib or library(),
        CFG,
        views(),
        play(*ledgers()) if index is None else index,
        extra,
        disks(),
        NOW,
    )


def with_ra(game: catalog.Game, hash_: str, ra_id: int, total: int, line: ra.UserGame) -> Library:
    """A library of one psx game linked to RA game `ra_id`, with the user's progress line."""
    linked = replace(game, ra={"hash": hash_, "of": game.primary, "size": 1, "mtime": 1})
    lists = {
        12: catalog.RaList(12, "2026-11-08T00:00:00Z", {hash_: ra.RaGame("T", total, ra_id, 50)})
    }
    user = ra_data.UserSummary(USER.upper(), "2026-11-09T09:00:00Z", {ra_id: line})
    return replace(library([("psx", linked)]), ra_lists=lists, ra_cache=ra_data.RaData(user=user))


class AvailabilityTests(unittest.TestCase):
    def tier(self, game: catalog.Game, system: str, free: dict[str, Any]) -> int:
        return _availability(CFG, views(), free, system, game)

    def test_on_a_console_by_game_or_by_system(self) -> None:
        self.assertEqual(self.tier(EXAMPLE, "psx", disks(0)), recommend.ON_CONSOLE)
        self.assertEqual(self.tier(TINY, "gba", disks(0)), recommend.ON_CONSOLE)

    def test_fits_when_a_console_that_takes_it_has_room(self) -> None:
        self.assertEqual(self.tier(OTHER, "psx", disks()), recommend.FITS)
        self.assertEqual(self.tier(OTHER, "psx", disks(GIB)), recommend.FITS)
        self.assertEqual(self.tier(OTHER, "psx", disks(GIB - 1)), recommend.ELSEWHERE)
        self.assertEqual(self.tier(OTHER, "psx", disks(None, GIB)), recommend.FITS)

    def test_no_room_without_a_report_for_an_unswitchable_game_or_an_excluded_system(self) -> None:
        self.assertEqual(self.tier(OTHER, "psx", disks(None, None)), recommend.ELSEWHERE)
        self.assertEqual(self.tier(OTHER, "psx", {}), recommend.ELSEWHERE)
        self.assertEqual(self.tier(BAD, "psx", disks()), recommend.ELSEWHERE)
        # The machine excludes ps4 and the deck has too little room.
        self.assertEqual(self.tier(PS4, "ps4", disks(GIB, 100 * GIB)), recommend.ELSEWHERE)
        self.assertEqual(self.tier(PS4, "ps4", disks(100 * GIB, 0)), recommend.FITS)

    def test_a_whitelist_nobody_can_read_is_not_room(self) -> None:
        unreadable = {"deck": view_of(b"\xff\xfe"), "machine": view_of(b"\xff\xfe")}
        self.assertEqual(_availability(CFG, unreadable, disks(), "psx", OTHER), recommend.ELSEWHERE)


class DateTests(unittest.TestCase):
    def test_ra_dates_are_read_to_the_day(self) -> None:
        day = int(datetime(2026, 10, 1, tzinfo=UTC).timestamp())
        self.assertEqual(_epoch("2026-10-01"), day)
        self.assertEqual(_epoch("2026-10-01 12:30:00"), day)
        self.assertEqual(_epoch("2026-10-01T12:30:00Z"), day)
        for bad in ("", "soon", "2026-13-01"):
            self.assertIsNone(_epoch(bad), bad)


class CandidateTests(unittest.TestCase):
    def build(
        self, lib: Library | None = None, index: PlayIndex | None = None, extra: Any = extras
    ) -> dict[str, recommend.Candidate]:
        found = candidates(
            lib or library(),
            CFG,
            views(),
            play(*ledgers()) if index is None else index,
            extra,
            disks(),
        )
        return {c.id: c for c in found}

    def test_what_a_candidate_carries(self) -> None:
        found = self.build()
        example = found[EXAMPLE.id]
        self.assertEqual(
            (example.system, example.key, example.genres, example.developer, example.year),
            ("psx", EXAMPLE.key, ("Platform",), "Dev", 1995),
        )
        self.assertEqual(
            (example.plays, example.playtime, example.last_played, example.tier),
            (3, 7200, NOW - 40 * DAY, recommend.ON_CONSOLE),
        )
        self.assertEqual(found[OTHER.id].genres, ("Platform", "Action"))
        self.assertEqual(found[OTHER.id].tier, recommend.FITS)

    def test_a_game_with_neither_metadata_nor_activity_is_left_out(self) -> None:
        # Only the games with a genre or a developer, or with a play record, are candidates.
        self.assertEqual(
            sorted(self.build()),
            sorted(g.id for g in (EXAMPLE, OTHER, MGS_PSX, MGS_GBC, TINY, PS4, BAD)),
        )
        lib = library([("psx", OTHER)])
        self.assertEqual(self.build(lib, play(), no_extras), {})
        self.assertEqual(list(self.build(lib, play(*ledgers()), no_extras)), [OTHER.id])

    def test_the_ra_progress_of_the_configured_user(self) -> None:
        line = ra.UserGame(77, awarded=2, awarded_hardcore=4, last="2026-10-01")
        lib = with_ra(EXAMPLE, "a" * 32, 77, 10, line)
        found = self.build(lib, play())[EXAMPLE.id]
        self.assertEqual((found.ra_unlocked, found.ra_total, found.ra_award), (4, 10, ""))
        self.assertEqual(found.ra_last, _epoch("2026-10-01"))

    def test_an_award_is_carried_and_a_game_without_a_line_has_only_its_total(self) -> None:
        line = ra.UserGame(77, awarded=3, last="2026-10-01", award="beaten-softcore")
        lib = with_ra(EXAMPLE, "a" * 32, 77, 10, line)
        self.assertEqual(self.build(lib, play())[EXAMPLE.id].ra_award, "beaten-softcore")
        none = replace(lib, ra_cache=ra_data.RaData(user=replace(lib.ra_cache.user, games={})))
        found = self.build(none, play())[EXAMPLE.id]
        self.assertEqual((found.ra_unlocked, found.ra_total), (0, 10))

    def test_progress_of_another_user_or_without_a_configured_user_is_ignored(self) -> None:
        line = ra.UserGame(77, awarded=2, last="2026-10-01")
        lib = with_ra(EXAMPLE, "a" * 32, 77, 10, line)
        user = lib.ra_cache.user
        assert user is not None
        other = replace(lib, ra_cache=ra_data.RaData(user=replace(user, user="Someone")))
        self.assertEqual(self.build(other, play())[EXAMPLE.id].ra_unlocked, 0)
        no_user = candidates(lib, config(), views(), play(), extras, disks())
        self.assertEqual(no_user[0].ra_unlocked, 0)


class ResponseTests(unittest.TestCase):
    def test_both_shelves(self) -> None:
        found = answer()
        (left,) = found["unfinished"]
        self.assertEqual(
            left,
            {
                "id": EXAMPLE.id,
                "system": "psx",
                "title": "Example Game (Europe)",
                "name": "Example Game (Europe)!",
                "cover": False,
                "last": NOW - 40 * DAY,
                "plays": 3,
                "playtime": 7200,
                "ra": None,
            },
        )
        similar = found["similar"]
        self.assertEqual(similar["to"]["id"], OTHER.id)
        # On a console first (Metal Gear scores higher than Tiny), then what fits (the PS4
        # game), then the rest. The played game is not there, nor the gbc copy of a title
        # already listed.
        self.assertEqual([g["id"] for g in similar["games"]], [MGS_PSX.id, TINY.id, PS4.id, BAD.id])
        first, tiny = similar["games"][:2]
        self.assertEqual(
            (first["genres"], first["developer"], first["year"]), (["Action"], "dev", None)
        )
        self.assertEqual(
            (tiny["genres"], tiny["developer"], tiny["cover"]), (["Platform"], None, True)
        )

    def test_the_answer_is_stable(self) -> None:
        self.assertEqual(json.dumps(answer()), json.dumps(answer()))

    def test_empty_shelves(self) -> None:
        self.assertEqual(answer(index=play(), extra=no_extras), {"unfinished": [], "similar": None})
        # A seed with nothing similar is no shelf.
        self.assertIsNone(answer(library([("psx", OTHER)]))["similar"])

    def test_ra_progress_is_in_the_unfinished_entry(self) -> None:
        line = ra.UserGame(8, awarded=5, last="2026-09-01")
        (left,) = answer(with_ra(OTHER, "b" * 32, 8, 40, line), play())["unfinished"]
        self.assertEqual(left["ra"], {"unlocked": 5, "total": 40})
        self.assertEqual(left["last"], _epoch("2026-09-01"))

    def test_a_beaten_game_is_not_unfinished(self) -> None:
        line = ra.UserGame(8, awarded=5, last="2026-09-01", award="beaten-hardcore")
        self.assertEqual(answer(with_ra(OTHER, "b" * 32, 8, 40, line), play())["unfinished"], [])


class ServeTests(test_serve.ServeCase):
    def setUp(self) -> None:
        super().setUp()
        self.start(now=lambda: datetime.fromtimestamp(NOW, UTC))
        self.folder = self.root / "frontend" / "play-state"
        self.folder.mkdir(parents=True)
        rows = [
            {"v": 1, "device": "deck", "replica": "wz-esde"},
            {
                "system": "psx",
                "path": "Example Game (Europe).chd",
                "plays": 2,
                "last": NOW - 30 * DAY,
                "playtime": 4000,
            },
        ]
        (self.folder / "deck.jsonl").write_text("\n".join(json.dumps(r) for r in rows) + "\n")

    def test_the_endpoint(self) -> None:
        reply = self.get("/api/v1/recommendations")
        self.assertEqual(reply.status, 200)
        found = reply.json
        self.assertEqual([g["id"] for g in found["unfinished"]], [EXAMPLE.id])
        # Nothing else in the gamelist looks like the game that was played.
        self.assertIsNone(found["similar"])

    def test_needs_auth(self) -> None:
        self.assertEqual(self.get("/api/v1/recommendations", auth=False).status, 401)

    def test_no_history_is_two_empty_shelves(self) -> None:
        (self.folder / "deck.jsonl").unlink()
        self.assertEqual(
            self.get("/api/v1/recommendations").json, {"unfinished": [], "similar": None}
        )


if __name__ == "__main__":
    unittest.main()
