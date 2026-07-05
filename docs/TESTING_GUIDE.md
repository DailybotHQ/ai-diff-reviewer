# Testing Guide

The testing strategy for AI PR Reviewer is deliberately pragmatic. The runtime is a single stdlib script whose meaningful surface is integration with two categories of external systems (LLM providers and the GitHub API) — neither of which can be mocked *end-to-end* without recreating the API contracts ourselves. So the bar has three tiers:

1. **Static check.** Does the script parse and compile?
2. **Unit tests.** Do the pure-logic paths (parsers, dispatch, subprocess boundary, roundtrip serialization) behave correctly on a vanilla runner with nothing installed?
3. **Dogfood.** Does the action successfully review its own PRs — for **every** shipping provider?

That's the entire test suite. The bar is deliberate: enough to catch every regression that `py_compile` alone would miss, cheap enough to run in seconds on a stdlib-only setup.

## What CI runs

The [`.github/workflows/code_check.yml`](../.github/workflows/code_check.yml) workflow runs on every PR and every push to `main`:

| Job | What it does | Why |
|---|---|---|
| `compile-check` | `python3 -m py_compile scripts/reviewer.py` | Catches syntax errors and undefined imports before we ship. |
| `validate-action-yml` | Runs `python3 .github/scripts/validate_action.py`, which asserts the required top-level keys, that every input the runtime reads is declared, and that every declared output matches a runtime writer. | Catches accidental key renames or forgotten `write_action_output()` calls in PRs. |
| `unit-tests` | `python3 -m unittest discover -s tests` — the full 109-test stdlib suite. | Catches regressions in pure logic without any network dependency. |
| `cli-install-smoke` (matrix: `claude-code`, `cursor`, `codex`) | Runs each agent-runner CLI's install command on a fresh runner, verifies `--version`, then imports `scripts/reviewer.py` and asserts `build_provider(PROVIDER_ID)` returns an `AgentRunnerProvider` instance. | Catches upstream CLI-installer breakage before it hits consumers. |
| `actionlint` | Downloads the official actionlint binary and runs it across `.github/workflows/`. | Catches malformed workflow YAML, unsafe `${{ }}` interpolations in `run:` blocks, and shellcheck issues in inline shell. |

The [`.github/workflows/self-review.yml`](../.github/workflows/self-review.yml) workflow runs on every PR and **invokes the action under review against itself**, as a `strategy.matrix` job with four legs (`anthropic`, `claude-code`, `cursor`, `codex`), each applying a distinct `self-reviewed:<provider>` label so the four reviews are individually identifiable in the PR conversation. The local checkout (`uses: ./`) is what gets executed, so the version of the action proposed by the PR is what reviews the PR.

If a leg's API-key secret isn't set on the repo, the leg gracefully skips (emits a `::notice::` and short-circuits before checkout) rather than failing red — this keeps fork PRs and secret-less consumer setups from breaking CI.

## What the unit suite covers

The suite lives in `tests/` and is composed of four files:

| File | Focus | ~Tests |
|---|---|---|
| [`tests/test_reviewer.py`](../tests/test_reviewer.py) | Core runtime — input parsing, log redaction, tool-output truncation, path sandboxing, `read_file` / `grep` / `glob` handlers, inline-comment queueing, tracking-comment rendering, `write_action_output()`, severity aggregation, strictness gating, conversation-pruning invariant, and the `state_to_review_result()` / `findings_to_gh_inline_comments()` converters. | 49 |
| [`tests/test_findings_parser.py`](../tests/test_findings_parser.py) | `parse_findings_file()` — happy paths and every documented error mode of the `.aiprr/findings.json` schema. | 21 |
| [`tests/test_agent_runner_providers.py`](../tests/test_agent_runner_providers.py) | The three `AgentRunnerProvider` implementations — `build_provider()` dispatch, MCP-config passthrough, `_invoke_cli_agent` semantics, subprocess-security invariants (no `shell=True`, `shlex.split` on `agent-extra-args`), and the `_CLI_ENV_ALLOWLIST` / `_build_cli_env` gate that stops `AIPRR_GH_TOKEN` and friends from leaking into vendor-CLI subprocesses. | 28 |
| [`tests/test_end_to_end_roundtrip.py`](../tests/test_end_to_end_roundtrip.py) | Cross-family invariants — `ReviewResult` → GitHub-shape serialization, env-var → `build_provider()` integration, provider-independence (both families produce the same payload for the same findings), and constant wiring. | 11 |
| **Total** | | **109** |

