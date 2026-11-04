"""The "Code layout" section of the design contract lists every module, so it cannot drift.

The section is found by its heading wherever the design pages live, so moving the pages does
not break the check.
"""

import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PACKAGE = ROOT / "warpzone"
DOCS = ROOT / "website" / "src" / "content" / "docs" / "development"
HEADING = "## Code layout"


def layout_entries() -> set[str]:
    """The paths in the code block of the "Code layout" section, relative to `warpzone/`."""
    pages = [p for p in sorted(DOCS.rglob("*.md")) if HEADING in p.read_text(encoding="utf-8")]
    if len(pages) != 1:
        raise AssertionError(f"expected one page with {HEADING!r}, found {pages}")
    text = pages[0].read_text(encoding="utf-8")
    section = text.split(HEADING, 1)[1].split("```", 2)[1]
    entries: set[str] = set()
    for line in section.splitlines()[1:]:
        match = re.match(r"^  (\S+)", line)
        if match:
            entries.add(match.group(1))
        elif line.startswith("templates/"):
            break
    return entries


def package_files() -> set[str]:
    modules = {p.name for p in PACKAGE.glob("*.py")}
    data = {p.relative_to(PACKAGE).as_posix() for p in (PACKAGE / "data").rglob("*") if p.is_file()}
    return modules | data


class CodeLayoutTest(unittest.TestCase):
    def test_every_module_and_data_file_is_listed(self):
        missing = sorted(package_files() - layout_entries())
        self.assertEqual(missing, [], "add these to the Code layout section of the design")

    def test_every_listed_file_exists(self):
        stale = sorted(layout_entries() - package_files())
        self.assertEqual(stale, [], "these are in the Code layout section but not in warpzone/")


if __name__ == "__main__":
    unittest.main()
