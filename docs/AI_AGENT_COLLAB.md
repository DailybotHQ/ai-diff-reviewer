# AI Agent Collaboration

How AI coding agents — running on this repo, possibly across multiple agents and sub-agents — should coordinate. The repo is small enough that most contributions are a single agent making a single change, but the patterns below scale up cleanly when multiple agents touch the same PR.

## Principles

1. **One change per PR.** Don't bundle a provider addition with a prompt rewrite with a CI refactor. Three PRs are easier to review and revert than one.
2. **Read before writing.** The repo is ~1500 LOC of runtime + ~200 lines of `action.yml` + a handful of docs. You can read the whole thing in an hour. Do.
3. **Match existing patterns.** This repo's consistency is a feature. New code should look like the surrounding code unless there's a deliberate reason to break the pattern.
4. **Update docs in the same PR as the code.** Out-of-sync docs are worse than no docs.
5. **Self-review before requesting human review.** The action runs on every PR; it's the first reviewer you need to satisfy.

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
| `prompt-engineer` | Tuning `prompts/default.md`; evaluating prompt changes against real PRs; thinking about severity calibration. |
| `provider-implementer` | Adding a new `Provider` implementation (OpenAI, Gemini, etc.); translating between provider message shapes. |
| `reviewer` | Code review specialist for this repo's standards (stdlib-only, type hints, error patterns, action.yml contract). |

The catalog with full descriptions is at [.agents/docs/skills_agents_catalog.md](../.agents/docs/skills_agents_catalog.md).

## When to use a Skill / slash command

Skills are reusable workflows. Useful for repetitive operations:

| Skill | When |
|---|---|
| `/commit` | Generate a Conventional Commits message from staged diff. |
| `/pr` | Generate a PR description from the branch's commits. |
| `/release` | Cut a new `vX.Y.Z` tag and publish a GitHub Release. |
| `/prompt-test` | Smoke-test a prompt change against a real PR. |
| `/add-provider` | Scaffold a new `Provider` implementation. |

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

## Working with the dogfood loop

Every PR triggers `.github/workflows/self-review.yml`, which runs the action against itself.

- **Watch the tracking comment.** It transitions in-place from `Working…` to `View review →` (or `failed`). If it gets stuck, something in the runtime crashed and didn't reach the failure-update path — that's a bug worth fixing immediately.
- **Read the inline comments.** Even when you're confident in the change, the bot will sometimes flag something worth thinking about.
- **Apply suggestion blocks if the bot's fix is right.** GitHub's one-click apply is fast.
- **Push a follow-up commit if the bot is wrong.** The next run reviews the new HEAD.
- **Don't manually dismiss the bot's comments.** They auto-collapse on the next push via `collapse-previous`. Manual dismissal hides the history.

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

- A test runner. Compile-check + dogfood is the bar.
- A linter / formatter in CI.
- A coverage tool.
- A type-checker in CI (mypy is fine to run locally; not enforced).
- A bot for stale issues / PRs.
- A reviewer-rotation tool.
- A code-owner enforcement on every file.
- A merge queue (the dogfood loop assumes single-PR-at-a-time semantics).

Each of those is a real cost. We've decided the cost outweighs the benefit at this repo's size. If a contribution wants to change that calculus, the PR needs to make the case explicitly.
