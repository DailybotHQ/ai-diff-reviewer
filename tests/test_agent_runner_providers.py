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

    def test_grok_constants(self) -> None:
        self.assertEqual(reviewer.GrokProvider.CLI_BIN, "grok")
        self.assertEqual(reviewer.GrokProvider.CLI_NAME, "xAI Grok")
        self.assertEqual(reviewer.GrokProvider.PROVIDER_ID, "grok")


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


class PromptV3DirectiveTests(unittest.TestCase):
    """Prompt v3 (Task 12): the agent-runner directive keeps only the
    file-safety rule (the triage/verification budget lives in the prompt,
    never duplicated), and the agent-runner closing asks for triage."""

    def test_directive_has_file_safety_rule_but_no_duplicated_budget(self) -> None:
        d = reviewer.write_findings_prompt_directive("RUBRIC", Path("/tmp/f.json"))
        self.assertIn("Never modify any file other than the findings file", d)
        self.assertNotIn("Exploration budget", d)

    def test_agent_runner_closing_mentions_triage_and_slices(self) -> None:
        text = reviewer.render_user_prompt(_make_pr_context(), for_agent_runner=True)
        self.assertIn("triage", text)
        self.assertIn("read slices, not whole trees", text)

    def test_bundled_prompt_carries_v3_sections(self) -> None:
        prompt = (_ROOT / "prompts" / "default.md").read_text(encoding="utf-8")
        for heading in ("## Plan the review first (triage)", "## Verification budget", "## Calibration: things that look like bugs but usually are not", "### Finding shape", "## Follow-up reviews", "## Severity definitions", "## What NOT to comment on"):
            self.assertIn(heading, prompt, heading)
        self.assertIn("It does **not** decide severity", prompt)
        self.assertIn("Always finish the session by calling `submit_review` exactly once", prompt)
        self.assertEqual(prompt, (_ROOT / "skills" / "ai-diff-reviewer" / "prompt.md").read_text(encoding="utf-8"), "skill prompt must be byte-identical")


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