Two guiding rules:

1. **No network.** The agentic loop, when covered, is driven by a fake provider. Subprocess-boundary tests stub the vendor CLI. There is nothing to install; the suite runs on `python3` and nothing else.
2. **Pure logic only.** If a test would require mocking the Anthropic API's exact response shape or the GitHub API's exact 422 body, it isn't pulling its weight — write a smoke test on a real PR instead.

## What CI does NOT run

- **`pytest` or any third-party test runner.** Stdlib `unittest` is enough.
- **Type checking with `mypy` in CI.** Type hints are mandatory (see `AGENTS.md`) but not statically enforced. The reasoning: most of the script's `Any` boundaries are JSON dicts from external APIs, where the type-checker can't help much. We rely on type hints as documentation, not as enforcement. Contributors are welcome to run `mypy` locally.
- **Code formatting with `black` / `ruff` in CI.** Formatting consistency matters for readability but the cost of running a formatter in CI for a small single-file script outweighs the benefit. Contributors are encouraged to format before committing.
- **Coverage tooling.** Coverage on a script whose meaningful behaviour lives in I/O calls is misleading.

If you want any of the above as a contributor, **run them locally**. The bar for *adding* them to CI is "show that this catches a class of bug we keep shipping". So far, none has.

## Testing locally

### Compile-check

Always run before pushing:

```bash
python3 -m py_compile scripts/reviewer.py
```

Takes ~1 second. Catches every syntax error and most import typos.

### Run the unit suite

```bash
python3 -m unittest discover -s tests
```

Takes ~2 seconds on a modern laptop. Runs with zero third-party installs — the whole point is that a fresh `git clone` on a runner passes this suite immediately.

For a specific file or class:

```bash
python3 -m unittest tests.test_agent_runner_providers
python3 -m unittest tests.test_findings_parser.ParseFindingsFileHappyPath
```

### Validate `action.yml`

```bash
python3 .github/scripts/validate_action.py
```

The validator asserts that every input the runtime reads is declared in `action.yml`, and every declared output matches a `write_action_output()` writer. Requires `pyyaml` (a dev convenience — install with `pip install pyyaml`, it is not a runtime dependency).

### Run the reviewer against a real PR

The script is designed to be invocable outside the action wrapper for local debugging. Set the provider you want to exercise:

```bash
cd <your-checkout-of-this-repo>

# Choose one provider family
export AIPRR_PROVIDER=anthropic             # chat-completions family
# export AIPRR_PROVIDER=claude-code         # agent-runner family (requires CLI)
# export AIPRR_PROVIDER=cursor              # agent-runner family (requires CLI)
# export AIPRR_PROVIDER=codex               # agent-runner family (requires CLI)

export AIPRR_API_KEY=$ANTHROPIC_API_KEY     # or the vendor's key for the family you picked
export AIPRR_GH_TOKEN=$GITHUB_TOKEN         # PAT with pull-requests:write
export AIPRR_REPO=DailybotHQ/ai-pr-reviewer
export AIPRR_PR_NUMBER=42                   # an existing open PR
export AIPRR_HEAD_SHA=$(git rev-parse HEAD)
export AIPRR_BASE_REF=main
export AIPRR_ACTION_PATH=$PWD               # must point at the action checkout
export AIPRR_STRICTNESS=lenient
export AIPRR_TRACKING_COMMENT=true
export AIPRR_COLLAPSE_PREVIOUS=true
export AIPRR_MAX_INLINE_COMMENTS=10
export AIPRR_MAX_TURNS=30                   # chat-completions family
# export AIPRR_AGENT_MAX_TURNS=30           # agent-runner family (forwarded to the CLI)
# export AIPRR_MCP_CONFIG_FILE=$PWD/mcp.json # agent-runner family, optional
# export AIPRR_AGENT_EXTRA_ARGS='--verbose' # agent-runner family, optional

python3 scripts/reviewer.py
```

The script will:
1. Talk to GitHub with your token (real comments, real review).
2. Talk to the provider you configured (real spend).
3. Post the review on the PR you specified.

