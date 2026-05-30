# AI Agent Onboarding

You're an AI coding agent (Claude Code, Cursor, Codex, Gemini, Copilot, OpenClaw, etc.) about to make a change to this repository. This is the fastest path from "where do I look first" to "shipping a sound PR".

## TL;DR

1. **Read [`AGENTS.md`](../AGENTS.md)** end-to-end. It's the source of truth.
2. **Read the file you're changing.** Don't write before you've read.
3. **Use the patterns that already exist.** This repo is small and consistent; copy the surrounding code.
4. **Compile-check before you commit:** `python3 -m py_compile scripts/reviewer.py`.
5. **Update docs in the same PR** if you changed behaviour.

## What this repo is

A composite GitHub Action that runs an LLM-driven code review on pull requests. Single Python script (`scripts/reviewer.py`, ~1500 LOC), stdlib only. Distributed via the GitHub Marketplace.

The product name is **"AI PR Reviewer"** (capitalised exactly that way). The repo slug is `ai-pr-reviewer`. The internal env-var prefix is `AIPRR_`.

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
- The `Provider.complete()` signature and Anthropic-shaped response contract.
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
| Add a new tool to the agentic loop | `tools_schema()` and the `tool_*` functions in `scripts/reviewer.py` |
| Add a new provider | `Provider` class + `AnthropicProvider` + `build_provider()` in `scripts/reviewer.py`, then `docs/PROVIDERS.md` |
| Tune the default prompt | `prompts/default.md`, then `docs/PROMPTS.md` for context |
| Change strictness behaviour | `evaluate_strictness()` and `overall_severity()` in `scripts/reviewer.py`, then `docs/STRICTNESS.md` |
| Fix a bug | The function that has the bug; then look for similar patterns nearby |
| Touch CI | The relevant file in `.github/workflows/`, then `docs/TESTING_GUIDE.md` |
| Add a doc | `docs/DOCUMENTATION_GUIDE.md` first — it tells you the audience map |

## What to look for in code review

When you're reviewing your own PR before pushing (or someone else's):

- Does it add a non-stdlib import? Reject.
- Does it rename or remove an `action.yml` input/output? Major-version-break flag.
- Does it pass user/model-supplied paths through `safe_repo_path()`? Required.
- Does it use `subprocess.run` with `shell=True`? Reject.
- Does it have a broad `except Exception` without `# noqa: BLE001` and a justifying comment? Fix.
- Does it log a value that might be a secret? Run it through `redact_for_log()` if it's a tool arg, or remove the log entirely.
- Does the diff change runtime behaviour without a `CHANGELOG.md` entry? Add the entry.
- Does the diff add a new input without a row in the README's table? Add the row.

## Pre-PR checklist

The same checklist as in `AGENTS.md`:

- [ ] All code in English with type hints.
- [ ] No new non-stdlib imports.
- [ ] `python3 -m py_compile scripts/reviewer.py` passes.
- [ ] `action.yml` parses.
- [ ] If `action.yml` inputs/outputs changed: README's tables updated.
- [ ] If runtime behaviour changed: `CHANGELOG.md` entry under `[Unreleased]`.
- [ ] If a new input was added: there's an example in `examples/`.
- [ ] If the default prompt changed: a before/after on a real PR linked in the PR description.
- [ ] No new files at `.claude/...` or `CLAUDE.md` — those are symlinks.
- [ ] Commit follows Conventional Commits.
- [ ] `.github/workflows/self-review.yml` ran successfully on the PR.

## When you don't know

The pattern, when uncertain:

1. Look for a similar pattern already in `scripts/reviewer.py` and copy it.
2. If no pattern exists, ask in a draft PR or an issue before committing — the maintainers care about consistency more than speed for new patterns.

The repo is small. You can read the entire runtime in 30 minutes. Do that before reaching for tools or asking; the answer is usually already in the code.