class ClaudeCodeCustomBackendTests(unittest.TestCase):
    """`api-base` on claude-code switches to the Anthropic-compatible-backend
    env contract (Z.ai GLM / xAI). The default profile must stay
    byte-identical — locked by snapshot assertions below."""

    ZAI = "https://api.z.ai/api/anthropic"

    def _zai_provider(self, *, api_key: str = "zai-KEY", model: str = "glm-5.3") -> Any:
        prof = reviewer.resolve_endpoint_profile(self.ZAI, "claude-code")
        return reviewer.ClaudeCodeProvider(api_key=api_key, model=model, profile=prof)

    def test_default_profile_env_snapshot_api_key(self) -> None:
        captured = _capture_provider_call(
            reviewer.ClaudeCodeProvider(api_key="sk-ant-api03-x", model="")
        )
        env = captured["kwargs"]["env"]
        self.assertEqual(env.get("ANTHROPIC_API_KEY"), "sk-ant-api03-x")
        for name in (reviewer.CLAUDE_CODE_AUTH_TOKEN_ENV, reviewer.CLAUDE_CODE_BASE_URL_ENV,
                     reviewer.CLAUDE_CODE_API_TIMEOUT_ENV, *reviewer.CLAUDE_CODE_DEFAULT_MODEL_ENVS):
            self.assertNotIn(name, env)
        self.assertNotIn("--model", captured["argv"])

    def test_default_profile_env_snapshot_oauth(self) -> None:
        captured = _capture_provider_call(
            reviewer.ClaudeCodeProvider(api_key="sk-ant-oat01-tok", model="auto")
        )
        env = captured["kwargs"]["env"]
        self.assertEqual(env.get("CLAUDE_CODE_OAUTH_TOKEN"), "sk-ant-oat01-tok")
        self.assertNotIn("ANTHROPIC_API_KEY", env)
        self.assertNotIn("--model", captured["argv"])

    def test_zai_profile_env_contract(self) -> None:
        env = self._zai_provider().auth_env_vars()
        self.assertEqual(env[reviewer.CLAUDE_CODE_AUTH_TOKEN_ENV], "zai-KEY")
        self.assertEqual(env[reviewer.CLAUDE_CODE_BASE_URL_ENV], self.ZAI)
        self.assertEqual(env[reviewer.CLAUDE_CODE_API_TIMEOUT_ENV], reviewer.CLAUDE_CODE_CUSTOM_BACKEND_TIMEOUT_MS)
        for name in reviewer.CLAUDE_CODE_DEFAULT_MODEL_ENVS:
            self.assertEqual(env[name], "glm-5.3")
        self.assertNotIn("ANTHROPIC_API_KEY", env)
        self.assertNotIn("CLAUDE_CODE_OAUTH_TOKEN", env)

    def test_zai_profile_forces_model_flag_and_env_reaches_subprocess(self) -> None:
        captured = _capture_provider_call(self._zai_provider())
        argv, env = captured["argv"], captured["kwargs"]["env"]
        self.assertIn("--model", argv)
        self.assertEqual(argv[argv.index("--model") + 1], "glm-5.3")
        self.assertEqual(env.get(reviewer.CLAUDE_CODE_BASE_URL_ENV), self.ZAI)
        self.assertNotIn("AIPRR_GH_TOKEN", env)
        self.assertNotIn("AIPRR_API_KEY", env)

    def test_auto_model_on_custom_backend_fails_fast(self) -> None:
        with self.assertRaises(ValueError) as ctx:
            self._zai_provider(model="auto").auth_env_vars()
        self.assertIn("glm-5.3", str(ctx.exception))
        with self.assertRaises(ValueError):
            self._zai_provider(model="").auth_env_vars()

    def test_subscription_token_on_custom_backend_fails_fast(self) -> None:
        with self.assertRaises(ValueError) as ctx:
            self._zai_provider(api_key="sk-ant-oat01-tok").auth_env_vars()
        self.assertIn("api.z.ai", str(ctx.exception))
        self.assertNotIn("sk-ant-oat01-tok", str(ctx.exception))

    def test_xai_anthropic_compatible_profile(self) -> None:
        prof = reviewer.resolve_endpoint_profile("https://api.x.ai", "claude-code")
        env = reviewer.ClaudeCodeProvider(api_key="xai-KEY", model="grok-4.3", profile=prof).auth_env_vars()
        self.assertEqual(env[reviewer.CLAUDE_CODE_BASE_URL_ENV], "https://api.x.ai")
        self.assertEqual(env["ANTHROPIC_DEFAULT_SONNET_MODEL"], "grok-4.3")


