#!/usr/bin/env python3
"""Pure decision policy for the Jev evaluation (PLAN Task 6).

Isolated, dependency-free, and deterministic: this module maps validated
Jev answers (and inventory facts) to review decisions. It holds NO transport
and makes NO model calls, so every rule here is unit-testable and hashed.

The policy is a versioned artifact (F3/F6): `policy.v1.json` is the shipped
bundle; `policy_hash()` is what the experiment manifest records to freeze a
calibrated policy before held-out contact (Task 6 -> Task 7 boundary).
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

POLICY_VERSION = "v1"
SCHEMA = "ai-diff-reviewer/jev-policy/1"
POLICY_FILE = Path(__file__).resolve().parent / "policy.v1.json"

SEVERITY_RANK = {"critical": 3, "warning": 2, "unknown": 1, "none": 0, "insufficient_evidence": 1}

VETO_REASONS = (
    "dependency_manifest",
    "policy_prompt_file",
    "inventory_incomplete",
    "prior_unresolved_findings",
    "exec_bit_change",
    "binary_change",
    "forced_full_review",
)


def default_policy() -> dict[str, Any]:
    """The v1 bundle. Thresholds are NAMED values from Task 5/6 calibration."""
    return {
        "schema": SCHEMA,
        "version": POLICY_VERSION,
        "thresholds": {
            "risk_confidence_floor": 0.70,
            "security_noul_floor": 0.70,
            "verification_confidence_floor": 0.60,
            "fast_pass_confidence_floor": 0.90,
        },
        "policy": {
            "on_below_floor": "abstain",  # never guess (F3)
            "on_contradiction": "insufficient_evidence",
            "critical_findings": "never_suppressed_by_verification",
        },
        "veto_always_full_review": list(VETO_REASONS),
        "evidence_rules": {
            "supported": "verification is_real >= floor and severity is not invalid",
            "contradicted": "verification is_real <= 1-floor and severity is invalid",
            "insufficient": "everything else: missing answers, low confidence, disagreement without affirmative contradiction",
        },
    }


def load_policy(path: Path = POLICY_FILE) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def policy_hash(policy: dict[str, Any]) -> str:
    canonical = json.dumps(policy, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return "policy:sha256:" + hashlib.sha256(canonical).hexdigest()


def policy_fingerprint_file(path: Path = POLICY_FILE) -> str:
    return policy_hash(json.loads(path.read_text(encoding="utf-8")))


def _floor(policy: dict[str, Any], name: str) -> float:
    return float(policy["thresholds"][name])


def map_batch(
    policy: dict[str, Any],
    questions: dict[str, dict[str, Any]],
    answers: dict[str, dict[str, Any]] | None,
) -> dict[str, dict[str, Any]]:
    """Map a validated client result to floored decisions.

    Handles the failure shapes the task names: missing answers, missing
    confidence, below-floor confidence, and contradictions between answers.
    Every decision is one of: `value` (committed), `insufficient_evidence`
    (abstained), or `contradiction` (abstained, with the conflicting pair).
    """
    answers = answers or {}
    risk_floor = _floor(policy, "risk_confidence_floor")
    decisions: dict[str, dict[str, Any]] = {}
    for name, question in questions.items():
        answer = answers.get(name)
        if not isinstance(answer, dict):
            decisions[name] = {"decision": "insufficient_evidence", "reason": "missing_answer"}
            continue
        confidence = answer.get("confidence")
        if confidence is None:
            decisions[name] = {"decision": "insufficient_evidence", "reason": "missing_confidence"}
            continue
        if confidence < _floor(policy, f"{name}_confidence_floor" if f"{name}_confidence_floor" in policy["thresholds"] else "risk_confidence_floor"):
            decisions[name] = {"decision": "insufficient_evidence",
                               "reason": "below_floor", "confidence": confidence}
            continue
        decisions[name] = {"decision": "value", "value": answer.get("value"), "confidence": confidence}
    # Contradiction pass: a high-confidence "no security relevance" alongside
    # a high-confidence security flag is not a decision — it is ambiguity.
    risk = decisions.get("risk", {})
    security = decisions.get("touches_security", {})
    if (
        risk.get("decision") == "value"
        and security.get("decision") == "value"
        and risk.get("value") == "none"
        and security.get("value") is True
    ):
        for name in ("risk", "touches_security"):
            decisions[name] = {
                "decision": "contradiction",
                "reason": "security_flag_vs_no_risk",
            }
    return decisions


def triage_route(
    policy: dict[str, Any],
    decisions: dict[str, dict[str, Any]],
    inventory: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Map triage decisions + inventory facts to a review route.

    Vetoes (F6/global guideline 5) force FULL review regardless of the
    model's confidence. Anything not cleanly "none" is not shallow.
    """
    inventory = inventory or {}
    vetoes = [v for v in VETO_REASONS if inventory.get(v)]
    risk = decisions.get("risk", {})
    if vetoes:
        return {"route": "full", "reason": "veto: " + ", ".join(vetoes), "vetoes": vetoes}
    if risk.get("decision") != "value":
        return {"route": "full", "reason": f"triage not committed: {risk.get('decision', 'missing')}",
                "vetoes": []}
    if risk.get("value") == "none":
        return {"route": "shallow", "reason": "triage clean above floor", "vetoes": []}
    return {"route": "full", "reason": f"triage risk={risk.get('value')}", "vetoes": []}


