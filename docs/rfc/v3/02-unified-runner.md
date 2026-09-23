# RFC-02 — Unified runner: tool parity, change inventory and lane decisions

## Status

Draft — discovery record (PLAN_v3_discovery, Task 4, 2026-09-23). Addresses
RFC-00 **P-2**, **P-10**, part of **P-5**. Depends on RFC-01 (the run record
every lane emits). Consumed by RFC-03 (verifier tools), RFC-04 (leg
outputs), RFC-06 (budgets), RFC-07 (lane changes are contract changes).

## Problem

Two provider families ship today (`docs/ARCHITECTURE.md` § 8). The
**chat-completions family** (`Provider`: `anthropic`, `openai`) runs the
action's own loop (`drive_review`) with five base tools; the **agent-runner
family** (`AgentRunnerProvider`: `claude-code`, `cursor`, `codex`, `grok`)
hands the whole loop to a vendor CLI with native git, LSP and MCP, and gets
findings back through `.aiprr/findings.json`. On the same coding model the
CLI lanes reached 3–4/5 labelled defects and the in-process lanes 0–1/5
(RFC-00 E-09, E-10, E-11, E-12); the in-process anthropic lane burned its
full 30-turn budget on two PRs with zero findings (E-36). The difference is
not weights — it is what the loop can see and do: no patch retrieval, a
diff truncated at 200 000 characters and re-billed every turn, no
change-inventory completeness signal, no instruction-file reading, and a
control loop that cannot tell "I am done" from "I ran out of turns".

## Evidence

| RFC-00 row | What it establishes for this RFC |
|---|---|
| E-09, E-10, E-11 vs E-12 | In-process 0–1/5 vs CLI 3–4/5 on the same model family |
| E-36 | In-process runs hitting `DEFAULT_MAX_TURNS` with zero findings at $1.85–$2.04 |
| E-25 | The in-process tool surface: 5 base tools + 3 conditional; nothing retrieves a patch |
| E-26 | `MAX_DIFF_CHARS = 200_000`, truncated diff re-billed per turn, pruning in pairs |
| E-31, P-10 | `fetch_pr_context` keeps `path/status/additions/deletions/omitted` per file; drops `previous_filename`; `omitted_files` is a log line, not a typed completeness flag |
| E-13, H-05 | The PR #37 class (documentation contradiction) is invisible without reading instruction files |
| R-05 | CLI legs hit the 900 s job timeout with no partial result |
| S6 §5 | "the API loop currently has file/search tools and no general git-diff tool … Reading current source alone cannot reconstruct deleted code" |
| S6 §8 | PR title/body/labels are untrusted; lockfiles, binaries, executable bits and policy/instruction files should escalate |
| H-02 | Hypothesis this RFC exists to test: tool parity explains most of the gap |

## Current state

Read from `scripts/reviewer.py` at `42194a4`; every symbol is listed under
*Cited symbols* and checked by the plan gate.

### In-process family (`Provider` → `drive_review`)

| Tool | Purpose | Bounds | Path safety | Notes |
|---|---|---|---|---|
| `read_file` | full-file context | `MAX_FILE_READ_LINES` per call, `offset`/`limit` paging | `safe_repo_path` | output through `truncate_for_tool` |
| `grep` | POSIX ERE search | 200 lines | `safe_repo_path` on the scope | `grep -rIn -E --` argv form |
| `glob` | list files | 200 paths | honors `.gitignore` | — |
| `post_inline_comment` | queue a finding | `max_inline_comments` (`DEFAULT_MAX_INLINE_COMMENTS` = 10) | — | severity enum `critical/warning/info`; line must be in the diff |
| `submit_review` | terminate with a summary | once | — | the only explicit "done" signal |
| `set_pr_description` | conditional (`pr-description-mode: autocomplete`) | once | — | untrusted output (`docs/SECURITY.md`) |
| `set_pr_complexity` | conditional (complexity labels) | once, **post-hoc** | — | the model's opinion after the spend (RFC-06) |
| `update_prior_finding` | conditional (IAR incremental) | once per fingerprint | — | verdict = model's say-so (RFC-03) |

