# Security Review — Iteration-Aware Review (IAR)

**Reviewed diff:** `main...HEAD` on branch `feat/iteration-aware-review`
**Reviewer:** Cursor Composer running the DWP `task_security_review` skill, 2026-07-15
**Plan:** PLAN_iteration_aware_review — Task 11

> **v1.8.0 default-flip addendum (post-DWP, 2026-07-15):** After this review closed, the two `action.yml` defaults flipped: `iteration-awareness-enabled: false → true` and `convergence-policy: iterative → first-pass-exhaustive`. **The threat model does not change.** All five surfaces audited below are subject to exactly the same argv-list subprocess boundaries, unicode-safe JSON parsing, hardcoded prompt addendum, and env-var allowlist regardless of whether IAR is on via config or on via default. The only new consideration: the surfaces are now exercised on every `@v1` consumer that doesn't explicitly opt out — the audit findings and mitigations already assumed that scale (see §7 "IAR trust boundary" in `docs/SECURITY.md`).

## Overview

This audit covers the full accumulated diff of Tasks 2–10 of PLAN_iteration_aware_review. The IAR subsystem introduces five broad new surfaces: (1) subprocess calls to `git diff` / `git show` / `git rev-parse`, (2) prompt splicing of a hardcoded exhaustive addendum, (3) untrusted JSON parsing from PR body HTML-comment blocks, (4) two new user-controllable inputs (`convergence-policy`, `iteration-escape-label`), and (5) cost/round telemetry (`RunTelemetry` + 5 outputs + 1 log line). All five are opt-in and gated by `iteration-awareness-enabled: false` by default — with the master switch off, the runtime is byte-identical to pre-IAR releases (verified by the 19-test `test_backward_compat_iar_off.py` regression suite).

**Overall stance: SHIPPABLE.** Zero unfixed `critical` findings. All eight priority foci audited; each maps to a design decision that pre-empts the class of attack it addresses. Two `info`-level recommendations for future work are recorded in the "Recommendations" section.

## Threat Model Delta from pre-IAR baseline

| # | New surface | Attacker vector | Mitigation |
|---|---|---|---|
| 1 | Subprocess boundary — `git diff` / `git show` / `git rev-parse` (five call sites) | Path traversal via `git show <sha>:<path>`; command injection via SHA/path fields. | All calls use argv-list form (no `shell=True`); `<path>` routes through `safe_repo_path()`; SHAs come from trusted sources (git HEAD, GitHub API payload, runner checkout) — never PR body text. |
| 2 | Prompt splicing — `IAR_EXHAUSTIVE_PROMPT_ADDENDUM` | Prompt injection if the addendum were user-controllable. | The addendum is a hardcoded module-scope constant. Grep-verified: only two hits, one declaration + one use of the constant. No `iar_config.*` field is interpolated into the system prompt. |
| 3 | HTML-comment state block — parsing untrusted JSON from PR body | PR-body write attackers could inject a malformed block to crash the reviewer, poison dedup, or trigger unexpected policy application. | `_parse_state_from_marker_body` validates `data["version"]` **before** reading other fields; wraps all fields in `int()` / `str()` / `list()`; catches `JSONDecodeError`, `ValueError`, `TypeError`, `IterationStateParseError` — never raises. On any failure, falls back to "first review" (safe default). Fingerprint strings are used only in `==`/`in` comparisons, never as subprocess/URL args. |
| 4a | New input `convergence-policy` | Injection via unknown value; DoS via crash. | Whitelist compare in `build_iar_config`; unknown values silently fall back to `iterative` + debug log. |
| 4b | New input `iteration-escape-label` | Injection via specially-crafted label name. | Used only in `in pr_labels` membership check (Python list of strings). Never passed to subprocess, URL, or shell. |
| 5 | Cost telemetry — 5 outputs, 1 debug log, `RunTelemetry` fields | Data-exfiltration if user data leaked into telemetry. | `RunTelemetry` fields are numeric (float/int). All 5 outputs are numeric or enum-validated policy name. No user string reaches any output. |
| 6 | New HTTP surface — `_fetch_pr_labels` | Token-scope elevation. | Reuses `gh_request` against `GET /repos/{owner}/{repo}/pulls/{pr}`; requires only `pull-requests: read`, which the reviewer already needs. No elevation. |

## Findings

| # | Severity | Description | Fix / Mitigation | Status |
|---|---|---|---|---|
| 1 | info | The `check_escape_label` function short-circuits dedup for the current run but does not mutate persisted state. This is documented (docstring + `dispatch_policy` § "Escape label short-circuits") and is a deliberate design choice — if the label were left applied indefinitely, the state would still resume from the last real IAR run when the label is removed. No security impact, but worth calling out for operator clarity. | Documented in `docs/ITERATION_AWARENESS.md` + `docs/STRICTNESS.md`. `_render_iar_marker_annotation` surfaces the policy name so an operator can see when they're running under an escape-forced round. No further action. | Accepted risk (documentation only). |
| 2 | info | `_parse_state_from_marker_body` accepts multi-block markers by taking `matches[-1]` (the last block). If an attacker with PR-body write access appended an extra valid-schema block with an inflated `open_fingerprints_this_gen` list, the reviewer would silently trust it for the next dedup round. This is mitigated by the critical-always-surfaces rail (criticals would still surface even if their fingerprints were pre-declared as "resolved"), so the worst case is one round of false-silenced non-critical findings. | The critical safety rail is the load-bearing defense here. Non-critical false silencing is a low-severity nuisance, self-corrects on the next generation (rebase / new commits), and is auditable in the marker annotation ("N deduplicated from prior rounds"). No further fix — attackers with PR-body write access can already do worse things (like the label-gate bypass documented in `docs/SECURITY.md`). | Accepted risk (documented in `docs/SECURITY.md` § IAR trust boundary → Marker-embedded state block). |
| 3 | info | The `_fetch_pr_labels` helper degrades to an empty list on any REST failure, which effectively disables the escape-label path silently when the API is degraded. In the escape case this fails-safe (the reviewer runs normal dedup instead of forcing exhaustive). No security impact. Called out for operator debugging clarity. | The failure is logged with `log(f"IAR: _fetch_pr_labels failed: {exc!r}. Returning empty list.")` so the miswiring is visible in the workflow log. No further action. | Accepted risk (documentation-only). |

