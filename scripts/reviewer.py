#!/usr/bin/env python3
"""AI PR reviewer — composite-action entry point.

Runs the full review lifecycle from a single Python process:

    1. Label gate     — exit early if the configured label is missing.
    2. Collapse prev  — mark previous bot reviews/comments as OUTDATED.
    3. Tracking comm. — post a spinner comment with the review marker.
    4. PR fetch       — pull metadata + diff once for the agentic loop seed.
    5. Agentic loop   — Anthropic Messages API + tool use (read/grep/glob/
                        post_inline_comment/submit_review).
    6. Submit review  — single POST with summary + queued inline comments,
                        with a 422 fallback that drops inline comments and
                        re-posts summary-only.
    7. Apply label    — apply `applied-label` if set and the run was not
                        blocked by strictness.
    8. Strictness     — exit code 2 if the configured strictness level is
                        violated, turning the GitHub check red.

Stdlib only — no extra dependencies, runs on any GitHub-hosted or
self-hosted runner that has Python 3.10+.

Environment (set by the composite action's `env:` block; see action.yml):

    AIPRR_PROVIDER           Provider id (`anthropic`, `claude-code`, `cursor`,
                            or `codex`).
    AIPRR_API_KEY            Provider API key.
    AIPRR_GH_TOKEN           GitHub token for PR/review operations.
    AIPRR_MODEL              Model id (empty = provider default).
    AIPRR_PROMPT_FILE        Path to a markdown system prompt (empty =
                            bundled `prompts/default.md`). Fully replaces
                            the base prompt.
    AIPRR_PROMPT_EXTENSION_FILE  Path to a markdown file APPENDED to the
                            base prompt. Layer overrides without copying
                            the whole default.
    AIPRR_AUTHOR_ASSOCIATION Comma-separated whitelist of accepted
                             GitHub `pull_request.author_association`
                             values. Default `OWNER,MEMBER,COLLABORATOR`
                             (write-tier only). Empty disables the gate.
                             See docs/SECURITY.md § "Author-association
                             gate" for rationale.
    AIPRR_LABEL_GATE         Required label, or empty for no gate.
    AIPRR_TRIGGER_MODE       `always` | `label-required` | `label-once` |
                            `label-added-only`. Empty = auto (label-required
                            when `label-gate` is set, else `always`).
    AIPRR_APPLIED_LABEL      Label to apply on success, or empty.
    AIPRR_COLLAPSE_PREVIOUS  `true`/`false`.
    AIPRR_TRACKING_COMMENT   `true`/`false`.
    AIPRR_STRICTNESS         `lenient` | `block-on-critical` |
                            `block-on-warning` | `block-on-any`.
    AIPRR_MAX_INLINE_COMMENTS  Integer cap.
    AIPRR_MAX_TURNS          Integer cap.
    AIPRR_PR_DESCRIPTION_MODE  `off` | `warn` | `block` | `autocomplete`.
    AIPRR_PR_DESCRIPTION_MIN_LENGTH  Integer threshold for adequacy.
    AIPRR_COMPLEXITY_LABELS_ENABLED  `true`/`false`.
    AIPRR_COMPLEXITY_LABEL_PREFIX  Label prefix, e.g. `complexity:`.
    AIPRR_REPO               `owner/name`.
    AIPRR_PR_NUMBER          PR number.
    AIPRR_HEAD_SHA           Commit SHA the review anchors to.
    AIPRR_BASE_REF           Base branch name.
    AIPRR_ACTION_PATH        Filesystem path to this action's checkout
                            (used to locate the bundled prompt).
    GITHUB_OUTPUT           Path to the workflow outputs file (set by
                            the runner, written here for action outputs).
"""

from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

ANTHROPIC_API_URL: str = "https://api.anthropic.com/v1/messages"
ANTHROPIC_VERSION: str = "2023-06-01"

GITHUB_REST_BASE: str = "https://api.github.com"
GITHUB_GRAPHQL_URL: str = "https://api.github.com/graphql"

# Provider defaults — keyed by `AIPRR_PROVIDER`. Adding a new provider means:
#   1. New entry here for the default model id (or a sentinel like "auto" for
#      agent-runner CLIs that pick their own default at invocation time).
#   2. New `Provider` or `AgentRunnerProvider` implementation below.
#   3. New branch in `build_provider()`.
DEFAULT_MODELS: dict[str, str] = {
    "anthropic": "claude-sonnet-4-6",
    # Agent-runner (CLI) providers — empty means "let the CLI pick its own
    # default at runtime" (usually the account-tier default for that vendor).
    # A `model:` input from the consumer overrides this.
    "claude-code": "auto",
    # `auto` routes through Cursor's model dispatch and is unlimited on Pro
    # plans (metered premium models like `composer-2.5` burn monthly credits).
    # docs/PROVIDERS.md recommends `auto` as the CI default; keep the default
    # aligned with that guidance. Consumers can still pin a specific model.
    "cursor": "auto",
    "codex": "gpt-5-codex",
}

DEFAULT_MAX_TURNS: int = 30
DEFAULT_MAX_INLINE_COMMENTS: int = 10
DEFAULT_BASE_REF: str = "main"

# Tool-use loop guardrails.
MAX_TOOL_OUTPUT_BYTES: int = 32_000
MAX_FILE_READ_LINES: int = 2_000
# Max matches/paths a single grep/glob call returns before truncation.
MAX_SEARCH_RESULTS: int = 200
# Cap on the seed diff embedded in the first user message (characters). Larger
# diffs are truncated with a pointer to the read_file tool.
MAX_DIFF_CHARS: int = 200_000
# Substrings (case-insensitive) that mark a tool-arg key as sensitive in
# logs. The model isn't expected to ever pass these — but if a prompt
# injection tricked it into echoing env vars, we don't want them in the
# public workflow log.
LOG_REDACT_SUBSTRINGS: tuple[str, ...] = (
    "token",
    "key",
    "secret",
    "password",
    "auth",
)
# Soft cap on conversation history. Each turn appends an assistant message +
# a user (tool_results) message; with `MAX_TOOL_OUTPUT_BYTES = 32_000` and a
# 30-turn ceiling the worst case is ~2 MB serialised, growing O(turns²) in
# token billing on every API call. When we exceed this many turn-pairs we
# drop the oldest tool-result pairs (keeping the original user message and
# the most recent K turns), since older tool results have already informed
# the model.
MAX_CONVERSATION_TURNS_RETAINED: int = 12

# Anthropic API parameters.
ANTHROPIC_MAX_TOKENS: int = 8192
# Anthropic API timeouts (seconds).
API_REQUEST_TIMEOUT: int = 600
API_RETRY_DELAYS_S: tuple[int, ...] = (2, 5, 15)

# GitHub API timeouts.
GH_REQUEST_TIMEOUT: int = 60
# Page size for GitHub connection queries (REST `per_page` and the GraphQL
# `first:` argument). 100 is GitHub's hard ceiling for both.
GH_CONNECTION_PAGE_SIZE: int = 100

# Truncation caps (characters) for text we echo into logs or comments, so a
# single large error body or payload can't flood the workflow log / a comment.
MAX_ERROR_BODY_CHARS: int = 500
MAX_422_BODY_CHARS: int = 1000
MAX_TOOL_LOG_PREVIEW_CHARS: int = 120
MAX_TRACKING_ERROR_CHARS: int = 1500

# Strictness modes.
STRICTNESS_LENIENT: str = "lenient"
STRICTNESS_BLOCK_CRITICAL: str = "block-on-critical"
STRICTNESS_BLOCK_WARNING: str = "block-on-warning"
STRICTNESS_BLOCK_ANY: str = "block-on-any"
VALID_STRICTNESS: tuple[str, ...] = (
    STRICTNESS_LENIENT,
    STRICTNESS_BLOCK_CRITICAL,
    STRICTNESS_BLOCK_WARNING,
    STRICTNESS_BLOCK_ANY,
)

# PR description review modes (v1.2.0+).
PR_DESC_MODE_OFF: str = "off"
PR_DESC_MODE_WARN: str = "warn"
PR_DESC_MODE_BLOCK: str = "block"
PR_DESC_MODE_AUTOCOMPLETE: str = "autocomplete"
PR_DESC_MODES: tuple[str, ...] = (
    PR_DESC_MODE_OFF,
    PR_DESC_MODE_WARN,
    PR_DESC_MODE_BLOCK,
    PR_DESC_MODE_AUTOCOMPLETE,
)
PR_DESC_MIN_LENGTH_DEFAULT: int = 50
PR_DESC_AUTOCOMPLETE_MARKER: str = (
    "<!-- ai-pr-reviewer-description-autocompleted -->"
)

# PR complexity labeling (v1.2.0+).
PR_COMPLEXITY_LOW: str = "low"
PR_COMPLEXITY_MEDIUM: str = "medium"
PR_COMPLEXITY_HIGH: str = "high"
PR_COMPLEXITY_LEVELS: tuple[str, ...] = (
    PR_COMPLEXITY_LOW,
    PR_COMPLEXITY_MEDIUM,
    PR_COMPLEXITY_HIGH,
)
PR_COMPLEXITY_LABEL_PREFIX_DEFAULT: str = "complexity:"

# Trigger modes (v1.2.0+).
TRIGGER_ALWAYS: str = "always"
TRIGGER_LABEL_REQUIRED: str = "label-required"
TRIGGER_LABEL_ONCE: str = "label-once"
TRIGGER_LABEL_ADDED_ONLY: str = "label-added-only"
TRIGGER_MODES: tuple[str, ...] = (
    TRIGGER_ALWAYS,
    TRIGGER_LABEL_REQUIRED,
    TRIGGER_LABEL_ONCE,
    TRIGGER_LABEL_ADDED_ONLY,
)
TRIGGER_STATE_MARKER_OPEN: str = "<!-- ai-pr-reviewer-state: "
TRIGGER_STATE_MARKER_CLOSE: str = " -->"

# Author-association gate (v1.3.0+). GitHub attaches `author_association`
# to every `pull_request` / `pull_request_target` payload; the field is
# server-computed and cannot be spoofed by the PR author, which makes it
# the primary line of defense against LLM-budget abuse on public repos
# (an attacker opens N PRs → each burns ~50–200K tokens).
#
# The canonical values are the full enum accepted by GitHub. See
# https://docs.github.com/en/graphql/reference/enums#commentauthorassociation.
VALID_AUTHOR_ASSOCIATIONS: tuple[str, ...] = (
    "OWNER",
    "MEMBER",
    "COLLABORATOR",
    "CONTRIBUTOR",
    "FIRST_TIME_CONTRIBUTOR",
    "FIRST_TIMER",
    "MANNEQUIN",
    "NONE",
)

# The default write-tier — what `action.yml`'s `author-association` input
# defaults to and what the runtime falls back to when the env var is
# unset. Any consumer who wants to allow external contributors sets the
# input explicitly (see docs/SECURITY.md § "Author-association gate").
AUTHOR_ASSOCIATION_WRITE_TIER: tuple[str, ...] = (
    "OWNER",
    "MEMBER",
    "COLLABORATOR",
)

# Severity levels — ordered low→high so `max(SEVERITY_RANK)` yields the most
# severe finding in a review.
SEVERITY_NONE: str = "none"
SEVERITY_INFO: str = "info"
SEVERITY_WARNING: str = "warning"
SEVERITY_CRITICAL: str = "critical"
SEVERITY_RANK: dict[str, int] = {
    SEVERITY_NONE: 0,
    SEVERITY_INFO: 1,
    SEVERITY_WARNING: 2,
    SEVERITY_CRITICAL: 3,
}

# Marker embedded in the tracking comment so downstream automation can find
# the most recent review unambiguously, even if other bots also comment.
REVIEW_MARKER: str = "<!-- ai-pr-reviewer-marker -->"

# Per-provider marker embedded in both the tracking comment AND the review
# body. It lets `collapse-previous` scope to "this provider's own prior
# artefacts" so several providers can review the same PR concurrently (with
# one shared GITHUB_TOKEN / bot author) without collapsing each other. See
# docs/PROVIDERS.md § "Running more than one provider on the same PR".
PROVIDER_MARKER_PREFIX: str = "<!-- ai-pr-reviewer-provider:"


def provider_marker(provider_id: str) -> str:
    """The HTML-comment marker identifying which provider produced a comment."""
    return f"{PROVIDER_MARKER_PREFIX} {provider_id} -->"

# Agent-runner findings contract (see AgentRunnerProvider docstring).
# Each CLI provider writes its findings to `<output_dir>/<FINDINGS_JSON_REL>`
# before exiting; `parse_findings_file` reads + validates that file.
FINDINGS_JSON_REL: str = ".aiprr/findings.json"
ALLOWED_SEVERITIES: tuple[str, ...] = (
    SEVERITY_CRITICAL,
    SEVERITY_WARNING,
    SEVERITY_INFO,
)
ALLOWED_SIDES: tuple[str, ...] = ("LEFT", "RIGHT")

# Timeout for a single agent-runner CLI invocation (seconds). Aligns with the
# recommended workflow `timeout-minutes: 15` in examples/*.yml.
CLI_INVOCATION_TIMEOUT: int = 900


# ---------------------------------------------------------------------------
# Logging / utilities
# ---------------------------------------------------------------------------


def log(msg: str) -> None:
    """Print a tagged log line to stdout (the workflow log)."""
    sys.stdout.write(f"[ai-pr-reviewer] {msg}\n")
    sys.stdout.flush()


def parse_bool(raw: str, *, default: bool = False) -> bool:
    """Parse a workflow-input string as a bool. Empty = default."""
    if not raw:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def redact_for_log(args: dict[str, Any]) -> dict[str, Any]:
    """Mask tool-arg values whose key looks sensitive before logging."""
    return {
        k: ("***" if any(s in k.lower() for s in LOG_REDACT_SUBSTRINGS) else v)
        for k, v in args.items()
    }


# Registry of literal secret VALUES that must never reach a public surface
# (a PR comment or review body). `redact_for_log` scrubs by key *name*; this
# scrubs by exact value. Populated once in `main()` with the provider API key
# and the GitHub token. Defense-in-depth for the agent-runner path, where a
# prompt-injected vendor CLI could echo its API key into a finding body (see
# docs/SECURITY.md § "Agent-runner providers: residual exfiltration surface").
_SECRET_VALUES: set[str] = set()
# Below this length a "secret" is too short to scrub without risking mangling
# ordinary review prose. Real API keys / tokens are far longer.
MIN_SCRUBBABLE_SECRET_LEN: int = 8


def register_secret(value: str) -> None:
    """Register a secret value for scrubbing from public-facing text."""
    if value and len(value) >= MIN_SCRUBBABLE_SECRET_LEN:
        _SECRET_VALUES.add(value)


def scrub_secrets(text: str) -> str:
    """Replace every registered secret value in `text` with `***`.

    Applied to review summaries, inline-comment bodies, and failure messages
    before they are posted to the PR, so a leaked/echoed key can't surface in
    a public comment even if the model (or a vendor CLI) was tricked into
    embedding it.
    """
    if not text:
        return text
    for secret in _SECRET_VALUES:
        if secret in text:
            text = text.replace(secret, "***")
    return text


def truncate_for_tool(text: str, *, label: str) -> str:
    """Cap tool output so a single bad command can't blow up the prompt.

    Guarantees `len(output.encode("utf-8")) <= MAX_TOOL_OUTPUT_BYTES` by
    reserving space for the truncation notice inside the byte budget.
    """
    if len(text.encode("utf-8")) <= MAX_TOOL_OUTPUT_BYTES:
        return text
    notice: str = (
        f"\n\n[output truncated at {MAX_TOOL_OUTPUT_BYTES} bytes — "
        f"narrow your {label} call (e.g. add path/glob/limit) for full content]"
    )
    body_budget: int = max(0, MAX_TOOL_OUTPUT_BYTES - len(notice.encode("utf-8")))
    truncated: str = text.encode("utf-8")[:body_budget].decode(
        "utf-8", errors="ignore"
    )
    return truncated + notice


def run_cmd(
    args: list[str], *, cwd: str | None = None, check: bool = False
) -> subprocess.CompletedProcess[str]:
    """Run a subprocess and capture its output as text."""
    return subprocess.run(
        args,
        cwd=cwd,
        check=check,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )


def write_action_output(name: str, value: str) -> None:
    """Append a key=value pair to `$GITHUB_OUTPUT` so it surfaces as an
    action output. No-op when run outside Actions (the file env var is
    unset), so the script remains directly invocable for local debugging.

    Multi-line values use the heredoc-style delimiter form documented in
    https://docs.github.com/en/actions/using-workflows/workflow-commands-for-github-actions#multiline-strings —
    we don't need it for the small scalars we emit here, but the path is
    handled defensively in case a future output carries newlines.
    """
    out_path: str | None = os.environ.get("GITHUB_OUTPUT")
    if not out_path:
        return
    with open(out_path, "a", encoding="utf-8") as fh:
        if "\n" in value:
            delim: str = "AIPRR_OUTPUT_EOF"
            fh.write(f"{name}<<{delim}\n{value}\n{delim}\n")
        else:
            fh.write(f"{name}={value}\n")


