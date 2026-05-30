---
description: Run a focused code review on the current branch
---

# Code Review

Run a focused review on the current branch's changes (`git diff <base>...HEAD`) using this repo's standards. The review checks for the things that matter most in this codebase: stdlib-only constraint, type hints, action.yml contract stability, error patterns, path/subprocess safety, doc-in-sync, and the marker/exit-code public surface.

This is a quick local-pass review. For a deeper sub-agent review, spawn the `reviewer` agent (see `.agents/agents/reviewer.md`).

## Steps

1. **Determine the base.** `git rev-parse --abbrev-ref --symbolic-full-name @{u}` for the upstream, falling back to `main`.
2. **Read the diff.** `git diff <base>...HEAD --stat` first, then full diff.
3. **Read related files** for context — at minimum the file the change lives in, plus its immediate neighbours.
4. **Run the checklist below** in order; stop at the first major finding so the user can decide whether to fix before continuing.

## Checklist

### Critical (block merge if hit)

- [ ] **No new non-stdlib imports** in `scripts/reviewer.py`. Check every new `import` line.
- [ ] **`action.yml` inputs/outputs unchanged** OR the change is documented as breaking and the major version is bumped.
- [ ] **No `subprocess` with `shell=True`.**
- [ ] **No paths bypassing `safe_repo_path()`.**
- [ ] **No log statements that leak secrets** (anything with `os.environ["AIPRR_API_KEY"]` or similar in a print/log).
- [ ] **No edits at `.claude/...` or `CLAUDE.md`** — those are symlinks; the canonical paths are `.agents/...` and `AGENTS.md`.
- [ ] **The marker constant `<!-- ai-pr-reviewer-marker -->` is unchanged** (or this is a deliberate major break with a documented migration path).

### Warnings (push back unless author has a good reason)

- [ ] **All new functions have type hints** on parameters and return type.
- [ ] **Broad `except Exception`** has `# noqa: BLE001` AND a comment explaining why.
- [ ] **Magic numbers are promoted to module-level constants.**
- [ ] **New tools update `tools_schema()`** with valid JSONSchema.
- [ ] **README's input/output table reflects** any `action.yml` change.
- [ ] **`CHANGELOG.md` has an entry under `[Unreleased]`** for any behaviour change.
- [ ] **A new input has at least one example** in `examples/`.
- [ ] **Prompt change has a before/after** linked in the PR description.
- [ ] **Self-review.yml ran successfully** on the latest push.

### Nits (mention if convenient)

- [ ] Naming follows project conventions (`tool_*` for tool functions, `gh_*` for GitHub helpers).
- [ ] Imports are ordered: `__future__` → stdlib `import` (alphabetical) → stdlib `from ... import` (alphabetical).
- [ ] Section banners (`# ---`) preserved when adding to existing sections.
- [ ] Comments explain *why*, not *what*.

## Output

Produce a Markdown report:

```markdown
# Code Review — <branch>

**Verdict:** approve / request-changes / comment-only

## Findings

| # | Severity | Location | Summary |
|---|---|---|---|
| 1 | 🚨 critical | `scripts/reviewer.py:312` | Adds `requests` import — violates stdlib-only constraint |
| 2 | ⚠️ warning  | `action.yml:42` | New input `foo-bar` lacks default and isn't documented in README |
| 3 | ℹ️ info     | `docs/PROMPTS.md:78` | Typo: "wich" → "which" |

## Detailed findings

### #1 — `scripts/reviewer.py:312`

<concrete failure mode + fix suggestion>

### #2 — `action.yml:42`

<concrete failure mode + fix suggestion>

## Cross-cutting observations

<anything that didn't fit a single line>

## Recommendation

<one sentence — approve / request-changes / comment-only — and why>
```

## Tone

Same tone as the bundled default review prompt: direct, specific, charitable. Don't pad. The author knows the codebase as well as you do; tell them the concrete failure mode you're worried about and let them decide.
