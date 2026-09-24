"""Task 26 — RFC-06 § Incremental rounds (BC-13, incremental half): the
delta-scaled turn budget, the verifier-only round when nothing changed, and
the rails that still force a full round with the full budget.
"""
from __future__ import annotations

import importlib.util
import subprocess
import sys
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


class Formula(unittest.TestCase):
    def test_rfc_table_rows(self) -> None:
        b = reviewer.incremental_budget
        self.assertEqual(b(2, 3, 20), 10, "1–2 files, 3 outstanding → 4 + 3 + 3")
        self.assertEqual(b(1, 3, 20), 9)
        self.assertEqual(b(6, 5, 20), 18, "4 + 9 + 5")
        self.assertEqual(b(6, 5, 12), 12, "capped at the tier ceiling")
        self.assertEqual(b(0, 0, 20), reviewer.INCREMENTAL_TURN_FLOOR)
        self.assertEqual(b(0, 3, 20), 7, "no files, 3 outstanding: the number exists but the round is verifier-only")
        self.assertEqual(b(40, 40, 30), 30)
        self.assertEqual(b(3, 1, 0), 10, "no ceiling → the formula alone")
        self.assertEqual(b(0, 0, 2), 2, "a ceiling below the floor yields the ceiling")

    def test_constants_are_the_rfc_values(self) -> None:
        self.assertEqual((reviewer.INCREMENTAL_TURN_FLOOR, reviewer.INCREMENTAL_TURNS_PER_FILE, reviewer.INCREMENTAL_TURNS_PER_OPEN), (4, 1.5, 1.0))


def _prior(fp: str = "a" * 16, path: str = "src/x.py", line: int = 10, sev: str = "warning") -> Any:
    return reviewer.PriorFinding(thread_id="T1", comment_id="C1", comment_database_id=101, path=path, line=line, severity=sev,
                                 fingerprint=fp, body_excerpt="Null deref.", is_outdated=False)


def _state() -> Any:
    return reviewer.new_iteration_state(generation=1, generation_range_hash="old", round_in_generation=1,
                                        policy_applied=reviewer.IAR_POLICY_FIRST_PASS_EXHAUSTIVE, base_sha="b" * 40, head_sha="1" * 40)


class PreLlmWiring(unittest.TestCase):
    """run_iar_pre_llm with the git layer faked: the delta's file count drives the budget."""

    def _run(self, *, prior: list[Any], files: list[str], labels: tuple[list[str], bool] = ([], True), max_turns: int = 30, state: Any = None) -> Any:
        def fake_run(argv: list[str], **kw: Any) -> Any:
            if argv[:2] == ["git", "rev-parse"]:
                return subprocess.CompletedProcess(argv, 0, stdout="b" * 40 + "\n", stderr="")
            if argv[:3] == ["git", "merge-base", "--is-ancestor"]:
                return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")
            if argv[:3] == ["git", "diff", "--name-only"]:
                return subprocess.CompletedProcess(argv, 0, stdout="".join(f + "\0" for f in files), stderr="")
            if argv[:3] == ["git", "diff", "--numstat"]:
                whole = any("..." in a for a in argv)
                rows = "".join(f"200\t0\t{f}\n" if whole else f"20\t0\t{f}\n" for f in (files or ["src/x.py"]))
                return subprocess.CompletedProcess(argv, 0, stdout=rows if files else ("200\t0\tsrc/x.py\n" if whole else ""), stderr="")
            return subprocess.CompletedProcess(argv, 0, stdout="".join(f"diff --git a/{f} b/{f}\n+new\n" for f in files), stderr="")
        cfg = reviewer.IARConfig(policy=reviewer.IAR_POLICY_FIRST_PASS_EXHAUSTIVE, max_review_rounds=0, cap_multiplier=3, escape_label="full-review-please")
        with mock.patch.object(reviewer, "read_prior_iteration_state", return_value=state if state is not None else _state()), \
             mock.patch.object(reviewer, "_fetch_pr_labels", return_value=labels), \
             mock.patch.object(reviewer, "fetch_prior_findings", return_value=prior), \
             mock.patch.object(reviewer.subprocess, "run", side_effect=fake_run):
            return reviewer.run_iar_pre_llm(iar_config=cfg, repo="o/r", pr_number=1, gh_token="t", base_ref="main", head_sha="2" * 40,
                                            base_max_inline_comments=10, applied_label="", provider_id="anthropic", bot_login="b", max_turns=max_turns)

    def test_two_files_three_outstanding_is_ten_turns(self) -> None:
        pre = self._run(prior=[_prior("a" * 16), _prior("b" * 16, line=20), _prior("c" * 16, line=30)], files=["src/x.py", "src/y.py"])
        self.assertEqual((pre.mode, pre.effective_max_turns, pre.verifier_only), ("incremental", 10, False))

    def test_budget_is_capped_by_the_full_budget(self) -> None:
        pre = self._run(prior=[_prior(chr(97 + i) * 16, line=10 + i) for i in range(5)], files=[f"src/f{i}.py" for i in range(6)], max_turns=12)
        self.assertEqual(pre.effective_max_turns, 12)

    def test_no_changed_file_is_a_verifier_only_round(self) -> None:
        pre = self._run(prior=[_prior()], files=[])
        self.assertEqual(pre.mode, "incremental")
        self.assertTrue(pre.verifier_only)
        self.assertIn("verifier-only", pre.mode_reason)

    def test_escape_label_forces_a_full_round_with_the_full_budget(self) -> None:
        pre = self._run(prior=[_prior()], files=["src/x.py"], labels=(["full-review-please"], True))
        self.assertEqual((pre.mode, pre.effective_max_turns, pre.verifier_only), ("full", 0, False))

    def test_new_generation_forces_a_full_round(self) -> None:
        # first review of the PR (no prior state) → full, full budget
        with mock.patch.object(reviewer, "read_prior_iteration_state", return_value=None):
            pre = self._run(prior=[], files=["src/x.py"], state=None)
        self.assertEqual((pre.mode, pre.effective_max_turns), ("full", 0))


