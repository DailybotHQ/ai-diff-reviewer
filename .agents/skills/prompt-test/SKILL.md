---
name: prompt-test
description: Smoke-test a prompt change by running the reviewer with the OLD and NEW prompt against the same target PR(s) and capturing a before/after comparison. Required evidence for any non-trivial prompts/default.md change.
disable-model-invocation: false
allowed-tools: Read, Write, Edit, Glob, Grep, Bash
model: sonnet
tier: 2
intent: evaluate
max-files: 5
max-loc: 100
---

# Skill: Prompt Test

## Objective

Provide before/after evidence that a change to `prompts/default.md` (or a custom prompt file) actually improves review quality. The skill runs the reviewer twice against the same PR — once with the OLD prompt, once with the NEW — and produces a comparison the user can paste into a PR description.

Without this evidence, prompt changes are opinion, not engineering.

## Non-goals

- Does NOT modify the prompt for the user. The user owns the prompt edit; this skill evaluates it.
- Does NOT post comments on the test PR — runs are configured with `AIPRR_TRACKING_COMMENT=false` and `AIPRR_COLLAPSE_PREVIOUS=false` to avoid disturbing the target PR's conversation.

## Inputs

- `target_prs` — list of PR numbers to test against. For substantive changes use 3–5 covering different change types (feature, bugfix, refactor, docs). For targeted changes one PR is enough.
- `repo` — `<owner>/<name>` of the target repo. Defaults to the repo the skill is invoked from.

## Pre-flight

```bash
# API keys present?
test -n "$ANTHROPIC_API_KEY" || { echo "Set ANTHROPIC_API_KEY"; exit 1; }
test -n "$GITHUB_TOKEN"      || { echo "Set GITHUB_TOKEN"; exit 1; }

# On a branch with the prompt change?
git diff main...HEAD -- prompts/default.md | head -1
```

If the prompt isn't actually changed in the working tree, ask the user whether they intended to test a different file.

## Steps

For each target PR `N` in `target_prs`:

### 1. Capture HEAD SHA of target PR

```bash
HEAD_SHA=$(gh api "repos/${REPO}/pulls/${N}" --jq .head.sha)
```

### 2. Run with OLD prompt

```bash
git stash                       # set aside the new prompt

export AIPRR_PROVIDER=anthropic
export AIPRR_API_KEY=$ANTHROPIC_API_KEY
export AIPRR_GH_TOKEN=$GITHUB_TOKEN
export AIPRR_REPO=$REPO
export AIPRR_PR_NUMBER=$N
export AIPRR_HEAD_SHA=$HEAD_SHA
export AIPRR_BASE_REF=main
export AIPRR_ACTION_PATH=$PWD
export AIPRR_STRICTNESS=lenient
export AIPRR_TRACKING_COMMENT=false       # capture, don't post
export AIPRR_COLLAPSE_PREVIOUS=false       # don't disturb the PR

python3 scripts/reviewer.py 2>&1 | tee "/tmp/old-${N}.log"
```

Capture from the log:
- The summary (the model's `submit_review` argument).
- The list of inline comments with severities.
- The number of tool calls.
- The number of turns the loop ran.

### 3. Run with NEW prompt

```bash
git stash pop                  # restore the new prompt

python3 scripts/reviewer.py 2>&1 | tee "/tmp/new-${N}.log"
```

Same captures.

### 4. Compare

Produce a per-PR comparison:

| Metric | OLD | NEW |
|---|---|---|
| Inline comments | N1 | N2 |
| Severities (critical/warning/info) | a/b/c | x/y/z |
| Tool calls | M1 | M2 |
| Turns | T1 | T2 |

Plus a qualitative bullet list:
- "New flags X that old missed." (better)
- "New misses Y that old caught." (regression)
- "Severity of Z shifted from `info` → `warning`." (calibration change)

## Aggregation across multiple PRs

After running each PR, aggregate:

```markdown
## Prompt smoke-test summary

Tested against N PRs covering <types>.

### Net findings
- **New catches old missed:** <list with PR links>
- **New misses old caught:** <list with PR links>
- **Calibration shifts:** <summary>

### Verdict
<one sentence: ship / refine / abandon>
```

The summary belongs in the PR description for the prompt change.

## Output

Print the summary to stdout. Optionally write it to `tmp/prompt-test-<branch>.md` if the user wants to attach a file.

## Common failure modes

- **Anthropic rate limits.** Add `time.sleep(5)` between PRs if you're testing >5 in a row.
- **Stash conflict.** If `git stash pop` fails, the user has uncommitted changes elsewhere; stop and ask.
- **Target PR closed.** PRs in closed/merged state still work for read-only review; skip if the diff is gone (rare).
- **Provider cost.** Each run is one full review at the configured model. For 5 PRs × 2 runs = 10 reviews. Estimate cost up front; if that's a problem use a cheaper model via `AIPRR_MODEL` for the smoke test.
