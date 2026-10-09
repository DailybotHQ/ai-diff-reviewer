"""Executes auto-release.yml's release-asset step against a throwaway tagged repo.

The public repository standard promises `SHA256SUMS` on every release. The
release itself only runs on a merge to `main`, so this test pins the promise
earlier: it extracts the step's `run:` block verbatim, runs it with the env
the workflow provides, and checks the archives, the checksum manifest and the
step output that the publish step attaches. It also checks that the publish
step attaches all three files and refuses to publish with any missing.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
import textwrap
import unittest
import zipfile
from pathlib import Path

REPO_ROOT: Path = Path(__file__).resolve().parent.parent
WORKFLOW: Path = REPO_ROOT / ".github" / "workflows" / "auto-release.yml"
ASSET_STEP: str = "Build release archives and SHA256SUMS"


def step_block(name_fragment: str) -> str:
    """Return the full YAML text of the step whose name contains the fragment."""
    text: str = WORKFLOW.read_text(encoding="utf-8")
    steps: list[str] = re.split(r"\n(?=      - name: )", text)
    for step in steps:
        if step.startswith("      - name: ") and name_fragment in step.splitlines()[0]:
            return step
    raise AssertionError(f"no step named like {name_fragment!r}")


def run_script(step: str) -> str:
    """Return the dedented `run: |` body of a step."""
    body: str = step.split("        run: |\n", 1)[1]
    return textwrap.dedent(body)


@unittest.skipUnless(
    shutil.which("git") and shutil.which("bash") and shutil.which("sha256sum"),
    "needs git, bash and sha256sum (GNU coreutils, present on ubuntu runners)",
)
class ReleaseAssetStepTest(unittest.TestCase):
    def test_step_builds_verifiable_archives_and_sha256sums(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root: Path = Path(tmp)
            repo: Path = root / "repo"
            repo.mkdir()
            env: dict[str, str] = {
                **os.environ,
                "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.invalid",
                "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.invalid",
            }
            subprocess.run(["git", "init", "-q"], cwd=repo, check=True, env=env)
            (repo / "action.yml").write_text("name: x\n", encoding="utf-8")
            subprocess.run(["git", "add", "-A"], cwd=repo, check=True, env=env)
            subprocess.run(["git", "commit", "-q", "-m", "x"], cwd=repo, check=True, env=env)
            subprocess.run(["git", "tag", "-a", "v9.8.7", "-m", "Release v9.8.7"], cwd=repo, check=True, env=env)
            # A later untagged change must not leak into the archives.
            (repo / "later.txt").write_text("not released\n", encoding="utf-8")

            runner_temp: Path = root / "runner_temp"
            runner_temp.mkdir()
            output: Path = root / "github_output"
            output.write_text("", encoding="utf-8")
            result = subprocess.run(
                ["bash", "-e", "-c", run_script(step_block(ASSET_STEP))],
                cwd=repo,
                env={**env, "NEW_VERSION": "v9.8.7", "RUNNER_TEMP": str(runner_temp), "GITHUB_OUTPUT": str(output)},
                capture_output=True, text=True, check=False,
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

            out_dir: Path = Path(output.read_text(encoding="utf-8").strip().split("dir=", 1)[1])
            names: list[str] = sorted(p.name for p in out_dir.iterdir())
            self.assertEqual(
                names,
                ["SHA256SUMS", "ai-diff-reviewer-v9.8.7.tar.gz", "ai-diff-reviewer-v9.8.7.zip"],
            )
            sums: str = (out_dir / "SHA256SUMS").read_text(encoding="utf-8")
            self.assertEqual(len(sums.splitlines()), 2)
            check = subprocess.run(["sha256sum", "-c", "SHA256SUMS"], cwd=out_dir, capture_output=True, text=True, check=False)
            self.assertEqual(check.returncode, 0, check.stdout + check.stderr)
            listing: str = subprocess.run(
                ["tar", "-tzf", str(out_dir / "ai-diff-reviewer-v9.8.7.tar.gz")],
                capture_output=True, text=True, check=True,
            ).stdout
            self.assertIn("ai-diff-reviewer-v9.8.7/action.yml", listing)
            self.assertNotIn("later.txt", listing)
            with zipfile.ZipFile(out_dir / "ai-diff-reviewer-v9.8.7.zip") as archive:
                members: list[str] = archive.namelist()
            self.assertIn("ai-diff-reviewer-v9.8.7/action.yml", members)
            self.assertFalse(any(m.endswith("later.txt") for m in members))


class ReleasePublishStepTest(unittest.TestCase):
    def test_publish_attaches_all_assets_and_fails_on_missing(self) -> None:
        step: str = step_block("Create GitHub Release")
        for needle in (
            "${{ steps.assets.outputs.dir }}/SHA256SUMS",
            "${{ steps.assets.outputs.dir }}/*.tar.gz",
            "${{ steps.assets.outputs.dir }}/*.zip",
            "fail_on_unmatched_files: true",
            "prerelease: ${{ contains(steps.version.outputs.new_version, '-') }}",
        ):
            self.assertIn(needle, step)
        # The asset step runs whenever a release is cut — same condition as publish.
        self.assertIn("if: steps.version.outputs.skip == 'false'", step_block(ASSET_STEP))


if __name__ == "__main__":
    unittest.main()
