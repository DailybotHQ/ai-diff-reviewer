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


class CollapseRunsBeforePriorFindingsAreRead(unittest.TestCase):
    """The escape only works because `collapse-previous` has ALREADY minimized
    the threads by the time `fetch_prior_findings` reads them.

    If someone reorders `main()` so the IAR pre-context is built before the
    collapse step, every prior finding would be read with `is_minimized=False`
    and the escape would silently stop firing — the check would go back to
    being un-unblockable. Lock the ordering.
    """

    def test_main_collapses_before_building_the_iar_pre_context(self) -> None:
        src = (_ROOT / "scripts" / "reviewer.py").read_text(encoding="utf-8")
        collapse_call = src.index("            gh_collapse_previous_reviews(")
        pre_llm_call = src.index("        iar_pre_context = run_iar_pre_llm(")
        self.assertLess(
            collapse_call,
            pre_llm_call,
            "collapse-previous must run before prior findings are read, or "
            "PriorFinding.is_minimized is always False",
        )

    def test_collapse_targets_inline_review_comments(self) -> None:
        """Minimizing only review bodies would leave inline threads live."""
        src = (_ROOT / "scripts" / "reviewer.py").read_text(encoding="utf-8")
        self.assertIn('for ic in inline:', src)
        self.assertIn('if not ic.get("isMinimized", False):', src)


class AutoRetiredIsVisibleInTheFooter(unittest.TestCase):
    """A green check nobody signed off on must still be traceable."""

    def test_footer_names_the_auto_retirement(self) -> None:
        pf = _prior("a" * 16, minimized=True)
        rec = reviewer.PriorFindingReconciliation(
            resolved=[pf], still_open=[], regressed=[], unverified=[], auto_retired=[pf]
        )
        footer = reviewer.render_incremental_footer(
            delta=_delta(), reconciliation=rec, new_findings=0
        )
        self.assertIn("resolved 1", footer)
        self.assertIn("1 auto-retired", footer)
        self.assertIn("thread already collapsed", footer)

    def test_verified_policy_retirement_is_not_labelled_auto(self) -> None:
        """`verified` replies on and resolves the thread, so it is not the
        silent path — it must not carry the auto-retired note."""
        fp = "b" * 16
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "src").mkdir()
            (root / "src" / "auth.py").write_text("ok\n", encoding="utf-8")
            rec = reviewer.reconcile_prior_findings(
                prior_findings=[_prior(fp, minimized=True)],
                updates=_resolved(fp),
                current_fingerprints=set(),
                delta=_delta(),
                workspace=root,
                policy=reviewer.RESOLUTION_POLICY_VERIFIED,
            )
        self.assertEqual([p.fingerprint for p in rec.resolved], [fp])
        self.assertEqual(rec.auto_retired, [])

    def test_advisory_retirement_is_labelled_auto(self) -> None:
        fp = "c" * 16
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "src").mkdir()
            (root / "src" / "auth.py").write_text("ok\n", encoding="utf-8")
            rec = reviewer.reconcile_prior_findings(
                prior_findings=[_prior(fp, minimized=True)],
                updates=_resolved(fp),
                current_fingerprints=set(),
                delta=_delta(),
                workspace=root,
                policy=reviewer.RESOLUTION_POLICY_ADVISORY,
            )
        self.assertEqual([p.fingerprint for p in rec.auto_retired], [fp])


