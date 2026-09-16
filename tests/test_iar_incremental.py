#!/usr/bin/env python3
"""Incremental review mode (v2.1.0+): inline finding marker, prior-finding
retrieval from review threads, trusted delta, mode selection, delta-scaled
budget, incremental prompt, verdict channels, verified reconciliation,
thread resolution, footer, and the § 13.1 single-path cap."""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

_ROOT: Path = Path(__file__).resolve().parent.parent
_SPEC = importlib.util.spec_from_file_location("reviewer", _ROOT / "scripts" / "reviewer.py")
assert _SPEC is not None and _SPEC.loader is not None
reviewer = importlib.util.module_from_spec(_SPEC)
sys.modules["reviewer"] = reviewer
_SPEC.loader.exec_module(reviewer)

GT = reviewer.GenerationTransition


def _finding(path: str = "src/x.py", line: int = 10, body: str = "Null deref.", severity: str = "warning") -> Any:
    return reviewer.Finding(path=path, line=line, body=body, severity=severity)


def _prior(fp: str = "a" * 16, path: str = "src/x.py", line: int = 10, sev: str = "warning", tid: str = "T1", dbid: int = 101) -> Any:
    return reviewer.PriorFinding(thread_id=tid, comment_id="C1", comment_database_id=dbid, path=path, line=line, severity=sev, fingerprint=fp, body_excerpt="Null deref.", is_outdated=False)


def _state(**over: Any) -> Any:
    base: dict[str, Any] = dict(version=reviewer.IAR_STATE_SCHEMA_VERSION, generation=1, generation_range_hash="h1", round_in_generation=1,
                                policy_applied=reviewer.IAR_POLICY_FIRST_PASS_EXHAUSTIVE, resolved_fingerprints=[], open_fingerprints_this_gen=[],
                                history=[], base_sha="b" * 40, head_sha="1" * 40)
    base.update(over)
    return reviewer.IterationState(**base)


def _policy(applied: str = reviewer.IAR_POLICY_FIRST_PASS_EXHAUSTIVE, cap: int = 30, addendum: str = "X") -> Any:
    return reviewer.PolicyResult(findings_to_surface=[], findings_silenced=[], effective_max_inline_comments=cap, prompt_addendum=addendum, policy_applied=applied)


def _delta(files: tuple[str, ...] = ("src/x.py",), ratio: float = 0.2) -> Any:
    return reviewer.IncrementalDelta(prior_head_sha="1" * 40, head_sha="2" * 40, changed_files=files, delta_ratio=ratio)


def _pre(mode: str = "incremental", delta: Any = None, prior: tuple[Any, ...] = ()) -> Any:
    return reviewer.IARPreLLMContext(prior_state=_state(), transition=GT.NEW_COMMITS, base_sha="b" * 40, head_sha="2" * 40, range_hash="h2",
                                     new_lines_pct=20.0, pr_labels=[], pre_policy_result=_policy(), mode=mode, mode_reason="t",
                                     delta=delta if delta is not None else _delta(), prior_findings=prior or (_prior(),), effective_max_turns=6)


class InlineMarkerTests(unittest.TestCase):
    def test_render_and_parse_roundtrip(self) -> None:
        f = _finding(); f.fingerprint = "deadbeefcafebabe"
        comments = reviewer.findings_to_gh_inline_comments([f])
        body = comments[0]["body"]
        self.assertTrue(body.startswith("Null deref."))
        self.assertIn(reviewer.INLINE_FINDING_MARKER_PREFIX, body)
        self.assertEqual(reviewer.parse_inline_finding_marker(body), ("deadbeefcafebabe", "warning"))

    def test_no_fingerprint_no_marker(self) -> None:
        body = reviewer.findings_to_gh_inline_comments([_finding()])[0]["body"]
        self.assertEqual(body, "Null deref.")
        self.assertIsNone(reviewer.parse_inline_finding_marker(body))
        self.assertIsNone(reviewer.parse_inline_finding_marker("<!-- ai-pr-reviewer-finding: garbage -->"))

    def test_unknown_severity_downgrades_to_info(self) -> None:
        self.assertEqual(reviewer.parse_inline_finding_marker("x <!-- ai-pr-reviewer-finding: fp=abcdef1234 sev=weird -->"), ("abcdef1234", "info"))

    def test_marker_constant_is_registered_shape(self) -> None:
        self.assertTrue(reviewer.INLINE_FINDING_MARKER_PREFIX.startswith("<!-- ai-pr-reviewer-"))


