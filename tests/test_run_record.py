"""Run record (v3, RFC-01): every exit path leaves a schema-valid record.

Covers the builder defaults, both provider families through
`populate_from_run`, the unknown-usage rule (`usage_known=false` ⇒ null
usage and cost), the sampling report of both in-process providers (incl. a
stripped parameter after the adaptive 400 fallback), the status derivation
for skipped / failed / completed / incomplete runs, the secret scrub, and
the real startup path (`main()` in a temp workspace) writing a record.
"""

from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

_ROOT: Path = Path(__file__).resolve().parent.parent
_SPEC = importlib.util.spec_from_file_location(
    "reviewer", _ROOT / "scripts" / "reviewer.py"
)
assert _SPEC is not None and _SPEC.loader is not None
reviewer = importlib.util.module_from_spec(_SPEC)
sys.modules["reviewer"] = reviewer
_SPEC.loader.exec_module(reviewer)

_SCHEMA_SPEC = importlib.util.spec_from_file_location(
    "schema_check", _ROOT / "tests" / "eval" / "schema_check.py"
)
assert _SCHEMA_SPEC is not None and _SCHEMA_SPEC.loader is not None
schema_check = importlib.util.module_from_spec(_SCHEMA_SPEC)
_SCHEMA_SPEC.loader.exec_module(schema_check)

SCHEMA: dict[str, Any] = json.loads(
    (_ROOT / "tests" / "eval" / "schemas" / "run-record.schema.json").read_text()
)
_SECRET = "sk-test-secret-value-0123456789abcdef"


def _valid(doc: dict[str, Any]) -> list[str]:
    return schema_check.validate(SCHEMA, doc, label="record")


