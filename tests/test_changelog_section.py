"""Tests for .github/scripts/changelog_section.py (release notes from the CHANGELOG)."""

from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT: Path = Path(__file__).resolve().parent.parent
SCRIPT: Path = REPO_ROOT / ".github" / "scripts" / "changelog_section.py"

SAMPLE: str = """# Changelog

## [Unreleased]

_Nothing yet._

## [1.2.0] — 2026-10-01

### Added

- Thing one.

## [1.1.0] — 2026-09-01

- Older thing.
"""


class ChangelogSectionTest(unittest.TestCase):
    def _run(self, version: str, text: str = SAMPLE) -> subprocess.CompletedProcess[str]:
        with tempfile.TemporaryDirectory() as tmp:
            path: Path = Path(tmp) / "CHANGELOG.md"
            path.write_text(text, encoding="utf-8")
            return subprocess.run(
                [sys.executable, str(SCRIPT), "--version", version, str(path)],
                capture_output=True, text=True, check=False,
            )

    def test_prints_the_tagged_section_only(self) -> None:
        result = self._run("v1.2.0")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "### Added\n\n- Thing one.\n")

    def test_last_section_runs_to_end_of_file(self) -> None:
        result = self._run("1.1.0")
        self.assertEqual(result.stdout, "- Older thing.\n")

    def test_missing_version_exits_1(self) -> None:
        self.assertEqual(self._run("v9.9.9").returncode, 1)

    def test_bad_version_is_usage_error(self) -> None:
        self.assertEqual(self._run("latest").returncode, 2)

    def test_real_changelog_has_current_release(self) -> None:
        result = subprocess.run(
            [sys.executable, str(SCRIPT), "--version", "v3.2.3", str(REPO_ROOT / "CHANGELOG.md")],
            capture_output=True, text=True, check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(result.stdout.strip())


if __name__ == "__main__":
    unittest.main()
