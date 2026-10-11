<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="https://raw.githubusercontent.com/DailybotHQ/ai-diff-reviewer/main/assets/logo-dark.png">
    <source media="(prefers-color-scheme: light)" srcset="https://raw.githubusercontent.com/DailybotHQ/ai-diff-reviewer/main/assets/logo-light.png">
    <img alt="AI Diff Reviewer" src="https://raw.githubusercontent.com/DailybotHQ/ai-diff-reviewer/main/assets/logo-light.png" width="440">
  </picture>
</p>

# AI Diff Reviewer

AI Diff Reviewer gives teams a local coding-agent review skill and a GitHub
Action that reviews pull requests against the same prompt and severity model.

[![Code Check](https://github.com/DailybotHQ/ai-diff-reviewer/actions/workflows/code_check.yml/badge.svg?branch=main)](https://github.com/DailybotHQ/ai-diff-reviewer/actions/workflows/code_check.yml)
[![Release](https://img.shields.io/github/v/release/DailybotHQ/ai-diff-reviewer?sort=semver)](https://github.com/DailybotHQ/ai-diff-reviewer/releases)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
[![GitHub Marketplace](https://img.shields.io/github/v/release/DailybotHQ/ai-diff-reviewer?label=Marketplace&logo=github&color=success)](https://github.com/marketplace/actions/ai-diff-reviewer)
[![Powered by Dailybot](https://img.shields.io/badge/Powered%20by-Dailybot-6C5CE7.svg)](https://www.dailybot.com?utm_source=dailybotopensource&utm_medium=ai-pr-reviewer)

A diff review should identify actionable defects, preserve evidence, and make
its merge decision inspectable. The Action reads the PR diff and relevant
files, verifies findings, and posts an inline review with a strictness gate.
The local skill applies the same methodology inside your existing coding
agent. Share `.review/extension.md` between them to add repository rules;
CI also tracks prior findings across review rounds.

## Install and quickstart

Pin the same release on both surfaces: **v3.3.9**. The skill and Action can
also be used independently.

### Local skill

Install into your repository with the cross-agent [skills CLI](https://skills.sh):

```bash
npx skills add DailybotHQ/ai-diff-reviewer@v3.3.9 --skill ai-diff-reviewer
```

Ask your agent: **"Review my current branch against origin/main."** The skill
reads the repository's instructions, the shipped prompt, and an existing
review extension. On first use without an extension, it offers to generate
one; choosing a base-prompt-only review keeps the pass read-only.

### GitHub Action

Add `.github/workflows/pr-review.yml` and configure `ANTHROPIC_API_KEY` as a
repository secret:

```yaml
name: PR review
on:
  pull_request:
    branches: [main]
    types: [opened, synchronize, reopened]

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
      - uses: actions/checkout@v7
        with:
          fetch-depth: 0
          persist-credentials: false
      - uses: DailybotHQ/ai-diff-reviewer@v3.3.9
        with:
          provider: anthropic
          model: balanced
          api-key: ${{ secrets.ANTHROPIC_API_KEY }}
          github-token: ${{ secrets.GITHUB_TOKEN }}
          strictness: block-on-critical
```

Full git history is required for the review range. The workflow uses the
in-process provider; read the [provider and credential model](docs/PROVIDERS.md)
before selecting a CLI runner or custom endpoint. The skill's setup flow can
write a workflow with your chosen provider, trigger policy, and shared extension.

### Verify release archives

The release attaches tagged source archives and `SHA256SUMS`. With GitHub CLI
installed, download both archives so every checksum entry can be checked:

```bash
(
  set -eu
  reviewer_download_dir="$(mktemp -d)"
  gh release download v3.3.9 --repo DailybotHQ/ai-diff-reviewer \
    --dir "$reviewer_download_dir" \
    --pattern '*.tar.gz' --pattern '*.zip' --pattern SHA256SUMS
  cd "$reviewer_download_dir"
  shasum -a 256 -c SHA256SUMS
)
```

Both archive entries should report `OK`. Linux can use `sha256sum -c SHA256SUMS`.
The manifest checks downloaded bytes against the published release; it is
not an independent signature. See [release recovery](docs/RELEASE_RECOVERY.md)
for the release and evaluation gates.

## Core usage

These requests activate the installed skill in your coding agent:

| Request | Result |
| --- | --- |
| "Review my current branch against origin/main" | A full local review of the explicit base range. |
| "Generate a .review/extension.md for this repo" | Repository-specific review rules, with consent to write them. |
| "Open the PR for this branch" | A documented PR from the branch's changes. |
| "What did the CI review say?" | Findings and their verification/check status from the PR. |
| "Address the review and re-run" | The authorized repair loop: findings, failing checks, push, and review re-arm. |

Local output follows this shape:

```markdown
## Verdict
<one-sentence conclusion>

## Findings
| # | Severity | File | Summary |
|---|----------|------|---------|
| 1 | warning | <path>:<line> | <verified issue> |

### 1. <path>:<line> — warning
<issue, failure mode, evidence, and suggested fix>

## Notes (no inline anchor)
<cross-cutting concerns and limitations>

**Recommendation:** approve / request-changes / comment-only
```

CI adds a runtime-written **Check status** and structured
`review-output/3.0` artifact. An `approve` recommendation is advisory; the
strictness gate and completion status decide whether the check passed.
An unfinished or timed-out review is not a clean pass.

### Configure the merge gate

| Setting | Purpose |
| --- | --- |
| `strictness` | `lenient`, `block-on-critical`, `block-on-warning`, or `block-on-any`; v3 verifies claimed critical findings before they block. |
| `trigger-mode` and `label-gate` | Review every eligible event or only selected PRs, for example through `Ready`. |
| `prompt-extension-file` | Set `.review/extension.md` to share local and CI rules. |
| `provider`, `api-base`, and `model` | Choose the runner, backend endpoint, and model or supported cost tier. |
| `budget-profile` | Select risk-aware review budgets or a fixed budget. |

The Action supports the `anthropic`, `openai`, `claude-code`, `cursor`,
`codex`, and `grok` runners. Backend support depends on the runner; use the
[provider matrix](docs/PROVIDERS.md), [complete input/output reference](skills/ai-diff-reviewer/setup/reference.md),
and [workflow examples](examples/README.md) rather than inferring compatibility.
Require the review job in your branch rules if it should gate merging.

### Run an independent reviewer lane

A lead can assign the local review to a separate vendor/account through
[coding-agents-kit](https://github.com/DailybotHQ/coding-agents-kit) v0.5.0,
and collect its findings through [herdr-peers](https://github.com/DailybotHQ/herdr-peers)
v0.2.0. For a Claude author, the lead selects a Codex reviewer:

```bash
ak pick codex --json
```

Read the selected account from the `pick-v1` result; it is a selection,
not an agent launch. The lead freezes a clean base/head range and pauses
author writes. The reviewer reports completion status and findings through
an authorized herdr-peers reply; the lead verifies those findings and holds
integration for blockers, repository gates, and release-order approval.
A stale head or missing reply does not count as a completed review. See the
[shipped orchestra guide](skills/ai-diff-reviewer/orchestra.md) for the full
protocol, remote snapshots, and read-only boundaries.

## How it fits with DeepWorkPlan

The [AI Diff Reviewer addon](https://deepworkplan.com/kit/ai-diff-reviewer)
wires the local skill into a DeepWorkPlan Final Review, with optional CI
review on the PR. The plan supplies the acceptance criteria and validation
gates; AI Diff Reviewer supplies findings and review evidence. AgentKit
selects independent accounts, herdr-peers carries the handoff, and Dailybot
can report each lane's progress. These integrations do not replace the
repository's tests or the authorized merge decision.

## Part of the DeepWorkPlan ecosystem

[DeepWorkPlan](https://deepworkplan.com) turns any repository into an AI-first,
agent-pilotable codebase. These projects ship together; every addon is optional.

| Repository | Role | Purpose |
| --- | --- | --- |
| [deepworkplan-skill](https://github.com/DailybotHQ/deepworkplan-skill) | Pack | The methodology, specification and kit as an installable agent skill |
| [coding-agents-kit](https://github.com/DailybotHQ/coding-agents-kit) | Addon: agentkit | One command for every coding agent: accounts, quota, `ak pick`, headless runs |
| [devcontainer-kit](https://github.com/DailybotHQ/devcontainer-kit) | Addon: devcontainer | A reproducible dev container per repository, wired to Herdr and the host's accounts |
| [herdr-peers](https://github.com/DailybotHQ/herdr-peers) | Addon: Herdr | Ask an agent in another Herdr pane, on any machine, and get one authorized reply |
| [deepworkplan-vim](https://github.com/DailybotHQ/deepworkplan-vim) | Addon: DeepWorkPlan Vim | The terminal editor for plans: plan browser, lanes view, Markdown viewer |
| [ai-diff-reviewer](https://github.com/DailybotHQ/ai-diff-reviewer) (this repository) | Required local review | Reviews a change set locally and in CI; every Final Review runs it |
| [agent-skill](https://github.com/DailybotHQ/agent-skill) | Addon: Dailybot | The Dailybot agent skill: progress reports, check-ins, chat and more |
| [deepworkplan.com](https://deepworkplan.com) ([source](https://github.com/DailybotHQ/deepworkplan-website)) | Site and hub | The methodology, specification, kit catalog and `/init.md`; coordinates the ecosystem |

## Security and trust

The default local pass reads files and prints findings through the running
agent. It does not push, post GitHub comments, or modify source files; any
configuration bootstrap needs consent. The Action sends the diff and read
context to the configured model provider and posts the review to GitHub.
Keep credentials in secret storage, pin the release, and assess the
provider's data-handling policy. CLI runners have broader local capabilities
than the in-process runner: use them only on trusted inputs and follow the
[security controls](docs/SECURITY.md). Never expose secrets to untrusted
`pull_request_target` code. Vulnerability reporting: [SECURITY.md](SECURITY.md).

## Documentation and contributing

- [Documentation index](docs/README.md), [architecture](docs/ARCHITECTURE.md), and [testing](docs/TESTING_GUIDE.md)
- [Prompts and local/CI parity](docs/PROMPTS.md), [strictness](docs/STRICTNESS.md), and [trigger modes](docs/TRIGGER_MODES.md)
- [Iteration-aware review](docs/ITERATION_AWARENESS.md), [PR metadata](docs/PR_METADATA_CHECKS.md), and [v3 migration](docs/MIGRATION_v3.md)
- [Local skill](skills/ai-diff-reviewer/SKILL.md) and [reading CI feedback](docs/PR_REVIEW_WORKFLOW.md)
- [Changelog](CHANGELOG.md), [contributing](CONTRIBUTING.md), and [agent instructions](AGENTS.md)
- [MIT license](LICENSE), maintained by [Dailybot](https://www.dailybot.com)
