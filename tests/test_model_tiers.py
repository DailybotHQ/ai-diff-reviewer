#!/usr/bin/env python3
"""Cost controls: `model` tier aliases (`balanced` / `economy` / `deep`)
resolved per runner × backend, the indicative price table, and the
per-provider `agent-max-turns` semantics."""

from __future__ import annotations

import importlib.util
import os
import sys
import unittest
from pathlib import Path
from unittest import mock

_ROOT: Path = Path(__file__).resolve().parent.parent
_SPEC = importlib.util.spec_from_file_location(
    "reviewer", _ROOT / "scripts" / "reviewer.py"
)
assert _SPEC is not None and _SPEC.loader is not None
reviewer = importlib.util.module_from_spec(_SPEC)
sys.modules["reviewer"] = reviewer
_SPEC.loader.exec_module(reviewer)


def _prof(api_base: str, provider: str):  # type: ignore[no-untyped-def]
    return reviewer.resolve_endpoint_profile(api_base, provider)


class ResolveModelTests(unittest.TestCase):
    def test_empty_resolves_to_default_models_for_every_runner(self) -> None:
        for pid, default in reviewer.DEFAULT_MODELS.items():
            with self.subTest(provider=pid):
                self.assertEqual(reviewer.resolve_model(pid, _prof("", pid), ""), default)

    def test_explicit_id_passes_through_untouched(self) -> None:
        self.assertEqual(
            reviewer.resolve_model("anthropic", _prof("", "anthropic"), "claude-opus-4-8"),
            "claude-opus-4-8",
        )
        self.assertEqual(
            reviewer.resolve_model("codex", _prof("https://x.services.ai.azure.com/openai/v1", "codex"), "gpt-5.4-mini-azure"),
            "gpt-5.4-mini-azure",
        )

    def test_tier_table_full_resolution(self) -> None:
        for (pid, kind), row in reviewer.MODEL_TIER_TABLE.items():
            # Moonshot/MiniMax expose BOTH protocol faces on their hosts, so
            # the base depends on the runner's protocol, not just the kind.
            bases = {
                reviewer.ENDPOINT_KIND_ANTHROPIC: "",
                reviewer.ENDPOINT_KIND_OPENAI: "",
                reviewer.ENDPOINT_KIND_XAI: "" if pid == "grok" else ("https://api.x.ai" if pid in ("anthropic", "claude-code") else "https://api.x.ai/v1"),
                reviewer.ENDPOINT_KIND_ZAI: "https://api.z.ai/api/anthropic" if pid in ("anthropic", "claude-code") else "https://api.z.ai/api/v1",
                reviewer.ENDPOINT_KIND_CUSTOM: "",
                reviewer.ENDPOINT_KIND_DEEPSEEK: "https://api.deepseek.com",
                reviewer.ENDPOINT_KIND_QWEN: "https://dashscope.aliyuncs.com/compatible-mode/v1",
                reviewer.ENDPOINT_KIND_GEMINI: "https://generativelanguage.googleapis.com/v1beta/openai",
                reviewer.ENDPOINT_KIND_OPENROUTER: "https://openrouter.ai/api/v1",
                reviewer.ENDPOINT_KIND_BEDROCK: "https://bedrock-runtime.us-east-1.amazonaws.com",
            }
            if kind == reviewer.ENDPOINT_KIND_MOONSHOT:
                base = ("https://api.moonshot.ai/anthropic" if pid in ("anthropic", "claude-code")
                        else "https://api.moonshot.ai/v1")
            elif kind == reviewer.ENDPOINT_KIND_MINIMAX:
                base = ("https://api.minimax.io/anthropic" if pid in ("anthropic", "claude-code")
                        else "https://api.minimax.io/v1")
            else:
                base = bases[kind]
            prof = _prof(base, pid)
            self.assertEqual(prof.kind, kind, (pid, kind, base))
            for tier in reviewer.MODEL_TIERS:
                with self.subTest(provider=pid, kind=kind, tier=tier):
                    self.assertEqual(reviewer.resolve_model(pid, prof, tier), row[tier])

    def test_tier_is_case_insensitive_and_trimmed(self) -> None:
        self.assertEqual(reviewer.resolve_model("anthropic", _prof("", "anthropic"), "  Balanced "), "claude-sonnet-5")
        self.assertEqual(reviewer.resolve_model("grok", _prof("", "grok"), "DEEP"), "grok-4.6")
        # 2026-09-16 benchmark: no cheaper xAI model still reviews, so economy == balanced == grok-4.5.
        self.assertEqual(reviewer.resolve_model("grok", _prof("", "grok"), "balanced"), "grok-4.5")
        self.assertEqual(reviewer.resolve_model("grok", _prof("", "grok"), "economy"), "grok-4.5")
        self.assertEqual(reviewer._XAI_TIERS, {"balanced": "grok-4.5", "economy": "grok-4.5", "deep": "grok-4.6"})

    def test_azure_and_custom_tiers_fail_fast_with_guidance(self) -> None:
        for base, pid in (("https://x.services.ai.azure.com/openai/v1", "openai"), ("https://gateway.example/v1", "codex")):
            with self.subTest(pid=pid), self.assertRaises(ValueError) as ctx:
                reviewer.resolve_model(pid, _prof(base, pid), "balanced")
            self.assertIn("explicit id", str(ctx.exception))

    def test_no_tier_ever_maps_to_claude_code_auto(self) -> None:
        for (pid, _kind), row in reviewer.MODEL_TIER_TABLE.items():
            if pid in ("anthropic", "claude-code", "codex", "openai", "grok"):
                for tier, model in row.items():
                    self.assertNotEqual(model, "auto", (pid, tier))

    def test_every_tier_row_matches_the_backend_family(self) -> None:
        for (pid, kind), row in reviewer.MODEL_TIER_TABLE.items():
            for tier, model in row.items():
                if kind == reviewer.ENDPOINT_KIND_ANTHROPIC:
                    self.assertTrue(model.startswith("claude-"), (pid, tier, model))
                elif kind == reviewer.ENDPOINT_KIND_OPENAI:
                    self.assertTrue(model.startswith("gpt-"), (pid, tier, model))
                elif kind == reviewer.ENDPOINT_KIND_XAI:
                    self.assertTrue(model.startswith("grok-"), (pid, tier, model))
                elif kind == reviewer.ENDPOINT_KIND_ZAI:
                    self.assertTrue(model.startswith("glm-"), (pid, tier, model))
                elif kind == reviewer.ENDPOINT_KIND_DEEPSEEK:
                    self.assertTrue(model.startswith("deepseek-"), (pid, tier, model))
                elif kind == reviewer.ENDPOINT_KIND_MOONSHOT:
                    self.assertTrue(model.startswith("kimi-"), (pid, tier, model))
                elif kind == reviewer.ENDPOINT_KIND_QWEN:
                    self.assertTrue(model.startswith("qwen"), (pid, tier, model))
                elif kind == reviewer.ENDPOINT_KIND_MINIMAX:
                    self.assertTrue(model.startswith("MiniMax-"), (pid, tier, model))
                elif kind == reviewer.ENDPOINT_KIND_GEMINI:
                    self.assertTrue(model.startswith("gemini-"), (pid, tier, model))
                elif kind == reviewer.ENDPOINT_KIND_OPENROUTER:
                    self.assertIn("/", model, (pid, tier, model))  # vendor-prefixed ids

    def test_every_tier_model_has_an_indicative_price_or_is_flat_rate(self) -> None:
        for (_pid, _kind), row in reviewer.MODEL_TIER_TABLE.items():
            for model in row.values():
                if model in ("auto", "composer-2.5"):
                    continue  # Cursor subscription — no per-token list price
                self.assertTrue(
                    any(model.startswith(prefix) for prefix in reviewer.INDICATIVE_PRICES_USD_PER_MTOK),
                    f"{model} missing from INDICATIVE_PRICES_USD_PER_MTOK",
                )

    def test_default_models_have_prices_and_legacy_hint_logs(self) -> None:
        for pid, default in reviewer.DEFAULT_MODELS.items():
            if default == "auto":
                continue
            self.assertTrue(any(default.startswith(p) for p in reviewer.INDICATIVE_PRICES_USD_PER_MTOK), default)
        with mock.patch.object(reviewer, "log") as fake_log:
            reviewer.resolve_model("anthropic", _prof("", "anthropic"), "")
        msgs = " ".join(str(c.args[0]) for c in fake_log.call_args_list)
        self.assertIn("claude-sonnet-5", msgs)
        self.assertIn("balanced", msgs)

    def test_resolution_is_logged_for_tiers_and_explicit(self) -> None:
        with mock.patch.object(reviewer, "log") as fake_log:
            reviewer.resolve_model("openai", _prof("", "openai"), "economy")
            reviewer.resolve_model("openai", _prof("", "openai"), "gpt-5.6-sol")
        msgs = [str(c.args[0]) for c in fake_log.call_args_list]
        self.assertTrue(any("tier=economy" in m for m in msgs), msgs)
        self.assertTrue(any("(explicit)" in m for m in msgs), msgs)

    def test_tier_table_kinds_are_registered_endpoint_kinds(self) -> None:
        for (_pid, kind) in reviewer.MODEL_TIER_TABLE:
            self.assertIn(kind, reviewer.ENDPOINT_KINDS)

    def test_price_table_values_are_positive_pairs(self) -> None:
        for model, (i, o) in reviewer.INDICATIVE_PRICES_USD_PER_MTOK.items():
            self.assertGreater(i, 0, model); self.assertGreater(o, 0, model)
        self.assertRegex(reviewer.MODEL_TIERS_VERIFIED_ON, r"^\d{4}-\d{2}-\d{2}$")


