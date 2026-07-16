#!/usr/bin/env python3
"""Unit tests for the Iteration-Aware Review (IAR) state layer —
`IterationState` dataclass, `read_prior_iteration_state`,
`_fetch_latest_marker_body`, `_parse_state_from_marker_body`, and
`embed_iteration_state`.

Every function under test lives in `scripts/reviewer.py`. IAR is
unconditional, but the state-layer helpers themselves must be robust
to malformed markers written by prior runs (schema drift, truncated
JSON, missing fields, unknown versions) — parse failures MUST return
`None` cleanly so the reviewer treats the run as a first-review
instead of crashing. Failure-fallback tests live in
`test_iar_failure_fallback.py`.

Stdlib `unittest` only.
"""

from __future__ import annotations

import importlib.util
import json
import sys
import unittest
from dataclasses import asdict
from pathlib import Path
from typing import Any
from unittest.mock import patch

_ROOT: Path = Path(__file__).resolve().parent.parent
_SPEC = importlib.util.spec_from_file_location(
    "reviewer", _ROOT / "scripts" / "reviewer.py"
)
assert _SPEC is not None and _SPEC.loader is not None
reviewer = importlib.util.module_from_spec(_SPEC)
sys.modules["reviewer"] = reviewer
_SPEC.loader.exec_module(reviewer)


def _make_state(**overrides: Any) -> "reviewer.IterationState":
    """Build an IterationState with plausible defaults; override any
    field for test scenarios."""
    base: dict[str, Any] = {
        "version": reviewer.IAR_STATE_SCHEMA_VERSION,
        "generation": 2,
        "generation_range_hash": "abc123def456",
        "round_in_generation": 3,
        "policy_applied": reviewer.IAR_POLICY_ITERATIVE,
        "resolved_fingerprints": ["fp-resolved-1", "fp-resolved-2"],
        "open_fingerprints_this_gen": ["fp-open-3"],
        "history": [
            {
                "gen": 1,
                "range_hash": "prev-hash-xyz",
                "rounds_ran": 3,
                "converged": True,
                "tokens_used": 5000,
                "wall_clock_ms": 45000,
            }
        ],
    }
    base.update(overrides)
    return reviewer.IterationState(**base)


