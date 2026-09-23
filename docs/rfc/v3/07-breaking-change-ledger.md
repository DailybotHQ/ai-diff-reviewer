# RFC-07 — v3 breaking-change ledger, SemVer justification and migration outline

## Status

Accepted — 2026-09-23 (D-00, developer instruction to PLAN_v3_implementation). Originally a discovery record (PLAN_v3_discovery, Task 9, 2026-09-23). Collects
every public-contract change proposed by RFC-01…06 plus the candidates
recorded at the v2.5.0 release, classifies each, and outlines
`docs/MIGRATION_v3.md`. Consumed by RFC-08 (release cuts; pending rows
become decision-log entries) and by every implementation plan (each contract
change lands with its `BC-nn` row and its documentation surfaces).

Rules this ledger obeys: AGENTS.md **Rule #4** (inputs/outputs are a public
contract; rename/remove/re-type = major, coordinated across `action.yml`,
the runtime env reads, `README.md`, `CHANGELOG.md`,
`skills/ai-diff-reviewer/setup/reference.md` and one example), **Rule #8**
(SemVer, moving major tag, never delete a tag), **Rule #9** (Marketplace
identity immutable), **DON'T #10** (no one-off inputs), **DON'T #13** (product
name spelling).

## Ledger

Type: **breaking** (a `@v2` consumer sees different behavior or must edit
their workflow), **additive** (opt-in or invisible default), **behavioral**
(same inputs, different output/cost profile — documented, not gated),
**dogfood-only** (this repository's workflow, not the action). Phase = the
RFC-08 phase that lands it. *pending* = a decision-log row (RFC-08 `D-nn`)
decides the option; the recommended option is shown.

| Id | Surface | Current → proposed | Type | Rationale | Migration path for a `@v2` consumer | Docs surfaces | Phase |
|---|---|---|---|---|---|---|---|
| BC-01 | Artifact / file | none → run record `run-record/3.0` written per run (inside the RFC-05 document) | additive | RFC-01; RFC-00 P-7, X-02 | none | ARCHITECTURE, TESTING_GUIDE | 0 |
| BC-02 | Release process | `auto-release.yml` cuts on every merge → cuts only when the latest eval verdict for the candidate is present and non-blocking | behavioral (maintainer-facing) | RFC-01 P-1 | none (release-side) | TESTING_GUIDE, RELEASE_RECOVERY | 0 |
| BC-03 | In-process request shape (`provider: anthropic` / `openai`) | one embedded diff (≤ 200 000 chars) + 5 base tools → inventory + bounded patches + parity tools | **breaking** (ends the "byte-identical to earlier releases" promise for these runners; request bodies, tool schemas and token profile change) | RFC-02 P-2, E-26 | nothing to edit; expect a different cost/turn profile; pin `@v2` to keep the old shape | PROVIDERS, PERFORMANCE, ARCHITECTURE § 8, MIGRATION_v3 | 1 |
| BC-04 | Review status semantics | cap/timeout → proceeds with partial output and can pass → `status: incomplete`/`timeout`, red under blocking strictness, partial findings posted with a note | **breaking** (a review that used to pass may fail) | RFC-02 E-36, R-05 | none required; consumers who relied on silent partial passes should raise `max-turns` or lower tier expectations | STRICTNESS, PROVIDERS (degrade table), MIGRATION_v3 | 1 |
| BC-05 | Prompt / loop | instruction files read at the model's discretion → `read_instruction_files` required step; `contradicts-documented-rule` category | behavioral (more findings of a new class) | RFC-02/03 E-13, H-05 | none; consumers can tune via `.review/extension.md` | PROMPTS, SECURITY (new tool) | 1 |
| BC-06 | `provider: anthropic` / `openai` for code review | kept → **pending D-03**: rebuilt on the unified loop (recommended) or retired for review and kept as verifier/economy engine | breaking if retired | RFC-02 lane decisions; E-09, E-10 | if retired: switch to a CLI runner (`grok`, `claude-code`) or keep `@v2`; Bedrock stays on `anthropic` either way | README runners table, PROVIDERS, MIGRATION_v3 | 1 (decided by measurement) |
| BC-07 | Strictness gate semantics | `block-on-critical` blocks on any `critical` claim → blocks on **verified** criticals only; unverified claims publish as annotated warnings; summary generated from the findings table | **breaking** (gate outcome changes on some PRs) | RFC-03 P-4, E-20, R-03 | none; opt-in `strict-unverified-criticals: true` restores v2 gating for one minor cycle (Q-12) | STRICTNESS, ITERATION_AWARENESS § 7.1, MIGRATION_v3 | 1 |
| BC-08 | Prior-finding retirement | corroborated by file change → also requires anchor re-read confirming the fix | behavioral (fewer auto-retirements) | RFC-03 P-9 | none | ITERATION_AWARENESS, MIGRATION_v3 | 1 |
| BC-09 | Inputs/outputs | none → `mode` (`review`/`emit`/`aggregate`), `expected-legs`, `min-agreement` (default 1), `require-all-legs` (default false); outputs `legs-expected`, `legs-delivered`, `duplicates-removed`, `agreement-histogram`; artifact `ai-diff-reviewer/<sha>/<leg>.json`; IAR state on an aggregate marker for opted-in matrices | additive (default `review` = v2 behavior) | RFC-04 P-3 | matrix consumers opt in by adding `mode: emit` per leg + one `aggregate` job; existing per-leg IAR history is migrated by the first aggregated round (collapse of surviving per-leg artefacts) | README (matrix example), PR_REVIEW_WORKFLOW, SECURITY (write surface per role), examples/ensemble-matrix.yml, setup/reference.md, MIGRATION_v3 | 2 |
| BC-10 | Dogfood labels | `self-reviewed:<provider>` per leg → one `applied-label`; provenance in the structured output | dogfood-only | RFC-04 | n/a (this repo) | PR_REVIEW_WORKFLOW, AGENTS.md § Reading PR Review Comments | 2 |
| BC-11 | Outputs / artifact | none → `structured-output-path`, `structured-output-sha256`; `.aiprr/review-output.json`; artifact upload | additive | RFC-05 P-3, P-7, P-10 | none; downstream steps may start reading the document | README outputs table, ARCHITECTURE, setup/reference.md, apply-review skill | 1–2 |
| BC-12 | Agent-runner input contract | `.aiprr/findings.json` v2 fields → v3 superset (optional `title`, `category`, `evidence.*`) | additive (unknown keys already ignored; legacy files still parse) | RFC-05 | none for CLIs; `write_findings_prompt_directive` documents the optional fields | PROVIDERS (findings-file schema) | 1 |
| BC-13 | Cost / turn profile | constant `max-turns` 30, tier alias per run → tiered by deterministic risk classification (`low` 8 … `critical` 40); incremental rounds scaled to the delta | **breaking** for cost expectations (some PRs cheaper, `critical`-tier PRs up to 40 turns) | RFC-06 P-5, P-6, R-04 | `budget-profile: fixed` restores the constant profile for one minor cycle (Q-29); `max-turns` still caps every tier | PERFORMANCE, README, MIGRATION_v3 | 3–4 |
| BC-14 | `complexity-labels-enabled` | label from the model's `set_pr_complexity` → same by default; `complexity-source: inventory` (new) derives it from `risk_tier`; the model's level never influences budget | additive input; behavioral note (level is telemetry for budgets) | RFC-06 | none | PR_METADATA_CHECKS, README | 3–4 |
| BC-15 | Default `model` | runner-specific pinned ids (`DEFAULT_MODELS`) → documented default **alias `balanced`** resolved per runner × kind (`MODEL_TIER_TABLE`) | **breaking** in principle (the resolved default id may move when the tier table is re-benchmarked) — in practice `balanced` resolves to today's defaults at cut time | v2.5 candidate; RFC-06 | pin `model: <id>` to freeze a specific model | README, PROVIDERS (defaults matrix), setup/reference.md, MIGRATION_v3 | release |
| BC-16 | `api-key` | `required: false` since v2.5.0 (Bedrock OIDC exception) → documented as optional whenever environment credentials exist for the lane (AWS today; future OIDC lanes) | additive (already shipped as an exception; v3 makes it the rule) | v2.5 candidate | none | README inputs table, SECURITY (credential lanes) | release |
| BC-17 | Byte-identical promise (`MIGRATION_v2.md` "An empty `api-base` keeps every existing runner byte-identical") | promise ends for in-process runners (BC-03) and for sampling defaults already changed in v2.4.0 | **breaking** (a documented promise is withdrawn) | RFC-02; v2.4.0 history | pin `@v2` for the old wire shape | MIGRATION_v3 (explicit withdrawal), PROVIDERS | 1 |
| BC-18 | Inputs (RFC-03) | none → `verifier` (`criticals-only` default / `on` / `off`), `verifier-model` (alias, default `economy`), `strict-unverified-criticals` (default false) | additive | RFC-03 Q-12, Q-15 | none | README, setup/reference.md, STRICTNESS | 1 |
| BC-19 | Inputs (RFC-06) | none → `budget-profile` (`auto`/`fixed`), `high-risk-paths` (glob list), `complexity-source` (`model`/`inventory`) | additive | RFC-06 | none | README, setup/reference.md, PERFORMANCE | 3–4 |
| BC-20 | Private env-var prefix `AIPRR_` and internal constants | unchanged | none | Rule #4 note (private contract) | none | CONTRIBUTING, DEVELOPMENT_COMMANDS (local debug) | — |

Items deliberately **not** in the ledger: the product name, description,
branding, repository slug (next section); the `AIPRR_` prefix (BC-20 records
it stays); the stdlib-only rule (Rule #2 is a constraint every RFC obeys).

## Marketplace identity

Unchanged, by Rule #9: `name: 'AI Diff Reviewer'`, the `description`,
`branding.icon: 'check-circle'`, `branding.color: 'purple'`, `author:
DailybotHQ`, the repository slug `DailybotHQ/ai-diff-reviewer` and the
Marketplace slug `ai-diff-reviewer`. **No row in this ledger touches any of
them**, and the plan's Task 9 gate greps this document for any wording that
would alter the listing title or its branding fields. The user-facing spelling stays
"AI Diff Reviewer" (DON'T #13). The `v3` line publishes under the same
listing; only the version and the moving tag change.

## SemVer justification

Rows that **alone** force a major: BC-03 (in-process request shape; the
byte-identical promise), BC-04 (a passing review can start failing), BC-07
(gate outcome changes), BC-13 (cost/turn profile), BC-17 (withdrawal of a
documented promise), and BC-06 if D-03 retires a lane. Any one of them is a
Rule #4 major.

What could have shipped as `v2.x`: BC-01, BC-02, BC-05, BC-08 through BC-12,
BC-14, BC-16, BC-18, BC-19 — all additive or maintainer-facing. Bundling
them into the same major is the better consumer experience: **one migration
document, one moving-tag switch, one CHANGELOG section** — instead of five
minor releases each nudging gate semantics or cost profile, which is how
v2.4.0's sampling change ended up as a "documented wire-format change, not a
migration step" footnote in `MIGRATION_v2.md`. v3 says the quiet part out
loud.

The number also signals the product change honestly: v3 reviews are
verified (BC-07), consolidated (BC-09), budgeted (BC-13) and measurable
(BC-01/02). A `v2.6` carrying those would misdescribe the risk to a
consumer pinned on `@v2`.

## Tag and maintenance policy

- **Moving tags (Rule #8).** `release.yml` force-updates the major alias on
  every `vX.Y.Z` release (`git tag -f "$MAJOR" "$RELEASE_TAG"`). When
  `v3.0.0` is published, `v3` starts moving; **`v2` stops moving** at the last
  `v2.x.y` release. Consumers pinned to `@v2` see **nothing change** — say
  so in the first line of `MIGRATION_v3.md`.
- **`v2` maintenance.** Recommend: security fixes and provider-catalog
  breakages (a vendor renames a model id) for **six months** after `v3.0.0`,
  cut from a `release/v2` branch created at the last v2 release; no features.
  Every `v2.x.y` cut still runs the v2 dogfood. Recorded as RFC-08 decision
  D-nn (owner: maintainer).
- **Never delete a tag.** `v2.0.0` … `v2.5.1` stay resolvable forever.
- **Pre-release.** Recommend one **`v3.0.0-rc.1`** on the `v3` line after
  Phase 2 lands, dogfooded for at least ten PRs on this repository with the
  two configured legs (XAI, ZAI) and one full RFC-01 campaign on the
  candidate `runtime_sha`; `auto-release.yml`'s BC-02 precondition must be
  live before the rc so the rc itself is the first gated release. The
  moving `v3` tag is **not** created for an rc (consumers opt into
  `@v3.0.0-rc.1` explicitly).
- **Skill artifacts.** `auto-release.yml` Step 2.5/3.5 keep syncing the
  skill copy and bumping its frontmatter `version:`; the vendored dogfood
  copy refreshes after the tag as today (Rule #10 B).

## MIGRATION_v3 outline

Mirrors `docs/MIGRATION_v2.md` (pin lines → Contract → what is additive →
platform behaviour → further reading), with two new sections drawn from the
ledger.

| Section | One-line intent |
|---|---|
| Title + pin lines | `uses: DailybotHQ/ai-diff-reviewer@v3`; skill install line with `@v3`; exact frozen tag; **first line: "`@v2` keeps working unchanged; `v2` stops moving at `v2.x.y`"** |
| Contract | inputs renamed/removed: **none**; behaviors changed: BC-03/04/07/13/17 in one list; `AIPRR_` prefix unchanged; repo path unchanged |
| **What you must change** (checklist from the breaking rows) | review your strictness expectations (BC-07), budget expectations (BC-13), any automation parsing the review body (now generated — BC-07/BC-11), any dependence on the in-process wire shape (BC-03/17); if D-03 retired a lane: switch runner |
| **What you may adopt** (from the additive rows) | structured output + digest (BC-11), ensemble mode (BC-09), verifier knobs (BC-18), budget knobs (BC-19), `complexity-source` (BC-14), `api-key` optional with env credentials (BC-16), `model: balanced` explicit (BC-15) |
| Transition knobs (one minor cycle) | `strict-unverified-criticals: true`, `budget-profile: fixed` — and the release in which each is removed |
| Platform behaviour (v3) | verification pass on every review; consolidated review on matrices; run record and artifact on every run; IAR unchanged rails plus anchor-verified retirement |
| Eval gate | how releases are gated (BC-02) and where verdicts live (RFC-01 Q-01) |
| Further reading | CHANGELOG, RFC index, STRICTNESS, PERFORMANCE, PR_REVIEW_WORKFLOW, examples |

## Documentation surfaces to update

Rows × surfaces, so each implementation plan derives its documentation tasks
mechanically (✓ = must update in the landing PR).

| Row | README | action.yml | CHANGELOG | setup/reference.md | examples/ | PROVIDERS | STRICTNESS | PERFORMANCE | SECURITY | ARCHITECTURE | ITERATION_AWARENESS | PR_REVIEW_WORKFLOW | TESTING_GUIDE | MIGRATION_v3 | AGENTS.md |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| BC-01 | | | ✓ | | | | | | | ✓ | | | ✓ | | |
| BC-02 | | | ✓ | | | | | | | | | | ✓ | ✓ | ✓ (Rule #8 note) |
| BC-03 | ✓ | | ✓ | | | ✓ | | ✓ | | ✓ | | | | ✓ | |
| BC-04 | ✓ | | ✓ | ✓ | | ✓ | ✓ | | | | | | | ✓ | |
| BC-05 | | | ✓ | | | | | | ✓ | | | | | | |
| BC-06 | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | | | ✓ | | | | | ✓ | ✓ (runners list) |
| BC-07 | ✓ | | ✓ | ✓ | | | ✓ | | | | ✓ | | | ✓ | |
| BC-08 | | | ✓ | | | | | | | | ✓ | | | ✓ | |
| BC-09 | ✓ | ✓ | ✓ | ✓ | ✓ | | ✓ | | ✓ | ✓ | ✓ | ✓ | | ✓ | |
| BC-10 | | | | | | | | | | | | ✓ | | | ✓ |
| BC-11 | ✓ | ✓ | ✓ | ✓ | ✓ | | | | | ✓ | | | | | |
| BC-12 | | | ✓ | | | ✓ | | | | | | | | | |
| BC-13 | ✓ | | ✓ | ✓ | | | | ✓ | | | ✓ | | | ✓ | ✓ (DON'T #9 note) |
| BC-14 | ✓ | ✓ | ✓ | ✓ | | | | | | | | | | | |
| BC-15 | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | | | | | | | | ✓ | |
| BC-16 | ✓ | ✓ | ✓ | ✓ | | | | | ✓ | | | | | | |
| BC-17 | | | ✓ | | | ✓ | | | | | | | | ✓ | |
| BC-18 | ✓ | ✓ | ✓ | ✓ | ✓ | | ✓ | | | | | | | | |
| BC-19 | ✓ | ✓ | ✓ | ✓ | ✓ | | | ✓ | | | | | | | |

Every row with an `action.yml` ✓ also updates `.github/scripts/validate_action.py`'s
expectations implicitly (new inputs must be read by the runtime; new outputs
must have `value:` expressions) and adds an `examples/README.md` index row
when an example is added (Rule #7).

## Open questions

| Id | Question | Recommendation |
|---|---|---|
| Q-31 | `v2` maintenance window | six months of security/catalog fixes from a `release/v2` branch; no features |
| Q-32 | Ship an rc? | yes, `v3.0.0-rc.1` after Phase 2, gated by BC-02, ≥ 10 dogfood PRs; no moving `v3` tag for the rc |
| Q-33 | Should BC-06 (lane retirement) be decided before `v3.0.0` or deferred to `v3.1`? | Decide before `v3.0.0` from the Phase 1 measurement (D-03); a major is the only place to retire a lane |
| Q-34 | Keep `budget-profile: fixed` and `strict-unverified-criticals` for one minor cycle or the whole major? | one minor cycle (removed in `v3.1.0`), announced in MIGRATION_v3 with the removal version |
| Q-35 | Should `MIGRATION_v2.md` be edited to point forward? | Add one line at the top linking to `MIGRATION_v3.md`; leave the rest as the v2 record |

## Acceptance for the implementation plan

The release plan (RFC-08's final DWP) is complete when:

1. Every `BC-nn` row has a landing commit reference and a `MIGRATION_v3.md`
   entry (breaking rows under "What you must change", additive under "What
   you may adopt") before the `v3.0.0` tag.
2. `python3 .github/scripts/validate_action.py` passes on the final
   `action.yml`; every new input is read by the runtime and every new output
   has a `value:`.
3. The Rule #9 grep is clean: `action.yml` `name`, `description`, `branding`
   unchanged from v2.5.1.
4. `README.md` inputs and outputs tables, `skills/ai-diff-reviewer/setup/reference.md`,
   `docs/PROVIDERS.md`, `docs/STRICTNESS.md`, `docs/PERFORMANCE.md`,
   `docs/SECURITY.md` and the surfaces matrix above are current for every
   landed row (Final Review documentation reconciliation).
5. `MIGRATION_v2.md` carries the forward pointer (Q-35); `MIGRATION_v3.md`'s
   first line states that `@v2` is unchanged and when `v2` stops moving.
6. `CHANGELOG.md` `[3.0.0]` lists the breaking rows under a **Breaking**
   heading with their `BC-nn` ids.
