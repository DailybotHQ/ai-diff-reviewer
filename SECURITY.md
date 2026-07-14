# Security Policy

## Reporting a vulnerability

**Please do not open a public GitHub issue.** Report vulnerabilities
privately through GitHub's Security Advisory flow:

**→ [Open a private advisory](https://github.com/DailybotHQ/ai-pr-reviewer/security/advisories/new)**
(`github.com/DailybotHQ/ai-pr-reviewer/security/advisories/new`)

The advisory is scoped to a small circle of maintainers and lets us
collaborate on the fix privately before a coordinated disclosure. You do
not need special permissions to open one — any GitHub account can submit.

We aim to acknowledge within **48 hours** and ship a fix or workaround
within **14 days** for high-severity issues. Lower-severity issues are
triaged on the same schedule but may take longer to resolve.

## Supported versions

We publish releases as SemVer git tags (`vX.Y.Z`) and maintain a moving
major-version alias (`v1`). Security fixes ship as `patch` releases against
the current major only; older majors do not receive back-ported fixes once
a new major is out.

| Version | Supported |
|---------|-----------|
| `v1.x` (`@v1`) | ✅ current major — receives security patches |
| < `v1.0` | ❌ pre-release, unsupported |

## Full security model

For the complete trust model, supply-chain notes, secrets handling,
per-provider egress surfaces, and known accepted risks, see
[`docs/SECURITY.md`](docs/SECURITY.md).

Highlights covered there:

- Runtime trust boundary (composite action runs in the consumer's runner
  with the consumer's tokens).
- Per-provider outbound network surfaces (Anthropic API vs the agent-runner
  CLIs — `claude-code`, `cursor`, `codex`).
- Agent-runner residual exfiltration surface (`GITHUB_TOKEN` persisted by
  `actions/checkout`, vendor API key in the CLI subprocess env) — recommended
  hardening: `persist-credentials: false` + trusted/non-fork PRs only.
- `author-association` gate — public-repo abuse defense, default write-tier
  (`OWNER,MEMBER,COLLABORATOR`), evaluated before any LLM API call.
- Log redaction (`redact_for_log` + `LOG_REDACT_SUBSTRINGS`) and safe path
  resolution (`safe_repo_path`).
- Cursor installer supply-chain note and MCP config persistence caveats.
