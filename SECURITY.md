# Security Policy

## Reporting a vulnerability

**Please do not open a public GitHub issue, discussion or pull request for a
vulnerability.** Report it privately through either channel:

- **GitHub private vulnerability reporting (preferred)** —
  **→ [Open a private advisory](https://github.com/DailybotHQ/ai-diff-reviewer/security/advisories/new)**
  (`github.com/DailybotHQ/ai-diff-reviewer/security/advisories/new`). Any
  GitHub account can submit; the advisory is visible only to the maintainers
  and lets us collaborate on the fix before a coordinated disclosure.
- **Email** — `security@dailybot.com`. Include the affected version, a
  description of the issue and reproduction steps. Never include live
  credentials; a redacted value is enough.

## Response targets

| Stage | Target |
|---|---|
| Acknowledge the report | within **48 hours** |
| Initial assessment and severity | within **5 business days** |
| Fix or workaround for high/critical severity | within **14 days** |
| Lower severity | triaged on the same schedule; may take longer to resolve |

We credit reporters in the release notes unless you ask us not to.

## Supported versions

We publish releases as SemVer git tags (`vX.Y.Z`) with a moving major alias
for each maintained line. Security fixes ship as `patch` releases.

| Version | Supported |
|---------|-----------|
| `v3.x` (`@v3`) | ✅ current major — receives security patches |
| `v2.x` (`@v2`) | ⚠️ frozen line maintained from `release/v2` for six months after v3.0.0 (see [docs/MIGRATION_v3.md](docs/MIGRATION_v3.md)) — security and catalog fixes |
| < `v2.0` | ❌ unsupported — upgrade to `@v3` |

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
