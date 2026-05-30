# Development Commands

A short reference of everything you might run while working on this repo. Most of it is one-liners; the script is small.

## Compile-check

```bash
python3 -m py_compile scripts/reviewer.py
```

Fastest sanity check. Run before every push. Takes ~1 second.

## Validate `action.yml`

```bash
python3 -c "import yaml; yaml.safe_load(open('action.yml'))"
```

CI does this; locally it's a smoke check after editing the action file.

## Run actionlint locally

```bash
bash <(curl -fsSL https://raw.githubusercontent.com/rhysd/actionlint/main/scripts/download-actionlint.bash)
./actionlint -color
```

The download script is the official one; it places `actionlint` in the current directory. CI installs it the same way.

## Run the reviewer against a real PR

```bash
export AIPRR_PROVIDER=anthropic
export AIPRR_API_KEY=$ANTHROPIC_API_KEY
export AIPRR_GH_TOKEN=$GITHUB_TOKEN
export AIPRR_REPO=DailybotHQ/ai-pr-reviewer
export AIPRR_PR_NUMBER=<n>
export AIPRR_HEAD_SHA=$(git rev-parse HEAD)
export AIPRR_BASE_REF=main
export AIPRR_ACTION_PATH=$PWD

python3 scripts/reviewer.py
```

Will post a real review on the configured PR. Use a throwaway PR for iteration.

Optional knobs:

```bash
export AIPRR_STRICTNESS=block-on-critical
export AIPRR_LABEL_GATE=ready
export AIPRR_APPLIED_LABEL=pr-reviewed
export AIPRR_PROMPT_FILE=$PWD/prompts/default.md
export AIPRR_MAX_INLINE_COMMENTS=10
export AIPRR_MAX_TURNS=30
```

## Cut a release

1. Update `CHANGELOG.md` — promote `[Unreleased]` to `[X.Y.Z]` with today's date, bump the comparison links at the bottom.
2. Commit on `main` with `chore(release): vX.Y.Z`.
3. Tag and push:
   ```bash
   git tag vX.Y.Z
   git push origin main vX.Y.Z
   ```
4. Create a GitHub Release from the tag — `release.yml` then auto-updates the moving major tag (`vX`).

## Refresh the symlinks

If a clone or filesystem mishandled them:

```bash
rm -f .claude && ln -s .agents .claude
rm -f CLAUDE.md && ln -s AGENTS.md CLAUDE.md
```

CI does not validate this; if you commit a regular file at `.claude` or `CLAUDE.md` by accident, please re-create the symlinks before pushing.

## Search

```bash
# All TODO / FIXME markers
git grep -nE 'TODO|FIXME|XXX'

# Every place the AIPRR_ env-var prefix is used
git grep -n 'AIPRR_'

# Every reference to the marker constant
git grep -n 'ai-pr-reviewer-marker'
```

## Dependency posture

Confirm we still ship zero non-stdlib runtime dependencies:

```bash
git grep -n '^import\|^from' scripts/reviewer.py | sort -u
```

Expected output: only stdlib modules. Anything else is a bug.

## Docs check

After editing `action.yml` inputs/outputs, sanity-check that the README table still matches:

```bash
# Pull every input name from action.yml
python3 -c "import yaml; print('\n'.join(yaml.safe_load(open('action.yml'))['inputs'].keys()))"

# Compare with the README table
grep -E '^\| `' README.md | head -20
```

A diff between the two is a documentation regression.

## Local Python version

Targets `python3.10+`. Most contributors will have `python3` from the system; the script also runs on `python3.11` and `python3.12`. We don't take a dep on a non-default version.
