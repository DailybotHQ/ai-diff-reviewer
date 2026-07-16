# Contributing

Thanks for your interest in improving AI Diff Reviewer. This is an open-source project maintained by the open-source community.

## Ways to contribute

- **Bug reports** — open an issue with a minimal reproduction (workflow YAML + the failure mode you saw).
- **Feature requests** — open an issue *first*, before sending a PR. Big surface-area changes (new inputs, new outputs, new providers) need a quick design discussion to make sure the action stays simple and stable.
- **Prompt improvements** — the bundled default prompt (`prompts/default.md`) is opinionated but not sacred. PRs that make the reviewer catch more real bugs and fewer false positives are very welcome. Include before/after examples on a real PR if you can.
- **Provider implementations** — see `docs/PROVIDERS.md` for the contract a new provider has to satisfy. OpenAI, Gemini, and Azure OpenAI are explicitly on the roadmap.

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
# Compile-check (the only smoke test we run in CI)
python3 -m py_compile scripts/reviewer.py

# Run against a real PR locally (requires the same env the action sets)
export AIPRR_PROVIDER=anthropic
export AIPRR_API_KEY=$ANTHROPIC_API_KEY
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

## Pull request checklist

- [ ] `python3 -m py_compile scripts/reviewer.py` passes.
- [ ] If you changed `action.yml` inputs/outputs: README's input/output tables updated.
- [ ] If you changed runtime behaviour: `CHANGELOG.md` updated under `[Unreleased]`.
- [ ] If you added a new input: there's an example in `examples/` showing realistic usage.
- [ ] If you touched the default prompt: a before/after on a real PR pasted in the PR description.
- [ ] No new dependencies (stdlib only).
- [ ] Commits follow Conventional Commits (`feat:`, `fix:`, `docs:`, …).

## Releasing

Tagged releases follow SemVer (`v1.2.3`). The `release.yml` workflow auto-updates the major-version moving tag for the current line (`v2`) when a new `v2.x.y` is published, so consumers pinning `@v2` get patches and minor features automatically.

## Code of conduct

Be kind. Assume good faith. Reviewers should treat contributors with the same charity the bundled default prompt asks of the reviewer model: assume the author has more context than you, frame findings as questions, prefer signal over volume.

## License

By contributing, you agree that your contributions will be licensed under the project's [MIT License](LICENSE).