def write_all_outputs(
    *,
    skipped: bool,
    severity: str = SEVERITY_NONE,
    inline_attached: int = 0,
    inline_dropped: int = 0,
    blocked: bool = False,
    review_url: str = "",
) -> None:
    """Write the complete set of six action outputs in one call.

    Every exit path — success, skip, and hard failure — routes through here so
    downstream steps never read an empty string for an output they key on
    (e.g. `steps.review.outputs.blocked == 'false'`). Defaults describe the
    "no review produced" state used by the skip and failure paths.
    """
    write_action_output("skipped", "true" if skipped else "false")
    write_action_output("severity", severity)
    write_action_output("inline-attached", str(inline_attached))
    write_action_output("inline-dropped", str(inline_dropped))
    write_action_output("blocked", "true" if blocked else "false")
    write_action_output("review-url", review_url)


# ---------------------------------------------------------------------------
# GitHub API helpers
# ---------------------------------------------------------------------------


def gh_request(
    method: str,
    path: str,
    *,
    token: str,
    body: dict[str, Any] | None = None,
) -> Any:
    """Call the GitHub REST API and return the parsed JSON response.

    Return type is `Any` rather than `dict[str, Any]` because GitHub's REST
    API legitimately returns both objects (e.g. `/pulls/{n}`) and arrays
    (e.g. `/pulls/{n}/files`) depending on the endpoint. Callers narrow the
    type at the call site.
    """
    url: str = f"{GITHUB_REST_BASE}{path}"
    data: bytes | None = (
        json.dumps(body).encode("utf-8") if body is not None else None
    )
    headers: dict[str, str] = {
        "Accept": "application/vnd.github+json",
        "Authorization": f"Bearer {token}",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "ai-pr-reviewer",
    }
    if data is not None:
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(
        url, data=data, headers=headers, method=method
    )
    with urllib.request.urlopen(request, timeout=GH_REQUEST_TIMEOUT) as response:
        raw: bytes = response.read()
        if not raw:
            return {}
        return json.loads(raw)


def gh_graphql(query: str, variables: dict[str, Any], *, token: str) -> Any:
    """POST a GraphQL query to GitHub and return the parsed `data` payload."""
    body: bytes = json.dumps({"query": query, "variables": variables}).encode(
        "utf-8"
    )
    headers: dict[str, str] = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "User-Agent": "ai-pr-reviewer",
    }
    request = urllib.request.Request(
        GITHUB_GRAPHQL_URL, data=body, headers=headers, method="POST"
    )
    with urllib.request.urlopen(request, timeout=GH_REQUEST_TIMEOUT) as response:
        raw: bytes = response.read()
    payload: dict[str, Any] = json.loads(raw)
    if payload.get("errors"):
        raise RuntimeError(f"GitHub GraphQL errors: {payload['errors']}")
    return payload.get("data", {})


DEFAULT_WORKFLOW_BOT_LOGIN: str = "github-actions[bot]"


def gh_get_authenticated_login(
    token: str, *, repo: str = "", pr_number: int = 0
) -> str:
    """Return the login the token authenticates as, with a 4-tier fallback.

    The naive `GET /user` call fails with `HTTP 403 Forbidden` when the
    caller is the built-in workflow `GITHUB_TOKEN` (an installation
    token, not a user token) — the well-known limitation that silently
    broke `collapse-previous` for every consumer using the recommended
    `github-token: ${{ secrets.GITHUB_TOKEN }}` pattern.

    The fallback chain, tried in order:

    1. `GET /user` — works for PATs and user OAuth tokens.
    2. `GET /app` — works for GitHub App installation tokens; returns
       `<slug>[bot]` (the shape GitHub uses in comment `.user.login`).
    3. Marker-scan the PR's issue comments for our
       `<!-- ai-pr-reviewer-marker -->` tracking comment; take that
       comment's `.user.login`. Works for any bot that previously
       posted here. Requires `repo` + `pr_number`.
    4. Hardcoded `"github-actions[bot]"` — the login of the built-in
       workflow `GITHUB_TOKEN`, which is the overwhelmingly common
       case in the wild.

    Failing all four tiers still returns tier 4's default, so callers
    downstream (`gh_collapse_previous_reviews`) get a login they can
    filter on. Their own error handling covers the case where the
    token is genuinely invalid.
    """
    try:
        me: dict[str, Any] = gh_request("GET", "/user", token=token)
        login: str = str(me.get("login", "") or "")
        if login:
            return login
    except Exception as e:  # noqa: BLE001 — best-effort; try next tier
        log(f"gh_get_authenticated_login: /user tier failed: {e}")

    try:
        app: dict[str, Any] = gh_request("GET", "/app", token=token)
        slug: str = str(app.get("slug", "") or "")
        if slug:
            return f"{slug}[bot]"
    except Exception as e:  # noqa: BLE001 — best-effort; try next tier
        log(f"gh_get_authenticated_login: /app tier failed: {e}")

    if repo and pr_number:
        try:
            owner, name = repo.split("/", 1)
            comments: list[dict[str, Any]] = gh_request(
                "GET",
                (
                    f"/repos/{owner}/{name}/issues/{pr_number}"
                    "/comments?per_page=100"
                ),
                token=token,
            )
            if isinstance(comments, list):
                for comment in reversed(comments):
                    if not isinstance(comment, dict):
                        continue
                    body: str = str(comment.get("body") or "")
                    if REVIEW_MARKER not in body:
                        continue
                    author: str = str(
                        (comment.get("user") or {}).get("login") or ""
                    )
                    if author:
                        return author
        except Exception as e:  # noqa: BLE001 — best-effort; fall through
            log(
                f"gh_get_authenticated_login: marker-scan tier failed: {e}"
            )

    return DEFAULT_WORKFLOW_BOT_LOGIN


def gh_post_issue_comment(
    *, token: str, repo: str, pr_number: int, body: str
) -> int:
    """Post a regular issue comment on the PR; return the new comment id."""
    owner, name = repo.split("/", 1)
    resp: Any = gh_request(
        "POST",
        f"/repos/{owner}/{name}/issues/{pr_number}/comments",
        token=token,
        body={"body": body},
    )
    return int(resp.get("id", 0)) if isinstance(resp, dict) else 0


def gh_update_issue_comment(
    *, token: str, repo: str, comment_id: int, body: str
) -> None:
    """Replace the body of an existing issue comment."""
    if comment_id <= 0:
        return
    owner, name = repo.split("/", 1)
    try:
        gh_request(
            "PATCH",
            f"/repos/{owner}/{name}/issues/comments/{comment_id}",
            token=token,
            body={"body": body},
        )
    except Exception as e:  # noqa: BLE001 — best-effort; do not crash the run
        log(f"Failed to update issue comment {comment_id}: {e}")


def gh_apply_label(
    *, token: str, repo: str, pr_number: int, label: str
) -> None:
    """Apply a single label to a PR. Creates the label on the fly if needed."""
    if not label:
        return
    owner, name = repo.split("/", 1)
    try:
        gh_request(
            "POST",
            f"/repos/{owner}/{name}/issues/{pr_number}/labels",
            token=token,
            body={"labels": [label]},
        )
    except urllib.error.HTTPError as e:
        # 422 here usually means the label doesn't exist yet — try to create
        # it then re-apply. Any other error is logged but non-fatal.
        if e.code == 422:
            try:
                gh_request(
                    "POST",
                    f"/repos/{owner}/{name}/labels",
                    token=token,
                    body={"name": label, "color": "0e8a16"},
                )
                gh_request(
                    "POST",
                    f"/repos/{owner}/{name}/issues/{pr_number}/labels",
                    token=token,
                    body={"labels": [label]},
                )
            except Exception as e2:  # noqa: BLE001
                log(f"Failed to create+apply label {label!r}: {e2}")
        else:
            log(f"Failed to apply label {label!r}: {e}")
    except Exception as e:  # noqa: BLE001
        log(f"Failed to apply label {label!r}: {e}")


def gh_pr_has_label(
    *, token: str, repo: str, pr_number: int, label: str
) -> bool:
    """Return True if the PR currently has the given label."""
    owner, name = repo.split("/", 1)
    pr: dict[str, Any] = gh_request(
        "GET", f"/repos/{owner}/{name}/pulls/{pr_number}", token=token
    )
    labels: list[dict[str, Any]] = pr.get("labels", []) or []
    return any((lbl.get("name") or "") == label for lbl in labels)


def gh_remove_labels_by_prefix(
    *,
    token: str,
    repo: str,
    pr_number: int,
    prefix: str,
    except_label: str = "",
) -> int:
    """Remove all PR labels starting with `prefix`, except `except_label`.

    Returns the number of labels removed. Best-effort — callers wrap in
    try/except so label bookkeeping failures do not crash the review.
    """
    if not prefix:
        return 0
    owner, name = repo.split("/", 1)
    pr: dict[str, Any] = gh_request(
        "GET", f"/repos/{owner}/{name}/pulls/{pr_number}", token=token
    )
    labels: list[dict[str, Any]] = pr.get("labels", []) or []
    removed: int = 0
    for lbl in labels:
        lbl_name: str = (lbl.get("name") or "").strip()
        if not lbl_name.startswith(prefix):
            continue
        if except_label and lbl_name == except_label:
            continue
        try:
            gh_request(
                "DELETE",
                (
                    f"/repos/{owner}/{name}/issues/{pr_number}/labels/"
                    f"{urllib.parse.quote(lbl_name)}"
                ),
                token=token,
            )
            removed += 1
        except Exception as e:  # noqa: BLE001 — best-effort GH API call
            log(f"Could not remove label {lbl_name!r}: {e}")
    return removed


def gh_collapse_previous_reviews(
    *,
    token: str,
    repo: str,
    pr_number: int,
    bot_login: str,
    provider_marker_text: str = "",
) -> int:
    """Mark prior bot reviews/comments as `OUTDATED` via GraphQL.

    Returns the number of nodes minimized. Best-effort: failures are logged
    but the review still proceeds.

    When `provider_marker_text` is non-empty, collapsing is **scoped to this
    provider**: only bot-authored comments/reviews whose body contains that
    marker are minimized. This lets several providers review the same PR
    concurrently (sharing one bot author) without collapsing each other. When
    it is empty, the legacy behaviour applies — every non-minimized comment/
    review by `bot_login` is collapsed.

    `GH_CONNECTION_PAGE_SIZE` (100) is GitHub's hard limit on the `comments`
    and `reviews` connections of a PullRequest. If a PR ever exceeds that many
    non-minimized bot artefacts, switch to cursor pagination rather than
    raising the cap.
    """
    owner, name = repo.split("/", 1)
    page: int = GH_CONNECTION_PAGE_SIZE
    query: str = (
        "query($owner:String!, $repo:String!, $number:Int!, $page:Int!) {"
        "  repository(owner:$owner, name:$repo) {"
        "    pullRequest(number:$number) {"
        "      comments(first:$page) {"
        "        nodes { id isMinimized body author { login } }"
        "      }"
        "      reviews(first:$page) {"
        "        nodes {"
        "          id"
        "          isMinimized"
        "          body"
        "          author { login }"
        "          comments(first:$page) { nodes { id isMinimized } }"
        "        }"
        "      }"
        "    }"
        "  }"
        "}"
    )
    try:
        data: Any = gh_graphql(
            query,
            {"owner": owner, "repo": name, "number": pr_number, "page": page},
            token=token,
        )
    except Exception as e:  # noqa: BLE001
        log(f"Could not list PR comments/reviews for collapsing: {e}")
        return 0

    pr: dict[str, Any] = (
        (data or {}).get("repository", {}) or {}
    ).get("pullRequest", {}) or {}
    issue_comments: list[dict[str, Any]] = (
        pr.get("comments", {}) or {}
    ).get("nodes", []) or []
    reviews: list[dict[str, Any]] = (
        pr.get("reviews", {}) or {}
    ).get("nodes", []) or []

    # GraphQL and REST disagree on the shape of a Bot's login. REST
    # `.user.login` returns `"github-actions[bot]"` (the shape the
    # /user endpoint, comment payloads, and our marker-scan tier all
    # use), but GraphQL `.author.login` on a Bot node returns
    # `"github-actions"` — no `[bot]` suffix. Comparing directly missed
    # every bot node and silently reported "Collapsed 0/N". Accept
    # both shapes so the filter matches regardless of where
    # `bot_login` came from.
    accepted_logins: set[str] = {bot_login}
    if bot_login.endswith("[bot]"):
        accepted_logins.add(bot_login[: -len("[bot]")])

    def _matches(author_login: str) -> bool:
        return author_login in accepted_logins

    def _in_scope(body: str) -> bool:
        """Provider scoping: in scoped mode (`provider_marker_text` set),
        only artefacts carrying this provider's marker are in scope. In
        legacy mode (empty), every bot-authored artefact is in scope."""
        if not provider_marker_text:
            return True
        return provider_marker_text in (body or "")

    targets: list[str] = []
    for c in issue_comments:
        author_login: str = str((c.get("author") or {}).get("login") or "")
        if (
            _matches(author_login)
            and not c.get("isMinimized", False)
            and _in_scope(str(c.get("body") or ""))
        ):
            targets.append(c["id"])
    for r in reviews:
        author_login = str((r.get("author") or {}).get("login") or "")
        if _matches(author_login) and _in_scope(str(r.get("body") or "")):
            if not r.get("isMinimized", False):
                targets.append(r["id"])
            inline: list[dict[str, Any]] = (
                r.get("comments", {}) or {}
            ).get("nodes", []) or []
            for ic in inline:
                if not ic.get("isMinimized", False):
                    targets.append(ic["id"])

    minimize_mutation: str = (
        "mutation($id:ID!) {"
        "  minimizeComment(input:{subjectId:$id, classifier:OUTDATED}) {"
        "    minimizedComment { isMinimized }"
        "  }"
        "}"
    )
    minimized: int = 0
    for node_id in targets:
        try:
            gh_graphql(minimize_mutation, {"id": node_id}, token=token)
            minimized += 1
        except Exception as e:  # noqa: BLE001
            log(f"  could not minimize {node_id}: {e}")
    log(f"Collapsed {minimized}/{len(targets)} previous bot artefact(s)")
    return minimized


def gh_submit_review(
    *,
    token: str,
    repo: str,
    pr_number: int,
    head_sha: str,
    body: str,
    inline_comments: list[dict[str, Any]],
) -> dict[str, Any]:
    """Submit a single PR review with the summary body + batched inline comments."""
    owner, name = repo.split("/", 1)
    payload: dict[str, Any] = {
        "commit_id": head_sha,
        "body": body,
        "event": "COMMENT",
        # The Reviews API accepts inline comments inline. The schema differs
        # from `pulls/{n}/comments`: here you pass `path`, `body`, `line`,
        # `side`, optionally `start_line`/`start_side` for multi-line.
        "comments": inline_comments,
    }
    return gh_request(
        "POST",
        f"/repos/{owner}/{name}/pulls/{pr_number}/reviews",
        token=token,
        body=payload,
    )


def gh_submit_review_with_fallback(
    *,
    token: str,
    repo: str,
    pr_number: int,
    head_sha: str,
    result: "ReviewResult",
) -> tuple[dict[str, Any], int]:
    """Submit the review; on a 422, retry summary-only and report the drop.

    Consumes a provider-independent `ReviewResult`. Encodes findings into the
    GitHub Reviews API inline shape at the boundary so agent-runner providers
    can hand back a `ReviewResult` without knowing the GitHub API schema.

    Returns `(review, dropped_count)`. A 422 from `POST /pulls/{n}/reviews`
    rejects the entire request when any single inline comment points at a
    line outside the PR's diff hunks (off-by-one from the model, file moved,
    multi-line range crossing a hunk boundary, etc.). Without this fallback,
    a single bad line loses the summary and every other queued comment.
    With it we drop the inline comments and post summary-only — the original
    422 body is logged so an operator can see which comment was rejected.
    """
    inline_comments: list[dict[str, Any]] = findings_to_gh_inline_comments(
        result.findings
    )
    try:
        review: dict[str, Any] = gh_submit_review(
            token=token,
            repo=repo,
            pr_number=pr_number,
            head_sha=head_sha,
            body=result.summary,
            inline_comments=inline_comments,
        )
        return review, 0
    except urllib.error.HTTPError as e:
        if e.code != 422 or not inline_comments:
            raise
        err_body: str = e.read().decode("utf-8", errors="replace")
        log(
            "GitHub rejected the review with HTTP 422 — most likely an inline "
            f"comment referenced a line outside the diff. Retrying with "
            f"summary-only ({len(inline_comments)} inline comment(s) will be "
            f"dropped). Error body: {err_body[:MAX_422_BODY_CHARS]}"
        )
        review = gh_submit_review(
            token=token,
            repo=repo,
            pr_number=pr_number,
            head_sha=head_sha,
            body=result.summary,
            inline_comments=[],
        )
        return review, len(inline_comments)


# ---------------------------------------------------------------------------
# Provider abstraction
# ---------------------------------------------------------------------------


class Provider:
    """Minimal interface every LLM provider must implement.

    The action treats the provider as a black box that takes the same
    Anthropic-shaped payload (system prompt, message history, tools) and
    returns the same Anthropic-shaped response (`stop_reason`, `content`
    blocks of `text` / `tool_use`). When we add OpenAI/Gemini we'll
    translate at the provider boundary so the rest of the code is
    unchanged.
    """

    def complete(
        self,
        *,
        system_prompt: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
    ) -> dict[str, Any]:
        raise NotImplementedError


