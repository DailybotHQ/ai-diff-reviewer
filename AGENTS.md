# AGENTS.md — Documentation for AI Agents

**Purpose:** Single source of truth for every AI coding assistant working on this repository (Claude Code, Cursor, OpenAI Codex, Google Gemini, GitHub Copilot, OpenClaw, and others). Human contributors are also welcome readers — this file is the fastest way to get oriented.

The product name in user-facing strings is **"AI PR Reviewer"** (capitalised exactly that way). The action repository slug is `ai-pr-reviewer` (lowercase, hyphenated).

---

## Detailed Documentation

| Category | Document |
|----------|----------|
| Product Spec | [docs/PRODUCT_SPEC.md](docs/PRODUCT_SPEC.md) |
| Architecture | [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) |
| Security | [docs/SECURITY.md](docs/SECURITY.md) |
| Testing | [docs/TESTING_GUIDE.md](docs/TESTING_GUIDE.md) |
| Development Commands | [docs/DEVELOPMENT_COMMANDS.md](docs/DEVELOPMENT_COMMANDS.md) |
| Python Guidelines | [docs/DEVELOPMENT_GUIDELINES.md](docs/DEVELOPMENT_GUIDELINES.md) |
| Repository Standards | [docs/STANDARDS.md](docs/STANDARDS.md) |
| Documentation Guide | [docs/DOCUMENTATION_GUIDE.md](docs/DOCUMENTATION_GUIDE.md) |
| AI Agent Onboarding | [docs/AI_AGENT_ONBOARDING.md](docs/AI_AGENT_ONBOARDING.md) |
| AI Agent Collaboration | [docs/AI_AGENT_COLLAB.md](docs/AI_AGENT_COLLAB.md) |
| PR Review Workflow | [docs/PR_REVIEW_WORKFLOW.md](docs/PR_REVIEW_WORKFLOW.md) |
| Strictness (user-facing) | [docs/STRICTNESS.md](docs/STRICTNESS.md) |
| Prompts (user-facing) | [docs/PROMPTS.md](docs/PROMPTS.md) |
| Providers (user-facing) | [docs/PROVIDERS.md](docs/PROVIDERS.md) |
| Skills & Agents Catalog | [.agents/docs/skills_agents_catalog.md](.agents/docs/skills_agents_catalog.md) |

---

## Project Overview

**AI PR Reviewer** is an LLM-driven pull-request reviewer packaged as a GitHub Action. It posts inline comments with severity tags, gates the GitHub check based on configurable strictness, applies a "reviewed" label, and collapses prior reviews — all from a single composite action with zero infrastructure.

**Stack constraints (load-bearing):**
- **Python 3.10+ standard library only.** No `requirements.txt`, no `pyproject.toml`, no virtualenv. Every dependency is a supply-chain question for every consumer.
- **Composite GitHub Action** — not Docker, not Node. The runtime is whatever Python ships with `ubuntu-latest`.
- **Single source file** for the runtime: `scripts/reviewer.py`. The simplicity is the feature.
- **Provider abstraction** for future LLM providers; today only Anthropic ships.

---

## Project Structure

```
.
├── action.yml                      # Composite-action contract (inputs/outputs/branding)
├── scripts/
│   └── reviewer.py                 # All runtime logic — stdlib only
├── prompts/
│   └── default.md                  # Bundled default system prompt (technology-agnostic)
├── examples/                       # Copy-paste workflow snippets for common setups
├── docs/                           # User-facing + contributor-facing documentation
├── .github/workflows/              # CI, release, self-review
├── .agents/                        # Canonical AI-agent configuration (symlinked from .claude)
│   ├── agents/                     # Sub-agent definitions
│   ├── commands/                   # Slash commands (commit, pr, release, prompt-test, …)
│   ├── docs/                       # Catalog + agent-targeted docs
│   ├── skills/                     # Skill definitions (release, prompt-test, add-provider, …)
│   ├── settings.json               # Agent harness settings
│   └── README.md
├── README.md                       # Marketplace-facing readme
├── AGENTS.md                       # ← you are here (source of truth)
├── CLAUDE.md                       # Symlink → AGENTS.md
├── CHANGELOG.md
├── CONTRIBUTING.md
└── LICENSE                         # MIT
```

---

