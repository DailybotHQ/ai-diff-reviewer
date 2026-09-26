# PR Review Workflow

This repo dogfoods itself. Every PR is reviewed by the action it ships, via `.github/workflows/self-review.yml`. The workflow keeps a 4-leg matrix (`anthropic`, `claude-code`, `cursor`, `codex`) but uses a cost-control scope gate: the direct `anthropic` leg runs on every PR/push, while the CLI-provider legs only invoke the LLM when provider-sensitive action/runtime files changed. A PR therefore has at least one live self-review and up to four, all posted by the same bot login but distinguished by per-leg `self-reviewed:<provider>` labels. As an AI agent (or human) reading review feedback on a PR, you need to know how to tell live feedback from collapsed/outdated feedback **and** how to attribute a specific comment to the leg that produced it.

## Lifecycle of a single review (per matrix leg)

Each matrix leg goes through the same lifecycle independently when enabled. The legs run in parallel with `fail-fast: false`, so one failing leg doesn't cancel the others.

1. A push lands on the PR branch (open or synchronize event).
2. The previous in-flight workflow run is cancelled (concurrency cancel-in-progress).
3. **Secret gate.** The leg checks whether its `api-key-secret` is set on the repo. If not, it emits a `::notice::` and short-circuits — no checkout, no action invocation, no review posted. The rest of the matrix continues.
4. **Scope gate.** After checkout, the leg decides whether it should invoke the reviewer. `anthropic` always runs. CLI-provider legs run only when the diff touches critical action/runtime surfaces (`action.yml`, `scripts/reviewer.py`, prompts, core workflow files, or provider/runtime tests).
5. The action starts (only for legs whose secret was set and whose scope gate passed):
   1. **Collapse-previous** marks every prior bot review/comment from **the same login** as `OUTDATED` via GraphQL `minimizeComment`. Because active legs authenticate as the same `github-actions[bot]` (or the same PAT owner), each leg's collapse step can see reviews from other legs on the previous run — provider markers keep each leg scoped to its own artefacts.
   2. **Tracking comment** is posted with `_Working…_` body and the `<!-- ai-pr-reviewer-marker -->` marker.
   3. **Agentic loop or vendor CLI** runs the model (chat-completions family drives it in-process; agent-runner family shells out to the vendor CLI).
   4. **Submit review** posts the summary + queued inline comments atomically.
   5. **Applied label** is added to the PR — `self-reviewed:anthropic`, `self-reviewed:claude-code`, `self-reviewed:cursor`, or `self-reviewed:codex` depending on the leg.
   6. **Update tracking comment** transitions to `done` (with review URL) or `failed`.
6. Once all legs settle, the PR's "Conversation" tab shows:
   - All prior bot artefacts collapsed as `OUTDATED`.
   - One to four live reviews for the latest HEAD (depending on secrets and the scope gate).
   - One to four live tracking comments, each with the marker.
   - One to four labels (`self-reviewed:*`) showing which provider legs successfully completed.

## The structured output artifact (v3) — the machine path

Since v3 every run uploads its `review-output/3.0` document as the workflow artifact `ai-diff-reviewer-<head12>-<provider>-<kind>-<model>` (90-day retention) and exposes `structured-output-path` / `structured-output-sha256` / `structured-output-artifact` as step outputs. **Tools read the artifact; humans read the thread.** The document carries what the thread cannot: the published findings with evidence and `verification`, the **refuted** findings with reasons, the prior-findings ledger (retired with reason / still open / regressed / unverified claims), the gate decision, usage and cost, and `summary.rendered_markdown` — the exact body that was posted, so the two can never disagree.

To fetch it for a PR head:

```bash
RUN_ID="$(gh run list --repo "$REPO" --commit "$HEAD_SHA" --json databaseId,status --jq '[.[] | select(.status == "completed")] | sort_by(.databaseId) | reverse | .[0].databaseId')"
gh api "repos/$REPO/actions/runs/$RUN_ID/artifacts" --jq '.artifacts[] | select(.name | startswith("ai-diff-reviewer-")) | .name'
gh run download "$RUN_ID" --repo "$REPO" -n "<artifact name>" -D /tmp/aiprr-out
```

Guards: `change_inventory.head_sha` must equal the head you are reading for; compare the file's SHA-256 with the `structured-output-sha256` output when you can read it; treat `run.status` `incomplete` / `timeout` as partial. One artifact per matrix leg — the name suffix identifies the leg, so no label lookup is needed. When no artifact exists (pre-v3 review, expired, in flight), the thread rules below remain the contract.

## Aggregated reviews (v3, `mode: aggregate`)

When the workflow runs a matrix of emit legs and one aggregate job ([RFC-04](rfc/v3/04-ensemble-consolidation.md)), there is **one** review, **one** tracking comment and **one** label per head, whatever the number of legs:

- The tracking comment and the review body carry `<!-- ai-pr-reviewer-aggregate -->` beside `<!-- ai-pr-reviewer-marker -->` in place of a per-provider marker; the IAR state block lives there, so there is one dedup history per PR instead of one per leg.
- The body starts with a legs line (`Legs: N delivered / M expected · partial: … · missing: …`), a per-leg table (status, findings, turns, cost) and the agreement histogram; each consolidated finding carries `agreement = {legs_total, legs_reporting, reported_by}` in the structured output and an "Also reported by" line inline.
- `collapse-previous` is scoped to the aggregate marker; on the first aggregated round it also minimizes surviving per-leg reviews from the previous shape (migration, logged).
- The job summary (`$GITHUB_STEP_SUMMARY`) repeats the legs table and the check line; the outputs `legs-expected`, `legs-delivered`, `duplicates-removed`, `agreement-histogram` are set on the aggregate step.
- Failure modes: a leg that timed out contributes its partial findings and is named as *partial*; a missing leg is named; with `require-all-legs: true` either fails the check. No delivered leg at all is red. A document for another head SHA is ignored; two documents for one leg — the newest `recorded_at` wins.

