"""RetroAchievements achievements and the user's progress: the parsers of the web API's
answers, the cache files, the pure merge into the API's answers, the pass that downloads them
and the routes that serve them. Nothing here reaches the real API: every call goes through a
fake fetch.
"""

import json
import os
import tempfile
import unittest
import urllib.error
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest import mock

from library_support import games, write_catalog
from support import config_data
from test_catalog_store import StoreCase
from test_cli import run
from test_serve import EXAMPLE, PNG, TINY, ServeCase
from warpzone import catalog, catalog_store, ra, ra_data, ra_store
from warpzone.catalog_store import CatalogError
from warpzone.config import ConfigError, RaSettings, load_ra_settings, parse_config

SECRET = "achievements-secret-key"  # gitleaks:allow (test fixture, not a real key)
USER = "ExampleUser"
HASH = "a" * 32
NOW = datetime(2026, 11, 9, 10, 0, 0, tzinfo=UTC)
PNG_OK = b"\x89PNG\r\n\x1a\n" + b"body"


def extended(game_id: int = 77, *, camel: bool = False) -> bytes:
    def achievement(ident: int, order: int, kind: str | None, awarded: int, hard: int) -> dict:
        item = {
            "ID": ident,
            "Title": f"Achievement {ident}",
            "Description": "Do the thing.",
            "Points": 10 * ident,
            "TrueRatio": 15 * ident,
            "NumAwarded": awarded,
            "NumAwardedHardcore": hard,
            "BadgeName": f"{ident}00",
            "DisplayOrder": order,
        }
        if kind:
            item["type"] = kind
        return item

    doc = {
        "ID": game_id,
        "Title": "Alpha Quest",
        "NumDistinctPlayers": 1000,
        "NumDistinctPlayersHardcore": 400,
        "NumAchievements": 3,
        "Achievements": {
            "2": achievement(2, 2, None, 100, 40),
            "1": achievement(1, 1, "progression", 900, 300),
            "3": achievement(3, 3, "win_condition", 10, 0),
        },
    }
    if camel:

        def lower(key: str) -> str:
            return "id" if key == "ID" else key[0].lower() + key[1:]

        doc = {lower(k): v for k, v in doc.items()}
        doc["achievements"] = {
            i: {lower(k): v for k, v in a.items()} for i, a in doc["achievements"].items()
        }
    return json.dumps(doc).encode()


def completion(*lines: dict, total: int | None = None) -> bytes:
    return json.dumps(
        {"Count": len(lines), "Total": total or len(lines), "Results": lines}
    ).encode()


def line(game_id: int, soft: int = 0, hard: int = 0, last: str = "", award: str = "") -> dict:
    return {
        "GameID": game_id,
        "ConsoleID": 12,
        "MaxPossible": 3,
        "NumAwarded": soft,
        "NumAwardedHardcore": hard,
        "MostRecentAwardedDate": last,
        "HighestAwardKind": award,
        "HighestAwardDate": "2026-10-01T20:00:00+00:00" if award else "",
    }


def user_progress(*, camel: bool = False) -> bytes:
    return json.dumps(
        {
            "ID": 77,
            "Achievements": {
                "1": {"ID": 1, "DateEarned": "2026-09-30 20:00:00", "DateEarnedHardcore": None},
                "2": {"ID": 2},
                "3": {
                    "ID": 3,
                    "DateEarned": "2026-10-01 20:00:00",
                    "DateEarnedHardcore": "2026-10-01 20:00:00",
                },
            },
        }
    ).encode()


class ParseTests(unittest.TestCase):
    def test_game_extended_in_display_order_with_both_casings(self) -> None:
        for camel in (False, True):
            detail = ra.parse_game_extended(extended(camel=camel))
            self.assertEqual((detail.id, detail.title, detail.players), (77, "Alpha Quest", 1000))
            self.assertEqual(detail.players_hardcore, 400)
            self.assertEqual([a.id for a in detail.achievements], [1, 2, 3])
            first = detail.achievements[0]
            self.assertEqual((first.points, first.true_points, first.badge), (10, 15, "100"))
            self.assertEqual((first.awarded, first.awarded_hardcore), (900, 300))
            self.assertEqual(
                [a.kind for a in detail.achievements], ["progression", None, "win_condition"]
            )

    def test_an_achievement_with_a_bad_badge_name_or_no_id_is_dropped(self) -> None:
        doc = json.loads(extended())
        doc["Achievements"]["1"]["BadgeName"] = "../../etc"
        doc["Achievements"]["2"]["ID"] = 0
        detail = ra.parse_game_extended(json.dumps(doc).encode())
        self.assertEqual([a.id for a in detail.achievements], [3])

    def test_a_game_with_no_achievements_has_a_list_not_an_object(self) -> None:
        detail = ra.parse_game_extended(b'{"ID": 5, "Title": "Empty", "Achievements": []}')
        self.assertEqual(detail.achievements, ())

    def test_an_answer_that_is_not_a_game_is_an_error(self) -> None:
        for data in (b"[]", b'{"message": "not found"}', b"not json"):
            with self.assertRaises(ValueError):
                ra.parse_game_extended(data)

    def test_completion_progress(self) -> None:
        data = completion(line(77, 2, 1, "2026-10-01", "beaten-hardcore"), line(9), total=40)
        total, games_ = ra.parse_completion(data)
        self.assertEqual(total, 40)
        self.assertEqual(games_[0].award, "beaten-hardcore")
        self.assertEqual((games_[0].awarded, games_[0].awarded_hardcore), (2, 1))
        self.assertEqual(games_[1].game_id, 9)

    def test_user_unlocks_keep_only_what_the_user_has(self) -> None:
        found = ra.parse_user_unlocks(user_progress())
        self.assertEqual(sorted(found), [1, 3])
        self.assertEqual(found[1], ra.Unlock("2026-09-30 20:00:00", None))
        self.assertEqual(found[3].hard, "2026-10-01 20:00:00")


