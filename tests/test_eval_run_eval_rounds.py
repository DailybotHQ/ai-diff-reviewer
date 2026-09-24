"""Task 27 — the eval harness replays multi-round fixtures: round 2 in
incremental mode (delta round1 → head, the round-1 findings as priors, the
RFC-06 turn budget) and the verifier-only round (round1 → round1)."""
from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any

_ROOT: Path = Path(__file__).resolve().parent.parent
_RE = importlib.util.spec_from_file_location("run_eval", _ROOT / "tests" / "eval" / "run_eval.py")
assert _RE is not None and _RE.loader is not None
run_eval = importlib.util.module_from_spec(_RE)
_RE.loader.exec_module(run_eval)
reviewer = run_eval.load_runtime()

CASE: dict[str, Any] = {
    "schema": "ai-diff-reviewer/eval-case/1", "id": "C990", "title": "multi-round synthetic", "stack": "python", "change_class": "correctness",
    "risk_class": "warning_positive", "family_group": "G990",
    "fixture": {
        "kind": "trees",
        "base": {"utils.py": "def total(items):\n    return sum(items)\n"},
        "iar": {"round1_head": {"utils.py": "def total(items):\n    return sum(i.price for i in items)\n\n\ndef first_name(user):\n    return user.name.split()[0]\n"},
                "round1_findings": [{"path": "utils.py", "line": 2, "severity": "warning", "body": "total() callers pass None — guard it."},
                                    {"path": "utils.py", "line": 6, "severity": "warning", "body": "first_name() crashes on an empty name."}]},
        "head": {"utils.py": "def total(items):\n    return sum(i.price for i in items or [])\n\n\ndef first_name(user):\n    return user.name.split()[0]\n\n\ndef last_name(user):\n    return user.name.split()[-1]\n"},
        "pr_metadata": {"title": "round 2", "body": "guard None", "deceptive": False}, "revision_pin": "fixture:sha256:0",
    },
    "labels": [{"id": "C990-d1", "severity": "warning", "path": "utils.py", "line": 9, "keywords": ["last_name", "empty"], "window": 25}],
    "expected": {"must_flag": ["C990-d1"], "must_not_flag": []}, "adjudication": {"status": "pending"}, "rights": "synthetic",
}


class _Provider(reviewer.Provider):
    """Records the first message; posts one finding on the new line, marks prior #1 resolved, submits."""

    def __init__(self) -> None:
        self.calls = 0; self.first_message = ""; self.tool_names: set[str] = set()
        self.profile = reviewer.resolve_endpoint_profile("https://api.x.ai/v1", "openai")

    def complete(self, *, system_prompt: str, messages: list[dict[str, Any]], tools: list[dict[str, Any]]) -> dict[str, Any]:
        self.calls += 1; self.tool_names = {t["name"] for t in tools}
        if self.calls == 1:
            self.first_message = str(messages[0]["content"])
            fps = [line for line in self.first_message.splitlines() if "fp=" in line or "`" in line]
            return {"stop_reason": "tool_use", "content": [
                {"type": "tool_use", "id": "c1", "name": "emit_finding", "input": {"path": "utils.py", "line": 9, "severity": "warning", "body": "last_name() crashes on an empty name."}},
            ], "usage": {"input_tokens": 300, "output_tokens": 30}}
        return {"stop_reason": "tool_use", "content": [{"type": "tool_use", "id": "s", "name": "submit_review", "input": {"summary": "done"}}], "usage": {"input_tokens": 50, "output_tokens": 5}}


class _Verifier(reviewer.Provider):
    def __init__(self) -> None:
        self.calls = 0; self.profile = reviewer.resolve_endpoint_profile("https://api.x.ai/v1", "openai")

    def complete(self, *, system_prompt: str, messages: list[dict[str, Any]], tools: list[dict[str, Any]]) -> dict[str, Any]:
        self.calls += 1
        return {"stop_reason": "tool_use", "content": [{"type": "tool_use", "id": "v", "name": reviewer.VERIFIER_VERDICT_TOOL,
                "input": {"status": "verified", "reason": "still there", "checks": [{"kind": "read_anchor", "result": "supports", "target": "utils.py:2"}]}}],
                "usage": {"input_tokens": 80, "output_tokens": 8}}


