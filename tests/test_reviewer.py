#!/usr/bin/env python3
"""Unit tests for `scripts/reviewer.py`.

Stdlib `unittest` only — no third-party test runner, no install step. Run with:

    python3 -m unittest discover -s tests -v

The runtime under test is stdlib-only by design (see AGENTS.md Rule #2); the
tests honour the same constraint so `python3 -m unittest` works on a vanilla
runner with nothing pre-installed.

Tests cover the pure, deterministic surface of the reviewer: parsing,
redaction, truncation, severity/strictness logic, path sandboxing, the tool
handlers, the tracking-comment renderers, output writing, provider
construction, and the conversation-pruning invariant of the agentic loop.
Network/GitHub-API paths are not exercised here (they're covered by the
dogfooding self-review workflow).
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
# Import the module under test from scripts/reviewer.py without requiring it to
# be installed or on PYTHONPATH.
# ---------------------------------------------------------------------------
_ROOT: Path = Path(__file__).resolve().parent.parent
_SPEC = importlib.util.spec_from_file_location(
    "reviewer", _ROOT / "scripts" / "reviewer.py"
)
assert _SPEC is not None and _SPEC.loader is not None
reviewer = importlib.util.module_from_spec(_SPEC)
# Register before exec so `@dataclass` (which looks the module up in
# sys.modules via cls.__module__) resolves correctly on Python 3.12+.
sys.modules["reviewer"] = reviewer
_SPEC.loader.exec_module(reviewer)


class ParseBoolTests(unittest.TestCase):
    def test_truthy_values(self) -> None:
        for raw in ("1", "true", "TRUE", "Yes", "on", " true "):
            self.assertTrue(reviewer.parse_bool(raw), raw)

    def test_falsy_values(self) -> None:
        for raw in ("0", "false", "no", "off", "nope"):
            self.assertFalse(reviewer.parse_bool(raw), raw)

    def test_empty_uses_default(self) -> None:
        self.assertTrue(reviewer.parse_bool("", default=True))
        self.assertFalse(reviewer.parse_bool("", default=False))


class RedactForLogTests(unittest.TestCase):
    def test_masks_sensitive_keys(self) -> None:
        masked = reviewer.redact_for_log(
            {"api_key": "sk-secret", "token": "ghp_x", "path": "src/a.py"}
        )
        self.assertEqual(masked["api_key"], "***")
        self.assertEqual(masked["token"], "***")
        self.assertEqual(masked["path"], "src/a.py")

    def test_case_insensitive_substring(self) -> None:
        masked = reviewer.redact_for_log({"AUTH_Header": "v", "PassWord": "v"})
        self.assertEqual(masked["AUTH_Header"], "***")
        self.assertEqual(masked["PassWord"], "***")


class TruncateForToolTests(unittest.TestCase):
    def test_short_text_unchanged(self) -> None:
        self.assertEqual(reviewer.truncate_for_tool("hi", label="grep"), "hi")

    def test_long_text_truncated_with_notice(self) -> None:
        big = "x" * (reviewer.MAX_TOOL_OUTPUT_BYTES + 100)
        out = reviewer.truncate_for_tool(big, label="grep")
        self.assertLess(len(out.encode("utf-8")), len(big.encode("utf-8")))
        self.assertIn("output truncated", out)
        self.assertIn("grep", out)


class OverallSeverityTests(unittest.TestCase):
    def test_empty_is_none(self) -> None:
        self.assertEqual(reviewer.overall_severity([]), reviewer.SEVERITY_NONE)

    def test_returns_highest(self) -> None:
        self.assertEqual(
            reviewer.overall_severity(["info", "critical", "warning"]),
            reviewer.SEVERITY_CRITICAL,
        )
        self.assertEqual(
            reviewer.overall_severity(["info", "warning"]),
            reviewer.SEVERITY_WARNING,
        )


class CountLabelEventsTests(unittest.TestCase):
    """`count_label_events` paginates and filters correctly."""

    def _monkeypatch_gh_request(
        self, responses: list[list[dict[str, Any]]]
    ) -> None:
        """Replace `gh_request` with a fake that returns queued responses.

        Each call pops the next response off the queue. When the queue
        is exhausted, returns an empty list (simulates "no more pages").
        """
        self._responses = responses
        self._call_count = 0

        def fake_gh_request(method: str, path: str, **kwargs: Any) -> Any:
            self._call_count += 1
            if not self._responses:
                return []
            return self._responses.pop(0)

        self._orig = reviewer.gh_request
        reviewer.gh_request = fake_gh_request  # type: ignore[assignment]

    def _restore(self) -> None:
        reviewer.gh_request = self._orig  # type: ignore[assignment]

    def test_counts_matching_labeled_events(self) -> None:
        self._monkeypatch_gh_request(
            [
                [
                    {"event": "labeled", "label": {"name": "ready"}},
                    {"event": "labeled", "label": {"name": "other"}},
                    {"event": "closed"},
                    {"event": "labeled", "label": {"name": "ready"}},
                ]
            ]
        )
        try:
            n = reviewer.count_label_events(
                token="t", repo="o/r", pr_number=1, label="ready"
            )
            self.assertEqual(n, 2)
        finally:
            self._restore()

    def test_empty_label_returns_zero_without_api_call(self) -> None:
        self._monkeypatch_gh_request([])
        try:
            n = reviewer.count_label_events(
                token="t", repo="o/r", pr_number=1, label=""
            )
            self.assertEqual(n, 0)
            self.assertEqual(self._call_count, 0)
        finally:
            self._restore()

    def test_paginates_until_less_than_full_page(self) -> None:
        # First page: 100 items → paginate. Second page: 2 items → stop.
        page1 = [
            {"event": "labeled", "label": {"name": "ready"}}
        ] * 100
        page2 = [
            {"event": "labeled", "label": {"name": "ready"}},
            {"event": "closed"},
        ]
        self._monkeypatch_gh_request([page1, page2])
        try:
            n = reviewer.count_label_events(
                token="t", repo="o/r", pr_number=1, label="ready"
            )
            self.assertEqual(n, 101)
            self.assertEqual(self._call_count, 2)
        finally:
            self._restore()

    def test_logs_warning_when_pagination_cap_hit(self) -> None:
        """Regression for PR #9 self-review comment #5: on long-lived,
        high-chatter PRs the 20-page cap silently undercounts. The cap
        stays (cost control) but must announce itself so operators see
        why `label-once` is stuck."""
        full_page = [{"event": "labeled", "label": {"name": "ready"}}] * 100
        # 21 identical full pages → the loop hits the cap on page 21
        # (after processing page 20) and exits with the warning.
        self._monkeypatch_gh_request([full_page] * 21)

        captured: list[str] = []

        def fake_log(msg: str, *args: Any, **kwargs: Any) -> None:
            captured.append(msg)

        orig_log = reviewer.log
        reviewer.log = fake_log  # type: ignore[assignment]
        try:
            n = reviewer.count_label_events(
                token="t", repo="o/r", pr_number=1, label="ready"
            )
            self.assertEqual(n, 2000, "20 pages × 100 events per page")
            warnings = [m for m in captured if m.startswith("WARNING:")]
            self.assertEqual(
                len(warnings), 1, f"expected one WARNING log, got {captured}"
            )
            self.assertIn("pagination cap", warnings[0])
            self.assertIn("label-added-only", warnings[0])
        finally:
            reviewer.log = orig_log  # type: ignore[assignment]
            self._restore()


class GhGetAuthenticatedLoginFallbackTests(unittest.TestCase):
    """Regression for the v1.2 silent-403 bug that broke `collapse-previous`
    for every consumer using the recommended `${{ secrets.GITHUB_TOKEN }}`
    pattern. The naive `GET /user` call fails 403 on installation tokens;
    `gh_get_authenticated_login` now has a 4-tier fallback chain.

    See docs/SECURITY.md and CHANGELOG's [1.2.0] "Fixed" section.
    """

    def _install_fake_gh_request(
        self, handler: Any
    ) -> None:
        self._orig_gh_request = reviewer.gh_request
        reviewer.gh_request = handler  # type: ignore[assignment]

    def _restore(self) -> None:
        reviewer.gh_request = self._orig_gh_request  # type: ignore[assignment]

    def test_tier1_user_endpoint_returns_login(self) -> None:
        """PAT / OAuth token case — `/user` succeeds → returns its login."""
        calls: list[str] = []

        def fake(method: str, path: str, **_: Any) -> Any:
            calls.append(path)
            if path == "/user":
                return {"login": "alice"}
            raise AssertionError(f"unexpected call: {path}")

        self._install_fake_gh_request(fake)
        try:
            login = reviewer.gh_get_authenticated_login("tok")
            self.assertEqual(login, "alice")
            self.assertEqual(calls, ["/user"], "no fallback should be tried")
        finally:
            self._restore()

    def test_tier2_app_endpoint_wraps_slug_with_bot_suffix(self) -> None:
        """GitHub App installation token — `/user` fails, `/app` returns
        the app slug; `[bot]` suffix matches how GitHub renders the login
        in comment.user.login payloads."""

        def fake(method: str, path: str, **_: Any) -> Any:
            if path == "/user":
                raise RuntimeError("HTTP Error 403: Forbidden")
            if path == "/app":
                return {"slug": "my-cool-app"}
            raise AssertionError(f"unexpected call: {path}")

        self._install_fake_gh_request(fake)
        try:
            login = reviewer.gh_get_authenticated_login("tok")
            self.assertEqual(login, "my-cool-app[bot]")
        finally:
            self._restore()

    def test_tier3_marker_scan_finds_prior_bot_author(self) -> None:
        """Workflow `GITHUB_TOKEN` case: both `/user` and `/app` refuse.
        The marker-scan tier reads recent PR comments, finds one with the
        canonical marker, and returns THAT comment's author."""
        marker = reviewer.REVIEW_MARKER

        def fake(method: str, path: str, **_: Any) -> Any:
            if path in ("/user", "/app"):
                raise RuntimeError("HTTP Error 403: Forbidden")
            if path.startswith("/repos/o/r/issues/9/comments"):
                return [
                    {
                        "body": "irrelevant human comment",
                        "user": {"login": "human-user"},
                    },
                    {
                        "body": f"prior review\n{marker}\nsome state",
                        "user": {"login": "github-actions[bot]"},
                    },
                    {"body": "later human comment", "user": {"login": "alice"}},
                ]
            raise AssertionError(f"unexpected call: {path}")

        self._install_fake_gh_request(fake)
        try:
            login = reviewer.gh_get_authenticated_login(
                "tok", repo="o/r", pr_number=9
            )
            self.assertEqual(login, "github-actions[bot]")
        finally:
            self._restore()

    def test_tier3_iterates_in_reverse_to_prefer_most_recent(self) -> None:
        """When multiple prior markers exist (rare — the collapse mutation
        cleans them up), the LAST one wins because it's the newest run."""
        marker = reviewer.REVIEW_MARKER

        def fake(method: str, path: str, **_: Any) -> Any:
            if path in ("/user", "/app"):
                raise RuntimeError("HTTP Error 403")
            return [
                {"body": f"old\n{marker}", "user": {"login": "old-bot[bot]"}},
                {"body": f"new\n{marker}", "user": {"login": "new-bot[bot]"}},
            ]

        self._install_fake_gh_request(fake)
        try:
            login = reviewer.gh_get_authenticated_login(
                "tok", repo="o/r", pr_number=1
            )
            self.assertEqual(login, "new-bot[bot]")
        finally:
            self._restore()

    def test_tier3_skipped_when_repo_or_pr_missing(self) -> None:
        """Callers not in the main() context (e.g. one-off helpers) don't
        pass repo/pr_number; tier 3 is skipped and tier 4's default fires."""

        def fake(method: str, path: str, **_: Any) -> Any:
            if path in ("/user", "/app"):
                raise RuntimeError("HTTP Error 403")
            raise AssertionError(f"tier3 should be skipped, got: {path}")

        self._install_fake_gh_request(fake)
        try:
            login = reviewer.gh_get_authenticated_login("tok")
            self.assertEqual(login, reviewer.DEFAULT_WORKFLOW_BOT_LOGIN)
        finally:
            self._restore()

    def test_tier4_falls_back_to_default_workflow_bot(self) -> None:
        """No marker → default to `github-actions[bot]`, the login used by
        the built-in `GITHUB_TOKEN`. Downstream steps still work: even if
        this guess is wrong, `gh_collapse_previous_reviews` just filters
        no nodes and logs it, staying non-fatal."""

        def fake(method: str, path: str, **_: Any) -> Any:
            if path in ("/user", "/app"):
                raise RuntimeError("HTTP Error 403")
            if path.startswith("/repos/"):
                return []
            raise AssertionError(f"unexpected call: {path}")

        self._install_fake_gh_request(fake)
        try:
            login = reviewer.gh_get_authenticated_login(
                "tok", repo="o/r", pr_number=1
            )
            self.assertEqual(login, "github-actions[bot]")
            self.assertEqual(login, reviewer.DEFAULT_WORKFLOW_BOT_LOGIN)
        finally:
            self._restore()

    def test_empty_login_from_tier1_falls_through(self) -> None:
        """Defensive: some tokens return `{"login": ""}` for `/user`. Don't
        treat that as a valid login; try the next tier instead."""

        def fake(method: str, path: str, **_: Any) -> Any:
            if path == "/user":
                return {"login": ""}
            if path == "/app":
                return {"slug": "fallback-app"}
            raise AssertionError(f"unexpected call: {path}")

        self._install_fake_gh_request(fake)
        try:
            login = reviewer.gh_get_authenticated_login("tok")
            self.assertEqual(login, "fallback-app[bot]")
        finally:
            self._restore()


