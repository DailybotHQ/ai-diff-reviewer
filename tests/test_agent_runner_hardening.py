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


class HardeningRegressionTests(unittest.TestCase):
    """Task 13 hardening: allowlist hygiene, bounded findings file, bounded
    ignore globs, and the credential lanes per runner."""

    def test_allowlist_has_no_credential_like_names(self) -> None:
        for name in reviewer._CLI_ENV_ALLOWLIST:
            low = name.lower()
            self.assertFalse(
                any(s in low for s in reviewer.LOG_REDACT_SUBSTRINGS),
                f"{name} looks like a credential and must not be forwarded",
            )
            self.assertFalse(name.startswith("AIPRR_"), name)

    def test_findings_file_above_cap_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "findings.json"
            path.write_text("{}" + " " * 16, encoding="utf-8")
            with mock.patch.object(reviewer, "MAX_FINDINGS_FILE_BYTES", 8):
                with self.assertRaises(ValueError) as ctx:
                    reviewer.parse_findings_file(path)
            self.assertIn("cap", str(ctx.exception))
            # Under the cap the same file parses (empty findings list is fine).
            path.write_text(json.dumps({"summary": "ok", "findings": []}), encoding="utf-8")
            self.assertEqual(reviewer.parse_findings_file(path).summary, "ok")

    def test_ignore_globs_are_capped(self) -> None:
        many = ",".join(f"dir{i}/**" for i in range(reviewer.MAX_IGNORE_GLOBS + 50))
        with mock.patch.object(reviewer, "log"):
            self.assertEqual(len(reviewer.parse_ignore_paths(many)), reviewer.MAX_IGNORE_GLOBS)
            long_glob = "a" * (reviewer.MAX_IGNORE_GLOB_LEN + 1)
            self.assertEqual(reviewer.parse_ignore_paths(f"{long_glob},keep.txt"), ("keep.txt",))

    def test_pathological_globs_match_in_bounded_time(self) -> None:
        """PR-controlled file names must not stall the matcher (ReDoS)."""
        import time
        cases = (
            ("**/" * 40 + "a", "b/" * 60 + "c"),
            ("*.*.*.*.*.*.*.*.*.*.ts", "a." * 120 + "x"),
            ("*a*a*a*a*a*a*a*a*a*a*b", "a" * 200),
        )
        for glob, path in cases:
            t0 = time.monotonic()
            self.assertFalse(reviewer.path_is_ignored(path, (glob,)))
            self.assertLess(time.monotonic() - t0, 0.5, glob)
        self.assertTrue(reviewer.path_is_ignored("a.b.c.d.e.f.g.h.i.j.ts", ("*.*.*.*.*.*.*.*.*.*.ts",)))

    def test_credential_lanes_per_agent_runner(self) -> None:
        """Each CLI receives exactly its own credential variable(s) and never
        the GitHub token or any other AIPRR_* variable."""
        expected: dict[str, set[str]] = {
            "claude-code": {"ANTHROPIC_API_KEY"},
            "codex": {"OPENAI_API_KEY", "CODEX_HOME"},
            "cursor": {"CURSOR_API_KEY"},
            "grok": {reviewer.GROK_API_KEY_ENV},
        }
        prev = dict(os.environ)
        try:
            os.environ["AIPRR_GH_TOKEN"] = "ghp_leak_value"
            os.environ["AIPRR_API_KEY"] = "leak_value"
            for pid, lanes in expected.items():
                provider = reviewer.build_provider(pid, api_key="sk-lane-KEY", model="")
                captured: dict[str, Any] = {}

                def fake_run(argv, **kw):
                    captured["env"] = dict(kw["env"])
                    findings = Path(kw["cwd"]) / reviewer.FINDINGS_JSON_REL
                    findings.parent.mkdir(parents=True, exist_ok=True)
                    findings.write_text(json.dumps({"summary": "s", "findings": []}))
                    return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

                with tempfile.TemporaryDirectory() as tmp, mock.patch.object(
                    reviewer.subprocess, "run", side_effect=fake_run
                ), mock.patch.object(reviewer, "log"):
                    provider.run_review(
                        pr_context=_make_pr_context(), review_instructions="R",
                        workspace=Path(tmp), output_dir=Path(tmp),
                    )
                env = captured["env"]
                extra = {k for k in env if k not in reviewer._CLI_ENV_ALLOWLIST}
                self.assertEqual(extra, lanes, pid)
                self.assertFalse(any(k.startswith("AIPRR_") for k in env), pid)
                self.assertNotIn("ghp_leak_value", " ".join(env.values()), pid)
        finally:
            os.environ.clear(); os.environ.update(prev)


class CursorApiBaseWarningTests(unittest.TestCase):
    def test_cursor_warns_and_ignores_api_base(self) -> None:
        with mock.patch.object(reviewer, "log") as fake_log:
            provider = reviewer.build_provider(
                "cursor", api_key="k", model="", api_base="https://gw.example.com/v1"
            )
        msgs = " ".join(str(c.args[0]) for c in fake_log.call_args_list)
        self.assertIn("api-base", msgs)
        self.assertIn("WARNING", msgs)
        self.assertEqual(provider.profile.host, "gw.example.com")

    def test_cursor_default_is_silent(self) -> None:
        with mock.patch.object(reviewer, "log") as fake_log:
            reviewer.build_provider("cursor", api_key="k", model="")
        self.assertFalse(any("api-base" in str(c.args[0]) for c in fake_log.call_args_list))


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

if __name__ == "__main__":
    unittest.main()
