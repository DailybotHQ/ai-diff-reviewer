# Testing Guide

The testing strategy for AI Diff Reviewer is deliberately pragmatic. The runtime is a single stdlib script whose meaningful surface is integration with two categories of external systems (LLM providers and the GitHub API) — neither of which can be mocked *end-to-end* without recreating the API contracts ourselves. So the bar has three tiers:

1. **Static check.** Does the script parse and compile?
2. **Unit tests.** Do the pure-logic paths (parsers, dispatch, subprocess boundary, roundtrip serialization) behave correctly on a vanilla runner with nothing installed?
3. **Dogfood.** Does the action successfully review its own PRs, with the direct Anthropic leg always on and the CLI-provider legs enabled when provider-sensitive surfaces change?

That's the entire test suite. The bar is deliberate: enough to catch every regression that `py_compile` alone would miss, cheap enough to run in seconds on a stdlib-only setup.

## What CI runs

The [`.github/workflows/code_check.yml`](../.github/workflows/code_check.yml) workflow runs on every PR and every push to `main`:

| Job | What it does | Why |
|---|---|---|
| `compile-check` | `python3 -m py_compile scripts/reviewer.py` | Catches syntax errors and undefined imports before we ship. |
| `validate-action-yml` | Runs `python3 .github/scripts/validate_action.py`, which asserts the required top-level keys, that every input the runtime reads is declared, and that every declared output matches a runtime writer. | Catches accidental key renames or forgotten `write_action_output()` calls in PRs. |
| `unit-tests` | `python3 -m unittest discover -s tests` — the full unit-test stdlib suite (27 files, listed below), including the offline corpus-validation suite (`test_eval_corpus*`). | Catches regressions in pure logic without any network dependency. |
| `cli-install-smoke` (matrix: `claude-code`, `cursor`, `codex`, `grok`) | Runs each agent-runner CLI's install command on a fresh runner (Cursor and Grok through `.github/scripts/verified_install.sh`, plus a dry-run proving the sha256 gate accepts the right hash and refuses a wrong one), verifies `--version`, then imports `scripts/reviewer.py` and asserts `build_provider(PROVIDER_ID)` returns an `AgentRunnerProvider` instance. | Catches upstream CLI-installer breakage before it hits consumers. |
| `actionlint` | Downloads the official actionlint binary and runs it across `.github/workflows/`. | Catches malformed workflow YAML, unsafe `${{ }}` interpolations in `run:` blocks, and shellcheck issues in inline shell. |

The [`.github/workflows/self-review.yml`](../.github/workflows/self-review.yml) workflow runs on every PR and **invokes the action under review against itself**. The `anthropic` leg runs on every PR/push as the baseline reviewer with a tighter self-review turn cap. The matrix is built from **secret presence only**: `anthropic`, `claude-code`, `cursor`, `codex` (the four original legs, unchanged), plus — when their secrets/variables exist — `grok` (`XAI_API_KEY`), `claude-code` on Z.ai GLM (`ZAI_CODING_API_KEY`, `api-base: https://api.z.ai/api/anthropic`), `codex` on Azure Foundry (`AZURE_OPENAI_API_KEY` + `AZURE_OPENAI_BASE_URL` / `AZURE_OPENAI_MODEL_DAILY` variables) and the in-process `openai` leg (default-on whenever `OPENAI_API_KEY` exists; opt out with the repo variable `SELF_REVIEW_OPENAI_CHAT=false`). Smoke legs use the `economy` tier alias so the dated defaults matrix stays the single source of truth. A leg without its secret is absent from the matrix (never a misleading green). Each active leg applies a distinct `self-reviewed:<provider>` label so reviews are identifiable in the PR conversation. The local checkout (`uses: ./`) is what gets executed, so the version of the action proposed by the PR is what reviews the PR.

If a leg's API-key secret isn't set on the repo, the leg gracefully skips (emits a `::notice::` and short-circuits before checkout) rather than failing red — this keeps fork PRs and secret-less consumer setups from breaking CI.

## What the unit suite covers

