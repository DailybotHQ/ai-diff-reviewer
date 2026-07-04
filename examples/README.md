# `examples/`

**Copy-paste workflow snippets** for the most common ways to wire the action into a consumer repo. Each file is a standalone `.github/workflows/*.yml` that a downstream project can drop into its own repo, tweak, and ship.

## Contents

| Example | Scenario |
|---|---|
| [`basic.yml`](basic.yml) | The minimum — API key + GitHub token, defaults for everything else. Same shape as the "Quick start" in the root [`../README.md`](../README.md). |
| [`label-gated.yml`](label-gated.yml) | Only run when the PR carries a specific label (e.g. `ready`); apply another label after a successful review (e.g. `pr-reviewed`). Keeps work-in-progress noise out of the review queue. |
| [`strict.yml`](strict.yml) | Fail the GitHub check on critical (or critical + warning) findings — pair with a branch-protection rule that requires the check to pass. |
| [`custom-prompt.yml`](custom-prompt.yml) | Point the action at a house-rules prompt inside the consumer's own repo. |

Each file is self-contained and ready to drop into `.github/workflows/` in a downstream project.

## Convention

- Every example uses `DailybotHQ/ai-pr-reviewer@v1` — pinned to the moving major tag so consumers pick up patch/minor updates automatically. Consumers who want strict pinning replace `@v1` with `@vX.Y.Z`.
- Every example includes `fetch-depth: 0` on `actions/checkout` (required — the runtime does `git diff origin/<base>...HEAD` and a shallow clone won't have the base ref).
- Every example sets the minimum permissions (`contents: read`, `pull-requests: write`).
- Every example includes a workflow-level `timeout-minutes: 15` (the recommended safety net — see [`../docs/PERFORMANCE.md`](../docs/PERFORMANCE.md)).

## When to add a new example

Add a new `.yml` here when a new `action.yml` input has a **non-trivial usage pattern** ([`../AGENTS.md`](../AGENTS.md) Pre-Commit Checklist). One-line input tweaks belong in the [`../README.md`](../README.md) "Recipes" section; anything worth 10+ lines of workflow YAML belongs here.

Add a row to the table above in the same PR.

## Related

- [`../README.md`](../README.md) — the marketplace-facing readme; the "Recipes" section links back to specific files here.
- [`../docs/STRICTNESS.md`](../docs/STRICTNESS.md) — full explanation of the three strictness modes referenced by `strict.yml`.
- [`../docs/PROMPTS.md`](../docs/PROMPTS.md) — writing the custom prompt that `custom-prompt.yml` points at.
