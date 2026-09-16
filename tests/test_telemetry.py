#!/usr/bin/env python3
"""Usage telemetry (v2.1.0+): vendor usage normalisation, per-CLI stdout
parsers (fixtures captured live on 2026-09-16), cost estimation, the
tracking-comment usage line, and the wiring into ReviewState/ReviewResult."""

from __future__ import annotations

import importlib.util
import json
import sys
import unittest
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

# --- fixtures (shapes observed live) --------------------------------------
CLAUDE_CODE_STREAM = "\n".join([
    json.dumps({"type": "system", "subtype": "init"}),
    json.dumps({"type": "assistant", "message": {"content": [{"type": "text", "text": "OK"}]}}),
    json.dumps({"type": "result", "subtype": "success", "is_error": False, "num_turns": 1, "duration_ms": 2500,
                "total_cost_usd": 0.0169967,
                "usage": {"input_tokens": 10, "cache_creation_input_tokens": 7218, "cache_read_input_tokens": 13607,
                          "output_tokens": 49, "output_tokens_details": {"thinking_tokens": 42}}}),
])
CODEX_JSONL = "\n".join([
    json.dumps({"type": "thread.started", "thread_id": "t1"}),
    json.dumps({"type": "turn.started"}),
    json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": "OK"}}),
    json.dumps({"type": "turn.completed", "usage": {"input_tokens": 13403, "cached_input_tokens": 0, "cache_write_input_tokens": 0, "output_tokens": 22, "reasoning_output_tokens": 15}}),
    json.dumps({"type": "turn.started"}),
    json.dumps({"type": "turn.completed", "usage": {"input_tokens": 1000, "cached_input_tokens": 900, "cache_write_input_tokens": 0, "output_tokens": 8, "reasoning_output_tokens": 0}}),
])
GROK_JSON = json.dumps({
    "text": "OK", "stopReason": "end_turn", "sessionId": "s", "requestId": "r",
    "usage": {"input_tokens": 12297, "cache_read_input_tokens": 128, "cache_creation_input_tokens": 0, "output_tokens": 1, "reasoning_tokens": 0, "total_tokens": 12426},
    "num_turns": 1, "total_cost_usd": 0.01539935,
    "modelUsage": {"grok-4.3": {"inputTokens": 12297, "outputTokens": 1, "costUSD": 0.01539935}},
}, indent=2)


class NormaliseUsageTests(unittest.TestCase):
    def test_anthropic_keys(self) -> None:
        u = reviewer.normalise_usage({"input_tokens": 10, "output_tokens": 5, "cache_read_input_tokens": 100, "cache_creation_input_tokens": 20})
        assert u is not None
        self.assertEqual((u.input_tokens, u.output_tokens, u.cache_read_tokens, u.cache_write_tokens, u.turns), (10, 5, 100, 20, 1))
        self.assertEqual(u.source, reviewer.USAGE_SOURCE_API)

    def test_openai_keys_split_cached_out_of_prompt(self) -> None:
        u = reviewer.normalise_usage({"prompt_tokens": 1000, "completion_tokens": 7, "prompt_tokens_details": {"cached_tokens": 800}})
        assert u is not None
        self.assertEqual((u.input_tokens, u.cache_read_tokens, u.output_tokens), (200, 800, 7))

    def test_codex_keys(self) -> None:
        u = reviewer.normalise_usage({"input_tokens": 100, "cached_input_tokens": 60, "cache_write_input_tokens": 5, "output_tokens": 3})
        assert u is not None
        self.assertEqual((u.input_tokens, u.cache_read_tokens, u.cache_write_tokens, u.output_tokens), (35, 60, 5, 3))
        self.assertEqual(u.total_tokens, 103)

    def test_garbage_is_none(self) -> None:
        for raw in (None, {}, [], "x", {"foo": 1}, {"input_tokens": "abc"}):
            with self.subTest(raw=raw):
                u = reviewer.normalise_usage(raw)
                self.assertTrue(u is None or u.total_tokens == 0)

    def test_add_accumulates_and_keeps_source(self) -> None:
        total = reviewer.UsageTelemetry()
        self.assertEqual(total.source, reviewer.USAGE_SOURCE_UNAVAILABLE)
        total.add(reviewer.UsageTelemetry(input_tokens=1, output_tokens=2, turns=1, source="api"))
        total.add(reviewer.UsageTelemetry(input_tokens=3, output_tokens=4, turns=1, cost_usd=0.5, source="api"))
        self.assertEqual((total.input_tokens, total.output_tokens, total.turns, total.cost_usd, total.source), (4, 6, 2, 0.5, "api"))


