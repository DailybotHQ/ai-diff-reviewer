#!/usr/bin/env python3
"""Validator for the v2 evaluation corpus (tests/eval/cases/*.json).

The corpus backs the Jev comparison experiments (PLAN_jev_review_acceleration,
experiment contract F2/F3/F7). It is deliberately strict: every rule here
exists because the contract requires it — pinned immutable fixtures, grounded
labels, blinded adjudication of release-critical labels, grouping before
splitting, declared floors, and no secret-looking material in fixtures.

Usage:
    python3 tests/eval/corpus_validate.py [--cases-dir tests/eval/cases] [--json]

Exit 0 = corpus valid. Any finding prints `FAIL: ...` and exit 1.
"""

from __future__ import annotations

import hashlib
import json
import re
import sys
from pathlib import Path
from typing import Any

SCHEMA = "ai-diff-reviewer/eval-case/1"
STACKS = {"python", "typescript", "go"}
RISK_CLASSES = {"critical_positive", "warning_positive", "negative_control"}
SEVERITIES = {"critical", "warning"}
ADJUDICATOR = "independent-reviewer-v1"

# Substrings that must never appear in fixture content: the corpus is synthetic
# and must stay that way (contract: no secrets, no real credentials).
SECRET_MARKERS = (
    "-----BEGIN",
    "AKIA",
    "ghp_",
    "gho_",
    "github_pat_",
    "sk-ant-",
    "sk-proj-",
    "xoxb-",
    "xoxp-",
)

MIN_CASES = 60
MIN_STACKS = 3
MIN_CRITICAL = 20
MIN_NEGATIVE = 20


class Findings:
    def __init__(self) -> None:
        self.items: list[str] = []

    def fail(self, msg: str) -> None:
        self.items.append(f"FAIL: {msg}")

    def ok(self) -> bool:
        return not self.items


def canonical_hash(payload: Any) -> str:
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def load_cases(cases_dir: Path) -> tuple[dict[str, dict[str, Any]], Findings]:
    f = Findings()
    cases: dict[str, dict[str, Any]] = {}
    if not cases_dir.is_dir():
        f.fail(f"cases dir missing: {cases_dir}")
        return cases, f
    files = sorted(cases_dir.glob("C*.json"))
    if not files:
        f.fail(f"no case files in {cases_dir}")
    for path in files:
        try:
            case = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            f.fail(f"{path.name}: invalid JSON: {exc}")
            continue
        case_id = case.get("id", "")
        if case_id in cases:
            f.fail(f"{path.name}: duplicate case id {case_id!r}")
        cases[case_id or path.stem] = case
    return cases, f


