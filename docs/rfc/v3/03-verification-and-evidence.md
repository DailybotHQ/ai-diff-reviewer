# RFC-03 — Verification pass, Finding v3 evidence bundle, severity policy and evidence-based retirement

## Status

Draft — discovery record (PLAN_v3_discovery, Task 5, 2026-09-23). Addresses
RFC-00 **P-4**, **P-9**, and the PR #37 class (**E-13**, **H-05**). Depends
on RFC-02 (tools the verifier uses) and RFC-01 (how the verifier is
measured). Consumed by RFC-04 (dedup key and agreement live on the finding),
RFC-05 (findings array), RFC-06 (verifier budget), RFC-07 (severity
semantics change). Schema:
[`schemas/finding-v3.schema.json`](schemas/finding-v3.schema.json)
(example: [`schemas/examples/finding-v3.example.json`](schemas/examples/finding-v3.example.json)).

## Problem

Severity is the model's unchecked claim. The runtime asks the model to
"verify with the tools before flagging" (`prompts/default.md` § Verification
budget) and to record "Evidence" as an optional clause of the comment body —
then trusts whatever comes back. `Finding` carries `path`, `line`, `body`,
`severity`, `start_line`, `side`, `fingerprint` (RFC-00 E-29): no evidence,
no verification state. Blinded adjudication changed 5 of 23 critical claims
(E-20); dogfood triage judged ≈ 30 % of critical labels false or overstated
(R-03). The independent audit names the failure: any check that reads only
the finding's text is satisfied by a persuasive wrong finding (S5 §6). Two
adjacent gaps compound it: the free-form summary can name a finding the
filter removed (E-32), and the PR #37 documentation contradiction defeated a
whole model family because nothing required reading the instruction files
(E-13).

## Evidence

| RFC-00 row | What it establishes for this RFC |
|---|---|
| E-20 | 23 critical claims → 18 confirmed, 4 downgraded, 1 rejected under blinded adjudication |
| R-03 | ≈ 30 % of dogfood `critical` labels false or overstated on triage |
| E-28 | No verification step exists in the runtime; only prompt text |
| E-29 | `Finding` has no evidence bundle or verification state |
| E-30 | Incremental IAR never retires by absence — the invariant to preserve |
| E-32 | Free-form summary can contradict the filtered findings |
| E-13, H-05 | The documentation-contradiction class needs instruction-file reading |
| E-05, E-11, H-06 | Economy models do not review; whether they can *verify* is the open hypothesis |
| S5 §6 | "A persuasive incorrect finding can satisfy that question" — finding-only verification is worthless |
| S6 §7 | "do not assume the finding body is a trustworthy proof. Never fetch arbitrary model-provided URLs or unvalidated paths to assemble evidence" |

Runtime facts read for this RFC (`scripts/reviewer.py` at `b2be9b0`):
`finding_fingerprint` hashes `path|line|severity|body[:prefix]|context_hash`
of `2 × IAR_CONTEXT_HASH_RADIUS + 1` lines around the anchor;
`dedupe_findings_against_prior` carries the critical-always-surfaces rail
and `_sort_findings_criticals_first` protects it under caps;
`reconcile_prior_findings` retires a prior finding only when the model says
`resolved` **and** the runtime corroborates (fingerprint absent this round
**and** the file changed since raised or no longer exists), under
`RESOLUTION_POLICY_VERIFIED` or when the thread is already collapsed under
`RESOLUTION_POLICY_ADVISORY`. So retirement is *not* pure say-so (RFC-00
P-9 is amended in this task): the runtime proves the file changed, not that
the defect is gone. That last step is what this RFC adds.

## Design

Four additions, one invariant:

1. **Every finding carries evidence** (what was read, what was checked, the
   anchor hash and a bounded excerpt) — produced by the reviewing model
   through `emit_finding` (RFC-02) and completed by the runtime (anchor hash,
   tool-trace ids).
2. **A verifier pass** re-examines findings with **code access**, never the
   text alone, and writes a verification state.
3. **Severity policy:** `critical` is published only when verified;
   otherwise it is downgraded with a recorded reason. The rail that makes
   criticals always surface is preserved — a downgraded critical surfaces as
   a warning with its claim visible.
4. **Retirement, summary and instruction files** become evidence-driven and
   structured.

Invariant (runtime + aggregator, tested): **no published finding has
`severity = critical` and `verification.status ≠ verified`.**

## Finding v3 contract

