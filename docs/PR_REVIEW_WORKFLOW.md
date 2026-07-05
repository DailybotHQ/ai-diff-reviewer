# PR Review Workflow

This repo dogfoods itself. Every PR is reviewed by the action it ships, via `.github/workflows/self-review.yml`. As of v1.1.0 that workflow runs as a **4-leg matrix** — one leg per shipping provider (`anthropic`, `claude-code`, `cursor`, `codex`) — so a single PR ends up with up to four independent reviews posted by the same bot login but distinguished by a per-leg `self-reviewed:<provider>` label. As an AI agent (or human) reading review feedback on a PR, you need to know how to tell live feedback from collapsed/outdated feedback **and** how to attribute a specific comment to the leg that produced it.

## Lifecycle of a single review (per matrix leg)

Each matrix leg goes through the same lifecycle independently. The four legs run in parallel with `fail-fast: false`, so one failing leg doesn't cancel the others.

1. A push lands on the PR branch (open or synchronize event).
2. The previous in-flight workflow run is cancelled (concurrency cancel-in-progress).
3. **Gate step.** The leg checks whether its `api-key-secret` is set on the repo. If not, it emits a `::notice::` and short-circuits — no checkout, no action invocation, no review posted. The rest of the matrix continues.
4. The action starts (only for legs whose secret was set):
   1. **Collapse-previous** marks every prior bot review/comment from **the same login** as `OUTDATED` via GraphQL `minimizeComment`. Because all four legs authenticate as the same `github-actions[bot]` (or the same PAT owner), each leg's collapse step collapses reviews from all prior legs on the previous run — that's fine, they were stale.
   2. **Tracking comment** is posted with `_Working…_` body and the `<!-- ai-pr-reviewer-marker -->` marker.
   3. **Agentic loop or vendor CLI** runs the model (chat-completions family drives it in-process; agent-runner family shells out to the vendor CLI).
   4. **Submit review** posts the summary + queued inline comments atomically.
   5. **Applied label** is added to the PR — `self-reviewed:anthropic`, `self-reviewed:claude-code`, `self-reviewed:cursor`, or `self-reviewed:codex` depending on the leg.
   6. **Update tracking comment** transitions to `done` (with review URL) or `failed`.
5. Once all legs settle, the PR's "Conversation" tab shows:
   - All prior bot artefacts collapsed as `OUTDATED`.
   - Up to four live reviews for the latest HEAD (one per leg whose secret was set).
   - Up to four live tracking comments, each with the marker.
   - Up to four labels (`self-reviewed:*`) showing which provider legs successfully completed.

## Reading review feedback correctly

When applying bot feedback on a PR, the only source of truth is the **most recent non-minimized** artefacts. Everything older is stale by construction.

### Mandatory rules

1. **Skip `isMinimized == true` comments.** The `OUTDATED` collapse is the action's signal that those comments are no longer authoritative.
2. **Anchor on the most recent `<!-- ai-pr-reviewer-marker -->` comments** — plural. On this repo's own PRs there are up to four live markers, one per matrix leg. Each one tells you the SHA its leg's review is for. If a marker SHA doesn't match the current HEAD, the workflow run is in flight or that leg's spinner failed to transition — wait or look at the workflow log for the specific matrix leg.
3. **Read inline comments from the latest review only.** Each review's inline comments share the review's SHA. Mixing inline comments across reviews on different SHAs gives wrong line numbers.
4. **Attribute comments to their leg via the applied label.** The bot login is the same across legs (they all use `secrets.GITHUB_TOKEN`); the differentiator is the `self-reviewed:*` label the leg applies on success. If two legs disagree on a finding, the label tells you which provider called it.

## Ready-to-copy GraphQL query

To list non-minimized bot comments and review summaries on a PR:

```graphql
query($owner: String!, $repo: String!, $number: Int!, $bot: String!) {
  repository(owner: $owner, name: $repo) {
    pullRequest(number: $number) {
      comments(first: 100) {
        nodes {
          id
          body
          isMinimized
          author { login }
        }
      }
      reviews(first: 100) {
        nodes {
          id
          body
          state
          isMinimized
          author { login }
          commit { oid }
          comments(first: 100) {
            nodes {
              id
              body
              path
              line
              isMinimized
            }
          }
        }
      }
    }
  }
}
```

Filter the result with:

```jq
[
  (.data.repository.pullRequest.comments.nodes[]
    | select(.author.login == $bot and .isMinimized == false)),
  (.data.repository.pullRequest.reviews.nodes[]
    | select(.author.login == $bot and .isMinimized == false))
]
```

Replace `$bot` with the login the action authenticates as (typically `github-actions[bot]` if you use the default `secrets.GITHUB_TOKEN`).

## Identifying the bot

The action collapses prior comments belonging to **the user the `github-token` authenticates as** — that's `gh_get_authenticated_login(token)` in `scripts/reviewer.py`. If the consumer passes:

- `secrets.GITHUB_TOKEN` (default) — the bot is `github-actions[bot]`.
- A PAT — the bot is the PAT owner's login.
- An automation account's PAT — the bot is that account's login.

Choose deliberately and document it in the PR template if you want a specific attribution.

**Multi-provider dogfooding on this repo** — the four self-review legs all authenticate as the same `github-actions[bot]`, so filtering by author gives you all four reviews indistinguishably. Use the `self-reviewed:<provider>` labels on the PR (or search the review body for the provider-specific footer emitted by each leg) to tell them apart.

## Reading a specific leg's review

To locate one particular provider's review programmatically (e.g. "did the `cursor` leg succeed?"):

1. Fetch the PR's labels via `gh pr view <n> --json labels`.
2. If `self-reviewed:cursor` is present, the leg completed successfully — fetch its tracking comment (they're all posted by the same bot login, so filter by body prefix on the marker).
3. If the label is missing, either the leg's secret isn't configured, the leg is still running, or it failed. Check the Actions tab under the `self-review` workflow and find the matrix leg with `matrix.provider == 'cursor'`.

The `applied-label` input is the public contract for this pattern — consumers can adopt the same convention on their own PRs by giving each leg a distinct label.

## What the marker enables

The marker `<!-- ai-pr-reviewer-marker -->` is a stable string at the top of every tracking comment. Any tool that wants to find "the most recent review run on this PR" should:

1. Fetch all PR comments.
2. Filter to comments whose body starts with the marker AND `isMinimized == false`.
3. The most recent one is the authoritative tracking comment.

If you need to programmatically check "did the bot finish?" without scraping the workflow log, the marker is the contract.

## Edge cases

### "I don't see any live review"

Possibilities:
- The workflow is still running. Check the Actions tab (specifically the `self-review` workflow — each matrix leg shows up as a separate job).
- The workflow failed before the spinner could update. Check the workflow log; the script should have logged a clear error.
- The PR has the `label-gate` set and is missing the gate label. The tracking comment was never posted.
- The action was disabled for this PR via a `claude-reviewed`-style opt-out label or a workflow `if:` condition.
- **All four matrix legs' API-key secrets are unset** on this repo (typical for fresh forks). Each leg emits a `::notice::` and short-circuits before running. Look for the notice in the Actions log; it's not an error.

### "I see two live reviews"

On a **consumer PR** (single-provider setup) this shouldn't happen if `collapse-previous: true` (the default). If it does:
- The collapse step might have failed (it's `try/except`-wrapped). Check the workflow log.
- A second workflow run might have raced; concurrency `cancel-in-progress` should prevent this, but a non-default consumer workflow might have removed it.

On **this repo's own PRs** you'll see up to four live reviews as a matter of course — one per matrix leg. That's not a bug; use the `self-reviewed:*` labels or the workflow leg name to disambiguate. Each leg is a legitimate live review for the current HEAD.

In consumer scenarios where two reviews really shouldn't be there, the most recent review (newest `created_at`) is the authoritative one; the older one should be ignored manually if not auto-collapsed.

### "The marker SHA doesn't match HEAD"

Either:
- A workflow run is currently in flight on HEAD; the marker is from the previous run. Wait for the new run to update it.
- The current run failed before transitioning the spinner. Check the workflow log; the script's broad-except wrapper should have written a `failed` body.

In neither case should you trust the older marker as authoritative for the current HEAD.

## For agents reviewing other agents' PRs

If you are an AI agent applying feedback from this bot to a PR:

1. Use the GraphQL query above to fetch live (non-minimized) feedback.
2. Anchor on the latest marker.
3. Apply the feedback directly to the diff.
4. Push the fix as a new commit; the next workflow run will re-review.
5. Don't manually dismiss the bot's comments — they auto-collapse on the next push.

If the bot is systematically wrong about a class of issue, that's signal for a `prompts/default.md` update (a separate PR, not bundled with whatever you're currently doing).
