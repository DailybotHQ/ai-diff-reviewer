# Review overrides for ai-diff-reviewer

This repo IS the AI Diff Reviewer — a stdlib-only Python composite GitHub
Action (`scripts/reviewer.py`, ~4000 LOC in one file) that ships to a global
audience via the GitHub Marketplace. Every finding here is high-stakes:
runtime changes affect thousands of PRs; contract changes break consumers;
security regressions leak tokens on public runners. Calibrate accordingly.

The load-bearing rules live in [`AGENTS.md`](../AGENTS.md); this file
overrides the base prompt for the patterns most likely to slip a review.

## Severity overrides for this codebase

- **Always `critical`:** any non-stdlib `import` added to
  `scripts/reviewer.py` (Rule #2 — the load-bearing constraint that lets
  the action stay a zero-install composite). `requests`, `httpx`,
  `pyyaml`, `pydantic`, `click`, `tenacity`, `loguru` — all rejected. Use
  `urllib.request`, `dataclasses`, `argparse`-via-env-vars, hand-rolled
  retry loops.
- **Always `critical`:** any `subprocess.run(..., shell=True)` anywhere in
  `scripts/`. Argv-list form only — see `docs/SECURITY.md § "Subprocess
  argument injection protection"`. `_invoke_cli_agent()` and `run_cmd()`
  are the codified helpers; do not hand-roll a `subprocess.run` for a new
  CLI or command.
- **Always `critical`:** any tool taking a path argument (`tool_read_file`,
  `tool_grep`, new tools) that does not route through `safe_repo_path()`.
  Bypassing it opens path traversal (`../../etc/passwd`), absolute-path
  reads, and symlink escapes on the runner.
- **Always `critical`:** a new `AIPRR_*` env var forwarded into a vendor
  CLI subprocess without being added to `_CLI_ENV_ALLOWLIST` in
  `_build_cli_env()`. The scrub is the reason `AIPRR_GH_TOKEN` doesn't
  leak to Claude Code / Cursor / Codex; adding a var without allowlisting
  breaks that boundary silently.