class AgentMaxTurnsTests(unittest.TestCase):
    def test_parse(self) -> None:
        self.assertEqual(reviewer.parse_agent_max_turns(""), 0)
        self.assertEqual(reviewer.parse_agent_max_turns(" 12 "), 12)
        for bad in ("abc", "-3", "1.5"):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                reviewer.parse_agent_max_turns(bad)

    def test_grok_gets_native_flag_and_no_warning(self) -> None:
        with mock.patch.dict(os.environ, {"AIPRR_AGENT_MAX_TURNS": "8"}), mock.patch.object(reviewer, "log") as fake_log:
            prov = reviewer.build_provider("grok", api_key="k", model="")
        self.assertEqual(prov.max_turns, 8)
        argv = prov.build_argv(prompt_path=Path("/tmp/p.md"), instructions="R")
        self.assertEqual(argv[argv.index("--max-turns") + 1], "8")
        msgs = " ".join(str(c.args[0]) for c in fake_log.call_args_list)
        self.assertNotIn("WARNING: agent-max-turns", msgs)

    def test_grok_without_cap_has_no_flag(self) -> None:
        with mock.patch.dict(os.environ, {"AIPRR_AGENT_MAX_TURNS": ""}):
            prov = reviewer.build_provider("grok", api_key="k", model="")
        self.assertNotIn("--max-turns", prov.build_argv(prompt_path=Path("/tmp/p.md"), instructions="R"))

    def test_other_clis_warn_per_provider(self) -> None:
        for pid, expected in (("claude-code", "max-budget-usd"), ("codex", "codex exec"), ("cursor", "cursor-agent")):
            with self.subTest(pid=pid):
                with mock.patch.dict(os.environ, {"AIPRR_AGENT_MAX_TURNS": "5"}), mock.patch.object(reviewer, "log") as fake_log:
                    reviewer.build_provider(pid, api_key="k", model="")
                msgs = " ".join(str(c.args[0]) for c in fake_log.call_args_list)
                self.assertIn("WARNING: agent-max-turns", msgs)
                self.assertIn(expected, msgs)
                self.assertIn("grok", msgs)

    def test_junk_value_raises_from_build_provider(self) -> None:
        with mock.patch.dict(os.environ, {"AIPRR_AGENT_MAX_TURNS": "lots"}):
            with self.assertRaises(ValueError):
                reviewer.build_provider("grok", api_key="k", model="")


