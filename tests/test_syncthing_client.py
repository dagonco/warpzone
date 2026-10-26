import unittest

from support import API_KEY, HUB_ID, FakeSyncthing
from warpzone.localst import (
    LocalSyncthingError,
    candidate_paths,
    find_config,
    parse_config,
)
from warpzone.syncthing import REDACTED, Client, SyncthingError, redact, scrub


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


if __name__ == "__main__":
    unittest.main()
