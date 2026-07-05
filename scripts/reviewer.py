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
                            bundled `prompts/default.md`).
    AIPRR_LABEL_GATE         Required label, or empty for no gate.
    AIPRR_APPLIED_LABEL      Label to apply on success, or empty.
    AIPRR_COLLAPSE_PREVIOUS  `true`/`false`.
    AIPRR_TRACKING_COMMENT   `true`/`false`.
    AIPRR_STRICTNESS         `lenient` | `block-on-critical` | `block-on-warning`.
    AIPRR_MAX_INLINE_COMMENTS  Integer cap.
    AIPRR_MAX_TURNS          Integer cap.
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
import subprocess
import sys
import time
import urllib.error
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
#   1. New entry here for the default model id.
#   2. New `Provider` implementation below.
#   3. New branch in `build_provider()`.
DEFAULT_MODELS: dict[str, str] = {
    "anthropic": "claude-sonnet-4-6",
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
VALID_STRICTNESS: tuple[str, ...] = (
    STRICTNESS_LENIENT,
    STRICTNESS_BLOCK_CRITICAL,
    STRICTNESS_BLOCK_WARNING,
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


def truncate_for_tool(text: str, *, label: str) -> str:
    """Cap tool output so a single bad command can't blow up the prompt."""
    if len(text.encode("utf-8")) <= MAX_TOOL_OUTPUT_BYTES:
        return text
    truncated: str = text.encode("utf-8")[:MAX_TOOL_OUTPUT_BYTES].decode(
        "utf-8", errors="ignore"
    )
    return (
        f"{truncated}\n\n[output truncated at {MAX_TOOL_OUTPUT_BYTES} bytes — "
        f"narrow your {label} call (e.g. add path/glob/limit) for full content]"
    )


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


def gh_get_authenticated_login(token: str) -> str:
    """Return the login of the user the token is authenticated as."""
    me: dict[str, Any] = gh_request("GET", "/user", token=token)
    return str(me.get("login", ""))


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


def gh_collapse_previous_reviews(
    *, token: str, repo: str, pr_number: int, bot_login: str
) -> int:
    """Mark prior bot reviews/comments as `OUTDATED` via GraphQL.

    Returns the number of nodes minimized. Best-effort: failures are logged
    but the review still proceeds.

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
        "        nodes { id isMinimized author { login } }"
        "      }"
        "      reviews(first:$page) {"
        "        nodes {"
        "          id"
        "          isMinimized"
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

    targets: list[str] = []
    for c in issue_comments:
        if (
            (c.get("author") or {}).get("login") == bot_login
            and not c.get("isMinimized", False)
        ):
            targets.append(c["id"])
    for r in reviews:
        if (r.get("author") or {}).get("login") == bot_login:
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


def render_user_prompt(ctx: PRContext) -> str:
    """Produce the first user message — PR metadata + diff."""
    files_block: str = "\n".join(
        f"- {f['path']} ({f['status']}) +{f['additions']}/-{f['deletions']}"
        for f in ctx.changed_files
    )
    body_block: str = ctx.body.strip() or "(no body)"
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
        "Review this PR using the system prompt's rubric. Use `read_file`, "
        "`grep`, and `glob` to verify findings against the broader codebase "
        "before reporting them. Queue inline comments with "
        "`post_inline_comment` (only on lines that appear in the diff) and "
        "set the `severity` argument honestly — it drives the gating "
        "behaviour configured by the consumer. When you're done, call "
        "`submit_review` exactly once with the summary markdown — that "
        "signals the end of the session and posts the review."
    )


# ---------------------------------------------------------------------------
# Tool definitions
# ---------------------------------------------------------------------------


def tools_schema(max_inline_comments: int) -> list[dict[str, Any]]:
    """JSONSchema for every tool the model can call."""
    return [
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
    path: Path = safe_repo_path(rel)
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
        return f"Error: unknown tool `{name}`"
    except Exception as e:  # noqa: BLE001 — surface to model rather than crash
        return f"Tool `{name}` raised {type(e).__name__}: {e}"


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


def render_tracking_body_working(
    head_sha: str, *, collapse_previous: bool
) -> str:
    """The initial 'Working…' tracking-comment body."""
    collapsed_note: str = (
        " Previous reviews on this PR have been collapsed as outdated."
        if collapse_previous
        else ""
    )
    return (
        f"{REVIEW_MARKER}\n"
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
        f"{REVIEW_MARKER}\n"
        f"### AI review for `{head_sha[:7]}` — {status_emoji} done\n\n"
        f"[View review →]({review_url})\n\n"
        f"**Highest severity:** `{severity}`{block_line}\n\n"
        f"{inline_line}"
    )


def render_tracking_body_failed(*, head_sha: str, error: str) -> str:
    """The terminal 'failed' tracking-comment body."""
    return (
        f"{REVIEW_MARKER}\n"
        f"### AI review for `{head_sha[:7]}` — ❌ failed\n\n"
        f"```\n{error[:MAX_TRACKING_ERROR_CHARS]}\n```\n\n"
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

    log(
        f"Reviewing {repo}#{pr_number} @ {head_sha[:7]} with "
        f"{provider_id}/{model} (strictness={strictness})"
    )

    # ------------------------------------------------------------------
    # Label gate — exit early if missing the required label
    # ------------------------------------------------------------------
    if label_gate:
        try:
            present: bool = gh_pr_has_label(
                token=gh_token,
                repo=repo,
                pr_number=pr_number,
                label=label_gate,
            )
        except Exception as e:  # noqa: BLE001
            log(f"Could not read PR labels for gate check: {e}")
            present = False
        if not present:
            log(
                f"Label gate: PR does not carry {label_gate!r} — skipping review."
            )
            write_all_outputs(skipped=True)
            return 0

    # ------------------------------------------------------------------
    # Collapse previous bot reviews/comments as outdated
    # ------------------------------------------------------------------
    if collapse_previous:
        try:
            bot_login: str = gh_get_authenticated_login(gh_token)
            log(f"Authenticated as: {bot_login}")
            gh_collapse_previous_reviews(
                token=gh_token,
                repo=repo,
                pr_number=pr_number,
                bot_login=bot_login,
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
                    head_sha, collapse_previous=collapse_previous
                ),
            )
            log(f"Tracking comment id: {tracking_id}")
        except Exception as e:  # noqa: BLE001
            log(f"Could not post tracking comment (non-fatal): {e}")
            tracking_id = 0

    # ------------------------------------------------------------------
    # Resolve and read system prompt
    # ------------------------------------------------------------------
    resolved_prompt_path: Path
    if prompt_file:
        resolved_prompt_path = Path(prompt_file)
    else:
        resolved_prompt_path = Path(action_path) / "prompts" / "default.md"
    try:
        system_prompt: str = resolved_prompt_path.read_text(encoding="utf-8")
        log(f"Prompt loaded from {resolved_prompt_path}")
    except OSError as e:
        log(f"Failed to read prompt file {resolved_prompt_path!r}: {e}")
        gh_update_issue_comment(
            token=gh_token,
            repo=repo,
            comment_id=tracking_id,
            body=render_tracking_body_failed(
                head_sha=head_sha,
                error=f"Could not read prompt file: {e}",
            ),
        )
        write_all_outputs(skipped=False)
        return 1

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

        provider: Provider | AgentRunnerProvider = build_provider(
            provider_id, api_key=api_key, model=model
        )

        if isinstance(provider, AgentRunnerProvider):
            # Agent-runner path: vendor CLI owns the tool-use loop, we
            # receive structured findings via file-based contract.
            # (Concrete AgentRunnerProvider implementations land in later
            # tasks; the abstraction lives here to unify the submission path.)
            raise RuntimeError(
                f"AgentRunnerProvider ({type(provider).__name__}) invoked but "
                "no concrete implementation registered — this is a wiring bug."
            )

        # Chat-completions path: this action owns the tool-use loop.
        messages: list[dict[str, Any]] = [
            {"role": "user", "content": render_user_prompt(pr_ctx)}
        ]
        tools: list[dict[str, Any]] = tools_schema(max_inline_comments)

        drive_review(
            provider=provider,
            system_prompt=system_prompt,
            messages=messages,
            tools=tools,
            state=state,
            max_turns=max_turns,
        )
        result: ReviewResult = state_to_review_result(state)
    except Exception as e:  # noqa: BLE001
        log(f"Agentic loop crashed: {type(e).__name__}: {e}")
        gh_update_issue_comment(
            token=gh_token,
            repo=repo,
            comment_id=tracking_id,
            body=render_tracking_body_failed(
                head_sha=head_sha,
                error=f"{type(e).__name__}: {e}",
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
            ),
        )
        write_all_outputs(skipped=False)
        return 1

    review_url: str = str(review.get("html_url", ""))
    log(f"Review posted: {review_url}")

    # ------------------------------------------------------------------
    # Strictness gate
    # ------------------------------------------------------------------
    severity: str = result.overall_severity
    blocked, block_reason = evaluate_strictness(severity, strictness)
    log(
        f"Severity: {severity}; strictness: {strictness}; blocked: {blocked} "
        f"({block_reason})"
    )

    attached_inline: int = len(result.findings) - dropped_inline
    gh_update_issue_comment(
        token=gh_token,
        repo=repo,
        comment_id=tracking_id,
        body=render_tracking_body_done(
            head_sha=head_sha,
            review_url=review_url,
            inline_attached=attached_inline,
            inline_dropped=dropped_inline,
            severity=severity,
            blocked=blocked,
            block_reason=block_reason,
        ),
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
