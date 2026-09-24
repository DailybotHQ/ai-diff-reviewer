# RFC-06 — Risk-tiered budgets and true incremental review

## Status

Accepted — 2026-09-23 (D-00, developer instruction to PLAN_v3_implementation). Originally a discovery record (PLAN_v3_discovery, Task 8, 2026-09-23). Addresses
RFC-00 **P-5**, **P-6**, part of **P-2** (budget as an input to the loop).
Depends on RFC-02 (change inventory, control-loop budget), RFC-03 (verifier
allowance), RFC-01 (how targets are measured), Task 2 metrics. Consumed by
RFC-07 (default `model` alias, new inputs, `complexity-labels-enabled`
semantics), RFC-08 (cost targets per phase).

## Problem

The review spends the same budget whatever the change is. `DEFAULT_MAX_TURNS`
(30), `ANTHROPIC_MAX_TOKENS` (8 192 per turn), `MAX_CONVERSATION_TURNS_RETAINED`
(12) and the model tier are constants per run; the one budget knob IAR turns
is the inline-comment cap (`IAR_DEFAULT_CAP_MULTIPLIER` = 3 on round 1),
never turns or tier (`docs/ITERATION_AWARENESS.md` § 9.1). In incremental
mode the **prompt** shrinks — `render_incremental_sections` sends the delta
hunks and one-liners for unchanged files — but the **budget** does not:
the anthropic in-process leg spent ≈ 12 turns and ≈ 1.15 M tokens on every
PR #57 round regardless of delta size (RFC-00 R-04), and two in-process runs
burned all 30 turns producing nothing (E-36). Complexity is assessed by the
model **after** the spend (`set_pr_complexity`, `tool_set_pr_complexity`,
"call this AT MOST ONCE, near the end of the review"). The obvious lever —
a cheaper model everywhere — is refuted: grok-4.3 reviewed 0/5 at one third
of the cost (E-05, E-11); cheap models that do not do the work are waste,
not savings.

## Evidence

| RFC-00 row / source | What it establishes for this RFC |
|---|---|
| R-04 | ≈ 12 turns / ≈ 1.15 M tokens per incremental round, constant across delta sizes (recorded) |
| E-36 | In-process runs at the 30-turn cap with 0 findings: $2.04 and $1.85 |
| E-26, E-27 | `MAX_DIFF_CHARS` re-billed per turn; `DEFAULT_MAX_TURNS` = 30 |
| E-05, E-11, H-06 | grok-4.3 0/5 at $0.16/run; grok-4.5 3–4/5 at $0.47–0.52 — the economy class does not review |
| E-01, E-14a | grok-4.5 CLI baseline ≈ $0.47–0.52 mean per PR (n = 14 incl. repetitions), turns ≈ 14, provider ≈ 228 s; spread median 0.266 |
| Task 2 cells | pr39 (2 974-line PR): $1.07, 27 turns, 401 s vs pr15 (small): $0.32, 7.5 turns — cost already tracks size ≈ 3× without any policy |
| E-08 | GLM lane ≈ $1.63 list / ≈ 0 marginal on a flat plan, 23 turns, 391 s |
| S6 §8 | Risky classes: lockfiles (omitted but still a dependency change), binaries, executable-bit, policy/instruction files; PR metadata untrusted |
| IAR § 9.2 | Theoretical lifetime savings from dedup (−34 % to −60 %) are on **posting**, not on LLM spend: "the LLM produces the same set of findings" |

## Design

Two changes to *when* budget is decided and *what* it scales with:

1. **Classify before spending.** The RFC-02 change inventory is classified
   deterministically into a **risk tier** before the first model call; the
   tier selects the budget row (turns, tier alias, output tokens, verifier
   policy, patch-byte budget, recommended legs). The model's post-hoc
   `set_pr_complexity` opinion is kept as telemetry (`run.complexity_claimed`)
   and may still drive the `complexity:*` label when
   `complexity-labels-enabled` is on, but it never changes the budget.
2. **Scale incremental rounds with the delta.** A follow-up round's budget is
   a function of the delta inventory and the outstanding findings, with a
   floor and a ceiling; a "no code changes" round is a verifier pass, not a
   review.

Both are enforced by the RFC-02 control-loop contract (budget is an input;
the loop records `budget.*` in the run record). The economy alias is used
where the work is verification or docs-only, never for code review at
elevated tiers (P-6).

## Risk classification

Deterministic, from inventory facts only. Inputs per file: path, `status`,
`previous_path`, `binary`, `mode_change`, `omitted`, `patch_chars`,
`additions + deletions`. Never: PR title, body, labels, comments, author.

