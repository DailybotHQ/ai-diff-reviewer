# Trigger modes — deciding when the reviewer runs

The `trigger-mode` input controls whether the review fires for a given webhook event. Four values:

| Mode | Fires when | Typical use |
|---|---|---|
| `always` (default when no `label-gate`) | Every subscribed event. | Continuous review, small teams, low PR volume. |
| `label-required` (implicit when `label-gate` is set) | The PR carries the `label-gate` label. | Keeping WIP PRs out of the review queue. |
| `label-once` | The PR carries the label AND the label has been applied more recently than the last successful review. | The user's preferred workflow — "review when I ask, not on every push, and let me force a re-review by toggling the label off/on". |
| `label-added-only` | The current event is a `labeled` webhook AND the label matches. | On-demand review with no ambient polling. |

The mode + `label-gate` combination is authoritative — the workflow's own `on:` block determines which events reach the runner in the first place, so pair the two carefully.

> **Label matching is case-insensitive.** `label-gate: ready` is satisfied by a `ready`, `Ready`, or `READY` label (comparison is lowercased and whitespace-trimmed), across all label-based modes — so you don't have to match the exact casing of the label as stored on GitHub.

> **`author-association` runs before `trigger-mode`.** The author-association gate (default write-tier: `OWNER,MEMBER,COLLABORATOR`) is evaluated *first* — before any label/trigger logic and before the PR diff is fetched — so an author who isn't in the whitelist is skipped with **zero API cost** regardless of `trigger-mode`. See [SECURITY.md § "Author-association gate"](SECURITY.md). The two gates compose (AND): a review runs only when the author is allowed **and** the trigger fires.

## Choosing the right mode

```
Do you want the reviewer to run on every push to open PRs?
├── Yes → `always` (or omit trigger-mode; that's the default)
└── No, run only when I signal readiness
    │
    Should it re-run on every subsequent push (once labeled)?
    ├── Yes → `label-required` + on: [opened, synchronize]
    └── No, run once per signal
        │
        Should it be possible to force a re-review by re-signaling?
        ├── Yes → `label-once` + on: [opened, synchronize, labeled, unlabeled]
        └── No, one review is enough → `label-added-only` + on: [labeled]
```

## `label-once` — how it works

The action counts `LabeledEvent` events for the `label-gate` label on the PR's `/issues/{n}/timeline`. That count is the **generation** — it increases every time the label is added.

The last successful review embeds the generation it was triggered on inside the tracking comment (as a JSON blob in an HTML comment marker). On subsequent runs, the action reads that state; if the current generation is higher, it runs. Otherwise, it skips with a log message and `outputs.skipped=true`.

**Rerun flow:**

1. Author pushes commits. The reviewer skips because generation == last-reviewed.
2. Author removes the `ai-review` label. Nothing happens (no `labeled` event fires).
3. Author re-adds the `ai-review` label. Generation bumps → the reviewer runs.

**Edge cases:**

- **First-run:** last-reviewed generation defaults to 0. If the label was added before the action was configured, the reviewer will still fire on its first opportunity — subsequent toggles behave normally.
- **Tracking comment disabled** (`tracking-comment: false`): `label-once` still works using the recent-comments API to find any prior marker; if none exists, the mode degrades gracefully to "run and record".
- **PR closed and re-opened:** the timeline persists — closing/reopening does not reset the generation. Toggling the label does.
- **Timeline API transiently fails / returns 0:** the reviewer still runs when the gate label is present (`label_toggle_generation=0` is treated as "count unknown," not "already reviewed"). This is the safer default — a review with an inaccurate generation is more useful than a silent skip. When the run completes it persists the current generation, so subsequent runs return to the normal flow.
- **Long-lived, high-chatter PRs** (thousands of timeline events): the `count_label_events` helper stops paginating after 20 pages (~2000 events) to bound API cost. When the cap is hit the action logs `WARNING: count_label_events hit the 20-page pagination cap …`; if `label-once` refuses to re-fire after the cap, either toggle the label off/on twice (the second toggle enters the visible window) or switch that PR's workflow to `label-added-only`.

## `label-added-only` — the strictest variant

This mode fires the workflow only when GitHub emits a `labeled` event with the matching label. The workflow's `on:` block MUST include `types: [labeled]`; if it's not there, the workflow doesn't even reach the action.

**Why this exists:** for teams that treat the reviewer as a one-shot on-demand tool. No re-running on push. Toggling the label off/on is the only way to trigger.

**Contrast with `label-once`:** `label-once` also runs when the label is already present and the PR gets a new commit *and* the generation is fresh. `label-added-only` requires the current event to literally be `labeled`.

