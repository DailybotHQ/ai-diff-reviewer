---
description: Generate a Conventional Commits message based on the staged diff
---

# Commit Message Generator

Generate a commit message that follows the Conventional Commits format used by this repo. Use the staged diff as the source of truth.

## Steps

1. Run `git diff --staged` to see the staged changes.
2. Run `git status` to see which files are involved.
3. Run `git log --oneline -10` to match the project's style.
4. Identify the change category and write a message following the format below.

## Format

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

## Allowed types

| Type | Use for |
|---|---|
| `feat` | A new user-visible feature (new input, new output, new tool). |
| `fix` | A bug fix in runtime behaviour. |
| `docs` | Documentation-only changes. |
| `chore` | Maintenance that doesn't fall in other categories (deps, config). |
| `refactor` | Code structure change without behaviour change. |
| `test` | Adding or fixing tests. |
| `ci` | Workflow or release-tooling changes. |
| `perf` | Performance improvement. |
| `style` | Formatting / whitespace only. |

## Common scopes

- `provider` — additions to `Provider` implementations.
- `prompt` — changes to `prompts/default.md`.
- `action` — changes to `action.yml`.
- `runtime` — changes to `scripts/reviewer.py`.
- `docs` — documentation.
- `ci` — workflow or release tooling.

## Rules

- Subject line under 70 characters.
- Subject line in imperative mood ("add", not "added").
- Body wrapped at ~80 characters.
- "Why" first; the "what" is in the diff.
- If the change touches `action.yml` inputs/outputs, the body MUST mention whether the change is backwards-compatible.
- If the change is a runtime behaviour change, the body MUST reference the corresponding `CHANGELOG.md` entry.

## Example

```
feat(provider): add OpenAI provider

## Summary
First non-Anthropic provider — translates Anthropic-shape messages and
tool calls to OpenAI's chat-completions schema at the boundary so the
rest of the runtime is unchanged.

## Change Log
- New OpenAIProvider class with tool-call translation in both directions
- New default model entry: openai → gpt-4o
- New optional input api-base for self-hosted OpenAI-compatible endpoints
- Updated docs/PROVIDERS.md to flip OpenAI status to shipping
- CHANGELOG entry under [Unreleased]

## Risks
- Translation layer is the only meaningful new surface; covered by smoke
  test on PR #42 (provider: openai). No change to existing Anthropic path.
```

After generating the message, present it to the user for approval before running `git commit`.
