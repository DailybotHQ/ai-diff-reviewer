# Skills & Agents Discovery — IAR plan

**Analyzed:** all completed tasks in PLAN_iteration_aware_review (Tasks 1–11)
**Reviewer:** Cursor Composer running the DWP `task_skills_agents_discovery` skill, 2026-07-15
**Plan:** PLAN_iteration_aware_review — Task 12

> **v1.8.0 default-flip addendum (post-DWP, 2026-07-15):** After this discovery closed, the two `action.yml` defaults flipped: IAR is now **on by default** with `first-pass-exhaustive`. The convention captured here — "Backward-compat regression suites for behaviorally-flagged features" — was generalized in `docs/TESTING_GUIDE.md` to cover both opt-in and opt-out master switches; the reference implementation at `tests/test_backward_compat_iar_off.py` now guards the explicit-opt-out path rather than the implicit-off default. No skills or agents were added or removed; the follow-up `CATALOG-FOLLOWUP-1` (`stateful-review-loop` general-purpose skill) is still recorded and still pending post-empirical-data.

## Executive summary

The IAR subsystem introduced five reusable patterns worth surfacing in the catalog. **Zero new skills were authored** — every pattern is either domain-specific to the reviewer (best captured as agent-persona additions or `.review/extension.md` rules) or is more accurately a repo convention (best captured in `docs/`). Two existing agent personas (`reviewer`, `prompt-engineer`) received IAR-specific sections; `docs/TESTING_GUIDE.md` gained a "Backward-compat regression suites" convention section; `.agents/docs/skills_agents_catalog.md` gained an "Iteration-Aware Review (v1.6.0+) coverage" callout. One follow-up (`CATALOG-FOLLOWUP-1`, a general-purpose `stateful-review-loop` skill) is tracked for a future release, contingent on the empirical IAR data collected post-Task-10.

## Pattern Scan

| Pattern | Novel? | Reusable? | Action |
|---|---|---|---|
| **Prompt-splicing addendum** — hardcoded module-scope constant appended to the system prompt conditionally on runtime state (Task 6). | Yes, in the context of this repo — no previous feature spliced runtime-conditional text into the system prompt. | Low — the pattern is inherently tied to prompt-engineering craft, and the load-bearing constraints (hardcoded, no user interpolation, additive) are already codified in `docs/SECURITY.md` § IAR trust boundary → Prompt splicing. | **Document in `prompt-engineer` agent.** Added a new "Iteration-Aware Review (IAR) prompt addendum" section to `.agents/agents/prompt-engineer.md` codifying the four design constraints (hardcoded module constant, additive to base prompt, round-1-of-generation only, ~150 tokens). No new skill. |
| **Content-anchored fingerprinting** — SHA256 fingerprint that hashes both the finding's own fields AND ±20 lines of code context around the anchor (Task 5). | Yes — no previous feature hashed context for dedup purposes. | Low — the pattern is inherently tied to the reviewer's "find-in-code" domain. Other AI-driven CI tools that do dedup would want it, but there's no other consumer in this repo. | **Document in `reviewer` agent** (indirectly, via the "critical-always-surfaces rail" invariant, which depends on this pattern being correct). No new skill. |
| **HTML-comment embedded state** — piggybacking structured JSON state on top of a user-visible PR comment via `<!-- ai-pr-reviewer-iteration-state:v1 --> {...} <!-- /ai-pr-reviewer-iteration-state -->` (Task 3). | Yes — a novel-to-this-repo way to persist per-PR state without an external DB. | **High** — could be reused for any future stateful workflow in this repo (e.g. per-PR provider preferences, per-PR strictness overrides). | **Document in `docs/SECURITY.md`** as a trust-boundary section (done in Task 11 — see `docs/SECURITY.md` § IAR trust boundary → Marker-embedded state block). Future stateful features should reuse the same `_parse_state_from_marker_body` failure discipline. No new skill for now; consider promoting to a reusable module if a second consumer emerges. |
| **Backward-compat regression suite** — dedicated `tests/test_backward_compat_<feature>.py` file per opt-in feature that asserts byte-identical runtime with the master switch off (Task 2). | Yes — no previous feature had a dedicated back-compat file. | **High** — every future opt-in feature should ship with one. | **Document as repo convention in `docs/TESTING_GUIDE.md`.** Added a new "Backward-compat regression suites for opt-in features (repo convention)" section with `tests/test_backward_compat_iar_off.py` as the reference implementation. No new skill; the convention is captured as documentation. |
| **Hard-cost-gate dogfooding** — dogfood validation where a numeric telemetry metric (cost delta ≤ +20% steady-state) is a hard blocking gate for plan completion (Task 10). | Somewhat — earlier plans have had dogfood tasks, but not with an explicit numeric gate that blocks completion. | Medium — future performance-sensitive features (e.g. new providers with significantly different token profiles) would benefit from the same pattern. | **Document in `.agents/docs/skills_agents_catalog.md`** as a future-opportunity note. Captured in the new "Iteration-Aware Review (v1.6.0+) coverage" callout as the `CATALOG-FOLLOWUP-1` recommendation. No new skill until a second consumer emerges. |

