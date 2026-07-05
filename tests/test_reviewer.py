#!/usr/bin/env python3
"""Unit tests for `scripts/reviewer.py`.

Stdlib `unittest` only — no third-party test runner, no install step. Run with:

    python3 -m unittest discover -s tests -v

The runtime under test is stdlib-only by design (see AGENTS.md Rule #2); the
tests honour the same constraint so `python3 -m unittest` works on a vanilla
runner with nothing pre-installed.

Tests cover the pure, deterministic surface of the reviewer: parsing,
redaction, truncation, severity/strictness logic, path sandboxing, the tool
handlers, the tracking-comment renderers, output writing, provider
construction, and the conversation-pruning invariant of the agentic loop.
Network/GitHub-API paths are not exercised here (they're covered by the
dogfooding self-review workflow).
"""

from __future__ import annotations

import importlib.util
import os
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Import the module under test from scripts/reviewer.py without requiring it to
# be installed or on PYTHONPATH.
# ---------------------------------------------------------------------------
_ROOT: Path = Path(__file__).resolve().parent.parent
_SPEC = importlib.util.spec_from_file_location(
    "reviewer", _ROOT / "scripts" / "reviewer.py"
)
assert _SPEC is not None and _SPEC.loader is not None
reviewer = importlib.util.module_from_spec(_SPEC)
# Register before exec so `@dataclass` (which looks the module up in
# sys.modules via cls.__module__) resolves correctly on Python 3.12+.
sys.modules["reviewer"] = reviewer
_SPEC.loader.exec_module(reviewer)


class ParseBoolTests(unittest.TestCase):
    def test_truthy_values(self) -> None:
        for raw in ("1", "true", "TRUE", "Yes", "on", " true "):
            self.assertTrue(reviewer.parse_bool(raw), raw)

    def test_falsy_values(self) -> None:
        for raw in ("0", "false", "no", "off", "nope"):
            self.assertFalse(reviewer.parse_bool(raw), raw)

    def test_empty_uses_default(self) -> None:
        self.assertTrue(reviewer.parse_bool("", default=True))
        self.assertFalse(reviewer.parse_bool("", default=False))


class RedactForLogTests(unittest.TestCase):
    def test_masks_sensitive_keys(self) -> None:
        masked = reviewer.redact_for_log(
            {"api_key": "sk-secret", "token": "ghp_x", "path": "src/a.py"}
        )
        self.assertEqual(masked["api_key"], "***")
        self.assertEqual(masked["token"], "***")
        self.assertEqual(masked["path"], "src/a.py")

    def test_case_insensitive_substring(self) -> None:
        masked = reviewer.redact_for_log({"AUTH_Header": "v", "PassWord": "v"})
        self.assertEqual(masked["AUTH_Header"], "***")
        self.assertEqual(masked["PassWord"], "***")


class TruncateForToolTests(unittest.TestCase):
    def test_short_text_unchanged(self) -> None:
        self.assertEqual(reviewer.truncate_for_tool("hi", label="grep"), "hi")

    def test_long_text_truncated_with_notice(self) -> None:
        big = "x" * (reviewer.MAX_TOOL_OUTPUT_BYTES + 100)
        out = reviewer.truncate_for_tool(big, label="grep")
        self.assertLess(len(out.encode("utf-8")), len(big.encode("utf-8")))
        self.assertIn("output truncated", out)
        self.assertIn("grep", out)


class OverallSeverityTests(unittest.TestCase):
    def test_empty_is_none(self) -> None:
        self.assertEqual(reviewer.overall_severity([]), reviewer.SEVERITY_NONE)

    def test_returns_highest(self) -> None:
        self.assertEqual(
            reviewer.overall_severity(["info", "critical", "warning"]),
            reviewer.SEVERITY_CRITICAL,
        )
        self.assertEqual(
            reviewer.overall_severity(["info", "warning"]),
            reviewer.SEVERITY_WARNING,
        )


