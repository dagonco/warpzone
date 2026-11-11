import hashlib
import http.client
import unittest
import urllib.request
from unittest import mock

from support import API_KEY, DECK_ID, HUB_ID, FakeSyncthing
from warpzone.localst import (
    LocalSyncthingError,
    candidate_paths,
    find_config,
    parse_config,
)
from warpzone.syncthing import (
    REDACTED,
    Client,
    SyncthingError,
    cert_sha256_matches,
    format_fingerprint,
    mismatch_message,
    redact,
    scrub,
)


class RedactionTests(unittest.TestCase):
    def test_redacts_secret_keys_recursively(self) -> None:
        body = {
            "apikey": "k1",
            "ApiKey": "k2",
            "gui": {"password": "p", "list": [{"X-API-Key": "k3"}]},
            "devices": [{"deviceID": "ID", "encryptionPassword": "s"}],
            "label": "ok",
        }
        out = redact(body)
        text = str(out)
        for secret in ("k1", "k2", "k3", "'p'", "'s'"):
            self.assertNotIn(secret, text)
        self.assertEqual(out["label"], "ok")
        self.assertEqual(out["devices"][0]["deviceID"], "ID")

    def test_redacts_known_secret_values_anywhere(self) -> None:
        out = redact({"note": f"key is {API_KEY} here", "list": [API_KEY]}, (API_KEY,))
        self.assertNotIn(API_KEY, str(out))
        self.assertIn(REDACTED, str(out))

    def test_scrub_ignores_empty_secret(self) -> None:
        self.assertEqual(scrub("abc", ("",)), "abc")


class ClientTests(unittest.TestCase):
    def test_sends_api_key_and_reads_json(self) -> None:
        with FakeSyncthing(HUB_ID) as fake:
            self.assertEqual(Client(fake.url, API_KEY).my_id(), HUB_ID)

    def test_wrong_key_error_does_not_leak_key(self) -> None:
        with FakeSyncthing(HUB_ID) as fake:
            client = Client(fake.url, "wrong-key-value", label="hub")
            with self.assertRaises(SyncthingError) as ctx:
                client.my_id()
        self.assertEqual(ctx.exception.status, 403)
        self.assertNotIn("wrong-key-value", str(ctx.exception))

    def test_error_body_echoing_the_key_is_scrubbed(self) -> None:
        with FakeSyncthing(HUB_ID, api_key="real-key-value") as fake:
            client = Client(fake.url, "real-key-value")
            fake.route = lambda *a: (500, "boom real-key-value boom")  # type: ignore[method-assign]
            with self.assertRaises(SyncthingError) as ctx:
                client.my_id()
        self.assertNotIn("real-key-value", str(ctx.exception))

    def test_unreachable(self) -> None:
        client = Client("http://127.0.0.1:1", API_KEY, label="hub", timeout=2)
        with self.assertRaises(SyncthingError) as ctx:
            client.my_id()
        self.assertIsNone(ctx.exception.status)
        self.assertIn("cannot reach", str(ctx.exception))
        self.assertNotIn(API_KEY, str(ctx.exception))

    def test_a_broken_http_exchange_is_a_syncthing_error(self) -> None:
        client = Client("http://127.0.0.1:1", "real-key-value", label="hub")
        broken = [
            http.client.RemoteDisconnected("closed, real-key-value"),
            http.client.IncompleteRead(b"abc", 10),
            http.client.BadStatusLine("real-key-value"),
        ]
        for exc in broken:
            with self.subTest(exc=type(exc).__name__):
                with (
                    mock.patch("urllib.request.urlopen", side_effect=exc),
                    self.assertRaises(SyncthingError) as ctx,
                ):
                    client.my_id()
                self.assertIsNone(ctx.exception.status)
                self.assertTrue(str(ctx.exception).startswith("hub: "))
                self.assertNotIn("real-key-value", str(ctx.exception))

    def test_a_body_cut_short_while_reading_is_a_syncthing_error(self) -> None:
        client = Client("http://127.0.0.1:1", API_KEY, label="hub")
        resp = mock.MagicMock()
        resp.__enter__.return_value = resp
        resp.read.side_effect = http.client.IncompleteRead(b"abc", 10)
        with (
            mock.patch("urllib.request.urlopen", return_value=resp),
            self.assertRaises(SyncthingError) as ctx,
        ):
            client.my_id()
        self.assertIn("IncompleteRead", str(ctx.exception))

    def test_a_full_scan_that_answers_is_done(self) -> None:
        with FakeSyncthing(HUB_ID) as fake:
            self.assertTrue(Client(fake.url, API_KEY).scan("wz-library"))
            self.assertEqual(fake.scans, 1)

    def test_a_full_scan_held_by_a_busy_folder_is_queued_not_an_error(self) -> None:
        # Syncthing answers a scan only when it ran, and a folder that is pulling runs it
        # after the pull: minutes on a slow device. The scan still happens then.
        with FakeSyncthing(HUB_ID) as fake:
            fake.scan_delay = 1.0
            client = Client(fake.url, API_KEY, timeout=0.2)
            self.assertFalse(client.scan("wz-library"))
            with self.assertRaises(SyncthingError) as ctx:
                client.scan("wz-library", sub="roms/n64")
            self.assertTrue(ctx.exception.timed_out)

    def test_an_unreachable_syncthing_is_still_an_error_for_a_full_scan(self) -> None:
        with self.assertRaises(SyncthingError) as ctx:
            Client("http://127.0.0.1:1", API_KEY, timeout=1).scan("wz-library")
        self.assertFalse(ctx.exception.timed_out)

    def test_connections_and_completion(self) -> None:
        with FakeSyncthing(HUB_ID) as fake:
            fake.connections = {DECK_ID: {"connected": True}}
            fake.completion[("wz-library", DECK_ID)] = {"completion": 42.5, "needBytes": 7}
            client = Client(fake.url, API_KEY)
            self.assertEqual(client.connections(), {DECK_ID: {"connected": True}})
            self.assertEqual(client.completion("wz-library", DECK_ID)["needBytes"], 7)
            with self.assertRaises(SyncthingError) as ctx:
                client.completion("wz-library", "X")
            self.assertEqual(ctx.exception.status, 404)

    def test_db_file_404_is_none(self) -> None:
        with FakeSyncthing(HUB_ID) as fake:
            self.assertIsNone(Client(fake.url, API_KEY).db_file("wz-library", "nope"))

    def test_dry_run_records_and_sends_no_writes(self) -> None:
        lines: list[str] = []
        with FakeSyncthing(HUB_ID) as fake:
            client = Client(fake.url, API_KEY, label="hub", dry_run=True, sink=lines.append)
            client.my_id()  # reads still go out
            client.add_device({"deviceID": "D", "name": "deck", "apikey": API_KEY})
            client.patch_folder("wz-x", {"paused": False})
            client.set_ignores("wz-x", ["*"])
            client.scan("wz-x")
            self.assertEqual(fake.writes(), [])
        self.assertEqual([c.method for c in client.recorded], ["POST", "PATCH", "POST", "POST"])
        self.assertEqual(client.recorded[2].path, "/rest/db/ignores?folder=wz-x")
        shown = "\n".join(lines)
        self.assertIn("[dry-run] hub: PATCH /rest/config/folders/wz-x", shown)
        self.assertIn('{"paused": false}', shown)
        self.assertNotIn(API_KEY, shown)

    def test_repr_has_no_key(self) -> None:
        self.assertNotIn(API_KEY, repr(Client("http://x", API_KEY)))


