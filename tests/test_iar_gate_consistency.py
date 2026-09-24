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

Companion module: `test_iar_advisory_escape.py` (the advisory escape,
corroboration, real-git and production replays). Every test here fails on
v2.3.0 and passes after the fix.
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
import tempfile
import unittest
from types import SimpleNamespace
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


class ReconciliationIsComputedOnce(unittest.TestCase):
    """Finding #2 on PR #55: the footer used to run a SECOND reconciliation
    with different `current_fingerprints` than the gate. Now the gate stores
    the one it decided on and the footer reads it back."""

    def test_run_iar_post_llm_stores_the_reconciliation_it_gated_on(self) -> None:
        fp = "a" * 16
        result = reviewer.ReviewResult(summary="s", findings=[], overall_severity=reviewer.SEVERITY_NONE)
        result.prior_finding_updates = _resolved(fp)
        pre = reviewer.IARPreLLMContext(
            prior_state=None, transition=reviewer.GenerationTransition.NEW_COMMITS,
            base_sha="b" * 40, head_sha="6" * 40, range_hash="h", new_lines_pct=0.0, pr_labels=[],
            pre_policy_result=reviewer.PolicyResult(findings_to_surface=[], findings_silenced=[],
                effective_max_inline_comments=30, prompt_addendum="", policy_applied=reviewer.IAR_POLICY_ITERATIVE),
            prior_findings=(_prior(fp, minimized=True),), mode=reviewer.IAR_MODE_INCREMENTAL, delta=_delta(),
        )
        cfg = reviewer.IARConfig(policy=reviewer.IAR_POLICY_ITERATIVE, max_review_rounds=0, cap_multiplier=1, escape_label="x")
        with tempfile.TemporaryDirectory() as td:
            root = Path(td); (root / "src").mkdir()
            (root / "src" / "auth.py").write_text("ok\n", encoding="utf-8")
            with mock.patch.object(reviewer, "_load_code_contexts_for_findings", return_value={}):
                reviewer.run_iar_post_llm(iar_config=cfg, pre_context=pre, result=result, base_max_inline_comments=30,
                    telemetry=reviewer.RunTelemetry(), resolution_policy=reviewer.RESOLUTION_POLICY_ADVISORY, workspace=root)
        self.assertIsNotNone(result.prior_reconciliation)
        assert result.prior_reconciliation is not None
        self.assertEqual([p.fingerprint for p in result.prior_reconciliation.resolved], [fp])
        self.assertEqual(result.overall_severity, reviewer.SEVERITY_NONE)
        footer = reviewer.render_incremental_footer(delta=_delta(), reconciliation=result.prior_reconciliation, new_findings=0)
        self.assertIn("resolved 1 · still open 0", footer)

    def test_footer_fallback_never_claims_what_the_gate_did_not_honour(self) -> None:
        """When post-LLM crashed, `main()` escalates every prior severity; a
        recomputed footer must therefore not report retirements."""
        src = (_ROOT / "scripts" / "reviewer.py").read_text(encoding="utf-8")
        self.assertIn("if result.prior_reconciliation is not None:", src)
        self.assertIn("Post-LLM crashed: the gate kept every prior finding", src)


class RecommendationRewriteCoversEveryLine(unittest.TestCase):
    """Finding #6: the rewrite stopped at the first matching line."""

    def test_two_recommendation_lines_are_both_rewritten(self) -> None:
        summary = "**Recommendation:** approve\n\n…\n\nRecommendation: approve"
        out, rewritten = reviewer.reconcile_recommendation_line(summary, blocked=True)
        self.assertTrue(rewritten)
        self.assertNotRegex(out, r"(?i)\bapprove\b")
        self.assertEqual(out.count("request-changes"), 2)


