#!/usr/bin/env bash
set -euo pipefail
# No pathname expansion: matched values are split into words below and must
# never glob against the working directory.
set -f

# Public-hygiene check — DeepWorkPlan ecosystem public repository standard
# (amendment A3, S3).
#
# Fails when a tracked file carries content a public repository must not:
# personal absolute paths, the private organization or private repository
# names, internal tooling and mesh names, @dailybot.com addresses other than
# the public role aliases, or secret-shaped strings. Bash + grep only, no
# network, bash 3.2 compatible (macOS default shell).
#
# Scope: `git ls-files` of the repository (or of $1), excluding the vendored
# skill copies under .agents/skills/ (pinned, owned upstream) and this
# script. Binary files are skipped.
#
# Allowlist: .public-hygiene-allow — one entry per line,
#   <tracked path><whitespace><reason>
# An allowlisted file may carry name-rule hits (the reason says why). A
# secret-shaped hit is accepted only when the file is allowlisted AND every
# matched value carries an obviously-fake marker as its own token (fake,
# test, planted or example, not preceded by a letter — so `latest` or
# `attestation` do not count) — a real-looking secret never passes,
# allowlisted or not. Findings print the matched name only, never the line,
# and secret values are never printed.
#
# `/home/runner/` is the GitHub-hosted runner's workspace, not a person's
# home directory, so it is not a personal-path hit. A quoted-assignment whose
# value is an UPPER_SNAKE environment-variable name (`api_key_env =
# "OPENAI_API_KEY"`) or a `${...}` interpolation names a secret, it does not
# carry one, so it is not a hit either.
#
# Usage: scripts/check-public-hygiene.sh [repo_root]
# Exit:  0 clean · 1 findings · 2 usage/environment error

ROOT="${1:-$(cd "$(dirname "$0")/.." && pwd)}"
if ! git -C "$ROOT" rev-parse --is-inside-work-tree >/dev/null 2>&1; then
    echo "ERROR: $ROOT is not a git work tree" >&2
    exit 2
fi
ROOT="$(cd "$ROOT" && pwd)"
ALLOW="$ROOT/.public-hygiene-allow"

# label<TAB>extended regex. Bracketed letters keep these patterns from
# matching this file's own text when the file is scanned elsewhere.
RULES=""
read -r -d '' RULES <<'EOF' || true
personal-path	(/Users/[A-Za-z][A-Za-z0-9._-]+|/home/[a-z][a-z0-9._-]+/)
private-org	DailyBot-[I]nc
private-repo	(dailybot-[c]ore|coding-agent-host-[k]it|dailybot-private-[s]kills|api-[s]ervices|chatbot-[f]unctions|discord-[g]ateway|msteams-app-[m]anifesto|labs-[p]rojects)
internal-tooling	((^|[^A-Za-z0-9_-])db[d]ev([^A-Za-z0-9_-]|$)|dailybot-[d]ev([^A-Za-z0-9_-]|$)|dailybot-[p]eers|dailybot-[w]orkspaces|dailybot-[w]s-|\[dailybot-[m]esh\])
private-email	[A-Za-z0-9._%+-]+@dailybot\.[c]om
EOF
SECRETS=""
read -r -d '' SECRETS <<'EOF' || true
aws-key	AKI[A][0-9A-Z]{16}
github-token	(gh[p]_[A-Za-z0-9]{36}|github_pa[t]_[A-Za-z0-9_]{40,})
openai-anthropic	(s[k]-ant-[A-Za-z0-9_-]{20,}|s[k]-[A-Za-z0-9]{32,})
slack-token	xo[x][baprs]-[A-Za-z0-9-]{10,}
google-key	AI[z]a[0-9A-Za-z_-]{35}
private-key	-----BEGIN [A-Z ]*PRIVATE [K]EY-----
quoted-assignment	(api[_-]?key|secret|token|passw(or)?d)[A-Za-z_]*["']?[[:space:]]*[:=][[:space:]]*["'][^"'[:space:]]{16,}["']
EOF
PUBLIC_ALIASES='^(security|support|ops|conduct)@dailybot\.com$'
FAKE_MARKERS='(^|[^A-Za-z])(fake|test|planted|example)'

WORK="$(mktemp -d "${TMPDIR:-/tmp}/public-hygiene.XXXXXX")"
trap 'rm -rf -- "$WORK"' EXIT

# Allowlisted paths, one per line.
if [ -f "$ALLOW" ]; then
    grep -vE '^[[:space:]]*(#|$)' "$ALLOW" | awk '{print $1}' > "$WORK/allow" || true
else
    : > "$WORK/allow"
fi
allowlisted() {  # $1 = repo-relative path
    grep -qxF -- "$1" "$WORK/allow"
}

git -C "$ROOT" ls-files \
    | grep -vE '^\.agents/skills/' \
    | grep -vxF 'scripts/check-public-hygiene.sh' > "$WORK/files" || true
scanned="$(wc -l < "$WORK/files" | tr -d ' ')"

# grep_all <pattern> — prints path:line:content for every hit (text files only).
grep_all() {
    (cd "$ROOT" && tr '\n' '\0' < "$WORK/files" | xargs -0 grep -nHIE -- "$1" 2>/dev/null) || true
}

# keep_line <label> <content> — 0 when the hit still counts after the
# rule-specific exemptions (role aliases, the runner workspace).
keep_line() {
    case "$1" in
        private-email)
            for addr in $(printf '%s' "$2" | grep -oE '[A-Za-z0-9._%+-]+@dailybot\.[c]om'); do
                printf '%s' "$addr" | grep -qE "$PUBLIC_ALIASES" || return 0
            done
            return 1 ;;
        personal-path)
            for p in $(printf '%s' "$2" | grep -oE '(/Users/[A-Za-z][A-Za-z0-9._-]+|/home/[a-z][a-z0-9._-]+/)'); do
                [ "$p" = "/home/runner/" ] || return 0
            done
            return 1 ;;
    esac
    return 0
}

