#!/usr/bin/env python3
"""Blinded, source-grounded adjudication of reviewer findings (RFC-01 § Metrics, F7).

Precision is *adjudicated-true / (adjudicated-true + adjudicated-false)* over
the findings a lane emitted on a scored set. The adjudicator must read the
code at the anchor and the fixture's ground truth — never only the finding
text — and must not know which arm produced the finding. This tool makes that
workflow mechanical and auditable, stdlib only:

    # 1. Build a blinded worksheet from campaign results (the `<out>.json`
    #    files run_eval.py wrote beside each run record).
    python3 tests/eval/adjudicate.py worksheet --results DIR --out tmp/worksheet.json [--seed 7]

    # 2. Fill `verdict` per item (true | false | overstated) and `note`, then
    #    seal it into the permanent adjudication record — the blind is lifted
    #    only here, when every item carries a verdict.
    python3 tests/eval/adjudicate.py seal --worksheet tmp/worksheet.json \
        --out tests/eval/records/adjudications/<campaign>.json --adjudicator NAME

    # 3. Precision per lane from a sealed record.
    python3 tests/eval/adjudicate.py precision --record tests/eval/records/adjudications/<campaign>.json

Ground truth is joined mechanically: a finding whose path matches a labelled
defect and whose body carries the label's keywords is pre-classified
`ground_truth: matches <label>`; a finding matching a `must_not_flag` label
is `ground_truth: must_not_flag`; everything else is `unlabelled` and needs
the adjudicator's reading of the anchor. Pre-classification is a hint, never
the verdict — the adjudicator confirms or overrides every item.

Verdicts: `true` (a real defect at the anchor), `false` (not a defect, or not
at the anchor), `overstated` (a real defect whose severity is inflated —
counts as true for precision, and is reported separately for calibration).
Items are deduplicated per (case, path, line, severity, body) so repetitions
of an identical finding are adjudicated once and the verdict applies to all.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import random
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT: Path = Path(__file__).resolve().parent.parent.parent
CASES_DIR: Path = ROOT / "tests" / "eval" / "cases"
SCHEMA_VERSION: str = "adjudication/1.0"
VERDICTS: frozenset[str] = frozenset({"true", "false", "overstated"})
TRUE_VERDICTS: frozenset[str] = frozenset({"true", "overstated"})
EXCERPT_RADIUS: int = 8  # lines of the head tree shown around the anchor
MAX_RESULT_FILES: int = 2_000


def _load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def load_results(results_dir: Path) -> list[dict[str, Any]]:
    """Every run_eval result (`<out>.json`, not the `.run-record.json` twin) with findings."""
    out: list[dict[str, Any]] = []
    files: list[Path] = sorted(p for p in results_dir.rglob("*.json")
                               if not p.name.endswith(".run-record.json") and p.name not in ("campaign.json", "ledger.json", "summary.json"))
    if len(files) > MAX_RESULT_FILES:
        raise SystemExit(f"refusing to read {len(files)} result files (> {MAX_RESULT_FILES})")
    for path in files:
        try:
            data: Any = _load_json(path)
        except (json.JSONDecodeError, OSError):
            continue
        if isinstance(data, dict) and isinstance(data.get("findings"), list) and (data.get("case") or data.get("pr") is not None):
            # PR-path results carry `pr` instead of `case`; normalise so the floor lane is adjudicable too
            # (no fixture → no excerpt, ground truth `no_case`; the adjudicator reads the PR at the anchor).
            if not data.get("case"):
                data["case"] = f"pr{data['pr']}"
            record_path: Path = Path(str(path) + ".run-record.json")
            lane: str = ""
            if record_path.is_file():
                try:
                    rec: Any = _load_json(record_path)
                    lane = f"{rec.get('provider')}|{rec.get('endpoint_kind')}|{rec.get('model')}"
                    data["_arm"] = ((rec.get("campaign") or {}).get("arm")) or ""
                except (json.JSONDecodeError, OSError):
                    pass
            data["_lane"] = lane or f"{data.get('provider')}|?|{data.get('model')}"
            data["_source"] = str(path.relative_to(ROOT)) if ROOT in path.parents else str(path)
            out.append(data)
    return out


def load_case(case_id: str) -> dict[str, Any] | None:
    path: Path = CASES_DIR / f"{case_id}.json"
    return _load_json(path) if path.is_file() else None


def _keywords_hit(label: dict[str, Any], body: str) -> bool:
    kws: list[str] = [str(k).lower() for k in label.get("keywords", [])]
    if not kws:
        return False
    low: str = body.lower()
    return all(k in low for k in kws) if label.get("all_keywords") else any(k in low for k in kws)


def classify(case: dict[str, Any] | None, finding: dict[str, Any]) -> str:
    """Mechanical join with the case labels — a hint for the adjudicator, never a verdict."""
    if case is None:
        return "no_case"
    expected: dict[str, Any] = case.get("expected") or {}
    must: set[str] = set(expected.get("must_flag") or [])
    must_not: set[str] = set(expected.get("must_not_flag") or [])
    body: str = str(finding.get("body") or "")
    path: str = str(finding.get("path") or "")
    for label in case.get("labels") or []:
        if label.get("path") and label["path"] != path:
            continue
        if _keywords_hit(label, body):
            if label.get("id") in must:
                return f"matches {label['id']}"
            if label.get("id") in must_not:
                return "must_not_flag"
    return "unlabelled"


def excerpt(case: dict[str, Any] | None, path: str, line: int | None) -> str:
    """Head-tree lines around the anchor so the adjudicator reads code, not only prose."""
    if case is None or line is None:
        return ""
    head: dict[str, str] = ((case.get("fixture") or {}).get("head") or {})
    content: str | None = head.get(path)
    if content is None:
        return "(path not in head tree)"
    lines: list[str] = content.splitlines()
    lo: int = max(0, int(line) - 1 - EXCERPT_RADIUS)
    hi: int = min(len(lines), int(line) + EXCERPT_RADIUS)
    return "\n".join(f"{i + 1:4d}{'>' if i + 1 == line else ' '} {lines[i]}" for i in range(lo, hi))


def item_id(case_id: str, finding: dict[str, Any]) -> str:
    key: str = json.dumps([case_id, finding.get("path"), finding.get("line"), finding.get("severity"), finding.get("body")], sort_keys=True)
    return hashlib.sha256(key.encode("utf-8")).hexdigest()[:16]


def _claimed_severity(finding: dict[str, Any], claimed_by_anchor: dict[str, str]) -> str:
    """The severity the reviewer claimed. The v3 severity policy publishes an
    unverified critical as a warning; the eval payload's `verification` list
    (and each `refuted` entry) carries `severity_claimed`, so the adjudicator
    judges every claim at the severity it was made."""
    claimed: Any = finding.get("severity_claimed")
    if claimed:
        return str(claimed)
    anchor: str = json.dumps([finding.get("path"), finding.get("line")], sort_keys=True)
    return claimed_by_anchor.get(anchor) or str(finding.get("severity") or "")


def build_worksheet(results: list[dict[str, Any]], *, seed: int = 7) -> dict[str, Any]:
    """Blinded items: no provider, model, arm or source — those stay in a sealed `key` the seal step re-joins."""
    items: dict[str, dict[str, Any]] = {}
    key: dict[str, dict[str, Any]] = {}
    for res in results:
        case_id: str = str(res["case"])
        case: dict[str, Any] | None = load_case(case_id)
        # v3: refuted claims (removed by the verifier) are adjudicated too, blind to
        # the verifier's verdict — that verdict only goes into the sealed key, so the
        # precision-after number can be computed on the same evidence as before.
        verdicts: dict[str, str] = {}
        claimed_by_anchor: dict[str, str] = {}
        for v in res.get("verification") or []:
            if isinstance(v, dict):
                anchor: str = json.dumps([v.get("path"), v.get("line")], sort_keys=True)
                verdicts[anchor] = str(v.get("status") or "")
                if v.get("severity_claimed"):
                    claimed_by_anchor[anchor] = str(v["severity_claimed"])
        claims: list[tuple[dict[str, Any], str]] = [(f, "published") for f in res["findings"]]
        claims += [({**rf, "severity": rf.get("severity_claimed") or rf.get("severity")}, "refuted") for rf in (res.get("refuted") or [])]
        for finding, disposition in claims:
            finding = {**finding, "severity": _claimed_severity(finding, claimed_by_anchor)}
            iid: str = item_id(case_id, finding)
            k: dict[str, Any] = key.setdefault(iid, {"lanes": [], "arms": [], "sources": [], "occurrences": 0, "dispositions": [], "verifier": []})
            k["occurrences"] += 1
            status: str = "refuted" if disposition == "refuted" else verdicts.get(json.dumps([finding.get("path"), finding.get("line")], sort_keys=True), "")
            for field, value in (("lanes", res.get("_lane", "")), ("arms", res.get("_arm", "")), ("sources", res.get("_source", "")),
                                 ("dispositions", disposition), ("verifier", status)):
                if value and value not in k[field]:
                    k[field].append(value)
            if iid in items:
                continue
            items[iid] = {
                "id": iid, "case": case_id, "path": finding.get("path"), "line": finding.get("line"),
                "severity": finding.get("severity"), "body": finding.get("body"),
                "ground_truth": classify(case, finding),
                "labels": [{"id": lb.get("id"), "severity": lb.get("severity"), "path": lb.get("path"), "defect": lb.get("defect")}
                           for lb in (case or {}).get("labels") or []],
                "excerpt": excerpt(case, str(finding.get("path") or ""), finding.get("line")),
                "verdict": None, "note": "",
            }
    order: list[str] = sorted(items)
    random.Random(seed).shuffle(order)
    return {
        "schema": f"{SCHEMA_VERSION}-worksheet", "blind": True, "seed": seed,
        "items": [items[i] for i in order], "sealed_key": key,
        "positive_cases": sorted({it["case"] for it in items.values()}),
    }


def seal(worksheet: dict[str, Any], *, adjudicator: str, campaign_id: str, notes: str = "") -> dict[str, Any]:
    missing: list[str] = [it["id"] for it in worksheet["items"] if it.get("verdict") not in VERDICTS]
    if missing:
        raise SystemExit(f"{len(missing)} item(s) without a verdict in {sorted(VERDICTS)}: {missing[:5]}")
    key: dict[str, dict[str, Any]] = worksheet.get("sealed_key") or {}
    findings: list[dict[str, Any]] = []
    for it in worksheet["items"]:
        k: dict[str, Any] = key.get(it["id"], {})
        findings.append({
            "id": it["id"], "case": it["case"], "path": it["path"], "line": it["line"], "severity": it["severity"],
            "ground_truth": it["ground_truth"], "verdict": it["verdict"], "note": it.get("note", ""),
            "lanes": k.get("lanes", []), "arms": k.get("arms", []), "occurrences": k.get("occurrences", 0),
            "dispositions": k.get("dispositions", []), "verifier": k.get("verifier", []),
            "body_sha256": hashlib.sha256(str(it.get("body") or "").encode("utf-8")).hexdigest(),
        })
    record: dict[str, Any] = {
        "schema": SCHEMA_VERSION, "campaign_id": campaign_id, "adjudicator": adjudicator, "blind": True,
        "method": "source-grounded: head-tree excerpt at the anchor + case labels read before the verdict; arm and lane hidden until seal",
        "adjudicated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "positive_cases": worksheet.get("positive_cases", []), "notes": notes,
        "findings": sorted(findings, key=lambda f: (f["case"], f["path"] or "", f["line"] or 0)),
    }
    record["precision"] = precision(record)
    return record


def precision(record: dict[str, Any]) -> dict[str, Any]:
    """Per lane and overall: adjudicated-true / (true + false); `overstated` counts as true, reported apart.

    v3 adds `per_verifier` (the verifier's verdict on the claim — `verified`,
    `refuted`, `downgraded`, `unverified`, `skipped` — against the blinded
    adjudication) and `published` / `refuted` buckets, so precision before the
    verifier (every claim) and after it (published claims only) come from one
    sealed record."""
    per_lane: dict[str, dict[str, int]] = {}
    per_verifier: dict[str, dict[str, int]] = {}
    per_disposition: dict[str, dict[str, int]] = {}
    overall: dict[str, int] = {"true": 0, "false": 0, "overstated": 0}
    for f in record["findings"]:
        overall[f["verdict"]] += 1
        for lane in f.get("lanes") or ["?"]:
            bucket: dict[str, int] = per_lane.setdefault(lane, {"true": 0, "false": 0, "overstated": 0})
            bucket[f["verdict"]] += 1
        for status in f.get("verifier") or []:
            per_verifier.setdefault(status, {"true": 0, "false": 0, "overstated": 0})[f["verdict"]] += 1
        for disposition in f.get("dispositions") or []:
            per_disposition.setdefault(disposition, {"true": 0, "false": 0, "overstated": 0})[f["verdict"]] += 1

    def ratio(b: dict[str, int]) -> float | None:
        pos: int = b["true"] + b["overstated"]
        total: int = pos + b["false"]
        return round(pos / total, 4) if total else None

    return {
        "positive_cases": len(record.get("positive_cases") or []),
        "findings": len(record["findings"]),
        "overall": {**overall, "precision": ratio(overall)},
        "per_lane": {lane: {**b, "precision": ratio(b)} for lane, b in sorted(per_lane.items())},
        "per_verifier": {status: {**b, "precision": ratio(b)} for status, b in sorted(per_verifier.items())},
        "per_disposition": {d: {**b, "precision": ratio(b)} for d, b in sorted(per_disposition.items())},
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    w = sub.add_parser("worksheet"); w.add_argument("--results", required=True); w.add_argument("--out", required=True); w.add_argument("--seed", type=int, default=7)
    s = sub.add_parser("seal"); s.add_argument("--worksheet", required=True); s.add_argument("--out", required=True)
    s.add_argument("--adjudicator", required=True); s.add_argument("--campaign-id", required=True); s.add_argument("--notes", default="")
    p = sub.add_parser("precision"); p.add_argument("--record", required=True)
    args = parser.parse_args(argv)
    if args.command == "worksheet":
        results: list[dict[str, Any]] = load_results(Path(args.results))
        ws: dict[str, Any] = build_worksheet(results, seed=args.seed)
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.out).write_text(json.dumps(ws, indent=2) + "\n", encoding="utf-8")
        gt: dict[str, int] = {}
        for it in ws["items"]:
            g: str = it["ground_truth"].split(" ")[0]
            gt[g] = gt.get(g, 0) + 1
        print(f"worksheet: {len(ws['items'])} distinct findings from {len(results)} results, "
              f"{len(ws['positive_cases'])} positive cases; pre-classification {json.dumps(gt, sort_keys=True)} → {args.out}")
        return 0
    if args.command == "seal":
        record: dict[str, Any] = seal(_load_json(Path(args.worksheet)), adjudicator=args.adjudicator, campaign_id=args.campaign_id, notes=args.notes)
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.out).write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
        print(json.dumps(record["precision"], indent=2))
        return 0
    print(json.dumps(precision(_load_json(Path(args.record))), indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
