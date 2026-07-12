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


class CountLabelEventsTests(unittest.TestCase):
    """`count_label_events` paginates and filters correctly."""

    def _monkeypatch_gh_request(
        self, responses: list[list[dict[str, Any]]]
    ) -> None:
        """Replace `gh_request` with a fake that returns queued responses.

        Each call pops the next response off the queue. When the queue
        is exhausted, returns an empty list (simulates "no more pages").
        """
        self._responses = responses
        self._call_count = 0

        def fake_gh_request(method: str, path: str, **kwargs: Any) -> Any:
            self._call_count += 1
            if not self._responses:
                return []
            return self._responses.pop(0)

        self._orig = reviewer.gh_request
        reviewer.gh_request = fake_gh_request  # type: ignore[assignment]

    def _restore(self) -> None:
        reviewer.gh_request = self._orig  # type: ignore[assignment]

    def test_counts_matching_labeled_events(self) -> None:
        self._monkeypatch_gh_request(
            [
                [
                    {"event": "labeled", "label": {"name": "ready"}},
                    {"event": "labeled", "label": {"name": "other"}},
                    {"event": "closed"},
                    {"event": "labeled", "label": {"name": "ready"}},
                ]
            ]
        )
        try:
            n = reviewer.count_label_events(
                token="t", repo="o/r", pr_number=1, label="ready"
            )
            self.assertEqual(n, 2)
        finally:
            self._restore()

    def test_empty_label_returns_zero_without_api_call(self) -> None:
        self._monkeypatch_gh_request([])
        try:
            n = reviewer.count_label_events(
                token="t", repo="o/r", pr_number=1, label=""
            )
            self.assertEqual(n, 0)
            self.assertEqual(self._call_count, 0)
        finally:
            self._restore()

    def test_paginates_until_less_than_full_page(self) -> None:
        # First page: 100 items → paginate. Second page: 2 items → stop.
        page1 = [
            {"event": "labeled", "label": {"name": "ready"}}
        ] * 100
        page2 = [
            {"event": "labeled", "label": {"name": "ready"}},
            {"event": "closed"},
        ]
        self._monkeypatch_gh_request([page1, page2])
        try:
            n = reviewer.count_label_events(
                token="t", repo="o/r", pr_number=1, label="ready"
            )
            self.assertEqual(n, 101)
            self.assertEqual(self._call_count, 2)
        finally:
            self._restore()


class GhPatchPrBodySignatureTests(unittest.TestCase):
    """Regression: `gh_patch_pr_body` uses positional method+path (matches
    `gh_request`'s actual signature). If someone reverts to keyword args,
    this will catch it."""

    def test_calls_patch_with_new_body(self) -> None:
        captured: dict[str, Any] = {}

        def fake_gh_request(method: str, path: str, **kwargs: Any) -> Any:
            captured["method"] = method
            captured["path"] = path
            captured["body"] = kwargs.get("body")
            return {}

        orig = reviewer.gh_request
        reviewer.gh_request = fake_gh_request  # type: ignore[assignment]
        try:
            reviewer.gh_patch_pr_body(
                token="t", repo="o/r", pr_number=42, new_body="hello"
            )
        finally:
            reviewer.gh_request = orig  # type: ignore[assignment]
        self.assertEqual(captured["method"], "PATCH")
        self.assertIn("/repos/o/r/pulls/42", captured["path"])
        self.assertEqual(captured["body"], {"body": "hello"})


class SetPrDescriptionToolSchemaTests(unittest.TestCase):
    """Sanity checks on the exposed schema for the autocomplete surface."""

    def test_tool_description_forbids_secrets(self) -> None:
        schema = reviewer.tools_schema(
            10, allow_set_pr_description=True
        )
        tool = next(
            t for t in schema if t["name"] == "set_pr_description"
        )
        # The description warns the model against leaking secrets — the
        # single most important prompt-injection mitigation for this
        # write-back path.
        desc: str = tool["description"].lower()
        self.assertTrue(
            "secret" in desc or "credential" in desc or "token" in desc,
            "set_pr_description schema must warn against secret leakage",
        )

    def test_tool_description_mentions_marker(self) -> None:
        schema = reviewer.tools_schema(
            10, allow_set_pr_description=True
        )
        tool = next(
            t for t in schema if t["name"] == "set_pr_description"
        )
        self.assertIn(
            "ai-pr-reviewer-description-autocompleted",
            tool["description"],
        )


