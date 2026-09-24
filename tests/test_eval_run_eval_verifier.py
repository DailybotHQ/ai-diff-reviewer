"""Task 19 — `run_eval.py --verifier on|off`: the eval harness runs the v3
verifier + severity policy after the review (precision arms of the Phase 1
campaigns), records the verifier facets in the payload and the run record,
and `campaign.run_eval_command` forwards an arm's `verifier` key.

Unit-only: a scripted review provider and a scripted verifier — no network.
"""
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

_CP = importlib.util.spec_from_file_location("campaign", _ROOT / "tests" / "eval" / "campaign.py")
assert _CP is not None and _CP.loader is not None
campaign = importlib.util.module_from_spec(_CP)
sys.modules["campaign"] = campaign
_CP.loader.exec_module(campaign)

_SC = importlib.util.spec_from_file_location("schema_check", _ROOT / "tests" / "eval" / "schema_check.py")
assert _SC is not None and _SC.loader is not None
schema_check = importlib.util.module_from_spec(_SC)
_SC.loader.exec_module(schema_check)
RUN_SCHEMA: dict[str, Any] = json.loads((_ROOT / "tests" / "eval" / "schemas" / "run-record.schema.json").read_text())

CASE: dict[str, Any] = {
    "schema": "ai-diff-reviewer/eval-case/1", "id": "C901", "title": "synthetic verifier case",
    "stack": "python", "change_class": "auth", "risk_class": "critical_positive", "family_group": "G901",
    "fixture": {
        "kind": "trees",
        "base": {"app.py": "def delete_user(u):\n    require_admin()\n    return remove(u)\n", "README.md": "docs\n"},
        "head": {"app.py": "def delete_user(u):\n    return remove(u)\n", "README.md": "docs\n"},
        "pr_metadata": {"title": "cleanup", "body": "tidy", "deceptive": False},
        "revision_pin": "fixture:sha256:0",
    },
    "labels": [
        {"id": "C901-d1", "severity": "critical", "path": "app.py", "line": 2, "keywords": ["require_admin", "authorization"], "window": 25},
    ],
    "expected": {"must_flag": ["C901-d1"], "must_not_flag": []},
    "adjudication": {"status": "pending"}, "rights": "synthetic",
}

TRUE_CLAIM: dict[str, Any] = {"path": "app.py", "line": 2, "severity": "critical",
                              "body": "The require_admin check was removed — authorization bypass."}
FALSE_CLAIM: dict[str, Any] = {"path": "README.md", "line": 1, "severity": "critical",
                               "body": "Docs leak a hard-coded credential."}
SUPPORTS: list[dict[str, Any]] = [{"kind": "read_anchor", "result": "supports", "target": "app.py:2"}]
CONTRADICTS: list[dict[str, Any]] = [{"kind": "read_anchor", "result": "contradicts", "target": "README.md:1"}]


class _PostingProvider(reviewer.Provider):
    """Posts the given findings on turn 1, submits on turn 2."""

    def __init__(self, findings: list[dict[str, Any]]) -> None:
        self._findings = findings
        self.calls = 0
        self.profile = reviewer.resolve_endpoint_profile("https://api.x.ai/v1", "openai")

    def complete(self, *, system_prompt: str, messages: list[dict[str, Any]], tools: list[dict[str, Any]]) -> dict[str, Any]:
        self.calls += 1
        if self.calls == 1:
            return {"stop_reason": "tool_use", "content": [
                {"type": "tool_use", "id": f"c{i}", "name": "emit_finding", "input": f} for i, f in enumerate(self._findings)
            ], "usage": {"input_tokens": 100, "output_tokens": 20}}
        return {"stop_reason": "tool_use", "content": [{"type": "tool_use", "id": "s", "name": "submit_review", "input": {"summary": "done"}}],
                "usage": {"input_tokens": 50, "output_tokens": 5}}


class _ScriptedVerifier(reviewer.Provider):
    """Verdict by anchor path: app.py claims verify, README claims are refuted."""

    def __init__(self) -> None:
        self.model = "grok-4.5"
        self.profile = reviewer.resolve_endpoint_profile("https://api.x.ai/v1", "openai")
        self.calls = 0

    def complete(self, *, system_prompt: str, messages: list[dict[str, Any]], tools: list[dict[str, Any]]) -> dict[str, Any]:
        self.calls += 1
        text = json.dumps(messages)
        if "README.md" in text:
            verdict = {"status": "refuted", "reason": "README has no credential", "checks": CONTRADICTS}
        else:
            verdict = {"status": "verified", "reason": "require_admin call is gone at head", "checks": SUPPORTS}
        return {"stop_reason": "tool_use", "content": [{"type": "tool_use", "id": "v1", "name": reviewer.VERIFIER_VERDICT_TOOL, "input": verdict}],
                "usage": {"input_tokens": 200, "output_tokens": 30}}


def _run(tmp: Path, *, verifier_on: bool, verifier: Any = None) -> tuple[dict[str, Any], dict[str, Any]]:
    case_path = tmp / "C901.json"; case_path.write_text(json.dumps(CASE))
    out = tmp / "out" / "C901.json"
    payload = run_eval.run_case(
        case_path=case_path, provider=_PostingProvider([TRUE_CLAIM, FALSE_CLAIM]), runtime=reviewer, system_prompt="sys",
        max_turns=5, out=out, provider_id="openai", model="grok-4.5", api_base="https://api.x.ai/v1",
        verifier_policy=reviewer.VerifierPolicy(enabled=verifier_on), verifier_provider=verifier,
    )
    record = json.loads(Path(str(out) + ".run-record.json").read_text())
    return payload, record


