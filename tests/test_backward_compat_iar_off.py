#!/usr/bin/env python3
"""Opt-out regression suite for the Iteration-Aware Review (IAR) subsystem.

As of v1.8.0 the IAR subsystem is **on by default**
(`iteration-awareness-enabled: true`, `convergence-policy:
first-pass-exhaustive`). Consumers who want the pre-v1.8 review shape can
explicitly set `iteration-awareness-enabled: false` — the load-bearing
correctness invariant of the opt-out path is:

    When the master switch is explicitly OFF, the runtime path is
    byte-identical to prior releases.

This suite verifies that invariant with narrow, pure-logic assertions that
cannot inadvertently drift as the IAR internals evolve. Any regression here
means IAR has bled into a code path a consumer opted out of and MUST be
treated as a blocker.

The file is still named `test_backward_compat_iar_off.py` (rather than
being renamed to `test_iar_opt_out.py`) so `git blame` continues to point
at the original intent; the semantics of "off" simply flipped from "the
default" to "an explicit opt-out" in v1.8.0.

Stdlib `unittest` only — matches the rest of `tests/*`. Run with:

    python3 -m unittest discover -s tests -v
"""

from __future__ import annotations

import importlib.util
import os
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Import the module under test from scripts/reviewer.py without requiring it
# to be installed or on PYTHONPATH. Same mechanism as tests/test_reviewer.py.
# ---------------------------------------------------------------------------
_ROOT: Path = Path(__file__).resolve().parent.parent
_SPEC = importlib.util.spec_from_file_location(
    "reviewer", _ROOT / "scripts" / "reviewer.py"
)
assert _SPEC is not None and _SPEC.loader is not None
reviewer = importlib.util.module_from_spec(_SPEC)
sys.modules["reviewer"] = reviewer
_SPEC.loader.exec_module(reviewer)


def _parse_github_outputs(raw: str) -> dict[str, str]:
    """Parse the two shapes emitted by `write_action_output`:

    * Single-line scalar (the common case): ``key=value\\n``.
    * Multi-line HEREDOC: ``key<<AIPRR_OUTPUT_EOF\\nvalue\\nAIPRR_OUTPUT_EOF\\n``.

    Returns a dict[key -> value]. Empty-string values ``key=\\n`` decode
    to ``""``.
    """
    result: dict[str, str] = {}
    lines: list[str] = raw.split("\n")
    i: int = 0
    while i < len(lines):
        line: str = lines[i]
        if "<<" in line and "=" not in line.split("<<", 1)[0]:
            key: str
            heredoc: str
            key, heredoc = line.split("<<", 1)
            value_lines: list[str] = []
            i += 1
            while i < len(lines) and lines[i] != heredoc:
                value_lines.append(lines[i])
                i += 1
            result[key] = "\n".join(value_lines)
        elif "=" in line:
            key, value = line.split("=", 1)
            result[key] = value
        i += 1
    return result