class GhCollapsePreviousReviewsTests(unittest.TestCase):
    """Regression for the login-shape mismatch that silently broke
    `collapse-previous` in every self-review run on this repo — even
    after the 4-tier fallback for `gh_get_authenticated_login` landed.

    REST returns `"github-actions[bot]"` but GraphQL's `.author.login`
    on the same Bot node returns `"github-actions"` (no suffix). The
    naive equality check filtered every bot node out, resulting in
    "Collapsed 0/N previous bot artefact(s)" on every run. The fix
    accepts both shapes for the comparison.
    """

    def _install_fake_gh_graphql(self, responses: list[Any]) -> list[dict]:
        """Return a call-log list; each `gh_graphql` invocation appends
        `{"query": ..., "variables": ...}` and pops the next response."""
        calls: list[dict] = []

        def fake(query: str, variables: dict, *, token: str) -> Any:
            calls.append({"query": query, "variables": variables})
            if not responses:
                return {}
            return responses.pop(0)

        self._orig_gh_graphql = reviewer.gh_graphql
        reviewer.gh_graphql = fake  # type: ignore[assignment]
        return calls

    def _restore(self) -> None:
        reviewer.gh_graphql = self._orig_gh_graphql  # type: ignore[assignment]

    def test_matches_graphql_bot_login_without_suffix(self) -> None:
        """Bot comments arriving from GraphQL as `"github-actions"`
        (no `[bot]`) must be recognized when caller passes the REST-
        shaped `"github-actions[bot]"`. Regression for PR #9."""
        query_payload = {
            "repository": {
                "pullRequest": {
                    "comments": {
                        "nodes": [
                            {
                                "id": "IC_kw_1",
                                "isMinimized": False,
                                "author": {"login": "github-actions"},
                            },
                            {
                                "id": "IC_kw_2",
                                "isMinimized": False,
                                "author": {"login": "xergioalex"},
                            },
                        ]
                    },
                    "reviews": {
                        "nodes": [
                            {
                                "id": "PRR_kw_1",
                                "isMinimized": False,
                                "author": {"login": "github-actions"},
                                "comments": {
                                    "nodes": [
                                        {"id": "PRRC_kw_1", "isMinimized": False}
                                    ]
                                },
                            }
                        ]
                    },
                }
            }
        }
        # First call: the LIST query. Then one mutation per target
        # (comment + review + inline) → 3 mutations. Each returns {}.
        calls = self._install_fake_gh_graphql(
            [query_payload, {}, {}, {}]
        )
        try:
            n = reviewer.gh_collapse_previous_reviews(
                token="tok",
                repo="o/r",
                pr_number=9,
                bot_login="github-actions[bot]",  # REST-shape login
            )
            self.assertEqual(
                n,
                3,
                "must collapse the 1 issue comment + 1 review + 1 inline "
                "comment, all authored by `github-actions` (Bot).",
            )
            mutation_ids = [
                c["variables"].get("id")
                for c in calls[1:]  # skip the LIST query
            ]
            self.assertEqual(sorted(mutation_ids), sorted(["IC_kw_1", "PRR_kw_1", "PRRC_kw_1"]))
        finally:
            self._restore()

    def test_matches_bot_login_with_suffix_too(self) -> None:
        """Sanity: when caller passes the raw `github-actions[bot]`
        AND GraphQL also returns it that way (unlikely but not
        impossible for future API changes), still matches."""
        query_payload = {
            "repository": {
                "pullRequest": {
                    "comments": {
                        "nodes": [
                            {
                                "id": "IC_kw_1",
                                "isMinimized": False,
                                "author": {"login": "github-actions[bot]"},
                            }
                        ]
                    },
                    "reviews": {"nodes": []},
                }
            }
        }
        self._install_fake_gh_graphql([query_payload, {}])
        try:
            n = reviewer.gh_collapse_previous_reviews(
                token="tok",
                repo="o/r",
                pr_number=9,
                bot_login="github-actions[bot]",
            )
            self.assertEqual(n, 1)
        finally:
            self._restore()

    def test_skips_already_minimized_nodes(self) -> None:
        """`isMinimized: True` nodes are excluded — no wasted mutations."""
        query_payload = {
            "repository": {
                "pullRequest": {
                    "comments": {
                        "nodes": [
                            {
                                "id": "IC_kw_1",
                                "isMinimized": True,
                                "author": {"login": "github-actions"},
                            }
                        ]
                    },
                    "reviews": {"nodes": []},
                }
            }
        }
        calls = self._install_fake_gh_graphql([query_payload])
        try:
            n = reviewer.gh_collapse_previous_reviews(
                token="tok",
                repo="o/r",
                pr_number=9,
                bot_login="github-actions[bot]",
            )
            self.assertEqual(n, 0)
            self.assertEqual(len(calls), 1, "only the LIST query, no mutations")
        finally:
            self._restore()

    def test_ignores_other_bots(self) -> None:
        """Only OUR bot's comments get collapsed — a dependabot or
        renovate bot's comments must be left alone."""
        query_payload = {
            "repository": {
                "pullRequest": {
                    "comments": {
                        "nodes": [
                            {
                                "id": "IC_kw_1",
                                "isMinimized": False,
                                "author": {"login": "dependabot"},
                            },
                            {
                                "id": "IC_kw_2",
                                "isMinimized": False,
                                "author": {"login": "github-actions"},
                            },
                        ]
                    },
                    "reviews": {"nodes": []},
                }
            }
        }
        calls = self._install_fake_gh_graphql([query_payload, {}])
        try:
            n = reviewer.gh_collapse_previous_reviews(
                token="tok",
                repo="o/r",
                pr_number=9,
                bot_login="github-actions[bot]",
            )
            self.assertEqual(n, 1, "only our bot's comment collapsed")
            self.assertEqual(calls[1]["variables"]["id"], "IC_kw_2")
        finally:
            self._restore()

    def test_provider_scoping_collapses_only_matching_provider(self) -> None:
        """With `provider_marker_text` set, only artefacts carrying that
        provider's marker are collapsed — so concurrent multi-provider
        reviews (one shared bot author) don't collapse each other."""
        anthropic_marker = reviewer.provider_marker("anthropic")
        codex_marker = reviewer.provider_marker("codex")
        query_payload = {
            "repository": {
                "pullRequest": {
                    "comments": {
                        "nodes": [
                            {
                                "id": "IC_anthropic",
                                "isMinimized": False,
                                "body": f"{reviewer.REVIEW_MARKER}\n{anthropic_marker}\ndone",
                                "author": {"login": "github-actions"},
                            },
                            {
                                "id": "IC_codex",
                                "isMinimized": False,
                                "body": f"{reviewer.REVIEW_MARKER}\n{codex_marker}\ndone",
                                "author": {"login": "github-actions"},
                            },
                        ]
                    },
                    "reviews": {
                        "nodes": [
                            {
                                "id": "PRR_anthropic",
                                "isMinimized": False,
                                "body": f"{anthropic_marker}\n\nsummary",
                                "author": {"login": "github-actions"},
                                "comments": {"nodes": []},
                            },
                            {
                                "id": "PRR_codex",
                                "isMinimized": False,
                                "body": f"{codex_marker}\n\nsummary",
                                "author": {"login": "github-actions"},
                                "comments": {"nodes": []},
                            },
                        ]
                    },
                }
            }
        }
        calls = self._install_fake_gh_graphql([query_payload, {}, {}])
        try:
            n = reviewer.gh_collapse_previous_reviews(
                token="tok",
                repo="o/r",
                pr_number=9,
                bot_login="github-actions[bot]",
                provider_marker_text=codex_marker,
            )
            self.assertEqual(n, 2, "only the codex comment + codex review")
            ids = sorted(c["variables"].get("id") for c in calls[1:])
            self.assertEqual(ids, sorted(["IC_codex", "PRR_codex"]))
            self.assertNotIn("IC_anthropic", ids)
            self.assertNotIn("PRR_anthropic", ids)
        finally:
            self._restore()

    def test_provider_scoping_skips_unmarked_artefacts(self) -> None:
        """In scoped mode an unmarked bot comment (e.g. from an unrelated
        github-actions workflow, or a pre-upgrade review) is left alone."""
        codex_marker = reviewer.provider_marker("codex")
        query_payload = {
            "repository": {
                "pullRequest": {
                    "comments": {
                        "nodes": [
                            {
                                "id": "IC_unrelated",
                                "isMinimized": False,
                                "body": "coverage report: 91%",
                                "author": {"login": "github-actions"},
                            }
                        ]
                    },
                    "reviews": {"nodes": []},
                }
            }
        }
        calls = self._install_fake_gh_graphql([query_payload])
        try:
            n = reviewer.gh_collapse_previous_reviews(
                token="tok",
                repo="o/r",
                pr_number=9,
                bot_login="github-actions[bot]",
                provider_marker_text=codex_marker,
            )
            self.assertEqual(n, 0)
            self.assertEqual(len(calls), 1, "no mutations — nothing in scope")
        finally:
            self._restore()


