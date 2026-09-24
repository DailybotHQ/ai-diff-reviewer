"""Task 23 — `mode: aggregate` end to end (RFC-04 § Publishing / § Failure modes):
`main` reads the leg documents from `AIPRR_ARTIFACT_DIR`, consolidates,
publishes exactly once with the aggregate marker, writes the legs outputs
and the job summary, and behaves as decided on every failure-mode row —
timeout leg (n−1, named; `require-all-legs` blocks), stale SHA ignored,
duplicate leg (newest wins), none delivered (red), invalid document.

Fake GitHub at the transport (`gh_request` / `gh_graphql`), `fetch_pr_context`
replaced by a local PRContext, the review submission recorded.
"""
from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import os
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

HEAD = "h" * 40
A, B, C = "grok|xai|grok-4.5", "claude-code|zai|glm-5.3-flash", "openai|xai|grok-4.5"


def _finding(path: str, line: int, sev: str, title: str, *, status: str = "unverified") -> Any:
    f = reviewer.Finding(path=path, line=line, body=f"{title}. Details.", severity=sev, title=title)
    f.severity_claimed = sev
    checks = [{"kind": "read_anchor", "result": "supports", "target": f"{path}:{line}"}] if status == "verified" else []
    f.verification = reviewer.FindingVerification(status=status, reason="r", checks=checks)
    f.evidence.anchor_sha256 = reviewer.hashlib.sha256(f"{path}:{line}".encode()).hexdigest()[:16]
    return f


def _document(leg: str, findings: list[Any], *, status: str = "completed", head: str = HEAD, recorded_at: str = "2026-09-24T00:00:00Z") -> dict[str, Any]:
    provider, kind, model = leg.split("|")
    rec = reviewer.RunRecord(); rec.provider, rec.endpoint_kind, rec.model, rec.head_sha = provider, kind, model, head
    rec.usage = reviewer.UsageTelemetry(input_tokens=500, output_tokens=20, source=reviewer.USAGE_SOURCE_API)
    ctx = reviewer.ReviewOutputContext(result=reviewer.ReviewResult(findings=findings, summary=f"narrative {leg}"), role="emit", narrative=f"narrative {leg}")
    doc = reviewer.build_review_output(run_doc=rec.to_dict(status=status, failure_class=None), ctx=ctx)
    doc = json.loads(reviewer.finalize_review_output(doc, hosts=()))
    doc["run"]["recorded_at"] = recorded_at
    return doc


def _pr_context() -> Any:
    changed = [{"path": "a.py", "status": "modified", "additions": 3, "deletions": 0, "omitted": False}]
    inv = reviewer.ChangeInventory(head_sha=HEAD, base_sha="b" * 40, base_resolved=True,
                                   files=[{**changed[0], "previous_path": None, "binary": False, "mode_change": False, "patch_chars": 40}])
    return reviewer.PRContext(title="T", author="dev", head_ref="feat", base_ref="main", state="open", additions=3, deletions=0, commits=1,
                              body="Body", changed_files=changed, diff="diff --git a/a.py b/a.py\n--- a/a.py\n+++ b/a.py\n@@ -1,3 +1,3 @@\n-x\n+y\n", omitted_files=[], inventory=inv)


