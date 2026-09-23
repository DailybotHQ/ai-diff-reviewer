"""Severity policy (RFC-03 § Severity policy; BC-07 / BC-18):

- the publication table, row by row;
- refuted findings leave `findings` (never inline) and land in `refuted`;
- the invariant for both families: no published `critical` without
  `verification.status == verified`, including verifier error / timeout /
  off / unavailable — unless `strict-unverified-criticals` restores v2;
- the critical-always-surfaces rail keys on the CLAIMED severity (dedup,
  round cap, criticals-first sort);
- `compute_check_gate` reads the published severity.
"""

from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any

_ROOT: Path = Path(__file__).resolve().parent.parent
_SPEC = importlib.util.spec_from_file_location("reviewer", _ROOT / "scripts" / "reviewer.py")
assert _SPEC is not None and _SPEC.loader is not None
reviewer = importlib.util.module_from_spec(_SPEC)
sys.modules["reviewer"] = reviewer
_SPEC.loader.exec_module(reviewer)
reviewer.log = lambda msg: None  # type: ignore[assignment]


def _f(claimed: str, status: str, reason: str = "r", line: int = 1) -> Any:
    f = reviewer.Finding(path="a.py", line=line, body="Body text", severity=claimed)
    f.severity_claimed = claimed
    f.verification = reviewer.FindingVerification(status=status, reason=reason)
    return f


class PolicyTable(unittest.TestCase):
    def _publish(self, claimed: str, status: str, **kw: Any) -> tuple[str | None, Any]:
        result = reviewer.ReviewResult(findings=[_f(claimed, status)])
        reviewer.apply_severity_policy(result, **kw)
        return (result.findings[0].severity if result.findings else None), result

    def test_every_row_of_the_table(self) -> None:
        rows = [
            ("critical", "verified", "critical"), ("critical", "downgraded", "warning"), ("critical", "unverified", "warning"),
            ("critical", "skipped", "warning"), ("critical", "refuted", None),
            ("warning", "verified", "warning"), ("warning", "unverified", "warning"), ("warning", "skipped", "warning"),
            ("warning", "downgraded", "info"), ("warning", "refuted", None),
            ("info", "unverified", "info"), ("info", "refuted", "info"),
        ]
        for claimed, status, published in rows:
            with self.subTest(claimed=claimed, status=status):
                self.assertEqual(self._publish(claimed, status)[0], published)

    def test_downgraded_body_annotation_and_counts(self) -> None:
        result = reviewer.ReviewResult(findings=[_f("critical", "downgraded", "guard covers it"), _f("critical", "verified"), _f("warning", "refuted"), _f("critical", "unverified", "verifier error: boom")])
        counts = reviewer.apply_severity_policy(result)
        self.assertEqual(counts, {"verified": 1, "downgraded": 1, "refuted": 1, "annotated": 2})
        annotated = [f for f in result.findings if f.body.startswith(reviewer.DOWNGRADE_PREFIX)]
        self.assertEqual(len(annotated), 2)
        self.assertTrue(annotated[0].body.startswith("**Claimed critical; verifier found:** guard covers it"))
        self.assertEqual([f.severity for f in result.findings], ["warning", "critical", "warning"])
        self.assertEqual(result.overall_severity, "critical")
        self.assertEqual(len(result.refuted), 1)
        reviewer.apply_severity_policy(result)  # idempotent: one prefix, no re-refuting
        self.assertEqual(sum(f.body.count(reviewer.DOWNGRADE_PREFIX) for f in result.findings), 2)

    def test_refuted_never_reaches_the_inline_encoder(self) -> None:
        result = reviewer.ReviewResult(findings=[_f("critical", "refuted", "the guard exists", line=7), _f("warning", "verified", line=9)])
        reviewer.apply_severity_policy(result)
        inline = reviewer.findings_to_gh_inline_comments(result.findings)
        self.assertEqual([c["line"] for c in inline], [9])
        self.assertEqual([f.line for f in result.refuted], [7])
        self.assertEqual(result.refuted[0].verification.reason, "the guard exists")

    def test_strict_unverified_criticals_restores_v2_gating_with_annotation(self) -> None:
        sev, result = self._publish("critical", "unverified", strict_unverified_criticals=True)
        self.assertEqual(sev, "critical")
        self.assertTrue(result.findings[0].body.startswith(reviewer.DOWNGRADE_PREFIX))
        self.assertEqual(result.overall_severity, "critical")


