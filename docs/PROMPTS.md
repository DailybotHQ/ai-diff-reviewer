# Prompts — making the reviewer yours

The bundled `prompts/default.md` is technology-agnostic and opinionated about severity definitions, what *not* to comment on, and review etiquette. It's a reasonable starting point for any codebase.

But the highest-leverage thing you can do with this action is **write a custom prompt for your team**. A generic prompt produces generic feedback. A prompt that knows your conventions, anti-patterns, and gotchas produces feedback that feels like a senior engineer on your team.

## The shape of a good custom prompt

A high-quality prompt typically has these sections:

1. **Persona and tone** — who is the reviewer, how do they sound, how aggressive are they.
2. **Severity definitions** — `critical`/`warning`/`info` mapped to your team's actual reality (not the generic default).
3. **House rules** — patterns and anti-patterns specific to your codebase, with file:line references to the docs that define them.
4. **What not to comment on** — things your linter, type checker, or formatter already catch, plus subjective taste your team has agreed not to bikeshed.
5. **Output format** — the verdict-then-table format, with severity emoji.

You don't need every section. The ones that move the needle most are sections 2 and 3.

## Illustrative example

The snippet below is a **fictional example** to show the *shape* of a useful prompt — not a recommendation to adopt any particular rule. Replace it entirely with rules that come from your own codebase, your own retrospectives, and your own house style.

```markdown
You are a senior engineer on our team, reviewing a pull request. You are
direct, technical, and prefer specific examples over vague concerns. You
assume the author knows the codebase as well as you do; your job is to
spot the things they didn't catch on their own pass.

## Severity overrides for our codebase

ALWAYS `critical`:
- A new piece of code that introduces a SQL injection or shell injection
  surface. We had an incident in 2024 caused by a string-formatted query;
  treat any `f"... {user_input} ..."` SQL construction as critical.
- A new background job enqueued from inside a database transaction without
  the appropriate "after-commit" hook — duplicates and lost messages have
  been our biggest reliability cost.
- Hard-coded credentials, API keys, or secrets in source code (even
  examples or test fixtures).

ALWAYS `warning`:
- N+1 query patterns inside a loop without an explicit comment justifying
  it. We have a per-endpoint latency budget documented at
  `docs/perf/budgets.md` — flag with that link.
- New cache keys with cardinality that grows in more than one dimension
  (per-user × per-team × per-day) without an upper-bound estimate.

ALWAYS `info` (downgrade from `warning` if the default rubric would say so):
- Function length > 80 lines, but already passing the linter.
- Missing docstrings on internal helpers.

## House rules

- Test files follow the pattern `<module>.test.<ext>` (or whatever your
  team uses).
- Public functions take a `context` object as their first parameter, not
  scattered keyword arguments. See `docs/architecture/contracts.md`.

## Don't comment on

- Issues the formatter or linter will catch — they run in CI already.
- Type-checker output — CI surfaces it directly.
- Subjective naming preferences without a concrete reason.

## Output

End your summary with a Findings table (Severity emoji + file:line + 1-line
summary) and a Recommendation line: approve / request-changes / comment-only.
```

The point of the example is the *structure* — persona, severity overrides, house rules, what-not-to-comment-on, output format. Take the structure; throw out the specific rules and write yours.

## Tips for prompt-writing

- **Cite the doc, not the rule.** Instead of *"don't use raw HTTP status codes"*, say *"raw integer HTTP status codes are forbidden — see AGENTS.md §9"*. The model can then `read_file` the doc and quote the relevant section in its inline comment, which is way more persuasive than a bare assertion.
- **Show, don't tell.** When the rule has nuance, paste the bad/good code:
  ```markdown
  ❌ `return Response(data, status=200)`
  ✅ `return Response(data, status=status.HTTP_200_OK)`
  ```
- **Be explicit about WHEN.** "When adding a new cache key, estimate cardinality" — the model will look for cache keys in the diff and skip the rule otherwise.
- **One file, not many.** A single prompt file is easier to maintain than three. The bundled default is ~250 lines and that's plenty of headroom.
- **Iterate from real PRs.** When the reviewer misses something obvious or flags something it shouldn't, that's data. Update the prompt; the next PR benefits.

