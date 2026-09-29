import { existsSync } from "node:fs";

import { joinSession } from "@github/copilot-sdk/extension";

import {
  EXTENSION_PROTOCOL,
  applyRuntimeFile,
  applyRuntimeMarker,
  joinSessionWithPermissionFallback,
  requestedEnvironmentVariables,
  selectRuntime,
  usesEnvironmentCredentials,
} from "./lib.mjs";
import { discoverInstanceCredentialNames, executeInstanceCommand } from "./instance.mjs";

applyRuntimeFile(process.env);
const startupWarnings = [];
let marker;
try {
  marker = applyRuntimeMarker(process.env);
} catch (error) {
  startupWarnings.push(error instanceof Error ? error.message : String(error));
}
if (marker && !marker.protocolCompatible) {
  startupWarnings.push(
    `the installed snagentic ${marker.version ?? ""} does not match extension protocol ` +
      `${EXTENSION_PROTOCOL}; run \`snagentic copilot install\` again`,
  );
}
if (marker && !existsSync(marker.executable)) {
  startupWarnings.push(
    `the snagentic executable ${marker.executable} is missing; reinstall snagentic and run ` +
      "`snagentic copilot install`",
  );
}
let runtimeMode = "docker";
try {
  runtimeMode = selectRuntime(process.env).mode;
} catch (error) {
  startupWarnings.push(error instanceof Error ? error.message : String(error));
}

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

const instanceTools = [
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

const extensionOptions = {
  requestedEnvironmentVariables: requestedEnvironmentVariables(
    runtimeMode,
    usesEnvironmentCredentials(runtimeMode) ? discoverInstanceCredentialNames() : [],
  ),
  tools: instanceTools,
};

const { session, environmentAccessDenied } = await joinSessionWithPermissionFallback(
  joinSession,
  extensionOptions,
);
if (environmentAccessDenied) {
  await session.log(
    "snagentic loaded without access to requested environment variables; " +
      "local tools remain available. Store ServiceNow credentials with " +
      "`snagentic auth login` so remote commands can use the OS credential store.",
    { level: "warning" },
  );
}
for (const warning of startupWarnings) {
  await session.log(`snagentic: ${warning}`, { level: "warning" });
}
