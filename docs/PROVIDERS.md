# Providers — current status and how to add a new one

## Providers are vendors; runners are how they run

From a consumer's point of view the action supports **six providers**: Anthropic, OpenAI, Azure Foundry, xAI (Grok), Z.ai GLM and Cursor — plus any Anthropic- or OpenAI-compatible gateway. Each is reached through one or more **runners**: the action's own in-process loop (`anthropic`, `openai`) or a vendor coding-agent CLI (`claude-code`, `codex`, `grok`, `cursor`). The `README.md` Providers table is organised by vendor; this document explains the mechanism underneath.

## Runner × backend matrix (v2.1.0+)

Two inputs decide a review: **`provider`** picks the *runner* (who drives the tool-use loop) and the optional **`api-base`** picks the *backend* — the vendor whose model answers. Empty `api-base` keeps every runner on its own vendor. Since v2.4.0 the default profiles pin deterministic sampling — `temperature: 0` and, where the backend accepts it, a fixed `seed` — so the wire *shape* (URL, headers, auth, caching) is unchanged from earlier releases but the sampling parameters are new (see the [CHANGELOG](../CHANGELOG.md) "Changed" notes).

v2.4.0 adds six further `openai` backends behind the same `api-base` input — **DeepSeek, Moonshot/Kimi, MiniMax, Qwen (DashScope), Google Gemini and OpenRouter** — each with its own dated tier row (matrix below). They are reachable through `provider: openai` only: the Codex CLI speaks the Responses API to every non-default gateway (`codex_wire_api` is pinned to `"responses"`), which none of these chat-completions vendors implements — the same limitation class as xAI. Moonshot/Kimi and MiniMax additionally speak the Anthropic-compatible protocol (rows in the Anthropic-compatible table further down). The per-runner matrix for the five established vendors:

| Runner (`provider`) | Anthropic | OpenAI | Azure Foundry | xAI | Z.ai GLM | Subscription / flat-rate |
|---|---|---|---|---|---|---|
| `anthropic` (in-process, default) | ✅ default (`claude-sonnet-4-6`) | — | — | ✅ `api-base: https://api.x.ai` | ✅ `api-base: https://api.z.ai/api/anthropic` | — |
| `openai` (in-process) | — | ✅ default (`gpt-5.6-luna`) | ✅ `api-base: https://<resource>.services.ai.azure.com/openai/v1`, `model` = deployment | ✅ `api-base: https://api.x.ai/v1` | ✅ `api-base: https://api.z.ai/api/coding/paas/v4` | Z.ai Coding Plan |
| `claude-code` (CLI) | ✅ default (`claude-sonnet-4-6`) | — | — | ✅ (Anthropic-compatible `api-base`) | ✅ **recommended for GLM** (`api-base: https://api.z.ai/api/anthropic`) | Claude Pro/Max token (`sk-ant-oat…`), Z.ai Coding Plan |
| `codex` (CLI) | — | ✅ default (`gpt-5.6-luna`) | ✅ (`config.toml` generated per run; `model` = deployment) | ⚠️ not usable with Codex ≥ 0.154 (rejects its `custom` tool) — use `grok` / `openai` | ✅ (Responses API, `api-base: https://api.z.ai/api/v1`) | — |
| `grok` (CLI) | — | — | — | ✅ default (`grok-4.5`) | — | — |
| `cursor` (CLI) | — | — | — | — | — | ✅ Cursor Pro (`model: auto`); no `api-base` lane |

`model` is **required** whenever `api-base` is set for a runner that routes it (v2.2.0+): a runner's built-in default names its own vendor's model, so the run aborts with the expected value (deployment name, `glm-5.3`, `grok-4.6`, the gateway's id) instead of sending the wrong model. `cursor` and `grok` ignore `api-base` (warned) and keep their defaults. Any other `https://` host is a **custom** backend (plain Anthropic- or OpenAI-shaped protocol for the runner's family; the run logs a WARNING naming the host that receives the key). Details per family below; cost per cell in the next section.

**Choosing in one minute**

- **Cheapest, predictable, zero install:** `anthropic` (or `openai` for OpenAI-compatible models) — bounded loop, prompt caching, real usage line.
- **Deepest review:** an agent-runner (`claude-code` first; `codex` / `grok` when that vendor is already paid for). Trusted (non-fork) PRs only.
- **Z.ai GLM:** run it through **`claude-code`** — the CLI's Anthropic-compatible backend contract (`ANTHROPIC_BASE_URL` + `ANTHROPIC_AUTH_TOKEN`) is what Z.ai documents and what works best locally; `anthropic` (in-process) is the bounded alternative. Verified offline only in this release (weekly quota exhausted during verification).
- **xAI Grok:** `provider: grok` (agent, native `--max-turns`, web search and subagents off) or `provider: openai` + `api-base: https://api.x.ai/v1` (bounded). Both verified live.
- **Azure Foundry:** `codex` (agent) or `openai` (bounded), `model` = deployment name. Both verified live.
- **AWS enterprise agreements:** AWS Bedrock through `anthropic` + the regional `bedrock-runtime` endpoint (SigV4 in-process; OIDC credentials supported) — the Claude review loop on infrastructure your cloud team already controls.
- **Cheapest third-party text models:** DeepSeek or Moonshot/Kimi through `openai` + their `api-base` (tier rows in the matrix below). **Widest catalog:** OpenRouter — vendor-prefixed ids (`vendor/model`) behind one key. **Gemini:** its OpenAI-compatible endpoint through `openai`.

## Choosing a cost-efficient model

Two things drive review cost: **how often it runs** and **which model it uses**.