## How the action loads your prompt

You have three levers, from least to most invasive:

### Base vs Extension vs Replacement — the decision guide

| Input | What it does | When to use |
|---|---|---|
| _(none)_ | The bundled `prompts/default.md` is the entire system prompt. | Just trying the action out; comfortable with the shipped defaults. |
| `prompt-extension-file:` | The bundled default is the base; your file is APPENDED with a `---` separator. | You mostly agree with the default but want stack-specific severity overrides or house rules. Best for teams: you get every future improvement to the default without merge pain. |
| `prompt-file:` | Your file REPLACES the default. Nothing from `prompts/default.md` remains. | You want full control over persona, tool guidance, severity system. Highest-effort but highest-fidelity. |
| Both set | `prompt-file:` becomes the base; `prompt-extension-file:` is APPENDED to it. | You have a custom base prompt and want to add per-repo or per-environment overrides on top (e.g. one custom base, three different extensions for `main` / `staging` / `experimental`). |

### Example — extend the default

```yaml
- uses: DailybotHQ/ai-diff-reviewer@v2
  with:
    prompt-extension-file: examples/prompts/python-strict.md
```

### Example — full replacement

```yaml
- uses: DailybotHQ/ai-diff-reviewer@v2
  with:
    prompt-file: .github/prompts/our_review_rules.md
```

Both inputs are paths **inside the consumer's checkout** (not the action's checkout). Make sure the files are committed to the same branch the workflow runs on.

### Starter extensions

If you're starting from scratch, copy one of the starter extensions from [`examples/prompts/`](../examples/prompts/) into your own repo:

- [`python-strict.md`](../examples/prompts/python-strict.md) — Python severity overrides.
- [`typescript-strict.md`](../examples/prompts/typescript-strict.md) — TypeScript severity overrides + React gotchas.
- [`security-focused.md`](../examples/prompts/security-focused.md) — OWASP top-10 severity categorization.

Each starter is a short (~40–60 lines) extension file — NOT a copy of the default. Reference it with `prompt-extension-file:`, not `prompt-file:`.

### Generate a fully custom prompt with your own AI

If none of the starter extensions match your stack (or you specifically want `prompt-file` full replacement), use the **meta-prompt** at [`examples/prompts/generate-custom-prompt-meta.md`](../examples/prompts/generate-custom-prompt-meta.md).

Workflow:

1. Copy the meta-prompt into your favorite coding AI (Claude Code, Cursor, Codex, ChatGPT, Gemini) with your repo checked out.
2. The AI analyzes your codebase — technology stack, architecture, security surface, existing quality standards, historical pain points — and produces a repo-specific `prompt-file`.
3. Save the AI's output to `.github/prompts/pr-review.md` in your own repo and reference it via `prompt-file:` in your workflow.
4. Iterate: run one review, read the inline comments, refine any overrides that feel off. Two or three iterations usually gets it dialed in.

This approach beats generic templates for teams whose stack is unusual, whose architecture is unconventional, or who have accumulated a lot of tacit "we learned this the hard way" knowledge worth encoding in the prompt.

## How the prompt is applied per provider family

The two provider families use your prompt slightly differently. Both accept the same file — the difference is where it lands in the model's context.

### Chat-completions family (`anthropic`, future raw OpenAI/Gemini)

Your prompt **is** the system prompt, verbatim. The action owns the tool-use loop and sends `system=<your prompt>` on every turn. This gives you full control: persona, severity rubric, output shape, and house rules all come from your file — the action does not add anything except the tool schema.

### Agent-runner family (`claude-code`, `cursor`, `codex`)

The vendor CLI already has its own tuned system prompt for code review (`claude` has a coding-agent prompt, `cursor-agent` has one, `codex` has one). The action **layers your prompt on top** rather than replacing the vendor's — your file is appended as a `--append-system-prompt`-style directive plus the `.aiprr/findings.json` output-schema directive that makes the file-based findings contract work.

