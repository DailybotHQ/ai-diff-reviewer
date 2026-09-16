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
                # v2.1.0: the diff-bearing first user message carries the
                # second cache breakpoint (block form on the wire).
                "messages": [
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "text",
                                "text": "hi",
                                "cache_control": {"type": "ephemeral"},
                            }
                        ],
                    }
                ],
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
        # no breakpoint on the user message either — and the plain string
        # form is kept as-is for gateways without cache_control support
        self.assertEqual(body["messages"], [{"role": "user", "content": "hi"}])

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


class DiffCacheBreakpointTests(unittest.TestCase):
    """Task 9: the first user message gets a cache breakpoint on Anthropic,
    without mutating the in-memory conversation."""

    def test_string_first_message_becomes_cached_block(self) -> None:
        messages = [{"role": "user", "content": "DIFF"}, {"role": "assistant", "content": []}]
        wire = reviewer._with_first_user_cache_breakpoint(messages)
        self.assertEqual(wire[0]["content"], [{"type": "text", "text": "DIFF", "cache_control": {"type": "ephemeral"}}])
        self.assertEqual(wire[1], messages[1])
        # caller's list untouched
        self.assertEqual(messages[0], {"role": "user", "content": "DIFF"})

    def test_block_list_marks_last_text_block_only(self) -> None:
        messages = [{"role": "user", "content": [{"type": "text", "text": "a"}, {"type": "image", "source": {}}, {"type": "text", "text": "b"}]}]
        wire = reviewer._with_first_user_cache_breakpoint(messages)
        blocks = wire[0]["content"]
        self.assertNotIn("cache_control", blocks[0])
        self.assertNotIn("cache_control", blocks[1])
        self.assertIn("cache_control", blocks[2])
        self.assertNotIn("cache_control", messages[0]["content"][2])

    def test_non_user_first_or_empty_is_passthrough(self) -> None:
        self.assertEqual(reviewer._with_first_user_cache_breakpoint([]), [])
        msgs = [{"role": "assistant", "content": "x"}]
        self.assertIs(reviewer._with_first_user_cache_breakpoint(msgs), msgs)
        msgs2 = [{"role": "user", "content": []}]
        self.assertIs(reviewer._with_first_user_cache_breakpoint(msgs2), msgs2)

    def test_provider_never_mutates_caller_messages(self) -> None:
        prov = reviewer.AnthropicProvider(api_key="k", model="m")
        messages = [{"role": "user", "content": "DIFF"}]
        captured: dict[str, object] = {}

        def fake_urlopen(request: object, timeout: float = 0) -> _FakeResponse:
            captured["body"] = json.loads(request.data)  # type: ignore[attr-defined]
            return _FakeResponse(json.dumps({"stop_reason": "end_turn", "content": [], "usage": {"input_tokens": 10, "cache_read_input_tokens": 8, "output_tokens": 1}}).encode())

        with mock.patch.object(reviewer.urllib.request, "urlopen", fake_urlopen), mock.patch.object(reviewer, "log") as fake_log:
            prov.complete(system_prompt="S", messages=messages, tools=[])
        self.assertEqual(messages, [{"role": "user", "content": "DIFF"}])
        self.assertIn("cache_control", captured["body"]["messages"][0]["content"][0])  # type: ignore[index]
        msgs = " ".join(str(c.args[0]) for c in fake_log.call_args_list)
        self.assertIn("usage: in=10 cache_read=8", msgs)

    def test_exactly_two_breakpoints_on_anthropic(self) -> None:
        prov = reviewer.AnthropicProvider(api_key="k", model="m")
        req = _complete_and_capture(prov)
        body = json.loads(req.data)
        count = json.dumps(body).count('"cache_control"')
        self.assertEqual(count, 2)

    def test_openai_usage_logged(self) -> None:
        prov = reviewer.OpenAIProvider(api_key="k", model="m")

        def fake_urlopen(request: object, timeout: float = 0) -> _FakeResponse:
            return _FakeResponse(json.dumps({"choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}], "usage": {"prompt_tokens": 20, "completion_tokens": 2, "prompt_tokens_details": {"cached_tokens": 15}}}).encode())

        with mock.patch.object(reviewer.urllib.request, "urlopen", fake_urlopen), mock.patch.object(reviewer, "log") as fake_log:
            prov.complete(system_prompt="S", messages=[{"role": "user", "content": "u"}], tools=[])
        msgs = " ".join(str(c.args[0]) for c in fake_log.call_args_list)
        self.assertIn("usage: in=20 cache_read=15 out=2", msgs)


if __name__ == "__main__":
    unittest.main()

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

