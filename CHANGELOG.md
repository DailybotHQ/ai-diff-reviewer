# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [1.3.0] — 2026-07-14

**Headline:** the "safe-for-open-source" release. Public-repo abuse defense (new [`author-association`](#author-association-gate-decision-table) gate, defaults ON), deterministic cost defaults for the CLI providers, Claude Code accepts a subscription OAuth token as `api-key`, Codex CLI 0.122+ auth breakage re-fixed, and the Marketplace listing goes live at [`github.com/marketplace/actions/ai-pr-reviewer`](https://github.com/marketplace/actions/ai-pr-reviewer). Consumers pinning `@v1` pick everything up automatically.

### Upgrade guide

The only behavioural change on upgrade is the new [`author-association`](#author-association-gate-decision-table) default. Public-repo consumers get safer defaults for free — private / internal teams that want to keep reviewing every PR must add one line.

| Consumer scenario | Action to take on upgrade |
|---|---|
| Public open-source repo (default) | **Nothing** — safer defaults protect your provider budget. Optionally add [`examples/open-source-safe.yml`](examples/open-source-safe.yml) for the full 3-gate hardening. |
| Private / internal repo, want to review every PR | Add `author-association: ''` (empty) to your workflow inputs to restore v1.2.x behaviour. |
| Public repo, want CONTRIBUTOR reviews too | Set `author-association: 'OWNER,MEMBER,COLLABORATOR,CONTRIBUTOR'`. |
| Strictest — org-members only, block collaborators | Set `author-association: 'OWNER,MEMBER'`. |
| Cost-conscious (smoke-tier model) | Pin `model: claude-haiku-4-5` (Anthropic/Claude Code) or `model: gpt-5.4-mini` (Codex). |
| Claude Pro/Max subscription instead of metered API | Run `claude setup-token` locally, store the `sk-ant-oat…` token as a secret, pass it as `api-key` (see [`docs/PROVIDERS.md` § "Billing Claude Code against a subscription"](docs/PROVIDERS.md)). |

<a id="author-association-gate-decision-table"></a>

**`author-association` gate — recommended value per repo type:**

| Repo type | Recommended value | Rationale |
|---|---|---|
| Public open-source (default) | `OWNER,MEMBER,COLLABORATOR` | Safe default. Blocks external contributors' PRs before any LLM call — closes the LLM-budget-abuse vector where an attacker opens N PRs to burn your provider tokens. |
| Public + selective external | `OWNER,MEMBER,COLLABORATOR,CONTRIBUTOR` | Adds returning contributors (anyone with a merged PR in the repo's history). |
| Private / internal team | `''` (empty — gate disabled) | Every PR is trusted; the abuse vector doesn't apply. |
| Security-critical / regulated | `OWNER,MEMBER` | Strictest. Collaborators (invited-but-not-org-members) are also gated out. |
| Fork-heavy monorepo | `OWNER,MEMBER,COLLABORATOR` **+** `permissions: pull-requests: write` on trusted-fork workflow only | Combine with [`pull_request_target`](docs/SECURITY.md) hardening. |

Full threat model + per-value semantics: [`docs/SECURITY.md` § "Author-association gate"](docs/SECURITY.md).

### Added
- **Claude Code subscription auth** — `provider: claude-code` now accepts a Claude Pro/Max OAuth token as `api-key`, parallel to Cursor's subscription model. Run `claude setup-token` on a logged-in machine, store the `sk-ant-oat…` token as a secret, and the action detects the prefix and forwards it as `CLAUDE_CODE_OAUTH_TOKEN` (subscription billing). Normal `sk-ant-api…` keys still forward as `ANTHROPIC_API_KEY` (metered) — no new input. **Security caveat:** subscription tokens grant broader account access than a scoped key; use only with `persist-credentials: false` on non-fork PRs. Codex has no clean CI equivalent (its ChatGPT-mode OAuth flow is interactive with rotating tokens and likely violates OpenAI's automation terms), so it stays on API-key auth. See [`docs/PROVIDERS.md` § "Billing Claude Code against a subscription"](docs/PROVIDERS.md).
- **New [`author-association`](#author-association-gate-decision-table) input** — comma-separated whitelist of GitHub `pull_request.author_association` values allowed to trigger a review. Defaults to `OWNER,MEMBER,COLLABORATOR` (the safe baseline for public repos). Reads a webhook-payload field the PR author cannot spoof and short-circuits *before* any LLM API call. Composes AND-style with `label-gate` and `trigger-mode` and is evaluated *first* (cheapest gate). Case- and whitespace-insensitive; empty string disables the gate. See [`docs/SECURITY.md` § "Author-association gate"](docs/SECURITY.md) and the ready-to-copy [`examples/open-source-safe.yml`](examples/open-source-safe.yml).

### Fixed
- **Agent-runner recovery from malformed `findings.json`.** PR #11 exposed the failure with `codex-cli 0.144.4`: Codex completed the review and wrote a useful Markdown summary, but one inline-finding string was invalid JSON, so the parser raised `Malformed findings.json` and the job failed. The subprocess boundary now enables an explicit summary-only fallback — malformed finding objects are dropped, the recovered summary is posted with a note, the run logs a warning. Direct parser calls remain strict by default.
- **`provider: codex` — 401 Missing bearer / basic authentication.** Codex CLI 0.122+ stopped reading `OPENAI_API_KEY` from the environment and now reads credentials **only** from `$CODEX_HOME/auth.json`. `codex-cli 0.144.3` (currently on npm) hit this breakage: every `codex exec` reached `api.openai.com/v1/responses` with an empty `Authorization` header and 401'd. `CodexProvider` now materializes an apikey-mode `auth.json` (`{"OPENAI_API_KEY": "..."}`) in an isolated per-run `CODEX_HOME` (`tempfile.mkdtemp(prefix="aiprr-codex-")`, mode `0700`, file mode `0600`) before each invocation and removes the whole tempdir in a `finally` block after. `OPENAI_API_KEY` continues to be forwarded for back-compat with Codex < 0.122. Regression tests in `CodexAuthJsonTests` cover env forwarding, on-disk file shape, permission modes, and cleanup. See [`docs/PROVIDERS.md` § "Codex auth model (0.122+ requires `$CODEX_HOME/auth.json`)"](docs/PROVIDERS.md).
- **`provider: codex` no longer copies ignored MCP JSON config.** After switching Codex auth to an isolated per-run `CODEX_HOME`, the old `mcp-config-file` copy still targeted `~/.codex/mcp.json`, which the subprocess ignored and Codex does not read anyway (`config.toml` is the supported path). The Codex provider now warns without copying the ignored JSON file; use `agent-extra-args` / `config.toml` for Codex MCP setup.

### Changed
- **Marketplace listing renamed back to "AI PR Reviewer"** (`action.yml` `name:` reverted from the v1.2.1 `Dailybot AI PR Reviewer`). The v1.2.1 vendor prefix was a defensive over-fix — the real slug collision (`ai-pull-request-reviewer`, owned by the third-party `appchoose/ai-pr-review`) was on the *full-form* title only. The *abbreviated* title "AI PR Reviewer" slugifies to `ai-pr-reviewer`, a distinct slug that was free all along and matches this repo's own slug exactly. Marketplace URL is now [`github.com/marketplace/actions/ai-pr-reviewer`](https://github.com/marketplace/actions/ai-pr-reviewer); workflow `uses:` pins are unaffected. Vendor attribution continues via the `author: 'DailybotHQ'` field, which GitHub auto-renders as "by DailybotHQ" beneath the tile. **No consumer action required.** See [`AGENTS.md § 9`](AGENTS.md) for the naming history.
- **Default behaviour tightening (soft-breaking) — `author-association: OWNER,MEMBER,COLLABORATOR`.** External-contributor PRs (`author_association` = `CONTRIBUTOR`, `FIRST_TIME_CONTRIBUTOR`, `FIRST_TIMER`, `NONE`) are **no longer reviewed automatically** after upgrading. Public-repo consumers get safer defaults for free; consumers who want v1.2.x behaviour set `author-association: ''`. The SemVer minor bump reflects that the behavioural change is opt-out. See the [Upgrade guide](#upgrade-guide) above for per-repo-type guidance.
- **Explicit, quality-tier default models for the CLI providers.** The CLI providers no longer default to `auto` (which deferred to the account default and could silently be Opus at ≈$5/$25). The action now pins an explicit quality-tier model per provider: **`claude-code` → `claude-sonnet-4-6`** (quality/price sweet spot); **`codex` → `gpt-5.6-luna`** (≈$1/$6 per 1M tokens; current-gen budget model, replaces the now-deprecated `gpt-5-codex` at ≈$1.75/$14). The `anthropic` default stays `claude-sonnet-4-6` and Cursor stays `auto` (flat-rate/unlimited on Pro). Consumers pin a cheaper smoke model (`claude-haiku-4-5` ≈$1/$5, `gpt-5.4-mini` ≈$0.75/$4.50) via `model:`. See [`docs/PROVIDERS.md` § "Choosing a cost-efficient model"](docs/PROVIDERS.md).
- **Label matching is now case-insensitive.** `label-gate` (and its `label-once` / `label-added-only` trigger logic) compares label names on a lowercased, whitespace-trimmed basis — `label-gate: ready` is satisfied by `ready`, `Ready`, or `READY`. Applies to `resolve_trigger_action`, `gh_pr_has_label`, and `count_label_events`; removes a foot-gun where a capitalized label silently failed to trigger.
- **Self-review dogfooding is now cost-scoped, model-pinned, and label-gated.** Three complementary changes bound dogfood spend: (1) the `anthropic` leg is the always-on smoke baseline with `max-turns: 12` (down from the consumer default `30`); the `claude-code`, `cursor`, and `codex` legs only fire when the diff touches provider-sensitive surfaces (`action.yml`, `scripts/reviewer.py`, prompts, core workflow files, or provider/runtime tests); (2) each leg pins an explicit cheap model — Anthropic and Claude Code on `claude-haiku-4-5`, Codex on `gpt-5.4-mini`, Cursor on `auto` (unlimited on Pro); (3) the whole dogfood is gated on a `ready` label + `trigger-mode: label-once`, so the maintainer holds explicit control of dogfood spend. Routine docs/README PRs stay cheap while full provider parity is still exercised on the changes that can realistically break it.

## [1.2.1] — 2026-07-14

**Headline:** the "actually-works-on-Marketplace" release — renames the Marketplace listing to unblock the first-time publish (a squatting `appchoose/ai-pr-review` action already owns the un-prefixed slug) and ships two provider-side fix batches that landed on `main` after `v1.2.0` was tagged (`claude-code` and `codex` were both broken out of the box in `v1.2.0`; this patch is what makes those providers actually usable). Consumers pinning `@v1` pick everything up automatically.

### Changed
- **Marketplace listing renamed to "Dailybot AI PR Reviewer"** (`action.yml` `name:`). The un-prefixed name slug-ifies to `ai-pull-request-reviewer`, which is already claimed by an unrelated third-party action (`appchoose/ai-pr-review`, v1.1.5). The vendor-prefix pattern is the standard Marketplace resolution and keeps our repo slug, docs, and user-facing product copy on "AI PR Reviewer". See `AGENTS.md` § 9 (Marketplace Branding Stable). No workflow changes required — `uses: DailybotHQ/ai-pr-reviewer@v1` is unaffected.
- **Default Cursor model is now `auto`** (was `composer-2.5`). `auto` is unlimited on Cursor Pro plans and is the CI recommendation in `docs/PROVIDERS.md`; the default now matches the docs. Pin `composer-2.5` (or any specific model) via `model:` if you want to force one.
- **`collapse-previous` is now scoped per provider.** Every review body and tracking comment carries an invisible `<!-- ai-pr-reviewer-provider: <id> -->` marker, and `collapse-previous` only minimizes *this provider's own* prior artefacts. Effects: (1) several providers can review the same PR concurrently — even sharing one `GITHUB_TOKEN` — without collapsing each other (`self-review.yml`'s four-provider matrix keeps the default `true` and relies on the scoping); (2) unrelated `github-actions[bot]` comments (coverage bots, labelers) are no longer collapsed. See `docs/PROVIDERS.md` § "Running more than one provider on the same PR". Transition: a single pre-upgrade review without the marker won't be auto-collapsed on the first run after upgrading.
- **`agent-max-turns` now warns instead of silently doing nothing** for the CLI providers. None of the shipping CLIs (Claude Code, Cursor, Codex) expose a turn-count cap flag, so the input can't be forwarded; the run logs a clear warning pointing at `agent-extra-args` and noting the `CLI_INVOCATION_TIMEOUT` (900 s) as the effective bound, rather than leaving a misleading dead input.

### Fixed
- **`provider: claude-code` and `provider: codex` now actually produce reviews.** Both were broken out of the box and failed on essentially every PR:
  - **Claude Code** received its review rubric + `findings.json` output contract as a literal *file path* (`--append-system-prompt <path>`) instead of text, so the instructions never reached the model — it was never told to write findings and the run failed with `FileNotFoundError`. Now the instructions are passed as text via `--append-system-prompt`.
  - **Claude Code** ran in the default headless permission mode, which denies the `Write` tool in non-interactive CI, so it could not emit `findings.json` even when instructed. Now invoked with `--permission-mode bypassPermissions` (the runner is already an isolated ephemeral sandbox; mirrors Cursor's `--force --trust`).
  - **Codex** ran `codex exec` in its default read-only sandbox and physically could not write `findings.json`. Now invoked with `--dangerously-bypass-approvals-and-sandbox` (documented for externally-sandboxed CI environments).
- **Large PRs no longer crash `claude-code` / `codex` with `E2BIG`.** Both embedded the full diff (up to 200 KB) in a single argv argument, exceeding the Linux ~128 KB per-argument limit. The prompt is now piped via stdin (`claude -p` reads stdin; `codex exec -`), matching the fix Cursor already had.
- **Agent-runner prompt hygiene.** The user prompt handed to the CLI providers referenced the chat-completions-only tools `post_inline_comment` / `submit_review`, which don't exist for a vendor CLI. Agent-runner providers now get a tailored prompt that points at the `findings.json` output contract instead of contradictory tool names.
- **Claude Code MCP passthrough now takes effect.** `mcp-config-file` was copied to `~/.claude/mcp.json`, which Claude Code does not read — the passthrough silently did nothing. The CLI is now invoked with `--mcp-config <file>` pointing at the consumer's config.
- **Codex MCP passthrough now warns instead of silently no-op'ing.** Codex configures MCP via `~/.codex/config.toml`, not a JSON file, so `mcp-config-file` never took effect for `provider: codex`. The run now logs a clear warning pointing at `agent-extra-args` / `config.toml` instead of pretending it worked. (Full Codex MCP support is a documented follow-up.)
- **Vendor CLIs now inherit proxy and custom-endpoint config.** `_build_cli_env` forwards `HTTP(S)_PROXY` / `NO_PROXY` and `ANTHROPIC_BASE_URL` / `OPENAI_BASE_URL` (non-secret network config) so agent-runner providers work on proxied / self-hosted runners and against compatible gateways.

### Security
- **`docs/SECURITY.md`** now documents the real exfiltration surface of the agent-runner providers (vendor API key in the CLI subprocess env + `GITHUB_TOKEN` persisted by `actions/checkout` in `.git/config`, both reachable by an injected CLI) and corrects the prior blast-radius claim, which only held for `provider: anthropic`. Recommends running agent-runner providers on trusted/non-fork PRs only and setting `persist-credentials: false`.
- **Runtime secret scrubbing.** The provider API key and GitHub token are registered as literal secret values (`register_secret`) and scrubbed (`scrub_secrets`) from the review summary, every inline-comment body, and any failure message before it is posted to the PR — a defense-in-depth backstop against a prompt-injected vendor CLI echoing its key into a public comment.

## [1.2.0] — 2026-07-11

**Headline:** the "configurable review workflow" release — five new inputs that let consumers control when the review fires, how the prompt is composed, whether the PR description is auto-completed, whether complexity labels are applied, and a fourth strictness tier for zero-tolerance stacks. Every knob is additive and opt-in; consumers on `@v1` see zero behavioural drift.

### Added
- **New `trigger-mode` input** with four values: `always`, `label-required`, `label-once`, `label-added-only`. Enables precise control over when the reviewer runs — including a "review once per label application" workflow where re-running requires toggling the label off and on. See [`docs/TRIGGER_MODES.md`](docs/TRIGGER_MODES.md).
- New helpers `count_label_events`, `resolve_trigger_action`, `read_trigger_state`, `write_trigger_state`. Marker state (a JSON blob in an HTML comment inside the tracking comment) carries the `label_toggle_generation` counter that powers `label-once`.
- **New `pr-description-mode` input** with four values: `off` (default), `warn`, `block`, `autocomplete`. When `autocomplete` is used, the AI writes a first-draft PR body when the current body is missing or too vague. Guarded by a marker so it never overwrites maintainer edits. See [`docs/PR_METADATA_CHECKS.md`](docs/PR_METADATA_CHECKS.md).
- **New `pr-description-min-length` input** (default `50`) — character threshold below which the body is treated as "missing/vague."
- **New `complexity-labels-enabled` input** — when `true`, the reviewer assesses PR complexity (`low`/`medium`/`high`) based on cognitive load, files touched, security surface, and coverage delta, then applies a `complexity:*` label.
- **New `complexity-label-prefix` input** (default `complexity:`) — configurable prefix for the applied complexity label.
- **New `set_pr_description` and `set_pr_complexity` tools** in the chat-completions tool schema, gated by the new inputs (exposed only when the corresponding feature is enabled).
- New GitHub API surface used: `PATCH /pulls/{n}` (autocomplete) and `DELETE /issues/{n}/labels/<name>` (complexity relabelling). See [`docs/SECURITY.md`](docs/SECURITY.md) § "PR metadata PATCH surface" for the threat model.
- **New `prompt-extension-file` input** — APPENDS content to the base system prompt (either the bundled default or a custom `prompt-file`) with a `---` separator. Layer stack-specific severity overrides and house rules without copy-pasting the entire default. Three starter extensions ship in `examples/prompts/` (`python-strict.md`, `typescript-strict.md`, `security-focused.md`).
- **Meta-prompt** at `examples/prompts/generate-custom-prompt-meta.md` — hand it to your favorite coding AI (Claude Code, Cursor, Codex, ChatGPT, Gemini) with your repo checked out, and the AI produces a repo-tailored `prompt-file`. Solves the blank-page problem for the full-replacement path.
- **New strictness mode `block-on-any`** — fails the GitHub check when the reviewer posts any inline comment, including `info`. Zero-tolerance mode for security-critical and regulated stacks. See [`docs/STRICTNESS.md`](docs/STRICTNESS.md) for the full decision tree.
- Documentation of the Cursor CLI billing model in `docs/PROVIDERS.md` (subscription-only, no BYOK, `model: auto` unlimited on Pro plans) — resolves consumer confusion about which API keys are compatible with `provider: cursor`.

### Changed
- `label-gate` semantics preserved for back-compat, now internally implemented as `trigger-mode: label-required` with `label-gate` supplying the label name. Consumers that only set `label-gate` see zero behavioural drift.
- `CursorProvider` now passes `--force --trust` by default in its headless invocation, per Cursor's own [Headless CLI docs](https://cursor.com/docs/cli/headless) recommendation for CI. Adds `--approve-mcps` conditionally when `mcp-config-file` is set, so the interactive MCP-approval prompt does not stall unattended runs. Consumers do not need to add these flags manually via `agent-extra-args`; the change is fully backward-compatible.
- `examples/provider-cursor.yml` now sets `model: auto` explicitly as the recommended CI default.
- `docs/PERFORMANCE.md` § "Two performance shapes" — added a Billing row clarifying that Cursor consumes subscription credits while other agent-runner providers use metered vendor API tokens.

### Fixed
- **CursorProvider E2BIG on large PRs.** The Cursor CLI concatenated review instructions + PR diff and passed the whole string as a positional argv token (`cursor-agent -p <200 KB…>`), which exceeded the Linux `ARG_MAX` (~128 KB) and crashed the review before the CLI could start (`OSError: [Errno 7] Argument list too long`). `_invoke_cli_agent()` now accepts an optional `stdin_input=` parameter; `CursorProvider.run_review()` pipes the prompt via stdin (`cursor-agent -p` with no positional argument), unblocking reviews of PRs whose diff alone can exceed 200 KB. Regression covered by `CursorHeadlessDefaultsTests.test_user_prompt_not_in_argv_and_goes_via_stdin`.
- Other providers (`anthropic`, `claude-code`, `codex`) were unaffected — Claude Code writes the system prompt to a file via `--append-system-prompt` and Codex's prompt shape stays under `ARG_MAX` in practice.
- **`label-added-only` no longer fires on unrelated labels.** When `label-gate` was already present on a PR and a webhook added a different label (e.g. `bug` or a Dependabot label), the workflow would still enter this action and pay for a full review. `_read_github_event_label()` now surfaces `event.label.name`; `resolve_trigger_action()` requires it to match `label-gate` in `label-added-only` mode. Consumers relying on `label-added-only` avoid stray runs and stray billing. Regression covered by `ResolveTriggerActionTests.test_label_added_only_skips_when_event_label_is_unrelated`.
- **Silent no-op on agent-runner providers now logs a `WARNING`.** Enabling `pr-description-mode: autocomplete` or `complexity-labels-enabled: true` with `provider: cursor|claude-code|codex` never populated the corresponding `state.proposed_*` fields (the tools are chat-completions-only in v1.2). Consumers used to pay for a review with nothing to show for those inputs. `main()` now emits a `WARNING:` line at run start listing which inputs will no-op, and `docs/PR_METADATA_CHECKS.md` § "Provider support matrix" documents the current split. Extracted into a testable helper `build_agent_runner_noop_warning()`.
- **`label-once` no longer skips silently when `count_label_events()` returns 0.** Previously `label_toggle_generation <= last_reviewed_generation` treated `0 <= 0` as "already reviewed" — so a transient timeline-API failure while the gate label WAS on the PR caused a silent skip with `should_run=False (already reviewed label generation 0 …)`. The check now requires `label_toggle_generation > 0` before treating a run as stale. Better to run and deliver a review than skip and deliver nothing. Regression covered by `ResolveTriggerActionTests.test_label_once_runs_when_count_zero_but_label_present`.
- **`count_label_events()` now logs a `WARNING` when the 20-page pagination cap is hit.** On long-lived, high-chatter PRs the safety bound could undercount the generation, and `label-once` re-runs would silently refuse to fire. The cap stays (cost control), but now announces itself so operators see why the mode is stuck. Documented workarounds in `docs/TRIGGER_MODES.md` § "Edge cases": toggle the label twice, or switch to `label-added-only` for that PR. Regression covered by `CountLabelEventsTests.test_logs_warning_when_pagination_cap_hit`.
- **README `prompt-extension-file` comment.** The recipe said the input was "mutually exclusive with `prompt-file`, or complementary" — the two are actually composable ("custom base + extension"). Comment rewritten to match `docs/PROMPTS.md` and `action.yml`.
- **`collapse-previous` silently failed on `${{ secrets.GITHUB_TOKEN }}`.** `gh_get_authenticated_login()` unconditionally called `GET /user`, which returns `403 Forbidden` for the built-in workflow installation token (a well-known GitHub limitation). The exception was swallowed by the outer try/except and logged as non-fatal, meaning the entire `minimizeComment` GraphQL step never ran — every consumer using the recommended `github-token: ${{ secrets.GITHUB_TOKEN }}` pattern lost the "hide previous reviews as outdated" feature since v1.0, without noticing. The function now walks a 4-tier fallback chain: (1) `/user` for PATs, (2) `/app` for GitHub App tokens (returns `<slug>[bot]`), (3) marker-scan the PR's issue comments for `<!-- ai-pr-reviewer-marker -->` and use that comment's author, (4) hardcoded default `github-actions[bot]`. Regression covered by seven `GhGetAuthenticatedLoginFallbackTests` cases across all four tiers plus the empty-login edge case. New public constant `DEFAULT_WORKFLOW_BOT_LOGIN`.
- **`collapse-previous` login-shape mismatch between REST and GraphQL.** Even with the 4-tier fallback landed, the dogfood run logged `Collapsed 0/N previous bot artefact(s)` because REST returns `.user.login = "github-actions[bot]"` while GraphQL returns `.author.login = "github-actions"` (no `[bot]` suffix) on the same Bot node. The naive equality check filtered every bot artefact out. The filter now accepts both shapes for the comparison (`bot_login` and `bot_login` stripped of the `[bot]` suffix). Regression covered by four `GhCollapsePreviousReviewsTests` cases — matches without suffix, matches with suffix, skips already-minimized nodes, ignores other bots (dependabot/renovate stay untouched).

## [1.1.0] — 2026-07-05

**Headline:** three new agent-runner providers (`claude-code`, `cursor`, `codex`) alongside the incumbent `anthropic` chat-completions provider — zero migration cost for consumers on `@v1`. See [`.dwp/plans/PLAN_multi_cli_provider_expansion/analysis_results/EXECUTIVE_REPORT.md`](.dwp/plans/PLAN_multi_cli_provider_expansion/analysis_results/EXECUTIVE_REPORT.md) for the full breakdown.

### Added
- **Multi-CLI provider expansion** — three new agent-runner providers that shell out to their vendor's coding-agent CLI in headless mode and receive findings via a file-based contract (`.aiprr/findings.json`):
  - `provider: claude-code` — installs `@anthropic-ai/claude-code` via npm; auth via `ANTHROPIC_API_KEY`.
  - `provider: cursor` — installs `cursor-agent` via `curl` (`cursor.com/install`); auth via `CURSOR_API_KEY`.
  - `provider: codex` — installs `@openai/codex` via npm; auth via `OPENAI_API_KEY`.
- New abstract `AgentRunnerProvider` peer of `Provider`. `build_provider()` now returns either family; `main()` dispatches on `isinstance`.
- New `Finding` + `ReviewResult` dataclasses provide the provider-independent submission-path payload.
- New `parse_findings_file()` parser + validator with strict schema enforcement (required fields, allowed severity/side enums, forward-compat with vendor extensions).
- New `write_findings_prompt_directive()` — standardises the "write your findings here" instruction appended to review prompts across all CLI providers.
- New optional inputs: `agent-max-turns`, `agent-extra-args`, `mcp-config-file`, `claude-code-version`, `cursor-version`, `codex-version`.
- Modular install in `action.yml`: each CLI install step is guarded by `if: inputs.provider == '...'`, so consumers picking the default `provider: anthropic` pay zero install overhead. One provider = one install.
- MCP servers passthrough: `mcp-config-file` copies the consumer's JSON config into the CLI's expected location (with round-trip backup) before invocation.
- New examples: `provider-claude-code.yml`, `provider-cursor.yml`, `provider-codex.yml`, `mcp-passthrough.yml`.
- New CI job `cli-install-smoke` — matrix over the three CLI providers exercising each installer script on a fresh runner, catching installer drift before it reaches consumers.
- Dogfooding matrix in `.github/workflows/self-review.yml` — every PR to this repo now runs a 4-leg review (`anthropic`, `claude-code`, `cursor`, `codex`) with per-provider `self-reviewed:*` labels.
- 67 new unit tests (109 total, up from 42) covering: adapter (state → ReviewResult), findings.json parser (happy + error paths), provider dispatch, MCP passthrough, subprocess boundary, security invariants (no `shell=True`, no `os.system`, all `extra_args` funnel through `shlex.split`), CLI env allowlist, and end-to-end serialization roundtrips across both provider families.

### Changed
- `gh_submit_review_with_fallback()` now accepts a `ReviewResult` (was: `body` + `inline_comments`). The submission path is provider-agnostic; findings are encoded to the GitHub Reviews inline shape at the boundary via `findings_to_gh_inline_comments()`.
- Refreshed `docs/PROVIDERS.md` with the Agent Runner Provider Contract section documenting the schema, validation, and prompt directive.
- Refreshed `docs/ARCHITECTURE.md` with the two-provider-family design decision and the modular-install approach.
- Refreshed `README.md` inputs table + provider roadmap with the four shipping providers, categorised by family.
- Refreshed `.agents/agents/provider-implementer.md`, `.agents/skills/add-provider/SKILL.md`, `.agents/agents/reviewer.md`, and `.agents/docs/skills_agents_catalog.md` for the two-family model.

### Fixed
- N/A — additive release. Existing `provider: anthropic` consumers see zero behavioural drift.

### Security
- `_invoke_cli_agent()` enforces argv-list subprocess invocation (no `shell=True`).
- All consumer-provided `agent-extra-args` are parsed with `shlex.split` before being appended to the CLI invocation.
- MCP config passthrough uses `shutil.copyfile` (not `shell=True` copy) and round-trips any pre-existing user config so an interrupted run doesn't leave stale state.
- **New `_build_cli_env(extra_vars=...)` helper** — vendor CLI subprocesses receive an explicit env allowlist (`PATH`, `HOME`, `NODE_PATH`, locale, runner metadata) plus the vendor API key only. `AIPRR_GH_TOKEN` and all other `AIPRR_*` variables stay in the parent process; enforced by static `CliEnvAllowlistTests`. Addresses Security-Review Finding #2.
- **`max-inline-comments` cap now enforced on the agent-runner path** — previously only enforced by the chat-completions tool handler. `main()` truncates `result.findings` to `max_inline_comments` after `provider.run_review()` and recomputes `overall_severity` on the retained subset. Addresses Security-Review Finding #1.
- **Documented accepted risks** in `docs/SECURITY.md`: (a) Cursor installer supply chain (`curl | bash`, no signed installer offered by vendor); (b) MCP config persistence after SIGKILL on self-hosted persistent runners.

### CI
- `code_check.yml` gains a `cli-install-smoke` matrix job (claude-code / cursor / codex).
- `self-review.yml` becomes a 4-leg matrix; `fail-fast: false` + `timeout-minutes: 25`.

## [1.0.0] — 2026-05-29

Initial public release.

### Added
- Composite GitHub Action that runs an LLM-driven code review on every pull request.
- Anthropic provider (`claude-sonnet-4-6` default), with `Provider` abstraction ready for OpenAI/Gemini drop-ins.
- Five-tool agentic loop: `read_file`, `grep`, `glob`, `post_inline_comment`, `submit_review`.
- Severity tagging (`critical` / `warning` / `info`) on every inline comment, surfaced as the `severity` action output.
- Three strictness modes (`lenient`, `block-on-critical`, `block-on-warning`) to gate the GitHub check.
- Optional `label-gate` input — only run when the PR carries a configured label.
- Optional `applied-label` input — auto-apply a label after a successful, non-blocked review (with auto-create if the label doesn't exist).
- Auto-collapse of previous bot reviews/comments via GraphQL `minimizeComment`.
- Tracking spinner comment with `<!-- ai-pr-reviewer-marker -->` marker, transitioning in-place from `Working…` to `View review →` (or `failed`).
- 422 fallback: if GitHub rejects the review because one inline comment anchored outside the diff, the action retries summary-only instead of losing every comment.
- Bundled default system prompt that's technology-agnostic and includes severity definitions.
- Bounded retries on Anthropic 429/5xx; bounded conversation pruning to keep token cost from compounding.
- Documentation: README, PROMPTS guide, STRICTNESS guide, PROVIDERS roadmap.
- Examples: `basic.yml`, `label-gated.yml`, `strict.yml`, `custom-prompt.yml`.
- `code_check` workflow gating every PR/push to `main` (compile, `action.yml`
  contract validation, actionlint, unit tests).
- `auto-release` workflow: SemVer bump from Conventional Commits on merge to
  `main`, tag + major-alias move + GitHub Release (tag-only, no commit to
  protected `main`).
- Stdlib-`unittest` test suite under `tests/` for the runtime's pure logic.
- Self-review workflow dogfooding the action on its own PRs.
- Repo hygiene: issue/PR templates and Dependabot for GitHub Actions.

[Unreleased]: https://github.com/DailybotHQ/ai-pr-reviewer/compare/v1.3.0...HEAD
[1.3.0]: https://github.com/DailybotHQ/ai-pr-reviewer/compare/v1.2.1...v1.3.0
[1.2.1]: https://github.com/DailybotHQ/ai-pr-reviewer/compare/v1.2.0...v1.2.1
[1.2.0]: https://github.com/DailybotHQ/ai-pr-reviewer/compare/v1.1.0...v1.2.0
[1.1.0]: https://github.com/DailybotHQ/ai-pr-reviewer/releases/tag/v1.1.0
[1.0.0]: https://github.com/DailybotHQ/ai-pr-reviewer/releases/tag/v1.0.0