def _thread(*, resolved: bool = False, login: str = "github-actions", review_body: str = "<!-- ai-pr-reviewer-provider: anthropic -->", body: str | None = None, path: str = "src/x.py", line: Any = 10, tid: str = "T1") -> dict[str, Any]:
    if body is None:
        body = "Null deref." + reviewer.render_inline_finding_marker("a" * 16, "warning")
    return {"id": tid, "isResolved": resolved, "isOutdated": False, "path": path, "line": line, "originalLine": 9,
            "comments": {"nodes": [{"id": "C1", "databaseId": 101, "body": body, "author": {"login": login}, "pullRequestReview": {"body": review_body}}]}}


def _gql(threads: list[dict[str, Any]], has_next: bool = False) -> dict[str, Any]:
    return {"repository": {"pullRequest": {"reviewThreads": {"pageInfo": {"hasNextPage": has_next}, "nodes": threads}}}}


class FetchPriorFindingsTests(unittest.TestCase):
    def _fetch(self, threads: list[dict[str, Any]], **kw: Any) -> list[Any]:
        with mock.patch.object(reviewer, "gh_graphql", return_value=_gql(threads, kw.pop("has_next", False))):
            return reviewer.fetch_prior_findings(token="t", repo="o/r", pr_number=1, bot_login=kw.pop("bot", "github-actions[bot]"), provider_marker_text=kw.pop("marker", "<!-- ai-pr-reviewer-provider: anthropic -->"))

    def test_open_marked_bot_thread_is_returned(self) -> None:
        got = self._fetch([_thread()])
        self.assertEqual(len(got), 1)
        pf = got[0]
        self.assertEqual((pf.fingerprint, pf.severity, pf.path, pf.line, pf.comment_database_id, pf.thread_id), ("a" * 16, "warning", "src/x.py", 10, 101, "T1"))
        self.assertEqual(pf.body_excerpt, "Null deref.")

    def test_filters(self) -> None:
        self.assertEqual(self._fetch([_thread(resolved=True)]), [])
        self.assertEqual(self._fetch([_thread(login="someone-else")]), [])
        self.assertEqual(self._fetch([_thread(review_body="<!-- ai-pr-reviewer-provider: codex -->")]), [])
        self.assertEqual(self._fetch([_thread(body="old comment without marker")]), [])
        # both bot login shapes accepted
        self.assertEqual(len(self._fetch([_thread(login="github-actions[bot]")])), 1)

    def test_line_falls_back_to_original_line(self) -> None:
        pf = self._fetch([_thread(line=None)])[0]
        self.assertEqual(pf.line, 9)

    def test_api_failure_returns_empty(self) -> None:
        with mock.patch.object(reviewer, "gh_graphql", side_effect=RuntimeError("boom")):
            self.assertEqual(reviewer.fetch_prior_findings(token="t", repo="o/r", pr_number=1, bot_login="b"), [])

    def test_missing_pagination_cursor_falls_back_to_full(self) -> None:
        with mock.patch.object(reviewer, "gh_graphql", return_value=_gql([_thread()], True)), mock.patch.object(reviewer, "log") as fake_log:
            result = reviewer.fetch_prior_findings(token="t", repo="o/r", pr_number=1, bot_login="github-actions")
        self.assertEqual(result, [], "partial history must not enable incremental mode")
        self.assertTrue(any("incomplete review-thread pagination" in str(c.args[0]) for c in fake_log.call_args_list))


class DeltaTests(unittest.TestCase):
    def _run(self, ancestor_rc: int, names: str = "src/x.py\0src/y.py\0") -> Any:
        def fake_run(argv: list[str], **kw: Any) -> Any:
            if argv[:3] == ["git", "merge-base", "--is-ancestor"]:
                return subprocess.CompletedProcess(argv, ancestor_rc, stdout="", stderr="")
            return subprocess.CompletedProcess(argv, 0, stdout=names, stderr="")
        with mock.patch.object(reviewer.subprocess, "run", side_effect=fake_run):
            return reviewer.compute_incremental_delta(prior_head_sha="1" * 40, head_sha="2" * 40, new_lines_pct=25.0)

    def test_trusted_delta(self) -> None:
        d = self._run(0)
        assert d is not None
        self.assertEqual(d.changed_files, ("src/x.py", "src/y.py"))
        self.assertAlmostEqual(d.delta_ratio, 0.25)

    def test_non_ancestor_is_untrusted(self) -> None:
        self.assertIsNone(self._run(1))

    def test_missing_prior_head(self) -> None:
        self.assertIsNone(reviewer.compute_incremental_delta(prior_head_sha="", head_sha="2" * 40, new_lines_pct=5.0))

    def test_git_failure_is_untrusted(self) -> None:
        with mock.patch.object(reviewer.subprocess, "run", side_effect=FileNotFoundError("git")):
            self.assertIsNone(reviewer.compute_incremental_delta(prior_head_sha="1" * 40, head_sha="2" * 40, new_lines_pct=5.0))


