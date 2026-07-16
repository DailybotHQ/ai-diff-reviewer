# `docs/` — Documentation Index

Everything an agent (or a human) needs to work on **AI Diff Reviewer** beyond the entrypoint [`AGENTS.md`](../AGENTS.md).

The docs tree is organised by intent: *what the product is* → *how it's built* → *how to change it* → *how AI agents collaborate on it*. Every document is repo-specific — there are no generic stubs. If you find one, that's a bug: open an issue.

## Product & architecture

| Document | Purpose |
|---|---|
| [PRODUCT_SPEC.md](PRODUCT_SPEC.md) | The non-technical "why" — the problem the action solves, who it's for, success criteria, and explicit non-goals. |
| [ARCHITECTURE.md](ARCHITECTURE.md) | The real components: composite action shell, `scripts/reviewer.py` runtime, the two provider families (chat-completions + agent-runner), the tools the chat-completions family calls, the `.aiprr/findings.json` contract the agent-runner family uses, and the review-submission flow (including the 422 fallback). |
| [PERFORMANCE.md](PERFORMANCE.md) | Cost and latency budget for both provider families: the agentic loop's `MAX_TURNS`/`max_tokens`/conversation pruning on the chat-completions path, and the single vendor-CLI invocation shape on the agent-runner path. |

## Standards & how to build

| Document | Purpose |
|---|---|
| [STANDARDS.md](STANDARDS.md) | Repository standards: stdlib-only rule, type hints, `AIPRR_` env-var prefix, action.yml stability, commit-message shape. |
| [DEVELOPMENT_GUIDELINES.md](DEVELOPMENT_GUIDELINES.md) | Python guidelines — style rules and anti-patterns specific to `scripts/reviewer.py`. |
| [DEVELOPMENT_COMMANDS.md](DEVELOPMENT_COMMANDS.md) | Verbatim command reference (compile check, unittest suite, action.yml validation, local debug). |
| [RELEASE_RECOVERY.md](RELEASE_RECOVERY.md) | Playbook for recovering from partial-release failures in `auto-release.yml` (tag pushed but sync commit rejected by branch protection, etc.). |
| [TESTING_GUIDE.md](TESTING_GUIDE.md) | How the stdlib `unittest` suite is organised, how to run it, and the dogfooding loop via `self-review.yml`. |
| [SECURITY.md](SECURITY.md) | Secrets handling (`AIPRR_API_KEY`), tool-arg redaction, safe-path resolution, sensitive-data boundaries. |
| [DOCUMENTATION_GUIDE.md](DOCUMENTATION_GUIDE.md) | How this documentation tree is organised and the rule that keeps it in sync with runtime behaviour. |

## User-facing surface (referenced from `README.md`)

| Document | Purpose |
|---|---|
| [PROMPTS.md](PROMPTS.md) | What a good custom prompt looks like — the main lever consumers pull to adapt the reviewer to their codebase. Explains `prompt-file` vs `prompt-extension-file`, the `.review/extension.md` convention that keeps local skill and CI action in sync, and the meta-prompt for AI-generated custom prompts. |
| [PROVIDERS.md](PROVIDERS.md) | Both provider families — the chat-completions Anthropic-shape contract and the agent-runner `.aiprr/findings.json` contract — plus the shipping providers (`anthropic`, `claude-code`, `cursor`, `codex`) and the roadmap for raw OpenAI / Gemini / Bedrock. |
| [STRICTNESS.md](STRICTNESS.md) | The four strictness modes (`lenient` / `block-on-critical` / `block-on-warning` / `block-on-any`) and how the model's `severity` argument maps to the GitHub check outcome. |
| [TRIGGER_MODES.md](TRIGGER_MODES.md) | The four `trigger-mode` values (`always` / `label-required` / `label-once` / `label-added-only`) and how to pair them with the workflow's `on:` block. |
| [PR_METADATA_CHECKS.md](PR_METADATA_CHECKS.md) | PR description review (`pr-description-mode`) and AI-driven complexity labeling (`complexity-labels-enabled`) — how each works, the tool schema, threat model. |
| [ITERATION_AWARENESS.md](ITERATION_AWARENESS.md) | Opt-in Iteration-Aware Review (IAR) subsystem — converges multi-round self-review loops via content-anchored fingerprints, four convergence policies, generation tracking, and a hardcoded critical-always-surfaces safety rail. Master switch defaults OFF so byte-identical backward compat is preserved. |

