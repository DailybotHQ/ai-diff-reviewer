# AI Diff Reviewer v3

> **Draft** — written during the v3 implementation plan (Phase 1, 2026-09-24). Sections marked *planned* describe rows of the [breaking-change ledger](rfc/v3/07-breaking-change-ledger.md) that ship later in this major; the shipped state is what `CHANGELOG.md § [Unreleased]` says.

**`@v2` keeps working unchanged; `v2` stops moving at its last `v2.x.y` and receives security and catalog fixes for six months from `release/v2` (RFC-08 D-09).**

**Default pin:** `uses: DailybotHQ/ai-diff-reviewer@v3`  
**Skill:** `npx skills add DailybotHQ/ai-diff-reviewer@v3 --skill ai-diff-reviewer`  
(or `npx skills update ai-diff-reviewer`)

Exact pin when you want a frozen tag: `@v3.0.0` (skill frontmatter `version: "3.0.0"`); the release candidate is `@v3.0.0-rc.1` (no moving tag for the rc).

## Contract

- **Inputs renamed or removed: none.** Every v2 input keeps its name, type and default; v3 adds optional inputs only (`verifier`, `verifier-model`, `strict-unverified-criticals`, `mode`, `expected-legs`, `min-agreement`, `require-all-legs`, `budget-profile`, `high-risk-paths`, `complexity-source`).
- **Behaviours changed** (the breaking rows): **BC-03** in-process request shape, **BC-04** review status semantics, **BC-07** strictness gates on *verified* criticals, **BC-17** the "byte-identical" promise ends, **BC-13** tiered turn budgets (full rounds by risk tier, follow-up rounds by the delta), **BC-15** the default model is the `balanced` alias.
- Env-var prefix stays `AIPRR_` (private contract). Repo path stays `DailybotHQ/ai-diff-reviewer`.

## What you must change

Work through this list once; most consumers change nothing.

