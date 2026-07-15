---
name: ai-diff-reviewer
description: Local companion to the AI Diff Reviewer GitHub Action (DailybotHQ/ai-diff-reviewer on GitHub, "AI Diff Reviewer" on the Marketplace). Router for four capabilities — (1) run a local review of the current branch's diff using the SAME methodology as the CI action, (2) generate a repo-tailored `.review/extension.md` via the `generate-extension` sub-skill, (3) install and configure the GitHub Action itself in a repo that doesn't have it yet via the `setup` sub-skill (also doubles as the reference manual for every `action.yml` input), (4) author a well-documented pull request from the current branch's diff (Conventional-Commits title inference, structured body, PR-template merge, `gh pr create`/`edit`) via the `open-pr` sub-skill. Auto-detects `.review/extension.md` (or `.github/ai-diff-reviewer/extension.md` as fallback) and layers it on top of the shipped default prompt for full local↔CI parity. Use when the developer wants a local pre-flight review before pushing, asks "run a code review on my current changes", wants to customize the reviewer to this repo, asks "how do I set up ai diff reviewer?", asks a reference-style question about any of the action's inputs, or asks to "open a PR", "create the pull request", or "write the PR body" for the current branch.
version: "1.6.0"
documentation_url: https://github.com/DailybotHQ/ai-diff-reviewer/blob/main/skills/ai-diff-reviewer/SKILL.md
user-invocable: true
metadata: {"openclaw":{"emoji":"🔍","homepage":"https://github.com/DailybotHQ/ai-diff-reviewer","requires":{"anyBins":["git"]}}}
allowed-tools: Bash, Read, Grep, Glob
---

# AI Diff Reviewer — Local Companion Skill

