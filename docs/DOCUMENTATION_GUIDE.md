# Documentation Guide

How documentation is organised in this repo, who each file is for, and what to update when you change something.

## Audience map

| File | Audience | Purpose |
|---|---|---|
| `README.md` | Marketplace browsers + first-time consumers | Hero copy, quick-start, full input/output table, recipes, FAQ. Scannable in 60 seconds. |
| `AGENTS.md` | AI coding agents + contributors | Source of truth: rules, standards, structure, do/don't, pre-commit checklist. |
| `CLAUDE.md` | Claude Code | Symlink → AGENTS.md. Don't edit. |
| `CHANGELOG.md` | Existing consumers | What changed in each release; impact assessment. |
| `CONTRIBUTING.md` | Prospective contributors | How to set up a dev environment, what's expected in a PR, code of conduct pointer. |
| `LICENSE` | Lawyers, license scanners | MIT verbatim. Don't paraphrase. |
| `docs/PRODUCT_SPEC.md` | Stakeholders, prospective users | What the product is, who it's for, what it does/doesn't do. |
| `docs/ARCHITECTURE.md` | Contributors | Mental model: topology, components, key design decisions. |
| `docs/SECURITY.md` | Security teams considering adoption | Trust model, supply chain, secrets, vulnerabilities. |
| `docs/TESTING_GUIDE.md` | Contributors | What CI runs, how to test locally, why we have so few tests. |
| `docs/DEVELOPMENT_COMMANDS.md` | Contributors | Cheat-sheet of common commands. |
| `docs/DEVELOPMENT_GUIDELINES.md` | Contributors | Python-specific rules: stdlib-only, type hints, error patterns. |
| `docs/STANDARDS.md` | Contributors | Repo-wide conventions: naming, commits, branches, file layout. |
| `docs/DOCUMENTATION_GUIDE.md` | Contributors editing docs | This file — meta-doc on doc organisation. |
| `docs/AI_AGENT_ONBOARDING.md` | AI coding agents (first-time on this repo) | Quick orientation: where to read, what's invariant, what's a public contract. |
| `docs/AI_AGENT_COLLAB.md` | AI coding agents (multi-agent or sub-agent flows) | When to spawn helpers, how to coordinate, escalation. |
| `docs/PR_REVIEW_WORKFLOW.md` | AI coding agents reading review feedback | How to tell live feedback from collapsed/outdated comments. |
| `docs/STRICTNESS.md` | Consumers configuring the gate | Three modes, how to calibrate, downstream patterns. |
| `docs/PROMPTS.md` | Consumers writing custom prompts | Anatomy of a good prompt, illustrative example, prompt caching, and how the two provider families differ in how they apply your prompt (verbatim vs. layered on top of the vendor's own system prompt). |
| `docs/PROVIDERS.md` | Consumers + contributors interested in providers | Both provider families — the chat-completions Anthropic-shape contract and the agent-runner `.aiprr/findings.json` contract — plus the shipping providers (`anthropic`, `claude-code`, `cursor`, `codex`), the roadmap, and gotchas per provider. |
| `docs/PERFORMANCE.md` | Consumers thinking about cost, contributors tuning caps | Cost and latency budget for both provider families: the agentic loop's `MAX_TURNS`/`max_tokens`/conversation pruning on the chat-completions path, and the single vendor-CLI invocation shape on the agent-runner path. |

## Two doc folders, three audiences

Within `docs/`, files split along three audiences:

- **User-facing** (consumers): `STRICTNESS.md`, `PROMPTS.md`, `PROVIDERS.md`, `PERFORMANCE.md`. These are what someone reading the README wants to dig into next.
- **Contributor-facing** (engineers working on this repo): `ARCHITECTURE.md`, `SECURITY.md`, `TESTING_GUIDE.md`, `DEVELOPMENT_COMMANDS.md`, `DEVELOPMENT_GUIDELINES.md`, `STANDARDS.md`, `DOCUMENTATION_GUIDE.md`, `PRODUCT_SPEC.md`.
- **AI-agent-facing** (Claude Code, Cursor, Codex, etc.): `AI_AGENT_ONBOARDING.md`, `AI_AGENT_COLLAB.md`, `PR_REVIEW_WORKFLOW.md`.

We don't enforce sub-folders — the docs are flat in `docs/`. The audience tag in the table above is informal but useful when deciding which docs to update.

## What to update when you change something

| Change | Update |
|---|---|
| New `action.yml` input | `action.yml`, `README.md` (table + at least one example), `CHANGELOG.md`, possibly `examples/` |
| New `action.yml` output | `action.yml`, `README.md` (table), `CHANGELOG.md`, possibly `STRICTNESS.md` if it's gate-related |
| Runtime behaviour change | `CHANGELOG.md`, possibly `ARCHITECTURE.md`, unit test under `tests/` |
| New chat-completions provider | `scripts/reviewer.py` (`Provider` subclass + `build_provider()` dispatch + `DEFAULT_MODELS`), `docs/PROVIDERS.md`, `CHANGELOG.md`, unit tests under `tests/`, smoke-test evidence on a real PR |
| New agent-runner provider (new CLI) | `scripts/reviewer.py` (`AgentRunnerProvider` subclass, reuse `_invoke_cli_agent` / `_build_cli_env`), `action.yml` (new install step + version input), `.github/workflows/code_check.yml` (add matrix leg to `cli-install-smoke`), `.github/workflows/self-review.yml` (add matrix leg with `applied-label`), `docs/PROVIDERS.md`, `examples/provider-<name>.yml`, `CHANGELOG.md`, unit tests under `tests/`, smoke-test evidence on a real PR |
| Default prompt change | `prompts/default.md`, `CHANGELOG.md`, before/after evidence in PR description (ideally across multiple providers, since agent-runner CLIs layer the prompt differently) |
| Process / convention change | `AGENTS.md`, the relevant doc in `docs/`, and a sentence in `CONTRIBUTING.md` if it affects contributors |
| Security-relevant change | `docs/SECURITY.md`, `CHANGELOG.md` (with a `[Security]` tag), unit test if a boundary function is touched |

## Style

### Tone

Direct, technical, no marketing fluff. The README's hero line is allowed to sell; everything past the first section reads like senior-engineer documentation, not landing-page copy.

Avoid:
- Emoji decoration in prose. (Severity emoji in tables and examples are fine.)
- "Magic" / "delight" / "amazing" / "blazing fast".
- Personal pronouns about the reader ("you'll love", "your team will rejoice").
- Long preambles before the actual information.

Prefer:
- Sentences that get to the point.
- Tables when there's a `attribute → value` mapping with more than three rows.
- Code blocks with the actual command, not pseudocode.

### Linking

- Internal links use relative paths (`../README.md`, `STRICTNESS.md`).
- External links go to permanent URLs (commits, tagged versions, archive.org for unreliable sources).
- GitHub references use `#NNN` for issues/PRs and full URLs for cross-repo references.

### Headings

- `#` for the doc title (one per file).
- `##` for top-level sections.
- `###` for subsections.
- Avoid going past `####`. If you need to, the doc probably wants splitting.

### Code blocks

Always include a language tag:

````markdown
```python
def main() -> int:
    return 0
```

```bash
python3 -m py_compile scripts/reviewer.py
```

```yaml
- uses: DailybotHQ/ai-diff-reviewer@v2
```
````

The tag enables syntax highlighting on GitHub and tells the reader what kind of artefact this is.

### Tables

Use tables for short reference data (input lists, output lists, severity mappings). For long, mostly-text "rows", use a bulleted list with bold leading terms instead — tables become hard to read past 5 columns or 30 rows.

## Versioning the docs

Docs version with the code. A change in `scripts/reviewer.py` plus a doc update goes in the same PR.

When cutting a release:
- `CHANGELOG.md` is updated *in the same commit* that bumps to the release tag.
- The README's "Quick start" example pins to `@v2` (or whichever current major), not to a specific patch — so it doesn't need updating per release.
- `docs/PROVIDERS.md` "Status" table updates only when a provider's status actually changes.

## Translations

We don't ship translations. Every doc, comment, and message is in English. Consumers who want translated docs are welcome to fork; we don't maintain non-English copies because the cost of keeping them in sync vastly outweighs the benefit.

## Removing docs

Docs go stale. If a doc is no longer accurate and the underlying topic is no longer relevant:

1. Delete the file.
2. Remove the link from `AGENTS.md`'s documentation table.
3. Note the removal in `CHANGELOG.md` under `[Removed]`.

Don't leave stale docs around with an "outdated, see X" header — that erodes trust in everything else.

## When in doubt

Match the surrounding docs. If you're writing new doc content and you're unsure about tone or structure, find the closest existing doc by audience and copy its scaffolding. Consistency across the doc set matters more than any individual doc being maximally polished.