class IAROptOutContractTests(unittest.TestCase):
    """When a consumer explicitly sets `iteration-awareness-enabled: false`
    the runtime MUST behave byte-identically to pre-v1.8 releases —
    regardless of what the other four IAR env vars carry."""

    def test_build_iar_config_explicit_false_returns_disabled(self) -> None:
        """`AIPRR_ITERATION_AWARENESS_ENABLED=false` MUST disable IAR
        end-to-end. Any regression here breaks the opt-out contract."""
        cfg: reviewer.IARConfig = reviewer.build_iar_config({
            "AIPRR_ITERATION_AWARENESS_ENABLED": "false",
        })
        self.assertFalse(
            cfg.enabled,
            "AIPRR_ITERATION_AWARENESS_ENABLED='false' MUST disable IAR. "
            "Any change that makes this True breaks the v1.8+ opt-out contract.",
        )

    def test_build_iar_config_switch_off_ignores_other_fields(self) -> None:
        """When the master switch is explicitly off, the other IAR env
        vars are still parsed for shape (so a subsequent flip of the
        master switch never breaks), but the runtime path is byte-
        identical. This test locks in that the parser is lenient enough
        to not crash on garbage."""
        cfg: reviewer.IARConfig = reviewer.build_iar_config({
            "AIPRR_ITERATION_AWARENESS_ENABLED": "false",
            "AIPRR_CONVERGENCE_POLICY": "not-a-real-policy",
            "AIPRR_MAX_REVIEW_ROUNDS": "not-a-number",
            "AIPRR_EXHAUSTIVE_FIRST_PASS_CAP_MULTIPLIER": "-99",
            "AIPRR_ITERATION_ESCAPE_LABEL": "",
        })
        self.assertFalse(cfg.enabled)
        # Falls back to defaults; no exception. In v1.8+ the fallback
        # for an unknown policy is `first-pass-exhaustive` (matches the
        # action.yml default), but this only matters when the master
        # switch is on — it's asserted here purely as a shape check.
        self.assertEqual(cfg.policy, reviewer.IAR_POLICY_FIRST_PASS_EXHAUSTIVE)
        self.assertEqual(cfg.max_review_rounds, 0)
        self.assertEqual(cfg.cap_multiplier, 1)  # clamped up from -99
        self.assertEqual(cfg.escape_label, reviewer.IAR_DEFAULT_ESCAPE_LABEL)

    def test_build_iar_config_various_falsy_values(self) -> None:
        """Every accepted "falsy" string keeps IAR off — same shape as
        every other repo-native bool flag (`author-association`,
        `label-gate`, etc.). parse_bool contract must stay symmetric
        across the codebase."""
        for raw in ("false", "False", "FALSE", "no", "0", "off"):
            with self.subTest(raw=raw):
                cfg: reviewer.IARConfig = reviewer.build_iar_config({
                    "AIPRR_ITERATION_AWARENESS_ENABLED": raw,
                })
                self.assertFalse(
                    cfg.enabled,
                    f"Raw env value {raw!r} unexpectedly enabled IAR. "
                    "parse_bool contract must stay symmetric with pre-IAR flags.",
                )

    def test_build_iar_config_empty_env_returns_v18_defaults(self) -> None:
        """v1.8.0 behavior change: a completely empty env dict now
        produces IAR-on with the `first-pass-exhaustive` policy — this
        matches the shipped action.yml defaults. Consumers who want the
        pre-v1.8 shape must explicitly set
        `iteration-awareness-enabled: false`."""
        cfg: reviewer.IARConfig = reviewer.build_iar_config({})
        self.assertTrue(
            cfg.enabled,
            "v1.8.0+: IAR master switch defaults to True. If this fails, "
            "either action.yml default was flipped back OR build_iar_config "
            "no longer matches the action.yml contract.",
        )
        self.assertEqual(cfg.policy, reviewer.IAR_POLICY_FIRST_PASS_EXHAUSTIVE)
        self.assertEqual(cfg.cap_multiplier, reviewer.IAR_DEFAULT_CAP_MULTIPLIER)
        self.assertEqual(cfg.escape_label, reviewer.IAR_DEFAULT_ESCAPE_LABEL)


class IARConfigParsingTests(unittest.TestCase):
    """Basic parser contract — validates the shape, not the runtime effect
    (which the master switch gates)."""

    def test_switch_on_and_valid_policy(self) -> None:
        cfg: reviewer.IARConfig = reviewer.build_iar_config({
            "AIPRR_ITERATION_AWARENESS_ENABLED": "true",
            "AIPRR_CONVERGENCE_POLICY": "first-pass-exhaustive",
            "AIPRR_MAX_REVIEW_ROUNDS": "5",
            "AIPRR_EXHAUSTIVE_FIRST_PASS_CAP_MULTIPLIER": "4",
            "AIPRR_ITERATION_ESCAPE_LABEL": "audit-me",
        })
        self.assertTrue(cfg.enabled)
        self.assertEqual(cfg.policy, "first-pass-exhaustive")
        self.assertEqual(cfg.max_review_rounds, 5)
        self.assertEqual(cfg.cap_multiplier, 4)
        self.assertEqual(cfg.escape_label, "audit-me")

    def test_unknown_policy_silently_falls_back(self) -> None:
        """Unknown policy values MUST fall back to the shipped default
        (`first-pass-exhaustive` as of v1.8.0) rather than crashing.
        The workflow log surfaces the miswiring via the log() call in
        main()."""
        cfg: reviewer.IARConfig = reviewer.build_iar_config({
            "AIPRR_ITERATION_AWARENESS_ENABLED": "true",
            "AIPRR_CONVERGENCE_POLICY": "bogus-policy",
        })
        self.assertTrue(cfg.enabled)
        self.assertEqual(cfg.policy, reviewer.IAR_POLICY_FIRST_PASS_EXHAUSTIVE)

    def test_max_rounds_negative_clamped_to_zero(self) -> None:
        cfg: reviewer.IARConfig = reviewer.build_iar_config({
            "AIPRR_ITERATION_AWARENESS_ENABLED": "true",
            "AIPRR_MAX_REVIEW_ROUNDS": "-3",
        })
        self.assertEqual(cfg.max_review_rounds, 0)

    def test_cap_multiplier_below_1_clamped_to_1(self) -> None:
        cfg: reviewer.IARConfig = reviewer.build_iar_config({
            "AIPRR_ITERATION_AWARENESS_ENABLED": "true",
            "AIPRR_EXHAUSTIVE_FIRST_PASS_CAP_MULTIPLIER": "0",
        })
        self.assertEqual(cfg.cap_multiplier, 1)

    def test_escape_label_defaults_when_blank(self) -> None:
        cfg: reviewer.IARConfig = reviewer.build_iar_config({
            "AIPRR_ITERATION_AWARENESS_ENABLED": "true",
            "AIPRR_ITERATION_ESCAPE_LABEL": "   ",
        })
        self.assertEqual(cfg.escape_label, reviewer.IAR_DEFAULT_ESCAPE_LABEL)


