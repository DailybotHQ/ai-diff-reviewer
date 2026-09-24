"""`run_eval.py --tree`: fixture-tree reviews without GitHub, scored on case labels.

Also locks `build_pr_context_from_local` (the runtime-side builder the
harness and the v3 change inventory share) and that `fetch_pr_context` is
untouched by it (signature + the diff-shaping call remain identical).
"""

from __future__ import annotations

import importlib.util
import inspect
import json
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any

_ROOT: Path = Path(__file__).resolve().parent.parent
_RE = importlib.util.spec_from_file_location("run_eval", _ROOT / "tests" / "eval" / "run_eval.py")
assert _RE is not None and _RE.loader is not None
run_eval = importlib.util.module_from_spec(_RE)
_RE.loader.exec_module(run_eval)
reviewer = run_eval.load_runtime()

_SC = importlib.util.spec_from_file_location("schema_check", _ROOT / "tests" / "eval" / "schema_check.py")
assert _SC is not None and _SC.loader is not None
schema_check = importlib.util.module_from_spec(_SC)
_SC.loader.exec_module(schema_check)
RUN_SCHEMA: dict[str, Any] = json.loads((_ROOT / "tests" / "eval" / "schemas" / "run-record.schema.json").read_text())

CASE: dict[str, Any] = {
    "schema": "ai-diff-reviewer/eval-case/1", "id": "C900", "title": "synthetic",
    "stack": "python", "change_class": "auth", "risk_class": "critical_positive", "family_group": "G900",
    "fixture": {
        "kind": "trees",
        "base": {"app.py": "def delete_user(u):\n    require_admin()\n    return remove(u)\n", "README.md": "docs\n", "gone.txt": "x\n"},
        "head": {"app.py": "def delete_user(u):\n    return remove(u)\n", "README.md": "docs\n"},
        "pr_metadata": {"title": "cleanup", "body": "tidy", "deceptive": False},
        "revision_pin": "fixture:sha256:0",
    },
    "labels": [
        {"id": "C900-d1", "severity": "critical", "path": "app.py", "line": 2, "keywords": ["require_admin", "authorization"], "window": 25},
        {"id": "C900-t1", "severity": "warning", "path": "README.md", "keywords": ["typo"], "window": 25},
    ],
    "expected": {"must_flag": ["C900-d1"], "must_not_flag": ["C900-t1"]},
    "adjudication": {"status": "pending"}, "rights": "synthetic",
}


class _PostingProvider(reviewer.Provider):
    """Posts the given findings on the first turn, submits on the second."""

    def __init__(self, findings: list[dict[str, Any]]) -> None:
        self._findings = findings
        self.calls = 0
        self.profile = reviewer.resolve_endpoint_profile("https://api.x.ai/v1", "openai")

    def complete(self, *, system_prompt: str, messages: list[dict[str, Any]], tools: list[dict[str, Any]]) -> dict[str, Any]:
        self.calls += 1
        if self.calls == 1:
            return {"stop_reason": "tool_use", "content": [
                {"type": "tool_use", "id": f"c{i}", "name": "post_inline_comment", "input": f} for i, f in enumerate(self._findings)
            ], "usage": {"input_tokens": 100, "output_tokens": 20}}
        return {"stop_reason": "tool_use", "content": [{"type": "tool_use", "id": "s", "name": "submit_review", "input": {"summary": "done"}}],
                "usage": {"input_tokens": 50, "output_tokens": 5}}


def _write_case(tmp: Path) -> Path:
    p = tmp / "C900.json"; p.write_text(json.dumps(CASE)); return p


class MaterialiseTreeTests(unittest.TestCase):
    def test_two_commits_and_deleted_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo, base, head = run_eval.materialise_tree(CASE, Path(tmp))
            self.assertNotEqual(base, head)
            names = run_eval._git(repo, "diff", "--name-status", f"{base}...{head}")
            self.assertIn("M\tapp.py", names)
            self.assertIn("D\tgone.txt", names)
            self.assertNotIn("README.md", names)

    def test_path_escape_rejected(self) -> None:
        bad = json.loads(json.dumps(CASE)); bad["fixture"]["head"]["../evil.txt"] = "x"
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(ValueError):
                run_eval.materialise_tree(bad, Path(tmp))


class LocalContextTests(unittest.TestCase):
    def test_build_pr_context_from_local_shapes_like_fetch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo, base, head = run_eval.materialise_tree(CASE, Path(tmp))
            ctx = reviewer.build_pr_context_from_local(base_sha=base, head_sha=head, repo_root=str(repo), title="t", body="b")
        paths = {f["path"]: f["status"] for f in ctx.changed_files}
        self.assertEqual(paths, {"app.py": "modified", "gone.txt": "removed"})
        self.assertIn("require_admin", ctx.diff)
        self.assertEqual((ctx.title, ctx.body, ctx.base_ref, ctx.head_ref), ("t", "b", base, head))
        self.assertEqual(ctx.omitted_files, [])

    def test_fetch_pr_context_signature_unchanged(self) -> None:
        params = list(inspect.signature(reviewer.fetch_pr_context).parameters)
        self.assertEqual(params, ["repo", "pr_number", "base_ref", "token", "ignore_globs"])


