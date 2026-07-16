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
| [`provider-implementer`](../agents/provider-implementer.md) | 3 | Provider implementation across **both** families (chat-completions + agent-runner) | Adding a new LLM provider — raw-API family (OpenAI, Gemini, Bedrock, self-hosted vLLM/Ollama) **or** coding-agent CLI family (Aider, Continue, Copilot CLI, …). |

## Skills

| Skill | Tier | Intent | Use when |
|---|---|---|---|
| [`release`](../skills/release/SKILL.md) | 2 | release | Cutting a new `vX.Y.Z` tag and publishing the GitHub Release. |
| [`prompt-test`](../skills/prompt-test/SKILL.md) | 2 | evaluate | Producing before/after evidence for a prompt change. Required for any non-trivial `prompts/default.md` PR. |
| [`add-provider`](../skills/add-provider/SKILL.md) | 3 | scaffold | Scaffolding a new provider — either chat-completions (`Provider`) or agent-runner (`AgentRunnerProvider`). Handles class, registry, defaults, action.yml inputs, install steps, dogfooding matrix legs, examples, docs. |
| [`deepworkplan`](../skills/deepworkplan/SKILL.md) | 3 | methodology | Structured plan-execute-verify loop for novel/large work. Router + eight sub-skills (`create`, `execute`, `refine`, `resume`, `status`, `verify`, `onboard`, `author`). Opt-in addons under `addons/` include Dailybot and **AI Diff Reviewer** (Flow A/B Security Review augmentation). Backed by the `dwp-*` / `skill-create` / `agent-create` slash commands. **Vendored** from `DailybotHQ/deepworkplan-skill` (**v2.17.0+**) and pinned via [`skills-lock.json`](../../skills-lock.json). |
| [`dailybot`](../skills/dailybot/SKILL.md) | 2 | integration | Report progress, check messages, complete check-ins, give kudos, submit forms, and send chat / email through Dailybot. Router + sub-skills (`report`, `messages`, `email`, `health`, `checkin`, `kudos`, `teams`, `forms`, `chat`, `ask`, `env`, `channels`, `conversation`, `workflow`). **Vendored** from `DailybotHQ/agent-skill` and pinned via [`skills-lock.json`](../../skills-lock.json). Wires into the DWP Dailybot addon at [`../skills/deepworkplan/addons/dailybot/`](../skills/deepworkplan/addons/dailybot/). |
| [`ai-diff-reviewer`](../skills/ai-diff-reviewer/SKILL.md) | 2 | review | Local companion to the shipped Action (parent default flow + `generate-extension` / `setup` / `open-pr` / `apply-review`). **Vendored dogfood snapshot** of the released skill (source of truth for consumers is [`skills/ai-diff-reviewer/`](../../skills/ai-diff-reviewer/) at repo root). Also the detection target for the DWP **AI Diff Reviewer addon** (Flow B in this repo — see [`AGENTS.md`](../../AGENTS.md)). |

### Vendored skills and the lockfile

