"""v3 CLI-lane parity (RFC-02 § Parity tool set, CLI column; RFC-05 § Relation
to the agent-runner findings file):

- every CLI prompt carries the change inventory and a required-reading block,
  and `.aiprr/inventory.json` is written to the workspace before invocation;
- the instruction files the prompt carried reach the run record;
- the findings directive documents the optional finding v3 fields, and the
  parser lifts them into `Finding.extra` with strict types / bounded sizes
  while legacy files parse identically.
"""

from __future__ import annotations

import importlib.util
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
reviewer.log = lambda msg: None  # type: ignore[assignment]


def _ctx(with_inventory: bool = True) -> Any:
    inv = reviewer.ChangeInventory(
        head_sha="h" * 40, base_sha="b" * 40, base_resolved=True,
        files=[{"path": "app.py", "previous_path": None, "status": "modified", "additions": 1, "deletions": 0,
                "binary": False, "mode_change": False, "omitted": False, "patch_chars": 60}],
    ) if with_inventory else None
    return reviewer.PRContext(title="T", author="a", head_ref="feat", base_ref="main", state="open", additions=1, deletions=0,
                              commits=1, body="b", changed_files=[{"path": "app.py", "status": "modified", "additions": 1, "deletions": 0}],
                              diff="diff --git a/app.py b/app.py\n--- a/app.py\n+++ b/app.py\n@@ -1 +1 @@\n-x\n+y\n", inventory=inv)


