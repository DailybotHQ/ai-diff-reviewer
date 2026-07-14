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
        for provider_id in ("anthropic", "claude-code", "cursor", "codex"):
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
            api_key="AK", model="gpt-5-codex", extra_args="", mcp_config_file=""
        )
        self.assertEqual(p.model, "gpt-5-codex")

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
            findings_path = _write_findings(
                tmp,
                {"summary": "ok", "findings": []},
            )
            # Use `python3 -c "pass"` — always exits 0.
            argv = ["python3", "-c", "pass"]
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

    def test_missing_findings_after_success_raises_from_parser(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            # Exit 0 but no findings file written.
            argv = ["python3", "-c", "pass"]
            with self.assertRaises(FileNotFoundError):
                reviewer._invoke_cli_agent(
                    argv=argv,
                    workspace=tmp,
                    findings_path=tmp / ".aiprr" / "findings.json",
                    env={**os.environ},
                    cli_name="TestCLI",
                )


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


class CliEnvAllowlistTests(unittest.TestCase):
    """`_build_cli_env` forwards only the allowlist + provided extras.

    Prevents leaking AIPRR_GH_TOKEN and other consumer secrets into the
    vendor CLI subprocess. See Security Review §2.
    """

    def test_allowlist_only_forwarded(self) -> None:
        prev = dict(os.environ)
        try:
            # Populate a mix of allowed and disallowed vars.
            os.environ.clear()
            os.environ.update(
                {
                    "PATH": "/usr/bin",
                    "HOME": "/root",
                    "AIPRR_GH_TOKEN": "ghp_secret",
                    "AIPRR_API_KEY": "sk-secret",
                    "MY_CUSTOM_LEAK": "leak-me",
                }
            )
            env = reviewer._build_cli_env(extra_vars={"VENDOR_KEY": "vk"})
            self.assertEqual(env.get("PATH"), "/usr/bin")
            self.assertEqual(env.get("HOME"), "/root")
            self.assertEqual(env.get("VENDOR_KEY"), "vk")
            self.assertNotIn("AIPRR_GH_TOKEN", env)
            self.assertNotIn("AIPRR_API_KEY", env)
            self.assertNotIn("MY_CUSTOM_LEAK", env)
        finally:
            os.environ.clear()
            os.environ.update(prev)

    def test_extra_vars_override_missing_from_env(self) -> None:
        env = reviewer._build_cli_env(extra_vars={"ANTHROPIC_API_KEY": "AK"})
        self.assertEqual(env["ANTHROPIC_API_KEY"], "AK")

    def test_no_gh_token_ever_reaches_env(self) -> None:
        prev = dict(os.environ)
        try:
            os.environ["AIPRR_GH_TOKEN"] = "ghp_should_not_leak"
            env = reviewer._build_cli_env(
                extra_vars={"OPENAI_API_KEY": "sk-x"}
            )
            self.assertNotIn("AIPRR_GH_TOKEN", env)
        finally:
            os.environ.clear()
            os.environ.update(prev)


class SecurityInvariantsTests(unittest.TestCase):
    """No shell=True, all agent-extra-args go through shlex.split."""

    def test_no_shell_true_in_reviewer_py(self) -> None:
        """`shell=True` must not appear in any actual subprocess call.

        Filters out docstring/comment references (e.g. "argv-list form
        (no `shell=True`)") — those are documentation, not code paths.
        """
        source: str = (_ROOT / "scripts" / "reviewer.py").read_text(
            encoding="utf-8"
        )
        code_lines: list[str] = []
        for line in source.splitlines():
            stripped = line.strip()
            if stripped.startswith("#"):
                continue
            if "`shell=True`" in stripped:
                continue
            code_lines.append(line)
        code_only: str = "\n".join(code_lines)
        self.assertNotIn(
            "shell=True",
            code_only,
            "shell=True is banned — every subprocess call must use argv-list "
            "form. See docs/SECURITY.md.",
        )

    def test_no_bare_os_system(self) -> None:
        source: str = (_ROOT / "scripts" / "reviewer.py").read_text(
            encoding="utf-8"
        )
        self.assertNotIn(
            "os.system(",
            source,
            "os.system() is banned — use subprocess.run with argv-list.",
        )

    def test_extra_args_flows_through_shlex(self) -> None:
        """Every provider that accepts extra_args uses shlex.split."""
        source: str = (_ROOT / "scripts" / "reviewer.py").read_text(
            encoding="utf-8"
        )
        # Each of the three CLI providers should have `shlex.split(self.extra_args)`
        occurrences: int = source.count("shlex.split(self.extra_args)")
        self.assertGreaterEqual(
            occurrences,
            3,
            "Each of the 3 CLI providers must funnel extra_args through "
            "shlex.split — never string-concat into argv.",
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

    def _capture(self, *, model: str = "", extra_args: str = "") -> dict[str, Any]:
        return _capture_provider_call(
            reviewer.CodexProvider(
                api_key="k", model=model, extra_args=extra_args
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


class AgentRunnerPromptHygieneTests(unittest.TestCase):
    """The agent-runner user prompt must NOT reference chat-completions-only
    tools (post_inline_comment / submit_review), which don't exist for a
    vendor CLI and would give it contradictory instructions."""

    def test_agent_runner_prompt_omits_chat_tools(self) -> None:
        text = reviewer.render_user_prompt(
            _make_pr_context(), for_agent_runner=True
        )
        self.assertNotIn("post_inline_comment", text)
        self.assertNotIn("submit_review", text)
        self.assertIn("findings file", text)

    def test_chat_prompt_still_references_tools(self) -> None:
        text = reviewer.render_user_prompt(_make_pr_context())
        self.assertIn("post_inline_comment", text)
        self.assertIn("submit_review", text)


if __name__ == "__main__":
    unittest.main()
