"""`tests/eval/campaign.py` — planning, budget cap (F8), unknown-cost accounting, manifest + ledger output."""

from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any

_ROOT: Path = Path(__file__).resolve().parent.parent
_CP = importlib.util.spec_from_file_location("campaign", _ROOT / "tests" / "eval" / "campaign.py")
assert _CP is not None and _CP.loader is not None
campaign = importlib.util.module_from_spec(_CP)
sys.modules["campaign"] = campaign  # dataclasses + deferred annotations need the module registered
_CP.loader.exec_module(campaign)
_RV = importlib.util.spec_from_file_location("records_validate", _ROOT / "tests" / "eval" / "records_validate.py")
assert _RV is not None and _RV.loader is not None
rv = importlib.util.module_from_spec(_RV)
_RV.loader.exec_module(rv)

EXAMPLE_RECORD: dict[str, Any] = json.loads((_ROOT / "tests" / "eval" / "schemas" / "examples" / "run-record.example.json").read_text())

MANIFEST: dict[str, Any] = {
    "campaign_id": "unit-campaign", "repetitions": 2,
    "lanes": {"grok": {"provider": "grok", "model": "balanced", "api_base": "", "api_key_env": "XAI_API_KEY", "indicative_max_cost_usd": 1.0},
              "glm": {"provider": "claude-code", "model": "economy", "api_base": "https://api.z.ai/api/anthropic", "api_key_env": "ZAI_CODING_API_KEY", "indicative_max_cost_usd": 2.0}},
    "arms": [{"name": "baseline", "prompt": "prompts/default.md", "extension": ".review/extension.md"}],
    "cells": [{"kind": "tree", "case": "tests/eval/cases/C001.json"}, {"kind": "pr", "repo": "o/r", "pr": 46, "worktree": "/tmp/wt"}],
}


def _fake_runner(cost: float | None, *, known: bool = True):
    calls: list[list[str]] = []

    def runner(argv: list[str], record_path: Path, env: dict[str, str]) -> dict[str, Any] | None:
        calls.append(argv)
        rec = dict(EXAMPLE_RECORD)
        rec["run_id"] = f"r-{len(calls)}"
        rec["usage_known"] = known
        rec["cost_usd"] = cost if known else None
        rec["usage"] = rec["usage"] if known else None
        return rec
    runner.calls = calls  # type: ignore[attr-defined]
    return runner


class PlanningTests(unittest.TestCase):
    def test_plan_counts_lanes_arms_cells_reps(self) -> None:
        runs = campaign.plan(MANIFEST, Path("/tmp/out"))
        self.assertEqual(len(runs), 2 * 1 * 2 * 2)
        self.assertEqual(len(campaign.plan(MANIFEST, Path("/tmp/out"), lane_filter="grok")), 4)

    def test_projection_uses_indicative_maxima(self) -> None:
        proj = campaign.projection(MANIFEST, campaign.plan(MANIFEST, Path("/tmp/out")))
        self.assertEqual(proj["projected_max_usd"], 4 * 1.0 + 4 * 2.0)

    def test_manifest_validation_reports_problems(self) -> None:
        bad = json.loads(json.dumps(MANIFEST)); bad["lanes"]["grok"].pop("indicative_max_cost_usd"); bad["cells"].append({"kind": "x"})
        problems = "\n".join(campaign.validate_manifest(bad))
        self.assertIn("indicative_max_cost_usd", problems)
        self.assertIn("must be pr or tree", problems)

    def test_run_eval_command_shapes(self) -> None:
        runs = campaign.plan(MANIFEST, Path("/tmp/out"), lane_filter="grok")
        tree_cmd = campaign.run_eval_command(MANIFEST, next(r for r in runs if r.cell["kind"] == "tree"))
        pr_cmd = campaign.run_eval_command(MANIFEST, next(r for r in runs if r.cell["kind"] == "pr"))
        self.assertIn("--tree", tree_cmd); self.assertIn("--api-key-env", tree_cmd); self.assertNotIn("XAI_API_KEY_VALUE", " ".join(tree_cmd))
        self.assertIn("--worktree", pr_cmd); self.assertIn("--extension", pr_cmd)


