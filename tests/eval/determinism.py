#!/usr/bin/env python3
"""Determinism, duplication and verdict computation over `run-record/3.0` files.

Promoted from the v3 discovery plan's offline re-aggregation of the Jev
campaign (RFC-08 decision D-02). Reads run records (the ones the runtime
writes to `.aiprr/run-record.json` and campaigns store under
`tests/eval/records/`), groups them into cells
(`provider|endpoint_kind|model|arm|case`), and reports:

- per-cell cost mean / relative spread `(max-min)/mean`, recall values,
  turns and provider seconds;
- the lane noise floor (median / mean / worst spread, max recall delta);
- cross-cell duplication of finding anchors when records carry findings
  (only the structured review output does — RFC-05; run records alone
  carry counts, so duplication is reported as unavailable then);
- `--verdict`: RFC-01's promotion / blocking rules applied to a baseline
  campaign vs a candidate campaign, written as `verdict/1.0`.

Stdlib only. Usage:

    python3 tests/eval/determinism.py --selftest
    python3 tests/eval/determinism.py summarize --records DIR [--out summary.json]
    python3 tests/eval/determinism.py verdict --baseline DIR --candidate DIR \
        --out verdict.json [--runtime-sha SHA --prompt-sha256 HEX]
"""

from __future__ import annotations

import argparse
import json
import random
import statistics
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

SCHEMA_VERSION_RUN: str = "run-record/3.0"
SCHEMA_VERSION_VERDICT: str = "verdict/1.0"
MAX_RECORD_FILE_BYTES: int = 1_000_000
MAX_RECORD_FILES: int = 1_000
MIN_REPLICATIONS: int = 3          # RFC-01: fewer → descriptive only
BOOTSTRAP_ROUNDS: int = 2_000
BOOTSTRAP_SEED: int = 42

# RFC-01 § Thresholds — Jev-campaign figures until Phase 0 re-measures them
# (PLAN_v3_implementation Task 8 re-stamps these constants and RFC-01 together).
PROMOTABLE_COST_REL_DELTA: float = 0.27        # ≥ 1.0 × median cell spread (0.266)
PROMOTABLE_RECALL_NET_DEFECTS: int = 3         # > max observed repetition swing (2)
BLOCKING_RECALL_DROP_DEFECTS: int = 2          # recall below baseline by more than the swing
BASELINE_SPREAD_MEDIAN: float = 0.266
BLOCKING_SPREAD_MEDIAN_FACTOR: float = 1.5     # median > 1.5 × baseline median blocks
BLOCKING_SPREAD_WORST: float = 1.0
FIRST_PARTY_KINDS: frozenset[str] = frozenset({"anthropic", "openai"})


# --------------------------------------------------------------------------- loading
def load_records(records_dir: Path) -> list[dict[str, Any]]:
    """Load every `*.json` run record under `records_dir` (bounded, recursive)."""
    out: list[dict[str, Any]] = []
    files: list[Path] = sorted(p for p in records_dir.rglob("*.json") if p.is_file())
    if len(files) > MAX_RECORD_FILES:
        raise ValueError(f"{records_dir}: {len(files)} files exceeds the {MAX_RECORD_FILES} cap")
    for path in files:
        if path.stat().st_size > MAX_RECORD_FILE_BYTES:
            raise ValueError(f"{path}: {path.stat().st_size} bytes exceeds the {MAX_RECORD_FILE_BYTES} cap")
        try:
            data: Any = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            continue
        if isinstance(data, dict) and data.get("schema_version") == SCHEMA_VERSION_RUN:
            data["_source_file"] = str(path)
            out.append(data)
    return out


def cell_key(record: dict[str, Any]) -> str:
    campaign: dict[str, Any] = record.get("campaign") or {}
    context: dict[str, Any] = record.get("context") or {}
    case: str = str(context.get("corpus_case_id") or context.get("head_sha") or "nohead")
    return "|".join(
        [
            str(record.get("provider")),
            str(record.get("endpoint_kind")),
            str(record.get("model")),
            str(campaign.get("arm") or "default"),
            case,
        ]
    )


