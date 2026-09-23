# RFC-00 — Evidence ledger and problem statement for AI Diff Reviewer v3

## Status

Draft — discovery record (PLAN_v3_discovery, Task 1, 2026-09-23). This
document is evidence only: it proposes no design. Every quantitative claim
below carries exactly one epistemic label:

| Label | Meaning |
|---|---|
| **verified** | re-derivable today from a file in this repository or in the retained plan record (`.dwp/plans/PLAN_jev_review_acceleration/analysis_results/`), or from GitHub metadata re-queried on 2026-09-23 |
| **recorded** | session telemetry from this maintainer's PR #57 / #58 dogfood sessions, with PR or run identifiers but no artifact in the repository |
| **hypothesis** | an inference the v3 design rests on; RFC-01 states what would confirm it |

Later RFCs cite rows by id (`E-nn`, `R-nn`, `H-nn`, `X-nn`, `P-n`).

## Sources

| Id | Source | What it contains | Date |
|---|---|---|---|
| S1 | `.dwp/plans/PLAN_jev_review_acceleration/analysis_results/LEARNINGS.md` | Campaign lessons: provider/model behavior, noise floor, adjudication, comparator, accounting | 2026-09-22 |
| S2 | `…/PRODUCTION_DECISION.md` | Frozen-contract decision JSON (`inconclusive`), held-out critical retention | 2026-09-21 |
| S3 | `…/EVALUATION_REPORT.md` | Generation 1 + 2 held-out results, final matrices, noise-floor section, campaign totals | 2026-09-21/22 |
| S4 | `…/COMPREHENSIVE_FINDINGS_REPORT.md` (CFR) | Handoff report: H1–H6 verdicts, results matrix §4, ledger §6, decision framework §8 | 2026-09-21 |
| S5 | `…/INDEPENDENT_JEV_ASSESSMENT.md` (IJA) | Independent read-only audit: treatment defects, recomputed cost/latency, scoring coverage, ledger reconciliation | 2026-09-21 |
| S6 | `…/ARCHITECTURE_RESEARCH.md` (AR) | Architectural assessment of the runtime seams (`fetch_pr_context`, tools, IAR, `Finding`, summary) | 2026-09-20 |
| S7 | `…/BASELINE.md`, `…/CALIBRATION.md`, `…/PILOT_REPORT.md`, `…/EXPERIMENT_CONTRACT.md` | Baseline timing caveats, calibration floor, pilot token figure, F-rules (unknown usage ≠ zero; per-lane publication) | 2026-09-20/21 |
| S8 | `tests/eval/BENCHMARK-xai-2026-09-16.md` | xAI model benchmark on the labelled corpus (in-process + CLI spot checks) | 2026-09-16 |
| S9 | `scripts/reviewer.py` at `38c6144` (v2.5.1) | Runtime facts (tool surface, caps, IAR behavior, `Finding` shape) | 2026-09-23 |
| S10 | GitHub PR #57 (`feat/jev-review-acceleration`) and PR #58 (`feat/aws_bedrock`), `self-review.yml` runs | Dogfood telemetry (runs, review threads) — re-queried via `gh api` on 2026-09-23 | 2026-09-16 → 2026-09-22 |
| S11 | `.dwp/plans/PLAN_jev_review_acceleration/analysis_results/runs/{shadow,holdout,pilot}/*.json`  + `holdout-gen2.json` | 176 run records loaded by Task 2 (156 complete, 2 failed, 20 without a provider label — holdout triage records, 20 flagged defective treatments per X-03); re-aggregated into `.dwp/plans/PLAN_v3_discovery/analysis_results/metrics/baseline.json` by `baseline_metrics.py` | 2026-09-20/22 |

## Verified facts

### Campaign results (grok-4.5 through the Grok CLI; 7 paired PRs; 5 labelled defects in PRs #46, #45, #43, #37)

