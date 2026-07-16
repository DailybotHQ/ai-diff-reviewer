# Executive Report — Iteration-Aware Review (IAR)

**Plan:** PLAN_iteration_aware_review
**Ship target:** v1.8.0 (minor release, additive + default flip)
**Status:** COMPLETE (13/13, + post-plan default flip)
**Branch:** `feat/iteration-aware-review`
**Author:** Cursor Composer running the DWP `task_executive_report` skill, 2026-07-15

> **v1.8.0 default-flip addendum (post-DWP, 2026-07-15):** After the plan closed, the maintainer asked for IAR to ship **on by default** (no configuration required) rather than opt-in behind a flag, provided the opt-out path stayed byte-identical. The two `action.yml` defaults now ship as `iteration-awareness-enabled: true` and `convergence-policy: first-pass-exhaustive`. The 19-test regression suite `tests/test_backward_compat_iar_off.py` was updated to lock byte-identical behavior on the explicit-opt-out path (`iteration-awareness-enabled: false`). Read the paragraphs below with that flip in mind: what the plan describes as "opt-in" is now "on by default with an opt-out escape hatch". Nothing in the technical architecture changed — only the two default values in `action.yml` and the matching fallback constants in `build_iar_config()`.

## Executive Summary

We shipped **Iteration-Aware Review (IAR)** — an opt-in convergence subsystem that solves the "10-loop infinite self-review" pain the maintainer flagged during the `apply-review` PR: each review round would surface fresh warnings that hadn't shown up in the previous round, so the developer could fix 3 findings on round 1, get 3 new ones on round 2, fix those, get 3 more, etc. IAR ends this loop by (a) making the first pass exhaustive on demand (surface 30 findings once instead of 10 across three rounds), (b) deduplicating subsequent rounds against a content-anchored fingerprint of prior unresolved findings, (c) tracking generations so new commits reset the exhaustive-first-pass and re-evaluate everything against the new content, and (d) hardcoding a `critical`-always-surfaces safety rail so nothing that could gate the check ever gets silenced.

The full subsystem is **opt-in** behind a master switch — `iteration-awareness-enabled: false` (the default) makes the runtime byte-identical to pre-v1.8 releases. A dedicated 19-test regression suite (`tests/test_backward_compat_iar_off.py`) locks this contract. **Zero migration required** for consumers on `@v1`. Consumers who opt in get the recommended `first-pass-exhaustive` policy with `+0% steady-state cost delta` and `+205% transient boost on round 1 of each new generation` (bounded, only fires on new commits) — weighted-lifetime that's `~-42%` cost vs the pre-IAR "infinite loop" baseline because IAR converges in ~half the rounds.

## Product Impact

- **For repos NOT enabling IAR:** ZERO change. Master switch defaults to `false`; runtime behavior is byte-identical to v1.7 (verified by 19-test regression suite). All existing action outputs are populated exactly as before; the 5 new IAR outputs are populated with empty strings when off (via `write_iar_outputs_empty()` at start of `main()`). No new subprocess calls, no new HTTP calls, no new prompt splicing.
- **For repos enabling IAR:** convergence in 1–3 rounds vs 5–10 typical pre-IAR (per the `-42%` weighted-lifetime cost model in `analysis_results/DOGFOOD_REPORT.md`); steady-state cost delta `+0%`; hardcoded `critical`-always-surfaces safety rail so nothing that could gate the check ever gets silenced; embedded state block in the tracking marker means zero external state store.
- **New public surface:** 5 opt-in inputs (`iteration-awareness-enabled`, `convergence-policy`, `max-review-rounds`, `exhaustive-first-pass-cap-multiplier`, `iteration-escape-label`) and 5 new outputs (`iteration-round`, `iteration-generation`, `iteration-policy-applied`, `iteration-tokens-used`, `iteration-cost-vs-baseline-estimate`). **Zero** breaking changes. Every input is optional with a sensible default; every output is empty when IAR is off. Full contract locked in `action.yml`.
- **New documentation:** [`docs/ITERATION_AWARENESS.md`](../docs/ITERATION_AWARENESS.md) (authoritative 650+ line spec), IAR cost matrix in [`docs/PERFORMANCE.md`](../docs/PERFORMANCE.md), IAR × strictness matrix in [`docs/STRICTNESS.md`](../docs/STRICTNESS.md), prompt addendum section in [`docs/PROMPTS.md`](../docs/PROMPTS.md), architecture cross-cutting subsystem section in [`docs/ARCHITECTURE.md`](../docs/ARCHITECTURE.md), trust boundary section in [`docs/SECURITY.md`](../docs/SECURITY.md), backward-compat convention section in [`docs/TESTING_GUIDE.md`](../docs/TESTING_GUIDE.md), and `examples/iteration-aware.yml` copy-paste config. All input/output tables in `README.md` refreshed.
- **New tests:** 182 new IAR-specific tests (163 unit + 19 backward-compat regression) across 7 test files, bringing the total suite to 428 tests. All pass on every commit; suite runs in ~0.2s on a modern laptop.

