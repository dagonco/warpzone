"""The order of package versions and release tags."""

import unittest

from warpzone.versions import tag_key, version_key


class VersionTests(unittest.TestCase):
    def test_version_key_puts_a_pre_release_before_its_release(self) -> None:
        assert version_key("0.3.0rc1") is not None
        assert version_key("0.3.0") is not None
        self.assertLess(version_key("0.3.0rc1"), version_key("0.3.0"))  # type: ignore[operator]
        self.assertLess(version_key("0.2.9"), version_key("0.3.0rc1"))  # type: ignore[operator]
        self.assertIsNone(version_key("dev"))

    def test_version_key_orders_release_candidates(self) -> None:
        order = ["0.2.9", "0.3.0rc1", "0.3.0rc2", "0.3.0rc10", "0.3.0", "0.3.1rc1", "0.10.0"]
        keys = [version_key(v) for v in order]
        self.assertNotIn(None, keys)
        self.assertEqual(keys, sorted(keys))  # type: ignore[type-var]
        self.assertEqual(len(set(keys)), len(keys), "no two versions tie")

    def test_tag_key_orders_candidate_tags(self) -> None:
        order = ["v0.2.9", "v0.3.0rc1", "v0.3.0rc2", "v0.3.0rc10", "v0.3.0", "v0.10.0rc1"]
        keys = [tag_key(t) for t in order]
        self.assertNotIn(None, keys)
        self.assertEqual(keys, sorted(keys))  # type: ignore[type-var]
        self.assertEqual(len(set(keys)), len(keys))
        shuffled = ["v0.3.0", "v0.10.0rc1", "v0.3.0rc2", "v0.2.9", "v0.3.0rc10", "v0.3.0rc1"]
        self.assertEqual(sorted(shuffled, key=tag_key), order)  # type: ignore[arg-type,return-value]

    def test_tag_key_takes_only_the_spelling_of_the_releasing_guide(self) -> None:
        for tag in (
            "0.3.0rc1",
            "v0.3",
            "v0.3.0-rc1",
            "v0.3.0-rc.1",
            "v0.3.0rc",
            "v0.3.0a1",
            "v0.3.0rc1.dev1",
            "nightly",
            "",
        ):
            self.assertIsNone(tag_key(tag), tag)

    def test_tag_key_agrees_with_the_version_key_of_the_package_version(self) -> None:
        for version in ("0.3.0", "0.3.0rc1", "10.0.12rc7"):
            self.assertEqual(tag_key(f"v{version}"), version_key(version))


if __name__ == "__main__":
    unittest.main()