**The webhook-vs-gate distinction (v1.2.0+):** GitHub's `labeled` event fires for *any* label. If `label-gate: ai-review` is already on the PR and someone adds an unrelated label (e.g. `bug`, `dependencies`), the workflow *would* fire — but the action inspects `event.label.name` and skips when it doesn't match `label-gate`, logging `Trigger decision: should_run=False (labeled event was for 'bug', not 'ai-review')`. So you don't have to worry about accidentally paying for a review every time a bot bumps a dependency label.

## Recipe: run once when labelled `ready`, block merge until it passes

A common ask: "only review when I apply a `ready` label, review exactly once per application, when it *doesn't* review show **Skipped** (not a misleading green **Success**) — **and** don't let me merge until a review has actually passed." This repo's own [`.github/workflows/self-review.yml`](../.github/workflows/self-review.yml) is the reference implementation. Three techniques combine:

**1. Fire on the label event, not on push.** Subscribe to `labeled` (and `opened` to catch a PR created with the label already on it), and *not* `synchronize`. Pushes then don't re-review — re-review happens by removing and re-adding `ready`. Guard against unrelated labels by checking the event's label:

```yaml
on:
  pull_request:
    types: [opened, labeled]   # NOT synchronize → run-once per label application
```

**2. Gate at the JOB level so a skipped run is grey, not green.** A step-level skip (`steps.*.if`) leaves the *job* green; only a **job-level `if:`** that evaluates false renders as **Skipped**. So compute the decision once and gate the job on it. For a **single provider**, a decision job + `needs` does it (case-insensitive label check in bash):

```yaml
jobs:
  gate:
    runs-on: ubuntu-latest
    outputs:
      run: ${{ steps.d.outputs.run }}
    steps:
      - id: d
        env:
          ACTION: ${{ github.event.action }}
          EVENT_LABEL: ${{ github.event.label.name }}
          LABELS: ${{ toJSON(github.event.pull_request.labels.*.name) }}
        shell: bash
        run: |
          lc() { tr '[:upper:]' '[:lower:]'; }
          run=false
          if [ "$ACTION" = labeled ] && [ "$(printf %s "$EVENT_LABEL" | lc)" = ready ]; then run=true; fi
          if [ "$ACTION" = opened ] && printf %s "$LABELS" | lc | grep -qE '"ready"'; then run=true; fi
          echo "run=$run" >> "$GITHUB_OUTPUT"
  review:
    needs: gate
    if: needs.gate.outputs.run == 'true'   # false → this job shows as *Skipped*
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
        with: { fetch-depth: 0 }
      - uses: DailybotHQ/ai-pr-reviewer@v1
        with:
          api-key: ${{ secrets.ANTHROPIC_API_KEY }}
          github-token: ${{ secrets.GITHUB_TOKEN }}
          label-gate: ready   # defense-in-depth; the action's gate agrees
```

**Why not a job-level `if:` directly on the matrix?** GitHub Actions does **not** expose the `matrix` context to a job-level `if:`, so a static matrix can only be skipped per-step (leaving the job green). For a **multi-provider matrix**, have the decision job emit the matrix of legs that should run and consume it with `strategy.matrix.include: ${{ fromJSON(needs.gate.outputs.matrix) }}` — ineligible legs are then simply absent (never a green no-op), and when nothing is eligible the whole job is **Skipped** via `if: needs.gate.outputs.matrix != '[]'`. See `self-review.yml` for the full four-provider version, including per-provider secret detection.

**3. Gate the *merge* with a stable-named job — because a Skipped required check does NOT block.** This is the subtle part. GitHub's branch protection treats a required check whose conclusion is **Skipped** as *passing* — the PR stays mergeable. (Only *failure* or a never-reported *pending* check blocks a merge.) So marking the `review` / `Self-review — <provider>` legs as "Required" does **not** stop someone from merging a PR that was never reviewed — GitHub will happily say *"All checks have passed — 1 skipped"* and enable the merge button. It is also a mistake to require the dynamic leg names directly: they vary per run and vanish when skipped.

The fix is a final job with a **fixed name** that you mark as the required check. It runs with `if: always()` and **fails** (red → blocks merge) unless the review actually ran and passed.

For a **single review job**, checking its result is enough:

