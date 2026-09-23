# RFC-04 — Ensemble consolidation: one published review per pull request

## Status

Draft — discovery record (PLAN_v3_discovery, Task 6, 2026-09-23). Addresses
RFC-00 **P-3**, **P-11**. Depends on RFC-03 (evidence anchor and agreement
fields), RFC-02 (leg outputs), RFC-01 (run records per leg). Consumed by
RFC-05 (the artifact the aggregator reads), RFC-07 (new inputs, workflow
shape for matrix consumers), RFC-08 (dogfood needs ≥ 3 legs to observe it).

## Problem

The dogfood workflow runs one review leg per configured provider secret and
**each leg publishes its own review**: its own collapse pass, tracking
comment, review body, inline comments and `self-reviewed:<provider>` label
(`docs/PR_REVIEW_WORKFLOW.md`). On PR #57 that produced 134 review threads
over 16 runs and on PR #58 114 over 13 (RFC-00 E-33, E-34); the maintainer
judged 70–80 % of threads to repeat a finding another leg had already
posted (R-02), and the offline re-aggregation of the Jev records puts the
cross-run anchor duplication at 0.882 (E-35, upper bound). Meanwhile the one
signal an ensemble produces — **agreement** — is never computed: the two
strategies that together reached 5/5 labelled defects (E-06) were only ever
seen as two separate reviews. The consumer pays N reviews and receives N
copies of the same finding with no indication of which findings every model
saw.

## Evidence