class ResolveTriggerActionTests(unittest.TestCase):
    """Matrix coverage across the four trigger modes."""

    def test_always_runs_regardless_of_state(self) -> None:
        d = reviewer.resolve_trigger_action(
            trigger_mode=reviewer.TRIGGER_ALWAYS,
            event_action="opened",
            label_gate="",
            current_labels=[],
            label_toggle_generation=0,
            last_reviewed_generation=0,
        )
        self.assertTrue(d.should_run)

    def test_missing_label_gate_falls_back_to_always(self) -> None:
        d = reviewer.resolve_trigger_action(
            trigger_mode=reviewer.TRIGGER_LABEL_ONCE,
            event_action="opened",
            label_gate="",
            current_labels=[],
            label_toggle_generation=0,
            last_reviewed_generation=0,
        )
        self.assertTrue(d.should_run)
        self.assertIn("no label-gate", d.reason)

    def test_label_required_blocks_when_missing(self) -> None:
        d = reviewer.resolve_trigger_action(
            trigger_mode=reviewer.TRIGGER_LABEL_REQUIRED,
            event_action="opened",
            label_gate="ready",
            current_labels=["bug"],
            label_toggle_generation=0,
            last_reviewed_generation=0,
        )
        self.assertFalse(d.should_run)
        self.assertIn("not present", d.reason)

    def test_label_required_runs_when_present(self) -> None:
        d = reviewer.resolve_trigger_action(
            trigger_mode=reviewer.TRIGGER_LABEL_REQUIRED,
            event_action="synchronize",
            label_gate="ready",
            current_labels=["ready", "bug"],
            label_toggle_generation=1,
            last_reviewed_generation=0,
        )
        self.assertTrue(d.should_run)

    def test_label_once_runs_on_first_generation(self) -> None:
        d = reviewer.resolve_trigger_action(
            trigger_mode=reviewer.TRIGGER_LABEL_ONCE,
            event_action="labeled",
            label_gate="ready",
            current_labels=["ready"],
            label_toggle_generation=1,
            last_reviewed_generation=0,
        )
        self.assertTrue(d.should_run)
        self.assertIn("new label generation", d.reason)

    def test_label_once_blocks_on_stale_generation(self) -> None:
        d = reviewer.resolve_trigger_action(
            trigger_mode=reviewer.TRIGGER_LABEL_ONCE,
            event_action="synchronize",
            label_gate="ready",
            current_labels=["ready"],
            label_toggle_generation=1,
            last_reviewed_generation=1,
        )
        self.assertFalse(d.should_run)
        self.assertIn("already reviewed", d.reason)

    def test_label_once_runs_after_toggle(self) -> None:
        d = reviewer.resolve_trigger_action(
            trigger_mode=reviewer.TRIGGER_LABEL_ONCE,
            event_action="labeled",
            label_gate="ready",
            current_labels=["ready"],
            label_toggle_generation=2,
            last_reviewed_generation=1,
        )
        self.assertTrue(d.should_run)

    def test_label_added_only_ignores_non_labeled_events(self) -> None:
        d = reviewer.resolve_trigger_action(
            trigger_mode=reviewer.TRIGGER_LABEL_ADDED_ONLY,
            event_action="synchronize",
            label_gate="ready",
            current_labels=["ready"],
            label_toggle_generation=1,
            last_reviewed_generation=0,
        )
        self.assertFalse(d.should_run)
        self.assertIn("is not 'labeled'", d.reason)

    def test_label_added_only_fires_on_labeled(self) -> None:
        d = reviewer.resolve_trigger_action(
            trigger_mode=reviewer.TRIGGER_LABEL_ADDED_ONLY,
            event_action="labeled",
            label_gate="ready",
            current_labels=["ready"],
            label_toggle_generation=1,
            last_reviewed_generation=0,
        )
        self.assertTrue(d.should_run)

    def test_unknown_trigger_mode_blocks(self) -> None:
        d = reviewer.resolve_trigger_action(
            trigger_mode="whatever",
            event_action="opened",
            label_gate="ready",
            current_labels=["ready"],
            label_toggle_generation=0,
            last_reviewed_generation=0,
        )
        self.assertFalse(d.should_run)


