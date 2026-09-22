"""Smoke tests for the comparison harness entry points (tests/eval/).

The harness lives outside `unittest discover`; these tests exercise its
pure functions and the report command so a crash like an UnboundLocalError
in `validate_manifest` can never ship silently (round-6 review).
"""

from __future__ import annotations

import argparse
import json
import tempfile
import unittest
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent / "eval"))

import jev_experiment  # noqa: E402


class HarnessSmokeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.cases_dir = Path(__file__).resolve().parent / "eval" / "cases"
        cls.cases, f = jev_experiment.corpus_validate_load_cases_safe()
        assert not f.items, f.items

    def test_validate_manifest_accepts_a_fresh_init(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            manifest = jev_experiment.build_manifest(
                "smoke-salt", self.cases_dir, Path(tmp) / "m.json"
            )
            jev_experiment.validate_manifest(manifest, self.cases)  # must not raise

    def test_validate_manifest_rejects_fabricated_splits(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            manifest = jev_experiment.build_manifest(
                "smoke-salt", self.cases_dir, Path(tmp) / "m.json"
            )
            manifest["splits"]["C999"] = "heldout"
            manifest["splits"][next(iter(manifest["splits"]))] = "nonsense-phase"
            with self.assertRaises(jev_experiment.ExperimentError) as ctx:
                jev_experiment.validate_manifest(manifest, self.cases)
            self.assertIn("unknown case ids", str(ctx.exception))
            self.assertIn("invalid phase values", str(ctx.exception))

    def test_plan_runs_assigns_repetitions(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            manifest = jev_experiment.build_manifest(
                "smoke-salt", self.cases_dir, Path(tmp) / "m.json"
            )
            plans, estimate = jev_experiment.plan_runs(
                manifest, self.cases, "calibration", seed=1
            )
            self.assertTrue(plans)
            reps = {p.rep for p in plans}
            self.assertEqual(reps, set(range(manifest["repetitions"])))

    def test_report_fails_on_oversized_run_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            manifest = jev_experiment.build_manifest(
                "smoke-salt", self.cases_dir, Path(tmp) / "m.json"
            )
            runs = Path(tmp) / "runs"
            runs.mkdir()
            good = runs / "run_1.json"
            good.write_text(json.dumps({
                "run_id": "run_1.json", "arm": "baseline", "lane": "grok",
                "case_id": sorted(manifest["splits"])[0], "status": "completed",
                "rep": 0, "provider_seconds": 1.0,
            }))
            bad = runs / "run_2.json"
            bad.write_text("x" * (jev_experiment.MAX_RUN_FILE_BYTES + 1))
            args = argparse.Namespace(manifest=str(Path(tmp) / "m.json"), runs=str(runs))
            self.assertEqual(jev_experiment.cmd_report(args), 1)


if __name__ == "__main__":
    unittest.main()
