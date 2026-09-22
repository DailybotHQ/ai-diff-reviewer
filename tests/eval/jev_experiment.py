#!/usr/bin/env python3
"""Controlled-comparison experiment driver (PLAN_jev_review_acceleration T4).

Implements the four interfaces the validation register requires:

    python3 tests/eval/jev_experiment.py init     --salt S --out experiment.json
    python3 tests/eval/jev_experiment.py validate --manifest experiment.json
    python3 tests/eval/jev_experiment.py dry-run  --manifest experiment.json [--out plan.json]
    python3 tests/eval/jev_experiment.py report   --manifest experiment.json [--runs runs]

`run` is intentionally absent: the Jev transport was removed as NO-GO, so this
harness plans (zero-call) and scores. Actual reviews are produced per vendor
by `run_eval.py`; their records are dropped into the `--runs` directory.

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


def corpus_validate_load_cases_safe():
    """Loader passthrough for the harness smoke tests (keeps the corpus
    import surface in one place)."""
    return corpus_validate.load_cases(Path(__file__).parent / "cases")

SCHEMA = "ai-diff-reviewer/jev-experiment/1"
ARMS = ("baseline", "deterministic")
PHASES = ("calibration", "heldout", "confirmation")
SPLIT_SHARES = {"calibration": 0.50, "heldout": 0.30, "confirmation": 0.20}
MIN_HELDOUT = {"critical_positive": 8, "negative_control": 8}
MIN_CONFIRMATION = {"critical_positive": 4}
DEFAULT_REPS = 3
# Report intake guards: run files are untrusted input (they may be
# vendor-written), so bound both their size and their count.
MAX_RUN_FILE_BYTES = 1_000_000
MAX_RUN_FILES = 1_000
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


def build_corpus_digest(cases: dict[str, dict[str, Any]]) -> str:
    """Stable digest of the corpus the splits were derived from.

    Covers each case's fixture `revision_pin` AND its labels, so a manifest
    frozen against one corpus fails validation the moment either the fixtures
    or the labelling changes (F2 split integrity).
    """
    material = {
        cid: {
            "revision_pin": cases[cid]["fixture"].get("revision_pin", ""),
            "labels": cases[cid].get("labels", []),
        }
        for cid in sorted(cases)
    }
    return hashlib.sha256(
        json.dumps(material, sort_keys=True).encode()
    ).hexdigest()


def build_manifest(salt: str, cases_dir: Path, out: Path, *, reps: int = DEFAULT_REPS,
                   pilot_cap: float = 25.0, campaign_cap: float = 100.0,
                   authorized: bool = False) -> dict[str, Any]:
    cases = _load_cases(cases_dir)
    assignment = assign_splits(cases, salt)
    corpus_digest = build_corpus_digest(cases)
    lanes = {
        "grok": {"model": "grok-4.5", "key_env": "XAI_API_KEY", "kind": "coding"},
        "glm-claude-code": {"model": "glm-5.3", "key_env": "ZAI_CODING_API_KEY", "kind": "coding"},
    }
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
        "lanes": lanes,
        "runs": [],
        # Default promotion bar: every arm x lane cell must be present, costed
        # and completed. Declared at init so an unattended `report` can fail
        # the process (exit 1) without anyone hand-editing the manifest first.
        "report_requirements": {
            "cells": [[arm, lane] for arm in ARMS for lane in sorted(lanes)]
        },
    }
    out.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    return manifest


def validate_manifest(manifest: dict[str, Any], cases: dict[str, dict[str, Any]]) -> None:
    problems: list[str] = []
    if manifest.get("schema") != SCHEMA:
        problems.append(f"schema {manifest.get('schema')!r} != {SCHEMA!r}")
    if not manifest.get("frozen_salt"):
        problems.append("frozen_salt missing: splits are not reproducible (F2)")
    if manifest.get("corpus_digest") != build_corpus_digest(cases):
        problems.append(
            "corpus_digest mismatch: the corpus (fixtures or labels) changed "
            "since init - refreeze the manifest before trusting the splits (F2)"
        )
    splits = manifest.get("splits") or {}
    unknown_split_ids = sorted(set(splits) - set(cases))
    if unknown_split_ids:
        problems.append(
            f"splits reference unknown case ids: {unknown_split_ids[:5]} - "
            "fabricated entries must never count toward the phase minimums (F2)"
        )
    invalid_phases = sorted({p for p in splits.values() if p not in PHASES})
    if invalid_phases:
        problems.append(f"splits carry invalid phase values: {invalid_phases}")
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
    cells: list[tuple[str, str, str, int]] = []
    for cid, phase_of in sorted(splits.items()):
        if phase_of != phase:
            continue
        for arm in manifest["arms"]:
            for lane in coding_lanes:
                for rep in range(manifest.get("repetitions", DEFAULT_REPS)):
                    cells.append((arm, lane, cid, rep))
    rng.shuffle(cells)
    for arm, lane, cid, rep in cells:
        size = sum(len(str(part)) for part in (
            cases[cid]["fixture"].get("base", {}), cases[cid]["fixture"].get("head", {})
        ))
        # `rep` travels with the cell so a plan identifies the intended
        # repetition regardless of shuffle order - order-dependent numbering
        # would make per-repetition results irreproducible.
        plans.append(RunPlan(arm, lane, cid, phase, rep, size))
    estimate = {
        "method": "chars/4 conservative proxy (labelled; measured-max x1.5 replaces it once the first live lane measures)",
        "runs": len(plans),
        "est_total_input_chars": sum(p.est_input_chars for p in plans),
        "est_total_input_tokens_proxy": sum(p.est_input_chars for p in plans) // 4,
    }
    return plans, estimate


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
    """The explicit, cheap rules comparator (contract: never weakened to flatter any arm).

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
    # F9 determinism: str hash() is salted per process - derive the seed from
    # the frozen salt via sha256 so the interleave order is reproducible.
    seed = int(hashlib.sha256(f"{manifest['frozen_salt']}:{phase}".encode("utf-8")).hexdigest()[:8], 16)
    plans, estimate = plan_runs(manifest, cases, phase, seed=seed)
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



