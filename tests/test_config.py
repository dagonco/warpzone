import copy
import tempfile
import unittest
from pathlib import Path

from support import API_KEY, config_data
from warpzone.config import ConfigError, load_config, parse_config


class ConfigTests(unittest.TestCase):
    def test_valid_config(self) -> None:
        cfg = parse_config(config_data(), {"steamos-emudeck"})
        self.assertEqual(cfg.hub.name, "nas")
        self.assertEqual(sorted(cfg.devices), ["deck", "machine"])
        self.assertEqual(cfg.devices["deck"].addresses, ("dynamic",))

    def test_api_key_is_not_in_repr(self) -> None:
        cfg = parse_config(config_data())
        self.assertNotIn(API_KEY, repr(cfg))

    def assert_error(self, mutate, needle: str, profiles=None) -> None:
        data = copy.deepcopy(config_data())
        mutate(data)
        with self.assertRaises(ConfigError) as ctx:
            parse_config(data, profiles)
        self.assertIn(needle, str(ctx.exception))

    def test_missing_hub(self) -> None:
        self.assert_error(lambda d: d.pop("hub"), "[hub]: required table")

    def test_no_devices(self) -> None:
        self.assert_error(lambda d: d.pop("devices"), "at least one device")

    def test_bad_device_id(self) -> None:
        self.assert_error(
            lambda d: d["devices"]["deck"].update(device_id="nope"), "not a Syncthing device ID"
        )

    def test_duplicate_device_id(self) -> None:
        self.assert_error(
            lambda d: d["devices"]["deck"].update(device_id=d["hub"]["device_id"]),
            "same ID as hub",
        )

    def test_key_and_key_file_are_exclusive(self) -> None:
        self.assert_error(lambda d: d["hub"].update(api_key_file="/x"), "exactly one of")
        self.assert_error(lambda d: d["hub"].pop("api_key"), "exactly one of")

    def test_unknown_keys_are_rejected(self) -> None:
        self.assert_error(lambda d: d["hub"].update(typo=1), "unknown key 'typo'")
        self.assert_error(lambda d: d["devices"]["deck"].update(typo=1), "unknown key 'typo'")
        self.assert_error(lambda d: d.update(extra={}), "unknown key 'extra'")

    def test_unknown_folder_group(self) -> None:
        self.assert_error(
            lambda d: d["devices"]["deck"].update(folders=["library", "games"]),
            "unknown folder 'games'",
        )

    def test_unknown_profile(self) -> None:
        self.assert_error(
            lambda d: d["devices"]["deck"].update(profile="nope"),
            "unknown profile 'nope'",
            {"steamos-emudeck"},
        )

    def test_bad_api_url_and_device_name(self) -> None:
        self.assert_error(lambda d: d["hub"].update(api_url="nas:8384"), "http:// or https://")
        self.assert_error(
            lambda d: d["devices"].update({"Bad Name": d["devices"]["deck"]}), "name must be"
        )

    def test_rescan_seconds(self) -> None:
        self.assertEqual(parse_config(config_data()).devices["deck"].rescan_seconds, 300)
        data = copy.deepcopy(config_data())
        data["devices"]["deck"]["rescan_seconds"] = 60
        self.assertEqual(parse_config(data).devices["deck"].rescan_seconds, 60)
        for bad in (0, 4, "60", 1.5, True):
            self.assert_error(
                lambda d, bad=bad: d["devices"]["deck"].update(rescan_seconds=bad),
                "rescan_seconds: must be an integer >= 5",
            )

    def test_all_errors_are_reported_together(self) -> None:
        data = copy.deepcopy(config_data())
        data["hub"]["device_id"] = "x"
        data["devices"]["deck"]["folders"] = []
        with self.assertRaises(ConfigError) as ctx:
            parse_config(data)
        self.assertEqual(len(str(ctx.exception).splitlines()), 2)

    def test_key_file(self) -> None:
        data = copy.deepcopy(config_data())
        with tempfile.TemporaryDirectory() as tmp:
            key_file = Path(tmp) / "hub.key"
            key_file.write_text(f"{API_KEY}\n")
            del data["hub"]["api_key"]
            data["hub"]["api_key_file"] = str(key_file)
            self.assertEqual(parse_config(data).hub.api_key, API_KEY)
            key_file.write_text("\n")
            with self.assertRaises(ConfigError) as ctx:
                parse_config(data)
            self.assertIn("is empty", str(ctx.exception))
            data["hub"]["api_key_file"] = str(Path(tmp) / "missing.key")
            with self.assertRaises(ConfigError) as ctx:
                parse_config(data)
            self.assertIn("cannot read", str(ctx.exception))

    def test_load_config_errors(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(ConfigError) as ctx:
                load_config(Path(tmp) / "missing.toml")
            self.assertIn("config not found", str(ctx.exception))
            bad = Path(tmp) / "bad.toml"
            bad.write_text("[hub\n")
            with self.assertRaises(ConfigError):
                load_config(bad)

    def test_example_config_parses(self) -> None:
        example = Path(__file__).parent.parent / "examples" / "config.example.toml"
        text = example.read_text().replace("api_key_file", "api_key")
        text = text.replace("~/.config/warpzone/hub.key", "fake-key")
        import tomllib

        cfg = parse_config(tomllib.loads(text), {"steamos-emudeck"})
        self.assertEqual(sorted(cfg.devices), ["deck", "machine"])


if __name__ == "__main__":
    unittest.main()
