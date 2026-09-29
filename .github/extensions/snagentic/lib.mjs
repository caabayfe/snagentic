import { spawn } from "node:child_process";
import { accessSync, constants as fsConstants, lstatSync, readFileSync, statSync } from "node:fs";
import { realpath } from "node:fs/promises";
import path from "node:path";
import { fileURLToPath } from "node:url";

export const EXTENSION_PROTOCOL = 1;
export const EXTENSION_DIRECTORY = path.dirname(fileURLToPath(import.meta.url));
export const RUNTIME_MARKER = "snagentic-runtime.json";

// A repository-local extension (<root>/.github/extensions/snagentic) serves <root>; a
// user-scope extension installed by `snagentic copilot install` serves the Copilot
// session's working directory, which is where extension processes start.
export function resolveWorkspaceRoot(
  extensionDirectory = EXTENSION_DIRECTORY,
  cwd = process.cwd(),
) {
  const parent = path.dirname(extensionDirectory);
  if (path.basename(parent) === "extensions" && path.basename(path.dirname(parent)) === ".github") {
    return path.dirname(path.dirname(parent));
  }
  return cwd;
}

const REPOSITORY_ROOT = resolveWorkspaceRoot();
export const WORKSPACE_ROOT = REPOSITORY_ROOT;

export const APPROVED_CREDENTIAL_ENVIRONMENT_VARIABLES = Object.freeze([
  "SNAGENTIC_TOKEN",
  "SNAGENTIC_USERNAME",
  "SNAGENTIC_PASSWORD",
  "SNAGENTIC_DEV_TOKEN",
  "SNAGENTIC_DEV_USERNAME",
  "SNAGENTIC_DEV_PASSWORD",
  "SNAGENTIC_TEST_TOKEN",
  "SNAGENTIC_TEST_USERNAME",
  "SNAGENTIC_TEST_PASSWORD",
  "SNAGENTIC_PROD_TOKEN",
  "SNAGENTIC_PROD_USERNAME",
  "SNAGENTIC_PROD_PASSWORD",
]);

// Instance profiles (instances/<name>/instance.yaml) may name credentials that follow
// this reviewed convention only; arbitrary host secrets can never be requested.
export const INSTANCE_CREDENTIAL_PATTERN = /^SNAGENTIC_[A-Z0-9_]{1,64}_(TOKEN|USERNAME|PASSWORD)$/u;

export const RUNTIME_ENVIRONMENT_VARIABLES = Object.freeze([
  "SNAGENTIC_EXECUTABLE",
  "SNAGENTIC_RUNTIME",
  "SNAGENTIC_PYTHON",
  "SNAGENTIC_UI_RUNNER",
  "SNAGENTIC_NODE",
  "SNAGENTIC_CREDENTIAL_STORE",
]);

export const REQUESTED_ENVIRONMENT_VARIABLES = Object.freeze([
  ...RUNTIME_ENVIRONMENT_VARIABLES,
  ...APPROVED_CREDENTIAL_ENVIRONMENT_VARIABLES,
]);

// The native CLI reads credentials from the OS credential store itself, so the extension
// never asks Copilot for ServiceNow credential variables in that mode unless the user
// explicitly opted in with SNAGENTIC_CREDENTIAL_STORE=env.
export function usesEnvironmentCredentials(mode, source = process.env) {
  return mode !== "native" || source.SNAGENTIC_CREDENTIAL_STORE?.trim().toLowerCase() === "env";
}

export function requestedEnvironmentVariables(
  mode,
  instanceCredentialNames = [],
  source = process.env,
) {
  if (!usesEnvironmentCredentials(mode, source)) return [...RUNTIME_ENVIRONMENT_VARIABLES];
  return [...new Set([...REQUESTED_ENVIRONMENT_VARIABLES, ...instanceCredentialNames])];
}