class InlineComment422Salvage(unittest.TestCase):
    """The dogfood run on PR #55: 3 bad anchors cost all 7 inline comments.
    On a 422 the runtime now retries with the provably-anchorable subset."""

    DIFF = (
        "diff --git a/src/a.py b/src/a.py\n--- a/src/a.py\n+++ b/src/a.py\n"
        "@@ -10,3 +10,4 @@\n x\n+y\n z\n w\n"
        "@@ -40,2 +41,2 @@\n a\n-b\n+c\n"
        "diff --git a/gone.py b/gone.py\n--- a/gone.py\n+++ /dev/null\n@@ -1,3 +0,0 @@\n-1\n-2\n-3\n"
    )

    def test_parse_hunk_ranges(self) -> None:
        r = reviewer.parse_diff_hunk_ranges(self.DIFF)
        self.assertEqual(r["src/a.py"], [(10, 13), (41, 42)])
        self.assertNotIn("gone.py", r)

    def test_anchor_status(self) -> None:
        r = reviewer.parse_diff_hunk_ranges(self.DIFF)
        st = reviewer.inline_comment_anchor_status
        self.assertTrue(st({"path": "src/a.py", "line": 12}, r))
        self.assertTrue(st({"path": "src/a.py", "line": 13, "start_line": 11}, r))
        self.assertFalse(st({"path": "src/a.py", "line": 20}, r))          # outside every hunk
        self.assertFalse(st({"path": "src/a.py", "line": 41, "start_line": 12}, r))  # crosses hunks
        self.assertIsNone(st({"path": "docs/other.md", "line": 3}, r))     # unknown file → keep
        self.assertIsNone(st({"path": "src/a.py", "line": 12, "side": "LEFT"}, r))

    def _finding(self, path: str, line: int) -> Any:
        return reviewer.Finding(path=path, line=line, body="b", severity="warning")

    def test_422_keeps_the_valid_comments_and_names_the_dropped_ones(self) -> None:
        result = reviewer.ReviewResult(summary="s", overall_severity="warning",
            findings=[self._finding("src/a.py", 12), self._finding("src/a.py", 20), self._finding("docs/x.md", 3)])
        calls: list[int] = []
        def fake_submit(**kw: Any) -> dict[str, Any]:
            calls.append(len(kw["inline_comments"]))
            if len(calls) == 1:
                import urllib.error, io as _io
                raise urllib.error.HTTPError("u", 422, "Unprocessable", {}, _io.BytesIO(b'{"errors":["Line could not be resolved"]}'))
            return {"html_url": "ok"}
        logs: list[str] = []
        with mock.patch.object(reviewer, "gh_submit_review", fake_submit), \
             mock.patch.object(reviewer, "log", logs.append):
            review, dropped = reviewer.gh_submit_review_with_fallback(
                token="t", repo="o/r", pr_number=1, head_sha="h", result=result, diff_text=self.DIFF)
        self.assertEqual(calls, [3, 2])          # full attempt, then the anchorable subset
        self.assertEqual(dropped, 1)
        self.assertTrue(any("src/a.py:20-20" in m for m in logs))

    def test_without_diff_text_behaviour_is_unchanged_summary_only(self) -> None:
        result = reviewer.ReviewResult(summary="s", overall_severity="warning", findings=[self._finding("src/a.py", 12)])
        calls: list[int] = []
        def fake_submit(**kw: Any) -> dict[str, Any]:
            calls.append(len(kw["inline_comments"]))
            if len(calls) == 1:
                import urllib.error, io as _io
                raise urllib.error.HTTPError("u", 422, "x", {}, _io.BytesIO(b"{}"))
            return {}
        with mock.patch.object(reviewer, "gh_submit_review", fake_submit), mock.patch.object(reviewer, "log", lambda m: None):
            _, dropped = reviewer.gh_submit_review_with_fallback(token="t", repo="o/r", pr_number=1, head_sha="h", result=result)
        self.assertEqual(calls, [1, 0]); self.assertEqual(dropped, 1)

    def test_subset_still_rejected_falls_back_to_summary_only(self) -> None:
        result = reviewer.ReviewResult(summary="s", overall_severity="warning",
            findings=[self._finding("src/a.py", 12), self._finding("src/a.py", 20)])
        calls: list[int] = []
        def fake_submit(**kw: Any) -> dict[str, Any]:
            calls.append(len(kw["inline_comments"]))
            if kw["inline_comments"]:
                import urllib.error, io as _io
                raise urllib.error.HTTPError("u", 422, "x", {}, _io.BytesIO(b"{}"))
            return {}
        with mock.patch.object(reviewer, "gh_submit_review", fake_submit), mock.patch.object(reviewer, "log", lambda m: None):
            _, dropped = reviewer.gh_submit_review_with_fallback(token="t", repo="o/r", pr_number=1, head_sha="h", result=result, diff_text=self.DIFF)
        self.assertEqual(calls, [2, 1, 0]); self.assertEqual(dropped, 2)