## CRITICAL: Mandatory Rules

### 1. English Only

All code, comments, documentation, and commit messages MUST be in English. The action ships to a global audience; a Spanish comment in the prompt or the script becomes a usability bug for everyone outside the team.

### 2. Standard Library Only (MANDATORY)

`scripts/reviewer.py` MUST run on a vanilla `ubuntu-latest` runner with **zero** non-stdlib imports. No `requests`, no `pyyaml` at runtime, no `httpx`. This is the load-bearing constraint that lets the action stay a single composite step with no install phase. PRs that introduce a non-stdlib runtime dependency will be rejected.

CI tooling (lint, test) is allowed to use third-party packages; the runtime is the line. See [docs/DEVELOPMENT_GUIDELINES.md](docs/DEVELOPMENT_GUIDELINES.md).

### 3. Type Hints (MANDATORY)

ALL Python code in `scripts/` MUST use complete type hints — parameters, return types, and meaningful local variables. The codebase is the documentation; readers shouldn't have to infer types.

```python
# CORRECT — fully typed
def fetch_pr_context(
    *, repo: str, pr_number: int, base_ref: str, token: str
) -> PRContext:
    ...

# WRONG — never generate untyped code
def fetch_pr_context(repo, pr_number, base_ref, token):
    ...
```

### 4. Public Surface Stability (MANDATORY)

`action.yml` inputs and outputs are a **public contract**. Renaming, removing, or changing the type of an input is a breaking change that requires a major-version bump. Adding a new optional input is non-breaking.

If you must break the contract:
1. Open an issue for discussion.
2. Coordinate the rename across `action.yml`, `scripts/reviewer.py` (env-var reads), `README.md` (input table), `CHANGELOG.md`, and at least one example workflow.
3. Cut a `v2.0.0` release.

The `AIPRR_*` env-var prefix used internally by the script is a private contract — but it has bled into examples in `CONTRIBUTING.md` and `docs/DEVELOPMENT_COMMANDS.md` for local-debug instructions, so coordinate any rename there too.

### 5. Compile-Check Before Commit

Every commit that touches `scripts/reviewer.py` MUST compile cleanly:

```bash
python3 -m py_compile scripts/reviewer.py
```

