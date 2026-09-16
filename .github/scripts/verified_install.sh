#!/usr/bin/env bash
# verified_install.sh — install a vendor CLI (Cursor Agent or xAI Grok) with an
# optional SHA-256 check on the artefact this step downloads.
#
# Usage: verified_install.sh <cursor|grok> [<version>] [<expected_sha256>]
#
#   cursor, no version   → downloads https://cursor.com/install to a file,
#                          prints its sha256, verifies it when an expected
#                          hash is given, then runs it (the script is stamped
#                          with the current release, so its hash pins a version).
#   cursor, version      → downloads the versioned package directly
#                          (https://downloads.cursor.com/lab/<version>/<os>/<arch>/agent-cli-package.tar.gz),
#                          verifies it when an expected hash is given, and
#                          installs it the way the official installer does.
#                          (The official installer ignores any VERSION env var;
#                          this is the only way to honour a pin.)
#   grok                 → downloads https://x.ai/cli/install.sh to a file,
#                          prints its sha256, verifies it when an expected
#                          hash is given, then runs `bash install.sh [version]`.
#
# Environment:
#   VERIFIED_INSTALL_DRY_RUN=1   download + verify only; never execute/extract.
#   CURSOR_INSTALL_URL / CURSOR_PACKAGE_BASE_URL / GROK_INSTALL_URL
#                                override the artefact locations (tests).
#
# Exit codes: 0 ok · 2 usage · 3 download failed · 4 sha256 mismatch · 5 install failed.
# The downloaded artefact is never printed; only its URL and sha256 are logged.
set -euo pipefail

cli="${1:-}"; version="${2:-}"; expected="${3:-}"
[ -n "$cli" ] || { echo "usage: verified_install.sh <cursor|grok> [version] [sha256]" >&2; exit 2; }
if [ -n "$expected" ] && ! [[ "$expected" =~ ^[0-9a-fA-F]{64}$ ]]; then
  echo "::error::<cli>-installer-sha256 must be a 64-hex SHA-256, got ${#expected} characters" >&2; exit 2
fi
if [ -n "$version" ] && ! [[ "$version" =~ ^[A-Za-z0-9._-]+$ ]]; then
  echo "::error::<cli>-version must match ^[A-Za-z0-9._-]+$ (it is used as a URL and path segment), got ${version@Q}" >&2; exit 2
fi
tmpdir="$(mktemp -d)"; trap 'rm -rf "$tmpdir"' EXIT

lower() { tr '[:upper:]' '[:lower:]'; }
sha_of() {  # GNU coreutils on Linux runners; perl shasum on macOS
  if command -v sha256sum >/dev/null 2>&1; then sha256sum "$1" | awk '{print $1}'
  else shasum -a 256 "$1" | awk '{print $1}'; fi
}
verify() {  # verify <file> <label>
  local actual; actual="$(sha_of "$1")"
  echo "$2 sha256: $actual"
  if [ -z "$expected" ]; then
    echo "::notice::$2 not verified (no expected sha256 configured). Pin it with the value above."
    return 0
  fi
  local want; want="$(printf '%s' "$expected" | lower)"
  if [ "$(printf '%s' "$actual" | lower)" != "$want" ]; then
    echo "::error::$2 sha256 mismatch: expected $want, got $actual. Refusing to run it. Update the pin if the vendor published a new artefact." >&2
    exit 4
  fi
  echo "$2 verified against the configured sha256"
}
fetch() {  # fetch <url> <out>
  echo "artefact: $1"
  curl -fsSL --retry 3 --retry-delay 2 -o "$2" "$1" || { echo "::error::download failed: $1" >&2; exit 3; }
}

case "$cli" in
  cursor)
    if [ -n "$version" ]; then
      os="$(uname -s)"; arch="$(uname -m)"
      case "$os" in Linux*) os=linux;; Darwin*) os=darwin;; *) echo "::error::unsupported OS $os" >&2; exit 5;; esac
      case "$arch" in x86_64|amd64) arch=x64;; arm64|aarch64) arch=arm64;; *) echo "::error::unsupported arch $arch" >&2; exit 5;; esac
      base="${CURSOR_PACKAGE_BASE_URL:-https://downloads.cursor.com/lab}"
      pkg="$tmpdir/agent-cli-package.tar.gz"
      fetch "${base}/${version}/${os}/${arch}/agent-cli-package.tar.gz" "$pkg"
      verify "$pkg" "cursor-agent ${version} package"
      [ "${VERIFIED_INSTALL_DRY_RUN:-}" = "1" ] && { echo "dry run: not installing"; exit 0; }
      dest="$HOME/.local/share/cursor-agent/versions/${version}"
      rm -rf "$dest"; mkdir -p "$dest" "$HOME/.local/bin"
      tar --strip-components=1 -xzf "$pkg" -C "$dest" || { echo "::error::could not extract the cursor-agent package" >&2; exit 5; }
      rm -f "$HOME/.local/bin/agent" "$HOME/.local/bin/cursor-agent"
      ln -s "$dest/cursor-agent" "$HOME/.local/bin/agent"; ln -s "$dest/cursor-agent" "$HOME/.local/bin/cursor-agent"
    else
      script="$tmpdir/cursor-install.sh"
      fetch "${CURSOR_INSTALL_URL:-https://cursor.com/install}" "$script"
      verify "$script" "cursor installer script"
      [ "${VERIFIED_INSTALL_DRY_RUN:-}" = "1" ] && { echo "dry run: not installing"; exit 0; }
      bash "$script" || { echo "::error::cursor installer failed" >&2; exit 5; }
    fi
    ;;
  grok)
    script="$tmpdir/grok-install.sh"
    fetch "${GROK_INSTALL_URL:-https://x.ai/cli/install.sh}" "$script"
    verify "$script" "grok installer script"
    [ "${VERIFIED_INSTALL_DRY_RUN:-}" = "1" ] && { echo "dry run: not installing"; exit 0; }
    if [ -n "$version" ]; then bash "$script" "$version"; else bash "$script"; fi || { echo "::error::grok installer failed" >&2; exit 5; }
    ;;
  *) echo "usage: verified_install.sh <cursor|grok> [version] [sha256]" >&2; exit 2;;
esac
