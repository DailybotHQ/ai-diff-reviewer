# Deferred review findings

## PR #70 — vendored DWP skill upgrade (v6.0.2)

- `.agents/skills/deepworkplan/addons/ai-diff-reviewer/SKILL.md:307` vs `SPEC.md:57,387` — the `address-review` introduction version differs (v3.1.1 vs v3.2.1). Vendored upstream text (`DailybotHQ/deepworkplan-skill@v6.0.2`); fix upstream, not in this repo's copy.
- **Resolved upstream in v7.0.0** (`fix(verify): judge v6/v7 plans by their own records`) — `.agents/skills/deepworkplan/verify/conformance.sh` did not route v6 plan folders to `contract_v6.py` (reported on `AGENTS.md`/`conformance.sh`). Vendored upstream script; the v6 validate commands are documented in `AGENTS.md` Quick Commands in the meantime. Contribute the fix upstream.

## Vendored DWP skill upgrade (v7.0.0)

- `.agents/skills/deepworkplan/verify/plan_contract.py` — the unresolved-critical heuristic matches the phrase `open critical finding` anywhere, so a clean review line such as "Open critical findings: **none**" is reported as an unresolved critical. Vendored upstream script; reword to "no unresolved critical finding" in the meantime and contribute the fix upstream.
