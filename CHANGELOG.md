# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Fixed
- **CI: `auto-release.yml` Step 3.5 now passes `-y` to `skills add`.**
  The vendored dogfood-refresh step ran the `skills` CLI without a
  non-interactive flag on the subcommand itself (`npx --yes` only
  covers npm's own "Ok to proceed?" prompt), so it hit the
  interactive *"Which agents do you want to install to?"* picker in
  a non-TTY environment, hung, and never actually refreshed
  `.agents/skills/ai-diff-reviewer/`. The subsequent sanity check
  correctly refused to commit a stale snapshot, but the whole
  release job failed after the tag was already pushed — leaving
  v1.6.1 in a partially-published state (see v1.6.1 recovery entry
  below). Fix adds the missing `-y` and a comment explaining why
  both flags are required.

### Changed
- **Parent `ai-diff-reviewer` skill (`skills/ai-diff-reviewer/SKILL.md`)
  clarifies the two supported flows.** Added a new "Two supported
  flows: local-only or dual-surface" section that names each flow
  explicitly (Flow A = local-only, no CI Action; Flow B = dual-surface,
  local + CI), spells out which sub-skills to run and which to skip for
  each, documents the parity guarantee that makes dual-surface
  worthwhile, and gives copy-paste signalling phrases so an agent
  doesn't have to guess. Motivation: the four sub-skills were already
  independent by design, but the parent SKILL.md read as a single
  lifecycle where every consumer runs all four — masking the fact that
  `setup` is entirely optional for repos that want local-only review.
- **Ambiguity tie-break no longer defaults to `setup`.** Previously,
  when a repo had no workflows and the developer had just installed the
  skill, the parent SKILL.md's disambiguation heuristic guessed
  "probably the setup flow." That biased agents toward writing
  `.github/workflows/pr-review.yml` on repos where the developer only
  wanted a local reviewer. The heuristic now instructs the agent to
  ASK ("Flow A local-only vs Flow B dual-surface") before routing, so
  the CI Action is never installed unrequested.

### Fixed (self-review of the above)
- **Ask-heuristic now scopes on the ai-diff-reviewer workflow
  specifically, not "no workflows at all".** Original wording gated
  on "Repo has NO workflows" — but almost every real repo already
  has some `.github/workflows/` file (tests, deploys, Dependabot), so
  the heuristic silently didn't fire and the routing fix was
  bypassed. Now scoped to "no `.github/workflows/pr-review.yml` (or
  similarly-named AI Diff Reviewer workflow) AND no
  `.review/extension.md`", with an explicit note that unrelated
  workflows are NOT evidence of Flow B. (self-review finding, warning)
- **Parity paragraph now correctly says the CI workflow only wires
  `prompt-extension-file` when the Step 5 handoff to
  `generate-extension` is accepted.** Original wording implied
  `setup` always wired it, which overstated Flow B's out-of-the-box
  behavior. (self-review finding, info)
- **Flow A row softened from "must run generate-extension" to
  "optional but recommended".** Reading the original cell literally,
  an agent might refuse to run a local review until an extension
  file exists — but Step 2.5 allows declining, and the base prompt
  alone is a supported local-only setup. (self-review finding, info)
- **Lifecycle framing above the "Two supported flows" section
  reworked to be flow-neutral.** Previously the paragraph opened
  "The four sub-skills form a lifecycle: setup installs the CI
  action…" which set a dual-surface-only mental model before an
  agent even reached the Two-supported-flows section. Now splits
  into "Dual-surface lifecycle" and "Local-only lifecycle" bullets
  so both flows are named as first-class from the top. (self-review
  note)
- **"This skill does not replace the CI action" note reframed for
  Flow A compatibility.** Original wording read as Flow-B-only and
  conflicted with the local-only flow. Now scoped explicitly: when
  paired with the CI Action (Flow B), the skill doesn't replace it;
  in Flow A the local skill IS the entire reviewer. (self-review
  note)

Content-only change to `skills/ai-diff-reviewer/SKILL.md` — no runtime
behavior touched, `prompt.md` unchanged, no `action.yml` surface
impact, all four sub-skills' frontmatter unchanged.

## [1.6.0] — 2026-07-14

**Headline:** ships a new **`open-pr` sub-skill** in the local `ai-diff-reviewer` skill pack — turns the current branch's diff into a well-documented pull request (Conventional-Commits title inference, structured body with mandatory + conditional sections, PR-template merge, `gh pr create`/`edit`) — plus a **comprehensive `docs/` audit** that corrects stale metrics across 10 files and adds dual-surface framing (GitHub Action + local companion skill) throughout the product spec, architecture, and prompts guide.

### Added
- **New `open-pr` sub-skill for the local `ai-diff-reviewer` skill
  pack** (`skills/ai-diff-reviewer/open-pr/SKILL.md`). Authors a
  well-documented pull request from the current branch's diff — infers
  a Conventional-Commits (or repo-native, empirically detected from
  recent merged PRs) title, drafts a structured body with mandatory
  Summary / Test plan / Risks and conditional Related issues /
  Screenshots / Breaking changes / Migrations / Dependencies sections
  based on diff signals, merges (never overwrites) any
  `.github/pull_request_template.md`, previews everything for a single
  yes/edit/cancel, and executes via `gh pr create` (new PR) or `gh pr
  edit` (refresh existing PR — includes body diff in the preview).
  Supports `--draft`, non-default base branches (stacked PRs), and
  degrades gracefully for fork PRs. Never pushes commits, never
  auto-merges, never fabricates issue refs / CI URLs / migration
  claims — surfaces the exact `gh` remediation and stops on any
  failure (non-blocking rule). Complements the parent skill's local
  review as the natural next step (review → fix → open the PR).
  Companion trigger phrases: "open the PR", "create a pull request",
  "write the PR body", "update the PR description", "make a draft PR".
- **Parent `ai-diff-reviewer` skill routes to `open-pr`.** Updated the
  `description` frontmatter to list four capabilities (was three);
  added activation triggers for the open-pr flow; added the
  `/ai-diff-reviewer-open-pr` slash-command hint; added an optional
  next-step hint at the end of the review flow (Step 4) suggesting
  the developer route to `open-pr` when the review comes back clean.
- **README section for `open-pr`** — "Open the pull request from the
  same diff" under `## Local review parity (companion skill)`,
  documenting the triggers, the three-mandatory / six-conditional
  section model, and the PR-template merge behavior.

