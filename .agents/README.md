# `.agents/` — Canonical Agent Configuration

This directory is the **single source of truth** for every AI coding agent working on this repository — Claude Code, Cursor, OpenAI Codex, Google Gemini, GitHub Copilot, OpenClaw, and anything else that supports a shared agent-config layout.

`.claude/` at the repo root is a tracked symlink → `.agents/` for back-compat with Claude Code. Always reference `.agents/...` in new docs and commit messages, never `.claude/...`.

## Contents

```
.agents/
├── README.md                          # This file
├── settings.json                      # Harness settings (env, Dailybot hooks)
├── agents/                            # Sub-agent definitions (specialised personas)
│   ├── prompt-engineer.md
│   ├── provider-implementer.md
│   └── reviewer.md
├── commands/                          # Slash commands (every .md becomes /<name>)
│   ├── add-provider.md
│   ├── agent-create.md                # → skills/deepworkplan/author (thin delegator)
│   ├── branch.md
│   ├── code-review.md
│   ├── commit.md
│   ├── dwp-create.md                  # → skills/deepworkplan/create
│   ├── dwp-execute.md                 # → skills/deepworkplan/execute
│   ├── dwp-refine.md                  # → skills/deepworkplan/refine
│   ├── dwp-resume.md                  # → skills/deepworkplan/resume
│   ├── dwp-status.md                  # → skills/deepworkplan/status
│   ├── dwp-verify.md                  # → skills/deepworkplan/verify
│   ├── pr.md
│   ├── prompt-test.md
│   ├── release.md
│   └── skill-create.md                # → skills/deepworkplan/author
├── skills/                            # Reusable workflows
│   ├── add-provider/SKILL.md
│   ├── deepworkplan/                  # DWP methodology (installed v2.15.0, SHA256-verified)
│   │   ├── SKILL.md                   # router
│   │   ├── {create,execute,refine,resume,status,verify,onboard,author}/SKILL.md
│   │   ├── addons/                    # dailybot (installed), devcontainer, design-system, dependency-upgrade
│   │   ├── spec/, shared/, guide/, examples/
│   ├── prompt-test/SKILL.md
│   └── release/SKILL.md
└── docs/
    ├── COMMANDS_REFERENCE.md          # Full reference for every slash command
    └── skills_agents_catalog.md       # Catalog with tier classification
```

## Deep Work Plan integration

This repo has the **Deep Work Plan (DWP)** methodology installed at `skills/deepworkplan/`. Eight sub-skills (`create`, `execute`, `refine`, `resume`, `status`, `verify`, `onboard`, `author`) drive the plan-execute-verify loop. Deep-work plan outputs land at the repo-root `.dwp/` directory (gitignored). See the root `AGENTS.md` → "Deep Work Plan" section for the full picture.

**Dailybot addon** (opt-in, already installed): hook enforcement is wired in `settings.json` (Claude Code) and `../.cursor/hooks.json` (Cursor). Repo identity is at `../.dailybot/profile.json` — credential-free.

## How agents discover what's here

- **Claude Code** loads everything under `.claude/` (which is `.agents/` via the symlink) automatically. Slash commands appear as `/<name>` based on the `.md` files in `commands/`. Sub-agents appear via `Task({subagent_type: ...})`.
- **Cursor / Codex / Gemini** can be configured to read `.agents/...` directly; consult their respective configuration docs.
- The single source of truth for *what every agent should do regardless of tool* is the root `AGENTS.md` — this directory is the *machine-readable* configuration that supplements the prose rules there.

## What lives where

| If you want to… | Edit this |
|---|---|
| …change a project rule that applies to all contributors | `../AGENTS.md` |
| …add a slash command | `commands/<name>.md` |
| …add a specialised sub-agent persona | `agents/<name>.md` |
| …add a reusable workflow | `skills/<name>/SKILL.md` |
| …change harness settings (env vars, hooks, etc.) | `settings.json` |
| …list a new skill or agent in the catalog | `docs/skills_agents_catalog.md` |

## Tier model

Skills and agents are tagged by tier in their frontmatter:

| Tier | Use case | Suggested model |
|------|----------|-----------------|
| 1 — Light | Trivial fixes, doc edits, quick lookups | Haiku / cheap-fast |
| 2 — Standard | Single-file features, tests, focused refactors | Sonnet / standard |
| 3 — Heavy | Architecture, prompt redesign, provider implementation | Opus / frontier |

Pick the tier that matches the actual work. Don't put a Tier 3 task on a Tier 1 model just because it's cheap — the model will struggle and the result will need redoing.

## Adding a new skill

1. Create `skills/<your-skill>/SKILL.md`. Use the existing skills as templates (the YAML frontmatter is the contract).
2. Add an entry in `docs/skills_agents_catalog.md`.
3. If the skill has a slash-command entry point, add `commands/<your-skill>.md` that points to the skill.
4. PR with the new skill — keep it focused and well-scoped.

## Adding a new sub-agent

1. Create `agents/<your-agent>.md` with the standard frontmatter (name, description, tools, model, tier, scope).
2. Document the role, when to use, and when NOT to use.
3. Add an entry in `docs/skills_agents_catalog.md`.
4. PR with the new agent.

## Recreating the symlinks

If a clone or filesystem mishandled them:

```bash
cd /path/to/repo
rm -f .claude && ln -s .agents .claude
rm -f CLAUDE.md && ln -s AGENTS.md CLAUDE.md
```

CI does not validate this; if you commit a regular file at `.claude` or `CLAUDE.md` by accident, please re-create the symlinks before pushing.