def validate_case(case: dict[str, Any], name: str, f: Findings) -> None:
    def bad(msg: str) -> None:
        f.fail(f"{name}: {msg}")

    if case.get("schema") != SCHEMA:
        bad(f"schema must be {SCHEMA!r}")
    cid = case.get("id", "")
    if not re.fullmatch(r"C\d{3}", cid):
        bad(f"id {cid!r} must match Cddd")
    title = case.get("title", "")
    if not isinstance(title, str) or len(title) < 8:
        bad("title missing or too short")
    if case.get("stack") not in STACKS:
        bad(f"stack {case.get('stack')!r} not in {sorted(STACKS)}")
    risk = case.get("risk_class")
    if risk not in RISK_CLASSES:
        bad(f"risk_class {risk!r} invalid")
    if not case.get("change_class"):
        bad("change_class missing")
    if not case.get("family_group", "").startswith("G"):
        bad("family_group must start with G (grouping before splitting, F2)")

    # Secret-marker sweep across the WHOLE case document — fixture trees,
    # `pr_metadata`, label evidence, inventory, every string — so a copied
    # token cannot pass validation by hiding outside the fixture trees.
    def _walk_for_secrets(node: Any, path: str) -> None:
        if isinstance(node, str):
            for marker in SECRET_MARKERS:
                if marker in node:
                    bad(f"{path} contains secret marker {marker!r}")
        elif isinstance(node, dict):
            for key, value in node.items():
                _walk_for_secrets(value, f"{path}.{key}")
        elif isinstance(node, list):
            for i, value in enumerate(node):
                _walk_for_secrets(value, f"{path}[{i}]")

    _walk_for_secrets(case, "case")

    fixture = case.get("fixture") or {}
    kind = fixture.get("kind", "trees")
    base: dict[str, Any] = {}
    head: dict[str, Any] = {}
    if kind == "historical_pr":
        # Real merged PR carried over from the legacy corpus: pinned by merge
        # commit, labels carried from the labelled expectations. No trees.
        if not fixture.get("repo") or not isinstance(fixture.get("pr_number"), int):
            bad("historical_pr fixture requires repo and pr_number")
        commit = fixture.get("merge_commit", "")
        if not re.fullmatch(r"[0-9a-f]{40}", commit):
            bad("historical_pr fixture requires a 40-hex merge_commit pin")
    else:
        base, head = fixture.get("base"), fixture.get("head")
        if not isinstance(base, dict) or not isinstance(head, dict):
            bad("fixture.base/fixture.head must be objects of path -> content")
            return
        if not base and not head:
            bad("fixture has no files")
        missing: list[str] = list(
            ((fixture.get("inventory") or {}).get("missing_from_fixture")) or []
        )
        for tree_name, tree in (("base", base), ("head", head)):
            for p, content in tree.items():
                if not isinstance(content, str):
                    bad(f"fixture.{tree_name}[{p!r}] content must be a string")
        pin = fixture.get("revision_pin", "")
        expected_pin = f"fixture:sha256:{canonical_hash({'base': base, 'head': head})}"
        if pin != expected_pin:
            bad(f"revision_pin mismatch (want {expected_pin[:40]}...)")
        if missing and not all(isinstance(m, str) for m in missing):
            bad("inventory.missing_from_fixture must be a list of paths")
    meta = fixture.get("pr_metadata") or {}
    if not isinstance(meta, dict) or not meta.get("title"):
        bad("fixture.pr_metadata.title missing")
    if fixture.get("deceptive") and meta.get("deceptive") is not True:
        bad("fixture.deceptive true but pr_metadata.deceptive not declared")

    labels = case.get("labels")
    if not isinstance(labels, list):
        bad("labels must be a list")
        return
    if risk == "negative_control" and labels:
        bad("negative_control must carry no labels")
    if risk in {"critical_positive", "warning_positive"} and not labels:
        bad(f"{risk} requires at least one label")
    seen_label_ids: set[str] = set()
    for lbl in labels:
        if not isinstance(lbl, dict):
            bad("label entries must be objects")
            continue
        lid = lbl.get("id", "")
        if lid in seen_label_ids:
            bad(f"duplicate label id {lid!r}")
        seen_label_ids.add(lid)
        if lbl.get("severity") not in SEVERITIES:
            bad(f"label {lid!r}: severity must be one of {sorted(SEVERITIES)}")
        if risk == "critical_positive" and lbl.get("severity") != "critical":
            bad(f"label {lid!r}: critical_positive labels must be critical")
        path = lbl.get("path", "")
        missing_ok = path in (((case.get("fixture") or {}).get("inventory") or {}).get("missing_from_fixture") or [])
        if kind != "historical_pr" and path not in head and path not in base and not missing_ok:
            bad(f"label {lid!r}: path {path!r} not in head or base trees")
        if not lbl.get("defect") or not lbl.get("evidence"):
            bad(f"label {lid!r}: defect and evidence are required")
        if lbl.get("introduced_by_change") is not True:
            bad(f"label {lid!r}: every corpus label must be introduced by the change")
        if not isinstance(lbl.get("keywords"), list) or not lbl.get("keywords"):
            bad(f"label {lid!r}: keywords (scorer window match) required")
        if not isinstance(lbl.get("window", 25), int) or lbl.get("window", 25) <= 0:
            bad(f"label {lid!r}: window must be a positive int")
        if lbl.get("severity") == "critical":
            adj = case.get("adjudication") or {}
            if adj.get("status") != "adjudicated":
                bad(f"label {lid!r}: critical label requires adjudicated status (F7)")
            elif adj.get("adjudicator") != ADJUDICATOR:
                bad(f"label {lid!r}: adjudicator must be {ADJUDICATOR!r}")
            elif adj.get("blind") is not True:
                bad(f"label {lid!r}: adjudication must be blind (F7)")
            elif adj.get("verdict") not in {"confirmed", "confirmed_with_note"}:
                bad(f"label {lid!r}: adjudication verdict must confirm the defect")

    expected = case.get("expected") or {}
    if risk != "negative_control":
        must = expected.get("must_flag")
        if not isinstance(must, list) or not must:
            bad("expected.must_flag must list label ids")
        else:
            for lid in must:
                if lid not in seen_label_ids:
                    bad(f"expected.must_flag references unknown label {lid!r}")
    else:
        ref = expected.get("reference_review", "")
        if not isinstance(ref, str) or len(ref) < 40:
            bad("negative_control requires a reasoned reference_review (>=40 chars)")
    must_not = expected.get("must_not_flag", [])
    if not isinstance(must_not, list):
        bad("expected.must_not_flag must be a list")

    adj = case.get("adjudication") or {}
    if not isinstance(adj, dict) or not adj.get("status"):
        bad("adjudication.status required (pending or adjudicated)")
    rights = case.get("rights") or {}
    if not rights.get("origin") or not rights.get("egress"):
        bad("rights.origin and rights.egress are required (source rights / data egress)")


