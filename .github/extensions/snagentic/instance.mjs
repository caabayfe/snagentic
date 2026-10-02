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

// The tool catalogue below is the single source of truth for names, descriptions and JSON
// Schemas shared by the Copilot CLI extension (extension.mjs) and the MCP server
// (mcp-server.mjs), so both front ends expose identical capabilities and safety gates.
const instanceProperty = {
  instance: {
    type: "string",
    minLength: 1,
    maxLength: 63,
    pattern: "^[a-z0-9][a-z0-9_-]{0,62}$",
    description:
      "Instance folder name under instances/<name>/. Optional when only one instance exists; " +
      "required for tools that change ServiceNow.",
  },
};

const stringMap = {
  type: "object",
  additionalProperties: { type: "string", maxLength: 200, pattern: "^[A-Za-z0-9_.:\\- /]{1,200}$" },
  maxProperties: 10,
};

function instanceSchema(properties = {}, required = []) {
  return {
    type: "object",
    additionalProperties: false,
    properties: { ...instanceProperty, ...properties },
    required,
  };
}

function instanceTool(name, command, description, parameters, { skipPermission = true } = {}) {
  return {
    name,
    description,
    parameters,
    skipPermission,
    handler: (args) => executeInstanceCommand(command, args),
  };
}

const confirmProperty = {
  confirm: {
    type: "boolean",
    const: true,
    description: "Must be true, and only after the user explicitly approved this action.",
  },
};

