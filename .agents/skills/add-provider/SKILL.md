---
name: add-provider
description: Scaffold a new LLM Provider implementation in scripts/reviewer.py — class, registry entry, default model, action.yml inputs, docs updates. Implementing the actual translation logic is the user's job.
disable-model-invocation: false
allowed-tools: Read, Write, Edit, Glob, Grep, Bash
model: opus
tier: 3
intent: scaffold
max-files: 6
max-loc: 250
---

# Skill: Add Provider

## Objective

Scaffold a new LLM provider in `scripts/reviewer.py` following the pattern set by `AnthropicProvider`. Adds the class skeleton, the `build_provider()` registry entry, the `DEFAULT_MODELS` mapping, any provider-specific `action.yml` inputs, and the corresponding doc updates.

The skill produces the **scaffold and the doc trail**; the actual translation between Anthropic-shape messages/tools and the new provider's API shape is the user's job. See [.agents/agents/provider-implementer.md](../../agents/provider-implementer.md) for the deep dive on translation gotchas.

## Non-goals

- Does NOT implement the translation logic (each provider's API shape differs; the agent calling this skill writes the body).
- Does NOT add non-stdlib dependencies. If the provider can't be implemented stdlib-only, stop and surface that to the user — it's a design decision, not a workaround.
- Does NOT cut a release. After scaffolding, the user merges, smoke-tests, and uses the `/release` skill separately.

## Inputs

- `provider_id` — the value users will pass via `inputs.provider` (e.g. `openai`, `azure-openai`, `google`, `bedrock`, `vllm`).
- `provider_name` — human-readable name for docstrings and docs (e.g. "OpenAI", "Azure OpenAI", "Google Gemini").
- `default_model` — recommended default model id for the provider.
- `provider_inputs` — optional list of new `action.yml` inputs (e.g. `["azure-resource", "azure-deployment", "azure-api-version"]`).
- `api_url` — the provider's HTTP endpoint.

## Pre-flight

```bash
# Read the contract
cat docs/PROVIDERS.md

# Read the reference impl
grep -n "class AnthropicProvider" scripts/reviewer.py

# Confirm we have an [Unreleased] section ready
grep -A1 "^## \[Unreleased\]" CHANGELOG.md
```

If `docs/PROVIDERS.md` doesn't list the new provider in its roadmap table, ask the user whether to add it before scaffolding.

## Steps

### 1. Add URL constants near `ANTHROPIC_API_URL`

```python
<NAME>_API_URL: str = "<api_url>"
# Add any version/auth headers as further constants here.
```

### 2. Add the provider class after `AnthropicProvider`

Skeleton with a clear `# TODO` for the translation work:

```python
class <Name>Provider(Provider):
    """<provider_name> Messages API client.

    Translates Anthropic-shape messages/tools to <provider>'s
    <chat-completions / generateContent / etc.> schema, and translates
    the response back to Anthropic-shape `content` blocks for the
    agentic loop in `drive_review()`.
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
        # TODO(provider-implementer): translate Anthropic-shape input to
        # <provider>'s request shape. Pattern to mirror from
        # AnthropicProvider:
        #   - bounded retries on 429/5xx via API_RETRY_DELAYS_S
        #   - returns dict with stop_reason + content[] in Anthropic shape
        raise NotImplementedError(
            "<provider_id> provider scaffold — translation logic pending."
        )
```

### 3. Add to `DEFAULT_MODELS`

```python
DEFAULT_MODELS: dict[str, str] = {
    "anthropic": "claude-sonnet-4-6",
    "<provider_id>": "<default_model>",
}
```

### 4. Register in `build_provider()`

```python
def build_provider(provider_id: str, *, api_key: str, model: str) -> Provider:
    if provider_id == "anthropic":
        return AnthropicProvider(api_key=api_key, model=model)
    if provider_id == "<provider_id>":
        return <Name>Provider(api_key=api_key, model=model)
    raise ValueError(...)
```

### 5. Add provider-specific inputs to `action.yml` (if any)

For each input in `provider_inputs`:

```yaml
  <input-name>:
    description: '<concise description>. Used only when `provider: <provider_id>`.'
    required: false
    default: ''
```

Forward each to the runtime via `AIPRR_<INPUT_NAME>` in the composite action's `env:` block, and read it from `os.environ` in `main()`.

### 6. Update `docs/PROVIDERS.md`

- Flip the status table entry to `🟡 scaffolded (translation pending)` until the implementation is complete; flip to `✅ shipping` only after smoke tests pass.
- Add a section under "Specific gotchas per planned provider" if not already present, capturing translation notes the implementer is going to need.

### 7. Update `README.md`

- Provider roadmap table at the bottom.
- Inputs table if you added new inputs.

### 8. Update `CHANGELOG.md`

Under `[Unreleased]`:

```markdown
### Added (in progress)
- `<provider_id>` provider scaffold — class, registry, default model.
  Translation logic pending; not yet user-invocable.
```

When the implementation is complete, the entry promotes to:

```markdown
### Added
- `<provider_id>` provider — translates Anthropic-shape messages/tools to
  <provider_name>'s API. Default model: <default_model>. See docs/PROVIDERS.md.
```

## Output to user

After running, summarise what was scaffolded:

- Files modified: `scripts/reviewer.py`, `action.yml` (if inputs added), `docs/PROVIDERS.md`, `README.md`, `CHANGELOG.md`.
- The `# TODO(provider-implementer)` markers placed in the new class.
- Pointers to `.agents/agents/provider-implementer.md` for the next step.

Remind the user that the scaffold currently raises `NotImplementedError` — it doesn't ship until the translation is implemented and smoke-tested.

## Quality gates after scaffolding

- [ ] `python3 -m py_compile scripts/reviewer.py` passes.
- [ ] `action.yml` parses.
- [ ] No new non-stdlib imports introduced.
- [ ] All TODO markers reference `provider-implementer` (so they're discoverable via `grep`).
