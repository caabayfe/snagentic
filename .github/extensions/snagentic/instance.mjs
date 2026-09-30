// Copilot CLI tools for `snagentic instance ...` (multi-instance mirror, update sets,
// documentation, CI/CD operations, promotion and Playwright UI recipes).

import { lstatSync, readdirSync, readFileSync } from "node:fs";
import path from "node:path";

import {
  INSTANCE_CREDENTIAL_PATTERN,
  WORKSPACE_ROOT,
  buildLauncherForArgv,
  childEnvironment,
  runPythonCli,
  selectRuntime,
  usesEnvironmentCredentials,
} from "./lib.mjs";

const REPOSITORY_ROOT = WORKSPACE_ROOT;

const MINUTE = 60_000;
const INSTANCE_NAME = /^[a-z0-9][a-z0-9_-]{0,62}$/u;
const IDENTIFIER = /^[A-Za-z0-9_$]{1,80}$/u;
const TABLE_NAME = /^[a-z0-9_]{1,80}$/u;
const TARGET = /^[A-Za-z0-9_.$:-]{1,200}$/u;
const PLAN_ID = /^[0-9a-f]{16}$/u;
const LABEL = /^[A-Za-z0-9_.-]{1,40}$/u;
const OPERATION = /^[a-z_]{1,40}(\.[a-z_]{1,40})?$/u;
const RECIPE = /^[a-z0-9][a-z0-9-]{0,62}$/u;
const DOCUMENT_ID = /^[a-z0-9][a-z0-9-]{0,79}$/u;
const PARAM_KEY = /^[a-z][a-z0-9_]{0,62}$/u;
const PARAM_VALUE = /^[A-Za-z0-9_.:\- /]{1,200}$/u;
const RULE_ID = /^SN-[A-Z]{2,5}-\d{3}$/u;
const RECORD_PATH = /^(?!-)(?!.*(^|\/)\.\.(\/|$))[A-Za-z0-9_.\/-]{1,300}$/u;
const SEVERITIES = ["block", "warn", "info"];
const SYS_ID = /^[0-9a-f]{32}$/u;
const SCAN_TARGET = /^[a-z0-9_]{1,80}:[0-9a-f]{32}$/u;
const REVIEWER = /^[A-Za-z0-9_.@ -]{1,80}$/u;

const INSTANCE_INSPECTION_SCRIPT = String.raw`
import json
import sys
from pathlib import Path

from snagentic.instance.config import InstanceRegistry

try:
    config = InstanceRegistry(Path.cwd()).load(sys.argv[1] or None)
    auth = config.auth
    names = auth.credential_names()
    names += [config.ui.username_env, config.ui.password_env]
    print(json.dumps({
        "ok": True,
        "instance": config.name,
        "kind": config.kind,
        "credential_names": sorted({name for name in names if name}),
    }))
except Exception as exc:
    print(json.dumps({"ok": False, "error": str(exc)}), file=sys.stderr)
    raise SystemExit(2)
`;

