#!/usr/bin/env python3
"""Unit tests for the OpenAI-compatible chat-completions runner
(`provider: openai`): boundary translation in both directions, request
shape per endpoint profile, retries, and a full `drive_review` loop driven
by canned chat-completions responses (no network)."""

from __future__ import annotations

import importlib.util
import io
import json
import os
import sys
import tempfile
import unittest
import urllib.error
from pathlib import Path
from typing import Any
from unittest import mock

_ROOT: Path = Path(__file__).resolve().parent.parent
_SPEC = importlib.util.spec_from_file_location(
    "reviewer", _ROOT / "scripts" / "reviewer.py"
)
assert _SPEC is not None and _SPEC.loader is not None
reviewer = importlib.util.module_from_spec(_SPEC)
sys.modules["reviewer"] = reviewer
_SPEC.loader.exec_module(reviewer)


class _FakeResponse(io.BytesIO):
    def __enter__(self) -> "_FakeResponse":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


def _oa(content: str | None = None, tool_calls: list[dict[str, Any]] | None = None,
        finish: str = "stop", usage: dict[str, Any] | None = None) -> dict[str, Any]:
    msg: dict[str, Any] = {"role": "assistant", "content": content}
    if tool_calls:
        msg["tool_calls"] = tool_calls
    return {
        "id": "chatcmpl-1",
        "model": "gpt-5.6-luna",
        "choices": [{"index": 0, "message": msg, "finish_reason": finish}],
        "usage": usage or {"prompt_tokens": 10, "completion_tokens": 5},
    }


def _call(cid: str, name: str, args: Any) -> dict[str, Any]:
    return {
        "id": cid,
        "type": "function",
        "function": {
            "name": name,
            "arguments": args if isinstance(args, str) else json.dumps(args),
        },
    }


class ToolsTranslationTests(unittest.TestCase):
    def test_anthropic_tools_become_function_tools(self) -> None:
        tools = reviewer.tools_schema(10)
        out = reviewer.anthropic_tools_to_openai(tools)
        self.assertEqual(len(out), len(tools))
        for src, dst in zip(tools, out):
            self.assertEqual(dst["type"], "function")
            self.assertEqual(dst["function"]["name"], src["name"])
            self.assertEqual(dst["function"]["parameters"], src["input_schema"])
            self.assertEqual(dst["function"]["description"], src["description"])


class MessagesTranslationTests(unittest.TestCase):
    def test_system_prompt_leads_and_string_user_passes_through(self) -> None:
        out = reviewer.anthropic_messages_to_openai("SYS", [{"role": "user", "content": "hi"}])
        self.assertEqual(out, [
            {"role": "system", "content": "SYS"},
            {"role": "user", "content": "hi"},
        ])

    def test_assistant_blocks_become_tool_calls_and_results_split(self) -> None:
        messages = [
            {"role": "user", "content": "review"},
            {"role": "assistant", "content": [
                {"type": "text", "text": "Let me look."},
                {"type": "tool_use", "id": "call_a", "name": "read_file", "input": {"path": "a.py"}},
                {"type": "tool_use", "id": "call_b", "name": "grep", "input": {"pattern": "x"}},
            ]},
            {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": "call_a", "content": "A"},
                {"type": "tool_result", "tool_use_id": "call_b", "content": [{"type": "text", "text": "B"}]},
            ]},
        ]
        out = reviewer.anthropic_messages_to_openai("S", messages)
        self.assertEqual(out[2]["role"], "assistant")
        self.assertEqual(out[2]["content"], "Let me look.")
        self.assertEqual([c["id"] for c in out[2]["tool_calls"]], ["call_a", "call_b"])
        self.assertEqual(json.loads(out[2]["tool_calls"][0]["function"]["arguments"]), {"path": "a.py"})
        # one `tool` message per result, in order, right after the assistant turn
        self.assertEqual(out[3], {"role": "tool", "tool_call_id": "call_a", "content": "A"})
        self.assertEqual(out[4], {"role": "tool", "tool_call_id": "call_b", "content": "B"})
        self.assertEqual(len(out), 5)

    def test_assistant_with_only_tool_calls_has_null_content(self) -> None:
        out = reviewer.anthropic_messages_to_openai("S", [
            {"role": "assistant", "content": [
                {"type": "tool_use", "id": "c", "name": "glob", "input": {"pattern": "*"}},
            ]},
        ])
        self.assertIsNone(out[1]["content"])
        self.assertEqual(len(out[1]["tool_calls"]), 1)