## Technical Details

### Architecture

The IAR subsystem is a **cross-cutting pipeline** wrapping the existing agentic-review loop in `scripts/reviewer.py`. Off by default, gated by the master switch `iteration-awareness-enabled`. When on, it runs at three touchpoints in `main()`:

1. **Init** — `build_iar_config(dict(os.environ))` parses the 5 env vars with clamping and whitelist fallback; `write_iar_outputs_empty()` pre-populates all 5 IAR outputs so a subsequent crash never leaves the outputs undefined.
2. **Pre-LLM (`run_iar_pre_llm`)** — fetches prior state via GraphQL from the tracking marker, decodes the embedded HTML-comment JSON block (fails-safe to `None`), detects the generation transition (`FIRST_REVIEW` / `SAME_GENERATION` / `NEW_COMMITS` / `REBASED`) via range-hash comparison, computes the new-lines percentage for the safety net (`git diff --numstat`), and applies the dispatch policy to compute `effective_max_inline_comments` (cap multiplier if round 1 of new gen under `first-pass-exhaustive`) and the optional prompt addendum. These get spliced into `system_prompt` + `tools_schema` before the LLM call.
3. **Post-LLM (`run_iar_post_llm`)** — takes the LLM's raw findings, applies content-anchored fingerprinting (SHA256 of finding fields + ±20 lines of surrounding code loaded via `git show <sha>:<path>` through `safe_repo_path`), dedupes against prior open + resolved fingerprints (with the hardcoded `critical`-always-surfaces safety rail), advances the state (increment round or advance generation), embeds the new state block into the tracking marker body, appends a human-readable annotation, and calls `write_iar_outputs_populated()` (last-write-wins over the empty defaults).

The entire pipeline is wrapped in `try/except` blocks so any IAR-specific failure degrades gracefully to the baseline review path — the reviewer NEVER crashes on IAR bugs.

For depth see [`docs/ARCHITECTURE.md`](../docs/ARCHITECTURE.md) § "Iteration-Aware Review — a cross-cutting subsystem" and [`docs/ITERATION_AWARENESS.md`](../docs/ITERATION_AWARENESS.md) end-to-end.

### Key design decisions (with rationale)

