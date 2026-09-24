# Release Recovery Playbook

Recovery procedures when [`auto-release.yml`](../.github/workflows/auto-release.yml)
fails partway through cutting a release. Companion to
[`docs/DEVELOPMENT_COMMANDS.md`](DEVELOPMENT_COMMANDS.md) and referenced from
[`AGENTS.md`](../AGENTS.md) Rule #8 (SemVer for Releases).

For the full release flow overview see `auto-release.yml` — the header
comments walk through every step.

---

## Partial release: tag pushed but sync commit rejected

**Symptom:** `auto-release.yml` fails in Step 3 with:

```text
remote: - Changes must be made through a pull request.
remote: - 8 of 8 required status checks are expected.
 * [new tag]         vX.Y.Z -> vX.Y.Z
 ! [remote rejected] HEAD -> main (push declined due to repository rule violations)
error: failed to push some refs
```

**Since Step 3 was hardened to use `git push --atomic HEAD:main vX.Y.Z`,
this shouldn't happen anymore** — the atomic push fails as one unit, so
you never end up with a lonely tag. But if you're recovering from a
pre-hardening release (like PR #26's initial `v1.5.0` cut) or from an
edge case that bypasses `--atomic`, the state to recognise is:

- Tag `vX.Y.Z` exists on remote.
- Main branch does NOT contain the `chore(release): sync skill artifacts
  for vX.Y.Z [skip release]` commit that Step 2.5 generated.
- No GitHub Release for `vX.Y.Z` exists (Step 5 never ran).
- Major alias tag (`v2`) still points at the previous release (Step 3's
  `git tag -f` never ran because of `set -euo pipefail` aborting on the
  Step 3 push failure).

### Root cause

The `AUTOMATION_GITHUB_TOKEN` (or its fallback `secrets.GITHUB_TOKEN`) is
not on the bypass list of the repo's branch protection ruleset for
`main`. Any release commit that touches `skills/**/SKILL.md` version
fields triggers Step 2.5 to produce a real `chore(release): sync…` commit
that must land on main alongside the tag; the direct push is blocked.

The two long-term fixes are documented under
["Preventing the recurrence"](#preventing-the-recurrence) below.

### Immediate recovery (manual, ~10 minutes)

Everything below assumes you're on a fresh clone or a clean working
tree, with push access to `main` (via a PR) and permission to move
the major-version tag.

#### 1. Confirm the state

```bash
git fetch --tags --force

# The bad tag exists locally and on remote.
git show vX.Y.Z --stat | head -20

# But main doesn't contain the sync commit yet.
git log origin/main..vX.Y.Z --oneline
# Should show ONE commit — the `chore(release): sync skill artifacts`.

# And @v2 is still on the old release. Peel both refs to their
# underlying commit SHA — if the major alias is still an annotated tag
# pointing at a tag object, a bare `rev-parse v2` returns the tag-object
# SHA and wouldn't compare cleanly against a commit SHA.
git rev-parse v2^{commit}     # ← previous release commit SHA
git rev-parse vX.Y.Z^{commit} # ← new release commit SHA (the sync commit)
```

If the last commit reachable from `vX.Y.Z` matches the current `main`
plus one `chore(release): sync…` commit, you're in the exact partial
state and can proceed.

#### 2. Verify nobody consumed `vX.Y.Z` yet

The tag content IS correct (Step 2.5 sedded the SKILL.md files to the
right version), so pinning `@vX.Y.Z` explicitly returns valid code. But
the tag SHA is orphan-ish (not reachable from main), so a subsequent
force-move by us is a minor breaking change for anyone who cached the
current SHA. Realistically: within a few hours of the failure, nobody
external will have pinned it.

```bash
# Check if there's a GitHub Release for the tag (there shouldn't be).
gh api repos/DailybotHQ/ai-diff-reviewer/releases/tags/vX.Y.Z 2>&1 | head -3

# Expected: '"message":"Not Found"' — that means no Marketplace update
# either, since Marketplace only publishes on Release creation.
```

#### 3. Open a recovery PR

```bash
git checkout main
git pull origin main

git checkout -b fix/recover-vX.Y.Z-and-harden-auto-release
git cherry-pick <sync-commit-sha>
# The cherry-picked commit's body still carries [skip release], but
# see the merge-message warning below — the squash commit that
# auto-release actually reads is a different beast.

git push -u origin HEAD
gh pr create --title "chore(release): recover vX.Y.Z sync commit + harden auto-release" \
  --body "Recovery for the partial vX.Y.Z release (see docs/RELEASE_RECOVERY.md)."
```

**IMPORTANT — squash-merge changes the effective commit message.** This
repo squash-merges by convention (see [`docs/STANDARDS.md`](STANDARDS.md)
and [`docs/DEVELOPMENT_COMMANDS.md`](DEVELOPMENT_COMMANDS.md)). The
`[skip release]` marker in the cherry-picked commit's body is
**invisible** to auto-release after squash — auto-release reads
`github.event.head_commit.message`, which for a squash push is the
newly-created merge commit's message (typically the PR title as
subject, plus whatever body was set at merge time).

To guarantee auto-release skips on merge, do ONE of:

1. **Prefix the PR title with `chore(release):`** — auto-release skips
   any head commit whose message starts with `chore(release):`
   (from `auto-release.yml`'s job-level `if:`).
2. **Include `[skip release]` in the squash commit BODY at merge time**
   — either via `gh pr merge <n> --squash --body "$(cat <<'EOF'
   original body

   [skip release]
   EOF
   )"` or by editing the body in the GitHub merge dialog.

Without one of these, auto-release will fire on merge, try to bump to
`vX.Y.Z+1`, run Step 2.5 again (which will re-sed the same SKILL.md
files to a NEW version), and hit the same branch-protection wall. With
the `--atomic` hardening in place the tag won't get published, but you'll
get a spurious failed workflow run and be back to square one.

#### 4. Move the major alias tag

After merge, the squash commit on `main` contains the same SKILL.md
version bumps as the `vX.Y.Z` tag, but the two commits will have
different SHAs (cherry-picks and squashes both create new SHAs). If
the recovery PR carried additional changes (hardening, docs), `main`'s
tree will also differ from `vX.Y.Z`'s tree — that's fine, those extras
will ship in a subsequent release.

The relevant invariant for consumers is that `@v2` should point at the
newest `v2.x.y` release tag, so move it to `vX.Y.Z` (not to `origin/main`):

```bash
git fetch origin --tags

# Sanity check — vX.Y.Z's target commit should have the SKILL.md
# version bumped correctly.
git show vX.Y.Z:skills/ai-diff-reviewer/SKILL.md | grep '^version:'
# Expected: version: "X.Y.Z"

# Move v2 to vX.Y.Z. Force is correct — v2 is a moving pointer by design.
#
# The `vX.Y.Z^{}` peel is load-bearing: without it, `git tag -f v2 vX.Y.Z`
# creates v2 as a nested annotated tag pointing at the vX.Y.Z tag object
# (which then points at the commit). Both are valid Git refs, but nested
# tags make `git rev-parse v2^{commit}` require an extra hop, and
# tag-object SHAs on GitHub's ref API don't compare equal to commit SHAs.
# Peeling to `^{}` makes v2 a lightweight tag directly at the commit,
# matching what a consumer running `git checkout v2` expects.
git tag -f v2 "vX.Y.Z^{}"
git push origin v2 --force

# Verify remote agrees — both must print the SAME commit SHA.
git fetch origin --tags --force
git rev-parse v2^{commit}       # commit the major alias now points at
git rev-parse vX.Y.Z^{commit}   # commit vX.Y.Z points at
```

#### 5. Create the GitHub Release

Auto-release's Step 5 never ran, so no Release page exists yet. Create
it manually with auto-generated notes:

```bash
# Filter to release tags (`vMAJOR.MINOR.PATCH`) only — matches the same
# pattern auto-release.yml Step 2 uses to compute `previous_tag`.
# Excludes major aliases (`v2`), pre-release tags, and anything else
# that could otherwise slip in with `git tag --sort=-v:refname | sed -n 2p`.
PREV=$(git tag --list 'v[0-9]*.[0-9]*.[0-9]*' --sort=-v:refname \
  | grep -E '^v[0-9]+\.[0-9]+\.[0-9]+$' \
  | grep -vFx "vX.Y.Z" \
  | head -n1)                                    # e.g. v1.4.2
gh release create vX.Y.Z \
  --title "vX.Y.Z" \
  --generate-notes \
  --notes-start-tag "$PREV"
```

This is the step that (a) publishes release notes on the Releases page
and (b) triggers GitHub Marketplace to pick up the new version.

#### 6. Refresh the vendored dogfood copy

Step 3.5 never ran, so `.agents/skills/ai-diff-reviewer/` is still at
the pre-release version. Fix that in one PR:

```bash
git checkout main && git pull
git checkout -b chore/refresh-vendored-vX.Y.Z

npx --yes skills add "DailybotHQ/ai-diff-reviewer@vX.Y.Z" \
  --skill ai-diff-reviewer --force

# Verify the vendored SKILL.md matches the just-published tag.
VENDORED=$(sed -nE 's/^version:[[:space:]]*"([^"]+)".*/\1/p' \
  .agents/skills/ai-diff-reviewer/SKILL.md | head -n1)
echo "Vendored version: $VENDORED"
# Must equal ${VERSION#v} (e.g. "1.5.0" if vX.Y.Z was "v1.5.0").

git add .agents/skills/ai-diff-reviewer skills-lock.json
git commit -m "chore(release): dogfood vendored ai-diff-reviewer to vX.Y.Z [skip release]"
git push -u origin HEAD
gh pr create --title "chore(release): dogfood vendored ai-diff-reviewer to vX.Y.Z" \
  --body "Manual Step 3.5 recovery for vX.Y.Z."
```

The PR title starts with `chore(release):` — auto-release's job-level
`if:` guard skips any head commit whose message begins with that
prefix, which is what actually protects against a spurious `vX.Y.Z+1`
under squash-merge (see the Step 3 warning above). The `[skip release]`
marker on the pre-squash commit is belt-and-suspenders only.

---

## Release skipped by the eval gate (v3, BC-02)

**Symptom:** `auto-release.yml` Step 1.5 prints one of

```text
release-gate: eval-verdict-missing — no verdict matches runtime content … + prompt …
release-gate: eval-verdict-stale — <file>: computed_at … is older than 30 days
release-gate: eval-verdict-blocking — <file>: <blocking findings>
```

A fourth line, `release-gate: eval-gate-error — <exception>`, means the gate
**itself** could not run (missing `scripts/reviewer.py` or `prompts/default.md`
at the candidate, an unreadable verdict file, a bug in the gate). The step
then fails with `::error::` and `blocked=error` — an infrastructure fault is
never reported as "no verdict". Fix the cause and re-run the workflow.

and every later step is skipped (`steps.version.outputs.skip` is empty, so
Step 2.5 onward never run). Nothing was tagged or pushed — this is a clean
skip, not a partial release.

**What the gate checks** (`tests/eval/release_gate.py`): the newest
`verdict/1.0` under `tests/eval/records/verdicts/` whose
`candidate_content_sha256` equals SHA-256 of `scripts/reviewer.py` on the
candidate commit and whose `prompt_sha256` equals SHA-256 of
`prompts/default.md`. Docs-only commits after the measured commit still
match; any runtime or prompt change needs a new verdict. **Newest matching
verdict only:** when several verdicts share the candidate's hashes (for
example a trees-only campaign and a PR campaign measured on the same
commit), the one with the latest `computed_at` decides — a later blocking
verdict freezes the cut and a later non-blocking one unlocks it, regardless
of which campaign was larger. Name the campaign in `--baseline-ref` and keep
one verdict per release candidate to avoid surprises.

**Recovery:**

1. `missing` — dispatch `eval-campaign.yml` on the candidate branch with a
   `budget_usd` and `baseline` (the stored baseline campaign's records dir);
   the workflow opens a PR with the verdict file; merge it (`[skip release]`
   is in its commit message); push a no-op or the next change to `main` to
   re-trigger the release.
2. `stale` — same as missing: re-run the campaign; a verdict is trusted for
   30 days (`--max-age-days`).
3. `blocking` — read the verdict's `blocking` list (recall regression,
   widened determinism, unknown usage on a first-party lane). Fix the
   regression, re-measure, and only then release. Do **not** delete or edit
   the blocking verdict; a newer non-blocking verdict for the fixed content
   supersedes it.

The gate never fails the workflow red; it skips the release so the log
explains itself. `[skip release]` commits bypass the whole job as before.

## Preventing the recurrence

Two paths — pick one before the next release that includes a
`skills/**/SKILL.md` version bump.

### Option A (recommended): AUTOMATION_GITHUB_TOKEN with bypass

Create a fine-grained PAT (or GitHub App installation token) for the
DailybotHQ automation account with:

- **Repository access:** just `DailybotHQ/ai-diff-reviewer`.
- **Permissions:** `Contents: Read and write`, `Metadata: Read` (default).

Then:

1. Add the token as `AUTOMATION_GITHUB_TOKEN` under
   `Settings → Secrets and variables → Actions`.
2. Add the same automation identity as a **bypass actor** for the `main`
   ruleset under `Settings → Rules → Rulesets → main`.

`auto-release.yml` already prefers `AUTOMATION_GITHUB_TOKEN` over
`GITHUB_TOKEN` at every step (`token: ${{ secrets.AUTOMATION_GITHUB_TOKEN
|| secrets.GITHUB_TOKEN }}`), so no workflow change is required — just
the settings.

### Option B: keep GITHUB_TOKEN, rework Step 2.5 to open a PR

Instead of committing the sync artifacts directly to main and pushing,
open a PR from the runner via `gh pr create` + `gh pr merge --auto
--squash`. This funnels the sync commit through the same PR flow as any
other change, so branch protection is satisfied without a bypass token.

Trade-off: adds a PR-lifetime of latency to every release (waiting for
required checks to run before auto-merge). For a repo that ships
weekly-to-monthly, this is fine. For a repo that ships multiple times a
day, it's noise.

Not implemented today; documented here as the alternative if
`AUTOMATION_GITHUB_TOKEN` bypass configuration is off the table for
policy reasons.

---

## Related failure modes (for completeness)

### Step 3.5 dogfood refresh fails after a successful release

Step 3.5 runs after the atomic tag+main push (Step 3) succeeded but
BEFORE the GitHub Release is created (Steps 4–5). Its own final push
(`git push origin HEAD:main` for the `chore(release): dogfood…` commit)
does NOT use `--atomic` because there's no accompanying tag — it's a
lone commit push. If that push is rejected by branch protection (same
root cause as the Step 3 failure this playbook covers) OR if the
`npx skills add` fetch fails OR if the version-assertion check
mismatches, the step exits non-zero AND `set -euo pipefail` aborts the
job before Steps 4 and 5.

After a Step 3.5 abort the state is:

- ✅ Tag `vX.Y.Z` on remote (Step 3 succeeded)
- ✅ `main` contains the Step 2.5 sync commit (Step 3 pushed it)
- ✅ `@v2` was moved to `vX.Y.Z` (last line of Step 3)
- ❌ Vendored `.agents/skills/` NOT refreshed (Step 3.5 aborted)
- ❌ **No GitHub Release for `vX.Y.Z`** (Step 4 didn't run)
- ❌ No Marketplace update (that trigger fires on Release creation)

Recovery is TWO manual steps, not one:

1. **Create the GH Release manually** — [section 5](#5-create-the-github-release)
   above. This is the load-bearing step for Marketplace; the vendored
   dogfood refresh is cosmetic by comparison.
2. **Refresh the vendored copy in a follow-up PR** — [section 6](#6-refresh-the-vendored-dogfood-copy)
   above.

Step 3.5's push has the same branch-protection blind spot as Step 3
did before the `--atomic` hardening. Two acceptable follow-up fixes,
in order of preference:

1. **Wire `AUTOMATION_GITHUB_TOKEN` bypass** (Option A above). Same
   configuration that unblocks Step 3's push also unblocks Step 3.5's
   commit push — one setting, both steps fixed. Preferred because it
   also fixes any FUTURE step that needs to push to `main`.
2. **Reorder Steps 4–5 before Step 3.5.** Publish the GitHub Release
   FIRST (so Marketplace gets the update and consumers can pin the
   new tag), THEN attempt the dogfood refresh. A Step 3.5 failure
   still aborts the workflow, but the release-visible surface is
   already complete by that point; recovery is only the vendored
   copy PR ([section 6](#6-refresh-the-vendored-dogfood-copy)).

Avoid `continue-on-error: true` on Step 3.5 as the fix — it would
silently mask a broken skill-install smoke test (the whole point of
Step 3.5's third purpose is to loudly catch broken `npx skills` fetches
on the just-published tag), turning green what should be red.

### CHANGELOG not stamped (or stamp step failed)

Since v2.2.0 Step 2.5 runs `python3 .github/scripts/stamp_changelog.py --version vX.Y.Z --date <UTC date> CHANGELOG.md` before the sync commit, turning `## [Unreleased]` into `## [X.Y.Z] — date` and opening a fresh empty `[Unreleased]`. Three outcomes are normal and logged: stamped; already stamped (idempotent re-run); empty section (`::notice`, release proceeds). The step fails only when the file has no `## [Unreleased]` header at all — restore the header and re-run the release job. If a release shipped before the stamp existed (v2.0.0, v2.0.1 and v2.1.0 did), add the dated section by hand from `git log <prev>..<tag>` and keep the bullet count intact; `--dry-run` previews what the script would do.

### `skills-prompt-sync` CI check fires on a release PR

Means `prompts/default.md` and `skills/ai-diff-reviewer/prompt.md`
drifted between merges. Fix by re-syncing in the PR:

```bash
cp prompts/default.md skills/ai-diff-reviewer/prompt.md
git add skills/ai-diff-reviewer/prompt.md
git commit -m "fix(ci): re-sync skill prompt with default"
```

This is normally handled by auto-release Step 2.5; only fires if you
edited `prompts/default.md` in a PR that also touches `skills/`.

### Scenario: the release silently skipped because a commit body *quoted* the marker

`auto-release.yml` skips when the head commit **message** (subject + body)
contains `[skip release]`. A squash merge concatenates every PR commit body
into the merge commit, so a task commit that merely *mentions* the marker in
prose ("rides the existing `[skip release]` sync commit") suppresses the
release of the whole PR — this happened with PR #51 (`bb7deec`): the run
reported `skipped`, no tag, no CHANGELOG stamp, no vendored-skill refresh.

Recovery: merge one more conventional commit to `main` (a `feat:` or `fix:`
subject decides the bump; the squash **subject** is what the bump derivation
reads, `git log --format=%s`). Prevention: never write the literal marker in a
commit body unless you mean it — spell it `skip-release marker` in prose.

---

## Contact

For anything not covered here, file a bug at
<https://github.com/DailybotHQ/ai-diff-reviewer/issues> with the
workflow-run URL and the observed remote state.
