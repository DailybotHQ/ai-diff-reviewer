"""Structured output document `review-output/3.0` (RFC-05, BC-11):

- schema-valid in every status (completed with findings / refuted / prior
  ledger, skipped, failed) against `tests/eval/schemas/review-output-v3.schema.json`;
- `summary.rendered_markdown` is exactly the posted body;
- truncation order (excerpts → narrative → findings, criticals last) never
  exceeds `MAX_REVIEW_OUTPUT_BYTES` and records every step;
- a registered secret and the configured `api-base` host never reach the file;
- the digest output matches the written file; artifact naming; the outputs
  are defined (empty) by `write_all_outputs`; the real `main()` startup
  failure leaves a document beside the run record.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

_ROOT: Path = Path(__file__).resolve().parent.parent
_SPEC = importlib.util.spec_from_file_location("reviewer", _ROOT / "scripts" / "reviewer.py")
assert _SPEC is not None and _SPEC.loader is not None
reviewer = importlib.util.module_from_spec(_SPEC)
sys.modules["reviewer"] = reviewer
_SPEC.loader.exec_module(reviewer)
reviewer.log = lambda msg: None  # type: ignore[assignment]

_SC = importlib.util.spec_from_file_location("schema_check", _ROOT / "tests" / "eval" / "schema_check.py")
assert _SC is not None and _SC.loader is not None
schema_check = importlib.util.module_from_spec(_SC)
_SC.loader.exec_module(schema_check)
SCHEMA: dict[str, Any] = json.loads((_ROOT / "tests" / "eval" / "schemas" / "review-output-v3.schema.json").read_text())
RUN_SCHEMA: dict[str, Any] = json.loads((_ROOT / "tests" / "eval" / "schemas" / "run-record.schema.json").read_text())


def _valid(doc: dict[str, Any]) -> list[str]:
    return schema_check.validate(SCHEMA, doc, label="review-output")


def _record() -> Any:
    r = reviewer.RunRecord()
    r.provider, r.endpoint_kind, r.model, r.head_sha, r.base_sha = "grok", "xai", "grok-4.5", "h" * 40, "b" * 40
    r.usage = reviewer.UsageTelemetry(input_tokens=100, output_tokens=10, source=reviewer.USAGE_SOURCE_API)
    return r


def _finding(path: str, line: int, sev: str, status: str = "unverified", excerpt: str = "") -> Any:
    f = reviewer.Finding(path=path, line=line, body=f"Issue at {path}", severity=sev, title=f"T {line}")
    f.severity_claimed = sev
    f.verification = reviewer.FindingVerification(status=status, reason="r")
    f.evidence.excerpt = excerpt
    return f


def _ctx(**kw: Any) -> Any:
    result = reviewer.ReviewResult(findings=[_finding("a.py", 1, "critical", "verified"), _finding("b.py", 2, "warning")])
    refuted = _finding("z.py", 9, "critical", "refuted"); refuted.verification.reason = "guard exists"
    result.refuted.append(refuted)
    rec = reviewer.PriorFindingReconciliation()
    pf = lambda fp, path: reviewer.PriorFinding(thread_id="T", comment_id="C", comment_database_id=1, path=path, line=1, severity="warning", fingerprint=fp, body_excerpt="b", is_outdated=False)  # noqa: E731
    rec.resolved = [pf("1" * 16, "r.py")]; rec.retired_reasons["1" * 16] = "verified_fixed (anchor re-read unavailable)"
    rec.still_open = [pf("2" * 16, "o.py"), pf("3" * 16, "g.py")]; rec.regressed = [rec.still_open[1]]; rec.unverified = [rec.still_open[0]]
    result.prior_reconciliation = rec
    inv = reviewer.ChangeInventory(head_sha="h" * 40, base_sha="b" * 40, base_resolved=True,
                                   files=[{"path": "a.py", "previous_path": None, "status": "modified", "additions": 1, "deletions": 0, "binary": False, "mode_change": False, "omitted": False, "patch_chars": 40},
                                          {"path": "x.lock", "status": "weird", "additions": 0, "deletions": 0, "omitted": True}])
    base = dict(result=result, inventory=inv, strictness="block-on-critical", blocked=True, block_reason="found critical",
                narrative="Looks risky at `a.py:1`.", posted_markdown="## Code review — posted body\n", review_url="https://github.com/o/r/pull/1#pullrequestreview-9")
    base.update(kw)
    return reviewer.ReviewOutputContext(**base)


class SchemaValidity(unittest.TestCase):
    def test_completed_document_is_valid_and_carries_everything(self) -> None:
        run_doc = _record().to_dict(status="completed", failure_class=None)
        self.assertEqual(schema_check.validate(RUN_SCHEMA, run_doc, label="run"), [])
        doc = reviewer.build_review_output(run_doc=run_doc, ctx=_ctx())
        self.assertEqual(_valid(doc), [])
        self.assertEqual(doc["role"], "review")
        self.assertEqual(doc["run"]["run_id"], run_doc["run_id"])
        self.assertEqual(doc["document_id"], f"review-{run_doc['run_id']}")
        self.assertEqual([f["risk_class"] for f in doc["change_inventory"]["files"]], ["unknown", "unknown"])
        self.assertEqual(doc["change_inventory"]["files"][1]["status"], "changed", "unknown statuses normalise")
        self.assertEqual((doc["change_inventory"]["omitted"], doc["change_inventory"]["complete"], doc["change_inventory"]["risk_tier"]), (1, False, "unclassified"))
        self.assertEqual(doc["summary"]["counts"], {"critical": 1, "warning": 1, "info": 0})
        self.assertEqual(doc["summary"]["verification_counts"], {"verified": 1, "unverified": 1, "downgraded": 0, "refuted": 1, "skipped": 0})
        self.assertEqual(doc["summary"]["agreement_histogram"], {"1": 2})
        self.assertEqual(doc["summary"]["rendered_markdown"], "## Code review — posted body\n")
        self.assertEqual(doc["refuted"][0]["reason"], "guard exists")
        self.assertEqual(doc["prior_findings"]["retired"], [{"id": "f-" + "1" * 16, "reason": "verified_fixed"}])
        self.assertEqual(doc["prior_findings"]["still_open"], ["f-" + "2" * 16]); self.assertEqual(doc["prior_findings"]["regressed"], ["f-" + "3" * 16])
        self.assertEqual(doc["gate"], {"strictness": "block-on-critical", "passed": False, "reason": "found critical", "min_agreement": 1, "require_all_legs": False})
        self.assertTrue(doc["usage_known"]); self.assertEqual(doc["usage"]["source"], "vendor"); self.assertEqual(doc["cost_usd"], run_doc["cost_usd"], "cost mirrors the record (null when the vendor reported none)")
        self.assertEqual(doc["review_url"], "https://github.com/o/r/pull/1#pullrequestreview-9")

    def test_skipped_and_failed_documents_are_valid(self) -> None:
        for status, failure in (("skipped", None), ("failed", "configuration"), ("timeout", "timeout")):
            run_doc = reviewer.RunRecord().to_dict(status=status, failure_class=failure)
            doc = reviewer.build_review_output(run_doc=run_doc, ctx=reviewer.ReviewOutputContext())
            self.assertEqual(_valid(doc), [], status)
            self.assertEqual((doc["findings"], doc["usage_known"], doc["usage"], doc["cost_usd"], doc["review_url"]), ([], False, None, None, None))
            self.assertEqual(doc["summary"]["agreement_histogram"], {"1": 0})


class Truncation(unittest.TestCase):
    def _doc(self) -> dict[str, Any]:
        result = reviewer.ReviewResult(findings=[
            _finding("i.py", 1, "info", excerpt="x" * 3000), _finding("w.py", 2, "warning", excerpt="y" * 3000),
            _finding("c.py", 3, "critical", "verified", excerpt="z" * 3000),
        ])
        ctx = reviewer.ReviewOutputContext(result=result, narrative="n" * 3000, posted_markdown="body")
        return reviewer.build_review_output(run_doc=reviewer.RunRecord().to_dict(status="completed", failure_class=None), ctx=ctx)

    def _with_cap(self, cap: int) -> tuple[dict[str, Any], int]:
        old = reviewer.MAX_REVIEW_OUTPUT_BYTES
        reviewer.MAX_REVIEW_OUTPUT_BYTES = cap
        try:
            text = reviewer.finalize_review_output(self._doc())
        finally:
            reviewer.MAX_REVIEW_OUTPUT_BYTES = old
        return json.loads(text), len(text.encode("utf-8"))

    def test_untouched_when_it_fits(self) -> None:
        doc, size = self._with_cap(10_000_000)
        self.assertEqual(doc["truncated"], {"any": False, "findings_dropped": 0, "excerpts_trimmed": 0, "narrative_trimmed": False})
        self.assertEqual(_valid(doc), [])

    def test_excerpts_first_then_narrative_then_findings_criticals_last(self) -> None:
        full = json.dumps(self._doc(), indent=1, ensure_ascii=False)
        doc, size = self._with_cap(len(full.encode()) - 5000)          # excerpts alone suffice
        self.assertEqual((doc["truncated"]["excerpts_trimmed"], doc["truncated"]["narrative_trimmed"], doc["truncated"]["findings_dropped"]), (3, False, 0))
        self.assertTrue(all(len(f["evidence"]["excerpt"]) == 200 for f in doc["findings"]))
        doc, size = self._with_cap(len(full.encode()) - 7_000)         # + narrative (excerpts are already ≤ 2 000 chars: 3 × 1 800 + 3 000 saved)
        self.assertTrue(doc["truncated"]["narrative_trimmed"]); self.assertEqual(doc["summary"]["narrative"], "")
        self.assertEqual(doc["truncated"]["findings_dropped"], 0)
        doc, size = self._with_cap(4_500)                               # + drop findings, critical survives longest
        self.assertTrue(doc["truncated"]["any"]); self.assertGreaterEqual(doc["truncated"]["findings_dropped"], 1)
        self.assertLessEqual(size, 4_500)
        remaining = [f["severity"] for f in doc["findings"]]
        self.assertTrue(remaining == [] or remaining[0] == "critical")
        self.assertNotIn("info", remaining)
        self.assertEqual(_valid(doc), [])


class Scrub(unittest.TestCase):
    def test_secret_and_host_never_reach_the_document(self) -> None:
        reviewer.register_secret("sk-live-VERY-SECRET-777")
        f = _finding("a.py", 1, "warning"); f.body = "Leaks sk-live-VERY-SECRET-777 to https://gw.example.internal/v1"
        f.evidence.excerpt = "TOKEN = 'sk-live-VERY-SECRET-777'"
        ctx = reviewer.ReviewOutputContext(result=reviewer.ReviewResult(findings=[f]), narrative="see gw.example.internal", posted_markdown="x", endpoint_host="gw.example.internal")
        doc = reviewer.build_review_output(run_doc=reviewer.RunRecord().to_dict(status="completed", failure_class=None), ctx=ctx)
        text = reviewer.finalize_review_output(doc, hosts=("gw.example.internal",))
        self.assertNotIn("sk-live-VERY-SECRET-777", text)
        self.assertNotIn("gw.example.internal", text)
        self.assertIn("<endpoint>", text); self.assertIn("***", text)
        self.assertEqual(_valid(json.loads(text)), [])


class WriteAndOutputs(unittest.TestCase):
    def test_digest_matches_the_file_and_outputs_are_written(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            out = Path(td) / "outputs.txt"
            with mock.patch.dict(os.environ, {"GITHUB_OUTPUT": str(out)}):
                cwd = os.getcwd(); os.chdir(td)
                try:
                    record = _record()
                    reviewer.write_review_output_for_run(record, _ctx(), status="completed", failure_class=None)
                finally:
                    os.chdir(cwd)
            written = Path(td) / ".aiprr" / "review-output.json"
            self.assertTrue(written.is_file())
            digest = hashlib.sha256(written.read_bytes()).hexdigest()
            lines = out.read_text().splitlines()
            self.assertIn(f"structured-output-sha256={digest}", lines)
            self.assertIn(f"structured-output-path={written.resolve()}", lines)
            self.assertIn("structured-output-artifact=ai-diff-reviewer-hhhhhhhhhhhh-grok-xai-grok-4-5", lines)
            self.assertEqual(_valid(json.loads(written.read_text())), [])

    def test_write_all_outputs_defines_the_structured_outputs_empty(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            out = Path(td) / "outputs.txt"
            with mock.patch.dict(os.environ, {"GITHUB_OUTPUT": str(out)}):
                reviewer.write_all_outputs(skipped=True)
            lines = out.read_text().splitlines()
            for name in ("structured-output-path", "structured-output-sha256", "structured-output-artifact"):
                self.assertIn(f"{name}=", lines)

    def test_artifact_name_is_slugged_and_bounded(self) -> None:
        r = reviewer.RunRecord(); r.provider, r.endpoint_kind, r.model, r.head_sha = "claude-code", "zai", "GLM 5.3/flash", ""
        name = reviewer.review_output_artifact_name(r)
        self.assertEqual(name, "ai-diff-reviewer-nohead-claude-code-zai-glm-5-3-flash")
        self.assertNotIn("/", name)

    def test_real_main_startup_failure_leaves_a_document_beside_the_record(self) -> None:
        keep = {k: v for k, v in os.environ.items() if k in ("PATH", "HOME")}
        with tempfile.TemporaryDirectory() as td, mock.patch.dict(os.environ, {**keep, "AIPRR_PROVIDER": "anthropic", "AIPRR_API_KEY": "", "GITHUB_OUTPUT": str(Path(td) / "o.txt")}, clear=True):
            cwd = os.getcwd(); os.chdir(td)
            try:
                code = reviewer.main()
            except SystemExit as e:  # startup failures may exit
                code = int(e.code or 1)
            finally:
                os.chdir(cwd)
            self.assertNotEqual(code, 0)
            record = json.loads((Path(td) / ".aiprr" / "run-record.json").read_text())
            doc = json.loads((Path(td) / ".aiprr" / "review-output.json").read_text())
        self.assertEqual(doc["run"]["run_id"], record["run_id"])
        self.assertEqual(doc["run"]["status"], "failed")
        self.assertEqual(_valid(doc), [])


if __name__ == "__main__":
    unittest.main()
