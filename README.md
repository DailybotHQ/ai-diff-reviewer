# AI PR Reviewer

> An LLM-driven pull-request reviewer as a GitHub Action — inline comments, severity-based gating, no infrastructure.

[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
[![Powered by Dailybot](https://img.shields.io/badge/Powered%20by-Dailybot-6C5CE7.svg)](https://www.dailybot.com?utm_source=dailybotopensource&utm_medium=ai-pr-reviewer)

A composite GitHub Action that runs a real code review on every pull request: posts inline comments, marks previous reviews as outdated, gates the GitHub check based on configurable strictness, and applies a "reviewed" label. Stdlib-only Python — no Docker image to pull, no Node modules, no infrastructure beyond your provider's API key.

Originally built to replace [`anthropics/claude-code-action@v1`](https://github.com/anthropics/claude-code-action) when its `restoreConfigFromBase` step started crashing on repos that ship `.claude` as a symlink. The action solves that problem and a few more — same review quality, more configuration knobs, friendlier failure modes.

---

## Contents

- [Quick start](#quick-start)
- [What you get out of the box](#what-you-get-out-of-the-box)
- [Inputs](#inputs)
- [Outputs](#outputs)
- [Strictness levels](#strictness-levels)
- [Recipes](#recipes)
- [Required permissions](#required-permissions)
- [How it works](#how-it-works)
- [Provider roadmap](#provider-roadmap)
- [FAQ](#faq)
- [Contributing](#contributing)
- [License](#license)

---

## Quick start

Drop this into `.github/workflows/pr-review.yml`:

```yaml
name: PR review
on:
  pull_request:
    branches: [main]
    types: [opened, synchronize]

concurrency:
  group: pr-review-${{ github.event.pull_request.number }}
  cancel-in-progress: true

jobs:
  review:
    runs-on: ubuntu-latest
    timeout-minutes: 15
    permissions:
      contents: read
      pull-requests: write
    steps:
      - uses: actions/checkout@v4
        with:
          fetch-depth: 0   # required: the action uses `git diff origin/<base>...HEAD`
      - uses: DailybotHQ/ai-pr-reviewer@v1
        with:
          api-key: ${{ secrets.ANTHROPIC_API_KEY }}
          github-token: ${{ secrets.GITHUB_TOKEN }}
```

That's the minimum. Open a PR; the action posts a tracking comment, runs a review, and updates the comment with the result.

---

## What you get out of the box

- **Inline comments** anchored to specific lines, with optional GitHub suggestion blocks (one-click apply).
- **Tracking comment** with a `<!-- ai-pr-reviewer-marker -->` marker that transitions in-place from `Working…` → `View review →`.
- **Auto-collapse** of *this provider's* previous reviews on every new push, scoped by a per-provider marker — leaves other bots' comments (coverage, labelers) and other-provider reviews alone, so multi-provider dogfooding on the same PR just works.
- **Severity-based gating**: the model assigns `critical` / `warning` / `info` to each finding; you decide what fails the check.
- **Optional label gate**: only run when a PR carries a label (e.g. `ready`).
- **Optional "reviewed" label**: applied automatically after a successful, non-blocked review.
- **Self-healing on GitHub 422**: if the model anchors a comment outside the diff, the action retries summary-only instead of losing every other comment.

---

## Inputs

| Input | Required | Default | Description |
|---|---|---|---|
| `api-key` | ✅ | — | Provider API key. For Anthropic this is your `ANTHROPIC_API_KEY`. |
| `github-token` | ✅ | — | Token with `pull-requests: write` and `contents: read`. The default `secrets.GITHUB_TOKEN` works; pass a PAT or automation-bot token if you want the review attributed to a specific account. |
| `provider` | | `anthropic` | LLM provider. `anthropic` (chat-completions), `claude-code` / `cursor` / `codex` (agent-runner CLIs). See [docs/PROVIDERS.md](docs/PROVIDERS.md). |
| `model` | | provider default | Model id. Anthropic → `claude-sonnet-4-6`, Cursor → `composer-2.5`, Codex → `gpt-5-codex`, Claude Code → account default. |
| `prompt-file` | | bundled `prompts/default.md` | Path **inside the consumer checkout** to a markdown system prompt. FULLY REPLACES the base. Customising the prompt is the main lever for adapting the review to your codebase — see [docs/PROMPTS.md](docs/PROMPTS.md). |
| `prompt-extension-file` | | _(empty)_ | Path **inside the consumer checkout** to a markdown file APPENDED to the base prompt. Use to layer overrides without copying the whole default. Combines with `prompt-file` (base + extension). Starter templates in [`examples/prompts/`](examples/prompts/). |
| `author-association` | | `OWNER,MEMBER,COLLABORATOR` | Comma-separated whitelist of GitHub `pull_request.author_association` values allowed to trigger a review. Default is write-tier only — the safe baseline for public open-source repos (prevents external-contributor PR spam from burning your LLM budget). Add `CONTRIBUTOR` to allow returning contributors, or set to empty string to disable the gate. See [docs/SECURITY.md § "Author-association gate"](docs/SECURITY.md). |
| `label-gate` | | `''` | If non-empty, the review only runs when the PR carries this label (e.g. `ready`). Combined with `trigger-mode`. |
| `trigger-mode` | | _(auto)_ | `always` / `label-required` / `label-once` / `label-added-only` — see [docs/TRIGGER_MODES.md](docs/TRIGGER_MODES.md). Empty picks `label-required` when `label-gate` is set, else `always`. |
| `applied-label` | | `''` | If non-empty, this label is applied to the PR after a successful, non-blocked review (e.g. `pr-reviewed`). The label is auto-created if it doesn't exist. |
| `collapse-previous` | | `true` | Mark previous bot reviews/comments as `OUTDATED` via GraphQL `minimizeComment`. |
| `tracking-comment` | | `true` | Post a spinner comment that transitions to the final review URL. |
| `strictness` | | `lenient` | `lenient` / `block-on-critical` / `block-on-warning` / `block-on-any` — see [docs/STRICTNESS.md](docs/STRICTNESS.md). |
| `max-inline-comments` | | `10` | Hard cap on inline comments per review. |
| `pr-description-mode` | | `off` | `off` / `warn` / `block` / `autocomplete` — see [docs/PR_METADATA_CHECKS.md](docs/PR_METADATA_CHECKS.md). |
| `pr-description-min-length` | | `50` | Char threshold below which the PR body is treated as vague. |
| `complexity-labels-enabled` | | `false` | When `true`, the reviewer applies a `complexity:low/medium/high` label to the PR. |
| `complexity-label-prefix` | | `complexity:` | Prefix for the complexity label (change to match your labeling conventions). |
| `max-turns` | | `30` | Hard cap on the agentic-loop iterations (chat-completions providers only). |
| `agent-max-turns` | | `''` | Cap on the CLI provider's internal turn count. Empty = provider default. Ignored for chat-completions providers. |
| `agent-extra-args` | | `''` | Raw string appended to the CLI invocation. Parsed with `shlex.split` (never `shell=True`). Escape hatch for provider-specific flags. |
| `mcp-config-file` | | `''` | Path inside the consumer checkout to an MCP servers JSON config. If set, the file is copied to the CLI's expected location before invocation. |
| `claude-code-version` | | `''` | Pin the Claude Code CLI version (npm semver). Empty = latest. |
| `cursor-version` | | `''` | Pin the Cursor Agent CLI version. Empty = latest stable. |
| `codex-version` | | `''` | Pin the OpenAI Codex CLI version (npm semver). Empty = latest. |

## Outputs

| Output | Type | Description |
|---|---|---|
| `review-url` | string | URL of the posted PR review (empty on skip). |
| `severity` | `none` \| `info` \| `warning` \| `critical` | Highest severity flagged. |
| `inline-attached` | int | Inline comments actually attached. |
| `inline-dropped` | int | Inline comments dropped because GitHub returned 422. |
| `blocked` | bool | Whether strictness blocked the check. When `true`, the action exits with code 2. |
| `skipped` | bool | Whether the run was skipped by the label gate. |

---

## Strictness levels

| Mode | What fails the check |
|---|---|
| `lenient` (default) | Nothing. The review posts; the check is always green. |
| `block-on-critical` | One or more inline comments tagged `critical`. |
| `block-on-warning` | One or more inline comments tagged `critical` or `warning`. |
| `block-on-any` | Any inline comment at all, including `info`. Zero-tolerance mode — use for security-critical or regulated stacks where every finding must be resolved before merge. |

The model decides severity per inline comment via the tool's `severity` argument; the bundled default prompt explains the levels in detail. Customise the prompt to make the model more or less aggressive about each tier.

Full guide: [docs/STRICTNESS.md](docs/STRICTNESS.md).

---

## Recipes

### Run only on PRs labelled `ready`, apply `pr-reviewed` on success

```yaml
- uses: DailybotHQ/ai-pr-reviewer@v1
  with:
    api-key: ${{ secrets.ANTHROPIC_API_KEY }}
    github-token: ${{ secrets.GITHUB_TOKEN }}
    label-gate: ready
    applied-label: pr-reviewed
```

### Block merge on critical findings

```yaml
- uses: DailybotHQ/ai-pr-reviewer@v1
  with:
    api-key: ${{ secrets.ANTHROPIC_API_KEY }}
    github-token: ${{ secrets.GITHUB_TOKEN }}
    strictness: block-on-critical
```

Pair with a branch protection rule that requires the PR-review check to pass.

### Custom prompt for your codebase

```yaml
- uses: DailybotHQ/ai-pr-reviewer@v1
  with:
    api-key: ${{ secrets.ANTHROPIC_API_KEY }}
    github-token: ${{ secrets.GITHUB_TOKEN }}
    # Full replacement:
    prompt-file: .github/prompts/our_house_rules.md
    # Or layer overrides on top of whichever base is loaded (default OR
    # a custom `prompt-file`). Both inputs may be used together — see
    # docs/PROMPTS.md for the "base vs extension vs replacement" guide:
    # prompt-extension-file: examples/prompts/python-strict.md
```

The prompt is the most powerful knob. See [docs/PROMPTS.md](docs/PROMPTS.md) for the "Base vs Extension vs Replacement" decision guide, the starter extensions in [`examples/prompts/`](examples/prompts/), and the meta-prompt that lets your favorite AI generate a repo-tailored prompt for you.

### Review-once-per-label workflow

Run only when you signal readiness by adding a label; toggle the label off/on to re-run:

```yaml
- uses: DailybotHQ/ai-pr-reviewer@v1
  with:
    api-key: ${{ secrets.ANTHROPIC_API_KEY }}
    github-token: ${{ secrets.GITHUB_TOKEN }}
    trigger-mode: label-once
    label-gate: ai-review
```

Full guide: [docs/TRIGGER_MODES.md](docs/TRIGGER_MODES.md).

### Auto-fill missing PR descriptions

Let the reviewer write a first-draft body when the current one is empty or under 50 chars. Guarded by a marker so it never overwrites your edits.

```yaml
- uses: DailybotHQ/ai-pr-reviewer@v1
  with:
    api-key: ${{ secrets.ANTHROPIC_API_KEY }}
    github-token: ${{ secrets.GITHUB_TOKEN }}
    pr-description-mode: autocomplete
```

Full guide: [docs/PR_METADATA_CHECKS.md](docs/PR_METADATA_CHECKS.md).

### AI-driven complexity labels

Apply a `complexity:low/medium/high` label based on cognitive load, files touched, and security surface — not line count:

```yaml
- uses: DailybotHQ/ai-pr-reviewer@v1
  with:
    api-key: ${{ secrets.ANTHROPIC_API_KEY }}
    github-token: ${{ secrets.GITHUB_TOKEN }}
    complexity-labels-enabled: true
```

### Use a non-default automation account

If you want the review attributed to a specific bot account (e.g. so branch protection rules can require approval from "anyone except the bot"):

```yaml
- uses: DailybotHQ/ai-pr-reviewer@v1
  with:
    api-key: ${{ secrets.ANTHROPIC_API_KEY }}
    github-token: ${{ secrets.AUTOMATION_GITHUB_TOKEN }}   # PAT for your bot account
```

### Public open-source repo (safest defaults)

For a public repo, external contributors can open PRs — and each review costs real money (~50K–200K tokens per PR). The `author-association` input (default `OWNER,MEMBER,COLLABORATOR`, v1.3.0+) gates reviews on the PR author's relationship to the repo; the field comes from GitHub's payload and cannot be spoofed. Belt-and-suspenders combined with a label gate:

```yaml
- uses: DailybotHQ/ai-pr-reviewer@v1
  with:
    api-key: ${{ secrets.ANTHROPIC_API_KEY }}
    author-association: 'OWNER,MEMBER,COLLABORATOR,CONTRIBUTOR'  # optional: allow returning contributors
    label-gate: 'ai-review'                                       # maintainer opt-in per PR
    trigger-mode: label-once
```

Result: an outsider opening PRs cannot trigger the review at all (gate fails, zero API calls); returning contributors get a review only when a maintainer applies the `ai-review` label; the review runs exactly once per label application. See [docs/SECURITY.md § "Author-association gate"](docs/SECURITY.md) and [`examples/open-source-safe.yml`](examples/open-source-safe.yml).

More recipes: [`examples/`](examples/).

---

## Required permissions

The action needs:

```yaml
permissions:
  contents: read
  pull-requests: write
```

The job-level `timeout-minutes: 15` is recommended — the agentic loop has its own caps but a sane workflow timeout is the final safety net.

`fetch-depth: 0` on `actions/checkout` is **required**: the action runs `git diff origin/<base>...HEAD` to produce the diff seed, and a shallow clone won't have the base ref locally.

---

## How it works

1. **Label gate** — early-exits if `label-gate` is set and missing.
2. **Collapse previous** — marks prior bot reviews/comments as `OUTDATED` via GraphQL.
3. **Tracking comment** — posts a `Working…` comment with a stable marker.
4. **Fetch PR** — pulls metadata, file list, and `git diff origin/<base>...HEAD`.
5. **Agentic loop** — runs the model with five tools: `read_file`, `grep`, `glob`, `post_inline_comment`, `submit_review`. Inline comments are queued in memory and posted atomically with the final review. Conversation history is pruned in pairs to bound token cost.
6. **Submit** — `POST /pulls/{n}/reviews` with the summary and queued inline comments. On HTTP 422 (one bad anchor line in any comment ⇒ entire request rejected), the action retries summary-only and reports the dropped count in the tracking comment.
7. **Apply label** — applies `applied-label` if set and the strictness gate didn't block.
8. **Strictness gate** — exits 2 if blocked, 0 otherwise.

For the full design, see [docs/PROVIDERS.md](docs/PROVIDERS.md), [docs/PROMPTS.md](docs/PROMPTS.md), [docs/STRICTNESS.md](docs/STRICTNESS.md).

---

## Provider roadmap

| Provider | Family | Status | Notes |
|---|---|---|---|
| Anthropic (Claude) | chat-completions | ✅ shipping (v1.0.0+) | `claude-sonnet-4-6` default. Zero CLI install. |
| Claude Code CLI | agent-runner | ✅ shipping (v1.2.1+) | `@anthropic-ai/claude-code` npm CLI in headless mode. Uses `ANTHROPIC_API_KEY`. Works with subscription auth. |
| Cursor Agent CLI | agent-runner | ✅ shipping (v1.2.1+) | `cursor-agent` local CLI in headless mode. Uses `CURSOR_API_KEY`. Default model `auto` — unlimited on Cursor Pro. |
| OpenAI Codex CLI | agent-runner | ✅ shipping (v1.2.1+) | `@openai/codex` npm CLI in headless mode. Uses `OPENAI_API_KEY`. |
| OpenAI (raw API) | chat-completions | 🛠 roadmap | Direct chat-completions, no CLI install. |
| Google Gemini | chat-completions | 🛠 roadmap | Function-calling translation. |
| AWS Bedrock | chat-completions | 🤔 considering | Anthropic-shape under Bedrock. |

**Two provider families:**

- **`Provider`** (chat-completions family) — the action owns the tool-use loop, calling the model's API in a bounded turn count. No install step needed. Zero overhead for consumers.
- **`AgentRunnerProvider`** (agent-runner family) — a vendor's coding-agent CLI owns the tool-use loop; we shell out in headless mode and receive findings via `.aiprr/findings.json`. Better code comprehension (vendor-tuned tools, LSP, semantic search) at the cost of a CLI install step (only when the consumer opts in — modular install; see [docs/PROVIDERS.md](docs/PROVIDERS.md)).

Adding a new provider means implementing one class and registering it in `build_provider()`. Contributions welcome — see [CONTRIBUTING.md](CONTRIBUTING.md).

---

## FAQ

**Why a custom action and not just `anthropics/claude-code-action`?**
That action's `restoreConfigFromBase` step crashes with `ENOENT` on repos that ship `.claude` as a symlink (a common pattern when consolidating multi-agent configs into a shared `.agents/` folder). This action removes that dependency and adds severity-based gating, configurable label gates, and a 422-recovery path the upstream action lacks.

**Why composite, not Docker?**
The reviewer is stdlib-only Python. A Docker action would add ~30s of pull time and a second supply chain (the image registry) for zero benefit.

**Will the model leak my code to the provider?**
The action sends the PR diff and any files the model `read_file`s to the configured provider. Treat it like any other LLM integration — review your provider's data-retention policy and use a self-hosted runner if you have specific constraints.

**Does it work on private repos?**
Yes. The default `secrets.GITHUB_TOKEN` has the right scope; just make sure the repo's settings allow Actions to read content and write PR comments.

**Can I dogfood the reviewer on its own PRs?**
Yes — see `.github/workflows/self-review.yml` in this repo for the pattern.

---

## Contributing

Bug reports, feature requests, provider implementations, and prompt improvements are all welcome. See [CONTRIBUTING.md](CONTRIBUTING.md).

## License

[MIT](LICENSE) © 2026 AI PR Reviewer contributors.

---

## :electric_plug: Powered by [Dailybot](https://www.dailybot.com?utm_source=dailybotopensource&utm_medium=ai-pr-reviewer)

[Dailybot](https://www.dailybot.com/product/ai) is an AI-powered async communication platform that keeps **people and agents** visible — without adding more meetings or tools. It lives where your team already works (Slack, Teams, Google Chat, Discord, VS Code, and the CLI) and turns scattered signals into clear progress: async check-ins and standups, AI summaries that detect blockers and read team sentiment, workflow automation and approvals, team analytics, and recognition. As AI agents join the workflow, Dailybot surfaces their status and activity right alongside your team's — so long-running agents never go dark. [Learn more](https://www.dailybot.com?utm_source=dailybotopensource&utm_medium=ai-pr-reviewer).