export const MAX_OUTPUT_BYTES = 128 * 1024;
const DEFAULT_TIMEOUT_MS = 120_000;
const DIAGNOSTICS_TIMEOUT_MS = 300_000;
const PROXY_TLS_ENVIRONMENT_KEYS = Object.freeze([
  "HTTPS_PROXY",
  "HTTP_PROXY",
  "NO_PROXY",
  "SSL_CERT_FILE",
  "https_proxy",
  "http_proxy",
  "no_proxy",
]);
const HOST_ENVIRONMENT_KEYS = Object.freeze([
  "APPDATA",
  "ComSpec",
  "DOCKER_CERT_PATH",
  "DOCKER_CONTEXT",
  "DOCKER_HOST",
  "DOCKER_TLS_VERIFY",
  "HOME",
  "HOMEDRIVE",
  "HOMEPATH",
  "LANG",
  "LC_ALL",
  "LOCALAPPDATA",
  "LOGNAME",
  "PATH",
  "PATHEXT",
  "PLAYWRIGHT_BROWSERS_PATH",
  "PYTHONPATH",
  "SNAGENTIC_CREDENTIAL_STORE",
  "SNAGENTIC_NODE",
  "SNAGENTIC_UI_RUNNER",
  "SYSTEMROOT",
  "SystemRoot",
  "TEMP",
  "TMP",
  "TMPDIR",
  "USER",
  "USERPROFILE",
  "VIRTUAL_ENV",
  "XDG_CACHE_HOME",
  "XDG_CONFIG_HOME",
  "XDG_DATA_HOME",
  // Linux Secret Service (D-Bus) keyring access for the native CLI.
  "DBUS_SESSION_BUS_ADDRESS",
  "XDG_RUNTIME_DIR",
  "windir",
  ...PROXY_TLS_ENVIRONMENT_KEYS,
]);
const CONFIG_INSPECTION_SCRIPT = String.raw`
import json
import sys
from pathlib import Path

import yaml

try:
    config_path = Path(sys.argv[1])
    requested_environment = sys.argv[2] or None
    raw = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError("configuration must be a mapping")
    environments = raw.get("environments")
    if not isinstance(environments, dict):
        raise ValueError("configuration environments must be a mapping")
    selected = requested_environment or raw.get("default_environment")
    if not isinstance(selected, str) or not selected:
        raise ValueError("configuration must select an environment")
    profile = environments.get(selected)
    if not isinstance(profile, dict):
        raise ValueError(f"unknown environment: {selected}")
    kind = profile.get("kind")
    if kind not in {"development", "test", "production"}:
        raise ValueError(f"environment {selected} has an invalid kind")
    auth = profile.get("auth") or {}
    if not isinstance(auth, dict):
        raise ValueError(f"environment {selected} auth must be a mapping")
    mode = auth.get("mode", "bearer")
    if mode == "bearer":
        credential_names = [auth.get("token_env", "SNAGENTIC_TOKEN")]
    elif mode == "basic":
        credential_names = [auth.get("username_env"), auth.get("password_env")]
    else:
        raise ValueError(f"environment {selected} has an invalid auth mode")
    if any(not isinstance(value, str) or not value for value in credential_names):
        raise ValueError(f"environment {selected} has incomplete {mode} credential variables")
    print(json.dumps({
        "ok": True,
        "environment": selected,
        "kind": kind,
        "credential_names": credential_names,
    }))
except Exception as exc:
    print(json.dumps({"ok": False, "error": str(exc)}), file=sys.stderr)
    raise SystemExit(2)
`;
const COMMON_KEYS = new Set(["config", "environment"]);
const COMMAND_KEYS = Object.freeze({
  inventory: COMMON_KEYS,
  pull: COMMON_KEYS,
  status: COMMON_KEYS,
  diff: COMMON_KEYS,
  validate: COMMON_KEYS,
  "push-plan": COMMON_KEYS,
  push: new Set([...COMMON_KEYS, "confirm"]),
  diagnostics: new Set([...COMMON_KEYS, "minutes", "limit", "domain"]),
  query: new Set([...COMMON_KEYS, "text", "domain", "artifactType"]),
});

function failure(message) {
  return {
    textResultForLlm: stringifyBounded({ ok: false, error: message }),
    resultType: "failure",
  };
}

function rejected(message) {
  return {
    textResultForLlm: stringifyBounded({ ok: false, error: message }),
    resultType: "rejected",
  };
}

function denied(message) {
  return {
    textResultForLlm: stringifyBounded({ ok: false, error: message }),
    resultType: "denied",
  };
}

export function truncateUtf8(value, maxBytes) {
  const buffer = Buffer.from(String(value), "utf8");
  if (buffer.length <= maxBytes) {
    return buffer.toString("utf8");
  }
  let end = Math.max(0, maxBytes);
  while (end > 0 && (buffer[end] & 0xc0) === 0x80) {
    end -= 1;
  }
  return buffer.subarray(0, end).toString("utf8");
}

