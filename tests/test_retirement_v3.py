"""Finding retirement v3 (RFC-03 § Finding retirement, BC-08) on a real repo:

- `verify_anchor_fixed`: anchor changed → fixed; identical → unchanged; path
  gone → file_removed; raising head unknown → unavailable;
- `reconcile_prior_findings` with `head_sha`: corroborated + anchor changed →
  retired as `verified_fixed`; corroborated + anchor identical → stays open
  (the new refusal, listed in `anchor_unchanged`); file gone → `file_removed`;
  absence never retires (both policies); uncorroborated stays open; re-read
  unavailable → the v2 rule with the reason recorded; `regressed` untouched;
- `assign_lifecycle` states; the footer notes the refusal.
"""

from __future__ import annotations

import importlib.util
import os
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
reviewer.log = lambda msg: None  # type: ignore[assignment]


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, check=True).stdout.strip()


def _prior(fp: str, *, path: str = "app.py", line: int = 12, review_sha: str = "", collapsed: bool = True) -> Any:
    return reviewer.PriorFinding(thread_id="T", comment_id="C", comment_database_id=1, path=path, line=line, severity="critical",
                                 fingerprint=fp, body_excerpt="b", is_outdated=False, is_minimized=collapsed, review_sha=review_sha)


class _Repo(unittest.TestCase):
    """raise@sha0: app.py 30 lines, other.py; head1 edits app.py line 25 (far from the anchor at 12);
    head2 edits app.py line 12; head3 deletes app.py."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self._tmp.name)
        _git(self.repo, "init", "-q", "-b", "main")
        _git(self.repo, "config", "user.email", "t@example.invalid"); _git(self.repo, "config", "user.name", "t")
        _git(self.repo, "config", "commit.gpgsign", "false")
        body = "".join(f"line {i}\n" for i in range(1, 31))
        (self.repo / "app.py").write_text(body); (self.repo / "other.py").write_text("x\n")
        _git(self.repo, "add", "-A"); _git(self.repo, "commit", "-q", "-m", "raise"); self.sha0 = _git(self.repo, "rev-parse", "HEAD")
        (self.repo / "app.py").write_text(body.replace("line 25\n", "line 25 changed\n"))
        _git(self.repo, "add", "-A"); _git(self.repo, "commit", "-q", "-m", "far"); self.sha1 = _git(self.repo, "rev-parse", "HEAD")
        (self.repo / "app.py").write_text(body.replace("line 12\n", "line 12 fixed\n"))
        _git(self.repo, "add", "-A"); _git(self.repo, "commit", "-q", "-m", "anchor"); self.sha2 = _git(self.repo, "rev-parse", "HEAD")
        (self.repo / "app.py").unlink()
        _git(self.repo, "add", "-A"); _git(self.repo, "commit", "-q", "-m", "gone"); self.sha3 = _git(self.repo, "rev-parse", "HEAD")
        self._cwd = os.getcwd(); os.chdir(self.repo)

    def tearDown(self) -> None:
        os.chdir(self._cwd); self._tmp.cleanup()

    def _delta(self, head: str, files: tuple[str, ...] = ("app.py",)) -> Any:
        return reviewer.IncrementalDelta(prior_head_sha=self.sha0, head_sha=head, changed_files=files, delta_ratio=0.3)


class AnchorReread(_Repo):
    def test_verdicts(self) -> None:
        pf = _prior("f" * 16, review_sha=self.sha0)
        self.assertEqual(reviewer.verify_anchor_fixed(pf, head_sha=self.sha1)[0], "unchanged")
        self.assertEqual(reviewer.verify_anchor_fixed(pf, head_sha=self.sha2)[0], "fixed")
        self.assertEqual(reviewer.verify_anchor_fixed(pf, head_sha=self.sha3)[0], "file_removed")
        self.assertEqual(reviewer.verify_anchor_fixed(_prior("f" * 16), head_sha=self.sha2)[0], "unavailable")
        self.assertEqual(reviewer.verify_anchor_fixed(_prior("f" * 16, review_sha="0" * 40), head_sha=self.sha2)[0], "unavailable")
        self.assertEqual(reviewer.verify_anchor_fixed(pf, head_sha="")[0], "unavailable")


class Reconcile(_Repo):
    def _rec(self, *, head: str, updates: dict[str, tuple[str, str]], policy: str = reviewer.RESOLUTION_POLICY_VERIFIED,
             collapsed: bool = True, current: set[str] | None = None, review_sha: str | None = None, files: tuple[str, ...] = ("app.py",)) -> Any:
        pf = _prior("f" * 16, review_sha=self.sha0 if review_sha is None else review_sha, collapsed=collapsed)
        _git(self.repo, "checkout", "-q", head)  # the working tree is at the head under review, as in a real run
        return reviewer.reconcile_prior_findings(prior_findings=[pf], updates=updates, current_fingerprints=current or set(),
                                                 delta=self._delta(head, files), workspace=self.repo, policy=policy, head_sha=head), pf

    def test_corroborated_but_anchor_unchanged_stays_open_with_reason(self) -> None:
        for policy in (reviewer.RESOLUTION_POLICY_VERIFIED, reviewer.RESOLUTION_POLICY_ADVISORY):
            rec, pf = self._rec(head=self.sha1, updates={"f" * 16: ("resolved", "fixed it")}, policy=policy)
            self.assertEqual(rec.resolved, [], policy)
            self.assertEqual(rec.anchor_unchanged, [pf], policy)
            self.assertIn(pf, rec.still_open); self.assertIn(pf, rec.unverified)
            self.assertIn("anchor unchanged", reviewer.render_incremental_footer(delta=self._delta(self.sha1), reconciliation=rec, new_findings=0))

    def test_corroborated_and_anchor_changed_retires_as_verified_fixed(self) -> None:
        rec, pf = self._rec(head=self.sha2, updates={"f" * 16: ("resolved", "fixed it")})
        self.assertEqual(rec.resolved, [pf]); self.assertEqual(rec.retired_reasons["f" * 16], "verified_fixed")
        self.assertEqual(rec.still_open, []); self.assertEqual(rec.anchor_unchanged, [])
        rec, pf = self._rec(head=self.sha2, updates={"f" * 16: ("resolved", "x")}, policy=reviewer.RESOLUTION_POLICY_ADVISORY, collapsed=True)
        self.assertEqual(rec.auto_retired, [pf])

    def test_file_removed_reason(self) -> None:
        rec, pf = self._rec(head=self.sha3, updates={"f" * 16: ("resolved", "x")})
        self.assertEqual(rec.resolved, [pf]); self.assertEqual(rec.retired_reasons["f" * 16], "file_removed")

    def test_absence_never_retires_and_uncorroborated_stays_open(self) -> None:
        for policy in (reviewer.RESOLUTION_POLICY_VERIFIED, reviewer.RESOLUTION_POLICY_ADVISORY):
            rec, pf = self._rec(head=self.sha2, updates={}, policy=policy)          # no verdict, anchor changed
            self.assertEqual(rec.resolved, []); self.assertEqual(rec.still_open, [pf])
            rec, pf = self._rec(head=self.sha2, updates={"f" * 16: ("resolved", "x")}, policy=policy, current={"f" * 16})  # re-emitted this round
            self.assertEqual(rec.resolved, []); self.assertIn(pf, rec.unverified)
            rec, pf = self._rec(head=self.sha2, updates={"f" * 16: ("resolved", "x")}, policy=policy, files=("other.py",))  # file untouched
            self.assertEqual(rec.resolved, []); self.assertIn(pf, rec.unverified)
        rec, pf = self._rec(head=self.sha2, updates={"f" * 16: ("resolved", "x")}, policy=reviewer.RESOLUTION_POLICY_ADVISORY, collapsed=False)
        self.assertEqual(rec.resolved, [], "advisory with a resolvable thread stays open")

    def test_reread_unavailable_falls_back_to_the_v2_rule_with_reason(self) -> None:
        rec, pf = self._rec(head=self.sha2, updates={"f" * 16: ("resolved", "x")}, review_sha="")
        self.assertEqual(rec.resolved, [pf]); self.assertIn("anchor re-read unavailable", rec.retired_reasons["f" * 16])

    def test_regressed_is_untouched(self) -> None:
        rec, pf = self._rec(head=self.sha2, updates={"f" * 16: ("regressed", "worse")})
        self.assertEqual(rec.regressed, [pf]); self.assertEqual(rec.resolved, [])


class Lifecycle(unittest.TestCase):
    def test_states(self) -> None:
        new = reviewer.Finding(path="a.py", line=1, body="b", fingerprint="1" * 16)
        old = reviewer.Finding(path="a.py", line=2, body="b", fingerprint="2" * 16)
        reg = reviewer.Finding(path="a.py", line=3, body="b", fingerprint="3" * 16)
        none = reviewer.Finding(path="a.py", line=4, body="b")
        reviewer.assign_lifecycle([new, old, reg, none], prior_open_fingerprints={"2" * 16, "3" * 16}, regressed_fingerprints={"3" * 16})
        self.assertEqual([f.lifecycle["state"] for f in (new, old, reg, none)], ["new", "open", "regressed", "new"])
        self.assertEqual(old.to_v3_dict()["lifecycle"]["state"], "open")


if __name__ == "__main__":
    unittest.main()
