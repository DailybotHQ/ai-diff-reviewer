# Commands Reference

Every `.md` file in `.agents/commands/` becomes a slash command. The full set, with what each does and where the procedure lives.

In Claude Code, type `/<name>`. In Codex / Cursor / Gemini, type `#<name>` (or whatever the agent's prefix is).

| Command | One-liner | Procedure |
|---|---|---|
| `/branch` | Generate a branch name in the project's `<type>/<short-kebab>` convention. | [commands/branch.md](../commands/branch.md) |
| `/commit` | Generate a Conventional Commits message for the staged diff. | [commands/commit.md](../commands/commit.md) |
| `/pr` | Generate a PR description from the branch's commits. | [commands/pr.md](../commands/pr.md) |
| `/code-review` | Run a focused review on the current branch's changes. | [commands/code-review.md](../commands/code-review.md) |
| `/release` | Cut a new `vX.Y.Z` release tag and publish a GitHub Release. | [commands/release.md](../commands/release.md) → [skills/release](../skills/release/SKILL.md) |
| `/prompt-test` | Smoke-test a prompt change with before/after on real PRs. | [commands/prompt-test.md](../commands/prompt-test.md) → [skills/prompt-test](../skills/prompt-test/SKILL.md) |
| `/add-provider` | Scaffold a new `Provider` implementation. | [commands/add-provider.md](../commands/add-provider.md) → [skills/add-provider](../skills/add-provider/SKILL.md) |

## How commands map to skills

Some commands are pure procedures (no skill backing) — for example `/commit` and `/branch`, which generate text and don't require multi-file orchestration. Others (`/release`, `/prompt-test`, `/add-provider`) are thin wrappers that read the corresponding skill's `SKILL.md` and follow its steps. The split is:

- **Pure command** — single procedure, ~50 lines or less, no shared logic with other commands. Lives in `commands/`.
- **Skill-backed command** — multi-step workflow, file-touching, shared logic (e.g. validation pre-flight). Logic lives in `skills/<name>/SKILL.md`; the command file is a one-line pointer.

When in doubt, write it as a pure command first. Promote to a skill if the procedure grows past ~50 lines or starts duplicating logic with another command.

## Adding a new command

1. Create `.agents/commands/<name>.md`. Use the existing files as templates. Frontmatter:

   ```yaml
   ---
   description: <one-line description>
   ---
   ```

2. Write the procedure inline. If it's >50 lines, factor it into a skill at `.agents/skills/<name>/SKILL.md` and have the command file just point at it.
3. Add a row to the table above.
4. Add a row to the table in [skills_agents_catalog.md](skills_agents_catalog.md).
5. PR with the new command.

## Reserved names

Don't shadow Claude Code's built-in commands (`/help`, `/clear`, `/config`, `/agents`, `/skills`, `/cost`, `/init`, etc.). The names below are project-specific and safe.

## Style for command procedures

- **Lead with the steps**, not the philosophy. The agent invoking this needs to *do* something; explain why later if needed.
- **Use numbered steps** for sequences and checklists for verifications.
- **Show concrete commands** — the actual `git`, `gh`, `python3` invocation, not pseudocode.
- **Include pre-flight checks** — the things that should be true before the procedure starts.
- **Include failure modes** at the bottom — what can go wrong and how to recover.
