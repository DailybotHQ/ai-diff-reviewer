---
description: Smoke-test a prompt change against a real PR and capture the before/after
---

# Prompt Test

Smoke-test a change to `prompts/default.md` (or a custom prompt file) by running the reviewer against a real PR with both the OLD and NEW prompt. The output is the before/after evidence that should accompany every prompt-engineering PR.

## When to run

- Before merging any non-trivial change to `prompts/default.md`.
- When investigating "the bot keeps misclassifying X" — to verify the proposed fix actually fixes the misclassification.
- When auditing an existing prompt for a class of failures.

## Pre-flight

You will need:

- `ANTHROPIC_API_KEY` exported in the shell.
- `GITHUB_TOKEN` exported with `pull-requests: write` on a sandbox repo.
- A target PR — ideally one that exercises the area the prompt change addresses.
- Access to checkout both the OLD and NEW prompt versions (e.g. `git stash` or two worktrees).

## Steps

### 1. Pick the test PRs

For a substantive prompt change, smoke-test against **3–5 PRs** covering:

- A feature PR.
- A bugfix PR.
- A refactor PR.
- A docs-only PR.
- An "easy" PR with little to flag.

For a targeted prompt change (fixing one misclassification), one representative PR is enough.

### 2. Run with the OLD prompt

Check out the prompt file as it was before your change:

```bash
git stash                           # or check out a separate worktree
```

Run the reviewer locally:

```bash
export AIPRR_PROVIDER=anthropic
export AIPRR_API_KEY=$ANTHROPIC_API_KEY
export AIPRR_GH_TOKEN=$GITHUB_TOKEN
export AIPRR_REPO=DailybotHQ/ai-pr-reviewer
export AIPRR_PR_NUMBER=<test-pr>
export AIPRR_HEAD_SHA=$(gh api repos/DailybotHQ/ai-pr-reviewer/pulls/<test-pr> --jq .head.sha)
export AIPRR_BASE_REF=main
export AIPRR_ACTION_PATH=$PWD
export AIPRR_STRICTNESS=lenient
export AIPRR_TRACKING_COMMENT=false   # don't post — capture locally
export AIPRR_COLLAPSE_PREVIOUS=false  # don't disturb the PR conversation

python3 scripts/reviewer.py 2>&1 | tee /tmp/old-prompt-pr-<n>.log
```

Capture:
- The summary content.
- The list of inline comments with their severities.
- The total tool calls made.

### 3. Run with the NEW prompt

```bash
git stash pop                       # restore your changes
```

Run again with the same target PR. Capture the same metrics.

### 4. Compare

Produce a short before/after table:

| Metric | OLD | NEW |
|---|---|---|
| Inline comments posted | | |
| Severities (critical/warning/info) | | |
| Tool calls (read_file/grep/glob) | | |
| Issues correctly flagged | | |
| Issues missed | | |
| False positives | | |

For each substantive difference, note whether it's better (the change worked), worse (regression), or unchanged (the change had no effect on this PR).

### 5. Repeat for the next PR

Continue through your 3–5 target PRs.

### 6. Aggregate

Summarise across all tested PRs:

- **Net positive findings** that the new prompt catches and the old one missed.
- **Net negative findings** that the new prompt mis-flags or that the old one correctly flagged but the new one misses.
- **Severity calibration changes** — did some `warning`s become `critical`? Did `info` get inflated to `warning`?

The summary goes into the PR description for the prompt change. Without this evidence, the prompt change is opinion, not engineering.

## Output template

Paste this into the prompt-change PR description:

```markdown
## Prompt smoke-test

Tested against 4 PRs covering feature/bugfix/refactor/docs.

### Net findings

**New prompt catches:**
- <PR-link>: now flags X as critical (old prompt missed)
- <PR-link>: better wording on the suggestion block

**New prompt misses (regression):**
- <PR-link>: old prompt flagged Y as warning; new prompt misses

**Calibration changes:**
- Severity distribution shifted from 60/30/10 (info/warning/critical) to 50/35/15 across the test set

### Verdict

<one sentence — does the change merit shipping? With what caveats?>
```

## Reminder

This skill is not a substitute for the `self-review.yml` workflow that runs on every PR. Self-review confirms the prompt **runs**; this skill confirms the prompt **reviews well**. Both gates should pass before merging.
