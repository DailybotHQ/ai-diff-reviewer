# `examples/`

**Copy-paste workflow snippets** for the most common ways to wire the action into a consumer repo. Each file is a standalone `.github/workflows/*.yml` that a downstream project can drop into its own repo, tweak, and ship.

## Contents

| Example | Scenario |
|---|---|
| [`basic.yml`](basic.yml) | The minimum — API key + GitHub token, defaults for everything else. Same shape as the "Quick start" in the root [`../README.md`](../README.md). |
| [`label-gated.yml`](label-gated.yml) | Only run when the PR carries a specific label (e.g. `ready`); apply another label after a successful review (e.g. `pr-reviewed`). Keeps work-in-progress noise out of the review queue. |
| [`strict.yml`](strict.yml) | Fail the GitHub check on critical (or critical + warning) findings — pair with a branch-protection rule that requires the check to pass. |
| [`custom-prompt.yml`](custom-prompt.yml) | Point the action at a house-rules prompt inside the consumer's own repo (full replacement). |
| [`custom-prompt-per-stack.yml`](custom-prompt-per-stack.yml) | Layer a stack-specific extension on top of the bundled default prompt. See [`prompts/`](prompts/). |
| [`provider-claude-code.yml`](provider-claude-code.yml) | Use the Claude Code CLI (agent-runner) instead of the direct Anthropic API. |
| [`provider-cursor.yml`](provider-cursor.yml) | Use the Cursor Agent CLI (agent-runner) for review. |
| [`provider-codex.yml`](provider-codex.yml) | Use the OpenAI Codex CLI (agent-runner) for review. |
| [`mcp-passthrough.yml`](mcp-passthrough.yml) | Inject a custom MCP servers config into whichever CLI provider you picked. |
| [`trigger-always.yml`](trigger-always.yml) | Run on every push (v1.1 behaviour, explicit). |
| [`trigger-label-once.yml`](trigger-label-once.yml) | Run exactly once per label application; toggle the label off/on to re-run. Recommended for teams that want the AI to review "when ready" and not on every push. |
| [`trigger-label-added-only.yml`](trigger-label-added-only.yml) | Fire only on the `labeled` webhook event. Never on push. |
| [`pr-description-autocomplete.yml`](pr-description-autocomplete.yml) | Let the reviewer AI write a first-draft PR body when the current body is missing/vague. Idempotent — never overwrites edits. |
| [`pr-description-block.yml`](pr-description-block.yml) | Fail the check when the PR body is empty or under a length threshold. Definition-of-Ready enforcement. |
| [`complexity-labeling.yml`](complexity-labeling.yml) | Ask the reviewer to apply `complexity:low\|medium\|high` labels based on cognitive load, files touched, and security surface — not line count. |
| [`full-featured.yml`](full-featured.yml) | Everything on: label-once trigger + description autocomplete + complexity labeling + extension prompt + block-on-warning. The showcase example. |

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
- [`../docs/STRICTNESS.md`](../docs/STRICTNESS.md) — full explanation of the four strictness modes referenced by `strict.yml`.
- [`../docs/PROMPTS.md`](../docs/PROMPTS.md) — writing the custom prompt that `custom-prompt.yml` points at, and layering extensions used by `custom-prompt-per-stack.yml`.
- [`../docs/TRIGGER_MODES.md`](../docs/TRIGGER_MODES.md) — how `trigger-mode` decides when to run.
- [`../docs/PR_METADATA_CHECKS.md`](../docs/PR_METADATA_CHECKS.md) — how `pr-description-mode` and `complexity-labels-enabled` work.
- [`prompts/`](prompts/) — starter extension prompts + the meta-prompt for AI-generated custom prompts.
