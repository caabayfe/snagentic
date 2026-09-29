# Native installation (no Docker)

The native snagentic bundle can be installed on macOS and Windows without Python, Node.js,
or Docker. It includes:

- the `snagentic` command, built with PyInstaller as a one-directory application;
- the user-scope GitHub Copilot CLI extension;
- the Playwright UI runner and its production `node_modules`;
- a private Node.js runtime that only the UI runner uses.

Playwright browsers are downloaded into your user cache by `snagentic ui install`. They
are not included in the bundle.

Credentials are stored in the operating system credential store: macOS Keychain or
Windows Credential Manager. They are not read from shell variables or `.env` files.

## Install

| Platform | Package | Command |
| --- | --- | --- |
| macOS (Apple silicon, Intel) | Homebrew cask | `brew install --cask <owner>/snagentic/snagentic` |
| Windows x64 | WinGet | `winget install <Publisher>.snagentic` |
| Either | Release archive | Extract `snagentic-<version>-<target>.(tar.gz\|zip)` and add the `snagentic/` folder to `PATH` |

Before extracting a release archive, check it against the `SHA256SUMS` file. Release
builds are signed. macOS builds are also notarized, and Windows builds are Authenticode
signed. The package templates are in `packaging/homebrew/` and `packaging/winget/`.

### Homebrew

```bash
brew tap <owner>/snagentic            # the <owner>/homebrew-snagentic repository
brew trust <owner>/snagentic          # Homebrew 7 asks you to trust third-party taps
brew install --cask snagentic
snagentic copilot install             # once, and again after every `brew upgrade`
```

The cask links `$(brew --prefix)/bin/snagentic`. `snagentic copilot install` records
that stable link, not the versioned Caskroom folder, so the extension and the plugin's
apply hook keep working after `brew upgrade`. The cask has no install scripts, because
Homebrew 7 runs them in a sandbox that cannot reach `~/.copilot`.

To remove everything, run `snagentic copilot uninstall` first, then
`brew uninstall --cask snagentic`. Add `--zap` to also delete the caches and the
Copilot files.

#### Publishing the cask

Maintainers need the following once:

1. **Release repository.** This repository on GitHub, with **public** release
   downloads. Homebrew cannot download assets from private repositories. For an
   internal-only tool, host the archives on an HTTPS artifact server and render the
   cask with `--url-base`.
2. **Tap repository** `<owner>/homebrew-snagentic`, containing a `Casks/` folder.
3. **Apple Developer ID Application certificate and notarization.** Configure the
   `MACOS_*` and `APPLE_*` secrets in the `release` environment. Without them the
   workflow refuses to publish, because macOS Gatekeeper blocks quarantined unsigned
   binaries that were downloaded by Homebrew.
4. **Repository variable** `HOMEBREW_TAP` (for example `acme/homebrew-snagentic`), plus
   the `release` environment secret `HOMEBREW_TAP_TOKEN`. The token must be a
   fine-grained token with contents write access to the tap only.

On a `v*` tag, the release workflow builds and notarizes the arm64 and x64 bundles and
publishes them with `SHA256SUMS`. It then renders `snagentic.rb` with
`packaging/homebrew/render_cask.py`, attaches it to the release and commits it to the
tap.

To test a cask locally without publishing anything:

```bash
python packaging/build_native.py
brew tap-new --no-git local/snagentic-test
python packaging/homebrew/render_cask.py --sums dist/native --partial \
  --url-base "file://$PWD/dist/native" --allow-file-url \
  --homepage https://example.com \
  --output "$(brew --repository)/Library/Taps/local/homebrew-snagentic-test/Casks/snagentic.rb"
brew style --cask local/snagentic-test/snagentic
brew audit --cask --strict local/snagentic-test/snagentic
brew install --cask local/snagentic-test/snagentic
```

Behind a TLS-inspecting proxy, `brew style` cannot download its gems because Homebrew
drops `SSL_CERT_FILE`.

## First-time setup

```bash
snagentic --version
snagentic doctor --local            # executable, keychain backend, extension, UI runtime
snagentic copilot install           # installs ~/.copilot/extensions/snagentic
snagentic ui install                # downloads Playwright Chromium (UI recipes only)
```

Run the following commands inside the Git repository that holds your ServiceNow mirrors:

```bash
snagentic instance add dev --url https://dev.service-now.com/ --kind development \
  --credential-store keychain
snagentic auth login -i dev          # prompts; the password is not echoed
snagentic auth status -i dev         # shows only whether each value is stored
```

