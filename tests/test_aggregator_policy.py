"""Task 22 — RFC-04 agreement, severity resolution, legs bookkeeping and the
gating knobs: a verified critical from one leg gates; an unverified critical
claimed by three legs publishes as an annotated warning through the severity
policy; two of three legs deliver (the missing one named, `legs_total = 2`);
`require-all-legs` forces the block on the same fixture; `min-agreement`
drops lone warnings from the gate; superseded and foreign-head documents.
"""
from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path
from typing import Any

_ROOT: Path = Path(__file__).resolve().parent.parent
_SPEC = importlib.util.spec_from_file_location("reviewer", _ROOT / "scripts" / "reviewer.py")
assert _SPEC is not None and _SPEC.loader is not None
reviewer = importlib.util.module_from_spec(_SPEC)
sys.modules["reviewer"] = reviewer
_SPEC.loader.exec_module(reviewer)

HEAD = "h" * 40


def _finding(path: str, line: int, sev: str, *, status: str = "unverified", title: str = "", checks: int = 0, run_id: str = "r") -> Any:
    f = reviewer.Finding(path=path, line=line, body=f"{title or 'Issue'} at {path}:{line}", severity=sev, title=title or f"Issue {line}")
    f.severity_claimed = sev
    f.verification = reviewer.FindingVerification(status=status, reason="r")
    f.evidence.anchor_sha256 = reviewer.hashlib.sha256(f"{path}:{line}".encode()).hexdigest()[:16]
    f.evidence.checks = [{"kind": "read_anchor", "result": "supports"}] * checks
    f.origin = {"run_id": run_id, "provider": "x", "endpoint_kind": "x", "model": "m"}
    return f


def _leg(leg: str, findings: list[Any], *, status: str = "completed", recorded_at: str = "2026-09-24T00:00:00Z", head: str = HEAD, refuted: list[dict[str, Any]] | None = None, prior: list[dict[str, Any]] | None = None, narrative: str = "") -> Any:
    provider, kind, model = leg.split("|")
    return reviewer.LegDocument(leg_id=leg, provider=provider, endpoint_kind=kind, model=model, head_sha=head, recorded_at=recorded_at, status=status,
                                run_id=f"run-{leg}-{recorded_at[-3:]}", findings=findings, refuted=refuted or [], prior_findings=prior or [], narrative=narrative, cost_usd=0.1, turns=4, source=f"{leg}.json")


A, B, C = "grok|xai|grok-4.5", "claude-code|zai|glm-5.3-flash", "openai|xai|grok-4.5"


def _gate(severity: str, strictness: str) -> bool:
    blocked, _ = reviewer.compute_check_gate(severity=severity, strictness=strictness, incomplete=False, cli_name="aggregate",
                                             pr_desc_mode=reviewer.PR_DESC_MODE_OFF, description_adequate=True, description_reason="")
    return blocked


