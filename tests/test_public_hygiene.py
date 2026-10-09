"""Tests for scripts/check-public-hygiene.sh (public repository standard, S3).

Each case builds a throwaway git repository, plants content, and runs the
script against it. Planted strings are assembled from fragments so this file
never matches the rules it exercises.
"""

from __future__ import annotations

import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

REPO_ROOT: Path = Path(__file__).resolve().parent.parent
SCRIPT: Path = REPO_ROOT / "scripts" / "check-public-hygiene.sh"

PRIVATE_ORG: str = "DailyBot" + "-Inc"
PERSONAL_PATH: str = "/Users/" + "somebody/projects/x"
RUNNER_PATH: str = "/home/" + "runner/work/repo"
PRIVATE_EMAIL: str = "jane" + "@dailybot.com"
ROLE_EMAIL: str = "security" + "@dailybot.com"
REAL_LOOKING_KEY: str = "sk-" + "ant-" + "api03-" + "Q7rX2mVb9LpZk4TnW8yHc3"
FAKE_KEY: str = "sk-" + "ant-" + "api03-" + "fake-key-for-tests-only"
ENV_NAME_VALUE: str = "OPENAI" + "_API_KEY_FOR_EVAL"


@unittest.skipUnless(shutil.which("git") and shutil.which("bash"), "needs git and bash")
class PublicHygieneScriptTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root: Path = Path(self._tmp.name)
        subprocess.run(["git", "init", "-q", str(self.root)], check=True)
        (self.root / "scripts").mkdir()
        shutil.copy2(SCRIPT, self.root / "scripts" / "check-public-hygiene.sh")

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _write(self, rel: str, text: str) -> None:
        path: Path = self.root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")

    def _run(self) -> subprocess.CompletedProcess[str]:
        subprocess.run(["git", "-C", str(self.root), "add", "-A"], check=True)
        return subprocess.run(
            ["bash", str(self.root / "scripts" / "check-public-hygiene.sh"), str(self.root)],
            capture_output=True,
            text=True,
            check=False,
        )

    def test_clean_repository_passes(self) -> None:
        self._write("README.md", f"Report to {ROLE_EMAIL}. CI runs in {RUNNER_PATH}.\n")
        self._write("cfg.py", "api_key_env = " + '"' + ENV_NAME_VALUE + '"\n')
        result = self._run()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("OK: public hygiene", result.stdout)

    def test_name_rules_fail(self) -> None:
        self._write("a.md", f"See {PRIVATE_ORG}/thing and {PERSONAL_PATH}.\n")
        self._write("b.md", f"Mail {PRIVATE_EMAIL}.\n")
        result = self._run()
        self.assertEqual(result.returncode, 1)
        self.assertIn("[private-org] a.md", result.stdout)
        self.assertIn("[personal-path] a.md", result.stdout)
        self.assertIn("[private-email] b.md", result.stdout)

    def test_real_looking_secret_fails_without_printing_value(self) -> None:
        self._write("conf.py", "api_key = " + '"' + REAL_LOOKING_KEY + '"\n')
        self._write(".public-hygiene-allow", "conf.py  allowlisting never admits a real-looking secret\n")
        result = self._run()
        self.assertEqual(result.returncode, 1)
        self.assertIn("(value not printed)", result.stdout)
        self.assertNotIn(REAL_LOOKING_KEY, result.stdout + result.stderr)

    def test_fake_fixture_needs_allowlist(self) -> None:
        self._write("tests/t.py", "api_key = " + '"' + FAKE_KEY + '"\n')
        self.assertEqual(self._run().returncode, 1)
        self._write(".public-hygiene-allow", "tests/t.py  fake key fixture\n")
        result = self._run()
        self.assertEqual(result.returncode, 0, result.stdout)

    def test_vendored_skills_are_excluded(self) -> None:
        self._write(".agents/skills/vendor/SKILL.md", f"{PRIVATE_ORG}\n")
        self.assertEqual(self._run().returncode, 0)

    def test_not_a_git_tree_is_usage_error(self) -> None:
        with tempfile.TemporaryDirectory() as plain:
            result = subprocess.run(
                ["bash", str(SCRIPT), plain], capture_output=True, text=True, check=False
            )
        self.assertEqual(result.returncode, 2)


if __name__ == "__main__":
    unittest.main()
