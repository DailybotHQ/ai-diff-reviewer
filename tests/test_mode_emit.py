"""Task 21 — `mode: emit` (RFC-04, BC-09): the review runs, the document is
written, and **no** GitHub mutation happens. The guard sits in the transport
(`gh_request` / `gh_graphql`), so every helper is covered at once; the D-19
note is the single exemption and only when `expected-legs` is unset.
"""
from __future__ import annotations

import importlib.util
import json
import os
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


class _Urlopen:
    """Records every HTTP call the transport actually makes."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def __call__(self, request: Any, timeout: float = 0) -> Any:
        self.calls.append((request.get_method(), request.full_url))
        payload = json.dumps({"id": 77, "html_url": "https://example.test/r/77", "data": {"ok": True}}).encode()

        class _Resp:
            def __enter__(self_inner) -> Any:
                return self_inner

            def __exit__(self_inner, *a: Any) -> None:
                return None

            def read(self_inner) -> bytes:
                return payload

        return _Resp()


class PolicyParsing(unittest.TestCase):
    def test_expected_legs_parse(self) -> None:
        self.assertEqual(reviewer.parse_expected_legs("grok|xai|grok-4.5, claude-code|zai|glm\n grok|xai|grok-4.5 ,,"), ("grok|xai|grok-4.5", "claude-code|zai|glm"))
        self.assertEqual(reviewer.parse_expected_legs(""), ())

    def test_modes(self) -> None:
        self.assertEqual(reviewer.VALID_MODES, ("review", "emit", "aggregate"))
        self.assertTrue(reviewer.PublishPolicy().writes_allowed)
        self.assertTrue(reviewer.PublishPolicy(mode="aggregate").writes_allowed)
        self.assertFalse(reviewer.PublishPolicy(mode="emit").writes_allowed)


class TransportGuard(unittest.TestCase):
    def setUp(self) -> None:
        self.urlopen = _Urlopen()
        self.addCleanup(reviewer.set_publish_policy, reviewer.PublishPolicy())

    def test_emit_suppresses_every_rest_mutation_but_lets_reads_through(self) -> None:
        reviewer.set_publish_policy(reviewer.PublishPolicy(mode="emit"))
        with mock.patch.object(reviewer.urllib.request, "urlopen", self.urlopen):
            got = reviewer.gh_request("GET", "/repos/o/r/pulls/1", token="t")
            self.assertEqual(got["id"], 77)
            for method in ("POST", "PATCH", "PUT", "DELETE"):
                self.assertEqual(reviewer.gh_request(method, f"/repos/o/r/x/{method}", token="t", body={"a": 1}), {})
            # the helpers built on the transport degrade gracefully
            self.assertEqual(reviewer.gh_post_issue_comment(token="t", repo="o/r", pr_number=1, body="hi"), 0)
            reviewer.gh_apply_label(token="t", repo="o/r", pr_number=1, label="x")
        self.assertEqual([m for m, _ in self.urlopen.calls], ["GET"])
        kinds = [(s["kind"], s["target"]) for s in reviewer.PUBLISH_POLICY.suppressed]
        self.assertEqual(kinds[:4], [("POST", "/repos/o/r/x/POST"), ("PATCH", "/repos/o/r/x/PATCH"), ("PUT", "/repos/o/r/x/PUT"), ("DELETE", "/repos/o/r/x/DELETE")])
        self.assertGreaterEqual(len(kinds), 6)

    def test_emit_suppresses_graphql_mutations_but_not_queries(self) -> None:
        reviewer.set_publish_policy(reviewer.PublishPolicy(mode="emit"))
        with mock.patch.object(reviewer.urllib.request, "urlopen", self.urlopen):
            self.assertEqual(reviewer.gh_graphql("query($id: ID!) { node(id: $id) { id } }", {"id": "x"}, token="t"), {"ok": True})
            self.assertEqual(reviewer.gh_graphql("  mutation($id: ID!) { minimizeComment(input: {subjectId: $id, classifier: OUTDATED}) { minimizedComment { isMinimized } } }", {"id": "x"}, token="t"), {})
        self.assertEqual(len(self.urlopen.calls), 1)
        self.assertEqual(reviewer.PUBLISH_POLICY.suppressed[0]["kind"], "GRAPHQL")

    def test_review_mode_is_untouched(self) -> None:
        reviewer.set_publish_policy(reviewer.PublishPolicy(mode="review"))
        with mock.patch.object(reviewer.urllib.request, "urlopen", self.urlopen):
            self.assertEqual(reviewer.gh_request("POST", "/repos/o/r/issues/1/comments", token="t", body={"body": "x"})["id"], 77)
        self.assertEqual(self.urlopen.calls, [("POST", f"{reviewer.GITHUB_REST_BASE}/repos/o/r/issues/1/comments")])
        self.assertEqual(reviewer.PUBLISH_POLICY.suppressed, [])

    def test_allow_writes_lifts_the_guard_for_one_block(self) -> None:
        reviewer.set_publish_policy(reviewer.PublishPolicy(mode="emit"))
        with mock.patch.object(reviewer.urllib.request, "urlopen", self.urlopen):
            with reviewer.allow_writes():
                self.assertEqual(reviewer.gh_request("POST", "/repos/o/r/issues/1/comments", token="t", body={"body": "note"})["id"], 77)
            self.assertEqual(reviewer.gh_request("POST", "/repos/o/r/issues/1/comments", token="t", body={"body": "again"}), {})
        self.assertEqual(len(self.urlopen.calls), 1)


class EmitNote(unittest.TestCase):
    def setUp(self) -> None:
        self.addCleanup(reviewer.set_publish_policy, reviewer.PublishPolicy())

    def _record(self) -> Any:
        r = reviewer.RunRecord(); r.provider, r.endpoint_kind, r.model, r.head_sha = "grok", "xai", "grok-4.5", "h" * 40
        return r

    def test_note_names_the_artifact_and_the_fix(self) -> None:
        text = reviewer.render_emit_note(artifact_name="ai-diff-reviewer-hhhhhhhhhhhh-grok-xai-grok-4-5", head_sha="h" * 40)
        self.assertTrue(text.startswith(reviewer.EMIT_NOTE_MARKER))
        self.assertIn("ai-diff-reviewer-hhhhhhhhhhhh-grok-xai-grok-4-5", text); self.assertIn("mode: aggregate", text); self.assertIn("expected-legs", text)

    def test_note_is_posted_once_and_refreshed_after(self) -> None:
        reviewer.set_publish_policy(reviewer.PublishPolicy(mode="emit"))
        calls: list[tuple[str, str]] = []
        existing: list[dict[str, Any]] = []

        def fake(method: str, path: str, *, token: str, body: Any = None) -> Any:
            calls.append((method, path))
            if method == "GET":
                return list(existing)
            if method == "POST":
                existing.append({"id": 5, "body": body["body"]}); return {"id": 5}
            return {}
        with mock.patch.object(reviewer, "gh_request", side_effect=fake):
            self.assertEqual(reviewer.post_emit_note(token="t", repo="o/r", pr_number=1, record=self._record()), 5)
            self.assertEqual(reviewer.post_emit_note(token="t", repo="o/r", pr_number=1, record=self._record()), 5)
        self.assertEqual([m for m, _ in calls], ["GET", "POST", "GET", "PATCH"])
        self.assertFalse(reviewer.PUBLISH_POLICY._exempt, "the exemption is lifted only inside the block")


class MainBranching(unittest.TestCase):
    def test_invalid_mode_fails_fast_before_any_network(self) -> None:
        keep = {k: v for k, v in os.environ.items() if k in ("PATH", "HOME")}
        import tempfile
        with tempfile.TemporaryDirectory() as td, mock.patch.dict(os.environ, {**keep, "AIPRR_PROVIDER": "grok", "AIPRR_API_KEY": "k", "AIPRR_MODE": "publish",
                                                                              "GITHUB_OUTPUT": str(Path(td) / "o.txt"), "GITHUB_REPOSITORY": "o/r", "AIPRR_GITHUB_TOKEN": "t",
                                                                              "GITHUB_EVENT_PATH": str(Path(td) / "e.json")}, clear=True):
            (Path(td) / "e.json").write_text(json.dumps({"pull_request": {"number": 1, "head": {"sha": "h" * 40}, "base": {"ref": "main"}}, "repository": {"full_name": "o/r"}}))
            cwd = os.getcwd(); os.chdir(td)
            try:
                with mock.patch.object(reviewer.urllib.request, "urlopen", side_effect=AssertionError("network must not be touched")):
                    try:
                        code = reviewer.main()
                    except SystemExit as e:
                        code = int(e.code or 1)
            finally:
                os.chdir(cwd)
        self.assertEqual(code, 1)


if __name__ == "__main__":
    unittest.main()
