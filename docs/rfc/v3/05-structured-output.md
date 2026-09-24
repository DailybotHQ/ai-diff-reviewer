# RFC-05 — Structured review output as the official v3 contract

## Status

Accepted — 2026-09-23 (D-00, developer instruction to PLAN_v3_implementation). Originally a discovery record (PLAN_v3_discovery, Task 7, 2026-09-23). Addresses
RFC-00 **P-3** (the aggregator's input), **P-7** (provenance travels with
the review), **P-10** (completeness is a typed field). Depends on RFC-01
(run record), RFC-02 (change inventory), RFC-03 (finding v3), RFC-04 (roles
and legs). Consumed by RFC-07 (new outputs are contract additions).
Schema: [`schemas/review-output-v3.schema.json`](schemas/review-output-v3.schema.json)
(example: [`schemas/examples/review-output-v3.example.json`](schemas/examples/review-output-v3.example.json)).

## Problem

The action's machine-readable surface is eleven scalar outputs written
through `write_action_output` / `write_all_outputs` (`review-url`,
`severity`, `inline-attached`, `inline-dropped`, `blocked`, `skipped`, five
`iteration-*` values) plus, for the agent-runner family only, the private
`.aiprr/findings.json` file the CLI writes and `parse_findings_file` reads.
Nothing carries the review itself as data: the aggregator (RFC-04) has no
input, the eval gate (RFC-01) has to re-derive a run record from logs, the
`apply-review` sub-skill scrapes GitHub review threads with GraphQL and
filters `isMinimized` comments (`skills/ai-diff-reviewer/apply-review/SKILL.md`),
and a consumer who wants "the findings as JSON" has no supported path.
Provenance (RFC-01), evidence and verification (RFC-03), agreement (RFC-04)
and completeness (RFC-02) all need a place to live that is not a comment
body.

## Evidence

