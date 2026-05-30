# Architecture

A high-level walk-through of how AI PR Reviewer is put together. For input/output configuration see the [README](../README.md); this doc is for contributors who need a mental model of the runtime.

## Topology

```
┌──────────────────────────────────────────────────────────────────┐
│  Consumer's workflow (any repo)                                  │
│  ┌────────────────────────────────────────────────────────────┐  │
│  │  jobs.review.steps:                                         │  │
│  │    - actions/checkout (fetch-depth: 0)                      │  │
│  │    - DailybotHQ/ai-pr-reviewer@v1                           │  │
│  └─────────────────────┬───────────────────────────────────────┘  │
└────────────────────────┼─────────────────────────────────────────┘
                         │
            (composite action loads here)
                         │
┌────────────────────────▼─────────────────────────────────────────┐
│  action.yml                                                      │
│  ─ Declares public inputs (api-key, prompt-file, strictness…)     │
│  ─ Declares outputs (review-url, severity, blocked…)              │
│  ─ Single step: invokes scripts/reviewer.py with AIPRR_* env       │
└────────────────────────┬─────────────────────────────────────────┘
                         │
┌────────────────────────▼─────────────────────────────────────────┐
│  scripts/reviewer.py  (stdlib only)                               │
│                                                                  │
│   1. Label gate          ── exit 0 if missing                     │
│   2. Collapse previous   ── GraphQL minimizeComment               │
│   3. Tracking comment    ── post spinner with marker              │
│   4. Fetch PR context    ── REST + git diff                       │
│   5. Agentic loop        ── provider.complete() + tools           │
│   6. Submit review       ── REST POST /pulls/N/reviews + 422 retry │
│   7. Apply label         ── if not blocked                        │
│   8. Strictness gate     ── exit 0 / 2                            │
│                                                                  │
│   Provider:  AnthropicProvider (today; OpenAI/Gemini stubs)       │
│   Tools:     read_file, grep, glob, post_inline_comment,          │
│              submit_review                                       │
└──────────────────────────────────────────────────────────────────┘
```

## Components

### `action.yml`

The public contract. Declares every input the consumer can set and every output the consumer can read. The composite-action `runs:` block does one thing: invoke `scripts/reviewer.py` with all inputs forwarded as `AIPRR_*` environment variables.

**Why composite, not Docker:** Docker actions add ~30s of pull time and a second supply chain (the image registry) for zero benefit — the runtime is stdlib Python, which is already on every `ubuntu-latest` runner.