class TriggerStateRoundtripTests(unittest.TestCase):
    """`write_trigger_state` + `read_trigger_state` roundtrip."""

    def test_write_then_read_recovers_state(self) -> None:
        body = "<!-- ai-pr-reviewer-marker -->\n\nSome body content."
        written = reviewer.write_trigger_state(
            body, {"label_toggle_generation": 3, "reviewed_sha": "abc"}
        )
        state = reviewer.read_trigger_state(written)
        self.assertEqual(state["label_toggle_generation"], 3)
        self.assertEqual(state["reviewed_sha"], "abc")

    def test_read_returns_empty_when_no_marker(self) -> None:
        self.assertEqual(reviewer.read_trigger_state(""), {})
        self.assertEqual(
            reviewer.read_trigger_state("Just some markdown, no state."),
            {},
        )

    def test_write_replaces_prior_state(self) -> None:
        body = "<!-- ai-pr-reviewer-marker -->\n\nBody."
        step1 = reviewer.write_trigger_state(
            body, {"label_toggle_generation": 1}
        )
        step2 = reviewer.write_trigger_state(
            step1, {"label_toggle_generation": 2}
        )
        # Only the most recent state should be present.
        self.assertEqual(step2.count("ai-pr-reviewer-state"), 1)
        state = reviewer.read_trigger_state(step2)
        self.assertEqual(state["label_toggle_generation"], 2)

    def test_read_ignores_malformed_json(self) -> None:
        body = (
            "<!-- ai-pr-reviewer-marker -->\n"
            "<!-- ai-pr-reviewer-state: {not valid json} -->\n"
            "Body."
        )
        self.assertEqual(reviewer.read_trigger_state(body), {})


class EvaluatePrDescriptionTests(unittest.TestCase):
    """Cheap heuristic covering empty / short / adequate bodies."""

    def test_empty_body_is_inadequate(self) -> None:
        v = reviewer.evaluate_pr_description("", min_length=50)
        self.assertFalse(v.is_adequate)
        self.assertIn("empty", v.reason.lower())

    def test_whitespace_only_body_is_inadequate(self) -> None:
        v = reviewer.evaluate_pr_description("   \n\t\n  ", min_length=50)
        self.assertFalse(v.is_adequate)

    def test_short_body_is_inadequate(self) -> None:
        v = reviewer.evaluate_pr_description("wip", min_length=50)
        self.assertFalse(v.is_adequate)
        self.assertIn("too short", v.reason.lower())
        self.assertIn("50", v.reason)

    def test_body_at_threshold_is_adequate(self) -> None:
        body = "x" * 50
        v = reviewer.evaluate_pr_description(body, min_length=50)
        self.assertTrue(v.is_adequate)
        self.assertEqual(v.reason, "")

    def test_marker_is_stripped_before_length_check(self) -> None:
        # The autocomplete marker adds ~50 chars; a body that's just the
        # marker should NOT pass the min_length gate.
        body = reviewer.PR_DESC_AUTOCOMPLETE_MARKER + "x"
        v = reviewer.evaluate_pr_description(body, min_length=50)
        self.assertFalse(v.is_adequate)

    def test_none_body_treated_as_empty(self) -> None:
        v = reviewer.evaluate_pr_description(None, min_length=50)  # type: ignore[arg-type]
        self.assertFalse(v.is_adequate)


