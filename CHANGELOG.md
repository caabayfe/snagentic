# Changelog

## Unreleased

### Added

- A standards-compliant Model Context Protocol (MCP) server
  (`.github/extensions/snagentic/mcp-server.mjs`, run via `snagentic mcp serve`)
  exposing the same instance tool catalogue, JSON Schemas, and safety gates
  (development-only writes, `confirm: true`) as the Copilot CLI extension, over the
  standard MCP stdio transport instead of Copilot's proprietary extension API. Any
  MCP-compatible client can now use it. `snagentic mcp status` and
  `snagentic doctor --local` report its readiness (Node runtime, script, and
  `@modelcontextprotocol/sdk` dependency).
- The Copilot extension's tool catalogue (previously defined in `extension.mjs`) now
  lives in `instance.mjs` as the exported `instanceTools`, shared verbatim between the
  Copilot extension and the new MCP server so both stay behaviourally identical.

### Fixed

- `snagentic mcp serve` now explicitly forwards its own runtime (`SNAGENTIC_EXECUTABLE`
  for a frozen/native binary, `SNAGENTIC_PYTHON` for a venv install) to the Node MCP
  server child process it spawns, unless the caller already set
  `SNAGENTIC_RUNTIME`/`SNAGENTIC_PYTHON`/`SNAGENTIC_EXECUTABLE`. Previously the child
  inherited the parent's environment as-is, so when `PATH` did not resolve `snagentic`
  (e.g. an MCP client launching the native binary by absolute path only) every tool
  call silently fell back to the Docker runtime instead of using the native binary
  itself. The native binary is now fully self-contained with zero Docker/PATH/venv
  dependency.
- `instance integrate` (`snagentic_instance_integrate` over MCP) could report
  `git merge failed` even after the merge had actually committed and advanced the
  current branch (observed when a post-merge git step fails on stray untracked content
  in the working tree). It now checks whether the mirror tip is reachable from `HEAD`
  after a non-zero `git merge` exit and, if so, reports success (`status: "merged"`)
  with the original git diagnostic preserved under a `warning` key, instead of raising
  a misleading failure. The failure message, when a real failure occurs, now includes
  the full `git` output instead of only its last line.
- CI/CD (`sn_cicd`) API failures from `instance ops-run` (e.g. `update_set.create`
  with `confirm:true` gate) surfaced only "unknown error" instead of the actual
  reason. It now also checks `result.status_message`/`result.error`/
  `result.error_message` (the CI/CD API's error shape) when the Table API's
  `error.message`/`error.detail` are absent, so real failures (e.g. a missing
  required parameter) are now visible instead of masked.

## 0.3.0

### Added

- Managed ServiceNow OAuth authentication using client credentials or a pre-provisioned
  refresh token. Access tokens are cached only in memory, renewed before expiry and
  retried once after an API `401`; rotated refresh tokens are saved back to the keychain.
- `install.sh` (macOS) and `install.ps1` (Windows): one-line, per-user installers that
  verify `SHA256SUMS`, work with unsigned builds and run `snagentic copilot install`.
  Each release attaches both scripts.

### Changed

- A version tag without signing secrets publishes an unsigned release, marked in the
  title, instead of failing. The Homebrew cask is still published only for signed
  builds.

## 0.2.0

### Removed (breaking)

- The legacy companion-app engine: the `x_snagentic_source` scoped app under
  `servicenow/app` and the allowlisted pull/push workflow.
- CLI commands `init`, `inventory`, `pull`, `status`, `diff`, `validate`, `push-plan`,
  `push`, `diagnostics`, `query`, `promotion-manifest`, `change-report`,
  `instance migrate` and the top-level `profile`. Use `snagentic instance …` instead.
- Copilot tools `snagentic_pull`, `snagentic_push`, `snagentic_push_plan`,
  `snagentic_query`, `snagentic_inventory`, `snagentic_validate`,
  `snagentic_diagnostics`, `snagentic_status` and `snagentic_diff`. Run
  `snagentic copilot install` after upgrading to refresh the installed extension.

### Changed

- `snagentic doctor` always reports on the local installation (`--local` is accepted).
- Build and bundled runtimes: Python 3.14 (3.13 minimum), Node.js 24 LTS; GitHub
  Actions pinned by SHA to current majors; release runners on macOS 15 and Windows 2025.

### Fixed

- Release `.sha256` files are written with LF line endings on every platform, so
  `shasum -a 256 -c` verifies the Windows archive.
- `instance plan` could miss local edits on macOS when Git's fsmonitor was enabled;
  snagentic now disables fsmonitor for its own Git calls.
- CI containers run as the runner user so hardened containers can write the checkout.

## 0.1.0

- Initial release: instance mirror workflow, native CLI with OS credential storage,
  user-scope Copilot extension and plugin with ServiceNow guardrails, Docker-free
  Playwright UI recipes and Homebrew cask packaging.
