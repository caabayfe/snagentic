# Support policy

## Supported environment

- Python 3.13 or newer; 3.14 is used by the project container image and native builds
- Docker Compose for the supported CLI, validation, and documentation workflows
- Node.js 22 or newer (24 LTS recommended) for direct execution of the Playwright runner
- ServiceNow development instances accessible through supported Table and CI/CD APIs

The latest default-branch revision is the supported pre-1.0 release line. Compatibility
with ServiceNow releases and optional plugins must be established by the acceptance
tests in `docs/acceptance-test-plan.md`.

## Getting help

Open an issue for reproducible non-security defects. Include the command, sanitized
output, platform versions, and whether execution used Docker or the approved direct
Python override.

Share only bounded, redacted local evidence from `.snagentic/` or UI traces. Review
every artifact before sharing it and never attach credentials, personal data, business
records, or the contents of local credential files.

Security concerns must follow `SECURITY.md`.
