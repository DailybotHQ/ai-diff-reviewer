# Standards

Repository conventions that apply across all contributions. For Python-specific guidelines see [DEVELOPMENT_GUIDELINES.md](DEVELOPMENT_GUIDELINES.md). For documentation see [DOCUMENTATION_GUIDE.md](DOCUMENTATION_GUIDE.md). The non-negotiables also live in [AGENTS.md](../AGENTS.md).

## Branding and naming

- **Product name (user-facing):** "AI Diff Reviewer". Capitalise exactly that way in user-facing copy (README, docs, marketplace listing, error messages, comments visible to users).
- **Slug:** `ai-diff-reviewer` (lowercase, hyphenated) — matches both the GitHub repo (`DailybotHQ/ai-diff-reviewer`) and the Marketplace listing. The old `ai-pr-reviewer` slug still resolves via GitHub's permanent 301 redirect on renamed repos, so pre-rename `uses:` pins keep working; new copy-paste examples should always use the canonical slug.
- **Env-var prefix:** `AIPRR_` (private contract, unchanged across the rename — internal only, but stable because it's referenced in local-dev docs and `CONTRIBUTING.md`).
- **Marker constants:** `<!-- ai-pr-reviewer-marker -->`, `<!-- ai-pr-reviewer-state: … -->`, `<!-- ai-pr-reviewer-provider:… -->`, `<!-- ai-pr-reviewer-description-autocompleted -->` — deliberately preserved as-is across the rename so already-posted PR tracking comments continue to be detected by `collapse-previous` and state-lookup logic.

Don't invent variants like "AI-Diff-Reviewer", "AIDR", "AiDiffReviewer", "AI PR Reviewer" (the old name), etc. The single canonical capitalisation makes search consistent across the marketplace, GitHub, and docs.

## Commits

Conventional Commits. Format:

```
<type>(<optional-scope>): <short description>

## Summary
<1–2 sentences — the why, not the what>

## Change Log
- <bullet 1>
- <bullet 2>

## Risks
- <risk 1, or "None — content-only change">
```

Allowed types: `feat`, `fix`, `docs`, `chore`, `refactor`, `test`, `ci`, `perf`, `style`. Scope is optional; common scopes:

- `provider` — additions to `Provider` implementations
- `prompt` — changes to `prompts/default.md`
- `action` — changes to `action.yml`
- `runtime` — changes to `scripts/reviewer.py`
- `docs` — documentation
- `ci` — workflow or release tooling

## Branch names

`<type>/<short-kebab-description>`, where `<type>` matches the commit type. Examples:

- `feat/openai-provider`
- `fix/422-fallback-empty-comments`
- `docs/strictness-rewrite`

Keep branch names short. Long branches show up in `gh pr view` and break terminal layouts.

## Pull requests

- Title in Conventional Commits format (the same as the squash-merge subject).
- Description follows the same `## Summary / ## Change Log / ## Risks` structure as commits.
- Link to any related issue (`Fixes #123`).
- Reference the self-review run that validated the change (or describe the manual verification if dogfooding can't cover it).

## File layout

- **Runtime:** `scripts/reviewer.py`. One file. Add helpers as functions, not new files, unless adding a new file is unavoidable (e.g. provider-specific code that's >300 LOC and warrants isolation — even then, prefer a single file). The four v1.1.0 CLI provider impls each stayed well under that budget by sharing the `_invoke_cli_agent` / `_build_cli_env` helpers rather than each rolling their own.
- **Tests:** `tests/test_<area>.py`. Split by concern (core, findings parser, agent-runner providers, roundtrip). See [DEVELOPMENT_GUIDELINES.md](DEVELOPMENT_GUIDELINES.md#test-discipline).
- **Default prompt:** `prompts/default.md`. Keep one. Don't fork into multiple defaults.
- **User-facing docs:** `docs/STRICTNESS.md`, `docs/PROMPTS.md`, `docs/PROVIDERS.md`. Each ~300–500 lines max; split if longer.
- **Contributor docs:** `docs/ARCHITECTURE.md`, `docs/SECURITY.md`, etc.
- **Examples:** `examples/<scenario>.yml`. One file per scenario, with comments explaining the intent. Keep them runnable as-is — they pin `DailybotHQ/ai-diff-reviewer@v2`.
- **AI-agent config:** `.agents/`. Edit there, never at `.claude/...`. The `.claude` symlink is for back-compat only.

## English only

All code, comments, documentation, commit messages, and PR descriptions are in English. The action ships globally; a Spanish (or any non-English) artefact is a usability bug for everyone outside that language.

## File size

- `scripts/reviewer.py` — soft ceiling ~4500 LOC. We're at ~4000 today (up from ~1500 pre-v1.1.0; the v1.1.0 growth was the two provider families plus the three CLI provider impls, and v1.3–v1.4 added the author-association gate + PR-metadata check tools + PR-description autocomplete + complexity-labeling paths). If the file approaches the limit, the conversation is "should we split into multiple files" — make that decision deliberately, not by drift. The next feature that would push us past the ceiling (a raw-OpenAI/Gemini provider, `.aiprr/findings.json` v2) is likely the trigger for that conversation.
- Doc files — under 500 lines. Long docs are signal that they need to be split.
- Examples — under 50 lines each. They're showcase, not reference.
- Test files — under 500 lines. Split by concern rather than growing an existing file (the current four-file split is the model).

## Whitespace and formatting

- 4-space indentation, no tabs. Python convention.
- LF line endings. CRLF causes spurious diffs on Linux runners.
- Trailing newline at end of file.
- No trailing whitespace.
- Markdown: 80-char soft limit on prose lines (improves diff readability); code blocks and tables can extend.

CI doesn't enforce these rigidly; the formatter you use locally should produce them by default. We don't add `prettier` / `black` to CI for the reasons in [TESTING_GUIDE.md](TESTING_GUIDE.md).

## Comments in code

- Comments explain *why*, not *what*. The `what` should be obvious from the code.
- Single-line comments preferred. Multi-line block comments are reserved for module/section headers (the `# ---` separators in `scripts/reviewer.py`).
- Reference issues or PRs by number when documenting a workaround for a specific external bug (e.g. `# upstream issue: anthropic/claude-code-action#5`).
- Don't write comments like `# TODO: ...` without a tracking issue. If it's worth flagging, it's worth filing.

## Tests

See [TESTING_GUIDE.md](TESTING_GUIDE.md). The summary:

- `py_compile` is the static gate.
- `actionlint` is the workflow gate.
- The stdlib `unittest` suite in `tests/` is the unit gate (242 tests across four files, no third-party deps).
- `cli-install-smoke` is the CLI-installer gate (matrix over the three agent-runner providers).
- `self-review.yml` is the integration gate (always-on Anthropic baseline plus scoped CLI-provider dogfooding for provider-sensitive changes).

## Security

See [SECURITY.md](SECURITY.md). The summary:

- Stdlib runtime, zero external deps.
- All paths through `safe_repo_path()`.
- No `shell=True` in subprocess; `shlex.split()` on any user-provided arg string; `_build_cli_env()` scrubs the vendor-CLI environment via `_CLI_ENV_ALLOWLIST`.
- Tool args logged with `redact_for_log()`.
- Vulnerabilities reported via private GitHub security advisory.

## Documentation

See [DOCUMENTATION_GUIDE.md](DOCUMENTATION_GUIDE.md). The summary:

- README is marketplace-facing — short, scannable, one quick-start, full input table.
- `docs/` splits user-facing (STRICTNESS, PROMPTS, PROVIDERS) from contributor-facing (ARCHITECTURE, SECURITY, TESTING_GUIDE, etc.).
- `AGENTS.md` is the source of truth that every other doc points back to.
- Keep `CHANGELOG.md` honest. One entry per behaviour change.

## Versioning

SemVer. Tags `vX.Y.Z`. The moving major tag for the current line (`v2`) auto-updates on every `v2.x.y` publish via `release.yml`. Don't delete published tags.

## License

MIT for everything in this repo unless a specific file says otherwise. By contributing you agree that your contribution is licensed under the same.