class CodexCustomBackendTests(unittest.TestCase):
    """`api-base` on codex materializes a `config.toml` in the isolated
    CODEX_HOME that routes the CLI to a Responses-API backend; the default
    profile writes no config and keeps argv/env byte-identical."""

    AZURE = "https://myres.services.ai.azure.com/openai/v1"

    def setUp(self) -> None:
        # Keep these hermetic: the catalog step shells out to `codex debug
        # models --bundled`; stub it as unavailable (covered separately).
        self._rc = mock.patch.object(reviewer, "run_cmd", return_value=_FakeCmd(1, ""))
        self._rc.start(); self.addCleanup(self._rc.stop)

    def _prov(self, base: str, *, model: str = "gpt-5.4-mini-azure", key: str = "az-KEY") -> Any:
        prof = reviewer.resolve_endpoint_profile(base, "codex")
        return reviewer.CodexProvider(api_key=key, model=model, profile=prof)

    def test_default_profile_writes_no_config_toml(self) -> None:
        c = _capture_codex_call_with_auth_state(reviewer.CodexProvider(api_key="k", model=""))
        self.assertFalse(c["config_toml_exists_at_invocation"])
        self.assertNotIn("--model", c["argv"])
        self.assertEqual(c["env"].get("OPENAI_API_KEY"), "k")

    def test_azure_profile_config_toml_content_and_perms(self) -> None:
        c = _capture_codex_call_with_auth_state(self._prov(self.AZURE))
        self.assertTrue(c["config_toml_exists_at_invocation"])
        self.assertEqual(c["config_toml_mode"], 0o600)
        toml = c["config_toml_content"]
        self.assertIn('model = "gpt-5.4-mini-azure"', toml)
        self.assertIn('model_provider = "aiprr"', toml)
        self.assertIn("[model_providers.aiprr]", toml)
        self.assertIn(f'base_url = "{self.AZURE}"', toml)
        self.assertIn('env_key = "OPENAI_API_KEY"', toml)
        self.assertIn('wire_api = "responses"', toml)
        self.assertIn(reviewer.AZURE_IMAGE_GEN_HEADER, toml)
        self.assertIn("[features]", toml)
        self.assertIn("image_generation = false", toml)
        # the key itself never lands in the TOML
        self.assertNotIn("az-KEY", toml)
        # --model forced, key still forwarded via env for env_key
        self.assertIn("--model", c["argv"])
        self.assertEqual(c["argv"][c["argv"].index("--model") + 1], "gpt-5.4-mini-azure")
        self.assertEqual(c["env"].get("OPENAI_API_KEY"), "az-KEY")
        self.assertTrue(c["auth_json_exists_at_invocation"])
        self.assertFalse(c["codex_home_path"].exists(), "CODEX_HOME must be cleaned up")

    def test_xai_and_zai_profiles_have_no_azure_block(self) -> None:
        for base, model in (("https://api.x.ai/v1", "grok-4.3"), ("https://api.z.ai/api/v1", "glm-5.3")):
            with self.subTest(base=base):
                c = _capture_codex_call_with_auth_state(self._prov(base, model=model))
                toml = c["config_toml_content"]
                self.assertIn(f'base_url = "{base}"', toml)
                self.assertNotIn(reviewer.AZURE_IMAGE_GEN_HEADER, toml)
                self.assertNotIn("[features]", toml)
                self.assertIn('wire_api = "responses"', toml)

    def test_model_required_on_custom_backend(self) -> None:
        for bad in ("", "auto"):
            with self.subTest(model=bad), self.assertRaises(ValueError):
                _capture_codex_call_with_auth_state(self._prov(self.AZURE, model=bad))

    def test_toml_escaping(self) -> None:
        esc = reviewer.CodexProvider._toml_escape
        self.assertEqual(esc('a"b'), 'a\\"b')
        self.assertEqual(esc("a\\b"), "a\\\\b")
        self.assertEqual(esc("a\nb"), "a\\nb")
        prof = reviewer.resolve_endpoint_profile("https://api.x.ai/v1", "codex")
        rendered = reviewer.CodexProvider.render_custom_provider_config(profile=prof, model='we"ird')
        self.assertIn('model = "we\\"ird"', rendered)

    def test_rendered_config_has_no_unescaped_newlines_in_strings(self) -> None:
        prof = reviewer.resolve_endpoint_profile(self.AZURE, "codex")
        rendered = reviewer.CodexProvider.render_custom_provider_config(profile=prof, model="m")
        for line in rendered.splitlines():
            if "=" in line and '"' in line:
                self.assertEqual(line.count('"') % 2, 0, line)


_FAKE_BUNDLED_CATALOG: dict[str, Any] = {
    "models": [
        {
            "slug": "gpt-6-astra", "display_name": "Astra", "description": "x",
            "visibility": "hidden", "supported_in_api": False, "priority": 9,
            "use_responses_lite": True, "supports_search_tool": True,
            "experimental_supported_tools": ["namespace"], "service_tiers": ["fast"],
            "additional_speed_tiers": ["x"], "include_apps_usage_instructions": True,
            "base_instructions": "BASE", "context_window": 1,
        },
        {
            "slug": "gpt-5.4", "display_name": "GPT-5.4", "description": "t",
            "visibility": "list", "supported_in_api": True, "priority": 3,
            "use_responses_lite": True, "supports_search_tool": True,
            "experimental_supported_tools": ["namespace", "apps"], "service_tiers": ["fast"],
            "additional_speed_tiers": ["x"], "include_apps_usage_instructions": True,
            "web_search_tool_type": "preview", "base_instructions": "BASE54", "context_window": 2,
            "upgrade": {"model": "gpt-5.6-terra", "migration_markdown": "moved"},
        },
        {
            "slug": "gpt-5.6-luna", "display_name": "Luna", "description": "l",
            "visibility": "list", "supported_in_api": True, "priority": 2,
            "use_responses_lite": True, "supports_search_tool": True,
            "experimental_supported_tools": ["namespace"], "service_tiers": ["fast"],
            "additional_speed_tiers": ["x"], "include_apps_usage_instructions": True,
            "web_search_tool_type": "text_and_image", "base_instructions": "BASELUNA",
            "context_window": 3, "upgrade": None, "availability_nux": None,
        },
    ]
}