class ModeSelectionTests(unittest.TestCase):
    def test_matrix(self) -> None:
        cases = [
            (dict(prior_state=None, transition=GT.FIRST_REVIEW, pre_policy_result=_policy(), prior_findings=[_prior()], delta=_delta()), "full"),
            (dict(prior_state=_state(), transition=GT.USER_FORCED_RESET, pre_policy_result=_policy(), prior_findings=[_prior()], delta=_delta()), "full"),
            (dict(prior_state=_state(), transition=GT.NEW_COMMITS, pre_policy_result=_policy(reviewer.IAR_POLICY_ESCAPE_LABEL_FORCED), prior_findings=[_prior()], delta=_delta()), "full"),
            (dict(prior_state=_state(), transition=GT.NEW_COMMITS, pre_policy_result=_policy(reviewer.IAR_POLICY_SAFETY_NET_FORCED), prior_findings=[_prior()], delta=_delta()), "full"),
            (dict(prior_state=_state(), transition=GT.NEW_COMMITS, pre_policy_result=_policy(), prior_findings=[_prior()], delta=None), "full"),
            (dict(prior_state=_state(), transition=GT.NEW_COMMITS, pre_policy_result=_policy(), prior_findings=[], delta=_delta()), "full"),
            (dict(prior_state=_state(), transition=GT.NEW_COMMITS, pre_policy_result=_policy(), prior_findings=[_prior()], delta=_delta()), "incremental"),
            (dict(prior_state=_state(), transition=GT.SAME_GENERATION, pre_policy_result=_policy(reviewer.IAR_POLICY_ITERATIVE), prior_findings=[_prior()], delta=_delta(files=())), "incremental"),
        ]
        for kwargs, expected in cases:
            with self.subTest(expected=expected, t=kwargs["transition"].value, p=kwargs["pre_policy_result"].policy_applied):
                mode, reason = reviewer.select_iar_mode(**kwargs)
                self.assertEqual(mode, expected, reason)
                self.assertTrue(reason)


class BudgetScalingTests(unittest.TestCase):
    def test_floors_and_scaling(self) -> None:
        self.assertEqual(reviewer.scale_incremental_budget(base_cap=10, base_turns=30, delta_ratio=0.05, prior_critical=0), (3, 6))
        self.assertEqual(reviewer.scale_incremental_budget(base_cap=10, base_turns=30, delta_ratio=0.5, prior_critical=0), (5, 15))
        self.assertEqual(reviewer.scale_incremental_budget(base_cap=10, base_turns=30, delta_ratio=0.05, prior_critical=7), (7, 6))
        self.assertEqual(reviewer.scale_incremental_budget(base_cap=10, base_turns=30, delta_ratio=1.0, prior_critical=0), (10, 30))
        # never above the base budgets
        self.assertEqual(reviewer.scale_incremental_budget(base_cap=10, base_turns=30, delta_ratio=5.0, prior_critical=50), (10, 30))