| Id | Claim | Value | Source |
|---|---|---|---|
| E-01 | Baseline arm: avg cost per PR / findings / recall | $0.470 / 18 / 3 of 5 | S4 §4.1; S5 §4 (recomputed mean $0.4702) |
| E-02 | Jev-priorities arm | $0.516 (+10 %) / 20 / 4 of 5 | S4 §4.1; S5 §4 ($0.5161) |
| E-03 | Rules-priorities arm (free, deterministic comparator) | $0.440 (−6 %) / 18 / 4 of 5 | S4 §4.1; S5 §4 ($0.4399) |
| E-04 | Focused arm | $0.654 (+39 %) / 21 / 4 of 5 — but S5 §2 shows the saved focused extensions are byte-identical to the Jev extensions, so the arm did not implement a distinct treatment | S4 §4.1; S5 §2 |
| E-05 | grok-4.3 plain / grok-4.3 "+ jev" | $0.162 / 2 findings / 0 of 5 — $0.108 / 0 findings / 0 of 5 (S5 §2: the `43-jev` arm actually rendered the rules plan) | S4 §4.1; S5 §2 |
| E-06 | Complementarity: #46 defect found only by the jev arm, #37 defect only by the rules arm; union jev ∪ rules | 5 of 5 | S4 §4.3 |
| E-07 | Repeated (rep1) grok arms: baseline / jev / rules recall and cost | 3/5 at $0.5699 · 4/5 at $0.6739 · 1/5 at $0.6748 | S5 §4 |

### Other lanes (same coding model unless stated)

| Id | Claim | Value | Source |
|---|---|---|---|
| E-08 | GLM lane (claude-code runner + Z.ai): all four arms | 3 of 5 at $1.63 / $1.67 / $1.79 / $1.91 list price (real bill ≈ 0 on the flat-rate plan) | S4 §4.2; S1 |
| E-09 | Anthropic in-process lane (claude-sonnet-5), 16 of 21 runs | 2 findings total; 1/2, 0/2, 1/2 recall by arm at $0.91–$1.17 per PR, up to $2 per PR — "REJECTED as a configuration" | S4 §3, §4.2; S3 "ANTHROPIC in-process lane"; S1 |
| E-10 | openai-runner on xAI (in-process), 21 runs | 0/5, 0/5, 1/5 by arm — below every CLI lane | S4 §4.2 |
| E-11 | xAI benchmark, in-process runner, 4 PRs each: grok-4.5 / grok-4.6 / grok-build-0.1 / grok-4.3 must-find recall | 3/5 · 3/5 · 1/5 · 0/5; grok-4.3 averaged 2.2 turns and 11 s per review | S8 aggregate table |
| E-12 | xAI benchmark, Grok CLI, grok-4.6 (Task 8 run) | 4 of 5 at $0.47–0.85 per PR | S8 CLI spot checks |
| E-13 | The PR #37 defect (a contradiction between two documentation paragraphs) was missed by every in-process xAI model in S8 and by the baseline and jev arms in S4 | "defeats the whole family" | S8 Reading; S4 §4.3 |

### Variance and measurement

