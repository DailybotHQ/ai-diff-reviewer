# AI Agent Onboarding

You're an AI coding agent (Claude Code, Cursor, Codex, Gemini, Copilot, OpenClaw, etc.) about to make a change to this repository. This is the fastest path from "where do I look first" to "shipping a sound PR".

## TL;DR

1. **Read [`AGENTS.md`](../AGENTS.md)** end-to-end. It's the source of truth.
2. **Read the file you're changing.** Don't write before you've read.
3. **Use the patterns that already exist.** This repo is small and consistent; copy the surrounding code.
4. **Compile-check before you commit:** `python3 -m py_compile scripts/reviewer.py`.
5. **Update docs in the same PR** if you changed behaviour.

## What this repo is

An LLM-driven code-review system that ships on **two surfaces from the same codebase**:

1. **A GitHub Action** — composite action, `action.yml` + `scripts/reviewer.py` (~4000 LOC, stdlib only). Distributed via the GitHub Marketplace. This is what most of the runtime code implements.
2. **A local companion skill** — `skills/ai-diff-reviewer/`, distributed via `npx skills add`. The skill runs the SAME review methodology inside the developer's coding agent (Cursor, Claude Code, Codex, Gemini, Copilot, Cline, Windsurf) using the same `prompts/default.md`. See [ARCHITECTURE.md § "Two surfaces, one methodology"](ARCHITECTURE.md#two-surfaces-one-methodology).

**Both surfaces are equally important.** If you're changing the review prompt, the severity model, or anything that affects how the reviewer sees a diff, your change affects both surfaces — the `Skills — prompt-sync invariant` CI job in `code_check.yml` enforces this.

As of v1.1.0 the runtime ships with **four providers across two families**:

- **Chat-completions family** (this action drives the tool-use loop): `anthropic`.
- **Agent-runner family** (vendor CLI drives the loop; findings return via `.aiprr/findings.json`): `claude-code`, `cursor`, `codex`.

The two families converge on a shared `ReviewResult` payload before submission, so downstream behaviour (severity gating, 422 fallback, tracking comment) is identical.

