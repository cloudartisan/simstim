#!/usr/bin/env bash
set -euo pipefail
repo="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$repo"
python3 tools/security-install-gitleaks.py
git config --local core.hooksPath .githooks
printf 'Installed fail-closed pre-commit hook (%s/.githooks/pre-commit).\n' "$repo"
printf 'Verify with: git config --local --get core.hooksPath\n'
