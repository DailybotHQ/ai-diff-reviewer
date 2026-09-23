# RFC-08 — Roadmap, phase-to-plan decomposition, release cuts, evaluation budget and decision log

## Status

Draft — discovery record (PLAN_v3_discovery, Task 10, 2026-09-23). Consumes
RFC-00…07. This is the document the maintainer acts on: every open question
from the RFC set is a decision-log row with a recommendation and an owner;
every phase is a named Deep Work Plan that can be created from the record
with `/dwp-create`.

## Phases

Ordered by dependency, not attractiveness. Each phase leaves the product
publishable. Entry criteria are RFC acceptance items already holding; exit
criteria are RFC-01 measurements.

| Phase | Objective | Implements | Entry criteria | Exit criteria |
|---|---|---|---|---|
| **0 — Measure** | Turn the harness into a release gate; re-measure the noise floor on the stabilized runtime; grow the corpus so critical precision is measurable | RFC-01 (all), Task 2 tooling promoted (D-02) | RFC set accepted (D-00) | RFC-01 acceptance 1–7: run records on every lane, 3 replicated cells of the default config stored, fixture trees scored against the reviewer (21 critical cases), corpus gaps closed, blocking verdict demonstrated on a synthetic regression, precision reported for one lane with n ≥ 20 |
| **1 — Runner + verification** | One control loop with tool parity; findings carry evidence; criticals are verified; instruction files are read; structured output exists | RFC-02, RFC-03, RFC-05 (single-leg parts), BC-03/04/05/07/08/11/12/17/18 | Phase 0 exit | RFC-02 acceptance 1–6 and RFC-03 acceptance 1–7 under RFC-01 replication; D-03 decided from the in-process vs CLI table |
| **2 — Consolidation** | One published review per head for matrix consumers; aggregator-side verifier; dogfood on the reference topology | RFC-04, RFC-05 (aggregate parts), BC-09/10 | Phase 1 exit; `mode: review` byte-comparable | RFC-04 acceptance 1–7: duplicate share ≤ 15 % on the labelled thread set at key precision ≥ 0.95 |
| **3 — Incremental** | Delta-scaled follow-up rounds; verifier-only no-change rounds; anchor-verified retirement live | RFC-06 (incremental half), RFC-03 retirement, BC-08/13 | Phase 1 exit (retirement), Phase 0 multi-round fixtures | RFC-06 targets: −50 % input tokens on 1–3-file deltas, 0 review turns on no-change rounds, recall unchanged |
| **4 — Risk-tiered budgets** | Deterministic classification before spend; budget matrix; `high-risk-paths`, `budget-profile`, `complexity-source` | RFC-06 (classification half), BC-13/14/15/19 | Phase 3 exit | RFC-06 acceptance 1–6 incl. low-tier cost ≤ $0.30 with recall unchanged, zero cap-exhausted zero-finding runs |
| **R — Release** | Ledger closure, `MIGRATION_v3.md`, rc, `v3.0.0` cut, `v2` maintenance branch | RFC-07 (all), BC-02 live, BC-15/16 | Phases 0–2 (minimum for `v3.0.0`, see cuts) | RFC-07 acceptance 1–6; rc dogfooded on ≥ 10 PRs; gated release passes |

## Implementation plans

