# AGENTS.md — Documentation for AI Agents

**Purpose:** Single source of truth for every AI coding assistant working on this repository (Claude Code, Cursor, OpenAI Codex, Google Gemini, GitHub Copilot, OpenClaw, and others). Human contributors are also welcome readers — this file is the fastest way to get oriented.

The product name in user-facing strings is **"AI Diff Reviewer"** (capitalised exactly that way). The **git repository slug** is `DailybotHQ/ai-diff-reviewer` — renamed from `DailybotHQ/ai-pr-reviewer` on 2026-07-14 to unblock Marketplace publish (see Rule #9). Old `uses: DailybotHQ/ai-pr-reviewer@v1` pins keep working via GitHub's permanent 301 redirect for renamed repos; new copy-paste examples in the README always use the canonical `DailybotHQ/ai-diff-reviewer` path. The **Marketplace listing slug** is `ai-diff-reviewer`, derived from the `action.yml` `name:` field — matches the repo slug exactly. Vendor attribution is handled by GitHub automatically via the `author:` field (`DailybotHQ`) — the Marketplace tile renders "by DailybotHQ" beneath the title, so we do NOT embed "Dailybot" in the `name:` field. See Rule #9 for the full rename decision log.

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
| Performance | [docs/PERFORMANCE.md](docs/PERFORMANCE.md) |
| Docs index | [docs/README.md](docs/README.md) |
| Skills & Agents Catalog | [.agents/docs/skills_agents_catalog.md](.agents/docs/skills_agents_catalog.md) |
| Deep Work Plan skill | [.agents/skills/deepworkplan/SKILL.md](.agents/skills/deepworkplan/SKILL.md) |
| Dailybot agent skill | [.agents/skills/dailybot/SKILL.md](.agents/skills/dailybot/SKILL.md) |

---

## Project Overview

**AI Diff Reviewer** is an LLM-driven pull-request reviewer packaged as a GitHub Action. It posts inline comments with severity tags, gates the GitHub check based on configurable strictness, applies a "reviewed" label, and collapses prior reviews — all from a single composite action with zero infrastructure.

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
├── tests/                          # Stdlib-unittest suite for the runtime
├── docs/                           # User-facing + contributor-facing documentation
├── .github/
│   ├── workflows/                  # code_check, auto-release, release, self-review
│   ├── scripts/                    # CI-only helpers (action.yml validator)
│   ├── ISSUE_TEMPLATE/             # Bug + feature issue forms
│   └── dependabot.yml              # Weekly GitHub Actions bumps
├── .agents/                        # Canonical AI-agent configuration (symlinked from .claude)
│   ├── agents/                     # Sub-agent definitions
│   ├── commands/                   # Slash commands (commit, pr, release, prompt-test, …)
│   ├── docs/                       # Catalog + agent-targeted docs
│   ├── skills/                     # Skill definitions (release, prompt-test, add-provider, deepworkplan, dailybot, …)
│   ├── settings.json               # Agent harness settings
│   └── README.md
├── skills-lock.json                # skills.sh lockfile pinning vendored skill sources + hashes
├── README.md                       # Marketplace-facing readme
├── AGENTS.md                       # ← you are here (source of truth)
├── CLAUDE.md                       # Symlink → AGENTS.md
├── CHANGELOG.md
├── CONTRIBUTING.md
└── LICENSE                         # MIT
```

---

## Quick Commands

The real, runnable commands for local work on this repo. No install phase — Python 3.10+ ships with everything needed (the runtime is stdlib-only). See [`docs/DEVELOPMENT_COMMANDS.md`](docs/DEVELOPMENT_COMMANDS.md) for the full reference and local-debug envs.

| Purpose | Command |
|---|---|
| Compile-check the runtime (MANDATORY before commit — [Rule #5](#5-compile-check-before-commit)) | `python3 -m py_compile scripts/reviewer.py` |
| Run the full unit-test suite (stdlib `unittest`, no third-party runner) | `python3 -m unittest discover -s tests -v` |
| Validate the `action.yml` public contract (CI parity — needs `pip install pyyaml`) | `python3 .github/scripts/validate_action.py` |
| Parse `action.yml` (quick sanity check, needs `pip install pyyaml`) | `python3 -c 'import yaml; yaml.safe_load(open("action.yml"))'` |
| Objectively verify DWP conformance | `bash .agents/skills/deepworkplan/verify/conformance.sh` |
| Verify auth to Dailybot (never prompts, safe to run) | `dailybot status --auth` |

Every one of these runs on a vanilla `ubuntu-latest` matching the CI environment ([`.github/workflows/code_check.yml`](.github/workflows/code_check.yml)) — if it passes locally, it passes in CI.

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

CI runs this on every PR; pre-commit-checking it locally is courtesy, not optional. Beyond compilation there is a stdlib-`unittest` suite in `tests/` covering the runtime's pure logic, plus dogfooding on real PRs. Run it locally with `python3 -m unittest discover -s tests` (see [docs/TESTING_GUIDE.md](docs/TESTING_GUIDE.md)).

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
- `skills/ai-diff-reviewer/setup/reference.md` → update if `action.yml` inputs, defaults, or descriptions changed (this file is the local companion skill's reference manual; drift breaks the "any agent can answer setup questions" promise).
- `AGENTS.md` (this file) → update the "Critical Rules" or "DO/DON'T" sections if you change a project standard.

### 8. SemVer for Releases (MANDATORY)

Releases follow Semantic Versioning. Tags are `vX.Y.Z`. The `release.yml` workflow auto-updates the moving major tag (`v1`) on every `v1.x.y` release; consumers pinning `@v1` get patches and minor features automatically. Never delete a published tag — consumers pin to it.

### 9. Marketplace Branding Stable

`action.yml` `name`, `description`, `branding.icon`, and `branding.color` are visible in the GitHub Marketplace listing. Once published, treat them as immutable for cosmetic reasons (consumers' search results and tile UI depend on them). Editorial changes are fine; identity changes need a deliberate decision.

The current values are:
- `name: 'AI Diff Reviewer'` (Marketplace tile + listing title; slugifies to `ai-diff-reviewer`)
- `description: 'Run an LLM-driven code review on every pull request — inline comments, severity-based gating, no infra required.'`
- `branding.icon: 'check-circle'`
- `branding.color: 'purple'`

**Repo slug ≠ Marketplace slug.** The git repo lives at `DailybotHQ/ai-diff-reviewer` (historical, with published tags v1.0.0–v1.4.2) and consumers pin against that path (`uses: DailybotHQ/ai-diff-reviewer@v1`). The Marketplace listing is a separate slug derived from `name:` — currently `ai-diff-reviewer`. The two are decoupled by design: consumers see the friendly name in Marketplace search; their workflows keep using the stable repo path.

### Rename decision log (chronological)

1. **v1.0.0 – v1.2.0:** initial `name: 'AI PR Reviewer'` (slug `ai-pr-reviewer`, repo `DailybotHQ/ai-pr-reviewer`) — assumed free based on Marketplace search.
2. **v1.2.1:** first publish attempt failed. Misdiagnosed the collision (thought it was against the `ai-pull-request-reviewer` full-form slug owned by `appchoose/ai-pr-review`), set defensive `name: 'Dailybot AI PR Reviewer'` (slug `dailybot-ai-pr-reviewer`) as a vendor-prefix workaround.
3. **v1.3.0:** re-checked Marketplace listing search — `ai-pr-reviewer` appeared free among Marketplace slugs. Reverted the prefix to `'AI PR Reviewer'` for cleaner OSS-community positioning. Repo still `DailybotHQ/ai-pr-reviewer`.
4. **v1.5.0 (current, 2026-07-14):** second publish attempt failed with the correct diagnosis this time — GitHub's Marketplace name-uniqueness rule includes `user or organization name`, and the org `github.com/ai-pr-reviewer` (created 2024-01-12, 0 public repos, name-squatting) blocks the slug at the org-namespace level, not the Marketplace-listing level. Two coordinated renames:
   - **`action.yml` `name:`** — `'AI PR Reviewer'` → `'AI Diff Reviewer'` (slug `ai-diff-reviewer`, verified free at both the Marketplace and org-namespace levels).
   - **GitHub repo** — `DailybotHQ/ai-pr-reviewer` → `DailybotHQ/ai-diff-reviewer`. So the repo slug now matches the Marketplace slug exactly. GitHub's permanent 301 redirect on renamed repos keeps `uses: DailybotHQ/ai-pr-reviewer@v1` pins working for all existing consumers — no migration required.
   - Rationale for the specific name: "AI Diff Reviewer" is more accurate than "AI PR Reviewer" — this action reviews the `git diff origin/<base>...HEAD` specifically, not the PR envelope (labels, description, metadata). The vendor prefix stays OFF: attribution is auto-rendered by GitHub via `author: DailybotHQ` in the listing footer, and OSS positioning is stronger without a brand prefix.

**Rule going forward:** do NOT rename this again unless there's a similarly load-bearing reason (Marketplace publish blocker, trademark issue). The name `'AI Diff Reviewer'` and the repo slug `DailybotHQ/ai-diff-reviewer` are now the stable public identity. Do not re-add the `Dailybot`-prefix (see v1.2.1 above for why it was a bad idea both times).

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
| `/dwp-create` | Decompose a goal into a Deep Work Plan (numbered tasks + validation gates). |
| `/dwp-execute` | Execute a Deep Work Plan task by task, validating each gate. |
| `/dwp-refine` | Add, remove, or reorder tasks while preserving completed work. |
| `/dwp-resume` | Reconstruct state and continue an interrupted plan. |
| `/dwp-status` | Report progress on a plan without making changes. |
| `/dwp-verify` | Objective pass/fail conformance report against the DWP spec. |
| `/skill-create` | Author or update a reusable skill under `.agents/skills/`. |
| `/agent-create` | Author or update a sub-agent persona under `.agents/agents/`. |

The eight `dwp-*` / `skill-create` / `agent-create` entries are thin delegators to the installed `deepworkplan` skill at [`.agents/skills/deepworkplan/`](.agents/skills/deepworkplan/) — see the [Deep Work Plan](#deep-work-plan) section below.

---

## Deep Work Plan

This repository ships the **Deep Work Plan (DWP)** methodology as an installed skill so any AI agent can plan, execute, and verify structured engineering work here. DWP rests on two pillars: **spec-driven development** (the plan is the spec — atomic tasks with binary validation gates) and **harness engineering** (the repository itself is the harness: `AGENTS.md`, `docs/`, `.agents/` kit, and the gitignored `.dwp/` state layer).

### The eight sub-skills

Installed at [.agents/skills/deepworkplan/](.agents/skills/deepworkplan/):

| Sub-skill | Purpose |
|---|---|
| `create` | Decompose a goal into a numbered, sequential Deep Work Plan with per-task validation gates. |
| `execute` | Run a plan task by task, checking each gate, updating progress. |
| `refine` | Modify a plan (add, remove, reorder tasks) while preserving completed work. |
| `resume` | Reconstruct state and continue an interrupted plan across sessions or agents. |
| `status` | Report progress without making changes. |
| `verify` | Emit an objective CONFORMANT / NOT CONFORMANT verdict against the DWP spec's Conformance document. |
| `onboard` | Make a repository AI-first (reasoned analysis + non-destructive generation). |
| `author` | Author or evolve this repo's own skills, agents, and commands. |

The `dwp-*`, `skill-create`, and `agent-create` slash commands in [.agents/commands/](.agents/commands/) are thin delegators to these — the skill is the single source of truth.

### Where plans live

Deep Work Plan outputs — plans, drafts, and onboarding recon/report — live under **`.dwp/`** at the repo root. That directory is **gitignored** (see [`.gitignore`](.gitignore)); plans are working artifacts, not tracked source.

```
.dwp/
├── plans/       ← PLAN_{name}/ directories (executing/executed plans)
├── drafts/      ← {name}_draft_refined.md (created by /dwp-create)
└── onboard/     ← RECON.md and REPORT.md from /deepworkplan-onboard
```

Full path convention: [.agents/skills/deepworkplan/shared/dwp-paths.md](.agents/skills/deepworkplan/shared/dwp-paths.md).

### When to reach for it

- The task has multiple valid approaches, touches many files, or needs to survive across sessions → `/dwp-create` first, then `/dwp-execute`.
- A previous plan was interrupted → `/dwp-resume`.
- Before wrapping onboarding or a large change → `/dwp-verify` gives an objective conformance gate.
- Small, obvious edits → don't bother; work directly.

DWP is complementary to the repo's existing `/release`, `/prompt-test`, and `/add-provider` skills — those remain the right tools for their specific workflows. DWP is for **novel** work that needs decomposition and gates.

### Dailybot reporting (optional, non-blocking)

This repo has the **Dailybot addon** enabled. When the `dailybot` CLI is on `PATH` and authenticated (`dailybot login`), significant DWP work surfaces to the team's Dailybot standup as agent updates. If Dailybot is absent, unauthenticated, unreachable, or `.dailybot/disabled` exists at the repo root, reporting **skips silently and never blocks the primary work**.

**Four lifecycle events** (per [DWP Dailybot addon SPEC §5.1](.agents/skills/deepworkplan/addons/dailybot/SPEC.md)):

| Event | When | Level |
|---|---|---|
| **Kickoff** | A plan is materialized and approved — describe *what is being built and why*. Fires once per plan. | regular |
| **Significant task** | A feature / bug fix / major refactor ships mid-plan. Intermediate setup tasks are **not** reported. | regular |
| **Blocked** | The plan halts on a stop condition and `state.json.blocked` is populated — the team sees what's stuck and what it needs. | regular (with `blockers`) |
| **Completion** | The plan finishes — describe *what was built*, never "completed a plan". Fires once per plan. | **milestone** |

Every event is emitted via the dailybot `report` sub-skill (`dailybot agent update ... --milestone --json-data ...`); payloads are derived from the plan's state layer when present (`.dwp/plans/PLAN_{name}/state.json`).

**Deterministic hooks.** This repo commits harness hook configs for both Claude Code (`.agents/settings.json`, resolved as `.claude/settings.json` via the symlink) and Cursor (`.cursor/hooks.json`). They call `dailybot hook session-start|activity|stop` at session start, after file writes, and end of turn — the harness itself reminds the agent about unreported work. All hook commands are local-only (no network), always exit `0`, and cannot block. When a reminder fires, respond with either a lifecycle-appropriate report or `dailybot hook dismiss` — never ignore silently.

**Repo identity.** `.dailybot/profile.json` carries the credential-free repo identity (`name`, `default_metadata`, `report` policy). To silence Dailybot for a session or a whole clone, `touch .dailybot/disabled`. To turn reminders off while keeping manual reporting available, set `"report": {"nudge": false}` in `profile.json`.

**Uninstall.** Delete `.dailybot/`, `.cursor/hooks.json`, and remove the three hook entries from `.agents/settings.json` (every entry contains the string `dailybot hook`).

---

## Skills & Agents

Reusable **Skills** (slash commands and one-shot workflows) and **Agents** (specialised personas) live in [.agents/skills/](.agents/skills/) and [.agents/agents/](.agents/agents/). The full catalog with tier classification is in [.agents/docs/skills_agents_catalog.md](.agents/docs/skills_agents_catalog.md).

**This repo also ships a native skill of its own — [`ai-diff-reviewer`](skills/ai-diff-reviewer/SKILL.md)** — the local companion to the shipped GitHub Action. It lives at [`skills/ai-diff-reviewer/`](skills/ai-diff-reviewer/) in the ROOT (not under `.agents/`) because that is where [`skills.sh`](https://skills.sh) scans when consumers run `npx skills add DailybotHQ/ai-diff-reviewer --skill ai-diff-reviewer`. Two CI invariants guarantee action ↔ skill parity: (a) [`code_check.yml`](.github/workflows/code_check.yml) `Skills — prompt-sync invariant` job fails on PRs where `skills/ai-diff-reviewer/prompt.md` diverges from `prompts/default.md`; (b) [`auto-release.yml`](.github/workflows/auto-release.yml) re-syncs the byte-copy AND bumps the skill's frontmatter `version:` field on every release cut so `@v1.4.3` on both surfaces ships the same prompt. Frontmatter is validated in CI against the Open Agent Skills contract by [`scripts/validate-frontmatter.py`](scripts/validate-frontmatter.py) (adapted from the DailybotHQ/agent-skill validator). Consumers layer repo-specific overrides at `.review/extension.md` (auto-detected by the skill; same path can be referenced from CI via the action's `prompt-extension-file:` input for local ↔ CI parity). See [`docs/PROMPTS.md` § "Local coding-agent parity"](docs/PROMPTS.md) for the extension-authoring guide.

Two other skills — [`deepworkplan`](.agents/skills/deepworkplan/SKILL.md) and [`dailybot`](.agents/skills/dailybot/SKILL.md) — are **vendored dogfood copies of upstream skills** (`DailybotHQ/deepworkplan-skill` and `DailybotHQ/agent-skill`), pinned via [`skills-lock.json`](skills-lock.json) at repo root. The lockfile is written by the [`skills.sh`](https://skills.sh) CLI (`npx skills`) and records the exact source repo + content hash for each vendored skill so any contributor can restore the same versions deterministically:

```bash
# Restore vendored skills from the lockfile (idempotent — no-op if already up to date)
npx skills experimental_install

# Pull the latest upstream release into the vendored copy + rewrite the hash in the lockfile
npx skills update deepworkplan dailybot

# Re-add a skill from scratch (e.g. after upstream renames the repo)
npx skills add DailybotHQ/deepworkplan-skill --skill deepworkplan -y
npx skills add DailybotHQ/agent-skill --skill dailybot -y
```

The four in-house skills (`release`, `prompt-test`, `add-provider`, plus the agent personas) are **not** vendored — they're authored directly in this repo and not tracked by the lockfile.

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
13. Spell the action name "AI-Diff-Reviewer" / "AIDR" / "AI/Diff Reviewer" / "AI PR Reviewer" (the old name) in user-facing copy — the canonical user-facing capitalisation is **"AI Diff Reviewer"**. The git repo slug is `ai-diff-reviewer` (renamed 2026-07-14; the old `ai-pr-reviewer` URL still resolves via GitHub's permanent 301 redirect), and the Marketplace listing slug is `ai-diff-reviewer` (derived from `action.yml` `name:`) — they match exactly. Rule #9 has the full rename decision log.

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
11. Use **"AI Diff Reviewer"** for product copy (Marketplace-facing), `DailybotHQ/ai-diff-reviewer` for the canonical repo slug (the old `DailybotHQ/ai-pr-reviewer` still redirects for back-compat), `ai-diff-reviewer` for the Marketplace slug (derived from `action.yml`), and `AIPRR_` for the env-var prefix (private, unchanged).

---

## Pre-Commit Checklist

- [ ] All code in English with type hints.
- [ ] No new non-stdlib imports in `scripts/reviewer.py`.
- [ ] `python3 -m py_compile scripts/reviewer.py` passes.
- [ ] `python3 -m unittest discover -s tests` passes (if the runtime changed).
- [ ] `action.yml` parses (the CI job validates this; locally: `python3 -c 'import yaml; yaml.safe_load(open("action.yml"))'`).
- [ ] If `action.yml` inputs/outputs changed: README's tables updated AND `skills/ai-diff-reviewer/setup/reference.md` updated.
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
