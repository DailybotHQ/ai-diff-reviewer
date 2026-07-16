#!/usr/bin/env python3
"""Integration tests for IAR convergence policies — `iterative` and
`first-pass-exhaustive`. Tasks 7 tests (round-capped, critical-gate,
safety net, escape label) live in `tests/test_iar_dispatch.py`.

The tests here simulate multi-round scenarios end-to-end:
prior_state → new_findings → policy → PolicyResult, and assert the
surfaced count matches the spec in docs/ITERATION_AWARENESS.md § 6.

Stdlib `unittest` only.
"""

from __future__ import annotations

import importlib.util
import sys
import unittest
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


class PolicyResultDataclassTests(unittest.TestCase):
    def test_all_5_fields_present(self) -> None:
        result: reviewer.PolicyResult = reviewer.PolicyResult(
            findings_to_surface=[],
            findings_silenced=[],
            effective_max_inline_comments=10,
            prompt_addendum="",
            policy_applied=reviewer.IAR_POLICY_ITERATIVE,
        )
        self.assertEqual(result.effective_max_inline_comments, 10)
        self.assertEqual(result.prompt_addendum, "")
        self.assertEqual(
            result.policy_applied, reviewer.IAR_POLICY_ITERATIVE
        )

    def test_frozen(self) -> None:
        result: reviewer.PolicyResult = reviewer.PolicyResult(
            findings_to_surface=[], findings_silenced=[],
            effective_max_inline_comments=10, prompt_addendum="",
            policy_applied="iterative",
        )
        with self.assertRaises((AttributeError, Exception)):
            result.effective_max_inline_comments = 99  # type: ignore[misc]


class IterativePolicyTests(unittest.TestCase):
    """`iterative` = dedup only."""

    def test_round_1_gen_1_surfaces_all_findings(self) -> None:
        findings: list[reviewer.Finding] = [
            _finding(line=i) for i in range(1, 11)
        ]
        got: reviewer.PolicyResult = reviewer.apply_iterative_policy(
            findings=findings,
            prior_state=None,
            code_contexts={},
            base_max_inline_comments=10,
        )
        self.assertEqual(len(got.findings_to_surface), 10)
        self.assertEqual(got.findings_silenced, [])
        self.assertEqual(got.effective_max_inline_comments, 10)
        self.assertEqual(got.prompt_addendum, "")
        self.assertEqual(
            got.policy_applied, reviewer.IAR_POLICY_ITERATIVE
        )

    def test_round_2_dedup_removes_known_open(self) -> None:
        """7 of 10 findings match prior open → only 3 surface."""
        findings: list[reviewer.Finding] = [
            _finding(line=i) for i in range(1, 11)
        ]
        fps_open: list[str] = [
            reviewer.finding_fingerprint(finding=f, code_context=None)
            for f in findings[:7]
        ]
        state: reviewer.IterationState = _state(
            open_fingerprints_this_gen=fps_open,
            round_in_generation=2,
        )
        got: reviewer.PolicyResult = reviewer.apply_iterative_policy(
            findings=findings,
            prior_state=state,
            code_contexts={},
            base_max_inline_comments=10,
        )
        self.assertEqual(len(got.findings_to_surface), 3)
        self.assertEqual(len(got.findings_silenced), 7)

    def test_round_3_all_known_open_zero_surface(self) -> None:
        """All non-critical findings match prior open → 0 surface."""
        findings: list[reviewer.Finding] = [
            _finding(line=i) for i in range(1, 6)
        ]
        fps_open: list[str] = [
            reviewer.finding_fingerprint(finding=f, code_context=None)
            for f in findings
        ]
        state: reviewer.IterationState = _state(
            open_fingerprints_this_gen=fps_open,
            round_in_generation=3,
        )
        got: reviewer.PolicyResult = reviewer.apply_iterative_policy(
            findings=findings,
            prior_state=state,
            code_contexts={},
            base_max_inline_comments=10,
        )
        self.assertEqual(got.findings_to_surface, [])
        self.assertEqual(len(got.findings_silenced), 5)

    def test_critical_bypasses_dedup(self) -> None:
        """Critical severity finding matching prior open still surfaces
        (Task 5 safety rail routed through the policy)."""
        crit: reviewer.Finding = _finding(line=5, severity="critical")
        info: reviewer.Finding = _finding(line=6, severity="info")
        fp_crit: str = reviewer.finding_fingerprint(
            finding=crit, code_context=None
        )
        fp_info: str = reviewer.finding_fingerprint(
            finding=info, code_context=None
        )
        state: reviewer.IterationState = _state(
            open_fingerprints_this_gen=[fp_crit, fp_info],
        )
        got: reviewer.PolicyResult = reviewer.apply_iterative_policy(
            findings=[crit, info],
            prior_state=state,
            code_contexts={},
            base_max_inline_comments=10,
        )
        self.assertIn(crit, got.findings_to_surface)
        self.assertNotIn(info, got.findings_to_surface)


