"""Task 29 — the eval harness applies the RFC-06 tier budget exactly as
`main` does: tier from the inventory, the matrix row as the turn cap / output
cap / patch bytes / verifier sample, `fixed` = the pre-v3 constants,
`high-risk-paths` → `critical` with the `deep` alias, and the native cap on a
CLI runner (`apply_native_turn_cap`)."""
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


def _case(files_base: dict[str, str], files_head: dict[str, str], title: str = "docs only") -> dict[str, Any]:
    return {
        "schema": "ai-diff-reviewer/eval-case/1", "id": "C991", "title": "budget synthetic", "stack": "python", "change_class": "docs",
        "risk_class": "warning_positive", "family_group": "G991",
        "fixture": {"kind": "trees", "base": files_base, "head": files_head, "pr_metadata": {"title": title, "body": "", "deceptive": False}, "revision_pin": "fixture:sha256:0"},
        "labels": [], "expected": {"must_flag": [], "must_not_flag": []}, "adjudication": {"status": "pending"}, "rights": "synthetic",
    }


DOCS_CASE: dict[str, Any] = _case({"README.md": "# a\n"}, {"README.md": "# a\n\nmore\n"})
CODE_CASE: dict[str, Any] = _case({"app.py": "x = 1\n"}, {"app.py": "x = 2\n"}, title="docs only")


class _Provider(reviewer.Provider):
    """Submits immediately; remembers the model it was built with."""

    def __init__(self, model: str = "grok-4.5") -> None:
        self.model = model; self.profile = reviewer.resolve_endpoint_profile("https://api.x.ai/v1", "openai")

    def complete(self, *, system_prompt: str, messages: list[dict[str, Any]], tools: list[dict[str, Any]]) -> dict[str, Any]:
        return {"stop_reason": "tool_use", "content": [{"type": "tool_use", "id": "s", "name": "submit_review", "input": {"summary": "done"}}], "usage": {"input_tokens": 50, "output_tokens": 5}}


def _run(case: dict[str, Any], **kw: Any) -> tuple[dict[str, Any], dict[str, Any], list[str]]:
    built: list[str] = []
    with tempfile.TemporaryDirectory() as tmp:
        case_path = Path(tmp) / "case.json"; case_path.write_text(json.dumps(case)); out = Path(tmp) / "out.json"
        policy = reviewer.VerifierPolicy(enabled=False)
        payload = run_eval.run_case(case_path=case_path, provider=_Provider(), runtime=reviewer, system_prompt="sys", max_turns=kw.pop("max_turns", 30), out=out,
                                    provider_id="openai", model="grok-4.5", api_base="https://api.x.ai/v1", verifier_policy=policy,
                                    provider_factory=lambda m: (built.append(m), _Provider(m))[1], **kw)
        payload["_policy_pct"] = policy.warning_sample_pct
        return payload, json.loads(Path(str(out) + ".run-record.json").read_text()), built


class HarnessBudget(unittest.TestCase):
    def test_low_tier_row_on_a_docs_only_change(self) -> None:
        payload, record, built = _run(DOCS_CASE)
        self.assertEqual(payload["budget"]["tier"], "low"); self.assertEqual(payload["effective_max_turns"], 8)
        self.assertEqual((payload["budget"]["output_tokens"], payload["budget"]["patch_bytes"], payload["_policy_pct"]), (4096, 60_000, 0))
        self.assertEqual(record["budget"]["risk_tier"], "low"); self.assertEqual(built, [], "balanced stays on the lane's model")

    def test_fixed_profile_restores_the_constants(self) -> None:
        payload, record, _ = _run(DOCS_CASE, budget_profile="fixed")
        self.assertEqual(payload["effective_max_turns"], reviewer.DEFAULT_MAX_TURNS); self.assertEqual(payload["_policy_pct"], 30)
        self.assertEqual(record["budget"]["risk_tier"], "low", "the tier is still recorded under fixed")

    def test_high_risk_paths_force_critical_and_the_deep_alias(self) -> None:
        payload, record, built = _run(CODE_CASE, high_risk_paths="**")
        self.assertEqual(payload["budget"]["tier"], "critical"); self.assertEqual(payload["effective_max_turns"], 40)
        self.assertEqual(payload["budget"]["alias"], "deep"); self.assertEqual(built, ["grok-4.6"]); self.assertEqual(record["model"], "grok-4.6")

    def test_metadata_never_lowers_and_explicit_max_turns_caps(self) -> None:
        payload, _, _ = _run(CODE_CASE, max_turns=12)
        self.assertEqual(payload["budget"]["tier"], "standard", "a 'docs only' title on a code change stays standard")
        self.assertEqual(payload["effective_max_turns"], 12, "an explicit max-turns is a ceiling")

    def test_native_turn_cap_helper(self) -> None:
        class _Grok(reviewer.AgentRunnerProvider):
            def __init__(self) -> None:
                self.max_turns = 0
        g = _Grok()
        self.assertTrue(reviewer.apply_native_turn_cap(g, provider_id="grok", budget_profile="auto", turns=8)); self.assertEqual(g.max_turns, 8)
        g2 = _Grok(); self.assertFalse(reviewer.apply_native_turn_cap(g2, provider_id="grok", budget_profile="fixed", turns=8)); self.assertEqual(g2.max_turns, 0)
        g3 = _Grok(); g3.max_turns = 5; self.assertFalse(reviewer.apply_native_turn_cap(g3, provider_id="grok", budget_profile="auto", turns=8)); self.assertEqual(g3.max_turns, 5, "agent-max-turns set explicitly wins")
        self.assertFalse(reviewer.apply_native_turn_cap(_Provider(), provider_id="openai", budget_profile="auto", turns=8))

    def test_campaign_forwards_the_budget_arm_keys(self) -> None:
        _C = importlib.util.spec_from_file_location("campaign", _ROOT / "tests" / "eval" / "campaign.py"); assert _C is not None and _C.loader is not None
        campaign = importlib.util.module_from_spec(_C); sys.modules["campaign"] = campaign; _C.loader.exec_module(campaign)  # dataclasses need the module registered
        manifest = {"lanes": {"grok": {"provider": "grok", "model": "balanced", "api_key_env": "XAI_API_KEY"}},
                    "arms": [{"name": "critical", "prompt": "prompts/default.md", "verifier": "on", "budget_profile": "auto", "high_risk_paths": "**"}]}
        rp = campaign.RunPlan("grok", "critical", "C001", 0, {"kind": "tree", "case": "tests/eval/cases/C001.json"}, Path("/tmp/x.json"))
        argv = campaign.run_eval_command(manifest, rp)
        self.assertIn("--budget-profile", argv); self.assertEqual(argv[argv.index("--budget-profile") + 1], "auto")
        self.assertEqual(argv[argv.index("--high-risk-paths") + 1], "**")


if __name__ == "__main__":
    unittest.main()
