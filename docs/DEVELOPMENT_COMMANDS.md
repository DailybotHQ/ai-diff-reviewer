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
export AIPRR_PROVIDER=anthropic             # or `openai`
export AIPRR_API_KEY=$ANTHROPIC_API_KEY
# Optional backend (v2.1.0+): point the runner at another host — the key
# above must be that backend's key. Empty = the runner's own vendor.
# export AIPRR_API_BASE=https://api.z.ai/api/anthropic   # Z.ai GLM
# export AIPRR_API_BASE=https://api.x.ai/v1              # xAI via `openai`

# --- or ---

# Agent-runner family (vendor CLI drives the loop; needs the CLI installed locally)
export AIPRR_PROVIDER=claude-code           # or `cursor`, `codex`, `grok`
export AIPRR_API_KEY=$ANTHROPIC_API_KEY     # or the vendor's key for the chosen CLI
# export AIPRR_API_BASE=...                 # claude-code → Z.ai / xAI; codex → Azure Foundry
# export AIPRR_GROK_VERSION=1.2.3           # CI-only: pins the installer; locally install `grok` yourself
```

Then set the shared context:

```bash
export AIPRR_GH_TOKEN=$GITHUB_TOKEN
export AIPRR_REPO=DailybotHQ/ai-diff-reviewer
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
export AIPRR_IGNORE_PATHS='**/fixtures/**,*.snap'   # extra globs on top of the built-in lockfile/minified/vendored exclusions
```

Chat-completions family only:

```bash
export AIPRR_MAX_TURNS=30                   # the action's own turn cap
```

Agent-runner family only:

```bash
export AIPRR_AGENT_MAX_TURNS=30             # enforced natively on grok; other CLIs log a per-provider warning
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

The runtime has a standard-library `unittest` suite (1058 tests across 50 files as of the v3 Phase 1 work, no install needed):

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

## Eval gate (v3) — offline checks, campaigns, verdicts

The eval gate ([RFC-01](rfc/v3/01-eval-gate-contract.md)) has an offline half that runs on every PR (`eval-gate-offline` in `code_check.yml`) and an online half that spends provider tokens only on demand. All tools are stdlib and live under `tests/eval/`.

```bash
# Offline (what CI runs — no secrets, no spend)
python3 tests/eval/schema_check.py --all                                    # every shipped schema validates its example
python3 tests/eval/records_validate.py --records tests/eval/records         # stored run records, verdicts, manifests, adjudications
python3 tests/eval/corpus_validate.py --json                                # the labelled corpus (pins, floors, blinded critical adjudication)
python3 tests/eval/determinism.py --selftest                                # verdict computation self-test

# Noise floor and verdicts over stored records
python3 tests/eval/determinism.py summarize --records tests/eval/records/campaigns --out /tmp/summary.json
python3 tests/eval/determinism.py verdict --baseline tests/eval/records/campaigns --candidate <records-dir> \
  --out tests/eval/records/verdicts/<campaign_id>.json --runtime-sha "$(git rev-parse HEAD)" \
  --prompt-sha256 "$(sha256sum prompts/default.md | cut -d' ' -f1)" --content-sha256 "$(sha256sum scripts/reviewer.py | cut -d' ' -f1)"
python3 tests/eval/release_gate.py --verdicts tests/eval/records/verdicts --runtime-sha "$(git rev-parse HEAD)"   # what auto-release.yml Step 1.5 asks

# Online campaigns (spend real tokens — projection first, hard stop at 90 % of the budget)
python3 tests/eval/campaign.py dry-run --manifest tests/eval/campaigns/phase0-floor.json --budget-usd 60
python3 tests/eval/campaign.py run --manifest tests/eval/campaigns/phase0-floor.json --lane grok --budget-usd 60 \
  --records-out tests/eval/records/campaigns/<campaign_id>/grok --runtime-sha "$(git rev-parse HEAD)"   # resumes over completed records
# In CI: Actions → "Eval Campaign" → Run workflow (budget_usd is required).

# One review against a labelled fixture tree (no GitHub access) or a real PR
XAI_API_KEY=… python3 tests/eval/run_eval.py run --provider grok --model balanced --api-key-env XAI_API_KEY \
  --tree tests/eval/cases/C001.json --out /tmp/C001.json
# … with the v3 verifier + severity policy after the review (the campaign arm sets `"verifier": "on"`):
XAI_API_KEY=… python3 tests/eval/run_eval.py run --provider grok --model balanced --api-key-env XAI_API_KEY \
  --tree tests/eval/cases/C001.json --verifier on --out /tmp/C001.json
GH_TOKEN=$(gh auth token) XAI_API_KEY=… python3 tests/eval/run_eval.py run --provider grok --model balanced --api-key-env XAI_API_KEY \
  --repo DailybotHQ/ai-diff-reviewer --pr 46 --worktree /path/to/worktree-at-pr-head --out /tmp/pr46.json

# Blinded, source-grounded adjudication of a campaign's findings → precision
python3 tests/eval/adjudicate.py worksheet --results tests/eval/records/campaigns/<campaign_id> --out /tmp/worksheet.json
#   fill `verdict` (true | false | overstated) and `note` per item, then:
python3 tests/eval/adjudicate.py seal --worksheet /tmp/worksheet.json --out tests/eval/records/adjudications/<campaign_id>.json \
  --adjudicator "<name>" --campaign-id <campaign_id>
```

Records are append-only; a `<campaign_id>.failed.md` note marks a bad campaign. Layout and rules: [`tests/eval/records/README.md`](../tests/eval/records/README.md); the measured floor and what it means for cost claims: [`PERFORMANCE.md`](PERFORMANCE.md#measured-noise-floor-and-the-eval-gate-verdict-v3).

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