1. **Strictness expectations (BC-07).** `block-on-critical` now blocks on criticals the verifier confirmed. A claimed critical the verifier could not confirm publishes as a **warning** with a `Claimed critical; verifier found:` note and does not block; a refuted claim is not posted inline at all (it is listed in the structured output). If your process depended on any claimed critical blocking, set `strict-unverified-criticals: true` for one minor cycle (removed in `v3.1.0`) while you calibrate. Details: [STRICTNESS.md § Verified criticals](STRICTNESS.md).
2. **Reviews that do not finish are red (BC-04).** A review that hits its turn cap without submitting (`incomplete`) or its wall-clock cap (`timeout`) used to pass with whatever it had. It now fails the check under `block-on-critical` and stricter, posts the partial findings with a note, and records the status in the run record and outputs. `lenient` stays green. If you see new failures on very large PRs, raise `max-turns` / `agent-max-turns` or split the PR.
3. **Automation that parses the review body (BC-07 / BC-11).** The posted body is now generated from the findings table (counts by published severity, a verification line, the check line, the table, a bounded narrative). Do not scrape it — read `.aiprr/review-output.json` (`review-output/3.0`, path and SHA-256 in the `structured-output-path` / `structured-output-sha256` outputs) or the uploaded artifact. The `<!-- ai-pr-reviewer-marker -->` anchor is unchanged.
4. **In-process runners (`provider: anthropic` / `openai`) changed their wire shape (BC-03, BC-17).** The first message carries a SHA-bound change inventory plus patches within a byte budget instead of one embedded diff; the tool set gains `get_change_inventory`, `get_patch`, `read_instruction_files`, `read_file ref=base`, `emit_finding`. Requests, token profile and prompt-cache behaviour differ from v2; there is no "empty `api-base` keeps the runner byte-identical" promise any more. Pin `@v2` if you need the old wire shape. Both runners **stay supported for code review** — the Phase 1 measurement kept them ([RFC-08 D-03](rfc/v3/08-roadmap-and-decisions.md)).
5. **Turn budgets** (BC-13): the constant `max-turns: 30` gives way to a risk-tiered budget classified from the change inventory (`low` 8 · `standard` 20 · `elevated` 30 · `critical` 40 turns, with patch bytes, the verifier's warning sample and the output-token cap per tier; `high-risk-paths` raises a PR to `critical`). An explicit `max-turns` stays a ceiling. `budget-profile: fixed` keeps the constant profile for one minor cycle (removed in `v3.1.0`). Follow-up rounds are budgeted by the delta (`4 + 1.5 × files + 1 × outstanding`, floor 4); a push with no code change runs a verifier-only round.

## What you may adopt

- **Structured output (BC-11):** `review-output/3.0` on every run, uploaded as the `ai-diff-reviewer-<head12>-<provider>-<kind>-<model>` artifact; the `apply-review` skill reads it first.
- **Verifier knobs (BC-18):** `verifier: on|off` (default `on`), `verifier-model` (alias `economy` by default, or an explicit id), `strict-unverified-criticals`. Measured cost: ≈ $0.009 and 10 s per verified finding ([PERFORMANCE.md § Verifier cost](PERFORMANCE.md#verifier-cost-v3-measured)).
- **Documented-rules findings (BC-05):** the bundled prompt now reads `AGENTS.md`, `CONTRIBUTING.md` and friends and reports `contradicts-documented-rule` with the quoted rule; tune with `.review/extension.md`.
- **Finding v3 fields in agent-runner findings files (BC-12):** optional `title`, `category`, `evidence.*` — legacy files still parse.
- **Ensemble mode (BC-09):** run each provider as a matrix leg with `mode: emit` and one `mode: aggregate` job — one consolidated review per head, duplicates merged by anchor, agreement per finding, the verifier run once; knobs `expected-legs`, `min-agreement`, `require-all-legs`; outputs `legs-expected`, `legs-delivered`, `duplicates-removed`, `agreement-histogram` ([examples/ensemble-matrix.yml](../examples/ensemble-matrix.yml)). Consumers with per-leg IAR history: the first aggregated round collapses the per-leg reviews and starts one history on the aggregate marker.
- Budget knobs `budget-profile` / `high-risk-paths` (BC-19); `complexity-source: inventory` derives the `complexity:*` label from the risk tier (BC-14); with an empty `model` the review runs on the lane's `balanced` alias — today's benchmarked defaults — and `economy` is never selected for review (BC-15). *Planned:* `api-key` optional wherever environment credentials exist (BC-16).

## Transition knobs (one minor cycle)

| Knob | Restores | Removed in |
|---|---|---|
| `strict-unverified-criticals: true` | v2 gating on the claimed critical | `v3.1.0` |
| `budget-profile: fixed` | the constant 30-turn budget (every tier) | `v3.1.0` |

## Platform behaviour (v3)

1. **Verification pass on every review:** every claimed critical and a 30 % sample of warnings get a second, read-only, code-grounded look before publishing; the verifier fails open into visibility (never blocks, never hides).
2. **Run record and structured output on every run** — success, skip, failure, timeout — with endpoint *kind* only, never a host.
3. **Iteration-Aware Review keeps its rails**; retirement of a prior finding additionally needs the anchor re-read at the new head (BC-08), so an edited file no longer retires a finding by itself.
4. **Consolidated review on matrices (BC-09):** several legs emit, one aggregate job posts; the legs' job token needs only read scopes.

## Eval gate

Releases are gated on a current, non-blocking eval verdict for the candidate's runtime and prompt (BC-02): `auto-release.yml` runs `tests/eval/release_gate.py` before the version bump and skips the cut when the verdict is missing, stale or blocking. Verdicts live in `tests/eval/records/verdicts/`; the measured floor and thresholds are in [PERFORMANCE.md § Measured noise floor](PERFORMANCE.md#measured-noise-floor-and-the-eval-gate-verdict-v3) and [RFC-01](rfc/v3/01-eval-gate-contract.md).

## Further reading

- [CHANGELOG.md](../CHANGELOG.md) — the shipped state, row by row
- [rfc/v3/README.md](rfc/v3/README.md) — the design records (RFC-00…08)
- [STRICTNESS.md](STRICTNESS.md) · [PERFORMANCE.md](PERFORMANCE.md) · [PR_REVIEW_WORKFLOW.md](PR_REVIEW_WORKFLOW.md) · [ITERATION_AWARENESS.md](ITERATION_AWARENESS.md)
- [examples/verifier.yml](../examples/verifier.yml)
- [MIGRATION_v2.md](MIGRATION_v2.md) — the v2 record
