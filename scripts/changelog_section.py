#!/usr/bin/env python3
"""Print the CHANGELOG.md section of one version (Keep a Changelog layout).

Used by the release workflow for the release notes and as a gate: it exits
non-zero when the version has no section or the section is empty.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

_HEADING = re.compile(r"^## \[(?P<name>[^\]]+)\]")
_LINK_REFERENCE = re.compile(r"^\[[^\]]+\]:\s")


def extract(text: str, version: str) -> str:
    """Return the body of the `## [version]` section, without its heading."""
    body: list[str] = []
    inside = False
    for line in text.splitlines():
        match = _HEADING.match(line)
        if match:
            if inside:
                break
            inside = match["name"] == version
            continue
        if inside:
            if _LINK_REFERENCE.match(line):
                break
            body.append(line)
    return "\n".join(body).strip()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("version", help="version without the leading v, e.g. 0.1.0")
    parser.add_argument("--file", default="CHANGELOG.md", type=Path)
    args = parser.parse_args(argv)
    try:
        text = args.file.read_text(encoding="utf-8")
    except OSError as exc:
        print(f"cannot read {args.file}: {exc}", file=sys.stderr)
        return 2
    section = extract(text, args.version)
    if not section:
        print(f"no non-empty section for {args.version} in {args.file}", file=sys.stderr)
        return 1
    print(section)
    return 0


if __name__ == "__main__":
    sys.exit(main())