class _Harness:
    """Runs `reviewer.main()` in aggregate mode inside a temp workspace with fakes."""

    def __init__(self, docs: list[tuple[str, dict[str, Any]]], *, expected: str, extra_env: dict[str, str] | None = None) -> None:
        self.docs = docs; self.expected = expected; self.extra_env = extra_env or {}
        self.requests: list[tuple[str, str]] = []
        self.submissions: list[Any] = []
        self.code: int = -1
        self.outputs: dict[str, str] = {}
        self.summary: str = ""
        self.document: dict[str, Any] = {}
        self.log: str = ""

    def _fake_gh_request(self, method: str, path: str, *, token: str, body: Any = None) -> Any:
        self.requests.append((method, path))
        if method == "GET":
            return [] if ("comments" in path or "reviews" in path or "labels" in path or "issues/events" in path or "timeline" in path) else {}
        if method == "POST" and path.endswith("/comments"):
            return {"id": 42}
        return {}

    def _fake_submit(self, **kw: Any) -> tuple[dict[str, Any], int]:
        self.submissions.append(kw["result"])
        return {"html_url": "https://example.test/review/1"}, 0

    def run(self) -> "_Harness":
        keep = {k: v for k, v in os.environ.items() if k in ("PATH", "HOME")}
        with tempfile.TemporaryDirectory() as td:
            root = Path(td); legs = root / "legs"; legs.mkdir()
            for name, doc in self.docs:
                d = legs / name; d.mkdir(parents=True, exist_ok=True)
                (d / "review-output.json").write_text(json.dumps(doc) if isinstance(doc, dict) else doc)
            env = {**keep, "AIPRR_PROVIDER": "grok", "AIPRR_API_KEY": "xai-k", "AIPRR_GH_TOKEN": "t", "AIPRR_REPO": "o/r", "AIPRR_PR_NUMBER": "7",
                   "AIPRR_HEAD_SHA": HEAD, "AIPRR_BASE_REF": "main", "AIPRR_MODE": "aggregate", "AIPRR_EXPECTED_LEGS": self.expected,
                   "AIPRR_ARTIFACT_DIR": str(legs), "AIPRR_STRICTNESS": "block-on-critical", "AIPRR_VERIFIER": "off",
                   "AIPRR_AUTHOR_ASSOCIATION": "", "AIPRR_LABEL_GATE": "", "AIPRR_ACTION_PATH": str(_ROOT),
                   "GITHUB_OUTPUT": str(root / "out.txt"), "GITHUB_STEP_SUMMARY": str(root / "summary.md"), **self.extra_env}
            cwd = os.getcwd(); os.chdir(root)
            buf = io.StringIO()
            try:
                with mock.patch.dict(os.environ, env, clear=True), \
                     mock.patch.object(reviewer, "gh_request", side_effect=self._fake_gh_request), \
                     mock.patch.object(reviewer, "gh_graphql", return_value={}), \
                     mock.patch.object(reviewer, "gh_get_authenticated_login", return_value="github-actions[bot]"), \
                     mock.patch.object(reviewer, "fetch_pr_context", return_value=_pr_context()), \
                     mock.patch.object(reviewer, "gh_submit_review_with_fallback", side_effect=self._fake_submit), \
                     contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
                    try:
                        self.code = reviewer.main()
                    except SystemExit as e:
                        self.code = int(e.code or 1)
            finally:
                os.chdir(cwd)
            self.log = buf.getvalue()
            for line in (root / "out.txt").read_text().splitlines():
                if "=" in line:
                    k, v = line.split("=", 1); self.outputs[k] = v
            self.summary = (root / "summary.md").read_text() if (root / "summary.md").exists() else ""
            self.document = json.loads((root / ".aiprr" / "review-output.json").read_text())
        return self