The suite lives in `tests/` and is composed of 30 files (858 tests; regenerate the counts with `for f in tests/test_*.py; do printf '%s %s\n' "$f" "$(grep -c 'def test_' "$f")"; done`):

| File | Focus | Tests |
|---|---|---|
| [`tests/test_agent_runner_backends.py`](../tests/test_agent_runner_backends.py) | Custom backends for CLI runners — Claude Code env contract on Z.ai/xAI, Codex `config.toml` + cloned model catalog, Codex custom-tool warning. | 21 |
| [`tests/test_agent_runner_cli_invocations.py`](../tests/test_agent_runner_cli_invocations.py) | Claude Code and Codex invocations — argv/env shapes, subscription auth, Codex `auth.json` and isolated `CODEX_HOME`. | 22 |
| [`tests/test_agent_runner_cursor.py`](../tests/test_agent_runner_cursor.py) | Cursor Agent CLI headless defaults — flags, model handling, extra args. | 6 |
| [`tests/test_agent_runner_grok_and_snapshots.py`](../tests/test_agent_runner_grok_and_snapshots.py) | Grok CLI invocation (prompt file, rules, hardening flags, `--max-turns`, env) and the **default-profile back-compat snapshot table captured from `main`**. | 9 |
| [`tests/test_agent_runner_hardening.py`](../tests/test_agent_runner_hardening.py) | Agent-runner security — `_CLI_ENV_ALLOWLIST` / `_build_cli_env`, subprocess invariants (no `shell=True`, `shlex.split`), hardening regressions (credential lanes, bounded findings file, glob caps, ReDoS timing), Cursor `api-base` warning, prompt v3 directive and prompt hygiene. | 18 |
| [`tests/test_agent_runner_providers.py`](../tests/test_agent_runner_providers.py) | Agent-runner core — `build_provider()` dispatch, provider construction, MCP passthrough, `_invoke_cli_agent` semantics, CLI binary constants. | 23 |
| [`tests/test_backend_matrix.py`](../tests/test_backend_matrix.py) | The **runner × backend matrix** (six runners × thirteen backends), endpoint path joining, backend-selection logging. | 9 |
| [`tests/test_backend_requests.py`](../tests/test_backend_requests.py) | Anthropic runner on compatible gateways — request headers per auth style, cache-control flags, the diff cache breakpoint. | 13 |
| [`tests/test_backends.py`](../tests/test_backends.py) | `api-base` contract — `validate_api_base` (https, ASCII hosts, IPv6, host tricks), `classify_endpoint_host`, `resolve_endpoint_profile`, `build_provider(api_base=…)`. | 29 |
| [`tests/test_bedrock_provider.py`](../tests/test_bedrock_provider.py) | `provider: anthropic` on AWS Bedrock — InvokeModel request shape (in-body `anthropic_version`, SigV4 headers, model-in-URL path), credential-resolution errors, non-Bedrock regression, OIDC startup lane (with/without AWS env credentials), runner gate. | 8 |
| [`tests/test_bedrock_sigv4.py`](../tests/test_bedrock_sigv4.py) | SigV4 signing (AWS documentation vector, cross-verified against botocore) + AWS credential resolution (env/OIDC first, packed `api-key` fallback, partial-env hard error). | 9 |
| [`tests/test_eval_harness.py`](../tests/test_eval_harness.py) | Comparison-harness smoke — manifest build/validate, fabricated-split rejection, repetition assignment, oversized run-file rejection. | 4 |
| [`tests/test_eval_corpus.py`](../tests/test_eval_corpus.py) | v2 labelled-corpus gates — every `tests/eval/cases/` fixture validates (identity, content SHA-256 pins, floors, blinded critical adjudication, secret markers) purely offline. | 13 |
| [`tests/test_iar_advisory_escape.py`](../tests/test_iar_advisory_escape.py) | The `iteration-escape-label` advisory path — escape semantics, state preservation, reset behaviour. | 35 |
| [`tests/test_iar_gate_consistency.py`](../tests/test_iar_gate_consistency.py) | Strictness-gate consistency — the check decision computed once matches the review footer, tracking comment and conclusion. | 18 |
| [`tests/test_end_to_end_roundtrip.py`](../tests/test_end_to_end_roundtrip.py) | Cross-family invariants — `ReviewResult` → GitHub-shape serialization, env-var → `build_provider()` integration for all CLI runners, in-process providers ignoring agent env vars, usage never leaking into the GitHub payload, constant wiring. | 13 |
| [`tests/test_findings_parser.py`](../tests/test_findings_parser.py) | `parse_findings_file()` — happy paths and every documented error mode of the `.aiprr/findings.json` schema, including the summary-only fallback. | 33 |
| [`tests/test_iar_dedup.py`](../tests/test_iar_dedup.py) | IAR fingerprinting and dedup of findings against prior rounds. | 32 |
| [`tests/test_iar_dispatch.py`](../tests/test_iar_dispatch.py) | IAR trigger dispatch — event/label/policy routing into review modes. | 29 |
| [`tests/test_iar_failure_fallback.py`](../tests/test_iar_failure_fallback.py) | IAR failure-fallback contract — the runtime still produces a review (and all IAR outputs) when the subsystem crashes mid-flight. | 20 |
| [`tests/test_iar_generation_tracking.py`](../tests/test_iar_generation_tracking.py) | IAR generation and range-hash tracking across force-pushes and rebases. | 30 |
| [`tests/test_iar_incremental.py`](../tests/test_iar_incremental.py) | Incremental review mode (rounds 2+) — prior findings from review threads, delta computation, mode selection, budget scaling, reconciliation, marker SHA coercion. | 36 |
| [`tests/test_iar_observability.py`](../tests/test_iar_observability.py) | IAR outputs, tracking-comment rendering, base/head SHA round trips, budget accounting. | 57 |
| [`tests/test_iar_policies.py`](../tests/test_iar_policies.py) | IAR policies (iterative / exhaustive) and their budget rules. | 16 |
| [`tests/test_iar_state_layer.py`](../tests/test_iar_state_layer.py) | Iteration-Aware Review state — marker embed/parse round trips, per-field shape validation of the persisted JSON. | 39 |
| [`tests/test_model_tiers.py`](../tests/test_model_tiers.py) | `model` tier aliases (`balanced` / `economy` / `deep`), the dated defaults matrix, legacy-default hints, `agent-max-turns` parsing and native caps. | 17 |
| [`tests/test_openai_provider.py`](../tests/test_openai_provider.py) | `provider: openai` — Anthropic-shape ↔ chat-completions translation both ways, headers per auth style (Bearer / Azure `api-key`), retry client, error surfacing. | 20 |
| [`tests/test_review_safety_regressions.py`](../tests/test_review_safety_regressions.py) | Local-review regressions for multi-backend and incremental safety — delta context, prior-critical gating, advisory resolution, thread pagination, usage accounting, routing safety, control characters, redirect refusal. | 14 |
| [`tests/test_reviewer.py`](../tests/test_reviewer.py) | Core runtime — input parsing, log redaction, tool-output truncation, path sandboxing, tool handlers, inline-comment queueing, tracking-comment rendering, `write_action_output()`, severity aggregation, strictness gating, conversation pruning, diff shaping (`ignore-paths`, omitted-files block) and the cache-prefix stability of the first user message. | 192 |
| [`tests/test_telemetry.py`](../tests/test_telemetry.py) | Usage telemetry — `normalise_usage`, the three CLI stdout parsers (bounded tail), cost estimation, `format_usage_line` variants, tracking-comment usage line, real `iteration-tokens-used`. | 22 |
| **Total** | | **858** |