def _run(tmp: Path, round_mode: str, provider: Any, verifier: Any = None) -> tuple[dict[str, Any], dict[str, Any]]:
    case_path = tmp / "C990.json"; case_path.write_text(json.dumps(CASE)); out = tmp / "out" / f"C990-{round_mode or 'full'}.json"
    payload = run_eval.run_case(case_path=case_path, provider=provider, runtime=reviewer, system_prompt="sys", max_turns=30, out=out, provider_id="openai",
                                model="grok-4.5", api_base="https://api.x.ai/v1", round_mode=round_mode,
                                verifier_policy=reviewer.VerifierPolicy(enabled=verifier is not None), verifier_provider=verifier)
    return payload, json.loads(Path(str(out) + ".run-record.json").read_text())


class MultiRound(unittest.TestCase):
    def test_materialise_with_round1_yields_three_commits(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo, base, r1, head = run_eval.materialise_tree(CASE, Path(tmp), with_round1=True)
            self.assertEqual(len({base, r1, head}), 3)
            self.assertIn("first_name", run_eval._git(repo, "show", f"{r1}:utils.py"))
            self.assertNotIn("first_name", run_eval._git(repo, "show", f"{base}:utils.py"))

    def test_round_two_is_incremental_with_the_rfc_budget(self) -> None:
        prov = _Provider()
        with tempfile.TemporaryDirectory() as tmp:
            payload, record = _run(Path(tmp), "2", prov)
        self.assertEqual(payload["round"], "2")
        self.assertEqual(payload["effective_max_turns"], reviewer.incremental_budget(1, 2, 30), "1 changed file, 2 outstanding → 4 + 1.5 + 2 → 8")
        self.assertEqual(record["context"]["iar_mode"], "incremental")
        self.assertIn(reviewer.IAR_INCREMENTAL_DIFF_HEADING, prov.first_message, "the first message is the delta, not the full diff")
        self.assertIn("first_name", prov.first_message, "prior findings are listed for the model")
        self.assertIn("update_prior_finding", prov.tool_names)
        self.assertEqual((payload["score"]["must_find_hits"], payload["score"]["must_find_total"]), (1, 1))

    def test_no_change_round_runs_the_verifier_only(self) -> None:
        prov = _Provider(); ver = _Verifier()
        with tempfile.TemporaryDirectory() as tmp:
            payload, record = _run(Path(tmp), "nochange", prov, ver)
        self.assertEqual(prov.calls, 0, "no model review on a no-change round")
        self.assertEqual((ver.calls, payload["verifier"]["runs"], record["budget"]["turns_used"], record["budget"]["verifier_runs"]), (2, 2, 0, 2))
        self.assertEqual(record["context"]["iar_mode"], "verifier-only")
        self.assertEqual([v["status"] for v in payload["outstanding_verdicts"]], ["verified", "verified"])
        self.assertIn("spent no review turns", payload["summary"])
        self.assertEqual(payload["findings"], [])

    def test_campaign_forwards_the_round(self) -> None:
        _CP = importlib.util.spec_from_file_location("campaign", _ROOT / "tests" / "eval" / "campaign.py"); campaign = importlib.util.module_from_spec(_CP)
        sys.modules["campaign"] = campaign; _CP.loader.exec_module(campaign)
        manifest = {"campaign_id": "t", "repetitions": 1, "lanes": {"grok": {"provider": "grok", "model": "grok-4.5", "api_key_env": "XAI_API_KEY", "indicative_max_cost_usd": 1.0}},
                    "arms": [{"name": "incremental", "prompt": "prompts/default.md", "round": "2"}, {"name": "full", "prompt": "prompts/default.md"}],
                    "cells": [{"kind": "tree", "case": "tests/eval/cases/C091.json"}]}
        runs = campaign.plan(manifest, Path("/tmp/out"))
        inc = campaign.run_eval_command(manifest, next(r for r in runs if r.arm == "incremental")); full = campaign.run_eval_command(manifest, next(r for r in runs if r.arm == "full"))
        self.assertEqual(inc[inc.index("--round") + 1], "2"); self.assertNotIn("--round", full)


if __name__ == "__main__":
    unittest.main()
