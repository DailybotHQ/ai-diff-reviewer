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

Pick the provider family you want to exercise:

```bash
# Chat-completions family (this action drives the tool-use loop)
export AIPRR_PROVIDER=anthropic
export AIPRR_API_KEY=$ANTHROPIC_API_KEY

# --- or ---

# Agent-runner family (vendor CLI drives the loop; needs the CLI installed locally)
export AIPRR_PROVIDER=claude-code           # or `cursor`, or `codex`
export AIPRR_API_KEY=$ANTHROPIC_API_KEY     # or the vendor's key for the chosen CLI
```

Then set the shared context:

```bash
export AIPRR_GH_TOKEN=$GITHUB_TOKEN
export AIPRR_REPO=DailybotHQ/ai-pr-reviewer
export AIPRR_PR_NUMBER=<n>
export AIPRR_HEAD_SHA=$(git rev-parse HEAD)
export AIPRR_BASE_REF=main
export AIPRR_ACTION_PATH=$PWD

python3 scripts/reviewer.py
```

Will post a real review on the configured PR. Use a throwaway PR for iteration.

Optional knobs shared across families:

```bash
export AIPRR_STRICTNESS=block-on-critical
export AIPRR_LABEL_GATE=ready
export AIPRR_APPLIED_LABEL=pr-reviewed
export AIPRR_PROMPT_FILE=$PWD/prompts/default.md
export AIPRR_MAX_INLINE_COMMENTS=10
```

Chat-completions family only:

```bash
export AIPRR_MAX_TURNS=30                   # the action's own turn cap
```

Agent-runner family only:

```bash
export AIPRR_AGENT_MAX_TURNS=30             # forwarded to the vendor CLI
export AIPRR_AGENT_EXTRA_ARGS='--verbose'   # raw vendor flags (shlex-split)
export AIPRR_MCP_CONFIG_FILE=$PWD/mcp.json  # optional MCP passthrough
```

For local iteration on an agent-runner provider you will need the corresponding CLI on your PATH:

```bash
# Claude Code
npm install -g @anthropic-ai/claude-code

# Cursor Agent
curl -fsSL https://cursor.com/install | bash

# OpenAI Codex
npm install -g @openai/codex
```

## Cut a release

Releases are automated. On merge to `main`, [`.github/workflows/auto-release.yml`](../.github/workflows/auto-release.yml) parses the Conventional-Commits history since the last tag, picks a SemVer bump, updates `CHANGELOG.md`, tags, and pushes. [`.github/workflows/release.yml`](../.github/workflows/release.yml) then moves the major-version alias (`v1`, `v2`) when the GitHub Release is published.

You do not tag manually. What you *do* control:

- **The commit types in the PR being merged.** `feat:` → minor, `fix:` / `perf:` → patch, `feat!:` / `fix!:` / `BREAKING CHANGE` → major, anything else (`docs:`, `chore:`, `refactor:`, `ci:`, `test:`) → patch.
- **The squash-merge subject.** Follow Conventional Commits and the auto-release will pick the right bump.
- **The `[Unreleased]` section of `CHANGELOG.md`.** Populate it in the same PR as the behaviour change; auto-release promotes it to `[X.Y.Z]` on merge.
- **Skipping a release entirely.** Put `[skip release]` in the squash-merge subject (typical for docs-only or infrastructure-only merges).

If you ever need to cut a release manually (e.g. auto-release failed and you can't wait for the fix):

```bash
git tag vX.Y.Z
git push origin main vX.Y.Z
gh release create vX.Y.Z --generate-notes
```

`release.yml` still moves the major alias on `gh release create`.

## Refresh the symlinks

If a clone or filesystem mishandled them:

```bash
rm -f .claude && ln -s .agents .claude
rm -f .cursor && ln -s .agents .cursor
rm -f CLAUDE.md && ln -s AGENTS.md CLAUDE.md
```

The `.cursor → .agents` symlink is a v2.16.0 methodology requirement (alongside `.claude → .agents`) — Cursor reads `hooks.json` from `.cursor/hooks.json`, which resolves via the symlink chain to `.agents/hooks.json` (the canonical location). Claude Code reads `settings.json` from `.claude/settings.json`, which resolves the same way to `.agents/settings.json`. Both agents share the same canonical `.agents/` store; the symlinks exist so each agent can find its own config file at the path it expects.

CI does not validate this; if you commit a regular file at `.claude`, `.cursor`, or `CLAUDE.md` by accident, please re-create the symlinks before pushing.

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

## Run the test suite

The runtime has a standard-library `unittest` suite (109 tests, no install needed):

```bash
python3 -m unittest discover -s tests
```

This is the same suite the `code_check` CI workflow runs on every PR and push
to `main`. Run it before pushing any change to `scripts/reviewer.py`.

To scope to a specific file or class:

```bash
python3 -m unittest tests.test_agent_runner_providers
python3 -m unittest tests.test_findings_parser.ParseFindingsFileHappyPath
```

## Validate the action.yml contract locally

```bash
python3 -m pip install pyyaml   # CI-only tooling, not a runtime dependency
python3 .github/scripts/validate_action.py
```

## Smoke-test the agent-runner CLI installers

The `cli-install-smoke` job in `code_check.yml` runs on every PR. To reproduce
locally on a specific provider (matches what CI does):

```bash
# claude-code
npm install -g @anthropic-ai/claude-code
claude --version

# cursor
curl -fsSL https://cursor.com/install | bash
cursor-agent --version

# codex
npm install -g @openai/codex
codex --version

# Then confirm reviewer.py can build the provider
PROVIDER_ID=claude-code python3 -c "
import sys
sys.path.insert(0, 'scripts')
import reviewer as r
p = r.build_provider('$PROVIDER_ID', api_key='dummy')
assert isinstance(p, r.AgentRunnerProvider), f'got {type(p).__name__}'
print(f'OK: {type(p).__name__}')
"
```