def fast_pass_eligible(
    policy: dict[str, Any],
    decisions: dict[str, dict[str, Any]],
    inventory: dict[str, Any] | None = None,
) -> tuple[bool, list[str]]:
    """SIMULATED fast-pass gate (advisory measurement only in this plan).

    Eligibility requires: no veto, triage committed clean, security noul
    confidently low, and the fast-pass confidence floor met. Any veto or
    missing piece makes it ineligible. This function NEVER authorizes
    skipping review in production — it exists so Task 6 can measure what a
    fast-pass WOULD have skipped against ground truth.
    """
    inventory = inventory or {}
    reasons: list[str] = []
    vetoes = [v for v in VETO_REASONS if inventory.get(v)]
    if vetoes:
        reasons.append("veto: " + ", ".join(vetoes))
    risk = decisions.get("risk", {})
    if risk.get("decision") != "value" or risk.get("value") != "none":
        reasons.append("triage not clean")
    security = decisions.get("touches_security", {})
    if security.get("decision") == "value" and security.get("value") is True:
        reasons.append("security surfaces touched")
    if security.get("decision") != "value":
        reasons.append("security question not committed")
    risk_floor = _floor(policy, "fast_pass_confidence_floor")
    if risk.get("decision") == "value" and float(risk.get("confidence") or 0.0) < risk_floor:
        reasons.append("below fast-pass floor")
    return (not reasons), reasons


def priority_order(
    triaged: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Non-authoritative review order: severity desc, confidence desc, stable.

    `triaged` items carry {"case": id, "risk": committed severity or
    "insufficient_evidence", "confidence": float, "touches_security": bool}.
    Ties keep input order (stable sort) — the ordering is a HINT, never a
    filter: every item is still reviewed (priorities mode, contract §Mode
    decisions).
    """
    def key(item: dict[str, Any]) -> tuple[int, float]:
        sev = SEVERITY_RANK.get(item.get("risk", "insufficient_evidence"), 1)
        security_bonus = 1 if item.get("touches_security") else 0
        return (sev + security_bonus, float(item.get("confidence") or 0.0))

    return sorted(triaged, key=key, reverse=True)


def classify_evidence(
    policy: dict[str, Any],
    verification: dict[str, Any] | None,
    finding_severity: str,
) -> dict[str, Any]:
    """Advisory verification semantics (contract: initially observe-only).

    `verification` carries {"is_real": 0..1, "severity": choice or None}.
    Critical findings are ALWAYS withheld from suppression regardless of the
    verdict. Missing/low-confidence/disagreeing-without-contradiction inputs
    are insufficient. Nothing here suppresses anything: the caller records
    the label and acts only under a separately-authorized enforcement mode.
    """
    if finding_severity == "critical":
        return {"class": "withheld_critical", "suppressable": False}
    if not isinstance(verification, dict):
        return {"class": "insufficient", "suppressable": False, "reason": "no_verification"}
    is_real = verification.get("is_real")
    severity = verification.get("severity")
    floor = _floor(policy, "verification_confidence_floor")
    if is_real is None or severity is None:
        return {"class": "insufficient", "suppressable": False, "reason": "missing_answer"}
    if not isinstance(is_real, (int, float)):
        return {"class": "insufficient", "suppressable": False, "reason": "bad_is_real"}
    if float(is_real) >= floor and severity != "invalid":
        return {"class": "supported", "suppressable": False}
    if float(is_real) <= 1.0 - floor and severity == "invalid":
        return {"class": "contradicted", "suppressable": finding_severity != "critical"}
    return {"class": "insufficient", "suppressable": False, "reason": "disagreement_without_contradiction"}


__all__ = [
    "POLICY_VERSION", "SCHEMA", "VETO_REASONS", "classify_evidence", "default_policy",
    "fast_pass_eligible", "load_policy", "map_batch", "policy_fingerprint_file",
    "policy_hash", "priority_order", "triage_route",
]
