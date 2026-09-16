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


class CursorHeadlessDefaultsTests(unittest.TestCase):
    """CursorProvider default argv includes Cursor's own headless-CI flags.

    v1.2.0+: `--force --trust` are always passed; `--approve-mcps` is
    added iff `mcp_config_file` is non-empty. These are Cursor's own
    recommendations from https://cursor.com/docs/cli/headless — without
    them, the CLI can stall on interactive approval prompts in CI.
    """

    _last_captured: dict[str, Any] = {}

    def _run_and_capture_argv(
        self, *, model: str = "", mcp_config_file: str = "", extra_args: str = ""
    ) -> list[str]:
        """Monkey-patch `_invoke_cli_agent` and return the argv it received."""
        captured: dict[str, Any] = {}

        def fake_invoke(*, argv: list[str], **_kwargs: Any) -> Any:
            captured["argv"] = list(argv)
            captured["kwargs"] = dict(_kwargs)
            return reviewer.ReviewResult(summary="ok", findings=[])

        orig = reviewer._invoke_cli_agent
        reviewer._invoke_cli_agent = fake_invoke  # type: ignore[assignment]
        try:
            with tempfile.TemporaryDirectory() as td:
                workspace = Path(td)
                # `_swap_mcp_config` reads the source path — for mcp_config_file
                # we need a real file. Create one when the test requests it.
                mcp_arg: str = ""
                if mcp_config_file:
                    mcp_arg = str(workspace / "mcp.json")
                    Path(mcp_arg).write_text('{"mcpServers":{}}', encoding="utf-8")
                p = reviewer.CursorProvider(
                    api_key="k",
                    model=model,
                    extra_args=extra_args,
                    mcp_config_file=mcp_arg,
                )
                # Override the default MCP_DEST so the swap does not touch the
                # real ~/.cursor/mcp.json during tests.
                p.MCP_DEST = workspace / ".cursor" / "mcp.json"  # type: ignore[misc]
                p.run_review(
                    pr_context=_make_pr_context(),
                    review_instructions="review this",
                    workspace=workspace,
                    output_dir=workspace,
                )
        finally:
            reviewer._invoke_cli_agent = orig  # type: ignore[assignment]
        CursorHeadlessDefaultsTests._last_captured = captured
        return captured["argv"]

    def test_force_and_trust_are_always_present(self) -> None:
        argv = self._run_and_capture_argv()
        self.assertIn(
            "--force",
            argv,
            "Cursor headless CI must pass --force (skip interactive tool "
            "approvals). See docs/PROVIDERS.md § Cursor CLI.",
        )
        self.assertIn(
            "--trust",
            argv,
            "Cursor headless CI must pass --trust (mark workspace trusted).",
        )

    def test_approve_mcps_absent_when_no_mcp_config(self) -> None:
        argv = self._run_and_capture_argv(mcp_config_file="")
        self.assertNotIn(
            "--approve-mcps",
            argv,
            "--approve-mcps is only relevant when an MCP config was injected.",
        )

    def test_approve_mcps_present_when_mcp_config_set(self) -> None:
        argv = self._run_and_capture_argv(mcp_config_file="mcp.json")
        self.assertIn(
            "--approve-mcps",
            argv,
            "When mcp-config-file is set, --approve-mcps must be added to "
            "prevent the interactive MCP approval prompt from stalling CI.",
        )

    def test_model_flag_still_honored(self) -> None:
        argv = self._run_and_capture_argv(model="auto")
        self.assertIn("--model", argv)
        model_idx = argv.index("--model")
        self.assertEqual(argv[model_idx + 1], "auto")

    def test_extra_args_still_appended_after_defaults(self) -> None:
        argv = self._run_and_capture_argv(extra_args="--custom-flag=value")
        self.assertIn("--force", argv)
        self.assertIn("--trust", argv)
        self.assertIn("--custom-flag=value", argv)
        # extra_args comes after the built-in flags so the CLI's own parser
        # resolves conflicts in favor of the consumer's explicit override.
        self.assertGreater(
            argv.index("--custom-flag=value"),
            argv.index("--force"),
            "agent-extra-args must be appended AFTER the default headless "
            "flags so consumer overrides take precedence in CLI parsing.",
        )

    def test_user_prompt_not_in_argv_and_goes_via_stdin(self) -> None:
        """Regression: user prompt (which includes the full diff) must NOT be
        embedded into argv, or the kernel raises E2BIG on large PRs. It must
        be piped via stdin instead. See PR #9 self-review-cursor failure."""
        argv = self._run_and_capture_argv()
        # `-p` MUST be present but with NO positional prompt argument
        # following it. The token right after `-p` should be another flag,
        # not the review-instructions payload.
        self.assertIn("-p", argv, "Cursor headless mode requires -p flag.")
        p_idx = argv.index("-p")
        if p_idx + 1 < len(argv):
            next_tok = argv[p_idx + 1]
            self.assertTrue(
                next_tok.startswith("-"),
                f"Nothing should be passed as a positional after -p, but "
                f"found {next_tok!r}. Large prompts must go via stdin, not "
                f"argv (Linux ARG_MAX ~128 KB blows up on 200 KB+ diffs).",
            )
        stdin_input = self._last_captured["kwargs"].get("stdin_input")
        self.assertIsNotNone(
            stdin_input,
            "CursorProvider must pipe the user prompt via stdin_input to "
            "avoid E2BIG. See _invoke_cli_agent's stdin_input parameter.",
        )
        # The stdin payload should contain both the review instructions
        # (findings.json contract) AND the PR context (title/diff header).
        self.assertIn("findings.json", stdin_input)
        self.assertIn("# PR Context", stdin_input)


if __name__ == "__main__":
    unittest.main()
