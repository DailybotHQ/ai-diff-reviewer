"""v3 first message + control-loop contract (RFC-02, BC-03 / BC-04):

- the first user message carries the inventory table and whole-file patches
  up to `FIRST_MESSAGE_PATCH_BYTES`, listing what did not fit;
- `drive_review` returns its stop reason and the turn cap / a silent end
  becomes `status: incomplete` with the partial findings kept;
- a CLI killed at the timeout with a partial findings file yields
  `status: timeout`;
- both are red under blocking strictness and green only under `lenient`;
- `ReviewState.tool_trace` is bounded; the tracking body names the status.
"""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

_ROOT: Path = Path(__file__).resolve().parent.parent
_SPEC = importlib.util.spec_from_file_location("reviewer", _ROOT / "scripts" / "reviewer.py")
assert _SPEC is not None and _SPEC.loader is not None
reviewer = importlib.util.module_from_spec(_SPEC)
sys.modules["reviewer"] = reviewer
_SPEC.loader.exec_module(reviewer)
reviewer.log = lambda msg: None  # type: ignore[assignment]


def _section(path: str, lines: int) -> str:
    body = "".join(f"+line {i} of {path}\n" for i in range(lines))
    return f"diff --git a/{path} b/{path}\n--- a/{path}\n+++ b/{path}\n@@ -0,0 +1,{lines} @@\n{body}"


def _ctx(files: list[tuple[str, int]], *, inventory: bool = True, ceiling: bool = False) -> Any:
    diff = "".join(_section(p, n) for p, n in files)
    if ceiling:
        diff = diff[: reviewer.MAX_DIFF_CHARS] + "\n\n[diff truncated at N characters — use the read_file tool]"
    changed = [{"path": p, "status": "modified", "additions": n, "deletions": 0, "omitted": False} for p, n in files]
    inv = None
    if inventory:
        chars = {path: len(sec) for path, sec in reviewer._diff_sections("".join(_section(p, n) for p, n in files))}
        inv = reviewer.ChangeInventory(
            head_sha="h" * 40, base_sha="b" * 40, base_resolved=True,
            files=[{**f, "previous_path": None, "binary": False, "mode_change": False, "patch_chars": chars[f["path"]]} for f in changed],
        )
    return reviewer.PRContext(title="T", author="a", head_ref="feat", base_ref="main", state="open", additions=1, deletions=0,
                              commits=1, body="Body text", changed_files=changed, diff=diff, omitted_files=[], inventory=inv)


class FirstMessageBudget(unittest.TestCase):
    def test_small_change_embeds_everything_in_inventory_order(self) -> None:
        text = reviewer.render_user_prompt(_ctx([("b.py", 3), ("a.py", 2)]))
        self.assertIn(reviewer.INVENTORY_HEADING, text)
        self.assertIn(reviewer.PATCHES_HEADING, text)
        self.assertIn("2 file(s) embedded whole", text)
        self.assertNotIn(reviewer.NOT_EMBEDDED_HEADING, text)
        self.assertLess(text.index("diff --git a/b.py"), text.index("diff --git a/a.py"), "inventory order, not alphabetical")
        self.assertIn("| `b.py` | modified | +3/-0 | — | ", text)
        self.assertIn("Inventory complete: yes", text)
        self.assertIn(reviewer.DESCRIPTION_HEADING, text)
        self.assertIn("never change what you review", text)

    def test_300k_char_diff_stays_under_budget_and_lists_the_rest(self) -> None:
        files = [(f"src/f{i:02d}.py", 900) for i in range(14)]  # ≈ 14 × 24 KB ≈ 336 KB of diff
        ctx = _ctx(files)
        self.assertGreater(sum(f["patch_chars"] for f in ctx.inventory.files), 300_000)
        embedded, skipped = reviewer.select_first_message_patches(ctx)
        self.assertEqual(len(embedded) + len(skipped), 14)
        self.assertLessEqual(sum(len(sec.encode()) for _, sec in embedded), reviewer.FIRST_MESSAGE_PATCH_BYTES)
        self.assertTrue(skipped)
        text = reviewer.render_user_prompt(ctx)
        self.assertIn(reviewer.NOT_EMBEDDED_HEADING, text)
        for path, chars in skipped:
            self.assertIn(f"- `{path}` ({chars:,} diff chars)", text)
            self.assertNotIn(f"diff --git a/{path}", text)
        self.assertIn("get_patch", text)
        self.assertLessEqual(len(text.encode()), reviewer.FIRST_MESSAGE_PATCH_BYTES + 20_000, "message = budgeted patches + small envelope")

    def test_greedy_fill_lets_a_small_late_file_in(self) -> None:
        big = reviewer.FIRST_MESSAGE_PATCH_BYTES // 30 + 10  # ≈ each big file > 1/30 of the budget
        files = [(f"big{i}.py", big) for i in range(40)] + [("tiny.py", 1)]
        embedded, skipped = reviewer.select_first_message_patches(_ctx(files))
        self.assertIn("tiny.py", [p for p, _ in embedded])
        self.assertTrue(any(p.startswith("big") for p, _ in skipped))

    def test_ceiling_cut_section_is_never_embedded_half_way(self) -> None:
        files = [(f"f{i}.py", 2000) for i in range(6)]
        ctx = _ctx(files, ceiling=True)
        embedded, skipped = reviewer.select_first_message_patches(ctx)
        self.assertNotIn("[diff truncated at", "".join(sec for _, sec in embedded))
        self.assertTrue(skipped)

    def test_agent_runner_closing_names_git_and_no_chat_tools(self) -> None:
        files = [(f"src/f{i:02d}.py", 900) for i in range(14)]
        text = reviewer.render_user_prompt(_ctx(files), for_agent_runner=True)
        self.assertIn("git diff bbbbbbbbbbbb...hhhhhhhhhhhh -- <path>", text)
        self.assertNotIn("get_patch", text)
        self.assertNotIn("submit_review", text)
        self.assertIn("findings file", text)

    def test_context_without_inventory_still_renders(self) -> None:
        text = reviewer.render_user_prompt(_ctx([("a.py", 2)], inventory=False))
        self.assertIn("Inventory completeness: unknown", text)
        self.assertIn("diff --git a/a.py", text)

    def test_max_diff_chars_equals_the_budget(self) -> None:
        self.assertEqual(reviewer.MAX_DIFF_CHARS, reviewer.FIRST_MESSAGE_PATCH_BYTES)
        self.assertEqual(reviewer.FIRST_MESSAGE_PATCH_BYTES, 120_000)