Loop facts: `drive_review` runs up to `DEFAULT_MAX_TURNS` (30); prunes the
conversation in assistant/tool_result pairs to `MAX_CONVERSATION_TURNS_RETAINED`;
the first user message (`render_user_prompt`) embeds the shaped diff
(`shape_diff` removes ignore-glob sections into `PRContext.omitted_files`,
then `MAX_DIFF_CHARS` truncates); tool args are logged through
`redact_for_log`; reaching the cap logs "Reached MAX_TURNS … without an
explicit submit_review" and the review proceeds with whatever was queued.

### Agent-runner family (`AgentRunnerProvider` → `_invoke_cli_agent`)

The CLI receives the layered prompt (stdin for large prompts), runs its own
loop in `workspace` with native git/LSP/MCP, and must write
`FINDINGS_JSON_REL` (`.aiprr/findings.json`); `parse_findings_file`
validates it (object root; `findings` list; `path`/`line`/`body` required;
severity default `info`; `MAX_FINDINGS_FILE_BYTES` cap). `_run_cli_process`
enforces argv-list form and `CLI_INVOCATION_TIMEOUT`; one retry when the CLI
exits 0 without a findings file (`CLI_INCOMPLETE_RETRIES`); env is scrubbed
by `_build_cli_env` (allowlist + the vendor credential). Turn caps are
advisory except where native (`AGENT_MAX_TURNS_NATIVE_PROVIDERS` = grok,
`AIPRR_AGENT_MAX_TURNS`). `mcp-config-file` is wired for Claude Code
(`--mcp-config`) and warned for Codex and Grok.

### PR context (`fetch_pr_context` → `PRContext`)

Per file: `path`, `status`, `additions`, `deletions`, `omitted`. Dropped:
`previous_filename` (renames), `sha`, `patch`, binary flag, mode change.
`diff` is one string from `git diff origin/<base>...HEAD --unified=3`.
`omitted_files` is a list of `(path, line_count)`; nothing in the prompt or
the outputs states "this review saw the whole change" as a typed value.

### What each lane can see today

| Capability | `anthropic` | `openai` | `claude-code` | `cursor` | `codex` | `grok` |
|---|---|---|---|---|---|---|
| Full files / search | yes (3 tools) | yes | native | native | native | native |
| Patch of a specific changed file | **no** (only the embedded, possibly truncated diff) | **no** | native `git` | native | native | native |
| Deleted code / before-after | **no** | **no** | native `git show` | native | native | native |
| Change inventory with completeness | no (log only) | no | no (prompt lists files) | no | no | no |
| Instruction files (AGENTS.md, `.review/extension.md`) | only if the prompt says so and the model chooses to `read_file` | same | vendor CLIs read `CLAUDE.md`/`AGENTS.md` by their own convention | partial | partial | no |
| Explicit "done" vs "budget exhausted" | `submit_review` vs cap | same | exit 0 without findings file → retry | same | same | same |
| Partial result on timeout | none | none | none (R-05) | none | none | none |
| Tool trace for evidence (RFC-03) | in logs only | in logs only | none (CLI-internal) | none | none | none |

## Design

### One control loop, one plan of record

Every lane executes the same **review plan**, whether the loop runs
in-process or inside a CLI:

1. **Inventory** — `get_change_inventory` (below) produces the SHA-bound list
   of changes with a `complete: bool`.
2. **Classify** — RFC-06 assigns the risk tier from the inventory (never from
   PR metadata) and sets the budget.
3. **Read the rules** — `read_instruction_files` returns the repository's
   agent instructions and the review extension (the PR #37 class becomes a
   first-class input).
4. **Review** — the model explores with the parity tool set and emits
   findings **with evidence** (RFC-03 shape).
5. **Verify** — RFC-03's verifier runs on `critical` (and sampled `warning`)
   findings with the same tools.
6. **Emit** — RFC-05 structured output including the RFC-01 run record and
   the inventory's completeness flag; publishing is the aggregator's job
   (RFC-04) or the single-leg default.

### Parity tool set

Every tool below exists in-process; the CLI lanes obtain the same capability
through the mechanism in the *how* column. Arguments are validated, paths go
through `safe_repo_path`, outputs through `truncate_for_tool`, arguments
through `redact_for_log`.

