# RFC-01 — Eval-gate contract and run-record schema

## Status

Draft — discovery record (PLAN_v3_discovery, Task 3, 2026-09-23). Addresses
RFC-00 **P-1**, **P-7**, **P-8**. Implemented by the Phase 0 plan named in
RFC-08. Schema: [`schemas/run-record.schema.json`](schemas/run-record.schema.json)
(example: [`schemas/examples/run-record.example.json`](schemas/examples/run-record.example.json)).

## Problem

The repository has an evaluation **harness** but no evaluation **gate**.
`tests/eval/run_eval.py` scores a review against `tests/eval/corpus.json`
(4 merged PRs, 5 must-find labels, all `warning`), `tests/eval/corpus_validate.py`
enforces floors and blinded adjudication over the 73-case fixture corpus, and
`tests/eval/jev_experiment.py` plans replicated cells with a promotion exit bar —
yet nothing blocks a release on measured quality, precision is never measured
(RFC-00 E-19), adjudication is keyword matching (E-19), the live scorer has zero
critical must-find labels (E-18), no run keeps an immutable provenance
snapshot (P-7), and run-to-run variance exceeds every effect anyone has tried
to measure (E-14, E-14a, E-15, E-16). Every v3 phase after this one claims a
quality or cost effect; without this gate none of those claims can be told from
noise.

## Evidence

| RFC-00 row | What it establishes for this RFC |
|---|---|
| E-14, E-14a | Relative cost spread between identical runs: median 0.266, mean 0.282, worst 0.888 over 27 replicated cells (`metrics/baseline.json → noise_floor`) |
| E-15, E-15a | Recall moves by up to 2 defects between identical runs; 3 of 27 cells moved |
| E-16, E-17 | Every treatment effect measured so far sits inside that band; two repetitions are not a variance estimate |
| E-18 | The live scorer has no critical must-find label — critical precision/recall is unmeasurable today |
| E-19 | Scorer = path/line/keyword match; no false-positive rate exists |
| E-20 | Blinded adjudication changed 5 of 23 critical claims — adjudication is load-bearing |
| E-24, E-24a, X-02 | Ledgers disagreed until re-aggregated from records; per-run cost must be a record field, not a report |
| E-22, X-01 | Provider-stage and end-to-end latency were conflated; they must be separate fields |
| E-23, S7 F4 | Usage is sometimes unknown; "missing usage is `unknown cost`, never zero" |
| X-03, X-04 | Treatments were not what their names said — per-run prompt/extension hashes are mandatory |

The 73-case fixture corpus already holds **21 critical_positive** cases (21
critical labels, all blinded-adjudicated), 27 warning_positive and 25
negative_control across python/typescript/go, with 4 deceptive-metadata
cases and 2 policy/prompt-file cases — but only 7 of them are `historical_pr`
fixtures the reviewer can be run against today; the other 66 are `trees`
fixtures consumed by the triage harness, not by `run_eval.py`. The critical
cases exist; the reviewer has never been scored on them.

## Design

The gate is a **two-layer contract**:

1. **Offline layer (mandatory on every PR, in `code_check.yml`).** Validates
   the instruments: corpus pins and floors (`corpus_validate.py`), every stored
   run record against `run-record.schema.json`, and the campaign ledger's
   internal consistency (every cell present, cost known or explicitly unknown,
   no duplicate run ids). It spends nothing and fails the PR on a broken
   instrument.
2. **Online layer (budgeted; manual or nightly; never on every PR).** Runs
   replicated cells of the configured lanes against the pinned corpus, stores
   one run record per run, scores with the metrics below, and writes a
   **promotion/blocking verdict** file. The release workflow refuses to cut a
   `v3.x` tag while the latest verdict for the release candidate's
   `runtime_sha` + `prompt_sha256` is missing, stale, or blocking.

Everything the gate consumes is a run record; everything it emits is a
verdict derived from run records. Reports are rendered from records, never
authored by hand (X-02 is the reason).

## Metrics

Every metric names the run-record field it is computed from. Metrics are
computed **per cell** (`provider × endpoint_kind × model × arm × case`) over
its repetitions, then aggregated per lane. Lanes are published separately
(S7 "Publish measured model/backend lanes individually").

