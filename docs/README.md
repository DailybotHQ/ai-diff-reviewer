# `docs/` — Documentation Index

Everything an agent (or a human) needs to work on **AI PR Reviewer** beyond the entrypoint [`AGENTS.md`](../AGENTS.md).

The docs tree is organised by intent: *what the product is* → *how it's built* → *how to change it* → *how AI agents collaborate on it*. Every document is repo-specific — there are no generic stubs. If you find one, that's a bug: open an issue.

## Product & architecture

| Document | Purpose |
|---|---|
| [PRODUCT_SPEC.md](PRODUCT_SPEC.md) | The non-technical "why" — the problem the action solves, who it's for, success criteria, and explicit non-goals. |
| [ARCHITECTURE.md](ARCHITECTURE.md) | The real components: composite action shell, `scripts/reviewer.py` runtime, the five tools the model calls, and the review-submission flow (including the 422 fallback). |
| [PERFORMANCE.md](PERFORMANCE.md) | Cost and latency budget of the agentic loop: `MAX_TURNS`, `max_tokens`, conversation pruning, and per-review cost drivers. |

## Standards & how to build

| Document | Purpose |
|---|---|
| [STANDARDS.md](STANDARDS.md) | Repository standards: stdlib-only rule, type hints, `AIPRR_` env-var prefix, action.yml stability, commit-message shape. |
| [DEVELOPMENT_GUIDELINES.md](DEVELOPMENT_GUIDELINES.md) | Python guidelines — style rules and anti-patterns specific to `scripts/reviewer.py`. |
| [DEVELOPMENT_COMMANDS.md](DEVELOPMENT_COMMANDS.md) | Verbatim command reference (compile check, unittest suite, action.yml validation, local debug). |
| [TESTING_GUIDE.md](TESTING_GUIDE.md) | How the stdlib `unittest` suite is organised, how to run it, and the dogfooding loop via `self-review.yml`. |
| [SECURITY.md](SECURITY.md) | Secrets handling (`AIPRR_API_KEY`), tool-arg redaction, safe-path resolution, sensitive-data boundaries. |
| [DOCUMENTATION_GUIDE.md](DOCUMENTATION_GUIDE.md) | How this documentation tree is organised and the rule that keeps it in sync with runtime behaviour. |

## User-facing surface (referenced from `README.md`)

| Document | Purpose |
|---|---|
| [PROMPTS.md](PROMPTS.md) | What a good custom prompt looks like — the main lever consumers pull to adapt the reviewer to their codebase. |
| [PROVIDERS.md](PROVIDERS.md) | Provider abstraction, the Anthropic-shape contract, and the roadmap for OpenAI / Gemini / Azure. |
| [STRICTNESS.md](STRICTNESS.md) | The three strictness modes and how the model's `severity` argument maps to the GitHub check outcome. |

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
