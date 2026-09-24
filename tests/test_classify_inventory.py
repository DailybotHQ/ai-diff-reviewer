"""Task 28 — RFC-06 § Risk classification: every rule and modifier, and the
untrusted-metadata guarantee (a "docs only" title cannot lower the tier)."""
from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path
from typing import Any

_ROOT: Path = Path(__file__).resolve().parent.parent
_SPEC = importlib.util.spec_from_file_location("reviewer", _ROOT / "scripts" / "reviewer.py")
assert _SPEC is not None and _SPEC.loader is not None
reviewer = importlib.util.module_from_spec(_SPEC)
sys.modules["reviewer"] = reviewer
_SPEC.loader.exec_module(reviewer)


def _inv(files: list[tuple[str, int]], *, complete: bool = True, binary: dict[str, Any] | None = None, mode_change: set[str] = frozenset(), omitted: set[str] = frozenset()) -> Any:
    entries = []
    for path, lines in files:
        entries.append({"path": path, "status": "modified", "additions": lines, "deletions": 0, "omitted": path in omitted, "previous_path": None,
                        "binary": (binary or {}).get(path, False), "mode_change": path in mode_change, "patch_chars": 10})
    return reviewer.ChangeInventory(head_sha="h" * 40, base_sha="b" * 40, base_resolved=complete, files=entries)


class ClassifyPath(unittest.TestCase):
    def test_rules_first_match(self) -> None:
        c = lambda p, **kw: reviewer.classify_path(p, binary=kw.get("binary", False), omitted=kw.get("omitted", False))  # noqa: E731
        self.assertEqual(c("AGENTS.md"), "prompts-policy"); self.assertEqual(c(".review/extension.md"), "prompts-policy"); self.assertEqual(c("prompts/default.md"), "prompts-policy")
        self.assertEqual(c("skills/ai-diff-reviewer/SKILL.md"), "prompts-policy"); self.assertEqual(c(".agents/skills/x/y.md"), "prompts-policy"); self.assertEqual(c(".cursorrules"), "prompts-policy")
        self.assertEqual(c(".github/workflows/ci.yml"), "workflows-ci"); self.assertEqual(c("action.yml"), "workflows-ci"); self.assertEqual(c("Dockerfile.dev"), "workflows-ci"); self.assertEqual(c("Makefile"), "workflows-ci")
        self.assertEqual(c("package.json"), "dependencies"); self.assertEqual(c("package-lock.json"), "dependencies"); self.assertEqual(c("go.sum"), "dependencies"); self.assertEqual(c("requirements-dev.txt"), "dependencies")
        self.assertEqual(c("package-lock.json", omitted=True), "dependencies", "an omitted lockfile is still a dependency change")
        self.assertEqual(c("dist/bundle.min.js"), "generated"); self.assertEqual(c("img/logo.png", binary=True), "generated"); self.assertEqual(c("src/big.json", omitted=True), "generated")
        self.assertEqual(c("tests/test_x.py"), "tests"); self.assertEqual(c("src/foo_test.go"), "tests"); self.assertEqual(c("web/app.spec.ts"), "tests"); self.assertEqual(c("src/__tests__/a.js"), "tests")
        self.assertEqual(c("README.md"), "docs"); self.assertEqual(c("docs/guide.rst"), "docs"); self.assertEqual(c("notes.txt"), "docs")
        self.assertEqual(c("src/app.py"), "code"); self.assertEqual(c("scripts/reviewer.py"), "code")
        self.assertEqual(c("src/blob.bin", binary=None), "unknown"); self.assertEqual(c(""), "unknown")

    def test_docs_never_shadow_policy_files(self) -> None:
        self.assertEqual(reviewer.classify_path("docs/AGENTS.md", binary=False, omitted=False), "prompts-policy")


class ClassifyInventory(unittest.TestCase):
    def test_tiers(self) -> None:
        ci = reviewer.classify_inventory
        self.assertEqual(ci(_inv([("README.md", 40), ("tests/test_a.py", 60)]))[1], "low")
        self.assertEqual(ci(_inv([("README.md", 400)]))[1], "standard", "docs beyond 300 lines are not low")
        self.assertEqual(ci(_inv([("src/app.py", 20)]))[1], "standard")
        self.assertEqual(ci(_inv([("package.json", 2)]))[1], "elevated")
        self.assertEqual(ci(_inv([(".github/workflows/ci.yml", 5)]))[1], "elevated")
        self.assertEqual(ci(_inv([("src/app.py", 20)], mode_change={"src/app.py"}))[1], "elevated")
        self.assertEqual(ci(_inv([("src/app.py", 20)], complete=False))[1], "elevated")
        self.assertEqual(ci(_inv([("src/app.py", 1_600)]))[1], "elevated")
        self.assertEqual(ci(_inv([(".github/workflows/ci.yml", 5), ("src/app.py", 20)]))[1], "critical", "policy/CI together with code")
        self.assertEqual(ci(_inv([("AGENTS.md", 5), ("src/app.py", 20)]))[1], "critical")
        self.assertEqual(ci(_inv([("src/x.bin", 1)], binary={"src/x.bin": None}))[1], "critical", "unknown escalates")
        self.assertEqual(ci(_inv([("src/auth/login.py", 3)]), ("auth/**", "**/auth/**"))[1], "critical", "high-risk-paths raise")
        self.assertEqual(ci(_inv([("README.md", 3)]), ("auth/**",))[1], "low", "high-risk-paths never lower")
        self.assertEqual(ci(None)[1], "unclassified")

    def test_classes_are_written_to_the_inventory(self) -> None:
        inv = _inv([("src/app.py", 20), ("README.md", 3)])
        classes, tier = reviewer.classify_inventory(inv)
        self.assertEqual(classes, {"src/app.py": "code", "README.md": "docs"})
        self.assertEqual([f["risk_class"] for f in inv.files], ["code", "docs"])
        self.assertEqual((inv.risk_tier, inv.to_dict()["risk_tier"], tier), ("standard", "standard", "standard"))

    def test_metadata_is_never_an_input(self) -> None:
        # the classifier has no metadata parameter at all; a PR titled "docs only" with a code file is standard
        import inspect
        self.assertEqual(list(inspect.signature(reviewer.classify_inventory).parameters), ["inventory", "high_risk_globs"])
        self.assertEqual(reviewer.classify_inventory(_inv([("src/app.py", 20)]))[1], "standard")


if __name__ == "__main__":
    unittest.main()