class GhPatchPrBodySignatureTests(unittest.TestCase):
    """Regression: `gh_patch_pr_body` uses positional method+path (matches
    `gh_request`'s actual signature). If someone reverts to keyword args,
    this will catch it."""

    def test_calls_patch_with_new_body(self) -> None:
        captured: dict[str, Any] = {}

        def fake_gh_request(method: str, path: str, **kwargs: Any) -> Any:
            captured["method"] = method
            captured["path"] = path
            captured["body"] = kwargs.get("body")
            return {}

        orig = reviewer.gh_request
        reviewer.gh_request = fake_gh_request  # type: ignore[assignment]
        try:
            reviewer.gh_patch_pr_body(
                token="t", repo="o/r", pr_number=42, new_body="hello"
            )
        finally:
            reviewer.gh_request = orig  # type: ignore[assignment]
        self.assertEqual(captured["method"], "PATCH")
        self.assertIn("/repos/o/r/pulls/42", captured["path"])
        self.assertEqual(captured["body"], {"body": "hello"})


class SetPrDescriptionToolSchemaTests(unittest.TestCase):
    """Sanity checks on the exposed schema for the autocomplete surface."""

    def test_tool_description_forbids_secrets(self) -> None:
        schema = reviewer.tools_schema(
            10, allow_set_pr_description=True
        )
        tool = next(
            t for t in schema if t["name"] == "set_pr_description"
        )
        # The description warns the model against leaking secrets — the
        # single most important prompt-injection mitigation for this
        # write-back path.
        desc: str = tool["description"].lower()
        self.assertTrue(
            "secret" in desc or "credential" in desc or "token" in desc,
            "set_pr_description schema must warn against secret leakage",
        )

    def test_tool_description_mentions_marker(self) -> None:
        schema = reviewer.tools_schema(
            10, allow_set_pr_description=True
        )
        tool = next(
            t for t in schema if t["name"] == "set_pr_description"
        )
        self.assertIn(
            "ai-pr-reviewer-description-autocompleted",
            tool["description"],
        )


class ResolveTriggerActionTests(unittest.TestCase):
    """Matrix coverage across the four trigger modes."""

    def test_always_runs_regardless_of_state(self) -> None:
        d = reviewer.resolve_trigger_action(
            trigger_mode=reviewer.TRIGGER_ALWAYS,
            event_action="opened",
            label_gate="",
            current_labels=[],
            label_toggle_generation=0,
            last_reviewed_generation=0,
        )
        self.assertTrue(d.should_run)

    def test_missing_label_gate_falls_back_to_always(self) -> None:
        d = reviewer.resolve_trigger_action(
            trigger_mode=reviewer.TRIGGER_LABEL_ONCE,
            event_action="opened",
            label_gate="",
            current_labels=[],
            label_toggle_generation=0,
            last_reviewed_generation=0,
        )
        self.assertTrue(d.should_run)
        self.assertIn("no label-gate", d.reason)

    def test_label_required_blocks_when_missing(self) -> None:
        d = reviewer.resolve_trigger_action(
            trigger_mode=reviewer.TRIGGER_LABEL_REQUIRED,
            event_action="opened",
            label_gate="ready",
            current_labels=["bug"],
            label_toggle_generation=0,
            last_reviewed_generation=0,
        )
        self.assertFalse(d.should_run)
        self.assertIn("not present", d.reason)

    def test_label_required_runs_when_present(self) -> None:
        d = reviewer.resolve_trigger_action(
            trigger_mode=reviewer.TRIGGER_LABEL_REQUIRED,
            event_action="synchronize",
            label_gate="ready",
            current_labels=["ready", "bug"],
            label_toggle_generation=1,
            last_reviewed_generation=0,
        )
        self.assertTrue(d.should_run)

    def test_label_match_is_case_insensitive(self) -> None:
        """`ready`/`Ready`/`READY` all satisfy `label-gate: ready` — label
        matching must lowercase both sides."""
        for gate, present in (
            ("ready", "Ready"),
            ("Ready", "ready"),
            ("ready", "READY"),
            ("READY", "  ready  "),
        ):
            d = reviewer.resolve_trigger_action(
                trigger_mode=reviewer.TRIGGER_LABEL_REQUIRED,
                event_action="synchronize",
                label_gate=gate,
                current_labels=[present, "bug"],
                label_toggle_generation=1,
                last_reviewed_generation=0,
            )
            self.assertTrue(
                d.should_run, f"gate={gate!r} vs label={present!r} should match"
            )

    def test_label_added_only_matches_event_label_case_insensitively(
        self,
    ) -> None:
        d = reviewer.resolve_trigger_action(
            trigger_mode=reviewer.TRIGGER_LABEL_ADDED_ONLY,
            event_action="labeled",
            label_gate="ready",
            current_labels=["Ready"],
            label_toggle_generation=1,
            last_reviewed_generation=0,
            event_label="READY",
        )
        self.assertTrue(d.should_run)

    def test_label_once_runs_on_first_generation(self) -> None:
        d = reviewer.resolve_trigger_action(
            trigger_mode=reviewer.TRIGGER_LABEL_ONCE,
            event_action="labeled",
            label_gate="ready",
            current_labels=["ready"],
            label_toggle_generation=1,
            last_reviewed_generation=0,
        )
        self.assertTrue(d.should_run)
        self.assertIn("new label generation", d.reason)

    def test_label_once_blocks_on_stale_generation(self) -> None:
        d = reviewer.resolve_trigger_action(
            trigger_mode=reviewer.TRIGGER_LABEL_ONCE,
            event_action="synchronize",
            label_gate="ready",
            current_labels=["ready"],
            label_toggle_generation=1,
            last_reviewed_generation=1,
        )
        self.assertFalse(d.should_run)
        self.assertIn("already reviewed", d.reason)

    def test_label_once_runs_after_toggle(self) -> None:
        d = reviewer.resolve_trigger_action(
            trigger_mode=reviewer.TRIGGER_LABEL_ONCE,
            event_action="labeled",
            label_gate="ready",
            current_labels=["ready"],
            label_toggle_generation=2,
            last_reviewed_generation=1,
        )
        self.assertTrue(d.should_run)

    def test_label_once_runs_when_count_zero_but_label_present(self) -> None:
        """Regression for PR #9 self-review comment #4: if the timeline
        API failed (or the PR has no timeline entries yet) and the label
        IS on the PR, we must NOT skip on `0 <= 0`. Better to run and
        deliver a review than to skip silently."""
        d = reviewer.resolve_trigger_action(
            trigger_mode=reviewer.TRIGGER_LABEL_ONCE,
            event_action="labeled",
            label_gate="ready",
            current_labels=["ready"],
            label_toggle_generation=0,
            last_reviewed_generation=0,
        )
        self.assertTrue(
            d.should_run,
            "label-once with generation=0 and gate label present must "
            "still run — 0<=0 is not a stale-generation skip signal.",
        )

    def test_label_added_only_ignores_non_labeled_events(self) -> None:
        d = reviewer.resolve_trigger_action(
            trigger_mode=reviewer.TRIGGER_LABEL_ADDED_ONLY,
            event_action="synchronize",
            label_gate="ready",
            current_labels=["ready"],
            label_toggle_generation=1,
            last_reviewed_generation=0,
        )
        self.assertFalse(d.should_run)
        self.assertIn("is not 'labeled'", d.reason)

    def test_label_added_only_fires_on_labeled(self) -> None:
        d = reviewer.resolve_trigger_action(
            trigger_mode=reviewer.TRIGGER_LABEL_ADDED_ONLY,
            event_action="labeled",
            label_gate="ready",
            current_labels=["ready"],
            label_toggle_generation=1,
            last_reviewed_generation=0,
        )
        self.assertTrue(d.should_run)

    def test_label_added_only_fires_when_event_label_matches_gate(self) -> None:
        """The labeled event carries `ready` → run."""
        d = reviewer.resolve_trigger_action(
            trigger_mode=reviewer.TRIGGER_LABEL_ADDED_ONLY,
            event_action="labeled",
            event_label="ready",
            label_gate="ready",
            current_labels=["ready"],
            label_toggle_generation=1,
            last_reviewed_generation=0,
        )
        self.assertTrue(d.should_run)

    def test_label_added_only_skips_when_event_label_is_unrelated(self) -> None:
        """Regression for the PR #9 self-review finding: someone adds an
        unrelated label (e.g. `bug`) while `ready` is already present. The
        `labeled` webhook fires; we must NOT run a full review."""
        d = reviewer.resolve_trigger_action(
            trigger_mode=reviewer.TRIGGER_LABEL_ADDED_ONLY,
            event_action="labeled",
            event_label="bug",
            label_gate="ready",
            current_labels=["ready", "bug"],
            label_toggle_generation=1,
            last_reviewed_generation=0,
        )
        self.assertFalse(d.should_run)
        self.assertIn("'bug'", d.reason)
        self.assertIn("'ready'", d.reason)

    def test_label_added_only_backcompat_when_event_label_unknown(self) -> None:
        """`event_label=""` (payload not available) preserves v1.2.0
        behaviour so runs from GitHub UI/API label additions where the
        payload is missing still work."""
        d = reviewer.resolve_trigger_action(
            trigger_mode=reviewer.TRIGGER_LABEL_ADDED_ONLY,
            event_action="labeled",
            event_label="",
            label_gate="ready",
            current_labels=["ready"],
            label_toggle_generation=1,
            last_reviewed_generation=0,
        )
        self.assertTrue(d.should_run)

    def test_unknown_trigger_mode_blocks(self) -> None:  # keep signature stable
        d = reviewer.resolve_trigger_action(
            trigger_mode="whatever",
            event_action="opened",
            label_gate="ready",
            current_labels=["ready"],
            label_toggle_generation=0,
            last_reviewed_generation=0,
        )
        self.assertFalse(d.should_run)


