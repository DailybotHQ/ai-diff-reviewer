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
        self.assertEqual(reviewer.DEFAULT_MODELS["grok"], "grok-4.5")  # v2.3.0: benchmark-driven (was grok-4.3)
        self.assertIn("grok", reviewer.PROVIDERS_WITHOUT_API_BASE)


if __name__ == "__main__":
    unittest.main()


class DefaultProfileBackCompatSnapshotTests(unittest.TestCase):
    """Default-profile argv/env for the three pre-existing CLI runners,
    captured from `main` before the multi-backend work (2026-09-16) and
    stored as literals. Documented, intentional deltas are listed per
    runner; anything else is a back-compat regression."""

    # Captured on `main` with model="" and no extra args. Prompt/system text
    # arguments are elided as <TEXT> (their content is covered elsewhere).
    MAIN_SNAPSHOT: dict[str, dict[str, Any]] = {
        "claude-code": {
            "argv": ["claude", "-p", "--append-system-prompt", "<TEXT>", "--output-format",
                     "stream-json", "--verbose", "--permission-mode", "bypassPermissions"],
            "env": {"ANTHROPIC_API_KEY": "sk-test-KEY"},
            "stdin": True,
        },
        "cursor": {
            # v2.2.0: `text` → `json` so usage telemetry can be read (findings still come from the file).
            "argv": ["cursor-agent", "-p", "--output-format", "json", "--force", "--trust"],
            "env": {"CURSOR_API_KEY": "sk-test-KEY"},
            "stdin": True,
        },
        "codex": {
            "argv": ["codex", "exec", "--skip-git-repo-check",
                     "--dangerously-bypass-approvals-and-sandbox", "-"],
            "env": {"OPENAI_API_KEY": "sk-test-KEY", "CODEX_HOME": "<TMP>"},
            "stdin": True,
        },
    }
    # Intentional deltas vs main (task → change). Keep this list honest.
    DOCUMENTED_DELTAS: dict[str, list[str]] = {
        "codex": ["--json"],  # Task 10: JSONL events on stdout carry usage telemetry
    }

    def _capture(self, pid: str) -> dict[str, Any]:
        captured: dict[str, Any] = {}

        def fake_run(argv, **kw):
            captured["argv"] = list(argv); captured["env"] = dict(kw["env"]); captured["stdin"] = kw.get("input") is not None
            fp = Path(kw["cwd"]) / reviewer.FINDINGS_JSON_REL
            fp.parent.mkdir(parents=True, exist_ok=True)
            fp.write_text(json.dumps({"summary": "s", "findings": []}))
            return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

        keep = {k: v for k, v in os.environ.items() if k in ("PATH", "HOME")}
        with mock.patch.dict(os.environ, keep, clear=True), tempfile.TemporaryDirectory() as tmp, \
                mock.patch.object(reviewer, "_run_cli_process", side_effect=fake_run), mock.patch.object(reviewer, "log"):
            provider = reviewer.build_provider(pid, api_key="sk-test-KEY", model="")
            provider.run_review(pr_context=_make_pr_context(), review_instructions="R", workspace=Path(tmp), output_dir=Path(tmp))
        argv = ["<TEXT>" if len(x) >= 200 else x for x in captured["argv"]]
        env = {k: ("<TMP>" if k == "CODEX_HOME" else v) for k, v in captured["env"].items() if k not in ("PATH", "HOME")}
        return {"argv": argv, "env": env, "stdin": captured["stdin"]}

    def test_default_profiles_match_main_snapshot_modulo_documented_deltas(self) -> None:
        for pid, expected in self.MAIN_SNAPSHOT.items():
            with self.subTest(runner=pid):
                got = self._capture(pid)
                argv = [x for x in got["argv"] if x not in self.DOCUMENTED_DELTAS.get(pid, [])]
                self.assertEqual(argv, expected["argv"])
                for flag in self.DOCUMENTED_DELTAS.get(pid, []):
                    self.assertIn(flag, got["argv"], f"{pid}: documented delta {flag} missing")
                self.assertEqual(got["env"], expected["env"])
                self.assertEqual(got["stdin"], expected["stdin"])

if __name__ == "__main__":
    unittest.main()
