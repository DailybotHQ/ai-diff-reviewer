"""Task 28 — RFC-06 § Budget matrix: rows per tier, `budget-profile: fixed`,
`max-turns` as a ceiling, `deep` fallback, `economy` never for review,
`balanced` as the documented default alias, `complexity-source`."""
from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path

_ROOT: Path = Path(__file__).resolve().parent.parent
_SPEC = importlib.util.spec_from_file_location("reviewer", _ROOT / "scripts" / "reviewer.py")
assert _SPEC is not None and _SPEC.loader is not None
reviewer = importlib.util.module_from_spec(_SPEC)
sys.modules["reviewer"] = reviewer
_SPEC.loader.exec_module(reviewer)


class Matrix(unittest.TestCase):
    def test_rows(self) -> None:
        rows = {t: reviewer.resolve_budget(t, has_deep=True) for t in ("low", "standard", "elevated", "critical")}
        self.assertEqual([(r.turns, r.alias, r.output_tokens, r.verifier_warning_pct, r.patch_bytes) for r in rows.values()],
                         [(8, "balanced", 4096, 0, 60_000), (20, "balanced", 8192, 30, 120_000), (30, "balanced", 8192, 100, 200_000), (40, "deep", 8192, 100, 200_000)])
        self.assertTrue(rows["critical"].verifier_read_base)
        self.assertEqual(reviewer.resolve_budget("unclassified").turns, 30, "unclassified is budgeted as elevated")
        self.assertEqual(reviewer.resolve_budget("critical", has_deep=False).alias, "balanced", "no deep row → balanced")

    def test_fixed_profile_restores_the_constants(self) -> None:
        for t in ("low", "critical"):
            b = reviewer.resolve_budget(t, profile="fixed", has_deep=True)
            self.assertEqual((b.turns, b.alias, b.output_tokens, b.verifier_warning_pct, b.patch_bytes), (reviewer.DEFAULT_MAX_TURNS, "balanced", 8192, 30, 120_000))

    def test_max_turns_is_a_ceiling_never_a_floor(self) -> None:
        self.assertEqual(reviewer.resolve_budget("critical", max_turns_input=25, has_deep=True).turns, 25)
        self.assertTrue(reviewer.resolve_budget("critical", max_turns_input=25, has_deep=True).turns_capped_by_input)
        self.assertEqual(reviewer.resolve_budget("low", max_turns_input=25).turns, 8)

    def test_economy_is_never_a_review_alias(self) -> None:
        for row in reviewer.BUDGET_MATRIX.values():
            self.assertNotEqual(row["alias"], reviewer.MODEL_TIER_ECONOMY)
        self.assertNotEqual(reviewer.FIXED_PROFILE_BUDGET["alias"], reviewer.MODEL_TIER_ECONOMY)

    def test_balanced_resolves_to_todays_defaults(self) -> None:
        # BC-15: the documented default alias resolves to the ids the tier tables carry today
        for provider, kind, expected in (("grok", "xai", "grok-4.5"), ("openai", "openai", "gpt-5.6-luna"), ("anthropic", "anthropic", "claude-sonnet-5")):
            self.assertEqual(reviewer.MODEL_TIER_TABLE[(provider, kind)][reviewer.MODEL_TIER_BALANCED], expected)

    def test_output_token_cap_and_glob_list(self) -> None:
        reviewer.set_output_token_cap(4096); self.assertEqual(reviewer.OUTPUT_TOKEN_CAP, 4096)
        reviewer.set_output_token_cap(0); self.assertEqual(reviewer.OUTPUT_TOKEN_CAP, 0)
        self.assertEqual(reviewer.parse_glob_list("auth/**, **/migrations/**\n auth/**"), ("auth/**", "**/migrations/**"))

    def test_complexity_for_tier(self) -> None:
        self.assertEqual([reviewer.COMPLEXITY_FOR_TIER[t] for t in ("low", "standard", "elevated", "critical")], ["low", "medium", "high", "high"])


if __name__ == "__main__":
    unittest.main()
