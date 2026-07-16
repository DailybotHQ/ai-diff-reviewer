# Product Spec — AI Diff Reviewer

## What it is

An LLM-driven code-review system that ships on **two surfaces from a single codebase**:

- **A GitHub Action** — runs on every pull request, posts inline comments anchored to specific lines, gates the GitHub check based on configurable severity thresholds, and applies a "reviewed" label after a successful run. Distributed as a single composite action — no Docker image, no Node modules, no infrastructure beyond a provider API key.
- **A coding-agent skill** (the local companion) — the same review methodology, running inside the developer's coding agent (Cursor, Claude Code, Codex, Gemini, Copilot, Cline, Windsurf) against the local `git diff origin/<base>...HEAD`. Distributed via [skills.sh](https://skills.sh) as `npx skills add DailybotHQ/ai-diff-reviewer --skill ai-diff-reviewer`. No CI, no API-key round-trip — the review runs inside whatever provider the developer's agent is already using.

Both surfaces share the same [`prompts/default.md`](../prompts/default.md) and honour the same [`.review/extension.md`](../.review/extension.md) repo-specific override file, so pinning the same version on the Action and the skill guarantees **local ↔ CI parity** — what the skill flags before the developer pushes is what CI will flag on the PR.

## What problem it solves

PR review is the highest-leverage quality gate in most engineering organisations and the most under-staffed. Senior engineers don't scale; review becomes a bottleneck or a rubber stamp. Existing solutions either (a) require complex self-hosted infrastructure, (b) lock the team into a specific vendor's full ecosystem, or (c) produce shallow, line-by-line nits without project context. On top of that, the review moment traditionally happens **after** the PR opens — by then the author has already switched contexts and the reviewer's feedback triggers an expensive round-trip.

This product targets the whole loop:

- **A stable, configurable, severity-aware reviewer in CI** that any GitHub user can drop into their workflow with a single `uses:` line, customise via prompt and strictness, and trust enough to gate merges on.
- **A parallel local skill** that surfaces the exact same feedback **before** the PR opens — inside the coding agent the developer is already using — so the CI review is a confirmation rather than a discovery.

## Who it's for

- **Open-source maintainers** who want a second opinion on community PRs before they hit human review, plus a self-check contributors can run locally.
- **Small engineering teams** without a dedicated reviewer rotation, who want consistent feedback on every PR and want their developers pre-flighting locally.
- **Internal platform teams** who want to enforce house rules (security, performance, conventions) automatically across both surfaces from a single `.review/extension.md`.
- **Contributors** themselves — running the local skill before pushing, or running the action on a fork, catches most issues before they become PR comments.
- **AI-first solo developers** whose primary workflow is inside a coding agent — the skill hooks straight into that loop without requiring a separate PR to see feedback.

It is **not** a replacement for human code review. It's an additional reviewer that scales — catching the obvious things, applying the documented rules, freeing humans to focus on architecture, design, and judgement calls.

## Core capabilities

### Shared across both surfaces

| Capability | What it means |
|---|---|
| Same base prompt | The bundled [`prompts/default.md`](../prompts/default.md) drives both surfaces. A CI invariant (`Skills — prompt-sync invariant` in [`code_check.yml`](../.github/workflows/code_check.yml)) fails PRs where the skill's copy has drifted, and [`auto-release.yml`](../.github/workflows/auto-release.yml) re-syncs it on every release. |
| Severity tagging | Every finding carries a `critical` / `warning` / `info` severity mapped to the same rubric on both surfaces. |
| Custom prompts | Bring your own system prompt via `prompt-file`, or layer stack-specific overrides via `prompt-extension-file` (CI) / `.review/extension.md` (local). |
| Repo-specific override in one file | `.review/extension.md` is auto-detected by the skill AND referenced from CI's `prompt-extension-file:` input — one file, one source of truth. |

### GitHub Action surface only