class FetchTests(unittest.TestCase):
    def test_the_calls_carry_the_key_and_the_user(self) -> None:
        urls: list[str] = []

        def fetch(url: str) -> bytes:
            urls.append(url)
            return extended() if "GetGameExtended" in url else user_progress()

        ra.fetch_game_detail(77, SECRET, fetch)
        ra.fetch_user_unlocks(77, USER, SECRET, fetch)
        self.assertEqual(urls[0], f"{ra.GAME_EXTENDED_URL}?i=77&y={SECRET}")
        self.assertEqual(urls[1], f"{ra.USER_GAME_URL}?g=77&u={USER}&y={SECRET}")

    def test_a_user_name_is_encoded_in_the_url(self) -> None:
        urls: list[str] = []
        ra.fetch_user_unlocks(1, "a b&c", SECRET, lambda url: urls.append(url) or b"{}")
        self.assertIn("u=a+b%26c", urls[0])

    def test_failures_name_their_kind_and_never_the_key(self) -> None:
        def forbidden(url: str) -> bytes:
            raise urllib.error.HTTPError(url, 429, "slow down", None, None)  # type: ignore[arg-type]

        def unreachable(url: str) -> bytes:
            raise urllib.error.URLError(f"cannot reach {url}")

        for fetch, text in (
            (forbidden, "HTTP 429"),
            (unreachable, "URLError"),
            (lambda u: b"x", "JSONDecodeError"),
        ):
            with self.assertRaises(ra.FetchError) as caught:
                ra.fetch_game_detail(77, SECRET, fetch)
            self.assertEqual(str(caught.exception), text)
            self.assertNotIn(SECRET, repr(caught.exception))

    def test_the_completion_progress_is_paged_with_a_pause(self) -> None:
        pages = {
            0: completion(line(1), line(2), total=5),
            2: completion(line(3), line(4), total=5),
            4: completion(line(5), total=5),
        }
        urls: list[str] = []
        pauses: list[float] = []

        def fetch(url: str) -> bytes:
            urls.append(url)
            return pages[int(url.split("&o=")[1].split("&")[0])]

        with mock.patch.object(ra, "PAGE_SIZE", 2):
            found = ra.fetch_completion(USER, SECRET, fetch, sleep=pauses.append)
        self.assertEqual(sorted(found), [1, 2, 3, 4, 5])
        self.assertEqual(len(urls), 3)
        self.assertEqual(pauses, [ra.CALL_DELAY, ra.CALL_DELAY])

    def test_an_empty_page_ends_the_paging(self) -> None:
        calls = []
        found = ra.fetch_completion(
            USER, SECRET, lambda url: calls.append(url) or completion(total=9)
        )
        self.assertEqual((found, len(calls)), ({}, 1))