// command -> allowed keys, whether it writes to ServiceNow, timeout, argv builder.
export const INSTANCE_COMMANDS = Object.freeze({
  list: { keys: [], remote: false, argv: () => ["list"] },
  status: { keys: [], remote: false, argv: () => ["status"] },
  fetch: {
    keys: ["full"],
    remote: true,
    timeoutMs: 60 * MINUTE,
    argv: (a) => ["fetch", ...(a.full ? ["--full"] : [])],
  },
  integrate: { keys: [], remote: false, argv: () => ["integrate"] },
  plan: { keys: [], remote: false, argv: () => ["plan"] },
  review: {
    keys: ["all", "table", "scope", "path", "customized", "rules", "minSeverity", "limit",
      "listRules"],
    remote: false,
    timeoutMs: 20 * MINUTE,
    argv: (a) => [
      "review",
      ...(a.listRules ? ["--rules"] : []),
      ...(a.all ? ["--all"] : []),
      ...(a.table ? ["--table", a.table] : []),
      ...(a.scope ? ["--scope", a.scope] : []),
      ...(a.path ? ["--path", a.path] : []),
      ...(a.customized ? ["--customized"] : []),
      ...(a.rules ?? []).flatMap((rule) => ["--rule", rule]),
      ...(a.minSeverity ? ["--min-severity", a.minSeverity] : []),
      ...(a.limit ? ["--limit", String(a.limit)] : []),
    ],
  },
  "review-record": {
    keys: ["planId", "verdict", "reviewer", "notes"],
    remote: false,
    timeoutMs: 20 * MINUTE,
    argv: (a) => [
      "review-record", "--plan-id", a.planId, "--verdict", a.verdict,
      `--reviewer=${a.reviewer}`, `--notes=${a.notes}`,
    ],
  },
  apply: {
    keys: ["planId", "confirm", "label", "allowCollisions"],
    remote: true,
    writes: () => true,
    timeoutMs: 20 * MINUTE,
    argv: (a) => [
      "apply",
      "--plan-id",
      a.planId,
      "--confirm",
      ...(a.label ? ["--label", a.label] : []),
      ...(a.allowCollisions ? ["--allow-collisions"] : []),
    ],
  },
  "update-sets": { keys: [], remote: false, argv: () => ["update-sets"] },
  collisions: { keys: [], remote: false, argv: () => ["collisions"] },
  activity: { keys: [], remote: false, argv: () => ["activity"] },
  index: { keys: [], remote: false, timeoutMs: 10 * MINUTE, argv: () => ["index"] },
  search: {
    keys: ["text", "table", "limit"],
    remote: false,
    argv: (a) => [
      "search",
      a.text,
      ...(a.table ? ["--table", a.table] : []),
      "--limit",
      String(a.limit),
    ],
  },
  refs: { keys: ["target"], remote: false, argv: (a) => ["refs", a.target] },
  table: {
    keys: ["table", "inherited"],
    remote: false,
    argv: (a) => ["table", a.table, ...(a.inherited === false ? ["--no-inherited"] : [])],
  },
  docs: {
    keys: ["mode", "type", "id", "strict"],
    remote: false,
    timeoutMs: 10 * MINUTE,
    argv: (a) => [
      "docs",
      a.mode,
      ...(a.type ? ["--type", a.type] : []),
      ...(a.id ? ["--id", a.id] : []),
      ...(a.strict ? ["--strict"] : []),
    ],
  },
  "ops-list": { keys: [], remote: false, argv: () => ["ops-list"] },
  "ops-run": {
    keys: ["operation", "params", "confirm", "wait"],
    remote: true,
    writes: (a) => a.operation !== "progress",
    timeoutMs: 20 * MINUTE,
    argv: (a) => [
      "ops-run",
      a.operation,
      ...paramFlags(a.params),
      ...(a.confirm ? ["--confirm"] : []),
      ...(a.wait === false ? ["--no-wait"] : []),
    ],
  },
  promote: {
    keys: ["label", "confirm"],
    remote: true,
    writes: () => true,
    timeoutMs: 10 * MINUTE,
    argv: (a) => ["promote", ...(a.label ? ["--label", a.label] : []), "--confirm"],
  },
  scan: {
    keys: ["label", "updateSets", "targets", "suite", "confirm"],
    remote: true,
    writes: () => true,
    timeoutMs: 60 * MINUTE,
    argv: (a) => [
      "scan",
      ...(a.label ? ["--label", a.label] : []),
      ...(a.updateSets ?? []).flatMap((id) => ["--update-set", id]),
      ...(a.targets ?? []).flatMap((target) => ["--target", target]),
      ...(a.suite ? ["--suite", a.suite] : []),
      "--confirm",
    ],
  },
  "scan-results": {
    keys: ["results", "progressId", "label"],
    remote: true,
    timeoutMs: 10 * MINUTE,
    argv: (a) => [
      "scan-results",
      ...(a.results ?? []).flatMap((id) => ["--result", id]),
      ...(a.progressId ? ["--progress-id", a.progressId] : []),
      ...(a.label ? ["--label", a.label] : []),
    ],
  },
  "ui-list": { keys: [], remote: false, argv: () => ["ui", "list"] },
  "ui-run": {
    keys: ["recipe", "params", "dryRun", "confirm"],
    remote: true,
    writes: (a) => a.dryRun !== true && a.recipe !== "export-app-inventory",
    timeoutMs: 30 * MINUTE,
    argv: (a) => [
      "ui",
      "run",
      a.recipe,
      ...paramFlags(a.params),
      ...(a.dryRun ? ["--dry-run"] : []),
      ...(a.confirm ? ["--confirm"] : []),
    ],
  },
});

