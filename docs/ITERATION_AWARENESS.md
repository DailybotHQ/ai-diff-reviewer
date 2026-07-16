# Iteration-Aware Review (IAR)

> **On-by-default subsystem that gives the reviewer memory across rounds — converging multi-round self-review workflows instead of surfacing new warnings indefinitely.**

**Status:** authoritative spec for the IAR subsystem. All implementation in `scripts/reviewer.py` and public surface in `action.yml` conforms to what is described here. If the code disagrees with this document, this document wins — file a bug.

**Master switch (v1.8.0+):** `iteration-awareness-enabled: true` (default). The shipped `convergence-policy` is `first-pass-exhaustive`. Consumers who want the pre-v1.8 review shape can set `iteration-awareness-enabled: false` and the runtime becomes byte-identical to prior releases — all 4 other IAR inputs are then ignored.

---

## Table of contents

1. [Motivation](#1-motivation)
2. [Master switch + backward-compat contract](#2-master-switch--backward-compat-contract)
3. [Inputs and outputs](#3-inputs-and-outputs)
4. [Generation model](#4-generation-model)
5. [Content-anchored fingerprints](#5-content-anchored-fingerprints)
6. [Convergence policies](#6-convergence-policies)
7. [Safety rails](#7-safety-rails)
8. [Escape hatch](#8-escape-hatch)
9. [Cost and latency model](#9-cost-and-latency-model)
10. [Walkthroughs](#10-walkthroughs)
11. [Recommended configurations](#11-recommended-configurations)
12. [Migration guide](#12-migration-guide)
13. [Reference — `IterationState` JSON schema v1](#13-reference--iterationstate-json-schema-v1)

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
- **Escape hatch**: developer can apply a label to force a full review whenever they want to see everything again.

As of v1.8.0 IAR is **on by default** with the `first-pass-exhaustive` policy — the shape the maintainer team recommends for the majority of consumers. Consumers who prefer the pre-v1.8 review behavior can flip `iteration-awareness-enabled: false` and the runtime becomes byte-identical to prior releases (see § 2).

---

## 2. Master switch + opt-out contract

**Contract:** when `iteration-awareness-enabled: false` (explicitly set), the runtime is **byte-identical** to pre-v1.8 releases.

This is enforced by two independent mechanisms:

1. **Structural guard rails.** Every IAR code path in `scripts/reviewer.py` is gated at its call site in `main()`. When the master switch is off, none of the new code executes — the runtime falls through to the pre-IAR path unchanged.
2. **Regression test suite.** `tests/test_backward_compat_iar_off.py` captures the runtime's output shape with `iteration-awareness-enabled=false` (explicit opt-out) and asserts byte-identical behavior against a mock LLM baseline. This suite runs in CI on every PR and MUST stay green — it is the load-bearing contract for consumers who opt out of the v1.8.0 default flip.

**Ways this can be broken:**
- Removing the guard rails would break Test 1 of the regression suite.
- Changing the marker title format for IAR-off runs would break Test 2.
- Adding non-empty values to the 5 new outputs when IAR is off would break Test 3.
- Adding new debug log lines that fire for IAR-off runs would break Test 4.

All 4 would fail CI. This is by design.

**Migration guidance (v1.7 → v1.8):**
- Consumers on `@v1` who don't touch their workflow YAML get IAR + `first-pass-exhaustive` automatically at the next release. Round 1 of the next new-generation review will surface up to 3× as many findings as before (bounded by `exhaustive-first-pass-cap-multiplier`, default `3`); subsequent rounds delegate to `iterative` (dedup only, no cost change).
- Consumers who want the pre-v1.8 shape can add `iteration-awareness-enabled: false` to their workflow — the regression suite locks byte-identical behavior on that path.
- Consumers who want IAR but prefer the flat cost profile can add `convergence-policy: iterative` — dedup only, no cap boost.
- The critical-always-surfaces safety rail is hardcoded and cannot be silenced by any policy or opt-out setting.

---

## 3. Inputs and outputs

### 3.1 Inputs (5 — all optional, defaults ship IAR-on)

| Input | Default | Type | Description |
|---|---|---|---|
| `iteration-awareness-enabled` | `true` | boolean | **Master switch.** Ships on. Set to `false` for byte-identical pre-v1.8 behavior; all other IAR inputs are then ignored. |
| `convergence-policy` | `first-pass-exhaustive` | enum | One of `iterative` \| `first-pass-exhaustive` \| `round-capped` \| `critical-gate`. Only consulted when master switch is on. See § 6. |
| `max-review-rounds` | `0` | integer | Hard cap for `round-capped` policy. `0` = unlimited. Ignored by other policies. See § 6.3. |
| `exhaustive-first-pass-cap-multiplier` | `3` | integer | Multiplier applied to `max-inline-comments` on round 1 of each generation when policy is `first-pass-exhaustive`. Ignored by other policies. See § 6.2. |
| `iteration-escape-label` | `full-review-please` | string | Label name a human can apply to a PR to force a full review (clears dedup for this run only; does NOT mutate persisted state). See § 8. |

### 3.2 Outputs (5 — 3 core + 2 cost telemetry)

| Output | Type | When populated |
|---|---|---|
| `iteration-round` | integer string | Round number within the current generation. `1` on first review, resets to `1` on generation change. Empty string when IAR disabled. |
| `iteration-generation` | integer string | Generation counter. Increments on new commits or rebase. Empty string when IAR disabled. |
| `iteration-policy-applied` | string | Which policy actually fired this run (usually matches `convergence-policy`, unless overridden by the 30% safety net). Empty string when IAR disabled. |
| `iteration-tokens-used` | integer string | Sum of input+output LLM tokens for this run. Cost telemetry. Empty string when IAR disabled. |
| `iteration-cost-vs-baseline-estimate` | string | Heuristic estimate vs a projected non-IAR baseline (e.g. `"-30%"`, `"+15%"`, `"unknown"`). Empty string when IAR disabled. |

### 3.3 Environment variable mapping

Each input is forwarded to `scripts/reviewer.py` via the existing `AIPRR_*` convention:

| Input | Env var |
|---|---|
| `iteration-awareness-enabled` | `AIPRR_ITERATION_AWARENESS_ENABLED` |
| `convergence-policy` | `AIPRR_CONVERGENCE_POLICY` |
| `max-review-rounds` | `AIPRR_MAX_REVIEW_ROUNDS` |
| `exhaustive-first-pass-cap-multiplier` | `AIPRR_EXHAUSTIVE_FIRST_PASS_CAP_MULTIPLIER` |
| `iteration-escape-label` | `AIPRR_ITERATION_ESCAPE_LABEL` |

The `AIPRR_*` prefix is a private convention (see `AGENTS.md` Rule #4 for its stability status).

---

## 4. Generation model

### 4.1 Concept

A **generation** is a stable diff-content window. Within a single generation, the reviewer may run multiple rounds (typically 1-3 rounds until convergence). When the developer pushes new commits or the branch is rebased, the diff content changes → a new generation begins.

This directly solves the "push new commits after convergence" concern: the round counter resets to 1, and any first-pass policies (like `first-pass-exhaustive`) re-fire on the new content instead of accidentally silencing critical bugs.

### 4.2 The 4 transition types

```python
class GenerationTransition(str, Enum):
    FIRST_REVIEW = "first_review"       # No prior state — starting from scratch.
    SAME_GENERATION = "same_generation" # HEAD unchanged since last review.
    NEW_COMMITS = "new_commits"         # HEAD advanced; base unchanged.
    REBASED = "rebased"                 # Base SHA changed (branch rebased).
```

Detection logic (deterministic, no ambiguity):
- `prior_state is None` → `FIRST_REVIEW`
- `prior_state.generation_range_hash == current_range_hash` → `SAME_GENERATION`
- `prior_base_sha != current_base_sha` → `REBASED` (takes precedence over NEW_COMMITS)
- Otherwise → `NEW_COMMITS`

### 4.3 The range hash

The generation is anchored by a **range hash** — a deterministic SHA256 of the git diff content:

```python
def compute_generation_range_hash(base_sha: str, head_sha: str) -> str:
    result = subprocess.run(
        ["git", "diff", f"{base_sha}..{head_sha}"],
        capture_output=True, check=True, text=True,
    )
    return hashlib.sha256(result.stdout.encode()).hexdigest()[:16]
```

Two commits producing the same diff (rare but possible via cherry-pick / revert cycles) → same hash → same generation. This is intentional: if the code content is truly identical, we shouldn't create false-positive generation changes.

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

And the marker title shows the transition:

```
### AI review for def456 — done · Gen 2 round 1 (new commits since Gen 1 · 3 rounds ran on abc123 · converged) · 8 findings (5 new-in-gen, 3 carried-over-open, 2 critical-forced-surface)
```

Developers can audit any PR by searching the conversation for `Gen \d+ round \d+`.

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
        f"{finding.body[:200]}|{context_hash}".encode()
    ).hexdigest()[:16]
```

Note the body is truncated to 200 chars: catches meaningful content differences without being brittle to trivial LLM output variation.

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

**When to use:** cost-sensitive repos, push-heavy workflows where the round-1-of-new-gen boost of `first-pass-exhaustive` would fire frequently. This is the closest-to-baseline policy — cost delta ~0% vs pre-IAR.

**Trade-off:** doesn't accelerate convergence beyond dedup. If the LLM's per-round finding output is non-deterministic (typical), convergence still takes multiple rounds — just without re-flagging already-reported findings.

### 6.2 `first-pass-exhaustive` (shipped default, v1.8.0+)

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

**Detection.** Uses `git diff --stat` on the range: `added_lines / (added + removed + context) * 100`.

**Loud + audible.** The marker title reflects the safety net:

```
### AI review for def456 — done · Gen 2 round 1 (SAFETY NET: 45% new lines) · exhaustive first-pass forced · 18 findings (15 new-in-gen, 3 critical-forced-surface)
```

Debug log entry:

```
IAR: safety net triggered (45.2% new lines exceeds threshold 30%) — forcing first-pass-exhaustive.
```

**Threshold configurability.** `IAR_SAFETY_NET_NEW_LINES_PCT` is currently a top-of-file constant (not an input). If a repo needs a different threshold, we'd add a new input in a future release rather than changing the default globally.

---

## 8. Escape hatch

The `iteration-escape-label` input (default `"full-review-please"`) is a **human-triggered** escape from IAR's dedup logic.

### 8.1 Behavior

If the PR has the specified label attached when the reviewer runs:
- **Dedup is skipped for this run.** All findings the LLM produces surface (subject to the base `max-inline-comments` cap).
- **Persisted state is NOT mutated.** The marker's embedded state block is left unchanged from the prior review. When the label is removed and a subsequent review runs, IAR resumes from where it left off.
- **Prompt splicing is NOT applied.** The system prompt is unchanged from what would be sent without IAR.
- The marker title reflects the escape:

```
### AI review for abc123 — done · escape-label forced full review (state preserved) · 12 findings
```

### 8.2 Why state is preserved

The escape label is meant for one-shot "I want to see everything again" moments — e.g., after a major refactor, or when the developer suspects IAR silenced something they need to see. Preserving state means the developer doesn't lose weeks of prior fingerprint history just because they clicked a button once.

If the developer wants to truly reset IAR state, they can remove the marker comment manually — the reviewer treats that as a first-review case on the next run.

### 8.3 Recursion guard

The escape label is a plain label. It does NOT participate in `.github/workflows/*.yml` `on: pull_request.types: [labeled]` triggers unless the consumer explicitly adds it. Recommended: keep `full-review-please` out of any trigger list — it's a state input, not an event.

### 8.4 Customization

Consumers can rename the label via the `iteration-escape-label` input. Useful when the default conflicts with an existing repo convention.

---

## 9. Cost and latency model

### 9.1 Design principles

- **DON'T #9 compliance:** IAR does NOT modify `max_tokens` or `MAX_TURNS` defaults. The `exhaustive-first-pass-cap-multiplier` raises `max-inline-comments` (the tool-call ceiling), which affects output token DEMAND but does not change the per-call `max_tokens` budget.
- **Cost telemetry is free (zero LLM tokens).** The `iteration-tokens-used` and `iteration-cost-vs-baseline-estimate` outputs are populated from response metadata and local computations. They add no LLM cost.

### 9.2 Lifetime cost matrix (theoretical — validated by dogfooding)

For a typical PR that converges in 5 rounds pre-IAR:

| Round | Baseline (no IAR) | `iterative` | `first-pass-exhaustive` | `round-capped` (N=3) | `critical-gate` |
|---|---|---|---|---|---|
| Round 1 | X | X | 1.5-2X | X | X |
| Round 2 | X | 0.9X | 0.9X | 0.9X | 0.9X |
| Round 3 | X | ~0 (converged) | ~0 | 0 (post-cap critical only) | 2.7X |
| Round 4 | X | ~0 | ~0 | 0 | ~0 |
| Round 5 | X | ~0 | ~0 | 0 | ~0 |
| **Lifetime total** | **5X** | **~2.8X** | **~3.3X** | **~2X** | **~4.5X** |
| **vs baseline** | 0% | **−44%** | **−34%** | **−60%** | **−10%** |

> **Note:** these numbers are theoretical. Dogfooding data from Task 10 of `PLAN_iteration_aware_review` will replace these estimates with empirically measured values. See `docs/PERFORMANCE.md` for the current authoritative numbers.

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
IAR cost: input_tokens=8432, output_tokens=2103, git_ops_ms=1250, total_wall_clock_ms=47320, findings_surfaced=8, findings_silenced=3
```

Two outputs allow programmatic access:
- `iteration-tokens-used` = `input_tokens + output_tokens` (per run).
- `iteration-cost-vs-baseline-estimate` = heuristic `-X%` / `+Y%` / `unknown` based on `state.history[]` averages.

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
  "tokens_used": 24580,
  "wall_clock_ms": 128000
}
```

Trends are inspectable across runs by reading the marker's embedded state block.

---

## 10. Walkthroughs

### 10.1 "10-round loop → 3-round convergence" (the primary win)

**Configuration:** `iteration-awareness-enabled: true`, `convergence-policy: first-pass-exhaustive`, `max-review-rounds: 5`, `cap-multiplier: 3`.

| Event | State transition | User-visible outcome |
|---|---|---|
| Dev opens PR | `FIRST_REVIEW` → Gen 1 round 1 | Reviewer runs with exhaustive prompt + 3x cap. Surfaces 22 findings. |
| Dev fixes 15 of them, pushes | (same generation — dev didn't add new code) `SAME_GENERATION` → Gen 1 round 2 | Reviewer runs with iterative dedup. Surfaces 5 findings (2 new, 3 carried-over-open). 15 findings silenced as "already reported". |
| Dev fixes remaining, pushes | `SAME_GENERATION` → Gen 1 round 3 | Reviewer surfaces 1 new finding + 0 carried-over. Dev fixes it, ships. **Converged in 3 rounds.** |

Pre-IAR baseline: same PR would have taken 5-10 rounds, each surfacing new low-priority findings that felt like nagging.

### 10.2 "Push new commits after convergence" (the correctness win)

**Scenario:** PR converged in Gen 1 (3 rounds, 22 findings resolved). Dev leaves it un-merged. A week later, dev pushes new commits to add a feature. Question: does IAR silence a critical bug in the new code?

**Answer: no.** Here's why:

| Event | State transition | What surfaces |
|---|---|---|
| Push new commits (`git commit && git push`) | `NEW_COMMITS` (range hash changed) → Gen 2 round 1 | Round counter resets. |
| Reviewer runs | Safety net check: 45% new lines → **auto-forces exhaustive** | Exhaustive prompt + 3x cap. |
| Model finds 8 findings on the new code | Fingerprint check: 8 new fingerprints (context hash reflects new code) | All 8 surface. Prior 22 resolved fingerprints don't match (different code context). |
| One of the 8 is a `critical` | Critical bypass rule (§ 7.1) | Surfaces unconditionally, even if it matched a prior fingerprint. |
| Marker title | `Gen 2 round 1 (SAFETY NET: 45% new lines) · exhaustive first-pass forced · 8 findings (2 critical-forced-surface)` | Developer sees clearly that this is a fresh review of new content. |

### 10.3 "Force full review with escape label"

**Scenario:** Dev is about to merge. Wants a final review of everything, not just deltas.

**Steps:**
1. Dev applies `full-review-please` label to the PR.
2. Push a trivial commit to trigger review (or manually re-run the workflow).
3. Reviewer runs. Escape label detected. Dedup skipped for this run only. All findings surface.
4. Persisted state UNCHANGED. Dev inspects findings; decides to merge.
5. Dev removes the label. Next review (post-merge, if it happens) resumes normal IAR behavior.

Marker title:

```
### AI review for def456 — done · escape-label forced full review (state preserved) · 12 findings
```

---

## 11. Recommended configurations

Empirical defaults (theoretical; refined by dogfooding data in Task 10).

### 11.1 Balanced (SHIPPED DEFAULT — v1.8.0+)

This is what every consumer gets by default. **No YAML required** — the values below are the shipped defaults; the block is shown only for clarity:

```yaml
# All of these are the v1.8.0+ shipped defaults — do NOT need to be set.
iteration-awareness-enabled: true
convergence-policy: first-pass-exhaustive
max-review-rounds: 0                        # unlimited (only meaningful for round-capped)
exhaustive-first-pass-cap-multiplier: 3
iteration-escape-label: full-review-please
```

**Use when:** general-purpose repos, medium-sized PRs typical. This is the shipped profile; you don't have to configure anything.

### 11.2 Cost-sensitive

```yaml
iteration-awareness-enabled: true
convergence-policy: iterative
max-review-rounds: 0  # unlimited
```

**Use when:** cost is the primary concern. Cost delta ~0% vs baseline; you get dedup without the round-1 amplification.

### 11.3 Strict-convergence

```yaml
iteration-awareness-enabled: true
convergence-policy: round-capped
max-review-rounds: 3
strictness: block-on-critical  # not block-on-warning; see § 6.3 note
```

**Use when:** you need a hard upper bound on rounds. Post-cap, only criticals surface. Warnings are silenced (see the § 6.3 strictness interaction warning).

### 11.4 Discovery / no-nag

```yaml
iteration-awareness-enabled: true
convergence-policy: critical-gate
```

**Use when:** you explicitly manage resolution and don't want prior-resolved warnings resurfacing across generations. Critical safety rail still applies.

---

## 12. Migration guide

### 12.1 Upgrading from v1.7 → v1.8.0

**Nothing to do.** IAR is now on by default with the `first-pass-exhaustive` policy. On your next review after upgrading, you'll see:
- The marker annotation gains a line like `_Iteration-Aware Review: gen 1, round 1, policy=first-pass-exhaustive (first_review) — N surfaced._`
- Round 1 of each new generation may surface more findings than pre-v1.8 (bounded by `max-inline-comments × 3` under the default multiplier).
- Rounds 2+ of the same generation dedup against prior unresolved findings.
- 5 new action outputs populate on every run.

**No other changes needed.** Your existing inputs (`strictness`, `provider`, `api-key`, etc.) work unchanged. The critical-always-surfaces safety rail guarantees no finding that could gate the check is ever silenced.

### 12.2 Tuning after upgrade

If the default profile doesn't suit your workflow:

- Push-heavy repos → set `convergence-policy: iterative` (cost-neutral, dedup only, no round-1 boost).
- Want a hard upper bound on rounds → `convergence-policy: round-capped` with `max-review-rounds: N`.
- Manage resolution manually → `convergence-policy: critical-gate` (strict cross-gen dedup).
- Want to force a full review on demand → apply the `full-review-please` label (or your custom label name via `iteration-escape-label`).

### 12.3 Opting out of IAR entirely

Add `iteration-awareness-enabled: false` to your workflow. The runtime becomes byte-identical to pre-v1.8:
- No marker annotation.
- 5 IAR outputs are empty strings; downstream steps reading them see `""`.
- The 4 other IAR inputs are ignored.

Persisted state in old markers is IGNORED (not deleted). If IAR is re-enabled later, it will resume from that state.

### 12.3 First-PR expectations

The first PR reviewed with IAR enabled will:
- Be a `FIRST_REVIEW` transition (no prior state).
- Have no dedup to apply.
- Behave like a normal review (with the exhaustive prompt if `first-pass-exhaustive` is the policy).
- Persist state for the next round.

Subsequent runs on the same PR will begin exercising the dedup engine.

### 12.4 Monitoring

Track adoption via:
- `iteration-tokens-used` output — feed into cost dashboards.
- `iteration-round` output — flag PRs stuck at high round counts.
- `iteration-cost-vs-baseline-estimate` output — spot regressions.
- Marker titles containing `Gen \d+ round \d+` — indicate IAR-enabled reviews.

---

## 13. Reference — `IterationState` JSON schema v1

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
      "tokens_used": 24580,
      "wall_clock_ms": 128000
    }
  ],
  "base_sha": "deadbeef00112233"
}
-->
```

### 13.1 Field definitions

| Field | Type | Description |
|---|---|---|
| `version` | integer | Schema version. Currently `1`. Future versions may add backward-read logic. |
| `generation` | integer | Monotonically increasing generation counter. `1` on `FIRST_REVIEW`. |
| `generation_range_hash` | string (16-char hex) | SHA256[:16] of `git diff base..HEAD` for the current generation. |
| `round_in_generation` | integer | Round number within the current generation. `1` when generation begins. |
| `policy_applied` | string | Which policy actually fired (may differ from configured policy if safety net or escape label overrode). One of: `iterative`, `first-pass-exhaustive`, `round-capped-pre-cap`, `round-capped-post-cap`, `critical-gate`, `escape-label-forced-full-review`, plus safety-net variants. |
| `resolved_fingerprints` | list of strings | Fingerprints of findings that were open in a prior round/generation and are no longer produced by the reviewer (assumed resolved). Preserved across generations. |
| `open_fingerprints_this_gen` | list of strings | Fingerprints of findings that surfaced in the current generation and are still open. Reset when generation advances. |
| `history` | list of objects | Append-only summary of past generations. Each object: `{gen, range_hash, rounds_ran, converged, tokens_used, wall_clock_ms}`. Capped to the last 20 entries (`IAR_HISTORY_MAX_ENTRIES`) so the marker body cannot grow unboundedly across a long-lived PR. |
| `base_sha` | string | The PR base SHA at the start of the current generation. Used by `detect_generation_change` to distinguish `REBASED` (base changed) from `NEW_COMMITS` (base same, head grew). Optional in schema v1 — an empty string means "prior IAR version didn't persist this" and `detect_generation_change` falls back to `NEW_COMMITS` on any hash mismatch (safe fallback: extra exhaustive review, never silent silencing). |

### 13.2 Parse failure handling

If parsing the state block fails for ANY reason (malformed JSON, unknown version, missing fields, wrong types), `read_prior_iteration_state()` returns `None` + emits a debug log. The reviewer treats this as a first-review case.

This is the safest fallback: nothing worse than a normal (non-IAR) review can happen when state is missing or malformed.

### 13.3 Schema evolution

Future schema changes will:
1. Increment `IAR_STATE_SCHEMA_VERSION` in `scripts/reviewer.py`.
2. Add explicit backward-read logic to `_parse_state_from_marker_body` (e.g. `if data["version"] == 1: hydrate_v1(); elif data["version"] == 2: hydrate_v2()`).
3. Document the migration in a new `## 13.4 Schema v2` section here.

No schema version is ever removed. Marker state written by prior versions will always be readable by future runtimes.

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

- **v1 (2026-07-16):** initial spec authored during Task 1 of `PLAN_iteration_aware_review`.
