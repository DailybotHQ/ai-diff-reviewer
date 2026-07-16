#!/usr/bin/env python3
"""Integration tests for the remaining IAR convergence policies —
`round-capped` and `critical-gate` — plus the automatic 30% new-lines
safety net, the human escape label, and the top-level `dispatch_policy`
that ties them all together with the documented precedence:

    escape label > safety net > configured policy > fallback (iterative)

Stdlib `unittest` only.
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import patch

_ROOT: Path = Path(__file__).resolve().parent.parent
_SPEC = importlib.util.spec_from_file_location(
    "reviewer", _ROOT / "scripts" / "reviewer.py"
)
assert _SPEC is not None and _SPEC.loader is not None
reviewer = importlib.util.module_from_spec(_SPEC)
sys.modules["reviewer"] = reviewer
_SPEC.loader.exec_module(reviewer)


def _finding(
    *, line: int, severity: str = "info", body: str | None = None,
    path: str = "src/x.py",
) -> "reviewer.Finding":
    return reviewer.Finding(
        path=path,
        line=line,
        body=body if body is not None else f"finding at line {line}",
        severity=severity,
        start_line=None,
        side="RIGHT",
    )


def _state(**overrides: Any) -> "reviewer.IterationState":
    base: dict[str, Any] = {
        "version": reviewer.IAR_STATE_SCHEMA_VERSION,
        "generation": 1,
        "generation_range_hash": "hash-abc",
        "round_in_generation": 1,
        "policy_applied": reviewer.IAR_POLICY_ITERATIVE,
        "resolved_fingerprints": [],
        "open_fingerprints_this_gen": [],
        "history": [],
        "base_sha": "base-sha-1",
    }
    base.update(overrides)
    return reviewer.IterationState(**base)


def _cfg(
    *, policy: str = "iterative",
    max_rounds: int = 0, cap_multiplier: int = 3,
    escape_label: str = "full-review-please",
) -> "reviewer.IARConfig":
    return reviewer.IARConfig(
        policy=policy, max_review_rounds=max_rounds,
        cap_multiplier=cap_multiplier, escape_label=escape_label,
    )


class RoundCappedPolicyTests(unittest.TestCase):
    def test_pre_cap_behaves_like_iterative(self) -> None:
        findings: list[reviewer.Finding] = [
            _finding(line=i) for i in range(1, 6)
        ]
        got: reviewer.PolicyResult = reviewer.apply_round_capped_policy(
            findings=findings,
            prior_state=None,
            code_contexts={},
            base_max_inline_comments=10,
            max_rounds=5,
            is_round_1_of_generation=True,
        )
        self.assertEqual(len(got.findings_to_surface), 5)
        self.assertEqual(
            got.policy_applied, reviewer.IAR_POLICY_ROUND_CAPPED_PRE_CAP
        )

    def test_post_cap_silences_warnings_surfaces_criticals(self) -> None:
        """Cap of 2 rounds; we're on round 3 → only criticals surface."""
        findings: list[reviewer.Finding] = [
            _finding(line=1, severity="info"),
            _finding(line=2, severity="warning"),
            _finding(line=3, severity="critical"),
            _finding(line=4, severity="warning"),
        ]
        prior: reviewer.IterationState = _state(round_in_generation=2)
        got: reviewer.PolicyResult = reviewer.apply_round_capped_policy(
            findings=findings,
            prior_state=prior,
            code_contexts={},
            base_max_inline_comments=10,
            max_rounds=2,
            is_round_1_of_generation=False,
        )
        self.assertEqual(len(got.findings_to_surface), 1)
        self.assertEqual(got.findings_to_surface[0].severity, "critical")
        self.assertEqual(len(got.findings_silenced), 3)
        for silenced in got.findings_silenced:
            self.assertIn("round cap", silenced.reason)
        self.assertEqual(
            got.policy_applied, reviewer.IAR_POLICY_ROUND_CAPPED_POST_CAP
        )

    def test_zero_max_rounds_means_unlimited(self) -> None:
        """max_rounds=0 → post-cap never triggers, behaves like iterative
        forever."""
        findings: list[reviewer.Finding] = [
            _finding(line=i, severity="info") for i in range(1, 11)
        ]
        prior: reviewer.IterationState = _state(
            round_in_generation=100,
        )
        got: reviewer.PolicyResult = reviewer.apply_round_capped_policy(
            findings=findings,
            prior_state=prior,
            code_contexts={},
            base_max_inline_comments=10,
            max_rounds=0,
            is_round_1_of_generation=False,
        )
        self.assertEqual(len(got.findings_to_surface), 10)
        self.assertEqual(
            got.policy_applied, reviewer.IAR_POLICY_ROUND_CAPPED_PRE_CAP
        )

    def test_critical_always_surfaces_even_post_cap(self) -> None:
        """Post-cap critical still surfaces — even with the round cap
        exceeded, criticals are never silenced."""
        crit: reviewer.Finding = _finding(line=1, severity="critical")
        prior: reviewer.IterationState = _state(round_in_generation=5)
        got: reviewer.PolicyResult = reviewer.apply_round_capped_policy(
            findings=[crit],
            prior_state=prior,
            code_contexts={},
            base_max_inline_comments=10,
            max_rounds=2,
            is_round_1_of_generation=False,
        )
        self.assertEqual(got.findings_to_surface, [crit])

    def test_new_generation_resets_round_counter_even_if_prior_past_cap(
        self,
    ) -> None:
        """Regression test: when a transition fires (NEW_COMMITS,
        REBASED, USER_FORCED_RESET, or FIRST_REVIEW),
        `is_round_1_of_generation=True` MUST force the round counter to
        restart at 1 for the new generation — otherwise a consumer with
        e.g. `max_rounds=3` who pushed new commits after a converged
        generation would land in the post-cap path on run 1 of the new
        generation and see all non-critical findings silenced.

        Pre-fix behaviour: `current_round = prior_state.round_in_generation + 1
        = 5 + 1 = 6 > 3 = max_rounds` → post-cap fires, warnings
        silenced. New-fix behaviour: `is_round_1_of_generation=True` →
        `current_round = 1` → pre-cap fires, warnings surface.
        """
        findings: list[reviewer.Finding] = [
            _finding(line=1, severity="warning"),
            _finding(line=2, severity="info"),
            _finding(line=3, severity="warning"),
        ]
        # Simulate a converged generation past the cap (round 5, max 3).
        prior_past_cap: reviewer.IterationState = _state(
            round_in_generation=5,
        )
        got: reviewer.PolicyResult = reviewer.apply_round_capped_policy(
            findings=findings,
            prior_state=prior_past_cap,
            code_contexts={},
            base_max_inline_comments=10,
            max_rounds=3,
            is_round_1_of_generation=True,  # NEW_COMMITS transition
        )
        self.assertEqual(
            got.policy_applied,
            reviewer.IAR_POLICY_ROUND_CAPPED_PRE_CAP,
            msg="is_round_1_of_generation=True must reset the round "
                "counter and land in pre-cap, not post-cap, on the "
                "first round of the new generation.",
        )
        self.assertEqual(
            len(got.findings_to_surface), 3,
            msg="All findings must surface on the round-1 restart "
                "(subject to dedup, which is a no-op here since "
                "prior_state has no fingerprints from the new gen).",
        )
        self.assertEqual(len(got.findings_silenced), 0)