| Metric | Definition | Fields | Source of truth |
|---|---|---|---|
| Must-find recall | hits / must-find labels, per case, averaged per lane | `outcome.score.must_find_hits`, `must_find_total` | labels in `tests/eval/cases/*.json` and `corpus.json` |
| **Precision (source-grounded)** | adjudicated-true / (adjudicated-true + adjudicated-false) over all findings of a case; the adjudicator reads the code at the anchor (`path`, `line`, evidence excerpt from RFC-03) and the fixture's ground truth, never only the finding text | `outcome.score.adjudicated_true`, `adjudicated_false` | blinded adjudication record per finding (F7: independent reviewer; a model judge alone is never ground truth) |
| Unlabelled-finding rate | findings matching no label / findings; **reported, not scored** | `outcome.score.unlabelled` | — |
| False-positive traps hit | matches on `must_not_flag` labels | `outcome.score.false_positives` | corpus labels |
| Severity calibration | critical claims confirmed / downgraded / rejected under blinded adjudication | `outcome.findings_by_severity.critical`, adjudication record | adjudication record |
| Cost (total) | `cost_usd` when `usage_known`; otherwise the run is **unknown-cost** and excluded from means but counted | `cost_usd`, `usage_known`, `cost_basis` | vendor/CLI usage; indicative table for in-process lanes; flat-rate plans reported with `cost_basis = flat-rate-plan` and list price beside it |
| Latency | `provider_seconds` and `total_seconds` reported **separately**; p50 and p95 per lane | `timings.*` | run record |
| **Determinism** | per cell with n ≥ 3: relative cost spread `(max−min)/mean` and recall delta `max−min`; lane summary = median and worst | `cost_usd`, `outcome.score.must_find_hits` | run records (same computation as `analysis_results/baseline_metrics.py`) |
| Budget use | turns used / max, tool calls, verifier runs | `budget.*` | run record |
| Coverage completeness | share of runs with `diff_truncated = false` and `omitted_files = 0` | `context.*` | run record |

A number computed from fewer than 3 repetitions is labelled *descriptive*
and cannot support a promotion or blocking verdict.

## Thresholds

Derived from `analysis_results/metrics/baseline.json` (Task 2, n = 27
replicated cells from the Jev campaign, grok-4.5 CLI dominated) and the
frozen statistics rule F9 (paired differences, bootstrap 95 % interval
clustered by PR family, ×3 repetitions). Phase 0 re-measures the floor on
the stabilized runtime (temperature 0 shipped in v2.4.0) and **replaces these
numbers with the re-measured ones in the same table**; until then the Jev
figures are the floor.

| Verdict | Rule | Number today | Why this number |
|---|---|---|---|
| **Promotable — cost** | paired relative cost difference, CI excludes 0 **and** point estimate ≥ 1.0 × median cell spread | ≥ 0.27 (≈ 27 %) | anything smaller is inside one cell's typical repetition spread (`noise_floor.cost_relative_spread_median` = 0.266) |
| **Promotable — recall** | net additional must-find hits across the corpus, CI excludes 0 **and** net ≥ (max observed repetition swing + 1) | ≥ 3 defects | the largest swing between identical runs was 2 (`noise_floor.recall_delta_max`) |
| **Promotable — precision** | false-positive findings per case reduced, CI excludes 0, ≥ 20 adjudicated positive cases in the sample | ≥ 20 % fewer FP per case | S7 "Claim improved quality only with separate evidence: ≥ 20 % fewer false-positive findings per PR" |
| **Blocking — recall regression** | lane recall on the pinned corpus below the stored baseline by more than the swing | > 2 defects below baseline | E-15a |
| **Blocking — unadjudicated critical** | any `critical_positive` label with `adjudication.status != adjudicated` in the scored set | 0 tolerated | `corpus_validate.py` F7 rule, extended to the reviewer scorer |
| **Blocking — determinism widened** | lane median spread > 1.5 × stored baseline median, or worst > 1.0 | median > 0.40 or worst > 1.0 | baseline median 0.266, worst 0.888 |
| **Blocking — instrument** | any run record invalid against the schema; any first-party lane run with `usage_known = false`; a cell missing a repetition | 0 tolerated | P-7; S7 F4 |
| **Descriptive only** | n < 3 per cell, or a lane with fewer than 4 cases | — | E-17 |

The promotion factor 1.0 × median spread is deliberately conservative and
cheap to explain; Phase 0's decision log may raise it, never lower it, after
the re-measured floor exists.

## Provenance

One run record per run, written **before** any report is rendered, immutable
after (append-only campaign directory; a corrected run is a new record with a
new `run_id` and the old one marked `status: failed` by a companion note,
never edited). The schema (draft 2020-12, `additionalProperties: false` at
the top level) carries:

- identity: `run_id`, `recorded_at`, `runner` (`in-process` / `cli` /
  `aggregator`), `provider`, `endpoint_kind` (**never a host or URL**), exact
  `model` and the `model_alias` it resolved from;
- pins: `runtime_sha`, `prompt_sha256`, `extension_sha256`,
  `context.corpus_sha256` + `corpus_case_id` for evaluation runs;
- what was actually sent: `sampling.requested` vs `sampling.sent` with the
  `stripped` list (the adaptive HTTP-400 fallback changes the wire — X-04 is
  the reason this must be recorded);
