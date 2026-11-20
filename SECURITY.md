# Security policy

## Reporting a vulnerability

Please do not report suspected vulnerabilities in public issues or pull requests. Use GitHub's **Report a vulnerability** flow on this repository's Security tab when private vulnerability reporting is available. If that flow is unavailable, contact the repository maintainers privately through an established, non-public channel and request a secure reporting route; do not include exploit details in a public post.

Include affected paths/versions, impact, reproduction steps and any suggested mitigation. Do not include live credentials or personal data. We will acknowledge receipt, coordinate remediation and disclosure with the reporter, and publish a fix/advisory when it is safe to do so. Response times depend on maintainer availability; no service-level guarantee is implied.

## Secret exposure

If a credential may have been committed, revoke or rotate it immediately. Removing it from the current tree does not invalidate copies in Git history, clones, logs or caches. Notify maintainers privately so the exposure can be assessed and history-remediation coordinated. Do not put the credential itself in an issue, pull request, test fixture or scan report.

The repository runs Gitleaks against available commit history and current files in CI and provides a staged-file pre-commit check. These controls reduce risk but do not guarantee that all secrets or vulnerabilities will be detected.

## Scope

This policy covers the SIMSTIM source, host software, firmware, build/test tooling and deployment configuration maintained in this repository. It does not imply that a particular device, third-party service, vendor firmware or deployment has been audited or is secure.