- **Frequency** is the biggest lever. Running several providers on every push is N× the reviews. Pick one provider for routine use, or gate the expensive legs (this repo's `self-review.yml` runs a cheap baseline on every `ready` PR and reserves deeper passes for high-risk changes).
- **Model** matters most for the agent-runner CLIs (`claude-code`, `codex`, `grok`), which are autonomous agents that explore the repo and spend far more tokens than the bounded chat-completions path.

### Quality is not optional for review

Code review's value is catching **subtle** bugs — logic errors, race conditions, security issues. That is exactly where model capability pays off, so the cheapest model is not always the best *value*: a cheap review that misses real issues can be worse than none (false confidence). The **balanced** tier below is the quality/cost sweet spot for real reviews; **economy** is for smoke/dogfood passes and docs-only PRs; **deep** for high-risk PRs.

### Pick a cost profile in one word — `model: balanced | economy | deep`

Cursor `auto` proved the pattern: a one-word cost profile is what teams actually use day to day. The `model` input now accepts the same idea for every runner × backend — the runtime resolves the tier from the matrix below and logs the concrete id. Empty `model` keeps resolving to the built-in default (unchanged for existing consumers); an explicit id always passes through.

### Cost-efficient defaults matrix (verified 2026-09-21 — ids and prices move, re-check when bumping)

Indicative list prices in USD per 1M tokens (input / output). Cached input is cheaper on every vendor (Anthropic cache reads are 10 % of input price; OpenAI/xAI cache automatically; Z.ai cache currently free). The runtime's copy of this table is dated by the constant `MODEL_TIERS_VERIFIED_ON` in `scripts/reviewer.py` (currently `2026-09-21`) — the `**Usage:**` line marks estimates as `(indicative)` for that reason; xAI prices are the <200k-token rates and double above that, so long reviews under-report.

| Runner | Backend | `balanced` (default recommendation) | `economy` (smoke) | `deep` (high-risk PRs) | Rationale |
|---|---|---|---|---|---|
| `anthropic`, `claude-code` | Anthropic | `claude-sonnet-5` — $2 / $10 | `claude-haiku-4-5` — $1 / $5 | `claude-opus-5` — $5 / $25 | Sonnet 5 is current **and** cheaper than the legacy `claude-sonnet-4-6` ($3 / $15) that the built-in default still names for back-compat — the run logs a hint; `model: balanced` opts in. Never `auto` on Claude Code (can silently be Opus). |
| `anthropic`, `claude-code` | Z.ai (Coding Plan) | `glm-5.3` — $1.40 / $4.40 | `glm-5.3-flash` — $0.15 / $0.50 | `glm-5.3` | Flat-rate Coding Plan ⇒ marginal cost ≈ 0 either way; `claude-code` is the recommended GLM runner. |
| `anthropic`, `claude-code` | xAI (Anthropic-compatible) | `grok-4.5` — $2 / $6 (cached input $0.30) | `grok-4.5` (same) | `grok-4.6` — $2 / $6 (cached $0.50) | Benchmark 2026-09-16 ([`tests/eval/BENCHMARK-xai-2026-09-16.md`](../tests/eval/BENCHMARK-xai-2026-09-16.md)): 4.5 and 4.6 tie at 3 of 5 known defects with no false positives; 4.5 costs the same and takes a quarter of the time; 4.3 found 0 of 5 (it approves without reviewing) and grok-build-0.1 1 of 5 with two runs that never submitted — neither is offered as a tier. (Prices are the <200k-token rates; above that they double.) |
| `openai`, `codex` | OpenAI | `gpt-5.6-luna` — $0.20 / $1.20 | `gpt-5.6-luna` | `gpt-5.6-terra` — $2 / $12 | Luna is both the balanced **and** the economy pick: `gpt-5.4-mini` ($0.75 / $4.50) is no longer cheaper. Codex-tier `gpt-5.3-codex` is $1.75 / $14. |
| `openai`, `codex` | xAI | `grok-4.5` | `grok-4.5` | `grok-4.6` | Same xAI reasoning; note Codex 0.154 cannot talk to xAI (see the Codex section) — use `openai` or `grok`. |
| `openai`, `codex` | Z.ai | `glm-5.3` | `glm-5.3-flash` | `glm-5.3` | Flat-rate plan. |
| `openai` | DeepSeek | `deepseek-chat` — $0.27 / $1.10 | `deepseek-chat` | `deepseek-reasoner` — $0.55 / $2.19 | Independent-family coder; reasoner is the deep pick (~2× cost, reasoning-first). Note: the vendor documents that `deepseek-reasoner` **ignores** sampling parameters — the `temperature`/`seed` pin is a no-op there, not a behaviour change. |
| `openai` | Moonshot/Kimi | `kimi-k2-0905-preview` — $0.60 / $2.50 | `kimi-k2-turbo-preview` — $1.15 / $1.15 | `kimi-k2-0905-preview` | Input-heavy reviews favour 0905 on cost; turbo wins on latency. Also speaks Anthropic-compatible (see below). |
| `openai` | MiniMax | `MiniMax-M2` — $0.30 / $1.20 | `MiniMax-Text-01` — $0.20 / $1.20 | `MiniMax-M2` | M2 is the agentic-coding flagship, documented for Claude Code via its Anthropic-compatible endpoint (see below). |
| `openai` | Qwen (DashScope) | `qwen3-coder-plus` — $0.40 / $1.60 | `qwen-turbo` — $0.05 / $0.40 | `qwen3-coder-plus` | Compatible-mode endpoint; family-independent coder. |
| `openai` | Google Gemini | `gemini-2.5-pro` — $1.25 / $10 | `gemini-2.5-flash` — $0.30 / $2.50 | `gemini-2.5-pro` | Gemini API's OpenAI-compatible surface; the runtime omits `seed` (the endpoint rejects it) and pins `temperature: 0`. |
| `anthropic` | AWS Bedrock (v2.5.0+) | `anthropic.claude-sonnet-5` — indicative mirror | `anthropic.claude-haiku-4-5` | `anthropic.claude-opus-5` | SigV4-signed InvokeModel; prices are an indicative mirror of first-party rates — **AWS bills Bedrock separately**. |
| `openai` | OpenRouter (meta-gateway) | `deepseek/deepseek-chat` — $0.30 / $1.20 (OpenRouter's listed price) | `deepseek/deepseek-chat` | `deepseek/deepseek-reasoner` — $0.60 / $2.40 (OpenRouter's listed price) | Model ids are vendor-prefixed (`vendor/model`); prices shown are OpenRouter's listed figures (the underlying vendor plus its margin, matching the runtime's `INDICATIVE_PRICES_USD_PER_MTOK`). One key, hundreds of models. |
| `grok` | xAI | `grok-4.5` | `grok-4.5` | `grok-4.6` | The Grok CLI's own system prompt + tools weigh ≈ 12k input tokens per call — the telemetry line makes that visible. Budget ~3 min and ~$0.75 per mid-size PR on 4.5 through the CLI (4–10 min on 4.6; one in-process 4.6 run took 22 min); the 900 s CLI timeout is the ceiling. |
| `cursor` | Cursor subscription | `auto` | `auto` | `composer-2.5` | `auto` is flat-rate on Pro and routes well; `composer-2.5` burns metered credits — reserve for deep passes. |
| any | Azure Foundry / custom gateway | *(no tier rows)* | | | Deployment names are consumer-defined; a tier word fails fast with guidance — set `model` to the deployment name or gateway model id. |

Built-in defaults when `model` is empty: `claude-sonnet-4-6` (anthropic, claude-code), `gpt-5.6-luna` (openai, codex), `grok-4.5` (grok — v2.3.0+, was `grok-4.3`), `auto` (cursor).

### Route tiers by risk (recipe)

Run `economy` on every push and `deep` only when the PR is risky — the complexity label the reviewer itself applies (`complexity-labels-enabled`) or a human `deep-review` label are the natural triggers. Two jobs with distinct `applied-label`s; per-provider collapse keeps their reviews apart:

```yaml
jobs:
  review-economy:
    if: "!contains(github.event.pull_request.labels.*.name, 'deep-review')"
    steps:
      - uses: actions/checkout@v4
        with: { fetch-depth: 0 }
      - uses: DailybotHQ/ai-diff-reviewer@v2
        with:
          provider: anthropic
          model: economy
          api-key: ${{ secrets.ANTHROPIC_API_KEY }}
          github-token: ${{ secrets.GITHUB_TOKEN }}
          applied-label: reviewed:economy
  review-deep:
    if: "contains(github.event.pull_request.labels.*.name, 'deep-review') || contains(github.event.pull_request.labels.*.name, 'complexity:high')"
    steps:
      - uses: actions/checkout@v4
        with: { fetch-depth: 0 }
      - uses: DailybotHQ/ai-diff-reviewer@v2
        with:
          provider: anthropic
          model: deep
          api-key: ${{ secrets.ANTHROPIC_API_KEY }}
          github-token: ${{ secrets.GITHUB_TOKEN }}
          applied-label: reviewed:deep
```

### Turn and budget caps per CLI

- `agent-max-turns` is enforced **natively on `grok`** (`--max-turns`). Claude Code has no turn cap but exposes `--max-budget-usd <amount>` (pass it via `agent-extra-args`); Codex and Cursor expose neither — the effective bound is the 900 s invocation timeout. The run logs an accurate per-provider warning when the input cannot be forwarded.
- `max-turns` (chat-completions only, default `30`) is a safety ceiling, not a target — the loop stops at `submit_review`, usually well under 10 turns.

