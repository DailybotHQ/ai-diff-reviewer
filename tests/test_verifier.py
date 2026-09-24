"""Verifier (RFC-03 § Verifier; D-06 / D-07):

- selection: every claimed critical, warnings sampled deterministically by
  fingerprint hash, `info` never, `verifier: off` → skipped;
- `verify_finding`: a scripted provider yields verified / refuted /
  downgraded; `verified` without a supporting `read_anchor` check or with a
  contradiction is demoted to `unverified`; errors and a silent end are
  `unverified` with the reason (fails open);
- model alias resolution per kind with the `balanced` fallback and explicit
  ids passing through; the in-process runner mapping for CLI lanes;
- `run_verifier` stamps alias / kind / time and counts; run-record fields.
"""

from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
import tempfile
import unittest
from unittest import mock
from pathlib import Path
from typing import Any

_ROOT: Path = Path(__file__).resolve().parent.parent
_SPEC = importlib.util.spec_from_file_location("reviewer", _ROOT / "scripts" / "reviewer.py")
assert _SPEC is not None and _SPEC.loader is not None
reviewer = importlib.util.module_from_spec(_SPEC)
sys.modules["reviewer"] = reviewer
_SPEC.loader.exec_module(reviewer)
reviewer.log = lambda msg: None  # type: ignore[assignment]


def _finding(sev: str = "critical", line: int = 12, body: str = "Hard-coded credential", fp: str | None = None) -> Any:
    f = reviewer.Finding(path="app.py", line=line, body=body, severity=sev, fingerprint=fp)
    f.severity_claimed = sev
    return f


class ScriptedVerifier(reviewer.Provider):
    """Turn 1: read the anchor; turn 2: record the scripted verdict (or misbehave)."""

    def __init__(self, verdict: dict[str, Any] | None = None, *, behaviour: str = "verdict") -> None:
        self.turn = 0
        self.verdict = verdict or {}
        self.behaviour = behaviour
        self.seen_system: str = ""

    def complete(self, *, system_prompt: str, messages: list[dict[str, Any]], tools: list[dict[str, Any]]) -> dict[str, Any]:
        self.turn += 1
        self.seen_system = system_prompt
        names = {t["name"] for t in tools}
        assert reviewer.VERIFIER_VERDICT_TOOL in names and "post_inline_comment" not in names and "submit_review" not in names
        if self.behaviour == "raise":
            raise RuntimeError("boom")
        if self.behaviour == "silent":
            return {"stop_reason": "end_turn", "content": [{"type": "text", "text": "hmm"}]}
        if self.turn == 1:
            return {"stop_reason": "tool_use", "content": [{"type": "tool_use", "id": "r1", "name": "read_file", "input": {"path": "app.py", "offset": 10, "limit": 5}}],
                    "usage": {"input_tokens": 100, "output_tokens": 10}}
        if self.behaviour == "loop":
            return {"stop_reason": "tool_use", "content": [{"type": "tool_use", "id": f"g{self.turn}", "name": "grep", "input": {"pattern": "x"}}]}
        return {"stop_reason": "tool_use", "content": [{"type": "tool_use", "id": "v1", "name": reviewer.VERIFIER_VERDICT_TOOL, "input": self.verdict}]}


class _Repo(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self._tmp.name)
        (self.repo / "app.py").write_text("".join(f"line {i}\n" for i in range(1, 30)))
        self._cwd = os.getcwd(); os.chdir(self.repo)

    def tearDown(self) -> None:
        os.chdir(self._cwd); self._tmp.cleanup()


SUPPORTS = [{"kind": "read_anchor", "result": "supports", "target": "app.py:12"}]


