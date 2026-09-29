# Copilot CLI integration

The project extension at `.github/extensions/snagentic/extension.mjs` exposes
repository-scoped tools for the instance mirror workflow.

### Instance tools

`.github/extensions/snagentic/instance.mjs` adds tools for the multi-instance mirror
(`snagentic instance ...`). Each accepts an optional `instance` (the folder name under
`instances/`); tools that change ServiceNow require it.

| Tool | Purpose | Writes |
| --- | --- | --- |
| `snagentic_instance_list` | List configured instances, kinds, and mirror branches | No |
| `snagentic_instance_status` | Mirror commit, integration state, local changes | No |
| `snagentic_instance_fetch` | Refresh `servicenow-remote/<name>` from the Table API | Local branch |
| `snagentic_instance_integrate` | Merge the mirror into the current branch (asks) | Local working tree |
| `snagentic_instance_plan` | Plan local metadata edits; returns `planId` and collisions | No |
| `snagentic_instance_review` | ServiceNow best-practice findings and the apply gate for local changes, or findings for mirrored records | No |
| `snagentic_instance_review_record` | Record an approve/reject verdict bound to a `planId` | Local (`reviews/`) |
| `snagentic_instance_apply` | Write a reviewed plan into agent update sets (asks; refused while the standards gate fails) | Remote development |
| `snagentic_instance_update_sets` | Mirrored update sets | No |
| `snagentic_instance_collisions` | Records in more than one open update set | No |
| `snagentic_instance_activity` | Work by user, application, and update set | No |
| `snagentic_instance_index` | Rebuild the search and dependency index | Local |
| `snagentic_instance_table` | Table fields, inheritance and all behaviour (own and inherited) with code paths | No |
| `snagentic_instance_search` | Search indexed metadata | No |
| `snagentic_instance_refs` | Who references a table, script include, event, or property | No |
| `snagentic_instance_docs` | Build/check docs, scaffold sources, or migrate narratives | Local |
| `snagentic_instance_ops_list` | List CI/CD API operations | No |
| `snagentic_instance_ops_run` | Run a CI/CD API operation (asks) | Remote development |
| `snagentic_instance_scan` | ServiceNow Instance Scan (point or suite scan) of agent update sets (asks) | Remote development |
| `snagentic_instance_scan_results` | Findings of existing Instance Scan results | No |
| `snagentic_instance_promote` | Complete agent update sets, write manifest with the last scan (asks) | Remote development |
| `snagentic_instance_ui_list` | List Playwright UI recipes | No |
| `snagentic_instance_ui_run` | Run a UI recipe; `dryRun` shows steps (asks) | Remote development |

Changing tools require `confirm: true`, use the CLI permission prompt, inspect the
instance profile, and are denied unless it declares `kind: development`. The Python
CLI enforces the same policy again. With the native runtime, real UI runs are handled
by one direct `snagentic instance ui run` call. The CLI reads the login from the OS
credential store and passes it to the bundled Node runner over stdin. In Docker mode,
the Python CLI first validates and prepares the run in the `cli` container. The run then
executes in the `ui` (Playwright) container, and only the names of the two UI credential
variables are forwarded.

Every handler uses Node's `spawn()` with an argv array and never invokes a
shell. Arguments are checked both by strict JSON Schema and by runtime
validation, output is bounded on valid UTF-8 boundaries, and each tool returns
an explicit Copilot result type.

`snagentic_instance_docs` accepts `mode: build|check|scaffold|migrate`. Scaffold mode
also requires `type: capability|process|guide` and a stable `id`; `strict: true` runs
MkDocs strict validation for build/check. Curated source lives under
`instances/<name>/documentation/`, while generated pages and the fingerprint manifest
live under `instances/<name>/docs/`. See
[documentation-authoring.md](documentation-authoring.md) for the schema and workflow.

Git-backed instance commands (`fetch`, `integrate`, `status`, `plan`, and `apply`)
explicitly enable Git's local untracked cache and, on supported platforms, built-in
filesystem monitor. These repository-local performance settings are only added when
missing and never replace an existing user choice.