class BadgeTests(unittest.TestCase):
    def test_urls_are_https_on_the_media_host_and_names_are_checked(self) -> None:
        self.assertEqual(
            ra.badge_url("250336"), "https://media.retroachievements.org/Badge/250336.png"
        )
        self.assertEqual(
            ra.badge_url("250336", locked=True),
            "https://media.retroachievements.org/Badge/250336_lock.png",
        )
        for bad in ("", "../x", "a/b", "a.png", "x" * 21, "a b"):
            self.assertIsNone(ra.badge_url(bad), bad)

    def test_only_the_media_host_over_https_is_allowed(self) -> None:
        self.assertTrue(ra.is_media_url("https://media.retroachievements.org/Badge/1.png"))
        for url in (
            "http://media.retroachievements.org/Badge/1.png",
            "https://retroachievements.org/Badge/1.png",
            "https://media.retroachievements.org.evil.example/Badge/1.png",
            "https://user@media.retroachievements.org/Badge/1.png",
            "https://media.retroachievements.org:8443/Badge/1.png",
            "file:///etc/passwd",
        ):
            self.assertFalse(ra.is_media_url(url), url)
        with self.assertRaises(ValueError):
            ra.http_fetch_binary("http://media.retroachievements.org/Badge/1.png", 10)

    def test_a_download_must_be_a_small_png(self) -> None:
        ok = ra.check_badge
        self.assertTrue(ok(PNG_OK, "image/png"))
        self.assertTrue(ok(PNG_OK, "Image/PNG; charset=binary"))
        self.assertFalse(ok(PNG_OK, "text/html"))
        self.assertFalse(ok(b"<html>", "image/png"))
        self.assertFalse(ok(b"", "image/png"))
        self.assertFalse(ok(PNG_OK + b"x" * ra.MAX_BADGE_BYTES, "image/png"))

    def test_fetch_badge_asks_for_the_right_url_and_names_failures_by_kind(self) -> None:
        seen: list[tuple[str, int]] = []

        def good(url: str, limit: int) -> tuple[bytes, str]:
            seen.append((url, limit))
            return PNG_OK, "image/png"

        self.assertEqual(ra.fetch_badge("12", locked=True, fetch=good), PNG_OK)
        self.assertEqual(
            seen, [("https://media.retroachievements.org/Badge/12_lock.png", ra.MAX_BADGE_BYTES)]
        )

        def missing(url: str, limit: int) -> tuple[bytes, str]:
            raise urllib.error.HTTPError(url, 404, "no", None, None)  # type: ignore[arg-type]

        def html(url: str, limit: int) -> tuple[bytes, str]:
            return PNG_OK, "text/html"

        def big(url: str, limit: int) -> tuple[bytes, str]:
            raise ValueError("too large")

        for fetch, text in ((missing, "HTTP 404"), (html, "BadImage"), (big, "ValueError")):
            with self.assertRaises(ra.FetchError) as caught:
                ra.fetch_badge("12", locked=False, fetch=fetch)
            self.assertEqual(str(caught.exception), text)
        with self.assertRaises(ra.FetchError):
            ra.fetch_badge("../x", locked=False, fetch=good)


def detail(game_id: int = 77) -> ra.RaGameDetail:
    return ra.parse_game_extended(extended(game_id))


def cached_data(*, unlocks: bool = True, user: str = USER) -> ra_data.RaData:
    seen = ra.UserGame(
        77, 12, 3, 2, 1, "2026-10-01", "beaten-hardcore", "2026-10-01T20:00:00+00:00"
    )
    return ra_data.RaData(
        {77: ra_data.CachedGame("2026-11-09T09:00:00Z", detail())},
        ra_data.UserSummary(user, "2026-11-09T09:00:00Z", {77: seen, 8: ra.UserGame(8, awarded=1)}),
        {
            77: ra_data.CachedUnlocks(
                user, 77, "2026-11-09T09:00:00Z", seen, ra.parse_user_unlocks(user_progress())
            )
        }
        if unlocks
        else {},
    )


