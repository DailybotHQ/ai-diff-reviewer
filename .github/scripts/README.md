# `.github/scripts/`

**CI-only helper scripts.** These run inside GitHub Actions to enforce contracts before merge; they are **not** part of the runtime that ships to consumers.

## Contents

| Script | Purpose |
|---|---|
| [`validate_action.py`](validate_action.py) | Validates the `action.yml` public contract: YAML parses, required top-level keys exist, `runs.using == 'composite'`, required inputs are declared, every declared input is wired into the composite step's `env:` block, every output has a `value:` expression. |

## Runtime vs CI tooling — the split

The scripts in this directory MAY use third-party packages (`validate_action.py` imports `pyyaml`). That is deliberately different from `../../scripts/reviewer.py`, which MUST stay stdlib-only:

- **`scripts/reviewer.py`** is what ships inside the action. Every added dependency is a supply-chain question for every consumer. Stdlib-only is the load-bearing rule ([`../../AGENTS.md`](../../AGENTS.md) Rule #2).
- **`.github/scripts/*.py`** run in this repo's own CI only. Consumers never touch them. Third-party packages are fine here — they are installed via `pip install pyyaml` in [`../workflows/code_check.yml`](../workflows/code_check.yml) at the moment the script runs.

## When to add a new CI helper here

- The check is repository-invariant and belongs in CI on every PR.
- The logic is more than a couple of shell lines (once it grows past a `run:` block, promoting it to a Python file with proper types + tests is cheaper long-term than inline bash).
- The helper is genuinely CI-only — no path that ships to consumers depends on it.

When adding a script:

1. Type-hint every function signature.
2. Fail fast — exit non-zero on any violation, print a clear message.
3. Wire it into the relevant workflow under [`../workflows/`](../workflows/) with an explicit `pip install` for any third-party dep.
4. Document its intent in a top-of-file docstring (see `validate_action.py` for the pattern).

## Related

- [`../workflows/code_check.yml`](../workflows/code_check.yml) — the workflow that invokes these helpers on every PR.
- [`../../docs/DEVELOPMENT_COMMANDS.md`](../../docs/DEVELOPMENT_COMMANDS.md) — verbatim commands for running the same checks locally.
