"""Local-review regressions for multi-backend and incremental safety."""

from __future__ import annotations

import importlib.util
import dataclasses
import io
import sys
import tempfile
import re
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any
from unittest import mock

_ROOT: Path = Path(__file__).resolve().parent.parent
_SPEC = importlib.util.spec_from_file_location("reviewer", _ROOT / "scripts/reviewer.py")
assert _SPEC is not None and _SPEC.loader is not None
reviewer = importlib.util.module_from_spec(_SPEC)
sys.modules["reviewer"] = reviewer
_SPEC.loader.exec_module(reviewer)


def prior(severity: str = "critical") -> Any:
    return reviewer.PriorFinding(
        thread_id="thread", comment_id="comment", comment_database_id=1,
        path="src/auth.py", line=10, severity=severity, fingerprint="a" * 16,
        body_excerpt="Authentication bypass", is_outdated=False,
    )


def context(transition: Any = None) -> Any:
    state = reviewer.new_iteration_state(
        generation_range_hash="old", base_sha="b" * 40, head_sha="1" * 40,
        policy_applied=reviewer.IAR_POLICY_ITERATIVE,
    )
    state.open_fingerprints_this_gen = [prior().fingerprint]
    return reviewer.IARPreLLMContext(
        prior_state=state,
        transition=transition or reviewer.GenerationTransition.SAME_GENERATION,
        base_sha="b" * 40, head_sha="2" * 40, range_hash="new",
        new_lines_pct=1.0, pr_labels=[],
        pre_policy_result=reviewer.PolicyResult(
            findings_to_surface=[], findings_silenced=[],
            effective_max_inline_comments=3, prompt_addendum="",
            policy_applied=reviewer.IAR_POLICY_ITERATIVE,
        ),
        mode=reviewer.IAR_MODE_INCREMENTAL, prior_findings=(prior(),),
        delta=reviewer.IncrementalDelta(
            prior_head_sha="1" * 40, head_sha="2" * 40,
            changed_files=("src/auth.py",), delta_ratio=0.01,
        ),
    )


