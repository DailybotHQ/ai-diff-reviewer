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


def _capture_codex_call_with_auth_state(
    provider: Any,
) -> dict[str, Any]:
    """Capture argv/env plus the auth.json state INSIDE `_invoke_cli_agent`.

    The Codex apikey-mode auth.json lives in a `mkdtemp()` directory
    that is removed after `run_review()` returns. Anything we want to
    assert about the file must be snapshotted from inside the
    invocation.
    """
    captured: dict[str, Any] = {}

    def fake_invoke(*, argv: list[str], **kwargs: Any) -> Any:
        captured["argv"] = list(argv)
        captured["kwargs"] = dict(kwargs)
        env: dict[str, str] = kwargs.get("env", {})
        captured["env"] = dict(env)
        codex_home_str: str = env.get("CODEX_HOME", "")
        captured["codex_home_present_in_env"] = bool(codex_home_str)
        if codex_home_str:
            codex_home: Path = Path(codex_home_str)
            captured["codex_home_path"] = codex_home
            auth_path: Path = codex_home / "auth.json"
            captured["auth_json_exists_at_invocation"] = auth_path.exists()
            config_path: Path = codex_home / "config.toml"
            captured["config_toml_exists_at_invocation"] = config_path.exists()
            catalog_path: Path = codex_home / "models.json"
            captured["catalog_exists_at_invocation"] = catalog_path.exists()
            if catalog_path.exists():
                captured["catalog_content"] = catalog_path.read_text(encoding="utf-8")
            if config_path.exists():
                captured["config_toml_content"] = config_path.read_text(
                    encoding="utf-8"
                )
                captured["config_toml_mode"] = config_path.stat().st_mode & 0o777
            if auth_path.exists():
                captured["auth_json_content"] = auth_path.read_text(
                    encoding="utf-8"
                )
                captured["auth_json_mode"] = (
                    auth_path.stat().st_mode & 0o777
                )
                captured["codex_home_mode"] = (
                    codex_home.stat().st_mode & 0o777
                )
        return reviewer.ReviewResult(summary="ok", findings=[])

    orig = reviewer._invoke_cli_agent
    reviewer._invoke_cli_agent = fake_invoke  # type: ignore[assignment]
    try:
        with tempfile.TemporaryDirectory() as td:
            workspace = Path(td)
            provider.MCP_DEST = workspace / "mcp.json"  # type: ignore[misc]
            provider.run_review(
                pr_context=_make_pr_context(),
                review_instructions="RUBRIC",
                workspace=workspace,
                output_dir=workspace,
            )
    finally:
        reviewer._invoke_cli_agent = orig  # type: ignore[assignment]
    return captured


