# Providers — current status and how to add a new one

## Status

| Provider | Status | Default model | Tracking issue |
|---|---|---|---|
| Anthropic | ✅ shipping in v1 | `claude-sonnet-4-6` | n/a |
| OpenAI | 🛠 roadmap (v1.1) | tbd (`gpt-4o`?) | tbd |
| Azure OpenAI | 🛠 roadmap (v1.1) | tbd | tbd |
| Google Gemini | 🛠 roadmap (v1.2) | tbd (`gemini-2.5-pro`?) | tbd |
| AWS Bedrock | 🤔 considering | claude via Bedrock | tbd |
| Self-hosted (vLLM/Ollama) | 🤔 considering | tbd | tbd |

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

### OpenAI

- OpenAI's `tools` schema accepts `function` items with `parameters` (JSONSchema). Anthropic's `tools` schema accepts top-level items with `input_schema` (also JSONSchema). The translation is mostly trivial; the biggest landmine is **tool_call id matching**: Anthropic's `tool_use_id` is paired with `tool_result.tool_use_id` in the next message; OpenAI's `tool_call_id` is paired with the `tool_call.id` of an `assistant`-role message containing a `tool_calls` array. Different message envelope shapes; same underlying mechanic.
- OpenAI does prompt caching automatically (no header needed) for prompts ≥1024 tokens. Just send the prompt; you get the discount.
- OpenAI response: `choices[0].message` has either `content` (text) or `tool_calls`. Translate to Anthropic-shape `content` blocks before returning.

### Azure OpenAI

- Same protocol as OpenAI but the URL is `https://<resource>.openai.azure.com/openai/deployments/<deployment>/chat/completions?api-version=...`. Add inputs `azure-resource`, `azure-deployment`, `azure-api-version`.

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

### Known limitations of the agent-runner path

- **`agent-max-turns` is not enforced for the CLI providers.** None of the shipping CLIs (Claude Code, Cursor, Codex) expose a turn-count cap flag on their current versions, so the input can't be forwarded. When it is set, the run now logs a clear warning (rather than silently ignoring it) — the effective bound is the `CLI_INVOCATION_TIMEOUT` (900 s). For a real cap use `agent-extra-args` with a vendor-native flag (e.g. Claude Code's `--max-budget-usd`).
- **`mcp-config-file` passthrough:** works for **Cursor** (`~/.cursor/mcp.json` + `--approve-mcps`) and **Claude Code** (passed via `--mcp-config <file>`). For **Codex** it does **not** take effect — Codex configures MCP via `config.toml`, not a JSON file — and the run warns accordingly without copying the ignored JSON file into `~/.codex` or the isolated per-run `CODEX_HOME`; supply MCP config via `agent-extra-args` (`-c mcp_servers...`) or a preconfigured `config.toml`.
- **Malformed CLI JSON fallback:** agent-runner providers are instructed to write strict JSON to `.aiprr/findings.json`. If a CLI exits successfully but writes malformed JSON with a recoverable top-level `summary`, AI Diff Reviewer posts a summary-only review and logs a warning; inline findings are dropped because malformed finding objects cannot be trusted. Direct parser validation remains strict unless this fallback is explicitly enabled at the subprocess boundary.

---

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

- **Frequency** is the biggest lever. Running several providers on every push is N× the reviews. Pick one provider for routine use, or gate the expensive legs (this repo's `self-review.yml` runs a cheap Anthropic baseline on every PR and only invokes the CLI providers when the diff touches runtime/action/prompt surfaces).
- **Model** matters most for the agent-runner CLIs (`claude-code`, `codex`), which are autonomous agents that explore the repo and spend far more tokens than the bounded chat-completions path — and whose turn count can't be capped from the action (only the 900 s timeout bounds them).

### Quality is not optional for review

Code review's value is catching **subtle** bugs — logic errors, race conditions, security issues. That's exactly where model capability pays off, so the cheapest model is not always the best *value*:

- **Haiku 4.5 / mini-tier models** are great for obvious bugs, style, and fast smoke passes, but noticeably weaker at the subtle bugs that justify running a reviewer. A cheap review that misses real issues can be worse than none (false confidence).
- **Sonnet-class** models are the sweet spot for real review — strong bug-finding at roughly 1/5th of Opus cost.
- **Opus-class** is best but usually overkill for routine PRs.

### Default models (chosen for quality/cost balance)

| Provider | Default model | Approx. API price (in / out per 1M) | Rationale |
|---|---|---|---|
| `anthropic` | `claude-sonnet-4-6` | $3 / $15 | Sweet spot for review quality. |
| `claude-code` | `claude-sonnet-4-6` | $3 / $15 | Sweet spot. Never `auto` (could be Opus $5/$25). Pin `claude-haiku-4-5` for a cheaper/shallower smoke review. |
| `cursor` | `auto` | subscription (flat) | Unlimited on Cursor Pro → ~$0 marginal. `auto` is the right choice here. |
| `codex` | `gpt-5.6-luna` | $1 / $6 | Current-gen budget model — the OpenAI parallel of the Sonnet-class choice: strong enough for subtle bugs, far below codex-tier (`gpt-5-codex` ≈$1.75/$14, and deprecated). Pin the cheaper `gpt-5.4-mini` ($0.75/$4.50) for a shallower smoke review. |

Prices are indicative (mid-2026) and change — check each vendor's pricing page. Anthropic has no separate "mini" tier: **Haiku 4.5 is the small/cheap Claude**; OpenAI's mini is `gpt-5.4-mini`. The consumer defaults for the metered providers are all **quality-tier** (Sonnet-class / current-gen budget) — the mini/Haiku tiers are reserved for smoke/dogfood passes (see `self-review.yml`), never a consumer default.

### Recommendations

- **Real reviews (consumers):** keep the Sonnet-class defaults — the quality is the point.
- **Cheapest predictable setup:** `provider: anthropic` (bounded loop + prompt caching keep it low and stable).
- **Cheapest if you're on Cursor Pro:** `provider: cursor`, `model: auto` (flat rate).
- **Smoke/dogfood reviews** (backed by human review, e.g. this repo's self-review baseline): `claude-haiku-4-5` / `gpt-5.4-mini` are fine — a cheap sanity pass, with deeper providers reserved for high-risk changes.
- **`max-turns` (chat-completions only):** the default `30` is a *safety ceiling*, not a target — the loop stops as soon as the model calls `submit_review` (usually well under 10 turns), so it rarely drives cost. It does not apply to the CLI providers. Lower it (e.g. `12`, as `self-review.yml` does for its smoke baseline) only to bound a pathological run.

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
