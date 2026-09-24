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

The action ships two provider families with different cost/latency profiles (the runner side of the runner × backend matrix in [PROVIDERS.md](PROVIDERS.md)). Choose based on which trade-off matches your team:

| Aspect | Chat-completions family (`anthropic`, `openai`) | Agent-runner family (`claude-code`, `cursor`, `codex`, `grok`) |
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
- Input token growth is bounded by `MAX_CONVERSATION_TURNS_RETAINED = 12` on retained turn-pairs plus the seed message (patches budgeted at `FIRST_MESSAGE_PATCH_BYTES = 120 000` bytes — see below; anything beyond is fetched on demand with `get_patch`).
- Since v2.1.0 follow-up rounds run in **incremental mode**: the seed message carries only the hunks changed since the last reviewed head plus the prior-findings table, the inline cap scales with the delta (floor 3 comments) and, since v3 (RFC-06), the turn budget is `clamp(4 + 1.5 × changed files + 1 × outstanding findings, 4, max-turns)` — ≈ 8–12 turns on a typical 1–3-file follow-up instead of the full cap; a follow-up with no code change spends **zero** review turns (the verifier re-reads the outstanding anchors instead). Measured (Task 27): on PR #61's real deltas the grok leg went from 4.16 M input tokens ($1.69) on a full round to 0.98–1.60 M ($0.53–0.77) on small incremental pushes (−62 … −76 %), the glm leg from 6.9–7.2 M to 3.4–4.4 M (−37 … −52 %); a no-change round costs ≈ $0.007 (verifier only). On a typical "push a fix" round this is the largest saving of all — most of the PR diff is not sent at all. See `docs/ITERATION_AWARENESS.md § 14.5`.
- Since v2.1.0 the seed diff is **cached** on Anthropic (a second `cache_control` breakpoint on the first user message), so on turns 2..N it is billed at the cache-read rate (~10 % of input) instead of full price; combined with diff shaping (`ignore-paths`) this is where most of the per-review input cost went. Watch the per-call `usage:` log line for `cache_read`.
- Realistic reviews come in **well under** the ceiling: typical runs terminate on `submit_review` after 5–15 turns.

If you increase `max-turns` or `MAX_CONVERSATION_TURNS_RETAINED`, **estimate the token impact first**. `AGENTS.md` DON'T #9 makes this explicit: raising defaults without measuring the per-review cost delta is not merged.

### Risk-tiered budgets (v3, RFC-06)

Every run classifies the change inventory deterministically (paths, statuses, binary / mode-change / omitted flags, line counts — never PR metadata) into a risk tier and takes its budget row from one table (`BUDGET_MATRIX`):

| Tier | When | Max turns | Review alias | Output tokens / turn | Verifier | Patch bytes in the first message |
|---|---|---|---|---|---|---|
| `low` | only docs / tests / generated files, ≤ 300 lines, inventory complete | 8 | `balanced` | 4 096 | criticals only | 60 k |
| `standard` | any code file, nothing sensitive, inventory complete | 20 | `balanced` | 8 192 | criticals + 30 % of warnings | 120 k |
| `elevated` | prompts / policy, workflow / CI or dependency files; a mode change; incomplete inventory; > 1 500 lines | 30 | `balanced` | 8 192 | all criticals + all warnings | 200 k |
| `critical` | policy or CI files **together with** code; an unknown file; a `high-risk-paths` match | 40 (the only raise: ≈ +$0.15–0.35 at grok-4.5 rates, on the rarest tier) | `deep` where the kind has one, else `balanced` | 8 192 | all | 200 k |

`budget-profile: fixed` restores today's constants (30 turns, `balanced`, 8 192, 30 %, 120 k) for every tier; an explicit `max-turns` (other than the default) is a ceiling a tier never exceeds; `high-risk-paths` raises, nothing lowers; `economy` is never a review alias. The tier is written to the change inventory, the run record (`budget.risk_tier`) and the structured output. The per-tier recall guard is measured in Phase 4 (RFC-06 § Measurable targets).

## The agent-runner budget

For the `claude-code`, `cursor`, `codex`, and `grok` providers, we don't run a turn loop — the vendor CLI does. Our cost surface is:

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
| `grok` | `curl -fsSL https://x.ai/cli/install.sh \| bash -s <grok-version>` (static binary) | 5–15 s |
| `openai` | (none — in-process) | 0 s |