class _LoopingProvider(reviewer.Provider):
    """Calls `glob` on every turn and never submits (or submits on turn N)."""

    def __init__(self, submit_on: int | None = None, end_silently_on: int | None = None) -> None:
        self.turn = 0
        self.submit_on = submit_on
        self.end_silently_on = end_silently_on

    def complete(self, *, system_prompt: str, messages: list[dict[str, Any]], tools: list[dict[str, Any]]) -> dict[str, Any]:
        self.turn += 1
        if self.end_silently_on and self.turn >= self.end_silently_on:
            return {"stop_reason": "end_turn", "content": [{"type": "text", "text": "done thinking"}]}
        blocks: list[dict[str, Any]] = [
            {"type": "tool_use", "id": f"c{self.turn}", "name": "post_inline_comment",
             "input": {"path": "a.py", "line": 1, "body": f"finding {self.turn}", "severity": "warning"}},
        ]
        if self.submit_on and self.turn >= self.submit_on:
            blocks.append({"type": "tool_use", "id": f"s{self.turn}", "name": "submit_review", "input": {"summary": "## Summary\nok"}})
        return {"stop_reason": "tool_use", "content": blocks}


class InProcessCap(unittest.TestCase):
    def _run(self, provider: _LoopingProvider, max_turns: int) -> tuple[str, Any, Any]:
        state = reviewer.ReviewState(max_inline_comments=10)
        messages = [{"role": "user", "content": "review"}]
        stop = reviewer.drive_review(provider=provider, system_prompt="s", messages=messages, tools=reviewer.tools_schema(10), state=state, max_turns=max_turns)
        return stop, state, reviewer.state_to_review_result(state, stop_reason=stop, max_turns=max_turns)

    def test_turn_cap_is_incomplete_with_partial_findings(self) -> None:
        stop, state, result = self._run(_LoopingProvider(), max_turns=3)
        self.assertEqual(stop, reviewer.LOOP_STOP_MAX_TURNS)
        self.assertEqual(result.status, reviewer.REVIEW_STATUS_INCOMPLETE)
        self.assertTrue(result.incomplete)
        self.assertEqual(len(result.findings), 3, "partial findings are kept")
        self.assertIn("turn cap 3 reached", result.status_note)
        self.assertIn("Review incomplete:", result.summary)
        self.assertEqual(len(state.tool_trace), 3)

    def test_silent_end_without_summary_is_incomplete_but_submit_is_completed(self) -> None:
        stop, _, result = self._run(_LoopingProvider(end_silently_on=2), max_turns=5)
        self.assertEqual(stop, reviewer.LOOP_STOP_NO_TOOL_CALLS)
        self.assertEqual(result.status, reviewer.REVIEW_STATUS_INCOMPLETE)
        self.assertIn("without calling submit_review", result.status_note)
        stop, _, result = self._run(_LoopingProvider(submit_on=2), max_turns=5)
        self.assertEqual(stop, reviewer.LOOP_STOP_SUBMITTED)
        self.assertEqual(result.status, reviewer.REVIEW_STATUS_COMPLETED)
        self.assertFalse(result.incomplete)
        self.assertEqual(result.status_note, "")
        self.assertNotIn("Review incomplete", result.summary)

    def test_gate_is_red_under_blocking_strictness_and_green_under_lenient(self) -> None:
        _, _, result = self._run(_LoopingProvider(), max_turns=2)
        for strictness in ("block-on-critical", "block-on-warning", "block-on-any"):
            blocked, reason = reviewer.compute_check_gate(
                severity=result.overall_severity, strictness=strictness, incomplete=result.incomplete, cli_name="anthropic",
                pr_desc_mode="off", description_adequate=True, description_reason="",
                review_status=result.status, status_note=result.status_note,
            )
            self.assertTrue(blocked, strictness)
            self.assertIn("incomplete review — turn cap 2 reached", reason)
        blocked, reason = reviewer.compute_check_gate(
            severity="warning", strictness="lenient", incomplete=False, cli_name="anthropic",
            pr_desc_mode="off", description_adequate=True, description_reason="",
            review_status=result.status, status_note=result.status_note,
        )
        self.assertFalse(blocked)
        self.assertIn("lenient — check stays green", reason)