class IterationStateRoundTripTests(unittest.TestCase):
    """embed → parse → deep-equal invariant, across a matrix of shapes."""

    def test_roundtrip_preserves_all_fields(self) -> None:
        state: reviewer.IterationState = _make_state()
        body: str = "### Tracking marker\n\nBody text.\n"
        new_body: str = reviewer.embed_iteration_state(body, state)
        parsed: reviewer.IterationState | None = (
            reviewer._parse_state_from_marker_body(new_body)
        )
        self.assertIsNotNone(parsed)
        self.assertEqual(parsed, state)

    def test_roundtrip_with_empty_fingerprint_lists(self) -> None:
        state: reviewer.IterationState = _make_state(
            resolved_fingerprints=[],
            open_fingerprints_this_gen=[],
            history=[],
        )
        body: str = reviewer.REVIEW_MARKER + "\n\n" + "some text"
        new_body: str = reviewer.embed_iteration_state(body, state)
        parsed: reviewer.IterationState | None = (
            reviewer._parse_state_from_marker_body(new_body)
        )
        self.assertEqual(parsed, state)

    def test_roundtrip_with_unicode_hash(self) -> None:
        """Fingerprints are ASCII SHA hashes today, but the parser must
        survive arbitrary strings in case a future scheme lands."""
        state: reviewer.IterationState = _make_state(
            resolved_fingerprints=["ünïcödé-fingerprint", "abc123"],
            generation_range_hash="こんにちは",
        )
        new_body: str = reviewer.embed_iteration_state("marker", state)
        parsed: reviewer.IterationState | None = (
            reviewer._parse_state_from_marker_body(new_body)
        )
        self.assertEqual(parsed, state)

    def test_embed_is_deterministic(self) -> None:
        """Same inputs → byte-identical output. Important because a
        non-deterministic embed makes debugging state drift very hard."""
        state: reviewer.IterationState = _make_state()
        body: str = "body"
        a: str = reviewer.embed_iteration_state(body, state)
        b: str = reviewer.embed_iteration_state(body, state)
        self.assertEqual(a, b)

    def test_embed_replaces_existing_block(self) -> None:
        """Two consecutive embeds MUST produce a body with exactly one
        state block, not two stacked."""
        state_a: reviewer.IterationState = _make_state(generation=1)
        state_b: reviewer.IterationState = _make_state(generation=5)
        body: str = "marker"
        step_1: str = reviewer.embed_iteration_state(body, state_a)
        step_2: str = reviewer.embed_iteration_state(step_1, state_b)
        self.assertEqual(step_2.count(reviewer.IAR_STATE_TAG_OPEN), 1)
        parsed: reviewer.IterationState | None = (
            reviewer._parse_state_from_marker_body(step_2)
        )
        self.assertIsNotNone(parsed)
        self.assertEqual(parsed.generation, 5)

    def test_embed_bounds_history_to_max_entries(self) -> None:
        """History is capped so a long-lived PR does not grow the marker
        body unboundedly."""
        oversized: list[dict[str, Any]] = [
            {"gen": i, "range_hash": f"h{i}", "rounds_ran": 1,
             "converged": True, "tokens_used": 100, "wall_clock_ms": 1000}
            for i in range(50)
        ]
        state: reviewer.IterationState = _make_state(history=oversized)
        embedded: str = reviewer.embed_iteration_state("marker", state)
        parsed: reviewer.IterationState | None = (
            reviewer._parse_state_from_marker_body(embedded)
        )
        self.assertIsNotNone(parsed)
        self.assertEqual(
            len(parsed.history), reviewer.IAR_HISTORY_MAX_ENTRIES
        )
        # It's the LAST N (most recent) that survive.
        self.assertEqual(parsed.history[-1]["gen"], 49)

    def test_roundtrip_preserves_reviewed_label_applied_bit(self) -> None:
        """The `reviewed_label_applied` bit is the load-bearing signal
        for the USER_FORCED_RESET gesture — it MUST survive embed →
        parse without truncation, sanitisation, or type coercion.
        Round-trip both explicit boolean values."""
        for expected in (True, False):
            with self.subTest(reviewed_label_applied=expected):
                state: reviewer.IterationState = _make_state()
                state.reviewed_label_applied = expected
                embedded: str = reviewer.embed_iteration_state("m", state)
                parsed: reviewer.IterationState | None = (
                    reviewer._parse_state_from_marker_body(embedded)
                )
                self.assertIsNotNone(parsed)
                self.assertEqual(parsed.reviewed_label_applied, expected)

    def test_parses_pre_v1_state_without_reviewed_label_applied(self) -> None:
        """Backward compat: a state body written before the field
        existed (i.e., no `reviewed_label_applied` key in the JSON
        block) must parse cleanly and default the field to `False`. The
        `False` default is the SAFE side of the reset gesture — it
        suppresses USER_FORCED_RESET until the reviewer completes one
        successful run and re-writes the state with the bit set (see
        `test_iar_observability.RunIarPreLlmTests.
        test_user_forced_reset_no_op_when_prior_state_never_stamped_label`)."""
        legacy_json: str = json.dumps({
            "version": reviewer.IAR_STATE_SCHEMA_VERSION,
            "generation": 3,
            "generation_range_hash": "abc",
            "round_in_generation": 2,
            "policy_applied": reviewer.IAR_POLICY_ITERATIVE,
            "resolved_fingerprints": [],
            "open_fingerprints_this_gen": [],
            "history": [],
            "base_sha": "b",
            "head_sha": "h",
            # DELIBERATELY OMITS reviewed_label_applied
        })
        body: str = (
            f"marker\n{reviewer.IAR_STATE_TAG_OPEN}\n"
            f"{legacy_json}\n"
            f"{reviewer.IAR_STATE_TAG_CLOSE}\n"
        )
        parsed: reviewer.IterationState | None = (
            reviewer._parse_state_from_marker_body(body)
        )
        self.assertIsNotNone(parsed)
        self.assertFalse(parsed.reviewed_label_applied)
        self.assertEqual(parsed.generation, 3)  # other fields unaffected