class CliParsersTests(unittest.TestCase):
    def test_claude_code_result_event(self) -> None:
        u = reviewer.parse_claude_code_usage(CLAUDE_CODE_STREAM)
        assert u is not None
        self.assertEqual(u.source, reviewer.USAGE_SOURCE_CLI)
        self.assertEqual((u.input_tokens, u.cache_write_tokens, u.cache_read_tokens, u.output_tokens, u.turns), (10, 7218, 13607, 49, 1))
        self.assertAlmostEqual(u.cost_usd or 0, 0.0169967)

    def test_codex_turns_are_summed(self) -> None:
        u = reviewer.parse_codex_usage(CODEX_JSONL)
        assert u is not None
        self.assertEqual((u.input_tokens, u.cache_read_tokens, u.output_tokens, u.turns), (13503, 900, 30, 2))
        self.assertEqual(u.total_tokens, 14433)
        self.assertIsNone(u.cost_usd)
        self.assertEqual(u.source, reviewer.USAGE_SOURCE_CLI)

    def test_grok_pretty_json_document(self) -> None:
        u = reviewer.parse_grok_usage(GROK_JSON)
        assert u is not None
        self.assertEqual((u.input_tokens, u.cache_read_tokens, u.output_tokens, u.turns), (12297, 128, 1, 1))
        self.assertAlmostEqual(u.cost_usd or 0, 0.01539935)

    def test_parsers_never_raise_on_garbage(self) -> None:
        for garbage in ("", "not json", "{broken", "\n".join(["{}"] * 3), "{\"type\":\"result\"}"):
            for fn in (reviewer.parse_claude_code_usage, reviewer.parse_codex_usage, reviewer.parse_grok_usage):
                with self.subTest(fn=fn.__name__, garbage=garbage[:10]):
                    self.assertIsNone(fn(garbage))

    def test_scan_is_bounded_to_tail(self) -> None:
        huge = ("x" * 100) + "\n"
        stdout = huge * (reviewer.CLI_STDOUT_SCAN_MAX_BYTES // 100 + 10) + CLAUDE_CODE_STREAM
        u = reviewer.parse_claude_code_usage(stdout)
        self.assertIsNotNone(u)


class CostEstimateTests(unittest.TestCase):
    def test_longest_prefix_and_math(self) -> None:
        u = reviewer.UsageTelemetry(input_tokens=1_000_000, output_tokens=100_000, cache_read_tokens=1_000_000, source="api")
        cost = reviewer.estimate_cost_usd("claude-sonnet-5", u)
        # 1M in @ $2 + 1M cached @ $0.2 + 0.1M out @ $10 = 2 + 0.2 + 1 = 3.2
        self.assertAlmostEqual(cost or 0, 3.2, places=4)
        self.assertEqual(reviewer.lookup_indicative_price("glm-5.3-flash"), (0.15, 0.50))
        self.assertEqual(reviewer.lookup_indicative_price("glm-5.3"), (1.40, 4.40))

    def test_unknown_model_is_none(self) -> None:
        self.assertIsNone(reviewer.estimate_cost_usd("my-deployment-name", reviewer.UsageTelemetry(input_tokens=5, source="api")))
        self.assertIsNone(reviewer.estimate_cost_usd("", reviewer.UsageTelemetry(input_tokens=5, source="api")))


class UsageLineTests(unittest.TestCase):
    def test_unavailable(self) -> None:
        line = reviewer.format_usage_line(None, model="m", wall_clock_ms=71000)
        self.assertEqual(line, "**Usage:** not reported by this provider · 71s")
        self.assertNotIn("$", line)

    def test_cli_reported_cost_has_no_indicative_label(self) -> None:
        u = reviewer.parse_grok_usage(GROK_JSON); assert u is not None
        line = reviewer.format_usage_line(u, model="grok-4.3", wall_clock_ms=33000)
        self.assertIn("12.4k in (1% cached)", line)
        self.assertIn("1 out", line)
        self.assertIn("est. $0.02", line)
        self.assertNotIn("indicative", line)
        self.assertIn("1 turn", line)
        self.assertIn("33s", line)

    def test_estimated_cost_is_labelled(self) -> None:
        u = reviewer.UsageTelemetry(input_tokens=41_200, output_tokens=2_100, cache_read_tokens=300_000, turns=6, source="estimated", cost_usd=0.05)
        line = reviewer.format_usage_line(u, model="claude-sonnet-5", wall_clock_ms=0)
        self.assertIn("341.2k in (88% cached)", line)
        self.assertIn("2.1k out", line)
        self.assertIn("est. $0.05 (indicative)", line)
        self.assertIn("6 turns", line)

    def test_tiny_cost(self) -> None:
        u = reviewer.UsageTelemetry(input_tokens=10, output_tokens=1, turns=1, source="estimated", cost_usd=0.00003)
        self.assertIn("est. <$0.01", reviewer.format_usage_line(u, model="x", wall_clock_ms=0))


class WiringTests(unittest.TestCase):
    def test_drive_review_accumulates_usage_into_result(self) -> None:
        class P:
            def __init__(self) -> None:
                self.n = 0
            def complete(self, *, system_prompt: str, messages: list, tools: list) -> dict:
                self.n += 1
                usage = {"input_tokens": 100, "output_tokens": 10, "cache_read_input_tokens": 50}
                if self.n == 1:
                    return {"stop_reason": "tool_use", "content": [{"type": "tool_use", "id": "a", "name": "glob", "input": {"pattern": "*.zzz"}}], "usage": usage}
                return {"stop_reason": "end_turn", "content": [{"type": "text", "text": "done"}], "usage": usage}
        state = reviewer.ReviewState()
        reviewer.drive_review(provider=P(), system_prompt="S", messages=[{"role": "user", "content": "u"}], tools=[], state=state, max_turns=5)
        self.assertEqual((state.usage.input_tokens, state.usage.output_tokens, state.usage.cache_read_tokens, state.usage.turns), (200, 20, 100, 2))
        result = reviewer.state_to_review_result(state)
        assert result.usage is not None
        self.assertEqual(result.usage.turns, 2)

    def test_invoke_cli_agent_attaches_parsed_usage_and_never_fails(self) -> None:
        import tempfile, subprocess as _sp
        with tempfile.TemporaryDirectory() as td:
            fp = Path(td) / ".aiprr" / "findings.json"; fp.parent.mkdir()
            fake = _sp.CompletedProcess(["x"], 0, stdout=CLAUDE_CODE_STREAM, stderr="")
            def fake_run(*_a: Any, **_k: Any) -> Any:  # the "CLI" writes the file (stale files are unlinked first)
                fp.write_text(json.dumps({"summary": "s", "findings": []})); return fake
            with mock.patch.object(reviewer, "_run_cli_process", side_effect=fake_run):
                res = reviewer._invoke_cli_agent(argv=["x"], workspace=Path(td), findings_path=fp, env={}, cli_name="X", usage_parser=reviewer.parse_claude_code_usage)
            assert res.usage is not None
            self.assertEqual(res.usage.output_tokens, 49)
            def boom(_: str) -> Any:
                raise RuntimeError("bad parser")
            with mock.patch.object(reviewer, "_run_cli_process", side_effect=fake_run), mock.patch.object(reviewer, "log"):
                res2 = reviewer._invoke_cli_agent(argv=["x"], workspace=Path(td), findings_path=fp, env={}, cli_name="X", usage_parser=boom)
            self.assertIsNone(res2.usage)
            self.assertEqual(res2.summary, "s")

    def test_codex_argv_requests_json_events(self) -> None:
        import tempfile
        captured: dict[str, Any] = {}
        def fake_invoke(*, argv: list[str], **kw: Any) -> Any:
            captured["argv"] = argv; captured["parser"] = kw.get("usage_parser")
            return reviewer.ReviewResult(summary="ok")
        orig = reviewer._invoke_cli_agent; reviewer._invoke_cli_agent = fake_invoke  # type: ignore[assignment]
        try:
            with tempfile.TemporaryDirectory() as td, mock.patch.object(reviewer, "run_cmd", return_value=type("C", (), {"returncode": 1, "stdout": "", "stderr": ""})()):
                ctx = reviewer.PRContext(title="t", author="a", head_ref="h", base_ref="main", state="open", additions=1, deletions=0, commits=1, body="")
                reviewer.CodexProvider(api_key="k", model="").run_review(pr_context=ctx, review_instructions="R", workspace=Path(td), output_dir=Path(td))
        finally:
            reviewer._invoke_cli_agent = orig  # type: ignore[assignment]
        self.assertIn("--json", captured["argv"])
        self.assertEqual(captured["argv"][-1], "-")
        self.assertIs(captured["parser"], reviewer.parse_codex_usage)

    def test_tracking_body_carries_usage_line(self) -> None:
        body = reviewer.render_tracking_body_done(head_sha="abcdef1234", review_url="u", inline_attached=1, inline_dropped=0, severity="info", blocked=False, block_reason="ok", provider="anthropic", usage_line="**Usage:** 1.0k in · 10 out")
        self.assertTrue(body.rstrip().endswith("**Usage:** 1.0k in · 10 out"))
        body2 = reviewer.render_tracking_body_done(head_sha="abcdef1234", review_url="u", inline_attached=1, inline_dropped=0, severity="info", blocked=False, block_reason="ok", provider="anthropic")
        self.assertNotIn("**Usage:**", body2)

    def test_tracking_body_usage_variants(self) -> None:
        """Unavailable / CLI-reported / estimated lines all render verbatim
        at the end of the body and never add a `$` when nothing is known."""
        kw = dict(head_sha="abcdef1234", review_url="u", inline_attached=0, inline_dropped=0, severity="none", blocked=False, block_reason="ok", provider="grok")
        unavailable = reviewer.format_usage_line(None, model="m", wall_clock_ms=33000)
        self.assertEqual(unavailable, "**Usage:** not reported by this provider · 33s")
        self.assertTrue(reviewer.render_tracking_body_done(usage_line=unavailable, **kw).rstrip().endswith(unavailable))
        est = reviewer.UsageTelemetry(input_tokens=1000, output_tokens=100, source=reviewer.USAGE_SOURCE_ESTIMATED, cost_usd=0.0123, turns=2)
        line = reviewer.format_usage_line(est, model="m", wall_clock_ms=0)
        self.assertIn("(indicative)", line); self.assertIn("2 turns", line); self.assertNotIn("$0.00", line)
        self.assertTrue(reviewer.render_tracking_body_done(usage_line=line, **kw).rstrip().endswith(line))

    def test_iar_output_reflects_real_tokens(self) -> None:
        import os, tempfile
        tel = reviewer.RunTelemetry(start_time_monotonic=0.0)
        tel.usage = reviewer.UsageTelemetry(input_tokens=1200, output_tokens=34, turns=3, source="api")
        tel.tokens_used = tel.usage.total_tokens
        state = reviewer.new_iteration_state(generation_range_hash="h", policy_applied="iterative", base_sha="a" * 40, head_sha="b" * 40)
        pr = reviewer.PolicyResult(findings_to_surface=[], findings_silenced=[], effective_max_inline_comments=10, prompt_addendum="", policy_applied="iterative")
        with tempfile.NamedTemporaryFile("w+", delete=False) as fh:
            path = fh.name
        try:
            with mock.patch.dict(os.environ, {"GITHUB_OUTPUT": path}):
                reviewer.write_iar_outputs_populated(state=state, policy_result=pr, telemetry=tel, effective_cap=10, base_cap=10)
            text = Path(path).read_text()
        finally:
            os.unlink(path)
        self.assertIn("iteration-tokens-used=1234", text)


if __name__ == "__main__":
    unittest.main()


class CursorUsageParserTests(unittest.TestCase):
    def test_single_document_with_usage(self) -> None:
        out = json.dumps({"type": "result", "usage": {"input_tokens": 100, "output_tokens": 20, "cache_read_input_tokens": 50}, "num_turns": 3, "total_cost_usd": 0.01})
        u = reviewer.parse_cursor_usage(out)
        assert u is not None
        self.assertEqual((u.input_tokens, u.output_tokens, u.cache_read_tokens, u.turns, u.cost_usd, u.source), (100, 20, 50, 3, 0.01, "cli"))

    def test_json_lines_take_the_last_usage_object(self) -> None:
        out = "\n".join([json.dumps({"type": "assistant", "text": "hi"}), json.dumps({"type": "result", "usage": {"input_tokens": 5, "output_tokens": 1}})])
        u = reviewer.parse_cursor_usage("noise\n" + out)
        assert u is not None
        self.assertEqual((u.input_tokens, u.output_tokens), (5, 1))

    def test_text_output_or_no_usage_is_ignored(self) -> None:
        self.assertIsNone(reviewer.parse_cursor_usage("Looks good.\n"))
        self.assertIsNone(reviewer.parse_cursor_usage(json.dumps({"type": "result"})))
        self.assertIsNone(reviewer.parse_cursor_usage(""))

