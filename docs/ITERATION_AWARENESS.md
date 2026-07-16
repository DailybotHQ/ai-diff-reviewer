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

1. **Structural try/except at every call site.** Both `run_iar_pre_llm()` and `run_iar_post_llm()` are wrapped in `try/except BaseException` in `main()`. On failure the caller logs the exception, leaves the safety-net empty outputs in place, and continues to the baseline review path.
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
| `iteration-policy-applied` | string | Which policy actually fired this run (usually matches `convergence-policy`, unless overridden by the 30% safety net). Empty string if the IAR pipeline crashed. |
| `iteration-tokens-used` | integer string | Sum of input+output LLM tokens for this run. Cost telemetry. Empty string if the IAR pipeline crashed. |
| `iteration-cost-vs-baseline-estimate` | string | Heuristic estimate vs a projected no-dedup baseline (e.g. `"-30%"`, `"+15%"`, `"unknown"`). Empty string if the IAR pipeline crashed. |

### 3.3 Environment variable mapping

Each input is forwarded to `scripts/reviewer.py` via the existing `AIPRR_*` convention:

| Input | Env var |
|---|---|
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

After the four-way classification above, one **override** may fire: if ALL FOUR of the following are true, the transition is upgraded to `USER_FORCED_RESET` and `prior_state` is discarded before any downstream logic runs:

1. The consumer's `applied-label` (the "reviewed" label the action stamps on every successful review) is non-empty.
2. That label is absent from the PR's current labels at trigger time.
3. `prior_state is not None` (i.e., a previous review exists in the marker chain).
4. `prior_state.reviewed_label_applied == True` (i.e., the reviewer previously succeeded at stamping the reviewed label — see § 8.5 for why this fourth condition is load-bearing).

Behaviorally identical to `FIRST_REVIEW` (fresh state, no dedup memory, round-1 exhaustive under the default policy) — the separate enum value only exists so the log and marker annotation can tell developers the reset was a deliberate gesture. The fourth condition prevents a blocked run (whose review posted but whose reviewed-label stamp was suppressed by the strictness gate) from being misread as a deliberate reset on the natural re-trigger that follows. Full spec in § 8.5.

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

### 7.3 State persistence across the `collapse-previous` boundary

**The interaction.** `collapse-previous: true` (the shipped default) minimizes the reviewer's previous tracking marker on the next run so the PR conversation stays scannable. But that marker is exactly where IAR embeds its state block — so a naive implementation that only reads *visible* markers would see no prior state on every run and treat every review as `first_review`, which would defeat dedup + generation tracking entirely (every consumer on defaults would burn round-1 exhaustive on every run and never converge).

**The rescue.** `_fetch_latest_marker_body` uses a three-tier ordering to find the latest marker that carries state, in priority order:

1. **Newest non-minimized marker WITH an `IAR_STATE_TAG_OPEN` block** — the common path when `collapse-previous: false` or the current run is the first review since collapse.
2. **Newest minimized marker WITH state block** — the collapse-boundary rescue. Under `collapse-previous: true` the last real tracking comment has been minimized, but the state block is still in its body. Reading it here is what makes IAR persistence work on defaults.
3. **Newest marker of any shape** — back-compat fallback for callers who just want "the last marker we posted"; `_parse_state_from_marker_body` returns `None` if the body carries no state block, which downstream treats as a first review.

Tiers 1 and 2 combined guarantee that no consumer setting of `collapse-previous` causes IAR to lose state. Tier 3 keeps the fetcher useful for other callers (currently none in-tree, but the API is public).

**Log signature.** When tier 2 fires, the reviewer emits a `INFO`-level log line so the collapse-vs-IAR interaction is visible in the workflow log without turning on debug: `IAR: no visible marker carries state; falling back to the latest minimized marker with an embedded state block (this is expected under collapse-previous: true — the prior tracking comment was minimized between runs).`

**Test coverage:** [`tests/test_iar_state_layer.py`](../tests/test_iar_state_layer.py) `FetchLatestMarkerTests.test_prefers_visible_marker_with_state_over_minimized_with_state`, `test_falls_back_to_minimized_marker_with_state_when_no_visible`, `test_falls_back_to_any_marker_when_none_carry_state` lock the three-tier ordering.

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

**How the two are distinguished in the marker.** The annotation carries the transition name in parentheses — `(escape_label)` for one-shot bypass (state preserved) vs `(user_forced_reset)` for the reset (state discarded).

