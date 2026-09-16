#!/usr/bin/env python3
"""Stamp the CHANGELOG's `## [Unreleased]` section with the version being
released and open a fresh empty `[Unreleased]` above it.

    python3 .github/scripts/stamp_changelog.py --version vX.Y.Z --date YYYY-MM-DD [--dry-run] CHANGELOG.md

Exit codes: 0 stamped (or nothing to stamp / already stamped — both logged),
2 usage or malformed CHANGELOG (no `## [Unreleased]` header). Idempotent:
a CHANGELOG that already has `## [X.Y.Z]` is left untouched. Stdlib only.
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

UNRELEASED_RE = re.compile(r"^## \[Unreleased\][^\n]*\n", re.M)
NEXT_SECTION_RE = re.compile(r"^## \[", re.M)
EMPTY_MARKER = "_Nothing yet._"


def stamp(text: str, version: str, date: str) -> tuple[str, str]:
    """Return (new_text, status) where status ∈ {stamped, already, empty}."""
    ver = version[1:] if version.startswith("v") else version
    if re.search(rf"^## \[{re.escape(ver)}\]", text, re.M):
        return text, "already"
    m = UNRELEASED_RE.search(text)
    if not m:
        raise ValueError("CHANGELOG has no `## [Unreleased]` header")
    body_start = m.end()
    nxt = NEXT_SECTION_RE.search(text, body_start)
    body_end = nxt.start() if nxt else len(text)
    body = text[body_start:body_end]
    if not body.replace(EMPTY_MARKER, "").strip():
        return text, "empty"
    new = (
        text[:m.start()]
        + f"## [Unreleased]\n\n{EMPTY_MARKER}\n\n## [{ver}] — {date}\n"
        + body.rstrip("\n") + "\n\n"
        + text[body_end:]
    )
    return new, "stamped"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--version", required=True)
    ap.add_argument("--date", required=True)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("path")
    args = ap.parse_args()
    if not re.fullmatch(r"v?\d+\.\d+\.\d+", args.version) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", args.date):
        print("usage: --version vX.Y.Z --date YYYY-MM-DD", file=sys.stderr)
        return 2
    p = Path(args.path)
    try:
        new, status = stamp(p.read_text(encoding="utf-8"), args.version, args.date)
    except ValueError as e:
        print(f"::error::{e}", file=sys.stderr)
        return 2
    if status == "already":
        print(f"CHANGELOG already has a section for {args.version}; nothing to stamp.")
        return 0
    if status == "empty":
        print(f"::notice::CHANGELOG [Unreleased] is empty; nothing to stamp for {args.version}.")
        return 0
    if args.dry_run:
        old_lines = p.read_text(encoding="utf-8").splitlines(); new_lines = new.splitlines()
        first = next((i for i, (a, b) in enumerate(zip(old_lines, new_lines)) if a != b), min(len(old_lines), len(new_lines)))
        print(f"dry run: would stamp [Unreleased] as [{args.version.lstrip('v')}] — {args.date}; first changed line {first + 1}:")
        for line in new_lines[first:first + 6]:
            print("  + " + line)
        return 0
    p.write_text(new, encoding="utf-8")
    print(f"Stamped CHANGELOG: [Unreleased] → [{args.version.lstrip('v')}] — {args.date}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