function stringifyBounded(value) {
  const text = JSON.stringify(value);
  if (Buffer.byteLength(text) <= MAX_OUTPUT_BYTES) {
    return text;
  }
  for (const field of ["outputPrefix", "error"]) {
    if (typeof value?.[field] !== "string") {
      continue;
    }
    let low = 0;
    let high = Buffer.byteLength(value[field]);
    let best = "";
    while (low <= high) {
      const midpoint = Math.floor((low + high) / 2);
      const candidateText = truncateUtf8(value[field], midpoint);
      const removedBytes = Buffer.byteLength(value[field]) - Buffer.byteLength(candidateText);
      const candidate = {
        ...value,
        [field]: candidateText,
        truncated: true,
        ...(field === "outputPrefix"
          ? { omittedBytes: (value.omittedBytes ?? 0) + removedBytes }
          : {}),
      };
      const serialized = JSON.stringify(candidate);
      if (Buffer.byteLength(serialized) <= MAX_OUTPUT_BYTES) {
        best = serialized;
        low = midpoint + 1;
      } else {
        high = midpoint - 1;
      }
    }
    if (best) {
      return best;
    }
  }
  return JSON.stringify({
    ok: value?.ok === true,
    truncated: true,
    error: "snagentic output exceeded the extension result limit",
  });
}

function isPlainObject(value) {
  return value !== null && typeof value === "object" && !Array.isArray(value);
}

function validateOptionalString(value, name, { maxLength, pattern }) {
  if (value === undefined) {
    return undefined;
  }
  if (typeof value !== "string" || value.length === 0 || value.length > maxLength) {
    throw new TypeError(`${name} must be a non-empty string of at most ${maxLength} characters`);
  }
  if (value.includes("\0") || !pattern.test(value)) {
    throw new TypeError(`${name} contains unsupported characters`);
  }
  return value;
}

function sanitizeConfig(value) {
  const config = validateOptionalString(value, "config", {
    maxLength: 240,
    pattern: /^[A-Za-z0-9._/-]+$/,
  });
  if (config === undefined) {
    return undefined;
  }
  if (path.isAbsolute(config) || config.split(/[\\/]/u).includes("..")) {
    throw new TypeError("config must be a repository-relative path without parent traversal");
  }
  return config;
}

function sanitizeEnvironment(value) {
  return validateOptionalString(value, "environment", {
    maxLength: 64,
    pattern: /^[A-Za-z0-9][A-Za-z0-9._-]*$/,
  });
}

function sanitizeLabel(value, name) {
  return validateOptionalString(value, name, {
    maxLength: 128,
    pattern: /^[A-Za-z0-9][A-Za-z0-9._:/-]*$/,
  });
}

function sanitizeInteger(value, name, minimum, maximum, fallback) {
  const candidate = value === undefined ? fallback : value;
  if (!Number.isInteger(candidate) || candidate < minimum || candidate > maximum) {
    throw new TypeError(`${name} must be an integer from ${minimum} through ${maximum}`);
  }
  return candidate;
}

export function sanitizeArguments(command, args) {
  if (!(command in COMMAND_KEYS)) {
    throw new TypeError(`unsupported command: ${command}`);
  }
  if (!isPlainObject(args)) {
    throw new TypeError("tool arguments must be an object");
  }
  const allowedKeys = COMMAND_KEYS[command];
  for (const key of Object.keys(args)) {
    if (!allowedKeys.has(key)) {
      throw new TypeError(`unsupported argument for ${command}: ${key}`);
    }
  }

  const sanitized = {};
  const config = sanitizeConfig(args.config);
  const environment = sanitizeEnvironment(args.environment);
  if (config !== undefined) {
    sanitized.config = config;
  }
  if (environment !== undefined) {
    sanitized.environment = environment;
  }

  if (command === "push") {
    if (environment === undefined) {
      throw new TypeError("push requires an explicit environment");
    }
    if (args.confirm !== true) {
      throw new TypeError("push requires confirm=true");
    }
    sanitized.confirm = true;
  } else if (command === "diagnostics") {
    sanitized.minutes = sanitizeInteger(args.minutes, "minutes", 1, 1_440, 60);
    sanitized.limit = sanitizeInteger(args.limit, "limit", 1, 5_000, 500);
    const domain = sanitizeLabel(args.domain, "domain");
    if (domain !== undefined) {
      sanitized.domain = domain;
    }
  } else if (command === "query") {
    if (typeof args.text !== "string" || args.text.trim().length === 0 || args.text.length > 1_000) {
      throw new TypeError("text must be a non-empty string of at most 1000 characters");
    }
    if (args.text.includes("\0")) {
      throw new TypeError("text contains unsupported characters");
    }
    sanitized.text = args.text;
    const domain = sanitizeLabel(args.domain, "domain");
    const artifactType = sanitizeLabel(args.artifactType, "artifactType");
    if (domain !== undefined) {
      sanitized.domain = domain;
    }
    if (artifactType !== undefined) {
      sanitized.artifactType = artifactType;
    }
  }
  return sanitized;
}