class VerifyFinding(_Repo):
    def test_verified_with_supporting_anchor_read(self) -> None:
        v = reviewer.verify_finding(ScriptedVerifier({"status": "verified", "reason": "the literal is there", "checks": SUPPORTS}), _finding(), inventory=None)
        self.assertEqual((v.status, v.reason), ("verified", "the literal is there"))
        self.assertEqual(v.checks[0]["kind"], "read_anchor")

    def test_verified_without_anchor_support_or_with_contradiction_is_unverified(self) -> None:
        v = reviewer.verify_finding(ScriptedVerifier({"status": "verified", "reason": "trust me", "checks": []}), _finding(), inventory=None)
        self.assertEqual(v.status, "unverified")
        self.assertIn("not backed by a supporting read_anchor", v.reason)
        v = reviewer.verify_finding(ScriptedVerifier({"status": "verified", "reason": "r", "checks": SUPPORTS + [{"kind": "grep_callers", "result": "contradicts"}]}), _finding(), inventory=None)
        self.assertEqual(v.status, "unverified")

    def test_refuted_and_downgraded_pass_through_and_bad_status_is_unverified(self) -> None:
        v = reviewer.verify_finding(ScriptedVerifier({"status": "REFUTED", "reason": "guard exists", "checks": [{"kind": "read_anchor", "result": "contradicts"}]}), _finding(), inventory=None)
        self.assertEqual(v.status, "refuted")
        v = reviewer.verify_finding(ScriptedVerifier({"status": "downgraded", "reason": "real but minor", "checks": SUPPORTS}), _finding(), inventory=None)
        self.assertEqual(v.status, "downgraded")
        v = reviewer.verify_finding(ScriptedVerifier({"status": "maybe", "reason": "?", "checks": []}), _finding(), inventory=None)
        self.assertEqual(v.status, "unverified")

    def test_error_silence_budget_and_invalid_checks_fail_open(self) -> None:
        v = reviewer.verify_finding(ScriptedVerifier(behaviour="raise"), _finding(), inventory=None)
        self.assertEqual(v.status, "unverified"); self.assertIn("verifier error: RuntimeError", v.reason)
        v = reviewer.verify_finding(ScriptedVerifier(behaviour="silent"), _finding(), inventory=None)
        self.assertEqual(v.status, "unverified"); self.assertIn("without a verdict", v.reason)
        v = reviewer.verify_finding(ScriptedVerifier(behaviour="loop"), _finding(), inventory=None, max_turns=3)
        self.assertEqual(v.status, "unverified"); self.assertIn("within 3 turns", v.reason)
        v = reviewer.verify_finding(ScriptedVerifier({"status": "verified", "reason": "r", "checks": [{"kind": "guess", "result": "supports"}]}), _finding(), inventory=None)
        self.assertEqual(v.status, "unverified"); self.assertIn("invalid checks", v.reason)

    def test_claim_and_usage_are_passed(self) -> None:
        usage = reviewer.UsageTelemetry()
        prov = ScriptedVerifier({"status": "verified", "reason": "r", "checks": SUPPORTS})
        f = _finding(); f.title = "Secret in source"; f.category = "security"
        f.evidence.documented_rule = {"file": "AGENTS.md", "quote": "never commit secrets"}
        reviewer.verify_finding(prov, f, inventory=None, usage=usage)
        self.assertIn("verification pass", prov.seen_system)
        self.assertEqual(usage.input_tokens, 100)
        claim = reviewer._render_claim(f)
        self.assertIn("Secret in source", claim); self.assertIn("never commit secrets", claim); self.assertIn("`app.py:12`", claim)


class Selection(unittest.TestCase):
    def test_criticals_always_warnings_sampled_info_never(self) -> None:
        policy = reviewer.VerifierPolicy(warning_sample_pct=30)
        crit = [_finding("critical", line=i, fp=f"{i:016x}") for i in range(5)]
        warn = [_finding("warning", line=i, fp=f"{i + 100:016x}") for i in range(200)]
        info = [_finding("info", line=i, fp=f"{i + 900:016x}") for i in range(5)]
        sel = reviewer.select_findings_for_verification(crit + warn + info, policy)
        self.assertTrue(all(c in sel for c in crit))
        self.assertFalse(any(i in sel for i in info))
        sampled = [w for w in warn if w in sel]
        self.assertTrue(30 < len(sampled) < 90, len(sampled))
        self.assertEqual([f.line for f in sampled], [f.line for f in reviewer.select_findings_for_verification(crit + warn + info, policy) if f.severity == "warning"], "deterministic")
        self.assertEqual(len([w for w in warn if w in reviewer.select_findings_for_verification(warn, reviewer.VerifierPolicy(warning_sample_pct=0))]), 0)
        self.assertEqual(len(reviewer.select_findings_for_verification(warn, reviewer.VerifierPolicy(warning_sample_pct=100))), 200)