def _recall(record: dict[str, Any]) -> tuple[int, int] | None:
    score: Any = (record.get("outcome") or {}).get("score")
    if isinstance(score, dict) and isinstance(score.get("must_find_hits"), int) and isinstance(score.get("must_find_total"), int):
        return int(score["must_find_hits"]), int(score["must_find_total"])
    return None


def _spread(values: list[float]) -> float | None:
    if len(values) < 2:
        return None
    mean: float = statistics.fmean(values)
    return (max(values) - min(values)) / mean if mean else None


# --------------------------------------------------------------------------- aggregation
def summarize(records: list[dict[str, Any]]) -> dict[str, Any]:
    complete: list[dict[str, Any]] = [r for r in records if r.get("status") in ("completed", "incomplete")]
    failed: list[dict[str, Any]] = [r for r in records if r.get("status") in ("failed", "timeout")]
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for r in complete:
        grouped[cell_key(r)].append(r)
    cells: dict[str, Any] = {}
    for key, rs in sorted(grouped.items()):
        costs: list[float] = [float(r["cost_usd"]) for r in rs if r.get("usage_known") and r.get("cost_usd") is not None]
        secs: list[float] = [float(r["timings"]["provider_seconds"]) for r in rs if (r.get("timings") or {}).get("provider_seconds") is not None]
        turns: list[int] = [int((r.get("budget") or {}).get("turns_used", 0)) for r in rs]
        recalls: list[int] = [rc[0] for rc in (_recall(r) for r in rs) if rc is not None]
        totals: list[int] = [rc[1] for rc in (_recall(r) for r in rs) if rc is not None]
        cells[key] = {
            "n": len(rs),
            "cost_mean": statistics.fmean(costs) if costs else None,
            "cost_min": min(costs) if costs else None,
            "cost_max": max(costs) if costs else None,
            "cost_relative_spread": _spread(costs),
            "seconds_mean": statistics.fmean(secs) if secs else None,
            "turns_mean": statistics.fmean(turns) if turns else None,
            "recall_values": recalls,
            "recall_total": totals[0] if totals else None,
            "recall_delta": (max(recalls) - min(recalls)) if len(recalls) >= 2 else None,
            "usage_unknown_runs": sum(1 for r in rs if not r.get("usage_known")),
            "descriptive_only": len(rs) < MIN_REPLICATIONS,
        }
    replicated: list[dict[str, Any]] = [c for c in cells.values() if c["n"] >= 2 and c["cost_relative_spread"] is not None]
    spreads: list[float] = sorted(c["cost_relative_spread"] for c in replicated)
    deltas: list[int] = [c["recall_delta"] for c in replicated if c["recall_delta"] is not None]
    noise_floor: dict[str, Any] = {
        "replicated_cells": len(replicated),
        "cost_relative_spread_median": statistics.median(spreads) if spreads else None,
        "cost_relative_spread_mean": statistics.fmean(spreads) if spreads else None,
        "cost_relative_spread_worst": spreads[-1] if spreads else None,
        "recall_delta_max": max(deltas) if deltas else None,
        "recall_delta_cells_nonzero": sum(1 for d in deltas if d),
    }
    first_party_unknown: list[str] = [
        str(r.get("run_id")) for r in complete
        if r.get("endpoint_kind") in FIRST_PARTY_KINDS and not r.get("usage_known")
    ]
    return {
        "records_total": len(records),
        "records_complete": len(complete),
        "records_failed": len(failed),
        "cells": cells,
        "noise_floor": noise_floor,
        "first_party_unknown_usage_run_ids": first_party_unknown,
        "duplication": {"available": False, "reason": "run records carry finding counts, not anchors; duplication is measured over structured review outputs (RFC-05)"},
        "campaign_cost": {
            "sum_usd": sum(float(r["cost_usd"]) for r in complete if r.get("usage_known") and r.get("cost_usd") is not None),
            "records_with_cost": sum(1 for r in complete if r.get("usage_known") and r.get("cost_usd") is not None),
            "records_without_cost": sum(1 for r in complete if not (r.get("usage_known") and r.get("cost_usd") is not None)),
        },
    }


