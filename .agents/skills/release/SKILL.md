---
name: release
description: Cut a new vX.Y.Z release tag, update CHANGELOG, push to origin, publish a GitHub Release. The release.yml workflow then auto-updates the moving major tag for the current line (e.g. v2) so consumers pinning @v2 pick up the new version.
disable-model-invocation: false
allowed-tools: Read, Write, Edit, Glob, Grep, Bash
model: sonnet
tier: 2
intent: release
max-files: 3
max-loc: 50
---

# Skill: Release

## Objective

Cut a SemVer release of AI Diff Reviewer — `chore(release): vX.Y.Z` commit, tag, push, and GitHub Release publication. The `release.yml` workflow handles the moving major tag automatically.

This skill is the canonical procedure; the slash-command entry point at `.agents/commands/release.md` is the user-facing interface.

## Non-goals

- Does NOT bump versions or generate changelog entries from scratch — those should already exist under `[Unreleased]` in `CHANGELOG.md`.
- Does NOT update the moving major tag manually — `release.yml` does that on the `release` event.
- Does NOT push breaking changes without a major-version discussion. If this is a `vN.0.0`, a separate process (issue, migration guide) must have happened first.

## Inputs

- `version` — the SemVer tag to cut, e.g. `v1.2.0`. Required. Ask the user if not provided.

## Pre-flight

Run before doing anything destructive:

```bash
# On the right branch?
git rev-parse --abbrev-ref HEAD              # must be `main`
git fetch origin && git status               # working tree clean, up to date

# Compile-check passes?
python3 -m py_compile scripts/reviewer.py    # exit 0

# Action.yml parses?
python3 -c "import yaml; yaml.safe_load(open('action.yml'))"

# CHANGELOG has [Unreleased] entries?
grep -A1 "^## \[Unreleased\]" CHANGELOG.md
```

If anything is wrong, stop and surface the issue to the user. Do not proceed.

## Steps

### 1. Update CHANGELOG.md

- Promote `## [Unreleased]` to `## [X.Y.Z] — YYYY-MM-DD` (today's date).
- Insert a fresh empty `## [Unreleased]` block above the new versioned entry.
- Update the comparison links at the bottom:

```diff
- [Unreleased]: https://github.com/DailybotHQ/ai-diff-reviewer/compare/<prev>...HEAD
- [<prev-version>]: ...
+ [Unreleased]: https://github.com/DailybotHQ/ai-diff-reviewer/compare/<new>...HEAD
+ [<new-version>]: https://github.com/DailybotHQ/ai-diff-reviewer/compare/<prev>...<new>
+ [<prev-version>]: ...
```

### 2. Commit

```bash
git add CHANGELOG.md
git commit -m "chore(release): vX.Y.Z"
```

### 3. Push the commit

```bash
git push origin main
```

### 4. Tag and push the tag

```bash
git tag vX.Y.Z
git push origin vX.Y.Z
```

### 5. Publish the GitHub Release

```bash
gh release create vX.Y.Z \
  --title "vX.Y.Z" \
  --notes-from-tag \
  --latest
```

If the auto-extracted notes look wrong, edit them in the GitHub UI afterwards.

### 6. Verify

After ~1 minute:

```bash
git fetch --tags
git rev-parse vX                              # major tag should match vX.Y.Z
git log --oneline vX -1                       # should be the release commit
gh run list --workflow=release.yml --limit 1  # the release workflow should be green
```

If `vX` didn't move to the new tag, check the `release.yml` run — it might have failed.

## Post-release

- Confirm with the user whether to announce. Patch releases usually don't; minor/major usually do.
- For a major release, double-check that `README.md`'s `uses: DailybotHQ/ai-diff-reviewer@vN` line points at the new major.

## Failure modes

- **Compile-check fails before tagging.** Fix on `main`, push, then start over.
- **Tag push fails because tag exists.** A previous attempt left the tag locally. Verify `git tag -l vX.Y.Z` matches the intended commit; if so, just push it. If not, delete and retag (`git tag -d vX.Y.Z`) — but only if it doesn't exist on the remote yet.
- **`gh release create` fails.** Likely the tag isn't pushed yet, or `gh` isn't authenticated. Check `gh auth status`.
- **`release.yml` fails to move the major tag.** Investigate the workflow log; a manual fallback is `git tag -f vX vX.Y.Z && git push origin vX --force` — but only if you understand the workflow's behaviour.

## Rollback

Releases are public. **Don't delete tags** — consumers may have pulled them. **Don't force-move the major tag back** — same reason. Instead, cut `vX.Y.{Z+1}` with the fix; the moving major tag advances forward to it.