### Billing Claude Code against a subscription (instead of API tokens)

Like Cursor's subscription model, `provider: claude-code` can bill reviews against a **Claude Pro/Max subscription** instead of metered API usage — useful if you already pay for a plan and want a flat cost.

1. On a machine logged into Claude Code with your subscription, run:
   ```bash
   claude setup-token
   ```
   It prints a long-lived OAuth token (starts with `sk-ant-oat…`).
2. Store that token as a repository secret and pass it as the action's `api-key`:
   ```yaml
   - uses: DailybotHQ/ai-diff-reviewer@v2
     with:
       provider: claude-code
       api-key: ${{ secrets.CLAUDE_CODE_OAUTH_TOKEN }}   # sk-ant-oat… token
       github-token: ${{ secrets.GITHUB_TOKEN }}
   ```

The action detects the `sk-ant-oat…` prefix and passes the value to Claude Code as `CLAUDE_CODE_OAUTH_TOKEN` (subscription auth); a normal `sk-ant-api…` key is passed as `ANTHROPIC_API_KEY` (metered) as before. No new input — the same `api-key` accepts either.

> **Security:** a subscription OAuth token grants broader account access than a scoped API key. It lives in the CLI subprocess env like any provider credential, so the [agent-runner exfiltration controls](SECURITY.md) apply with extra force — use it only with `persist-credentials: false` and on **trusted (non-fork) PRs**, never with `pull_request_target`.
>
> **Codex has no clean equivalent:** its ChatGPT-subscription auth (`codex login`) is an interactive OAuth flow whose `auth.json` tokens rotate, and using a ChatGPT plan for CI automation likely violates OpenAI's terms. Keep `provider: codex` on an API key (`gpt-5.6-luna` / `gpt-5.4-mini` are already cheap).

---

## Why an abstraction at all?

The action is fundamentally a tool-use loop. Every modern instruct-tuned model has a tool-use API, but they disagree on:

- **Message envelope shape** — Anthropic uses `messages` with content blocks of types `text` and `tool_use`; OpenAI uses `messages` with separate `tool_calls` arrays; Gemini uses `contents` with `parts` and `function_call`/`function_response`.
- **System prompt placement** — separate `system` field (Anthropic) vs. role-based (`{"role": "system"}`) message (OpenAI).
- **Caching mechanics** — Anthropic's `cache_control: ephemeral` blocks vs. OpenAI's automatic prompt caching vs. Gemini's explicit caching API.
- **Response shape** — `stop_reason` vs `finish_reason`; tool calls embedded in `content` vs separate `tool_calls`.
- **Streaming and retries** — different status codes, different error envelopes, different rate-limit headers.

Rather than abstract the messaging upward (which would force every code path to handle the lowest common denominator), the `Provider` interface makes each implementation translate **at the boundary**: we keep the in-memory representation in Anthropic's shape (the first provider, and the one the tool loop was designed around), and `OpenAIProvider` translates Anthropic-shape messages into chat-completions requests on the way out and responses back into Anthropic-shape `content` blocks on the way in (v2.1.0).

## What a provider has to satisfy

Implement this interface in `scripts/reviewer.py`:

```python
class Provider:
    def complete(
        self,
        *,
        system_prompt: str,
        messages: list[dict[str, Any]],   # Anthropic shape
        tools: list[dict[str, Any]],      # Anthropic-style input_schema
    ) -> dict[str, Any]:                  # Anthropic-shape response
        ...
```

The return value must look like an Anthropic `Messages.create` response — minimally:

```json
{
  "stop_reason": "tool_use" | "end_turn" | "max_tokens" | ...,
  "content": [
    {"type": "text", "text": "..."},
    {"type": "tool_use", "id": "<unique>", "name": "<tool_name>", "input": {...}}
  ]
}
```

Then register the implementation in `build_provider()` and add a default model in `DEFAULT_MODELS`. That's it.

## Per-provider translation notes and roadmap

### OpenAI / Azure / OpenAI-compatible — shipped as `provider: openai`

Implemented in v2.1.0 (`OpenAIProvider` + `anthropic_tools_to_openai` / `anthropic_messages_to_openai` / `openai_response_to_anthropic`). The translation notes that used to live here are now behaviour:

- Tools: Anthropic `input_schema` ↔ OpenAI `function.parameters`; `tool_choice: auto` whenever tools are present.
- Messages: system prompt → leading `system` message; each assistant `tool_use` block → a `tool_calls[]` entry (arguments JSON-encoded); each user `tool_result` block → its **own** `role: tool` message keyed by `tool_call_id`, in order.
- Responses: `choices[0].message.content` → `text` block; `tool_calls[]` → `tool_use` blocks (malformed JSON arguments are surfaced to the model as a tool error instead of crashing the loop); `finish_reason` → `stop_reason` (`tool_calls`→`tool_use`, `stop`→`end_turn`, `length`→`max_tokens`).
- Output ceiling: `max_completion_tokens` on OpenAI and Azure (current-gen models reject `max_tokens`), `max_tokens` on xAI / Z.ai / custom gateways.
- Azure: the v1 endpoint (`https://<resource>.services.ai.azure.com/openai/v1`) is OpenAI-shaped; the runtime sends both `Authorization: Bearer` and the `api-key` header. `model` is the **deployment name**.
- Caching is automatic on OpenAI/xAI (≥ 1,024-token prefixes); nothing to send.

See § "OpenAI-compatible backends" below for the consumer-facing matrix.

### Google Gemini — shipped via the OpenAI-compatible surface (v2.4.0+)

Gemini is reachable today through its **OpenAI-compatible endpoint** (`https://generativelanguage.googleapis.com/v1beta/openai`) with `provider: openai` — chat-completions in, so no native `contents`/`functionDeclarations` translation is needed. Two endpoint quirks are handled in the runtime:

- The endpoint **rejects `seed` with a 400** — the runtime omits it and pins `temperature: 0` only (part of the v2.4.0 deterministic-sampling contract).
- Everything else is the plain OpenAI-compatible behaviour: `max_tokens`, automatic tool translation, bearer auth.

The notes below are kept only in case a first-class native-surface provider is ever added:

- Gemini's native tool use uses `functionDeclarations` and the response has `functionCall` parts; `contents` is an array of `{role: "user"|"model", parts: [...]}` rather than message-with-content-blocks. Translate at the boundary.
- Native Gemini caching is explicit: you create a cached content object via a separate API call and pass its name on subsequent requests. For a 30-turn loop within one review, that's worth it; the implementation should create the cache on first call and reuse the name.

### AWS Bedrock — shipped on the `anthropic` runner (v2.5.0+)

Bedrock's Claude models speak the Anthropic Messages shape through the regional
`bedrock-runtime.{region}.amazonaws.com` InvokeModel endpoint, so the
`anthropic` runner drives them directly with two wire quirks handled in the
runtime:

- **`anthropic_version` rides inside the body** (`bedrock-2023-05-31`) — there
  is no `anthropic-version` header, and no `x-api-key`: auth is **SigV4**
  (`AWS4-HMAC-SHA256`, service `bedrock`, region parsed from the endpoint
  host), implemented in-process with the standard library.