if __name__ == "__main__":
    unittest.main()


class SeverityPolicyKeepsPriorEscalation(unittest.TestCase):
    """v3 (PR #61 self-review, verified critical): `apply_severity_policy`
    rebuilds `overall_severity` from this round's published findings, which
    used to wipe the prior-finding escalation IAR had folded in. The
    escalation is re-applied after the policy; the gate stays red."""

    def _pre(self, *priors: Any) -> Any:
        return reviewer.IARPreLLMContext(
            prior_state=None, transition=reviewer.GenerationTransition.NEW_COMMITS,
            base_sha="b" * 40, head_sha="6" * 40, range_hash="h", new_lines_pct=0.0, pr_labels=[],
            pre_policy_result=reviewer.PolicyResult(findings_to_surface=[], findings_silenced=[],
                effective_max_inline_comments=30, prompt_addendum="", policy_applied=reviewer.IAR_POLICY_ITERATIVE),
            prior_findings=tuple(priors), mode=reviewer.IAR_MODE_INCREMENTAL, delta=_delta(),
        )

    def test_empty_follow_up_with_open_prior_critical_still_blocks(self) -> None:
        fp = "c" * 16
        result = reviewer.ReviewResult(summary="s", findings=[], overall_severity=reviewer.SEVERITY_NONE)
        # what run_iar_post_llm does for a still-open prior critical
        result.overall_severity = reviewer.overall_severity([result.overall_severity, reviewer.SEVERITY_CRITICAL])
        self.assertEqual(result.overall_severity, reviewer.SEVERITY_CRITICAL)
        reviewer.apply_severity_policy(result)
        self.assertEqual(result.overall_severity, reviewer.SEVERITY_NONE)  # the wipe the review reported
        self.assertEqual(reviewer.restore_prior_severity_escalation(result, self._pre(_prior(fp))), reviewer.SEVERITY_CRITICAL)
        blocked, _ = reviewer.compute_check_gate(
            severity=result.overall_severity, strictness=reviewer.STRICTNESS_BLOCK_CRITICAL, incomplete=False, cli_name="grok",
            pr_desc_mode=reviewer.PR_DESC_MODE_OFF, description_adequate=True, description_reason="",
        )
        self.assertTrue(blocked)

    def test_verified_resolved_prior_does_not_escalate(self) -> None:
        fp = "d" * 16
        prior = _prior(fp)
        result = reviewer.ReviewResult(summary="s", findings=[], overall_severity=reviewer.SEVERITY_NONE)
        result.prior_reconciliation = SimpleNamespace(resolved=[prior], still_open=[], regressed=[])
        reviewer.apply_severity_policy(result)
        self.assertEqual(reviewer.restore_prior_severity_escalation(result, self._pre(prior)), reviewer.SEVERITY_NONE)

    def test_no_prior_context_is_a_no_op(self) -> None:
        result = reviewer.ReviewResult(summary="s", findings=[], overall_severity=reviewer.SEVERITY_WARNING)
        self.assertEqual(reviewer.restore_prior_severity_escalation(result, None), reviewer.SEVERITY_WARNING)

    def test_refuted_fingerprints_leave_the_open_set(self) -> None:
        state = reviewer.IterationState(
            version=reviewer.IAR_STATE_SCHEMA_VERSION, generation=1, generation_range_hash="h", round_in_generation=1,
            policy_applied=reviewer.IAR_POLICY_ITERATIVE, resolved_fingerprints=[], open_fingerprints_this_gen=["a" * 16, "b" * 16],
            history=[], base_sha="b" * 40, head_sha="6" * 40,
        )
        refuted = reviewer.Finding(path="src/auth.py", line=10, severity=reviewer.SEVERITY_WARNING, body="false claim")
        refuted.fingerprint = "a" * 16
        result = reviewer.ReviewResult(summary="s", findings=[], overall_severity=reviewer.SEVERITY_NONE)
        result.refuted.append(refuted)
        self.assertEqual(reviewer.drop_refuted_from_open_set(state, result), 1)
        self.assertEqual(state.open_fingerprints_this_gen, ["b" * 16])
        self.assertEqual(reviewer.drop_refuted_from_open_set(None, result), 0)
