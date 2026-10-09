#!/usr/bin/env python3
"""Print one version's section of the CHANGELOG — the release notes body.

    python3 .github/scripts/changelog_section.py --version vX.Y.Z CHANGELOG.md

Prints the lines between `## [X.Y.Z] …` and the next `## [` header (header
excluded, surrounding blank lines trimmed). Exit codes: 0 printed, 1 the
version has no section (or an empty one), 2 usage error. Stdlib only.
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

NEXT_SECTION_RE = re.compile(r"^## \[", re.M)


def section(text: str, version: str) -> str | None:
    """Return the body of `version`'s section, or None when absent or empty."""
    ver = version[1:] if version.startswith("v") else version
    header = re.search(rf"^## \[{re.escape(ver)}\][^\n]*\n", text, re.M)
    if not header:
        return None
    nxt = NEXT_SECTION_RE.search(text, header.end())
    body = text[header.end():nxt.start() if nxt else len(text)].strip("\n")
    return body or None


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--version", required=True)
    ap.add_argument("path")
    args = ap.parse_args()
    if not re.fullmatch(r"v?\d+\.\d+\.\d+(-[0-9A-Za-z.-]+)?", args.version):
        print("usage: --version vX.Y.Z[-pre]", file=sys.stderr)
        return 2
    body = section(Path(args.path).read_text(encoding="utf-8"), args.version)
    if body is None:
        print(f"no CHANGELOG section for {args.version}", file=sys.stderr)
        return 1
    print(body)
    return 0


if __name__ == "__main__":
    sys.exit(main())
