# Providers — current status and how to add a new one

## Status

| Provider | Status | Default model | Tracking issue |
|---|---|---|---|
| Anthropic | ✅ shipping in v1 | `claude-sonnet-4-6` | n/a |
| OpenAI-compatible (`openai`) | ✅ shipping (v2.1.0) | `gpt-5.6-luna` | n/a |
| Azure Foundry / Azure OpenAI | ✅ via `api-base` (`openai`, `codex`) | deployment name | n/a |
| Google Gemini | 🛠 roadmap (v1.2) | tbd (`gemini-2.5-pro`?) | tbd |
| AWS Bedrock | 🤔 considering | claude via Bedrock | tbd |
| Self-hosted (vLLM/Ollama, any OpenAI- or Anthropic-compatible gateway) | ✅ via `api-base` (custom host) | gateway-defined | n/a |
| xAI Grok CLI (`grok`) | ✅ shipping (v2.1.0) — agent-runner | `grok-4.3` | n/a |

The roadmap is loose and contributor-driven. If you want a provider sooner than the order above suggests, send a PR.

## Why an abstraction at all?

The action is fundamentally a tool-use loop. Every modern instruct-tuned model has a tool-use API, but they disagree on:

- **Message envelope shape** — Anthropic uses `messages` with content blocks of types `text` and `tool_use`; OpenAI uses `messages` with separate `tool_calls` arrays; Gemini uses `contents` with `parts` and `function_call`/`function_response`.
- **System prompt placement** — separate `system` field (Anthropic) vs. role-based (`{"role": "system"}`) message (OpenAI).
- **Caching mechanics** — Anthropic's `cache_control: ephemeral` blocks vs. OpenAI's automatic prompt caching vs. Gemini's explicit caching API.
- **Response shape** — `stop_reason` vs `finish_reason`; tool calls embedded in `content` vs separate `tool_calls`.
- **Streaming and retries** — different status codes, different error envelopes, different rate-limit headers.

Rather than abstract the messaging upward (which would force every code path to handle the lowest common denominator), the `Provider` interface makes each implementation translate **at the boundary**: we keep the in-memory representation in Anthropic's shape (because it's currently the only provider), and a future OpenAI provider would translate Anthropic-shape messages into OpenAI requests on the way out, and OpenAI responses back into Anthropic-shape `content` blocks on the way in.

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

## Specific gotchas per planned provider

### OpenAI / Azure / OpenAI-compatible — shipped as `provider: openai`

Implemented in v2.1.0 (`OpenAIProvider` + `anthropic_tools_to_openai` / `anthropic_messages_to_openai` / `openai_response_to_anthropic`). The translation notes that used to live here are now behaviour:

- Tools: Anthropic `input_schema` ↔ OpenAI `function.parameters`; `tool_choice: auto` whenever tools are present.
- Messages: system prompt → leading `system` message; each assistant `tool_use` block → a `tool_calls[]` entry (arguments JSON-encoded); each user `tool_result` block → its **own** `role: tool` message keyed by `tool_call_id`, in order.
- Responses: `choices[0].message.content` → `text` block; `tool_calls[]` → `tool_use` blocks (malformed JSON arguments are surfaced to the model as a tool error instead of crashing the loop); `finish_reason` → `stop_reason` (`tool_calls`→`tool_use`, `stop`→`end_turn`, `length`→`max_tokens`).
- Output ceiling: `max_completion_tokens` on OpenAI and Azure (current-gen models reject `max_tokens`), `max_tokens` on xAI / Z.ai / custom gateways.
- Azure: the v1 endpoint (`https://<resource>.services.ai.azure.com/openai/v1`) is OpenAI-shaped; the runtime sends both `Authorization: Bearer` and the `api-key` header. `model` is the **deployment name**.
- Caching is automatic on OpenAI/xAI (≥ 1,024-token prefixes); nothing to send.

See § "OpenAI-compatible backends" below for the consumer-facing matrix.

### Google Gemini

- Gemini's tool use uses `functionDeclarations` and the response has `functionCall` parts. The bigger translation: Gemini's `contents` is an array of `{role: "user"|"model", parts: [...]}` rather than message-with-content-blocks. The `model` role replaces `assistant`. Translate at the boundary.
- Gemini's caching is explicit: you create a cached content object via a separate API call and pass its name on subsequent requests. For a 30-turn loop within one review, that's worth it; the implementation should create the cache on first call and reuse the name.

