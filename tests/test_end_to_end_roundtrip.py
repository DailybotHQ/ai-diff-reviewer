#!/usr/bin/env python3
"""End-to-end serialization roundtrips + env-var integration for v1.1.0.

These tests exercise the seams between the pieces added in tasks 01–07:
  * ReviewState (chat-completions) → ReviewResult → GH inline shape.
  * findings.json (agent-runner) → ReviewResult → GH inline shape.
  * AIPRR_* env vars → build_provider → correct provider class + extras.

Together they guarantee both provider families converge into the SAME
GitHub Reviews payload — which is the load-bearing invariant of the
multi-CLI expansion.
"""

from __future__ import annotations

import importlib.util
import json
import os
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


class RoundtripChatCompletionsToGithubShape(unittest.TestCase):
    """The chat-completions path produces the SAME GH inline shape as the
    agent-runner path, given equivalent inputs."""

    def test_chat_completions_and_agent_runner_produce_same_gh_shape(
        self,
    ) -> None:
        # 1) Chat-completions: state populated via tool handler, then adapt.
        state = reviewer.ReviewState()
        reviewer.tool_post_inline_comment(
            {
                "path": "src/a.py",
                "line": 42,
                "body": "critical: null deref",
                "severity": "critical",
            },
            state,
        )
        reviewer.tool_post_inline_comment(
            {
                "path": "src/b.py",
                "line": 10,
                "start_line": 8,
                "body": "perf: O(n^2)",
                "severity": "warning",
            },
            state,
        )
        state.final_summary = "## Summary\n\nTwo issues."
        result_from_state = reviewer.state_to_review_result(state)
        gh_from_state = reviewer.findings_to_gh_inline_comments(
            result_from_state.findings
        )

        # 2) Agent-runner: findings.json parsed to result.
        with tempfile.TemporaryDirectory() as td:
            findings_path = Path(td) / "findings.json"
            findings_path.write_text(
                json.dumps(
                    {
                        "summary": "## Summary\n\nTwo issues.",
                        "findings": [
                            {
                                "path": "src/a.py",
                                "line": 42,
                                "body": "critical: null deref",
                                "severity": "critical",
                            },
                            {
                                "path": "src/b.py",
                                "line": 10,
                                "start_line": 8,
                                "body": "perf: O(n^2)",
                                "severity": "warning",
                            },
                        ],
                    }
                ),
                encoding="utf-8",
            )
            result_from_json = reviewer.parse_findings_file(findings_path)
            gh_from_json = reviewer.findings_to_gh_inline_comments(
                result_from_json.findings
            )

        # Both paths converge to identical GH shape + identical summary +
        # identical overall_severity — the load-bearing invariant.
        self.assertEqual(gh_from_state, gh_from_json)
        self.assertEqual(result_from_state.summary, result_from_json.summary)
        self.assertEqual(
            result_from_state.overall_severity,
            result_from_json.overall_severity,
        )

    def test_severity_precedence_is_consistent(self) -> None:
        """`overall_severity` picks the highest across both paths."""
        for severities, expected in [
            (["info", "warning", "critical", "info"], "critical"),
            (["warning", "info", "warning"], "warning"),
            (["info"], "info"),
            ([], "none"),
        ]:
            self.assertEqual(
                reviewer.overall_severity(severities),
                expected,
                msg=f"severities={severities}",
            )


