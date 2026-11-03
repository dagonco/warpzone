"""Path helpers."""

import unittest
from pathlib import Path

from warpzone.paths import expand_home


class ExpandHomeTests(unittest.TestCase):
    def test_expand_home(self) -> None:
        home = Path("/home/x")
        self.assertEqual(expand_home("~/Emulation", home), "/home/x/Emulation")
        self.assertEqual(expand_home("~", home), "/home/x")
        self.assertEqual(expand_home("/abs/~/x", home), "/abs/~/x")


if __name__ == "__main__":
    unittest.main()