**Use a throwaway PR for debugging**. The action makes real changes to real PRs.

## Smoke testing a code change

Whenever you touch the agentic loop, the prompt, the review-submission path, or a provider implementation:

1. Open a PR in this repo with your change.
2. `self-review.yml` runs the action against itself across the 4-leg matrix.
3. Watch the four tracking comments (one per provider). Each should transition `Working… → done`.
4. Verify the inline comments and the summary look right for **the provider you touched**. If your change also affected shared code (`state_to_review_result`, the submission path, the strictness gate), verify all four legs.
5. If anything is off — comment posted on a wrong line, summary missing a section, severity mis-assigned — fix it on the same PR. Each push re-triggers the 4-leg matrix against the new HEAD.

The PR description should explicitly reference which self-review runs validated the change (per provider, if the change is not provider-agnostic).

## Smoke testing a prompt change

Prompt changes are particularly tricky because the same prompt + same diff + same model produces stochastic output. The recommended process:

1. Write the new prompt in `prompts/default.md` (or your custom prompt file).
2. Open a PR with the change.
3. **Compare reviews on the same PR**: `self-review.yml` will produce four reviews using the new prompt. Compare them with a manual run of the *old* prompt against the same PR for an apples-to-apples view.
4. Run on 3–5 representative PRs (covering different types of changes — feature, bugfix, refactor, docs) to see the prompt's behaviour spread.
5. Paste the before/after reviews into the PR description.

Remember: the agent-runner family layers your prompt on top of the vendor's tuned system prompt (see [PROMPTS.md](PROMPTS.md#how-the-prompt-is-applied-per-provider-family)). Expect more provider-to-provider variance on that path than on `anthropic`.

## Adding tests for a new component

If you're adding a new self-contained component (a new tool, a new severity-evaluation rule, a new provider implementation), unit tests are welcome — the bar is:

- **Pure-function logic only.** Severity ranking, line-range parsing, marker extraction, findings-file parsing, subprocess-argv construction. Not anything that hits a network end-to-end.
- **Stdlib `unittest` only.** No `pytest` dependency.
- Place tests in `tests/test_<area>.py` and run via `python3 -m unittest discover -s tests`.
- Keep each file under ~500 lines. If a file grows beyond that, split it by concern (parser vs dispatch vs security invariants), following the four-file structure that already exists.

If your test would require mocking the entire Anthropic API surface or the entire GitHub API surface, the test isn't pulling its weight — write a smoke test on a real PR instead.

## Releasing

Releases are cut by [`.github/workflows/auto-release.yml`](../.github/workflows/auto-release.yml) on push to `main`. It parses the Conventional-Commits history since the last tag, picks a SemVer bump (`major`/`minor`/`patch`), updates `CHANGELOG.md`, tags, and pushes. Then [`.github/workflows/release.yml`](../.github/workflows/release.yml) moves the major-version alias (`v1`, `v2`) on publish.

Pre-release courtesies for the person landing the merge:

- [ ] `python3 -m py_compile scripts/reviewer.py` passes.
- [ ] `python3 -m unittest discover -s tests` passes.
- [ ] `actionlint` passes on `.github/workflows/`.
- [ ] `self-review.yml` ran successfully on the PR being merged.
- [ ] `CHANGELOG.md` has entries under `[Unreleased]` (auto-release will promote them).
- [ ] `examples/` snippets compile under `actionlint` (the CI job covers this).
- [ ] No `<TODO>` / `<FIXME>` markers in the diff that ships.

To skip the auto-release for a docs-only or infrastructure-only merge, put `[skip release]` in the squash-merge subject.

## When the bar might rise

We already crossed some of the thresholds from earlier versions of this doc (the runtime is past 2000 LOC, and we now have multiple runtime providers). The remaining triggers for tightening the bar further:

1. The runtime file grows past ~4000 LOC. That's the point at which single-file readability starts to lose to modularity.
2. A class of bug ships repeatedly that `py_compile` + the unit suite + dogfooding doesn't catch.
3. We add features that aren't safely dogfoodable (e.g. `block-on-warning` exercising paths that don't fire on this repo's own PRs).

Until any of those hit: keep the bar at compile + 109 unit tests + 4-leg dogfood, and keep the contributor experience friction-free.