if __name__ == "__main__":
    unittest.main()


class ModelRequiredOnCustomBackendTests(unittest.TestCase):
    """P-01: an empty `model` on a non-default api-base must fail fast for
    every runner that routes api-base (cursor and grok ignore it); default
    profiles keep resolving to the built-in default."""

    def test_empty_model_on_custom_base_raises_with_hint(self) -> None:
        cases = [("claude-code", "https://api.z.ai/api/anthropic", "glm"), ("codex", "https://r.services.ai.azure.com/openai/v1", "deployment"),
                 ("anthropic", "https://api.x.ai", "Grok"), ("openai", "https://gw.example.com/v1", "gateway")]
        with mock.patch.object(reviewer, "log"):
            for pid, base, hint in cases:
                prof = reviewer.resolve_endpoint_profile(base, pid)
                with self.assertRaises(ValueError) as ctx:
                    reviewer.resolve_model(pid, prof, "")
                self.assertIn(hint, str(ctx.exception), pid)

    def test_default_profile_and_runners_without_api_base_lane_unchanged(self) -> None:
        with mock.patch.object(reviewer, "log"):
            for pid in ("anthropic", "openai", "claude-code", "codex", "grok", "cursor"):
                self.assertEqual(reviewer.resolve_model(pid, reviewer.resolve_endpoint_profile("", pid), ""), reviewer.DEFAULT_MODELS[pid])
            # cursor and grok ignore api-base (warned, not routed): a stray value must not break an empty model.
            self.assertEqual(reviewer.resolve_model("cursor", reviewer.resolve_endpoint_profile("https://gw.example.com/v1", "cursor"), ""), "auto")
            self.assertEqual(reviewer.resolve_model("grok", reviewer.resolve_endpoint_profile("https://gw.example.com/v1", "grok"), ""), reviewer.DEFAULT_MODELS["grok"])
            self.assertEqual(reviewer.PROVIDERS_WITHOUT_API_BASE_LANE, ("cursor", "grok"))
            self.assertEqual(reviewer.resolve_model("claude-code", reviewer.resolve_endpoint_profile("https://api.z.ai/api/anthropic", "claude-code"), "balanced"), "glm-5.3")



class BedrockTierTests(unittest.TestCase):
    """The (anthropic, bedrock) cell: tier aliases resolve to Bedrock ids."""

    def _resolve(self, raw: str) -> str:
        prof = reviewer.resolve_endpoint_profile(
            "https://bedrock-runtime.us-east-1.amazonaws.com", "anthropic"
        )
        with mock.patch.object(reviewer, "log"):
            return reviewer.resolve_model("anthropic", prof, raw)

    def test_tier_aliases_resolve_to_bedrock_ids(self) -> None:
        self.assertEqual(self._resolve("balanced"), "anthropic.claude-sonnet-5")
        self.assertEqual(self._resolve("economy"), "anthropic.claude-haiku-4-5")
        self.assertEqual(self._resolve("deep"), "anthropic.claude-opus-5")

    def test_explicit_bedrock_id_passes_through(self) -> None:
        self.assertEqual(
            self._resolve("us.anthropic.claude-sonnet-5"),
            "us.anthropic.claude-sonnet-5",
        )