class RealWorldRegression_ApiServices7987(unittest.TestCase):
    """The exact production scenario, replayed from the real PR.

    DailyBot-Inc/api-services#7987, grok, block-on-critical, advisory,
    collapse-previous: round 1 at `30f1675` posted 5 findings (1 critical);
    round 2 at `66003ff` reported all 5 resolved with 0 new inline comments.

    Every input below was read off the live PR: the five fingerprints and
    severities from the inline `ai-pr-reviewer-finding` markers, the thread
    states from `reviewThreads` (all five `isMinimized: true`), and the
    changed-file set from `git diff 30f1675...66003ff`. The footer on that
    review — "5 claimed resolved but unverified" — is reachable only via a
    `resolved` verdict, which is how we know grok populated all five.

    v2.3.0 produced: resolved 0 · still open 5, severity `critical`, check
    FAILED, body "Recommendation: approve". Both halves are asserted here.
    """

    FINDINGS: tuple[tuple[str, str, str], ...] = (
        ("4b2a3c24aeabae3c", "critical", "docker/local/docker-compose.yaml"),
        ("035fed8a5d5428e1", "warning", "docker/local/docker-compose.yaml"),
        ("3b8717af7b89be10", "warning", ".github/workflows/0_pr-review.yml"),
        ("763e8abf38fc6047", "warning", "docker/local/django/entrypoint.sh"),
        ("bcf8530e3991aa70", "info", "docker/local/django/Dockerfile"),
    )
    CHANGED: tuple[str, ...] = (
        ".github/workflows/0_pr-review.yml", "AGENTS.md", "docker/local/README.md",
        "docker/local/django/Dockerfile", "docker/local/django/entrypoint.sh",
        "docker/local/docker-compose.yaml", "docker/local/herdr-api-ssh-setup.md",
        "skills-lock.json",
    )

    def _priors(self) -> list[Any]:
        return [
            reviewer.PriorFinding(
                thread_id=f"T{i}", comment_id=f"C{i}", comment_database_id=i,
                path=path, line=10, severity=sev, fingerprint=fp,
                body_excerpt="", is_outdated=True, is_minimized=True,
            )
            for i, (fp, sev, path) in enumerate(self.FINDINGS)
        ]

    def _delta(self) -> Any:
        return reviewer.IncrementalDelta(
            prior_head_sha="30f1675" + "0" * 33,
            head_sha="66003ff" + "0" * 33,
            changed_files=self.CHANGED,
            delta_ratio=0.4,
        )

    def _gate(self, priors: list[Any], retired: set[str]) -> tuple[str, bool]:
        severity = reviewer.overall_severity(
            [reviewer.SEVERITY_NONE]
            + [p.severity for p in priors if p.fingerprint not in retired]
        )
        blocked, _ = reviewer.compute_check_gate(
            severity=severity, strictness=reviewer.STRICTNESS_BLOCK_CRITICAL,
            incomplete=False, cli_name="grok",
            pr_desc_mode=reviewer.PR_DESC_MODE_OFF,
            description_adequate=True, description_reason="",
        )
        return severity, blocked

    def test_all_five_resolved_unblocks_the_check(self) -> None:
        priors = self._priors()
        updates = {
            fp: (reviewer.PRIOR_FINDING_STATUS_RESOLVED, "")
            for fp, _, _ in self.FINDINGS
        }
        with tempfile.TemporaryDirectory() as td:
            rec = reviewer.reconcile_prior_findings(
                prior_findings=priors, updates=updates,
                current_fingerprints=set(),  # 0 new inline findings that round
                delta=self._delta(), workspace=Path(td),
                policy=reviewer.RESOLUTION_POLICY_ADVISORY,
            )
        self.assertEqual(len(rec.resolved), 5)
        self.assertEqual(rec.still_open, [])
        self.assertEqual(len(rec.auto_retired), 5)
        severity, blocked = self._gate(priors, {p.fingerprint for p in rec.resolved})
        self.assertEqual(severity, reviewer.SEVERITY_NONE)
        self.assertFalse(blocked, "v2.3.0 kept this check red with no way to unblock")

    def test_the_critical_alone_still_blocks_and_corrects_the_body(self) -> None:
        """Same PR, but the critical is NOT fixed — must stay red and the
        body must stop saying approve."""
        priors = self._priors()
        updates = {
            fp: (reviewer.PRIOR_FINDING_STATUS_RESOLVED, "")
            for fp, _, _ in self.FINDINGS[1:]  # everything except the critical
        }
        with tempfile.TemporaryDirectory() as td:
            rec = reviewer.reconcile_prior_findings(
                prior_findings=priors, updates=updates,
                current_fingerprints=set(), delta=self._delta(),
                workspace=Path(td), policy=reviewer.RESOLUTION_POLICY_ADVISORY,
            )
        self.assertEqual(len(rec.resolved), 4)
        self.assertEqual(
            [p.fingerprint for p in rec.still_open], ["4b2a3c24aeabae3c"]
        )
        severity, blocked = self._gate(priors, {p.fingerprint for p in rec.resolved})
        self.assertEqual(severity, reviewer.SEVERITY_CRITICAL)
        self.assertTrue(blocked)
        body, rewritten = reviewer.reconcile_recommendation_line(
            "**Recommendation:** approve", blocked=blocked
        )
        self.assertTrue(rewritten)
        self.assertIn("request-changes", body)


class RetiredFindingDoesNotFlapBackToRed(unittest.TestCase):
    """A retired finding must not re-gate on a LATER round.

    Under `advisory` the auto-retired thread stays unresolved on GitHub, so
    `fetch_prior_findings` keeps returning it. On round 3 the delta no longer
    touches the file that was fixed in round 2, corroboration fails, and the
    finding would go back to outstanding — flipping the check green → red with
    nothing having changed.
    """

    def test_retired_fingerprints_are_dropped_open_ones_kept(self) -> None:
        retired, live = _prior("a" * 16, minimized=True), _prior("b" * 16, minimized=True)
        kept = reviewer.filter_retired_prior_findings(
            prior_findings=[retired, live],
            resolved_fingerprints=["a" * 16],
        )
        self.assertEqual([p.fingerprint for p in kept], ["b" * 16])

    def test_no_resolved_history_is_a_passthrough(self) -> None:
        priors = [_prior("a" * 16), _prior("b" * 16)]
        self.assertEqual(
            reviewer.filter_retired_prior_findings(
                prior_findings=priors, resolved_fingerprints=[]
            ),
            priors,
        )

    def test_round_three_stays_green_when_the_delta_moved_on(self) -> None:
        """Round 2 retired the finding; round 3 touches an unrelated file.
        Without the filter this re-gated and turned the check red again."""
        fp = "a" * 16
        priors = [_prior(fp, minimized=True)]  # thread still unresolved on GitHub
        gating = reviewer.filter_retired_prior_findings(
            prior_findings=priors, resolved_fingerprints=[fp]
        )
        severity = reviewer.overall_severity(
            [reviewer.SEVERITY_NONE] + [p.severity for p in gating]
        )
        blocked, _ = reviewer.compute_check_gate(
            severity=severity, strictness=reviewer.STRICTNESS_BLOCK_CRITICAL,
            incomplete=False, cli_name="grok",
            pr_desc_mode=reviewer.PR_DESC_MODE_OFF,
            description_adequate=True, description_reason="",
        )
        self.assertEqual(gating, [])
        self.assertEqual(severity, reviewer.SEVERITY_NONE)
        self.assertFalse(blocked)

    def test_filter_is_wired_into_the_pre_llm_path(self) -> None:
        src = (_ROOT / "scripts" / "reviewer.py").read_text(encoding="utf-8")
        self.assertIn("prior_findings = filter_retired_prior_findings(", src)


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
