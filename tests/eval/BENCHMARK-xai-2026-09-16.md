# xAI model benchmark — 2026-09-16

**Question.** Which xAI model is the cost-efficient default for review, and is grok-4.3 (the pre-2.3.0 default) actually reviewing?

**Method.** `tests/eval/run_eval.py` over the labelled corpus (`corpus.json`: PRs #46, #45, #43, #37 — 5 `must_find` defects, 2 `must_not_flag` traps), in-process runner (`--provider openai --api-base https://api.x.ai/v1`) so the runtime handles the tools and only the model varies; prompt `prompts/default.md` v3.1.1 with `.review/extension.md`; one run per model × PR (16 runs), single day, prices from `GET /v1/language-models`. Extra ("unlabelled") findings were read one by one; "valid" below is the maintainer's adjudication, not a score. Grok CLI spot checks (the route the dogfood leg uses) are listed separately.

## Aggregate (in-process, 4 PRs each)

| runner/model | PRs | must-find recall | FP | unlabelled | findings | no summary | total cost | avg cost/PR | avg wall | avg turns | hits per $ |
|---|---|---|---|---|---|---|---|---|---|---|---|
| openai/grok-4.5 | 37,43,45,46 | 3/5 (60%) | 0 | 6 | 10 | 0 | $1.10 | $0.27 | 184s | 7.8 | 2.7 |
| openai/grok-4.6 | 37,43,45,46 | 3/5 (60%) | 0 | 5 | 7 | 0 | $1.20 | $0.30 | 724s | 10.2 | 2.5 |
| grok/grok-4.5 | 45 | 1/2 (50%) | 0 | 2 | 3 | 0 | $0.75 | $0.75 | 194s | 7.0 | 1.3 |
| openai/grok-build-0.1 | 37,43,45,46 | 1/5 (20%) | 0 | 3 | 4 | 2 | $1.09 | $0.27 | 373s | 20.0 | 0.9 |
| openai/grok-4.3 | 37,43,45,46 | 0/5 (0%) | 0 | 0 | 0 | 1 | $0.13 | $0.03 | 11s | 2.2 | 0.0 |

Adjudication of the extra findings: grok-4.5 6/6 valid (a silent gate-label rename in #43, a label race between parallel legs and substring path markers in #45, an under-detected repo-visibility path in #46); grok-4.6 5/5 valid (same families); grok-build-0.1 mixed — includes praise comments ("Good addition", "Good —") that the prompt forbids, and two runs (#45, #46) hit 30 turns without ever submitting; grok-4.3 produced no findings at all and one run (#46) no summary either.

## Per run

| model | PR | recall | FP | unlabelled | turns | wall | cost | summary |
|---|---|---|---|---|---|---|---|---|
| grok-4.3 | #37 | 0/1 | 0 | 0 | 3 | 10s | $0.022 | yes |
| grok-4.3 | #43 | 0/1 | 0 | 0 | 3 | 9s | $0.026 | yes |
| grok-4.3 | #45 | 0/2 | 0 | 0 | 2 | 10s | $0.056 | yes |
| grok-4.3 | #46 | 0/1 | 0 | 0 | 1 | 13s | $0.025 | NO |
| grok-4.5 | #37 | 0/1 | 0 | 0 | 5 | 113s | $0.138 | yes |
| grok-4.5 | #43 | 1/1 | 0 | 1 | 8 | 110s | $0.253 | yes |
| grok-4.5 | #45 | 2/2 | 0 | 3 | 7 | 221s | $0.5 | yes |
| grok-4.5 | #46 | 0/1 | 0 | 2 | 11 | 291s | $0.21 | yes |
| grok-4.6 | #37 | 0/1 | 0 | 0 | 6 | 345s | $0.184 | yes |
| grok-4.6 | #43 | 1/1 | 0 | 1 | 8 | 325s | $0.175 | yes |
| grok-4.6 | #45 | 2/2 | 0 | 2 | 14 | 879s | $0.515 | yes |
| grok-4.6 | #46 | 0/1 | 0 | 2 | 13 | 1347s | $0.329 | yes |
| grok-build-0.1 | #37 | 0/1 | 0 | 1 | 11 | 287s | $0.077 | yes |
| grok-build-0.1 | #43 | 1/1 | 0 | 2 | 9 | 149s | $0.149 | yes |
| grok-build-0.1 | #45 | 0/2 | 0 | 0 | 30 | 455s | $0.444 | NO |
| grok-build-0.1 | #46 | 0/1 | 0 | 0 | 30 | 599s | $0.418 | NO |


## Grok CLI spot checks (the dogfood route)

| model | PR | recall | FP | unlabelled | turns | wall | cost |
|---|---|---|---|---|---|---|---|
| grok-4.5 (CLI) | #45 | 1/2 | 0 | 2 (valid) | 7 | 194s | $0.75 |
| grok-4.5 (CLI) | #46 | 0/1 | 0 | 3 (valid: permission lookup not scoped to private repos, `repository.private` signal ignored, no direct tests for the permission helper) | 10 | 447s | $0.42 |
| grok-4.6 (CLI, Task 8 run, 2026-09-16) | #43/#45/#46/#37 | 4/5 | 0 | — | — | 4–10 min | $0.47–0.85 |
| grok-4.3 (CLI, Task 8 run, 2026-09-16) | 4 PRs | 0/4 | 0 | 0 (one run wrote no findings file) | — | 1–2 min | ~$0.07 |
| grok-4.3 (CLI, live on PR #52 `532dd0d`, full review) | — | 0 findings | — | — | 6 | 32s | $0.09 (42 output tokens) |

## Reading

- **grok-4.5 is the cost-efficient pick**: ties grok-4.6 on recall (both miss the same two — #37 is a contradiction between two documentation paragraphs, #46 the fail-open when the payload lacks the author login), zero false positives for both, ~10 % cheaper, **4× faster** (3.1 vs 12.1 min average; one grok-4.6 run took 22 min, longer than the `timeout-minutes: 15` the examples recommend).
- **grok-4.3 does not review.** 0 of 12 labelled defects across four independent tests (corpus via CLI, prompt v3.2 via CLI, live CI round, corpus in-process); 1–6 turns and 10–32 s per review; verdicts like "Solid feature… no blocking issues found". Cheap, green, and empty — the worst combination for a required check.
- **grok-build-0.1** is not a reviewer either: it loops on tools without converging on half the corpus and posts praise.
- **#37 defeats the whole family** (a docs-only contradiction) — a prompt-side candidate for the next calibration, not a model choice.

## Decision (shipped in the same PR)

`DEFAULT_MODELS["grok"] = "grok-4.5"`; `_XAI_TIERS`: `balanced` = `economy` = `grok-4.5`, `deep` = `grok-4.6`; dogfood leg back on `balanced`. Re-run this file's method when xAI publishes new ids (the tier table comment points here).

Reproduce: `python3 tests/eval/run_eval.py run --repo DailybotHQ/ai-diff-reviewer --pr <n> --worktree <dir-at-pr-head> --provider openai --api-base https://api.x.ai/v1 --model <id> --api-key-env XAI_API_KEY --prompt prompts/default.md --extension .review/extension.md --out results/<id>-<n>.json`, then `python3 tests/eval/run_eval.py score results/*.json`.