| Capability | What it means |
|---|---|
| Inline comments | Comments anchored to specific lines in the diff, with optional GitHub suggestion blocks (one-click apply). |
| Configurable gating | Four strictness modes (`lenient`, `block-on-critical`, `block-on-warning`, `block-on-any`) translate severity into the GitHub check status. |
| Trigger control | Four `trigger-mode` values (`always`, `label-required`, `label-once`, `label-added-only`) for cost control and review-on-demand patterns. |
| Label gate | Optionally only run when a PR has a specific label (e.g. `ready`). |
| Applied label | Optionally label a PR after a successful review (e.g. `pr-reviewed`) so downstream automation can require it. |
| Auto-collapse | Previous bot reviews are marked `OUTDATED` on every new push so only the latest is visually active. Per-provider marker so multiple providers can co-exist on one PR. |
| Tracking comment | A spinner comment with a stable `<!-- ai-pr-reviewer-marker -->` marker transitions in-place from `Working…` to `View review →`. |
| Self-healing on 422 | If GitHub rejects the review because one comment anchored outside the diff, the action retries summary-only instead of losing every comment. |
| PR metadata checks | Optional PR description review (`pr-description-mode: warn` or `autocomplete`) and AI-driven complexity labeling (`complexity-labels-enabled`). |
| External-contributor gate | Optional `author-association`-based skip so drive-by fork PRs don't burn LLM budget until a maintainer signs off. |
| Iteration-Aware Review (v1.8.0+, on by default) | Content-anchored fingerprinting + four convergence policies (default `first-pass-exhaustive`) + hardcoded critical-always-surfaces rail + safety net + human escape label. Solves the "same warnings re-posted on every re-run" symptom without silencing anything the reviewer should still surface. Set `iteration-awareness-enabled: false` to get pre-v1.8 behavior. See [`ITERATION_AWARENESS.md`](ITERATION_AWARENESS.md). |

### Local skill surface only

| Capability | What it means |
|---|---|
| Pre-push review | Runs `git diff origin/<base>...HEAD` locally — no fetch, no push, no PR required. Same output format the CI bot would post. |
| Zero extra LLM cost | The skill hands the diff + prompt to whatever coding-agent provider the developer already uses (Claude, Cursor, Codex, Copilot, Gemini…). No separate API key, no round-trip. |
| Five sub-skills | `run a review` (default), [`generate-extension`](../skills/ai-diff-reviewer/generate-extension/SKILL.md) (author `.review/extension.md` tailored to this repo), [`setup`](../skills/ai-diff-reviewer/setup/SKILL.md) (install the Action from scratch — also doubles as the reference manual for every `action.yml` input), [`open-pr`](../skills/ai-diff-reviewer/open-pr/SKILL.md) (author a well-structured PR title + body from the current branch after a clean review), and [`apply-review`](../skills/ai-diff-reviewer/apply-review/SKILL.md) (read the CI review posted on the PR and walk the developer through applying, deferring, or skipping each finding — closes the loop back from CI to local). |
| First-run bootstrap | Detects a repo with no `.review/extension.md` and offers a single-question prompt to generate one via `generate-extension`. Answer `never` to opt out via a tracked marker. |
| Trust boundary | The parent review flow is read-only. Every sub-skill that writes files (setup writes `.github/workflows/pr-review.yml`; generate-extension writes `.review/extension.md`; open-pr calls `gh pr create/edit`; apply-review edits source files on per-finding *"apply"* consent and optionally writes `.review/deferred.md` / appends it to `.gitignore` on separate consent) asks for explicit consent first. |

## Non-goals