def cmd_report(args: argparse.Namespace) -> int:
    manifest = json.loads(Path(args.manifest).read_text())
    runs_dir = Path(args.runs)
    run_files = sorted(runs_dir.glob("run_*.json"))
    if len(run_files) > MAX_RUN_FILES:
        print(f"FAIL: {len(run_files)} run files exceeds the {MAX_RUN_FILES}-file cap", file=sys.stderr)
        return 1
    records: list[dict[str, Any]] = []
    rejected: list[str] = []
    for run_path in run_files:
        if run_path.stat().st_size > MAX_RUN_FILE_BYTES:
            rejected.append(f"{run_path.name}: file exceeds the {MAX_RUN_FILE_BYTES}-byte cap")
            continue
        try:
            record = json.loads(run_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError, OSError) as exc:
            rejected.append(f"{run_path.name}: unreadable ({exc})")
            continue
        record.setdefault("run_id", run_path.name)
        records.append(record)
    if rejected:
        print("FAIL: run files rejected at intake:\n  - " + "\n  - ".join(rejected[:10]), file=sys.stderr)
        return 1
    if not records:
        print("FAIL: no run records; a report without evidence would be a fabrication", file=sys.stderr)
        return 1
    # Records must belong to THIS manifest: unknown arms/lanes/cases or a
    # mismatched corpus digest mean the evidence is stale or fabricated.
    arms = set(manifest.get("arms", ARMS))
    lanes = set(manifest.get("lanes", {}))
    splits = manifest.get("splits", {})
    digest = manifest.get("corpus_digest", "")
    binding_failures: list[str] = []
    covered: set[tuple[str, str, str]] = set()
    for r in records:
        label = r.get("run_id") or "<unnamed record>"
        if r.get("arm") not in arms:
            binding_failures.append(f"{label}: unknown arm {r.get('arm')!r}")
        if r.get("lane") not in lanes:
            binding_failures.append(f"{label}: unknown lane {r.get('lane')!r}")
        if r.get("case_id") not in splits:
            binding_failures.append(f"{label}: case_id {r.get('case_id')!r} not in manifest splits")
        if digest and r.get("corpus_digest") not in (None, "", digest):
            binding_failures.append(f"{label}: corpus_digest mismatch (frozen {digest!r})")
        covered.add((r.get("arm"), r.get("lane"), r.get("case_id"), int(r.get("rep") or 0)))
    if binding_failures:
        print("FAIL: run records do not belong to this manifest:\n  - " + "\n  - ".join(binding_failures[:10]), file=sys.stderr)
        return 1
    by_arm: dict[str, dict[str, Any]] = {}
    for r in records:
        arm = r.get("arm", "?")
        cell = by_arm.setdefault(arm, {"runs": 0, "failures": 0, "unknown_cost": 0,
                                       "provider_seconds": 0.0,
                                       "setup_seconds": 0.0, "must_find_hits": 0, "must_find_total": 0})
        cell["runs"] += 1
        if r.get("status") != "completed":
            cell["failures"] += 1
        if r.get("usage_unknown"):
            cell["unknown_cost"] += 1
        # run_eval.py records carry total `seconds` + `setup_seconds`; the
        # campaign runner schema carries `provider_seconds` directly.
        provider_time = r.get("provider_seconds")
        if provider_time is None and r.get("seconds") is not None:
            provider_time = float(r["seconds"]) - float(r.get("setup_seconds") or 0.0)
        cell["provider_seconds"] += float(provider_time or 0.0)
        cell["setup_seconds"] += float(r.get("setup_seconds") or 0.0)
        cell["must_find_hits"] += int(r.get("must_find_hits") or 0)
        cell["must_find_total"] += int(r.get("must_find_total") or 0)
    required = manifest.get("report_requirements", {})
    missing_cells = [
        req for req in required.get("cells", [])
        if tuple(req) not in {(r.get("arm"), r.get("lane")) for r in records}
    ]
    # Cell cardinality: a required (arm, lane) cell is only satisfied when
    # it reviewed EVERY case in the frozen split - a handful of stale but
    # complete-looking records must not be able to tick a cell.
    required_cells = [tuple(c) for c in (required.get("cells") or [])]
    repetitions = int(manifest.get("repetitions", 1))
    expected_triples = {
        (arm, lane, cid, rep)
        for (arm, lane) in required_cells
        for cid in splits
        for rep in range(repetitions)
    }
    missing_case_coverage = sorted(expected_triples - covered)
    promotion_ready = not missing_cells and not missing_case_coverage and all(
        c["unknown_cost"] == 0 and c["failures"] == 0 for c in by_arm.values()
    )
    print(json.dumps({
        "schema": SCHEMA + "+report",
        "runs_recorded": len(records),
        "per_arm": by_arm,
        "missing_required_cells": missing_cells,
        "missing_case_coverage_count": len(missing_case_coverage),
        "missing_case_coverage_sample": missing_case_coverage[:5],
        "promotion_ready": promotion_ready,
        "_note": "unknown cost, failed runs, or missing cells fail promotion (F8/F9); failures are reported, never averaged away",
    }, indent=2))
    # The exit code IS the promotion bar: any unmet requirement - missing
    # declared cells, unknown cost, failed runs - exits 1, so a CI gate keyed
    # on the exit code can never green-wash a failed campaign. `init` seeds
    # default report_requirements, so even an unattended report is strict.
    if not promotion_ready:
        return 1
    return 0


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