function paramFlags(params = {}) {
  return Object.entries(params).flatMap(([key, value]) => ["--param", `${key}=${value}`]);
}

function result(resultType, message) {
  return { textResultForLlm: JSON.stringify({ ok: false, error: message }), resultType };
}

function requireString(value, name, pattern) {
  if (typeof value !== "string" || !pattern.test(value)) {
    throw new TypeError(`${name} is missing or contains unsupported characters`);
  }
  return value;
}

function optionalString(value, name, pattern) {
  return value === undefined ? undefined : requireString(value, name, pattern);
}

function optionalBoolean(value, name) {
  if (value !== undefined && typeof value !== "boolean") {
    throw new TypeError(`${name} must be a boolean`);
  }
  return value;
}

function sanitizeParams(value) {
  if (value === undefined) return {};
  if (value === null || typeof value !== "object" || Array.isArray(value)) {
    throw new TypeError("params must be an object of string values");
  }
  const entries = Object.entries(value);
  if (entries.length > 10) throw new TypeError("params accepts at most 10 entries");
  const params = {};
  for (const [key, item] of entries) {
    requireString(key, "params key", PARAM_KEY);
    params[key] = requireString(item, `params.${key}`, PARAM_VALUE);
  }
  return params;
}

export function sanitizeInstanceArguments(command, args) {
  const spec = INSTANCE_COMMANDS[command];
  if (!spec) throw new TypeError(`unsupported instance command: ${command}`);
  if (args === null || typeof args !== "object" || Array.isArray(args)) {
    throw new TypeError("tool arguments must be an object");
  }
  const allowed = new Set(["instance", ...spec.keys]);
  for (const key of Object.keys(args)) {
    if (!allowed.has(key)) throw new TypeError(`unsupported argument for ${command}: ${key}`);
  }
  const clean = {};
  const instance = optionalString(args.instance, "instance", INSTANCE_NAME);
  if (instance !== undefined) clean.instance = instance;
  if (spec.keys.includes("full")) clean.full = optionalBoolean(args.full, "full") === true;
  if (command === "search") {
    if (typeof args.text !== "string" || !args.text.trim() || args.text.length > 1_000) {
      throw new TypeError("text must be a non-empty string of at most 1000 characters");
    }
    if (args.text.includes("\0") || args.text.startsWith("-")) {
      throw new TypeError("text contains unsupported characters");
    }
    clean.text = args.text;
    const table = optionalString(args.table, "table", IDENTIFIER);
    if (table !== undefined) clean.table = table;
    const limit = args.limit ?? 50;
    if (!Number.isInteger(limit) || limit < 1 || limit > 500) {
      throw new TypeError("limit must be an integer from 1 through 500");
    }
    clean.limit = limit;
  }
  if (command === "review") {
    for (const key of ["all", "customized", "listRules"]) {
      clean[key] = optionalBoolean(args[key], key) === true;
    }
    const table = optionalString(args.table, "table", TABLE_NAME);
    if (table !== undefined) clean.table = table;
    const scope = optionalString(args.scope, "scope", IDENTIFIER);
    if (scope !== undefined) clean.scope = scope;
    const recordPath = optionalString(args.path, "path", RECORD_PATH);
    if (recordPath !== undefined) clean.path = recordPath;
    if (args.rules !== undefined) {
      if (!Array.isArray(args.rules) || args.rules.length > 30) {
        throw new TypeError("rules must be an array of at most 30 rule IDs");
      }
      clean.rules = args.rules.map((rule) => requireString(rule, "rules[]", RULE_ID));
    }
    if (args.minSeverity !== undefined) {
      if (!SEVERITIES.includes(args.minSeverity)) {
        throw new TypeError("minSeverity must be block, warn or info");
      }
      clean.minSeverity = args.minSeverity;
    }
    if (args.limit !== undefined) {
      if (!Number.isInteger(args.limit) || args.limit < 1 || args.limit > 2000) {
        throw new TypeError("limit must be an integer from 1 through 2000");
      }
      clean.limit = args.limit;
    }
  }
  if (command === "refs") clean.target = requireString(args.target, "target", TARGET);
  if (command === "table") {
    clean.table = requireString(args.table, "table", TABLE_NAME);
    const inherited = optionalBoolean(args.inherited, "inherited");
    if (inherited !== undefined) clean.inherited = inherited;
  }
  if (command === "docs") {
    clean.mode = args.mode ?? "build";
    if (!["build", "check", "scaffold", "migrate"].includes(clean.mode)) {
      throw new TypeError("mode must be build, check, scaffold or migrate");
    }
    clean.strict = optionalBoolean(args.strict, "strict") === true;
    if (clean.mode === "scaffold") {
      if (!["capability", "process", "guide"].includes(args.type)) {
        throw new TypeError("docs scaffold requires type capability, process or guide");
      }
      clean.type = args.type;
      clean.id = requireString(args.id, "id", DOCUMENT_ID);
    } else if (args.type !== undefined || args.id !== undefined) {
      throw new TypeError("type and id are only supported for docs scaffold");
    }
  }
  if (command === "apply") {
    clean.planId = requireString(args.planId, "planId", PLAN_ID);
    const label = optionalString(args.label, "label", LABEL);
    if (label !== undefined) clean.label = label;
    clean.allowCollisions = optionalBoolean(args.allowCollisions, "allowCollisions") === true;
  }
  if (command === "review-record") {
    clean.planId = requireString(args.planId, "planId", PLAN_ID);
    if (!["approve", "reject"].includes(args.verdict)) {
      throw new TypeError("verdict must be approve or reject");
    }
    clean.verdict = args.verdict;
    clean.reviewer = requireString(args.reviewer, "reviewer", REVIEWER);
    if (typeof args.notes !== "string" || args.notes.trim().length < 10 ||
        args.notes.length > 4_000 || args.notes.includes("\0")) {
      throw new TypeError("notes must be 10 to 4000 characters");
    }
    clean.notes = args.notes;
  }
  if (command === "scan" || command === "scan-results") {
    const label = optionalString(args.label, "label", LABEL);
    if (label !== undefined) clean.label = label;
    const list = (key, pattern, max) => {
      if (args[key] === undefined) return;
      if (!Array.isArray(args[key]) || args[key].length > max) {
        throw new TypeError(`${key} must be an array of at most ${max} items`);
      }
      clean[key] = args[key].map((item) => requireString(item, `${key}[]`, pattern));
    };
    if (command === "scan") {
      list("updateSets", SYS_ID, 20);
      list("targets", SCAN_TARGET, 50);
      const suite = optionalString(args.suite, "suite", SYS_ID);
      if (suite !== undefined) clean.suite = suite;
    } else {
      list("results", SYS_ID, 50);
      const progressId = optionalString(args.progressId, "progressId", SYS_ID);
      if (progressId !== undefined) clean.progressId = progressId;
    }
  }
  if (command === "promote") {
    const label = optionalString(args.label, "label", LABEL);
    if (label !== undefined) clean.label = label;
  }
  if (command === "ops-run") {
    clean.operation = requireString(args.operation, "operation", OPERATION);
    clean.params = sanitizeParams(args.params);
    const wait = optionalBoolean(args.wait, "wait");
    if (wait !== undefined) clean.wait = wait;
  }
  if (command === "ui-run") {
    clean.recipe = requireString(args.recipe, "recipe", RECIPE);
    clean.params = sanitizeParams(args.params);
    clean.dryRun = optionalBoolean(args.dryRun, "dryRun") === true;
  }
  if (spec.keys.includes("confirm")) {
    const confirm = optionalBoolean(args.confirm, "confirm") === true;
    if (spec.writes?.(clean) && !confirm) {
      throw new TypeError(`${command} changes a ServiceNow instance and requires confirm=true`);
    }
    clean.confirm = confirm;
  }
  if (spec.writes?.(clean) && clean.instance === undefined) {
    throw new TypeError(`${command} requires an explicit instance`);
  }
  return clean;
}

