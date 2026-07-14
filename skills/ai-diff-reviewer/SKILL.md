---
name: ai-diff-reviewer
description: Local companion to the AI Diff Reviewer GitHub Action (DailybotHQ/ai-pr-reviewer on GitHub, "AI Diff Reviewer" on the Marketplace) — runs the SAME code-review methodology (severity model, tool-use pattern, output format) against the current branch's changes without opening a pull request. Auto-detects and layers repo-specific `.review/extension.md` (or `.github/ai-diff-reviewer/extension.md` as fallback) on top of the shipped default prompt for full parity with what the CI action would report. Use when the developer wants a local pre-flight review before pushing, asks "run a code review on my current changes", or is iterating on prompt-extension rules and wants to test them locally before shipping to CI.
version: "1.4.2"
documentation_url: https://github.com/DailybotHQ/ai-pr-reviewer/blob/main/skills/ai-diff-reviewer/SKILL.md
user-invocable: true
metadata: {"openclaw":{"emoji":"🔍","homepage":"https://github.com/DailybotHQ/ai-pr-reviewer","requires":{"anyBins":["git"]}}}
allowed-tools: Bash, Read, Grep, Glob
---

# AI Diff Reviewer — Local Companion Skill

The **official local companion** to the
[**AI Diff Reviewer**](https://github.com/marketplace/actions/ai-diff-reviewer)
GitHub Action (source: [`DailybotHQ/ai-pr-reviewer`](https://github.com/DailybotHQ/ai-pr-reviewer) —
historical repo slug preserved so `uses:` pins stay stable). It gives your
local coding agent (Cursor, Claude Code, Codex, Gemini, Copilot, Cline,
Windsurf) the exact same review methodology that the CI action would run
on your PR — but on the branch you're working on right now, without pushing.

**Why:** dogfood your PR review before pushing. Catch what CI would catch,
locally, in seconds. Iterate on `prompt-extension-file` rules without a
full push→CI→wait→review cycle.

**Version parity:** this skill ships with the exact same
[`prompt.md`](prompt.md) that the action shipped in the same tagged
release. Pinning `@v1.4.2` for both the action and this skill guarantees
local ↔ CI parity.

**Source of truth:** <https://github.com/DailybotHQ/ai-pr-reviewer>. License: MIT.

---

## Install

```bash
# Latest v1.x
npx skills add DailybotHQ/ai-pr-reviewer --skill ai-diff-reviewer

# Or pin to a specific tag for reproducibility
npx skills add DailybotHQ/ai-pr-reviewer@v1.4.2 --skill ai-diff-reviewer
```

This vendors the skill into `.agents/skills/ai-diff-reviewer/` in the
consumer repo and records source + content hash in `skills-lock.json` so
any teammate can restore identical bytes with `npx skills experimental_install`.
Bump to the latest with `npx skills update ai-diff-reviewer`.

> **Note on the git repo slug.** The repo path stays at
> `DailybotHQ/ai-pr-reviewer` (historical — published tags v1.0.0–v1.4.2
> anchor the URL space). The `--skill ai-diff-reviewer` flag matches the
> Marketplace listing name; both refer to the same product.

---

## What it does

Two coordinated capabilities, routed by intent:

| Capability | Sub-skill | When it fires |
|---|---|---|
| **Run a local review** | (this file — default flow) | Developer wants CI-parity review of the current branch's diff before pushing |
| **Generate the extension file** | [`generate-extension`](generate-extension/SKILL.md) | Developer wants to customize the reviewer to this repo — "generate a `.review/extension.md` for this project" |

Both share the same shipped [`prompt.md`](prompt.md) as the base — one
runs it, the other tailors what layers on top.

## Activation

**Default flow (run a review) — triggers:**

- "Review my current branch"
- "Run a code review on my changes"
- "Do a pre-flight review before I push"
- "Code review the diff against `main`"
- "What would CI say about my current commits?"

**Generate-extension flow — triggers:**

- "Generate a `.review/extension.md` for this repo"
- "Customize the code review for our project"
- "Help me write repo-specific review rules"
- "Set up the AI reviewer for this codebase"
- "Tailor the reviewer to our stack"

If the trigger is ambiguous (e.g. developer says "help me with the
review" on a repo that has no `.review/extension.md` yet), ask ONE
clarifying question before routing.

Some harnesses (Claude Code, Cursor) also expose these as slash
commands (`/ai-diff-reviewer`, `/ai-diff-reviewer-generate-extension`);
check the harness's skill-invocation docs.

---

## Step 0 — Trust boundary

This skill is **read-only** on the working tree and does **not** call any
remote API. It:

- Reads files from the current git checkout (`Read`, `Grep`, `Glob`).
- Runs `git diff` and `git log` locally (no push, no fetch).
- Composes the review prompt in the agent's context and produces the
  review as terminal output.

It does **not**:

- Post inline comments to GitHub (that's the CI action's job).
- Modify any file in the working tree.
- Call the LLM provider directly — it uses the coding agent that's
  already running you.
- Send any data off your machine.

If the coding agent has broader powers (e.g. can write files or run
arbitrary bash), those come from the harness, not this skill.

---

## Step 1 — Detect context

Run these to establish the review's inputs. Emit the JSON to your working
context; do not print it to the user unless they ask.

```bash
# Base branch: prefer the tracked upstream's short name, fall back to `main`.
BASE=$(git rev-parse --abbrev-ref --symbolic-full-name '@{u}' 2>/dev/null | sed 's|.*/||')
BASE="${BASE:-main}"

# Current branch + head SHA
HEAD_BRANCH=$(git branch --show-current)
HEAD_SHA=$(git rev-parse --short HEAD)

# The three artifacts the review needs
git diff --stat "origin/${BASE}...HEAD"    # summary of what changed
git diff "origin/${BASE}...HEAD"           # the actual diff
git log "origin/${BASE}..HEAD" --oneline   # the commit trail
```

If the diff is empty, tell the developer "no changes vs `<BASE>` — nothing
to review" and stop. If `origin/${BASE}` doesn't exist (fresh clone,
missing remote), fall back to `git merge-base main HEAD` and diff against
that; note the fallback in the summary.

---

## Step 2 — Compose the prompt (base + extension)

The review methodology lives in [`prompt.md`](prompt.md) — the exact same
prompt the CI action ships. Read it into your context as the base.

Then check for a **repo-specific extension** in this order of precedence
(first match wins; the rest are ignored):

1. `.review/extension.md` (recommended convention — runtime-agnostic)
2. `.github/ai-diff-reviewer/extension.md` (fallback for teams that
   prefer `.github/` sibling to workflow files;
   `.github/ai-pr-reviewer/extension.md` also accepted for back-compat
   with the pre-v1.5 skill name)

Read the matching file (if any) and append its content to the base prompt
verbatim. If neither exists, use the base prompt alone — no error, no
warning.

Announce the composed configuration in one line, e.g.
`Reviewing feat/foo (a1b2c3d) against main. Base prompt + .review/extension.md.`

The final composed prompt is what governs the review — the severity
definitions, the "what NOT to comment on" rules, the output shape.

---

## Step 3 — Execute the review

Apply the composed prompt to the diff **using the coding agent's own
tools** (Read, Grep, Glob):

- The prompt tells you to `read_file` / `grep` / `glob` — translate those
  to whatever primitives the harness gives you. Read the changed files in
  full; the diff alone is rarely enough context.
- The prompt tells you to `post_inline_comment(path, line, body, severity)`
  — since you are running locally without GitHub write access, instead
  **collect** each finding into an internal list of
  `{path, line, severity, body}` records and print them as a table in
  Step 4.
- The prompt tells you to `submit_review(summary)` **exactly once** — this
  is your cue that the review is complete. When you reach this point,
  print the final summary and stop.

**Cost discipline:** cap yourself at the same number of turns the CI
action does (~25) and the same inline-comment cap (default 20). Don't
grep the whole world; grep the files you're commenting on plus their
imports.

---

## Step 4 — Print the review

Emit the review to the terminal in the **same format** the CI bot would
post on a PR — this is the parity contract:

```markdown
## Verdict
<one sentence — "Looks good", "Blocking security fix needed", etc.>

## Findings

| # | Severity | File | Summary |
|---|----------|------|---------|
| 1 | 🚨 critical | `src/auth.ts:55` | SQL injection in raw-string login query |
| 2 | ⚠️ warning  | `src/cache.ts:120` | Unbounded cache key cardinality |
| 3 | ℹ️ info     | `tests/utils.ts:12` | Helper could be reused from existing fixture |

### 1. `src/auth.ts:55` — 🚨 critical
<the full finding body — 2-4 sentences + optional ```suggestion block```>

### 2. `src/cache.ts:120` — ⚠️ warning
<...>

### 3. `tests/utils.ts:12` — ℹ️ info
<...>

## Notes (no inline anchor)
- <cross-cutting concerns, architecture, test-strategy comments>

**Recommendation:** approve / request-changes / comment-only
```

Reproducing this exact shape (verdict → findings table → per-finding
body → notes → recommendation) is what lets a developer trust "the
local review says X, so CI will say X too."

---

## Step 5 — Extension file convention (for consumers)

If a maintainer wants repo-specific rules layered on top of the base
prompt, create **either**:

**Option A — `.review/extension.md`** (recommended):

```
mi-repo/
├── .review/
│   └── extension.md         ← auto-detected by this skill
└── .github/
    └── workflows/
        └── pr-review.yml    ← CI workflow uses the same file
```

**Option B — `.github/ai-diff-reviewer/extension.md`** (fallback if you
prefer keeping the file next to your workflows). The pre-v1.5 path
`.github/ai-pr-reviewer/extension.md` is still recognised for
back-compat.

The **same file** should be referenced from your CI workflow's
`prompt-extension-file:` input so local and CI stay in perfect sync:

```yaml
# .github/workflows/pr-review.yml
- uses: DailybotHQ/ai-pr-reviewer@v1
  with:
    api-key: ${{ secrets.ANTHROPIC_API_KEY }}
    github-token: ${{ secrets.GITHUB_TOKEN }}
    prompt-extension-file: .review/extension.md   # same file the skill auto-detects
```

Example `.review/extension.md`:

```markdown
## Severity overrides for our codebase

- Any `SELECT * FROM users` in a request path is **critical** (PII exposure).
- Missing `AbortController` on a `fetch()` in `apps/frontend/` is **warning**
  (React 18 pattern we standardized on in RFC-014).

## Don't comment on

- Formatting in `apps/legacy/*` — that module is scheduled for a rewrite.
- Missing tests in `experiments/` — that folder is intentionally exploratory.
```

Full authoring guide (structure, tips, worked examples):
[`docs/PROMPTS.md`](https://github.com/DailybotHQ/ai-pr-reviewer/blob/main/docs/PROMPTS.md).

---

## Notes

- **The skill runs your local agent — it doesn't invoke a separate
  LLM.** If your harness is Cursor and you're on `auto`, the review costs
  are billed to your Cursor Pro subscription. If your harness is Claude
  Code with an API key, it's Anthropic tokens. Either way the local
  review is a "free bonus" if you were going to use the agent anyway.
- **This skill does not replace the CI action.** CI still runs on every
  PR and posts the authoritative review (inline comments, severity
  gating, merge-blocking). The skill is for the "before pushing" moment.
- **Extension parity is guaranteed on your side, not enforced by
  tooling.** If your `.review/extension.md` says something different
  from what your CI workflow's `prompt-extension-file:` points at, you
  get drift. Keep them at the same path.
- **Bugs, feature requests, and extension patterns to add to the
  starter templates:**
  [`github.com/DailybotHQ/ai-pr-reviewer/issues`](https://github.com/DailybotHQ/ai-pr-reviewer/issues).