Emit legs post nothing (with `expected-legs` set) or a single marked note (`<!-- ai-pr-reviewer-emit-note -->`) reminding you to add the aggregate job.

## Reading review feedback correctly

When applying bot feedback on a PR, the only source of truth is the **most recent non-minimized** artefacts. Everything older is stale by construction.

### Mandatory rules

1. **Skip `isMinimized == true` comments.** The `OUTDATED` collapse is the action's signal that those comments are no longer authoritative.
2. **Anchor on the most recent `<!-- ai-pr-reviewer-marker -->` comments** — plural when multiple legs ran. On this repo's own PRs there can be up to four live markers, one per active matrix leg. Each one tells you the SHA its leg's review is for. If a marker SHA doesn't match the current HEAD, the workflow run is in flight or that leg's spinner failed to transition — wait or look at the workflow log for the specific matrix leg.
3. **Read inline comments from the latest review only.** Each review's inline comments share the review's SHA. Mixing inline comments across reviews on different SHAs gives wrong line numbers.
4. **Attribute comments to their leg via the applied label.** The bot login is the same across legs (they all use `secrets.GITHUB_TOKEN`); the differentiator is the `self-reviewed:*` label the leg applies on success. If two legs disagree on a finding, the label tells you which provider called it.

## Ready-to-copy GraphQL query

To list non-minimized bot comments and review summaries on a PR:

```graphql
query($owner: String!, $repo: String!, $number: Int!) {
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

**Multi-provider dogfooding on this repo** — when the scope gate enables multiple self-review legs, they all authenticate as the same `github-actions[bot]`, so filtering by author gives you every active provider review indistinguishably. Use the `self-reviewed:<provider>` labels on the PR (or search the review body for the provider-specific footer emitted by each leg) to tell them apart.

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
- **All matrix legs' API-key secrets are unset** on this repo (typical for fresh forks). Each leg emits a `::notice::` and short-circuits before running. Look for the notice in the Actions log; it's not an error.

### "I see two live reviews"

On a **consumer PR** (single-provider setup) this shouldn't happen if `collapse-previous: true` (the default). If it does:
- The collapse step might have failed (it's `try/except`-wrapped). Check the workflow log.
- A second workflow run might have raced; concurrency `cancel-in-progress` should prevent this, but a non-default consumer workflow might have removed it.

On **this repo's own PRs** you can see up to four live reviews when the scope gate enables the CLI-provider legs. That's not a bug; use the `self-reviewed:*` labels or the workflow leg name to disambiguate. Each active leg is a legitimate live review for the current HEAD.

In consumer scenarios where two reviews really shouldn't be there, the most recent review (newest `created_at`) is the authoritative one; the older one should be ignored manually if not auto-collapsed.

### "The marker SHA doesn't match HEAD"

Either:
- A workflow run is currently in flight on HEAD; the marker is from the previous run. Wait for the new run to update it.
- The current run failed before transitioning the spinner. Check the workflow log; the script's broad-except wrapper should have written a `failed` body.

In neither case should you trust the older marker as authoritative for the current HEAD.

## Closing the loop — the `address-review` sub-skill

Everything above is the manual procedure; the local skill's [`address-review` sub-skill](../skills/ai-diff-reviewer/address-review/SKILL.md) executes it end to end: find the branch's open PR(s), survey every other workflow and check the PR runs and diagnose the failing ones from their logs (a failing codecheck is fixed in the same pass, a branch `BEHIND` its base is updated, a flaky failure earns at most one offered re-run), check the review is fresh for HEAD (marker SHA, or the [structured artifact](#the-structured-output-artifact-v3--the-machine-path)), present the CI fixes and the findings as one apply/defer/skip plan, then — on one yes — apply, commit (small Conventional Commits batches), push, and re-arm the reviewer the way the repo triggers it (label-gated: toggle the label off/on or add it; push-triggered: confirm the new run started). It obeys the same rules as this doc: never a stale review, minimized comments skipped, per-leg attribution, structured artifact preferred over scraped bodies — and never a weakened check: workflow files only as a plan-named direct fix of a diagnosed failure, never a bypass. A bare *"loop the review"* invocation on a fresh context needs no other instruction — the current branch's open PR is the target. And a PR the reviewer has never run on is a **cold start**, not a stop: the loop detects the repo's trigger and arms it — on an otherwise-green PR it adds the label now and waits for the round; on a red one it fixes the failures first and arms after the push, so the first round reviews the fixed head.

## For agents reviewing other agents' PRs

If you are an AI agent applying feedback from this bot to a PR:

1. Use the GraphQL query above to fetch live (non-minimized) feedback.
2. Anchor on the latest marker.
3. Apply the feedback directly to the diff.
4. Push the fix as a new commit; the next workflow run will re-review.
5. Don't manually dismiss the bot's comments — they auto-collapse on the next push.

If the bot is systematically wrong about a class of issue, that's signal for a `prompts/default.md` update (a separate PR, not bundled with whatever you're currently doing).