| Id | Claim | Value | Source |
|---|---|---|---|
| E-14 | Cost variance between identical runs (rep0 vs rep1) | ±$0.210 average = ±45 % of the $0.47 mean; extremes $0.42→$0.81 (pr46/baseline), $1.21→$2.26 (pr39/jev) | S4 §4.4; S3 "THE NOISE FLOOR FINDING" |
| E-15 | Recall swing between identical runs | pr45/rules (grok CLI): 2/2 → 0/2 | S4 §4.4 |
| E-16 | Every treatment effect measured (+1 recall, ±10 % cost) lies inside the E-14/E-15 band | stated by the campaign itself | S4 §2 (4), §4.4 |
| E-17 | The independent audit's caveat: a mean absolute difference expressed as ± % "is not automatically a standard deviation"; two repetitions do not establish a stable variance estimate | — | S5 §8 |
| E-18 | Scoring basis of the live campaign: 5 must-find labels, all `warning`; **zero live critical must-find labels**; 7 warning labels in later cases C071/C073/C074 are not loaded by the live scorer | — | S5 §5, §7; S6 (0 critical must-find) |
| E-19 | Scorer method: path/line/keyword matching; unlabelled findings are neither proven true nor false positives; no false-positive **rate** was measured anywhere in the campaign | — | S5 §7 |
| E-20 | Blinded adjudication of the corpus' critical claims | 23 claims → 18 confirmed, 4 downgraded (real defects, overstated severity), 1 rejected (C017) | S4 §5; S1 |
| E-21 | Critical retention by the frozen triage policy (fixture holdout) | 9 criticals → 7 committed + 2 abstentions, 0 lost; S5 §5: this is routing retention, not end-to-end detection | S2; S3 gen-2 addendum; S5 §5 |
| E-22 | Provider-stage seconds recomputed from the saved records, original arms: baseline / jev / rules mean (median) | 208.07 (195.7) · 211.53 (192.1) · 196.63 (219.5) | S5 §4 |
| E-23 | Pilot token figure (the only token count in the campaign): Jev triage | 12 decisions, 6 697 input tokens, $0.000281 | S7 PILOT_REPORT |
| E-24 | Campaign spend: summing all completed shadow result files with numeric cost | ≈ $110.99 (includes repeats and GLM list-price accounting; not an invoice) | S5 §9 |
| E-24a | Campaign spend re-aggregated by Task 2 over every complete record with cost (shadow + pilot + anthropic lane, repeats included, GLM list price) | $118.24 over 150 records (6 complete records carry no cost); GLM $49.02 / 28 runs and openai-on-xAI $11.57 / 21 runs match S5 §9 exactly | S11 via `analysis_results/metrics/baseline.json` → `campaign_cost` |
| E-14a | Noise floor re-computed by Task 2 as relative spread `(max−min)/mean` per replicated cell | 27 replicated cells; median 0.266, mean 0.282, worst 0.888 (`grok/grok-4.5/rules/pr39`: $0.749 → $1.944); definition differs from E-14's mean-absolute ± % | S11 via `baseline.json` → `noise_floor` |
| E-15a | Recall delta between repetitions re-computed by Task 2 | max 2 defects; 3 of 27 replicated cells moved | S11 via `baseline.json` → `noise_floor.recall_delta_max` |
| E-35 | Cross-run duplication of finding anchors (path + line ±3) within the same PR across all arms, lanes and repetitions — an upper-bound proxy for R-02 | 0.882 over 296 findings (per PR: 0.58–1.00) | S11 via `baseline.json` → `duplication` |
| E-36 | Anthropic in-process lane per-run profile from the records: turns hit the 30-turn cap on PRs #25 and #45 with zero findings | 30 turns / 0 findings / $2.036 (pr25), 30 / 0 / $1.847 (pr45) | S11 via `baseline.json` → `cells` (`anthropic|claude-sonnet-5|baseline|25`, `…|45`) |

### Runtime facts (read from `scripts/reviewer.py` at `38c6144`)

| Id | Claim | Value | Source |
|---|---|---|---|
| E-25 | In-process tool surface | `read_file`, `grep`, `glob`, `post_inline_comment`, `submit_review`, `set_pr_description`, `set_pr_complexity`, `update_prior_finding` — no diff/patch retrieval tool | S9 tool definitions block; S6 §5 |
| E-26 | Diff cap embedded in the prompt | `MAX_DIFF_CHARS = 200_000`; the truncated diff is re-billed on every turn (`drive_review` docstring) | S9 |
| E-27 | Turn cap | `DEFAULT_MAX_TURNS = 30` | S9 |
| E-28 | No verification step exists: the only occurrences of "verify" in the runtime are tool descriptions and prompt text asking the model to verify | grep over S9 | S9 |
| E-29 | `Finding` carries `path`, `line`, `body`, `severity`, `start_line`, `side`, `fingerprint` — no evidence bundle, no verification state | S9 `class Finding`; S6 §7 | S9; S6 |
| E-30 | Incremental IAR never resolves a prior finding by absence: "Their absence from this run's new comments must not clear the gate — except for findings `reconcile_prior_findings` retires" | S9 `run_iar_post_llm` | S9 |
| E-31 | `fetch_pr_context` discards fields such as previous filename and exposes no typed completeness flag; omitted files are reported in a log line and `PRContext.omitted_files` | S6 §4; S9 | S6; S9 |
| E-32 | The free-form summary can repeat a finding that a filter removed — no structured summary generation exists | S6 §7 | S6 |

### Dogfood telemetry re-queried from GitHub (2026-09-23)

| Id | Claim | Value | Source |
|---|---|---|---|
| E-33 | PR #57: `self-review.yml` runs on its branch / review threads / reviews | 16 / 134 / 62 | S10 (`gh api` workflow runs by branch; GraphQL `reviewThreads.totalCount`, `reviews.totalCount`) |
| E-34 | PR #58: same | 13 / 114 / 46 | S10 |