export function buildCliArguments(command, args) {
  const argv = ["--json"];
  if (args.config !== undefined) {
    argv.push("--config", args.config);
  }
  if (args.environment !== undefined) {
    argv.push("--environment", args.environment);
  }
  argv.push(command);
  if (command === "push") {
    argv.push("--approve");
  } else if (command === "diagnostics") {
    argv.push("--minutes", String(args.minutes), "--limit", String(args.limit));
    if (args.domain !== undefined) {
      argv.push("--domain", args.domain);
    }
  } else if (command === "query") {
    argv.push(args.text);
    if (args.domain !== undefined) {
      argv.push("--domain", args.domain);
    }
    if (args.artifactType !== undefined) {
      argv.push("--artifact-type", args.artifactType);
    }
  }
  return argv;
}

export function buildPythonArguments(command, args) {
  return ["-m", "snagentic", ...buildCliArguments(command, args)];
}

// Reads only SNAGENTIC_PYTHON from ~/.config/snagentic/runtime.env (user-local, outside the
// repository) when the variable is not already set. The value is validated on use.
export function applyRuntimeFile(source = process.env) {
  if (typeof source.SNAGENTIC_PYTHON === "string" && source.SNAGENTIC_PYTHON !== "") return source;
  const base = source.XDG_CONFIG_HOME || (source.HOME ? path.join(source.HOME, ".config") : "");
  if (!base) return source;
  const file = path.join(base, "snagentic", "runtime.env");
  let text;
  try {
    const metadata = lstatSync(file);
    if (!metadata.isFile()) {
      throw new TypeError(`runtime configuration path is not a regular file: ${file}`);
    }
    if (process.platform !== "win32") {
      if ((metadata.mode & 0o077) !== 0) {
        throw new TypeError(
          `runtime configuration file must not be accessible by group or others: ${file}`,
        );
      }
      if (typeof process.getuid === "function" && metadata.uid !== process.getuid()) {
        throw new TypeError(
          `runtime configuration file must be owned by the current user: ${file}`,
        );
      }
    }
    text = readFileSync(file, "utf8");
  } catch (error) {
    if (error && typeof error === "object" && error.code === "ENOENT") return source;
    if (error instanceof TypeError) throw error;
    if (error && typeof error === "object" && error.code) throw error;
    return source;
  }
  for (const line of text.split(/\r?\n/u)) {
    const match = /^\s*(?:export\s+)?SNAGENTIC_PYTHON=(.*)$/u.exec(line);
    if (!match) continue;
    let value = match[1].trim();
    if (value.length >= 2 && (value[0] === "'" || value[0] === '"') && value.at(-1) === value[0]) {
      value = value.slice(1, -1);
    }
    if (value) source.SNAGENTIC_PYTHON = value;
  }
  return source;
}

export async function joinSessionWithPermissionFallback(join, options) {
  try {
    return {
      session: await join(options),
      environmentAccessDenied: false,
    };
  } catch (error) {
    const message = error instanceof Error ? error.message : String(error);
    if (!message.includes("was denied permission access and will not be loaded")) {
      throw error;
    }
    const { requestedEnvironmentVariables: _requested, ...fallbackOptions } = options;
    return {
      session: await join(fallbackOptions),
      environmentAccessDenied: true,
    };
  }
}

export function selectPythonOverride(source = process.env) {
  const configured = source.SNAGENTIC_PYTHON;
  if (configured === undefined || configured === "") {
    return undefined;
  }
  if (
    typeof configured !== "string" ||
    configured.length > 512 ||
    configured.includes("\0") ||
    (!path.isAbsolute(configured) && !/^[A-Za-z0-9._+-]+$/u.test(configured))
  ) {
    throw new TypeError(
      "SNAGENTIC_PYTHON must be an absolute executable path or a simple executable name",
    );
  }
  return configured;
}

// Reads the marker written by `snagentic copilot install` next to this extension and
// exposes its executable as SNAGENTIC_EXECUTABLE unless the user already set one.
export function applyRuntimeMarker(source = process.env, directory = EXTENSION_DIRECTORY) {
  let marker;
  try {
    marker = JSON.parse(readFileSync(path.join(directory, RUNTIME_MARKER), "utf8"));
  } catch (error) {
    if (error && typeof error === "object" && error.code === "ENOENT") return undefined;
    throw new TypeError(`invalid ${RUNTIME_MARKER}: ${error?.message ?? String(error)}`);
  }
  if (!isPlainObject(marker) || typeof marker.executable !== "string") {
    throw new TypeError(`invalid ${RUNTIME_MARKER}: missing executable`);
  }
  if (!source.SNAGENTIC_EXECUTABLE) source.SNAGENTIC_EXECUTABLE = marker.executable;
  return {
    executable: marker.executable,
    version: typeof marker.version === "string" ? marker.version : undefined,
    protocolCompatible: marker.protocol === EXTENSION_PROTOCOL,
  };
}