### AWS Bedrock

- Bedrock's Anthropic models use the same Anthropic API shape under `bedrock-runtime` `InvokeModel` / `Converse`. Likely the easiest provider to add; the main work is auth (SigV4) and endpoint routing.

### Self-hosted (vLLM, Ollama, llama.cpp)

- Most expose an OpenAI-compatible chat-completions endpoint. If yours does, the OpenAI provider should work with `api-key: <whatever>` and a custom base URL. Plan to add an `api-base` input alongside the OpenAI provider for this case.

## Testing a new provider

The bar for merging a provider implementation:

1. **Compile-check passes** (`python3 -m py_compile scripts/reviewer.py`).
2. **Manual smoke test on a real PR** — open a PR in a fork or sandbox repo, run the action with `provider: <new>`, paste the resulting tracking comment + review URL in the PR description.
3. **No regressions on existing providers** — run the smoke test on a second PR with `provider: anthropic` to confirm nothing leaked.
4. **`docs/PROVIDERS.md` updated** with the new entry, default model, and any provider-specific inputs.
5. **`CHANGELOG.md` updated** under `[Unreleased]`.

We don't ask for a unit-test framework yet — the testing surface is the integration with the provider's API, which is hard to mock honestly. Smoke tests on real PRs are the bar.

## Cost considerations

The Anthropic provider uses prompt caching aggressively, so a long custom prompt only pays full token cost on the first turn. When adding new providers, replicate this where possible: it cuts the cost of a typical review by ~5x once the cache warms.

---

## Anthropic-compatible backends (`provider: anthropic` + `api-base`, v2.1.0+)

`provider` names the **runner** (who owns the review loop); the optional `api-base` input names the **backend** (where the model lives). For the direct chat-completions runner that means any Anthropic-compatible Messages endpoint:

| Backend | `api-base` | `api-key` | Models | Notes |
|---|---|---|---|---|
| Anthropic (default) | *(empty)* | Anthropic API key | `claude-sonnet-4-6` (default) | Byte-identical to previous releases: `x-api-key` auth, `cache_control` on the system prompt. |
| Z.ai GLM (Coding Plan) | `https://api.z.ai/api/anthropic` | Z.ai Coding Plan key | `glm-5.3`, `glm-5.3-flash` | Flat-rate plan ⇒ ≈ 0 marginal cost per review. Zero-install GLM path; the deepest GLM reviews use `provider: claude-code` with the same base (see below). |
| xAI Grok | `https://api.x.ai` | xAI API key | `grok-4.3`, `grok-4.6` | Anthropic-compatible surface of the xAI API. |
| Any other host | `https://<gateway>` | gateway key | gateway-defined | Treated as a plain Anthropic-compatible gateway (the run logs a warning naming the host). |

How the profile changes the request:

- **URL:** `<api-base>/v1/messages` — a trailing slash on `api-base` is stripped; the default composes to exactly `https://api.anthropic.com/v1/messages`.
- **Auth headers:** Anthropic gets `x-api-key` only. Other hosts get **both** `x-api-key` and `Authorization: Bearer <key>` (Z.ai documents bearer auth, xAI documents `x-api-key`; sending both is harmless and avoids a per-gateway matrix).
- **Prompt caching:** the `cache_control` breakpoint is sent **only** to `api.anthropic.com`. Compatible gateways cache server-side automatically and may reject unknown block fields, so the runtime omits it there.
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

**How the `claude-code` runner talks to Z.ai.** When `api-base` is set the action switches Claude Code to the backend env contract Z.ai documents: `ANTHROPIC_BASE_URL=<api-base>`, `ANTHROPIC_AUTH_TOKEN=<api-key>` (bearer-style; `ANTHROPIC_API_KEY` is deliberately **not** set), `API_TIMEOUT_MS=3000000`, and `ANTHROPIC_DEFAULT_OPUS_MODEL` / `SONNET_MODEL` / `HAIKU_MODEL` all pinned to `model` so Claude Code's internal aliases resolve to the chosen GLM model; `--model` is always passed. A Claude subscription token (`sk-ant-oat…`) is rejected against a non-Anthropic host with an actionable error. xAI's Anthropic-compatible surface (`https://api.x.ai`, e.g. `grok-4.3`) uses the identical contract.