class _FakeCmd:
    def __init__(self, returncode: int, stdout: str) -> None:
        self.returncode = returncode; self.stdout = stdout; self.stderr = ""


class CodexModelCatalogTests(unittest.TestCase):
    """Custom backends get a cloned model catalog so Codex does not send
    OpenAI-only tool types (xAI rejects `tools[].type: namespace`)."""

    XAI = "https://api.x.ai/v1"

    def test_entry_cloned_from_preferred_template_with_safe_overrides(self) -> None:
        entry = reviewer.CodexProvider.build_model_catalog_entry(_FAKE_BUNDLED_CATALOG, model="grok-4.3", kind="xai")
        assert entry is not None
        self.assertEqual(entry["slug"], "grok-4.3")
        self.assertEqual(entry["display_name"], "grok-4.3")
        self.assertEqual(entry["base_instructions"], "BASELUNA", "preferred template is the first upgrade-free entry (gpt-5.6-luna)")
        # a non-null template field is never overwritten with null …
        self.assertEqual(entry["web_search_tool_type"], "text_and_image")
        # … but a nullable one may stay null
        self.assertIsNone(entry["availability_nux"])
        self.assertEqual(entry["visibility"], "list")
        self.assertTrue(entry["supported_in_api"])
        self.assertFalse(entry["use_responses_lite"])
        self.assertFalse(entry["supports_search_tool"])
        self.assertEqual(entry["experimental_supported_tools"], [])
        self.assertEqual(entry["service_tiers"], [])
        self.assertFalse(entry["include_apps_usage_instructions"])
        # keys absent from the template are never invented
        self.assertNotIn("multi_agent_version", entry)
        # the bundled catalog object is not mutated
        self.assertEqual(_FAKE_BUNDLED_CATALOG["models"][1]["slug"], "gpt-5.4")

    def test_entry_falls_back_to_first_model_and_none_when_empty(self) -> None:
        only_astra = {"models": [_FAKE_BUNDLED_CATALOG["models"][0]]}
        entry = reviewer.CodexProvider.build_model_catalog_entry(only_astra, model="m", kind="xai")
        assert entry is not None
        self.assertEqual(entry["base_instructions"], "BASE")

    def test_templates_with_an_upgrade_redirect_are_skipped(self) -> None:
        bundled = {"models": [_FAKE_BUNDLED_CATALOG["models"][1], _FAKE_BUNDLED_CATALOG["models"][0]]}
        entry = reviewer.CodexProvider.build_model_catalog_entry(bundled, model="m", kind="xai")
        assert entry is not None
        self.assertEqual(entry["base_instructions"], "BASE", "gpt-5.4 carries an upgrade block and must not be the template")
        self.assertIsNone(reviewer.CodexProvider.build_model_catalog_entry({"models": []}, model="m", kind="xai"))

    def test_catalog_written_and_referenced_from_config(self) -> None:
        prof = reviewer.resolve_endpoint_profile(self.XAI, "codex")
        prov = reviewer.CodexProvider(api_key="k", model="grok-4.3", profile=prof)
        with mock.patch.object(reviewer, "run_cmd", return_value=_FakeCmd(0, json.dumps(_FAKE_BUNDLED_CATALOG))):
            c = _capture_codex_call_with_auth_state(prov)
        self.assertTrue(c["catalog_exists_at_invocation"])
        catalog = json.loads(c["catalog_content"])
        self.assertEqual([m["slug"] for m in catalog["models"]], ["grok-4.3"])
        self.assertIn("model_catalog_json = ", c["config_toml_content"])
        self.assertIn("models.json", c["config_toml_content"])

    def test_catalog_unavailable_degrades_to_no_catalog(self) -> None:
        prof = reviewer.resolve_endpoint_profile(self.XAI, "codex")
        prov = reviewer.CodexProvider(api_key="k", model="grok-4.3", profile=prof)
        with mock.patch.object(reviewer, "run_cmd", return_value=_FakeCmd(1, "")), \
             mock.patch.object(reviewer, "log") as fake_log:
            c = _capture_codex_call_with_auth_state(prov)
        self.assertFalse(c["catalog_exists_at_invocation"])
        self.assertTrue(c["config_toml_exists_at_invocation"])
        self.assertNotIn("model_catalog_json", c["config_toml_content"])
        self.assertTrue(any("catalog" in str(call.args[0]) for call in fake_log.call_args_list))

    def test_non_json_catalog_degrades(self) -> None:
        prof = reviewer.resolve_endpoint_profile(self.XAI, "codex")
        prov = reviewer.CodexProvider(api_key="k", model="grok-4.3", profile=prof)
        with mock.patch.object(reviewer, "run_cmd", return_value=_FakeCmd(0, "not json")):
            c = _capture_codex_call_with_auth_state(prov)
        self.assertFalse(c["catalog_exists_at_invocation"])


