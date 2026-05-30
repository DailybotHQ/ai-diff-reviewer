# Skills & Agents Catalog

The full inventory of specialised personas (`.agents/agents/`) and reusable workflows (`.agents/skills/`) shipped with this repo. Slash commands (`.agents/commands/`) are referenced from [COMMANDS_REFERENCE.md](COMMANDS_REFERENCE.md).

For the philosophy of why these exist and when to use each, see [docs/AI_AGENT_COLLAB.md](../../docs/AI_AGENT_COLLAB.md).

## Tier model

| Tier | Use case | Suggested model |
|------|----------|-----------------|
| 1 — Light | Trivial fixes, doc edits, quick lookups | Haiku / cheap-fast |
| 2 — Standard | Single-file features, focused refactors | Sonnet / standard |
| 3 — Heavy | Architecture, prompt redesign, provider implementation | Opus / frontier |

## Agents

| Agent | Tier | Scope | Use when |
|---|---|---|---|
| [`reviewer`](../agents/reviewer.md) | 2 | Code review and standards enforcement | After any non-trivial change to `scripts/reviewer.py`, `action.yml`, or `prompts/default.md`. |
| [`prompt-engineer`](../agents/prompt-engineer.md) | 3 | System-prompt design and evaluation | Substantive prompt changes; severity-calibration shifts; investigating systematic misclassifications. |
| [`provider-implementer`](../agents/provider-implementer.md) | 3 | Provider implementation and message-shape translation | Adding a new LLM provider (OpenAI, Gemini, Bedrock, self-hosted). |

## Skills

| Skill | Tier | Intent | Use when |
|---|---|---|---|
| [`release`](../skills/release/SKILL.md) | 2 | release | Cutting a new `vX.Y.Z` tag and publishing the GitHub Release. |
| [`prompt-test`](../skills/prompt-test/SKILL.md) | 2 | evaluate | Producing before/after evidence for a prompt change. Required for any non-trivial `prompts/default.md` PR. |
| [`add-provider`](../skills/add-provider/SKILL.md) | 3 | scaffold | Scaffolding a new `Provider` implementation (class, registry, defaults, inputs, docs). |

## Slash commands

The full reference lives in [COMMANDS_REFERENCE.md](COMMANDS_REFERENCE.md). Quick map:

| Command | Backed by | Tier |
|---|---|---|
| `/commit` | inline procedure in [.agents/commands/commit.md](../commands/commit.md) | 1 |
| `/branch` | inline procedure in [.agents/commands/branch.md](../commands/branch.md) | 1 |
| `/pr` | inline procedure in [.agents/commands/pr.md](../commands/pr.md) | 2 |
| `/code-review` | inline procedure in [.agents/commands/code-review.md](../commands/code-review.md) | 2 |
| `/release` | [skills/release](../skills/release/SKILL.md) | 2 |
| `/prompt-test` | [skills/prompt-test](../skills/prompt-test/SKILL.md) | 2 |
| `/add-provider` | [skills/add-provider](../skills/add-provider/SKILL.md) | 3 |

## Adding a new agent

1. Create `.agents/agents/<name>.md` using the existing files as templates. The frontmatter is the contract:

   ```yaml
   ---
   name: <name>
   description: <one-line description that explains when to use this agent>
   tools: <comma-separated list of tools the agent is allowed to use>
   model: <haiku | sonnet | opus>
   permissionMode: default
   tier: <1 | 2 | 3>
   scope: <short description of focus area>
   can-execute-code: <true | false>
   can-modify-files: <true | false>
   ---
   ```

2. Document the role, when to use, when NOT to use, the workflow, and the tone.
3. Add a row to the table above.
4. PR with the new agent.

## Adding a new skill

1. Create `.agents/skills/<name>/SKILL.md`. Frontmatter:

   ```yaml
   ---
   name: <name>
   description: <one-line description>
   disable-model-invocation: false
   allowed-tools: <comma-separated list>
   model: <haiku | sonnet | opus>
   tier: <1 | 2 | 3>
   intent: <fix | add | scaffold | evaluate | release | review | …>
   max-files: <integer cap>
   max-loc: <integer cap on LOC changed>
   ---
   ```

2. Document the objective, non-goals, inputs, pre-flight, steps, and quality gates.
3. If the skill should be invocable as a slash command, add `.agents/commands/<name>.md` that references the skill.
4. Add a row to the table above.
5. PR with the new skill.

## Removing a skill or agent

If a skill or agent has lapsed into uselessness (e.g. the workflow it automated no longer exists):

1. Delete the `.md` file (or directory).
2. Remove the row from the catalog above.
3. Remove the slash-command alias if it had one.
4. If anything else in the repo links to it, update those links.

Stale entries with "deprecated" headers erode trust in the rest of the catalog. Either it's load-bearing or it's gone.
