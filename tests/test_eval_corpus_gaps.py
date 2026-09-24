"""RFC-01 corpus gaps: minimum coverage per gap class, and every class runs through `run_eval --tree`.

Gap classes are detected from case CONTENT (so pre-existing cases count too)
and cross-checked with the `gap_tags` the v3 batch declares.
"""

from __future__ import annotations

import fnmatch
import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any

_ROOT: Path = Path(__file__).resolve().parent.parent
CASES: Path = _ROOT / "tests" / "eval" / "cases"
_RE = importlib.util.spec_from_file_location("run_eval", _ROOT / "tests" / "eval" / "run_eval.py")
assert _RE is not None and _RE.loader is not None
run_eval = importlib.util.module_from_spec(_RE)
_RE.loader.exec_module(run_eval)
reviewer = run_eval.load_runtime()
_SC = importlib.util.spec_from_file_location("schema_check", _ROOT / "tests" / "eval" / "schema_check.py")
assert _SC is not None and _SC.loader is not None
schema_check = importlib.util.module_from_spec(_SC)
_SC.loader.exec_module(schema_check)
RUN_SCHEMA: dict[str, Any] = json.loads((_ROOT / "tests" / "eval" / "schemas" / "run-record.schema.json").read_text())

INSTRUCTION_FILES: tuple[str, ...] = ("AGENTS.md", "CLAUDE.md", ".review/extension.md", ".github/ai-diff-reviewer/extension.md")
# RFC-01 § Corpus gaps minimums
MINIMUMS: dict[str, int] = {
    "cross_file": 6, "instruction_file": 5, "deceptive_metadata": 6, "omitted_patch": 4, "iar_multi_round": 4,
}
MIN_STACKS: int = 5
MIN_PER_NEW_STACK: int = 4


def load_cases() -> list[dict[str, Any]]:
    return [json.loads(p.read_text(encoding="utf-8")) for p in sorted(CASES.glob("C*.json"))]


def classes_of(case: dict[str, Any]) -> set[str]:
    out: set[str] = set(case.get("gap_tags") or [])
    fx: dict[str, Any] = case.get("fixture") or {}
    labels: list[dict[str, Any]] = case.get("labels") or []
    paths = {l.get("path") for l in labels}
    if len(paths) > 1:
        out.add("cross_file")
    tree_paths = set((fx.get("base") or {}).keys()) | set((fx.get("head") or {}).keys())
    if tree_paths & set(INSTRUCTION_FILES) or case.get("change_class") == "policy_prompt_file":
        out.add("instruction_file")
    if (fx.get("pr_metadata") or {}).get("deceptive") is True:
        out.add("deceptive_metadata")
    missing = set(((fx.get("inventory") or {}).get("missing_from_fixture")) or [])
    for l in labels:
        p = str(l.get("path") or "")
        if p in missing or any(fnmatch.fnmatch(p, g) or fnmatch.fnmatch(Path(p).name, g) for g in reviewer.DEFAULT_IGNORE_PATH_GLOBS):
            out.add("omitted_patch")
    if "iar" in fx:
        out.add("iar_multi_round")
    return out


class _SilentProvider(reviewer.Provider):
    """Submits an empty review immediately (exercises the tree path, not the model)."""

    def __init__(self) -> None:
        self.profile = reviewer.resolve_endpoint_profile("https://api.x.ai/v1", "openai")

    def complete(self, *, system_prompt: str, messages: list[dict[str, Any]], tools: list[dict[str, Any]]) -> dict[str, Any]:
        return {"stop_reason": "tool_use", "content": [{"type": "tool_use", "id": "s", "name": "submit_review", "input": {"summary": "no findings"}}],
                "usage": {"input_tokens": 10, "output_tokens": 2}}


class CorpusGapCoverageTests(unittest.TestCase):
    def setUp(self) -> None:
        self.cases = load_cases()

    def test_minimums_per_gap_class(self) -> None:
        counts: dict[str, int] = {k: 0 for k in MINIMUMS}
        for c in self.cases:
            for k in classes_of(c):
                if k in counts:
                    counts[k] += 1
        for k, minimum in MINIMUMS.items():
            self.assertGreaterEqual(counts[k], minimum, f"{k}: {counts[k]} < {minimum}")

    def test_stacks_and_new_stack_counts(self) -> None:
        stacks: dict[str, int] = {}
        for c in self.cases:
            stacks[c["stack"]] = stacks.get(c["stack"], 0) + 1
        self.assertGreaterEqual(len(stacks), MIN_STACKS, stacks)
        for s in ("shell", "rust"):
            self.assertGreaterEqual(stacks.get(s, 0), MIN_PER_NEW_STACK, stacks)

    def test_declared_tags_agree_with_content(self) -> None:
        for c in self.cases:
            for tag in c.get("gap_tags") or []:
                if tag.startswith("new_stack_"):
                    self.assertEqual(c["stack"], tag[len("new_stack_"):], c["id"])
                else:
                    self.assertIn(tag, classes_of(c), f"{c['id']}: tag {tag} not supported by content")

    def test_iar_fixtures_carry_round_one(self) -> None:
        for c in self.cases:
            iar = (c.get("fixture") or {}).get("iar")
            if iar is None:
                continue
            self.assertIsInstance(iar.get("round1_head"), dict, c["id"])
            self.assertTrue(iar.get("round1_findings"), c["id"])
            for f in iar["round1_findings"]:
                self.assertTrue({"path", "line", "severity", "body"} <= set(f), c["id"])

    def test_omitted_patch_labels_sit_in_ignored_paths(self) -> None:
        for c in self.cases:
            if "omitted_patch" not in (c.get("gap_tags") or []):
                continue
            for l in c["labels"]:
                p = str(l["path"])
                self.assertTrue(
                    any(fnmatch.fnmatch(p, g) or fnmatch.fnmatch(Path(p).name, g) for g in reviewer.DEFAULT_IGNORE_PATH_GLOBS),
                    f"{c['id']}: {p} is not matched by the default ignore globs",
                )


class OneCasePerClassRunsThroughTreeModeTests(unittest.TestCase):
    def test_each_gap_class_materialises_and_records(self) -> None:
        cases = load_cases()
        picked: dict[str, dict[str, Any]] = {}
        for c in cases:
            if (c.get("fixture") or {}).get("kind", "trees") != "trees":
                continue
            for k in classes_of(c):
                picked.setdefault(k, c)
        for k in MINIMUMS:
            self.assertIn(k, picked, k)
        for k, c in picked.items():
            with self.subTest(gap=k, case=c["id"]), tempfile.TemporaryDirectory() as tmp:
                out = Path(tmp) / f"{c['id']}.json"
                payload = run_eval.run_case(case_path=CASES / f"{c['id']}.json", provider=_SilentProvider(), runtime=reviewer,
                                            system_prompt="sys", max_turns=3, out=out, provider_id="openai", model="grok-4.5")
                record = json.loads(Path(str(out) + ".run-record.json").read_text())
                self.assertEqual(schema_check.validate(RUN_SCHEMA, record), [], c["id"])
                self.assertEqual(record["context"]["corpus_case_id"], c["id"])
                self.assertEqual(payload["score"]["must_find_hits"], 0)


if __name__ == "__main__":
    unittest.main()
