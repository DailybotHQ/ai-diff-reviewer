"""Finding v3 (RFC-03 § Finding v3 contract):

- `Finding` keeps every existing constructor valid and `to_v3_dict()` is
  valid against `tests/eval/schemas/finding-v3.schema.json`;
- `emit_finding` validates enums / bounds and queues the v3 payload;
  `post_inline_comment` is its alias (title from the body, category `other`);
- `state_to_review_result` lifts the payload; `parse_findings_file` promotes
  the CLI-lifted fields;
- `complete_finding_evidence` fills the runtime-owned fields on a real repo:
  fingerprint / id, anchor hash at head, scrubbed bounded excerpt, trace ids
  and files read, origin, first-seen run; a secret in the anchored code
  never reaches the excerpt.
"""

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

_ROOT: Path = Path(__file__).resolve().parent.parent
_SPEC = importlib.util.spec_from_file_location("reviewer", _ROOT / "scripts" / "reviewer.py")
assert _SPEC is not None and _SPEC.loader is not None
reviewer = importlib.util.module_from_spec(_SPEC)
sys.modules["reviewer"] = reviewer
_SPEC.loader.exec_module(reviewer)
reviewer.log = lambda msg: None  # type: ignore[assignment]

_SC = importlib.util.spec_from_file_location("schema_check", _ROOT / "tests" / "eval" / "schema_check.py")
assert _SC is not None and _SC.loader is not None
schema_check = importlib.util.module_from_spec(_SC)
_SC.loader.exec_module(schema_check)
SCHEMA: dict[str, Any] = json.loads((_ROOT / "tests" / "eval" / "schemas" / "finding-v3.schema.json").read_text())


def _valid(doc: dict[str, Any]) -> list[str]:
    return schema_check.validate(SCHEMA, doc, label="finding")


class FindingShape(unittest.TestCase):
    def test_minimal_constructor_serialises_schema_valid(self) -> None:
        f = reviewer.Finding(path="a.py", line=3, body="**Bug.** Something is off.\nmore")
        doc = f.to_v3_dict()
        self.assertEqual(_valid(doc), [])
        self.assertRegex(doc["id"], r"^f-[0-9a-f]{16}$")
        self.assertEqual(doc["title"], "Bug. Something is off.")
        self.assertEqual((doc["category"], doc["severity"], doc["severity_claimed"]), ("other", "info", "info"))
        self.assertEqual(doc["verification"]["status"], "unverified")
        self.assertIsNone(doc["agreement"])
        self.assertEqual(doc["lifecycle"]["state"], "new")
        self.assertEqual(doc["origin"]["run_id"], reviewer.ORIGIN_UNKNOWN_RUN_ID)
        self.assertRegex(doc["evidence"]["anchor_sha256"], r"^[0-9a-f]{16}$")

    def test_id_is_stable_and_uses_the_fingerprint(self) -> None:
        f = reviewer.Finding(path="a.py", line=3, body="x", fingerprint="abcdef0123456789")
        self.assertEqual(f.to_v3_dict()["id"], "f-abcdef0123456789")
        g = reviewer.Finding(path="a.py", line=3, body="x")
        self.assertEqual(g.to_v3_dict()["id"], g.to_v3_dict()["id"])

    def test_bounds_and_bad_values_are_neutralised(self) -> None:
        f = reviewer.Finding(path="a.py", line=1, body="b", title="T" * 300, category="vibes", severity="critical",
                             lifecycle={"state": "weird", "retired_reason": "nope"})
        f.evidence.files_read = [f"f{i}" for i in range(40)]
        f.evidence.excerpt = "x" * 5000
        doc = f.to_v3_dict()
        self.assertEqual(_valid(doc), [])
        self.assertEqual(len(doc["title"]), 120)
        self.assertEqual(doc["category"], "other")
        self.assertEqual(len(doc["evidence"]["files_read"]), 20)
        self.assertEqual(len(doc["evidence"]["excerpt"]), 2000)
        self.assertEqual((doc["lifecycle"]["state"], doc["lifecycle"]["retired_reason"]), ("new", None))