class ParseFailureTests(unittest.TestCase):
    """Every failure mode falls back to `None` — never raises."""

    def test_missing_state_block_returns_none(self) -> None:
        """First-review case: marker exists but has no embedded state."""
        body: str = f"{reviewer.REVIEW_MARKER}\n\nSome body without state."
        self.assertIsNone(
            reviewer._parse_state_from_marker_body(body)
        )

    def test_empty_body_returns_none(self) -> None:
        self.assertIsNone(reviewer._parse_state_from_marker_body(""))

    def test_malformed_json_returns_none(self) -> None:
        body: str = (
            f"{reviewer.IAR_STATE_TAG_OPEN}\n"
            "{not valid json {{{\n"
            f"{reviewer.IAR_STATE_TAG_CLOSE}"
        )
        self.assertIsNone(reviewer._parse_state_from_marker_body(body))

    def test_unknown_version_returns_none(self) -> None:
        """Schema version > current runtime → fallback to None (safest;
        avoids interpreting a shape we don't understand)."""
        body: str = (
            f"{reviewer.IAR_STATE_TAG_OPEN}\n"
            + json.dumps({"version": 99, "generation": 1})
            + f"\n{reviewer.IAR_STATE_TAG_CLOSE}"
        )
        self.assertIsNone(reviewer._parse_state_from_marker_body(body))

    def test_missing_version_returns_none(self) -> None:
        body: str = (
            f"{reviewer.IAR_STATE_TAG_OPEN}\n"
            + json.dumps({"generation": 1})
            + f"\n{reviewer.IAR_STATE_TAG_CLOSE}"
        )
        self.assertIsNone(reviewer._parse_state_from_marker_body(body))

    def test_root_is_not_object_returns_none(self) -> None:
        body: str = (
            f"{reviewer.IAR_STATE_TAG_OPEN}\n"
            "[1, 2, 3]\n"
            f"{reviewer.IAR_STATE_TAG_CLOSE}"
        )
        self.assertIsNone(reviewer._parse_state_from_marker_body(body))

    def test_wrong_type_for_int_field_returns_none(self) -> None:
        body: str = (
            f"{reviewer.IAR_STATE_TAG_OPEN}\n"
            + json.dumps({
                "version": 1,
                "generation": "not-an-int",
                "generation_range_hash": "x",
                "round_in_generation": 1,
                "policy_applied": "iterative",
                "resolved_fingerprints": [],
                "open_fingerprints_this_gen": [],
                "history": [],
            })
            + f"\n{reviewer.IAR_STATE_TAG_CLOSE}"
        )
        self.assertIsNone(reviewer._parse_state_from_marker_body(body))


class TrustBoundaryHardeningTests(unittest.TestCase):
    """Round-10 F2/F3 regression: parser MUST NOT trust untyped JSON.
    Poisoned markers (from a compromised bot account or a workflow
    re-run that edits the same comment — the author filter still
    keeps random PR participants out) should degrade to safe defaults,
    not crash the runtime or spuriously arm USER_FORCED_RESET.
    """

    def _wrap(self, payload: dict[str, Any]) -> str:
        return (
            f"marker\n{reviewer.IAR_STATE_TAG_OPEN}\n"
            + json.dumps(payload)
            + f"\n{reviewer.IAR_STATE_TAG_CLOSE}"
        )

    def test_reviewed_label_applied_rejects_string_false(self) -> None:
        """`bool("false")` is `True` in Python — the parser must NOT
        naively `bool()` the JSON value. String `"false"` / `"no"` /
        `"0"` all fall back to `False` (the safe default —
        USER_FORCED_RESET disarmed rather than spuriously triggered)."""
        for weird_value in ("false", "no", "0", "off", "None"):
            body: str = self._wrap({
                "version": 1,
                "generation": 1,
                "generation_range_hash": "abc",
                "round_in_generation": 1,
                "policy_applied": "iterative",
                "resolved_fingerprints": [],
                "open_fingerprints_this_gen": [],
                "history": [],
                "reviewed_label_applied": weird_value,
            })
            parsed = reviewer._parse_state_from_marker_body(body)
            self.assertIsNotNone(parsed, weird_value)
            self.assertFalse(
                parsed.reviewed_label_applied,
                f"non-boolean {weird_value!r} must NOT arm reset",
            )

    def test_reviewed_label_applied_accepts_json_true(self) -> None:
        body: str = self._wrap({
            "version": 1, "generation": 1,
            "generation_range_hash": "abc",
            "round_in_generation": 1, "policy_applied": "iterative",
            "resolved_fingerprints": [], "open_fingerprints_this_gen": [],
            "history": [],
            "reviewed_label_applied": True,
        })
        parsed = reviewer._parse_state_from_marker_body(body)
        self.assertIsNotNone(parsed)
        self.assertTrue(parsed.reviewed_label_applied)

    def test_fingerprint_list_drops_non_string_elements(self) -> None:
        """Round-10 F3 sticky-DoS regression: `set(list_with_dict)`
        raises `TypeError: unhashable type: 'dict'` inside
        `dedupe_findings_against_prior`, which crashes the whole IAR
        pipeline and falls back to baseline on every subsequent run
        until the poisoned marker ages out. Coerce at parse time —
        keep only str entries."""
        body: str = self._wrap({
            "version": 1, "generation": 1,
            "generation_range_hash": "abc",
            "round_in_generation": 1, "policy_applied": "iterative",
            "resolved_fingerprints": [
                "valid-fp-1",
                {"attacker": "dict"},
                42,
                None,
                ["nested", "list"],
                "valid-fp-2",
            ],
            "open_fingerprints_this_gen": [
                {"another": "dict"},
                "keep-me",
            ],
            "history": [],
            "reviewed_label_applied": True,
        })
        parsed = reviewer._parse_state_from_marker_body(body)
        self.assertIsNotNone(parsed)
        self.assertEqual(parsed.resolved_fingerprints, ["valid-fp-1", "valid-fp-2"])
        self.assertEqual(parsed.open_fingerprints_this_gen, ["keep-me"])
        # Prove the downstream would have crashed on the poisoned
        # input if the coercion wasn't in place.
        set(parsed.resolved_fingerprints)  # must not raise
        set(parsed.open_fingerprints_this_gen)  # must not raise

    def test_history_drops_non_dict_elements(self) -> None:
        """History entries are mutated by `run_iar_post_llm`
        (`state.history[-1]["tokens_used"] = ...`) which blows up if
        an entry is a scalar. Coerce at parse time."""
        body: str = self._wrap({
            "version": 1, "generation": 1,
            "generation_range_hash": "abc",
            "round_in_generation": 1, "policy_applied": "iterative",
            "resolved_fingerprints": [], "open_fingerprints_this_gen": [],
            "history": [
                {"gen": 1, "range_hash": "x", "rounds_ran": 2},
                "not-a-dict",
                42,
                None,
                {"gen": 2, "range_hash": "y", "rounds_ran": 1},
            ],
            "reviewed_label_applied": False,
        })
        parsed = reviewer._parse_state_from_marker_body(body)
        self.assertIsNotNone(parsed)
        self.assertEqual(len(parsed.history), 2)
        self.assertEqual(parsed.history[0]["gen"], 1)
        self.assertEqual(parsed.history[1]["gen"], 2)