- **Credentials** resolve from the environment first
  (`AWS_ACCESS_KEY_ID`/`AWS_SECRET_ACCESS_KEY`/`AWS_SESSION_TOKEN` — the OIDC
  pattern), then the packed `api-key` format `KEY:SECRET[:SESSION]`.
- **`cache_control` is omitted in v1** (conservative; prompt caching support
  on Bedrock is model-dependent) — a documented cost tradeoff with a
  follow-up to enable per model.
- **Out of scope for v1:** the `claude-code` runner on Bedrock
  (`CLAUDE_CODE_USE_BEDROCK`), streaming, and the Converse API.

Tier rows exist for the `anthropic` × `bedrock` cell; pass an explicit
inference-profile id (`us.anthropic.claude-sonnet-5`) when you need a specific
region routing — the indicative-price lookup normalizes the `us.` / `eu.` /
`apac.` geo prefixes, so cross-region profiles keep their cost estimate.

**Not supported in v2.5.0:** the China partition
(`bedrock-runtime.cn-north-1.amazonaws.com.cn` — five-label host, classified
as `custom`), dual-stack endpoints (`*.api.aws`), streaming
(`invoke-with-response-stream`), and the Converse API.

### Self-hosted (vLLM, Ollama, llama.cpp)

- Most expose an OpenAI-compatible chat-completions endpoint: `provider: openai` + `api-base: https://<your-gateway>/v1` (plain `http://` is accepted for `localhost` / `127.0.0.1` / `[::1]` so a local dev gateway works). Anthropic-shaped gateways use `provider: anthropic` + `api-base`. Custom hosts log a WARNING naming where the key goes.

## Adding a runner or backend

The bar for merging, in the order the code is wired:

1. **Backend first, when it is only a new host.** Add the host suffix to `ENDPOINT_HOST_SUFFIXES` → a kind → `_profile_for_kind` quirks (auth style, cache flags, Codex wire API) → a tier row in `MODEL_TIER_TABLE` + `INDICATIVE_PRICES_USD_PER_MTOK` (dated). Every URL must still come from `resolve_endpoint_profile`; never build one in a provider.
2. **Runner (new `provider` id).** A `Provider` (in-process; translate at the boundary) or an `AgentRunnerProvider` (CLI; file-based findings contract, `_build_cli_env`, private temp files 0600, usage parser bounded by `CLI_STDOUT_SCAN_MAX_BYTES`), `PROVIDER_ID`, `DEFAULT_MODELS`, `PROVIDER_DEFAULT_ENDPOINT_KIND` / `PROVIDER_DEFAULT_API_BASE`, `build_provider` dispatch, `register_secret` for any new credential.
3. **`action.yml`.** An install step guarded by `if: inputs.provider == '<id>'` (skips when the binary is already on `PATH`), an optional `<cli>-version` input, the `provider` description; `validate_action.py` must stay green.
4. **CI.** `cli-install-smoke` matrix entry; a `self-review.yml` leg keyed on secret presence.
5. **Tests.** Default-profile snapshot (`DefaultProfileBackCompatSnapshotTests`), matrix row (`RunnerBackendMatrixTests`), credential lane (`HardeningRegressionTests`), telemetry fixture.
6. **Docs.** This matrix + the cost matrix, `README.md` (runners table, inputs row, roadmap), `examples/provider-<id>.yml` + `examples/README.md` row, `skills/ai-diff-reviewer/setup/reference.md` and the wizard's Q1 table, `docs/SECURITY.md` credential lanes, `CHANGELOG.md`.
7. **Live evidence.** One real PR reviewed with the new runner/backend, tracking comment + usage line pasted in the PR description (the `/prompt-test` skill has the procedure).

## Cost considerations

The Anthropic provider caches both the system prompt and the diff-bearing first user message, so turns 2..N read them at ~10 % of the input price; OpenAI/xAI cache long prefixes automatically. Every review ends with a `**Usage:**` line in the tracking comment (see "Usage telemetry per provider") — read it before tuning anything.

---

## Anthropic-compatible backends (`provider: anthropic` + `api-base`, v2.1.0+)

`provider` names the **runner** (who owns the review loop); the optional `api-base` input names the **backend** (where the model lives). For the direct chat-completions runner that means any Anthropic-compatible Messages endpoint:

| Backend | `api-base` | `api-key` | Models | Notes |
|---|---|---|---|---|
| Anthropic (default) | *(empty)* | Anthropic API key | `claude-sonnet-4-6` (default) | Byte-identical to previous releases: `x-api-key` auth, `cache_control` on the system prompt. |
| Z.ai GLM (Coding Plan) | `https://api.z.ai/api/anthropic` | Z.ai Coding Plan key | `glm-5.3`, `glm-5.3-flash` | Flat-rate plan ⇒ ≈ 0 marginal cost per review. Zero-install GLM path; the deepest GLM reviews use `provider: claude-code` with the same base (see below). |
| xAI Grok | `https://api.x.ai` | xAI API key | `grok-4.5`, `grok-4.6` | Anthropic-compatible surface of the xAI API. |
| Moonshot/Kimi | `https://api.moonshot.ai/anthropic` | Moonshot API key | `kimi-k2-0905-preview`, `kimi-k2-turbo-preview` | Kimi for Claude Code: the `claude-code` runner runs K2 through this base. |
| MiniMax | `https://api.minimax.io/anthropic` | MiniMax API key | `MiniMax-M2`, `MiniMax-Text-01` | M2 documents Claude Code via this Anthropic-compatible endpoint. |
| Any other host | `https://<gateway>` | gateway key | gateway-defined | Treated as a plain Anthropic-compatible gateway (the run logs a warning naming the host). |

How the profile changes the request:

- **URL:** `<api-base>/v1/messages` — a trailing slash on `api-base` is stripped; the default composes to exactly `https://api.anthropic.com/v1/messages`.
- **Auth headers:** Anthropic gets `x-api-key` only. Other hosts get **both** `x-api-key` and `Authorization: Bearer <key>` (Z.ai documents bearer auth, xAI documents `x-api-key`; sending both is harmless and avoids a per-gateway matrix).
- **Prompt caching:** the `cache_control` breakpoint is sent **only** to `api.anthropic.com`. Compatible gateways cache server-side automatically and may reject unknown block fields, so the runtime omits it there.
- **Sampling:** since v2.4.0 the first-party Anthropic host pins `temperature: 0`; compatible gateways receive **no** sampling fields — the same conservative pattern as `cache_control` — because their tolerance for extra fields is not documented.
- **Errors and logs** name the endpoint kind and host (`zai messages API (api.z.ai) HTTP 401 …`), never the key.

Copy-paste workflow: [`examples/provider-anthropic-zai.yml`](../examples/provider-anthropic-zai.yml). Validation rules for `api-base` (https only, no embedded credentials) and the security note live in the README inputs table and [`SECURITY.md`](SECURITY.md).

---

## Z.ai GLM — recommended runner and why (v2.1.0+)

GLM models are reachable from three runners. The recommendation, in order:

| Rank | Runner | `api-base` | Why |
|---|---|---|---|
| **1 — recommended** | `claude-code` | `https://api.z.ai/api/anthropic` | Z.ai's Coding Plan is **flat-rate**, so the marginal cost of a review is ≈ 0 — the same economics that make Cursor `auto` attractive. Z.ai's first-class integration target is the Claude Code harness, so GLM's tool use is tuned for this loop, and you get the vendor coding-agent prompt, LSP and semantic tools. Deepest GLM review available. |
| 2 | `anthropic` | `https://api.z.ai/api/anthropic` | Zero install, bounded turns, most predictable. Same flat-rate billing. Pick this when you want the review to be cheap **and** deterministic, or on untrusted PRs where you do not want an agent CLI with local access. |
| 3 | `codex` / `openai` | `https://api.z.ai/api/v1` (Codex, Responses API) / `https://api.z.ai/api/coding/paas/v4` (`openai`) | Supported for teams standardised on the OpenAI-shaped stack; less battle-tested than the Anthropic-shaped path for GLM. |

