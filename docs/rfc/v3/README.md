# AI Diff Reviewer v3 — design records (RFCs)

The evidence-backed design for the `v3` major. Produced by the discovery plan
`PLAN_v3_discovery` (2026-09-23) from the PR #57 / #58 dogfood telemetry, the
Jev validation campaign record and the runtime as of v2.5.1. These are
**design records**, not living documentation: once a record is accepted
(decision D-00 in RFC-08) it changes only through a new record that
supersedes it. The runtime documentation under `docs/` stays authoritative
for shipped behavior.

## Index

| Id | File | Title | Status |
|---|---|---|---|
| RFC-00 | [00-evidence-ledger.md](00-evidence-ledger.md) | Evidence ledger and problem statement (P-1…P-11; every claim labelled verified / recorded / hypothesis) | Draft |
| RFC-01 | [01-eval-gate-contract.md](01-eval-gate-contract.md) | Eval-gate contract, thresholds from the measured noise floor, run-record schema | Draft |
| RFC-02 | [02-unified-runner.md](02-unified-runner.md) | Unified runner: tool parity, change inventory with completeness, lane decisions | Draft |
| RFC-03 | [03-verification-and-evidence.md](03-verification-and-evidence.md) | Verification pass, Finding v3 evidence bundle, severity policy, evidence-based retirement | Draft |
| RFC-04 | [04-ensemble-consolidation.md](04-ensemble-consolidation.md) | Ensemble consolidation: one published review per pull request | Draft |
| RFC-05 | [05-structured-output.md](05-structured-output.md) | Structured review output as the official contract | Draft |
| RFC-06 | [06-risk-tiered-budgets-and-incremental.md](06-risk-tiered-budgets-and-incremental.md) | Risk-tiered budgets and true incremental review | Draft |
| RFC-07 | [07-breaking-change-ledger.md](07-breaking-change-ledger.md) | Breaking-change ledger (BC-01…BC-20), SemVer, tag policy, MIGRATION_v3 outline | Draft |
| RFC-08 | [08-roadmap-and-decisions.md](08-roadmap-and-decisions.md) | Roadmap, phase → plan decomposition, release cuts, evaluation budget, decision log (D-00…D-28) | Draft |

Schemas (JSON Schema draft 2020-12) with passing examples under
[`schemas/`](schemas/): `run-record.schema.json`, `finding-v3.schema.json`,
`review-output-v3.schema.json`.

## Reading order

1. **RFC-00** — what is known, with what confidence. Everything else cites
   its rows.
2. **RFC-08** — the phases, the plans and the decisions waiting for the
   maintainer. Read this second if you need the shape before the detail.
3. **RFC-01** — the gate every later claim is measured by.
4. **RFC-02 → RFC-03 → RFC-04 → RFC-05 → RFC-06** — the design, in dependency
   order (runner → verification → consolidation → output → budgets).
5. **RFC-07** — what changes for a consumer, and why the number is 3.

## Status legend

| Status | Meaning |
|---|---|
| Draft | written by the discovery plan; open questions carried into RFC-08's decision log |
| Accepted | the maintainer recorded D-00 (or the RFC's own decision rows) as accepted in RFC-08, with a date; implementation plans may be created from it |
| Superseded | replaced by a later record named in its Status section |
| Implemented | the acceptance criteria in its last section hold on `main`; the shipped behavior is documented under `docs/` |

An RFC moves from Draft to Accepted **only** by a decision recorded in
RFC-08's log — never by an implementation landing first.