class HappyPath(unittest.TestCase):
    def test_two_legs_publish_once_with_the_aggregate_marker(self) -> None:
        shared = _finding("a.py", 2, "warning", "Unbounded loop")
        docs = [("ai-diff-reviewer-h-grok", _document(A, [shared, _finding("a.py", 1, "critical", "SQL injection", status="verified")])),
                ("ai-diff-reviewer-h-claude", _document(B, [_finding("a.py", 3, "warning", "Unbounded loop in handler")]))]
        h = _Harness(docs, expected=f"{A},{B}").run()
        self.assertEqual(len(h.submissions), 1, h.log)
        body = h.submissions[0].summary
        self.assertTrue(body.startswith(reviewer.AGGREGATE_MARKER), body[:80])
        self.assertIn("Legs: 2 delivered / 2 expected", body)
        self.assertIn("1 duplicate(s) removed", body)
        self.assertEqual(h.code, 2, "the verified critical blocks under block-on-critical")
        self.assertEqual((h.outputs["legs-expected"], h.outputs["legs-delivered"], h.outputs["duplicates-removed"]), (f"{A},{B}", f"{A},{B}", "1"))
        self.assertEqual(json.loads(h.outputs["agreement-histogram"]), {"1": 1, "2": 1})
        self.assertIn("aggregated review", h.summary); self.assertIn("🚫 failing", h.summary)
        self.assertEqual(h.document["role"], "aggregate")
        self.assertEqual([(l["leg_id"], l["delivered"]) for l in h.document["legs"]], [(A, True), (B, True)])
        self.assertEqual(h.document["duplicates_removed"], 1)
        posts = [(m, p) for m, p in h.requests if m != "GET"]
        self.assertTrue(all("comments" in p or "labels" in p for _, p in posts), posts)


class FailureModes(unittest.TestCase):
    def test_timeout_leg_publishes_with_n_minus_one_and_require_all_legs_blocks(self) -> None:
        docs = [("l1", _document(A, [_finding("a.py", 1, "warning", "Lone warning")])),
                ("l2", _document(B, [_finding("a.py", 2, "info", "Partial info")], status="timeout"))]
        h = _Harness(docs, expected=f"{A},{B},{C}").run()
        self.assertEqual(len(h.submissions), 1)
        body = h.submissions[0].summary
        self.assertIn("1 delivered / 3 expected", body); self.assertIn("partial: " + B, body); self.assertIn("**missing: " + C, body)
        self.assertEqual(h.code, 0, "warnings do not block under block-on-critical; missing legs only note")
        self.assertEqual(h.outputs["legs-delivered"], A)
        strict = _Harness(docs, expected=f"{A},{B},{C}", extra_env={"AIPRR_REQUIRE_ALL_LEGS": "true"}).run()
        self.assertEqual(strict.code, 2)
        self.assertIn("require-all-legs: 2 leg(s) not delivered", strict.submissions[0].summary)

    def test_stale_sha_and_duplicate_leg(self) -> None:
        docs = [("old", _document(A, [_finding("a.py", 1, "info", "Old run")], recorded_at="2026-09-24T00:00:00Z")),
                ("new", _document(A, [_finding("a.py", 1, "warning", "New run")], recorded_at="2026-09-24T01:00:00Z")),
                ("stale", _document(B, [_finding("a.py", 1, "critical", "From another head", status="verified")], head="g" * 40))]
        h = _Harness(docs, expected=f"{A},{B}").run()
        self.assertEqual(h.code, 0)
        body = h.submissions[0].summary
        self.assertIn("New run", body); self.assertNotIn("Old run", body); self.assertNotIn("another head", body)
        self.assertIn("1 document(s) for another head ignored", h.log)
        self.assertEqual(h.outputs["legs-delivered"], A)

    def test_no_leg_delivered_is_red(self) -> None:
        h = _Harness([], expected=f"{A},{B}").run()
        self.assertEqual(h.code, 2)
        self.assertEqual(len(h.submissions), 1, "one publish per head even when empty — the check line says why")
        self.assertIn("no review leg delivered a complete document", h.submissions[0].summary)
        self.assertNotIn("hit the turn cap", h.submissions[0].summary)
        self.assertEqual(h.document["run"]["status"], "incomplete")

    def test_invalid_document_is_reported_not_fatal(self) -> None:
        docs = [("good", _document(A, [_finding("a.py", 1, "warning", "Real")])), ("bad", '{"schema_version": "run-record/3.0"}')]
        h = _Harness(docs, expected=A).run()
        self.assertEqual(h.code, 0)
        self.assertIn("1 invalid", h.log); self.assertIn("Invalid documents:", h.summary)
        self.assertEqual(len(h.submissions), 1)


if __name__ == "__main__":
    unittest.main()