class AgreementAndSeverity(unittest.TestCase):
    def test_verified_critical_from_one_leg_gates(self) -> None:
        docs = [_leg(A, [_finding("a.py", 10, "critical", status="verified", checks=2, run_id="a1")]), _leg(B, [_finding("b.py", 5, "info", run_id="b1")]), _leg(C, [])]
        result, report = reviewer.aggregate_documents(docs, head_sha=HEAD, expected_legs=(A, B, C))
        crit = result.findings[0]
        self.assertEqual((crit.severity, crit.verification.status, crit.agreement), ("critical", "verified", {"legs_total": 3, "legs_reporting": 1, "reported_by": [A]}))
        reviewer.apply_severity_policy(result)
        decision = reviewer.apply_aggregate_gate_knobs(result, report, min_agreement=2)
        self.assertEqual(decision.severity, "critical", "criticals ignore min-agreement")
        self.assertTrue(_gate(decision.severity, reviewer.STRICTNESS_BLOCK_CRITICAL))

    def test_unverified_critical_claimed_by_three_legs_publishes_as_annotated_warning(self) -> None:
        docs = [_leg(leg, [_finding("a.py", 10, "critical", run_id=f"{leg}-1")]) for leg in (A, B, C)]
        result, report = reviewer.aggregate_documents(docs, head_sha=HEAD, expected_legs=(A, B, C))
        self.assertEqual(len(result.findings), 1)
        f = result.findings[0]
        self.assertEqual((f.severity_claimed, f.agreement["legs_reporting"], f.verification.status), ("critical", 3, "unverified"))
        report_v = reviewer.run_verifier(result, policy=reviewer.VerifierPolicy(enabled=True), provider=None, model="", alias="", endpoint_kind="", unavailable_reason="verifier unavailable", inventory=None)
        counts = reviewer.apply_severity_policy(result)
        self.assertEqual((f.severity, counts["annotated"]), ("warning", 1))
        self.assertTrue(f.body.startswith(reviewer.DOWNGRADE_PREFIX))
        self.assertFalse(_gate(reviewer.apply_aggregate_gate_knobs(result, report).severity, reviewer.STRICTNESS_BLOCK_CRITICAL), "agreement is not evidence (E-13)")
        self.assertEqual(report_v.unverified, 1)

    def test_max_claim_wins_and_the_richest_body_is_kept(self) -> None:
        rich = _finding("a.py", 10, "warning", title="Hard-coded credential", checks=3, run_id="rich"); rich.body = "A long, evidence-backed body " * 5
        thin = _finding("a.py", 12, "critical", title="Hard-coded credential in default", run_id="thin")
        result, _ = reviewer.aggregate_documents([_leg(A, [rich]), _leg(B, [thin])], head_sha=HEAD)
        f = result.findings[0]
        self.assertEqual((f.severity_claimed, f.severity), ("critical", "critical"))
        self.assertTrue(f.body.startswith("A long, evidence-backed body"))
        self.assertIn(f"Also reported by `{B}`", f.body)
        self.assertEqual([r["run_id"] for r in f.extra["reports"]], ["rich", "thin"])
        self.assertIsNone(f.fingerprint, "recomputed on the consolidated finding")