class CriticalGatePolicyTests(unittest.TestCase):
    def test_all_severities_surface_when_no_dedup_match(self) -> None:
        findings: list[reviewer.Finding] = [
            _finding(line=1, severity="info"),
            _finding(line=2, severity="warning"),
            _finding(line=3, severity="critical"),
        ]
        got: reviewer.PolicyResult = reviewer.apply_critical_gate_policy(
            findings=findings,
            prior_state=None,
            code_contexts={},
            base_max_inline_comments=10,
        )
        self.assertEqual(len(got.findings_to_surface), 3)
        self.assertEqual(
            got.policy_applied, reviewer.IAR_POLICY_CRITICAL_GATE
        )

    def test_resolved_fingerprints_silenced_across_generations(self) -> None:
        """The load-bearing behavior of critical-gate: previously
        resolved non-critical findings are silenced when they re-appear
        (strict cross-gen dedup)."""
        info: reviewer.Finding = _finding(line=1, severity="info")
        fp_info: str = reviewer.finding_fingerprint(
            finding=info, code_context=None
        )
        state: reviewer.IterationState = _state(
            resolved_fingerprints=[fp_info],
            generation=3,  # cross-generation scenario
        )
        got: reviewer.PolicyResult = reviewer.apply_critical_gate_policy(
            findings=[info],
            prior_state=state,
            code_contexts={},
            base_max_inline_comments=10,
        )
        self.assertEqual(got.findings_to_surface, [])
        self.assertEqual(len(got.findings_silenced), 1)
        self.assertIn(
            "cross-generation", got.findings_silenced[0].reason
        )

    def test_critical_bypasses_strict_cross_gen_dedup(self) -> None:
        """Critical severity ALWAYS surfaces via the hardcoded safety
        rail, even when strict_cross_gen would silence it. This is the
        load-bearing safety guarantee of the whole IAR subsystem."""
        crit: reviewer.Finding = _finding(line=1, severity="critical")
        fp_crit: str = reviewer.finding_fingerprint(
            finding=crit, code_context=None
        )
        state: reviewer.IterationState = _state(
            resolved_fingerprints=[fp_crit],
        )
        got: reviewer.PolicyResult = reviewer.apply_critical_gate_policy(
            findings=[crit],
            prior_state=state,
            code_contexts={},
            base_max_inline_comments=10,
        )
        self.assertEqual(got.findings_to_surface, [crit])