class DataTests(unittest.TestCase):
    def test_cache_files_round_trip_and_are_stable(self) -> None:
        one = detail()
        data = ra_data.encode_game(one, "2026-11-09T09:00:00Z")
        self.assertEqual(data, ra_data.encode_game(one, "2026-11-09T09:00:00Z"))
        self.assertEqual(ra_data.decode_game(data), ra_data.CachedGame("2026-11-09T09:00:00Z", one))
        games_ = {77: ra.UserGame(77, 12, 3, 2, 1, "d", "mastered", "e")}
        self.assertEqual(
            ra_data.decode_user(ra_data.encode_user(USER, "t", games_)),
            ra_data.UserSummary(USER, "t", games_),
        )
        unl = {1: ra.Unlock("a", None), 3: ra.Unlock("b", "c")}
        cached = ra_data.decode_unlocks(ra_data.encode_unlocks(USER, 77, "t", games_[77], unl))
        self.assertEqual(cached, ra_data.CachedUnlocks(USER, 77, "t", games_[77], unl))

    def test_files_of_another_format_or_shape_are_not_read(self) -> None:
        for decode in (ra_data.decode_game, ra_data.decode_user, ra_data.decode_unlocks):
            for data in (b"", b"[]", b'{"format": 2}', b'{"format": 1}', b"\xff"):
                self.assertIsNone(decode(data), (decode.__name__, data))

    def test_freshness_counts_a_bad_or_future_time_as_stale(self) -> None:
        day = timedelta(days=1)
        self.assertTrue(ra_data.fresh("2026-11-08T10:00:01Z", NOW, day))
        self.assertFalse(ra_data.fresh("2026-11-08T10:00:00Z", NOW, day))
        self.assertFalse(ra_data.fresh("2026-11-10T10:00:00Z", NOW, day))
        self.assertFalse(ra_data.fresh("yesterday", NOW, day))

    def test_rarity_is_a_share_of_the_players_with_one_decimal(self) -> None:
        self.assertEqual(ra_data.percent(900, 1000), 90.0)
        self.assertEqual(ra_data.percent(1, 3), 33.3)
        self.assertEqual(ra_data.percent(5, 0), 0.0)
        self.assertEqual(ra_data.percent(0, 10), 0.0)
        self.assertEqual(ra_data.percent(20, 10), 100.0)

    def test_unlock_dates_are_current_only_for_the_same_user_and_line(self) -> None:
        line_ = ra.UserGame(77, awarded=2, awarded_hardcore=1, last="2026-10-01")
        cached = ra_data.CachedUnlocks(USER, 77, "t", line_, {})
        self.assertTrue(ra_data.unlocks_current(cached, USER.lower(), line_))
        self.assertFalse(ra_data.unlocks_current(cached, "Other", line_))
        self.assertFalse(ra_data.unlocks_current(cached, USER, replace(line_, awarded=3)))
        self.assertFalse(ra_data.unlocks_current(cached, USER, replace(line_, last="2026-11-01")))
        self.assertFalse(ra_data.unlocks_current(None, USER, line_))

    def test_a_game_view_merges_achievements_rarity_unlocks_and_the_award(self) -> None:
        view = ra_data.game_view(ra.RaGame("Alpha Quest", 3, 77, 60), cached_data(), USER)
        self.assertTrue(view["available"])
        self.assertEqual((view["players"], view["players_hardcore"]), (1000, 400))
        self.assertEqual(
            view["totals"],
            {
                "count": 3,
                "points": 60,
                "unlocked": 2,
                "unlocked_hardcore": 1,
                "points_unlocked": 40,
                "points_unlocked_hardcore": 30,
            },
        )
        self.assertEqual(
            view["progress"]["award"],
            {"kind": "beaten-hardcore", "date": "2026-10-01T20:00:00+00:00"},
        )
        self.assertTrue(view["progress"]["dates"])
        first, second, third = view["list"]
        self.assertEqual(
            first["rarity"],
            {"players": 900, "hardcore_players": 300, "percent": 90.0, "hardcore_percent": 75.0},
        )
        self.assertEqual(first["unlocked"], {"softcore": "2026-09-30 20:00:00", "hardcore": None})
        self.assertIsNone(second["unlocked"])
        self.assertEqual(third["unlocked"]["hardcore"], "2026-10-01 20:00:00")
        self.assertEqual(
            (first["type"], first["badge"], first["true_points"]), ("progression", "100", 15)
        )

    def test_without_a_user_there_is_no_progress_and_no_unlock(self) -> None:
        for user in (None, "Somebody"):
            view = ra_data.game_view(ra.RaGame("Alpha Quest", 3, 77, 60), cached_data(), user)
            self.assertIsNone(view["progress"])
            self.assertEqual(view["totals"]["unlocked"], None)
            self.assertTrue(all(a["unlocked"] is None for a in view["list"]))
            self.assertEqual(len(view["list"]), 3)

    def test_without_unlock_dates_the_counts_of_the_summary_line_stand_in(self) -> None:
        view = ra_data.game_view(
            ra.RaGame("Alpha Quest", 3, 77, 60), cached_data(unlocks=False), USER
        )
        self.assertFalse(view["progress"]["dates"])
        self.assertEqual(view["totals"]["unlocked"], 2)
        self.assertIsNone(view["totals"]["points_unlocked"])
        self.assertTrue(all(a["unlocked"] is None for a in view["list"]))

    def test_a_game_whose_achievements_are_not_downloaded_yet(self) -> None:
        view = ra_data.game_view(ra.RaGame("Alpha Quest", 3, 78, 60), ra_data.RaData(), None)
        self.assertFalse(view["available"])
        self.assertEqual(
            (view["list"], view["totals"]["count"], view["totals"]["points"]), ([], 3, 60)
        )
        self.assertIsNone(view["fetched"])

    def test_the_summary_counts_the_library_per_system_and_in_all(self) -> None:
        systems, lists = library_with_ra()
        summary = ra_data.summary_view(systems, lists, cached_data(), USER)
        self.assertEqual((summary["user"], summary["fetched"]), (USER, "2026-11-09T09:00:00Z"))
        psx = next(s for s in summary["systems"] if s["system"] == "psx")
        self.assertEqual(
            psx,
            {
                "system": "psx",
                "games": 2,
                "achievements": 8,
                "points": 100,
                "played": 2,
                "beaten": 1,
                "mastered": 0,
                "unlocked": 3,
                "unlocked_hardcore": 1,
                "points_unlocked": 40,
                "points_unlocked_hardcore": 30,
                "points_complete": False,
            },
        )  # game 8 is played but its dates are not downloaded
        self.assertEqual([s["system"] for s in summary["systems"]], ["psx"])
        self.assertEqual(summary["total"]["games"], 2)
        self.assertEqual(summary["total"]["points_unlocked"], 40)

    def test_a_mastery_is_not_also_a_beaten_game(self) -> None:
        systems, lists = library_with_ra()
        data = cached_data()
        mastered = ra.UserGame(77, awarded=3, awarded_hardcore=3, award="mastered")
        data = replace(data, user=ra_data.UserSummary(USER, "t", {77: mastered}), unlocks={})
        total = ra_data.summary_view(systems, lists, data, USER)["total"]
        self.assertEqual((total["beaten"], total["mastered"], total["played"]), (0, 1, 1))

    def test_without_a_user_the_possible_totals_stay_and_the_rest_is_null(self) -> None:
        systems, lists = library_with_ra()
        summary = ra_data.summary_view(systems, lists, ra_data.RaData(), None)
        self.assertIsNone(summary["user"])
        self.assertIsNone(summary["fetched"])
        total = summary["total"]
        self.assertEqual((total["games"], total["achievements"], total["points"]), (2, 8, 100))
        for name in (
            "played",
            "beaten",
            "mastered",
            "unlocked",
            "points_unlocked",
            "points_complete",
        ):
            self.assertIsNone(total[name], name)