class EmitFindingTool(unittest.TestCase):
    def test_emit_finding_queues_v3_payload_and_lifts_into_the_result(self) -> None:
        state = reviewer.ReviewState(max_inline_comments=5)
        msg = reviewer.execute_tool("emit_finding", {
            "path": "a.py", "line": 10, "body": "Null deref", "severity": "CRITICAL", "title": "Null deref on empty list",
            "category": "Correctness", "suggestion": "if xs:\n    xs[0]",
            "evidence": {"files_read": ["a.py", "b.py"], "checks": [{"kind": "read_anchor", "result": "supports", "target": "a.py:10"}]},
        }, state)
        self.assertIn("Queued finding #1", msg)
        self.assertIn("category=correctness", msg)
        self.assertEqual(state.severities, ["critical"])
        result = reviewer.state_to_review_result(state, stop_reason=reviewer.LOOP_STOP_SUBMITTED)
        f = result.findings[0]
        self.assertEqual((f.title, f.category, f.severity_claimed, f.suggestion), ("Null deref on empty list", "correctness", "critical", "if xs:\n    xs[0]"))
        self.assertEqual(f.evidence.files_read, ["a.py", "b.py"])
        self.assertEqual(f.evidence.checks[0]["kind"], "read_anchor")
        self.assertEqual(_valid(f.to_v3_dict()), [])

    def test_invalid_enums_come_back_as_errors_not_reinterpretations(self) -> None:
        state = reviewer.ReviewState(max_inline_comments=5)
        self.assertIn("Error:", reviewer.execute_tool("emit_finding", {"path": "a.py", "line": 1, "body": "b", "category": "vibes"}, state))
        self.assertIn("Error:", reviewer.execute_tool("emit_finding", {"path": "a.py", "line": 1, "body": "b", "evidence": {"checks": [{"kind": "guess", "result": "supports"}]}}, state))
        self.assertIn("Error:", reviewer.execute_tool("emit_finding", {"path": "a.py", "line": 1, "body": "b", "suggestion": 5}, state))
        self.assertEqual(state.inline_comments, [])
        self.assertIn("cap reached", reviewer.execute_tool("emit_finding", {"path": "a.py", "line": 1, "body": "b"}, reviewer.ReviewState(max_inline_comments=0)))

    def test_post_inline_comment_is_the_alias(self) -> None:
        state = reviewer.ReviewState(max_inline_comments=5)
        msg = reviewer.execute_tool("post_inline_comment", {"path": "a.py", "line": 2, "body": "## Off by one\nDetails", "severity": "warning", "category": "security"}, state)
        self.assertIn("Queued finding #1", msg)
        f = reviewer.state_to_review_result(state, stop_reason=reviewer.LOOP_STOP_SUBMITTED).findings[0]
        self.assertEqual((f.category, f.title, f.effective_title(), f.severity), ("other", "", "Off by one", "warning"))

    def test_tools_schema_exposes_emit_finding_with_the_v3_input(self) -> None:
        tool = next(t for t in reviewer.tools_schema(4) if t["name"] == "emit_finding")
        props = tool["input_schema"]["properties"]
        self.assertEqual(set(props) >= {"path", "line", "body", "severity", "title", "category", "suggestion", "evidence"}, True)
        self.assertEqual(props["category"]["enum"], list(reviewer.FINDING_CATEGORIES))
        self.assertIn("4", tool["description"])
        alias = next(t for t in reviewer.tools_schema(4) if t["name"] == "post_inline_comment")
        self.assertIn("Alias of `emit_finding`", alias["description"])


class ParserPromotion(unittest.TestCase):
    def test_findings_file_v3_fields_become_typed_fields(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "findings.json"
            p.write_text(json.dumps({"summary": "s", "findings": [{
                "path": "a.py", "line": 1, "body": "b", "severity": "warning", "title": "T", "category": "test-gap",
                "evidence": {"files_read": ["a.py"], "checks": [{"kind": "run_test", "result": "contradicts", "note": "n"}],
                             "documented_rule": {"file": "AGENTS.md", "quote": "q"}},
            }]}))
            f = reviewer.parse_findings_file(p).findings[0]
        self.assertEqual((f.title, f.category, f.severity_claimed), ("T", "test-gap", "warning"))
        self.assertEqual(f.evidence.documented_rule, {"file": "AGENTS.md", "quote": "q"})
        self.assertEqual(f.evidence.checks[0]["result"], "contradicts")
        self.assertEqual(_valid(f.to_v3_dict()), [])


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, check=True).stdout.strip()


