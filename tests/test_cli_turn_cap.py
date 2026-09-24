"""Task 29 — a CLI runner that stops at its native turn cap (`grok
--max-turns`, stderr `Error: max turns reached`, exit 1) yields an incomplete
review, never a crashed run; other non-zero exits without a findings file
still raise. `Budget.tier_turns` is the row's turns before any `max-turns`
ceiling — the native cap of a CLI."""
from __future__ import annotations

import importlib.util
import json
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

GROK_STDOUT: str = json.dumps({"usage": {"input_tokens": 1000, "output_tokens": 50, "cache_read_input_tokens": 200}, "num_turns": 12, "total_cost_usd": 0.5})


def _invoke_with(returncode: int, stderr: str) -> Any:
    calls: list[list[str]] = []

    def fake_run(argv: list[str], **kw: Any) -> subprocess.CompletedProcess[str]:
        calls.append(argv)
        return subprocess.CompletedProcess(argv, returncode, stdout=GROK_STDOUT, stderr=stderr)

    orig = reviewer._run_cli_process
    reviewer._run_cli_process = fake_run  # type: ignore[assignment]
    try:
        with tempfile.TemporaryDirectory() as tmp:
            return reviewer._invoke_cli_agent(argv=["grok", "--max-turns", "12"], workspace=Path(tmp), findings_path=Path(tmp) / ".aiprr" / "findings.json",
                                              env={}, cli_name="xAI Grok", usage_parser=reviewer.parse_grok_usage)
    finally:
        reviewer._run_cli_process = orig  # type: ignore[assignment]


class NativeTurnCapExhaustion(unittest.TestCase):
    def test_cap_hit_without_findings_is_an_incomplete_review(self) -> None:
        result = _invoke_with(1, "Error: max turns reached\n")
        self.assertEqual(result.status, reviewer.REVIEW_STATUS_INCOMPLETE); self.assertTrue(result.incomplete)
        self.assertEqual(result.findings, []); self.assertIn("turn cap", result.status_note)
        self.assertIsNotNone(result.usage); self.assertEqual(result.usage.turns, 12)

    def test_other_nonzero_exit_without_findings_still_raises(self) -> None:
        with self.assertRaises(RuntimeError):
            _invoke_with(2, "boom: unrelated vendor error\n")

    def test_marker_is_a_constant(self) -> None:
        self.assertIn("max turns reached", reviewer.CLI_TURN_CAP_STDERR_MARKERS)


class TierTurnsForTheNativeCap(unittest.TestCase):
    def test_tier_turns_ignores_the_max_turns_ceiling(self) -> None:
        b = reviewer.resolve_budget("critical", max_turns_input=12, has_deep=True)
        self.assertEqual((b.turns, b.tier_turns, b.turns_capped_by_input), (12, 40, True))
        self.assertEqual(reviewer.resolve_budget("low").tier_turns, 8)
        self.assertEqual(reviewer.resolve_budget("standard", profile="fixed").tier_turns, reviewer.DEFAULT_MAX_TURNS)


if __name__ == "__main__":
    unittest.main()
