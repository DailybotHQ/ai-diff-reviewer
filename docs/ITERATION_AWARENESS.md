# Iteration-Aware Review (IAR)

> **Subsystem that gives the reviewer memory across rounds — converging multi-round self-review workflows instead of surfacing new warnings indefinitely.**

**Status:** authoritative spec for the IAR subsystem. All implementation in `scripts/reviewer.py` and public surface in `action.yml` conforms to what is described here. If the code disagrees with this document, this document wins — file a bug.

**How to configure:** IAR runs on every review. The default `convergence-policy` is `first-pass-exhaustive`; the four tunable inputs (`convergence-policy`, `max-review-rounds`, `exhaustive-first-pass-cap-multiplier`, `iteration-escape-label`) shape the pipeline. Full input reference in § 3.

---

## Table of contents

1. [Motivation](#1-motivation)
2. [Failure-fallback safety contract](#2-failure-fallback-safety-contract)
3. [Inputs and outputs](#3-inputs-and-outputs)
4. [Generation model](#4-generation-model)
5. [Content-anchored fingerprints](#5-content-anchored-fingerprints)
6. [Convergence policies](#6-convergence-policies)
7. [Safety rails](#7-safety-rails)
8. [Escape hatch](#8-escape-hatch)
9. [Cost and latency model](#9-cost-and-latency-model)
10. [Walkthroughs](#10-walkthroughs)
11. [Recommended configurations](#11-recommended-configurations)
12. [Reference — `IterationState` JSON schema v1](#12-reference--iterationstate-json-schema-v1)
13. [Known limitations](#13-known-limitations)

---

## 1. Motivation

The AI Diff Reviewer is designed to be re-run every time a developer pushes commits to a PR. Each run is **stateless** by default: the model reviews the current diff without any memory of what prior runs reported. In practice, this produces a specific frustrating pattern:

> **The "infinite loop" symptom.** Round 1 surfaces 3 findings. Developer fixes them, pushes. Round 2 surfaces 3 *different* findings. Developer fixes those, pushes. Round 3 surfaces 3 *more* different findings. And so on. Ten rounds later, the developer is exhausted and merges anyway.

Two independent forces cause this:

1. **LLM non-determinism.** Even on the same diff, the model may notice different issues on different runs.
2. **Diff evolution.** Each round of fixes changes the code, which surfaces new issues in the newly-added lines.

Neither is a bug — reviewers really do find real issues. The problem is the **shape** of the feedback: a slow trickle of new findings feels adversarial, especially when the reviewer keeps re-flagging things you already know about.

IAR reshapes the feedback:

- **First-pass exhaustive**: prefer 20 findings on round 1 to 3 findings on round 1 + 3 more per round for 5 rounds.
- **Dedup**: don't re-flag findings you've already reported that remain open — the developer already knows.
- **Generation reset**: when the developer pushes new commits, review the new content as if it were a fresh review — don't silence critical bugs in the new code just because you've seen a lot of the file already.
- **Safety rails**: `critical` severity findings ALWAYS surface, unconditionally. No policy can silence them.
- **Escape hatch**: developer can apply the `iteration-escape-label` to force a full review whenever they want to see everything again (one-shot, state preserved), or remove the `applied-label` before the next run to force a complete state reset (fresh generation, dedup memory wiped).

IAR ships with the `first-pass-exhaustive` policy — the shape the maintainer team recommends for the majority of consumers. Consumers who want a different convergence profile can pick one of the other three policies (see § 6).

---

## 2. Failure-fallback safety contract

**Contract:** when the IAR pipeline fails mid-flight (any exception in `run_iar_pre_llm()` or `run_iar_post_llm()`), the reviewer MUST still ship a review. The IAR outputs stay as empty strings (via `write_iar_outputs_empty()`), the tracking marker skips the IAR annotation, and the review posts with the raw LLM findings — the consumer never experiences an IAR bug as "no review at all".

This is enforced by two independent mechanisms:

1. **Structural try/except at every call site.** Both `run_iar_pre_llm()` and `run_iar_post_llm()` are wrapped in `try/except Exception` in `main()`. On failure the caller logs the exception, leaves the safety-net empty outputs in place, and continues to the baseline review path. `Exception` (not `BaseException`) is deliberate — the reviewer still wants `SystemExit` and `KeyboardInterrupt` to propagate (a shell `SIGINT` or an intentional `sys.exit()` must not be swallowed by IAR's safety net).
2. **Regression test suite.** `tests/test_iar_failure_fallback.py` locks the invariant: garbled env vars still produce a valid `IARConfig`, the safety-net writer writes exactly 5 empty outputs, and `write_all_outputs()` on every exit path (skip, success, block) always includes the 5 IAR outputs. This suite runs in CI on every PR and MUST stay green.

**Ways this can be broken:**
- Removing a try/except around an IAR call site would let an IAR bug surface as a workflow failure.
- Changing `write_iar_outputs_empty()` to write fewer than 5 keys would break downstream steps that key on `steps.review.outputs.iteration-round`.
- Making `build_iar_config()` raise on any input would crash the run before the safety net is even in place.

All three would fail CI. This is by design.

---

## 3. Inputs and outputs

### 3.1 Inputs (4 — all optional, tune the pipeline)

| Input | Default | Type | Description |
|---|---|---|---|
| `convergence-policy` | `first-pass-exhaustive` | enum | One of `iterative` \| `first-pass-exhaustive` \| `round-capped` \| `critical-gate`. See § 6. |
| `max-review-rounds` | `0` | integer | Hard cap for `round-capped` policy. `0` = unlimited. Ignored by other policies. See § 6.3. |
| `exhaustive-first-pass-cap-multiplier` | `3` | integer | Multiplier applied to `max-inline-comments` on round 1 of each generation when policy is `first-pass-exhaustive`. Ignored by other policies. See § 6.2. |
| `iteration-escape-label` | `full-review-please` | string | Label name a human can apply to a PR to force a full review (clears dedup for this run only; does NOT mutate persisted state). See § 8. |

### 3.2 Outputs (5 — 3 core + 2 cost telemetry)

| Output | Type | When populated |
|---|---|---|
| `iteration-round` | integer string | Round number within the current generation. `1` on first review, resets to `1` on generation change. Empty string if the IAR pipeline crashed. |
| `iteration-generation` | integer string | Generation counter. Increments on new commits or rebase. Empty string if the IAR pipeline crashed. |
| `iteration-policy-applied` | string | Which policy actually fired this run. Usually matches `convergence-policy`; the 30% safety net overrides it to `safety-net-forced-first-pass-exhaustive`, and the escape label overrides it to `escape-label-forced-full-review`. Consumers should key on the full override string (grep `policy=\`safety-net-forced-` / `policy=\`escape-label-forced-`), not the short policy name. Empty string if the IAR pipeline crashed. |
| `iteration-tokens-used` | integer string | Total tokens this review actually consumed — every input partition (uncached, cache-read, cache-write, each counted once) plus output —, captured from the provider — API `usage` objects (`anthropic` / `openai`), the Claude Code stream-json `result` event, Codex `--json` `turn.completed` events, or the Grok JSON document. `0` when the provider reports nothing (Cursor). The tracking comment shows the same numbers with cache ratio, turns and an indicative cost; never gate CI on the value. Empty string ONLY if the IAR pipeline crashed. |
| `iteration-cost-vs-baseline-estimate` | string | Coarse cost-delta heuristic derived from cap expansion (`effective_cap / base_cap`) plus a small prompt-addendum flag. Always non-negative today: `"0%"` when no cap expansion fires; `"+N%"` when round 1 of `first-pass-exhaustive` or the safety net raises the cap. Silenced-finding savings are not yet modelled (see § 9.5); the promised `"-N%"` / `"unknown"` values do NOT ship in this cost function today — do not gate CI steps on them. Empty string if the IAR pipeline crashed. |

### 3.3 Environment variable mapping

Each input is forwarded to `scripts/reviewer.py` via the existing `AIPRR_*` convention:

| Input | Env var |
|---|---|
| `convergence-policy` | `AIPRR_CONVERGENCE_POLICY` |
| `max-review-rounds` | `AIPRR_MAX_REVIEW_ROUNDS` |
| `exhaustive-first-pass-cap-multiplier` | `AIPRR_EXHAUSTIVE_FIRST_PASS_CAP_MULTIPLIER` |
| `iteration-escape-label` | `AIPRR_ITERATION_ESCAPE_LABEL` |

The `AIPRR_*` prefix is a private convention (see `AGENTS.md` Rule #4 for its stability status).

### 3.4 Load-bearing dependency on `tracking-comment: true`

IAR persists state by embedding a `<!-- ai-pr-reviewer-iteration-state -->` JSON block inside the reviewer's tracking marker comment on the PR. If the consumer disables the tracking comment (`tracking-comment: false`), `gh_update_issue_comment` sees `comment_id <= 0` and the embed becomes a silent no-op — IAR still runs in memory, but nothing gets persisted for the next run to read. Every subsequent run then reads `prior_state = None` and classifies as `first_review`, re-firing round-1 exhaustive under the shipped default policy.

**Consequence.** `tracking-comment: true` (the default) is a hard requirement for IAR convergence. Disabling it does not disable IAR outright — it disables IAR *persistence*, which is the same thing from a cost/behaviour standpoint. Consumers who want to disable IAR entirely have no supported knob today; the recommended path is to keep the tracking comment on and tune `convergence-policy` (`iterative` is the lightest option that still deduplicates within a generation). See [`ARCHITECTURE.md § Iteration-Aware Review`](ARCHITECTURE.md).

---

## 4. Generation model

### 4.1 Concept

A **generation** is a stable diff-content window. Within a single generation, the reviewer may run multiple rounds (typically 1-3 rounds until convergence). When the developer pushes new commits or the branch is rebased, the diff content changes → a new generation begins.

This directly solves the "push new commits after convergence" concern: the round counter resets to 1, and any first-pass policies (like `first-pass-exhaustive`) re-fire on the new content instead of accidentally silencing critical bugs.

### 4.2 The 5 transition types

```python
class GenerationTransition(str, Enum):
    FIRST_REVIEW = "first_review"           # No prior state — starting from scratch.
    SAME_GENERATION = "same_generation"     # HEAD unchanged since last review.
    NEW_COMMITS = "new_commits"             # HEAD advanced; base unchanged.
    REBASED = "rebased"                     # Base SHA changed (branch rebased).
    USER_FORCED_RESET = "user_forced_reset" # Reviewed label removed → full reset. See § 8.5.
```

Detection logic (deterministic, no ambiguity):
- `prior_state is None` → `FIRST_REVIEW`
- `prior_state.generation_range_hash == current_range_hash` → `SAME_GENERATION`
- `prior_base_sha != current_base_sha` → `REBASED` (takes precedence over NEW_COMMITS)
- Otherwise → `NEW_COMMITS`

After the four-way classification above, one **override** may fire: if ALL FIVE of the following are true, the transition is upgraded to `USER_FORCED_RESET` and `prior_state` is discarded before any downstream logic runs:

1. The consumer's `applied-label` (the "reviewed" label the action stamps on every successful review) is non-empty.
2. `prior_state is not None` (i.e., a previous review exists in the marker chain).
3. `prior_state.reviewed_label_applied == True` (i.e., the reviewer previously succeeded at stamping the reviewed label — see § 8.5 for why this condition is load-bearing).
4. `label_fetch_ok == True` — the `_fetch_pr_labels` REST call SUCCEEDED (as opposed to returning `([], False)` on a transient network failure). Without this guard, a GitHub 5xx would silently look identical to "label absent" and fire a false reset, wiping fingerprint memory on every transient outage (round-14 F1).
5. That label is absent from the PR's current labels at trigger time.

Behaviorally identical to `FIRST_REVIEW` (fresh state, no dedup memory, round-1 exhaustive under the default policy) — the separate enum value only exists so the log and marker annotation can tell developers the reset was a deliberate gesture. Two of the five conditions are load-bearing safety guards: condition (3) — `reviewed_label_applied == True` — prevents a blocked run (whose review posted but whose reviewed-label stamp was suppressed by the strictness gate) from being misread as a deliberate reset on the natural re-trigger. Condition (4) — `label_fetch_ok == True` — prevents a transient GitHub 5xx returning an empty label list from being misread as "label absent" and silently wiping fingerprint memory on every outage. Full spec + full failure-mode analysis in § 8.5.

### 4.3 The range hash

The generation is anchored by a **range hash** — a deterministic SHA256 of the git diff content:

```python
def compute_generation_range_hash(base_sha: str, head_sha: str) -> str:
    result = subprocess.run(
        # Three-dot: matches `fetch_pr_context`'s `origin/<base>...HEAD`
        # so upstream base-branch movement does not flip the hash while
        # the PR-visible diff stays the same.
        ["git", "diff", f"{base_sha}...{head_sha}"],
        capture_output=True, check=True, text=True,
    )
    return hashlib.sha256(result.stdout.encode()).hexdigest()[:16]
```

Two commits producing the same diff (rare but possible via cherry-pick / revert cycles) → same hash → same generation. This is intentional: if the code content is truly identical, we shouldn't create false-positive generation changes.

**Why three-dot, not two-dot:** three-dot diff (`base_sha...head_sha`) pins the comparison to the merge base of the two commits — matching exactly what `fetch_pr_context` sends to the LLM as the review payload. Two-dot would recompute the hash every time `origin/<base>` moved upstream even though the PR-visible content is unchanged, producing false `NEW_COMMITS` / `REBASED` transitions on any label-gated re-review after the base branch advanced (burning a full exhaustive pass and re-surfacing already-open warnings). `compute_new_lines_pct` uses the same three-dot convention for its `total` denominator, so the safety net stays proportional to what the LLM actually reviewed.

### 4.4 What survives across generations

When a new generation begins (`NEW_COMMITS` or `REBASED`):

- ✅ **`resolved_fingerprints`** — findings that were fixed in prior generations remain marked as resolved. Same finding can't come back unless the code changes bring it back.
- ✅ **`history[]`** — appended (not reset). Provides audit trail across generations.
- ❌ **`round_in_generation`** — resets to `1`.
- ❌ **`open_fingerprints_this_gen`** — resets to empty (will be populated by this run's dedup engine).
- ❌ **`generation_range_hash`** — replaced with the new value.
- ↑  **`generation`** — increments by 1.

### 4.5 Generation change is loud + auditable

Every generation transition emits a debug log line:

```
IAR: generation change detected (new_commits). Prior: gen=1, rounds=3, range_hash=abc123. New: gen=2, range_hash=def456.
```

And the marker shows the iteration status as a short italic footer under the H3 title (the H3 itself keeps the short `AI review for <sha> — <status>` shape — the transition/gen/round detail goes in the annotation line):

```
### AI review for def456 — ✅ done

...standard review summary + strictness gate lines...

_Iteration-Aware Review: gen 2, round 1, policy=`first-pass-exhaustive` (new_commits) — 8 surfaced._
```

Developers can audit any PR by grepping the tracking marker comments for `gen \d+, round \d+` (lowercase, comma-separated — matching what `_render_iar_marker_annotation` actually emits). The transition name in parentheses is one of the five natural values: `(first_review)`, `(new_commits)`, `(rebased)`, `(same_generation)`, `(user_forced_reset)`. Policy overrides — safety-net and escape-label — do NOT appear in the transition slot; they surface via the `policy_applied` slot prefixed with `safety-net-forced-` (e.g. `policy=`safety-net-forced-first-pass-exhaustive` (new_commits)`) or renamed to `escape-label-forced-full-review` (e.g. `policy=`escape-label-forced-full-review` (same_generation)`). To audit those overrides, grep `policy=\`safety-net-forced-` or `policy=\`escape-label-forced-` — the transition value continues to describe the actual generation delta.

---

## 5. Content-anchored fingerprints

### 5.1 Why "content-anchored"

A naive fingerprint of `(path, line, severity, body)` is not sufficient. Consider:

- Round 1 reports `warning at src/auth.ts:55 — missing null check on user.id`. Developer fixes it.
- Later, developer pushes a new commit that adds different code at `src/auth.ts:55` — this new code has a genuine `critical` bug on the same line number.
- With a naive fingerprint, the round-1 warning fingerprint would match, and dedup would silence the new critical. **This would be a serious bug.**

Content-anchored fingerprints solve this by including a hash of the ~20 lines of code around the anchor. When the surrounding code changes, the fingerprint changes, and the new critical surfaces correctly.

### 5.2 The algorithm

```python
IAR_CONTEXT_HASH_RADIUS: int = 10  # lines above + below the anchor

def finding_fingerprint(finding: Finding, code_context: CodeContext | None) -> str:
    if code_context is not None:
        context_lines = code_context.lines_around(finding.line, radius=IAR_CONTEXT_HASH_RADIUS)
        context_hash = hashlib.sha256("\n".join(context_lines).encode()).hexdigest()[:16]
    else:
        context_hash = "no_context"  # file didn't exist at review SHA
    return hashlib.sha256(
        f"{finding.path}|{finding.line}|{finding.severity}|"
        f"{finding.body[:IAR_FINGERPRINT_BODY_PREFIX_CHARS]}|{context_hash}".encode()
    ).hexdigest()[:16]
```

Note the body is truncated to `IAR_FINGERPRINT_BODY_PREFIX_CHARS = 200` chars (module-level constant next to `IAR_CONTEXT_HASH_RADIUS`): catches meaningful content differences without being brittle to trivial LLM output variation.

### 5.3 Behavior matrix

| Situation | Old fingerprint | New fingerprint | Dedup applies? |
|---|---|---|---|
| Same warning, same file/line, code around anchor unchanged | `abc123` | `abc123` | ✅ Yes — dedup silences (correct: developer already knows) |
| Same warning, same file/line, **code around anchor changed** | `abc123` | `def456` | ❌ No — surface (correct: code changed, warning may have new significance) |
| Same warning, same file/line, **body text substantially different** | `abc123` | `ghi789` | ❌ No — surface (correct: LLM found something different) |
| Different anchor (file, line, or severity) | — | `jkl012` | ❌ No — surface (new finding) |

### 5.4 Code context source

The reviewer loads the code context from the file content at the **review SHA** (not from the working tree) via `git show <sha>:<path>`. This ensures the context hash is stable regardless of what the working tree looks like when the reviewer runs.

If the file didn't exist at the review SHA (e.g., newly added file, or a race condition), `code_context` is `None` and the fingerprint uses `"no_context"` as a fallback. This makes the fingerprint less stable in that edge case (small chance of not deduping when we should), which is the safer failure mode.

### 5.5 What fingerprints do NOT capture

Explicitly out of scope:
- **Semantic equivalence.** Two warnings with different body text but the same underlying meaning get different fingerprints. This is fine — dedup is a UX optimization, not a proof of soundness.
- **Cross-file reasoning.** A warning "the function you're calling here has a broken contract" wouldn't dedup if the callee moved to a different file.
- **Fuzzy matching.** No embedding, no LLM-based similarity. All hashes are deterministic.

---

## 6. Convergence policies

Four policies, selected via the `convergence-policy` input. All policies enforce the safety rails from § 7 (critical-always-surfaces, 30% safety net) — this is documented per-policy for clarity but the guarantee is orthogonal.

### 6.1 `iterative` (cost-neutral alternative)

**Behavior:** dedup only. No prompt splicing, no cap adjustment.

**Round 1 of Gen 1:** all findings surface (no prior state to dedup against).
**Round N of Gen G (N > 1):** dedup silences findings whose fingerprints match `open_fingerprints_this_gen`. Critical severities bypass unconditionally.

**When to use:** cost-sensitive repos, push-heavy workflows where the round-1-of-new-gen boost of `first-pass-exhaustive` would fire frequently. This is the closest-to-flat-cost policy — cost delta ~0% vs a no-dedup baseline.

**Trade-off:** doesn't accelerate convergence beyond dedup. If the LLM's per-round finding output is non-deterministic (typical), convergence still takes multiple rounds — just without re-flagging already-reported findings.

### 6.2 `first-pass-exhaustive` (shipped default)

**Behavior:** on round 1 of each generation, the reviewer is instructed to be exhaustive with a higher findings cap. On rounds 2+ within the same generation, delegates to `iterative`.

**Round 1 of any generation:**
- `max-inline-comments` cap is multiplied by `exhaustive-first-pass-cap-multiplier` (default 3).
- A hardcoded prompt addendum is spliced into the system prompt asking the model to prioritize exhaustive coverage over conciseness.

**Round 2+ of the same generation:** dedup only (same as `iterative`).

**When generation resets** (new commits / rebase): round 1 fires exhaustive again on the new content.

**When to use:** repos where "surface everything upfront" is preferred over "trickle findings". This is the direct answer to the *"prefer 20 warnings at once vs 10 loops"* pain.

**Trade-off:** round 1 tokens are ~1.5-2x higher. Total lifetime cost is typically LOWER because convergence happens in 1-2 rounds instead of 5-10. Documented in § 9.

### 6.3 `round-capped`

**Behavior:** full policy pre-cap (iterative dedup). Once `round_in_generation > max-review-rounds`, only critical findings surface. Warnings and infos are silenced with a "cap reached" annotation.

**Requires:** `max-review-rounds > 0` (default `0` = unlimited = same as `iterative`).

**When to use:** repos with strict "N attempts and ship it" convergence contracts. Combined with `strictness: block-on-critical`, this guarantees the check status never fails on warnings post-cap.

**Trade-off:** genuine post-cap warnings do not surface. A silenced warning could theoretically be a real problem. Mitigation: developer can apply the `iteration-escape-label` to force a full review at any time.

⚠️ **Interaction with strictness:** if you use `round-capped` post-cap AND `strictness: block-on-warning`, silenced warnings won't be able to block the check because they never surface. Documented in `docs/STRICTNESS.md`.

### 6.4 `critical-gate`

**Behavior:** all severities always surface (no cap), but **strict cross-generation dedup** for findings marked as resolved in prior generations.

**Difference from `iterative`:** in `iterative`, a `resolved_fingerprint` that reappears in a later generation is treated as a regression signal and surfaces. In `critical-gate`, it stays silenced (unless critical). This is a stricter posture: "if I said I fixed it, don't nag me about it again".

**When to use:** teams where the developer explicitly manages resolution status and doesn't want prior-resolved warnings resurfacing. The critical safety rail still applies — reappearance of a critical still surfaces.

**Trade-off:** if a fingerprint match is a false positive (LLM found a different-but-similar issue), it stays silenced. Rarer than the code-context-hash makes it seem.

---

## 7. Safety rails

Two safety rails apply to **every** policy, unconditionally. They are non-negotiable design commitments.

### 7.1 Critical severity findings ALWAYS surface

**Hardcoded rule.** Not a knob. Not a policy variant. Not conditional.

**v3 (RFC-03):** the rail keys on the *claimed* severity (`is_critical_claim`: `severity == critical` **or** `severity_claimed == critical`). The verifier's severity policy may publish a claimed critical as an annotated `warning`; that changes the label, never the visibility — dedup, the round cap and the criticals-first sort still treat it as critical.

In `scripts/reviewer.py`, the dedup engine (`dedupe_findings_against_prior`) contains the following early-return:

```python
# CRITICAL SAFETY RAIL (docs/ITERATION_AWARENESS.md § 7.1):
# NEVER silence critical severity findings under any circumstance.
# This rule is hardcoded and MUST NOT be moved into a policy or made
# configurable. Doing so is a bug.
if finding.severity == "critical":
    surfaced.append(finding)
    continue
```

**Test coverage.** `tests/test_iar_dedup.py::TestCriticalAlwaysSurfaces` contains ≥ 4 tests that construct adversarial cases (critical in `known_open`, critical in `known_resolved`, multiple criticals, criticals across generations) and assert that ALL surface. If any test in that class fails, the entire subsystem is considered broken.

**Consequence.** No developer using IAR can accidentally silence a critical bug. Even if they set `max-review-rounds: 1` and post-cap silencing kicks in, the critical still surfaces (post-cap filter is `f.severity == "critical"`).

### 7.2 30% new-lines safety net (auto-exhaustive)

**When triggered.** On generation change (`NEW_COMMITS` or `REBASED`), if the new lines added exceed `IAR_SAFETY_NET_NEW_LINES_PCT = 30` of the total diff, the safety net overrides the configured policy and forces `first-pass-exhaustive` for that round-1 pass.

**Rationale.** A big PR growth is exactly when a subtle bug is most likely to hide. Auto-exhaustive ensures the review pays extra attention when the code has substantially changed.

**Detection.** `compute_new_lines_pct` uses `git diff --numstat` on the three-dot range (`current_base_sha...current_head_sha`) and computes `new_added / (total_added + total_removed) * 100`, where `new_added` counts only lines added between the prior review's head and the current head (`prior_head_sha..current_head_sha`, two-dot on purpose — both are head SHAs on the same branch). Context lines are NOT in the denominator (`--numstat` doesn't emit them; `--stat` would but the code deliberately picks `--numstat` for machine-parseable output).

**Loud + audible.** The marker annotation footer reflects the safety net via the `policy_applied` slot — the runtime prefixes the policy name with `safety-net-forced-` when the safety net overrides the configured policy. The `transition` slot keeps its natural value (`new_commits` or `rebased`); the safety net is a policy override, not a new transition kind, so no `safety_net_*` token appears in the transition slot. Actual output shape:

```
### AI review for def456 — ✅ done

...standard review summary + strictness gate lines...

_Iteration-Aware Review: gen 2, round 1, policy=`safety-net-forced-first-pass-exhaustive` (new_commits) — 18 surfaced._
```

To audit safety-net firings, grep the marker chain for `policy=\`safety-net-forced-` — that's the reliable signal.

Debug log entry:

```
IAR: safety net triggered (45.2% new lines exceeds threshold 30%) — forcing first-pass-exhaustive.
```

**Threshold configurability.** `IAR_SAFETY_NET_NEW_LINES_PCT` is currently a top-of-file constant (not an input). If a repo needs a different threshold, we'd add a new input in a future release rather than changing the default globally.

### 7.3 State persistence across the `collapse-previous` boundary

**The interaction.** `collapse-previous: true` (the shipped default) minimizes the reviewer's previous tracking marker on the next run so the PR conversation stays scannable. But that marker is exactly where IAR embeds its state block — so a naive implementation that only reads *visible* markers would see no prior state on every run and treat every review as `first_review`, which would defeat dedup + generation tracking entirely (every consumer on defaults would burn round-1 exhaustive on every run and never converge).

**The rescue.** `_fetch_latest_marker_body` uses a three-tier ordering to find the latest marker that carries state, in priority order:

1. **Newest non-minimized marker WITH an `IAR_STATE_TAG_OPEN` block** — the common path when `collapse-previous: false` or the current run is the first review since collapse.
2. **Newest minimized marker WITH state block** — the collapse-boundary rescue. Under `collapse-previous: true` the last real tracking comment has been minimized, but the state block is still in its body. Reading it here is what makes IAR persistence work on defaults.
3. **Newest marker of any shape** — back-compat fallback for callers who just want "the last marker we posted"; `_parse_state_from_marker_body` returns `None` if the body carries no state block, which downstream treats as a first review.

Tiers 1 and 2 combined guarantee that no consumer setting of `collapse-previous` causes IAR to lose state. Tier 3 keeps the fetcher useful for other callers (currently none in-tree, but the API is public).

**Log signature.** When tier 2 fires, the reviewer emits a `INFO`-level log line so the collapse-vs-IAR interaction is visible in the workflow log without turning on debug: `IAR: no visible marker carries state; falling back to the latest minimized marker with an embedded state block (this is expected under collapse-previous: true — the prior tracking comment was minimized between runs).`

**Test coverage:** [`tests/test_iar_state_layer.py`](../tests/test_iar_state_layer.py) `FetchLatestMarkerTests.test_prefers_visible_marker_with_state_over_minimized_with_state`, `test_falls_back_to_minimized_marker_with_state_when_no_visible`, `test_falls_back_to_any_marker_when_none_carry_state` lock the three-tier ordering.

**Fetch window ceiling.** `_fetch_latest_marker_body` reads the last **100** issue comments on the PR — the hard cap the v4 GraphQL endpoint enforces on the `pullRequest.comments` connection (server returns `EXCESSIVE_PAGINATION` for any higher value). On very long-lived PRs where more than 100 human/bot comments accumulate AFTER the last state-bearing marker was minimized, IAR can fail to find any marker in the window and classify the run as `first_review` — re-firing round-1 exhaustive under the shipped default policy. Failure mode is SAFE (over-review, never under-surface — no critical finding gets silenced), but convergence is defeated on that specific run. The clean fix is cursor pagination via `pageInfo.hasPreviousPage`; deliberately deferred to keep the runtime stdlib-only and the code path simple. If your PR pattern regularly hits this ceiling, the practical workaround is either the reviewed-label reset (§ 8.5) or explicitly deleting stale non-marker comments; a follow-up patch will add pagination when a real user reports the symptom.

---

## 8. Escape hatch

The `iteration-escape-label` input (default `"full-review-please"`) is a **human-triggered** escape from IAR's dedup logic.

### 8.1 Behavior

If the PR has the specified label attached when the reviewer runs:
- **Dedup is skipped for this run.** All findings the LLM produces surface (subject to the base `max-inline-comments` cap).
- **Persisted state is NOT mutated.** The marker's embedded state block is left unchanged from the prior review. When the label is removed and a subsequent review runs, IAR resumes from where it left off.
- **Prompt splicing is NOT applied.** The system prompt is unchanged from what would be sent without IAR.
- The marker footer reflects the escape via `policy_applied` — same shape as the safety-net override, but with the `escape-label-forced-full-review` policy name:

```
### AI review for abc123 — ✅ done

...standard review summary + strictness gate lines...

_Iteration-Aware Review: gen 3, round 6, policy=`escape-label-forced-full-review` (same_generation) — 12 surfaced._
```

The H3 stays short (`### AI review for <sha> — <status>`); the escape-label signal is in the `policy=` slot of the italic footer, not the title.

### 8.2 Why state is preserved

The escape label is meant for one-shot "I want to see everything again" moments — e.g., after a major refactor, or when the developer suspects IAR silenced something they need to see. Preserving state means the developer doesn't lose weeks of prior fingerprint history just because they clicked a button once.

If the developer wants to truly reset IAR state, use the reviewed-label removal gesture documented in § 8.5 (remove `applied-label` from the PR, then re-trigger the workflow — `USER_FORCED_RESET` fires and discards prior state on a clean slate). Do NOT try to reset by deleting the visible tracking-marker comment: under the shipped default `collapse-previous: true`, `_fetch_latest_marker_body`'s tier-2 fallback (§ 7.3) reads the latest **minimized** marker that still carries an IAR state block, so deleting the visible marker leaves the prior state accessible and the next run continues the generation instead of becoming `first_review`. Only deleting **every** marker in the PR conversation that contains `<!-- ai-pr-reviewer-iteration-state` would achieve a "delete-to-reset" outcome — the § 8.5 gesture is safer and does not touch the conversation history.

### 8.3 Recursion guard

The escape label is a plain label. It does NOT participate in `.github/workflows/*.yml` `on: pull_request.types: [labeled]` triggers unless the consumer explicitly adds it. Recommended: keep `full-review-please` out of any trigger list — it's a state input, not an event.

### 8.4 Customization

Consumers can rename the label via the `iteration-escape-label` input. Useful when the default conflicts with an existing repo convention.

### 8.5 Full reset via reviewed-label removal

Distinct from the escape label, there is a second **stateful** escape gesture that lets a developer force a complete IAR reset (as opposed to a one-run dedup skip). It uses the labels the workflow already has, so there is nothing new to configure.

**Gesture:** on a PR that has previously been through a successful (non-blocked) IAR run — i.e. the reviewer stamped the reviewed label (e.g. `ai-reviewed`, whatever the consumer set as `applied-label`) at the end of that run — the developer removes the reviewed label. On the next review the reviewer confirms that (a) the previous run's state records the label was in fact stamped, and (b) that same label is no longer on the PR, and interprets the removal as an intentional reset request.

**Effect (this run):**
- Transition classified as `USER_FORCED_RESET` (visible in the marker annotation and the workflow log).
- Prior state is discarded — generation counter resets to 1, round-in-generation resets to 1, `resolved_fingerprints` and `open_fingerprints_this_gen` start empty, `history` starts empty.
- The default `first-pass-exhaustive` policy fires round-1 exhaustive behaviour on a clean slate — identical to the very first review of the PR.
- The safety net is deliberately silenced (`new_lines_pct` forced to 0.0) because "fresh start" already implies exhaustive.

**Effect (subsequent runs):** whatever policy the consumer configured resumes normally, but the dedup timeline now starts from this reset point — prior findings that were resolved in earlier generations are treated as unseen and can resurface if the LLM finds them again.

**Why this exists.** The escape-label pattern (§ 8.1–8.4) is one-shot: dedup is bypassed for a single run and state carries on. The reset gesture is for the harder case — the developer suspects the accumulated state itself is wrong (stale fingerprints, drift after a big rebase, or simply "I want to start clean"). Removing the reviewed label is a gesture the developer already makes when they want to re-open a PR for review, so the cost is zero.

**How the two interact.**
- `full-review-please` label applied AND reviewed label removed → reset takes precedence. The next run is USER_FORCED_RESET (fresh start) rather than an escape-label run (dedup skipped, state preserved). Enforced in `dispatch_policy`: the escape-label short-circuit skips when `transition == USER_FORCED_RESET`, letting the reset's exhaustive first-pass path fire.
- Escape label alone → dedup skipped this run, state preserved.
- Reviewed label removed alone (with prior state recording a successful stamp) → USER_FORCED_RESET, state discarded.

**How the two are distinguished in the marker.** The two gestures surface in different slots of the italic footer, matching `_render_iar_marker_annotation`:

- **Escape label** (state preserved): the natural `GenerationTransition` is unchanged (`same_generation`, `new_commits`, …); `policy_applied` is renamed to `escape-label-forced-full-review`. Example footer: `policy=`escape-label-forced-full-review` (same_generation)`.
- **Reset** (state discarded): the transition itself becomes `USER_FORCED_RESET`; `policy_applied` follows the configured convergence policy (typically `first-pass-exhaustive`). Example footer: `policy=`first-pass-exhaustive` (user_forced_reset)`.

To audit these programmatically, grep the marker chain: `policy=\`escape-label-forced-` for escape usage; `(user_forced_reset)` for resets.

**The load-bearing safety guards.** The reset detection requires five conditions to fire — (1) `applied-label` is configured, (2) prior IAR state exists, (3) that prior state records the reviewer had previously stamped the label (`prior_state.reviewed_label_applied == True`), (4) the `_fetch_pr_labels` REST call succeeded (`label_fetch_ok == True`), and (5) the label is absent from the returned list. Two of the five are load-bearing safety guards:

- **Condition (3)** — `reviewed_label_applied` guard: without it, any blocked review (`block-on-critical` fired, so the reviewer never stamped the label) followed by the natural re-trigger would look identical to a deliberate reset and wipe fingerprint memory.
- **Condition (4)** — `label_fetch_ok` guard (round-14 F1): without it, any transient GitHub 5xx returning an empty label list would look identical to "label absent" and fire a false reset — the exact "infinite loop" symptom IAR is designed to prevent, but triggered by API instability rather than by convergence pressure. `_fetch_pr_labels` returns a `(labels, ok)` tuple specifically so this branch can distinguish "the API said no labels" from "we don't know if there are labels".

The bit is written at the end of each run as the OR of three signals — `label_stamped OR label_currently_on_pr OR prior_bit`:

- **`label_stamped`** — this run's `gh_apply_label` call succeeded (unblocked, permissions healthy, API responded).
- **`label_currently_on_pr`** — the label was already on the PR at trigger time (a previous run stamped it; this run may be blocked or a no-op re-trigger, but the label is still there).
- **`prior_bit`** — the previous state's `reviewed_label_applied` was `True` AND this run took a path that does not remove the label (blocked, escape-label, etc.). Preserving the prior bit here prevents a blocked follow-up from silently clearing the arming signal for a later legitimate reset gesture.

The bit becomes `False` only when NONE of these hold — i.e., the reviewer has never successfully stamped this label, it's not currently on the PR, and prior state does not record a successful stamp either. In that case there is nothing meaningful to "reset from" and the gesture correctly no-ops. For state written before the field existed (missing key in the JSON block), the parser defaults to `False` — the safe side of the guard, which suppresses the gesture until the reviewer completes one successful run and re-writes the state with the bit set.

**No-op cases.** The reset gesture does nothing when:
- `applied-label` is empty / not configured (the consumer opted out of the reviewed-label workflow entirely).
- No prior IAR state exists (first-ever review of the PR — no state to reset).
- The reviewed label is still on the PR at review time (nothing was removed).
- The prior state records `reviewed_label_applied=False` (the previous run was blocked or IAR itself failed, so the label was never on the PR — removing "the label" is nonsensical because it was never there).

**Test coverage:** [`tests/test_iar_observability.py`](../tests/test_iar_observability.py) `RunIarPreLlmTests.test_user_forced_reset_*` (five cases — fires, four no-ops) + [`tests/test_iar_state_layer.py`](../tests/test_iar_state_layer.py) `IterationStateRoundTripTests.test_roundtrip_preserves_reviewed_label_applied_bit` + `test_parses_pre_v1_state_without_reviewed_label_applied` (state persistence + backward compat) + [`tests/test_iar_dispatch.py`](../tests/test_iar_dispatch.py) `DispatchPolicyPrecedenceTests.test_user_forced_reset_beats_escape_label` (precedence).

---

## 9. Cost and latency model

### 9.1 Design principles

- **DON'T #9 compliance:** IAR does NOT modify `max_tokens` or `MAX_TURNS` defaults. The `exhaustive-first-pass-cap-multiplier` raises `max-inline-comments` (the tool-call ceiling), which affects output token DEMAND but does not change the per-call `max_tokens` budget.
- **Cost telemetry is free (zero LLM tokens).** `iteration-cost-vs-baseline-estimate` is derived locally from cap expansion + a small addendum flag. `iteration-tokens-used` is read from the provider's own usage report (v2.1.0+). Neither output adds LLM cost.

### 9.2 Lifetime cost matrix (theoretical — validated by dogfooding)

For a typical PR that would converge in 5 rounds without dedup:

| Round | Baseline (no dedup) | `iterative` | `first-pass-exhaustive` | `round-capped` (N=3) | `critical-gate` |
|---|---|---|---|---|---|
| Round 1 | X | X | 1.5-2X | X | X |
| Round 2 | X | 0.9X | 0.9X | 0.9X | 0.9X |
| Round 3 | X | ~0 (converged) | ~0 | 0 (post-cap critical only) | 2.7X |
| Round 4 | X | ~0 | ~0 | 0 | ~0 |
| Round 5 | X | ~0 | ~0 | 0 | ~0 |
| **Lifetime total** | **5X** | **~2.8X** | **~3.3X** | **~2X** | **~4.5X** |
| **vs baseline** | 0% | **−44%** | **−34%** | **−60%** | **−10%** |

> **Note:** these numbers are theoretical. Empirically measured values from self-review dogfooding will replace them over time. See [`docs/PERFORMANCE.md`](PERFORMANCE.md) for the current authoritative numbers.

### 9.3 Per-round wall-clock breakdown

| Phase | Baseline | With IAR (any policy) |
|---|---|---|
| Setup (git ops + state parse) | ~1s | ~2-3s |
| LLM call latency | ~30-60s typical | unchanged for `iterative`/`round-capped`/`critical-gate`; +30-60% on round 1 of `first-pass-exhaustive` (larger output) |
| Comment posting | ~5-10s | proportional to findings surfaced; net reduction on rounds 2+ from dedup |
| **Per-round total** | ~40-70s | ~40-105s (round 1 heaviest for exhaustive; comparable otherwise) |
| **Lifetime total (5 rounds → 2-3 rounds with IAR)** | ~200-350s | ~120-315s (typical 30-50% reduction) |

### 9.4 Worst-case cost impact + mitigation

**Worst case:** developer pushes new commits after every round → every round is generation-1-round-1 → `first-pass-exhaustive` re-fires every time → +15-30% cost vs baseline.

**Mitigations:**
1. Use `convergence-policy: iterative` instead — cost-neutral vs baseline while still deduping.
2. Lower `exhaustive-first-pass-cap-multiplier` to `2` or `1` to reduce round-1 amplification.
3. Set `max-review-rounds: 3` under `round-capped` to guarantee an upper bound.

### 9.5 Cost telemetry

Every IAR-enabled run emits a debug log line at end-of-run:

```
IAR cost: tokens=41234, git_ops_ms=1250, total_wall_clock_ms=47320, findings_surfaced=8, findings_silenced=3
Usage: source=estimated in=41200 cache_read=300000 cache_write=0 out=2100 turns=6 cost_usd=0.05
```

Two outputs allow programmatic access:
- `iteration-tokens-used` — **real since v2.1.0**: all input partitions (uncached, cache-read and cache-write, counted once) + output tokens captured from the provider (see § 3.2 for the per-provider source). The end-of-run `Usage:` log line and the tracking comment add cache reads/writes, turns and an indicative cost (`INDICATIVE_PRICES_USD_PER_MTOK`, dated in `scripts/reviewer.py`) or the vendor-reported cost when the CLI gives one (Claude Code, Grok). Estimates are for humans and dashboards — never gate CI on them.
- `iteration-cost-vs-baseline-estimate` — a **coarse, always-non-negative** heuristic derived from cap expansion (`effective_cap / base_cap`) plus a small addendum flag (`+5%` when the IAR exhaustive prompt addendum is spliced). Today the function returns either `"0%"` (no cap expansion) or `"+N%"` (round 1 of `first-pass-exhaustive` / safety net raises the cap). **The signal savings from silenced findings and `state.history[]` averages are not yet modelled** — a future revision may extend the function to emit `"-N%"` / `"unknown"` as originally sketched, but consumers today MUST NOT gate CI steps on those values (the condition will simply never fire).

Example: gate a downstream CI step on IAR cost:

```yaml
- name: Warn on IAR cost regression
  if: steps.review.outputs.iteration-cost-vs-baseline-estimate == '+20%'
  run: echo "::warning::IAR cost above expected baseline"
```

### 9.6 Persisted cost history

Each generation's cost is stored in `state.history[]`:

```json
{
  "gen": 1,
  "range_hash": "abc123",
  "rounds_ran": 3,
  "converged": true,
  "tokens_used": 0,
  "wall_clock_ms": 128000
}
```

> `tokens_used` is always `0` today — the per-provider usage-metadata capture path is not yet wired (see § 13.2). Examples keep the field so the schema shape is copyable; do not treat non-zero values as shipping behaviour.

Trends are inspectable across runs by reading the marker's embedded state block.

---

## 10. Walkthroughs

### 10.1 "10-round loop → 3-round convergence" (the primary win)

**Configuration:** `convergence-policy: first-pass-exhaustive`, `max-review-rounds: 0` (default — unlimited; ignored under `first-pass-exhaustive`), `cap-multiplier: 3`.

> Do **not** copy `max-review-rounds: 5` into this profile. That value is ignored under `first-pass-exhaustive`, so it looks harmless — but if you later switch to `round-capped`, it silently caps every PR at 5 rounds and silences non-critical findings past that point. Keep the default `0` unless you deliberately want the cap.

| Event | State transition | User-visible outcome |
|---|---|---|
| Dev opens PR | `FIRST_REVIEW` → Gen 1 round 1 | Reviewer runs with exhaustive prompt + 3x cap. Surfaces 22 findings. |
| Dev fixes 15 of them, pushes | (same generation — dev didn't add new code) `SAME_GENERATION` → Gen 1 round 2 | Reviewer runs with iterative dedup. Surfaces 5 findings (2 new, 3 carried-over-open). 15 findings silenced as "already reported". |
| Dev fixes remaining, pushes | `SAME_GENERATION` → Gen 1 round 3 | Reviewer surfaces 1 new finding + 0 carried-over. Dev fixes it, ships. **Converged in 3 rounds.** |

Without dedup: the same PR would have taken 5-10 rounds, each surfacing new low-priority findings that felt like nagging.

### 10.2 "Push new commits after convergence" (the correctness win)

**Scenario:** PR converged in Gen 1 (3 rounds, 22 findings resolved). Dev leaves it un-merged. A week later, dev pushes new commits to add a feature. Question: does IAR silence a critical bug in the new code?

**Answer: no.** Here's why:

| Event | State transition | What surfaces |
|---|---|---|
| Push new commits (`git commit && git push`) | `NEW_COMMITS` (range hash changed) → Gen 2 round 1 | Round counter resets. |
| Reviewer runs | Safety net check: 45% new lines → **auto-forces exhaustive** | Exhaustive prompt + 3x cap. |
| Model finds 8 findings on the new code | Fingerprint check: 8 new fingerprints (context hash reflects new code) | All 8 surface. Prior 22 resolved fingerprints don't match (different code context). |
| One of the 8 is a `critical` | Critical bypass rule (§ 7.1) | Surfaces unconditionally, even if it matched a prior fingerprint. |
| Marker footer | `_Iteration-Aware Review: gen 2, round 1, policy=`safety-net-forced-first-pass-exhaustive` (new_commits) — 8 surfaced._` | Developer sees clearly that this is a fresh review of new content — the `safety-net-forced-` prefix in `policy=` is the reliable audit anchor. |

### 10.3 "Force full review with escape label"

**Scenario:** Dev is about to merge. Wants a final review of everything, not just deltas.

**Steps:**
1. Dev applies `full-review-please` label to the PR.
2. Push a trivial commit to trigger review (or manually re-run the workflow).
3. Reviewer runs. Escape label detected. Dedup skipped for this run only. All findings surface.
4. Persisted state UNCHANGED. Dev inspects findings; decides to merge.
5. Dev removes the label. Next review (post-merge, if it happens) resumes normal IAR behavior.

Marker footer:

```
### AI review for def456 — ✅ done

...standard review summary + strictness gate lines...

_Iteration-Aware Review: gen 3, round 6, policy=`escape-label-forced-full-review` (same_generation) — 12 surfaced._
```

---

## 11. Recommended configurations

### 11.1 Balanced (shipped default)

This is what every consumer gets by default. **No YAML required** — the values below are the shipped defaults; the block is shown only for clarity:

```yaml
# All shipped defaults — do NOT need to be set.
convergence-policy: first-pass-exhaustive
max-review-rounds: 0                        # unlimited (only meaningful for round-capped)
exhaustive-first-pass-cap-multiplier: 3
iteration-escape-label: full-review-please
```

**Use when:** general-purpose repos, medium-sized PRs typical. This is the shipped profile; you don't have to configure anything.

### 11.2 Cost-sensitive

```yaml
convergence-policy: iterative
max-review-rounds: 0  # unlimited
```

**Use when:** cost is the primary concern. Cost delta ~0% vs a no-dedup baseline; you get dedup without the round-1 amplification.

### 11.3 Strict-convergence

```yaml
convergence-policy: round-capped
max-review-rounds: 3
strictness: block-on-critical  # not block-on-warning; see § 6.3 note
```

**Use when:** you need a hard upper bound on rounds. Post-cap, only criticals surface. Warnings are silenced (see the § 6.3 strictness interaction warning).

### 11.4 Discovery / no-nag

```yaml
convergence-policy: critical-gate
```

**Use when:** you explicitly manage resolution and don't want prior-resolved warnings resurfacing across generations. Critical safety rail still applies.

---

## 12. Reference — `IterationState` JSON schema v1

Embedded in the tracking marker comment as an HTML-comment block:

```
<!-- ai-pr-reviewer-iteration-state
{
  "version": 1,
  "generation": 2,
  "generation_range_hash": "def4567890abcdef",
  "round_in_generation": 1,
  "policy_applied": "first-pass-exhaustive",
  "resolved_fingerprints": ["fp1abc", "fp2def", "fp3ghi"],
  "open_fingerprints_this_gen": ["fp4jkl"],
  "history": [
    {
      "gen": 1,
      "range_hash": "abc1234567890abc",
      "rounds_ran": 3,
      "converged": true,
      "tokens_used": 0,
      "wall_clock_ms": 128000
    }
  ],
  "base_sha": "deadbeef00112233",
  "head_sha": "cafef00d55667788",
  "reviewed_label_applied": true
}
-->
```

> `tokens_used: 0` in the example matches shipping behaviour today (§ 13.2). Do not invent non-zero values in dashboards until the provider-hook follow-up lands.

### 12.1 Field definitions

| Field | Type | Description |
|---|---|---|
| `version` | integer | Schema version. Currently `1`. Future versions may add backward-read logic. |
| `generation` | integer | Monotonically increasing generation counter. `1` on `FIRST_REVIEW`. |
| `generation_range_hash` | string (16-char hex) | SHA256[:16] of `git diff base...HEAD` (three-dot; see § 4.3) for the current generation. |
| `round_in_generation` | integer | Round number within the current generation. `1` when generation begins. |
| `policy_applied` | string | Which policy actually fired (may differ from configured policy if safety net or escape label overrode). One of: `iterative`, `first-pass-exhaustive`, `round-capped-pre-cap`, `round-capped-post-cap`, `critical-gate`, `escape-label-forced-full-review`, plus safety-net variants. |
| `resolved_fingerprints` | list of strings | Fingerprints of findings that were open in a prior round/generation and are no longer produced by the reviewer (assumed resolved). Preserved across generations. |
| `open_fingerprints_this_gen` | list of strings | Fingerprints of findings that surfaced in the current generation and are still open. Reset when generation advances. |
| `history` | list of objects | Append-only summary of past generations. Each object: `{gen, range_hash, rounds_ran, converged, tokens_used, wall_clock_ms}`. Capped to the last 20 entries (`IAR_HISTORY_MAX_ENTRIES`) so the marker body cannot grow unboundedly across a long-lived PR. |
| `base_sha` | string | The PR base SHA at the start of the current generation. Used by `detect_generation_change` to distinguish `REBASED` (base changed) from `NEW_COMMITS` (base same, head grew). Optional in schema v1 — an empty string means "prior IAR version didn't persist this" and `detect_generation_change` falls back to `NEW_COMMITS` on any hash mismatch (safe fallback: extra exhaustive review, never silent silencing). |
| `head_sha` | string | The PR head SHA at the end of the run that wrote this state. Used by `compute_new_lines_pct` to measure how much has been added since — the input to the 30% new-lines safety net (§ 7.2). Optional in schema v1 — an empty string means "unknown prior head" and the safety net degrades to a no-op (never silences a review that would have benefited from an exhaustive pass; just skips the boost). |
| `reviewed_label_applied` | bool | Whether the `applied-label` is (or should be treated as) on the PR at the end of the run that wrote this state. Computed by `compute_reviewed_label_applied` as the OR of three signals: (1) this run's stamp succeeded, (2) the label was already on the PR at trigger time, (3) the prior state recorded `True` and this run took a path that does not remove the label. Load-bearing signal for the USER_FORCED_RESET gesture (§ 8.5) — the reset only fires when this bit was `True` in the prior state AND the label is absent from the PR now, which is the difference between "developer removed the label deliberately" and "reviewer never established the label workflow". Optional in schema v1 — defaults to `False`, which is the safe side (suppresses the gesture until the reviewer completes one successful run). |

### 12.2 Parse failure handling

If parsing the state block fails for ANY reason (malformed JSON, unknown version, missing fields, wrong types), `read_prior_iteration_state()` returns `None` + emits a debug log. The reviewer treats this as a first-review case.

This is the safest fallback: nothing worse than a normal (non-IAR) review can happen when state is missing or malformed.

### 12.3 Schema evolution

Future schema changes will:
1. Increment `IAR_STATE_SCHEMA_VERSION` in `scripts/reviewer.py`.
2. Add explicit backward-read logic to `_parse_state_from_marker_body` (e.g. `if data["version"] == 1: hydrate_v1(); elif data["version"] == 2: hydrate_v2()`).
3. Document the migration in a new `## 12.4 Schema v2` section here.

No schema version is ever removed. Marker state written by prior versions will always be readable by future runtimes.

---

## 13. Known limitations

Documented edge cases and follow-up items that consumers should be aware of. None of these break the primary IAR contract (convergence + critical-always-surfaces + failure-fallback); they are quality-of-implementation gaps tracked for future work.

### 13.1 Agent-runner overflow findings — resolved (v2.1.0)

The inline cap for the agent-runner path is now enforced **inside `run_iar_post_llm`, after fingerprinting** (`surface_cap` argument): every finding the CLI emitted is fingerprinted and recorded in `open_fingerprints_this_gen`, then the surfaced list is truncated criticals-first. Overflow findings are therefore known to the dedup engine on the next round instead of re-surfacing as "new". If the IAR pipeline is unavailable for a run, `main()` falls back to the previous pre-IAR truncation so the documented safety control still holds.

### 13.2 Per-generation telemetry — token capture resolved (v2.1.0), history attribution still pending

**Resolved in v2.1.0:** the metadata-capture path from provider responses into `RunTelemetry.tokens_used` is wired for every provider (API `usage` for `anthropic` / `openai`; Claude Code stream-json `result`; Codex `--json` `turn.completed`; Grok JSON document; Cursor reports nothing). `iteration-tokens-used` is real, and the tracking comment carries a `**Usage:**` line (tokens, cache ratio, turns, indicative or vendor-reported cost).

**Still pending:** per-generation attribution in `state.history[]`. On a `NEW_COMMITS` / `REBASED` transition `advance_generation` closes the prior generation's entry, and the current run's tokens belong to the *new* generation — so the closed entry keeps `tokens_used=0` rather than being back-filled with the wrong run. The clean fix remains accumulators on `IterationState` (`tokens_used_this_gen`, `wall_clock_ms_this_gen`) folded into `history[-1]` when the entry closes; deferred because the state schema is closed (`additionalProperties: false`) and a new field needs a schema generation bump.

### 13.3 Cost-vs-baseline heuristic is coarse and non-negative

`_estimate_cost_vs_baseline` combines cap-expansion (`effective_cap / base_cap - 1`) with a flat `+5%` addendum flag when the exhaustive prompt is spliced. It ignores `silenced_count` / `surfaced_count`, never consults `state.history[]`, and can only emit `"0%"` or `"+N%"` — the `"-N%"` / `"unknown"` values sketched in the original spec do NOT ship today.

**Consequence.** § 3.2, § 9.5, README, `action.yml`, and `skills/ai-diff-reviewer/setup/reference.md` now describe only the values the function actually returns. Consumers MUST NOT gate CI on `iteration-cost-vs-baseline-estimate == '-30%'` — the condition will never fire under the current implementation.

**Follow-up.** Extend the heuristic to (a) fold in silenced-finding token savings on `iterative` / `round-capped` rounds and (b) surface `"unknown"` when `state.history[]` lacks enough data to project a baseline. Once implemented, restore the `"-N%"` / `"unknown"` language across the four surfaces listed above.

### 13.4 `scripts/reviewer.py` has grown past the file-size ceiling

The IAR subsystem added ~1300 lines to `scripts/reviewer.py`, pushing the file to roughly 6700 LOC — comfortably past the ~4500-line soft ceiling in [`docs/STANDARDS.md § "File size"`](STANDARDS.md). Per Rule #2, the runtime is stdlib-only and lives in a single source file, so the standard splitting playbook (extract to a subpackage) trades the file-size ceiling against a load-bearing project invariant.

**Consequence.** New code, tests, and reviewers navigate a larger file than the standard recommends. The dedicated IAR subsections in `scripts/reviewer.py` are heavily commented and grouped with clear section banners (`# ---- IAR: <topic> ----`) to compensate.

**Follow-up.** Two paths were considered and deferred:
1. **Extract IAR to a submodule inside `scripts/`** (e.g. `scripts/iar/state.py`, `scripts/iar/dispatch.py`) imported by `scripts/reviewer.py`. Preserves stdlib-only, but changes the `run: python3 ${{ github.action_path }}/scripts/reviewer.py` composite-action contract's implicit "single-file runtime" assumption and would require the CI compile-check to walk the subpackage.
2. **Split `scripts/reviewer.py` into orchestrator + step scripts wired by `action.yml`**. Larger refactor; would enforce a natural boundary between the diff-fetching / IAR-state / LLM-call / posting phases but is out of scope for this PR.

Both paths deferred to a dedicated refactor PR that can be reviewed on its own without the IAR-behavioural noise mixed in.

---

## Related documentation

- [`docs/PERFORMANCE.md`](PERFORMANCE.md) — cost + latency numbers (theoretical here, empirical there after Task 10 dogfooding).
- [`docs/STRICTNESS.md`](STRICTNESS.md) — how IAR interacts with strictness modes (esp. `round-capped` + `block-on-warning`).
- [`docs/PROMPTS.md`](PROMPTS.md) — the exhaustive prompt addendum text.
- [`docs/ARCHITECTURE.md`](ARCHITECTURE.md) — where IAR slots into the runtime pipeline.
- [`docs/PR_REVIEW_WORKFLOW.md`](PR_REVIEW_WORKFLOW.md) — how to fetch marker comments (used by IAR to read prior state).
- [`examples/iteration-aware.yml`](../examples/iteration-aware.yml) — copy-paste recommended config.
- [`AGENTS.md`](../AGENTS.md) — repo-level rules (all 14 apply to IAR).

## Change log

- **v1 (2026-07-16):** initial spec authored during Task 1 of `PLAN_iteration_aware_review`. Post-launch corrections in the same day (three-dot generation range hash, five-condition USER_FORCED_RESET guard with three-signal `reviewed_label_applied` write logic + `label_fetch_ok` transient-failure guard, § 13 known-limitations catalogue) folded in during self-review dogfooding.

---

## 14. Incremental follow-up mode (v2.1.0+)

Rounds 2+ no longer re-review the whole PR. When a prior review exists and the delta can be trusted, the run switches to **incremental mode**:

### 14.1 When it engages

All of the following must hold — otherwise the run is a **full** review (the reason is logged as `IAR pre-LLM: … mode=full (<reason>)`):

- a prior IAR state exists and the transition is not `first_review` / `user_forced_reset` / `rebased` (including base movement);
- no policy override forced an exhaustive pass (`escape-label-forced-full-review`, `safety-net-forced-first-pass-exhaustive`);
- the previously reviewed head is an **ancestor** of the current HEAD (`git merge-base --is-ancestor`) — a rebase, force-push or amend makes the delta untrustworthy;
- at least one of the reviewer's own inline findings is still **open** on the PR.

There is no input to enable it; the `iteration-escape-label` is the per-PR off switch (one full pass, state preserved).

### 14.2 What the model receives

The `## Patches` section (v3; `## Full Diff` before v3) is replaced by:

1. `## Changes since your last review (<prior> → <head>)` — the actual two-tree delta between those heads, respecting file omissions before applying its own size limit;
2. `## Other files changed in this PR (unchanged since your last review)` — one line per remaining file;
3. `## Your prior findings still open (N)` — a table (criticals first, capped at 40) with the fingerprint, severity, location, a summary and whether the file changed since; read back from the PR's review threads (`fetch_prior_findings`: first comment authored by the bot, parent review carrying this provider's marker, inline marker present, thread not resolved).

The system prompt gains the incremental addendum instead of the exhaustive one, and the bundled prompt's "Follow-up reviews" section tells the model to verify rather than repeat.

### 14.3 The inline finding marker

Every inline comment now ends with a hidden, **stable** marker: `<!-- ai-pr-reviewer-finding: fp=<fingerprint> sev=<severity> -->` (registered in `docs/STANDARDS.md`). It is how prior findings are matched back without growing the tracking-marker state. Comments posted before v2.1.0 have no marker and are skipped (logged).

### 14.4 Verdicts and the resolution policy

The model reports `resolved`, `open` or `regressed` for each prior finding through `update_prior_finding` (chat-completions) or the `prior_findings` array of the findings file (agent-runners). What happens next is governed by the `prior-findings-resolution` input (v2.2.0+):

| Policy | A `resolved` verdict… | Thread on GitHub | Strictness gate |
|---|---|---|---|
| `advisory` (**default**) | is reported in the summary footer as *claimed resolved but unverified*; the finding stays in the outstanding set — **unless** its thread is already collapsed by `collapse-previous` and the claim is corroborated (§ 14.4.1, v2.3.1) | untouched — a maintainer resolves it | the finding keeps counting, except for a corroborated collapsed-thread retirement |
| `verified` | is honoured only when the runtime can corroborate it: the fingerprint is absent from this round **and** the file changed since the finding was raised (or no longer exists) | the runtime replies (`✅ Resolved in <sha> — verified by the reviewer…`) and resolves the thread, best-effort | the finding stops counting; `resolved_fingerprints` gains it |

Under both policies an edited file and an absent fingerprint are **not** taken as proof on their own: they are the corroboration required *in addition to* the model's verdict, and anything the runtime cannot corroborate stays open and is listed as unverified. `regressed` is model-asserted in both. Outstanding prior findings continue to contribute to the strictness gate even when the model correctly avoids reposting them; an incremental pass retains outstanding fingerprints instead of marking unmentioned findings resolved.

#### 14.4.0 The anchor re-read (v3, RFC-03 § Finding retirement — BC-08)

The corroboration above stays the **necessary** condition. Since v3 retirement also needs a **sufficient** one: the runtime re-reads the finding's anchor at the new head (`verify_anchor_fixed`) and compares the lines around it with the same lines at the head where the finding was raised (`PriorFinding.review_sha`). A corroborated `resolved` claim retires as `verified_fixed` only when those lines changed, or as `file_removed` when the path is gone. A claim whose anchor is **identical** stays open — the footer says `N kept open (anchor unchanged at head)` and the prior-findings ledger lists it as `claimed resolved, anchor unchanged at head` — so a file that changed elsewhere no longer retires a finding whose defect is untouched. When the raising head is not in the checkout (shallow clone, unknown review SHA) the re-read is unavailable and the v2 rule applies, with the reason recorded (`verified_fixed (anchor re-read unavailable …)`). Absence from a later review still never retires; `regressed` stays model-asserted. Retirement reasons are kept on `PriorFindingReconciliation.retired_reasons` and every current finding carries a `lifecycle.state` (`new` / `open` / `regressed`).

#### 14.4.1 The collapsed-thread escape (v2.3.1)

`advisory` retires a finding only when a maintainer resolves its thread. With `collapse-previous: true` — the default — every prior round's threads are minimized as `OUTDATED` on the next push, so that path disappears: an outstanding `critical` gates the check forever, while the round-2 review body reports the finding fixed. Consumers saw the review say *approve* and CI stay red, with no way to unblock short of switching policy.

So when a finding's thread is already **collapsed** — `isMinimized` on its anchoring comment (`PriorFinding.is_collapsed`) — `advisory` applies the same corroboration test as `verified` and retires the finding. `isOutdated` is deliberately *not* part of the eligibility test: on a `collapse-previous: false` repo an outdated thread is still visible and resolvable, and "outdated" is a code-moved signal — evidence, not eligibility.

| Model verdict | Fingerprint re-emitted this round | File changed since raised, or gone | Thread collapsed | `advisory` outcome |
|---|---|---|---|---|
| `resolved` | no | yes | yes | **retired** — stops gating |
| `resolved` | no | yes | no | unverified — maintainer still owns it |
| `resolved` | yes | — | — | unverified — the model contradicted itself |
| `resolved` | no | no | yes | unverified — no corroboration |
| none / `open` | — | — | — | still open |

Corroboration is not weakened; `verified` keeps its semantics and — like `advisory` — now measures "file changed" since the finding was raised (next paragraph). New findings never take this path: a fresh `critical` blocks exactly as before.

**"File changed" means changed since the finding was raised.** Each prior finding carries the head SHA of the review that posted it (`PriorFinding.review_sha`, from `pullRequestReview.commit.oid`), and `compute_changed_since_raised()` runs one `git diff --name-only <review_sha> <HEAD>` per distinct review SHA. The last-round delta alone was an accident of round timing: a fix that landed in round 2 was uncorroboratable in round 3, or on a same-head re-run, because that round's delta no longer touched the file — so a PR that was already stuck stayed stuck after upgrading. Findings posted before 2.3.1 have no review SHA and keep the delta-only evidence; a SHA git cannot resolve (shallow clone) is absent from the map, which counts as *no evidence*, never as *changed*.

Because nobody signed off on these retirements, they are reported separately in the summary footer — `resolved 5 · … · 5 auto-retired (fix corroborated; thread already collapsed)` — so a green check is always traceable to why it went green. `verified` retirements are not labelled this way: they carry a reply on the thread instead.

The summary footer reports the **same** reconciliation the gate was decided on: `run_iar_post_llm()` stores it on `ReviewResult.prior_reconciliation` and the footer reads it back, so "resolved N" in the footer and "N fewer severities in the gate" can never diverge. If the post-LLM step crashed, the gate escalated every prior severity and the footer reports nothing as resolved.

The escape depends on an ordering invariant in `main()`: `gh_collapse_previous_reviews()` runs **before** `run_iar_pre_llm()` reads the threads, and it minimizes each review's inline comments as well as the review body — so by the time `fetch_prior_findings()` runs, a prior round's findings already carry `isMinimized: true`. Reordering those two steps would silently disable the escape; `tests/test_iar_gate_consistency.py` locks it.

#### 14.4.2 One source of truth for the check result

The model writes its `Recommendation:` line before the runtime knows the gate outcome, so the two could disagree. Since v2.3.1 `compute_check_gate()` is the single decision point — the review body, the tracking comment's `**Strictness gate:**` line and the process exit code all derive from one call, evaluated **before** the review is posted. Every review body ends with a runtime-written `> **Check status: ✅ passing | 🚫 failing**` block, and a model `Recommendation: approve` is rewritten to `request-changes` when the gate is failing. A review that recommends approval can no longer ship with a red check.

The review summary ends with `Since last review (<prior> → <head>): resolved N · still open M · regressed K · new J` (plus `· policy: verified` when opted in, `· N kept open (anchor unchanged at head)` since v3), and the tracking marker annotation carries `mode=incremental`. Since v3 the posted body is generated by the runtime from the findings array (counts, verification, the findings table, the bounded narrative, the refuted section and a `### Prior findings` ledger with each retirement's reason); the model's summary is the narrative inside it.

### 14.5 Budget scaling

`effective cap = max(3, ceil(base cap × delta ratio), prior open criticals)` and `max-turns = max(6, ceil(max-turns × delta ratio))`, both capped at the base values; the delta ratio is the new-lines percentage of the generation (floor 10 %). Prior critical findings are always re-listed first, so they can never starve.

### 14.6 Failure semantics

Unreadable or incomplete thread pagination, Git errors, a non-ancestor prior head and base movement select a **full** review. Reconciliation failures are logged; previously known severity is retained by the post-review gate, not cleared by absence of new comments. The dedup engine, the 30 % safety net, the escape label and the critical-always-surfaces rail are unchanged.

### Incremental context and history completeness

The focused body is `git diff <last-reviewed-head> <current-head>`, filtered before its own size limit, not filtered from an already-truncated full PR diff. Old hunks in the same file are not retransmitted solely because that file changed. Review-thread history uses bounded cursor pagination; incomplete pagination cannot enable incremental mode. Base changes also force full mode even when the old head remains an ancestor.