Practical consequences:

- Persona and tone rules still work — they add to whatever the vendor already asks for.
- Severity definitions still work — they overlay the vendor's default severity thinking.
- Output-format instructions in your prompt are **best-effort** — the definitive output contract is `.aiprr/findings.json`, injected by the action after your prompt.
- The vendor CLI's own review skills (running tests, using its native file-search tools, executing local commands with its own sandbox) are still active. Your prompt does not disable them.

If you need the exact same behaviour across providers, use the chat-completions family (`anthropic`) — that's what it exists for. If you want the highest-effort review at the price of some determinism, use the agent-runner family and lean into the vendor's own reviewer strengths.

## Prompt caching

The action sends the system prompt with `cache_control: ephemeral` on every Anthropic call, so a long, opinionated prompt only pays the full token cost on the first turn of each review. Subsequent turns within the same review (and within the ~5-minute cache TTL) read from cache. **Don't worry about prompt length** — go as long as you need to be specific.

Agent-runner providers do their own caching internally (Claude Code, Cursor Agent and Codex all cache their system prompts with the underlying model provider), so the same "long, opinionated prompt is free after the first call" principle applies — you just don't set the cache flag yourself.

## Local coding-agent parity

The action ships a companion **local review skill** ([`skills/ai-diff-reviewer/`](../skills/ai-diff-reviewer/SKILL.md)) that runs the SAME prompt against your current branch — from Cursor / Claude Code / Codex / Gemini / Copilot / Cline / Windsurf — without opening a PR. Two invariants keep the parity real:

1. **The skill's `prompt.md` is a byte-identical copy of `prompts/default.md`.** [`code_check.yml`](../.github/workflows/code_check.yml) has a `Skills — prompt-sync invariant` job that fails PRs where the copy has drifted; [`auto-release.yml`](../.github/workflows/auto-release.yml) re-syncs the copy on every release cut so pinning `@v2.0.0` on both action and skill guarantees you see the same review methodology on both surfaces.
2. **The skill auto-detects the same `prompt-extension-file` your CI uses.** By convention, put repo-specific overrides at `.review/extension.md` and reference the same path from your workflow's `prompt-extension-file:` input.

### Install the skill in a consumer repo

```bash
# Latest v2.x — vendors into .agents/skills/ai-diff-reviewer/ + adds skills-lock.json entry
npx skills add DailybotHQ/ai-diff-reviewer --skill ai-diff-reviewer

# Or pin to a specific tag for reproducibility
npx skills add DailybotHQ/ai-diff-reviewer@v2.0.0 --skill ai-diff-reviewer

# Bump to latest published action tag later
npx skills update ai-diff-reviewer
```

### The `.review/extension.md` convention

Put project-specific rules in **one file** that both surfaces read:

```
my-repo/
├── .review/
│   └── extension.md              ← the extension file (recommended path)
├── .github/
│   └── workflows/
│       └── pr-review.yml         ← CI workflow references the same path
└── (project code)
```

The skill's auto-detection order (first match wins):

1. `.review/extension.md` ← recommended default
2. `.github/ai-diff-reviewer/extension.md` ← fallback for teams that prefer keeping config next to workflow files (pre-v1.5 `.github/ai-pr-reviewer/extension.md` also accepted for back-compat)
3. None found → the skill enters the **first-run bootstrap prompt** (see below) unless `.review/.skip-bootstrap` exists

### First-run bootstrap prompt

The first time the review flow activates on a repo without an extension file, the skill asks ONE question:

> No `.review/extension.md` found. Bootstrap one now? (yes / no / never)

- **yes** → invokes the `generate-extension` sub-skill (see below), then re-enters the review with the fresh extension layered on top of the base prompt.
- **no** → runs the review this once with the base prompt only. The prompt fires again the next time the skill activates.
- **never** → creates `.review/.skip-bootstrap` (a 0-byte tracked marker). The prompt never fires again in this repo. Commit the marker so the whole team inherits the same UX; delete it to re-enable the offer.