Copy-paste workflows: [`examples/provider-claude-code-glm.yml`](../examples/provider-claude-code-glm.yml) (recommended) and [`examples/provider-anthropic-zai.yml`](../examples/provider-anthropic-zai.yml) (zero-install).

---

## OpenAI-compatible backends (`provider: openai` + `api-base`, v2.1.0+)

The in-process OpenAI-compatible runner is the most portable path: zero install, a bounded loop, and one input to move between vendors.

| Backend | `api-base` | `api-key` | Models | Notes |
|---|---|---|---|---|
| OpenAI (default) | *(empty)* | OpenAI API key | `gpt-5.6-luna` (default), `gpt-5.4-mini` (smoke) | `max_completion_tokens`; automatic prompt caching. |
| Azure Foundry (v1) | `https://<resource>.services.ai.azure.com/openai/v1` | Azure key | your **deployment names** (e.g. `gpt-5.4-mini-azure`) | Bearer + `api-key` headers; `max_completion_tokens`. |
| xAI Grok | `https://api.x.ai/v1` | xAI API key | `grok-4.3`, `grok-4.6` | `max_tokens`; automatic caching. |
| Z.ai GLM (Coding Plan) | `https://api.z.ai/api/coding/paas/v4` | Z.ai Coding Plan key | `glm-5.3`, `glm-5.3-flash` | `max_tokens`; flat-rate plan. |
| Self-hosted / other | `https://<gateway>/v1` (or `http://localhost:…` for local dev) | gateway key | gateway-defined | Plain OpenAI-compatible behaviour; the run logs the host. |

Copy-paste workflow with all four variants: [`examples/provider-openai.yml`](../examples/provider-openai.yml). Errors and logs name the endpoint kind and host, never the key.

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
| xAI Grok | `https://api.x.ai/v1` | `grok-4.3` / `grok-4.6` | Responses API. **Rejected by xAI with Codex 0.154** (see verification below) — prefer `provider: openai` or `provider: grok`. |
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
- **Models:** `grok-4.3` default (daily tier); `grok-4.6` for a deeper reasoning pass. Never `auto`.
- **Install:** the official installer (`curl -fsSL https://x.ai/cli/install.sh | bash`) drops a static binary in `~/.grok/bin`; `grok-version` pins it (`bash -s <X.Y.Z>`). Same supply-chain consideration as the Cursor installer (see [`SECURITY.md`](SECURITY.md)).
- **Output format:** the runtime asks for `--output-format json`; the CLI returns one JSON document with `usage` (input/output/cache tokens), `num_turns` and `total_cost_usd` — consumed by the usage telemetry.
- **MCP:** `mcp-config-file` is not wired for Grok (warned); configure MCP through `grok mcp` or `agent-extra-args`.

Copy-paste workflow: [`examples/provider-grok.yml`](../examples/provider-grok.yml).

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

This repo's own [`self-review.yml`](../.github/workflows/self-review.yml) uses this pattern when CLI-provider dogfooding is enabled by the workflow's critical-file scope gate. The direct Anthropic baseline runs on every PR/push; the other provider legs run only when provider-sensitive files changed.

> **Passing multiple provider API keys** (e.g. both `ANTHROPIC_API_KEY` and `OPENAI_API_KEY` as repo secrets) is fine and does **not** cause cross-contamination: each job forwards only its own provider's key to the CLI subprocess (`_build_cli_env` scrubs everything else), and a single action invocation uses exactly one `provider` + one `api-key`. There is no "both keys in one run" mode — the keys only coexist as separate secrets consumed by separate jobs.

> **Transition note.** A review posted by a version **before** per-provider scoping shipped has no provider marker, so the first run after upgrading won't auto-collapse that one pre-upgrade review (it stays live until you manually mark it outdated). Every review from the upgraded version onward collapses correctly.

> **Scoping keys on the marker, not the author.** A useful side effect: `collapse-previous` no longer collapses unrelated `github-actions[bot]` comments (a coverage bot, a labeler) — only comments carrying this action's provider marker are ever minimized.

---

## Choosing a cost-efficient model

Two things drive review cost: **how often it runs** and **which model it uses**.