class PreLlmIntegrationTests(unittest.TestCase):
    """run_iar_pre_llm selects incremental mode and rewrites the cap/addendum."""

    def _run(self, *, prior: list[Any], ancestor_rc: int = 0, prior_state: Any = None, labels: tuple[list[str], bool] = ([], True)) -> Any:
        def fake_run(argv: list[str], **kw: Any) -> Any:
            if argv[:2] == ["git", "rev-parse"]:
                return subprocess.CompletedProcess(argv, 0, stdout="b" * 40 + "\n", stderr="")
            if argv[:3] == ["git", "merge-base", "--is-ancestor"]:
                return subprocess.CompletedProcess(argv, ancestor_rc, stdout="", stderr="")
            if argv[:3] == ["git", "diff", "--name-only"]:
                return subprocess.CompletedProcess(argv, 0, stdout="src/x.py\0", stderr="")
            if argv[:3] == ["git", "diff", "--numstat"]:
                # three-dot = whole PR (200 lines); two-dot = new since prior head (20 lines) → 10 % (< 30 % safety net)
                whole = any("..." in a for a in argv)
                return subprocess.CompletedProcess(argv, 0, stdout=("200\t0\tsrc/x.py\n" if whole else "20\t0\tsrc/x.py\n"), stderr="")
            return subprocess.CompletedProcess(argv, 0, stdout="diff --git a/src/x.py b/src/x.py\n+new\n", stderr="")
        cfg = reviewer.IARConfig(policy=reviewer.IAR_POLICY_FIRST_PASS_EXHAUSTIVE, max_review_rounds=0, cap_multiplier=3, escape_label="full-review-please")
        with mock.patch.object(reviewer, "read_prior_iteration_state", return_value=prior_state), \
             mock.patch.object(reviewer, "_fetch_pr_labels", return_value=labels), \
             mock.patch.object(reviewer, "fetch_prior_findings", return_value=prior), \
             mock.patch.object(reviewer.subprocess, "run", side_effect=fake_run):
            return reviewer.run_iar_pre_llm(iar_config=cfg, repo="o/r", pr_number=1, gh_token="t", base_ref="main", head_sha="2" * 40,
                                            base_max_inline_comments=10, applied_label="", provider_id="anthropic", bot_login="b", max_turns=30)

    def test_first_review_stays_full_and_exhaustive(self) -> None:
        pre = self._run(prior=[], prior_state=None)
        self.assertEqual(pre.mode, "full")
        self.assertEqual(pre.pre_policy_result.effective_max_inline_comments, 30)
        self.assertEqual(pre.effective_max_turns, 0)

    def test_new_commits_with_prior_findings_goes_incremental(self) -> None:
        pre = self._run(prior=[_prior()], prior_state=_state(generation_range_hash="old"))
        self.assertEqual(pre.mode, "incremental")
        self.assertIn("incremental follow-up mode", pre.pre_policy_result.prompt_addendum)
        self.assertLess(pre.pre_policy_result.effective_max_inline_comments, 30)
        self.assertGreaterEqual(pre.pre_policy_result.effective_max_inline_comments, reviewer.IAR_INCREMENTAL_MIN_CAP)
        self.assertGreaterEqual(pre.effective_max_turns, reviewer.IAR_INCREMENTAL_MIN_TURNS)
        self.assertEqual(pre.pre_policy_result.policy_applied, reviewer.IAR_POLICY_FIRST_PASS_EXHAUSTIVE)
        assert pre.delta is not None
        self.assertEqual(pre.delta.changed_files, ("src/x.py",))

    def test_rebase_falls_back_to_full(self) -> None:
        pre = self._run(prior=[_prior()], prior_state=_state(generation_range_hash="old"), ancestor_rc=1)
        self.assertEqual(pre.mode, "full")
        self.assertIn("delta not trusted", pre.mode_reason)

    def test_escape_label_wins(self) -> None:
        pre = self._run(prior=[_prior()], prior_state=_state(generation_range_hash="old"), labels=(["full-review-please"], True))
        self.assertEqual(pre.mode, "full")


