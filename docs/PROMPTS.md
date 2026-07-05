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

The `prompt-file` input is a path **inside the consumer's checkout** (not the action's checkout). Make sure the file is committed to the same branch the workflow runs on:

```yaml
- uses: DailybotHQ/ai-pr-reviewer@v1
  with:
    prompt-file: .github/prompts/our_review_rules.md
```

The default prompt (`prompts/default.md` *inside this action's repo*) is used when `prompt-file` is empty. You can also start by copying the default into your own repo, then editing it — that's how most teams converge on a high-quality prompt fastest.

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

## Sharing prompts

If your team writes a prompt that works really well, consider opening a PR to add it to `prompts/community/` in this repo. Curated, tested prompts for common stacks (Rails, Django, Next.js, Go services) are the kind of contribution that compounds across users.
