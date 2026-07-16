# Performance

> Budget, caps, and cost drivers of the AI Diff Reviewer runtime. Every constant referenced here is defined at the top of [`scripts/reviewer.py`](../scripts/reviewer.py) — treat that file as the source of truth; this doc explains **why** the numbers are what they are.

## The performance shape

The action is I/O-bound, not CPU-bound. On a `ubuntu-latest` runner the runtime is dominated by:

1. **The provider call** — either N API round-trips on the chat-completions family (one per agentic-loop turn, multi-second each) or one long-running vendor CLI invocation on the agent-runner family (see [Two performance shapes](#two-performance-shapes) below).
2. **GitHub API calls** — repo metadata, file list, PR diff, the final `POST /pulls/{n}/reviews`, and (optionally) the GraphQL `minimizeComment` mutation for auto-collapse.
3. **Local subprocess** — `git diff origin/<base>...HEAD` once at the start, plus (on the agent-runner family) the `claude` / `cursor-agent` / `codex` subprocess for the entire review.
4. **CLI installation** — on the agent-runner family only, a one-off `npm install -g` (Claude Code / Codex) or `curl | bash` (Cursor Agent) before the review runs. See [Modular install cost](#modular-install-cost).

CPU time inside `scripts/reviewer.py` is negligible. That's why the runtime is stdlib-only and single-file: there's no compute-heavy path that would benefit from a native extension or a virtualenv.

## Two performance shapes

As of v1.1.0 the action ships two provider families with different cost/latency profiles. Choose based on which trade-off matches your team:

| Aspect | Chat-completions family (`anthropic`) | Agent-runner family (`claude-code`, `cursor`, `codex`) |
|---|---|---|
| Loop owner | This action drives the turn loop | Vendor CLI drives its own loop |
| Cost knob you control | `max-turns` × `max_tokens` × conversation pruning | Workflow/job timeout + whatever the vendor bills per invocation; `agent-extra-args` can pass vendor-native budget flags |
| Latency floor | ~5–15 s per turn × 5–15 turns typical | Wallclock of a single vendor CLI invocation (typically 30–180 s end-to-end for a mid-size PR) |
| Cold-start cost | Zero — action starts and immediately hits the provider API | One-off install of the selected CLI (~10–40 s wallclock on `ubuntu-latest`, cached in the runner image on subsequent steps of the same job but not across jobs) |
| Predictability | High — every constant is enforced by our runtime | Medium — the vendor CLI decides how many turns it needs; we only cap the wall clock via `CLI_INVOCATION_TIMEOUT` and workflow-level `timeout-minutes` |
| Findings contract | Model calls the `post_inline_comment` tool; we accumulate `ReviewState` in-process | Vendor CLI writes `.aiprr/findings.json`; we parse it, cap at `max-inline-comments`, and submit |
| Billing model | Metered API tokens (Anthropic account) | `codex`: metered OpenAI API tokens (BYOK). **`claude-code`: metered Anthropic API tokens, OR a Claude Pro/Max subscription — pass a `claude setup-token` OAuth token (`sk-ant-oat…`) as `api-key` (see [docs/PROVIDERS.md § "Billing Claude Code against a subscription"](PROVIDERS.md#billing-claude-code-against-a-subscription-instead-of-api-tokens)). `cursor`: consumes credits from your Cursor Pro/Pro+/Ultra subscription — no BYOK; use `model: auto` on Pro for unlimited routing.** |

Both families converge on the same `ReviewResult` payload before `POST /pulls/{n}/reviews`, so downstream behaviour (severity gating, 422 fallback, tracking comment) is identical.

## The agentic-loop budget (chat-completions family only)

For the `anthropic` provider (and any future chat-completions provider — raw OpenAI, Gemini, Bedrock), the primary cost dimension is **turns × tokens**. Each turn is one API call plus a batch of tool calls; conversation history grows across turns (quadratic in billable tokens if unbounded).

Agent-runner providers don't hit this section — they own their own loop internally. Skip to [The agent-runner budget](#the-agent-runner-budget).

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

## The agent-runner budget

For the `claude-code`, `cursor`, and `codex` providers, we don't run a turn loop — the vendor CLI does. Our cost surface is:

| Knob | Effect |
|---|---|
| `agent-max-turns` | Currently logs a warning for CLI providers instead of enforcing a limit. None of the shipping CLIs expose one stable cross-provider turn-count flag, so the effective runtime cap is `CLI_INVOCATION_TIMEOUT` plus the workflow job timeout. |
| `agent-extra-args` | Escape hatch to pass raw vendor flags (e.g. `--model`, `--verbose`). Not cost-capped by us — misuse (`--max-turns 999`) will bill you exactly what the CLI bills you. |
| `mcp-config-file` | Path to an MCP config the CLI loads. Extra tools = more turns = more spend. Same "you pay what you enable" principle. |
| `max-inline-comments` | Hard cap on findings we ingest from `.aiprr/findings.json`. Extra findings are dropped and counted in the `inline-dropped` action output. Default `10`. |

The vendor CLI decides how many turns it needs; there is no per-turn output-token cap we control. In practice a mid-size PR review takes 60–180 s of wallclock and bills like a normal Claude / Cursor / Codex session of similar length. Estimate cost by running once against a representative PR before turning it on across the org.

## Modular install cost

The `runs.steps` in `action.yml` install the selected agent-runner CLI **only when needed**. The gate is a shell `if:` against `inputs.provider`.

| Provider | Install command | Cold wallclock (rough) |
|---|---|---|
| `anthropic` | (none) | 0 s |
| `claude-code` | `npm install -g @anthropic-ai/claude-code@<claude-code-version>` | 10–25 s |
| `cursor` | `curl -fsSL https://cursor.com/install | bash -s -- --version <cursor-version>` | 20–40 s |
| `codex` | `npm install -g @openai/codex@<codex-version>` | 10–25 s |

Selecting `provider: anthropic` (the default) pays the classic zero-install cost this repo is optimised for. Selecting a CLI provider pays a one-off install per workflow job; there is no cross-job cache (GitHub-hosted runners don't share filesystem state), so pinning a specific `<cli>-version` matters mostly for reproducibility, not for warm-boot speed.

## Tool-loop guardrails

Every tool the model can call has a hard cap so a bad `read_file(path, limit=999999)` or a runaway `grep` can't blow up the prompt.

| Constant | Default | Effect |
|---|---|---|
| [`MAX_TOOL_OUTPUT_BYTES`](../scripts/reviewer.py) | `32_000` | Any tool result larger than this is truncated with a pointer telling the model to narrow the call. |
| [`MAX_FILE_READ_LINES`](../scripts/reviewer.py) | `2_000` | Hard ceiling on `read_file` line count per call. |
| [`MAX_SEARCH_RESULTS`](../scripts/reviewer.py) | `200` | Hard ceiling on `grep` / `glob` result counts. |
| [`MAX_DIFF_CHARS`](../scripts/reviewer.py) | `200_000` | Cap on the seed diff embedded in the first user message. Larger diffs are truncated with a pointer to `read_file`. |

These caps mean the model **cannot** flood its own context. A huge file or an over-broad grep degrades gracefully into a truncation message — the review continues, the offending call retries with a narrower scope.

Agent-runner providers use the vendor CLI's own tools (their file-search, their code execution, their MCP integrations) rather than our five-tool shim, so these particular guardrails don't apply on that path. The vendor CLIs have their own equivalents.

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

Common to both provider families:

- **`max-inline-comments`** (default `10`) — hard cap on inline comments; the review summary is not capped. Applied uniformly across both families.
- **`model`** — swapping model tiers has the biggest effect on both cost and quality. The `DEFAULT_MODELS` table at the top of `scripts/reviewer.py` picks a deliberate midpoint per provider; override if your budget or quality bar is different.

Chat-completions family only:

- **`max-turns`** (default `30`) — increase for larger PRs, but each unit added is up to `ANTHROPIC_MAX_TOKENS = 8192` extra output tokens.

Agent-runner family only:

- **`agent-max-turns`** — currently warns on CLI providers instead of enforcing a cap. Use `agent-extra-args` for vendor-native budget flags when a CLI exposes one.
- **`agent-extra-args`** — free-form vendor flags. Not cost-capped by us.
- **`mcp-config-file`** — path to an MCP config for the vendor CLI. Extra tools = more turns = more spend.

## Local performance measurement

There is no benchmark suite (adding one would violate the stdlib-only rule for the runtime). The dogfooding channel via [`.github/workflows/self-review.yml`](../.github/workflows/self-review.yml) is the real measurement: it always runs the direct Anthropic baseline leg and runs the CLI-provider legs only when critical action/runtime files change. Its Actions logs record turn count, per-turn latency, and total wallclock for every leg that actually invokes the reviewer. See [`docs/PR_REVIEW_WORKFLOW.md`](PR_REVIEW_WORKFLOW.md) for how to read those logs and how to tell which review came from which leg (via the per-provider `self-reviewed:*` labels).

## Iteration-Aware Review (IAR) — Cost and Latency Model

IAR (on by default as of v1.8.0, with the `first-pass-exhaustive` policy) rebuilds the "converge in 1–3 rounds instead of 5–10" experience without raising steady-state per-turn cost. This section makes the cost impact explicit so you can tune it — or opt out via `iteration-awareness-enabled: false` — deliberately.

**One-line summary:** on the *first* review of a new commit-set with the recommended `first-pass-exhaustive` policy, the LLM may produce up to `cap_multiplier` × `max-inline-comments` findings and receives ~150 extra tokens of prompt guidance (the exhaustive addendum). On *subsequent* rounds of the same generation, IAR is close to a no-op — the LLM produces its normal set, and the reviewer dedupes them before posting. The full authoritative spec is in [`ITERATION_AWARENESS.md`](ITERATION_AWARENESS.md).

**Rule #9 compliance (AGENTS.md DON'T #9):** IAR **never** raises the `max_tokens` or `MAX_TURNS` defaults. The only knob it turns is `max-inline-comments`, and only for round 1 of a new generation on the two "exhaustive-eligible" policies (`first-pass-exhaustive` and any policy short-circuited by the safety net). Round 2+ of the same generation always use the baseline cap.

### Lifetime cost matrix per policy

Assumes a mid-size PR reviewed 5 times over its lifetime (once on open, then 4 push/rebase iterations). Uses the default `cap_multiplier=3` and `max-inline-comments=10`. **All numbers are theoretical** — validated against real dogfooding data in Task 10 of the IAR rollout plan.

| Policy | Round 1 cost | Round 2 cost | Rounds 3–5 cost | Lifetime cost vs baseline | User-visible effect |
|---|---|---|---|---|---|
| **(baseline, IAR off)** | 1.0× | 1.0× | 1.0× | 1.0× | Same findings re-posted each round → the "infinite loop" symptom. |
| `iterative` | 1.0× | 1.0× | 1.0× | 1.0× | Same LLM cost; only *deltas* posted. Best steady-state ratio. |
| `first-pass-exhaustive` | ~1.3× | 1.0× | 1.0× | ~1.06× | Extended initial pass; dedup afterwards. Recommended default. |
| `round-capped` (max 3) | 1.0× | 1.0× | 1.0× rounds 3, then 0.5× rounds 4–5 (silence pass) | ~0.9× | Aggressive cost saver — non-critical findings silenced after round 3. |
| `critical-gate` | 1.0× | 1.0× | 1.0× | ~0.85× | Non-critical resolved findings stay silenced across generations too. |

**Ambient overhead per run (regardless of policy):**

- 1 GraphQL query to read the last non-minimized tracking marker (~200 ms).
- 1 REST call to read PR labels for escape-label detection (~100 ms).
- 2 `git diff` invocations for the range hash + new-lines-pct (~100 ms combined on a mid-size PR).
- 1 `git rev-parse` for the base SHA (~10 ms).
- 1 `git show` per unique file mentioned in a finding (~10 ms each; capped by the number of findings, not the size of the diff).

Total ambient overhead: **~500–800 ms per run**, well under the seconds of latency added by the LLM's own tool calls. Not measurable in end-user wall-clock.

### Per-round wall-clock breakdown

For a typical `first-pass-exhaustive` review on a 500-line PR:

| Phase | Baseline (IAR off) | IAR round 1 (new gen) | IAR round 2+ (same gen) |
|---|---|---|---|
| Setup + PR fetch | ~3 s | ~3.5 s (+GraphQL marker read) | ~3.5 s |
| LLM turn loop | 30–90 s | 40–120 s (cap × 3) | 30–90 s (baseline cap) |
| GitHub submission | 1–3 s | 1–3 s | 1–3 s |
| **Total** | **~40–95 s** | **~50–130 s** | **~40–95 s** |

The round-1 extension is the meaningful cost, and only when a new generation actually needs it. On steady-state rounds it's noise.

### Worst-case cost impact + mitigation

**The failure mode you're paying to prevent:** push-heavy workflows (dozens of small commits per hour) that repeatedly trigger new generations. In principle, each `push` → `NEW_COMMITS` transition → `round_in_generation=1` → cap-expanded pass.

**Mitigation guidance (choose one):**

1. **Debounce your triggers** — use `on: push` for the base branch and `on: pull_request: [synchronize]` for feature branches; skip drafts. Standard workflow hygiene.
2. **Cap ambition explicitly** — set `exhaustive-first-pass-cap-multiplier: 1` to disable cap expansion entirely, keeping only the prompt addendum. Turns "extended pass" into "same-cost pass with better guidance".
3. **Switch to `iterative`** — no cap expansion, no prompt addendum, pure dedup engine. Same LLM cost as baseline, better UX.
4. **Enable the 30% safety net's alternative** — the built-in safety net already forces `first-pass-exhaustive` when >= 30% of lines are new-in-generation. This is *usually* what you want. To turn it off (rare), set `iteration-awareness-enabled: false` and re-enable IAR only for the initial opening of the PR.

### How to tune for cost sensitivity

Three recommended profiles:

**Cost-sensitive (minimize spend, tolerate some multi-round noise):**
```yaml
iteration-awareness-enabled: true
convergence-policy: iterative              # dedup only, no cap expansion
exhaustive-first-pass-cap-multiplier: 1    # ignored by iterative, safe default
max-review-rounds: 0                       # unused by iterative
```

**Balanced (recommended default — one-shot exhaustive, then converge):**
```yaml
iteration-awareness-enabled: true
convergence-policy: first-pass-exhaustive
exhaustive-first-pass-cap-multiplier: 3    # round 1 gets 30 max instead of 10
max-review-rounds: 5                       # unused by first-pass-exhaustive
iteration-escape-label: full-review-please
```

**Quality-sensitive (biggest first-pass net, silence noise later):**
```yaml
iteration-awareness-enabled: true
convergence-policy: round-capped
max-review-rounds: 3                       # exhaustive round 1, dedup 2-3, silence 4+
exhaustive-first-pass-cap-multiplier: 5    # round 1 gets 50 max instead of 10
```

### Reading the cost telemetry

Every IAR-enabled run writes five outputs (empty strings when IAR is off):

| Output | Meaning | Example use |
|---|---|---|
| `iteration-round` | Round number in the current generation (1, 2, …). | `if: steps.review.outputs.iteration-round == '1'` for round-1-only steps. |
| `iteration-generation` | Monotonic generation counter across the PR's lifetime. | Track how many force-pushes / rebases the PR has seen. |
| `iteration-policy-applied` | The policy actually applied (may differ from configured — safety net or escape label can override). | Detect when the safety net fired. |
| `iteration-tokens-used` | Best-effort token count for this run. Currently `0` — populated by a future provider-hook PR. Ship the schema now so consumers can wire dashboards. |
| `iteration-cost-vs-baseline-estimate` | Heuristic delta vs an IAR-off run (e.g. `+15%`, `-5%`, `0%`). Approximate — not a substitute for provider billing. |

Example CI dashboard snippet — surface cost telemetry as a workflow annotation:

```yaml
- name: IAR cost telemetry
  if: always() && steps.review.outputs.iteration-round != ''
  run: |
    echo "::notice title=IAR::gen=${{ steps.review.outputs.iteration-generation }} \
    round=${{ steps.review.outputs.iteration-round }} \
    policy=${{ steps.review.outputs.iteration-policy-applied }} \
    cost=${{ steps.review.outputs.iteration-cost-vs-baseline-estimate }} \
    tokens=${{ steps.review.outputs.iteration-tokens-used }}"
```

## Related docs

- [`STRICTNESS.md`](STRICTNESS.md) — how the model's `severity` argument maps to the GitHub check outcome (the strictness gate is decoupled from any perf constant).
- [`ARCHITECTURE.md`](ARCHITECTURE.md) — the full runtime shape: composite-action shell, the five tools, and the review-submission flow.
- [`SECURITY.md`](SECURITY.md) — log redaction (`redact_for_log` + `LOG_REDACT_SUBSTRINGS`) and safe path resolution.
- [`ITERATION_AWARENESS.md`](ITERATION_AWARENESS.md) — the authoritative IAR spec (schema, policies, safety rails, upgrade path).