**The load-bearing `reviewed_label_applied` safety guard.** The reset detection requires four conditions to fire — (1) `applied-label` is configured, (2) prior IAR state exists, (3) that prior state records the reviewer had previously stamped the label (`prior_state.reviewed_label_applied == True`), and (4) the label is absent from the PR now. Condition (3) is critical: without it, any blocked review (`block-on-critical` fired, so the reviewer never stamped the label) followed by the natural re-trigger would look identical to a deliberate reset and wipe fingerprint memory.

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
- **Cost telemetry is free (zero LLM tokens).** The `iteration-tokens-used` and `iteration-cost-vs-baseline-estimate` outputs are populated from response metadata and local computations. They add no LLM cost.

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

**Configuration:** `convergence-policy: first-pass-exhaustive`, `max-review-rounds: 5`, `cap-multiplier: 3`.

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
      "tokens_used": 24580,
      "wall_clock_ms": 128000
    }
  ],
  "base_sha": "deadbeef00112233"
}
-->
```

### 12.1 Field definitions

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
| `head_sha` | string | The PR head SHA at the end of the run that wrote this state. Used by `compute_new_lines_pct` to measure how much has been added since — the input to the 30% new-lines safety net (§ 7.2). Optional in schema v1 — an empty string means "unknown prior head" and the safety net degrades to a no-op (never silences a review that would have benefited from an exhaustive pass; just skips the boost). |
| `reviewed_label_applied` | bool | Whether the reviewer successfully stamped `applied-label` at the end of the run that wrote this state. `True` only when the review posted AND the strictness gate did not block. Load-bearing signal for the USER_FORCED_RESET gesture (§ 8.5) — the reset only fires when this bit was `True` in the prior state AND the label is absent from the PR now, which is the difference between "developer removed the label deliberately" and "reviewer never stamped it because the run was blocked". Optional in schema v1 — defaults to `False`, which is the safe side (suppresses the gesture until the reviewer completes one successful run). |

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

### 13.1 Agent-runner overflow findings are not fingerprinted

The tool-call cap enforcement in the agent-runner code path (Claude Code / Cursor / Codex integrations) truncates any surplus findings the CLI emits above `effective-max-inline-comments` — after the criticals-first sort, so the safety rail still holds — BEFORE `run_iar_post_llm` fingerprints them. This means overflow findings do not enter `open_fingerprints_this_gen`, so if the next round's LLM re-emits the same findings the dedup engine cannot suppress them and they re-surface.

**Failure mode:** an agent-runner provider emits 40 findings in round 1 (effective cap 30, so 10 dropped after criticals-first sort). Round 2 re-emits the same 40; the 30 previously surfaced are correctly deduped, but the 10 that were dropped surface as "new" findings — the very "infinite loop" symptom IAR is designed to prevent, scoped to the overflow tail.

**Scope:** only bites when the agent-runner CLI overshoots the cap (typical LLM output stays under 30 findings, so the failure mode is a tail-risk edge case). Does not affect the chat-completions Provider path (Anthropic / OpenAI / Gemini) which fingerprints the full result set before the pipeline caps it.

**Follow-up:** the clean fix is either (a) plumb `code_contexts` to the agent-runner truncation site so `finding_fingerprint` can produce merge-compatible hashes, or (b) move the cap enforcement into `run_iar_post_llm` so the pipeline is single-path. Either resolves the semantic mismatch of `code_context=None` fingerprints from the truncation site vs `code_context=<real>` fingerprints from the post-LLM stage.

### 13.2 Fingerprint body slice is a magic constant

`finding.body[:200]` in `finding_fingerprint` (`scripts/reviewer.py`) is inlined as a magic number rather than promoted to a module-level `IAR_FINGERPRINT_BODY_CHARS: int = 200` next to `IAR_CONTEXT_HASH_RADIUS`. The value is stable and documented in this file (§ 5.2), but a single named constant would let tests and docs reference one source of truth. Cosmetic; no behavioral impact.

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

- **v1 (2026-07-16):** initial spec authored during Task 1 of `PLAN_iteration_aware_review`. Post-launch corrections in the same day (three-dot generation range hash, four-condition USER_FORCED_RESET guard with three-signal `reviewed_label_applied` write logic, § 13 known-limitations catalogue) folded in during self-review dogfooding.
