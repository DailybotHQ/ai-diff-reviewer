# Performance

> Budget, caps, and cost drivers of the AI PR Reviewer runtime. Every constant referenced here is defined at the top of [`scripts/reviewer.py`](../scripts/reviewer.py) — treat that file as the source of truth; this doc explains **why** the numbers are what they are.

## The performance shape

The action is I/O-bound, not CPU-bound. On a `ubuntu-latest` runner the runtime is dominated by:

1. **The provider API latency** — one Anthropic call per agentic-loop turn. Each call is a full round-trip over TLS with model inference at the far end (multi-second per turn, not milliseconds).
2. **GitHub API calls** — repo metadata, file list, PR diff, the final `POST /pulls/{n}/reviews`, and (optionally) the GraphQL `minimizeComment` mutation for auto-collapse.
3. **Local subprocess** — `git diff origin/<base>...HEAD` once at the start.

CPU time inside `scripts/reviewer.py` is negligible. That's why the runtime is stdlib-only and single-file: there's no compute-heavy path that would benefit from a native extension or a virtualenv.

## The agentic-loop budget

The primary cost dimension is **turns × tokens**. Each turn is one Anthropic API call plus a batch of tool calls; conversation history grows across turns (quadratic in billable tokens if unbounded).

| Constant | Default | Effect |
|---|---|---|
| [`DEFAULT_MAX_TURNS`](../scripts/reviewer.py) | `30` | Hard ceiling on API calls per review. Overrideable via the `max-turns` input. |
| [`ANTHROPIC_MAX_TOKENS`](../scripts/reviewer.py) | `8192` | Max output tokens per turn. Passed verbatim to the Anthropic `messages` API. |
| [`MAX_CONVERSATION_TURNS_RETAINED`](../scripts/reviewer.py) | `12` | Soft cap on retained turn-pairs. Older tool-result pairs are dropped once this is exceeded (the original user message is always kept). This is the guard against O(turns²) token billing. |
| [`DEFAULT_MAX_INLINE_COMMENTS`](../scripts/reviewer.py) | `10` | Hard cap on queued inline comments per review. Overrideable via `max-inline-comments`. |

**Worst-case cost per review** (in Anthropic API terms, using the defaults):

- Up to **30 turns** × up to **8192 output tokens** = ~245 K output tokens.
- Input token growth is bounded by `MAX_CONVERSATION_TURNS_RETAINED = 12` on retained turn-pairs plus the seed diff (capped at `MAX_DIFF_CHARS = 200 000` chars — see below).
- Realistic reviews come in **well under** the ceiling: typical runs terminate on `submit_review` after 5–15 turns.

If you increase `max-turns` or `MAX_CONVERSATION_TURNS_RETAINED`, **estimate the token impact first**. `AGENTS.md` DON'T #9 makes this explicit: raising defaults without measuring the per-review cost delta is not merged.

## Tool-loop guardrails

Every tool the model can call has a hard cap so a bad `read_file(path, limit=999999)` or a runaway `grep` can't blow up the prompt.

| Constant | Default | Effect |
|---|---|---|
| [`MAX_TOOL_OUTPUT_BYTES`](../scripts/reviewer.py) | `32_000` | Any tool result larger than this is truncated with a pointer telling the model to narrow the call. |
| [`MAX_FILE_READ_LINES`](../scripts/reviewer.py) | `2_000` | Hard ceiling on `read_file` line count per call. |
| [`MAX_SEARCH_RESULTS`](../scripts/reviewer.py) | `200` | Hard ceiling on `grep` / `glob` result counts. |
| [`MAX_DIFF_CHARS`](../scripts/reviewer.py) | `200_000` | Cap on the seed diff embedded in the first user message. Larger diffs are truncated with a pointer to `read_file`. |

These caps mean the model **cannot** flood its own context. A huge file or an over-broad grep degrades gracefully into a truncation message — the review continues, the offending call retries with a narrower scope.

## Timeouts