| RFC-00 row | What it establishes for this RFC |
|---|---|
| E-33, E-34 | 134 and 114 threads on two PRs; 62 and 46 reviews |
| R-02 | 70–80 % same-round cross-leg duplication (recorded) |
| E-35 | 0.882 cross-run anchor duplication with a ±3-line window (verified upper bound; per PR 0.58–1.00) |
| E-06 | jev ∪ rules = 5/5 while each alone reached 4/5 — complementarity exists and is discarded |
| R-05, P-11 | A leg that times out publishes nothing; the others publish anyway |
| E-13 | Whole families share a blind spot: agreement is a signal, not proof (verification stays RFC-03's job) |
| S5 §7 | "the relevant equal-budget comparator includes repeated baseline reviews" — agreement across *different* lanes must be distinguished from repetitions of one lane |

Runtime facts read for this RFC (`scripts/reviewer.py` at `280eb6d`):
`REVIEW_MARKER` and `provider_marker` scope every bot artefact;
`gh_collapse_previous_reviews` minimizes prior artefacts of the same login
within the provider scope via GraphQL `minimizeComment` (`OUTDATED`);
`gh_submit_review_with_fallback` posts summary + inline comments atomically
and salvages anchorable comments on HTTP 422 using `parse_diff_hunk_ranges`
/ `inline_comment_anchor_status`; `read_prior_iteration_state` and
`embed_iteration_state` keep the IAR state in the tracking comment;
`MAX_HTTP_BODY_BYTES` bounds request bodies. The self-review workflow is
`scope` → matrix `self-review` (`fail-fast: false`, `uses: ./`) → `gate`
(`always()`, requires at least one passed leg). No artifacts are used today.

## Design

Split the action into **two roles** selected by a new `mode` input:

- `mode: review` (default, unchanged behavior for a single leg): run the
  review and publish it — exactly today's flow.
- `mode: emit` : run the review, **do not publish**; write the RFC-05
  structured output (findings v3 + run record) to the workspace and upload
  it as a workflow artifact named `ai-diff-reviewer/<head_sha>/<leg_id>.json`.
- `mode: aggregate` : download every `ai-diff-reviewer/<head_sha>/*` artifact
  for the PR head, consolidate, run the RFC-03 verifier **once** over the
  consolidated set, compute the gate, and publish **one** review with one
  tracking comment, one marker, one label.

A consumer with one provider changes nothing. A consumer with a matrix adds
`mode: emit` to each leg and one `mode: aggregate` job after them. The
dogfood workflow is the reference implementation.

## Topology

```
scope ──► self-review (matrix, fail-fast: false, mode: emit) ──► aggregate (mode: aggregate) ──► gate
                 │  uploads ai-diff-reviewer/<sha>/<leg>.json          │  one review · one marker · one label
                 │  no PR writes at all                                 │  verifier runs here (RFC-03 Q-11)
                 └─ a leg that fails/times out uploads a partial or nothing ┘  publishes with n−1 and a visible note
```

Composite-action-only shape (no matrix): a single `uses:` step with
`mode: review` — byte-comparable to v2 output on the corpus (acceptance
item 5). The matrix shape needs the aggregate job to have
`pull-requests: write`; the emit legs need only `contents: read` plus the
provider secret — a **smaller** write surface per leg than today, which
`docs/SECURITY.md` records as the security argument for the split (a
prompt-injected leg can no longer post anything).

**Leg identity.** `leg_id = <provider>|<endpoint_kind>|<model>` (from the run
record). Two repetitions of the same leg (RFC-01 campaigns) carry the same
`leg_id` and count as **one** reporter for agreement (S5 §7).

**Which artifacts belong to this head.** Artifacts are keyed by `head_sha`;
the aggregator ignores any other SHA (a superseded push) and records which
legs were expected (from the matrix, passed as an input list) and which
delivered.

## Deduplication

Two findings are the **same finding** when their anchors match; text is a
tie-breaker, never the primary key.

| Step | Rule | Rationale |
|---|---|---|
| 1. Path | identical repository path after normalisation (`previous_path` from the inventory maps renames) | — |
| 2. Line window | `|line_a − line_b| ≤ 3` (same window as Task 2's proxy, E-35), or overlapping `start_line..line` ranges | ±3 absorbs models anchoring on the statement vs the block opener; the proxy shows almost all duplicates fall inside it (per-PR 0.58–1.00) |
| 3. Anchor content | `evidence.anchor_sha256` equal **or** either finding's anchor context contains the other's anchor line | RFC-03's content hash is stable across lanes for the same code; the containment clause handles the ±3 offset |
| 4. Text similarity (tie-break only when 1–2 match and 3 does not) | `difflib.SequenceMatcher(None, title_a, title_b).ratio() ≥ 0.6` **or** token-Jaccard of `title + first 200 chars of body` ≥ 0.4 (stdlib only) | catches the same defect described from different angles; the thresholds are RFC-01 calibration targets, not constants of nature |
| 5. Severity | never part of the key; resolved in the next section | two legs may disagree on severity for the same defect |

Findings **without a line** (file-level) key on path + category + text
similarity. Near-duplicates that fail step 3 and step 4 stay separate
findings — the aggregator prefers a duplicate pair over a false merge, and
RFC-01 measures the residual duplicate share against the 0.882 baseline.

The IAR fingerprint (`finding_fingerprint`) is computed by the aggregator on
the **consolidated** finding (path, line of the richest report, published
severity, body prefix, code context), so IAR state stays one set per PR.

## Agreement and severity resolution

- `agreement = {legs_total, legs_reporting, reported_by[]}` on every
  consolidated finding (RFC-03 field). `legs_total` counts legs that
  **delivered** a complete artifact; a timed-out leg is excluded from the
  denominator and named in the summary.
- **Severity** = the maximum `severity_claimed` across reporters, then
  passed through the RFC-03 verifier and severity policy: a `critical`
  stands only if **verified**, regardless of how many legs claimed it
  (E-13: agreement is not evidence). `severity_claimed` records the maximum
  claim; `reported_by` records who claimed what.
- **Body** = the richest report by RFC-03 evidence (most `checks` with
  `supports`, then longest bounded body); the others are linked in a short
  "also reported by `<leg>`" line. Suggestions: keep the one from the chosen
  body; alternatives go to the structured output only.
- **Category** = the chosen body's category; disagreement is recorded in the
  structured output, not in the thread.

## Gating policy

Strictness modes keep their meaning over the **consolidated, verified** set
(`compute_check_gate` on published severities — RFC-03 `BC-07`). Two optional
knobs, both default-off so v2 semantics hold:

| Input | Default | Effect |
|---|---|---|
| `min-agreement` | `1` | a `warning` gates under `block-on-warning`/`block-on-any` only when `legs_reporting ≥ min-agreement`; criticals ignore it (verified critical from one leg still gates) |
| `require-all-legs` | `false` | when `true`, a missing or timed-out leg fails the gate (for consumers who treat every lane as required); default publishes with n−1 and a note |

Recommendation: ship `min-agreement: 1` as the default (today's semantics),
document `2` as the noise-reduction setting for ≥ 3-leg matrices once RFC-01
shows what it costs in recall.

## Publishing

- **One review** per head: summary generated by RFC-03's structured summary
  from the consolidated array (counts, verified/downgraded/refuted, agreement
  histogram, legs delivered/expected, gate statement, table, bounded
  narrative from the leg with the richest summary, refuted section, prior
  findings).
- **One tracking comment** carrying `REVIEW_MARKER` and a new
  `<!-- ai-pr-reviewer-aggregate -->` marker in place of a `provider_marker`;
  the IAR state (`embed_iteration_state`) is embedded there and read back by
  the aggregator's `read_prior_iteration_state` on the next round — one IAR
  history per PR instead of one per leg.
- **`collapse-previous`** runs in the aggregator, scoped to the aggregate
  marker; on the first aggregated round it also minimizes any surviving
  per-leg artefacts from the v2 shape (migration note in RFC-07).
- **One label**: `applied-label` as today (e.g. `ai-reviewed`); per-leg
  provenance lives in the structured output and the check-run summary, not
  in labels — `self-reviewed:<provider>` labels are retired in the dogfood
  workflow (RFC-07 `BC-10`, dogfood-only).
- **Check run / job summary** (`$GITHUB_STEP_SUMMARY`): legs expected vs
  delivered, per-leg cost and turns from the run records, agreement
  histogram, duplicate share removed.
- **Inline comments**: the consolidated set through
  `gh_submit_review_with_fallback` unchanged (422 salvage kept — DON'T #8);
  the aggregator uses the head diff from its own checkout for
  `parse_diff_hunk_ranges`.

## Failure modes

| Failure | Behavior | Why |
|---|---|---|
| A leg times out or errors | its artifact is absent or `status: timeout` with partial findings (RFC-02); aggregator publishes with the delivered legs, lists the missing leg in the summary and job summary, `legs_total` excludes it; gate unaffected unless `require-all-legs` | one slow lane must not erase the others' work (R-05) |
| Aggregator fails after download | no review is posted; the check fails with the error; artifacts remain for a manual re-run of the aggregate job (`workflow_dispatch` with `head_sha`) | falling back to per-leg posting would reintroduce P-3 on exactly the runs that most need care; a red check is honest |
| Artifact for a superseded SHA | ignored by key | stale reviews never post |
| Artifact too large | producers cap the document (RFC-05 truncation policy with a `truncated` flag); aggregator refuses a document above the cap and treats the leg as failed | bounded request bodies (`MAX_HTTP_BODY_BYTES` class) |
| Two artifacts for one leg (re-run) | newest `recorded_at` wins; the other is recorded as superseded | idempotent re-runs |
| No artifacts at all | aggregator posts a tracking comment "no review legs delivered" and fails the check when the matrix expected ≥ 1 leg | never a silent green |
| Consolidation bug | the aggregator validates every input against the RFC-05 schema and every output finding against RFC-03 before posting; a validation failure is a hard error | schemas are the contract |
| Artifact retention | GitHub default (90 days) is sufficient; verdicts and run records for campaigns are kept per RFC-01 Q-01 | — |

## Alternatives considered

| Alternative | Why not |
|---|---|
| Keep per-leg posting, filter duplicates client-side (in `apply-review`) | The PR thread stays noisy for humans; agreement is never computed; the gate still sees N independent verdicts |
| Single strongest leg only (IJA's "one strong runner initially") | Right for a *measurement* campaign; as the product default it throws away the 5/5 complementarity (E-06) and the cross-family blind-spot coverage (E-13) |
| Voting without verification (critical if ≥ 2 legs claim) | E-13: shared blind spots and shared hallucinations; RFC-03's verifier is the evidence step, agreement is telemetry |
| Aggregate inside the last matrix leg (no extra job) | Matrix legs do not know they are last; a separate job with `needs:` is the only reliable barrier |
| Publish per-leg reviews **and** an aggregate | Doubles the thread count this RFC exists to cut |

## Impact on the public contract

- **Additive inputs:** `mode` (`review` default / `emit` / `aggregate`),
  `expected-legs` (list, aggregate only), `min-agreement` (default `1`),
  `require-all-legs` (default `false`). **Additive outputs:** `legs-expected`,
  `legs-delivered`, `duplicates-removed`, `agreement-histogram` (RFC-05
  carries the full detail; scalar outputs stay minimal). Ledger rows
  `BC-09` (mode + aggregate inputs/outputs, additive).
- **Behavioral for matrix consumers who opt in:** one review, one marker,
  one label; IAR state moves to the aggregate marker — a migration note for
  consumers with existing per-leg IAR history (`BC-09` note).
- **Dogfood-only breaking:** `self-reviewed:<provider>` labels retired in
  `self-review.yml` (`BC-10`).
- **Security posture:** emit legs need no `pull-requests: write` — recorded
  in `docs/SECURITY.md`.

## Open questions

| Id | Question | Recommendation |
|---|---|---|
| Q-16 | Line window 3 vs 5 | 3 (matches the Task 2 proxy); RFC-01 calibrates against residual duplicate share |
| Q-17 | Text-similarity thresholds (0.6 ratio / 0.4 Jaccard) | Start there; calibrate on the dogfood PR set (E-33/E-34 threads are labelled duplicates by the maintainer's triage) |
| Q-18 | Should the aggregate job be a separate action entry point (`aggregate/action.yml`) or the same action with `mode`? | Same action, `mode` input — one contract, one version pin (`DailybotHQ/ai-diff-reviewer@v3`) |
| Q-19 | Default when a matrix consumer forgets the aggregate job | Emit legs post a tracking comment "artifact uploaded; add an aggregate job" and exit 0 — never silently review nothing |
| Q-20 | Per-leg tracking comments during the run (progress visibility) | None; the aggregate tracking comment shows "waiting for legs (k/n)" via the job summary only — no extra PR writes |

## Acceptance for the implementation plan

The Phase 2 consolidation DWP is complete when:

1. `mode: emit` uploads a schema-valid RFC-05 document per leg and performs
   **no** PR write (tested by a fake GitHub client asserting zero mutations).
2. `mode: aggregate` consolidates fixtures with known duplicates: the
   duplicate share on the labelled dogfood thread set (PR #57/#58 triage)
   drops from the recorded 70–80 % to **≤ 15 %** by the dedup key, with
   **no** labelled distinct finding merged (precision of the key ≥ 0.95 on
   the fixture).
3. Agreement fields are filled; a verified critical from one leg gates;
   an unverified critical claimed by three legs publishes as an annotated
   warning (RFC-03 invariant holds through the aggregator).
4. Timeout fixture: two of three legs deliver, the review publishes with the
   missing leg named and `legs_total = 2`; `require-all-legs: true` fails
   the gate on the same fixture.
5. `mode: review` (single leg) output is byte-comparable to v2 on the pinned
   corpus except for the RFC-03 severity annotations (diffed in CI).
6. `self-review.yml` runs the reference topology on this repository with the
   two configured legs (XAI, ZAI) and the check-run summary shows legs,
   agreement and duplicates removed; a third leg is exercised with a fixture
   artifact in tests.
7. `docs/PR_REVIEW_WORKFLOW.md`, `docs/SECURITY.md` (write surface per
   role), `README.md` (matrix example), `examples/ensemble-matrix.yml` and
   `MIGRATION_v3.md` (`BC-09`, `BC-10`) are current.

## Cited symbols

- `REVIEW_MARKER`
- `provider_marker`
- `gh_collapse_previous_reviews`
- `gh_submit_review_with_fallback`
- `parse_diff_hunk_ranges`
- `inline_comment_anchor_status`
- `read_prior_iteration_state`
- `embed_iteration_state`
- `finding_fingerprint`
- `compute_check_gate`
- `MAX_HTTP_BODY_BYTES`
- `collapse-previous`
- `applied-label`