| `risk_class` (RFC-05 field) | Rule (first match) | Examples |
|---|---|---|
| `prompts-policy` | path is an agent-instruction or review-policy file: `AGENTS.md`, `CLAUDE.md`, `.review/**`, `prompts/**`, `.github/ai-diff-reviewer/**`, `**/SKILL.md`, `.cursorrules`, `.agents/**` | the PR #37 class lives here |
| `workflows-ci` | `.github/workflows/**`, `action.yml`, `Dockerfile*`, `*.gitlab-ci.yml`, `Makefile`, `justfile` | permissions and secrets surface |
| `dependencies` | `DEFAULT_IGNORE_PATH_GLOBS` lockfile names, plus manifests: `package.json`, `pyproject.toml`, `requirements*.txt`, `go.mod`, `Cargo.toml`, `Gemfile`, `composer.json`, `*.csproj` | an omitted lockfile is still a dependency change (S6 §8) |
| `generated` | the non-lockfile `DEFAULT_IGNORE_PATH_GLOBS` (minified, maps, vendored, snapshots) and `binary = true` | never reviewed line by line |
| `tests` | `tests/**`, `test/**`, `**/*_test.*`, `**/*.test.*`, `**/*.spec.*`, `__tests__/**`, `spec/**` | — |
| `docs` | `*.md`, `*.rst`, `*.txt`, `docs/**` **except** anything already matched by `prompts-policy` | "documentation extensions alone are never a safe-class label: executable examples, prompts and policy files are counterexamples" (S7) |
| `code` | everything else | — |
| `unknown` | path unresolvable or inventory incomplete for that file | escalates |

Tier = the **maximum** over files, with size and completeness modifiers:

| `risk_tier` | Condition |
|---|---|
| `low` | only `docs` / `tests` / `generated` files; `complete = true`; total `additions + deletions` ≤ 300 |
| `standard` | any `code` file; no `prompts-policy`, `workflows-ci`, `dependencies` or `unknown`; `complete = true` |
| `elevated` | any `prompts-policy`, `workflows-ci` or `dependencies` file; **or** any `mode_change`; **or** `complete = false`; **or** total changed lines > 1 500 |
| `critical` | a `prompts-policy` or `workflows-ci` file **together with** `code`; **or** any `unknown`; **or** paths matching the consumer's `high-risk-paths` input (new, optional glob list — e.g. `auth/**`, `**/migrations/**`) |
| `unclassified` | classification failed (bug) → treated as `elevated` and logged |

The tier is written to `change_inventory.risk_tier` (RFC-05) and
`budget.risk_tier` (RFC-01). Consumers may **raise** the tier with
`high-risk-paths`; nothing lowers it. Because the inventory is computed from
git and the files API, a description that says "docs only" cannot lower the
tier (untrusted metadata, RFC-02).

## Budget matrix