| Constant | Default | Effect |
|---|---|---|
| [`API_REQUEST_TIMEOUT`](../scripts/reviewer.py) | `600` s (10 min) | Per-turn Anthropic API timeout. Long enough for max-tokens outputs on the slower Sonnet models. |
| [`API_RETRY_DELAYS_S`](../scripts/reviewer.py) | `(2, 5, 15)` s | Retry backoff on transient failures. Three attempts total. |
| [`GH_REQUEST_TIMEOUT`](../scripts/reviewer.py) | `60` s | Per-request GitHub API timeout. |
| Recommended job-level `timeout-minutes` | `15` | The final safety net — set at the workflow level (see `README.md` → "Required permissions"). |

The 15-minute workflow timeout is deliberate: it is longer than any single review should take at the defaults, but short enough that a runaway loop (e.g. a provider outage manifesting as slow-but-not-erroring responses) doesn't burn 6 h of Actions minutes.

## Log-flood protection

The workflow log is a scarce resource — it's what a human debugs a review from. These caps stop a single bad payload from drowning out the useful lines.

| Constant | Default | Purpose |
|---|---|---|
| [`MAX_ERROR_BODY_CHARS`](../scripts/reviewer.py) | `500` | Truncated error-body echo in general failures. |
| [`MAX_422_BODY_CHARS`](../scripts/reviewer.py) | `1_000` | Slightly higher for the GitHub 422 path — the body is the primary signal for which anchor line was rejected. |
| [`MAX_TOOL_LOG_PREVIEW_CHARS`](../scripts/reviewer.py) | `120` | Per-tool-call preview in the log. |
| [`MAX_TRACKING_ERROR_CHARS`](../scripts/reviewer.py) | `1_500` | Cap on error text surfaced in the tracking comment on the PR. |

## The 422 recovery path

GitHub's `POST /pulls/{n}/reviews` is atomic: if **any** inline comment anchors a line outside the diff, the entire request is rejected with HTTP 422 and the whole review is lost. This is a real failure mode the model can trigger even with `MAX_INLINE_COMMENTS = 10`.

The runtime handles this by **retrying summary-only** on 422 — the review still posts, the count of dropped inline comments is surfaced via the `inline-dropped` output, and the tracking comment tells the human what happened. Preserving this fallback is a non-negotiable invariant: any new submission path MUST retain 422 recovery (`AGENTS.md` DON'T #8).

## Cost knobs consumers actually pull

Only two `action.yml` inputs affect per-review cost directly:

- **`max-turns`** (default `30`) — increase for larger PRs, but each unit added is up to `ANTHROPIC_MAX_TOKENS = 8192` extra output tokens.
- **`max-inline-comments`** (default `10`) — increase for very large PRs; low is fine because the review summary is not capped.

The `model` input has an indirect effect: switching from Sonnet to Haiku roughly halves per-token cost at the price of some review quality; switching to Opus doubles cost at the price of latency. The bundled default is `claude-sonnet-4-6` (from [`DEFAULT_MODELS`](../scripts/reviewer.py)) — a deliberate midpoint.

## Local performance measurement

There is no benchmark suite (adding one would violate the stdlib-only rule for the runtime). The dogfooding channel via [`.github/workflows/self-review.yml`](../.github/workflows/self-review.yml) is the real measurement: it runs against every PR to this repo and its Actions logs record turn count, per-turn latency, and total wallclock. See [`docs/PR_REVIEW_WORKFLOW.md`](PR_REVIEW_WORKFLOW.md) for how to read those logs.

## Related docs

- [`STRICTNESS.md`](STRICTNESS.md) — how the model's `severity` argument maps to the GitHub check outcome (the strictness gate is decoupled from any perf constant).
- [`ARCHITECTURE.md`](ARCHITECTURE.md) — the full runtime shape: composite-action shell, the five tools, and the review-submission flow.
- [`SECURITY.md`](SECURITY.md) — log redaction (`redact_for_log` + `LOG_REDACT_SUBSTRINGS`) and safe path resolution.