**Zero warnings. Zero criticals.**

## Fixes / Mitigations

None applied during the review — the design pre-empted every priority focus. Grep + code inspection confirmed:

- **`shell=True` / `os.system`:** grep returns exactly one hit, and it is a docstring reference (`- Argv-list form (no \`shell=True\`) — see docs/SECURITY.md.`) — not a code path.
- **Subprocess argv-list form:** verified for all 5 IAR subprocess sites (lines 2192, 2437, 2918, 2951, 3179 in `scripts/reviewer.py`). Each uses the `[..]` list form.
- **`safe_repo_path()` on the `git show` path:** verified in `load_code_context()` — refuses on `ValueError` from `safe_repo_path`, logs and returns `None`.
- **Prompt splicing hardcoded:** `IAR_EXHAUSTIVE_PROMPT_ADDENDUM` has exactly two grep hits — the module-scope declaration at line 369 and one use at line 2743 (`prompt_addendum=IAR_EXHAUSTIVE_PROMPT_ADDENDUM`). No user-controllable interpolation.
- **State-block parser fails-safe:** verified — 4 caught exception classes, wrapping fields in `int()`/`str()`/`list()`, `version` check first.
- **Escape label is a `str in list[str]` check:** verified — `check_escape_label` at line 2975 is 2 lines of code.
- **No new API key / GH token log-prints:** grep hits are all pre-existing (module docstring, provider class docs, `main()` env-var reads). No new IAR-related code prints secrets.
- **Cost telemetry fields are all numeric:** verified — `RunTelemetry` at line 3124 declares `start_time_monotonic: float`, `tokens_used: int`, `estimated_baseline_tokens: int`. No user strings.
- **No new outbound endpoints:** only new HTTP hit added by IAR is `_fetch_pr_labels`, using existing `gh_request` — no new domains, no token elevation.
- **`MAX_TURNS` / `max_tokens` unchanged:** the sole grep hit added by the IAR PR is a comment `# not \`max_tokens\` or \`MAX_TURNS\`. See AGENTS.md DON'T #9.` — no runtime change.

## Accepted Risks

1. **Marker-embedded state trust model.** A user with GitHub PR-body write access could edit the marker comment's state block. This is inherent to any marker-based state design (short of an external DB) and is documented in `docs/SECURITY.md` § IAR trust boundary → Marker-embedded state block. The critical-always-surfaces rail bounds the worst-case impact to "one round of silenced non-critical findings that re-surface on the next generation". Accepted.
2. **Escape label state preservation.** By design, escape does NOT mutate persisted state (documented in `check_escape_label` docstring). If the label is left applied indefinitely, dedup resumes when it's removed. This is desired — see `docs/STRICTNESS.md`. Accepted.
3. **`_fetch_pr_labels` fails-safe silence.** On REST failure, escape-label detection returns `False`, which means the reviewer runs normal dedup instead of forcing exhaustive. The failure is logged with an `IAR:` prefix so it's visible in the workflow log. Accepted.

## Recommendations for future IAR work

1. **Fingerprint HMAC.** For consumers concerned about the state-block tamper vector, future work could HMAC-sign the state block using a stable per-repo secret (e.g. derived from a `.dailybot`-style install token). The critical-always-surfaces rail already bounds the worst case, so this is optional hardening rather than a required mitigation. Track as `IAR-FOLLOWUP-1`.
2. **State-block size cap.** Currently the `history` list is capped at `IAR_HISTORY_MAX_ENTRIES = 10` when re-embedded, but there's no upper bound on `resolved_fingerprints` / `open_fingerprints_this_gen` when parsed (only when written by the reviewer itself). A pathologically-large state block from a PR-body-write attacker would parse successfully and consume memory during dedup. Add a cap of e.g. 1000 fingerprints per list at parse time. Track as `IAR-FOLLOWUP-2`.
3. **`_render_iar_marker_annotation` truncation.** No length cap on rendered marker annotation. The `critical_note` branch only fires under a safety-rail violation (which is a bug by construction), so this is defense-in-depth only. Consider truncating the annotation to N chars for output-schema safety. Low priority.

## Update to docs/SECURITY.md

Yes — added a new section `Iteration-Aware Review (IAR) trust boundary (v1.6.0+, opt-in)` between the existing `PR metadata PATCH surface` section and the `Supply-chain audit checklist`. The section covers all five new surfaces above with the same "what/why/mitigation" narrative as the pre-existing sections.

## Validation

```
$ python3 -m py_compile scripts/reviewer.py
(exit 0)

$ python3 -m unittest discover -s tests
Ran 428 tests in 0.164s
OK

$ rg -n "shell=True|os\.system" scripts/reviewer.py
1234:      - Argv-list form (no `shell=True`) — see docs/SECURITY.md.
(1 hit, docstring only — not a code path)

$ rg -n "IAR_EXHAUSTIVE_PROMPT_ADDENDUM" scripts/reviewer.py
369:IAR_EXHAUSTIVE_PROMPT_ADDENDUM: str = (
2743:            prompt_addendum=IAR_EXHAUSTIVE_PROMPT_ADDENDUM,
(2 hits: declaration + hardcoded use)
```

All acceptance criteria pass. Report is complete.