class _Workspace(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.ws = Path(self._tmp.name)
        (self.ws / "AGENTS.md").write_text("# Agents\nrule one\n")
        os.symlink("AGENTS.md", self.ws / "CLAUDE.md")
        (self.ws / ".review").mkdir(); (self.ws / ".review" / "extension.md").write_text("# Ext\n")

    def tearDown(self) -> None:
        self._tmp.cleanup()


class PromptBlocks(_Workspace):
    def test_helper_writes_inventory_and_prepends_required_reading(self) -> None:
        provider = reviewer.AgentRunnerProvider()
        provider.extra_instruction_files = ("prompt-ext.md", "../outside.md")
        (self.ws / "prompt-ext.md").write_text("extra rules\n")
        text = provider._agent_runner_user_prompt(_ctx(), self.ws)
        self.assertIn(reviewer.INVENTORY_HEADING, text)
        self.assertIn(reviewer.REQUIRED_READING_HEADING, text)
        self.assertIn("### AGENTS.md", text)
        self.assertNotIn("### CLAUDE.md", text, "symlink read once")
        self.assertIn("### .review/extension.md", text)
        self.assertIn("### prompt-ext.md", text)
        self.assertIn("data, not a command", text)
        self.assertLess(text.index(reviewer.INVENTORY_HEADING), text.index(reviewer.REQUIRED_READING_HEADING))
        self.assertEqual(provider.last_instruction_files_read, ("AGENTS.md", ".review/extension.md", "prompt-ext.md"))
        inv_path = self.ws / reviewer.INVENTORY_JSON_REL
        self.assertTrue(inv_path.is_file())
        doc = json.loads(inv_path.read_text())
        self.assertEqual((doc["head_sha"], doc["complete"], doc["files"][0]["path"]), ("h" * 40, True, "app.py"))
        self.assertIn(f"`{reviewer.INVENTORY_JSON_REL}`", text)

    def test_no_inventory_means_no_file_and_stale_file_is_removed(self) -> None:
        inv_path = self.ws / reviewer.INVENTORY_JSON_REL
        inv_path.parent.mkdir(parents=True); inv_path.write_text("{stale}")
        provider = reviewer.AgentRunnerProvider()
        text = provider._agent_runner_user_prompt(_ctx(with_inventory=False), self.ws)
        self.assertFalse(inv_path.exists())
        self.assertNotIn(reviewer.INVENTORY_JSON_REL, text)
        self.assertIn(reviewer.REQUIRED_READING_HEADING, text)

    def test_empty_workspace_says_no_instruction_files(self) -> None:
        with tempfile.TemporaryDirectory() as empty:
            text = reviewer.AgentRunnerProvider()._agent_runner_user_prompt(_ctx(), Path(empty))
        self.assertIn("no repository instruction files found", text)

    def test_claude_code_stdin_carries_both_blocks(self) -> None:
        captured: dict[str, Any] = {}

        def fake_invoke(*, argv: list[str], **kwargs: Any) -> Any:
            captured["stdin"] = kwargs.get("stdin_input") or ""
            return reviewer.ReviewResult(summary="ok", findings=[])

        provider = reviewer.build_provider("claude-code", api_key="sk-test-KEY", model="")
        provider.MCP_DEST = self.ws / "mcp.json"  # type: ignore[misc]
        with mock.patch.object(reviewer, "_invoke_cli_agent", side_effect=fake_invoke):
            provider.run_review(pr_context=_ctx(), review_instructions="RUBRIC", workspace=self.ws, output_dir=self.ws)
        self.assertIn(reviewer.INVENTORY_HEADING, captured["stdin"])
        self.assertIn(reviewer.REQUIRED_READING_HEADING, captured["stdin"])
        self.assertTrue((self.ws / reviewer.INVENTORY_JSON_REL).is_file())
        self.assertEqual(provider.last_instruction_files_read, ("AGENTS.md", ".review/extension.md"))

    def test_grok_prompt_file_carries_both_blocks(self) -> None:
        captured: dict[str, Any] = {}

        def fake_invoke(*, argv: list[str], **kwargs: Any) -> Any:
            idx = argv.index("--prompt-file") if "--prompt-file" in argv else -1
            captured["prompt"] = Path(argv[idx + 1]).read_text() if idx >= 0 else "".join(a for a in argv if len(a) > 200)
            return reviewer.ReviewResult(summary="ok", findings=[])

        provider = reviewer.build_provider("grok", api_key="xai-test-KEY", model="")
        with mock.patch.object(reviewer, "_invoke_cli_agent", side_effect=fake_invoke):
            provider.run_review(pr_context=_ctx(), review_instructions="RUBRIC", workspace=self.ws, output_dir=self.ws)
        self.assertIn(reviewer.INVENTORY_HEADING, captured["prompt"])
        self.assertIn(reviewer.REQUIRED_READING_HEADING, captured["prompt"])


class DirectiveAndParser(unittest.TestCase):
    def test_directive_documents_the_optional_v3_fields(self) -> None:
        d = reviewer.write_findings_prompt_directive("RUBRIC", Path("/tmp/f.json"))
        for key in ('"title"', '"category"', '"evidence"', '"files_read"', '"checks"', '"documented_rule"'):
            self.assertIn(key, d)
        self.assertIn("contradicts-documented-rule", d)
        # the JSON example inside the four-backtick fence must stay valid JSON
        example = d.split("````json\n", 1)[1].split("````", 1)[0]
        doc = json.loads(example)
        self.assertEqual(set(doc["findings"][0]) >= {"path", "line", "body", "severity", "title", "category", "evidence"}, True)

    def _parse(self, payload: dict[str, Any]) -> Any:
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "findings.json"; p.write_text(json.dumps(payload))
            return reviewer.parse_findings_file(p)

    def test_legacy_file_parses_identically_with_empty_extra(self) -> None:
        res = self._parse({"summary": "s", "findings": [{"path": "a.py", "line": 1, "body": "b", "severity": "warning", "vendor_key": 1}]})
        f = res.findings[0]
        self.assertEqual((f.path, f.line, f.body, f.severity, f.start_line, f.side, f.extra), ("a.py", 1, "b", "warning", None, "RIGHT", {}))

    def test_v3_fields_are_lifted_bounded_and_normalised(self) -> None:
        res = self._parse({"summary": "s", "findings": [{
            "path": "a.py", "line": 1, "body": "b", "severity": "critical",
            "title": "  " + "T" * 200, "category": " Security ",
            "evidence": {
                "files_read": [f"f{i}.py" for i in range(25)],
                "checks": [{"kind": "READ_ANCHOR", "result": "Supports", "target": "a.py:1", "note": "n" * 400}] * 25,
                "documented_rule": {"file": "AGENTS.md", "quote": "q" * 600},
                "surprise": "ignored",
            },
        }]})
        extra = res.findings[0].extra
        self.assertEqual(len(extra["title"]), 120)
        self.assertEqual(extra["category"], "security")
        ev = extra["evidence"]
        self.assertEqual(len(ev["files_read"]), 20)
        self.assertEqual(len(ev["checks"]), 20)
        self.assertEqual((ev["checks"][0]["kind"], ev["checks"][0]["result"]), ("read_anchor", "supports"))
        self.assertEqual(len(ev["checks"][0]["note"]), 300)
        self.assertEqual(len(ev["documented_rule"]["quote"]), 500)
        self.assertNotIn("surprise", ev)

    def test_invalid_enum_or_type_raises_like_severity(self) -> None:
        base = {"path": "a.py", "line": 1, "body": "b"}
        for bad in (
            {"category": "vibes"},
            {"category": 7},
            {"title": ["x"]},
            {"evidence": "text"},
            {"evidence": {"files_read": "a.py"}},
            {"evidence": {"checks": [{"kind": "guess", "result": "supports"}]}},
            {"evidence": {"checks": [{"kind": "other", "result": "maybe"}]}},
            {"evidence": {"documented_rule": {"file": "AGENTS.md"}}},
        ):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                self._parse({"summary": "s", "findings": [{**base, **bad}]})

    def test_null_optionals_are_ignored(self) -> None:
        res = self._parse({"summary": "s", "findings": [{"path": "a.py", "line": 1, "body": "b", "title": None, "category": None,
                                                          "evidence": {"documented_rule": None, "files_read": None}}]})
        self.assertEqual(res.findings[0].extra, {})


class RunRecordTrace(unittest.TestCase):
    def test_cli_instruction_files_flow_into_the_record_via_the_provider(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            ws = Path(td); (ws / "AGENTS.md").write_text("rules\n")
            provider = reviewer.AgentRunnerProvider()
            provider._agent_runner_user_prompt(_ctx(), ws)
        record = reviewer.RunRecord()
        record.instruction_files_read = list(provider.last_instruction_files_read)
        doc = record.to_dict(status="completed", failure_class=None)
        self.assertEqual(doc["context"]["instruction_files_read"], ["AGENTS.md"])


if __name__ == "__main__":
    unittest.main()
