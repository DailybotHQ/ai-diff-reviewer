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


class ValidateApiBaseTests(unittest.TestCase):
    def test_empty_and_whitespace_resolve_to_empty(self) -> None:
        self.assertEqual(reviewer.validate_api_base(""), "")
        self.assertEqual(reviewer.validate_api_base("   "), "")

    def test_trailing_slash_is_stripped(self) -> None:
        self.assertEqual(
            reviewer.validate_api_base("https://api.z.ai/api/anthropic/"),
            "https://api.z.ai/api/anthropic",
        )

    def test_surrounding_whitespace_is_ignored(self) -> None:
        self.assertEqual(
            reviewer.validate_api_base("  https://api.x.ai/v1  "),
            "https://api.x.ai/v1",
        )

    def test_scheme_is_lowercased(self) -> None:
        self.assertEqual(
            reviewer.validate_api_base("HTTPS://api.x.ai"), "https://api.x.ai"
        )

    def test_rejects_plain_http_on_remote_host(self) -> None:
        with self.assertRaises(ValueError):
            reviewer.validate_api_base("http://api.x.ai/v1")

    def test_accepts_plain_http_on_localhost(self) -> None:
        self.assertEqual(
            reviewer.validate_api_base("http://localhost:8000/v1"),
            "http://localhost:8000/v1",
        )
        self.assertEqual(
            reviewer.validate_api_base("http://127.0.0.1:11434/v1"),
            "http://127.0.0.1:11434/v1",
        )

    def test_rejects_userinfo(self) -> None:
        with self.assertRaises(ValueError):
            reviewer.validate_api_base("https://user:pass@gateway.example/v1")

    def test_rejects_query_and_fragment(self) -> None:
        with self.assertRaises(ValueError):
            reviewer.validate_api_base("https://gateway.example/v1?x=1")
        with self.assertRaises(ValueError):
            reviewer.validate_api_base("https://gateway.example/v1#frag")

    def test_rejects_relative_or_hostless_values(self) -> None:
        for bad in ("api.z.ai/api/anthropic", "https://", "/v1", "ftp://x"):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                reviewer.validate_api_base(bad)

    def test_error_messages_are_actionable(self) -> None:
        with self.assertRaises(ValueError) as ctx:
            reviewer.validate_api_base("http://api.x.ai/v1")
        self.assertIn("https://", str(ctx.exception))


class ClassifyEndpointHostTests(unittest.TestCase):
    def test_truth_table(self) -> None:
        cases = {
            "api.anthropic.com": reviewer.ENDPOINT_KIND_ANTHROPIC,
            "api.openai.com": reviewer.ENDPOINT_KIND_OPENAI,
            "myres.openai.azure.com": reviewer.ENDPOINT_KIND_AZURE,
            "dailybot-ai-prod.services.ai.azure.com": reviewer.ENDPOINT_KIND_AZURE,
            "myres.cognitiveservices.azure.com": reviewer.ENDPOINT_KIND_AZURE,
            "api.x.ai": reviewer.ENDPOINT_KIND_XAI,
            "api.z.ai": reviewer.ENDPOINT_KIND_ZAI,
            "gateway.example.com": reviewer.ENDPOINT_KIND_CUSTOM,
            "localhost": reviewer.ENDPOINT_KIND_CUSTOM,
            "": reviewer.ENDPOINT_KIND_CUSTOM,
        }
        for host, kind in cases.items():
            with self.subTest(host=host):
                self.assertEqual(reviewer.classify_endpoint_host(host), kind)

    def test_bare_hosts_do_not_match_lookalike_subdomains(self) -> None:
        # `api.x.ai` is an exact match — `evil-api.x.ai` must not classify
        # as xAI (the suffix table only allows subdomain matching for the
        # entries that start with a dot).
        self.assertEqual(
            reviewer.classify_endpoint_host("evil-api.x.ai"),
            reviewer.ENDPOINT_KIND_CUSTOM,
        )
        self.assertEqual(
            reviewer.classify_endpoint_host("api.x.ai.evil.com"),
            reviewer.ENDPOINT_KIND_CUSTOM,
        )

    def test_classification_is_case_insensitive(self) -> None:
        self.assertEqual(
            reviewer.classify_endpoint_host("API.Z.AI"),
            reviewer.ENDPOINT_KIND_ZAI,
        )