## Local companion skill (`skills/ai-diff-reviewer/`)

The action also ships a **local companion skill** that runs the same review methodology inside the developer's coding agent — same prompt, same severity model, same output shape — before opening a PR. The skill is not a doc; it's a package installed via `npx skills add DailybotHQ/ai-diff-reviewer --skill ai-diff-reviewer`. Its documentation lives inside the skill package itself so it can be read by any AI agent that has it installed:

| Skill file | Purpose |
|---|---|
| [`skills/ai-diff-reviewer/SKILL.md`](../skills/ai-diff-reviewer/SKILL.md) | Parent skill — routes to the five sub-skills, defines the trust boundary, and runs the default "review the current branch's diff" flow when no sub-skill is invoked. |
| [`skills/ai-diff-reviewer/generate-extension/SKILL.md`](../skills/ai-diff-reviewer/generate-extension/SKILL.md) | Sub-skill that inspects THIS repo (stack, architecture, security surface, existing conventions, historical pain) and writes a tailored `.review/extension.md` — no copy-paste, no manual authoring. |
| [`skills/ai-diff-reviewer/setup/SKILL.md`](../skills/ai-diff-reviewer/setup/SKILL.md) | Interactive installer for the GitHub Action itself — six-question wizard that writes `.github/workflows/pr-review.yml` tailored to the repo's stack and visibility. |
| [`skills/ai-diff-reviewer/setup/reference.md`](../skills/ai-diff-reviewer/setup/reference.md) | Reference manual for every `action.yml` input (description, default, choices, per-scenario recommendations). Any coding agent with the skill installed can answer *"what does `strictness` do?"* without opening the action source. |
| [`skills/ai-diff-reviewer/open-pr/SKILL.md`](../skills/ai-diff-reviewer/open-pr/SKILL.md) | Sub-skill that authors a well-documented PR title + body from the current branch's diff (Conventional-Commits inference, structured body sections, merges with `.github/pull_request_template.md` when present, executes via `gh pr create`/`edit`). |
| [`skills/ai-diff-reviewer/apply-review/SKILL.md`](../skills/ai-diff-reviewer/apply-review/SKILL.md) | Sub-skill that closes the CI-back-to-local loop — reads the live AI review on the PR (filters minimized/outdated comments), presents findings in the same shape as a local review, and walks the developer through apply / defer / skip per finding under the same trust boundary as the parent skill. Never commits, never pushes; edits source files only under per-finding *"apply"* consent with pre-image safety. |
| [`skills/ai-diff-reviewer/prompt.md`](../skills/ai-diff-reviewer/prompt.md) | Byte-identical copy of [`prompts/default.md`](../prompts/default.md); a CI invariant (`Skills — prompt-sync invariant` in [`code_check.yml`](../.github/workflows/code_check.yml)) fails PRs where the copy has drifted. |

See [PROMPTS.md § "Local coding-agent parity"](PROMPTS.md#local-coding-agent-parity) for the user-facing story on how the two surfaces stay in sync, and [ARCHITECTURE.md § "The local companion skill pack"](ARCHITECTURE.md) for the architectural view.

## AI-agent playbooks

| Document | Purpose |
|---|---|
| [AI_AGENT_ONBOARDING.md](AI_AGENT_ONBOARDING.md) | First-session checklist for any AI agent working on this repo. |
| [AI_AGENT_COLLAB.md](AI_AGENT_COLLAB.md) | Multi-agent coordination, when to spawn sub-agents, when a deep-work plan is warranted. |
| [PR_REVIEW_WORKFLOW.md](PR_REVIEW_WORKFLOW.md) | How to read this repo's own self-review comments (skip minimized comments, anchor on the marker). |

## Related directories

- [`.agents/`](../.agents/) — the canonical AI-agent kit: personas, skills, commands, and the installed [Deep Work Plan skill](../.agents/skills/deepworkplan/).
- [`.agents/docs/`](../.agents/docs/) — the skills & agents catalog plus the commands reference.
- [`prompts/`](../prompts/) — the bundled default system prompt shipped with the action.
- [`examples/`](../examples/) — copy-paste workflow snippets for common setups.

## Convention

Every document above is stable enough to link to from `README.md` and `AGENTS.md`. If you add a new doc, add a row here and (if it changes runtime behaviour) update the [`CHANGELOG.md`](../CHANGELOG.md) entry under `[Unreleased]`.
