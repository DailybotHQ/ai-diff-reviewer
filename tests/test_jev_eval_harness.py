#!/usr/bin/env python3
"""Offline tests for the controlled-comparison harness (Task 4).

Nothing here performs AI calls or GitHub writes; the manifest, splitter,
planner, triage rules and report aggregation are exercised directly.

Run: python3 -m unittest discover -s tests -p 'test_jev_eval*.py' -v
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

TESTS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(TESTS_DIR / "eval"))

import corpus_validate  # noqa: E402
import jev_experiment as je  # noqa: E402

REAL_CASES = TESTS_DIR / "eval" / "cases"


def minimal_corpus(tmp: Path, critical: int = 14, negative: int = 11, warning: int = 4) -> dict:
    """A synthetic corpus large enough for split floors, tiny enough to be fast."""
    def case(cid: str, risk: str, group: str) -> dict:
        c = {
            "schema": corpus_validate.SCHEMA, "id": cid, "title": f"Synthetic {cid}",
            "stack": "python", "change_class": "logic_bug", "risk_class": risk,
            "family_group": group,
            "fixture": {
                "kind": "trees",
                "base": {"a.py": "x = 1\n"},
                "head": {"a.py": "x = 2\n"},
                "revision_pin": "PIN",
                "pr_metadata": {"title": f"Change {cid}", "body": "d"},
            },
            "labels": [],
            "expected": {"must_flag": [], "must_not_flag": [], "reference_review": "x" * 60},
            "adjudication": {"status": "pending"},
            "rights": {"origin": "synthetic", "egress": "synthetic"},
        }
        if risk != "negative_control":
            c["expected"] = {"must_flag": [], "must_not_flag": []}
            c.pop("labels")
            c["labels"] = [{
                "id": f"{cid}-d1", "severity": "critical" if risk == "critical_positive" else "warning",
                "path": "a.py", "defect": "d", "evidence": "e", "introduced_by_change": True,
                "keywords": ["x"], "window": 25,
            }]
            if risk == "critical_positive":
                c["adjudication"] = {"status": "adjudicated", "adjudicator": "independent-reviewer-v1",
                                     "blind": True, "verdict": "confirmed"}
        return c

    n = 1
    out = {}
    for _ in range(critical):
        out[f"C{900 + n:03d}"] = case(f"C{900 + n:03d}", "critical_positive", f"GC{n}")
        n += 1
    for _ in range(warning):
        out[f"C{900 + n:03d}"] = case(f"C{900 + n:03d}", "warning_positive", f"GW{n}")
        n += 1
    for _ in range(negative):
        out[f"C{900 + n:03d}"] = case(f"C{900 + n:03d}", "negative_control", f"GN{n}")
        n += 1
    return out


class SplitAssignmentTests(unittest.TestCase):
    def setUp(self) -> None:
        self.cases = minimal_corpus(Path(tempfile.mkdtemp()))

    def test_deterministic_given_same_salt(self) -> None:
        self.assertEqual(je.assign_splits(self.cases, "salt-A"), je.assign_splits(self.cases, "salt-A"))

    def test_different_salt_can_differ(self) -> None:
        # Not guaranteed to differ for every pair, but near-certain for these.
        assignments = {json.dumps(je.assign_splits(self.cases, s), sort_keys=True) for s in ("s1", "s2", "s3", "s4")}
        self.assertGreater(len(assignments), 1)

    def test_every_case_assigned(self) -> None:
        a = je.assign_splits(self.cases, "salt-A")
        self.assertEqual(sorted(a), sorted(self.cases))
        self.assertTrue(set(a.values()) <= set(je.PHASES))

    def test_min_strata_met(self) -> None:
        a = je.assign_splits(self.cases, "salt-A")
        risk = {cid: c["risk_class"] for cid, c in self.cases.items()}
        for phase, minimums in (("heldout", je.MIN_HELDOUT), ("confirmation", je.MIN_CONFIRMATION)):
            for r, m in minimums.items():
                have = sum(1 for cid, ph in a.items() if ph == phase and risk[cid] == r)
                self.assertGreaterEqual(have, m, f"{phase}/{r}")

    def test_too_small_corpus_raises(self) -> None:
        one = {cid: self.cases[cid] for cid in list(self.cases)[:2]}
        with self.assertRaises(je.ExperimentError):
            je.assign_splits(one, "salt-A")

    def test_real_corpus_assigns_with_frozen_salt(self) -> None:
        cases, f = corpus_validate.load_cases(REAL_CASES)
        self.assertEqual(f.items, [])
        a = je.assign_splits(cases, "jev-plan-frozen-salt-2026-09-20")
        risk = {cid: c["risk_class"] for cid, c in cases.items()}
        heldout_crit = sum(1 for cid, ph in a.items() if ph == "heldout" and risk[cid] == "critical_positive")
        self.assertGreaterEqual(heldout_crit, je.MIN_HELDOUT["critical_positive"])


class ManifestTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())

    def test_init_writes_valid_manifest(self) -> None:
        out = self.tmp / "experiment.json"
        manifest = je.build_manifest("salt-A", REAL_CASES, out)
        self.assertEqual(manifest["schema"], je.SCHEMA)
        self.assertFalse(manifest["budget"]["authorized"])  # locked by default
        je.validate_manifest(manifest, je._load_cases(REAL_CASES))  # must not raise

    def test_validate_rejects_contaminated_group(self) -> None:
        manifest = je.build_manifest("salt-A", REAL_CASES, self.tmp / "m.json")
        # Pick two cases from one family group and force them apart.
        groups: dict[str, str] = {}
        for cid, c in je._load_cases(REAL_CASES).items():
            groups.setdefault(c["family_group"], cid)
        pair_group = next(g for g in groups if g != groups[groups and list(groups)[0]] and sum(
            1 for c in je._load_cases(REAL_CASES).values() if c["family_group"] == g) > 1)
        members = [cid for cid, c in je._load_cases(REAL_CASES).items() if c["family_group"] == pair_group]
        manifest["splits"][members[0]] = "calibration"
        manifest["splits"][members[1]] = "heldout"
        with self.assertRaises(je.ExperimentError) as cm:
            je.validate_manifest(manifest, je._load_cases(REAL_CASES))
        self.assertIn("contaminated", str(cm.exception))

    def test_validate_rejects_missing_salt(self) -> None:
        manifest = je.build_manifest("salt-A", REAL_CASES, self.tmp / "m.json")
        manifest["frozen_salt"] = ""
        with self.assertRaises(je.ExperimentError) as cm:
            je.validate_manifest(manifest, je._load_cases(REAL_CASES))
        self.assertIn("frozen_salt", str(cm.exception))

    def test_run_refuses_unauthorized_budget(self) -> None:
        rc = je.main(["run", "--manifest", str(self.tmp / "m.json")]) if (self.tmp / "m.json").exists() else None
        manifest = je.build_manifest("salt-A", REAL_CASES, self.tmp / "m.json")
        self.assertFalse(manifest["budget"]["authorized"])
        rc = je.main(["run", "--manifest", str(self.tmp / "m.json")])
        self.assertEqual(rc, 1)  # locked run fails closed


class DryRunTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.manifest_path = self.tmp / "experiment.json"
        je.build_manifest("salt-A", REAL_CASES, self.manifest_path)

    def test_dry_run_makes_zero_calls_and_names_credentials(self) -> None:
        out = self.tmp / "plan.json"
        rc = je.main(["dry-run", "--manifest", str(self.manifest_path), "--phase", "calibration",
                      "--out", str(out)])
        self.assertEqual(rc, 0)
        plan = json.loads(out.read_text())
        self.assertEqual(plan["ai_calls"], 0)
        self.assertEqual(plan["github_writes"], 0)
        self.assertIn("TYPESAFE_API_KEY", plan["credentials_required"])
        self.assertGreater(len(plan["runs"]), 0)  # the doc carries the run list
        self.assertIn("proxy", plan["estimate"]["method"])  # labelled as proxy

    def test_dry_run_fails_over_cap(self) -> None:
        manifest = json.loads(self.manifest_path.read_text())
        manifest["budget"]["campaign_cap"] = 0.000001
        locked = self.tmp / "tight.json"
        locked.write_text(json.dumps(manifest))
        rc = je.main(["dry-run", "--manifest", str(locked), "--phase", "calibration"])
        self.assertEqual(rc, 1)

    def test_dry_run_phases_partition(self) -> None:
        seen: set[str] = set()
        for phase in je.PHASES:
            out = self.tmp / f"plan-{phase}.json"
            self.assertEqual(je.main(["dry-run", "--manifest", str(self.manifest_path),
                                      "--phase", phase, "--out", str(out)]), 0)
            plan = json.loads(out.read_text())
            for run in plan["runs"]:
                self.assertEqual(run["phase"], phase)
                seen.add(run["case_id"])
        cases = {c["id"]: c for c in (json.loads(p.read_text()) for p in sorted(REAL_CASES.glob("C*.json")))}
        self.assertEqual(seen, set(cases))


class DeterministicTriageTests(unittest.TestCase):
    def test_dependency_change_always_full(self) -> None:
        case = {"fixture": {"base": {"package.json": "{}"}, "head": {"package.json": "{...}"}}}
        self.assertEqual(je.deterministic_triage(case)["route"], "full")

    def test_policy_file_always_full(self) -> None:
        case = {"fixture": {"base": {}, "head": {"prompts/review-policy.md": "# p"}}}
        self.assertEqual(je.deterministic_triage(case)["route"], "full")

    def test_docs_only_shallow(self) -> None:
        case = {"fixture": {"base": {"README.md": "a"}, "head": {"README.md": "b"}}}
        self.assertEqual(je.deterministic_triage(case)["route"], "shallow")

    def test_code_full(self) -> None:
        case = {"fixture": {"base": {"a.py": "x=1"}, "head": {"a.py": "x=2"}}}
        self.assertEqual(je.deterministic_triage(case)["route"], "full")


class ReportTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.manifest_path = self.tmp / "m.json"
        je.build_manifest("salt-A", REAL_CASES, self.manifest_path)
        self.runs = self.tmp / "runs"
        self.runs.mkdir()

    def write_run(self, name: str, **fields: object) -> None:
        (self.runs / name).write_text(json.dumps(fields))

    def test_report_fails_without_records(self) -> None:
        self.assertEqual(je.main(["report", "--manifest", str(self.manifest_path),
                                  "--runs", str(self.runs)]), 1)

    def test_report_counts_failures_and_unknown_cost(self) -> None:
        self.write_run("run_1.json", arm="baseline", lane="grok", status="completed",
                       usage_unknown=False, provider_seconds=10.0, jev_seconds=0.0,
                       setup_seconds=2.0, must_find_hits=1, must_find_total=2)
        self.write_run("run_2.json", arm="jev", lane="grok", status="failed",
                       usage_unknown=True, provider_seconds=1.0, jev_seconds=0.5,
                       setup_seconds=1.0, must_find_hits=0, must_find_total=2)
        rc = je.main(["report", "--manifest", str(self.manifest_path), "--runs", str(self.runs)])
        self.assertEqual(rc, 0)
        # unknown cost on the jev arm must appear; a promotion claim would fail
        # (assert via the printed JSON by re-running capture)
        import contextlib, io
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            je.main(["report", "--manifest", str(self.manifest_path), "--runs", str(self.runs)])
        payload = json.loads(buf.getvalue())
        self.assertEqual(payload["per_arm"]["jev"]["unknown_cost"], 1)
        self.assertEqual(payload["per_arm"]["jev"]["failures"], 1)
        self.assertFalse(payload["promotion_ready"])

    def test_report_promotion_ready_when_complete(self) -> None:
        manifest = json.loads(self.manifest_path.read_text())
        manifest["report_requirements"] = {"cells": [["baseline", "grok"]]}
        self.manifest_path.write_text(json.dumps(manifest))
        self.write_run("run_1.json", arm="baseline", lane="grok", status="completed",
                       usage_unknown=False, provider_seconds=10.0, jev_seconds=0.0,
                       setup_seconds=2.0, must_find_hits=2, must_find_total=2)
        import contextlib, io
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = je.main(["report", "--manifest", str(self.manifest_path), "--runs", str(self.runs)])
        payload = json.loads(buf.getvalue())
        self.assertTrue(payload["promotion_ready"])


if __name__ == "__main__":
    unittest.main()