For an instance profile, the keychain service is `snagentic`, and the account is
`<instance host>/<VARIABLE NAME>`. For example:
`dev.service-now.com/SNAGENTIC_DEV_PASSWORD`. Because the host is part of the account
name, a profile that is pointed at a different host cannot read those credentials.

Run `snagentic auth logout -i dev` to remove the stored values.

## Where credentials come from

`auth.store` in `instance.yaml` controls where credentials come from:

| `auth.store` | Behaviour |
| --- | --- |
| `keychain` | Uses only the OS credential store. |
| `env` | Uses only environment variables (CI and containers). |
| `auto` (default) | Source checkout and wheel: keychain first, then environment. Native bundle: keychain only. |

`SNAGENTIC_CREDENTIAL_STORE=keychain|env` changes only an `auto` profile. It is the
explicit opt-in for environment credentials in a native bundle, such as on a CI runner.
If no supported secure backend is available, snagentic stops with an error. It never
falls back to a plaintext file.

Credentials are retrieved in-process immediately before the HTTP client is created. For
UI recipes, they are passed to the Node runner over its private stdin pipe. They are not
put in command arguments, the child-process environment, `request.json`, traces,
screenshots, or results.

## Copilot CLI extension

`snagentic copilot install` copies the extension into
`$COPILOT_HOME/extensions/snagentic` (default `~/.copilot`). It also writes
`snagentic-runtime.json`, which records the executable path, version, protocol version,
and file digest. The file contains no secrets.

The extension chooses a runtime in this order:

1. `SNAGENTIC_RUNTIME=native|python|docker`, if set;
2. `SNAGENTIC_PYTHON`, the developer override;
3. the native executable, from `SNAGENTIC_EXECUTABLE`, the install marker, or
   `snagentic` on `PATH`;
4. Docker Compose, as the contributor fallback.

In native mode, the extension does not request ServiceNow credential variables from
Copilot, and it does not read `~/.config/snagentic/*.env`. The installed CLI reads the
keychain itself. If you set `SNAGENTIC_CREDENTIAL_STORE=env`, the extension requests
and forwards the credential variables as it does in the legacy modes.

The same command installs the `snagentic-servicenow` Copilot plugin: the ServiceNow
architect and reviewer agents, the `servicenow-*` skills and the apply-gate hook. It is
staged as a local marketplace in `$COPILOT_HOME/snagentic-marketplace` and registered
with `copilot plugin`. Pass `--no-plugin` to skip it. See
[ServiceNow standards](servicenow-standards.md#copilot-plugin-agents-skills-and-hook).

After you upgrade snagentic, run `snagentic copilot install` again. `snagentic copilot
status` reports `current: false` when the extension files or version differ from the
installed CLI. It also reports `shadowed_by_project_extension` when the repository's
`.github/extensions/snagentic` extension takes precedence. `snagentic copilot uninstall`
removes only a directory that snagentic installed.

## UI recipes without Docker

`snagentic instance -i dev ui run <recipe> --confirm` runs the bundled Node runner with
Playwright Chromium from `snagentic ui install`. Check readiness with
`snagentic ui status`. Contributors can still set `SNAGENTIC_UI_RUNNER=docker` to use the
Compose `ui` service. `SNAGENTIC_NODE` selects a specific Node executable.

## Migrating from environment variables and `.env` files

1. Install the native bundle, then run `snagentic copilot install`.
2. For each instance, run `snagentic auth login -i <name> --from-env` in a shell that
   still has the old variables. This copies them into the keychain.
3. Run `snagentic auth status -i <name>` and check that `keychain: true` is shown for each
   value.
4. Remove the variables from your shell profiles, and delete
   `~/.config/snagentic/<name>.env` yourself. snagentic does not delete secrets it did
   not create.
5. Optionally, set `auth.store: keychain` in `instances/<name>/instance.yaml` so that
   environment variables are never used for that instance.

## Building locally

```bash
python -m pip install -c requirements/constraints.txt -e ".[native]"
python packaging/build_native.py            # dist/native/snagentic + archive + .sha256
```

The build uses `node` from `PATH`, or the executable passed with `--node`. It installs the
UI runner's production dependencies with `npm ci --omit=dev`. Local builds are unsigned.
`.github/workflows/native-release.yml` builds each target on its native runner and
smoke-tests the bundle outside the checkout with a stripped `PATH`, a real
credential-store round trip, and a Chromium install. On `v*` tags, it signs, notarizes,
and publishes the bundles. It refuses to publish if signing secrets are missing.