class Invariant(unittest.TestCase):
    """No published critical without `verified` — both families, every failure path."""

    def _gate(self, result: Any, strictness: str = "block-on-critical", **kw: Any) -> bool:
        blocked, _ = reviewer.compute_check_gate(severity=result.overall_severity, strictness=strictness, incomplete=False, cli_name="x",
                                                 pr_desc_mode="off", description_adequate=True, description_reason="", **kw)
        return blocked

    def test_in_process_family(self) -> None:
        for status in ("unverified", "skipped", "downgraded"):
            state = reviewer.ReviewState(max_inline_comments=5)
            reviewer.execute_tool("emit_finding", {"path": "a.py", "line": 1, "body": "b", "severity": "critical"}, state)
            result = reviewer.state_to_review_result(state, stop_reason=reviewer.LOOP_STOP_SUBMITTED)
            result.findings[0].verification = reviewer.FindingVerification(status=status, reason=status)
            reviewer.apply_severity_policy(result)
            self.assertFalse(any(f.severity == "critical" for f in result.findings), status)
            self.assertFalse(self._gate(result), status)
        state = reviewer.ReviewState(max_inline_comments=5)
        reviewer.execute_tool("emit_finding", {"path": "a.py", "line": 1, "body": "b", "severity": "critical"}, state)
        result = reviewer.state_to_review_result(state, stop_reason=reviewer.LOOP_STOP_SUBMITTED)
        result.findings[0].verification = reviewer.FindingVerification(status="verified", reason="ok")
        reviewer.apply_severity_policy(result)
        self.assertTrue(self._gate(result))

    def test_agent_runner_family_incl_verifier_off_and_unavailable(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "findings.json"
            p.write_text(json.dumps({"summary": "s", "findings": [{"path": "a.py", "line": 1, "body": "b", "severity": "critical"}]}))
            for policy, provider, reason in (
                (reviewer.VerifierPolicy(enabled=False), None, "verifier off"),
                (reviewer.VerifierPolicy(), None, "no in-process backend"),
            ):
                result = reviewer.parse_findings_file(p)
                reviewer.run_verifier(result, policy=policy, provider=provider, model="", alias="", endpoint_kind="", unavailable_reason=reason, inventory=None)
                reviewer.apply_severity_policy(result)
                self.assertEqual([f.severity for f in result.findings], ["warning"], reason)
                self.assertFalse(self._gate(result), reason)
                self.assertTrue(self._gate(result, strictness="block-on-warning"), reason)
            result = reviewer.parse_findings_file(p)
            reviewer.run_verifier(result, policy=reviewer.VerifierPolicy(), provider=None, model="", alias="", endpoint_kind="", unavailable_reason="x", inventory=None)
            reviewer.apply_severity_policy(result, strict_unverified_criticals=True)
            self.assertTrue(self._gate(result), "strict mode gates on the claim")


class Rail(unittest.TestCase):
    def test_claimed_critical_sorts_first_and_is_never_silenced(self) -> None:
        downgraded = _f("critical", "downgraded"); reviewer.apply_severity_policy(reviewer.ReviewResult(findings=[downgraded]))
        self.assertEqual((downgraded.severity, downgraded.severity_claimed), ("warning", "critical"))
        plain_warning = _f("warning", "verified", line=2)
        ordered = reviewer._sort_findings_criticals_first([plain_warning, downgraded])
        self.assertIs(ordered[0], downgraded)
        self.assertTrue(reviewer.is_critical_claim(downgraded))
        self.assertFalse(reviewer.is_critical_claim(plain_warning))

    def test_dedup_rail_surfaces_a_downgraded_claim(self) -> None:
        downgraded = _f("critical", "downgraded"); reviewer.apply_severity_policy(reviewer.ReviewResult(findings=[downgraded]))
        prior_fp = reviewer.finding_fingerprint(finding=downgraded, code_context=None)
        state = reviewer.new_iteration_state(generation_range_hash="g", base_sha="b" * 40, head_sha="h" * 40, policy_applied=reviewer.IAR_POLICY_ITERATIVE)
        state.open_fingerprints_this_gen = [prior_fp]  # already known open → a plain warning would be silenced
        plain = _f("warning", "verified"); plain.body = downgraded.body
        out = reviewer.dedupe_findings_against_prior(new_findings=[downgraded], prior_state=state, code_contexts={})
        self.assertIn(downgraded, out.surfaced, "a downgraded claimed critical is never silenced")
        out_plain = reviewer.dedupe_findings_against_prior(new_findings=[plain], prior_state=state, code_contexts={})
        self.assertEqual(len(out_plain.silenced), 1, "the same finding as a plain warning IS deduped")


if __name__ == "__main__":
    unittest.main()
