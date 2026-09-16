# `tests/eval/` — review-quality evaluation (offline, labelled corpus)

Not part of `unittest discover` (no `test_*.py` here). `run_eval.py` runs the
action's own review loop against a **merged** PR without posting anything,
then scores the result against `corpus.json`.

```bash
# one run (in-process runner; key comes from the named env var, never the CLI)
python3 tests/eval/run_eval.py run --repo DailybotHQ/ai-diff-reviewer --pr 46 \
  --worktree /path/to/worktree-at-pr-46-head --provider openai \
  --api-base https://api.x.ai/v1 --model grok-4.6 --api-key-env XAI_API_KEY \
  --out results/xai-46.json

# agent-runner (the CLI must be installed locally)
python3 tests/eval/run_eval.py run --repo DailybotHQ/ai-diff-reviewer --pr 46 \
  --worktree /path/to/worktree-at-pr-46-head --provider grok --model grok-4.3 \
  --api-key-env XAI_API_KEY --out results/grok-46.json

# table across runs
python3 tests/eval/run_eval.py score results/*.json
```

Prerequisites: `gh auth token` (the PR context is fetched from GitHub), a
worktree checked out at the PR head (`git worktree add <dir> <head-sha>`), the
vendor credential in the environment.

## Corpus labels (`corpus.json`)

Per PR: `must_find` (defects a good review must report — recall), `acceptable`
(true but optional observations — neither reward nor penalty), `must_not_flag`
(known false positives — each hit is a penalty). A label matches a finding on
the same `path` within `window` lines (default 25) when any `keywords` entry
appears in the body (`all_keywords: true` requires all). `severity` on a
`must_find` label enables the severity-match metric.

## Metrics per run

must-find recall · false positives · unlabelled findings (neither labelled
true nor false — inspect before calling them noise) · severity match ·
summary present (contract compliance) · suggestion blocks · coverage paths ·
turns · tokens (in incl. cache reads, out) · cost (vendor-reported for CLIs
that expose it, estimated otherwise) · wall clock.

Adding a PR: pick a merged PR with at least one defect that was later fixed,
label it from the fix commits (not from a model's output), and record the
date; extend the label set only from evidence.
