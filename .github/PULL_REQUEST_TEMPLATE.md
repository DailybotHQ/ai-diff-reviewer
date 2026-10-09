<!--
Thanks for contributing to AI Diff Reviewer! Keep PRs small and focused
(1–3 related changes). See CONTRIBUTING.md and AGENTS.md for the full rules.
-->

## Summary

<!-- 1–2 sentences: the why, not just the what. -->

## Linked issue

<!-- e.g. "Closes #123", or "none — small fix". -->

## Change Log

-
-

## Risks

<!-- e.g. "None — docs-only change", or describe blast radius. -->
-

## Test evidence

<!-- Commands you ran and their result (e.g. `python3 -m unittest discover -s tests` → OK), or the manual verification you did. -->

## Checklist

- [ ] No secrets, credentials, personal paths or private context (internal names, private repositories, customer data) — `bash scripts/check-public-hygiene.sh` passes.
- [ ] Commit messages follow [Conventional Commits](https://www.conventionalcommits.org/).
- [ ] `python3 -m py_compile scripts/reviewer.py` passes (if the runtime changed).
- [ ] `python3 -m unittest discover -s tests` passes (if the runtime changed).
- [ ] No new non-stdlib imports in `scripts/reviewer.py` (runtime is stdlib-only).
- [ ] `README.md` input/output tables updated (if `action.yml` changed).
- [ ] `CHANGELOG.md` entry added under `[Unreleased]` (if behaviour changed).
- [ ] An `examples/` snippet was added (if a new input has a non-trivial usage).
- [ ] Edited the canonical `AGENTS.md` / `.agents/...` paths — not the `CLAUDE.md` / `.claude` symlinks.
