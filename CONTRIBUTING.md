# Contributing

Thanks for your interest in improving AI Diff Reviewer. This is an open-source project maintained by DailybotHQ and its contributors. AI coding agents (and humans who want the full rule set) start at [`AGENTS.md`](AGENTS.md) — the single source of truth for repository standards.

## Ways to contribute

- **Bug reports** — open an issue with a minimal reproduction (workflow YAML + the failure mode you saw).
- **Feature requests** — open an issue *first*, before sending a PR. Big surface-area changes (new inputs, new outputs, new providers) need a quick design discussion to make sure the action stays simple and stable.
- **Prompt improvements** — the bundled default prompt (`prompts/default.md`) is opinionated but not sacred. PRs that make the reviewer catch more real bugs and fewer false positives are very welcome. Include before/after examples on a real PR if you can.
- **Provider implementations** — see `docs/PROVIDERS.md § Adding a runner or backend` (a new vendor on an existing protocol is a host suffix + tier row; a new runner is a class). OpenAI and Azure Foundry shipped in v2.1.0; Gemini and Bedrock remain open.

## Project layout

```
.
├── action.yml              # Composite-action entrypoint and inputs/outputs schema
├── scripts/
│   └── reviewer.py         # All runtime logic — stdlib only
├── prompts/
│   └── default.md          # Default system prompt (technology-agnostic)
├── examples/               # Copy-paste workflow snippets for common setups
├── docs/                   # Deep-dive docs (prompts, strictness, providers)
├── .github/workflows/      # CI: compile-check, self-review, release
├── README.md
├── CHANGELOG.md
└── LICENSE                 # MIT
```

## Local development

The reviewer is one Python script using only the standard library. No virtualenv, no `pip install`, no Docker.

```bash
# The gate — what CI runs on every PR (see docs/TESTING_GUIDE.md for scoped commands)
python3 -m py_compile scripts/reviewer.py
python3 -m unittest discover -s tests
bash scripts/check-public-hygiene.sh

# Run against a real PR locally (requires the same env the action sets)
export AIPRR_PROVIDER=anthropic              # any of the six runners; see docs/DEVELOPMENT_COMMANDS.md
export AIPRR_API_KEY=$ANTHROPIC_API_KEY
# export AIPRR_API_BASE=https://…            # optional backend (Z.ai / xAI / Azure / gateway)
export AIPRR_GH_TOKEN=$GITHUB_TOKEN
export AIPRR_REPO=DailybotHQ/ai-diff-reviewer
export AIPRR_PR_NUMBER=42
export AIPRR_HEAD_SHA=$(git rev-parse HEAD)
export AIPRR_BASE_REF=main
export AIPRR_ACTION_PATH=$PWD
python3 scripts/reviewer.py
```

## Code style

- Python ≥ 3.10. Type hints everywhere. The script targets the runners' default Python; we don't take a dependency on a non-default version.
- **Standard library only.** This is a load-bearing constraint — every dep is a supply-chain question for every consumer.
- Functions over classes unless state is genuinely shared. The two real classes (`PRContext`, `ReviewState`) carry mutable state across calls; everything else is a free function.
- Comments explain *why*, not *what*. If the why is obvious from the name, omit the comment.
- Keep the action surface small. Every new input is a long-lived public contract.

## Pull request flow

1. Fork (or branch, if you have write access) from `main`; keep the change to 1–3 tightly related concerns.
2. Commit with [Conventional Commits](https://www.conventionalcommits.org/) (`feat:`, `fix:`, `docs:`, `ci:`, `chore:` …). The squash-merge subject decides the release bump.
3. Run the gate above locally, then open a pull request against `main` and fill in the template (summary, linked issue, test evidence).
4. CI (`code_check.yml`) must be green; maintainers apply the `ready` label to run the self-review dogfood (`self-review.yml`). A maintainer review is required before merge.
5. Never commit secrets, real credentials, personal paths or private context — the `Public hygiene` CI job enforces it.

A DCO sign-off is **not** required.

## Pull request checklist

- [ ] `python3 -m py_compile scripts/reviewer.py` passes.
- [ ] `python3 -m unittest discover -s tests` and `bash scripts/check-public-hygiene.sh` pass.
- [ ] If you changed `action.yml` inputs/outputs: README's input/output tables updated.
- [ ] If you changed runtime behaviour: `CHANGELOG.md` updated under `[Unreleased]`.
- [ ] If you added a new input: there's an example in `examples/` showing realistic usage.
- [ ] If you touched the default prompt: a before/after on a real PR pasted in the PR description.
- [ ] No new dependencies (stdlib only).
- [ ] Commits follow Conventional Commits (`feat:`, `fix:`, `docs:`, …).

## Releasing

Tagged releases follow SemVer (`v1.2.3`). The `release.yml` workflow auto-updates the major-version moving tag for the current line (`v3`) when a new `v3.x.y` is published, so consumers pinning `@v3` get patches and minor features automatically. Each release carries its CHANGELOG section as notes, source archives and a `SHA256SUMS` file (`sha256sum -c SHA256SUMS` to verify).

- The squash merge's **subject** decides the version bump (`feat:` → minor, `fix:`/`perf:` → patch, `!`/`BREAKING CHANGE` → major): give PRs a Conventional Commits title.
- Never write the literal `[skip release]` marker in a commit body unless you want to suppress the release. Squash merges carry every commit body into the merge commit and the release job reads the whole message (see `docs/RELEASE_RECOVERY.md`). In prose, write "skip-release marker".

## Code of conduct

This project follows the [Contributor Covenant 2.1](CODE_OF_CONDUCT.md). Be kind. Assume good faith. Reviewers should treat contributors with the same charity the bundled default prompt asks of the reviewer model: assume the author has more context than you, frame findings as questions, prefer signal over volume.

## License

By contributing, you agree that your contributions will be licensed under the project's [MIT License](LICENSE).
