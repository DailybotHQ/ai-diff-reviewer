#!/usr/bin/env python3
"""Unit tests for the three agent-runner CLI providers.

Covers construction, argv builders, MCP config passthrough, and dispatch via
`build_provider()`. Actual CLI invocations are exercised via dogfooding
(`.github/workflows/self-review.yml`) — these tests validate the pure logic
that surrounds the subprocess boundary.
"""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest
from unittest import mock
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

def _make_pr_context() -> Any:
    """Minimal PRContext for tests that need one."""
    return reviewer.PRContext(
        title="Test PR",
        author="reviewer-tester",
        head_ref="feat/x",
        base_ref="main",
        state="open",
        additions=1,
        deletions=0,
        commits=1,
        body="Test body",
    )


def _write_findings(tmp: Path, payload: dict) -> Path:
    """Write a canonical findings.json into `tmp/.aiprr/findings.json`."""
    findings_dir = tmp / ".aiprr"
    findings_dir.mkdir(parents=True, exist_ok=True)
    path = findings_dir / "findings.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def _writer_argv(path: Path, payload: dict, exit_code: int = 0) -> list[str]:
    """A fake CLI that writes `payload` to `path` and exits with `exit_code`
    (v2.2.0+: `_invoke_cli_agent` removes any pre-existing findings file
    before the subprocess, so the file must come from the subprocess)."""
    return [
        "python3", "-c",
        "import pathlib, sys; p = pathlib.Path(sys.argv[1]); p.parent.mkdir(parents=True, exist_ok=True); "
        "p.write_text(sys.argv[2]); sys.exit(int(sys.argv[3]))",
        str(path), json.dumps(payload), str(exit_code),
    ]


class BuildProviderDispatchTests(unittest.TestCase):
    """`build_provider()` returns the right class per `provider_id`."""

    def test_anthropic_returns_anthropic_provider(self) -> None:
        p = reviewer.build_provider("anthropic", api_key="k", model="m")
        self.assertIsInstance(p, reviewer.AnthropicProvider)

    def test_claude_code_returns_claude_code_provider(self) -> None:
        p = reviewer.build_provider("claude-code", api_key="k", model="")
        self.assertIsInstance(p, reviewer.ClaudeCodeProvider)
        self.assertIsInstance(p, reviewer.AgentRunnerProvider)

    def test_cursor_returns_cursor_provider(self) -> None:
        p = reviewer.build_provider("cursor", api_key="k", model="")
        self.assertIsInstance(p, reviewer.CursorProvider)
        self.assertIsInstance(p, reviewer.AgentRunnerProvider)

    def test_codex_returns_codex_provider(self) -> None:
        p = reviewer.build_provider("codex", api_key="k", model="")
        self.assertIsInstance(p, reviewer.CodexProvider)
        self.assertIsInstance(p, reviewer.AgentRunnerProvider)

    def test_unknown_provider_raises_value_error(self) -> None:
        with self.assertRaises(ValueError) as ctx:
            reviewer.build_provider("mystery", api_key="k", model="m")
        self.assertIn("Unsupported provider", str(ctx.exception))

    def test_default_models_covers_all_shipping_providers(self) -> None:
        for provider_id in ("anthropic", "openai", "claude-code", "cursor", "codex", "grok"):
            self.assertIn(provider_id, reviewer.DEFAULT_MODELS)
            self.assertTrue(reviewer.DEFAULT_MODELS[provider_id])


class ProviderConstructionTests(unittest.TestCase):
    """Each provider records constructor args as expected."""

    def test_claude_code_stores_all_fields(self) -> None:
        p = reviewer.ClaudeCodeProvider(
            api_key="AK", model="opus", extra_args="--foo", mcp_config_file="/x"
        )
        self.assertEqual(p.api_key, "AK")
        self.assertEqual(p.model, "opus")
        self.assertEqual(p.extra_args, "--foo")
        self.assertEqual(p.mcp_config_file, "/x")

    def test_cursor_stores_all_fields(self) -> None:
        p = reviewer.CursorProvider(
            api_key="AK", model="composer-2.5", extra_args="", mcp_config_file=""
        )
        self.assertEqual(p.model, "composer-2.5")

    def test_codex_stores_all_fields(self) -> None:
        p = reviewer.CodexProvider(
            api_key="AK", model="gpt-5.4-mini", extra_args="", mcp_config_file=""
        )
        self.assertEqual(p.model, "gpt-5.4-mini")

    def test_default_extras_are_empty(self) -> None:
        p = reviewer.ClaudeCodeProvider(api_key="k", model="m")
        self.assertEqual(p.extra_args, "")
        self.assertEqual(p.mcp_config_file, "")