| Field | Meaning | Producer |
|---|---|---|
| `id` | `f-` + `finding_fingerprint` (content-anchored; stable across identical runs) | runtime |
| `path`, `line`, `start_line`, `side` | anchor, as today | model |
| `severity`, `severity_claimed` | published severity; the model's original claim | policy / model |
| `category` | enum incl. `contradicts-documented-rule` (the PR #37 class) | model |
| `title`, `body`, `suggestion` | as today; `title` ≤ 120 chars for tables | model |
| `evidence.anchor_sha256` | hash of the anchor context at head (same radius as the fingerprint) | runtime |
| `evidence.excerpt` | ≤ 2 000 chars, redacted through the secret scrub; never PR metadata | runtime |
| `evidence.files_read`, `tool_trace_ids` | what the model consulted (≤ 20 / ≤ 50) | runtime from `ReviewState` trace; CLI lanes: whatever the findings file declares, else empty |
| `evidence.checks[]` | `{kind, target, result ∈ supports/contradicts/inconclusive, note}` — `kind` ∈ read_anchor, grep_callers, read_base_version, read_instruction_file, run_test, type_check, other | model, validated |
| `evidence.documented_rule` | `{file, quote}` for `contradicts-documented-rule` | model, validated against `read_instruction_files` output |
| `verification.status` | `unverified` / `verified` / `refuted` / `downgraded` / `skipped` | verifier / policy |
| `verification.reason`, `checks[]`, `verifier_model_alias`, `verifier_endpoint_kind`, `verified_at` | the verifier's own evidence | verifier |
| `agreement` | `{legs_total, legs_reporting, reported_by[]}` | aggregator (RFC-04); null single-leg |
| `lifecycle` | `new` / `open` / `retired` / `regressed` + `retired_reason` ∈ verified_fixed / maintainer_resolved / file_removed | IAR |
| `origin` | `run_id`, `provider`, `endpoint_kind`, `model` (RFC-01) | runtime |

The JSON Schema is draft 2020-12, closed at the top level and in every
nested object, with enums for every categorical field. The severity
invariant is not expressible without conditionals, so the schema states it
in `description` and the runtime enforces it (acceptance item 2).

## Verifier

**Input.** The finding **and** the code, retrieved by the verifier itself
through the RFC-02 tools on the same workspace: the anchor (`read_file` at
head, ± radius), the base version when the finding claims a regression
(`read_file` with `ref: base`), callers/definitions (`grep`), the patch
(`get_patch`), and — for `contradicts-documented-rule` — the instruction
file (`read_instruction_files`). The verifier **never** receives the
reviewing model's reasoning as authority; it receives the claim (title,
body, category, anchor) and must re-derive support from code. Paths go
through `safe_repo_path`; no URL fetching (S6 §7).

**Output.** `verification.{status, reason, checks[]}`: `verified` when at
least one `read_anchor` check supports the claim and no check contradicts
it; `refuted` when a check contradicts it (e.g. the guard the finding says
is missing exists at the anchor or in the callers); `downgraded` when the
claim stands but at a lower severity than claimed; `unverified` when checks
are inconclusive or the verifier could not run; `skipped` when policy chose
not to verify (budget).

**When it runs.** Every `critical` claim, always. `warning` claims on a
sampled basis set by RFC-06's tier budget (all of them at `elevated`/
`critical` tiers; a fixed sample at `standard`; none at `low`). `info`
never. On multi-leg runs the verifier runs **once in the aggregator** over
the consolidated set (RFC-04 Q-13) so agreement is known before verification
and duplicates are not verified twice; single-leg runs verify in-loop.

**Model.** The verifier is a second call with a **separate budget** (turns
and tokens from RFC-06) using the `economy` alias of the lane's kind by
default — H-06 is the hypothesis RFC-01 tests; if economy-class models
cannot verify to the precision bar, RFC-06's matrix moves the verifier to
`balanced` and the cost model below is re-stamped.

**Failure.** Verifier error, timeout or budget exhaustion → `unverified`
with the reason → the severity policy downgrades a claimed critical to
warning **and keeps it visible**. The verifier fails **open into
visibility**, never closed into a block and never into silence.

**Refuted findings** are not published inline. They are counted in the run
record (`outcome.findings_refuted`, RFC-01 amendment) and listed in the
structured output (RFC-05) under `refuted[]` with their reasons, so a
maintainer can audit what the verifier removed. Nothing is dropped
silently.

## Severity policy

| Model claimed | Verifier status | Published severity | Visible how |
|---|---|---|---|
| critical | verified | **critical** | inline, gates under `block-on-critical` |
| critical | downgraded | warning | inline, body opens with "Claimed critical; verifier found: …" |
| critical | unverified / skipped | warning | inline, same annotation; gates only under `block-on-warning`/`block-on-any` |
| critical | refuted | — | not inline; listed in the refuted section of the structured output |
| warning | verified / unverified / skipped | warning | inline |
| warning | downgraded | info | inline |
| warning | refuted | — | refuted section |
| info | any | info | inline (never verified) |