# --------------------------------------------------------------------------- verdict
def _paired_cells(base: dict[str, Any], cand: dict[str, Any]) -> list[str]:
    """Cells present in both campaigns, matched on everything but the arm."""
    def strip_arm(key: str) -> str:
        parts: list[str] = key.split("|")
        return "|".join(parts[:3] + parts[4:]) if len(parts) == 5 else key
    b: dict[str, str] = {strip_arm(k): k for k in base["cells"]}
    c: dict[str, str] = {strip_arm(k): k for k in cand["cells"]}
    return sorted(k for k in b if k in c)


def _bootstrap_ci(diffs: list[float]) -> tuple[float, float] | None:
    if len(diffs) < 2:
        return None
    rng = random.Random(BOOTSTRAP_SEED)
    means: list[float] = []
    for _ in range(BOOTSTRAP_ROUNDS):
        sample: list[float] = [rng.choice(diffs) for _ in diffs]
        means.append(statistics.fmean(sample))
    means.sort()
    lo: float = means[int(0.025 * (len(means) - 1))]
    hi: float = means[int(0.975 * (len(means) - 1))]
    return lo, hi


def verdict(
    baseline: list[dict[str, Any]],
    candidate: list[dict[str, Any]],
    *,
    candidate_runtime_sha: str,
    prompt_sha256: str,
    baseline_ref: str,
) -> dict[str, Any]:
    base: dict[str, Any] = summarize(baseline)
    cand: dict[str, Any] = summarize(candidate)
    blocking: list[str] = []
    notes: list[str] = []
    paired: list[str] = _paired_cells(base, cand)

    def strip_arm(key: str) -> str:
        parts: list[str] = key.split("|")
        return "|".join(parts[:3] + parts[4:]) if len(parts) == 5 else key
    b_by: dict[str, dict[str, Any]] = {strip_arm(k): v for k, v in base["cells"].items()}
    c_by: dict[str, dict[str, Any]] = {strip_arm(k): v for k, v in cand["cells"].items()}

    descriptive_only: bool = any(c_by[k]["descriptive_only"] for k in paired) or len(paired) < 4
    if len(paired) < 4:
        notes.append(f"only {len(paired)} paired cell(s); a lane needs at least 4 cases")

    # cost: paired relative difference (negative = cheaper)
    cost_diffs: list[float] = []
    recall_diffs: list[int] = []
    for k in paired:
        b, c = b_by[k], c_by[k]
        if b["cost_mean"] and c["cost_mean"] is not None:
            cost_diffs.append((c["cost_mean"] - b["cost_mean"]) / b["cost_mean"])
        if b["recall_values"] and c["recall_values"]:
            recall_diffs.append(round(statistics.fmean(c["recall_values"]) - statistics.fmean(b["recall_values"])))
    cost_point: float | None = statistics.fmean(cost_diffs) if cost_diffs else None
    cost_ci = _bootstrap_ci(cost_diffs)
    recall_net: int | None = sum(recall_diffs) if recall_diffs else None
    recall_ci = _bootstrap_ci([float(d) for d in recall_diffs])

    promotable: dict[str, Any] = {
        "cost": bool(
            not descriptive_only and cost_point is not None and cost_ci is not None
            and cost_ci[1] < 0 and -cost_point >= PROMOTABLE_COST_REL_DELTA
        ),
        "recall": bool(
            not descriptive_only and recall_net is not None and recall_ci is not None
            and recall_ci[0] > 0 and recall_net >= PROMOTABLE_RECALL_NET_DEFECTS
        ),
        "precision": False,  # needs adjudication records (Task 8+); never inferred from counts
    }
    # blocking rules
    for k in paired:
        b, c = b_by[k], c_by[k]
        if b["recall_values"] and c["recall_values"]:
            drop: float = statistics.fmean(b["recall_values"]) - statistics.fmean(c["recall_values"])
            if drop > BLOCKING_RECALL_DROP_DEFECTS:
                blocking.append(f"recall regression on {k}: -{drop:.1f} defects")
    nf: dict[str, Any] = cand["noise_floor"]
    if nf["cost_relative_spread_median"] is not None and nf["cost_relative_spread_median"] > BLOCKING_SPREAD_MEDIAN_FACTOR * BASELINE_SPREAD_MEDIAN:
        blocking.append(f"determinism widened: median spread {nf['cost_relative_spread_median']:.3f} > {BLOCKING_SPREAD_MEDIAN_FACTOR} × {BASELINE_SPREAD_MEDIAN}")
    if nf["cost_relative_spread_worst"] is not None and nf["cost_relative_spread_worst"] > BLOCKING_SPREAD_WORST:
        blocking.append(f"determinism widened: worst spread {nf['cost_relative_spread_worst']:.3f} > {BLOCKING_SPREAD_WORST}")
    if cand["first_party_unknown_usage_run_ids"]:
        blocking.append(f"unknown usage on first-party lane runs: {', '.join(cand['first_party_unknown_usage_run_ids'][:5])}")
    if cand["records_failed"]:
        notes.append(f"{cand['records_failed']} failed/timeout record(s) excluded from cells")
    return {
        "schema_version": SCHEMA_VERSION_VERDICT,
        "candidate_runtime_sha": candidate_runtime_sha,
        "prompt_sha256": prompt_sha256,
        "baseline_ref": baseline_ref,
        "computed_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "paired_cells": len(paired),
        "descriptive_only": descriptive_only,
        "measurements": {
            "cost_relative_delta_point": cost_point,
            "cost_relative_delta_ci95": list(cost_ci) if cost_ci else None,
            "recall_net_defects": recall_net,
            "recall_ci95": list(recall_ci) if recall_ci else None,
            "candidate_noise_floor": nf,
        },
        "promotable": promotable,
        "blocking": blocking,
        "notes": notes,
        "thresholds": {
            "promotable_cost_rel_delta": PROMOTABLE_COST_REL_DELTA,
            "promotable_recall_net_defects": PROMOTABLE_RECALL_NET_DEFECTS,
            "blocking_recall_drop_defects": BLOCKING_RECALL_DROP_DEFECTS,
            "baseline_spread_median": BASELINE_SPREAD_MEDIAN,
            "blocking_spread_median_factor": BLOCKING_SPREAD_MEDIAN_FACTOR,
            "blocking_spread_worst": BLOCKING_SPREAD_WORST,
        },
    }


