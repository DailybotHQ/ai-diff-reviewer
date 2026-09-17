#!/usr/bin/env python3
"""Regression tests for the IAR gate/body consistency bug (v2.3.1).

The reported failure (consumer PR, `grok`, `block-on-critical`, advisory
policy, `collapse-previous: true`):

    round 1  → 5 findings, 1 critical, check fails correctly
    round 2  → model reports all 5 resolved, 0 new inline comments,
               review body says "approve"
               …but the check still FAILS on the prior critical, and
               `collapse-previous` has minimized the round-1 threads so no
               maintainer can resolve them to unblock.

Two defects, both covered here:

1. The review body could say `approve` while the check was red — the gate was
   evaluated only AFTER the review had been posted.
2. Under `advisory`, an outstanding prior finding could never be retired once
   its thread was collapsed, so the check could not go green after a real fix.

Every test below fails on v2.3.0 and passes after the fix.
"""

from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
import unittest
from unittest import mock
from pathlib import Path
from typing import Any

_ROOT: Path = Path(__file__).resolve().parent.parent
_SPEC = importlib.util.spec_from_file_location(
    "reviewer", _ROOT / "scripts" / "reviewer.py"
)
assert _SPEC is not None and _SPEC.loader is not None
reviewer = importlib.util.module_from_spec(_SPEC)
sys.modules["reviewer"] = reviewer
_SPEC.loader.exec_module(reviewer)


def _prior(
    fp: str,
    *,
    path: str = "src/auth.py",
    sev: str = "critical",
    minimized: bool = False,
    outdated: bool = False,
) -> Any:
    """A prior bot finding read back from a review thread."""
    return reviewer.PriorFinding(
        thread_id="T1",
        comment_id="C1",
        comment_database_id=1,
        path=path,
        line=10,
        severity=sev,
        fingerprint=fp,
        body_excerpt="Unvalidated token accepted.",
        is_outdated=outdated,
        is_minimized=minimized,
    )


def _delta(files: tuple[str, ...] = ("src/auth.py",)) -> Any:
    return reviewer.IncrementalDelta(
        prior_head_sha="3" * 40, head_sha="6" * 40, changed_files=files, delta_ratio=0.3
    )


def _resolved(fp: str) -> dict[str, tuple[str, str]]:
    return {fp: (reviewer.PRIOR_FINDING_STATUS_RESOLVED, "Fixed by validating the token.")}