| Tool | Arguments | Bounds | Returns | How CLI lanes get it |
|---|---|---|---|---|
| `get_change_inventory` | none | one call per review (cached) | `{head_sha, base_sha, files:[{path, previous_path, status, additions, deletions, binary, mode_change, omitted, patch_chars}], omitted_count, complete}` — `complete` is false when any file is omitted, oversized, binary-unknown, or the base ref did not resolve | rendered verbatim into the CLI prompt **and** written to `.aiprr/inventory.json` in the workspace; the CLI is instructed to read it before exploring |
| `get_patch` | `path`, optional `hunk_index` or `line_range` | `MAX_PATCH_CHARS` per call (new constant, sized so a call never re-bills the whole diff) | the unified diff for that file (or hunk) from `git diff <base>...<head> -- <path>` | native `git diff`; the prompt names the exact base/head SHAs so the CLI diffs the same range |
| `read_file` / `grep` / `glob` | unchanged | unchanged | unchanged | native |
| `read_instruction_files` | none | bounded total bytes | the contents of `AGENTS.md` / `CLAUDE.md` (dedup by symlink), `.review/extension.md`, and the repo's declared docs index when present, each with its SHA-256 | the same list is prepended to the CLI prompt as a **required reading** block; the run record's `context.instruction_files_read` is filled from the prompt, not from the CLI's behavior |
| `emit_finding` | RFC-03 finding v3 (path, line, severity, body, `evidence`) | `max_inline_comments` | ack + finding id | `.aiprr/findings.json` extended to finding v3 (RFC-05 § relation) |
| `submit_review` | `summary` | once | — | writing the findings file **is** the submit; a missing file after exit 0 is `status: incomplete` |

The embedded diff shrinks: the first message carries the **inventory** and
the patches of files up to a byte budget (RFC-06), not one 200 000-character
blob; anything beyond is fetched on demand through `get_patch`. This removes
the per-turn re-billing of E-26 without withholding anything — the model
always knows what it has not seen (`complete: false` + the omitted list).

### Control-loop contract

- **Budget is an input, not a hope.** `max_turns`, token ceiling and the
  verifier allowance come from RFC-06 by tier; the loop records
  `budget.turns_used` and stops with `status: incomplete` (never a silent
  approve) when the cap is hit without `submit_review`.
- **Partial result on timeout.** Findings emitted before a CLI timeout are
  read from the findings file as they stand (`status: timeout`); the gate
  never turns green on a partial review (`incomplete_review_gate` semantics
  extended to timeouts).
- **Done is explicit.** `submit_review` or a findings file. "No tool calls"
  from the model ends the loop as `incomplete` unless a summary exists.
- **Tool trace.** Every tool call (name, redacted args, result hash) is kept
  in `ReviewState` and referenced by finding evidence (RFC-03); for CLI lanes
  the trace is whatever the CLI exposes — the run record marks
  `tool_trace: unavailable` rather than inventing one.

### Untrusted inputs

PR title, body, labels and comments are **data**. They never change the
budget, the tier, or the instruction set; `read_instruction_files` reads
repository files only, at the head SHA, through `safe_repo_path`. The
inventory is computed from git and the GitHub files API, never from the
description.

## Gap matrix

`today / target / how`. Kinds that constrain a lane are noted (Bedrock is
anthropic-runner-only; the six chat-completions kinds are Codex-blocked via
`_assert_codex_backend_supported` / `CODEX_UNUSABLE_CHAT_COMPLETIONS_KINDS`).