# --------------------------------------------------------------------------- selftest
def _rec(**kw: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION_RUN, "run_id": kw.pop("run_id", "r"), "provider": "grok", "endpoint_kind": "xai",
        "model": "grok-4.5", "status": "completed", "usage_known": True, "cost_usd": 0.5,
        "timings": {"provider_seconds": 100.0}, "budget": {"turns_used": 10},
        "outcome": {"score": {"must_find_hits": 3, "must_find_total": 5}},
        "campaign": {"arm": "baseline"}, "context": {"corpus_case_id": "C001"},
    }
    for k, v in kw.items():
        if k in ("cost_usd", "usage_known", "status", "provider", "endpoint_kind", "model", "run_id"):
            base[k] = v
        elif k == "hits":
            base["outcome"]["score"]["must_find_hits"] = v
        elif k == "arm":
            base["campaign"]["arm"] = v
        elif k == "case":
            base["context"]["corpus_case_id"] = v
    return base


def selftest() -> int:
    recs: list[dict[str, Any]] = [
        _rec(run_id="a1", cost_usd=1.0), _rec(run_id="a2", cost_usd=2.0, hits=1), _rec(run_id="a3", cost_usd=1.5),
        _rec(run_id="f1", status="failed"), _rec(run_id="u1", provider="anthropic", endpoint_kind="anthropic", usage_known=False, cost_usd=None, case="C002"),
    ]
    s = summarize(recs)
    assert s["records_total"] == 5 and s["records_failed"] == 1, s["records_failed"]
    cell = s["cells"]["grok|xai|grok-4.5|baseline|C001"]
    assert cell["n"] == 3 and abs(cell["cost_relative_spread"] - (1.0 / 1.5)) < 1e-9, cell
    assert cell["recall_delta"] == 2 and not cell["descriptive_only"]
    assert s["first_party_unknown_usage_run_ids"] == ["u1"]
    assert abs(s["campaign_cost"]["sum_usd"] - 4.5) < 1e-9
    # verdict: candidate 40% cheaper and +1 recall on 4 cases × 3 reps → cost promotable, recall not (net 4 but CI…)
    base: list[dict[str, Any]] = []
    cand: list[dict[str, Any]] = []
    for case in ("C001", "C002", "C003", "C004"):
        for i in range(3):
            base.append(_rec(run_id=f"b-{case}-{i}", case=case, cost_usd=1.0 + 0.05 * i, hits=3))
            cand.append(_rec(run_id=f"c-{case}-{i}", case=case, arm="candidate", cost_usd=0.6 + 0.05 * i, hits=4))
    v = verdict(base, cand, candidate_runtime_sha="abc", prompt_sha256="0" * 64, baseline_ref="phase0")
    assert v["promotable"]["cost"] is True, v["measurements"]
    assert v["promotable"]["recall"] is True and v["measurements"]["recall_net_defects"] == 4, v["measurements"]
    assert v["blocking"] == [], v["blocking"]
    # regression: candidate loses 3 defects on one case → blocking
    worse: list[dict[str, Any]] = [dict(r, outcome={"score": {"must_find_hits": 0, "must_find_total": 5}}) if r["context"]["corpus_case_id"] == "C001" else r for r in cand]
    v2 = verdict(base, worse, candidate_runtime_sha="abc", prompt_sha256="0" * 64, baseline_ref="phase0")
    assert any("recall regression" in b for b in v2["blocking"]), v2["blocking"]
    # descriptive only with 1 rep
    v3 = verdict(base[:4], cand[:4], candidate_runtime_sha="abc", prompt_sha256="0" * 64, baseline_ref="phase0")
    assert v3["descriptive_only"] is True and v3["promotable"]["cost"] is False
    print("selftest ok")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--selftest", action="store_true")
    sub = parser.add_subparsers(dest="command")
    ps = sub.add_parser("summarize"); ps.add_argument("--records", required=True); ps.add_argument("--out")
    pv = sub.add_parser("verdict"); pv.add_argument("--baseline", required=True); pv.add_argument("--candidate", required=True)
    pv.add_argument("--out", required=True); pv.add_argument("--runtime-sha", default="unknown"); pv.add_argument("--prompt-sha256", default="0" * 64)
    pv.add_argument("--baseline-ref", default="baseline")
    args = parser.parse_args(argv)
    if args.selftest:
        return selftest()
    if args.command == "summarize":
        result = summarize(load_records(Path(args.records)))
        text = json.dumps(result, indent=2)
        if args.out:
            Path(args.out).write_text(text + "\n", encoding="utf-8")
        else:
            print(text)
        return 0
    if args.command == "verdict":
        v = verdict(
            load_records(Path(args.baseline)), load_records(Path(args.candidate)),
            candidate_runtime_sha=args.runtime_sha, prompt_sha256=args.prompt_sha256, baseline_ref=args.baseline_ref,
        )
        Path(args.out).write_text(json.dumps(v, indent=2) + "\n", encoding="utf-8")
        print(f"verdict written: promotable={v['promotable']} blocking={len(v['blocking'])} descriptive_only={v['descriptive_only']}")
        return 1 if v["blocking"] else 0
    parser.error("give --selftest, summarize or verdict")
    return 2


if __name__ == "__main__":
    sys.exit(main())