The launcher policy is:

1. `SNAGENTIC_RUNTIME=native|python|docker` forces a runtime when set.
2. If the user explicitly approved and set `SNAGENTIC_PYTHON`, run that interpreter
   directly with `-m snagentic`.
3. Otherwise, run the installed native `snagentic` executable directly. The executable
   is selected from the validated absolute `SNAGENTIC_EXECUTABLE`, then the
   `snagentic-runtime.json` marker written by `snagentic copilot install`, then
   `snagentic` on `PATH`. On Windows, only `snagentic.exe` is accepted.
4. As the contributor fallback, run `docker compose run --rm -T cli` followed by the
   snagentic JSON CLI arguments.
5. Reject malformed `SNAGENTIC_PYTHON` or `SNAGENTIC_EXECUTABLE` values; never interpret
   flags or shell syntax from them.

The same extension files work as the repository extension and as the user-scope
extension installed by `snagentic copilot install` (see
[native-install.md](native-install.md)). A user-scope install uses the Copilot session's
working directory as its workspace. A project extension with the same name takes
precedence over it.

When `SNAGENTIC_PYTHON` is not already set, the extension may read only that
variable from `~/.config/snagentic/runtime.env` (or the equivalent
`$XDG_CONFIG_HOME/snagentic/runtime.env`). The file uses one `KEY=value` or
`export KEY=value` assignment per line; all variables other than
`SNAGENTIC_PYTHON` are ignored. The selected value is still subject to the
normal executable-name/path validation. On macOS and Linux the file must be a
regular file owned by the current user with mode `0600`.

## Credentials

With the native runtime, the extension requests only the non-secret runtime variables
(`SNAGENTIC_EXECUTABLE`, `SNAGENTIC_RUNTIME`, `SNAGENTIC_PYTHON`,
`SNAGENTIC_UI_RUNNER`, `SNAGENTIC_NODE`, and `SNAGENTIC_CREDENTIAL_STORE`). It does not
request ServiceNow credential variables or read `~/.config/snagentic/*.env` files. The CLI
reads credentials from macOS Keychain or Windows Credential Manager, where they are stored
with `snagentic auth login`. If you set `SNAGENTIC_CREDENTIAL_STORE=env`, the credential
handling described below applies in native mode too.

The rest of this section applies to the Docker and direct-Python launchers. In those
modes, the extension also requests only these ServiceNow credential variables:

```text
SNAGENTIC_TOKEN
SNAGENTIC_USERNAME
SNAGENTIC_PASSWORD
SNAGENTIC_DEV_TOKEN
SNAGENTIC_DEV_USERNAME
SNAGENTIC_DEV_PASSWORD
SNAGENTIC_TEST_TOKEN
SNAGENTIC_TEST_USERNAME
SNAGENTIC_TEST_PASSWORD
SNAGENTIC_PROD_TOKEN
SNAGENTIC_PROD_USERNAME
SNAGENTIC_PROD_PASSWORD
```

The extension also requests `SNAGENTIC_PYTHON` for interpreter selection, but
does not pass it into the child environment.

Before every command, a shell-free Python/PyYAML helper safely resolves the
selected configuration inside the repository, reads the selected profile, and
validates each `token_env`, `username_env`, or `password_env` against the
reviewed allowlist above. By default this helper runs inside the same Compose
`cli` image with its entrypoint overridden to `python`, so it does not depend
on host PyYAML. A configuration that names any other credential variable fails
clearly before the CLI runs.

The child environment also preserves standard enterprise connectivity
variables: `HTTP_PROXY`, `HTTPS_PROXY`, `NO_PROXY`, their lowercase variants,
and `SSL_CERT_FILE`. `REQUESTS_CA_BUNDLE` is intentionally excluded because
snagentic uses `httpx`.

For Compose launches, only the selected profile's reviewed credential names
and approved proxy/TLS names are supplied with `-e NAME`. Their values remain
in the spawned Docker process environment and are never placed in argv or
logged. Direct-Python override launches use the same environment allowlist.