| Capability | `anthropic` | `openai` | `claude-code` | `cursor` | `codex` | `grok` |
|---|---|---|---|---|---|---|
| Change inventory + completeness | none / **yes** / `get_change_inventory` tool | same | none / **yes** / `.aiprr/inventory.json` + prompt block | same | same | same |
| Patch retrieval | none / **yes** / `get_patch` (git, bounded) | same | native / native / prompt names base..head | native | native | native |
| Before/after (deleted code) | none / **yes** / `get_patch` shows `-` lines; `read_file` at base via `git show <base>:<path>` behind `safe_repo_path` | same | native | native | native | native |
| Instruction files | prompt hint / **required** / `read_instruction_files` result injected | same | convention / **required** / required-reading block | convention (partial) / required / same | none / required / same | none / required / same |
| Explicit done / incomplete | cap-or-submit / **status enum** / loop contract | same | file-or-retry / **status enum** / timeout reads partial file | same | same | same |
| Finding evidence (RFC-03) | none / **yes** / `emit_finding` | same | none / **yes** / findings file v3 | same | same | same |
| Verifier pass (RFC-03) | none / **yes** / in-loop, same tools | same | none / **yes** / runtime-side verifier after the CLI returns (in-process tools on the workspace) | same | same | same |
| Tool trace | logs / **structured** / `ReviewState` trace | same | none / **unavailable, declared** / run record flag | same | same | same |
| Budget by tier (RFC-06) | constant 30 / **tiered** / RFC-06 matrix | same | advisory / **tiered where native** / `AIPRR_AGENT_MAX_TURNS`, `--max-turns` on grok | advisory | advisory | native |
| Run record (RFC-01) | none / **yes** / emitted by `main` | same | same | same | same | same |
| Bedrock (`bedrock` kind) | SigV4 lane, keep | n/a | n/a | n/a | n/a | n/a |
| Codex-blocked kinds | n/a | ok | n/a | n/a | blocked (6 kinds) | n/a |

No cell is left as "to be decided": every target has a mechanism.

## Lane decisions

| `provider` | Decision | Evidence | Cost of the option | Consequence |
|---|---|---|---|---|
| `anthropic` (in-process) | **Rebuild on the unified loop** — keep the runner id, replace the loop's inputs and tools | E-09, E-36 show the *loop* failing, not the model; H-02 says parity should close the gap; the SigV4 Bedrock lane and the Anthropic-compatible kinds (Z.ai, Moonshot, MiniMax) only exist through this runner | one implementation (the parity tools are runtime code shared with the verifier, so the work is not lane-specific) | if the RFC-01 measurement after Phase 1 still shows the in-process band far below CLI on the corpus, **retire** the lane for code review and keep it as the verifier/economy engine (RFC-08 decision D-03). `provider: anthropic` keeps resolving; behavior changes → RFC-07 |
| `openai` (in-process) | **Rebuild** (same loop; it shares `drive_review`) | E-10; the six OpenAI-compatible vendor kinds and Gemini/OpenRouter only exist through this runner | zero extra beyond the anthropic rebuild (`openai_response_to_anthropic` boundary unchanged) | same fallback as above |
| `claude-code` | **Keep**, add inventory/required-reading block, findings v3, runtime-side verifier, timeout-partial | E-08 (3/5 on GLM at flat rate), R-05 | prompt + findings-file contract changes | dogfood leg on ZAI is one of the two legs available today |
| `cursor` | **Keep**, same additions | no campaign evidence (key not configured); shares the agent-runner contract | same | measured only when a secret exists |
| `codex` | **Keep**, same additions; keep the kind block | v2.4.0 hard-block on chat-completions kinds stands | same | Azure lane unmeasured today |
| `grok` | **Keep** as the reference lane (native `--max-turns`), same additions | E-01, E-12: strongest measured lane and the production default | same | dogfood leg on XAI |

Recommended default remains the strongest **measured** lane (`grok`) until
RFC-01 shows another lane in the same band; the in-process rebuild is judged
by measurement, not by intent.

## Alternatives considered

| Alternative | Why not |
|---|---|
| CLI-only product (drop the in-process family) | Loses Bedrock (SigV4 is in-process only), the six OpenAI-compatible vendor kinds, Gemini, OpenRouter and the zero-install default; retiring is the *fallback* after measurement, not the plan |
| MCP-only tool delivery (ship the parity tools as an MCP server the CLIs mount) | `mcp-config-file` is wired only for Claude Code and warned on Codex/Grok; a second transport to maintain; the inventory/instruction blocks in the prompt cover the CLI side with no new surface |
| Per-lane bespoke tools | Exactly the drift this RFC removes; one tool set, one trace shape |
| Keep embedding the whole diff, raise `MAX_DIFF_CHARS` | Raises per-turn cost on every lane (E-26) and still cannot express completeness |
| Let the model decide the budget (today's `set_pr_complexity`) | Post-hoc; RFC-06 decides before the spend |

## Impact on the public contract

- **Additive:** `.aiprr/inventory.json` (workspace file), finding v3 in the
  findings file (superset), `context.*` fields in the run record, new
  outputs via RFC-05. New constant `MAX_PATCH_CHARS` (internal).
- **Behavioral:** the in-process lanes' request shape changes (inventory +
  bounded patches instead of one embedded diff; new tools) — this ends the
  "byte-identical to earlier releases" promise for `provider: anthropic` /
  `openai` (RFC-07 `BC-03`); `status: incomplete` on cap/timeout instead of
  proceeding with a partial silently (`BC-04`); required instruction-file
  reading (`BC-05`).