Run one file with `python3 -m unittest tests.test_backends` (module form, from the repo root). Three cross-cutting nets are worth knowing about when you touch providers: the **runner × backend matrix** (`tests/test_backend_matrix.py::RunnerBackendMatrixTests`) locks the endpoint kind and constructability of every `provider` × `api-base` combination across all thirteen registered backends; the **default-profile snapshot table** (`tests/test_agent_runner_grok_and_snapshots.py::DefaultProfileBackCompatSnapshotTests`) compares each CLI runner's argv/env against literals captured from `main` before the multi-backend work, with intentional deltas listed explicitly; and the **hardening regressions** (`tests/test_agent_runner_hardening.py::HardeningRegressionTests`) keep the security fixes from regressing (credential lanes, bounded findings file, glob caps, ReDoS timing). Every module added or grown by v2.1.0 stays under 500 lines (split by concern); the pre-v2.1.0 modules over that size (`test_reviewer.py`, the `test_iar_*` family) are recorded debt in `docs/STANDARDS.md § File size`.

Two guiding rules:

1. **No network.** The agentic loop, when covered, is driven by a fake provider. Subprocess-boundary tests stub the vendor CLI. There is nothing to install; the suite runs on `python3` and nothing else.
2. **Pure logic only.** If a test would require mocking the Anthropic API's exact response shape or the GitHub API's exact 422 body, it isn't pulling its weight — write a smoke test on a real PR instead.