class AgentRunnerNoopWarningTests(unittest.TestCase):
    """`build_agent_runner_noop_warning` — autocomplete-only notice on CLIs.

    Regression for the PR #9 self-review finding: enabling
    `pr-description-mode=autocomplete` on an agent-runner CLI provider
    silently no-ops the PATCH. Complexity labeling is bridged via
    `findings.json` on all provider families.
    """

    def test_chat_completions_never_warns(self) -> None:
        w = reviewer.build_agent_runner_noop_warning(
            provider_id="anthropic",
            is_agent_runner=False,
            pr_desc_mode=reviewer.PR_DESC_MODE_AUTOCOMPLETE,
            complexity_labels_enabled=True,
        )
        self.assertEqual(w, "")

    def test_agent_runner_with_no_optin_features_is_silent(self) -> None:
        w = reviewer.build_agent_runner_noop_warning(
            provider_id="cursor",
            is_agent_runner=True,
            pr_desc_mode=reviewer.PR_DESC_MODE_OFF,
            complexity_labels_enabled=False,
        )
        self.assertEqual(w, "")

    def test_warn_and_block_modes_do_not_trigger(self) -> None:
        """`warn`/`block` inspect the body themselves — they don't need
        `set_pr_description` tool and DO work on agent-runners."""
        for mode in (
            reviewer.PR_DESC_MODE_WARN,
            reviewer.PR_DESC_MODE_BLOCK,
        ):
            with self.subTest(mode=mode):
                w = reviewer.build_agent_runner_noop_warning(
                    provider_id="cursor",
                    is_agent_runner=True,
                    pr_desc_mode=mode,
                    complexity_labels_enabled=False,
                )
                self.assertEqual(w, "", f"mode={mode!r} should not warn")

    def test_autocomplete_on_agent_runner_warns(self) -> None:
        w = reviewer.build_agent_runner_noop_warning(
            provider_id="cursor",
            is_agent_runner=True,
            pr_desc_mode=reviewer.PR_DESC_MODE_AUTOCOMPLETE,
            complexity_labels_enabled=False,
        )
        self.assertIn("WARNING", w)
        self.assertIn("pr-description-mode=autocomplete", w)
        self.assertIn("'cursor'", w)
        self.assertIn("PR_METADATA_CHECKS.md", w)

    def test_complexity_on_agent_runner_does_not_warn(self) -> None:
        """Complexity labeling is bridged via findings.json on agent-runners."""
        w = reviewer.build_agent_runner_noop_warning(
            provider_id="claude-code",
            is_agent_runner=True,
            pr_desc_mode=reviewer.PR_DESC_MODE_OFF,
            complexity_labels_enabled=True,
        )
        self.assertEqual(w, "")

    def test_both_features_listed_together(self) -> None:
        w = reviewer.build_agent_runner_noop_warning(
            provider_id="codex",
            is_agent_runner=True,
            pr_desc_mode=reviewer.PR_DESC_MODE_AUTOCOMPLETE,
            complexity_labels_enabled=True,
        )
        self.assertIn("pr-description-mode=autocomplete", w)
        self.assertNotIn("complexity-labels-enabled=true", w)


class TriggerStateRoundtripTests(unittest.TestCase):
    """`write_trigger_state` + `read_trigger_state` roundtrip."""

    def test_write_then_read_recovers_state(self) -> None:
        body = "<!-- ai-pr-reviewer-marker -->\n\nSome body content."
        written = reviewer.write_trigger_state(
            body, {"label_toggle_generation": 3, "reviewed_sha": "abc"}
        )
        state = reviewer.read_trigger_state(written)
        self.assertEqual(state["label_toggle_generation"], 3)
        self.assertEqual(state["reviewed_sha"], "abc")

    def test_read_returns_empty_when_no_marker(self) -> None:
        self.assertEqual(reviewer.read_trigger_state(""), {})
        self.assertEqual(
            reviewer.read_trigger_state("Just some markdown, no state."),
            {},
        )

    def test_write_replaces_prior_state(self) -> None:
        body = "<!-- ai-pr-reviewer-marker -->\n\nBody."
        step1 = reviewer.write_trigger_state(
            body, {"label_toggle_generation": 1}
        )
        step2 = reviewer.write_trigger_state(
            step1, {"label_toggle_generation": 2}
        )
        # Only the most recent state should be present.
        self.assertEqual(step2.count("ai-pr-reviewer-state"), 1)
        state = reviewer.read_trigger_state(step2)
        self.assertEqual(state["label_toggle_generation"], 2)

    def test_read_ignores_malformed_json(self) -> None:
        body = (
            "<!-- ai-pr-reviewer-marker -->\n"
            "<!-- ai-pr-reviewer-state: {not valid json} -->\n"
            "Body."
        )
        self.assertEqual(reviewer.read_trigger_state(body), {})


class EvaluatePrDescriptionTests(unittest.TestCase):
    """Cheap heuristic covering empty / short / adequate bodies."""

    def test_empty_body_is_inadequate(self) -> None:
        v = reviewer.evaluate_pr_description("", min_length=50)
        self.assertFalse(v.is_adequate)
        self.assertIn("empty", v.reason.lower())

    def test_whitespace_only_body_is_inadequate(self) -> None:
        v = reviewer.evaluate_pr_description("   \n\t\n  ", min_length=50)
        self.assertFalse(v.is_adequate)

    def test_short_body_is_inadequate(self) -> None:
        v = reviewer.evaluate_pr_description("wip", min_length=50)
        self.assertFalse(v.is_adequate)
        self.assertIn("too short", v.reason.lower())
        self.assertIn("50", v.reason)

    def test_body_at_threshold_is_adequate(self) -> None:
        body = "x" * 50
        v = reviewer.evaluate_pr_description(body, min_length=50)
        self.assertTrue(v.is_adequate)
        self.assertEqual(v.reason, "")

    def test_marker_is_stripped_before_length_check(self) -> None:
        # The autocomplete marker adds ~50 chars; a body that's just the
        # marker should NOT pass the min_length gate.
        body = reviewer.PR_DESC_AUTOCOMPLETE_MARKER + "x"
        v = reviewer.evaluate_pr_description(body, min_length=50)
        self.assertFalse(v.is_adequate)

    def test_none_body_treated_as_empty(self) -> None:
        v = reviewer.evaluate_pr_description(None, min_length=50)  # type: ignore[arg-type]
        self.assertFalse(v.is_adequate)


class ToolSetPrDescriptionTests(unittest.TestCase):
    """`tool_set_pr_description` records into state and enforces one-shot."""

    def test_records_proposal(self) -> None:
        state = reviewer.ReviewState()
        result = reviewer.tool_set_pr_description(
            {"body": "New rich PR body with context."}, state
        )
        self.assertEqual(
            state.proposed_pr_description, "New rich PR body with context."
        )
        self.assertIn("recorded", result.lower())

    def test_rejects_empty_body(self) -> None:
        state = reviewer.ReviewState()
        result = reviewer.tool_set_pr_description({"body": "  \n  "}, state)
        self.assertIsNone(state.proposed_pr_description)
        self.assertIn("Error", result)

    def test_one_shot(self) -> None:
        state = reviewer.ReviewState()
        reviewer.tool_set_pr_description({"body": "first"}, state)
        result = reviewer.tool_set_pr_description({"body": "second"}, state)
        self.assertEqual(state.proposed_pr_description, "first")
        self.assertIn("already", result.lower())


class ToolSetPrComplexityTests(unittest.TestCase):
    """`tool_set_pr_complexity` records + validates level enum."""

    def test_records_low(self) -> None:
        state = reviewer.ReviewState()
        result = reviewer.tool_set_pr_complexity({"level": "low"}, state)
        self.assertEqual(state.proposed_pr_complexity, "low")
        self.assertIn("recorded", result.lower())

    def test_case_normalized(self) -> None:
        state = reviewer.ReviewState()
        reviewer.tool_set_pr_complexity({"level": "HIGH"}, state)
        self.assertEqual(state.proposed_pr_complexity, "high")

    def test_rejects_unknown_level(self) -> None:
        state = reviewer.ReviewState()
        result = reviewer.tool_set_pr_complexity({"level": "epic"}, state)
        self.assertIsNone(state.proposed_pr_complexity)
        self.assertIn("Error", result)

    def test_one_shot(self) -> None:
        state = reviewer.ReviewState()
        reviewer.tool_set_pr_complexity({"level": "low"}, state)
        result = reviewer.tool_set_pr_complexity({"level": "high"}, state)
        self.assertEqual(state.proposed_pr_complexity, "low")
        self.assertIn("already", result.lower())


