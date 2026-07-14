# Python Development Guidelines

These are the Python-specific guidelines for `scripts/reviewer.py`. For repository-wide standards see [STANDARDS.md](STANDARDS.md). For documentation see [DOCUMENTATION_GUIDE.md](DOCUMENTATION_GUIDE.md). For testing see [TESTING_GUIDE.md](TESTING_GUIDE.md).

## Stdlib only (load-bearing)

`scripts/reviewer.py` MUST run on a vanilla `ubuntu-latest` runner with **zero non-stdlib imports**. This is the single most important rule in the repo. It means:

- No `requests`, no `httpx` — use `urllib.request` / `urllib.error`.
- No `pyyaml` at runtime — `action.yml` is parsed by GitHub itself; the script reads env vars.
- No `pydantic`, no `attrs` — use `dataclasses`.
- No `click`, no `argparse` — env vars are the contract.
- No `tenacity`, no `backoff` — write the retry loop in 8 lines.
- No `loguru`, no `structlog` — `print` to stdout with a tag.

This rule is enforced socially, not by tooling. PRs that introduce a non-stdlib import will be rejected. The simplicity of "no install phase" is the action's competitive moat against more elaborate alternatives.

If you're tempted to add a dependency, ask: can the stdlib do this in 30 lines? If yes, write the 30 lines. If no, open an issue first to discuss whether the feature is worth the supply-chain cost.

## Type hints (mandatory)

Every function signature gets full type annotations: parameters, return type. Variables that aren't trivially inferred from the right-hand side also get annotations.

```python
# Good — fully typed
def fetch_pr_context(
    *, repo: str, pr_number: int, base_ref: str, token: str
) -> PRContext:
    owner, name = repo.split("/", 1)
    pr: dict[str, Any] = gh_request(...)
    files_resp: list[dict[str, Any]] = []
    ...
    return PRContext(...)

# Bad — missing annotations
def fetch_pr_context(*, repo, pr_number, base_ref, token):
    owner, name = repo.split("/", 1)
    pr = gh_request(...)
    files_resp = []
    ...
    return PRContext(...)
```

Use the modern syntax: `dict[str, Any]` not `Dict[str, Any]`, `list[X] | None` not `Optional[List[X]]`. We target Python 3.10+, so `from __future__ import annotations` is at the top of the file and the new syntax works everywhere.

## Functions over classes

The codebase's real classes are:

- `PRContext` — dataclass holding the seed user message data.
- `ReviewState` — dataclass holding mutable state across the agentic loop (chat-completions family).
- `Finding` and `ReviewResult` — dataclasses for the provider-independent inline-comment payload (both families converge on `ReviewResult` before submission).
- `Provider` (abstract) and its concrete impl (`AnthropicProvider`) — chat-completions family.
- `AgentRunnerProvider` (abstract) and its concrete impls (`ClaudeCodeProvider`, `CursorProvider`, `CodexProvider`) — agent-runner family.

Everything else is a free function. Don't add a class to "organise" related functions — Python modules are already a namespace. `tool_read_file`, `tool_grep`, etc. are functions; they don't need a `Tools` class wrapping them. Same for the CLI-invocation helpers (`_invoke_cli_agent`, `_build_cli_env`, `_swap_mcp_config`) — they're shared free functions consumed by all three `AgentRunnerProvider` impls.

## Module structure

`scripts/reviewer.py` is organised in this order, separated by `# --- ... ---` banner comments:

1. Module docstring (with usage and env-var documentation).
2. Imports.
3. Constants (URLs, timeouts, caps, severity ranks, `_CLI_ENV_ALLOWLIST`).
4. Logging utilities.
5. GitHub API helpers.
6. `Finding` and `ReviewResult` dataclasses (the provider-independent payload).
7. `Provider` abstraction + `AnthropicProvider` (chat-completions family).
8. `AgentRunnerProvider` abstraction + CLI helpers (`_build_cli_env`, `_invoke_cli_agent`, `_swap_mcp_config`) + `ClaudeCodeProvider` / `CursorProvider` / `CodexProvider`.
9. PR context dataclass + `fetch_pr_context`.
10. Tool definitions (`tools_schema`) — chat-completions family only.
11. Tool implementations (`tool_read_file`, `tool_grep`, `tool_glob`, `tool_post_inline_comment`, `tool_submit_review`).
12. Findings parsing (`parse_findings_file`, `write_findings_prompt_directive`) — agent-runner family only.
13. Severity / strictness logic.
14. Agentic loop (`drive_review`, `state_to_review_result`).
15. Review submission (`findings_to_gh_inline_comments`, `gh_submit_review_with_fallback`).
16. Tracking-comment renderers.
17. `main()` (dispatches to `drive_review` or `AgentRunnerProvider.run_review()` based on the provider family).
18. `if __name__ == "__main__": sys.exit(main())`.