class IncrementalSafetyTests(unittest.TestCase):
    def test_incremental_context_uses_actual_delta_not_truncated_pr_diff(self) -> None:
        pre = context()
        delta_text = "diff --git a/src/auth.py b/src/auth.py\n--- a/src/auth.py\n+++ b/src/auth.py\n@@ -10 +10 @@\n-allowed = False\n+allowed = True\n"
        pre = dataclasses.replace(pre, delta=dataclasses.replace(pre.delta, diff=delta_text))
        pr = reviewer.PRContext(
            title="t", author="a", head_ref="feature", base_ref="main", state="open",
            additions=1, deletions=1, commits=2, body="", changed_files=[],
            diff="diff --git a/old.py b/old.py\n+old change\n[truncated]",
            incremental=pre,
        )
        rendered = reviewer.render_incremental_sections(pr, pre)
        self.assertIn("+allowed = True", rendered)
        self.assertNotIn("No code changes since", rendered)
        self.assertNotIn("+old change", rendered)

    def test_unposted_prior_critical_still_blocks_and_is_not_marked_resolved(self) -> None:
        result = reviewer.ReviewResult(summary="No new issues", findings=[], overall_severity="none")
        with mock.patch.object(reviewer, "_load_code_contexts_for_findings", return_value={}):
            state, _ = reviewer.run_iar_post_llm(
                iar_config=reviewer.build_iar_config({"AIPRR_CONVERGENCE_POLICY": "iterative"}),
                pre_context=context(), result=result, base_max_inline_comments=3,
                telemetry=reviewer.RunTelemetry(),
            )
        self.assertEqual(result.findings, [], "do not re-post the old inline comment")
        self.assertEqual(result.overall_severity, "critical")
        self.assertTrue(reviewer.evaluate_strictness(result.overall_severity, "block-on-critical")[0])
        self.assertIn(prior().fingerprint, state.open_fingerprints_this_gen)
        self.assertNotIn(prior().fingerprint, state.resolved_fingerprints)

    def test_verified_policy_retires_corroborated_finding_and_unblocks(self) -> None:
        """Opt-in `verified`: a resolved verdict + file changed + fingerprint
        absent leaves the outstanding set and stops gating; default stays put."""
        pre = context()
        result = reviewer.ReviewResult(summary="Fixed it", findings=[], overall_severity="none")
        result.prior_finding_updates = {prior().fingerprint: ("resolved", "auth check restored")}
        with tempfile.TemporaryDirectory() as td, mock.patch.object(reviewer, "_load_code_contexts_for_findings", return_value={}):
            state, _ = reviewer.run_iar_post_llm(
                iar_config=reviewer.build_iar_config({"AIPRR_CONVERGENCE_POLICY": "iterative"}),
                pre_context=pre, result=result, base_max_inline_comments=3,
                telemetry=reviewer.RunTelemetry(), resolution_policy="verified", workspace=Path(td),
            )
        self.assertEqual(result.overall_severity, "none")
        self.assertNotIn(prior().fingerprint, state.open_fingerprints_this_gen)
        self.assertIn(prior().fingerprint, state.resolved_fingerprints)

    def test_verified_policy_keeps_a_reposted_finding_open(self) -> None:
        """Corroboration must see THIS round's fingerprints: when the model
        re-posts the issue (same fingerprint) while also claiming `resolved`,
        the finding stays open and keeps gating."""
        pre = context()
        reposted = reviewer.Finding(path="src/auth.py", line=10, body="Authentication bypass", severity="critical")
        result = reviewer.ReviewResult(summary="", findings=[reposted], overall_severity="critical")
        result.prior_finding_updates = {prior().fingerprint: ("resolved", "claims fixed")}
        with tempfile.TemporaryDirectory() as td, \
             mock.patch.object(reviewer, "_load_code_contexts_for_findings", return_value={}), \
             mock.patch.object(reviewer, "finding_fingerprint", return_value=prior().fingerprint):
            state, _ = reviewer.run_iar_post_llm(
                iar_config=reviewer.build_iar_config({"AIPRR_CONVERGENCE_POLICY": "iterative"}),
                pre_context=pre, result=result, base_max_inline_comments=3,
                telemetry=reviewer.RunTelemetry(), resolution_policy="verified", workspace=Path(td),
            )
        self.assertEqual(result.overall_severity, "critical")
        self.assertIn(prior().fingerprint, state.open_fingerprints_this_gen)
        self.assertNotIn(prior().fingerprint, state.resolved_fingerprints)

    def test_file_change_and_model_claim_do_not_prove_resolution(self) -> None:
        pre = context()
        result = reviewer.reconcile_prior_findings(
            prior_findings=pre.prior_findings,
            updates={prior().fingerprint: ("resolved", "A comment changed")},
            current_fingerprints=set(), delta=pre.delta, workspace=_ROOT,
        )
        self.assertEqual(result.resolved, [])
        self.assertEqual(result.still_open, [prior()])
        self.assertEqual(result.unverified, [prior()])

    def test_base_movement_requires_full_review_even_with_ancestor_head(self) -> None:
        pre = context(reviewer.GenerationTransition.REBASED)
        mode, _ = reviewer.select_iar_mode(
            prior_state=pre.prior_state, transition=pre.transition,
            pre_policy_result=pre.pre_policy_result,
            prior_findings=list(pre.prior_findings), delta=pre.delta,
        )
        self.assertEqual(mode, reviewer.IAR_MODE_FULL)


class PriorPaginationTests(unittest.TestCase):
    @staticmethod
    def page(nodes: list[dict[str, Any]], more: bool, cursor: str | None) -> dict[str, Any]:
        return {"repository": {"pullRequest": {"reviewThreads": {
            "nodes": nodes, "pageInfo": {"hasNextPage": more, "endCursor": cursor},
        }}}}

    def test_finding_on_second_page_is_carried_forward(self) -> None:
        thread = {
            "id": "thread", "isResolved": False, "path": "src/auth.py", "line": 10,
            "comments": {"nodes": [{
                "id": "comment", "databaseId": 1, "author": {"login": "github-actions"},
                "body": "Authentication bypass" + reviewer.render_inline_finding_marker("a" * 16, "critical"),
                "pullRequestReview": {"body": reviewer.provider_marker("anthropic")},
            }]},
        }
        with mock.patch.object(reviewer, "gh_graphql", side_effect=[
            self.page([], True, "cursor-1"), self.page([thread], False, None),
        ]) as api:
            findings = reviewer.fetch_prior_findings(
                token="fake", repo="owner/repo", pr_number=1,
                bot_login="github-actions[bot]", provider_marker_text=reviewer.provider_marker("anthropic"),
            )
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0].severity, "critical")
        self.assertEqual(api.call_args_list[1].args[1]["after"], "cursor-1")

    def test_broken_pagination_is_not_a_complete_partial_history(self) -> None:
        with mock.patch.object(reviewer, "gh_graphql", return_value=self.page([], True, None)) as api:
            self.assertEqual(reviewer.fetch_prior_findings(
                token="fake", repo="owner/repo", pr_number=1, bot_login="github-actions",
            ), [])
        self.assertEqual(api.call_count, 1)


