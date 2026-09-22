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

    def test_rejects_non_ascii_host_requires_punycode(self) -> None:
        with self.assertRaises(ValueError) as ctx:
            reviewer.validate_api_base("https://api.\u0445.ai/v1")  # Cyrillic kha
        self.assertIn("punycode", str(ctx.exception))
        # The explicit punycode form is classified as custom (not a vendor).
        norm = reviewer.validate_api_base("https://api.xn--80a.ai/v1")
        self.assertEqual(reviewer.classify_endpoint_host(
            reviewer.urllib.parse.urlsplit(norm).hostname), "custom")

    def test_ipv6_literals(self) -> None:
        self.assertEqual(
            reviewer.validate_api_base("http://[::1]:8000/v1"), "http://[::1]:8000/v1"
        )
        self.assertEqual(
            reviewer.validate_api_base("https://[2001:db8::1]:8443/v1/"),
            "https://[2001:db8::1]:8443/v1",
        )
        with self.assertRaises(ValueError):
            reviewer.validate_api_base("http://[2001:db8::1]/v1")

    def test_host_tricks_never_classify_as_vendor(self) -> None:
        for raw in (
            "https://api.x.ai.evil.example/v1",
            "https://evil.example/api.x.ai",
            "https://api.x.ai./v1",
            "https://API.X.AI.example/v1",
        ):
            host = reviewer.urllib.parse.urlsplit(reviewer.validate_api_base(raw)).hostname
            self.assertEqual(reviewer.classify_endpoint_host(host), "custom", raw)

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
            "api.deepseek.com": reviewer.ENDPOINT_KIND_DEEPSEEK,
            "api.moonshot.ai": reviewer.ENDPOINT_KIND_MOONSHOT,
            "api.minimax.io": reviewer.ENDPOINT_KIND_MINIMAX,
            "api.minimaxi.com": reviewer.ENDPOINT_KIND_MINIMAX,
            "dashscope.aliyuncs.com": reviewer.ENDPOINT_KIND_QWEN,
            "generativelanguage.googleapis.com": reviewer.ENDPOINT_KIND_GEMINI,
            "openrouter.ai": reviewer.ENDPOINT_KIND_OPENROUTER,
            "gateway.example.com": reviewer.ENDPOINT_KIND_CUSTOM,
            "localhost": reviewer.ENDPOINT_KIND_CUSTOM,
            "": reviewer.ENDPOINT_KIND_CUSTOM,
        }
        for host, kind in cases.items():
            with self.subTest(host=host):
                self.assertEqual(reviewer.classify_endpoint_host(host), kind)

    def test_v2_4_0_hosts_do_not_match_lookalikes(self) -> None:
        # The six v2.4.0 vendor hosts are exact matches too — a mistyped or
        # hostile lookalike must fall through to `custom` (and its warning),
        # never inherit the vendor's endpoint profile.
        for host in (
            "evil-api.deepseek.com",
            "api.moonshot.ai.evil.com",
            "openrouter.ai.gw.example.com",
            "generativelanguage.googleapis.commm",
        ):
            with self.subTest(host=host):
                self.assertEqual(
                    reviewer.classify_endpoint_host(host), reviewer.ENDPOINT_KIND_CUSTOM
                )

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
        self.assertEqual(len(reviewer.ENDPOINT_KINDS), 12)
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


if __name__ == "__main__":
    unittest.main()
