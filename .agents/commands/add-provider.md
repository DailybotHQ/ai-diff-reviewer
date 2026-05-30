---
description: Scaffold a new Provider implementation in scripts/reviewer.py
---

# Add Provider

Scaffold a new LLM `Provider` implementation in `scripts/reviewer.py` following the pattern established by `AnthropicProvider`. Adds the provider class, registers it in `build_provider()`, adds a `DEFAULT_MODELS` entry, and updates `docs/PROVIDERS.md` from "roadmap" to "shipping".

This skill creates the **scaffold**; the actual translation logic for the new provider's API shape is your job — see [.agents/agents/provider-implementer.md](../agents/provider-implementer.md) for the deep dive on translation gotchas.

## Pre-flight

- Read `docs/PROVIDERS.md` end-to-end so you know the contract a new provider must satisfy.
- Read `AnthropicProvider` in `scripts/reviewer.py` — that's the reference implementation.
- Confirm the new provider can be implemented stdlib-only. If not, stop — that's a design discussion, not a "just install one dep" PR.

## Steps

### 1. Pick the provider id and default model

The id is the value users will pass via `inputs.provider`. Conventions:

- `openai` for OpenAI
- `azure-openai` for Azure OpenAI
- `google` for Google Gemini
- `bedrock` for AWS Bedrock
- `vllm` / `ollama` / `openai-compatible` for self-hosted

The default model is the recommended one for typical PR-review workloads. For a roadmap reference, see the table in `docs/PROVIDERS.md`.

### 2. Add the class

In `scripts/reviewer.py`, immediately after `AnthropicProvider`, add:

```python
class <Name>Provider(Provider):
    """<one-sentence description>.

    Translates Anthropic-shape messages and tools to <provider>'s
    <chat-completions / generateContent / etc.> schema, and translates
    the response back to Anthropic-shape `content` blocks for the
    agentic loop.
    """

    def __init__(self, *, api_key: str, model: str) -> None:
        self.api_key: str = api_key
        self.model: str = model

    def complete(
        self,
        *,
        system_prompt: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
    ) -> dict[str, Any]:
        # 1. Translate Anthropic-shape input → provider-shape request
        # 2. POST with bounded retries on 429/5xx (mirror the
        #    API_RETRY_DELAYS_S pattern from AnthropicProvider)
        # 3. Translate provider response → Anthropic-shape dict
        #    with `stop_reason` and `content: list[{type, ...}]`
        ...
```

Place URL constants and version headers next to `ANTHROPIC_API_URL` at the top of the file:

```python
<NAME>_API_URL: str = "https://..."
<NAME>_API_VERSION: str = "..."
```

### 3. Register in `build_provider()`

```python
def build_provider(provider_id: str, *, api_key: str, model: str) -> Provider:
    if provider_id == "anthropic":
        return AnthropicProvider(api_key=api_key, model=model)
    if provider_id == "<id>":
        return <Name>Provider(api_key=api_key, model=model)
    raise ValueError(...)
```

### 4. Add to `DEFAULT_MODELS`

```python
DEFAULT_MODELS: dict[str, str] = {
    "anthropic": "claude-sonnet-4-6",
    "<id>": "<recommended-default-model>",
}
```

### 5. Add provider-specific inputs to `action.yml` (if any)

Examples:

- Azure OpenAI: `azure-resource`, `azure-deployment`, `azure-api-version`.
- Self-hosted: `api-base`.
- Bedrock: `aws-region`.

Each new input is a public contract — name it carefully.

### 6. Update `docs/PROVIDERS.md`

- Flip the status table entry from 🛠 roadmap to ✅ shipping.
- Document any provider-specific inputs in a new subsection.
- Add a "Translation notes" subsection documenting the gotchas you hit (this is invaluable for future maintainers).

### 7. Update `README.md`

- Provider roadmap table at the bottom.
- Inputs table if you added new inputs.

### 8. Update `CHANGELOG.md`

Under `[Unreleased]`:

```markdown
### Added
- `<id>` provider — translates Anthropic-shape messages/tools to
  <provider>'s API. Default model: <model>. See docs/PROVIDERS.md.
```

If you added new inputs:

```markdown
- New optional inputs: `<input1>`, `<input2>` (used only when `provider: <id>`).
```

### 9. Smoke test

Required before merge:

1. **Compile-check:** `python3 -m py_compile scripts/reviewer.py`.
2. **New-provider live run:** open a sandbox PR; run with `provider: <id>`. Capture the review URL.
3. **Anthropic regression run:** open a different sandbox PR; run with `provider: anthropic`. Confirm unchanged behaviour.
4. **Multi-tool turn:** the smoke-test PR should trigger the model calling `read_file` AND `grep` in the same turn — verify the tool-call translation handles batched calls.

Paste the URLs into the PR description.

### 10. Open the PR

Use `/pr` to generate the PR description. Make sure the description includes:

- Smoke-test PR URLs (new provider + Anthropic regression).
- A 1-paragraph "what was hard" summary — translation subtleties, caching semantics, anything a future maintainer should know.

## Quality gates

- **Stdlib only.** No `boto3`, `openai`, `google-generativeai`, etc.
- **No new files** unless the provider implementation is genuinely large (>300 LOC), in which case `scripts/providers/<name>.py` is acceptable but must stay stdlib-only.
- **Don't refactor the agentic loop.** New providers add code; they don't change `drive_review()`.
- **Preserve the existing Anthropic path.** `AnthropicProvider` and the constants it uses must not change.