class AdvisoryDeadlockEscape(unittest.TestCase):
    """(a) A corroborated fix on a COLLAPSED thread must stop gating."""

    def _reconcile(self, prior: Any, updates: dict[str, tuple[str, str]], root: Path) -> Any:
        return reviewer.reconcile_prior_findings(
            prior_findings=[prior],
            updates=updates,
            current_fingerprints=set(),  # fingerprint NOT re-emitted this round
            delta=_delta(),              # file changed since the last head
            workspace=root,
            policy=reviewer.RESOLUTION_POLICY_ADVISORY,
        )

    def test_collapsed_thread_with_corroboration_is_retired(self) -> None:
        fp = "a" * 16
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "src").mkdir()
            (root / "src" / "auth.py").write_text("ok\n", encoding="utf-8")
            rec = self._reconcile(_prior(fp, minimized=True), _resolved(fp), root)
        self.assertEqual([p.fingerprint for p in rec.resolved], [fp])
        self.assertEqual(rec.still_open, [])
        self.assertEqual(rec.unverified, [])

    def test_outdated_thread_counts_as_collapsed(self) -> None:
        fp = "b" * 16
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "src").mkdir()
            (root / "src" / "auth.py").write_text("ok\n", encoding="utf-8")
            rec = self._reconcile(_prior(fp, outdated=True), _resolved(fp), root)
        self.assertEqual([p.fingerprint for p in rec.resolved], [fp])

    def test_live_thread_keeps_strict_advisory_behaviour(self) -> None:
        """Not collapsed → a maintainer can still resolve it, so advisory
        must NOT auto-retire. This is the guard on the escape hatch."""
        fp = "c" * 16
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "src").mkdir()
            (root / "src" / "auth.py").write_text("ok\n", encoding="utf-8")
            rec = self._reconcile(_prior(fp), _resolved(fp), root)
        self.assertEqual(rec.resolved, [])
        self.assertEqual([p.fingerprint for p in rec.unverified], [fp])
        self.assertEqual([p.fingerprint for p in rec.still_open], [fp])

    def test_collapsed_but_file_untouched_is_not_retired(self) -> None:
        """Corroboration is never weakened: the file must have changed (or be
        gone) even when the thread is collapsed."""
        fp = "d" * 16
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "src").mkdir()
            (root / "src" / "auth.py").write_text("ok\n", encoding="utf-8")
            rec = reviewer.reconcile_prior_findings(
                prior_findings=[_prior(fp, minimized=True)],
                updates=_resolved(fp),
                current_fingerprints=set(),
                delta=_delta(files=("README.md",)),  # auth.py untouched
                workspace=root,
                policy=reviewer.RESOLUTION_POLICY_ADVISORY,
            )
        self.assertEqual(rec.resolved, [])
        self.assertEqual([p.fingerprint for p in rec.unverified], [fp])

    def test_collapsed_but_finding_re_emitted_is_not_retired(self) -> None:
        """The model said 'resolved' but produced the same finding again."""
        fp = "e" * 16
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "src").mkdir()
            (root / "src" / "auth.py").write_text("ok\n", encoding="utf-8")
            rec = reviewer.reconcile_prior_findings(
                prior_findings=[_prior(fp, minimized=True)],
                updates=_resolved(fp),
                current_fingerprints={fp},  # re-emitted this round
                delta=_delta(),
                workspace=root,
                policy=reviewer.RESOLUTION_POLICY_ADVISORY,
            )
        self.assertEqual(rec.resolved, [])
        self.assertEqual([p.fingerprint for p in rec.unverified], [fp])


class StillOpenPriorCriticalStillBlocks(unittest.TestCase):
    """(b) A prior critical the model did NOT mark resolved keeps blocking,
    and the body must say so."""

    def test_no_verdict_stays_open_even_when_collapsed(self) -> None:
        fp = "f" * 16
        with tempfile.TemporaryDirectory() as td:
            rec = reviewer.reconcile_prior_findings(
                prior_findings=[_prior(fp, minimized=True)],
                updates={},  # model gave no verdict
                current_fingerprints=set(),
                delta=_delta(),
                workspace=Path(td),
                policy=reviewer.RESOLUTION_POLICY_ADVISORY,
            )
        self.assertEqual(rec.resolved, [])
        self.assertEqual([p.fingerprint for p in rec.still_open], [fp])

    def test_explicit_open_verdict_stays_open(self) -> None:
        fp = "0" * 16
        with tempfile.TemporaryDirectory() as td:
            rec = reviewer.reconcile_prior_findings(
                prior_findings=[_prior(fp, minimized=True)],
                updates={fp: (reviewer.PRIOR_FINDING_STATUS_OPEN, "still broken")},
                current_fingerprints=set(),
                delta=_delta(),
                workspace=Path(td),
                policy=reviewer.RESOLUTION_POLICY_ADVISORY,
            )
        self.assertEqual(rec.resolved, [])
        self.assertEqual([p.fingerprint for p in rec.still_open], [fp])

    def test_body_states_the_block_when_a_prior_critical_is_open(self) -> None:
        blocked, reason = reviewer.compute_check_gate(
            severity=reviewer.SEVERITY_CRITICAL,
            strictness=reviewer.STRICTNESS_BLOCK_CRITICAL,
            incomplete=False,
            cli_name="grok",
            pr_desc_mode=reviewer.PR_DESC_MODE_OFF,
            description_adequate=True,
            description_reason="",
        )
        self.assertTrue(blocked)
        block = reviewer.render_gate_status_block(
            blocked=blocked,
            block_reason=reason,
            severity=reviewer.SEVERITY_CRITICAL,
            strictness=reviewer.STRICTNESS_BLOCK_CRITICAL,
        )
        self.assertIn("failing", block)
        self.assertNotIn("passing", block)
        self.assertIn("block-on-critical", block)