class AnthropicProvider(Provider):
    """Anthropic Messages API client with prompt caching + bounded retries."""

    def __init__(self, *, api_key: str, model: str) -> None:
        self.api_key: str = api_key
        self.model: str = model

    def complete(
        self,
        *,
        system_prompt: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
    ) -> dict[str, Any]:
        body: bytes = json.dumps(
            {
                "model": self.model,
                "max_tokens": ANTHROPIC_MAX_TOKENS,
                # Cache the system prompt — it's stable across the loop's
                # many iterations and is by far the largest static input.
                "system": [
                    {
                        "type": "text",
                        "text": system_prompt,
                        "cache_control": {"type": "ephemeral"},
                    }
                ],
                "messages": messages,
                "tools": tools,
            }
        ).encode("utf-8")
        headers: dict[str, str] = {
            "Content-Type": "application/json",
            "x-api-key": self.api_key,
            "anthropic-version": ANTHROPIC_VERSION,
        }
        last_error: Exception | None = None
        for attempt, delay in enumerate((0,) + API_RETRY_DELAYS_S):
            if delay:
                log(f"Anthropic retry attempt {attempt} after {delay}s")
                time.sleep(delay)
            request = urllib.request.Request(
                ANTHROPIC_API_URL, data=body, headers=headers, method="POST"
            )
            try:
                with urllib.request.urlopen(
                    request, timeout=API_REQUEST_TIMEOUT
                ) as response:
                    return json.loads(response.read())
            except urllib.error.HTTPError as e:
                err_body: str = e.read().decode("utf-8", errors="replace")
                last_error = RuntimeError(
                    f"Anthropic API HTTP {e.code}: "
                    f"{err_body[:MAX_ERROR_BODY_CHARS]}"
                )
                if e.code != 429 and not (500 <= e.code < 600):
                    raise last_error
            except (urllib.error.URLError, TimeoutError) as e:
                last_error = RuntimeError(f"Anthropic API network error: {e}")
        assert last_error is not None
        raise last_error


class AgentRunnerProvider:
    """Provider that delegates the full review to a vendor's coding-agent CLI.

    Unlike `Provider` (chat-completions family — this action owns the tool-use
    loop), an `AgentRunnerProvider` hands off the entire agentic loop to the
    vendor CLI running in headless mode and receives structured findings via a
    file-based contract (`.aiprr/findings.json` — see `parse_findings_file`).

    Concrete implementations (`ClaudeCodeProvider`, `CursorProvider`,
    `CodexProvider`) live below this class.
    """

    def install(self) -> None:
        """Sanity-check that the CLI is on PATH.

        The composite action installs the CLI in a preceding step; this
        method is a defensive verification, not the install itself.
        """
        raise NotImplementedError

    def run_review(
        self,
        *,
        pr_context: PRContext,
        review_instructions: str,
        workspace: Path,
        output_dir: Path,
    ) -> ReviewResult:
        """Invoke the vendor CLI headless; return a ReviewResult."""
        raise NotImplementedError


def _swap_mcp_config(
    src_file: str, dest_path: Path
) -> tuple[Path | None, str | None]:
    """Copy an MCP config to a CLI's expected location, backing up the previous.

    Returns `(dest_path_or_None, backup_content_or_None)` so the caller can
    restore/delete on exit. If `src_file` is empty, both return values are
    `None` — a no-op that the finally block can safely handle.
    """
    if not src_file:
        return None, None
    dest_path.parent.mkdir(parents=True, exist_ok=True)
    backup: str | None = None
    if dest_path.exists():
        backup = dest_path.read_text(encoding="utf-8")
    shutil.copyfile(src_file, dest_path)
    return dest_path, backup


def _restore_mcp_config(dest_path: Path | None, backup: str | None) -> None:
    """Restore or delete the MCP config after a CLI invocation."""
    if dest_path is None:
        return
    if backup is not None:
        dest_path.write_text(backup, encoding="utf-8")
    else:
        dest_path.unlink(missing_ok=True)


# Environment variables the vendor CLIs need to function on ubuntu-latest.
# Everything else (notably AIPRR_GH_TOKEN and every other AIPRR_* secret)
# stays in the parent process. See docs/SECURITY.md and Security Review §2.
_CLI_ENV_ALLOWLIST: tuple[str, ...] = (
    "PATH",
    "HOME",
    "USER",
    "LOGNAME",
    "LANG",
    "LC_ALL",
    "LC_CTYPE",
    "TZ",
    "TERM",
    "SHELL",
    # Node.js CLIs (@anthropic-ai/claude-code, @openai/codex).
    "NODE_PATH",
    "NPM_CONFIG_PREFIX",
    "NODE_OPTIONS",
    # GitHub Actions runner metadata (harmless; useful for debug output).
    "RUNNER_OS",
    "RUNNER_ARCH",
    "GITHUB_ACTIONS",
    "CI",
    # Outbound-proxy configuration — a CLI on a corporate / self-hosted
    # runner behind a proxy can't reach its vendor API without these. They
    # are non-secret network config, not credentials.
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "NO_PROXY",
    "http_proxy",
    "https_proxy",
    "no_proxy",
    # Custom / self-hosted API endpoints for the vendor CLIs (e.g. an
    # Anthropic- or OpenAI-compatible gateway). Non-secret base URLs.
    "ANTHROPIC_BASE_URL",
    "OPENAI_BASE_URL",
)


def _build_cli_env(
    *, extra_vars: dict[str, str]
) -> dict[str, str]:
    """Build a scrubbed environment for a vendor-CLI subprocess.

    Forwards only variables the CLI likely needs to function (PATH,
    HOME, locale, Node.js paths). Adds `extra_vars` on top (typically
    the vendor-specific API key). Everything else — notably the
    consumer's GitHub token and any other secrets in the workflow's
    env: block — stays in the parent process.
    """
    scrubbed: dict[str, str] = {}
    for name in _CLI_ENV_ALLOWLIST:
        val: str | None = os.environ.get(name)
        if val is not None:
            scrubbed[name] = val
    scrubbed.update(extra_vars)
    return scrubbed


def _invoke_cli_agent(
    *,
    argv: list[str],
    workspace: Path,
    findings_path: Path,
    env: dict[str, str],
    cli_name: str,
    stdin_input: str | None = None,
) -> ReviewResult:
    """Run a CLI agent subprocess and parse its findings.json output.

    Common to all AgentRunnerProvider implementations. Enforces:
      - Argv-list form (no `shell=True`) — see docs/SECURITY.md.
      - Hard timeout via CLI_INVOCATION_TIMEOUT.
      - Structured error on non-zero exit with truncated stderr.
      - Delegation to parse_findings_file() for output validation.

    `stdin_input`, when provided, is piped to the subprocess' stdin. Providers
    that hit the OS ARG_MAX limit (Linux E2BIG on argv > ~128 KB) pass their
    large prompt this way instead of via a positional CLI argument.
    """
    log(f"Invoking {cli_name}: {' '.join(shlex.quote(a) for a in argv[:2])} …")
    try:
        result = subprocess.run(
            argv,
            cwd=str(workspace),
            env=env,
            timeout=CLI_INVOCATION_TIMEOUT,
            check=False,
            capture_output=True,
            text=True,
            input=stdin_input,
        )
    except subprocess.TimeoutExpired as e:
        raise RuntimeError(
            f"{cli_name} CLI exceeded the timeout of "
            f"{CLI_INVOCATION_TIMEOUT}s. Consider lowering `agent-max-turns` "
            f"or narrowing the PR scope."
        ) from e

    if result.returncode != 0:
        stderr_tail: str = (result.stderr or "")[-MAX_ERROR_BODY_CHARS:]
        stdout_tail: str = (result.stdout or "")[-MAX_ERROR_BODY_CHARS:]
        raise RuntimeError(
            f"{cli_name} CLI exited with code {result.returncode}. "
            f"stderr tail: {stderr_tail!r}. stdout tail: {stdout_tail!r}."
        )

    return parse_findings_file(
        findings_path, allow_malformed_summary_fallback=True
    )


class ClaudeCodeProvider(AgentRunnerProvider):
    """Claude Code CLI (headless) as an agent-runner provider.

    Auth: `ANTHROPIC_API_KEY` env var (from the consumer's `api-key` input).
    CLI: `@anthropic-ai/claude-code` on npm. Installed by the composite step
    in `action.yml` when `provider: claude-code`.
    """

    CLI_NAME: str = "Claude Code"
    CLI_BIN: str = "claude"
    MCP_DEST: Path = Path.home() / ".claude" / "mcp.json"

    def __init__(
        self,
        *,
        api_key: str,
        model: str,
        extra_args: str = "",
        mcp_config_file: str = "",
    ) -> None:
        self.api_key: str = api_key
        self.model: str = model
        self.extra_args: str = extra_args
        self.mcp_config_file: str = mcp_config_file

    def install(self) -> None:
        result = run_cmd([self.CLI_BIN, "--version"])
        if result.returncode != 0:
            raise RuntimeError(
                f"{self.CLI_NAME} CLI not found on PATH. The composite step "
                "should install `@anthropic-ai/claude-code` before invoking "
                "reviewer.py."
            )

    def run_review(
        self,
        *,
        pr_context: PRContext,
        review_instructions: str,
        workspace: Path,
        output_dir: Path,
    ) -> ReviewResult:
        findings_path: Path = output_dir / FINDINGS_JSON_REL
        findings_path.parent.mkdir(parents=True, exist_ok=True)

        # The review instructions + findings-contract directive go into the
        # system prompt as LITERAL TEXT via `--append-system-prompt <text>`.
        # (The flag takes a prompt string, not a path — passing a path would
        # deliver the literal filename to the model and the rubric/contract
        # would never arrive.) The instructions are a few KB, well under the
        # per-argv byte limit; only the diff-carrying user prompt is large,
        # and that goes via stdin below.
        enriched_instructions: str = write_findings_prompt_directive(
            review_instructions, findings_path
        )

        mcp_dest, mcp_backup = _swap_mcp_config(
            self.mcp_config_file, self.MCP_DEST
        )
        try:
            # User prompt (PR metadata + full diff) is piped via stdin, not
            # argv: the diff can exceed the OS single-argument limit (~128 KB
            # E2BIG on Linux). `claude -p` reads the prompt from stdin when no
            # positional prompt is given.
            user_prompt: str = render_user_prompt(
                pr_context, for_agent_runner=True
            )
            argv: list[str] = [
                self.CLI_BIN,
                "-p",
                "--append-system-prompt",
                enriched_instructions,
                "--output-format",
                "stream-json",
                "--verbose",
                # Headless CI: the runner is already an isolated, ephemeral
                # sandbox, so bypass the interactive permission gate that
                # would otherwise block the Write tool (used to emit
                # findings.json) in non-interactive mode. Mirrors Cursor's
                # `--force --trust`. Consumers can override via
                # `agent-extra-args`.
                "--permission-mode",
                "bypassPermissions",
            ]
            # Claude Code only loads MCP servers from an explicit
            # `--mcp-config <file>` (or project `.mcp.json`) — a bare copy to
            # ~/.claude/mcp.json is NOT read. Point the flag at the consumer's
            # file directly so the passthrough actually takes effect.
            if self.mcp_config_file:
                argv += ["--mcp-config", self.mcp_config_file]
            if self.model and self.model != "auto":
                argv += ["--model", self.model]
            if self.extra_args:
                argv += shlex.split(self.extra_args)

            env: dict[str, str] = _build_cli_env(
                extra_vars={"ANTHROPIC_API_KEY": self.api_key}
            )
            return _invoke_cli_agent(
                argv=argv,
                workspace=workspace,
                findings_path=findings_path,
                env=env,
                cli_name=self.CLI_NAME,
                stdin_input=user_prompt,
            )
        finally:
            _restore_mcp_config(mcp_dest, mcp_backup)


class CursorProvider(AgentRunnerProvider):
    """Cursor Agent CLI (headless, local runtime) as an agent-runner provider.

    Auth: `CURSOR_API_KEY` env var (from the consumer's `api-key` input). The
    key must belong to a Cursor Pro/Pro+/Ultra subscription — usage credits
    are debited from that subscription (there is no BYOK). Consumers on the
    Pro plan can select `model: auto` to route through Cursor's dispatch
    layer and avoid burning monthly credits on premium models.

    CLI: `cursor-agent` — installed via `curl -fsSL https://cursor.com/install
    | bash` by the composite step.

    Headless defaults (v1.2.0+): the invocation always passes `--force` and
    `--trust`, which are what Cursor's own headless CLI docs recommend for
    CI (they prevent interactive approval prompts that would otherwise stall
    the run). When `mcp_config_file` is set, `--approve-mcps` is added so
    the MCP approval prompt is also non-interactive. Consumers can still
    override any of this via `agent-extra-args`.

    Local runtime only for v1.1.0+ (no `/v1/agents` cloud REST path). The
    CLI operates against `workspace` directly.
    """

    CLI_NAME: str = "Cursor Agent"
    CLI_BIN: str = "cursor-agent"
    MCP_DEST: Path = Path.home() / ".cursor" / "mcp.json"

    def __init__(
        self,
        *,
        api_key: str,
        model: str,
        extra_args: str = "",
        mcp_config_file: str = "",
    ) -> None:
        self.api_key: str = api_key
        self.model: str = model
        self.extra_args: str = extra_args
        self.mcp_config_file: str = mcp_config_file

    def install(self) -> None:
        result = run_cmd([self.CLI_BIN, "--version"])
        if result.returncode != 0:
            raise RuntimeError(
                f"{self.CLI_NAME} CLI not found on PATH. The composite step "
                "should install cursor-agent before invoking reviewer.py."
            )

    def run_review(
        self,
        *,
        pr_context: PRContext,
        review_instructions: str,
        workspace: Path,
        output_dir: Path,
    ) -> ReviewResult:
        findings_path: Path = output_dir / FINDINGS_JSON_REL
        findings_path.parent.mkdir(parents=True, exist_ok=True)

        # Cursor Agent CLI does not expose a separate --append-system-prompt;
        # we inline our review instructions as the front of the user prompt.
        # The vendor's own code-tuned baseline system prompt still applies.
        enriched_instructions: str = write_findings_prompt_directive(
            review_instructions, findings_path
        )
        user_prompt: str = (
            enriched_instructions
            + "\n\n---\n\n"
            + render_user_prompt(pr_context, for_agent_runner=True)
        )

        mcp_dest, mcp_backup = _swap_mcp_config(
            self.mcp_config_file, self.MCP_DEST
        )
        try:
            # Cursor CLI reads the prompt from stdin when `-p` is passed
            # without a positional argument. This avoids the E2BIG kernel
            # limit (~128 KB on Linux) which the argv path hits on large
            # PRs where the diff alone can exceed 200 KB.
            argv: list[str] = [
                self.CLI_BIN,
                "-p",
                "--output-format",
                "text",
                # Headless-CI defaults per Cursor's own documentation:
                # `--force` skips interactive tool approvals, `--trust` marks
                # the workspace as trusted for the run. Without these the
                # CLI can stall on approval prompts.
                "--force",
                "--trust",
            ]
            if self.model:
                argv += ["--model", self.model]
            if self.mcp_config_file:
                # Only relevant when an MCP config was injected; suppresses
                # the interactive "approve this MCP server" prompt.
                argv.append("--approve-mcps")
            if self.extra_args:
                argv += shlex.split(self.extra_args)

            env: dict[str, str] = _build_cli_env(
                extra_vars={"CURSOR_API_KEY": self.api_key}
            )
            return _invoke_cli_agent(
                argv=argv,
                workspace=workspace,
                findings_path=findings_path,
                env=env,
                cli_name=self.CLI_NAME,
                stdin_input=user_prompt,
            )
        finally:
            _restore_mcp_config(mcp_dest, mcp_backup)


