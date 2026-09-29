# Changelog

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

- `instance plan` could miss local edits on macOS when Git's fsmonitor was enabled;
  snagentic now disables fsmonitor for its own Git calls.
- CI containers run as the runner user so hardened containers can write the checkout.

## 0.1.0

- Initial release: instance mirror workflow, native CLI with OS credential storage,
  user-scope Copilot extension and plugin with ServiceNow guardrails, Docker-free
  Playwright UI recipes and Homebrew cask packaging.