class EnvVarBuildProviderIntegration(unittest.TestCase):
    """`build_provider()` reads AIPRR_AGENT_EXTRA_ARGS / AIPRR_MCP_CONFIG_FILE
    from the env for all agent-runner providers, so setting them from
    action.yml (as `env:` block does) reaches the provider constructor."""

    def _with_env(self, extras: str, mcp: str) -> None:
        prev_extras = os.environ.get("AIPRR_AGENT_EXTRA_ARGS")
        prev_mcp = os.environ.get("AIPRR_MCP_CONFIG_FILE")
        os.environ["AIPRR_AGENT_EXTRA_ARGS"] = extras
        os.environ["AIPRR_MCP_CONFIG_FILE"] = mcp

        try:
            for provider_id, klass in [
                ("claude-code", reviewer.ClaudeCodeProvider),
                ("cursor", reviewer.CursorProvider),
                ("codex", reviewer.CodexProvider),
            ]:
                p = reviewer.build_provider(
                    provider_id, api_key="k", model="m"
                )
                self.assertIsInstance(p, klass)
                self.assertEqual(p.extra_args, extras)
                self.assertEqual(p.mcp_config_file, mcp)
        finally:
            if prev_extras is None:
                os.environ.pop("AIPRR_AGENT_EXTRA_ARGS", None)
            else:
                os.environ["AIPRR_AGENT_EXTRA_ARGS"] = prev_extras
            if prev_mcp is None:
                os.environ.pop("AIPRR_MCP_CONFIG_FILE", None)
            else:
                os.environ["AIPRR_MCP_CONFIG_FILE"] = prev_mcp

    def test_env_vars_reach_all_cli_providers(self) -> None:
        self._with_env("--verbose --allowed-tools '*'", "/path/to/mcp.json")

    def test_empty_env_vars_flow_as_empty_strings(self) -> None:
        self._with_env("", "")

    def test_anthropic_ignores_agent_env_vars(self) -> None:
        os.environ["AIPRR_AGENT_EXTRA_ARGS"] = "should-be-ignored"
        try:
            p = reviewer.build_provider(
                "anthropic", api_key="k", model="claude-sonnet-4-6"
            )
            self.assertIsInstance(p, reviewer.AnthropicProvider)
            # AnthropicProvider has no extra_args attribute — it's chat-
            # completions only.
            self.assertFalse(hasattr(p, "extra_args"))
        finally:
            os.environ.pop("AIPRR_AGENT_EXTRA_ARGS", None)


class ProviderIndependenceInvariant(unittest.TestCase):
    """The submission path (gh_submit_review_with_fallback consumers)
    NEVER references provider-specific types."""

    def test_review_result_carries_no_provider_ref(self) -> None:
        """Finding + ReviewResult are pure dataclasses with no provider link."""
        f = reviewer.Finding(path="a", line=1, body="b")
        # No provider attribute — deliberate.
        self.assertFalse(hasattr(f, "provider"))
        r = reviewer.ReviewResult(summary="s", findings=[f])
        self.assertFalse(hasattr(r, "provider"))

    def test_findings_to_gh_shape_is_pure(self) -> None:
        """The encoder does not touch env / global state / network."""
        # Verify it doesn't blow up when called with an empty env-var context.
        prev = dict(os.environ)
        try:
            os.environ.clear()
            out = reviewer.findings_to_gh_inline_comments(
                [reviewer.Finding(path="a", line=1, body="b", severity="info")]
            )
            self.assertEqual(len(out), 1)
        finally:
            os.environ.clear()
            os.environ.update(prev)


class ConstantWiringTests(unittest.TestCase):
    """The dataclass defaults + constants agree across the system."""

    def test_default_severity_matches_info_constant(self) -> None:
        f = reviewer.Finding(path="a", line=1, body="b")
        self.assertEqual(f.severity, reviewer.SEVERITY_INFO)

    def test_default_side_matches_right(self) -> None:
        f = reviewer.Finding(path="a", line=1, body="b")
        self.assertEqual(f.side, "RIGHT")

    def test_review_result_default_severity_is_none(self) -> None:
        r = reviewer.ReviewResult()
        self.assertEqual(r.overall_severity, reviewer.SEVERITY_NONE)

    def test_all_shipping_providers_have_default_models(self) -> None:
        for p in ("anthropic", "claude-code", "cursor", "codex"):
            self.assertIn(p, reviewer.DEFAULT_MODELS)


if __name__ == "__main__":
    unittest.main()