```yaml
  gate:
    name: 'Self-review gate'          # ← mark THIS as the required check
    needs: [gate-decision, review]    # your decision job + the review job
    if: always()                      # report on every event, even when review skipped
    runs-on: ubuntu-latest
    steps:
      - shell: bash
        env:
          MATRIX: ${{ needs.gate-decision.outputs.matrix }}
          REVIEW_RESULT: ${{ needs.review.result }}
        run: |
          set -euo pipefail
          if [ "${MATRIX:-[]}" = "[]" ]; then
            echo "::error::Review did not run — apply the 'ready' label so the required review executes, then merge once it passes."
            exit 1                     # no ready label → block merge
          fi
          [ "$REVIEW_RESULT" = "success" ] || {
            echo "::error::Review ran but did not pass (result=$REVIEW_RESULT)."; exit 1; }
          echo "Merge gate satisfied."
```

For a **provider matrix**, you usually want "**at least one** provider passed" — one flaky provider shouldn't block a merge another approved. The aggregate `needs.review.result` can't express this (it's `failure` if *any* leg fails), so count the successful legs from the run's jobs via the API (`permissions: actions: read`):

```yaml
  gate:
    name: 'Self-review gate'
    needs: [gate-decision, review]
    if: always()
    runs-on: ubuntu-latest
    permissions:
      actions: read                   # read this run's job conclusions
    steps:
      - shell: bash
        env:
          GH_TOKEN: ${{ github.token }}
          MATRIX: ${{ needs.gate-decision.outputs.matrix }}
          REPO: ${{ github.repository }}
          RUN_ID: ${{ github.run_id }}
        run: |
          set -euo pipefail
          [ "${MATRIX:-[]}" != "[]" ] || {
            echo "::error::Review did not run — apply the 'ready' label."; exit 1; }
          passed=$(gh api "repos/$REPO/actions/runs/$RUN_ID/jobs" --paginate \
            --jq '[.jobs[] | select(.name != "Self-review gate")
                   | select(.name | startswith("Self-review"))
                   | select(.conclusion == "success")] | length')
          [ "${passed:-0}" -ge 1 ] || {
            echo "::error::No provider leg passed — at least one must run and pass."; exit 1; }
          echo "Merge gate satisfied ($passed leg(s) passed)."
```

Net effect: the per-leg checks stay **honestly Skipped** for humans reading the list (technique 2), while the single `Self-review gate` check is **red until at least one `ready`-triggered review passes** — so `Required` on that one check actually blocks the merge. In branch protection, require **only** `Self-review gate`, not the individual legs.

> **Note — this is the opposite trade-off from "Skipped is fine".** If you *want* un-reviewed PRs to stay freely mergeable (the review is advisory, not a gate), skip technique 3 entirely and don't mark anything required — the Skipped legs are exactly right. Add the gate only when a review must be a *precondition* for merge.

**Variant — opt-in gate (skip when no `ready`).** The strict recipe above blocks *every* PR without `ready`. If you instead want "when a review is requested it must pass, but PRs that don't request a review shouldn't carry a red X in the checks list", add a label check to the gate's `if:` so the gate only runs when `ready` is applied:

```yaml
  gate:
    name: 'Self-review gate'
    needs: [gate-decision, review]
    # Runs only when a review was requested. Without `ready` the gate is Skipped
    # (grey) instead of red — no noise on docs/other PRs.
    if: always() && contains(github.event.pull_request.labels.*.name, 'ready')
```

Trade-off: since GitHub treats a Skipped required check as *passing*, marking this gate `Required` under the opt-in variant means a PR without `ready` becomes mergeable **without a review**. Pair it with a separate rule that enforces the `ready` label (a lightweight labeler action or a repository ruleset) if you want to force `ready` on every PR. This repo's own [`.github/workflows/self-review.yml`](../.github/workflows/self-review.yml) uses this opt-in variant.

## Interaction with `on:` and `concurrency`

- The workflow's `on:` block is the outer gate — GitHub only fires the runner when the subscribed event matches.
- `trigger-mode` is the inner gate — the action decides whether to actually run once it's fired.
- `concurrency` cancellation is the safety net — a `synchronize` event that fires while a prior `labeled` event is still running will cancel the older run (recommended pattern in all examples).

The examples in [`../examples/`](../examples/) show the right `on:` block for each mode.

## Back-compat with v1.1

Consumers who set `label-gate: X` in v1.1 workflows continue to work identically — the action internally resolves `trigger-mode` to `label-required` when only `label-gate` is set. The new `trigger-mode` input is optional; no breaking change.

## Rollback / troubleshooting

- **"It didn't run when I expected it to"** — check the workflow log for the `Trigger decision: should_run=False` line. The `reason` field explains why (missing label, stale generation, wrong event action).
- **"It ran twice unexpectedly"** — check if you have both an `opened` and a `labeled` event firing in quick succession. The `concurrency` group in every example cancels the older run to avoid this.
- **"I want to force a re-review"** — for `label-once`, toggle the label off and on. For `label-required`, push a commit. For `label-added-only`, toggle the label off and on.