class ClaudeCodeInvocationTests(unittest.TestCase):
    """ClaudeCodeProvider must deliver the rubric as text, bypass the
    permission gate so the Write tool can emit findings.json, and pipe the
    diff-carrying user prompt via stdin (E2BIG safety)."""

    def _capture(self, *, model: str = "", extra_args: str = "") -> dict[str, Any]:
        return _capture_provider_call(
            reviewer.ClaudeCodeProvider(
                api_key="k", model=model, extra_args=extra_args
            )
        )

    def test_append_system_prompt_is_text_not_path(self) -> None:
        argv = self._capture()["argv"]
        self.assertIn("--append-system-prompt", argv)
        idx = argv.index("--append-system-prompt")
        value = argv[idx + 1]
        # The value must be the rubric + findings contract TEXT, never a
        # filesystem path (the flag takes a prompt string, not a file).
        self.assertIn("RUBRIC_TEXT_MARKER", value)
        self.assertIn("findings.json", value)
        self.assertNotIn(
            "instructions.md",
            value,
            "--append-system-prompt must receive the instruction TEXT, not a "
            "path — passing a path delivers the filename to the model and the "
            "rubric/output-contract never arrive.",
        )

    def test_permission_gate_is_bypassed(self) -> None:
        argv = self._capture()["argv"]
        self.assertIn("--permission-mode", argv)
        idx = argv.index("--permission-mode")
        self.assertEqual(
            argv[idx + 1],
            "bypassPermissions",
            "Headless Claude Code must bypass the permission gate or the Write "
            "tool that emits findings.json is denied in non-interactive CI.",
        )

    def test_user_prompt_goes_via_stdin_not_argv(self) -> None:
        captured = self._capture()
        argv, kwargs = captured["argv"], captured["kwargs"]
        # `-p` present with no positional prompt after it (next token is a flag).
        self.assertIn("-p", argv)
        p_idx = argv.index("-p")
        self.assertTrue(argv[p_idx + 1].startswith("-"))
        stdin_input = kwargs.get("stdin_input")
        self.assertIsNotNone(stdin_input)
        self.assertIn("# PR Context", stdin_input)

    def test_model_and_extra_args_still_applied(self) -> None:
        argv = self._capture(model="claude-opus-4-8", extra_args="--foo")[
            "argv"
        ]
        self.assertIn("--model", argv)
        self.assertEqual(argv[argv.index("--model") + 1], "claude-opus-4-8")
        self.assertIn("--foo", argv)

    def test_model_auto_is_not_forwarded(self) -> None:
        argv = self._capture(model="auto")["argv"]
        self.assertNotIn(
            "--model",
            argv,
            "model 'auto' means 'let the CLI pick its default' — no --model.",
        )

    def test_mcp_config_flag_added_when_set(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            mcp_src = str(Path(td) / "mcp.json")
            Path(mcp_src).write_text('{"mcpServers":{}}', encoding="utf-8")
            captured = _capture_provider_call(
                reviewer.ClaudeCodeProvider(
                    api_key="k", model="", mcp_config_file=mcp_src
                )
            )
            argv = captured["argv"]
            self.assertIn(
                "--mcp-config",
                argv,
                "Claude Code only loads MCP from --mcp-config; a bare copy to "
                "~/.claude/mcp.json is ignored.",
            )
            self.assertEqual(argv[argv.index("--mcp-config") + 1], mcp_src)

    def test_no_mcp_config_flag_when_unset(self) -> None:
        argv = self._capture()["argv"]
        self.assertNotIn("--mcp-config", argv)


class CodexInvocationTests(unittest.TestCase):
    """CodexProvider must escape the default read-only sandbox and pipe the
    prompt via stdin."""

    def _capture(
        self,
        *,
        model: str = "",
        extra_args: str = "",
        mcp_config_file: str = "",
    ) -> dict[str, Any]:
        return _capture_provider_call(
            reviewer.CodexProvider(
                api_key="k",
                model=model,
                extra_args=extra_args,
                mcp_config_file=mcp_config_file,
            )
        )

    def test_sandbox_is_escaped(self) -> None:
        argv = self._capture()["argv"]
        self.assertIn(
            "--dangerously-bypass-approvals-and-sandbox",
            argv,
            "codex exec defaults to a read-only sandbox; without escaping it "
            "the agent cannot write findings.json and every review fails.",
        )

    def test_prompt_via_stdin_sentinel(self) -> None:
        captured = self._capture()
        argv, kwargs = captured["argv"], captured["kwargs"]
        self.assertEqual(
            argv[-1],
            "-",
            "codex reads the prompt from stdin when the final positional is "
            "'-'; embedding it in argv risks E2BIG on large diffs.",
        )
        stdin_input = kwargs.get("stdin_input")
        self.assertIsNotNone(stdin_input)
        self.assertIn("# PR Context", stdin_input)
        # No argv token should carry the large prompt body.
        self.assertFalse(
            any("# PR Context" in tok for tok in argv),
            "The PR prompt must not appear in argv — it goes via stdin.",
        )

    def test_extra_args_precede_stdin_sentinel(self) -> None:
        argv = self._capture(extra_args="--foo")["argv"]
        self.assertIn("--foo", argv)
        self.assertLess(
            argv.index("--foo"),
            argv.index("-"),
            "extra_args must come before the '-' stdin sentinel.",
        )

    def test_mcp_config_file_does_not_copy_ignored_json(self) -> None:
        calls: list[tuple[str, Path]] = []

        def fake_swap(src_file: str, dest_path: Path) -> tuple[Path | None, str | None]:
            calls.append((src_file, dest_path))
            return None, None

        orig = reviewer._swap_mcp_config
        reviewer._swap_mcp_config = fake_swap  # type: ignore[assignment]
        try:
            with tempfile.TemporaryDirectory() as td:
                mcp_src = Path(td) / "mcp.json"
                mcp_src.write_text('{"mcpServers":{}}', encoding="utf-8")
                self._capture(mcp_config_file=str(mcp_src))
        finally:
            reviewer._swap_mcp_config = orig  # type: ignore[assignment]

        self.assertEqual(
            calls,
            [],
            "Codex ignores JSON MCP files and runs with an isolated CODEX_HOME; "
            "provider=codex must warn without copying to ~/.codex/mcp.json.",
        )


class CodexAuthJsonTests(unittest.TestCase):
    """Codex CLI 0.122+ ignores OPENAI_API_KEY from env and reads
    credentials from $CODEX_HOME/auth.json. The provider must
    materialize that file per-run in an isolated CODEX_HOME."""

    def _capture(self) -> dict[str, Any]:
        return _capture_codex_call_with_auth_state(
            reviewer.CodexProvider(api_key="sk-test-abc", model="")
        )

    def test_codex_home_is_set_in_subprocess_env(self) -> None:
        c = self._capture()
        self.assertTrue(
            c["codex_home_present_in_env"],
            "CODEX_HOME must be forwarded to the codex subprocess or "
            "Codex 0.122+ falls back to ~/.codex/ which may hold a "
            "ChatGPT-mode auth.json that overrides our apikey.",
        )
        self.assertTrue(
            str(c["codex_home_path"]).startswith(tempfile.gettempdir())
            or "aiprr-codex-" in str(c["codex_home_path"]),
            f"CODEX_HOME should be an isolated tempdir, got "
            f"{c['codex_home_path']}.",
        )

    def test_openai_api_key_is_still_forwarded(self) -> None:
        # Back-compat: pre-0.122 Codex still reads OPENAI_API_KEY from
        # env. Forwarding it costs nothing.
        c = self._capture()
        self.assertEqual(
            c["env"].get("OPENAI_API_KEY"),
            "sk-test-abc",
            "OPENAI_API_KEY must stay forwarded for back-compat with "
            "Codex CLI versions before 0.122.",
        )

    def test_auth_json_exists_at_invocation(self) -> None:
        c = self._capture()
        self.assertTrue(
            c["auth_json_exists_at_invocation"],
            "$CODEX_HOME/auth.json must exist when codex exec is "
            "invoked — this is exactly what fixes the 401 "
            "'Missing bearer or basic authentication in header'.",
        )

    def test_auth_json_shape_is_apikey_mode(self) -> None:
        c = self._capture()
        payload: dict[str, Any] = json.loads(c["auth_json_content"])
        self.assertIn(
            "OPENAI_API_KEY",
            payload,
            "Codex apikey-mode auth.json must carry the OPENAI_API_KEY "
            "field verbatim (per the paperclipai/paperclip#5276 fix "
            "and the clauditor#177 workaround).",
        )
        self.assertEqual(
            payload["OPENAI_API_KEY"],
            "sk-test-abc",
            "The materialized auth.json must contain the provider's "
            "own api_key, not a leftover value from another test.",
        )

    def test_auth_json_permissions_are_0600(self) -> None:
        c = self._capture()
        self.assertEqual(
            c["auth_json_mode"],
            0o600,
            "auth.json must be readable only by the runner user — a "
            "shared runner could otherwise leak the OPENAI_API_KEY to "
            "another job's process.",
        )

    def test_codex_home_directory_permissions_are_0700(self) -> None:
        c = self._capture()
        self.assertEqual(
            c["codex_home_mode"],
            0o700,
            "CODEX_HOME must be private to the runner user (tempfile "
            "already defaults to 0700 on Unix; this test locks it in "
            "as an invariant).",
        )

    def test_codex_home_is_removed_after_run_review(self) -> None:
        c = self._capture()
        # After run_review returns, the finally-block cleanup must have
        # removed the tempdir. This is the state the runner is left in.
        self.assertFalse(
            c["codex_home_path"].exists(),
            "CODEX_HOME must be removed after run_review returns so "
            "self-hosted runners don't accumulate stale api-key state.",
        )


class ClaudeCodeSubscriptionAuthTests(unittest.TestCase):
    """`api-key` maps to metered API auth OR subscription OAuth auth based on
    the token prefix — so a Claude Pro/Max subscription can bill the review
    instead of API usage (parallel to Cursor's subscription model)."""

    def test_api_key_maps_to_anthropic_api_key(self) -> None:
        p = reviewer.ClaudeCodeProvider(
            api_key="sk-ant-api03-abc123", model=""
        )
        self.assertEqual(
            p.auth_env_vars(), {"ANTHROPIC_API_KEY": "sk-ant-api03-abc123"}
        )

    def test_oauth_token_maps_to_oauth_env(self) -> None:
        p = reviewer.ClaudeCodeProvider(
            api_key="sk-ant-oat01-subtoken", model=""
        )
        self.assertEqual(
            p.auth_env_vars(),
            {"CLAUDE_CODE_OAUTH_TOKEN": "sk-ant-oat01-subtoken"},
        )

    def test_oauth_token_never_sets_api_key_var(self) -> None:
        """Regression: an OAuth token must NOT be exported as
        ANTHROPIC_API_KEY, or Claude Code would try metered API auth with a
        subscription token and fail."""
        p = reviewer.ClaudeCodeProvider(
            api_key="sk-ant-oat01-subtoken", model=""
        )
        self.assertNotIn("ANTHROPIC_API_KEY", p.auth_env_vars())

    def test_oauth_env_forwarded_into_subprocess_env(self) -> None:
        captured = _capture_provider_call(
            reviewer.ClaudeCodeProvider(
                api_key="sk-ant-oat01-subtoken", model=""
            )
        )
        env = captured["kwargs"]["env"]
        self.assertEqual(env.get("CLAUDE_CODE_OAUTH_TOKEN"), "sk-ant-oat01-subtoken")
        self.assertNotIn("ANTHROPIC_API_KEY", env)

if __name__ == "__main__":
    unittest.main()