class RuntimeCompletion(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self._tmp.name) / "repo"; self.repo.mkdir()
        _git(self.repo, "init", "-q", "-b", "main")
        _git(self.repo, "config", "user.email", "t@example.invalid"); _git(self.repo, "config", "user.name", "t")
        _git(self.repo, "config", "commit.gpgsign", "false")
        body = "".join(f"line {i}\n" for i in range(1, 30)).replace("line 12\n", 'TOKEN = "sk-live-SECRET-VALUE-42"\n')
        (self.repo / "app.py").write_text(body)
        _git(self.repo, "add", "-A"); _git(self.repo, "commit", "-q", "-m", "head")
        self.head = _git(self.repo, "rev-parse", "HEAD")
        self._cwd = os.getcwd(); os.chdir(self.repo)
        reviewer.register_secret("sk-live-SECRET-VALUE-42")

    def tearDown(self) -> None:
        os.chdir(self._cwd); self._tmp.cleanup()

    def test_completion_fills_runtime_fields_and_scrubs_the_excerpt(self) -> None:
        state = reviewer.ReviewState(max_inline_comments=5)
        reviewer.execute_tool("read_file", {"path": "app.py", "offset": 10, "limit": 5}, state)   # t-0000 touches app.py
        reviewer.execute_tool("glob", {"pattern": "*.md"}, state)                                  # t-0001 does not
        reviewer.execute_tool("emit_finding", {"path": "app.py", "line": 12, "body": "Hard-coded credential", "severity": "critical", "category": "security"}, state)
        result = reviewer.state_to_review_result(state, stop_reason=reviewer.LOOP_STOP_SUBMITTED)
        reviewer.complete_finding_evidence(result, state=state, head_sha=self.head, run_id="run-test-1", provider_id="grok", endpoint_kind="xai", model="grok-4.5")
        f = result.findings[0]
        doc = f.to_v3_dict()
        self.assertEqual(_valid(doc), [])
        self.assertRegex(f.fingerprint or "", r"^[0-9a-f]{16}$")
        self.assertEqual(doc["id"], f"f-{f.fingerprint}")
        self.assertRegex(doc["evidence"]["anchor_sha256"], r"^[0-9a-f]{16}$")
        self.assertNotEqual(doc["evidence"]["anchor_sha256"], reviewer.hashlib.sha256(b"no_context").hexdigest()[:16])
        self.assertIn("line 11", doc["evidence"]["excerpt"])
        self.assertIn("***", doc["evidence"]["excerpt"])
        self.assertNotIn("sk-live-SECRET-VALUE-42", json.dumps(doc))
        self.assertEqual(doc["evidence"]["tool_trace_ids"], ["t-0000", "t-0002"])
        self.assertEqual(doc["evidence"]["files_read"], ["app.py"])
        self.assertEqual(doc["origin"], {"run_id": "run-test-1", "provider": "grok", "endpoint_kind": "xai", "model": "grok-4.5"})
        self.assertEqual(doc["lifecycle"]["first_seen_run_id"], "run-test-1")
        self.assertEqual(doc["severity_claimed"], "critical")

    def test_missing_file_at_head_degrades_to_no_context(self) -> None:
        result = reviewer.ReviewResult(findings=[reviewer.Finding(path="gone.py", line=1, body="b")])
        reviewer.complete_finding_evidence(result, state=None, head_sha=self.head, run_id="r", provider_id="claude-code", endpoint_kind="zai", model="glm")
        doc = result.findings[0].to_v3_dict()
        self.assertEqual(_valid(doc), [])
        self.assertEqual(doc["evidence"]["excerpt"], "")
        self.assertEqual(doc["evidence"]["anchor_sha256"], reviewer.hashlib.sha256(b"no_context").hexdigest()[:16])
        self.assertEqual(doc["origin"]["provider"], "claude-code")

    def test_run_record_id_is_stable_across_calls(self) -> None:
        record = reviewer.RunRecord()
        rid = record.ensure_run_id()
        self.assertEqual(rid, record.ensure_run_id())
        self.assertEqual(record.to_dict(status="completed", failure_class=None)["run_id"], rid)
        self.assertRegex(rid, r"^[a-z0-9-]{1,64}$")


if __name__ == "__main__":
    unittest.main()