class ResolveEndpointProfileTests(unittest.TestCase):
    def test_default_profile_for_every_provider(self) -> None:
        for pid in ALL_PROVIDERS:
            with self.subTest(provider=pid):
                prof = reviewer.resolve_endpoint_profile("", pid)
                self.assertTrue(prof.is_default)
                self.assertEqual(
                    prof.kind, reviewer.PROVIDER_DEFAULT_ENDPOINT_KIND[pid]
                )
                self.assertEqual(
                    prof.base_url, reviewer.PROVIDER_DEFAULT_API_BASE[pid]
                )

    def test_default_anthropic_profile_matches_legacy_url(self) -> None:
        prof = reviewer.resolve_endpoint_profile("", "anthropic")
        self.assertEqual(
            prof.base_url + "/v1/messages", reviewer.ANTHROPIC_API_URL
        )
        self.assertTrue(prof.supports_anthropic_cache_control)
        self.assertEqual(
            prof.anthropic_auth_style, reviewer.ANTHROPIC_AUTH_STYLE_X_API_KEY
        )

    def test_cursor_default_has_no_endpoint(self) -> None:
        prof = reviewer.resolve_endpoint_profile("", "cursor")
        self.assertEqual(prof.kind, reviewer.ENDPOINT_KIND_CUSTOM)
        self.assertEqual(prof.base_url, "")
        self.assertEqual(prof.host, "")

    def test_zai_profile(self) -> None:
        prof = reviewer.resolve_endpoint_profile(
            "https://api.z.ai/api/anthropic", "claude-code"
        )
        self.assertFalse(prof.is_default)
        self.assertEqual(prof.kind, reviewer.ENDPOINT_KIND_ZAI)
        self.assertEqual(prof.host, "api.z.ai")
        self.assertFalse(prof.supports_anthropic_cache_control)
        self.assertEqual(
            prof.anthropic_auth_style, reviewer.ANTHROPIC_AUTH_STYLE_BOTH
        )
        self.assertEqual(prof.codex_extra_toml, "")

    def test_azure_profile_carries_codex_workaround(self) -> None:
        prof = reviewer.resolve_endpoint_profile(
            "https://myres.services.ai.azure.com/openai/v1", "codex"
        )
        self.assertEqual(prof.kind, reviewer.ENDPOINT_KIND_AZURE)
        self.assertEqual(prof.openai_auth_style, reviewer.OPENAI_AUTH_STYLE_AZURE)
        self.assertEqual(prof.codex_wire_api, reviewer.CODEX_WIRE_API_RESPONSES)
        self.assertIn(reviewer.AZURE_IMAGE_GEN_HEADER, prof.codex_extra_toml)
        self.assertIn("image_generation = false", prof.codex_extra_toml)

    def test_xai_profile_for_openai_family(self) -> None:
        prof = reviewer.resolve_endpoint_profile("https://api.x.ai/v1", "openai")
        self.assertEqual(prof.kind, reviewer.ENDPOINT_KIND_XAI)
        self.assertEqual(prof.openai_auth_style, reviewer.OPENAI_AUTH_STYLE_BEARER)

    def test_custom_host_profile(self) -> None:
        prof = reviewer.resolve_endpoint_profile(
            "https://gateway.example.com/v1", "anthropic"
        )
        self.assertEqual(prof.kind, reviewer.ENDPOINT_KIND_CUSTOM)
        self.assertFalse(prof.supports_anthropic_cache_control)
        self.assertEqual(prof.host, "gateway.example.com")

    def test_profile_is_immutable(self) -> None:
        prof = reviewer.resolve_endpoint_profile("", "anthropic")
        with self.assertRaises(Exception):
            prof.kind = "x"  # type: ignore[misc]

    def test_every_kind_constant_is_registered(self) -> None:
        self.assertEqual(len(reviewer.ENDPOINT_KINDS), 6)
        for _suffix, kind in reviewer.ENDPOINT_HOST_SUFFIXES:
            self.assertIn(kind, reviewer.ENDPOINT_KINDS)


