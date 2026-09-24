"""Structured review summary (RFC-03 § Structured summary):

- header counts by published severity, verification counts, the check line;
- the findings table (criticals first, claimed severity shown when it
  differs, verification, agreement);
- the bounded narrative: trimmed at `SUMMARY_NARRATIVE_MAX_CHARS`, and every
  `path:line` it names that is not a table row is footnoted (E-32);
- refuted section and the prior-findings ledger with retirement reasons;
- the gate status block still closes the body in `main`'s order.
"""

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
reviewer.log = lambda msg: None  # type: ignore[assignment]


def _f(path: str, line: int, sev: str, *, claimed: str | None = None, status: str = "unverified", title: str = "", reason: str = "") -> Any:
    f = reviewer.Finding(path=path, line=line, body=f"**{title or 'Something'}**\nDetails", severity=sev, title=title)
    f.severity_claimed = claimed or sev
    f.verification = reviewer.FindingVerification(status=status, reason=reason)
    return f


def _render(result: Any, narrative: str = "Looks fine.", blocked: bool = False) -> str:
    return reviewer.render_review_summary(result, narrative=narrative, blocked=blocked, block_reason="no findings — passes", strictness="block-on-critical")


class Shape(unittest.TestCase):
    def test_header_table_and_check_line(self) -> None:
        result = reviewer.ReviewResult(findings=[
            _f("b.py", 5, "warning", claimed="critical", status="downgraded", title="Claimed too high"),
            _f("a.py", 1, "critical", status="verified", title="Real | pipe"),
            _f("c.py", 9, "info"),
        ])
        result.findings[1].agreement = {"legs_total": 2, "legs_reporting": 2}
        body = _render(result, blocked=True)
        self.assertTrue(body.startswith("## Code review — 3 finding(s): 1 critical · 1 warning · 1 info"))
        self.assertIn("Verification: 1 verified · 1 downgraded · 0 refuted · 1 unverified · 0 skipped", body)
        self.assertIn("Check: 🚫 failing — strictness `block-on-critical`: no findings — passes", body)
        rows = [l for l in body.splitlines() if l.startswith("| ")]
        self.assertEqual(rows[0], "| Severity | Location | Title | Verification | Agreement |")
        # rows[0] is the header (the |---| separator does not start with "| ")
        self.assertIn("| 🚨 critical | `a.py:1` | Real \\| pipe | verified | 2/2 |", rows[1])
        self.assertIn("| ⚠️ warning (claimed critical) | `b.py:5` | Claimed too high | downgraded | — |", rows[2])
        self.assertIn("| ℹ️ info | `c.py:9` | Something | unverified | — |", rows[3])
        self.assertIn("### Summary\n\nLooks fine.", body)

    def test_no_findings_and_no_verification_line(self) -> None:
        body = _render(reviewer.ReviewResult(findings=[]), narrative="")
        self.assertIn("## Code review — 0 finding(s): 0 critical · 0 warning · 0 info", body)
        self.assertIn("_No findings posted inline._", body)
        self.assertNotIn("Verification:", body)
        self.assertNotIn("### Summary", body)


class NarrativeInvariant(unittest.TestCase):
    def test_unknown_path_line_is_footnoted_and_table_rows_are_not(self) -> None:
        result = reviewer.ReviewResult(findings=[_f("src/a.py", 10, "warning")])
        body = _render(result, narrative="See `src/a.py:10` and also src/gone.py:44 which I removed. Again `src/gone.py:44`.")
        self.assertIn("`src/a.py:10` and", body)
        self.assertNotIn("`src/a.py:10`[^", body)
        self.assertIn("src/gone.py:44[^1]", body)
        self.assertEqual(body.count("[^1]"), 2, "footnote marker once on first mention + the footnote definition")
        self.assertIn("[^1]: `src/gone.py:44` is mentioned above but is not a row of the findings table", body)

    def test_findings_past_the_table_cap_are_still_table_anchors(self) -> None:
        # PR #61 self-review (glm, info): rows are capped for display, but every published
        # finding is posted inline — naming one past the cap must not be footnoted as "not posted".
        n = reviewer.SUMMARY_MAX_TABLE_ROWS + 2
        result = reviewer.ReviewResult(findings=[_f("src/a.py", 10 + i, "warning") for i in range(n)])
        last = f"src/a.py:{10 + n - 1}"
        body = _render(result, narrative=f"See `{last}`.")
        self.assertNotIn("[^1]", body)
        self.assertIn("more inline", body)

    def test_narrative_is_bounded(self) -> None:
        big = "word " * 2000
        body = _render(reviewer.ReviewResult(findings=[]), narrative=big)
        self.assertIn("_[narrative trimmed to 4,000 characters]_", body)
        section = body.split("### Summary\n\n", 1)[1]
        self.assertLess(len(section), reviewer.SUMMARY_NARRATIVE_MAX_CHARS + 200)


class RefutedAndLedger(unittest.TestCase):
    def test_refuted_section_and_prior_ledger(self) -> None:
        result = reviewer.ReviewResult(findings=[_f("a.py", 1, "warning")])
        refuted = _f("z.py", 3, "critical", status="refuted", title="Ghost bug", reason="the guard exists")
        result.refuted.append(refuted)
        def pf(fp: str, path: str, line: int) -> Any:
            return reviewer.PriorFinding(thread_id="T", comment_id="C", comment_database_id=1, path=path, line=line, severity="warning",
                                         fingerprint=fp, body_excerpt="b", is_outdated=False)
        rec = reviewer.PriorFindingReconciliation()
        rec.resolved = [pf("1" * 16, "r.py", 1)]; rec.retired_reasons["1" * 16] = "file_removed"
        rec.regressed = [pf("2" * 16, "g.py", 2)]
        open_pf = pf("3" * 16, "o.py", 3); rec.still_open = [open_pf, rec.regressed[0]]; rec.anchor_unchanged = [open_pf]; rec.unverified = [open_pf]
        result.prior_reconciliation = rec
        body = _render(result)
        self.assertIn("### Refuted by the verifier (not posted inline)\n\n- `z.py:3` — Ghost bug: the guard exists", body)
        self.assertIn("- retired `r.py:1` (file_removed)", body)
        self.assertIn("- regressed `g.py:2`", body)
        self.assertIn("- still open `o.py:3` — claimed resolved, anchor unchanged at head", body)
        self.assertEqual(body.count("`g.py:2`"), 1, "a regressed prior is listed once")

    def test_gate_block_still_closes_the_body(self) -> None:
        result = reviewer.ReviewResult(findings=[])
        body = _render(result) .rstrip() + reviewer.render_gate_status_block(blocked=False, block_reason="ok", severity="none", strictness="lenient")
        self.assertTrue(body.rstrip().endswith("not the gate."))
        self.assertLess(body.index("## Code review"), body.index("> **Check status"))


if __name__ == "__main__":
    unittest.main()