The **official local companion** to the
[**AI Diff Reviewer**](https://github.com/marketplace/actions/ai-diff-reviewer)
GitHub Action (source: [`DailybotHQ/ai-diff-reviewer`](https://github.com/DailybotHQ/ai-diff-reviewer) —
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

**Source of truth:** <https://github.com/DailybotHQ/ai-diff-reviewer>. License: MIT.

---

## Install

```bash
# Latest v1.x
npx skills add DailybotHQ/ai-diff-reviewer --skill ai-diff-reviewer

# Or pin to a specific tag for reproducibility
npx skills add DailybotHQ/ai-diff-reviewer@v1.4.2 --skill ai-diff-reviewer
```

This vendors the skill into `.agents/skills/ai-diff-reviewer/` in the
consumer repo and records source + content hash in `skills-lock.json` so
any teammate can restore identical bytes with `npx skills experimental_install`.
Bump to the latest with `npx skills update ai-diff-reviewer`.

> **Note on the git repo slug.** The repo path stays at
> `DailybotHQ/ai-diff-reviewer` (historical — published tags v1.0.0–v1.4.2
> anchor the URL space). The `--skill ai-diff-reviewer` flag matches the
> Marketplace listing name; both refer to the same product.

---

## What it does

Four coordinated capabilities, routed by intent:

| Capability | Sub-skill | When it fires |
|---|---|---|
| **Run a local review** | (this file — default flow) | Developer wants CI-parity review of the current branch's diff before pushing |
| **Generate the extension file** | [`generate-extension`](generate-extension/SKILL.md) | Developer wants to customize the reviewer to this repo — "generate a `.review/extension.md` for this project" |
| **Set up the GitHub Action** | [`setup`](setup/SKILL.md) | Developer wants to install and configure the AI Diff Reviewer action in a repo that doesn't have it yet — "set up ai diff reviewer for this repo" |
| **Open a well-documented pull request** | [`open-pr`](open-pr/SKILL.md) | Developer wants to author the PR title + body from the current branch's diff — "open the PR", "create a pull request", "write the PR body", "update the PR description" |

The four sub-skills form a lifecycle: **setup** installs the CI action
once per repo; **generate-extension** tailors the review prompt once
per repo; the parent **run a local review** flow catches issues before
pushing on every branch; and **open-pr** authors the PR that ships the
change to reviewers. The `setup` sub-skill also serves as the
**reference manual** for every `action.yml` input via
[`setup/reference.md`](setup/reference.md) — any coding agent can
answer *"what does `strictness` do?"* without opening the action
source. All four share the same shipped [`prompt.md`](prompt.md) as
the review base.

**First-time bootstrap.** The first time the review flow runs on a repo
with no `.review/extension.md`, the skill asks a single question
(**yes** / **no** / **never**) offering to invoke the `generate-extension`
sub-skill so the review has repo-tailored overrides layered on top of
the base prompt from day one. Declining once (`no`) or forever
(`never`, persists as `.review/.skip-bootstrap`) is fine — the base
prompt still catches ~90% of general-purpose issues. Full flow in Step
2.5 below.

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
- "Tailor the reviewer to our stack"

**Setup flow (install the GitHub Action) — triggers:**

- "Set up AI Diff Reviewer for this repo"
- "Configure the reviewer action"
- "Install the AI Diff Reviewer GitHub Action"
- "Help me create the pr-review workflow"
- "How do I add AI Diff Reviewer to this project?"
- Also fires as the answer to reference-style questions about the
  action — *"what does `strictness` do?"*, *"how do I use
  `label-gate`?"* — via [`setup/reference.md`](setup/reference.md).

**Open-PR flow (author the pull request) — triggers:**

- "Open the PR", "create a pull request for this branch"
- "Draft the PR title and description"
- "Write the PR body"
- "Update the PR description", "rewrite the PR body in the proper format"
- "Make a draft PR" (adds `--draft`)

If the trigger is ambiguous (e.g. developer says "help me with the
review" on a repo that has no `.review/extension.md` yet, or says
"handle the PR" on a repo where a PR both needs a review AND has a
one-line body), ask ONE clarifying question before routing. Heuristics
that help disambiguate:

- Repo already has `.github/workflows/pr-review.yml` (or similar) →
  probably NOT the setup flow.
- Repo has NO workflows and the developer just installed the skill →
  probably the setup flow.
- Developer just finished a session of code changes and hasn't asked for
  a review yet → default review flow.
- Developer just accepted a review's findings and applied fixes →
  probably the open-pr flow (natural next step).

Some harnesses (Claude Code, Cursor) also expose these as slash
commands (`/ai-diff-reviewer`, `/ai-diff-reviewer-generate-extension`,
`/ai-diff-reviewer-setup`, `/ai-diff-reviewer-open-pr`); check the
harness's skill-invocation docs.

---

## Step 0 — Trust boundary

This skill is **near read-only** on the working tree and does **not**
call any remote API. It:

- Reads files from the current git checkout (`Read`, `Grep`, `Glob`).
- Runs `git diff` and `git log` locally (no push, no fetch).
- Composes the review prompt in the agent's context and produces the
  review as terminal output.

The **only** writes it may perform, and only with explicit developer
consent in Step 2.5:

- Create `.review/` and write `.review/extension.md` — if the developer
  answers **yes** to the bootstrap offer (invokes the
  `generate-extension` sub-skill).
- Create `.review/` and touch `.review/.skip-bootstrap` (0 bytes) — if
  the developer answers **never** to the bootstrap offer.

It does **not**:

- Post inline comments to GitHub (that's the CI action's job).
- Modify any source file, workflow, or config file in the working tree.
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

**If a match is found** — read it, append its content to the base prompt
verbatim, and skip to Step 3.

**If no match is found**:

- If `.review/.skip-bootstrap` exists → the developer opted out of the
  bootstrap offer previously. Use the base prompt alone (no announcement),
  skip to Step 3.
- Otherwise → go to **Step 2.5** (first-time bootstrap offer).

Announce the composed configuration in one line, e.g.
`Reviewing feat/foo (a1b2c3d) against main. Base prompt + .review/extension.md.`

The final composed prompt is what governs the review — the severity
definitions, the "what NOT to comment on" rules, the output shape.

---

## Step 2.5 — Offer to bootstrap the extension (first-time only)

This step fires only when Step 2 found no extension file **and** no
`.review/.skip-bootstrap` marker exists. It's the one moment the skill
educates the developer about the extension convention. After the answer
is recorded (either as generated content or an opt-out marker), the
skill never asks again in this repo unless the developer removes the
marker.

Ask the developer ONE question:

> **No `.review/extension.md` found for this repo.**
>
> I can run the review right now with the shipped default prompt — that
> catches ~90% of general-purpose issues (SQL injection, unhandled
> promises, missing input validation, obvious perf regressions, etc.).
>
> But it will miss the **repo-specific** stuff: your money-handling
> conventions, the modules where `console.log` is banned, the RFC-014
> pattern, the always-critical SQL patterns tied to YOUR schema. That's
> what a `.review/extension.md` gives you — file-anchored severity
> overrides written against THIS codebase.
>
> Want to bootstrap one now? (~30 seconds of Discovery + a ~100-line
> file of concrete overrides.)
>
> - **yes** — I'll route to the `generate-extension` sub-skill, then
>   come back and run the review with the fresh extension layered on.
> - **no** — run the review this once with the base prompt only. I'll
>   ask again the next time the skill activates.
> - **never** — never ask again in this repo. I'll create
>   `.review/.skip-bootstrap` (a tracked 0-byte marker). Commit it so
>   your whole team inherits the same preference. To re-enable the
>   offer later, delete the marker.

Handle the response:

- **yes** → invoke the `generate-extension` sub-skill in extension mode
  (see [`generate-extension/SKILL.md`](generate-extension/SKILL.md)).
  When the sub-skill finishes writing `.review/extension.md`, re-enter
  Step 2 from the top — the freshly-written file will be picked up and
  layered onto the base prompt. Do NOT skip the sub-skill's Discovery
  phase (12+ tool calls); that's where the value is.
- **no** → skip to Step 3 with the base prompt alone. Do NOT persist
  anything. The offer fires again next time.
- **never** → run:
  ```bash
  mkdir -p .review
  touch .review/.skip-bootstrap
  ```
  Then skip to Step 3 with the base prompt alone. Suggest the developer
  commit the marker: `git add .review/.skip-bootstrap && git commit -m
  "chore(review): opt out of AI Diff Reviewer bootstrap offer"`.

If the developer's response is ambiguous, default to **no** (the
minimally-disruptive choice) — do not silently opt them out.

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

**Optional next-step hint.** When the review is clean (no 🚨 critical or
⚠️ warning findings) OR when the developer explicitly signals they're
ready to push, close the output with a one-line pointer to the sibling
sub-skill:

```text
Next step: want me to open the PR? — I can draft the title + body from
this same diff (see the `open-pr` sub-skill). Or run `gh pr create`
yourself.
```

Do not print this hint when the review found blocking issues — fix
first, ship second.

---

## Step 5 — Extension file convention (for consumers)

Three ways to end up with an extension file, all valid:

1. **Automated bootstrap** — say "review my branch" on a fresh repo,
   answer **yes** at the Step 2.5 prompt. The `generate-extension`
   sub-skill runs its 12+ tool-call Discovery and writes
   `.review/extension.md` for you. Simplest path — recommended for the
   first setup.
2. **Explicit sub-skill invocation** — say "generate a
   `.review/extension.md` for this repo" (or one of the other triggers
   listed in Activation). Same result as (1) but skips the bootstrap
   prompt. Use this to regenerate or refine an existing file.
3. **Hand-written** — create the file yourself, using the schema and
   examples below. Best when you know exactly what overrides you want
   and don't need the Discovery walkthrough.

Whichever path you take, the layout options are the same:

**Option A — `.review/extension.md`** (recommended):

```
my-repo/
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
- uses: DailybotHQ/ai-diff-reviewer@v1
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
[`docs/PROMPTS.md`](https://github.com/DailybotHQ/ai-diff-reviewer/blob/main/docs/PROMPTS.md).

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
- **Opt-out marker (`.review/.skip-bootstrap`).** A 0-byte tracked
  marker file that tells the skill "don't offer to bootstrap the
  extension anymore in this repo — the team knows the option exists
  and chose to stick with the base prompt." Created by answering
  **never** at the Step 2.5 prompt. Delete the file to re-enable the
  offer. Committing it is the intended behaviour so the whole team
  inherits the same UX.
- **Bugs, feature requests, and extension patterns to add to the
  starter templates:**
  [`github.com/DailybotHQ/ai-diff-reviewer/issues`](https://github.com/DailybotHQ/ai-diff-reviewer/issues).
