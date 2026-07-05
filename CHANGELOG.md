# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

_No changes yet._

## [1.1.0] — 2026-07-05

**Headline:** three new agent-runner providers (`claude-code`, `cursor`, `codex`) alongside the incumbent `anthropic` chat-completions provider — zero migration cost for consumers on `@v1`. See [`.dwp/plans/PLAN_multi_cli_provider_expansion/analysis_results/EXECUTIVE_REPORT.md`](.dwp/plans/PLAN_multi_cli_provider_expansion/analysis_results/EXECUTIVE_REPORT.md) for the full breakdown.

### Added
- **Multi-CLI provider expansion** — three new agent-runner providers that shell out to their vendor's coding-agent CLI in headless mode and receive findings via a file-based contract (`.aiprr/findings.json`):
  - `provider: claude-code` — installs `@anthropic-ai/claude-code` via npm; auth via `ANTHROPIC_API_KEY`.
  - `provider: cursor` — installs `cursor-agent` via `curl` (`cursor.com/install`); auth via `CURSOR_API_KEY`.
  - `provider: codex` — installs `@openai/codex` via npm; auth via `OPENAI_API_KEY`.
- New abstract `AgentRunnerProvider` peer of `Provider`. `build_provider()` now returns either family; `main()` dispatches on `isinstance`.
- New `Finding` + `ReviewResult` dataclasses provide the provider-independent submission-path payload.
- New `parse_findings_file()` parser + validator with strict schema enforcement (required fields, allowed severity/side enums, forward-compat with vendor extensions).
- New `write_findings_prompt_directive()` — standardises the "write your findings here" instruction appended to review prompts across all CLI providers.
- New optional inputs: `agent-max-turns`, `agent-extra-args`, `mcp-config-file`, `claude-code-version`, `cursor-version`, `codex-version`.
- Modular install in `action.yml`: each CLI install step is guarded by `if: inputs.provider == '...'`, so consumers picking the default `provider: anthropic` pay zero install overhead. One provider = one install.
- MCP servers passthrough: `mcp-config-file` copies the consumer's JSON config into the CLI's expected location (with round-trip backup) before invocation.
- New examples: `provider-claude-code.yml`, `provider-cursor.yml`, `provider-codex.yml`, `mcp-passthrough.yml`.
- New CI job `cli-install-smoke` — matrix over the three CLI providers exercising each installer script on a fresh runner, catching installer drift before it reaches consumers.
- Dogfooding matrix in `.github/workflows/self-review.yml` — every PR to this repo now runs a 4-leg review (`anthropic`, `claude-code`, `cursor`, `codex`) with per-provider `self-reviewed:*` labels.
- 67 new unit tests (109 total, up from 42) covering: adapter (state → ReviewResult), findings.json parser (happy + error paths), provider dispatch, MCP passthrough, subprocess boundary, security invariants (no `shell=True`, no `os.system`, all `extra_args` funnel through `shlex.split`), CLI env allowlist, and end-to-end serialization roundtrips across both provider families.

### Changed
- `gh_submit_review_with_fallback()` now accepts a `ReviewResult` (was: `body` + `inline_comments`). The submission path is provider-agnostic; findings are encoded to the GitHub Reviews inline shape at the boundary via `findings_to_gh_inline_comments()`.
- Refreshed `docs/PROVIDERS.md` with the Agent Runner Provider Contract section documenting the schema, validation, and prompt directive.
- Refreshed `docs/ARCHITECTURE.md` with the two-provider-family design decision and the modular-install approach.
- Refreshed `README.md` inputs table + provider roadmap with the four shipping providers, categorised by family.
- Refreshed `.agents/agents/provider-implementer.md`, `.agents/skills/add-provider/SKILL.md`, `.agents/agents/reviewer.md`, and `.agents/docs/skills_agents_catalog.md` for the two-family model.

### Fixed
- N/A — additive release. Existing `provider: anthropic` consumers see zero behavioural drift.

### Security
- `_invoke_cli_agent()` enforces argv-list subprocess invocation (no `shell=True`).
- All consumer-provided `agent-extra-args` are parsed with `shlex.split` before being appended to the CLI invocation.
- MCP config passthrough uses `shutil.copyfile` (not `shell=True` copy) and round-trips any pre-existing user config so an interrupted run doesn't leave stale state.
- **New `_build_cli_env(extra_vars=...)` helper** — vendor CLI subprocesses receive an explicit env allowlist (`PATH`, `HOME`, `NODE_PATH`, locale, runner metadata) plus the vendor API key only. `AIPRR_GH_TOKEN` and all other `AIPRR_*` variables stay in the parent process; enforced by static `CliEnvAllowlistTests`. Addresses Security-Review Finding #2.
- **`max-inline-comments` cap now enforced on the agent-runner path** — previously only enforced by the chat-completions tool handler. `main()` truncates `result.findings` to `max_inline_comments` after `provider.run_review()` and recomputes `overall_severity` on the retained subset. Addresses Security-Review Finding #1.
- **Documented accepted risks** in `docs/SECURITY.md`: (a) Cursor installer supply chain (`curl | bash`, no signed installer offered by vendor); (b) MCP config persistence after SIGKILL on self-hosted persistent runners.

### CI
- `code_check.yml` gains a `cli-install-smoke` matrix job (claude-code / cursor / codex).
- `self-review.yml` becomes a 4-leg matrix; `fail-fast: false` + `timeout-minutes: 25`.

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

[Unreleased]: https://github.com/DailybotHQ/ai-pr-reviewer/compare/v1.1.0...HEAD
[1.1.0]: https://github.com/DailybotHQ/ai-pr-reviewer/releases/tag/v1.1.0
[1.0.0]: https://github.com/DailybotHQ/ai-pr-reviewer/releases/tag/v1.0.0