# secret_counts <label> <pattern> <content> — 0 when a secret-shaped hit
# still counts.
secret_counts() {
    [ "$1" = quoted-assignment ] || return 0
    for v in $(printf '%s' "$3" | grep -oE -- "$2" | sed -E "s/^.*[:=][[:space:]]*[\"']//; s/[\"']$//"); do
        printf '%s' "$v" | grep -qE '^([A-Z][A-Z0-9_]+|\$\{.*)$' || return 0
    done
    return 1
}

# all_fake <pattern> <content> — 0 when every matched value carries a
# fake marker token.
all_fake() {
    printf '%s\n' "$2" | grep -oE -- "$1" | while IFS= read -r m; do
        # For an assignment, only the quoted value can vouch for itself.
        v="$(printf '%s' "$m" | sed -E "s/^.*[:=][[:space:]]*[\"']//")"
        printf '%s' "$v" | grep -qiE "$FAKE_MARKERS" || { echo real; break; }
    done | grep -q real && return 1
    return 0
}

: > "$WORK/out"
while IFS="$(printf '\t')" read -r label pattern; do
    [ -n "$label" ] || continue
    grep_all "$pattern" | while IFS= read -r hit; do
        rel="${hit%%:*}"; rest="${hit#*:}"; lineno="${rest%%:*}"; content="${rest#*:}"
        keep_line "$label" "$content" || continue
        allowlisted "$rel" && continue
        names="$(printf '%s' "$content" | grep -oE -- "$pattern" | sort -u | tr '\n' ' ')"
        printf 'FAIL [%s] %s:%s: %s\n' "$label" "$rel" "$lineno" "${names% }"
    done >> "$WORK/out"
done <<EOF_RULES
$RULES
EOF_RULES

while IFS="$(printf '\t')" read -r label pattern; do
    [ -n "$label" ] || continue
    grep_all "$pattern" | while IFS= read -r hit; do
        rel="${hit%%:*}"; rest="${hit#*:}"; lineno="${rest%%:*}"; content="${rest#*:}"
        secret_counts "$label" "$pattern" "$content" || continue
        if allowlisted "$rel" && all_fake "$pattern" "$content"; then
            continue
        fi
        printf 'FAIL [%s] %s:%s: (value not printed)\n' "$label" "$rel" "$lineno"
    done >> "$WORK/out"
done <<EOF_SECRETS
$SECRETS
EOF_SECRETS

findings="$(wc -l < "$WORK/out" | tr -d ' ')"
if [ "$findings" -gt 0 ]; then
    cat "$WORK/out"
    echo "public hygiene: $findings finding(s) — fix them, or list an obviously fake fixture in .public-hygiene-allow with a reason"
    exit 1
fi
echo "OK: public hygiene ($scanned tracked files scanned)"
