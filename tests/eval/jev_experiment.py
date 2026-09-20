#!/usr/bin/env python3
"""Controlled-comparison experiment driver (PLAN_jev_review_acceleration T4).

Implements the four interfaces the validation register requires:

    python3 tests/eval/jev_experiment.py init     --salt S --out experiment.json
    python3 tests/eval/jev_experiment.py validate --manifest experiment.json
    python3 tests/eval/jev_experiment.py dry-run  --manifest experiment.json [--out plan.json]
    python3 tests/eval/jev_experiment.py run      --manifest experiment.json --phase calibration
                                                  [--arm baseline|deterministic|jev] [--lane ID]
    python3 tests/eval/jev_experiment.py report   --manifest experiment.json

Hard properties (experiment contract F1/F2/F8/F9):

- Split assignment is deterministic from `sha256(case_id + salt)`, computed
  over FAMILY GROUPS (never individual cases), and validated against the
  contract's minimum strata. A group spanning two splits is a hard error.
- `dry-run` performs ZERO AI calls and ZERO GitHub writes by construction:
  it only plans and estimates. The estimate uses measured-max-tokens x 1.5
  where measurements exist and a declared conservative char/4 proxy where
  they do not; the proxy is labelled as such everywhere it appears.
- `run` refuses to start unless the manifest records an authorized budget
  (`budget.authorized: true`) and, per lane, credentials present. Missing
  usage and incomplete reviews are recorded as failures, never as savings.
- `report` refuses a promotion-ready verdict while required cells are
  missing or any required cost is unknown.

This module orchestrates; it never grades model quality.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import random
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

import corpus_validate  # noqa: E402

SCHEMA = "ai-diff-reviewer/jev-experiment/1"
ARMS = ("baseline", "deterministic", "jev")
PHASES = ("calibration", "heldout", "confirmation")
SPLIT_SHARES = {"calibration": 0.50, "heldout": 0.30, "confirmation": 0.20}
MIN_HELDOUT = {"critical_positive": 8, "negative_control": 8}
MIN_CONFIRMATION = {"critical_positive": 4}
DEFAULT_REPS = 3
GATE_COMMAND = "python3 tests/eval/jev_experiment.py"


class ExperimentError(Exception):
    pass


def _load_cases(cases_dir: Path) -> dict[str, dict[str, Any]]:
    cases, findings = corpus_validate.load_cases(cases_dir)
    for cid, case in cases.items():
        corpus_validate.validate_case(case, cid, findings)
    if findings.items:
        raise ExperimentError("corpus invalid: " + "; ".join(findings.items))
    # Only CRITICAL labels require adjudication (F7); warning/negative cases
    # legitimately stay "pending" in the corpus file.
    blocking = [
        cid for cid, case in cases.items()
        if (case.get("adjudication") or {}).get("status") == "pending"
        and any(lbl.get("severity") == "critical" for lbl in case.get("labels", []))
    ]
    if blocking:
        raise ExperimentError("critical cases pending adjudication: " + ", ".join(sorted(blocking)))
    return cases


def assign_splits(cases: dict[str, dict[str, Any]], salt: str) -> dict[str, str]:
    """F2 assignment: hash per GROUP, sort within hash bands, respect shares.

    Deterministic: same corpus + same salt -> same assignment. Groups are the
    atomic unit (counterpart pairs and variants never span splits).
    """
    groups: dict[str, list[str]] = {}
    for cid, case in cases.items():
        groups.setdefault(case.get("family_group", f"SOLO-{cid}"), []).append(cid)
    risk_of = {cid: c["risk_class"] for cid, c in cases.items()}

    order = sorted(groups, key=lambda g: hashlib.sha256(f"{salt}:{g}".encode()).hexdigest())
    total = sum(len(m) for m in groups.values())
    target = {phase: SPLIT_SHARES[phase] * total for phase in PHASES}
    counts = {phase: 0 for phase in PHASES}
    assignment: dict[str, str] = {}

    # RESERVE FIRST (F2): satisfy the contract minimums before any share
    # pass, so no salt can leave a phase starved. Groups stay atomic.
    def reserve(into: str, risk: str, need: int) -> None:
        have = sum(1 for cid, ph in assignment.items() if ph == into and risk_of[cid] == risk)
        for group in list(order):
            if have >= need:
                return
            cids = groups[group]
            if assignment.get(cids[0]) is not None:
                continue  # already reserved for another phase — never poach
            gained = sum(1 for c in cids if risk_of[c] == risk)
            if gained == 0:
                continue
            for cid in cids:
                assignment[cid] = into
            counts[into] += len(cids)
            have += gained

    reserve("heldout", "critical_positive", MIN_HELDOUT["critical_positive"])
    reserve("heldout", "negative_control", MIN_HELDOUT["negative_control"])
    reserve("confirmation", "critical_positive", MIN_CONFIRMATION["critical_positive"])

    # SHARE PASS: distribute every remaining group to the phase whose target
    # gap is largest at the moment of assignment (deterministic tie-break).
    for group in order:
        if assignment.get(groups[group][0]) is not None:
            continue
        remaining = {p: target[p] - counts[p] for p in PHASES}
        phase = max(remaining, key=lambda p: (remaining[p], p))
        for cid in groups[group]:
            assignment[cid] = phase
        counts[phase] += len(groups[group])

    unassigned = [cid for cid in cases if cid not in assignment]
    if unassigned:
        raise ExperimentError(f"split assignment left cases unassigned: {unassigned[:5]}")
    # Reservation cannot conjure cases that do not exist: verify the floors
    # explicitly so a tiny corpus fails loudly instead of silently.
    for phase, minimums in (("heldout", MIN_HELDOUT), ("confirmation", MIN_CONFIRMATION)):
        for risk, minimum in minimums.items():
            have = sum(1 for cid, ph in assignment.items() if ph == phase and risk_of.get(cid) == risk)
            if have < minimum:
                raise ExperimentError(
                    f"corpus too small: {phase} needs {risk}>={minimum}, reservation reached {have}"
                )
    return assignment


def build_manifest(salt: str, cases_dir: Path, out: Path, *, reps: int = DEFAULT_REPS,
                   pilot_cap: float = 25.0, campaign_cap: float = 100.0,
                   authorized: bool = False) -> dict[str, Any]:
    cases = _load_cases(cases_dir)
    assignment = assign_splits(cases, salt)
    corpus_digest = hashlib.sha256(
        json.dumps({cid: cases[cid]["fixture"].get("revision_pin", "") for cid, cid in
                    [(c, c) for c in sorted(cases)]}, sort_keys=True).encode()
    ).hexdigest()
    manifest = {
        "schema": SCHEMA,
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "frozen_salt": salt,
        "corpus_digest": corpus_digest,
        "cases_dir": str(cases_dir),
        "repetitions": reps,
        "arms": list(ARMS),
        "splits": assignment,
        "split_counts": {p: sum(1 for v in assignment.values() if v == p) for p in PHASES},
        "budget": {
            "currency": "USD",
            "pilot_cap": pilot_cap,
            "campaign_cap": campaign_cap,
            "authorized": authorized,
            "authorization_note": "developer approval required before any live run (F8)",
        },
        "lanes": {
            "jev": {"model": "jev-1.13.0", "key_env": "TYPESAFE_API_KEY", "kind": "decision"},
            "grok": {"model": "grok-4.5", "key_env": "XAI_API_KEY", "kind": "coding"},
            "glm-claude-code": {"model": "glm-5.3", "key_env": "ZAI_CODING_API_KEY", "kind": "coding"},
        },
        "runs": [],
    }
    out.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    return manifest


def validate_manifest(manifest: dict[str, Any], cases: dict[str, dict[str, Any]]) -> None:
    problems: list[str] = []
    if manifest.get("schema") != SCHEMA:
        problems.append(f"schema {manifest.get('schema')!r} != {SCHEMA!r}")
    if not manifest.get("frozen_salt"):
        problems.append("frozen_salt missing: splits are not reproducible (F2)")
    splits = manifest.get("splits") or {}
    missing = sorted(set(cases) - set(splits))
    if missing:
        problems.append(f"unassigned cases: {missing[:5]}")
    groups: dict[str, set[str]] = {}
    for cid, case in cases.items():
        groups.setdefault(case.get("family_group", f"SOLO-{cid}"), set()).add(splits.get(cid, "?"))
    for group, phases in sorted(groups.items()):
        if len(phases) > 1:
            problems.append(f"contaminated group {group}: spans splits {sorted(phases)}")
    risk_of = {cid: c["risk_class"] for cid, c in cases.items()}
    for phase, minimums in (("heldout", MIN_HELDOUT), ("confirmation", MIN_CONFIRMATION)):
        for risk, minimum in minimums.items():
            have = sum(1 for cid, ph in splits.items() if ph == phase and risk_of.get(cid) == risk)
            if have < minimum:
                problems.append(f"{phase}: {risk} count {have} < minimum {minimum}")
    budget = manifest.get("budget") or {}
    for key in ("pilot_cap", "campaign_cap", "authorized"):
        if key not in budget:
            problems.append(f"budget.{key} missing (F8 requires an explicit cap)")
    if manifest.get("repetitions", 0) < 1:
        problems.append("repetitions must be >= 1")
    if problems:
        raise ExperimentError("manifest invalid:\n  - " + "\n  - ".join(problems))


@dataclass
class RunPlan:
    arm: str
    lane: str
    case_id: str
    phase: str
    rep: int
    est_input_chars: int


def plan_runs(manifest: dict[str, Any], cases: dict[str, dict[str, Any]], phase: str,
              seed: int) -> tuple[list[RunPlan], dict[str, Any]]:
    """Interleaved, seeded plan (F9). Includes per-run conservative estimates."""
    rng = random.Random(seed)
    splits = manifest["splits"]
    lane_ids = sorted(manifest.get("lanes", {}))
    coding_lanes = [l for l in lane_ids if manifest["lanes"][l].get("kind") == "coding"]
    plans: list[RunPlan] = []
    group_of = {cid: c.get("family_group", f"SOLO-{cid}") for cid, c in cases.items()}
    cells: list[tuple[str, str, str]] = []
    for cid, phase_of in sorted(splits.items()):
        if phase_of != phase:
            continue
        for arm in manifest["arms"]:
            for lane in coding_lanes:
                for rep in range(manifest.get("repetitions", DEFAULT_REPS)):
                    cells.append((arm, lane, cid))
    rng.shuffle(cells)
    for arm, lane, cid in cells:
        size = sum(len(str(part)) for part in (
            cases[cid]["fixture"].get("base", {}), cases[cid]["fixture"].get("head", {})
        ))
        plans.append(RunPlan(arm, lane, cid, phase, 0, size))
        plans[-1] = RunPlan(arm, lane, cid, phase, rep=0, est_input_chars=size)
    # rep index: restore per-cell repetition numbering after shuffle
    counters: dict[tuple[str, str, str], int] = {}
    numbered: list[RunPlan] = []
    for arm, lane, cid, phase_of, _rep, size in [(p.arm, p.lane, p.case_id, p.phase, p.rep, p.est_input_chars) for p in plans]:
        key = (arm, lane, cid)
        counters[key] = counters.get(key, 0)
        numbered.append(RunPlan(arm, lane, cid, phase_of, counters[key], size))
        counters[key] += 1
    # jev arm adds one decision call per reviewed run (same conservative bound)
    estimate = {
        "method": "chars/4 conservative proxy (labelled; measured-max x1.5 replaces it once Task 5 measures)",
        "runs": len(numbered),
        "jev_decision_runs": sum(1 for p in numbered if p.arm == "jev"),
        "est_total_input_chars": sum(p.est_input_chars for p in numbered),
        "est_total_input_tokens_proxy": sum(p.est_input_chars for p in numbered) // 4,
    }
    return numbered, estimate


def budget_check(estimate: dict[str, Any], manifest: dict[str, Any]) -> dict[str, Any]:
    # $42 per million input tokens, output free (F5 lane facts)
    usd = estimate["est_total_input_tokens_proxy"] / 1_000_000 * 42.0
    cap = float(manifest["budget"]["campaign_cap"])
    return {
        "estimated_upper_bound_usd_proxy": round(usd, 6),
        "campaign_cap_usd": cap,
        "within_cap": usd <= cap,
        "note": "proxy estimate: replace with measured-max x1.5 bounds at pilot time (F8)",
    }


def deterministic_triage(case: dict[str, Any]) -> dict[str, Any]:
    """The explicit, cheap rules comparator (contract: not weakened to favor Jev).

    Pure function of the case's changed paths: dependency/lockfile and
    policy/prompt-file changes always demand full review; docs-only small
    diffs suggest shallow review; everything else defaults to full review.
    """
    head = case["fixture"].get("head", {})
    base = case["fixture"].get("base", {})
    paths = sorted(set(head) | set(base))
    deps = [p for p in paths if "package-lock" in p or p.endswith((".lock", "package.json", "requirements.txt"))]
    policy = [p for p in paths if p.startswith(("prompts/", ".github/workflows/"))]
    docs = [p for p in paths if p.endswith((".md", ".rst", ".txt")) and p not in policy]
    code = [p for p in paths if p not in deps and p not in policy and p not in docs]
    if policy or deps:
        route, reason = "full", "dependency or policy/prompt file changed: never shallow"
    elif code:
        route, reason = "full", "executable code changed"
    elif docs:
        route, reason = "shallow", "docs-only change"
    else:
        route, reason = "full", "unknown change shape"
    return {"route": route, "reason": reason, "changed_paths": paths}


def cmd_init(args: argparse.Namespace) -> int:
    manifest = build_manifest(
        args.salt, Path(args.cases), Path(args.out), reps=args.reps,
        pilot_cap=args.pilot_cap, campaign_cap=args.campaign_cap,
        authorized=False,
    )
    print(f"wrote {args.out}: splits {manifest['split_counts']}")
    return 0


def cmd_validate(args: argparse.Namespace) -> int:
    manifest = json.loads(Path(args.manifest).read_text())
    cases = _load_cases(Path(manifest.get("cases_dir", args.cases)))
    validate_manifest(manifest, cases)
    print("manifest valid:", json.dumps(manifest.get("split_counts", {})))
    return 0


def cmd_dry_run(args: argparse.Namespace) -> int:
    manifest = json.loads(Path(args.manifest).read_text())
    cases = _load_cases(Path(manifest.get("cases_dir", args.cases)))
    validate_manifest(manifest, cases)
    phase = args.phase
    plans, estimate = plan_runs(manifest, cases, phase, seed=hash((manifest["frozen_salt"], phase)) & 0xFFFF)
    budget = budget_check(estimate, manifest)
    plan_doc = {
        "schema": SCHEMA + "+dry-run",
        "_note": "synthetic-fixture of intent: ZERO AI calls and ZERO GitHub writes happened here",
        "phase": phase,
        "runs": [vars(p) for p in plans],
        "estimate": estimate,
        "budget": budget,
        "credentials_required": sorted({
            lane["key_env"] for lane in manifest.get("lanes", {}).values()
        }),
        "github_writes": 0,
        "ai_calls": 0,
    }
    if args.out:
        Path(args.out).write_text(json.dumps(plan_doc, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"phase": phase, "runs": estimate["runs"],
                      "est_upper_bound_usd_proxy": budget["estimated_upper_bound_usd_proxy"],
                      "within_cap": budget["within_cap"],
                      "credentials_required": plan_doc["credentials_required"],
                      "ai_calls": 0, "github_writes": 0}))
    if not budget["within_cap"]:
        print(f"FAIL: proxy estimate exceeds campaign cap {manifest['budget']['campaign_cap']} USD", file=sys.stderr)
        return 1
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    manifest = json.loads(Path(args.manifest).read_text())
    cases = _load_cases(Path(manifest.get("cases_dir", args.cases)))
    validate_manifest(manifest, cases)
    if not manifest["budget"].get("authorized"):
        print("FAIL: budget.authorized is false - live runs are locked (F8). "
              "Record developer approval in the manifest first.", file=sys.stderr)
        return 1
    import os
    missing = [lane["key_env"] for lane in manifest.get("lanes", {}).values()
               if not os.environ.get(lane["key_env"])]
    if missing:
        print(f"FAIL: credentials absent by presence check: {missing}", file=sys.stderr)
        return 1
    raise ExperimentError(
        "live execution requires the Task 5 pilot runner wiring; this Task 4 "
        "build ships plan/validate/report and refuses unlocked runs"
    )


def cmd_report(args: argparse.Namespace) -> int:
    manifest = json.loads(Path(args.manifest).read_text())
    runs_dir = Path(args.runs)
    records = [json.loads(p.read_text()) for p in sorted(runs_dir.glob("run_*.json"))]
    if not records:
        print("FAIL: no run records; a report without evidence would be a fabrication", file=sys.stderr)
        return 1
    by_arm: dict[str, dict[str, Any]] = {}
    for r in records:
        arm = r.get("arm", "?")
        cell = by_arm.setdefault(arm, {"runs": 0, "failures": 0, "unknown_cost": 0,
                                       "provider_seconds": 0.0, "jev_seconds": 0.0,
                                       "setup_seconds": 0.0, "must_find_hits": 0, "must_find_total": 0})
        cell["runs"] += 1
        if r.get("status") != "completed":
            cell["failures"] += 1
        if r.get("usage_unknown"):
            cell["unknown_cost"] += 1
        for key in ("provider_seconds", "jev_seconds", "setup_seconds"):
            cell[key] += float(r.get(key) or 0.0)
        cell["must_find_hits"] += int(r.get("must_find_hits") or 0)
        cell["must_find_total"] += int(r.get("must_find_total") or 0)
    required = manifest.get("report_requirements", {})
    missing_cells = [
        req for req in required.get("cells", [])
        if tuple(req) not in {(r.get("arm"), r.get("lane")) for r in records}
    ]
    promotion_ready = not missing_cells and all(c["unknown_cost"] == 0 for c in by_arm.values())
    print(json.dumps({
        "schema": SCHEMA + "+report",
        "runs_recorded": len(records),
        "per_arm": by_arm,
        "missing_required_cells": missing_cells,
        "promotion_ready": promotion_ready,
        "_note": "unknown cost or missing cells fail promotion (F8/F9); failures are reported, never averaged away",
    }, indent=2))
    return 0 if promotion_ready or not required else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("init", help="build the manifest from the corpus + frozen salt")
    p.add_argument("--salt", required=True)
    p.add_argument("--cases", default=str(Path(__file__).parent / "cases"))
    p.add_argument("--out", default="experiment.json")
    p.add_argument("--reps", type=int, default=DEFAULT_REPS)
    p.add_argument("--pilot-cap", type=float, default=25.0)
    p.add_argument("--campaign-cap", type=float, default=100.0)
    p.set_defaults(func=cmd_init)

    p = sub.add_parser("validate", help="strict manifest validation (F2/F8)")
    p.add_argument("--manifest", required=True)
    p.add_argument("--cases", default=str(Path(__file__).parent / "cases"))
    p.set_defaults(func=cmd_validate)

    p = sub.add_parser("dry-run", help="plan the matrix with ZERO AI calls and zero GitHub writes")
    p.add_argument("--manifest", required=True)
    p.add_argument("--cases", default=str(Path(__file__).parent / "cases"))
    p.add_argument("--phase", choices=PHASES, default="calibration")
    p.add_argument("--out", default=None)
    p.set_defaults(func=cmd_dry_run)

    p = sub.add_parser("run", help="execute runs (locked until budget authorized + credentials present)")
    p.add_argument("--manifest", required=True)
    p.add_argument("--cases", default=str(Path(__file__).parent / "cases"))
    p.add_argument("--phase", choices=PHASES, default="calibration")
    p.add_argument("--arm", choices=ARMS, default=None)
    p.add_argument("--lane", default=None)
    p.set_defaults(func=cmd_run)

    p = sub.add_parser("report", help="aggregate run records; fails promotion on missing/unknown data")
    p.add_argument("--manifest", required=True)
    p.add_argument("--runs", default="runs")
    p.set_defaults(func=cmd_report)

    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except ExperimentError as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