class CodexProvider(AgentRunnerProvider):
    """OpenAI Codex CLI (headless) as an agent-runner provider.

    Auth (Codex CLI 0.122+): Codex **no longer reads** `OPENAI_API_KEY`
    from the environment. It now reads credentials only from
    `$CODEX_HOME/auth.json`. Without that file (or with a ChatGPT-mode
    file present from a prior `codex login`), `codex exec` fails with:

        401 Unauthorized: Missing bearer or basic authentication in header,
        url: https://api.openai.com/v1/responses

    We materialize an apikey-mode `auth.json` in an isolated per-run
    `CODEX_HOME` (a `tempfile.mkdtemp()`-managed directory) before each
    invocation and remove it in a `finally` block. Doing this in an
    isolated home rather than `~/.codex/` means:
      - Self-hosted runners with a persistent `~/.codex/` (e.g. from a
        prior `codex login` in ChatGPT mode) don't override our apikey
        auth for this run.
      - We never clobber a user's real credentials on any runner.
      - Cleanup is fire-and-forget — `shutil.rmtree()` removes the whole
        temp directory, no per-file backup/restore dance.

    We also still forward `OPENAI_API_KEY` for back-compat with older
    Codex versions that read it from env (cost: zero).

    CLI: `@openai/codex` on npm. Installed by the composite step when
    `provider: codex`.
    """

    CLI_NAME: str = "OpenAI Codex"
    CLI_BIN: str = "codex"
    MCP_DEST: Path = Path.home() / ".codex" / "mcp.json"
    # apikey-mode auth.json shape — validated against Codex CLI 0.122+
    # via the paperclipai/paperclip#5276 fix and the shell one-liner
    # `echo '{"OPENAI_API_KEY": "..."}' > $CODEX_HOME/auth.json`
    # that is documented in the wjduenow/clauditor#177 workaround.
    AUTH_JSON_FILENAME: str = "auth.json"

    @staticmethod
    def _materialize_apikey_auth_json(
        *, codex_home: Path, api_key: str
    ) -> None:
        """Write an apikey-mode auth.json under `codex_home`.

        Sets mode `0o600` on the file so a shared runner cannot read it
        from another process. Fails loudly on OSError — a missing
        auth.json is exactly the bug we are here to prevent.
        """
        codex_home.mkdir(mode=0o700, parents=True, exist_ok=True)
        auth_path: Path = codex_home / CodexProvider.AUTH_JSON_FILENAME
        auth_path.write_text(
            json.dumps({"OPENAI_API_KEY": api_key}),
            encoding="utf-8",
        )
        try:
            os.chmod(auth_path, 0o600)
        except OSError as e:
            log(
                f"WARNING: could not chmod 0600 on Codex auth.json at "
                f"{auth_path}: {e}. Continuing — the temp CODEX_HOME "
                f"parent directory is already 0700."
            )

    def __init__(
        self,
        *,
        api_key: str,
        model: str,
        extra_args: str = "",
        mcp_config_file: str = "",
    ) -> None:
        self.api_key: str = api_key
        self.model: str = model
        self.extra_args: str = extra_args
        self.mcp_config_file: str = mcp_config_file

    def install(self) -> None:
        result = run_cmd([self.CLI_BIN, "--version"])
        if result.returncode != 0:
            raise RuntimeError(
                f"{self.CLI_NAME} CLI not found on PATH. The composite step "
                "should install `@openai/codex` before invoking reviewer.py."
            )

    def run_review(
        self,
        *,
        pr_context: PRContext,
        review_instructions: str,
        workspace: Path,
        output_dir: Path,
    ) -> ReviewResult:
        findings_path: Path = output_dir / FINDINGS_JSON_REL
        findings_path.parent.mkdir(parents=True, exist_ok=True)

        enriched_instructions: str = write_findings_prompt_directive(
            review_instructions, findings_path
        )
        user_prompt: str = (
            enriched_instructions
            + "\n\n---\n\n"
            + render_user_prompt(pr_context, for_agent_runner=True)
        )

        if self.mcp_config_file:
            # Codex configures MCP servers via `~/.codex/config.toml`
            # ([mcp_servers] TOML), NOT a JSON file — the copied mcp.json is
            # ignored. Warn loudly rather than silently no-op so the consumer
            # knows the passthrough didn't take effect. See docs/PROVIDERS.md.
            log(
                "WARNING: mcp-config-file is set but Codex does not read a JSON "
                "MCP config (it uses ~/.codex/config.toml). The MCP passthrough "
                "will NOT take effect for provider=codex. Configure MCP via "
                "agent-extra-args (`-c mcp_servers...`) or a preconfigured "
                "config.toml instead."
            )

        # Isolated per-run CODEX_HOME with an apikey-mode auth.json —
        # see the class docstring for the 0.122+ auth breakage rationale.
        # `mkdtemp` creates a private dir with mode 0700 by default.
        codex_home: Path = Path(tempfile.mkdtemp(prefix="aiprr-codex-"))
        try:
            self._materialize_apikey_auth_json(
                codex_home=codex_home, api_key=self.api_key
            )

            # Do not copy `mcp-config-file` to `~/.codex/mcp.json`: Codex
            # ignores that JSON file, and this run uses an isolated CODEX_HOME
            # anyway. The warning above points users at the supported
            # `config.toml` / `agent-extra-args` path.
            # Codex CLI headless is `codex exec`. Two CI-critical flags:
            #   --dangerously-bypass-approvals-and-sandbox: `codex exec`
            #     defaults to a READ-ONLY sandbox, so without this the agent
            #     physically cannot write findings.json and every review
            #     fails. This flag is documented as "intended solely for
            #     running in environments that are externally sandboxed" —
            #     exactly a GitHub-hosted runner. Mirrors Cursor's
            #     `--force --trust`.
            #   `-` positional: read the (diff-carrying, potentially >128 KB)
            #     prompt from stdin instead of argv, avoiding the OS E2BIG
            #     single-argument limit.
            argv: list[str] = [
                self.CLI_BIN,
                "exec",
                "--skip-git-repo-check",
                "--dangerously-bypass-approvals-and-sandbox",
            ]
            if self.model:
                argv += ["--model", self.model]
            if self.extra_args:
                argv += shlex.split(self.extra_args)
            # The stdin sentinel must be the final positional argument.
            argv.append("-")

            # CODEX_HOME redirects the CLI to read our apikey auth.json
            # (0.122+ requirement). OPENAI_API_KEY stays in the env for
            # back-compat with < 0.122 which read it directly.
            env: dict[str, str] = _build_cli_env(
                extra_vars={
                    "OPENAI_API_KEY": self.api_key,
                    "CODEX_HOME": str(codex_home),
                }
            )
            return _invoke_cli_agent(
                argv=argv,
                workspace=workspace,
                findings_path=findings_path,
                env=env,
                cli_name=self.CLI_NAME,
                stdin_input=user_prompt,
            )
        finally:
            # Best-effort cleanup of the isolated CODEX_HOME. The temp dir
            # is 0700 so cross-process leakage during the run is bounded;
            # unlink failures here are logged, not fatal.
            try:
                shutil.rmtree(codex_home)
            except OSError as e:  # noqa: BLE001 — cleanup is best-effort
                log(
                    f"Could not remove Codex temp home {codex_home}: {e}. "
                    "The runner is ephemeral; leftover files will be "
                    "destroyed with the VM."
                )


def build_provider(
    provider_id: str, *, api_key: str, model: str
) -> Provider | AgentRunnerProvider:
    """Construct the provider implementation for `provider_id`.

    Returns either a `Provider` (chat-completions family, action owns the
    tool-use loop) or an `AgentRunnerProvider` (vendor CLI owns the loop).
    `main()` dispatches on the returned instance type.
    """
    if provider_id == "anthropic":
        return AnthropicProvider(api_key=api_key, model=model)

    # Agent-runner providers share a common constructor shape — extra_args
    # and mcp_config_file come from the AIPRR_* env vars set by action.yml.
    extra_args: str = os.environ.get("AIPRR_AGENT_EXTRA_ARGS", "").strip()
    mcp_config: str = os.environ.get("AIPRR_MCP_CONFIG_FILE", "").strip()
    # `agent-max-turns` has no universal enforcement point: none of the
    # shipping CLIs (Claude Code, Cursor, Codex) expose a turn-count cap flag
    # on their current versions. Rather than silently ignore the input, warn
    # so the consumer knows the effective bound is CLI_INVOCATION_TIMEOUT and
    # can use `agent-extra-args` for a vendor-native limit. See docs/PROVIDERS.md.
    agent_max_turns: str = os.environ.get("AIPRR_AGENT_MAX_TURNS", "").strip()
    if agent_max_turns:
        log(
            f"WARNING: agent-max-turns={agent_max_turns!r} is set but is not "
            f"forwarded to the {provider_id} CLI — no turn-cap flag is "
            f"available on the shipping CLI. The effective bound is the "
            f"{CLI_INVOCATION_TIMEOUT}s invocation timeout. Use agent-extra-args "
            "for a vendor-native limit (e.g. Claude Code's --max-budget-usd)."
        )
    if provider_id == "claude-code":
        return ClaudeCodeProvider(
            api_key=api_key,
            model=model,
            extra_args=extra_args,
            mcp_config_file=mcp_config,
        )
    if provider_id == "cursor":
        return CursorProvider(
            api_key=api_key,
            model=model,
            extra_args=extra_args,
            mcp_config_file=mcp_config,
        )
    if provider_id == "codex":
        return CodexProvider(
            api_key=api_key,
            model=model,
            extra_args=extra_args,
            mcp_config_file=mcp_config,
        )
    raise ValueError(
        f"Unsupported provider: {provider_id!r}. Currently supported: "
        f"{sorted(DEFAULT_MODELS)}."
    )


# ---------------------------------------------------------------------------
# Provider-independent review payload
# ---------------------------------------------------------------------------


@dataclass
class Finding:
    """A single inline finding, provider-independent.

    Both provider families (chat-completions via `Provider` and agent-runner
    via `AgentRunnerProvider`) surface findings as this dataclass so the
    downstream submission / label / strictness paths never need to know
    which provider produced the review.
    """

    path: str
    line: int
    body: str
    severity: str = SEVERITY_INFO
    start_line: int | None = None
    side: str | None = "RIGHT"


@dataclass
class ReviewResult:
    """Provider-independent review payload consumed by the submission path."""

    summary: str = ""
    findings: list[Finding] = field(default_factory=list)
    overall_severity: str = SEVERITY_NONE


# ---------------------------------------------------------------------------
# PR context (the user message the model sees first)
# ---------------------------------------------------------------------------


@dataclass
class PRContext:
    """Snapshot of everything the model needs to start reviewing."""

    title: str
    author: str
    head_ref: str
    base_ref: str
    state: str
    additions: int
    deletions: int
    commits: int
    body: str
    changed_files: list[dict[str, Any]] = field(default_factory=list)
    diff: str = ""


def fetch_pr_context(
    *, repo: str, pr_number: int, base_ref: str, token: str
) -> PRContext:
    """Pull PR metadata + diff once and shape it into a single dataclass."""
    owner, name = repo.split("/", 1)
    pr: dict[str, Any] = gh_request(
        "GET", f"/repos/{owner}/{name}/pulls/{pr_number}", token=token
    )
    files_resp: list[dict[str, Any]] = []
    page: int = 1
    while True:
        chunk: Any = gh_request(
            "GET",
            f"/repos/{owner}/{name}/pulls/{pr_number}/files"
            f"?per_page={GH_CONNECTION_PAGE_SIZE}&page={page}",
            token=token,
        )
        if not chunk or not isinstance(chunk, list):
            break
        files_resp.extend(chunk)
        if len(chunk) < GH_CONNECTION_PAGE_SIZE:
            break
        page += 1

    # `git diff origin/<base>...HEAD` matches what reviewers see in the PR
    # diff tab, so the model's line numbers match GitHub's RIGHT-side diff
    # numbers. The consumer's checkout step needs `fetch-depth: 0` for this
    # to resolve — actions/checkout's default shallow clone won't have the
    # base ref locally.
    diff_proc = run_cmd(
        ["git", "diff", f"origin/{base_ref}...HEAD", "--no-color", "--unified=3"],
    )
    if diff_proc.returncode != 0:
        # Most common cause: a shallow checkout without `fetch-depth: 0`, so
        # `origin/<base>` isn't present locally. Surface it in the log rather
        # than silently feeding the model an empty diff.
        log(
            f"`git diff origin/{base_ref}...HEAD` failed "
            f"(exit {diff_proc.returncode}): "
            f"{diff_proc.stderr.strip()[:MAX_ERROR_BODY_CHARS]} — the consumer "
            "checkout likely needs `fetch-depth: 0`. Proceeding with whatever "
            "diff git produced."
        )
    diff_text: str = diff_proc.stdout
    if len(diff_text) > MAX_DIFF_CHARS:
        diff_text = (
            diff_text[:MAX_DIFF_CHARS]
            + f"\n\n[diff truncated at {MAX_DIFF_CHARS} characters — use the "
            "read_file tool to inspect specific changed files in full]"
        )

    return PRContext(
        title=pr.get("title", ""),
        author=(pr.get("user") or {}).get("login", ""),
        head_ref=(pr.get("head") or {}).get("ref", ""),
        base_ref=(pr.get("base") or {}).get("ref", base_ref),
        state=pr.get("state", ""),
        additions=pr.get("additions", 0),
        deletions=pr.get("deletions", 0),
        commits=pr.get("commits", 0),
        body=pr.get("body") or "",
        changed_files=[
            {
                "path": f.get("filename", ""),
                "status": f.get("status", ""),
                "additions": f.get("additions", 0),
                "deletions": f.get("deletions", 0),
            }
            for f in files_resp
        ],
        diff=diff_text,
    )


def render_user_prompt(ctx: PRContext, *, for_agent_runner: bool = False) -> str:
    """Produce the first user message — PR metadata + diff.

    The closing paragraph differs by provider family:
      - Chat-completions (`for_agent_runner=False`): references the built-in
        `read_file`/`grep`/`glob`/`post_inline_comment`/`submit_review` tools
        that this action owns.
      - Agent-runner (`for_agent_runner=True`): those tools do NOT exist for a
        vendor CLI, which uses its own file/search tools and returns findings
        via the `findings.json` output contract (see
        `write_findings_prompt_directive`). Emitting the chat-completions tool
        names here would give the CLI contradictory, unfollowable instructions.
    """
    files_block: str = "\n".join(
        f"- {f['path']} ({f['status']}) +{f['additions']}/-{f['deletions']}"
        for f in ctx.changed_files
    )
    body_block: str = ctx.body.strip() or "(no body)"
    if for_agent_runner:
        closing: str = (
            "Review this PR using the rubric in the instructions above. Use "
            "your own file-reading and search tools to verify findings "
            "against the broader codebase before reporting them. Only comment "
            "on lines that appear in the diff, and set each finding's "
            "`severity` honestly — it drives the gating behaviour configured "
            "by the consumer. When you're done, write your review to the "
            "findings file exactly as described in the output contract."
        )
    else:
        closing = (
            "Review this PR using the system prompt's rubric. Use `read_file`, "
            "`grep`, and `glob` to verify findings against the broader "
            "codebase before reporting them. Queue inline comments with "
            "`post_inline_comment` (only on lines that appear in the diff) and "
            "set the `severity` argument honestly — it drives the gating "
            "behaviour configured by the consumer. When you're done, call "
            "`submit_review` exactly once with the summary markdown — that "
            "signals the end of the session and posts the review."
        )
    return (
        f"# PR Context\n\n"
        f"**Title:** {ctx.title}\n"
        f"**Author:** {ctx.author}\n"
        f"**Branch:** `{ctx.head_ref}` → `{ctx.base_ref}`\n"
        f"**Stats:** +{ctx.additions}/-{ctx.deletions} across "
        f"{len(ctx.changed_files)} files in {ctx.commits} commit(s)\n\n"
        f"## Description\n\n{body_block}\n\n"
        f"## Changed Files\n\n{files_block or '(none)'}\n\n"
        f"## Full Diff\n\n```diff\n{ctx.diff}\n```\n\n"
        "---\n\n"
        + closing
    )


# ---------------------------------------------------------------------------
# Tool definitions
# ---------------------------------------------------------------------------