def corpus_checks(cases: dict[str, dict[str, Any]], f: Findings) -> dict[str, Any]:
    stacks = {c.get("stack") for c in cases.values()}
    critical = [c for c in cases.values() if c.get("risk_class") == "critical_positive"]
    negative = [c for c in cases.values() if c.get("risk_class") == "negative_control"]
    if len(cases) < MIN_CASES:
        f.fail(f"corpus has {len(cases)} cases, floor is {MIN_CASES}")
    if len(stacks) < MIN_STACKS:
        f.fail(f"corpus spans {len(stacks)} stacks, floor is {MIN_STACKS}")
    if len(critical) < MIN_CRITICAL:
        f.fail(f"corpus has {len(critical)} critical_positive cases, floor is {MIN_CRITICAL}")
    if len(negative) < MIN_NEGATIVE:
        f.fail(f"corpus has {len(negative)} negative_control cases, floor is {MIN_NEGATIVE}")
    pending = sorted(c["id"] for c in cases.values() if (c.get("adjudication") or {}).get("status") == "pending")
    return {
        "cases": len(cases),
        "stacks": sorted(s for s in stacks if s),
        "critical_positive": len(critical),
        "warning_positive": sum(1 for c in cases.values() if c.get("risk_class") == "warning_positive"),
        "negative_control": len(negative),
        "adjudication_pending": pending,
    }


def group_map(cases: dict[str, dict[str, Any]]) -> dict[str, str]:
    """family_group per case id — the unit splits must never cut across (F2)."""
    return {cid: c.get("family_group", "") for cid, c in sorted(cases.items())}


def main(argv: list[str]) -> int:
    args = [a for a in argv[1:] if not a.startswith("--")]
    as_json = "--json" in argv
    cases_dir = Path(args[0]) if args else Path(__file__).parent / "cases"
    cases, f = load_cases(cases_dir)
    for cid, case in sorted(cases.items()):
        validate_case(case, cid or "case", f)
    stats = corpus_checks(cases, f)
    if as_json:
        print(json.dumps({"stats": stats, "findings": f.items}, indent=2))
    else:
        print(f"cases={stats['cases']} stacks={stats['stacks']} critical={stats['critical_positive']} "
              f"warning={stats['warning_positive']} negative={stats['negative_control']} "
              f"adjudication_pending={len(stats['adjudication_pending'])}")
        for item in f.items:
            print(item)
    return 0 if f.ok() else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