class IncrementalPromptTests(unittest.TestCase):
    DIFF = ("diff --git a/src/x.py b/src/x.py\n--- a/src/x.py\n+++ b/src/x.py\n@@ -1 +1 @@\n-a\n+b\n"
            "diff --git a/src/z.py b/src/z.py\n--- a/src/z.py\n+++ b/src/z.py\n@@ -1 +1 @@\n-c\n+d\n")

    def _ctx(self, pre: Any) -> Any:
        return reviewer.PRContext(title="t", author="a", head_ref="h", base_ref="main", state="open", additions=2, deletions=2, commits=2, body="",
                                  changed_files=[{"path": "src/x.py", "status": "modified", "additions": 1, "deletions": 1}, {"path": "src/z.py", "status": "modified", "additions": 1, "deletions": 1}],
                                  diff=self.DIFF, incremental=pre)

    def test_incremental_sections_replace_full_diff(self) -> None:
        prior = (_prior(fp="c" * 16, sev="critical", path="src/z.py", line=3), _prior(fp="a" * 16))
        text = reviewer.render_user_prompt(self._ctx(_pre(prior=prior)))
        self.assertNotIn("## Full Diff", text)
        self.assertIn(reviewer.IAR_INCREMENTAL_DIFF_HEADING, text)
        self.assertIn("diff --git a/src/x.py", text)
        self.assertNotIn("diff --git a/src/z.py", text, "unchanged-since-last-review files are summarised, not shown")
        self.assertIn(reviewer.IAR_UNCHANGED_FILES_HEADING, text)
        self.assertIn("- src/z.py (modified) +1/-1", text)
        self.assertIn(reviewer.PRIOR_FINDINGS_HEADING + " (2)", text)
        # criticals first; file-changed column correct
        rows = [l for l in text.splitlines() if l.startswith("| 1 |") or l.startswith("| 2 |")]
        self.assertIn("critical", rows[0]); self.assertIn("| no |", rows[0])
        self.assertIn("warning", rows[1]); self.assertIn("| yes |", rows[1])

    def test_full_mode_keeps_full_diff(self) -> None:
        text = reviewer.render_user_prompt(self._ctx(_pre(mode="full")))
        self.assertIn("## Full Diff", text)
        self.assertNotIn(reviewer.PRIOR_FINDINGS_HEADING, text)

    def test_empty_delta_states_no_code_changes(self) -> None:
        text = reviewer.render_user_prompt(self._ctx(_pre(delta=_delta(files=()))))
        self.assertIn("No code changes since your last review", text)

    def test_prior_findings_table_is_capped(self) -> None:
        many = tuple(_prior(fp=f"{i:016x}", line=i, tid=f"T{i}") for i in range(reviewer.PRIOR_FINDINGS_MAX_LISTED + 5))
        block = reviewer.render_prior_findings_block(many, changed_files=set())
        self.assertIn("… and 5 more", block)

    def test_agent_runner_directive_and_tool_exposure(self) -> None:
        d = reviewer.write_findings_prompt_directive("R", Path("/tmp/f.json"), prior_findings_expected=True)
        self.assertIn('"prior_findings"', d)
        self.assertIn("required", d)
        self.assertNotIn("prior_findings", reviewer.write_findings_prompt_directive("R", Path("/tmp/f.json")))
        names = {t["name"] for t in reviewer.tools_schema(10, allow_update_prior_finding=True)}
        self.assertIn("update_prior_finding", names)
        self.assertNotIn("update_prior_finding", {t["name"] for t in reviewer.tools_schema(10)})

    def test_pr_context_is_incremental(self) -> None:
        self.assertTrue(reviewer.pr_context_is_incremental(self._ctx(_pre())))
        self.assertFalse(reviewer.pr_context_is_incremental(self._ctx(None)))


