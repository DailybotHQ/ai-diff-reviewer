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

Scaffold a new LLM provider in `scripts/reviewer.py` following the pattern set by the shipping references — `AnthropicProvider` for the chat-completions family or `ClaudeCodeProvider` / `CursorProvider` / `CodexProvider` for the agent-runner family. Adds the class skeleton, the `build_provider()` registry entry, the `DEFAULT_MODELS` mapping, any provider-specific `action.yml` inputs (including CLI install steps for agent-runners), and the corresponding doc updates.

The skill produces the **scaffold and the doc trail**; the actual message/response translation (chat-completions) or the CLI subprocess wiring specifics (agent-runner) are the user's job. See [.agents/agents/provider-implementer.md](../../agents/provider-implementer.md) for the deep dive on both families.

## Family selection — ASK FIRST

Before scaffolding, ask the user which family the new provider belongs to:

- **Chat-completions (`Provider`)** — the vendor exposes a raw HTTP messages/completions API and you own the tool-use loop (Anthropic, OpenAI, Gemini, Bedrock, vLLM/Ollama).
- **Agent-runner (`AgentRunnerProvider`)** — the vendor ships a headless coding-agent CLI you shell out to; the CLI writes `.aiprr/findings.json` (Claude Code, Cursor Agent, OpenAI Codex, Aider, Continue, ...).

The rest of this skill has two implementation branches; run the one matching the family.

## Non-goals

- Does NOT implement chat-completions translation logic (each provider's API shape differs).
- Does NOT invent CLI subprocess quirks for agent-runner providers — you must confirm the CLI flags with vendor docs.
- Does NOT add non-stdlib dependencies. If the provider can't be implemented stdlib-only, stop and surface that to the user — it's a design decision, not a workaround.
- Does NOT cut a release. After scaffolding, the user merges, smoke-tests, and uses the `/release` skill separately.

## Inputs

- `provider_id` — the value users will pass via `inputs.provider` (e.g. `openai`, `azure-openai`, `google`, `bedrock`, `vllm`, `claude-code`, `cursor`, `codex`).
- `provider_name` — human-readable name for docstrings and docs (e.g. "OpenAI", "Azure OpenAI", "Google Gemini", "Claude Code", "Cursor Agent").
- `provider_family` — `"chat-completions"` or `"agent-runner"`.
- `default_model` — recommended default model id for the provider (or `"auto"` sentinel for agent-runner if the CLI picks its own default).
- `provider_inputs` — optional list of new `action.yml` inputs (e.g. `["azure-resource", "azure-deployment", "azure-api-version"]` for Azure, or the per-CLI version pin like `<provider_id>-version`).
- For chat-completions: `api_url` — the provider's default HTTP endpoint (becomes the runner's `PROVIDER_DEFAULT_API_BASE` entry).
- For agent-runner: `cli_bin` (binary name on `PATH`), `mcp_dest` (relative path under `$HOME`, e.g. `.claude/mcp.json`), `install_command` (npm-install or curl-installer snippet).
- `endpoint_kind` — the `EndpointProfile.kind` the runner uses by default (`anthropic`, `openai`, `xai`, `zai`, `azure`, or `custom`). **Ask first whether this is a new runner or only a new backend host**: a host that speaks an existing protocol needs no provider class — add its suffix to `ENDPOINT_HOST_SUFFIXES`, its quirks to `_profile_for_kind`, and a tier row; consumers reach it through `api-base` (v2.1.0+).

## Pre-flight

```bash
# Read the contract (both families)
cat docs/PROVIDERS.md

# Read the reference impl(s) — OpenAIProvider / GrokProvider are the v2.1.0 templates
grep -n "class AnthropicProvider\|class OpenAIProvider\|class ClaudeCodeProvider\|class CursorProvider\|class CodexProvider\|class GrokProvider" scripts/reviewer.py

# Read the backend contract every provider must honour (never build a URL yourself)
grep -n "def resolve_endpoint_profile\|class EndpointProfile\|^ENDPOINT_HOST_SUFFIXES\|^PROVIDER_DEFAULT_API_BASE\|^PROVIDER_DEFAULT_ENDPOINT_KIND\|^MODEL_TIER_TABLE" scripts/reviewer.py

# The end-to-end checklist (code → action.yml → CI → tests → docs → live evidence)
sed -n '/^## Adding a runner or backend/,/^## /p' docs/PROVIDERS.md

# Confirm we have an [Unreleased] section ready
grep -A1 "^## \[Unreleased\]" CHANGELOG.md
```

