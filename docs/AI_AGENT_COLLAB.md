# AI Agent Collaboration

How AI coding agents — running on this repo, possibly across multiple agents and sub-agents — should coordinate. The repo is small enough that most contributions are a single agent making a single change, but the patterns below scale up cleanly when multiple agents touch the same PR.

## Principles

1. **One change per PR.** Don't bundle a provider addition with a prompt rewrite with a CI refactor. Three PRs are easier to review and revert than one.
2. **Read before writing.** The repo is ~4000 LOC of runtime + ~300 lines of `action.yml` + a stdlib-`unittest` suite in `tests/` + a handful of docs + the local companion skill pack under `skills/ai-diff-reviewer/`. You can read the runtime in an hour and the skill pack in another 30 minutes. Do.
3. **Match existing patterns.** This repo's consistency is a feature. New code should look like the surrounding code unless there's a deliberate reason to break the pattern.
4. **Update docs in the same PR as the code.** Out-of-sync docs are worse than no docs.
5. **Self-review before requesting human review.** The action runs on every PR with an always-on Anthropic baseline and scoped CLI-provider legs for provider-sensitive changes; it's the first reviewer you need to satisfy.

## When to spawn a sub-agent

Spawn a specialised agent (architect, debugger, security-auditor, etc.) when:

- The task spans multiple cross-cutting concerns and you'd benefit from independent perspectives.
- The task needs a deep, focused investigation (debugging an obscure failure, auditing security trade-offs).
- You want a "second opinion" on an ambiguous design decision.
- The investigation will produce findings you don't want to clutter the main conversation context with.

Don't spawn a sub-agent when:

- The task is a single straightforward change ("fix typo", "rename variable", "add input").
- You'd just be passing the question through verbatim.
- The cost of context-switching exceeds the benefit of specialisation.

The available specialised agents for this repo live in `.agents/agents/`. The current set:

| Agent | Use for |
|---|---|
| `prompt-engineer` | Tuning `prompts/default.md`; evaluating prompt changes against real PRs; thinking about severity calibration; navigating the layered-prompt semantics of the agent-runner family. |
| `provider-implementer` | Adding a new provider in either family — a `Provider` implementation for chat-completions APIs (OpenAI, Gemini, Bedrock) or an `AgentRunnerProvider` implementation for a new CLI. Knows both contracts and the translation gotchas for each. |
| `reviewer` | Code review specialist for this repo's standards (stdlib-only, type hints, error patterns, action.yml contract, `_invoke_cli_agent` / `_build_cli_env` / `shlex.split` invariants). |

The catalog with full descriptions is at [.agents/docs/skills_agents_catalog.md](../.agents/docs/skills_agents_catalog.md).

## When to use a Skill / slash command

Skills are reusable workflows. Useful for repetitive operations:

| Skill | When |
|---|---|
| `/commit` | Generate a Conventional Commits message from staged diff. |
| `/pr` | Generate a PR description from the branch's commits. |
| `/release` | Cut a new `vX.Y.Z` tag and publish a GitHub Release manually (fallback for when `auto-release.yml` can't do it for you). |
| `/prompt-test` | Smoke-test a prompt change against a real PR. |
| `/add-provider` | Scaffold a new provider — asks first which family (`Provider` for chat-completions, `AgentRunnerProvider` for a new CLI) and then produces the right skeleton for the chosen path. |

Defined in `.agents/commands/`.

## Coordinating across agents

If two agents are working on overlapping areas in the same conversation:

- **Sequential by default.** One finishes its change, commits, and then the other agent starts. Avoids merge conflicts and inconsistent state.
- **Parallel only with disjoint files.** If agent A is editing `scripts/reviewer.py` and agent B is editing `docs/PROMPTS.md`, parallel is fine. If both are touching `scripts/reviewer.py`, run them sequentially.
- **Communicate via the PR description.** Update the PR description as you make significant changes so a later agent (or human) can reconstruct the chain of decisions.

## Coordinating with human reviewers

The action ships to a global audience. Human review remains essential for:

- Architecture changes (anything past a single-function refactor).
- Public-contract changes (`action.yml`).
- New provider implementations.
- Major prompt overhauls.
- CHANGELOG entries that include `[Security]`.

When you make a change in any of these categories, leave a clear PR description that explains:

1. What changed.
2. Why.
3. What you considered and rejected.
4. What evidence (self-review run, manual smoke test, before/after comparison) supports the change.

Reviewers shouldn't have to dig through tool outputs to understand the change. A clean PR description is the courtesy that makes review fast.

## Escalation

If you're stuck:

1. **Re-read AGENTS.md.** Often the answer is there and you missed it.
2. **Read the closest existing code/doc.** Pattern-match to similar problems.
3. **Open a draft PR** with your best attempt and a question in the description. Other agents and humans can iterate on the draft.
4. **Open an issue** if the question is bigger than a single PR (e.g. "should we add provider X?").

Don't sit on a blocker silently. Don't push half-baked code to mainline. Don't bypass the dogfooding step "just this once".

## DWP Security Review + AI Diff Reviewer addon (Flow B)

This repo enables the Deep Work Plan **AI Diff Reviewer addon** in **Flow B** (documented in [`AGENTS.md`](../AGENTS.md) § Deep Work Plan). When executing a plan's mandatory Security Review:

1. Run the usual SR checklist (keep [`docs/SECURITY.md`](SECURITY.md) current).
2. **Additionally** invoke the local companion skill's parent default flow ("Review my current branch") when `.agents/skills/ai-diff-reviewer/` and `.review/extension.md` are both present. Append the output under `## AI Diff Reviewer local review` in the plan's `analysis_results/SECURITY_REVIEW.md`.
3. Soft-fail the local pass only on missing skill/extension or invocation errors; once a review ran, open `critical` findings still block SR completion.
4. After push, optionally run `apply-review` to walk CI findings from `self-review.yml` — never as an extra plan task.

Do not install a consumer `pr-review.yml` in this repo; dogfood CI is already [`self-review.yml`](../.github/workflows/self-review.yml).

## Working with the dogfood loop

Every PR triggers `.github/workflows/self-review.yml`, which runs the action against itself. The direct `anthropic` leg is the always-on baseline. The `claude-code`, `cursor`, and `codex` legs are present in the matrix but only invoke the LLM when the diff touches provider-sensitive action/runtime surfaces. Each active leg posts an independent review with a distinct `self-reviewed:<provider>` label so you can tell them apart in the PR conversation.

- **Watch the active tracking comments.** Each transitions in-place from `Working…` to `View review →` (or `failed`). If a leg's API-key secret is not set on the repo, or the scope gate decides that a CLI-provider leg is unnecessary for this diff, that leg emits a `::notice::` and short-circuits before invoking the reviewer.
- **Read the inline comments per leg.** Provider-family behaviour can diverge: the `anthropic` leg is the strictest control (this action drives the loop), the three CLI legs may find different classes of issue because their vendor CLIs run their own tools.
- **Apply suggestion blocks if any bot's fix is right.** GitHub's one-click apply is fast; it doesn't matter which leg suggested it.
- **Push a follow-up commit if a leg is wrong.** The next run reviews the new HEAD across the baseline leg and any CLI legs enabled by the scope gate.
- **Don't manually dismiss the bots' comments.** They auto-collapse on the next push via `collapse-previous`. Manual dismissal hides the history.
- **A single-leg failure is signal, not noise.** If `codex` fails but the other three succeed, the fault is almost certainly in the `CodexProvider` path (or the `codex` CLI itself), not shared code. Narrow the investigation before touching shared helpers.

## When the bot is wrong

The bot is wrong sometimes. The recourse depends on the kind of wrongness:

- **Wrong on a single PR (one-off).** Push a fix or argue back in a comment. Move on.
- **Wrong systematically (a pattern of misclassifying severity, missing a class of issue).** That's prompt-engineering signal. Open a PR that updates `prompts/default.md`, with examples of the pattern in the description.
- **Wrong because of a bug in `scripts/reviewer.py`.** Open a PR with the fix and a self-review run that demonstrates the fix works.

Don't disable the gate. Don't pin to an older version to silence false positives. Iterate on the prompt or the code instead.

## Communicating outcomes

When you finish a non-trivial change:

- The PR description is the canonical record of what shipped.
- The CHANGELOG entry is the record of what consumers see.
- The commit messages are the record of how the change came together.

These three are read in different contexts; keep all three honest.

## What this repo doesn't have

To set expectations, things you might be used to from other codebases that **don't exist here** and shouldn't be added casually:

- A third-party test runner. We use stdlib `unittest` deliberately — no `pytest`, no `nose`. The suite runs on a bare Python install.
- A linter / formatter in CI.
- A coverage tool.
- A type-checker in CI (mypy is fine to run locally; not enforced).
- A bot for stale issues / PRs.
- A reviewer-rotation tool.
- A code-owner enforcement on every file.
- A merge queue (the dogfood loop assumes single-PR-at-a-time semantics).

Each of those is a real cost. We've decided the cost outweighs the benefit at this repo's size. If a contribution wants to change that calculus, the PR needs to make the case explicitly.

What we **do** have (post v1.1.0) that earlier versions of this doc claimed we didn't:

- A stdlib `unittest` suite (242 tests across four files in `tests/`) that runs in CI.
- A CLI-installer smoke matrix (`cli-install-smoke` in `code_check.yml`).
- Automated releases from Conventional Commits (`auto-release.yml`), so no manual tag-and-push step.
- A local companion skill (`skills/ai-diff-reviewer/`) shipped alongside the Action, with two CI invariants (`Skills — prompt-sync invariant` in `code_check.yml`; `npx skills update` smoke in `auto-release.yml` Step 3.5) keeping the two surfaces in lock-step.