class UsageAccountingTests(unittest.TestCase):
    def test_cached_and_cache_creation_tokens_are_in_total_and_display(self) -> None:
        usage = reviewer.normalise_usage({
            "input_tokens": 10, "output_tokens": 5,
            "cache_read_input_tokens": 100, "cache_creation_input_tokens": 20,
        })
        self.assertIsNotNone(usage)
        self.assertEqual(usage.total_tokens, 135)
        self.assertAlmostEqual(usage.cached_ratio, 100 / 130)
        self.assertIn("130 in", reviewer.format_usage_line(usage, model="m", wall_clock_ms=0))

    def test_openai_cached_subset_is_counted_once(self) -> None:
        usage = reviewer.normalise_usage({
            "prompt_tokens": 1000, "completion_tokens": 7,
            "prompt_tokens_details": {"cached_tokens": 800},
        })
        self.assertEqual(usage.total_tokens, 1007)

    def test_codex_cached_subset_is_not_billed_as_uncached_too(self) -> None:
        usage = reviewer.normalise_usage({
            "input_tokens": 1000, "cached_input_tokens": 800, "output_tokens": 7,
        })
        self.assertEqual(usage.input_tokens, 200)
        self.assertEqual(usage.total_tokens, 1007)


class RoutingSafetyTests(unittest.TestCase):
    def test_install_steps_select_exactly_one_cli_or_none_for_direct_apis(self) -> None:
        action = (_ROOT / "action.yml").read_text(encoding="utf-8")
        gates = re.findall(r"    - name: Install[^\n]*\n      if: inputs.provider == '([^']+)'", action)
        self.assertCountEqual(gates, ["claude-code", "cursor", "codex", "grok"])
        for provider in reviewer.DEFAULT_MODELS:
            self.assertEqual(gates.count(provider), 0 if provider in ("anthropic", "openai") else 1)

    def test_trigger_state_is_scoped_to_custom_backend(self) -> None:
        scope = reviewer.review_scope_id("codex", "https://gateway.example/v1")
        body = reviewer.write_trigger_state(
            reviewer.REVIEW_MARKER + reviewer.provider_marker(scope),
            {"label_toggle_generation": 1},
        )
        other = reviewer.write_trigger_state(
            reviewer.REVIEW_MARKER + reviewer.provider_marker("codex"),
            {"label_toggle_generation": 2},
        )
        with mock.patch.object(reviewer, "gh_request", return_value=[{"body": body}, {"body": other}]):
            state = reviewer._read_existing_tracking_state(
                token="fake", repo="owner/repo", pr_number=1, provider_id=scope,
            )
        self.assertEqual(state["label_toggle_generation"], 1)

    def test_custom_backends_have_distinct_state_scopes(self) -> None:
        default = reviewer.review_scope_id("codex", "")
        azure = reviewer.review_scope_id("codex", "https://resource.services.ai.azure.com/openai/v1")
        xai = reviewer.review_scope_id("codex", "https://api.x.ai/v1")
        self.assertEqual(default, "codex")
        self.assertEqual(len({default, azure, xai}), 3)
        self.assertEqual(azure, reviewer.review_scope_id("codex", "https://resource.services.ai.azure.com/openai/v1/"))
        self.assertNotIn("https", azure)

    def test_controls_in_url_are_rejected_before_urlsplit_strips_them(self) -> None:
        for value in ("https://api.x.ai/\nv1", "https://api.x.ai/\tv1", "\x00https://api.x.ai/v1"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                reviewer.validate_api_base(value)

    def test_authenticated_provider_redirects_are_refused(self) -> None:
        request = urllib.request.Request(
            "https://gateway.example/v1/messages", data=b"{}",
            headers={"Authorization": "Bearer fake", "x-api-key": "fake"}, method="POST",
        )
        handler = reviewer.ProviderRedirectHandler()
        with self.assertRaises(urllib.error.HTTPError):
            handler.redirect_request(request, io.BytesIO(), 302, "Found", {}, "https://other.example/collect")


if __name__ == "__main__":
    unittest.main()