class CliTimeout(unittest.TestCase):
    def _invoke(self, write_file: bool, *, malformed: bool = False) -> Any:
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            findings_path = tmp / ".aiprr" / "findings.json"

            def fake_run(argv: list[str], **kw: Any) -> Any:
                if write_file:
                    findings_path.parent.mkdir(parents=True, exist_ok=True)
                    findings_path.write_text("{not json" if malformed else json.dumps(
                        {"summary": "## Summary\npartial", "findings": [{"path": "a.py", "line": 1, "body": "b", "severity": "critical"}]}))
                raise subprocess.TimeoutExpired(cmd=argv, timeout=reviewer.CLI_INVOCATION_TIMEOUT)

            with mock.patch.object(reviewer, "_run_cli_process", side_effect=fake_run):
                return reviewer._invoke_cli_agent(argv=["x"], workspace=tmp, findings_path=findings_path, env={**os.environ}, cli_name="TestCLI")

    def test_partial_findings_file_becomes_status_timeout(self) -> None:
        res = self._invoke(write_file=True)
        self.assertEqual(res.status, reviewer.REVIEW_STATUS_TIMEOUT)
        self.assertTrue(res.incomplete)
        self.assertEqual(len(res.findings), 1)
        self.assertIn("Review timed out:", res.summary)
        self.assertIn(f"{reviewer.CLI_INVOCATION_TIMEOUT}s timeout", res.status_note)
        blocked, reason = reviewer.compute_check_gate(
            severity=res.overall_severity, strictness="block-on-critical", incomplete=res.incomplete, cli_name="TestCLI",
            pr_desc_mode="off", description_adequate=True, description_reason="", review_status=res.status, status_note=res.status_note,
        )
        self.assertTrue(blocked)
        self.assertIn("timed-out review", reason)

    def test_no_file_or_unreadable_file_still_raises(self) -> None:
        with self.assertRaises(RuntimeError):
            self._invoke(write_file=False)
        with self.assertRaises(RuntimeError):
            self._invoke(write_file=True, malformed=True)


class TraceAndTracking(unittest.TestCase):
    def test_tool_trace_is_bounded_and_redacted(self) -> None:
        state = reviewer.ReviewState()
        old = reviewer.MAX_TOOL_TRACE_ENTRIES
        reviewer.MAX_TOOL_TRACE_ENTRIES = 3
        try:
            for i in range(5):
                reviewer.execute_tool("glob", {"pattern": f"*{i}", "api_key": "sk-secret"}, state)
        finally:
            reviewer.MAX_TOOL_TRACE_ENTRIES = old
        self.assertEqual(len(state.tool_trace), 3)
        self.assertEqual(state.tool_trace_overflow, 2)
        entry = state.tool_trace[0]
        self.assertEqual(entry["name"], "glob")
        self.assertEqual(len(entry["result_sha256"]), 64)
        self.assertNotIn("sk-secret", json.dumps(entry))
        self.assertEqual([e["index"] for e in state.tool_trace], [0, 1, 2])

    def test_tracking_body_names_the_status(self) -> None:
        kw = dict(head_sha="abc1234def", review_url="u", inline_attached=1, inline_dropped=0, severity="warning", blocked=True, block_reason="r")
        body = reviewer.render_tracking_body_done(review_status="incomplete", status_note="turn cap 30 reached", **kw)
        self.assertIn("**Review incomplete:** ⚠️ turn cap 30 reached", body)
        body = reviewer.render_tracking_body_done(review_status="timeout", status_note="killed at 900s", **kw)
        self.assertIn("**Review timed out:** ⚠️ killed at 900s", body)
        self.assertNotIn("Review incomplete", reviewer.render_tracking_body_done(**kw))

    def test_run_record_accepts_every_review_status(self) -> None:
        for status in ("completed", "incomplete", "timeout"):
            doc = reviewer.RunRecord().to_dict(status=status, failure_class=None)
            self.assertEqual(doc["status"], status)


if __name__ == "__main__":
    unittest.main()