class ResponseTranslationTests(unittest.TestCase):
    def test_text_only(self) -> None:
        r = reviewer.openai_response_to_anthropic(_oa(content="done", finish="stop"))
        self.assertEqual(r["stop_reason"], "end_turn")
        self.assertEqual(r["content"], [{"type": "text", "text": "done"}])
        self.assertEqual(r["usage"]["prompt_tokens"], 10)

    def test_single_tool_call(self) -> None:
        r = reviewer.openai_response_to_anthropic(
            _oa(tool_calls=[_call("call_1", "read_file", {"path": "x.py"})], finish="tool_calls")
        )
        self.assertEqual(r["stop_reason"], "tool_use")
        self.assertEqual(r["content"], [
            {"type": "tool_use", "id": "call_1", "name": "read_file", "input": {"path": "x.py"}}
        ])

    def test_parallel_tool_calls_preserve_order(self) -> None:
        r = reviewer.openai_response_to_anthropic(_oa(
            content="checking",
            tool_calls=[_call("c1", "grep", {"pattern": "a"}), _call("c2", "glob", {"pattern": "b"})],
            finish="tool_calls",
        ))
        self.assertEqual([b["type"] for b in r["content"]], ["text", "tool_use", "tool_use"])
        self.assertEqual([b["id"] for b in r["content"][1:]], ["c1", "c2"])

    def test_malformed_arguments_surface_instead_of_crash(self) -> None:
        r = reviewer.openai_response_to_anthropic(
            _oa(tool_calls=[_call("c1", "read_file", "{not json")], finish="tool_calls")
        )
        block = r["content"][0]
        self.assertEqual(block["type"], "tool_use")
        self.assertIn("_raw_arguments", block["input"])
        self.assertIn("_error", block["input"])
        # and execute_tool turns that into a tool_result error string, no raise
        text = reviewer.execute_tool("read_file", block["input"], reviewer.ReviewState())
        self.assertTrue(text.startswith("Tool `read_file` raised"))

    def test_finish_reason_mapping(self) -> None:
        self.assertEqual(reviewer.openai_response_to_anthropic(_oa(content="x", finish="length"))["stop_reason"], "max_tokens")
        self.assertEqual(reviewer.openai_response_to_anthropic(_oa(content="x", finish="content_filter"))["stop_reason"], "end_turn")
        self.assertEqual(reviewer.openai_response_to_anthropic(_oa(content="x", finish="weird"))["stop_reason"], "weird")

    def test_missing_id_gets_synthetic_one(self) -> None:
        call = _call("", "grep", {"pattern": "a"}); call["id"] = None
        r = reviewer.openai_response_to_anthropic(_oa(tool_calls=[call], finish="tool_calls"))
        self.assertEqual(r["content"][0]["id"], "call_0")

    def test_no_choices_raises(self) -> None:
        with self.assertRaises(RuntimeError):
            reviewer.openai_response_to_anthropic({"choices": []})

    def test_list_content_parts_are_joined(self) -> None:
        resp = _oa(content=None)
        resp["choices"][0]["message"]["content"] = [{"type": "text", "text": "a"}, {"type": "text", "text": "b"}]
        r = reviewer.openai_response_to_anthropic(resp)
        self.assertEqual(r["content"], [{"type": "text", "text": "a\nb"}])