If `docs/PROVIDERS.md` doesn't list the new provider in its roadmap table, ask the user whether to add it before scaffolding.

## Steps — chat-completions family

### 1. Register the default backend (constants block, v2.1.0 layout)

```python
# Default endpoint for the runner when `api-base` is empty.
PROVIDER_DEFAULT_API_BASE["<provider_id>"] = "<api_url>"
PROVIDER_DEFAULT_ENDPOINT_KIND["<provider_id>"] = "<endpoint_kind>"
# Cost tiers (balanced / economy / deep) per (runner, kind) + dated prices.
MODEL_TIER_TABLE[("<provider_id>", "<endpoint_kind>")] = _<NAME>_TIERS
```

The request URL is always `profile.base_url` + the protocol path constant — never a literal host inside the class (`.review/extension.md` flags that as `critical`).

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

    PROVIDER_ID: str = "<provider_id>"

    def __init__(
        self,
        *,
        api_key: str,
        model: str,
        profile: EndpointProfile | None = None,
    ) -> None:
        self.api_key: str = api_key
        self.model: str = model
        # Backend profile; `None` = the runner's default endpoint.
        self.profile: EndpointProfile = (
            profile
            if profile is not None
            else resolve_endpoint_profile("", self.PROVIDER_ID)
        )

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
def build_provider(
    provider_id: str, *, api_key: str, model: str, api_base: str = ""
) -> Provider | AgentRunnerProvider:
    profile: EndpointProfile = resolve_endpoint_profile(api_base, provider_id)
    ...
    if provider_id == "<provider_id>":
        return <Name>Provider(api_key=api_key, model=model, profile=profile)
    raise ValueError(...)
```

Then lock it: add the runner to `tests/test_backends.py::RunnerBackendMatrixTests.RUNNERS`, a default-profile snapshot in `tests/test_agent_runner_providers.py::DefaultProfileBackCompatSnapshotTests` (agent-runners), the credential lane in `HardeningRegressionTests.test_credential_lanes_per_agent_runner`, and a telemetry fixture in `tests/test_telemetry.py`.

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

## Steps — agent-runner family

### 1. Add class attributes constants block near the other AgentRunnerProviders

Nothing to add at module scope; the constants live on the class itself (`CLI_NAME`, `CLI_BIN`, `MCP_DEST`).

### 2. Add the provider class after `CodexProvider`

Skeleton with a clear `# TODO` for CLI-flag confirmation:

```python
class <Name>Provider(AgentRunnerProvider):
    """<provider_name> agent-runner client.

    Shells out to the `<cli_bin>` CLI; the CLI owns the tool-use loop and
    writes findings to `.aiprr/findings.json` via the schema documented in
    docs/PROVIDERS.md.
    """

    CLI_NAME: str = "<provider_id>"
    CLI_BIN: str = "<cli_bin>"
    MCP_DEST: str = "<mcp_dest>"  # e.g. ".<vendor>/mcp.json"

    PROVIDER_ID: str = "<provider_id>"

    def __init__(
        self,
        *,
        api_key: str,
        model: str,
        extra_args: str,
        mcp_config_file: str,
        profile: EndpointProfile | None = None,
    ) -> None:
        self.api_key: str = api_key
        self.model: str = model
        self.extra_args: str = extra_args
        self.profile: EndpointProfile = (
            profile if profile is not None
            else resolve_endpoint_profile("", self.PROVIDER_ID)
        )  # warn and ignore, or map to the CLI's env/config contract (see Claude Code / Codex)
        self.mcp_config_file: str = mcp_config_file

    def install(self) -> None:
        # Defensive check — install is done in action.yml, this verifies PATH.
        run_cmd([self.CLI_BIN, "--version"])

    def run_review(
        self,
        *,
        pr_context: PRContext,
        review_instructions: str,
        workspace: Path,
        output_dir: Path,
    ) -> ReviewResult:
        findings_path: Path = output_dir / FINDINGS_JSON_REL
        findings_path.parent.mkdir(parents=True, exist_ok=True)
        instructions: str = write_findings_prompt_directive(
            review_instructions, findings_path
        )
        backup = _swap_mcp_config(self.mcp_config_file, self.MCP_DEST)
        try:
            argv: list[str] = [
                self.CLI_BIN,
                # TODO(provider-implementer): confirm the CLI's flags for:
                #   - user prompt (positional? -p? stdin?)
                #   - model selection
                #   - non-interactive/headless mode
                #   - workspace / cwd
            ]
            if self.model and self.model != "auto":
                argv += ["--model", self.model]
            if self.extra_args:
                argv += shlex.split(self.extra_args)
            env: dict[str, str] = _build_cli_env(
                extra_vars={"<VENDOR>_API_KEY": self.api_key}
            )
            return _invoke_cli_agent(
                argv=argv,
                workspace=workspace,
                findings_path=findings_path,
                env=env,
                cli_name=self.CLI_NAME,
            )
        finally:
            _restore_mcp_config(backup, self.MCP_DEST)
```