class EvaluateStrictnessTests(unittest.TestCase):
    def test_lenient_never_blocks(self) -> None:
        blocked, _ = reviewer.evaluate_strictness(
            reviewer.SEVERITY_CRITICAL, reviewer.STRICTNESS_LENIENT
        )
        self.assertFalse(blocked)

    def test_block_on_critical(self) -> None:
        self.assertTrue(
            reviewer.evaluate_strictness(
                reviewer.SEVERITY_CRITICAL, reviewer.STRICTNESS_BLOCK_CRITICAL
            )[0]
        )
        self.assertFalse(
            reviewer.evaluate_strictness(
                reviewer.SEVERITY_WARNING, reviewer.STRICTNESS_BLOCK_CRITICAL
            )[0]
        )

    def test_block_on_warning(self) -> None:
        self.assertTrue(
            reviewer.evaluate_strictness(
                reviewer.SEVERITY_WARNING, reviewer.STRICTNESS_BLOCK_WARNING
            )[0]
        )
        self.assertTrue(
            reviewer.evaluate_strictness(
                reviewer.SEVERITY_CRITICAL, reviewer.STRICTNESS_BLOCK_WARNING
            )[0]
        )
        self.assertFalse(
            reviewer.evaluate_strictness(
                reviewer.SEVERITY_INFO, reviewer.STRICTNESS_BLOCK_WARNING
            )[0]
        )

    def test_unknown_strictness_treated_lenient(self) -> None:
        blocked, reason = reviewer.evaluate_strictness(
            reviewer.SEVERITY_CRITICAL, "bogus"
        )
        self.assertFalse(blocked)
        self.assertIn("lenient", reason)


class SafeRepoPathTests(unittest.TestCase):
    def setUp(self) -> None:
        self._prev_cwd = os.getcwd()
        self._tmp = tempfile.TemporaryDirectory()
        os.chdir(self._tmp.name)

    def tearDown(self) -> None:
        os.chdir(self._prev_cwd)
        self._tmp.cleanup()

    def test_in_workspace_ok(self) -> None:
        p = reviewer.safe_repo_path("sub/file.py")
        self.assertTrue(str(p).startswith(str(Path.cwd().resolve())))

    def test_parent_traversal_rejected(self) -> None:
        with self.assertRaises(ValueError):
            reviewer.safe_repo_path("../../etc/passwd")

    def test_sibling_prefix_not_bypassed(self) -> None:
        # `/x/repo` vs `/x/repo_evil` — string-prefix must not pass.
        with self.assertRaises(ValueError):
            reviewer.safe_repo_path("../" + Path.cwd().name + "_evil/x")


class ToolReadFileTests(unittest.TestCase):
    def setUp(self) -> None:
        self._prev_cwd = os.getcwd()
        self._tmp = tempfile.TemporaryDirectory()
        os.chdir(self._tmp.name)
        Path("a.txt").write_text(
            "line1\nline2\nline3\nline4\n", encoding="utf-8"
        )

    def tearDown(self) -> None:
        os.chdir(self._prev_cwd)
        self._tmp.cleanup()

    def test_reads_and_numbers_lines(self) -> None:
        out = reviewer.tool_read_file({"path": "a.txt"})
        self.assertIn("line1", out)
        self.assertIn("line4", out)
        self.assertIn("of 4", out)

    def test_offset_and_limit(self) -> None:
        out = reviewer.tool_read_file({"path": "a.txt", "offset": 2, "limit": 2})
        self.assertIn("line2", out)
        self.assertIn("line3", out)
        self.assertNotIn("line4", out)

    def test_missing_file(self) -> None:
        self.assertIn("not found", reviewer.tool_read_file({"path": "nope.txt"}))

    def test_escape_is_blocked(self) -> None:
        out = reviewer.tool_read_file({"path": "../../etc/hosts"})
        self.assertIn("escapes the workspace", out)


