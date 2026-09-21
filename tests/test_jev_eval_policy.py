#!/usr/bin/env python3
"""Policy unit tests (Task 6): uncertain/missing/contradictory inputs,
batched mapping, vetoes, evidence classes, hash stability. Offline only.

Run: python3 -m unittest discover -s tests -p 'test_jev_eval*.py' -v
"""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

TESTS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(TESTS_DIR / "eval"))

import policy as pol  # noqa: E402


def decisions(**kw: dict) -> dict:
    return kw


def value(v, conf: float) -> dict:
    return {"decision": "value", "value": v, "confidence": conf}


class PolicyBundleTests(unittest.TestCase):
    def test_shipped_bundle_is_the_calibrated_artifact(self) -> None:
        shipped = pol.load_policy()
        # The shipped bundle is NOT the factory default: it carries the
        # Task 6 calibration (floor selected by the predeclared sweep;
        # see analysis_results/CALIBRATION.md). Pin the calibrated value so
        # a re-calibration must consciously update this test.
        self.assertEqual(shipped["version"], "v1")
        self.assertEqual(shipped["thresholds"]["risk_confidence_floor"], 0.60)
        self.assertIn("calibration", shipped)
        h1 = pol.policy_hash(shipped)
        h2 = pol.policy_hash(pol.load_policy())
        self.assertEqual(h1, h2)
        self.assertTrue(h1.startswith("policy:sha256:"))

    def test_hash_changes_when_any_threshold_changes(self) -> None:
        p = pol.default_policy()
        base = pol.policy_hash(p)
        p["thresholds"]["risk_confidence_floor"] = 0.71
        self.assertNotEqual(base, pol.policy_hash(p))

    def test_fingerprint_file_matches_bundle(self) -> None:
        self.assertEqual(pol.policy_fingerprint_file(), pol.policy_hash(pol.load_policy()))


class MapBatchTests(unittest.TestCase):
    def setUp(self) -> None:
        self.p = pol.load_policy()
        self.q = {"risk": {"type": "choice"}}

    def test_missing_answer_is_insufficient(self) -> None:
        d = pol.map_batch(self.p, self.q, {})
        self.assertEqual(d["risk"]["decision"], "insufficient_evidence")
        self.assertEqual(d["risk"]["reason"], "missing_answer")

    def test_none_answer_is_insufficient(self) -> None:
        d = pol.map_batch(self.p, self.q, {"risk": None})
        self.assertEqual(d["risk"]["decision"], "insufficient_evidence")

    def test_missing_confidence_is_insufficient(self) -> None:
        d = pol.map_batch(self.p, self.q, {"risk": {"value": "critical"}})
        self.assertEqual(d["risk"]["reason"], "missing_confidence")

    def test_below_floor_abstains(self) -> None:
        d = pol.map_batch(self.p, self.q, {"risk": {"value": "critical", "confidence": 0.46}})
        self.assertEqual(d["risk"]["decision"], "insufficient_evidence")
        self.assertEqual(d["risk"]["reason"], "below_floor")

    def test_at_floor_commits(self) -> None:
        d = pol.map_batch(self.p, self.q, {"risk": {"value": "critical", "confidence": 0.70}})
        self.assertEqual(d["risk"]["decision"], "value")

    def test_contradiction_detected(self) -> None:
        answers = {
            "risk": {"value": "none", "confidence": 0.95},
            "touches_security": {"value": True, "confidence": 0.9},
        }
        d = pol.map_batch(self.p, {"risk": {}, "touches_security": {}}, answers)
        self.assertEqual(d["risk"]["decision"], "contradiction")
        self.assertEqual(d["touches_security"]["decision"], "contradiction")

    def test_no_contradiction_when_security_low(self) -> None:
        answers = {
            "risk": {"value": "none", "confidence": 0.95},
            "touches_security": {"value": False, "confidence": 0.9},
        }
        d = pol.map_batch(self.p, {"risk": {}, "touches_security": {}}, answers)
        self.assertEqual(d["risk"]["decision"], "value")

    def test_batched_mapping_over_many_questions(self) -> None:
        q = {f"q{i}": {} for i in range(50)}
        answers = {f"q{i}": {"value": "x", "confidence": 0.9} for i in range(0, 50, 2)}
        d = pol.map_batch(self.p, q, answers)
        self.assertEqual(len(d), 50)
        self.assertEqual(sum(1 for v in d.values() if v["decision"] == "value"), 25)
        self.assertEqual(sum(1 for v in d.values() if v["decision"] == "insufficient_evidence"), 25)