class ToolsSchemaGatingTests(unittest.TestCase):
    """Optional tools are exposed only when their flag is set."""

    def test_base_five_always_present(self) -> None:
        schema = reviewer.tools_schema(10)
        names = [t["name"] for t in schema]
        for expected in (
            "read_file",
            "grep",
            "glob",
            "post_inline_comment",
            "submit_review",
        ):
            self.assertIn(expected, names)

    def test_set_pr_description_absent_by_default(self) -> None:
        schema = reviewer.tools_schema(10)
        names = [t["name"] for t in schema]
        self.assertNotIn("set_pr_description", names)

    def test_set_pr_description_present_when_allowed(self) -> None:
        schema = reviewer.tools_schema(
            10, allow_set_pr_description=True
        )
        names = [t["name"] for t in schema]
        self.assertIn("set_pr_description", names)

    def test_set_pr_complexity_absent_by_default(self) -> None:
        schema = reviewer.tools_schema(10)
        names = [t["name"] for t in schema]
        self.assertNotIn("set_pr_complexity", names)

    def test_set_pr_complexity_present_when_allowed(self) -> None:
        schema = reviewer.tools_schema(
            10, allow_set_pr_complexity=True
        )
        names = [t["name"] for t in schema]
        self.assertIn("set_pr_complexity", names)

    def test_both_optional_tools_present_when_both_flags(self) -> None:
        schema = reviewer.tools_schema(
            10,
            allow_set_pr_description=True,
            allow_set_pr_complexity=True,
        )
        names = [t["name"] for t in schema]
        self.assertIn("set_pr_description", names)
        self.assertIn("set_pr_complexity", names)
        self.assertEqual(len(names), 7)


class ComposeSystemPromptTests(unittest.TestCase):
    """`compose_system_prompt(base, extension)` covers the four cases of
    the base+extension matrix.
    """

    def test_empty_extension_returns_base_unchanged(self) -> None:
        base = "You are the reviewer.\n"
        result = reviewer.compose_system_prompt(base, "")
        self.assertEqual(result, base)

    def test_extension_appended_with_separator(self) -> None:
        base = "You are the reviewer."
        ext = "Extra rule: never suggest `any`."
        result = reviewer.compose_system_prompt(base, ext)
        self.assertIn("You are the reviewer.", result)
        self.assertIn("---", result)
        self.assertIn("Extra rule: never suggest `any`.", result)
        # The base appears before the separator, extension after.
        self.assertLess(
            result.index("You are the reviewer."), result.index("---")
        )
        self.assertLess(
            result.index("---"),
            result.index("Extra rule: never suggest `any`."),
        )

    def test_extension_strips_leading_whitespace(self) -> None:
        base = "Base."
        ext = "\n\n\nExtension."
        result = reviewer.compose_system_prompt(base, ext)
        # Should not have four consecutive newlines between separator and
        # extension body — extension is lstripped.
        self.assertNotIn("---\n\n\n\nExtension", result)
        self.assertIn("---\n\nExtension.", result)

    def test_base_strips_trailing_whitespace(self) -> None:
        base = "Base.\n\n\n\n"
        ext = "Ext."
        result = reviewer.compose_system_prompt(base, ext)
        self.assertIn("Base.\n\n---\n\nExt.", result)

    def test_full_replacement_semantic_preserved(self) -> None:
        # When a custom prompt-file is used as the base, the same
        # composition rule applies with no default content leaked.
        custom_base = "# Custom prompt\nOnly rule: be brief."
        ext = "Additional rule: cite line numbers."
        result = reviewer.compose_system_prompt(custom_base, ext)
        self.assertIn("Custom prompt", result)
        self.assertIn("Only rule: be brief.", result)
        self.assertIn("Additional rule: cite line numbers.", result)
        # No leakage of the bundled default (its unique phrase).
        self.assertNotIn("post_inline_comment", result)


class EvaluateStrictnessTests(unittest.TestCase):
    def test_lenient_never_blocks(self) -> None:
        blocked, _ = reviewer.evaluate_strictness(
            reviewer.SEVERITY_CRITICAL, reviewer.STRICTNESS_LENIENT
        )
        self.assertFalse(blocked)

    def test_block_on_critical(self) -> None:
        self.assertTrue(
            reviewer.evaluate_strictness(
                reviewer.SEVERITY_CRITICAL, reviewer.STRICTNESS_BLOCK_CRITICAL
            )[0]
        )
        self.assertFalse(
            reviewer.evaluate_strictness(
                reviewer.SEVERITY_WARNING, reviewer.STRICTNESS_BLOCK_CRITICAL
            )[0]
        )

    def test_block_on_warning(self) -> None:
        self.assertTrue(
            reviewer.evaluate_strictness(
                reviewer.SEVERITY_WARNING, reviewer.STRICTNESS_BLOCK_WARNING
            )[0]
        )
        self.assertTrue(
            reviewer.evaluate_strictness(
                reviewer.SEVERITY_CRITICAL, reviewer.STRICTNESS_BLOCK_WARNING
            )[0]
        )
        self.assertFalse(
            reviewer.evaluate_strictness(
                reviewer.SEVERITY_INFO, reviewer.STRICTNESS_BLOCK_WARNING
            )[0]
        )

    def test_unknown_strictness_treated_lenient(self) -> None:
        blocked, reason = reviewer.evaluate_strictness(
            reviewer.SEVERITY_CRITICAL, "bogus"
        )
        self.assertFalse(blocked)
        self.assertIn("lenient", reason)

    def test_block_on_any_blocks_on_info(self) -> None:
        blocked, reason = reviewer.evaluate_strictness(
            reviewer.SEVERITY_INFO, reviewer.STRICTNESS_BLOCK_ANY
        )
        self.assertTrue(blocked)
        self.assertIn("block-on-any", reason)

    def test_block_on_any_blocks_on_warning(self) -> None:
        self.assertTrue(
            reviewer.evaluate_strictness(
                reviewer.SEVERITY_WARNING, reviewer.STRICTNESS_BLOCK_ANY
            )[0]
        )

    def test_block_on_any_blocks_on_critical(self) -> None:
        self.assertTrue(
            reviewer.evaluate_strictness(
                reviewer.SEVERITY_CRITICAL, reviewer.STRICTNESS_BLOCK_ANY
            )[0]
        )

    def test_block_on_any_passes_on_none(self) -> None:
        blocked, reason = reviewer.evaluate_strictness(
            reviewer.SEVERITY_NONE, reviewer.STRICTNESS_BLOCK_ANY
        )
        self.assertFalse(blocked)
        self.assertIn("no findings", reason)

    def test_block_on_any_in_valid_strictness(self) -> None:
        self.assertIn(
            reviewer.STRICTNESS_BLOCK_ANY, reviewer.VALID_STRICTNESS
        )


class SafeRepoPathTests(unittest.TestCase):
    def setUp(self) -> None:
        self._prev_cwd = os.getcwd()
        self._tmp = tempfile.TemporaryDirectory()
        os.chdir(self._tmp.name)

    def tearDown(self) -> None:
        os.chdir(self._prev_cwd)
        self._tmp.cleanup()

    def test_in_workspace_ok(self) -> None:
        p = reviewer.safe_repo_path("sub/file.py")
        self.assertTrue(str(p).startswith(str(Path.cwd().resolve())))

    def test_parent_traversal_rejected(self) -> None:
        with self.assertRaises(ValueError):
            reviewer.safe_repo_path("../../etc/passwd")

    def test_sibling_prefix_not_bypassed(self) -> None:
        # `/x/repo` vs `/x/repo_evil` — string-prefix must not pass.
        with self.assertRaises(ValueError):
            reviewer.safe_repo_path("../" + Path.cwd().name + "_evil/x")


class ToolReadFileTests(unittest.TestCase):
    def setUp(self) -> None:
        self._prev_cwd = os.getcwd()
        self._tmp = tempfile.TemporaryDirectory()
        os.chdir(self._tmp.name)
        Path("a.txt").write_text(
            "line1\nline2\nline3\nline4\n", encoding="utf-8"
        )

    def tearDown(self) -> None:
        os.chdir(self._prev_cwd)
        self._tmp.cleanup()

    def test_reads_and_numbers_lines(self) -> None:
        out = reviewer.tool_read_file({"path": "a.txt"})
        self.assertIn("line1", out)
        self.assertIn("line4", out)
        self.assertIn("of 4", out)

    def test_offset_and_limit(self) -> None:
        out = reviewer.tool_read_file({"path": "a.txt", "offset": 2, "limit": 2})
        self.assertIn("line2", out)
        self.assertIn("line3", out)
        self.assertNotIn("line4", out)

    def test_missing_file(self) -> None:
        self.assertIn("not found", reviewer.tool_read_file({"path": "nope.txt"}))

    def test_escape_is_blocked(self) -> None:
        out = reviewer.tool_read_file({"path": "../../etc/hosts"})
        self.assertIn("escapes the workspace", out)


class ToolPostInlineCommentTests(unittest.TestCase):
    def test_queues_and_normalizes_severity(self) -> None:
        state = reviewer.ReviewState(max_inline_comments=3)
        msg = reviewer.tool_post_inline_comment(
            {"path": "a.py", "line": 10, "body": "x", "severity": "CRITICAL"},
            state,
        )
        self.assertIn("Queued", msg)
        self.assertEqual(state.severities, ["critical"])
        self.assertEqual(state.inline_comments[0]["line"], 10)
        self.assertEqual(state.inline_comments[0]["side"], "RIGHT")

    def test_invalid_severity_defaults_info(self) -> None:
        state = reviewer.ReviewState()
        reviewer.tool_post_inline_comment(
            {"path": "a.py", "line": 1, "body": "x", "severity": "bogus"}, state
        )
        self.assertEqual(state.severities, [reviewer.SEVERITY_INFO])

    def test_multiline_sets_start_fields(self) -> None:
        state = reviewer.ReviewState()
        reviewer.tool_post_inline_comment(
            {"path": "a.py", "line": 10, "start_line": 8, "body": "x"}, state
        )
        self.assertEqual(state.inline_comments[0]["start_line"], 8)
        self.assertEqual(state.inline_comments[0]["start_side"], "RIGHT")

    def test_cap_enforced(self) -> None:
        state = reviewer.ReviewState(max_inline_comments=1)
        reviewer.tool_post_inline_comment(
            {"path": "a.py", "line": 1, "body": "x"}, state
        )
        msg = reviewer.tool_post_inline_comment(
            {"path": "a.py", "line": 2, "body": "y"}, state
        )
        self.assertIn("cap reached", msg)
        self.assertEqual(len(state.inline_comments), 1)