class SafetyNetTests(unittest.TestCase):
    def test_triggers_on_new_commits_above_threshold(self) -> None:
        self.assertTrue(
            reviewer.should_force_exhaustive_via_safety_net(
                transition=reviewer.GenerationTransition.NEW_COMMITS,
                new_lines_pct=50.0,
            )
        )

    def test_triggers_on_rebased_above_threshold(self) -> None:
        self.assertTrue(
            reviewer.should_force_exhaustive_via_safety_net(
                transition=reviewer.GenerationTransition.REBASED,
                new_lines_pct=30.0,
            )
        )

    def test_does_not_trigger_at_exactly_below_threshold(self) -> None:
        self.assertFalse(
            reviewer.should_force_exhaustive_via_safety_net(
                transition=reviewer.GenerationTransition.NEW_COMMITS,
                new_lines_pct=29.9,
            )
        )

    def test_does_not_trigger_on_same_generation(self) -> None:
        self.assertFalse(
            reviewer.should_force_exhaustive_via_safety_net(
                transition=reviewer.GenerationTransition.SAME_GENERATION,
                new_lines_pct=99.0,
            )
        )

    def test_does_not_trigger_on_first_review(self) -> None:
        """First reviews already run exhaustive when policy is
        `first-pass-exhaustive`; the safety net is specifically about
        NEW_COMMITS / REBASED where a prior converged state could
        silence new critical findings without this override."""
        self.assertFalse(
            reviewer.should_force_exhaustive_via_safety_net(
                transition=reviewer.GenerationTransition.FIRST_REVIEW,
                new_lines_pct=99.0,
            )
        )

    def test_custom_threshold(self) -> None:
        self.assertTrue(
            reviewer.should_force_exhaustive_via_safety_net(
                transition=reviewer.GenerationTransition.NEW_COMMITS,
                new_lines_pct=60.0,
                threshold_pct=50,
            )
        )
        self.assertFalse(
            reviewer.should_force_exhaustive_via_safety_net(
                transition=reviewer.GenerationTransition.NEW_COMMITS,
                new_lines_pct=45.0,
                threshold_pct=50,
            )
        )


class ComputeNewLinesPctTests(unittest.TestCase):
    def _stub(self, stdout: str) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(
            args=[], returncode=0, stdout=stdout
        )

    def test_zero_when_no_shas(self) -> None:
        self.assertEqual(
            reviewer.compute_new_lines_pct(
                prior_base_sha="", prior_head_sha="",
                current_base_sha="", current_head_sha="",
            ),
            0.0,
        )

    def test_zero_when_git_fails(self) -> None:
        with patch.object(
            subprocess, "run",
            side_effect=subprocess.CalledProcessError(
                returncode=128, cmd=["git"]
            ),
        ):
            got: float = reviewer.compute_new_lines_pct(
                prior_base_sha="b", prior_head_sha="h1",
                current_base_sha="b", current_head_sha="h2",
            )
        self.assertEqual(got, 0.0)

    def test_percentage_calculation(self) -> None:
        """Total added=80 removed=20 → total=100. New added since prior
        head = 40. 40/100 = 40%."""
        stubs: list[str] = [
            "80\t20\tsrc/a.py\n",  # total diff current
            "40\t5\tsrc/a.py\n",   # new-since-prior-head
        ]
        call_index: dict[str, int] = {"i": 0}

        def _fake_run(*args: Any, **kwargs: Any) -> Any:
            i: int = call_index["i"]
            call_index["i"] += 1
            return self._stub(stubs[i])

        with patch.object(subprocess, "run", side_effect=_fake_run):
            got: float = reviewer.compute_new_lines_pct(
                prior_base_sha="b", prior_head_sha="h1",
                current_base_sha="b", current_head_sha="h2",
            )
        self.assertAlmostEqual(got, 40.0, delta=0.01)

    def test_handles_binary_dash_lines(self) -> None:
        """`--numstat` uses "-" for binary files → skip cleanly."""
        stubs: list[str] = [
            "-\t-\tbinary.png\n50\t10\tsrc/a.py\n",
            "20\t0\tsrc/a.py\n-\t-\tbinary.png\n",
        ]
        idx: dict[str, int] = {"i": 0}

        def _fake_run(*args: Any, **kwargs: Any) -> Any:
            i: int = idx["i"]
            idx["i"] += 1
            return self._stub(stubs[i])

        with patch.object(subprocess, "run", side_effect=_fake_run):
            got: float = reviewer.compute_new_lines_pct(
                prior_base_sha="b", prior_head_sha="h1",
                current_base_sha="b", current_head_sha="h2",
            )
        # total = 50+10 = 60; new_added = 20. 20/60 = ~33.33.
        self.assertAlmostEqual(got, 33.33, delta=0.02)


