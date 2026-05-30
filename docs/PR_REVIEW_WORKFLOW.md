# PR Review Workflow

This repo dogfoods itself. Every PR is reviewed by the action it ships, via `.github/workflows/self-review.yml`. As an AI agent (or human) reading review feedback on a PR, you need to know how to tell live feedback from collapsed/outdated feedback.

## Lifecycle of a single review

1. A push lands on the PR branch (open or synchronize event).
2. The previous in-flight workflow run is cancelled (concurrency cancel-in-progress).
3. The action starts:
   1. **Collapse-previous** marks every prior bot review/comment as `OUTDATED` via GraphQL `minimizeComment`.
   2. **Tracking comment** is posted with `_Working…_` body and the `<!-- ai-pr-reviewer-marker -->` marker.
   3. **Agentic loop** runs the model with tool use.
   4. **Submit review** posts the summary + queued inline comments atomically.
   5. **Update tracking comment** transitions to `done` (with review URL) or `failed`.
4. The PR's "Conversation" tab now shows:
   - All prior bot artefacts (reviews, summary comments, inline comments) collapsed as `OUTDATED`.
   - One live review for the latest HEAD.
   - One live tracking comment with the marker.

## Reading review feedback correctly

When applying bot feedback on a PR, the only source of truth is the **most recent non-minimized** artefacts. Everything older is stale by construction.

### Mandatory rules

1. **Skip `isMinimized == true` comments.** The `OUTDATED` collapse is the action's signal that those comments are no longer authoritative.
2. **Anchor on the most recent `<!-- ai-pr-reviewer-marker -->` comment.** It tells you the SHA the live review is for. If the marker SHA doesn't match the current HEAD, a workflow run is in flight or the spinner failed to transition — wait or look at the workflow log.
3. **Read inline comments from the latest review only.** Each review's inline comments share the review's SHA. Mixing inline comments across reviews on different SHAs gives wrong line numbers.

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

## What the marker enables

The marker `<!-- ai-pr-reviewer-marker -->` is a stable string at the top of every tracking comment. Any tool that wants to find "the most recent review run on this PR" should:

1. Fetch all PR comments.
2. Filter to comments whose body starts with the marker AND `isMinimized == false`.
3. The most recent one is the authoritative tracking comment.

If you need to programmatically check "did the bot finish?" without scraping the workflow log, the marker is the contract.

## Edge cases

### "I don't see any live review"

Possibilities:
- The workflow is still running. Check the Actions tab.
- The workflow failed before the spinner could update. Check the workflow log; the script should have logged a clear error.
- The PR has the `label-gate` set and is missing the gate label. The tracking comment was never posted.
- The action was disabled for this PR via a `claude-reviewed`-style opt-out label or a workflow `if:` condition.

### "I see two live reviews"

Shouldn't happen if `collapse-previous: true` (the default). If it does:
- The collapse step might have failed (it's `try/except`-wrapped). Check the workflow log.
- A second workflow run might have raced; concurrency `cancel-in-progress` should prevent this, but a non-default consumer workflow might have removed it.

In either case, the most recent review (newest `created_at`) is the authoritative one; the older one should be ignored manually if not auto-collapsed.

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