Selecting `provider: anthropic` or `provider: openai` pays the classic zero-install cost this repo is optimised for; the `cursor` / `grok` steps also skip the installer when the CLI is already on `PATH`. Selecting a CLI provider pays a one-off install per workflow job; there is no cross-job cache (GitHub-hosted runners don't share filesystem state), so pinning a specific `<cli>-version` matters mostly for reproducibility, not for warm-boot speed.

## Tool-loop guardrails

Every tool the model can call has a hard cap so a bad `read_file(path, limit=999999)` or a runaway `grep` can't blow up the prompt.

| Constant | Default | Effect |
|---|---|---|
| [`MAX_TOOL_OUTPUT_BYTES`](../scripts/reviewer.py) | `32_000` | Any tool result larger than this is truncated with a pointer telling the model to narrow the call. |
| [`MAX_FILE_READ_LINES`](../scripts/reviewer.py) | `2_000` | Hard ceiling on `read_file` line count per call. |
| [`MAX_SEARCH_RESULTS`](../scripts/reviewer.py) | `200` | Hard ceiling on `grep` / `glob` result counts. |
| [`FIRST_MESSAGE_PATCH_BYTES`](../scripts/reviewer.py) | `120_000` | v3: byte budget for the patches embedded in the first user message. Files are embedded **whole, in inventory order, while they fit** (greedy); the rest are listed under `## Not embedded — fetch on demand` and fetched with `get_patch` (in-process) or `git diff <base>...<head> -- <path>` (CLI lanes). Lowered from the old 200 000-char single blob. RFC-06 tiers override it (60 k / 120 k / 200 k). |
| [`MAX_DIFF_CHARS`](../scripts/reviewer.py) | `= FIRST_MESSAGE_PATCH_BYTES` | Ceiling on the diff kept on `PRContext`; a section cut by the ceiling is never embedded half-way. The embedding rule is per file (above), not this constant. |
| [`MAX_PATCH_CHARS`](../scripts/reviewer.py) | `40_000` | Per-call cap of `get_patch`; a truncated answer lists the remaining hunk indices. |
| [`MAX_TOOL_TRACE_ENTRIES`](../scripts/reviewer.py) | `500` | Bound on `ReviewState.tool_trace` (name, redacted args, result hash per tool call). |
| [`DEFAULT_IGNORE_PATH_GLOBS`](../scripts/reviewer.py) | lockfiles, `*.min.*`, `*.map`, `node_modules/`, `vendor/`, `dist/`, snapshots | Diff sections removed **before** the byte budget applies and reported to the model as omitted (inventory flag + `## Omitted` block). Extended by `ignore-paths`. |

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
- **`ignore-paths`** (+ built-in exclusions) — lockfiles, minified bundles, source maps, vendored trees and snapshots are dropped from the diff the model receives and listed back as omitted. On lockfile-heavy PRs this is the single largest token saving, and it applies to every turn of the chat-completions loop.
- **`model`** — swapping model tiers has the biggest effect on both cost and quality. Pick a profile in one word — `model: balanced | economy | deep` — resolved per runner × backend from the dated matrix in [`PROVIDERS.md` § "Cost-efficient defaults matrix"](PROVIDERS.md#cost-efficient-defaults-matrix-verified-2026-09-16--ids-and-prices-move-re-check-when-bumping); empty keeps the built-in default; an explicit id passes through.

Chat-completions family only:

- **`max-turns`** (default `30`) — increase for larger PRs, but each unit added is up to `ANTHROPIC_MAX_TOKENS = 8192` extra output tokens.

Agent-runner family only:

- **`agent-max-turns`** — enforced natively on `grok` (`--max-turns`); on Claude Code / Codex / Cursor the run logs a per-provider warning (Claude Code: use `--max-budget-usd` via `agent-extra-args`; otherwise the 900 s timeout is the bound).
- **`agent-extra-args`** — free-form vendor flags. Not cost-capped by us.
- **`mcp-config-file`** — path to an MCP config for the vendor CLI. Extra tools = more turns = more spend.

## Local performance measurement

There is no benchmark suite (adding one would violate the stdlib-only rule for the runtime). The dogfooding channel via [`.github/workflows/self-review.yml`](../.github/workflows/self-review.yml) is the real measurement: it always runs the direct Anthropic baseline leg and runs the CLI-provider legs only when critical action/runtime files change. Its Actions logs record turn count, per-turn latency, and total wallclock for every leg that actually invokes the reviewer. See [`docs/PR_REVIEW_WORKFLOW.md`](PR_REVIEW_WORKFLOW.md) for how to read those logs and how to tell which review came from which leg (via the per-provider `self-reviewed:*` labels).

## Iteration-Aware Review (IAR) — Cost and Latency Model

IAR runs on every review with the `first-pass-exhaustive` policy by default. It delivers the "converge in 1–3 rounds instead of 5–10" experience without raising steady-state per-turn cost. This section makes the cost impact explicit so you can tune the pipeline deliberately.

**One-line summary:** on the *first* review of a new commit-set with the recommended `first-pass-exhaustive` policy, the LLM may produce up to `cap_multiplier` × `max-inline-comments` findings and receives ~150 extra tokens of prompt guidance (the exhaustive addendum). On *subsequent* rounds of the same generation, IAR is close to a no-op — the LLM produces its normal set, and the reviewer dedupes them before posting. The full authoritative spec is in [`ITERATION_AWARENESS.md`](ITERATION_AWARENESS.md).

**Rule #9 compliance (AGENTS.md DON'T #9):** IAR **never** raises the `max_tokens` or `MAX_TURNS` defaults. The only knob it turns is `max-inline-comments`, and only for round 1 of a new generation on the two "exhaustive-eligible" policies (`first-pass-exhaustive` and any policy short-circuited by the safety net). Round 2+ of the same generation always use the baseline cap.

### Lifetime cost matrix per policy

Assumes a mid-size PR reviewed 5 times over its lifetime (once on open, then 4 push/rebase iterations). Uses the default `cap_multiplier=3` and `max-inline-comments=10`. **All numbers are theoretical** — validated against real dogfooding data.

| Policy | Round 1 cost | Round 2 cost | Rounds 3–5 cost | Lifetime cost vs no-dedup baseline | User-visible effect |
|---|---|---|---|---|---|
| **(no-dedup baseline)** | 1.0× | 1.0× | 1.0× | 1.0× | Same findings re-posted each round → the "infinite loop" symptom. |
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

| Phase | No-dedup baseline | IAR round 1 (new gen) | IAR round 2+ (same gen) |
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
3. **Switch to `iterative`** — no cap expansion, no prompt addendum, pure dedup engine. Same LLM cost as the no-dedup baseline, better UX.

### How to tune for cost sensitivity

Three recommended profiles:

**Cost-sensitive (minimize spend, tolerate some multi-round noise):**
```yaml
convergence-policy: iterative              # dedup only, no cap expansion
exhaustive-first-pass-cap-multiplier: 1    # ignored by iterative, safe default
max-review-rounds: 0                       # unused by iterative
```

**Balanced (recommended default — one-shot exhaustive, then converge):**
```yaml
convergence-policy: first-pass-exhaustive
exhaustive-first-pass-cap-multiplier: 3    # round 1 gets 30 max instead of 10
max-review-rounds: 0                       # DEFAULT (0 = unlimited); ignored under first-pass-exhaustive
iteration-escape-label: full-review-please
```

> **Do NOT copy `max-review-rounds: 5` into this profile** (a
> non-default value the reviewer previously suggested here). It is
> ignored under `first-pass-exhaustive` so the setting looks
> harmless — but if you later switch `convergence-policy` to
> `round-capped`, `max-review-rounds: 5` silently caps every PR at
> 5 rounds without you realising, silencing all non-critical
> findings past that point. Keep the default `0` (unlimited) unless
> you deliberately want the cap.

**Quality-sensitive (biggest first-pass net, silence noise later):**
```yaml
convergence-policy: first-pass-exhaustive  # ONLY this policy amplifies round 1
exhaustive-first-pass-cap-multiplier: 5    # round 1 gets 50 max instead of 10
max-review-rounds: 0                       # unused by first-pass-exhaustive
```

> **Note.** `exhaustive-first-pass-cap-multiplier` is only consulted by
> `first-pass-exhaustive` (and the safety-net override) — `round-capped`
> uses the baseline cap on every round and then silences all non-critical
> findings past `max-review-rounds`. If you want the biggest round-1 net,
> use `first-pass-exhaustive`. If you want a hard round cap with no
> amplification, `round-capped` is the right choice — just don't expect
> the multiplier to compose across policies (`action.yml`'s input
> description is explicit: "Ignored by other policies").

**Round-cap discipline (bound total rounds, no round-1 amplification):**
```yaml
convergence-policy: round-capped
max-review-rounds: 3                       # baseline cap rounds 1-3, silence non-critical from round 4+
exhaustive-first-pass-cap-multiplier: 1    # ignored by round-capped, safe default
```

### Reading the cost telemetry

Every run writes five outputs (empty strings only if the IAR pipeline crashed):

| Output | Meaning | Example use |
|---|---|---|
| `iteration-round` | Round number in the current generation (1, 2, …). | `if: steps.review.outputs.iteration-round == '1'` for round-1-only steps. |
| `iteration-generation` | Monotonic generation counter across the PR's lifetime. | Track how many force-pushes / rebases the PR has seen. |
| `iteration-policy-applied` | The policy actually applied (may differ from configured — safety net or escape label can override). | Detect when the safety net fired. |
| `iteration-tokens-used` | Total tokens this review actually consumed — every input partition (uncached, cache-read, cache-write, each counted once) plus output —, captured from the provider — API `usage` objects (`anthropic` / `openai`), the Claude Code stream-json `result` event, Codex `--json` `turn.completed` events, or the Grok JSON document. `0` when the provider reports nothing (Cursor). The tracking comment shows the same numbers with cache ratio, turns and an indicative cost; never gate CI on the value. Empty string ONLY if the IAR pipeline crashed. |
| `iteration-cost-vs-baseline-estimate` | Coarse cost-delta heuristic derived from cap expansion + a small prompt-addendum flag. Always `"0%"` or `"+N%"` today — silenced-finding savings are not yet modelled, so a `"-N%"` value never appears (see [`docs/ITERATION_AWARENESS.md § 13.3`](ITERATION_AWARENESS.md)). Never gate CI on `== '-N%'`. |

The tracking comment on the PR shows the human version on every run — e.g. `**Usage:** 341.2k in (88% cached) · 2.1k out · est. $0.05 (indicative) · 6 turns · 71s` — so the effect of diff shaping, the diff cache breakpoint and the model tier is visible per review without opening the logs.

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

## Measured noise floor and the eval-gate verdict (v3)

Since v3 every run writes a `run-record/3.0` file (`.aiprr/run-record.json`) with cost, usage, turns and separated timings, and the eval gate ([RFC-01](rfc/v3/01-eval-gate-contract.md)) reasons over replicated runs of the pinned corpus instead of single dogfood logs. The first measured floor (Phase 0, 2026-09-23, 3 repetitions per cell; default lane grok-4.5 CLI `balanced` unless noted):

| Set | Cells | Cost spread median `(max−min)/mean` | Worst | Recall swing | Mean cost / run |
|---|---|---|---|---|---|
| 7 historical PRs (`phase0-floor`) | 7 | 0.252 | 0.403 | 1 defect (1 of 7 cells) | $0.60 (range $0.34–$1.46) |
| 21 critical fixture trees (`phase0-trees-critical`) | 21 | 0.294 | 0.680 | 0 | $0.093 |
| 7 historical PRs, Claude Code on Z.ai `glm-5.3-flash` (`phase0-floor` glm) | 7 | 0.206 | 0.305 | 2 defects (2 of 7 cells) | $1.50 (range $0.89–$3.47, list price) |
| **All baseline lanes** | **35** | **0.268** | **0.680** | **2** | — |
| *v3 re-stamp (2026-09-24)* — 7 historical PRs, in-process `openai` runner on xAI (`phase1-parity`) | 7 | 0.410 | 1.221 | 2 | $0.354 |
| 21 critical trees, v3.0 prompt, verifier off / on (`phase1-precision-off` / `-on`) | 21 / 21 | 0.405 / 0.312 | 0.801 / 0.954 | 1 / 1 | $0.109 / $0.104 |
| 21 critical trees, release-candidate runtime, verifier on (`phase2-rc`) | 21 | 0.387 | 0.927 | 1 | $0.114 |
| **All v3-prompt lanes (the floor in force)** | **70** | **0.370** | **1.22** | **2** | — |

What this means when reading cost numbers: two identical runs of the same PR routinely differ by a quarter of their cost, and a single PR moved from 6 to 9 turns between repetitions. A cost claim below ≈ 38 % on a paired comparison is inside the noise and is not promotable (v3 re-stamp; v2 said 27 %); a recall claim needs a net gain of 3 defects; a change is blocking when the lane's median spread widens past 1.5 × this baseline (0.56) or any cell exceeds 1.3. The verdict file that encodes the decision is `verdict/1.0` (`tests/eval/schemas/verdict.schema.json`, example in `schemas/examples/`), produced by `python3 tests/eval/determinism.py verdict --baseline DIR --candidate DIR`, and the release workflow refuses to cut a release without a fresh non-blocking one ([`RELEASE_RECOVERY.md`](RELEASE_RECOVERY.md) → "Release skipped by the eval gate"). Raw records and summaries: `tests/eval/records/campaigns/`.

### Verifier cost (v3, measured)

The verifier ([RFC-03](rfc/v3/03-verification-and-evidence.md), input `verifier`, default on) re-reads the anchor of every claimed critical and a 30 % sample of warnings with the `economy` alias and at most four tool calls. Measured on the Phase 1 precision campaign (Task 19, 2026-09-24; 70 verifications over 62 grok-4.5 reviews of the 21 critical fixture trees, verifier = grok-4.5 on xAI):

| Per verified finding | Per tree review ($0.104 mean) | Per PR-class review (projected from the Phase 0 grok floor: 0.10 criticals + 1.86 warnings → ≈ 0.65 verifications) |
|---|---|---|
| ≈ 3.0 k input + 0.36 k output tokens, 10.1 s, $0.0088 (max 6.5 k / 20 s / $0.016) | +$0.010 ≈ +9.6 % | ≈ +$0.006 ≈ +1 % of $0.60 |

Verifier wall-clock is serial after the review loop (`timings.verifier_seconds` in the run record), so a PR with three claimed criticals adds ≈ 30 s. The knobs are `verifier: off` (claimed criticals then publish as annotated warnings — see [STRICTNESS](STRICTNESS.md)), `verifier-model` (an explicit id or alias) and `strict-unverified-criticals`.

**Prompt-cache lottery in the cost floor.** On xAI-backed lanes the same review repeated three times routinely differs by a third in cost while its token *totals* are near-identical: the cached share of input swings between ≈ 35 % and ≈ 90 % from one repetition to the next (Phase 0 trees: cache-normalised token spread median 0.02 versus real-cost spread 0.29). Cost spreads in this document therefore mix provider-side cache warmth with genuine behaviour; the v3.0 prompt and first message also widened the *behavioural* part on the tree corpus (cache-normalised spread 0.24–0.28, turns 3–9 where v2 used 3–4) at unchanged recall — recorded as a Stage Gate B item in the plan, not folded into the thresholds.

## Complete token accounting and focused context

Token totals include all input partitions (uncached, cache-read and cache-write) plus output, exactly once. OpenAI and Codex report cached input as a subset of input; Anthropic reports disjoint input/cache partitions. The displayed cache ratio uses total input as its denominator. Cost estimates remain indicative, not billing records.

Incremental reviews construct the actual previous-head-to-current-head diff before truncation, instead of reusing a truncated full PR diff. This reduces repeated old hunks without losing new edits merely because their files appeared late in the original PR diff. Outstanding prior issues still gate the check.

## Related docs

- [`STRICTNESS.md`](STRICTNESS.md) — how the model's `severity` argument maps to the GitHub check outcome (the strictness gate is decoupled from any perf constant).
- [`ARCHITECTURE.md`](ARCHITECTURE.md) — the full runtime shape: composite-action shell, the five tools, and the review-submission flow.
- [`SECURITY.md`](SECURITY.md) — log redaction (`redact_for_log` + `LOG_REDACT_SUBSTRINGS`) and safe path resolution.
- [`ITERATION_AWARENESS.md`](ITERATION_AWARENESS.md) — the authoritative IAR spec (schema, policies, safety rails, upgrade path).