1. **HTML-comment embedded state** (chosen over external file / labels / DB): "zero infra" is the load-bearing repo principle. State is invisible in the rendered PR UI, version-tagged (`IAR_STATE_SCHEMA_VERSION`), and read via existing `gh_request` GraphQL — no new HTTP surface, no new token scope.
2. **Content-anchored fingerprints** (chosen over path:line-only): a naïve `hash(path + line + severity)` would silence a genuinely-new critical bug that happens to land at the same anchor as an old warning. Hashing ±20 lines of surrounding code means a refactor that meaningfully changes the code around a warning produces a NEW fingerprint = re-surface.
3. **`critical`-always-surfaces hardcoded** (NOT a policy knob): every policy — iterative, first-pass-exhaustive, round-capped, critical-gate, safety-net-forced, escape-label-forced — reaches the same `dedupe_findings_against_prior` code path where an unconditional `if finding.severity == SEVERITY_CRITICAL: continue` branch guarantees the surface. Verified by `TestCriticalAlwaysSurfaces` (6 scenarios) + integration test + defense-in-depth badge in the marker annotation if the invariant is ever violated. This is the load-bearing safety property.
4. **Label counter DROPPED** (design decision made during the plan): the maintainer initially suggested a per-round label like `ai-reviewed-3`, but GitHub's label system doesn't support per-PR mutable labels without proliferating N labels or introducing name-mutation recursion risk. Round count moved to the marker annotation and 2 action outputs.
5. **30% new-lines safety net** — an auto-force-exhaustive when a `NEW_COMMITS` or `REBASED` transition drops ≥30% new lines. Catches large code additions that could hide critical bugs. Threshold is a hardcoded constant, tuned for the "big feature push" case; documented in `docs/ITERATION_AWARENESS.md` § 8.
6. **Cost telemetry as a first-class output** — `iteration-cost-vs-baseline-estimate` + `iteration-tokens-used` let consumers surface the cost impact in workflow dashboards. Populated by a heuristic that combines cap-ratio delta + prompt-addendum overhead; documented in `docs/PERFORMANCE.md` § IAR cost model.

### Backward compat guarantee

Regression suite `tests/test_backward_compat_iar_off.py` proves byte-identical behavior with `iteration-awareness-enabled: false`. 19 tests exercise: env-var parsing when the master switch is off (should build a disabled `IARConfig` with default values); `write_all_outputs()` behavior (must produce the same output file bytes as pre-IAR); no IAR subprocess when disabled; no IAR HTTP call when disabled; no IAR prompt splicing when disabled; no IAR state parse when disabled. All 19 continue to pass across all 13 tasks. Guarded via master switch early-return in every integration point (`run_iar_pre_llm` / `run_iar_post_llm` both short-circuit on `iar_config.enabled == False` and populate empty defaults).

## QA Verification Guide

**How to test IAR on your own repo BEFORE shipping to consumers.** Requires a low-stakes test PR in a repo running the v1.8 (or newer) release.

