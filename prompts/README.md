# `prompts/`

**The bundled default system prompt** that ships with the action.

## Contents

| File | Purpose |
|---|---|
| [`default.md`](default.md) | The default system prompt loaded when the `prompt-file` input is empty. Technology-agnostic — the reviewer describes tool use, per-finding decision framework, severity definitions (`critical` / `warning` / `info`), and output shape without opinionating on any specific stack. |

## Where it's used

`scripts/reviewer.py` loads this file when the consumer doesn't pass a custom `prompt-file`. The relevant chain is:

1. `action.yml` → `AIPRR_PROMPT_FILE` env var (empty by default).
2. `scripts/reviewer.py` → falls back to `${AIPRR_ACTION_PATH}/prompts/default.md`.
3. The content is passed as the Anthropic `system` message on every API call.

## When to change `default.md`

Prompt changes are **load-bearing** — the model's behaviour, its severity calibration, and the review quality all trace back here. `AGENTS.md` DON'T #6 makes it explicit: **no non-trivial prompt change ships without before/after evidence on a real PR**.

The workflow:

1. Read [`../docs/PROMPTS.md`](../docs/PROMPTS.md) — what a good prompt looks like, common failure modes, severity definitions.
2. Iterate the prompt locally.
3. Use the `/prompt-test` skill ([`../.agents/skills/prompt-test/SKILL.md`](../.agents/skills/prompt-test/SKILL.md)) to run the OLD and NEW prompt against the same target PR(s) and capture a diff. Real evidence, not vibes.
4. Attach the before/after in the PR description.
5. `.github/workflows/self-review.yml` provides the final dogfooding gate on the PR that changes the prompt.

## Custom prompts for consumers

Consumers can point the action at their own prompt via the `prompt-file` input:

```yaml
- uses: DailybotHQ/ai-diff-reviewer@v2
  with:
    api-key: ${{ secrets.ANTHROPIC_API_KEY }}
    github-token: ${{ secrets.GITHUB_TOKEN }}
    prompt-file: .github/prompts/our_house_rules.md
```

The consumer prompt is read from **their** checkout, not this repo. [`../docs/PROMPTS.md`](../docs/PROMPTS.md) has the guidance for writing house-rules prompts (project-specific anti-patterns, "don't comment on" lists, severity calibration).