**The base prompt alone is fully functional** — it's the same [`prompts/default.md`](../prompts/default.md) that the CI action uses when no `prompt-file`/`prompt-extension-file` are configured, and it catches the ~90% of general-purpose issues (SQL injection, unhandled promises, missing input validation, obvious perf regressions). The bootstrap prompt exists to nudge repos into the higher-value tailored-review path without blocking impatient users or forcing Discovery on repos that genuinely don't need customization.

### The `.review/.skip-bootstrap` marker

| Property | Value |
|---|---|
| **Path** | `.review/.skip-bootstrap` (relative to repo root) |
| **Content** | 0 bytes (presence is the signal) |
| **Created by** | The skill, when the developer answers **never** at the bootstrap prompt |
| **Committed?** | Yes — the whole point is that the team inherits the preference. If left uncommitted, every teammate sees the prompt on their first review run. |
| **To re-enable the offer** | `rm .review/.skip-bootstrap && git commit -am "chore(review): re-enable AI Diff Reviewer bootstrap offer"` |
| **Interaction with extension** | Orthogonal — if you later run `generate-extension` explicitly and end up with both files, the extension is loaded normally (Step 2 wins over Step 2.5). |

Reference the same file from your CI workflow so both surfaces produce the same review:

```yaml
- uses: DailybotHQ/ai-diff-reviewer@v2
  with:
    api-key: ${{ secrets.ANTHROPIC_API_KEY }}
    github-token: ${{ secrets.GITHUB_TOKEN }}
    prompt-extension-file: .review/extension.md
```

### Why `.review/` and not `.github/`?

`.github/` implies "GitHub-specific configuration". The local skill has nothing to do with GitHub — it runs against `git diff origin/<base>...HEAD` on your workstation. A runtime-agnostic dotfolder (`.review/`, following the pattern of `.dwp/`, `.dailybot/`, `.claude/`, `.cursor/`) generalizes cleanly to non-GitHub setups and doesn't overload `.github/` namespace. Both paths are supported for backward compatibility with teams that already keep their prompt overrides next to workflow files.

### What the skill runs

- **`git diff origin/<base>...HEAD`** locally (no fetch, no push).
- **Read / Grep / Glob** through your coding agent's tools (no separate LLM call — you're billed to whatever provider your agent is already using).
- **Produces the review as terminal output** in the same shape the CI bot would post as a PR comment — verdict + findings table + per-finding body + notes + recommendation.

Full workflow details, trust boundary, activation triggers, and step-by-step methodology: [`skills/ai-diff-reviewer/SKILL.md`](../skills/ai-diff-reviewer/SKILL.md).

### Generate a repo-tailored extension automatically

The skill ships a **`generate-extension` sub-skill** that inspects THIS repo (stack, architecture, security surface, existing conventions, historical pain) and writes a tailored `.review/extension.md` for you — no copy-paste, no manual authoring.

Natural-language triggers:

- *"Generate a `.review/extension.md` for this repo"*
- *"Customize the code review for our project"*
- *"Help me write repo-specific review rules"*
- *"Set up the AI reviewer for this codebase"*

The sub-skill runs a mandatory Discovery phase (≥ 12 tool calls covering package manifests, top-level source layout, security-adjacent patterns via `grep`, existing quality standards, and — if `gh` is available — recent bugs) before writing anything. This is the difference between a generic extension that could belong to any repo and one that names specific files, modules, and RFCs.

**Two output modes:**

| Mode | File written | Structure | When |
|---|---|---|---|
| **Extension** (default) | `.review/extension.md` | ~50-150 lines of overrides layered on top of the shipped default prompt | Almost always — cheap iteration, keeps benefiting from upstream default improvements |
| **Full replacement** (advanced) | `.github/prompts/pr-review.md` | 200-500 lines standalone prompt replacing the default entirely | Rare — teams that want total control, or codebases so idiosyncratic that the default is more noise than signal |