class ToolSubmitReviewTests(unittest.TestCase):
    def test_records_summary(self) -> None:
        state = reviewer.ReviewState()
        reviewer.tool_submit_review({"summary": "All good"}, state)
        self.assertEqual(state.final_summary, "All good")

    def test_idempotent_second_call_errors(self) -> None:
        state = reviewer.ReviewState()
        reviewer.tool_submit_review({"summary": "first"}, state)
        msg = reviewer.tool_submit_review({"summary": "second"}, state)
        self.assertIn("already called", msg)
        self.assertEqual(state.final_summary, "first")


class StateToReviewResultTests(unittest.TestCase):
    """Adapter: ReviewState (chat-completions path) → ReviewResult (unified)."""

    def test_happy_path_maps_all_fields(self) -> None:
        state = reviewer.ReviewState()
        reviewer.tool_post_inline_comment(
            {
                "path": "a.py",
                "line": 12,
                "body": "critical bug",
                "severity": "critical",
            },
            state,
        )
        reviewer.tool_post_inline_comment(
            {
                "path": "b.py",
                "line": 8,
                "start_line": 5,
                "body": "range issue",
                "severity": "warning",
                "side": "LEFT",
            },
            state,
        )
        state.final_summary = "## Summary\n\nTwo issues found."

        result = reviewer.state_to_review_result(state)

        self.assertEqual(result.summary, "## Summary\n\nTwo issues found.")
        self.assertEqual(len(result.findings), 2)
        self.assertEqual(result.overall_severity, reviewer.SEVERITY_CRITICAL)

        first = result.findings[0]
        self.assertEqual(first.path, "a.py")
        self.assertEqual(first.line, 12)
        self.assertEqual(first.body, "critical bug")
        self.assertEqual(first.severity, "critical")
        self.assertIsNone(first.start_line)

        second = result.findings[1]
        self.assertEqual(second.path, "b.py")
        self.assertEqual(second.start_line, 5)
        self.assertEqual(second.side, "LEFT")
        self.assertEqual(second.severity, "warning")

    def test_empty_state_yields_empty_result(self) -> None:
        state = reviewer.ReviewState()

        result = reviewer.state_to_review_result(state)

        self.assertEqual(result.summary, "")
        self.assertEqual(result.findings, [])
        self.assertEqual(result.overall_severity, reviewer.SEVERITY_NONE)


class FindingsToGhInlineCommentsTests(unittest.TestCase):
    """Encoder: list[Finding] → GitHub Reviews API inline shape."""

    def test_single_line_finding(self) -> None:
        findings = [
            reviewer.Finding(
                path="a.py",
                line=42,
                body="typo",
                severity="info",
                side="RIGHT",
            )
        ]
        out = reviewer.findings_to_gh_inline_comments(findings)
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["path"], "a.py")
        self.assertEqual(out[0]["line"], 42)
        self.assertEqual(out[0]["side"], "RIGHT")
        self.assertNotIn("start_line", out[0])

    def test_multiline_finding_adds_start_side(self) -> None:
        findings = [
            reviewer.Finding(
                path="a.py",
                line=10,
                body="range",
                start_line=8,
                side="LEFT",
            )
        ]
        out = reviewer.findings_to_gh_inline_comments(findings)
        self.assertEqual(out[0]["start_line"], 8)
        self.assertEqual(out[0]["start_side"], "LEFT")

    def test_empty_findings_yields_empty_list(self) -> None:
        self.assertEqual(reviewer.findings_to_gh_inline_comments([]), [])


class AgentRunnerProviderContractTests(unittest.TestCase):
    """The abstract base class exposes the expected interface."""

    def test_install_raises_not_implemented(self) -> None:
        with self.assertRaises(NotImplementedError):
            reviewer.AgentRunnerProvider().install()

    def test_run_review_raises_not_implemented(self) -> None:
        provider = reviewer.AgentRunnerProvider()
        with self.assertRaises(NotImplementedError):
            provider.run_review(
                pr_context=reviewer.PRContext(
                    title="t",
                    author="a",
                    head_ref="h",
                    base_ref="b",
                    state="open",
                    additions=0,
                    deletions=0,
                    commits=0,
                    body="",
                ),
                review_instructions="",
                workspace=Path("."),
                output_dir=Path("."),
            )


class ExecuteToolTests(unittest.TestCase):
    def test_unknown_tool(self) -> None:
        state = reviewer.ReviewState()
        self.assertIn(
            "unknown tool", reviewer.execute_tool("frobnicate", {}, state)
        )

    def test_exception_is_surfaced_not_raised(self) -> None:
        state = reviewer.ReviewState()
        # Missing required "path" key -> KeyError inside handler, surfaced.
        out = reviewer.execute_tool("read_file", {}, state)
        self.assertIn("raised", out)


class TrackingRenderTests(unittest.TestCase):
    def test_working_includes_collapse_note_when_enabled(self) -> None:
        body = reviewer.render_tracking_body_working(
            "abc1234def", collapse_previous=True
        )
        self.assertIn(reviewer.REVIEW_MARKER, body)
        self.assertIn("collapsed as outdated", body)

    def test_working_omits_collapse_note_when_disabled(self) -> None:
        body = reviewer.render_tracking_body_working(
            "abc1234def", collapse_previous=False
        )
        self.assertNotIn("collapsed", body)

    def test_done_blocked_shows_block_emoji(self) -> None:
        body = reviewer.render_tracking_body_done(
            head_sha="abc1234def",
            review_url="https://example/r",
            inline_attached=2,
            inline_dropped=0,
            severity="critical",
            blocked=True,
            block_reason="block-on-critical fired",
        )
        self.assertIn("🚫", body)
        self.assertIn("2 inline comment(s) attached", body)

    def test_done_reports_dropped(self) -> None:
        body = reviewer.render_tracking_body_done(
            head_sha="abc1234def",
            review_url="https://example/r",
            inline_attached=1,
            inline_dropped=3,
            severity="info",
            blocked=False,
            block_reason="ok",
        )
        self.assertIn("3 dropped", body)
        self.assertIn("422", body)

    # ------------------------------------------------------------------
    # Skip-review-label short-circuit renderer
    # ------------------------------------------------------------------

    def test_skipped_by_label_body_carries_review_marker(self) -> None:
        """The skip-tracker comment MUST include `REVIEW_MARKER` so that
        `collapse-previous` on the next real review recognises it as a
        prior bot artefact and minimises it. Missing the marker would
        leave the skip comment orphaned in the PR."""
        body: str = reviewer.render_tracking_body_skipped_by_label(
            head_sha="abc1234def", skip_label="skip-ai-review",
        )
        self.assertIn(reviewer.REVIEW_MARKER, body)

    def test_skipped_by_label_body_surfaces_the_label(self) -> None:
        """The label name MUST appear verbatim in the comment body so
        the developer reading the PR sees WHICH label triggered the
        skip — critical for audit trail on multi-label workflows."""
        body: str = reviewer.render_tracking_body_skipped_by_label(
            head_sha="abc1234def", skip_label="emergency-bypass",
        )
        self.assertIn("emergency-bypass", body)

    def test_skipped_by_label_body_uses_skip_emoji(self) -> None:
        """Distinguishes the skip terminal state from ✅ done, 🚫 blocked,
        and ❌ failed — glanceable in a comments list."""
        body: str = reviewer.render_tracking_body_skipped_by_label(
            head_sha="abc1234def", skip_label="skip-ai-review",
        )
        self.assertIn("⏭️", body)
        self.assertIn("skipped", body.lower())

    def test_skipped_by_label_body_states_no_llm_call(self) -> None:
        """Documents the contract in-line so a developer reading the
        comment understands nothing was analysed — no false sense that
        the review passed."""
        body: str = reviewer.render_tracking_body_skipped_by_label(
            head_sha="abc1234def", skip_label="skip-ai-review",
        )
        self.assertIn("no LLM call", body)

    def test_skipped_by_label_body_includes_provider_marker(self) -> None:
        """When `provider` is set the body must include the per-provider
        marker so provider-scoped `collapse-previous` sees this skip
        comment on the next same-provider run — otherwise a
        multi-provider workflow would leave the skip comment live
        forever."""
        body: str = reviewer.render_tracking_body_skipped_by_label(
            head_sha="abc1234def",
            skip_label="skip-ai-review",
            provider="anthropic",
        )
        self.assertIn(reviewer.provider_marker("anthropic"), body)

    def test_skipped_by_label_body_omits_provider_marker_when_empty(
        self,
    ) -> None:
        """No provider → no per-provider marker line. Symmetry with the
        working/done renderers."""
        body: str = reviewer.render_tracking_body_skipped_by_label(
            head_sha="abc1234def",
            skip_label="skip-ai-review",
            provider="",
        )
        self.assertNotIn(reviewer.PROVIDER_MARKER_PREFIX, body)