Basic authentication is supported by setting a profile to:

```yaml
auth:
  mode: basic
  username_env: SNAGENTIC_DEV_USERNAME
  password_env: SNAGENTIC_DEV_PASSWORD
```

The Docker launcher forwards only those two approved variable names for the
selected profile. Password values are not added to command arguments or
repository files. OAuth bearer authentication remains recommended for
production use.

Instance profiles may also name credentials matching
`SNAGENTIC_<NAME>_TOKEN`, `SNAGENTIC_<NAME>_USERNAME`, or `SNAGENTIC_<NAME>_PASSWORD`.
The extension discovers these names from `instances/*/instance.yaml` at startup and
rejects any other variable name, so a profile cannot request unrelated host secrets.

If the user denies the extension access to requested environment variables, the
extension retries registration without that access. Local inspection and planning
tools remain available instead of failing with an unknown-tool error. Remote instance
commands can still use credentials from `~/.config/snagentic/<instance>.env`; otherwise
they fail explicitly when the selected credential is unavailable.

The per-instance file uses one `KEY=value` or `export KEY=value` assignment per
line. Only credential names declared by that instance profile are read, and an
already-set environment variable wins. On macOS and Linux the file must be owned
by the current user and inaccessible to group and other users:

```bash
mkdir -p ~/.config/snagentic
cat > ~/.config/snagentic/dev.env <<'EOF'
SNAGENTIC_DEV_USERNAME=service-account-name
SNAGENTIC_DEV_PASSWORD=service-account-password
EOF
chmod 600 ~/.config/snagentic/dev.env
```

Apply the same ownership and `0600` mode to
`~/.config/snagentic/runtime.env` when using the direct-Python override.

## Safe change workflow (instance mirror)

1. `snagentic_instance_fetch`, then `snagentic_instance_integrate`; resolve any
   conflict markers and commit.
2. Start with `snagentic_instance_table` for each table involved, then read with
   `snagentic_instance_search`, `snagentic_instance_refs`, and the files under
   `instances/<name>/metadata/` (the `servicenow-capability` prompt walks through this). Check `snagentic_instance_activity` for work in progress.
3. Edit files under `instances/<name>/metadata/` and commit.
4. Run `snagentic_instance_plan`; review changes, collisions and best-practice
   findings with the user (see [ServiceNow standards](servicenow-standards.md)).
   Fix `block` findings or ask a human for a waiver. For significant changes, ask the
   `servicenow-reviewer` agent to review the plan; it records its verdict with
   `snagentic_instance_review_record`.
5. After explicit approval, run `snagentic_instance_apply` with the `planId`,
   `instance`, and `confirm: true`. The standards gate refuses the apply (in Python and
   in the plugin's `preToolUse` hook) while it fails.
6. Run `snagentic_instance_scan` so ServiceNow Instance Scan checks the records in the
   agent update sets, and address its findings.
7. When the feature is done, `snagentic_instance_promote` completes the update sets and
   records the last scan in the manifest; promotion to test/production uses the
   supported deployment process.

## Generated and local-only paths

- `instances/<name>/` holds the instance profile (no secrets), mirrored metadata,
  update sets, durable authored documentation under `documentation/`, and generated
  docs under `docs/`. Its contents are intended for source control.
- `.snagentic/` is ignored local state: baselines, raw payloads, diagnostics,
  locks, synchronization state, mirror work trees, search indexes, UI evidence
  (screenshots and traces), and promotion manifests.

Do not copy raw diagnostic or state data into tracked files without an explicit
redaction review.

## SDK compatibility

The extension uses the current `@github/copilot-sdk/extension` APIs:
`joinSession`, `requestedEnvironmentVariables`, and tool `skipPermission`.
Older Copilot CLI versions may ignore environment requests or permission
settings. In that case the extension should not be used for writes; upgrade
Copilot CLI.

The SDK is supplied by Copilot CLI's extension runtime. No npm dependency or
package manifest entry is required.
