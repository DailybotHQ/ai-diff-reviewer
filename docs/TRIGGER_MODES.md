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