class RunCaseTests(unittest.TestCase):
    def test_planted_defect_is_recalled_and_record_is_valid(self) -> None:
        provider = _PostingProvider([{"path": "app.py", "line": 2, "body": "The require_admin check was removed — authorization bypass.", "severity": "critical"}])
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "out" / "C900.json"
            payload = run_eval.run_case(case_path=_write_case(Path(tmp)), provider=provider, runtime=reviewer, system_prompt="sys",
                                        max_turns=5, out=out, provider_id="openai", model="grok-4.5", api_base="https://api.x.ai/v1")
            record = json.loads(Path(str(out) + ".run-record.json").read_text())
        self.assertEqual((payload["score"]["must_find_hits"], payload["score"]["must_find_total"]), (1, 1))
        self.assertEqual(payload["score"]["false_positives"], [])
        self.assertEqual(schema_check.validate(RUN_SCHEMA, record), [])
        self.assertEqual(record["context"]["repo_kind"], "fixture_tree")
        self.assertEqual(record["context"]["corpus_case_id"], "C900")
        self.assertEqual(record["outcome"]["score"]["must_find_hits"], 1)
        self.assertEqual(record["status"], "completed")
        self.assertEqual(record["endpoint_kind"], "xai")
        self.assertTrue(record["usage_known"])

    def test_trap_hit_counts_as_false_positive(self) -> None:
        provider = _PostingProvider([{"path": "README.md", "line": 1, "body": "typo in docs", "severity": "info"}])
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "C900.json"
            payload = run_eval.run_case(case_path=_write_case(Path(tmp)), provider=provider, runtime=reviewer, system_prompt="sys",
                                        max_turns=5, out=out, provider_id="openai", model="grok-4.5")
        self.assertEqual(payload["score"]["false_positives"], ["C900-t1"])
        self.assertEqual(payload["score"]["must_find_hits"], 0)

    def test_labels_map_to_corpus_entry(self) -> None:
        entry = run_eval.case_labels_as_corpus_entry(CASE)
        self.assertEqual([l["id"] for l in entry["must_find"]], ["C900-d1"])
        self.assertEqual([l["id"] for l in entry["must_not_flag"]], ["C900-t1"])
        self.assertEqual(entry["acceptable"], [])

    def test_real_corpus_case_materialises(self) -> None:
        case = json.loads((_ROOT / "tests" / "eval" / "cases" / "C001.json").read_text())
        with tempfile.TemporaryDirectory() as tmp:
            repo, base, head = run_eval.materialise_tree(case, Path(tmp))
            ctx = reviewer.build_pr_context_from_local(base_sha=base, head_sha=head, repo_root=str(repo))
        self.assertGreaterEqual(len(ctx.changed_files), 1)
        self.assertIn("app.py", ctx.diff)



class PrPathFailedRecord(unittest.TestCase):
    def test_setup_crash_still_writes_a_failed_run_record(self) -> None:
        import argparse
        import os
        rt = run_eval.load_runtime()

        def boom(**kw: Any) -> Any:
            raise ConnectionError("IncompleteRead(454103 bytes read, 4888 more expected)")

        rt.fetch_pr_context = boom
        orig_load, orig_token, cwd = run_eval.load_runtime, run_eval.gh_token, os.getcwd()
        run_eval.load_runtime, run_eval.gh_token = (lambda: rt), (lambda: "gh-token")
        os.environ["AIPRR_TEST_EVAL_KEY"] = "k"
        try:
            with tempfile.TemporaryDirectory() as tmp:
                out = Path(tmp) / "pr1-r0.json"
                args = argparse.Namespace(tree=None, repo="o/r", pr=1, worktree=tmp, out=str(out), api_key_env="AIPRR_TEST_EVAL_KEY",
                                          provider="grok", model="", api_base="", base_ref="main",
                                          prompt=str(_ROOT / "prompts" / "default.md"), extension="", max_turns=3)
                with self.assertRaises(ConnectionError):
                    run_eval.run(args)
                record = json.loads(Path(str(out) + ".run-record.json").read_text())
        finally:
            run_eval.load_runtime, run_eval.gh_token = orig_load, orig_token
            os.chdir(cwd)
            os.environ.pop("AIPRR_TEST_EVAL_KEY", None)
        self.assertEqual(record["status"], "failed")
        self.assertEqual(record["failure_class"], "github_api")
        self.assertEqual(record["provider"], "grok")
        self.assertFalse(record["usage_known"])
        self.assertEqual(schema_check.validate(RUN_SCHEMA, record, label="failed"), [])

if __name__ == "__main__":
    unittest.main()
