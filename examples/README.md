# `examples/`

**Copy-paste workflow snippets** for the most common ways to wire the action into a consumer repo. Each file is a standalone `.github/workflows/*.yml` that a downstream project can drop into its own repo, tweak, and ship.

## Contents

| Example | Scenario |
|---|---|
| [`basic.yml`](basic.yml) | The minimum — API key + GitHub token, defaults for everything else. Same shape as the "Quick start" in the root [`../README.md`](../README.md). |
| [`open-source-safe.yml`](open-source-safe.yml) | **Public repo, safest defaults.** Combines the write-tier `author-association` gate, `label-gate: ai-review`, and `trigger-mode: label-once` so external contributors' PRs never trigger the reviewer (prevents LLM-budget abuse). Recommended starting point for any public open-source project. |
| [`label-gated.yml`](label-gated.yml) | Only run when the PR carries a specific label (e.g. `ready`); apply another label after a successful review (e.g. `pr-reviewed`). Keeps work-in-progress noise out of the review queue. |
| [`skip-review-label.yml`](skip-review-label.yml) | **Emergency-bypass label.** When the PR carries `skip-ai-review` (opt-in, configurable), the reviewer short-circuits to success — no LLM call, GitHub check green. For hotfixes, rollbacks, or trivially-safe commits. Security: pair with a ruleset that restricts who can apply the label. |
| [`iteration-aware.yml`](iteration-aware.yml) | **Iteration-Aware Review tuning knobs** — explicit reference showing every `convergence-*` / `iteration-*` input at its shipped default. IAR runs unconditionally on every review; this file is a copy-paste starting point for tweaking the convergence policy, the round-1 cap multiplier, the escape label, or wiring up the IAR cost-telemetry outputs as workflow annotations. See [`../docs/ITERATION_AWARENESS.md`](../docs/ITERATION_AWARENESS.md). |
| [`strict.yml`](strict.yml) | Fail the GitHub check on critical (or critical + warning) findings — pair with a branch-protection rule that requires the check to pass. |
| [`custom-prompt.yml`](custom-prompt.yml) | Point the action at a house-rules prompt inside the consumer's own repo (full replacement). |
| [`custom-prompt-per-stack.yml`](custom-prompt-per-stack.yml) | Layer a stack-specific extension on top of the bundled default prompt. See [`prompts/`](prompts/). |
| [`provider-claude-code.yml`](provider-claude-code.yml) | Use the Claude Code CLI (agent-runner) instead of the direct Anthropic API. Shows both auth modes: a metered Anthropic API key **or** a Claude Pro/Max **subscription** token (`claude setup-token` → `sk-ant-oat…`). |
| [`provider-cursor.yml`](provider-cursor.yml) | Use the Cursor Agent CLI (agent-runner) for review. |
| [`provider-codex.yml`](provider-codex.yml) | Use the OpenAI Codex CLI (agent-runner) for review. |
| [`provider-openai.yml`](provider-openai.yml) | **OpenAI-compatible in-process runner** (`provider: openai`, v2.1.0+): zero install, bounded turns. Default backend OpenAI; commented variants for Azure Foundry, xAI and Z.ai via `api-base`. |
| [`provider-anthropic-zai.yml`](provider-anthropic-zai.yml) | **Bring your own endpoint** (`api-base`, v2.1.0+): the direct Anthropic-compatible loop against Z.ai GLM (zero install, flat-rate Coding Plan). Same shape works for xAI's Anthropic-compatible endpoint. |
| [`mcp-passthrough.yml`](mcp-passthrough.yml) | Inject a custom MCP servers config into whichever CLI provider you picked. |
| [`trigger-always.yml`](trigger-always.yml) | Run on every push (v1.1 behaviour, explicit). |
| [`trigger-label-once.yml`](trigger-label-once.yml) | Run exactly once per label application; toggle the label off/on to re-run. Recommended for teams that want the AI to review "when ready" and not on every push. |
| [`trigger-label-added-only.yml`](trigger-label-added-only.yml) | Fire only on the `labeled` webhook event. Never on push. |
| [`pr-description-autocomplete.yml`](pr-description-autocomplete.yml) | Let the reviewer AI write a first-draft PR body when the current body is missing/vague. Idempotent — never overwrites edits. |
| [`pr-description-block.yml`](pr-description-block.yml) | Fail the check when the PR body is empty or under a length threshold. Definition-of-Ready enforcement. |
| [`complexity-labeling.yml`](complexity-labeling.yml) | Ask the reviewer to apply `complexity:low\|medium\|high` labels based on cognitive load, files touched, and security surface — not line count. |
| [`full-featured.yml`](full-featured.yml) | Showcase: label-once + description autocomplete + complexity labels + extension prompt + block-on-warning + explicit IAR knobs (defaults shown) + optional `skip-review-label` (commented). |

Each file is self-contained and ready to drop into `.github/workflows/` in a downstream project.

## Convention

- Every example uses `DailybotHQ/ai-diff-reviewer@v2` — pinned to the moving major tag so consumers pick up patch/minor updates automatically. Consumers who want strict pinning replace `@v2` with `@vX.Y.Z`.
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
- [`../docs/TRIGGER_MODES.md`](../docs/TRIGGER_MODES.md) — how `trigger-mode` decides when to run, plus § Emergency-bypass label.
- [`../docs/ITERATION_AWARENESS.md`](../docs/ITERATION_AWARENESS.md) — IAR policies, escape label, USER_FORCED_RESET, outputs.
- [`../docs/PR_METADATA_CHECKS.md`](../docs/PR_METADATA_CHECKS.md) — how `pr-description-mode` and `complexity-labels-enabled` work.
- [`prompts/`](prompts/) — starter extension prompts + the meta-prompt for AI-generated custom prompts.