class RunRecordShapeTests(unittest.TestCase):
    def test_default_record_is_schema_valid_for_every_status(self) -> None:
        record = reviewer.RunRecord()
        for status in ("completed", "incomplete", "failed", "timeout", "skipped"):
            doc = record.to_dict(status=status, failure_class=None)
            self.assertEqual(_valid(doc), [], status)
            self.assertEqual(doc["schema_version"], "run-record/3.0")
            self.assertFalse(doc["usage_known"])
            self.assertIsNone(doc["usage"])
            self.assertIsNone(doc["cost_usd"])

    def test_rfc_and_repo_schema_copies_are_byte_identical(self) -> None:
        for name in ("run-record", "finding-v3", "review-output-v3"):
            rfc = (_ROOT / "docs" / "rfc" / "v3" / "schemas" / f"{name}.schema.json").read_bytes()
            repo = (_ROOT / "tests" / "eval" / "schemas" / f"{name}.schema.json").read_bytes()
            self.assertEqual(rfc, repo, name)

    def test_in_process_run_populates_budget_outcome_and_usage(self) -> None:
        prof = reviewer.resolve_endpoint_profile("", "anthropic")
        provider = reviewer.AnthropicProvider(api_key="k", model="claude-sonnet-4-6", profile=prof)
        state = reviewer.ReviewState()
        state.tool_call_count = 7
        result = reviewer.ReviewResult(
            summary="ok",
            findings=[
                reviewer.Finding(path="a.py", line=1, body="x", severity="critical"),
                reviewer.Finding(path="b.py", line=2, body="y", severity="warning"),
                reviewer.Finding(path="c.py", line=3, body="z", severity="info"),
            ],
        )
        usage = reviewer.UsageTelemetry(
            input_tokens=100, output_tokens=10, turns=4, source=reviewer.USAGE_SOURCE_API, cost_usd=0.5
        )
        record = reviewer.RunRecord()
        record.populate_from_run(provider=provider, state=state, result=result, usage=usage, max_turns=30)
        doc = record.to_dict(status="completed", failure_class=None)
        self.assertEqual(_valid(doc), [])
        self.assertEqual(doc["runner"], "in-process")
        self.assertEqual(doc["budget"], {"max_turns": 30, "turns_used": 4, "tool_calls": 7, "risk_tier": "unclassified", "verifier_runs": 0})
        self.assertEqual(doc["outcome"]["findings_by_severity"], {"critical": 1, "warning": 1, "info": 1})
        self.assertTrue(doc["usage_known"])
        self.assertEqual(doc["usage"]["source"], "vendor")
        self.assertEqual(doc["cost_usd"], 0.5)
        self.assertEqual(doc["cost_basis"], "vendor-reported")
        self.assertEqual(doc["sampling"]["requested"], {"temperature": reviewer.REVIEW_TEMPERATURE})

    def test_agent_runner_run_has_cli_runner_and_estimated_cost_basis(self) -> None:
        provider = reviewer.build_provider("grok", api_key="k", model="grok-4.5", api_base="")
        result = reviewer.ReviewResult(summary="", findings=[])
        usage = reviewer.UsageTelemetry(
            input_tokens=5, output_tokens=1, turns=1, source=reviewer.USAGE_SOURCE_ESTIMATED, cost_usd=0.01
        )
        record = reviewer.RunRecord()
        record.populate_from_run(provider=provider, state=None, result=result, usage=usage, max_turns=30)
        doc = record.to_dict(status="completed", failure_class=None)
        self.assertEqual(_valid(doc), [])
        self.assertEqual(doc["runner"], "cli")
        self.assertEqual(doc["budget"]["tool_calls"], 0)
        self.assertFalse(doc["outcome"]["summary_present"])
        self.assertEqual(doc["usage"]["source"], "estimated")
        self.assertEqual(doc["cost_basis"], "indicative-price-table")
        self.assertEqual(doc["sampling"], {"requested": {}, "sent": {}, "stripped": []})

    def test_unknown_usage_is_never_reported_as_zero(self) -> None:
        provider = reviewer.build_provider("cursor", api_key="k", model="auto", api_base="")
        record = reviewer.RunRecord()
        record.populate_from_run(
            provider=provider, state=None, result=reviewer.ReviewResult(),
            usage=reviewer.UsageTelemetry(), max_turns=30,
        )
        doc = record.to_dict(status="completed", failure_class=None)
        self.assertFalse(doc["usage_known"])
        self.assertIsNone(doc["usage"])
        self.assertIsNone(doc["cost_usd"])
        self.assertEqual(doc["cost_basis"], "unknown")

    def test_context_population_and_truncation_flag(self) -> None:
        ctx = reviewer.PRContext(
            title="t", author="a", head_ref="h", base_ref="main", state="open",
            additions=1, deletions=1, commits=1, body="",
            changed_files=[{"path": "a.py"}, {"path": "b.lock"}],
            diff="diff --git a/a.py b/a.py\n" + "[diff truncated at 200000 characters — use the read_file tool]",
            omitted_files=[("b.lock", 12)],
        )
        record = reviewer.RunRecord()
        record.populate_context(ctx, base_sha="abc1234", iar_mode="incremental")
        doc = record.to_dict(status="completed", failure_class=None)
        self.assertEqual(_valid(doc), [])
        c = doc["context"]
        self.assertEqual((c["changed_files"], c["omitted_files"], c["iar_mode"], c["base_sha"]), (2, 1, "incremental", "abc1234"))
        self.assertTrue(c["diff_truncated"])


class SamplingReportTests(unittest.TestCase):
    def test_openai_report_lists_stripped_params_after_400_fallback(self) -> None:
        prof = reviewer.resolve_endpoint_profile("https://api.deepseek.com", "openai")
        provider = reviewer.OpenAIProvider(api_key="k", model="deepseek-chat", profile=prof)
        before = provider.sampling_report()
        self.assertEqual(before["requested"], {"temperature": 0.0, "seed": 42})
        self.assertEqual(before["stripped"], [])
        provider._suppressed_params.add("seed")
        after = provider.sampling_report()
        self.assertEqual(after["sent"], {"temperature": 0.0})
        self.assertEqual(after["stripped"], ["seed"])

    def test_openai_reasoning_kind_reports_effort_not_temperature(self) -> None:
        prof = reviewer.resolve_endpoint_profile("", "openai")
        provider = reviewer.OpenAIProvider(api_key="k", model="gpt-5.6-luna", profile=prof)
        report = provider.sampling_report()
        self.assertIn("reasoning_effort", report["requested"])
        self.assertNotIn("temperature", report["requested"])

    def test_payload_and_report_agree(self) -> None:
        prof = reviewer.resolve_endpoint_profile("https://api.x.ai/v1", "openai")
        provider = reviewer.OpenAIProvider(api_key="k", model="grok-4.5", profile=prof)
        payload = provider.build_request_body(system_prompt="s", messages=[{"role": "user", "content": "u"}], tools=[])
        report = provider.sampling_report()
        for key, value in report["requested"].items():
            self.assertEqual(payload.get(key), value, key)

    def test_anthropic_gateway_kinds_report_no_sampling(self) -> None:
        prof = reviewer.resolve_endpoint_profile("https://api.z.ai/api/anthropic", "anthropic")
        provider = reviewer.AnthropicProvider(api_key="k", model="glm-5.3", profile=prof)
        self.assertEqual(provider.sampling_report()["requested"], {})


