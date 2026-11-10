import io
import json
import tempfile
import unittest
import urllib.error
from pathlib import Path

from inbox_support import RAHASHER, make_stub, stub_md5
from warpzone import __version__, ra

LIST = json.dumps(
    [
        {
            "Title": "Adventure Island",
            "ID": 77,
            "NumAchievements": 12,
            "Points": 150,
            "Hashes": ["ABCDEF" + "0" * 26],
        },
        {"Title": "Empty Set", "NumAchievements": 0, "Hashes": ["1" * 32]},
        {"Title": "No Hashes"},
        "junk",
    ]
).encode()
KEY = "very-secret-key"  # gitleaks:allow (test fixture, not a real key)


class ParseTests(unittest.TestCase):
    def test_hashes_map_to_games_in_lower_case(self) -> None:
        games = ra.parse_game_list(LIST)
        self.assertEqual(games["abcdef" + "0" * 26].title, "Adventure Island")
        self.assertEqual(games["1" * 32].achievements, 0)
        self.assertEqual(len(games), 2)

    def test_the_id_and_points_are_kept(self) -> None:
        games = ra.parse_game_list(LIST)
        first = games["abcdef" + "0" * 26]
        self.assertEqual((first.id, first.points), (77, 150))
        self.assertEqual((games["1" * 32].id, games["1" * 32].points), (0, 0))

    def test_not_a_list_is_an_error(self) -> None:
        with self.assertRaises(ValueError):
            ra.parse_game_list(b'{"Error": "x"}')

    def test_console_ids_cover_what_inbox_places(self) -> None:
        from warpzone.dat import SYSTEMS

        self.assertLessEqual(set(SYSTEMS.values()), set(ra.CONSOLE_IDS))

    def test_console_ids_pin_known_systems(self) -> None:
        ids = ra.CONSOLE_IDS
        known = {
            "genesis": 1,
            "n64": 2,
            "snes": 3,
            "nes": 7,
            "psx": 12,
            "nds": 18,
            "ps2": 21,
            "pokemini": 24,
            "atari2600": 25,
            "arcade": 27,
            "dreamcast": 40,
            "psp": 41,
            "n3ds": 62,
            "fds": 81,
            "atarilynx": 13,
            "atari7800": 51,
            "wonderswancolor": 53,
        }
        for system, console in known.items():
            self.assertEqual(ids.get(system), console, system)

    def test_console_ids_alias_folders_share_the_console(self) -> None:
        ids = ra.CONSOLE_IDS
        self.assertEqual(ids["genesis"], ids["megadrive"])
        self.assertEqual(ids["pcengine"], ids["tg16"])
        self.assertEqual(ids["segacd"], ids["megacd"])
        self.assertEqual(ids["ngp"], ids["ngpc"])
        self.assertEqual({ids[s] for s in ("arcade", "mame", "fbneo", "neogeo")}, {27})

    def test_console_ids_are_es_de_folder_names_and_rcheevos_ids(self) -> None:
        for system, console in ra.CONSOLE_IDS.items():
            self.assertRegex(system, r"^[a-z0-9][a-z0-9-]*$")
            self.assertTrue(1 <= console <= 82, system)  # rcheevos console ids, without hubs
        # Consoles RA has no sets for, or RAHasher cannot hash, stay out.
        for system in ("wii", "wiiu", "xbox", "ps3", "switch", "ps4", "psvita", "atari5200"):
            self.assertNotIn(system, ra.CONSOLE_IDS)


class FetchTests(unittest.TestCase):
    def test_the_list_of_a_console_is_asked_for_with_hashes_and_the_key(self) -> None:
        urls: list[str] = []
        games = ra.fetch_game_list(12, KEY, lambda url: urls.append(url) or LIST)
        self.assertEqual(urls, [f"{ra.API_URL}?i=12&h=1&y={KEY}"])
        self.assertEqual(len(games), 2)

    def test_a_failure_names_its_kind_and_keeps_nothing_of_the_original_error(self) -> None:
        forbidden = urllib.error.HTTPError(
            f"http://x/?y={KEY}",
            403,
            "Forbidden",
            {},  # type: ignore[arg-type]
            io.BytesIO(),
        )
        self.addCleanup(forbidden.close)
        for error, kind in (
            (forbidden, "HTTP 403"),
            (urllib.error.URLError(f"cannot reach http://x/?y={KEY}"), "URLError"),
            (ValueError(f"bad json for y={KEY}"), "ValueError"),
            (TimeoutError(f"timed out y={KEY}"), "TimeoutError"),
        ):

            def fetch(url: str, error: Exception = error) -> bytes:
                raise error

            with self.assertRaises(ra.FetchError) as caught:
                ra.fetch_game_list(12, KEY, fetch)
            self.assertEqual(str(caught.exception), kind)
            self.assertIsNone(caught.exception.__cause__)
            self.assertTrue(caught.exception.__suppress_context__)


