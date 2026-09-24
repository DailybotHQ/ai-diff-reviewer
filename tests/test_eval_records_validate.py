"""`tests/eval/records_validate.py` — every offline rule over a records tree."""

from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any

_ROOT: Path = Path(__file__).resolve().parent.parent
_DET = importlib.util.spec_from_file_location("determinism", _ROOT / "tests" / "eval" / "determinism.py")
assert _DET is not None and _DET.loader is not None
det = importlib.util.module_from_spec(_DET)
_DET.loader.exec_module(det)
_RV = importlib.util.spec_from_file_location("records_validate", _ROOT / "tests" / "eval" / "records_validate.py")
assert _RV is not None and _RV.loader is not None
rv = importlib.util.module_from_spec(_RV)
_RV.loader.exec_module(rv)

EXAMPLE: dict[str, Any] = json.loads((_ROOT / "tests" / "eval" / "schemas" / "examples" / "run-record.example.json").read_text())
VERDICT_EXAMPLE: dict[str, Any] = json.loads((_ROOT / "tests" / "eval" / "schemas" / "examples" / "verdict.example.json").read_text())


def _write(d: Path, name: str, doc: Any) -> None:
    (d / name).write_text(json.dumps(doc))


class RecordsValidateTests(unittest.TestCase):
    def test_result_twins_are_skipped_and_adjudications_validated(self) -> None:
        adj: dict[str, Any] = {
            "schema": "adjudication/1.0", "campaign_id": "c", "adjudicator": "x", "blind": True, "method": "m",
            "adjudicated_at": "2026-09-23T00:00:00Z", "positive_cases": ["C001"], "precision": {},
            "findings": [{"id": "a", "case": "C001", "verdict": "true", "body_sha256": "0" * 64}],
        }
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp); (d / "adjudications").mkdir()
            _write(d, "pr46-r0.json", {"case": None, "pr": 46, "findings": [], "score": {}})  # run_eval payload
            _write(d, "pr46-r0.json.run-record.json", EXAMPLE)
            _write(d / "adjudications", "c.json", adj)
            self.assertEqual(rv.validate_tree(d), [])
            bad = dict(adj, blind=False, findings=[{"id": "a", "verdict": "maybe", "body": "text"}])
            _write(d / "adjudications", "c.json", bad)
            problems = rv.validate_tree(d)
        self.assertTrue(any("must be blind" in p for p in problems))
        self.assertTrue(any("verdict 'maybe'" in p for p in problems))
        self.assertTrue(any("carries the finding body" in p for p in problems))

    def test_clean_tree_passes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp); (d / "verdicts").mkdir()
            _write(d, "r1.json", EXAMPLE)
            second = dict(EXAMPLE, run_id="p0-baseline-grok-c046-r2")
            _write(d, "r2.json", second)
            _write(d / "verdicts", "v.json", VERDICT_EXAMPLE)
            self.assertEqual(rv.validate_tree(d), [])

    def test_duplicate_run_id_and_unknown_usage_rule(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp)
            _write(d, "a.json", EXAMPLE); _write(d, "b.json", EXAMPLE)
            bad = dict(EXAMPLE, run_id="p0-baseline-grok-c046-r9", usage_known=False)
            _write(d, "c.json", bad)
            problems = "\n".join(rv.validate_tree(d))
        self.assertIn("duplicate run_id", problems)
        self.assertIn("usage_known is false but usage/cost_usd are not null", problems)

    def test_schema_violation_and_unknown_version_reported(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp)
            broken = dict(EXAMPLE); broken.pop("status")
            _write(d, "a.json", broken)
            _write(d, "b.json", {"schema_version": "something/9"})
            problems = "\n".join(rv.validate_tree(d))
        self.assertIn("missing required key 'status'", problems)
        self.assertIn("unknown schema_version", problems)

    def test_manifest_completeness(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp) / "campaigns" / "c1"; d.mkdir(parents=True)
            _write(d, "campaign.json", {"cells": ["grok|xai|grok-4.5|baseline|46"], "repetitions": 3})
            rec = dict(EXAMPLE); rec["campaign"] = dict(EXAMPLE["campaign"], cell="grok|xai|grok-4.5|baseline|46")
            _write(d, "r1.json", rec)
            problems = "\n".join(rv.validate_tree(Path(tmp)))
        self.assertIn("has 1 completed record(s), manifest requires 3", problems)

    def test_cli_exit_codes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp); _write(d, "a.json", EXAMPLE)
            self.assertEqual(rv.main(["--records", str(d)]), 0)
            _write(d, "b.json", EXAMPLE)
            self.assertEqual(rv.main(["--records", str(d)]), 1)

    def test_repository_records_tree_is_valid(self) -> None:
        self.assertEqual(rv.validate_tree(_ROOT / "tests" / "eval" / "records"), [])


if __name__ == "__main__":
    unittest.main()
