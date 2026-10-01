# Deferred review findings

## PR #70 — vendored DWP skill upgrade (v6.0.2)

- `.agents/skills/deepworkplan/addons/ai-diff-reviewer/SKILL.md:307` vs `SPEC.md:57,387` — the `address-review` introduction version differs (v3.1.1 vs v3.2.1). Vendored upstream text (`DailybotHQ/deepworkplan-skill@v6.0.2`); fix upstream, not in this repo's copy.
- `.agents/skills/deepworkplan/verify/conformance.sh` does not route v6 plan folders to `contract_v6.py` (reported on `AGENTS.md`/`conformance.sh`). Vendored upstream script; the v6 validate commands are documented in `AGENTS.md` Quick Commands in the meantime. Contribute the fix upstream.