function isExecutableFile(candidate) {
  try {
    if (!statSync(candidate).isFile()) return false;
    if (process.platform !== "win32") accessSync(candidate, fsConstants.X_OK);
    return true;
  } catch {
    return false;
  }
}

export function findOnPath(name, source = process.env, platform = process.platform) {
  const searchPath = source.PATH ?? source.Path ?? "";
  const delimiter = platform === "win32" ? ";" : ":";
  // Only real executables: Windows .cmd/.bat shims cannot be spawned without a shell.
  const fileName = platform === "win32" ? `${name}.exe` : name;
  for (const directory of searchPath.split(delimiter)) {
    if (!directory || !path.isAbsolute(directory)) continue;
    const candidate = path.join(directory, fileName);
    if (isExecutableFile(candidate)) return candidate;
  }
  return undefined;
}

function validateExecutablePath(value, name) {
  if (
    typeof value !== "string" ||
    value.length > 1_024 ||
    value.includes("\0") ||
    !path.isAbsolute(value)
  ) {
    throw new TypeError(`${name} must be an absolute path to the snagentic executable`);
  }
  return value;
}

// Chooses how snagentic runs:
//   SNAGENTIC_RUNTIME=native|python|docker forces a mode;
//   otherwise SNAGENTIC_PYTHON (explicit developer override), then an installed native
//   executable (SNAGENTIC_EXECUTABLE, the install marker, or `snagentic` on PATH), then the
//   Docker Compose `cli` service as the contributor fallback.
export function selectRuntime(source = process.env, { findExecutable = findOnPath } = {}) {
  const forced = source.SNAGENTIC_RUNTIME;
  if (forced !== undefined && forced !== "" && !["native", "python", "docker"].includes(forced)) {
    throw new TypeError("SNAGENTIC_RUNTIME must be native, python or docker");
  }
  const python = selectPythonOverride(source);
  if (forced === "python" || (!forced && python !== undefined)) {
    if (python === undefined) throw new TypeError("SNAGENTIC_RUNTIME=python requires SNAGENTIC_PYTHON");
    return { mode: "python", executable: python };
  }
  if (forced === "docker") return { mode: "docker", executable: "docker" };
  const explicit = source.SNAGENTIC_EXECUTABLE
    ? validateExecutablePath(source.SNAGENTIC_EXECUTABLE, "SNAGENTIC_EXECUTABLE")
    : undefined;
  const native = explicit ?? findExecutable("snagentic", source);
  if (native !== undefined) return { mode: "native", executable: native };
  if (forced === "native") {
    throw new TypeError(
      "SNAGENTIC_RUNTIME=native but no snagentic executable was found; " +
        "install snagentic or set SNAGENTIC_EXECUTABLE",
    );
  }
  return { mode: "docker", executable: "docker" };
}

export function validateCredentialNames(names) {
  if (!Array.isArray(names)) {
    throw new TypeError("configuration credential names must be an array");
  }
  const uniqueNames = [...new Set(names)];
  for (const name of uniqueNames) {
    if (typeof name !== "string" || !/^[A-Za-z_][A-Za-z0-9_]*$/u.test(name)) {
      throw new TypeError(`configuration references an invalid credential variable: ${String(name)}`);
    }
    if (
      !APPROVED_CREDENTIAL_ENVIRONMENT_VARIABLES.includes(name) &&
      !INSTANCE_CREDENTIAL_PATTERN.test(name)
    ) {
      throw new TypeError(
        `configuration references unapproved credential variable ${name}; ` +
          "add it to the reviewed extension allowlist before use",
      );
    }
  }
  return uniqueNames;
}

export function childEnvironment(source = process.env, credentialNames = []) {
  const environment = {};
  for (const key of [...HOST_ENVIRONMENT_KEYS, ...validateCredentialNames(credentialNames)]) {
    if (typeof source[key] === "string") {
      environment[key] = source[key];
    }
  }
  environment.PYTHONUNBUFFERED = "1";
  return environment;
}

