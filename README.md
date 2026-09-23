<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="https://raw.githubusercontent.com/DailybotHQ/ai-diff-reviewer/main/assets/logo-dark.png">
    <source media="(prefers-color-scheme: light)" srcset="https://raw.githubusercontent.com/DailybotHQ/ai-diff-reviewer/main/assets/logo-light.png">
    <img alt="AI Diff Reviewer" src="https://raw.githubusercontent.com/DailybotHQ/ai-diff-reviewer/main/assets/logo-light.png" width="440">
  </picture>
</p>

# AI Diff Reviewer

> One review methodology, two surfaces — an **LLM-driven code reviewer** that ships as a **GitHub Action** for CI **and** a **coding-agent skill** for your local machine.

[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
[![GitHub Marketplace](https://img.shields.io/github/v/release/DailybotHQ/ai-diff-reviewer?label=Marketplace&logo=github&color=success)](https://github.com/marketplace/actions/ai-diff-reviewer)
[![Powered by Dailybot](https://img.shields.io/badge/Powered%20by-Dailybot-6C5CE7.svg)](https://www.dailybot.com?utm_source=dailybotopensource&utm_medium=ai-pr-reviewer)

Reviews `git diff origin/<base>...HEAD` on every pull request (or on your feature branch, before you push), posts findings with severity tags (`critical` / `warning` / `info`), gates the check based on configurable strictness, collapses prior reviews, and auto-labels the PR when it passes. Stdlib-only Python — no Docker image, no Node modules, no infrastructure beyond your provider's API key.

The same [`prompts/default.md`](prompts/default.md) drives both surfaces. Pinning the same version on the Action and the skill guarantees **local ↔ CI parity** — what the skill flags locally is what CI will flag on the PR.

---

## Two ways to run the same review

| Surface | Where it runs | Install |
|---|---|---|
| **GitHub Action** — auto-review every PR | On `ubuntu-latest` in your CI | Add `uses: DailybotHQ/ai-diff-reviewer@v2` to a workflow → [§ jump](#in-ci--as-a-github-action) |
| **Coding-agent skill** — review before you push | In your coding agent (Cursor, Claude Code, Codex, Gemini, Copilot, Cline, Windsurf) | `npx skills add DailybotHQ/ai-diff-reviewer --skill ai-diff-reviewer` → [§ jump](#locally--as-a-coding-agent-skill) |

**Use them together** — install the skill for pre-push checks, install the Action for the merge gate, share **one `.review/extension.md`** for your repo-specific rules, and the two stay in perfect sync (see [§ Bringing them together](#bringing-them-together--reviewextensionmd)).

**Just discovered the skill?** The skill even ships a wizard that installs the Action for you — say *"Set up AI Diff Reviewer for this repo"* and it walks you through six questions to generate a tailored workflow file. No YAML editing required for the first install.

---

## Contents

### As a GitHub Action (CI)

- [Quick start](#quick-start)
- [What you get out of the box](#what-you-get-out-of-the-box)
- [Providers](#providers)
- [Inputs](#inputs)
- [Outputs](#outputs)
- [Strictness levels](#strictness-levels)
- [Controlling cost & access](#controlling-cost--access)
- [Recipes](#recipes)
- [Required permissions](#required-permissions)

### As a coding-agent skill (local)

- [Quick start (skill)](#quick-start-skill)
- [The five sub-skills](#the-five-sub-skills)
- [First-run bootstrap prompt](#first-run-bootstrap-prompt)
- [Sub-skill: run a local review](#sub-skill-run-a-local-review-default-flow)
- [Sub-skill: `setup` — install the Action via wizard](#sub-skill-setup--install-the-action-via-wizard)
- [Sub-skill: `generate-extension` — tailor the reviewer](#sub-skill-generate-extension--tailor-the-reviewer-to-your-repo)
- [Sub-skill: `open-pr` — author the PR from the diff](#sub-skill-open-pr--author-the-pr-from-the-same-diff)

### Shared foundations

- [Bringing them together — `.review/extension.md`](#bringing-them-together--reviewextensionmd)
- [How it works](#how-it-works)
- [Provider roadmap](#provider-roadmap)
- [Documentation](#documentation)
- [FAQ](#faq)
- [Contributing](#contributing) · [License](#license)

---

# In CI — as a GitHub Action

The primary surface: an auto-triggered code review on every pull request, with inline comments and severity-based merge gating. Zero infrastructure — the action is a stdlib-only Python composite step that runs on `ubuntu-latest` without an install phase.

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
      - uses: DailybotHQ/ai-diff-reviewer@v2
        with:
          api-key: ${{ secrets.ANTHROPIC_API_KEY }}
          github-token: ${{ secrets.GITHUB_TOKEN }}
```

That's the minimum. Open a PR; the action posts a tracking comment, runs a review, and updates the comment with the result.

**Prefer a guided install?** The [companion skill's `setup` sub-skill](#sub-skill-setup--install-the-action-via-wizard) generates this file for you — six questions (provider, strictness, trigger mode, external-contributor policy, PR-description mode, complexity labels) → tailored workflow.

## What you get out of the box

- **Inline comments** anchored to specific lines, with optional GitHub suggestion blocks (one-click apply).
- **Tracking comment** with a `<!-- ai-pr-reviewer-marker -->` marker that transitions in-place from `Working…` → `View review →`.
- **Auto-collapse** of *this provider's* previous reviews on every new push, scoped by a per-provider marker — leaves other bots' comments (coverage, labelers) and other-provider reviews alone, so multi-provider dogfooding on the same PR just works.
- **Severity-based gating**: the model assigns `critical` / `warning` / `info` to each finding; you decide what fails the check.
- **Optional label gate**: only run when a PR carries a label (e.g. `ready` or `AIReview`).
- **Optional complexity labels**: when `complexity-labels-enabled` is `true`, the reviewer assesses cognitive load and applies `complexity:low`, `complexity:medium`, or `complexity:high` (not line-count based). See [docs/PR_METADATA_CHECKS.md](docs/PR_METADATA_CHECKS.md).
- **Optional "reviewed" label**: applied automatically after a successful, non-blocked review.
- **Self-healing on GitHub 422**: if the model anchors a comment outside the diff, the action retries with only the comments whose anchor is provably inside a diff hunk (naming the dropped ones in the log), and falls back to summary-only as a last resort — one bad anchor no longer costs every other comment (v2.3.1).
- **One source of truth for the check** (v2.3.1): the pass/fail decision is computed once, before the review is posted, and every review body ends with a runtime-written `> **Check status: ✅ passing / 🚫 failing**` block that matches the tracking comment and the check conclusion. A model `Recommendation: approve` is rewritten to `request-changes` whenever the gate is failing, so the review can never say one thing while CI says another.
- **Iteration-Aware Review**: dedup findings against prior rounds so the same warnings don't come back forever. Every review runs with `convergence-policy: first-pass-exhaustive` by default — exhaustive first pass (up to 3× the normal cap), dedup on subsequent rounds. Four convergence policies (`iterative`, `first-pass-exhaustive`, `round-capped`, `critical-gate`), a 30% new-lines safety net that forces an exhaustive pass when a big push arrives, and a hardcoded "criticals always surface" rail that no policy can bypass. Two escape gestures: apply the `iteration-escape-label` to bypass dedup for one run (state preserved) or remove the `applied-label` before the next review to force a full state reset (fresh generation, dedup memory wiped). See [docs/ITERATION_AWARENESS.md](docs/ITERATION_AWARENESS.md) for the full spec and [examples/iteration-aware.yml](examples/iteration-aware.yml) for tuning knobs.
- **Incremental follow-up reviews** (v2.1.0+): on rounds 2+ the model receives the actual diff since its last review plus its own still-open findings from paginated PR threads. Resolution claims are advisory by default — a maintainer resolves the thread — except when `collapse-previous` has already minimized that thread and the runtime can corroborate the fix (the model reported it resolved, the finding was not re-emitted, and the file changed since the finding was raised or was deleted): then the finding is retired and stops gating, reported as *auto-retired* in the summary footer (v2.3.1). Anything uncorroborated keeps counting toward the strictness gate without duplicate comments. The summary reports prior-finding status; cap and turn budgets scale with the delta. Base movement, rebases / force-pushes, incomplete history, the escape label and the 30% safety net force a full review. See [docs/ITERATION_AWARENESS.md § 14](docs/ITERATION_AWARENESS.md).
- **Six runners, thirteen backends, your billing** (v2.1.0+; v2.4.0 adds DeepSeek, Moonshot/Kimi, MiniMax, Qwen, Google Gemini and OpenRouter behind `api-base`; v2.5.0 adds AWS Bedrock on the `anthropic` runner with in-process SigV4): each backend through the in-process loop or a vendor CLI — see [Providers](#providers). One-word model tiers (`balanced` / `economy` / `deep`) resolve per vendor from a dated matrix.
- **Cost you can see** (v2.1.0+): every tracking comment ends with a `**Usage:**` line (tokens in/out, cache hit rate, turns, cost — vendor-reported where the CLI gives it, marked `(indicative)` when estimated) and `iteration-tokens-used` carries the real number. Lockfiles, minified bundles, source maps and vendored trees are dropped from the diff automatically (`ignore-paths` adds yours); the prompt and the diff are cached across turns.
- **Hardened by default** (v2.1.0+): the key goes to exactly one configured host (custom hosts are called out in the log), vendor CLIs receive only their own credential, per-run config files are private and removed, vendor output is size-bounded, redirects are refused. See [docs/SECURITY.md](docs/SECURITY.md).
- **Emergency-bypass label** (`skip-review-label`): opt-in short-circuit when a PR carries e.g. `skip-ai-review` — no LLM call, check stays green, audit tracking comment posted. Pair with a ruleset restricting who can apply the label. See [docs/TRIGGER_MODES.md § Emergency-bypass label](docs/TRIGGER_MODES.md) and [examples/skip-review-label.yml](examples/skip-review-label.yml).

## Providers

Six runners ship, each pointing at any of **thirteen backends**. Pick the **vendor** whose models and billing you want; each runs through one or more **runners**. The `provider` input names the runner — who drives the review loop: the action's own bounded in-process loop (`anthropic`, `openai`, zero install) or a vendor coding-agent CLI installed on the fly (`claude-code`, `codex`, `grok`, `cursor`). The optional `api-base` input points a runner at a vendor other than its default; `api-key` always carries the credential of the vendor you chose. Full mechanics and the dated cost matrix: [docs/PROVIDERS.md](docs/PROVIDERS.md).

| Provider (vendor) | Runs through — `provider:` | `api-base` | Secret to store as `api-key` | Default / recommended model | Billing | Status (2026-09-22) |
|---|---|---|---|---|---|---|
| **Anthropic (Claude)** | `anthropic` *(default, in-process)* · `claude-code` *(CLI)* | *(default)* | `ANTHROPIC_API_KEY` — or a `claude setup-token` subscription token (`sk-ant-oat…`) with `claude-code` | `claude-sonnet-4-6` (built-in); `model: balanced` → `claude-sonnet-5` | metered API, or Claude Pro/Max subscription | ✅ since v1.0 / v1.2.1 |
| **OpenAI** | `openai` *(in-process)* · `codex` *(CLI)* | *(default)* | `OPENAI_API_KEY` | `gpt-5.6-luna` (`balanced` and `economy`) | metered API | ✅ `codex` v1.2.1 · `openai` v2.1.0 |
| **Azure Foundry / Azure OpenAI** | `openai` *(in-process)* · `codex` *(CLI)* | `https://<resource>.services.ai.azure.com/openai/v1` (or `https://<resource>.openai.azure.com/openai/v1`) | `AZURE_OPENAI_API_KEY` | your **deployment name** (no tier aliases on Azure) | Azure metered | ✅ v2.1.0 — both runners verified live |
| **xAI (Grok)** | `grok` *(CLI, default vendor)* · `openai` *(in-process)* · `anthropic` *(in-process)* | *(default)* for `grok`; `https://api.x.ai/v1` for `openai`; `https://api.x.ai` for `anthropic` | `XAI_API_KEY` | `grok-4.5` built-in default, `balanced` and `economy`; `deep` → `grok-4.6` (benchmark 2026-09-16: 4.3 found 0 of 5 known defects, so it is not offered as a tier — pin `model: grok-4.3` explicitly if you want it) | xAI credits | ✅ v2.1.0 — `grok` and `openai` verified live; `codex` on xAI not usable (Codex ≥ 0.154 sends a tool type xAI rejects) |
| **Z.ai GLM** | `claude-code` *(CLI — recommended)* · `anthropic` *(in-process)* · `openai` *(in-process)* · `codex` *(CLI)* | `https://api.z.ai/api/anthropic` (`claude-code`, `anthropic`) · `https://api.z.ai/api/coding/paas/v4` (`openai`) · `https://api.z.ai/api/v1` (`codex`) | `ZAI_CODING_API_KEY` | `glm-5.3` (`balanced`); `glm-5.3-flash` (`economy`) | flat-rate Coding Plan (marginal cost ≈ 0) | ✅ v2.1.0 — verified offline; first live run pending |
| **DeepSeek** | `openai` *(in-process)* | `https://api.deepseek.com` | `DEEPSEEK_API_KEY` | `deepseek-chat` (`balanced` and `economy`); `deep` → `deepseek-reasoner` | metered API | ✅ v2.4.0 |
| **Moonshot / Kimi** | `openai` *(OpenAI-compatible)* · `anthropic` · `claude-code` *(Anthropic-compatible)* | `https://api.moonshot.ai/v1` (`openai`) · `https://api.moonshot.ai/anthropic` (`anthropic`, `claude-code`) | `MOONSHOT_API_KEY` | `kimi-k2-0905-preview` (`balanced` and `deep`); `kimi-k2-turbo-preview` (`economy`) | metered API | ✅ v2.4.0 |
| **MiniMax** | `openai` *(OpenAI-compatible)* · `anthropic` · `claude-code` *(Anthropic-compatible)* | `https://api.minimax.io/v1` (`openai`) · `https://api.minimax.io/anthropic` (`anthropic`, `claude-code`) | `MINIMAX_API_KEY` | `MiniMax-M2` (`balanced` and `deep`); `MiniMax-Text-01` (`economy`) | metered API | ✅ v2.4.0 |
| **Qwen (Alibaba DashScope)** | `openai` *(in-process)* | `https://dashscope.aliyuncs.com/compatible-mode/v1` | `DASHSCOPE_API_KEY` | `qwen3-coder-plus` (`balanced` and `deep`); `qwen-turbo` (`economy`) | metered API | ✅ v2.4.0 |
| **Google Gemini** | `openai` *(in-process)* | `https://generativelanguage.googleapis.com/v1beta/openai` | `GEMINI_API_KEY` | `gemini-2.5-pro` (`balanced` and `deep`); `gemini-2.5-flash` (`economy`) | metered API (free tier available) | ✅ v2.4.0 — `seed` omitted (the endpoint rejects it) |
| **OpenRouter** | `openai` *(in-process)* | `https://openrouter.ai/api/v1` | `OPENROUTER_API_KEY` | `deepseek/deepseek-chat` (`balanced` and `economy`); `deep` → `deepseek/deepseek-reasoner` — vendor-prefixed ids | OpenRouter's listed prices (underlying vendor + margin) | ✅ v2.4.0 — meta-gateway, hundreds of models behind one key |
| **AWS Bedrock (Claude)** | `anthropic` *(in-process — SigV4-signed InvokeModel)* | `https://bedrock-runtime.{region}.amazonaws.com` | AWS credentials: env (OIDC) **or** packed `KEY:SECRET[:SESSION]` as `api-key` | `us.anthropic.claude-sonnet-5` (`balanced`); `us.anthropic.claude-haiku-4-5` (`economy`); `us.anthropic.claude-opus-5` (`deep`) — inference-profile ids | AWS Bedrock metered | ✅ v2.5.0 — Anthropic-shape InvokeModel; `cache_control` omitted in v1 |
| **Cursor** | `cursor` *(CLI)* | *(none — no bring-your-own endpoint)* | `CURSOR_API_KEY` | `auto` | Cursor Pro (unlimited) | ✅ v1.2.1 |
| **Self-hosted / any compatible gateway** | `anthropic` · `claude-code` (Anthropic-shaped) · `openai` · `codex` (OpenAI-shaped) | your `https://` URL (`http://` only for `localhost`) | your gateway's key | the gateway's model id | — | ✅ v2.1.0 — treated as a custom host; the run logs where the key goes |

- **The six v2.4.0 chat-completions backends (DeepSeek, Moonshot/Kimi, MiniMax, Qwen, Gemini, OpenRouter) run through `provider: openai`.** The Codex CLI speaks the Responses API to every non-default gateway, which none of these vendors implements — the documented xAI limitation applies to all six. Moonshot/Kimi and MiniMax are additionally reachable through their Anthropic-compatible endpoints with `anthropic` / `claude-code`.
- **In-process runners (`anthropic`, `openai`)** are the simplest and cheapest to operate: no install, a bounded tool-use loop, prompt caching, a real usage line on every review. Recommended for most repos.
- **CLI runners (`claude-code`, `codex`, `grok`, `cursor`)** hand the review to a vendor coding agent (deeper code comprehension, vendor-tuned tools) at the cost of an install step and higher token use. They run with broad local access — on public repos use them only on trusted (non-fork) PRs.
- **One word for the model:** `model: balanced` (recommended), `economy` (smoke passes) or `deep` (high-risk PRs) resolves to the vendor's current pick from the dated matrix; an explicit id always passes through; empty keeps the built-in default.

### Switching provider

```yaml
# Cursor — flat-rate on Pro
- uses: DailybotHQ/ai-diff-reviewer@v2
  with:
    provider: cursor
    api-key: ${{ secrets.CURSOR_API_KEY }}
    github-token: ${{ secrets.GITHUB_TOKEN }}
```

```yaml
# OpenAI Codex
- uses: DailybotHQ/ai-diff-reviewer@v2
  with:
    provider: codex
    api-key: ${{ secrets.OPENAI_API_KEY }}
    github-token: ${{ secrets.GITHUB_TOKEN }}
```

```yaml
# xAI Grok CLI — full review contract, web search off, token never reaches the agent
- uses: DailybotHQ/ai-diff-reviewer@v2
  with:
    provider: grok
    api-key: ${{ secrets.XAI_API_KEY }}
    github-token: ${{ secrets.GITHUB_TOKEN }}
```

```yaml
# OpenAI Codex on Azure Foundry — model is your deployment name
- uses: DailybotHQ/ai-diff-reviewer@v2
  with:
    provider: codex
    api-base: https://<resource>.services.ai.azure.com/openai/v1
    api-key: ${{ secrets.AZURE_OPENAI_API_KEY }}
    github-token: ${{ secrets.GITHUB_TOKEN }}
    model: gpt-5.4-mini-azure
```

```yaml
# Z.ai GLM through Claude Code — the recommended GLM runner (flat-rate Coding Plan)
- uses: DailybotHQ/ai-diff-reviewer@v2
  with:
    provider: claude-code
    api-base: https://api.z.ai/api/anthropic
    api-key: ${{ secrets.ZAI_CODING_API_KEY }}
    github-token: ${{ secrets.GITHUB_TOKEN }}
    model: glm-5.3          # required on a custom backend
```

```yaml
# Z.ai GLM in-process (bounded loop, zero install)
- uses: DailybotHQ/ai-diff-reviewer@v2
  with:
    provider: anthropic
    api-base: https://api.z.ai/api/anthropic
    api-key: ${{ secrets.ZAI_CODING_API_KEY }}
    github-token: ${{ secrets.GITHUB_TOKEN }}
    model: balanced         # → glm-5.3
```

```yaml
# Azure Foundry in-process (bounded loop, zero install) — model is your deployment name
- uses: DailybotHQ/ai-diff-reviewer@v2
  with:
    provider: openai
    api-base: https://<resource>.services.ai.azure.com/openai/v1
    api-key: ${{ secrets.AZURE_OPENAI_API_KEY }}
    github-token: ${{ secrets.GITHUB_TOKEN }}
    model: <your-deployment-name>
```

```yaml
# xAI in-process (bounded loop) — same key as the Grok CLI
- uses: DailybotHQ/ai-diff-reviewer@v2
  with:
    provider: openai
    api-base: https://api.x.ai/v1
    api-key: ${{ secrets.XAI_API_KEY }}
    github-token: ${{ secrets.GITHUB_TOKEN }}
    model: balanced         # → grok-4.5 (deep → grok-4.6; 4.3 measured 0/5 — not offered)
```

Ready-to-copy workflows per provider: [`examples/provider-openai.yml`](examples/provider-openai.yml), [`examples/provider-anthropic-zai.yml`](examples/provider-anthropic-zai.yml), [`examples/provider-claude-code.yml`](examples/provider-claude-code.yml), [`examples/provider-claude-code-glm.yml`](examples/provider-claude-code-glm.yml), [`examples/provider-codex-azure.yml`](examples/provider-codex-azure.yml), [`examples/provider-grok.yml`](examples/provider-grok.yml), [`examples/provider-cursor.yml`](examples/provider-cursor.yml), [`examples/provider-codex.yml`](examples/provider-codex.yml).

### Bill Claude Code against a subscription (instead of API tokens)

Like Cursor, `claude-code` can bill against a **Claude Pro/Max subscription**. Run `claude setup-token` on a machine logged into your plan, store the resulting `sk-ant-oat…` token as a secret, and pass it as `api-key` — the action auto-detects the prefix and uses subscription auth:

```yaml
- uses: DailybotHQ/ai-diff-reviewer@v2
  with:
    provider: claude-code
    api-key: ${{ secrets.CLAUDE_CODE_OAUTH_TOKEN }}   # sk-ant-oat… subscription token
    github-token: ${{ secrets.GITHUB_TOKEN }}
```

> **Security:** a subscription token grants broader account access than a scoped API key. On public repos, use the CLI providers only on trusted (non-fork) PRs and set `persist-credentials: false` on `actions/checkout`. Full details: [docs/PROVIDERS.md](docs/PROVIDERS.md) and [docs/SECURITY.md](docs/SECURITY.md).

## Inputs

| Input | Required | Default | Description |
|---|---|---|---|
| `api-key` | ✅* | — | Provider API key. For Anthropic this is your `ANTHROPIC_API_KEY`. *Exception — AWS Bedrock (`provider: anthropic` + a `bedrock-runtime` api-base): omit it and the action reads the AWS credentials exported by the `configure-aws-credentials` step (OIDC), or pass the packed `KEY:SECRET[:SESSION]`. |
| `github-token` | ✅ | — | Token with `pull-requests: write` and `contents: read`. The default `secrets.GITHUB_TOKEN` works; pass a PAT or automation-bot token if you want the review attributed to a specific account. |
| `provider` | | `anthropic` | LLM provider (the runner). `anthropic` / `openai` (chat-completions, zero install), `claude-code` / `cursor` / `codex` / `grok` (agent-runner CLIs, installed only when selected). Pair with `api-base` to choose the backend. See [docs/PROVIDERS.md](docs/PROVIDERS.md). |
| `model` | | provider default | Model id **or a tier alias** (`balanced` / `economy` / `deep`, resolved per runner × backend — see the cost-efficient defaults matrix). Built-in defaults balance review quality vs cost. Anthropic → `claude-sonnet-4-6`, Claude Code → `claude-sonnet-4-6` (never `auto`; Claude Code's `api-key` also accepts a `claude setup-token` subscription token, `sk-ant-oat…`), Cursor → `auto` (flat-rate on Pro), Codex → `gpt-5.6-luna` (`gpt-5-codex` is deprecated). See [docs/PROVIDERS.md](docs/PROVIDERS.md#choosing-a-cost-efficient-model). |
| `ignore-paths` | | _(built-ins only)_ | Extra globs whose diff sections are **omitted from the prompt**, on top of the built-in lockfile / minified / source-map / vendored / snapshot list. Omitted files are still listed to the model with line counts; exclusion happens before the diff-size cap so lockfiles never crowd out real changes. Saves tokens on every turn. Example: `**/*.generated.ts, docs/api/**`. |
| `api-base` | | provider default | Bring your own endpoint for the chosen provider. Empty keeps the provider's default endpoint (request shape unchanged; since v2.4.0 the default profiles pin deterministic sampling — see [docs/PROVIDERS.md](docs/PROVIDERS.md)). Point `anthropic` / `claude-code` at an Anthropic-compatible gateway (Z.ai GLM: `https://api.z.ai/api/anthropic`; xAI: `https://api.x.ai`; Moonshot/Kimi: `https://api.moonshot.ai/anthropic`; MiniMax: `https://api.minimax.io/anthropic`) or `codex` / `openai` at an OpenAI-compatible one (Azure Foundry v1: `https://<resource>.services.ai.azure.com/openai/v1` with `model` = deployment name; xAI: `https://api.x.ai/v1`; Z.ai: `https://api.z.ai/api/v1` for Codex, `https://api.z.ai/api/coding/paas/v4` for `openai`; and — `openai` only, these are chat-completions surfaces the Codex CLI's Responses API cannot reach — DeepSeek: `https://api.deepseek.com`; Moonshot/Kimi: `https://api.moonshot.ai/v1`; MiniMax: `https://api.minimax.io/v1`; Qwen/DashScope: `https://dashscope.aliyuncs.com/compatible-mode/v1`; Google Gemini: `https://generativelanguage.googleapis.com/v1beta/openai`; OpenRouter: `https://openrouter.ai/api/v1`), and AWS Bedrock is its own Anthropic-shape surface (`https://bedrock-runtime.{region}.amazonaws.com` with `provider: anthropic` — SigV4-signed in-process; credentials from the AWS env or the packed format). The host selects the endpoint profile automatically. **Your `api-key` is sent to this host** — trusted endpoints only; `https://` required. Ignored by `cursor`. See [docs/PROVIDERS.md](docs/PROVIDERS.md). |
| `prompt-file` | | bundled `prompts/default.md` | Path **inside the consumer checkout** to a markdown system prompt. FULLY REPLACES the base. Customising the prompt is the main lever for adapting the review to your codebase — see [docs/PROMPTS.md](docs/PROMPTS.md). |
| `prompt-extension-file` | | _(empty)_ | Path **inside the consumer checkout** to a markdown file APPENDED to the base prompt. Use to layer overrides without copying the whole default. Combines with `prompt-file` (base + extension). Starter templates in [`examples/prompts/`](examples/prompts/). |
| `author-association` | | `OWNER,MEMBER,COLLABORATOR` | Comma-separated whitelist of GitHub `pull_request.author_association` values allowed to trigger a review. Default is write-tier only — the safe baseline for public open-source repos (prevents external-contributor PR spam from burning your LLM budget). On **private / internal** repos, when the webhook value is not in the list, the runtime also checks collaborator permission (`admin` / `maintain` / `write` still pass — fixes webhook under-reporting). Public repos stay association-only. Add `CONTRIBUTOR` to allow returning contributors, or set to empty string to disable the gate. See [docs/SECURITY.md § "Author-association gate"](docs/SECURITY.md). |
| `label-gate` | | `''` | If non-empty, the review only runs when the PR carries this label (e.g. `ready`). Combined with `trigger-mode`. |
| `trigger-mode` | | _(auto)_ | `always` / `label-required` / `label-once` / `label-added-only` — see [docs/TRIGGER_MODES.md](docs/TRIGGER_MODES.md). Empty picks `label-required` when `label-gate` is set, else `always`. |
| `applied-label` | | `''` | If non-empty, this label is applied to the PR after a successful, non-blocked review (e.g. `pr-reviewed`). The label is auto-created if it doesn't exist. |
| `skip-review-label` | | `''` | Optional emergency-bypass label (e.g. `skip-ai-review`). When BOTH the workflow trigger fires AND this label is on the PR, the reviewer short-circuits: no LLM call, no findings, no state mutation, the GitHub check reports success. A tracking comment records the skip. Intended for hotfixes / rollbacks. **Security:** anyone who can label a PR can bypass review — pair with a ruleset/CODEOWNERS rule restricting who can apply this label. **Misconfig guard:** the reviewer aborts at startup if this value collides with `label-gate`, `applied-label`, or `iteration-escape-label` (each collision would silently convert every normal trigger into a skip). Empty (default) disables the feature. See [docs/TRIGGER_MODES.md § Emergency-bypass label](docs/TRIGGER_MODES.md). |
| `collapse-previous` | | `true` | Mark previous bot reviews/comments as `OUTDATED` via GraphQL `minimizeComment`. |
| `tracking-comment` | | `true` | Post a spinner comment that transitions to the final review URL. |
| `strictness` | | `lenient` | `lenient` / `block-on-critical` / `block-on-warning` / `block-on-any` — see [docs/STRICTNESS.md](docs/STRICTNESS.md). |
| `max-inline-comments` | | `10` | Hard cap on inline comments per review. |
| `pr-description-mode` | | `off` | `off` / `warn` / `block` / `autocomplete` — see [docs/PR_METADATA_CHECKS.md](docs/PR_METADATA_CHECKS.md). |
| `pr-description-min-length` | | `50` | Char threshold below which the PR body is treated as vague. |
| `complexity-labels-enabled` | | `false` | When `true`, the reviewer applies a `complexity:low/medium/high` label to the PR. |
| `complexity-label-prefix` | | `complexity:` | Prefix for the complexity label (change to match your labeling conventions). |
| `max-turns` | | `30` | Hard cap on the agentic-loop iterations (chat-completions providers only). |
| `agent-max-turns` | | `''` | Turn cap for agent-runner CLIs. Enforced natively on `grok` (`--max-turns`); Claude Code / Codex / Cursor expose no stable turn flag, so the run logs a per-provider warning with the equivalent knob (`--max-budget-usd` via `agent-extra-args` for Claude Code) and the 900 s timeout bounds the run. Ignored for chat-completions providers. |
| `agent-extra-args` | | `''` | Raw string appended to the CLI invocation. Parsed with `shlex.split` (never `shell=True`). Escape hatch for provider-specific flags. |
| `mcp-config-file` | | `''` | Path inside the consumer checkout to an MCP servers JSON config. If set, the file is copied to the CLI's expected location before invocation. |
| `claude-code-version` | | `''` | Pin the Claude Code CLI version (npm semver). Empty = latest. |
| `cursor-version` | | `''` | Pin the Cursor Agent CLI version (e.g. `2026.09.15-d2fe57e`). A pinned version installs the versioned package directly — the vendor's installer script ignores version hints. Empty = the current release. |
| `codex-version` | | `''` | Pin the OpenAI Codex CLI version (npm semver). Empty = latest. |
| `grok-version` | | `''` | Pin the xAI Grok CLI version (`bash -s <X.Y.Z>` on the official installer). Only used when `provider: grok`. |
| `cursor-installer-sha256` | | `''` | SHA-256 of the artefact the Cursor install step downloads (the versioned package when `cursor-version` is set, else the installer script). When set, a mismatch fails the step before anything runs; when empty, the step logs the observed hash so you can pin it. |
| `grok-installer-sha256` | | `''` | SHA-256 of the xAI Grok installer script. When set, a mismatch fails the step before anything runs; when empty, the step logs the observed hash. Pair with `grok-version` to pin the binary too. |
| `convergence-policy` | | `first-pass-exhaustive` | Iteration-Aware Review policy. Default `first-pass-exhaustive` (exhaustive round 1 + higher cap, dedup on rounds 2+) — solves the "10 loops of trickled warnings" pain. Alternatives: `iterative` (dedup only, cost-neutral), `round-capped` (post-cap only critical surfaces), `critical-gate` (strict cross-gen dedup). See [docs/ITERATION_AWARENESS.md](docs/ITERATION_AWARENESS.md). |
| `max-review-rounds` | | `0` | Hard cap for `round-capped`. `0` = unlimited. After N rounds only critical severity findings surface. Ignored by other policies. |
| `exhaustive-first-pass-cap-multiplier` | | `3` | Multiplier applied to `max-inline-comments` on round 1 of each generation when policy is `first-pass-exhaustive`. Set to `1` to keep exhaustive prompting without amplification. |
| `iteration-escape-label` | | `full-review-please` | Label a human applies to force a full review — dedup skipped for that run, persisted state unchanged. |
| `prior-findings-resolution` | | `advisory` | Incremental rounds: `advisory` reports the model's `resolved` verdicts, leaves threads to a maintainer and keeps the finding gating — except a thread `collapse-previous` already minimized, where a corroborated verdict retires the finding without touching the thread (v2.3.1); `verified` also replies on and resolves the thread. Corroboration under both: fingerprint gone **and** file changed since the finding was raised, or deleted. See [docs/ITERATION_AWARENESS.md § 14.4](docs/ITERATION_AWARENESS.md). |

**Prefer to look up an input from your coding agent?** The [companion skill's `setup` sub-skill](#sub-skill-setup--install-the-action-via-wizard) doubles as a reference manual — ask any agent with the skill installed *"what does `strictness` do?"* or *"how do I pin the Cursor CLI version?"* and it answers from [`skills/ai-diff-reviewer/setup/reference.md`](skills/ai-diff-reviewer/setup/reference.md) without opening the action source.

## Outputs

| Output | Type | Description |
|---|---|---|
| `review-url` | string | URL of the posted PR review (empty on skip). |
| `severity` | `none` \| `info` \| `warning` \| `critical` | Highest severity flagged. |
| `inline-attached` | int | Inline comments actually attached. |
| `inline-dropped` | int | Inline comments dropped because GitHub returned 422. |
| `blocked` | bool | Whether strictness blocked the check. When `true`, the action exits with code 2. |
| `skipped` | bool | Whether the run was skipped without invoking the LLM (label/author gate OR `skip-review-label` emergency bypass). |
| `iteration-round` | int (as string) | IAR round number within the current generation. Populated on every successful IAR pipeline run; empty if the pipeline crashed mid-flight (caught by the try/except safety net). |
| `iteration-generation` | int (as string) | IAR generation counter; increments on new commits or rebase. Empty if the IAR pipeline crashed. |
| `iteration-policy-applied` | string | Which IAR policy actually fired this run. Usually matches `convergence-policy`; the 30% new-lines safety net overrides it to `safety-net-forced-first-pass-exhaustive` and the escape label overrides to `escape-label-forced-full-review`. Empty if the IAR pipeline crashed. |
| `iteration-tokens-used` | int (as string) | Total tokens this review actually consumed — every input partition (uncached, cache-read, cache-write, each counted once) plus output —, captured from the provider — API `usage` objects (`anthropic` / `openai`), the Claude Code stream-json `result` event, Codex `--json` `turn.completed` events, or the Grok JSON document. `0` when the provider reports nothing (Cursor). The tracking comment shows the same numbers with cache ratio, turns and an indicative cost; never gate CI on the value. Empty string ONLY if the IAR pipeline crashed. |
| `iteration-cost-vs-baseline-estimate` | string | Coarse cost-delta heuristic derived from cap expansion + a small addendum flag. Always `"0%"` or `"+N%"` today (silenced-finding savings not yet modelled — see [`docs/ITERATION_AWARENESS.md § 9.5`](docs/ITERATION_AWARENESS.md)). Empty if the IAR pipeline crashed. |

Consume them in a later step by giving the action step an `id`:

```yaml
      - id: review
        uses: DailybotHQ/ai-diff-reviewer@v2
        with:
          api-key: ${{ secrets.ANTHROPIC_API_KEY }}
          github-token: ${{ secrets.GITHUB_TOKEN }}
      - if: steps.review.outputs.severity == 'critical'
        run: echo "Critical findings — see ${{ steps.review.outputs.review-url }}"
```

## Strictness levels

| Mode | What fails the check |
|---|---|
| `lenient` (default) | Nothing. The review posts; the check is always green. |
| `block-on-critical` | One or more inline comments tagged `critical`. |
| `block-on-warning` | One or more inline comments tagged `critical` or `warning`. |
| `block-on-any` | Any inline comment at all, including `info`. Zero-tolerance mode — use for security-critical or regulated stacks where every finding must be resolved before merge. |

The model decides severity per inline comment via the tool's `severity` argument; the bundled default prompt explains the levels in detail. Customise the prompt to make the model more or less aggressive about each tier.

Full guide: [docs/STRICTNESS.md](docs/STRICTNESS.md).

## Controlling cost & access

Every review spends tokens, so the action layers three controls, evaluated **cheapest-first** — a denied gate costs **zero API calls**:

**1. Who can trigger a review — `author-association`** (default `OWNER,MEMBER,COLLABORATOR` = write-tier only). This is evaluated **first**, before the diff is even fetched, so an outsider opening PRs from a fork can never burn your budget on public repos. The webhook field cannot be spoofed; when it under-reports membership on private org repos, a collaborator-permission API check still allows write-tier authors.

| Want to… | Set |
|---|---|
| Only people with write access (default, safe for public repos) | `OWNER,MEMBER,COLLABORATOR` |
| Also allow returning contributors | `OWNER,MEMBER,COLLABORATOR,CONTRIBUTOR` |
| Only org members (public repos — association-only) | `OWNER,MEMBER` |
| Disable the gate entirely | `''` (empty) |

**2. When it runs — `label-gate` + `trigger-mode`:**

| Behaviour | Set |
|---|---|
| Every push (default) | `trigger-mode: always` |
| Only when the PR has a label | `label-gate: ready` (matching is **case-insensitive**: `ready`/`Ready`/`READY`) |
| Only once per label application (re-run by toggling the label off/on) | `label-gate: ready` + `trigger-mode: label-once` |
| Only on the moment the label is added | `trigger-mode: label-added-only` (workflow must subscribe with `types: [labeled]`) |

**3. How much each run spends — `model`, `max-inline-comments`, `max-turns`:**

- Pick a cost profile in one word: `model: balanced` (quality/cost sweet spot — the recommendation), `model: economy` (smoke passes, docs-only PRs) or `model: deep` (high-risk PRs). The runtime resolves the concrete id per runner × backend from the dated matrix in [docs/PROVIDERS.md § "Cost-efficient defaults matrix"](docs/PROVIDERS.md#cost-efficient-defaults-matrix-verified-2026-09-21--ids-and-prices-move-re-check-when-bumping) — modelled on Cursor `auto`, which stays the flat-rate pick on Pro. Empty `model` keeps the built-in default; an explicit id always passes through. `agent-max-turns` is enforced natively on `grok`.
- `max-inline-comments` (default `10`) caps how many comments a run can post; `max-turns` (default `30`, chat-completions only) is a safety ceiling on the agentic loop.
- **Fewer tokens per turn, automatically:** lockfiles, minified bundles, source maps, vendored trees and snapshots are dropped from the diff the model sees (and listed back as omitted). Add your own generated paths with `ignore-paths`.
- **Cheaper turns after the first:** the `anthropic` runner caches both the prompt and the diff-bearing first message across turns; OpenAI/xAI cache long prefixes automatically.
- **Know what you spent:** every tracking comment ends with a `**Usage:**` line (tokens in/out, cache hit rate, cost — vendor-reported where the CLI gives it, marked `(indicative)` when estimated) and the `iteration-tokens-used` output carries the real number; round 2+ of a PR runs in incremental mode (only what changed plus your open threads) so follow-ups cost a fraction of round 1.

These compose. For a public open-source repo the safe combination is author-association (default) **+** a label gate — see the [recipe below](#public-open-source-repo-safest-defaults).

Threat model & full detail: [docs/SECURITY.md § "Author-association gate"](docs/SECURITY.md), [docs/TRIGGER_MODES.md](docs/TRIGGER_MODES.md).

## Recipes

### Run only on PRs labelled `ready`, apply `pr-reviewed` on success

```yaml
- uses: DailybotHQ/ai-diff-reviewer@v2
  with:
    api-key: ${{ secrets.ANTHROPIC_API_KEY }}
    github-token: ${{ secrets.GITHUB_TOKEN }}
    label-gate: ready
    applied-label: pr-reviewed
```

### Block merge on critical findings

```yaml
- uses: DailybotHQ/ai-diff-reviewer@v2
  with:
    api-key: ${{ secrets.ANTHROPIC_API_KEY }}
    github-token: ${{ secrets.GITHUB_TOKEN }}
    strictness: block-on-critical
```

Pair with a branch protection rule that requires the PR-review check to pass.

### Require a passing review before merge (branch protection)

Marking the review job **Required** in branch protection is *not enough* on its own: GitHub treats a required check in the **Skipped** state as **passing**, so a label-gated PR that never triggered a review still merges freely. To make "no review ⇒ no merge" real, gate the merge on a small **stable-named** job that *fails* (rather than skips) when the review didn't run — then require only that job:

```yaml
jobs:
  # … your review job (may be skipped when no label / non-critical diff) …
  review-gate:
    name: 'Review gate'          # ← mark ONLY this as the required check
    needs: [review]
    if: always()                 # report even when `review` was skipped
    runs-on: ubuntu-latest
    steps:
      - shell: bash
        run: |
          [ "${{ needs.review.result }}" = "success" ] || {
            echo "::error::No passing review — apply the label to trigger it, then merge."; exit 1; }
```

A *failing* required check blocks the merge; a *skipped* one does not. With **several** review legs (a provider matrix), the reference implementation passes the gate when **at least one** leg ran and passed — so one flaky provider can't block a merge another provider approved. Full walkthrough (single-provider + dynamic matrix + the "≥1 passed" count): [docs/TRIGGER_MODES.md § "Recipe: run once when labelled `ready`, block merge until it passes"](docs/TRIGGER_MODES.md). This repo's own [`.github/workflows/self-review.yml`](.github/workflows/self-review.yml) is the reference implementation.

### Custom prompt for your codebase

```yaml
- uses: DailybotHQ/ai-diff-reviewer@v2
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
- uses: DailybotHQ/ai-diff-reviewer@v2
  with:
    api-key: ${{ secrets.ANTHROPIC_API_KEY }}
    github-token: ${{ secrets.GITHUB_TOKEN }}
    trigger-mode: label-once
    label-gate: ai-review
```

Full guide: [docs/TRIGGER_MODES.md](docs/TRIGGER_MODES.md). Want unlabeled PRs to show the check as **Skipped** (grey) instead of a green **Success** — and the review to fire only when you apply the label, once per application? See [docs/TRIGGER_MODES.md § "Recipe: run once when labelled `ready`, and show Skipped (not green)"](docs/TRIGGER_MODES.md), the exact pattern this repo's own `self-review.yml` uses.

### Auto-fill missing PR descriptions

Let the reviewer write a first-draft body when the current one is empty or under 50 chars. Guarded by a marker so it never overwrites your edits.

```yaml
- uses: DailybotHQ/ai-diff-reviewer@v2
  with:
    api-key: ${{ secrets.ANTHROPIC_API_KEY }}
    github-token: ${{ secrets.GITHUB_TOKEN }}
    pr-description-mode: autocomplete
```

Full guide: [docs/PR_METADATA_CHECKS.md](docs/PR_METADATA_CHECKS.md).

### AI-driven complexity labels

Apply a `complexity:low/medium/high` label based on cognitive load, files touched, and security surface — not line count:

```yaml
- uses: DailybotHQ/ai-diff-reviewer@v2
  with:
    api-key: ${{ secrets.ANTHROPIC_API_KEY }}
    github-token: ${{ secrets.GITHUB_TOKEN }}
    complexity-labels-enabled: true
```

### Use a non-default automation account

If you want the review attributed to a specific bot account (e.g. so branch protection rules can require approval from "anyone except the bot"):

```yaml
- uses: DailybotHQ/ai-diff-reviewer@v2
  with:
    api-key: ${{ secrets.ANTHROPIC_API_KEY }}
    github-token: ${{ secrets.AUTOMATION_GITHUB_TOKEN }}   # PAT for your bot account
```

### Public open-source repo (safest defaults)

For a public repo, external contributors can open PRs — and each review costs real money (~50K–200K tokens per PR). The `author-association` input (default `OWNER,MEMBER,COLLABORATOR`, v1.3.0+) gates reviews on the PR author's relationship to the repo; the field comes from GitHub's payload and cannot be spoofed. Belt-and-suspenders combined with a label gate:

```yaml
- uses: DailybotHQ/ai-diff-reviewer@v2
  with:
    api-key: ${{ secrets.ANTHROPIC_API_KEY }}
    author-association: 'OWNER,MEMBER,COLLABORATOR,CONTRIBUTOR'  # optional: allow returning contributors
    label-gate: 'ai-review'                                       # maintainer opt-in per PR
    trigger-mode: label-once
```

Result: an outsider opening PRs cannot trigger the review at all (gate fails, zero API calls); returning contributors get a review only when a maintainer applies the `ai-review` label; the review runs exactly once per label application. See [docs/SECURITY.md § "Author-association gate"](docs/SECURITY.md) and [`examples/open-source-safe.yml`](examples/open-source-safe.yml).

More recipes: [`examples/`](examples/).

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

# Locally — as a coding-agent skill

The second surface: the **exact same review methodology** the CI Action runs, executed by your local coding agent (Cursor, Claude Code, Codex, Gemini, Copilot, Cline, Windsurf, Grok CLI, Pi, OpenCode) on the branch you're working on right now — no push required. Useful for pre-flight checks, iterating on prompt-extension rules, or getting a second opinion on a WIP branch without opening a draft PR.

The skill's [`prompt.md`](skills/ai-diff-reviewer/prompt.md) is a byte-identical copy of the Action's shipped [`prompts/default.md`](prompts/default.md), kept in sync by [`auto-release.yml`](.github/workflows/auto-release.yml) on every release cut. Pin the same version on both surfaces → **same methodology and severity model locally and in CI** (CI may additionally dedupe on round 2+ via Iteration-Aware Review; local reviews stay a full pass).

## Quick start (skill)

```bash
# Latest v2.x
npx skills add DailybotHQ/ai-diff-reviewer --skill ai-diff-reviewer

# Or pin to a specific action version for reproducibility:
npx skills add DailybotHQ/ai-diff-reviewer@v2 --skill ai-diff-reviewer
```

`npx skills` vendors the skill into `.agents/skills/ai-diff-reviewer/` in your repo and records source + content hash in `skills-lock.json` so teammates restore identical bytes with `npx skills experimental_install`. Bump with `npx skills update ai-diff-reviewer`.

Once installed, natural-language triggers activate each of the five capabilities — no memorized commands to look up:

```text
"Review my current branch"                         → local review
"Set up AI Diff Reviewer for this repo"            → install the CI Action
"Generate a .review/extension.md for this repo"    → tailor the reviewer to your stack
"Open the PR for this branch"                      → sync with main, then author the PR from the diff
"What did the CI review say?"                      → read the CI review on the PR and walk through the findings
```

Some harnesses (Claude Code, Cursor) also expose these as slash commands: `/ai-diff-reviewer`, `/ai-diff-reviewer-setup`, `/ai-diff-reviewer-generate-extension`, `/ai-diff-reviewer-open-pr`, `/ai-diff-reviewer-apply-review`.

## The five sub-skills

The skill is a **router** — it inspects your intent from natural language and routes to one of five capabilities, all sharing the same shipped prompt as the review base:

| Sub-skill | Purpose | Fires when you say… |
|---|---|---|
| **[Local review](skills/ai-diff-reviewer/SKILL.md)** *(default flow)* | Run the CI review methodology locally on your current branch's diff | *"Review my current branch"* · *"Do a pre-flight review before I push"* |
| **[`setup`](skills/ai-diff-reviewer/setup/SKILL.md)** | Install & configure the CI GitHub Action via a 6-question wizard. Also the reference manual for every `action.yml` input | *"Set up AI Diff Reviewer for this repo"* · *"What does `strictness` do?"* |
| **[`generate-extension`](skills/ai-diff-reviewer/generate-extension/SKILL.md)** | Bootstrap a repo-tailored `.review/extension.md` after inspecting your stack (≥ 12 Discovery tool calls) | *"Generate a `.review/extension.md` for this repo"* · *"Customize the review for our project"* |
| **[`open-pr`](skills/ai-diff-reviewer/open-pr/SKILL.md)** | Sync the branch with the remote base (merge, resolve conflicts, push), then author a well-documented pull request (title + body) from the diff — Conventional Commits inference, PR-template merge, `gh pr create` / `edit` | *"Open the PR"* · *"Draft the PR title and description"* · *"Rewrite the PR body properly"* |
| **[`apply-review`](skills/ai-diff-reviewer/apply-review/SKILL.md)** | Read the CI review posted on the branch's PR (latest marker, collapsed history filtered, per-leg attribution) and walk through each finding to apply / defer / skip with per-finding consent — never commits, never pushes | *"What did the CI review say?"* · *"Apply the AI review's fixes"* · *"Walk me through the findings"* |

Together they form a **lifecycle**: `setup` installs the Action once per repo → `generate-extension` tailors the review once per repo → the default review flow catches issues before pushing on every branch → `open-pr` authors the PR that ships the change.

## First-run bootstrap prompt

You don't have to think about `generate-extension` on day one. The first time you run the review on a repo with no `.review/extension.md`, the skill offers a **yes / no / never** prompt inline:

- **yes** → routes to `generate-extension`, then re-runs the review with the fresh extension layered on.
- **no** → runs the review this once with the base prompt only. Offer fires again next time.
- **never** → creates `.review/.skip-bootstrap` (a 0-byte tracked marker) so the offer never fires again in this repo. Commit it and your whole team inherits the same UX. Delete the file to re-enable the offer.

The base prompt alone (bundled with the action at [`prompts/default.md`](prompts/default.md)) catches ~90% of general-purpose issues — SQL injection, unhandled promises, missing input validation, obvious perf regressions. The extension is what turns "generic senior reviewer" into "senior reviewer who knows YOUR repo."

## Sub-skill: run a local review (default flow)

The skill uses your local agent's own tools (Read / Grep / Glob) to gather context, then produces the review in **the same output format** the CI bot would post — verdict, findings table, per-finding body, notes, recommendation:

```markdown
## Verdict
<one sentence — "Looks good", "Blocking security fix needed", etc.>

## Findings
| # | Severity     | File               | Summary                                   |
|---|--------------|--------------------|-------------------------------------------|
| 1 | 🚨 critical  | `src/auth.ts:55`   | SQL injection in raw-string login query   |
| 2 | ⚠️ warning   | `src/cache.ts:120` | Unbounded cache key cardinality           |
| 3 | ℹ️ info      | `tests/utils.ts:12`| Helper could be reused from existing fixture |

### 1. `src/auth.ts:55` — 🚨 critical
<full finding body + optional ```suggestion block```>

### 2. `src/cache.ts:120` — ⚠️ warning
<...>

## Notes (no inline anchor)
- <cross-cutting concerns, architecture, test-strategy comments>

**Recommendation:** approve / request-changes / comment-only
```

Reproducing this exact shape (verdict → findings table → per-finding body → notes → recommendation) is what lets you trust the same methodology and severity model on both surfaces. On CI round 2+ of a generation, Iteration-Aware Review may additionally dedupe findings a local full pass would still list — expected, not a parity bug. Full flow: [`skills/ai-diff-reviewer/SKILL.md`](skills/ai-diff-reviewer/SKILL.md).

## Sub-skill: `setup` — install the Action via wizard

Walks you through installing the CI action on a repo that doesn't have it yet — six questions (provider, strictness, trigger mode, external-contributor policy, PR-description mode, complexity labels), sensible per-stack defaults, and a tailored `.github/workflows/pr-review.yml` written for you. Say:

- *"Set up AI Diff Reviewer for this repo"*
- *"Configure the reviewer action"*
- *"Install the AI Diff Reviewer GitHub Action"*

At the end, it also offers to bootstrap the extension file (Step 5 of the wizard hands off to `generate-extension`), so the same conversation takes you from **zero setup → installed → tailored** in one go.

The sub-skill also serves as the **reference manual** for every `action.yml` input via [`skills/ai-diff-reviewer/setup/reference.md`](skills/ai-diff-reviewer/setup/reference.md). Ask any coding agent with the skill installed *"what does `strictness` do?"* or *"how do I pin the Cursor CLI version?"* and it can answer without opening the action source.

Full wizard flow: [`skills/ai-diff-reviewer/setup/SKILL.md`](skills/ai-diff-reviewer/setup/SKILL.md).

## Sub-skill: `generate-extension` — tailor the reviewer to your repo

Instead of copy-pasting the [meta-prompt](examples/prompts/generate-custom-prompt-meta.md) into a chat window, just say:

- *"Generate a `.review/extension.md` for this repo"*
- *"Customize the code review for our project"*
- *"Set up the AI reviewer for this codebase"*

The sub-skill inspects your stack, architecture, security surface, and existing conventions (≥ 12 tool calls minimum — real Discovery, not a guess), then writes the file directly.

Two output modes:

- **`extension` (default, recommended)** — layers repo-specific overrides ON TOP of the battle-tested default prompt. Cheap iteration; the default keeps improving upstream.
- **`full replacement` (advanced)** — for teams that want total control, or whose codebase is so idiosyncratic (proprietary DSL, unusual paradigm) that the default is more noise than signal. Requires ongoing maintenance; you lose upstream improvements to the default.

Full authoring guide: [`skills/ai-diff-reviewer/generate-extension/SKILL.md`](skills/ai-diff-reviewer/generate-extension/SKILL.md).

## Sub-skill: `open-pr` — author the PR from the same diff

Turns the current branch's diff into a well-documented pull request — natural next step after the local review says "looks good." Say:

- *"Open the PR"* / *"Create a pull request for this branch"*
- *"Draft the PR title and description"*
- *"Update the PR description"* / *"Rewrite the PR body in the proper format"* (edit mode)
- *"Make a draft PR"* (adds `--draft`)

It reads the diff, empirically detects your repo's title convention from the last 20 merged PRs (Conventional Commits vs plain sentence), infers `<type>` and `<scope>` from the touched files, and drafts a structured body with **three mandatory sections** (Summary, Test plan, Risks) plus **conditional sections** that only appear when the diff signals them:

- **Related issues** — when commits reference `Closes #N` / `Fixes #N`
- **Screenshots** — when UI files (`.tsx`, `.vue`, `.css`, …) are touched (opt-in prompt)
- **Breaking changes** — when a commit uses `feat!:` / `BREAKING CHANGE:` or removes public API surface
- **Migrations** — when `migrations/`, `alembic/versions/`, `prisma/migrations/`, etc. are touched
- **Dependencies** — when `package.json`, `poetry.lock`, `go.sum`, etc. are touched

Before drafting anything it **brings the branch up to date with the remote base** (v2.1.0+): fetch, merge `origin/<base>` into the current branch when it is behind, resolve conflicts by reading both sides (never dropping the base's change), run the repo's quick validation, commit, and push the current branch — non-force, current branch only, announced in one line, no extra prompt. Dirty tree, an unjustifiable conflict, a failing quick gate, or a rejected push **stop** the skill with the exact commands instead. Repos that mandate a linear history get one question before any rebase + `--force-with-lease`.

Your existing `.github/pull_request_template.md` is **merged, never overwritten** — repo-specific `## Checklist` / `## Rollout plan` sections are preserved intact; only the diff-derived sections override the template's placeholders. Preview (with the sync outcome on its own line) → single `yes` / `edit` / `cancel` → `gh pr create` (new PR) or `gh pr edit` (refresh existing — with a body diff shown). A `## Merge notes` section lists resolved conflicts when there were any. Never force-pushes, never auto-merges, never fabricates issue refs.

Full skill: [`skills/ai-diff-reviewer/open-pr/SKILL.md`](skills/ai-diff-reviewer/open-pr/SKILL.md).

---

## Bringing them together — `.review/extension.md`

The two surfaces converge on a single **repo-specific extension file** — a plain-Markdown override layer that sits on top of the shipped default prompt. Both the CI Action and the local skill read it from the same path, so your team's custom review rules (severity overrides, "don't comment on" scopes, house conventions) apply identically in both places.

**Put your custom overrides in `.review/extension.md`** at your repo root — the local skill auto-detects it, and your CI workflow references the same file via the `prompt-extension-file:` input:

```yaml
- uses: DailybotHQ/ai-diff-reviewer@v2
  with:
    api-key: ${{ secrets.ANTHROPIC_API_KEY }}
    github-token: ${{ secrets.GITHUB_TOKEN }}
    prompt-extension-file: .review/extension.md   # same file the skill auto-detects
```

**One file, two surfaces, zero drift.**

Example `.review/extension.md`:

```markdown
## Severity overrides for our codebase

- Any `SELECT * FROM users` in a request path is **critical** (PII exposure).
- Missing `AbortController` on a `fetch()` in `apps/frontend/` is **warning**
  (React 18 pattern we standardized on in RFC-014).

## Don't comment on

- Formatting in `apps/legacy/*` — that module is scheduled for a rewrite.
- Missing tests in `experiments/` — that folder is intentionally exploratory.
```

The [`generate-extension` sub-skill](#sub-skill-generate-extension--tailor-the-reviewer-to-your-repo) can bootstrap this file for you — it reads your stack, architecture, security surface, and existing conventions, then writes concrete, code-anchored overrides. If you'd rather write it by hand, the full authoring guide (structure, tips, worked examples) is in [docs/PROMPTS.md § "Sharing repo-specific rules between CI and local"](docs/PROMPTS.md).

**Fallback path** for teams that prefer keeping the file next to `.github/workflows/`: the skill also recognizes `.github/ai-diff-reviewer/extension.md` (and the legacy `.github/ai-pr-reviewer/extension.md` for pre-v1.5 repos). The recommended convention is `.review/extension.md` at the repo root — runtime-agnostic and future-proof.

---

## How it works

The Action's runtime (the local skill mirrors these steps in your coding agent):

1. **Access & trigger gates** (cheapest first) — `author-association` runs first (skip if the PR author isn't in the whitelist and lacks write-tier collaborator permission), then the `label-gate` / `trigger-mode` check (skip if the required label is missing or this label application was already reviewed).
2. **Emergency-bypass check** — if `skip-review-label` is configured AND that label is on the PR, short-circuit to success (no LLM call, no state mutation, `⏭️ skipped` tracking comment posted). See [docs/TRIGGER_MODES.md § Emergency-bypass label](docs/TRIGGER_MODES.md).
3. **Collapse previous** — marks prior bot reviews/comments as `OUTDATED` via GraphQL.
4. **Tracking comment** — posts a `Working…` comment with a stable marker.
5. **Fetch PR** — pulls metadata, file list, and `git diff origin/<base>...HEAD`.
6. **IAR pre-LLM** — reads prior iteration state from the marker, detects generation transitions (including `USER_FORCED_RESET`), shapes the effective cap + exhaustive prompt addendum, and checks the escape label / safety net. See [docs/ITERATION_AWARENESS.md](docs/ITERATION_AWARENESS.md).
7. **Agentic loop** — runs the model with five tools: `read_file`, `grep`, `glob`, `post_inline_comment`, `submit_review`. Inline comments are queued in memory and posted atomically with the final review. Conversation history is pruned in pairs to bound token cost.
8. **IAR post-LLM** — dedupes findings against prior fingerprints (criticals always surface), advances generation/round state, embeds the state block in the tracking marker, and writes the five IAR action outputs.
9. **Submit** — `POST /pulls/{n}/reviews` with the summary and queued inline comments. On HTTP 422 (one bad anchor line in any comment ⇒ entire request rejected), the action first retries with only the comments whose anchor is provably inside a diff hunk (v2.3.1), then summary-only as a last resort, and reports the dropped count in the tracking comment.
10. **Apply label** — applies `applied-label` if set and the strictness gate didn't block.
11. **Strictness gate** — exits 2 if blocked, 0 otherwise.

The local companion skill diverges at two boundaries: (1) it prints findings instead of posting to GitHub, and (2) it does **not** run the IAR dedup pipeline — a local review is always a full pass against the current diff (it may surface findings CI has already deduped on round 2+ of the same generation). Same base prompt, same severity model, same output shape. IAR steps 6 and 8 apply only on Action runs; see [docs/ITERATION_AWARENESS.md](docs/ITERATION_AWARENESS.md).

For the full design, see [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md), [docs/PROVIDERS.md](docs/PROVIDERS.md), [docs/PROMPTS.md](docs/PROMPTS.md), [docs/STRICTNESS.md](docs/STRICTNESS.md).

---

## Provider roadmap

| Provider | Family | Status | Notes |
|---|---|---|---|
| Anthropic (Claude) | chat-completions | ✅ shipping (v1.0.0+) | `claude-sonnet-4-6` default. Zero CLI install. |
| Claude Code CLI | agent-runner | ✅ shipping (v1.2.1+) | `@anthropic-ai/claude-code` npm CLI in headless mode. Uses `ANTHROPIC_API_KEY`. Works with subscription auth. |
| Cursor Agent CLI | agent-runner | ✅ shipping (v1.2.1+) | `cursor-agent` local CLI in headless mode. Uses `CURSOR_API_KEY`. Default model `auto` — unlimited on Cursor Pro. |
| OpenAI Codex CLI | agent-runner | ✅ shipping (v1.2.1+) | `@openai/codex` npm CLI in headless mode. Uses `OPENAI_API_KEY`. |
| xAI Grok CLI | agent-runner | ✅ shipping (v2.1.0+) | Official `grok` CLI in headless mode. Uses `XAI_API_KEY`. Web search/subagents off by default; native `--max-turns`. |
| OpenAI-compatible (raw API) | chat-completions | ✅ shipping (v2.1.0+) | `provider: openai` — direct chat-completions, no CLI install. Covers OpenAI, Azure Foundry, xAI, Z.ai and — since v2.4.0 — DeepSeek, Moonshot/Kimi, MiniMax, Qwen, Gemini and OpenRouter through `api-base`. |
| Azure Foundry / Azure OpenAI | via `api-base` | ✅ shipping (v2.1.0+) | Through `openai` (in-process) or `codex` (CLI, generated per-run `config.toml`). `model` is the deployment name. Both verified live. |
| Z.ai GLM | via `api-base` | ✅ shipping (v2.1.0+) | Through `claude-code` (recommended, Anthropic-compatible endpoint), `anthropic`, `openai` or `codex`. Flat-rate Coding Plan. Verified offline; first live run pending. |
| xAI (in-process) | via `api-base` | ✅ shipping (v2.1.0+) | `anthropic` or `openai` runner against xAI's Anthropic-/OpenAI-compatible endpoints, same `XAI_API_KEY` as the Grok CLI. |
| Google Gemini | via `api-base` | ✅ shipping (v2.4.0+) | Through its OpenAI-compatible endpoint (`v1beta/openai`) with `provider: openai`; `seed` is omitted (the endpoint rejects it). |
| OpenRouter | via `api-base` | ✅ shipping (v2.4.0+) | Meta-gateway: `openai` + `https://openrouter.ai/api/v1`; vendor-prefixed model ids (`vendor/model`). |
| AWS Bedrock | Anthropic-shape | ✅ shipping (v2.5.0+) | `provider: anthropic` + the regional `bedrock-runtime` endpoint; SigV4 signed in-process (stdlib), credentials via env (OIDC) or the packed `api-key` format. |

**Two provider families:**

- **`Provider`** (chat-completions family) — the action owns the tool-use loop, calling the model's API in a bounded turn count. No install step needed. Zero overhead for consumers.
- **`AgentRunnerProvider`** (agent-runner family) — a vendor's coding-agent CLI owns the tool-use loop; we shell out in headless mode and receive findings via `.aiprr/findings.json`. Better code comprehension (vendor-tuned tools, LSP, semantic search) at the cost of a CLI install step (only when the consumer opts in — modular install; see [docs/PROVIDERS.md](docs/PROVIDERS.md)).

Adding a new **vendor** is often just a host suffix and a tier row (see [docs/PROVIDERS.md § Adding a runner or backend](docs/PROVIDERS.md)); adding a new **runner** means implementing one class and registering it in `build_provider()`. Contributions welcome — see [CONTRIBUTING.md](CONTRIBUTING.md).

---

## Documentation

The deep-dive docs live under [`docs/`](docs/) and are cross-linked from every relevant section above. Quick index for browsing:

| Topic | Doc |
|---|---|
| Product spec (what & why) | [docs/PRODUCT_SPEC.md](docs/PRODUCT_SPEC.md) |
| Architecture (single-file runtime + provider abstraction) | [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) |
| Providers (chat-completions vs agent-runner, model choice) | [docs/PROVIDERS.md](docs/PROVIDERS.md) |
| Prompts (base / extension / full replacement, worked examples) | [docs/PROMPTS.md](docs/PROMPTS.md) |
| Strictness (`lenient` / `block-on-*` semantics) | [docs/STRICTNESS.md](docs/STRICTNESS.md) |
| Trigger modes, branch-protection recipes & emergency-bypass label | [docs/TRIGGER_MODES.md](docs/TRIGGER_MODES.md) |
| Iteration-Aware Review (dedup, policies, escape label, outputs) | [docs/ITERATION_AWARENESS.md](docs/ITERATION_AWARENESS.md) |
| v2 pin + platform notes | [docs/MIGRATION_v2.md](docs/MIGRATION_v2.md) |
| PR-metadata checks (autocomplete, warn, block) | [docs/PR_METADATA_CHECKS.md](docs/PR_METADATA_CHECKS.md) |
| Security model (author-association, egress surfaces, provider trust) | [docs/SECURITY.md](docs/SECURITY.md) |
| Performance (turn budgets, prompt caching, token cost) | [docs/PERFORMANCE.md](docs/PERFORMANCE.md) |
| Testing guide | [docs/TESTING_GUIDE.md](docs/TESTING_GUIDE.md) |
| PR-review workflow (reading past comments, marker-anchored) | [docs/PR_REVIEW_WORKFLOW.md](docs/PR_REVIEW_WORKFLOW.md) |
| Release recovery playbook | [docs/RELEASE_RECOVERY.md](docs/RELEASE_RECOVERY.md) |
| Full docs index | [docs/README.md](docs/README.md) |

**For AI agents working on this repo:** the canonical entry point is [`AGENTS.md`](AGENTS.md) — the single source of truth for every coding assistant (Claude Code, Cursor, Codex, Gemini, Copilot, OpenClaw). Rule numbers referenced from `.review/extension.md` anchor there.

---

## FAQ

**Why a custom action and not just `anthropics/claude-code-action`?**
That action's `restoreConfigFromBase` step crashes with `ENOENT` on repos that ship `.claude` as a symlink (a common pattern when consolidating multi-agent configs into a shared `.agents/` folder). This action removes that dependency and adds severity-based gating, configurable label gates, and a 422-recovery path the upstream action lacks.

**Why composite, not Docker?**
The reviewer is stdlib-only Python. A Docker action would add ~30s of pull time and a second supply chain (the image registry) for zero benefit.

**Do I have to install both the Action and the skill?**
No — they're independent. Many teams start with just the Action (auto-review on every PR is the highest-leverage install) and add the skill later for pre-push checks. Others start with just the skill (their coding agent knows how to review). If you install both, they naturally reinforce each other via `.review/extension.md`.

**Will the model leak my code to the provider?**
The action sends the PR diff and any files the model `read_file`s to the configured provider. Treat it like any other LLM integration — review your provider's data-retention policy and use a self-hosted runner if you have specific constraints. The skill runs your local agent, so it inherits your agent's data-handling posture (Cursor Pro, Claude Pro/API, etc.).

**Does it work on private repos?**
Yes. The default `secrets.GITHUB_TOKEN` has the right scope; just make sure the repo's settings allow Actions to read content and write PR comments. The default `author-association` allow-list works on private org repos — org members and admins are not skipped when GitHub's webhook under-reports `CONTRIBUTOR`. Set `author-association: ''` only if you want the gate fully disabled.

**Can I dogfood the reviewer on its own PRs?**
Yes — see [`.github/workflows/self-review.yml`](.github/workflows/self-review.yml) in this repo for the pattern. This repo also vendors its own skill copy at [`.agents/skills/ai-diff-reviewer/`](.agents/skills/ai-diff-reviewer/) refreshed automatically on every release — dogfooding the install flow every time we publish.

**How do I review pull requests with AWS Bedrock?**
Point `provider: anthropic` at the regional endpoint — `api-base: https://bedrock-runtime.us-east-1.amazonaws.com` — and pass a Bedrock inference-profile id (`us.anthropic.claude-sonnet-5`; `model: balanced` resolves it — bare foundation ids are not accepted for on-demand InvokeModel). Credentials: run the [`aws-actions/configure-aws-credentials`](https://github.com/aws-actions/configure-aws-credentials) step and let the action read the exported environment (OIDC — no long-lived keys, `api-key` can stay empty), or store `KEY:SECRET[:SESSION]` as the `api-key`. Requests are SigV4-signed in-process; nothing is sent anywhere but your region's `bedrock-runtime` endpoint. Details: [docs/PROVIDERS.md](docs/PROVIDERS.md).

**Which runner should I use for Z.ai GLM, Azure Foundry, xAI — or the newer DeepSeek / Kimi / MiniMax / Qwen / Gemini / OpenRouter backends?**
GLM: `claude-code` + `api-base: https://api.z.ai/api/anthropic` (Z.ai's first-class integration; flat-rate Coding Plan), or `anthropic` with the same base for the bounded zero-install loop. Azure Foundry: `openai` (bounded) or `codex` (agentic), `model` = your deployment name. xAI: `grok` (the agent, web search and subagents off, native turn cap) or `openai` + `api-base: https://api.x.ai/v1` (bounded). Codex on xAI is not usable today (Codex ≥ 0.154 sends a tool type xAI rejects). DeepSeek: `openai` + `api-base: https://api.deepseek.com`. Kimi: `openai` + `https://api.moonshot.ai/v1`, or `claude-code` + `https://api.moonshot.ai/anthropic`. MiniMax: same two shapes (`api.minimax.io/v1` / `api.minimax.io/anthropic`). Qwen: `openai` + `https://dashscope.aliyuncs.com/compatible-mode/v1`. Gemini: `openai` + `https://generativelanguage.googleapis.com/v1beta/openai`. OpenRouter: `openai` + `https://openrouter.ai/api/v1` with vendor-prefixed ids. Details: [docs/PROVIDERS.md](docs/PROVIDERS.md).

**Does the usage / cost telemetry add cost to a review?**
No. It reads the `usage` block every API response already carries, or parses the CLI's own JSON output (Claude Code `stream-json`, Codex `--json`, Grok `--output-format json`). No extra request, no extra prompt tokens; the cost estimate is local arithmetic against a dated price table, replaced by the vendor's real figure when the CLI reports one.

**Why doesn't the reviewer resolve my review threads when it says a finding is fixed?**
Because a changed file and the model's word are not proof that a bug disappeared. By default (`prior-findings-resolution: advisory`) the model's `resolved` verdicts appear in the summary, the thread stays open until a maintainer resolves it, and an outstanding prior finding keeps counting toward the strictness gate. One exception (v2.3.1): when `collapse-previous` has already minimized that thread — so nobody can find it to resolve — a verdict the runtime can corroborate (finding not re-emitted **and** the file changed since the finding was raised, or was deleted) retires the finding without touching the thread, and the footer says `N auto-retired`. Teams that want fixed threads to actually close can opt into `prior-findings-resolution: verified`: the runtime then replies on and resolves a thread it can corroborate; anything it cannot corroborate stays open under both policies. Either way, read the check result from the `**Check status**` block or the tracking comment, never from the recommendation line. See [docs/ITERATION_AWARENESS.md § 14.4](docs/ITERATION_AWARENESS.md).

**How do version pins between the Action and the skill line up?**
They're intended to be identical. `uses: DailybotHQ/ai-diff-reviewer@v2.0.0` in CI + `npx skills add DailybotHQ/ai-diff-reviewer@v2.0.0 --skill ai-diff-reviewer` locally = byte-identical prompt on both surfaces. Pinning to the moving `@v2` alias on both sides also works — new patches and minor features flow to both simultaneously.

---

## Contributing

Bug reports, feature requests, provider implementations, sub-skills, and prompt improvements are all welcome. See [CONTRIBUTING.md](CONTRIBUTING.md) and [AGENTS.md](AGENTS.md) (the single source of truth for repo standards, canonical file paths, and the mandatory-rules checklist).

## License

[MIT](LICENSE) © 2026 AI Diff Reviewer contributors.

---

## :electric_plug: Powered by [Dailybot](https://www.dailybot.com?utm_source=dailybotopensource&utm_medium=ai-pr-reviewer)

[Dailybot](https://www.dailybot.com/product/ai) is an AI-powered async communication platform that keeps **people and agents** visible — without adding more meetings or tools. It lives where your team already works (Slack, Teams, Google Chat, Discord, VS Code, and the CLI) and turns scattered signals into clear progress: async check-ins and standups, AI summaries that detect blockers and read team sentiment, workflow automation and approvals, team analytics, and recognition. As AI agents join the workflow, Dailybot surfaces their status and activity right alongside your team's — so long-running agents never go dark. [Learn more](https://www.dailybot.com?utm_source=dailybotopensource&utm_medium=ai-pr-reviewer).