class VerifierOffTests(unittest.TestCase):
    def test_off_publishes_claimed_criticals_as_annotated_warnings_and_records_zero_runs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            payload, record = _run(Path(tmp), verifier_on=False)
        self.assertEqual(payload["verifier"]["runs"], 0)
        self.assertEqual(payload["verifier"]["reason"], "verifier off")
        self.assertEqual(payload["refuted"], [])
        self.assertEqual({v["status"] for v in payload["verification"]}, {"skipped"})
        # severity policy: unverified criticals publish as warnings, claimed severity kept
        self.assertEqual([f["severity"] for f in payload["findings"]], ["warning", "warning"])
        self.assertEqual({v["severity_claimed"] for v in payload["verification"]}, {"critical"})
        self.assertEqual(record["budget"]["verifier_runs"], 0)
        self.assertIsNone(record["timings"]["verifier_seconds"])
        self.assertEqual(schema_check.validate(RUN_SCHEMA, record), [])


class VerifierOnTests(unittest.TestCase):
    def test_on_verifies_the_true_claim_and_refutes_the_false_one(self) -> None:
        verifier = _ScriptedVerifier()
        with tempfile.TemporaryDirectory() as tmp:
            payload, record = _run(Path(tmp), verifier_on=True, verifier=verifier)
        self.assertEqual(verifier.calls, 2)
        self.assertEqual(payload["verifier"]["runs"], 2)
        self.assertEqual((payload["verifier"]["verified"], payload["verifier"]["refuted"]), (1, 1))
        self.assertEqual(payload["verifier"]["usage"], {"in": 400, "out": 60})
        self.assertIsNotNone(payload["verifier"]["cost_usd"])
        # the refuted claim leaves the published set and lands in `refuted`
        self.assertEqual([(f["path"], f["severity"]) for f in payload["findings"]], [("app.py", "critical")])
        self.assertEqual([r["path"] for r in payload["refuted"]], ["README.md"])
        self.assertIn("no credential", payload["refuted"][0]["reason"])
        self.assertEqual(payload["verification"][0]["status"], "verified")
        # the score sees only the published findings: recall 1/1, no FP
        self.assertEqual((payload["score"]["must_find_hits"], payload["score"]["must_find_total"]), (1, 1))
        self.assertEqual(payload["score"]["false_positives"], [])
        self.assertEqual((record["outcome"]["findings_verified"], record["outcome"]["findings_refuted"]), (1, 1))
        self.assertEqual(record["budget"]["verifier_runs"], 2)
        self.assertIsInstance(record["timings"]["verifier_seconds"], float)
        self.assertEqual(schema_check.validate(RUN_SCHEMA, record), [])

    def test_on_without_a_lane_provider_fails_open(self) -> None:
        # cursor has no in-process verifier equivalent → unavailable reason, nothing refuted
        with tempfile.TemporaryDirectory() as tmp:
            case_path = Path(tmp) / "C901.json"; case_path.write_text(json.dumps(CASE))
            out = Path(tmp) / "C901.out.json"
            payload = run_eval.run_case(
                case_path=case_path, provider=_PostingProvider([TRUE_CLAIM]), runtime=reviewer, system_prompt="sys",
                max_turns=5, out=out, provider_id="cursor", model="", api_base="",
                verifier_policy=reviewer.VerifierPolicy(enabled=True),
            )
        self.assertEqual(payload["verifier"]["runs"], 0)
        self.assertTrue(payload["verifier"]["reason"])
        self.assertEqual(payload["refuted"], [])
        self.assertEqual(payload["findings"][0]["severity"], "warning")


class CliAndCampaignTests(unittest.TestCase):
    def test_run_parser_accepts_verifier_flag(self) -> None:
        parser = run_eval.build_parser()
        args = parser.parse_args(["run", "--provider", "grok", "--tree", "x.json", "--out", "o.json", "--verifier", "on"])
        self.assertEqual(args.verifier, "on")
        self.assertEqual(parser.parse_args(["run", "--provider", "grok", "--tree", "x.json", "--out", "o.json"]).verifier, "off")

    def test_campaign_arm_forwards_verifier(self) -> None:
        manifest = {
            "campaign_id": "t", "repetitions": 1,
            "lanes": {"grok": {"provider": "grok", "model": "grok-4.5", "api_key_env": "XAI_API_KEY", "indicative_max_cost_usd": 1.0}},
            "arms": [{"name": "verifier-on", "prompt": "prompts/default.md", "verifier": "on"},
                     {"name": "baseline", "prompt": "prompts/default.md"}],
            "cells": [{"kind": "tree", "case": "tests/eval/cases/C001.json"}],
        }
        runs = campaign.plan(manifest, Path("/tmp/out"))
        on_cmd = campaign.run_eval_command(manifest, next(r for r in runs if r.arm == "verifier-on"))
        off_cmd = campaign.run_eval_command(manifest, next(r for r in runs if r.arm == "baseline"))
        self.assertEqual(on_cmd[on_cmd.index("--verifier") + 1], "on")
        self.assertNotIn("--verifier", off_cmd)

    def test_fmt_row_shows_verifier_counts_only_when_it_ran(self) -> None:
        base = {"pr": 1, "provider": "grok", "model": "m", "findings": [], "turns": 1, "seconds": 1.0, "cost_usd": None,
                "score": {"must_find_hits": 0, "must_find_total": 0, "false_positives": [], "unlabelled_findings": 0,
                          "severity_matches": 0, "summary_present": True, "suggestion_blocks": 0}}
        self.assertNotIn("verifier", run_eval.fmt_row(base))
        row = run_eval.fmt_row({**base, "verifier": {"runs": 2, "verified": 1, "downgraded": 0, "refuted": 1, "cost_usd": 0.02}})
        self.assertIn("verifier 1v/0d/1r 2 runs $0.020", row)


if __name__ == "__main__":
    unittest.main()