class NewCriticalsStillBlock(unittest.TestCase):
    """The escape hatch must not weaken block-on-critical for NEW findings."""

    def test_new_critical_blocks(self) -> None:
        blocked, _ = reviewer.compute_check_gate(
            severity=reviewer.SEVERITY_CRITICAL,
            strictness=reviewer.STRICTNESS_BLOCK_CRITICAL,
            incomplete=False,
            cli_name="grok",
            pr_desc_mode=reviewer.PR_DESC_MODE_OFF,
            description_adequate=True,
            description_reason="",
        )
        self.assertTrue(blocked)

    def test_incomplete_review_never_greens_the_check(self) -> None:
        blocked, _ = reviewer.compute_check_gate(
            severity=reviewer.SEVERITY_NONE,
            strictness=reviewer.STRICTNESS_BLOCK_CRITICAL,
            incomplete=True,
            cli_name="grok",
            pr_desc_mode=reviewer.PR_DESC_MODE_OFF,
            description_adequate=True,
            description_reason="",
        )
        self.assertTrue(blocked)


class RecommendationNeverContradictsTheGate(unittest.TestCase):
    """(a) continued — never `approve` in the body with a failed check."""

    def test_approve_is_rewritten_when_blocked(self) -> None:
        summary = (
            "## Verdict\n\nPrior review items are addressed.\n\n"
            "**Recommendation:** approve\n"
        )
        out, rewritten = reviewer.reconcile_recommendation_line(summary, blocked=True)
        self.assertTrue(rewritten)
        self.assertIn("request-changes", out)
        self.assertNotIn("**Recommendation:** approve", out)

    def test_markdown_wrapper_is_preserved(self) -> None:
        out, rewritten = reviewer.reconcile_recommendation_line(
            "**Recommendation:** approve", blocked=True
        )
        self.assertTrue(rewritten)
        self.assertTrue(out.startswith("**Recommendation:** request-changes"))

    def test_untouched_when_the_gate_passes(self) -> None:
        summary = "**Recommendation:** approve"
        out, rewritten = reviewer.reconcile_recommendation_line(summary, blocked=False)
        self.assertFalse(rewritten)
        self.assertEqual(out, summary)

    def test_request_changes_is_left_alone(self) -> None:
        summary = "**Recommendation:** request-changes"
        out, rewritten = reviewer.reconcile_recommendation_line(summary, blocked=True)
        self.assertFalse(rewritten)
        self.assertEqual(out, summary)

    def test_status_block_matches_the_gate_in_both_directions(self) -> None:
        for severity, strictness, want_blocked in (
            (reviewer.SEVERITY_CRITICAL, reviewer.STRICTNESS_BLOCK_CRITICAL, True),
            (reviewer.SEVERITY_WARNING, reviewer.STRICTNESS_BLOCK_CRITICAL, False),
            (reviewer.SEVERITY_CRITICAL, reviewer.STRICTNESS_LENIENT, False),
        ):
            with self.subTest(severity=severity, strictness=strictness):
                blocked, reason = reviewer.compute_check_gate(
                    severity=severity,
                    strictness=strictness,
                    incomplete=False,
                    cli_name="grok",
                    pr_desc_mode=reviewer.PR_DESC_MODE_OFF,
                    description_adequate=True,
                    description_reason="",
                )
                self.assertEqual(blocked, want_blocked)
                block = reviewer.render_gate_status_block(
                    blocked=blocked,
                    block_reason=reason,
                    severity=severity,
                    strictness=strictness,
                )
                self.assertIn("failing" if want_blocked else "passing", block)