- **Real-time IDE integration inside the running action.** The action is CI-time. The skill covers pre-push local review; deeper IDE integration is out of scope.
- **Multi-PR or repo-wide reasoning.** Both surfaces review one diff at a time. Cross-PR refactoring suggestions are out of scope.
- **Replacing branch protection.** Strictness gating *complements* branch protection (require the action's check to pass); it doesn't replace required-reviewer rules.
- **Auto-merging.** The action posts review feedback; merge decisions are the maintainer's. Pair with a separate auto-merge action if that's your workflow.
- **Convergence across multi-round reviews** — until `v1.7.x`. Prior to Iteration-Aware Review the reviewer produced the same warnings on every re-run against the same commit-set (the "infinite loop" symptom). From `v1.8.0` on, convergence is a **default-on capability** via the IAR subsystem shipped with `convergence-policy: first-pass-exhaustive`. Consumers on `@v1` who don't touch their workflow YAML get convergence automatically at the next release; consumers who want the pre-v1.8 shape can set `iteration-awareness-enabled: false` to opt out.
- **Generating code or auto-pushing fixes from CI.** The GitHub Action comments and suggests; it never pushes fixes on the developer's behalf, and never commits from CI. (Suggestion blocks let the maintainer one-click apply in the GitHub UI.) The local companion skill's `apply-review` sub-skill can walk the developer through applying suggestions **locally with per-finding consent** — it uses the `Edit` tool to rewrite lines only after the developer says *"apply"* for that specific finding, and never runs `git commit` / `git push`. Consent-gated local edits are a different contract from unattended CI writes; the "no auto-push" boundary holds on both surfaces.
- **Hosting any infrastructure.** Inputs go to the configured provider; outputs go to GitHub. No third party between.
- **The skill wrapping its own LLM call.** The skill runs *inside* the developer's coding agent; it doesn't ship its own model or its own API key path. That's a deliberate architectural choice — it's what makes the skill zero-cost to install.

## Distribution

### GitHub Action

- **License:** MIT.
- **Channel:** GitHub Marketplace (publicly searchable at [`marketplace/actions/ai-diff-reviewer`](https://github.com/marketplace/actions/ai-diff-reviewer)) + direct repo URL for `uses: DailybotHQ/ai-diff-reviewer@v1`.
- **Repo path:** `DailybotHQ/ai-diff-reviewer` (renamed 2026-07-14 from `DailybotHQ/ai-pr-reviewer`; the old path still resolves via GitHub's permanent 301 redirect, so existing `@v1` pins keep working).
- **Versioning:** SemVer. The moving major tag (`v1`) auto-points to the latest `v1.x.y` so consumers pinning `@v1` get patches and minor features automatically.
- **Provider parity:** as of `v1.1.0` the action ships with **four** providers across two families:
  - Chat-completions family (this action drives the tool-use loop): `anthropic`.
  - Agent-runner family (vendor CLI drives the loop; findings return via `.aiprr/findings.json`): `claude-code`, `cursor`, `codex`.
  Each CLI provider only installs when selected — `provider: anthropic` (the default) pays zero install cost. Adding a new chat-completions provider (OpenAI, Gemini, Azure OpenAI, self-hosted vLLM/Ollama) or a new agent-runner CLI is a one-class addition. See [PROVIDERS.md](PROVIDERS.md).

### Local companion skill

- **License:** MIT (same repository).
- **Channel:** [skills.sh](https://skills.sh) via `npx skills add DailybotHQ/ai-diff-reviewer --skill ai-diff-reviewer` — a one-liner that vendors the skill into `.agents/skills/ai-diff-reviewer/` and records the pinned version in `skills-lock.json`.
- **Versioning:** the skill package's `version:` frontmatter is bumped in lockstep with the Action's tag by [`auto-release.yml`](../.github/workflows/auto-release.yml). Pinning `DailybotHQ/ai-diff-reviewer@v1.5.0` on both surfaces guarantees the exact same review methodology on both.
- **Agent support:** any coding agent that reads the Open Agent Skills format — Cursor, Claude Code, Codex CLI, Gemini CLI, GitHub Copilot's agent mode, Cline, Windsurf, OpenClaw.
- **Dogfooded install:** this repo also vendors its own skill copy at [`.agents/skills/ai-diff-reviewer/`](../.agents/skills/ai-diff-reviewer/) using the exact same `npx skills` install path, refreshed automatically after every release by `auto-release.yml` Step 3.5.

## Quality bar

- **Stdlib-only runtime** — no install phase, no supply-chain surface beyond Python itself.
- **Single-file implementation** — `scripts/reviewer.py` is ~4000 LOC, fully type-hinted, runnable directly without the action wrapper for local debugging.
- **Compile-checked in CI** on every PR, plus a **242-test stdlib `unittest` suite** covering the pure logic (core reviewer helpers in `test_reviewer.py`, `.aiprr/findings.json` schema in `test_findings_parser.py`, CLI providers + subprocess-security invariants in `test_agent_runner_providers.py`, cross-family serialization in `test_end_to_end_roundtrip.py`).
- **CLI installers smoke-tested** — a matrix job exercises each agent-runner CLI installer on a fresh runner before it reaches consumers.
- **Prompt sync enforced** — `Skills — prompt-sync invariant` in `code_check.yml` fails any PR where the skill's `prompt.md` byte-copy has drifted from the Action's `prompts/default.md`. Local↔CI parity is a hard CI gate, not a convention.
- **Dogfooded on both surfaces:**
  - **CI action:** reviews its own PRs via [`.github/workflows/self-review.yml`](../.github/workflows/self-review.yml). The direct Anthropic baseline runs on every PR/push; the CLI-provider legs run when their secret is configured. Active legs use distinct `self-reviewed:*` labels so each provider's review is separately identifiable in the PR conversation.
  - **Skill:** the vendored copy at `.agents/skills/ai-diff-reviewer/` is re-installed via `npx skills update` after every release, so a broken install flow fails the release itself.

## Roadmap (not a commitment)

The shipped versions so far:

| Version | Headline |
|---|---|
| **v1.0.0** (2026-05-29) | Initial release — Anthropic provider, composite action, severity gating, tracking comment, 422 fallback. |
| **v1.1.0** (2026-07-05) | Three new agent-runner providers (`claude-code`, `cursor`, `codex`) alongside the incumbent `anthropic` — zero migration for `@v1` consumers. |
| **v1.2.x** (2026-07-11 → 2026-07-14) | Cost-scoped dogfooding, per-provider `collapse-previous`, provider-side bug fixes making `claude-code` + `codex` actually usable end-to-end. |
| **v1.3.x** (2026-07-14) | Marketplace listing published; `author-association` gate for public-repo abuse defense; Claude Code accepts subscription OAuth tokens as `api-key`. |
| **v1.4.x** (2026-07-14) | Full four-leg self-review matrix on every ready-labeled PR (removed the critical-surface filter); vendored `dailybot` skill dogfood; strictness dogfood at `block-on-critical`. |
| **v1.5.0** (2026-07-14) | Coordinated rename to **AI Diff Reviewer** (unblocked the Marketplace publish) + **the local companion `ai-diff-reviewer` skill** with the `generate-extension` and `setup` sub-skills + the `.review/extension.md` convention as a single source of truth. |
| **v1.6.0** (in flight — PR #29) | New `open-pr` sub-skill authoring well-structured PR titles + bodies from the current branch's diff (Conventional-Commits inference, structured body, PR-template merge, `gh pr create`/`edit`). |

The upcoming work — no commitment on ordering, all `v1.x` unless flagged:

- **Raw chat-completions providers** (`openai`, `gemini`, `bedrock`) — for teams who want to use those models without installing the corresponding vendor CLI. Bedrock is pending a stdlib-only SigV4 design discussion.
- **Community-curated prompt library** at `prompts/community/<stack>.md` — Rails, Django, Next.js, Go services, etc. Curated extension files consumers can reference from `prompt-extension-file:`.
- **`.aiprr/findings.json` v2 schema** — optional `suggestions` field for line-range code snippets, backwards-compatible via forward-compat parser.
- **More local sub-skills** as the pattern proves out — likely candidates: a `triage` sub-skill for filing follow-up issues from remaining findings, a `changelog` sub-skill for authoring `CHANGELOG.md` entries in the same shape as the commits.
- **v2.0** — only if a breaking change to the public input/output contract of `action.yml` is unavoidable. There's no such change on the horizon; the abstraction has held across six minor versions.