### Changed
- **Comprehensive `docs/` audit for the dual-surface reality** (10
  files, +252 −66 lines, all content-only). Corrects stale metrics
  that had drifted across five minor versions and adds the local
  companion skill as a first-class architectural component:
  - **`docs/PRODUCT_SPEC.md`** — full rewrite. "What it is" names
    both surfaces (Action + skill) upfront. Core capabilities split
    into Shared / Action-only / Skill-only groups. New "Local
    companion skill" distribution section covers `npx skills add`.
    Roadmap replaced with an accurate v1.0.0–v1.6.0 history table +
    honest `v1.x` outlook (removed the "v1.2 raw OpenAI, v1.3
    Gemini" predictions that never landed).
  - **`docs/ARCHITECTURE.md`** — new "Two surfaces, one methodology"
    intro. Two topology diagrams (Action + Skill). New "The local
    companion skill pack" component section. Fixed the
    "AnthropicProvider (today; OpenAI/Gemini stubs)" obsolete note
    to reflect the four shipping providers.
  - **`docs/README.md`** — new "Local companion skill" section with
    pointers to all six skill-pack files.
  - **`docs/PROMPTS.md`** — added "Author a well-documented PR with
    the open-pr sub-skill" section, closing the sub-skill coverage.
  - **`docs/AI_AGENT_ONBOARDING.md`** — reframed "What this repo is"
    as dual-surface; local-companion-skill task rows; hardened
    pre-PR checklist for the prompt-sync invariant and reference.md
    sync.
  - **Stale metrics corrected across 6 files:** `~2400 LOC` →
    **~4000 LOC** (`4018` exact in `scripts/reviewer.py`); `109
    tests` → **242 tests** across 4 files; repo slug
    `ai-pr-reviewer` → **`ai-diff-reviewer`** (with 301 redirect
    for back-compat pins); soft ceiling raised to `~4500 LOC` with
    "next feature triggers split" note.

## [1.5.0] — 2026-07-14

**Headline:** rename the Marketplace listing to **"AI Diff Reviewer"** (was "AI PR Reviewer"), which unblocks the first-time publish that had been stuck against a name-squatting org (`github.com/ai-pr-reviewer`, 0 public repos since 2024-01, blocks the slug under GitHub's global-namespace uniqueness rule). Also ships a local companion **`ai-diff-reviewer` skill** so every developer's coding agent (Cursor, Claude Code, Codex, Gemini, Copilot, Cline, Windsurf) can run the SAME review methodology locally — same prompt, same severity model, same output format — before pushing. The skill and the action share `prompts/default.md` as a single source of truth, kept in sync by a new CI invariant + an `auto-release.yml` step. Also establishes the `.review/extension.md` convention so a project's custom rules apply to both surfaces from a single file.

### Changed
- **Coordinated rename to "AI Diff Reviewer".** Two moves in the same
  release cycle to unblock the Marketplace publish:
  - **`action.yml` `name:`** — `'AI PR Reviewer'` → `'AI Diff Reviewer'`
    (Marketplace slug `ai-diff-reviewer`).
  - **GitHub repo** — `DailybotHQ/ai-pr-reviewer` →
    `DailybotHQ/ai-diff-reviewer` (both slugs now match exactly).

  Why: the v1.4.x publish attempt failed with `Cannot match an existing
  action, user or organization name` — GitHub's name-uniqueness rule
  includes user/org names and the org `github.com/ai-pr-reviewer`
  (created 2024-01, 0 public repos) name-squats the slug at the
  org-namespace level. "AI Diff Reviewer" (verified free at both the
  Marketplace and org-namespace levels) is also more precise: this
  action reviews the `git diff origin/<base>...HEAD` specifically,
  not the PR envelope (labels, description, metadata).

  **Consumer impact — none.** GitHub's permanent 301 redirect on
  renamed repos keeps every `uses: DailybotHQ/ai-pr-reviewer@v1` pin
  working transparently. Published tags v1.0.0–v1.4.2 resolve to the
  new URL automatically. The `AIPRR_*` env-var prefix (private
  contract) stays unchanged. New README/example copy uses the
  canonical `DailybotHQ/ai-diff-reviewer` path. See
  [`AGENTS.md § 9`](AGENTS.md) for the full rename decision log.
- **User-facing branding updated across scripts and docs.** Log prefix
  (`[ai-pr-reviewer]` → `[ai-diff-reviewer]`), HTTP `User-Agent`
  (`ai-pr-reviewer` → `ai-diff-reviewer`), the malformed-`findings.json`
  fallback comment ("**AI PR Reviewer note:**" → "**AI Diff Reviewer
  note:**"), and every occurrence of the product name in `README.md`,
  `AGENTS.md`, `docs/`, `examples/`, `prompts/default.md`, and the
  bundled `.agents/` catalog. **HTML marker strings kept intact**
  (`<!-- ai-pr-reviewer-marker -->`, `<!-- ai-pr-reviewer-state: … -->`,
  `<!-- ai-pr-reviewer-provider:… -->`,
  `<!-- ai-pr-reviewer-description-autocompleted -->`) — these are stable
  contracts on already-posted PR comments; renaming them would silently
  break `collapse-previous` and state detection on every existing
  consumer PR.
- **Local companion skill folder renamed** to `skills/ai-diff-reviewer/`
  (was `skills/ai-diff-reviewer/`) to mirror the product name. Install command
  is now `npx skills add DailybotHQ/ai-pr-reviewer --skill ai-diff-reviewer`;
  vendored path is `.agents/skills/ai-diff-reviewer/`. The router skill's
  `name:` frontmatter changes from `ai-diff-reviewer` to `ai-diff-reviewer`,
  and the sub-skill's from `code-review-generate-extension` to
  `ai-diff-reviewer-generate-extension`. Since the skill was introduced
  in this same release, there is no pre-existing consumer install to
  migrate.

### Fixed
- **Auto-release Step 3 now uses `git push --atomic HEAD:main $NEW_VERSION`**
  when there's a `chore(release): sync` commit to accompany the new tag.
  Prevents the partial-release state PR #26's initial `v1.5.0` cut hit —
  a branch-protection rejection on the branch push used to leave the tag
  on the remote while main (and `@v1`) stayed behind. The atomic push
  ensures both refs land together or neither does. When the atomic push
  fails, Step 3 now emits a clear error message pointing at the recovery
  playbook.
- **Auto-release Step 3 now peels the major-alias tag** with
  `git tag -f "$MAJOR_TAG" "${NEW_VERSION}^{}"` instead of the bare
  `git tag -f "$MAJOR_TAG" "$NEW_VERSION"`. Without the `^{}` peel,
  `@v1` became a nested annotated tag pointing at the `v1.X.Y` tag
  object (which then pointed at the commit), which is technically
  valid Git but confused any tooling that reads `refs/tags/v1`'s
  object SHA and expected a commit SHA. Now `@v1` is a lightweight
  tag directly at the commit — matching what a consumer running
  `git checkout v1` expects and letting the playbook's verification
  recipes cleanly compare commit SHAs.

### Added
- **`docs/RELEASE_RECOVERY.md`** — playbook for the partial-release
  failure mode above (recover the sync commit via a PR, move `@v1`,
  create the GitHub Release, refresh the vendored dogfood copy) plus the
  two long-term fixes to prevent recurrence: `AUTOMATION_GITHUB_TOKEN`
  with branch-protection bypass (recommended), or reworking Step 2.5 to
  open a PR instead of pushing directly to main. Indexed from
  `docs/README.md` and `AGENTS.md`.
- **First `.review/extension.md` for this repo** — generated by
  dogfooding the `ai-diff-reviewer-generate-extension` sub-skill on our
  own codebase (12+ Discovery tool calls reading AGENTS.md, ARCHITECTURE,
  SECURITY, STANDARDS, DEVELOPMENT_GUIDELINES, reviewer.py header, and
  code_check.yml before writing). ~193 lines with 17 severity overrides,
  6 "don't comment on" rules, 9 repo-specific conventions, test-strategy
  expectations, and PR-hygiene rules — all code-anchored to specific
  AGENTS.md rule numbers, file paths, or constant names (`safe_repo_path`,
  `redact_for_log`, `_CLI_ENV_ALLOWLIST`, HTML markers, etc.). Covers
  the load-bearing patterns any change to `scripts/reviewer.py` could
  regress: stdlib-only imports, subprocess/shell safety, path traversal
  routing, env-var scrubbing for CLI subprocesses, HTML-marker
  preservation, symlink hygiene, type-hint mandate, three codified
  broad-except patterns, magic numbers → constants, secret scrubbing.
- **`self-review.yml` layers `.review/extension.md` on every provider
  leg** — added `prompt-extension-file: .review/extension.md` to the
  Claude / Cursor / Codex provider matrix's `with:` block. Same file
  the local `ai-diff-reviewer` skill auto-detects, so local + CI
  reviews use one source of truth (`docs/PROMPTS.md § "Sharing
  repo-specific rules between CI and local"`). The self-review that
  ran on the PR introducing the extension was the first live use —
  meta-verification that the rules pass their own scrutiny.
- **Auto-release Step 3.5 hardening: pinned fetch + version assertion.**
  The step now runs `npx skills add DailybotHQ/ai-diff-reviewer@<new-tag>
  --skill ai-diff-reviewer --force` (explicit pin to the exact tag we
  just cut, not "the newest matching tag") and then asserts the
  vendored `SKILL.md`'s `version:` frontmatter equals the new tag
  before committing. Closes the failure mode where GitHub tag
  propagation lag past the 5s sleep would let `npx skills update`
  no-op silently — the pinned fetch fails loudly if the tag isn't
  visible, and the version assertion catches "fetch succeeded but
  installed the wrong content" cases. Empty diff after both checks
  pass is still treated as success (idempotent re-runs).
- **Auto-release dogfooding of the vendored companion skill.** New Step 3.5
  in [`auto-release.yml`](.github/workflows/auto-release.yml) runs
  `npx skills add DailybotHQ/ai-diff-reviewer@<new-tag> --skill
  ai-diff-reviewer --force` immediately after each release tag is
  pushed and commits the refreshed `.agents/skills/ai-diff-reviewer/`
  + `skills-lock.json` as `chore(release): dogfood vendored
  ai-diff-reviewer to vX.Y.Z [skip release]`. Three benefits:
  - **Live smoke-test** — every release proves the just-published tag
    installs cleanly via `npx skills` on a real consumer setup (this
    repo IS a consumer). Broken installers fail here, before external
    consumers ever try to update.
  - **Cloning-parity** — anyone cloning at HEAD gets a vendored copy
    that matches the current release, not the previous one.
  - **Lockfile freshness** — the recorded content hash stays accurate
    after every release, keeping `experimental_install` restore flows
    reliable.
  Documented as Rule #10 pillar (B) in [`AGENTS.md`](AGENTS.md) — the
  runtime dogfooding via `self-review.yml` (pillar A) and the install-flow
  dogfooding via this step (pillar B) are now formal invariants. Also
  bans hand-editing `.agents/skills/ai-diff-reviewer/**` on feature
  branches (new DON'T #14).
- **First-time vendor of the ai-diff-reviewer skill into this repo.**
  Ran `npx skills add DailybotHQ/ai-diff-reviewer@v1.4.2 --skill
  ai-diff-reviewer` to establish the vendored baseline (lands with
  this PR) that Step 3.5 will refresh going forward. Adds
  `.agents/skills/ai-diff-reviewer/` (SKILL.md + prompt.md +
  generate-extension/ + setup/) and an entry in `skills-lock.json`
  alongside `dailybot` and `deepworkplan`.
- **Sub-skill: [`setup`](skills/ai-diff-reviewer/setup/SKILL.md)** —
  interactive installer for the GitHub Action itself. Walks the
  developer through six decisions (provider, strictness, trigger mode,
  external-contributor policy, PR-description mode, complexity
  labels), uses light Discovery (repo visibility, existing workflows,
  detected stack, default branch) to pre-fill sensible defaults, then
  writes a tailored `.github/workflows/pr-review.yml` with only the
  inputs that differ from action defaults (so the composed workflow
  reads like a hand-written minimal file, not a config dump). Prints
  the exact GitHub Secrets URL for the chosen provider and the git
  commit-and-test steps at the end. Optionally hands off to
  `generate-extension` in Step 5 so a single conversation takes the
  developer from **zero setup → installed → tailored**. Also serves
  as the **reference manual** for every `action.yml` input via
  [`setup/reference.md`](skills/ai-diff-reviewer/setup/reference.md) —
  any coding agent with the skill installed can answer *"what does
  `strictness` do?"* or *"how do I pin the Cursor CLI version?"*
  without opening the action source. Wired into the parent skill's
  router table (three capabilities now) and the natural-language
  triggers ("set up ai diff reviewer for this repo", "install the ai
  diff reviewer github action", "how do I configure this?"). Documented
  in the [`README.md` § "Bootstrap the GitHub Action itself"](README.md)
  section and [`docs/PROMPTS.md`](docs/PROMPTS.md).
- **First-run bootstrap prompt in the `ai-diff-reviewer` skill** — when
  the review flow activates on a repo with no `.review/extension.md`
  (and no `.review/.skip-bootstrap` marker), the skill asks ONE
  question — **yes / no / never** — offering to route to
  `generate-extension` so the review is layered on repo-tailored
  overrides from day one. Choosing **yes** invokes the sub-skill and
  re-runs the review with the fresh extension; **no** proceeds with
  the base prompt just this once (offer fires again next time);
  **never** creates `.review/.skip-bootstrap` (a 0-byte tracked
  marker) so the offer never fires again in this repo. Committing the
  marker is the intended workflow — the whole team inherits the same
  UX. Deletion re-enables the offer. Documented in the skill's Step
  2.5, [`docs/PROMPTS.md` § "First-run bootstrap prompt"](docs/PROMPTS.md),
  and the `README.md` § "First-run bootstrap prompt" section. Trust
  boundary updated: the skill is still near read-only, but now writes
  to `.review/` in exactly two consented cases (bootstrap → extension
  file, opt-out → skip marker).
- **Sub-skill: [`generate-extension`](skills/ai-diff-reviewer/generate-extension/SKILL.md)** —
  bootstraps a repo-tailored `.review/extension.md` (or, in advanced
  mode, a full-replacement `.github/prompts/pr-review.md`) by inspecting
  the codebase for stack, architecture, security surface, existing
  conventions, and historical pain points. Mandatory Discovery phase
  (≥ 12 tool calls before writing anything) keeps the output tied to
  concrete file/module references, not generic "avoid magic numbers"
  advice. Wraps the existing meta-prompt at
  [`examples/prompts/generate-custom-prompt-meta.md`](examples/prompts/generate-custom-prompt-meta.md)
  as a proper skill so consumers don't have to copy-paste it into a
  chat window. Two output modes chosen by a single clarifying question
  (extension by default, full-replacement as advanced). Meta-prompt
  file kept for zero-install use cases (web chatbots without file-system
  access) with a header redirecting to the skill. Follows the
  `DailybotHQ/agent-skill` router-plus-sub-skills pattern used by
  `dailybot-report`, `dailybot-kudos`, etc.
- **Local companion skill: [`ai-diff-reviewer`](skills/ai-diff-reviewer/SKILL.md)** —
  the same review methodology that runs on your PR in CI, now available
  locally in every coding-agent harness. Install into any consumer repo
  with `npx skills add DailybotHQ/ai-pr-reviewer --skill ai-diff-reviewer`;
  vendors into `.agents/skills/ai-diff-reviewer/` and pins via
  `skills-lock.json`. Uses the harness's own Read/Grep/Glob tools to
  gather context and produces the review as terminal output in the same
  format the CI bot would post (verdict + findings table + severity +
  notes + recommendation). Because `skills/ai-diff-reviewer/prompt.md` is a
  byte-identical copy of `prompts/default.md` (kept in sync by
  `auto-release.yml`), pinning the same version on both surfaces
  guarantees local ↔ CI parity. Adopts the Open Agent Skills
  conventions used by `DailybotHQ/agent-skill` and
  `DailybotHQ/deepworkplan-skill`: YAML frontmatter with `name` /
  `description` / `version` / `documentation_url` / `user-invocable` /
  `metadata.openclaw` / `allowed-tools`, layout at
  `skills/<name>/SKILL.md` in the repo root so `skills.sh` discovers
  it, and no separate release cadence — the skill's version tracks
  the action tag.
- **Convention: `.review/extension.md`** for repo-specific prompt
  overrides. The new companion skill auto-detects this path (with
  `.github/ai-pr-reviewer/extension.md` as fallback for teams that
  prefer `.github/` sibling to workflow files) and layers the file's
  contents on top of the base prompt. Consumers reference the same
  path from their CI workflow's `prompt-extension-file:` input, so
  local and CI share ONE extension file with zero drift. Documented in
  [`docs/PROMPTS.md` § "Local coding-agent parity"](docs/PROMPTS.md)
  and the [`README.md` § "Local review parity"](README.md) section.
- **CI: [`scripts/validate-frontmatter.py`](scripts/validate-frontmatter.py)** —
  Python + PyYAML validator that enforces the Open Agent Skills
  contract on every `skills/**/SKILL.md`. Runs as the new
  `Skills — SKILL.md frontmatter validation` job in
  [`code_check.yml`](.github/workflows/code_check.yml). Adapted from
  `DailybotHQ/agent-skill/scripts/validate-frontmatter.py` with the
  `dailybot-` name-prefix rule dropped (this repo's skills use their
  own slug space). Runtime remains stdlib-only per AGENTS.md Rule #2
  — PyYAML is CI-only tooling.
- **CI: `Skills — prompt-sync invariant`** job in
  [`code_check.yml`](.github/workflows/code_check.yml) — fails any PR
  where `skills/ai-diff-reviewer/prompt.md` has drifted from
  `prompts/default.md`, with a clear fix message pointing at
  `cp prompts/default.md skills/ai-diff-reviewer/prompt.md`. Ensures `main`
  is never in an inconsistent state between prompt edits and the next
  release cut.

### Changed
- **Auto-release now syncs skill artifacts on every version bump.** A
  new step in [`auto-release.yml`](.github/workflows/auto-release.yml)
  runs before the tag is created and (a) `sed`s the new SemVer into
  the `version:` field of every `skills/**/SKILL.md` file, and (b)
  copies `prompts/default.md` → `skills/ai-diff-reviewer/prompt.md`. If the
  changes are non-empty, they're committed as
  `chore(release): sync skill artifacts for vX.Y.Z [skip release]` and
  pushed to `main` alongside the tag via `git push --follow-tags`. The
  `[skip release]` marker + the existing `chore(release):` prefix
  guard on the workflow's `if:` together prevent an infinite trigger
  loop. Consumer impact: none — the action's public contract in
  `action.yml` is untouched; only the skill artifacts inside `skills/`
  move.

## [1.4.2] — 2026-07-14

**Headline:** Marketplace-readiness housekeeping — root-level `SECURITY.md` so GitHub's *Report a vulnerability* discovery works out of the box, simplified security reporting channel (dropped the dead-end `CODEOWNERS` email path), and the CHANGELOG backfilled for the five same-day releases (`v1.3.1` through `v1.4.1`) whose bullets had accumulated in `[Unreleased]`. Purely docs and metadata — no runtime, `action.yml`, or workflow behavior changed.

### Added
- **Root-level [`SECURITY.md`](SECURITY.md)** — Marketplace-readiness fix.
  GitHub's *Report a vulnerability* discovery flow prefers the file at
  repo root (or `.github/`); until now we only had the long-form model
  at [`docs/SECURITY.md`](docs/SECURITY.md), which is the least-
  discoverable of the three canonical locations. The new root file is
  a thin pointer that carries the private-advisory reporting
  instructions, a supported-versions table (`v1.x` current major),
  and highlights that link to the long-form doc for the full trust
  model, per-provider egress surfaces, and accepted risks. The two
  files stay in sync trivially (root = pointer; `docs/` = source of
  truth).

### Changed
- **Security reporting channel simplified** in both
  [`SECURITY.md`](SECURITY.md) and [`docs/SECURITY.md`](docs/SECURITY.md).
  The prior "email the address in `CODEOWNERS`" fallback pointed at a
  file that does not exist in the repo — a dead end for reporters who
  could not use the private-advisory UI. Both files now point
  exclusively at the GitHub Security Advisory
  (`security/advisories/new`), which any GitHub account can submit
  against a public repo. No new dependency, no new attack surface
  (open-advisory URLs are already public), and no spam magnet from
  publishing a personal email in a Marketplace-facing file.

### Docs
- **CHANGELOG backfilled for [`1.3.1`](#131--2026-07-14) through
  [`1.4.1`](#141--2026-07-14)** — the auto-release workflow cuts SemVer
  tags on every merge to `main` but deliberately does **not** edit the
  changelog, so five same-day releases had accumulated their bullets
  in `[Unreleased]`. Each bullet has been redistributed to its owning
  tag section with headline notes on the `[1.4.0]` release (vendored
  Dailybot skill + full-coverage self-review + opt-in gate).
  Compare-URL footer entries added for `[1.3.1]`–`[1.4.1]` so the
  bracket-link convention is complete for every section.

## [1.4.1] — 2026-07-14

### Changed
- **Self-review dogfood promoted from `lenient` to `block-on-critical`**
  (dogfood-only, no product change). Every provider leg in
  [`.github/workflows/self-review.yml`](.github/workflows/self-review.yml)
  now passes `strictness: block-on-critical` to the action, so a `critical`
  finding on a self-review'd PR turns the check red and blocks the merge
  gate. `warning` and `info` still post inline but don't gate. This is the
  calibrated default recommended in [`docs/STRICTNESS.md`](docs/STRICTNESS.md)
  once a repo has run in `lenient` long enough to trust the model's
  severity assignments. Consumers are unaffected — the action's own
  default remains `lenient` (safe rollout) and `action.yml` is unchanged.

## [1.4.0] — 2026-07-14

**Headline:** vendored the Dailybot agent skill (v3.10.3) into `.agents/skills/dailybot/` and adopted the `skills.sh` lockfile mechanism (`skills-lock.json` at repo root) for pinning both vendored dogfood skills; also closed a coverage hole in the self-review dogfood so every configured provider reviews every `ready`-labeled PR regardless of diff shape. Consumer impact: **none** — the vendored skills live entirely inside `.agents/` and are invisible to the composite-action runtime; the dogfood policy is workflow-only.

### Added
- **Vendored `dailybot` agent skill (v3.10.3) + `skills-lock.json` lockfile at
  repo root.** Both vendored skills — [`.agents/skills/dailybot/`](.agents/skills/dailybot/)
  and the already-vendored [`.agents/skills/deepworkplan/`](.agents/skills/deepworkplan/) —
  are now installed and pinned via the [`skills.sh`](https://skills.sh) CLI
  (`npx skills add DailybotHQ/agent-skill --skill dailybot -y` and
  `npx skills add DailybotHQ/deepworkplan-skill --skill deepworkplan -y`).
  The lockfile records source repo + content hash per skill so any contributor
  can restore identical vendored copies with `npx skills experimental_install`,
  and can bump to the latest upstream release with
  `npx skills update deepworkplan dailybot`. Dailybot integration
  (progress reporting, check-ins, kudos, chat, forms, email, and per-repo API
  keys via `.dailybot/env.json` — CLI 3.7.0+) is now discoverable directly from
  `.agents/skills/dailybot/SKILL.md` without requiring a global install on the
  contributor's machine. This aligns with the existing DWP dogfood-copy pattern
  and matches the Dailybot addon in DWP's `.agents/skills/deepworkplan/addons/dailybot/`
  which expects the Dailybot agent skill ≥ 3.10.3 and CLI ≥ 3.7.0. Consumer
  impact: none — the vendored skills only ship inside this repo's `.agents/`
  tree and are invisible to users of the `DailybotHQ/ai-pr-reviewer` action.
  Docs updated: [`AGENTS.md`](AGENTS.md) (Project Structure + Skills & Agents
  sections), [`.agents/docs/skills_agents_catalog.md`](.agents/docs/skills_agents_catalog.md)
  (new `dailybot` row + a "Vendored skills and the lockfile" subsection with
  the common `npx skills` workflows).

### Changed
- **Self-review dogfood now runs on EVERY `ready`-labeled PR** — no more
  critical-surface filter. Previously the three CLI provider legs
  (`claude-code`, `cursor`, `codex`) only ran when the diff touched a small
  hardcoded list of "critical" paths (`action.yml`, `scripts/reviewer.py`,
  `prompts/*`, the workflows, the main test files), and the Anthropic
  baseline was the only always-on reviewer. That optimized for cost but
  created a coverage hole: docs-only, `.agents/**` (vendored skills) and
  other AI-tooling PRs got zero review unless `ANTHROPIC_API_KEY` was
  configured — and when it wasn't, the merge gate failed red on legitimate
  PRs (e.g. the Dailybot-skill-vendoring PR that lit up this fix). Since
  vendored skills, workflow tweaks, prompt edits, and docs are exactly the
  surface where prompt injection or malicious content can hide, every
  configured provider now reviews every ready-labeled PR regardless of the
  diff shape. A leg is only absent from the matrix when its secret isn't
  configured on the repo. The scope job's `empty_reason` output renamed
  `no-eligible-provider` → `no-provider-secret` to match. The gate's error
  message now instructs "set at least one of ANTHROPIC_API_KEY,
  CLAUDE_CODE_OAUTH_TOKEN, CURSOR_API_KEY, or OPENAI_API_KEY" rather than
  the older "configure ANTHROPIC_API_KEY or touch a critical surface". The
  scope job also no longer checks out the repo (nothing left to diff), so
  the decision runs faster. See
  [`.github/workflows/self-review.yml`](.github/workflows/self-review.yml)
  header comment (Design goals #2 and #3).
- **Self-review gate is now opt-in per PR** (this repo's dogfood only, no
  runtime/action.yml change). The `Self-review gate` job in
  [`.github/workflows/self-review.yml`](.github/workflows/self-review.yml) now
  runs **only when this event was a review-request** (i.e. the `scope` job
  decided a review should run — a `ready` labeling event, or `opened` with
  `ready` already present). Without a review-request event the gate is
  cleanly *Skipped* (grey) instead of red, so a docs/other PR that doesn't
  request a review no longer carries noise in the checks list; an unrelated
  `labeled` event on a PR that happens to carry `ready` also skips (avoids a
  false "no provider eligible" red on the expected empty matrix). When the
  event IS a review-request, the gate still fails hard if no provider leg
  passed (missing `ANTHROPIC_API_KEY` on a non-critical diff, or every leg
  failing). Trade-off: GitHub treats a Skipped required check as *passing*,
  so under this opt-in flow marking the gate `Required` in branch protection
  lets PRs without `ready` merge without a self-review — combine it with a
  separate rule that enforces the `ready` label (a labeler action or
  repository ruleset) if you want to force `ready` on every PR.
  [`docs/TRIGGER_MODES.md` § "Variant — opt-in gate"](docs/TRIGGER_MODES.md)
  documents both flavors (strict = fail without label; opt-in = skip without
  a review-request) with the `empty_reason`-based predicate.

## [1.3.3] — 2026-07-14

### Added
- **README recipe: "Require a passing review before merge (branch protection)"** —
  documents the merge-gate pattern (a stable-named job that *fails* rather than
  *skips* so a required check actually blocks the merge), cross-linked to
  [`docs/TRIGGER_MODES.md`](docs/TRIGGER_MODES.md).

### Changed
- **Merge gate passes when ≥1 provider leg passes** (not all). A single flaky or
  failing provider no longer blocks a merge that another provider reviewed
  cleanly — the gate counts successful `Self-review — <provider>` legs from the
  run's jobs API rather than trusting the all-or-nothing aggregate result.
- **`scope` job explains an empty matrix.** When only a CLI provider is
  configured (e.g. just `CURSOR_API_KEY`) and the diff is non-critical, it emits
  a `::notice::` clarifying that CLI legs review only critical changes and
  Anthropic is the always-on baseline — so the resulting empty matrix (and red
  merge gate) isn't a mystery. (Superseded in [1.4.0](#140--2026-07-14) when the
  critical-surface filter was removed entirely.)

## [1.3.2] — 2026-07-14

### Changed
- **Self-review dogfood has a real merge gate.** The stable-named
  `Self-review gate` job **fails (blocks merge)** when the review ran but no
  leg passed — because GitHub's branch protection treats a *Skipped* required
  check as *passing*, so the per-leg Skipped status alone never blocked a
  merge. Mark **only** `Self-review gate` as the required check (never the
  dynamic `Self-review — <provider>` legs). Documented as a reusable consumer
  recipe in
  [`docs/TRIGGER_MODES.md` § "Recipe: run once when labelled `ready`, block merge until it passes"](docs/TRIGGER_MODES.md).

## [1.3.1] — 2026-07-14

### Changed
- **Post-release documentation polish for v1.3.0** — CHANGELOG promotion,
  Upgrade guide rewrite, and release-notes rewrite. Documentation-only; no
  behavior change to the runtime or `action.yml` contract.

## [1.3.0] — 2026-07-14

**Headline:** the "safe-for-open-source" release. Public-repo abuse defense (new [`author-association`](#author-association-gate-decision-table) gate, defaults ON), deterministic cost defaults for the CLI providers, Claude Code accepts a subscription OAuth token as `api-key`, Codex CLI 0.122+ auth breakage re-fixed, and the Marketplace listing goes live at [`github.com/marketplace/actions/ai-pr-reviewer`](https://github.com/marketplace/actions/ai-pr-reviewer). Consumers pinning `@v1` pick everything up automatically.

### Upgrade guide

The only behavioural change on upgrade is the new [`author-association`](#author-association-gate-decision-table) default. Public-repo consumers get safer defaults for free — private / internal teams that want to keep reviewing every PR must add one line.

| Consumer scenario | Action to take on upgrade |
|---|---|
| Public open-source repo (default) | **Nothing** — safer defaults protect your provider budget. Optionally add [`examples/open-source-safe.yml`](examples/open-source-safe.yml) for the full 3-gate hardening. |
| Private / internal repo, want to review every PR | Add `author-association: ''` (empty) to your workflow inputs to restore v1.2.x behaviour. |
| Public repo, want CONTRIBUTOR reviews too | Set `author-association: 'OWNER,MEMBER,COLLABORATOR,CONTRIBUTOR'`. |
| Strictest — org-members only, block collaborators | Set `author-association: 'OWNER,MEMBER'`. |
| Cost-conscious (smoke-tier model) | Pin `model: claude-haiku-4-5` (Anthropic/Claude Code) or `model: gpt-5.4-mini` (Codex). |
| Claude Pro/Max subscription instead of metered API | Run `claude setup-token` locally, store the `sk-ant-oat…` token as a secret, pass it as `api-key` (see [`docs/PROVIDERS.md` § "Billing Claude Code against a subscription"](docs/PROVIDERS.md)). |

<a id="author-association-gate-decision-table"></a>

**`author-association` gate — recommended value per repo type:**

| Repo type | Recommended value | Rationale |
|---|---|---|
| Public open-source (default) | `OWNER,MEMBER,COLLABORATOR` | Safe default. Blocks external contributors' PRs before any LLM call — closes the LLM-budget-abuse vector where an attacker opens N PRs to burn your provider tokens. |
| Public + selective external | `OWNER,MEMBER,COLLABORATOR,CONTRIBUTOR` | Adds returning contributors (anyone with a merged PR in the repo's history). |
| Private / internal team | `''` (empty — gate disabled) | Every PR is trusted; the abuse vector doesn't apply. |
| Security-critical / regulated | `OWNER,MEMBER` | Strictest. Collaborators (invited-but-not-org-members) are also gated out. |
| Fork-heavy monorepo | `OWNER,MEMBER,COLLABORATOR` **+** `permissions: pull-requests: write` on trusted-fork workflow only | Combine with [`pull_request_target`](docs/SECURITY.md) hardening. |

Full threat model + per-value semantics: [`docs/SECURITY.md` § "Author-association gate"](docs/SECURITY.md).

### Added
- **Claude Code subscription auth** — `provider: claude-code` now accepts a Claude Pro/Max OAuth token as `api-key`, parallel to Cursor's subscription model. Run `claude setup-token` on a logged-in machine, store the `sk-ant-oat…` token as a secret, and the action detects the prefix and forwards it as `CLAUDE_CODE_OAUTH_TOKEN` (subscription billing). Normal `sk-ant-api…` keys still forward as `ANTHROPIC_API_KEY` (metered) — no new input. **Security caveat:** subscription tokens grant broader account access than a scoped key; use only with `persist-credentials: false` on non-fork PRs. Codex has no clean CI equivalent (its ChatGPT-mode OAuth flow is interactive with rotating tokens and likely violates OpenAI's automation terms), so it stays on API-key auth. See [`docs/PROVIDERS.md` § "Billing Claude Code against a subscription"](docs/PROVIDERS.md).
- **New [`author-association`](#author-association-gate-decision-table) input** — comma-separated whitelist of GitHub `pull_request.author_association` values allowed to trigger a review. Defaults to `OWNER,MEMBER,COLLABORATOR` (the safe baseline for public repos). Reads a webhook-payload field the PR author cannot spoof and short-circuits *before* any LLM API call. Composes AND-style with `label-gate` and `trigger-mode` and is evaluated *first* (cheapest gate). Case- and whitespace-insensitive; empty string disables the gate. See [`docs/SECURITY.md` § "Author-association gate"](docs/SECURITY.md) and the ready-to-copy [`examples/open-source-safe.yml`](examples/open-source-safe.yml).

### Fixed
- **Agent-runner recovery from malformed `findings.json`.** PR #11 exposed the failure with `codex-cli 0.144.4`: Codex completed the review and wrote a useful Markdown summary, but one inline-finding string was invalid JSON, so the parser raised `Malformed findings.json` and the job failed. The subprocess boundary now enables an explicit summary-only fallback — malformed finding objects are dropped, the recovered summary is posted with a note, the run logs a warning. Direct parser calls remain strict by default.
- **`provider: codex` — 401 Missing bearer / basic authentication.** Codex CLI 0.122+ stopped reading `OPENAI_API_KEY` from the environment and now reads credentials **only** from `$CODEX_HOME/auth.json`. `codex-cli 0.144.3` (currently on npm) hit this breakage: every `codex exec` reached `api.openai.com/v1/responses` with an empty `Authorization` header and 401'd. `CodexProvider` now materializes an apikey-mode `auth.json` (`{"OPENAI_API_KEY": "..."}`) in an isolated per-run `CODEX_HOME` (`tempfile.mkdtemp(prefix="aiprr-codex-")`, mode `0700`, file mode `0600`) before each invocation and removes the whole tempdir in a `finally` block after. `OPENAI_API_KEY` continues to be forwarded for back-compat with Codex < 0.122. Regression tests in `CodexAuthJsonTests` cover env forwarding, on-disk file shape, permission modes, and cleanup. See [`docs/PROVIDERS.md` § "Codex auth model (0.122+ requires `$CODEX_HOME/auth.json`)"](docs/PROVIDERS.md).
- **`provider: codex` no longer copies ignored MCP JSON config.** After switching Codex auth to an isolated per-run `CODEX_HOME`, the old `mcp-config-file` copy still targeted `~/.codex/mcp.json`, which the subprocess ignored and Codex does not read anyway (`config.toml` is the supported path). The Codex provider now warns without copying the ignored JSON file; use `agent-extra-args` / `config.toml` for Codex MCP setup.

### Changed
- **Marketplace listing renamed back to "AI PR Reviewer"** (`action.yml` `name:` reverted from the v1.2.1 `Dailybot AI PR Reviewer`). The v1.2.1 vendor prefix was a defensive over-fix — the real slug collision (`ai-pull-request-reviewer`, owned by the third-party `appchoose/ai-pr-review`) was on the *full-form* title only. The *abbreviated* title "AI PR Reviewer" slugifies to `ai-pr-reviewer`, a distinct slug that appeared free at the time (Marketplace listing search only — the org-level `github.com/ai-pr-reviewer` collision that later blocked the v1.5.0 publish wasn't discovered until 2026-07-14). Marketplace URL was staged at [`github.com/marketplace/actions/ai-pr-reviewer`](https://github.com/marketplace/actions/ai-pr-reviewer); workflow `uses:` pins are unaffected. Vendor attribution continues via the `author: 'DailybotHQ'` field, which GitHub auto-renders as "by DailybotHQ" beneath the tile. **No consumer action required.** See [`AGENTS.md § 9`](AGENTS.md) for the full naming history including the v1.5.0 final rename to `AI Diff Reviewer`.
- **Default behaviour tightening (soft-breaking) — `author-association: OWNER,MEMBER,COLLABORATOR`.** External-contributor PRs (`author_association` = `CONTRIBUTOR`, `FIRST_TIME_CONTRIBUTOR`, `FIRST_TIMER`, `NONE`) are **no longer reviewed automatically** after upgrading. Public-repo consumers get safer defaults for free; consumers who want v1.2.x behaviour set `author-association: ''`. The SemVer minor bump reflects that the behavioural change is opt-out. See the [Upgrade guide](#upgrade-guide) above for per-repo-type guidance.
- **Explicit, quality-tier default models for the CLI providers.** The CLI providers no longer default to `auto` (which deferred to the account default and could silently be Opus at ≈$5/$25). The action now pins an explicit quality-tier model per provider: **`claude-code` → `claude-sonnet-4-6`** (quality/price sweet spot); **`codex` → `gpt-5.6-luna`** (≈$1/$6 per 1M tokens; current-gen budget model, replaces the now-deprecated `gpt-5-codex` at ≈$1.75/$14). The `anthropic` default stays `claude-sonnet-4-6` and Cursor stays `auto` (flat-rate/unlimited on Pro). Consumers pin a cheaper smoke model (`claude-haiku-4-5` ≈$1/$5, `gpt-5.4-mini` ≈$0.75/$4.50) via `model:`. See [`docs/PROVIDERS.md` § "Choosing a cost-efficient model"](docs/PROVIDERS.md).
- **Label matching is now case-insensitive.** `label-gate` (and its `label-once` / `label-added-only` trigger logic) compares label names on a lowercased, whitespace-trimmed basis — `label-gate: ready` is satisfied by `ready`, `Ready`, or `READY`. Applies to `resolve_trigger_action`, `gh_pr_has_label`, and `count_label_events`; removes a foot-gun where a capitalized label silently failed to trigger.
- **Self-review dogfooding is now cost-scoped, model-pinned, and label-gated.** Three complementary changes bound dogfood spend: (1) the `anthropic` leg is the always-on smoke baseline with `max-turns: 12` (down from the consumer default `30`); the `claude-code`, `cursor`, and `codex` legs only fire when the diff touches provider-sensitive surfaces (`action.yml`, `scripts/reviewer.py`, prompts, core workflow files, or provider/runtime tests); (2) each leg pins an explicit cheap model — Anthropic and Claude Code on `claude-haiku-4-5`, Codex on `gpt-5.4-mini`, Cursor on `auto` (unlimited on Pro); (3) the whole dogfood is gated on a `ready` label + `trigger-mode: label-once`, so the maintainer holds explicit control of dogfood spend. Routine docs/README PRs stay cheap while full provider parity is still exercised on the changes that can realistically break it.

## [1.2.1] — 2026-07-14

**Headline:** the "actually-works-on-Marketplace" release — renames the Marketplace listing to unblock the first-time publish (a squatting `appchoose/ai-pr-review` action already owns the un-prefixed slug) and ships two provider-side fix batches that landed on `main` after `v1.2.0` was tagged (`claude-code` and `codex` were both broken out of the box in `v1.2.0`; this patch is what makes those providers actually usable). Consumers pinning `@v1` pick everything up automatically.

### Changed
- **Marketplace listing renamed to "Dailybot AI PR Reviewer"** (`action.yml` `name:`). The un-prefixed name was mis-diagnosed as slug-ifying to `ai-pull-request-reviewer`, which is claimed by an unrelated third-party action (`appchoose/ai-pr-review`, v1.1.5). The vendor-prefix pattern was the assumed Marketplace resolution and kept the repo slug, docs, and user-facing product copy on "AI PR Reviewer". This was reverted in v1.3.0 after re-checking Marketplace slug availability. No workflow changes required — `uses: DailybotHQ/ai-pr-reviewer@v1` is unaffected.
- **Default Cursor model is now `auto`** (was `composer-2.5`). `auto` is unlimited on Cursor Pro plans and is the CI recommendation in `docs/PROVIDERS.md`; the default now matches the docs. Pin `composer-2.5` (or any specific model) via `model:` if you want to force one.
- **`collapse-previous` is now scoped per provider.** Every review body and tracking comment carries an invisible `<!-- ai-pr-reviewer-provider: <id> -->` marker, and `collapse-previous` only minimizes *this provider's own* prior artefacts. Effects: (1) several providers can review the same PR concurrently — even sharing one `GITHUB_TOKEN` — without collapsing each other (`self-review.yml`'s four-provider matrix keeps the default `true` and relies on the scoping); (2) unrelated `github-actions[bot]` comments (coverage bots, labelers) are no longer collapsed. See `docs/PROVIDERS.md` § "Running more than one provider on the same PR". Transition: a single pre-upgrade review without the marker won't be auto-collapsed on the first run after upgrading.
- **`agent-max-turns` now warns instead of silently doing nothing** for the CLI providers. None of the shipping CLIs (Claude Code, Cursor, Codex) expose a turn-count cap flag, so the input can't be forwarded; the run logs a clear warning pointing at `agent-extra-args` and noting the `CLI_INVOCATION_TIMEOUT` (900 s) as the effective bound, rather than leaving a misleading dead input.

### Fixed
- **`provider: claude-code` and `provider: codex` now actually produce reviews.** Both were broken out of the box and failed on essentially every PR:
  - **Claude Code** received its review rubric + `findings.json` output contract as a literal *file path* (`--append-system-prompt <path>`) instead of text, so the instructions never reached the model — it was never told to write findings and the run failed with `FileNotFoundError`. Now the instructions are passed as text via `--append-system-prompt`.
  - **Claude Code** ran in the default headless permission mode, which denies the `Write` tool in non-interactive CI, so it could not emit `findings.json` even when instructed. Now invoked with `--permission-mode bypassPermissions` (the runner is already an isolated ephemeral sandbox; mirrors Cursor's `--force --trust`).
  - **Codex** ran `codex exec` in its default read-only sandbox and physically could not write `findings.json`. Now invoked with `--dangerously-bypass-approvals-and-sandbox` (documented for externally-sandboxed CI environments).
- **Large PRs no longer crash `claude-code` / `codex` with `E2BIG`.** Both embedded the full diff (up to 200 KB) in a single argv argument, exceeding the Linux ~128 KB per-argument limit. The prompt is now piped via stdin (`claude -p` reads stdin; `codex exec -`), matching the fix Cursor already had.
- **Agent-runner prompt hygiene.** The user prompt handed to the CLI providers referenced the chat-completions-only tools `post_inline_comment` / `submit_review`, which don't exist for a vendor CLI. Agent-runner providers now get a tailored prompt that points at the `findings.json` output contract instead of contradictory tool names.
- **Claude Code MCP passthrough now takes effect.** `mcp-config-file` was copied to `~/.claude/mcp.json`, which Claude Code does not read — the passthrough silently did nothing. The CLI is now invoked with `--mcp-config <file>` pointing at the consumer's config.
- **Codex MCP passthrough now warns instead of silently no-op'ing.** Codex configures MCP via `~/.codex/config.toml`, not a JSON file, so `mcp-config-file` never took effect for `provider: codex`. The run now logs a clear warning pointing at `agent-extra-args` / `config.toml` instead of pretending it worked. (Full Codex MCP support is a documented follow-up.)
- **Vendor CLIs now inherit proxy and custom-endpoint config.** `_build_cli_env` forwards `HTTP(S)_PROXY` / `NO_PROXY` and `ANTHROPIC_BASE_URL` / `OPENAI_BASE_URL` (non-secret network config) so agent-runner providers work on proxied / self-hosted runners and against compatible gateways.

### Security
- **`docs/SECURITY.md`** now documents the real exfiltration surface of the agent-runner providers (vendor API key in the CLI subprocess env + `GITHUB_TOKEN` persisted by `actions/checkout` in `.git/config`, both reachable by an injected CLI) and corrects the prior blast-radius claim, which only held for `provider: anthropic`. Recommends running agent-runner providers on trusted/non-fork PRs only and setting `persist-credentials: false`.
- **Runtime secret scrubbing.** The provider API key and GitHub token are registered as literal secret values (`register_secret`) and scrubbed (`scrub_secrets`) from the review summary, every inline-comment body, and any failure message before it is posted to the PR — a defense-in-depth backstop against a prompt-injected vendor CLI echoing its key into a public comment.

## [1.2.0] — 2026-07-11

**Headline:** the "configurable review workflow" release — five new inputs that let consumers control when the review fires, how the prompt is composed, whether the PR description is auto-completed, whether complexity labels are applied, and a fourth strictness tier for zero-tolerance stacks. Every knob is additive and opt-in; consumers on `@v1` see zero behavioural drift.

### Added
- **New `trigger-mode` input** with four values: `always`, `label-required`, `label-once`, `label-added-only`. Enables precise control over when the reviewer runs — including a "review once per label application" workflow where re-running requires toggling the label off and on. See [`docs/TRIGGER_MODES.md`](docs/TRIGGER_MODES.md).
- New helpers `count_label_events`, `resolve_trigger_action`, `read_trigger_state`, `write_trigger_state`. Marker state (a JSON blob in an HTML comment inside the tracking comment) carries the `label_toggle_generation` counter that powers `label-once`.
- **New `pr-description-mode` input** with four values: `off` (default), `warn`, `block`, `autocomplete`. When `autocomplete` is used, the AI writes a first-draft PR body when the current body is missing or too vague. Guarded by a marker so it never overwrites maintainer edits. See [`docs/PR_METADATA_CHECKS.md`](docs/PR_METADATA_CHECKS.md).
- **New `pr-description-min-length` input** (default `50`) — character threshold below which the body is treated as "missing/vague."
- **New `complexity-labels-enabled` input** — when `true`, the reviewer assesses PR complexity (`low`/`medium`/`high`) based on cognitive load, files touched, security surface, and coverage delta, then applies a `complexity:*` label.
- **New `complexity-label-prefix` input** (default `complexity:`) — configurable prefix for the applied complexity label.
- **New `set_pr_description` and `set_pr_complexity` tools** in the chat-completions tool schema, gated by the new inputs (exposed only when the corresponding feature is enabled).
- New GitHub API surface used: `PATCH /pulls/{n}` (autocomplete) and `DELETE /issues/{n}/labels/<name>` (complexity relabelling). See [`docs/SECURITY.md`](docs/SECURITY.md) § "PR metadata PATCH surface" for the threat model.
- **New `prompt-extension-file` input** — APPENDS content to the base system prompt (either the bundled default or a custom `prompt-file`) with a `---` separator. Layer stack-specific severity overrides and house rules without copy-pasting the entire default. Three starter extensions ship in `examples/prompts/` (`python-strict.md`, `typescript-strict.md`, `security-focused.md`).
- **Meta-prompt** at `examples/prompts/generate-custom-prompt-meta.md` — hand it to your favorite coding AI (Claude Code, Cursor, Codex, ChatGPT, Gemini) with your repo checked out, and the AI produces a repo-tailored `prompt-file`. Solves the blank-page problem for the full-replacement path.
- **New strictness mode `block-on-any`** — fails the GitHub check when the reviewer posts any inline comment, including `info`. Zero-tolerance mode for security-critical and regulated stacks. See [`docs/STRICTNESS.md`](docs/STRICTNESS.md) for the full decision tree.
- Documentation of the Cursor CLI billing model in `docs/PROVIDERS.md` (subscription-only, no BYOK, `model: auto` unlimited on Pro plans) — resolves consumer confusion about which API keys are compatible with `provider: cursor`.

### Changed
- `label-gate` semantics preserved for back-compat, now internally implemented as `trigger-mode: label-required` with `label-gate` supplying the label name. Consumers that only set `label-gate` see zero behavioural drift.
- `CursorProvider` now passes `--force --trust` by default in its headless invocation, per Cursor's own [Headless CLI docs](https://cursor.com/docs/cli/headless) recommendation for CI. Adds `--approve-mcps` conditionally when `mcp-config-file` is set, so the interactive MCP-approval prompt does not stall unattended runs. Consumers do not need to add these flags manually via `agent-extra-args`; the change is fully backward-compatible.
- `examples/provider-cursor.yml` now sets `model: auto` explicitly as the recommended CI default.
- `docs/PERFORMANCE.md` § "Two performance shapes" — added a Billing row clarifying that Cursor consumes subscription credits while other agent-runner providers use metered vendor API tokens.

### Fixed
- **CursorProvider E2BIG on large PRs.** The Cursor CLI concatenated review instructions + PR diff and passed the whole string as a positional argv token (`cursor-agent -p <200 KB…>`), which exceeded the Linux `ARG_MAX` (~128 KB) and crashed the review before the CLI could start (`OSError: [Errno 7] Argument list too long`). `_invoke_cli_agent()` now accepts an optional `stdin_input=` parameter; `CursorProvider.run_review()` pipes the prompt via stdin (`cursor-agent -p` with no positional argument), unblocking reviews of PRs whose diff alone can exceed 200 KB. Regression covered by `CursorHeadlessDefaultsTests.test_user_prompt_not_in_argv_and_goes_via_stdin`.
- Other providers (`anthropic`, `claude-code`, `codex`) were unaffected — Claude Code writes the system prompt to a file via `--append-system-prompt` and Codex's prompt shape stays under `ARG_MAX` in practice.
- **`label-added-only` no longer fires on unrelated labels.** When `label-gate` was already present on a PR and a webhook added a different label (e.g. `bug` or a Dependabot label), the workflow would still enter this action and pay for a full review. `_read_github_event_label()` now surfaces `event.label.name`; `resolve_trigger_action()` requires it to match `label-gate` in `label-added-only` mode. Consumers relying on `label-added-only` avoid stray runs and stray billing. Regression covered by `ResolveTriggerActionTests.test_label_added_only_skips_when_event_label_is_unrelated`.
- **Silent no-op on agent-runner providers now logs a `WARNING`.** Enabling `pr-description-mode: autocomplete` or `complexity-labels-enabled: true` with `provider: cursor|claude-code|codex` never populated the corresponding `state.proposed_*` fields (the tools are chat-completions-only in v1.2). Consumers used to pay for a review with nothing to show for those inputs. `main()` now emits a `WARNING:` line at run start listing which inputs will no-op, and `docs/PR_METADATA_CHECKS.md` § "Provider support matrix" documents the current split. Extracted into a testable helper `build_agent_runner_noop_warning()`.
- **`label-once` no longer skips silently when `count_label_events()` returns 0.** Previously `label_toggle_generation <= last_reviewed_generation` treated `0 <= 0` as "already reviewed" — so a transient timeline-API failure while the gate label WAS on the PR caused a silent skip with `should_run=False (already reviewed label generation 0 …)`. The check now requires `label_toggle_generation > 0` before treating a run as stale. Better to run and deliver a review than skip and deliver nothing. Regression covered by `ResolveTriggerActionTests.test_label_once_runs_when_count_zero_but_label_present`.
- **`count_label_events()` now logs a `WARNING` when the 20-page pagination cap is hit.** On long-lived, high-chatter PRs the safety bound could undercount the generation, and `label-once` re-runs would silently refuse to fire. The cap stays (cost control), but now announces itself so operators see why the mode is stuck. Documented workarounds in `docs/TRIGGER_MODES.md` § "Edge cases": toggle the label twice, or switch to `label-added-only` for that PR. Regression covered by `CountLabelEventsTests.test_logs_warning_when_pagination_cap_hit`.
- **README `prompt-extension-file` comment.** The recipe said the input was "mutually exclusive with `prompt-file`, or complementary" — the two are actually composable ("custom base + extension"). Comment rewritten to match `docs/PROMPTS.md` and `action.yml`.
- **`collapse-previous` silently failed on `${{ secrets.GITHUB_TOKEN }}`.** `gh_get_authenticated_login()` unconditionally called `GET /user`, which returns `403 Forbidden` for the built-in workflow installation token (a well-known GitHub limitation). The exception was swallowed by the outer try/except and logged as non-fatal, meaning the entire `minimizeComment` GraphQL step never ran — every consumer using the recommended `github-token: ${{ secrets.GITHUB_TOKEN }}` pattern lost the "hide previous reviews as outdated" feature since v1.0, without noticing. The function now walks a 4-tier fallback chain: (1) `/user` for PATs, (2) `/app` for GitHub App tokens (returns `<slug>[bot]`), (3) marker-scan the PR's issue comments for `<!-- ai-pr-reviewer-marker -->` and use that comment's author, (4) hardcoded default `github-actions[bot]`. Regression covered by seven `GhGetAuthenticatedLoginFallbackTests` cases across all four tiers plus the empty-login edge case. New public constant `DEFAULT_WORKFLOW_BOT_LOGIN`.
- **`collapse-previous` login-shape mismatch between REST and GraphQL.** Even with the 4-tier fallback landed, the dogfood run logged `Collapsed 0/N previous bot artefact(s)` because REST returns `.user.login = "github-actions[bot]"` while GraphQL returns `.author.login = "github-actions"` (no `[bot]` suffix) on the same Bot node. The naive equality check filtered every bot artefact out. The filter now accepts both shapes for the comparison (`bot_login` and `bot_login` stripped of the `[bot]` suffix). Regression covered by four `GhCollapsePreviousReviewsTests` cases — matches without suffix, matches with suffix, skips already-minimized nodes, ignores other bots (dependabot/renovate stay untouched).

## [1.1.0] — 2026-07-05

**Headline:** three new agent-runner providers (`claude-code`, `cursor`, `codex`) alongside the incumbent `anthropic` chat-completions provider — zero migration cost for consumers on `@v1`. See [`.dwp/plans/PLAN_multi_cli_provider_expansion/analysis_results/EXECUTIVE_REPORT.md`](.dwp/plans/PLAN_multi_cli_provider_expansion/analysis_results/EXECUTIVE_REPORT.md) for the full breakdown.

### Added
- **Multi-CLI provider expansion** — three new agent-runner providers that shell out to their vendor's coding-agent CLI in headless mode and receive findings via a file-based contract (`.aiprr/findings.json`):
  - `provider: claude-code` — installs `@anthropic-ai/claude-code` via npm; auth via `ANTHROPIC_API_KEY`.
  - `provider: cursor` — installs `cursor-agent` via `curl` (`cursor.com/install`); auth via `CURSOR_API_KEY`.
  - `provider: codex` — installs `@openai/codex` via npm; auth via `OPENAI_API_KEY`.
- New abstract `AgentRunnerProvider` peer of `Provider`. `build_provider()` now returns either family; `main()` dispatches on `isinstance`.
- New `Finding` + `ReviewResult` dataclasses provide the provider-independent submission-path payload.
- New `parse_findings_file()` parser + validator with strict schema enforcement (required fields, allowed severity/side enums, forward-compat with vendor extensions).
- New `write_findings_prompt_directive()` — standardises the "write your findings here" instruction appended to review prompts across all CLI providers.
- New optional inputs: `agent-max-turns`, `agent-extra-args`, `mcp-config-file`, `claude-code-version`, `cursor-version`, `codex-version`.
- Modular install in `action.yml`: each CLI install step is guarded by `if: inputs.provider == '...'`, so consumers picking the default `provider: anthropic` pay zero install overhead. One provider = one install.
- MCP servers passthrough: `mcp-config-file` copies the consumer's JSON config into the CLI's expected location (with round-trip backup) before invocation.
- New examples: `provider-claude-code.yml`, `provider-cursor.yml`, `provider-codex.yml`, `mcp-passthrough.yml`.
- New CI job `cli-install-smoke` — matrix over the three CLI providers exercising each installer script on a fresh runner, catching installer drift before it reaches consumers.
- Dogfooding matrix in `.github/workflows/self-review.yml` — every PR to this repo now runs a 4-leg review (`anthropic`, `claude-code`, `cursor`, `codex`) with per-provider `self-reviewed:*` labels.
- 67 new unit tests (109 total, up from 42) covering: adapter (state → ReviewResult), findings.json parser (happy + error paths), provider dispatch, MCP passthrough, subprocess boundary, security invariants (no `shell=True`, no `os.system`, all `extra_args` funnel through `shlex.split`), CLI env allowlist, and end-to-end serialization roundtrips across both provider families.

### Changed
- `gh_submit_review_with_fallback()` now accepts a `ReviewResult` (was: `body` + `inline_comments`). The submission path is provider-agnostic; findings are encoded to the GitHub Reviews inline shape at the boundary via `findings_to_gh_inline_comments()`.
- Refreshed `docs/PROVIDERS.md` with the Agent Runner Provider Contract section documenting the schema, validation, and prompt directive.
- Refreshed `docs/ARCHITECTURE.md` with the two-provider-family design decision and the modular-install approach.
- Refreshed `README.md` inputs table + provider roadmap with the four shipping providers, categorised by family.
- Refreshed `.agents/agents/provider-implementer.md`, `.agents/skills/add-provider/SKILL.md`, `.agents/agents/reviewer.md`, and `.agents/docs/skills_agents_catalog.md` for the two-family model.

### Fixed
- N/A — additive release. Existing `provider: anthropic` consumers see zero behavioural drift.

### Security
- `_invoke_cli_agent()` enforces argv-list subprocess invocation (no `shell=True`).
- All consumer-provided `agent-extra-args` are parsed with `shlex.split` before being appended to the CLI invocation.
- MCP config passthrough uses `shutil.copyfile` (not `shell=True` copy) and round-trips any pre-existing user config so an interrupted run doesn't leave stale state.
- **New `_build_cli_env(extra_vars=...)` helper** — vendor CLI subprocesses receive an explicit env allowlist (`PATH`, `HOME`, `NODE_PATH`, locale, runner metadata) plus the vendor API key only. `AIPRR_GH_TOKEN` and all other `AIPRR_*` variables stay in the parent process; enforced by static `CliEnvAllowlistTests`. Addresses Security-Review Finding #2.
- **`max-inline-comments` cap now enforced on the agent-runner path** — previously only enforced by the chat-completions tool handler. `main()` truncates `result.findings` to `max_inline_comments` after `provider.run_review()` and recomputes `overall_severity` on the retained subset. Addresses Security-Review Finding #1.
- **Documented accepted risks** in `docs/SECURITY.md`: (a) Cursor installer supply chain (`curl | bash`, no signed installer offered by vendor); (b) MCP config persistence after SIGKILL on self-hosted persistent runners.

### CI
- `code_check.yml` gains a `cli-install-smoke` matrix job (claude-code / cursor / codex).
- `self-review.yml` becomes a 4-leg matrix; `fail-fast: false` + `timeout-minutes: 25`.

## [1.0.0] — 2026-05-29

Initial public release.

### Added
- Composite GitHub Action that runs an LLM-driven code review on every pull request.
- Anthropic provider (`claude-sonnet-4-6` default), with `Provider` abstraction ready for OpenAI/Gemini drop-ins.
- Five-tool agentic loop: `read_file`, `grep`, `glob`, `post_inline_comment`, `submit_review`.
- Severity tagging (`critical` / `warning` / `info`) on every inline comment, surfaced as the `severity` action output.
- Three strictness modes (`lenient`, `block-on-critical`, `block-on-warning`) to gate the GitHub check.
- Optional `label-gate` input — only run when the PR carries a configured label.
- Optional `applied-label` input — auto-apply a label after a successful, non-blocked review (with auto-create if the label doesn't exist).
- Auto-collapse of previous bot reviews/comments via GraphQL `minimizeComment`.
- Tracking spinner comment with `<!-- ai-pr-reviewer-marker -->` marker, transitioning in-place from `Working…` to `View review →` (or `failed`).
- 422 fallback: if GitHub rejects the review because one inline comment anchored outside the diff, the action retries summary-only instead of losing every comment.
- Bundled default system prompt that's technology-agnostic and includes severity definitions.
- Bounded retries on Anthropic 429/5xx; bounded conversation pruning to keep token cost from compounding.
- Documentation: README, PROMPTS guide, STRICTNESS guide, PROVIDERS roadmap.
- Examples: `basic.yml`, `label-gated.yml`, `strict.yml`, `custom-prompt.yml`.
- `code_check` workflow gating every PR/push to `main` (compile, `action.yml`
  contract validation, actionlint, unit tests).
- `auto-release` workflow: SemVer bump from Conventional Commits on merge to
  `main`, tag + major-alias move + GitHub Release (tag-only, no commit to
  protected `main`).
- Stdlib-`unittest` test suite under `tests/` for the runtime's pure logic.
- Self-review workflow dogfooding the action on its own PRs.
- Repo hygiene: issue/PR templates and Dependabot for GitHub Actions.

[Unreleased]: https://github.com/DailybotHQ/ai-diff-reviewer/compare/v1.6.0...HEAD
[1.6.0]: https://github.com/DailybotHQ/ai-diff-reviewer/compare/v1.5.0...v1.6.0
[1.5.0]: https://github.com/DailybotHQ/ai-diff-reviewer/compare/v1.4.2...v1.5.0
[1.4.2]: https://github.com/DailybotHQ/ai-pr-reviewer/compare/v1.4.1...v1.4.2
[1.4.1]: https://github.com/DailybotHQ/ai-pr-reviewer/compare/v1.4.0...v1.4.1
[1.4.0]: https://github.com/DailybotHQ/ai-pr-reviewer/compare/v1.3.3...v1.4.0
[1.3.3]: https://github.com/DailybotHQ/ai-pr-reviewer/compare/v1.3.2...v1.3.3
[1.3.2]: https://github.com/DailybotHQ/ai-pr-reviewer/compare/v1.3.1...v1.3.2
[1.3.1]: https://github.com/DailybotHQ/ai-pr-reviewer/compare/v1.3.0...v1.3.1
[1.3.0]: https://github.com/DailybotHQ/ai-pr-reviewer/compare/v1.2.1...v1.3.0
[1.2.1]: https://github.com/DailybotHQ/ai-pr-reviewer/compare/v1.2.0...v1.2.1
[1.2.0]: https://github.com/DailybotHQ/ai-pr-reviewer/compare/v1.1.0...v1.2.0
[1.1.0]: https://github.com/DailybotHQ/ai-pr-reviewer/releases/tag/v1.1.0
[1.0.0]: https://github.com/DailybotHQ/ai-pr-reviewer/releases/tag/v1.0.0
