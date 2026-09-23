"""v3 change inventory (RFC-02): `ChangeInventory`, `build_change_inventory`,
`tool_get_change_inventory`, and the two builders that fill `PRContext.inventory`.

Runs on a real throwaway git repository so renames, binary files, mode changes
and the base-ref resolution are exercised the way the runtime sees them.
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
from unittest import mock

_ROOT: Path = Path(__file__).resolve().parent.parent
_SPEC = importlib.util.spec_from_file_location("reviewer", _ROOT / "scripts" / "reviewer.py")
assert _SPEC is not None and _SPEC.loader is not None
reviewer = importlib.util.module_from_spec(_SPEC)
sys.modules["reviewer"] = reviewer
_SPEC.loader.exec_module(reviewer)
reviewer.log = lambda msg: None  # type: ignore[assignment]


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, check=True).stdout.strip()


def make_repo(root: Path) -> tuple[Path, str, str]:
    """base: app.py, old.py, big.bin, run.sh (644); head: app.py edited, old.py → new.py,
    big.bin changed, run.sh mode 755, gone.txt deleted, package-lock.json added."""
    repo = root / "repo"; repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "t@example.invalid"); _git(repo, "config", "user.name", "t")
    _git(repo, "config", "commit.gpgsign", "false")
    (repo / "app.py").write_text("def a():\n    return 1\n\n\ndef b():\n    return 2\n")
    (repo / "old.py").write_text("x = 1\n" * 20)
    (repo / "big.bin").write_bytes(bytes(range(256)))
    (repo / "run.sh").write_text("#!/bin/sh\necho hi\n")
    (repo / "gone.txt").write_text("bye\n")
    _git(repo, "add", "-A"); _git(repo, "commit", "-q", "-m", "base")
    base = _git(repo, "rev-parse", "HEAD")
    (repo / "app.py").write_text("def a():\n    return 10\n\n\ndef b():\n    return 2\n")
    (repo / "old.py").rename(repo / "new.py")
    (repo / "big.bin").write_bytes(bytes(range(255, -1, -1)))
    os.chmod(repo / "run.sh", 0o755)
    (repo / "gone.txt").unlink()
    (repo / "package-lock.json").write_text('{"lockfileVersion": 3}\n')
    _git(repo, "add", "-A"); _git(repo, "commit", "-q", "-m", "head")
    head = _git(repo, "rev-parse", "HEAD")
    return repo, base, head


class InventoryOnARealRepo(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.repo, self.base, self.head = make_repo(Path(self._tmp.name))
        self._cwd = os.getcwd(); os.chdir(self.repo)

    def tearDown(self) -> None:
        os.chdir(self._cwd); self._tmp.cleanup()

    def _ctx(self, globs: tuple[str, ...] = reviewer.DEFAULT_IGNORE_PATH_GLOBS) -> Any:
        return reviewer.build_pr_context_from_local(base_sha=self.base, head_sha=self.head, repo_root=str(self.repo), ignore_globs=globs)

    def test_local_builder_fills_a_sha_bound_inventory(self) -> None:
        ctx = self._ctx()
        inv = ctx.inventory
        self.assertIsNotNone(inv)
        self.assertEqual((inv.base_sha, inv.head_sha, inv.base_resolved), (self.base, self.head, True))
        by = {f["path"]: f for f in inv.files}
        self.assertEqual(by["app.py"]["status"], "modified")
        self.assertGreater(by["app.py"]["patch_chars"], 0)
        self.assertIs(by["app.py"]["binary"], False)
        self.assertIs(by["big.bin"]["binary"], True)
        self.assertTrue(by["run.sh"]["mode_change"])
        self.assertFalse(by["app.py"]["mode_change"])
        self.assertTrue(by["package-lock.json"]["omitted"])
        self.assertEqual(inv.omitted_count, 1)
        self.assertFalse(inv.complete, "an omitted file makes the picture incomplete")

    def test_rename_maps_previous_path_from_git(self) -> None:
        inv = reviewer.build_change_inventory(
            base_sha=self.base, head_sha=self.head, base_resolved=True, range_spec=f"{self.base}...{self.head}",
            changed_files=[{"path": "new.py", "status": "renamed", "additions": 0, "deletions": 0}],
            full_diff="", ignore_globs=(), repo_root=str(self.repo),
        )
        self.assertEqual(inv.files[0]["previous_path"], "old.py")
        self.assertIs(inv.files[0]["binary"], False)

    def test_complete_when_nothing_omitted_and_base_resolved(self) -> None:
        ctx = self._ctx(globs=())
        self.assertTrue(ctx.inventory.complete)
        self.assertEqual(ctx.inventory.omitted_count, 0)

    def test_unresolved_base_or_oversized_patch_is_incomplete(self) -> None:
        inv = reviewer.ChangeInventory(head_sha="h", base_sha="", base_resolved=False, files=[])
        self.assertFalse(inv.complete)
        big = reviewer.ChangeInventory(head_sha="h", base_sha="b", base_resolved=True,
                                       files=[{"path": "a", "omitted": False, "binary": False, "patch_chars": reviewer.MAX_PATCH_CHARS + 1}])
        self.assertFalse(big.complete)
        unknown = reviewer.ChangeInventory(head_sha="h", base_sha="b", base_resolved=True,
                                           files=[{"path": "a", "omitted": False, "binary": None, "patch_chars": 1}])
        self.assertFalse(unknown.complete)

    def test_git_failures_degrade_to_unknown_not_exceptions(self) -> None:
        inv = reviewer.build_change_inventory(
            base_sha="0" * 40, head_sha=self.head, base_resolved=False, range_spec="0000000...HEAD",
            changed_files=[{"path": "app.py", "status": "modified", "additions": 1, "deletions": 1}],
            full_diff="", ignore_globs=(), repo_root=str(self.repo),
        )
        self.assertIsNone(inv.files[0]["binary"])
        self.assertFalse(inv.complete)

    def test_tool_answer_is_cached_bounded_json(self) -> None:
        ctx = self._ctx()
        state = reviewer.ReviewState(inventory=ctx.inventory)
        first = reviewer.execute_tool("get_change_inventory", {}, state)
        doc = json.loads(first)
        self.assertEqual(set(doc), {"head_sha", "base_sha", "files", "omitted_count", "complete"})
        self.assertEqual(doc["head_sha"], self.head)
        self.assertIs(reviewer.execute_tool("get_change_inventory", {}, state), state.inventory_json)
        self.assertLessEqual(len(first.encode("utf-8")), reviewer.MAX_TOOL_OUTPUT_BYTES)

    def test_tool_without_inventory_reports_unavailable(self) -> None:
        out = reviewer.execute_tool("get_change_inventory", {}, reviewer.ReviewState())
        self.assertIn("unavailable", out)


class FetchPrContextInventory(unittest.TestCase):
    """`fetch_pr_context` builds the inventory from git + the files API (rename via `previous_filename`)."""

    def test_inventory_from_files_api_and_git(self) -> None:
        files = [{"filename": "src/new.py", "status": "renamed", "previous_filename": "src/old.py", "additions": 1, "deletions": 0},
                 {"filename": "package-lock.json", "status": "modified", "additions": 1, "deletions": 1}]
        pr = {"title": "t", "user": {"login": "u"}, "head": {"ref": "h"}, "base": {"ref": "main"}, "state": "open",
              "additions": 2, "deletions": 1, "commits": 1, "body": ""}
        calls = iter([pr, files, []])
        diff = "diff --git a/src/new.py b/src/new.py\n+x\ndiff --git a/package-lock.json b/package-lock.json\n+y\n-z\n"

        def fake_run_cmd(args: list[str], **kw: Any) -> Any:
            out = ""
            if args[:2] == ["git", "rev-parse"]:
                out = "abc1234def5678901234567890abcdef12345678\n"
            elif "--numstat" in args:
                out = "1\t0\tsrc/{old.py => new.py}\n1\t1\tpackage-lock.json\n"
            elif "--name-status" in args:
                out = "R100\tsrc/old.py\tsrc/new.py\nM\tpackage-lock.json\n"
            elif "--summary" in args:
                out = " rename src/{old.py => new.py} (100%)\n"
            elif args[:2] == ["git", "diff"]:
                out = diff
            return type("P", (), {"returncode": 0, "stdout": out, "stderr": ""})()

        with mock.patch.object(reviewer, "gh_request", side_effect=lambda *a, **k: next(calls)), \
             mock.patch.object(reviewer, "run_cmd", side_effect=fake_run_cmd):
            ctx = reviewer.fetch_pr_context(repo="o/r", pr_number=1, base_ref="main", token="t",
                                            ignore_globs=reviewer.DEFAULT_IGNORE_PATH_GLOBS)
        inv = ctx.inventory
        self.assertTrue(inv.base_resolved)
        by = {f["path"]: f for f in inv.files}
        self.assertEqual(by["src/new.py"]["previous_path"], "src/old.py")
        self.assertIs(by["src/new.py"]["binary"], False)
        self.assertTrue(by["package-lock.json"]["omitted"])
        self.assertGreater(by["src/new.py"]["patch_chars"], 0)
        self.assertFalse(inv.complete)


if __name__ == "__main__":
    unittest.main()