class CodexCustomToolWarningTests(unittest.TestCase):
    """Codex 0.154 emits a `custom` (freeform apply_patch) tool that some
    Responses gateways reject; the runtime warns on those kinds, never blocks."""

    def _run(self, base: str) -> list[str]:
        prof = reviewer.resolve_endpoint_profile(base, "codex")
        prov = reviewer.CodexProvider(api_key="k", model="m", profile=prof)
        with mock.patch.object(reviewer, "run_cmd", return_value=_FakeCmd(1, "")), \
             mock.patch.object(reviewer, "log") as fake_log:
            _capture_codex_call_with_auth_state(prov)
        return [str(c.args[0]) for c in fake_log.call_args_list]

    def test_warns_on_xai_and_custom_hosts(self) -> None:
        for base in ("https://api.x.ai/v1", "https://api.z.ai/api/v1", "https://gateway.example/v1"):
            with self.subTest(base=base):
                msgs = self._run(base)
                self.assertTrue(any("custom" in m and "422" in m for m in msgs), msgs)

    def test_no_warning_on_azure(self) -> None:
        msgs = self._run("https://myres.services.ai.azure.com/openai/v1")
        self.assertFalse(any("422" in m for m in msgs), msgs)


def _capture_grok_call(provider: Any) -> dict[str, Any]:
    """Like `_capture_provider_call` but also snapshots the prompt file
    (it lives in a mkdtemp() dir removed after run_review returns)."""
    captured: dict[str, Any] = {}

    def fake_invoke(*, argv: list[str], **kwargs: Any) -> Any:
        captured["argv"] = list(argv)
        captured["kwargs"] = dict(kwargs)
        captured["env"] = dict(kwargs.get("env", {}))
        idx = argv.index("--prompt-file")
        prompt_path = Path(argv[idx + 1])
        captured["prompt_path"] = prompt_path
        captured["prompt_exists_at_invocation"] = prompt_path.exists()
        if prompt_path.exists():
            captured["prompt_content"] = prompt_path.read_text(encoding="utf-8")
            captured["prompt_mode"] = prompt_path.stat().st_mode & 0o777
            captured["prompt_dir_mode"] = prompt_path.parent.stat().st_mode & 0o777
        return reviewer.ReviewResult(summary="ok", findings=[])

    orig = reviewer._invoke_cli_agent
    reviewer._invoke_cli_agent = fake_invoke  # type: ignore[assignment]
    try:
        with tempfile.TemporaryDirectory() as td:
            workspace = Path(td)
            provider.run_review(
                pr_context=_make_pr_context(),
                review_instructions="RUBRIC_TEXT_MARKER",
                workspace=workspace,
                output_dir=workspace,
            )
    finally:
        reviewer._invoke_cli_agent = orig  # type: ignore[assignment]
    return captured