class ToolPostInlineCommentTests(unittest.TestCase):
    def test_queues_and_normalizes_severity(self) -> None:
        state = reviewer.ReviewState(max_inline_comments=3)
        msg = reviewer.tool_post_inline_comment(
            {"path": "a.py", "line": 10, "body": "x", "severity": "CRITICAL"},
            state,
        )
        self.assertIn("Queued", msg)
        self.assertEqual(state.severities, ["critical"])
        self.assertEqual(state.inline_comments[0]["line"], 10)
        self.assertEqual(state.inline_comments[0]["side"], "RIGHT")

    def test_invalid_severity_defaults_info(self) -> None:
        state = reviewer.ReviewState()
        reviewer.tool_post_inline_comment(
            {"path": "a.py", "line": 1, "body": "x", "severity": "bogus"}, state
        )
        self.assertEqual(state.severities, [reviewer.SEVERITY_INFO])

    def test_multiline_sets_start_fields(self) -> None:
        state = reviewer.ReviewState()
        reviewer.tool_post_inline_comment(
            {"path": "a.py", "line": 10, "start_line": 8, "body": "x"}, state
        )
        self.assertEqual(state.inline_comments[0]["start_line"], 8)
        self.assertEqual(state.inline_comments[0]["start_side"], "RIGHT")

    def test_cap_enforced(self) -> None:
        state = reviewer.ReviewState(max_inline_comments=1)
        reviewer.tool_post_inline_comment(
            {"path": "a.py", "line": 1, "body": "x"}, state
        )
        msg = reviewer.tool_post_inline_comment(
            {"path": "a.py", "line": 2, "body": "y"}, state
        )
        self.assertIn("cap reached", msg)
        self.assertEqual(len(state.inline_comments), 1)


class ToolSubmitReviewTests(unittest.TestCase):
    def test_records_summary(self) -> None:
        state = reviewer.ReviewState()
        reviewer.tool_submit_review({"summary": "All good"}, state)
        self.assertEqual(state.final_summary, "All good")

    def test_idempotent_second_call_errors(self) -> None:
        state = reviewer.ReviewState()
        reviewer.tool_submit_review({"summary": "first"}, state)
        msg = reviewer.tool_submit_review({"summary": "second"}, state)
        self.assertIn("already called", msg)
        self.assertEqual(state.final_summary, "first")


class StateToReviewResultTests(unittest.TestCase):
    """Adapter: ReviewState (chat-completions path) → ReviewResult (unified)."""

    def test_happy_path_maps_all_fields(self) -> None:
        state = reviewer.ReviewState()
        reviewer.tool_post_inline_comment(
            {
                "path": "a.py",
                "line": 12,
                "body": "critical bug",
                "severity": "critical",
            },
            state,
        )
        reviewer.tool_post_inline_comment(
            {
                "path": "b.py",
                "line": 8,
                "start_line": 5,
                "body": "range issue",
                "severity": "warning",
                "side": "LEFT",
            },
            state,
        )
        state.final_summary = "## Summary\n\nTwo issues found."

        result = reviewer.state_to_review_result(state)

        self.assertEqual(result.summary, "## Summary\n\nTwo issues found.")
        self.assertEqual(len(result.findings), 2)
        self.assertEqual(result.overall_severity, reviewer.SEVERITY_CRITICAL)

        first = result.findings[0]
        self.assertEqual(first.path, "a.py")
        self.assertEqual(first.line, 12)
        self.assertEqual(first.body, "critical bug")
        self.assertEqual(first.severity, "critical")
        self.assertIsNone(first.start_line)

        second = result.findings[1]
        self.assertEqual(second.path, "b.py")
        self.assertEqual(second.start_line, 5)
        self.assertEqual(second.side, "LEFT")
        self.assertEqual(second.severity, "warning")

    def test_empty_state_yields_empty_result(self) -> None:
        state = reviewer.ReviewState()

        result = reviewer.state_to_review_result(state)

        self.assertEqual(result.summary, "")
        self.assertEqual(result.findings, [])
        self.assertEqual(result.overall_severity, reviewer.SEVERITY_NONE)


class FindingsToGhInlineCommentsTests(unittest.TestCase):
    """Encoder: list[Finding] → GitHub Reviews API inline shape."""

    def test_single_line_finding(self) -> None:
        findings = [
            reviewer.Finding(
                path="a.py",
                line=42,
                body="typo",
                severity="info",
                side="RIGHT",
            )
        ]
        out = reviewer.findings_to_gh_inline_comments(findings)
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["path"], "a.py")
        self.assertEqual(out[0]["line"], 42)
        self.assertEqual(out[0]["side"], "RIGHT")
        self.assertNotIn("start_line", out[0])

    def test_multiline_finding_adds_start_side(self) -> None:
        findings = [
            reviewer.Finding(
                path="a.py",
                line=10,
                body="range",
                start_line=8,
                side="LEFT",
            )
        ]
        out = reviewer.findings_to_gh_inline_comments(findings)
        self.assertEqual(out[0]["start_line"], 8)
        self.assertEqual(out[0]["start_side"], "LEFT")

    def test_empty_findings_yields_empty_list(self) -> None:
        self.assertEqual(reviewer.findings_to_gh_inline_comments([]), [])