## Review-quality evaluation (offline, labelled corpus)

Counting findings is not a quality metric — a prompt that doubles false positives "finds more". `tests/eval/run_eval.py` (stdlib; deliberately outside `unittest discover`) runs the action's own loop against a **merged** PR without posting, and scores the result against `tests/eval/corpus.json`: must-find recall, false positives against known-wrong findings, unlabelled findings, severity match, contract compliance (summary present), suggestion-block rate, coverage, tokens and cost. Works for in-process runners (`drive_review`) and agent-runner CLIs installed locally (`run_review` in a worktree at the PR head). See [`tests/eval/README.md`](../tests/eval/README.md) for usage and how labels are authored (from fix commits, never from a model's output). Measurements for v2.1.0 live in the plan record `analysis_results/REVIEW_QUALITY_EVAL.md`; the harness is what a prompt or tier change must be run through before it ships.

### Comparison-harness gates (PLAN_jev_review_acceleration)

| Gate | Command | Notes |
| --- | --- | --- |
| CORPUS | `python3 -m json.tool tests/eval/corpus.json` + `python3 -m unittest discover -s tests -p 'test_eval_corpus*.py' -v` | v2 corpus validator + floors; fails on unpinned fixtures, unadjudicated critical labels, secret markers |
| HARNESS | `python3 tests/eval/corpus_validate.py --json` + `python3 tests/eval/jev_experiment.py dry-run --manifest <manifest>` | the validator CLI and the zero-call dry-run planner; strictly offline |
| BENCH | `python3 tests/eval/jev_experiment.py validate --manifest <manifest>` / `report --manifest <manifest>` | manifest/split/budget validation; report fails promotion on missing runs, failed runs, or unknown cost |


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
export AIPRR_REPO=DailybotHQ/ai-diff-reviewer
export AIPRR_PR_NUMBER=42                   # an existing open PR
export AIPRR_HEAD_SHA=$(git rev-parse HEAD)
export AIPRR_BASE_REF=main
export AIPRR_ACTION_PATH=$PWD               # must point at the action checkout
export AIPRR_STRICTNESS=lenient
export AIPRR_TRACKING_COMMENT=true
export AIPRR_COLLAPSE_PREVIOUS=true
export AIPRR_MAX_INLINE_COMMENTS=10
export AIPRR_MAX_TURNS=30                   # chat-completions family
# export AIPRR_AGENT_MAX_TURNS=30           # agent-runner family (warns; no universal CLI cap)
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
2. `self-review.yml` runs the action against itself when the PR carries the `ready` label. Every leg whose provider secret is configured on the repo reviews the PR (today: `grok`); a leg without its secret is absent from the matrix.
3. Watch the active tracking comments. Each should transition `Working… → done`.
4. Verify the inline comments and the summary look right for **the provider you touched**. If your change also affected shared code (`state_to_review_result`, the submission path, the strictness gate), toggle `ready` again after your fix and verify every active provider leg.
5. If anything is off — comment posted on a wrong line, summary missing a section, severity mis-assigned — fix it on the same PR. Each push re-triggers self-review against the new HEAD.

