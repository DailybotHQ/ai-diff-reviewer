# PR metadata checks

Two v1.2.0+ features let the reviewer inspect and (optionally) mutate the PR itself: the description and the labels. Both are opt-in.

- **`pr-description-mode`** — checks whether the PR body is missing/vague and either warns, blocks, or writes a first-draft body.
- **`complexity-labels-enabled`** — asks the reviewer to assess PR complexity and apply a `complexity:*` label.

Together they turn the reviewer from a comment-only tool into one that shapes the PR's metadata, saving maintainer time on the housekeeping parts of review.

## `pr-description-mode`

### Modes

| Mode | Detection | Side-effect on PR | Effect on the check |
|---|---|---|---|
| `off` (default) | Not run. | None. | Unchanged. |
| `warn` | Body missing or under `pr-description-min-length` (default 50). | Appended note in the review summary. | Strictness gate unchanged. |
| `block` | Same as `warn`. | Appended note in the summary. | `blocked=true` regardless of severity. |
| `autocomplete` | Same as `warn`. | `PATCH /pulls/{n}` writes a first-draft body. Marker prevents re-writes. | Strictness gate unchanged. |

### `autocomplete` in detail

1. Before the review starts, the action evaluates the current body length (with the marker stripped).
2. If inadequate AND the marker is not present, the `set_pr_description` tool is exposed to the model.
3. The model calls the tool with a proposed body.
4. After the review posts, the action PATCHes the PR with `<proposed>\n\n<!-- ai-pr-reviewer-description-autocompleted -->`.
5. On subsequent runs, the marker is detected → no PATCH.

**Manual maintainer edits:** if a maintainer edits the body while keeping the marker (e.g. editing text before it), the marker still prevents re-write. If they delete the marker, the next run treats the body as fresh — that's the intended affordance for "reset the AI-generated body".

### Cost

Autocomplete adds ~1 extra tool call to the loop (`set_pr_description`) plus the summary tokens for the proposed body. In practice: ~300–800 extra output tokens on the first run per PR, zero on subsequent runs.

### Threat model summary

See [`SECURITY.md`](SECURITY.md) § "PR metadata PATCH surface" for the full model. Short version: no new permission required (`pull-requests: write` already needed for inline comments), at most 1 PATCH per run, prompt-injection-guarded via the tool description, best-effort failure semantics.

## `complexity-labels-enabled`

### How the reviewer assesses complexity

The model receives an explicit tool (`set_pr_complexity`) with three enum values (`low`/`medium`/`high`). The tool description in the schema explicitly asks the model to consider:

- **Cognitive load** — how many concepts a reviewer must hold in mind at once.
- **Files touched and layers crossed** — a 1000-line refactor of one file may be simpler than a 50-line change that crosses three subsystems.
- **Security surface** — auth, crypto, session, PII handling automatically shifts the assessment upward.
- **Test-coverage delta** — a large feature with proportional tests is easier to review than a small change with none.

The bundled default prompt reinforces these guidelines in its "How to think about each finding" section.

### The label

- Applied via `POST /issues/{n}/labels`.
- Named `<complexity-label-prefix><level>` — e.g. `complexity:high` with the default prefix.
- Prior labels matching the prefix are removed via `DELETE /issues/{n}/labels/<name>` before the new one is applied (`gh_remove_labels_by_prefix`).
- Idempotent: every run reassesses and re-labels. If the PR's scope grew between runs, the label updates.

### Why not just count lines?

The existing pattern in many repos is a line-count-based labeller (small = <50 lines, medium = <500, huge = >500). This works OK for very small PRs but is misleading for anything else — a 30-line change in auth code is not "small" from a reviewer's perspective. The AI signal captures cognitive load rather than diff size.

If you want to *combine* both signals (e.g. show `size:XL` for line-count and `complexity:high` for cognitive load), keep your existing labeller and add this one alongside it — they use different prefixes and don't collide.

### Downstream routing

The applied label is a real PR label — any GitHub Action or webhook can react to it. Common patterns:

- Route `complexity:high` PRs to require two reviewers via `CODEOWNERS` + label-based branch protection.
- Auto-request a specific senior team when `complexity:high` is applied.
- Post a Slack alert when `complexity:high` lands to make the review visible to the on-call channel.

## Combining the two features

They're orthogonal — enable both if you want:

```yaml
- uses: DailybotHQ/ai-diff-reviewer@v2
  with:
    api-key: ${{ secrets.ANTHROPIC_API_KEY }}
    github-token: ${{ secrets.GITHUB_TOKEN }}
    pr-description-mode: autocomplete
    complexity-labels-enabled: true
```

The `set_pr_description` and `set_pr_complexity` tools are exposed conditionally to the model in the same turn — no extra API call cost.

## Provider support matrix

| Provider family | `pr-description-mode: warn` / `block` | `pr-description-mode: autocomplete` | `complexity-labels-enabled` |
|---|:---:|:---:|:---:|
| Chat-completions (`provider: anthropic`) | ✅ | ✅ | ✅ |
| Agent-runner (`provider: cursor` / `claude-code` / `codex`) | ✅ | ⚠️ no-op in v1.2 | ⚠️ no-op in v1.2 |

**Why the split.** `warn` and `block` only need to inspect the PR body, which `main()` does independently of the provider. `autocomplete` and complexity labeling require the *model* to call `set_pr_description` / `set_pr_complexity` tools; those tools ride on top of this action's built-in tool-use loop, which agent-runner CLIs bypass by design (they own their own loops). Bridging the CLIs' findings.json schema to carry these two extra signals is on the v1.3 roadmap.

**What happens if you turn them on with an agent-runner provider.** The action logs a `WARNING:` line at the start of the run listing the specific inputs that will no-op, and the review still runs end-to-end (inline comments, summary, strictness gate — all unchanged). You just don't get the PATCH or the label. If you rely on either, pin `provider: anthropic` until v1.3.