class VerdictChannelsTests(unittest.TestCase):
    def test_tool_records_and_validates(self) -> None:
        state = reviewer.ReviewState()
        self.assertIn("Recorded", reviewer.execute_tool("update_prior_finding", {"fingerprint": "abc", "status": "Resolved", "note": "fixed in hunk"}, state))
        self.assertEqual(state.prior_finding_updates["abc"], ("resolved", "fixed in hunk"))
        self.assertTrue(reviewer.execute_tool("update_prior_finding", {"fingerprint": "abc", "status": "maybe"}, state).startswith("Error"))
        self.assertTrue(reviewer.execute_tool("update_prior_finding", {"status": "open"}, state).startswith("Error"))
        self.assertEqual(reviewer.state_to_review_result(state).prior_finding_updates, {"abc": ("resolved", "fixed in hunk")})

    def test_findings_json_prior_findings(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "f.json"
            p.write_text(json.dumps({"summary": "s", "findings": [], "prior_findings": [
                {"fingerprint": "abc", "status": "resolved", "note": "n"},
                {"fingerprint": "def", "status": "bogus"},
                {"fingerprint": "", "status": "open"},
                "garbage",
            ]}))
            with mock.patch.object(reviewer, "log"):
                res = reviewer.parse_findings_file(p)
            self.assertEqual(res.prior_finding_updates, {"abc": ("resolved", "n")})
            p.write_text(json.dumps({"summary": "s", "findings": [], "prior_findings": "nope"}))
            with mock.patch.object(reviewer, "log"):
                self.assertEqual(reviewer.parse_findings_file(p).prior_finding_updates, {})


class ReconciliationTests(unittest.TestCase):
    def _recon(self, status: str | None, *, fp_present: bool = False, file_changed: bool = True, file_exists: bool = True) -> Any:
        with tempfile.TemporaryDirectory() as td:
            if file_exists:
                (Path(td) / "src").mkdir(); (Path(td) / "src" / "x.py").write_text("x")
            updates = {"a" * 16: (status, "n")} if status else {}
            return reviewer.reconcile_prior_findings(prior_findings=(_prior(),), updates=updates,
                                                     current_fingerprints={"a" * 16} if fp_present else set(),
                                                     delta=_delta(files=("src/x.py",) if file_changed else ("other.py",)), workspace=Path(td))

    def test_resolution_claims_require_maintainer_confirmation(self) -> None:
        r = self._recon("resolved")
        self.assertEqual((len(r.resolved), len(r.still_open), len(r.unverified)), (0, 1, 1))
        r = self._recon("resolved", fp_present=True)
        self.assertEqual((len(r.resolved), len(r.still_open), len(r.unverified)), (0, 1, 1))
        r = self._recon("resolved", file_changed=False)
        self.assertEqual((len(r.resolved), len(r.unverified)), (0, 1))
        r = self._recon("resolved", file_changed=False, file_exists=False)
        self.assertEqual((len(r.resolved), len(r.still_open)), (0, 1), "deletion alone does not prove the failure was fixed")
        r = self._recon("open")
        self.assertEqual((len(r.still_open), len(r.resolved)), (1, 0))
        r = self._recon("regressed")
        self.assertEqual((len(r.regressed), len(r.still_open)), (1, 0))
        r = self._recon(None)
        self.assertEqual(len(r.still_open), 1)

    def test_close_resolved_threads_best_effort(self) -> None:
        recon = reviewer.PriorFindingReconciliation(resolved=[_prior(), _prior(fp="b" * 16, tid="T2", dbid=102)])
        calls: list[Any] = []
        with mock.patch.object(reviewer, "gh_graphql", side_effect=lambda q, v, token: (calls.append(v) or {"resolveReviewThread": {"thread": {"isResolved": v["id"] == "T1"}}})), \
             mock.patch.object(reviewer, "gh_request", return_value={}) as rest:
            n = reviewer.close_resolved_prior_findings(token="t", repo="o/r", pr_number=7, head_sha="abcdef1234", reconciliation=recon)
        self.assertEqual(n, 1)
        self.assertEqual([c["id"] for c in calls], ["T1", "T2"])
        self.assertEqual(rest.call_count, 2)
        self.assertIn("/pulls/7/comments/101/replies", rest.call_args_list[0].args[1])
        self.assertIn("abcdef1", rest.call_args_list[0].kwargs["body"]["body"])

    def test_close_never_raises(self) -> None:
        recon = reviewer.PriorFindingReconciliation(resolved=[_prior()])
        with mock.patch.object(reviewer, "gh_graphql", side_effect=RuntimeError("x")), mock.patch.object(reviewer, "gh_request", side_effect=RuntimeError("y")), mock.patch.object(reviewer, "log"):
            self.assertEqual(reviewer.close_resolved_prior_findings(token="t", repo="o/r", pr_number=1, head_sha="s", reconciliation=recon), 0)

    def test_footer_and_annotation(self) -> None:
        recon = reviewer.PriorFindingReconciliation(resolved=[_prior()], still_open=[_prior(fp="b" * 16)], regressed=[], unverified=[_prior(fp="b" * 16)])
        footer = reviewer.render_incremental_footer(delta=_delta(), reconciliation=recon, new_findings=2)
        self.assertIn("resolved 1 · still open 1 · regressed 0 · new 2", footer)
        self.assertIn("1 claimed resolved but unverified", footer)
        ann = reviewer._render_iar_marker_annotation(state=_state(), policy_result=_policy(), transition=GT.NEW_COMMITS, mode="incremental")
        self.assertIn("mode=incremental", ann)
        self.assertNotIn("mode=", reviewer._render_iar_marker_annotation(state=_state(), policy_result=_policy(), transition=GT.NEW_COMMITS))


class SinglePathCapTests(unittest.TestCase):
    """§ 13.1: agent-runner overflow is fingerprinted before the cap."""

    def test_overflow_recorded_as_open_and_criticals_kept(self) -> None:
        findings = [_finding(line=i, severity="info", body=f"f{i}") for i in range(1, 6)] + [_finding(line=99, severity="critical", body="crit")]
        result = reviewer.ReviewResult(summary="s", findings=list(findings), overall_severity="critical")
        pre = reviewer.IARPreLLMContext(prior_state=None, transition=GT.FIRST_REVIEW, base_sha="b" * 40, head_sha="2" * 40, range_hash="h", new_lines_pct=0.0, pr_labels=[],
                                        pre_policy_result=_policy(reviewer.IAR_POLICY_ITERATIVE, cap=3, addendum=""))
        cfg = reviewer.IARConfig(policy=reviewer.IAR_POLICY_ITERATIVE, max_review_rounds=0, cap_multiplier=1, escape_label="full-review-please")
        with mock.patch.object(reviewer, "_load_code_contexts_for_findings", return_value={}):
            state, pr = reviewer.run_iar_post_llm(iar_config=cfg, pre_context=pre, result=result, base_max_inline_comments=3, telemetry=reviewer.RunTelemetry(), surface_cap=3)
        self.assertEqual(len(result.findings), 3)
        self.assertEqual(result.findings[0].severity, "critical")
        self.assertEqual(len(state.open_fingerprints_this_gen), 6, "overflow findings are still recorded as open")
        for f in result.findings:
            self.assertTrue(f.fingerprint)

    def test_no_cap_on_chat_path(self) -> None:
        findings = [_finding(line=i, body=f"f{i}") for i in range(1, 6)]
        result = reviewer.ReviewResult(summary="s", findings=list(findings), overall_severity="warning")
        pre = reviewer.IARPreLLMContext(prior_state=None, transition=GT.FIRST_REVIEW, base_sha="b" * 40, head_sha="2" * 40, range_hash="h", new_lines_pct=0.0, pr_labels=[],
                                        pre_policy_result=_policy(reviewer.IAR_POLICY_ITERATIVE, cap=3, addendum=""))
        cfg = reviewer.IARConfig(policy=reviewer.IAR_POLICY_ITERATIVE, max_review_rounds=0, cap_multiplier=1, escape_label="x")
        with mock.patch.object(reviewer, "_load_code_contexts_for_findings", return_value={}):
            reviewer.run_iar_post_llm(iar_config=cfg, pre_context=pre, result=result, base_max_inline_comments=3, telemetry=reviewer.RunTelemetry())
        self.assertEqual(len(result.findings), 5)


if __name__ == "__main__":
    unittest.main()

class MarkerShaCoercionTests(unittest.TestCase):
    """Task 13 hardening: SHA fields from persisted marker state are argv
    tokens for `git diff` / `git merge-base` — only hex object ids pass."""

    def test_accepts_hex_object_ids(self) -> None:
        self.assertEqual(reviewer._coerce_git_sha("ABCDEF1234"), "abcdef1234")
        self.assertEqual(reviewer._coerce_git_sha(" " + "a" * 40 + " "), "a" * 40)
        self.assertEqual(reviewer._coerce_git_sha("0" * 64), "0" * 64)

    def test_rejects_options_and_garbage(self) -> None:
        for bad in ("--output=/tmp/pwn", "-", "HEAD", "main..evil", "abc", "", None, 12, ["a" * 40], "a" * 65, "zzzz1234"):
            self.assertEqual(reviewer._coerce_git_sha(bad), "", repr(bad))

    def test_parser_drops_poisoned_shas(self) -> None:
        """Embed a real state, then poison the persisted JSON the way an
        editor of the tracking comment could, and parse it back."""
        good = "b" * 40
        state = reviewer.IterationState(
            version=reviewer.IAR_STATE_SCHEMA_VERSION, generation=1,
            generation_range_hash="abc123", round_in_generation=1,
            policy_applied=reviewer.IAR_POLICY_ITERATIVE, resolved_fingerprints=[],
            open_fingerprints_this_gen=[], history=[], base_sha=good, head_sha=good,
        )
        body = reviewer.embed_iteration_state("### Tracking marker\n", state)
        poisoned = body.replace(json.dumps(good)[1:-1], "--output=/tmp/pwn", 1)
        parsed = reviewer._parse_state_from_marker_body(poisoned)
        self.assertIsNotNone(parsed)
        self.assertEqual(parsed.base_sha, "")
        self.assertEqual(parsed.head_sha, good)


class ResolutionPolicyTests(unittest.TestCase):
    """v2.2.0: `prior-findings-resolution` — advisory (default, unchanged) vs
    verified (runtime-corroborated auto-resolution)."""

    def _recon(self, status: str | None, *, policy: str, fp_present: bool = False, file_changed: bool = True, file_exists: bool = True) -> Any:
        with tempfile.TemporaryDirectory() as td:
            if file_exists:
                (Path(td) / "src").mkdir(); (Path(td) / "src" / "x.py").write_text("x")
            updates = {"a" * 16: (status, "n")} if status else {}
            return reviewer.reconcile_prior_findings(prior_findings=(_prior(),), updates=updates,
                                                     current_fingerprints={"a" * 16} if fp_present else set(),
                                                     delta=_delta(files=("src/x.py",) if file_changed else ("other.py",)),
                                                     workspace=Path(td), policy=policy)

    def test_parse_policy(self) -> None:
        self.assertEqual(reviewer.parse_resolution_policy(""), "advisory")
        self.assertEqual(reviewer.parse_resolution_policy(" Verified "), "verified")
        with self.assertRaises(ValueError):
            reviewer.parse_resolution_policy("auto")

    def test_advisory_never_resolves(self) -> None:
        r = self._recon("resolved", policy="advisory")
        self.assertEqual((len(r.resolved), len(r.still_open), len(r.unverified)), (0, 1, 1))

    def test_verified_matrix(self) -> None:
        self.assertEqual(len(self._recon("resolved", policy="verified").resolved), 1)
        r = self._recon("resolved", policy="verified", fp_present=True)
        self.assertEqual((len(r.resolved), len(r.still_open), len(r.unverified)), (0, 1, 1), "fingerprint still present → not resolved")
        r = self._recon("resolved", policy="verified", file_changed=False)
        self.assertEqual((len(r.resolved), len(r.unverified)), (0, 1), "file untouched → not resolved")
        self.assertEqual(len(self._recon("resolved", policy="verified", file_changed=False, file_exists=False).resolved), 1, "deleted file counts as changed")
        self.assertEqual(len(self._recon("open", policy="verified").still_open), 1)
        self.assertEqual(len(self._recon("regressed", policy="verified").regressed), 1)

    def test_apply_policy_touches_threads_only_when_verified(self) -> None:
        recon = reviewer.PriorFindingReconciliation(resolved=[_prior()])
        with mock.patch.object(reviewer, "close_resolved_prior_findings", return_value=1) as close:
            self.assertEqual(reviewer.apply_resolution_policy(policy="advisory", reconciliation=recon, token="t", repo="o/r", pr_number=1, head_sha="deadbeef"), 0)
            close.assert_not_called()
            self.assertEqual(reviewer.apply_resolution_policy(policy="verified", reconciliation=recon, token="t", repo="o/r", pr_number=1, head_sha="deadbeef"), 1)
            close.assert_called_once()

    def test_footer_names_policy_only_when_not_advisory(self) -> None:
        recon = reviewer.PriorFindingReconciliation(resolved=[_prior()])
        d = _delta(files=("src/x.py",))
        self.assertNotIn("policy:", reviewer.render_incremental_footer(delta=d, reconciliation=recon, new_findings=0))
        self.assertIn("policy: verified", reviewer.render_incremental_footer(delta=d, reconciliation=recon, new_findings=0, policy="verified"))


class IncrementalTruncationHintTests(unittest.TestCase):
    """The incremental delta's truncation notice carries the read-the-rest
    hint (tool-name-neutral), like the full-diff path does."""

    def test_truncated_delta_carries_read_hint(self) -> None:
        big = "diff --git a/src/auth.py b/src/auth.py\n--- a/src/auth.py\n+++ b/src/auth.py\n@@ -1 +1 @@\n" + ("+x\n" * (reviewer.MAX_DIFF_CHARS // 3 + 10))
        state = reviewer.new_iteration_state(generation_range_hash="old", base_sha="b" * 40, head_sha="1" * 40, policy_applied=reviewer.IAR_POLICY_ITERATIVE)
        pre = reviewer.IARPreLLMContext(
            prior_state=state, transition=reviewer.GenerationTransition.SAME_GENERATION,
            base_sha="b" * 40, head_sha="2" * 40, range_hash="new", new_lines_pct=1.0, pr_labels=[],
            pre_policy_result=reviewer.PolicyResult(findings_to_surface=[], findings_silenced=[], effective_max_inline_comments=3, prompt_addendum="", policy_applied=reviewer.IAR_POLICY_ITERATIVE),
            mode=reviewer.IAR_MODE_INCREMENTAL, prior_findings=(),
            delta=reviewer.IncrementalDelta(prior_head_sha="1" * 40, head_sha="2" * 40, changed_files=("src/auth.py",), delta_ratio=0.5, diff=big),
        )
        pr = reviewer.PRContext(title="t", author="a", head_ref="feature", base_ref="main", state="open", additions=1, deletions=1, commits=2, body="",
                                changed_files=[{"path": "src/auth.py", "status": "modified", "additions": 1, "deletions": 0}], diff=big, incremental=pre)
        rendered = reviewer.render_incremental_sections(pr, pre)
        self.assertIn("diff truncated at", rendered)
        self.assertIn("use your file-reading tool", rendered)