export function containerEnvironmentNames(source = process.env, credentialNames = []) {
  const names = [...validateCredentialNames(credentialNames)];
  for (const key of PROXY_TLS_ENVIRONMENT_KEYS) {
    if (typeof source[key] === "string") {
      names.push(key);
    }
  }
  return [...new Set(names)];
}

function environmentFlags(names) {
  return names.flatMap((name) => ["-e", name]);
}

export function buildDockerArguments(command, args, source = process.env, credentialNames = []) {
  return [
    "compose",
    "run",
    "--rm",
    "-T",
    ...environmentFlags(containerEnvironmentNames(source, credentialNames)),
    "cli",
    ...buildCliArguments(command, args),
  ];
}

export function buildCliLauncher(command, args, source = process.env, credentialNames = []) {
  return buildLauncherForArgv(buildCliArguments(command, args), source, credentialNames);
}

export function buildLauncherForArgv(cliArgv, source = process.env, credentialNames = []) {
  const runtime = selectRuntime(source);
  if (runtime.mode === "native") {
    return {
      executable: runtime.executable,
      argv: [...cliArgv],
      environment: childEnvironment(source, credentialNames),
      mode: "native",
    };
  }
  if (runtime.mode === "python") {
    return {
      executable: runtime.executable,
      argv: ["-m", "snagentic", ...cliArgv],
      environment: childEnvironment(source, credentialNames),
      mode: "python",
    };
  }
  return {
    executable: "docker",
    argv: [
      "compose",
      "run",
      "--rm",
      "-T",
      ...environmentFlags(containerEnvironmentNames(source, credentialNames)),
      "cli",
      ...cliArgv,
    ],
    environment: childEnvironment(source, credentialNames),
    mode: "docker",
  };
}

function cappedCollector(maxBytes) {
  let text = "";
  let bytes = 0;
  let omittedBytes = 0;
  return {
    append(chunk) {
      const chunkText = String(chunk);
      const chunkBytes = Buffer.byteLength(chunkText);
      const remaining = maxBytes - bytes;
      if (remaining <= 0) {
        omittedBytes += chunkBytes;
        return;
      }
      if (chunkBytes <= remaining) {
        text += chunkText;
        bytes += chunkBytes;
        return;
      }
      const prefix = truncateUtf8(chunkText, remaining);
      text += prefix;
      bytes += Buffer.byteLength(prefix);
      omittedBytes += chunkBytes - Buffer.byteLength(prefix);
    },
    value() {
      return { text, omittedBytes };
    },
  };
}

export function formatCappedOutput(parsed, rawText, omittedBytes) {
  if (omittedBytes === 0) {
    return stringifyBounded(parsed);
  }
  return stringifyBounded({
    ok: parsed?.ok === true,
    truncated: true,
    omittedBytes,
    outputPrefix: rawText,
  });
}

export function parseCliFailure(raw, fallback) {
  try {
    const parsed = JSON.parse(raw);
    if (isPlainObject(parsed) && parsed.ok === false && typeof parsed.error === "string") {
      return failure(parsed.error);
    }
  } catch {
    // Fall through to a bounded plain-text error for non-JSON process failures.
  }
  return failure(raw || fallback);
}

async function resolveSafeConfigPath(cwd, config = "config/snagentic.yaml") {
  const root = await realpath(cwd);
  const target = await realpath(path.resolve(root, config));
  const relative = path.relative(root, target);
  if (relative === ".." || relative.startsWith(`..${path.sep}`) || path.isAbsolute(relative)) {
    throw new TypeError("config must resolve inside the repository working directory");
  }
  return {
    hostPath: target,
    containerPath: path.posix.join("/workspace", ...relative.split(path.sep)),
  };
}

export function buildInspectionLauncher(
  configPaths,
  environment,
  source = process.env,
) {
  const runtime = selectRuntime(source);
  if (runtime.mode === "native") {
    return {
      executable: runtime.executable,
      argv: [
        "--json",
        "--config",
        configPaths.hostPath,
        ...(environment ? ["--environment", environment] : []),
        "profile",
      ],
      environment: childEnvironment(source),
      mode: "native",
    };
  }
  if (runtime.mode === "python") {
    return {
      executable: runtime.executable,
      argv: ["-c", CONFIG_INSPECTION_SCRIPT, configPaths.hostPath, environment ?? ""],
      environment: childEnvironment(source),
      mode: "python",
    };
  }
  return {
    executable: "docker",
    argv: [
      "compose",
      "run",
      "--rm",
      "-T",
      ...environmentFlags(containerEnvironmentNames(source)),
      "--entrypoint",
      "python",
      "cli",
      "-c",
      CONFIG_INSPECTION_SCRIPT,
      configPaths.containerPath,
      environment ?? "",
    ],
    environment: childEnvironment(source),
    mode: "docker",
  };
}

