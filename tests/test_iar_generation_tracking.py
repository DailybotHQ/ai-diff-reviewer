#!/usr/bin/env python3
"""Unit tests for the Iteration-Aware Review (IAR) generation-tracking
layer — `GenerationTransition` enum, `compute_generation_range_hash`,
`detect_generation_change`, `advance_generation`,
`increment_round_in_generation`.

The generation model is the load-bearing correctness guarantee for the
"new commits after convergence" scenario the user raised: when the diff
content window changes, we MUST advance the generation so downstream
policies (Tasks 6-7) re-activate `first-pass-exhaustive` on the fresh
content — otherwise new critical findings in new code could be silenced.

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


def _make_state(**overrides: Any) -> "reviewer.IterationState":
    base: dict[str, Any] = {
        "version": reviewer.IAR_STATE_SCHEMA_VERSION,
        "generation": 1,
        "generation_range_hash": "hash-abc-1234",
        "round_in_generation": 1,
        "policy_applied": reviewer.IAR_POLICY_ITERATIVE,
        "resolved_fingerprints": [],
        "open_fingerprints_this_gen": [],
        "history": [],
        "base_sha": "base-sha-1",
    }
    base.update(overrides)
    return reviewer.IterationState(**base)


class GenerationTransitionEnumTests(unittest.TestCase):
    """The enum values are consumed as strings (logs, marker debug
    output), so their spelling matters."""

    def test_all_transition_values_present(self) -> None:
        vals: set[str] = {t.value for t in reviewer.GenerationTransition}
        self.assertEqual(
            vals,
            {
                "first_review",
                "same_generation",
                "new_commits",
                "rebased",
                "user_forced_reset",
            },
        )

    def test_is_string_enum(self) -> None:
        t: reviewer.GenerationTransition = (
            reviewer.GenerationTransition.NEW_COMMITS
        )
        # Instances double as strings for easy comparison in logs.
        self.assertEqual(str(t.value), "new_commits")


class DetectGenerationChangeTests(unittest.TestCase):
    """Exercises every branch of the classifier."""

    def test_first_review_when_prior_none(self) -> None:
        got: reviewer.GenerationTransition = (
            reviewer.detect_generation_change(
                prior_state=None,
                current_range_hash="x",
                current_base_sha="y",
            )
        )
        self.assertEqual(got, reviewer.GenerationTransition.FIRST_REVIEW)

    def test_same_generation_when_hash_matches(self) -> None:
        prior: reviewer.IterationState = _make_state(
            generation_range_hash="hash-stable",
            base_sha="base-sha-1",
        )
        got: reviewer.GenerationTransition = (
            reviewer.detect_generation_change(
                prior_state=prior,
                current_range_hash="hash-stable",
                current_base_sha="base-sha-1",
            )
        )
        self.assertEqual(
            got, reviewer.GenerationTransition.SAME_GENERATION
        )

    def test_new_commits_when_hash_differs_but_base_same(self) -> None:
        prior: reviewer.IterationState = _make_state(
            generation_range_hash="old-hash",
            base_sha="base-sha-1",
        )
        got: reviewer.GenerationTransition = (
            reviewer.detect_generation_change(
                prior_state=prior,
                current_range_hash="new-hash",
                current_base_sha="base-sha-1",
            )
        )
        self.assertEqual(got, reviewer.GenerationTransition.NEW_COMMITS)

    def test_rebased_when_base_sha_differs(self) -> None:
        prior: reviewer.IterationState = _make_state(
            generation_range_hash="old-hash",
            base_sha="old-base",
        )
        got: reviewer.GenerationTransition = (
            reviewer.detect_generation_change(
                prior_state=prior,
                current_range_hash="new-hash",
                current_base_sha="rebased-base",
            )
        )
        self.assertEqual(got, reviewer.GenerationTransition.REBASED)

    def test_missing_prior_base_sha_falls_back_to_new_commits(self) -> None:
        """Backward-compat: prior IAR version didn't persist base_sha →
        rebase detection is impossible → default to NEW_COMMITS. This
        is the SAFE fallback: extra exhaustive review, never silent
        silencing."""
        prior: reviewer.IterationState = _make_state(
            generation_range_hash="old-hash",
            base_sha="",  # older marker didn't persist this.
        )
        got: reviewer.GenerationTransition = (
            reviewer.detect_generation_change(
                prior_state=prior,
                current_range_hash="new-hash",
                current_base_sha="whatever-base",
            )
        )
        self.assertEqual(got, reviewer.GenerationTransition.NEW_COMMITS)

    def test_empty_current_range_hash_never_matches(self) -> None:
        """When git-diff fails and we can't compute a current hash, we
        force NEW_COMMITS (never SAME_GENERATION on unknown state)."""
        prior: reviewer.IterationState = _make_state(
            generation_range_hash="",
            base_sha="b",
        )
        got: reviewer.GenerationTransition = (
            reviewer.detect_generation_change(
                prior_state=prior,
                current_range_hash="",
                current_base_sha="b",
            )
        )
        self.assertEqual(got, reviewer.GenerationTransition.NEW_COMMITS)


class AdvanceGenerationTests(unittest.TestCase):
    """`advance_generation` is called on every non-SAME_GENERATION
    transition. It's the state mutator that resets the round counter,
    preserves resolved fingerprints, and appends a history entry."""

    def test_first_review_creates_gen_1_round_1(self) -> None:
        got: reviewer.IterationState = reviewer.advance_generation(
            prior_state=None,
            transition=reviewer.GenerationTransition.FIRST_REVIEW,
            new_range_hash="hash-1",
            new_base_sha="base-1",
            policy=reviewer.IAR_POLICY_ITERATIVE,
        )
        self.assertEqual(got.generation, 1)
        self.assertEqual(got.round_in_generation, 1)
        self.assertEqual(got.generation_range_hash, "hash-1")
        self.assertEqual(got.base_sha, "base-1")
        self.assertEqual(got.history, [])
        self.assertEqual(got.resolved_fingerprints, [])
        self.assertEqual(got.open_fingerprints_this_gen, [])

    def test_new_commits_increments_generation(self) -> None:
        prior: reviewer.IterationState = _make_state(
            generation=3, round_in_generation=5,
            generation_range_hash="old-h", base_sha="b1",
        )
        got: reviewer.IterationState = reviewer.advance_generation(
            prior_state=prior,
            transition=reviewer.GenerationTransition.NEW_COMMITS,
            new_range_hash="new-h",
            new_base_sha="b1",
            policy=reviewer.IAR_POLICY_ITERATIVE,
        )
        self.assertEqual(got.generation, 4)
        self.assertEqual(got.round_in_generation, 1)
        self.assertEqual(got.generation_range_hash, "new-h")

    def test_new_commits_resets_round_to_1(self) -> None:
        prior: reviewer.IterationState = _make_state(round_in_generation=8)
        got: reviewer.IterationState = reviewer.advance_generation(
            prior_state=prior,
            transition=reviewer.GenerationTransition.NEW_COMMITS,
            new_range_hash="h", new_base_sha="b",
            policy="iterative",
        )
        self.assertEqual(got.round_in_generation, 1)

    def test_advance_preserves_resolved_fingerprints(self) -> None:
        """resolved_fingerprints crosses generations — audit trail +
        cross-gen dedup for `critical-gate` policy."""
        prior: reviewer.IterationState = _make_state(
            resolved_fingerprints=["fp-A", "fp-B", "fp-C"],
        )
        got: reviewer.IterationState = reviewer.advance_generation(
            prior_state=prior,
            transition=reviewer.GenerationTransition.NEW_COMMITS,
            new_range_hash="h", new_base_sha="b",
            policy="iterative",
        )
        self.assertEqual(
            got.resolved_fingerprints, ["fp-A", "fp-B", "fp-C"]
        )

    def test_advance_resets_open_fingerprints_this_gen(self) -> None:
        """open_fingerprints_this_gen is scoped to the current
        generation → must reset when a new generation begins."""
        prior: reviewer.IterationState = _make_state(
            open_fingerprints_this_gen=["fp-still-open"],
        )
        got: reviewer.IterationState = reviewer.advance_generation(
            prior_state=prior,
            transition=reviewer.GenerationTransition.NEW_COMMITS,
            new_range_hash="h", new_base_sha="b",
            policy="iterative",
        )
        self.assertEqual(got.open_fingerprints_this_gen, [])

    def test_advance_appends_history_entry(self) -> None:
        prior: reviewer.IterationState = _make_state(
            generation=2,
            round_in_generation=3,
            generation_range_hash="prev-h",
            open_fingerprints_this_gen=[],
        )
        got: reviewer.IterationState = reviewer.advance_generation(
            prior_state=prior,
            transition=reviewer.GenerationTransition.NEW_COMMITS,
            new_range_hash="new-h", new_base_sha="new-b",
            policy="iterative",
        )
        self.assertEqual(len(got.history), 1)
        entry: dict[str, Any] = got.history[0]
        self.assertEqual(entry["gen"], 2)
        self.assertEqual(entry["range_hash"], "prev-h")
        self.assertEqual(entry["rounds_ran"], 3)
        self.assertTrue(entry["converged"])
        # Task 8 populates the actual numbers; scaffolding is 0.
        self.assertEqual(entry["tokens_used"], 0)
        self.assertEqual(entry["wall_clock_ms"], 0)

    def test_converged_marked_false_when_open_fingerprints_exist(self) -> None:
        prior: reviewer.IterationState = _make_state(
            open_fingerprints_this_gen=["fp-still-open"],
        )
        got: reviewer.IterationState = reviewer.advance_generation(
            prior_state=prior,
            transition=reviewer.GenerationTransition.NEW_COMMITS,
            new_range_hash="h", new_base_sha="b",
            policy="iterative",
        )
        self.assertFalse(got.history[0]["converged"])

    def test_rebased_transition_advances_generation(self) -> None:
        """REBASED and NEW_COMMITS are treated identically for the
        state-mutation semantics; the distinction lives in policy
        selection + logging."""
        prior: reviewer.IterationState = _make_state(generation=5)
        got: reviewer.IterationState = reviewer.advance_generation(
            prior_state=prior,
            transition=reviewer.GenerationTransition.REBASED,
            new_range_hash="h-post-rebase",
            new_base_sha="new-base",
            policy="iterative",
        )
        self.assertEqual(got.generation, 6)
        self.assertEqual(got.base_sha, "new-base")


class IncrementRoundInGenerationTests(unittest.TestCase):
    """SAME_GENERATION path: no fingerprint reset, no history append,
    just a round counter bump."""

    def test_increments_round_by_one(self) -> None:
        prior: reviewer.IterationState = _make_state(round_in_generation=3)
        got: reviewer.IterationState = (
            reviewer.increment_round_in_generation(
                prior_state=prior, policy="iterative"
            )
        )
        self.assertEqual(got.round_in_generation, 4)

    def test_preserves_generation_and_hash(self) -> None:
        prior: reviewer.IterationState = _make_state(
            generation=7, generation_range_hash="hash-stable",
        )
        got: reviewer.IterationState = (
            reviewer.increment_round_in_generation(
                prior_state=prior, policy="iterative"
            )
        )
        self.assertEqual(got.generation, 7)
        self.assertEqual(got.generation_range_hash, "hash-stable")

    def test_preserves_fingerprints(self) -> None:
        prior: reviewer.IterationState = _make_state(
            resolved_fingerprints=["r1"],
            open_fingerprints_this_gen=["o1", "o2"],
        )
        got: reviewer.IterationState = (
            reviewer.increment_round_in_generation(
                prior_state=prior, policy="iterative"
            )
        )
        self.assertEqual(got.resolved_fingerprints, ["r1"])
        self.assertEqual(got.open_fingerprints_this_gen, ["o1", "o2"])

    def test_preserves_history(self) -> None:
        history: list[dict[str, Any]] = [{"gen": 1, "converged": True}]
        prior: reviewer.IterationState = _make_state(history=history)
        got: reviewer.IterationState = (
            reviewer.increment_round_in_generation(
                prior_state=prior, policy="iterative"
            )
        )
        self.assertEqual(got.history, history)

    def test_updates_policy_applied(self) -> None:
        """The active policy can change mid-generation (e.g. safety net
        overrides) — the round-increment path reflects that."""
        prior: reviewer.IterationState = _make_state(
            policy_applied="iterative",
        )
        got: reviewer.IterationState = (
            reviewer.increment_round_in_generation(
                prior_state=prior, policy="first-pass-exhaustive",
            )
        )
        self.assertEqual(got.policy_applied, "first-pass-exhaustive")


class ComputeGenerationRangeHashTests(unittest.TestCase):
    """`compute_generation_range_hash` shells out to `git diff` and
    hashes stdout. Tests use mocking to keep runs fast and hermetic."""

    def test_deterministic_same_diff_same_hash(self) -> None:
        completed_stub: subprocess.CompletedProcess[str] = (
            subprocess.CompletedProcess(
                args=[], returncode=0, stdout="diff --git a/x b/x\n+foo\n"
            )
        )
        with patch.object(
            subprocess, "run", return_value=completed_stub
        ):
            a: str = reviewer.compute_generation_range_hash(
                base_sha="b", head_sha="h"
            )
            b: str = reviewer.compute_generation_range_hash(
                base_sha="b", head_sha="h"
            )
        self.assertEqual(a, b)
        self.assertEqual(len(a), 16)

    def test_different_diffs_produce_different_hashes(self) -> None:
        with patch.object(
            subprocess, "run",
            return_value=subprocess.CompletedProcess(
                args=[], returncode=0, stdout="one",
            ),
        ):
            a: str = reviewer.compute_generation_range_hash(
                base_sha="b", head_sha="h"
            )
        with patch.object(
            subprocess, "run",
            return_value=subprocess.CompletedProcess(
                args=[], returncode=0, stdout="two",
            ),
        ):
            b: str = reviewer.compute_generation_range_hash(
                base_sha="b", head_sha="h"
            )
        self.assertNotEqual(a, b)

    def test_empty_base_sha_returns_empty_string(self) -> None:
        self.assertEqual(
            reviewer.compute_generation_range_hash(
                base_sha="", head_sha="h"
            ),
            "",
        )

    def test_empty_head_sha_returns_empty_string(self) -> None:
        self.assertEqual(
            reviewer.compute_generation_range_hash(
                base_sha="b", head_sha=""
            ),
            "",
        )

    def test_git_error_returns_empty_string(self) -> None:
        """A missing ref (network hiccup, sparse checkout) MUST NOT
        crash the reviewer. Empty string → callers fall back to
        FIRST_REVIEW (safe: extra work, no silencing)."""
        with patch.object(
            subprocess, "run",
            side_effect=subprocess.CalledProcessError(
                returncode=128, cmd=["git", "diff"]
            ),
        ):
            got: str = reviewer.compute_generation_range_hash(
                base_sha="b", head_sha="h"
            )
        self.assertEqual(got, "")

    def test_git_not_installed_returns_empty_string(self) -> None:
        with patch.object(
            subprocess, "run", side_effect=FileNotFoundError()
        ):
            got: str = reviewer.compute_generation_range_hash(
                base_sha="b", head_sha="h"
            )
        self.assertEqual(got, "")

    def test_argv_form_never_uses_shell(self) -> None:
        """Security invariant: the subprocess call MUST use argv-list
        form. If a future refactor accidentally passes `shell=True`,
        this test surfaces it before merge."""
        captured: dict[str, Any] = {}

        def _capture(*args: Any, **kwargs: Any) -> Any:
            captured["args"] = args
            captured["kwargs"] = kwargs
            return subprocess.CompletedProcess(
                args=args, returncode=0, stdout="x"
            )

        with patch.object(subprocess, "run", side_effect=_capture):
            reviewer.compute_generation_range_hash(
                base_sha="b", head_sha="h"
            )
        self.assertFalse(
            captured["kwargs"].get("shell", False),
            "compute_generation_range_hash MUST NOT use shell=True",
        )
        cmd: Any = captured["args"][0]
        self.assertIsInstance(cmd, list)
        self.assertEqual(cmd[0], "git")

    def test_uses_three_dot_diff_not_two_dot(self) -> None:
        """Correctness invariant: the diff spec must be three-dot
        (`base...head`) so the hash mirrors the PR-visible diff pinned
        to the merge base. Two-dot (`base..head`) recomputes whenever
        `origin/<base>` advances upstream even though the PR-visible
        content is unchanged, producing false NEW_COMMITS/REBASED
        transitions and burning full exhaustive passes on label-gated
        re-reviews. See docs/ITERATION_AWARENESS.md § 4.3."""
        captured: dict[str, Any] = {}

        def _capture(*args: Any, **kwargs: Any) -> Any:
            captured["args"] = args
            return subprocess.CompletedProcess(
                args=args, returncode=0, stdout="x"
            )

        with patch.object(subprocess, "run", side_effect=_capture):
            reviewer.compute_generation_range_hash(
                base_sha="basesha1", head_sha="headsha1"
            )
        cmd: list[str] = captured["args"][0]
        # cmd should be ["git", "diff", "basesha1...headsha1"]
        self.assertEqual(cmd[:2], ["git", "diff"])
        self.assertIn(
            "...", cmd[2],
            msg=f"Diff spec must use three-dot: got {cmd[2]!r}",
        )
        self.assertNotIn(
            "..", cmd[2].replace("...", "X"),
            msg=f"Diff spec must NOT contain two-dot: got {cmd[2]!r}",
        )
        self.assertEqual(cmd[2], "basesha1...headsha1")


class GenerationRoundtripThroughMarkerTests(unittest.TestCase):
    """End-to-end: advance a generation, embed in marker, re-parse, and
    confirm base_sha survives the round trip. Ensures the new field
    added in Task 4 correctly threads through Task 3's state layer."""

    def test_base_sha_persists_across_roundtrip(self) -> None:
        state: reviewer.IterationState = reviewer.advance_generation(
            prior_state=None,
            transition=reviewer.GenerationTransition.FIRST_REVIEW,
            new_range_hash="hash-42",
            new_base_sha="deadbeefcafe",
            policy="iterative",
        )
        embedded: str = reviewer.embed_iteration_state(
            "marker body", state
        )
        parsed: reviewer.IterationState | None = (
            reviewer._parse_state_from_marker_body(embedded)
        )
        self.assertIsNotNone(parsed)
        self.assertEqual(parsed.base_sha, "deadbeefcafe")
        self.assertEqual(parsed.generation, 1)


if __name__ == "__main__":
    unittest.main()
