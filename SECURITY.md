# Security policy

## Supported versions

Until the first stable release, security fixes are provided on the latest commit of the
default branch only. Released `0.x` versions may contain breaking security changes.

## Reporting a vulnerability

Do not open a public issue for a suspected vulnerability. Use the repository's private
vulnerability reporting feature in GitHub Security when available, or contact the
maintainers through an approved private organizational channel.

Include the affected command or component, reproduction steps, impact, and any relevant
sanitized logs. Never include ServiceNow credentials, tokens, business records, or raw
diagnostic bundles.

The maintainers should acknowledge a report within three business days, provide an
initial assessment within seven business days, and coordinate disclosure after a fix is
available.

## Security boundaries

- Configuration files contain environment-variable names, never credential values.
- Interactive credentials are stored in the OS credential store: macOS Keychain or
  Windows Credential Manager. Each entry is keyed by instance host and variable name.
  snagentic never falls back to plaintext files. Native bundles ignore credential
  environment variables unless you explicitly opt in. When a UI recipe runs, the
  credentials are passed to the runner over stdin only.
- Legacy environment and `.env` credentials belong outside the repository and must be
  owner-only on supported Unix platforms.
- Native release artifacts are signed. macOS artifacts are also notarized. Signing
  secrets are used only for `v*` tag builds, and a release fails rather than publishing
  unsigned artifacts.
- Writes are allowed only to configured development instances and require explicit
  confirmation.
- Test and production promotion uses the supported ServiceNow deployment process; this
  project does not write directly to those environments.
- `.snagentic/` contains sensitive local operational evidence and must not be committed.

See `docs/architecture.md` and `docs/copilot-cli.md` for the detailed trust and execution
model.
