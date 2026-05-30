---
description: Generate a pull-request description based on the branch's commits
---

# PR Description Generator

Generate a PR description for the current branch using the same shape as the project's commit messages: `## Summary`, `## Change Log`, `## Risks`, plus a `## Test plan` section specific to PR descriptions.

## Steps

1. Find the base branch — usually `main`. Run `gh pr view --json baseRefName 2>/dev/null` if a PR already exists; otherwise default to `main`.
2. Run `git log <base>...HEAD --oneline` to enumerate the commits on this branch.
3. Run `git diff <base>...HEAD --stat` to summarise the file-level impact.
4. Run `git diff <base>...HEAD` for the full diff if needed.
5. Read `scripts/reviewer.py` and any other touched files for context.
6. Compose the PR description.

## Format

```
<short title in Conventional Commits format>

## Summary
<2–3 sentences: what the change does and why>

## Change Log
- <bullet 1>
- <bullet 2>

## Risks
- <risk 1, or "None — content-only change">

## Test plan
- [ ] `python3 -m py_compile scripts/reviewer.py` passes
- [ ] `actionlint` passes on `.github/workflows/`
- [ ] `.github/workflows/self-review.yml` ran successfully on this PR
- [ ] (additional checklist items specific to the change)
```

## Rules

- Title under 70 characters; same format as a Conventional Commits subject.
- Summary explains "why" first, "what" second.
- Change Log lists user-visible changes only — internal refactors that don't change behaviour can be one bullet ("internal: refactor X for clarity").
- Risks: be honest. "None" is acceptable when true; "the translation layer is the only new surface, covered by smoke test on PR #42" is much better than vague reassurance.
- Test plan: include the standard items above, plus PR-specific verification (e.g. "Smoke-tested on PR #42 with provider: openai — see review URL: ..." for a new-provider PR).

## Example

```
feat(strictness): add `block-on-warning` mode

## Summary
Teams that treat warnings as blockers can now configure the action to
fail the GitHub check when any inline comment is severity `warning` or
`critical`. Existing `lenient` and `block-on-critical` modes are
unchanged.

## Change Log
- New `strictness` value `block-on-warning`
- Updated `evaluate_strictness()` to handle the new mode
- README's strictness table updated
- docs/STRICTNESS.md extended with the new mode's calibration guidance
- New `examples/strict-warning.yml`
- CHANGELOG entry under [Unreleased]

## Risks
- New mode is opt-in; default `lenient` behaviour unchanged.
- Existing `block-on-critical` consumers are unaffected.

## Test plan
- [ ] `python3 -m py_compile scripts/reviewer.py` passes
- [ ] `actionlint` passes
- [ ] `.github/workflows/self-review.yml` ran with the default strictness on this PR (verifying default path)
- [ ] Manually verified `block-on-warning` on PR #43 in a sandbox repo (link in PR comment)
```

After generating, ask the user to confirm before opening the PR via `gh pr create`. Pass the body via a heredoc so formatting is preserved.