class MultipleBlocksTests(unittest.TestCase):
    """Defensive: if a marker somehow contains multiple state blocks,
    the LAST one wins. Callers only ever produce one block, so this is
    strictly a safety rail against future bugs."""

    def test_multiple_blocks_takes_last(self) -> None:
        state_a: reviewer.IterationState = _make_state(generation=1)
        state_b: reviewer.IterationState = _make_state(generation=7)
        embedded_a: str = reviewer.embed_iteration_state("m", state_a)
        # Manually append a second block (embed() would normally replace).
        state_b_json: str = json.dumps(
            asdict(state_b), indent=2, sort_keys=True
        )
        embedded_ab: str = (
            embedded_a + f"\n\n{reviewer.IAR_STATE_TAG_OPEN}\n"
            + state_b_json
            + f"\n{reviewer.IAR_STATE_TAG_CLOSE}\n"
        )
        parsed: reviewer.IterationState | None = (
            reviewer._parse_state_from_marker_body(embedded_ab)
        )
        self.assertIsNotNone(parsed)
        self.assertEqual(parsed.generation, 7)


class FetchLatestMarkerTests(unittest.TestCase):
    """`_fetch_latest_marker_body` reads via GraphQL, must skip
    minimized comments, non-marker comments, and pick the most recent
    by createdAt."""

    def _gql_return(
        self, comments: list[dict[str, Any]]
    ) -> dict[str, Any]:
        return {
            "repository": {
                "pullRequest": {
                    "comments": {"nodes": comments}
                }
            }
        }

    def test_prefers_visible_marker_with_state_over_minimized_with_state(
        self,
    ) -> None:
        """Tier 1 rule: when a non-minimized marker carries IAR state, it
        wins over any minimized marker (regardless of createdAt), because
        the visible marker is the authoritative live review.
        """
        state_block: str = (
            f"\n{reviewer.IAR_STATE_TAG_OPEN}\n"
            + '{"version": 1}\n'
            + f"{reviewer.IAR_STATE_TAG_CLOSE}\n"
        )
        newer_minimized: dict[str, Any] = {
            "body": reviewer.REVIEW_MARKER + " STALE" + state_block,
            "isMinimized": True,
            "createdAt": "2026-07-15T20:00:00Z",
        }
        older_live: dict[str, Any] = {
            "body": reviewer.REVIEW_MARKER + " LIVE" + state_block,
            "isMinimized": False,
            "createdAt": "2026-07-15T10:00:00Z",
        }
        with patch.object(
            reviewer, "gh_graphql",
            return_value=self._gql_return([newer_minimized, older_live]),
        ):
            got: str | None = reviewer._fetch_latest_marker_body(
                repo="acme/x", pr_number=1, token="tok"
            )
        self.assertIsNotNone(got)
        self.assertIn("LIVE", got)

    def test_falls_back_to_minimized_marker_with_state_when_no_visible(
        self,
    ) -> None:
        """Tier 2 rule: when NO visible marker carries IAR state (typical
        under `collapse-previous: true` — the last tracking comment got
        minimized between runs), fall back to the latest minimized marker
        that still carries a state block. This is the load-bearing fix
        for the IAR-vs-collapse ordering bug (without it, every default
        config sees `first_review` on every run and never dedups).
        """
        state_block: str = (
            f"\n{reviewer.IAR_STATE_TAG_OPEN}\n"
            + '{"version": 1}\n'
            + f"{reviewer.IAR_STATE_TAG_CLOSE}\n"
        )
        older_minimized: dict[str, Any] = {
            "body": reviewer.REVIEW_MARKER + " OLDER" + state_block,
            "isMinimized": True,
            "createdAt": "2026-07-15T10:00:00Z",
        }
        newer_minimized: dict[str, Any] = {
            "body": reviewer.REVIEW_MARKER + " NEWER" + state_block,
            "isMinimized": True,
            "createdAt": "2026-07-15T20:00:00Z",
        }
        # A visible marker without a state block (like the spinner) must
        # not steal precedence from a minimized marker with real state.
        visible_no_state: dict[str, Any] = {
            "body": reviewer.REVIEW_MARKER + " SPINNER",
            "isMinimized": False,
            "createdAt": "2026-07-15T21:00:00Z",
        }
        with patch.object(
            reviewer, "gh_graphql",
            return_value=self._gql_return(
                [older_minimized, newer_minimized, visible_no_state]
            ),
        ):
            got: str | None = reviewer._fetch_latest_marker_body(
                repo="acme/x", pr_number=1, token="tok"
            )
        self.assertIsNotNone(got)
        self.assertIn("NEWER", got)

    def test_author_filter_rejects_non_bot_forged_marker(self) -> None:
        """Round-10 F1 (critical) regression: `_fetch_latest_marker_body`
        MUST filter by comment author when a bot_login is supplied.
        Otherwise a PR participant can forge a marker with fabricated
        fingerprints, and — under `collapse-previous: true` — the real
        (minimized) bot marker loses to the attacker's (visible) forgery
        under tier-1 ordering."""
        state_block: str = (
            f"\n{reviewer.IAR_STATE_TAG_OPEN}\n"
            + '{"version": 1}\n'
            + reviewer.IAR_STATE_TAG_CLOSE
        )
        forged_marker: dict[str, Any] = {
            "body": (
                reviewer.REVIEW_MARKER + " FORGED"
                + reviewer.provider_marker("cursor")
                + state_block
            ),
            "isMinimized": False,   # visible
            "createdAt": "2026-07-15T20:00:00Z",  # newer
            "author": {"login": "attacker"},
        }
        genuine_marker: dict[str, Any] = {
            "body": (
                reviewer.REVIEW_MARKER + " GENUINE"
                + reviewer.provider_marker("cursor")
                + state_block
            ),
            "isMinimized": True,   # collapsed by prior run
            "createdAt": "2026-07-15T10:00:00Z",
            "author": {"login": "github-actions[bot]"},
        }
        with patch.object(
            reviewer, "gh_graphql",
            return_value=self._gql_return([forged_marker, genuine_marker]),
        ):
            got: str | None = reviewer._fetch_latest_marker_body(
                repo="acme/x", pr_number=1, token="tok",
                provider_id="cursor",
                bot_login="github-actions[bot]",
            )
        # Attacker's newer visible forgery must be dropped; the
        # genuine bot-authored (minimized) marker wins via tier 2.
        self.assertIsNotNone(got)
        self.assertIn("GENUINE", got)
        self.assertNotIn("FORGED", got)

    def test_author_filter_normalizes_bot_suffix(self) -> None:
        """`gh_get_authenticated_login` returns `github-actions[bot]` from
        the /user tier but plain `github-actions` from the marker-scan
        tier — the filter MUST accept both, same as
        `gh_collapse_previous_reviews`."""
        state_block: str = (
            f"\n{reviewer.IAR_STATE_TAG_OPEN}\n"
            + '{"version": 1}\n'
            + reviewer.IAR_STATE_TAG_CLOSE
        )
        node_plain: dict[str, Any] = {
            "body": reviewer.REVIEW_MARKER + " PLAIN" + state_block,
            "isMinimized": False,
            "createdAt": "2026-07-15T10:00:00Z",
            "author": {"login": "github-actions"},  # no [bot] suffix
        }
        with patch.object(
            reviewer, "gh_graphql",
            return_value=self._gql_return([node_plain]),
        ):
            got: str | None = reviewer._fetch_latest_marker_body(
                repo="acme/x", pr_number=1, token="tok",
                bot_login="github-actions[bot]",
            )
        self.assertIsNotNone(got)
        self.assertIn("PLAIN", got)

    def test_author_filter_disabled_when_no_bot_login(self) -> None:
        """Empty `bot_login` = filter off (back-compat for callers that
        can't resolve an identity; safe because the pre-round-10
        behaviour is the fallback and IAR still has the critical-
        always-surfaces safety rail)."""
        state_block: str = (
            f"\n{reviewer.IAR_STATE_TAG_OPEN}\n"
            + '{"version": 1}\n'
            + reviewer.IAR_STATE_TAG_CLOSE
        )
        node: dict[str, Any] = {
            "body": reviewer.REVIEW_MARKER + " ANY" + state_block,
            "isMinimized": False,
            "createdAt": "2026-07-15T10:00:00Z",
            "author": {"login": "some-random-user"},
        }
        with patch.object(
            reviewer, "gh_graphql",
            return_value=self._gql_return([node]),
        ):
            got: str | None = reviewer._fetch_latest_marker_body(
                repo="acme/x", pr_number=1, token="tok",
                # bot_login empty → filter off
            )
        self.assertIsNotNone(got)
        self.assertIn("ANY", got)

    def test_provider_isolation_reads_only_matching_provider_markers(
        self,
    ) -> None:
        """Round-9 F1 (critical) regression guard: in a multi-provider
        setup (e.g. self-review matrix running cursor + anthropic on
        the same PR) each provider's IAR state chain MUST stay isolated
        — otherwise providers cross-poison each other's fingerprint
        memory, generation hashes, and round counters. When
        `provider_id` is passed, `_fetch_latest_marker_body` skips
        markers tagged with a DIFFERENT provider and returns the
        newest marker tagged with the matching (or untagged legacy)
        provider marker."""
        state_block: str = (
            f"\n{reviewer.IAR_STATE_TAG_OPEN}\n"
            + '{"version": 1}\n'
            + reviewer.IAR_STATE_TAG_CLOSE
        )
        cursor_marker: dict[str, Any] = {
            "body": (
                reviewer.REVIEW_MARKER + " CURSOR-BODY\n"
                + reviewer.provider_marker("cursor")
                + state_block
            ),
            "isMinimized": False,
            "createdAt": "2026-07-15T10:00:00Z",
        }
        anthropic_marker: dict[str, Any] = {
            "body": (
                reviewer.REVIEW_MARKER + " ANTHROPIC-BODY\n"
                + reviewer.provider_marker("anthropic")
                + state_block
            ),
            "isMinimized": False,
            "createdAt": "2026-07-15T20:00:00Z",  # newer
        }
        # With provider_id="cursor", the newer anthropic marker MUST
        # be skipped even though it's newer.
        with patch.object(
            reviewer, "gh_graphql",
            return_value=self._gql_return([cursor_marker, anthropic_marker]),
        ):
            got: str | None = reviewer._fetch_latest_marker_body(
                repo="acme/x", pr_number=1, token="tok",
                provider_id="cursor",
            )
        self.assertIsNotNone(got)
        self.assertIn("CURSOR-BODY", got)
        self.assertNotIn("ANTHROPIC-BODY", got)
        # With provider_id="anthropic", the anthropic marker wins.
        with patch.object(
            reviewer, "gh_graphql",
            return_value=self._gql_return([cursor_marker, anthropic_marker]),
        ):
            got_anthropic: str | None = reviewer._fetch_latest_marker_body(
                repo="acme/x", pr_number=1, token="tok",
                provider_id="anthropic",
            )
        self.assertIsNotNone(got_anthropic)
        self.assertIn("ANTHROPIC-BODY", got_anthropic)

    def test_untagged_legacy_markers_match_every_provider(self) -> None:
        """Round-9 F1 back-compat: markers posted BEFORE the provider
        marker was introduced (or by callers that omit it) carry no
        `<!-- ai-pr-reviewer-provider: -->` tag. Those legacy markers
        MUST match every provider filter so consumers upgrading from
        an older version don't experience a one-off `first_review`
        classification on their first post-upgrade run."""
        state_block: str = (
            f"\n{reviewer.IAR_STATE_TAG_OPEN}\n"
            + '{"version": 1}\n'
            + reviewer.IAR_STATE_TAG_CLOSE
        )
        legacy_marker: dict[str, Any] = {
            "body": reviewer.REVIEW_MARKER + " LEGACY-NO-PROVIDER" + state_block,
            "isMinimized": False,
            "createdAt": "2026-07-15T10:00:00Z",
        }
        with patch.object(
            reviewer, "gh_graphql",
            return_value=self._gql_return([legacy_marker]),
        ):
            got: str | None = reviewer._fetch_latest_marker_body(
                repo="acme/x", pr_number=1, token="tok",
                provider_id="cursor",
            )
        self.assertIsNotNone(got)
        self.assertIn("LEGACY-NO-PROVIDER", got)

    def test_no_provider_id_matches_every_marker(self) -> None:
        """Round-9 F1: back-compat for callers that don't pass a
        provider_id (older test suites, non-IAR callers). When
        `provider_id` is empty, filtering is disabled and behaviour
        reverts to the pre-round-9 semantics — the caller gets
        whichever marker matches the three-tier priority regardless
        of provider tag."""
        state_block: str = (
            f"\n{reviewer.IAR_STATE_TAG_OPEN}\n"
            + '{"version": 1}\n'
            + reviewer.IAR_STATE_TAG_CLOSE
        )
        cursor_marker: dict[str, Any] = {
            "body": (
                reviewer.REVIEW_MARKER + " CURSOR"
                + reviewer.provider_marker("cursor")
                + state_block
            ),
            "isMinimized": False,
            "createdAt": "2026-07-15T20:00:00Z",  # newest
        }
        anthropic_marker: dict[str, Any] = {
            "body": (
                reviewer.REVIEW_MARKER + " ANTHROPIC"
                + reviewer.provider_marker("anthropic")
                + state_block
            ),
            "isMinimized": False,
            "createdAt": "2026-07-15T10:00:00Z",
        }
        with patch.object(
            reviewer, "gh_graphql",
            return_value=self._gql_return([cursor_marker, anthropic_marker]),
        ):
            got: str | None = reviewer._fetch_latest_marker_body(
                repo="acme/x", pr_number=1, token="tok",
                # No provider_id — filter disabled.
            )
        # Newest wins (cursor).
        self.assertIsNotNone(got)
        self.assertIn("CURSOR", got)

    def test_falls_back_to_any_marker_when_none_carry_state(self) -> None:
        """Tier 3 rule: when no marker carries an IAR state block at all
        (fresh PR, or state predates IAR), return the newest marker
        anyway so back-compat callers still see something to work with.
        `_parse_state_from_marker_body` will return None for the missing
        state block, which callers interpret as `first_review`.
        """
        older_stateless: dict[str, Any] = {
            "body": reviewer.REVIEW_MARKER + " OLD",
            "isMinimized": False,
            "createdAt": "2026-07-15T10:00:00Z",
        }
        newer_stateless: dict[str, Any] = {
            "body": reviewer.REVIEW_MARKER + " NEW",
            "isMinimized": False,
            "createdAt": "2026-07-15T20:00:00Z",
        }
        with patch.object(
            reviewer, "gh_graphql",
            return_value=self._gql_return([older_stateless, newer_stateless]),
        ):
            got: str | None = reviewer._fetch_latest_marker_body(
                repo="acme/x", pr_number=1, token="tok"
            )
        self.assertIsNotNone(got)
        self.assertIn("NEW", got)

    def test_takes_most_recent_by_created_at(self) -> None:
        older: dict[str, Any] = {
            "body": reviewer.REVIEW_MARKER + " OLD",
            "isMinimized": False,
            "createdAt": "2026-07-15T10:00:00Z",
        }
        newer: dict[str, Any] = {
            "body": reviewer.REVIEW_MARKER + " NEW",
            "isMinimized": False,
            "createdAt": "2026-07-15T20:00:00Z",
        }
        with patch.object(
            reviewer, "gh_graphql",
            return_value=self._gql_return([older, newer]),
        ):
            got: str | None = reviewer._fetch_latest_marker_body(
                repo="acme/x", pr_number=1, token="tok"
            )
        self.assertIsNotNone(got)
        self.assertIn("NEW", got)

    def test_skips_comments_without_marker(self) -> None:
        with patch.object(
            reviewer, "gh_graphql",
            return_value=self._gql_return([
                {
                    "body": "just some other bot comment",
                    "isMinimized": False,
                    "createdAt": "2026-07-15T20:00:00Z",
                }
            ]),
        ):
            self.assertIsNone(
                reviewer._fetch_latest_marker_body(
                    repo="acme/x", pr_number=1, token="tok"
                )
            )

    def test_graphql_failure_returns_none(self) -> None:
        def _boom(*args: Any, **kwargs: Any) -> Any:
            raise RuntimeError("simulated 5xx")

        with patch.object(reviewer, "gh_graphql", side_effect=_boom):
            self.assertIsNone(
                reviewer._fetch_latest_marker_body(
                    repo="acme/x", pr_number=1, token="tok"
                )
            )

    def test_empty_repo_input_returns_none(self) -> None:
        self.assertIsNone(
            reviewer._fetch_latest_marker_body(
                repo="", pr_number=1, token="tok"
            )
        )
        self.assertIsNone(
            reviewer._fetch_latest_marker_body(
                repo="malformed", pr_number=1, token="tok"
            )
        )

    def test_negative_pr_number_returns_none(self) -> None:
        self.assertIsNone(
            reviewer._fetch_latest_marker_body(
                repo="acme/x", pr_number=0, token="tok"
            )
        )


