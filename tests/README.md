# `tests/`

**Stdlib `unittest` suite** covering the pure, deterministic surface of `scripts/reviewer.py`.

## Contents

| File | Purpose (tests) |
|---|---|
| [`test_agent_runner_backends.py`](test_agent_runner_backends.py) | Custom backends for CLI runners (21) |
| [`test_agent_runner_cli_invocations.py`](test_agent_runner_cli_invocations.py) | Claude Code and Codex invocations (22) |
| [`test_agent_runner_cursor.py`](test_agent_runner_cursor.py) | Cursor Agent CLI headless defaults (6) |
| [`test_agent_runner_grok_and_snapshots.py`](test_agent_runner_grok_and_snapshots.py) | Grok CLI invocation (prompt file, rules, hardening flags, `--max-turns`, env) and the **default-profile back-compat snapshot table captured from `main`** (9) |
| [`test_agent_runner_hardening.py`](test_agent_runner_hardening.py) | Agent-runner security (18) |
| [`test_agent_runner_providers.py`](test_agent_runner_providers.py) | Agent-runner core (23) |
| [`test_backend_matrix.py`](test_backend_matrix.py) | The **runner × backend matrix** (six runners × five backends), endpoint path joining, backend-selection logging (9) |
| [`test_backend_requests.py`](test_backend_requests.py) | Anthropic runner on compatible gateways (13) |
| [`test_backends.py`](test_backends.py) | `api-base` contract (29) |
| [`test_end_to_end_roundtrip.py`](test_end_to_end_roundtrip.py) | Cross-family invariants (13) |
| [`test_findings_parser.py`](test_findings_parser.py) | `parse_findings_file()` (33) |
| [`test_iar_dedup.py`](test_iar_dedup.py) | IAR fingerprinting and dedup of findings against prior rounds (32) |
| [`test_iar_dispatch.py`](test_iar_dispatch.py) | IAR trigger dispatch (29) |
| [`test_iar_failure_fallback.py`](test_iar_failure_fallback.py) | IAR failure-fallback contract (20) |
| [`test_iar_generation_tracking.py`](test_iar_generation_tracking.py) | IAR generation and range-hash tracking across force-pushes and rebases (30) |
| [`test_iar_incremental.py`](test_iar_incremental.py) | Incremental review mode (rounds 2+) (36) |
| [`test_iar_observability.py`](test_iar_observability.py) | IAR outputs, tracking-comment rendering, base/head SHA round trips, budget accounting (57) |
| [`test_iar_policies.py`](test_iar_policies.py) | IAR policies (iterative / exhaustive) and their budget rules (16) |
| [`test_iar_state_layer.py`](test_iar_state_layer.py) | Iteration-Aware Review state (39) |
| [`test_model_tiers.py`](test_model_tiers.py) | `model` tier aliases (`balanced` / `economy` / `deep`), the dated defaults matrix, legacy-default hints, `agent-max-turns` parsing and native caps (17) |
| [`test_openai_provider.py`](test_openai_provider.py) | `provider: openai` (20) |
| [`test_review_safety_regressions.py`](test_review_safety_regressions.py) | Local-review regressions for multi-backend and incremental safety (14) |
| [`test_reviewer.py`](test_reviewer.py) | Core runtime (192) |
| [`test_telemetry.py`](test_telemetry.py) | Usage telemetry (22) |

Every file imports `scripts/reviewer.py` directly via `importlib.util` (no install, no `PYTHONPATH` hackery, no third-party test runner). Total: **720** tests.

## What is (and isn't) covered

| Covered — pure logic | Not covered — I/O paths |
|---|---|
| `parse_bool` | HTTP calls to Anthropic / OpenAI-compatible backends |
| `redact_for_log` + `LOG_REDACT_SUBSTRINGS` | HTTP calls to the GitHub REST + GraphQL APIs |
| `truncate_for_tool` / output caps | `git diff origin/<base>...HEAD` subprocess |
| Severity ranking + strictness gate | Real review submission |
| Path sandboxing (`safe_repo_path`) | The `POST /pulls/{n}/reviews` 422 fallback (real GitHub response) |
| Tool handlers (`read_file`, `grep`, `glob`, `post_inline_comment`, `submit_review`) | End-to-end agentic loop against a live provider |
| Tracking-comment renderers | |
| `write_action_output` | |
| Provider construction (`build_provider()`), `api-base` profiles, runner × backend matrix | |
| Agent-runner argv/env (stubbed subprocess), default-profile back-compat snapshots | |
| Usage telemetry parsers, cost estimate, tracking-comment usage line | |
| Diff shaping (`ignore-paths`), IAR state/dedup/incremental mode | |
| The conversation-pruning invariant (`MAX_CONVERSATION_TURNS_RETAINED`) | |

The I/O paths on the right are validated by the **dogfooding** workflow — every PR to this repo runs the action against itself via [`../.github/workflows/self-review.yml`](../.github/workflows/self-review.yml). See [`../docs/PR_REVIEW_WORKFLOW.md`](../docs/PR_REVIEW_WORKFLOW.md).

## Running the suite

```bash
# From the repo root:
python3 -m unittest discover -s tests -v
```

- Requires Python 3.10+ (matches the runtime constraint).
- No install, no venv, no `pytest` — the suite honours the same stdlib-only rule as the runtime it tests.
- Runs on every PR via [`../.github/workflows/code_check.yml`](../.github/workflows/code_check.yml).

## Adding tests

1. Group new tests in a `TestCase` subclass with a descriptive class name (e.g. `PathSandboxTests`, `ConversationPruningTests`).
2. Type-hint every method signature — the runtime is fully typed; tests match it.
3. Use `tempfile.TemporaryDirectory` for anything that needs a filesystem; do not touch cwd or the real repo.
4. Do NOT reach for `pytest`, `hypothesis`, or any other third-party runner — the constraint is stdlib-only.
5. If you find yourself needing a network mock, that's the signal to move the test to the dogfooding path instead.

Full testing philosophy: [`../docs/TESTING_GUIDE.md`](../docs/TESTING_GUIDE.md).