**Coexistence with the critical-always-surfaces rail.** The rail
(`dedupe_findings_against_prior`, `_sort_findings_criticals_first`,
`docs/ITERATION_AWARENESS.md` § 7.1) guarantees that a `critical` is never
*silenced* by dedup or caps. This policy changes the *label*, not the
visibility: a claimed critical always surfaces, as `critical` when verified
and as an annotated `warning` otherwise, and the rail is applied to
`severity_claimed` when sorting under caps so a not-yet-verified critical
claim is never truncated away before the verifier sees it. Strictness modes
(`STRICTNESS_BLOCK_CRITICAL` …) read the **published** severity through
`compute_check_gate`, so `block-on-critical` blocks only on verified
criticals — the behavior change RFC-07 records as `BC-07`.

## Finding retirement

Today (`reconcile_prior_findings`): retire when the model says `resolved`
**and** the fingerprint is absent this round **and** the file changed since
raised (or is gone), under `verified` policy or with a collapsed thread under
`advisory`. Kept as the **necessary** condition. Added **sufficient**
condition: the verifier re-reads the anchor at the new head and confirms the
defect is gone (`lifecycle.retired_reason = verified_fixed`) — a file that
changed elsewhere no longer retires a finding whose anchor is intact.
`file_removed` retires when the path is gone (as today). `maintainer_resolved`
is the human path (thread resolved), unchanged.

Invariants (tests in Phase 1):

- Absence from a later review **never** retires (E-30 preserved, both
  modes).
- A `resolved` verdict without corroboration stays `open` and is listed as
  unverified (as today).
- A corroborated `resolved` verdict whose anchor is unchanged stays `open`
  with `verification.reason = "anchor unchanged"` — this is the new refusal.
- `regressed` remains model-asserted but is now verified like a new
  finding before it can re-gate.

`update_prior_finding` keeps its tool shape; its `note` becomes the
verifier's input, not the decision.

## Structured summary

The review summary is generated by the runtime from the final findings
array, not written free-form by the model:

```
<counts by published severity> · <verified / downgraded / refuted counts> ·
<agreement histogram when multi-leg> · <gate statement from compute_check_gate>
<findings table: severity · path:line · title · verification · agreement>
<bounded narrative (model-written, ≤ N chars) that may not name a finding absent from the table>
<refuted findings section: title · reason>
<prior findings: retired (reason) · still open · regressed>
```

Invariant: every `path:line` mentioned in the narrative must match a row in
the table; the runtime strips or footnotes any that does not (E-32). The
narrative is still requested from the model (its explanation has value) but
it is **bounded** and subordinate to the table — no second model call is
added for the summary (S6 §7's caution).

## Instruction-file awareness

Before reviewing, the loop calls `read_instruction_files` (RFC-02): the
repository's agent instructions (`AGENTS.md` / `CLAUDE.md`, deduplicated by
symlink), the review extension (`.review/extension.md` and the two
back-compat paths) and the declared docs index when present. The prompt
gains a required step: *"Check the change against the documented rules;
report a contradiction as `contradicts-documented-rule` with the file and
the quoted rule as evidence."* The verifier for this category re-reads the
quoted file and confirms the quote exists and applies. The run record's
`context.instruction_files_read` proves the step ran. This converts the PR
#37 class from a model-family failure into a first-class check measured by
RFC-01's instruction-file corpus cases.

## Cost model

Inputs (RFC-00 / Task 2): grok-4.5 CLI review ≈ $0.47 per PR (E-01, n = 14),
≈ 18–21 findings per 7 PRs ⇒ ≈ 2.5–3 findings per PR, of which ≈ 0.5–1 are
claimed `critical` on the dogfood PRs (R-03 basis); economy-class verifier
call with ≤ 4 tool reads ≈ 15–30 k input tokens ⇒ **≈ $0.02–0.05 per
verified finding** at grok-4.5 list price (the same model class; an economy
alias where one exists is cheaper). Verifying every critical and a 30 %
sample of warnings on a typical PR adds **≈ $0.05–0.15 per PR (10–30 % of
the review cost)**. Against it: a false critical under `block-on-critical`
blocks a merge and costs a human triage round plus a re-review round
(≈ $0.47 + engineer time); at the recorded ≈ 30 % false-critical rate
(R-03) roughly one in three PRs with a critical pays that. Break-even
assumptions, stated: the verifier removes ≥ half of false criticals (H-01)
and the review model's finding count does not rise because it "knows" a
verifier follows (RFC-01 measures both). RFC-01's precision metric on the
critical corpus is the confirmation; the run record's `verifier_seconds`
and `budget.verifier_runs` make the cost visible per run.

