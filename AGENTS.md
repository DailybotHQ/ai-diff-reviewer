# AGENTS.md — Documentation for AI Agents

**Purpose:** Single source of truth for every AI coding assistant working on this repository (Claude Code, Cursor, OpenAI Codex, Google Gemini, GitHub Copilot, OpenClaw, and others). Human contributors are also welcome readers — this file is the fastest way to get oriented.

The product name in user-facing strings is **"AI Diff Reviewer"** (capitalised exactly that way). The **git repository slug** is `DailybotHQ/ai-diff-reviewer` — renamed from `DailybotHQ/ai-pr-reviewer` on 2026-07-14 to unblock Marketplace publish (see Rule #9). Old `uses: DailybotHQ/ai-pr-reviewer@v1` pins keep working via GitHub's permanent 301 redirect for renamed repos; new copy-paste examples in the README always use the canonical `DailybotHQ/ai-diff-reviewer` path. The **Marketplace listing slug** is `ai-diff-reviewer`, derived from the `action.yml` `name:` field — matches the repo slug exactly. Vendor attribution is handled by GitHub automatically via the `author:` field (`DailybotHQ`) — the Marketplace tile renders "by DailybotHQ" beneath the title, so we do NOT embed "Dailybot" in the `name:` field. See Rule #9 for the naming rule, and the [Marketplace rename decision log](docs/STANDARDS.md#marketplace-rename-decision-log) for the full chronology.

---

## Working principles

Work with autonomy, ownership, and sound judgment. Pursue excellence through
correctness, clarity, simplicity, and verified completion.

- **Own the outcome.** Carry authorized work through investigation, execution,
  and appropriate validation. Continue until the requested outcome is complete
  or a concrete blocker prevents further progress.
- **Be resourceful before asking.** Inspect available code, documentation,
  tools, and prior decisions. Resolve questions you can answer through
  reasonable investigation instead of transferring that work to the user.
- **Make routine decisions independently.** Choose sensible approaches within
  the authorized scope. State consequential assumptions. Avoid confirmation
  requests for routine steps or actions already authorized.
- **Ask when judgment or authorization is missing.** Consult the user when
  essential information is unavailable, a material decision cannot be inferred
  reliably, or an action requires approval not already granted. Bring the
  investigation, relevant options, and your recommendation.
- **Make approvals concrete.** Complete authorized preparation before asking
  for approval. Present a reviewable result and identify the action requiring
  approval and why it requires it.
- **Work through obstacles.** Investigate failures and attempt reasonable
  recovery within scope. Continue independent authorized work when possible.
  Respect applicable stop conditions; escalate when progress requires user
  input or an external change.
- **Respect intent and scope.** Analysis requests remain analysis. Propose
  broader improvements separately unless already authorized. Preserve the
  user's existing work, decisions, and repository-specific approval rules.
- **Apply proportionate rigor.** Address underlying causes and favor
  maintainable solutions. Match investigation, validation, and polish to the
  task's impact. Avoid unnecessary complexity and unrelated changes.
- **Communicate directly and precisely.** Lead with the result or decision.
  Explain consequential tradeoffs concisely. Distinguish verified facts,
  assumptions, and unresolved uncertainty.
- **Verify before declaring completion.** Review the result against the
  request, perform appropriate checks, and fix issues within scope. Report
  what was validated and any remaining limitations. Never claim actions,
  checks, or outcomes that did not occur.

---

## Detailed Documentation

| Category | Document |
|----------|----------|
| Product Spec | [docs/PRODUCT_SPEC.md](docs/PRODUCT_SPEC.md) |
| Architecture | [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) |
| Security | [docs/SECURITY.md](docs/SECURITY.md) |
| Testing | [docs/TESTING_GUIDE.md](docs/TESTING_GUIDE.md) |
| Development Commands | [docs/DEVELOPMENT_COMMANDS.md](docs/DEVELOPMENT_COMMANDS.md) |
| Release Recovery Playbook | [docs/RELEASE_RECOVERY.md](docs/RELEASE_RECOVERY.md) |
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
| Iteration-Aware Review | [docs/ITERATION_AWARENESS.md](docs/ITERATION_AWARENESS.md) |
| v2 pin + platform notes | [docs/MIGRATION_v2.md](docs/MIGRATION_v2.md) |
| v3 design records (RFCs) | [docs/rfc/v3/README.md](docs/rfc/v3/README.md) |
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
- **Runner × backend abstraction.** Six runners (`anthropic`, `openai` in-process; `claude-code`, `cursor`, `codex`, `grok` CLIs) and an `EndpointProfile` resolved from the optional `api-base` input (Anthropic, OpenAI, Azure Foundry, xAI, Z.ai, custom). Every backend URL comes from `resolve_endpoint_profile`; an empty `api-base` selects the vendor's default endpoint (the v2 "byte-identical to earlier releases" promise ended with v3's request shape — BC-17, `docs/MIGRATION_v3.md`).

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
- `examples/README.md` → add a row for every new `examples/*.yml` (the index is checked by script against the files on disk).
- `skills/**/SKILL.md` frontmatter → `description` ≤ 1,024 characters and `name` ≤ 64 (Open Agent Skills limits; `scripts/validate-frontmatter.py` enforces both in CI — hosts such as Pi warn on longer descriptions). Put trigger catalogues in the body, not the frontmatter.
- `AGENTS.md` (this file) → update the "Critical Rules" or "DO/DON'T" sections if you change a project standard.

