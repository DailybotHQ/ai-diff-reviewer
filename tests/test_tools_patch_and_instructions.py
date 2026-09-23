"""v3 parity tools (RFC-02): `get_patch` (bounded, hunk-sliced), `read_instruction_files`
(dedup by symlink, total-bytes cap, SHA-256, run-record trace) and `read_file(ref=base)`
(D-14: `git show <base_sha>:<safe path>`), plus path safety for every path argument.
"""

from __future__ import annotations

import importlib.util
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


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, check=True).stdout.strip()


class _RepoCase(unittest.TestCase):
    """A repo with a 3-hunk change in `mod.py`, a deleted `dead.py`, AGENTS.md + CLAUDE.md symlink,
    `.review/extension.md`, and a sibling directory outside the workspace."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        self.repo = root / "repo"; self.repo.mkdir()
        (root / "outside.md").write_text("SECRET OUTSIDE\n")
        _git(self.repo, "init", "-q", "-b", "main")
        _git(self.repo, "config", "user.email", "t@example.invalid"); _git(self.repo, "config", "user.name", "t")
        _git(self.repo, "config", "commit.gpgsign", "false")
        body = "".join(f"line {i}\n" for i in range(1, 121))
        (self.repo / "mod.py").write_text(body)
        (self.repo / "dead.py").write_text("def dead():\n    return 'was here'\n")
        (self.repo / "AGENTS.md").write_text("# Agents\nrule one\n")
        os.symlink("AGENTS.md", self.repo / "CLAUDE.md")
        (self.repo / ".review").mkdir(); (self.repo / ".review" / "extension.md").write_text("# Ext\n")
        os.symlink(root / "outside.md", self.repo / "escape.md")
        _git(self.repo, "add", "-A"); _git(self.repo, "commit", "-q", "-m", "base")
        self.base = _git(self.repo, "rev-parse", "HEAD")
        lines = body.splitlines(keepends=True)
        lines[4] = "line 5 CHANGED\n"; lines[59] = "line 60 CHANGED\n"; lines[114] = "line 115 CHANGED\n"
        (self.repo / "mod.py").write_text("".join(lines))
        (self.repo / "dead.py").unlink()
        _git(self.repo, "add", "-A"); _git(self.repo, "commit", "-q", "-m", "head")
        self.head = _git(self.repo, "rev-parse", "HEAD")
        self._cwd = os.getcwd(); os.chdir(self.repo)
        self.state = reviewer.ReviewState(inventory=reviewer.ChangeInventory(
            head_sha=self.head, base_sha=self.base, base_resolved=True, files=[]))

    def tearDown(self) -> None:
        os.chdir(self._cwd); self._tmp.cleanup()


class GetPatch(_RepoCase):
    def test_whole_file_patch_has_three_hunks(self) -> None:
        out = reviewer.execute_tool("get_patch", {"path": "mod.py"}, self.state)
        self.assertEqual(out.count("\n@@"), 3)
        self.assertIn("+line 60 CHANGED", out)
        self.assertNotIn("[patch truncated", out)

    def test_hunk_index_and_line_range_slice(self) -> None:
        one = reviewer.execute_tool("get_patch", {"path": "mod.py", "hunk_index": 1}, self.state)
        self.assertEqual(one.count("\n@@") + one.startswith("@@"), 1)
        self.assertIn("line 60 CHANGED", one)
        self.assertNotIn("line 5 CHANGED", one)
        rng = reviewer.execute_tool("get_patch", {"path": "mod.py", "line_range": "110-120"}, self.state)
        self.assertIn("line 115 CHANGED", rng)
        self.assertNotIn("line 60 CHANGED", rng)
        self.assertIn("out of range", reviewer.execute_tool("get_patch", {"path": "mod.py", "hunk_index": 9}, self.state))
        self.assertIn("no hunks", reviewer.execute_tool("get_patch", {"path": "mod.py", "line_range": "200-300"}, self.state))
        self.assertIn("start-end", reviewer.execute_tool("get_patch", {"path": "mod.py", "line_range": "abc"}, self.state))

    def test_deleted_file_shows_minus_lines(self) -> None:
        out = reviewer.execute_tool("get_patch", {"path": "dead.py"}, self.state)
        self.assertIn("-def dead():", out)

    def test_cap_lists_remaining_hunks(self) -> None:
        old = reviewer.MAX_PATCH_CHARS
        reviewer.MAX_PATCH_CHARS = 200
        try:
            out = reviewer.execute_tool("get_patch", {"path": "mod.py"}, self.state)
        finally:
            reviewer.MAX_PATCH_CHARS = old
        self.assertIn("[patch truncated at 200 characters", out)
        self.assertIn("hunk_index in [", out)
        self.assertLess(len(out), 600)

    def test_path_safety_and_missing_inventory(self) -> None:
        self.assertIn("escapes the workspace", reviewer.execute_tool("get_patch", {"path": "../outside.md"}, self.state))
        self.assertIn("escapes the workspace", reviewer.execute_tool("get_patch", {"path": "escape.md"}, self.state))
        self.assertIn("no changes", reviewer.execute_tool("get_patch", {"path": "AGENTS.md"}, self.state))
        self.assertIn("unavailable", reviewer.execute_tool("get_patch", {"path": "mod.py"}, reviewer.ReviewState()))


class ReadFileAtBase(_RepoCase):
    def test_base_read_shows_the_deleted_file_and_old_lines(self) -> None:
        out = reviewer.execute_tool("read_file", {"path": "dead.py", "ref": "base"}, self.state)
        self.assertIn("return 'was here'", out)
        self.assertIn(f"@ base {self.base[:12]}", out)
        old = reviewer.execute_tool("read_file", {"path": "mod.py", "ref": "base", "offset": 5, "limit": 1}, self.state)
        self.assertIn("line 5\n", old)
        self.assertNotIn("CHANGED", old)
        head = reviewer.execute_tool("read_file", {"path": "mod.py", "offset": 5, "limit": 1}, self.state)
        self.assertIn("line 5 CHANGED", head)

    def test_base_read_refuses_escapes_bad_refs_and_missing_base(self) -> None:
        self.assertIn("escapes the workspace", reviewer.execute_tool("read_file", {"path": "../outside.md", "ref": "base"}, self.state))
        self.assertIn("escapes the workspace", reviewer.execute_tool("read_file", {"path": "escape.md", "ref": "base"}, self.state))
        self.assertIn("ref must be", reviewer.execute_tool("read_file", {"path": "mod.py", "ref": "merge-base"}, self.state))
        self.assertIn("not found at base", reviewer.execute_tool("read_file", {"path": "nope.py", "ref": "base"}, self.state))
        self.assertIn("unavailable", reviewer.execute_tool("read_file", {"path": "mod.py", "ref": "base"}, reviewer.ReviewState()))
        self.assertIn("not found", reviewer.execute_tool("read_file", {"path": "dead.py"}, self.state))  # head: deleted


class ReadInstructionFiles(_RepoCase):
    def test_dedups_symlink_hashes_and_records_the_trace(self) -> None:
        out = reviewer.execute_tool("read_instruction_files", {}, self.state)
        self.assertEqual(out.count("rule one"), 1, "AGENTS.md and its CLAUDE.md symlink are read once")
        self.assertIn("## AGENTS.md", out)
        self.assertNotIn("## CLAUDE.md", out)
        self.assertIn("## .review/extension.md", out)
        self.assertIn("sha256 ", out)
        self.assertEqual(self.state.instruction_files_read, ["AGENTS.md", ".review/extension.md"])
        reviewer.execute_tool("read_instruction_files", {}, self.state)
        self.assertEqual(len(self.state.instruction_files_read), 2, "no duplicates on a second call")

    def test_extra_candidate_and_escape_are_handled(self) -> None:
        (self.repo / "prompt-ext.md").write_text("extra\n")
        self.state.extra_instruction_files = ("prompt-ext.md", "escape.md", "../outside.md")
        out = reviewer.execute_tool("read_instruction_files", {}, self.state)
        self.assertIn("## prompt-ext.md", out)
        self.assertNotIn("SECRET OUTSIDE", out)
        self.assertNotIn("escape.md", self.state.instruction_files_read)

    def test_total_bytes_cap(self) -> None:
        (self.repo / "AGENTS.md").write_text("A" * 5000)
        old = reviewer.MAX_INSTRUCTION_FILE_BYTES
        reviewer.MAX_INSTRUCTION_FILE_BYTES = 1000
        try:
            out = reviewer.execute_tool("read_instruction_files", {}, self.state)
        finally:
            reviewer.MAX_INSTRUCTION_FILE_BYTES = old
        self.assertIn("[truncated: 5000 bytes", out)
        self.assertNotIn("## .review/extension.md", out, "budget exhausted after the first file")
        self.assertEqual(self.state.instruction_files_read, ["AGENTS.md"])

    def test_nothing_found_message(self) -> None:
        with tempfile.TemporaryDirectory() as empty:
            cwd = os.getcwd(); os.chdir(empty)
            try:
                out = reviewer.execute_tool("read_instruction_files", {}, reviewer.ReviewState())
            finally:
                os.chdir(cwd)
        self.assertIn("no instruction files found", out)


class RunRecordTrace(unittest.TestCase):
    def test_instruction_files_read_flow_into_the_run_record(self) -> None:
        state = reviewer.ReviewState()
        state.instruction_files_read = ["AGENTS.md"]
        record = reviewer.RunRecord()
        result = reviewer.ReviewResult(findings=[], summary="ok")
        record.populate_from_run(provider=object(), state=state, result=result, usage=reviewer.UsageTelemetry(), max_turns=3)
        self.assertEqual(record.to_dict(status="completed", failure_class=None)["context"]["instruction_files_read"], ["AGENTS.md"])


if __name__ == "__main__":
    unittest.main()