class LegsAndKnobs(unittest.TestCase):
    def test_two_of_three_legs_deliver_and_the_missing_one_is_named(self) -> None:
        docs = [_leg(A, [_finding("a.py", 1, "warning", run_id="a")]), _leg(B, [_finding("b.py", 1, "warning", run_id="b")], status="timeout")]
        result, report = reviewer.aggregate_documents(docs, head_sha=HEAD, expected_legs=(A, B, C))
        self.assertEqual((report.legs_delivered, report.legs_partial, report.legs_missing, report.legs_total), ([A], [B], [C], 1))
        self.assertEqual(len(result.findings), 2, "partial legs still contribute their findings")
        self.assertEqual(result.status, "completed")
        table = reviewer.render_aggregate_legs_table(report)
        self.assertIn("**missing: " + C, table); self.assertIn("partial: " + B, table); self.assertIn("1 delivered / 3 expected", table)
        block = report.legs_block()
        self.assertEqual([(b["leg_id"], b["delivered"], b["status"]) for b in block], [(A, True, "completed"), (B, False, "timeout"), (C, False, "missing")])
        decision = reviewer.apply_aggregate_gate_knobs(result, report, require_all_legs=True)
        self.assertIn("require-all-legs: 2 leg(s) not delivered", decision.forced_block_reason)
        self.assertEqual(reviewer.apply_aggregate_gate_knobs(result, report).forced_block_reason, "")

    def test_no_complete_leg_makes_the_result_incomplete(self) -> None:
        result, report = reviewer.aggregate_documents([_leg(A, [_finding("a.py", 1, "warning")], status="incomplete")], head_sha=HEAD, expected_legs=(A,))
        self.assertEqual((result.status, report.legs_total, result.findings[0].agreement["legs_total"]), ("incomplete", 0, 1))

    def test_min_agreement_drops_lone_warnings_from_the_gate_but_not_from_the_review(self) -> None:
        shared = [_finding("a.py", 10, "warning", title="Unbounded loop", run_id=f"s{i}") for i in range(2)]
        docs = [_leg(A, [shared[0], _finding("z.py", 3, "warning", title="Lone warning", run_id="lone")]), _leg(B, [shared[1]])]
        result, report = reviewer.aggregate_documents(docs, head_sha=HEAD, expected_legs=(A, B))
        reviewer.apply_severity_policy(result)
        self.assertEqual(len(result.findings), 2)
        d1 = reviewer.apply_aggregate_gate_knobs(result, report, min_agreement=1)
        d2 = reviewer.apply_aggregate_gate_knobs(result, report, min_agreement=2)
        self.assertEqual((d1.severity, d1.warnings_below_agreement), ("warning", 0))
        self.assertEqual((d2.severity, d2.warnings_below_agreement), ("warning", 1))  # the shared warning still gates
        lone_only, rep2 = reviewer.aggregate_documents([_leg(A, [_finding("z.py", 3, "warning", title="Lone warning")]), _leg(B, [])], head_sha=HEAD, expected_legs=(A, B))
        reviewer.apply_severity_policy(lone_only)
        self.assertEqual(reviewer.apply_aggregate_gate_knobs(lone_only, rep2, min_agreement=2).severity, "none")

    def test_superseded_and_foreign_head_documents(self) -> None:
        old = _leg(A, [_finding("a.py", 1, "info", run_id="old")], recorded_at="2026-09-24T00:00:00Z")
        new = _leg(A, [_finding("a.py", 1, "warning", run_id="new")], recorded_at="2026-09-24T01:00:00Z")
        foreign = _leg(B, [_finding("q.py", 1, "critical", status="verified")], head="g" * 40)
        result, report = reviewer.aggregate_documents([old, new, foreign], head_sha=HEAD, expected_legs=(A, B))
        self.assertEqual((report.superseded, report.ignored_other_head, report.legs_missing), ([old.source], 1, [B]))
        self.assertEqual([(f.severity, f.extra["reports"][0]["run_id"]) for f in result.findings], [("warning", "new")])

    def test_refuted_lists_and_prior_claims_are_merged(self) -> None:
        refuted = {"id": "f-x", "path": "r.py", "line": 4, "severity_claimed": "critical", "title": "Missing null guard", "reason": "guard exists", "origin": {"run_id": "a"}}
        block_a = {"retired": [{"id": "f-" + "p" * 16, "reason": "verified_fixed"}], "still_open": ["f-" + "q" * 16], "regressed": [], "unverified_claims": ["f-" + "r" * 16]}
        block_b = {"retired": [], "still_open": ["f-" + "p" * 16], "regressed": ["f-" + "r" * 16], "unverified_claims": []}
        doc_a = _leg(A, [], refuted=[refuted], narrative="short"); doc_a.prior_updates = reviewer._prior_updates_from_block(block_a, A)
        doc_b = _leg(B, [], refuted=[dict(refuted)], narrative="the longer narrative wins"); doc_b.prior_updates = reviewer._prior_updates_from_block(block_b, B)
        result, report = reviewer.aggregate_documents([doc_a, doc_b], head_sha=HEAD)
        self.assertEqual(len(result.refuted), 1)
        self.assertEqual((result.refuted[0].verification.status, result.refuted[0].verification.reason, result.refuted[0].agreement["reported_by"]), ("refuted", "guard exists", [A]))
        # the aggregate re-reconciles the legs' claims: a retired or claimed-resolved prior is a `resolved` claim, regressed wins
        self.assertEqual(result.prior_finding_updates["p" * 16][0], reviewer.PRIOR_FINDING_STATUS_RESOLVED)
        self.assertIn(A, result.prior_finding_updates["p" * 16][1])
        self.assertEqual(result.prior_finding_updates["r" * 16][0], reviewer.PRIOR_FINDING_STATUS_REGRESSED)
        self.assertNotIn("q" * 16, result.prior_finding_updates, "still open is not a claim")
        self.assertEqual(result.summary, "the longer narrative wins")
        self.assertEqual(result.findings, [])

    def test_failed_and_skipped_legs_carry_no_prior_claims(self) -> None:
        # round-3 self-review (verified warning): a leg that reviewed nothing must not retire priors
        claim = reviewer._prior_updates_from_block({"retired": [{"id": "f-" + "z" * 16, "reason": "verified_fixed"}], "still_open": [], "regressed": [], "unverified_claims": []}, A)
        failed = _leg(A, [], status="failed"); failed.prior_updates = claim
        good = _leg(B, [_finding("a.py", 1, "info", title="x")])
        result, _ = reviewer.aggregate_documents([failed, good], head_sha=HEAD, expected_legs=(A, B))
        self.assertEqual(result.prior_finding_updates, {})
        partial = _leg(A, [], status="timeout"); partial.prior_updates = claim
        result2, _ = reviewer.aggregate_documents([partial, good], head_sha=HEAD, expected_legs=(A, B))
        self.assertIn("z" * 16, result2.prior_finding_updates, "a partial leg did review and may claim")

    def test_prior_block_round_trips_from_writer_to_reader(self) -> None:
        # round-3 self-review (info): the document the runtime writes parses back into the same claims
        rec = reviewer.PriorFindingReconciliation()
        pf = lambda fp: reviewer.PriorFinding(thread_id="T", comment_id="C", comment_database_id=1, path="p.py", line=1, severity="warning", fingerprint=fp, body_excerpt="b", is_outdated=False)  # noqa: E731
        rec.resolved = [pf("1" * 16)]; rec.retired_reasons["1" * 16] = "verified_fixed"
        rec.still_open = [pf("2" * 16)]; rec.regressed = [pf("3" * 16)]; rec.unverified = [pf("4" * 16)]
        result = reviewer.ReviewResult(findings=[], summary="narrative\n\n_Since last review (a → b): resolved 1 · still open 1_")
        result.prior_reconciliation = rec
        run = reviewer.RunRecord(); run.provider, run.endpoint_kind, run.model, run.head_sha = "grok", "xai", "grok-4.5", HEAD
        ctx = reviewer.ReviewOutputContext(result=result, role="emit", narrative=result.summary)
        doc = reviewer.build_review_output(run_doc=run.to_dict(status="completed", failure_class=None), ctx=ctx)
        leg = reviewer.parse_leg_document(doc, source="rt")
        self.assertEqual({fp: st for fp, (st, _) in leg.prior_updates.items()}, {"1" * 16: "resolved", "4" * 16: "resolved", "3" * 16: "regressed"})
        self.assertEqual(leg.narrative, "narrative", "the leg's own incremental footer is stripped")

    def test_leg_document_prior_block_parses_into_claims(self) -> None:
        doc = {"schema_version": reviewer.REVIEW_OUTPUT_SCHEMA_VERSION, "run": {"provider": "grok", "endpoint_kind": "xai", "model": "grok-4.5", "status": "completed",
               "context": {"head_sha": HEAD}}, "findings": [],
               "prior_findings": {"retired": [{"id": "f-" + "a" * 16, "reason": "verified_fixed"}], "still_open": [], "regressed": ["f-" + "b" * 16], "unverified_claims": []}}
        leg = reviewer.parse_leg_document(doc, source="x")
        self.assertEqual(leg.prior_updates["a" * 16][0], "resolved"); self.assertEqual(leg.prior_updates["b" * 16][0], "regressed")
        self.assertEqual(leg.prior_findings["retired"][0]["id"], "f-" + "a" * 16)

    def test_iar_read_scope_is_the_aggregate_in_an_ensemble(self) -> None:
        self.assertEqual(reviewer.iar_read_scope("emit", "grok"), reviewer.AGGREGATE_SCOPE)
        self.assertEqual(reviewer.iar_read_scope("aggregate", "aggregate"), reviewer.AGGREGATE_SCOPE)
        self.assertEqual(reviewer.iar_read_scope("review", "grok:abc"), "grok:abc")


if __name__ == "__main__":
    unittest.main()