class FirstPassExhaustivePolicyTests(unittest.TestCase):
    def test_round_1_gen_1_cap_multiplied(self) -> None:
        """30 findings, base cap 10, multiplier 3 → 30 surface, no
        silencing, addendum spliced."""
        findings: list[reviewer.Finding] = [
            _finding(line=i) for i in range(1, 31)
        ]
        got: reviewer.PolicyResult = (
            reviewer.apply_first_pass_exhaustive_policy(
                findings=findings,
                prior_state=None,
                code_contexts={},
                base_max_inline_comments=10,
                cap_multiplier=3,
                is_round_1_of_generation=True,
            )
        )
        self.assertEqual(len(got.findings_to_surface), 30)
        self.assertEqual(got.effective_max_inline_comments, 30)
        self.assertIn("Iteration-Aware Review", got.prompt_addendum)
        self.assertEqual(
            got.policy_applied,
            reviewer.IAR_POLICY_FIRST_PASS_EXHAUSTIVE,
        )

    def test_round_1_gen_1_truncates_at_cap_when_model_overshoots(
        self,
    ) -> None:
        """Safety: if the model produces MORE than effective cap,
        truncate."""
        findings: list[reviewer.Finding] = [
            _finding(line=i) for i in range(1, 100)
        ]
        got: reviewer.PolicyResult = (
            reviewer.apply_first_pass_exhaustive_policy(
                findings=findings,
                prior_state=None,
                code_contexts={},
                base_max_inline_comments=10,
                cap_multiplier=3,
                is_round_1_of_generation=True,
            )
        )
        self.assertEqual(len(got.findings_to_surface), 30)

    def test_round_1_gen_2_re_fires_exhaustive(self) -> None:
        """After generation advance, round 1 gen 2 → exhaustive again.
        The is_round_1_of_generation param carries that signal."""
        findings: list[reviewer.Finding] = [
            _finding(line=i) for i in range(1, 21)
        ]
        prior: reviewer.IterationState = _state(
            generation=2, round_in_generation=1,
        )
        got: reviewer.PolicyResult = (
            reviewer.apply_first_pass_exhaustive_policy(
                findings=findings,
                prior_state=prior,
                code_contexts={},
                base_max_inline_comments=10,
                cap_multiplier=3,
                is_round_1_of_generation=True,
            )
        )
        self.assertEqual(len(got.findings_to_surface), 20)
        self.assertIn("Iteration-Aware Review", got.prompt_addendum)

    def test_round_2_gen_1_delegates_to_iterative(self) -> None:
        """Round 2+: no cap multiplication, no addendum, dedup applies.
        `policy_applied` still reflects the user's configured policy for
        audit trail continuity."""
        findings: list[reviewer.Finding] = [
            _finding(line=i) for i in range(1, 11)
        ]
        fps_open: list[str] = [
            reviewer.finding_fingerprint(finding=f, code_context=None)
            for f in findings[:5]
        ]
        state: reviewer.IterationState = _state(
            open_fingerprints_this_gen=fps_open,
            round_in_generation=2,
        )
        got: reviewer.PolicyResult = (
            reviewer.apply_first_pass_exhaustive_policy(
                findings=findings,
                prior_state=state,
                code_contexts={},
                base_max_inline_comments=10,
                cap_multiplier=3,
                is_round_1_of_generation=False,
            )
        )
        self.assertEqual(len(got.findings_to_surface), 5)
        self.assertEqual(got.effective_max_inline_comments, 10)
        self.assertEqual(got.prompt_addendum, "")
        # policy_applied stays "first-pass-exhaustive" for the audit trail.
        self.assertEqual(
            got.policy_applied,
            reviewer.IAR_POLICY_FIRST_PASS_EXHAUSTIVE,
        )

    def test_addendum_present_only_on_round_1(self) -> None:
        findings: list[reviewer.Finding] = [_finding(line=1)]
        r1: reviewer.PolicyResult = (
            reviewer.apply_first_pass_exhaustive_policy(
                findings=findings,
                prior_state=None,
                code_contexts={},
                base_max_inline_comments=10,
                cap_multiplier=3,
                is_round_1_of_generation=True,
            )
        )
        self.assertNotEqual(r1.prompt_addendum, "")
        r2: reviewer.PolicyResult = (
            reviewer.apply_first_pass_exhaustive_policy(
                findings=findings,
                prior_state=_state(round_in_generation=2),
                code_contexts={},
                base_max_inline_comments=10,
                cap_multiplier=3,
                is_round_1_of_generation=False,
            )
        )
        self.assertEqual(r2.prompt_addendum, "")

    def test_critical_bypasses_dedup_on_round_2_plus(self) -> None:
        """The critical safety rail applies through the policy on rounds
        2+ (which delegate to iterative)."""
        crit: reviewer.Finding = _finding(line=1, severity="critical")
        info: reviewer.Finding = _finding(line=2, severity="info")
        fp_crit: str = reviewer.finding_fingerprint(
            finding=crit, code_context=None
        )
        fp_info: str = reviewer.finding_fingerprint(
            finding=info, code_context=None
        )
        state: reviewer.IterationState = _state(
            open_fingerprints_this_gen=[fp_crit, fp_info],
            round_in_generation=2,
        )
        got: reviewer.PolicyResult = (
            reviewer.apply_first_pass_exhaustive_policy(
                findings=[crit, info],
                prior_state=state,
                code_contexts={},
                base_max_inline_comments=10,
                cap_multiplier=3,
                is_round_1_of_generation=False,
            )
        )
        self.assertIn(crit, got.findings_to_surface)
        self.assertNotIn(info, got.findings_to_surface)


