#!/usr/bin/env python3
"""Budgeted evaluation campaign driver (RFC-01 online layer; stdlib only).

A campaign is a manifest (`campaign.json`) of lanes × arms × cells ×
repetitions. The driver plans the runs, projects their cost from each lane's
indicative maximum, refuses to start above the cap, stops at 90 % of the
budget (experiment contract F8), executes each run through `run_eval.py`
(PR cells or fixture-tree cells), collects the `run-record/3.0` files into
a records directory beside a copy of the manifest, and — when a baseline
records directory is given — computes the RFC-01 verdict.

    python3 tests/eval/campaign.py plan     --manifest campaign.json
    python3 tests/eval/campaign.py dry-run  --manifest campaign.json [--budget-usd 40]
    python3 tests/eval/campaign.py run      --manifest campaign.json --budget-usd 40 \
        --records-out tests/eval/records/campaigns/<id> [--lane grok] [--baseline DIR --verdict-out verdicts/<id>.json]

Manifest shape:
{
  "campaign_id": "phase0-floor",
  "repetitions": 3,
  "lanes": {"grok": {"provider": "grok", "model": "balanced", "api_base": "", "api_key_env": "XAI_API_KEY",
                     "indicative_max_cost_usd": 1.5}},
  "arms":  [{"name": "baseline", "prompt": "prompts/default.md", "extension": ".review/extension.md"}],
  "cells": [{"kind": "pr", "repo": "DailybotHQ/ai-diff-reviewer", "pr": 46, "worktree": "/tmp/wt-46"},
            {"kind": "tree", "case": "tests/eval/cases/C001.json"}]
}
Unknown cost counts against the cap at the lane's indicative maximum — never zero.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

ROOT: Path = Path(__file__).resolve().parents[2]
RUN_EVAL: Path = Path(__file__).resolve().parent / "run_eval.py"
DETERMINISM: Path = Path(__file__).resolve().parent / "determinism.py"
STOP_FRACTION: float = 0.90  # F8: hard stop at 90 % of the cap, in-flight cost accounted
SCHEMA_VERSION: str = "ai-diff-reviewer/campaign/1"


@dataclass(frozen=True)
class RunPlan:
    lane: str
    arm: str
    cell_id: str
    repetition: int
    cell: dict[str, Any]
    out: Path

    @property
    def cell_key(self) -> str:
        return f"{self.lane}|{self.arm}|{self.cell_id}"


def load_manifest(path: Path) -> dict[str, Any]:
    manifest: Any = json.loads(path.read_text(encoding="utf-8"))
    problems: list[str] = validate_manifest(manifest)
    if problems:
        raise ValueError("manifest invalid:\n  - " + "\n  - ".join(problems))
    return manifest


def validate_manifest(m: Any) -> list[str]:
    problems: list[str] = []
    if not isinstance(m, dict):
        return ["manifest must be an object"]
    if not m.get("campaign_id"):
        problems.append("campaign_id required")
    if not isinstance(m.get("repetitions"), int) or m["repetitions"] < 1:
        problems.append("repetitions must be an int >= 1")
    lanes: Any = m.get("lanes")
    if not isinstance(lanes, dict) or not lanes:
        problems.append("lanes must be a non-empty object")
    else:
        for name, lane in lanes.items():
            for key in ("provider", "api_key_env", "indicative_max_cost_usd"):
                if key not in lane:
                    problems.append(f"lane {name!r}: {key} required")
            if not isinstance(lane.get("indicative_max_cost_usd"), (int, float)) or lane.get("indicative_max_cost_usd", 0) <= 0:
                problems.append(f"lane {name!r}: indicative_max_cost_usd must be a positive number")
    arms: Any = m.get("arms")
    if not isinstance(arms, list) or not arms:
        problems.append("arms must be a non-empty list")
    else:
        for arm in arms:
            if not arm.get("name") or not arm.get("prompt"):
                problems.append("every arm needs name and prompt")
    cells: Any = m.get("cells")
    if not isinstance(cells, list) or not cells:
        problems.append("cells must be a non-empty list")
    else:
        for cell in cells:
            kind = cell.get("kind")
            if kind == "pr":
                if not (cell.get("repo") and isinstance(cell.get("pr"), int) and cell.get("worktree")):
                    problems.append("pr cell needs repo, pr, worktree")
            elif kind == "tree":
                if not cell.get("case"):
                    problems.append("tree cell needs case")
            else:
                problems.append(f"cell kind {kind!r} must be pr or tree")
    return problems


def cell_id_of(cell: dict[str, Any]) -> str:
    if cell.get("kind") == "pr":
        return f"pr{cell['pr']}"
    return Path(str(cell["case"])).stem


def plan(manifest: dict[str, Any], records_out: Path, *, lane_filter: str | None = None) -> list[RunPlan]:
    runs: list[RunPlan] = []
    for lane_name in sorted(manifest["lanes"]):
        if lane_filter and lane_name != lane_filter:
            continue
        for arm in manifest["arms"]:
            for cell in manifest["cells"]:
                cid: str = cell_id_of(cell)
                for rep in range(int(manifest["repetitions"])):
                    out: Path = records_out / lane_name / arm["name"] / f"{cid}-r{rep}.json"
                    runs.append(RunPlan(lane_name, str(arm["name"]), cid, rep, cell, out))
    return runs


def projection(manifest: dict[str, Any], runs: list[RunPlan]) -> dict[str, Any]:
    per_lane: dict[str, float] = {}
    for r in runs:
        per_lane[r.lane] = per_lane.get(r.lane, 0.0) + float(manifest["lanes"][r.lane]["indicative_max_cost_usd"])
    return {"runs": len(runs), "projected_max_usd": round(sum(per_lane.values()), 2), "per_lane_max_usd": {k: round(v, 2) for k, v in per_lane.items()}}


def run_eval_command(manifest: dict[str, Any], r: RunPlan) -> list[str]:
    lane: dict[str, Any] = manifest["lanes"][r.lane]
    arm: dict[str, Any] = next(a for a in manifest["arms"] if a["name"] == r.arm)
    argv: list[str] = [sys.executable, str(RUN_EVAL), "run", "--provider", str(lane["provider"]), "--model", str(lane.get("model", "")),
                       "--api-base", str(lane.get("api_base", "")), "--api-key-env", str(lane["api_key_env"]),
                       "--prompt", str(ROOT / arm["prompt"]), "--out", str(r.out)]
    if arm.get("extension"):
        argv += ["--extension", str(ROOT / arm["extension"])]
    if r.cell["kind"] == "pr":
        wt: Path = Path(str(r.cell["worktree"]))
        if not wt.is_absolute():
            wt = ROOT / wt
        argv += ["--repo", str(r.cell["repo"]), "--pr", str(r.cell["pr"]), "--worktree", str(wt)]
    else:
        argv += ["--tree", str(ROOT / r.cell["case"])]
    return argv


def _completed_record(record_path: Path) -> dict[str, Any] | None:
    """A record left by an earlier (interrupted) campaign counts as done when it carries a campaign stamp."""
    if not record_path.is_file():
        return None
    try:
        data: Any = json.loads(record_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    if isinstance(data, dict) and isinstance(data.get("campaign"), dict) and data.get("status") in ("completed", "incomplete"):
        return data
    return None


def default_runner(argv: list[str], record_path: Path, env: dict[str, str]) -> dict[str, Any] | None:
    """Execute one run through run_eval.py; return its run record (or None)."""
    subprocess.run(argv, check=False, env=env)
    if record_path.is_file():
        try:
            return json.loads(record_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return None
    return None


def execute(
    manifest: dict[str, Any],
    runs: list[RunPlan],
    *,
    budget_usd: float,
    records_out: Path,
    runner: Callable[[list[str], Path, dict[str, str]], dict[str, Any] | None] = default_runner,
    runtime_sha: str = "",
) -> dict[str, Any]:
    """Run the plan under the cap. Returns the spend ledger; never exceeds 90 % of the budget."""
    records_out.mkdir(parents=True, exist_ok=True)
    stop_at: float = STOP_FRACTION * budget_usd
    spent: float = 0.0
    unknown_runs: int = 0
    executed: int = 0
    resumed: int = 0
    stopped_reason: str | None = None
    env: dict[str, str] = dict(os.environ)
    if runtime_sha:
        env["AIPRR_RUNTIME_SHA"] = runtime_sha
    for r in runs:
        lane_max: float = float(manifest["lanes"][r.lane]["indicative_max_cost_usd"])
        if spent + lane_max > stop_at:
            stopped_reason = (f"cap: spent ${spent:.2f} + next run max ${lane_max:.2f} would exceed "
                              f"{int(STOP_FRACTION * 100)} % of the ${budget_usd:.2f} budget (${stop_at:.2f})")
            break
        r.out.parent.mkdir(parents=True, exist_ok=True)
        record_path: Path = Path(str(r.out) + ".run-record.json")
        existing: dict[str, Any] | None = _completed_record(record_path)
        if existing is not None:
            # Resume: a completed record from an interrupted campaign is kept, never re-bought.
            resumed += 1
            if existing.get("usage_known") and existing.get("cost_usd") is not None:
                spent += float(existing["cost_usd"])
            else:
                unknown_runs += 1
                spent += lane_max
            continue
        record: dict[str, Any] | None = runner(run_eval_command(manifest, r), record_path, env)
        executed += 1
        if record is None:
            unknown_runs += 1
            spent += lane_max
            continue
        record.setdefault("campaign", None)
        record["campaign"] = {"id": str(manifest["campaign_id"]), "cell": r.cell_key, "repetition": r.repetition, "arm": r.arm}
        record_path.write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
        if record.get("usage_known") and record.get("cost_usd") is not None:
            spent += float(record["cost_usd"])
        else:
            unknown_runs += 1
            spent += lane_max
    manifest_copy: dict[str, Any] = dict(manifest)
    manifest_copy["schema"] = SCHEMA_VERSION
    manifest_copy["cells_planned"] = sorted({r.cell_key for r in runs})
    manifest_copy["budget_usd"] = budget_usd
    (records_out / "campaign.json").write_text(json.dumps(
        {"schema": SCHEMA_VERSION, "campaign_id": manifest["campaign_id"], "repetitions": manifest["repetitions"],
         "cells": sorted({r.cell_key for r in runs}), "budget_usd": budget_usd}, indent=2) + "\n", encoding="utf-8")
    ledger: dict[str, Any] = {
        "campaign_id": manifest["campaign_id"], "planned": len(runs), "executed": executed, "resumed": resumed,
        "spent_usd_upper_bound": round(spent, 4), "unknown_cost_runs": unknown_runs,
        "budget_usd": budget_usd, "stop_at_usd": round(stop_at, 4), "stopped_reason": stopped_reason,
    }
    (records_out / "ledger.json").write_text(json.dumps(ledger, indent=2) + "\n", encoding="utf-8")
    return ledger


def compute_verdict(records_out: Path, baseline: Path, verdict_out: Path, *, runtime_sha: str, prompt_sha256: str) -> int:
    import hashlib
    content_sha: str = hashlib.sha256((ROOT / "scripts" / "reviewer.py").read_bytes()).hexdigest()
    argv: list[str] = [sys.executable, str(DETERMINISM), "verdict", "--baseline", str(baseline), "--candidate", str(records_out),
                       "--out", str(verdict_out), "--runtime-sha", runtime_sha, "--prompt-sha256", prompt_sha256,
                       "--baseline-ref", baseline.name, "--content-sha256", content_sha]
    return subprocess.run(argv, check=False).returncode


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("plan", "dry-run", "run"):
        p = sub.add_parser(name)
        p.add_argument("--manifest", required=True)
        p.add_argument("--records-out", default="")
        p.add_argument("--lane", default=None)
        p.add_argument("--budget-usd", type=float, default=None)
        if name == "run":
            p.add_argument("--baseline", default="")
            p.add_argument("--verdict-out", default="")
            p.add_argument("--runtime-sha", default="")
            p.add_argument("--prompt-sha256", default="0" * 64)
    args = parser.parse_args(argv)
    manifest: dict[str, Any] = load_manifest(Path(args.manifest))
    records_out: Path = Path(args.records_out or (ROOT / "tests" / "eval" / "records" / "campaigns" / manifest["campaign_id"]))
    runs: list[RunPlan] = plan(manifest, records_out, lane_filter=args.lane)
    proj: dict[str, Any] = projection(manifest, runs)
    if args.command == "plan":
        for r in runs:
            print(f"{r.cell_key} r{r.repetition} → {r.out}")
        print(json.dumps(proj))
        return 0
    if args.command == "dry-run":
        budget: float | None = args.budget_usd
        verdict_line: str = ""
        if budget is not None:
            fits: bool = proj["projected_max_usd"] <= STOP_FRACTION * budget
            verdict_line = f" — {'fits' if fits else 'EXCEEDS'} {int(STOP_FRACTION * 100)} % of ${budget:.2f}"
        print(json.dumps(proj) + verdict_line)
        return 0 if (budget is None or proj["projected_max_usd"] <= STOP_FRACTION * budget) else 1
    if args.budget_usd is None or args.budget_usd <= 0:
        parser.error("run requires --budget-usd > 0 (experiment contract F8: no cap, no spend)")
    if proj["projected_max_usd"] > STOP_FRACTION * args.budget_usd:
        print(f"refusing to start: projected max ${proj['projected_max_usd']:.2f} exceeds {int(STOP_FRACTION * 100)} % of the ${args.budget_usd:.2f} budget")
        return 1
    ledger: dict[str, Any] = execute(manifest, runs, budget_usd=args.budget_usd, records_out=records_out, runtime_sha=args.runtime_sha)
    print(json.dumps(ledger))
    if args.baseline and args.verdict_out:
        Path(args.verdict_out).parent.mkdir(parents=True, exist_ok=True)
        return compute_verdict(records_out, Path(args.baseline), Path(args.verdict_out), runtime_sha=args.runtime_sha or "unknown", prompt_sha256=args.prompt_sha256)
    return 0


if __name__ == "__main__":
    sys.exit(main())