class AgentRunnerProviderContractTests(unittest.TestCase):
    """The abstract base class exposes the expected interface."""

    def test_install_raises_not_implemented(self) -> None:
        with self.assertRaises(NotImplementedError):
            reviewer.AgentRunnerProvider().install()

    def test_run_review_raises_not_implemented(self) -> None:
        provider = reviewer.AgentRunnerProvider()
        with self.assertRaises(NotImplementedError):
            provider.run_review(
                pr_context=reviewer.PRContext(
                    title="t",
                    author="a",
                    head_ref="h",
                    base_ref="b",
                    state="open",
                    additions=0,
                    deletions=0,
                    commits=0,
                    body="",
                ),
                review_instructions="",
                workspace=Path("."),
                output_dir=Path("."),
            )


class ExecuteToolTests(unittest.TestCase):
    def test_unknown_tool(self) -> None:
        state = reviewer.ReviewState()
        self.assertIn(
            "unknown tool", reviewer.execute_tool("frobnicate", {}, state)
        )

    def test_exception_is_surfaced_not_raised(self) -> None:
        state = reviewer.ReviewState()
        # Missing required "path" key -> KeyError inside handler, surfaced.
        out = reviewer.execute_tool("read_file", {}, state)
        self.assertIn("raised", out)


class TrackingRenderTests(unittest.TestCase):
    def test_working_includes_collapse_note_when_enabled(self) -> None:
        body = reviewer.render_tracking_body_working(
            "abc1234def", collapse_previous=True
        )
        self.assertIn(reviewer.REVIEW_MARKER, body)
        self.assertIn("collapsed as outdated", body)

    def test_working_omits_collapse_note_when_disabled(self) -> None:
        body = reviewer.render_tracking_body_working(
            "abc1234def", collapse_previous=False
        )
        self.assertNotIn("collapsed", body)

    def test_done_blocked_shows_block_emoji(self) -> None:
        body = reviewer.render_tracking_body_done(
            head_sha="abc1234def",
            review_url="https://example/r",
            inline_attached=2,
            inline_dropped=0,
            severity="critical",
            blocked=True,
            block_reason="block-on-critical fired",
        )
        self.assertIn("🚫", body)
        self.assertIn("2 inline comment(s) attached", body)

    def test_done_reports_dropped(self) -> None:
        body = reviewer.render_tracking_body_done(
            head_sha="abc1234def",
            review_url="https://example/r",
            inline_attached=1,
            inline_dropped=3,
            severity="info",
            blocked=False,
            block_reason="ok",
        )
        self.assertIn("3 dropped", body)
        self.assertIn("422", body)


class OutputWritingTests(unittest.TestCase):
    def setUp(self) -> None:
        self._fd, self._path = tempfile.mkstemp()
        os.close(self._fd)
        self._prev = os.environ.get("GITHUB_OUTPUT")
        os.environ["GITHUB_OUTPUT"] = self._path

    def tearDown(self) -> None:
        if self._prev is None:
            os.environ.pop("GITHUB_OUTPUT", None)
        else:
            os.environ["GITHUB_OUTPUT"] = self._prev
        os.unlink(self._path)

    def _read(self) -> str:
        return Path(self._path).read_text(encoding="utf-8")

    def test_scalar_written(self) -> None:
        reviewer.write_action_output("blocked", "false")
        self.assertIn("blocked=false", self._read())

    def test_multiline_uses_heredoc(self) -> None:
        reviewer.write_action_output("summary", "a\nb")
        content = self._read()
        self.assertIn("summary<<", content)
        self.assertIn("a\nb", content)

    def test_write_all_outputs_emits_six(self) -> None:
        reviewer.write_all_outputs(
            skipped=False,
            severity="warning",
            inline_attached=4,
            inline_dropped=1,
            blocked=True,
            review_url="https://x/r",
        )
        content = self._read()
        for key in (
            "skipped=false",
            "severity=warning",
            "inline-attached=4",
            "inline-dropped=1",
            "blocked=true",
            "review-url=https://x/r",
        ):
            self.assertIn(key, content)

    def test_write_all_outputs_defaults_for_failure(self) -> None:
        reviewer.write_all_outputs(skipped=False)
        content = self._read()
        self.assertIn("severity=none", content)
        self.assertIn("inline-attached=0", content)
        self.assertIn("blocked=false", content)
        self.assertIn("review-url=\n", content + "\n")


