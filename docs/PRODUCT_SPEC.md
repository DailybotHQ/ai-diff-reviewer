# Product Spec — AI PR Reviewer

## What it is

A GitHub Action that runs a real LLM-driven code review on every pull request, posts inline comments anchored to specific lines, gates the GitHub check based on configurable severity thresholds, and applies a "reviewed" label after a successful run. Distributed as a single composite action — no Docker image, no Node modules, no infrastructure beyond a provider API key.

## What problem it solves

PR review is the highest-leverage quality gate in most engineering organisations and the most under-staffed. Senior engineers don't scale; review becomes a bottleneck or a rubber stamp. Existing solutions either (a) require complex self-hosted infrastructure, (b) lock the team into a specific vendor's full ecosystem, or (c) produce shallow, line-by-line nits without project context.

This action targets the gap: **a stable, configurable, severity-aware reviewer that any GitHub user can drop into their workflow with a single `uses:` line, customise via prompt and strictness, and trust enough to gate merges on**.

## Who it's for

- **Open-source maintainers** who want a second opinion on community PRs before they hit human review.
- **Small engineering teams** without a dedicated reviewer rotation, who want consistent feedback on every PR.
- **Internal platform teams** who want to enforce house rules (security, performance, conventions) automatically.
- **Contributors** themselves — running the action on a fork lets you self-review before opening the PR upstream.

It is **not** a replacement for human code review. It's an additional reviewer that scales — catching the obvious things, applying the documented rules, freeing humans to focus on architecture, design, and judgement calls.

## Core capabilities

| Capability | What it means |
|---|---|
| Inline comments | Comments anchored to specific lines in the diff, with optional GitHub suggestion blocks (one-click apply). |
| Severity tagging | Every inline comment carries a `critical` / `warning` / `info` severity that the action aggregates. |
| Configurable gating | Three strictness modes (`lenient`, `block-on-critical`, `block-on-warning`) translate severity into the GitHub check status. |
| Custom prompts | Bring your own system prompt; the bundled default is technology-agnostic. |
| Label gate | Optionally only run when a PR has a specific label (e.g. `ready`). |
| Applied label | Optionally label a PR after a successful review (e.g. `pr-reviewed`) so downstream automation can require it. |
| Auto-collapse | Previous bot reviews are marked `OUTDATED` on every new push so only the latest is visually active. |
| Tracking comment | A spinner comment with a stable `<!-- ai-pr-reviewer-marker -->` marker transitions in-place from `Working…` to `View review →`. |
| Self-healing on 422 | If GitHub rejects the review because one comment anchored outside the diff, the action retries summary-only instead of losing every comment. |

## Non-goals

- **Real-time IDE integration.** This is a CI-time action; for IDE-time review use a code completion / chat tool.
- **Multi-PR or repo-wide reasoning.** The action reviews one PR at a time, with the diff as the contract. Cross-PR refactoring suggestions are out of scope.
- **Replacing branch protection.** Strictness gating *complements* branch protection (require the action's check to pass); it doesn't replace required-reviewer rules.
- **Auto-merging.** The action posts review feedback; merge decisions are the maintainer's. Pair with a separate auto-merge action if that's your workflow.
- **Generating code or fixing issues.** It comments and suggests; it doesn't push fixes. (Suggestion blocks let the maintainer one-click apply in the GitHub UI.)
- **Hosting any infrastructure.** Inputs go to the configured provider; outputs go to GitHub. No third party between.

## Distribution

- **License:** MIT.
- **Channel:** GitHub Marketplace (publicly searchable) + direct repo URL for `uses:`.
- **Versioning:** SemVer. The moving major tag (`v1`) auto-points to the latest `v1.x.y` so consumers pinning `@v1` get patches and minor features automatically.
- **Provider parity:** v1 ships with Anthropic; the `Provider` abstraction in `scripts/reviewer.py` makes adding OpenAI, Gemini, Azure OpenAI, or self-hosted endpoints a one-class addition. See [PROVIDERS.md](PROVIDERS.md).

## Quality bar

- **Stdlib-only runtime** — no install phase, no supply-chain surface beyond Python itself.
- **Single-file implementation** — `scripts/reviewer.py` is ~1500 LOC, fully type-hinted, runnable directly without the action wrapper for local debugging.
- **Compile-checked in CI** on every PR.
- **Dogfooded** — the action reviews its own PRs via `.github/workflows/self-review.yml`.

## Roadmap (not a commitment)

- v1.1 — OpenAI provider, Azure OpenAI provider.
- v1.2 — Gemini provider.
- v1.x — Community-curated prompt library at `prompts/community/<stack>.md`.
- v2.0 — only if a breaking change to the public input/output contract is unavoidable.