class ToolSetPrDescriptionTests(unittest.TestCase):
    """`tool_set_pr_description` records into state and enforces one-shot."""

    def test_records_proposal(self) -> None:
        state = reviewer.ReviewState()
        result = reviewer.tool_set_pr_description(
            {"body": "New rich PR body with context."}, state
        )
        self.assertEqual(
            state.proposed_pr_description, "New rich PR body with context."
        )
        self.assertIn("recorded", result.lower())

    def test_rejects_empty_body(self) -> None:
        state = reviewer.ReviewState()
        result = reviewer.tool_set_pr_description({"body": "  \n  "}, state)
        self.assertIsNone(state.proposed_pr_description)
        self.assertIn("Error", result)

    def test_one_shot(self) -> None:
        state = reviewer.ReviewState()
        reviewer.tool_set_pr_description({"body": "first"}, state)
        result = reviewer.tool_set_pr_description({"body": "second"}, state)
        self.assertEqual(state.proposed_pr_description, "first")
        self.assertIn("already", result.lower())


class ToolSetPrComplexityTests(unittest.TestCase):
    """`tool_set_pr_complexity` records + validates level enum."""

    def test_records_low(self) -> None:
        state = reviewer.ReviewState()
        result = reviewer.tool_set_pr_complexity({"level": "low"}, state)
        self.assertEqual(state.proposed_pr_complexity, "low")
        self.assertIn("recorded", result.lower())

    def test_case_normalized(self) -> None:
        state = reviewer.ReviewState()
        reviewer.tool_set_pr_complexity({"level": "HIGH"}, state)
        self.assertEqual(state.proposed_pr_complexity, "high")

    def test_rejects_unknown_level(self) -> None:
        state = reviewer.ReviewState()
        result = reviewer.tool_set_pr_complexity({"level": "epic"}, state)
        self.assertIsNone(state.proposed_pr_complexity)
        self.assertIn("Error", result)

    def test_one_shot(self) -> None:
        state = reviewer.ReviewState()
        reviewer.tool_set_pr_complexity({"level": "low"}, state)
        result = reviewer.tool_set_pr_complexity({"level": "high"}, state)
        self.assertEqual(state.proposed_pr_complexity, "low")
        self.assertIn("already", result.lower())


class ToolsSchemaGatingTests(unittest.TestCase):
    """Optional tools are exposed only when their flag is set."""

    def test_base_five_always_present(self) -> None:
        schema = reviewer.tools_schema(10)
        names = [t["name"] for t in schema]
        for expected in (
            "read_file",
            "grep",
            "glob",
            "post_inline_comment",
            "submit_review",
        ):
            self.assertIn(expected, names)

    def test_set_pr_description_absent_by_default(self) -> None:
        schema = reviewer.tools_schema(10)
        names = [t["name"] for t in schema]
        self.assertNotIn("set_pr_description", names)

    def test_set_pr_description_present_when_allowed(self) -> None:
        schema = reviewer.tools_schema(
            10, allow_set_pr_description=True
        )
        names = [t["name"] for t in schema]
        self.assertIn("set_pr_description", names)

    def test_set_pr_complexity_absent_by_default(self) -> None:
        schema = reviewer.tools_schema(10)
        names = [t["name"] for t in schema]
        self.assertNotIn("set_pr_complexity", names)

    def test_set_pr_complexity_present_when_allowed(self) -> None:
        schema = reviewer.tools_schema(
            10, allow_set_pr_complexity=True
        )
        names = [t["name"] for t in schema]
        self.assertIn("set_pr_complexity", names)

    def test_both_optional_tools_present_when_both_flags(self) -> None:
        schema = reviewer.tools_schema(
            10,
            allow_set_pr_description=True,
            allow_set_pr_complexity=True,
        )
        names = [t["name"] for t in schema]
        self.assertIn("set_pr_description", names)
        self.assertIn("set_pr_complexity", names)
        self.assertEqual(len(names), 7)