class BuildProviderApiBaseTests(unittest.TestCase):
    """`build_provider` threads the profile into every constructor."""

    def test_default_profile_stored_on_every_shipping_provider(self) -> None:
        for pid in ("anthropic", "claude-code", "cursor", "codex"):
            with self.subTest(provider=pid):
                prov = reviewer.build_provider(pid, api_key="k", model="m")
                self.assertTrue(prov.profile.is_default)

    def test_custom_profile_reaches_the_provider(self) -> None:
        prov = reviewer.build_provider(
            "claude-code",
            api_key="k",
            model="glm-5.3",
            api_base="https://api.z.ai/api/anthropic",
        )
        self.assertEqual(prov.profile.kind, reviewer.ENDPOINT_KIND_ZAI)
        self.assertFalse(prov.profile.is_default)

    def test_cursor_ignores_api_base_with_a_warning(self) -> None:
        with mock.patch.object(reviewer, "log") as fake_log:
            prov = reviewer.build_provider(
                "cursor", api_key="k", model="auto", api_base="https://api.x.ai/v1"
            )
        messages = " ".join(str(c.args[0]) for c in fake_log.call_args_list)
        self.assertIn("api-base", messages)
        self.assertIn("ignoring", messages)
        self.assertEqual(prov.profile.kind, reviewer.ENDPOINT_KIND_XAI)

    def test_constructors_default_profile_without_build_provider(self) -> None:
        self.assertTrue(
            reviewer.AnthropicProvider(api_key="k", model="m").profile.is_default
        )
        self.assertTrue(
            reviewer.CodexProvider(api_key="k", model="m").profile.is_default
        )


class _FakeResponse(io.BytesIO):
    """Minimal context-manager stand-in for `urlopen`'s response."""

    def __enter__(self) -> "_FakeResponse":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


def _complete_and_capture(provider: object) -> object:
    """Drive `provider.complete()` once with a canned 200 and return the
    `urllib.request.Request` the provider built."""
    captured: dict[str, object] = {}

    def fake_urlopen(request: object, timeout: float = 0) -> _FakeResponse:
        captured["request"] = request
        return _FakeResponse(
            json.dumps({"stop_reason": "end_turn", "content": []}).encode()
        )

    with mock.patch.object(reviewer.urllib.request, "urlopen", fake_urlopen):
        provider.complete(  # type: ignore[attr-defined]
            system_prompt="SYS", messages=[{"role": "user", "content": "hi"}], tools=[]
        )
    return captured["request"]