export async function inspectConfigProfile(
  args,
  {
    cwd = REPOSITORY_ROOT,
    spawnImpl = spawn,
    sourceEnvironment = process.env,
    timeoutMs = DEFAULT_TIMEOUT_MS,
  } = {},
) {
  const configPaths = await resolveSafeConfigPath(cwd, args.config);
  const launcher = buildInspectionLauncher(configPaths, args.environment, sourceEnvironment);
  return new Promise((resolve, reject) => {
    const stdout = cappedCollector(MAX_OUTPUT_BYTES);
    const stderr = cappedCollector(MAX_OUTPUT_BYTES);
    let settled = false;
    let timer;
    let child;

    const fail = (error) => {
      if (settled) {
        return;
      }
      settled = true;
      clearTimeout(timer);
      reject(error);
    };

    try {
      child = spawnImpl(launcher.executable, launcher.argv, {
        cwd,
        env: launcher.environment,
        shell: false,
        stdio: ["ignore", "pipe", "pipe"],
      });
    } catch (error) {
      fail(error);
      return;
    }

    child.stdout?.setEncoding("utf8");
    child.stderr?.setEncoding("utf8");
    child.stdout?.on("data", (chunk) => stdout.append(chunk));
    child.stderr?.on("data", (chunk) => stderr.append(chunk));
    child.on("error", fail);
    child.on("close", (code) => {
      if (settled) {
        return;
      }
      settled = true;
      clearTimeout(timer);
      const standardOutput = stdout.value();
      const standardError = stderr.value();
      const raw = (code === 0 ? standardOutput.text : standardError.text).trim();
      if (code !== 0) {
        try {
          const parsed = JSON.parse(raw);
          if (isPlainObject(parsed) && typeof parsed.error === "string") {
            reject(new Error(parsed.error));
            return;
          }
        } catch {
          // Use the bounded raw error below.
        }
        reject(new Error(raw || `configuration inspection exited with code ${String(code)}`));
        return;
      }
      if (standardOutput.omittedBytes > 0) {
        reject(new Error("configuration inspection output exceeded the extension limit"));
        return;
      }
      try {
        const parsed = JSON.parse(raw);
        // The native `profile` command nests its summary under `result`.
        const profile = isPlainObject(parsed?.result) ? parsed.result : parsed;
        if (
          !isPlainObject(parsed) ||
          parsed.ok !== true ||
          typeof profile.environment !== "string" ||
          typeof profile.kind !== "string"
        ) {
          throw new TypeError("configuration inspection returned an invalid result");
        }
        resolve({
          environment: profile.environment,
          kind: profile.kind,
          credentialNames: validateCredentialNames(profile.credential_names),
        });
      } catch (error) {
        reject(error);
      }
    });

    timer = setTimeout(() => {
      child.kill("SIGTERM");
      fail(new Error(`configuration inspection timed out after ${timeoutMs}ms`));
    }, timeoutMs);
    timer.unref?.();
  });
}