class BuildProviderTests(unittest.TestCase):
    def test_anthropic(self) -> None:
        p = reviewer.build_provider("anthropic", api_key="k", model="m")
        self.assertIsInstance(p, reviewer.AnthropicProvider)

    def test_unknown_provider_raises(self) -> None:
        with self.assertRaises(ValueError):
            reviewer.build_provider("openai", api_key="k", model="m")


class ToolsSchemaTests(unittest.TestCase):
    def test_all_five_tools_present(self) -> None:
        names = {t["name"] for t in reviewer.tools_schema(10)}
        self.assertEqual(
            names,
            {
                "read_file",
                "grep",
                "glob",
                "post_inline_comment",
                "submit_review",
            },
        )

    def test_cap_referenced_in_description(self) -> None:
        schema = reviewer.tools_schema(7)
        post = next(
            t for t in schema if t["name"] == "post_inline_comment"
        )
        self.assertIn("7", post["description"])


class FakeProvider(reviewer.Provider):
    """Provider stub that drives the loop deterministically.

    Emits N tool-call turns (each a no-op read_file) then a final text turn,
    so we can assert the conversation-pruning invariant without any network.
    """

    def __init__(self, *, tool_turns: int) -> None:
        self._tool_turns = tool_turns
        self.calls = 0

    def complete(
        self,
        *,
        system_prompt: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
    ) -> dict[str, Any]:
        self.calls += 1
        if self.calls <= self._tool_turns:
            return {
                "stop_reason": "tool_use",
                "content": [
                    {
                        "type": "tool_use",
                        "id": f"call_{self.calls}",
                        "name": "grep",
                        "input": {"pattern": "x"},
                    }
                ],
            }
        return {
            "stop_reason": "end_turn",
            "content": [{"type": "text", "text": "done"}],
        }


class DriveReviewPruningTests(unittest.TestCase):
    def test_history_is_bounded_and_alternates(self) -> None:
        provider = FakeProvider(tool_turns=40)
        messages: list[dict[str, Any]] = [
            {"role": "user", "content": "seed"}
        ]
        state = reviewer.ReviewState()
        reviewer.drive_review(
            provider=provider,
            system_prompt="sys",
            messages=messages,
            tools=reviewer.tools_schema(10),
            state=state,
            max_turns=40,
        )
        # Seed message is preserved at index 0.
        self.assertEqual(messages[0]["role"], "user")
        # History stays bounded by the retention setting (seed + pairs).
        cap = 1 + 2 * reviewer.MAX_CONVERSATION_TURNS_RETAINED + 2
        self.assertLessEqual(len(messages), cap)
        # Roles must strictly alternate user/assistant/user/... so the
        # Anthropic API never sees an orphaned tool_result.
        for i in range(1, len(messages)):
            self.assertNotEqual(
                messages[i]["role"],
                messages[i - 1]["role"],
                f"non-alternating roles at index {i}",
            )

    def test_stops_on_submit_review(self) -> None:
        class SubmittingProvider(reviewer.Provider):
            def complete(
                self,
                *,
                system_prompt: str,
                messages: list[dict[str, Any]],
                tools: list[dict[str, Any]],
            ) -> dict[str, Any]:
                return {
                    "stop_reason": "tool_use",
                    "content": [
                        {
                            "type": "tool_use",
                            "id": "c1",
                            "name": "submit_review",
                            "input": {"summary": "ok"},
                        }
                    ],
                }

        messages: list[dict[str, Any]] = [{"role": "user", "content": "seed"}]
        state = reviewer.ReviewState()
        reviewer.drive_review(
            provider=SubmittingProvider(),
            system_prompt="sys",
            messages=messages,
            tools=reviewer.tools_schema(10),
            state=state,
            max_turns=30,
        )
        self.assertEqual(state.final_summary, "ok")


if __name__ == "__main__":
    unittest.main()