def tools_schema(
    max_inline_comments: int,
    *,
    allow_set_pr_description: bool = False,
    allow_set_pr_complexity: bool = False,
) -> list[dict[str, Any]]:
    """JSONSchema for every tool the model can call.

    `set_pr_description` is exposed only when `allow_set_pr_description`
    is True (i.e. `pr-description-mode: autocomplete`). Similarly for
    `set_pr_complexity` and the complexity-labeling feature. The base
    five tools are always present.
    """
    base: list[dict[str, Any]] = [
        {
            "name": "read_file",
            "description": (
                "Read a file from the repository. Use this to verify "
                "findings against full file context (the diff alone often "
                "lacks surrounding code). Output is capped to "
                f"{MAX_FILE_READ_LINES} lines per call — use `offset` and "
                "`limit` to paginate if needed."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Repository-relative file path.",
                    },
                    "offset": {
                        "type": "integer",
                        "description": "1-indexed starting line. Default 1.",
                    },
                    "limit": {
                        "type": "integer",
                        "description": (
                            f"Max lines to return. Default "
                            f"{MAX_FILE_READ_LINES}."
                        ),
                    },
                },
                "required": ["path"],
            },
        },
        {
            "name": "grep",
            "description": (
                "Search for a regex pattern in the repository. Returns "
                "file:line:match lines (up to 200). Pattern is POSIX "
                "extended regex (no PCRE features like lookahead/`\\b`). "
                "Use to verify whether a pattern exists elsewhere before "
                "flagging an issue as novel."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "pattern": {
                        "type": "string",
                        "description": "POSIX extended regex pattern.",
                    },
                    "path": {
                        "type": "string",
                        "description": (
                            "Optional path or glob to scope the search."
                        ),
                    },
                },
                "required": ["pattern"],
            },
        },
        {
            "name": "glob",
            "description": (
                "List repository files matching a glob (e.g. "
                "`src/**/*.ts`). Honors `.gitignore`. Returns up to 200 paths."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "pattern": {
                        "type": "string",
                        "description": "Glob pattern relative to repo root.",
                    }
                },
                "required": ["pattern"],
            },
        },
        {
            "name": "post_inline_comment",
            "description": (
                "Queue a single inline review comment. Comments are batched "
                "and submitted with the final review. The line you "
                "reference MUST appear in the PR diff (RIGHT side for new "
                "lines, LEFT for removed lines). For multi-line, set "
                "`start_line` < `line`. Set `severity` honestly: it drives "
                "the GitHub check status via the consumer's strictness "
                f"setting. Cap: {max_inline_comments} comments per review."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Repository-relative file path.",
                    },
                    "line": {
                        "type": "integer",
                        "description": (
                            "Line number (end line for multi-line)."
                        ),
                    },
                    "body": {
                        "type": "string",
                        "description": (
                            "Markdown body. Supports GitHub suggestion "
                            "blocks via ```suggestion ... ``` — those "
                            "replace the entire commented line range."
                        ),
                    },
                    "severity": {
                        "type": "string",
                        "enum": ["critical", "warning", "info"],
                        "description": (
                            "`critical` = correctness/security/data-loss/"
                            "broken-API. `warning` = bug-prone, perf, "
                            "maintainability. `info` = style/nit/"
                            "improvement. Default `info`."
                        ),
                    },
                    "start_line": {
                        "type": "integer",
                        "description": (
                            "Optional. Start line for multi-line comments."
                        ),
                    },
                    "side": {
                        "type": "string",
                        "enum": ["LEFT", "RIGHT"],
                        "description": (
                            "RIGHT (new code, default) or LEFT (removed code)."
                        ),
                    },
                },
                "required": ["path", "line", "body"],
            },
        },
        {
            "name": "submit_review",
            "description": (
                "Submit the final PR review. Call exactly once at the end. "
                "Provide the full summary markdown. Any queued inline "
                "comments post atomically with this review."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "summary": {
                        "type": "string",
                        "description": "The full review markdown body.",
                    },
                },
                "required": ["summary"],
            },
        },
    ]
    if allow_set_pr_description:
        base.append(
            {
                "name": "set_pr_description",
                "description": (
                    "Set the PR body to a new markdown value. Call this "
                    "AT MOST ONCE, only when the current PR body is missing "
                    "or too vague. Do NOT call it if the current body "
                    f"already carries the `{PR_DESC_AUTOCOMPLETE_MARKER}` "
                    "marker (that means a previous run already wrote it). "
                    "Do NOT include environment variables, tokens, or "
                    "secrets in the body."
                ),
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "body": {
                            "type": "string",
                            "description": (
                                "New markdown for the PR body. Should NOT "
                                "include the autocomplete marker — the "
                                "action appends it automatically."
                            ),
                        }
                    },
                    "required": ["body"],
                },
            }
        )
    if allow_set_pr_complexity:
        base.append(
            {
                "name": "set_pr_complexity",
                "description": (
                    "Assess and record the PR's overall complexity. Call "
                    "this AT MOST ONCE, near the end of the review. The "
                    "value drives a `complexity:*` label on the PR. Assess "
                    "based on total change (files touched, cognitive load, "
                    "cross-cutting concerns, security surface, test "
                    "coverage delta) — NOT line count."
                ),
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "level": {
                            "type": "string",
                            "enum": list(PR_COMPLEXITY_LEVELS),
                            "description": (
                                "`low` = self-contained, easy to review "
                                "(e.g. typo fix, doc, isolated helper). "
                                "`medium` = one subsystem, moderate "
                                "cognitive load. `high` = multiple "
                                "subsystems, security-adjacent code, "
                                "novel abstraction, or requires "
                                "cross-team review."
                            ),
                        }
                    },
                    "required": ["level"],
                },
            }
        )
    return base


# ---------------------------------------------------------------------------
# Tool implementations
# ---------------------------------------------------------------------------


@dataclass
class ReviewState:
    """Mutable state shared by tool handlers."""

    inline_comments: list[dict[str, Any]] = field(default_factory=list)
    severities: list[str] = field(default_factory=list)
    final_summary: str | None = None
    max_inline_comments: int = DEFAULT_MAX_INLINE_COMMENTS
    # Populated by `set_pr_description` tool (only in `autocomplete` mode).
    # None = model did not propose a description. `""` is treated identically
    # to None so an accidental empty-string tool call is a no-op.
    proposed_pr_description: str | None = None
    # Populated by `set_pr_complexity` tool (only when complexity labeling
    # is enabled). Values: `low`, `medium`, `high`. None = not proposed.
    proposed_pr_complexity: str | None = None


def safe_repo_path(rel: str) -> Path:
    """Resolve a repo-relative path, refusing to escape the workspace.

    Uses `Path.relative_to` (component-wise comparison) so a sibling
    directory that string-prefixes the repo root — e.g. workspace
    `/x/repo` and target `/x/repo_evil/file` — does not bypass the check.
    `Path.resolve()` follows symlinks, so a symlinked path that escapes
    the workspace is also caught.
    """
    repo_root: Path = Path.cwd().resolve()
    target: Path = (repo_root / rel).resolve()
    try:
        target.relative_to(repo_root)
    except ValueError as e:
        raise ValueError(f"Path escapes the workspace: {rel}") from e
    return target


def tool_read_file(args: dict[str, Any]) -> str:
    rel: str = args["path"]
    offset: int = max(1, int(args.get("offset", 1)))
    limit: int = min(
        MAX_FILE_READ_LINES, int(args.get("limit", MAX_FILE_READ_LINES))
    )
    try:
        path: Path = safe_repo_path(rel)
    except ValueError as e:
        return f"Error: {e}"
    if not path.exists() or not path.is_file():
        return f"Error: file not found: {rel}"
    with path.open("r", encoding="utf-8", errors="replace") as f:
        all_lines: list[str] = f.readlines()
    selected: list[str] = all_lines[offset - 1 : offset - 1 + limit]
    numbered: str = "".join(
        f"{i + offset:>6}\t{line}" for i, line in enumerate(selected)
    )
    header: str = (
        f"# {rel}  (lines {offset}–{offset + len(selected) - 1} of "
        f"{len(all_lines)})\n"
    )
    return truncate_for_tool(header + numbered, label="read_file")


def tool_grep(args: dict[str, Any]) -> str:
    pattern: str = args["pattern"]
    scope: str | None = args.get("path")
    cmd: list[str] = ["grep", "-rIn", "-E", "--", pattern]
    if scope:
        # Validate the scope path the same way `tool_read_file` does so a
        # caller cannot smuggle `../../etc/passwd`-style traversal. The `--`
        # separator only protects the pattern from flag injection; it does
        # NOT restrict which filesystem paths grep will read.
        try:
            scope = str(safe_repo_path(scope))
        except ValueError as e:
            return f"Error: {e}"
        cmd.append(scope)
    else:
        cmd.append(".")
    proc = run_cmd(cmd)
    if proc.returncode not in (0, 1):  # 1 = no matches, fine
        return (
            f"grep error (exit {proc.returncode}): "
            f"{proc.stderr.strip()[:MAX_ERROR_BODY_CHARS]}"
        )
    lines: list[str] = proc.stdout.splitlines()
    if not lines:
        return f"(no matches for /{pattern}/)"
    if len(lines) > MAX_SEARCH_RESULTS:
        lines = lines[:MAX_SEARCH_RESULTS] + [
            f"... [{len(lines) - MAX_SEARCH_RESULTS} more matches truncated]"
        ]
    return truncate_for_tool("\n".join(lines), label="grep")


def tool_glob(args: dict[str, Any]) -> str:
    pattern: str = args["pattern"]
    proc = run_cmd(["git", "ls-files", "--", pattern])
    if proc.returncode != 0:
        return (
            f"glob error (exit {proc.returncode}): "
            f"{proc.stderr.strip()[:MAX_ERROR_BODY_CHARS]}"
        )
    paths: list[str] = proc.stdout.splitlines()
    if not paths:
        return f"(no files match {pattern})"
    if len(paths) > MAX_SEARCH_RESULTS:
        paths = paths[:MAX_SEARCH_RESULTS] + [
            f"... [{len(paths) - MAX_SEARCH_RESULTS} more paths truncated]"
        ]
    return truncate_for_tool("\n".join(paths), label="glob")


def tool_post_inline_comment(args: dict[str, Any], state: ReviewState) -> str:
    if len(state.inline_comments) >= state.max_inline_comments:
        return (
            f"Error: inline-comment cap reached ({state.max_inline_comments}). "
            "Drop or merge less-critical comments before adding more."
        )
    severity: str = (args.get("severity") or SEVERITY_INFO).lower()
    if severity not in SEVERITY_RANK or severity == SEVERITY_NONE:
        severity = SEVERITY_INFO
    comment: dict[str, Any] = {
        "path": args["path"],
        "body": args["body"],
        "line": int(args["line"]),
        "side": args.get("side", "RIGHT"),
    }
    if "start_line" in args and args["start_line"] is not None:
        comment["start_line"] = int(args["start_line"])
        comment["start_side"] = args.get("side", "RIGHT")
    state.inline_comments.append(comment)
    state.severities.append(severity)
    return (
        f"Queued inline comment #{len(state.inline_comments)} on "
        f"{comment['path']}:{comment['line']} (severity={severity}). It will "
        "post with the final review when you call submit_review."
    )


def tool_submit_review(args: dict[str, Any], state: ReviewState) -> str:
    if state.final_summary is not None:
        # Idempotency guard — models occasionally re-call across multi-tool
        # turns. Keep the first articulation; surface a clear error so the
        # model stops trying.
        return (
            "Error: submit_review was already called this session and your "
            "review summary has been recorded. Do not call submit_review "
            "again — end your turn so the script can post the review."
        )
    state.final_summary = args["summary"]
    return (
        "Review accepted. End your turn now — the script will post the review "
        "with the queued inline comments. Do not call any more tools."
    )


def tool_set_pr_description(
    args: dict[str, Any], state: ReviewState
) -> str:
    """Record a proposed PR body. The actual PATCH happens in `main()`
    after the loop terminates, so the whole lifecycle stays atomic and
    the marker check can inspect the final resolved body.
    """
    body: str = args.get("body", "")
    if not isinstance(body, str) or not body.strip():
        return (
            "Error: `body` must be a non-empty string. Skipped — the "
            "current PR body will be left unchanged."
        )
    if state.proposed_pr_description is not None:
        return (
            "Error: set_pr_description was already called this session. "
            "The first proposal is retained; do not call it again."
        )
    state.proposed_pr_description = body
    return (
        "PR description proposal recorded. The action will PATCH the PR "
        "body after this run if the current body is missing/vague and "
        "does not already carry the autocomplete marker."
    )


def tool_set_pr_complexity(
    args: dict[str, Any], state: ReviewState
) -> str:
    """Record an AI-assessed complexity level. The actual label
    application happens in `main()` after the loop terminates.
    """
    level: str = str(args.get("level", "")).strip().lower()
    if level not in PR_COMPLEXITY_LEVELS:
        return (
            f"Error: `level` must be one of {list(PR_COMPLEXITY_LEVELS)}. "
            f"Got: {level!r}. Skipped."
        )
    if state.proposed_pr_complexity is not None:
        return (
            "Error: set_pr_complexity was already called this session. "
            "The first assessment is retained; do not call it again."
        )
    state.proposed_pr_complexity = level
    return (
        f"PR complexity `{level}` recorded. The action will apply the "
        "corresponding label after this run."
    )


def execute_tool(name: str, args: dict[str, Any], state: ReviewState) -> str:
    """Dispatch a tool call to its handler and return a tool_result string."""
    try:
        if name == "read_file":
            return tool_read_file(args)
        if name == "grep":
            return tool_grep(args)
        if name == "glob":
            return tool_glob(args)
        if name == "post_inline_comment":
            return tool_post_inline_comment(args, state)
        if name == "submit_review":
            return tool_submit_review(args, state)
        if name == "set_pr_description":
            return tool_set_pr_description(args, state)
        if name == "set_pr_complexity":
            return tool_set_pr_complexity(args, state)
        return f"Error: unknown tool `{name}`"
    except Exception as e:  # noqa: BLE001 — surface to model rather than crash
        return f"Tool `{name}` raised {type(e).__name__}: {e}"


# ---------------------------------------------------------------------------
# Author-association gate (v1.3.0+): the first / cheapest gate. Runs before
# `trigger-mode` so a rejected PR never consumes a single API call.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AuthorAssociationDecision:
    """Result of `resolve_author_association_gate()`.

    - `should_run` — True when the PR author is allowed through the gate.
    - `reason` — one-line explanation for the runtime log.
    - `author_association` — the association we compared, uppercase.
    - `allowed_associations` — the parsed whitelist, for downstream log
      messages (`Skipping: author X not in [OWNER, MEMBER, …]`).
    """

    should_run: bool
    reason: str
    author_association: str
    allowed_associations: tuple[str, ...]


def resolve_author_association_gate(
    *, gate: str, actual_association: str
) -> AuthorAssociationDecision:
    """Decide whether the review should run given the author-association
    whitelist `gate` and the PR's actual `author_association`.

    Semantics:

    - **Empty `gate`** — gate disabled, every author allowed.
    - **Empty `actual_association`** — no PR context (local run,
      `workflow_dispatch`, malformed event payload). Fail-open: the
      operator running locally already has write access, and CI-time
      failures to read the payload are already logged elsewhere.
    - **`actual_association` in whitelist** — allowed.
    - **`actual_association` not in whitelist** — denied.

    Parsing is case-insensitive and tolerates whitespace between commas.
    Unknown values in the whitelist are logged as a warning and can
    never match (fail-safe).
    """
    normalized_gate: str = gate.strip()
    if not normalized_gate:
        return AuthorAssociationDecision(
            should_run=True,
            reason="no author-association gate configured",
            author_association=(actual_association or "").upper(),
            allowed_associations=(),
        )

    allowed: tuple[str, ...] = tuple(
        piece.strip().upper()
        for piece in normalized_gate.split(",")
        if piece.strip()
    )
    unknown: list[str] = [
        a for a in allowed if a not in VALID_AUTHOR_ASSOCIATIONS
    ]
    if unknown:
        log(
            f"WARNING: author-association gate lists unknown value(s) "
            f"{unknown}; they will never match. Valid values: "
            f"{list(VALID_AUTHOR_ASSOCIATIONS)}."
        )

    normalized_actual: str = (actual_association or "").upper()

    if not normalized_actual:
        return AuthorAssociationDecision(
            should_run=True,
            reason=(
                "author-association gate fail-open: no PR "
                "author_association in event payload (likely a local "
                "run, workflow_dispatch, or malformed event)"
            ),
            author_association="",
            allowed_associations=allowed,
        )

    if normalized_actual in allowed:
        return AuthorAssociationDecision(
            should_run=True,
            reason=(
                f"author_association '{normalized_actual}' matches "
                f"gate {list(allowed)}"
            ),
            author_association=normalized_actual,
            allowed_associations=allowed,
        )

    return AuthorAssociationDecision(
        should_run=False,
        reason=(
            f"author_association '{normalized_actual}' not in gate "
            f"{list(allowed)}"
        ),
        author_association=normalized_actual,
        allowed_associations=allowed,
    )


# ---------------------------------------------------------------------------
# Trigger modes (v1.2.0+): decide whether to run based on webhook event
# + label state + prior-run marker generation.
# ---------------------------------------------------------------------------


@dataclass
class TriggerDecision:
    """Result of `resolve_trigger_action()`."""

    should_run: bool
    reason: str  # log line + optional tracking-comment note


COUNT_LABEL_EVENTS_MAX_PAGES: int = 20


def count_label_events(
    *, token: str, repo: str, pr_number: int, label: str
) -> int:
    """Return the number of times `label` was applied to the PR.

    Uses `/issues/{n}/timeline`, filtered on `labeled` events with the
    matching label name. Best-effort: any error returns the count
    accumulated so far (possibly 0) — callers use the "was the label
    present at all?" signal to decide whether that 0 is meaningful.

    Pagination is capped at `COUNT_LABEL_EVENTS_MAX_PAGES` (~2000
    timeline events) to bound cost on long-lived, high-chatter PRs.
    When the cap is hit a `WARNING:` is logged; `label-once` may
    undercount the generation on such PRs, in which case toggling the
    label twice or switching to `label-added-only` are the documented
    workarounds (see docs/TRIGGER_MODES.md § "Edge cases").
    """
    if not label:
        return 0
    owner, name = repo.split("/", 1)
    count: int = 0
    page: int = 1
    while True:
        try:
            events: list[dict[str, Any]] = gh_request(
                "GET",
                (
                    f"/repos/{owner}/{name}/issues/{pr_number}/timeline"
                    f"?per_page=100&page={page}"
                ),
                token=token,
            )
        except Exception as e:  # noqa: BLE001 — best-effort GH API call
            log(f"count_label_events: could not read timeline page {page}: {e}")
            return count
        if not isinstance(events, list) or not events:
            break
        for ev in events:
            if not isinstance(ev, dict):
                continue
            if ev.get("event") != "labeled":
                continue
            lbl_field: dict[str, Any] = ev.get("label") or {}
            if (lbl_field.get("name") or "") == label:
                count += 1
        if len(events) < 100:
            break
        page += 1
        if page > COUNT_LABEL_EVENTS_MAX_PAGES:
            log(
                f"WARNING: count_label_events hit the "
                f"{COUNT_LABEL_EVENTS_MAX_PAGES}-page pagination cap for "
                f"label {label!r} on PR #{pr_number}. `label-once` "
                f"generation may be undercounted; if a re-review does not "
                f"fire, toggle {label!r} off/on twice or switch to "
                f"`trigger-mode: label-added-only`."
            )
            break
    return count