- **Frequency** is the biggest lever. Running several providers on every push is N× the reviews. Pick one provider for routine use, or gate the expensive legs (this repo's `self-review.yml` runs a cheap baseline on every `ready` PR and reserves deeper passes for high-risk changes).
- **Model** matters most for the agent-runner CLIs (`claude-code`, `codex`, `grok`), which are autonomous agents that explore the repo and spend far more tokens than the bounded chat-completions path.

### Quality is not optional for review

Code review's value is catching **subtle** bugs — logic errors, race conditions, security issues. That is exactly where model capability pays off, so the cheapest model is not always the best *value*: a cheap review that misses real issues can be worse than none (false confidence). The **balanced** tier below is the quality/cost sweet spot for real reviews; **economy** is for smoke/dogfood passes and docs-only PRs; **deep** for high-risk PRs.

### Pick a cost profile in one word — `model: balanced | economy | deep`

Cursor `auto` proved the pattern: a one-word cost profile is what teams actually use day to day. The `model` input now accepts the same idea for every runner × backend — the runtime resolves the tier from the matrix below and logs the concrete id. Empty `model` keeps resolving to the built-in default (unchanged for existing consumers); an explicit id always passes through.

### Cost-efficient defaults matrix (verified 2026-09-16 — ids and prices move, re-check when bumping)

Indicative list prices in USD per 1M tokens (input / output). Cached input is cheaper on every vendor (Anthropic cache reads are 10 % of input price; OpenAI/xAI cache automatically; Z.ai cache currently free).

| Runner | Backend | `balanced` (default recommendation) | `economy` (smoke) | `deep` (high-risk PRs) | Rationale |
|---|---|---|---|---|---|
| `anthropic`, `claude-code` | Anthropic | `claude-sonnet-5` — $2 / $10 | `claude-haiku-4-5` — $1 / $5 | `claude-opus-5` — $5 / $25 | Sonnet 5 is current **and** cheaper than the legacy `claude-sonnet-4-6` ($3 / $15) that the built-in default still names for back-compat — the run logs a hint; `model: balanced` opts in. Never `auto` on Claude Code (can silently be Opus). |
| `anthropic`, `claude-code` | Z.ai (Coding Plan) | `glm-5.3` — $1.40 / $4.40 | `glm-5.3-flash` — $0.15 / $0.50 | `glm-5.3` | Flat-rate Coding Plan ⇒ marginal cost ≈ 0 either way; `claude-code` is the recommended GLM runner. |
| `anthropic`, `claude-code` | xAI (Anthropic-compatible) | `grok-4.3` — $1.25 / $2.50 | `grok-4.3` | `grok-4.6` — $2 / $6 | 4.3 is the daily tier at a low price point; 4.6 is the reasoning tier. (Prices are the <200k-token rates; above that they double.) |
| `openai`, `codex` | OpenAI | `gpt-5.6-luna` — $0.20 / $1.20 | `gpt-5.6-luna` | `gpt-5.6-terra` — $2 / $12 | Luna is both the balanced **and** the economy pick: `gpt-5.4-mini` ($0.75 / $4.50) is no longer cheaper. Codex-tier `gpt-5.3-codex` is $1.75 / $14. |
| `openai`, `codex` | xAI | `grok-4.3` | `grok-4.3` | `grok-4.6` | Same xAI reasoning; note Codex 0.154 cannot talk to xAI (see the Codex section) — use `openai` or `grok`. |
| `openai`, `codex` | Z.ai | `glm-5.3` | `glm-5.3-flash` | `glm-5.3` | Flat-rate plan. |
| `grok` | xAI | `grok-4.3` | `grok-4.3` | `grok-4.6` | The Grok CLI's own system prompt + tools weigh ≈ 12k input tokens per call — the telemetry line makes that visible. |
| `cursor` | Cursor subscription | `auto` | `auto` | `composer-2.5` | `auto` is flat-rate on Pro and routes well; `composer-2.5` burns metered credits — reserve for deep passes. |
| any | Azure Foundry / custom gateway | *(no tier rows)* | | | Deployment names are consumer-defined; a tier word fails fast with guidance — set `model` to the deployment name or gateway model id. |

Built-in defaults when `model` is empty (unchanged this release): `claude-sonnet-4-6` (anthropic, claude-code), `gpt-5.6-luna` (openai, codex), `grok-4.3` (grok), `auto` (cursor).

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
