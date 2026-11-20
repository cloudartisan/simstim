#!/usr/bin/env bash
set -euo pipefail
repo="$(git rev-parse --show-toplevel 2>/dev/null)" || {
  echo "security scan: run from inside the repository" >&2
  exit 2
}
cd "$repo"
mode="${1:-all}"
if [[ "$#" -gt 1 || ( "$mode" != all && "$mode" != staged && "$mode" != history ) ]]; then
  echo "usage: tools/security-scan.sh [all|staged|history]" >&2
  exit 2
fi

git_dir="$(git rev-parse --git-path simstim-security/gitleaks)"
scanner="$(cd "$(dirname "$git_dir")" && pwd)/$(basename "$git_dir")"
if [[ ! -x "$scanner" ]]; then
  echo "security scan: pinned Gitleaks missing; run python3 tools/security-install-gitleaks.py" >&2
  exit 2
fi
version="$("$scanner" version 2>/dev/null)" || {
  echo "security scan: unable to run pinned Gitleaks" >&2
  exit 2
}
if [[ "$version" != "8.30.0" ]]; then
  echo "security scan: expected Gitleaks 8.30.0, found ${version:-unknown}" >&2
  exit 2
fi

scan_history() {
  if git rev-parse --verify HEAD >/dev/null 2>&1; then
    "$scanner" git --no-banner --redact --log-opts=--all .
  else
    echo "security scan: no commits yet; history scan skipped (working tree and index are still scanned)"
  fi
}
scan_worktree() {
  "$scanner" dir --no-banner --redact .
}
scan_staged() {
  "$scanner" git --no-banner --redact --staged .
}
case "$mode" in
  all)
    scan_history
    scan_worktree
    scan_staged
    ;;
  staged)
    scan_staged
    ;;
  history)
    scan_history
    ;;
esac
