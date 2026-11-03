"""The `RETIRED` file at the root of the repository and how `updater.sh` reads it.

A tag cannot be edited, so a release is withdrawn by a line in `RETIRED`: `vX.Y.Z
YYYY-MM-DD reason` (docs/releasing.md). The device reads it with `git show` and splits it
into words; nothing in it is ever run. These tests keep the file in the repository parseable
and check that the script and an independent reading of the format agree.
"""

import datetime
import re
import shutil
import subprocess
import unittest
from pathlib import Path

from warpzone.selfupdate import tag_key

ROOT = Path(__file__).resolve().parent.parent
RETIRED = ROOT / "RETIRED"
UPDATER = ROOT / "warpzone" / "data" / "updater.sh"
SHELLS = [s for s in ("sh", "dash", "bash") if shutil.which(s)]

_LINE = re.compile(r"(\S+)[ \t]+(\d{4}-\d{2}-\d{2})[ \t]+(\S.*)", re.ASCII)


def parse_line(line: str) -> str | None:
    """The tag a RETIRED line retires, or None for a comment, a blank or a bad line.
    Returns "" for a line that is not in the format (so a test can tell it from a comment)."""
    text = line.strip()
    if not text or text.startswith("#"):
        return None
    match = _LINE.fullmatch(text)
    if match is None or tag_key(match.group(1)) is None:
        return ""
    try:
        datetime.date.fromisoformat(match.group(2))
    except ValueError:
        return ""
    return match.group(1)


def script_reads(text: str, shell: str) -> tuple[list[str], list[int]]:
    """What `updater.sh retired` makes of `text`: the tags it retires, the lines it refuses."""
    proc = subprocess.run(
        [shell, str(UPDATER), "retired"], input=text, capture_output=True, text=True, check=False
    )
    assert proc.returncode == 0, proc.stderr + proc.stdout
    fields = dict(line.split(":", 1) for line in proc.stdout.splitlines() if ":" in line)
    return fields["tags"].split(), [int(n) for n in fields["malformed"].split()]


class RetiredFileTests(unittest.TestCase):
    def test_the_file_exists_and_every_line_is_a_comment_or_an_entry(self) -> None:
        self.assertTrue(RETIRED.is_file(), "RETIRED is missing from the root of the repository")
        text = RETIRED.read_text()
        self.assertTrue(text.startswith("#"), "it starts with a comment that says what it is")
        seen: list[str] = []
        for number, line in enumerate(text.splitlines(), start=1):
            tag = parse_line(line)
            self.assertNotEqual(tag, "", f"RETIRED line {number} is not 'vX.Y.Z YYYY-MM-DD reason'")
            if tag:
                self.assertNotIn(tag, seen, f"{tag} is listed twice")
                seen.append(tag)

    def test_the_script_reads_the_file_in_the_repository_the_same_way(self) -> None:
        text = RETIRED.read_text()
        expected = [t for t in map(parse_line, text.splitlines()) if t]
        for shell in SHELLS:
            with self.subTest(shell=shell):
                self.assertEqual(script_reads(text, shell), (expected, []))

    def test_the_script_and_the_format_agree_on_what_a_line_is(self) -> None:
        good = [
            "v0.2.0 2026-11-03 broke the settings merge",
            "v0.3.0rc1 2026-01-31 a candidate",
            "v10.20.30\t2026-02-01\ttabs",
            "  v1.0.0 2026-12-31 indented",
        ]
        bad = [
            "v0.2.0",
            "v0.2.0 2026-11-03",
            "0.2.0 2026-11-03 no v",
            "v0.2 2026-11-03 two numbers",
            "v0.2.0-rc1 2026-11-03 not the PEP 440 spelling",
            "v0.2.0rc 2026-11-03 no number",
            "v0.2.0 2026-13-03 month",
            "v0.2.0 2026-11-32 day",
            "v0.2.0 03/11/2026 shape",
            "v0.2.0 2026-11-3 shape",
            "garbage",
            "v0.2.0,v0.3.0 2026-11-03 two tags",
        ]
        comments = ["# a comment", "", "   ", "#v0.9.0 2026-11-03 commented out"]
        lines = comments + good + bad
        expected_tags = [parse_line(line) for line in good]
        self.assertTrue(all(expected_tags))
        self.assertTrue(all(parse_line(line) == "" for line in bad), bad)
        malformed = [n for n, line in enumerate(lines, start=1) if line in bad]
        for shell in SHELLS:
            with self.subTest(shell=shell):
                text = "\r\n".join(lines) + "\r\n"  # a CRLF file reads the same
                self.assertEqual(script_reads(text, shell), (expected_tags, malformed))


if __name__ == "__main__":
    unittest.main()
