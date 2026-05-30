# Standards

Repository conventions that apply across all contributions. For Python-specific guidelines see [DEVELOPMENT_GUIDELINES.md](DEVELOPMENT_GUIDELINES.md). For documentation see [DOCUMENTATION_GUIDE.md](DOCUMENTATION_GUIDE.md). The non-negotiables also live in [AGENTS.md](../AGENTS.md).

## Branding and naming

- **Product name (user-facing):** "AI PR Reviewer". Capitalise exactly that way in user-facing copy (README, docs, marketplace listing, error messages, comments visible to users).
- **Slug:** `ai-pr-reviewer` (lowercase, hyphenated). This is the GitHub repo name and the module identifier in `uses:` lines.
- **Env-var prefix:** `AIPRR_` (4 characters: AI PR Reviewer). Internal use only — but stable, because it's referenced in local-dev docs and `CONTRIBUTING.md`.
- **Marker constant:** `<!-- ai-pr-reviewer-marker -->`. Embedded in the tracking comment so downstream automation can locate the most recent review.

Don't invent variants like "AI-PR-reviewer", "AIPR", "AiPrReviewer", etc. The single canonical capitalisation makes search consistent across the marketplace, GitHub, and docs.

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

- **Runtime:** `scripts/reviewer.py`. One file. Add helpers as functions, not new files, unless adding a new file is unavoidable (e.g. provider-specific code that's >300 LOC and warrants isolation — even then, prefer a single file).
- **Default prompt:** `prompts/default.md`. Keep one. Don't fork into multiple defaults.
- **User-facing docs:** `docs/STRICTNESS.md`, `docs/PROMPTS.md`, `docs/PROVIDERS.md`. Each ~300–500 lines max; split if longer.
- **Contributor docs:** `docs/ARCHITECTURE.md`, `docs/SECURITY.md`, etc.
- **Examples:** `examples/<scenario>.yml`. One file per scenario, with comments explaining the intent. Keep them runnable as-is — they pin `DailybotHQ/ai-pr-reviewer@v1`.
- **AI-agent config:** `.agents/`. Edit there, never at `.claude/...`. The `.claude` symlink is for back-compat only.

## English only

All code, comments, documentation, commit messages, and PR descriptions are in English. The action ships globally; a Spanish (or any non-English) artefact is a usability bug for everyone outside that language.

## File size

- `scripts/reviewer.py` — keep under 3000 LOC. We're at ~1500 today. If the file approaches the limit, the conversation is "should we split into multiple files" — make that decision deliberately, not by drift.
- Doc files — under 500 lines. Long docs are signal that they need to be split.
- Examples — under 50 lines each. They're showcase, not reference.

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
- `self-review.yml` is the integration gate (dogfooding).
- Unit tests are welcome for pure-function logic only, using stdlib `unittest`.

## Security

See [SECURITY.md](SECURITY.md). The summary:

- Stdlib runtime, zero external deps.
- All paths through `safe_repo_path()`.
- No `shell=True` in subprocess.
- Tool args logged with `redact_for_log()`.
- Vulnerabilities reported via private GitHub security advisory.

## Documentation

See [DOCUMENTATION_GUIDE.md](DOCUMENTATION_GUIDE.md). The summary:

- README is marketplace-facing — short, scannable, one quick-start, full input table.
- `docs/` splits user-facing (STRICTNESS, PROMPTS, PROVIDERS) from contributor-facing (ARCHITECTURE, SECURITY, TESTING_GUIDE, etc.).
- `AGENTS.md` is the source of truth that every other doc points back to.
- Keep `CHANGELOG.md` honest. One entry per behaviour change.

## Versioning

SemVer. Tags `vX.Y.Z`. The moving major tag (`v1`) auto-updates on every `v1.x.y` publish via `release.yml`. Don't delete published tags.

## License

MIT for everything in this repo unless a specific file says otherwise. By contributing you agree that your contribution is licensed under the same.