def library_with_ra() -> tuple[dict, dict]:
    """Two psx games linked to RA games 77 (3 achievements, 60 points) and 8 (5, 40)."""
    items = []
    for system, g in games():
        if system == "psx" and g.title == "Example Game (Europe)":
            g = replace(g, ra={"hash": HASH, "of": g.primary, "size": 1, "mtime": 1})
        elif system == "psx" and g.title == "Other Game":
            g = replace(g, ra={"hash": "b" * 32, "of": g.primary, "size": 1, "mtime": 1})
        items.append((system, g))
    systems: dict[str, dict[str, catalog.Game]] = {}
    for system, g in items:
        systems.setdefault(system, {})[g.id] = g
    lists = {
        12: catalog.RaList(
            12,
            "2026-11-08T00:00:00Z",
            {HASH: ra.RaGame("Alpha Quest", 3, 77, 60), "b" * 32: ra.RaGame("Beta", 5, 8, 40)},
        )
    }
    return systems, lists


class ConfigTests(unittest.TestCase):
    def parse(self, ra_table: object) -> object:
        data = config_data()
        data["ra"] = ra_table
        return parse_config(data)

    def test_the_user_is_optional(self) -> None:
        self.assertEqual(parse_config(config_data()).ra, RaSettings())
        self.assertEqual(self.parse({}).ra, RaSettings())  # type: ignore[attr-defined]

    def test_a_valid_user_is_kept_trimmed(self) -> None:
        self.assertEqual(self.parse({"user": " Example_User-1.x "}).ra.user, "Example_User-1.x")  # type: ignore[attr-defined]

    def test_a_bad_user_or_table_or_key_is_a_config_error(self) -> None:
        for table in (
            {"user": ""},
            {"user": 5},
            {"user": "has space"},
            {"user": "x" * 41},
            {"user": "a/b"},
            {"name": "x"},
            "text",
        ):
            with self.assertRaises(ConfigError, msg=table):
                self.parse(table)

    def test_the_ra_table_alone_is_readable_without_a_hub(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.toml"
            self.assertEqual(load_ra_settings(path), RaSettings())
            path.write_text(f'[ra]\nuser = "{USER}"\n')
            self.assertEqual(load_ra_settings(path).user, USER)
            path.write_text('[ra]\nuser = ""\n')
            with self.assertRaises(ConfigError):
                load_ra_settings(path)


class PassCase(StoreCase):
    """A hub root with one psx game linked to RA game 77 (and one to game 8) and a fake RA."""

    def setUp(self) -> None:
        super().setUp()
        systems, lists = library_with_ra()
        (self.roms / "psx").mkdir()
        write_catalog(self.root, [("psx", g) for g in systems["psx"].values()])
        path = catalog_store.ra_list_path(self.root, 12)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(catalog.encode_ra_list(12, lists[12].games, lists[12].fetched))
        self.urls: list[str] = []
        self.pauses: list[float] = []
        self.binary: list[str] = []
        self.warnings: list[str] = []
        self.progress_lines = {77: line(77, 2, 1, "2026-10-01", "beaten-hardcore")}
        self.down: set[str] = set()

    def fetch(self, url: str) -> bytes:
        self.urls.append(url)
        for name in self.down:
            if name in url:
                raise urllib.error.URLError(f"cannot reach {url}")
        if "GetGameExtended" in url:
            return extended(int(url.split("i=")[1].split("&")[0]))
        if "CompletionProgress" in url:
            return completion(*self.progress_lines.values())
        return user_progress()

    def fetch_binary(self, url: str, limit: int) -> tuple[bytes, str]:
        self.binary.append(url)
        return PNG_OK, "image/png"

    def go(self, **kw):  # type: ignore[no-untyped-def]
        self.lines.clear()
        options = dict(
            key=SECRET,
            user=USER,
            force=False,
            dry_run=False,
            out=self.lines.append,
            warn=self.warnings.append,
            fetch=self.fetch,
            fetch_binary=self.fetch_binary,
            sleep=self.pauses.append,
            now=NOW,
        )
        options.update(kw)
        return ra_store.run_ra_achievements(self.root, **options)

    def calls(self, name: str) -> list[str]:
        return [u for u in self.urls if name in u]


class PassTests(PassCase):
    def test_a_first_pass_downloads_everything_and_keeps_the_key_out_of_its_words(self) -> None:
        result = self.go()
        self.assertEqual(
            (result.games, result.details_fetched, result.user_fetched, result.unlocks_fetched),
            (2, 2, True, 1),
        )
        self.assertFalse(result.failed)
        self.assertEqual(len(self.calls("GetGameExtended")), 2)
        self.assertEqual(len(self.calls("CompletionProgress")), 1)
        self.assertEqual(
            self.calls("GameInfoAndUserProgress"), [f"{ra.USER_GAME_URL}?g=77&u={USER}&y={SECRET}"]
        )
        # The two games share their badge names here: 3 achievements x 2 images.
        self.assertEqual(result.badges_fetched, 6)
        self.assertEqual(result.badges_left, 0)
        self.assertNotIn(SECRET, "\n".join(self.lines + self.warnings))
        self.assertTrue(all(ra.is_media_url(u) for u in self.binary))
        self.assertEqual(len({*self.binary}), len(self.binary))
        root = self.root / "catalog" / "ra"
        self.assertTrue((root / "games" / "77.json").is_file())
        self.assertTrue((root / "user" / "summary.json").is_file())
        self.assertTrue((root / "user" / "games" / "77.json").is_file())
        self.assertEqual((root / "badges" / "100.png").read_bytes(), PNG_OK)
        self.assertTrue((root / "badges" / "100_lock.png").is_file())
        self.assertFalse((root / "user" / "games" / "8.json").exists())  # 8 was never played
        self.assertEqual(self.pauses.count(ra.CALL_DELAY), len(self.urls) - 1)

    def test_a_second_pass_asks_for_nothing(self) -> None:
        self.go()
        self.urls.clear()
        self.binary.clear()
        result = self.go()
        self.assertEqual((self.urls, self.binary), ([], []))
        self.assertEqual(
            (result.details_fetched, result.user_fetched, result.unlocks_fetched), (0, False, 0)
        )

    def test_achievements_are_asked_again_after_14_days_and_the_progress_after_5_hours(
        self,
    ) -> None:
        self.go()
        self.urls.clear()
        self.go(now=NOW + timedelta(hours=6))
        self.assertEqual(len(self.calls("GetGameExtended")), 0)
        self.assertEqual(len(self.calls("CompletionProgress")), 1)
        self.assertEqual(self.calls("GameInfoAndUserProgress"), [])  # the line did not change
        self.urls.clear()
        self.go(now=NOW + timedelta(days=15))
        self.assertEqual(len(self.calls("GetGameExtended")), 2)

    def test_the_unlock_dates_are_asked_again_when_the_line_changes_or_the_user_does(self) -> None:
        self.go()
        self.urls.clear()
        self.progress_lines[77] = line(77, 3, 1, "2026-11-01", "beaten-hardcore")
        self.go(now=NOW + timedelta(hours=6))
        self.assertEqual(len(self.calls("GameInfoAndUserProgress")), 1)
        self.urls.clear()
        self.go(now=NOW + timedelta(hours=12), user="Somebody")
        self.assertEqual(len(self.calls("CompletionProgress")), 1)
        self.assertIn("u=Somebody", self.calls("CompletionProgress")[0])
        self.assertEqual(len(self.calls("GameInfoAndUserProgress")), 1)

    def test_without_a_user_nothing_is_asked_about_one(self) -> None:
        result = self.go(user=None)
        self.assertEqual(
            self.calls("CompletionProgress") + self.calls("GameInfoAndUserProgress"), []
        )
        self.assertFalse((self.root / "catalog" / "ra" / "user").exists())
        self.assertEqual(result.details_fetched, 2)

    def test_force_asks_again(self) -> None:
        self.go()
        self.urls.clear()
        self.go(force=True)
        self.assertEqual(len(self.calls("GetGameExtended")), 2)
        self.assertEqual(len(self.calls("CompletionProgress")), 1)

    def test_a_dry_run_says_what_it_would_do_and_does_nothing(self) -> None:
        result = self.go(
            dry_run=True,
            fetch=lambda url: self.fail_test(),
            fetch_binary=lambda u, n: self.fail_test(),
        )
        self.assertEqual(result.details_left, 2)
        self.assertFalse((self.root / "catalog" / "ra").joinpath("games").exists())
        self.assertIn("RA achievements: would download 2 game(s) of 2", self.lines)

    def fail_test(self):  # type: ignore[no-untyped-def]
        self.fail("no call in a dry run")

    def test_the_budgets_bound_a_run_and_the_next_one_goes_on(self) -> None:
        first = self.go(call_limit=2, badge_limit=3)
        self.assertEqual(len(self.urls), 2)
        self.assertEqual(first.details_fetched, 2)
        self.assertFalse(first.user_fetched)
        self.assertEqual((first.badges_fetched, first.badges_left), (3, 3))
        second = self.go(call_limit=5, badge_limit=100)
        self.assertEqual(
            (second.user_fetched, second.unlocks_fetched, second.badges_left), (True, 1, 0)
        )

    def test_a_failed_call_is_reported_by_kind_and_the_pass_goes_on(self) -> None:
        self.down = {"i=77"}
        result = self.go()
        self.assertEqual((result.details_failed, result.details_fetched), (1, 1))
        self.assertTrue(result.failed)
        self.assertTrue(any("RA game 77: download failed (URLError)" in w for w in self.warnings))
        self.assertNotIn(SECRET, "\n".join(self.warnings + self.lines))
        self.assertTrue(result.user_fetched)
        self.assertGreater(result.badges_fetched, 0)
        self.urls.clear()
        self.down = set()
        self.go()
        self.assertEqual(
            [u.split("i=")[1].split("&")[0] for u in self.calls("GetGameExtended")], ["77"]
        )

    def test_three_failures_in_a_row_stop_the_section(self) -> None:
        self.down = {"GetGameExtended"}
        with mock.patch.object(ra_store, "STOP_AFTER", 1):
            result = self.go()
        self.assertEqual(result.details_failed, 1)  # the second game was not even tried
        self.assertEqual(len(self.urls), 1)
        self.assertFalse(result.user_fetched)  # a key that fails once is not tried again
        self.assertTrue(any("stopping after repeated failures" in w for w in self.warnings))
        self.assertNotIn(SECRET, "\n".join(self.warnings))

    def test_a_failed_progress_keeps_the_cached_one(self) -> None:
        self.go()
        before = (self.root / "catalog" / "ra" / "user" / "summary.json").read_bytes()
        self.down = {"CompletionProgress"}
        result = self.go(now=NOW + timedelta(hours=6))
        self.assertTrue(result.user_failed)
        self.assertEqual(
            (self.root / "catalog" / "ra" / "user" / "summary.json").read_bytes(), before
        )

    def test_a_bad_badge_is_not_written(self) -> None:
        result = self.go(fetch_binary=lambda url, limit: (b"<html>", "text/html"))
        self.assertEqual(result.badges_fetched, 0)
        self.assertGreater(result.badges_failed, 0)
        self.assertFalse((self.root / "catalog" / "ra" / "badges").exists())
        self.assertTrue(any("BadImage" in w for w in self.warnings))

    def test_a_pass_needs_a_key_and_a_hub_root(self) -> None:
        with self.assertRaises(CatalogError):
            self.go(key=None)
        with tempfile.TemporaryDirectory() as tmp, self.assertRaises(CatalogError):
            ra_store.run_ra_achievements(
                Path(tmp),
                key=SECRET,
                user=None,
                force=False,
                dry_run=False,
                out=print,
                warn=print,
            )

    def test_without_cached_lists_there_is_nothing_to_do(self) -> None:
        catalog_store.ra_list_path(self.root, 12).unlink()
        result = self.go()
        self.assertEqual((result.games, self.urls), (0, []))


class CliTests(PassCase):
    def setUp(self) -> None:
        super().setUp()
        env = mock.patch.dict(os.environ)
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop(ra.KEY_ENV, None)
        self.config = self.root / "config.toml"
        self.config.write_text(f'[ra]\nuser = "{USER}"\n')

    def catalog(self, *args: str) -> tuple[int, str, str]:
        return run("catalog", "--config", str(self.config), "--root", str(self.root), *args)

    def test_it_needs_the_key_before_scanning_anything(self) -> None:
        code, _, err = self.catalog("--ra-achievements")
        self.assertEqual(code, 2)
        self.assertIn(ra.KEY_ENV, err)

    def test_it_uses_the_user_of_the_config_and_never_prints_the_key(self) -> None:
        os.environ[ra.KEY_ENV] = SECRET
        with (
            mock.patch.object(ra, "http_fetch", self.fetch),
            mock.patch.object(ra, "http_fetch_binary", self.fetch_binary),
            mock.patch.object(ra.time, "sleep", lambda s: None),
            mock.patch.object(catalog_store, "run_catalog", return_value=[]),
        ):
            code, out, err = self.catalog("--ra-achievements")
        self.assertEqual((code, err), (0, ""), out)
        self.assertIn(f"u={USER}", self.calls("CompletionProgress")[0])
        self.assertNotIn(SECRET, out + err)
        self.assertIn("RA badges: 6 image(s) downloaded, 0 left", out)

    def test_force_is_allowed_with_it(self) -> None:
        os.environ[ra.KEY_ENV] = SECRET
        with (
            mock.patch.object(ra, "http_fetch", self.fetch),
            mock.patch.object(ra, "http_fetch_binary", self.fetch_binary),
            mock.patch.object(ra.time, "sleep", lambda s: None),
        ):
            self.assertEqual(self.catalog("--ra-achievements", "--force", "--dry-run")[0], 0)


class ServeAchievementsTests(ServeCase):
    def setUp(self) -> None:
        super().setUp()
        systems, lists = library_with_ra()
        items = [("psx", g) for g in systems["psx"].values()]
        write_catalog(self.root, items + [(s, g) for s, g in games() if s != "psx"])
        ra_dir = self.root / "catalog" / "ra"
        ra_dir.mkdir(parents=True, exist_ok=True)
        (ra_dir / "12.json").write_bytes(
            catalog.encode_ra_list(12, lists[12].games, lists[12].fetched)
        )
        data = cached_data()
        (ra_dir / "games").mkdir()
        (ra_dir / "games" / "77.json").write_bytes(
            ra_data.encode_game(detail(), "2026-11-09T09:00:00Z")
        )
        (ra_dir / "user" / "games").mkdir(parents=True)
        assert data.user is not None
        (ra_dir / "user" / "summary.json").write_bytes(
            ra_data.encode_user(USER, "2026-11-09T09:00:00Z", data.user.games)
        )
        cached = data.unlocks[77]
        (ra_dir / "user" / "games" / "77.json").write_bytes(
            ra_data.encode_unlocks(USER, 77, cached.fetched, cached.seen, cached.unlocks)
        )
        (ra_dir / "badges").mkdir()
        (ra_dir / "badges" / "100.png").write_bytes(PNG)
        self.ra_dir = ra_dir
        self.cfg = replace(self.cfg, ra=RaSettings(USER))
        self.start()
        self.example = next(g for g in systems["psx"].values() if g.title == EXAMPLE.title)

    def test_the_game_has_its_achievements_with_the_progress(self) -> None:
        reply = self.get(f"/api/v1/games/{self.example.id}")
        self.assertEqual(reply.status, 200)
        found = reply.json["achievements"]
        self.assertTrue(found["available"])
        self.assertEqual(found["totals"]["unlocked"], 2)
        self.assertEqual(found["progress"]["user"], USER)
        self.assertEqual(len(found["list"]), 3)
        self.assertEqual(found["list"][0]["rarity"]["percent"], 90.0)
        self.assertEqual(reply.json["ra"]["state"], "supported")

    def test_a_game_without_ra_achievements_has_null(self) -> None:
        self.assertIsNone(self.get(f"/api/v1/games/{TINY.id}").json["achievements"])

    def test_without_a_configured_user_the_list_is_there_and_the_progress_is_not(self) -> None:
        self.cfg = replace(self.cfg, ra=RaSettings())
        self.start()
        found = self.get(f"/api/v1/games/{self.example.id}").json["achievements"]
        self.assertIsNone(found["progress"])
        self.assertEqual(len(found["list"]), 3)
        summary = self.get("/api/v1/ra/summary").json
        self.assertIsNone(summary["user"])
        self.assertEqual(summary["total"]["games"], 2)
        self.assertIsNone(summary["total"]["played"])

    def test_the_summary(self) -> None:
        reply = self.get("/api/v1/ra/summary")
        self.assertEqual(reply.status, 200)
        self.assertEqual(reply.json["user"], USER)
        self.assertEqual(reply.json["total"]["played"], 2)
        self.assertEqual([s["system"] for s in reply.json["systems"]], ["psx"])

    def test_a_badge_is_served_with_the_same_auth(self) -> None:
        reply = self.get("/api/v1/ra/badges/100/unlocked")  # only 100.png exists, not _lock
        self.assertEqual((reply.status, reply.body), (200, PNG))
        self.assertEqual(self.get("/api/v1/ra/badges/100/locked").status, 404)
        (self.ra_dir / "badges" / "100_lock.png").write_bytes(PNG)
        ok = self.get("/api/v1/ra/badges/100/locked")
        self.assertEqual((ok.status, ok.body), (200, PNG))
        self.assertEqual(ok.headers["Content-Type"], "image/png")
        self.assertEqual(ok.headers["X-Content-Type-Options"], "nosniff")
        self.assertEqual(self.get("/api/v1/ra/badges/100/unlocked", auth=False).status, 401)
        self.assertEqual(self.get("/api/v1/ra/summary", auth=False).status, 401)

    def test_a_badge_that_is_not_one_is_404(self) -> None:
        secret = self.ra_dir.parent / "secret.png"
        secret.write_bytes(PNG)
        for path in (
            "/api/v1/ra/badges/100/other",
            "/api/v1/ra/badges/..%2fsecret/unlocked",
            "/api/v1/ra/badges/100.png/unlocked",
            "/api/v1/ra/badges/999/unlocked",
            "/api/v1/ra/badges/100",
            "/api/v1/ra/nothing",
        ):
            self.assertEqual(self.get(path).status, 404, path)

    def test_a_symlinked_badge_is_not_served(self) -> None:
        (self.ra_dir / "badges" / "200.png").symlink_to(self.secret)
        self.assertEqual(self.get("/api/v1/ra/badges/200/unlocked").status, 404)

    def test_a_cache_file_that_changes_is_picked_up(self) -> None:
        path = self.ra_dir / "games" / "77.json"
        doc = json.loads(path.read_text())
        doc["achievements"] = doc["achievements"][:1]
        path.write_text(json.dumps(doc))
        found = self.get(f"/api/v1/games/{self.example.id}").json["achievements"]
        self.assertEqual(len(found["list"]), 1)


if __name__ == "__main__":
    unittest.main()
