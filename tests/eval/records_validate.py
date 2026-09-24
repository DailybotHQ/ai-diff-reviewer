#!/usr/bin/env python3
"""Offline validator for stored run records and verdicts (RFC-01 offline layer).

Checks, without any network or dependency:

- every `*.json` under `--records` that declares `run-record/3.0` validates
  against `tests/eval/schemas/run-record.schema.json` (stdlib subset walk);
- `run_id` values are unique across the tree;
- `usage_known == false` ⇒ `usage` and `cost_usd` are null (unknown is never zero);
- files are bounded (1 MB each, 1 000 per tree);
- when a `campaign.json` manifest sits beside records, every declared cell
  has at least `repetitions` completed records (`ledger.json` / `summary.json`
  written by the campaign driver are auxiliary and skipped);
- every `verdicts/*.json` validates against `verdict.schema.json`.

    python3 tests/eval/records_validate.py --records tests/eval/records
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path
from typing import Any

HERE: Path = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import schema_check  # noqa: E402  (sibling module, stdlib)

RUN_SCHEMA: Path = HERE / "schemas" / "run-record.schema.json"
VERDICT_SCHEMA: Path = HERE / "schemas" / "verdict.schema.json"
MAX_RECORD_FILE_BYTES: int = 1_000_000
MAX_RECORD_FILES: int = 1_000
AUXILIARY_FILES: frozenset[str] = frozenset({"ledger.json", "summary.json"})
RESULT_TWIN_SUFFIX: str = ".run-record.json"  # `<out>.json` + `<out>.json.run-record.json` are written together
ADJUDICATION_SCHEMA: str = "adjudication/1.0"
ADJUDICATION_VERDICTS: frozenset[str] = frozenset({"true", "false", "overstated"})
ADJUDICATION_REQUIRED: tuple[str, ...] = ("campaign_id", "adjudicator", "blind", "method", "adjudicated_at", "positive_cases", "findings", "precision")


def _validate_adjudication(path: Path, data: dict[str, Any]) -> list[str]:
    """Adjudication records (tests/eval/adjudicate.py seal): blind, every finding with a verdict, no finding bodies."""
    problems: list[str] = []
    for key in ADJUDICATION_REQUIRED:
        if key not in data:
            problems.append(f"{path}: adjudication missing {key!r}")
    if data.get("blind") is not True:
        problems.append(f"{path}: adjudication must be blind (F7)")
    for i, finding in enumerate(data.get("findings") or []):
        if not isinstance(finding, dict):
            problems.append(f"{path}: findings[{i}] is not an object")
            continue
        if finding.get("verdict") not in ADJUDICATION_VERDICTS:
            problems.append(f"{path}: findings[{i}] verdict {finding.get('verdict')!r} not in {sorted(ADJUDICATION_VERDICTS)}")
        if "body" in finding:
            problems.append(f"{path}: findings[{i}] carries the finding body — sealed records keep body_sha256 only")
    return problems


def _load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def validate_tree(records_dir: Path) -> list[str]:
    problems: list[str] = []
    if not records_dir.is_dir():
        return [f"{records_dir}: not a directory"]
    files: list[Path] = sorted(p for p in records_dir.rglob("*.json") if p.is_file())
    if len(files) > MAX_RECORD_FILES:
        return [f"{records_dir}: {len(files)} files exceeds the {MAX_RECORD_FILES} cap"]
    run_schema: dict[str, Any] = _load_json(RUN_SCHEMA)
    verdict_schema: dict[str, Any] | None = _load_json(VERDICT_SCHEMA) if VERDICT_SCHEMA.is_file() else None
    run_ids: Counter[str] = Counter()
    completed_by_cell: Counter[str] = Counter()
    manifests: list[Path] = []
    for path in files:
        if path.stat().st_size > MAX_RECORD_FILE_BYTES:
            problems.append(f"{path}: exceeds {MAX_RECORD_FILE_BYTES} bytes")
            continue
        try:
            data: Any = _load_json(path)
        except json.JSONDecodeError as exc:
            problems.append(f"{path}: invalid JSON ({exc})")
            continue
        if path.name == "campaign.json":
            manifests.append(path)
            continue
        if path.name in AUXILIARY_FILES:
            # campaign driver outputs beside the records — not run records
            continue
        if Path(str(path) + RESULT_TWIN_SUFFIX).is_file():
            # run_eval.py result payload (findings + score) kept beside its run record for adjudication
            continue
        if not isinstance(data, dict):
            problems.append(f"{path}: not an object")
            continue
        version: Any = data.get("schema_version")
        if version is None and data.get("schema") == ADJUDICATION_SCHEMA:
            problems.extend(_validate_adjudication(path, data))
            continue
        if version == "run-record/3.0":
            problems.extend(schema_check.validate(run_schema, data, label=str(path)))
            run_ids[str(data.get("run_id"))] += 1
            if data.get("usage_known") is False and (data.get("usage") is not None or data.get("cost_usd") is not None):
                problems.append(f"{path}: usage_known is false but usage/cost_usd are not null")
            if data.get("status") in ("completed", "incomplete"):
                campaign: dict[str, Any] = data.get("campaign") or {}
                if campaign.get("cell"):
                    completed_by_cell[str(campaign["cell"])] += 1
        elif version == "verdict/1.0":
            if verdict_schema is None:
                problems.append(f"{path}: verdict found but verdict schema is missing")
            else:
                problems.extend(schema_check.validate(verdict_schema, data, label=str(path)))
        else:
            problems.append(f"{path}: unknown schema_version {version!r}")
    for run_id, count in run_ids.items():
        if count > 1:
            problems.append(f"duplicate run_id {run_id!r} ({count} records)")
    for manifest_path in manifests:
        manifest: Any = _load_json(manifest_path)
        if not isinstance(manifest, dict):
            problems.append(f"{manifest_path}: manifest is not an object")
            continue
        reps: int = int(manifest.get("repetitions", 1) or 1)
        for cell in manifest.get("cells", []) or []:
            if completed_by_cell[str(cell)] < reps:
                problems.append(f"{manifest_path}: cell {cell!r} has {completed_by_cell[str(cell)]} completed record(s), manifest requires {reps}")
    return problems


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--records", required=True, help="records tree to validate")
    args = parser.parse_args(argv)
    problems: list[str] = validate_tree(Path(args.records))
    for line in problems:
        print(f"FAIL {line}")
    print(f"records_validate: {len(problems)} problem(s)")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