class SkipLabelCollisionGuardTests(unittest.TestCase):
    """Regression coverage for `detect_skip_label_collisions` — the
    misconfiguration guard that runs before `main()` invokes the
    skip-review short-circuit. Round-13 F1: if `skip-review-label`
    matches any of the runtime's other semantic labels, every normal
    trigger silently becomes a skip. This guard aborts loudly instead.

    Also locks the case-insensitive comparison (matches
    `_labels_contain_ci` and `resolve_trigger_action` semantics)."""

    def test_no_skip_label_configured_no_collision(self) -> None:
        """Empty `skip-review-label` disables the feature entirely, so
        no collision is possible even if the other labels are set."""
        result: list[str] = reviewer.detect_skip_label_collisions(
            skip_review_label="",
            label_gate="Ready",
            applied_label="ai-reviewed",
            iteration_escape_label="full-review-please",
        )
        self.assertEqual(result, [])

    def test_distinct_labels_no_collision(self) -> None:
        """Well-configured setup with four distinct labels: safe."""
        result: list[str] = reviewer.detect_skip_label_collisions(
            skip_review_label="skip-ai-review",
            label_gate="Ready",
            applied_label="ai-reviewed",
            iteration_escape_label="full-review-please",
        )
        self.assertEqual(result, [])

    def test_collision_with_label_gate_detected(self) -> None:
        """Same as label-gate → every gated review silently skips."""
        result: list[str] = reviewer.detect_skip_label_collisions(
            skip_review_label="Ready",
            label_gate="Ready",
            applied_label="ai-reviewed",
            iteration_escape_label="full-review-please",
        )
        self.assertEqual(len(result), 1)
        self.assertIn("label-gate", result[0])

    def test_collision_with_applied_label_detected(self) -> None:
        """Same as applied-label → first successful review arms the
        skip on every subsequent trigger, freezing IAR at round 1."""
        result: list[str] = reviewer.detect_skip_label_collisions(
            skip_review_label="ai-reviewed",
            label_gate="Ready",
            applied_label="ai-reviewed",
            iteration_escape_label="full-review-please",
        )
        self.assertEqual(len(result), 1)
        self.assertIn("applied-label", result[0])

    def test_collision_with_escape_label_detected(self) -> None:
        """Same as escape label → the "force full review" gesture is
        silently converted into a "skip review" gesture."""
        result: list[str] = reviewer.detect_skip_label_collisions(
            skip_review_label="full-review-please",
            label_gate="Ready",
            applied_label="ai-reviewed",
            iteration_escape_label="full-review-please",
        )
        self.assertEqual(len(result), 1)
        self.assertIn("iteration-escape-label", result[0])

    def test_collision_detection_is_case_insensitive(self) -> None:
        """Casing mismatch between config and PR labels can never be
        the load-bearing signal that skip-label is safe — the check
        must match `_labels_contain_ci` semantics used at the call
        sites."""
        result: list[str] = reviewer.detect_skip_label_collisions(
            skip_review_label="READY",
            label_gate="ready",
            applied_label="ai-reviewed",
            iteration_escape_label="full-review-please",
        )
        self.assertEqual(len(result), 1)
        self.assertIn("label-gate", result[0])

    def test_multiple_collisions_reported_together(self) -> None:
        """A pathological config that collides with two other labels
        surfaces both in the error message so the developer sees the
        full picture in one abort, not one collision at a time."""
        result: list[str] = reviewer.detect_skip_label_collisions(
            skip_review_label="Ready",
            label_gate="Ready",
            applied_label="Ready",
            iteration_escape_label="Ready",
        )
        self.assertEqual(len(result), 3)
        self.assertTrue(any("label-gate" in c for c in result))
        self.assertTrue(any("applied-label" in c for c in result))
        self.assertTrue(
            any("iteration-escape-label" in c for c in result)
        )

    def test_whitespace_padding_normalised(self) -> None:
        """User might paste a label with trailing whitespace from a
        copy-paste. Guard must catch the collision anyway."""
        result: list[str] = reviewer.detect_skip_label_collisions(
            skip_review_label="  skip-ai-review  ",
            label_gate="Ready",
            applied_label="skip-ai-review",
            iteration_escape_label="full-review-please",
        )
        self.assertEqual(len(result), 1)
        self.assertIn("applied-label", result[0])

    def test_empty_label_gate_ignored(self) -> None:
        """Consumers who don't use `label-gate` leave it as `""`.
        An empty gate is "not configured" and must not spuriously
        collide with an empty `skip-review-label`."""
        result: list[str] = reviewer.detect_skip_label_collisions(
            skip_review_label="skip-ai-review",
            label_gate="",
            applied_label="",
            iteration_escape_label="full-review-please",
        )
        self.assertEqual(result, [])


class OutputWritingTests(unittest.TestCase):
    def setUp(self) -> None:
        self._fd, self._path = tempfile.mkstemp()
        os.close(self._fd)
        self._prev = os.environ.get("GITHUB_OUTPUT")
        os.environ["GITHUB_OUTPUT"] = self._path

    def tearDown(self) -> None:
        if self._prev is None:
            os.environ.pop("GITHUB_OUTPUT", None)
        else:
            os.environ["GITHUB_OUTPUT"] = self._prev
        os.unlink(self._path)

    def _read(self) -> str:
        return Path(self._path).read_text(encoding="utf-8")

    def test_scalar_written(self) -> None:
        reviewer.write_action_output("blocked", "false")
        self.assertIn("blocked=false", self._read())

    def test_multiline_uses_heredoc(self) -> None:
        reviewer.write_action_output("summary", "a\nb")
        content = self._read()
        self.assertIn("summary<<", content)
        self.assertIn("a\nb", content)

    def test_write_all_outputs_emits_six(self) -> None:
        reviewer.write_all_outputs(
            skipped=False,
            severity="warning",
            inline_attached=4,
            inline_dropped=1,
            blocked=True,
            review_url="https://x/r",
        )
        content = self._read()
        for key in (
            "skipped=false",
            "severity=warning",
            "inline-attached=4",
            "inline-dropped=1",
            "blocked=true",
            "review-url=https://x/r",
        ):
            self.assertIn(key, content)

    def test_write_all_outputs_defaults_for_failure(self) -> None:
        reviewer.write_all_outputs(skipped=False)
        content = self._read()
        self.assertIn("severity=none", content)
        self.assertIn("inline-attached=0", content)
        self.assertIn("blocked=false", content)
        self.assertIn("review-url=\n", content + "\n")


class BuildProviderTests(unittest.TestCase):
    def test_anthropic(self) -> None:
        p = reviewer.build_provider("anthropic", api_key="k", model="m")
        self.assertIsInstance(p, reviewer.AnthropicProvider)

    def test_unknown_provider_raises(self) -> None:
        with self.assertRaises(ValueError):
            reviewer.build_provider("openai", api_key="k", model="m")


class ToolsSchemaTests(unittest.TestCase):
    def test_all_five_tools_present(self) -> None:
        names = {t["name"] for t in reviewer.tools_schema(10)}
        self.assertEqual(
            names,
            {
                "read_file",
                "grep",
                "glob",
                "post_inline_comment",
                "submit_review",
            },
        )

    def test_cap_referenced_in_description(self) -> None:
        schema = reviewer.tools_schema(7)
        post = next(
            t for t in schema if t["name"] == "post_inline_comment"
        )
        self.assertIn("7", post["description"])


class FakeProvider(reviewer.Provider):
    """Provider stub that drives the loop deterministically.

    Emits N tool-call turns (each an unknown-tool stub so `execute_tool`
    returns immediately without touching the filesystem or shelling out)
    then a final text turn, so we can assert the conversation-pruning
    invariant without any network AND without any I/O — critical for the
    40-turn stress test in DriveReviewPruningTests.
    """

    def __init__(self, *, tool_turns: int) -> None:
        self._tool_turns = tool_turns
        self.calls = 0

    def complete(
        self,
        *,
        system_prompt: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
    ) -> dict[str, Any]:
        self.calls += 1
        if self.calls <= self._tool_turns:
            return {
                "stop_reason": "tool_use",
                "content": [
                    {
                        "type": "tool_use",
                        "id": f"call_{self.calls}",
                        # Unknown tool name → execute_tool returns
                        # "Error: unknown tool" immediately. The pruning
                        # loop treats it as a tool_result and moves on.
                        "name": "__pruning_test_stub__",
                        "input": {},
                    }
                ],
            }
        return {
            "stop_reason": "end_turn",
            "content": [{"type": "text", "text": "done"}],
        }


class DriveReviewPruningTests(unittest.TestCase):
    def test_history_is_bounded_and_alternates(self) -> None:
        provider = FakeProvider(tool_turns=40)
        messages: list[dict[str, Any]] = [
            {"role": "user", "content": "seed"}
        ]
        state = reviewer.ReviewState()
        reviewer.drive_review(
            provider=provider,
            system_prompt="sys",
            messages=messages,
            tools=reviewer.tools_schema(10),
            state=state,
            max_turns=40,
        )
        # Seed message is preserved at index 0.
        self.assertEqual(messages[0]["role"], "user")
        # History stays bounded by the retention setting (seed + pairs).
        cap = 1 + 2 * reviewer.MAX_CONVERSATION_TURNS_RETAINED + 2
        self.assertLessEqual(len(messages), cap)
        # Roles must strictly alternate user/assistant/user/... so the
        # Anthropic API never sees an orphaned tool_result.
        for i in range(1, len(messages)):
            self.assertNotEqual(
                messages[i]["role"],
                messages[i - 1]["role"],
                f"non-alternating roles at index {i}",
            )

    def test_stops_on_submit_review(self) -> None:
        class SubmittingProvider(reviewer.Provider):
            def complete(
                self,
                *,
                system_prompt: str,
                messages: list[dict[str, Any]],
                tools: list[dict[str, Any]],
            ) -> dict[str, Any]:
                return {
                    "stop_reason": "tool_use",
                    "content": [
                        {
                            "type": "tool_use",
                            "id": "c1",
                            "name": "submit_review",
                            "input": {"summary": "ok"},
                        }
                    ],
                }

        messages: list[dict[str, Any]] = [{"role": "user", "content": "seed"}]
        state = reviewer.ReviewState()
        reviewer.drive_review(
            provider=SubmittingProvider(),
            system_prompt="sys",
            messages=messages,
            tools=reviewer.tools_schema(10),
            state=state,
            max_turns=30,
        )
        self.assertEqual(state.final_summary, "ok")


class SecretScrubbingTests(unittest.TestCase):
    """Registered secret VALUES must be scrubbed from any public-facing text.

    Defense-in-depth for the agent-runner path (a prompt-injected vendor CLI
    could echo its API key into a finding). See docs/SECURITY.md.
    """

    def setUp(self) -> None:
        self._saved = set(reviewer._SECRET_VALUES)
        reviewer._SECRET_VALUES.clear()

    def tearDown(self) -> None:
        reviewer._SECRET_VALUES.clear()
        reviewer._SECRET_VALUES.update(self._saved)

    def test_registered_secret_is_scrubbed(self) -> None:
        reviewer.register_secret("sk-ant-supersecretvalue123")
        out = reviewer.scrub_secrets(
            "leaked: sk-ant-supersecretvalue123 end"
        )
        self.assertNotIn("sk-ant-supersecretvalue123", out)
        self.assertIn("***", out)

    def test_short_values_are_not_registered(self) -> None:
        reviewer.register_secret("abc")  # below MIN_SCRUBBABLE_SECRET_LEN
        self.assertEqual(reviewer.scrub_secrets("abc def"), "abc def")

    def test_empty_and_none_safe(self) -> None:
        reviewer.register_secret("")
        self.assertEqual(reviewer.scrub_secrets(""), "")

    def test_failure_tracking_body_scrubs_secret(self) -> None:
        reviewer.register_secret("ghp_tokenvalue_1234567890")
        body = reviewer.render_tracking_body_failed(
            head_sha="abc1234def",
            error="RuntimeError: leaked ghp_tokenvalue_1234567890 in stderr",
        )
        self.assertNotIn("ghp_tokenvalue_1234567890", body)
        self.assertIn("***", body)


