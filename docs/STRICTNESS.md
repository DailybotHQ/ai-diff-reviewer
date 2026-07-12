# Strictness — gating the GitHub check

The `strictness` input decides what makes the GitHub check go red. Four modes; pick the one that matches your team's tolerance for false positives.

## How it works

1. The model sets a `severity` (`critical` / `warning` / `info`) on every inline comment, via the `post_inline_comment` tool.
2. The action computes the **highest** severity across all queued comments. That value is exposed as the `severity` action output.
3. The action compares the highest severity against `strictness`. If the gate is violated, the action exits with code `2`, which makes the GitHub check fail.

That's the entire mechanism. There's no separate "scoring" step — the model's per-comment severity decisions are the gate, and the action just adds them up.

## The four modes

### `lenient` (default)

The check is always green, regardless of what the reviewer found. The review still posts; the inline comments are still created; the maintainer still has to read them. The check just doesn't block merge.

**Use when:**
- You're rolling the reviewer out for the first time and don't want to break existing merge flows.
- Your team treats AI review as advisory ("a second pair of eyes"), not gating.
- You haven't yet calibrated the prompt's severity assignments and want to observe before enforcing.

### `block-on-critical`

The check fails if **any** inline comment is severity `critical`. Warnings and info don't block.

**Use when:**
- You want to catch the worst class of issues (security, data loss, broken APIs) before merge, but trust your team to triage warnings on their own.
- Most teams. This is the recommended setting once you've spent a week or two on `lenient` and trust the model's calibration.

**Branch protection setup:** require the PR-review job in your branch protection rule on `main`. The check name in the dropdown is your job's `name:` — usually `review`.

### `block-on-warning`

The check fails if any inline comment is `warning` **or** `critical`. Only `info` doesn't block.

**Use when:**
- You're running a high-stakes service (payments, identity, healthcare) where "bug-prone" is too close to "production incident".
- Your prompt is well-calibrated and you've gotten used to the false-positive rate.
- The team culture is "every warning gets resolved before merge".

This mode will produce the most "wait, why is the check red" moments. It's strict for a reason; pick it deliberately.

### `block-on-any`

The check fails if the reviewer posted **any** inline comment, including `info`. Zero-tolerance mode.

**Use when:**
- You're running a security-critical stack (payments, identity, medical, defense) where every finding — even informational — deserves triage before merge.
- Your team has committed to a "no unresolved review comments on `main`" policy.
- You're working on a small, mature codebase where the prompt is very well calibrated and every `info` finding is genuinely actionable.

**Warning:** this mode is unforgiving. It **will** produce false positives if your prompt has not been calibrated — an over-eager `info` from a generic prompt will block every PR. Do NOT jump to `block-on-any` from `lenient`; run at `block-on-warning` for at least two weeks first, tune the `Project-specific severity overrides` section of your custom prompt (see [PROMPTS.md](PROMPTS.md)) until the false-positive rate is near zero, then promote.

**Escape hatches:** treat `block-on-any` the same way you would treat "must pass linter" — if the reviewer is wrong, the fix is to tighten the prompt (downgrade a whole class of `info` to "don't post it in the first place") rather than override the gate on a per-PR basis.

## Choosing your mode

A short decision tree:

```
Do you already have the reviewer running and are the severity labels
well-calibrated?
├── No  → start with `lenient` (observe for at least a week)
└── Yes
    │
    Does your stack tolerate any un-triaged findings on `main`?
    ├── Yes, findings are advisory → `block-on-critical` (recommended default)
    └── No, warnings must be resolved before merge
        │
        Does the team also want to enforce triage of `info` findings?
        ├── No, warnings + critical is enough → `block-on-warning`
        └── Yes, zero-tolerance                → `block-on-any`
```

**Practical rollout path:** `lenient` (week 1) → `block-on-critical` (weeks 2–4) → `block-on-warning` (once the team is used to it) → `block-on-any` (only if you have a strong reason and a well-calibrated prompt).

## Calibrating severity in your prompt

The default prompt's severity definitions are general. The strictness gate is only as good as the prompt that drives the model's severity choices. If your team treats certain things as "always critical" and others as "always info", say so explicitly in your prompt:

```markdown
## Project-specific severity overrides

- ALWAYS `critical` (regardless of the default rubric):
  - <a class of issue your team learned about the hard way — describe the
    pattern, why it's critical, and link to the postmortem or doc that
    captures the lesson>
  - <another concrete pattern, with the same level of specificity>

- ALWAYS `info` (downgrade if the default would say `warning`):
  - <a class of issue your team has decided isn't blocking right now —
    e.g. a known tech-debt category with an existing backfill ticket>
```

These overrides — the things only your team knows — are what turn a generic reviewer into one that feels like a senior engineer on your team. The example placeholders above are intentionally vague; replace them with the specific patterns your retrospectives and on-call docs have identified.

## Outputs you can use in downstream steps

The `severity` and `blocked` outputs are available to subsequent steps in the same job:

```yaml
- id: review
  uses: DailybotHQ/ai-pr-reviewer@v1
  with:
    api-key: ${{ secrets.ANTHROPIC_API_KEY }}
    github-token: ${{ secrets.GITHUB_TOKEN }}
    strictness: block-on-critical

- name: Notify Slack on critical findings
  if: steps.review.outputs.severity == 'critical'
  run: |
    curl -X POST -H 'Content-type: application/json' \
      --data "{\"text\":\"Critical PR finding on ${{ github.event.pull_request.html_url }}\"}" \
      $SLACK_WEBHOOK
```

## Common pitfalls

- **Branch protection didn't pick up the check name.** GitHub only lists checks in the dropdown after they've run *at least once* on a PR. Open a throwaway PR, let the action run, then add the check to your protection rule.
- **"It blocked but I think the finding is wrong."** The model's severity assignment is editable: tell the bot to downgrade in a follow-up comment, or push a fix and let the next run reassess. Don't disable the gate just because one finding was wrong; tighten the prompt instead.
- **"It didn't block but I think it should have."** Either (a) the model called the issue `info` when your team would call it `critical` — fix in the prompt — or (b) the model didn't catch the issue at all. The reviewer is not a substitute for human review; pair `block-on-critical` with required human approval, not as a replacement for it.