CONFIG_XML = """<configuration version="52">
  <gui enabled="true" tls="{tls}"><address>{addr}</address><apikey>{key}</apikey></gui>
</configuration>"""


class LocalSyncthingTests(unittest.TestCase):
    def write(self, tmp, *, tls="false", addr="127.0.0.1:8384", key="abc"):
        from pathlib import Path

        path = Path(tmp) / "config.xml"
        path.write_text(CONFIG_XML.format(tls=tls, addr=addr, key=key))
        return path

    def test_parse(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            local = parse_config(self.write(tmp))
            self.assertEqual((local.base_url, local.api_key), ("http://127.0.0.1:8384", "abc"))
            self.assertNotIn("abc", repr(local))
            self.assertEqual(
                parse_config(self.write(tmp, tls="true", addr="0.0.0.0:8443")).base_url,
                "https://127.0.0.1:8443",
            )
            self.assertEqual(
                parse_config(self.write(tmp, addr="[::]:8384")).base_url, "http://127.0.0.1:8384"
            )

    def test_parse_errors(self) -> None:
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as tmp:
            for kwargs in ({"key": ""}, {"addr": "/run/st.sock"}, {"addr": "localhost"}):
                with self.assertRaises(LocalSyncthingError):
                    parse_config(self.write(tmp, **kwargs))
            bad = Path(tmp) / "bad.xml"
            bad.write_text("<nope")
            with self.assertRaises(LocalSyncthingError):
                parse_config(bad)

    def test_candidates(self) -> None:
        from pathlib import Path

        home = Path("/home/deck")
        paths = [str(p) for p in candidate_paths({}, home, "linux")]
        self.assertIn("/home/deck/.local/state/syncthing/config.xml", paths)
        self.assertIn("/home/deck/.config/syncthing/config.xml", paths)
        self.assertTrue(any("com.github.zocker_160.SyncThingy" in p for p in paths))
        mac = [str(p) for p in candidate_paths({}, Path("/home/mac"), "darwin")]
        self.assertEqual(mac[0], "/home/mac/Library/Application Support/Syncthing/config.xml")
        env = candidate_paths({"STHOMEDIR": "/st", "XDG_STATE_HOME": "/state"}, home, "linux")
        self.assertEqual(str(env[0]), "/st/config.xml")
        self.assertIn("/state/syncthing/config.xml", [str(p) for p in env])

    def test_find_config(self) -> None:
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            with self.assertRaises(LocalSyncthingError) as ctx:
                find_config(env={}, home=home, platform="linux")
            self.assertIn("--syncthing-config", str(ctx.exception))
            target = home / ".config" / "syncthing" / "config.xml"
            target.parent.mkdir(parents=True)
            target.write_text("<configuration/>")
            self.assertEqual(find_config(env={}, home=home, platform="linux"), target)
            self.assertEqual(find_config(target.parent), target)
            with self.assertRaises(LocalSyncthingError):
                find_config(home / "nowhere.xml")


class CertificatePinTests(unittest.TestCase):
    """The pure parts of pinning; tests/test_syncthing_pin.py talks to a real TLS server."""

    DER = b"not really a certificate, only bytes to hash"

    def test_matching_digest(self) -> None:
        self.assertTrue(cert_sha256_matches(self.DER, hashlib.sha256(self.DER).digest()))

    def test_a_digest_one_bit_off_does_not_match(self) -> None:
        digest = bytearray(hashlib.sha256(self.DER).digest())
        digest[-1] ^= 1
        self.assertFalse(cert_sha256_matches(self.DER, bytes(digest)))

    def test_no_certificate_is_a_mismatch(self) -> None:
        self.assertFalse(cert_sha256_matches(None, hashlib.sha256(b"").digest()))
        self.assertFalse(cert_sha256_matches(None, bytes(32)))

    def test_format_fingerprint_is_openssls_form(self) -> None:
        self.assertEqual(format_fingerprint("abcd01"), "AB:CD:01")
        self.assertEqual(format_fingerprint("0f" * 32), ":".join(["0F"] * 32))

    def test_mismatch_message_has_both_fingerprints_and_no_key(self) -> None:
        text = mismatch_message("hub", "https://hub.example.lan:8384", "ab" * 32, "01" * 32)
        self.assertIn(format_fingerprint("ab" * 32), text)
        self.assertIn(format_fingerprint("01" * 32), text)
        self.assertIn("https://hub.example.lan:8384", text)
        self.assertIn("nothing was sent", text)
        self.assertIn("hub.api_cert_sha256", text)
        self.assertIn("openssl x509 -in", text)
        self.assertNotIn(API_KEY, text)

    def test_mismatch_message_when_no_certificate_was_presented(self) -> None:
        text = mismatch_message("hub", "https://hub.example.lan:8384", None, "01" * 32)
        self.assertIn("It presented no certificate.", text)
        self.assertIn(format_fingerprint("01" * 32), text)

    def test_a_pin_needs_an_https_url(self) -> None:
        for url in ("http://hub.example.lan:8384", "http://127.0.0.1:8384"):
            with self.subTest(url=url), self.assertRaises(SyncthingError) as ctx:
                Client(url, API_KEY, label="hub", cert_sha256="ab" * 32)
            self.assertIn("https://", str(ctx.exception))
            self.assertNotIn(API_KEY, str(ctx.exception))

    def test_the_pinned_opener_has_no_redirect_proxy_or_plain_http_handler(self) -> None:
        client = Client("https://hub.example.lan:8384", API_KEY, cert_sha256="ab" * 32)
        opener = client._opener
        assert opener is not None
        forbidden = (
            urllib.request.ProxyHandler,
            urllib.request.HTTPRedirectHandler,
            urllib.request.HTTPHandler,
        )
        for handler in opener.handlers:
            self.assertNotIsInstance(handler, forbidden, type(handler).__name__)
        self.assertTrue(any(isinstance(h, urllib.request.HTTPSHandler) for h in opener.handlers))

    def test_without_a_pin_there_is_no_pinned_opener(self) -> None:
        self.assertIsNone(Client("https://hub.example.lan:8384", API_KEY)._opener)
        self.assertIsNone(Client("http://hub.example.lan:8384", API_KEY)._opener)


if __name__ == "__main__":
    unittest.main()