CI runs this on every PR; pre-commit-checking it locally is courtesy, not optional. There is no broader test suite (the reviewer's "test" is dogfooding on real PRs — see [docs/TESTING_GUIDE.md](docs/TESTING_GUIDE.md)).

### 6. Conventional Commits (MANDATORY)

Every commit follows [Conventional Commits](https://www.conventionalcommits.org/):

```
<type>(<optional-scope>): <short description>

<optional body — Summary, Change Log, Risks>
```

Types: `feat`, `fix`, `docs`, `chore`, `refactor`, `test`, `ci`, `perf`. Scope is optional but useful for multi-file changes (`feat(provider): add OpenAI support`).

### 7. Documentation Stays in Sync

Whenever you change runtime behaviour:

- `README.md` input/output tables → update if `action.yml` changed.
- `CHANGELOG.md` → entry under `[Unreleased]`.
- `docs/STRICTNESS.md` / `PROMPTS.md` / `PROVIDERS.md` → update the section that covers the area you touched.
- `examples/` → add an example if you added an input that has a non-trivial usage pattern.
- `AGENTS.md` (this file) → update the "Critical Rules" or "DO/DON'T" sections if you change a project standard.

### 8. SemVer for Releases (MANDATORY)

Releases follow Semantic Versioning. Tags are `vX.Y.Z`. The `release.yml` workflow auto-updates the moving major tag (`v1`) on every `v1.x.y` release; consumers pinning `@v1` get patches and minor features automatically. Never delete a published tag — consumers pin to it.

### 9. Marketplace Branding Stable

`action.yml` `name`, `description`, `branding.icon`, and `branding.color` are visible in the GitHub Marketplace listing. Once published, treat them as immutable for cosmetic reasons (consumers' search results and tile UI depend on them). Editorial changes are fine; identity changes need a deliberate decision.

### 10. Dogfooding is Required

Any change that affects the agentic loop, the prompt, or the review-submission path MUST be verified by `.github/workflows/self-review.yml` running successfully on the PR that introduces the change. If the change can't be verified by self-review (e.g. it only fires on the `block-on-warning` strictness path), describe the manual verification you did in the PR description.

---

## Slash Commands

| Agent | Prefix | Example |
|-------|--------|---------|
| Claude Code | `/` | `/release` |
| Codex / Cursor / Gemini | `#` | `#release` |

Defined in [.agents/commands/](.agents/commands/). When invoked, look up the procedure file there and follow it exactly. The current set:

| Command | Purpose |
|---|---|
| `/commit` | Generate a Conventional Commits message for the current diff. |
| `/pr` | Generate a PR description from the branch's commits. |
| `/release` | Cut a new `vX.Y.Z` tag and publish a GitHub Release. |
| `/prompt-test` | Smoke-test a prompt change against a real PR. |
| `/add-provider` | Scaffold a new `Provider` implementation. |
| `/code-review` | Run a focused review on the current branch. |
| `/branch` | Generate a branch name from intent. |

---

## Skills & Agents

Reusable **Skills** (slash commands and one-shot workflows) and **Agents** (specialised personas) live in [.agents/skills/](.agents/skills/) and [.agents/agents/](.agents/agents/). The full catalog with tier classification is in [.agents/docs/skills_agents_catalog.md](.agents/docs/skills_agents_catalog.md).

### Tier Model

| Tier | Use case | Model |
|------|----------|-------|
| 1 — Light | Trivial fixes, doc edits | Haiku / cheap-fast |
| 2 — Standard | Single-file features, tests | Sonnet / standard |
| 3 — Heavy | Architecture, prompt redesign, provider implementation | Opus / frontier |

---

## Common Mistakes

### DON'T

1. Add a non-stdlib import to `scripts/reviewer.py` — see Rule #2.
2. Rename or remove an input in `action.yml` without bumping the major version — see Rule #4.
3. Skip the compile-check before pushing — see Rule #5.
4. Hard-code provider-specific fields outside the `Provider` implementation — the abstraction has to stay clean for v1.1.
5. Inline secrets into the script (e.g. for "local debugging convenience") — they end up in commit history.
6. Send a PR that changes the prompt without a before/after comparison on a real PR.
7. Print the API key (or any sensitive env var) to stdout — `redact_for_log` is the gate for tool-arg logging, but never `print(os.environ["AIPRR_API_KEY"])`.
8. Bypass the existing 422 fallback path when adding a new submission code path — preserve graceful degradation.
9. Increase `max_tokens` or `MAX_TURNS` defaults without estimating the cost-per-review impact and documenting it.
10. Add a new top-level `action.yml` input "just to support a one-off use case" — every input is a long-lived public contract.
11. Hardcode anything that should be a constant — magic numbers, paths, severity ranks. The top of `scripts/reviewer.py` is the canonical place for runtime constants.
12. Edit content in `.claude/...` or `CLAUDE.md` — both are symlinks. Edit the canonical paths under `.agents/...` and `AGENTS.md`.
13. Spell the action name "AI-PR-reviewer" / "AIPR" / "AI/PR Reviewer" in user-facing copy — the canonical user-facing capitalisation is **"AI PR Reviewer"** and the slug is `ai-pr-reviewer`.

### DO

1. Keep the runtime stdlib-only.
2. Use type hints on every function signature and meaningful local.
3. Write `# noqa: BLE001` on intentionally broad excepts and explain in a comment WHY (the patterns are: "best-effort GH API call", "surface to model rather than crash", "wrap loop so failures hit the spinner").
4. Run `python3 -m py_compile scripts/reviewer.py` before pushing.
5. Update `README.md` + `CHANGELOG.md` in the same PR as the behaviour change.
6. Use `write_action_output()` for any new value you want consumers to read in downstream steps.
7. Use `safe_repo_path()` for any new tool that takes a path argument — never resolve user-supplied paths manually.
8. Add a row to the inputs table in `README.md` for any new input.
9. Verify the change via `.github/workflows/self-review.yml` running on the PR.
10. Edit the canonical `AGENTS.md` / `.agents/...` paths.
11. Use **"AI PR Reviewer"** for product copy, `ai-pr-reviewer` for the slug, `AIPRR_` for env-var prefix.

---

## Pre-Commit Checklist

- [ ] All code in English with type hints.
- [ ] No new non-stdlib imports in `scripts/reviewer.py`.
- [ ] `python3 -m py_compile scripts/reviewer.py` passes.
- [ ] `action.yml` parses (the CI job validates this; locally: `python3 -c 'import yaml; yaml.safe_load(open("action.yml"))'`).
- [ ] If `action.yml` inputs/outputs changed: README's tables updated.
- [ ] If runtime behaviour changed: `CHANGELOG.md` entry under `[Unreleased]`.
- [ ] If a new input was added: there's an example in `examples/` showing realistic usage.
- [ ] If the default prompt changed: a before/after on a real PR linked in the PR description.
- [ ] No new files at `.claude/...` or `CLAUDE.md` — those are symlinks; edit the canonical paths.
- [ ] Commit message follows Conventional Commits.
- [ ] `.github/workflows/self-review.yml` ran successfully on the PR (or manual verification is described).

---

## Commit Message Format (MANDATORY)

```
<type>(<scope>): <short description>

## Summary
<1–2 sentences — the why, not the what>

## Change Log
- <bullet 1>
- <bullet 2>

## Risks
- <risk 1, or "None — content-only change">
```

Example:

```
feat(provider): add OpenAI provider

## Summary
First non-Anthropic provider — translates Anthropic-shape messages and
tool calls to OpenAI's chat-completions schema at the boundary so the
rest of the runtime is unchanged.

## Change Log
- New OpenAIProvider class with tool-call translation in both directions
- New default model entry: openai → gpt-4o
- New optional input api-base for self-hosted OpenAI-compatible endpoints

## Risks
- Translation layer is the only meaningful new surface; covered by smoke
  test on PR #42 (provider: openai). No change to existing Anthropic path.
```

---

## Shared Agent Coordination

Every AI agent that works on this repo (Claude Code, Cursor, Codex, Gemini, Copilot, OpenClaw) is guided by **this `AGENTS.md`** — the single source of truth. Agent-specific entry points (`CLAUDE.md`, `.cursorrules`, etc.) MUST be thin pointers and MUST NOT duplicate content.

The canonical configuration directory is **`.agents/`**. `.claude/` is a tracked symlink to `.agents/` for back-compat with Claude Code. Always reference `.agents/...` in new docs and commit messages — never `.claude/...`. If you ever need to recreate the symlink (e.g. on a clone that mishandled it):

```bash
rm -f .claude && ln -s .agents .claude
rm -f CLAUDE.md && ln -s AGENTS.md CLAUDE.md
```

For the full collaboration model — when to spawn sub-agents, how to coordinate between agents, when to use a deep-work plan — see [docs/AI_AGENT_COLLAB.md](docs/AI_AGENT_COLLAB.md).

---

## Reading PR Review Comments

This repository **dogfoods itself**: every PR is reviewed by the action it ships, via `.github/workflows/self-review.yml`. When applying review feedback:

- Skip `isMinimized == true` comments (those are previous reviews collapsed by `collapse-previous`).
- Anchor on the most recent `<!-- ai-pr-reviewer-marker -->` comment to identify the authoritative review SHA.
- The action collapses prior reviews on every push, so reading all comments blindly will mix live and stale feedback.

Full workflow + ready-to-copy GraphQL query: [docs/PR_REVIEW_WORKFLOW.md](docs/PR_REVIEW_WORKFLOW.md).

---

## Small-Batch Delivery

For larger initiatives (multi-provider rollout, prompt overhaul, output schema redesign):

1. Pick only tasks whose dependencies are complete.
2. 1–3 tightly related tasks per PR.
3. Each PR self-reviewable via `self-review.yml`.
4. Verify each batch before starting the next.
5. Keep each batch publishable as a `vX.Y.Z` release behind clear changelog entries.

---

## Temporary Files (tmp/)

The `tmp/` folder at project root is **git-ignored** and available for scratch
work, inter-agent prompts, data exports, and temporary files. Agents can freely
write to `tmp/` without affecting the repository.

**Nothing inside `tmp/` is ever tracked or committed** — the whole folder is
ignored by git. Write freely (scratch notes, inter-agent prompts, data exports,
query results); it will never show up in `git status` or a diff.

---

## License

[MIT](LICENSE) — by contributing to this repo you agree your contribution is licensed under MIT.