class StatusDerivationTests(unittest.TestCase):
    def test_exit_zero_before_any_model_call_is_skipped(self) -> None:
        record = reviewer.RunRecord()
        self.assertEqual(reviewer.resolve_run_status(record, 0, crashed=False), ("skipped", None))

    def test_exit_one_defaults_to_configuration_failure(self) -> None:
        record = reviewer.RunRecord()
        self.assertEqual(reviewer.resolve_run_status(record, 1, crashed=False), ("failed", "configuration"))

    def test_explicit_failure_class_wins(self) -> None:
        record = reviewer.RunRecord(); record.failure_class = reviewer.RUN_FAILURE_PROMPT_FILE
        self.assertEqual(reviewer.resolve_run_status(record, 1, crashed=False), ("failed", "prompt_file"))

    def test_review_path_status_wins_over_exit_code(self) -> None:
        record = reviewer.RunRecord(); record.run_started = True; record.status = reviewer.RUN_STATUS_INCOMPLETE
        self.assertEqual(reviewer.resolve_run_status(record, 2, crashed=False), ("incomplete", None))

    def test_crash_is_failed_provider_error(self) -> None:
        record = reviewer.RunRecord(); record.run_started = True
        self.assertEqual(reviewer.resolve_run_status(record, 1, crashed=True), ("failed", "provider_error"))


class WriteRunRecordTests(unittest.TestCase):
    def test_writes_scrubbed_record_into_workspace(self) -> None:
        reviewer.register_secret(_SECRET)
        record = reviewer.RunRecord()
        record.model = f"model-{_SECRET}"  # a secret that leaked into a field is scrubbed on write
        with tempfile.TemporaryDirectory() as tmp:
            path = reviewer.write_run_record(record, status="completed", failure_class=None, workspace=Path(tmp))
            assert path is not None
            text = path.read_text()
            self.assertNotIn(_SECRET, text)
            self.assertEqual(_valid(json.loads(text)), [])
            self.assertEqual(path, Path(tmp) / ".aiprr" / "run-record.json")

    def test_write_failure_never_raises(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            blocker = Path(tmp) / ".aiprr"
            blocker.write_text("not a directory")
            self.assertIsNone(
                reviewer.write_run_record(reviewer.RunRecord(), status="failed", failure_class=None, workspace=Path(tmp))
            )


class MainWritesRecordTests(unittest.TestCase):
    """The real entry point leaves a record on a configuration failure."""

    def test_missing_env_leaves_failed_configuration_record(self) -> None:
        env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "AIPRR_PROVIDER": "grok"}
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(os.environ, env, clear=True), \
             contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            cwd = os.getcwd()
            os.chdir(tmp)
            try:
                code = reviewer.main()
            finally:
                os.chdir(cwd)
            doc = json.loads((Path(tmp) / ".aiprr" / "run-record.json").read_text())
        self.assertEqual(code, 1)
        self.assertEqual(_valid(doc), [])
        self.assertEqual((doc["status"], doc["failure_class"]), ("failed", "configuration"))
        self.assertEqual(doc["provider"], "grok")
        self.assertEqual(doc["endpoint_kind"], "xai")
        self.assertIsNone(doc["prompt_sha256"])


if __name__ == "__main__":
    unittest.main()
