#!/usr/bin/env python3
"""Tests for the v2 evaluation corpus (contract F2/F3/F7).

Integration test: the real corpus under tests/eval/cases must validate clean
and meet the declared floors. Mutation tests: the validator must reject the
failure modes the contract forbids (unpinned fixtures, duplicate identities,
unadjudicated critical labels, secret-looking fixtures, mislabelled negatives,
label paths outside the fixture).

Run: python3 -m unittest discover -s tests -p 'test_eval_corpus*.py' -v
"""

from __future__ import annotations

import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

TESTS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(TESTS_DIR / "eval"))

import corpus_validate  # noqa: E402


def make_case(case_id: str = "C901", risk: str = "warning_positive") -> dict:
    """A minimal valid trees case (used as the base for mutation tests)."""
    return {
        "schema": corpus_validate.SCHEMA,
        "id": case_id,
        "title": "Synthetic validator-fixture case",
        "stack": "python",
        "change_class": "logic_bug",
        "risk_class": risk,
        "family_group": "G90",
        "fixture": {
            "kind": "trees",
            "base": {"svc.py": "def run(x):\n    return x + 1\n"},
            "head": {"svc.py": "def run(x):\n    return x + 2\n"},
            "revision_pin": "PIN",
            "pr_metadata": {"title": "Off-by-one in run()", "body": "Adjusts the increment."},
        },
        "labels": [
            {
                "id": f"{case_id}-d1",
                "severity": "warning" if risk != "critical_positive" else "critical",
                "path": "svc.py",
                "defect": "run() increments by 2 instead of 1",
                "evidence": "run(1) returns 3; caller expects 2 (documented contract)",
                "introduced_by_change": True,
                "ambiguous": False,
                "keywords": ["x +", "increment"],
                "window": 25,
            }
        ],
        "expected": {"must_flag": [f"{case_id}-d1"], "must_not_flag": []},
        "adjudication": {"status": "pending"},
        "rights": {
            "origin": "original synthetic fixture authored for this corpus",
            "egress": "synthetic; no real secrets, hosts, or user data",
        },
    }


def pin(case: dict) -> dict:
    case["fixture"]["revision_pin"] = "fixture:sha256:" + corpus_validate.canonical_hash(
        {"base": case["fixture"]["base"], "head": case["fixture"]["head"]}
    )
    return case


class ValidatorMutationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="corpus-val-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def write(self, case: dict, name: str = "C901.json") -> Path:
        path = self.tmp / name
        path.write_text(json.dumps(case, indent=2), encoding="utf-8")
        return path

    def validate_one(self, case: dict) -> list[str]:
        # Write the case EXACTLY as given — tests that need a valid pin call
        # pin() themselves; re-pinning here would hide pin-mutation bugs.
        self.write(case)
        f = corpus_validate.Findings()
        cases, f2 = corpus_validate.load_cases(self.tmp)
        f.items.extend(f2.items)
        for cid, c in cases.items():
            corpus_validate.validate_case(c, cid, f)
        return f.items

    def test_minimal_valid_case_passes(self) -> None:
        items = self.validate_one(pin(make_case()))
        self.assertEqual(items, [])

    def test_rejects_wrong_revision_pin(self) -> None:
        case = pin(make_case())
        case["fixture"]["revision_pin"] = "fixture:sha256:deadbeef"
        items = self.validate_one(case)
        self.assertTrue(any("revision_pin mismatch" in i for i in items), items)

    def test_rejects_duplicate_case_id(self) -> None:
        good = pin(make_case())
        self.write(good, "C901.json")
        dupe = pin(make_case("C901"))
        dupe["title"] = "A different synthetic case"
        self.write(dupe, "C901b.json")
        _, f = corpus_validate.load_cases(self.tmp)
        self.assertTrue(any("duplicate case id" in i for i in f.items), f.items)

    def test_rejects_critical_without_adjudication(self) -> None:
        items = self.validate_one(pin(make_case(risk="critical_positive")))
        self.assertTrue(any("requires adjudicated status" in i for i in items), items)

    def test_rejects_unblinded_adjudication(self) -> None:
        case = pin(make_case(risk="critical_positive"))
        case["adjudication"] = {
            "status": "adjudicated",
            "adjudicator": corpus_validate.ADJUDICATOR,
            "blind": False,
            "verdict": "confirmed",
        }
        items = self.validate_one(case)
        self.assertTrue(any("must be blind" in i for i in items), items)

    def test_rejects_negative_control_with_labels(self) -> None:
        case = pin(make_case(risk="negative_control"))
        items = self.validate_one(case)
        self.assertTrue(any("negative_control must carry no labels" in i for i in items), items)

    def test_rejects_secret_marker_in_fixture(self) -> None:
        case = pin(make_case())
        case["fixture"]["head"]["svc.py"] = 'KEY = "sk-ant-abc123"\n'
        items = self.validate_one(case)
        self.assertTrue(any("secret marker" in i for i in items), items)

    def test_rejects_label_path_outside_fixture(self) -> None:
        case = pin(make_case())
        case["labels"][0]["path"] = "elsewhere.py"
        items = self.validate_one(case)
        self.assertTrue(any("not in head or base trees" in i for i in items), items)

    def test_rejects_unknown_must_flag_reference(self) -> None:
        case = pin(make_case())
        case["expected"]["must_flag"] = ["C901-nope"]
        items = self.validate_one(case)
        self.assertTrue(any("unknown label" in i for i in items), items)

    def test_negative_control_requires_reasoned_reference_review(self) -> None:
        case = make_case(risk="negative_control")
        case["labels"] = []
        case["expected"] = {"must_flag": [], "must_not_flag": [], "reference_review": "too short"}
        items = self.validate_one(case)
        self.assertTrue(any("reference_review" in i for i in items), items)

    def test_historical_pr_case_passes(self) -> None:
        case = make_case("C902")
        case["fixture"] = {
            "kind": "historical_pr",
            "repo": "DailybotHQ/ai-diff-reviewer",
            "pr_number": 46,
            "merge_commit": "a" * 40,
            "pr_metadata": {"title": "Permission-aware author gate (#46)"},
        }
        case["labels"][0]["path"] = "scripts/reviewer.py"
        items = self.validate_one(case)
        self.assertEqual(items, [])


class RealCorpusIntegrationTest(unittest.TestCase):
    """The gate: the shipped corpus must validate clean and meet floors."""

    CASES_DIR = TESTS_DIR / "eval" / "cases"

    def test_corpus_validates_and_meets_floors(self) -> None:
        cases, f = corpus_validate.load_cases(self.CASES_DIR)
        self.assertEqual(f.items, [], f"loader findings: {f.items}")
        for cid, case in sorted(cases.items()):
            corpus_validate.validate_case(case, cid, f)
        self.assertEqual(f.items, [], f"validation findings:\n" + "\n".join(f.items))
        stats = corpus_validate.corpus_checks(cases, corpus_validate.Findings())
        # Floors are re-asserted here so a floor change must touch two places.
        self.assertGreaterEqual(stats["cases"], corpus_validate.MIN_CASES)
        self.assertGreaterEqual(len(stats["stacks"]), corpus_validate.MIN_STACKS)
        self.assertGreaterEqual(stats["critical_positive"], corpus_validate.MIN_CRITICAL)
        self.assertGreaterEqual(stats["negative_control"], corpus_validate.MIN_NEGATIVE)

    def test_group_map_covers_every_case(self) -> None:
        cases, f = corpus_validate.load_cases(self.CASES_DIR)
        self.assertEqual(f.items, [])
        gmap = corpus_validate.group_map(cases)
        self.assertEqual(sorted(gmap), sorted(cases))
        for cid, group in gmap.items():
            self.assertTrue(group.startswith("G"), f"{cid}: group {group!r}")


if __name__ == "__main__":
    unittest.main()