class AnthropicProviderBackendTests(unittest.TestCase):
    """Request shape per endpoint profile — the default profile is a locked
    snapshot of the pre-`api-base` request (byte-identical contract)."""

    def test_default_profile_request_is_byte_identical_to_legacy(self) -> None:
        prov = reviewer.AnthropicProvider(api_key="sk-ant-api-TEST", model="claude-sonnet-4-6")
        req = _complete_and_capture(prov)
        self.assertEqual(req.full_url, reviewer.ANTHROPIC_API_URL)
        self.assertEqual(req.get_method(), "POST")
        # Exactly the legacy header set — no Authorization on Anthropic.
        self.assertEqual(
            {k.lower(): v for k, v in req.header_items()},
            {
                "content-type": "application/json",
                "x-api-key": "sk-ant-api-TEST",
                "anthropic-version": reviewer.ANTHROPIC_VERSION,
            },
        )
        body = json.loads(req.data)
        self.assertEqual(
            body,
            {
                "model": "claude-sonnet-4-6",
                "max_tokens": reviewer.ANTHROPIC_MAX_TOKENS,
                "system": [
                    {
                        "type": "text",
                        "text": "SYS",
                        "cache_control": {"type": "ephemeral"},
                    }
                ],
                "messages": [{"role": "user", "content": "hi"}],
                "tools": [],
            },
        )

    def test_zai_profile_url_auth_and_no_cache_control(self) -> None:
        prof = reviewer.resolve_endpoint_profile("https://api.z.ai/api/anthropic", "anthropic")
        prov = reviewer.AnthropicProvider(api_key="zai-KEY", model="glm-5.3", profile=prof)
        req = _complete_and_capture(prov)
        self.assertEqual(req.full_url, "https://api.z.ai/api/anthropic/v1/messages")
        headers = {k.lower(): v for k, v in req.header_items()}
        self.assertEqual(headers["x-api-key"], "zai-KEY")
        self.assertEqual(headers["authorization"], "Bearer zai-KEY")
        body = json.loads(req.data)
        self.assertNotIn("cache_control", body["system"][0])
        self.assertEqual(body["system"][0]["text"], "SYS")

    def test_xai_anthropic_compatible_profile_url(self) -> None:
        prof = reviewer.resolve_endpoint_profile("https://api.x.ai", "anthropic")
        prov = reviewer.AnthropicProvider(api_key="xai-KEY", model="grok-4.3", profile=prof)
        req = _complete_and_capture(prov)
        self.assertEqual(req.full_url, "https://api.x.ai/v1/messages")

    def test_trailing_slash_in_api_base_never_doubles(self) -> None:
        base = reviewer.validate_api_base("https://api.z.ai/api/anthropic/")
        prof = reviewer.resolve_endpoint_profile(base, "anthropic")
        prov = reviewer.AnthropicProvider(api_key="k", model="glm-5.3", profile=prof)
        req = _complete_and_capture(prov)
        self.assertNotIn("//v1", req.full_url.replace("https://", ""))

    def test_error_message_names_kind_and_host_never_the_key(self) -> None:
        prof = reviewer.resolve_endpoint_profile("https://api.z.ai/api/anthropic", "anthropic")
        prov = reviewer.AnthropicProvider(api_key="zai-SECRET", model="glm-5.3", profile=prof)

        def fake_urlopen(request: object, timeout: float = 0) -> _FakeResponse:
            raise urllib.error.HTTPError(
                request.full_url, 401, "Unauthorized", None, io.BytesIO(b"nope")  # type: ignore[attr-defined]
            )

        with mock.patch.object(reviewer.urllib.request, "urlopen", fake_urlopen):
            with self.assertRaises(RuntimeError) as ctx:
                prov.complete(system_prompt="S", messages=[], tools=[])
        msg = str(ctx.exception)
        self.assertIn("zai", msg)
        self.assertIn("api.z.ai", msg)
        self.assertIn("401", msg)
        self.assertNotIn("zai-SECRET", msg)

    def test_default_profile_error_keeps_legacy_wording(self) -> None:
        prov = reviewer.AnthropicProvider(api_key="k", model="m")

        def fake_urlopen(request: object, timeout: float = 0) -> _FakeResponse:
            raise urllib.error.HTTPError(request.full_url, 400, "Bad", None, io.BytesIO(b"x"))  # type: ignore[attr-defined]

        with mock.patch.object(reviewer.urllib.request, "urlopen", fake_urlopen):
            with self.assertRaises(RuntimeError) as ctx:
                prov.complete(system_prompt="S", messages=[], tools=[])
        self.assertIn("Anthropic API HTTP 400", str(ctx.exception))

    def test_retry_on_429_then_success(self) -> None:
        prov = reviewer.AnthropicProvider(api_key="k", model="m")
        calls: list[str] = []

        def fake_urlopen(request: object, timeout: float = 0) -> _FakeResponse:
            calls.append(request.full_url)  # type: ignore[attr-defined]
            if len(calls) == 1:
                raise urllib.error.HTTPError(request.full_url, 429, "slow", None, io.BytesIO(b""))  # type: ignore[attr-defined]
            return _FakeResponse(json.dumps({"stop_reason": "end_turn", "content": []}).encode())

        with mock.patch.object(reviewer.urllib.request, "urlopen", fake_urlopen), \
             mock.patch.object(reviewer.time, "sleep", lambda s: None):
            resp = prov.complete(system_prompt="S", messages=[], tools=[])
        self.assertEqual(resp["stop_reason"], "end_turn")
        self.assertEqual(len(calls), 2)


if __name__ == "__main__":
    unittest.main()