class AliasResolution(unittest.TestCase):
    def _profile(self, base: str, runner: str) -> Any:
        return reviewer.resolve_endpoint_profile(base, runner)

    def test_economy_per_kind_with_balanced_fallback_and_passthrough(self) -> None:
        self.assertEqual(reviewer.resolve_verifier_model("anthropic", self._profile("", "anthropic"), ""), ("claude-haiku-4-5", "economy"))
        self.assertEqual(reviewer.resolve_verifier_model("openai", self._profile(reviewer.XAI_OPENAI_COMPAT_API_BASE, "openai"), ""), ("grok-4.5", "economy"))
        self.assertEqual(reviewer.resolve_verifier_model("anthropic", self._profile(reviewer.ZAI_ANTHROPIC_COMPAT_API_BASE, "anthropic"), "balanced"), ("glm-5.3", "balanced"))
        self.assertEqual(reviewer.resolve_verifier_model("anthropic", self._profile("", "anthropic"), "claude-opus-5"), ("claude-opus-5", ""))
        self.assertEqual(reviewer.resolve_verifier_model("openai", self._profile("https://example.invalid/v1", "openai"), ""), ("", ""))

    def test_cli_lanes_map_to_in_process_runners_of_the_same_kind(self) -> None:
        prov, model, alias, kind, reason = reviewer.build_verifier_provider(provider_id="grok", api_key="xai-k", api_base="", requested_model="", review_model="grok-4.5")
        self.assertIsInstance(prov, reviewer.OpenAIProvider); self.assertEqual((model, alias, kind, reason), ("grok-4.5", "economy", "xai", ""))
        prov, model, alias, kind, reason = reviewer.build_verifier_provider(provider_id="claude-code", api_key="zai-k", api_base=reviewer.ZAI_ANTHROPIC_COMPAT_API_BASE, requested_model="", review_model="glm-5.3")
        self.assertIsInstance(prov, reviewer.AnthropicProvider); self.assertEqual((model, alias, kind), ("glm-5.3-flash", "economy", "zai"))
        prov, model, alias, kind, reason = reviewer.build_verifier_provider(provider_id="anthropic", api_key="k", api_base="", requested_model="", review_model="claude-sonnet-5")
        self.assertIsInstance(prov, reviewer.AnthropicProvider); self.assertEqual(model, "claude-haiku-4-5")
        prov, model, alias, kind, reason = reviewer.build_verifier_provider(provider_id="cursor", api_key="k", api_base="", requested_model="", review_model="auto")
        self.assertIsNone(prov); self.assertIn("no in-process backend", reason)

    def test_cli_lane_verifier_follows_the_inherited_base_url_hook(self) -> None:
        # PR #61 self-review (glm, warning): a claude-code lane pointed at Z.ai only through
        # ANTHROPIC_BASE_URL used to send the verifier (with the Z.ai key) to the vendor default host.
        with mock.patch.dict(os.environ, {"ANTHROPIC_BASE_URL": reviewer.ZAI_ANTHROPIC_COMPAT_API_BASE}):
            prov, model, alias, kind, reason = reviewer.build_verifier_provider(provider_id="claude-code", api_key="zai-k", api_base="", requested_model="", review_model="glm-5.3")
        self.assertIsInstance(prov, reviewer.AnthropicProvider); self.assertEqual((kind, reason), ("zai", ""))
        with mock.patch.dict(os.environ, {"ANTHROPIC_BASE_URL": "not a url"}):
            prov, _, _, _, reason = reviewer.build_verifier_provider(provider_id="claude-code", api_key="k", api_base="", requested_model="", review_model="m")
        self.assertIsNone(prov); self.assertIn("ANTHROPIC_BASE_URL", reason)
        with mock.patch.dict(os.environ, {"ANTHROPIC_BASE_URL": reviewer.ZAI_ANTHROPIC_COMPAT_API_BASE}):
            # an explicit api-base input wins over the hook: the input names the vendor default host, the env points at Z.ai
            _, _, _, kind, _ = reviewer.build_verifier_provider(provider_id="claude-code", api_key="k", api_base="https://api.anthropic.com", requested_model="", review_model="m")
            self.assertEqual(kind, "anthropic")
        prov, model, alias, kind, reason = reviewer.build_verifier_provider(provider_id="openai", api_key="k", api_base="https://example.invalid/v1", requested_model="", review_model="my-deployment")
        self.assertIsInstance(prov, reviewer.OpenAIProvider); self.assertEqual((model, alias), ("my-deployment", ""))