export const instanceTools = [
  instanceTool("snagentic_instance_list", "list",
    "List configured ServiceNow instances (instances/<name>/instance.yaml), their kind and mirror branch.",
    instanceSchema()),
  instanceTool("snagentic_instance_status", "status",
    "Show mirror commit, whether the mirror is integrated into the current branch, and local metadata changes.",
    instanceSchema()),
  instanceTool("snagentic_instance_fetch", "fetch",
    "Read the instance through the Table API and commit remote state to the servicenow-remote/<name> branch. " +
      "Does not touch the working tree. Use full=true for a periodic full reconcile.",
    instanceSchema({ full: { type: "boolean", default: false } })),
  instanceTool("snagentic_instance_integrate", "integrate",
    "Merge servicenow-remote/<name> into the current branch (three-way; conflicts get git markers). " +
      "Refuses when instances/<name>/metadata has uncommitted changes.",
    instanceSchema(), { skipPermission: false }),
  instanceTool("snagentic_instance_plan", "plan",
    "Plan local edits under instances/<name>/metadata as create/update/delete writes, with collisions " +
      "against other open update sets. Returns the planId required by apply.",
    instanceSchema()),
  instanceTool("snagentic_instance_review", "review",
    "ServiceNow best-practice review. Without selectors it reviews the local changes that plan " +
      "would write, reports only findings the change introduces, and returns the apply gate " +
      "(unwaived block findings or a missing required review refuse apply); with " +
      "all/table/scope/path/customized it scans mirrored records. Findings carry a rule ID " +
      "(SN-SEC/PERF/UPG/MNT/UX-nnn), severity (block, warn, info), path, line and fix. " +
      "listRules returns the rule catalogue.",
    instanceSchema({
      all: { type: "boolean", default: false,
        description: "Review mirrored records instead of local changes." },
      table: { type: "string", pattern: "^[a-z0-9_]{1,80}$" },
      scope: { type: "string", pattern: "^[A-Za-z0-9_$]{1,80}$" },
      path: { type: "string", maxLength: 300,
        description: "Record folder or subtree, repository or instance relative." },
      customized: { type: "boolean", default: false,
        description: "Only customer-updated records from model/customer-updates.yaml." },
      rules: { type: "array", maxItems: 30,
        items: { type: "string", pattern: "^SN-[A-Z]{2,5}-\\d{3}$" } },
      minSeverity: { type: "string", enum: ["block", "warn", "info"], default: "info" },
      limit: { type: "integer", minimum: 1, maximum: 2000, default: 200 },
      listRules: { type: "boolean", default: false },
    })),
  instanceTool("snagentic_instance_review_record", "review-record",
    "Record a review verdict (approve or reject) for the current plan, bound to its planId. Writes " +
      "instances/<name>/reviews/<planId>.yaml. Approve is refused while unwaived blocking findings " +
      "remain. Instances with gate.require_review need an approving record before apply.",
    instanceSchema({
      planId: { type: "string", pattern: "^[0-9a-f]{16}$" },
      verdict: { type: "string", enum: ["approve", "reject"] },
      reviewer: { type: "string", pattern: "^[A-Za-z0-9_.@ -]{1,80}$" },
      notes: { type: "string", minLength: 10, maxLength: 4000,
        description: "Condensed review report: verdict reasons and findings." },
    }, ["planId", "verdict", "reviewer", "notes"])),
  instanceTool("snagentic_instance_apply", "apply",
    "After explicit user approval of a plan, write it into agent-owned update sets on a development " +
      "instance, then refetch touched records into the mirror. Refused while the standards gate " +
      "fails (unwaived block findings, or no approving review when gate.require_review is set).",
    instanceSchema({
      planId: { type: "string", pattern: "^[0-9a-f]{16}$" },
      label: { type: "string", pattern: "^[A-Za-z0-9_.-]{1,40}$",
        description: "Update set label (default: current git branch)." },
      allowCollisions: { type: "boolean", default: false,
        description: "Only when the user accepted writing records held in other open update sets." },
      ...confirmProperty,
    }, ["instance", "planId", "confirm"]),
    { skipPermission: false }),
  instanceTool("snagentic_instance_update_sets", "update-sets",
    "List mirrored open and recently changed update sets with owner, state and change counts.",
    instanceSchema()),
  instanceTool("snagentic_instance_collisions", "collisions",
    "Report records that are captured in more than one open update set.", instanceSchema()),
  instanceTool("snagentic_instance_activity", "activity",
    "Summarize who is working on what, by user, application and update set.", instanceSchema()),
  instanceTool("snagentic_instance_index", "index",
    "Rebuild the local search and dependency index for the instance mirror.", instanceSchema()),
  instanceTool("snagentic_instance_search", "search",
    "Search indexed metadata (names, scripts and fields) of the local instance mirror.",
    instanceSchema({
      text: { type: "string", minLength: 1, maxLength: 1000 },
      table: { type: "string", pattern: "^[A-Za-z0-9_$]{1,80}$", description: "sys_class_name filter" },
      limit: { type: "integer", minimum: 1, maximum: 500, default: 50 },
    }, ["text"])),
  instanceTool("snagentic_instance_refs", "refs",
    "Find records that reference a table, script include, event or system property.",
    instanceSchema({ target: { type: "string", pattern: "^[A-Za-z0-9_.$:-]{1,200}$" } },
      ["target"])),
  instanceTool("snagentic_instance_table", "table",
    "Start here to understand a table: fields (types, references, choices), inheritance, and " +
      "every business rule, client script, UI policy, UI action, ACL, notification, event, SLA " +
      "and data policy that applies (own and inherited), each with the path of its mirrored code.",
    instanceSchema({
      table: { type: "string", pattern: "^[a-z0-9_]{1,80}$" },
      inherited: { type: "boolean", default: true,
        description: "include behaviour inherited from parent tables" },
    }, ["table"])),
  instanceTool("snagentic_instance_docs", "docs",
    "Build or validate audience-oriented Markdown + MkDocs documentation, scaffold curated " +
      "capability/process/guide sources, or migrate legacy narrative blocks.",
    instanceSchema({
      mode: {
        type: "string",
        enum: ["build", "check", "scaffold", "migrate"],
        default: "build",
      },
      type: {
        type: "string",
        enum: ["capability", "process", "guide"],
        description: "Required only when mode=scaffold.",
      },
      id: {
        type: "string",
        pattern: "^[a-z0-9][a-z0-9-]{0,79}$",
        description: "Stable document id required only when mode=scaffold.",
      },
      strict: {
        type: "boolean",
        default: false,
        description: "Also run mkdocs build --strict for build/check modes.",
      },
    })),
  instanceTool("snagentic_instance_ops_list", "ops-list",
    "List supported ServiceNow CI/CD API operations (plugins, apps, update sets, ATF, scans).",
    instanceSchema()),
  instanceTool("snagentic_instance_ops_run", "ops-run",
    "Run a CI/CD API operation on a development instance after explicit approval " +
      "(operation 'progress' is read-only). Prefer this over UI recipes.",
    instanceSchema({
      operation: { type: "string", pattern: "^[a-z_]{1,40}(\\.[a-z_]{1,40})?$" },
      params: stringMap,
      wait: { type: "boolean", default: true },
      confirm: { type: "boolean" },
    }, ["instance", "operation"]),
    { skipPermission: false }),
  instanceTool("snagentic_instance_promote", "promote",
    "Complete the agent update sets for a label on a development instance and write a content-free " +
      "promotion manifest. Moving them to test/production stays with the supported deployment process.",
    instanceSchema({
      label: { type: "string", pattern: "^[A-Za-z0-9_.-]{1,40}$" },
      ...confirmProperty,
    }, ["instance", "confirm"]),
    { skipPermission: false }),
  instanceTool("snagentic_instance_scan", "scan",
    "After apply, run ServiceNow Instance Scan on a development instance as the platform's second " +
      "opinion: point scans of every record in the agent update sets for a label (default: current " +
      "branch), or one suite scan of the update sets when suite is given. Needs explicit approval. " +
      "The report is stored and summarised in the promotion manifest.",
    instanceSchema({
      label: { type: "string", pattern: "^[A-Za-z0-9_.-]{1,40}$" },
      updateSets: { type: "array", maxItems: 20,
        items: { type: "string", pattern: "^[0-9a-f]{32}$" } },
      targets: { type: "array", maxItems: 50,
        items: { type: "string", pattern: "^[a-z0-9_]{1,80}:[0-9a-f]{32}$" },
        description: "Extra records to point scan, as table:sys_id." },
      suite: { type: "string", pattern: "^[0-9a-f]{32}$",
        description: "scan_check_suite sys_id for a suite scan of the update sets." },
      ...confirmProperty,
    }, ["instance", "confirm"]),
    { skipPermission: false }),
  instanceTool("snagentic_instance_scan_results", "scan-results",
    "Read-only: Instance Scan findings (check, category, priority, record, resolution) for scan " +
      "result sys_ids, a CI/CD progress id, or the last scan stored for a label.",
    instanceSchema({
      results: { type: "array", maxItems: 50,
        items: { type: "string", pattern: "^[0-9a-f]{32}$" } },
      progressId: { type: "string", pattern: "^[0-9a-f]{32}$" },
      label: { type: "string", pattern: "^[A-Za-z0-9_.-]{1,40}$" },
    })),
  instanceTool("snagentic_instance_ui_list", "ui-list",
    "List Playwright UI recipes for operations that have no supported API.", instanceSchema()),
  instanceTool("snagentic_instance_ui_run", "ui-run",
    "Run a Playwright UI recipe on a development instance (dryRun=true shows the steps first). " +
      "Screenshots and a post-login trace are stored under .snagentic/<name>/ui/.",
    instanceSchema({
      recipe: { type: "string", pattern: "^[a-z0-9][a-z0-9-]{0,62}$" },
      params: stringMap,
      dryRun: { type: "boolean", default: false },
      confirm: { type: "boolean" },
    }, ["instance", "recipe"]),
    { skipPermission: false }),
];