class CheckEscapeLabelTests(unittest.TestCase):
    def test_returns_true_when_present(self) -> None:
        self.assertTrue(
            reviewer.check_escape_label(
                pr_labels=["needs-review", "full-review-please"],
                escape_label="full-review-please",
            )
        )

    def test_returns_false_when_absent(self) -> None:
        self.assertFalse(
            reviewer.check_escape_label(
                pr_labels=["needs-review"],
                escape_label="full-review-please",
            )
        )

    def test_returns_false_on_empty_label_string(self) -> None:
        self.assertFalse(
            reviewer.check_escape_label(
                pr_labels=["full-review-please"], escape_label="",
            )
        )


class DispatchPolicyPrecedenceTests(unittest.TestCase):
    """Precedence (highest → lowest):
    USER_FORCED_RESET transition > escape label > safety net > configured policy.
    """

    def test_user_forced_reset_beats_escape_label(self) -> None:
        """Both gestures applied simultaneously (user removed reviewed
        label AND added escape label) → reset wins. Reset is the more
        forceful gesture (DISCARDS state, whereas escape label only
        bypasses dedup for one run with state preserved); when both are
        active the user's intent is "start clean" so we defer to reset
        semantics and skip the escape-label short-circuit.

        Under `first-pass-exhaustive` (the shipped default), a
        USER_FORCED_RESET run becomes a fresh round-1 with the
        multiplied cap and exhaustive prompt addendum — the same
        outcome the escape label would produce, but without silently
        preserving stale fingerprint memory.
        """
        findings: list[reviewer.Finding] = [
            _finding(line=i) for i in range(1, 5)
        ]
        got: reviewer.PolicyResult = reviewer.dispatch_policy(
            iar_config=_cfg(policy="first-pass-exhaustive"),
            findings=findings,
            # By this point in the pipeline, `run_iar_pre_llm` has
            # already cleared prior_state to None on USER_FORCED_RESET —
            # dispatch_policy just sees the transition value.
            prior_state=None,
            code_contexts={},
            base_max_inline_comments=10,
            transition=reviewer.GenerationTransition.USER_FORCED_RESET,
            new_lines_pct=0.0,
            pr_labels=["full-review-please"],  # escape label ALSO present
        )
        self.assertEqual(
            got.policy_applied,
            reviewer.IAR_POLICY_FIRST_PASS_EXHAUSTIVE,
            msg="USER_FORCED_RESET must fall through to the configured "
                "policy's exhaustive path — the escape-label "
                "short-circuit MUST be skipped to preserve the reset's "
                "state-discard semantics.",
        )
        self.assertEqual(len(got.findings_to_surface), 4)

    def test_escape_label_beats_safety_net(self) -> None:
        """Even with safety-net threshold exceeded AND label present, the
        escape label wins → surfaces all, policy_applied=escape."""
        findings: list[reviewer.Finding] = [
            _finding(line=i) for i in range(1, 5)
        ]
        got: reviewer.PolicyResult = reviewer.dispatch_policy(
            iar_config=_cfg(policy="iterative"),
            findings=findings,
            prior_state=_state(),
            code_contexts={},
            base_max_inline_comments=10,
            transition=reviewer.GenerationTransition.NEW_COMMITS,
            new_lines_pct=99.0,
            pr_labels=["full-review-please"],
        )
        self.assertEqual(len(got.findings_to_surface), 4)
        self.assertEqual(
            got.policy_applied,
            reviewer.IAR_POLICY_ESCAPE_LABEL_FORCED,
        )

    def test_safety_net_beats_configured_iterative_policy(self) -> None:
        """Configured `iterative` + safety net triggered → safety net
        forces `first-pass-exhaustive`. policy_applied reflects the
        override."""
        findings: list[reviewer.Finding] = [
            _finding(line=i) for i in range(1, 11)
        ]
        got: reviewer.PolicyResult = reviewer.dispatch_policy(
            iar_config=_cfg(policy="iterative"),
            findings=findings,
            prior_state=_state(),
            code_contexts={},
            base_max_inline_comments=5,
            transition=reviewer.GenerationTransition.NEW_COMMITS,
            new_lines_pct=50.0,
            pr_labels=[],
        )
        self.assertEqual(
            got.policy_applied, reviewer.IAR_POLICY_SAFETY_NET_FORCED
        )
        # Cap was multiplied via first-pass-exhaustive path.
        self.assertEqual(got.effective_max_inline_comments, 15)  # 5*3
        self.assertNotEqual(got.prompt_addendum, "")

    def test_configured_iterative_used_when_no_override(self) -> None:
        got: reviewer.PolicyResult = reviewer.dispatch_policy(
            iar_config=_cfg(policy="iterative"),
            findings=[_finding(line=1)],
            prior_state=None,
            code_contexts={},
            base_max_inline_comments=10,
            transition=reviewer.GenerationTransition.FIRST_REVIEW,
            new_lines_pct=0.0,
            pr_labels=[],
        )
        self.assertEqual(
            got.policy_applied, reviewer.IAR_POLICY_ITERATIVE
        )

    def test_configured_first_pass_exhaustive_no_override(self) -> None:
        got: reviewer.PolicyResult = reviewer.dispatch_policy(
            iar_config=_cfg(policy="first-pass-exhaustive"),
            findings=[_finding(line=i) for i in range(1, 15)],
            prior_state=None,
            code_contexts={},
            base_max_inline_comments=5,
            transition=reviewer.GenerationTransition.FIRST_REVIEW,
            new_lines_pct=0.0,
            pr_labels=[],
        )
        self.assertEqual(
            got.policy_applied,
            reviewer.IAR_POLICY_FIRST_PASS_EXHAUSTIVE,
        )
        self.assertEqual(got.effective_max_inline_comments, 15)  # 5*3

    def test_configured_round_capped_no_override(self) -> None:
        prior: reviewer.IterationState = _state(round_in_generation=5)
        got: reviewer.PolicyResult = reviewer.dispatch_policy(
            iar_config=_cfg(policy="round-capped", max_rounds=2),
            findings=[_finding(line=1, severity="warning")],
            prior_state=prior,
            code_contexts={},
            base_max_inline_comments=10,
            transition=reviewer.GenerationTransition.SAME_GENERATION,
            new_lines_pct=0.0,
            pr_labels=[],
        )
        # Round 6, cap of 2 → post-cap; warning silenced.
        self.assertEqual(
            got.policy_applied,
            reviewer.IAR_POLICY_ROUND_CAPPED_POST_CAP,
        )
        self.assertEqual(got.findings_to_surface, [])

    def test_configured_critical_gate_no_override(self) -> None:
        got: reviewer.PolicyResult = reviewer.dispatch_policy(
            iar_config=_cfg(policy="critical-gate"),
            findings=[_finding(line=1, severity="info")],
            prior_state=None,
            code_contexts={},
            base_max_inline_comments=10,
            transition=reviewer.GenerationTransition.FIRST_REVIEW,
            new_lines_pct=0.0,
            pr_labels=[],
        )
        self.assertEqual(
            got.policy_applied, reviewer.IAR_POLICY_CRITICAL_GATE
        )

    def test_state_not_mutated_when_escape_fires(self) -> None:
        """Escape label surfaces all but MUST NOT mutate persisted
        state. Caller confirms by observing that a subsequent normal
        dispatch reads the same prior_state and behaves normally."""
        state: reviewer.IterationState = _state(
            round_in_generation=3,
            open_fingerprints_this_gen=["fp1", "fp2"],
        )
        # First call fires escape label.
        _ = reviewer.dispatch_policy(
            iar_config=_cfg(policy="iterative"),
            findings=[_finding(line=1)],
            prior_state=state,
            code_contexts={},
            base_max_inline_comments=10,
            transition=reviewer.GenerationTransition.SAME_GENERATION,
            new_lines_pct=0.0,
            pr_labels=["full-review-please"],
        )
        # `state` is a frozen dataclass in Task 4? No — it's regular.
        # Assertion: same object, unchanged.
        self.assertEqual(state.round_in_generation, 3)
        self.assertEqual(
            state.open_fingerprints_this_gen, ["fp1", "fp2"]
        )


if __name__ == "__main__":
    unittest.main()