SUPPORTS = [{"kind": "read_anchor", "result": "supports", "target": "src/x.py:10"}]
CONTRADICTS = [{"kind": "read_anchor", "result": "contradicts", "target": "src/x.py:20"}]


class _Verifier(reviewer.Provider):
    def __init__(self) -> None:
        self.calls = 0
        self.profile = reviewer.resolve_endpoint_profile("https://api.x.ai/v1", "openai")

    def complete(self, *, system_prompt: str, messages: list[dict[str, Any]], tools: list[dict[str, Any]]) -> dict[str, Any]:
        self.calls += 1
        text = str(messages)
        verdict = {"status": "refuted", "reason": "guard exists at 20", "checks": CONTRADICTS} if ":20" in text or "line 20" in text else {"status": "verified", "reason": "still there", "checks": SUPPORTS}
        return {"stop_reason": "tool_use", "content": [{"type": "tool_use", "id": "v", "name": reviewer.VERIFIER_VERDICT_TOOL, "input": verdict}],
                "usage": {"input_tokens": 100, "output_tokens": 10}}


class VerifierOnlyRound(unittest.TestCase):
    def test_outstanding_anchors_are_reread_and_reported(self) -> None:
        priors = (_prior("a" * 16, line=10), _prior("b" * 16, line=20))
        prov = _Verifier()
        report, verdicts = reviewer.verify_outstanding_findings(priors, policy=reviewer.VerifierPolicy(), provider=prov, model="m", alias="economy", endpoint_kind="xai", unavailable_reason="", inventory=None)
        self.assertEqual((report.runs, report.verified, report.refuted, prov.calls), (2, 1, 1, 2))
        self.assertGreater(report.usage.input_tokens, 0)
        narrative = reviewer.render_verifier_only_narrative(verdicts, prior_head="1" * 40)
        self.assertIn("spent no review turns", narrative); self.assertIn("`src/x.py:20` (warning) — **refuted**", narrative); self.assertIn("stay open until the maintainer", narrative)

    def test_without_a_verifier_everything_is_unverified_and_nothing_crashes(self) -> None:
        report, verdicts = reviewer.verify_outstanding_findings((_prior(),), policy=reviewer.VerifierPolicy(), provider=None, model="", alias="", endpoint_kind="", unavailable_reason="no backend", inventory=None)
        self.assertEqual((report.runs, report.unverified, verdicts[0][1].status, verdicts[0][1].reason), (0, 1, "unverified", "no backend"))
        self.assertIn("nothing to re-verify", reviewer.render_verifier_only_narrative([], prior_head="x" * 40))