class LookupTests(unittest.TestCase):
    def lookup(self, fetch):  # type: ignore[no-untyped-def]
        return ra.Lookup(KEY, fetch)

    def test_status_texts(self) -> None:
        look = self.lookup(lambda url: LIST)
        self.assertEqual(look.status("nes", "abcdef" + "0" * 26), "supported (Adventure Island)")
        self.assertEqual(look.status("nes", "1" * 32), "linked, no achievements yet (Empty Set)")
        self.assertEqual(look.status("nes", "2" * 32), "hash not linked")

    def test_one_download_per_console(self) -> None:
        urls: list[str] = []

        def fetch(url: str) -> bytes:
            urls.append(url)
            return LIST

        look = self.lookup(fetch)
        for _ in range(3):
            look.status("nes", "2" * 32)
        look.status("psx", "2" * 32)
        self.assertEqual(len(urls), 2)
        self.assertIn("i=7&h=1&y=" + KEY, urls[0])
        self.assertIn("i=12&", urls[1])

    def test_failures_never_carry_the_key(self) -> None:
        for error in (
            urllib.error.HTTPError(f"http://x/?y={KEY}", 403, "Forbidden", {}, io.BytesIO()),  # type: ignore[arg-type]
            urllib.error.URLError(f"cannot reach http://x/?y={KEY}"),
            ValueError(f"bad json for y={KEY}"),
        ):

            def fetch(url: str, error: Exception = error) -> bytes:
                raise error

            text = self.lookup(fetch).status("nes", "2" * 32)
            self.assertTrue(text.startswith("lookup failed"), text)
            self.assertNotIn(KEY, text)

    def test_failed_list_is_not_retried(self) -> None:
        calls: list[str] = []

        def fetch(url: str) -> bytes:
            calls.append(url)
            raise urllib.error.URLError("down")

        look = self.lookup(fetch)
        look.status("nes", "a" * 32)
        look.status("nes", "b" * 32)
        self.assertEqual(len(calls), 1)

    def test_without_a_key_nothing_is_fetched(self) -> None:
        look = ra.Lookup(None, lambda url: self.fail("no request without a key"))
        self.assertFalse(look.enabled)
        self.assertEqual(look.status("nes", "a" * 32), "lookup skipped")

    def test_user_agent_names_the_version(self) -> None:
        self.assertEqual(ra.USER_AGENT, f"warpzone/{__version__}")


class HashTests(unittest.TestCase):
    def test_hash_image_reads_the_last_line_and_rejects_noise(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tool = make_stub(Path(tmp) / "bin", "RAHasher", RAHASHER)
            rom = Path(tmp) / "a.nes"
            rom.write_bytes(b"abc")
            self.assertEqual(ra.hash_image(tool, "nes", rom), stub_md5(b"abc"))
            self.assertIsNone(ra.hash_image(tool, "nes", Path(tmp) / "missing.nes"))  # tool fails
            self.assertIsNone(ra.hash_image(tool, "unknown-system", rom))
            self.assertIsNone(ra.hash_image(str(Path(tmp) / "nope"), "nes", rom))

    def test_cue_to_gdi_places_the_high_density_area_at_45000(self) -> None:
        cue = (
            'FILE "t1.bin" BINARY\n  TRACK 01 MODE1/2352\n    INDEX 01 00:00:00\n'
            'FILE "t2.raw" BINARY\n  TRACK 02 AUDIO\n    INDEX 01 00:00:00\n'
            'FILE "t3.bin" BINARY\n  TRACK 03 MODE1/2352\n    INDEX 01 00:00:00\n'
        )
        gdi = ra.cue_to_gdi(cue, [600, 6000, 500000])
        self.assertEqual(
            gdi.splitlines(),
            ["3", '1 0 4 2352 "t1.bin" 0', '2 750 0 2352 "t2.raw" 0', '3 45000 4 2352 "t3.bin" 0'],
        )
        with self.assertRaises(ValueError):
            ra.cue_to_gdi(cue, [1])


if __name__ == "__main__":
    unittest.main()