export function buildInstanceArguments(command, args) {
  return [
    "--json",
    "instance",
    ...(args.instance ? ["-i", args.instance] : []),
    ...INSTANCE_COMMANDS[command].argv(args),
  ];
}

export function discoverInstanceCredentialNames(root = REPOSITORY_ROOT) {
  const names = new Set();
  let entries = [];
  try {
    entries = readdirSync(path.join(root, "instances"), { withFileTypes: true });
  } catch {
    return [];
  }
  for (const entry of entries) {
    if (!entry.isDirectory() || !INSTANCE_NAME.test(entry.name)) continue;
    let text;
    try {
      text = readFileSync(path.join(root, "instances", entry.name, "instance.yaml"), "utf8");
    } catch {
      continue;
    }
    for (const match of text.matchAll(/(?:token|username|password)_env:\s*["']?([A-Z0-9_]+)/gu)) {
      if (INSTANCE_CREDENTIAL_PATTERN.test(match[1])) names.add(match[1]);
    }
  }
  return [...names].sort();
}

export function credentialFilePath(instance, source = process.env) {
  if (!INSTANCE_NAME.test(instance ?? "")) return undefined;
  const base = source.XDG_CONFIG_HOME
    ? source.XDG_CONFIG_HOME
    : source.HOME
      ? path.join(source.HOME, ".config")
      : undefined;
  return base === undefined ? undefined : path.join(base, "snagentic", `${instance}.env`);
}

// Fills missing credential variables from ~/.config/snagentic/<instance>.env (outside the
// repository). Only the profile's allowlisted names are read; values set in the environment win.
export function withCredentialFile(source, instance, credentialNames) {
  const file = credentialFilePath(instance, source);
  if (file === undefined) return source;
  let text;
  try {
    const metadata = lstatSync(file);
    if (!metadata.isFile()) {
      throw new TypeError(`credential path is not a regular file: ${file}`);
    }
    if (process.platform !== "win32") {
      if ((metadata.mode & 0o077) !== 0) {
        throw new TypeError(`credential file must not be accessible by group or others: ${file}`);
      }
      if (typeof process.getuid === "function" && metadata.uid !== process.getuid()) {
        throw new TypeError(`credential file must be owned by the current user: ${file}`);
      }
    }
    text = readFileSync(file, "utf8");
  } catch (error) {
    if (error && typeof error === "object" && error.code === "ENOENT") return source;
    if (error instanceof TypeError) throw error;
    if (error && typeof error === "object" && error.code) throw error;
    return source;
  }
  const wanted = new Set(credentialNames.filter((name) => INSTANCE_CREDENTIAL_PATTERN.test(name)));
  const merged = { ...source };
  for (const line of text.split(/\r?\n/u)) {
    const match = /^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)=(.*)$/u.exec(line);
    if (!match || !wanted.has(match[1]) || typeof merged[match[1]] === "string") continue;
    let value = match[2].trim();
    if (value.length >= 2 && (value[0] === "'" || value[0] === '"') && value.at(-1) === value[0]) {
      value = value.slice(1, -1);
    }
    merged[match[1]] = value;
  }
  return merged;
}

export function buildInstanceInspectionLauncher(instance, source = process.env) {
  const runtime = selectRuntime(source);
  if (runtime.mode === "native") {
    return {
      executable: runtime.executable,
      argv: ["--json", "instance", ...(instance ? ["-i", instance] : []), "profile"],
      environment: childEnvironment(source),
      mode: "native",
    };
  }
  if (runtime.mode === "python") {
    return {
      executable: runtime.executable,
      argv: ["-c", INSTANCE_INSPECTION_SCRIPT, instance ?? ""],
      environment: childEnvironment(source),
      mode: "python",
    };
  }
  return {
    executable: "docker",
    argv: [
      "compose", "run", "--rm", "-T", "--entrypoint", "python", "cli",
      "-c", INSTANCE_INSPECTION_SCRIPT, instance ?? "",
    ],
    environment: childEnvironment(source),
    mode: "docker",
  };
}

export async function inspectInstance(instance, options = {}) {
  let launcher;
  try {
    launcher = buildInstanceInspectionLauncher(instance, options.sourceEnvironment);
  } catch (error) {
    throw new Error(error instanceof Error ? error.message : String(error));
  }
  const inspection = await runPythonCli("instance-inspect", {}, { ...options, launcher });
  // The native `instance profile` command nests its summary under `result`.
  const data = inspection.data?.result ?? inspection.data;
  if (inspection.resultType !== "success" || typeof data?.kind !== "string") {
    let message = "instance inspection failed";
    try {
      message = JSON.parse(inspection.textResultForLlm).error ?? message;
    } catch {
      // keep the generic message
    }
    throw new Error(message);
  }
  return { instance: data.instance, kind: data.kind, credentialNames: data.credential_names };
}

export function buildUiLauncher(credentialNames, source = process.env) {
  const flags = credentialNames.flatMap((name) => ["-e", name]);
  return {
    executable: "docker",
    argv: ["compose", "run", "--rm", "-T", ...flags, "ui"],
    environment: childEnvironment(source, credentialNames),
    mode: "docker",
  };
}

export async function executeInstanceCommand(command, rawArgs, options = {}) {
  let args;
  try {
    args = sanitizeInstanceArguments(command, rawArgs);
  } catch (error) {
    return result("rejected", error instanceof Error ? error.message : String(error));
  }
  const spec = INSTANCE_COMMANDS[command];
  const { inspect = inspectInstance, ...runtime } = options;
  let source = runtime.sourceEnvironment ?? process.env;
  let mode;
  try {
    mode = selectRuntime(source).mode;
  } catch (error) {
    return result("failure", error instanceof Error ? error.message : String(error));
  }
  let credentialNames = [];
  if (spec.remote) {
    let profile;
    try {
      profile = await inspect(args.instance, runtime);
    } catch (error) {
      const message = `instance profile validation failed: ${
        error instanceof Error ? error.message : String(error)
      }`;
      return result(spec.writes?.(args) ? "denied" : "failure", message);
    }
    if (spec.writes?.(args) && profile.kind !== "development") {
      return result(
        "denied",
        `${command} is denied for ${profile.kind} instance ${profile.instance}; ` +
          "only development instances can be changed",
      );
    }
    credentialNames = profile.credentialNames;
    // The native CLI reads the OS credential store itself; credential files are a
    // legacy aid for the Docker and direct-Python launchers or an explicit env opt-in.
    if (usesEnvironmentCredentials(mode, source)) {
      source = withCredentialFile(source, profile.instance, credentialNames);
    }
  }
  const timeoutMs = spec.timeoutMs ?? 2 * MINUTE;

  // Inside the cli container Docker is unavailable, so real UI runs are prepared by the
  // Python CLI (policy + validation + evidence folder) and executed in the ui container.
  if (command === "ui-run" && !args.dryRun && mode === "docker") {
    const prepared = await runPythonCli("instance", {}, {
      ...runtime,
      credentialNames,
      launcher: buildLauncherForArgv(
        [...buildInstanceArguments(command, args), "--prepare-only"],
        source,
        credentialNames,
      ),
    });
    if (prepared.resultType !== "success") return prepared;
    const request = prepared.data?.result?.request;
    const outcome = await runPythonCli("ui", {}, {
      ...runtime,
      credentialNames,
      timeoutMs,
      stdinText: JSON.stringify(request),
      launcher: buildUiLauncher(
        [request.username_env, request.password_env].filter((name) =>
          credentialNames.includes(name),
        ),
        source,
      ),
    });
    delete outcome.data;
    return outcome;
  }

  const outcome = await runPythonCli("instance", {}, {
    ...runtime,
    credentialNames,
    timeoutMs,
    launcher: buildLauncherForArgv(buildInstanceArguments(command, args), source, credentialNames),
  });
  delete outcome.data;
  return outcome;
}