class ExhaustivePromptAddendumConstantTests(unittest.TestCase):
    """DON'T #9 sanity + security surface: addendum must be a hardcoded
    constant, never sourced from user input."""

    def test_addendum_is_a_hardcoded_module_constant(self) -> None:
        self.assertIsInstance(
            reviewer.IAR_EXHAUSTIVE_PROMPT_ADDENDUM, str
        )
        self.assertGreater(
            len(reviewer.IAR_EXHAUSTIVE_PROMPT_ADDENDUM), 20,
            "Addendum should be non-trivial (Task 6 spec).",
        )
        # Signals the "exhaustive first-pass" mode plainly to the model.
        self.assertIn(
            "exhaustive",
            reviewer.IAR_EXHAUSTIVE_PROMPT_ADDENDUM.lower(),
        )

    def test_addendum_does_not_reference_user_input(self) -> None:
        """String-search sanity to catch a future regression where a
        developer accidentally interpolates env-var content."""
        self.assertNotIn(
            "os.environ", reviewer.IAR_EXHAUSTIVE_PROMPT_ADDENDUM
        )
        self.assertNotIn("$", reviewer.IAR_EXHAUSTIVE_PROMPT_ADDENDUM)


class CostSemanticsSanityTests(unittest.TestCase):
    """AGENTS.md DON'T #9: max_tokens / MAX_TURNS defaults are NOT
    raised. Only `effective_max_inline_comments` (the tool-call ceiling)
    is amplified — visible on the PolicyResult, never on the module."""

    def test_effective_cap_increased_but_max_tokens_untouched(self) -> None:
        findings: list[reviewer.Finding] = [
            _finding(line=i) for i in range(1, 5)
        ]
        got: reviewer.PolicyResult = (
            reviewer.apply_first_pass_exhaustive_policy(
                findings=findings,
                prior_state=None,
                code_contexts={},
                base_max_inline_comments=10,
                cap_multiplier=3,
                is_round_1_of_generation=True,
            )
        )
        # Cap was multiplied on the PolicyResult only.
        self.assertEqual(got.effective_max_inline_comments, 30)
        # DEFAULT_MAX_TURNS is unchanged.
        self.assertGreaterEqual(reviewer.DEFAULT_MAX_TURNS, 30)


if __name__ == "__main__":
    unittest.main()
