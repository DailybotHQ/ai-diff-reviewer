#!/usr/bin/env python3
"""Release precondition (RFC-01 § CI wiring, BC-02): is there a current, non-blocking verdict?

`auto-release.yml` calls this before computing the version bump. It exits 0
when the newest verdict that MATCHES the release candidate is non-blocking,
and 1 with one of three reasons otherwise:

- `eval-verdict-missing`  — no verdict matches the candidate;
- `eval-verdict-stale`    — the matching verdict is older than `--max-age-days`;
- `eval-verdict-blocking` — the matching verdict lists blocking findings.

Matching rule (documented in RFC-01 / Task 7): a verdict matches when its
`candidate_content_sha256` equals SHA-256 of `scripts/reviewer.py` on the
candidate AND its `prompt_sha256` equals SHA-256 of `prompts/default.md`.
Docs-only commits after the measured commit therefore still pass — the
measured content is what matters, not the git SHA. When a verdict carries no
content hash (older verdicts), `candidate_runtime_sha` must equal the
candidate's git SHA exactly.

    python3 tests/eval/release_gate.py --verdicts tests/eval/records/verdicts \
        --runtime-sha "$GITHUB_SHA" --runtime scripts/reviewer.py --prompt prompts/default.md
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

DEFAULT_MAX_AGE_DAYS: int = 30
REASON_MISSING: str = "eval-verdict-missing"
REASON_STALE: str = "eval-verdict-stale"
REASON_BLOCKING: str = "eval-verdict-blocking"
REASON_ERROR: str = "eval-gate-error"          # the gate itself could not run (exit 2, never a silent skip)
EXIT_GATE_ERROR: int = 2


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def load_verdicts(verdicts_dir: Path) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    if not verdicts_dir.is_dir():
        return out
    for path in sorted(verdicts_dir.glob("*.json")):
        try:
            data: Any = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            continue
        if isinstance(data, dict) and data.get("schema_version") == "verdict/1.0":
            data["_file"] = path.name
            out.append(data)
    return out


def _parse_time(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def matches(verdict: dict[str, Any], *, runtime_sha: str, content_sha256: str, prompt_sha256: str) -> bool:
    if verdict.get("prompt_sha256") != prompt_sha256:
        return False
    content: Any = verdict.get("candidate_content_sha256")
    if content:
        return content == content_sha256
    return bool(runtime_sha) and verdict.get("candidate_runtime_sha") == runtime_sha


def evaluate(
    verdicts: list[dict[str, Any]], *, runtime_sha: str, content_sha256: str, prompt_sha256: str,
    now: datetime | None = None, max_age_days: int = DEFAULT_MAX_AGE_DAYS,
) -> tuple[bool, str, str]:
    """Return `(ok, reason, detail)`; `reason` is empty when ok."""
    now = now or datetime.now(timezone.utc)
    matching: list[dict[str, Any]] = [v for v in verdicts if matches(v, runtime_sha=runtime_sha, content_sha256=content_sha256, prompt_sha256=prompt_sha256)]
    if not matching:
        return False, REASON_MISSING, f"no verdict matches runtime content {content_sha256[:12]} + prompt {prompt_sha256[:12]} (checked {len(verdicts)} verdict(s))"
    matching.sort(key=lambda v: _parse_time(v.get("computed_at")) or datetime.min.replace(tzinfo=timezone.utc))
    newest: dict[str, Any] = matching[-1]
    computed: datetime | None = _parse_time(newest.get("computed_at"))
    if computed is None or (now - computed).days > max_age_days:
        return False, REASON_STALE, f"{newest.get('_file')}: computed_at {newest.get('computed_at')!r} is older than {max_age_days} days"
    blocking: list[str] = list(newest.get("blocking") or [])
    if blocking:
        return False, REASON_BLOCKING, f"{newest.get('_file')}: " + "; ".join(blocking[:3])
    return True, "", f"{newest.get('_file')}: non-blocking, computed {newest.get('computed_at')}"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--verdicts", required=True)
    parser.add_argument("--runtime-sha", default="")
    parser.add_argument("--runtime", default="scripts/reviewer.py")
    parser.add_argument("--prompt", default="prompts/default.md")
    parser.add_argument("--max-age-days", type=int, default=DEFAULT_MAX_AGE_DAYS)
    args = parser.parse_args(argv)
    try:
        ok, reason, detail = evaluate(
            load_verdicts(Path(args.verdicts)), runtime_sha=args.runtime_sha,
            content_sha256=sha256_file(Path(args.runtime)), prompt_sha256=sha256_file(Path(args.prompt)),
            max_age_days=args.max_age_days,
        )
    except Exception as exc:  # noqa: BLE001 — "the gate could not run" must not read as "the gate said no"
        print(f"release-gate: {REASON_ERROR} — {type(exc).__name__}: {exc}")
        return EXIT_GATE_ERROR
    print(("release-gate: OK — " if ok else f"release-gate: {reason} — ") + detail)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