class ComposeSystemPromptTests(unittest.TestCase):
    """`compose_system_prompt(base, extension)` covers the four cases of
    the base+extension matrix.
    """

    def test_empty_extension_returns_base_unchanged(self) -> None:
        base = "You are the reviewer.\n"
        result = reviewer.compose_system_prompt(base, "")
        self.assertEqual(result, base)

    def test_extension_appended_with_separator(self) -> None:
        base = "You are the reviewer."
        ext = "Extra rule: never suggest `any`."
        result = reviewer.compose_system_prompt(base, ext)
        self.assertIn("You are the reviewer.", result)
        self.assertIn("---", result)
        self.assertIn("Extra rule: never suggest `any`.", result)
        # The base appears before the separator, extension after.
        self.assertLess(
            result.index("You are the reviewer."), result.index("---")
        )
        self.assertLess(
            result.index("---"),
            result.index("Extra rule: never suggest `any`."),
        )

    def test_extension_strips_leading_whitespace(self) -> None:
        base = "Base."
        ext = "\n\n\nExtension."
        result = reviewer.compose_system_prompt(base, ext)
        # Should not have four consecutive newlines between separator and
        # extension body — extension is lstripped.
        self.assertNotIn("---\n\n\n\nExtension", result)
        self.assertIn("---\n\nExtension.", result)

    def test_base_strips_trailing_whitespace(self) -> None:
        base = "Base.\n\n\n\n"
        ext = "Ext."
        result = reviewer.compose_system_prompt(base, ext)
        self.assertIn("Base.\n\n---\n\nExt.", result)

    def test_full_replacement_semantic_preserved(self) -> None:
        # When a custom prompt-file is used as the base, the same
        # composition rule applies with no default content leaked.
        custom_base = "# Custom prompt\nOnly rule: be brief."
        ext = "Additional rule: cite line numbers."
        result = reviewer.compose_system_prompt(custom_base, ext)
        self.assertIn("Custom prompt", result)
        self.assertIn("Only rule: be brief.", result)
        self.assertIn("Additional rule: cite line numbers.", result)
        # No leakage of the bundled default (its unique phrase).
        self.assertNotIn("post_inline_comment", result)


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

    def test_block_on_any_blocks_on_info(self) -> None:
        blocked, reason = reviewer.evaluate_strictness(
            reviewer.SEVERITY_INFO, reviewer.STRICTNESS_BLOCK_ANY
        )
        self.assertTrue(blocked)
        self.assertIn("block-on-any", reason)

    def test_block_on_any_blocks_on_warning(self) -> None:
        self.assertTrue(
            reviewer.evaluate_strictness(
                reviewer.SEVERITY_WARNING, reviewer.STRICTNESS_BLOCK_ANY
            )[0]
        )

    def test_block_on_any_blocks_on_critical(self) -> None:
        self.assertTrue(
            reviewer.evaluate_strictness(
                reviewer.SEVERITY_CRITICAL, reviewer.STRICTNESS_BLOCK_ANY
            )[0]
        )

    def test_block_on_any_passes_on_none(self) -> None:
        blocked, reason = reviewer.evaluate_strictness(
            reviewer.SEVERITY_NONE, reviewer.STRICTNESS_BLOCK_ANY
        )
        self.assertFalse(blocked)
        self.assertIn("no findings", reason)

    def test_block_on_any_in_valid_strictness(self) -> None:
        self.assertIn(
            reviewer.STRICTNESS_BLOCK_ANY, reviewer.VALID_STRICTNESS
        )


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

    Emits N tool-call turns (each an unknown-tool stub so `execute_tool`
    returns immediately without touching the filesystem or shelling out)
    then a final text turn, so we can assert the conversation-pruning
    invariant without any network AND without any I/O — critical for the
    40-turn stress test in DriveReviewPruningTests.
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
                        # Unknown tool name → execute_tool returns
                        # "Error: unknown tool" immediately. The pruning
                        # loop treats it as a tool_result and moves on.
                        "name": "__pruning_test_stub__",
                        "input": {},
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
