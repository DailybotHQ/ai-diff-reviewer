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


class _FakeCmd:
    def __init__(self, returncode: int, stdout: str) -> None:
        self.returncode = returncode; self.stdout = stdout; self.stderr = ""


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

class CodexBackendGateTests(unittest.TestCase):
    """The v2.4.0 chat-completions backends are Codex-impossible (wire_api
    responses vs chat-completions surfaces): fail fast, never half-start."""

    def test_codex_cannot_reach_chat_completions_only_backends(self) -> None:
        for kind in ("deepseek", "moonshot", "minimax", "qwen", "gemini", "openrouter"):
            with self.subTest(kind=kind):
                with self.assertRaises(ValueError) as ctx:
                    reviewer._assert_codex_backend_supported(kind)
                self.assertIn("provider: openai", str(ctx.exception))

    def test_responses_capable_kinds_still_allowed(self) -> None:
        # Azure / Z.ai implement the Responses API; xAI and custom gateways
        # keep the existing freeform-tool warning path instead.
        for kind in ("azure", "zai", "xai", "custom"):
            reviewer._assert_codex_backend_supported(kind)  # must not raise

if __name__ == "__main__":
    unittest.main()