def read_trigger_state(tracking_comment_body: str) -> dict[str, Any]:
    """Parse the ai-pr-reviewer-state HTML comment, or `{}` if absent."""
    if not tracking_comment_body:
        return {}
    body: str = tracking_comment_body
    open_at: int = body.find(TRIGGER_STATE_MARKER_OPEN)
    if open_at < 0:
        return {}
    close_at: int = body.find(
        TRIGGER_STATE_MARKER_CLOSE, open_at + len(TRIGGER_STATE_MARKER_OPEN)
    )
    if close_at < 0:
        return {}
    raw: str = body[
        open_at + len(TRIGGER_STATE_MARKER_OPEN) : close_at
    ].strip()
    try:
        parsed: Any = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    if not isinstance(parsed, dict):
        return {}
    return parsed


def write_trigger_state(body: str, state: dict[str, Any]) -> str:
    """Emit `body` with the ai-pr-reviewer-state marker inserted/updated.

    The state block sits on a line by itself just after the runtime's
    canonical `<!-- ai-pr-reviewer-marker -->` marker (or at the top if
    that marker is absent). Round-trips cleanly through `read_trigger_state`.
    """
    payload: str = (
        TRIGGER_STATE_MARKER_OPEN + json.dumps(state, sort_keys=True)
        + TRIGGER_STATE_MARKER_CLOSE
    )
    # Strip any pre-existing state block to keep the body idempotent.
    prior_open: int = body.find(TRIGGER_STATE_MARKER_OPEN)
    if prior_open >= 0:
        prior_close: int = body.find(
            TRIGGER_STATE_MARKER_CLOSE,
            prior_open + len(TRIGGER_STATE_MARKER_OPEN),
        )
        if prior_close >= 0:
            body = (
                body[:prior_open]
                + body[prior_close + len(TRIGGER_STATE_MARKER_CLOSE) :]
            )
            body = body.lstrip("\n")
    canonical_marker: str = "<!-- ai-pr-reviewer-marker -->"
    marker_at: int = body.find(canonical_marker)
    if marker_at < 0:
        return payload + "\n" + body
    insert_at: int = marker_at + len(canonical_marker)
    return body[:insert_at] + "\n" + payload + body[insert_at:]


def _read_github_event_payload() -> dict[str, Any]:
    """Return the current GitHub event payload as a dict, or `{}`.

    Reads `GITHUB_EVENT_PATH` — a JSON file provided by the runner for
    every workflow event. Best-effort: parsing errors return `{}` so
    the trigger resolver treats the event as generic.
    """
    path: str = os.environ.get("GITHUB_EVENT_PATH", "")
    if not path:
        return {}
    try:
        with open(path, "r", encoding="utf-8") as fh:
            payload: Any = json.load(fh)
    except (OSError, json.JSONDecodeError) as e:
        log(f"Could not read GITHUB_EVENT_PATH ({path!r}): {e}")
        return {}
    if not isinstance(payload, dict):
        return {}
    return payload


def _read_github_event_action() -> str:
    """Return the `action` field of the current GitHub event, or `""`."""
    payload = _read_github_event_payload()
    return str(payload.get("action", "") or "")


def _read_github_event_label() -> str:
    """Return the `label.name` field of the current event, or `""`.

    Only meaningful for `labeled` / `unlabeled` webhook events, where
    GitHub attaches the specific label that triggered the event to the
    payload. Any other event returns `""`. Used by
    `label-added-only` to reject webhooks fired by unrelated labels.
    """
    payload = _read_github_event_payload()
    label = payload.get("label")
    if not isinstance(label, dict):
        return ""
    return str(label.get("name", "") or "")


def _read_github_event_pr_author_association() -> str:
    """Return `pull_request.author_association` from the event payload
    in uppercase, or `""` when unavailable.

    GitHub attaches this field to every `pull_request` /
    `pull_request_target` webhook — it is derived server-side and
    cannot be spoofed by the PR author. When empty, the caller is
    outside a PR-event context (local run, `workflow_dispatch`, etc.)
    and the caller should fail-open on the author gate.
    """
    payload = _read_github_event_payload()
    pr = payload.get("pull_request")
    if not isinstance(pr, dict):
        return ""
    return str(pr.get("author_association", "") or "").upper()


def _read_existing_tracking_state(
    *, token: str, repo: str, pr_number: int
) -> dict[str, Any]:
    """Fetch prior ai-pr-reviewer tracking-comment state, or `{}`.

    Looks for an issue comment carrying the canonical
    `<!-- ai-pr-reviewer-marker -->` marker. Returns the parsed state
    JSON from the same body, or `{}` when no prior comment exists.
    """
    owner, name = repo.split("/", 1)
    try:
        comments: list[dict[str, Any]] = gh_request(
            "GET",
            f"/repos/{owner}/{name}/issues/{pr_number}/comments?per_page=100",
            token=token,
        )
    except Exception as e:  # noqa: BLE001 — best-effort GH API call
        log(f"Could not list issue comments for trigger state: {e}")
        return {}
    if not isinstance(comments, list):
        return {}
    marker: str = "<!-- ai-pr-reviewer-marker -->"
    # Iterate in reverse — the most recent tracking comment wins.
    for comment in reversed(comments):
        if not isinstance(comment, dict):
            continue
        body: str = str(comment.get("body") or "")
        if marker not in body:
            continue
        return read_trigger_state(body)
    return {}


def resolve_trigger_action(
    *,
    trigger_mode: str,
    event_action: str,
    label_gate: str,
    current_labels: list[str],
    label_toggle_generation: int,
    last_reviewed_generation: int,
    event_label: str = "",
) -> TriggerDecision:
    """Decide whether to run the review for the current event.

    `event_label` is the specific label attached to `labeled`/`unlabeled`
    webhook payloads (from `event.label.name`). It's used by
    `label-added-only` to distinguish "this label was just added" from
    "some unrelated label was just added while `label_gate` was already
    present."

    See docs/TRIGGER_MODES.md for the full semantics per mode.
    """
    if trigger_mode == TRIGGER_ALWAYS:
        return TriggerDecision(True, "trigger-mode=always")

    if not label_gate:
        return TriggerDecision(
            True,
            f"trigger-mode={trigger_mode} requires label-gate; "
            "no label-gate set → treating as always.",
        )

    label_present: bool = label_gate in current_labels

    if trigger_mode == TRIGGER_LABEL_REQUIRED:
        if not label_present:
            return TriggerDecision(
                False, f"label {label_gate!r} not present"
            )
        return TriggerDecision(True, f"label {label_gate!r} present")

    if trigger_mode == TRIGGER_LABEL_ONCE:
        if not label_present:
            return TriggerDecision(
                False, f"label {label_gate!r} not present"
            )
        # Only skip on a stale generation if we actually counted at least
        # one `labeled` event. Otherwise `count_label_events()` returned 0
        # (transient API error, permissions issue, or empty timeline) —
        # skipping would silently mask "the label is present but we can't
        # tell how many times it's been applied." Better to run than to
        # deliver nothing. Regression for PR #9 self-review comment #4.
        if (
            label_toggle_generation > 0
            and label_toggle_generation <= last_reviewed_generation
        ):
            return TriggerDecision(
                False,
                (
                    f"already reviewed label generation "
                    f"{last_reviewed_generation} — toggle "
                    f"{label_gate!r} off/on to re-run"
                ),
            )
        return TriggerDecision(
            True,
            (
                f"new label generation ({label_toggle_generation} "
                f"vs. last reviewed {last_reviewed_generation})"
            ),
        )

    if trigger_mode == TRIGGER_LABEL_ADDED_ONLY:
        if event_action != "labeled":
            return TriggerDecision(
                False,
                (
                    f"event action {event_action!r} is not 'labeled' — "
                    "workflow must subscribe with `types: [labeled]`"
                ),
            )
        if not label_present:
            return TriggerDecision(
                False, f"label {label_gate!r} not present"
            )
        # `labeled` fires for ANY label — reject when it's not our gate.
        # Without this, adding an unrelated label (e.g. `bug`) triggers
        # a full review as long as `label_gate` was already present.
        if event_label and event_label != label_gate:
            return TriggerDecision(
                False,
                (
                    f"labeled event was for {event_label!r}, "
                    f"not {label_gate!r}"
                ),
            )
        return TriggerDecision(True, "labeled event fired")

    return TriggerDecision(
        False, f"unknown trigger-mode {trigger_mode!r} — no action taken"
    )


# ---------------------------------------------------------------------------
# PR metadata checks (v1.2.0+): description review + complexity labeling.
# ---------------------------------------------------------------------------


@dataclass
class DescriptionVerdict:
    """Result of `evaluate_pr_description()`."""

    is_adequate: bool
    reason: str  # empty when adequate, otherwise a short human-readable reason


def build_agent_runner_noop_warning(
    *,
    provider_id: str,
    is_agent_runner: bool,
    pr_desc_mode: str,
    complexity_labels_enabled: bool,
) -> str:
    """Return the WARNING log line for v1.2 features that silently no-op
    on agent-runner providers, or `""` if none apply.

    Extracted from `main()` for unit-testability. `set_pr_description` and
    `set_pr_complexity` are exposed via `tools_schema()` only on the
    chat-completions path, so `pr-description-mode=autocomplete` and
    `complexity-labels-enabled=true` never populate their `state.proposed_*`
    fields on agent-runner providers → the post-loop PATCH/label blocks
    silently no-op. See docs/PR_METADATA_CHECKS.md § "Provider support
    matrix".
    """
    if not is_agent_runner:
        return ""
    skips: list[str] = []
    if pr_desc_mode == PR_DESC_MODE_AUTOCOMPLETE:
        skips.append("pr-description-mode=autocomplete")
    if complexity_labels_enabled:
        skips.append("complexity-labels-enabled=true")
    if not skips:
        return ""
    return (
        "WARNING: "
        + ", ".join(skips)
        + f" requested but provider={provider_id!r} is an "
        "agent-runner CLI. These features are chat-completions-only "
        "in v1.2 and will silently no-op. See "
        "docs/PR_METADATA_CHECKS.md § 'Provider support matrix'."
    )


def evaluate_pr_description(
    body: str, *, min_length: int
) -> DescriptionVerdict:
    """Cheap heuristic — 'missing' if body is empty/whitespace after strip,
    'vague' if under `min_length` after stripping the autocomplete marker.

    The heuristic is intentionally simple; a smart LLM check would burn
    tokens on trivia. The maintainer decides the minimum length; the
    action just enforces it.
    """
    stripped: str = (
        (body or "").replace(PR_DESC_AUTOCOMPLETE_MARKER, "").strip()
    )
    if not stripped:
        return DescriptionVerdict(
            is_adequate=False, reason="PR description is empty."
        )
    if len(stripped) < min_length:
        return DescriptionVerdict(
            is_adequate=False,
            reason=(
                f"PR description is too short ({len(stripped)} chars); "
                f"minimum is {min_length}."
            ),
        )
    return DescriptionVerdict(is_adequate=True, reason="")


def gh_patch_pr_body(
    *, token: str, repo: str, pr_number: int, new_body: str
) -> None:
    """PATCH the PR body via the GitHub REST API.

    Raises on non-2xx. Callers are expected to wrap this in try/except
    so PR-description autocomplete failures do not crash the review.
    """
    owner, name = repo.split("/", 1)
    gh_request(
        "PATCH",
        f"/repos/{owner}/{name}/pulls/{pr_number}",
        token=token,
        body={"body": new_body},
    )


# ---------------------------------------------------------------------------
# Severity / strictness
# ---------------------------------------------------------------------------


def overall_severity(severities: list[str]) -> str:
    """Return the highest severity in the list, or `none` if empty."""
    if not severities:
        return SEVERITY_NONE
    ranked: list[tuple[int, str]] = [
        (SEVERITY_RANK.get(s, 0), s) for s in severities
    ]
    return max(ranked)[1]


def state_to_review_result(state: "ReviewState") -> ReviewResult:
    """Adapt a `ReviewState` (populated by `drive_review`) into a `ReviewResult`.

    Bridges the chat-completions provider family into the provider-independent
    shape the submission path consumes. The CLI (agent-runner) providers
    produce `ReviewResult` directly via `parse_findings_file`, so the two
    families converge at this dataclass.
    """
    findings: list[Finding] = []
    for i, comment in enumerate(state.inline_comments):
        severity: str = (
            state.severities[i] if i < len(state.severities) else SEVERITY_INFO
        )
        findings.append(
            Finding(
                path=str(comment.get("path", "")),
                line=int(comment.get("line", 0)),
                body=str(comment.get("body", "")),
                severity=severity,
                start_line=(
                    int(comment["start_line"])
                    if "start_line" in comment
                    and comment["start_line"] is not None
                    else None
                ),
                side=comment.get("side", "RIGHT"),
            )
        )
    severities: list[str] = [f.severity for f in findings]
    return ReviewResult(
        summary=state.final_summary or "",
        findings=findings,
        overall_severity=overall_severity(severities),
    )


def _extract_summary_from_malformed_findings(raw_text: str) -> str | None:
    """Best-effort extraction for malformed agent-runner JSON.

    Some vendor CLIs occasionally hand-write invalid JSON while still leaving
    a valid top-level `summary` string. Recovering that summary lets the action
    post a review instead of failing the whole check; inline findings are
    intentionally not recovered from malformed JSON.
    """
    key_index: int = raw_text.find('"summary"')
    if key_index < 0:
        return None
    colon_index: int = raw_text.find(":", key_index + len('"summary"'))
    if colon_index < 0:
        return None
    value_start: int = colon_index + 1
    while value_start < len(raw_text) and raw_text[value_start].isspace():
        value_start += 1
    if value_start >= len(raw_text) or raw_text[value_start] != '"':
        return None

    decoder = json.JSONDecoder()
    try:
        value, _end_index = decoder.raw_decode(raw_text[value_start:])
    except json.JSONDecodeError:
        return None
    if not isinstance(value, str):
        return None
    return value


def parse_findings_file(
    path: Path, *, allow_malformed_summary_fallback: bool = False
) -> ReviewResult:
    """Parse an agent-runner `findings.json` into a `ReviewResult`.

    Strict validation:
      - Root MUST be a JSON object.
      - `findings` MUST be a list (may be empty).
      - Every finding MUST carry non-empty `path`, integer `line`, non-empty
        `body`. Missing severity defaults to `info`; unknown severities raise.
      - Optional `start_line` is coerced to int; optional `side` MUST be one
        of LEFT/RIGHT (case-normalised).
      - Unknown top-level or per-finding keys are silently ignored (forward-
        compat with vendor extensions).

    Raises:
      - `FileNotFoundError` with an actionable message if the file is missing.
      - `ValueError` for malformed JSON or schema violations, quoting the
        offending path/index/value so the caller can surface it to the model.
    """
    if not path.exists():
        raise FileNotFoundError(
            f"Agent-runner provider did not write {path}. "
            "The CLI may have crashed, the review-instruction prompt may be "
            "missing the write-to-file directive, or the workspace path is "
            "wrong. See docs/PROVIDERS.md for the contract."
        )
    raw_text: str = path.read_text(encoding="utf-8")
    try:
        raw: Any = json.loads(raw_text)
    except json.JSONDecodeError as e:
        if allow_malformed_summary_fallback:
            recovered_summary: str | None = (
                _extract_summary_from_malformed_findings(raw_text)
            )
            if recovered_summary:
                log(
                    "WARNING: Agent-runner provider wrote malformed "
                    f"findings.json ({e}). Posting summary-only review; "
                    "inline findings were dropped because the JSON could "
                    "not be trusted."
                )
                summary: str = (
                    recovered_summary.rstrip()
                    + "\n\n---\n\n"
                    + "**AI PR Reviewer note:** The CLI wrote malformed "
                    + "`findings.json`, so this run posted the recovered "
                    + "summary only and dropped inline findings."
                )
                return ReviewResult(
                    summary=summary,
                    findings=[],
                    overall_severity=SEVERITY_NONE,
                )
        snippet: str = raw_text[:MAX_ERROR_BODY_CHARS]
        raise ValueError(
            f"Malformed findings.json ({e}). Content head: {snippet!r}"
        ) from e

    if not isinstance(raw, dict):
        raise ValueError(
            f"findings.json root must be an object, got {type(raw).__name__}"
        )

    summary: str = str(raw.get("summary") or "")
    raw_findings: Any = raw.get("findings") if raw.get("findings") is not None else []
    if not isinstance(raw_findings, list):
        raise ValueError(
            f"'findings' must be a list, got {type(raw_findings).__name__}"
        )

    findings: list[Finding] = []
    for i, item in enumerate(raw_findings):
        if not isinstance(item, dict):
            raise ValueError(f"finding[{i}] must be an object")
        try:
            path_val: str = str(item["path"])
            line_val: int = int(item["line"])
            body_val: str = str(item["body"])
        except (KeyError, TypeError, ValueError) as e:
            raise ValueError(
                f"finding[{i}] missing or invalid required field: {e}"
            ) from e
        if not path_val:
            raise ValueError(f"finding[{i}].path is empty")
        if not body_val.strip():
            raise ValueError(f"finding[{i}].body is empty")

        severity_val: str = str(item.get("severity") or SEVERITY_INFO).lower()
        if severity_val not in ALLOWED_SEVERITIES:
            raise ValueError(
                f"finding[{i}].severity={severity_val!r} not in "
                f"{ALLOWED_SEVERITIES}"
            )

        start_line_val: int | None = None
        if item.get("start_line") is not None:
            try:
                start_line_val = int(item["start_line"])
            except (TypeError, ValueError) as e:
                raise ValueError(
                    f"finding[{i}].start_line must be an integer: {e}"
                ) from e

        side_val: str | None = "RIGHT"
        if item.get("side") is not None:
            side_val = str(item["side"]).upper()
            if side_val not in ALLOWED_SIDES:
                raise ValueError(
                    f"finding[{i}].side={side_val!r} not in {ALLOWED_SIDES}"
                )

        findings.append(
            Finding(
                path=path_val,
                line=line_val,
                body=body_val,
                severity=severity_val,
                start_line=start_line_val,
                side=side_val,
            )
        )

    severities: list[str] = [f.severity for f in findings]
    return ReviewResult(
        summary=summary,
        findings=findings,
        overall_severity=overall_severity(severities),
    )