- **Possible breaking (pending D-03):** retiring in-process lanes for code
  review if measurement says so (`BC-06`).

## Open questions

| Id | Question | Recommendation |
|---|---|---|
| Q-06 | Base-side file reads (`git show <base>:<path>`): a new tool or a `ref` argument on `read_file`? | `ref` argument limited to `{base, head}`; one tool, no new surface |
| Q-07 | `MAX_PATCH_CHARS` value | 40 000 (≈ one large file); the inventory's `patch_chars` lets the model page hunks |
| Q-08 | Should the CLI lanes receive the inventory only in the prompt or also as a file? | Both: prompt for attention, file for exactness (CLIs can re-read it) |
| Q-09 | Where does the CLI-side verifier run — inside the CLI session or runtime-side after it returns? | Runtime-side with in-process tools on the same workspace: one verifier implementation, same evidence shape (RFC-03 Q-11) |
| Q-10 | Retire-or-keep threshold for the in-process lanes | Retire for code review if, after Phase 1, in-process recall on the pinned corpus is below the CLI band by more than the RFC-01 promotable-recall threshold (3 defects) across two lanes; decision D-03 |

## Acceptance for the implementation plan

The Phase 1 runner DWP is complete when:

1. `get_change_inventory`, `get_patch`, `read_instruction_files` and
   `emit_finding` exist in-process with unit tests for bounds, path safety
   (`safe_repo_path`) and redaction; the CLI prompt carries the inventory and
   required-reading blocks and `.aiprr/inventory.json` is written.
2. The same run record (RFC-01) with `context.changed_files`,
   `omitted_files`, `diff_truncated = false` on the 7 historical PRs and
   `instruction_files_read` non-empty is emitted by **every** lane on corpus
   case C037 (the documentation-contradiction PR).
3. The in-process first message no longer embeds more than the RFC-06 byte
   budget of patches; `MAX_DIFF_CHARS` is retired or reduced to that budget.
4. A capped or timed-out review ends with `status: incomplete|timeout`, a
   red gate under `block-on-critical`, and the partial findings posted with
   an explicit note — covered by tests for both families.
5. In-process recall on the pinned corpus (RFC-01 replicated cells) is
   reported beside the CLI lanes; D-03 is decided from that table.
6. `docs/ARCHITECTURE.md` § 8, `docs/PROVIDERS.md` (findings-file contract)
   and `docs/SECURITY.md` (new tools, base-ref reads) are current.

## Cited symbols

- `Provider`
- `AgentRunnerProvider`
- `drive_review`
- `render_user_prompt`
- `shape_diff`
- `fetch_pr_context`
- `PRContext`
- `omitted_files`
- `MAX_DIFF_CHARS`
- `DEFAULT_MAX_TURNS`
- `DEFAULT_MAX_INLINE_COMMENTS`
- `MAX_CONVERSATION_TURNS_RETAINED`
- `MAX_FILE_READ_LINES`
- `safe_repo_path`
- `truncate_for_tool`
- `redact_for_log`
- `ReviewState`
- `execute_tool`
- `read_file`
- `grep`
- `glob`
- `post_inline_comment`
- `submit_review`
- `set_pr_description`
- `set_pr_complexity`
- `update_prior_finding`
- `FINDINGS_JSON_REL`
- `parse_findings_file`
- `MAX_FINDINGS_FILE_BYTES`
- `_invoke_cli_agent`
- `_run_cli_process`
- `CLI_INVOCATION_TIMEOUT`
- `CLI_INCOMPLETE_RETRIES`
- `_build_cli_env`
- `AGENT_MAX_TURNS_NATIVE_PROVIDERS`
- `AIPRR_AGENT_MAX_TURNS`
- `incomplete_review_gate`
- `openai_response_to_anthropic`
- `_assert_codex_backend_supported`
- `CODEX_UNUSABLE_CHAT_COMPLETIONS_KINDS`
- `mcp-config-file`