## Alternatives considered

| Alternative | Why not |
|---|---|
| Single-pass "be careful" prompting (status quo) | E-20, R-03: the prompt already asks; the label is still wrong a material share of the time |
| Consensus-only verification (a critical stands if ≥ 2 legs claim it) | Legs share the same blind spots (E-13 defeated a whole family); agreement is a signal (RFC-04), not evidence; single-leg consumers get nothing |
| Human-only adjudication | Does not scale to every PR; the verifier is what makes human adjudication (RFC-01) a sample instead of a queue |
| LLM judge over the finding text | S5 §6: finding-only verification passes persuasive wrong findings |
| Verify everything including `info` | Cost with no gating value; `info` never gates |
| Auto-suppress refuted findings silently | S6 §7 and the Jev contract: suppression needs an audit trail; the refuted section is that trail |

## Impact on the public contract

- **Behavioral (breaking for gating semantics):** `block-on-critical` blocks
  on **verified** criticals only; claimed-but-unverified criticals publish as
  annotated warnings (`BC-07`). Finding retirement additionally requires
  anchor verification (`BC-08`).
- **Additive:** finding v3 fields in the findings file and structured output
  (RFC-05); refuted section; `contradicts-documented-rule` category; new
  inputs proposed to RFC-07: `verifier` (`on` / `criticals-only` / `off`,
  default `criticals-only`) and `verifier-model` alias (default `economy`).
- **Prompt:** `prompts/default.md` gains the instruction-file step and the
  evidence fields; per Rule #10 the change ships with a before/after on a
  real PR and the skill copy stays byte-identical.

## Open questions

| Id | Question | Recommendation |
|---|---|---|
| Q-11 | Verifier placement on multi-leg runs: per leg or once in the aggregator? | Once in the aggregator over the consolidated set (agreement known, no duplicate verification); single-leg verifies in-loop |
| Q-12 | Should `unverified` criticals gate under `block-on-critical` during a transition release? | No; but add an opt-in `strict-unverified-criticals: true` for consumers who prefer the v2 behavior for one minor cycle |
| Q-13 | Warning sample rate at `standard` tier | 30 %, chosen by the budget table in RFC-06; re-measured by RFC-01 |
| Q-14 | Should refuted findings be posted as collapsed/minimized comments instead of only in the structured output? | Only in the structured output and the summary's refuted section; avoid thread noise (P-3) |
| Q-15 | Default verifier model alias | `economy`; H-06 decides; fallback `balanced` |

## Acceptance for the implementation plan

The Phase 1 verification DWP is complete when:

1. `finding-v3.schema.json` is copied to `tests/eval/schemas/` unchanged; both
   families emit schema-valid findings on the 7 historical PRs.
2. A test proves the invariant: no published finding has `severity =
   critical` with `verification.status ≠ verified`, for in-process and CLI
   lanes, including verifier timeout and error paths (fail-open into an
   annotated warning).
3. The verifier's inputs are shown by test to include the anchor read from
   the workspace and never only the finding text (a fixture where the finding
   text is persuasive but the code contradicts it is refuted).
4. Retirement tests: absence never retires; corroborated `resolved` with an
   unchanged anchor stays open with the new reason; `verified_fixed` retires
   only after the anchor re-read.
5. The structured summary strips or footnotes any `path:line` not in the
   findings table; a test feeds a narrative that names a refuted finding.
6. On RFC-01's critical corpus, critical precision under blinded adjudication
   is reported for at least one lane with n ≥ 20, beside the pre-verifier
   baseline; the corpus instruction-file cases show the PR #37 class detected
   by at least one lane.
7. `docs/STRICTNESS.md`, `docs/ITERATION_AWARENESS.md` § 7.1, `docs/PROMPTS.md`
   and `docs/SECURITY.md` are current; `MIGRATION_v3.md` (RFC-07) carries
   `BC-07`/`BC-08`.

## Cited symbols

- `class Finding`
- `finding_fingerprint`
- `IAR_CONTEXT_HASH_RADIUS`
- `dedupe_findings_against_prior`
- `_sort_findings_criticals_first`
- `reconcile_prior_findings`
- `RESOLUTION_POLICY_VERIFIED`
- `RESOLUTION_POLICY_ADVISORY`
- `PRIOR_FINDING_STATUS_RESOLVED`
- `PRIOR_FINDING_STATUS_REGRESSED`
- `update_prior_finding`
- `compute_check_gate`
- `STRICTNESS_BLOCK_CRITICAL`
- `ReviewState`
- `safe_repo_path`
- `post_inline_comment`
