# Testing Guide

The testing strategy for AI PR Reviewer is deliberately minimal. The runtime is a single stdlib script whose meaningful surface is integration with two external APIs (Anthropic and GitHub) — neither of which can be mocked honestly without recreating the API contracts ourselves. So the test bar is:

1. **Static check.** Does the script parse and compile?
2. **Action-shape check.** Does `action.yml` declare the keys the runtime reads?
3. **Dogfood.** Does the action successfully review its own PRs?

That's the entire test suite. If you find yourself wanting more, read this doc — there are good reasons we haven't added more, and good reasons you might want to.

## What CI runs

The `.github/workflows/ci.yml` workflow runs on every PR:

| Job | What it does | Why |
|---|---|---|
| `compile-check` | `python3 -m py_compile scripts/reviewer.py` | Catches syntax errors and undefined imports before we ship. |
| `compile-check` (cont.) | YAML-parses `action.yml` and asserts top-level keys exist (`inputs`, `outputs`, `runs`) plus the `api-key` and `github-token` inputs. | Catches accidental key renames in PRs. |
| `actionlint` | Downloads the official actionlint binary and runs it across `.github/workflows/`. | Catches malformed workflow YAML, unsafe `${{ }}` interpolations in `run:` blocks, and shellcheck issues in inline shell. |

The `.github/workflows/self-review.yml` workflow runs on every PR and **invokes the action under review against itself**. The local checkout (`uses: ./`) is what gets executed, so the version of the action proposed by the PR is what reviews the PR.

## What CI does NOT run

We don't have:

- **Unit tests with `pytest`.** The runtime is mostly thin wrappers over external APIs. Mocked unit tests would test our mocks, not our code. `py_compile` + dogfooding is the bar.
- **Type checking with `mypy` in CI.** Type hints are mandatory (see `AGENTS.md`) but not statically enforced. The reasoning: most of the script's `Any` boundaries are JSON dicts from external APIs, where the type-checker can't help much. We rely on type hints as documentation, not as enforcement.
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

### Validate `action.yml`

```bash
python3 -c "import yaml; yaml.safe_load(open('action.yml'))"
```

(`pyyaml` is a dev convenience, not a runtime dep — install it locally if you want.)

### Run the script directly against a real PR

The script is designed to be invocable outside the action wrapper for local debugging:

```bash
cd <your-checkout-of-this-repo>

export AIPRR_PROVIDER=anthropic
export AIPRR_API_KEY=$ANTHROPIC_API_KEY
export AIPRR_GH_TOKEN=$GITHUB_TOKEN          # PAT with pull-requests:write
export AIPRR_REPO=DailybotHQ/ai-pr-reviewer
export AIPRR_PR_NUMBER=42                    # an existing open PR
export AIPRR_HEAD_SHA=$(git rev-parse HEAD)
export AIPRR_BASE_REF=main
export AIPRR_ACTION_PATH=$PWD                # must point at the action checkout
export AIPRR_STRICTNESS=lenient
export AIPRR_TRACKING_COMMENT=true
export AIPRR_COLLAPSE_PREVIOUS=true
export AIPRR_MAX_INLINE_COMMENTS=10
export AIPRR_MAX_TURNS=30

python3 scripts/reviewer.py
```

The script will:
1. Talk to GitHub with your token (real comments, real review).
2. Talk to Anthropic with your API key (real spend).
3. Post the review on the PR you specified.

**Use a throwaway PR for debugging**. The action makes real changes to real PRs.

## Smoke testing a code change

Whenever you touch the agentic loop, the prompt, or the review-submission path:

1. Open a PR in this repo with your change.
2. The `self-review.yml` workflow runs the action against itself.
3. Watch the tracking comment. It should transition `Working… → done`.
4. Verify the inline comments and the summary look right.
5. If anything is off — comment posted on a wrong line, summary missing a section, severity mis-assigned — fix it on the same PR. Each push re-triggers the review against the new HEAD.

The PR description should explicitly reference which self-review run validated the change.

## Smoke testing a prompt change

Prompt changes are particularly tricky because the same prompt + same diff + same model produces stochastic output. The recommended process:

1. Write the new prompt in `prompts/default.md` (or your custom prompt file).
2. Open a PR with the change.
3. **Compare reviews on the same PR**: the `self-review.yml` will produce a review using the new prompt. Compare it with a manual run of the *old* prompt against the same PR for an apples-to-apples view.
4. Run on 3–5 representative PRs (covering different types of changes — feature, bugfix, refactor, docs) to see the prompt's behaviour spread.
5. Paste the before/after reviews into the PR description.

## Adding tests for a new component

If you're adding a new self-contained component (a new tool, a new severity-evaluation rule, a new translation function for an upcoming provider), unit tests are welcome — but the bar is:

- **Pure-function logic only.** Severity ranking, line-range parsing, marker extraction. Not anything that hits a network.
- **Stdlib `unittest` only.** No `pytest` dependency.
- Place tests in `scripts/test_<module>.py` and run via `python3 -m unittest discover scripts`.

If your test would require mocking the Anthropic or GitHub APIs, the test isn't pulling its weight — write a smoke test on a real PR instead.

## Releasing

Before tagging a release:

- [ ] `python3 -m py_compile scripts/reviewer.py` passes.
- [ ] `actionlint` passes on `.github/workflows/`.
- [ ] `self-review.yml` ran successfully on the most recent PR merged to `main`.
- [ ] `CHANGELOG.md` has an entry under `[Unreleased]`; promote it to `[X.Y.Z]` with the release date.
- [ ] `examples/` snippets compile under `actionlint` (the CI job covers this).
- [ ] No `<TODO>` / `<FIXME>` markers in the diff that ships.

Then tag, push, and let `release.yml` move the moving major tag.

## When the bar might rise

We will add more rigorous testing if any of the following becomes true:

1. The script grows past ~3000 LOC (currently ~1500).
2. We add a second runtime provider — at that point the boundary translation is non-trivial and unit-testable.
3. A class of bug ships repeatedly that `py_compile` + dogfooding doesn't catch.
4. We add features that aren't safely dogfoodable (e.g. `block-on-warning` exercising paths that don't fire on this repo's own PRs).

Until then: keep it simple, keep the bar at compile + dogfood, and keep the contributor experience friction-free.