The sub-skill asks a single clarifying question ("extension or full replacement?") before generating, defaulting to extension. Full details, quality-gate checklist, and Discovery methodology: [`skills/ai-diff-reviewer/generate-extension/SKILL.md`](../skills/ai-diff-reviewer/generate-extension/SKILL.md); condensed sample outputs in [`skills/ai-diff-reviewer/generate-extension/examples.md`](../skills/ai-diff-reviewer/generate-extension/examples.md).

**Zero-install alternative:** if you don't want to vendor the skill (e.g. using a web chatbot without file-system access), the same discovery-and-generation flow is still available as a copy-paste meta-prompt at [`examples/prompts/generate-custom-prompt-meta.md`](../examples/prompts/generate-custom-prompt-meta.md).

### Bootstrap the GitHub Action with the `setup` sub-skill

The same skill package includes a **`setup` sub-skill** that installs the CI action from scratch — for repos that don't have a `pr-review.yml` workflow yet. Natural-language triggers:

- *"Set up AI Diff Reviewer for this repo"*
- *"Install the AI Diff Reviewer GitHub Action"*
- *"Configure the reviewer action"*

The wizard asks six questions (provider, strictness, trigger mode, external-contributor policy, PR-description mode, complexity labels), uses light Discovery to pre-fill defaults from the repo's stack + visibility + default branch, writes `.github/workflows/pr-review.yml` with only the inputs that differ from defaults, and prints the exact URL to add the required GitHub Secret. At the end it offers to also invoke `generate-extension`, closing the loop from **zero setup → installed → tailored** in a single conversation.

The sub-skill also doubles as the **reference manual** for every `action.yml` input — its [`reference.md`](../skills/ai-diff-reviewer/setup/reference.md) sibling documents each input with description, default, choices, and per-scenario recommendations. Any coding agent with the skill installed can answer *"what does `pr-description-mode: autocomplete` do?"* or *"how do I pin the Claude Code CLI version?"* without opening the action source. Full flow: [`skills/ai-diff-reviewer/setup/SKILL.md`](../skills/ai-diff-reviewer/setup/SKILL.md).

### Author a well-documented PR with the `open-pr` sub-skill

The final piece of the loop: once a local review comes back clean (and the local extension has caught the repo-specific issues before CI ever runs), the **`open-pr` sub-skill** authors a well-structured PR title + body from the current branch's diff and executes via `gh pr create` (new PR) or `gh pr edit` (refresh an existing PR that's still a one-liner).

Natural-language triggers:

- *"Open the PR"*, *"create a pull request for this branch"*
- *"Draft the PR title and description"*, *"write the PR body"*
- *"Update the PR description"*, *"the PR body is a one-liner — rewrite it properly"*
- *"Make a draft PR for this branch"*

What it does, in order:

1. **Reads the branch's diff and commit trail** — infers a Conventional Commits title (`feat(scope): summary`) or the repo's native title style if a `.github/pull_request_template.md` or the commit history reveals a different convention.
2. **Drafts a structured body** with the sections a good PR review actually needs: Summary (the *why*), Changes (the *what*, per file), Test plan (checklist), Related issues (auto-linked from `Fixes #123` / `Refs #456` in commits), Screenshots (when UI files changed), Breaking changes (when applicable), Risks (called out for architectural or security-adjacent diffs).
3. **Merges with `.github/pull_request_template.md` when present** — never overwrites the team's template, layers the generated content into the template's placeholders.
4. **Previews everything to the developer** with a single confirmation before executing — the sub-skill never opens a PR unattended.
5. **Executes via `gh`** — supports `--draft`, stacked PRs against non-default bases, and forks.

Full flow, quality gates, and sample dialogues: [`skills/ai-diff-reviewer/open-pr/SKILL.md`](../skills/ai-diff-reviewer/open-pr/SKILL.md).

