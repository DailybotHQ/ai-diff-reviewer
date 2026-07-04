# `tests/`

**Stdlib `unittest` suite** covering the pure, deterministic surface of `scripts/reviewer.py`.

## Contents

| File | Purpose |
|---|---|
| [`test_reviewer.py`](test_reviewer.py) | The full suite — imports `scripts/reviewer.py` directly via `importlib.util` (no install, no `PYTHONPATH` hackery, no third-party test runner). |

## What is (and isn't) covered

| Covered — pure logic | Not covered — I/O paths |
|---|---|
| `parse_bool` | HTTP calls to Anthropic |
| `redact_for_log` + `LOG_REDACT_SUBSTRINGS` | HTTP calls to the GitHub REST + GraphQL APIs |
| `truncate_for_tool` / output caps | `git diff origin/<base>...HEAD` subprocess |
| Severity ranking + strictness gate | Real review submission |
| Path sandboxing (`safe_repo_path`) | The `POST /pulls/{n}/reviews` 422 fallback (real GitHub response) |
| Tool handlers (`read_file`, `grep`, `glob`, `post_inline_comment`, `submit_review`) | End-to-end agentic loop against a live provider |
| Tracking-comment renderers | |
| `write_action_output` | |
| Provider construction (`build_provider()`) | |
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