- **Always `critical`:** removal or rename of any `action.yml` input or
  output (Rule #4 — public contract). Same for the moving major tag `v1`.
  Breaking these needs a coordinated `v2.0.0` bump. Adding a new
  **optional** input is fine.
- **Always `critical`:** rename of any HTML marker string:
  `<!-- ai-pr-reviewer-marker -->`, `<!-- ai-pr-reviewer-state: … -->`,
  `<!-- ai-pr-reviewer-provider:… -->`,
  `<!-- ai-pr-reviewer-description-autocompleted -->`. These are stable
  contracts on **already-posted** PR comments across every consumer repo;
  renaming silently breaks `collapse-previous` and idempotency
  (`docs/STANDARDS.md § "Marker constants"`).
- **Always `critical`:** any `print(os.environ["AIPRR_API_KEY"])` /
  `log(f"key={api_key}")` / logging path that echoes an env var matching
  the redaction substrings (`token`, `key`, `secret`, `password`,
  `auth`). Use `redact_for_log()` for tool-arg logging;
  `register_secret` + `scrub_secrets` are the outbound gate for anything
  posted to a PR body.

- **Always `warning`:** missing type hints on new function signatures in
  `scripts/**.py` (Rule #3, `docs/DEVELOPMENT_GUIDELINES.md § "Type hints
  (mandatory)"`). Includes parameters, return type, and meaningful local
  variables. Modern syntax: `dict[str, Any]` not `Dict`, `list[X] | None`
  not `Optional[List[X]]`.
- **Always `warning`:** `except Exception` in `scripts/reviewer.py`
  without both `# noqa: BLE001` AND an inline comment explaining WHY.
  Three approved patterns only: "best-effort GH API call, never blocks
  the review", "surface to model rather than crash", "wrap loop so
  failures hit the spinner". Any other broad except is either mis-scoped
  (should be narrower) or a bug hiding in noise.
- **Always `warning`:** a magic number or magic string inlined into
  `scripts/reviewer.py` when it has meaning (a timeout, a cap, a
  severity name, an API URL, a marker template). Promote to a module-level
  `SCREAMING_SNAKE_CASE` constant at the top of the file — see the
  Constants section for the canonical shape.
- **Always `warning`:** a change to `action.yml` inputs/outputs that
  isn't mirrored in ALL of: `README.md` inputs table, `CHANGELOG.md`
  `[Unreleased]`, `skills/ai-diff-reviewer/setup/reference.md`, at least
  one file under `examples/`. The setup skill's reference is the local
  companion's manual — drift makes the "any agent can answer setup
  questions" promise silently false (AGENTS.md Rule #7).
- **Always `warning`:** hand-edit of `.agents/skills/ai-diff-reviewer/**`
  on a feature branch. That directory is the vendored released version;
  it's refreshed automatically by `auto-release.yml` Step 3.5 after each
  tag. Work on `skills/ai-diff-reviewer/**` (the source-of-truth).
  See AGENTS.md Rule #10 pillar (B) and DON'T #14.
- **Always `warning`:** any new step in `.github/workflows/*.yml` that
  pipes attacker-controlled PR content (`title`, `body`, `pull_request.*`)
  into a shell command via `${{ ... }}` interpolation — that's remote
  code execution on the runner. Use `env:` mapping instead
  (`actionlint SC2086` guidance).

- **Escalate to `warning`:** Spanish or any non-English text in code,
  comments, docstrings, docs, PR titles/bodies, or commit messages
  (Rule #1 — global audience). The base prompt might treat this as
  `info`-level style; in this repo it's a usability regression.
- **Escalate to `warning`:** an increase of `MAX_TURNS`,
  `MAX_INLINE_COMMENTS`, `MAX_TOOL_OUTPUT_BYTES`, or any cap in
  `scripts/reviewer.py` without a cost-per-review estimate in the PR
  description. These caps are billing decisions for every consumer.

- **De-escalate to `info`:** `scripts/reviewer.py` line count creeping
  toward the ~4000 LOC soft ceiling (`docs/STANDARDS.md § "File size"`).
  The ceiling is a prompt for a "should we split" conversation, not a
  merge blocker. Same for test files past 500 LOC.
- **De-escalate to `info`:** the byte-copy of `prompts/default.md` at
  `skills/ai-diff-reviewer/prompt.md` — CI enforces they're identical
  (`code_check.yml § "Skills — prompt-sync invariant"`), so this is not
  dead code duplication.

## Don't comment on

- Missing unit tests for code that exercises live LLM provider APIs
  (Anthropic / Claude Code / Cursor / Codex). The strategy is dogfooding
  via `self-review.yml` + `cli-install-smoke`, not mocking every network
  boundary — see `docs/DEVELOPMENT_GUIDELINES.md § "Test discipline"`
  ("Don't mock external APIs end-to-end").
- Absence of `requirements.txt` / `pyproject.toml` / `setup.py` /
  `Pipfile` / virtualenv. The stdlib-only stance is the deliberate
  competitive moat (Rule #2 / `docs/PRODUCT_SPEC.md`).
- Absence of `pytest`, `black`, `ruff`, `mypy`, or any third-party tool
  in `.github/workflows/code_check.yml`. Stdlib `unittest` + `py_compile`
  + `actionlint` are the deliberate CI set.
- `# noqa: BLE001` **when accompanied by** an inline WHY comment
  matching one of the three codified patterns. That combination is
  intentional and load-bearing (`docs/DEVELOPMENT_GUIDELINES.md § "Error
  handling"`); flagging it is false-positive noise.
- Formatting or whitespace in `.claude/**` or `CLAUDE.md` — those are
  symlinks. Any content edit belongs at `.agents/**` and `AGENTS.md`
  (Rule #12 / DON'T #12). If the diff touches the symlink target
  itself, that IS the bug worth flagging.
- The `<!-- ai-pr-reviewer-marker -->`-family HTML strings appearing
  verbatim in `scripts/reviewer.py` and tests. Preserved across the
  v1.5.0 rename on purpose (Rule #9) for back-compat with existing PR
  comments; not stale copy.

## Repo-specific conventions

- **Subprocess:** use `run_cmd()` for internal commands (git, etc.) or
  `_invoke_cli_agent()` for vendor CLIs. `shell=False` always; never
  string-interpolate a user-controlled value into a command.
  `shlex.split()` on any user-provided arg string (`agent-extra-args`).
- **Paths:** any model-supplied or user-supplied path routes through
  `safe_repo_path(repo_root, candidate)`. Direct `Path.resolve()` on
  attacker-influenced input is a bug.
- **Env vars for the CLI subprocess:** only `_CLI_ENV_ALLOWLIST` entries
  are forwarded. Adding a variable requires an inline comment explaining
  why it's safe to hand to a third-party binary.
- **Logging:** use the `log(msg)` helper — writes to stdout with a
  `[ai-diff-reviewer]` prefix. Don't `print()` directly; don't
  `import logging`. Never log the value of any env var whose key matches
  the redaction substring list.
- **Constants at the top:** module-level `SCREAMING_SNAKE_CASE`. Examples
  already codified: `MAX_TOOL_OUTPUT_BYTES: int = 32_000`,
  `DEFAULT_MAX_TURNS: int = 30`, `SEVERITY_CRITICAL: str = "critical"`.
  Full annotation is mandatory even on the LHS.
- **Action outputs:** use `write_action_output(name, value)` for any new
  value consumers should read in a downstream workflow step. Direct
  `print(f"::set-output ...")` is deprecated and not the pattern here.
- **Commit format:** Conventional Commits with body structure
  `## Summary / ## Change Log / ## Risks` — the same shape the PR
  description should follow (`docs/STANDARDS.md § "Commits"`).
- **Branding:** the product name is exactly **"AI Diff Reviewer"** in
  user-facing copy; slug is `ai-diff-reviewer`; env prefix is `AIPRR_`.
  Do not introduce variants ("AI-Diff-Reviewer", "AIDR", "AI PR
  Reviewer") — Rule #13.
- **Symlinks:** `.claude/` → `.agents/`, `CLAUDE.md` → `AGENTS.md`.
  Never create a real file at `.claude/foo` or edit `CLAUDE.md`
  directly — edit the canonical.

## Test-strategy expectations

- New pure-logic function in `scripts/reviewer.py` → matching case in
  `tests/test_reviewer.py` (stdlib `unittest`, no `pytest`).
- New tool implementation (`tool_foo`) → `ToolFooTests` class following
  the existing pattern in `tests/test_reviewer.py`.
- New agent-runner provider or CLI helper (`_invoke_cli_agent`,
  `_build_cli_env`) → cases in `tests/test_agent_runner_providers.py`,
  including the subprocess-security invariants
  (`shell=False`, env allowlist).
- New findings-file parsing → cases in `tests/test_findings_parser.py`.
- Split at ~500 LOC per test file rather than growing an existing one
  past that (four-file split is the codified model).
- Any change to the agentic loop, `prompts/default.md`, or the
  review-submission path MUST be verified by `self-review.yml` on the
  PR (Rule #10 pillar A). If it can't (e.g. only fires on
  `block-on-warning`), describe the manual verification in the PR body.

## PR hygiene

- PR title in Conventional Commits format (matches the squash-merge
  subject).
- PR body follows `## Summary / ## Change Log / ## Risks`.
- If `action.yml` changed → verify README's inputs table AND
  `skills/ai-diff-reviewer/setup/reference.md` are both updated in the
  same PR.
- If `prompts/default.md` changed → link before/after review evidence on
  a real PR (Rule #7 / DON'T #6).
- If any skill under `skills/ai-diff-reviewer/**` changed → do NOT also
  edit the vendored `.agents/skills/ai-diff-reviewer/**`; Step 3.5 of
  auto-release handles it (Rule #10 pillar B).
