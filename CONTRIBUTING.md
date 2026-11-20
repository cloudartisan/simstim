# Contributing

## Before opening a pull request

1. Keep credentials, device-specific keys, Wi-Fi details, local configuration and generated firmware/build output out of Git. The `.gitignore` is a safety net, not a secret store.
2. Install the pinned Gitleaks scanner and repository pre-commit hook:

   ```sh
   tools/security-install-hooks.sh
   ```

   The installer downloads Gitleaks 8.30.0 and verifies the official release archive's SHA-256 before installing it under `.git/`. The hook fails closed if the scanner is missing, has a different version, or reports a finding. It scans staged changes only, so unrelated unstaged files are not included.
3. Run the complete local gate from the repository root:

   ```sh
   python3 tools/security-install-gitleaks.py
   tools/security-scan.sh all
   python3 -m pip install --disable-pip-version-check -r ble-fallback/requirements-host.txt
   python3 -m unittest discover -s wireless-control/tests -v
   python3 -m unittest discover -s attention-lab -p 'test_*.py' -v
   python3 -m unittest discover -s k151-standalone/host -v
   python3 -m unittest discover -s ble-fallback/tests -v
   python3 -m unittest discover -s tests -v
   bash ble-fallback/tests/run_firmware_tests.sh
   ```

   `security-scan.sh all` checks all locally available Git history, the working tree, and the index. Run it after staging; the pre-commit hook independently checks the staged diff. CI repeats the history scan on pushes and pull requests, including pull requests from forks, and runs every test suite above.
4. Do not bypass the hook with `git -c core.hooksPath=/dev/null`, `--no-verify`, or equivalent. Fix findings or coordinate with maintainers before proceeding.

No CI job needs repository secrets or write permissions. Pull requests must not trigger device flashing or deployment; those remain separate, explicitly operated workflows.
