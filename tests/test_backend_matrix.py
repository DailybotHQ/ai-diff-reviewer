#!/usr/bin/env python3
"""Unit tests for the backend contract: `api-base` validation and endpoint
profile resolution (`EndpointProfile`, `resolve_endpoint_profile`).

Pure logic — no network, no subprocess. The provider request paths that
consume the profile are covered by their own modules.
"""

from __future__ import annotations

import importlib.util
import io
import json
import sys
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

_ROOT: Path = Path(__file__).resolve().parent.parent
_SPEC = importlib.util.spec_from_file_location(
    "reviewer", _ROOT / "scripts" / "reviewer.py"
)
assert _SPEC is not None and _SPEC.loader is not None
reviewer = importlib.util.module_from_spec(_SPEC)
sys.modules["reviewer"] = reviewer
_SPEC.loader.exec_module(reviewer)


ALL_PROVIDERS: tuple[str, ...] = (
    "anthropic",
    "claude-code",
    "cursor",
    "codex",
    "openai",
    "grok",
)

def _ctx():
    import dataclasses
    vals = {}
    for f in dataclasses.fields(reviewer.PRContext):
        if f.default is not dataclasses.MISSING or f.default_factory is not dataclasses.MISSING:
            continue
        tname = str(f.type)
        vals[f.name] = 0 if "int" in tname else ([] if "list" in tname else "x")
    names = {f.name for f in dataclasses.fields(reviewer.PRContext)}
    wanted = dict(number=1, pr_number=1, base_ref="main", head_sha="deadbeef", diff="diff --git a/a.py b/a.py\n+x\n")
    vals.update({k: v for k, v in wanted.items() if k in names})
    return reviewer.PRContext(**vals)


class RunnerBackendMatrixTests(unittest.TestCase):
    """Task 14 regression net: every runner × backend combination resolves
    to the documented endpoint kind and constructs without raising; the
    two documented fail-fast cases fire at run time, before any CLI call."""

    RUNNERS = ("anthropic", "openai", "claude-code", "cursor", "codex", "grok")
    BASES = {
        "": None,  # runner default — kind comes from PROVIDER_DEFAULT_ENDPOINT_KIND
        "https://api.z.ai/api/anthropic": "zai",
        "https://api.x.ai/v1": "xai",
        "https://myres.openai.azure.com/openai/v1": "azure",
        "https://gw.example.com/v1": "custom",
    }

    def test_every_combination_resolves_and_constructs(self) -> None:
        with mock.patch.object(reviewer, "log"):
            for pid in self.RUNNERS:
                for base, kind in self.BASES.items():
                    profile = reviewer.resolve_endpoint_profile(base, pid)
                    expected = kind or reviewer.PROVIDER_DEFAULT_ENDPOINT_KIND[pid]
                    self.assertEqual(profile.kind, expected, (pid, base))
                    self.assertEqual(profile.is_default, base == "", (pid, base))
                    provider = reviewer.build_provider(pid, api_key="sk-test", model="", api_base=base)
                    self.assertEqual(provider.profile.kind, expected, (pid, base))
                    self.assertEqual(provider.PROVIDER_ID, pid)

    def test_default_profiles_have_stable_kinds(self) -> None:
        self.assertEqual(
            {p: reviewer.resolve_endpoint_profile("", p).kind for p in self.RUNNERS},
            {"anthropic": "anthropic", "openai": "openai", "claude-code": "anthropic",
             "cursor": "custom", "codex": "openai", "grok": "xai"},
        )

    def _run(self, provider) -> None:
        import subprocess as sp
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(
            reviewer.subprocess, "run",
            side_effect=AssertionError("CLI must not be invoked"),
        ), mock.patch.object(reviewer, "log"):
            provider.run_review(
                pr_context=_ctx(), review_instructions="R",
                workspace=Path(tmp), output_dir=Path(tmp),
            )

    def test_codex_custom_backend_without_model_fails_fast(self) -> None:
        with mock.patch.object(reviewer, "log"):
            provider = reviewer.build_provider("codex", api_key="sk-test", model="", api_base="https://gw.example.com/v1")
        with self.assertRaises(ValueError):
            self._run(provider)

    def test_claude_code_oauth_token_with_custom_backend_fails_fast(self) -> None:
        with mock.patch.object(reviewer, "log"):
            provider = reviewer.build_provider("claude-code", api_key="sk-ant-oat01-abc", model="", api_base="https://api.z.ai/api/anthropic")
        with self.assertRaises(ValueError):
            self._run(provider)


class JoinEndpointPathTests(unittest.TestCase):
    """A base URL given with a trailing `/v1` must not produce `/v1/v1/…`
    (dogfood finding on PR #50)."""

    def test_trailing_v1_is_not_doubled_for_anthropic(self) -> None:
        self.assertEqual(reviewer.join_endpoint_path("https://api.anthropic.com/v1", "/v1/messages"), "https://api.anthropic.com/v1/messages")
        self.assertEqual(reviewer.join_endpoint_path("https://api.x.ai", "/v1/messages"), "https://api.x.ai/v1/messages")
        self.assertEqual(reviewer.join_endpoint_path("https://api.z.ai/api/anthropic", "/v1/messages"), "https://api.z.ai/api/anthropic/v1/messages")

    def test_openai_style_bases_join_verbatim(self) -> None:
        self.assertEqual(reviewer.join_endpoint_path("https://api.x.ai/v1", "/chat/completions"), "https://api.x.ai/v1/chat/completions")
        self.assertEqual(reviewer.join_endpoint_path("https://r.openai.azure.com/openai/v1/", "/chat/completions"), "https://r.openai.azure.com/openai/v1/chat/completions")

    def test_anthropic_provider_uses_the_join(self) -> None:
        captured: dict = {}
        def fake_post(**kw):
            captured["url"] = kw["url"]; return {"stop_reason": "end_turn", "content": [], "usage": {}}
        prov = reviewer.build_provider("anthropic", api_key="sk-ant-api-TEST", model="m", api_base="https://api.anthropic.com/v1")
        with mock.patch.object(reviewer, "_post_json_with_retries", side_effect=fake_post), mock.patch.object(reviewer, "log"):
            try:
                prov.complete(system_prompt="s", messages=[{"role": "user", "content": "x"}], tools=[])
            except Exception:
                pass
        self.assertEqual(captured.get("url"), "https://api.anthropic.com/v1/messages")


class BackendSelectionLogTests(unittest.TestCase):
    """Custom hosts trigger a visible warning naming where the key goes."""

    def test_custom_host_warns_and_names_host(self) -> None:
        profile = reviewer.resolve_endpoint_profile("https://gw.example.com/v1", "openai")
        with mock.patch.object(reviewer, "log") as fake_log:
            reviewer.log_backend_selection(profile)
        msgs = [str(c.args[0]) for c in fake_log.call_args_list]
        self.assertTrue(any("WARNING" in m and "gw.example.com" in m and "api-key" in m for m in msgs), msgs)

    def test_vendor_and_default_hosts_do_not_warn(self) -> None:
        for api_base, pid in (("", "anthropic"), ("https://api.x.ai/v1", "openai"), ("https://api.z.ai/api/anthropic", "anthropic")):
            with mock.patch.object(reviewer, "log") as fake_log:
                reviewer.log_backend_selection(reviewer.resolve_endpoint_profile(api_base, pid))
            msgs = " ".join(str(c.args[0]) for c in fake_log.call_args_list)
            self.assertNotIn("WARNING", msgs, api_base)
            self.assertIn("Backend:", msgs)

if __name__ == "__main__":
    unittest.main()
