import contextlib
import importlib.util
import io
import tempfile
import unittest
from pathlib import Path

from warpzone import __version__

ROOT = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location(
    "changelog_section", ROOT / "scripts" / "changelog_section.py"
)
changelog_section = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(changelog_section)

SAMPLE = """# Changelog

## [Unreleased]

## [0.2.0rc1] - 2026-11-01

### Added

- Second thing.

## [0.1.0] - 2026-10-27

### Added

- First thing.
- Another thing.

### Fixed

- A fix.

[Unreleased]: https://example.test/compare/v0.2.0rc1...HEAD
[0.2.0rc1]: https://example.test/compare/v0.1.0...v0.2.0rc1
[0.1.0]: https://example.test/releases/tag/v0.1.0
"""


class ExtractTests(unittest.TestCase):
    def test_middle_section_stops_at_next_heading(self) -> None:
        text = changelog_section.extract(SAMPLE, "0.2.0rc1")
        self.assertEqual(text, "### Added\n\n- Second thing.")

    def test_last_section_stops_at_link_references(self) -> None:
        text = changelog_section.extract(SAMPLE, "0.1.0")
        self.assertTrue(text.startswith("### Added"))
        self.assertTrue(text.endswith("- A fix."))
        self.assertNotIn("example.test", text)

    def test_empty_section_is_empty(self) -> None:
        self.assertEqual(changelog_section.extract(SAMPLE, "Unreleased"), "")

    def test_missing_version_is_empty(self) -> None:
        self.assertEqual(changelog_section.extract(SAMPLE, "9.9.9"), "")

    def test_version_is_matched_exactly(self) -> None:
        self.assertEqual(changelog_section.extract(SAMPLE, "0.2.0"), "")


class MainTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = Path(tmp.name) / "CHANGELOG.md"
        self.path.write_text(SAMPLE, encoding="utf-8")

    def run_main(self, *argv: str) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = changelog_section.main(list(argv))
        return code, out.getvalue(), err.getvalue()

    def test_prints_section(self) -> None:
        code, out, _ = self.run_main("0.1.0", "--file", str(self.path))
        self.assertEqual(code, 0)
        self.assertIn("- First thing.", out)

    def test_missing_section_fails(self) -> None:
        code, out, err = self.run_main("9.9.9", "--file", str(self.path))
        self.assertEqual((code, out), (1, ""))
        self.assertIn("9.9.9", err)

    def test_unreadable_file_fails(self) -> None:
        code, _, _ = self.run_main("0.1.0", "--file", str(self.path.with_name("nope.md")))
        self.assertEqual(code, 2)

    def test_repository_changelog_has_current_version(self) -> None:
        code, out, _ = self.run_main(__version__, "--file", str(ROOT / "CHANGELOG.md"))
        self.assertEqual(code, 0, "CHANGELOG.md needs a section for the package version")
        self.assertTrue(out.strip())