**Why composite, not JS:** the implementation is Python; rewriting to JS would double the maintenance surface. JS actions are appropriate for actions that run on Windows runners (we don't claim to support those).

### `scripts/reviewer.py`

The entire runtime in one file. Sections, in source order:

1. **Constants** — every magic number is a named module-level constant. URLs, timeouts, retry delays, severity ranks.
2. **Logging utilities** — `log()`, `redact_for_log()`, `truncate_for_tool()`. The redaction list is the gate that prevents accidental token leakage in tool-arg logging.
3. **GitHub API helpers** — `gh_request`, `gh_graphql`, `gh_post_issue_comment`, `gh_apply_label`, `gh_collapse_previous_reviews`, `gh_submit_review`, `gh_submit_review_with_fallback`. Pure stdlib `urllib`; no `requests`, no `gh` CLI.
4. **Provider abstraction** — `Provider` base class + `AnthropicProvider`. The contract: `complete(system_prompt, messages, tools) -> dict`. Anthropic-shaped messages are the in-memory representation; future providers translate at the boundary.
5. **PR context** — `PRContext` dataclass + `fetch_pr_context()`. Pulls metadata + the unified diff as the agentic loop's seed user message.
6. **Tool definitions** — `tools_schema()` and the per-tool implementations (`tool_read_file`, `tool_grep`, `tool_glob`, `tool_post_inline_comment`, `tool_submit_review`). Each tool implementation handles its own errors and returns a string for the `tool_result`.
7. **Severity / strictness** — `overall_severity()` aggregates the per-comment severities; `evaluate_strictness()` maps `(severity, strictness)` to a blocking decision.
8. **Agentic loop** — `drive_review()`. Drives `provider.complete()` in a loop, executes any tools the model calls, prunes conversation history in pairs to bound token cost, terminates when the model calls `submit_review` or hits the turn cap.
9. **Tracking comment renderers** — `render_tracking_body_working/done/failed`. Pure functions; emit the marker.
10. **`main()`** — orchestrates the lifecycle: load env, label gate, collapse, tracking, prompt resolution, agentic loop (wrapped in failure-update guards), review submission, label application, strictness gate, action outputs.

### `prompts/default.md`

The bundled default system prompt. Technology-agnostic; opinionated about severity definitions and what *not* to comment on. Consumers can override entirely via the `prompt-file` input.

## Key design decisions

### 1. Stdlib only

The biggest constraint and the biggest feature. Means consumers can install the action with a single `uses:` and never think about a `pip install` step or a Docker pull. Forces the codebase to stay small and audit-friendly.

### 2. Anthropic-shaped in-memory representation

The agentic loop stores messages in Anthropic's content-block format (`{role, content: [{type: "text", ...}, {type: "tool_use", ...}]}`). When we add OpenAI we translate at the provider boundary rather than abstract upward. The trade-off:

- **Pro:** simpler runtime; only one shape to reason about.
- **Con:** the OpenAI provider has more translation work than a hypothetical OpenAI-native runtime would.

For a tool-use agentic loop, the per-turn translation cost is trivial; the runtime simplicity dominates.

### 3. Inline comments queued in memory, posted atomically

The `post_inline_comment` tool doesn't actually call GitHub — it appends to `ReviewState.inline_comments`. Submission happens once, in `gh_submit_review_with_fallback`, with summary + comments in a single `POST /pulls/{n}/reviews`.

**Why:** GitHub's per-comment endpoint (`POST /pulls/{n}/comments`) creates one separate review per call. Batching into a single review keeps the PR conversation tab clean (one entry, not 30) and matches the model's mental model of "one review with N comments".

**Trade-off:** if any one comment has an invalid `line` (anchor outside the diff), GitHub rejects the entire review with HTTP 422. The action handles this with a fallback path: catch 422, retry summary-only, log the original error body, surface the dropped count in the tracking comment.

### 4. Conversation pruning in pairs

The agentic loop prunes conversation history when it grows past `MAX_CONVERSATION_TURNS_RETAINED * 2 + 1` messages. The pruning is in **pairs** (one assistant message + the matching tool_results) rather than one message at a time, because Anthropic's API rejects orphan `tool_use_id`s — a pruned `tool_use` block whose matching `tool_result` is still in the history (or vice versa) crashes the next turn.

### 5. Failure paths always update the tracking comment

The tracking comment is the single source of truth for "what happened to this review". Every error path in `main()` either:
- Updates the tracking comment with a `failed` body before returning a non-zero exit code, OR
- Logs and re-raises so the broad-except wrapper at the top of `main()` does the update.

A "stuck on Working…" tracking comment is a regression; if you add an error path, make sure it transitions the spinner.

### 6. Strictness exits 2, not 1

- Exit 0 — success.
- Exit 2 — the strictness gate blocked the check.
- Exit 1 — hard failure (auth error, prompt file missing, etc.).

Distinguishing 1 from 2 lets the consumer's workflow `if:` clauses tell "the bot didn't run" from "the bot ran and decided this PR shouldn't merge".

### 7. Prompt caching

The Anthropic provider sends the system prompt with `cache_control: ephemeral` so a long custom prompt only pays the full token cost on the first turn of each review. Subsequent turns within the same review (and within the ~5-minute cache TTL) read from cache. This is what makes long, opinionated prompts economically viable.

## What lives where

| Concern | Lives in |
|---|---|
| Public input/output names and defaults | `action.yml` |
| Public input documentation | `README.md` table |
| Detailed user-facing guides | `docs/STRICTNESS.md`, `docs/PROMPTS.md`, `docs/PROVIDERS.md` |
| Runtime constants (limits, timeouts, ranks) | top of `scripts/reviewer.py` |
| Internal env-var contract (`AIPRR_*`) | `action.yml` `env:` block + `scripts/reviewer.py` `main()` |
| Provider abstraction | `Provider` class + `build_provider()` in `scripts/reviewer.py` |
| Default prompt | `prompts/default.md` |
| CI strategy | `.github/workflows/ci.yml`, `.github/workflows/self-review.yml` |
| Release strategy | `.github/workflows/release.yml`, `CHANGELOG.md` |
| AI-agent configuration | `.agents/` (see [AGENTS.md](../AGENTS.md)) |

## What we deliberately don't have

- **No `requirements.txt` / `pyproject.toml`.** Stdlib only; no version pin file because there's nothing to pin.
- **No unit-test framework.** The testing strategy is `py_compile` + dogfooding (see [TESTING_GUIDE.md](TESTING_GUIDE.md)). Adding `pytest` is a real cost (more deps, more CI time, more contributor overhead) for a script whose meaningful tests are integrations against external APIs.
- **No `setup.py` / package install.** This is a GitHub Action, not a library. Consumers `uses:` it; nobody `pip install`s it.
- **No Sentry, no telemetry, no analytics.** Logs go to the workflow log; there's no phone-home.
- **No plugin system.** The provider abstraction is the only extension point; adding new ones is a code change in this repo, not a runtime hook.