class ReadPriorIterationStateTests(unittest.TestCase):
    """End-to-end: read_prior_iteration_state chains
    _fetch_latest_marker_body + _parse_state_from_marker_body."""

    def test_returns_state_when_marker_and_block_present(self) -> None:
        state: reviewer.IterationState = _make_state()
        marker: str = reviewer.embed_iteration_state(
            reviewer.REVIEW_MARKER + "\n\nWorking on it.", state
        )
        with patch.object(
            reviewer, "_fetch_latest_marker_body", return_value=marker
        ):
            got: reviewer.IterationState | None = (
                reviewer.read_prior_iteration_state(
                    repo="a/b", pr_number=1, token="t"
                )
            )
        self.assertEqual(got, state)

    def test_returns_none_when_no_marker(self) -> None:
        with patch.object(
            reviewer, "_fetch_latest_marker_body", return_value=None
        ):
            self.assertIsNone(
                reviewer.read_prior_iteration_state(
                    repo="a/b", pr_number=1, token="t"
                )
            )

    def test_returns_none_when_marker_has_no_state_block(self) -> None:
        with patch.object(
            reviewer, "_fetch_latest_marker_body",
            return_value=reviewer.REVIEW_MARKER + "\n\nFirst review.",
        ):
            self.assertIsNone(
                reviewer.read_prior_iteration_state(
                    repo="a/b", pr_number=1, token="t"
                )
            )


class NewIterationStateHelperTests(unittest.TestCase):
    """`new_iteration_state` is the constructor for first-round runs."""

    def test_defaults_produce_valid_schema(self) -> None:
        state: reviewer.IterationState = reviewer.new_iteration_state()
        self.assertEqual(state.version, reviewer.IAR_STATE_SCHEMA_VERSION)
        self.assertEqual(state.generation, 1)
        self.assertEqual(state.round_in_generation, 1)
        self.assertEqual(state.resolved_fingerprints, [])
        self.assertEqual(state.open_fingerprints_this_gen, [])
        self.assertEqual(state.history, [])

    def test_roundtrip_with_default_construction(self) -> None:
        state: reviewer.IterationState = reviewer.new_iteration_state(
            generation_range_hash="range-hash-abc"
        )
        embedded: str = reviewer.embed_iteration_state("marker", state)
        parsed: reviewer.IterationState | None = (
            reviewer._parse_state_from_marker_body(embedded)
        )
        self.assertEqual(parsed, state)


if __name__ == "__main__":
    unittest.main()
