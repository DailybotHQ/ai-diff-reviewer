# `scripts/`

**The runtime.** This directory holds `reviewer.py` — the *single source file* that implements the entire AI PR Reviewer action, and the module `.github/workflows/*` invoke through the composite step defined in [`../action.yml`](../action.yml).

## Contents

| File | Purpose |
|---|---|
| [`reviewer.py`](reviewer.py) | ~1600 lines of stdlib-only Python 3.10+. All runtime logic — provider abstraction, agentic loop, tool implementations, GitHub API, review submission, strictness gate. |

## The load-bearing constraint

`reviewer.py` **MUST** import from the Python standard library only. No `requests`, no `pyyaml` at runtime, no `httpx`, no `anthropic-sdk`. See [`../AGENTS.md`](../AGENTS.md) Rule #2 for the "why" — every added dependency becomes a supply-chain question for every consumer of the action, and there is no install phase to bring one in. HTTP is done with `urllib.request`, JSON with `json`, subprocess with `subprocess`, and so on.

CI tooling (linter, tests, `action.yml` validator) MAY use third-party packages; the **runtime** is the line.

## How to work on it

1. **Read** [`../docs/DEVELOPMENT_GUIDELINES.md`](../docs/DEVELOPMENT_GUIDELINES.md) for the Python-specific rules that apply to this file.
2. **Read** [`../docs/ARCHITECTURE.md`](../docs/ARCHITECTURE.md) for the runtime shape (the five tools, the agentic loop, the review-submission flow, the 422 fallback).
3. **Type hints on every function** — parameters, return types, and meaningful locals ([`../AGENTS.md`](../AGENTS.md) Rule #3).
4. **Compile-check before every commit:**
   ```bash
   python3 -m py_compile scripts/reviewer.py
   ```
5. **Run the unit tests** — they cover parsing, redaction, truncation, severity, strictness, path sandboxing, tool handlers, and the conversation-pruning invariant:
   ```bash
   python3 -m unittest discover -s tests -v
   ```
6. **Read** [`../docs/PERFORMANCE.md`](../docs/PERFORMANCE.md) before touching any of the top-of-file constants (`DEFAULT_MAX_TURNS`, `ANTHROPIC_MAX_TOKENS`, `MAX_*` caps). Raising them shifts cost per review; document the delta.

## Constants live at the top

Every magic number is a named constant at the top of `reviewer.py` — model defaults, turn/token caps, tool guardrails, timeouts, log-redaction substrings, severity ranks, the review marker. Never inline a numeric literal in the body; add a constant instead ([`../AGENTS.md`](../AGENTS.md) DON'T #11).

## Dogfooding

Every change to this file is reviewed by the action itself via [`../.github/workflows/self-review.yml`](../.github/workflows/self-review.yml). Any change to the agentic loop, prompt, or submission path MUST pass self-review on the PR that introduces it ([`../AGENTS.md`](../AGENTS.md) Rule #10).