class TriageRouteTests(unittest.TestCase):
    def setUp(self) -> None:
        self.p = pol.load_policy()

    def test_veto_forces_full_review(self) -> None:
        d = decisions(risk=value("none", 0.99))
        route = pol.triage_route(self.p, d, {"dependency_manifest": True})
        self.assertEqual(route["route"], "full")
        self.assertIn("veto", route["reason"])

    def test_inventory_incomplete_vetoes(self) -> None:
        d = decisions(risk=value("none", 0.99))
        route = pol.triage_route(self.p, d, {"inventory_incomplete": True})
        self.assertEqual(route["route"], "full")

    def test_uncommitted_triage_is_full(self) -> None:
        d = decisions(risk={"decision": "insufficient_evidence", "reason": "below_floor"})
        self.assertEqual(pol.triage_route(self.p, d)["route"], "full")

    def test_clean_triage_is_shallow(self) -> None:
        d = decisions(risk=value("none", 0.98), touches_security=value(False, 0.9))
        self.assertEqual(pol.triage_route(self.p, d)["route"], "shallow")

    def test_flagged_risk_is_full(self) -> None:
        d = decisions(risk=value("critical", 0.95))
        self.assertEqual(pol.triage_route(self.p, d)["route"], "full")


class FastPassSimulationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.p = pol.load_policy()

    def clean(self, conf: float = 0.95) -> dict:
        return decisions(risk=value("none", conf), touches_security=value(False, 0.99))

    def test_clean_above_floor_eligible(self) -> None:
        eligible, reasons = pol.fast_pass_eligible(self.p, self.clean())
        self.assertTrue(eligible, reasons)

    def test_veto_blocks(self) -> None:
        eligible, reasons = pol.fast_pass_eligible(self.p, self.clean(), {"policy_prompt_file": True})
        self.assertFalse(eligible)
        self.assertTrue(any("veto" in r for r in reasons))

    def test_security_touch_blocks(self) -> None:
        d = decisions(risk=value("none", 0.95), touches_security=value(True, 0.9))
        eligible, _ = pol.fast_pass_eligible(self.p, d)
        self.assertFalse(eligible)

    def test_below_fast_pass_floor_blocks(self) -> None:
        eligible, reasons = pol.fast_pass_eligible(self.p, self.clean(conf=0.8))
        self.assertFalse(eligible)
        self.assertTrue(any("floor" in r for r in reasons))

    def test_uncommitted_blocks(self) -> None:
        d = decisions(risk={"decision": "insufficient_evidence"}, touches_security=value(False, 0.9))
        self.assertFalse(pol.fast_pass_eligible(self.p, d)[0])


class PriorityOrderTests(unittest.TestCase):
    def test_severity_descends_with_stable_ties(self) -> None:
        items = [
            {"case": "a", "risk": "warning", "confidence": 0.9, "touches_security": False},
            {"case": "b", "risk": "critical", "confidence": 0.8, "touches_security": False},
            {"case": "c", "risk": "warning", "confidence": 0.95, "touches_security": False},
            {"case": "d", "risk": "insufficient_evidence", "confidence": 0.5, "touches_security": False},
        ]
        order = [i["case"] for i in pol.priority_order(items)]
        self.assertEqual(order, ["b", "c", "a", "d"])

    def test_security_flag_boosts_within_severity(self) -> None:
        items = [
            {"case": "a", "risk": "warning", "confidence": 0.9, "touches_security": False},
            {"case": "b", "risk": "warning", "confidence": 0.9, "touches_security": True},
        ]
        self.assertEqual([i["case"] for i in pol.priority_order(items)], ["b", "a"])

    def test_ordering_never_drops_items(self) -> None:
        items = [{"case": str(i), "risk": "none", "confidence": 0.9, "touches_security": False} for i in range(20)]
        self.assertEqual(len(pol.priority_order(items)), 20)


class EvidenceClassificationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.p = pol.load_policy()

    def test_critical_always_withheld(self) -> None:
        v = {"is_real": 0.01, "severity": "invalid"}
        c = pol.classify_evidence(self.p, v, "critical")
        self.assertEqual(c["class"], "withheld_critical")
        self.assertFalse(c["suppressable"])

    def test_supported_when_confident_real(self) -> None:
        c = pol.classify_evidence(self.p, {"is_real": 0.9, "severity": "warning"}, "warning")
        self.assertEqual(c["class"], "supported")
        self.assertFalse(c["suppressable"])

    def test_contradicted_only_with_affirmative_contradiction(self) -> None:
        c = pol.classify_evidence(self.p, {"is_real": 0.05, "severity": "invalid"}, "warning")
        self.assertEqual(c["class"], "contradicted")
        self.assertTrue(c["suppressable"])

    def test_low_confidence_disagreement_is_insufficient(self) -> None:
        c = pol.classify_evidence(self.p, {"is_real": 0.45, "severity": "warning"}, "warning")
        self.assertEqual(c["class"], "insufficient")

    def test_missing_verification_is_insufficient(self) -> None:
        self.assertEqual(pol.classify_evidence(self.p, None, "warning")["class"], "insufficient")

    def test_missing_fields_are_insufficient(self) -> None:
        c = pol.classify_evidence(self.p, {"is_real": 0.9}, "warning")
        self.assertEqual(c["class"], "insufficient")


if __name__ == "__main__":
    unittest.main()