## Skills Created

**None.** No pattern from the IAR plan was novel *and* general-purpose enough to warrant a new standalone skill in this release. The domain-specific patterns (prompt splicing, fingerprinting) were captured in the affected agent personas; the general patterns (backward-compat suite, hard-cost-gate) were captured in `docs/` conventions.

## Skills Updated

**None.** No existing skill's SKILL.md was materially affected by the IAR release:

- `add-provider` — unchanged; IAR is orthogonal to provider implementation.
- `prompt-test` — unchanged; the smoke-test flow described in the skill is the same for IAR-on vs IAR-off (the `prompt-engineer` agent's new IAR addendum section is where the IAR-specific guidance lives).
- `release` — unchanged; the release procedure is the same for the v1.6 IAR release as for any prior release. The auto-release workflow already handles the vendored skill refresh (Rule #10 pillar B).
- `deepworkplan` (vendored) — unchanged; IAR was implemented AS a deep work plan; the skill itself required no changes.
- `dailybot` (vendored) — unchanged; used for reporting during the plan.

## Agents Updated

Two agent personas received IAR-specific sections:

- **`.agents/agents/reviewer.md`** — new Section 11 "Iteration-Aware Review (IAR) contract (v1.6.0+)" listing 7 review-checklist items specific to IAR code paths (frozen `IARConfig`, whitelist fallback, critical-always-surfaces rail preservation, `_parse_state_from_marker_body` failure discipline, argv-list subprocess pattern, hardcoded prompt-splicing constant, `write_iar_outputs_empty()` when off, `test_iar_*.py` file naming). Rationale: any future PR that touches IAR code needs to be reviewed against these load-bearing invariants, and a code reviewer without the checklist would miss them.
- **`.agents/agents/prompt-engineer.md`** — new "Iteration-Aware Review (IAR) prompt addendum (v1.6.0+)" section codifying the four design constraints of `IAR_EXHAUSTIVE_PROMPT_ADDENDUM` (hardcoded module constant, additive to base prompt, round-1-of-generation only, ~150 tokens) plus a smoke-test procedure for edits to the addendum. Rationale: the exhaustive addendum is the only prompt-adjacent surface introduced by IAR, and it's already caused security-relevant discussion in Task 11's review — the persona should know about it.

**Not updated:**
- `.agents/agents/provider-implementer.md` — unchanged; IAR is provider-agnostic. Any future provider addition operates within the IAR pipeline transparently via the existing `Provider` / `AgentRunnerProvider` interfaces.

## Catalog Updates

- **`.agents/docs/skills_agents_catalog.md`** — new "Iteration-Aware Review (v1.6.0+) coverage across the catalog" callout that inventories where IAR coverage was added (which agents, which docs) and records the `CATALOG-FOLLOWUP-1` recommendation for a future `stateful-review-loop` skill.

## Doc conventions added

- **`docs/TESTING_GUIDE.md`** — new "Backward-compat regression suites for opt-in features (repo convention)" section that promotes the `test_backward_compat_<feature>.py` file naming as a repo standard. This is a genuine new convention (no previous doc articulated it); the IAR file at `tests/test_backward_compat_iar_off.py` is the reference implementation.

## Recommendations

1. **Consider extracting IAR into a standalone reusable module for other AI-driven CI tools** — the fingerprint / dedup / policy / generation patterns are self-contained enough that a `stateful-review-loop` Python package could carry them cross-repo. Not shipped in this release because (a) the IAR patterns are still evolving and would benefit from empirical dogfood evidence (Task 10) before abstraction; (b) the abstraction cost would violate this repo's stdlib-only constraint if pulled from an external package. Track as `CATALOG-FOLLOWUP-1`. Best revisited after 3–6 months of production IAR data.
2. **Watch for a second stateful-workflow consumer.** If a future feature (say, per-PR provider preferences or per-PR strictness overrides) also needs marker-embedded state, that's the trigger to promote the HTML-comment state pattern to a shared helper (`_parse_marker_state<T>`) rather than let each feature reinvent it.
3. **Once IAR ships empirical numbers** (Task 10 dogfood + release-PR self-review data), consider promoting the `docs/PERFORMANCE.md` IAR section to a first-class linked doc in the "Detailed Documentation" table of `AGENTS.md`. Currently it's a subsection of `PERFORMANCE.md`; if the empirical section grows, it may deserve its own file at `docs/IAR_PERFORMANCE.md`.

## Validation

```
$ python3 -m unittest discover -s tests
Ran 428 tests in 0.205s
OK

$ python3 -m py_compile scripts/reviewer.py
(exit 0)
```

No new skill files were authored, so no `scripts/validate-frontmatter.py` run is needed. All agent-persona additions are documentation-only (no new agent file with new frontmatter). Repo continues to compile and test-clean.
