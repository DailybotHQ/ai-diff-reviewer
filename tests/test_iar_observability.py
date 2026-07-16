"""Unit tests for the IAR observability layer + main() integration
scaffolding.

Scope: the pure helpers that surround the `main()` wiring. The wiring
itself is validated by (a) the failure-fallback regression suite
(`test_iar_failure_fallback.py`) locking the try/except safety contract,
and (b) the dogfood run on self-review.yml which exercises the full
end-to-end path against a real PR.

These tests focus on:
- `RunTelemetry` — wall-clock computation, defaults.
- `write_iar_outputs_populated` — writes the 5 outputs correctly and
  `write_all_outputs` composes with it under last-write-wins semantics.
- `_estimate_cost_vs_baseline` — heuristic invariants (never raises,
  monotone in cap ratio, sign correctness).
- `_render_iar_marker_annotation` — includes generation, round, policy,
  transition, surfaced count; renders the critical-silenced warning
  when applicable (should be impossible but a red-flag safety net).
- `_resolve_base_sha` — subprocess-mocked happy path + failure paths.
- `_fetch_pr_labels` — REST-mocked happy path + failure paths.
- `_load_code_contexts_for_findings` — dedups per-path, missing files
  resolve to None.
- `IterationState.head_sha` field — round-trips through parse/embed
  and defaults to empty string for older markers (backward-compat).
"""

from __future__ import annotations