The PR description should explicitly reference which self-review runs validated the change (per provider, if the change is not provider-agnostic).

## Smoke testing a prompt change

Prompt changes are particularly tricky because the same prompt + same diff + same model produces stochastic output. The recommended process:

1. Write the new prompt in `prompts/default.md` (or your custom prompt file).
2. Open a PR with the change.
3. **Compare reviews on the same PR**: prompt changes trip the critical-file scope gate, so `self-review.yml` will produce provider reviews using the new prompt. Compare them with a manual run of the *old* prompt against the same PR for an apples-to-apples view.
4. Run on 3–5 representative PRs (covering different types of changes — feature, bugfix, refactor, docs) to see the prompt's behaviour spread.
5. Paste the before/after reviews into the PR description.

Remember: the agent-runner family layers your prompt on top of the vendor's tuned system prompt (see [PROMPTS.md](PROMPTS.md#how-the-prompt-is-applied-per-provider-family)). Expect more provider-to-provider variance on that path than on `anthropic`.

## Adding tests for a new component

If you're adding a new self-contained component (a new tool, a new severity-evaluation rule, a new provider implementation), unit tests are welcome — the bar is:

- **Pure-function logic only.** Severity ranking, line-range parsing, marker extraction, findings-file parsing, subprocess-argv construction. Not anything that hits a network end-to-end.
- **Stdlib `unittest` only.** No `pytest` dependency.
- Place tests in `tests/test_<area>.py` and run via `python3 -m unittest discover -s tests`.
- Keep each file under ~500 lines. If a file grows beyond that, split it by concern (parser vs dispatch vs security invariants), following the concern-scoped structure in the table above.

If your test would require mocking the entire Anthropic API surface or the entire GitHub API surface, the test isn't pulling its weight — write a smoke test on a real PR instead.

## Failure-fallback regression suites for cross-cutting subsystems (repo convention)

When you add a **cross-cutting subsystem** whose failure mode must NOT crash the runtime (e.g. Iteration-Aware Review, which runs on every review but is wrapped in `try/except` at each `main()` call site), pair it with a dedicated `tests/test_<feature>_failure_fallback.py` file that asserts the runtime **still produces a review** when the subsystem crashes mid-flight. This convention exists because:

- The stdlib-only, single-file runtime cannot afford a cross-cutting subsystem to silently take down every review.
- A dedicated file lets a reviewer see, at a glance, exactly which invariants the subsystem's safety contract protects (parser leniency, output-writer completeness on every exit path, no new subprocess when the subsystem faults).
- The file becomes the failing test that any future refactor of the subsystem must first update — a deliberate friction point.

Existing example: [`tests/test_iar_failure_fallback.py`](../tests/test_iar_failure_fallback.py) locks the IAR safety contract — parser can't crash on garbage env vars, `write_iar_outputs_empty()` always writes exactly 5 empty outputs, and `write_all_outputs()` on every exit path (skip, success, block) always includes the 5 IAR outputs so downstream steps never read an undefined value. Copy that structure when adding a new cross-cutting subsystem.

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

We already crossed some of the thresholds from earlier versions of this doc: the runtime sits around **~10k LOC as of v2.1.0**, we ship six runtime providers across two families (plus thirteen bring-your-own-endpoint backends), we ship a companion local skill with its own sub-skills, and the unit suite has grown to 858 tests across 30 files. The remaining triggers for tightening the bar further:

1. The runtime file is already past the historical ~4500 LOC soft ceiling (see `docs/STANDARDS.md § "File size"`); the "split into modules" decision is open and should be made deliberately — the next feature that adds significant surface (a Gemini provider, a v2 findings schema) should not land as more lines in the single file.
2. A class of bug ships repeatedly that `py_compile` + the unit suite + dogfooding doesn't catch.
3. We add features that aren't safely dogfoodable (e.g. `block-on-warning` exercising paths that don't fire on this repo's own PRs).

Until any of those hit: keep the bar at compile + unit tests + scoped dogfood, and keep the contributor experience friction-free.