class RunVerifierAndRecord(_Repo):
    def test_run_verifier_stamps_and_counts(self) -> None:
        result = reviewer.ReviewResult(findings=[_finding("critical", fp="a" * 16), _finding("warning", line=3, fp="b" * 16), _finding("info", line=4)])
        prov = ScriptedVerifier({"status": "verified", "reason": "r", "checks": SUPPORTS})
        report = reviewer.run_verifier(result, policy=reviewer.VerifierPolicy(warning_sample_pct=0), provider=prov, model="m", alias="economy", endpoint_kind="xai", unavailable_reason="", inventory=None)
        self.assertEqual((report.runs, report.verified, report.skipped), (1, 1, 2))
        self.assertEqual(result.findings[0].verification.status, "verified")
        self.assertEqual((result.findings[0].verification.verifier_model_alias, result.findings[0].verification.verifier_endpoint_kind), ("economy", "xai"))
        self.assertIsNotNone(result.findings[0].verification.verified_at)
        self.assertEqual((result.findings[1].verification.status, result.findings[1].verification.reason), ("skipped", "not sampled"))
        self.assertEqual((result.findings[2].verification.status, result.findings[2].verification.reason), ("skipped", "info is never verified"))
        self.assertGreater(report.usage.input_tokens, 0)

    def test_off_and_unavailable_paths(self) -> None:
        result = reviewer.ReviewResult(findings=[_finding("critical")])
        report = reviewer.run_verifier(result, policy=reviewer.VerifierPolicy(enabled=False), provider=None, model="", alias="", endpoint_kind="", unavailable_reason="verifier off", inventory=None)
        self.assertEqual((report.runs, report.skipped, result.findings[0].verification.status, result.findings[0].verification.reason), (0, 1, "skipped", "verifier off"))
        result = reviewer.ReviewResult(findings=[_finding("critical")])
        report = reviewer.run_verifier(result, policy=reviewer.VerifierPolicy(), provider=None, model="", alias="", endpoint_kind="", unavailable_reason="no in-process backend", inventory=None)
        self.assertEqual((report.unverified, result.findings[0].verification.status), (1, "unverified"))
        self.assertIn("no in-process backend", result.findings[0].verification.reason)
        line = reviewer.format_verifier_line(report, enabled=True)
        self.assertIn("unavailable", line)
        self.assertIn("off", reviewer.format_verifier_line(report, enabled=False))

    def test_run_record_carries_verifier_fields(self) -> None:
        record = reviewer.RunRecord()
        record.verifier_runs = 3; record.verifier_seconds = 4.5; record.findings_verified = 2; record.findings_downgraded = 1; record.findings_refuted = 1
        doc = record.to_dict(status="completed", failure_class=None)
        self.assertEqual(doc["budget"]["verifier_runs"], 3)
        self.assertEqual(doc["timings"]["verifier_seconds"], 4.5)
        self.assertEqual((doc["outcome"]["findings_verified"], doc["outcome"]["findings_downgraded"], doc["outcome"]["findings_refuted"]), (2, 1, 1))


if __name__ == "__main__":
    unittest.main()