class WriteAllOutputsBackwardCompatTests(unittest.TestCase):
    """The `write_all_outputs()` helper is on every exit path. It MUST
    still write the 6 pre-IAR outputs, and it MUST write the 5 new IAR
    outputs as empty strings so downstream steps reading them always see
    a defined value (never a missing key)."""

    def _write_and_read_outputs(
        self, **kwargs: Any
    ) -> dict[str, str]:
        """Redirect $GITHUB_OUTPUT to a tempfile, run write_all_outputs,
        parse the resulting file into a dict."""
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".txt", delete=False
        ) as tmp:
            tmp_path: str = tmp.name
        prior: str | None = os.environ.get("GITHUB_OUTPUT")
        try:
            os.environ["GITHUB_OUTPUT"] = tmp_path
            reviewer.write_all_outputs(**kwargs)
            with open(tmp_path, encoding="utf-8") as fh:
                raw: str = fh.read()
        finally:
            if prior is None:
                os.environ.pop("GITHUB_OUTPUT", None)
            else:
                os.environ["GITHUB_OUTPUT"] = prior
            os.unlink(tmp_path)
        return _parse_github_outputs(raw)

    def test_skipped_path_writes_all_11_outputs(self) -> None:
        """The skip path (e.g. author-association gate rejected the PR)
        must write all 6 pre-IAR outputs AND all 5 IAR outputs. That's
        11 total. IAR outputs must be empty strings by default."""
        out: dict[str, str] = self._write_and_read_outputs(skipped=True)
        pre_iar_keys: set[str] = {
            "skipped", "severity", "inline-attached",
            "inline-dropped", "blocked", "review-url",
        }
        iar_keys: set[str] = {
            "iteration-round", "iteration-generation",
            "iteration-policy-applied", "iteration-tokens-used",
            "iteration-cost-vs-baseline-estimate",
        }
        for key in pre_iar_keys | iar_keys:
            self.assertIn(
                key, out,
                f"Missing output {key!r} — write_all_outputs contract broken.",
            )
        # Pre-IAR shape on skip.
        self.assertEqual(out["skipped"], "true")
        self.assertEqual(out["severity"], "none")
        self.assertEqual(out["inline-attached"], "0")
        self.assertEqual(out["inline-dropped"], "0")
        self.assertEqual(out["blocked"], "false")
        self.assertEqual(out["review-url"], "")
        # IAR values MUST be empty strings when the runtime never
        # populated them (i.e. IAR was disabled OR the code path never
        # reached the IAR-populating helper).
        for key in iar_keys:
            self.assertEqual(
                out[key], "",
                f"IAR output {key!r} MUST be empty string on IAR-off paths.",
            )

    def test_success_path_writes_all_11_outputs(self) -> None:
        """The success path with a real severity + attached comments
        still keeps the 5 IAR outputs empty when IAR is off."""
        out: dict[str, str] = self._write_and_read_outputs(
            skipped=False,
            severity="warning",
            inline_attached=3,
            inline_dropped=1,
            blocked=False,
            review_url="https://github.com/x/y/pull/1#pullrequestreview-42",
        )
        self.assertEqual(out["skipped"], "false")
        self.assertEqual(out["severity"], "warning")
        self.assertEqual(out["inline-attached"], "3")
        self.assertEqual(out["inline-dropped"], "1")
        self.assertEqual(out["blocked"], "false")
        self.assertEqual(
            out["review-url"],
            "https://github.com/x/y/pull/1#pullrequestreview-42",
        )
        # All 5 IAR outputs empty.
        self.assertEqual(out["iteration-round"], "")
        self.assertEqual(out["iteration-generation"], "")
        self.assertEqual(out["iteration-policy-applied"], "")
        self.assertEqual(out["iteration-tokens-used"], "")
        self.assertEqual(out["iteration-cost-vs-baseline-estimate"], "")

    def test_blocked_path_writes_all_11_outputs(self) -> None:
        """Strictness-blocked exit path also writes IAR outputs empty."""
        out: dict[str, str] = self._write_and_read_outputs(
            skipped=False,
            severity="critical",
            inline_attached=5,
            inline_dropped=0,
            blocked=True,
            review_url="https://github.com/x/y/pull/1#pullrequestreview-99",
        )
        self.assertEqual(out["blocked"], "true")
        # Master-switch-off contract: IAR outputs stay empty.
        self.assertEqual(out["iteration-round"], "")
        self.assertEqual(out["iteration-cost-vs-baseline-estimate"], "")