Why it belongs in the AI Diff Reviewer skill pack: the diff → review → PR-authoring loop is a single developer workflow. The four sub-skills that operate on the local repo (`review`, `generate-extension`, `setup`, `open-pr`) share the same repo-analysis code and the same `.review/extension.md` context, so the PR body `open-pr` drafts already reflects the review's findings — a `warning: consider adding a regression test` becomes a `Test plan` checkbox in the PR body automatically. `apply-review` (the fifth sub-skill) is different in shape: its input is the **CI review posted back on the PR**, not the local diff, so it doesn't participate in the shared repo-analysis pass — but it uses the same trust boundary, the same severity model, and the same `.review/extension.md` context to interpret findings in the walkthrough. Same skill pack, same conventions; just a different entry point in the loop.

### Close the loop from CI back to local with the `apply-review` sub-skill

Once `open-pr` has posted the PR and the CI action has reviewed it, the **`apply-review` sub-skill** reads the live review comments off the PR (filtering the `isMinimized: true` collapsed history), presents the findings in the same shape as a local review, and walks the developer through applying, deferring, or skipping each one. It shares the parent skill's **no-commit / no-push boundary** but extends it with per-finding *"apply"* consent — on `apply` it uses the `Edit` tool to rewrite the target file's lines the suggestion covers, and on `defer` it may append to (and create) `.review/deferred.md` with a separate consent prompt to also add the file to `.gitignore`. It honors the pre-image safety contract via `git show <marker-sha>:<path>` so an apply against a stale review never silently clobbers already-edited lines. Natural-language triggers include *"read the review on this PR"*, *"apply the AI review comments"*, *"walk me through the CI feedback"*. Full flow, GraphQL query, and Sample Dialogues: [`skills/ai-diff-reviewer/apply-review/SKILL.md`](../skills/ai-diff-reviewer/apply-review/SKILL.md).

---

## The Iteration-Aware Review exhaustive addendum

When [Iteration-Aware Review](ITERATION_AWARENESS.md) fires the `first-pass-exhaustive` policy (either configured explicitly or forced by the 30% new-lines safety net), the reviewer appends a small, deterministic addendum to your custom prompt for **round 1 of that generation only**. The addendum tells the model that this pass gets a wider net and should prefer completeness over conciseness.

The exact text (from `IAR_EXHAUSTIVE_PROMPT_ADDENDUM` in `scripts/reviewer.py` — this is the source of truth):

```
[Iteration-Aware Review — exhaustive first-pass mode active]
This is round 1 of a fresh review generation. Prioritize exhaustive
coverage over conciseness: surface every relevant finding you can
identify in this diff, up to the increased inline-comments ceiling.
Subsequent rounds will dedupe against these findings, so it is
preferable to report a superset now than to trickle findings across
future rounds. Focus areas, severity model, and output shape are
unchanged.
```

**When it fires:**

| Trigger | Fires? |
|---|---|
| `convergence-policy: iterative` | Never |
| `convergence-policy: first-pass-exhaustive` (default), round 1 of new generation | Yes |
| `convergence-policy: first-pass-exhaustive`, round 2+ of same generation | No (dedup-only) |
| `convergence-policy: round-capped`, any round | No |
| `convergence-policy: critical-gate`, any round | No |
| Safety net triggered (≥ 30% new-lines-pct) | Yes (forces `first-pass-exhaustive` for that round) |
| Escape label applied | No (escape bypasses dedup, no addendum) |

The addendum is spliced with the same `---` separator used for user overrides ([compose_system_prompt](../scripts/reviewer.py) at the top of the file) so the model treats it as an unambiguous late-binding note — your project-specific severity overrides and house rules still take precedence.

**Cost impact:** ~150 tokens per round-1-of-generation run. Included in the total accounted for by the [`docs/PERFORMANCE.md § Iteration-Aware Review`](PERFORMANCE.md#iteration-aware-review-iar--cost-and-latency-model) cost matrix.

---

## Sharing prompts

If your team writes a prompt that works really well, consider opening a PR to add it to `prompts/community/` in this repo. Curated, tested prompts for common stacks (Rails, Django, Next.js, Go services) are the kind of contribution that compounds across users.