def write_findings_prompt_directive(
    review_instructions: str, findings_path: Path
) -> str:
    """Append the "write your findings to this file" directive to the
    review instructions handed to an agent-runner CLI.

    Standardised so every CLI provider emits the same schema — the receiving
    parser (`parse_findings_file`) is a single implementation shared across
    all providers.
    """
    return (
        review_instructions
        + "\n\n---\n\n"
        + "## Output contract (MANDATORY)\n\n"
        + "Before ending your turn, write your review to the file:\n\n"
        + f"    {findings_path}\n\n"
        + "as JSON matching this schema:\n\n"
        + "```json\n"
        + "{\n"
        + '  "summary": "markdown body of the overall review",\n'
        + '  "findings": [\n'
        + "    {\n"
        + '      "path": "repo-relative file path (must appear in the PR diff)",\n'
        + '      "line": 123,\n'
        + '      "body": "markdown body of this inline comment",\n'
        + '      "severity": "critical | warning | info",\n'
        + '      "start_line": 121,\n'
        + '      "side": "RIGHT"\n'
        + "    }\n"
        + "  ]\n"
        + "}\n"
        + "```\n\n"
        + "Rules:\n"
        + "- `path` and `line` MUST reference a line that appears in the PR "
        + "diff. Off-diff lines are rejected by GitHub with HTTP 422 and lose "
        + "the whole review.\n"
        + "- `severity` MUST be exactly one of `critical`, `warning`, `info` "
        + "(lowercase). Choose honestly — it drives the strictness gate.\n"
        + "- `start_line` and `side` are optional. `side` defaults to `RIGHT` "
        + "(new code); use `LEFT` for removed code.\n"
        + "- Empty `findings` is valid — it means "
        + '"no issues found; just the summary".\n'
        + "- Only write the file once, at the end. Do NOT stream partials.\n"
        + "- The file MUST parse with Python `json.load()`. Do not hand-write "
        + "JSON when the content contains Markdown, quotes, or code blocks; "
        + "use a JSON serializer so strings are escaped correctly."
    )


def findings_to_gh_inline_comments(
    findings: list[Finding],
) -> list[dict[str, Any]]:
    """Convert a `list[Finding]` into the GitHub Reviews API inline shape.

    Kept separate from `state_to_review_result` so agent-runner providers
    (which produce `Finding`s directly from `.aiprr/findings.json`) can reuse
    the same encoder without round-tripping through `ReviewState`.
    """
    out: list[dict[str, Any]] = []
    for f in findings:
        comment: dict[str, Any] = {
            "path": f.path,
            "body": f.body,
            "line": f.line,
            "side": f.side or "RIGHT",
        }
        if f.start_line is not None:
            comment["start_line"] = f.start_line
            comment["start_side"] = f.side or "RIGHT"
        out.append(comment)
    return out


def compose_system_prompt(base: str, extension: str) -> str:
    """Compose the effective system prompt from a base + optional extension.

    - `extension` empty → returns `base` unchanged.
    - `extension` non-empty → returns `base.rstrip() + "\\n\\n---\\n\\n" +
      extension.lstrip()`. The `---` separator gives the model an
      unambiguous boundary between the base prompt and the consumer's
      overrides so overrides can safely contradict the base.
    """
    if not extension:
        return base
    return base.rstrip() + "\n\n---\n\n" + extension.lstrip()


def evaluate_strictness(
    severity: str, strictness: str
) -> tuple[bool, str]:
    """Decide whether the configured strictness blocks the check.

    Returns `(blocked, reason)`. `reason` is a short human-readable string
    that goes into both the workflow log and the tracking comment.
    """
    if strictness not in VALID_STRICTNESS:
        # Defensive fallback — invalid input becomes lenient so a typo can
        # never fail the check unexpectedly.
        return False, f"unknown strictness {strictness!r} → treated as lenient"
    if strictness == STRICTNESS_LENIENT:
        return False, "lenient — never blocks"
    rank: int = SEVERITY_RANK.get(severity, 0)
    if strictness == STRICTNESS_BLOCK_CRITICAL:
        if rank >= SEVERITY_RANK[SEVERITY_CRITICAL]:
            return True, "found `critical` severity — block-on-critical fired"
        return False, f"highest severity `{severity}` ≤ critical threshold"
    if strictness == STRICTNESS_BLOCK_WARNING:
        if rank >= SEVERITY_RANK[SEVERITY_WARNING]:
            return True, (
                f"found `{severity}` severity — block-on-warning fired"
            )
        return False, f"highest severity `{severity}` ≤ warning threshold"
    if strictness == STRICTNESS_BLOCK_ANY:
        # Zero-tolerance: blocks on any finding, including `info`. The gate
        # fires whenever a comment was posted (i.e. severity is not `none`).
        if severity != SEVERITY_NONE:
            return True, (
                f"found `{severity}` severity — block-on-any fired"
            )
        return False, "no findings — block-on-any passes"
    return False, "unhandled strictness branch"


# ---------------------------------------------------------------------------
# Agentic loop
# ---------------------------------------------------------------------------


def drive_review(
    *,
    provider: Provider,
    system_prompt: str,
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]],
    state: ReviewState,
    max_turns: int,
) -> None:
    """Drive the agentic tool-use loop until submit_review or end_turn.

    Mutates `messages` and `state` in place; raises if the API or a tool
    call surfaces an uncaught exception.
    """
    for turn in range(1, max_turns + 1):
        log(f"Turn {turn}/{max_turns} — calling provider")
        resp: dict[str, Any] = provider.complete(
            system_prompt=system_prompt, messages=messages, tools=tools
        )
        stop_reason: str = resp.get("stop_reason", "")
        content_blocks: list[dict[str, Any]] = resp.get("content", [])

        # Append assistant turn verbatim — the API requires us to echo back
        # the same content blocks (including tool_use ids) on the next call.
        messages.append({"role": "assistant", "content": content_blocks})

        tool_uses: list[dict[str, Any]] = [
            b for b in content_blocks if b.get("type") == "tool_use"
        ]
        if not tool_uses:
            log(f"Stop reason: {stop_reason} (no tool calls — ending)")
            break

        tool_results: list[dict[str, Any]] = []
        for use in tool_uses:
            tool_name: str = use.get("name", "")
            tool_args: dict[str, Any] = use.get("input", {})
            log(
                f"  → {tool_name}("
                f"{json.dumps(redact_for_log(tool_args))[:MAX_TOOL_LOG_PREVIEW_CHARS]})"
            )
            result_text: str = execute_tool(tool_name, tool_args, state)
            tool_results.append(
                {
                    "type": "tool_result",
                    "tool_use_id": use.get("id"),
                    "content": result_text,
                }
            )

        # Prune BEFORE appending the new tool_results so the just-arrived
        # turn-pair is never at risk of being dropped on the boundary, AND
        # always drop in pairs of 2 (assistant + tool_results) so we don't
        # leave an orphan tool_result whose `tool_use_id` no longer has a
        # matching `tool_use` block in any preceding message — which the
        # Anthropic API rejects with `messages.X.content.Y: unexpected
        # tool_use_id found in tool_result blocks`.
        pair_target: int = 2 * MAX_CONVERSATION_TURNS_RETAINED
        while len(messages) > 1 + pair_target:
            del messages[1:3]
            log("Pruned 1 turn-pair (2 messages) to bound token usage")

        messages.append({"role": "user", "content": tool_results})

        if state.final_summary is not None:
            log("submit_review captured — terminating loop")
            break
    else:
        log(f"Reached MAX_TURNS={max_turns} without an explicit submit_review")


# ---------------------------------------------------------------------------
# Tracking comment
# ---------------------------------------------------------------------------


def _tracking_marker_header(provider: str) -> str:
    """First line(s) of every tracking comment: the review marker, plus the
    per-provider marker when `provider` is set (enables provider-scoped
    `collapse-previous`)."""
    if provider:
        return f"{REVIEW_MARKER}\n{provider_marker(provider)}"
    return REVIEW_MARKER


def render_tracking_body_working(
    head_sha: str, *, collapse_previous: bool, provider: str = ""
) -> str:
    """The initial 'Working…' tracking-comment body."""
    collapsed_note: str = (
        " Previous reviews on this PR have been collapsed as outdated."
        if collapse_previous
        else ""
    )
    return (
        f"{_tracking_marker_header(provider)}\n"
        f"### AI review for `{head_sha[:7]}` — _Working…_\n\n"
        f"Full SHA: `{head_sha}`\n\n"
        f"Reviewing the latest pushed changes.{collapsed_note}"
    )


def render_tracking_body_done(
    *,
    head_sha: str,
    review_url: str,
    inline_attached: int,
    inline_dropped: int,
    severity: str,
    blocked: bool,
    block_reason: str,
    provider: str = "",
) -> str:
    """The terminal 'done' tracking-comment body."""
    status_emoji: str = "✅" if not blocked else "🚫"
    block_line: str = (
        f"\n\n**Strictness gate:** 🚫 {block_reason}"
        if blocked
        else f"\n\n**Strictness gate:** ✅ {block_reason}"
    )
    inline_line: str
    if inline_dropped:
        inline_line = (
            f"_{inline_attached} inline comment(s) attached; "
            f"{inline_dropped} dropped — GitHub rejected them with HTTP 422 "
            "(line outside the diff). See the workflow logs for the original "
            "payload._"
        )
    else:
        inline_line = f"_{inline_attached} inline comment(s) attached._"
    return (
        f"{_tracking_marker_header(provider)}\n"
        f"### AI review for `{head_sha[:7]}` — {status_emoji} done\n\n"
        f"[View review →]({review_url})\n\n"
        f"**Highest severity:** `{severity}`{block_line}\n\n"
        f"{inline_line}"
    )