One Deep Work Plan per phase (Full format recommended for every plan that
touches `scripts/reviewer.py` — shared-core file, Rule #2/#3/#5 discipline,
DON'T #9). Sizes are task-count bands, not estimates of effort.

| Plan | Scope (areas) | Inputs from this record | Gates (repository commands + RFC-01 measurement) | Depends on | Ships | Size |
|---|---|---|---|---|---|---|
| `PLAN_v3_phase0_eval_gate` | `tests/eval/` (schemas, run-record emission, `--tree` mode, records layout, verdict), `.github/workflows/code_check.yml` (offline job), new `eval-campaign.yml`, `auto-release.yml` precondition, corpus additions under `tests/eval/cases/`, `analysis_results/baseline_metrics.py` → `tests/eval/`, `docs/TESTING_GUIDE.md`, `docs/PERFORMANCE.md` | RFC-01, `schemas/run-record.schema.json`, Task 2 metrics | `py_compile`; `unittest discover` (new `test_eval_*` modules); `corpus_validate.py`; `validate_action.py` unchanged; offline job green; one live campaign under `budget_usd` | D-00, D-01, D-02 | internal (`v2.6.0`-class additive release is possible but **not recommended** — keep the gate for the `v3` line so the first gated release is the rc) | 8–12 tasks |
| `PLAN_v3_phase1_unified_runner` | `scripts/reviewer.py` (parity tools, inventory, `get_patch`, `read_instruction_files`, `emit_finding`, status enum, `MAX_PATCH_CHARS`), CLI prompt blocks + `.aiprr/inventory.json`, `parse_findings_file` v3 superset, `docs/ARCHITECTURE.md` § 8, `docs/PROVIDERS.md`, `docs/SECURITY.md` | RFC-02, RFC-05 (input superset), BC-03/04/05/12/17 | `py_compile`; scoped tests + full suite (shared core); RFC-01 campaign on the 7 PRs per lane; C067 (PR #37) record check | Phase 0 | rc candidate | 10–14 tasks |
| `PLAN_v3_phase1_verification` | `scripts/reviewer.py` (finding v3, verifier, severity policy, structured summary, retirement anchor re-read), `prompts/default.md` (+ skill byte-copy via release sync), `action.yml` (`verifier`, `verifier-model`, `strict-unverified-criticals`), `docs/STRICTNESS.md`, `docs/ITERATION_AWARENESS.md` § 7.1, `docs/PROMPTS.md` | RFC-03, `schemas/finding-v3.schema.json`, BC-07/08/18 | `py_compile`; full suite; `validate_action.py`; prompt before/after on a real PR (Rule #10 A); RFC-01 precision campaign on the critical corpus (n ≥ 20) | Phase 1 runner (tools) | rc candidate | 10–14 tasks |
| `PLAN_v3_phase1_structured_output` | `scripts/reviewer.py` (`review-output.json`, digest outputs, truncation, scrub), `action.yml` outputs, artifact upload step, `skills/ai-diff-reviewer/apply-review/` (artifact path + fallback), `README.md` outputs table, `setup/reference.md` | RFC-05, `schemas/review-output-v3.schema.json`, BC-11 | `py_compile`; full suite; `validate_action.py`; `validate-frontmatter.py`; prompt-sync invariant; schema validity on 7 PRs | Phase 1 verification (finding v3) | rc candidate | 6–9 tasks |
| `PLAN_v3_phase2_ensemble` | `scripts/reviewer.py` (`mode`, aggregator, dedup, agreement, gating knobs, aggregate marker, migration collapse), `action.yml` inputs/outputs, `.github/workflows/self-review.yml` (reference topology, label retirement), `examples/ensemble-matrix.yml` + index, `docs/PR_REVIEW_WORKFLOW.md`, `docs/SECURITY.md` | RFC-04, BC-09/10 | `py_compile`; full suite (dedup fixtures from the PR #57/#58 labelled threads); `validate_action.py`; examples parse; actionlint; dogfood run on this repo | Phase 1 (all three) | `v3.0.0-rc.1` | 9–12 tasks |
| `PLAN_v3_phase3_incremental` | `scripts/reviewer.py` (delta-scaled budget, verifier-only round, retirement wiring), `docs/ITERATION_AWARENESS.md` § 9, `docs/PERFORMANCE.md` | RFC-06 (incremental), RFC-03 retirement, BC-08/13 | `py_compile`; full suite (multi-round fixtures); RFC-01 campaign on multi-round fixtures | Phase 1 verification; Phase 0 fixtures | `v3.0.0` or `v3.1.0` (D-08) | 6–8 tasks |
| `PLAN_v3_phase4_risk_budgets` | `scripts/reviewer.py` (`classify_inventory`, budget matrix constant, tier → budget wiring, `complexity-source`), `action.yml` (`budget-profile`, `high-risk-paths`, `complexity-source`), `docs/PERFORMANCE.md`, `docs/PR_METADATA_CHECKS.md`, `README.md` | RFC-06 (classification), BC-13/14/15/19 | `py_compile`; full suite (classifier + untrusted-metadata tests); `validate_action.py`; RFC-01 per-tier recall guard + low-tier cost | Phase 3 | `v3.0.0` or `v3.1.0` (D-08) | 8–10 tasks |
| `PLAN_v3_release` | `CHANGELOG.md` `[3.0.0]` Breaking section, `docs/MIGRATION_v3.md`, `MIGRATION_v2.md` forward pointer, `release/v2` branch, rc + tag, README/reference sweeps for every landed BC row, `action.yml` Rule #9 check | RFC-07 (ledger + outline + surfaces matrix) | `validate_action.py`; Rule #9 grep; full battery; BC-02 verdict present and non-blocking for the candidate `runtime_sha`; rc dogfood evidence | Phases 0–2 (+3–4 if D-08 says so) | `v3.0.0` | 6–9 tasks |

Parallelism: `phase1_verification` and `phase1_structured_output` both
depend on `phase1_unified_runner` and touch `scripts/reviewer.py`; run them
**sequentially** (shared-core collision risk), verification first. Phases 3
and 4 are sequential by dependency. Nothing here is a candidate for team
agents beyond documentation sweeps.

## Release cuts

| Cut | Contains | Does not contain | Condition |
|---|---|---|---|
| `v3.0.0-rc.1` | Phases 0, 1, 2; BC-02 gate live; `MIGRATION_v3.md` draft; transition knobs (`strict-unverified-criticals`, `budget-profile: fixed` if Phase 4 landed) | Phases 3–4 unless finished (D-08) | first release **gated** by the eval verdict; dogfooded ≥ 10 PRs on this repo (XAI + ZAI legs, reference topology); no moving `v3` tag |
| **`v3.0.0`** | everything in the rc + fixes; **recommended minimum: Phases 0–2** — the measurable, verified, consolidated reviewer; D-03 decided (retire or keep in-process lanes) | Phases 3–4 if D-08 defers them | RFC-07 acceptance 1–6; `v2` stops moving; `release/v2` branch created |
| `v3.1.0` | Phases 3–4 if deferred; removal of the transition knobs (Q-34) | — | RFC-06 targets met under RFC-01 |
| `v2.x.y` (maintenance) | security fixes; provider-catalog breakages | features | six months from `v3.0.0` (D-09) |

Interaction with `auto-release.yml`: the BC-02 precondition reads the
latest verdict for the candidate's `runtime_sha`/`prompt_sha256`; Step 2.5
(skill artifact sync) and Step 3.5 (vendored dogfood refresh) stay as they
are; `release.yml` keeps force-moving the major alias, so the first
`v3.0.0` push creates `v3` and the last `v2.x.y` push is the final move of
`v2`. The rc is cut manually (`/release` with an explicit pre-release tag)
because `auto-release.yml`'s bump derivation has no rc path — recorded as a
release-plan task, not a workflow redesign.

## Evaluation budget

Per-run costs from Task 2 (`metrics/baseline.json`): grok-4.5 CLI ≈ $0.47
(E-01) to $0.52 (cells mean incl. repetitions), pr39-class ≈ $1.07; GLM on
the flat plan ≈ $0 marginal (list ≈ $1.63); fixture-tree runs **unmeasured**
(assumed ≈ $0.30, calibrated by the first 10 runs). Only **XAI + ZAI** secrets
exist today.

| Phase | Campaign | Runs (per lane) | grok-4.5 | GLM | Total | Cannot be measured today |
|---|---|---|---|---|---|---|
| 0 | floor re-measurement, default config, 7 PRs × 3 | 21 | ≈ $10 | ≈ $0 | ≈ $10 | in-process first-party lanes, Bedrock, Codex/Azure, Cursor |
| 0 | fixture-tree baseline, 66 trees × 3 | 198 | ≈ $60 (assumption) | ≈ $0 | ≈ $60 | same |
| 1 | runner parity: in-process vs CLI on 7 PRs × 3 × 2 arms — **needs an in-process lane** | 42 | ≈ $20 (grok via `openai` runner on xAI **is** in-process — the one in-process lane measurable with XAI) | — | ≈ $20 | anthropic in-process (needs `ANTHROPIC_API_KEY`) — D-03 can be decided on the xAI in-process lane first |
| 1 | verifier precision on the critical corpus, 21 cases × 3 × 2 arms | 126 | ≈ $40 (assumption) + verifier ≈ $5 | ≈ $0 | ≈ $45 | — |
| 2 | consolidation on the dogfood thread set (offline fixtures) + 10 live PRs | live only | ≈ $15 | ≈ $0 | ≈ $15 | third leg (needs another secret; fixture-tested otherwise) |
| 3–4 | multi-round fixtures 4 × 3 × 2 rounds × 2 arms; per-tier recall guard on the corpus | 48 + 219 | ≈ $25 + $65 (assumption) | ≈ $0 | ≈ $90 | — |
| **Total** | | | | | **≈ $240** at grok-4.5 list price, dominated by fixture-tree assumptions | |

To unlock what cannot be measured, the maintainer would need, in priority
order: `ANTHROPIC_API_KEY` (decides D-03 for the default first-party lane and
enables the economy-verifier hypothesis H-06 on Anthropic kinds),
`AWS_REVIEW_CREDENTIALS` (Bedrock lane regression), `OPENAI_API_KEY`
(Codex/openai-kind lanes). Each is a repo secret; none changes the plan
structure.

## Decision log

ADR-style. Owner: maintainer unless stated. Status `open` until recorded
here as `accepted` / `rejected` with a date. Rows that change the public
contract name their `BC-nn`.

| Id | Question | Options | Recommendation | Evidence | BC | Status |
|---|---|---|---|---|---|---|
| D-00 | Accept the RFC set as the v3 direction? | accept / revise / reject | **accept**; an RFC moves to *Accepted* only via this row | RFC-00 P-1..P-4 | — | open |
| D-01 | Where run records and verdicts live (RFC-01 Q-01) | committed under `tests/eval/records/`; records branch; release assets | verdicts committed (small), raw records as artifacts (90 d) + monthly squash to a records branch | X-02 | BC-01 | open |
| D-02 | Promote `analysis_results/baseline_metrics.py` into `tests/eval/` (RFC-01 Q-05; Task 2 skills entry T2-001) | promote / keep plan-local | **promote** as the determinism computation of the gate | Task 2 | — | open |
| D-03 | In-process `anthropic`/`openai` lanes: rebuild or retire for code review (RFC-02 Q-10; BC-06) | rebuild on the unified loop / retire, keep as verifier engine | **rebuild**, decide retire after Phase 1 measurement: retire if recall stays > 3 defects below the CLI band across two lanes | E-09, E-10, H-02 | BC-06 | open (decided by measurement) |
| D-04 | Promotion factor 1.0 × median spread (RFC-01 Q-02) | 1.0 / 1.5 / CI-only | 1.0 now; revisit after the Phase 0 re-measurement; never lower | E-14a | — | open |
| D-05 | Fixture-tree scoring mode (RFC-01 Q-03) | `run_eval.py --tree` worktree / synthetic PRs | `--tree` | E-18 | — | open |
| D-06 | Verifier placement on multi-leg runs (RFC-03 Q-11, RFC-02 Q-09) | per leg / once in the aggregator; CLI-side inside the CLI / runtime-side | once in the aggregator; runtime-side with in-process tools for CLI lanes | P-3 | — | open |
| D-07 | Default verifier alias and warning sample (RFC-03 Q-13, Q-15; RFC-06 Q-30) | economy 30 % / balanced 30 % / economy 100 % | economy, 30 % at `standard`, 4 verifier turns per finding; fallback balanced if H-06 fails | E-05, H-06 | BC-18 | open |
| D-08 | Phases 3–4 in `v3.0.0` or `v3.1.0`? | include / defer | **defer to `v3.1.0` unless finished before the rc soaks** — the major is justified by Phases 0–2 alone (BC-03/04/07/17) | RFC-07 SemVer | BC-13 | open |
| D-09 | `v2` maintenance window (RFC-07 Q-31) | 3 / 6 / 12 months | 6 months, security + catalog fixes, `release/v2` | Rule #8 | — | open |
| D-10 | Ship `v3.0.0-rc.1`? (RFC-07 Q-32) | yes / no | yes, after Phase 2, ≥ 10 dogfood PRs, no moving tag | BC-02 | — | open |
| D-11 | Transition knobs lifetime (RFC-07 Q-34; RFC-03 Q-12; RFC-06 Q-29) | one minor / whole major | one minor cycle; removal version stated in MIGRATION_v3 | — | BC-18, BC-19 | open |
| D-12 | Retire BC-06 lane before `v3.0.0` or in `v3.1` (RFC-07 Q-33) | before / after | before `v3.0.0` if D-03 says retire — a major is the only place to do it | Rule #4 | BC-06 | open |
| D-13 | `MIGRATION_v2.md` forward pointer (RFC-07 Q-35) | add one line / leave | add one line | — | — | open |
| D-14 | Base-ref file reads: new tool or `ref` argument (RFC-02 Q-06) | new tool / `ref ∈ {base, head}` | `ref` argument | — | — | open |
| D-15 | `MAX_PATCH_CHARS` (RFC-02 Q-07) and `MAX_REVIEW_OUTPUT_BYTES` (RFC-05 Q-23) | 40 000 / 4 MB | 40 000; 4 MB | E-26 | — | open |
| D-16 | Inventory delivery to CLI lanes (RFC-02 Q-08) | prompt only / prompt + file | both | — | — | open |
| D-17 | Dedup thresholds (RFC-04 Q-16, Q-17) | window 3 vs 5; 0.6 ratio / 0.4 Jaccard | 3; 0.6 / 0.4; calibrated on the labelled thread set | E-35 | — | open |
| D-18 | Aggregate as `mode` input vs separate action (RFC-04 Q-18) | same action / `aggregate/action.yml` | same action, `mode` | Rule #4 | BC-09 | open |
| D-19 | Forgotten aggregate job behavior (RFC-04 Q-19); per-leg progress comments (Q-20) | tracking note + exit 0 / fail; none / per-leg | tracking note + exit 0; none | P-3 | — | open |
| D-20 | Refuted findings surface (RFC-03 Q-14) | structured output only / minimized comments | structured output + summary section only | P-3 | — | open |
| D-21 | Embed vs reference the run record; store `rendered_markdown` (RFC-05 Q-21, Q-22) | embed / reference; store / re-render | embed; store | X-02 | BC-11 | open |
| D-22 | SARIF export (RFC-05 Q-24) | derived optional artifact / none | derived, behind `export-sarif`, Phase 2+ | — | — | open |
| D-23 | `apply-review` artifact-first with thread fallback (RFC-05 Q-25) | artifact only / artifact + fallback | artifact + fallback | — | — | open |
| D-24 | Tier thresholds and `dependencies` tier (RFC-06 Q-26, Q-27) | 300/1 500 lines; dependencies `standard` vs `elevated` | 300 / 1 500; `elevated` | F6 weights | BC-13 | open |
| D-25 | `deep` alias vs second leg at `critical` tier (RFC-06 Q-28) | deep / balanced + leg | balanced + leg when an ensemble exists; deep single-leg | E-12 | — | open |
| D-26 | Adjudicator for precision samples (RFC-01 Q-04) | maintainer blinded / external | maintainer, blinded to the arm, per-finding record | E-20 | — | open |
| D-27 | Promote the plan-local RFC gate (`check_rfc.py`) into the repository? | promote as a docs check / keep plan-local | keep plan-local; the RFCs become *Accepted* records, not living docs — re-evaluate if RFC sets recur | — | — | open |
| D-28 | Additional provider secrets for measurement | none / `ANTHROPIC_API_KEY` / + AWS / + OpenAI | `ANTHROPIC_API_KEY` first (decides D-03 on the default lane) | Evaluation budget | — | open |

## Risks

| Risk | Why it is real | Mitigation |
|---|---|---|
| Phase 0 is skipped or trimmed because it ships no feature | It is the phase the Jev campaign showed indispensable and the one with no visible product change | RFC-08 makes Phase 0 the entry criterion of every other plan; the rc is the first gated release, so skipping it blocks the release path, not just the science |
| The corpus cannot measure critical precision until trees are scored | Zero live critical must-find labels today (E-18) | Phase 0 acceptance 3 (fixture-tree scoring) is a hard gate for Phase 1's precision claims; the 21 adjudicated cases already exist |
| Only two dogfood legs (XAI, ZAI) | Consolidation with n = 2 hides most of the agreement signal; the third leg is fixture-tested only | D-28 (a third secret) or accept fixture-only coverage for the third leg until one exists; record it in MIGRATION_v3 limitations |
| In-process rebuild does not close the gap | H-02 may be wrong; the gap may be model-side prompting the CLIs do better | D-03 is decided by measurement with a stated retire threshold; the verifier/economy role keeps the lane useful either way |
| Verifier adds cost without removing false criticals | H-01 may be weaker than R-03 suggests | RFC-01 precision campaign with n ≥ 20 before BC-07 ships in an rc; fail-open design means the worst case is v2 behavior plus ≈ $0.05–0.15 |
| Fixture-tree run cost is an assumption | ≈ $0.30/run is unmeasured | first 10 runs calibrate; budget table re-stamped before the 66 × 3 campaign |
| Ledger drift during implementation | 20 rows across 8 plans | every implementation PR names its `BC-nn`; the release plan's acceptance 1 checks every row has a landing commit |
| Rule #9 by accident | a rename slips into a README example | release-plan acceptance 3 (identity grep on `action.yml`) |

## Acceptance for the implementation plan

This record is complete (and the discovery plan closes) when:

1. Every open question in RFC-00…07 (Q-01…Q-35) and every *pending* ledger
   row (BC-06) appears above as a `D-nn` row with a recommendation and an
   owner — checked by reading the RFCs' *Open questions* tables.
2. Every phase names a plan with scope, inputs, gates and dependencies;
   release cuts and the budget carry numbers with their assumptions.
3. `docs/rfc/v3/README.md` indexes RFC-00…08 with status; `docs/README.md`
   and `AGENTS.md` link to it.
4. The whole-contract gate (`check_rfc.py all`) passes on the complete RFC
   set.

The **first action after acceptance (D-00)** is
`/dwp-create PLAN_v3_phase0_eval_gate` from RFC-01 and this table.