class NoChangeRoundThroughMain(unittest.TestCase):
    """`main` on a verifier-only round: no provider is built, no review turns run, the verifier
    re-reads the outstanding anchors, the ledger is posted, the run record says turns 0."""

    def test_main_skips_the_model_and_posts_the_ledger(self) -> None:
        import contextlib, io, json, os, tempfile
        HEAD = "2" * 40
        prior = _prior("a" * 16, line=10)
        pre = reviewer.IARPreLLMContext(
            prior_state=_state(), transition=reviewer.GenerationTransition.NEW_COMMITS, base_sha="b" * 40, head_sha=HEAD, range_hash="h",
            new_lines_pct=0.0, pr_labels=[], pre_policy_result=reviewer.PolicyResult(findings_to_surface=[], findings_silenced=[], effective_max_inline_comments=3,
            prompt_addendum=reviewer.IAR_INCREMENTAL_PROMPT_ADDENDUM, policy_applied=reviewer.IAR_POLICY_ITERATIVE),
            mode=reviewer.IAR_MODE_INCREMENTAL, mode_reason="no code changes — verifier-only", prior_findings=(prior,),
            delta=reviewer.IncrementalDelta(prior_head_sha="1" * 40, head_sha=HEAD, changed_files=(), delta_ratio=0.0), effective_max_turns=0, verifier_only=True,
        )
        changed = [{"path": "src/x.py", "status": "modified", "additions": 1, "deletions": 0, "omitted": False}]
        inv = reviewer.ChangeInventory(head_sha=HEAD, base_sha="b" * 40, base_resolved=True, files=[{**changed[0], "previous_path": None, "binary": False, "mode_change": False, "patch_chars": 10}])
        ctx = reviewer.PRContext(title="T", author="dev", head_ref="f", base_ref="main", state="open", additions=1, deletions=0, commits=1, body="B",
                                 changed_files=changed, diff="diff --git a/src/x.py b/src/x.py\n--- a/src/x.py\n+++ b/src/x.py\n@@ -1 +1 @@\n-a\n+b\n", omitted_files=[], inventory=inv)
        submissions: list[Any] = []
        requests: list[tuple[str, str]] = []

        def fake_gh(method: str, path: str, *, token: str, body: Any = None) -> Any:
            requests.append((method, path))
            if method == "GET":
                return [] if any(k in path for k in ("comments", "reviews", "labels", "timeline")) else {}
            return {"id": 42}

        def fake_submit(**kw: Any) -> tuple[dict[str, Any], int]:
            submissions.append(kw["result"]); return {"html_url": "https://example.test/r/1"}, 0
        verifier = _Verifier()
        keep = {k: v for k, v in os.environ.items() if k in ("PATH", "HOME")}
        with tempfile.TemporaryDirectory() as td:
            env = {**keep, "AIPRR_PROVIDER": "grok", "AIPRR_API_KEY": "xai-k", "AIPRR_GH_TOKEN": "t", "AIPRR_REPO": "o/r", "AIPRR_PR_NUMBER": "7", "AIPRR_HEAD_SHA": HEAD,
                   "AIPRR_BASE_REF": "main", "AIPRR_STRICTNESS": "block-on-critical", "AIPRR_AUTHOR_ASSOCIATION": "", "AIPRR_LABEL_GATE": "",
                   "AIPRR_ACTION_PATH": str(_ROOT), "GITHUB_OUTPUT": str(Path(td) / "o.txt")}
            cwd = os.getcwd(); os.chdir(td); buf = io.StringIO()
            try:
                with mock.patch.dict(os.environ, env, clear=True), \
                     mock.patch.object(reviewer, "gh_request", side_effect=fake_gh), mock.patch.object(reviewer, "gh_graphql", return_value={}), \
                     mock.patch.object(reviewer, "gh_get_authenticated_login", return_value="github-actions[bot]"), \
                     mock.patch.object(reviewer, "fetch_pr_context", return_value=ctx), mock.patch.object(reviewer, "run_iar_pre_llm", return_value=pre), \
                     mock.patch.object(reviewer, "build_provider", side_effect=AssertionError("no review provider on a verifier-only round")), \
                     mock.patch.object(reviewer, "build_verifier_provider", return_value=(verifier, "grok-4.5", "economy", "xai", "")), \
                     mock.patch.object(reviewer, "gh_submit_review_with_fallback", side_effect=fake_submit), \
                     contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
                    code = reviewer.main()
            finally:
                os.chdir(cwd)
            record = json.loads((Path(td) / ".aiprr" / "run-record.json").read_text())
        self.assertEqual(code, 0, buf.getvalue()[-800:])
        self.assertEqual(len(submissions), 1)
        self.assertEqual(submissions[0].findings, [])
        self.assertIn("spent no review turns", submissions[0].summary)
        self.assertEqual((verifier.calls, record["budget"]["turns_used"], record["budget"]["verifier_runs"]), (1, 0, 1))
        self.assertIn("verifier-only round", buf.getvalue())


if __name__ == "__main__":
    unittest.main()
