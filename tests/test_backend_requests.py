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

    with mock.patch.object(reviewer.urllib.request.OpenerDirector, "open", side_effect=fake_urlopen):
        provider.complete(  # type: ignore[attr-defined]
            system_prompt="SYS", messages=[{"role": "user", "content": "hi"}], tools=[]
        )
    return captured["request"]


class AnthropicProviderBackendTests(unittest.TestCase):
    """Request shape per endpoint profile — the default profile is a locked
    snapshot of the pre-`api-base` request, plus the intentional deterministic
    `temperature` (the stabilization change; see the review-acceleration plan's
    noise-floor finding)."""

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
                "temperature": reviewer.REVIEW_TEMPERATURE,
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

        with mock.patch.object(reviewer.urllib.request.OpenerDirector, "open", side_effect=fake_urlopen):
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

        with mock.patch.object(reviewer.urllib.request.OpenerDirector, "open", side_effect=fake_urlopen):
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

        with mock.patch.object(reviewer.urllib.request.OpenerDirector, "open", side_effect=fake_urlopen), \
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

        with mock.patch.object(reviewer.urllib.request.OpenerDirector, "open", side_effect=fake_urlopen), mock.patch.object(reviewer, "log") as fake_log:
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

        with mock.patch.object(reviewer.urllib.request.OpenerDirector, "open", side_effect=fake_urlopen), mock.patch.object(reviewer, "log") as fake_log:
            prov.complete(system_prompt="S", messages=[{"role": "user", "content": "u"}], tools=[])
        msgs = " ".join(str(c.args[0]) for c in fake_log.call_args_list)
        self.assertIn("usage: in=20 cache_read=15 out=2", msgs)


if __name__ == "__main__":
    unittest.main()

if __name__ == "__main__":
    unittest.main()