class IARConstantsFrozenTests(unittest.TestCase):
    """Small guard against accidental drift in IAR constants that other
    tasks (3–8) will depend on. If a constant changes intentionally, this
    test needs an intentional update — that's the point."""

    def test_valid_policies_tuple(self) -> None:
        self.assertEqual(
            set(reviewer.IAR_VALID_POLICIES),
            {"iterative", "first-pass-exhaustive",
             "round-capped", "critical-gate"},
        )

    def test_default_cap_multiplier(self) -> None:
        self.assertEqual(reviewer.IAR_DEFAULT_CAP_MULTIPLIER, 3)

    def test_context_hash_radius(self) -> None:
        # 10 lines above + 10 below = 21-line window (fingerprint anchor).
        self.assertEqual(reviewer.IAR_CONTEXT_HASH_RADIUS, 10)

    def test_safety_net_threshold(self) -> None:
        # docs/ITERATION_AWARENESS.md § 7.2 pins this at 30%.
        self.assertEqual(reviewer.IAR_SAFETY_NET_NEW_LINES_PCT, 30)

    def test_default_escape_label(self) -> None:
        self.assertEqual(
            reviewer.IAR_DEFAULT_ESCAPE_LABEL, "full-review-please"
        )

    def test_state_schema_version_is_1(self) -> None:
        # Increment only when the JSON schema breaks backward-read compat
        # AND _parse_state_from_marker_body handles the older shape.
        self.assertEqual(reviewer.IAR_STATE_SCHEMA_VERSION, 1)


class IARDoesNotLeakIntoOtherCodePathsTests(unittest.TestCase):
    """Sanity-check that the IAR module surface is small and contained.
    If a future refactor mixes IAR into an unrelated function, this test
    should be the first to fail."""

    def test_iar_config_dataclass_is_frozen(self) -> None:
        """IARConfig is built once per run and passed around. A frozen
        dataclass prevents accidental mutation deep in the runtime."""
        cfg: reviewer.IARConfig = reviewer.build_iar_config({})
        with self.assertRaises((AttributeError, Exception)):
            cfg.enabled = True  # type: ignore[misc]

    def test_write_iar_outputs_empty_writes_exactly_5_keys(self) -> None:
        """`write_iar_outputs_empty()` is the sole helper that populates
        IAR outputs on the off path. It must write exactly the 5 IAR
        outputs (no more, no fewer) so it can be swapped for the
        populate-real-values helper in Task 8 without contamination."""
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".txt", delete=False
        ) as tmp:
            tmp_path: str = tmp.name
        prior: str | None = os.environ.get("GITHUB_OUTPUT")
        try:
            os.environ["GITHUB_OUTPUT"] = tmp_path
            reviewer.write_iar_outputs_empty()
            with open(tmp_path, encoding="utf-8") as fh:
                raw: str = fh.read()
        finally:
            if prior is None:
                os.environ.pop("GITHUB_OUTPUT", None)
            else:
                os.environ["GITHUB_OUTPUT"] = prior
            os.unlink(tmp_path)
        keys: set[str] = set(_parse_github_outputs(raw).keys())
        self.assertEqual(
            keys,
            {"iteration-round", "iteration-generation",
             "iteration-policy-applied", "iteration-tokens-used",
             "iteration-cost-vs-baseline-estimate"},
            "write_iar_outputs_empty MUST write exactly the 5 IAR outputs.",
        )


if __name__ == "__main__":
    unittest.main()