The product name is **"AI Diff Reviewer"** (capitalised exactly that way). The git repo slug is `DailybotHQ/ai-diff-reviewer` (renamed 2026-07-14; the old `ai-pr-reviewer` URL still resolves via GitHub's permanent 301 redirect for existing `@v1` pins). The internal env-var prefix is `AIPRR_` (private contract, unchanged across the rename).

## What's invariant

If you find yourself wanting to change any of the following, **stop and open an issue first**:

1. **Stdlib-only runtime.** No `requirements.txt`, no `pip install`. PRs that import a non-stdlib module in `scripts/reviewer.py` get rejected.
2. **Single-file runtime.** Everything in `scripts/reviewer.py`. Don't split into modules without a serious architectural reason.
3. **Composite action.** Don't switch to Docker or JS.
4. **`action.yml` input/output names.** Renaming or removing an input is a major-version break.
5. **The `AIPRR_` env-var prefix.** It's referenced in local-dev docs and `CONTRIBUTING.md`.
6. **The `<!-- ai-pr-reviewer-marker -->` marker constant.** Downstream consumers of the tracking comment depend on it.
7. **English-only artefacts.** Code, comments, docs, commit messages.

## What's a public contract

Treat as immutable unless you're cutting a major version:

- All keys under `inputs:` and `outputs:` in `action.yml`.
- Their default values.
- The exit codes (`0` = success, `1` = hard failure, `2` = strictness blocked).
- The `Provider.complete()` signature and Anthropic-shaped response contract (chat-completions family).
- The `AgentRunnerProvider.run_review()` signature and the `.aiprr/findings.json` schema documented in [PROVIDERS.md](PROVIDERS.md#agent-runner-provider-contract-v110) (agent-runner family). The schema is versioned; add fields backwards-compatibly rather than reshaping existing ones.
- The marker string (above).
- The action name and description in `action.yml` (Marketplace-visible).

## What's freely editable

You can change any of these without ceremony as long as compile-check passes and docs stay in sync:

- Internal helper functions in `scripts/reviewer.py`.
- The bundled default prompt at `prompts/default.md` (but include a before/after PR comparison).
- Constants at the top of `scripts/reviewer.py` (caps, timeouts, retry delays) — but justify the change in the commit message.
- Examples in `examples/`.
- All docs in `docs/`.
- CI workflows in `.github/workflows/`.

## Where to read first, by task

| Task | Read first |
|---|---|
| Add a new `action.yml` input | `action.yml` (existing patterns), README's input table, `STANDARDS.md` (naming) |
| Add a new tool to the agentic loop (chat-completions family) | `tools_schema()` and the `tool_*` functions in `scripts/reviewer.py` |
| Add a new chat-completions provider (OpenAI, Gemini, Bedrock) | `Provider` class + `AnthropicProvider` + `build_provider()` in `scripts/reviewer.py`, `.agents/agents/provider-implementer.md`, then `docs/PROVIDERS.md` |
| Add a new agent-runner provider (a new CLI) | `AgentRunnerProvider` + `ClaudeCodeProvider` (as reference) + `_invoke_cli_agent` + `_build_cli_env` in `scripts/reviewer.py`, the `AgentRunnerProviderContractTests` in `tests/test_agent_runner_providers.py`, then `docs/PROVIDERS.md#agent-runner-provider-contract-v110` |
| Tune the default prompt | `prompts/default.md`, then `docs/PROMPTS.md` for context (note the layered-prompt semantics for the agent-runner family) — the byte-copy at `skills/ai-diff-reviewer/prompt.md` gets re-synced by CI on release, but PRs that only touch `prompts/default.md` will still be flagged by the `Skills — prompt-sync invariant` job until they update the copy too |
| Change the local companion skill | `skills/ai-diff-reviewer/**/SKILL.md` (source of truth) — never `.agents/skills/ai-diff-reviewer/**` (that's the released-version snapshot, refreshed by `auto-release.yml` Step 3.5) |
| Add or change an `action.yml` input's description | `action.yml`, then README's inputs table, then `skills/ai-diff-reviewer/setup/reference.md` (the reference manual the setup sub-skill uses to answer *"what does `strictness` do?"* — all three must agree) |
| Change strictness behaviour | `evaluate_strictness()` and `overall_severity()` in `scripts/reviewer.py`, then `docs/STRICTNESS.md` |
| Fix a bug | The function that has the bug; then look for similar patterns nearby; add a regression test under `tests/` |
| Touch CI | The relevant file in `.github/workflows/`, then `docs/TESTING_GUIDE.md` |
| Add a doc | `docs/DOCUMENTATION_GUIDE.md` first — it tells you the audience map |

## What to look for in code review

When you're reviewing your own PR before pushing (or someone else's):

- Does it add a non-stdlib import? Reject.
- Does it rename or remove an `action.yml` input/output? Major-version-break flag.
- Does it pass user/model-supplied paths through `safe_repo_path()`? Required.
- Does it use `subprocess.run` with `shell=True`? Reject.
- Does it call a vendor CLI without going through `_invoke_cli_agent()`, or without scrubbing the environment via `_build_cli_env()`? Reject.
- Does it `.split()` an `agent-extra-args` string on whitespace rather than `shlex.split()`? Reject — that's the injection-vector footgun the helper exists to prevent.
- Does it have a broad `except Exception` without `# noqa: BLE001` and a justifying comment? Fix.
- Does it log a value that might be a secret? Run it through `redact_for_log()` if it's a tool arg, or remove the log entirely.
- Does the diff change runtime behaviour without a `CHANGELOG.md` entry? Add the entry.
- Does the diff add a new input without a row in the README's table? Add the row.
- Does the diff modify the `.aiprr/findings.json` schema? Confirm forward-compat: parse_findings_file must accept older payloads without raising. Add tests to `tests/test_findings_parser.py`.

## Pre-PR checklist

The same checklist as in `AGENTS.md`:

- [ ] All code in English with type hints.
- [ ] No new non-stdlib imports.
- [ ] `python3 -m py_compile scripts/reviewer.py` passes.
- [ ] `python3 -m unittest discover -s tests` passes.
- [ ] `action.yml` parses.
- [ ] If `action.yml` inputs/outputs changed: README's tables updated.
- [ ] If runtime behaviour changed: `CHANGELOG.md` entry under `[Unreleased]`.
- [ ] If a new input was added: there's an example in `examples/` **and** a row in `skills/ai-diff-reviewer/setup/reference.md`.
- [ ] If the default prompt changed: `skills/ai-diff-reviewer/prompt.md` re-synced (else the `Skills — prompt-sync invariant` job fails), plus a before/after on a real PR linked in the PR description.
- [ ] If a new agent-runner provider was added: `cli-install-smoke` matrix updated with a new leg.
- [ ] No new files at `.claude/...` or `CLAUDE.md` — those are symlinks.
- [ ] Commit follows Conventional Commits.
- [ ] `.github/workflows/self-review.yml` ran successfully across all applicable legs on the PR.

## When you don't know

The pattern, when uncertain:

1. Look for a similar pattern already in `scripts/reviewer.py` and copy it.
2. If no pattern exists, ask in a draft PR or an issue before committing — the maintainers care about consistency more than speed for new patterns.

The runtime is small. You can read `scripts/reviewer.py` in an hour. Do that before reaching for tools or asking; the answer is usually already in the code.

The skill pack (`skills/ai-diff-reviewer/`) is smaller — four `SKILL.md` files + a `reference.md` + a `prompt.md`. Read those to understand the local-companion surface before touching it.
