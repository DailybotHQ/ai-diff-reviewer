---
description: Cut a new vX.Y.Z release tag and publish a GitHub Release
---

# Release Cutter

Cut a new SemVer release of AI Diff Reviewer. The `release.yml` workflow then auto-updates the moving major tag for the current line (e.g. `v2`) so consumers pinning `@v2` pick up the new version automatically.

## Pre-flight

Before cutting a release, confirm:

1. You are on the default branch (`main`), up to date with `origin/main`.
2. `python3 -m py_compile scripts/reviewer.py` passes.
3. The most recent PR merged to `main` had a passing `self-review.yml` run.
4. `CHANGELOG.md` has at least one entry under `[Unreleased]`.

If any of those is false, stop and fix it first.

## Choose the version

SemVer:

- **Patch** (`v1.0.x`) — bug fixes, doc updates, internal refactors that don't change behaviour.
- **Minor** (`v1.x.0`) — new optional inputs, new providers, new outputs, new features that don't break existing consumers.
- **Major** (`v2.0.0`) — breaking changes to inputs/outputs, exit-code semantics, the marker constant, or any other public contract. Major bumps need an issue to discuss the migration story before proceeding.

If unsure, ask the user which level they want.

## Steps

### 1. Update CHANGELOG

Promote the `[Unreleased]` block to a versioned entry:

```diff
- ## [Unreleased]
+ ## [Unreleased]
+
+ ## [1.2.0] — 2026-MM-DD
```

Update the comparison links at the bottom:

```diff
- [Unreleased]: https://github.com/DailybotHQ/ai-diff-reviewer/compare/v1.1.0...HEAD
- [1.1.0]: ...
+ [Unreleased]: https://github.com/DailybotHQ/ai-diff-reviewer/compare/v1.2.0...HEAD
+ [1.2.0]: https://github.com/DailybotHQ/ai-diff-reviewer/compare/v1.1.0...v1.2.0
+ [1.1.0]: ...
```

### 2. Commit and push

```bash
git add CHANGELOG.md
git commit -m "chore(release): vX.Y.Z"
git push origin main
```

### 3. Tag and push the tag

```bash
git tag vX.Y.Z
git push origin vX.Y.Z
```

### 4. Publish the GitHub Release

Use the CLI:

```bash
gh release create vX.Y.Z \
  --title "vX.Y.Z" \
  --notes-from-tag \
  --latest
```

Or use the GitHub UI ("Draft a new release" on the Releases page) — paste the changelog section as the release notes.

The `release.yml` workflow fires on the `release` event and auto-updates the moving major tag for the current line (e.g. `v2`) to point at `vX.Y.Z`. No manual tag-update needed.

### 5. Verify

After a couple of minutes:

```bash
git fetch --tags
git rev-parse v2            # should resolve to the same SHA as vX.Y.Z
git log --oneline v2 -1     # should show the release commit
```

If the major tag didn't move, check the Actions tab for a failed `release.yml` run.

### 6. Announce

If this is a feature release worth announcing, ping the relevant channels (the project's discussions tab, the team chat, etc.). Patch releases typically don't need an announcement.

## Rollback

If something is wrong after release:

1. **Don't delete the tag.** Consumers may have already pulled it.
2. **Don't move the major tag back.** Same reason.
3. **Cut a patch release** (`vX.Y.{Z+1}`) that fixes the issue. The major tag will move forward to it.

The "moving major" pattern means consumers pinning `@v2` always get the latest patched version on the current major. That's the recovery mechanism.

## Major-version (vN.0.0) extras

For a major bump:

- The PR introducing the breaking change must include a migration guide (in the PR description or a new `docs/MIGRATION_v2.md`).
- The release notes must call out every breaking change up front.
- The README's "Quick start" `uses:` line gets updated to the new major (`@v2`).
- Open issues with the `breaking-change` label are linked in the release notes.
