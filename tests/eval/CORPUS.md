# The v2 evaluation corpus (`tests/eval/cases/`)

Labelled, immutable review cases backing the Jev comparison experiments
(experiment contract F2/F3/F7 in the plan's `analysis_results/EXPERIMENT_CONTRACT.md`).
Every case is one reviewable change: a small before/after fixture (or a pinned
historical PR), PR metadata, labels with reproducible evidence, and expected
reviewer behavior. The corpus answers "did the reviewer find what a careful
human finds?" — nothing else.

## Layout

| Path | Purpose |
| --- | --- |
| `cases/C*.json` | One JSON file per case (schema `ai-diff-reviewer/eval-case/1`) |
| `corpus_validate.py` | Strict validator: structure, pins, floors, adjudication, secret markers |
| `corpus.json` | **Legacy** PR-keyed labels used by `run_eval.py score` (v3.1 era); kept untouched for backward compatibility |

Case ids are assigned at authoring time and never reused. `C072` was never
published, so files run C001–C102 minus C072 (101 cases; C075–C102 are
the v3 corpus-gap batch) — the gap is intentional, and ids, not positions, are
the identity. The validator pins
each case's `id` to its file name, so a renamed, duplicated, or hand-copied
file fails the strict gate, and the declared inventory (C001–C102 minus
C072) is itself enforced by the validator — an omitted or replaced case
cannot slip past the aggregate floors.

## Validate

```bash
python3 tests/eval/corpus_validate.py                 # human-readable
python3 tests/eval/corpus_validate.py --json          # machine-readable
python3 -m unittest discover -s tests -p 'test_eval_corpus*.py' -v
```

The validator is the gate (`CORPUS` in the plan's validation register): it
rejects duplicate identities, wrong or missing fixture pins (SHA-256 over the full immutable fixture payload — trees, inventory, pr_metadata, deceptive flag), undeclared or missing case ids,
unadjudicated critical labels, unblinded adjudication, secret-looking fixture
content, labels whose path is outside the fixture, negatives that carry labels,
and any breach of the declared floors.

## Floors (declared, enforced) and current counts

| Dimension | Floor | Current |
| --- | --- | --- |
| Total cases | ≥ 60 | **101** |
| Stack families | ≥ 3 (python, typescript, go, shell, rust) | **5** |
| Critical-positive cases | ≥ 20 (every label adjudicated, blinded) | **21** |
| Warning-positive cases | — | **53** |
| Negative/false-positive controls | ≥ 20, reasoned reference review | **27** |

Warning-positive labels are evidence-grounded but are not required to carry a
blinded adjudication record (the validator enforces adjudication only for
critical labels, whose floor they protect).

Adjudication outcome (blinded, `independent-reviewer-v1`): of 23 original
critical claims, 18 confirmed, 4 downgraded to warning (real defect, overstated
severity: C007, C015, C018, C063), 1 rejected outright (C017 — the removed Go
guard was provably redundant; the case now serves as a negative control). The
floor breach that rejections cause was repaired by adding C068–C070, which went
through the same blinded adjudication (3/3 confirmed). No label survives on its
author's word.

## Case classes covered

Security (auth bypass, SQL/OS injection, path traversal, SSRF, XSS, prototype
pollution, CORS, crypto, secret handling, TLS verification, supply chain),
concurrency (races in Go and Python), migrations (destructive), resource leaks,
error handling, public contracts, UI, dependencies (typosquat vs. legitimate
upgrade), policy/prompt-file injection, ordinary logic bugs, and the negative
families (docs, formatting, tests-only, renames, lockfile noise, equivalent
refactors, safe implementations of dangerous-looking features).

Adversarial shapes are first-class: deceptive PR metadata (declared
`deceptive: true`), prompt/policy files embedding agent-directed instructions,
an incomplete-diff coverage probe (`inventory.missing_from_fixture` — any
triage verdict must be `insufficient_evidence`, never a clean pass), and
multi-round IAR cases (a partially fixed finding and a reintroduced one).

## v3 corpus-gap coverage (RFC-01 § Corpus gaps)

Added by PLAN_v3_implementation Task 5 as C075–C102. Gap classes are detected
from case content by `tests/test_eval_corpus_gaps.py` (labels spanning more
than one path; an instruction file in the trees or `change_class:
policy_prompt_file`; `pr_metadata.deceptive`; a label path the reviewer omits
by default or lists in `inventory.missing_from_fixture`; a `fixture.iar` block)
and cross-checked against the `gap_tags` each new case declares.

| Gap | Minimum | Cases |
| --- | --- | --- |
| Cross-file defects (labels on two paths) | ≥ 6 | C075–C080 (+ any earlier multi-path case) |
| Instruction-file contradictions (the PR #37 class: `AGENTS.md` / `.review/extension.md` in the trees, the change violates a stated rule) | ≥ 5 | C081–C085 (+ C022, C059) |
| Deceptive PR metadata (description claims an absent fix) | ≥ 6 | C086, C087 (+ C012, C017, C021, C022) |
| Omitted / oversized patch (defect inside a default-ignored file: lockfile, minified bundle) | ≥ 4 | C088–C090 (+ C063) |
| Multi-round IAR (`fixture.iar.round1_head` + `round1_findings`; `head` is the round-2 push; labels = what must still surface) | ≥ 4 | C091 (partially fixed), C092 (anchor moved), C093 (fake fix), C094 (regression) |
| New stacks × 4 | shell, rust | C095–C098 (shell), C099–C102 (rust) |

**Adjudication status of the batch.** All positive labels in C075–C102 are
`warning` and every case carries `adjudication.status: pending`: no blinded
adjudication (F7 / RFC-08 D-26) has run on them yet. A case the blinded
adjudicator confirms as critical is upgraded to `critical_positive` with the
adjudication record; the critical floor is protected by the 21 adjudicated
cases already in the corpus. Multi-round fixtures are consumed by the
incremental-review evaluation (RFC-06); `--tree` runs them as a single
base → head review until that harness lands.

## Grouping before splitting (F2)

`family_group` ties together variants that must never land in different
evaluation splits — most importantly the counterpart pairs where the same
feature exists in a dangerous and a safe implementation (e.g. G01: decorator
bypass vs. equivalent inline check; G17: traversal vs. strengthened containment;
G20: interpolated vs. parameterized LIKE; G21: typosquat vs. real upgrade;
G22: policy injection vs. plain prose). The split assigner (Task 4, frozen
salt) groups by `family_group` first; leakage of a group across splits is a
validator error at that layer.

## Adjudication protocol (F7)

Every critical label is adjudicated by an independent reviewer (blinded: it
does not see the claimed severity or evidence before forming its own view, and
it knows nothing about the experiment arms or the corpus purpose). The
adjudicator records `real`, `introduced`, independent `severity`, and whether
it agrees with the claim. Labels that fail adjudication are downgraded or
removed — never kept on the author's word. Legacy historical-PR labels carry
the v3.1 in-repo evaluation as their adjudicator of record.

## Rights and egress

Synthetic fixtures were authored for this corpus (repository MIT). The four
historical cases reference this repository's own public PRs by merge-commit
SHA. No real credentials, hosts, or user data appear anywhere (validator
enforced). Fixtures are safe to ship inside the repo and to send to model
providers during experiments.