def render_tracking_body_failed(
    *, head_sha: str, error: str, provider: str = ""
) -> str:
    """The terminal 'failed' tracking-comment body.

    The error text can carry CLI stderr/stdout tails (see `_invoke_cli_agent`),
    so it is passed through `scrub_secrets` before being embedded in this
    public comment.
    """
    safe_error: str = scrub_secrets(error)[:MAX_TRACKING_ERROR_CHARS]
    return (
        f"{_tracking_marker_header(provider)}\n"
        f"### AI review for `{head_sha[:7]}` — ❌ failed\n\n"
        f"```\n{safe_error}\n```\n\n"
        "_See the workflow logs for the full traceback._"
    )


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> int:
    # ------------------------------------------------------------------
    # Load + validate environment
    # ------------------------------------------------------------------
    provider_id: str = os.environ.get("AIPRR_PROVIDER", "anthropic").strip()
    api_key: str = os.environ.get("AIPRR_API_KEY", "").strip()
    gh_token: str = os.environ.get("AIPRR_GH_TOKEN", "").strip()
    repo: str = os.environ.get("AIPRR_REPO", "").strip()
    pr_number_raw: str = os.environ.get("AIPRR_PR_NUMBER", "").strip()
    head_sha: str = os.environ.get("AIPRR_HEAD_SHA", "").strip()
    base_ref: str = (
        os.environ.get("AIPRR_BASE_REF", "").strip() or DEFAULT_BASE_REF
    )
    action_path: str = os.environ.get("AIPRR_ACTION_PATH", "").strip()

    if not (api_key and gh_token and repo and pr_number_raw and head_sha):
        log(
            "Missing required env (AIPRR_API_KEY, AIPRR_GH_TOKEN, AIPRR_REPO, "
            "AIPRR_PR_NUMBER, AIPRR_HEAD_SHA). Aborting."
        )
        write_all_outputs(skipped=False)
        return 1
    # Register the two secrets so their literal values are scrubbed from any
    # text that reaches a public PR comment / review body (see scrub_secrets).
    register_secret(api_key)
    register_secret(gh_token)
    pr_number: int = int(pr_number_raw)

    model: str = (
        os.environ.get("AIPRR_MODEL", "").strip()
        or DEFAULT_MODELS.get(provider_id, "")
    )
    if not model:
        log(f"No default model for provider {provider_id!r} — aborting.")
        write_all_outputs(skipped=False)
        return 1

    prompt_file: str = os.environ.get("AIPRR_PROMPT_FILE", "").strip()
    prompt_extension_file: str = os.environ.get(
        "AIPRR_PROMPT_EXTENSION_FILE", ""
    ).strip()
    label_gate: str = os.environ.get("AIPRR_LABEL_GATE", "").strip()
    applied_label: str = os.environ.get("AIPRR_APPLIED_LABEL", "").strip()
    collapse_previous: bool = parse_bool(
        os.environ.get("AIPRR_COLLAPSE_PREVIOUS", "true"), default=True
    )
    tracking_comment_enabled: bool = parse_bool(
        os.environ.get("AIPRR_TRACKING_COMMENT", "true"), default=True
    )
    strictness: str = (
        os.environ.get("AIPRR_STRICTNESS", STRICTNESS_LENIENT).strip()
        or STRICTNESS_LENIENT
    )
    max_inline_comments: int = int(
        os.environ.get("AIPRR_MAX_INLINE_COMMENTS", DEFAULT_MAX_INLINE_COMMENTS)
        or DEFAULT_MAX_INLINE_COMMENTS
    )
    max_turns: int = int(
        os.environ.get("AIPRR_MAX_TURNS", DEFAULT_MAX_TURNS)
        or DEFAULT_MAX_TURNS
    )

    # PR description review (v1.2.0+)
    pr_desc_mode: str = (
        os.environ.get(
            "AIPRR_PR_DESCRIPTION_MODE", PR_DESC_MODE_OFF
        ).strip()
        or PR_DESC_MODE_OFF
    )
    if pr_desc_mode not in PR_DESC_MODES:
        log(
            f"Unknown pr-description-mode {pr_desc_mode!r} — falling back "
            f"to {PR_DESC_MODE_OFF}"
        )
        pr_desc_mode = PR_DESC_MODE_OFF
    pr_desc_min_length: int = int(
        os.environ.get(
            "AIPRR_PR_DESCRIPTION_MIN_LENGTH",
            str(PR_DESC_MIN_LENGTH_DEFAULT),
        )
        or PR_DESC_MIN_LENGTH_DEFAULT
    )

    # PR complexity labeling (v1.2.0+)
    complexity_labels_enabled: bool = parse_bool(
        os.environ.get("AIPRR_COMPLEXITY_LABELS_ENABLED", "false"),
        default=False,
    )
    complexity_label_prefix: str = (
        os.environ.get(
            "AIPRR_COMPLEXITY_LABEL_PREFIX",
            PR_COMPLEXITY_LABEL_PREFIX_DEFAULT,
        ).strip()
        or PR_COMPLEXITY_LABEL_PREFIX_DEFAULT
    )

    # Trigger-mode resolution (v1.2.0+). Empty default falls back to
    # `always` (or `label-required` when `label-gate` is set) for full
    # back-compat with v1.1 workflows.
    trigger_mode_raw: str = (
        os.environ.get("AIPRR_TRIGGER_MODE", "").strip()
    )
    if trigger_mode_raw:
        trigger_mode: str = trigger_mode_raw
        if trigger_mode not in TRIGGER_MODES:
            log(
                f"Unknown trigger-mode {trigger_mode!r} — falling back to "
                f"{TRIGGER_ALWAYS}"
            )
            trigger_mode = TRIGGER_ALWAYS
    else:
        trigger_mode = (
            TRIGGER_LABEL_REQUIRED if label_gate else TRIGGER_ALWAYS
        )

    event_action: str = _read_github_event_action()
    event_label: str = _read_github_event_label()

    log(
        f"Reviewing {repo}#{pr_number} @ {head_sha[:7]} with "
        f"{provider_id}/{model} (strictness={strictness}, "
        f"trigger-mode={trigger_mode})"
    )

    # ------------------------------------------------------------------
    # Author-association gate (v1.3.0+) — cheapest gate, runs first so a
    # denied PR never consumes an LLM API call. Defaults to write-tier
    # only, which is the safe baseline for public open-source repos.
    # ------------------------------------------------------------------
    author_gate_raw: str = os.environ.get(
        "AIPRR_AUTHOR_ASSOCIATION",
        ",".join(AUTHOR_ASSOCIATION_WRITE_TIER),
    )
    pr_author_association: str = _read_github_event_pr_author_association()
    author_decision: AuthorAssociationDecision = (
        resolve_author_association_gate(
            gate=author_gate_raw,
            actual_association=pr_author_association,
        )
    )
    log(f"Author-association gate: {author_decision.reason}")
    if not author_decision.should_run:
        log(
            "Skipping review — this is the abuse-prevention default. To "
            "allow this author, add their association to "
            "`author-association` (see docs/SECURITY.md § "
            "'Author-association gate') or set the input to an empty "
            "string to disable the gate entirely."
        )
        write_all_outputs(skipped=True)
        return 0

    # ------------------------------------------------------------------
    # Trigger evaluation (v1.2.0+) — subsumes the v1.x `label-gate` block
    # ------------------------------------------------------------------
    label_toggle_generation: int = 0
    last_reviewed_generation: int = 0
    if trigger_mode in (TRIGGER_LABEL_REQUIRED, TRIGGER_LABEL_ONCE):
        try:
            label_toggle_generation = count_label_events(
                token=gh_token,
                repo=repo,
                pr_number=pr_number,
                label=label_gate,
            )
        except Exception as e:  # noqa: BLE001 — best-effort GH API call
            log(f"Could not count label events (assuming 0): {e}")

    if trigger_mode == TRIGGER_LABEL_ONCE:
        prior_state: dict[str, Any] = _read_existing_tracking_state(
            token=gh_token, repo=repo, pr_number=pr_number
        )
        try:
            last_reviewed_generation = int(
                prior_state.get("label_toggle_generation", 0) or 0
            )
        except (TypeError, ValueError):
            last_reviewed_generation = 0

    try:
        current_labels_raw: list[dict[str, Any]] = (
            gh_request(
                "GET",
                f"/repos/{repo.split('/')[0]}/{repo.split('/')[1]}"
                f"/pulls/{pr_number}",
                token=gh_token,
            )
            .get("labels", [])
            or []
        )
        current_labels: list[str] = [
            (lbl.get("name") or "") for lbl in current_labels_raw
        ]
    except Exception as e:  # noqa: BLE001 — best-effort GH API call
        log(f"Could not read PR labels for trigger check: {e}")
        current_labels = []

    trigger_decision: TriggerDecision = resolve_trigger_action(
        trigger_mode=trigger_mode,
        event_action=event_action,
        event_label=event_label,
        label_gate=label_gate,
        current_labels=current_labels,
        label_toggle_generation=label_toggle_generation,
        last_reviewed_generation=last_reviewed_generation,
    )
    log(
        f"Trigger decision: should_run={trigger_decision.should_run} "
        f"({trigger_decision.reason})"
    )
    if not trigger_decision.should_run:
        write_all_outputs(skipped=True)
        return 0

    # ------------------------------------------------------------------
    # Collapse previous bot reviews/comments as outdated
    # ------------------------------------------------------------------
    if collapse_previous:
        try:
            # v1.2.0+: pass repo + pr_number so the fallback chain in
            # `gh_get_authenticated_login` can marker-scan for the prior
            # bot's login when the built-in `GITHUB_TOKEN` refuses
            # `/user` (the fix for the silent 403 that broke this
            # feature for every workflow-token consumer).
            bot_login: str = gh_get_authenticated_login(
                gh_token, repo=repo, pr_number=pr_number
            )
            log(f"Authenticated as: {bot_login}")
            gh_collapse_previous_reviews(
                token=gh_token,
                repo=repo,
                pr_number=pr_number,
                bot_login=bot_login,
                # Scope collapsing to THIS provider's prior artefacts so
                # concurrent multi-provider reviews don't collapse each other.
                provider_marker_text=provider_marker(provider_id),
            )
        except Exception as e:  # noqa: BLE001
            log(f"Collapse-previous step failed (non-fatal): {e}")

    # ------------------------------------------------------------------
    # Tracking spinner comment
    # ------------------------------------------------------------------
    tracking_id: int = 0
    if tracking_comment_enabled:
        try:
            tracking_id = gh_post_issue_comment(
                token=gh_token,
                repo=repo,
                pr_number=pr_number,
                body=render_tracking_body_working(
                    head_sha,
                    collapse_previous=collapse_previous,
                    provider=provider_id,
                ),
            )
            log(f"Tracking comment id: {tracking_id}")
        except Exception as e:  # noqa: BLE001
            log(f"Could not post tracking comment (non-fatal): {e}")
            tracking_id = 0

    # ------------------------------------------------------------------
    # Resolve and read system prompt
    # ------------------------------------------------------------------
    # Composition matrix:
    #   1) neither set              → bundled default
    #   2) prompt_file only         → prompt_file replaces default
    #   3) prompt_extension only    → default + "\n\n---\n\n" + extension
    #   4) both set                 → prompt_file + "\n\n---\n\n" + extension
    # The `---` separator gives the model an unambiguous boundary between
    # the base prompt and the consumer's overrides.
    resolved_prompt_path: Path
    if prompt_file:
        resolved_prompt_path = Path(prompt_file)
    else:
        resolved_prompt_path = Path(action_path) / "prompts" / "default.md"
    try:
        base_prompt: str = resolved_prompt_path.read_text(encoding="utf-8")
        log(f"Base prompt loaded from {resolved_prompt_path}")
    except OSError as e:
        log(f"Failed to read prompt file {resolved_prompt_path!r}: {e}")
        gh_update_issue_comment(
            token=gh_token,
            repo=repo,
            comment_id=tracking_id,
            body=render_tracking_body_failed(
                head_sha=head_sha,
                error=f"Could not read prompt file: {e}",
                provider=provider_id,
            ),
        )
        write_all_outputs(skipped=False)
        return 1

    extension_text: str = ""
    if prompt_extension_file:
        extension_path: Path = Path(prompt_extension_file)
        try:
            extension_text = extension_path.read_text(encoding="utf-8")
            log(f"Prompt extension appended from {extension_path}")
        except OSError as e:
            log(
                f"Failed to read prompt extension file "
                f"{extension_path!r}: {e}"
            )
            gh_update_issue_comment(
                token=gh_token,
                repo=repo,
                comment_id=tracking_id,
                body=render_tracking_body_failed(
                    head_sha=head_sha,
                    error=f"Could not read prompt extension file: {e}",
                    provider=provider_id,
                ),
            )
            write_all_outputs(skipped=False)
            return 1
    system_prompt: str = compose_system_prompt(base_prompt, extension_text)

    # ------------------------------------------------------------------
    # Fetch PR + run agentic loop, all wrapped so failures hit the spinner
    # ------------------------------------------------------------------
    state: ReviewState = ReviewState(max_inline_comments=max_inline_comments)
    try:
        pr_ctx: PRContext = fetch_pr_context(
            repo=repo, pr_number=pr_number, base_ref=base_ref, token=gh_token
        )
        log(
            f"PR loaded: +{pr_ctx.additions}/-{pr_ctx.deletions} across "
            f"{len(pr_ctx.changed_files)} files"
        )

        # PR description verdict (only computed when the mode is not `off`,
        # to keep the log clean when the feature is disabled).
        description_verdict: DescriptionVerdict = DescriptionVerdict(
            is_adequate=True, reason=""
        )
        if pr_desc_mode != PR_DESC_MODE_OFF:
            description_verdict = evaluate_pr_description(
                pr_ctx.body, min_length=pr_desc_min_length
            )
            log(
                f"PR description: mode={pr_desc_mode}, "
                f"adequate={description_verdict.is_adequate}"
            )

        provider: Provider | AgentRunnerProvider = build_provider(
            provider_id, api_key=api_key, model=model
        )

        # v1.2.0 dispatch caveat: the `set_pr_description` and
        # `set_pr_complexity` tools are only exposed on the chat-completions
        # path (they piggyback on the built-in tool-use loop). On agent-
        # runner providers, `state.proposed_*` stays empty and the post-loop
        # PATCH/label blocks silently no-op. Warn the consumer so they
        # don't silently pay for a review that can't do what they asked.
        # Roadmap: extend findings.json schema to carry these signals from
        # the CLI back to the runtime (see docs/PR_METADATA_CHECKS.md).
        agent_runner_warning: str = build_agent_runner_noop_warning(
            provider_id=provider_id,
            is_agent_runner=isinstance(provider, AgentRunnerProvider),
            pr_desc_mode=pr_desc_mode,
            complexity_labels_enabled=complexity_labels_enabled,
        )
        if agent_runner_warning:
            log(agent_runner_warning)

        if isinstance(provider, AgentRunnerProvider):
            # Agent-runner path: vendor CLI owns the tool-use loop. Verify the
            # CLI is on PATH (defensive — the composite step should have
            # installed it), then invoke and parse findings.json.
            provider.install()
            workspace: Path = Path.cwd()
            result: ReviewResult = provider.run_review(
                pr_context=pr_ctx,
                review_instructions=system_prompt,
                workspace=workspace,
                output_dir=workspace,
            )
            # Enforce max_inline_comments on the agent-runner path too. The
            # tool handler enforces this for chat-completions providers; the
            # cap is a documented safety control (docs/SECURITY.md) that
            # applies to every provider family.
            if len(result.findings) > max_inline_comments:
                dropped: int = len(result.findings) - max_inline_comments
                log(
                    f"Agent-runner provider produced {len(result.findings)} "
                    f"findings; capping to max-inline-comments="
                    f"{max_inline_comments} ({dropped} dropped)"
                )
                result.findings = result.findings[:max_inline_comments]
                # Recompute overall_severity — dropping the tail may lower it.
                result.overall_severity = overall_severity(
                    [f.severity for f in result.findings]
                )
        else:
            # Chat-completions path: this action owns the tool-use loop.
            messages: list[dict[str, Any]] = [
                {"role": "user", "content": render_user_prompt(pr_ctx)}
            ]
            # Expose set_pr_description only in autocomplete mode; expose
            # set_pr_complexity only when complexity labeling is enabled.
            tools: list[dict[str, Any]] = tools_schema(
                max_inline_comments,
                allow_set_pr_description=(
                    pr_desc_mode == PR_DESC_MODE_AUTOCOMPLETE
                    and not description_verdict.is_adequate
                    and PR_DESC_AUTOCOMPLETE_MARKER not in (pr_ctx.body or "")
                ),
                allow_set_pr_complexity=complexity_labels_enabled,
            )

            drive_review(
                provider=provider,
                system_prompt=system_prompt,
                messages=messages,
                tools=tools,
                state=state,
                max_turns=max_turns,
            )
            result = state_to_review_result(state)
    except Exception as e:  # noqa: BLE001
        log(f"Agentic loop crashed: {type(e).__name__}: {e}")
        gh_update_issue_comment(
            token=gh_token,
            repo=repo,
            comment_id=tracking_id,
            body=render_tracking_body_failed(
                head_sha=head_sha,
                error=f"{type(e).__name__}: {e}",
                provider=provider_id,
            ),
        )
        write_all_outputs(skipped=False)
        return 1

    # ------------------------------------------------------------------
    # Post the review (with 422 fallback)
    # ------------------------------------------------------------------
    if not result.summary:
        result.summary = (
            "## Code Review Summary\n\n"
            "_The reviewer hit the turn cap without producing a structured "
            "summary. Inline comments (if any) are still attached below._"
        )
        log("No submit_review captured — posting fallback summary")

    # Append the PR description verdict to the summary when warn/block mode
    # flagged the description. In autocomplete mode we don't warn — the
    # feature *fixed* the description.
    if (
        pr_desc_mode in (PR_DESC_MODE_WARN, PR_DESC_MODE_BLOCK)
        and not description_verdict.is_adequate
    ):
        result.summary = (
            result.summary.rstrip()
            + "\n\n---\n\n"
            + "> **PR description check**: "
            + description_verdict.reason
            + (
                "  (mode: `block` — the check will fail on this)"
                if pr_desc_mode == PR_DESC_MODE_BLOCK
                else "  (mode: `warn` — advisory only)"
            )
        )

    # Scrub any registered secret value out of everything that is about to be
    # posted publicly — the summary and each inline-comment body. On the
    # agent-runner path these strings originate from a vendor CLI that holds
    # an API key in its env; this is the last line of defence before a leaked
    # key could land in a public comment (see docs/SECURITY.md).
    result.summary = scrub_secrets(result.summary)
    for _finding in result.findings:
        _finding.body = scrub_secrets(_finding.body)

    # Embed the provider marker (an invisible HTML comment) at the top of the
    # review body so `collapse-previous` can scope to this provider's own
    # prior reviews — see provider_marker / gh_collapse_previous_reviews.
    result.summary = f"{provider_marker(provider_id)}\n\n{result.summary}"

    log(
        f"Submitting review: {len(result.findings)} inline comment(s), "
        f"{len(result.summary)} chars of summary"
    )

    try:
        review, dropped_inline = gh_submit_review_with_fallback(
            token=gh_token,
            repo=repo,
            pr_number=pr_number,
            head_sha=head_sha,
            result=result,
        )
    except Exception as e:  # noqa: BLE001
        log(f"Failed to post review: {e}")
        gh_update_issue_comment(
            token=gh_token,
            repo=repo,
            comment_id=tracking_id,
            body=render_tracking_body_failed(
                head_sha=head_sha,
                error=f"Could not post the review: {e}",
                provider=provider_id,
            ),
        )
        write_all_outputs(skipped=False)
        return 1

    review_url: str = str(review.get("html_url", ""))
    log(f"Review posted: {review_url}")

    # ------------------------------------------------------------------
    # PR description autocomplete (v1.2.0+) — best-effort PATCH.
    # ------------------------------------------------------------------
    if (
        pr_desc_mode == PR_DESC_MODE_AUTOCOMPLETE
        and not description_verdict.is_adequate
        and PR_DESC_AUTOCOMPLETE_MARKER not in (pr_ctx.body or "")
        and state.proposed_pr_description
    ):
        new_body: str = (
            state.proposed_pr_description.rstrip()
            + "\n\n"
            + PR_DESC_AUTOCOMPLETE_MARKER
        )
        try:
            gh_patch_pr_body(
                token=gh_token,
                repo=repo,
                pr_number=pr_number,
                new_body=new_body,
            )
            log("PR description autocompleted by the reviewer.")
        except Exception as e:  # noqa: BLE001 — best-effort GH API call
            log(f"Could not PATCH PR body (non-fatal): {e}")

    # ------------------------------------------------------------------
    # PR complexity labeling (v1.2.0+) — best-effort label update.
    # ------------------------------------------------------------------
    if complexity_labels_enabled and state.proposed_pr_complexity:
        new_label: str = (
            f"{complexity_label_prefix}{state.proposed_pr_complexity}"
        )
        try:
            # Remove any prior `complexity:*` label so the labels reflect
            # the current review's assessment, not a stale one.
            gh_remove_labels_by_prefix(
                token=gh_token,
                repo=repo,
                pr_number=pr_number,
                prefix=complexity_label_prefix,
                except_label=new_label,
            )
            gh_apply_label(
                token=gh_token,
                repo=repo,
                pr_number=pr_number,
                label=new_label,
            )
            log(f"Applied complexity label {new_label!r}")
        except Exception as e:  # noqa: BLE001 — best-effort GH API call
            log(f"Could not apply complexity label (non-fatal): {e}")

    # ------------------------------------------------------------------
    # Strictness gate
    # ------------------------------------------------------------------
    severity: str = result.overall_severity
    blocked, block_reason = evaluate_strictness(severity, strictness)

    # PR description gate — orthogonal to the strictness gate. When
    # `pr-description-mode: block`, an inadequate description forces
    # `blocked=True` regardless of inline-comment severity.
    if (
        pr_desc_mode == PR_DESC_MODE_BLOCK
        and not description_verdict.is_adequate
    ):
        blocked = True
        block_reason = (
            f"pr-description-mode=block: {description_verdict.reason}"
        )
        log(f"PR description gate: blocking — {description_verdict.reason}")
    elif (
        pr_desc_mode in (PR_DESC_MODE_WARN, PR_DESC_MODE_BLOCK)
        and not description_verdict.is_adequate
    ):
        log(f"PR description gate: warning — {description_verdict.reason}")

    log(
        f"Severity: {severity}; strictness: {strictness}; blocked: {blocked} "
        f"({block_reason})"
    )

    attached_inline: int = len(result.findings) - dropped_inline
    tracking_body: str = render_tracking_body_done(
        head_sha=head_sha,
        review_url=review_url,
        inline_attached=attached_inline,
        inline_dropped=dropped_inline,
        severity=severity,
        blocked=blocked,
        block_reason=block_reason,
        provider=provider_id,
    )
    # For `label-once` mode, embed the label-toggle generation so the
    # next run can detect "already reviewed this label application".
    if trigger_mode == TRIGGER_LABEL_ONCE and not blocked:
        tracking_body = write_trigger_state(
            tracking_body,
            {"label_toggle_generation": label_toggle_generation},
        )
    gh_update_issue_comment(
        token=gh_token,
        repo=repo,
        comment_id=tracking_id,
        body=tracking_body,
    )

    # ------------------------------------------------------------------
    # Apply success label (only if not blocked)
    # ------------------------------------------------------------------
    if applied_label and not blocked:
        gh_apply_label(
            token=gh_token,
            repo=repo,
            pr_number=pr_number,
            label=applied_label,
        )
        log(f"Applied label {applied_label!r}")
    elif applied_label and blocked:
        log(f"Skipped applying {applied_label!r} — strictness gate blocked")

    # ------------------------------------------------------------------
    # Action outputs
    # ------------------------------------------------------------------
    write_all_outputs(
        skipped=False,
        severity=severity,
        inline_attached=attached_inline,
        inline_dropped=dropped_inline,
        blocked=blocked,
        review_url=review_url,
    )

    # Exit code 2 = blocked, so the GitHub check turns red but we keep
    # exit code 1 reserved for hard failures.
    return 2 if blocked else 0


if __name__ == "__main__":
    sys.exit(main())
