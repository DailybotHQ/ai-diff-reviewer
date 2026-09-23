"""`tests/eval/adjudicate.py`: blinded, source-grounded adjudication records for precision (RFC-01 § Metrics, F7)."""

from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any

_ROOT: Path = Path(__file__).resolve().parent.parent
_SPEC = importlib.util.spec_from_file_location("adjudicate", _ROOT / "tests" / "eval" / "adjudicate.py")
assert _SPEC is not None and _SPEC.loader is not None
adjudicate = importlib.util.module_from_spec(_SPEC)
sys.modules["adjudicate"] = adjudicate
_SPEC.loader.exec_module(adjudicate)

CASE: dict[str, Any] = {
    "id": "C001",
    "fixture": {"kind": "trees", "head": {"app.py": "\n".join(f"line {i}" for i in range(1, 40))}},
    "labels": [
        {"id": "C001-d1", "severity": "critical", "path": "app.py", "keywords": ["require_role", "decorator"], "defect": "auth removed"},
        {"id": "C001-t1", "severity": "warning", "path": "README.md", "keywords": ["typo"]},
    ],
    "expected": {"must_flag": ["C001-d1"], "must_not_flag": ["C001-t1"]},
}
F_TRUE: dict[str, Any] = {"path": "app.py", "line": 19, "severity": "critical", "body": "The require_role decorator was removed."}
F_STYLE: dict[str, Any] = {"path": "app.py", "line": 30, "severity": "info", "body": "Prefer f-strings here."}
F_NOT: dict[str, Any] = {"path": "README.md", "line": 2, "severity": "warning", "body": "Typo in heading."}


def _results() -> list[dict[str, Any]]:
    a = {"case": "C001", "findings": [F_TRUE, F_STYLE], "_lane": "grok|xai|grok-4.5", "_arm": "default", "_source": "r0"}
    b = {"case": "C001", "findings": [dict(F_TRUE), F_NOT], "_lane": "grok|xai|grok-4.5", "_arm": "default", "_source": "r1"}
    return [a, b]


class Classification(unittest.TestCase):
    def test_ground_truth_join_is_a_hint_per_label_kind(self) -> None:
        self.assertEqual(adjudicate.classify(CASE, F_TRUE), "matches C001-d1")
        self.assertEqual(adjudicate.classify(CASE, F_NOT), "must_not_flag")
        self.assertEqual(adjudicate.classify(CASE, F_STYLE), "unlabelled")
        self.assertEqual(adjudicate.classify(None, F_STYLE), "no_case")

    def test_excerpt_marks_the_anchor_line_from_the_head_tree(self) -> None:
        text = adjudicate.excerpt(CASE, "app.py", 19)
        self.assertIn("  19> line 19", text)
        self.assertIn("  11  line 11", text)
        self.assertNotIn("line 10\n", text)
        self.assertEqual(adjudicate.excerpt(CASE, "missing.py", 1), "(path not in head tree)")
        self.assertEqual(adjudicate.excerpt(CASE, "app.py", None), "")


class Worksheet(unittest.TestCase):
    def setUp(self) -> None:
        self._orig = adjudicate.load_case
        adjudicate.load_case = lambda cid: CASE if cid == "C001" else None  # type: ignore[assignment]

    def tearDown(self) -> None:
        adjudicate.load_case = self._orig  # type: ignore[assignment]

    def test_items_are_deduplicated_blinded_and_keyed(self) -> None:
        ws = adjudicate.build_worksheet(_results(), seed=1)
        self.assertEqual(len(ws["items"]), 3)  # F_TRUE twice → once
        for it in ws["items"]:
            self.assertNotIn("lane", it); self.assertNotIn("arm", it); self.assertNotIn("source", it)
            self.assertIsNone(it["verdict"])
        true_item = next(it for it in ws["items"] if it["ground_truth"] == "matches C001-d1")
        self.assertEqual(ws["sealed_key"][true_item["id"]]["occurrences"], 2)
        self.assertEqual(ws["sealed_key"][true_item["id"]]["lanes"], ["grok|xai|grok-4.5"])
        self.assertEqual(ws["positive_cases"], ["C001"])

    def test_seal_refuses_missing_verdicts_and_computes_precision(self) -> None:
        ws = adjudicate.build_worksheet(_results(), seed=1)
        with self.assertRaises(SystemExit):
            adjudicate.seal(ws, adjudicator="x", campaign_id="c")
        for it in ws["items"]:
            it["verdict"] = {"matches C001-d1": "true", "unlabelled": "false", "must_not_flag": "overstated"}[it["ground_truth"]]
        rec = adjudicate.seal(ws, adjudicator="x", campaign_id="c")
        self.assertEqual(rec["schema"], adjudicate.SCHEMA_VERSION)
        self.assertTrue(rec["blind"])
        self.assertEqual(rec["precision"]["overall"], {"true": 1, "false": 1, "overstated": 1, "precision": 0.6667})
        self.assertEqual(rec["precision"]["per_lane"]["grok|xai|grok-4.5"]["precision"], 0.6667)
        self.assertTrue(all("body" not in f and f["body_sha256"] for f in rec["findings"]))

    def test_pr_path_results_are_loaded_under_a_pr_case_id(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "pr46-r0.json").write_text(json.dumps({"pr": 46, "repo": "o/r", "findings": [F_STYLE], "score": {}}))
            (root / "pr46-r0.json.run-record.json").write_text(json.dumps({"provider": "grok", "endpoint_kind": "xai", "model": "grok-4.5", "campaign": {"arm": "default"}}))
            results = adjudicate.load_results(root)
        self.assertEqual([r["case"] for r in results], ["pr46"])
        ws = adjudicate.build_worksheet(results, seed=1)
        self.assertEqual(len(ws["items"]), 1)
        self.assertEqual(ws["items"][0]["ground_truth"], "no_case")
        self.assertEqual(ws["positive_cases"], ["pr46"])

    def test_cli_round_trip_on_disk(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for i, res in enumerate(_results()):
                res = {k: v for k, v in res.items() if not k.startswith("_")}
                (root / f"C001-r{i}.json").write_text(json.dumps(res))
                (root / f"C001-r{i}.json.run-record.json").write_text(json.dumps(
                    {"provider": "grok", "endpoint_kind": "xai", "model": "grok-4.5", "campaign": {"arm": "default"}}))
            (root / "ledger.json").write_text("{}")
            ws_path = root / "ws.json"
            self.assertEqual(adjudicate.main(["worksheet", "--results", str(root), "--out", str(ws_path)]), 0)
            ws = json.loads(ws_path.read_text())
            self.assertEqual(len(ws["items"]), 3)
            for it in ws["items"]:
                it["verdict"] = "true"
            ws_path.write_text(json.dumps(ws))
            out = root / "adj.json"
            self.assertEqual(adjudicate.main(["seal", "--worksheet", str(ws_path), "--out", str(out), "--adjudicator", "t", "--campaign-id", "c"]), 0)
            self.assertEqual(adjudicate.main(["precision", "--record", str(out)]), 0)
            self.assertEqual(json.loads(out.read_text())["precision"]["overall"]["precision"], 1.0)


if __name__ == "__main__":
    unittest.main()