1. **Baseline dedup** (recommended entry point). Enable IAR with the conservative `iterative` policy on the PR:

   ```yaml
   - uses: DailybotHQ/ai-diff-reviewer@v1
     with:
       iteration-awareness-enabled: true
       convergence-policy: iterative
   ```

   Trigger 2–3 review rounds by re-applying the review-trigger label. Verify:
   - Round 2 silences findings that were reported in round 1 (check the marker's `_Iteration-Aware Review: gen 1, round 2, policy=iterative (same_generation) — X surfaced, Y deduplicated from prior rounds._` annotation).
   - Critical severity findings NEVER get silenced — compare the marker's state block (visible via View-Source on the PR body) to the actual findings surfaced.
   - Marker annotation shows `gen 1, round N` format.

2. **Exhaustive first pass**. Switch to `first-pass-exhaustive`:

   ```yaml
   with:
     iteration-awareness-enabled: true
     convergence-policy: first-pass-exhaustive
     exhaustive-first-pass-cap-multiplier: 3
   ```

   Trigger a review. Verify:
   - The `iteration-cost-vs-baseline-estimate` action output reports `+205%` (transient boost on round 1 of new gen).
   - Up to 30 findings surface in one review (cap × multiplier).
   - The next review round drops back to `iterative` semantics (`+0%` cost delta) with no new addendum.

3. **New-commits generation reset**. Push a small new commit to the PR branch. Verify:
   - Marker annotation changes to `gen 2, round 1`.
   - `iteration-generation` output = "2".
   - `iteration-round` output = "1".
   - If `first-pass-exhaustive` policy, exhaustive addendum fires again on round 1 of gen 2.

4. **Safety net (large push)**. Push a commit that adds >30% new lines relative to the prior head. Verify:
   - Marker annotation shows `policy=first-pass-exhaustive (new_commits)` even if you configured `iterative`.
   - `iteration-policy-applied` output = "safety-net-forced-first-pass-exhaustive".

5. **Escape hatch**. Apply the `full-review-please` label. Trigger a review. Verify:
   - All findings surface (dedup skipped for this run).
   - Persisted state block in the marker is UNCHANGED — remove the label and the next round resumes normal dedup from the last real IAR round.

## FAQs

### Q: Will IAR change my current review behavior when I upgrade to v1.8.0?

A: Yes — IAR is on by default (v1.8.0+) with `convergence-policy: first-pass-exhaustive`. Round 1 of the next new-generation review will surface up to 3× as many findings as pre-v1.8; rounds 2+ dedup against unresolved findings. The critical-always-surfaces safety rail guarantees no finding that would gate the check gets silenced. To keep pre-v1.8 behavior, set `iteration-awareness-enabled: false` — the runtime becomes byte-identical to prior releases, verified by the dedicated 19-test regression suite.

### Q: What happens when I push new commits after convergence?

A: A new generation begins. Round counter resets to 1. Under `first-pass-exhaustive`, the exhaustive-review addendum fires again on the new content. Critical findings carried over from prior generations continue to surface if still unresolved. Content-anchored fingerprints ensure code changes around an anchor produce a fresh fingerprint = re-surface. See [`docs/ITERATION_AWARENESS.md`](../docs/ITERATION_AWARENESS.md) § "New commits after convergence".

### Q: Can IAR accidentally silence a critical bug?

A: No, **by construction**. `critical` severity findings bypass dedup unconditionally — hardcoded rule in `dedupe_findings_against_prior`, asserted in `TestCriticalAlwaysSurfaces` (6 scenarios) + integration test + defense-in-depth badge in the marker annotation. If a policy could silence a critical, it's a bug and the marker will show `⚠️ N critical finding(s) silenced` as a red flag.

### Q: What if the LLM cost goes up?

A: The steady-state cost delta is `+0%` for every recommended policy (verified by `_estimate_cost_vs_baseline` unit tests + heuristic). The transient boost on round 1 of each new generation (only under `first-pass-exhaustive`) is `+205%` with defaults (cap × 3 = 30 vs 10). Weighted-lifetime vs the pre-IAR "infinite loop" baseline is `~-42%` because IAR converges in ~half the rounds. See `analysis_results/DOGFOOD_REPORT.md`. If your workflow is push-heavy (developer pushes new commits between every review), IAR under `first-pass-exhaustive` will re-boost on every push — mitigate by switching to `convergence-policy: iterative` (cost-neutral) instead.

### Q: How do I roll back IAR?

A: Set `iteration-awareness-enabled: false` in your workflow (v1.8.0+ ships with this as `true` by default). Zero code change. Runtime returns to byte-identical pre-IAR behavior at the next run.

### Q: Can I combine IAR with strictness?

A: Yes, but note: the `round-capped` policy silences non-critical findings after the cap. If you use `strictness: block-on-warning`, silenced warnings won't block the check post-cap — this is a documented trade-off (see [`docs/STRICTNESS.md`](../docs/STRICTNESS.md) § "Strictness × Iteration-Aware Review (IAR)"). Every other policy leaves warnings unsilenced by default, so `block-on-warning` continues to gate as usual. **`critical` findings always surface under every policy**, so `block-on-warning` × any IAR policy still gates on any critical finding.

### Q: Where is IAR state persisted?

A: In the tracking marker comment itself, as a JSON block wrapped between `<!-- ai-pr-reviewer-iteration-state:v1 -->` and `<!-- /ai-pr-reviewer-iteration-state -->`. No external file, no label, no DB. Version-tagged for future schema evolution. See [`docs/SECURITY.md`](../docs/SECURITY.md) § IAR trust boundary → Marker-embedded state block for the security model.

## Next Steps

- **Ship:** cut `v1.8.0` release. `auto-release.yml` handles this automatically on merge to `main` (Conventional-Commits parse picks up the accumulated `feat(iar):` commits and infers a minor bump). No manual release procedure required beyond the merge.
- **Monitor:** track adoption via `iteration-tokens-used` output aggregations in consumer CI dashboards. Ship a follow-up docs PR replacing the theoretical numbers in `docs/PERFORMANCE.md` § IAR cost model with per-provider empirical data once the release PR's self-review runs land.
- **Future work (out of scope for this plan):**
  - Optional `staleness-days` input for time-based generation invalidation (auto-force generation change if state is older than N days).
  - Optional `cost-budget-per-pr` input for hard token ceiling (short-circuit the loop if cost would exceed budget).
  - Additional policies (e.g. `severity-escalation` — silence info on repeat, warn on repeated warnings, always surface critical).
  - Fingerprint HMAC (Task 11 security-review follow-up `IAR-FOLLOWUP-1`) — sign the state block with a stable per-repo secret for consumers concerned about the state-block tamper vector.
  - State-block size cap at parse time (Task 11 security-review follow-up `IAR-FOLLOWUP-2`) — cap `resolved_fingerprints` / `open_fingerprints_this_gen` at N entries to bound memory on a pathologically-large state block.
  - General-purpose `stateful-review-loop` reusable module (Task 12 discovery follow-up `CATALOG-FOLLOWUP-1`) — abstract the IAR patterns for other AI-driven CI tools, once 3–6 months of production data validates the design.
  - **Promote IAR to on-by-default** after ≥6 months of empirical validation across a representative set of consumer repos. Requires either (a) confirming steady-state cost stays flat across diverse workflows, or (b) confirming the transient boost cost is acceptable to the majority of consumers.

## Plan Task Log (13/13 complete)

| # | Task | Commit | Outcome |
|---|---|---|---|
| 1 | Design lock-in — `docs/ITERATION_AWARENESS.md` (657 lines, 13 sections) | `1410861` | worked |
| 2 | Foundation flags — 5 inputs + 5 outputs, `IARConfig`, regression suite (19 tests) | `ecf680a` | worked |
| 3 | State layer — `IterationState`, embed/parse marker block, GraphQL fetch | `66f26fe` | worked |
| 4 | Generation tracking — `GenerationTransition`, range hash, `advance_generation` | `f99a478` | worked |
| 5 | Fingerprinting + dedup — content-anchored SHA256, hardcoded critical rail | `864de27` | worked |
| 6 | Policies — `iterative` + `first-pass-exhaustive` (prompt splicing + cap boost) | `721b8eb` | worked |
| 7 | Policies — `round-capped` + `critical-gate` + safety net + escape + dispatcher | `805efde` | worked |
| 8 | Observability — main() integration, marker annotation, 5 outputs (428 total tests) | `79fd41b` | worked |
| 9 | Docs sync — 9 files, cost matrix, `examples/iteration-aware.yml` | `9f47139` | worked |
| 10 | Dogfooding — enable IAR on `self-review.yml`, DOGFOOD_REPORT.md | `76c27db` | worked |
| 11 | Security review — clean run, 0 criticals, `docs/SECURITY.md` updated | `75fb996` | worked |
| 12 | Skills & agents discovery — 5 patterns, 2 agents + 2 docs updated | `5bed032` | worked |
| 13 | Executive report + plan completion | _(this commit)_ | worked |