class GrokInvocationTests(unittest.TestCase):
    """GrokProvider: rubric via --rules (text), diff via --prompt-file
    (0600 temp file), hardening defaults, XAI_API_KEY-only env."""

    def _capture(self, *, model: str = "", extra_args: str = "", mcp: str = "") -> dict[str, Any]:
        return _capture_grok_call(
            reviewer.GrokProvider(api_key="xai-KEY", model=model, extra_args=extra_args, mcp_config_file=mcp)
        )

    def test_rules_carry_instruction_text_and_findings_contract(self) -> None:
        argv = self._capture()["argv"]
        idx = argv.index("--rules")
        self.assertIn("RUBRIC_TEXT_MARKER", argv[idx + 1])
        self.assertIn("findings.json", argv[idx + 1])

    def test_prompt_file_exists_private_and_carries_pr_context(self) -> None:
        c = self._capture()
        self.assertTrue(c["prompt_exists_at_invocation"])
        self.assertIn("# PR Context", c["prompt_content"])
        self.assertEqual(c["prompt_mode"], 0o600)
        self.assertEqual(c["prompt_dir_mode"], 0o700)
        self.assertIsNone(c["kwargs"].get("stdin_input"))
        self.assertFalse(c["prompt_path"].exists(), "temp prompt dir must be removed after run_review")

    def test_hardening_defaults_present(self) -> None:
        argv = self._capture()["argv"]
        for flag in ("--always-approve", "--disable-web-search", "--no-subagents", "--no-plan"):
            self.assertIn(flag, argv)
        self.assertEqual(argv[argv.index("--output-format") + 1], "json")

    def test_model_flag_and_auto(self) -> None:
        argv = self._capture(model="grok-4.6")["argv"]
        self.assertEqual(argv[argv.index("-m") + 1], "grok-4.6")
        self.assertNotIn("-m", self._capture(model="auto")["argv"])

    def test_extra_args_appended_after_defaults(self) -> None:
        argv = self._capture(extra_args="--reasoning-effort high")["argv"]
        self.assertGreater(argv.index("--reasoning-effort"), argv.index("--no-plan"))
        self.assertEqual(argv[argv.index("--reasoning-effort") + 1], "high")

    def test_env_is_allowlist_plus_xai_key_only(self) -> None:
        prev = dict(os.environ)
        try:
            os.environ["AIPRR_GH_TOKEN"] = "ghp_leak"
            os.environ["AIPRR_API_KEY"] = "leak"
            env = self._capture()["env"]
        finally:
            os.environ.clear(); os.environ.update(prev)
        self.assertEqual(env.get("XAI_API_KEY"), "xai-KEY")
        self.assertNotIn("AIPRR_GH_TOKEN", env)
        self.assertNotIn("AIPRR_API_KEY", env)
        for name in env:
            self.assertTrue(name in reviewer._CLI_ENV_ALLOWLIST or name == "XAI_API_KEY", name)

    def test_mcp_and_api_base_warn(self) -> None:
        with mock.patch.object(reviewer, "log") as fake_log:
            self._capture(mcp="/tmp/mcp.json")
            reviewer.build_provider("grok", api_key="k", model="", api_base="https://api.x.ai/v1")
        msgs = " ".join(str(c.args[0]) for c in fake_log.call_args_list)
        self.assertIn("mcp-config-file", msgs)
        self.assertIn("api-base", msgs)

    def test_dispatch_and_default_model(self) -> None:
        p = reviewer.build_provider("grok", api_key="k", model="")
        self.assertIsInstance(p, reviewer.GrokProvider)
        self.assertIsInstance(p, reviewer.AgentRunnerProvider)
        self.assertEqual(reviewer.DEFAULT_MODELS["grok"], "grok-4.3")
        self.assertIn("grok", reviewer.PROVIDERS_WITHOUT_API_BASE)


if __name__ == "__main__":
    unittest.main()