| RFC-00 row | What it establishes for this RFC |
|---|---|
| E-25, S6 §5 | The in-process family has no findings document at all; only the agent-runner family writes a file |
| E-31, P-10 | Completeness is a log line today; consumers cannot tell a partial review from a full one |
| E-32 | The posted summary can contradict the filtered findings — the posted body must be *generated from* the document, not the reverse |
| E-33, E-34, R-02 | Consumers (and this repo's own `apply-review`) read reviews by scraping threads and filtering minimized comments — the only machine path today |
| X-02, E-24a | Costs became reliable only when re-aggregated from per-run records: the record must travel with the output |
| S7 F4 | "Metadata obligations per run: complete change-inventory digest, exact revision pair, prompt/policy hashes, model ids, cache state, and usage; missing usage is `unknown cost`, never zero" |

Runtime facts read for this RFC (`scripts/reviewer.py` at `7c5be24`,
`action.yml`, `.github/scripts/validate_action.py`): `write_action_output`
appends to `$GITHUB_OUTPUT` (heredoc form for multi-line values);
`write_all_outputs` is the single exit path for the six core outputs plus
empty IAR outputs; `validate_action.py` asserts every declared output has a
`value:` expression and every input the runtime reads is declared; the
findings-file contract in `docs/PROVIDERS.md` has `summary`, `complexity`,
`findings[] {path, line, body, severity, start_line, side}`,
`prior_findings[] {fingerprint, status, note}`, unknown keys ignored,
`MAX_FINDINGS_FILE_BYTES` cap.

## Design

One document, `review-output/3.0`, produced by **every** run in every role:

- **Where it is written.** `<workspace>/.aiprr/review-output.json` (next to
  the existing findings file). In Actions it is also uploaded as the
  artifact `ai-diff-reviewer/<head_sha>/<leg_id>.json` (RFC-04) and two new
  scalar outputs point at it: `structured-output-path` and
  `structured-output-sha256`. The digest lets a downstream step verify it
  reads the document this run wrote.
- **What it contains.** The embedded RFC-01 run record (`run`), the RFC-02
  change inventory with `complete` and the RFC-06 `risk_tier`, the published
  findings (RFC-03 v3), the **refuted** findings with reasons, the prior
  findings ledger (retired / still open / regressed / unverified claims),
  the **generated** summary (counts, verification counts, agreement
  histogram, bounded narrative, and the exact Markdown that was posted),
  the gate decision with the RFC-04 knobs, the legs table and
  `duplicates_removed` for aggregated reviews, usage (nullable +
  `usage_known`), cost (nullable), the `truncated` block and, once
  published, `review_url`.
- **Direction of truth.** The posted review body **is** `summary.rendered_markdown`;
  the runtime renders Markdown from the document and posts that string, so
  the thread can never say something the document does not (E-32).
- **Versioning.** `schema_version` is a `const`; additive fields bump the
  minor (`3.1`), removals or type changes bump the major and are RFC-07
  breaking rows. Consumers must ignore unknown fields (the same
  forward-compatibility rule `parse_findings_file` already applies).
- **Bounds and safety.** The document is capped (`MAX_REVIEW_OUTPUT_BYTES`,
  new constant, sized above the findings-file cap); findings are **never**
  dropped silently — when the cap would be exceeded, excerpts are trimmed
  first (`truncated.excerpts_trimmed`), then the narrative
  (`narrative_trimmed`), and only then findings beyond the inline cap
  (`findings_dropped`, criticals first-protected by
  `_sort_findings_criticals_first`), with `truncated.any = true`. Every
  string field passes the outbound secret scrub (`scrub_secrets`,
  `register_secret`) and excerpts the `redact_for_log` discipline. No
  hostname: backends are `endpoint_kind`. The only URL-typed field is
  `review_url`, written by the runtime from GitHub's response, never by a
  model. PR title/body are **not** copied into the document (untrusted
  input, RFC-02); only SHAs, paths and counts describe the change.

## Schema

Draft 2020-12, closed at the top level and in every nested object the
runtime owns. The `run` and `findings[]` members are validated **in full**
against `run-record.schema.json` and `finding-v3.schema.json` by the
runtime; this document's schema restates only the identity keys the
aggregator and consumers key on, so the plan gate's self-contained walk
passes on the example without cross-file `$ref` resolution (the stdlib
validator has none). Enumerations: `role`, `run.status`, `findings[].severity`
and `verification.status`, `refuted[].severity_claimed`, `prior_findings.retired[].reason`,
`gate.strictness`, `legs[].status`, `change_inventory.files[].status` and
`risk_class`, `usage.source` (adds `aggregated`).

> Amendment (2026-09-24, PLAN_v3_implementation Task 17): `run.status` also admits `skipped`, matching the RFC-01 run-record amendment — a run that ended before any model call still writes its document.

Field groups and their producers:

| Group | Producer | Notes |
|---|---|---|
| `run` | runtime (`main`) / aggregator | the RFC-01 record; `runner: aggregator` for consolidated documents |
| `change_inventory` | `get_change_inventory` (RFC-02) + RFC-06 classifier | `complete` false when anything was omitted or oversized; `risk_tier` from RFC-06 |
| `findings[]` | reviewing model → verifier → aggregator | finding v3; `agreement` null on single-leg |
| `refuted[]` | verifier | audit trail for suppressed claims (RFC-03) |
| `prior_findings` | IAR (`reconcile_prior_findings` + RFC-03 anchor re-read) | fingerprints, not bodies |
| `summary` | runtime renderer | `rendered_markdown` is the posted body |
| `gate` | `compute_check_gate` + RFC-04 knobs | `reason` is the human line the tracking comment shows |
| `legs[]`, `duplicates_removed` | aggregator | null / absent on single-leg |
| `usage`, `usage_known`, `cost_usd` | runtime | aggregated across legs with `source: aggregated`; unknown stays unknown |
| `truncated` | runtime | never silent |

## Relation to the agent-runner findings file

`.aiprr/findings.json` stays the **CLI → runtime** input contract; the
structured output is the **runtime → world** output contract. v3 extends the
input file to finding v3 as a **superset** and the runtime lifts it into the
output document — CLIs are never asked to produce provenance they do not
have.

| Today (`findings.json`) | v3 input (`findings.json`, superset) | v3 output (`review-output.json`) |
|---|---|---|
| `summary` (string) | `summary` (string, becomes the bounded narrative source) | `summary.narrative` (bounded) + `summary.rendered_markdown` (generated) |
| `complexity` (`low`/`medium`/`high`) | kept, optional | `change_inventory.risk_tier` is the runtime's classification (RFC-06); the model's `complexity` is recorded in `run` telemetry only |
| `findings[].path`, `line`, `body`, `severity`, `start_line`, `side` | kept | finding v3 `path`, `line`, `body`, `severity_claimed` (→ `severity` after verification), `start_line`, `side` |
| — | `findings[].title`, `category`, `evidence.{files_read, checks, documented_rule}` (optional; the runtime fills `anchor_sha256`, `excerpt`) | finding v3 `evidence` complete |
| — | — | finding v3 `verification`, `agreement`, `lifecycle`, `origin` (runtime/verifier/aggregator only) |
| `prior_findings[] {fingerprint, status, note}` | kept | `prior_findings.{retired, still_open, regressed, unverified_claims}` by fingerprint |
| unknown keys ignored | unchanged | consumers ignore unknown keys |
| `MAX_FINDINGS_FILE_BYTES` | unchanged | `MAX_REVIEW_OUTPUT_BYTES` (new, larger) |

Deprecation path: none needed for the input file (superset, unknown keys
already ignored); `write_findings_prompt_directive` documents the optional
v3 fields so CLIs that can supply `title`/`category`/`checks` do.

## Consumers

| Consumer | Reads | Must never trust |
|---|---|---|
| Aggregator (RFC-04) | every leg's document by artifact key; `findings[]`, `run`, `change_inventory.complete`, `legs` | any document for another `head_sha`; a document failing schema validation (leg treated as failed) |
| Eval gate (RFC-01) | `run` (stored as the run record), `summary.verification_counts`, `gate`, `findings[]` for adjudication | narrative text as evidence |
| `apply-review` sub-skill | downloads the artifact for the PR head (or reads the path locally) instead of scraping threads; the marker stays for humans and as the fallback when no artifact exists | `review_url` as proof the artifact is current — it checks `run.runtime_sha`/`head_sha` |
| Downstream workflow steps | `structured-output-path` + `structured-output-sha256`, then the file | a path whose digest does not match |
| Dashboards / cost tooling | `run`, `usage`, `cost_usd`, `legs[]` | `cost_usd` when `usage_known` is false (it is null by contract) |
| Humans | `summary.rendered_markdown` in the PR | — |

PR metadata never enters the document; consumers that need the title read
GitHub. The document describes the **change** (SHAs, paths) and the
**review**, nothing the author typed.

## Alternatives considered

| Alternative | Why not |
|---|---|
| Keep scalar outputs only | No input for the aggregator or the gate; `apply-review` keeps scraping; provenance stays in logs (X-02) |
| SARIF as the primary contract | SARIF has no place for verification state, agreement, legs, usage or the change inventory without heavy `properties` bags; offer SARIF as an optional **export** derived from this document (RFC-08 D-nn), not as the source |
| Comments-as-API (structured HTML comments in the thread) | Minimized/outdated handling, size limits and injection surface make the thread a poor database; the marker already carries the small IAR state and stays for that |
| Emit only on `mode: emit` | Single-leg consumers and the eval gate need it too; a universal document is one code path |
| Let CLIs write the full v3 output directly | They cannot know provenance, verification or agreement; lifting is the runtime's job |

## Impact on the public contract

- **Additive outputs:** `structured-output-path`, `structured-output-sha256`
  (declared in `action.yml` with `value:` expressions so `validate_action.py`
  passes); the artifact `ai-diff-reviewer/<head_sha>/<leg_id>.json`.
  Ledger row `BC-11` (additive).
- **Additive input contract:** finding v3 optional fields in
  `.aiprr/findings.json` (`BC-12`, additive).
- **Behavioral:** the posted review body is generated from the document
  (already covered by RFC-03 `BC-07`'s summary change); `severity` output
  reports the **published** severity after verification (same row).
- **Breaking:** none in this RFC by itself; a future change to a `const` or
  a removed field is a major.
- **Documentation:** `docs/PROVIDERS.md` (findings-file superset),
  `docs/ARCHITECTURE.md` (output contract), `README.md` outputs table,
  `skills/ai-diff-reviewer/setup/reference.md`, `apply-review/SKILL.md`.

## Open questions

| Id | Question | Recommendation |
|---|---|---|
| Q-21 | Embed the full run record or reference it by `run_id`? | Embed — the document must be self-contained for the aggregator and for artifact consumers with no access to campaign storage |
| Q-22 | Should `rendered_markdown` be stored (duplication) or re-rendered on read? | Store it: the posted body must be reproducible byte-for-byte for audits and IAR marker matching |
| Q-23 | `MAX_REVIEW_OUTPUT_BYTES` value | 4 MB (half of `MAX_HTTP_BODY_BYTES`, above `MAX_FINDINGS_FILE_BYTES`); excerpt trimming keeps typical documents far below |
| Q-24 | Optional SARIF export | Yes, as a derived artifact behind `export-sarif: true`, Phase 2 or later; never the source of truth |
| Q-25 | Should `apply-review` require the artifact or fall back to thread scraping? | Prefer the artifact; fall back to the marker/thread path with a note, so pre-v3 reviews stay readable |

## Acceptance for the implementation plan

The Phase 1/2 output DWP is complete when:

1. `review-output-v3.schema.json` (plus the two referenced schemas) is copied
   to `tests/eval/schemas/` unchanged; every lane in every `mode` writes a
   schema-valid document on the 7 historical PRs, validated in-process
   before posting and by the offline eval job.
2. `structured-output-path` / `structured-output-sha256` are declared and
   written on every exit path (success, skip, failure) — `validate_action.py`
   passes and a test asserts the digest matches the file.
3. The posted review body equals `summary.rendered_markdown` byte-for-byte
   (test through the fake GitHub client).
4. Truncation test: a document above the cap trims excerpts, then the
   narrative, then drops findings beyond the inline cap **criticals last**,
   sets `truncated.*`, and never exceeds `MAX_REVIEW_OUTPUT_BYTES`.
5. Secret/host test: a finding body containing a registered secret and an
   `api-base` hostname produces a document with neither.
6. `parse_findings_file` accepts the v3 superset and legacy files
   unchanged; the field-mapping table is exercised by tests for both.
7. `apply-review` reads the artifact when present and falls back to the
   marker path otherwise (skill docs updated; prompt-sync invariant intact).
8. `docs/PROVIDERS.md`, `docs/ARCHITECTURE.md`, `README.md`,
   `skills/ai-diff-reviewer/setup/reference.md` are current.

## Cited symbols

- `write_action_output`
- `write_all_outputs`
- `write_iar_outputs_empty`
- `parse_findings_file`
- `MAX_FINDINGS_FILE_BYTES`
- `write_findings_prompt_directive`
- `FINDINGS_JSON_REL`
- `scrub_secrets`
- `register_secret`
- `redact_for_log`
- `_sort_findings_criticals_first`
- `compute_check_gate`
- `reconcile_prior_findings`
- `MAX_HTTP_BODY_BYTES`
- `REVIEW_MARKER`
