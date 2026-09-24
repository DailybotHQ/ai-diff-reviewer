"""`tests/eval/release_gate.py` — missing / stale / blocking / passing, content-hash matching, CLI exit codes."""

from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

_ROOT: Path = Path(__file__).resolve().parent.parent
_RG = importlib.util.spec_from_file_location("release_gate", _ROOT / "tests" / "eval" / "release_gate.py")
assert _RG is not None and _RG.loader is not None
rg = importlib.util.module_from_spec(_RG)
_RG.loader.exec_module(rg)

NOW = datetime(2026, 10, 10, 12, 0, tzinfo=timezone.utc)
CONTENT = "c" * 64
PROMPT = "p" * 64


def _verdict(*, content: str | None = CONTENT, runtime_sha: str = "abc1234", prompt: str = PROMPT, age_days: int = 1, blocking: list[str] | None = None, name: str = "v.json") -> dict[str, Any]:
    v: dict[str, Any] = {
        "schema_version": "verdict/1.0", "candidate_runtime_sha": runtime_sha, "prompt_sha256": prompt,
        "baseline_ref": "b", "computed_at": (NOW - timedelta(days=age_days)).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "paired_cells": 7, "descriptive_only": False, "measurements": {}, "promotable": {}, "blocking": blocking or [], "notes": [], "thresholds": {}, "_file": name,
    }
    if content is not None:
        v["candidate_content_sha256"] = content
    return v


class EvaluateTests(unittest.TestCase):
    def test_missing(self) -> None:
        ok, reason, _ = rg.evaluate([], runtime_sha="x", content_sha256=CONTENT, prompt_sha256=PROMPT, now=NOW)
        self.assertEqual((ok, reason), (False, rg.REASON_MISSING))

    def test_content_hash_match_passes_even_when_git_sha_differs(self) -> None:
        ok, reason, _ = rg.evaluate([_verdict(runtime_sha="old")], runtime_sha="new", content_sha256=CONTENT, prompt_sha256=PROMPT, now=NOW)
        self.assertTrue(ok, reason)

    def test_prompt_change_invalidates(self) -> None:
        ok, reason, _ = rg.evaluate([_verdict()], runtime_sha="abc1234", content_sha256=CONTENT, prompt_sha256="q" * 64, now=NOW)
        self.assertEqual((ok, reason), (False, rg.REASON_MISSING))

    def test_stale(self) -> None:
        ok, reason, _ = rg.evaluate([_verdict(age_days=45)], runtime_sha="abc1234", content_sha256=CONTENT, prompt_sha256=PROMPT, now=NOW)
        self.assertEqual((ok, reason), (False, rg.REASON_STALE))

    def test_gate_that_cannot_run_exits_2_not_missing(self) -> None:
        # PR #61 self-review (glm, warning): a crashed gate must not read as "eval-verdict-missing".
        import io, contextlib
        with tempfile.TemporaryDirectory() as td:
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                rc = rg.main(["--verdicts", td, "--runtime", str(Path(td) / "does-not-exist.py"), "--prompt", str(Path(td) / "nope.md")])
        self.assertEqual(rc, rg.EXIT_GATE_ERROR)
        self.assertIn(rg.REASON_ERROR, buf.getvalue())

    def test_blocking(self) -> None:
        ok, reason, detail = rg.evaluate([_verdict(blocking=["recall regression on grok|…"])], runtime_sha="abc1234", content_sha256=CONTENT, prompt_sha256=PROMPT, now=NOW)
        self.assertEqual((ok, reason), (False, rg.REASON_BLOCKING))
        self.assertIn("recall regression", detail)

    def test_newest_matching_verdict_wins(self) -> None:
        old_blocking = _verdict(age_days=5, blocking=["x"], name="old.json")
        new_ok = _verdict(age_days=1, name="new.json")
        ok, _, detail = rg.evaluate([old_blocking, new_ok], runtime_sha="abc1234", content_sha256=CONTENT, prompt_sha256=PROMPT, now=NOW)
        self.assertTrue(ok); self.assertIn("new.json", detail)

    def test_legacy_verdict_without_content_hash_needs_exact_sha(self) -> None:
        legacy = _verdict(content=None, runtime_sha="abc1234")
        self.assertTrue(rg.evaluate([legacy], runtime_sha="abc1234", content_sha256=CONTENT, prompt_sha256=PROMPT, now=NOW)[0])
        self.assertFalse(rg.evaluate([legacy], runtime_sha="other", content_sha256=CONTENT, prompt_sha256=PROMPT, now=NOW)[0])


class CliTests(unittest.TestCase):
    def test_cli_against_real_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            vd = Path(tmp) / "verdicts"; vd.mkdir()
            runtime = _ROOT / "scripts" / "reviewer.py"; prompt = _ROOT / "prompts" / "default.md"
            v = _verdict(content=rg.sha256_file(runtime), prompt=rg.sha256_file(prompt), age_days=0)
            v["computed_at"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            v.pop("_file")
            (vd / "ok.json").write_text(json.dumps(v))
            self.assertEqual(rg.main(["--verdicts", str(vd), "--runtime", str(runtime), "--prompt", str(prompt)]), 0)
            v["blocking"] = ["determinism widened"]; v["computed_at"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            (vd / "zz-newer.json").write_text(json.dumps(v))
            self.assertEqual(rg.main(["--verdicts", str(vd), "--runtime", str(runtime), "--prompt", str(prompt)]), 1)
            self.assertEqual(rg.main(["--verdicts", str(Path(tmp) / "nope"), "--runtime", str(runtime), "--prompt", str(prompt)]), 1)


if __name__ == "__main__":
    unittest.main()
