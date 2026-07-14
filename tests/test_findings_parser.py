#!/usr/bin/env python3
"""Unit tests for the agent-runner findings.json parser + validator.

Strict schema enforcement is the load-bearing invariant of the `AgentRunnerProvider`
contract — a bad findings file from a CLI must NOT silently produce a broken
review. These tests exercise the happy path and every error path documented in
the parser's docstring.
"""

from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any

_ROOT: Path = Path(__file__).resolve().parent.parent
_SPEC = importlib.util.spec_from_file_location(
    "reviewer", _ROOT / "scripts" / "reviewer.py"
)
assert _SPEC is not None and _SPEC.loader is not None
reviewer = importlib.util.module_from_spec(_SPEC)
sys.modules["reviewer"] = reviewer
_SPEC.loader.exec_module(reviewer)


def _write_json(tmpdir: Path, content: Any) -> Path:
    """Serialise `content` (or a raw string) to a findings.json file."""
    path = tmpdir / "findings.json"
    if isinstance(content, str):
        path.write_text(content, encoding="utf-8")
    else:
        path.write_text(json.dumps(content), encoding="utf-8")
    return path


class ParseFindingsFileHappyPath(unittest.TestCase):
    def test_canonical_three_findings(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            path = _write_json(
                tmp,
                {
                    "summary": "## Review\n\nThree issues found.",
                    "findings": [
                        {
                            "path": "src/a.py",
                            "line": 12,
                            "body": "critical bug: missing null check",
                            "severity": "critical",
                        },
                        {
                            "path": "src/b.py",
                            "line": 8,
                            "start_line": 5,
                            "body": "perf: O(n^2) here",
                            "severity": "warning",
                            "side": "RIGHT",
                        },
                        {
                            "path": "README.md",
                            "line": 42,
                            "body": "typo: `receieve` -> `receive`",
                            "severity": "info",
                        },
                    ],
                },
            )
            result = reviewer.parse_findings_file(path)
            self.assertEqual(result.summary, "## Review\n\nThree issues found.")
            self.assertEqual(len(result.findings), 3)
            self.assertEqual(result.overall_severity, reviewer.SEVERITY_CRITICAL)
            self.assertEqual(result.findings[1].start_line, 5)

    def test_empty_findings_returns_severity_none(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            path = _write_json(
                Path(td),
                {"summary": "All clean.", "findings": []},
            )
            result = reviewer.parse_findings_file(path)
            self.assertEqual(result.findings, [])
            self.assertEqual(result.overall_severity, reviewer.SEVERITY_NONE)
            self.assertEqual(result.summary, "All clean.")

    def test_missing_severity_defaults_to_info(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            path = _write_json(
                Path(td),
                {
                    "findings": [
                        {"path": "a.py", "line": 1, "body": "note"}
                    ]
                },
            )
            result = reviewer.parse_findings_file(path)
            self.assertEqual(result.findings[0].severity, reviewer.SEVERITY_INFO)

    def test_missing_side_defaults_to_right(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            path = _write_json(
                Path(td),
                {"findings": [{"path": "a.py", "line": 1, "body": "note"}]},
            )
            result = reviewer.parse_findings_file(path)
            self.assertEqual(result.findings[0].side, "RIGHT")

    def test_severity_case_insensitive(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            path = _write_json(
                Path(td),
                {
                    "findings": [
                        {"path": "a.py", "line": 1, "body": "x", "severity": "CRITICAL"}
                    ]
                },
            )
            result = reviewer.parse_findings_file(path)
            self.assertEqual(result.findings[0].severity, "critical")

    def test_side_case_normalized_to_upper(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            path = _write_json(
                Path(td),
                {
                    "findings": [
                        {"path": "a.py", "line": 1, "body": "x", "side": "left"}
                    ]
                },
            )
            result = reviewer.parse_findings_file(path)
            self.assertEqual(result.findings[0].side, "LEFT")

    def test_unicode_body_preserved(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            path = _write_json(
                Path(td),
                {
                    "findings": [
                        {
                            "path": "a.py",
                            "line": 1,
                            "body": "\u26a0\ufe0f rename this variable \u2014 the name conflicts with \u4e2d\u6587",
                        }
                    ]
                },
            )
            result = reviewer.parse_findings_file(path)
            self.assertIn("\u26a0\ufe0f", result.findings[0].body)
            self.assertIn("\u4e2d\u6587", result.findings[0].body)

    def test_unknown_keys_ignored(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            path = _write_json(
                Path(td),
                {
                    "summary": "s",
                    "findings": [
                        {
                            "path": "a.py",
                            "line": 1,
                            "body": "x",
                            "vendor_extra": {"anything": "here"},
                            "future_field": 42,
                        }
                    ],
                    "top_level_extra": "ok",
                },
            )
            result = reviewer.parse_findings_file(path)
            self.assertEqual(len(result.findings), 1)


class ParseFindingsFileErrorPaths(unittest.TestCase):
    def test_missing_file_raises_actionable_error(self) -> None:
        path = Path("/tmp/definitely-does-not-exist-aiprr.json")
        with self.assertRaises(FileNotFoundError) as ctx:
            reviewer.parse_findings_file(path)
        self.assertIn("did not write", str(ctx.exception))

    def test_malformed_json_raises_value_error(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            path = _write_json(Path(td), "{ this is not JSON")
            with self.assertRaises(ValueError) as ctx:
                reviewer.parse_findings_file(path)
            self.assertIn("Malformed findings.json", str(ctx.exception))

    def test_malformed_json_can_recover_summary_when_opted_in(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            path = _write_json(
                Path(td),
                (
                    '{\n'
                    '  "summary": "Review summary with `markdown`.",\n'
                    '  "findings": [\n'
                    '    {"path": "a.py", "line": 1, "body": "bad "quote""}\n'
                    "  ]\n"
                    "}\n"
                ),
            )
            result = reviewer.parse_findings_file(
                path, allow_malformed_summary_fallback=True
            )
            self.assertIn("Review summary", result.summary)
            self.assertIn("summary only", result.summary)
            self.assertEqual(result.findings, [])
            self.assertEqual(result.overall_severity, reviewer.SEVERITY_NONE)

    def test_malformed_json_without_summary_still_raises_when_opted_in(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            path = _write_json(Path(td), "{ this is not JSON")
            with self.assertRaises(ValueError):
                reviewer.parse_findings_file(
                    path, allow_malformed_summary_fallback=True
                )

    def test_root_not_object_raises(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            path = _write_json(Path(td), ["not", "an", "object"])
            with self.assertRaises(ValueError) as ctx:
                reviewer.parse_findings_file(path)
            self.assertIn("root must be an object", str(ctx.exception))

    def test_findings_not_list_raises(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            path = _write_json(Path(td), {"findings": {"nope": "object"}})
            with self.assertRaises(ValueError) as ctx:
                reviewer.parse_findings_file(path)
            self.assertIn("'findings' must be a list", str(ctx.exception))

    def test_missing_required_field_raises(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            path = _write_json(
                Path(td),
                {"findings": [{"path": "a.py", "line": 1}]},  # missing body
            )
            with self.assertRaises(ValueError) as ctx:
                reviewer.parse_findings_file(path)
            self.assertIn("missing or invalid required field", str(ctx.exception))

    def test_empty_body_raises(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            path = _write_json(
                Path(td),
                {"findings": [{"path": "a.py", "line": 1, "body": "   "}]},
            )
            with self.assertRaises(ValueError) as ctx:
                reviewer.parse_findings_file(path)
            self.assertIn("body is empty", str(ctx.exception))

    def test_empty_path_raises(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            path = _write_json(
                Path(td),
                {"findings": [{"path": "", "line": 1, "body": "x"}]},
            )
            with self.assertRaises(ValueError) as ctx:
                reviewer.parse_findings_file(path)
            self.assertIn("path is empty", str(ctx.exception))

    def test_invalid_severity_raises(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            path = _write_json(
                Path(td),
                {
                    "findings": [
                        {
                            "path": "a.py",
                            "line": 1,
                            "body": "x",
                            "severity": "blocker",
                        }
                    ]
                },
            )
            with self.assertRaises(ValueError) as ctx:
                reviewer.parse_findings_file(path)
            self.assertIn("severity=", str(ctx.exception))

    def test_invalid_side_raises(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            path = _write_json(
                Path(td),
                {
                    "findings": [
                        {
                            "path": "a.py",
                            "line": 1,
                            "body": "x",
                            "side": "MIDDLE",
                        }
                    ]
                },
            )
            with self.assertRaises(ValueError) as ctx:
                reviewer.parse_findings_file(path)
            self.assertIn("side=", str(ctx.exception))

    def test_non_dict_finding_raises(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            path = _write_json(
                Path(td),
                {"findings": ["not a dict"]},
            )
            with self.assertRaises(ValueError) as ctx:
                reviewer.parse_findings_file(path)
            self.assertIn("must be an object", str(ctx.exception))


class WriteFindingsPromptDirectiveTests(unittest.TestCase):
    def test_appends_directive_to_instructions(self) -> None:
        original = "# Review Instructions\n\nBe thorough."
        directive = reviewer.write_findings_prompt_directive(
            original, Path("/tmp/x/.aiprr/findings.json")
        )
        self.assertTrue(directive.startswith(original))
        self.assertIn("Output contract", directive)
        self.assertIn(".aiprr/findings.json", directive)

    def test_directive_documents_the_schema(self) -> None:
        directive = reviewer.write_findings_prompt_directive(
            "", Path("/tmp/x/findings.json")
        )
        self.assertIn("severity", directive)
        self.assertIn("critical", directive)
        self.assertIn("warning", directive)
        self.assertIn("info", directive)
        self.assertIn("side", directive)
        self.assertIn("RIGHT", directive)
        self.assertIn("LEFT", directive)
        self.assertIn("json.load", directive)


class FindingsContractConstantsTests(unittest.TestCase):
    def test_constants_are_wired(self) -> None:
        self.assertEqual(reviewer.FINDINGS_JSON_REL, ".aiprr/findings.json")
        self.assertEqual(
            set(reviewer.ALLOWED_SEVERITIES),
            {"critical", "warning", "info"},
        )
        self.assertEqual(set(reviewer.ALLOWED_SIDES), {"LEFT", "RIGHT"})
        self.assertGreater(reviewer.CLI_INVOCATION_TIMEOUT, 0)


if __name__ == "__main__":
    unittest.main()