class ExecutionTests(unittest.TestCase):
    def test_stops_at_ninety_percent_of_the_budget(self) -> None:
        runner = _fake_runner(1.0)
        with tempfile.TemporaryDirectory() as tmp:
            runs = campaign.plan(MANIFEST, Path(tmp), lane_filter="grok")  # 4 runs × max 1.0
            ledger = campaign.execute(MANIFEST, runs, budget_usd=3.0, records_out=Path(tmp), runner=runner)
        # stop_at = 2.7: run1 (0→1), run2 (1→2), run3 would need 2+1 > 2.7 → stop after 2
        self.assertEqual(ledger["executed"], 2)
        self.assertIn("cap", ledger["stopped_reason"])

    def test_unknown_cost_counts_at_indicative_max(self) -> None:
        runner = _fake_runner(None, known=False)
        with tempfile.TemporaryDirectory() as tmp:
            runs = campaign.plan(MANIFEST, Path(tmp), lane_filter="grok")
            ledger = campaign.execute(MANIFEST, runs, budget_usd=100.0, records_out=Path(tmp), runner=runner)
        self.assertEqual(ledger["unknown_cost_runs"], 4)
        self.assertEqual(ledger["spent_usd_upper_bound"], 4.0)

    def test_records_get_campaign_stamp_and_tree_validates(self) -> None:
        runner = _fake_runner(0.5)
        with tempfile.TemporaryDirectory() as tmp:
            runs = campaign.plan(MANIFEST, Path(tmp), lane_filter="grok")
            campaign.execute(MANIFEST, runs, budget_usd=100.0, records_out=Path(tmp), runner=runner)
            recs = sorted(Path(tmp).rglob("*.run-record.json"))
            self.assertEqual(len(recs), 4)
            first = json.loads(recs[0].read_text())
            self.assertEqual(first["campaign"]["id"], "unit-campaign")
            self.assertIn("grok|baseline|", first["campaign"]["cell"])
            self.assertTrue((Path(tmp) / "campaign.json").is_file())
            self.assertEqual(rv.validate_tree(Path(tmp)), [])

    def test_resume_keeps_completed_records_and_their_cost(self) -> None:
        runs = campaign.plan(MANIFEST, Path("/x"))
        with tempfile.TemporaryDirectory() as tmp:
            runs = campaign.plan(MANIFEST, Path(tmp))
            first = Path(str(runs[0].out) + ".run-record.json")
            first.parent.mkdir(parents=True, exist_ok=True)
            first.write_text(json.dumps({"status": "completed", "usage_known": True, "cost_usd": 0.5,
                                         "campaign": {"id": "x", "cell": "c", "repetition": 0, "arm": "a"}}))
            calls: list[Path] = []

            def runner(argv: list[str], record_path: Path, env: dict[str, str]) -> dict[str, Any] | None:
                calls.append(record_path)
                return {"status": "completed", "usage_known": True, "cost_usd": 0.1}

            ledger = campaign.execute(MANIFEST, runs, budget_usd=100.0, records_out=Path(tmp), runner=runner)
        self.assertEqual(ledger["resumed"], 1)
        self.assertEqual(ledger["executed"], len(runs) - 1)
        self.assertNotIn(first, calls)
        self.assertAlmostEqual(ledger["spent_usd_upper_bound"], 0.5 + 0.1 * (len(runs) - 1), places=4)

    def test_run_refuses_without_budget_and_dry_run_flags_excess(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            m = Path(tmp) / "campaign.json"; m.write_text(json.dumps(MANIFEST))
            self.assertEqual(campaign.main(["dry-run", "--manifest", str(m), "--budget-usd", "100"]), 0)
            self.assertEqual(campaign.main(["dry-run", "--manifest", str(m), "--budget-usd", "5"]), 1)
            with self.assertRaises(SystemExit):
                campaign.main(["run", "--manifest", str(m), "--records-out", tmp])


if __name__ == "__main__":
    unittest.main()