class McpConfigPassthroughTests(unittest.TestCase):
    """`_swap_mcp_config` + `_restore_mcp_config` round-trip."""

    def test_swap_with_empty_src_is_noop(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            dest = Path(td) / "mcp.json"
            dest_ret, backup = reviewer._swap_mcp_config("", dest)
            self.assertIsNone(dest_ret)
            self.assertIsNone(backup)
            self.assertFalse(dest.exists())

    def test_swap_copies_to_dest_when_dest_missing(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            src = tmp / "src-mcp.json"
            src.write_text('{"servers": {}}', encoding="utf-8")
            dest = tmp / "sub" / "mcp.json"

            dest_ret, backup = reviewer._swap_mcp_config(str(src), dest)

            self.assertEqual(dest_ret, dest)
            self.assertIsNone(backup)
            self.assertEqual(dest.read_text(), '{"servers": {}}')

    def test_swap_backs_up_existing_dest(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            src = tmp / "src.json"
            src.write_text("NEW", encoding="utf-8")
            dest = tmp / "dest.json"
            dest.write_text("OLD", encoding="utf-8")

            dest_ret, backup = reviewer._swap_mcp_config(str(src), dest)

            self.assertEqual(backup, "OLD")
            self.assertEqual(dest.read_text(), "NEW")

    def test_restore_with_backup_restores_old_content(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            dest = Path(td) / "dest.json"
            dest.write_text("NEW", encoding="utf-8")
            reviewer._restore_mcp_config(dest, "OLD")
            self.assertEqual(dest.read_text(), "OLD")

    def test_restore_without_backup_deletes_file(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            dest = Path(td) / "dest.json"
            dest.write_text("NEW", encoding="utf-8")
            reviewer._restore_mcp_config(dest, None)
            self.assertFalse(dest.exists())

    def test_restore_none_dest_is_noop(self) -> None:
        # Should not raise
        reviewer._restore_mcp_config(None, None)
        reviewer._restore_mcp_config(None, "content")


class InvokeCliAgentTests(unittest.TestCase):
    """`_invoke_cli_agent` correctly reads findings.json on success + raises
    on non-zero exit / timeout."""

    def test_success_parses_findings_file(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            findings_path = tmp / ".aiprr" / "findings.json"
            argv = _writer_argv(findings_path, {"summary": "ok", "findings": []})
            result = reviewer._invoke_cli_agent(
                argv=argv,
                workspace=tmp,
                findings_path=findings_path,
                env={**os.environ},
                cli_name="TestCLI",
            )
            self.assertEqual(result.summary, "ok")
            self.assertEqual(result.findings, [])

    def test_nonzero_exit_raises(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            # findings file NOT written; python exits 1.
            argv = ["python3", "-c", "import sys; sys.exit(1)"]
            with self.assertRaises(RuntimeError) as ctx:
                reviewer._invoke_cli_agent(
                    argv=argv,
                    workspace=tmp,
                    findings_path=tmp / ".aiprr" / "findings.json",
                    env={**os.environ},
                    cli_name="TestCLI",
                )
            self.assertIn("exited with code 1", str(ctx.exception))

    def test_missing_findings_after_success_degrades_to_summary_only(self) -> None:
        """v2.2.0: exit 0 without a findings file → an explicit summary-only
        review (the agent ended without producing the contract output),
        never a failed run and never a silent 'no findings'."""
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            argv = ["python3", "-c", "pass"]
            with mock.patch.object(reviewer, "log") as fake_log:
                res = reviewer._invoke_cli_agent(
                    argv=argv, workspace=tmp, findings_path=tmp / ".aiprr" / "findings.json",
                    env={**os.environ}, cli_name="TestCLI",
                )
            self.assertEqual(res.findings, [])
            self.assertTrue(res.incomplete, "main() reads this flag to fail the gate and skip the label")
            self.assertIn("without writing its findings file", res.summary)
            self.assertIn("incomplete review", res.summary)
            self.assertTrue(any("WARNING" in str(c.args[0]) and "did not write" in str(c.args[0]) for c in fake_log.call_args_list))

    def test_stale_findings_file_is_removed_before_the_cli_runs(self) -> None:
        """A findings file left by a previous step or a persistent workspace
        must never be posted as this run's review."""
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            stale = _write_findings(tmp, {"summary": "STALE", "findings": [{"path": "a.py", "line": 1, "body": "old", "severity": "critical"}]})
            with mock.patch.object(reviewer, "log"):
                res = reviewer._invoke_cli_agent(
                    argv=["python3", "-c", "pass"], workspace=tmp, findings_path=stale,
                    env={**os.environ}, cli_name="TestCLI",
                )
            self.assertTrue(res.incomplete)
            self.assertNotIn("STALE", res.summary)
            self.assertEqual(res.findings, [])

    def test_incomplete_review_gate_never_greens_a_blocking_strictness(self) -> None:
        for strictness in ("block-on-critical", "block-on-warning", "block-on-any"):
            blocked, reason = reviewer.incomplete_review_gate(strictness, "Grok")
            self.assertTrue(blocked, strictness)
            self.assertIn("incomplete review", reason)
        blocked, reason = reviewer.incomplete_review_gate("lenient", "Grok")
        self.assertFalse(blocked)
        self.assertIn("lenient", reason)

    def test_nonzero_exit_with_findings_file_is_a_partial_review(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            argv = _writer_argv(tmp / ".aiprr" / "findings.json", {"summary": "s", "findings": [{"path": "a.py", "line": 1, "body": "b", "severity": "info"}]}, exit_code=3)
            with mock.patch.object(reviewer, "log") as fake_log:
                res = reviewer._invoke_cli_agent(
                    argv=argv, workspace=tmp, findings_path=tmp / ".aiprr" / "findings.json",
                    env={**os.environ}, cli_name="TestCLI",
                )
            self.assertEqual(len(res.findings), 1)
            self.assertIn("Partial review: TestCLI exited with code 3", res.summary)
            self.assertTrue(any("partial review" in str(c.args[0]) for c in fake_log.call_args_list))

    def test_nonzero_exit_without_findings_file_still_raises(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            with self.assertRaises(RuntimeError) as ctx:
                reviewer._invoke_cli_agent(
                    argv=["python3", "-c", "import sys; sys.exit(2)"], workspace=tmp,
                    findings_path=tmp / ".aiprr" / "findings.json", env={**os.environ}, cli_name="TestCLI",
                )
            self.assertIn("exited with code 2", str(ctx.exception))


class CliBinaryConstantsTests(unittest.TestCase):
    """Each provider knows its CLI binary + MCP destination."""

    def test_claude_code_constants(self) -> None:
        self.assertEqual(reviewer.ClaudeCodeProvider.CLI_BIN, "claude")
        self.assertEqual(reviewer.ClaudeCodeProvider.CLI_NAME, "Claude Code")
        self.assertTrue(
            str(reviewer.ClaudeCodeProvider.MCP_DEST).endswith(".claude/mcp.json")
        )

    def test_cursor_constants(self) -> None:
        self.assertEqual(reviewer.CursorProvider.CLI_BIN, "cursor-agent")
        self.assertTrue(
            str(reviewer.CursorProvider.MCP_DEST).endswith(".cursor/mcp.json")
        )

    def test_codex_constants(self) -> None:
        self.assertEqual(reviewer.CodexProvider.CLI_BIN, "codex")
        self.assertTrue(
            str(reviewer.CodexProvider.MCP_DEST).endswith(".codex/mcp.json")
        )

    def test_grok_constants(self) -> None:
        self.assertEqual(reviewer.GrokProvider.CLI_BIN, "grok")
        self.assertEqual(reviewer.GrokProvider.CLI_NAME, "xAI Grok")
        self.assertEqual(reviewer.GrokProvider.PROVIDER_ID, "grok")


def _capture_provider_call(provider: Any) -> dict[str, Any]:
    """Run `provider.run_review` with `_invoke_cli_agent` stubbed; return the
    captured argv + kwargs (including stdin_input)."""
    captured: dict[str, Any] = {}

    def fake_invoke(*, argv: list[str], **kwargs: Any) -> Any:
        captured["argv"] = list(argv)
        captured["kwargs"] = dict(kwargs)
        return reviewer.ReviewResult(summary="ok", findings=[])

    orig = reviewer._invoke_cli_agent
    reviewer._invoke_cli_agent = fake_invoke  # type: ignore[assignment]
    try:
        with tempfile.TemporaryDirectory() as td:
            workspace = Path(td)
            # Keep MCP swaps off the real ~/.<cli>/mcp.json during tests.
            provider.MCP_DEST = workspace / "mcp.json"  # type: ignore[misc]
            provider.run_review(
                pr_context=_make_pr_context(),
                review_instructions="RUBRIC_TEXT_MARKER",
                workspace=workspace,
                output_dir=workspace,
            )
    finally:
        reviewer._invoke_cli_agent = orig  # type: ignore[assignment]
    return captured


if __name__ == "__main__":
    unittest.main()
