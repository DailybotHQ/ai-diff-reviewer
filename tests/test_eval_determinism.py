"""`tests/eval/determinism.py` — cells, noise floor and the RFC-01 verdict rules."""

from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any

_ROOT: Path = Path(__file__).resolve().parent.parent
_SPEC = importlib.util.spec_from_file_location("determinism", _ROOT / "tests" / "eval" / "determinism.py")
assert _SPEC is not None and _SPEC.loader is not None
det = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(det)

_SC = importlib.util.spec_from_file_location("schema_check", _ROOT / "tests" / "eval" / "schema_check.py")
assert _SC is not None and _SC.loader is not None
schema_check = importlib.util.module_from_spec(_SC)
_SC.loader.exec_module(schema_check)
VERDICT_SCHEMA: dict[str, Any] = json.loads((_ROOT / "tests" / "eval" / "schemas" / "verdict.schema.json").read_text())


def _campaign(arm: str, cost: float, hits: int, cases: int = 4, reps: int = 3) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for c in range(cases):
        for i in range(reps):
            out.append(det._rec(run_id=f"{arm}-{c}-{i}", case=f"C{c:03d}", arm=arm, cost_usd=cost + 0.02 * i, hits=hits))
    return out


class SummarizeTests(unittest.TestCase):
    def test_selftest_passes(self) -> None:
        self.assertEqual(det.selftest(), 0)

    def test_cells_exclude_failed_and_flag_descriptive(self) -> None:
        recs = [det._rec(run_id="a", cost_usd=1.0), det._rec(run_id="b", status="failed")]
        s = det.summarize(recs)
        cell = s["cells"]["grok|xai|grok-4.5|baseline|C001"]
        self.assertEqual(cell["n"], 1)
        self.assertTrue(cell["descriptive_only"])
        self.assertIsNone(cell["cost_relative_spread"])
        self.assertEqual(s["records_failed"], 1)

    def test_unknown_usage_counts_not_zero_cost(self) -> None:
        recs = [det._rec(run_id="u", usage_known=False, cost_usd=None)]
        s = det.summarize(recs)
        self.assertEqual(s["campaign_cost"]["sum_usd"], 0.0)
        self.assertEqual(s["campaign_cost"]["records_without_cost"], 1)
        self.assertEqual(s["cells"]["grok|xai|grok-4.5|baseline|C001"]["usage_unknown_runs"], 1)

    def test_load_records_ignores_non_run_json_and_is_bounded(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp)
            (d / "a.json").write_text(json.dumps(det._rec(run_id="a")))
            (d / "other.json").write_text(json.dumps({"schema_version": "verdict/1.0"}))
            (d / "bad.json").write_text("{not json")
            recs = det.load_records(d)
        self.assertEqual([r["run_id"] for r in recs], ["a"])


class VerdictTests(unittest.TestCase):
    def test_promotable_when_cheaper_beyond_threshold_and_ci_excludes_zero(self) -> None:
        v = det.verdict(_campaign("baseline", 1.0, 3), _campaign("cand", 0.6, 3), candidate_runtime_sha="x", prompt_sha256="0" * 64, baseline_ref="b")
        self.assertTrue(v["promotable"]["cost"])
        self.assertFalse(v["promotable"]["recall"])
        self.assertEqual(v["blocking"], [])
        self.assertEqual(schema_check.validate(VERDICT_SCHEMA, v), [])

    def test_small_cost_gain_inside_noise_is_not_promotable(self) -> None:
        v = det.verdict(_campaign("baseline", 1.0, 3), _campaign("cand", 0.9, 3), candidate_runtime_sha="x", prompt_sha256="0" * 64, baseline_ref="b")
        self.assertFalse(v["promotable"]["cost"])

    def test_recall_regression_blocks(self) -> None:
        v = det.verdict(_campaign("baseline", 1.0, 4), _campaign("cand", 1.0, 1), candidate_runtime_sha="x", prompt_sha256="0" * 64, baseline_ref="b")
        self.assertTrue(any("recall regression" in b for b in v["blocking"]))

    def test_incomplete_candidate_lane_blocks(self) -> None:
        # PR #61 self-review (glm, warning): a lane that finished half its cells must not mint a
        # non-blocking verdict (the verdict job used to run even after a failed campaign).
        base = _campaign("baseline", 1.0, 4, cases=6)
        cand = [r for r in _campaign("cand", 1.0, 4, cases=6) if r["context"]["corpus_case_id"] in ("C000", "C001", "C002", "C003")]
        v = det.verdict(base, cand, candidate_runtime_sha="x", prompt_sha256="0" * 64, baseline_ref="b")
        self.assertTrue(any(b.startswith("incomplete candidate lane") and "2 of 6" in b for b in v["blocking"]), v["blocking"])
        full = det.verdict(base, _campaign("cand", 1.0, 4, cases=6), candidate_runtime_sha="x", prompt_sha256="0" * 64, baseline_ref="b")
        self.assertFalse(any(b.startswith("incomplete candidate lane") for b in full["blocking"]))

    def test_first_party_unknown_usage_blocks(self) -> None:
        cand = _campaign("cand", 1.0, 3)
        for r in cand:
            r["endpoint_kind"] = "anthropic"; r["provider"] = "anthropic"
        cand[0]["usage_known"] = False; cand[0]["cost_usd"] = None
        base = _campaign("baseline", 1.0, 3)
        for r in base:
            r["endpoint_kind"] = "anthropic"; r["provider"] = "anthropic"
        v = det.verdict(base, cand, candidate_runtime_sha="x", prompt_sha256="0" * 64, baseline_ref="b")
        self.assertTrue(any("unknown usage" in b for b in v["blocking"]))

    def test_widened_determinism_blocks(self) -> None:
        cand = _campaign("cand", 1.0, 3)
        for r in cand:
            if r["run_id"].endswith("-2"):
                r["cost_usd"] = 3.0  # worst spread > 1.0
        v = det.verdict(_campaign("baseline", 1.0, 3), cand, candidate_runtime_sha="x", prompt_sha256="0" * 64, baseline_ref="b")
        self.assertTrue(any("determinism widened" in b for b in v["blocking"]))

    def test_fewer_than_three_reps_is_descriptive_only(self) -> None:
        v = det.verdict(_campaign("baseline", 1.0, 3, reps=2), _campaign("cand", 0.5, 3, reps=2), candidate_runtime_sha="x", prompt_sha256="0" * 64, baseline_ref="b")
        self.assertTrue(v["descriptive_only"])
        self.assertFalse(v["promotable"]["cost"])

    def test_cli_verdict_writes_file_and_exit_code_reflects_blocking(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            b = Path(tmp) / "b"; c = Path(tmp) / "c"; b.mkdir(); c.mkdir()
            for r in _campaign("baseline", 1.0, 4):
                (b / f"{r['run_id']}.json").write_text(json.dumps(r))
            for r in _campaign("cand", 1.0, 1):
                (c / f"{r['run_id']}.json").write_text(json.dumps(r))
            out = Path(tmp) / "v.json"
            code = det.main(["verdict", "--baseline", str(b), "--candidate", str(c), "--out", str(out)])
            self.assertEqual(code, 1)
            self.assertEqual(schema_check.validate(VERDICT_SCHEMA, json.loads(out.read_text())), [])


if __name__ == "__main__":
    unittest.main()