class RequestShapeTests(unittest.TestCase):
    def _capture(self, prov: Any) -> Any:
        captured: dict[str, Any] = {}

        def fake_urlopen(request: Any, timeout: float = 0) -> _FakeResponse:
            captured["request"] = request
            return _FakeResponse(json.dumps(_oa(content="ok")).encode())

        with mock.patch.object(reviewer.urllib.request, "urlopen", fake_urlopen):
            prov.complete(system_prompt="S", messages=[{"role": "user", "content": "u"}], tools=reviewer.tools_schema(3))
        return captured["request"]

    def test_default_openai_profile(self) -> None:
        prov = reviewer.OpenAIProvider(api_key="sk-TEST", model="gpt-5.6-luna")
        req = self._capture(prov)
        self.assertEqual(req.full_url, "https://api.openai.com/v1/chat/completions")
        headers = {k.lower(): v for k, v in req.header_items()}
        self.assertEqual(headers["authorization"], "Bearer sk-TEST")
        self.assertNotIn("api-key", headers)
        body = json.loads(req.data)
        self.assertEqual(body["model"], "gpt-5.6-luna")
        self.assertEqual(body["max_completion_tokens"], reviewer.OPENAI_MAX_TOKENS)
        self.assertNotIn("max_tokens", body)
        self.assertEqual(body["tool_choice"], "auto")
        self.assertEqual(body["messages"][0], {"role": "system", "content": "S"})
        self.assertEqual(len(body["tools"]), 5)

    def test_azure_profile_adds_api_key_header_and_v1_path(self) -> None:
        prof = reviewer.resolve_endpoint_profile("https://myres.services.ai.azure.com/openai/v1", "openai")
        prov = reviewer.OpenAIProvider(api_key="az-KEY", model="gpt-5.4-mini-azure", profile=prof)
        req = self._capture(prov)
        self.assertEqual(req.full_url, "https://myres.services.ai.azure.com/openai/v1/chat/completions")
        headers = {k.lower(): v for k, v in req.header_items()}
        self.assertEqual(headers["api-key"], "az-KEY")
        self.assertEqual(headers["authorization"], "Bearer az-KEY")
        self.assertIn("max_completion_tokens", json.loads(req.data))

    def test_xai_and_zai_profiles_use_max_tokens(self) -> None:
        for base, model in (("https://api.x.ai/v1", "grok-4.3"), ("https://api.z.ai/api/coding/paas/v4", "glm-5.3")):
            with self.subTest(base=base):
                prof = reviewer.resolve_endpoint_profile(base, "openai")
                prov = reviewer.OpenAIProvider(api_key="k", model=model, profile=prof)
                req = self._capture(prov)
                self.assertTrue(req.full_url.startswith(base))
                self.assertTrue(req.full_url.endswith("/chat/completions"))
                body = json.loads(req.data)
                self.assertEqual(body["max_tokens"], reviewer.OPENAI_MAX_TOKENS)
                self.assertNotIn("max_completion_tokens", body)

    def test_no_tools_means_no_tool_fields(self) -> None:
        prov = reviewer.OpenAIProvider(api_key="k", model="m")
        body = prov.build_request_body(system_prompt="S", messages=[], tools=[])
        self.assertNotIn("tools", body)
        self.assertNotIn("tool_choice", body)

    def test_retry_on_503_then_success_and_error_label(self) -> None:
        prof = reviewer.resolve_endpoint_profile("https://api.x.ai/v1", "openai")
        prov = reviewer.OpenAIProvider(api_key="SECRET-xai", model="grok-4.3", profile=prof)
        calls: list[int] = []

        def fake_urlopen(request: Any, timeout: float = 0) -> _FakeResponse:
            calls.append(1)
            if len(calls) == 1:
                raise urllib.error.HTTPError(request.full_url, 503, "busy", None, io.BytesIO(b"busy"))
            return _FakeResponse(json.dumps(_oa(content="ok")).encode())

        with mock.patch.object(reviewer.urllib.request, "urlopen", fake_urlopen), \
             mock.patch.object(reviewer.time, "sleep", lambda s: None):
            r = prov.complete(system_prompt="S", messages=[], tools=[])
        self.assertEqual(r["stop_reason"], "end_turn")
        self.assertEqual(len(calls), 2)

        def fake_401(request: Any, timeout: float = 0) -> _FakeResponse:
            raise urllib.error.HTTPError(request.full_url, 401, "no", None, io.BytesIO(b"denied"))

        with mock.patch.object(reviewer.urllib.request, "urlopen", fake_401):
            with self.assertRaises(RuntimeError) as ctx:
                prov.complete(system_prompt="S", messages=[], tools=[])
        msg = str(ctx.exception)
        self.assertIn("xai chat completions API (api.x.ai)", msg)
        self.assertNotIn("SECRET-xai", msg)


