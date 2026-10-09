"""Executes the local review's documented Step 1 block (skills/ai-diff-reviewer/SKILL.md).

The skill is prose an agent follows, so the contract worth pinning is the
shell block itself: run it verbatim in throwaway repositories and check that
an explicit `--base <rev>` (REVIEW_BASE) selects exactly the requested range,
that an unresolvable base stops instead of falling back, and that the default
upstream detection still works.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

REPO_ROOT: Path = Path(__file__).resolve().parent.parent
SKILL: Path = REPO_ROOT / "skills" / "ai-diff-reviewer" / "SKILL.md"


def step1_block() -> str:
    """Return the first ```bash block under '## Step 1 — Detect context'."""
    text: str = SKILL.read_text(encoding="utf-8")
    section: str = text.split("## Step 1 — Detect context", 1)[1].split("\n## ", 1)[0]
    match = re.search(r"```bash\n(.*?)```", section, re.S)
    if match is None:
        raise AssertionError("Step 1 has no bash block")
    return match.group(1)


def git(cwd: Path, *args: str) -> str:
    env: dict[str, str] = {
        **os.environ,
        "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.invalid",
        "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.invalid",
    }
    return subprocess.run(
        ["git", *args], cwd=cwd, env=env, check=True, capture_output=True, text=True
    ).stdout.strip()


def commit_file(cwd: Path, name: str) -> str:
    (cwd / name).write_text(name + "\n", encoding="utf-8")
    git(cwd, "add", name)
    git(cwd, "commit", "-q", "-m", f"add {name}")
    return git(cwd, "rev-parse", "HEAD")


@unittest.skipUnless(shutil.which("git") and shutil.which("bash"), "needs git and bash")
class LocalReviewBaseTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        root: Path = Path(self._tmp.name)
        self.origin: Path = root / "origin.git"
        self.work: Path = root / "work"
        git(root, "init", "-q", "--bare", "-b", "main", str(self.origin))
        git(root, "clone", "-q", str(self.origin), str(self.work))
        git(self.work, "checkout", "-q", "-b", "main")
        commit_file(self.work, "base.txt")
        git(self.work, "push", "-q", "-u", "origin", "main")
        git(self.work, "checkout", "-q", "-b", "feature")
        git(self.work, "push", "-q", "-u", "origin", "feature")  # upstream = origin/feature
        self.plan_start: str = commit_file(self.work, "before_plan.txt")
        git(self.work, "push", "-q")
        git(self.work, "tag", "plan-start")
        commit_file(self.work, "plan_task_1.txt")
        commit_file(self.work, "plan_task_2.txt")

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _run(self, review_base: str) -> subprocess.CompletedProcess[str]:
        env: dict[str, str] = {**os.environ, "REVIEW_BASE": review_base}
        return subprocess.run(
            ["bash", "-c", step1_block()], cwd=self.work, env=env,
            capture_output=True, text=True, check=False,
        )

    def test_explicit_sha_reviews_exactly_the_plan_range(self) -> None:
        result = self._run(self.plan_start)
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stderr, "")
        self.assertIn("plan_task_1.txt", result.stdout)
        self.assertIn("plan_task_2.txt", result.stdout)
        self.assertNotIn("before_plan.txt", result.stdout)
        self.assertNotIn("base.txt |", result.stdout)

    def test_explicit_tag_and_relative_revisions_resolve(self) -> None:
        for rev in ("plan-start", "HEAD~2"):
            result = self._run(rev)
            self.assertIn("plan_task_2.txt", result.stdout, rev)
            self.assertNotIn("before_plan.txt", result.stdout, rev)

    def test_explicit_base_overrides_the_upstream(self) -> None:
        # `main` is older than the tracked upstream (origin/feature), so the
        # explicit range must include a commit the upstream already holds.
        result = self._run("main")
        self.assertIn("before_plan.txt", result.stdout)
        self.assertIn("plan_task_2.txt", result.stdout)
        self.assertEqual(result.stderr, "")

    def test_unresolvable_base_stops_without_fallback(self) -> None:
        result = self._run("no-such-rev")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("ERROR: --base 'no-such-rev' does not resolve", result.stderr)
        self.assertNotIn("plan_task_1.txt", result.stdout)

    def test_option_shaped_base_is_not_an_option(self) -> None:
        result = self._run("--output=pwned")
        self.assertIn("does not resolve", result.stderr)
        self.assertFalse((self.work / "pwned").exists())
        self.assertNotIn("plan_task_1.txt", result.stdout)

    def test_default_uses_the_tracked_upstream(self) -> None:
        # Upstream origin/feature already has before_plan.txt, so only the
        # two unpushed plan commits are in range.
        result = self._run("")
        self.assertIn("plan_task_1.txt", result.stdout)
        self.assertNotIn("before_plan.txt", result.stdout)


if __name__ == "__main__":
    unittest.main()