class CliProxyEnvForwardingTests(unittest.TestCase):
    """Proxy + base-url network config must reach the vendor CLI subprocess
    (I3) — a CLI behind a corporate proxy can't reach its API otherwise."""

    def test_proxy_and_base_url_vars_forwarded(self) -> None:
        prev = dict(os.environ)
        try:
            os.environ.clear()
            os.environ.update(
                {
                    "PATH": "/usr/bin",
                    "HTTPS_PROXY": "http://proxy:8080",
                    "NO_PROXY": "localhost",
                    "ANTHROPIC_BASE_URL": "https://gw.internal/v1",
                }
            )
            env = reviewer._build_cli_env(extra_vars={})
            self.assertEqual(env.get("HTTPS_PROXY"), "http://proxy:8080")
            self.assertEqual(env.get("NO_PROXY"), "localhost")
            self.assertEqual(
                env.get("ANTHROPIC_BASE_URL"), "https://gw.internal/v1"
            )
        finally:
            os.environ.clear()
            os.environ.update(prev)


class DefaultModelTests(unittest.TestCase):
    """Lock the intended per-provider default models. These are the
    load-bearing cost/quality guardrail — `test_default_models_covers_all_
    shipping_providers` only checks the keys are truthy, so `"auto"` or a
    smoke-tier model would pass there. Assert the exact values so a future
    edit can't silently revert `claude-code` → `auto` or `codex` → a
    deprecated/smoke model without a red suite."""

    def test_anthropic_default_is_sonnet(self) -> None:
        self.assertEqual(
            reviewer.DEFAULT_MODELS["anthropic"], "claude-sonnet-4-6"
        )

    def test_claude_code_default_is_sonnet_not_auto(self) -> None:
        self.assertEqual(
            reviewer.DEFAULT_MODELS["claude-code"], "claude-sonnet-4-6"
        )
        self.assertNotEqual(reviewer.DEFAULT_MODELS["claude-code"], "auto")

    def test_cursor_default_is_auto(self) -> None:
        self.assertEqual(reviewer.DEFAULT_MODELS["cursor"], "auto")

    def test_codex_default_is_quality_tier_not_deprecated(self) -> None:
        self.assertEqual(reviewer.DEFAULT_MODELS["codex"], "gpt-5.6-luna")
        # Never the deprecated model, never the smoke/mini tier.
        self.assertNotEqual(reviewer.DEFAULT_MODELS["codex"], "gpt-5-codex")
        self.assertNotEqual(reviewer.DEFAULT_MODELS["codex"], "gpt-5.4-mini")


class ProviderMarkerTrackingBodyTests(unittest.TestCase):
    """Tracking-comment bodies carry the per-provider marker (for scoped
    collapse) when `provider` is set, and always carry the review marker."""

    def test_working_body_carries_provider_marker(self) -> None:
        body = reviewer.render_tracking_body_working(
            "abc1234", collapse_previous=True, provider="codex"
        )
        self.assertIn(reviewer.REVIEW_MARKER, body)
        self.assertIn(reviewer.provider_marker("codex"), body)

    def test_done_body_carries_provider_marker(self) -> None:
        body = reviewer.render_tracking_body_done(
            head_sha="abc1234",
            review_url="http://x",
            inline_attached=0,
            inline_dropped=0,
            severity="none",
            blocked=False,
            block_reason="ok",
            provider="cursor",
        )
        self.assertIn(reviewer.provider_marker("cursor"), body)

    def test_omitting_provider_keeps_legacy_body(self) -> None:
        body = reviewer.render_tracking_body_working(
            "abc1234", collapse_previous=True
        )
        self.assertIn(reviewer.REVIEW_MARKER, body)
        self.assertNotIn(reviewer.PROVIDER_MARKER_PREFIX, body)


class AgentMaxTurnsWarningTests(unittest.TestCase):
    """`agent-max-turns` has no CLI enforcement point today — build_provider
    must WARN when it's set for an agent-runner rather than silently ignore
    it (W1)."""

    def _capture_logs(self) -> list[str]:
        logs: list[str] = []
        self._orig_log = reviewer.log
        reviewer.log = logs.append  # type: ignore[assignment]
        return logs

    def _restore(self) -> None:
        reviewer.log = self._orig_log  # type: ignore[assignment]

    def test_warns_when_set_for_agent_runner(self) -> None:
        prev = dict(os.environ)
        logs = self._capture_logs()
        try:
            os.environ["AIPRR_AGENT_MAX_TURNS"] = "20"
            reviewer.build_provider("cursor", api_key="k", model="")
            self.assertTrue(
                any("agent-max-turns" in m for m in logs),
                "build_provider must warn that agent-max-turns is not "
                "enforced for the CLI providers.",
            )
        finally:
            self._restore()
            os.environ.clear()
            os.environ.update(prev)

    def test_no_warning_when_unset(self) -> None:
        prev = dict(os.environ)
        logs = self._capture_logs()
        try:
            os.environ.pop("AIPRR_AGENT_MAX_TURNS", None)
            reviewer.build_provider("cursor", api_key="k", model="")
            self.assertFalse(any("agent-max-turns" in m for m in logs))
        finally:
            self._restore()
            os.environ.clear()
            os.environ.update(prev)


class ResolveAuthorAssociationGateTests(unittest.TestCase):
    """Covers the abuse-prevention gate for public open-source repos."""

    def test_empty_gate_allows_any_author(self) -> None:
        d = reviewer.resolve_author_association_gate(
            gate="", actual_association="NONE"
        )
        self.assertTrue(d.should_run)
        self.assertIn("no author-association gate", d.reason)
        self.assertEqual(d.allowed_associations, ())

    def test_owner_matches_default_write_tier(self) -> None:
        d = reviewer.resolve_author_association_gate(
            gate="OWNER,MEMBER,COLLABORATOR",
            actual_association="OWNER",
        )
        self.assertTrue(d.should_run)
        self.assertEqual(d.author_association, "OWNER")

    def test_member_matches_default_write_tier(self) -> None:
        d = reviewer.resolve_author_association_gate(
            gate="OWNER,MEMBER,COLLABORATOR",
            actual_association="MEMBER",
        )
        self.assertTrue(d.should_run)

    def test_first_time_contributor_denied_by_default(self) -> None:
        d = reviewer.resolve_author_association_gate(
            gate="OWNER,MEMBER,COLLABORATOR",
            actual_association="FIRST_TIME_CONTRIBUTOR",
        )
        self.assertFalse(d.should_run)
        self.assertIn("not in gate", d.reason)
        self.assertIn("FIRST_TIME_CONTRIBUTOR", d.reason)

    def test_none_denied_by_default(self) -> None:
        d = reviewer.resolve_author_association_gate(
            gate="OWNER,MEMBER,COLLABORATOR",
            actual_association="NONE",
        )
        self.assertFalse(d.should_run)

    def test_contributor_allowed_when_explicitly_added(self) -> None:
        d = reviewer.resolve_author_association_gate(
            gate="OWNER,MEMBER,COLLABORATOR,CONTRIBUTOR",
            actual_association="CONTRIBUTOR",
        )
        self.assertTrue(d.should_run)

    def test_case_insensitive_gate(self) -> None:
        d = reviewer.resolve_author_association_gate(
            gate="owner, member ,collaborator",
            actual_association="owner",
        )
        self.assertTrue(d.should_run)
        self.assertEqual(d.author_association, "OWNER")
        self.assertEqual(
            d.allowed_associations, ("OWNER", "MEMBER", "COLLABORATOR")
        )

    def test_empty_actual_association_fail_open(self) -> None:
        # Local runs and workflow_dispatch have no PR context — fail-open
        # so the operator (who by definition has write access) is not
        # blocked from local debugging.
        d = reviewer.resolve_author_association_gate(
            gate="OWNER,MEMBER,COLLABORATOR",
            actual_association="",
        )
        self.assertTrue(d.should_run)
        self.assertIn("fail-open", d.reason)

    def test_unknown_gate_values_warn_but_dont_crash(self) -> None:
        # Typo in the whitelist ("MAINTAINER" is not a real GitHub value)
        # — the gate still applies, unknown values just never match.
        logs: list[str] = []
        original_log = reviewer.log
        reviewer.log = lambda m: logs.append(m)  # type: ignore[assignment]
        try:
            d = reviewer.resolve_author_association_gate(
                gate="OWNER,MAINTAINER,MEMBER",
                actual_association="MAINTAINER",
            )
        finally:
            reviewer.log = original_log  # type: ignore[assignment]
        # `MAINTAINER` is in the whitelist verbatim so it does match here,
        # but the warning must still fire.
        self.assertTrue(any("MAINTAINER" in m for m in logs))
        self.assertTrue(d.should_run)  # verbatim match

    def test_empty_pieces_in_gate_are_ignored(self) -> None:
        d = reviewer.resolve_author_association_gate(
            gate=",,OWNER,,MEMBER,,",
            actual_association="OWNER",
        )
        self.assertTrue(d.should_run)
        self.assertEqual(d.allowed_associations, ("OWNER", "MEMBER"))

    def test_strict_gate_allowing_only_owner_and_member(self) -> None:
        # Consumer excludes COLLABORATOR because some collaborators only
        # have read/triage access.
        d = reviewer.resolve_author_association_gate(
            gate="OWNER,MEMBER",
            actual_association="COLLABORATOR",
        )
        self.assertFalse(d.should_run)

    def test_gate_decision_is_frozen(self) -> None:
        d = reviewer.resolve_author_association_gate(
            gate="OWNER", actual_association="OWNER"
        )
        with self.assertRaises((AttributeError, Exception)):
            d.should_run = False  # type: ignore[misc]


if __name__ == "__main__":
    unittest.main()