Three skills under `.agents/skills/` are **vendored dogfood copies**, installed and pinned via the [`skills.sh`](https://skills.sh) CLI (`npx skills`). The lockfile at repo root — [`skills-lock.json`](../../skills-lock.json) — records the source repo and a content hash for each:

```json
{
  "version": 1,
  "skills": {
    "ai-diff-reviewer": { "source": "DailybotHQ/ai-diff-reviewer",  "sourceType": "github", "skillPath": "skills/ai-diff-reviewer/SKILL.md", "computedHash": "…" },
    "dailybot":         { "source": "DailybotHQ/agent-skill",       "sourceType": "github", "skillPath": "skills/dailybot/SKILL.md",         "computedHash": "…" },
    "deepworkplan":     { "source": "DailybotHQ/deepworkplan-skill", "sourceType": "github", "skillPath": "skills/deepworkplan/SKILL.md",     "computedHash": "…" }
  }
}
```

Common workflows:

| Task | Command |
|---|---|
| Restore vendored skills from the lockfile (fresh clone) | `npx skills experimental_install` |
| Bump vendored skills to the latest upstream releases | `npx skills update deepworkplan dailybot ai-diff-reviewer` |
| Re-add a single vendored skill from scratch | `npx skills add DailybotHQ/agent-skill --skill dailybot -y` |
| List everything currently installed for this project | `npx skills list` |

The in-house entries (`release`, `prompt-test`, `add-provider`, and the agent personas) are authored directly in this repo and **not** tracked by the lockfile — modifying them just means editing files under `.agents/`. Do **not** hand-edit the vendored `.agents/skills/ai-diff-reviewer/` snapshot on a feature branch; change [`skills/ai-diff-reviewer/`](../../skills/ai-diff-reviewer/) instead (AGENTS.md Rule #10).

### Iteration-Aware Review (IAR) coverage across the catalog

The IAR subsystem did **not** warrant a new dedicated skill or agent — the design patterns it introduced (content-anchored fingerprinting, HTML-comment embedded state, prompt-splicing addenda) are all domain-specific to the reviewer. Instead, IAR coverage is layered onto the existing catalog as follows:

- **`reviewer` agent** — "Iteration-Aware Review (IAR) contract" checklist item (Section 11 in the agent's review checklist) so any PR touching IAR code paths is reviewed against the load-bearing invariants (critical-always-surfaces rail, whitelist-fallback for policy, argv-list subprocess, `_parse_state_from_marker_body` failure discipline, try/except safety wrap around every IAR call site).
- **`prompt-engineer` agent** — "Iteration-Aware Review (IAR) prompt addendum" section codifying the design constraints of `IAR_EXHAUSTIVE_PROMPT_ADDENDUM` (hardcoded module constant, additive to base prompt, round-1-of-generation only, ~150 tokens).
- **`docs/TESTING_GUIDE.md`** — "Failure-fallback regression suites for cross-cutting subsystems (repo convention)" section promotes the `test_<feature>_failure_fallback.py` file naming convention as a repo standard for any cross-cutting subsystem whose failure mode must NOT crash the runtime. IAR's own file at `tests/test_iar_failure_fallback.py` is the reference implementation.
- **`.review/extension.md`** — "Iteration-Aware Review (IAR) conventions" section that codifies IAR-specific rules the CI reviewer enforces on every PR.

Future opportunity: a general-purpose `stateful-review-loop` skill that abstracts the IAR patterns (fingerprint / dedup / policy / generation) for other AI-driven CI tools. Not shipped because the IAR patterns are still evolving and would benefit from more empirical dogfood evidence before abstraction. Track as `CATALOG-FOLLOWUP-1`.

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
| `/dwp-create` | thin delegator → [skills/deepworkplan/create](../skills/deepworkplan/create/SKILL.md) | 2 |
| `/dwp-execute` | thin delegator → [skills/deepworkplan/execute](../skills/deepworkplan/execute/SKILL.md) | 2 |
| `/dwp-refine` | thin delegator → [skills/deepworkplan/refine](../skills/deepworkplan/refine/SKILL.md) | 2 |
| `/dwp-resume` | thin delegator → [skills/deepworkplan/resume](../skills/deepworkplan/resume/SKILL.md) | 2 |
| `/dwp-status` | thin delegator → [skills/deepworkplan/status](../skills/deepworkplan/status/SKILL.md) | 1 |
| `/dwp-verify` | thin delegator → [skills/deepworkplan/verify](../skills/deepworkplan/verify/SKILL.md) | 2 |
| `/skill-create` | thin delegator → [skills/deepworkplan/author](../skills/deepworkplan/author/SKILL.md) | 2 |
| `/agent-create` | thin delegator → [skills/deepworkplan/author](../skills/deepworkplan/author/SKILL.md) | 2 |

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