### 3. Add to `DEFAULT_MODELS`

```python
DEFAULT_MODELS: dict[str, str] = {
    ...,
    "<provider_id>": "<default_model_or_auto>",
}
```

### 4. Register in `build_provider()`

```python
if provider_id == "<provider_id>":
    return <Name>Provider(
        api_key=api_key,
        model=model,
        extra_args=extra_args,
        mcp_config_file=mcp_config_file,
    )
```

### 5. Add install step in `action.yml`

Guarded by `if: inputs.provider == '<provider_id>'`. Follow the shape of the existing Claude Code / Cursor / Codex / Grok install steps — set up Node if needed, npm-install (or curl-installer) the CLI, honour the optional `<provider_id>-version` input, and **skip the installer when the binary is already on `PATH`** (pre-provisioned runners; see the `grok` step). Credential goes to the CLI through `_build_cli_env(extra_vars={...})` only — one env var the CLI needs, nothing else; temp files (prompt/config) live in a `tempfile.mkdtemp()` dir, `0o600`, removed in `finally`; stdout parsers scan a bounded tail. Add the lane to `docs/SECURITY.md § "Credential lanes"`.

### 6. Add the `<provider_id>-version` input in `action.yml`

```yaml
  <provider_id>-version:
    description: 'Optional version pin for the <provider_name> CLI. Empty = latest.'
    required: false
    default: ''
```

Forward via `AIPRR_<PROVIDER_ID>_VERSION` if the runtime needs it (usually not — the install step consumes it directly).

### 7. Update `docs/PROVIDERS.md`

Add an entry to the Agent Runner Provider Contract section — CLI binary name, MCP destination, install command, any provider-specific gotchas.

### 8. Update `README.md`

- Provider roadmap table (`Family: agent-runner`).
- Inputs table (the new version-pin input).

### 9. Update `.github/workflows/self-review.yml`

Add a matrix leg for the new provider so dogfooding covers it.

### 10. Update `.github/workflows/code_check.yml`

Add a matrix leg to the `cli-install-smoke` job so installer drift is caught in CI.

### 11. Add an example workflow

`examples/provider-<provider_id>.yml` — copy-paste consumer setup.

### 12. Update `CHANGELOG.md`

```markdown
### Added
- `<provider_id>` agent-runner provider — shells out to the `<cli_bin>` CLI;
  findings via `.aiprr/findings.json`. Default model: `<default_model>`.
  See docs/PROVIDERS.md.
```

## Output to user

After running, summarise what was scaffolded:

- Files modified:
  - Chat-completions: `scripts/reviewer.py`, `action.yml` (if inputs added), `docs/PROVIDERS.md`, `README.md`, `CHANGELOG.md`.
  - Agent-runner: same as above **plus** `.github/workflows/self-review.yml`, `.github/workflows/code_check.yml`, `examples/provider-<provider_id>.yml`.
- The `# TODO(provider-implementer)` markers placed in the new class.
- Pointers to `.agents/agents/provider-implementer.md` for the next step.

Remind the user that the scaffold currently raises `NotImplementedError` (chat-completions) or has TODO-marked CLI flags to verify (agent-runner) — it doesn't ship until the translation / CLI wiring is implemented and smoke-tested.

## Quality gates after scaffolding

- [ ] `python3 -m py_compile scripts/reviewer.py` passes.
- [ ] `action.yml` parses (`python3 -c 'import yaml; yaml.safe_load(open("action.yml"))'` — CI-only tool, not runtime).
- [ ] No new non-stdlib **runtime** imports introduced.
- [ ] All TODO markers reference `provider-implementer` (so they're discoverable via `grep`).
- [ ] Agent-runner scaffolds: `_build_cli_env` used (not `{**os.environ, ...}`), `shlex.split(extra_args)` used (not string-concat), `_invoke_cli_agent` used (not bare `subprocess.run`).