- context completeness: `changed_files`, `omitted_files`, `diff_chars`,
  `diff_truncated`, `iar_mode`, `instruction_files_read` (RFC-02/03 hooks);
- budget and outcome counts, severity histogram, verified/downgraded/refuted
  counts (RFC-03; `findings_refuted` added by Task 7), gate result, optional score;
- `usage_known` + nullable `usage`, nullable `cost_usd` + `cost_basis`;
  `timings` split into setup / provider / verifier / total; `status` and
  `failure_class`; optional `campaign` cell/arm/repetition.

The example passes the plan-local schema walk and contains no hostname,
credential or log excerpt. Production runs emit the same record (RFC-05
embeds it in the structured output); evaluation runs add `campaign` and
`outcome.score`.

## Corpus gaps

Phase 0 must close these before Phase 1 can measure critical precision.
Additions follow the fixture-pinning and adjudication rules already enforced
by `corpus_validate.py` (`EXPECTED_CASE_IDS` extended explicitly; content
SHA-256 pins; blinded adjudication for every critical label).

| Gap | Today | Minimum for Phase 1 | Why |
|---|---|---|---|
| Critical must-find cases scored **against the reviewer** | 0 in the live scorer (E-18); 21 critical `trees` fixtures unused by `run_eval.py` | run the reviewer against fixture trees (`context.repo_kind = fixture_tree`) so all 21 adjudicated critical cases score; ≥ 20 critical positives in any lane's scored set before a precision claim | E-18, E-20; the cases already exist |
| Cross-file defects | 1 case with labels spanning more than one path | ≥ 6 | P-2 (tool parity is invisible on single-file cases) |
| Instruction-file contradictions (PR #37 class) | 2 `policy_prompt_file` cases; the #37 historical PR | ≥ 5, across the three stacks | E-13, H-05 |
| Misleading PR metadata | 4 deceptive-metadata cases | ≥ 6, at least one where the description claims a fix that is absent | S6 §8 (untrusted metadata) |
| Omitted / oversized patch | 0 scored | ≥ 4 with a labelled defect **inside** the omitted portion | E-26, E-31 |
| Multi-round IAR with a prior unresolved finding | 3 cases mention rounds; none scored live | ≥ 4 fixtures with round-1 findings and a round-2 delta, one where the anchor moved | P-5, P-9 |
| Stacks | python 40 / typescript 22 / go 11 | keep the 3-stack floor; add ≥ 4 cases each in two further stacks the dogfood repos actually use | S7 corpus rule |
| Adjudication coverage | 33 adjudicated / 40 pending (all pending are warning/negative) | every case in a scored precision sample adjudicated, positives and negatives | precision needs both sides |

Labels come from fix commits or reproducible failures, never from a model's
output (`tests/eval/README.md`). A case whose ground truth changed at the
evaluated revision is re-adjudicated at that revision (IJA §7).

## CI wiring and budget

**Offline (every PR).** A `eval-gate-offline` job in `code_check.yml`, next
to the existing `tests` job: `corpus_validate.py --json`, schema validation
of every `tests/eval/records/**/*.json` against `run-record.schema.json`
(stdlib walk, no dependency), ledger consistency, and `jev_experiment.py
validate` where a manifest exists. Failure blocks the PR. Cost: none.

**Online (budgeted).** A `workflow_dispatch` (and optional weekly schedule)
`eval-campaign.yml` that runs replicated cells for the lanes whose secrets
exist — today **XAI** (grok CLI, pay-per-token) and **ZAI** (claude-code on
GLM, flat-rate plan). It writes run records as artifacts and commits the
verdict file to a records branch or uploads it as a release asset (decision
D-01 in RFC-08). Only maintainers dispatch it; the workflow refuses to start
without an explicit `budget_usd` input and stops at 90 % of it (F8). The new
workflow file is registered in `.github/dependabot.yml`'s `github-actions`
section in the same PR (repository rule in `.review/extension.md` §
"Documentation sync with the audits"), so its action pins are bumped.

**Release blocking.** Options, with recommendation:

1. `auto-release.yml` precondition step: read the latest verdict for the
   candidate's `runtime_sha`/`prompt_sha256`; skip the release with a clear
   log line when missing or blocking. **Recommended** — no new required
   check, no branch-protection change, honest failure mode (release does not
   happen; nothing is half-published).
2. A required status check on `main` — rejected for now: the online campaign
   cannot run on every push, so the check would be perpetually stale.

**Budget estimate** (per-run costs from `metrics/baseline.json`, n in
parentheses; assumptions flagged):

| Campaign | Cells × reps | Runs | grok-4.5 CLI at $0.47/run (E-01, n = 14) | GLM via claude-code | Total per campaign |
|---|---|---|---|---|---|
| Phase 0 floor re-measurement (7 historical PRs, current default config) | 7 × 3 | 21 per lane | ≈ $10 | ≈ $0 marginal (list ≈ $35) | ≈ $10 |
| Fixture-tree baseline (66 trees) | 66 × 3 | 198 per lane | ≈ $60 **assuming ≈ $0.30/run on small trees — unmeasured; the first 10 runs calibrate it** | ≈ $0 marginal | ≈ $60 |
| One treatment comparison (2 arms, 7 PRs) | 2 × 7 × 3 | 42 per lane | ≈ $20 | ≈ $0 marginal | ≈ $20 |
| One treatment comparison on trees | 2 × 66 × 3 | 396 per lane | ≈ $120 (same assumption) | ≈ $0 marginal | ≈ $120 |

What cannot be measured with today's secrets: any in-process first-party
lane (anthropic / openai kinds), Bedrock, Codex on Azure, Cursor. RFC-08's
budget section states what the maintainer would need to enable for each.

## Alternatives considered

| Alternative | Why not |
|---|---|
| Keep the harness advisory (status quo) | P-1 persists: every later phase would ship unmeasurable claims; the Jev campaign already showed where that leads |
| A third-party evaluation framework | Rule #2 (stdlib-only runtime) and the supply-chain posture; the harness is 3 stdlib files and the gate adds a schema walk |
| LLM-as-judge without code access | The finding-only weakness (IJA §6): a persuasive wrong finding passes; adjudication must read the anchor |
| Required status check for the online campaign | Perpetually stale on a per-push check; release precondition is honest |
| Measure only recall (today's shape) | A prompt that doubles false positives "finds more"; precision is the metric P-4 needs |

## Impact on the public contract

- **Additive:** the run record as a produced artifact (RFC-05 embeds it);
  a `records/` layout under `tests/eval/`; two workflows (`eval-gate-offline`
  job, `eval-campaign.yml`). No `action.yml` input changes in Phase 0.
- **Behavioral (release process, not consumer-facing):** `auto-release.yml`
  gains a verdict precondition; a release can be skipped for quality reasons.
- Ledger rows: RFC-07 `BC-01` (run-record artifact), `BC-02` (release
  precondition). Neither is breaking.

## Open questions

| Id | Question | Recommendation |
|---|---|---|
| Q-01 | Where do run records live: a `records/` branch, release assets, or committed under `tests/eval/records/`? | Commit **verdicts** (small) under `tests/eval/records/verdicts/`; keep raw run records as workflow artifacts with 90-day retention plus a monthly squash into a records branch. Decision D-01 (RFC-08) |
| Q-02 | Promotion factor: 1.0 × median spread or higher? | Start at 1.0; revisit after the Phase 0 re-measurement with the stabilized runtime |
| Q-03 | Should the fixture-tree runs use a synthetic PR (git worktree with the tree applied) or a new `run_eval.py --tree` mode? | `--tree` mode building a temporary worktree; no GitHub PR needed, so trees can be scored offline of GitHub |
| Q-04 | Who adjudicates precision samples? | The maintainer, blinded to the arm, with the model's finding hidden until the code was read; recorded per finding with the same `adjudication` shape as case labels |
| Q-05 | Promote `analysis_results/baseline_metrics.py` into `tests/eval/`? | Yes, as the determinism computation of the gate (RFC-08 D-02) |

## Acceptance for the implementation plan

The Phase 0 DWP is complete when every criterion below holds:

1. `run-record.schema.json` is copied to `tests/eval/schemas/` unchanged and
   every lane (in-process and CLI) emits a schema-valid record on the 7
   historical PRs; the offline job validates them in CI.
2. Three replicated cells of the current default configuration (grok-4.5 CLI,
   `balanced`) on the 7 historical PRs are stored as run records, and the
   Thresholds table is re-stamped with the measured median/worst spread and
   recall delta.
3. `run_eval.py` scores the reviewer against fixture trees; all 21 adjudicated
   critical cases produce a score field.
4. The corpus additions in *Corpus gaps* reach their minimums with
   `corpus_validate.py` green and `EXPECTED_CASE_IDS` updated explicitly.
5. A synthetic regression (a prompt change that demonstrably drops recall by
   3 on the corpus) produces a **blocking** verdict, and `auto-release.yml`
   skips the release on it in a dry run.
6. Precision is reported for at least one lane from a blinded adjudication
   sample of ≥ 20 positive cases.
7. `docs/TESTING_GUIDE.md` registers the offline job and the campaign
   workflow; `docs/PERFORMANCE.md` links the verdict format;
   `.github/dependabot.yml` lists `eval-campaign.yml`.

## Cited symbols

- `estimate_cost_usd`
- `UsageTelemetry`
- `compute_check_gate`
- `resolve_endpoint_profile`
- `STRICTNESS_BLOCK_CRITICAL`
- `SEVERITY_CRITICAL`
