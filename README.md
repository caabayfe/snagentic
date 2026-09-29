# snagentic

`snagentic` mirrors ServiceNow instances into git so coding agents such as GitHub
Copilot CLI can:

1. **Understand an instance**: every customized `sys_metadata` record, normalized to
   YAML with scripts as real files, plus a local search and dependency index.
2. **Change it safely**: edit files, review a plan, and write the changes into
   agent-owned update sets on a development instance.
3. **See team activity**: open update sets, who is changing what, and collisions.
4. **Document it**: audience-oriented capability, process, and end-user guides backed
   by generated technical reference, Mermaid diagrams, and change-impact fingerprints.
5. **Operate it**: supported CI/CD API operations, and Playwright recipes for the few
   UI-only tasks.

For a non-technical explanation, product capabilities, use cases, safeguards, and a
ready-to-use demonstration script, see the
[product guide](docs/product-overview.md). For formal requirements, detailed data flows,
security review, governance, and operating guidance, see the
[product handbook](docs/product-handbook.md).

## Install

snagentic ships as a native bundle for macOS and Windows. The bundle does not require
Python, Node.js, or Docker. It includes the Copilot CLI extension and the Playwright UI
runner. Credentials are kept in macOS Keychain or Windows Credential Manager. See
[docs/native-install.md](docs/native-install.md).

```bash
snagentic copilot install     # Copilot CLI extension + ServiceNow agents/skills plugin
snagentic ui install          # Playwright Chromium for UI recipes
snagentic doctor --local      # verify executable, keychain, extension, and UI runtime
```

## Quick start (instance mirror)

```bash
snagentic instance add dev --url https://dev.service-now.com/ --kind development \
  --credential-store keychain
snagentic auth login -i dev              # stored in the OS keychain, never in files
snagentic instance -i dev fetch          # remote state -> servicenow-remote/dev branch
snagentic instance -i dev integrate      # merge into your branch (git conflict markers)
snagentic instance -i dev docs           # instances/dev/docs (mkdocs serve -f ...)
snagentic instance -i dev docs check     # validate references and generated freshness
# edit instances/dev/metadata/..., commit, then:
snagentic instance -i dev plan               # changes, collisions, findings, gate
snagentic instance -i dev apply --plan-id <id> --confirm   # refused while the gate fails
snagentic instance -i dev scan --confirm      # ServiceNow Instance Scan of the update sets
snagentic instance -i dev promote --confirm   # complete update sets + manifest
```

Other commands: `status`, `review`, `review-record`, `scan-results`, `update-sets`, `collisions`, `activity`, `index`, `search`,
`refs`, `ops-list`, `ops-run`, `complete`, `ui list`, and `ui run`. Contributors can also run them through Docker with
`docker compose run --rm cli instance ...`. See [docs/architecture.md](docs/architecture.md)
for the design and [docs/copilot-cli.md](docs/copilot-cli.md) for the agent tools.
The ServiceNow architect and reviewer agents, skills, review rules, waivers, the apply
gate and Instance Scan are described in
[docs/servicenow-standards.md](docs/servicenow-standards.md).

The safety model is intentionally asymmetric:

- Development instances may support guarded pull and push.
- Test and production instances are read-only to this tool; reviewed packages are
  promoted through supported ServiceNow deployment mechanisms.
- Credentials, encrypted values, business records, attachments, journal content, and
  unrestricted logs are never source-controlled.

## Repository areas

- `src/snagentic/instance/`: multi-instance Table API mirror, update sets, index, docs,
  CI/CD operations, and UI recipe runner (`snagentic instance ...`).
- `instances/<name>/`: per-instance profile, mirrored metadata (every `sys_metadata`
  class, out of box included, plus read-only plugin, application-repository, and domain
  inventory, with code exploded to `.js`/`.html`/`.css` files and read-only `_children/`
  such as flow logic), the derived table model
  (`model/tables/<table>.yaml`), update sets, and docs including functional behaviour
  pages per table. Agents start with `snagentic instance -i <name> table <table>`.
- `instances/<name>/documentation/`: durable, reviewed capability, process, and user-guide
  source. `instances/<name>/docs/` is generated output and includes the technical reference.
- `ui/`: Playwright runner and recipes. The runner is bundled with a private Node.js
  runtime; `SNAGENTIC_UI_RUNNER=docker` selects the Compose `ui` service.
- `packaging/`: native bundle build (`build_native.py`), plus Homebrew and WinGet
  templates.
- `docs/features/incident-task-creation.md`: feature design and end-user process for
  creating Incident Tasks from classic Incident forms.
- `docs/acceptance-test-plan.md`: evidence-based end-to-end acceptance criteria,
  execution order, cleanup controls, and sign-off matrix.
- `docs/documentation-authoring.md`: content model, examples, evidence links, review rules,
  and local/CI documentation workflow.
- `.github/extensions/snagentic/`: Copilot CLI tools.
- `copilot-plugin/`: Copilot CLI plugin with the ServiceNow architect and reviewer agents,
  the `servicenow-*` skills, and the `preToolUse` apply-gate hook.
- `config/`: safe example instance profiles.
- `.snagentic/`: local-only state: baselines, mirror work trees, sync state, search
  index, diagnostics, UI evidence, and promotion manifests.

## Development

Run quality gates inside Docker:

```bash
docker compose run --rm lint
docker compose run --rm test
node --test tests/extension tests/ui
```

Create an instance profile with:

```bash
docker compose run --rm cli instance add dev --url https://dev.service-now.com/ --kind development
snagentic auth login -i dev
```

Instances that enforce the ServiceNow basic-auth restriction reject REST basic auth
(`401 User is not authenticated`) unless the user has the `snc_basic_auth_api_access`
role.

Prefer `snagentic auth login`, which stores these values in the OS credential store.
Environment variables named by the selected profile remain supported for CI and
containers; set `auth.store: env` or `SNAGENTIC_CREDENTIAL_STORE=env`. Native bundles
read only the keychain unless you opt in. Do not add credentials to repository files.

Instance profiles name their own variables (`SNAGENTIC_<NAME>_USERNAME`,
`SNAGENTIC_<NAME>_PASSWORD`, or `SNAGENTIC_<NAME>_TOKEN`). UI recipes need a local
(non-SSO) user; set `ui.username_env`/`ui.password_env` when the API uses a bearer token.
If extension environment access is denied, remote commands may use a user-local
`~/.config/snagentic/<instance>.env`; protect it with mode `0600` on macOS and Linux.
`~/.config/snagentic/runtime.env` may contain only the approved `SNAGENTIC_PYTHON`
override and must use the same owner-only permissions. See `docs/copilot-cli.md` for
the exact format and precedence.

Development and CI containers use the reviewed Python versions in
`requirements/constraints.txt`. Regenerate that file from the supported Python 3.14 test
container after reviewing dependency updates. The UI runner uses the committed
`ui/package-lock.json` and `npm ci`.

Security reports must follow [SECURITY.md](SECURITY.md), and supported environments and
diagnostic-sharing rules are defined in [SUPPORT.md](SUPPORT.md). CI performs static
analysis, dependency auditing, coverage enforcement, deterministic documentation checks,
and package-build validation. The default Docker build produces the non-root runtime
image; Compose explicitly selects the development image for mounted repository workflows.
