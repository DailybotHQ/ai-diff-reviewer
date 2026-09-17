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
- Does NOT post comments on the test PR — `tests/eval/run_eval.py` runs the review loop in-process with the GitHub submission path stubbed out, so the target PR's conversation is never touched.

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

Run everything through the in-tree harness `tests/eval/run_eval.py` (stdlib,
offline — it fetches the PR context and runs the review loop **without posting
anything**; never run `scripts/reviewer.py` against a live PR to smoke-test a
prompt). Pick target PRs from `tests/eval/corpus.json` whenever possible so
the comparison is scored against labelled expectations, not eyeballed.

### 1. Check out each target PR at its head

```bash
HEAD_SHA=$(gh api "repos/${REPO}/pulls/${N}" --jq .head.sha)
git worktree add "/tmp/aiprr-pr-${N}" "$HEAD_SHA"
```

### 2. Run with the OLD prompt

```bash
git show main:prompts/default.md > /tmp/prompt-old.md     # or the previous tag
python3 tests/eval/run_eval.py run --repo "$REPO" --pr "$N" --worktree "/tmp/aiprr-pr-${N}" \
  --provider openai --api-base "$AZURE_OPENAI_BASE_URL" --model "$AZURE_OPENAI_MODEL_DAILY" \
  --api-key-env AZURE_OPENAI_API_KEY --prompt /tmp/prompt-old.md --out "results/old-${N}.json"
```

Any runner works (`--provider anthropic --api-key-env ANTHROPIC_API_KEY` for
the default backend; `--provider grok --model grok-4.5` for the Grok CLI when
it is installed). Use the same runner and model for OLD and NEW.

### 3. Run with the NEW prompt

```bash
python3 tests/eval/run_eval.py run ... --prompt prompts/default.md --out "results/new-${N}.json"
```

### 4. Compare

```bash
python3 tests/eval/run_eval.py score results/old-*.json results/new-*.json
```

The table gives, per run: must-find recall, false positives (labelled
`must_not_flag`), unlabelled findings (inspect them — they are either new true
positives to add to the corpus or noise), severity match, summary present,
suggestion blocks, turns, tokens, cost, wall clock. Add a qualitative bullet
list from the JSON bodies:
- "NEW flags X that OLD missed." (better)
- "NEW misses Y that OLD caught." (regression)
- "Severity of Z shifted from `info` → `warning`." (calibration change)

Ship only when recall is ≥ and false positives ≤ on every model you could run;
record blocked models honestly (no key, quota) instead of assuming.

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

- **Comparing across different runners or models.** OLD and NEW must share runner + model; the runner's own tool loop changes the result more than most prompt edits.
- **Trusting finding counts.** A prompt that doubles false positives "finds more"; the corpus score is the metric.


- **Anthropic rate limits.** Add `time.sleep(5)` between PRs if you're testing >5 in a row.
- **Target PR closed.** PRs in closed/merged state still work for read-only review; skip if the diff is gone (rare).
- **Provider cost.** Each run is one full review at the configured model. For 5 PRs × 2 runs = 10 reviews. Estimate cost up front; if that's a problem use a cheaper model via `AIPRR_MODEL` for the smoke test.
