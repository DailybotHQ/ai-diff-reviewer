# Migrating to v2.0.0

Short guide for consumers moving from the `v1` line to **AI Diff Reviewer v2**.

## What to change

| Surface | Before | After |
|---|---|---|
| GitHub Action pin | `uses: DailybotHQ/ai-diff-reviewer@v1` | `uses: DailybotHQ/ai-diff-reviewer@v2` |
| Exact pin (optional) | `@v1.8.0` | `@v2.0.0` |
| Local companion skill | `npx skills add DailybotHQ/ai-diff-reviewer@v1 --skill ai-diff-reviewer` | `npx skills add DailybotHQ/ai-diff-reviewer@v2 --skill ai-diff-reviewer` (or `npx skills update ai-diff-reviewer`) |

`@v1` remains valid and **freezes at `v1.8.0`**. It does not auto-move to v2.

## Contract

- **No `action.yml` inputs renamed or removed.** Optional IAR / `skip-review-label` inputs keep the same names and defaults as on the `v1.8.0` tag line.
- Env-var prefix stays `AIPRR_`.
- Repo path stays `DailybotHQ/ai-diff-reviewer` (old `ai-pr-reviewer` URL still 301-redirects).

## Behavioral notes (already on v1.8.0; now the v2 platform story)

1. **Iteration-Aware Review** runs on every CI review (default policy `first-pass-exhaustive`). Round 2+ of a generation may dedupe non-critical findings you already saw.
2. **Local skill reviews** stay a full pass — they do not run IAR dedup.
3. Escape / reset / emergency-bypass gestures:
   - Escape once: label `full-review-please` (default `iteration-escape-label`)
   - Clean slate: remove `applied-label` after a successful stamp (five-condition `USER_FORCED_RESET` — see [ITERATION_AWARENESS.md § 8.5](ITERATION_AWARENESS.md))
   - Emergency bypass: opt-in `skip-review-label: skip-ai-review` (+ ruleset) — see [TRIGGER_MODES.md](TRIGGER_MODES.md)

## Further reading

- [CHANGELOG.md](../CHANGELOG.md) — full Unreleased / v2.0.0 notes
- [ITERATION_AWARENESS.md](ITERATION_AWARENESS.md)
- [examples/iteration-aware.yml](../examples/iteration-aware.yml)
- [examples/skip-review-label.yml](../examples/skip-review-label.yml)
