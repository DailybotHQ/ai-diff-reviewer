<!--
Meta-prompt for AI Diff Reviewer — generate a repo-tailored `prompt-file`.

────────────────────────────────────────────────────────────────────────
👉 PREFER THE SKILL. This meta-prompt is now packaged as a sub-skill
────────────────────────────────────────────────────────────────────────

Install the ai-diff-reviewer skill in your repo — it wraps this whole
discovery-and-generation flow AND is smart enough to:

  * ask whether you want an EXTENSION file (default, layered) or a
    FULL-REPLACEMENT prompt-file (advanced),
  * auto-detect your stack and inspect the repo,
  * write the output directly to `.review/extension.md` or
    `.github/prompts/pr-review.md` (no copy-paste),
  * explain the workflow-file wiring for both modes.

    npx skills add DailybotHQ/ai-pr-reviewer --skill ai-diff-reviewer

Then just tell your coding agent:

    > "Generate a .review/extension.md for this repo"
    > "Customize the code review for our project"

Full docs: skills/ai-diff-reviewer/generate-extension/SKILL.md
────────────────────────────────────────────────────────────────────────

STILL USEFUL FOR:  zero-install (you don't want to vendor a skill),
web chatbots without file-system access (ChatGPT/Gemini web UI), or
one-off customization where installing a skill is more friction than
the copy-paste.

Usage: copy this entire file into your favorite coding AI (Claude Code,
Cursor Agent, Codex, ChatGPT, Gemini) with your repository checked out
locally or opened as workspace context. The AI will inspect your codebase
and produce a `prompt-file` tailored to your stack, architecture, and
security surface. Save the AI's output to `.github/prompts/pr-review.md`
in your own repo and reference it via `prompt-file:` in the AI PR
Reviewer workflow. (If you only want to LAYER overrides on top of the
bundled default, use `prompt-extension-file:` instead and skip this
meta-prompt.)
-->

# You are generating a custom system prompt for AI Diff Reviewer

AI Diff Reviewer is a GitHub Action that runs an LLM against pull requests
and posts inline comments with severity tags. Your job in this
conversation is to produce a **`prompt-file`** — the system prompt that
tells that LLM how to review THIS repository.

The consumer will save your output to their repo (typically
`.github/prompts/pr-review.md`) and reference it via `prompt-file:` in
their workflow. `prompt-file` FULLY REPLACES the bundled default, so
your output must be a complete, standalone system prompt — not a delta.

## Non-negotiable structure

Your generated prompt MUST have these sections, in this order:

1. **Role & mission** — one paragraph defining the reviewer as a senior
   engineer familiar with THIS stack (name the stack explicitly). Set
   the tone: rigorous, specific, kind, no false positives.
2. **Tool schema summary** — a short reminder that the reviewer uses
   these tools: `read_file`, `grep`, `glob`, `post_inline_comment`,
   `submit_review`, plus optional `set_pr_description` and
   `set_pr_complexity` when the consumer opts in. Instruct the model
   to USE the tools to gather evidence before commenting, never guess.
3. **How to think about each finding** — the loop the model follows:
   read the diff → hypothesize a concern → verify by reading
   surrounding context → assign severity → write a comment that names
   the specific line and explains the WHY, not just the WHAT.
4. **Severity definitions** — three levels: `critical`, `warning`,
   `info`. Anchor each to CONCRETE, REPO-SPECIFIC examples derived from
   the analysis you did in the "Discovery" step below. Do NOT paste
   generic OWASP category names without a code-level example. If you
   cannot name a concrete pattern that would trigger a level, you don't
   understand the codebase well enough — go back and read more.
5. **Project-specific overrides** — the section that makes this prompt
   valuable. See "Discovery" below.
6. **Style & tone** — polite, specific, factual. No sycophancy. Ask
   clarifying questions in a comment when uncertain rather than raising
   a low-confidence finding.
7. **Comment format** — each `post_inline_comment` body should be short
   (2–5 sentences), start with the WHY, cite the specific line, and
   suggest a concrete fix when possible.

## Discovery — read the repo before writing

Before generating the prompt, you MUST spend at least 8–12 tool calls
learning the repo. Cover ALL of these:

### 1. Technology stack

Read `package.json`, `pyproject.toml`, `requirements*.txt`, `Gemfile`,
`go.mod`, `Cargo.toml`, `pom.xml`, `build.gradle`, `composer.json`,
`mix.exs`, `deno.json`, `.python-version`, `.nvmrc` — whichever apply.
Identify:

- Primary language(s) and version.
- Web framework (Django/Flask/FastAPI/Rails/Next.js/Nest/Spring/etc.).
- ORM / DB library (SQLAlchemy, Prisma, ActiveRecord, TypeORM, etc.).
- Testing framework and coverage tool.
- Linter, formatter, type checker (ruff, black, mypy, ESLint,
  Prettier, tsc-strict, rubocop, golangci-lint, etc.).
- Auth library (if any).
- Runtime target (Node version, Python version, browser targets, JVM).

### 2. Implementation & architecture patterns

Skim `README.md`, `docs/`, `ARCHITECTURE.md`, `CONTRIBUTING.md`, and
walk the top 2 levels of the source tree:

- Is this a monolith, monorepo, microservices, library, or CLI?
- Layered architecture? Domain-driven design? Hexagonal?
- Where does the HTTP layer live? Where do handlers/controllers sit?
- Where is domain logic? Where are shared utilities?
- Any DI / IoC pattern in use?
- Migration story: how are schema changes managed?
- Async patterns: promises, async/await, coroutines, actors, queues?

### 3. Security surface

Grep for:

- Auth / session / JWT / OAuth / cookies handling.
- Input parsing endpoints (form/query/body).
- File uploads, file downloads.
- Outbound HTTP calls / webhooks.
- Subprocess / shell / `eval` / `exec`.
- SQL query construction (parameterized? string-concatenated?).
- Secret handling (env vars, secret managers, config files).
- CSRF / CORS / rate-limit / content-security-policy configuration.
- Cryptography (hashing, signing, encryption).
- Serialization (`pickle`, YAML load, `unserialize`).

For each pattern found, note WHERE it lives — you'll reference these
locations in the overrides section of the generated prompt.

### 4. Existing quality standards

Look for `.pre-commit-config.yaml`, GitHub Actions in
`.github/workflows/`, `CODEOWNERS`, `RULES.md`, `.editorconfig`, and
any project-authored convention docs (`docs/STANDARDS.md`,
`STYLE_GUIDE.md`, etc.). Identify:

- What lint/format rules are enforced?
- Is there a test-coverage floor?
- Any commit message convention (Conventional Commits, gitmoji)?
- Any prohibited patterns already codified (e.g. "no `console.log` in
  main")?

### 5. Historical pain points (if visible)

Skim `CHANGELOG.md`, closed issue titles from the recent months if you
have GitHub access, and post-mortem docs if any exist. Anything that
repeatedly bit the team goes into "Always `critical`" — that's the
signal that turns a generic reviewer into a senior teammate.

## Generation rules

Follow these rules when writing the output prompt:

1. **English only.** Even if the codebase is in another language, the
   prompt is English — the LLM that consumes it operates in English.
2. **Concrete over abstract.** Every severity example must reference a
   real file, module, or pattern you saw. "Anywhere handling money
   uses Decimal, never float" is good; "financial precision matters"
   is bad.
3. **No secrets in the output.** Do not echo any environment variable,
   token, or credential you may have encountered during Discovery.
4. **Length target.** 200–500 lines of markdown. Longer is fine if the
   codebase justifies it; shorter usually means you skipped Discovery.
5. **No YAML frontmatter, no HTML comments in the header** — the LLM
   reads the raw text. Start with `# ` (h1) and go from there.
6. **Do not embed the tool schema JSON.** Just describe the tools and
   how the reviewer should use them; the runtime injects the actual
   schema.
7. **Do not exceed the LLM's context window.** If the analysis produced
   more content than a single prompt can carry, choose the top 20 most
   impactful overrides and note the rest as "future additions."

## Quality gate — self-check before delivering

Before handing the prompt back to the consumer, verify:

- [ ] Every severity level has AT LEAST TWO concrete, code-level
      examples that reference identifiable patterns in this repo.
- [ ] The "Project-specific overrides" section has at least FIVE
      overrides, each tied to a specific file, module, or class of
      operation.
- [ ] The prompt names the repo's language, framework, and one or two
      distinctive libraries by name.
- [ ] The prompt describes the tools the reviewer uses (does not
      redefine or contradict them).
- [ ] There is nothing project-agnostic that could apply equally to
      any other repo — if a section could survive a copy-paste to a
      different stack unchanged, rewrite it.
- [ ] No secrets, no tokens, no credentials.
- [ ] English only, well-structured markdown, one h1 at the top.

## After you deliver

Tell the consumer:

1. Save the prompt to `.github/prompts/pr-review.md` in their repo.
2. Reference it in their workflow with:

   ```yaml
   with:
     prompt-file: .github/prompts/pr-review.md
   ```

3. Run one review with it, read the inline comments, and if any feel
   over-strict or off-tone, come back to you and refine the overrides.
   The prompt is a living document — expect two or three iterations
   before it feels dialed in.

Now — read the repository, then generate the custom `prompt-file`.