class DispatchAndDefaultsTests(unittest.TestCase):
    def test_build_provider_openai(self) -> None:
        p = reviewer.build_provider("openai", api_key="k", model="")
        self.assertIsInstance(p, reviewer.OpenAIProvider)
        self.assertIsInstance(p, reviewer.Provider)
        self.assertNotIsInstance(p, reviewer.AgentRunnerProvider)

    def test_default_model_registered(self) -> None:
        self.assertEqual(reviewer.DEFAULT_MODELS["openai"], "gpt-5.6-luna")
        self.assertEqual(reviewer.DEFAULT_MODELS["openai"], reviewer.DEFAULT_MODELS["codex"])


class DriveReviewEndToEndTests(unittest.TestCase):
    """A full agentic loop through `OpenAIProvider` with canned responses:
    read a file → post an inline comment → submit the review."""

    def test_loop_reads_posts_and_submits(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "app.py").write_text("x = 1\ny = x / 0\n", encoding="utf-8")
            responses = [
                _oa(tool_calls=[_call("c1", "read_file", {"path": "app.py"})], finish="tool_calls"),
                _oa(tool_calls=[
                    _call("c2", "post_inline_comment", {"path": "app.py", "line": 2, "body": "Division by zero.", "severity": "critical"}),
                    _call("c3", "submit_review", {"summary": "One critical."}),
                ], finish="tool_calls"),
            ]
            seen_requests: list[dict[str, Any]] = []

            def fake_urlopen(request: Any, timeout: float = 0) -> _FakeResponse:
                seen_requests.append(json.loads(request.data))
                return _FakeResponse(json.dumps(responses[len(seen_requests) - 1]).encode())

            prov = reviewer.OpenAIProvider(api_key="k", model="gpt-5.6-luna")
            state = reviewer.ReviewState(max_inline_comments=10)
            messages: list[dict[str, Any]] = [{"role": "user", "content": "review this"}]
            cwd = os.getcwd(); os.chdir(tmp)
            try:
                with mock.patch.object(reviewer.urllib.request, "urlopen", fake_urlopen):
                    reviewer.drive_review(
                        provider=prov, system_prompt="S", messages=messages,
                        tools=reviewer.tools_schema(10), state=state, max_turns=5,
                    )
            finally:
                os.chdir(cwd)
            self.assertEqual(len(seen_requests), 2)
            # second request carried the tool result of read_file as a `tool` message
            roles = [m["role"] for m in seen_requests[1]["messages"]]
            self.assertEqual(roles, ["system", "user", "assistant", "tool"])
            self.assertIn("y = x / 0", seen_requests[1]["messages"][3]["content"])
            self.assertEqual(state.final_summary, "One critical.")
            self.assertEqual(len(state.inline_comments), 1)
            result = reviewer.state_to_review_result(state)
            self.assertEqual(result.overall_severity, "critical")


if __name__ == "__main__":
    unittest.main()