## Recorded telemetry

Session observations from the maintainer's PR #57 / #58 dogfood rounds
(2026-09-16 → 2026-09-22). They carry identifiers but no artifact in the
repository; Task 2 and RFC-01 state how each is re-measured.

| Id | Claim | Value | Identifier |
|---|---|---|---|
| R-01 | Total tokens consumed by the review legs across all rounds | ≈ 51 M (PR #57), ≈ 30 M (PR #58) | usage footers of the posted reviews, summed by hand |
| R-02 | Share of review threads that repeated a finding already posted by another leg on the same round | 70–80 % | manual triage of the 134 + 114 threads |
| R-03 | Share of `critical` labels judged false or overstated on triage | ≈ 30 % | manual triage; consistent in direction with E-20 (5 of 23 not confirmed as critical) |
| R-04 | Anthropic in-process leg per round, regardless of delta size | ≈ 12 turns, ≈ 1.15 M tokens | usage footers, PR #57 rounds |
| R-05 | `claude-code` legs hitting the workflow timeout | 900 s | `self-review.yml` run logs, PR #57 |

## Hypotheses

| Id | Statement | What would confirm it (RFC-01 instrument) |
|---|---|---|
| H-01 | A verification pass with code access at the anchor removes most false or overstated `critical` labels (R-03, E-20) without losing verified ones | Precision of `critical` under source-grounded adjudication on the RFC-01 critical corpus, replicated cells |
| H-02 | Tool parity (diff/patch retrieval, change inventory, instruction-file reading) explains most of the CLI vs in-process gap (E-09, E-10, E-11 vs E-12) | In-process recall on the pinned corpus reaching the CLI band after RFC-02, same model, replicated |
| H-03 | Consolidating N legs into one review preserves the union recall (E-06) while removing the duplication (R-02) | Duplicate share by dedup key on the dogfood PR set; union recall on the corpus |
| H-04 | Budgets scaled to the delta cut incremental-round cost by more than the noise floor (E-14) relative to R-04 | Tokens per incremental round on multi-round fixtures, replicated |
| H-05 | A mandatory instruction-file check catches the PR #37 class (E-13) | Recall on the instruction-file-contradiction corpus cases |
| H-06 | Economy-class models are viable for the verifier role even though they do not review (E-05, E-11) | Verifier precision/recall on adjudicated findings with an economy model |

## Contradictions in the record

Both sides are quoted with their pointer. This ledger does not adjudicate.

| Id | Topic | Side A | Side B |
|---|---|---|---|
| X-01 | Latency effect of the jev arm | CFR §1 (H4) and §8: "−28 % recorded on timed jev-arm runs" (S4) | IJA §4: provider-stage mean +1.7 %, median −1.8 %; "Neither aggregate is a 28 % improvement"; timings exclude setup and the Jev prepass (S5; E-22) |
| X-02 | Campaign cost total | CFR §3 "≈ $46"; CFR §6 "≈ $52 of the $100 cap" (S4) | IJA §9: GLM records sum to ≈ $49.02 (report: $11.44); xAI-API records ≈ $11.57 (report: $6.58); all shadow files ≈ $110.99 (S5; E-24). Task 2's re-aggregation confirms the IJA per-lane sums exactly and totals $118.24 once pilot and anthropic records are included (E-24a) |
| X-03 | Whether the focused and 43-jev arms tested their named treatments | CFR §4.1 treats them as distinct arms and calls the cheap-model hypothesis "REFUTED" (S4) | IJA §2: focused extensions byte-identical to jev; `43-jev` rendered the rules plan; "not fairly tested by the named arm" (S5; E-04, E-05) |
| X-04 | Delivery of the Jev priorities | CFR §4.1 reports jev-priorities as the treatment (S4) | IJA §1: Noul probabilities booleanised — all 37 file entries render as HIGH; per-file prioritization was not delivered (S5) |
| X-05 | Fast-pass zero eligibility | CFR: "Fast-pass floor admitted nothing anywhere" as a policy outcome (S3, S4) | IJA §3: an unconditional missing-confidence veto in `policy.map_batch` explains the zero, not the threshold (S5) |
| X-06 | Verdict wording | CFR §8: "INCONCLUSIVE — production stays halted"; PRODUCTION_DECISION: `"decision": "inconclusive"` (S4, S2) | IJA: "Do not promote … a clear **no now**"; LEARNINGS: "NO-GO for Jev (developer, 2026-09-22)" (S5, S1) |
| X-07 | Meaning of "zero critical losses" | CFR H2: "the safety precondition … CONFIRMED" (S4) | IJA §5: routing retention only; the live scorer has no critical must-find label (S5; E-18, E-21) |
| X-08 | Whether determinism is the prerequisite | CFR §2, §9(a): "No model comparison is interpretable on this pipeline until it is stabilized" (S4) | IJA §8: determinism "cannot guarantee that the next comparison will be interpretable"; intervention correctness, provenance and scoring coverage are more urgent (S5) |

## Structural problems

Each problem names the rows that support it and the RFC that addresses it.

| Id | Problem | Evidence | Addressed by |
|---|---|---|---|
| **P-1** | **Noise floor above every treatment effect.** Run-to-run variance of identical configurations exceeds any improvement measured; no quality claim about a v2.x release is defensible today, and the harness measures no precision | E-14, E-14a, E-15, E-15a, E-16, E-17, E-18, E-19, X-01, X-02 | RFC-01 (eval gate: replication, source-grounded adjudication, provenance, thresholds from Task 2) |
| **P-2** | **Runner architecture dominates model choice.** Same models: CLI lanes 3–4/5, in-process lanes 0–1/5; the in-process loop lacks diff retrieval, a change inventory with completeness, and instruction-file reading | E-09, E-10, E-11, E-12, E-25, E-26, E-31, H-02 | RFC-02 (unified runner), RFC-03 (instruction-file awareness) |
| **P-3** | **The ensemble is paid for and wasted.** Several legs post independent reviews; most threads duplicate another leg's finding while the complementarity that reached 5/5 is never surfaced as agreement | E-06, E-33, E-34, E-35, R-02, H-03 | RFC-04 (consolidation), RFC-05 (structured output the aggregator consumes) |
| **P-4** | **No verification exists.** Severity is the model's unchecked claim; `Finding` has no evidence; blinded adjudication and triage both show a material share of `critical` labels that do not hold; the summary can contradict the filtered findings | E-20, E-28, E-29, E-32, R-03, H-01 | RFC-03 (verification pass, evidence bundle, structured summary) |

Secondary problems:

| Id | Problem | Evidence | Addressed by |
|---|---|---|---|
| P-5 | IAR incremental mode is not budget-incremental: the delta shrinks but turns, tier and tokens do not; the in-process lane also burns the full 30-turn cap producing nothing | R-04, E-26, E-27, E-36, H-04 | RFC-06 |
| P-6 | Cheap models do not review; "cheaper model everywhere" is not a cost lever | E-05, E-11, H-06 | RFC-06 (budgets by risk, economy only for verifier/docs tiers) |
| P-7 | No immutable per-run provenance; usage sometimes unknown and at risk of being counted as zero; ledgers disagree | X-02, X-03, E-24, S7 EXPERIMENT_CONTRACT F-rules | RFC-01 (run record) |
| P-8 | The corpus cannot measure critical precision: zero live critical must-find labels; no cross-file, instruction-file, missing-patch or multi-round IAR cases scored live | E-18, E-13 | RFC-01 (corpus gaps) |
| P-9 | Finding retirement rests on the model's say-so (`update_prior_finding`) rather than on evidence that the anchor changed; the code already refuses resolution-by-absence, which the v3 design must preserve | E-29, E-30 | RFC-03 (evidence-based retirement) |
| P-10 | PR context loses metadata (renames) and exposes no completeness flag; PR metadata is untrusted input but nothing marks it so in the output | E-31; S6 §8 | RFC-02, RFC-05 |
| P-11 | Long-running CLI legs hit the workflow timeout with no partial result | R-05 | RFC-04 (failure modes), RFC-06 (budgets) |

## Cited symbols

- `read_file`
- `grep`
- `glob`
- `post_inline_comment`
- `submit_review`
- `set_pr_description`
- `set_pr_complexity`
- `update_prior_finding`
- `MAX_DIFF_CHARS`
- `DEFAULT_MAX_TURNS`
- `drive_review`
- `class Finding`
- `run_iar_post_llm`
- `reconcile_prior_findings`
- `fetch_pr_context`
- `omitted_files`