class AgentRunnerPriorFindingRoundTrip(unittest.TestCase):
    """(c) A grok-shaped findings.json must populate `prior_finding_updates`."""

    def _parse(self, payload: dict[str, Any]) -> Any:
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "findings.json"
            path.write_text(json.dumps(payload), encoding="utf-8")
            return reviewer.parse_findings_file(path)

    def test_grok_findings_file_populates_prior_finding_updates(self) -> None:
        fp1, fp2 = "a1b2c3d4e5f60718", "1122334455667788"
        result = self._parse(
            {
                "summary": "## Verdict\n\nPrior review items are addressed.\n\n"
                           "**Recommendation:** approve",
                "findings": [],
                "prior_findings": [
                    {"fingerprint": fp1, "status": "resolved", "note": "Token now validated."},
                    {"fingerprint": fp2, "status": "open", "note": "Still unbounded."},
                ],
            }
        )
        self.assertEqual(
            result.prior_finding_updates,
            {
                fp1: (reviewer.PRIOR_FINDING_STATUS_RESOLVED, "Token now validated."),
                fp2: (reviewer.PRIOR_FINDING_STATUS_OPEN, "Still unbounded."),
            },
        )

    def test_regressed_status_round_trips(self) -> None:
        fp = "9" * 16
        result = self._parse(
            {
                "summary": "s",
                "findings": [],
                "prior_findings": [{"fingerprint": fp, "status": "regressed"}],
            }
        )
        self.assertEqual(
            result.prior_finding_updates, {fp: (reviewer.PRIOR_FINDING_STATUS_REGRESSED, "")}
        )

    def test_end_to_end_grok_round_two_retires_the_critical(self) -> None:
        """The full reported scenario: grok writes `resolved` for a prior
        critical whose thread `collapse-previous` minimized, the file changed,
        nothing was re-emitted → the finding must stop gating."""
        fp = "a1b2c3d4e5f60718"
        result = self._parse(
            {
                "summary": "Prior review items are addressed.",
                "findings": [],
                "prior_findings": [{"fingerprint": fp, "status": "resolved", "note": "Fixed."}],
            }
        )
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "src").mkdir()
            (root / "src" / "auth.py").write_text("validated\n", encoding="utf-8")
            rec = reviewer.reconcile_prior_findings(
                prior_findings=[_prior(fp, minimized=True)],
                updates=result.prior_finding_updates,
                current_fingerprints=set(),
                delta=_delta(),
                workspace=root,
                policy=reviewer.RESOLUTION_POLICY_ADVISORY,
            )
        self.assertEqual([p.fingerprint for p in rec.resolved], [fp])
        # …and with nothing outstanding, block-on-critical passes.
        blocked, _ = reviewer.compute_check_gate(
            severity=reviewer.SEVERITY_NONE,
            strictness=reviewer.STRICTNESS_BLOCK_CRITICAL,
            incomplete=False,
            cli_name="grok",
            pr_desc_mode=reviewer.PR_DESC_MODE_OFF,
            description_adequate=True,
            description_reason="",
        )
        self.assertFalse(blocked)