When adding new code, place it in the section it logically belongs to. If you find yourself wanting a new section, that might be a sign the logic warrants a separate file — but err toward keeping the single-file structure.

## Naming

- `snake_case` for functions, variables, modules.
- `PascalCase` for classes and type aliases.
- `SCREAMING_SNAKE_CASE` for module-level constants.
- Tool functions: `tool_<name>` (e.g. `tool_read_file`).
- GitHub API helpers: `gh_<verb>_<noun>` (e.g. `gh_post_issue_comment`, `gh_apply_label`).

## Magic numbers

Don't inline integers or strings that have meaning. Promote them to module-level constants at the top of the file. Examples already in the codebase:

```python
MAX_TOOL_OUTPUT_BYTES: int = 32_000
MAX_FILE_READ_LINES: int = 2_000
MAX_INLINE_COMMENTS: int = 10
DEFAULT_MAX_TURNS: int = 30
API_RETRY_DELAYS_S: tuple[int, ...] = (2, 5, 15)
SEVERITY_CRITICAL: str = "critical"
```

When you write `if foo > 32_000` inline, that's a bug waiting for someone to forget the constant.

## Error handling

Three patterns are explicitly OK:

### 1. Best-effort GitHub call, log on failure

For helpers that update the spinner or apply a label, where failure is acceptable:

```python
try:
    gh_request(...)
except Exception as e:  # noqa: BLE001
    log(f"Failed to update X: {e}")
```

The `# noqa: BLE001` is mandatory and a brief inline comment should explain *why* a broad except is appropriate (here: "best-effort; never blocks the review").

### 2. Surface to the model rather than crash

For tool implementations:

```python
def execute_tool(name: str, args: dict[str, Any], state: ReviewState) -> str:
    try:
        ...
    except Exception as e:  # noqa: BLE001 — surface to model rather than crash
        return f"Tool `{name}` raised {type(e).__name__}: {e}"
```

The model gets the error in the next `tool_result`, can adjust, and try a different tool call. This keeps a transient bug from killing the entire review.

### 3. Wrap the loop so failures hit the spinner

In `main()`:

```python
try:
    drive_review(...)
except Exception as e:  # noqa: BLE001
    log(f"Agentic loop crashed: {type(e).__name__}: {e}")
    gh_update_issue_comment(... body=render_tracking_body_failed(...))
    return 1
```

Same pattern: broad except, with `# noqa: BLE001` and a comment.

**What's NOT OK:** broad except without a noqa, or a noqa without a comment. The combination communicates "intentional and load-bearing" — both halves are important.

## Logging

```python
log("Reviewing repo#42 @ abc1234 with anthropic/claude-sonnet-4-6")
```

The `log()` helper writes to stdout with a `[ai-pr-reviewer]` prefix. Don't use `print()` directly; don't import `logging` and configure handlers. The single helper is enough; the workflow log is the consumer.

**Never log a secret.** The `redact_for_log()` helper is for tool-arg logging and is a defence-in-depth measure against prompt injection. The rule for everything else: don't print env-var contents that match the substring filter (`token`, `key`, `secret`, `password`, `auth`).

## Subprocess

Use the `run_cmd()` helper for internal commands (e.g. `git diff`). It uses `subprocess.run` with explicit argv, `capture_output=True`, `text=True`, `errors="replace"`. Never pass `shell=True`. Never construct shell strings via formatting.

For invoking a vendor CLI on the agent-runner path, use `_invoke_cli_agent()` — the shared helper enforces three invariants that a hand-rolled `subprocess.run` would be easy to get wrong:

1. **`shell=False` always.** No exceptions. If you find yourself wanting shell features (globbing, redirection, pipes), invoke the shell as an explicit argv (`["bash", "-c", ...]`) with a hard-coded command — never string-interpolate user input.
2. **`shlex.split()` on any user-provided arg string** (`agent-extra-args`). Never `.split()` on whitespace; that breaks quoted values and lets attackers hide shell metacharacters.
3. **Scrubbed environment via `_build_cli_env()`.** The vendor CLI subprocess only sees variables in `_CLI_ENV_ALLOWLIST` — no `AIPRR_GH_TOKEN`, no arbitrary `AIPRR_*`, no accidental leakage of secrets to a third-party binary. If a new CLI needs a new env var, add it to the allowlist explicitly with a comment justifying why it's safe.

## Path handling

User-supplied (model-supplied) paths route through `safe_repo_path()`. This catches `..` traversal, absolute paths, and symlinks-out-of-repo via `Path.resolve() + Path.relative_to()`.

## URL construction

Use the `gh_request()` and `gh_graphql()` helpers. They handle headers, auth, JSON encoding/decoding. Don't write a new urllib request.

If you need a new HTTP endpoint (e.g. for a future provider), add a thin helper next to the existing ones, following the same pattern.

## Imports

Standard order:

```python
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
```

1. `from __future__ import annotations` — must be first, enables modern type-hint syntax.
2. Stdlib `import` statements alphabetised.
3. Stdlib `from ... import` statements alphabetised.

No third-party imports (see "Stdlib only" above).

## Dataclasses

Prefer `@dataclass` with type annotations to manual `__init__` / `__repr__`. Use `field(default_factory=...)` for mutable defaults (lists, dicts).

```python
@dataclass
class ReviewState:
    inline_comments: list[dict[str, Any]] = field(default_factory=list)
    severities: list[str] = field(default_factory=list)
    final_summary: str | None = None
    max_inline_comments: int = DEFAULT_MAX_INLINE_COMMENTS
```

## Constants over enums

Stdlib `enum.Enum` is fine but adds JSON-serialisation friction (model gets `<Severity.CRITICAL>` in tool results). For string-valued constants where we serialise to JSON, plain string constants + a `tuple` of valid values is simpler:

```python
SEVERITY_CRITICAL: str = "critical"
SEVERITY_RANK: dict[str, int] = {...}
```

Use `enum.Enum` only when type-safe state machines are clearly load-bearing.

## Comments

- Explain *why*, not *what*.
- One sentence per comment unless context is genuinely needed.
- Multi-line section banners (`# --- ... ---`) are reserved for module structure.
- Don't comment what type-hints already convey.
- Reference upstream bugs by URL when documenting a workaround:
  ```python
  # Workaround for anthropics/claude-code-action#5: cpSync fails on
  # symlinks because the destination's parent doesn't exist.
  ```

## Test discipline

There's a stdlib `unittest` suite in `tests/` (242 tests as of v1.6, across four files) plus dogfooding via `self-review.yml`. See [TESTING_GUIDE.md](TESTING_GUIDE.md) for the full breakdown. For pure-function logic that warrants a new test:

- Place at `tests/test_<area>.py` — **not** at `scripts/test_<area>.py`. The tests live in a sibling directory to keep `scripts/reviewer.py` importable as a plain module (`sys.path.insert(0, 'scripts'); import reviewer as r` — see the pattern in the existing test files).
- Use stdlib `unittest`. No `pytest`.
- Run via `python3 -m unittest discover -s tests`.
- Keep each file focused on one concern; the existing four-file split (`test_reviewer.py` for core, `test_findings_parser.py` for the findings-file parser, `test_agent_runner_providers.py` for CLI providers + subprocess-security invariants, `test_end_to_end_roundtrip.py` for cross-family serialization) is the model. Add a new file rather than growing an existing one past ~500 lines.
- Don't mock external APIs end-to-end. If your code is "mostly mocking out the network", it's not testing the right thing — write a smoke test on a real PR instead. Mocking a subprocess boundary (e.g. simulating a fake `.aiprr/findings.json` file that a vendor CLI would have written) is fine and encouraged.

## When in doubt

Read `scripts/reviewer.py`. It's ~4000 LOC and follows every rule above. The patterns that exist are the patterns; new code should look like the surrounding code.