**Models:** `glm-5.3` is the balanced default for GLM backends; `glm-5.3-flash` is the cheaper smoke tier. Always pin `model` explicitly — on a custom backend `auto` is rejected (it only means something on Anthropic's own endpoint, where it can silently select Opus).

**How the `claude-code` runner talks to Z.ai.** When `api-base` is set the action switches Claude Code to the backend env contract Z.ai documents: `ANTHROPIC_BASE_URL=<api-base>`, `ANTHROPIC_AUTH_TOKEN=<api-key>` (bearer-style; `ANTHROPIC_API_KEY` is deliberately **not** set), `API_TIMEOUT_MS=3000000`, and `ANTHROPIC_DEFAULT_OPUS_MODEL` / `SONNET_MODEL` / `HAIKU_MODEL` all pinned to `model` so Claude Code's internal aliases resolve to the chosen GLM model; `--model` is always passed. A Claude subscription token (`sk-ant-oat…`) is rejected against a non-Anthropic host with an actionable error. xAI's Anthropic-compatible surface (`https://api.x.ai`, e.g. `grok-4.5`) uses the identical contract.

Copy-paste workflows: [`examples/provider-claude-code-glm.yml`](../examples/provider-claude-code-glm.yml) (recommended) and [`examples/provider-anthropic-zai.yml`](../examples/provider-anthropic-zai.yml) (zero-install).

---

## OpenAI-compatible backends (`provider: openai` + `api-base`, v2.1.0+)

The in-process OpenAI-compatible runner is the most portable path: zero install, a bounded loop, and one input to move between vendors.

| Backend | `api-base` | `api-key` | Models | Notes |
|---|---|---|---|---|
| OpenAI (default) | *(empty)* | OpenAI API key | `gpt-5.6-luna` (default), `gpt-5.4-mini` (smoke) | `max_completion_tokens`; automatic prompt caching. |
| Azure Foundry (v1) | `https://<resource>.services.ai.azure.com/openai/v1` | Azure key | your **deployment names** (e.g. `gpt-5.4-mini-azure`) | Bearer + `api-key` headers; `max_completion_tokens`. |
| xAI Grok | `https://api.x.ai/v1` | xAI API key | `grok-4.5`, `grok-4.6` | `max_tokens`; automatic caching. |
| Z.ai GLM (Coding Plan) | `https://api.z.ai/api/coding/paas/v4` | Z.ai Coding Plan key | `glm-5.3`, `glm-5.3-flash` | `max_tokens`; flat-rate plan. |
| DeepSeek (v2.4.0+) | `https://api.deepseek.com` | DeepSeek API key | `deepseek-chat`, `deepseek-reasoner` | `max_tokens`; deterministic `temperature: 0` + `seed` pinned. |
| Moonshot/Kimi (v2.4.0+) | `https://api.moonshot.ai/v1` | Moonshot API key | `kimi-k2-0905-preview`, `kimi-k2-turbo-preview` | `max_tokens`; also Anthropic-compatible (table above). |
| MiniMax (v2.4.0+) | `https://api.minimax.io/v1` | MiniMax API key | `MiniMax-M2`, `MiniMax-Text-01` | `max_tokens`; also Anthropic-compatible (table above). |
| Qwen / DashScope (v2.4.0+) | `https://dashscope.aliyuncs.com/compatible-mode/v1` | DashScope API key | `qwen3-coder-plus`, `qwen-turbo` | Compatible-mode endpoint; `max_tokens`. |
| Google Gemini (v2.4.0+) | `https://generativelanguage.googleapis.com/v1beta/openai` | Gemini API key | `gemini-2.5-pro`, `gemini-2.5-flash` | `seed` omitted — the endpoint rejects it with a 400. |
| OpenRouter (v2.4.0+) | `https://openrouter.ai/api/v1` | OpenRouter API key | vendor-prefixed ids (`deepseek/deepseek-chat`, …) | Meta-gateway; prices are the underlying vendors' + ~5%. |
| Self-hosted / other | `https://<gateway>/v1` (or `http://localhost:…` for local dev) | gateway key | gateway-defined | Plain OpenAI-compatible behaviour; the run logs the host. |

Copy-paste workflows: [`examples/provider-openai.yml`](../examples/provider-openai.yml) (OpenAI/Azure/xAI/Z.ai variants) plus one recipe per v2.4.0 backend — [`provider-deepseek.yml`](../examples/provider-deepseek.yml), [`provider-kimi.yml`](../examples/provider-kimi.yml), [`provider-minimax.yml`](../examples/provider-minimax.yml), [`provider-qwen.yml`](../examples/provider-qwen.yml), [`provider-gemini.yml`](../examples/provider-gemini.yml), [`provider-openrouter.yml`](../examples/provider-openrouter.yml). Errors and logs name the endpoint kind and host, never the key.

**Verified live (2026-09-16):** the `openai` runner completed a tool-enabled request against xAI (`grok-4.3`, `https://api.x.ai/v1`) and Azure Foundry (`gpt-5.4-mini-azure` deployment, v1 endpoint) with the action's real tool schema — both returned `end_turn` with `usage` populated. Z.ai could not be exercised that day (quota exhausted).

---

## Agent Runner Provider Contract (v1.1.0)

Alongside the chat-completions `Provider` above, `scripts/reviewer.py` supports a second provider family: **`AgentRunnerProvider`**. Rather than owning the tool-use loop, this family shells out to a vendor's coding-agent CLI in headless mode and receives structured findings via a file-based contract. This is what powers `provider: claude-code`, `provider: cursor`, and `provider: codex`.

### Why file-based (and not MCP, not fenced-stdout)?

- **Portable across CLIs.** Every vendor CLI can already write files; the schema is our contract, not theirs.
- **Robust to stdout noise.** CLIs emit banners, progress bars, warnings, and streaming JSON that would be brittle to parse.
- **Small blast radius.** A malformed findings file surfaces a clean error to the operator; a broken stdout parser would silently produce empty reviews.
- **Future-proof.** When the ecosystem coalesces on MCP-as-tool-server we'll add it as an additional path; file-based stays as the fallback.

### The file

Every agent-runner provider MUST write its review to:

    <workspace>/.aiprr/findings.json

exactly once, at the end of its run. `parse_findings_file()` in `scripts/reviewer.py` reads and validates it.

### The schema

```json
{
  "summary": "markdown body of the overall review",
  "complexity": "low",
  "findings": [
    {
      "path": "src/foo.py",
      "line": 42,
      "body": "markdown body of this inline comment",
      "severity": "critical",
      "start_line": 40,
      "side": "RIGHT"
    }
  ]
}
```

| Field | Type | Required | Notes |
|---|---|---|---|
| `summary` | string | recommended | Markdown for the top-level review body. Empty string is legal (produces a default fallback summary). |
| `complexity` | string | optional* | Exactly one of `low`, `medium`, `high` (case-insensitive). Drives `complexity-labels-enabled` when set. *Required in the output contract when the consumer enables complexity labeling. |
| `findings` | array | required | May be empty (means "no issues"). |
| `prior_findings` | array | optional* | Incremental follow-up mode (v2.1.0+): one `{"fingerprint", "status": "resolved\|open\|regressed", "note"}` per row of the *Your prior findings still open* table the prompt showed. *Required by the output contract when the run is a follow-up review; ignored otherwise. Unknown fingerprints and statuses are logged and dropped. |
| `findings[].path` | string | required | Repo-relative file path. Must appear in the PR diff. |
| `findings[].line` | integer | required | Line number (end line for multi-line). Must appear in the diff. |
| `findings[].body` | string | required | Non-empty markdown body of the inline comment. |
| `findings[].severity` | string | optional (default `info`) | Exactly one of `critical`, `warning`, `info` (lowercase). Drives the strictness gate. |
| `findings[].start_line` | integer | optional | Start line for multi-line comments. |
| `findings[].side` | string | optional (default `RIGHT`) | `LEFT` or `RIGHT` (case-normalised). `RIGHT` = new code, `LEFT` = removed. |

### Validation guarantees

`parse_findings_file()` in `scripts/reviewer.py` enforces:

- Root is a JSON object (list/string/number roots rejected).
- `findings` is a list (dict/scalar rejected).
- Every finding is an object with non-empty `path`, integer `line`, non-empty `body`.
- Severity is exactly one of the allowed values (case-insensitive on input, lowercased on output).
- Side is `LEFT`/`RIGHT` (case-insensitive on input, uppercased on output).
- Unknown top-level or per-finding keys are silently ignored (forward-compatibility with vendor extensions).

Missing files raise `FileNotFoundError` with an actionable message. Malformed JSON raises `ValueError` with the offending snippet quoted.

### Degrade paths (v2.2.0+)

| CLI outcome | What the action does | Check / label |
|---|---|---|
| exit 0, findings file written | normal review | strictness gate as usual |
| non-zero exit, findings file written | posts the review with a `Partial review: <cli> exited with code N` footer and a WARNING in the log | strictness gate as usual |
| exit 0, no findings file | **retried once** with a fresh session when the first attempt used less than half of the CLI timeout (both attempts' usage is reported, the summary carries a `Retried once` note); if the retry also produces no file, posts an explicit summary-only **incomplete review** naming the cause | **fails** under every blocking strictness (`lenient` stays green); the reviewed label is not stamped; `label-once` keeps the toggle armed; prior IAR state is re-embedded unchanged |
| non-zero exit, no findings file | the run fails with the CLI's stderr/stdout tail | red |

Any findings file that exists before the CLI starts is removed first, so a file that exists afterwards was written by this run. CLI stdout/stderr are captured **bounded** (last 4 MB of each stream): a chatty agent cannot grow the reviewer's memory, and a multi-megabyte prompt on stdin cannot deadlock against a full pipe.

### The prompt directive

CLI providers wrap the review instructions with `write_findings_prompt_directive()`, which appends the schema + "write your findings to this file before ending your turn" instruction to whatever comes from `prompts/default.md`. The directive is standardised so every CLI writes the same schema — one parser, three producers.

### Adding a new agent-runner provider

1. Implement `AgentRunnerProvider` — install check + `run_review()` that invokes the CLI with `write_findings_prompt_directive`-wrapped instructions and returns `parse_findings_file(findings_path)`.
2. Register in `build_provider()`.
3. Add to `DEFAULT_MODELS`.
4. Add a conditional install step in `action.yml` (see the modular-install pattern in Task 07 of the DWP plan).
5. Add a matrix entry in `.github/workflows/self-review.yml` for dogfooding.

### Headless-CI invocation requirements (per CLI)

Each vendor CLI needs three things to work headlessly: the review instructions delivered as **text** (not a path), the ability to **write** `findings.json` without an interactive approval prompt, and the (large) user prompt passed via **stdin** to avoid the OS `E2BIG` single-argument limit.

| CLI | Instructions | Write-permission flag | Prompt input |
|-----|--------------|-----------------------|--------------|
| Claude Code | `--append-system-prompt <text>` | `--permission-mode bypassPermissions` | stdin (`claude -p`) |
| Cursor | inlined into the prompt | `--force --trust` | stdin (`cursor-agent -p`) |
| Codex | inlined into the prompt | `--dangerously-bypass-approvals-and-sandbox` | stdin (`codex exec -`) |
| Grok | `--rules <text>` (appended to the system prompt) | `--always-approve` | private 0600 temp file via `--prompt-file` (`-p` needs an inline value and does not read stdin) |

The write-permission flags are load-bearing: the runner is already an isolated ephemeral sandbox, but the CLIs default to gating file writes (Claude Code's permission prompt) or a read-only sandbox (Codex `exec`), either of which silently prevents `findings.json` from being written. See [`docs/SECURITY.md`](SECURITY.md) § "Agent-runner providers: residual exfiltration surface" for the trust-boundary implications of these flags.

### Codex auth model (0.122+ requires `$CODEX_HOME/auth.json`, not `OPENAI_API_KEY`)

Codex CLI 0.122 changed how it reads credentials: it **no longer honours** `OPENAI_API_KEY` from the environment and instead reads credentials **only** from `$CODEX_HOME/auth.json` (default `~/.codex/auth.json`). Without that file — or with a ChatGPT-mode `auth.json` present from a prior interactive `codex login` — `codex exec` fails with:

```
401 Unauthorized: Missing bearer or basic authentication in header,
url: https://api.openai.com/v1/responses
```

AI Diff Reviewer handles this automatically. For each Codex invocation the provider:

1. Creates an isolated per-run `CODEX_HOME` via `tempfile.mkdtemp(prefix="aiprr-codex-")` (mode `0700`) — importantly *not* `~/.codex/`, so a self-hosted runner with a persistent ChatGPT-mode session file is never overridden and never clobbered.
2. Writes an apikey-mode `auth.json` at `$CODEX_HOME/auth.json` (mode `0600`) whose content is `{"OPENAI_API_KEY": "<your key>"}`.
3. Forwards `CODEX_HOME=<the tempdir>` in the `codex exec` subprocess env alongside `OPENAI_API_KEY` (the latter kept for back-compat with Codex < 0.122).
4. Removes the entire `CODEX_HOME` in a `finally` block after the invocation returns — success or failure.

No consumer action is required. If you need to override where `auth.json` is materialized (e.g. an air-gapped runner with a pre-seeded `CODEX_HOME`), that is a roadmap item; open an issue.

### Codex on Azure Foundry / xAI / Z.ai (`api-base`, v2.1.0+)

When `provider: codex` runs with a non-default `api-base`, the provider writes a `config.toml` **next to `auth.json` in the same isolated per-run `CODEX_HOME`** (mode `0600`, removed in the `finally` block) that routes the CLI to an OpenAI-compatible **Responses API** backend:

```toml
model = "<your model or deployment name>"
model_provider = "aiprr"

[model_providers.aiprr]
name = "AI Diff Reviewer backend (<kind>)"
base_url = "<api-base>"
env_key = "OPENAI_API_KEY"        # the key is already forwarded under this name
wire_api = "responses"
# Azure hosts only — Foundry rejects plain text turns without these:
http_headers = { "x-ms-oai-image-generation-deployment" = "gpt-image-1" }
[features]
image_generation = false
```

| Backend | `api-base` | `model` | Notes |
|---|---|---|---|
| Azure Foundry (v1) | `https://<resource>.services.ai.azure.com/openai/v1` | your **deployment name** (e.g. `gpt-5.4-mini-azure`) | Image-generation header workaround + `image_generation = false` added automatically. |
| xAI Grok | `https://api.x.ai/v1` | `grok-4.5` / `grok-4.6` | Responses API. **Rejected by xAI with Codex 0.154** (see verification below) — prefer `provider: openai` or `provider: grok`. |
| Z.ai GLM | `https://api.z.ai/api/v1` | `glm-5.3` / `glm-5.3-flash` | **Responses** base — different from the chat base (`/api/coding/paas/v4`) used by `provider: openai`. Unverified; prefer `claude-code` or `openai` for GLM. |

`model` is **required** on a custom backend (`auto` and empty are rejected with an actionable error): Codex's built-in default only exists on OpenAI. Everything else — the apikey-mode `auth.json`, the `--dangerously-bypass-approvals-and-sandbox` flag, stdin prompt delivery, the MCP caveat — is unchanged. Copy-paste workflow: [`examples/provider-codex-azure.yml`](../examples/provider-codex-azure.yml).

**Model catalog (why a `models.json` is written too).** Codex resolves per-model capabilities from its bundled catalog — including OpenAI-only tool types that third-party Responses endpoints reject (xAI answers `422 … tools[].type: unknown variant "namespace"`). So on a custom backend the provider also writes `models.json` into the same `CODEX_HOME`: one entry cloned from a bundled template (`gpt-5.4`, else `gpt-5.4-mini`, else the first) under **your** model id, with conservative capabilities (`use_responses_lite = false`, no search/apps/plugin tool namespaces, no service tiers), and `config.toml` points `model_catalog_json` at it. This is the recipe the maintainer runs locally and it is what makes xAI work; Azure Foundry works with or without it. If `codex debug models --bundled` is unavailable on the installed CLI, the run logs it and proceeds without a catalog.

**Verification (2026-09-16, Codex CLI 0.154.0, headless `codex exec` with the generated files):**

| Backend | Result | Detail |
|---|---|---|
| Azure Foundry (`gpt-5.4-mini-azure` deployment) | ✅ works | With and without the cloned catalog. |
| xAI (`grok-4.3`) | ❌ **not usable with Codex 0.154** | The catalog removes the `namespace` tool types, but Codex 0.154 only supports `apply_patch_tool_type = "freeform"`, which is sent as `tools[].type: custom`; xAI's Responses API rejects it (`422 … unknown variant "custom"`). Not fixable from this action. **Use `provider: openai` + `api-base: https://api.x.ai/v1` (in-process, verified) or `provider: grok` for xAI.** A consumer who knows an older Codex build that predates freeform `apply_patch` can pin it via `codex-version`. |
| Z.ai (`glm-5.3`, Responses base) | ⚠️ unverified | The plan's weekly quota was exhausted on the verification day. Expect the same `custom`-tool sensitivity as xAI; the `openai` runner on `https://api.z.ai/api/coding/paas/v4` and the `claude-code` runner are the verified GLM paths. |

The runtime logs a WARNING naming this limitation whenever Codex runs on an `xai`, `zai` or `custom` host, and never blocks (a future CLI or gateway release may lift it).

### Known limitations of the agent-runner path

- **`agent-max-turns` is enforced natively only on `grok`** (`--max-turns`). Claude Code, Cursor and Codex expose no turn-count flag; when the input is set on those providers the run logs a per-provider warning naming the alternative (Claude Code: `--max-budget-usd` via `agent-extra-args`; otherwise the 900 s invocation timeout is the bound).
- **`mcp-config-file` passthrough:** works for **Cursor** (`~/.cursor/mcp.json` + `--approve-mcps`) and **Claude Code** (passed via `--mcp-config <file>`). For **Codex** it does **not** take effect — Codex configures MCP via `config.toml`, not a JSON file — and the run warns accordingly without copying the ignored JSON file into `~/.codex` or the isolated per-run `CODEX_HOME`; supply MCP config via `agent-extra-args` (`-c mcp_servers...`) or a preconfigured `config.toml`.
- **Malformed CLI JSON fallback:** agent-runner providers are instructed to write strict JSON to `.aiprr/findings.json`. If a CLI exits successfully but writes malformed JSON with a recoverable top-level `summary`, AI Diff Reviewer posts a summary-only review and logs a warning; inline findings are dropped because malformed finding objects cannot be trusted. Direct parser validation remains strict unless this fallback is explicitly enabled at the subprocess boundary.

---

## xAI Grok CLI — `provider: grok` (v2.1.0+)

The official `grok` CLI as an agent-runner, kept inside the action's review contract rather than xAI's suggested bare workflow (`grok -p "Review this PR" --always-approve` with `GH_TOKEN` in the env):

| Concern | xAI's bare workflow | `provider: grok` |
|---|---|---|
| GitHub token | handed to the agent | **never reaches the CLI** — the runtime posts the review |
| Output | free-form text the agent posts itself | `.aiprr/findings.json` → severity gating, IAR dedup, inline cap, collapse-previous, tracking comment |
| Web search / subagents | on | **off by default** (`--disable-web-search`, `--no-subagents`; plan mode off too) — re-enable via `agent-extra-args` |
| Turn cap | none | `agent-max-turns` → native `--max-turns` |
| Prompt delivery | inline `-p` (argv limit) | rubric + contract via `--rules`; diff via a private `--prompt-file` (0600 in a 0700 temp dir, removed after the run) |

- **Auth / billing:** `XAI_API_KEY` from `api-key`, billed as xAI API credits. `api-base` is ignored (the CLI talks to xAI only; use `provider: openai` + `https://api.x.ai/v1` if you want the in-process path).
- **Models:** `grok-4.5` default (v2.3.0+; `balanced` and `economy`); `grok-4.6` for a deeper reasoning pass (`deep`). `grok-4.3` is not offered: it returned 0 of 5 known defects in the 2026-09-16 benchmark. Never `auto`.
- **Install:** the official installer (`curl -fsSL https://x.ai/cli/install.sh | bash`) drops a static binary in `~/.grok/bin`; `grok-version` pins it (`bash -s <X.Y.Z>`). Same supply-chain consideration as the Cursor installer (see [`SECURITY.md`](SECURITY.md)).
- **Output format:** the runtime asks for `--output-format json`; the CLI returns one JSON document with `usage` (input/output/cache tokens), `num_turns` and `total_cost_usd` — consumed by the usage telemetry.
- **MCP:** `mcp-config-file` is not wired for Grok (warned); configure MCP through `grok mcp` or `agent-extra-args`.

Copy-paste workflow: [`examples/provider-grok.yml`](../examples/provider-grok.yml).

## Usage telemetry per provider (v2.1.0+)

Every review reports what its provider can tell us. The tracking comment ends with a `**Usage:**` line and the run log prints `Usage: source=… in=… cache_read=… cache_write=… out=… turns=… cost_usd=…`; the `iteration-tokens-used` output includes uncached input, cache reads, cache writes and output exactly once. OpenAI/Codex cached-input subsets are subtracted before normalization; Anthropic input partitions are already disjoint.

| Provider | Usage source | Cache stats | Cost |
|---|---|---|---|
| `anthropic` | API `usage` on every turn, summed | reads + writes | indicative estimate from list prices |
| `openai` | API `usage` on every turn, summed (`prompt_tokens_details.cached_tokens` → cache reads) | reads | indicative estimate |
| `claude-code` | stream-json `result` event | reads + writes | **vendor-reported** `total_cost_usd` |
| `codex` | `--json` `turn.completed` events, summed | reads + writes | indicative estimate (Codex reports none) |
| `grok` | JSON document (`usage`, `num_turns`, `total_cost_usd`) | reads + writes | **vendor-reported** |
| `cursor` | `--output-format json` (v2.2.0+, parse-or-ignore: any `usage` object the CLI prints) | when the CLI reports them | `total_cost_usd` when reported; otherwise `not reported by this provider` — **unverified live** (no Cursor key in the dogfood matrix); please report the CLI's JSON shape if the line stays empty |

Estimates use `INDICATIVE_PRICES_USD_PER_MTOK` (dated in `scripts/reviewer.py`; cache reads at 10 % and writes at 125 % of the input price) and are labelled `(indicative)`; vendor-reported costs are shown as-is. Unknown model ids (Azure deployment names, custom gateways) show tokens without a cost. Nothing here is meant to gate CI.

## Cursor CLI — billing and model selection

The `provider: cursor` leg has a materially different cost profile from the chat-completions providers, and its subscription-only model surprises consumers who assume they can bring their own API key. This section clarifies what to expect.

### Billing model

- **`CURSOR_API_KEY` must belong to a Cursor subscription** (Pro, Pro+, or Ultra). There is **no BYOK** — Cursor CLI does not accept OpenAI, Anthropic, or self-hosted keys. Every review consumes credits from that subscription.
- Usage is visible on the [Cursor Dashboard](https://cursor.com/dashboard). Under a Pro plan, the monthly credit allowance is shared between the IDE and CI/CLI usage; large PRs on `composer-2.5` or `sonnet-4.6` can burn credits quickly if you review every push.
- Pricing terms live at [`cursor.com/pricing`](https://cursor.com/pricing).

### Recommended default: `model: auto`

- Cursor's `auto` selector routes to the best available model based on availability and load. On Pro plans, `auto` is **unlimited** (subject to fair-use rate limits) and is the right default for CI to avoid draining monthly credits on premium models.
- **`auto` is the built-in default for `provider: cursor`** (empty `model:` resolves to it). You only need to set it explicitly if you want to be self-documenting:

  ```yaml
  - uses: DailybotHQ/ai-diff-reviewer@v2
    with:
      provider: cursor
      api-key: ${{ secrets.CURSOR_API_KEY }}
      model: auto
  ```

- Pin a specific model only when you have a concrete reason (e.g. reproducibility for a research review, or you want to force `sonnet-4.6` for the highest-quality passes). Otherwise `auto` is the cheapest correct choice.

### Headless-CI defaults (v1.2.0+)

The `CursorProvider` always passes these flags on top of anything you set in `agent-extra-args`:

- **`--force`** — skips interactive tool-approval prompts. Without this the CLI can stall in CI when it wants to run a tool that would normally prompt in the IDE.
- **`--trust`** — marks the workspace as trusted for the duration of the run. Same rationale as `--force`.
- **`--approve-mcps`** (added conditionally when `mcp-config-file` is set) — suppresses the interactive MCP-approval prompt.

These are Cursor's own recommendations from the [Headless CLI docs](https://cursor.com/docs/cli/headless) for CI usage. Consumers do NOT need to add them via `agent-extra-args`; the action wires them by default. If you need to override (rare), pass a different combination via `agent-extra-args:` — argv appends after the defaults, so the last occurrence of a flag wins in the CLI's own parsing.

### Monitoring cost

Every run logs the resolved model + argv (with the API key redacted). Combine that with the Cursor Dashboard to correlate action runs with credit consumption. If a repo's monthly review load starts costing more than expected, the fix is almost always one of:

1. Switch to `model: auto` (unlimited on Pro).
2. Use `trigger-mode: label-once` (v1.2.0+) so reviews fire only on-demand.
3. Use `label-gate` to skip reviews on non-user-facing PRs.

### Comparison with the other providers

| | Cursor | Claude Code | Codex | Anthropic (direct) |
|---|---|---|---|---|
| Auth | Subscription API key | Anthropic API key **or** `sk-ant-oat…` subscription token (`claude setup-token`) | OpenAI API key | Anthropic API key |
| Billing | Cursor subscription credits | Anthropic-metered tokens **or** Claude Pro/Max subscription | OpenAI-metered tokens | Anthropic-metered tokens |
| BYOK | ❌ Not supported | ✅ Bring your own Anthropic key | ✅ Bring your own OpenAI key | ✅ Bring your own Anthropic key |
| Subscription plan | ✅ `model: auto` on Pro | ✅ via `sk-ant-oat…` token (see "Billing Claude Code against a subscription") | ❌ Metered (no clean CI path) | ❌ Metered |
| Best for | Teams already on Cursor Pro | Teams already on Anthropic (API or Pro/Max plan) | Teams already on OpenAI | Pure API workloads |

---

## Running more than one provider on the same PR

Most consumers run **one provider per PR** — that's the common case and needs no special setup. But you *can* run several providers side-by-side on the same PR, and it works correctly out of the box.

**`collapse-previous` is scoped per-provider.** Every review body and tracking comment carries an invisible per-provider marker (`<!-- ai-pr-reviewer-provider: <id> -->`). When `collapse-previous` runs (default `true`), it only minimizes *this provider's own* prior artefacts — it will **not** collapse a different provider's review, even though all jobs share one `github-token` (author `github-actions[bot]`). So each provider keeps a single live review, and re-running a provider outdates only its own previous run.

To run multiple providers cleanly:

1. Keep `collapse-previous` at its default (`true`) — the per-provider scoping does the right thing.
2. **Give each provider a distinct `applied-label`** (e.g. `reviewed:anthropic`, `reviewed:codex`) so you can tell the reviews apart in the conversation tab.

This repo's own [`self-review.yml`](../.github/workflows/self-review.yml) uses this pattern: every runner/backend whose secret is configured reviews each `ready`-labelled PR with its own label — `self-reviewed:anthropic`, `self-reviewed:claude-code`, `self-reviewed:cursor`, `self-reviewed:codex`, `self-reviewed:grok`, `self-reviewed:claude-code-glm` (Z.ai via `api-base`), `self-reviewed:codex-azure` (Azure Foundry via `api-base`) and `self-reviewed:openai` (in-process, default-on whenever `OPENAI_API_KEY` exists). The same runner can appear twice with different backends: a non-empty `api-base` adds a stable endpoint hash to the marker scope, isolating collapse, IAR and label-once tracking state. Default endpoints retain the historical runner marker. Give each lane a distinct applied label; different models on the same runner/endpoint still share a lane.

> **Passing multiple provider API keys** (e.g. both `ANTHROPIC_API_KEY` and `OPENAI_API_KEY` as repo secrets) is fine and does **not** cause cross-contamination: each job forwards only its own provider's key to the CLI subprocess (`_build_cli_env` scrubs everything else), and a single action invocation uses exactly one `provider` + one `api-key`. There is no "both keys in one run" mode — the keys only coexist as separate secrets consumed by separate jobs.

> **Transition note.** A review posted by a version **before** per-provider scoping shipped has no provider marker, so the first run after upgrading won't auto-collapse that one pre-upgrade review (it stays live until you manually mark it outdated). Every review from the upgraded version onward collapses correctly.

> **Scoping keys on the marker, not the author.** A useful side effect: `collapse-previous` no longer collapses unrelated `github-actions[bot]` comments (a coverage bot, a labeler) — only comments carrying this action's provider marker are ever minimized.

---
