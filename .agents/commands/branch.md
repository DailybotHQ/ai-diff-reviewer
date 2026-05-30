---
description: Generate a branch name from a short description of intent
---

# Branch Name Generator

Generate a branch name in the project's convention from a short user description of intent.

## Format

```
<type>/<short-kebab-description>
```

`<type>` matches the Conventional Commits type: `feat`, `fix`, `docs`, `chore`, `refactor`, `test`, `ci`, `perf`, `style`.

The description is kebab-case (lowercase, hyphens), short — ideally under 30 characters, hard cap at 50.

## Rules

- Use `feat/` for new features (new input, new provider, new tool).
- Use `fix/` for bug fixes.
- Use `docs/` for documentation-only changes.
- Use `chore/` for releases, dependency bumps, generated-file updates.
- Use `refactor/` for code structure changes without behaviour change.
- Use `ci/` for workflow / release-tooling changes.

If the user's intent doesn't clearly map to one type, ask before guessing. Don't invent new types.

## Examples

| Intent | Branch name |
|---|---|
| Add an OpenAI provider | `feat/openai-provider` |
| Fix the 422 fallback edge case for empty comments | `fix/422-fallback-empty-comments` |
| Rewrite the strictness doc | `docs/strictness-rewrite` |
| Cut v1.2.0 release | `chore/release-v1.2.0` |
| Refactor the tool-dispatch function | `refactor/tool-dispatch` |
| Add a CI step that lints YAML | `ci/yaml-lint` |

## Output

After generating, present the branch name to the user. Don't run `git checkout -b` automatically — the user might want to refine the name first.

If the user agrees, suggest:

```bash
git checkout -b <generated-name>
```