class GateSeverityNoLongerEscalates(unittest.TestCase):
    """The user-visible symptom, through the real IAR post-LLM path.

    On v2.3.0 `run_iar_post_llm` folded EVERY outstanding prior severity into
    `result.overall_severity` under `advisory`, so a fixed critical kept the
    check red forever. It must now drop out once the reconciliation retires it
    — and must still escalate when the finding is genuinely open.
    """

    def _run(self, prior: Any, updates: dict[str, tuple[str, str]], root: Path) -> Any:
        result = reviewer.ReviewResult(
            summary="Prior review items are addressed.",
            findings=[],
            overall_severity=reviewer.SEVERITY_NONE,
        )
        result.prior_finding_updates = dict(updates)
        pre = reviewer.IARPreLLMContext(
            prior_state=None,
            transition=reviewer.GenerationTransition.NEW_COMMITS,
            base_sha="b" * 40,
            head_sha="6" * 40,
            range_hash="h",
            new_lines_pct=0.0,
            pr_labels=[],
            pre_policy_result=reviewer.PolicyResult(
                findings_to_surface=[],
                findings_silenced=[],
                effective_max_inline_comments=30,
                prompt_addendum="",
                policy_applied=reviewer.IAR_POLICY_ITERATIVE,
            ),
            prior_findings=(prior,),
            mode=reviewer.IAR_MODE_INCREMENTAL,
            delta=_delta(),
        )
        cfg = reviewer.IARConfig(
            policy=reviewer.IAR_POLICY_ITERATIVE,
            max_review_rounds=0,
            cap_multiplier=1,
            escape_label="full-review-please",
        )
        with mock.patch.object(reviewer, "_load_code_contexts_for_findings", return_value={}):
            state, _ = reviewer.run_iar_post_llm(
                iar_config=cfg,
                pre_context=pre,
                result=result,
                base_max_inline_comments=30,
                telemetry=reviewer.RunTelemetry(),
                resolution_policy=reviewer.RESOLUTION_POLICY_ADVISORY,
                workspace=root,
            )
        return result, state

    def _workspace(self, td: str) -> Path:
        root = Path(td)
        (root / "src").mkdir()
        (root / "src" / "auth.py").write_text("validated\n", encoding="utf-8")
        return root

    def test_retired_critical_stops_gating_and_check_goes_green(self) -> None:
        fp = "a" * 16
        with tempfile.TemporaryDirectory() as td:
            result, state = self._run(
                _prior(fp, minimized=True), _resolved(fp), self._workspace(td)
            )
        self.assertEqual(result.overall_severity, reviewer.SEVERITY_NONE)
        self.assertNotIn(fp, state.open_fingerprints_this_gen)
        self.assertIn(fp, state.resolved_fingerprints)
        blocked, _ = reviewer.compute_check_gate(
            severity=result.overall_severity,
            strictness=reviewer.STRICTNESS_BLOCK_CRITICAL,
            incomplete=False,
            cli_name="grok",
            pr_desc_mode=reviewer.PR_DESC_MODE_OFF,
            description_adequate=True,
            description_reason="",
        )
        self.assertFalse(blocked, "a corroborated fix must be able to green the check")

    def test_open_critical_still_escalates_and_still_blocks(self) -> None:
        fp = "f" * 16
        with tempfile.TemporaryDirectory() as td:
            result, state = self._run(
                _prior(fp, minimized=True), {}, self._workspace(td)
            )
        self.assertEqual(result.overall_severity, reviewer.SEVERITY_CRITICAL)
        self.assertIn(fp, state.open_fingerprints_this_gen)
        blocked, _ = reviewer.compute_check_gate(
            severity=result.overall_severity,
            strictness=reviewer.STRICTNESS_BLOCK_CRITICAL,
            incomplete=False,
            cli_name="grok",
            pr_desc_mode=reviewer.PR_DESC_MODE_OFF,
            description_adequate=True,
            description_reason="",
        )
        self.assertTrue(blocked)

    def test_live_thread_critical_still_escalates_under_advisory(self) -> None:
        """Escape hatch stays shut while a maintainer can resolve the thread."""
        fp = "c" * 16
        with tempfile.TemporaryDirectory() as td:
            result, _ = self._run(_prior(fp), _resolved(fp), self._workspace(td))
        self.assertEqual(result.overall_severity, reviewer.SEVERITY_CRITICAL)


class PriorFindingCollapsedFlag(unittest.TestCase):
    def test_is_collapsed_covers_minimized_and_outdated(self) -> None:
        self.assertFalse(_prior("1" * 16).is_collapsed)
        self.assertTrue(_prior("1" * 16, minimized=True).is_collapsed)
        self.assertTrue(_prior("1" * 16, outdated=True).is_collapsed)

    def test_graphql_query_selects_is_minimized(self) -> None:
        """`fetch_prior_findings` cannot populate the flag it never asks for."""
        src = (_ROOT / "scripts" / "reviewer.py").read_text(encoding="utf-8")
        self.assertIn("isMinimized body author { login }", src)


if __name__ == "__main__":
    unittest.main()