import io
import os
import sys
import tempfile
import time
import unittest
from typing import Any
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from scripts import reviewer  # noqa: E402
from scripts.reviewer import (  # noqa: E402
    CodeContext,
    Finding,
    GenerationTransition,
    IAR_POLICY_CRITICAL_GATE,
    IAR_POLICY_ESCAPE_LABEL_FORCED,
    IAR_POLICY_FIRST_PASS_EXHAUSTIVE,
    IAR_POLICY_ITERATIVE,
    IAR_POLICY_ROUND_CAPPED,
    IAR_STATE_SCHEMA_VERSION,
    IARConfig,
    IARPreLLMContext,
    IterationState,
    PolicyResult,
    ReviewResult,
    RunTelemetry,
    SEVERITY_CRITICAL,
    SEVERITY_INFO,
    SEVERITY_WARNING,
    SilencedFinding,
    _estimate_cost_vs_baseline,
    _fetch_pr_labels,
    _load_code_contexts_for_findings,
    _render_iar_marker_annotation,
    _resolve_base_sha,
    embed_iteration_state,
    new_iteration_state,
    read_prior_iteration_state,
    run_iar_post_llm,
    run_iar_pre_llm,
    write_iar_outputs_populated,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _parse_github_outputs(path: str) -> dict[str, str]:
    """Parse a $GITHUB_OUTPUT file — supports both single-line
    `key=value` and multi-line HEREDOC formats. Mirrors the parser in
    test_iar_failure_fallback.py so both suites read the runtime's
    output format identically."""
    outputs: dict[str, str] = {}
    with open(path, "r", encoding="utf-8") as fh:
        lines: list[str] = fh.readlines()
    i: int = 0
    while i < len(lines):
        line: str = lines[i].rstrip("\n")
        if "<<" in line:
            key, delim = line.split("<<", 1)
            key = key.strip()
            delim = delim.strip()
            i += 1
            body_parts: list[str] = []
            while i < len(lines) and lines[i].rstrip("\n") != delim:
                body_parts.append(lines[i].rstrip("\n"))
                i += 1
            outputs[key] = "\n".join(body_parts)
            i += 1  # skip the terminator line
        elif "=" in line:
            key, val = line.split("=", 1)
            outputs[key.strip()] = val
            i += 1
        else:
            i += 1
    return outputs


def _basic_finding(path: str = "src/x.py", severity: str = SEVERITY_INFO) -> Finding:
    """Deterministic Finding for tests."""
    return Finding(
        path=path, line=42, body="body", severity=severity,
        start_line=None, side="RIGHT",
    )


# ---------------------------------------------------------------------------
# RunTelemetry
# ---------------------------------------------------------------------------


class RunTelemetryTests(unittest.TestCase):

    def test_defaults(self) -> None:
        rt: RunTelemetry = RunTelemetry()
        self.assertEqual(rt.tokens_used, 0)
        self.assertEqual(rt.estimated_baseline_tokens, 0)
        self.assertEqual(rt.start_time_monotonic, 0.0)
        # wall_clock_ms() returns 0 when start_time_monotonic is 0.0 —
        # never returns a giant number from `monotonic() - 0`.
        self.assertEqual(rt.wall_clock_ms(), 0)

    def test_wall_clock_grows(self) -> None:
        rt: RunTelemetry = RunTelemetry(start_time_monotonic=time.monotonic())
        first: int = rt.wall_clock_ms()
        time.sleep(0.005)
        second: int = rt.wall_clock_ms()
        self.assertGreaterEqual(second, first)

    def test_wall_clock_int_type(self) -> None:
        rt: RunTelemetry = RunTelemetry(start_time_monotonic=time.monotonic())
        self.assertIsInstance(rt.wall_clock_ms(), int)


# ---------------------------------------------------------------------------
# _estimate_cost_vs_baseline
# ---------------------------------------------------------------------------


class EstimateCostTests(unittest.TestCase):

    def test_no_change_returns_zero_pct(self) -> None:
        got: str = _estimate_cost_vs_baseline(
            effective_cap=10, base_cap=10, prompt_addendum="",
            silenced_count=0, surfaced_count=0,
        )
        self.assertEqual(got, "0%")

    def test_double_cap_returns_plus_100(self) -> None:
        got: str = _estimate_cost_vs_baseline(
            effective_cap=20, base_cap=10, prompt_addendum="",
            silenced_count=0, surfaced_count=0,
        )
        self.assertEqual(got, "+100%")

    def test_addendum_adds_5_pct(self) -> None:
        got: str = _estimate_cost_vs_baseline(
            effective_cap=10, base_cap=10, prompt_addendum="do exhaustive",
            silenced_count=0, surfaced_count=0,
        )
        self.assertEqual(got, "+5%")

    def test_cap_and_addendum_stack(self) -> None:
        got: str = _estimate_cost_vs_baseline(
            effective_cap=30, base_cap=10, prompt_addendum="addendum",
            silenced_count=0, surfaced_count=0,
        )
        # cap delta: 200%, addendum: 5% → 205%
        self.assertEqual(got, "+205%")

    def test_zero_base_cap_returns_zero(self) -> None:
        got: str = _estimate_cost_vs_baseline(
            effective_cap=10, base_cap=0, prompt_addendum="",
            silenced_count=0, surfaced_count=0,
        )
        self.assertEqual(got, "0%")

    def test_negative_effective_cap_yields_negative(self) -> None:
        # Never raises for weird inputs — best-effort.
        got: str = _estimate_cost_vs_baseline(
            effective_cap=5, base_cap=10, prompt_addendum="",
            silenced_count=0, surfaced_count=0,
        )
        self.assertEqual(got, "-50%")


# ---------------------------------------------------------------------------
# _render_iar_marker_annotation
# ---------------------------------------------------------------------------


class RenderMarkerAnnotationTests(unittest.TestCase):

    def _make_state(self, gen: int = 2, round_: int = 3) -> IterationState:
        return new_iteration_state(
            generation=gen, round_in_generation=round_,
            policy_applied=IAR_POLICY_ITERATIVE,
        )

    def _make_policy_result(
        self, *, surfaced: int, silenced: int,
        critical_silenced: int = 0,
        policy: str = IAR_POLICY_ITERATIVE,
    ) -> PolicyResult:
        surfaced_list: list[Finding] = [
            _basic_finding() for _ in range(surfaced)
        ]
        silenced_findings: list[SilencedFinding] = [
            SilencedFinding(
                finding=_basic_finding(
                    severity=SEVERITY_CRITICAL if i < critical_silenced
                    else SEVERITY_INFO,
                ),
                reason="dedup",
            )
            for i in range(silenced)
        ]
        return PolicyResult(
            findings_to_surface=surfaced_list,
            findings_silenced=silenced_findings,
            effective_max_inline_comments=10,
            prompt_addendum="",
            policy_applied=policy,
        )

    def test_includes_generation_round_policy_transition(self) -> None:
        got: str = _render_iar_marker_annotation(
            state=self._make_state(gen=5, round_=2),
            policy_result=self._make_policy_result(surfaced=3, silenced=1),
            transition=GenerationTransition.NEW_COMMITS,
        )
        self.assertIn("gen 5", got)
        self.assertIn("round 2", got)
        self.assertIn(f"`{IAR_POLICY_ITERATIVE}`", got)
        self.assertIn("new_commits", got)
        self.assertIn("3 surfaced", got)

    def test_reports_deduplicated_count(self) -> None:
        got: str = _render_iar_marker_annotation(
            state=self._make_state(),
            policy_result=self._make_policy_result(surfaced=2, silenced=4),
            transition=GenerationTransition.SAME_GENERATION,
        )
        self.assertIn("4 deduplicated", got)

    def test_omits_dedup_detail_when_no_silenced(self) -> None:
        got: str = _render_iar_marker_annotation(
            state=self._make_state(),
            policy_result=self._make_policy_result(surfaced=2, silenced=0),
            transition=GenerationTransition.FIRST_REVIEW,
        )
        self.assertNotIn("deduplicated", got)

    def test_critical_silenced_shows_warning_badge(self) -> None:
        # This should never happen (safety rail guarantees it), but if it
        # does, the annotation MUST make it visible so a human catches it.
        got: str = _render_iar_marker_annotation(
            state=self._make_state(),
            policy_result=self._make_policy_result(
                surfaced=0, silenced=2, critical_silenced=1,
            ),
            transition=GenerationTransition.SAME_GENERATION,
        )
        self.assertIn("⚠️", got)
        self.assertIn("critical", got.lower())
        self.assertIn("safety rail", got.lower())

    def test_no_warning_when_no_critical_silenced(self) -> None:
        got: str = _render_iar_marker_annotation(
            state=self._make_state(),
            policy_result=self._make_policy_result(surfaced=2, silenced=3),
            transition=GenerationTransition.SAME_GENERATION,
        )
        self.assertNotIn("⚠️", got)


# ---------------------------------------------------------------------------
# _resolve_base_sha
# ---------------------------------------------------------------------------


class ResolveBaseShaTests(unittest.TestCase):

    def test_empty_base_ref_returns_empty(self) -> None:
        got: str = _resolve_base_sha(base_ref="")
        self.assertEqual(got, "")

    def test_happy_path_returns_sha(self) -> None:
        import subprocess
        with patch.object(subprocess, "run") as mock_run:
            mock_run.return_value.stdout = "abc123def456\n"
            got: str = _resolve_base_sha(base_ref="main")
        self.assertEqual(got, "abc123def456")

    def test_all_candidates_fail_returns_empty(self) -> None:
        import subprocess
        def raise_cpe(*args: Any, **kwargs: Any) -> Any:
            raise subprocess.CalledProcessError(returncode=128, cmd=args[0])
        with patch.object(subprocess, "run", side_effect=raise_cpe):
            got: str = _resolve_base_sha(base_ref="nonexistent-branch")
        self.assertEqual(got, "")

    def test_falls_back_to_bare_ref_when_origin_fails(self) -> None:
        import subprocess
        call_count: dict[str, int] = {"count": 0}
        def side_effect(*args: Any, **kwargs: Any) -> Any:
            call_count["count"] += 1
            if call_count["count"] == 1:
                raise subprocess.CalledProcessError(returncode=128, cmd=args[0])
            # Second call succeeds.
            class Result:
                stdout = "fallback_sha_here\n"
            return Result()
        with patch.object(subprocess, "run", side_effect=side_effect):
            got: str = _resolve_base_sha(base_ref="feature/foo")
        self.assertEqual(got, "fallback_sha_here")
        self.assertEqual(call_count["count"], 2)


# ---------------------------------------------------------------------------
# _fetch_pr_labels
# ---------------------------------------------------------------------------


class FetchPrLabelsTests(unittest.TestCase):

    def test_invalid_repo_returns_empty(self) -> None:
        self.assertEqual(_fetch_pr_labels(token="t", repo="bad", pr_number=1), [])

    def test_invalid_pr_number_returns_empty(self) -> None:
        self.assertEqual(
            _fetch_pr_labels(token="t", repo="a/b", pr_number=0), []
        )

    def test_happy_path_returns_label_names(self) -> None:
        payload: dict[str, Any] = {
            "labels": [
                {"name": "bug"},
                {"name": "priority-high"},
                {"name": ""},  # empty name should be filtered
                {},  # missing name should be filtered
            ]
        }
        with patch.object(reviewer, "gh_request", return_value=payload):
            got: list[str] = _fetch_pr_labels(
                token="t", repo="owner/repo", pr_number=42
            )
        self.assertEqual(got, ["bug", "priority-high"])

    def test_api_failure_returns_empty(self) -> None:
        with patch.object(reviewer, "gh_request", side_effect=RuntimeError("500")):
            got: list[str] = _fetch_pr_labels(
                token="t", repo="owner/repo", pr_number=42
            )
        self.assertEqual(got, [])


# ---------------------------------------------------------------------------
# _load_code_contexts_for_findings
# ---------------------------------------------------------------------------


class LoadCodeContextsTests(unittest.TestCase):

    def test_dedupes_by_path(self) -> None:
        findings: list[Finding] = [
            _basic_finding(path="a.py"),
            _basic_finding(path="a.py"),
            _basic_finding(path="b.py"),
        ]
        with patch.object(
            reviewer, "load_code_context", return_value=None
        ) as mock_load:
            got: dict[str, Any] = _load_code_contexts_for_findings(
                findings=findings, review_sha="deadbeef",
            )
        # Only 2 unique paths → 2 subprocess calls.
        self.assertEqual(mock_load.call_count, 2)
        self.assertEqual(set(got.keys()), {"a.py", "b.py"})
        self.assertIsNone(got["a.py"])
        self.assertIsNone(got["b.py"])

    def test_populates_valid_contexts(self) -> None:
        findings: list[Finding] = [_basic_finding(path="x.py")]
        ctx: CodeContext = CodeContext(path="x.py", lines=("line1", "line2"))
        with patch.object(reviewer, "load_code_context", return_value=ctx):
            got: dict[str, Any] = _load_code_contexts_for_findings(
                findings=findings, review_sha="sha",
            )
        self.assertIs(got["x.py"], ctx)

    def test_empty_findings_returns_empty_dict(self) -> None:
        got: dict[str, Any] = _load_code_contexts_for_findings(
            findings=[], review_sha="sha",
        )
        self.assertEqual(got, {})

    def test_findings_with_empty_path_skipped(self) -> None:
        findings: list[Finding] = [
            _basic_finding(path=""), _basic_finding(path="a.py"),
        ]
        with patch.object(
            reviewer, "load_code_context", return_value=None
        ) as mock_load:
            _load_code_contexts_for_findings(
                findings=findings, review_sha="sha",
            )
        self.assertEqual(mock_load.call_count, 1)


# ---------------------------------------------------------------------------
# write_iar_outputs_populated
# ---------------------------------------------------------------------------


class WriteIarOutputsPopulatedTests(unittest.TestCase):

    def _make_bundle(
        self, *,
        gen: int = 2, round_: int = 3,
        policy: str = IAR_POLICY_ITERATIVE,
        tokens: int = 1234,
        effective_cap: int = 15, base_cap: int = 10,
        addendum: str = "",
    ) -> tuple[IterationState, PolicyResult, RunTelemetry]:
        state: IterationState = new_iteration_state(
            generation=gen, round_in_generation=round_,
            policy_applied=policy,
        )
        pr: PolicyResult = PolicyResult(
            findings_to_surface=[],
            findings_silenced=[],
            effective_max_inline_comments=effective_cap,
            prompt_addendum=addendum,
            policy_applied=policy,
        )
        rt: RunTelemetry = RunTelemetry(
            start_time_monotonic=time.monotonic() - 1.0,
            tokens_used=tokens,
        )
        return state, pr, rt

    def test_writes_all_five_outputs(self) -> None:
        state, pr, rt = self._make_bundle()
        with tempfile.NamedTemporaryFile(
            mode="w", delete=False, suffix=".txt"
        ) as fh:
            output_path: str = fh.name
        try:
            with patch.dict(os.environ, {"GITHUB_OUTPUT": output_path}):
                write_iar_outputs_populated(
                    state=state, policy_result=pr, telemetry=rt,
                    effective_cap=15, base_cap=10,
                )
            outputs: dict[str, str] = _parse_github_outputs(output_path)
        finally:
            os.unlink(output_path)
        self.assertEqual(outputs["iteration-round"], "3")
        self.assertEqual(outputs["iteration-generation"], "2")
        self.assertEqual(outputs["iteration-policy-applied"], IAR_POLICY_ITERATIVE)
        self.assertEqual(outputs["iteration-tokens-used"], "1234")
        self.assertEqual(
            outputs["iteration-cost-vs-baseline-estimate"], "+50%"
        )

    def test_last_write_wins_over_write_all_outputs(self) -> None:
        # write_all_outputs writes empty defaults for all 5 IAR outputs;
        # write_iar_outputs_populated writes real values LATER, so the
        # last write wins on $GITHUB_OUTPUT append semantics.
        state, pr, rt = self._make_bundle(
            gen=7, round_=4, policy=IAR_POLICY_FIRST_PASS_EXHAUSTIVE,
        )
        with tempfile.NamedTemporaryFile(
            mode="w", delete=False, suffix=".txt"
        ) as fh:
            output_path: str = fh.name
        try:
            with patch.dict(os.environ, {"GITHUB_OUTPUT": output_path}):
                reviewer.write_all_outputs(skipped=False)
                write_iar_outputs_populated(
                    state=state, policy_result=pr, telemetry=rt,
                    effective_cap=30, base_cap=10,
                )
            # Read the raw file to verify BOTH sets of writes are present,
            # with the populated ones AFTER the empty ones.
            with open(output_path, "r", encoding="utf-8") as f:
                raw: str = f.read()
        finally:
            os.unlink(output_path)
        # First (empty) writes present:
        self.assertIn("iteration-round=\n", raw)
        # Second (populated) writes present:
        self.assertIn("iteration-round=4\n", raw)
        self.assertIn("iteration-generation=7\n", raw)
        self.assertIn(
            f"iteration-policy-applied={IAR_POLICY_FIRST_PASS_EXHAUSTIVE}\n",
            raw,
        )
        # Ordering: populated writes come after empty ones (last-write-wins
        # semantics of GITHUB_OUTPUT).
        empty_pos: int = raw.find("iteration-round=\n")
        populated_pos: int = raw.find("iteration-round=4\n")
        self.assertLess(empty_pos, populated_pos)


# ---------------------------------------------------------------------------
# IterationState.head_sha round-trip (schema extension)
# ---------------------------------------------------------------------------


class HeadShaRoundTripTests(unittest.TestCase):

    def test_default_empty_string(self) -> None:
        state: IterationState = new_iteration_state()
        self.assertEqual(state.head_sha, "")

    def test_new_iteration_state_accepts_head_sha(self) -> None:
        state: IterationState = new_iteration_state(head_sha="abc123")
        self.assertEqual(state.head_sha, "abc123")

    def test_embed_and_parse_round_trip(self) -> None:
        state: IterationState = new_iteration_state(
            generation=3, generation_range_hash="range",
            round_in_generation=2, base_sha="baseabc",
            head_sha="headabc",
        )
        marker_body: str = "<!-- ai-pr-reviewer-marker -->\nBody text."
        embedded: str = embed_iteration_state(marker_body, state)
        # Parse it back.
        parsed_state = reviewer._parse_state_from_marker_body(embedded)
        self.assertIsNotNone(parsed_state)
        assert parsed_state is not None  # type narrowing
        self.assertEqual(parsed_state.head_sha, "headabc")
        self.assertEqual(parsed_state.base_sha, "baseabc")

    def test_older_markers_without_head_sha_parse_to_empty(self) -> None:
        # Simulate a marker written by pre-Task-8 IAR: no head_sha field.
        # Use the same tag constants the runtime uses so the test doesn't
        # drift if the tag values change in a future PR.
        old_json: str = (
            "{\n"
            f'  "version": {IAR_STATE_SCHEMA_VERSION},\n'
            '  "generation": 1,\n'
            '  "generation_range_hash": "old",\n'
            '  "round_in_generation": 1,\n'
            f'  "policy_applied": "{IAR_POLICY_ITERATIVE}",\n'
            '  "resolved_fingerprints": [],\n'
            '  "open_fingerprints_this_gen": [],\n'
            '  "history": [],\n'
            '  "base_sha": "old_base"\n'
            "}"
        )
        old_body: str = (
            "<!-- ai-pr-reviewer-marker -->\n"
            "Body.\n\n"
            f"{reviewer.IAR_STATE_TAG_OPEN}\n"
            f"{old_json}\n"
            f"{reviewer.IAR_STATE_TAG_CLOSE}"
        )
        parsed_state = reviewer._parse_state_from_marker_body(old_body)
        self.assertIsNotNone(parsed_state)
        assert parsed_state is not None
        self.assertEqual(parsed_state.head_sha, "")
        self.assertEqual(parsed_state.base_sha, "old_base")


# ---------------------------------------------------------------------------
# run_iar_pre_llm — mocked integration
# ---------------------------------------------------------------------------


class RunIarPreLlmTests(unittest.TestCase):

    def _iar_config(self, *, policy: str = IAR_POLICY_ITERATIVE) -> IARConfig:
        return IARConfig(
            policy=policy,
            max_review_rounds=0, cap_multiplier=3, escape_label="full-review-please",
        )

    def test_first_review_no_prior_state(self) -> None:
        with patch.object(
            reviewer, "read_prior_iteration_state", return_value=None,
        ), patch.object(
            reviewer, "_resolve_base_sha", return_value="baseabc",
        ), patch.object(
            reviewer, "compute_generation_range_hash", return_value="rangehash",
        ), patch.object(
            reviewer, "_fetch_pr_labels", return_value=[],
        ):
            ctx: IARPreLLMContext = run_iar_pre_llm(
                iar_config=self._iar_config(),
                repo="a/b", pr_number=1, gh_token="t",
                base_ref="main", head_sha="head123",
                base_max_inline_comments=10,
            )
        self.assertIsNone(ctx.prior_state)
        self.assertEqual(ctx.transition, GenerationTransition.FIRST_REVIEW)
        self.assertEqual(ctx.base_sha, "baseabc")
        self.assertEqual(ctx.head_sha, "head123")
        self.assertEqual(ctx.range_hash, "rangehash")
        self.assertEqual(ctx.new_lines_pct, 0.0)  # skipped for FIRST_REVIEW
        self.assertEqual(
            ctx.pre_policy_result.effective_max_inline_comments, 10
        )

    def test_same_generation_no_new_lines_call(self) -> None:
        prior_state: IterationState = new_iteration_state(
            generation=2, generation_range_hash="samehash",
            round_in_generation=2, base_sha="baseabc",
            head_sha="prior_head",
        )
        with patch.object(
            reviewer, "read_prior_iteration_state", return_value=prior_state,
        ), patch.object(
            reviewer, "_resolve_base_sha", return_value="baseabc",
        ), patch.object(
            reviewer, "compute_generation_range_hash", return_value="samehash",
        ), patch.object(
            reviewer, "_fetch_pr_labels", return_value=[],
        ), patch.object(
            reviewer, "compute_new_lines_pct",
        ) as mock_new_lines:
            ctx: IARPreLLMContext = run_iar_pre_llm(
                iar_config=self._iar_config(),
                repo="a/b", pr_number=1, gh_token="t",
                base_ref="main", head_sha="head123",
                base_max_inline_comments=10,
            )
        # SAME_GENERATION → new_lines_pct skipped (safety net can't fire).
        mock_new_lines.assert_not_called()
        self.assertEqual(ctx.transition, GenerationTransition.SAME_GENERATION)

    def test_new_commits_triggers_new_lines_computation(self) -> None:
        prior_state: IterationState = new_iteration_state(
            generation=2, generation_range_hash="oldhash",
            round_in_generation=1, base_sha="baseabc",
            head_sha="prior_head",
        )
        with patch.object(
            reviewer, "read_prior_iteration_state", return_value=prior_state,
        ), patch.object(
            reviewer, "_resolve_base_sha", return_value="baseabc",
        ), patch.object(
            reviewer, "compute_generation_range_hash", return_value="newhash",
        ), patch.object(
            reviewer, "_fetch_pr_labels", return_value=[],
        ), patch.object(
            reviewer, "compute_new_lines_pct", return_value=42.5,
        ) as mock_new_lines:
            ctx: IARPreLLMContext = run_iar_pre_llm(
                iar_config=self._iar_config(),
                repo="a/b", pr_number=1, gh_token="t",
                base_ref="main", head_sha="head123",
                base_max_inline_comments=10,
            )
        mock_new_lines.assert_called_once()
        self.assertEqual(ctx.transition, GenerationTransition.NEW_COMMITS)
        self.assertEqual(ctx.new_lines_pct, 42.5)

    # ------------------------------------------------------------------
    # User-forced reset (reviewed-label absence)
    # ------------------------------------------------------------------

    def test_user_forced_reset_when_reviewed_label_absent_with_prior_state(
        self,
    ) -> None:
        """Reviewed label absent + prior state present + prior state
        recorded that the label WAS previously stamped → USER_FORCED_RESET,
        prior_state wiped to None, new_lines_pct forced to 0.0 so the
        safety net can't fire (irrelevant when we're starting fresh).

        The `reviewed_label_applied=True` bit on the prior state is
        load-bearing: without it, this looks identical to a blocked-run
        re-trigger and the safety guard suppresses the reset (see
        `test_user_forced_reset_no_op_when_prior_state_never_stamped_label`).
        """
        prior_state: IterationState = new_iteration_state(
            generation=5, generation_range_hash="oldhash",
            round_in_generation=3, base_sha="baseabc",
            head_sha="prior_head",
        )
        prior_state.reviewed_label_applied = True
        with patch.object(
            reviewer, "read_prior_iteration_state", return_value=prior_state,
        ), patch.object(
            reviewer, "_resolve_base_sha", return_value="basexyz",
        ), patch.object(
            reviewer, "compute_generation_range_hash", return_value="newhash",
        ), patch.object(
            reviewer, "_fetch_pr_labels",
            return_value=["Ready", "priority:high"],  # reviewed label removed
        ), patch.object(
            reviewer, "compute_new_lines_pct", return_value=42.5,
        ) as mock_new_lines:
            ctx: IARPreLLMContext = run_iar_pre_llm(
                iar_config=self._iar_config(),
                repo="a/b", pr_number=1, gh_token="t",
                base_ref="main", head_sha="head123",
                base_max_inline_comments=10,
                applied_label="ai-reviewed",
            )
        self.assertEqual(
            ctx.transition, GenerationTransition.USER_FORCED_RESET
        )
        self.assertIsNone(ctx.prior_state)
        self.assertEqual(
            ctx.new_lines_pct, 0.0,
            msg="USER_FORCED_RESET must zero out new_lines_pct so the "
                "safety net cannot fire on a fresh-start run.",
        )
        # compute_new_lines_pct MAY have been called during the initial
        # NEW_COMMITS detection (before the reset override kicks in),
        # so we don't assert on the call count — only on the effective
        # value that survives to the returned context.
        _ = mock_new_lines

    def test_user_forced_reset_no_op_when_prior_state_never_stamped_label(
        self,
    ) -> None:
        """The blocked-run safety guard: prior state exists AND the
        reviewed label is absent from the PR, BUT the prior state records
        `reviewed_label_applied=False` (the last review was BLOCKED and
        therefore never stamped the label). This looks superficially like
        a reset gesture but is actually a natural re-trigger after a
        blocked review — the label was NEVER on the PR to remove — so
        USER_FORCED_RESET MUST NOT fire and dedup memory MUST be
        preserved. Without this guard, every `block-on-critical`
        consumer would see fingerprint memory wiped after every blocked
        run.
        """
        prior_state: IterationState = new_iteration_state(
            generation=2, generation_range_hash="samehash",
            round_in_generation=1, base_sha="baseabc",
            head_sha="prior_head",
        )
        # Explicit for readability — `new_iteration_state` defaults to False,
        # which is the correct "safe" default for consumers on state that
        # predates the field.
        self.assertFalse(prior_state.reviewed_label_applied)
        with patch.object(
            reviewer, "read_prior_iteration_state", return_value=prior_state,
        ), patch.object(
            reviewer, "_resolve_base_sha", return_value="baseabc",
        ), patch.object(
            reviewer, "compute_generation_range_hash", return_value="samehash",
        ), patch.object(
            reviewer, "_fetch_pr_labels",
            return_value=["Ready"],  # reviewed label ABSENT
        ):
            ctx: IARPreLLMContext = run_iar_pre_llm(
                iar_config=self._iar_config(),
                repo="a/b", pr_number=1, gh_token="t",
                base_ref="main", head_sha="head123",
                base_max_inline_comments=10,
                applied_label="ai-reviewed",  # configured
            )
        self.assertEqual(
            ctx.transition, GenerationTransition.SAME_GENERATION,
            msg="prior_state.reviewed_label_applied=False must suppress "
                "USER_FORCED_RESET even when the reviewed label is absent "
                "from the PR — otherwise every blocked-run re-trigger "
                "looks like a deliberate reset gesture.",
        )
        self.assertIs(
            ctx.prior_state, prior_state,
            msg="Prior state must be preserved (dedup memory intact) "
                "when the reset gesture is suppressed.",
        )

    def test_user_forced_reset_no_op_when_reviewed_label_empty(self) -> None:
        """Consumer didn't set a reviewed label → the gesture can't
        exist, USER_FORCED_RESET never fires even if prior state exists."""
        prior_state: IterationState = new_iteration_state(
            generation=2, generation_range_hash="samehash",
            round_in_generation=1, base_sha="baseabc",
            head_sha="prior_head",
        )
        with patch.object(
            reviewer, "read_prior_iteration_state", return_value=prior_state,
        ), patch.object(
            reviewer, "_resolve_base_sha", return_value="baseabc",
        ), patch.object(
            reviewer, "compute_generation_range_hash", return_value="samehash",
        ), patch.object(
            reviewer, "_fetch_pr_labels", return_value=["Ready"],
        ):
            ctx: IARPreLLMContext = run_iar_pre_llm(
                iar_config=self._iar_config(),
                repo="a/b", pr_number=1, gh_token="t",
                base_ref="main", head_sha="head123",
                base_max_inline_comments=10,
                applied_label="",  # not configured
            )
        self.assertEqual(
            ctx.transition, GenerationTransition.SAME_GENERATION
        )
        self.assertIs(ctx.prior_state, prior_state)

    def test_user_forced_reset_no_op_when_reviewed_label_still_present(
        self,
    ) -> None:
        """Reviewed label still on PR → not a reset gesture, IAR
        proceeds along the normal generation path."""
        prior_state: IterationState = new_iteration_state(
            generation=2, generation_range_hash="samehash",
            round_in_generation=1, base_sha="baseabc",
            head_sha="prior_head",
        )
        with patch.object(
            reviewer, "read_prior_iteration_state", return_value=prior_state,
        ), patch.object(
            reviewer, "_resolve_base_sha", return_value="baseabc",
        ), patch.object(
            reviewer, "compute_generation_range_hash", return_value="samehash",
        ), patch.object(
            reviewer, "_fetch_pr_labels",
            return_value=["Ready", "ai-reviewed"],
        ):
            ctx: IARPreLLMContext = run_iar_pre_llm(
                iar_config=self._iar_config(),
                repo="a/b", pr_number=1, gh_token="t",
                base_ref="main", head_sha="head123",
                base_max_inline_comments=10,
                applied_label="ai-reviewed",
            )
        self.assertEqual(
            ctx.transition, GenerationTransition.SAME_GENERATION
        )
        self.assertIs(ctx.prior_state, prior_state)

    def test_user_forced_reset_no_op_on_first_review(self) -> None:
        """No prior state → reset gesture is undefined, transition
        stays FIRST_REVIEW even if the reviewed label is absent (which
        it always is on a first-ever review)."""
        with patch.object(
            reviewer, "read_prior_iteration_state", return_value=None,
        ), patch.object(
            reviewer, "_resolve_base_sha", return_value="baseabc",
        ), patch.object(
            reviewer, "compute_generation_range_hash", return_value="rangehash",
        ), patch.object(
            reviewer, "_fetch_pr_labels", return_value=["Ready"],
        ):
            ctx: IARPreLLMContext = run_iar_pre_llm(
                iar_config=self._iar_config(),
                repo="a/b", pr_number=1, gh_token="t",
                base_ref="main", head_sha="head123",
                base_max_inline_comments=10,
                applied_label="ai-reviewed",
            )
        self.assertEqual(ctx.transition, GenerationTransition.FIRST_REVIEW)
        self.assertIsNone(ctx.prior_state)


# ---------------------------------------------------------------------------
# run_iar_post_llm
# ---------------------------------------------------------------------------


class RunIarPostLlmTests(unittest.TestCase):

    def _iar_config(self, *, policy: str = IAR_POLICY_ITERATIVE) -> IARConfig:
        return IARConfig(
            policy=policy,
            max_review_rounds=0, cap_multiplier=3, escape_label="full-review-please",
        )

    def _pre_context(
        self, *, transition: GenerationTransition = GenerationTransition.FIRST_REVIEW,
        prior_state: IterationState | None = None,
    ) -> IARPreLLMContext:
        return IARPreLLMContext(
            prior_state=prior_state, transition=transition,
            base_sha="base", head_sha="head",
            range_hash="hash", new_lines_pct=0.0, pr_labels=[],
            pre_policy_result=PolicyResult(
                findings_to_surface=[], findings_silenced=[],
                effective_max_inline_comments=10, prompt_addendum="",
                policy_applied=IAR_POLICY_ITERATIVE,
            ),
        )

    def test_first_review_creates_gen_1(self) -> None:
        pre: IARPreLLMContext = self._pre_context()
        result: ReviewResult = ReviewResult(
            summary="", findings=[
                _basic_finding("a.py"), _basic_finding("b.py"),
            ],
        )
        with patch.object(reviewer, "load_code_context", return_value=None):
            state_final, policy_result = run_iar_post_llm(
                iar_config=self._iar_config(),
                pre_context=pre, result=result,
                base_max_inline_comments=10, telemetry=RunTelemetry(),
            )
        self.assertEqual(state_final.generation, 1)
        self.assertEqual(state_final.round_in_generation, 1)
        self.assertEqual(state_final.head_sha, "head")
        self.assertEqual(state_final.base_sha, "base")
        self.assertEqual(len(state_final.open_fingerprints_this_gen), 2)
        self.assertEqual(len(policy_result.findings_to_surface), 2)

    def test_same_generation_increments_round(self) -> None:
        prior_state: IterationState = new_iteration_state(
            generation=1, round_in_generation=1,
            generation_range_hash="samehash", base_sha="base", head_sha="oldhead",
        )
        pre: IARPreLLMContext = self._pre_context(
            transition=GenerationTransition.SAME_GENERATION,
            prior_state=prior_state,
        )
        result: ReviewResult = ReviewResult(
            summary="", findings=[_basic_finding("a.py")],
        )
        with patch.object(reviewer, "load_code_context", return_value=None):
            state_final, _ = run_iar_post_llm(
                iar_config=self._iar_config(),
                pre_context=pre, result=result,
                base_max_inline_comments=10, telemetry=RunTelemetry(),
            )
        self.assertEqual(state_final.generation, 1)
        self.assertEqual(state_final.round_in_generation, 2)
        # head_sha refreshed to the current head.
        self.assertEqual(state_final.head_sha, "head")

    def test_escape_label_preserves_prior_state(self) -> None:
        prior_state: IterationState = new_iteration_state(
            generation=3, round_in_generation=5,
            generation_range_hash="prior", base_sha="base",
        )
        pre: IARPreLLMContext = self._pre_context(
            transition=GenerationTransition.SAME_GENERATION,
            prior_state=prior_state,
        )
        pre = IARPreLLMContext(
            prior_state=pre.prior_state, transition=pre.transition,
            base_sha=pre.base_sha, head_sha=pre.head_sha,
            range_hash=pre.range_hash, new_lines_pct=pre.new_lines_pct,
            pr_labels=["full-review-please"],  # ← escape label present
            pre_policy_result=pre.pre_policy_result,
        )
        result: ReviewResult = ReviewResult(
            summary="", findings=[_basic_finding("a.py")],
        )
        with patch.object(reviewer, "load_code_context", return_value=None):
            state_final, policy_result = run_iar_post_llm(
                iar_config=self._iar_config(),
                pre_context=pre, result=result,
                base_max_inline_comments=10, telemetry=RunTelemetry(),
            )
        self.assertEqual(
            policy_result.policy_applied, IAR_POLICY_ESCAPE_LABEL_FORCED
        )
        # Prior state preserved EXACTLY — no mutation.
        self.assertIs(state_final, prior_state)

    def test_findings_mutated_in_place(self) -> None:
        prior_state: IterationState = new_iteration_state(
            generation=1, round_in_generation=1,
            generation_range_hash="samehash", base_sha="base", head_sha="head",
        )
        pre: IARPreLLMContext = self._pre_context(
            transition=GenerationTransition.SAME_GENERATION,
            prior_state=prior_state,
        )
        # Two findings; the first is critical (must surface), the second
        # will match a prior open fingerprint (must be silenced).
        finding_a: Finding = _basic_finding(
            path="a.py", severity=SEVERITY_CRITICAL,
        )
        finding_b: Finding = _basic_finding(
            path="b.py", severity=SEVERITY_WARNING,
        )
        result: ReviewResult = ReviewResult(
            summary="", findings=[finding_a, finding_b],
        )
        with patch.object(reviewer, "load_code_context", return_value=None):
            # Prepopulate prior_state.open_fingerprints_this_gen with the
            # fingerprint of finding_b so it gets silenced.
            fp_b: str = reviewer.finding_fingerprint(
                finding=finding_b, code_context=None,
            )
            prior_state.open_fingerprints_this_gen = [fp_b]
            state_final, policy_result = run_iar_post_llm(
                iar_config=self._iar_config(),
                pre_context=pre, result=result,
                base_max_inline_comments=10, telemetry=RunTelemetry(),
            )
        # result.findings mutated to only the surfaced ones.
        self.assertEqual(len(result.findings), 1)
        self.assertEqual(result.findings[0].severity, SEVERITY_CRITICAL)
        # Silenced set contains finding_b.
        self.assertEqual(len(policy_result.findings_silenced), 1)
        # Overall severity recomputed — critical still present.
        self.assertEqual(result.overall_severity, SEVERITY_CRITICAL)


# ---------------------------------------------------------------------------
# compute_reviewed_label_applied — the three-signal OR write logic for the
# USER_FORCED_RESET arming bit. Extracted from main() so the invariant can
# be unit-tested in isolation. See docs/ITERATION_AWARENESS.md § 8.5 for
# the load-bearing correctness contract.
# ---------------------------------------------------------------------------


class ComputeReviewedLabelAppliedTests(unittest.TestCase):
    """Every code path in `compute_reviewed_label_applied`. The function
    computes the `reviewed_label_applied` bit that arms USER_FORCED_RESET
    on the NEXT run — inverting the logic silently disarms the reset
    gesture, so each signal gets a dedicated test."""

    def _state(self, *, reviewed_label_applied: bool) -> IterationState:
        return IterationState(
            version=IAR_STATE_SCHEMA_VERSION,
            generation=1, generation_range_hash="h", round_in_generation=1,
            policy_applied=IAR_POLICY_ITERATIVE, resolved_fingerprints=[],
            open_fingerprints_this_gen=[], history=[], base_sha="b",
            head_sha="h", reviewed_label_applied=reviewed_label_applied,
        )

    def test_empty_applied_label_always_false(self) -> None:
        """Consumers who don't configure `applied-label` opt out of the
        reviewed-label workflow entirely — the bit must stay False so
        USER_FORCED_RESET cannot fire on them."""
        got: bool = reviewer.compute_reviewed_label_applied(
            applied_label="",
            label_stamped=True,      # even if somehow stamped
            current_labels=["foo"],  # even if PR has labels
            prior_state=self._state(reviewed_label_applied=True),
        )
        self.assertFalse(got)

    def test_label_stamped_this_run_true(self) -> None:
        """Signal 1: this run's stamp succeeded → True."""
        got: bool = reviewer.compute_reviewed_label_applied(
            applied_label="ai-reviewed",
            label_stamped=True,
            current_labels=[],
            prior_state=None,
        )
        self.assertTrue(got)

    def test_label_already_on_pr_true(self) -> None:
        """Signal 2: label present at trigger time → True even if this
        run didn't stamp (blocked / re-trigger)."""
        got: bool = reviewer.compute_reviewed_label_applied(
            applied_label="ai-reviewed",
            label_stamped=False,
            current_labels=["ready", "ai-reviewed"],
            prior_state=None,
        )
        self.assertTrue(got)

    def test_prior_state_bit_preserved_when_blocked(self) -> None:
        """Signal 3 — CORE FIX for the round-3 warning: a blocked
        follow-up run does not stamp AND does not remove the label,
        but MUST preserve the prior state's arming bit so a later
        legitimate reset gesture can still fire. Before the fix, this
        case returned False and silently disarmed USER_FORCED_RESET."""
        got: bool = reviewer.compute_reviewed_label_applied(
            applied_label="ai-reviewed",
            label_stamped=False,       # blocked: didn't stamp
            current_labels=[],         # label GONE from PR view
            prior_state=self._state(reviewed_label_applied=True),
        )
        self.assertTrue(
            got,
            msg="Blocked run with prior arming bit=True MUST preserve "
                "True so the reset gesture stays armed. "
                "Regression against docs § 8.5 four-condition guard.",
        )

    def test_all_signals_false_returns_false(self) -> None:
        """No stamp, no label on PR, no prior arming → False. This is
        the correct disarm case: reviewer has never established the
        reviewed-label state, so USER_FORCED_RESET must not fire on
        the next run (there is nothing meaningful to reset from)."""
        got: bool = reviewer.compute_reviewed_label_applied(
            applied_label="ai-reviewed",
            label_stamped=False,
            current_labels=["ready"],  # unrelated label only
            prior_state=self._state(reviewed_label_applied=False),
        )
        self.assertFalse(got)

    def test_no_prior_state_falls_back_to_stamp_or_pr(self) -> None:
        """First-ever run with no prior state → bit reflects this
        run's outcome only (signals 1 + 2)."""
        # No stamp, no PR label → False
        self.assertFalse(
            reviewer.compute_reviewed_label_applied(
                applied_label="ai-reviewed",
                label_stamped=False,
                current_labels=["ready"],
                prior_state=None,
            )
        )
        # Stamp only → True
        self.assertTrue(
            reviewer.compute_reviewed_label_applied(
                applied_label="ai-reviewed",
                label_stamped=True,
                current_labels=[],
                prior_state=None,
            )
        )

    def test_prior_bit_false_and_no_current_signals_returns_false(
        self,
    ) -> None:
        """Prior state exists but its arming bit was False (e.g. the
        previous run was blocked too). No stamp this run, no PR label
        → False. This ensures the bit doesn't ratchet True from
        nothing."""
        got: bool = reviewer.compute_reviewed_label_applied(
            applied_label="ai-reviewed",
            label_stamped=False,
            current_labels=[],
            prior_state=self._state(reviewed_label_applied=False),
        )
        self.assertFalse(got)


if __name__ == "__main__":
    unittest.main()