export function runPythonCli(
  command,
  args,
  {
    cwd = REPOSITORY_ROOT,
    spawnImpl = spawn,
    sourceEnvironment = process.env,
    credentialNames = [],
    timeoutMs = command === "diagnostics" ? DIAGNOSTICS_TIMEOUT_MS : DEFAULT_TIMEOUT_MS,
    launcher: launcherOverride,
    stdinText,
  } = {},
) {
  return new Promise((resolve) => {
    const stdout = cappedCollector(MAX_OUTPUT_BYTES);
    const stderr = cappedCollector(MAX_OUTPUT_BYTES);
    let settled = false;
    let timedOut = false;
    let child;
    let timer;
    let killTimer;
    let launcher;

    const finish = (result) => {
      if (settled) {
        return;
      }
      settled = true;
      clearTimeout(timer);
      clearTimeout(killTimer);
      resolve(result);
    };

    try {
      launcher =
        launcherOverride ?? buildCliLauncher(command, args, sourceEnvironment, credentialNames);
      child = spawnImpl(launcher.executable, launcher.argv, {
        cwd,
        env: launcher.environment,
        shell: false,
        stdio: [stdinText === undefined ? "ignore" : "pipe", "pipe", "pipe"],
      });
      if (stdinText !== undefined) {
        child.stdin?.on("error", () => {});
        child.stdin?.end(stdinText);
      }
    } catch (error) {
      resolve(failure(`failed to start snagentic: ${error instanceof Error ? error.message : String(error)}`));
      return;
    }

    child.stdout?.setEncoding("utf8");
    child.stderr?.setEncoding("utf8");
    child.stdout?.on("data", (chunk) => stdout.append(chunk));
    child.stderr?.on("data", (chunk) => stderr.append(chunk));
    child.on("error", (error) => finish(failure(`failed to start snagentic: ${error.message}`)));
    child.on("close", (code, signal) => {
      const standardOutput = stdout.value();
      const standardError = stderr.value();
      const raw = (code === 0 ? standardOutput.text : standardError.text || standardOutput.text).trim();
      if (timedOut) {
        finish(failure(`snagentic ${command} timed out after ${timeoutMs}ms`));
        return;
      }
      if (code !== 0) {
        finish(
          parseCliFailure(
            raw,
            `snagentic ${command} exited with code ${String(code)}${signal ? ` (${signal})` : ""}`,
          ),
        );
        return;
      }
      if (standardOutput.omittedBytes > 0) {
        finish({
          textResultForLlm: formatCappedOutput(
            { ok: true },
            standardOutput.text,
            standardOutput.omittedBytes,
          ),
          resultType: "success",
          data: { ok: true, truncated: true },
        });
        return;
      }
      let parsed;
      try {
        parsed = JSON.parse(raw);
      } catch {
        finish(failure(`snagentic ${command} returned invalid JSON`));
        return;
      }
      if (!isPlainObject(parsed) || parsed.ok !== true) {
        finish(failure(`snagentic ${command} returned an unsuccessful result`));
        return;
      }
      finish({
        textResultForLlm: formatCappedOutput(
          parsed,
          standardOutput.text,
          standardOutput.omittedBytes,
        ),
        resultType: "success",
        data: parsed,
      });
    });

    timer = setTimeout(() => {
      timedOut = true;
      child.kill("SIGTERM");
      killTimer = setTimeout(() => child.kill("SIGKILL"), 2_000);
      killTimer.unref?.();
    }, timeoutMs);
    timer.unref?.();
  });
}

export async function pushPermissionDecision(input, options = {}) {
  if (input?.toolName !== "snagentic_push") {
    return undefined;
  }
  let args;
  try {
    args = sanitizeArguments("push", input.toolArgs);
  } catch (error) {
    return {
      permissionDecision: "deny",
      permissionDecisionReason: error instanceof Error ? error.message : String(error),
    };
  }
  const { inspectProfile = inspectConfigProfile, ...inspectionOptions } = options;
  try {
    const profile = await inspectProfile(args, {
      ...inspectionOptions,
    });
    if (profile.kind !== "development") {
      return {
        permissionDecision: "deny",
        permissionDecisionReason:
          `snagentic push denied: environment ${profile.environment} is ${profile.kind}, ` +
          "not development",
      };
    }
  } catch (error) {
    return {
      permissionDecision: "deny",
      permissionDecisionReason:
        `snagentic could not verify the push environment: ` +
        `${error instanceof Error ? error.message : String(error)}`,
    };
  }
  return {
    permissionDecision: "ask",
    permissionDecisionReason:
      "This will apply the current reviewed change plan to a writable development ServiceNow environment.",
  };
}

export async function executeCommand(command, rawArgs, options = {}) {
  let args;
  try {
    args = sanitizeArguments(command, rawArgs);
  } catch (error) {
    return rejected(error instanceof Error ? error.message : String(error));
  }

  const { inspectProfile = inspectConfigProfile, ...runtimeOptions } = options;
  let profile;
  try {
    profile = await inspectProfile(args, runtimeOptions);
  } catch (error) {
    const message =
      `configuration profile validation failed: ` +
      `${error instanceof Error ? error.message : String(error)}`;
    return command === "push" ? denied(message) : failure(message);
  }

  if (command === "push") {
    if (profile.kind !== "development") {
      return denied(
        `push is denied for ${profile.kind} environment ${profile.environment}; ` +
          "only development profiles are writable",
      );
    }
    const validation = await runPythonCli("validate", args, {
      ...runtimeOptions,
      credentialNames: profile.credentialNames,
    });
    if (validation.resultType !== "success") {
      return validation;
    }
    if (validation.data?.result?.write_allowed !== true) {
      return denied("push is allowed only for a configured development environment");
    }
  }

  const result = await runPythonCli(command, args, {
    ...runtimeOptions,
    credentialNames: profile.credentialNames,
  });
  delete result.data;
  return result;
}
