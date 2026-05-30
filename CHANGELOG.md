# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [1.0.0] — 2026-05-29

Initial public release.

### Added
- Composite GitHub Action that runs an LLM-driven code review on every pull request.
- Anthropic provider (`claude-sonnet-4-6` default), with `Provider` abstraction ready for OpenAI/Gemini drop-ins.
- Five-tool agentic loop: `read_file`, `grep`, `glob`, `post_inline_comment`, `submit_review`.
- Severity tagging (`critical` / `warning` / `info`) on every inline comment, surfaced as the `severity` action output.
- Three strictness modes (`lenient`, `block-on-critical`, `block-on-warning`) to gate the GitHub check.
- Optional `label-gate` input — only run when the PR carries a configured label.
- Optional `applied-label` input — auto-apply a label after a successful, non-blocked review (with auto-create if the label doesn't exist).
- Auto-collapse of previous bot reviews/comments via GraphQL `minimizeComment`.
- Tracking spinner comment with `<!-- ai-pr-reviewer-marker -->` marker, transitioning in-place from `Working…` to `View review →` (or `failed`).
- 422 fallback: if GitHub rejects the review because one inline comment anchored outside the diff, the action retries summary-only instead of losing every comment.
- Bundled default system prompt that's technology-agnostic and includes severity definitions.
- Bounded retries on Anthropic 429/5xx; bounded conversation pruning to keep token cost from compounding.
- Documentation: README, PROMPTS guide, STRICTNESS guide, PROVIDERS roadmap.
- Examples: `basic.yml`, `label-gated.yml`, `strict.yml`, `custom-prompt.yml`.
- `code_check` workflow gating every PR/push to `main` (compile, `action.yml`
  contract validation, actionlint, unit tests).
- `auto-release` workflow: SemVer bump from Conventional Commits on merge to
  `main`, tag + major-alias move + GitHub Release (tag-only, no commit to
  protected `main`).
- Stdlib-`unittest` test suite under `tests/` for the runtime's pure logic.
- Self-review workflow dogfooding the action on its own PRs.
- Repo hygiene: issue/PR templates and Dependabot for GitHub Actions.

[Unreleased]: https://github.com/DailybotHQ/ai-pr-reviewer/compare/v1.0.0...HEAD
[1.0.0]: https://github.com/DailybotHQ/ai-pr-reviewer/releases/tag/v1.0.0