Defaults for a first (full) review round. Costs are per-PR estimates from
Task 2 cells (grok-4.5 CLI, n = 14; the strongest measured lane) and the
recorded in-process profile; they are inputs to RFC-01, not promises. Rows
that **raise** a current default carry their estimate (AGENTS.md DON'T #9).

| Tier | Max turns | Model alias (review) | Output tokens / turn | Verifier (RFC-03) | Patch bytes in first message | Legs recommended | Est. cost vs today (grok-4.5 CLI ≈ $0.52) |
|---|---|---|---|---|---|---|---|
| `low` | 8 | `balanced` (**never** `economy` for review — P-6) | 4 096 | criticals only | 60 k | 1 | ≈ $0.25–0.35 (pr15-class runs used 7.5 turns at $0.32) |
| `standard` | 20 | `balanced` | 8 192 (`ANTHROPIC_MAX_TOKENS`, unchanged) | criticals + 30 % of warnings | 120 k | 1–2 | ≈ $0.45–0.60 (today's mean; 14 turns avg) |
| `elevated` | 30 (`DEFAULT_MAX_TURNS`, unchanged) | `balanced` | 8 192 | all criticals + all warnings | 200 k | 2–3 | ≈ $0.60–1.10 (pr39-class: $1.07 at 27 turns) + verifier ≈ $0.05–0.15 |
| `critical` | 40 (**raised**: +10 turns ≈ +$0.15–0.35 at grok-4.5 rates, only on this tier) | `deep` where the kind has one (`_XAI_TIERS`: grok-4.6; `_ANTHROPIC_TIERS`: opus) **or** `balanced` + a second leg | 8 192 | all + `read_base_version` on every critical | 200 k + on-demand `get_patch` | 3 | ≈ $1.20–2.50; `deep` is 2–4× slower (E-12: grok-4.6 4–10 min) — the tier is rare by construction |

Verifier model alias: `economy` of the same kind where it exists (H-06),
else `balanced`; the verifier's own turn cap is 4 per finding and its
tokens are counted separately (`timings.verifier_seconds`,
`budget.verifier_runs`). Economy models are **never** selected for the
review itself at any tier: E-05/E-11 show they do not review, and a tier
that saved money by finding nothing would fail RFC-01's recall blocker.

The `model` input keeps overriding the alias for consumers who pin a model;
`max-turns` keeps overriding turns (as a ceiling — a tier never exceeds an
explicit `max-turns`). New optional inputs: `budget-profile`
(`auto` default / `fixed` = today's constants for every tier) and
`high-risk-paths`.

## Incremental rounds

Round *r* > 1 of a generation receives (RFC-02): the delta inventory, the
patches of changed files up to the tier's patch budget, the outstanding
findings (RFC-03 states) and the instruction files. Its budget:

```
turns(r)  = clamp( floor + k_files × |Δfiles| + k_open × |outstanding| , floor, ceiling(tier) )
floor     = 4        (inventory, rules, one look at each prior finding, submit)
k_files   = 1.5      turns per changed file in the delta (capped by the tier ceiling)
k_open    = 1        turn per outstanding prior finding (the verifier re-reads anchors separately)
```

| Delta | Outstanding | Budget | Today (R-04 profile) |
|---|---|---|---|
| 0 files ("no code changes since your last review") | 3 | **verifier pass only** — RFC-03 re-reads the 3 anchors (≈ 3 × 4 verifier turns at economy rates ≈ $0.05–0.10); no review turns | ≈ 12 turns / ≈ 1.15 M tokens |
| 1–2 files, small hunks | 3 | 4 + 3 + 3 = **10 turns**, ≤ 120 k patch bytes | same ≈ 12 / 1.15 M |
| 6 files | 5 | 4 + 9 + 5 = 18 → capped at the tier ceiling (20 `standard`) | same |
| new generation (rebase / large push) | any | full-round matrix row again | full round |

Expected profile for the PR #57 rounds (typical delta 1–3 files, 2–5
outstanding): **≈ 8–12 review turns with a first message of ≤ 120 k patch
bytes instead of the full re-embedded context**, plus the verifier. Tokens
fall mainly through the first-message size and the pruning window (the
1.15 M figure is dominated by re-billed context, E-26), not through the turn
count alone — RFC-01 measures `usage.input_tokens` per round on the
multi-round fixtures. Safety rails unchanged: `IAR_SAFETY_NET_NEW_LINES_PCT`
(30 % new lines → exhaustive first pass) and the escape label still force a
full round; a full round is always a **full-round budget**.

## Measurable targets

Each target names its RFC-01 metric, the baseline row, the target and the
n needed to see it above the noise floor (median relative cost spread
0.268 as re-measured in Phase 0 → a paired difference must exceed ≈ 27 %
with n ≥ 3 replications per cell to be promotable).

| Target | Metric (RFC-01) | Baseline | Target | n |
|---|---|---|---|---|
| Incremental-round cost | `cost_usd` and `usage.input_tokens` per follow-up round on the multi-round IAR fixtures | R-04: ≈ 1.15 M tokens / round (recorded; re-measured in Phase 0) | **−50 % tokens** on 1–3-file deltas (well above the 27 % floor) | ≥ 4 fixtures × 3 reps × 2 rounds |
| No-change round | `budget.turns_used` | ≈ 12 turns | **0 review turns** (verifier only) | 3 reps |
| Zero-finding cap exhaustion | share of runs with `turns_used = max_turns` and `findings_total = 0` | E-36: 2 of 6 anthropic baseline runs | **0** after RFC-02's status contract (such a run becomes `incomplete`, never a silent approve) | corpus |
| Low-tier cost | `cost_usd` on docs/tests-only corpus cases | ≈ $0.32 (pr15-class) | ≤ $0.30 with recall unchanged on those cases | ≥ 6 cases × 3 reps |
| Recall guard | must-find recall per tier | Phase 0 baseline | **no drop beyond the RFC-01 blocking rule** (−2 defects) at any tier — a budget that loses recall is rejected | corpus |
| Critical-tier precision | RFC-01 precision on the critical corpus at `critical` tier | Phase 1 baseline | ≥ baseline (the extra budget must buy precision, not findings) | ≥ 20 adjudicated |

## Alternatives considered

| Alternative | Why not |
|---|---|
| Flat budgets with a cheaper default model | E-05/E-11: economy models do not review; the saving is fictitious |
| Model-decided budgets (today's post-hoc `set_pr_complexity`) | The spend has already happened when the opinion arrives; the model's assessment is also an untrusted output |
| Per-consumer manual tuning only (`max-turns`, `model`) | Consumers tune once for their largest PR and pay it on every docs change; the inventory already knows the difference |
| Skip review on `low` tier (fast-pass) | The Jev campaign's fast-pass was rejected (S2: `reject_for_now`); RFC-01's corpus has docs-only cases with real defects (C-class `docs` change_class with warning labels) — `low` gets a smaller budget, never zero |
| Lower the tier from PR metadata ("docs only" in the title) | Untrusted input (RFC-02, S6 §8) |

## Impact on the public contract

- **Behavioral (breaking for cost/turn expectations):** turns and output
  tokens vary by tier; the recorded per-review cost profile changes
  (`BC-13`). `complexity-labels-enabled` keeps its label behavior but the
  model's level no longer influences anything else, and the label may be
  derived from `risk_tier` instead when `complexity-source: inventory` is
  set (new optional input; default keeps the model's level) (`BC-14`).
- **Default `model` alias:** `balanced` becomes the documented default
  alias for every lane (RFC-07 `BC-15`, already a v2.5 candidate); the
  tier matrix never selects `economy` for review.
- **Additive inputs:** `budget-profile` (`auto`/`fixed`), `high-risk-paths`,
  `complexity-source`. **Additive outputs/fields:** `risk_tier` in the
  structured output and run record (RFC-05/01).
- **Raised default:** `critical` tier max turns 40 (> `DEFAULT_MAX_TURNS`
  30) with its cost estimate above (DON'T #9 satisfied); every other tier
  is ≤ 30.

## Open questions

| Id | Question | Recommendation |
|---|---|---|
| Q-26 | Line thresholds (300 for `low`, 1 500 for `elevated`) | Start there; calibrate against the corpus `change_class` distribution in Phase 0 |
| Q-27 | Should `dependencies` alone be `elevated` or `standard`? | `elevated`: a lockfile bump can pull a compromised package; the cost is one tier on a rare change class (F6 weight 10 %) |
| Q-28 | `deep` alias at `critical` tier vs `balanced` + second leg | `balanced` + second leg when an ensemble (RFC-04) is configured; `deep` only for single-leg consumers — decided per RFC-01 measurement of precision per dollar |
| Q-29 | Should `budget-profile: fixed` exist at all? | Yes, for one minor cycle as the escape hatch; retire after RFC-01 shows `auto` non-inferior on recall |
| Q-30 | Verifier turn cap per finding | 4; re-measure with RFC-03 acceptance item 6 |

## Acceptance for the implementation plan

The Phase 3/4 budgets DWP is complete when:

1. `classify_inventory` (new) is pure, typed, and tested on every
   `risk_class` rule and tier modifier, including the untrusted-metadata
   test (a title saying "docs only" does not change the tier of a code PR).
2. The tier is chosen **before** the first model call and recorded in the
   run record; `set_pr_complexity` no longer influences turns, tier or
   tokens (test).
3. The budget matrix is a single table constant at the top of
   `scripts/reviewer.py` (DON'T #11), consumed by both families; `max-turns`
   caps every tier.
4. Incremental rounds: the no-change round runs zero review turns; the
   1–3-file fixture rounds show ≥ 50 % fewer input tokens than the Phase 0
   baseline under RFC-01 replication, with recall unchanged.
5. `economy` is never selected for the review at any tier (test over the
   matrix); the verifier uses it where the kind defines one.
6. `docs/PERFORMANCE.md` carries the matrix and the per-tier estimates;
   `docs/ITERATION_AWARENESS.md` § 9 replaces its theoretical table with the
   measured incremental profile; `README.md` documents `budget-profile`,
   `high-risk-paths`, `complexity-source`; `MIGRATION_v3.md` carries
   `BC-13`–`BC-15`.

## Cited symbols

- `DEFAULT_MAX_TURNS`
- `ANTHROPIC_MAX_TOKENS`
- `MAX_CONVERSATION_TURNS_RETAINED`
- `MAX_DIFF_CHARS`
- `IAR_DEFAULT_CAP_MULTIPLIER`
- `IAR_SAFETY_NET_NEW_LINES_PCT`
- `IAR_MODE_INCREMENTAL`
- `render_incremental_sections`
- `render_prior_findings_block`
- `IAR_INCREMENTAL_DIFF_HEADING`
- `run_iar_pre_llm`
- `effective_max_turns`
- `set_pr_complexity`
- `tool_set_pr_complexity`
- `PR_COMPLEXITY_LEVELS`
- `MODEL_TIER_TABLE`
- `_XAI_TIERS`
- `_ANTHROPIC_TIERS`
- `DEFAULT_IGNORE_PATH_GLOBS`
- `complexity-labels-enabled`
- `max-turns`