### 8. SemVer for Releases (MANDATORY)

Releases follow Semantic Versioning. Tags are `vX.Y.Z`. The `release.yml` workflow auto-updates the moving major tag for the current line (`v3`) on every `v3.x.y` release; consumers pinning `@v3` get patches and minor features automatically (the frozen `v2` line is maintained from `release/v2` for six months — see [MIGRATION_v3](docs/MIGRATION_v3.md)). Never delete a published tag — consumers pin to it. **v3 adds an eval gate (BC-02):** `auto-release.yml` cuts a release only when `tests/eval/release_gate.py` finds a current, non-blocking `verdict/1.0` for the candidate's runtime + prompt; a missing or stale verdict skips the cut (see `docs/RELEASE_RECOVERY.md`).

### 9. Marketplace Branding Stable

`action.yml` `name`, `description`, `branding.icon`, and `branding.color` are visible in the GitHub Marketplace listing. Once published, treat them as immutable for cosmetic reasons (consumers' search results and tile UI depend on them). Editorial changes are fine; identity changes need a deliberate decision.

The current values are:
- `name: 'AI Diff Reviewer'` (Marketplace tile + listing title; slugifies to `ai-diff-reviewer`)
- `description: 'Run an LLM-driven code review on every pull request — inline comments, severity-based gating, no infra required.'`
- `branding.icon: 'check-circle'`
- `branding.color: 'purple'`

**Repo slug ≠ Marketplace slug.** The git repo lives at `DailybotHQ/ai-diff-reviewer` and copy-paste examples pin against that path (`uses: DailybotHQ/ai-diff-reviewer@v3`). The Marketplace listing is a separate slug derived from `name:` — currently `ai-diff-reviewer`. The two are decoupled by design: consumers see the friendly name in Marketplace search; their workflows keep using the stable repo path.

The [Marketplace rename decision log](docs/STANDARDS.md#marketplace-rename-decision-log) records the release history and rationale.

**Rule going forward:** do NOT rename this again unless there's a similarly load-bearing reason (Marketplace publish blocker, trademark issue). The name `'AI Diff Reviewer'` and the repo slug `DailybotHQ/ai-diff-reviewer` are now the stable public identity. Do not re-add the `Dailybot`-prefix (see the [rename decision log](docs/STANDARDS.md#marketplace-rename-decision-log) for the rationale).

### 10. Dogfooding is Required

Two independent dogfooding surfaces enforce that we consume our own product the way our users do:

**A. The CI action reviews its own PRs.** Any change that affects the agentic loop, the prompt, or the review-submission path MUST be verified by `.github/workflows/self-review.yml` running successfully on the PR that introduces the change. If the change can't be verified by self-review (e.g. it only fires on the `block-on-warning` strictness path), describe the manual verification you did in the PR description.

**B. The local companion skill is vendored into this repo.** `.agents/skills/ai-diff-reviewer/` is a vendored copy of the same skill package we ship, pinned via [`skills-lock.json`](skills-lock.json) — installed exactly the way any consumer would install it (`npx skills add DailybotHQ/ai-diff-reviewer --skill ai-diff-reviewer`). Refreshing that copy after every release is handled automatically by [`auto-release.yml` Step 3.5](.github/workflows/auto-release.yml): it runs `npx skills update ai-diff-reviewer` right after the new tag is pushed, commits the diff as `chore(release): dogfood vendored ai-diff-reviewer to vX.Y.Z [skip release]`, and fails loudly if the just-published tag doesn't install cleanly. **Do not manually edit `.agents/skills/ai-diff-reviewer/` on a feature branch** — that's the released-version snapshot; work on `skills/ai-diff-reviewer/` (the source-of-truth copy that ships to consumers) and the vendored copy refreshes itself at the next release.

Together, (A) is the runtime-behavior gate; (B) is the install-flow gate. A skill change that ships broken `npx skills add` compatibility will fail Step 3.5 of the very release that publishes it.

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
| `/dwp-upgrade` | Check for a newer DeepWorkPlan skill; read-only until consent, then installs the accepted tag and re-onboards. |
| `/skill-create` | Author or update a reusable skill under `.agents/skills/`. |
| `/agent-create` | Author or update a sub-agent persona under `.agents/agents/`. |

The nine `dwp-*` / `skill-create` / `agent-create` entries are thin delegators to the installed `deepworkplan` skill at [`.agents/skills/deepworkplan/`](.agents/skills/deepworkplan/) — see the [Deep Work Plan](#deep-work-plan) section below.

---

## Deep Work Plan

This repository ships the **Deep Work Plan (DWP)** methodology as an installed skill so any AI agent can plan, execute, and verify structured engineering work here. DWP rests on two pillars: **spec-driven development** (the plan is the spec — atomic tasks with binary validation gates) and **harness engineering** (the repository itself is the harness: `AGENTS.md`, `docs/`, `.agents/` kit, and the gitignored `.dwp/` state layer). DWP standard: 5.0.0 (onboarded 2026-07-04; upgraded 2026-09-13; skill 5.3.0).

### Deep Work Plans — invocation

Structured work runs through the local DWP flows (`.agents/commands/dwp-*` delegators; the flows live in `.agents/skills/deepworkplan/` — discovery is local, no network service is consulted):

| Intent | Route |
|---|---|
| "plan this work", "create a plan" | `/dwp-create` |
| "execute / run the plan" | `/dwp-execute` |
| "modify the plan", "change the scope", "promote Lite→Full" | `/dwp-refine` |
| "continue / resume the interrupted plan" | `/dwp-resume` |
| "plan status", "what's left" | `/dwp-status` (read-only) |
| "verify the repo / the plan" | `/dwp-verify` (read-only) |
| "upgrade the DWP skill / harness" | `/dwp-upgrade` (read-only until consent; then installs the accepted tag and re-onboards) |
| ordinary direct edit ("fix this", "rename that") | done directly — never silently becomes a plan |

Hosts without slash commands invoke the same flows by name (`#deepworkplan-create` or plain text). `trust`/`auto` authorizes unattended continuation within the requested flow; it is not a flow selector, and read-only routes stay read-only.

The [DeepWorkPlan sub-skill reference](docs/AI_AGENT_ONBOARDING.md#deepworkplan-sub-skills) describes the nine flows.

### Where plans live

Deep Work Plan outputs — `plans/` (`PLAN_{name}/` directories) and `onboard/` (RECON.md + REPORT.md) — live under **`.dwp/`** at the repo root. That directory is **gitignored** (see [`.gitignore`](.gitignore)); plans are working artifacts, not tracked source. Full path convention: [.agents/skills/deepworkplan/shared/dwp-paths.md](.agents/skills/deepworkplan/shared/dwp-paths.md).

### When to reach for it

Reach for a plan when work has multiple valid approaches, touches many files, or must survive across sessions (`/dwp-create` → `/dwp-execute`; `/dwp-resume` if interrupted; `/dwp-verify` for an objective gate). Small, obvious edits → work directly. DWP is complementary to the repo's existing `/release`, `/prompt-test`, and `/add-provider` skills — those remain the right tools for their specific workflows. DWP is for **novel** work that needs decomposition and gates.

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

### AI Diff Reviewer addon (Flow B — dual-surface)

This repo has the **AI Diff Reviewer addon** enabled in **Flow B** (local skill + CI Action). Detection for DWP `create` / `execute` is: vendored skill at [`.agents/skills/ai-diff-reviewer/`](.agents/skills/ai-diff-reviewer/) **plus** [`.review/extension.md`](.review/extension.md). Spec: [`.agents/skills/deepworkplan/addons/ai-diff-reviewer/SPEC.md`](.agents/skills/deepworkplan/addons/ai-diff-reviewer/SPEC.md).

**Final Review security-pass augmentation (both flows).** Every 2.3.0+ plan ends in a single mandatory Final Review; its security pass runs the upstream parent default flow ("Review my current branch" / `/ai-diff-reviewer`), appends verdict + findings under `## AI Diff Reviewer local review` in `analysis_results/SECURITY_REVIEW.md`, and treats open `critical` findings as Final Review blockers until fixed or explicitly accepted. A missing vendored skill or extension file is **not** a silent skip — record a `local reviewer not installed` finding (installation is onboarding-only). Soft-fail (warn once, continue the security pass) applies only to invocation errors (network down, upstream skill error) — an unset CI provider secret must **not** skip the local pass.

**CI surface (this repo).** Consumer Flow B normally installs `.github/workflows/pr-review.yml` via the upstream `setup` sub-skill. This repository **is** the Action, so the dual-surface CI gate is the dogfood workflow [`.github/workflows/self-review.yml`](.github/workflows/self-review.yml) (`uses: ./` against the PR HEAD, label-gated on `ready`, stable gate job). Do **not** add a second consumer-style `pr-review.yml` here — that would double-review every PR. Provider secrets for the dogfood matrix: at least one of `ANTHROPIC_API_KEY`, `CLAUDE_CODE_OAUTH_TOKEN`, `CURSOR_API_KEY`, `OPENAI_API_KEY`, `XAI_API_KEY` (Grok), `ZAI_CODING_API_KEY` (Claude Code on Z.ai GLM via `api-base`), or `AZURE_OPENAI_API_KEY` + the repo variables `AZURE_OPENAI_BASE_URL` / `AZURE_OPENAI_MODEL_DAILY` (Codex on Azure Foundry); the in-process `openai` leg runs whenever `OPENAI_API_KEY` exists (opt out with the repo variable `SELF_REVIEW_OPENAI_CHAT=false`). Legs whose secret is absent are simply not in the matrix (see `self-review.yml`).

**Optional post-CI companion (Flow B).** After a plan's PR has been pushed and self-review has posted, developers MAY invoke the upstream `apply-review` sub-skill to walk CI findings per-finding (apply / defer / skip) with explicit consent. Read-only by default; never commits or pushes. This is an available option during `/dwp-execute`, not a plan task file.

**Vendor-neutral reminder.** The core DWP methodology has zero dependency on this product. Declining the local reviewer elsewhere is allowed but recorded as a declared exception and reported non-conformant on that point until installed; enabling it here is dogfood + Final Review quality for plans that touch this codebase.

---

## Skills & Agents

Reusable **Skills** (slash commands and one-shot workflows) and **Agents** (specialised personas) live in [.agents/skills/](.agents/skills/) and [.agents/agents/](.agents/agents/). The full catalog with tier classification is in [.agents/docs/skills_agents_catalog.md](.agents/docs/skills_agents_catalog.md).

**This repo also ships a native skill of its own — [`ai-diff-reviewer`](skills/ai-diff-reviewer/SKILL.md)** — the local companion to the shipped GitHub Action. It lives at [`skills/ai-diff-reviewer/`](skills/ai-diff-reviewer/) in the ROOT (not under `.agents/`) because that is where [`skills.sh`](https://skills.sh) scans when consumers run `npx skills add DailybotHQ/ai-diff-reviewer --skill ai-diff-reviewer`. Two CI invariants guarantee action ↔ skill parity: (a) [`code_check.yml`](.github/workflows/code_check.yml) `Skills — prompt-sync invariant` job fails on PRs where `skills/ai-diff-reviewer/prompt.md` diverges from `prompts/default.md`; (b) [`auto-release.yml`](.github/workflows/auto-release.yml) re-syncs the byte-copy AND bumps the skill's frontmatter `version:` field on every release cut so `@v2.0.0` on both surfaces ships the same prompt. Frontmatter is validated in CI against the Open Agent Skills contract by [`scripts/validate-frontmatter.py`](scripts/validate-frontmatter.py) (adapted from the DailybotHQ/agent-skill validator). Consumers layer repo-specific overrides at `.review/extension.md` (auto-detected by the skill; same path can be referenced from CI via the action's `prompt-extension-file:` input for local ↔ CI parity). See [`docs/PROMPTS.md` § "Local coding-agent parity"](docs/PROMPTS.md) for the extension-authoring guide.

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
9. Increase `max_tokens` or `MAX_TURNS` defaults without estimating the cost-per-review impact and documenting it. Since v3 the per-run turn budget comes from `BUDGET_MATRIX` (a row per risk tier, RFC-06) — change a row, not `DEFAULT_MAX_TURNS`, and re-run the Phase 4 per-tier campaign (`docs/PERFORMANCE.md § "Risk-tiered budgets"`) so the recall guard is re-measured.
10. Add a new top-level `action.yml` input "just to support a one-off use case" — every input is a long-lived public contract.
11. Hardcode anything that should be a constant — magic numbers, paths, severity ranks. The top of `scripts/reviewer.py` is the canonical place for runtime constants.
12. Edit content in `.claude/...` or `CLAUDE.md` — both are symlinks. Edit the canonical paths under `.agents/...` and `AGENTS.md`.
13. Spell the action name "AI-Diff-Reviewer" / "AIDR" / "AI/Diff Reviewer" / "AI PR Reviewer" (the old name) in user-facing copy — the canonical user-facing capitalisation is **"AI Diff Reviewer"**. The git repo slug is `ai-diff-reviewer` (renamed 2026-07-14; the old `ai-pr-reviewer` URL still resolves via GitHub's permanent 301 redirect), and the Marketplace listing slug is `ai-diff-reviewer` (derived from `action.yml` `name:`) — they match exactly. Rule #9 has the naming rule; the full chronology is in the [Marketplace rename decision log](docs/STANDARDS.md#marketplace-rename-decision-log).
14. Hand-edit `.agents/skills/ai-diff-reviewer/**` on a feature branch — that's the vendored snapshot of the released version, refreshed automatically by `auto-release.yml` Step 3.5 after each release. Work on the source-of-truth copy at `skills/ai-diff-reviewer/**` instead. Rule #10 has the two-layer dogfooding model.
15. Build a backend URL outside `resolve_endpoint_profile()` / `EndpointProfile.base_url`, read `AIPRR_API_BASE` directly in a provider, or forward a credential to a CLI under a name it does not need — `docs/SECURITY.md § "Custom endpoints"` is the contract (`.review/extension.md` flags all three as `critical`).

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
12. Follow the runner/backend checklist when adding one (`docs/PROVIDERS.md § "Adding a runner or backend"`): endpoint profile → provider class → `DEFAULT_MODELS` + tier row → `action.yml` install step (CLI only, skip-if-present) → `cli-install-smoke` entry → `self-review.yml` leg → `examples/provider-<id>.yml` + index row → README runners table + inputs row → `setup/reference.md` + wizard Q1 table → `docs/SECURITY.md` credential lanes → CHANGELOG.

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

See the [Conventional Commit example](docs/STANDARDS.md#example-commit-message) in `STANDARDS.md § Commits` for a complete message.

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
- Anchor on the most recent `<!-- ai-pr-reviewer-marker -->` comment to identify the authoritative review SHA. Since v3 the self-review runs the RFC-04 ensemble topology: the provider legs emit artifacts and post nothing, and **one** aggregate job publishes the review — its body and tracking comment carry `<!-- ai-pr-reviewer-aggregate -->` beside the marker, one `self-reviewed` label replaces the per-provider labels, and each finding says how many legs reported it.
- The action collapses prior reviews on every push, so reading all comments blindly will mix live and stale feedback.
- Prefer the structured artifact (`review-output/3.0`, uploaded by the aggregate job) over scraping the body: it carries the findings with evidence, verification and agreement, the refuted list and the legs block.

Full workflow + ready-to-copy GraphQL query: [docs/PR_REVIEW_WORKFLOW.md](docs/PR_REVIEW_WORKFLOW.md).

## Small-Batch Delivery

For larger initiatives (multi-provider rollout, prompt overhaul, output schema redesign):

1. Pick only tasks whose dependencies are complete.
2. 1–3 tightly related tasks per PR.
3. Each PR self-reviewable via `self-review.yml`.
4. Verify each batch before starting the next.
5. Keep each batch publishable as a `vX.Y.Z` release behind clear changelog entries.

## Temporary Files (tmp/)

The `tmp/` folder at project root is **git-ignored** and available for scratch
work, inter-agent prompts, data exports, and temporary files. Agents can freely
write to `tmp/` without affecting the repository.

**Nothing inside `tmp/` is ever tracked or committed** — the whole folder is
ignored by git. Write freely (scratch notes, inter-agent prompts, data exports,
query results); it will never show up in `git status` or a diff.

## License

[MIT](LICENSE) — by contributing to this repo you agree your contribution is licensed under MIT.
